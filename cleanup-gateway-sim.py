import argparse
import re
import runpy
import sqlite3
from datetime import datetime
from pathlib import Path

import serial


VERIFY_SCRIPT = Path(__file__).with_name(
    "verify-sim-fragment.py"
)

verify_module = runpy.run_path(
    str(VERIFY_SCRIPT),
    run_name="verify_module",
)

PORT = verify_module["PORT"]
send_command = verify_module["send_command"]
find_pdu_line = verify_module["find_pdu_line"]
short_digest = verify_module["short_digest"]

DEFAULT_DB_PATH = Path(__file__).with_name("gateway.db")


def require_ok(
    response: list[str],
    description: str,
) -> None:
    """确认 AT 命令成功。"""

    if "OK" not in response:
        raise RuntimeError(f"{description}失败")


def load_candidates(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """读取已经安全保存且可以删除的短信分片。"""

    connection.row_factory = sqlite3.Row

    return connection.execute(
        """
        SELECT
            fragment.id,
            fragment.storage,
            fragment.sim_index,
            fragment.raw_pdu,
            fragment.message_id
        FROM sms_fragments AS fragment
        JOIN messages AS message
          ON message.id = fragment.message_id
        WHERE fragment.deleted_from_sim = 0
          AND fragment.parse_status = 'parsed'
          AND fragment.message_id IS NOT NULL
          AND message.complete = 1
        ORDER BY fragment.sim_index
        """
    ).fetchall()


def validate_candidate(row: sqlite3.Row) -> str:
    """检查数据库中的删除候选记录。"""

    if row["storage"] != "SM":
        raise ValueError(
            f"不支持的存储区：{row['storage']}"
        )

    if row["sim_index"] <= 0:
        raise ValueError("SIM 编号无效")

    raw_pdu = row["raw_pdu"].strip().upper()

    if not raw_pdu:
        raise ValueError("原始 PDU 为空")

    if len(raw_pdu) % 2 != 0:
        raise ValueError("原始 PDU 长度不是偶数")

    if re.fullmatch(r"[0-9A-F]+", raw_pdu) is None:
        raise ValueError("原始 PDU 不是有效十六进制")

    return raw_pdu


def mark_deleted(
    connection: sqlite3.Connection,
    fragment_id: int,
) -> None:
    """更新数据库中的 SIM 删除状态。"""

    deleted_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    cursor = connection.execute(
        """
        UPDATE sms_fragments
        SET deleted_from_sim = 1,
            deleted_at = ?,
            delete_error = NULL
        WHERE id = ?
          AND deleted_from_sim = 0
        """,
        (
            deleted_at,
            fragment_id,
        ),
    )

    if cursor.rowcount != 1:
        raise RuntimeError("数据库删除状态更新失败")

    connection.commit()


def record_error(
    connection: sqlite3.Connection,
    fragment_id: int,
    error: Exception,
) -> None:
    """记录删除失败原因。"""

    connection.execute(
        """
        UPDATE sms_fragments
        SET delete_error = ?
        WHERE id = ?
          AND deleted_from_sim = 0
        """,
        (
            str(error),
            fragment_id,
        ),
    )

    connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量验证并删除已安全保存的 SIM 短信。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行删除",
    )

    args = parser.parse_args()
    database_path = args.db.resolve()

    connection = sqlite3.connect(database_path)

    candidates = load_candidates(connection)

    print(f"数据库：{database_path}")
    print(f"符合安全条件的分片：{len(candidates)}")

    if not candidates:
        connection.close()
        print("没有需要删除的 SIM 短信。")
        return

    if not args.execute:
        print()
        print("当前为预演模式：")

        for row in candidates:
            print(
                f"[预览] AT+CMGD={row['sim_index']}"
            )

        connection.close()
        return

    ser = serial.Serial(
        port=PORT,
        baudrate=115200,
        timeout=1,
        exclusive=True,
    )

    deleted_count = 0

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

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
            "选择 SM 存储区",
        )

        print()
        print("删除前 SIM 容量：")
        send_command(ser, "AT+CPMS?")

        for row in candidates:
            fragment_id = row["id"]
            sim_index = row["sim_index"]

            try:
                database_pdu = validate_candidate(row)

                print()
                print(
                    f"正在处理 SIM 编号 {sim_index}，"
                    f"数据库摘要="
                    f"{short_digest(database_pdu)}"
                )

                read_response = send_command(
                    ser,
                    f"AT+CMGR={sim_index}",
                    timeout=10.0,
                )

                require_ok(
                    read_response,
                    f"读取 SIM 编号 {sim_index}",
                )

                sim_pdu = find_pdu_line(read_response)

                if sim_pdu is None:
                    raise RuntimeError(
                        "SIM 编号不存在或没有读到 PDU"
                    )

                print(
                    "SIM 摘要="
                    f"{short_digest(sim_pdu)}"
                )

                if sim_pdu != database_pdu:
                    raise RuntimeError(
                        "SIM PDU 与数据库不一致，禁止删除"
                    )

                delete_response = send_command(
                    ser,
                    f"AT+CMGD={sim_index}",
                    timeout=10.0,
                )

                require_ok(
                    delete_response,
                    f"删除 SIM 编号 {sim_index}",
                )

                verify_response = send_command(
                    ser,
                    f"AT+CMGR={sim_index}",
                    timeout=5.0,
                )

                remaining_pdu = find_pdu_line(
                    verify_response
                )

                if remaining_pdu is not None:
                    raise RuntimeError(
                        "删除后仍然读取到 PDU"
                    )

                mark_deleted(
                    connection=connection,
                    fragment_id=fragment_id,
                )

                deleted_count += 1

                print(
                    f"SIM 编号 {sim_index}："
                    "删除并记录成功"
                )

            except Exception as error:
                record_error(
                    connection=connection,
                    fragment_id=fragment_id,
                    error=error,
                )

                print()
                print(
                    f"SIM 编号 {sim_index} 处理失败："
                    f"{error}"
                )
                print("批量操作立即停止。")
                raise

        print()
        print("删除后 SIM 容量：")
        send_command(ser, "AT+CPMS?")

    finally:
        try:
            send_command(ser, "AT+CMGF=1")
        except Exception as error:
            print(f"恢复文本模式失败：{error}")

        ser.close()
        connection.close()

        print("串口和数据库已关闭。")

    print()
    print(f"本次成功删除：{deleted_count}")


if __name__ == "__main__":
    main()