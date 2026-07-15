import argparse
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
DEFAULT_DB_PATH = verify_module["DEFAULT_DB_PATH"]
send_command = verify_module["send_command"]
find_pdu_line = verify_module["find_pdu_line"]
short_digest = verify_module["short_digest"]


def load_delete_candidate(
    database_path: Path,
    sim_index: int,
) -> sqlite3.Row:
    """读取并检查待删除的短信分片。"""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row

        row = connection.execute(
            """
            SELECT
                fragment.id,
                fragment.storage,
                fragment.sim_index,
                fragment.raw_pdu,
                fragment.deleted_from_sim,

                EXISTS (
                    SELECT 1
                    FROM messages
                    WHERE messages.sender = fragment.sender
                      AND messages.modem_time =
                          fragment.modem_time
                      AND messages.dcs =
                          fragment.dcs
                      AND messages.reference_number =
                          fragment.reference_number
                      AND messages.reference_bits =
                          fragment.reference_bits
                      AND messages.total_parts =
                          fragment.total_parts
                      AND messages.complete = 1
                ) AS complete_message_exists

            FROM sms_fragments AS fragment
            WHERE fragment.storage = 'SM'
              AND fragment.sim_index = ?
            LIMIT 1
            """,
            (sim_index,),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"数据库中没有找到 SIM 编号 {sim_index}"
        )

    if row["deleted_from_sim"] != 0:
        raise ValueError(
            f"数据库显示 SIM 编号 {sim_index} 已经删除"
        )

    if row["complete_message_exists"] != 1:
        raise ValueError(
            "对应的完整逻辑短信尚未生成，禁止删除"
        )

    raw_pdu = row["raw_pdu"].strip()

    if not raw_pdu:
        raise ValueError("数据库中的原始 PDU 为空")

    return row


def mark_deleted(
    database_path: Path,
    fragment_id: int,
) -> None:
    """在确认 SIM 删除成功后更新数据库状态。"""

    deleted_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    with sqlite3.connect(database_path) as connection:
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
            raise RuntimeError(
                "数据库删除状态更新失败"
            )


def record_delete_error(
    database_path: Path,
    fragment_id: int,
    error_message: str,
) -> None:
    """记录删除过程中发生的错误。"""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE sms_fragments
            SET delete_error = ?
            WHERE id = ?
              AND deleted_from_sim = 0
            """,
            (
                error_message,
                fragment_id,
            ),
        )


def require_ok(
    response: list[str],
    description: str,
) -> None:
    """确认 AT 命令以 OK 结束。"""

    if "OK" not in response:
        raise RuntimeError(f"{description}失败")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="验证并安全删除单个 SIM 短信分片。",
    )
    parser.add_argument(
        "sim_index",
        type=int,
        help="SIM 卡中的短信物理编号",
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
        help="真正执行删除；省略时只进行预演",
    )

    args = parser.parse_args()

    if args.sim_index <= 0:
        raise SystemExit("SIM 编号必须大于 0")

    database_path = args.db.resolve()

    candidate = load_delete_candidate(
        database_path=database_path,
        sim_index=args.sim_index,
    )

    fragment_id = candidate["id"]
    database_pdu = candidate["raw_pdu"].strip().upper()

    print(f"数据库：{database_path}")
    print(f"待验证 SIM 编号：{args.sim_index}")
    print(
        "数据库 PDU 摘要："
        f"{short_digest(database_pdu)}"
    )

    ser = serial.Serial(
        port=PORT,
        baudrate=115200,
        timeout=1,
        exclusive=True,
    )

    deletion_attempted = False

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

        read_response = send_command(
            ser,
            f"AT+CMGR={args.sim_index}",
            timeout=10.0,
        )

        require_ok(read_response, "读取 SIM 短信")

        sim_pdu = find_pdu_line(read_response)

        if sim_pdu is None:
            raise RuntimeError(
                "没有从 SIM 响应中提取到 PDU"
            )

        print(
            "SIM PDU 摘要："
            f"{short_digest(sim_pdu)}"
        )

        if sim_pdu != database_pdu:
            raise RuntimeError(
                "SIM PDU 与数据库不一致，禁止删除"
            )

        print("删除前验证：通过")

        if not args.execute:
            print()
            print("当前为预演模式。")
            print(
                f"若执行，将发送：AT+CMGD="
                f"{args.sim_index}"
            )
            print("本次没有删除短信。")
            return

        deletion_attempted = True

        print()
        print("正在执行删除……")

        delete_response = send_command(
            ser,
            f"AT+CMGD={args.sim_index}",
            timeout=10.0,
        )

        require_ok(delete_response, "删除 SIM 短信")

        print("模块返回删除成功，正在再次确认……")

        verify_response = send_command(
            ser,
            f"AT+CMGR={args.sim_index}",
            timeout=5.0,
        )

        remaining_pdu = find_pdu_line(verify_response)

        if "OK" in verify_response and remaining_pdu is not None:
            raise RuntimeError(
                "删除后该 SIM 编号仍能读到短信"
            )

        mark_deleted(
            database_path=database_path,
            fragment_id=fragment_id,
        )

        print("删除后验证：该编号已不存在")
        print("数据库删除状态：更新成功")

    except Exception as error:
        if deletion_attempted:
            record_delete_error(
                database_path=database_path,
                fragment_id=fragment_id,
                error_message=str(error),
            )

        raise

    finally:
        try:
            send_command(ser, "AT+CMGF=1")
        except Exception as error:
            print(f"恢复文本模式失败：{error}")

        ser.close()
        print("串口已关闭。")


if __name__ == "__main__":
    main()