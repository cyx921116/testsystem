"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clone = (value) => JSON.parse(JSON.stringify(value));
const state = {
  scripts: [],
  selectedScriptId: null,
  draft: null,
  draftId: null,
  draftVersion: null,
  scan: null,
  run: null,
  pollTimer: null,
  protocols: [],
  selectedProtocol: "simulator",
  mesConfig: {},
  settings: {},
  reports: [],
};

const pageMeta = {
  production: ["生产测试", "扫描产品 SN，执行测试并绑定唯一报告"],
  scripts: ["流程编排", "低代码编辑测试逻辑，后端统一编译执行"],
  protocols: ["通讯配置", "管理串口、总线、网络与调试器连接"],
  mes: ["MES 集成", "配置报告报文、接口与工位上传策略"],
  reports: ["报告中心", "按 SN 追溯测试数据与 MES 上传状态"],
};

const protocolSymbols = {
  simulator: "SIM", serial: "COM", can: "CAN", tcp: "TCP", modbus: "MB",
  bluetooth: "BLE", wifi: "WiFi", mqtt: "MQTT", jlink: "JLK",
};

const actionLabels = {
  request: "发送并等待响应", send: "仅发送", receive: "等待接收", execute: "设备操作",
  set: "设置变量", assert: "结果断言", sleep: "延时等待",
};

const operatorLabels = {
  equals: "等于", not_equals: "不等于", contains: "包含", not_contains: "不包含",
  regex: "正则匹配", gt: "大于", gte: "大于等于", lt: "小于", lte: "小于等于",
  between: "数值区间", truthy: "为真", exists: "存在且非空",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(value) {
  const ms = Number(value || 0);
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

function statusBadge(status) {
  const value = status || "PENDING";
  return `<span class="badge badge-${escapeHtml(value.toLowerCase())}">${escapeHtml(value)}</span>`;
}

function mesStatusText(status) {
  return { PENDING: "待上传", UPLOADING: "上传中", SUCCESS: "上传成功", FAILED: "上传失败" }[status] || "未生成";
}

async function api(url, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.body && typeof request.body !== "string") {
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
    throw new Error(message);
  }
  return payload;
}

function toast(message, type = "") {
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.textContent = message;
  $("#toast-container").append(element);
  setTimeout(() => element.remove(), 4200);
}

function parseJsonField(value, label) {
  try { return JSON.parse(value); } catch (error) { throw new Error(`${label} JSON 格式错误：${error.message}`); }
}

function parseSmartValue(value) {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  return value;
}

function navigate(page) {
  if (!pageMeta[page]) return;
  $$(".page").forEach((element) => element.classList.toggle("active", element.id === `page-${page}`));
  $$(".nav-item").forEach((element) => element.classList.toggle("active", element.dataset.page === page));
  $("#page-title").textContent = pageMeta[page][0];
  $("#page-subtitle").textContent = pageMeta[page][1];
  history.replaceState(null, "", `#${page}`);
  if (page === "production") loadDashboard();
  if (page === "scripts") renderScriptLibrary();
  if (page === "protocols") loadProtocols();
  if (page === "mes") loadMesSettings();
  if (page === "reports") loadReports();
}

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

async function loadScripts() {
  state.scripts = await api("/api/scripts");
  const enabled = state.scripts.filter((script) => script.enabled);
  if (!enabled.some((script) => script.id === state.selectedScriptId)) state.selectedScriptId = enabled[0]?.id || null;
  $("#production-script").innerHTML = enabled.map((script) => `
    <option value="${script.id}" ${script.id === state.selectedScriptId ? "selected" : ""}>${escapeHtml(script.product_model)} · ${escapeHtml(script.name)} · v${script.version}</option>`).join("");
}

function currentProductionScript() {
  return state.scripts.find((script) => script.id === Number($("#production-script").value || state.selectedScriptId));
}

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

function renderPreview() {
  const script = currentProductionScript();
  if (!script || state.run) return;
  renderTestItems(script.definition.steps.map((step) => ({
    name: step.name, status: "PENDING", expected: step.actions.find((action) => action.type === "assert")?.expected,
  })));
  $("#unit-model").textContent = script.product_model;
  $("#unit-script-version").textContent = `v${script.version}`;
  $("#unit-sn").textContent = state.scan?.sn || "—";
  $("#unit-station").textContent = state.settings.station_id || "STATION-01";
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

function resetConsole() {
  clearTimeout(state.pollTimer);
  state.run = null;
  state.scan = null;
  $("#sn-input").value = "";
  $("#sn-input").disabled = false;
  $("#production-script").disabled = false;
  $("#scan-feedback").className = "scan-feedback";
  $("#scan-feedback").textContent = "SN 将与本次测试脚本快照和唯一报告绑定";
  $("#start-test").disabled = true;
  renderPreview();
  $("#sn-input").focus();
}

async function scanSn() {
  const sn = $("#sn-input").value.trim();
  const script = currentProductionScript();
  if (!sn) { toast("请先扫描或输入 SN", "error"); return; }
  if (!script) { toast("没有可用的测试流程", "error"); return; }
  const feedback = $("#scan-feedback");
  feedback.className = "scan-feedback";
  feedback.textContent = "正在校验 SN 与报告唯一性…";
  try {
    const result = await api("/api/test-runs/scan", { method: "POST", body: { sn, script_id: script.id } });
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

async function startTest() {
  if (!state.scan?.can_start || state.run?.status === "RUNNING") return;
  const script = currentProductionScript();
  const operator = $("#operator-input").value.trim();
  if (!operator) { toast("请填写测试操作员", "error"); return; }
  $("#start-test").disabled = true;
  try {
    state.run = await api("/api/test-runs", {
      method: "POST",
      body: { sn: state.scan.sn, script_id: script.id, operator },
    });
    $("#sn-input").disabled = true;
    $("#production-script").disabled = true;
    renderRun();
    pollRun();
  } catch (error) {
    toast(error.message, "error");
    $("#start-test").disabled = false;
  }
}

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

async function resumeRun(runId) {
  clearTimeout(state.pollTimer);
  state.run = await api(`/api/test-runs/${runId}`);
  state.selectedScriptId = state.run.script_id;
  $("#production-script").value = String(state.run.script_id);
  $("#sn-input").value = state.run.sn;
  $("#sn-input").disabled = state.run.status === "RUNNING";
  $("#production-script").disabled = state.run.status === "RUNNING";
  renderRun();
  if (state.run.status === "RUNNING") pollRun();
}

function defaultAction(type = "request") {
  const defaults = {
    request: { type, data: "AT+SELFTEST?", encoding: "text", save_as: "response" },
    send: { type, data: "AT+START\r\n", encoding: "text" },
    receive: { type, save_as: "response", timeout_ms: 1000, size: 4096 },
    execute: { type, operation: "read_holding_registers", params: { address: 0, count: 1 }, save_as: "result" },
    set: { type, variable: "expected_value", value: "PASS" },
    assert: { type, actual: "${response}", operator: "equals", expected: "PASS" },
    sleep: { type, duration_ms: 500 },
  };
  return clone(defaults[type] || defaults.request);
}

function newStep(index = 0) {
  return {
    id: `item_${Date.now().toString(36).slice(-5)}_${index + 1}`,
    name: "新测试项", protocol: "simulator", timeout_ms: 5000, retries: 0, on_fail: "stop",
    actions: [defaultAction("request"), defaultAction("assert")],
  };
}

function optionTags(options, selected) {
  return Object.entries(options).map(([value, label]) => `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function textActionField(si, ai, key, label, value, wide = false, type = "text") {
  return `<div class="action-field ${wide ? "wide" : ""}"><label>${escapeHtml(label)}</label><input type="${type}" data-action-field data-s="${si}" data-a="${ai}" data-key="${key}" value="${escapeHtml(value ?? "")}"></div>`;
}

function renderAction(action, si, ai) {
  let fields = "";
  if (["request", "send"].includes(action.type)) {
    fields += textActionField(si, ai, "data", "发送数据 / 支持 ${变量}", action.data, true);
    fields += `<div class="action-field"><label>编码</label><select data-action-field data-s="${si}" data-a="${ai}" data-key="encoding">${optionTags({ text: "文本", hex: "HEX", json: "JSON" }, action.encoding || "text")}</select></div>`;
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
    fields += textActionField(si, ai, "actual", "实际值 / 模板", action.actual, true);
    fields += `<div class="action-field"><label>判断方式</label><select data-action-field data-s="${si}" data-a="${ai}" data-key="operator">${optionTags(operatorLabels, action.operator)}</select></div>`;
    if (!["truthy", "exists"].includes(action.operator)) {
      const expected = Array.isArray(action.expected) ? action.expected.join(", ") : action.expected;
      fields += textActionField(si, ai, "expected", action.operator === "between" ? "下限, 上限" : "期望值", expected, true);
    }
    fields += textActionField(si, ai, "message", "失败提示（可选）", action.message, true);
  } else if (action.type === "sleep") {
    fields += textActionField(si, ai, "duration_ms", "延时时长 ms", action.duration_ms, false, "number");
  }
  return `<div class="action-row">
    <div class="action-type"><label>动作 ${ai + 1}</label><select data-action-type data-s="${si}" data-a="${ai}">${optionTags(actionLabels, action.type)}</select></div>
    ${fields}<button class="icon-button danger" title="删除动作" data-delete-action data-s="${si}" data-a="${ai}">×</button>
  </div>`;
}

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
}

function openScript(scriptId) {
  const script = state.scripts.find((item) => item.id === Number(scriptId));
  if (!script) return;
  state.draftId = script.id;
  state.draftVersion = script.version;
  state.draft = clone(script.definition);
  renderScriptLibrary();
}

function renderScriptLibrary() {
  if (!state.draft && state.scripts.length) {
    const preferred = state.scripts.find((script) => script.id === state.selectedScriptId) || state.scripts[0];
    state.draftId = preferred.id;
    state.draftVersion = preferred.version;
    state.draft = clone(preferred.definition);
  }
  $("#script-count").textContent = state.scripts.length;
  $("#script-list").innerHTML = state.scripts.map((script) => `
    <button class="script-list-item ${script.id === state.draftId ? "active" : ""} ${script.enabled ? "" : "disabled"}" data-script-select="${script.id}">
      <strong>${escapeHtml(script.name)}</strong><span>${escapeHtml(script.product_model)} · ${script.definition.steps.length} 个测试项</span><small>VERSION ${script.version}${script.enabled ? "" : " · 已停用"}</small>
    </button>`).join("") || '<div class="empty-row">暂无流程，请新建</div>';
  if (!state.draft) state.draft = { schema_version: 1, name: "新产品测试流程", product_model: "NEW-MODEL", variables: {}, steps: [newStep()] };
  $("#draft-name").value = state.draft.name || "";
  $("#draft-model").value = state.draft.product_model || "";
  $("#draft-version").textContent = state.draftVersion ? `v${state.draftVersion}` : "未保存";
  $("#validation-results").innerHTML = "";
  $("#script-json").value = JSON.stringify(state.draft, null, 2);
  renderSteps();
}

function createNewScript() {
  state.draftId = null;
  state.draftVersion = null;
  state.draft = { schema_version: 1, name: "新产品测试流程", product_model: "NEW-MODEL", variables: {}, steps: [newStep()] };
  renderScriptLibrary();
  $("#draft-name").focus();
}

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

function updateActionField(element) {
  const step = state.draft.steps[Number(element.dataset.s)];
  const action = step?.actions[Number(element.dataset.a)];
  if (!action) return;
  const key = element.dataset.key;
  let value = element.value;
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

function bindScriptEditor() {
  $("#new-script").addEventListener("click", createNewScript);
  $("#validate-script").addEventListener("click", () => validateScript());
  $("#save-script").addEventListener("click", saveScript);
  $("#add-step").addEventListener("click", () => {
    state.draft.steps ||= [];
    state.draft.steps.push(newStep(state.draft.steps.length));
    renderSteps();
  });
  $("#script-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-script-select]");
    if (button) openScript(button.dataset.scriptSelect);
  });
  [$("#draft-name"), $("#draft-model")].forEach((input) => input.addEventListener("input", () => {
    state.draft[input.dataset.draftField] = input.value;
  }));
  $("#steps-container").addEventListener("input", (event) => {
    const stepInput = event.target.closest("[data-step-field]");
    if (stepInput) {
      const step = state.draft.steps[Number(stepInput.dataset.s)];
      if (step) step[stepInput.dataset.stepField] = ["timeout_ms", "retries"].includes(stepInput.dataset.stepField) ? Number(stepInput.value) : stepInput.value;
      return;
    }
    if (event.target.matches("[data-action-field]")) updateActionField(event.target);
  });
  $("#steps-container").addEventListener("change", (event) => {
    if (event.target.matches("[data-action-field]")) updateActionField(event.target);
    if (event.target.matches("[data-action-type]")) {
      const si = Number(event.target.dataset.s);
      const ai = Number(event.target.dataset.a);
      state.draft.steps[si].actions[ai] = defaultAction(event.target.value);
      renderSteps();
    }
  });
  $("#steps-container").addEventListener("click", (event) => {
    const deleteButton = event.target.closest("[data-delete-action]");
    if (deleteButton) {
      const step = state.draft.steps[Number(deleteButton.dataset.s)];
      step.actions.splice(Number(deleteButton.dataset.a), 1);
      renderSteps();
      return;
    }
    const button = event.target.closest("[data-step-command]");
    if (!button) return;
    const index = Number(button.dataset.s);
    const command = button.dataset.stepCommand;
    if (command === "delete") state.draft.steps.splice(index, 1);
    if (command === "add-action") state.draft.steps[index].actions.push(defaultAction("assert"));
    if (command === "up" && index > 0) [state.draft.steps[index - 1], state.draft.steps[index]] = [state.draft.steps[index], state.draft.steps[index - 1]];
    if (command === "down" && index < state.draft.steps.length - 1) [state.draft.steps[index + 1], state.draft.steps[index]] = [state.draft.steps[index], state.draft.steps[index + 1]];
    renderSteps();
  });
  $("#apply-script-json").addEventListener("click", () => {
    try {
      const parsed = parseJsonField($("#script-json").value, "流程");
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("流程根节点必须是 JSON 对象");
      state.draft = parsed;
      state.draft.steps ||= [];
      $("#draft-name").value = state.draft.name || "";
      $("#draft-model").value = state.draft.product_model || "";
      renderSteps();
      toast("JSON 已应用，请执行编译校验", "success");
    } catch (error) { toast(error.message, "error"); }
  });
}

async function loadProtocols() {
  try {
    state.protocols = await api("/api/protocols");
    if (!state.protocols.some((item) => item.name === state.selectedProtocol)) state.selectedProtocol = state.protocols[0]?.name;
    renderProtocols();
  } catch (error) { toast(error.message, "error"); }
}

function selectedProtocol() {
  return state.protocols.find((item) => item.name === state.selectedProtocol);
}

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
  $("#protocol-form").innerHTML = protocol.fields.map((field) => {
    const value = protocol.config[field.key] ?? field.default;
    if (field.type === "checkbox") {
      return `<div class="checkbox-field"><input type="checkbox" id="config-${escapeHtml(field.key)}" data-config-key="${escapeHtml(field.key)}" data-kind="checkbox" ${value ? "checked" : ""}><label for="config-${escapeHtml(field.key)}">${escapeHtml(field.label)}</label></div>`;
    }
    if (field.type === "select") {
      return `<div class="field"><label>${escapeHtml(field.label)}</label><select data-config-key="${escapeHtml(field.key)}" data-kind="select">${field.options.map((option) => `<option value="${escapeHtml(option.value)}" ${option.value === value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select></div>`;
    }
    if (field.type === "json") {
      return `<div class="field span-2"><label>${escapeHtml(field.label)}</label><textarea class="code-input" rows="5" data-config-key="${escapeHtml(field.key)}" data-kind="json">${escapeHtml(JSON.stringify(value || {}, null, 2))}</textarea></div>`;
    }
    const type = ["number", "password"].includes(field.type) ? field.type : "text";
    return `<div class="field"><label>${escapeHtml(field.label)}${field.required ? " *" : ""}</label><input type="${type}" data-config-key="${escapeHtml(field.key)}" data-kind="${type}" value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(field.placeholder || "")}"></div>`;
  }).join("");
  $("#protocol-json").value = JSON.stringify(protocol.config, null, 2);
  $("#protocol-test-result").textContent = "";
  $("#protocol-test-result").className = "";
}

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

async function saveProtocol(showToast = true) {
  const config = collectProtocolConfig();
  const name = state.selectedProtocol;
  const result = await api(`/api/protocols/${name}`, { method: "PUT", body: { config } });
  selectedProtocol().config = config;
  $("#protocol-json").value = JSON.stringify(config, null, 2);
  if (showToast) toast("通讯配置已保存", "success");
  return result.config;
}

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

async function loadMesSettings() {
  try {
    const [config, settings] = await Promise.all([api("/api/mes/config"), api("/api/settings")]);
    state.mesConfig = config;
    state.settings = settings;
    $("#mes-endpoint").value = config.endpoint || "";
    $("#mes-method").value = config.method || "POST";
    $("#mes-timeout").value = config.timeout_seconds || 10;
    $("#mes-headers").value = JSON.stringify(config.headers || {}, null, 2);
    $("#mes-idempotency").value = config.idempotency_header || "Idempotency-Key";
    $("#mes-payload-mode").value = config.payload_mode || "mapped";
    $("#mes-mapping").value = JSON.stringify(config.payload_mapping || {}, null, 2);
    $("#setting-station").value = settings.station_id;
    $("#setting-operator").value = settings.operator;
    $("#setting-sn-pattern").value = settings.sn_pattern;
    $("#setting-auto-upload").checked = Boolean(settings.auto_upload);
    $("#sidebar-station").textContent = settings.station_id;
    if (!state.run) $("#operator-input").value = settings.operator;
    renderPreview();
  } catch (error) { toast(error.message, "error"); }
}

async function saveMesSettings(showToast = true) {
  const headers = parseJsonField($("#mes-headers").value, "请求头");
  const mapping = parseJsonField($("#mes-mapping").value, "字段映射");
  const config = {
    ...state.mesConfig,
    endpoint: $("#mes-endpoint").value.trim(),
    method: $("#mes-method").value,
    timeout_seconds: Number($("#mes-timeout").value),
    headers,
    idempotency_header: $("#mes-idempotency").value.trim(),
    payload_mode: $("#mes-payload-mode").value,
    payload_mapping: mapping,
  };
  const settings = {
    station_id: $("#setting-station").value.trim(),
    operator: $("#setting-operator").value.trim(),
    sn_pattern: $("#setting-sn-pattern").value.trim(),
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

async function previewMes() {
  try {
    await saveMesSettings(false);
    const payload = await api("/api/mes/preview");
    $("#mes-preview").textContent = JSON.stringify(payload, null, 2);
    toast("报文预览已生成，未调用 MES", "success");
  } catch (error) { toast(error.message, "error"); }
}

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
        <td><div class="table-actions"><button class="btn btn-secondary" data-report-id="${escapeHtml(report.id)}">详情</button>${report.mes_status !== "SUCCESS" ? `<button class="btn btn-ghost" data-upload-report="${escapeHtml(report.id)}">上传</button>` : ""}<button class="text-button" data-download-report="${escapeHtml(report.id)}">下载</button></div></td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty-row">未找到符合条件的报告</td></tr>';
  } catch (error) { toast(error.message, "error"); }
}

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
        <div class="report-mes-info"><strong>MES：${escapeHtml(mesStatusText(report.mes_status))}</strong> · 已尝试 ${report.mes_attempts} 次${report.uploaded_at ? ` · ${escapeHtml(formatTime(report.uploaded_at))}` : ""}${report.mes_response ? `<br>响应：${escapeHtml(report.mes_response)}` : ""}</div>
        ${logs.length ? `<details class="advanced-editor"><summary>MES 上传日志 <span>${logs.length} 条</span></summary><pre class="json-preview">${escapeHtml(JSON.stringify(logs, null, 2))}</pre></details>` : ""}
      </div>
      <div class="report-footer"><button class="btn btn-ghost" data-print-report>打印报告</button><button class="btn btn-secondary" data-download-report="${escapeHtml(report.id)}">下载 JSON</button>${report.mes_status !== "SUCCESS" ? `<button class="btn btn-primary" data-upload-report="${escapeHtml(report.id)}">${report.mes_status === "FAILED" ? "重试上传" : "上传 MES"}</button>` : ""}<button class="btn btn-ghost" data-close-modal>关闭</button></div>`;
  } catch (error) {
    $("#report-modal-content").innerHTML = `<div class="report-body"><p>${escapeHtml(error.message)}</p><button class="btn btn-secondary" data-close-modal>关闭</button></div>`;
  }
}

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

function closeReport() {
  $("#report-modal").hidden = true;
  $("#report-modal-content").innerHTML = "";
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
  $("#scan-form").addEventListener("submit", (event) => { event.preventDefault(); scanSn(); });
  $("#start-test").addEventListener("click", startTest);
  $("#reset-test").addEventListener("click", resetConsole);
  $("#production-script").addEventListener("change", () => {
    state.selectedScriptId = Number($("#production-script").value);
    resetConsole();
  });
  $("#report-filter").addEventListener("submit", (event) => { event.preventDefault(); loadReports(); });
  $("#save-protocol").addEventListener("click", async () => {
    try { await saveProtocol(); } catch (error) { toast(error.message, "error"); }
  });
  $("#test-protocol").addEventListener("click", testProtocol);
  $("#protocol-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-protocol]");
    if (button) { state.selectedProtocol = button.dataset.protocol; renderProtocols(); }
  });
  $("#save-mes").addEventListener("click", async () => {
    try { await saveMesSettings(); } catch (error) { toast(error.message, "error"); }
  });
  $("#preview-mes").addEventListener("click", previewMes);
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
    if (event.target.closest("[data-close-modal]")) closeReport();
    if (event.target.closest("[data-print-report]")) window.print();
  });
  $("#report-modal").addEventListener("click", (event) => { if (event.target === $("#report-modal")) closeReport(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#report-modal").hidden) closeReport();
    if (event.key === "F2" && $("#page-production").classList.contains("active") && $("#report-modal").hidden) {
      event.preventDefault();
      startTest();
    }
  });
}

async function initialize() {
  bindEvents();
  setInterval(() => { $("#current-time").textContent = new Date().toLocaleString("zh-CN", { hour12: false }); }, 1000);
  $("#current-time").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
  try {
    const health = await api("/api/health");
    $("#server-label").textContent = "后端服务已连接";
    $("#server-dot").classList.remove("offline");
    $("#sidebar-station").textContent = health.station;
    await Promise.all([loadScripts(), loadMesSettings()]);
    resetConsole();
    const active = await api("/api/test-runs/active");
    if (active) await resumeRun(active.id);
    navigate(pageMeta[location.hash.slice(1)] ? location.hash.slice(1) : "production");
  } catch (error) {
    $("#server-label").textContent = "后端服务连接失败";
    $("#server-dot").classList.add("offline");
    toast(error.message, "error");
  }
}

initialize();
