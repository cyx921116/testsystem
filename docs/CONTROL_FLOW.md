# 控制流与 React Flow 开发说明

## 1. 为什么使用 v2 控制流

v1 是线性测试项数组，只适合按顺序执行。v2 在不改变 SN、报告、协议和 MES 体系的前提下，增加：

- `condition`：条件判断和 TRUE/FALSE 分支；
- `parallel`：多分支并行执行；
- `loop`：固定次数和 while 循环；
- `subflow`：可复用子流程；
- `test`：原有测试动作，作为控制流叶子节点。

v2 使用递归结构，而不是允许用户任意画边。这样执行语义明确，能够检查非法引用、循环调用、嵌套深度和迭代上限。

已有 `schema_version: 1` 脚本继续走原线性执行器，不需要迁移。React 编辑器修改老脚本时会规范化为 v2。

## 2. 顶层结构

```json
{
  "schema_version": 2,
  "name": "复杂产品测试",
  "product_model": "PRODUCT-200",
  "variables": {"expected_version": "1.0.0"},
  "subflows": {},
  "steps": []
}
```

- `variables`：主流程初始变量。
- `steps`：主流程节点数组。
- `subflows`：子流程 ID 到定义的映射。
- 所有节点具有唯一 `id`、`name` 和 `on_fail`。
- 编译器最多接受 500 个节点、6 层结构嵌套、6 层子流程调用。

## 3. 节点类型

## 3.1 测试节点 `test`

```json
{
  "type": "test",
  "id": "voltage",
  "name": "电压检查",
  "protocol": "simulator",
  "timeout_ms": 5000,
  "retries": 1,
  "on_fail": "stop",
  "actions": [
    {"type": "request", "data": "AT+VOLTAGE?", "save_as": "voltage"},
    {"type": "assert", "actual": "${voltage}", "operator": "between", "expected": [11.5, 12.5]}
  ]
}
```

动作类型和断言操作符与 v1 完全一致（含 `flash` 固件烧录动作，见 [固件烧录框架开发说明](FIRMWARE_FLASH.md)）。`retries` 是失败后的额外重试次数，报告中的 `attempts` 包含第一次执行。

## 3.2 条件节点 `condition`

```json
{
  "type": "condition",
  "id": "version_gate",
  "name": "版本判断",
  "condition": {"actual": "${version}", "operator": "equals", "expected": "1.0.0"},
  "then": [],
  "else": [],
  "on_fail": "stop"
}
```

- 条件使用与测试断言相同的 `_assert()`。
- TRUE 执行 `then`，FALSE 执行 `else`。
- 条件为 FALSE 是正常控制结果，不是测试失败。
- 条件数据无法比较时，该控制事件为 FAIL。
- 控制事件记录实际值、期望值、选中分支和分支执行结果。

## 3.3 并行节点 `parallel`

```json
{
  "type": "parallel",
  "id": "parallel_checks",
  "name": "并行检查",
  "max_workers": 2,
  "branches": [
    {"id": "voltage_branch", "name": "电压分支", "steps": []},
    {"id": "firmware_branch", "name": "固件分支", "steps": []}
  ],
  "on_fail": "stop"
}
```

执行规则：

- 支持 2~8 个分支。
- `max_workers` 限制本节点的线程数。
- 所有分支都会被等待，不强制终止慢分支。
- 每个分支使用深拷贝的变量上下文。
- 每个分支使用独立的协议实例，结束后独立关闭。
- 任一分支失败，则并行节点失败；父级的 `on_fail` 决定是否继续后续节点。

分支输出不会直接覆盖主流程变量，而是按分支 ID 隔离：

```text
${branches.voltage_branch.voltage}
${branches.firmware_branch.firmware}
```

**硬件安全**：并行不代表同一硬件可以并发访问。两个分支如果同时打开同一个 COM 口、CAN 通道或 J-Link，仍可能冲突。并行应优先用于相互独立的设备、网络服务或可以安全并发的仪器。SQLite 每个线程使用独立连接并设置 busy timeout。

## 3.4 循环节点 `loop`

固定次数：

```json
{
  "type": "loop",
  "id": "repeat_test",
  "name": "重复自检",
  "mode": "count",
  "count": 3,
  "max_iterations": 20,
  "break_on_fail": true,
  "steps": [],
  "on_fail": "stop"
}
```

While：

```json
{
  "type": "loop",
  "id": "while_test",
  "name": "条件循环",
  "mode": "while",
  "condition": {"actual": "${iteration}", "operator": "lte", "expected": 3},
  "max_iterations": 10,
  "break_on_fail": true,
  "steps": [],
  "on_fail": "stop"
}
```

- `count` 执行指定次数。
- `while` 在每次迭代前检查条件。
- `${iteration}` 从 1 开始，`${iteration0}` 从 0 开始。
- 最大迭代次数为 100，防止死循环。
- `break_on_fail=true` 时，某次迭代失败会停止循环。
- 设为 false 时会继续剩余迭代，但最终循环节点仍为 FAIL。
- 离开循环后恢复外层迭代变量，后续普通测试不会被误标为某次迭代。

## 3.5 子流程节点 `subflow`

定义：

```json
{
  "subflows": {
    "identity": {
      "name": "身份检查子流程",
      "steps": []
    }
  }
}
```

调用：

```json
{
  "type": "subflow",
  "id": "call_identity",
  "name": "调用身份检查",
  "subflow": "identity",
  "inputs": {"expected_sn": "${sn}"},
  "on_fail": "stop"
}
```

- 子流程可以在主流程或其他子流程中复用。
- 输入在父上下文中渲染，然后写入子上下文。
- 子上下文是深拷贝，变量修改不会直接污染父流程。
- 子流程协议连接与父顺序流程复用；并行分支则各自独立。
- 编译器拒绝不存在的引用、循环调用和超过 6 层的调用。

输出通过调用节点 ID 访问：

```text
${subflows.call_identity.device_sn}
```

## 4. 失败作用域

每个序列都在当前层级处理 `on_fail`：

- `stop`：停止当前序列后续节点。
- `continue`：记录失败并继续当前序列。
- 并行分支的失败在分支内部处理后，再汇总到并行节点。
- 子流程失败后，由调用节点的 `on_fail` 决定主流程是否继续。
- 循环是否继续剩余迭代由 `break_on_fail` 决定。
- 测试项全部通过但控制事件 FAIL、RUNNING、INTERRUPTED 或运行存在系统错误时，最终报告仍为 FAIL。

未选中的条件分支、未发生的循环迭代和停止后未执行的节点不会伪造测试记录。

## 5. 报告结构变化

### 5.1 `test_items` 新字段

- `item_id`：一次实际执行的唯一 occurrence ID。
- `node_id`：脚本中的逻辑节点 ID。
- `path`：完整执行路径，包含分支、子流程和迭代。
- `iteration`：当前迭代次数，非循环项为 NULL。

示例：

```text
root/version_gate.then/count_loop[2]/count_test
root/parallel_checks.voltage_branch/voltage_test
root/call_identity<identity>/identity_test
```

因此同一个测试节点循环执行多次时，每项都独立保存，不会覆盖。

### 5.2 `control_events` 表

记录控制节点，而不是把条件 FALSE 当成测试 FAIL：

- `node_id`、`node_name`、`node_type`；
- `path`；
- `status`：RUNNING、TRUE、FALSE、PASS、FAIL、INTERRUPTED；
- `details_json`：条件结果、并行分支、迭代明细、子流程输入等；
- `started_at`、`finished_at`。

运行 API 的 `controls` 和报告 JSON 的 `controls` 都来自该表。MES 完整报告也会包含这些字段。

### 5.3 数据库迁移

启动时自动：

- 给旧 `test_items` 添加 `node_id`、`path`、`iteration`；
- 回填旧记录的 node_id/path；
- 创建 `control_events`；
- 保留旧 v1 脚本、运行和报告。

## 6. React Flow 前端

### 6.1 混合架构

只有复杂流程编辑器使用 React：

- React 19；
- TypeScript；
- Vite；
- `@xyflow/react`（React Flow）；
- `@dagrejs/dagre` 自动布局。

生产测试、协议、MES 和报告页面继续使用原生 JavaScript。Node.js 只用于开发构建，正常运行不需要 Node.js。

### 6.2 专用编辑面板

- 条件节点分别显示 TRUE/FALSE 分支，可添加测试、条件、并行、循环或子任务。
- 循环节点提供固定次数/while、最大迭代次数、失败中断开关和循环体节点添加器。
- 并行节点可编辑分支名称和 ID、调整展示顺序、增删分支，并在每个分支内嵌套任意节点。
- 子任务调用节点可直接跳转到被引用子任务；左侧可新建或删除子任务，删除前检查所有嵌套引用。

### 6.3 源文件

```text
src/testsystem/frontend/
├── FlowEditor.tsx       React 组件、画布、拖放、检查器、全局挂载
├── model.ts             AST 创建、规范化、路径读写、插入、删除、重排
├── types.ts             v2 类型、节点/动作/操作符标签
└── flow-editor.css      React Flow 和检查器样式
```

构建产物：

```text
src/testsystem/static/
├── flow-editor.js
└── flow-editor.css
```

产物随源码提交并打入 wheel，产线无需执行 npm。

### 6.4 `FlowEditor.tsx` 主要函数

- `PilotNode`：显示五类节点、动作摘要、分支和失败策略。
- `buildGraph`：把递归 AST 转为 React Flow 节点和边。
- `sequence`：构建一个顺序序列并连接前后节点。
- `expand`：展开条件、并行和循环内部结构。
- `FlowEditorApp`：维护当前定义、子流程、选中路径和节点位置。
- `addNode` / `addFromPalette`：从节点库插入节点。
- `onNodeDragStop`：按拖动后的纵向顺序重排同层节点。
- `onDrop`：把新节点放到画布或目标控制节点。
- `addSubflow`：创建并切换到可复用子流程。
- `Inspector`：编辑五类节点及失败策略。
- `NestedNodeAdder`：在条件分支、循环体或并行分支内选择并添加任意控制节点。
- `editSubflow`：从调用节点直接跳转到对应子任务。
- `deleteCurrentSubflow`：删除当前子任务前检查所有嵌套引用，仍被引用时阻止删除。
- `ActionFields`：编辑七类测试动作；发送动作使用十进制数据或文本指令，编码由协议配置接管，Modbus/J-Link 常用参数使用具名十进制表单。
- `AssertionFields`：编辑条件和断言，区间上下限与比较值使用十进制输入。
- `ActionAdder` / 动作排序 / 测试项复制：无需编辑 JSON 即可组合和调整执行步骤。
- `JsonField`：编辑操作参数和子流程输入，保留临时非法 JSON 的输入状态。
- `mount`：向现有页面挂载 React 编辑器。
- `setDefinition`：从旧页面同步新草稿，避免异步初始化丢失数据。

### 6.5 `model.ts` 主要函数

- `createNode`：创建五类节点的合法默认结构。
- `createAction`：创建七类动作。
- `emptyDefinition` / `normalizeDefinition`：创建和规范化 v2。
- `getAt` / `setAt`：按路径读取和不可变更新嵌套对象。
- `updateNode`：更新指定节点。
- `insertNode` / `removeNode`：插入和删除节点。
- `reorderSequence`：按节点原下标重排序列。
- `currentStepsPath`：计算主流程或当前子流程的 steps 路径。
- `findSubflowReferences`：递归查找主流程、条件、并行、循环和其他子任务中的引用，防止误删。
- `pathKey` / `pathId`：生成选择键和 React Flow 节点 ID。

### 6.6 与原页面同步

React 不直接调用保存 API。它通过 `onChange` 把完整 v2 definition 传回原页面的 `state.draft`，原页面继续负责：

- 编译校验；
- 创建和更新脚本；
- 版本递增；
- 生产测试选择；
- 报告和 MES。

原页面打开脚本、应用 JSON 或切换列表时，通过 `PilotFlowEditor.setDefinition()` 同步回 React。同步过程不会再次触发 onChange 循环。

## 7. 后端源文件

### `control_compiler.py`

- `compile_control_script`：v2 入口。
- `_sequence`：递归校验五类节点。
- `_condition`：复用动作断言校验。
- `_register_id`：检查 ID、名称和失败策略。
- `_subflow_graph` / `_check_subflow_calls`：检查缺失引用、循环调用和调用深度。
- `_contains_test`：确保脚本至少包含一个测试节点。

`compiler.compile_script()` 检测 `schema_version == 2` 后转到该编译器；其他版本继续走 v1。

### `control_flow.py`

`ControlFlowRunner` 是 v2 递归执行器：

- `execute`：建立主上下文并执行主序列。
- `_sequence`：执行当前层级并处理失败作用域。
- `_execute_node`：按节点类型分派。
- `_execute_test`：执行测试、重试并保存唯一 occurrence。
- `_execute_condition`：计算条件并执行选中分支。
- `_execute_parallel`：用独立线程池执行分支并汇总上下文。
- `_execute_loop`：处理 count/while、迭代变量和循环上限。
- `_execute_subflow`：隔离输入输出并检查调用深度。
- `_evaluate`：渲染并执行条件断言。
- `_start_control` / `_finish_control`：保存控制事件。

原 `TestExecutor` 仍负责运行生命周期、v1 执行、协议关闭、报告定稿和自动 MES 上传。

## 8. 构建和运行

正常运行：

```bash
uv sync
uv run testsystem
```

修改 React 源码后：

```bash
npm ci
npm run build
```

单独执行 TypeScript 检查：

```bash
npm run typecheck
```

Python 打包：

```bash
uv build
```

`package-lock.json` 和 `uv.lock` 都应提交，保证前后端依赖可复现。`node_modules` 已被 `.gitignore` 排除。

## 9. 当前边界

- 画布结构由 AST 决定，不允许用户任意连接端口；这是为了防止产生无法确定语义的执行图。
- 并行分支会全部等待，驱动自身永久阻塞时不能安全强杀。
- 当前没有子流程版本独立发布机制，子流程跟随所属脚本版本一起保存。
- 并行硬件是否安全取决于设备和驱动，配置人员必须确认端口和通道不冲突。
- React 构建文件较大，但只在内网加载一次；正常部署不依赖 npm 或外部 CDN。
