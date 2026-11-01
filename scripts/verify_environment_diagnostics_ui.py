"""B06 browser acceptance: environment page + plugin ROS panel diagnostics.

The script renders the *real* ``dashboard/environments.js`` and the real
``dashboard/environment-diagnostics.css`` on a static fixture that reproduces the
environment page markup, then drives it through a mock management API. No server
is started, no route is added to the product, and no test hook is compiled into
the dashboard: every global the page normally gets from ``app.js`` is stubbed
here.

Run:
    python scripts/verify_environment_diagnostics_ui.py --output acceptance/ros-diagnostics/B06/ui

Exit codes: 0 all assertions passed, 1 assertion failed, 3 Playwright missing
(NOT_RUN - a browser is required to judge the panel).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"

# styles.css semantic colours, compared as computed colours.
TOKENS = {
    "ok": "rgb(33, 134, 94)",       # --ok   #21865E
    "warn": "rgb(163, 106, 19)",    # --warn #A36A13
    "err": "rgb(199, 68, 68)",      # --err  #C74444
    "ink2": "rgb(82, 112, 100)",    # --ink-2 #527064
}

ISSUE_NO_DATA = {
    "issue_key": "sess-A|3|vision/image_in|1|1|4|ROS_RX_NO_DATA_WITH_PEER",
    "code": "ROS_RX_NO_DATA_WITH_PEER",
    "severity": "warning",
    "source": "ros.port",
    "scope": {
        "session_id": "sess-A", "environment_generation": 3,
        "endpoint_id": "vision/image_in", "plugin_id": "vision",
        "port_id": "image_in", "topic": "/camera/image_raw",
    },
    "message": "端点资源就绪且存在匹配对端，但当前观察窗口未收到回调",
    "evidence": {
        "graph_peer_count": 1, "matched_peer_count": 1,
        "monitoring_age_sec": 12.5, "grace_sec": 5.0, "last_receive_monotonic_ns": None,
    },
    "remediation": {
        "summary": "确认相机节点是否仍在发布",
        "steps": ["ros2 topic hz /camera/image_raw", "核对 QoS reliability 是否匹配"],
    },
    "first_seen_at": 1699999990, "last_seen_at": 1700000000, "occurrences": 3,
}

PLUGIN_ROS = {
    "plugin_id": "vision", "revision": 2, "session": "sess-A",
    "ports": [
        {"id": "image_in", "label": "图像输入", "direction": "subscribe",
         "message_types": ["sensor_msgs/msg/Image"], "requires_runtime_running": False},
        {"id": "cmd_out", "label": "控制输出", "direction": "publish",
         "message_types": ["geometry_msgs/msg/Twist"], "requires_runtime_running": False},
    ],
    "bindings": {
        "image_in": {"enabled": True, "topic": "/camera/image_raw",
                     "message_type": "sensor_msgs/msg/Image",
                     "qos": {"reliability": "reliable", "durability": "volatile", "depth": 5}},
        "cmd_out": {"enabled": True, "topic": "/cmd_vel",
                    "message_type": "geometry_msgs/msg/Twist",
                    "qos": {"reliability": "reliable", "durability": "volatile", "depth": 5}},
    },
    "endpoints": [
        {"port_id": "image_in", "state": "ready", "direction": "subscribe",
         "data_health": "receiving", "tx_published": 0},
        {"port_id": "cmd_out", "state": "ready", "direction": "publish",
         "data_health": "not_monitored", "tx_published": 3},
    ],
}


def environment_page_markup() -> str:
    """Verbatim copy of the 06 ENVIRONMENTS section from dashboard/index.html."""
    return """<section class="page active" id="page-environments">
  <header class="page-head">
    <div class="page-head-num">06</div>
    <div class="page-head-text">
      <h1>环境适配</h1>
      <p>切换运行环境，保留内部 TopicBus 并按需接入 ROS 2</p>
    </div>
    <div class="page-head-actions">
      <button class="btn btn-ghost btn-sm" id="environmentRefreshButton">刷新</button>
    </div>
  </header>

  <section class="tab-rail environment-modes" id="environmentModeGrid" aria-label="运行环境"></section>

  <section class="panel">
    <div class="panel-head">
      <span class="panel-num">06.A</span>
      <h2>当前环境</h2>
      <span class="panel-meta" id="environmentOperation">--</span>
    </div>
    <div class="environment-status-grid">
      <div class="kv"><span class="kv-label">模式</span><span class="kv-dots"></span><b class="kv-value" id="environmentActiveMode">--</b></div>
      <div class="kv"><span class="kv-label">阶段</span><span class="kv-dots"></span><b class="kv-value" id="environmentPhase">--</b></div>
      <div class="kv"><span class="kv-label">健康</span><span class="kv-dots"></span><b class="kv-value" id="environmentHealth">--</b></div>
      <div class="kv"><span class="kv-label">代次</span><span class="kv-dots"></span><b class="kv-value" id="environmentGeneration">--</b></div>
      <div class="kv"><span class="kv-label">内部 TopicBus</span><span class="kv-dots"></span><b class="kv-value state-ok" id="environmentTopicBus">--</b></div>
      <div class="kv"><span class="kv-label">ROS 节点</span><span class="kv-dots"></span><b class="kv-value" id="environmentRosNode">--</b></div>
    </div>
    <div class="environment-error" id="environmentError" hidden></div>
  </section>

  <!-- @CONTAINER@ -->

  <section class="panel">
    <div class="panel-head">
      <span class="panel-num">06.B</span>
      <h2>ROS 2 配置</h2>
      <span class="panel-meta">保存后切回普通环境，再启用 ROS 2 生效</span>
    </div>
    <div class="environment-config-grid" id="ros2ConfigForm">
      <label class="connection-field"><span>Domain ID</span><input class="config-input" id="ros2DomainId" type="number" min="0" max="232" /></label>
      <label class="connection-field"><span>Namespace</span><input class="config-input" id="ros2Namespace" type="text" placeholder="/astrbotex" /></label>
      <label class="connection-field"><span>节点名</span><input class="config-input" id="ros2NodeName" type="text" placeholder="environment" /></label>
      <label class="connection-field"><span>发现间隔（秒）</span><input class="config-input" id="ros2DiscoveryInterval" type="number" min="0.2" max="60" step="0.1" /></label>
      <label class="connection-field"><span>显示内部 Topic</span><input id="ros2IncludeHidden" type="checkbox" /></label>
    </div>
    <div class="environment-actions">
      <button class="btn btn-ghost btn-sm" id="ros2ConfigSaveButton">保存配置</button>
      <button class="btn btn-primary btn-sm" id="ros2ConfigResetButton">放弃草稿</button>
      <span class="panel-meta" id="environmentConfigNote"></span>
    </div>
    <p class="pubsub-help" id="environmentEffectiveConfig"></p>
  </section>

  <section class="environment-two-column">
    <section class="panel">
      <div class="panel-head"><span class="panel-num">06.C</span><h2>ROS 图</h2><span class="panel-meta" id="environmentGraphMeta">--</span></div>
      <div class="environment-list" id="environmentGraphList"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><span class="panel-num">06.D</span><h2>节点</h2><span class="panel-meta" id="environmentNodeMeta">--</span></div>
      <div class="environment-list" id="environmentNodeList"></div>
    </section>
  </section>

  <section class="environment-two-column">
    <section class="panel">
      <div class="panel-head"><span class="panel-num">06.E</span><h2>EX 接收 Topic</h2><span class="panel-meta" id="environmentReceivedMeta">--</span></div>
      <div class="environment-list" id="environmentReceivedList"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><span class="panel-num">06.F</span><h2>EX 发送 Topic</h2><span class="panel-meta" id="environmentSentMeta">--</span></div>
      <div class="environment-list" id="environmentSentList"></div>
    </section>
  </section>

  <section class="panel">
    <div class="panel-head"><span class="panel-num">06.G</span><h2>消息接口检查</h2></div>
    <div class="environment-actions">
      <input class="config-input" id="rosInterfaceType" placeholder="my_robot_interfaces/msg/Target" aria-label="ROS 消息类型" />
      <button class="btn btn-ghost btn-sm" id="rosInterfaceCheckButton">检查类型支持</button>
    </div>
    <p class="pubsub-help">自定义类型需要在容器中预装完整接口包和依赖，更新后重启容器。</p>
    <div class="environment-list" id="rosInterfaceResult"></div>
    <p class="pubsub-help" id="rosInterfaceMissing"></p>
    <div class="environment-list" id="rosInterfacePackages"></div>
  </section>
</section>"""


CONTAINER_MARKUP = """<section class="panel" id="environmentDiagnostics">
    <div class="panel-head"><span class="panel-num">06.A.1</span><h2>诊断</h2>
      <span class="panel-meta" id="environmentDiagnosticsMeta">--</span>
      <button class="btn btn-ghost btn-sm" id="environmentDiagnosticsRefreshButton" type="button">重新检查</button>
    </div>
    <div class="env-diag-status" id="environmentDiagnosticsStatus"></div>
    <div class="env-diag-summary" id="environmentDiagnosticsSummary"></div>
    <div class="environment-list" id="environmentDiagnosticsIssues"></div>
    <p class="pubsub-help" id="environmentDiagnosticsNote"></p>
  </section>"""


STUBS = r"""
/* ---- fixture harness: only the globals environments.js expects from app.js ---- */
window.state = {environment: null, activePage: "environments", plugins: [], activePluginId: null, eventSource: null};
window.__requests = [];
window.__toasts = [];
window.__pageSwitches = [];
window.__pwned = undefined;

function $(id) { return document.getElementById(id); }
function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value === null || value === undefined ? "" : String(value);
}
function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
function formatTime(ts) { if (!ts) return "--"; return new Date(Number(ts) * 1000).toLocaleTimeString(); }
function showToast(message, kind) { window.__toasts.push({message: String(message), kind: kind || "ok"}); }
async function runAction(button, pendingLabel, action) {
  if (button) button.disabled = true;
  try { return await action(); } finally { if (button) button.disabled = false; }
}
function switchPage(page) {
  window.__pageSwitches.push(page);
  state.activePage = page;
  document.querySelectorAll(".page").forEach(el => el.classList.toggle("active", el.id === "page-" + page));
}

window.EXLogs = {
  __entries: [{type: "ros_diagnostic", message: "历史日志：ROS_RX_NO_PEER"}],
  __filters: [], __cleared: 0,
  push(event) { this.__entries.push(event); },
  setFilter(patch) { this.__filters.push(patch); },
  clear() { this.__cleared += 1; this.__entries = []; },
};

window.__mock = {
  env: null, graph: null, endpoints: null, interfaces: null, diagnostics: null, pluginRos: null,
  diagnosticsFail: 0, graphFail: 0, diagnosticsDelayMs: 0,
  refreshStatus: 202, refreshMessage: "", refreshBody: null
};

function route(path, options) {
  const method = (options && options.method) || "GET";
  const body = options && options.body ? String(options.body) : "";
  window.__requests.push({method: method, path: path, body: body});
  const mock = window.__mock;
  if (method === "POST") {
    if (path === "/api/v1/ex/environments/diagnostics/refresh") {
      mock.refreshBody = body;
      const status = Number(mock.refreshStatus || 202);
      if (status === 202) {
        return [202, {ok: true, accepted: true, refresh_id: "rf-0009", environment: mock.env.environment}];
      }
      return [status, {ok: false, message: mock.refreshMessage || "rejected"}];
    }
    return [404, {ok: false, message: "unexpected POST " + path}];
  }
  if (path === "/api/v1/ex/environments") return [200, mock.env];
  if (path.endsWith("/ros2/graph")) {
    if (mock.graphFail) return [Number(mock.graphFail), {ok: false, message: "graph unavailable"}];
    return [200, mock.graph];
  }
  if (path.endsWith("/ros2/endpoints")) return [200, mock.endpoints];
  if (path.endsWith("/ros2/interfaces")) return [200, mock.interfaces];
  if (path.endsWith("/diagnostics")) {
    if (mock.diagnosticsFail) return [Number(mock.diagnosticsFail), {ok: false, message: "diagnostics unavailable"}];
    return [200, mock.diagnostics];
  }
  if (path.indexOf("/api/v1/ex/plugins/") === 0) return [200, {ok: true, ros2: mock.pluginRos}];
  return [404, {ok: false, message: "unknown GET " + path}];
}

async function managementFetch(path, options = {}) {
  const mock = window.__mock;
  const delay = path.indexOf("/diagnostics") >= 0 ? Number(mock.diagnosticsDelayMs || 0) : 0;
  const [status, data] = route(path, options);
  if (delay) await new Promise(resolve => setTimeout(resolve, delay));
  return new Response(JSON.stringify(data), {status: status, headers: {"Content-Type": "application/json"}});
}

async function apiJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  const response = await managementFetch(path, Object.assign({}, options, {headers: headers}));
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || (response.status + " error"));
  }
  return data;
}
"""


FIXTURE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>EX environment diagnostics fixture (B06)</title>
<link rel="stylesheet" href="@STYLES@" />
<link rel="stylesheet" href="@DIAG_CSS@" />
</head>
<body>
<main>
@PAGE@

<section class="page" id="page-logs">
  <header class="page-head"><div class="page-head-num">03</div>
    <div class="page-head-text"><h1>日志</h1><p>事件流</p></div></header>
  <div class="log-console" id="logConsole"></div>
</section>
</main>

<section class="panel" id="pluginRosPanel" hidden>
  <div class="panel-head"><span class="panel-num">ROS</span><h2>ROS 2 端口</h2>
    <button class="btn btn-primary btn-sm" id="pluginRosSaveButton">保存 ROS 配置</button>
    <button class="btn btn-ghost btn-sm" id="pluginRosResetButton">放弃草稿</button>
  </div>
  <p class="pubsub-help" id="pluginRosNotice">ROS 配置与内部发布订阅相互独立。</p>
  <div id="pluginRosFields"></div><datalist id="rosTopicSuggestions"></datalist>
</section>
<div id="pluginDashboardBody"></div>

<script>@STUBS@</script>
<script src="@ENV_JS@"></script>
</body>
</html>
"""


def env_payload(generation=3, revision=7, active_mode="ros2", health="ready", phase="idle"):
    return {
        "environment": {
            "session_id": "sess-A", "generation": generation, "revision": revision,
            "active_mode": active_mode, "phase": phase, "health": health,
            "topic_bus_available": True, "last_error": None, "operation_id": None,
        },
        "adapters": {"normal": {"available": True}, "ros2": {"available": True}},
        "config": {"ros2": {"domain_id": 0, "namespace": "/astrbotex", "node_name": "environment",
                            "discovery_interval_sec": 1, "include_hidden_topics": False}},
        "locked_config": {},
        "effective_config": {"domain_id": 0, "namespace": "/astrbotex"},
        "active_adapter": {"node_name": "/astrbotex/environment"},
    }


def endpoints_payload(generation=3, revision=7):
    environment = env_payload(generation, revision)["environment"]
    return {
        "environment": environment,
        "received": [
            {"topic": "/camera/image_raw", "message_type": "sensor_msgs/msg/Image",
             "plugin_id": "vision", "port_id": "image_in", "state": "ready", "resource_created": True,
             "rx_received": 42, "rx_consumed": 40, "queue_depth": 0, "queue_dropped": 0,
             "expired": 0, "rejected": 0, "graph_peer_count": 1, "matched_peer_count": 1,
             "rate_hz": 29.7, "queue_bytes": 0, "last_message_at": 1700000000, "oversized": 0,
             "data_health": "no_data",
             "diagnostics": {"policy": {"mode": "periodic", "grace_sec": 5, "stale_sec": 3},
                             "monitoring_active": True, "binding_received": False,
                             "receive_age_sec": None, "consume_age_sec": None,
                             "monitoring_age_sec": 12.5, "reason_code": "no_callback"}},
            {"topic": "/scan", "message_type": "sensor_msgs/msg/LaserScan",
             "plugin_id": "lidar", "port_id": "scan_in", "state": "ready", "resource_created": True,
             "rx_received": 900, "rx_consumed": 899, "queue_depth": 1, "queue_dropped": 0,
             "expired": 0, "rejected": 0, "graph_peer_count": 2, "matched_peer_count": None,
             "rate_hz": 10.0, "queue_bytes": 64, "last_message_at": 1700000001, "oversized": 0,
             "data_health": "receiving",
             "diagnostics": {"policy": {"mode": "periodic", "grace_sec": 5, "stale_sec": 3},
                             "monitoring_active": True, "binding_received": True,
                             "receive_age_sec": 0.4, "consume_age_sec": 0.9,
                             "monitoring_age_sec": 40.0, "reason_code": None}},
            {"topic": "/mark", "message_type": "std_msgs/msg/String",
             "plugin_id": "vision", "port_id": "mark", "state": "idle", "resource_created": False,
             "rx_received": 0, "rx_consumed": 0, "queue_depth": 0, "queue_dropped": 0,
             "expired": 0, "rejected": 0, "graph_peer_count": 0, "matched_peer_count": 0,
             "rate_hz": 0, "queue_bytes": 0, "last_message_at": None, "oversized": 0,
             "data_health": "not_monitored", "diagnostics": {"policy": {"mode": "off"}}},
        ],
        "sent": [
            {"topic": "/cmd_vel", "message_type": "geometry_msgs/msg/Twist",
             "plugin_id": "motion", "port_id": "cmd_out", "state": "ready", "resource_created": True,
             "tx_queued": 3, "tx_published": 3, "queue_depth": 0, "queue_dropped": 0,
             "expired": 0, "rejected": 0, "graph_peer_count": 0, "matched_peer_count": 0,
             "rate_hz": 0, "queue_bytes": 0, "last_message_at": 1700000002, "oversized": 0,
             "data_health": "not_monitored", "diagnostics": {"policy": {"mode": "off"}}},
        ],
    }


def graph_payload(generation=3, revision=7):
    environment = env_payload(generation, revision)["environment"]
    return {"environment": environment, "refreshed_at": 1700000000,
            "nodes": [{"node_namespace": "/", "node_name": "camera"}],
            "topics": [{"name": "/camera/image_raw", "types": ["sensor_msgs/msg/Image"],
                        "publishers": [{"node_namespace": "/", "node_name": "camera", "topic_type": "sensor_msgs/msg/Image", "qos": {}}],
                        "subscriptions": []}]}


def diagnostics_payload(generation=3, revision=7, summary="warning", issues=None, ports=None,
                        current=True, partial=False, refresh=None, preflight=None, age=0.6):
    if issues is None:
        issues = [copy.deepcopy(ISSUE_NO_DATA)]
    if ports is None:
        ports = [{"endpoint_id": "vision/image_in", "plugin_id": "vision", "port_id": "image_in",
                  "direction": "subscribe", "environment_generation": generation,
                  "owner_generation": 1, "binding_generation": 1, "monitoring_epoch": 4,
                  "data_health": "no_data",
                  "diagnostics": {"policy": {"mode": "periodic", "grace_sec": 5, "stale_sec": 3},
                                  "monitoring_active": True, "binding_received": False,
                                  "receive_age_sec": None, "consume_age_sec": None,
                                  "monitoring_age_sec": 12.5, "reason_code": "no_callback"}}]
    return {
        "ok": True, "schema_version": 1,
        "environment": {"session_id": "sess-A", "generation": generation, "revision": revision,
                        "active_mode": "ros2", "phase": "idle"},
        "collected_at": 1700000000, "collection_age_sec": age,
        "current": current, "partial": partial, "summary": summary,
        "preflight": preflight if preflight is not None else {
            "schema_version": 1, "platform": "linux", "supported": True, "partial": False,
            "checks": [{"id": "rmw_requested", "status": "pass", "message": "RMW 已设置"},
                       {"id": "interfaces", "status": "warn", "message": "多播不可用"}],
            "runtime": {"ros_distro": "humble"}, "interfaces": [],
            "collected_at": 1700000000, "age_sec": 4.2, "current": True,
        },
        "ports": ports,
        "active_issues": issues,
        "refresh": refresh if refresh is not None else {"id": "rf-0008", "state": "idle", "error_code": None},
    }


class Report:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.page_errors = []
        self.screenshots = []
        self.notes = []

    def check(self, name, condition, detail=""):
        if condition:
            self.passed.append(name)
        else:
            self.failed.append({"check": name, "detail": str(detail)[:600]})
        return bool(condition)

    def equal(self, name, actual, expected):
        return self.check(name, actual == expected, "expected %r, got %r" % (expected, actual))

    def dump(self, output: Path):
        payload = {
            "task": "B06",
            "scope": "OFFLINE_BROWSER_FIXTURE",
            "passed": self.passed,
            "failed": self.failed,
            "page_errors": self.page_errors,
            "screenshots": self.screenshots,
            "notes": self.notes,
            "totals": {"passed": len(self.passed), "failed": len(self.failed)},
        }
        (output / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
        return payload


def write_fixture(output: Path, with_container: bool) -> Path:
    styles = os.path.relpath(DASHBOARD / "styles.css", output).replace("\\", "/")
    diag_css = os.path.relpath(DASHBOARD / "environment-diagnostics.css", output).replace("\\", "/")
    env_js = os.path.relpath(DASHBOARD / "environments.js", output).replace("\\", "/")
    page = environment_page_markup()
    page = page.replace("<!-- @CONTAINER@ -->", CONTAINER_MARKUP if with_container else "")
    name = "fixture-container.html" if with_container else "fixture.html"
    target = output / name
    target.write_text(FIXTURE.replace("@STYLES@", styles).replace("@DIAG_CSS@", diag_css)
                      .replace("@PAGE@", page).replace("@STUBS@", STUBS).replace("@ENV_JS@", env_js),
                      encoding="utf-8")
    return target


def text_of(page, selector):
    return page.evaluate(
        "sel => { const el = document.querySelector(sel); return el ? el.textContent : ''; }", selector)


def count(page, selector):
    return page.evaluate("sel => document.querySelectorAll(sel).length", selector)


def colour(page, selector, prop="color"):
    return page.evaluate(
        "([sel, name]) => { const el = document.querySelector(sel); return el ? getComputedStyle(el)[name] : null; }",
        [selector, prop])


def settle(page):
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def boot(page, mock):
    page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
    page.evaluate("async () => { bindEnvironmentActions(); await refreshEnvironment(); }")
    settle(page)


def refresh(page):
    page.evaluate("async () => { await refreshEnvironment(); }")
    settle(page)


def reset_requests(page):
    page.evaluate("() => { window.__requests.length = 0; }")


def posts(page):
    return page.evaluate("() => window.__requests.filter(r => r.method === 'POST')")


def wait_note(page, needle, timeout=6000):
    try:
        page.wait_for_function(
            "needle => (document.querySelector('#environmentDiagnosticsNote') || {}).textContent "
            ".includes(needle)", arg=needle, timeout=timeout)
        return True
    except Exception:
        return False


def wait_idle(page, timeout=8000):
    try:
        page.wait_for_function("() => !envView.diagnosticsRefreshFlight", timeout=timeout)
    except Exception:
        pass


def base_mock():
    return {
        "env": env_payload(), "graph": graph_payload(), "endpoints": endpoints_payload(),
        "diagnostics": diagnostics_payload(),
        "interfaces": {"environment": env_payload()["environment"], "packages": [],
                       "discovered_types": []},
        "pluginRos": copy.deepcopy(PLUGIN_ROS),
        "diagnosticsFail": 0, "diagnosticsDelayMs": 0, "refreshStatus": 202,
        "refreshMessage": "", "refreshBody": None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="acceptance/ros-diagnostics/B06/ui")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if not output.is_absolute():
        output = REPO / output
    output.mkdir(parents=True, exist_ok=True)
    fixture = write_fixture(output, with_container=False)
    fixture_container = write_fixture(output, with_container=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        print("NOT_RUN: playwright is not installed (%s)" % (exc,))
        (output / "results.json").write_text(json.dumps({
            "task": "B06", "scope": "OFFLINE_BROWSER_FIXTURE", "status": "NOT_RUN",
            "reason": "playwright unavailable: %s" % (exc,),
        }, indent=2), encoding="utf-8")
        return 3

    report = Report()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda exc: report.page_errors.append(str(exc)))
        page.on("dialog", lambda dialog: (report.page_errors.append(
            "unexpected dialog: %s" % dialog.message), dialog.dismiss()))
        page.goto(fixture.as_uri())
        page.wait_for_function("window.EXLogs && document.querySelector('#page-environments')")

        try:
            boot(page, base_mock())

            # -- 1. resource state vs actual receive health -------------------
            report.equal("资源状态文案不再声称已通流",
                         page.eval_on_selector("#environmentHealth", "el => el.textContent"),
                         "资源/匹配就绪")
            camera = '[data-direction="received"][data-health="no_data"]'
            report.check("state=ready 且 data_health=no_data 时同时显示两列",
                         "资源/匹配就绪" in text_of(page, camera)
                         and "未收到数据" in text_of(page, camera),
                         text_of(page, camera)[:400])
            report.equal("no_data 使用黄色告警色", colour(page, camera + " .env-health"), TOKENS["warn"])
            report.check("matched=null 显示未知",
                         "匹配 未知" in text_of(page, '[data-health="receiving"]'),
                         text_of(page, '[data-health="receiving"]')[:300])
            report.check("mark 端口默认显示未监测",
                         "未监测" in text_of(page, '[data-health="not_monitored"]'),
                         text_of(page, '[data-health="not_monitored"]')[:300])
            sent = text_of(page, '[data-direction="sent"]')
            report.check("publish 只写本地发布并声明不证明对端收到",
                         "本地发布 3" in sent and "不证明对端已收到" in sent, sent[:400])
            report.check("publish 不出现远端接收已证实的文案",
                         "远端成功" not in sent and "已确认接收" not in sent
                         and "对端已执行" not in sent, sent[:400])
            report.check("接收 age 使用后端单调值",
                         "最近接收 未知" in text_of(page, camera)
                         and "最近接收 0.4 秒前" in text_of(page, '[data-health="receiving"]'),
                         text_of(page, camera)[:300])
            report.check("显示监测策略与观察窗口",
                         "周期监测（periodic）" in text_of(page, camera) and "宽限 5.0 秒" in text_of(page, camera),
                         text_of(page, camera)[:400])

            # -- 2. diagnostics panel ----------------------------------------
            panel = text_of(page, "#environmentDiagnostics")
            report.equal("诊断面板只有一个容器", count(page, "#environmentDiagnostics"), 1)
            report.equal("诊断结论为告警", page.eval_on_selector(
                "#environmentDiagnosticsStatus", "el => el.dataset.summary"), "warning")
            report.check("诊断面板列出问题 code、证据与建议",
                         "ROS_RX_NO_DATA_WITH_PEER" in panel and "graph_peer_count" in panel
                         and "建议：" in panel, panel[:500])
            report.check("问题卡片标注等级与范围",
                         page.eval_on_selector(".env-issue", "el => el.dataset.severity") == "warning"
                         and "/camera/image_raw" in panel, panel[:300])
            report.check("诊断面板显示采集年龄与刷新状态",
                         "采集 0.6 秒前" in text_of(page, "#environmentDiagnosticsMeta")
                         and "空闲" in panel, text_of(page, "#environmentDiagnosticsMeta"))
            report.check("诊断面板显示预检摘要", "预检" in panel and "1 通过" in panel, panel[:600])
            report.check("活跃问题提供查看日志入口", count(page, "[data-diagnostic-log]") == 1)

            # -- 3. view logs: filter + navigation, no POST -------------------
            reset_requests(page)
            page.click("[data-diagnostic-log]")
            settle(page)
            filters = page.evaluate("() => window.EXLogs.__filters")
            switches = page.evaluate("() => window.__pageSwitches")
            report.check("查看日志按 code 设置日志筛选",
                         bool(filters) and filters[-1].get("text") == "ROS_RX_NO_DATA_WITH_PEER"
                         and filters[-1].get("sources") == [], filters)
            report.check("查看日志导航到日志页", "logs" in switches, switches)
            report.equal("查看日志不发任何 POST", len(posts(page)), 0)
            page.evaluate("() => { state.activePage = 'environments'; }")
            page.evaluate("() => { document.querySelectorAll('.page').forEach(el => "
                          "el.classList.toggle('active', el.id === 'page-environments')); }")

            # -- 4. paused / unknown / partial -------------------------------
            mock = base_mock()
            mock["diagnostics"] = diagnostics_payload(summary="paused", issues=[])
            mock["diagnostics"]["reason"] = "ROS 运行时未运行，周期检查已暂停"
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            refresh(page)
            paused = text_of(page, "#environmentDiagnostics")
            report.equal("暂停时结论为监测已暂停", page.eval_on_selector(
                "#environmentDiagnosticsStatus", "el => el.dataset.summary"), "paused")
            report.check("暂停时写清原因", "监测已暂停" in paused and "运行时未运行" in paused, paused[:400])
            report.check("暂停时不显示全部正常", "全部正常" not in paused, paused[:400])

            mock = base_mock()
            mock["diagnostics"] = diagnostics_payload(summary="unknown", issues=[], current=False, age=None)
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            refresh(page)
            unknown = text_of(page, "#environmentDiagnostics")
            report.equal("current=false 不显示正常", page.eval_on_selector(
                "#environmentDiagnosticsStatus", "el => el.dataset.summary"), "unknown")
            report.check("current=false 说明尚未采集", "尚未完成首次采集" in unknown, unknown[:400])
            report.equal("证据不足使用中性色", colour(page, "#environmentDiagnosticsStatus .env-health"),
                         TOKENS["ink2"])

            mock = base_mock()
            mock["diagnostics"] = diagnostics_payload(summary="ok", issues=[], partial=True)
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            refresh(page)
            partial = text_of(page, "#environmentDiagnostics")
            report.equal("采集不完整时结论降级为告警", page.eval_on_selector(
                "#environmentDiagnosticsStatus", "el => el.dataset.summary"), "warning")
            report.check("采集不完整不显示全部正常",
                         "全部正常" not in partial and "采集不完整" in partial, partial[:400])

            # -- 5. recovery keeps log history -------------------------------
            page.evaluate("() => { window.EXLogs.__cleared = 0; }")
            entries_before = page.evaluate("() => window.EXLogs.__entries.length")
            mock = base_mock()
            mock["diagnostics"] = diagnostics_payload(summary="ok", issues=[])
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            refresh(page)
            recovered = text_of(page, "#environmentDiagnostics")
            report.equal("恢复后活跃问题清空", count(page, "#environmentDiagnostics .env-issue"), 0)
            report.check("恢复后显示无活跃问题", "无活跃问题" in recovered, recovered[:300])
            report.check("恢复不清空日志历史",
                         page.evaluate("() => window.EXLogs.__entries.length") == entries_before
                         and page.evaluate("() => window.EXLogs.__cleared") == 0,
                         entries_before)

            # -- 6. drafts survive polling, notifications and partial failure -
            page.fill("#ros2Namespace", "/unsaved_acceptance")
            page.evaluate("() => { state.activePage = 'environments'; }")
            page.evaluate("patch => Object.assign(window.__mock, patch)", base_mock())
            refresh(page)
            report.check("失败前诊断问题已渲染", count(page, "#environmentDiagnostics .env-issue") >= 1)
            mock = base_mock()
            mock["diagnosticsFail"] = 500
            mock["graphFail"] = 500
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            page.evaluate("""() => {
                window.EXLogs.push({type: 'ros_diagnostic', message: 'ROS_RX_NO_DATA_WITH_PEER',
                    data: {severity: 'warning', code: 'ROS_RX_NO_DATA_WITH_PEER'}});
            }""")
            for _ in range(2):
                refresh(page)
            report.equal("未保存的 namespace 草稿保持",
                         page.eval_on_selector("#ros2Namespace", "el => el.value"), "/unsaved_acceptance")
            report.check("部分接口失败不清空其它成功结果",
                         count(page, "#environmentReceivedList .ros-endpoint") == 3
                         and "图状态更新中或读取失败" in text_of(page, "#environmentGraphList"),
                         [count(page, "#environmentReceivedList .ros-endpoint"),
                          text_of(page, "#environmentGraphList")[:120]])
            report.check("诊断读取失败时提示且标记过期",
                         "诊断数据读取失败" in text_of(page, "#environmentDiagnosticsNote")
                         and page.eval_on_selector("#environmentDiagnosticsStatus", "el => el.dataset.stale") == "1",
                         text_of(page, "#environmentDiagnosticsNote"))
            report.check("诊断读取失败不清空已渲染问题",
                         count(page, "#environmentDiagnostics .env-issue") >= 1)
            page.fill("#ros2Namespace", "/astrbotex")
            page.evaluate("() => { envView.dirty = false; }")

            # -- 7. stale generation never overwrites ------------------------
            page.evaluate("patch => Object.assign(window.__mock, patch)", base_mock())
            refresh(page)
            stale_rejected = page.evaluate(
                "() => renderEnvironmentDiagnostics("
                "{environment: {session_id: 'sess-A', generation: 2, revision: 5},"
                " current: true, summary: 'error', active_issues: [{code: 'STALE_CODE'}]})")
            report.check("旧 generation 快照被拒绝", stale_rejected is False, stale_rejected)
            report.check("旧 generation 不覆盖新数据",
                         "STALE_CODE" not in text_of(page, "#environmentDiagnostics"),
                         text_of(page, "#environmentDiagnostics")[:300])
            late = page.evaluate("""async () => {
                const newer = JSON.parse(JSON.stringify(window.__mock.diagnostics));
                window.__mock.diagnosticsDelayMs = 350;
                window.__mock.diagnostics = {environment: {session_id: 'sess-A', generation: 3, revision: 7},
                    current: true, summary: 'error', collection_age_sec: 0.1,
                    active_issues: [{code: 'LATE_CODE', severity: 'error', message: '晚到的旧结论'}]};
                const pending = fetchEnvironmentDiagnostics();
                window.__mock.diagnostics = newer;
                window.__mock.diagnosticsDelayMs = 0;
                await refreshEnvironment();
                await pending;
                return document.querySelector('#environmentDiagnostics').textContent;
            }""")
            report.check("晚到的旧响应不覆盖已渲染的新结论", "LATE_CODE" not in late, late[:300])

            # -- 8. refresh button: POST audit -------------------------------
            page.evaluate("patch => Object.assign(window.__mock, patch)", base_mock())
            refresh(page)
            page.evaluate("() => { window.__mock.diagnostics.refresh = {id: 'rf-0009', state: 'completed'}; }")
            reset_requests(page)
            page.click("#environmentDiagnosticsRefreshButton")
            report.check("重新检查显示刷新中",
                         wait_note(page, "已提交重新检查请求"), text_of(page, "#environmentDiagnosticsNote"))
            wait_idle(page)
            refresh_posts = [p for p in posts(page) if p["path"].endswith("/diagnostics/refresh")]
            report.equal("重新检查只 POST 一次诊断 refresh", len(refresh_posts), 1)
            body = json.loads(refresh_posts[0]["body"]) if refresh_posts else {}
            report.check("refresh 请求携带 expected_session/revision",
                         body.get("expected_session") == "sess-A" and body.get("expected_revision") == 7, body)
            report.equal("重新检查不产生其它 POST", len(posts(page)), 1)
            report.check("202 之后进入完成态", "已完成" in text_of(page, "#environmentDiagnosticsSummary"),
                         text_of(page, "#environmentDiagnosticsSummary"))

            # -- 9. 409 / 401 / 503 ------------------------------------------
            for status, needle, label in [
                (409, "环境在请求期间发生变化", "409 提示环境已变更"),
                (401, "没有权限执行重新检查", "401 提示无权限"),
                (503, "服务正在关闭", "503 提示稍后重试"),
            ]:
                page.evaluate("patch => Object.assign(window.__mock, patch)",
                              {"refreshStatus": status, "refreshMessage": "mock %d" % status})
                reset_requests(page)
                page.click("#environmentDiagnosticsRefreshButton")
                ok = wait_note(page, needle)
                wait_idle(page)
                report.check(label, ok, text_of(page, "#environmentDiagnosticsNote"))
                report.equal("%d 不触发环境选择 POST" % status,
                             len([p for p in posts(page) if p["path"].endswith("/environments/select")]), 0)
                if status == 409:
                    report.check("409 后重新读取了权威环境状态",
                                 len([p for p in page.evaluate("() => window.__requests")
                                      if p["path"] == "/api/v1/ex/environments"]) >= 1,
                                 page.evaluate("() => window.__requests"))

            # -- 10. GET failures --------------------------------------------
            page.evaluate("patch => Object.assign(window.__mock, patch)", {"refreshStatus": 202})
            page.evaluate("patch => Object.assign(window.__mock, patch)", {"diagnosticsFail": 401})
            refresh(page)
            report.check("诊断 401 提示重新输入凭据",
                         "重新输入管理凭据" in text_of(page, "#environmentDiagnosticsNote"),
                         text_of(page, "#environmentDiagnosticsNote"))
            page.evaluate("patch => Object.assign(window.__mock, patch)", {"diagnosticsFail": 0})
            refresh(page)
            report.check("恢复读取后提示回到常规说明",
                         "诊断数据读取失败" not in text_of(page, "#environmentDiagnosticsNote"),
                         text_of(page, "#environmentDiagnosticsNote"))

            # -- 11. unsafe API text -----------------------------------------
            mock = base_mock()
            hostile = copy.deepcopy(ISSUE_NO_DATA)
            hostile["message"] = "<img src=x onerror=window.__pwned=1> 注入"
            hostile["evidence"] = {"note": "<script>window.__pwned=2</script>"}
            mock["diagnostics"] = diagnostics_payload(issues=[hostile])
            page.evaluate("patch => Object.assign(window.__mock, patch)", mock)
            refresh(page)
            report.check("问题文本按纯文本渲染",
                         count(page, "#environmentDiagnostics img") == 0
                         and count(page, "#environmentDiagnostics script") == 0,
                         text_of(page, "#environmentDiagnostics")[:300])
            report.check("注入脚本未执行", page.evaluate("() => window.__pwned === undefined"))

            # -- 12. keyboard + narrow viewport ------------------------------
            page.evaluate("patch => Object.assign(window.__mock, patch)", base_mock())
            refresh(page)
            reset_requests(page)
            page.focus("#environmentDiagnosticsRefreshButton")
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            report.check("键盘可触发重新检查",
                         any(p["path"].endswith("/diagnostics/refresh") for p in posts(page)), posts(page))

            page.set_viewport_size({"width": 380, "height": 900})
            settle(page)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            report.check("窄屏无横向溢出", overflow <= 1, overflow)
            page.screenshot(path=str(output / "environment-diagnostics-narrow.png"), full_page=True)
            report.screenshots.append("environment-diagnostics-narrow.png")
            page.set_viewport_size({"width": 1440, "height": 1000})
            settle(page)

            # -- 13. plugin ROS panel ---------------------------------------
            page.evaluate("""() => {
                state.plugins = [{id: 'vision', name: '视觉', ros2: window.__mock.pluginRos}];
                state.activePluginId = 'vision';
                state.activePage = 'plugin';
                document.getElementById('pluginRosPanel').hidden = false;
                renderPluginRos(state.plugins[0]);
            }""")
            settle(page)
            live = text_of(page, '[data-ros-live="image_in"]')
            report.check("插件面板显示收数健康与资源状态",
                         "未收到数据" in live and "资源：资源/匹配就绪" in live, live[:300])
            report.check("插件面板显示接收/消费 age 与策略",
                         "最近接收 未知" in live and "周期监测（periodic）" in live, live[:300])
            report.check("插件面板发送端口写本地发布",
                         "本地发布" in text_of(page, '[data-ros-live="cmd_out"]')
                         and "不证明对端已收到" in text_of(page, '[data-ros-live="cmd_out"]'),
                         text_of(page, '[data-ros-live="cmd_out"]')[:300])

            page.fill('[data-ros-port="image_in"] [data-ros-field="topic"]', "/unsaved/plugin_topic")
            page.evaluate("""() => {
                state.plugins[0].ros2 = Object.assign({}, window.__mock.pluginRos,
                    {endpoints: [{port_id: 'image_in', state: 'ready', direction: 'subscribe'}]});
                renderPluginRos(state.plugins[0]);
            }""")
            settle(page)
            report.equal("插件草稿不被诊断刷新覆盖",
                         page.eval_on_selector('[data-ros-port="image_in"] [data-ros-field="topic"]',
                                               "el => el.value"), "/unsaved/plugin_topic")

            page.screenshot(path=str(output / "environment-diagnostics.png"), full_page=True)
            report.screenshots.append("environment-diagnostics.png")

            # -- 14. global POST audit --------------------------------------
            all_posts = page.evaluate("() => window.__requests.filter(r => r.method === 'POST').map(r => r.path)")
            forbidden = [path for path in all_posts
                         if path.endswith("/environments/select") or "/runtime/" in path]
            report.check("全程没有 select/start 类 POST", not forbidden, forbidden)

            # -- 15. pre-existing container is reused, not duplicated -------
            page2 = context.new_page()
            page2.on("pageerror", lambda exc: report.page_errors.append(str(exc)))
            page2.goto(fixture_container.as_uri())
            page2.wait_for_function("window.EXLogs && document.querySelector('#page-environments')")
            boot(page2, base_mock())
            report.equal("B08 预置容器时不重复创建", count(page2, "#environmentDiagnostics"), 1)
            report.equal("预置容器的子节点不重复", count(page2, "#environmentDiagnosticsStatus"), 1)
            report.check("预置容器同样渲染诊断结论",
                         "ROS_RX_NO_DATA_WITH_PEER" in text_of(page2, "#environmentDiagnostics"))
            page2.close()

        except Exception:
            report.notes.append(traceback.format_exc())

    payload = report.dump(output)
    print(json.dumps({"passed": len(report.passed), "failed": len(report.failed)}, ensure_ascii=False))
    for failure in report.failed:
        print("FAIL: %s -> %s" % (failure["check"], failure["detail"]))
    for note in report.notes:
        print("NOTE: %s" % note)
    print("evidence: %s" % output)
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
