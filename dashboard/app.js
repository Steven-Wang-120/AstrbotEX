const API_BASE = window.ASTRBOTEX_API_BASE || "http://127.0.0.1:8765";
const MAX_TRACE_EVENTS = 80;

const state = {
  events: [],
  eventSource: null,
  visionSources: [],
  activeVisionSource: null,
  selectedVisionSourceId: null,
};

const $ = (id) => document.getElementById(id);

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function isPresent(value) {
  return value !== null && value !== undefined && value !== "";
}

function formatValue(value, fallback = "--") {
  return isPresent(value) ? String(value) : fallback;
}

function formatPose(pose) {
  if (!Array.isArray(pose) || pose.length === 0) return "--";
  return pose.map((item) => Number(item).toFixed(2)).join(", ");
}

function formatTime(timestamp) {
  if (!timestamp) return "未同步";
  return new Date(timestamp * 1000).toLocaleTimeString();
}

async function apiJson(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function switchPage(page) {
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${page}`));
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });
  if (page === "vision") refreshVisionSources();
}

async function refreshStatus() {
  try {
    const status = await apiJson("/api/status");
    renderStatus(status);
    setConnection(true, "API 已连接");
  } catch (error) {
    setConnection(false, `API 连接失败：${error.message}`);
  }
}

function renderStatus(status) {
  const world = status.world || {};
  const robot = world.robot || {};
  const entities = world.entities || [];
  const zones = world.zones || [];
  const plugins = status.plugins || [];

  setText("runtimeState", String(status.runtime_state || "--").toUpperCase());
  setText("runtimeDetail", `目标频率 ${formatValue(status.tick_hz)} Hz`);
  setText("worldSummary", `${entities.length} 实体`);
  setText("worldDetail", `${zones.length} 区域，链路 ${robot.link_ok ? "正常" : "--"}`);
  setText("motionState", robot.link_ok ? "连接正常" : "--");
  setText("motionDetail", robot.estop ? "急停触发" : "--");
  setText("activeSkill", status.active_skill || "--");
  setText("skillDetail", status.active_goal ? status.active_goal.type : "--");
  setText("snapshotTime", formatTime(world.timestamp));
  setText("tickHz", `${formatValue(status.tick_hz)} Hz`);
  setText("batteryVoltage", isPresent(robot.battery_voltage) ? `${Number(robot.battery_voltage).toFixed(1)} V` : "--");
  setText("estopState", robot.estop ? "触发" : "--");
  setText("robotPose", formatPose(robot.pose));
  setText("goalPreview", `active_goal: ${JSON.stringify(status.active_goal, null, 2)}`);
  setText("pluginCount", `${plugins.length} 个`);
  renderPlugins(plugins);

  if (Array.isArray(status.recent_events) && state.events.length === 0) {
    status.recent_events.forEach(pushEvent);
  }
}

function renderPlugins(plugins) {
  const list = $("pluginList");
  list.innerHTML = "";
  plugins.forEach((plugin) => {
    const item = document.createElement("div");
    item.className = `slot ${plugin.enabled ? "ok" : ""}`;
    item.innerHTML = `<span>${plugin.kind}</span><b>${plugin.id}</b>`;
    list.appendChild(item);
  });
}

function pushEvent(event) {
  state.events.unshift(event);
  state.events = state.events.slice(0, MAX_TRACE_EVENTS);
  renderEvents();
}

function renderEvents() {
  const list = $("traceList");
  list.innerHTML = "";
  if (state.events.length === 0) {
    const empty = document.createElement("div");
    empty.innerHTML = "<b>waiting</b><span>等待运行时事件</span>";
    list.appendChild(empty);
    return;
  }
  state.events.forEach((event) => {
    const item = document.createElement("div");
    const detail = event.data ? JSON.stringify(event.data) : "";
    item.innerHTML = `<b>${event.type}</b><span>${event.message} ${detail}</span>`;
    list.appendChild(item);
  });
}

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource(`${API_BASE}/api/events`);
  state.eventSource = source;
  source.onopen = () => setText("eventStatus", "SSE 已连接");
  source.onerror = () => setText("eventStatus", "SSE 重连中");
  source.addEventListener("event", (message) => {
    try {
      pushEvent(JSON.parse(message.data));
    } catch {
      // Ignore malformed dev-time events.
    }
  });
}

function setConnection(ok, label) {
  const pill = $("connectionPill");
  pill.classList.toggle("offline", !ok);
  pill.lastChild.textContent = label;
}

async function startRuntime() {
  await apiJson("/api/runtime/start", { method: "POST", body: "{}" });
  await refreshStatus();
}

async function stopRuntime() {
  await apiJson("/api/runtime/stop", {
    method: "POST",
    body: JSON.stringify({ reason: "stopped from dashboard" }),
  });
  await refreshStatus();
}

async function refreshVisionSources() {
  const data = await apiJson("/api/v1/ex/vision/sources");
  state.visionSources = data.sources || [];
  state.activeVisionSource = data.active_source || null;
  if (!state.selectedVisionSourceId && state.visionSources.length > 0) {
    state.selectedVisionSourceId = state.activeVisionSource || state.visionSources[0].id;
  }
  renderVisionSourceList();
  renderSelectedVisionSource();
}

function selectedVisionSource() {
  return state.visionSources.find((source) => source.id === state.selectedVisionSourceId) || null;
}

function renderVisionSourceList() {
  setText("visionSourceCount", `${state.visionSources.length}`);
  const list = $("visionSourceList");
  list.innerHTML = "";
  state.visionSources.forEach((source) => {
    const item = document.createElement("button");
    item.className = `source-item ${source.id === state.selectedVisionSourceId ? "active" : ""}`;
    const title = source.id || "未命名视觉源";
    item.innerHTML = `<span>${source.type}${source.id === state.activeVisionSource ? " · active" : ""}</span><b>${title}</b>`;
    item.addEventListener("click", () => {
      state.selectedVisionSourceId = source.id;
      renderVisionSourceList();
      renderSelectedVisionSource();
    });
    list.appendChild(item);
  });
}

function renderSelectedVisionSource() {
  const source = selectedVisionSource();
  if (!source) {
    setText("visionTitle", "未选择视觉源");
    setText("visionActiveBadge", "--");
    setText("visionLatestPreview", "--");
    renderPreview("");
    return;
  }
  setText("visionTitle", source.id || "未命名视觉源");
  setText("visionActiveBadge", source.id === state.activeVisionSource ? "当前输入" : "未激活");
  $("visionId").value = source.id || "";
  $("visionType").value = source.type || "local_api";
  $("visionEnabled").checked = Boolean(source.enabled);
  $("visionResultEndpoint").value = source.result_endpoint || "";
  $("visionPreviewUrl").value = source.preview_url || "";
  $("visionSnapshotUrl").value = source.snapshot_url || "";
  $("visionTimeoutMs").value = source.timeout_ms ?? 80;
  $("visionStaleAfterMs").value = source.stale_after_ms ?? 300;
  $("visionMinConfidence").value = source.min_confidence ?? 0.4;
  renderPreview(source.preview_url || "");
  refreshVisionLatest();
}

function readVisionForm() {
  return {
    id: $("visionId").value.trim(),
    type: $("visionType").value,
    enabled: $("visionEnabled").checked,
    result_endpoint: $("visionResultEndpoint").value.trim(),
    preview_url: $("visionPreviewUrl").value.trim(),
    snapshot_url: $("visionSnapshotUrl").value.trim(),
    timeout_ms: Number($("visionTimeoutMs").value || 80),
    stale_after_ms: Number($("visionStaleAfterMs").value || 300),
    min_confidence: Number($("visionMinConfidence").value || 0.4),
    metadata: {},
  };
}

function renderPreview(url) {
  const box = $("visionPreviewBox");
  box.innerHTML = "";
  if (!url) {
    box.innerHTML = "<span>未配置 preview_url</span>";
    setText("visionPreviewStatus", "--");
    return;
  }
  const image = document.createElement("img");
  image.src = url;
  image.alt = "vision preview";
  image.onerror = () => setText("visionPreviewStatus", "无法加载");
  image.onload = () => setText("visionPreviewStatus", "已加载");
  box.appendChild(image);
  setText("visionPreviewStatus", "加载中");
}

async function saveVisionSource() {
  const payload = readVisionForm();
  if (!payload.id) {
    alert("Vision Source ID 不能为空");
    return;
  }
  await apiJson("/api/v1/ex/vision/sources", { method: "POST", body: JSON.stringify(payload) });
  state.selectedVisionSourceId = payload.id;
  await refreshVisionSources();
}

async function addVisionSource() {
  const id = "";
  state.selectedVisionSourceId = id;
  state.visionSources.push({
    id,
    type: "local_api",
    enabled: true,
    result_endpoint: "",
    preview_url: "",
    snapshot_url: "",
    timeout_ms: 80,
    stale_after_ms: 300,
    min_confidence: 0.4,
    metadata: {},
  });
  renderVisionSourceList();
  renderSelectedVisionSource();
}

async function setActiveVisionSource() {
  const source = readVisionForm();
  await saveVisionSource();
  await apiJson("/api/v1/ex/vision/active-source", { method: "POST", body: JSON.stringify({ id: source.id }) });
  state.activeVisionSource = source.id;
  await refreshVisionSources();
}

async function deleteVisionSource() {
  const source = selectedVisionSource();
  if (!source || !confirm(`删除视觉源 ${source.id}？`)) return;
  await apiJson(`/api/v1/ex/vision/sources/${encodeURIComponent(source.id)}`, { method: "DELETE" });
  state.selectedVisionSourceId = null;
  await refreshVisionSources();
}

async function testVisionSource() {
  const source = readVisionForm();
  if (!source.id) return;
  await saveVisionSource();
  const result = await apiJson(`/api/v1/ex/vision/sources/${encodeURIComponent(source.id)}/test`, { method: "POST" });
  setText("visionLatestStatus", result.ok ? `测试通过 ${result.latency_ms} ms` : "测试失败");
  setText("visionLatestPreview", JSON.stringify(result, null, 2));
}

async function refreshVisionLatest() {
  const source = selectedVisionSource();
  if (!source) return;
  const latest = await apiJson("/api/v1/ex/vision/latest");
  setText("visionLatestStatus", latest.ok === false ? "无结果" : "已更新");
  setText("visionLatestPreview", JSON.stringify(latest, null, 2));
}

function bindActions() {
  setText("apiBaseLabel", API_BASE.replace(/^https?:\/\//, ""));
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  $("refreshButton").addEventListener("click", refreshStatus);
  $("startButton").addEventListener("click", startRuntime);
  $("stopButton").addEventListener("click", stopRuntime);
  $("visionRefreshButton").addEventListener("click", refreshVisionSources);
  $("visionAddButton").addEventListener("click", addVisionSource);
  $("visionSaveButton").addEventListener("click", (event) => {
    event.preventDefault();
    saveVisionSource();
  });
  $("visionTestButton").addEventListener("click", (event) => {
    event.preventDefault();
    testVisionSource();
  });
  $("visionSetActiveButton").addEventListener("click", (event) => {
    event.preventDefault();
    setActiveVisionSource();
  });
  $("visionDeleteButton").addEventListener("click", (event) => {
    event.preventDefault();
    deleteVisionSource();
  });
}

bindActions();
renderEvents();
refreshStatus();
refreshVisionSources();
connectEvents();
setInterval(refreshStatus, 2000);
