import argparse
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import serial


PORT = (
    "/dev/serial/by-id/"
    "usb-SIMCom_Wireless_Solution_A76XX_Series_LTE_Module_"
    "200806006809080000-if05-port0"
)

DEFAULT_DB_PATH = Path(__file__).with_name("gateway.db")


def send_command(
    ser: serial.Serial,
    command: str,
    timeout: float = 10.0,
) -> list[str]:
    """发送一条 AT 命令，并读取到 OK 或错误。"""

    print(f">>> {command}")

    ser.write((command + "\r\n").encode("ascii"))
    ser.flush()

    lines: list[str] = []
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        raw_line = ser.readline()

        if not raw_line:
            continue

        line = raw_line.decode(
            "utf-8",
            errors="replace",
        ).strip()

        if not line:
            continue

        lines.append(line)

        if re.fullmatch(r"[0-9A-Fa-f]+", line):
            print(f"<PDU 已隐藏，共 {len(line) // 2} 字节>")
        else:
            print(line)

        if line == "OK":
            return lines

        if line == "ERROR":
            return lines

        if line.startswith("+CME ERROR:"):
            return lines

        if line.startswith("+CMS ERROR:"):
            return lines

    raise TimeoutError(f"AT 命令等待超时：{command}")


def require_ok(
    response: list[str],
    description: str,
) -> None:
    """确认命令执行成功。"""

    if "OK" not in response:
        raise RuntimeError(f"{description}失败")


def init_database(database_path: Path) -> None:
    """创建正式短信分片表。"""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_fragments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                storage TEXT NOT NULL,
                sim_index INTEGER NOT NULL,
                status_code INTEGER,
                tpdu_length INTEGER,

                raw_pdu TEXT NOT NULL,

                sender TEXT,
                modem_time TEXT,
                dcs INTEGER,
                reference_number INTEGER,
                reference_bits INTEGER,
                total_parts INTEGER,
                part_number INTEGER,
                decoded_part TEXT,

                parse_status TEXT NOT NULL DEFAULT 'pending',
                parse_error TEXT,

                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,

                deleted_from_sim INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                delete_error TEXT,

                UNIQUE (
                    storage,
                    sim_index,
                    raw_pdu
                )
            )
            """
        )


def extract_cmgl_records(
    lines: list[str],
) -> list[dict[str, int | str]]:
    """从 AT+CMGL=4 的响应中提取短信编号和 PDU。"""

    records: list[dict[str, int | str]] = []

    position = 0

    while position < len(lines):
        line = lines[position]

        if not line.startswith("+CMGL:"):
            position += 1
            continue

        header = line.removeprefix("+CMGL:").strip()
        fields = [
            field.strip()
            for field in header.split(",")
        ]

        if len(fields) < 3:
            raise ValueError(
                f"无法解析 CMGL 头部：{line}"
            )

        try:
            sim_index = int(fields[0])
            status_code = int(fields[1])
            tpdu_length = int(fields[-1])
        except ValueError as error:
            raise ValueError(
                f"CMGL 头部数字字段无效：{line}"
            ) from error

        pdu = None
        search_position = position + 1

        while search_position < len(lines):
            candidate = lines[search_position]

            if candidate.startswith("+CMGL:"):
                break

            if candidate in {"OK", "ERROR"}:
                break

            if (
                re.fullmatch(r"[0-9A-Fa-f]+", candidate)
                and len(candidate) % 2 == 0
            ):
                pdu = candidate.upper()
                break

            search_position += 1

        if pdu is None:
            raise ValueError(
                f"SIM 编号 {sim_index} 后没有找到 PDU"
            )

        records.append(
            {
                "sim_index": sim_index,
                "status_code": status_code,
                "tpdu_length": tpdu_length,
                "raw_pdu": pdu,
            }
        )

        position = search_position + 1

    return records


def validate_pdu_length(
    raw_pdu: str,
    tpdu_length: int,
) -> None:
    """验证 PDU 总长度和模块报告的 TPDU 长度。"""

    pdu = bytes.fromhex(raw_pdu)

    if not pdu:
        raise ValueError("PDU 为空")

    smsc_length = pdu[0]
    expected_total_length = 1 + smsc_length + tpdu_length

    if len(pdu) != expected_total_length:
        raise ValueError(
            "PDU 长度不一致："
            f"实际={len(pdu)}，"
            f"预计={expected_total_length}"
        )


def save_records(
    database_path: Path,
    records: list[dict[str, int | str]],
) -> tuple[int, int]:
    """把扫描得到的原始 PDU 保存到 SQLite。"""

    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    inserted_count = 0
    existing_count = 0

    with sqlite3.connect(database_path) as connection:
        for record in records:
            raw_pdu = str(record["raw_pdu"])

            validate_pdu_length(
                raw_pdu=raw_pdu,
                tpdu_length=int(record["tpdu_length"]),
            )

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO sms_fragments (
                    storage,
                    sim_index,
                    status_code,
                    tpdu_length,
                    raw_pdu,
                    first_seen_at,
                    last_seen_at
                )
                VALUES ('SM', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(record["sim_index"]),
                    int(record["status_code"]),
                    int(record["tpdu_length"]),
                    raw_pdu,
                    now,
                    now,
                ),
            )

            if cursor.rowcount == 1:
                inserted_count += 1
            else:
                existing_count += 1

                connection.execute(
                    """
                    UPDATE sms_fragments
                    SET status_code = ?,
                        tpdu_length = ?,
                        last_seen_at = ?
                    WHERE storage = 'SM'
                      AND sim_index = ?
                      AND raw_pdu = ?
                    """,
                    (
                        int(record["status_code"]),
                        int(record["tpdu_length"]),
                        now,
                        int(record["sim_index"]),
                        raw_pdu,
                    ),
                )

    return inserted_count, existing_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="扫描 SIM 全部短信并保存原始 PDU。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite 数据库路径",
    )

    args = parser.parse_args()
    database_path = args.db.resolve()

    init_database(database_path)

    ser = serial.Serial(
        port=PORT,
        baudrate=115200,
        timeout=1,
        exclusive=True,
    )

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print(f"数据库：{database_path}")
        print(f"已打开串口：{ser.port}")

        require_ok(
            send_command(ser, "ATE0"),
            "关闭命令回显",
        )

        require_ok(
            send_command(ser, "AT+CMGF=0"),
            "切换 PDU 模式",
        )

        require_ok(
            send_command(
                ser,
                'AT+CPMS="SM","SM","SM"',
            ),
            "选择 SIM 短信存储区",
        )

        response = send_command(
            ser,
            "AT+CMGL=4",
            timeout=30.0,
        )

        require_ok(response, "扫描 SIM 短信")

        records = extract_cmgl_records(response)

        inserted_count, existing_count = save_records(
            database_path=database_path,
            records=records,
        )

        print()
        print(f"SIM 中扫描到：{len(records)} 个物理记录")
        print(f"数据库新增：{inserted_count}")
        print(f"数据库已存在：{existing_count}")
        print("本次没有删除 SIM 短信。")

    finally:
        ser.close()
        print("串口已关闭。")


if __name__ == "__main__":
    main()