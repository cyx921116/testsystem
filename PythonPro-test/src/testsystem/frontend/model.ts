/**
 * 图形流程编辑器的数据模型层：节点/动作的工厂函数与不可变路径更新工具。
 *
 * 所有编辑操作返回新对象（setAt 沿路径逐层浅拷贝），配合 React 状态更新触发重渲染；
 * Path 为从流程定义根到目标值的下标/键序列（如 ["steps", 0, "then", 1]）。
 */
import type { Action, FlowDefinition, FlowNode, NodeType, Path } from "./types";

/** 深拷贝（JSON 往返，流程定义均为纯数据）。 */
export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 生成节点/分支 ID：前缀 + 时间戳 + 随机段，保证同页多次创建不冲突。 */
export function createId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

/** 各动作类型的默认参数（与 app.js 的 defaultAction 保持一致）。 */
export function createAction(type: string): Action {
  const defaults: Record<string, Action> = {
    request: { type, data: "AT+SELFTEST?", save_as: "response" },
    send: { type, data: "AT+START\r\n" },
    receive: { type, save_as: "response", timeout_ms: 1000, size: 4096 },
    execute: { type, operation: "read_holding_registers", params: { address: 0, count: 1 }, save_as: "result" },
    set: { type, variable: "expected_value", value: "PASS" },
    assert: { type, actual: "${response}", operator: "equals", expected: "PASS" },
    sleep: { type, duration_ms: 500 },
    flash: { type, firmware: "", target: "", save_as: "flash_result", options: { erase: true, verify: true, reset: true, timeout_ms: 60000 } },
  };
  return clone(defaults[type] || defaults.request);
}

/** 按节点类型创建带合理默认内容的节点（拖入画布时调用）。 */
export function createNode(type: NodeType): FlowNode {
  const base = { id: createId(type), name: `新${type === "test" ? "测试项" : "控制节点"}`, type, on_fail: "stop" as const };
  if (type === "test") {
    return { ...base, name: "新测试项", protocol: "simulator", timeout_ms: 5000, retries: 0, actions: [createAction("request"), createAction("assert")] };
  }
  if (type === "condition") {
    return { ...base, name: "条件判断", condition: { actual: "${response}", operator: "equals", expected: "PASS" }, then: [createNode("test")], else: [] };
  }
  if (type === "parallel") {
    return {
      ...base,
      name: "并行执行",
      max_workers: 2,
      branches: [
        { id: createId("branch_a"), name: "分支 A", steps: [createNode("test")] },
        { id: createId("branch_b"), name: "分支 B", steps: [createNode("test")] },
      ],
    } as FlowNode;
  }
  if (type === "loop") {
    return { ...base, name: "计数循环", mode: "count", count: 2, max_iterations: 20, break_on_fail: true, steps: [createNode("test")] };
  }
  return { ...base, name: "调用子流程", subflow: "self_check", inputs: {} };
}

/** 空白 v2 控制流流程（新建流程时的初始定义）。 */
export function emptyDefinition(): FlowDefinition {
  return {
    schema_version: 2,
    name: "新产品控制流程",
    product_model: "NEW-MODEL",
    station_code: "",
    variables: {},
    steps: [createNode("test")],
    subflows: {},
  };
}

/** 规范化外部传入的定义：补 schema_version/variables/subflows 与节点默认字段。 */
export function normalizeDefinition(input: FlowDefinition): FlowDefinition {
  const next = clone(input || emptyDefinition());
  next.schema_version = 2;
  next.variables ||= {};
  next.subflows ||= {};
  next.steps = (next.steps || []).map((node) => ({ ...node, on_fail: node.on_fail || "stop", type: node.type || "test" }));
  return next;
}

/** 按路径取值。 */
export function getAt<T>(root: unknown, path: Path): T {
  let value: unknown = root;
  for (const key of path) value = (value as Record<string | number, unknown>)[key];
  return value as T;
}

/** 按路径不可变更新：沿路径逐层浅拷贝，叶子用 updater 计算新值。 */
export function setAt(root: unknown, path: Path, updater: (value: unknown) => unknown): unknown {
  if (!path.length) return updater(root);
  const [key, ...rest] = path;
  const source = root as Record<string | number, unknown>;
  const next = (Array.isArray(source) ? [...source] : { ...source }) as Record<string | number, unknown>;
  next[key] = setAt(source[key], rest, updater);
  return next;
}

/** 更新指定路径的节点（updater 收到节点副本，返回值整体替换）。 */
export function updateNode(definition: FlowDefinition, path: Path, updater: (node: FlowNode) => FlowNode): FlowDefinition {
  return setAt(definition, path, (node) => updater(clone(node as FlowNode))) as FlowDefinition;
}

/** 在指定序列的 index 处插入节点。 */
export function insertNode(definition: FlowDefinition, sequencePath: Path, index: number, node: FlowNode): FlowDefinition {
  return setAt(definition, sequencePath, (value) => {
    const sequence = [...(value as FlowNode[])];
    sequence.splice(index, 0, node);
    return sequence;
  }) as FlowDefinition;
}

/** 删除路径末端节点（路径最后一段为序列下标）。 */
export function removeNode(definition: FlowDefinition, path: Path): FlowDefinition {
  const sequencePath = path.slice(0, -1);
  const index = Number(path[path.length - 1]);
  return setAt(definition, sequencePath, (value) => {
    const sequence = [...(value as FlowNode[])];
    sequence.splice(index, 1);
    return sequence;
  }) as FlowDefinition;
}

/** 按新顺序重排序列（拖拽排序后回写）。 */
export function reorderSequence(definition: FlowDefinition, sequencePath: Path, order: number[]): FlowDefinition {
  return setAt(definition, sequencePath, (value) => {
    const sequence = value as FlowNode[];
    return order.map((index) => sequence[index]);
  }) as FlowDefinition;
}

/** 路径 → 字符串键（React key / 选中态标识）。 */
export function pathKey(path: Path): string {
  return path.join(".");
}

/** 路径 → DOM id。 */
export function pathId(path: Path): string {
  return `node-${path.join("-")}`;
}

/** 当前编辑的流程（主流程或某个子流程）的 steps 路径。 */
export function currentStepsPath(currentFlow: string): Path {
  return currentFlow === "main" ? ["steps"] : ["subflows", currentFlow, "steps"];
}

/** 查找对某个子流程的全部引用位置（删除子流程前提示影响面）。 */
export function findSubflowReferences(definition: FlowDefinition, target: string): string[] {
  const references: string[] = [];
  function visit(nodes: FlowNode[], owner: string) {
    for (const node of nodes || []) {
      if (node.type === "subflow" && node.subflow === target) references.push(`${owner}/${node.id}`);
      if (node.type === "condition") {
        visit(node.then || [], `${owner}/${node.id}.then`);
        visit(node.else || [], `${owner}/${node.id}.else`);
      } else if (node.type === "parallel") {
        (node.branches || []).forEach((branch) => visit(branch.steps, `${owner}/${node.id}.${branch.id}`));
      } else if (node.type === "loop") {
        visit(node.steps || [], `${owner}/${node.id}`);
      }
    }
  }
  visit(definition.steps, "main");
  for (const [id, subflow] of Object.entries(definition.subflows)) visit(subflow.steps, id);
  return references;
}
