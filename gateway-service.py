import argparse
import queue
import re
import runpy
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import serial


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "gateway.db"

PORT = (
    "/dev/serial/by-id/"
    "usb-SIMCom_Wireless_Solution_A76XX_Series_LTE_Module_"
    "200806006809080000-if05-port0"
)


scan_module = runpy.run_path(
    str(BASE_DIR / "scan-sim-pdu.py"),
    run_name="scan_module",
)

process_module = runpy.run_path(
    str(BASE_DIR / "process-gateway-sms.py"),
    run_name="process_module",
)

verify_module = runpy.run_path(
    str(BASE_DIR / "verify-sim-fragment.py"),
    run_name="verify_module",
)


init_database = scan_module["init_database"]
extract_cmgl_records = scan_module["extract_cmgl_records"]
save_records = scan_module["save_records"]
validate_pdu_length = scan_module["validate_pdu_length"]

ensure_schema = process_module["ensure_schema"]
parse_pending_fragments = process_module["parse_pending_fragments"]
assemble_messages = process_module["assemble_messages"]

find_pdu_line = verify_module["find_pdu_line"]
short_digest = verify_module["short_digest"]


class Modem:
    """只允许读取线程调用串口 readline。"""

    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(
            port=port,
            baudrate=115200,
            timeout=1,
            exclusive=True,
        )

        self.command_lock = threading.Lock()
        self.state_lock = threading.Lock()

        self.response_lines: list[str] | None = None
        self.response_event: threading.Event | None = None

        self.events: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name="modem-reader",
            daemon=True,
        )

    def start(self) -> None:
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.reader_thread.start()

    @staticmethod
    def _is_terminal_line(line: str) -> bool:
        return (
            line in {"OK", "ERROR"}
            or line.startswith("+CME ERROR:")
            or line.startswith("+CMS ERROR:")
        )

    @staticmethod
    def _is_urc(line: str) -> bool:
        """识别模块主动上报。"""

        return (
            line.startswith("+CMTI:")
            or line == "RING"
            or line == "NO CARRIER"
            or line.startswith("+CLCC:")
            or line.startswith("VOICE CALL:")
            or line.startswith("+CEREG:")
        )

    @staticmethod
    def _print_received_line(line: str) -> None:
        if (
            re.fullmatch(r"[0-9A-Fa-f]+", line)
            and len(line) % 2 == 0
            and len(line) >= 20
        ):
            print(f"<PDU 已隐藏，共 {len(line) // 2} 字节>")
        else:
            print(line)

    def _reader_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                raw_line = self.ser.readline()

                if not raw_line:
                    continue

                line = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if not line:
                    continue

                self._print_received_line(line)

                if self._is_urc(line):
                    self.events.put(line)
                    continue

                with self.state_lock:
                    if self.response_lines is None:
                        self.events.put(line)
                        continue

                    self.response_lines.append(line)

                    if self._is_terminal_line(line):
                        if self.response_event is not None:
                            self.response_event.set()

        except Exception as error:
            self.events.put(
                f"__SERIAL_ERROR__:{error}"
            )

    def command(
        self,
        command: str,
        timeout: float = 10.0,
    ) -> list[str]:
        """串行发送命令，由唯一读取线程收集响应。"""

        with self.command_lock:
            response_event = threading.Event()

            with self.state_lock:
                if self.response_lines is not None:
                    raise RuntimeError("已有 AT 命令正在执行")

                self.response_lines = []
                self.response_event = response_event

            print(f">>> {command}")

            try:
                self.ser.write(
                    (command + "\r\n").encode("ascii")
                )
                self.ser.flush()

                if not response_event.wait(timeout):
                    raise TimeoutError(
                        f"AT 命令等待超时：{command}"
                    )

                with self.state_lock:
                    return list(self.response_lines or [])

            finally:
                with self.state_lock:
                    self.response_lines = None
                    self.response_event = None

    def close(self) -> None:
        self.stop_event.set()

        if self.ser.is_open:
            self.ser.close()

        self.reader_thread.join(timeout=2)


def require_ok(
    response: list[str],
    description: str,
) -> None:
    if "OK" not in response:
        raise RuntimeError(f"{description}失败")


def initialize_schema(database_path: Path) -> None:
    init_database(database_path)

    with sqlite3.connect(database_path) as connection:
        ensure_schema(connection)


def process_database(database_path: Path) -> None:
    """解析待处理 PDU 并生成完整逻辑短信。"""

    with sqlite3.connect(database_path) as connection:
        ensure_schema(connection)

        parsed, unsupported, errors = parse_pending_fragments(
            connection=connection,
            retry_errors=False,
        )

        singles, multipart, incomplete = assemble_messages(
            connection
        )

    print(
        "数据库处理："
        f"解析={parsed}，"
        f"不支持={unsupported}，"
        f"错误={errors}，"
        f"单片={singles}，"
        f"长短信={multipart}，"
        f"未完整={incomplete}"
    )


def parse_cmgr_record(
    response: list[str],
    sim_index: int,
) -> dict[str, int | str]:
    """把 AT+CMGR 的 PDU 响应转换为分片记录。"""

    header = next(
        (
            line
            for line in response
            if line.startswith("+CMGR:")
        ),
        None,
    )

    if header is None:
        raise RuntimeError(
            f"SIM 编号 {sim_index} 缺少 CMGR 头部"
        )

    content = header.removeprefix("+CMGR:").strip()

    try:
        status_code = int(
            content.split(",", 1)[0].strip()
        )
        tpdu_length = int(
            content.rsplit(",", 1)[1].strip()
        )
    except (ValueError, IndexError) as error:
        raise RuntimeError(
            f"无法解析 CMGR 头部：{header}"
        ) from error

    raw_pdu = find_pdu_line(response)

    if raw_pdu is None:
        raise RuntimeError(
            f"SIM 编号 {sim_index} 没有 PDU"
        )

    validate_pdu_length(
        raw_pdu=raw_pdu,
        tpdu_length=tpdu_length,
    )

    return {
        "sim_index": sim_index,
        "status_code": status_code,
        "tpdu_length": tpdu_length,
        "raw_pdu": raw_pdu,
    }


def mark_deleted(
    database_path: Path,
    fragment_id: int,
) -> None:
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
    error: Exception,
) -> None:
    with sqlite3.connect(database_path) as connection:
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


def find_delete_candidate(
    database_path: Path,
    sim_index: int,
    raw_pdu: str,
) -> sqlite3.Row | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row

        return connection.execute(
            """
            SELECT
                fragment.id,
                fragment.raw_pdu
            FROM sms_fragments AS fragment
            JOIN messages AS message
              ON message.id = fragment.message_id
            WHERE fragment.storage = 'SM'
              AND fragment.sim_index = ?
              AND fragment.raw_pdu = ?
              AND fragment.parse_status = 'parsed'
              AND fragment.deleted_from_sim = 0
              AND message.complete = 1
            LIMIT 1
            """,
            (
                sim_index,
                raw_pdu,
            ),
        ).fetchone()


def safely_delete_current_fragment(
    modem: Modem,
    database_path: Path,
    sim_index: int,
    raw_pdu: str,
) -> None:
    """删除刚读取并已生成完整逻辑短信的分片。"""

    candidate = find_delete_candidate(
        database_path=database_path,
        sim_index=sim_index,
        raw_pdu=raw_pdu,
    )

    if candidate is None:
        print(
            f"SIM 编号 {sim_index} 暂不删除："
            "尚未关联完整逻辑短信"
        )
        return

    fragment_id = int(candidate["id"])
    database_pdu = str(candidate["raw_pdu"]).upper()

    if database_pdu != raw_pdu.upper():
        raise RuntimeError(
            "数据库 PDU 与刚读取的 PDU 不一致"
        )

    print(
        f"准备删除 SIM 编号 {sim_index}，"
        f"摘要={short_digest(raw_pdu)}"
    )

    try:
        require_ok(
            modem.command(
                f"AT+CMGD={sim_index}",
                timeout=10.0,
            ),
            f"删除 SIM 编号 {sim_index}",
        )

        verify_response = modem.command(
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
            database_path=database_path,
            fragment_id=fragment_id,
        )

        print(
            f"SIM 编号 {sim_index}："
            "安全删除完成"
        )

    except Exception as error:
        record_delete_error(
            database_path=database_path,
            fragment_id=fragment_id,
            error=error,
        )
        raise


def ingest_single_index(
    modem: Modem,
    database_path: Path,
    storage: str,
    sim_index: int,
) -> None:
    """处理一条 +CMTI 产生的短信任务。"""

    if storage != "SM":
        print(
            f"暂不处理存储区 {storage}，"
            f"编号={sim_index}"
        )
        return

    print()
    print(
        f"开始处理新短信："
        f"存储区={storage}，编号={sim_index}"
    )

    response = modem.command(
        f"AT+CMGR={sim_index}",
        timeout=15.0,
    )

    require_ok(response, "读取新短信")

    record = parse_cmgr_record(
        response=response,
        sim_index=sim_index,
    )

    inserted, existing = save_records(
        database_path=database_path,
        records=[record],
    )

    print(
        f"原始 PDU 入库：新增={inserted}，"
        f"已存在={existing}"
    )

    process_database(database_path)

    safely_delete_current_fragment(
        modem=modem,
        database_path=database_path,
        sim_index=sim_index,
        raw_pdu=str(record["raw_pdu"]),
    )


def startup_scan(
    modem: Modem,
    database_path: Path,
) -> None:
    """扫描服务停止期间留在 SIM 中的短信。"""

    print()
    print("正在执行启动扫描……")

    response = modem.command(
        "AT+CMGL=4",
        timeout=30.0,
    )

    require_ok(response, "启动扫描")

    records = extract_cmgl_records(response)

    inserted, existing = save_records(
        database_path=database_path,
        records=records,
    )

    print(
        f"启动扫描：SIM={len(records)}，"
        f"新增={inserted}，已存在={existing}"
    )

    process_database(database_path)

    for record in records:
        safely_delete_current_fragment(
            modem=modem,
            database_path=database_path,
            sim_index=int(record["sim_index"]),
            raw_pdu=str(record["raw_pdu"]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A7670 短信网关长期运行服务。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite 数据库路径",
    )

    args = parser.parse_args()
    database_path = args.db.resolve()

    initialize_schema(database_path)

    modem = Modem(PORT)

    try:
        modem.start()

        print(f"数据库：{database_path}")
        print(f"已打开串口：{PORT}")

        require_ok(
            modem.command("ATE0"),
            "关闭命令回显",
        )

        require_ok(
            modem.command("AT+CMGF=0"),
            "切换 PDU 模式",
        )

        require_ok(
            modem.command(
                'AT+CPMS="SM","SM","SM"'
            ),
            "选择 SM 存储区",
        )

        require_ok(
            modem.command(
                "AT+CNMI=2,1,0,0,0"
            ),
            "配置短信主动上报",
        )

        startup_scan(
            modem=modem,
            database_path=database_path,
        )

        print()
        print("短信网关正在运行，按 Ctrl+C 退出。")

        while True:
            try:
                event = modem.events.get(timeout=1)
            except queue.Empty:
                continue

            if event.startswith("__SERIAL_ERROR__:"):
                raise RuntimeError(event)

            match = re.fullmatch(
                r'\+CMTI:\s*"([^"]+)",(\d+)',
                event,
            )

            if match:
                storage = match.group(1)
                sim_index = int(match.group(2))

                try:
                    ingest_single_index(
                        modem=modem,
                        database_path=database_path,
                        storage=storage,
                        sim_index=sim_index,
                    )
                except Exception as error:
                    print(
                        f"短信编号 {sim_index} "
                        f"处理失败：{error}"
                    )

                continue

            now = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            print(f"[主动上报 {now}] {event}")

    except KeyboardInterrupt:
        print("\n收到退出指令。")

    finally:
        modem.close()
        print("串口已关闭。")


if __name__ == "__main__":
    main()