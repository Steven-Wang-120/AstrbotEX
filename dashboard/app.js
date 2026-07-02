const API_BASE = window.ASTRBOTEX_API_BASE || "http://127.0.0.1:8765";
const MAX_TRACE_EVENTS = 200;
const PLUGIN_CATEGORIES = ["vision", "control", "decision", "special"];

const CATEGORY_LABELS = {
  vision: "视觉",
  control: "控制",
  decision: "决策",
  special: "特种",
};

const state = {
  events: [],
  eventSource: null,
  plugins: [],
  selectedPluginIds: {
    vision: null,
    control: null,
    decision: null,
    special: null,
  },
  activeUploadCategory: null,
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
  if (PLUGIN_CATEGORIES.includes(page)) refreshPlugins();
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
    const detail = event.data && Object.keys(event.data).length > 0 ? ` ${JSON.stringify(event.data)}` : "";
    const row = document.createElement("div");
    row.className = "log-line";
    row.innerHTML = `
      <span class="log-time">${escapeHtml(formatTime(event.timestamp))}</span>
      <span class="log-level log-level-${eventLevel(event).toLowerCase()}">[${escapeHtml(eventLevel(event))}]</span>
      <span class="log-type">${escapeHtml(event.type || "event")}</span>
      <span class="log-message">${escapeHtml(`${event.message || ""}${detail}`)}</span>
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
      // ignore malformed events
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

function pluginsByCategory(category) {
  return state.plugins.filter((plugin) => (plugin.category || "special") === category);
}

async function refreshPlugins() {
  const data = await apiJson("/api/v1/ex/plugins");
  state.plugins = data.plugins || [];
  for (const category of PLUGIN_CATEGORIES) {
    const plugins = pluginsByCategory(category);
    if (!plugins.find((plugin) => plugin.id === state.selectedPluginIds[category])) {
      state.selectedPluginIds[category] = plugins.length > 0 ? plugins[0].id : null;
    }
  }
  renderAllPluginCategories();
}

function renderAllPluginCategories() {
  for (const category of PLUGIN_CATEGORIES) {
    renderPluginCategory(category);
  }
}

function renderPluginCategory(category) {
  const plugins = pluginsByCategory(category);
  setText(`${category}Count`, `${plugins.length}`);
  const grid = $(`${category}Grid`);
  grid.innerHTML = "";
  if (plugins.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty";
    empty.textContent = `还没有安装${CATEGORY_LABELS[category]}插件`;
    grid.appendChild(empty);
    renderPluginDetail(category, null);
    return;
  }
  plugins.forEach((plugin) => {
    const card = document.createElement("article");
    card.className = `plugin-card ${plugin.id === state.selectedPluginIds[category] ? "selected" : ""}`;
    card.addEventListener("click", () => {
      state.selectedPluginIds[category] = plugin.id;
      renderPluginCategory(category);
    });

    const cover = document.createElement("div");
    cover.className = "plugin-cover";
    if (plugin.cover_url) {
      const image = document.createElement("img");
      image.src = `${API_BASE}${plugin.cover_url}`;
      image.alt = plugin.name;
      cover.appendChild(image);
    } else {
      cover.innerHTML = `<span>${escapeHtml((plugin.name || plugin.id).slice(0, 2).toUpperCase())}</span>`;
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
  const selected = plugins.find((plugin) => plugin.id === state.selectedPluginIds[category]) || plugins[0];
  renderPluginDetail(category, selected);
}

function renderPluginDetail(category, plugin) {
  $(`${category}DetailEmpty`).hidden = Boolean(plugin);
  $(`${category}DetailBody`).hidden = !plugin;
  if (!plugin) {
    setText(`${category}DetailStatus`, "--");
    return;
  }
  setText(`${category}DetailStatus`, plugin.enabled ? "已启用" : plugin.status);
  setText(`${category}DetailName`, plugin.name || "--");
  setText(`${category}DetailDescription`, plugin.description || "无描述");
  setText(`${category}DetailId`, plugin.id || "--");
  setText(`${category}DetailVersion`, plugin.version || "--");
  setText(`${category}DetailAuthor`, plugin.author || "--");
  setText(`${category}DetailState`, plugin.error ? `${plugin.status}: ${plugin.error}` : plugin.status);
  setText(
    `${category}SchemaPreview`,
    JSON.stringify(plugin.config_schema || { category: plugin.category, provides: plugin.provides, requires: plugin.requires }, null, 2),
  );
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
  renderAllPluginCategories();
  await refreshStatus();
}

async function uploadPluginZip(file, category) {
  const form = new FormData();
  form.append("file", file);
  form.append("category", category);
  const response = await fetch(`${API_BASE}/api/v1/ex/plugins/upload`, {
    method: "POST",
    body: form,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  state.selectedPluginIds[category] = data.plugin.id;
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

  document.querySelectorAll("[data-plugin-refresh]").forEach((button) => {
    button.addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", refreshPlugins));
  });
  document.querySelectorAll("[data-upload-category]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeUploadCategory = button.dataset.uploadCategory;
      $("pluginZipInput").click();
    });
  });
  $("pluginZipInput").addEventListener("change", (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    if (!file || !state.activeUploadCategory) return;
    const button = document.querySelector(`[data-upload-category="${state.activeUploadCategory}"]`);
    runAction(button, "+", () => uploadPluginZip(file, state.activeUploadCategory));
    event.currentTarget.value = "";
  });

  $("logAutoscrollButton").addEventListener("click", toggleLogAutoscroll);
  $("logClearButton").addEventListener("click", clearLogs);
}

bindActions();
renderEvents();
refreshStatus();
refreshPlugins();
connectEvents();
setInterval(refreshStatus, 2000);
