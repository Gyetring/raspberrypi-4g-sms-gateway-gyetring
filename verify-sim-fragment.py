import argparse
import hashlib
import re
import sqlite3
import time
from pathlib import Path

import serial


PORT = (
    "/dev/serial/by-id/"
    "usb-SIMCom_Wireless_Solution_A76XX_Series_LTE_Module_"
    "200806006809080000-if05-port0"
)

DEFAULT_DB_PATH = Path(__file__).with_name("pdu-test.db")


def send_command(
    ser: serial.Serial,
    command: str,
    timeout: float = 5.0,
) -> list[str]:
    """发送 AT 命令，并读取到 OK 或错误响应。"""

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

        # 不直接打印原始 PDU，避免日志泄露短信内容。
        if re.fullmatch(r"[0-9A-Fa-f]+", line):
            print(f"<PDU 已隐藏，共 {len(line) // 2} 字节>")
        else:
            print(line)

        lines.append(line)

        if line == "OK":
            return lines

        if line == "ERROR":
            return lines

        if line.startswith("+CME ERROR:"):
            return lines

        if line.startswith("+CMS ERROR:"):
            return lines

    raise TimeoutError(f"AT 命令等待超时：{command}")


def find_pdu_line(lines: list[str]) -> str | None:
    """从 AT+CMGR 响应中提取 PDU。"""

    candidates: list[str] = []

    for line in lines:
        if re.fullmatch(r"[0-9A-Fa-f]+", line):
            if len(line) % 2 == 0:
                candidates.append(line.upper())

    if not candidates:
        return None

    return max(candidates, key=len)


def short_digest(pdu: str) -> str:
    """生成不暴露正文的 PDU 摘要。"""

    digest = hashlib.sha256(
        bytes.fromhex(pdu)
    ).hexdigest()

    return digest[:16]


def load_database_fragment(
    database_path: Path,
    sim_index: int,
) -> sqlite3.Row:
    """读取数据库中的指定 SIM 分片。"""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row

        row = connection.execute(
            """
            SELECT
                id,
                storage,
                sim_index,
                raw_pdu,
                deleted_from_sim
            FROM sms_fragments
            WHERE storage = 'SM'
              AND sim_index = ?
            LIMIT 1
            """,
            (sim_index,),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"数据库中找不到 SIM 编号 {sim_index}"
        )

    if row["deleted_from_sim"] != 0:
        raise ValueError(
            f"数据库显示 SIM 编号 {sim_index} 已被删除"
        )

    return row


def main() -> None:
    parser = argparse.ArgumentParser(
        description="比对 SIM 短信与数据库原始 PDU。",
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

    args = parser.parse_args()

    if args.sim_index <= 0:
        raise SystemExit("SIM 编号必须大于 0")

    database_path = args.db.resolve()

    row = load_database_fragment(
        database_path=database_path,
        sim_index=args.sim_index,
    )

    database_pdu = row["raw_pdu"].strip().upper()

    print(f"数据库：{database_path}")
    print(f"正在验证 SIM 编号：{args.sim_index}")
    print(f"数据库 PDU 字节数：{len(database_pdu) // 2}")
    print(f"数据库 PDU 摘要：{short_digest(database_pdu)}")

    ser = serial.Serial(
        port=PORT,
        baudrate=115200,
        timeout=1,
        exclusive=True,
    )

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print(f"已打开串口：{ser.port}")

        response = send_command(ser, "ATE0")

        if "OK" not in response:
            raise RuntimeError("ATE0 执行失败")

        response = send_command(ser, "AT+CMGF=0")

        if "OK" not in response:
            raise RuntimeError("切换 PDU 模式失败")

        response = send_command(
            ser,
            'AT+CPMS="SM","SM","SM"',
        )

        if "OK" not in response:
            raise RuntimeError("选择 SM 存储区失败")

        response = send_command(
            ser,
            f"AT+CMGR={args.sim_index}",
            timeout=10.0,
        )

        if "OK" not in response:
            raise RuntimeError(
                "SIM 短信读取失败或编号已经不存在"
            )

        sim_pdu = find_pdu_line(response)

        if sim_pdu is None:
            raise RuntimeError(
                "响应中没有找到有效的 PDU"
            )

        print(f"SIM PDU 字节数：{len(sim_pdu) // 2}")
        print(f"SIM PDU 摘要：{short_digest(sim_pdu)}")

        if sim_pdu != database_pdu:
            print()
            print("验证结果：不一致")
            print("禁止删除这个 SIM 编号。")
            raise SystemExit(1)

        print()
        print("验证结果：完全一致")
        print("该编号通过删除前身份验证。")
        print("本脚本没有执行 AT+CMGD。")

    finally:
        try:
            send_command(ser, "AT+CMGF=1")
        except Exception as error:
            print(f"恢复文本模式失败：{error}")

        ser.close()
        print("串口已关闭。")


if __name__ == "__main__":
    main()