"""B05 browser acceptance: black terminal, coloured levels, filters, export.

The script builds a static fixture page next to the evidence directory, loads the
*real* ``dashboard/styles.css`` and ``dashboard/logs.css`` plus the real
``dashboard/logs.js``, and drives ``window.EXLogs`` the way B08 will. It adds no
test API to the product and no route to the running server.

Run:
    python scripts/verify_log_ui.py --output acceptance/ros-diagnostics/B05/ui

Exit codes: 0 all assertions passed, 1 assertion failed, 3 Playwright missing
(NOT_RUN - the component cannot be judged without a browser).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"

# C05 fixed tokens, compared as computed colours.
TOKENS = {
    "background": "rgb(17, 17, 17)",
    "normal": "rgb(108, 192, 112)",
    "warning": "rgb(255, 209, 102)",
    "error": "rgb(255, 107, 107)",
    "metadata": "rgb(156, 163, 175)",
}

FIXTURE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>EX logs fixture (B05)</title>
<link rel="stylesheet" href="{styles}" />
<link rel="stylesheet" href="{logs_css}" />
<style>
  body {{ margin: 0; padding: 20px; background: #F5F8F6; }}
  #page-logs {{ max-width: 1200px; margin: 0 auto; }}
</style>
</head>
<body>
<section class="page active" id="page-logs">
  <header class="page-head">
    <div class="page-head-actions">
      <div class="status-pill" id="logConnectionPill">
        <span class="sq" aria-hidden="true"></span><span data-label>日志流连接中</span>
      </div>
      <button class="btn btn-ghost btn-sm" id="logAutoscrollButton">自动滚动: 开</button>
      <button class="btn btn-ghost btn-sm" id="logClearButton">清空</button>
    </div>
  </header>
  <section class="panel log-panel">
    <div class="panel-head">
      <span class="panel-num">03.A</span><h2>事件流</h2>
      <span class="panel-meta" id="eventStatus">SSE 未连接</span>
    </div>
    <div class="log-toolbar">
      <div class="log-level-filters" id="logLevelFilters"></div>
      <select class="log-source-filter" id="logSourceFilter" aria-label="来源筛选"></select>
      <input class="log-search-input" id="logSearchInput" type="search" placeholder="搜索纯文本" />
      <button class="log-export-button" id="logExportButton" type="button">导出可见日志</button>
      <span class="log-unread-count" id="logUnreadCount" hidden></span>
    </div>
    <div class="log-gap" id="logGapNotice" hidden></div>
    <div class="log-console" id="logConsole"></div>
  </section>
</section>
<script src="{logs_js}"></script>
<script>window.EXLogs.mount();</script>
</body>
</html>
"""


def event(event_type, message, *, severity=None, code=None, source=None, data=None,
          timestamp=1700000000.0):
    payload = dict(data or {})
    if severity:
        payload["severity"] = severity
    if code:
        payload["code"] = code
    if source:
        payload["source"] = source
    return {"type": event_type, "message": message, "timestamp": timestamp, "data": payload}


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
            self.failed.append({"check": name, "detail": str(detail)})
        return bool(condition)

    def equal(self, name, actual, expected):
        return self.check(name, actual == expected, "expected %r, got %r" % (expected, actual))

    def dump(self, output: Path):
        payload = {
            "task": "B05",
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


def write_fixture(output: Path) -> Path:
    styles = os.path.relpath(DASHBOARD / "styles.css", output).replace("\\", "/")
    logs_css = os.path.relpath(DASHBOARD / "logs.css", output).replace("\\", "/")
    logs_js = os.path.relpath(DASHBOARD / "logs.js", output).replace("\\", "/")
    page = output / "fixture.html"
    page.write_text(FIXTURE.format(styles=styles, logs_css=logs_css, logs_js=logs_js),
                    encoding="utf-8")
    return page


def color_of(page, selector, prop="color"):
    return page.eval_on_selector(
        selector, "(element, name) => getComputedStyle(element)[name]", prop)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="acceptance/ros-diagnostics/B05/ui")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if not output.is_absolute():
        output = REPO / output
    output.mkdir(parents=True, exist_ok=True)
    fixture = write_fixture(output)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        print("NOT_RUN: playwright is not installed (%s)" % (exc,))
        (output / "results.json").write_text(json.dumps({
            "task": "B05", "scope": "OFFLINE_BROWSER_FIXTURE", "status": "NOT_RUN",
            "reason": "playwright unavailable: %s" % (exc,),
        }, indent=2), encoding="utf-8")
        return 3

    report = Report()
    url = fixture.as_uri()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda exc: report.page_errors.append(str(exc)))
        page.on("dialog", lambda dialog: (report.page_errors.append(
            "unexpected dialog: %s" % dialog.message), dialog.dismiss()))
        page.goto(url)
        page.wait_for_function("window.EXLogs && document.querySelector('#logConsole')")
        settle = lambda: page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

        def push(events, meta=None):
            page.evaluate(
                "items => items.forEach(entry => window.EXLogs.push(entry.event, entry.meta || {}))",
                [{"event": item, "meta": (meta or {}).get(index, {})}
                 for index, item in enumerate(events)])
            settle()

        # -- 1. terminal surface ------------------------------------------
        report.equal("console background is #111111", color_of(page, "#logConsole", "backgroundColor"),
                     TOKENS["background"])

        push([
            event("runtime_state", "运行时已启动", source="runtime", data={"state": "running"}),
            event("vision_plugin", "检测到目标", severity="warn", source="vision.yolo"),
            event("plugin_fault", "夹爪插件执行失败", source="gripper"),
            event("ros_diagnostic", "严重：接口缺失", severity="critical", code="ROS_INTERFACE_MISSING",
                  source="ros.port"),
        ])
        report.check("four rows rendered", page.eval_on_selector_all(
            "#logConsole .log-line", "rows => rows.length") == 4,
            page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"))

        report.equal("INFO body text is green",
                     color_of(page, '.log-line[data-level="INFO"] .log-message'), TOKENS["normal"])
        report.equal("WARNING body text is yellow",
                     color_of(page, '.log-line[data-level="WARNING"] .log-message'), TOKENS["warning"])
        report.equal("ERROR body text is red",
                     color_of(page, '.log-line[data-level="ERROR"] .log-message'), TOKENS["error"])
        report.equal("CRITICAL body text is red",
                     color_of(page, '.log-line[data-level="CRITICAL"] .log-message'), TOKENS["error"])
        report.equal("metadata time is grey",
                     color_of(page, ".log-line .log-time"), TOKENS["metadata"])
        report.equal("INFO badge is green",
                     color_of(page, '.log-line[data-level="INFO"] .log-badge'), TOKENS["normal"])
        report.check("warning row is visibly marked before expanding",
                     page.eval_on_selector('.log-line[data-level="WARNING"]',
                                           "row => getComputedStyle(row).backgroundColor") != "rgba(0, 0, 0, 0)",
                     "warning rows carry a tinted background")
        page.screenshot(path=str(output / "logs-levels.png"), full_page=True)
        report.screenshots.append("logs-levels.png")

        # -- 2. level resolution ------------------------------------------
        report.check("plugin_fault resolves to ERROR without a severity field",
                     page.eval_on_selector_all(
                         '.log-line[data-level="ERROR"] .log-message',
                         "rows => rows.some(r => r.textContent.includes('夹爪插件执行失败'))"))
        page.evaluate("""() => {
            window.EXLogs.push({type: 'custom', message: 'explicit bus level',
                timestamp: 1700000001, data: {log: {level: 'WARNING'}}});
            window.EXLogs.push({type: 'fault', message: 'fault with low level',
                timestamp: 1700000002, data: {severity: 'debug', error: 'boom'}});
        }""")
        settle()
        report.check("data.log.level wins over the type",
                     page.eval_on_selector_all(".log-line",
                         "rows => rows.some(r => r.dataset.level === 'WARNING' && r.textContent.includes('explicit bus level'))"))
        report.check("an error payload cannot be downgraded to DEBUG",
                     page.eval_on_selector_all(".log-line",
                         "rows => rows.some(r => r.dataset.level === 'ERROR' && r.textContent.includes('fault with low level'))"))

        # -- 3. unsafe text ------------------------------------------------
        push([event("plugin", "<img src=x onerror=window.__pwned=1> & <b>bold</b>",
                    source="evil"),
              event("plugin", "\u001b[31mANSI\u001b[0m 与\u0007控制字符", source="evil")])
        report.check("markup is shown as text, not parsed",
                     page.evaluate("() => !document.querySelector('#logConsole img')"))
        report.check("injected handler never ran", page.evaluate("() => window.__pwned === undefined"))
        report.check("ANSI escape is stripped",
                     page.eval_on_selector_all(".log-line",
                         "rows => rows.some(r => r.textContent.includes('ANSI') && !r.textContent.includes('\\u001b'))"))
        report.check("control characters are stripped",
                     page.eval_on_selector_all(".log-line",
                         "rows => rows.every(r => !/[\\u0007\\u001b]/.test(r.textContent))"))

        # -- 4. dedupe by event id ----------------------------------------
        page.evaluate("() => window.EXLogs.clear()")
        settle()
        page.evaluate("""() => {
            const e = {type: 'runtime_state', message: '同一条事件', timestamp: 1700000003, data: {}};
            window.EXLogs.push(e, {eventId: 'stream:1'});
            window.EXLogs.push(e, {eventId: 'stream:1'});
            window.EXLogs.push({type: 'runtime_state', message: '同一条事件', timestamp: 1700000004, data: {}},
                               {eventId: 'stream:2'});
        }""")
        settle()
        report.equal("replayed id is deduplicated, same text with a new id is kept",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 2)

        # -- 5. filters ----------------------------------------------------
        page.evaluate("() => window.EXLogs.clear()")
        settle()
        push([
            event("runtime_state", "普通信息一", source="alpha"),
            event("vision_plugin", "黄色警告一", severity="warn", source="beta"),
            event("plugin_fault", "红色错误一", source="alpha"),
        ])
        page.uncheck('#logLevelFilters input[data-level="WARNING"]')
        settle()
        report.equal("level filter hides warnings",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 2)
        page.check('#logLevelFilters input[data-level="WARNING"]')
        settle()
        page.fill("#logSearchInput", "错误")
        settle()
        report.equal("literal search keeps only matches",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 1)
        page.fill("#logSearchInput", "[")
        settle()
        report.check("a bracket in the search box is treated as text, not a regex",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length") == 0)
        page.fill("#logSearchInput", "")
        settle()
        page.select_option("#logSourceFilter", "beta")
        settle()
        report.equal("source filter keeps only that source",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 1)
        page.select_option("#logSourceFilter", "")
        settle()

        # -- 6. autoscroll, unread and clear -------------------------------
        page.evaluate("""() => {
            for (let i = 0; i < 60; i += 1) {
                window.EXLogs.push({type: 'runtime_state', message: '填充行 ' + i,
                                    timestamp: 1700000100 + i, data: {}});
            }
        }""")
        settle()
        page.click("#logAutoscrollButton")
        settle()
        scroll_before = page.eval_on_selector("#logConsole", "el => el.scrollTop")
        page.evaluate("""() => {
            for (let i = 0; i < 10; i += 1) {
                window.EXLogs.push({type: 'runtime_state', message: '暂停后新日志 ' + i,
                                    timestamp: 1700000200 + i, data: {}});
            }
        }""")
        settle()
        report.equal("paused autoscroll keeps the scroll position",
                     page.eval_on_selector("#logConsole", "el => el.scrollTop"), scroll_before)
        report.check("unread counter appears while paused",
                     page.eval_on_selector("#logUnreadCount", "el => !el.hidden && el.textContent.includes('10')"),
                     page.eval_on_selector("#logUnreadCount", "el => el.textContent"))
        page.click("#logAutoscrollButton")
        settle()
        report.check("resuming scrolls to the bottom",
                     page.eval_on_selector("#logConsole", "el => Math.abs(el.scrollHeight - el.clientHeight - el.scrollTop) < 4"))
        report.check("unread counter clears on resume",
                     page.eval_on_selector("#logUnreadCount", "el => el.hidden"))

        page.click("#logClearButton")
        settle()
        report.equal("clear empties the console",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 0)
        page.evaluate("() => window.EXLogs.push({type: 'runtime_state', message: '清空后的新事件',"
                      " timestamp: 1700000300, data: {}}, {eventId: 'stream:9'})")
        settle()
        report.equal("history is not replayed after clear",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 1)

        # -- 7. gap and reset notices --------------------------------------
        page.evaluate("() => window.EXLogs.control({kind: 'gap', reason: 'queue_overflow',"
                      " stream_id: 's1', oldest_sequence: 41, newest_sequence: 90, dropped_count: 12})")
        settle()
        report.check("gap notice is visible and explains the drop",
                     page.eval_on_selector("#logGapNotice",
                                           "el => !el.hidden && el.textContent.includes('12')"),
                     page.eval_on_selector("#logGapNotice", "el => el.textContent"))
        page.evaluate("() => window.EXLogs.control({kind: 'reset', reason: 'server_restart'})")
        settle()
        report.check("reset notice is distinguishable from a gap",
                     page.eval_on_selector("#logGapNotice",
                                           "el => el.dataset.kind === 'reset' && el.textContent.includes('重启')"))

        # -- 8. detail expansion and truncation ----------------------------
        page.evaluate("() => window.EXLogs.clear()")
        settle()
        page.evaluate("""() => window.EXLogs.push({
            type: 'ros_diagnostic', message: '端口无数据', timestamp: 1700000400,
            data: {severity: 'warning', code: 'ROS_RX_NO_PEER', source: 'ros.port',
                   evidence: {graph_peer_count: 0, matched_peer_count: null,
                              note: 'x'.repeat(40000)}}})""")
        settle()
        page.click(".log-detail-toggle")
        settle()
        report.check("detail expands on click",
                     page.eval_on_selector(".log-detail", "el => !el.hidden"))
        report.check("oversized detail is truncated and labelled",
                     page.eval_on_selector(".log-detail",
                                           "el => el.textContent.includes('详情已截断') && el.textContent.length < 20000"),
                     page.eval_on_selector(".log-detail", "el => el.textContent.length"))
        page.screenshot(path=str(output / "logs-terminal.png"), full_page=True)
        report.screenshots.append("logs-terminal.png")

        # -- 9. cache and DOM bounds ---------------------------------------
        page.evaluate("() => window.EXLogs.clear()")
        settle()
        page.evaluate("""() => {
            for (let i = 0; i < 10000; i += 1) {
                window.EXLogs.push({type: 'runtime_state', message: 'bulk ' + i,
                                    timestamp: 1700000500 + i, data: {}},
                                   {eventId: 'bulk:' + i});
            }
        }""")
        settle()
        rows = page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length")
        report.check("10k events leave at most 1000 rendered rows", rows <= 1000, rows)
        report.check("the newest event survived the cache window",
                     page.eval_on_selector_all("#logConsole .log-line",
                         "rows => rows[rows.length - 1].textContent.includes('bulk 9999')"))

        # -- 10. export -----------------------------------------------------
        page.evaluate("() => window.EXLogs.clear()")
        settle()
        push([event("runtime_state", "导出这一行", source="alpha"),
              event("plugin_fault", "导出那一行", source="beta")])
        with page.expect_download() as download_info:
            page.click("#logExportButton")
        download = download_info.value
        exported = Path(download.path()).read_text(encoding="utf-8")
        report.check("export contains the visible rows",
                     "导出这一行" in exported and "导出那一行" in exported)
        report.check("export keeps level and code columns",
                     "[INFO]" in exported and "[ERROR]" in exported, exported[:200])
        report.check("export has no markup", "<" not in exported or "&lt;" not in exported)

        # -- 11. theme independence and listener hygiene -------------------
        page.evaluate("() => document.body.setAttribute('data-theme', 'dark')")
        settle()
        report.equal("dark theme keeps the terminal background",
                     color_of(page, "#logConsole", "backgroundColor"), TOKENS["background"])
        report.equal("dark theme keeps INFO green",
                     color_of(page, '.log-line[data-level="INFO"] .log-message'), TOKENS["normal"])
        page.evaluate("() => document.body.removeAttribute('data-theme')")

        page.evaluate("() => { window.EXLogs.mount(); window.EXLogs.mount(); }")
        settle()
        label_before = page.eval_on_selector("#logAutoscrollButton", "el => el.textContent")
        page.click("#logAutoscrollButton")
        settle()
        label_after = page.eval_on_selector("#logAutoscrollButton", "el => el.textContent")
        report.check("re-mounting does not stack duplicate listeners",
                     label_before != label_after and "关" in label_after,
                     "%r -> %r" % (label_before, label_after))

        page.evaluate("() => { window.EXLogs.destroy(); window.EXLogs.mount(); }")
        settle()
        page.evaluate("() => window.EXLogs.push({type: 'runtime_state', message: '重建后',"
                      " timestamp: 1700000600, data: {}})")
        settle()
        report.equal("destroy and mount starts from a clean console",
                     page.eval_on_selector_all("#logConsole .log-line", "rows => rows.length"), 1)

        context.close()
        browser.close()

    if report.page_errors:
        report.failed.append({"check": "no page errors", "detail": report.page_errors})
    payload = report.dump(output)
    print(json.dumps(payload["totals"], ensure_ascii=False))
    for item in report.failed:
        print("FAIL: %s -> %s" % (item["check"], item["detail"]))
    print("evidence: %s" % (output,))
    return 0 if not report.failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
