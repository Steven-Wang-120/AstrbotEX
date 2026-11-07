const API_BASE = window.ASTRBOTEX_API_BASE || window.location.origin;
let managementCredential = "";
let managementCredentialEpoch = 0;
let lastEventId = null;
let eventLogSeeded = false;
const seenEventIds = new Set();
const seenEventOrder = [];
function validEventId(value) {
  return typeof value === 'string' && value.length <= 128 && /^[A-Za-z0-9_.:-]{1,120}:[1-9][0-9]*$/.test(value);
}
function rememberEventId(id) {
  if (!id) return false;
  if (seenEventIds.has(id)) return true;
  seenEventIds.add(id); seenEventOrder.push(id);
  while (seenEventOrder.length > 2000) seenEventIds.delete(seenEventOrder.shift());
  return false;
}
function resetEventLogState() {
  lastEventId = null; eventLogSeeded = false;
  seenEventIds.clear(); seenEventOrder.length = 0;
  window.EXLogs?.destroy(); window.EXLogs?.mount();
  if (typeof envView !== 'undefined') {
    clearTimeout(envView.timer); envView.timer = null;
    envView.diagnosticsSeq += 1;
  }
}
const PLUGIN_CATEGORIES = ["vision", "perception", "control", "decision", "special", "interaction"];

const CATEGORY_LABELS = {
  all: "全部",
  vision: "视觉",
  perception: "感知",
  control: "控制",
  decision: "决策",
  special: "特殊",
  interaction: "交互",
};

const CONNECTION_FIELDS = {
  host: { label: "监听地址", type: "text", placeholder: "0.0.0.0" },
  port: { label: "端口", type: "number", min: 1, max: 65535 },
  endpoint: { label: "连接地址", type: "text", placeholder: "tcp://127.0.0.1:8766" },
  identity: { label: "Identity", type: "text", placeholder: "astrbotex-main" },
  channel: { label: "功能选择", type: "select", options: [["text", "文本"], ["audio", "语音"], ["vision", "视觉"], ["control", "上位步骤"]] },
  protocol_profile: { label: "协议配置", type: "select", options: [["raw", "原始消息"], ["astrbotex", "AstrBotEX ZMQ"]] },
  path: { label: "握手路径", type: "text", placeholder: "/" },
  url: { label: "服务地址", type: "text", placeholder: "ws://127.0.0.1:8780/" },
  token: { label: "访问令牌", type: "password", placeholder: "可选" },
  ping_interval_sec: { label: "心跳间隔（秒）", type: "number", min: 1, max: 3600 },
  reconnect_interval_sec: { label: "重连间隔（秒）", type: "number", min: 0.2, max: 3600, step: 0.1 },
  upper_robot_id: { label: "绑定机器人 ID", type: "text", placeholder: "本地可信值，不采信对端声明" },
  upper_route_refs: { label: "可信来源 route_ref", type: "text", placeholder: "逗号分隔，例如 route-1,route-2" },
  upper_ex_session: { label: "固定会话（可选）", type: "text", placeholder: "留空则使用当前 ex_session" },
};

//: Identity anchors for the upper-step channel; only meaningful on control.
const UPPER_FIELDS = new Set(["upper_robot_id", "upper_route_refs", "upper_ex_session"]);

const state = {
  eventSource: null,
  plugins: [],
  publishers: [],
  runtimeState: "idle",
  activePage: "core",
  activePluginId: null,
  activePluginCategory: null,
  activePluginTab: "all",
  toastTimer: null,
  configDirty: false,
  pubsubDirty: false,
  uninstallArmed: false,
  uninstallTimer: null,
  connections: [],
  connectionTypes: {},
  activeConnectionId: null,
  connectionDirty: false,
  connectionDeleteArmed: false,
  connectionDeleteTimer: null,
  environment: null,
};

const $ = (id) => document.getElementById(id);

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function setTextWithTitle(id, value, fallback = "--") {
  const text =
    value === null || value === undefined || value === "" ? fallback : String(value);
  setText(id, text);
  const el = $(id);
  if (el) el.title = text;
}

function formatProviderLabel(value) {
  const text = String(value || "").trim();
  if (!/^Provider/i.test(text) || !/API$/i.test(text)) return text;
  return text
    .replace(/^Provider\s*/i, "")
    .replace(/API$/i, "")
    .replace(/^\[/, "")
    .replace(/\]$/, "")
    .trim();
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
  if (!button) return action();
  const dirtyDot = button.querySelector(".dirty-dot");
  const originalHTML = button.innerHTML;
  button.disabled = true;
  button.classList.add("busy");
  button.textContent = pendingLabel;
  try {
    await action();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
    button.innerHTML = originalHTML;
    if (dirtyDot) updateDirtyUI();
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

async function managementFetch(path, options = {}) {
  if (!managementCredential) throw new Error("请输入管理凭据；刷新后需要重新输入。");
  const target = new URL(path, `${API_BASE}/`);
  if (target.origin !== window.location.origin || target.protocol !== "http:") {
    throw new Error("请通过本机或 SSH 隧道的同源 HTTP 页面访问管理服务。");
  }
  const epoch = managementCredentialEpoch;
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${managementCredential}`);
  const response = await fetch(target.href, { ...options, headers, cache: "no-store", redirect: "error" });
  if (epoch !== managementCredentialEpoch) throw new Error("凭据已变更，已忽略旧请求。");
  if (response.status === 401) {
    managementCredential = "";
    managementCredentialEpoch += 1;
    resetEventLogState();
    window.DecisionPage?.credentialChanged();
    if (state.eventSource) state.eventSource.close();
    setText("managementCredentialStatus", "凭据无效或已失效，请重新输入。");
    setEventConnection(false, "等待凭据");
  }
  return response;
}

async function apiJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await managementFetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

async function applyManagementCredential() {
  const input = $("managementCredential");
  const value = input.value.trim();
  if (!value) throw new Error("请输入管理凭据。");
  managementCredential = value;
  managementCredentialEpoch += 1;
  resetEventLogState();
  const epoch = managementCredentialEpoch;
  window.DecisionPage?.credentialChanged();
  input.value = "";
  if (state.eventSource) state.eventSource.close();
  setText("managementCredentialStatus", "正在验证凭据；仅保存在本页内存。");
  try {
    await refreshStatus();
    await Promise.allSettled([refreshPlugins(), refreshEnvironment()]);
    if (!managementCredential || epoch !== managementCredentialEpoch) return;
    setText("managementCredentialStatus", "凭据已应用；刷新后需要重新输入。");
    connectEvents();
  } catch (error) {
    if (epoch !== managementCredentialEpoch) return;
    setText("managementCredentialStatus", error.message);
    throw error;
  }
}

async function authenticatedBlob(path) {
  const response = await managementFetch(path);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${response.status} ${response.statusText}`);
  }
  return response.blob();
}

function loadAuthenticatedCover(image, path) {
  authenticatedBlob(path).then((blob) => {
    if (!image.isConnected) return;
    const url = URL.createObjectURL(blob);
    image.onload = image.onerror = () => URL.revokeObjectURL(url);
    image.src = url;
  }).catch(() => { image.remove(); });
}

/* ============ ROUTER ============ */

function parseHash() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (!hash) return { page: "core" };
  const parts = hash.split("/");
  if (parts[0] === "plugins" && parts.length >= 3) {
    return { page: "plugin", category: parts[1], pluginId: decodeURIComponent(parts.slice(2).join("/")) };
  }
  if (parts[0] === "plugins" && parts.length === 2) {
    return { page: "plugins", tab: parts[1] };
  }
  if (parts[0] === "plugins") return { page: "plugins" };
  if (parts[0] === "connections" && parts.length >= 2) {
    return { page: "connection", connectionId: decodeURIComponent(parts.slice(1).join("/")) };
  }
  if (parts[0] === "connections") return { page: "connections" };
  if (parts[0] === "decision") return { page: "decision" };
  if (parts[0] === "environments") return { page: "environments" };
  if (parts[0] === "archives") return { page: "archives" };
  if (parts[0] === "logs") return { page: "logs" };
  if (parts[0] === "voice") return { page: "voice" };
  if (parts[0] === "core") return { page: "core" };
  return { page: "core" };
}

function writeHash(push = false) {
  let hash = "#/core";
  if (state.activePage === "plugins") hash = `#/plugins/${state.activePluginTab}`;
  else if (state.activePage === "connections") hash = "#/connections";
  else if (state.activePage === "decision") hash = "#/decision";
  else if (state.activePage === "environments") hash = "#/environments";
  else if (state.activePage === "archives") hash = "#/archives";
  else if (state.activePage === "connection" && state.activeConnectionId) {
    hash = `#/connections/${encodeURIComponent(state.activeConnectionId)}`;
  }
  else if (state.activePage === "logs") hash = "#/logs";
  else if (state.activePage === "voice") hash = "#/voice";
  else if (state.activePage === "plugin" && state.activePluginId) {
    hash = `#/plugins/${state.activePluginCategory || "special"}/${encodeURIComponent(state.activePluginId)}`;
  }
  if (window.location.hash !== hash) {
    history[push ? "pushState" : "replaceState"](null, "", hash);
  }
}

function switchPage(page, options = {}) {
  if (state.activePage === "decision" && page !== "decision" && window.DecisionPage?.leave() === false) {
    writeHash();
    return;
  }
  state.activePage = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${page}`));
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    const navPage = page === "connection" ? "connections" : page;
    el.classList.toggle("active", el.dataset.page === navPage);
  });
  const fab = $("pluginUploadFab");
  if (fab) fab.hidden = page !== "plugins";
  if (page === "decision") window.DecisionPage?.enter();
  if (page === "plugins") renderPluginGrid();
  if (page === "logs") renderEvents();
  if (page === "plugin") renderPluginDashboard();
  if (page === "voice") {
    refreshVoiceStatus().catch(() => {});
    refreshLiveVoice().catch(() => {});
  }
  if (page === "connections") refreshConnections({ preserveForm: true }).catch(() => {});
  if (page === "environments") refreshEnvironment().catch(() => {});
  if (page === "connection") renderConnectionDetail({ preserveForm: state.connectionDirty });
  if (!options.silent) writeHash(options.push === true);
}

/* ============ ARCHIVES ============ */

function setArchiveStatus(stateName, title, detail) {
  const status = $("archiveStatus");
  if (status) status.dataset.state = stateName;
  setText("archiveStatusTitle", title);
  setText("archiveStatusDetail", detail);
}

async function createArchive() {
  setArchiveStatus("busy", "正在打包实例数据", "正在生成 ZIP 快照");
  try {
    const data = await apiJson("/api/v1/ex/backups", { method: "POST", body: "{}" });
    const backup = data.backup || {};
    if (!backup.download_url) throw new Error("服务端未返回存档下载地址");
    const link = document.createElement("a");
    const url = URL.createObjectURL(await authenticatedBlob(backup.download_url));
    link.href = url;
    link.download = backup.filename || "astrbotex_snapshot.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const fileCount = Number(backup.file_count || 0);
    setArchiveStatus("success", "存档已生成并开始下载", `${backup.filename} · ${fileCount} 个文件`);
    showToast("实例存档已开始下载");
  } catch (error) {
    setArchiveStatus("error", "备份存档失败", error.message);
    throw error;
  }
}

async function uploadArchive(file) {
  setArchiveStatus("busy", "正在校验并恢复存档", file.name);
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const response = await managementFetch("/api/v1/ex/backups/upload", {
      method: "POST",
      body: form,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.message || data.error || `${response.status} ${response.statusText}`);
    }
    await Promise.allSettled([
      refreshStatus(),
      refreshPlugins(),
      refreshConnections({ preserveForm: false }),
    ]);
    const count = Number(data.restored_files || 0);
    setArchiveStatus("success", "存档恢复完成", `${file.name} · 已恢复 ${count} 个文件 · 建议重启服务`);
    showToast("实例存档恢复完成");
  } catch (error) {
    setArchiveStatus("error", "上传存档失败", error.message);
    throw error;
  }
}

/* ============ CONNECTIONS ============ */

function currentConnection() {
  return state.connections.find((item) => item.id === state.activeConnectionId) || null;
}

function connectionTypeMeta(type) {
  return state.connectionTypes[type] || { label: type, protocol: type, fields: [], defaults: {} };
}

function connectionEndpoint(connection) {
  const config = connection?.config || {};
  if (connection?.type === "zmq_server") return `tcp://${config.host || "0.0.0.0"}:${config.port || "--"}`;
  if (connection?.type === "zmq_client") return config.endpoint || "--";
  if (connection?.type === "websocket_server") {
    const path = String(config.path || "/").startsWith("/") ? config.path || "/" : `/${config.path}`;
    return `ws://${config.host || "0.0.0.0"}:${config.port || "--"}${path}`;
  }
  if (connection?.type === "websocket_client") return config.url || "--";
  return "--";
}

function connectionStateLabel(value) {
  return {
    starting: "启动中",
    running: "运行中",
    reconnecting: "重连中",
    stopped: "已停止",
    error: "错误",
  }[String(value || "stopped")] || String(value || "--");
}

function connectionMark(type) {
  return {
    zmq_server: "ZS",
    zmq_client: "ZC",
    websocket_server: "WS",
    websocket_client: "WC",
  }[type] || "IO";
}

async function refreshConnections(options = {}) {
  const preserveForm = options.preserveForm ?? state.connectionDirty;
  if (Object.keys(state.connectionTypes).length === 0) {
    const types = await apiJson("/api/v1/ex/connections/types");
    state.connectionTypes = types.types || {};
  }
  const result = await apiJson("/api/v1/ex/connections");
  state.connections = Array.isArray(result.connections) ? result.connections : [];
  renderConnectionGrid();
  if (state.activePage === "connection") renderConnectionDetail({ preserveForm });
  return state.connections;
}

function renderConnectionGrid() {
  const grid = $("connectionGrid");
  if (!grid) return;
  const running = state.connections.filter((item) => ["running", "reconnecting", "starting"].includes(item.runtime?.state)).length;
  const peers = state.connections.reduce((sum, item) => sum + Number(item.runtime?.clients || 0), 0);
  setText("connectionTotal", String(state.connections.length));
  setText("connectionRunning", String(running));
  setText("connectionPeers", String(peers));

  if (state.connections.length === 0) {
    grid.innerHTML = '<div class="connection-empty"><b>还没有连接</b><span>使用“新增连接”创建第一个传输通道</span></div>';
    return;
  }

  grid.innerHTML = state.connections.map((connection) => {
    const runtime = connection.runtime || {};
    const runtimeState = runtime.state || "stopped";
    const meta = connectionTypeMeta(connection.type);
    return `
      <article class="connection-card" data-connection-id="${escapeHtml(connection.id)}" tabindex="0">
        <div class="connection-card-top">
          <label class="switch connection-card-switch" title="${connection.enabled ? "停止连接" : "启动连接"}">
            <input type="checkbox" data-connection-toggle="${escapeHtml(connection.id)}" ${connection.enabled ? "checked" : ""} />
            <span></span>
          </label>
          <span class="connection-protocol">${escapeHtml(meta.protocol || connection.type)}</span>
          <span class="connection-state connection-state-${escapeHtml(runtimeState)}"><i></i>${escapeHtml(connectionStateLabel(runtimeState))}</span>
        </div>
        <div class="connection-card-body">
          <span class="connection-card-mark">${escapeHtml(connectionMark(connection.type))}</span>
          <div>
            <h2>${escapeHtml(connection.name)}</h2>
            <p>${escapeHtml(connectionEndpoint(connection))}</p>
          </div>
        </div>
        <div class="connection-card-foot">
          <span>${escapeHtml(meta.label || connection.type)}</span>
          <span>${Number(runtime.clients || 0)} 对端</span>
          <span>${Number(runtime.received || 0)} 收 / ${Number(runtime.sent || 0)} 发</span>
        </div>
      </article>`;
  }).join("");

  grid.querySelectorAll(".connection-card").forEach((card) => {
    const open = () => openConnectionDashboard(card.dataset.connectionId);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
  grid.querySelectorAll("[data-connection-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", (event) => event.stopPropagation());
    toggle.addEventListener("change", (event) => {
      event.stopPropagation();
      setConnectionEnabled(toggle.dataset.connectionToggle, toggle.checked, toggle).catch((error) => {
        toggle.checked = !toggle.checked;
        showToast(error.message, "error");
      });
    });
  });
}

function openConnectionDashboard(connectionId) {
  state.activeConnectionId = connectionId;
  state.connectionDirty = false;
  disarmConnectionDelete();
  switchPage("connection");
}

async function createConnection(type) {
  const meta = connectionTypeMeta(type);
  const suffix = Date.now().toString(36).slice(-6);
  const id = `${type.replaceAll("_", "-")}-${suffix}`;
  const result = await apiJson("/api/v1/ex/connections", {
    method: "POST",
    body: JSON.stringify({ id, name: meta.label || type, type, enabled: false, config: meta.defaults || {} }),
  });
  state.connections.push(result.connection);
  closeConnectionAddMenu();
  showToast("连接已创建");
  openConnectionDashboard(result.connection.id);
}

function renderConnectionConfig(connection) {
  const form = $("connectionConfigForm");
  if (!form) return;
  const meta = connectionTypeMeta(connection.type);
  const allFields = Array.isArray(meta.fields) ? meta.fields : Object.keys(connection.config || {});
  const channel = String(connection.config?.channel ?? meta.defaults?.channel ?? "text");
  const fields = allFields.filter((key) => {
    if (key === "channel") {
      return connection.type === "zmq_client" &&
        String(connection.config?.protocol_profile ?? meta.defaults?.protocol_profile ?? "raw") === "astrbotex";
    }
    // The upper-step identity fields are local trust anchors; showing them on a
    // text/audio/vision connection would suggest they do something there.
    if (UPPER_FIELDS.has(key)) return channel === "control";
    return true;
  });
  form.dataset.connectionId = connection.id;
  form.innerHTML = fields.map((key) => {
    const field = CONNECTION_FIELDS[key] || { label: key, type: "text" };
    const value = connection.config?.[key] ?? meta.defaults?.[key] ?? "";
    if (field.type === "select") {
      const options = (field.options || []).map(([optionValue, optionLabel]) =>
        `<option value="${escapeHtml(optionValue)}" ${String(value) === String(optionValue) ? "selected" : ""}>${escapeHtml(optionLabel)}</option>`
      ).join("");
      return `<label class="connection-field"><span>${escapeHtml(field.label)}</span><select class="config-input" data-connection-config="${escapeHtml(key)}" data-value-type="string">${options}</select></label>`;
    }
    const numeric = field.type === "number";
    return `<label class="connection-field"><span>${escapeHtml(field.label)}</span><input class="config-input" data-connection-config="${escapeHtml(key)}" data-value-type="${numeric ? "number" : "string"}" type="${escapeHtml(field.type || "text")}" value="${escapeHtml(value)}" placeholder="${escapeHtml(field.placeholder || "")}" ${isPresent(field.min) ? `min="${field.min}"` : ""} ${isPresent(field.max) ? `max="${field.max}"` : ""} ${isPresent(field.step) ? `step="${field.step}"` : ""} /></label>`;
  }).join("");
  form.querySelectorAll("input, select").forEach((input) => input.addEventListener("input", (event) => {
    markConnectionDirty();
    const changed = event.currentTarget.dataset.connectionConfig;
    if (changed === "protocol_profile" || changed === "channel") {
      // Both change which fields are relevant, so rebuild the form in place.
      connection.config = { ...connection.config, ...collectConnectionConfig(), [changed]: event.currentTarget.value };
      renderConnectionConfig(connection);
    }
  }));
}

function renderConnectionDetail(options = {}) {
  const connection = currentConnection();
  if (!connection) return;
  const preserveForm = Boolean(options.preserveForm);
  const runtime = connection.runtime || {};
  const meta = connectionTypeMeta(connection.type);
  const formBelongsToConnection = $("connectionConfigForm")?.dataset.connectionId === connection.id;

  setText("connectionDetailProtocol", meta.label || connection.type);
  setText("connectionDetailTitle", connection.name);
  setTextWithTitle("connectionDetailEndpoint", connectionEndpoint(connection));
  const typeInput = $("connectionTypeInput");
  if (typeInput) typeInput.value = meta.label || connection.type;
  const idInput = $("connectionIdInput");
  if (idInput) idInput.value = connection.id;
  if (!preserveForm || !formBelongsToConnection) {
    const nameInput = $("connectionNameInput");
    if (nameInput) nameInput.value = connection.name;
    renderConnectionConfig(connection);
  }

  const enabledInput = $("connectionEnabledInput");
  if (enabledInput) enabledInput.checked = Boolean(connection.enabled);
  setText("connectionEnableLabel", connection.enabled ? "已启用" : "已停止");
  setText("connectionStateText", connectionStateLabel(runtime.state));
  const dot = $("connectionStateDot");
  if (dot) dot.className = `connection-state-dot connection-state-dot-${runtime.state || "stopped"}`;
  setText("connectionStatClients", String(runtime.clients || 0));
  setText("connectionStatReceived", String(runtime.received || 0));
  setText("connectionStatSent", String(runtime.sent || 0));
  setText("connectionStatLast", runtime.last_message_at ? formatTime(runtime.last_message_at) : "--");

  const errorBox = $("connectionRuntimeError");
  if (errorBox) {
    errorBox.hidden = !runtime.error;
    errorBox.textContent = runtime.error || "";
  }
  renderConnectionTrace(runtime.recent_messages || []);
}

function renderConnectionTrace(messages) {
  const trace = $("connectionTrace");
  if (!trace) return;
  if (!Array.isArray(messages) || messages.length === 0) {
    trace.innerHTML = '<div class="connection-trace-empty">暂无消息</div>';
    return;
  }
  trace.innerHTML = [...messages].reverse().map((message) => `
    <div class="connection-trace-item">
      <div><span class="trace-direction trace-${message.direction === "out" ? "out" : "in"}">${message.direction === "out" ? "OUT" : "IN"}</span><time>${escapeHtml(formatTime(message.timestamp))}</time><small>${Number(message.bytes || 0)} B</small></div>
      <pre>${escapeHtml(message.preview || "")}</pre>
    </div>`).join("");
}

function markConnectionDirty() {
  state.connectionDirty = true;
  const button = $("connectionSaveButton");
  if (button) button.classList.add("has-changes");
}

function collectConnectionConfig() {
  const config = {};
  $("connectionConfigForm")?.querySelectorAll("[data-connection-config]").forEach((input) => {
    const key = input.dataset.connectionConfig;
    config[key] = input.dataset.valueType === "number" ? Number(input.value) : input.value;
  });
  return config;
}

async function saveConnection() {
  const connection = currentConnection();
  if (!connection) throw new Error("连接不存在");
  const result = await apiJson(`/api/v1/ex/connections/${encodeURIComponent(connection.id)}`, {
    method: "PUT",
    body: JSON.stringify({
      name: $("connectionNameInput")?.value.trim() || connection.name,
      type: connection.type,
      enabled: Boolean($("connectionEnabledInput")?.checked),
      config: collectConnectionConfig(),
    }),
  });
  state.connections = state.connections.map((item) => item.id === connection.id ? result.connection : item);
  state.connectionDirty = false;
  $("connectionSaveButton")?.classList.remove("has-changes");
  renderConnectionDetail({ preserveForm: false });
  showToast("连接配置已保存");
}

async function setConnectionEnabled(connectionId, enabled, input) {
  if (input) input.disabled = true;
  try {
    const action = enabled ? "start" : "stop";
    const result = await apiJson(`/api/v1/ex/connections/${encodeURIComponent(connectionId)}/${action}`, { method: "POST", body: "{}" });
    state.connections = state.connections.map((item) => item.id === connectionId ? result.connection : item);
    renderConnectionGrid();
    if (state.activeConnectionId === connectionId) renderConnectionDetail({ preserveForm: state.connectionDirty });
    showToast(enabled ? "连接已启动" : "连接已停止");
  } finally {
    if (input) input.disabled = false;
  }
}

async function sendConnectionMessage() {
  const connection = currentConnection();
  if (!connection) throw new Error("连接不存在");
  const text = $("connectionMessageInput")?.value || "";
  let data = text;
  try { data = JSON.parse(text); } catch { /* send as plain text */ }
  await apiJson(`/api/v1/ex/connections/${encodeURIComponent(connection.id)}/send`, {
    method: "POST",
    body: JSON.stringify({ data, peer: $("connectionPeerInput")?.value.trim() || null }),
  });
  showToast("消息已发送");
  await refreshConnections({ preserveForm: true });
}

function disarmConnectionDelete() {
  state.connectionDeleteArmed = false;
  if (state.connectionDeleteTimer) clearTimeout(state.connectionDeleteTimer);
  state.connectionDeleteTimer = null;
  const button = $("connectionDeleteButton");
  if (button) button.textContent = "删除连接";
}

async function handleConnectionDelete() {
  const connection = currentConnection();
  if (!connection) return;
  if (!state.connectionDeleteArmed) {
    state.connectionDeleteArmed = true;
    setText("connectionDeleteButton", "再次点击确认删除");
    state.connectionDeleteTimer = setTimeout(disarmConnectionDelete, 4000);
    return;
  }
  await apiJson(`/api/v1/ex/connections/${encodeURIComponent(connection.id)}`, { method: "DELETE" });
  state.connections = state.connections.filter((item) => item.id !== connection.id);
  state.activeConnectionId = null;
  state.connectionDirty = false;
  disarmConnectionDelete();
  showToast("连接已删除");
  switchPage("connections");
}

function closeConnectionAddMenu() {
  const menu = $("connectionAddMenu");
  const button = $("connectionAddButton");
  if (menu) menu.hidden = true;
  if (button) button.setAttribute("aria-expanded", "false");
}

/* ============ STATUS ============ */

async function refreshStatus(options = {}) {
  const { suppressToast = false } = options;
  try {
    const status = await apiJson("/api/status");
    renderStatus(status);
    setConnection(true, "已连接");
    return status;
  } catch (error) {
    setConnection(false, `连接失败`);
    if (!suppressToast) {
      throw error;
    }
    return null;
  }
}

function renderStatus(status) {
  const world = status.world || {};
  const robot = world.robot || {};
  const entities = world.entities || [];
  const zones = world.zones || [];
  const robotConnected = robot.link_ok === true;
  const estopKnown = robotConnected && typeof robot.estop === "boolean";
  state.runtimeState = String(status.runtime_state || "idle").toLowerCase();

  setText("runtimeState", String(status.runtime_state || "--").toUpperCase());
  setText("runtimeDetail", `目标频率 ${formatValue(status.tick_hz)} Hz`);
  setText("worldSummary", `${entities.length} 实体`);
  setText("worldDetail", `${zones.length} 区域，链路 ${robotConnected ? "正常" : "未连接"}`);
  setText("motionState", robotConnected ? "连接正常" : "未连接");
  setText("motionDetail", !estopKnown ? "急停状态未知" : robot.estop ? "急停触发" : "急停正常");
  setText("activeSkill", status.active_skill || "--");
  setText("skillDetail", status.active_goal ? status.active_goal.type : "--");
  setText("snapshotTime", formatTime(world.timestamp));
  setText("tickHz", `${formatValue(status.tick_hz)} Hz`);
  setText("batteryVoltage", isPresent(robot.battery_voltage) ? `${Number(robot.battery_voltage).toFixed(1)} V` : "--");
  setText("estopState", !estopKnown ? "未知" : robot.estop ? "触发" : "正常");
  setText("robotPose", formatPose(robot.pose));
  setText("goalPreview", `active_goal: ${JSON.stringify(status.active_goal, null, 2)}`);

  // status strip
  setText("stripRuntime", state.runtimeState.toUpperCase());
  setText("stripTick", isPresent(status.tick_hz) ? `${status.tick_hz} Hz` : "--");
  setText("stripBatt", isPresent(robot.battery_voltage) ? `${Number(robot.battery_voltage).toFixed(1)} V` : "--");
  setText("stripEstop", !estopKnown ? "UNKNOWN" : robot.estop ? "TRIG" : "OK");
  setText("stripSkill", status.active_skill || "--");

  renderRuntimeToggleButton();

  if (Array.isArray(status.recent_events) && !eventLogSeeded) {
    window.EXLogs?.seed(status.recent_events);
    for (const event of status.recent_events) {
      const id = event?.data?.log?.event_id || event?.event_id;
      if (validEventId(id)) rememberEventId(id);
    }
    eventLogSeeded = true;
  }
}

function renderRuntimeToggleButton(pending = "") {
  const button = $("runtimeToggleButton");
  if (!button) return;
  button.classList.remove("btn-primary", "btn-danger-solid", "btn-ghost");
  button.disabled = false;

  if (pending === "starting") {
    button.classList.add("btn-primary", "busy");
    button.textContent = "启动中";
    button.disabled = true;
    return;
  }
  if (pending === "stopping") {
    button.classList.add("btn-danger-solid", "busy");
    button.textContent = "停止中";
    button.disabled = true;
    return;
  }

  button.classList.remove("busy");
  if (state.runtimeState === "running") {
    button.classList.add("btn-primary");
    button.textContent = "运行中";
    return;
  }
  button.classList.add("btn-danger-solid");
  button.textContent = "已停止";
}

function setConnection(ok, label) {
  const pill = $("connectionPill");
  if (!pill) return;
  pill.classList.toggle("offline", !ok);
  const labelEl = pill.querySelector("[data-label]");
  if (labelEl) labelEl.textContent = label;
}

function setEventConnection(ok, label) {
  window.DecisionPage?.connectionChanged(ok);
  setText("eventStatus", label);
  setText("stripSse", ok ? "LIVE" : "RETRY");
  window.EXLogs?.setConnection(ok, label);
}

/* ============ EVENTS / LOGS ============ */

function pushEvent(event, meta = {}) { window.EXLogs?.push(event, meta); }
function renderEvents() { /* EXLogs owns its bounded cache and DOM. */ }
function toggleLogAutoscroll() { window.EXLogs?.toggleAutoscroll(); }
function clearLogs() { window.EXLogs?.clear(); }

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  if (!managementCredential) { setEventConnection(false, '等待凭据'); return; }
  const controller = new AbortController();
  const epoch = managementCredentialEpoch;
  let retryTimer = null;
  const source = {
    readyState: 0,
    close: () => { source.readyState = 2; controller.abort(); clearTimeout(retryTimer); },
  };
  state.eventSource = source;
  function receive(name, lines, id) {
    if (epoch !== managementCredentialEpoch || controller.signal.aborted || !lines.length) return;
    const payload = JSON.parse(lines.join('\n'));
    if (name === 'log_control') {
      if (payload.kind === 'reset') {
        lastEventId = null; // Stream IDs provide identity; retain bounded dedup across a cursor reset.
      }
      window.EXLogs?.control(payload);
      return;
    }
    if (name !== 'event') return;
    if (id && !validEventId(id)) throw new Error('SSE 游标无效');
    if (!payload || typeof payload !== 'object') throw new Error('SSE 事件无效');
    const duplicate = id && seenEventIds.has(id);
    if (!duplicate) {
      pushEvent(payload, id ? {eventId: id} : {});
      rememberEventId(id);
      if (payload.type === 'decision_changed') window.DecisionPage?.notify();
      if (['environment', 'environment_changed', 'ros_graph_changed', 'ros_endpoints_changed', 'ros_diagnostic'].includes(payload.type)) scheduleEnvironmentRefresh();
    }
    // Advance only after successful parsing and dispatch. Duplicate old history
    // must not move the cursor backwards after /status seeded a newer event.
    if (id) {
      const split = id.lastIndexOf(':');
      const oldSplit = lastEventId ? lastEventId.lastIndexOf(':') : -1;
      if (!lastEventId || id.slice(0, split) !== lastEventId.slice(0, oldSplit) ||
          BigInt(id.slice(split + 1)) > BigInt(lastEventId.slice(oldSplit + 1))) lastEventId = id;
    }
  }
  async function readEvents() {
    let reader = null;
    let retry = false;
    try {
      source.readyState = 0;
      const headers = {Accept: 'text/event-stream'};
      if (lastEventId) headers['Last-Event-ID'] = lastEventId;
      const response = await managementFetch('/api/events', {signal: controller.signal, headers});
      if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
      if (controller.signal.aborted || epoch !== managementCredentialEpoch) return;
      source.readyState = 1;
      setEventConnection(true, 'SSE 已连接');
      scheduleEnvironmentRefresh();
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', name = 'message', lines = [], id = null, frameSize = 0;
      while (!controller.signal.aborted && epoch === managementCredentialEpoch) {
        const chunk = await reader.read();
        if (chunk.done) throw new Error('SSE 已断开');
        buffer += decoder.decode(chunk.value, {stream: true});
        let end;
        while ((end = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, end).replace(/\r$/, '');
          buffer = buffer.slice(end + 1);
          frameSize += line.length + 1;
          if (frameSize > 131072) throw new Error('SSE 消息超出展示上限');
          if (!line) {
            receive(name, lines, id);
            name = 'message'; lines = []; id = null; frameSize = 0;
          } else if (line.startsWith('event:')) name = line.slice(6).trimStart();
          else if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
          else if (line.startsWith('id:')) id = line.slice(3).replace(/^ /, '');
        }
        if (buffer.length + frameSize > 131072) throw new Error('SSE 消息超出展示上限');
      }
    } catch {
      if (controller.signal.aborted || epoch !== managementCredentialEpoch || !managementCredential) return;
      source.readyState = 0; setEventConnection(false, 'SSE 重连中'); retry = true;
    } finally {
      if (reader) await reader.cancel().catch(() => {});
      if (retry && !controller.signal.aborted && epoch === managementCredentialEpoch && managementCredential) {
        retryTimer = setTimeout(readEvents, 1000);
      }
    }
  }
  readEvents();
}

/* ============ RUNTIME TOGGLE ============ */

async function startRuntime() {
  renderRuntimeToggleButton("starting");
  const result = await apiJson("/api/runtime/start", { method: "POST", body: "{}" });
  state.runtimeState = String(result.state || "running").toLowerCase();
  renderRuntimeToggleButton();
  await refreshStatus({ suppressToast: true });
  showToast("运行时已启动");
}

async function stopRuntime() {
  renderRuntimeToggleButton("stopping");
  const result = await apiJson("/api/runtime/stop", {
    method: "POST",
    body: JSON.stringify({ reason: "stopped from dashboard" }),
  });
  state.runtimeState = String(result.state || "idle").toLowerCase();
  renderRuntimeToggleButton();
  await refreshStatus({ suppressToast: true });
  showToast("运行时已停止");
}

async function toggleRuntime() {
  if (state.runtimeState === "running") {
    await stopRuntime();
    return;
  }
  await startRuntime();
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function handleRuntimeToggleClick() {
  const targetState = state.runtimeState === "running" ? "idle" : "running";
  try {
    await toggleRuntime();
  } catch (error) {
    await sleep(500);
    const status = await refreshStatus({ suppressToast: true });
    const actualState = String(status?.runtime_state || state.runtimeState || "idle").toLowerCase();
    if (actualState === targetState) {
      renderRuntimeToggleButton();
      return;
    }
    showToast(targetState === "running" ? "启动状态机失败" : "停止状态机失败", "error");
  }
}

/* ============ PLUGINS ============ */

function pluginsByCategory(category) {
  return state.plugins.filter((plugin) => (plugin.category || "special") === category);
}

function currentPlugin() {
  return state.plugins.find((plugin) => plugin.id === state.activePluginId) || null;
}

function currentPublishers() {
  return state.publishers.filter((item) => item.plugin_id !== state.activePluginId);
}

async function fetchPluginDetail(pluginId) {
  const data = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(pluginId)}`);
  const detailed = data.plugin;
  state.plugins = state.plugins.map((plugin) => (plugin.id === detailed.id ? detailed : plugin));
  return detailed;
}

async function refreshPublishers() {
  const data = await apiJson("/api/v1/ex/pubsub/publishers");
  state.publishers = data.publishers || [];
}

async function refreshPlugins() {
  const data = await apiJson("/api/v1/ex/plugins");
  state.plugins = data.plugins || [];
  await refreshPublishers();
  renderPluginGrid();
  renderTabCounts();
  if (state.activePluginId) {
    const matched = currentPlugin();
    if (!matched) {
      state.activePluginId = null;
      state.activePluginCategory = null;
      if (state.activePage === "plugin") switchPage("plugins");
    } else {
      if (!matched.config_schema) {
        await fetchPluginDetail(matched.id);
      }
      renderPluginDashboard();
    }
  }
}

function renderTabCounts() {
  const counts = { all: state.plugins.length };
  for (const cat of PLUGIN_CATEGORIES) {
    counts[cat] = pluginsByCategory(cat).length;
  }
  document.querySelectorAll("[data-count]").forEach((el) => {
    const key = el.dataset.count;
    el.textContent = String(counts[key] ?? 0);
  });
}

function renderPluginGrid() {
  const grid = $("pluginGrid");
  if (!grid) return;

  const tab = state.activePluginTab;
  const plugins = tab === "all" ? state.plugins : pluginsByCategory(tab);

  setText("pluginCount", String(plugins.length));
  setText("pluginPanelTitle", `已安装 · ${CATEGORY_LABELS[tab] || "全部"}`);

  document.querySelectorAll(".tab-item[data-category]").forEach((el) => {
    el.classList.toggle("active", el.dataset.category === tab);
  });

  grid.innerHTML = "";
  if (plugins.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty";
    empty.textContent = tab === "all" ? "还没有安装任何插件" : `还没有安装${CATEGORY_LABELS[tab]}插件`;
    grid.appendChild(empty);
    return;
  }

  plugins.forEach((plugin) => {
    const card = document.createElement("article");
    card.className = "plugin-card";
    card.addEventListener("click", () => {
      openPluginDashboard(plugin).catch((error) => showToast(error.message, "error"));
    });

    const toggle = document.createElement("label");
    toggle.className = "switch plugin-switch";
    toggle.title = plugin.enabled ? "停用" : "启用";
    toggle.addEventListener("click", (event) => event.stopPropagation());
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(plugin.enabled);
    input.addEventListener("change", () => {
      setPluginEnabled(plugin.id, input.checked, input).catch((error) => showToast(error.message, "error"));
    });
    toggle.appendChild(input);
    toggle.appendChild(document.createElement("span"));

    const cover = document.createElement("div");
    cover.className = "plugin-cover";
    if (plugin.cover_url) {
      const image = document.createElement("img");
      image.alt = plugin.name;
      cover.appendChild(image);
      loadAuthenticatedCover(image, plugin.cover_url);
    } else {
      cover.innerHTML = `<span>${escapeHtml((plugin.name || plugin.id).slice(0, 2).toUpperCase())}</span>`;
    }

    const body = document.createElement("div");
    body.className = "plugin-body";
    const tags = (plugin.provides || []).map((item) => `<span class="plugin-tag">${escapeHtml(item)}</span>`).join("");
    const statusClass = plugin.error ? "state-err" : plugin.enabled ? "state-ok" : "";
    body.innerHTML = `
      <h2 class="plugin-name">${escapeHtml(plugin.name)}</h2>
      <p class="plugin-desc">${escapeHtml(plugin.description || "暂无说明")}</p>
      <div class="plugin-tags">${tags}</div>
      <div class="plugin-meta"><span class="${statusClass}">${escapeHtml(plugin.status)}</span> · v${escapeHtml(plugin.version || "--")}</div>
    `;

    card.appendChild(toggle);
    card.appendChild(cover);
    card.appendChild(body);
    grid.appendChild(card);
  });
}

async function openPluginDashboard(plugin) {
  state.activePluginId = plugin.id;
  state.activePluginCategory = plugin.category || "special";
  if (!plugin.config_schema) {
    await fetchPluginDetail(plugin.id);
  }
  await refreshPublishers();
  resetDirty();
  disarmUninstall();
  renderPluginDashboard();
  switchPage("plugin");
}

function renderPluginDashboard() {
  const plugin = currentPlugin();
  const empty = $("pluginDashboardEmpty");
  const body = $("pluginDashboardBody");
  if (!plugin) {
    empty.hidden = false;
    body.hidden = true;
    setText("pluginDashboardTitle", "插件控制台");
    setText("pluginDashboardSubtitle", "选择一个插件进入");
    return;
  }

  empty.hidden = true;
  body.hidden = false;
  setPluginDashboardTitle(plugin.name || plugin.id);
  setText("pluginDashboardSubtitle", `${CATEGORY_LABELS[plugin.category] || "特殊"} / ${plugin.id}`);
  setText("pluginDashboardName", plugin.name || "--");
  setText("pluginDashboardDescription", plugin.description || "暂无说明");
  setText("pluginDashboardId", plugin.id || "--");
  setText("pluginDashboardVersion", plugin.version || "--");
  setText("pluginDashboardAuthor", plugin.author || "--");
  setText("pluginDashboardState", plugin.error ? `${plugin.status}: ${plugin.error}` : plugin.status);
  setText("pluginDashboardPath", plugin.path || "--");
  setText("pluginDashboardProvides", (plugin.provides || []).join(", ") || "--");
  $("pluginEnabledSwitch").checked = Boolean(plugin.enabled);

  const cover = $("pluginDashboardCover");
  cover.innerHTML = "";
  if (plugin.cover_url) {
    const image = document.createElement("img");
    image.alt = plugin.name;
    cover.appendChild(image);
    loadAuthenticatedCover(image, plugin.cover_url);
  } else {
    cover.innerHTML = `<span>${escapeHtml((plugin.name || plugin.id).slice(0, 2).toUpperCase())}</span>`;
  }

  renderPluginConfigForm(plugin);
  renderPluginPubSub(plugin);
  renderPluginRos(plugin);
}

function normalizeSchemaField(key, schema) {
  const fallbackType = typeof schema.default === "boolean"
    ? "boolean"
    : typeof schema.default === "number"
      ? (Number.isInteger(schema.default) ? "integer" : "number")
      : "string";
  return {
    key,
    type: schema.type || (Array.isArray(schema.enum) ? "string" : fallbackType),
    title: schema.title || key,
    required: false,
    enum: Array.isArray(schema.enum) ? schema.enum : null,
    defaultValue: schema.default,
    minimum: schema.minimum,
    maximum: schema.maximum,
    items: schema.items || null,
    description: schema.description || "",
  };
}

function renderPluginConfigForm(plugin) {
  const container = $("pluginConfigFields");
  container.innerHTML = "";
  const schema = plugin.config_schema || {};
  const properties = schema.properties || {};
  const required = Array.isArray(schema.required) ? schema.required : [];
  const config = plugin.config || {};

  const entries = Object.entries(properties).filter(([key, fieldSchema]) => key !== "pubsub" && fieldSchema?.type !== "object");
  if (entries.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty compact";
    empty.textContent = "这个插件没有声明配置项";
    container.appendChild(empty);
    return;
  }

  entries.forEach(([key, rawSchema]) => {
    const field = normalizeSchemaField(key, rawSchema || {});
    field.required = required.includes(key);
    const value = Object.prototype.hasOwnProperty.call(config, key) ? config[key] : field.defaultValue;

    const row = document.createElement("label");
    row.className = "config-field";
    row.setAttribute("for", `config-${field.key}`);

    const head = document.createElement("div");
    head.className = "config-field-head";
    head.innerHTML = `
      <span>${escapeHtml(field.title)}</span>
      ${field.required ? '<b class="required-mark">必填</b>' : ""}
    `;
    row.appendChild(head);

    let input;
    if (field.enum) {
      input = document.createElement("select");
      field.enum.forEach((optionValue) => {
        const option = document.createElement("option");
        option.value = String(optionValue);
        option.textContent = String(optionValue);
        if (String(value) === String(optionValue)) option.selected = true;
        input.appendChild(option);
      });
    } else if (field.type === "boolean") {
      input = document.createElement("select");
      [["true", "是"], ["false", "否"]].forEach(([optionValue, label]) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = label;
        if (String(Boolean(value)) === optionValue) option.selected = true;
        input.appendChild(option);
      });
    } else if (field.type === "integer" || field.type === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.step = field.type === "integer" ? "1" : "any";
      if (isPresent(field.minimum)) input.min = String(field.minimum);
      if (isPresent(field.maximum)) input.max = String(field.maximum);
      if (isPresent(value)) input.value = String(value);
    } else if (field.type === "array") {
      input = document.createElement("input");
      input.type = "text";
      input.value = Array.isArray(value) ? value.join(", ") : "";
      input.dataset.configItemType = field.items?.type || "string";
    } else {
      input = document.createElement("input");
      input.type = "text";
      if (isPresent(value)) input.value = String(value);
    }

    input.id = `config-${field.key}`;
    input.dataset.configKey = field.key;
    input.dataset.configType = field.type;
    if (field.required) input.dataset.required = "true";
    input.className = "config-input";
    input.addEventListener("input", () => markDirty("config"));
    input.addEventListener("change", () => markDirty("config"));
    row.appendChild(input);

    if (field.description || field.type === "array") {
      const note = document.createElement("small");
      note.className = "config-note";
      note.textContent = [field.description, field.type === "array" ? "多个值请使用英文逗号分隔。" : ""].filter(Boolean).join(" ");
      row.appendChild(note);
    }

    container.appendChild(row);
  });
}

function renderPluginPubSub(plugin) {
  const publishes = Array.isArray(plugin.publishes) ? plugin.publishes : [];
  const pubsub = plugin.pubsub || { publish_enabled: false, enabled_topics: [], subscriptions: [] };
  const enabledTopics = new Set(pubsub.enabled_topics || []);

  const publishMaster = $("pluginPublishEnabled");
  if (publishMaster) publishMaster.checked = Boolean(pubsub.publish_enabled);

  const publishList = $("pluginPublishList");
  publishList.innerHTML = "";
  if (publishes.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty compact";
    empty.textContent = "这个插件没有声明可发布 topic";
    publishList.appendChild(empty);
  } else {
    publishes.forEach((item) => {
      const row = document.createElement("label");
      row.className = "pubsub-topic-row";
      row.innerHTML = `
        <span class="pubsub-topic-main">
          <b>${escapeHtml(item.label || item.topic)}</b>
          <small>${escapeHtml(item.topic)}${item.schema ? ` · ${escapeHtml(item.schema)}` : ""}</small>
        </span>
      `;
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.dataset.publishTopic = item.topic;
      toggle.checked = enabledTopics.has(item.topic);
      toggle.addEventListener("change", () => markDirty("pubsub"));
      row.appendChild(toggle);
      publishList.appendChild(row);
    });
  }

  renderPublisherSelect(plugin);
  renderSubscriptionList(plugin);
}

function renderPublisherSelect(plugin) {
  const select = $("subscriptionPublisherSelect");
  select.innerHTML = '<option value="">先选择来源插件</option>';
  currentPublishers().forEach((publisher) => {
    const option = document.createElement("option");
    option.value = publisher.plugin_id;
    const stateLabel = publisher.publish_enabled ? "已启用发布" : "已声明未启用";
    option.textContent = `${publisher.name} (${publisher.plugin_id}) · ${stateLabel}`;
    select.appendChild(option);
  });
  renderPublisherTopicOptions(select.value, plugin);
}

function renderPublisherTopicOptions(publisherId, plugin) {
  const select = $("subscriptionTopicSelect");
  select.innerHTML = '<option value="">再选择 topic</option>';
  const publisher = state.publishers.find((item) => item.plugin_id === publisherId);
  if (!publisher) return;

  const allowedSchemas = new Set((plugin.subscribes || []).map((item) => item.schema).filter(Boolean));
  const allowedTopics = plugin.subscribes || [];
  const hasDeclaredSubscriptions = allowedTopics.length > 0;

  publisher.topics.forEach((item) => {
    const schemaAllowed = allowedSchemas.size === 0 || !item.schema || allowedSchemas.has(item.schema);
    const topicAllowed = !hasDeclaredSubscriptions || allowedTopics.some((declared) => declared.schema === item.schema || declared.topic === item.topic);
    if (!schemaAllowed || !topicAllowed) return;
    const option = document.createElement("option");
    option.value = item.topic;
    option.textContent = `${item.label || item.topic} (${item.topic})${item.enabled ? "" : " · 未启用发布"}`;
    option.dataset.label = item.label || item.topic;
    select.appendChild(option);
  });
}

function renderSubscriptionList(plugin) {
  const list = $("pluginSubscriptionList");
  list.innerHTML = "";
  const pubsub = plugin.pubsub || { subscriptions: [] };
  const subscriptions = Array.isArray(pubsub.subscriptions) ? pubsub.subscriptions : [];
  if (subscriptions.length === 0) {
    const empty = document.createElement("div");
    empty.className = "plugin-empty compact";
    empty.textContent = "当前没有订阅任何 topic";
    list.appendChild(empty);
    return;
  }

  subscriptions.forEach((item, index) => {
    const publisher = state.publishers.find((entry) => entry.plugin_id === item.plugin_id);
    const topic = publisher?.topics?.find((entry) => entry.topic === item.topic);
    const row = document.createElement("div");
    row.className = "subscription-item";
    row.innerHTML = `
      <div>
        <b>${escapeHtml(topic?.label || item.topic)}</b>
        <small>${escapeHtml(item.plugin_id)} · ${escapeHtml(item.topic)}</small>
      </div>
    `;
    const remove = document.createElement("button");
    remove.className = "btn btn-ghost btn-sm";
    remove.textContent = "移除";
    remove.addEventListener("click", () => removeSubscription(index));
    row.appendChild(remove);
    list.appendChild(row);
  });
}

function currentPubsubDraft(plugin) {
  const current = plugin.pubsub || { publish_enabled: false, enabled_topics: [], subscriptions: [] };
  return {
    publish_enabled: Boolean(current.publish_enabled),
    enabled_topics: [...(current.enabled_topics || [])],
    subscriptions: [...(current.subscriptions || [])],
  };
}

function collectPluginConfig() {
  const payload = {};
  const missing = [];
  const inputs = Array.from(document.querySelectorAll("[data-config-key]"));
  for (const input of inputs) {
    const key = input.dataset.configKey;
    const type = input.dataset.configType;
    const required = input.dataset.required === "true";
    let value = input.value;

    if (!input.checkValidity()) {
      input.reportValidity();
      throw new Error(`${key} 超出允许范围或格式不正确`);
    }

    if (required && !String(value).trim()) {
      missing.push(key);
      continue;
    }
    if (type === "array") {
      const itemType = input.dataset.configItemType || "string";
      value = String(value).split(",").map((item) => item.trim()).filter(Boolean);
      if (itemType === "integer") value = value.map((item) => Number.parseInt(item, 10));
      if (itemType === "number") value = value.map((item) => Number(item));
    } else if (type === "boolean") {
      value = value === "true";
    } else if (type === "integer" || type === "number") {
      if (!String(value).trim()) {
        throw new Error(`${key} 需要有效数字`);
      }
      value = type === "integer" ? Number.parseInt(value, 10) : Number(value);
      if (!Number.isFinite(value)) {
        throw new Error(`${key} 需要有效数字`);
      }
    }
    payload[key] = value;
  }
  if (missing.length > 0) {
    throw new Error(`缺少必填项: ${missing.join(", ")}`);
  }
  return payload;
}

function collectPubsubConfig(plugin) {
  const payload = currentPubsubDraft(plugin);
  payload.publish_enabled = Boolean($("pluginPublishEnabled").checked);
  payload.enabled_topics = Array.from(document.querySelectorAll("[data-publish-topic]:checked")).map((input) => input.dataset.publishTopic);
  return payload;
}

async function setPluginEnabled(pluginId, enabled, input = null) {
  const action = enabled ? "enable" : "disable";
  const previousEnabled = Boolean(state.plugins.find((plugin) => plugin.id === pluginId)?.enabled);
  if (input) input.disabled = true;
  try {
    const result = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(pluginId)}/${action}`, {
      method: "POST",
      body: "{}",
    });
    const updated = result.plugin;
    if (updated.error || updated.status === "fault") {
      throw new Error(updated.error || `${updated.name}进入故障状态`);
    }
    state.plugins = state.plugins.map((plugin) => (plugin.id === updated.id ? updated : plugin));
    renderPluginGrid();
    renderTabCounts();
    renderPluginDashboard();
    await refreshStatus({ suppressToast: true });
    showToast(`${updated.name}已${enabled ? "启用" : "停用"}`);
  } catch (error) {
    if (input) input.checked = previousEnabled;
    try {
      await refreshPlugins();
    } catch {
      // Keep the last confirmed local state when reconciliation is unavailable.
    }
    throw error;
  } finally {
    if (input) input.disabled = false;
  }
}

async function savePluginConfig() {
  const plugin = currentPlugin();
  if (!plugin) return;
  const payload = collectPluginConfig();
  const result = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(plugin.id)}/config`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const updated = result.plugin;
  state.plugins = state.plugins.map((item) => (item.id === updated.id ? updated : item));
  clearDirty("config");
  renderPluginDashboard();
  showToast("配置已保存");
}

async function savePluginPubsub() {
  const plugin = currentPlugin();
  if (!plugin) return;
  const payload = collectPubsubConfig(plugin);
  const result = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(plugin.id)}/pubsub`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const updated = result.plugin;
  state.plugins = state.plugins.map((item) => (item.id === updated.id ? updated : item));
  await refreshPublishers();
  clearDirty("pubsub");
  renderPluginDashboard();
  showToast("发布 / 订阅配置已保存");
}

function addSubscription() {
  const plugin = currentPlugin();
  if (!plugin) return;
  const publisherId = $("subscriptionPublisherSelect").value;
  const topic = $("subscriptionTopicSelect").value;
  if (!publisherId || !topic) {
    showToast("请先选择来源插件和 topic", "error");
    return;
  }
  const pubsub = currentPubsubDraft(plugin);
  const exists = pubsub.subscriptions.some((item) => item.plugin_id === publisherId && item.topic === topic);
  if (exists) {
    showToast("这个订阅已经存在", "error");
    return;
  }
  pubsub.subscriptions.push({ plugin_id: publisherId, topic });
  plugin.pubsub = pubsub;
  markDirty("pubsub");
  renderSubscriptionList(plugin);
}

function removeSubscription(index) {
  const plugin = currentPlugin();
  if (!plugin) return;
  const pubsub = currentPubsubDraft(plugin);
  pubsub.subscriptions.splice(index, 1);
  plugin.pubsub = pubsub;
  markDirty("pubsub");
  renderSubscriptionList(plugin);
}

/* ============ UNINSTALL (two-step) ============ */

function disarmUninstall() {
  state.uninstallArmed = false;
  if (state.uninstallTimer) {
    clearTimeout(state.uninstallTimer);
    state.uninstallTimer = null;
  }
  const btn = $("pluginUninstallButton");
  if (!btn) return;
  btn.textContent = "卸载插件";
  btn.classList.remove("btn-danger-solid");
  btn.classList.add("btn-danger");
}

async function handleUninstallClick(event) {
  const plugin = currentPlugin();
  if (!plugin) return;
  const btn = event.currentTarget;

  if (!state.uninstallArmed) {
    state.uninstallArmed = true;
    btn.textContent = "确认卸载？再点一次";
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-danger-solid");
    state.uninstallTimer = setTimeout(() => {
      disarmUninstall();
    }, 4000);
    return;
  }

  if (state.uninstallTimer) {
    clearTimeout(state.uninstallTimer);
    state.uninstallTimer = null;
  }
  state.uninstallArmed = false;

  await runAction(btn, "卸载中", async () => {
    await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(plugin.id)}`, { method: "DELETE" });
    showToast(`已卸载${plugin.name}`);
    const category = plugin.category || "special";
    state.activePluginId = null;
    state.activePluginCategory = null;
    state.activePluginTab = category;
    disarmUninstall();
    await refreshPlugins();
    switchPage("plugins");
    await refreshStatus();
  });
  disarmUninstall();
}

/* ============ UPLOAD ============ */

async function uploadPluginZip(file, category) {
  const form = new FormData();
  form.append("file", file);
  form.append("category", category);
  const response = await managementFetch("/api/v1/ex/plugins/upload", {
    method: "POST",
    body: form,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || `${response.status} ${response.statusText}`);
  }
  await refreshPlugins();
  const plugin = state.plugins.find((item) => item.id === data.plugin.id) || data.plugin;
  await openPluginDashboard(plugin);
  showToast(`已安装${plugin.name}`);
}

function handleUploadClick() {
  let category = state.activePluginTab;
  if (category === "all") {
    const choice = window.prompt(
      `选择插件类别（输入编号）:\n${PLUGIN_CATEGORIES.map((c, i) => `  ${i + 1}. ${CATEGORY_LABELS[c]} (${c})`).join("\n")}`,
      "1"
    );
    if (!choice) return;
    const idx = Number.parseInt(choice, 10) - 1;
    if (Number.isNaN(idx) || idx < 0 || idx >= PLUGIN_CATEGORIES.length) {
      showToast("类别选择无效", "error");
      return;
    }
    category = PLUGIN_CATEGORIES[idx];
  }
  const input = $("pluginZipInput");
  input.dataset.uploadCategory = category;
  input.click();
}

/* ============ DIRTY TRACKING ============ */

function markDirty(which) {
  if (which === "config") state.configDirty = true;
  if (which === "pubsub") state.pubsubDirty = true;
  updateDirtyUI();
}

function clearDirty(which) {
  if (which === "config") state.configDirty = false;
  if (which === "pubsub") state.pubsubDirty = false;
  updateDirtyUI();
}

function resetDirty() {
  state.configDirty = false;
  state.pubsubDirty = false;
  updateDirtyUI();
}

function updateDirtyUI() {
  const configDot = $("pluginSaveButton")?.querySelector(".dirty-dot");
  if (configDot) configDot.hidden = !state.configDirty;
  const pubsubDot = $("pluginPubsubSaveButton")?.querySelector(".dirty-dot");
  if (pubsubDot) pubsubDot.hidden = !state.pubsubDirty;

  const title = $("pluginDashboardTitle");
  if (!title) return;
  let flag = title.querySelector(".unsaved-flag");
  if (state.configDirty || state.pubsubDirty) {
    if (!flag) {
      flag = document.createElement("span");
      flag.className = "unsaved-flag";
      flag.textContent = "未保存";
      title.appendChild(flag);
    }
  } else if (flag) {
    flag.remove();
  }
}

function setPluginDashboardTitle(text) {
  const title = $("pluginDashboardTitle");
  if (!title) return;
  title.textContent = text;
  updateDirtyUI();
}

window.addEventListener("beforeunload", (event) => {
  if (state.configDirty || state.pubsubDirty || state.connectionDirty || environmentHasDrafts() || window.DecisionPage?.hasDraft()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

/* ============ BINDINGS ============ */

function bindActions() {
  window.DecisionPage?.init();
  setText("apiBaseLabel", API_BASE.replace(/^https?:\/\//, ""));
  $("managementCredentialApply").addEventListener("click", (event) =>
    runAction(event.currentTarget, "验证中", applyManagementCredential)
  );
  $("managementCredential").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("managementCredentialApply").click(); }
  });

  document.querySelectorAll(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      switchPage(button.dataset.page, { push: true });
    });
  });

  $("refreshButton").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", () => refreshStatus()));
  $("runtimeToggleButton").addEventListener("click", () => {
    handleRuntimeToggleClick().catch(() => showToast("状态机切换失败", "error"));
  });

  document.querySelectorAll(".tab-item[data-category]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activePluginTab = button.dataset.category;
      renderPluginGrid();
      writeHash();
    });
  });

  $("pluginRefreshButton").addEventListener("click", (event) =>
    runAction(event.currentTarget, "刷新中", refreshPlugins)
  );

  const fab = $("pluginUploadFab");
  if (fab) fab.addEventListener("click", handleUploadClick);

  $("pluginZipInput").addEventListener("change", (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    const category = event.currentTarget.dataset.uploadCategory;
    if (!file || !category) return;
    const fabEl = $("pluginUploadFab");
    runAction(fabEl, "+", () => uploadPluginZip(file, category));
    event.currentTarget.value = "";
  });

  bindEnvironmentActions();

  $("archiveBackupButton")?.addEventListener("click", (event) =>
    runAction(event.currentTarget, "正在打包…", createArchive)
  );
  $("archiveUploadButton")?.addEventListener("click", () => {
    const confirmed = window.confirm("上传存档会覆盖当前 profiles 和 plugins 数据，并停止运行时。是否继续选择 ZIP？");
    if (!confirmed) return;
    const input = $("archiveZipInput");
    if (!input) return;
    input.value = "";
    input.click();
  });
  $("archiveZipInput")?.addEventListener("change", (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    if (!file) return;
    runAction($("archiveUploadButton"), "正在上传…", () => uploadArchive(file));
    event.currentTarget.value = "";
  });

  $("pluginBackButton").addEventListener("click", () => {
    if (state.activePluginCategory) {
      state.activePluginTab = state.activePluginCategory;
    }
    switchPage("plugins");
  });

  $("pluginEnabledSwitch").addEventListener("change", (event) => {
    const plugin = currentPlugin();
    if (!plugin) return;
    setPluginEnabled(plugin.id, event.currentTarget.checked, event.currentTarget).catch((error) => showToast(error.message, "error"));
  });
  $("pluginSaveButton").addEventListener("click", (event) => runAction(event.currentTarget, "保存中", savePluginConfig));
  $("pluginPubsubSaveButton").addEventListener("click", (event) => runAction(event.currentTarget, "保存中", savePluginPubsub));
  $("pluginUninstallButton").addEventListener("click", (event) => {
    handleUninstallClick(event).catch((error) => showToast(error.message, "error"));
  });
  $("pluginPublishEnabled").addEventListener("change", () => markDirty("pubsub"));
  $("subscriptionPublisherSelect").addEventListener("change", (event) => {
    const plugin = currentPlugin();
    if (!plugin) return;
    renderPublisherTopicOptions(event.currentTarget.value, plugin);
  });
  $("subscriptionAddButton").addEventListener("click", addSubscription);


  const voiceRefreshButton = $("voiceRefreshButton");
  if (voiceRefreshButton) {
    voiceRefreshButton.addEventListener("click", (event) =>
      runAction(event.currentTarget, "刷新中", refreshVoiceStatus)
    );
  }
  const voiceTestTextButton = $("voiceTestTextButton");
  if (voiceTestTextButton) {
    voiceTestTextButton.addEventListener("click", (event) =>
      runAction(event.currentTarget, "发送中", testSendText)
    );
  }
  const voiceTestTtsButton = $("voiceTestTtsButton");
  if (voiceTestTtsButton) {
    voiceTestTtsButton.addEventListener("click", (event) =>
      runAction(event.currentTarget, "合成中", testTts)
    );
  }
  const voiceTestSttButton = $("voiceTestSttButton");
  if (voiceTestSttButton) {
    voiceTestSttButton.addEventListener("click", (event) =>
      runAction(event.currentTarget, "识别中", testStt)
    );
  }

  $("connectionAddButton")?.addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = $("connectionAddMenu");
    if (!menu) return;
    menu.hidden = !menu.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
  });
  $("connectionAddMenu")?.addEventListener("click", (event) => event.stopPropagation());
  document.querySelectorAll("[data-connection-type]").forEach((button) => {
    button.addEventListener("click", (event) => {
      runAction(event.currentTarget, "创建中", () => createConnection(button.dataset.connectionType));
    });
  });
  document.addEventListener("click", closeConnectionAddMenu);
  $("connectionRefreshButton")?.addEventListener("click", (event) =>
    runAction(event.currentTarget, "刷新中", () => refreshConnections({ preserveForm: true }))
  );
  $("connectionBackButton")?.addEventListener("click", () => switchPage("connections"));
  $("connectionDetailRefreshButton")?.addEventListener("click", (event) =>
    runAction(event.currentTarget, "刷新中", async () => {
      state.connectionDirty = false;
      $("connectionSaveButton")?.classList.remove("has-changes");
      await refreshConnections({ preserveForm: false });
    })
  );
  $("connectionNameInput")?.addEventListener("input", markConnectionDirty);
  $("connectionEnabledInput")?.addEventListener("change", (event) => {
    const connection = currentConnection();
    if (!connection) return;
    setConnectionEnabled(connection.id, event.currentTarget.checked, event.currentTarget).catch((error) => {
      event.currentTarget.checked = !event.currentTarget.checked;
      showToast(error.message, "error");
    });
  });
  $("connectionSaveButton")?.addEventListener("click", (event) =>
    runAction(event.currentTarget, "保存中", saveConnection)
  );
  $("connectionSendButton")?.addEventListener("click", (event) =>
    runAction(event.currentTarget, "发送中", sendConnectionMessage)
  );
  $("connectionDeleteButton")?.addEventListener("click", (event) => {
    if (!state.connectionDeleteArmed) {
      handleConnectionDelete().catch((error) => showToast(error.message, "error"));
      return;
    }
    runAction(event.currentTarget, "删除中", handleConnectionDelete);
  });

  window.addEventListener("hashchange", () => {
    applyRoute(parseHash());
  });
}

/* ============ ROUTE APPLICATION ============ */

async function applyRoute(route) {
  if (route.page === "decision") { switchPage("decision", { silent: true }); return; }
  if (route.page === "connection" && route.connectionId) {
    state.activeConnectionId = route.connectionId;
    state.connectionDirty = false;
    try {
      await refreshConnections({ preserveForm: false });
    } catch (error) {
      showToast(error.message, "error");
    }
    if (!currentConnection()) {
      showToast(`连接 ${route.connectionId} 不存在`, "error");
      switchPage("connections");
      return;
    }
    disarmConnectionDelete();
    switchPage("connection", { silent: true });
    return;
  }
  if (route.page === "connections") {
    switchPage("connections", { silent: true });
    return;
  }
  if (route.page === "environments") {
    switchPage("environments", { silent: true });
    return;
  }
  if (route.page === "archives") {
    switchPage("archives", { silent: true });
    return;
  }
  if (route.page === "plugin" && route.pluginId) {
    state.activePluginId = route.pluginId;
    state.activePluginCategory = route.category || null;
    let plugin = currentPlugin();
    if (!plugin) {
      try {
        plugin = await fetchPluginDetail(route.pluginId);
      } catch {
        showToast(`插件 ${route.pluginId} 不存在`, "error");
        switchPage("plugins");
        return;
      }
    } else if (!plugin.config_schema) {
      try {
        await fetchPluginDetail(plugin.id);
      } catch { /* non-fatal */ }
    }
    try {
      await refreshPublishers();
    } catch { /* non-fatal */ }
    resetDirty();
    disarmUninstall();
    switchPage("plugin", { silent: true });
    return;
  }
  if (route.page === "plugins") {
    if (route.tab && (route.tab === "all" || PLUGIN_CATEGORIES.includes(route.tab))) {
      state.activePluginTab = route.tab;
    }
    switchPage("plugins", { silent: true });
    return;
  }
  if (route.page === "logs") {
    switchPage("logs", { silent: true });
    return;
  }
  if (route.page === "voice") {
    switchPage("voice", { silent: true });
    return;
  }
  switchPage("core", { silent: true });
}

async function refreshVoiceStatus() {
  try {
    const s = await apiJson("/api/v1/ex/interaction/status");
    setText("voiceAstrBotReachable", s.astrbot_reachable ? "已连接" : "未连接");
    const reachEl = $("voiceAstrBotReachable");
    if (reachEl) {
      reachEl.className = s.astrbot_reachable ? "state-ok" : "state-err";
    }
    setText("voiceAstrBotUrl", s.astrbot_base_url || "--");
    setTextWithTitle("voiceSttProvider", formatProviderLabel(s.stt_provider), "未配置");
    setTextWithTitle("voiceTtsProvider", formatProviderLabel(s.tts_provider), "未配置");
    const micCount = Array.isArray(s.mic_plugins) ? s.mic_plugins.length : 0;
    const spkCount = Array.isArray(s.speaker_plugins) ? s.speaker_plugins.length : 0;
    setText("voiceMicCount", String(micCount));
    setText("voiceSpeakerCount", `扬声器 ${spkCount}`);
    setText("voiceDetailUrl", s.astrbot_base_url || "--");
    setText("voiceDetailMic", micCount > 0 ? s.mic_plugins.join(", ") : "无");
    setText("voiceDetailSpeaker", spkCount > 0 ? s.speaker_plugins.join(", ") : "无");
    setText("voiceDetailSttProxy", s.stt_proxy || "未启用");
    setText("voiceDetailTtsProxy", s.tts_proxy || "未启用");
    setText(
      "voiceDetailLastStt",
      s.last_stt_at ? new Date(s.last_stt_at * 1000).toLocaleString() : "--"
    );
    setText(
      "voiceDetailLastTts",
      s.last_tts_audio_at ? new Date(s.last_tts_audio_at * 1000).toLocaleString() : "--"
    );
    setText(
      "voiceDetailError",
      s.last_error ? `${s.last_error.operation}: ${s.last_error.error}` : (s.astrbot_error || "无")
    );
  } catch (error) {
    setText("voiceAstrBotReachable", "错误");
    throw error;
  }
}

async function testSendText() {
  const result = await apiJson("/api/v1/ex/interaction/message", {
    method: "POST",
    body: JSON.stringify({ text: "Dashboard 手动测试消息", session_id: "astrbotex_default" }),
  });
  setText("voiceTestResult", result.ok ? "发送成功" : `失败: ${result.error || "unknown"}`);
  showToast(result.ok ? "文本已发送到 AstrBot" : "发送失败");
}

async function testTts() {
  const result = await apiJson("/api/v1/ex/interaction/tts", {
    method: "POST",
    body: JSON.stringify({ text: "你好，这是语音合成测试。" }),
  });
  if (result.ok) {
    setText("voiceTestResult", `TTS 成功: ${result.audio_url || "ok"}`);
    showToast("TTS 合成成功");
  } else {
    setText("voiceTestResult", `TTS 失败: ${result.error || "unknown"}`);
    showToast(`TTS 失败: ${result.error}`, "error");
  }
}

async function testStt() {
  const input = $("voiceSttFile");
  const file = input?.files?.[0];
  if (!file) throw new Error("请选择音频文件");
  const audioUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(reader.result));
    reader.addEventListener("error", () => reject(reader.error || new Error("读取音频失败")));
    reader.readAsDataURL(file);
  });
  const result = await apiJson("/api/v1/ex/interaction/stt", {
    method: "POST",
    body: JSON.stringify({ audio_url: audioUrl }),
  });
  setText("voiceTestResult", `STT: ${result.text || "（空结果）"}`);
  showToast("STT 识别完成");
  await refreshVoiceStatus();
}

/* ============ BOOT ============ */

async function boot() {
  window.EXLogs?.mount();
  bindActions();
  renderEvents();
  renderRuntimeToggleButton();
  const fab = $("pluginUploadFab");
  if (fab) fab.hidden = true;

  refreshStatus().catch(() => {});
  refreshEnvironment().catch(() => {});
  await refreshPlugins().catch(() => {});
  connectEvents();

  await applyRoute(parseHash());

  setInterval(() => {
    refreshStatus({ suppressToast: true }).catch(() => {});
    if (state.activePage === "connections" || state.activePage === "connection") {
      refreshConnections({ preserveForm: true }).catch(() => {});
    }
    if (state.activePage === "voice") refreshLiveVoice().catch(() => {});
  }, 2000);
}

const liveVoice = {
  config: null, devices: [], fetching: false, initialized: false, busy: false,
  profile: {}, seen: new Set(), messages: [], mode: "chat", lastUser: "", lastUserAt: 0,
  // 清空时记下的时间水位：早于它的事件不再渲染（刷新页面后也保持清空）
  clearBefore: Number(localStorage.getItem("exVoiceClearBefore") || 0),
};
const voicePathFields = ["python_executable", "vosk_model_path", "speech_config_path", "chat_config_path", "gate_config_path", "provider_id"];

function voiceSelect(direction, selected) {
  const element = $(direction === "input" ? "voiceLiveInput" : "voiceLiveOutput");
  if (!element) return;
  element.replaceChildren();
  const add = (label, value, disabled = false) => {
    const option = document.createElement("option");
    option.textContent = label;
    option.value = JSON.stringify(value);
    option.disabled = disabled;
    element.append(option);
    return option;
  };
  add("系统默认", null);
  for (const device of liveVoice.devices.filter((item) => item[`${direction}_channels`] > 0)) {
    add(device.label || `${device.name} (${device.host_api})`, {
      id: device.id,
      name: device.name,
      host_api: device.host_api,
    }, device.selectable === false);
  }
  const identity = (value) => value && typeof value === "object" ? value.id : value;
  const match = Array.from(element.options).find((option) => identity(JSON.parse(option.value)) === identity(selected));
  if (match) match.selected = true;
  else add("已保存设备暂不可用，请重选", selected).selected = true;
}

async function voiceCall(path, body) {
  const result = await apiJson(`/api/v1/ex/voice/${path}`, body === undefined ? undefined : {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (result.ok === false) throw new Error(result.error || result.message || "语音操作失败");
  return result;
}

/* ===================================================================
   连续语音：状态、配置与聊天渲染
   =================================================================== */

function voiceSetResult(id, text, ok) {
  const node = $(id);
  if (!node) return;
  node.textContent = text || "";
  node.className = "voice-inline-result" + (ok === true ? " ok" : ok === false ? " err" : "");
}

function voiceBusy(busy) {
  liveVoice.busy = busy;
  document.querySelectorAll(".voice-live-fields input, .voice-live-fields select, .voice-live-fields button").forEach((element) => {
    element.disabled = busy;
  });
  if ($("voiceLiveStart")) $("voiceLiveStart").disabled = busy;
  if ($("voiceLiveStop")) $("voiceLiveStop").disabled = !busy;
  if ($("voiceLiveSave")) $("voiceLiveSave").disabled = busy;
  voiceLinkage();
}

const VOICE_PROTOCOLS = {
  systemone: "systemone：typed state/questions，URL 可只填根地址，自动补 /v1/systemone。",
  laya: "laya：分类模型，URL 填根地址即可，模型留空默认 multilingual。",
  cloudflare: "cloudflare：需填完整 Workers AI URL（含账户 ID），模型通常是 clef。",
  http_json: "http_json：自定义 JSON 接口，返回三项概率或严格布尔值。",
  openai: "openai：OpenAI 兼容接口，必须指定分类模型。",
};

/* 决策模式 / 协议联动：topic 模式禁用 URL·Key·模型；http 模式给协议提示 */
function voiceLinkage() {
  const isHttp = ($("voiceLiveMode") || {}).value !== "topic";
  for (const id of ["voiceLiveType", "voiceLiveUrl", "voiceLiveKey", "voiceLiveModel"]) {
    if ($(id)) $(id).disabled = !isHttp || liveVoice.busy;
  }
  const type = $("voiceLiveType") ? $("voiceLiveType").value : "systemone";
  let hint = isHttp
    ? (VOICE_PROTOCOLS[type] || "")
    : "topic 模式由 EX 消息总线裁决，URL / Key / 模型都不生效。";
  const config = liveVoice.config || {};
  if (isHttp && !String(config.decision_url || "").trim()) {
    const profile = liveVoice.profile || {};
    hint += ` 当前 URL 为空，实际使用本地 gate 配置：${profile.type || "本地配置"}${profile.model ? " · " + profile.model : ""}。`;
  }
  if (isHttp && $("voiceLiveKey") && !$("voiceLiveKey").value.trim()) {
    hint += " Key 留空时回退到 token 环境变量或 gate 配置里的凭据。";
  }
  setText("voiceLiveHint", hint);
}

async function refreshLiveVoice() {
  if (liveVoice.fetching || state.activePage !== "voice" || !$("voiceLiveState")) return;
  liveVoice.fetching = true;
  try {
    const result = await voiceCall("status");
    const status = result.status || {};
    const profile = result.decision_profile || {};
    liveVoice.profile = profile;
    setText("voiceLiveState", !result.installed ? "语音插件未安装" : `EX: ${result.runtime_state || "--"} · 插件: ${result.enabled ? "已启用" : "未启用"} · 语音: ${status.state || "stopped"} · 决策: ${profile.type || "--"} ${profile.model || ""}${status.last_error ? ` · ${status.last_error}` : ""}`);
    const names = { stt: "语音识别", decision: "决策", chat: "对话", tts: "语音合成" };
    setText("voiceLiveModels", Object.entries(names).map(([kind, label]) => {
      const model = (result.models || {})[kind] || {};
      const check = model.last_check_ok === true ? "实测通过" : model.last_check_ok === false ? "最近检测失败" : model.configured ? "已配置，未检测" : "未配置";
      return `${label}：${model.model || "--"}（${check}）`;
    }).join(" · "));
    if ($("voiceLiveRuntime")) $("voiceLiveRuntime").hidden = result.runtime_state === "running";
    voiceBusy(Boolean(status.streaming_enabled || status.worker_running));
    if (!liveVoice.initialized) {
      const config = result.config || {};
      liveVoice.config = config;
      [["Threshold", "decision_threshold", 0.4], ["Mode", "decision_mode", "http"],
       ["Type", "decision_type", "systemone"], ["Url", "decision_url", ""],
       ["Model", "decision_model", ""], ["Key", "decision_api_key", ""]].forEach(([id, key, fallback]) => {
        const element = $(`voiceLive${id}`);
        if (element) element.value = config[key] ?? fallback;
      });
      // URL 为空时，协议与模型实际来自本地 gate 配置，这里显示真实生效值
      if (!config.decision_url && profile.type) $("voiceLiveType").value = profile.type;
      if (!config.decision_url && !config.decision_model && profile.model) $("voiceLiveModel").value = profile.model;
      const paths = $("voiceLivePaths");
      if (paths) {
        paths.replaceChildren();
        for (const key of voicePathFields) {
          const label = document.createElement("label");
          label.textContent = key;
          const input = document.createElement("input");
          input.className = "config-input";
          input.id = `voicePath_${key}`;
          input.value = config[key] || "";
          label.append(input);
          paths.append(label);
        }
      }
      voiceSelect("input", config.input_device ?? null);
      voiceSelect("output", config.output_device ?? null);
      liveVoice.initialized = true;
      voiceLinkage();
    }
    // 事件过滤：只把完整消息渲染成气泡，原始事件进调试日志
    voiceIngestEvents(result.events || []);
  } catch (error) {
    setText("voiceLiveState", error.message);
  } finally {
    liveVoice.fetching = false;
  }
}

function voiceTime(ts) {
  return new Date(ts ? ts * 1000 : Date.now()).toLocaleTimeString("zh-CN", { hour12: false });
}

/* ★ 过滤规则
   partial / status(非「你：」) / gate_result(无文本) / error / audio_devices 全部丢弃，
   只保留：用户整句（你：… 或 已确认对我说话：… / 背景记忆：…）与助手整句（reply partial=false）。 */
function voiceExtract(event) {
  const payload = event.payload || {};
  if (event.type === "reply") {
    if (payload.partial === true) return null;
    const text = String(payload.text || "").trim();
    return text ? { role: "ai", text, at: event.timestamp } : null;
  }
  if (event.type === "status") {
    const message = String(payload.message || "");
    if (message.startsWith("你：")) {
      const text = message.slice(2).trim();
      return text ? { role: "user", text, at: event.timestamp } : null;
    }
    return null;
  }
  if (event.type === "gate_result") {
    const message = String(payload.message || "");
    for (const [prefix, kind] of [["已确认对我说话：", "user"], ["背景记忆（不回答）：", "user-bg"]]) {
      if (message.startsWith(prefix)) {
        const text = message.slice(prefix.length).trim();
        return text ? { role: "user", kind, text, at: event.timestamp } : null;
      }
    }
    return null;
  }
  return null;
}

function voiceIngestEvents(events) {
  for (const event of events) {
    // 清空水位之前的事件一律跳过，避免"清空后又冒出来"
    if (event.timestamp && event.timestamp <= liveVoice.clearBefore) continue;
    const key = event.id != null ? `id:${event.timestamp}:${event.id}` : `h:${event.type}:${event.timestamp}`;
    if (liveVoice.seen.has(key)) continue;
    liveVoice.seen.add(key);
    voiceAppendRaw(event);
    const message = voiceExtract(event);
    if (message) voicePushMessage(message);
  }
  if (liveVoice.seen.size > 2000) liveVoice.seen = new Set();
}

function voicePushMessage(message) {
  if (message.role === "user") {
    const now = performance.now();
    if (message.text === liveVoice.lastUser && now - liveVoice.lastUserAt < 8000) return;
    liveVoice.lastUser = message.text;
    liveVoice.lastUserAt = now;
  }
  voiceAppendBubble(message);
}

async function voiceCopy(text, bubble) {
  try {
    await navigator.clipboard.writeText(text);
    bubble.classList.add("copied");
    setTimeout(() => bubble.classList.remove("copied"), 600);
  } catch (_) {
    /* 复制失败静默处理，仍可手动选择文本 */
  }
}

function voiceAppendBubble(message) {
  const empty = $("voiceChatEmpty");
  if (empty) empty.remove();
  liveVoice.messages.push(message);
  const chat = $("voiceChat");
  if (!chat) return;
  const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
  const wrap = document.createElement("div");
  wrap.className = "voice-msg " + (message.role === "ai" ? "ai" : "user");
  const bubble = document.createElement("div");
  bubble.className = "voice-bubble";
  bubble.textContent = message.text;
  bubble.title = "点击复制";
  bubble.addEventListener("click", () => voiceCopy(message.text, bubble));
  wrap.append(bubble);
  const meta = document.createElement("div");
  meta.className = "voice-meta";
  meta.textContent = (message.role === "ai" ? "🤖 助手" : "🗣 用户")
    + (message.kind === "user-bg" ? " · 背景" : "") + " · " + voiceTime(message.at);
  wrap.append(meta);
  chat.append(wrap);
  if (nearBottom) chat.scrollTop = chat.scrollHeight;
}

function voiceAppendRaw(event) {
  const pre = $("voiceRaw");
  if (!pre) return;
  let payload = event.payload;
  if (typeof payload !== "string") {
    try { payload = JSON.stringify(payload); } catch (_) { payload = String(payload); }
  }
  if (payload && payload.length > 600) payload = payload.slice(0, 600) + "…";
  pre.textContent += `[${voiceTime(event.timestamp)}] ${String(event.type).padEnd(15, " ")} ${payload}\n`;
  const lines = pre.textContent.split("\n");
  if (lines.length > 401) pre.textContent = lines.slice(-400).join("\n");
  if (!pre.hidden) pre.scrollTop = pre.scrollHeight;
}

function voiceSetMode(mode) {
  liveVoice.mode = mode;
  const debug = mode === "debug";
  if ($("voiceChat")) $("voiceChat").hidden = debug;
  if ($("voiceRaw")) $("voiceRaw").hidden = !debug;
  $("voiceChatTab").classList.toggle("active", !debug);
  $("voiceDebugTab").classList.toggle("active", debug);
  if (debug && $("voiceRaw")) $("voiceRaw").scrollTop = $("voiceRaw").scrollHeight;
  else if ($("voiceChat")) $("voiceChat").scrollTop = $("voiceChat").scrollHeight;
}

function voiceClearChat() {
  // 注意：这里不能清空 liveVoice.seen。
  // /voice/status 每次轮询都会带回最近 80 条事件，一旦清掉去重记录，
  // 下一次轮询会把它们当成新事件重新渲染 —— 表现就是"点了清空没反应"。
  // 只有保留 seen，旧记录才会被永久压住；新事件 id 不同，照常显示。
  liveVoice.messages = [];
  liveVoice.lastUser = "";
  liveVoice.lastUserAt = 0;
  liveVoice.clearBefore = Date.now() / 1000;
  localStorage.setItem("exVoiceClearBefore", String(liveVoice.clearBefore));
  if ($("voiceRaw")) $("voiceRaw").textContent = "";
  const chat = $("voiceChat");
  if (chat) {
    chat.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "voice-chat-empty";
    empty.id = "voiceChatEmpty";
    empty.textContent = "已清空，等待新的语音…";
    chat.append(empty);
  }
}

function voiceExportChat() {
  if (liveVoice.messages.length === 0) {
    voiceSetResult("voiceLiveResult", "还没有对话可导出", false);
    return;
  }
  const lines = ["AstrBotEX 语音对话记录", "导出时间：" + voiceTime(null), "-".repeat(40)];
  for (const message of liveVoice.messages) {
    const who = message.role === "ai" ? "助手" : (message.kind === "user-bg" ? "用户(背景)" : "用户");
    lines.push(`[${voiceTime(message.at)}] ${who}：${message.text}`);
  }
  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `voice-transcript-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  voiceSetResult("voiceLiveResult", `已导出 ${liveVoice.messages.length} 条`, true);
}

async function voiceDevices() {
  const selected = {
    input: JSON.parse($("voiceLiveInput").value),
    output: JSON.parse($("voiceLiveOutput").value),
  };
  const result = await voiceCall("devices");
  liveVoice.devices = result.devices || [];
  voiceSelect("input", selected.input);
  voiceSelect("output", selected.output);
  voiceSetResult("voiceDeviceResult", `已刷新 ${liveVoice.devices.length} 个端点`, true);
}

async function voiceSaveConfig() {
  const threshold = Number($("voiceLiveThreshold").value);
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) throw new Error("阈值须为0～1");
  const config = {
    input_device: JSON.parse($("voiceLiveInput").value),
    output_device: JSON.parse($("voiceLiveOutput").value),
    decision_threshold: threshold,
    forward_threshold: threshold,
    decision_mode: $("voiceLiveMode").value,
    decision_type: $("voiceLiveType").value,
    decision_url: $("voiceLiveUrl").value.trim(),
    decision_model: $("voiceLiveModel").value.trim(),
    decision_api_key: $("voiceLiveKey").value.trim(),
  };
  for (const key of voicePathFields) {
    const element = $(`voicePath_${key}`);
    if (element) config[key] = element.value.trim();
  }
  await voiceCall("config", { config });
  voiceSetResult("voiceLiveResult", "设置已保存", true);
  liveVoice.initialized = false;
  await refreshLiveVoice();
}

function setupLiveVoice() {
  const bind = (id, action) => {
    const element = $(id);
    if (!element) return;
    element.addEventListener("click", async () => {
      try { await action(); } catch (error) { voiceSetResult("voiceLiveResult", error.message, false); }
    });
  };
  bind("voiceLiveRefresh", voiceDevices);
  bind("voiceLiveCheck", async () => {
    const result = await voiceCall("devices/check", {
      input_device: JSON.parse($("voiceLiveInput").value),
      output_device: JSON.parse($("voiceLiveOutput").value),
    });
    voiceSetResult("voiceDeviceResult", result.ok ? "格式检查通过" : "格式检查未通过", result.ok === true);
  });
  bind("voiceLiveSave", voiceSaveConfig);
  bind("voiceLiveStart", async () => {
    await voiceCall("streaming", { enabled: true });
    voiceSetResult("voiceLiveActionResult", "已开启连续对话", true);
    await refreshLiveVoice();
  });
  bind("voiceLiveStop", async () => {
    await voiceCall("streaming", { enabled: false });
    voiceSetResult("voiceLiveActionResult", "已停止连续对话", true);
    await refreshLiveVoice();
  });
  bind("voiceLiveRuntime", async () => {
    await apiJson("/api/v1/ex/runtime/start", { method: "POST", body: "{}" });
    voiceSetResult("voiceLiveActionResult", "运行时已启动", true);
    await refreshLiveVoice();
  });
  bind("voiceChatClear", voiceClearChat);
  bind("voiceChatExport", voiceExportChat);
  bind("voiceChatTab", () => voiceSetMode("chat"));
  bind("voiceDebugTab", () => voiceSetMode("debug"));

  $("voiceLiveMode").addEventListener("change", voiceLinkage);
  $("voiceLiveType").addEventListener("change", () => {
    const defaults = { laya: "multilingual", cloudflare: "clef" };
    const model = $("voiceLiveModel");
    if (model && !model.value.trim() && defaults[$("voiceLiveType").value]) {
      model.value = defaults[$("voiceLiveType").value];
    }
    voiceLinkage();
  });
  $("voiceLiveKeyEye").addEventListener("click", () => {
    const input = $("voiceLiveKey");
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    $("voiceLiveKeyEye").textContent = hidden ? "隐藏" : "显示";
  });
  $("voiceLiveUrl").addEventListener("input", voiceLinkage);

  voiceSetMode("chat");
  voiceLinkage();
  refreshLiveVoice();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupLiveVoice);
else setupLiveVoice();

boot();
