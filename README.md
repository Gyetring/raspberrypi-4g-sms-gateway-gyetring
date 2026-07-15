# Raspberry Pi A7670 私人蜂窝通信网关

这是一个运行在 Raspberry Pi 4B 上的私人蜂窝通信网关项目。

项目通过 USB 连接 SIMCom A7670C-LASC 4G 模块，使用 AT 命令收取短信，并将短信原始 PDU、解析结果和完整正文保存到树莓派本地 SQLite 数据库中。

当前重点是构建一个可以长期运行、只通过 SSH 或 WireGuard 管理的短信网关。项目暂不开放公网端口，也不会通过短信执行任意 Shell 命令。

## 当前状态

目前已经完成短信接收主链路：

```text
A7670 收到短信
→ 模块将短信保存到 SIM
→ 主动上报 +CMTI
→ Python 读取对应 SIM 编号
→ 保存完整原始 PDU
→ 解析发送方、时间、编码和分片信息
→ 将物理分片写入 SQLite
→ 检查同组分片是否完整
→ 按 UDH 片号排序并拼接
→ 将完整短信写入 messages
→ 确认数据库提交成功
→ 从 SIM 删除已安全保存的分片
```

已经验证：

- 普通英文和数字短信
- UCS2 中文短信
- 两片中文长短信
- 四片中文长短信
- 分片乱序
- 分片暂时缺失
- 重复分片
- 服务停止后重新扫描 SIM
- 原始 PDU 持久化
- 完整逻辑短信持久化
- 删除前重新比对 SIM PDU 与数据库 PDU
- 删除成功后更新数据库状态
- 单一串口读取线程
- AT 命令串行执行
- `+CMTI` 实时短信事件处理

当前尚未完成：

- `systemd` 开机自启
- USB 拔出后的自动重新连接
- 模块无响应时自动恢复
- 远程发送短信
- 中文长短信发送
- 来电和通话状态持久化
- 信号、运营商和注册状态定时监控
- 日志轮转
- 本地管理命令行工具

## 硬件与环境

- Raspberry Pi 4B
- A7670C-LASC 4G 模块
- USB 连接
- 中国移动 SIM 卡
- Raspberry Pi OS / Linux 无图形界面
- Python 3.13.5
- pyserial 3.5
- SQLite 3
- SSH 和 WireGuard 远程管理

AT 命令端口使用稳定的设备路径：

```text
/dev/serial/by-id/usb-SIMCom_Wireless_Solution_A76XX_Series_LTE_Module_200806006809080000-if05-port0
```

该路径当前通常指向：

```text
/dev/ttyUSB2
```

正式程序使用 `exclusive=True` 独占串口。程序运行期间不要同时打开 `minicom` 或其他 AT 串口程序。

## 目录结构

```text
modem-control/
├── gateway-service.py
├── scan-sim-pdu.py
├── process-gateway-sms.py
├── verify-sim-fragment.py
├── cleanup-gateway-sim.py
├── delete-sim-fragment.py
├── plan-sim-deletion.py
├── gateway.db
├── tools/
│   └── manual/
├── .gitignore
└── README.md
```

主要文件：

### `gateway-service.py`

长期运行的短信接收服务。

负责：

- 打开并独占 AT 串口
- 启动唯一串口读取线程
- 串行执行 AT 命令
- 区分 AT 命令响应和模块主动上报
- 启动时扫描 SIM 中已有短信
- 处理实时 `+CMTI` 事件
- 保存、解析和拼接短信
- 安全删除 SIM 中已经保存的物理分片

### `scan-sim-pdu.py`

扫描 SIM 卡中的全部短信，并将完整原始 PDU 保存到 SQLite。

主要用于：

- 独立调试
- 数据恢复
- 验证启动扫描
- 在长期服务之外人工补录短信

### `process-gateway-sms.py`

处理数据库中的原始 PDU。

负责：

- 解析 SMS-DELIVER PDU
- 解析发送方号码
- 解析短信服务中心时间戳
- 解析 DCS
- 解码 UCS2 中文
- 解码普通 GSM 7-bit 英文
- 解析 8 位和 16 位长短信 UDH
- 拼接完整长短信
- 创建 `messages` 记录
- 将物理分片关联到完整逻辑短信

当前暂不支持带 UDH 的 GSM 7-bit 长短信。

### `verify-sim-fragment.py`

删除前验证工具。

它会重新读取指定 SIM 编号，将刚读取的 PDU 与数据库中保存的 PDU 逐字节比较。

终端默认只显示 SHA-256 摘要，不显示完整短信正文。

### `cleanup-gateway-sim.py`

批量安全清理工具。

只会删除满足以下条件的物理分片：

- 原始 PDU 已写入数据库
- PDU 已成功解析
- 分片已关联到完整逻辑短信
- `messages.complete = 1`
- 删除前重新读取的 SIM PDU 与数据库 PDU 一致
- 数据库尚未标记为已删除

任意一条短信出现异常时，批量操作会立即停止。

### `delete-sim-fragment.py`

安全删除单个 SIM 物理编号。

适合人工排障和小范围恢复操作。

### `plan-sim-deletion.py`

只生成删除候选清单，不连接串口，也不会真正删除短信。

## 安装依赖

安装 pyserial：

```bash
python3 -m pip install pyserial
```

`pip` 是 Package Installer for Python，用于安装 Python 软件包。

确认 SQLite 命令行工具存在：

```bash
sqlite3 --version
```

`sqlite3` 是 SQLite 3 command-line interface，用于直接查询 SQLite 数据库。

## 运行服务

进入项目目录：

```bash
cd ~/modem-control
```

`cd` 是 change directory，用于切换当前目录。

检查 Python 语法：

```bash
python3 -m py_compile \
  gateway-service.py \
  scan-sim-pdu.py \
  process-gateway-sms.py
```

`py_compile` 是 Python compile，用于检查 Python 文件是否存在语法错误。

启动：

```bash
python3 gateway-service.py
```

正常启动后会：

1. 初始化数据库
2. 打开 AT 串口
3. 设置 PDU 模式
4. 选择 SIM 短信存储区
5. 配置 `+CMTI` 主动上报
6. 扫描服务停止期间留在 SIM 中的短信
7. 进入长期监听

停止程序：

```text
Ctrl+C
```

停止时程序会关闭串口。

## 查看短信

服务运行时可以直接读取 `gateway.db`，不需要停止服务。

### 查看最近 20 条短信摘要

```bash
sqlite3 -header -column gateway.db "
SELECT
    id,
    sender,
    modem_time,
    total_parts,
    length(body) AS characters,
    substr(replace(body, char(10), ' '), 1, 40) AS preview
FROM messages
WHERE complete = 1
ORDER BY id DESC
LIMIT 20;
"
```

### 查看最新一条短信正文

```bash
sqlite3 gateway.db "
SELECT body
FROM messages
WHERE complete = 1
ORDER BY id DESC
LIMIT 1;
"
```

### 查看指定短信

将下面的 `15` 替换为实际短信编号：

```bash
sqlite3 gateway.db "
SELECT
    '发送方：' || sender,
    '时间：' || modem_time,
    '分片数：' || total_parts,
    '',
    body
FROM messages
WHERE id = 15;
"
```

### 查看处理异常

```bash
sqlite3 -header -column gateway.db "
SELECT
    id,
    sim_index,
    sender,
    parse_status,
    deleted_from_sim,
    parse_error,
    delete_error
FROM sms_fragments
WHERE parse_status != 'parsed'
   OR deleted_from_sim = 0
   OR parse_error IS NOT NULL
   OR delete_error IS NOT NULL
ORDER BY id DESC;
"
```

## 数据库结构

### `sms_fragments`

保存 SIM 中的物理短信记录。

主要字段：

- `id`：数据库记录编号
- `storage`：短信存储区，目前使用 `SM`
- `sim_index`：SIM 卡物理存储编号
- `status_code`：模块返回的短信状态编号
- `tpdu_length`：模块报告的 TPDU 字节数
- `raw_pdu`：未经修改的完整原始 PDU
- `sender`：发送方号码或服务号码
- `modem_time`：短信携带的接收时间
- `dcs`：Data Coding Scheme，短信数据编码方案
- `reference_number`：长短信引用编号
- `reference_bits`：引用编号位宽，通常为 8 或 16
- `total_parts`：长短信总片数
- `part_number`：当前分片序号
- `decoded_part`：当前分片解码后的正文
- `parse_status`：解析状态
- `parse_error`：解析失败原因
- `message_id`：对应的完整逻辑短信编号
- `deleted_from_sim`：是否已经从 SIM 删除
- `deleted_at`：删除成功时间
- `delete_error`：删除失败原因

### `messages`

保存用户实际阅读的完整逻辑短信。

主要字段：

- `id`：完整短信编号
- `message_key`：防止重复写入的稳定标识
- `sender`：发送方
- `modem_time`：短信时间
- `body`：完整正文
- `dcs`：编码方案
- `reference_number`：长短信引用编号
- `reference_bits`：引用编号位宽
- `total_parts`：物理分片总数
- `complete`：是否完整，`1` 表示完整
- `assembled_at`：最近一次拼接时间
- `created_at`：首次创建时间

## SIM 存储与安全删除

当前 SIM 短信存储区为：

```text
SM
```

容量约为：

```text
50
```

一条长短信的每个分片都会单独占用一个物理位置。

安全删除必须遵循：

```text
读取 SIM 分片
→ 保存完整原始 PDU
→ 提交 SQLite 事务
→ 成功解析并关联完整消息
→ 删除前重新读取 SIM 分片
→ 与数据库 PDU 比对
→ 执行 AT+CMGD
→ 确认该 SIM 编号已经不存在
→ 更新 deleted_from_sim
```

不要在数据库保存成功前执行：

```text
AT+CMGD=<编号>
```

## 隐私与安全

本项目会处理短信正文、验证码、手机号和运营商信息。

应遵守以下原则：

- 不把 `gateway.db` 提交到 Git
- 不把原始 PDU 日志提交到 Git
- 不在公开日志中显示完整电话号码
- 不在公开日志中显示 IMEI
- 不在公开日志中显示验证码和短信正文
- 不开放公网管理端口
- 只通过本地网络、SSH 或 WireGuard 管理
- 不实现通过短信执行任意 Shell 命令
- 限制项目目录和数据库文件权限

建议权限：

```bash
chmod 700 ~/modem-control
chmod 600 ~/modem-control/gateway.db
```

`chmod` 是 change mode，用于修改文件和目录权限。

## Git 管理

建议的 `.gitignore`：

```gitignore
# Python cache
__pycache__/
*.py[cod]

# Runtime databases
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3

# Logs and captured modem data
*.log
pdu-read-*.txt

# Local environment
.env
.venv/
venv/

# Editor files
.vscode/
.idea/
*.swp
```

初始化 Git：

```bash
git init
git add .
git commit -m "Initial SMS gateway implementation"
```

`git init` 用于初始化 Git 仓库；`git add` 将文件加入暂存区；`git commit` 创建一次版本提交。

提交前检查：

```bash
git status
```

`git status` 用于查看当前仓库和暂存区状态。

必须确认 `gateway.db`、原始 PDU 和短信日志没有出现在待提交列表中。

## 数据库备份

不要在服务运行时直接复制可能正在写入的 SQLite 数据库文件。

推荐使用 SQLite 自带备份命令：

```bash
mkdir -p ~/modem-backups

sqlite3 ~/modem-control/gateway.db \
  ".backup '$HOME/modem-backups/gateway-backup.db'"
```

`mkdir` 是 make directory，用于创建目录；SQLite 的 `.backup` 会生成一致性更好的数据库备份。

## 下一阶段

下一个大里程碑是短信网关 v0.2：

1. 使用 `systemd` 开机自动运行
2. 异常退出后自动重启
3. USB 模块掉线后自动重新连接
4. 定期查询 SIM 容量
5. 定期记录信号、运营商和网络注册状态
6. 本地命令行工具，例如：
   - `smsctl list`
   - `smsctl read <id>`
   - `smsctl status`
7. 远程发送英文短信
8. 远程发送 UCS2 中文短信
9. 长短信自动分片发送
10. 保存发送状态和失败原因

后续再实现来电号码和通话状态记录，电话音频不属于当前优先任务。
