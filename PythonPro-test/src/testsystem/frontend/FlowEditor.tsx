/**
 * 图形流程编辑器（React + @xyflow/react + dagre 自动布局）。
 *
 * 这是 flow-editor.js（static 下的构建产物）的源码，构建命令：npx vite build。
 * 通过 mount()/setDefinition()/setFirmware() 与 app.js 交互：
 * app.js 把流程草稿传入画布，画布每次编辑经 onChange 回写同一份草稿，
 * 与步骤列表/高级 JSON 编辑器保持同步（最终都编译为同一份流程 JSON）。
 *
 * 布局：左栏节点库（拖入/点击添加）+ 中间只读连线画布（拖动排序）+ 右栏节点检查器。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent, MouseEvent as ReactMouseEvent } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  applyNodeChanges,
} from "@xyflow/react";
import type { Edge, Node, NodeProps, OnNodeDrag, ReactFlowInstance } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./flow-editor.css";
import {
  clone,
  createAction,
  createId,
  createNode,
  currentStepsPath,
  findSubflowReferences,
  getAt,
  insertNode,
  normalizeDefinition,
  pathId,
  pathKey,
  removeNode,
  reorderSequence,
  updateNode,
} from "./model";
import type { Action, Assertion, FirmwareInfo, FlowDefinition, FlowNode, NodeType, Path } from "./types";
import { actionLabels, nodeLabels, operatorLabels } from "./types";

/** 画布节点携带的数据：流程节点（含 AST 与路径）或并行汇合点。 */
interface PilotNodeData extends Record<string, unknown> {
  kind: "flow" | "join";
  ast?: FlowNode;
  path?: Path;
  sequencePath?: Path;
  index?: number;
}

type PilotRfNode = Node<PilotNodeData>;

interface GraphResult {
  nodes: PilotRfNode[];
  edges: Edge[];
}

/** 动作的一行摘要（画布节点内显示前 3 个动作）。 */
function actionSummary(action: Action): string {
  if (["request", "send"].includes(action.type)) return String(action.data ?? "");
  if (action.type === "assert") return `${String(action.actual ?? "")} ${String(action.operator)} ${String(action.expected ?? "")}`;
  if (action.type === "execute") return String(action.operation ?? "");
  if (action.type === "set") return `${String(action.variable)} = ${String(action.value)}`;
  if (action.type === "sleep") return `${String(action.duration_ms)} ms`;
  if (action.type === "flash") return `烧录 ${String(action.firmware || "未选择固件")}`;
  return String(action.save_as ?? action.type);
}

/** 自定义画布节点：按节点类型渲染摘要信息（测试项动作、条件分支计数、并行分支等）。 */
function PilotNode({ data, selected }: NodeProps) {
  const nodeData = data as PilotNodeData;
  if (nodeData.kind === "join") {
    return <div className="pilot-join"><Handle type="target" position={Position.Top} />JOIN<Handle type="source" position={Position.Bottom} /></div>;
  }
  const ast = nodeData.ast!;
  return (
    <div className={`pilot-rf-node pilot-rf-${ast.type} ${selected ? "selected" : ""}`}>
      <Handle type="target" position={Position.Top} />
      <div className="pilot-rf-heading"><span>{nodeLabels[ast.type]}</span><strong>{ast.name}</strong><small>{ast.id}</small></div>
      {ast.type === "test" && <div className="pilot-rf-body"><span>{ast.protocol} · {ast.timeout_ms} ms · 重试 {ast.retries}</span><div className="pilot-rf-actions">{(ast.actions || []).slice(0, 3).map((action, index) => <code key={index}>{index + 1}. {actionLabels[action.type]} · {actionSummary(action)}</code>)}{(ast.actions || []).length > 3 && <small>另 {ast.actions!.length - 3} 个动作</small>}</div></div>}
      {ast.type === "condition" && <div className="pilot-rf-body"><code>{String(ast.condition?.actual)} {ast.condition?.operator} {String(ast.condition?.expected)}</code><div className="branch-labels"><span>TRUE {ast.then?.length || 0}</span><span>FALSE {ast.else?.length || 0}</span></div></div>}
      {ast.type === "parallel" && <div className="pilot-rf-body"><span>{ast.branches?.length || 0} 路并行 · 最多 {ast.max_workers} 个工作线程</span><div className="branch-labels">{ast.branches?.map((branch) => <span key={branch.id}>{branch.name}</span>)}</div></div>}
      {ast.type === "loop" && <div className="pilot-rf-body"><span>{ast.mode === "count" ? `COUNT ${ast.count} 次` : `WHILE ${String(ast.condition?.actual)} ${ast.condition?.operator} ${String(ast.condition?.expected)}`}</span><small>上限 {ast.max_iterations} · {ast.break_on_fail ? "失败中断" : "失败继续"}</small></div>}
      {ast.type === "subflow" && <div className="pilot-rf-body"><code>CALL {ast.subflow}</code><small>{Object.keys(ast.inputs || {}).length} 个输入变量</small></div>}
      <div className={`pilot-rf-fail ${ast.on_fail}`}>FAIL {ast.on_fail === "stop" ? "停止后续" : "继续"}</div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { pilot: PilotNode };
/** 节点库中各类型的用途说明。 */
const nodeDescriptions: Record<NodeType, string> = {
  test: "通讯、采集与结果判定",
  condition: "按结果选择执行分支",
  parallel: "同时执行多个独立任务",
  loop: "重复执行一组测试",
  subflow: "复用已有测试任务",
};

/**
 * 把流程定义转换为 React Flow 的节点与边：
 * 顺序节点纵向相连；条件节点引出 TRUE/FALSE 两条边；并行节点经各分支到汇合点（join）；
 * 循环体回边指向自身。最后交给 dagre 做层次自动布局（TB 方向）。
 */
function buildGraph(definition: FlowDefinition, currentFlow: string): GraphResult {
  const nodes: PilotRfNode[] = [];
  const edges: Edge[] = [];
  const dimensions = { width: 250, height: 140 };

  function connect(source: string, target: string, label?: string) {
    edges.push({ id: `edge-${edges.length}-${source}-${target}`, source, target, label, animated: false, type: "smoothstep" });
  }

  function sequence(sequenceNodes: FlowNode[], sequencePath: Path): { entry?: string; exits: string[] } {
    let entry: string | undefined;
    let previous: string[] = [];
    sequenceNodes.forEach((ast, index) => {
      const path = [...sequencePath, index];
      const id = pathId(path);
      nodes.push({ id, type: "pilot", position: { x: 0, y: 0 }, data: { kind: "flow", ast, path, sequencePath, index }, width: dimensions.width, height: dimensions.height });
      const built = expand(ast, path, id);
      if (!entry) entry = built.entry;
      previous.forEach((source) => connect(source, built.entry));
      previous = built.exits;
    });
    return { entry, exits: previous };
  }

  function expand(ast: FlowNode, path: Path, id: string): { entry: string; exits: string[] } {
    if (ast.type === "condition") {
      const thenFlow = sequence(ast.then || [], [...path, "then"]);
      const elseFlow = sequence(ast.else || [], [...path, "else"]);
      if (thenFlow.entry) connect(id, thenFlow.entry, "TRUE");
      if (elseFlow.entry) connect(id, elseFlow.entry, "FALSE");
      return { entry: id, exits: [...(thenFlow.entry ? thenFlow.exits : [id]), ...(elseFlow.entry ? elseFlow.exits : [id])] };
    }
    if (ast.type === "parallel") {
      const joinId = `${id}-join`;
      nodes.push({ id: joinId, type: "pilot", position: { x: 0, y: 0 }, data: { kind: "join" }, width: 80, height: 45 });
      (ast.branches || []).forEach((branch, branchIndex) => {
        const branchFlow = sequence(branch.steps, [...path, "branches", branchIndex, "steps"]);
        if (branchFlow.entry) connect(id, branchFlow.entry, branch.name);
        branchFlow.exits.forEach((exit) => connect(exit, joinId));
      });
      return { entry: id, exits: [joinId] };
    }
    if (ast.type === "loop") {
      const body = sequence(ast.steps || [], [...path, "steps"]);
      if (body.entry) connect(id, body.entry, ast.mode === "count" ? `× ${ast.count}` : "WHILE");
      body.exits.forEach((exit) => connect(exit, id, "next iteration"));
      return { entry: id, exits: [id] };
    }
    return { entry: id, exits: [id] };
  }

  sequence(getAt<FlowNode[]>(definition, currentStepsPath(currentFlow)), currentStepsPath(currentFlow));
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "TB", ranksep: 80, nodesep: 55, marginx: 35, marginy: 35 });
  graph.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((node) => graph.setNode(node.id, { width: node.width || dimensions.width, height: node.height || dimensions.height }));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);
  return {
    nodes: nodes.map((node) => {
      const position = graph.node(node.id);
      return { ...node, position: { x: position.x - (node.width || dimensions.width) / 2, y: position.y - (node.height || dimensions.height) / 2 } };
    }),
    edges,
  };
}

interface EditorProps {
  initialDefinition: FlowDefinition;
  onChange: (definition: FlowDefinition) => void;
}

// 供 app.js 调用的外部句柄（mount 前调用也能生效：先存 latest*，挂载后由副作用应用）
let externalSetDefinition: ((definition: FlowDefinition) => void) | null = null;
let externalSetFirmware: ((firmware: FirmwareInfo[]) => void) | null = null;
let latestDefinition: FlowDefinition | null = null;
let latestFirmware: FirmwareInfo[] = [];
let editorRoot: Root | null = null;

/** 编辑器主组件：画布状态、节点增删/排序、子流程切换与检查器联动。 */
function FlowEditorApp({ initialDefinition, onChange }: EditorProps) {
  const [definition, setDefinition] = useState(() => normalizeDefinition(latestDefinition || initialDefinition));
  const definitionRef = useRef(definition);
  const onChangeRef = useRef(onChange);
  const [currentFlow, setCurrentFlow] = useState("main");
  const [subflowError, setSubflowError] = useState("");
  const [selectedPath, setSelectedPath] = useState<Path | null>(null);
  const [firmware, setFirmwareList] = useState<FirmwareInfo[]>(latestFirmware);
  const layout = useMemo(() => buildGraph(definition, currentFlow), [definition, currentFlow]);
  const [nodes, setNodes] = useState<PilotRfNode[]>(layout.nodes);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const flowInstance = useRef<ReactFlowInstance<PilotRfNode, Edge> | null>(null);

  useEffect(() => {
    definitionRef.current = definition;
  }, [definition]);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);
  useEffect(() => setNodes(layout.nodes), [layout.nodes]);
  /** 画布从隐藏变为可见（切换页面）时自动 fitView，避免节点渲染在视口外。 */
  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    let width = 0;
    let height = 0;
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      const next = entry.contentRect;
      const becameVisible = next.width > 0 && next.height > 0 && (width === 0 || height === 0);
      width = next.width;
      height = next.height;
      if (becameVisible) {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => flowInstance.current?.fitView({ padding: 0.15, maxZoom: 1 }));
      }
    });
    observer.observe(element);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const element = canvasRef.current;
      if (element && element.clientWidth > 0 && element.clientHeight > 0) {
        flowInstance.current?.fitView({ padding: 0.15, maxZoom: 1 });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [currentFlow, layout.nodes.length]);
  useEffect(() => {
    externalSetDefinition = (next) => {
      const normalized = normalizeDefinition(next);
      definitionRef.current = normalized;
      setDefinition(normalized);
      setSelectedPath((current) => {
        if (current) {
          try {
            getAt(normalized, current);
            return current;
          } catch {
            return normalized.steps.length ? ["steps", 0] : null;
          }
        }
        return normalized.steps.length ? ["steps", 0] : null;
      });
    };
    return () => { externalSetDefinition = null; };
  }, []);
  useEffect(() => {
    externalSetFirmware = (next) => {
      latestFirmware = next;
      setFirmwareList(next);
    };
    return () => { externalSetFirmware = null; };
  }, []);

  /** 所有编辑的统一出口：规范化 → 更新本地状态 → 回写 app.js 草稿。 */
  const updateDefinition = useCallback((next: FlowDefinition) => {
    const normalized = normalizeDefinition(next);
    definitionRef.current = normalized;
    setDefinition(normalized);
    onChangeRef.current(normalized);
  }, []);

  /** 添加节点：默认追加到当前流程末尾；首个子流程节点会自动补一个示例子流程。 */
  const addNode = useCallback((type: NodeType, sequencePath?: Path, index?: number) => {
    let next = definitionRef.current;
    if (type === "subflow" && !Object.keys(next.subflows).length) {
      next = clone(next);
      next.subflows.self_check = { name: "产品自检子流程", steps: [createNode("test")] };
    }
    const path = sequencePath || currentStepsPath(currentFlow);
    const sequence = getAt<FlowNode[]>(next, path);
    const target = index ?? sequence.length;
    const node = createNode(type);
    if (type === "subflow") node.subflow = Object.keys(next.subflows)[0];
    next = insertNode(next, path, target, node);
    updateDefinition(next);
    setSelectedPath([...path, target]);
  }, [currentFlow, updateDefinition]);

  /** 节点库点击添加：落在选中节点的内部（条件/循环/并行）或其后。 */
  const addFromPalette = useCallback((type: NodeType) => {
    if (!selectedPath) {
      addNode(type);
      return;
    }
    const selected = getAt<FlowNode>(definitionRef.current, selectedPath);
    if (selected.type === "condition") {
      addNode(type, [...selectedPath, "then"]);
    } else if (selected.type === "loop") {
      addNode(type, [...selectedPath, "steps"]);
    } else if (selected.type === "parallel") {
      addNode(type, [...selectedPath, "branches", 0, "steps"]);
    } else {
      const sequencePath = selectedPath.slice(0, -1);
      addNode(type, sequencePath, Number(selectedPath[selectedPath.length - 1]) + 1);
    }
  }, [selectedPath, addNode]);

  /** 拖动结束：按画布纵坐标重排同层节点顺序（只影响同序列，不改变嵌套归属）。 */
  const onNodeDragStop: OnNodeDrag = useCallback((_event, dragged) => {
    const data = dragged.data as PilotNodeData;
    if (!data.sequencePath || data.index === undefined) return;
    const key = pathKey(data.sequencePath);
    const siblings = nodes
      .filter((node) => {
        const item = node.data as PilotNodeData;
        return item.kind === "flow" && item.sequencePath && pathKey(item.sequencePath) === key;
      })
      .sort((a, b) => a.position.y - b.position.y);
    const order = siblings.map((node) => (node.data as PilotNodeData).index!);
    updateDefinition(reorderSequence(definitionRef.current, data.sequencePath, order));
    setSelectedPath(null);
  }, [nodes, updateDefinition]);

  const onNodeClick = useCallback((_event: ReactMouseEvent, node: Node) => {
    const data = node.data as PilotNodeData;
    if (data.path) setSelectedPath(data.path);
  }, []);

  /** 从节点库拖拽落点添加：落在容器节点内部，或目标节点的其后。 */
  const onDrop = useCallback((event: DragEvent) => {
    event.preventDefault();
    const type = event.dataTransfer.getData("application/pilot-node") as NodeType;
    if (!type) return;
    const element = document.elementFromPoint(event.clientX, event.clientY)?.closest(".react-flow__node");
    const targetId = element?.getAttribute("data-id");
    const target = nodes.find((node) => node.id === targetId);
    const data = target?.data as PilotNodeData | undefined;
    if (data?.path && data.ast) {
      if (data.ast.type === "condition") addNode(type, [...data.path, "then"]);
      else if (data.ast.type === "loop") addNode(type, [...data.path, "steps"]);
      else if (data.ast.type === "parallel") addNode(type, [...data.path, "branches", 0, "steps"]);
      else addNode(type, data.sequencePath, (data.index ?? -1) + 1);
    } else {
      addNode(type);
    }
  }, [nodes, addNode]);

  /** 新建子流程并切换到它。 */
  const addSubflow = useCallback(() => {
    const id = createId("subflow");
    const next = clone(definitionRef.current);
    next.subflows[id] = { name: "新子任务", steps: [createNode("test")] };
    updateDefinition(next);
    setSubflowError("");
    setCurrentFlow(id);
    setSelectedPath(null);
  }, [updateDefinition]);

  const editSubflow = useCallback((id: string) => {
    setSubflowError("");
    setCurrentFlow(id);
    setSelectedPath(null);
  }, []);

  /** 删除当前子流程：仍被引用时拒绝并列出引用位置。 */
  const deleteCurrentSubflow = useCallback(() => {
    if (currentFlow === "main") return;
    const references = findSubflowReferences(definitionRef.current, currentFlow);
    if (references.length) {
      setSubflowError(`无法删除：仍被 ${references.join("、")} 引用`);
      return;
    }
    if (!window.confirm(`确定删除未被引用的子任务 ${currentFlow} 吗？`)) return;
    const next = clone(definitionRef.current);
    delete next.subflows[currentFlow];
    updateDefinition(next);
    setSubflowError("");
    setCurrentFlow("main");
    setSelectedPath(null);
  }, [currentFlow, updateDefinition]);

  /** 复制选中的测试项（新 ID + “副本”名称），插到原节点之后。 */
  const duplicateSelected = useCallback(() => {
    if (!selectedPath) return;
    const original = getAt<FlowNode>(definitionRef.current, selectedPath);
    if (original.type !== "test") return;
    const sequencePath = selectedPath.slice(0, -1);
    const index = Number(selectedPath[selectedPath.length - 1]) + 1;
    const copy = { ...clone(original), id: createId("test"), name: `${original.name} 副本` };
    updateDefinition(insertNode(definitionRef.current, sequencePath, index, copy));
    setSelectedPath([...sequencePath, index]);
  }, [selectedPath, updateDefinition]);

  const selectedNode = selectedPath ? getAt<FlowNode>(definition, selectedPath) : null;
  return (
    <div className="pilot-flow-editor">
      <aside className="pilot-flow-palette">
        <div className="pilot-flow-panel-title"><span>NODE LIBRARY</span><strong>控制节点</strong></div>
        {(["test", "condition", "parallel", "loop", "subflow"] as NodeType[]).map((type) => (
          <button key={type} className={`pilot-library-node pilot-library-${type}`} draggable onDragStart={(event) => { event.dataTransfer.setData("application/pilot-node", type); event.dataTransfer.effectAllowed = "copy"; }} onClick={() => addFromPalette(type)}>
            <span>{nodeLabels[type]}</span><small>{nodeDescriptions[type]}</small>
          </button>
        ))}
        <p>拖到画布或节点上添加。拖动已有节点可重排同一层级。</p>
        <label className="pilot-subflow-label">子任务 / 子流程</label>
        <select value={currentFlow} onChange={(event) => { setSubflowError(""); setCurrentFlow(event.target.value); setSelectedPath(null); }}>
          <option value="main">主流程</option>
          {Object.entries(definition.subflows).map(([id, subflow]) => <option key={id} value={id}>{subflow.name} · {id}</option>)}
        </select>
        <button className="pilot-secondary-button" onClick={addSubflow}>+ 新建子任务</button>
        {currentFlow !== "main" && <button className="pilot-secondary-button danger" onClick={deleteCurrentSubflow}>删除当前子任务</button>}
        {subflowError && <p className="pilot-subflow-error">{subflowError}</p>}
      </aside>
      <div className="pilot-flow-canvas" ref={canvasRef}>
        <div className="pilot-canvas-banner"><strong>{currentFlow === "main" ? "主流程" : definition.subflows[currentFlow]?.name}</strong><span>点击节点配置 · 拖动排序 · 从左侧添加</span></div>
        <ReactFlow
          nodes={nodes}
          edges={layout.edges}
          nodeTypes={nodeTypes}
          onNodesChange={(changes) => setNodes((current) => applyNodeChanges(changes, current))}
          onNodeDragStop={onNodeDragStop}
          onNodeClick={onNodeClick}
          onDrop={onDrop}
          onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }}
          onInit={(instance) => { flowInstance.current = instance; }}
          nodesConnectable={false}
          deleteKeyCode={null}
          fitView
          fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
          minZoom={0.25}
          maxZoom={1.6}
        >
          <Background gap={18} size={1} />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
      <aside className="pilot-flow-inspector">
        {selectedNode && selectedPath ? (
          <Inspector
            definition={definition}
            node={selectedNode}
            path={selectedPath}
            currentFlow={currentFlow}
            firmware={firmware}
            onUpdate={(updater) => updateDefinition(updateNode(definitionRef.current, selectedPath, updater))}
            onDelete={() => { updateDefinition(removeNode(definitionRef.current, selectedPath)); setSelectedPath(null); }}
            onDuplicate={duplicateSelected}
            onAddNested={(path, type) => addNode(type, path)}
            onEditSubflow={editSubflow}
            onUpdateDefinition={updateDefinition}
          />
        ) : (
          <div className="pilot-inspector-empty"><strong>选择一个节点</strong><p>点击节点编辑参数，或从左侧拖入条件、并行、循环和子流程。</p></div>
        )}
      </aside>
    </div>
  );
}

/** 检查器通用字段包装：标签 + 控件 + 可选提示。 */
function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <label className="pilot-field"><span>{label}</span>{children}{hint && <small className="pilot-field-hint">{hint}</small>}</label>;
}

/** JSON 编辑框：本地保留文本态，仅在解析成功时回写（非法输入标红不打断输入）。 */
function JsonField({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const serialized = JSON.stringify(value ?? {}, null, 2);
  const [text, setText] = useState(serialized);
  const [valid, setValid] = useState(true);
  useEffect(() => setText(serialized), [serialized]);
  return <textarea
    className={valid ? "" : "invalid"}
    rows={4}
    value={text}
    onChange={(event) => {
      const next = event.target.value;
      setText(next);
      try {
        const parsed = JSON.parse(next) as unknown;
        setValid(true);
        onChange(parsed);
      } catch {
        setValid(false);
      }
    }}
  />;
}

/** 输入文本智能转型：纯十进制数字转 Number，其余保持字符串。 */
function decimalOrText(text: string): unknown {
  const trimmed = text.trim();
  return /^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(trimmed) ? Number(trimmed) : text;
}

/** 断言/条件编辑字段组：按操作符切换期望值输入形态（区间双输入、数值、文本）。 */
function AssertionFields({ value, onChange }: { value: Assertion; onChange: (value: Assertion) => void }) {
  const assertion = value || { actual: "${response}", operator: "equals", expected: "PASS" };
  const bounds = Array.isArray(assertion.expected) ? [...assertion.expected] : [0, 1];
  const numeric = ["gt", "gte", "lt", "lte"].includes(assertion.operator);
  const updateBound = (index: number, text: string) => {
    bounds[index] = text === "" ? "" : Number(text);
    onChange({ ...assertion, expected: bounds });
  };
  return <div className="pilot-assertion-fields">
    <Field label="实际值 / 变量" hint="直接填变量路径（response、flash_result.ok），也支持 ${response} 模板写法"><input value={String(assertion.actual ?? "")} onChange={(event) => onChange({ ...assertion, actual: event.target.value })} /></Field>
    <Field label="判断方式"><select value={assertion.operator} onChange={(event) => {
      const operator = event.target.value;
      const next = { ...assertion, operator };
      if (operator === "between" && !Array.isArray(next.expected)) next.expected = [0, 1];
      if (["gt", "gte", "lt", "lte"].includes(operator) && Array.isArray(next.expected)) next.expected = 0;
      onChange(next);
    }}>{Object.entries(operatorLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
    {assertion.operator === "between" && <div className="pilot-field-grid"><Field label="十进制下限"><input type="number" step="any" value={String(bounds[0] ?? "")} onChange={(event) => updateBound(0, event.target.value)} /></Field><Field label="十进制上限"><input type="number" step="any" value={String(bounds[1] ?? "")} onChange={(event) => updateBound(1, event.target.value)} /></Field></div>}
    {!["truthy", "exists", "between"].includes(assertion.operator) && <Field label={numeric ? "十进制期望值" : "期望值 / 变量"}><input
      type={numeric ? "number" : "text"}
      step={numeric ? "any" : undefined}
      value={String(assertion.expected ?? "")}
      onChange={(event) => {
        const text = event.target.value;
        const expected = numeric ? (text === "" ? "" : Number(text)) : decimalOrText(text);
        onChange({ ...assertion, expected });
      }}
    /></Field>}
  </div>;
}

/** 十进制列表输入（J-Link 写内存字节等）：逗号/空白分隔，全部合法才回写。 */
function DecimalListInput({ value, onChange }: { value: unknown; onChange: (value: number[]) => void }) {
  const serialized = Array.isArray(value) ? value.join(", ") : "";
  const [text, setText] = useState(serialized);
  const [valid, setValid] = useState(true);
  useEffect(() => setText(serialized), [serialized]);
  return <input className={valid ? "" : "invalid"} inputMode="decimal" placeholder="例如：0, 255, 16" value={text} onChange={(event) => {
    const next = event.target.value;
    setText(next);
    const parts = next.split(/[,，\s]+/).filter(Boolean);
    const isValid = parts.length > 0 && parts.every((part) => /^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(part));
    setValid(isValid);
    if (isValid) onChange(parts.map(Number));
  }} />;
}

// execute 动作的设备操作选项（Modbus / J-Link），与 protocols.py 的执行分支一一对应
const modbusOperations: Record<string, string> = {
  read_holding_registers: "读保持寄存器",
  read_input_registers: "读输入寄存器",
  read_coils: "读线圈",
  read_discrete_inputs: "读离散输入",
  write_register: "写单个寄存器",
  write_coil: "写单个线圈",
};
const jlinkOperations: Record<string, string> = {
  read_memory: "读内存",
  write_memory: "写内存",
  read_register: "读寄存器",
  write_register: "写寄存器",
  reset: "复位",
  halt: "暂停",
  go: "运行",
};

/** 动作参数编辑表单：按动作类型渲染对应字段；Modbus/J-Link 的 execute 提供结构化参数。 */
function ActionFields({ action, protocol, firmware, onChange }: { action: Action; protocol?: string; firmware: FirmwareInfo[]; onChange: (action: Action) => void }) {
  const update = (patch: Partial<Action>) => onChange({ ...action, ...patch });
  const params = (action.params || {}) as Record<string, unknown>;
  const updateParam = (key: string, value: unknown) => update({ params: { ...params, [key]: value } });
  const numberParam = (key: string, label: string) => <Field label={label}><input type="number" step="any" value={String(params[key] ?? "")} onChange={(event) => updateParam(key, event.target.value === "" ? "" : Number(event.target.value))} /></Field>;
  const operation = String(action.operation || "");
  return <>
    <Field label="动作类型"><select value={action.type} onChange={(event) => onChange(createAction(event.target.value))}>{Object.entries(actionLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
    {["request", "send"].includes(action.type) && <>
      <Field label="十进制数据 / 文本指令 / 变量" hint="数值只填十进制，多个数值用逗号分隔；数据类型、字节序和缩放由当前协议配置统一决定。"><textarea rows={2} inputMode="decimal" value={String(action.data ?? "")} onChange={(event) => update({ data: event.target.value })} /></Field>
      {action.type === "request" && <Field label="响应保存为" hint="后续动作通过 ${变量名} 使用此响应"><input value={String(action.save_as ?? "")} onChange={(event) => update({ save_as: event.target.value })} /></Field>}
    </>}
    {action.type === "receive" && <>
      <Field label="响应保存为"><input value={String(action.save_as ?? "")} onChange={(event) => update({ save_as: event.target.value })} /></Field>
      <div className="pilot-field-grid"><Field label="超时 ms"><input type="number" min={1} value={Number(action.timeout_ms || 1000)} onChange={(event) => update({ timeout_ms: Number(event.target.value) })} /></Field><Field label="字节数"><input type="number" min={1} value={Number(action.size || 4096)} onChange={(event) => update({ size: Number(event.target.value) })} /></Field></div>
    </>}
    {action.type === "execute" && <>
      <Field label="设备操作">{protocol === "modbus" || protocol === "jlink" ? <select value={operation} onChange={(event) => update({ operation: event.target.value, params: {} })}>{Object.entries(protocol === "modbus" ? modbusOperations : jlinkOperations).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select> : <input value={operation} onChange={(event) => update({ operation: event.target.value })} />}</Field>
      {["read_holding_registers", "read_input_registers", "read_coils", "read_discrete_inputs", "write_register", "write_coil"].includes(operation) && protocol === "modbus" && numberParam("address", "寄存器地址（十进制）")}
      {["read_holding_registers", "read_input_registers", "read_coils", "read_discrete_inputs"].includes(operation) && protocol === "modbus" && numberParam("count", "读取数量")}
      {operation === "write_register" && protocol === "modbus" && numberParam("value", "写入值（十进制）")}
      {operation === "write_coil" && protocol === "modbus" && <Field label="线圈值"><select value={String(params.value === true ? 1 : params.value === false ? 0 : params.value ?? 0)} onChange={(event) => updateParam("value", Number(event.target.value))}><option value="0">0 · 断开</option><option value="1">1 · 闭合</option></select></Field>}
      {["read_memory", "write_memory"].includes(operation) && protocol === "jlink" && numberParam("address", "内存地址（十进制）")}
      {operation === "read_memory" && protocol === "jlink" && numberParam("size", "读取字节数")}
      {operation === "write_memory" && protocol === "jlink" && <Field label="写入字节（十进制）"><DecimalListInput value={params.data} onChange={(value) => updateParam("data", value)} /></Field>}
      {["read_register", "write_register"].includes(operation) && protocol === "jlink" && numberParam("register", "寄存器编号（十进制）")}
      {operation === "write_register" && protocol === "jlink" && numberParam("value", "写入值（十进制）")}
      {!["modbus", "jlink"].includes(protocol || "") && <Field label="操作参数（十进制 JSON）"><JsonField value={params} onChange={(value) => update({ params: value })} /></Field>}
      <Field label="结果保存为"><input value={String(action.save_as ?? "")} onChange={(event) => update({ save_as: event.target.value })} /></Field>
    </>}
    {action.type === "set" && <>
      <Field label="变量名"><input value={String(action.variable ?? "")} onChange={(event) => update({ variable: event.target.value })} /></Field>
      <Field label="十进制数值 / 文本 / 变量"><input value={String(action.value ?? "")} onChange={(event) => update({ value: decimalOrText(event.target.value) })} /></Field>
    </>}
    {action.type === "assert" && <AssertionFields value={action as unknown as Assertion} onChange={(assertion) => onChange({ ...assertion, type: "assert" })} />}
    {action.type === "sleep" && <Field label="延时 ms"><input type="number" min={0} value={Number(action.duration_ms || 0)} onChange={(event) => update({ duration_ms: Number(event.target.value) })} /></Field>}
    {action.type === "flash" && <FlashFields action={action} firmware={firmware} onChange={onChange} />}
  </>;
}

/** 固件烧录动作字段：固件候选来自固件仓（支持 ${变量}），含擦除/校验/复位选项。 */
function FlashFields({ action, firmware, onChange }: { action: Action; firmware: FirmwareInfo[]; onChange: (action: Action) => void }) {
  const options = (action.options || {}) as Record<string, unknown>;
  const update = (patch: Partial<Action>) => onChange({ ...action, ...patch });
  const updateOption = (key: string, value: unknown) => update({ options: { ...options, [key]: value } });
  return <>
    <Field label="固件" hint="候选来自“固件烧录”页面登记的固件仓；也可填 ${变量} 在运行时解析。">
      <input list="pilot-firmware-options" placeholder={firmware.length ? "选择或输入固件名称" : "固件仓为空，请先在“固件烧录”页面登记"} value={String(action.firmware ?? "")} onChange={(event) => update({ firmware: event.target.value })} />
      <datalist id="pilot-firmware-options">
        {firmware.map((item) => <option key={item.name} value={item.name}>{`v${item.version || "?"} · ${item.product_model || "通用"}`}</option>)}
      </datalist>
    </Field>
    <div className="pilot-field-grid">
      <Field label="目标槽位（可选）"><input value={String(action.target ?? "")} onChange={(event) => update({ target: event.target.value })} /></Field>
      <Field label="结果保存为" hint="保存烧录结果，可断言 ${变量.ok} 判定烧录是否成功"><input value={String(action.save_as ?? "")} onChange={(event) => update({ save_as: event.target.value })} /></Field>
    </div>
    <div className="pilot-field-grid">
      <Field label="烧录超时 ms"><input type="number" min={1} value={Number(options.timeout_ms ?? 60000)} onChange={(event) => updateOption("timeout_ms", Number(event.target.value))} /></Field>
      {(["erase", "verify", "reset"] as const).map((key) => (
        <Field key={key} label={{ erase: "烧录前擦除", verify: "烧录后校验", reset: "烧录后复位" }[key]}>
          <select value={String(options[key] ?? true)} onChange={(event) => updateOption(key, event.target.value === "true")}><option value="true">是</option><option value="false">否</option></select>
        </Field>
      ))}
    </div>
  </>;
}

/** 动作追加器：选择动作类型后添加到当前测试项末尾。 */
function ActionAdder({ onAdd }: { onAdd: (type: string) => void }) {
  const [type, setType] = useState("request");
  return <div className="pilot-action-adder"><select value={type} onChange={(event) => setType(event.target.value)}>{Object.entries(actionLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button className="pilot-secondary-button" onClick={() => onAdd(type)}>+ 添加动作</button></div>;
}

/** 嵌套节点追加器：向条件分支/循环体/并行分支内添加节点。 */
function NestedNodeAdder({ onAdd }: { onAdd: (type: NodeType) => void }) {
  const [type, setType] = useState<NodeType>("test");
  return <span className="pilot-nested-adder"><select value={type} onChange={(event) => setType(event.target.value as NodeType)}>{Object.entries(nodeLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button className="pilot-secondary-button" onClick={() => onAdd(type)}>+ 添加</button></span>;
}

interface InspectorProps {
  definition: FlowDefinition;
  node: FlowNode;
  path: Path;
  currentFlow: string;
  firmware: FirmwareInfo[];
  onUpdate: (updater: (node: FlowNode) => FlowNode) => void;
  onDelete: () => void;
  onDuplicate: () => void;
  onAddNested: (path: Path, type: NodeType) => void;
  onEditSubflow: (id: string) => void;
  onUpdateDefinition: (definition: FlowDefinition) => void;
}

/** 右侧节点检查器：按节点类型渲染属性表单（测试项动作列表、条件分支、并行分支、循环、子流程）。 */
function Inspector({ definition, node, path, currentFlow, firmware, onUpdate, onDelete, onDuplicate, onAddNested, onEditSubflow, onUpdateDefinition }: InspectorProps) {
  const update = (patch: Partial<FlowNode>) => onUpdate((current) => ({ ...current, ...patch }));
  return <div className="pilot-inspector-content">
    <div className="pilot-inspector-title"><span>{nodeLabels[node.type]}</span><strong>{node.name}</strong><small>{path.join(" / ")}</small></div>
    <Field label="节点名称"><input value={node.name} onChange={(event) => update({ name: event.target.value })} /></Field>
    <Field label="节点 ID"><input value={node.id} onChange={(event) => update({ id: event.target.value })} /></Field>
    <Field label="失败策略"><select value={node.on_fail} onChange={(event) => update({ on_fail: event.target.value as "stop" | "continue" })}><option value="stop">停止后续</option><option value="continue">继续</option></select></Field>

    {node.type === "test" && <>
      <Field label="通讯协议"><select value={node.protocol} onChange={(event) => update({ protocol: event.target.value })}>{["simulator", "serial", "can", "tcp", "modbus", "bluetooth", "wifi", "mqtt", "jlink"].map((protocol) => <option key={protocol}>{protocol}</option>)}</select></Field>
      <div className="pilot-field-grid"><Field label="超时 ms"><input type="number" value={node.timeout_ms} onChange={(event) => update({ timeout_ms: Number(event.target.value) })} /></Field><Field label="重试"><input type="number" value={node.retries} onChange={(event) => update({ retries: Number(event.target.value) })} /></Field></div>
      <div className="pilot-inspector-section"><div className="pilot-section-title"><strong>执行动作</strong><span>从上到下依次执行</span></div>{(node.actions || []).map((action, index) => <div className="pilot-action-editor" key={index}>
        <div className="pilot-action-toolbar"><span><i>{index + 1}</i>{actionLabels[action.type]}</span><div><button title="上移动作" disabled={index === 0} onClick={() => onUpdate((current) => { const next = clone(current); [next.actions![index - 1], next.actions![index]] = [next.actions![index], next.actions![index - 1]]; return next; })}>↑</button><button title="下移动作" disabled={index === node.actions!.length - 1} onClick={() => onUpdate((current) => { const next = clone(current); [next.actions![index + 1], next.actions![index]] = [next.actions![index], next.actions![index + 1]]; return next; })}>↓</button><button title="删除动作" onClick={() => onUpdate((current) => { const next = clone(current); next.actions!.splice(index, 1); return next; })}>×</button></div></div>
        <ActionFields action={action} protocol={node.protocol} firmware={firmware} onChange={(updated) => onUpdate((current) => { const next = clone(current); next.actions![index] = updated; return next; })} />
      </div>)}<ActionAdder onAdd={(type) => update({ actions: [...(node.actions || []), createAction(type)] })} /><p className="pilot-inspector-note">先采集数据，再添加断言。动作结果用变量连接，无需手工编写编码或执行代码。</p></div>
    </>}

    {node.type === "condition" && <>
      <AssertionFields value={node.condition!} onChange={(condition) => update({ condition })} />
      <div className="pilot-inspector-section"><strong>分支内容</strong>
        <div className="pilot-control-branch true"><div><strong>TRUE 分支</strong><span>{node.then?.length || 0} 个节点</span></div><NestedNodeAdder onAdd={(type) => onAddNested([...path, "then"], type)} /></div>
        <div className="pilot-control-branch false"><div><strong>FALSE 分支</strong><span>{node.else?.length || 0} 个节点</span></div><NestedNodeAdder onAdd={(type) => onAddNested([...path, "else"], type)} /></div>
        <p className="pilot-inspector-note">条件为 FALSE 是正常分支，不代表测试失败。分支内还可以嵌套条件、循环和子任务。</p>
      </div>
    </>}

    {node.type === "parallel" && <>
      <Field label="最大并行工作线程"><input type="number" min={1} max={8} value={node.max_workers} onChange={(event) => update({ max_workers: Number(event.target.value) })} /></Field>
      <div className="pilot-inspector-section"><strong>并行分支</strong>{node.branches?.map((branch, index) => <div className="pilot-branch-editor" key={index}>
        <div className="pilot-branch-order"><strong>分支 {index + 1}</strong><div><button title="上移分支" disabled={index === 0} onClick={() => onUpdate((current) => { const next = clone(current); [next.branches![index - 1], next.branches![index]] = [next.branches![index], next.branches![index - 1]]; return next; })}>↑</button><button title="下移分支" disabled={index === node.branches!.length - 1} onClick={() => onUpdate((current) => { const next = clone(current); [next.branches![index + 1], next.branches![index]] = [next.branches![index], next.branches![index + 1]]; return next; })}>↓</button></div></div>
        <Field label="分支名称"><input value={branch.name} onChange={(event) => onUpdate((current) => { const next = clone(current); next.branches![index].name = event.target.value; return next; })} /></Field>
        <Field label="分支 ID"><input value={branch.id} onChange={(event) => onUpdate((current) => { const next = clone(current); next.branches![index].id = event.target.value; return next; })} /></Field>
        <small>{branch.steps.length} 个节点 · 输出路径 <code>{`\${branches.${branch.id}.变量名}`}</code></small>
        <NestedNodeAdder onAdd={(type) => onAddNested([...path, "branches", index, "steps"], type)} />
        <button className="pilot-delete-button" disabled={node.branches!.length <= 2} onClick={() => onUpdate((current) => { const next = clone(current); next.branches!.splice(index, 1); next.max_workers = Math.min(next.max_workers || 2, next.branches!.length); return next; })}>删除此分支</button>
      </div>)}<button className="pilot-secondary-button" onClick={() => onUpdate((current) => { const next = clone(current); next.branches!.push({ id: createId("branch"), name: `分支 ${next.branches!.length + 1}`, steps: [createNode("test")] }); return next; })}>+ 添加并行分支</button><p className="pilot-inspector-note">排序只影响展示，各分支仍并发执行。共享串口、CAN 或 J-Link 时不要并行占用。</p></div>
    </>}

    {node.type === "loop" && <>
      <Field label="循环模式"><select value={node.mode} onChange={(event) => { const mode = event.target.value as "count" | "while"; update({ mode, ...(mode === "while" && !node.condition ? { condition: { actual: "${iteration}", operator: "lte", expected: 2 } } : {}) }); }}><option value="count">固定次数</option><option value="while">条件 while</option></select></Field>
      {node.mode === "count" ? <Field label="循环次数"><input type="number" min={1} max={100} value={node.count} onChange={(event) => update({ count: Number(event.target.value) })} /></Field> : <AssertionFields value={node.condition!} onChange={(condition) => update({ condition })} />}
      <Field label="最大迭代次数"><input type="number" min={1} max={100} value={node.max_iterations} onChange={(event) => update({ max_iterations: Number(event.target.value) })} /></Field>
      <label className="pilot-checkbox"><input type="checkbox" checked={node.break_on_fail} onChange={(event) => update({ break_on_fail: event.target.checked })} />失败时中断循环</label>
      <div className="pilot-inspector-section"><strong>循环体</strong><NestedNodeAdder onAdd={(type) => onAddNested([...path, "steps"], type)} /><p className="pilot-inspector-note">当前次数使用 ${"{iteration}"}，从 0 开始的下标使用 ${"{iteration0}"}。循环体可以包含条件、并行或子任务。</p></div>
    </>}

    {node.type === "subflow" && <>
      <Field label="复用子任务"><select value={node.subflow} onChange={(event) => update({ subflow: event.target.value })}>{Object.entries(definition.subflows).map(([id, subflow]) => <option key={id} value={id}>{subflow.name} · {id}</option>)}</select></Field>
      <button className="pilot-secondary-button" disabled={!node.subflow || !definition.subflows[node.subflow]} onClick={() => onEditSubflow(node.subflow!)}>编辑此子任务</button>
      <Field label="输入变量 JSON"><JsonField value={node.inputs || {}} onChange={(inputs) => update({ inputs: inputs as Record<string, unknown> })} /></Field>
      <p className="pilot-inspector-note">输入可使用 ${"{sn}"} 等父流程变量；输出通过 <code>{`\${subflows.${node.id}.变量名}`}</code> 访问。子任务使用独立变量作用域。</p>
    </>}

    {currentFlow !== "main" && <Field label="当前子任务显示名称"><input value={definition.subflows[currentFlow].name} onChange={(event) => { const next = clone(definition); next.subflows[currentFlow].name = event.target.value; onUpdateDefinition(next); }} /></Field>}
    {node.type === "test" && <button className="pilot-duplicate-button" onClick={onDuplicate}>复制此测试项</button>}
    <button className="pilot-delete-button" onClick={onDelete}>删除此节点</button>
  </div>;
}

/** 挂载编辑器到指定容器（app.js 调用；重复调用复用同一 React root）。 */
function mount(container: HTMLElement, options: EditorProps) {
  if (!editorRoot) editorRoot = createRoot(container);
  editorRoot.render(<FlowEditorApp {...options} />);
}

/** 外部（app.js）推送新草稿：已挂载则直接更新，未挂载则暂存待挂载时应用。 */
function setDefinition(definition: FlowDefinition) {
  latestDefinition = definition;
  externalSetDefinition?.(definition);
}

/** 外部（app.js）推送固件仓清单，供 flash 动作的候选列表使用。 */
function setFirmware(firmware: FirmwareInfo[]) {
  latestFirmware = firmware;
  externalSetFirmware?.(firmware);
}

export { FlowEditorApp, Inspector, mount, setDefinition, setFirmware };
