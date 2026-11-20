# Pilot 生产测试系统详细开发说明

## 1. 系统定位

本项目是一个单工位生产测试工作站，核心目标是把“产品 SN、测试脚本、实际测试过程、测试报告、MES 上传记录”串成一条可追溯链路。

主要能力：

1. 产品必须先扫描 SN，后端也会强制检查，不能仅靠前端绕过。
2. SN 与产品型号、脚本版本、工位、操作员和测试运行绑定。
3. 测试脚本采用低代码 JSON，后端编译校验后执行，不运行任意 Python 代码。
4. 每个测试项独立记录实测值、期望、断言明细、重试次数、耗时和 PASS/FAIL。
5. 一个 SN 最多生成一份报告，由数据库唯一约束保证，而不是只依赖页面判断。
6. 报告可查询、打印、下载，并通过可配置 HTTP 报文上传 MES。
7. 串口、CAN、TCP、Modbus、BLE、WiFi、MQTT、J-Link 使用统一协议接口。
8. 默认内置产品仿真器，无硬件也能验证完整业务流程。
9. v2 控制流支持条件、并行、循环和可复用子流程，复杂画布使用 React Flow。

当前版本适合隔离产线内网中的单进程、单工位部署，已提供登录、管理员/操作工后端权限、账号管理和会话失效控制。它仍不是包含电子签名、审计审批和多工位调度的大型 MES，而是一个可继续扩展的生产测试执行系统。

## 2. 总体架构

```text
┌─────────────────────────────────────────────────────────────┐
│ 浏览器：原生 JavaScript + React Flow 编辑器岛               │
│ 生产测试 / 复杂流程编排 / 通讯配置 / MES 集成 / 报告中心     │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP JSON API
┌──────────────────────────▼──────────────────────────────────┐
│ FastAPI：app.py                                             │
│ 参数校验、扫码、启动测试、配置管理、报告查询、静态页面       │
└───────────────┬───────────────────────┬─────────────────────┘
                │                       │
       ┌────────▼────────┐     ┌────────▼────────────────────┐
       │ compiler.py     │     │ executor.py                 │
       │ JSON 编译与校验 │────▶│ 后台执行、断言、重试、报告   │
       └─────────────────┘     └────────┬────────────────────┘
                                        │
                           ┌────────────▼─────────────┐
                           │ protocols.py             │
                           │ 统一产品通讯适配器        │
                           └────────────┬─────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
      ┌──────▼──────┐          ┌────────▼────────┐        ┌───────▼──────┐
      │ SQLite      │          │ 产品硬件/仿真器  │        │ mes.py       │
      │ 追溯与配置  │          │ 串口/CAN/网络等  │        │ MES HTTP     │
      └─────────────┘          └─────────────────┘        └──────────────┘
```

### 2.1 为什么使用 SQLite

SQLite 随 Python 自带，无需额外数据库服务，适合单工位工作站。项目启用了：

- 外键约束：防止报告引用不存在的 SN 或运行记录。
- WAL（Write-Ahead Logging）：改善读取与写入并发。
- `busy_timeout`：减少短时间锁竞争直接失败。
- 唯一索引：从数据库层保证 SN 报告唯一和单工位活动运行唯一。
- 显式事务：扫码、创建运行、生成报告等关键操作要么全部成功，要么全部回滚。
- 显式关闭连接：避免 Windows 上 SQLite 文件被后台连接长期占用。

如果以后需要多工位集中管理、网页远程访问、多用户权限或高并发查询，建议把数据访问层迁移到 PostgreSQL，并把执行器留在各工位边缘服务中。

### 2.2 混合前端架构

生产测试、通讯配置、MES 和报告页面继续使用原生 HTML、CSS 和 JavaScript，状态集中在 `state` 对象中。复杂流程编辑器使用 React + TypeScript + React Flow，以“编辑器岛”挂载到现有页面。

这种方案避免全站重写，同时支持条件、并行、循环和子流程。Node.js 只负责开发构建，Vite 产物由 FastAPI 托管，产线运行仍只需要 Python 和浏览器。构建后的 JS/CSS 随 wheel 发布，不依赖外部 CDN。详见 [控制流与 React Flow 开发说明](CONTROL_FLOW.md)。

## 3. 目录和文件职责

```text
Testsystem/
├── .gitattributes             Git 跨平台换行和二进制文件规则
├── .gitignore                 排除数据库、密钥、虚拟环境和缓存
├── .python-version            指定 Python 3.12
├── pyproject.toml             Python 项目元数据、依赖和启动命令
├── uv.lock                    Python 依赖锁文件
├── package.json               React、React Flow、Vite 和构建命令
├── package-lock.json          npm 依赖锁文件
├── tsconfig.json              TypeScript 严格检查配置
├── vite.config.ts             React 编辑器库构建配置
├── README.md                  快速启动和常用配置
├── docs/
│   ├── PROJECT_GUIDE.md       本详细开发说明
│   ├── CONTROL_FLOW.md        v2 控制流、执行语义和 React 构建
│   └── GIT_GITHUB.md          Git、GitHub 登录与上传指南
└── src/
    ├── main.py                兼容直接运行该脚本时调用系统入口
    └── testsystem/
        ├── __init__.py        `uv run testsystem` 的主入口
        ├── app.py             FastAPI 应用、API、权限中间件、静态页面
        ├── auth.py            用户、密码哈希、签名会话和角色 API
        ├── data_codec.py      十进制数值与协议二进制编解码
        ├── compiler.py        v1 校验和 v2 编译入口
        ├── control_compiler.py 条件、并行、循环、子流程的递归校验
        ├── database.py        SQLite 表结构、迁移、默认配置、连接和事务
        ├── executor.py        运行生命周期、v1 执行、报告定稿
        ├── control_flow.py    v2 递归执行、并行、循环和控制事件
        ├── mes.py             MES 报文构造、HTTP 上传、上传日志
        ├── protocols.py       各通讯协议适配器
        ├── protocol_definitions.py 协议字段元数据和默认连接参数
        ├── utils.py           时间、ID、JSON、模板变量工具
        ├── frontend/
        │   ├── FlowEditor.tsx React Flow 组件、拖放、检查器和挂载 API
        │   ├── model.ts       v2 AST 创建、路径更新和重排
        │   ├── types.ts       控制流 TypeScript 类型
        │   └── flow-editor.css React 编辑器样式
        └── static/
            ├── index.html     全部页面结构和 React 挂载容器
            ├── styles.css     工业风界面、响应式布局、打印样式
            ├── flow-model.js  旧顺序列表的纯数据模型
            ├── flow-editor.js React Flow 构建产物
            ├── flow-editor.css React Flow 构建样式
            └── app.js         前端状态、API 调用、交互和渲染
```

运行时还会出现 `data/testsystem.db`，它是本地产线数据，不属于源码，已被 `.gitignore` 排除。

## 4. 启动入口和初始化顺序

### 4.1 `src/testsystem/__init__.py`

`main()` 是项目的命令行入口：

1. 延迟导入 `uvicorn`，避免仅引用包时加载服务器。
2. 导入 `app.py` 中的 FastAPI `app`。
3. 从环境变量读取监听地址和端口。
4. 调用 `uvicorn.run()` 启动 HTTP 服务。

可用环境变量：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TESTSYSTEM_HOST` | `127.0.0.1` | HTTP 监听地址；改成 `0.0.0.0` 才允许其他机器访问 |
| `TESTSYSTEM_PORT` | `8000` | HTTP 端口 |
| `TESTSYSTEM_DATA_DIR` | 当前目录下的 `data` | SQLite 数据目录 |

`pyproject.toml` 中：

```toml
[project.scripts]
testsystem = "testsystem:main"
```

因此 `uv run testsystem` 实际调用上述 `main()`。

### 4.2 `src/main.py`

只保留：

```python
from testsystem import main

if __name__ == "__main__":
    main()
```

它用于兼容在 IDE 中直接运行 `main.py`，不包含独立业务逻辑。

### 4.3 FastAPI 生命周期

`app.py` 的 `lifespan()` 在服务启动和关闭时执行：

启动：

1. `database.initialize()` 创建表和默认配置。
2. `database.recover_interrupted_runs()` 处理上次异常退出留下的运行。
3. 开始接收 API 请求。

关闭：

1. `executor.shutdown()` 停止接收新任务并等待已提交任务结束。
2. 服务退出。

不要在生产环境随意强制结束 Python 进程。如果必须停止，应先停止新产品扫码，再等待当前产品测试结束。

## 5. 项目依赖和构建配置

### 5.1 `pyproject.toml`

基础依赖：

- `fastapi`：HTTP API、请求参数校验、静态文件和文档。
- `uvicorn`：ASGI HTTP 服务器。

开发依赖：

- `httpx`：API 测试客户端所需。
- `pytest`：为后续自动化测试预留。

硬件可选依赖：

| 包 | 对应能力 |
| --- | --- |
| `pyserial` | 串口 |
| `python-can` | CAN 接口 |
| `pymodbus` | Modbus TCP/RTU |
| `bleak` | 跨平台 BLE GATT |
| `paho-mqtt` | MQTT Client |
| `pylink-square` | SEGGER J-Link Python 接口 |

只运行仿真器：

```bash
uv sync
```

安装所有硬件 Python 依赖：

```bash
uv sync --extra hardware
```

Python 包不等于全部硬件驱动。例如 PCAN 还需要厂商驱动，J-Link 还需要 SEGGER J-Link Software。

### 5.2 `uv.lock`

锁文件记录实际解析的依赖版本，应提交 Git。其他工程师使用 `uv sync` 时可以还原一致环境，避免“我的电脑可以运行”的依赖漂移。

### 5.3 构建

```bash
uv build
```

构建生成源码包和 wheel。前端 `index.html`、`app.js`、`styles.css`、React 构建产物也属于 Python wheel 内容，否则安装包会只有 API 没有页面。

修改 React/TypeScript 源码后，先执行 `npm ci` 和 `npm run build`。该命令先进行 TypeScript 严格检查，再由 Vite 生成 `static/flow-editor.js` 和 `flow-editor.css`。`package-lock.json` 保证 npm 依赖可复现，`node_modules/` 不提交。正常运行和安装 wheel 不需要 Node.js。

## 6. 数据库详细设计

数据库结构和默认数据位于 `database.py` 的 `SCHEMA`、`DEFAULT_MES_CONFIG`、`DEFAULT_SETTINGS`。

### 6.1 `products`：产品 SN 主表

| 字段 | 含义 |
| --- | --- |
| `sn` | 产品序列号，主键 |
| `product_model` | 最近绑定的产品型号 |
| `first_seen_at` | 第一次扫码时间 |
| `last_tested_at` | 最近报告完成时间 |

作用：证明 SN 先经过扫码登记，再允许创建测试运行。报告中的产品型号使用自己的快照，不受后续产品主表变化影响。

### 6.2 `scripts`：测试脚本表

| 字段 | 含义 |
| --- | --- |
| `id` | 自增脚本 ID |
| `name` | 脚本名称 |
| `product_model` | 对应产品型号 |
| `version` | 每次保存自动加 1 |
| `enabled` | 1 可用，0 停用 |
| `definition_json` | 编译规范化后的完整 JSON |
| `created_at` / `updated_at` | 创建和最后修改时间 |

脚本更新不是原地覆盖历史运行。`test_runs` 会额外保存启动时的 `script_snapshot_json`，所以脚本继续编辑不会改变旧报告。

### 6.3 `protocol_configs`：通讯配置表

每个协议一行：

- `name`：`serial`、`can`、`tcp` 等协议名，主键。
- `config_json`：该协议的全部连接参数。
- `updated_at`：最后保存时间。

这种设计便于增加驱动特定参数，不需要每加一个串口字段就修改数据库。

### 6.4 `settings`：工位与 MES 配置表

采用 `key + value_json`：

- `station_id`：工位号，默认 `STATION-01`。
- `operator`：默认操作员。
- `sn_pattern`：SN 正则表达式。
- `auto_upload`：报告生成后是否自动上传 MES。
- `mes`：MES 地址、方法、请求头、报文映射等完整 JSON。
- `auth_secret`：仅用于签名登录 Cookie 的本地随机密钥，不通过工位设置 API 返回。

### 6.4.1 `users`：登录账号与角色

保存用户名、显示名称、`admin/operator` 角色、随机盐、PBKDF2-HMAC-SHA256 密码哈希、启用状态和会话版本。修改或重置密码、启停账号会递增会话版本，使旧 Cookie 失效。默认账号只在首次初始化时创建，重启不会重置密码。数据库中不保存明文密码。

### 6.5 `test_runs`：一次测试运行

关键字段：

- `id`：`run_` 加随机 UUID，主键。
- `sn`：关联产品。
- `product_model`：本次运行型号快照。
- `script_id`、`script_version`：脚本追溯。
- `script_snapshot_json`：本次实际执行脚本快照。
- `station`、`operator`：工位和操作员快照。
- `status`：`RUNNING`、`PASS` 或 `FAIL`。
- `current_item`：当前步骤 ID，供页面显示。
- `error`：执行器或系统异常。
- `started_at`、`finished_at`：开始与完成时间。

部分唯一索引：

```sql
CREATE UNIQUE INDEX idx_active_station
ON test_runs(station) WHERE status = 'RUNNING';
```

它表示同一个工位最多只有一条 `RUNNING`，防止两个产品同时争用同一套硬件。

### 6.6 `test_items`：逐项测试明细

每条记录对应运行中的一个步骤：

- `run_id + item_id` 唯一，防止同一运行插入重复步骤。
- `sequence`：页面和报告中的执行顺序。
- `status`：`PENDING`、`RUNNING`、`PASS`、`FAIL`，默认 `PENDING`。
- `expected`、`actual`：期望和实测值的文本形式。
- `message`：通过、失败、重试或未执行原因。
- `attempts`：实际执行次数，包含第一次，不只是额外重试次数。
- `details_json`：所有已执行断言的结构化明细。
- `started_at`、`finished_at`、`duration_ms`：步骤时间和耗时。

前序步骤设置为 `stop` 且失败时，后续步骤会记录为 FAIL，消息为“前序测试失败，本项未执行”，从而保证报告中每项都明确是 PASS 或 FAIL。

v2 另外增加 `node_id`、`path` 和 `iteration`。`item_id` 表示一次实际执行的唯一路径，`node_id` 表示脚本中的逻辑节点；循环项通过路径中的迭代序号避免覆盖。v2 只记录实际到达的执行项，未选分支和未发生的迭代不创建记录。

### 6.7 `reports`：一对一正式报告

最重要的约束：

```sql
sn TEXT NOT NULL UNIQUE
run_id TEXT NOT NULL UNIQUE
report_no TEXT NOT NULL UNIQUE
```

分别保证：

- 一个 SN 只能有一份正式报告。
- 一次运行只能生成一份报告。
- 报告编号不能重复，可作为 MES 幂等键。

其他字段保存产品、工位、操作员、脚本、状态、统计、完整 JSON、起止时间、耗时，以及 MES 状态：

- `PENDING`：待上传。
- `UPLOADING`：正在上传。
- `SUCCESS`：HTTP 上传成功。
- `FAILED`：上传失败，可重试。

`mes_attempts`、`mes_response`、`uploaded_at` 用于追溯上传次数、响应和成功时间。

### 6.8 `mes_upload_logs`：每次上传尝试

每次成功或失败都追加一行，不覆盖历史：

- `report_id`：对应报告。
- `attempt`：第几次尝试。
- `request_json`：实际发送报文。
- `success`：是否成功。
- `response`：MES 响应或异常。
- `created_at`：上传时间。

### 6.9 `control_events`：v2 控制事件

记录条件、并行、循环和子流程调用。字段包括节点 ID、名称、类型、执行路径、状态、JSON 明细和起止时间。状态为 RUNNING、TRUE、FALSE、PASS、FAIL 或 INTERRUPTED。条件 FALSE 是正常分支结果，不会误判为测试失败。启动时自动创建该表，并为旧 test_items 补充 v2 字段。

### 6.10 表关系

```text
products 1 ──────── N test_runs
scripts  1 ──────── N test_runs
test_runs 1 ─────── N test_items
test_runs 1 ─────── N control_events
test_runs 1 ─────── 0..1 reports
products 1 ─────── 0..1 reports
reports  1 ─────── N mes_upload_logs
```

同一 SN 可能有“异常中断但未生成报告”的历史运行；一旦正式报告生成，就不能再开始新的运行。

## 7. 后端逐文件、逐函数说明

## 7.1 `utils.py`：通用工具

### `utc_now()`

返回 UTC ISO 8601 时间，毫秒精度，以 `Z` 结尾。例如：

```text
2026-11-16T08:30:15.123Z
```

数据库统一存 UTC，前端 `formatTime()` 再转换为浏览器本地时间，避免服务器和浏览器时区不一致导致原始数据混乱。

### `new_id(prefix)`

使用 UUID4 生成无连字符随机 ID，并加业务前缀。例如 `run_...`、`report_...`。随机 ID 避免暴露连续业务量，也避免多线程生成主键冲突。

### `json_dumps(value)`

统一 JSON 序列化：

- `ensure_ascii=False`：中文不转换成 Unicode 转义。
- 紧凑分隔符：减少数据库存储和 HTTP 报文体积。
- `default=str`：无法直接序列化的对象退化为字符串。

### `resolve_path(context, path)`

按点号读取模板路径：

```text
sn
script.name
summary.total
registers.0
```

字典按 key 读取，列表允许数字下标。路径不存在时抛出 `KeyError`。

### `render_template(value, context)`

递归处理字典、数组和字符串中的 `${...}`：

- 字符串整体就是 `${items}` 时，保留原对象的数组类型。
- 字符串整体就是 `${duration_ms}` 时，保留数字类型。
- 嵌入文本时，例如 `"SN=${sn}"`，替换为字符串。
- 对象和数组嵌入普通字符串时，先转换为 JSON 文本。
- 未找到的完整占位符返回 `None`；嵌入文本中的未知占位符保留原文，便于排查配置。

该函数同时用于测试脚本变量和 MES 报文映射。

## 7.2 `compiler.py`：低代码脚本编译器

### 常量集合

- `SUPPORTED_PROTOCOLS`：后端允许执行的协议名。
- `ACTION_TYPES`：允许的动作类型。
- `ASSERT_OPERATORS`：允许的断言操作符。
- `ID_PATTERN`：步骤 ID 规则，字母开头，最长 40 位。
- `VARIABLE_PATTERN`：变量名规则。
- `DEFAULT_SCRIPT`：首次启动创建的仿真演示流程。

默认流程包含 SN 绑定核验、固件版本、电源电压、产品功能自检四个测试项。

### `ScriptValidationError`

保存一组人类可读错误，而不是遇到第一个错误就停止。API 可以把所有问题一次返回给前端编辑器。

### `_number(value, label, errors, minimum, maximum)`

内部工具，检查数字范围并拒绝布尔值。用于超时、重试、接收长度等字段。

### `_validate_action(action, path, errors)`

根据动作类型检查必填字段、编码、变量名、参数对象、正则表达式和区间格式。`path` 类似：

```text
steps[2].actions[1].expected
```

工程师可以根据路径快速定位 JSON 中的问题。

### `compile_script(source)`

脚本唯一入口，主要步骤：

1. 深拷贝输入，避免校验过程修改调用者对象。
2. 检查 `schema_version`、名称、产品型号和变量对象。
3. 检查至少一个测试项，最多 100 项。
4. 逐项检查 ID 唯一性、协议、超时、重试和失败策略。
5. 每项至少一个动作，最多 50 个动作。
6. 调用 `_validate_action()` 检查全部动作。
7. 有任何错误则抛出 `ScriptValidationError`。
8. 补齐默认值并返回规范化脚本。

这里不使用 `eval()` 或 `exec()`，因此测试工程师编写的是受控数据流，而不是可直接访问服务器文件和系统的 Python 程序。

## 7.3 `database.py`：数据库访问层

### `Database.__init__(path=None)`

- 显式传入路径时使用该路径。
- 否则读取 `TESTSYSTEM_DATA_DIR`。
- 默认数据库为当前工作目录下 `data/testsystem.db`。
- 自动创建父目录。

### `connect()`

每次创建新的 SQLite 连接，并设置：

- `row_factory=sqlite3.Row`：查询结果既能按索引，也能按列名读取。
- `PRAGMA foreign_keys=ON`：SQLite 默认不开外键，必须每个连接开启。
- `busy_timeout=15000`：锁竞争时最多等待 15 秒。
- 连接超时 15 秒。

### `session()`

上下文管理器，负责事务提交/回滚和最终关闭连接。`one()`、`all()`、`execute()` 等短操作使用它，避免 Windows 文件句柄残留。

### `initialize()`

启动时执行：

1. 设置 WAL。
2. 创建不存在的表和索引。
3. 用 `INSERT OR IGNORE` 写入各协议默认配置。
4. 写入工位和 MES 默认配置。
5. 只有脚本表为空时才创建默认仿真脚本。

已有配置不会因为服务重启而被默认值覆盖。

### `transaction()`

用于多步写入：

- `BEGIN IMMEDIATE` 提前获取写锁，避免关键检查与写入之间发生竞争。
- 正常结束提交。
- 任何异常回滚。
- 最终关闭连接。

扫码、启动测试、报告定稿和 MES 状态更新都依赖事务。

### `one()` / `all()`

参数化查询一行或多行。业务代码使用 `?` 占位符传参，不拼接用户输入，降低 SQL 注入风险。

### `execute()`

执行单行更新、插入或删除，返回受影响行数。

### `setting()` / `save_setting()`

读取和保存 JSON 设置。保存使用 SQLite UPSERT：键存在就更新，不存在就插入。

### `recover_interrupted_runs()`

服务重启时：

1. 找出仍为 `RUNNING` 的运行。
2. 将未完成的测试项标记为 FAIL，注明服务重启中断。
3. 将运行标记为 FAIL。
4. 不生成不完整正式报告。

由于还没有正式报告，该 SN 可以重新扫码测试。

## 7.4 `protocol_definitions.py`：协议元数据

### `field(...)`

生成一个前端配置字段的描述，包括：

- `key`：保存到 JSON 的键。
- `label`：页面中文名称。
- `type`：文本、数字、密码、下拉、复选框或 JSON。
- `default`：首次启动默认值。
- `required`：是否提示必填。
- `options`：下拉选项。
- `placeholder`：输入示例。

### `PROTOCOL_DEFINITIONS`

描述九种协议的名称、说明和配置字段。前端不是把每种协议表单写死，而是读取这些元数据动态生成页面。

### `DEFAULT_PROTOCOL_CONFIGS`

从字段默认值生成首次启动的配置 JSON，写入 `protocol_configs`。

增加协议字段时，通常只需要修改这里和对应适配器，前端表单会自动出现新字段。

## 7.5 `protocols.py`：统一通讯适配层

### 通用辅助函数

`_as_int(value)`：同时接受数字和字符串，支持 `0x701` 这类十六进制输入。

`BaseProtocol.encode_data()` / `decode_data()` 调用 `data_codec.py`：

- `text`：按字符编码转换文本指令。
- `decimal`：用 `struct` 按 uint/int/float 类型和大小端转换十进制数值，支持多个值、偏移、长度和缩放。
- `json`：JSON 序列化或解析。
- `hex`：保留给旧动作和响应诊断使用，低代码新建动作不再提供 HEX 输入。

旧 `_encode()` / `_decode()` 以及动作中的 `encoding` 字段仅用于兼容既有脚本。新动作不带 `encoding` 时，执行器把 `None` 传给协议适配器，由该协议的 `send_*`、`receive_*` 配置决定实际格式。

### `ProtocolError`

协议层统一异常。执行器捕获后把它转成测试项失败原因，前端“测试连接”则显示具体连接错误。

### `BaseProtocol`

定义所有协议统一接口：

| 方法 | 作用 |
| --- | --- |
| `connect()` | 建立连接 |
| `close()` | 释放连接 |
| `send(data, encoding)` | 只发送 |
| `receive(timeout_ms, size)` | 只接收 |
| `request(data, encoding, timeout_ms)` | 默认组合 send + receive |
| `execute(operation, params)` | 寄存器、内存等协议特定操作 |
| `test_connection()` | 页面连接测试 |

执行器只依赖这个接口，不需要知道底层是串口还是网络。

### `SimulatorProtocol`

内置仿真产品：

- `set_context()` 注入当前 SN 等运行变量。
- `request()` 根据请求返回仿真响应，并模拟延迟。
- `send()` 模拟接受数据。
- `receive()` 返回默认响应。
- `execute()` 从 `values` 映射读取操作结果。

内置请求：

| 请求 | 默认响应 |
| --- | --- |
| `AT+SN?` | 当前运行 SN |
| `AT+VERSION?` | `1.0.0` |
| `AT+VOLTAGE?` | `12.0` |
| `AT+SELFTEST?` | `PASS` |

`responses` 可覆盖请求映射，例如把 `AT+VOLTAGE?` 改成 `13.5` 来验证 FAIL。`loopback=true` 时未匹配请求原样返回。

### `SerialProtocol`

- `connect()`：延迟导入 `serial`，按端口、波特率、数据位、校验、停止位和超时打开串口。
- `send()`：编码并写入，随后 `flush()`。
- `receive()`：按动作指定超时读取给定字节数。
- `close()`：关闭串口。
- `test_connection()`：确认端口可打开。

真实 AT 设备通常需要 `\r\n` 结束符，应在脚本 `data` 中明确写出。

### `TcpProtocol`

- `connect()`：创建 TCP Client 并设置超时。
- `send()`：`sendall()` 保证全部字节提交给 socket。
- `receive()`：读取一次响应，处理超时和对端关闭。
- `close()`：关闭 socket。
- `test_connection()`：验证地址和端口可连接。

WiFi 复用该类，只额外保存 SSID 等追溯信息。它不负责修改 Windows 的 WiFi 连接。

### `CanProtocol`

- `connect()`：通过 `python-can` 打开指定接口和通道。
- `send()`：构造仲裁 ID、扩展帧、CAN FD 和数据字节。
- `receive()`：在总超时内循环读取；配置 `response_id` 时过滤其他帧。
- `close()`：关闭总线。

新流程的 CAN ID 和数据均按十进制编辑，默认按 `uint16 + 大端` 编解码，可在协议配置中修改。旧 HEX 动作仍可执行。实际 CANopen、UDS 等上层协议可在动作组合或新适配器中扩展，本类提供 CAN 帧收发基础。

### `ModbusProtocol`

- `connect()`：按 `mode` 创建 `ModbusTcpClient` 或 `ModbusSerialClient`。
- `execute()`：执行具体操作并统一处理 `isError()`。
- `close()`：释放客户端。

支持操作：

| operation | 返回 |
| --- | --- |
| `read_holding_registers` | 寄存器数组 |
| `read_input_registers` | 寄存器数组 |
| `read_coils` | 线圈布尔数组 |
| `read_discrete_inputs` | 离散输入布尔数组 |
| `write_register` | 成功时 `OK` |
| `write_coil` | 成功时 `OK` |

常用参数为 `address`、`count`、`value`，从站地址来自协议的 `unit_id`。

### `BluetoothProtocol`

面向 BLE GATT：

- `_run()`：把异步 bleak 调用转换为同步执行器可调用形式。
- `_request()`：连接设备、写特征、按需读特征，并关闭本次连接。
- `request()`：写入后读取。
- `send()`：只写入。
- `receive()`：只读取特征。
- `test_connection()`：确认 BLE 能连接。

经典蓝牙 SPP 不属于 GATT，应先在操作系统配对形成虚拟串口，然后使用 Serial 协议。

### `MqttProtocol`

- `connect()`：配置客户端 ID、账号、TLS，连接 Broker，启动网络循环并订阅响应主题。
- `send()`：向请求主题发布消息并等待发布完成。
- `receive()`：等待响应主题消息。
- `request()`：清空旧事件，发布请求，在超时内等待本次响应。
- `close()`：停止网络循环并断开。

响应通过线程锁和 `threading.Event` 在网络回调与测试线程之间传递。设备端必须把应答发布到配置的 `response_topic`。

### `JLinkProtocol`

- `connect()`：加载 J-Link DLL，选择 SWD/JTAG，按芯片型号和速率连接。
- `execute()`：执行调试器操作。
- `close()`：关闭设备。
- `test_connection()`：连接并读取 Core ID。

支持操作：

| operation | 参数 | 作用 |
| --- | --- | --- |
| `read_memory` | `address`、`size` | 读取内存，返回 HEX |
| `write_memory` | `address`、`data` | 写内存；data 可为数组或 HEX |
| `read_register` | `register` | 读寄存器 |
| `write_register` | `register`、`value` | 写寄存器 |
| `reset` / `halt` / `go` | 无 | 复位、暂停、运行 |

烧录算法、量产 Flash 下载和安全解锁需要按具体芯片进一步封装，不应把当前内存读写误认为完整烧录方案。

### 工厂和目录

- `PROTOCOL_CLASSES`：协议名到实现类的映射；`wifi` 映射到 `TcpProtocol`。
- `DRIVER_MODULES`：协议对应的 Python 模块，用于页面显示驱动是否可用。
- `create_protocol(name, config)`：根据名称创建适配器，未知名称抛出异常。
- `protocol_catalog()`：合并协议元数据和驱动可用状态，供 `/api/protocols` 返回。

## 7.6 `executor.py`：测试执行核心

### `StepFailure`

测试步骤失败时抛出，除了错误消息，还携带：

- `actual`：失败时实际值。
- `expected`：失败时期望值。
- `assertions`：失败前已经产生的断言明细。

这样即使失败发生在中间动作，也能保留足够上下文。

### `_text(value)`

把任意结果转换为数据库可保存的文本：字典和列表转换为 JSON，`None` 转为空字符串，其他值转为字符串。

完整类型仍保留在断言 `details_json` 中，因此页面显示简洁，报告明细仍可追溯。

### `_elapsed_ms(started_at)`

根据 UTC 开始时间计算总耗时，返回非负整数毫秒。

### `_assert(actual, operator, expected)`

所有断言的唯一实现：

| 操作符 | 含义 |
| --- | --- |
| `equals` / `not_equals` | 等于 / 不等于 |
| `contains` / `not_contains` | 字符串包含或容器包含 / 相反 |
| `regex` | Python 正则搜索 |
| `gt` / `gte` / `lt` / `lte` | 转浮点数后比较大小 |
| `between` | `下限 <= 实际值 <= 上限`，闭区间 |
| `truthy` | 真值判断 |
| `exists` | 不是 `None` 且不是空字符串 |

数字转换失败、区间长度错误或数据类型不支持时，由上层转成明确测试失败，而不是让服务进程退出。

### `TestExecutor.__init__()`

保存数据库实例，并创建单工作线程的 `ThreadPoolExecutor`。单线程意味着同一个服务实例一次只执行一个产品，避免硬件并发访问。

### `submit(run_id)`

把已写入数据库的运行提交到后台线程。HTTP 请求可以立即返回运行 ID，前端随后轮询状态。

### `shutdown()`

停止线程池接收新任务，取消尚未开始的任务，并等待正在执行的测试结束，避免退出时数据库连接和硬件线程交叉关闭。

### `_protocol(name, cache, context)`

- 第一次使用某协议时，从数据库读取配置并调用 `create_protocol()`。
- 在一次运行内缓存协议实例，多个步骤可以复用连接。
- 如果是仿真器，更新当前 SN 等上下文。
- 运行结束后统一关闭缓存中的所有协议。

### `_execute_actions(step, context, protocols)`

按数组顺序执行一个步骤中的所有动作：

1. 每个动作前检查步骤耗时是否超过 `timeout_ms`。
2. `sleep`：延时。
3. `set`：模板渲染后写入上下文变量。
4. 获取该步骤指定的协议。
5. `request`：发送并接收，保存到 `last_response` 和可选变量。
6. `send`：只发送。
7. `receive`：接收并保存变量。
8. `execute`：执行 Modbus/J-Link 等特定操作。
9. `assert`：渲染实际值与期望值，执行断言并记录明细。

返回最后一个实际值、最后一个期望值和断言数组。断言失败时抛出 `StepFailure`。

注意：步骤超时是协作式检查。底层驱动也必须设置合理超时；如果厂商驱动自身永久阻塞，Python 无法安全地强杀其内部 C 调用。

### `_run_safely(run_id)`

后台任务最外层保护：

1. 调用 `_execute_run()`。
2. 捕获未预期异常，把未完成项标记为 FAIL，并记录运行异常。
3. 无论成功失败，最终关闭所有协议连接。
4. 调用 `_finalize()` 生成正式报告。
5. 如果开启 `auto_upload`，创建 `MESUploader` 上传；上传失败只记录，不删除报告。

### `_execute_run(run_id, protocols)`

核心状态机：

1. 读取运行和脚本快照。
2. 建立上下文：`sn`、`station`、`operator`、`run_id`、`product_model` 和脚本变量。
3. 按顺序遍历步骤。
4. 更新运行 `current_item` 和测试项 `RUNNING`。
5. 根据 `retries` 计算最大尝试次数。
6. 每次尝试调用 `_execute_actions()`，成功则停止重试。
7. 失败后若还有次数，更新“正在重试”消息。
8. 保存最终状态、实测、期望、消息、实际尝试次数、断言和耗时。
9. 如果步骤失败且 `on_fail=stop`，后续步骤统一记录未执行 FAIL；`continue` 则继续。

### `_finalize(run_id)`

报告定稿事务：

1. 检查该运行是否已有报告，防止线程重复定稿。
2. 读取运行和全部测试项。
3. 把仍为 PENDING/RUNNING 的项按 FAIL 处理。
4. 统计总数、通过数和失败数。
5. 只有项目非空且全部 PASS，报告才是 PASS。
6. 创建报告 ID 和 `RPT-日期-随机码` 报告编号。
7. 构造完整标准报告 JSON，包括脚本、工位、操作员、明细和统计。
8. 更新运行状态与产品最后测试时间。
9. 插入 `reports`，利用唯一约束做最后防线。
10. 返回报告 ID，供自动 MES 上传使用。

## 7.7 `mes.py`：MES 报文和上传

### `MESUploadError`

统一 MES 配置、网络和 HTTP 错误，并保存服务端响应文本，便于写入报告和上传日志。

### `build_payload(report, config)`

两种模式：

- `full`：直接发送完整标准报告。
- `mapped`：把配置中的 `payload_mapping` 递归交给 `render_template()`。

模板上下文既包含报告顶层字段，也包含 `${report}` 这个完整对象。该函数只构造数据，不执行网络请求，因此“报文预览”不会误发 MES。

### `MESUploader.upload(report_id)`

上传状态机：

1. 在事务中读取报告。
2. 报告不存在则报错；已经 SUCCESS 则直接返回，不重复发送。
3. 尝试次数加 1，状态改为 UPLOADING。
4. 读取 MES 配置并构造报文。
5. 调用 `_send()`。
6. 失败时写 FAILED、错误响应和失败日志，然后抛出异常。
7. 成功时写 SUCCESS、响应、上传时间和成功日志。

报告生成与 MES 上传解耦：MES 离线不会导致测试报告丢失。

### `MESUploader._send(payload, report, config)`

- 验证 HTTP/HTTPS 地址和 POST/PUT/PATCH 方法。
- 合并自定义请求头。
- 默认添加 JSON Content-Type。
- 按 `idempotency_header` 写入报告编号。
- 使用 Python 标准库 `urllib.request` 发送 UTF-8 JSON。
- 只有 HTTP 2xx 视为成功。
- 读取最多 64 KiB 响应，避免异常大响应占用内存。
- HTTPError 保留服务端响应，URLError 转成网络错误。
- HTTPS 默认校验证书；`verify_tls=false` 仅适合受控调试，不建议生产使用。

## 7.8 `auth.py`：登录与角色权限

`AuthService` 负责默认账号初始化、PBKDF2 密码校验、HMAC 签名 Cookie、12 小时会话、用户启停和密码重置。`create_auth_router()` 提供 `/api/auth/login`、`logout`、`me`、`password` 及管理员用户管理 API。

`app.py` 的 `authenticate_request` 中间件对除健康检查和登录以外的 `/api` 请求鉴权。操作工只允许读取测试所需流程、工位、运行和报告，以及调用扫码和开始测试接口；其他写操作和配置接口全部要求管理员。开始测试时操作员直接取登录用户名，忽略客户端伪造的 operator 字段。

## 7.9 `app.py`：API 和页面入口

### 请求模型

- `ScriptPayload`：接收 `definition` 脚本对象。
- `SNPayload`：接收 `sn` 和 `script_id`，限制 SN 长度。
- `StartPayload`：在 SNPayload 上增加可选 `operator`。
- `ConfigPayload`：通用配置对象。
- `SettingsPayload`：工位、操作员、SN 正则和自动上传开关。

Pydantic 在业务函数之前拒绝缺失或类型明显错误的请求。

### 内部辅助函数

`_script(row)`：数据库脚本行转 API 对象，解析 definition JSON 和 enabled 布尔值。

`_get_script(script_id, enabled_only=True)`：读取脚本；不存在或已停用时返回 404。

`_validate_sn(sn)`：去除首尾空白，按工位正则执行完整匹配；非法 SN 返回 422，非法正则配置返回 500。

`_run(run_id)`：组合运行、测试项和报告摘要，是启动、轮询和恢复运行共用的响应结构。

`_report_list_item(row)`：报告列表统一序列化，包括 MES 状态和统计。

`_validate_mes_config(config)`：检查 URL、方法、请求头、报文模式和 1~120 秒超时。

### 页面和基础 API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 返回前端 `index.html` |
| GET | `/static/*` | 返回 CSS、JavaScript 等静态资源 |
| GET | `/api/health` | 服务健康、当前时间和工位 |
| GET | `/api/dashboard` | 总量、今日数量、通过率、最近报告和活动运行 |

### 脚本 API

| 方法 | 路径 | 对应函数与作用 |
| --- | --- | --- |
| GET | `/api/scripts` | `list_scripts()`：列出流程及定义 |
| POST | `/api/scripts` | `create_script()`：编译后创建 v1 |
| POST | `/api/scripts/validate` | `validate_script()`：只校验不保存，返回全部错误 |
| PUT | `/api/scripts/{id}` | `update_script()`：编译后保存，版本自动加 1 |
| DELETE | `/api/scripts/{id}` | `disable_script()`：逻辑停用，不删除历史追溯 |

### 通讯 API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/protocols` | 合并协议元数据、当前配置和驱动可用状态 |
| PUT | `/api/protocols/{name}` | 保存连接 JSON |
| POST | `/api/protocols/{name}/test` | 用提交的配置实际尝试连接，并在 finally 中关闭 |

“测试连接”会访问真实设备，只有用户点击或调用 API 时才执行；普通页面加载不会主动连接硬件。

### 工位与 MES 配置 API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/settings` | 读取工位策略 |
| PUT | `/api/settings` | 校验 SN 正则并保存工位策略 |
| GET | `/api/mes/config` | 读取 MES 配置 |
| PUT | `/api/mes/config` | 校验并保存 MES 配置 |
| GET | `/api/mes/preview` | 用最新报告或示例报告生成预览，不发送网络请求 |

### 生产执行 API

`POST /api/test-runs/scan` → `scan_sn()`：

1. 校验 SN 和脚本。
2. 把 SN 登记到 `products`，记录首次扫码时间。
3. 查询是否已有正式报告。
4. 查询工位是否有活动运行。
5. 返回 `can_start`、已有报告或活动运行信息。

`POST /api/test-runs` → `start_test()`：

1. 再次校验 SN、脚本、工位和操作员。
2. 在一个 `BEGIN IMMEDIATE` 事务中检查正式报告和工位占用。
3. 检查 `products` 是否已有扫码记录，没有则拒绝，防止 API 绕过扫码。
4. 创建 RUNNING 运行和脚本快照。
5. 按脚本创建全部 PENDING 测试项。
6. 提交后台执行器并返回运行 ID。

数据库唯一索引处理极端并发，冲突返回 409，而不会生成双报告。

`GET /api/test-runs/active`：页面刷新后恢复尚未完成的运行。

`GET /api/test-runs/{run_id}`：返回当前状态和逐项明细，供前端轮询。

### 报告 API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/reports` | 按 SN 模糊搜索、结果、MES 状态筛选，支持 limit/offset |
| GET | `/api/reports/{id}` | 返回完整报告和 MES 状态 |
| GET | `/api/reports/{id}/download` | 下载标准 JSON，文件名使用报告编号 |
| GET | `/api/reports/{id}/upload-logs` | 查询每次 MES 请求和响应 |
| POST | `/api/reports/{id}/upload` | 手动上传或失败重试 |

`upload_report()` 把 MES 异常转换为 HTTP 502，表示上游 MES 不可用或拒绝，不是本地报告丢失。

## 8. 前端详细说明

## 8.1 `static/index.html`：页面结构

### 侧边栏和顶部栏

- 登录页：账号密码登录、角色说明和默认账号快捷填充。
- `.sidebar`：品牌、当前工位、六个主导航、当前用户、修改密码和退出入口。
- `.topbar`：页面标题、说明、本地时间、当前角色和 PRODUCTION 标识。

导航通过 `data-page` 与六个 `<section>` 对应，不依赖第三方路由。操作工只显示生产测试和报告中心，管理页面同时由前端隐藏、导航拦截和后端中间件保护。

### 生产测试页 `#page-production`

- 指标卡：今日测试、今日通过率、累计报告、累计失败。
- 流程选择：`#production-script`。
- 扫码表单：`#scan-form`、`#sn-input`。
- 扫码反馈：`#scan-feedback`。
- 进度：`#run-progress`、`#overall-badge`。
- 测试项表格：`#test-items`。
- 当前产品：`#unit-sn`、`#unit-model`、`#unit-station` 等。
- 操作员：`#operator-input`，只读展示当前登录账号，后端启动运行时强制使用会话用户名。
- 开始与清空：`#start-test`、`#reset-test`。
- 最近报告：`#recent-reports`。

扫码枪通常模拟键盘输入，因此结束符配置为 Enter 后可以直接提交表单，不需要额外扫码枪 SDK。

### 流程编排页 `#page-scripts`

- 流程库：`#script-list`。
- 新建、校验、保存按钮。
- 名称和型号：`#draft-name`、`#draft-model`。
- 步骤容器：`#steps-container`。
- 校验结果：`#validation-results`。
- 高级 JSON：`#script-json`。

表单模式和 JSON 模式编辑的是同一个 `state.draft`，保存前都经过后端编译。

### 通讯配置页 `#page-protocols`

- 协议列表：`#protocol-tabs`。
- 动态字段表单：`#protocol-form`。
- 完整 JSON：`#protocol-json`。
- 驱动状态：`#driver-badge`。
- 连接测试结果：`#protocol-test-result`。

### MES 页 `#page-mes`

包含地址、方法、超时、请求头、幂等头、报文模式、字段映射，以及工位号、默认操作员、SN 正则和自动上传。`#mes-preview` 只展示本地预览。

### 报告中心 `#page-reports`

- 筛选：`#report-filter`。
- 表格：`#reports-table`。
- 报告弹窗：`#report-modal`。
- 全局消息：`#toast-container`。

## 8.2 `static/styles.css`：视觉与布局

主要分组：

- `:root`：颜色、边框、阴影和字体变量，便于统一换主题。
- `.app-shell`：桌面端左侧导航 + 右侧内容网格。
- `.metric-card`、`.card`：指标与业务卡片。
- `.scan-input-row`：扫码输入区域，扫码时保持明显焦点。
- `.badge-pass/.badge-fail/.badge-running`：结果状态色。
- `.step-card/.action-row`：低代码步骤与动作编辑器。
- `.config-grid`：协议和 MES 配置表单。
- `.modal-backdrop`：报告详情弹窗。
- `.toast-container`：操作成功和错误提示。

响应式：

- 1200px 以下缩小侧边栏和步骤字段列数。
- 900px 以下导航变为顶部图标，业务面板改为单列。
- 600px 以下进一步压缩指标、表单和报告弹窗。

`@media print` 只显示报告弹窗，隐藏系统导航和操作按钮，实现直接打印测试报告。

## 8.3 `static/app.js`：全局状态

`state` 主要字段：

| 字段 | 用途 |
| --- | --- |
| `scripts` | 后端流程库 |
| `selectedScriptId` | 生产台当前流程 |
| `draft` / `draftId` / `draftVersion` | 编辑器草稿和版本 |
| `scan` | 当前 SN 校验结果 |
| `run` | 当前运行与逐项状态 |
| `pollTimer` | 运行轮询定时器 |
| `protocols` / `selectedProtocol` | 协议目录和选中项 |
| `mesConfig` / `settings` | MES 与工位配置 |
| `reports` | 报告中心当前查询结果 |

## 8.4 前端通用函数

### `$()` 和 `$$()`

`querySelector` 和 `querySelectorAll` 简写，后者转换为数组，便于遍历。

### `clone(value)`

通过 JSON 序列化深拷贝脚本，避免编辑器草稿直接修改流程库原对象。

### `escapeHtml(value)`

替换 `& < > ' "`，所有来自 SN、配置、报告和错误的动态文本在插入 `innerHTML` 前转义，降低存储型 XSS 风险。

### `displayValue(value)`

把空值显示为 `—`，对象显示为 JSON，其余转为字符串。

### `formatTime()` / `formatDuration()`

UTC 时间转本地时间；毫秒根据大小显示为 ms 或秒。

### `statusBadge()` / `mesStatusText()`

生成状态徽章和 MES 中文说明。

### `api(url, options)`

统一 `fetch` 封装：

1. 对象 body 自动 JSON 序列化并添加 Content-Type。
2. 尝试解析 JSON 响应。
3. 非 2xx 时提取 FastAPI `detail`、编译错误或参数错误。
4. 抛出带可读消息的 Error。
5. 成功时返回解析后的数据。

### `toast()` / `parseJsonField()` / `parseSmartValue()`

- `toast()`：右下角临时消息。
- `parseJsonField()`：JSON 配置解析并标注是哪个字段出错。
- `parseSmartValue()`：把编辑器中的数字、`true`、`false`、`null` 转成对应类型，普通文本保持字符串。

### `navigate(page)`

切换页面和导航状态，更新标题与 URL hash，并按需加载仪表盘、协议、MES 或报告数据。

## 8.5 生产测试函数

### `loadDashboard()`

读取统计和最近报告，更新指标卡及最近报告表格。

### `loadScripts()`

加载启用流程，保持合理选中项，并生成生产台下拉框。

### `currentProductionScript()`

根据当前下拉值从 `state.scripts` 找完整脚本。

### `renderTestItems(items)`

统一渲染预览、运行中和已完成测试项，包括状态行颜色、实测、期望、消息、耗时和结果。

### `renderPreview()`

尚未开始运行时，根据选中脚本生成 PENDING 预览，显示型号、版本和当前 SN，设置开始按钮状态。

### `resetConsole()`

停止轮询、清空 SN/扫码/运行状态，重新启用流程选择并聚焦扫码框，为下一台产品做准备。

### `scanSn()`

1. 读取 SN 和流程。
2. 调用 `/api/test-runs/scan`。
3. 展示唯一性和工位检查结果。
4. 可以开始时启用按钮。
5. 已有报告时提供查看入口。
6. 工位有活动运行时恢复对应运行。

### `startTest()`

确认扫码状态和操作员，调用 `POST /api/test-runs`，锁定 SN 和流程选择，随后开始轮询。

### `renderRun()`

把后端运行状态完整反映到页面：进度、当前产品、步骤明细、运行状态、MES 状态和报告入口。

### `pollRun()`

运行中每 450ms 查询一次；完成后提示 PASS/FAIL 并刷新仪表盘。请求失败时退到 1500ms 后重试，避免网络短暂波动直接丢失当前运行。

### `resumeRun(runId)`

页面刷新或发现活动运行时恢复 SN、脚本、状态和轮询。

## 8.6 低代码编辑器函数

### `defaultAction(type)`

为动作类型生成默认参数。例如 request 默认 AT 指令，assert 默认等于 PASS，Modbus execute 默认读保持寄存器。

### `newStep(index)`

生成带唯一步骤 ID、默认仿真协议、超时和 request + assert 两个动作的新步骤。

### `optionTags()` / `textActionField()`

生成下拉选项和动作输入框，减少重复 HTML。

### `renderAction(action, si, ai)`

根据动作类型动态生成不同字段：

- request/send：数据、编码、保存变量。
- receive：变量、超时、字节数。
- execute：操作名、JSON 参数、结果变量。
- set：变量名和值。
- assert：实际值、操作符、期望值、失败提示。
- sleep：毫秒。

`si`、`ai` 分别是步骤和动作下标，用于事件更新正确定位。

### `renderSteps()`

渲染所有步骤卡片、上下移动、删除、协议、超时、重试、失败策略和动作列表。

### `openScript()` / `renderScriptLibrary()`

打开已有脚本为草稿；刷新流程库列表、元数据、JSON 和步骤编辑器。

### `createNewScript()`

创建未保存草稿，清空 draftId 和版本。

### `validateScript(showSuccess)`

调用后端 `/api/scripts/validate`，显示成功信息或全部字段错误，不直接保存。

### `saveScript()`

先校验，再按 draftId 决定 POST 新建或 PUT 更新；保存后刷新版本、流程库和生产台下拉框。

### `updateActionField(element)`

把输入事件写回草稿，处理数字、JSON 参数、数值区间、布尔值等类型；操作符变化时重新渲染期望字段。

### `bindScriptEditor()`

集中绑定新建、校验、保存、添加步骤、步骤排序、动作增删、字段输入和高级 JSON 应用事件。

## 8.7 通讯配置函数

### `loadProtocols() / selectedProtocol()`

加载协议元数据、配置和驱动状态，并保持当前选中协议。

### `renderProtocols()`

动态生成协议导航和字段表单，同时显示完整 JSON 与驱动状态。

### `collectProtocolConfig()`

先解析高级 JSON，再用页面字段覆盖对应键。数字、复选框和 JSON 字段分别转换类型。

### `saveProtocol(showToast)`

PUT 保存当前协议，更新本地配置和 JSON 编辑器。

### `testProtocol()`

先保存，再调用连接测试 API；显示成功或具体驱动/连接错误。

## 8.8 MES 与工位配置函数

### `loadMesSettings()`

并行读取 MES 和工位配置，填充页面，同时更新侧边工位和生产台默认操作员。

### `saveMesSettings(showToast)`

解析请求头和映射 JSON，并行保存 MES 与工位策略，更新本地状态。

### `previewMes()`

先保存配置，再请求本地预览。不会调用真实 MES。

## 8.9 报告和全局事件函数

### `loadReports()`

按 SN、结果、MES 状态查询，渲染详情、上传和下载操作。

### `viewReport(reportId)`

并行读取完整报告与上传日志，显示报告摘要、逐项断言、MES 响应，以及打印、下载和上传按钮。

### `uploadReport(reportId, button)`

调用上传 API，防止按钮重复点击；成功后刷新报告、仪表盘和当前运行 MES 状态；失败后显示原因并恢复操作。

### `closeReport()`

关闭并清空报告弹窗。

### `bindEvents()`

统一绑定导航、扫码、F2、筛选、配置保存、报告操作、弹窗关闭和打印事件。大量按钮使用事件委托，因此动态重新渲染表格后仍然有效。

### `initialize()`

页面入口：

1. 绑定事件并启动本地时钟。
2. 检查后端健康状态。
3. 并行加载流程、MES 和工位配置。
4. 初始化生产台。
5. 恢复活动运行。
6. 根据 URL hash 打开对应页面。

## 8.10 React Flow 复杂图形编辑器

复杂画布现在使用 React + TypeScript + React Flow，源码位于 `src/testsystem/frontend`，支持五类节点：test、condition、parallel、loop 和 subflow。节点采用递归 AST，画布由 AST 生成，保存时仍使用原来的 `state.draft` 和脚本 API。

### 页面区域

- 节点库：拖入测试、条件、并行、循环和子流程调用。
- React Flow 画布：条件 TRUE/FALSE 分支、并行分支汇聚、循环回边、缩放和小地图。
- 属性检查器：编辑节点、条件、分支、循环参数、子流程输入和七类动作。
- 子流程切换：主流程与多个可复用子流程分别编辑。
- 步骤列表和高级 JSON：与 React 编辑器共享草稿。

### React 源码职责

- `FlowEditor.tsx`：React Flow 节点、Dagre 布局、拖放、子流程切换、属性检查器和全局挂载。
- `model.ts`：v2 默认节点、不可变路径更新、插入、删除和同层重排。
- `types.ts`：递归节点定义和界面标签。
- `flow-editor.css`：React Flow 和检查器样式。

`PilotFlowEditor.mount()` 把编辑器挂载到现有页面；`setDefinition()` 同步原页面草稿。React 的 `onChange` 只更新 `state.draft`，保存和编译仍由原页面负责。

旧 `flow-model.js` 继续服务于传统顺序列表。旧的 `renderVisualEditor()` 和 `bindVisualEditor()` 保留为兼容代码，但默认画布由 React 接管。

完整结构、执行语义、构建方式和函数说明见 [控制流与 React Flow 开发说明](CONTROL_FLOW.md)。

## 9. 低代码脚本完整参考

## 9.1 顶层结构

```json
{
  "schema_version": 1,
  "name": "示例产品生产测试",
  "product_model": "PRODUCT-100",
  "variables": {
    "firmware_expected": "1.0.0",
    "voltage_min": 11.5,
    "voltage_max": 12.5
  },
  "steps": []
}
```

- `schema_version` 当前固定为 1。
- `name` 和 `product_model` 会进入运行及报告快照。
- `variables` 是初始上下文，可被 `set` 动作覆盖。
- `steps` 按数组顺序执行。

## 9.2 步骤字段

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | 是 | 字母开头，只允许字母、数字、下划线、连字符，同一脚本唯一 |
| `name` | 是 | 页面和报告中的测试项名称 |
| `protocol` | 是 | simulator/serial/can/tcp/modbus/bluetooth/wifi/mqtt/jlink |
| `timeout_ms` | 否 | 步骤超时，默认 5000 |
| `retries` | 否 | 失败后额外重试次数，默认 0 |
| `on_fail` | 否 | `stop` 停止后续或 `continue` 继续 |
| `actions` | 是 | 顺序执行的动作数组 |

## 9.3 动作字段

### request：发送并等待响应

```json
{
  "type": "request",
  "data": "AT+VERSION?\r\n",
  "save_as": "firmware"
}
```

返回同时写入 `last_response` 和 `firmware`。新动作不填写 `encoding`，由协议配置决定下发格式；旧动作的 `encoding: text/hex/json` 仍兼容。

### send：只发送

```json
{"type": "send", "data": "1, 3, 0, 2"}
```

适合先发后收，或不需要响应的指令。十进制数据按协议配置的数值类型和字节序下发；文本协议也可以直接填写 AT 等指令。

### receive：等待接收

```json
{
  "type": "receive",
  "save_as": "response",
  "timeout_ms": 1500,
  "size": 4096
}
```

### execute：设备特定操作

```json
{
  "type": "execute",
  "operation": "read_holding_registers",
  "params": {"address": 0, "count": 2},
  "save_as": "registers"
}
```

### set：设置变量

```json
{"type": "set", "variable": "expected", "value": "PASS"}
```

也支持模板：

```json
{"type": "set", "variable": "command", "value": "BIND:${sn}"}
```

### assert：判定结果

```json
{
  "type": "assert",
  "actual": "${registers.0}",
  "operator": "between",
  "expected": [1100, 1300],
  "message": "采样电压超出允许范围"
}
```

`truthy` 和 `exists` 不需要 expected；between 需要两个元素的数组。

### sleep：等待

```json
{"type": "sleep", "duration_ms": 500}
```

用于产品启动稳定、继电器动作后等待等场景。

## 9.4 一个完整自定义流程

```json
{
  "schema_version": 1,
  "name": "串口产品出厂测试",
  "product_model": "UART-100",
  "variables": {"expected_version": "1.0.0"},
  "steps": [
    {
      "id": "sn_binding",
      "name": "产品 SN 绑定",
      "protocol": "serial",
      "timeout_ms": 5000,
      "retries": 1,
      "on_fail": "stop",
      "actions": [
        {"type": "request", "data": "AT+SN?\r\n", "save_as": "device_sn"},
        {"type": "assert", "actual": "${device_sn}", "operator": "equals", "expected": "${sn}"}
      ]
    },
    {
      "id": "firmware",
      "name": "固件版本",
      "protocol": "serial",
      "timeout_ms": 5000,
      "retries": 1,
      "on_fail": "continue",
      "actions": [
        {"type": "request", "data": "AT+VERSION?\r\n", "save_as": "version"},
        {"type": "assert", "actual": "${version}", "operator": "equals", "expected": "${expected_version}"}
      ]
    }
  ]
}
```

SN 绑定步骤建议同时校验产品返回 SN 与扫码 SN，避免扫了 A 产品却实际测试 B 产品。

## 9.5 运行上下文

默认可用：

- `${sn}`：扫码 SN。
- `${station}`：工位。
- `${operator}`：操作员。
- `${run_id}`：运行 ID。
- `${product_model}`：产品型号。
- `${last_response}`：最近一次协议响应。
- 顶层 variables 和 save_as/set 创建的变量。

## 10. 标准报告和 MES 配置参考

## 10.1 标准报告字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schema_version` | string | 报告结构版本 |
| `id` / `report_no` | string | 内部报告 ID / 对外唯一编号 |
| `sn` / `run_id` | string | 唯一 SN / 对应运行 |
| `product_model` | string | 型号快照 |
| `station` / `operator` | string | 工位与操作员快照 |
| `script` | object | 脚本 ID、名称、版本 |
| `status` | string | PASS 或 FAIL |
| `started_at` / `finished_at` | string | UTC 时间 |
| `duration_ms` | number | 总耗时 |
| `summary` | object | total、passed、failed |
| `items` | array | 逐项结果 |

每个 item 包含：

- `item_id`、`node_id`、`path`、`iteration`、`name`、`sequence`。
- `status`、`expected`、`actual`、`message`。
- `attempts`、`assertions`。
- `started_at`、`finished_at`、`duration_ms`。

## 10.2 默认 MES 包装报文

```json
{
  "event": "production_test.completed",
  "idempotencyKey": "${report_no}",
  "data": "${report}"
}
```

`${report}` 整体作为值，因此发送时是嵌套对象，不是 JSON 字符串。

## 10.3 自定义 MES 字段映射示例

```json
{
  "factory": "F01",
  "line": "L02",
  "serialNumber": "${sn}",
  "result": "${status}",
  "testDate": "${finished_at}",
  "workStation": "${station}",
  "recipe": {
    "name": "${script.name}",
    "version": "${script.version}"
  },
  "measurements": "${items}"
}
```

如果 MES 要求结果只能是 1/0，目前可在后端 `build_payload()` 前增加结果转换，或扩展模板过滤器；不要通过字符串包含多个占位符来伪造数字和数组类型。

## 10.4 MES 上传要求

- MES 接收端应校验 `Idempotency-Key` 或 `idempotencyKey`，自己也要防止重复写入。
- 网络超时和 500 不应删除本地报告。
- MES 返回 2xx 才标记成功；仅返回 HTTP 200 但 body 表示业务失败时，当前版本仍会视为成功。如果现场 MES 使用这种协议，应在 `_send()` 中增加业务码校验配置。
- 上传日志可能包含 SN，应按生产数据控制访问。
- 生产环境建议 HTTPS，并校验证书。

## 11. 典型业务流程时序

```text
扫码枪 → 前端 scanSn() → POST /scan
                         → SN 正则、产品登记、报告/工位检查
用户 F2 → 前端 startTest() → POST /test-runs
                           → 再检查扫码/报告/工位
                           → 创建 RUNNING + PENDING 明细
                           → 返回 run_id
后台 TestExecutor → 逐动作通讯 → 逐项断言/重试 → 更新 SQLite
前端 pollRun() → 每 450ms GET run → 更新页面
执行结束 → _finalize() 事务 → 唯一正式报告
         → auto_upload 开启时调用 MESUploader
用户报告中心 → 查看/打印/下载/手动上传或重试
```

## 12. 如何扩展

### 12.1 增加一种新产品

通常不需要改 Python：

1. 在通讯配置中设置实际连接。
2. 新建测试流程并填写产品型号。
3. 按产品协议组合 request/execute/assert。
4. 点击编译校验。
5. 保存流程，回到生产台选择该流程。
6. 先用样机验证 PASS/FAIL 和异常断线，再上线。

### 12.2 增加一种动作类型

需要同步修改：

1. `compiler.py` 的 `ACTION_TYPES`。
2. `_validate_action()` 增加字段规则。
3. `executor.py` 的 `_execute_actions()` 增加执行分支。
4. `static/app.js` 的 `actionLabels`。
5. `defaultAction()` 提供默认参数。
6. `renderAction()` 提供低代码字段。
7. 文档和端到端验证。

不要只在执行器中增加动作而跳过编译器，否则前端无法得到明确配置错误。

### 12.3 增加一种协议

需要：

1. 在 `protocol_definitions.py` 增加字段元数据和默认配置。
2. 在 `protocols.py` 继承 `BaseProtocol`。
3. 实现 connect/send/receive/execute/close 中需要的接口。
4. 注册到 `PROTOCOL_CLASSES` 和 `DRIVER_MODULES`。
5. 把协议名加入 compiler 的 `SUPPORTED_PROTOCOLS`。
6. 在前端 `protocolSymbols` 增加简称。
7. 在 `pyproject.toml` 硬件依赖中增加必要驱动。
8. 用真实设备验证连接、超时、断线和关闭。

### 12.4 增加 MES 业务响应校验

目前以 HTTP 2xx 为成功。若 MES 返回：

```json
{"code": 0, "message": "ok"}
```

可在 MES 配置增加 `success_body_path` 和 `success_body_value`，然后在 `_send()` 中解析 body 并调用 `resolve_path()` 判断。配置未设置时保持现有 2xx 行为。

### 12.5 增加重测审批

当前严格“一 SN 一报告”，没有页面重测入口。若业务需要重测，不建议删除唯一约束后直接重跑。更合理的设计：

- 保留首份报告不可变。
- 新增 `retest_authorizations`，记录审批人、原因、时间。
- 报告增加 `test_round` 或建立 SN 与报告的一对多版本关系。
- MES 明确是否支持更新原报告或追加新报告。
- 保留原始失败数据和全部重测历史。

这是业务模型变化，需要现场质量/MES 团队确认，不能简单删除唯一索引。

### 12.6 多工位集中化

推荐边缘执行架构：

- 每个工位保留本地执行器和硬件适配。
- 中央服务使用 PostgreSQL。
- 工位通过认证 API 上报运行和报告。
- 断网时本地缓存，恢复后按报告编号幂等补传。
- 引入用户、角色、电子签名、审计日志和脚本审批。
- 不要把多个 Uvicorn worker 当成多工位调度器。

## 13. 部署与安全建议

1. 使用 Windows 服务或受控进程管理器启动，避免用户误关终端。
2. 数据目录放在可备份、权限受控的磁盘，不要放在源码目录同步盘中。
3. 定期备份 SQLite，备份时采用 SQLite 在线备份或停止写入，避免直接复制不一致文件。
4. 只允许受信产线内网访问；跨网段时增加反向代理、TLS 和身份认证。
5. 系统已有登录和角色权限，但仍不应直接暴露到公网；上线前修改所有初始密码，HTTPS 环境启用安全 Cookie。
6. MQTT、MES 密码存储在本地数据库，必须限制文件访问。
7. 流程编辑、协议配置和 MES 上传已由后端限定为管理员权限，不要仅依赖隐藏前端按钮。
8. 为关键硬件设置断开恢复策略和 watchdog，但不要让 watchdog 删除失败数据。
9. 实际产品协议应增加样例记录和边界测试：超时、空响应、非法 HEX、错误 SN、重复帧、断线、重试成功等。
10. 生产升级前先备份数据库，并用仿真流程验证升级版本。

## 14. 验证记录与已知边界

当前实现已执行以下验证：

- Python 源码编译检查。
- JavaScript `node --check` 语法检查。
- `uv build` 构建，以及 wheel 内前端资源检查。
- 实际启动 HTTP 服务，访问健康接口、首页和静态 JavaScript。
- 仿真产品四个测试项全部 PASS。
- 修改仿真电压为 13.5 后对应项 FAIL，后续 continue 项仍执行。
- 未扫码直接调用开始 API 被拒绝；正常扫码后测试通过。
- 同一 SN 重复扫码和重复开始被拦截，数据库只有一份报告。
- 自定义脚本创建、编辑升到 v2、执行并生成 v2 快照报告。
- v2 条件 TRUE/FALSE、计数/while 循环、并行、子流程输出、失败继续和循环上限验证。
- 非法子流程引用、循环调用、控制事件 FAIL 对最终报告的影响验证。
- React 五类节点工厂、嵌套插入、不可变重排、后端编译及 React Flow 渲染初始化验证。
- 条件、并行、循环和子任务专用编辑面板渲染，以及子任务递归引用检查验证。
- 图形数据模型的步骤插入/排序、动作插入/跨项移动、边界检查和不可变性验证。
- 实际调用前端拖放事件生成流程，再交由后端编译、执行并生成报告。
- 本地 MES 首次返回 500，报告标记 FAILED；重试返回 201 后标记 SUCCESS。
- MES 字段映射、幂等请求头、成功后不重复发送和两条上传日志。
- Windows 下后台结束和 SQLite 临时文件清理。

尚未在真实串口、CAN、Modbus、BLE、MQTT、J-Link 硬件上联调。协议适配代码已提供，但现场仍需验证具体设备驱动、电气连接、产品协议和超时。

自动化测试位于 `tests/`，覆盖控制流编译、十进制编解码、协议旧配置迁移、未登录拦截、管理员/操作工权限、用户管理、密码会话失效和操作工完整仿真测试。运行 `uv run pytest`；前端运行 `npm run typecheck` 和 `npm run build`。真实硬件和现场 MES 仍需按设备协议单独联调。

## 15. 常见问题排查

### 页面打不开

检查 `uv run testsystem` 是否仍在运行、端口是否被占用、访问地址是否与 `TESTSYSTEM_HOST/PORT` 一致。

### 扫码后不能开始

查看扫码反馈：可能是 SN 正则不匹配、已有报告或工位仍有活动运行。不要直接删除数据库；先确认质量和追溯要求。

### 测试一直 RUNNING

检查当前步骤对应硬件和驱动超时。页面刷新不会终止后台测试；服务正常重启会把中断运行标记失败。

### 串口/CAN/J-Link 驱动不可用

执行 `uv sync --extra hardware`，再检查厂商驱动、端口占用、设备管理器和连接参数。

### MES 失败但报告存在

这是预期设计。到报告中心查看响应和上传日志，修正网络或配置后重试，不要重新测试同一 SN 来“补报告”。

### 修改脚本后旧报告变了

正常不会。旧运行和报告使用启动时快照；只有脚本库当前版本会变化。

### 数据库文件被占用

确保服务已正常停止，不要同时用多个进程操作同一工位数据库。项目数据库方法会显式关闭连接，正常退出后不应长期保留锁。
