const API_BASE = window.ASTRBOTEX_API_BASE || "http://127.0.0.1:8765";
const MAX_TRACE_EVENTS = 200;
const PLUGIN_CATEGORIES = ["vision", "perception", "control", "decision", "special"];

const CATEGORY_LABELS = {
  vision: "视觉",
  perception: "感知",
  control: "控制",
  decision: "决策",
  special: "特殊",
};

const state = {
  events: [],
  eventSource: null,
  plugins: [],
  publishers: [],
  runtimeState: "idle",
  activePage: "core",
  activePluginId: null,
  activePluginCategory: null,
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
  } catch (error) {
    showToast(error.message, "error");
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
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

function switchPage(page) {
  state.activePage = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${page}`));
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });
  if (PLUGIN_CATEGORIES.includes(page)) refreshPlugins();
  if (page === "logs") renderEvents();
  if (page === "plugin") renderPluginDashboard();
}

async function refreshStatus(options = {}) {
  const { suppressToast = false } = options;
  try {
    const status = await apiJson("/api/status");
    renderStatus(status);
    setConnection(true, "API 已连接");
    return status;
  } catch (error) {
    setConnection(false, `API 连接失败: ${error.message}`);
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
  state.runtimeState = String(status.runtime_state || "idle").toLowerCase();

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
  renderRuntimeToggleButton();

  if (Array.isArray(status.recent_events) && state.events.length === 0) {
    status.recent_events.forEach(pushEvent);
  }
}

function renderRuntimeToggleButton(pending = "") {
  const button = $("runtimeToggleButton");
  if (!button) return;
  button.classList.remove("primary", "danger", "secondary");
  button.disabled = false;

  if (pending === "starting") {
    button.classList.add("primary", "busy");
    button.textContent = "启动中";
    button.disabled = true;
    return;
  }
  if (pending === "stopping") {
    button.classList.add("danger", "busy");
    button.textContent = "停止中";
    button.disabled = true;
    return;
  }

  button.classList.remove("busy");
  if (state.runtimeState === "running") {
    button.classList.add("primary");
    button.textContent = "运行中";
    return;
  }
  button.classList.add("danger");
  button.textContent = "已停止";
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
  if (!pill) return;
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
  setText("logAutoscrollButton", `自动滚动: ${state.logAutoscroll ? "开" : "关"}`);
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
  renderAllPluginCategories();
  if (state.activePluginId) {
    const matched = currentPlugin();
    if (!matched) {
      state.activePluginId = null;
      state.activePluginCategory = null;
      if (state.activePage === "plugin") switchPage("vision");
    } else {
      if (!matched.config_schema) {
        await fetchPluginDetail(matched.id);
      }
      renderPluginDashboard();
    }
  }
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
    return;
  }

  plugins.forEach((plugin) => {
    const card = document.createElement("article");
    card.className = "plugin-card";
    card.addEventListener("click", () => {
      openPluginDashboard(plugin).catch((error) => showToast(error.message, "error"));
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
      <p>${escapeHtml(plugin.description || "暂无说明")}</p>
      <div class="plugin-badges">${(plugin.provides || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
      <small>${escapeHtml(plugin.status)} · ${escapeHtml(plugin.version || "--")}</small>
    `;

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
  setText("pluginDashboardTitle", plugin.name || plugin.id);
  setText("pluginDashboardSubtitle", `${CATEGORY_LABELS[plugin.category] || "特殊"}插件`);
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
    image.src = `${API_BASE}${plugin.cover_url}`;
    image.alt = plugin.name;
    cover.appendChild(image);
  } else {
    cover.innerHTML = `<span>${escapeHtml((plugin.name || plugin.id).slice(0, 2).toUpperCase())}</span>`;
  }

  renderPluginConfigForm(plugin);
  renderPluginPubSub(plugin);
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

  const entries = Object.entries(properties);
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
      if (isPresent(field.minimum)) input.min = String(field.minimum);
      if (isPresent(field.maximum)) input.max = String(field.maximum);
      if (isPresent(value)) input.value = String(value);
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
    row.appendChild(input);

    if (field.description) {
      const note = document.createElement("small");
      note.className = "config-note";
      note.textContent = field.description;
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
    remove.className = "secondary";
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
  document.querySelectorAll("[data-config-key]").forEach((input) => {
    const key = input.dataset.configKey;
    const type = input.dataset.configType;
    const required = input.dataset.required === "true";
    let value = input.value;

    if (required && !String(value).trim()) {
      missing.push(key);
      return;
    }
    if (!String(value).trim() && !required) {
      payload[key] = "";
      return;
    }
    if (type === "boolean") value = value === "true";
    if (type === "integer") value = Number.parseInt(value, 10);
    if (type === "number") value = Number(value);
    payload[key] = value;
  });
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

async function setPluginEnabled(pluginId, enabled) {
  const action = enabled ? "enable" : "disable";
  const result = await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(pluginId)}/${action}`, {
    method: "POST",
    body: "{}",
  });
  const updated = result.plugin;
  state.plugins = state.plugins.map((plugin) => (plugin.id === updated.id ? updated : plugin));
  if (state.activePluginId === updated.id) {
    state.activePluginId = updated.id;
  }
  renderAllPluginCategories();
  renderPluginDashboard();
  await refreshStatus();
  showToast(`${updated.name}已${enabled ? "启用" : "停用"}`);
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
  renderSubscriptionList(plugin);
}

function removeSubscription(index) {
  const plugin = currentPlugin();
  if (!plugin) return;
  const pubsub = currentPubsubDraft(plugin);
  pubsub.subscriptions.splice(index, 1);
  plugin.pubsub = pubsub;
  renderSubscriptionList(plugin);
}

async function uninstallActivePlugin() {
  const plugin = currentPlugin();
  if (!plugin) return;
  if (!window.confirm(`确认卸载插件“${plugin.name}”吗？`)) return;
  await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(plugin.id)}`, { method: "DELETE" });
  showToast(`已卸载${plugin.name}`);
  const category = plugin.category || "special";
  state.activePluginId = null;
  state.activePluginCategory = null;
  await refreshPlugins();
  switchPage(category);
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
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  await refreshPlugins();
  const plugin = state.plugins.find((item) => item.id === data.plugin.id) || data.plugin;
  await openPluginDashboard(plugin);
  showToast(`已安装${plugin.name}`);
}

function bindActions() {
  setText("apiBaseLabel", API_BASE.replace(/^https?:\/\//, ""));
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  $("refreshButton").addEventListener("click", (event) => runAction(event.currentTarget, "刷新中", () => refreshStatus()));
  $("runtimeToggleButton").addEventListener("click", () => {
    handleRuntimeToggleClick().catch(() => showToast("状态机切换失败", "error"));
  });

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

  $("pluginBackButton").addEventListener("click", () => switchPage(state.activePluginCategory || "vision"));
  $("pluginEnabledSwitch").addEventListener("change", (event) => {
    const plugin = currentPlugin();
    if (!plugin) return;
    setPluginEnabled(plugin.id, event.currentTarget.checked);
  });
  $("pluginSaveButton").addEventListener("click", (event) => runAction(event.currentTarget, "保存中", savePluginConfig));
  $("pluginPubsubSaveButton").addEventListener("click", (event) => runAction(event.currentTarget, "保存中", savePluginPubsub));
  $("pluginUninstallButton").addEventListener("click", (event) => runAction(event.currentTarget, "卸载中", uninstallActivePlugin));
  $("subscriptionPublisherSelect").addEventListener("change", (event) => {
    const plugin = currentPlugin();
    if (!plugin) return;
    renderPublisherTopicOptions(event.currentTarget.value, plugin);
  });
  $("subscriptionAddButton").addEventListener("click", addSubscription);

  $("logAutoscrollButton").addEventListener("click", toggleLogAutoscroll);
  $("logClearButton").addEventListener("click", clearLogs);
}

bindActions();
renderEvents();
renderRuntimeToggleButton();
refreshStatus();
refreshPlugins();
connectEvents();
setInterval(() => {
  refreshStatus({ suppressToast: true }).catch(() => {});
}, 2000);
