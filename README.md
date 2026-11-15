# Pilot 生产测试系统

一个可直接运行的生产测试工作站，包含产品 SN 追溯、低代码测试流程、逐项 PASS/FAIL 展示、唯一测试报告、可配置 MES 上传，以及多协议产品通讯。

## 文档

- [详细项目开发说明](docs/PROJECT_GUIDE.md)：架构、目录、数据库、全部后端函数、前端函数、脚本、协议、MES、API 和扩展方法。
- [Git 与 GitHub 配置指南](docs/GIT_GITHUB.md)：Windows 环境、提交身份、登录授权、创建仓库、推送和常见问题。

## 启动

要求 Python 3.12+，项目使用 `uv` 管理依赖。

```bash
uv sync
uv run testsystem
```

打开 <http://127.0.0.1:8000>。API 文档位于 <http://127.0.0.1:8000/docs>。

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

1. 在“生产测试”选择产品型号对应的流程。
2. 用扫码枪扫描 SN，扫码枪结束符设为 Enter；也可手工输入。
3. 系统检查 SN 格式、工位占用情况及报告唯一性，然后点击“开始测试”或按 F2。
4. 页面持续刷新每个测试项的实际值、期望、耗时和 PASS/FAIL。
5. 测试结束后生成唯一报告。同一 SN 已有报告时禁止重新测试，数据库通过 `reports.sn UNIQUE` 强制保证一对一。
6. 在“报告中心”按 SN 查询、查看断言明细、打印、下载 JSON 或重试 MES 上传。

服务异常重启时，未完成的运行会标记为中断失败，不生成不完整报告，因此该 SN 可以重新测试。已生成的报告及脚本快照不会被后续脚本编辑改变。

## 低代码流程

“流程编排”支持：

- 添加、删除、上下移动测试项；
- 每个测试项独立选择通讯协议、超时、失败重试次数和失败后的停止/继续策略；
- 组合发送并等待响应、仅发送、接收、设备操作、变量赋值、断言、延时动作；
- 使用 JSON 高级模式批量编辑；
- 保存前由后端编译校验，保存后版本自动递增。

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
    {"type": "request", "data": "AT+VOLTAGE?\r\n", "encoding": "text", "save_as": "voltage"},
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
- 默认无登录鉴权，适合隔离的产线内网单机部署。跨终端或暴露到非可信网络前，应在反向代理增加 TLS、认证和访问控制。
- MES 密码、MQTT 密码等保存在本地配置数据库中，请限制数据目录访问权限并纳入受控备份。
