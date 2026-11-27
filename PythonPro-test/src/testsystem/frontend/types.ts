/**
 * 图形流程编辑器的类型定义：与后端 v2 控制流流程 JSON（control_compiler.py）一一对应。
 * 流程定义最终会编译为同一份 JSON 提交后端，字段名保持 snake_case 与后端一致。
 */

/** 控制流节点类型：测试项 / 条件 / 并行 / 循环 / 子流程调用。 */
export type NodeType = "test" | "condition" | "parallel" | "loop" | "subflow";
/** 路径：从流程定义根到目标值的键/下标序列（不可变更新用）。 */
export type Path = Array<string | number>;

/** 动作（request/send/receive/execute/set/assert/sleep/flash），字段随类型而异。 */
export interface Action {
  type: string;
  [key: string]: unknown;
}

/** 断言/条件表达式：实际值 + 操作符 + 期望值（truthy/exists 无期望值）。 */
export interface Assertion {
  actual: unknown;
  operator: string;
  expected?: unknown;
}

/** 控制流节点：公共字段 + 各类型专属字段（按 type 取用）。 */
export interface FlowNode {
  id: string;
  name: string;
  type: NodeType;
  on_fail: "stop" | "continue";
  // test 节点
  protocol?: string;
  timeout_ms?: number;
  retries?: number;
  actions?: Action[];
  // condition 节点
  condition?: Assertion;
  then?: FlowNode[];
  else?: FlowNode[];
  // parallel 节点
  branches?: Branch[];
  max_workers?: number;
  // loop 节点
  mode?: "count" | "while";
  count?: number;
  max_iterations?: number;
  break_on_fail?: boolean;
  steps?: FlowNode[];
  // subflow 节点
  subflow?: string;
  inputs?: Record<string, unknown>;
}

/** 并行分支：独立 ID 与节点序列。 */
export interface Branch {
  id: string;
  name: string;
  on_fail?: "stop" | "continue";
  steps: FlowNode[];
}

/** 子流程定义（脚本级 subflows 字典的值）。 */
export interface SubflowDefinition {
  name: string;
  steps: FlowNode[];
}

/** 流程定义根（v2 控制流）：与后端编译器输出结构一致。 */
export interface FlowDefinition {
  schema_version: number;
  name: string;
  product_model: string;
  station_code?: string;
  variables: Record<string, unknown>;
  steps: FlowNode[];
  subflows: Record<string, SubflowDefinition>;
}

/** 固件仓条目（flash 动作选择固件时的候选信息）。 */
export interface FirmwareInfo {
  name: string;
  version?: string;
  product_model?: string;
  description?: string;
}

/** 节点类型 → 中文名（节点库与画布标题）。 */
export const nodeLabels: Record<NodeType, string> = {
  test: "测试项",
  condition: "条件判断",
  parallel: "并行执行",
  loop: "循环",
  subflow: "调用子任务",
};

/** 动作类型 → 中文名。 */
export const actionLabels: Record<string, string> = {
  request: "发送并等待响应",
  send: "仅发送",
  receive: "等待接收",
  execute: "设备操作",
  set: "设置变量",
  assert: "结果断言",
  sleep: "延时等待",
  flash: "固件烧录",
};

/** 断言操作符 → 中文名。 */
export const operatorLabels: Record<string, string> = {
  equals: "等于",
  not_equals: "不等于",
  contains: "包含",
  not_contains: "不包含",
  regex: "正则匹配",
  gt: "大于",
  gte: "大于等于",
  lt: "小于",
  lte: "小于等于",
  between: "数值区间",
  truthy: "为真",
  exists: "存在且非空",
};
