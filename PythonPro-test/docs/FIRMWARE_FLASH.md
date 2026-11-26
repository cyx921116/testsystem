# 固件烧录框架开发说明

## 1. 定位与当前阶段

生产测试经常要求在测试前或测试过程中把指定固件烧录到产品，并记录“哪个 SN 在什么时间烧了什么版本的固件”。本框架为该系统提供统一的固件烧录能力：

- 固件文件统一入仓管理，带版本和 SHA-256 校验和，防止烧错文件；
- 烧录后端与具体烧录器解耦，同一套流程可换 J-Link、外部 CLI 工具或仿真器；
- 烧录结果生成可追溯记录，为后续写入数据库和报告做准备。

当前**已接入主系统**：流程脚本通过 `flash` 动作烧录已登记固件，烧录器连接由执行器按工位配置管理，结果写入 `flash_records` 表并随运行和报告追溯，前端提供固件管理页面和烧录动作字段。接入方式见第 6 节。J-Link 后端的安全解锁和量产校验仍是 TODO，量产前必须按芯片封装。

## 2. 总体结构

```text
烧录请求（固件名称/ID + SN + 选项）
        │
        ▼
flasher.py
├── 固件仓 firmware_dir()/register_firmware()/find_firmware()
│   └── data/firmware/ + firmware.json 清单（版本、SHA-256）
├── validate_flash_request()  规范化请求与默认选项
├── BaseFlasher               统一后端接口
│   ├── SimulatorFlasher      仿真烧录，无硬件验证流程
│   ├── JLinkFlasher          J-Link 烧录骨架（待按芯片封装）
│   └── ExternalToolFlasher   调用外部 CLI 烧录工具
├── build_flash_record()      可追溯烧录记录
└── create_flasher()/flasher_catalog()  工厂与目录
```

## 3. 固件仓

固件仓位于 `TESTSYSTEM_DATA_DIR/firmware`，默认是当前目录下 `data/firmware`，与 SQLite 数据库同受数据目录环境变量控制。两个部分：

- 固件文件本体：二进制文件原样复制入仓；
- `firmware.json` 清单：JSON 数组，每个固件一条记录。

清单条目字段：

| 字段 | 含义 |
| --- | --- |
| `id` | `fw_` 前缀随机 ID，供 API 按 ID 引用 |
| `name` | 文件名，生产脚本中按名称引用 |
| `version` | 固件版本，如 `1.2.0` |
| `product_model` | 适用产品型号，可选 |
| `description` | 说明，可选 |
| `size` / `sha256` | 文件大小与 SHA-256 校验和 |
| `registered_at` | 登记时间（UTC） |

管理函数：

- `register_firmware(source, version, product_model="", description="")`：复制文件入仓、计算校验和、写入清单。同名固件重新登记会**覆盖**旧条目和旧文件，因此产线建议文件名带版本号（如 `app_v1.2.0.bin`）。
- `find_firmware(name_or_id)`：按名称或 ID 查找，并校验文件存在且 SHA-256 与登记一致，防止文件被替换后烧入错误固件。
- `load_firmware_index()` / `save_firmware_index()` / `validate_firmware()`：清单读写与单条校验。

## 4. 烧录请求与选项

一次烧录调用传入 dict：

```python
{
    "firmware": "app_v1.2.0.bin",   # 固件名称或 ID，必须已登记
    "sn": "SN-DEMO-0001",           # 可选，写入烧录记录
    "target": "",                   # 可选，目标槽位/地址描述
    "options": {                     # 可选，与默认值合并
        "erase": True,               # 烧录前擦除
        "verify": True,              # 烧录后校验
        "reset": True,               # 烧录后复位运行
        "timeout_ms": 60000          # 烧录总超时
    },
    "variables": {}                  # 传给命令模板的额外变量
}
```

`validate_flash_request()` 负责规范化：固件必须已登记，选项与 `default_flash_options()` 合并，超时必须是正数。

在测试流程脚本中，一次烧录就是一个 `flash` 动作（v1 步骤与 v2 测试节点均可使用）：

```json
{"type": "flash", "firmware": "app_v1.2.0.bin", "save_as": "flash_result",
 "options": {"erase": true, "verify": true, "reset": true, "timeout_ms": 60000}}
```

`firmware` 支持 `${变量}` 在运行时解析，前端编辑器会列出固件仓中已登记的固件供选择；`save_as` 保存烧录结果，后续动作可用 `${flash_result.ok}` 判定烧录是否成功、用 `${flash_result.firmware.version}` 等路径断言版本；`target`、`variables` 会透传给烧录后端（如外部工具命令模板）。

## 5. 烧录后端

### 5.1 统一接口 `BaseFlasher`

| 方法 | 作用 |
| --- | --- |
| `probe()` | 检查驱动/配置是否可用，返回 `{"ok": bool, "message": str}` |
| `flash(request, on_progress=None)` | 执行烧录，返回结果 dict |
| `close()` | 释放资源 |

`flash()` 统一返回：

| 字段 | 含义 |
| --- | --- |
| `ok` | 是否成功（失败时抛 `FlashError`） |
| `backend` | 后端名 |
| `firmware` | 固件清单条目（含路径） |
| `duration_ms` / `finished_at` | 耗时与完成时间 |
| `message` / `details` | 人类可读结果与结构化明细 |

`on_progress(percent, message)` 回调用于后续前端进度条。

### 5.2 `SimulatorFlasher`

仿真烧录器，不访问硬件。按“连接烧录器 → 擦除 → 写入 → 校验 → 复位”分阶段推进，阶段间隔由 `step_delay_ms` 控制；`fail_at_stage` 可指定在某个阶段注入失败，用于验证失败流程和重试逻辑。默认选项中的 `erase/verify/reset=false` 会跳过对应阶段。

### 5.3 `JLinkFlasher`

J-Link 烧录骨架，流程为：连接（复用通讯配置中的 `dll_path`、`interface`、`chip_name`、`speed_khz`）→ 擦除 → `flash_file()` 下载（使用 SEGGER J-Link 自带设备算法，`base_address` 可配，默认 `0x0`）→ 复位。已明确的 TODO：

- 安全解锁（RDP/TrustZone 回退）按芯片封装；
- 量产校验（CRC 或回读比对）按芯片封装，当前 `verify=True` 时只记录 `verify_note` 说明未校验，**不会**静默声称已校验。

需要 `uv sync --extra hardware` 安装 `pylink-square`，并安装 SEGGER J-Link Software。尚未在真实硬件上验证。

### 5.4 `ExternalToolFlasher`

通过命令模板调用外部烧录工具（如 STM32CubeProgrammer CLI、openocd、厂商量产工具），适合框架阶段就有真实工具可用的产线。配置：

| 配置 | 含义 |
| --- | --- |
| `command` | 命令模板，支持 `${firmware}`、`${sn}`、`${target}` 和 `variables` 变量 |
| `timeout_ms` | 超时，默认 120000 |
| `success_text` | 返回码为 0 且输出包含该文本才判定成功；留空则只看返回码 |

工具返回码非 0 或超时抛出 `FlashError`，错误信息带输出尾部，便于排查。Windows 下按非 POSIX 规则拆分命令以兼容反斜杠路径。

### 5.5 工厂与目录

- `create_flasher(name, config)`：按名称创建后端，未知名称抛 `FlashError`；
- `flasher_catalog()`：列出后端名称、说明和驱动可用状态（`pylink` 未安装时 `jlink` 标记为不可用），风格与 `protocol_catalog()` 一致，供未来配置页面使用。

## 6. 主系统接入

固件烧录已按以下方式接入主系统：

1. **编译器**：`compiler.py` 的 `ACTION_TYPES` 包含 `flash`，`_validate_action()` 校验固件名称非空且已登记（含 `${变量}` 的固件名留到运行时解析）、`options` 中擦除/校验/复位为布尔值、超时为正数；v2 控制流经 `control_compiler.py` 复用同一校验。
2. **执行器**：`executor.py` 的 `_execute_actions()` 增加 `flash` 分支，按工位烧录配置（settings 表 `flasher` 键）经 `create_flasher()` 创建烧录器并在运行内缓存复用；烧录进度通过 `on_progress` 回调实时写入测试项消息，前端轮询即可看到“烧录 40%：写入固件”；`FlashError` 转换为 `StepFailure`，按步骤失败处理（重试/`on_fail`）；并行分支使用独立烧录器实例，运行结束统一关闭。
3. **数据库**：`flash_records` 表保存每次烧录的 `build_flash_record()` 结果（运行、测试项路径、SN、固件名称/版本/校验和、后端、结果、耗时、时间），`/api/test-runs/{id}` 与正式报告 JSON 均携带记录。
4. **前端**：脚本编辑器（列表与 React 画布）提供“固件烧录”动作字段，固件名称带固件仓下拉候选；“固件烧录”管理页提供固件登记（工作站路径或打开文件夹选择文件上传）、清单、SHA-256 校验和后端选择、`probe()` 测试；生产测试页经测试项消息展示烧录进度；报告详情展示烧录记录。
5. **API**：`/api/firmware`（清单/按路径登记/校验）、`/api/firmware/upload`（浏览器文件上传登记）、`/api/flashers`（后端目录）、`/api/flasher/config`（后端配置）、`/api/flasher/test`（连接测试），均仅管理员可用。
6. **MES**：如需上报烧录事件，复用现有 MES 报文映射，`${report.flash_records}` 等字段可进入映射上下文。

## 7. 安全与生产注意

1. 固件仓纳入数据目录权限控制和备份范围，防止固件被替换；`find_firmware()` 的 SHA-256 校验是入烧前的最后防线。
2. 烧录是写操作：确认型号匹配（`product_model`）、供电稳定和断电恢复策略，避免把产品刷成砖。
3. 解锁和整片擦除会清除设备数据，必须确认业务允许，并在烧录记录中保留原始状态。
4. 外部工具后端务必配置 `timeout_ms`，防止厂商工具挂死阻塞工位。
5. 烧录失败的产品不要直接重测同一 SN，先按第 6 节的记录定位是文件、连接还是目标芯片问题。
6. 与测试流程一样，烧录脚本和固件版本应由管理员维护，操作工只执行。
