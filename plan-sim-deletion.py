import argparse
import re
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).with_name("pdu-test.db")


def mask_number(number: str) -> str:
    """遮挡较长的电话号码。"""

    if len(number) <= 8:
        return number

    if number.startswith("+"):
        return number[:6] + "****" + number[-4:]

    return number[:4] + "****" + number[-4:]


def is_valid_raw_pdu(raw_pdu: str) -> bool:
    """检查原始 PDU 是否为非空、偶数长度的十六进制字符串。"""

    if not raw_pdu:
        return False

    if len(raw_pdu) % 2 != 0:
        return False

    return re.fullmatch(
        r"[0-9A-Fa-f]+",
        raw_pdu,
    ) is not None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="预演可以安全删除的 SIM 短信分片。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite 数据库路径",
    )

    args = parser.parse_args()
    database_path = args.db

    eligible: list[sqlite3.Row] = []
    blocked: list[tuple[sqlite3.Row, list[str]]] = []

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row

        rows = connection.execute(
            """
            SELECT
                fragment.id,
                fragment.storage,
                fragment.sim_index,
                fragment.sender,
                fragment.modem_time,
                fragment.raw_pdu,
                fragment.dcs,
                fragment.reference_number,
                fragment.reference_bits,
                fragment.total_parts,
                fragment.part_number,
                fragment.deleted_from_sim,

                EXISTS (
                    SELECT 1
                    FROM messages
                    WHERE messages.sender = fragment.sender
                      AND messages.modem_time = fragment.modem_time
                      AND messages.dcs = fragment.dcs
                      AND messages.reference_number =
                          fragment.reference_number
                      AND messages.reference_bits =
                          fragment.reference_bits
                      AND messages.total_parts =
                          fragment.total_parts
                      AND messages.complete = 1
                ) AS complete_message_exists

            FROM sms_fragments AS fragment
            ORDER BY
                fragment.sender,
                fragment.reference_number,
                fragment.part_number
            """
        ).fetchall()

    for row in rows:
        reasons: list[str] = []

        if row["deleted_from_sim"] != 0:
            reasons.append("数据库显示已经从 SIM 删除")

        if row["storage"] != "SM":
            reasons.append(
                f"存储区不是 SM：{row['storage']}"
            )

        if row["sim_index"] <= 0:
            reasons.append("SIM 编号无效")

        if not is_valid_raw_pdu(row["raw_pdu"]):
            reasons.append("原始 PDU 无效或不完整")

        if row["complete_message_exists"] != 1:
            reasons.append("尚未生成完整逻辑短信")

        if reasons:
            blocked.append((row, reasons))
        else:
            eligible.append(row)

    print(f"数据库：{database_path.resolve()}")
    print(f"分片总数：{len(rows)}")
    print(f"可删除分片：{len(eligible)}")
    print(f"暂不可删除：{len(blocked)}")

    if eligible:
        print()
        print("可安全删除的分片：")

        for row in eligible:
            print(
                f"SIM 编号={row['sim_index']}，"
                f"发送方={mask_number(row['sender'])}，"
                f"引用编号={row['reference_number']}，"
                f"片号={row['part_number']}/"
                f"{row['total_parts']}"
            )

        print()
        print("以下仅为命令预览，本脚本没有连接串口：")

        for row in sorted(
            eligible,
            key=lambda item: item["sim_index"],
        ):
            print(
                f"[预览] AT+CMGD={row['sim_index']}"
            )

    if blocked:
        print()
        print("暂不可删除的分片：")

        for row, reasons in blocked:
            print(
                f"SIM 编号={row['sim_index']}："
                f"{'；'.join(reasons)}"
            )


if __name__ == "__main__":
    main()