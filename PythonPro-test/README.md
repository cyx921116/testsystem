# Pilot 生产测试系统

一个可直接运行的生产测试工作站，包含产品 SN 追溯、管理员/操作工权限、直观低代码测试流程、逐项 PASS/FAIL 展示、唯一测试报告、可配置 MES 上传，以及多协议产品通讯。低代码数值统一使用十进制，协议层可配置二进制编解码。

## 文档

- [用户使用手册](docs/USER_MANUAL.md)：按页面说明登录、生产测试、流程编排、通讯配置、固件烧录、MES 集成、报告中心与账号权限的日常操作。
- [详细项目开发说明](docs/PROJECT_GUIDE.md)：架构、目录、数据库、后端函数、前端函数、脚本、协议、MES、API 和扩展方法。
- [控制流与 React Flow 开发说明](docs/CONTROL_FLOW.md)：条件、并行、循环、子流程、v2 JSON、执行语义和前端构建。
- [固件烧录框架开发说明](docs/FIRMWARE_FLASH.md)：固件仓、烧录后端、烧录记录和接入主系统的路线。
- [Git 与 GitHub 配置指南](docs/GIT_GITHUB.md)：Windows 环境、提交身份、登录授权、创建仓库、推送和常见问题。

## 启动

要求 Python 3.12+，项目使用 `uv` 管理依赖。

```bash
uv sync
uv run testsystem
```

打开 <http://127.0.0.1:8000>。API 文档位于 <http://127.0.0.1:8000/docs>。

停止脚本（Ctrl+C 或直接关闭终端）后再次启动时，若端口仍被上一次异常退出的残留服务进程占用，启动器会自动结束该进程并释放端口（仅限可确认为 Python 的本系统残留实例），无需手动执行 `netstat`/`taskkill`。端口被其他程序占用且无法确认来源时，会打印手动释放端口的命令提示。

首次启动创建以下账号，密码使用 PBKDF2 加盐哈希保存，登录会话通过 HttpOnly Cookie 保持 12 小时；每次服务启动都会轮换会话签名密钥，重启后需要重新登录：

| 角色 | 初始账号 | 初始密码 | 权限 |
| --- | --- | --- | --- |
| 管理员 | `admin` | `admin123` | 全部功能、账号管理、MES 上传与生产测试 |
| 操作工 | `operator` | `operator123` | 扫码、执行测试、查看运行与报告 |

首次登录后请立即修改密码。可在首次初始化前通过 `TESTSYSTEM_ADMIN_PASSWORD`、`TESTSYSTEM_OPERATOR_PASSWORD` 指定不同的初始密码；已存在的账号不会被环境变量重置。HTTPS 部署时设置 `TESTSYSTEM_SECURE_COOKIE=true`。

也可以用 `uv run uvicorn testsystem.app:app --reload` 开发调试。生产运行请使用单进程；不要使用多个 Uvicorn worker 共用同一个物理工位。

首次启动自动创建 SQLite 数据库和演示流程。默认使用内置仿真器，不需要硬件即可完成扫码、测试和报告流程。数据库默认保存到 `data/testsystem.db`，可通过环境变量修改：

```bash
# Git Bash
export TESTSYSTEM_DATA_DIR=/path/to/data
export TESTSYSTEM_HOST=0.0.0.0
export TESTSYSTEM_PORT=8000
uv run testsystem
```

## 生产操作

1. 在“生产测试”执行台依次选择：产品型号（关联 MES 产品主数据，有多少启用产品就列出多少）→ 测试工站（显示工站主数据全部启用工站）；已绑定该“产品 + 工站”组合的测试流程会自动带出。选中的工站即本次执行工站，写入运行与报告。
2. 用扫码枪扫描 SN，扫码枪结束符设为 Enter；也可手工输入。
3. 系统检查 SN 格式、复测限制及工站占用情况，然后点击“开始测试”或按 F2。
4. 页面持续刷新每个测试项的实际值、期望、耗时和 PASS/FAIL。
5. 每次测试结束都会生成一份独立报告并永久留档：失败报告不会因复测被清除，复测通过后历史 FAIL 报告仍可追溯。同一 SN 已有合格（PASS）报告时禁止重新测试；失败复测次数按 SN 跨全部流程累计，上限取流程定义的 `max_retests`（未配置时跟随工位策略）。
6. 在“报告中心”按 SN 查询、查看断言明细、打印、下载 JSON 或重试 MES 上传。

服务异常重启时，未完成的运行会标记为中断失败，不生成不完整报告，因此该 SN 可以重新测试。已生成的报告及脚本快照不会被后续脚本编辑改变。

## 低代码流程

“流程编排”的**流程库按产品分组索引**：每个产品一组（含未建流程的产品），组内可一键为该产品新建流程；编辑器使用 React Flow 复杂图形画布，支持：

- `test` 测试项：组合请求、接收、设备操作、变量、断言和延时；
- `condition` 条件判断：按断言结果选择 TRUE/FALSE 分支；
- `parallel` 并行执行：2~8 路分支，独立上下文和协议连接；
- `loop` 循环：固定次数或 while 条件，最多 100 次；
- `subflow` 子流程/子任务：复用独立流程、传递输入并读取输出；
- 条件 TRUE/FALSE 和循环体可添加任意类型节点；
- 并行分支支持名称、ID、排序、增删和分支内嵌套编辑；
- 子任务支持新建、调用节点跳转编辑，以及删除前引用检查；
- 节点拖入、同层拖动重排、缩放、小地图和右侧属性检查器；
- 测试项一键复制、动作上下排序、全类型动作添加器和常用寄存器参数具名表单；
- 数值断言使用独立的十进制上下限，发送动作不再要求选择 HEX/JSON 编码；
- 主流程与多个子流程切换编辑；
- 传统步骤列表和高级 JSON；所有编辑方式共用同一份流程草稿；
- 保存前由后端编译校验，保存后版本自动递增。

从左侧拖入控制节点，点击节点编辑条件、分支、循环和动作。子流程输出通过 `${subflows.调用节点ID.变量名}` 访问，并行分支输出通过 `${branches.分支ID.变量名}` 访问。详细结构见 [控制流说明](docs/CONTROL_FLOW.md)。

### 前端构建

仓库已经包含构建后的 `flow-editor.js` 和 `flow-editor.css`，正常运行系统不需要 Node.js。只有修改 `src/testsystem/frontend` 中的 React/TypeScript 源码时才需要：

```bash
npm ci
npm run build
```

`npm run build` 先执行 TypeScript 类型检查，再用 Vite 构建到 FastAPI 静态目录。生产部署仍只需要 Python 和浏览器。

脚本使用受限 JSON 结构，不执行任意 Python 代码。模板变量格式为 `${sn}`、`${station}`、`${run_id}`、`${response}`；列表可用 `${registers.0}` 访问。发送数据和期望值都支持模板替换。

一个测试项示例：

```json
{
  "id": "voltage",
  "name": "电源电压检查",
  "protocol": "serial",
  "timeout_ms": 5000,
  "retries": 1,
  "on_fail": "stop",
  "actions": [
    {"type": "request", "data": "AT+VOLTAGE?\r\n", "save_as": "voltage"},
    {"type": "assert", "actual": "${voltage}", "operator": "between", "expected": [11.5, 12.5]}
  ]
}
```

断言支持等于、不等于、包含、不包含、正则、大小比较、闭区间、真值和非空判断。

## 通讯协议

默认安装只包含内置仿真器、TCP 和 WiFi(TCP) 所需能力。真实硬件驱动统一通过可选依赖安装：

```bash
uv sync --extra hardware
```

| 协议 | 实现方式 |
| --- | --- |
| Serial | pyserial，串口参数和超时可配置 |
| CAN | python-can，接口、通道、仲裁 ID、扩展帧和 CAN FD 可配置 |
| TCP / WiFi | TCP Client，地址、端口、接收长度、编码可配置 |
| Modbus | Modbus TCP/RTU，读写线圈、离散量和寄存器 |
| Bluetooth | BLE GATT 特征读写；经典蓝牙 SPP 使用系统虚拟串口 |
| MQTT | 请求/响应主题、QoS、账号密码和 TLS 可配置 |
| J-Link | 通过 pylink-square 与 J-Link 连接，读写内存/寄存器及复位控制 |

“通讯配置”页面可保存参数并测试连接。真实硬件测试需要相应厂商驱动，例如 CAN 适配器驱动或 SEGGER J-Link Software。WiFi 配置负责已建立网络后的 TCP 通讯，不负责修改操作系统无线网络。

### 十进制数据与协议编解码

低代码编辑器中的 ID、地址、寄存器值和断言数值均按十进制填写。文本指令（如 `AT+SN?`）仍可直接填写；多个十进制字节或寄存器值使用逗号分隔，例如 `1, 3, 1200`。不要在低代码输入框中填写 `0x...`。

串口、CAN、TCP、WiFi、BLE、MQTT 的配置页分为“连接参数 / 下发编码 / 响应解析”：

| 配置 | 作用 |
| --- | --- |
| `send_format` | `text` 文本、`decimal` 十进制转二进制、`json` JSON 文本 |
| `send_type` / `send_byte_order` | 下发整数或浮点类型、大小端 |
| `send_scale` | 实际发送值 = 页面输入 × 缩放系数 |
| `receive_format` | 文本、十进制数值、JSON，或诊断用原始 HEX |
| `receive_type` / `receive_byte_order` | 响应数值类型和大小端 |
| `receive_offset` / `receive_length` | 跳过报文头并限制解析字节数，0 表示解析剩余全部 |
| `receive_scale` | 断言使用的数值 = 原始解析值 × 缩放系数 |

例如 `1200` 按 `uint16 + 大端` 下发为 `04 B0`；响应 `04 B0` 配置 `receive_scale=0.01` 后得到十进制 `12.0`。旧脚本动作中的 `encoding: text/hex/json` 仍由后端兼容，新动作省略 `encoding` 并继承协议配置。

Modbus 和 J-Link 使用 `execute` 动作。例如读取保持寄存器：

```json
{
  "type": "execute",
  "operation": "read_holding_registers",
  "params": {"address": 0, "count": 2},
  "save_as": "registers"
}
```

## MES 报文配置

“MES 集成”页同时管理产品与工站主数据：从 MES 同步（内置模拟器可离线演示）或本地添加，流程编排的产品型号/绑定工站、执行台的产品与工站索引、工位策略的工位编号下拉都引用这份主数据；本地行可增删改，MES 同步行仅可启停，MES 端删除后同步自动停用。

“MES 集成”支持 HTTP POST/PUT/PATCH、自定义请求头、超时、TLS 校验开关（JSON 配置中的 `verify_tls`）、完整报告或字段映射模式。默认报文为：

```json
{
  "event": "production_test.completed",
  "idempotencyKey": "${report_no}",
  "data": "${report}"
}
```

占位符单独作为值时保留原始类型。例如 `${items}` 发送数组，`${report}` 发送完整报告对象；嵌入字符串时进行文本替换。常用字段包括：

- `${report_no}`、`${sn}`、`${product_model}`、`${status}`；
- `${station}`、`${operator}`、`${started_at}`、`${finished_at}`、`${duration_ms}`；
- `${script.name}`、`${script.version}`、`${items}`、`${summary}`、`${report}`。

请求头 `Idempotency-Key` 默认填写报告编号，上传成功的报告不能重复上传；失败记录可在报告中心重试。MES 是否真实接收成功以 HTTP 2xx 为准，原始响应和每次请求报文都保存在上传日志中。

标准完整报告包含：

```json
{
  "schema_version": "1.0",
  "report_no": "RPT-20261116-XXXXXXXX",
  "sn": "SN-DEMO-0001",
  "run_id": "run_...",
  "product_model": "DEMO-100",
  "station": "STATION-01",
  "operator": "OP-001",
  "script": {"id": 1, "name": "通用电子产品出厂测试", "version": 1},
  "status": "PASS",
  "started_at": "2026-11-16T08:00:00.000Z",
  "finished_at": "2026-11-16T08:00:01.000Z",
  "duration_ms": 1000,
  "summary": {"total": 4, "passed": 4, "failed": 0},
  "items": []
}
```

## 数据与安全说明

- SQLite 启用外键和 WAL；SN、运行和报告均有唯一约束，工位同时只允许一个活动测试。
- 报告保存脚本快照，确保后续编辑不影响历史追溯。
- 内置登录和管理员/操作工角色，所有 API 均由后端鉴权；操作工不能修改脚本、协议、MES、用户或上传报告。
- 默认仍建议部署在受信产线内网。跨终端或暴露到非可信网络前，应使用 HTTPS、修改初始密码并限制数据目录访问；Cookie 本身不替代 TLS。
- MES 密码、MQTT 密码等保存在本地配置数据库中，请限制数据目录访问权限并纳入受控备份。
