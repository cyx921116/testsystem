import type { Action, FlowDefinition, FlowNode, NodeType, Path } from "./types";

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export function createId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}

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

export function normalizeDefinition(input: FlowDefinition): FlowDefinition {
  const next = clone(input || emptyDefinition());
  next.schema_version = 2;
  next.variables ||= {};
  next.subflows ||= {};
  next.steps = (next.steps || []).map((node) => ({ ...node, on_fail: node.on_fail || "stop", type: node.type || "test" }));
  return next;
}

export function getAt<T>(root: unknown, path: Path): T {
  let value: unknown = root;
  for (const key of path) value = (value as Record<string | number, unknown>)[key];
  return value as T;
}

export function setAt(root: unknown, path: Path, updater: (value: unknown) => unknown): unknown {
  if (!path.length) return updater(root);
  const [key, ...rest] = path;
  const source = root as Record<string | number, unknown>;
  const next = (Array.isArray(source) ? [...source] : { ...source }) as Record<string | number, unknown>;
  next[key] = setAt(source[key], rest, updater);
  return next;
}

export function updateNode(definition: FlowDefinition, path: Path, updater: (node: FlowNode) => FlowNode): FlowDefinition {
  return setAt(definition, path, (node) => updater(clone(node as FlowNode))) as FlowDefinition;
}

export function insertNode(definition: FlowDefinition, sequencePath: Path, index: number, node: FlowNode): FlowDefinition {
  return setAt(definition, sequencePath, (value) => {
    const sequence = [...(value as FlowNode[])];
    sequence.splice(index, 0, node);
    return sequence;
  }) as FlowDefinition;
}

export function removeNode(definition: FlowDefinition, path: Path): FlowDefinition {
  const sequencePath = path.slice(0, -1);
  const index = Number(path[path.length - 1]);
  return setAt(definition, sequencePath, (value) => {
    const sequence = [...(value as FlowNode[])];
    sequence.splice(index, 1);
    return sequence;
  }) as FlowDefinition;
}

export function reorderSequence(definition: FlowDefinition, sequencePath: Path, order: number[]): FlowDefinition {
  return setAt(definition, sequencePath, (value) => {
    const sequence = value as FlowNode[];
    return order.map((index) => sequence[index]);
  }) as FlowDefinition;
}

export function pathKey(path: Path): string {
  return path.join(".");
}

export function pathId(path: Path): string {
  return `node-${path.join("-")}`;
}

export function currentStepsPath(currentFlow: string): Path {
  return currentFlow === "main" ? ["steps"] : ["subflows", currentFlow, "steps"];
}

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
