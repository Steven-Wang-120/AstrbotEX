const API_BASE = window.ASTRBOTEX_API_BASE || "http://127.0.0.1:8765";
const MAX_TRACE_EVENTS = 200;

const state = {
  events: [],
  eventSource: null,
  visionSources: [],
  activeVisionSource: null,
  selectedVisionSourceId: null,
  plugins: [],
  selectedPluginId: null,
  toastTimer: null,
  logAutoscroll: true,
};

const $ = (id) => document.getElementById(id);

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function showToast(message, kind = "ok") {
  const toast = $("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast show ${kind}`;
  if (state.toastTimer) clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.className = "toast";
  }, 2600);
}

async function runAction(button, pendingLabel, action) {
  const originalText = button.textContent;
  button.disabled = true;
  button.classList.add("busy");
  button.textContent = pendingLabel;
  try {
    await action();
    showToast(`${originalText}完成`);
  } catch (error) {
    showToast(`${originalText}失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
    button.textContent = originalText;
  }
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
  if (!timestamp) return "--";
  return new Date(timestamp * 1000).toLocaleTimeString();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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
  if (page === "plugins") refreshPlugins();
  if (page === "logs") renderEvents();
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

  if (Array.isArray(status.recent_events) && state.events.length === 0) {
    status.recent_events.forEach(pushEvent);
  }
}

function eventLevel(event) {
  if (event.type === "fault" || event.type === "rule_rejected") return "ERROR";
  if (event.type === "runtime_state" || event.type === "plugin") return "INFO";
  return "DEBUG";
}

function formatEventLine(event) {
  const time = formatTime(event.timestamp);
  const level = eventLevel(event);
  const detail = event.data && Object.keys(event.data).length > 0 ? ` ${JSON.stringify(event.data)}` : "";
  return {
    time,
    level,
    type: event.type || "event",
    message: `${event.message || ""}${detail}`,
  };
}

function pushEvent(event) {
  state.events.push(event);
  state.events = state.events.slice(-MAX_TRACE_EVENTS);
  renderEvents();
}

function renderEvents() {
  const consoleEl = $("logConsole");
  if (!consoleEl) return;
  consoleEl.innerHTML = "";

  if (state.events.length === 0) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = "等待运行时事件";
    consoleEl.appendChild(empty);
    return;
  }

  state.events.forEach((event) => {
    const line = formatEventLine(event);
    const row = document.createElement("div");
    row.className = "log-line";
    row.innerHTML = `
      <span class="log-time">${escapeHtml(line.time)}</span>
      <span class="log-level log-level-${line.level.toLowerCase()}">[${escapeHtml(line.level)}]</span>
      <span class="log-type">${escapeHtml(line.type)}</span>
      <span class="log-message">${escapeHtml(line.message)}</span>
    `;
    consoleEl.appendChild(row);
  });

  if (state.logAutoscroll) {
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource(`${API_BASE}/api/events`);
  state.eventSource = source;
  source.onopen = () => setEventConnection(true, "SSE 已连接");
  source.onerror = () => setEventConnection(false, "SSE 重连中");
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

function setEventConnection(ok, label) {
  setText("eventStatus", label);
  const pill = $("logConnectionPill");
  if (!pill) return;
  pill.classList.toggle("offline", !ok);
  pill.lastChild.textContent = label;
}

function toggleLogAutoscroll() {
  state.logAutoscroll = !state.logAutoscroll;
  setText("logAutoscrollButton", `自动滚动：${state.logAutoscroll ? "开" : "关"}`);
  if (state.logAutoscroll) {
    const consoleEl = $("logConsole");
    if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function clearLogs() {
  state.events = [];
  renderEvents();
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

async function refreshPlugins() {
  const data = await apiJson("/api/v1/ex/plugins");
  state.plugins = data.plugins || [];
  if (!state.selectedPluginId && state.plugins.length > 0) {
    state.selectedPluginId = state.plugins[0].id;
  }
  renderPluginGrid();
  renderPluginDetail();
}

function selectedPlugin() {
  return state.plugins.find((plugin) => plugin.id === state.selectedPluginId) || null;
}

function renderPluginGrid() {
  const grid = $("pluginGrid");
  grid.innerHTML = "";
  if (state.plugins.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty";
    empty.textContent = "还没有安装本地插件";
    grid.appendChild(empty);
    return;
  }
  state.plugins.forEach((plugin) => {
    const card = document.createElement("article");
    card.className = `plugin-card ${plugin.id === state.selectedPluginId ? "selected" : ""}`;
    card.addEventListener("click", () => {
      state.selectedPluginId = plugin.id;
      renderPluginGrid();
      renderPluginDetail();
    });

    const cover = document.createElement("div");
    cover.className = "plugin-cover";
    if (plugin.cover_url) {
      const image = document.createElement("img");
      image.src = `${API_BASE}${plugin.cover_url}`;
      image.alt = plugin.name;
      cover.appendChild(image);
    } else {
      cover.innerHTML = `<span>${plugin.name.slice(0, 2).toUpperCase()}</span>`;
    }

    const toggle = document.createElement("label");
    toggle.className = "switch plugin-switch";
    toggle.addEventListener("click", (event) => event.stopPropagation());
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(plugin.enabled);
    input.addEventListener("change", () => setPluginEnabled(plugin.id, input.checked));
    toggle.appendChild(input);
    toggle.appendChild(document.createElement("span"));
    cover.appendChild(toggle);

    const body = document.createElement("div");
    body.className = "plugin-card-body";
    body.innerHTML = `
      <h2>${escapeHtml(plugin.name)}</h2>
      <p>${escapeHtml(plugin.description || "无描述")}</p>
      <div class="plugin-badges">${(plugin.provides || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
      <small>${escapeHtml(plugin.status)} · ${escapeHtml(plugin.version || "--")}</small>
    `;
    card.appendChild(cover);
    card.appendChild(body);
    grid.appendChild(card);
  });
}

function renderPluginDetail() {
  const plugin = selectedPlugin();
  $("pluginDetailEmpty").hidden = Boolean(plugin);
  $("pluginDetailBody").hidden = !plugin;
  if (!plugin) {
    setText("pluginDetailStatus", "--");
    return;
  }
  setText("pluginDetailStatus", plugin.enabled ? "已启用" : plugin.status);
  setText("pluginDetailName", plugin.name || "--");
  setText("pluginDetailDescription", plugin.description || "无描述");
  setText("pluginDetailId", plugin.id || "--");
  setText("pluginDetailVersion", plugin.version || "--");
  setText("pluginDetailAuthor", plugin.author || "--");
  setText("pluginDetailState", plugin.error ? `${plugin.status}: ${plugin.error}` : plugin.status);
  setText("pluginSchemaPreview", JSON.stringify(plugin.config_schema || { provides: plugin.provides, requires: plugin.requires }, null, 2));
}

async function setPluginEnabled(pluginId, enabled) {
  const action = enabled ? "enable" : "disable";
  const result = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(pluginId)}/${action}`, {
    method: "POST",
    body: "{}",
  });
  const updated = result.plugin;
  state.plugins = state.plugins.map((plugin) => (plugin.id === updated.id ? updated : plugin));
  showToast(`${updated.name} ${enabled ? "已启用" : "已停用"}`);
  renderPluginGrid();
  renderPluginDetail();
  await refreshStatus();
}

async function uploadPluginZip(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}/api/v1/ex/plugins/upload`, {
    method: "POST",
    body: form,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  state.selectedPluginId = data.plugin.id;
  await refreshPlugins();
}

function bindActions() {
  setText("apiBaseLabel", API_BASE.replace(/^https?:\/\//, ""));
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  $("refreshButton").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", refreshStatus));
  $("startButton").addEventListener("click", (event) => runAction(event.currentTarget, "启动中", startRuntime));
  $("stopButton").addEventListener("click", (event) => runAction(event.currentTarget, "停止中", stopRuntime));

  $("visionRefreshButton").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", refreshVisionSources));
  $("visionAddButton").addEventListener("click", (event) => runAction(event.currentTarget, "添加中", addVisionSource));
  $("visionSaveButton").addEventListener("click", (event) => {
    event.preventDefault();
    runAction(event.currentTarget, "保存中", saveVisionSource);
  });
  $("visionTestButton").addEventListener("click", (event) => {
    event.preventDefault();
    runAction(event.currentTarget, "测试中", testVisionSource);
  });
  $("visionSetActiveButton").addEventListener("click", (event) => {
    event.preventDefault();
    runAction(event.currentTarget, "设置中", setActiveVisionSource);
  });
  $("visionDeleteButton").addEventListener("click", (event) => {
    event.preventDefault();
    runAction(event.currentTarget, "删除中", deleteVisionSource);
  });

  $("pluginsRefreshButton").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", refreshPlugins));
  $("pluginUploadButton").addEventListener("click", () => $("pluginZipInput").click());
  $("pluginZipInput").addEventListener("change", (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    if (!file) return;
    runAction($("pluginUploadButton"), "+", () => uploadPluginZip(file));
    event.currentTarget.value = "";
  });

  $("logAutoscrollButton").addEventListener("click", toggleLogAutoscroll);
  $("logClearButton").addEventListener("click", clearLogs);
}

bindActions();
renderEvents();
refreshStatus();
refreshVisionSources();
refreshPlugins();
connectEvents();
setInterval(refreshStatus, 2000);
