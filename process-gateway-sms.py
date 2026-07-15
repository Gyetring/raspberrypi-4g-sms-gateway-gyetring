import argparse
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).with_name("gateway.db")


GSM7_DEFAULT_ALPHABET = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ"
    "\x1bÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

GSM7_EXTENSION = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}


class UnsupportedPduError(ValueError):
    """当前解析器尚未支持的合法 PDU。"""


def decode_numeric_address(
    encoded: bytes,
    digit_count: int,
    address_type: int,
) -> str:
    """解码半字节交换格式的数字地址。"""

    digits: list[str] = []

    for value in encoded:
        for nibble in (
            value & 0x0F,
            (value >> 4) & 0x0F,
        ):
            if nibble <= 9:
                digits.append(str(nibble))
            elif nibble == 0x0F:
                continue
            else:
                raise ValueError(
                    f"号码中存在无效半字节：0x{nibble:X}"
                )

    number = "".join(digits)[:digit_count]

    type_of_number = (address_type >> 4) & 0x07

    if type_of_number == 1:
        number = "+" + number

    return number


def decode_scts(raw: bytes) -> datetime:
    """解析短信服务中心时间戳。"""

    if len(raw) != 7:
        raise ValueError("短信时间戳必须为 7 字节")

    def decode_swapped_bcd(value: int) -> int:
        return (value & 0x0F) * 10 + ((value >> 4) & 0x0F)

    year = 2000 + decode_swapped_bcd(raw[0])
    month = decode_swapped_bcd(raw[1])
    day = decode_swapped_bcd(raw[2])
    hour = decode_swapped_bcd(raw[3])
    minute = decode_swapped_bcd(raw[4])
    second = decode_swapped_bcd(raw[5])

    timezone_octet = raw[6]
    low_nibble = timezone_octet & 0x0F
    high_nibble = (timezone_octet >> 4) & 0x0F

    negative = bool(low_nibble & 0x08)
    quarter_hours = (low_nibble & 0x07) * 10 + high_nibble
    offset_minutes = quarter_hours * 15

    if negative:
        offset_minutes = -offset_minutes

    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        tzinfo=timezone(
            timedelta(minutes=offset_minutes)
        ),
    )


def decode_gsm7(
    packed_data: bytes,
    septet_count: int,
) -> str:
    """解码不带 UDH 的 GSM 7-bit 正文。"""

    septets: list[int] = []

    for index in range(septet_count):
        bit_offset = index * 7
        byte_index = bit_offset // 8
        shift = bit_offset % 8

        if byte_index >= len(packed_data):
            raise ValueError("GSM 7-bit 数据长度不足")

        value = (
            packed_data[byte_index] >> shift
        ) & 0x7F

        if (
            shift > 1
            and byte_index + 1 < len(packed_data)
        ):
            value |= (
                packed_data[byte_index + 1]
                << (8 - shift)
            ) & 0x7F

        septets.append(value)

    characters: list[str] = []
    escaped = False

    for value in septets:
        if escaped:
            characters.append(
                GSM7_EXTENSION.get(value, "�")
            )
            escaped = False
            continue

        if value == 0x1B:
            escaped = True
            continue

        if value >= len(GSM7_DEFAULT_ALPHABET):
            characters.append("�")
        else:
            characters.append(
                GSM7_DEFAULT_ALPHABET[value]
            )

    if escaped:
        characters.append("�")

    return "".join(characters)


def parse_udh(
    udh: bytes,
) -> tuple[int | None, int | None, int, int]:
    """解析 UDH 中的长短信拼接信息。"""

    reference_number = None
    reference_bits = None
    total_parts = 1
    part_number = 1

    offset = 0

    while offset < len(udh):
        if offset + 2 > len(udh):
            raise ValueError("UDH 信息元素头部不完整")

        element_id = udh[offset]
        element_length = udh[offset + 1]
        offset += 2

        element_data = udh[
            offset:
            offset + element_length
        ]
        offset += element_length

        if len(element_data) != element_length:
            raise ValueError("UDH 信息元素内容不完整")

        if element_id == 0x00 and element_length == 3:
            reference_bits = 8
            reference_number = element_data[0]
            total_parts = element_data[1]
            part_number = element_data[2]

        elif element_id == 0x08 and element_length == 4:
            reference_bits = 16
            reference_number = int.from_bytes(
                element_data[0:2],
                byteorder="big",
            )
            total_parts = element_data[2]
            part_number = element_data[3]

    return (
        reference_number,
        reference_bits,
        total_parts,
        part_number,
    )


def parse_sms_deliver(raw_pdu: str) -> dict[str, object]:
    """解析一条 SMS-DELIVER PDU。"""

    pdu = bytes.fromhex(raw_pdu)

    if not pdu:
        raise ValueError("PDU 为空")

    position = 0

    smsc_length = pdu[position]
    position += 1 + smsc_length

    if position >= len(pdu):
        raise ValueError("PDU 缺少 TPDU")

    first_octet = pdu[position]
    position += 1

    message_type = first_octet & 0x03
    has_udh = bool(first_octet & 0x40)

    if message_type != 0:
        raise UnsupportedPduError(
            f"当前只处理 SMS-DELIVER，MTI={message_type}"
        )

    sender_length = pdu[position]
    position += 1

    sender_type = pdu[position]
    position += 1

    sender_type_of_number = (sender_type >> 4) & 0x07

    if sender_type_of_number == 5:
        raise UnsupportedPduError(
            "暂不支持字母数字发送方地址"
        )

    sender_octets = (sender_length + 1) // 2
    sender_data = pdu[
        position:
        position + sender_octets
    ]
    position += sender_octets

    if len(sender_data) != sender_octets:
        raise ValueError("发送方地址不完整")

    sender = decode_numeric_address(
        encoded=sender_data,
        digit_count=sender_length,
        address_type=sender_type,
    )

    if position + 10 > len(pdu):
        raise ValueError("PDU 头部不完整")

    pid = pdu[position]
    position += 1

    dcs = pdu[position]
    position += 1

    timestamp_raw = pdu[position:position + 7]
    position += 7

    modem_time = decode_scts(timestamp_raw)

    udl = pdu[position]
    position += 1

    reference_number = None
    reference_bits = None
    total_parts = 1
    part_number = 1

    if dcs == 8:
        user_data = pdu[
            position:
            position + udl
        ]

        if len(user_data) != udl:
            raise ValueError("UCS2 用户数据长度不完整")

        if has_udh:
            if not user_data:
                raise ValueError("短信声明有 UDH，但用户数据为空")

            udhl = user_data[0]
            udh_end = 1 + udhl

            if udh_end > len(user_data):
                raise ValueError("UDH 长度超过用户数据")

            udh = user_data[1:udh_end]
            body_data = user_data[udh_end:]

            (
                reference_number,
                reference_bits,
                total_parts,
                part_number,
            ) = parse_udh(udh)
        else:
            body_data = user_data

        decoded_part = body_data.decode("utf-16-be")

    elif dcs == 0:
        if has_udh:
            raise UnsupportedPduError(
                "暂不支持带 UDH 的 GSM 7-bit 长短信"
            )

        packed_length = (udl * 7 + 7) // 8
        packed_data = pdu[
            position:
            position + packed_length
        ]

        if len(packed_data) != packed_length:
            raise ValueError("GSM 7-bit 用户数据长度不完整")

        decoded_part = decode_gsm7(
            packed_data=packed_data,
            septet_count=udl,
        )

    else:
        raise UnsupportedPduError(
            f"暂不支持 DCS={dcs}"
        )

    if total_parts <= 0:
        raise ValueError("总片数无效")

    if not 1 <= part_number <= total_parts:
        raise ValueError("当前片号无效")

    return {
        "sender": sender,
        "modem_time": modem_time.isoformat(
            timespec="seconds"
        ),
        "pid": pid,
        "dcs": dcs,
        "reference_number": reference_number,
        "reference_bits": reference_bits,
        "total_parts": total_parts,
        "part_number": part_number,
        "decoded_part": decoded_part,
    }


def mask_number(number: str) -> str:
    """遮挡较长电话号码。"""

    if len(number) <= 8:
        return number

    if number.startswith("+"):
        return number[:6] + "****" + number[-4:]

    return number[:4] + "****" + number[-4:]


def ensure_schema(connection: sqlite3.Connection) -> None:
    """创建完整短信表，并补充分片关联字段。"""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_key TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            modem_time TEXT NOT NULL,
            body TEXT NOT NULL,
            dcs INTEGER NOT NULL,
            reference_number INTEGER,
            reference_bits INTEGER,
            total_parts INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            assembled_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(sms_fragments)"
        )
    }

    if "message_id" not in columns:
        connection.execute(
            """
            ALTER TABLE sms_fragments
            ADD COLUMN message_id INTEGER
            """
        )


def parse_pending_fragments(
    connection: sqlite3.Connection,
    retry_errors: bool,
) -> tuple[int, int, int]:
    """解析尚未处理的原始 PDU。"""

    statuses = ["pending"]

    if retry_errors:
        statuses.extend(["error", "unsupported"])

    placeholders = ",".join("?" for _ in statuses)

    rows = connection.execute(
        f"""
        SELECT id, raw_pdu
        FROM sms_fragments
        WHERE parse_status IN ({placeholders})
        ORDER BY id
        """,
        statuses,
    ).fetchall()

    parsed_count = 0
    unsupported_count = 0
    error_count = 0

    for fragment_id, raw_pdu in rows:
        try:
            parsed = parse_sms_deliver(raw_pdu)

            connection.execute(
                """
                UPDATE sms_fragments
                SET sender = ?,
                    modem_time = ?,
                    dcs = ?,
                    reference_number = ?,
                    reference_bits = ?,
                    total_parts = ?,
                    part_number = ?,
                    decoded_part = ?,
                    parse_status = 'parsed',
                    parse_error = NULL
                WHERE id = ?
                """,
                (
                    parsed["sender"],
                    parsed["modem_time"],
                    parsed["dcs"],
                    parsed["reference_number"],
                    parsed["reference_bits"],
                    parsed["total_parts"],
                    parsed["part_number"],
                    parsed["decoded_part"],
                    fragment_id,
                ),
            )

            parsed_count += 1

        except UnsupportedPduError as error:
            connection.execute(
                """
                UPDATE sms_fragments
                SET parse_status = 'unsupported',
                    parse_error = ?
                WHERE id = ?
                """,
                (
                    str(error),
                    fragment_id,
                ),
            )

            unsupported_count += 1

        except Exception as error:
            connection.execute(
                """
                UPDATE sms_fragments
                SET parse_status = 'error',
                    parse_error = ?
                WHERE id = ?
                """,
                (
                    str(error),
                    fragment_id,
                ),
            )

            error_count += 1

    return (
        parsed_count,
        unsupported_count,
        error_count,
    )


def upsert_message(
    connection: sqlite3.Connection,
    message_key: str,
    sender: str,
    modem_time: str,
    body: str,
    dcs: int,
    reference_number: int | None,
    reference_bits: int | None,
    total_parts: int,
) -> int:
    """写入或更新一条完整逻辑短信。"""

    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    connection.execute(
        """
        INSERT INTO messages (
            message_key,
            sender,
            modem_time,
            body,
            dcs,
            reference_number,
            reference_bits,
            total_parts,
            complete,
            assembled_at,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)

        ON CONFLICT(message_key)
        DO UPDATE SET
            body = excluded.body,
            complete = 1,
            assembled_at = excluded.assembled_at
        """,
        (
            message_key,
            sender,
            modem_time,
            body,
            dcs,
            reference_number,
            reference_bits,
            total_parts,
            now,
            now,
        ),
    )

    row = connection.execute(
        """
        SELECT id
        FROM messages
        WHERE message_key = ?
        """,
        (message_key,),
    ).fetchone()

    if row is None:
        raise RuntimeError("无法取得 messages 记录编号")

    return int(row[0])


def assemble_messages(
    connection: sqlite3.Connection,
) -> tuple[int, int, int]:
    """生成单片短信和完整长短信。"""

    single_count = 0
    multipart_count = 0
    incomplete_count = 0

    single_rows = connection.execute(
        """
        SELECT
            id,
            sender,
            modem_time,
            decoded_part,
            dcs
        FROM sms_fragments
        WHERE parse_status = 'parsed'
          AND total_parts = 1
        ORDER BY id
        """
    ).fetchall()

    for (
        fragment_id,
        sender,
        modem_time,
        decoded_part,
        dcs,
    ) in single_rows:
        message_key = f"single:{fragment_id}"

        message_id = upsert_message(
            connection=connection,
            message_key=message_key,
            sender=sender,
            modem_time=modem_time,
            body=decoded_part,
            dcs=dcs,
            reference_number=None,
            reference_bits=None,
            total_parts=1,
        )

        connection.execute(
            """
            UPDATE sms_fragments
            SET message_id = ?
            WHERE id = ?
            """,
            (
                message_id,
                fragment_id,
            ),
        )

        single_count += 1

    groups = connection.execute(
        """
        SELECT DISTINCT
            sender,
            modem_time,
            dcs,
            reference_number,
            reference_bits,
            total_parts
        FROM sms_fragments
        WHERE parse_status = 'parsed'
          AND total_parts > 1
        ORDER BY modem_time
        """
    ).fetchall()

    for group in groups:
        (
            sender,
            modem_time,
            dcs,
            reference_number,
            reference_bits,
            total_parts,
        ) = group

        fragments = connection.execute(
            """
            SELECT
                id,
                part_number,
                decoded_part
            FROM sms_fragments
            WHERE parse_status = 'parsed'
              AND sender = ?
              AND modem_time = ?
              AND dcs = ?
              AND reference_number = ?
              AND reference_bits = ?
              AND total_parts = ?
            ORDER BY part_number
            """,
            group,
        ).fetchall()

        part_map: dict[int, tuple[int, str]] = {}
        duplicate_conflict = False

        for fragment_id, part_number, decoded_part in fragments:
            existing = part_map.get(part_number)

            if existing is None:
                part_map[part_number] = (
                    fragment_id,
                    decoded_part,
                )
            elif existing[1] != decoded_part:
                duplicate_conflict = True

        if duplicate_conflict:
            print(
                "分片冲突，未拼接："
                f"发送方={mask_number(sender)}，"
                f"引用编号={reference_number}"
            )
            incomplete_count += 1
            continue

        expected_parts = list(
            range(1, total_parts + 1)
        )
        actual_parts = sorted(part_map)

        if actual_parts != expected_parts:
            incomplete_count += 1
            continue

        complete_body = "".join(
            part_map[number][1]
            for number in expected_parts
        )

        identity = "\0".join(
            [
                sender,
                modem_time,
                str(dcs),
                str(reference_bits),
                str(reference_number),
                str(total_parts),
            ]
        )

        message_key = "multipart:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()

        message_id = upsert_message(
            connection=connection,
            message_key=message_key,
            sender=sender,
            modem_time=modem_time,
            body=complete_body,
            dcs=dcs,
            reference_number=reference_number,
            reference_bits=reference_bits,
            total_parts=total_parts,
        )

        fragment_ids = [
            part_map[number][0]
            for number in expected_parts
        ]

        placeholders = ",".join(
            "?"
            for _ in fragment_ids
        )

        connection.execute(
            f"""
            UPDATE sms_fragments
            SET message_id = ?
            WHERE id IN ({placeholders})
            """,
            (
                message_id,
                *fragment_ids,
            ),
        )

        multipart_count += 1

    return (
        single_count,
        multipart_count,
        incomplete_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="解析 gateway.db 中的短信并生成完整消息。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="重新处理 error 和 unsupported 记录",
    )

    args = parser.parse_args()
    database_path = args.db.resolve()

    with sqlite3.connect(database_path) as connection:
        ensure_schema(connection)

        (
            parsed_count,
            unsupported_count,
            error_count,
        ) = parse_pending_fragments(
            connection=connection,
            retry_errors=args.retry_errors,
        )

        (
            single_count,
            multipart_count,
            incomplete_count,
        ) = assemble_messages(connection)

    print(f"数据库：{database_path}")
    print(f"本次成功解析分片：{parsed_count}")
    print(f"暂不支持：{unsupported_count}")
    print(f"解析错误：{error_count}")
    print(f"已生成单片短信：{single_count}")
    print(f"已生成完整长短信：{multipart_count}")
    print(f"尚不完整或冲突的长短信组：{incomplete_count}")


if __name__ == "__main__":
    main()