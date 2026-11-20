export type NodeType = "test" | "condition" | "parallel" | "loop" | "subflow";
export type Path = Array<string | number>;

export interface Action {
  type: string;
  [key: string]: unknown;
}

export interface Assertion {
  actual: unknown;
  operator: string;
  expected?: unknown;
}

export interface FlowNode {
  id: string;
  name: string;
  type: NodeType;
  on_fail: "stop" | "continue";
  protocol?: string;
  timeout_ms?: number;
  retries?: number;
  actions?: Action[];
  condition?: Assertion;
  then?: FlowNode[];
  else?: FlowNode[];
  branches?: Branch[];
  max_workers?: number;
  mode?: "count" | "while";
  count?: number;
  max_iterations?: number;
  break_on_fail?: boolean;
  steps?: FlowNode[];
  subflow?: string;
  inputs?: Record<string, unknown>;
}

export interface Branch {
  id: string;
  name: string;
  on_fail?: "stop" | "continue";
  steps: FlowNode[];
}

export interface SubflowDefinition {
  name: string;
  steps: FlowNode[];
}

export interface FlowDefinition {
  schema_version: number;
  name: string;
  product_model: string;
  variables: Record<string, unknown>;
  steps: FlowNode[];
  subflows: Record<string, SubflowDefinition>;
}

export const nodeLabels: Record<NodeType, string> = {
  test: "测试项",
  condition: "条件判断",
  parallel: "并行执行",
  loop: "循环",
  subflow: "调用子任务",
};

export const actionLabels: Record<string, string> = {
  request: "发送并等待响应",
  send: "仅发送",
  receive: "等待接收",
  execute: "设备操作",
  set: "设置变量",
  assert: "结果断言",
  sleep: "延时等待",
};

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
