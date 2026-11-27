"use strict";

/**
 * 前端单页应用（原生 JS，无框架）：登录、生产测试执行台、流程编排、通讯配置、
 * 固件烧录、MES 集成、报告中心、账号权限八个页面。
 *
 * 结构约定：
 * - state 为唯一全局状态；render* 函数把 state 渲染到 DOM，load* 函数拉取后端数据；
 * - 所有后端请求经 api() 封装（JSON、错误消息提取、401 统一跳登录）；
 * - 图形流程编辑画布由 flow-editor.js（React 构建产物，源码在 frontend/）提供，
 *   通过 window.PilotFlowEditor 挂载，与步骤列表编辑器共享同一份草稿 state.draft。
 */

/** querySelector / querySelectorAll 的简写。 */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
/** 深拷贝（流程草稿编辑等场景避免共享引用）。 */
const clone = (value) => JSON.parse(JSON.stringify(value));

/** 全局状态：登录用户、流程列表与执行台选中项、编辑草稿、运行/轮询、各配置页数据。 */
const state = {
  user: null,
  users: [],
  applicationMounted: false,
  scripts: [],
  selectedScriptId: null,
  selectedProduct: null,
  selectedStation: "",
  draft: null,
  draftId: null,
  draftVersion: null,
  scan: null,
  run: null,
  pollTimer: null,
  protocols: [],
  selectedProtocol: "simulator",
  firmware: [],
  flashers: [],
  flasherConfig: {},
  mesConfig: {},
  settings: {},
  master: { products: [], stations: [], productsSync: null, stationsSync: null, mode: "simulator" },
  reports: [],
  editorMode: "visual",
  selectedStep: 0,
  selectedAction: null,
  drag: null,
};

/** 页面元信息：hash 路由 → 顶栏标题/副标题。 */
const pageMeta = {
  production: ["生产测试", "扫描产品 SN，执行测试并绑定唯一报告"],
  scripts: ["流程编排", "低代码编辑测试逻辑，后端统一编译执行"],
  protocols: ["通讯配置", "管理串口、总线、网络与调试器连接"],
  firmware: ["固件烧录", "登记固件入仓，选择烧录后端并验证连接"],
  mes: ["MES 集成", "配置报告报文、接口与工位上传策略"],
  reports: ["报告中心", "按 SN 追溯测试数据与 MES 上传状态"],
  users: ["账号权限", "管理登录账号、角色和启用状态"],
};

/** 协议缩写图标（通讯配置页左侧导航）。 */
const protocolSymbols = {
  simulator: "SIM", serial: "COM", can: "CAN", tcp: "TCP", modbus: "MB",
  bluetooth: "BLE", wifi: "WiFi", mqtt: "MQTT", jlink: "JLK",
};

/** 动作类型 → 中文名（流程编排编辑器）。 */
const actionLabels = {
  request: "发送并等待响应", send: "仅发送", receive: "等待接收", execute: "设备操作",
  set: "设置变量", assert: "结果断言", sleep: "延时等待", flash: "固件烧录",
};

/** 动作类型 → 节点角标缩写（画布节点上的徽标）。 */
const actionSymbols = {
  request: "REQ", send: "TX", receive: "RX", execute: "EXE",
  set: "SET", assert: "IF", sleep: "WAIT", flash: "FLASH",
};

/** 断言操作符 → 中文名（断言动作的下拉选项）。 */
const operatorLabels = {
  equals: "等于", not_equals: "不等于", contains: "包含", not_contains: "不包含",
  regex: "正则匹配", gt: "大于", gte: "大于等于", lt: "小于", lte: "小于等于",
  between: "数值区间", truthy: "为真", exists: "存在且非空",
};

/** HTML 转义：所有拼接进 innerHTML 的动态值必须经此处理，防 XSS。 */
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

/** 展示值格式化：空值显示为 —，对象转 JSON 字符串。 */
function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** ISO 时间戳 → 本地时间字符串；非法值原样返回。 */
function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

/** 毫秒时长 → “123 ms” / “1.23 s”。 */
function formatDuration(value) {
  const ms = Number(value || 0);
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** 状态徽标 HTML（PASS/FAIL/RUNNING/PENDING 等着色由 styles.css 的 badge-* 决定）。 */
function statusBadge(status) {
  const value = status || "PENDING";
  return `<span class="badge badge-${escapeHtml(value.toLowerCase())}">${escapeHtml(value)}</span>`;
}

/** MES 上传状态 → 中文文案。 */
function mesStatusText(status) {
  return { PENDING: "待上传", UPLOADING: "上传中", SUCCESS: "上传成功", FAILED: "上传失败" }[status] || "未生成";
}

/**
 * 后端请求封装：自动 JSON 序列化请求体、解析响应；
 * 非 2xx 时提取 FastAPI detail/编译错误并抛出带可读消息的 Error（error.status 附状态码）；
 * 401 时统一跳转登录页（登录/会话探测接口除外）。
 */
async function api(url, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.body && typeof request.body !== "string" && !(request.body instanceof Blob)) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(url, request);
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    let message = payload?.detail || payload?.message || `请求失败（HTTP ${response.status}）`;
    if (typeof message === "object") {
      message = message.message || (Array.isArray(message) ? message.map((item) => item.msg).join("；") : JSON.stringify(message));
      if (payload?.detail?.errors) message += `：${payload.detail.errors.join("；")}`;
    }
    const error = new Error(message);
    error.status = response.status;
    if (response.status === 401 && !["/api/auth/login", "/api/auth/me"].includes(url)) showLogin();
    throw error;
  }
  return payload;
}

/** 右下角浮动提示，4.2 秒后自动消失；type 为 "success"/"error"/""。 */
function toast(message, type = "") {
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.textContent = message;
  $("#toast-container").append(element);
  setTimeout(() => element.remove(), 4200);
}

/** 解析 JSON 输入框，失败时抛出带字段名的可读错误。 */
function parseJsonField(value, label) {
  try { return JSON.parse(value); } catch (error) { throw new Error(`${label} JSON 格式错误：${error.message}`); }
}

/** 智能取值：数字/true/false/null 文本转对应类型，其余保持字符串（编辑器输入用）。 */
function parseSmartValue(value) {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  return value;
}

/**
 * 页面切换（hash 路由）：操作工访问管理页时回落到生产页；
 * 每次切换按需加载该页数据（生产页同时刷新流程与主数据，保证编排页改动即时生效）。
 */
function navigate(page) {
  if (!pageMeta[page]) return;
  if (state.user?.role !== "admin" && ["scripts", "protocols", "firmware", "mes", "users"].includes(page)) page = "production";
  $$(".page").forEach((element) => element.classList.toggle("active", element.id === `page-${page}`));
  $$(".nav-item").forEach((element) => element.classList.toggle("active", element.dataset.page === page));
  $("#page-title").textContent = pageMeta[page][0];
  $("#page-subtitle").textContent = pageMeta[page][1];
  history.replaceState(null, "", `#${page}`);
  // 切回生产页时刷新流程列表与主数据，保证编排页新建/停用的流程和 MES 同步的产品即时反映到执行台索引
  if (page === "production") { loadDashboard(); loadMasterData(); loadScripts().then(renderPreview); }
  if (page === "scripts") { renderScriptLibrary(); loadFirmware(); loadMasterData(); }
  if (page === "protocols") loadProtocols();
  if (page === "firmware") loadFirmware();
  if (page === "mes") { loadMesSettings(); loadMasterData(); }
  if (page === "reports") loadReports();
  if (page === "users") loadUsers();
}

/** 加载仪表盘：四张指标卡 + 最近测试报告表。 */
async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    $("#metric-today").textContent = data.today;
    $("#metric-total").textContent = data.total;
    $("#metric-failed").textContent = data.failed;
    $("#metric-pass-rate").textContent = data.today ? `${(data.today_passed / data.today * 100).toFixed(1)}%` : "—";
    $("#metric-today-pass").textContent = `${data.today_passed} 项通过`;
    $("#recent-reports").innerHTML = data.recent_reports.length ? data.recent_reports.map((report) => `
      <tr>
        <td><span class="sn-primary">${escapeHtml(report.report_no)}</span></td>
        <td><span class="sn-primary">${escapeHtml(report.sn)}</span></td>
        <td>${escapeHtml(report.product_model)}</td>
        <td>${escapeHtml(formatTime(report.finished_at))}</td>
        <td>${statusBadge(report.status)}</td>
        <td>${statusBadge(report.mes_status)} <span class="sn-secondary">${mesStatusText(report.mes_status)}</span></td>
        <td><button class="text-button" data-report-id="${escapeHtml(report.id)}">查看</button></td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty-row">暂无报告，扫描 SN 开始第一台产品测试</td></tr>';
  } catch (error) { toast(error.message, "error"); }
}

// 执行台索引的候选流程：包含所有启用的流程。
// 不再按终端工站预先过滤（否则会“索引不到”绑定其他工站的新建流程）；
// 工站维度交给“测试工站”下拉区分，终端配置工站仅用于默认选中项。
function productionCandidates() {
  return state.scripts.filter((script) => script.enabled);
}

/** 按（产品, 工站）精确匹配启用流程：执行台“带出测试流程”的匹配规则。 */
function matchingScripts() {
  return productionCandidates().filter((script) =>
    script.product_model === state.selectedProduct && (script.station_code || "") === (state.selectedStation || ""));
}

/**
 * 加载流程列表并确定执行台默认选中项：
 * 优先保持当前选中，其次选绑定终端工站的流程，最后取第一个启用流程。
 */
async function loadScripts() {
  state.scripts = await api("/api/scripts");
  const candidates = productionCandidates();
  let script = candidates.find((item) => item.id === state.selectedScriptId);
  if (!script) {
    const station = state.settings?.station_id || "";
    script = (state.selectedProduct && matchingScripts()[0])
      || candidates.find((item) => station && item.station_code === station)
      || candidates[0]
      || null;
  }
  state.selectedScriptId = script ? script.id : null;
  state.selectedProduct = script ? script.product_model : null;
  state.selectedStation = script ? (script.station_code || "") : "";
  renderProductionPicker();
}

/** 产品/工站变化后重选匹配流程（无匹配则清空），并刷新执行台。 */
function resolveProductionScript() {
  const matches = matchingScripts();
  if (!matches.some((item) => item.id === state.selectedScriptId)) {
    state.selectedScriptId = matches[0]?.id || null;
  }
  renderProductionPicker();
}

// 测试工站候选：显示工站主数据中的全部启用工站；
// 产品有未绑定工站的流程时保留“不限工站”入口，流程绑定了主数据之外的工站编号时附加在末尾
function productionStations() {
  const productScripts = state.selectedProduct
    ? productionCandidates().filter((script) => script.product_model === state.selectedProduct)
    : [];
  const enabledStations = state.master.stations.filter((item) => item.enabled);
  if (!enabledStations.length) {
    return [...new Set(productScripts.map((script) => script.station_code || ""))];
  }
  const stations = enabledStations.map((item) => item.code);
  if (productScripts.some((script) => !(script.station_code || ""))) stations.unshift("");
  productScripts.forEach((script) => {
    const code = script.station_code || "";
    if (code && !stations.includes(code)) stations.push(code);
  });
  return stations;
}

function renderProductionPicker() {
  const candidates = productionCandidates();
  // 产品型号与 MES 集成的主数据保持一致：主数据里有多少启用产品就关联出多少；
  // 已有启用流程但不在主数据中的型号附加在末尾，保证旧流程仍可索引执行
  const enabledProducts = state.master.products.filter((item) => item.enabled);
  const products = enabledProducts.map((item) => item.code);
  candidates.forEach((script) => {
    if (!products.includes(script.product_model)) products.push(script.product_model);
  });
  if (products.length && !products.includes(state.selectedProduct)) {
    state.selectedProduct = products[0];
    state.selectedStation = "";
    state.selectedScriptId = null;
  }
  const productSelect = $("#production-product");
  productSelect.innerHTML = products.length
    ? products.map((model) => {
      const known = enabledProducts.find((item) => item.code === model);
      const label = known ? `${model} · ${known.name}` : (state.master.products.length ? `${model} · 不在主数据` : model);
      return `<option value="${escapeHtml(model)}">${escapeHtml(label)}</option>`;
    }).join("")
    : '<option value="">无可用产品</option>';
  productSelect.value = state.selectedProduct || "";
  const stations = productionStations();
  if (!stations.includes(state.selectedStation || "")) {
    const preferred = state.settings?.station_id || "";
    state.selectedStation = stations.includes(preferred) ? preferred : (stations[0] || "");
    state.selectedScriptId = null;
  }
  const stationSelect = $("#production-station");
  stationSelect.innerHTML = stations.length
    ? stations.map((code) => {
      const known = state.master.stations.find((item) => item.enabled && item.code === code);
      const label = code ? (known ? `${code} · ${known.name}` : code) : "不限工站";
      return `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`;
    }).join("")
    : '<option value="">—</option>';
  stationSelect.value = state.selectedStation || "";
  const matches = matchingScripts();
  if (!matches.some((item) => item.id === state.selectedScriptId)) {
    state.selectedScriptId = matches[0]?.id || null;
  }
  // 产品 + 工站选定后，若流程编排中已绑定该组合则直接带出测试流程
  $("#production-flow-field").hidden = matches.length === 0;
  $("#production-flow").innerHTML = matches.map((script) =>
    `<option value="${script.id}" ${script.id === state.selectedScriptId ? "selected" : ""}>${escapeHtml(script.name)} · v${script.version}</option>`).join("");
  const script = currentProductionScript();
  $("#banner-model").textContent = state.selectedProduct || "—";
  $("#banner-station").textContent = stations.length ? (state.selectedStation || "不限工站") : "—";
  // 选中的测试工站即执行工站（随扫码/开始传入后端），与流程绑定工站天然一致，无需再预警
  const banner = $("#script-banner");
  banner.classList.toggle("no-match", !script);
  if (script) {
    $("#banner-flow-name").textContent = script.name;
    $("#banner-flow-meta").textContent = `流程 v${script.version} · ${previewTestNodes(script.definition).length} 个测试项`;
  } else {
    $("#banner-flow-name").textContent = products.length ? "该产品在当前工站下没有绑定的测试流程" : "暂无可用流程，请在流程编排中创建并启用";
    $("#banner-flow-meta").textContent = "";
  }
}

function setProductionPickerDisabled(disabled) {
  ["#production-product", "#production-station", "#production-flow"].forEach((selector) => { $(selector).disabled = disabled; });
}

function currentProductionScript() {
  return state.scripts.find((script) => script.id === state.selectedScriptId) || null;
}

/** 渲染执行台选中流程的测试项预览（PENDING 状态）。 */
function renderTestItems(items) {
  $("#test-items").innerHTML = items.length ? items.map((item, index) => {
    const status = item.status || "PENDING";
    const message = item.message && !["测试通过", "正在测试"].includes(item.message) ? `<span class="item-message">${escapeHtml(item.message)}</span>` : "";
    return `<tr class="row-${status.toLowerCase()}">
      <td>${String(index + 1).padStart(2, "0")}</td>
      <td>${escapeHtml(item.name)}${message}</td>
      <td title="${escapeHtml(displayValue(item.actual))}">${escapeHtml(displayValue(item.actual))}</td>
      <td>${escapeHtml(displayValue(item.expected))}</td>
      <td>${status === "PENDING" ? "—" : formatDuration(item.duration_ms)}</td>
      <td>${statusBadge(status)}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="6" class="empty-row">请选择测试流程</td></tr>';
}

/** 展开流程定义中的全部测试节点（含条件/并行/循环/子流程），用于预览与计数。 */
function previewTestNodes(definition) {
  const subflows = definition.subflows || {};
  function collect(nodes, visited = new Set()) {
    return (nodes || []).flatMap((node) => {
      const type = node.type || "test";
      if (type === "test") return [node];
      if (type === "condition") return [...collect(node.then, visited), ...collect(node.else, visited)];
      if (type === "parallel") return (node.branches || []).flatMap((branch) => collect(branch.steps, visited));
      if (type === "loop") return collect(node.steps, visited);
      if (type === "subflow" && subflows[node.subflow] && !visited.has(node.subflow)) {
        const nextVisited = new Set(visited).add(node.subflow);
        return collect(subflows[node.subflow].steps, nextVisited);
      }
      return [];
    });
  }
  return collect(definition.steps);
}

/**
 * 未运行时的执行台预览：按选中流程渲染 PENDING 测试项与右侧产品信息；
 * 有正在进行的运行时直接返回（运行状态由 renderRun 负责）。
 */
function renderPreview() {
  const script = currentProductionScript();
  if (!script || state.run) return;
  renderTestItems(previewTestNodes(script.definition).map((step) => ({
    name: step.name, status: "PENDING", expected: step.actions.find((action) => action.type === "assert")?.expected,
  })));
  $("#unit-model").textContent = script.product_model;
  $("#unit-script-version").textContent = `v${script.version}`;
  $("#unit-sn").textContent = state.scan?.sn || "—";
  // 执行工站即执行台选中的测试工站；选择“不限工站”流程时回退为终端工站
  $("#unit-station").textContent = state.selectedStation || state.settings.station_id || "STATION-01";
  $("#unit-started").textContent = "—";
  $("#run-id-label").textContent = state.scan ? `${state.scan.sn} · 已绑定当前流程` : "等待扫描产品";
  $("#overall-badge").className = "badge badge-idle";
  $("#overall-badge").textContent = state.scan ? "BOUND" : "READY";
  $("#run-progress").style.width = "0";
  $("#run-progress").classList.remove("failed");
  $("#unit-status").className = "unit-status";
  $("#unit-status").innerHTML = '<div class="status-ring"><span>READY</span></div><p>等待开始测试</p>';
  $(".live-dot", $(".run-panel")).classList.remove("running");
  $("#start-test").disabled = !state.scan?.can_start;
  $("#run-mes-state").innerHTML = "<span>MES 状态</span><strong>等待生成报告</strong>";
}

/** 清空/下一台：停止轮询、清空 SN 与运行状态、解锁选择器并聚焦扫码框。 */
function resetConsole() {
  clearTimeout(state.pollTimer);
  state.run = null;
  state.scan = null;
  $("#sn-input").value = "";
  $("#sn-input").disabled = false;
  setProductionPickerDisabled(false);
  $("#scan-feedback").className = "scan-feedback";
  $("#scan-feedback").textContent = "SN 将与本次测试脚本快照和唯一报告绑定";
  $("#start-test").disabled = true;
  renderPreview();
  $("#sn-input").focus();
}

/** 扫码预检：把 SN、流程与所选测试工站提交后端，展示能否开始及原因；有活动运行时恢复该运行。 */
async function scanSn() {
  const sn = $("#sn-input").value.trim();
  const script = currentProductionScript();
  if (!sn) { toast("请先扫描或输入 SN", "error"); return; }
  if (!script) { toast("没有可用的测试流程", "error"); return; }
  const feedback = $("#scan-feedback");
  feedback.className = "scan-feedback";
  feedback.textContent = "正在校验 SN 与报告唯一性…";
  try {
    const result = await api("/api/test-runs/scan", { method: "POST", body: { sn, script_id: script.id, station: state.selectedStation || undefined } });
    state.scan = result;
    $("#sn-input").value = result.sn;
    feedback.className = `scan-feedback ${result.can_start ? "success" : "error"}`;
    feedback.innerHTML = escapeHtml(result.message) + (result.report ? ` <button class="text-button" data-report-id="${escapeHtml(result.report.id)}">查看已有报告</button>` : "");
    renderPreview();
    if (result.active_run_id) await resumeRun(result.active_run_id);
  } catch (error) {
    state.scan = null;
    feedback.className = "scan-feedback error";
    feedback.textContent = error.message;
    $("#start-test").disabled = true;
  }
}

/** 开始测试：创建运行（携带所选工站）、锁定选择器并开始轮询。 */
async function startTest() {
  if (!state.scan?.can_start || state.run?.status === "RUNNING") return;
  const script = currentProductionScript();
  const operator = $("#operator-input").value.trim();
  if (!operator) { toast("请填写测试操作员", "error"); return; }
  $("#start-test").disabled = true;
  try {
    state.run = await api("/api/test-runs", {
      method: "POST",
      body: { sn: state.scan.sn, script_id: script.id, operator, station: state.selectedStation || undefined },
    });
    $("#sn-input").disabled = true;
    setProductionPickerDisabled(true);
    renderRun();
    pollRun();
  } catch (error) {
    toast(error.message, "error");
    $("#start-test").disabled = false;
  }
}

/** 把后端运行状态完整渲染到页面：进度条、逐项结果、右侧面板、MES 状态与报告入口。 */
function renderRun() {
  const run = state.run;
  if (!run) return;
  renderTestItems(run.items);
  const completed = run.items.filter((item) => ["PASS", "FAIL"].includes(item.status)).length;
  const progress = run.items.length ? completed / run.items.length * 100 : 0;
  $("#run-progress").style.width = `${progress}%`;
  $("#run-progress").classList.toggle("failed", run.status === "FAIL");
  $("#run-id-label").textContent = run.id;
  $("#overall-badge").className = `badge badge-${run.status.toLowerCase()}`;
  $("#overall-badge").textContent = run.status;
  $("#unit-sn").textContent = run.sn;
  $("#unit-model").textContent = run.product_model;
  $("#unit-station").textContent = run.station;
  $("#unit-script-version").textContent = `v${run.script_version}`;
  $("#unit-started").textContent = formatTime(run.started_at);
  $("#operator-input").value = run.operator;
  const statusClass = run.status.toLowerCase();
  $("#unit-status").className = `unit-status ${statusClass}`;
  const statusText = run.status === "RUNNING" ? "TESTING" : run.status;
  const statusDescription = run.status === "RUNNING" ? "正在执行产品测试" : run.status === "PASS" ? "全部测试项通过" : "测试未通过，请查看失败项";
  $("#unit-status").innerHTML = `<div class="status-ring"><span>${statusText}</span></div><p>${statusDescription}</p>`;
  $(".live-dot", $(".run-panel")).classList.toggle("running", run.status === "RUNNING");
  $("#start-test").disabled = true;
  const feedback = $("#scan-feedback");
  if (run.status === "RUNNING") {
    feedback.className = "scan-feedback";
    feedback.textContent = `正在测试 ${run.sn}，请勿断开产品连接`;
  } else {
    feedback.className = `scan-feedback ${run.status === "PASS" ? "success" : "error"}`;
    feedback.innerHTML = `测试完成，报告 ${escapeHtml(run.report_no)} 已与 ${escapeHtml(run.sn)} 一对一绑定 ` +
      (run.report_id ? `<button class="text-button" data-report-id="${escapeHtml(run.report_id)}">查看报告</button>` : "");
  }
  $("#run-mes-state").innerHTML = run.report_id
    ? `<span>MES 状态</span><strong>${escapeHtml(mesStatusText(run.mes_status))}</strong>`
    : "<span>MES 状态</span><strong>报告生成中</strong>";
}

/** 轮询运行状态：执行中每 450ms 一次；完成时提示结果并刷新仪表盘；失败退避到 1500ms。 */
async function pollRun() {
  if (!state.run || state.run.status !== "RUNNING") return;
  try {
    const previousStatus = state.run.status;
    state.run = await api(`/api/test-runs/${state.run.id}`);
    renderRun();
    if (state.run.status === "RUNNING") {
      state.pollTimer = setTimeout(pollRun, 450);
    } else {
      toast(`${state.run.sn} 测试完成：${state.run.status}`, state.run.status === "PASS" ? "success" : "error");
      loadDashboard();
      if (previousStatus === "RUNNING" && $("#page-reports").classList.contains("active")) loadReports();
    }
  } catch (error) {
    toast(`状态刷新失败：${error.message}`, "error");
    state.pollTimer = setTimeout(pollRun, 1500);
  }
}

/** 恢复运行：页面刷新或扫码发现活动运行时，重建选中项与轮询。 */
async function resumeRun(runId) {
  clearTimeout(state.pollTimer);
  state.run = await api(`/api/test-runs/${runId}`);
  state.selectedScriptId = state.run.script_id;
  const boundScript = state.scripts.find((item) => item.id === state.run.script_id);
  state.selectedProduct = boundScript ? boundScript.product_model : state.run.product_model;
  state.selectedStation = boundScript ? (boundScript.station_code || "") : "";
  renderProductionPicker();
  $("#sn-input").value = state.run.sn;
  $("#sn-input").disabled = state.run.status === "RUNNING";
  setProductionPickerDisabled(state.run.status === "RUNNING");
  renderRun();
  if (state.run.status === "RUNNING") pollRun();
}

/** 各动作类型的默认参数（编辑器新建动作时使用）。 */
function defaultAction(type = "request") {
  const defaults = {
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

/** 新建测试项模板：仿真器协议 + 请求/断言两个默认动作。 */
function newStep(index = 0) {
  return {
    id: `item_${Date.now().toString(36).slice(-5)}_${index + 1}`,
    name: "新测试项", protocol: "simulator", timeout_ms: 5000, retries: 0, on_fail: "stop",
    actions: [defaultAction("request"), defaultAction("assert")],
  };
}

/** 生成 <option> 列表（编辑器下拉用）。 */
function optionTags(options, selected) {
  return Object.entries(options).map(([value, label]) => `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

/** 动作字段输入框 HTML：data-* 属性记录步骤/动作下标与字段名，供事件委托回写草稿。 */
function textActionField(si, ai, key, label, value, wide = false, type = "text", list = "") {
  return `<div class="action-field ${wide ? "wide" : ""}"><label>${escapeHtml(label)}</label><input type="${type}"${list ? ` list="${list}"` : ""} data-action-field data-s="${si}" data-a="${ai}" data-key="${key}" value="${escapeHtml(value ?? "")}"></div>`;
}

/** 按动作类型渲染其参数编辑表单（列表编辑器与节点检查器共用）。 */
function renderAction(action, si, ai) {
  let fields = "";
  if (["request", "send"].includes(action.type)) {
    fields += textActionField(si, ai, "data", "十进制数据 / 文本指令 / 支持 ${变量}", action.data, true);
    if (action.type === "request") fields += textActionField(si, ai, "save_as", "响应保存为", action.save_as);
  } else if (action.type === "receive") {
    fields += textActionField(si, ai, "save_as", "响应保存为", action.save_as);
    fields += textActionField(si, ai, "timeout_ms", "超时 ms", action.timeout_ms, false, "number");
    fields += textActionField(si, ai, "size", "字节数", action.size, false, "number");
  } else if (action.type === "execute") {
    fields += textActionField(si, ai, "operation", "操作名称", action.operation, true);
    fields += `<div class="action-field wide"><label>操作参数（JSON）</label><textarea data-action-field data-s="${si}" data-a="${ai}" data-key="params">${escapeHtml(JSON.stringify(action.params || {}))}</textarea></div>`;
    fields += textActionField(si, ai, "save_as", "结果保存为", action.save_as);
  } else if (action.type === "set") {
    fields += textActionField(si, ai, "variable", "变量名", action.variable);
    fields += textActionField(si, ai, "value", "变量值 / 支持模板", action.value, true);
  } else if (action.type === "assert") {
    fields += textActionField(si, ai, "actual", "实际值 / 变量路径（可免 ${}）", action.actual, true);
    fields += `<div class="action-field"><label>判断方式</label><select data-action-field data-s="${si}" data-a="${ai}" data-key="operator">${optionTags(operatorLabels, action.operator)}</select></div>`;
    if (!["truthy", "exists"].includes(action.operator)) {
      const expected = Array.isArray(action.expected) ? action.expected.join(", ") : action.expected;
      fields += textActionField(si, ai, "expected", action.operator === "between" ? "下限, 上限" : "期望值", expected, true);
    }
    fields += textActionField(si, ai, "message", "失败提示（可选）", action.message, true);
  } else if (action.type === "sleep") {
    fields += textActionField(si, ai, "duration_ms", "延时时长 ms", action.duration_ms, false, "number");
  } else if (action.type === "flash") {
    const options = action.options || {};
    fields += textActionField(si, ai, "firmware", "固件名称（候选来自固件仓）/ 支持 ${变量}", action.firmware, true, "text", "firmware-options");
    fields += textActionField(si, ai, "target", "目标槽位（可选）", action.target);
    fields += textActionField(si, ai, "save_as", "结果保存为（可断言 ${变量.ok}）", action.save_as);
    fields += textActionField(si, ai, "timeout_ms", "烧录超时 ms", options.timeout_ms ?? 60000, false, "number");
    for (const [key, label] of [["erase", "烧录前擦除"], ["verify", "烧录后校验"], ["reset", "烧录后复位"]]) {
      fields += `<div class="action-field"><label>${label}</label><select data-action-field data-s="${si}" data-a="${ai}" data-key="${key}">${optionTags({ true: "是", false: "否" }, String(options[key] ?? true))}</select></div>`;
    }
  }
  return `<div class="action-row">
    <div class="action-type"><label>动作 ${ai + 1}</label><select data-action-type data-s="${si}" data-a="${ai}">${optionTags(actionLabels, action.type)}</select></div>
    ${fields}<button class="icon-button danger" title="删除动作" data-delete-action data-s="${si}" data-a="${ai}">×</button>
  </div>`;
}

/** 切换编辑器模式：图形画布（React Flow）/ 步骤列表。 */
function setEditorMode(mode) {
  state.editorMode = mode;
  $$("[data-editor-mode]").forEach((button) => button.classList.toggle("active", button.dataset.editorMode === mode));
  $("#react-flow-editor").hidden = mode !== "visual";
  $("#steps-container").hidden = mode !== "list";
  if (mode === "visual") window.PilotFlowEditor?.setDefinition(state.draft);
}

/** 动作的一行摘要（画布节点上显示）。 */
function actionSummary(action) {
  if (["request", "send"].includes(action.type)) return action.save_as ? `${action.data} → ${action.save_as}` : action.data;
  if (action.type === "receive") return `接收至 ${action.save_as} · ${action.timeout_ms} ms`;
  if (action.type === "execute") return `${action.operation} ${JSON.stringify(action.params || {})}`;
  if (action.type === "set") return `${action.variable} = ${displayValue(action.value)}`;
  if (action.type === "assert") {
    const expected = ["truthy", "exists"].includes(action.operator) ? "" : ` ${displayValue(action.expected)}`;
    return `${displayValue(action.actual)} ${operatorLabels[action.operator] || action.operator}${expected}`;
  }
  if (action.type === "sleep") return `等待 ${action.duration_ms} ms`;
  if (action.type === "flash") return `烧录 ${action.firmware || "未选择固件"}${action.save_as ? ` → ${action.save_as}` : ""}`;
  return action.type;
}

/** 选中画布节点（测试项或动作）并刷新检查器。 */
function selectFlowNode(stepIndex, actionIndex = null) {
  state.selectedStep = stepIndex;
  state.selectedAction = actionIndex;
  renderVisualEditor();
}

/** 节点检查器：显示选中测试项的属性表单或选中动作的参数表单。 */
function renderNodeInspector() {
  const inspector = $("#node-inspector");
  const step = state.draft?.steps?.[state.selectedStep];
  if (!step) {
    inspector.innerHTML = '<div class="inspector-empty"><strong>未选择节点</strong><p>从节点库拖入一个测试项，或点击画布中的节点编辑属性。</p></div>';
    return;
  }
  const si = state.selectedStep;
  const action = state.selectedAction === null ? null : step.actions[state.selectedAction];
  if (action) {
    inspector.innerHTML = `<div class="inspector-header"><span class="eyebrow">ACTION ${state.selectedAction + 1}</span><h3>${escapeHtml(actionLabels[action.type])}</h3><p>${escapeHtml(step.name)}</p></div><div class="inspector-action">${renderAction(action, si, state.selectedAction)}</div><p class="inspector-tip">动作在测试项内按顺序执行。可拖到其他动作前后或其他测试项中。</p>`;
    return;
  }
  inspector.innerHTML = `<div class="inspector-header"><span class="eyebrow">STEP ${si + 1}</span><h3>测试项属性</h3><p>${escapeHtml(step.id)}</p></div>
    <div class="inspector-fields">
      <div class="field"><label>步骤 ID</label><input data-step-field="id" data-s="${si}" value="${escapeHtml(step.id)}"></div>
      <div class="field"><label>测试项名称</label><input data-step-field="name" data-s="${si}" value="${escapeHtml(step.name)}"></div>
      <div class="field"><label>通讯协议</label><select data-step-field="protocol" data-s="${si}">${optionTags(protocolSymbols, step.protocol)}</select></div>
      <div class="field"><label>超时（毫秒）</label><input type="number" data-step-field="timeout_ms" data-s="${si}" value="${escapeHtml(step.timeout_ms)}"></div>
      <div class="field"><label>失败后额外重试</label><input type="number" min="0" max="10" data-step-field="retries" data-s="${si}" value="${escapeHtml(step.retries)}"></div>
      <div class="field"><label>失败策略</label><select data-step-field="on_fail" data-s="${si}">${optionTags({ stop: "停止后续", continue: "继续下一项" }, step.on_fail)}</select></div>
    </div>
    <div class="inspector-actions"><button class="btn btn-small btn-secondary" data-step-command="add-action" data-s="${si}">+ 添加断言</button><button class="btn btn-small btn-ghost danger-text" data-step-command="delete" data-s="${si}">删除测试项</button></div>
    <p class="inspector-tip">拖动此节点标题可调整执行顺序。点击内部动作可编辑通讯数据和判定条件。</p>`;
}

/** 渲染旧版可视化编辑器（节点卡片 + 拖拽排序）；新版画布见 PilotFlowEditor。 */
function renderVisualEditor() {
  if (!$("#visual-editor")) return;
  const steps = state.draft?.steps || [];
  if (!steps.length) {
    state.selectedStep = -1;
    state.selectedAction = null;
  } else {
    if (state.selectedStep < 0 || state.selectedStep >= steps.length) state.selectedStep = 0;
    const actions = steps[state.selectedStep].actions || [];
    if (state.selectedAction !== null && state.selectedAction >= actions.length) state.selectedAction = null;
  }
  $("#palette-actions").innerHTML = Object.entries(actionLabels).map(([type, label]) => `
    <div class="palette-action" draggable="true" data-palette-action="${escapeHtml(type)}"><span class="palette-symbol">${escapeHtml(actionSymbols[type])}</span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(type)}</small></div>`).join("");
  $("#flow-steps").innerHTML = steps.map((step, si) => `
    <div class="flow-step-wrap">
      <article class="flow-node ${si === state.selectedStep ? "selected" : ""}" data-flow-step="${si}" data-drop-step="${si}">
        <header class="flow-node-header" draggable="true" data-step-drag="${si}" title="拖动调整测试项顺序"><span class="drag-handle">⋮⋮</span><span class="flow-node-number">${si + 1}</span><div><strong class="flow-node-title">${escapeHtml(step.name)}</strong><small>${escapeHtml(step.id)}</small></div><span class="flow-protocol">${escapeHtml(step.protocol)}</span></header>
        <div class="flow-node-meta"><span>超时 ${escapeHtml(step.timeout_ms)} ms</span><span>重试 ${escapeHtml(step.retries)}</span><span class="${step.on_fail === "stop" ? "fail-stop" : "fail-continue"}">FAIL ${step.on_fail === "stop" ? "停止后续" : "继续下一项"}</span></div>
        <div class="flow-actions">${(step.actions || []).map((action, ai) => `
          <button type="button" class="flow-action ${si === state.selectedStep && ai === state.selectedAction ? "selected" : ""}" draggable="true" data-action-node data-action-drag data-drop-action="${ai}" data-s="${si}" data-a="${ai}" title="拖动调整动作顺序，点击编辑"><span class="flow-action-type">${escapeHtml(actionSymbols[action.type] || action.type)}</span><span><strong>${ai + 1}. ${escapeHtml(actionLabels[action.type] || action.type)}</strong><small>${escapeHtml(actionSummary(action))}</small></span></button>`).join("")}</div>
        <div class="flow-drop-hint">拖动作到这里</div>
      </article>
      <div class="flow-edge"><i></i><span>${si === steps.length - 1 ? "写入测试结果" : "PASS 下一项"}</span></div>
    </div>`).join("");
  renderNodeInspector();
}

/** 编辑器输入事件（委托）：步骤字段直接写入草稿，动作字段走 updateActionField。 */
function handleEditorInput(event) {
  const stepInput = event.target.closest("[data-step-field]");
  if (stepInput) {
    const step = state.draft.steps[Number(stepInput.dataset.s)];
    if (step) step[stepInput.dataset.stepField] = ["timeout_ms", "retries"].includes(stepInput.dataset.stepField) ? Number(stepInput.value) : stepInput.value;
    return;
  }
  if (event.target.matches("[data-action-field]")) updateActionField(event.target);
}

/** 编辑器 change 事件：下拉类字段变更后按需重渲染；切换动作类型时整体替换默认参数。 */
function handleEditorChange(event) {
  if (event.target.closest("[data-step-field]")) renderVisualEditor();
  if (event.target.matches("[data-action-field]")) {
    updateActionField(event.target);
    if (event.target.dataset.key !== "operator") renderVisualEditor();
  }
  if (event.target.matches("[data-action-type]")) {
    const si = Number(event.target.dataset.s);
    const ai = Number(event.target.dataset.a);
    state.draft.steps[si].actions[ai] = defaultAction(event.target.value);
    state.selectedStep = si;
    state.selectedAction = ai;
    renderSteps();
  }
}

/** 编辑器命令按钮（委托）：删除/上下移测试项、添加动作，统一经 FlowModel 不可变更新。 */
function handleEditorCommand(event) {
  const deleteButton = event.target.closest("[data-delete-action]");
  if (deleteButton) {
    const si = Number(deleteButton.dataset.s);
    state.draft = FlowModel.removeAction(state.draft, si, Number(deleteButton.dataset.a));
    state.selectedStep = si;
    state.selectedAction = null;
    renderSteps();
    return;
  }
  const button = event.target.closest("[data-step-command]");
  if (!button) return;
  const index = Number(button.dataset.s);
  const command = button.dataset.stepCommand;
  if (command === "delete") {
    state.draft = FlowModel.removeStep(state.draft, index);
    state.selectedStep = Math.max(0, index - 1);
    state.selectedAction = null;
  }
  if (command === "add-action") {
    state.draft = FlowModel.insertAction(state.draft, index, defaultAction("assert"));
    state.selectedStep = index;
    state.selectedAction = state.draft.steps[index].actions.length - 1;
  }
  if (command === "up" && index > 0) {
    state.draft = FlowModel.moveStep(state.draft, index, index - 1);
    state.selectedStep = index - 1;
    state.selectedAction = null;
  }
  if (command === "down" && index < state.draft.steps.length - 1) {
    state.draft = FlowModel.moveStep(state.draft, index, index + 2);
    state.selectedStep = index + 1;
    state.selectedAction = null;
  }
  renderSteps();
}

/** 渲染步骤列表编辑器 + 同步高级 JSON 文本框 + 刷新画布。 */
function renderSteps() {
  const steps = state.draft?.steps || [];
  $("#steps-container").innerHTML = steps.length ? steps.map((step, si) => `
    <section class="step-card">
      <div class="step-header"><span class="step-number">${si + 1}</span><strong>${escapeHtml(step.name)}</strong><span class="step-protocol-label">${escapeHtml(step.protocol)}</span>
        <div class="step-order"><button class="icon-button" title="上移" data-step-command="up" data-s="${si}" ${si === 0 ? "disabled" : ""}>↑</button><button class="icon-button" title="下移" data-step-command="down" data-s="${si}" ${si === steps.length - 1 ? "disabled" : ""}>↓</button><button class="icon-button danger" title="删除测试项" data-step-command="delete" data-s="${si}">×</button></div>
      </div>
      <div class="step-fields">
        <div class="field"><label>步骤 ID</label><input data-step-field="id" data-s="${si}" value="${escapeHtml(step.id)}"></div>
        <div class="field"><label>测试项名称</label><input data-step-field="name" data-s="${si}" value="${escapeHtml(step.name)}"></div>
        <div class="field"><label>通讯协议</label><select data-step-field="protocol" data-s="${si}">${optionTags(protocolSymbols, step.protocol)}</select></div>
        <div class="field"><label>超时 ms</label><input type="number" data-step-field="timeout_ms" data-s="${si}" value="${escapeHtml(step.timeout_ms)}"></div>
        <div class="field"><label>失败重试</label><input type="number" min="0" max="10" data-step-field="retries" data-s="${si}" value="${escapeHtml(step.retries)}"></div>
        <div class="field"><label>失败策略</label><select data-step-field="on_fail" data-s="${si}">${optionTags({ stop: "停止后续", continue: "继续测试" }, step.on_fail)}</select></div>
      </div>
      <div class="actions-label"><strong>逻辑动作（依次执行）</strong><button class="text-button" data-step-command="add-action" data-s="${si}">+ 添加动作</button></div>
      <div class="action-list">${(step.actions || []).map((action, ai) => renderAction(action, si, ai)).join("")}</div>
    </section>`).join("") : '<div class="validation error">尚无测试项，请点击“添加测试项”。</div>';
  $("#script-json").value = JSON.stringify(state.draft, null, 2);
  renderVisualEditor();
  window.PilotFlowEditor?.setDefinition(state.draft);
}

/** 打开已有流程为编辑草稿（深拷贝，避免改动影响列表中的原始定义）。 */
function openScript(scriptId) {
  const script = state.scripts.find((item) => item.id === Number(scriptId));
  if (!script) return;
  state.draftId = script.id;
  state.draftVersion = script.version;
  state.draft = clone(script.definition);
  state.selectedStep = 0;
  state.selectedAction = null;
  renderScriptLibrary();
}

/** 新建流程草稿：默认关联首个启用产品（或调用方指定的产品），含一个空白测试项。 */
function newScriptDraft(productCode = "") {
  const firstProduct = state.master.products.find((item) => item.enabled);
  return { schema_version: 1, name: "新产品测试流程", product_model: productCode || (firstProduct ? firstProduct.code : ""), station_code: "", variables: {}, steps: [newStep()] };
}

// 流程库按产品索引：启用中的主数据产品在前（含未建流程的产品，便于按产品管理），
// 流程绑定但不在启用主数据中的型号附加在末尾
function scriptLibraryGroups() {
  const byProduct = new Map();
  state.scripts.forEach((script) => {
    const key = script.product_model || "";
    if (!byProduct.has(key)) byProduct.set(key, []);
    byProduct.get(key).push(script);
  });
  const groups = state.master.products
    .filter((item) => item.enabled)
    .map((item) => {
      const scripts = byProduct.get(item.code) || [];
      byProduct.delete(item.code);
      return { code: item.code, name: item.name, scripts };
    });
  const masterLoaded = state.master.products.length > 0;
  byProduct.forEach((scripts, code) => {
    const known = state.master.products.find((item) => item.code === code);
    groups.push({ code, name: known ? `${known.name}（已停用）` : (masterLoaded ? "不在主数据" : ""), scripts });
  });
  return groups;
}

function renderScriptLibrary() {
  if (!state.draft && state.scripts.length) {
    const preferred = state.scripts.find((script) => script.id === state.selectedScriptId) || state.scripts[0];
    state.draftId = preferred.id;
    state.draftVersion = preferred.version;
    state.draft = clone(preferred.definition);
  }
  $("#script-count").textContent = state.scripts.length;
  const groups = scriptLibraryGroups();
  $("#script-list").innerHTML = groups.length ? groups.map((group) => `
    <details class="script-group" ${group.scripts.length ? "open" : ""}>
      <summary>
        <span class="script-group-code">${escapeHtml(group.code || "未关联产品")}</span>
        ${group.name ? `<small>${escapeHtml(group.name)}</small>` : ""}
        <span class="count-badge">${group.scripts.length}</span>
        <button type="button" class="btn btn-small btn-ghost script-group-add" data-new-script-for="${escapeHtml(group.code)}" title="为该产品新建流程">+ 新建</button>
      </summary>
      <div class="script-group-items">${group.scripts.map((script) => `
        <button class="script-list-item ${script.id === state.draftId ? "active" : ""} ${script.enabled ? "" : "disabled"}" data-script-select="${script.id}">
          <strong>${escapeHtml(script.name)}</strong><span>${script.station_code ? escapeHtml(script.station_code) : "不限工站"} · ${script.definition.steps.length} 个测试项</span><small>VERSION ${script.version}${script.enabled ? "" : " · 已停用"}</small>
        </button>`).join("") || '<div class="script-group-empty">该产品暂无流程</div>'}
      </div>
    </details>`).join("") : '<div class="empty-row">暂无流程，请新建</div>';
  if (!state.draft) state.draft = newScriptDraft();
  $("#draft-name").value = state.draft.name || "";
  $("#draft-retests").value = state.draft.max_retests ?? "";
  $("#draft-version").textContent = state.draftVersion ? `v${state.draftVersion}` : "未保存";
  $("#validation-results").innerHTML = "";
  $("#script-json").value = JSON.stringify(state.draft, null, 2);
  renderMasterSelects();
  renderSteps();
}

function createNewScript(productCode = "") {
  state.draftId = null;
  state.draftVersion = null;
  state.draft = newScriptDraft(productCode);
  state.selectedStep = 0;
  state.selectedAction = null;
  renderScriptLibrary();
  $("#draft-name").focus();
}

/** 编译校验草稿并在页面上展示结果；返回是否通过。 */
async function validateScript(showSuccess = true) {
  try {
    const result = await api("/api/scripts/validate", { method: "POST", body: { definition: state.draft } });
    $("#validation-results").innerHTML = result.valid
      ? `<div class="validation success">${escapeHtml(result.message)}，脚本可保存并由后端执行。</div>`
      : `<div class="validation error"><strong>脚本校验未通过</strong><ul>${result.errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul></div>`;
    if (showSuccess && result.valid) toast("脚本编译校验通过", "success");
    return result.valid;
  } catch (error) {
    $("#validation-results").innerHTML = `<div class="validation error">${escapeHtml(error.message)}</div>`;
    return false;
  }
}

/** 保存流程：先编译校验，再按 draftId 决定新建/更新；保存后刷新版本与执行台。 */
async function saveScript() {
  if (!await validateScript(false)) { toast("请先修正脚本校验错误", "error"); return; }
  try {
    const isNew = !state.draftId;
    const saved = await api(isNew ? "/api/scripts" : `/api/scripts/${state.draftId}`, {
      method: isNew ? "POST" : "PUT", body: { definition: state.draft },
    });
    state.draftId = saved.id;
    state.draftVersion = saved.version;
    state.draft = clone(saved.definition);
    state.selectedScriptId = saved.id;
    await loadScripts();
    renderScriptLibrary();
    renderPreview();
    toast(`流程已保存为 v${saved.version}`, "success");
  } catch (error) { toast(error.message, "error"); }
}

/** 动作字段回写草稿：按字段类型转换数字/JSON/断言期望值，烧录选项单独处理。 */
function updateActionField(element) {
  const step = state.draft.steps[Number(element.dataset.s)];
  const action = step?.actions[Number(element.dataset.a)];
  if (!action) return;
  const key = element.dataset.key;
  let value = element.value;
  if (action.type === "flash" && ["erase", "verify", "reset", "timeout_ms"].includes(key)) {
    action.options ||= {};
    action.options[key] = key === "timeout_ms" ? Number(value) : value === "true";
    return;
  }
  if (["timeout_ms", "size", "duration_ms"].includes(key)) value = Number(value);
  if (key === "params") {
    try { value = JSON.parse(value); } catch { return; }
  }
  if (key === "expected") {
    if (action.operator === "between") value = value.split(",").map((part) => Number(part.trim()));
    else if (["gt", "gte", "lt", "lte"].includes(action.operator)) value = Number(value);
    else value = parseSmartValue(value);
  }
  if (key === "value") value = parseSmartValue(value);
  action[key] = value;
  if (key === "operator") {
    if (["truthy", "exists"].includes(value)) delete action.expected;
    else if (action.expected === undefined) action.expected = value === "between" ? [0, 1] : "PASS";
    renderSteps();
  }
}

/** 清除旧版可视化编辑器的拖拽高亮。 */
function clearDragStyles() {
  $$(".drag-over", $("#visual-editor")).forEach((element) => element.classList.remove("drag-over"));
}

/** 绑定旧版可视化编辑器的拖拽与点击事件（节点/动作拖放排序、点击选中）。 */
function bindVisualEditor() {
  const visual = $("#visual-editor");
  if (!visual) return;
  visual.addEventListener("dragstart", (event) => {
    const paletteStep = event.target.closest("[data-palette-step]");
    const paletteAction = event.target.closest("[data-palette-action]");
    const action = event.target.closest("[data-action-drag]");
    const step = event.target.closest("[data-step-drag]");
    if (paletteStep) state.drag = { kind: "palette-step" };
    else if (paletteAction) state.drag = { kind: "palette-action", actionType: paletteAction.dataset.paletteAction };
    else if (action) state.drag = { kind: "action", fromStep: Number(action.dataset.s), fromAction: Number(action.dataset.a) };
    else if (step) state.drag = { kind: "step", from: Number(step.dataset.stepDrag) };
    else return;
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = state.drag.kind.startsWith("palette") ? "copy" : "move";
      event.dataTransfer.setData("text/plain", state.drag.kind);
    }
  });
  visual.addEventListener("dragend", () => {
    state.drag = null;
    clearDragStyles();
  });
  visual.addEventListener("dragover", (event) => {
    if (!state.drag) return;
    const actionTarget = event.target.closest("[data-drop-action]");
    const stepTarget = event.target.closest("[data-drop-step]");
    const endTarget = event.target.closest("[data-drop-end]");
    const actionDrag = ["palette-action", "action"].includes(state.drag.kind);
    if ((!actionTarget && !stepTarget && !endTarget) || (endTarget && actionDrag)) return;
    event.preventDefault();
    clearDragStyles();
    (actionTarget || stepTarget || endTarget).classList.add("drag-over");
    if (event.dataTransfer) event.dataTransfer.dropEffect = state.drag.kind.startsWith("palette") ? "copy" : "move";
  });
  visual.addEventListener("drop", (event) => {
    if (!state.drag) return;
    const drag = state.drag;
    const actionTarget = event.target.closest("[data-drop-action]");
    const stepTarget = event.target.closest("[data-drop-step]");
    const endTarget = event.target.closest("[data-drop-end]");
    if (!actionTarget && !stepTarget && !endTarget) return;
    event.preventDefault();
    event.stopPropagation();
    state.drag = null;
    clearDragStyles();
    try {
      if (actionTarget && ["palette-action", "action"].includes(drag.kind)) {
        event.stopPropagation();
        const si = Number(actionTarget.dataset.s);
        const rect = actionTarget.getBoundingClientRect();
        const offset = event.clientY > rect.top + rect.height / 2 ? 1 : 0;
        const ai = Number(actionTarget.dataset.dropAction) + offset;
        state.draft = drag.kind === "palette-action"
          ? FlowModel.insertAction(state.draft, si, defaultAction(drag.actionType), ai)
          : FlowModel.moveAction(state.draft, drag.fromStep, drag.fromAction, si, ai);
        state.selectedStep = si;
        state.selectedAction = null;
      } else if (stepTarget) {
        const si = Number(stepTarget.dataset.dropStep);
        if (["palette-action", "action"].includes(drag.kind)) {
          const ai = state.draft.steps[si].actions.length;
          state.draft = drag.kind === "palette-action"
            ? FlowModel.insertAction(state.draft, si, defaultAction(drag.actionType), ai)
            : FlowModel.moveAction(state.draft, drag.fromStep, drag.fromAction, si, ai);
        } else {
          const rect = stepTarget.getBoundingClientRect();
          const offset = event.clientY > rect.top + rect.height / 2 ? 1 : 0;
          const target = si + offset;
          state.draft = drag.kind === "palette-step"
            ? FlowModel.insertStep(state.draft, newStep(target), target)
            : FlowModel.moveStep(state.draft, drag.from, target);
          state.selectedStep = drag.kind === "step" && drag.from < target ? target - 1 : target;
        }
        state.selectedAction = null;
      } else if (endTarget && ["palette-step", "step"].includes(drag.kind)) {
        const target = state.draft.steps.length;
        state.draft = drag.kind === "palette-step"
          ? FlowModel.insertStep(state.draft, newStep(target), target)
          : FlowModel.moveStep(state.draft, drag.from, target);
        state.selectedStep = state.draft.steps.length - 1;
        state.selectedAction = null;
      }
      renderSteps();
    } catch (error) {
      toast(`无法放置节点：${error.message}`, "error");
    }
  });
  visual.addEventListener("click", (event) => {
    const paletteStep = event.target.closest("[data-palette-step]");
    const paletteAction = event.target.closest("[data-palette-action]");
    if (paletteStep) {
      const index = state.draft.steps.length;
      state.draft = FlowModel.insertStep(state.draft, newStep(index), index);
      state.selectedStep = index;
      state.selectedAction = null;
      renderSteps();
      return;
    }
    if (paletteAction) {
      if (state.selectedStep < 0 || !state.draft.steps.length) {
        toast("请先添加一个测试项", "error");
        return;
      }
      const si = state.selectedStep;
      state.draft = FlowModel.insertAction(state.draft, si, defaultAction(paletteAction.dataset.paletteAction));
      state.selectedAction = state.draft.steps[si].actions.length - 1;
      renderSteps();
      return;
    }
    const action = event.target.closest("[data-action-node]");
    if (action) {
      selectFlowNode(Number(action.dataset.s), Number(action.dataset.a));
      return;
    }
    const step = event.target.closest("[data-flow-step]");
    if (step) selectFlowNode(Number(step.dataset.flowStep));
  });
}

/** 绑定流程编排页事件：新建/校验/保存、编辑器模式切换、元信息输入、JSON 应用等。 */
function bindScriptEditor() {
  $("#new-script").addEventListener("click", () => createNewScript());
  $("#validate-script").addEventListener("click", () => validateScript());
  $("#save-script").addEventListener("click", saveScript);
  $("#add-step").addEventListener("click", () => {
    const index = state.draft.steps.length;
    state.draft = FlowModel.insertStep(state.draft, newStep(index), index);
    state.selectedStep = index;
    state.selectedAction = null;
    renderSteps();
  });
  $$("[data-editor-mode]").forEach((button) => button.addEventListener("click", () => setEditorMode(button.dataset.editorMode)));
  $("#script-list").addEventListener("click", (event) => {
    const createButton = event.target.closest("[data-new-script-for]");
    if (createButton) {
      event.preventDefault();
      createNewScript(createButton.dataset.newScriptFor);
      return;
    }
    const button = event.target.closest("[data-script-select]");
    if (button) openScript(button.dataset.scriptSelect);
  });
  [$("#draft-name"), $("#draft-model"), $("#draft-station"), $("#draft-retests")].forEach((input) => input.addEventListener("input", () => {
    // 复测次数是可选数字字段：留空表示跟随工位策略（从流程定义中移除该键），填写时存为整数
    if (input.dataset.draftField === "max_retests") {
      if (input.value === "") delete state.draft.max_retests;
      else state.draft.max_retests = Number(input.value);
      return;
    }
    state.draft[input.dataset.draftField] = input.value;
  }));
  [$("#steps-container"), $("#node-inspector")].filter(Boolean).forEach((container) => {
    container.addEventListener("input", handleEditorInput);
    container.addEventListener("change", handleEditorChange);
    container.addEventListener("click", handleEditorCommand);
  });
  bindVisualEditor();
  $("#script-json").closest("details").addEventListener("toggle", (event) => {
    if (event.target.open) $("#script-json").value = JSON.stringify(state.draft, null, 2);
  });
  $("#apply-script-json").addEventListener("click", () => {
    try {
      const parsed = parseJsonField($("#script-json").value, "流程");
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("流程根节点必须是 JSON 对象");
      state.draft = parsed;
      state.draft.steps ||= [];
      state.selectedStep = state.draft.steps.length ? 0 : -1;
      state.selectedAction = null;
      $("#draft-name").value = state.draft.name || "";
      $("#draft-retests").value = state.draft.max_retests ?? "";
      renderMasterSelects();
      renderSteps();
      toast("JSON 已应用，请执行编译校验", "success");
    } catch (error) { toast(error.message, "error"); }
  });
}

/** 加载协议目录与已保存配置。 */
async function loadProtocols() {
  try {
    state.protocols = await api("/api/protocols");
    if (!state.protocols.some((item) => item.name === state.selectedProtocol)) state.selectedProtocol = state.protocols[0]?.name;
    renderProtocols();
  } catch (error) { toast(error.message, "error"); }
}

/** 当前选中的协议对象。 */
function selectedProtocol() {
  return state.protocols.find((item) => item.name === state.selectedProtocol);
}

/** 协议表单字段联动：depends_on 不满足时隐藏字段（如选“十进制”才显示数值类型）。 */
function updateProtocolDependencies() {
  $$('[data-config-depends-key]', $("#protocol-form")).forEach((element) => {
    const controller = $(`[data-config-key="${element.dataset.configDependsKey}"]`, $("#protocol-form"));
    element.hidden = controller?.value !== element.dataset.configDependsValue;
  });
}

/** 渲染通讯配置页：左侧协议导航 + 右侧按分组（连接/下发编码/响应解析）的参数表单。 */
function renderProtocols() {
  $("#protocol-tabs").innerHTML = state.protocols.map((protocol) => `
    <button class="protocol-tab ${protocol.name === state.selectedProtocol ? "active" : ""}" data-protocol="${escapeHtml(protocol.name)}">
      <span class="protocol-symbol">${escapeHtml(protocolSymbols[protocol.name])}</span><span><strong>${escapeHtml(protocol.label)}</strong><small>${protocol.driver_available ? "驱动可用" : "需安装硬件依赖"}</small></span>
    </button>`).join("");
  const protocol = selectedProtocol();
  if (!protocol) return;
  $("#protocol-title").textContent = protocol.label;
  $("#protocol-description").textContent = protocol.description;
  $("#driver-badge").className = `badge ${protocol.driver_available ? "badge-pass" : "badge-pending"}`;
  $("#driver-badge").textContent = protocol.driver_available ? "DRIVER READY" : "DRIVER MISSING";
  const groups = { connection: [], downlink: [], uplink: [] };
  protocol.fields.forEach((field) => (groups[field.group || "connection"] ||= []).push(field));
  const groupMeta = {
    connection: ["连接参数", "硬件地址、通讯速率与驱动参数"],
    downlink: ["下发编码", "低代码页面输入十进制数值，这里决定实际写入设备的字节"],
    uplink: ["响应解析", "把设备返回的字节转换为断言可直接使用的数值或文本"],
  };
  $("#protocol-form").innerHTML = Object.entries(groups).filter(([, fields]) => fields.length).map(([group, fields]) => `
    <section class="config-section"><div class="config-section-title"><h3>${groupMeta[group][0]}</h3><p>${groupMeta[group][1]}</p></div><div class="config-section-fields">${fields.map((field) => {
      let value = protocol.config[field.key] ?? (field.key === "receive_format" ? protocol.config.response_format : undefined) ?? field.default;
      if (field.type === "number" && typeof value === "string" && /^0x[0-9a-f]+$/i.test(value)) value = Number.parseInt(value, 16);
      const dependency = field.depends_on?.key ? `data-config-depends-key="${escapeHtml(field.depends_on.key)}" data-config-depends-value="${escapeHtml(field.depends_on.value)}"` : "";
      const help = field.help ? `<small>${escapeHtml(field.help)}</small>` : "";
      if (field.type === "checkbox") {
        return `<div class="checkbox-field"><input type="checkbox" id="config-${escapeHtml(field.key)}" data-config-key="${escapeHtml(field.key)}" data-kind="checkbox" ${value ? "checked" : ""}><label for="config-${escapeHtml(field.key)}">${escapeHtml(field.label)}</label></div>`;
      }
      if (field.type === "select") {
        return `<div class="field" ${dependency}><label>${escapeHtml(field.label)}</label><select data-config-key="${escapeHtml(field.key)}" data-kind="select">${field.options.map((option) => `<option value="${escapeHtml(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>${help}</div>`;
      }
      if (field.type === "json") {
        return `<div class="field span-2" ${dependency}><label>${escapeHtml(field.label)}</label><textarea class="code-input" rows="5" data-config-key="${escapeHtml(field.key)}" data-kind="json">${escapeHtml(JSON.stringify(value || {}, null, 2))}</textarea>${help}</div>`;
      }
      const type = ["number", "password"].includes(field.type) ? field.type : "text";
      const numberOptions = type === "number" ? 'step="any" inputmode="decimal"' : "";
      return `<div class="field" ${dependency}><label>${escapeHtml(field.label)}${field.required ? " *" : ""}</label><input type="${type}" ${numberOptions} data-config-key="${escapeHtml(field.key)}" data-kind="${type}" value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(field.placeholder || "")}">${help}</div>`;
    }).join("")}</div></section>`).join("");
  updateProtocolDependencies();
  $("#protocol-json").value = JSON.stringify(protocol.config, null, 2);
  $("#protocol-test-result").textContent = "";
  $("#protocol-test-result").className = "";
}

/** 收集协议表单 + 高级 JSON 为最终配置对象（表单字段覆盖 JSON 同名键）。 */
function collectProtocolConfig() {
  const protocol = selectedProtocol();
  const config = parseJsonField($("#protocol-json").value, "通讯配置");
  if (!config || Array.isArray(config) || typeof config !== "object") throw new Error("通讯配置根节点必须是 JSON 对象");
  $$("[data-config-key]", $("#protocol-form")).forEach((input) => {
    const key = input.dataset.configKey;
    if (input.dataset.kind === "checkbox") config[key] = input.checked;
    else if (input.dataset.kind === "number") config[key] = Number(input.value);
    else if (input.dataset.kind === "json") config[key] = parseJsonField(input.value, key);
    else config[key] = input.value;
  });
  return config;
}

/** 保存当前协议配置。 */
async function saveProtocol(showToast = true) {
  const config = collectProtocolConfig();
  const name = state.selectedProtocol;
  const result = await api(`/api/protocols/${name}`, { method: "PUT", body: { config } });
  selectedProtocol().config = config;
  $("#protocol-json").value = JSON.stringify(config, null, 2);
  if (showToast) toast("通讯配置已保存", "success");
  return result.config;
}

/** 测试连接：先保存配置再调用后端探测。 */
async function testProtocol() {
  const result = $("#protocol-test-result");
  result.className = "";
  result.textContent = "正在连接…";
  try {
    const config = await saveProtocol(false);
    const response = await api(`/api/protocols/${state.selectedProtocol}/test`, { method: "POST", body: { config } });
    result.className = "success";
    result.textContent = response.message;
    toast(response.message, "success");
  } catch (error) {
    result.className = "error";
    result.textContent = error.message;
    toast(error.message, "error");
  }
}

/** 加载固件仓、烧录后端目录与当前后端配置。 */
async function loadFirmware() {
  try {
    const [firmware, flashers, config] = await Promise.all([
      api("/api/firmware"), api("/api/flashers"), api("/api/flasher/config"),
    ]);
    state.firmware = firmware;
    state.flashers = flashers;
    state.flasherConfig = config;
    renderFirmware();
    renderFirmwareOptions();
  } catch (error) { toast(error.message, "error"); }
}

/** 固件名称候选（flash 动作的 datalist 与画布编辑器共用）。 */
function renderFirmwareOptions() {
  const datalist = $("#firmware-options");
  if (datalist) {
    datalist.innerHTML = state.firmware.map((item) =>
      `<option value="${escapeHtml(item.name)}">v${escapeHtml(item.version || "?")} · ${escapeHtml(item.product_model || "通用")}</option>`).join("");
  }
  window.PilotFlowEditor?.setFirmware(state.firmware);
}

/** 渲染固件仓表格与烧录后端配置表单。 */
function renderFirmware() {
  $("#firmware-count").textContent = state.firmware.length;
  $("#firmware-table").innerHTML = state.firmware.length ? state.firmware.map((item) => `
    <tr>
      <td><span class="sn-primary">${escapeHtml(item.name)}</span><span class="sn-secondary">${escapeHtml(item.description || "")}</span></td>
      <td><span class="sn-primary">v${escapeHtml(item.version)}</span><span class="sn-secondary">${escapeHtml(item.product_model || "通用")}</span></td>
      <td>${(Number(item.size || 0) / 1024).toFixed(1)} KB</td>
      <td><span class="sn-secondary mono" title="${escapeHtml(item.sha256)}">${escapeHtml(String(item.sha256 || "").slice(0, 16))}…</span></td>
      <td>${escapeHtml(formatTime(item.registered_at))}</td>
      <td><button class="btn btn-secondary" data-verify-firmware="${escapeHtml(item.name)}">校验</button></td>
    </tr>`).join("") : '<tr><td colspan="6" class="empty-row">固件仓为空，请先登记固件</td></tr>';
  $("#flasher-backend").innerHTML = state.flashers.map((item) =>
    `<option value="${escapeHtml(item.name)}" ${item.name === state.flasherConfig.backend ? "selected" : ""}>${escapeHtml(item.name)}${item.driver_available ? "" : "（驱动缺失）"} · ${escapeHtml(item.description)}</option>`).join("");
  $("#flasher-config").value = JSON.stringify(state.flasherConfig.config || {}, null, 2);
  $("#flasher-test-result").textContent = "";
  $("#flasher-test-result").className = "";
}

/** 登记固件：优先上传选择的文件，否则按工作站本地路径入仓。 */
async function registerFirmware(event) {
  event.preventDefault();
  try {
    const file = $("#firmware-file").files[0];
    const meta = {
      version: $("#firmware-version").value.trim(),
      product_model: $("#firmware-model").value.trim(),
      description: $("#firmware-description").value.trim(),
    };
    let entry;
    if (file) {
      const query = new URLSearchParams({ name: file.name, ...meta });
      entry = await api(`/api/firmware/upload?${query}`, { method: "POST", body: file });
    } else {
      const source = $("#firmware-source").value.trim();
      if (!source) throw new Error("请填写固件文件路径，或打开文件夹选择固件文件");
      entry = await api("/api/firmware", { method: "POST", body: { source, ...meta } });
    }
    $("#firmware-form").reset();
    $("#firmware-file-name").textContent = "未选择文件；选择后将以上传方式入仓，优先于路径";
    toast(`固件 ${entry.name} v${entry.version} 已登记入仓`, "success");
    await loadFirmware();
  } catch (error) { toast(error.message, "error"); }
}

/** 校验固件 SHA-256 与登记一致。 */
async function verifyFirmware(button) {
  button.disabled = true;
  try {
    const result = await api(`/api/firmware/${encodeURIComponent(button.dataset.verifyFirmware)}/verify`, { method: "POST" });
    toast(result.message, "success");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
}

/** 保存烧录后端配置（JSON 参数需为对象）。 */
async function saveFlasherConfig(showToast = true) {
  const config = parseJsonField($("#flasher-config").value, "烧录后端参数");
  if (!config || Array.isArray(config) || typeof config !== "object") throw new Error("烧录后端参数必须是 JSON 对象");
  const saved = await api("/api/flasher/config", { method: "PUT", body: { backend: $("#flasher-backend").value, config } });
  state.flasherConfig = saved;
  if (showToast) toast("烧录后端配置已保存", "success");
  return saved;
}

/** 探测烧录后端可用性。 */
async function testFlasher() {
  const result = $("#flasher-test-result");
  result.className = "";
  result.textContent = "正在检查…";
  try {
    const saved = await saveFlasherConfig(false);
    const response = await api("/api/flasher/test", { method: "POST", body: saved });
    result.className = response.ok ? "success" : "error";
    result.textContent = response.message;
    toast(response.message, response.ok ? "success" : "error");
  } catch (error) {
    result.className = "error";
    result.textContent = error.message;
    toast(error.message, "error");
  }
}

/** 应用工位策略到页面：侧边栏工位、工位策略表单（工位编号下拉随主数据联动）、执行台预览。 */
function applyStationSettings(settings) {
  state.settings = settings;
  renderStationSettingOptions();
  $("#setting-operator").value = settings.operator;
  $("#setting-sn-pattern").value = settings.sn_pattern;
  $("#setting-max-retests").value = settings.max_retests ?? 3;
  $("#setting-auto-upload").checked = Boolean(settings.auto_upload);
  $("#sidebar-station").textContent = settings.station_id;
  if (!state.run) $("#operator-input").value = state.user?.username || settings.operator;
  renderProductionPicker();
  renderPreview();
}

/** 读取工位策略。 */
async function loadStationSettings() {
  const settings = await api("/api/settings");
  applyStationSettings(settings);
  return settings;
}

/** 加载 MES 配置（报告上传 + 主数据段）与工位策略，填充 MES 集成页。 */
async function loadMesSettings() {
  try {
    const [config] = await Promise.all([api("/api/mes/config"), loadStationSettings()]);
    state.mesConfig = config;
    $("#mes-endpoint").value = config.endpoint || "";
    $("#mes-method").value = config.method || "POST";
    $("#mes-timeout").value = config.timeout_seconds || 10;
    $("#mes-headers").value = JSON.stringify(config.headers || {}, null, 2);
    $("#mes-idempotency").value = config.idempotency_header || "Idempotency-Key";
    $("#mes-payload-mode").value = config.payload_mode || "mapped";
    $("#mes-mapping").value = JSON.stringify(config.payload_mapping || {}, null, 2);
    const master = config.master_data || {};
    $("#mes-master-mode").value = master.mode || "simulator";
    const products = master.products || {};
    $("#mes-master-products-url").value = products.url || "";
    $("#mes-master-products-path").value = products.items_path || "";
    $("#mes-master-products-map").value = JSON.stringify(products.field_map || {}, null, 2);
    const stations = master.stations || {};
    $("#mes-master-stations-url").value = stations.url || "";
    $("#mes-master-stations-path").value = stations.items_path || "";
    $("#mes-master-stations-map").value = JSON.stringify(stations.field_map || {}, null, 2);
  } catch (error) { toast(error.message, "error"); }
}

/** 保存 MES 配置与工位策略（保留未在表单暴露的配置键）。 */
async function saveMesSettings(showToast = true) {
  const headers = parseJsonField($("#mes-headers").value, "请求头");
  const mapping = parseJsonField($("#mes-mapping").value, "字段映射");
  const previousMaster = state.mesConfig.master_data || {};
  const config = {
    ...state.mesConfig,
    endpoint: $("#mes-endpoint").value.trim(),
    method: $("#mes-method").value,
    timeout_seconds: Number($("#mes-timeout").value),
    headers,
    idempotency_header: $("#mes-idempotency").value.trim(),
    payload_mode: $("#mes-payload-mode").value,
    payload_mapping: mapping,
    master_data: {
      mode: $("#mes-master-mode").value,
      products: {
        ...(previousMaster.products || {}),
        url: $("#mes-master-products-url").value.trim(),
        items_path: $("#mes-master-products-path").value.trim(),
        field_map: parseJsonField($("#mes-master-products-map").value || "{}", "产品字段映射"),
      },
      stations: {
        ...(previousMaster.stations || {}),
        url: $("#mes-master-stations-url").value.trim(),
        items_path: $("#mes-master-stations-path").value.trim(),
        field_map: parseJsonField($("#mes-master-stations-map").value || "{}", "工站字段映射"),
      },
    },
  };
  const settings = {
    station_id: $("#setting-station").value.trim(),
    operator: $("#setting-operator").value.trim(),
    sn_pattern: $("#setting-sn-pattern").value.trim(),
    max_retests: Math.max(0, Math.min(20, Math.trunc(Number($("#setting-max-retests").value) || 0))),
    auto_upload: $("#setting-auto-upload").checked,
  };
  const [savedConfig, savedSettings] = await Promise.all([
    api("/api/mes/config", { method: "PUT", body: { config } }),
    api("/api/settings", { method: "PUT", body: settings }),
  ]);
  state.mesConfig = savedConfig;
  state.settings = savedSettings;
  $("#sidebar-station").textContent = savedSettings.station_id;
  if (showToast) toast("MES 与工位配置已保存", "success");
}

/** 报文预览：保存配置后请求后端渲染实际发送结构（不调用 MES）。 */
async function previewMes() {
  try {
    await saveMesSettings(false);
    const payload = await api("/api/mes/preview");
    $("#mes-preview").textContent = JSON.stringify(payload, null, 2);
    toast("报文预览已生成，未调用 MES", "success");
  } catch (error) { toast(error.message, "error"); }
}

/** 主数据实体元信息：表格/表单/字段选择器与中文名的映射（products/stations 共用一套逻辑）。 */
const masterEntityMeta = {
  products: { tbody: "#master-products", count: "#master-products-count", form: "#master-product-form", fields: [["code", "#mp-code"], ["name", "#mp-name"], ["spec", "#mp-spec"]], cancel: "#mp-cancel", label: "产品" },
  stations: { tbody: "#master-stations", count: "#master-stations-count", form: "#master-station-form", fields: [["code", "#ms-code"], ["name", "#ms-name"], ["line", "#ms-line"], ["station_type", "#ms-type"]], cancel: "#ms-cancel", label: "工站" },
};

/** 加载产品/工站主数据并刷新 MES 页表格与所有联动下拉（执行台、流程编排、工位策略）。 */
async function loadMasterData() {
  try {
    const [products, stations] = await Promise.all([api("/api/mes/products"), api("/api/mes/stations")]);
    state.master.products = products.items;
    state.master.stations = stations.items;
    state.master.productsSync = products.last_sync;
    state.master.stationsSync = stations.last_sync;
    state.master.mode = products.mode || "simulator";
    renderMasterTables();
    renderMasterSelects();
    // 执行台的产品型号与测试工位候选来自主数据，主数据刷新后同步刷新执行台
    renderProductionPicker();
    renderPreview();
  } catch (error) { toast(error.message, "error"); }
}

/** 主数据来源与最近同步时间说明行。 */
function renderMasterMeta() {
  const modeText = state.master.mode === "simulator" ? "模拟器" : "HTTP 接口";
  const format = (value) => (value ? formatTime(value) : "未同步");
  $("#master-sync-meta").textContent = `来源：${modeText} · 产品 ${format(state.master.productsSync)} · 工站 ${format(state.master.stationsSync)}`;
}

/** 渲染产品/工站主数据表格（来源标记、启停徽标、编辑/停用/删除操作）。 */
function renderMasterTables() {
  renderMasterMeta();
  for (const [entity, meta] of Object.entries(masterEntityMeta)) {
    const items = state.master[entity];
    $(meta.count).textContent = items.length;
    const columns = entity === "products"
      ? (item) => `<td>${escapeHtml(item.spec || "—")}</td>`
      : (item) => `<td>${escapeHtml(item.line || "—")}</td><td>${escapeHtml(item.station_type || "—")}</td>`;
    $(meta.tbody).innerHTML = items.map((item) => `
      <tr>
        <td><span class="sn-primary">${escapeHtml(item.code)}</span></td>
        <td>${escapeHtml(item.name)}</td>
        ${columns(item)}
        <td>${item.source === "mes" ? "MES" : "本地"}</td>
        <td><span class="badge ${item.enabled ? "badge-pass" : "badge-fail"}">${item.enabled ? "启用" : "停用"}</span></td>
        <td><div class="table-actions">
          ${item.source === "local" ? `<button class="btn btn-small btn-secondary" data-master-action="edit" data-entity="${entity}" data-code="${escapeHtml(item.code)}">编辑</button>` : ""}
          <button class="btn btn-small btn-secondary" data-master-action="toggle" data-entity="${entity}" data-code="${escapeHtml(item.code)}">${item.enabled ? "停用" : "启用"}</button>
          ${item.source === "local" ? `<button class="btn btn-small btn-ghost danger-text" data-master-action="delete" data-entity="${entity}" data-code="${escapeHtml(item.code)}">删除</button>` : ""}
        </div></td>
      </tr>`).join("") || '<tr><td colspan="7" class="empty-row">暂无数据，点击右上角“同步”从 MES 拉取，或在下方添加本地数据</td></tr>';
  }
}

/** 主数据下拉选项：启用行 + 当前值兜底（已停用/不在主数据时保留并标注）。 */
function masterSelectOptions(items, currentValue, placeholder) {
  const enabled = items.filter((item) => item.enabled);
  const options = [`<option value="">${escapeHtml(placeholder)}</option>`];
  options.push(...enabled.map((item) => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.code)} · ${escapeHtml(item.name)}</option>`));
  if (currentValue && !enabled.some((item) => item.code === currentValue)) {
    const known = items.find((item) => item.code === currentValue);
    options.push(`<option value="${escapeHtml(currentValue)}">${escapeHtml(currentValue)} · ${known ? "已停用" : "不在主数据"}</option>`);
  }
  return options.join("");
}

function renderMasterSelects() {
  const draft = state.draft || {};
  const productSelect = $("#draft-model");
  const stationSelect = $("#draft-station");
  const productPlaceholder = state.master.products.some((item) => item.enabled)
    ? "请选择产品"
    : "暂无产品，请先在 MES 页同步或添加";
  productSelect.innerHTML = masterSelectOptions(state.master.products, draft.product_model || "", productPlaceholder);
  stationSelect.innerHTML = masterSelectOptions(state.master.stations, draft.station_code || "", "不限工站");
  productSelect.value = draft.product_model || "";
  stationSelect.value = draft.station_code || "";
  renderStationSettingOptions();
}

// 工位策略的工位编号与工站主数据联动：下拉候选为已启用的工站，同步工站后自动更新
function renderStationSettingOptions() {
  const select = $("#setting-station");
  if (!select) return;
  const current = state.settings?.station_id || select.value || "";
  select.innerHTML = masterSelectOptions(state.master.stations, current, "请选择工站");
  select.value = current;
}

/** 从 MES 同步主数据（products/stations/both），完成后刷新主数据与流程列表。 */
async function syncMaster(target) {
  try {
    const result = await api("/api/mes/sync", { method: "POST", body: { target } });
    const summary = Object.entries(result).map(([entity, stats]) =>
      `${masterEntityMeta[entity]?.label || entity}：新增 ${stats.added}、更新 ${stats.updated}、停用 ${stats.disabled}`).join("；");
    toast(`同步完成（${summary}）`, "success");
    await loadMasterData();
    await loadScripts();
  } catch (error) { toast(error.message, "error"); }
}

/** 清空主数据添加/编辑表单并恢复为“添加”模式。 */
function resetMasterForm(entity) {
  const meta = masterEntityMeta[entity];
  meta.fields.forEach(([, selector]) => { $(selector).value = ""; $(selector).disabled = false; });
  $(meta.form).dataset.editCode = "";
  $(meta.form).querySelector("button[type=submit]").textContent = `添加本地${meta.label}`;
  $(meta.cancel).hidden = true;
}

/** 把本地行载入表单进入编辑模式（编号不可改）。 */
function editMasterItem(entity, code) {
  const item = state.master[entity].find((entry) => entry.code === code);
  if (!item) return;
  const meta = masterEntityMeta[entity];
  meta.fields.forEach(([field, selector]) => { $(selector).value = item[field] || ""; });
  $(meta.fields[0][1]).disabled = true;
  $(meta.form).dataset.editCode = code;
  $(meta.form).querySelector("button[type=submit]").textContent = `保存${meta.label}`;
  $(meta.cancel).hidden = false;
  $(meta.fields[1][1]).focus();
}

/** 提交主数据表单：有 editCode 走更新，否则新建本地行。 */
async function submitMasterForm(form) {
  const entity = form.dataset.entity;
  const meta = masterEntityMeta[entity];
  const data = {};
  meta.fields.forEach(([field, selector]) => { data[field] = $(selector).value.trim(); });
  const editCode = form.dataset.editCode;
  try {
    if (editCode) {
      await api(`/api/mes/${entity}/${encodeURIComponent(editCode)}`, { method: "PUT", body: { ...data, enabled: true } });
      toast(`${meta.label}已更新`, "success");
    } else {
      await api(`/api/mes/${entity}`, { method: "POST", body: data });
      toast(`本地${meta.label}已添加`, "success");
    }
    resetMasterForm(entity);
    await loadMasterData();
  } catch (error) { toast(error.message, "error"); }
}

/** 启用/停用主数据行（MES 行唯一允许的本地操作）。 */
async function toggleMasterItem(entity, code) {
  const item = state.master[entity].find((entry) => entry.code === code);
  if (!item) return;
  try {
    await api(`/api/mes/${entity}/${encodeURIComponent(code)}`, { method: "PUT", body: { enabled: !item.enabled } });
    await loadMasterData();
    toast(`${code} 已${item.enabled ? "停用" : "启用"}`, "success");
  } catch (error) { toast(error.message, "error"); }
}

/** 删除本地主数据行（需确认；MES 行后端会拒绝）。 */
async function deleteMasterItem(entity, code) {
  const meta = masterEntityMeta[entity];
  if (!window.confirm(`确认删除本地${meta.label} ${code}？`)) return;
  try {
    await api(`/api/mes/${entity}/${encodeURIComponent(code)}`, { method: "DELETE" });
    toast(`本地${meta.label}已删除`, "success");
    await loadMasterData();
  } catch (error) { toast(error.message, "error"); }
}

/** 查询报告列表（SN 模糊 + 结果 + MES 状态筛选）。 */
async function loadReports() {
  try {
    const params = new URLSearchParams();
    const sn = $("#report-sn").value.trim();
    const status = $("#report-status").value;
    const mes = $("#report-mes").value;
    if (sn) params.set("sn", sn);
    if (status) params.set("status", status);
    if (mes) params.set("mes_status", mes);
    params.set("limit", "100");
    const data = await api(`/api/reports?${params}`);
    state.reports = data.items;
    $("#reports-total").textContent = `共 ${data.total} 份报告${data.total > data.items.length ? `，当前显示前 ${data.items.length} 份` : ""}`;
    $("#reports-table").innerHTML = data.items.length ? data.items.map((report) => `
      <tr>
        <td><span class="sn-primary">${escapeHtml(report.report_no)}</span><span class="sn-secondary">${escapeHtml(report.station)} · ${escapeHtml(report.operator)}</span></td>
        <td><span class="sn-primary">${escapeHtml(report.sn)}</span><span class="sn-secondary">${escapeHtml(report.product_model)}</span></td>
        <td>${escapeHtml(report.script_name)}<span class="sn-secondary">版本 v${report.script_version}</span></td>
        <td>${escapeHtml(formatTime(report.finished_at))}<span class="sn-secondary">${formatDuration(report.duration_ms)}</span></td>
        <td>${statusBadge(report.status)}<span class="sn-secondary">${report.summary.passed}/${report.summary.total} 项通过</span></td>
        <td>${statusBadge(report.mes_status)}<span class="sn-secondary">${mesStatusText(report.mes_status)} · ${report.mes_attempts} 次</span></td>
        <td><div class="table-actions"><button class="btn btn-secondary" data-report-id="${escapeHtml(report.id)}">详情</button>${state.user?.role === "admin" && report.mes_status !== "SUCCESS" ? `<button class="btn btn-ghost" data-upload-report="${escapeHtml(report.id)}">上传</button>` : ""}<button class="text-button" data-download-report="${escapeHtml(report.id)}">下载</button></div></td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty-row">未找到符合条件的报告</td></tr>';
  } catch (error) { toast(error.message, "error"); }
}

/** 报告详情弹窗：汇总信息、测试项明细、控制流记录、烧录记录、MES 上传信息与日志。 */
async function viewReport(reportId) {
  const modal = $("#report-modal");
  modal.hidden = false;
  $("#report-modal-content").innerHTML = '<div class="report-body">正在读取报告…</div>';
  try {
    const [report, logs] = await Promise.all([
      api(`/api/reports/${reportId}`), api(`/api/reports/${reportId}/upload-logs`),
    ]);
    const summaryFields = [
      ["产品 SN", report.sn], ["产品型号", report.product_model], ["工位 / 操作员", `${report.station} / ${report.operator}`],
      ["脚本版本", `${report.script.name} · v${report.script.version}`], ["开始时间", formatTime(report.started_at)],
      ["完成时间", formatTime(report.finished_at)], ["总耗时", formatDuration(report.duration_ms)],
      ["结果统计", `${report.summary.passed} PASS / ${report.summary.failed} FAIL`],
    ];
    $("#report-modal-content").innerHTML = `
      <div class="report-head"><div><h2 id="report-modal-title">生产测试报告</h2><p>${escapeHtml(report.report_no)} · 唯一绑定 ${escapeHtml(report.sn)}</p></div><div style="display:flex;gap:12px">${statusBadge(report.status)}<button class="modal-close" data-close-modal aria-label="关闭">×</button></div></div>
      <div class="report-body">
        <div class="report-summary">${summaryFields.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>
        <h3 class="report-section-title">测试项与判定明细</h3>
        <div class="table-wrap"><table class="data-table"><thead><tr><th>#</th><th>测试项</th><th>实测 / 期望</th><th>重试</th><th>耗时</th><th>结果</th></tr></thead><tbody>
          ${report.items.map((item, index) => `<tr><td>${index + 1}</td><td><span class="sn-primary">${escapeHtml(item.name)}</span><span class="sn-secondary">${escapeHtml(item.message || "")}</span>${item.assertions?.length ? `<details><summary class="text-button">断言明细</summary><pre class="assertion-detail">${escapeHtml(JSON.stringify(item.assertions, null, 2))}</pre></details>` : ""}</td><td><span class="sn-primary">${escapeHtml(displayValue(item.actual))}</span><span class="sn-secondary">期望 ${escapeHtml(displayValue(item.expected))}</span></td><td>${item.attempts || 0}</td><td>${formatDuration(item.duration_ms)}</td><td>${statusBadge(item.status)}</td></tr>`).join("")}
        </tbody></table></div>
        ${report.controls?.length ? `<h3 class="report-section-title">控制流执行记录</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>节点</th><th>类型</th><th>路径</th><th>结果</th><th>明细</th></tr></thead><tbody>${report.controls.map((control) => `<tr><td>${escapeHtml(control.name)}</td><td>${escapeHtml(control.type)}</td><td><span class="sn-secondary">${escapeHtml(control.path)}</span></td><td>${statusBadge(control.status)}</td><td><details><summary class="text-button">查看</summary><pre class="assertion-detail">${escapeHtml(JSON.stringify(control.details, null, 2))}</pre></details></td></tr>`).join("")}</tbody></table></div>` : ""}
        ${report.flash_records?.length ? `<h3 class="report-section-title">固件烧录记录</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>固件</th><th>版本</th><th>后端</th><th>结果</th><th>耗时</th><th>完成时间</th></tr></thead><tbody>${report.flash_records.map((record) => `<tr><td><span class="sn-primary">${escapeHtml(record.firmware?.name || "")}</span><span class="sn-secondary mono" title="${escapeHtml(record.firmware?.sha256 || "")}">${escapeHtml(String(record.firmware?.sha256 || "").slice(0, 16))}…</span></td><td>${escapeHtml(record.firmware?.version || "")}</td><td>${escapeHtml(record.backend)}</td><td>${statusBadge(record.ok ? "PASS" : "FAIL")}<span class="sn-secondary">${escapeHtml(record.message || "")}</span></td><td>${formatDuration(record.duration_ms)}</td><td>${escapeHtml(formatTime(record.finished_at))}</td></tr>`).join("")}</tbody></table></div>` : ""}
        <div class="report-mes-info"><strong>MES：${escapeHtml(mesStatusText(report.mes_status))}</strong> · 已尝试 ${report.mes_attempts} 次${report.uploaded_at ? ` · ${escapeHtml(formatTime(report.uploaded_at))}` : ""}${report.mes_response ? `<br>响应：${escapeHtml(report.mes_response)}` : ""}</div>
        ${logs.length ? `<details class="advanced-editor"><summary>MES 上传日志 <span>${logs.length} 条</span></summary><pre class="json-preview">${escapeHtml(JSON.stringify(logs, null, 2))}</pre></details>` : ""}
      </div>
      <div class="report-footer"><button class="btn btn-ghost" data-print-report>打印报告</button><button class="btn btn-secondary" data-download-report="${escapeHtml(report.id)}">下载 JSON</button>${state.user?.role === "admin" && report.mes_status !== "SUCCESS" ? `<button class="btn btn-primary" data-upload-report="${escapeHtml(report.id)}">${report.mes_status === "FAILED" ? "重试上传" : "上传 MES"}</button>` : ""}<button class="btn btn-ghost" data-close-modal>关闭</button></div>`;
  } catch (error) {
    $("#report-modal-content").innerHTML = `<div class="report-body"><p>${escapeHtml(error.message)}</p><button class="btn btn-secondary" data-close-modal>关闭</button></div>`;
  }
}

/** 上传/重试上传报告到 MES，成功后刷新列表与弹窗。 */
async function uploadReport(reportId, button) {
  if (button) button.disabled = true;
  try {
    await api(`/api/reports/${reportId}/upload`, { method: "POST" });
    toast("报告已成功上传 MES", "success");
    await Promise.all([loadReports(), loadDashboard()]);
    if (!$("#report-modal").hidden) await viewReport(reportId);
    if (state.run?.report_id === reportId) {
      state.run = await api(`/api/test-runs/${state.run.id}`);
      renderRun();
    }
  } catch (error) {
    toast(error.message, "error");
    if (button) button.disabled = false;
    if (!$("#report-modal").hidden) await viewReport(reportId);
  }
}

/** 角色 → 中文名。 */
function roleText(role) {
  return role === "admin" ? "管理员" : "操作工";
}

/** 按角色应用界面权限：隐藏管理员专属入口，写入当前用户与操作员。 */
function applyRole() {
  const user = state.user;
  const isAdmin = user?.role === "admin";
  $$("[data-admin-only]").forEach((element) => { element.hidden = !isAdmin; });
  $("#current-user").textContent = user?.display_name || "未登录";
  $("#current-role").textContent = user ? `${roleText(user.role)} · ${user.username}` : "—";
  $("#user-avatar").textContent = user?.display_name?.slice(0, 1).toUpperCase() || "—";
  $("#topbar-role").textContent = user ? roleText(user.role) : "—";
  $("#operator-input").value = user?.username || "";
  if (!isAdmin) $("#report-mes").value = "";
}

/** 切换到登录页（清空运行状态，401 时由 api() 统一调用）。 */
function showLogin(message = "") {
  clearTimeout(state.pollTimer);
  state.user = null;
  state.run = null;
  state.scan = null;
  $("#app-shell").hidden = true;
  $("#auth-screen").hidden = false;
  $("#login-password").value = "";
  $("#login-error").textContent = message;
  document.body.classList.add("authenticating");
}

/** 登录成功后进入应用外壳。 */
function showApplication(user) {
  state.user = user;
  document.body.classList.remove("authenticating");
  $("#auth-screen").hidden = true;
  $("#app-shell").hidden = false;
  applyRole();
}

/** 登录表单提交。 */
async function login(event) {
  event.preventDefault();
  const button = $("#login-submit");
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("#login-username").value.trim(), password: $("#login-password").value },
    });
    showApplication(result.user);
    await startApplication();
  } catch (error) {
    if (!state.user) $("#login-error").textContent = error.message;
    else toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

/** 退出登录。 */
async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  showLogin("已安全退出，请重新登录");
}

/** 加载用户列表（仅管理员）。 */
async function loadUsers() {
  if (state.user?.role !== "admin") return;
  try {
    state.users = await api("/api/auth/users");
    $("#user-count").textContent = state.users.length;
    $("#users-table").innerHTML = state.users.map((user) => `<tr>
      <td><span class="sn-primary">${escapeHtml(user.display_name)}</span><span class="sn-secondary">${escapeHtml(user.username)}</span></td>
      <td><span class="badge ${user.role === "admin" ? "badge-running" : "badge-idle"}">${roleText(user.role)}</span></td>
      <td>${user.enabled ? '<span class="badge badge-pass">启用</span>' : '<span class="badge badge-fail">停用</span>'}</td>
      <td>${escapeHtml(formatTime(user.updated_at))}</td>
      <td><div class="table-actions"><button class="btn btn-secondary" data-reset-user="${escapeHtml(user.username)}">重置密码</button><button class="btn btn-ghost" data-toggle-user="${escapeHtml(user.username)}" data-enabled="${user.enabled}" ${user.username === state.user.username ? "disabled" : ""}>${user.enabled ? "停用" : "启用"}</button></div></td>
    </tr>`).join("") || '<tr><td colspan="5" class="empty-row">暂无用户</td></tr>';
  } catch (error) { toast(error.message, "error"); }
}

/** 创建用户。 */
async function createUser(event) {
  event.preventDefault();
  try {
    await api("/api/auth/users", { method: "POST", body: {
      username: $("#new-username").value.trim(),
      display_name: $("#new-display-name").value.trim(),
      role: $("#new-role").value,
      password: $("#new-password").value,
    } });
    $("#user-form").reset();
    toast("用户已创建", "success");
    await loadUsers();
  } catch (error) { toast(error.message, "error"); }
}

/** 启用/停用账号。 */
async function toggleUser(button) {
  const username = button.dataset.toggleUser;
  try {
    await api(`/api/auth/users/${encodeURIComponent(username)}/enabled`, { method: "PUT", body: { enabled: button.dataset.enabled !== "true" } });
    toast("账号状态已更新", "success");
    await loadUsers();
  } catch (error) { toast(error.message, "error"); }
}

/** 管理员重置他人密码（弹窗输入新密码）。 */
async function resetUserPassword(button) {
  const username = button.dataset.resetUser;
  const password = window.prompt(`为用户 ${username} 设置新密码（至少 8 位）`);
  if (password === null) return;
  try {
    await api(`/api/auth/users/${encodeURIComponent(username)}/password`, { method: "PUT", body: { password } });
    if (username === state.user.username) showLogin("密码已重置，请使用新密码登录");
    else toast("密码已重置，该用户需要重新登录", "success");
  } catch (error) { toast(error.message, "error"); }
}

/** 关闭修改密码弹窗并清空表单。 */
function closePasswordModal() {
  $("#password-modal").hidden = true;
  $("#password-form").reset();
}

/** 修改自己的密码（需验证当前密码，两次输入需一致）。 */
async function changeOwnPassword(event) {
  event.preventDefault();
  const next = $("#new-own-password").value;
  if (next !== $("#confirm-own-password").value) { toast("两次输入的新密码不一致", "error"); return; }
  try {
    await api("/api/auth/password", { method: "POST", body: { current_password: $("#current-password").value, new_password: next } });
    closePasswordModal();
    toast("密码已更新", "success");
  } catch (error) { toast(error.message, "error"); }
}

/** 关闭报告详情弹窗。 */
function closeReport() {
  $("#report-modal").hidden = true;
  $("#report-modal-content").innerHTML = "";
}

/**
 * 绑定全部事件：登录/账号、导航、执行台（扫码、开始、F2 快捷键、三个索引下拉）、
 * 流程编辑器、协议/固件/MES/主数据表单，以及报告相关的文档级事件委托。
 */
function bindEvents() {
  $("#login-form").addEventListener("submit", login);
  $$("[data-fill-account]").forEach((button) => button.addEventListener("click", () => {
    const username = button.dataset.fillAccount;
    $("#login-username").value = username;
    $("#login-password").value = username === "admin" ? "admin123" : "operator123";
    $("#login-password").focus();
  }));
  $("#logout").addEventListener("click", logout);
  $("#change-password").addEventListener("click", () => { $("#password-modal").hidden = false; $("#current-password").focus(); });
  $("#password-form").addEventListener("submit", changeOwnPassword);
  $$("[data-close-password]").forEach((button) => button.addEventListener("click", closePasswordModal));
  $("#user-form").addEventListener("submit", createUser);
  $("#protocol-form").addEventListener("change", updateProtocolDependencies);
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
  $("#scan-form").addEventListener("submit", (event) => { event.preventDefault(); scanSn(); });
  $("#start-test").addEventListener("click", startTest);
  $("#reset-test").addEventListener("click", resetConsole);
  $("#production-product").addEventListener("change", () => {
    state.selectedProduct = $("#production-product").value || null;
    const stations = productionStations();
    const preferred = state.settings?.station_id && stations.includes(state.settings.station_id)
      ? state.settings.station_id
      : stations[0] || "";
    state.selectedStation = stations.includes(state.selectedStation) ? state.selectedStation : preferred;
    resolveProductionScript();
    resetConsole();
  });
  $("#production-station").addEventListener("change", () => {
    state.selectedStation = $("#production-station").value;
    resolveProductionScript();
    resetConsole();
  });
  $("#production-flow").addEventListener("change", () => {
    state.selectedScriptId = Number($("#production-flow").value);
    renderProductionPicker();
    resetConsole();
  });
  $("#report-filter").addEventListener("submit", (event) => { event.preventDefault(); loadReports(); });
  $("#save-protocol").addEventListener("click", async () => {
    try { await saveProtocol(); } catch (error) { toast(error.message, "error"); }
  });
  $("#test-protocol").addEventListener("click", testProtocol);
  $("#firmware-form").addEventListener("submit", registerFirmware);
  $("#pick-firmware-file").addEventListener("click", () => $("#firmware-file").click());
  $("#firmware-file").addEventListener("change", () => {
    const file = $("#firmware-file").files[0];
    $("#firmware-file-name").textContent = file ? `已选择：${file.name}（${(file.size / 1024).toFixed(1)} KB），登记时上传入仓` : "未选择文件；选择后将以上传方式入仓，优先于路径";
  });
  $("#save-flasher").addEventListener("click", async () => {
    try { await saveFlasherConfig(); } catch (error) { toast(error.message, "error"); }
  });
  $("#test-flasher").addEventListener("click", testFlasher);
  $("#protocol-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-protocol]");
    if (button) { state.selectedProtocol = button.dataset.protocol; renderProtocols(); }
  });
  $("#save-mes").addEventListener("click", async () => {
    try { await saveMesSettings(); } catch (error) { toast(error.message, "error"); }
  });
  $("#preview-mes").addEventListener("click", previewMes);
  $("#sync-products").addEventListener("click", () => syncMaster("products"));
  $("#sync-stations").addEventListener("click", () => syncMaster("stations"));
  $("#sync-master").addEventListener("click", () => syncMaster("both"));
  ["products", "stations"].forEach((entity) => {
    const meta = masterEntityMeta[entity];
    $(meta.form).addEventListener("submit", (event) => {
      event.preventDefault();
      submitMasterForm(event.target);
    });
    $(meta.cancel).addEventListener("click", () => resetMasterForm(entity));
  });
  bindScriptEditor();
  document.addEventListener("click", (event) => {
    const navigateButton = event.target.closest("[data-navigate]");
    if (navigateButton) navigate(navigateButton.dataset.navigate);
    const reportButton = event.target.closest("[data-report-id]");
    if (reportButton) viewReport(reportButton.dataset.reportId);
    const uploadButton = event.target.closest("[data-upload-report]");
    if (uploadButton) uploadReport(uploadButton.dataset.uploadReport, uploadButton);
    const downloadButton = event.target.closest("[data-download-report]");
    if (downloadButton) window.location.href = `/api/reports/${downloadButton.dataset.downloadReport}/download`;
    const toggleUserButton = event.target.closest("[data-toggle-user]");
    if (toggleUserButton) toggleUser(toggleUserButton);
    const resetUserButton = event.target.closest("[data-reset-user]");
    if (resetUserButton) resetUserPassword(resetUserButton);
    const verifyFirmwareButton = event.target.closest("[data-verify-firmware]");
    if (verifyFirmwareButton) verifyFirmware(verifyFirmwareButton);
    const masterButton = event.target.closest("[data-master-action]");
    if (masterButton) {
      const { masterAction, entity, code } = masterButton.dataset;
      if (masterAction === "edit") editMasterItem(entity, code);
      else if (masterAction === "toggle") toggleMasterItem(entity, code);
      else if (masterAction === "delete") deleteMasterItem(entity, code);
    }
    if (event.target.closest("[data-close-modal]")) closeReport();
    if (event.target.closest("[data-print-report]")) window.print();
  });
  $("#report-modal").addEventListener("click", (event) => { if (event.target === $("#report-modal")) closeReport(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#report-modal").hidden) closeReport();
    if (event.key === "Escape" && !$("#password-modal").hidden) closePasswordModal();
    if (event.key === "F2" && $("#page-production").classList.contains("active") && $("#report-modal").hidden) {
      event.preventDefault();
      startTest();
    }
  });
}

/**
 * 登录后的应用启动：管理员挂载 React 流程画布（一次）、健康检查、
 * 并行加载流程与工位策略、恢复未完成的运行，最后按 URL hash 进入对应页面。
 */
async function startApplication() {
  if (state.user?.role === "admin" && !state.applicationMounted) {
    window.PilotFlowEditor?.mount($("#react-flow-editor"), {
      initialDefinition: state.draft || { schema_version: 2, name: "新产品控制流程", product_model: "NEW-MODEL", variables: {}, steps: [], subflows: {} },
      onChange: (definition) => {
        state.draft = definition;
        renderSteps();
      },
    });
    state.applicationMounted = true;
  }
  const health = await api("/api/health");
  $("#server-label").textContent = "后端服务已连接";
  $("#server-dot").classList.remove("offline");
  $("#sidebar-station").textContent = health.station;
  await Promise.all([loadScripts(), loadStationSettings()]);
  resetConsole();
  const active = await api("/api/test-runs/active");
  if (active) await resumeRun(active.id);
  navigate(pageMeta[location.hash.slice(1)] ? location.hash.slice(1) : "production");
}

/** 入口：绑定事件、启动时钟、尝试用已有会话恢复登录（失败则显示登录页）。 */
async function initialize() {
  bindEvents();
  setInterval(() => { $("#current-time").textContent = new Date().toLocaleString("zh-CN", { hour12: false }); }, 1000);
  $("#current-time").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
  try {
    const result = await api("/api/auth/me");
    showApplication(result.user);
    await startApplication();
  } catch (error) {
    $("#server-label").textContent = "后端服务连接失败";
    $("#server-dot").classList.add("offline");
    showLogin(error.status === 401 ? "" : `连接失败：${error.message}`);
  }
}

initialize();
