"""Full dashboard + authenticated HTTP; isolated temp instance, no ROS/actions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.management import ManagementSettings
from playwright.sync_api import sync_playwright, expect

OUTPUT = Path(__file__).resolve().parent / 'ui-integrated'
OUTPUT.mkdir(parents=True, exist_ok=True)
checks, requests, page_errors = [], [], []
def check(name, condition=True):
    if not condition: raise AssertionError(name)
    checks.append(name)


def sse(event, event_id):
    return 'id: ' + event_id + '\r\nevent: event\r\n' + ''.join('data: ' + line + '\r\n' for line in json.dumps(event, ensure_ascii=False, indent=2).splitlines()) + '\r\n'


def main():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    shutil.copytree(Path('D:/Code/EXplugin/special/ros2_echo'), root / 'plugins/special/ros2_echo')
    with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': str(root), 'ASTRBOTEX_STT_ENABLED': '', 'ASTRBOTEX_TTS_ENABLED': ''}):
        server = build_server('127.0.0.1', 0, 20, management_settings=ManagementSettings())
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    token_file = root / 'secrets/admin.token'
    token = token_file.read_text().strip()
    origin = 'http://127.0.0.1:' + str(server.server_address[1])
    bus = server.controller.runtime.event_bus
    result = {'status': 'FAIL', 'checks': checks, 'requests': requests, 'page_errors': page_errors}
    try:
        with sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1080}, accept_downloads=True)
            page.on('pageerror', lambda error: page_errors.append(str(error)))
            def request_seen(request):
                row = {'path': urlparse(request.url).path, 'method': request.method}
                cursor = request.header_value('last-event-id')
                if cursor: row['last_event_id'] = cursor
                requests.append(row)
            page.on('request', request_seen)
            def login():
                page.locator('#managementCredential').fill(token)
                page.locator('#managementCredentialApply').click()
                page.wait_for_function("state.eventSource && state.eventSource.readyState === 1")
                expect(page.locator('#managementCredential')).to_have_value('')
            page.goto(origin + '/#/logs')
            login()
            check('memory-only credential bootstrap and authenticated SSE')
            bus.emit('runtime_state', 'normal green log', state='idle')
            bus.emit('ros_diagnostic', 'publisher visible, no callback', severity='warning', source='ros.port',
                     code='ROS_RX_NO_DATA_WITH_PEER', scope={'plugin_id': 'yolo', 'port_id': 'image', 'topic': '/camera'},
                     evidence={'graph_peer_count': 1, 'matched_peer_count': 1, 'receive_age_sec': None}, remediation=['check producer and QoS'])
            bus.emit('plugin_fault', 'inference error red log', plugin_id='yolo')
            for level, color in [('INFO', 'rgb(108, 192, 112)'), ('WARNING', 'rgb(255, 209, 102)'), ('ERROR', 'rgb(255, 107, 107)')]:
                locator = page.locator(f'#logConsole .log-line[data-level="{level}"] .log-message').last
                expect(locator).to_be_visible()
                check(level + ' body color in actual page', locator.evaluate('(el) => getComputedStyle(el).color') == color)
            check('black terminal in actual page', page.locator('#logConsole').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(17, 17, 17)')
            check('diagnostic scope and suggestion in visible summary', 'yolo/image' in page.locator('#logConsole').inner_text() and 'check producer' in page.locator('#logConsole').inner_text())
            page.screenshot(path=str(OUTPUT / 'logs-three-levels.png'), full_page=True)
            previous = page.evaluate('lastEventId')
            page.locator('#logClearButton').click()
            expect(page.locator('#logConsole .log-line')).to_have_count(0)
            check('clear preserves replay cursor', page.evaluate('lastEventId') == previous)
            page.evaluate('connectEvents()')
            page.wait_for_function("state.eventSource.readyState === 1")
            page.wait_for_timeout(200)
            expect(page.locator('#logConsole .log-line')).to_have_count(0)
            check('clear does not replay old history after reconnect')
            check('reconnect uses Last-Event-ID header', any(row.get('last_event_id') == previous for row in requests))
            # Exercise real app.js against a fetch-compatible byte stream. No
            # product test hook, second EventSource or browser credential store.
            page.evaluate(r"""() => {
              window.__savedFetch = window.fetch;
              window.__sseQueue = [];
              window.__readerCount = 0; window.__readerMax = 0;
              window.__mockCursors = [];
              window.fetch = async (target, options) => {
                if (!String(target).endsWith('/api/events')) return window.__savedFetch(target, options);
                window.__mockCursors.push(new Headers(options.headers).get('Last-Event-ID'));
                const text = window.__sseQueue.shift() || ': waiting\n\n';
                const bytes = new TextEncoder().encode(text);
                let active = true;
                const done = () => { if (active) {active = false; window.__readerCount--;} };
                const stream = new ReadableStream({
                  start(controller) {
                    window.__readerCount++; window.__readerMax = Math.max(window.__readerMax, window.__readerCount);
                    options.signal.addEventListener('abort', () => {done(); controller.error(new DOMException('aborted', 'AbortError'));}, {once:true});
                    const step = bytes.length > 10000 ? 4000 : 1;
                    for (let index = 0; index < bytes.length; index += step) controller.enqueue(bytes.slice(index, index + step));
                  }, cancel() {done();}
                });
                return new Response(stream, {headers:{'Content-Type':'text/event-stream'}});
              };
            }""")
            attack = {'type': 'vision_health', 'message': '中文跨块 <img src=x onerror="window.__injected=1">',
                      'data': {'severity': 'warning', 'source': 'vision.yolo'}}
            page.evaluate('(text) => {__sseQueue.push(text); connectEvents();}', sse(attack, 'mock-stream:1'))
            page.wait_for_function("lastEventId === 'mock-stream:1'")
            page.wait_for_timeout(150)
            check('UTF-8 split at each byte, CRLF and multiline data parsed', '中文跨块' in page.locator('#logConsole').inner_text())
            check('HTML injection is plain text', page.locator('#logConsole img').count() == 0 and page.evaluate('window.__injected || null') is None)
            page.evaluate(r"""() => { window.__notices = 0; window.__oldNotify = window.DecisionPage.notify;
              window.DecisionPage.notify = () => {window.__notices++;}; EXLogs.setFilter({text:'hidden-filter'}); }""")
            decision = {'type': 'decision_changed', 'message': 'changed', 'data': {'request_id': 'test'}}
            replay = sse(decision, 'mock-stream:2')
            reset = 'event: log_control\r\ndata: {"kind":"reset","reason":"cursor_ahead","stream_id":"mock-stream"}\r\n\r\n'
            page.evaluate('(text) => {__sseQueue.push(text); connectEvents();}', replay + replay + reset + replay)
            page.wait_for_function("lastEventId === 'mock-stream:2'")
            check('replay and same-stream reset notify business once', page.evaluate('__notices') == 1)
            check('filtering does not suppress business notification', page.locator('#logConsole .log-line').count() == 0)
            expect(page.locator('#logGapNotice')).to_be_visible()
            check('reset visibly explains lost continuity')
            good = sse({'type': 'vision_health', 'message': 'buffer recovered', 'data': {'severity': 'info'}}, 'mock-stream:3')
            page.evaluate('(texts) => {__sseQueue.push(...texts); connectEvents();}', ['id: mock-stream:99\ndata: ' + 'x' * 131073 + '\n\n', good])
            page.wait_for_function("state.eventSource.readyState === 0")
            check('oversized frame does not advance cursor', page.evaluate('lastEventId') == 'mock-stream:2')
            page.wait_for_function("lastEventId === 'mock-stream:3'", timeout=7000)
            check('oversized frame reconnects from last processed cursor', page.evaluate('__mockCursors.slice(-1)[0]') == 'mock-stream:2')
            check('one active streaming reader', page.evaluate('__readerMax') == 1)
            page.evaluate(r"""() => {state.eventSource.close(); window.fetch = window.__savedFetch;
              window.DecisionPage.notify = window.__oldNotify; EXLogs.setFilter({text:''});} """)
            login()
            check('credential epoch resets cursor and component dedup cache', page.locator('#logConsole .log-line').count() > 0)
            page.evaluate("managementCredential = 'invalid-test-only'; managementCredentialEpoch++; connectEvents();")
            page.wait_for_function('managementCredential === ""')
            count_before = len([row for row in requests if row['path'] == '/api/events'])
            page.wait_for_timeout(1300)
            check('401 waits for credentials instead of retry storm', len([row for row in requests if row['path'] == '/api/events']) == count_before)
            login()
            page.goto(origin + '/#/environments')
            expect(page.locator('#environmentDiagnostics')).to_be_visible()
            page.locator('#environmentDiagnosticsRefreshButton').click()
            page.wait_for_timeout(400)
            check('refresh only requests diagnostics', not any(row['method'] == 'POST' and (row['path'].endswith('/environments/select') or '/runtime/' in row['path']) for row in requests))
            check('runtime stays idle with action gate closed', server.controller.runtime.state.value == 'idle' and not server.action_service.status()['gate_open'])
            check('no browser page errors', not page_errors)
            page.screenshot(path=str(OUTPUT / 'environment.png'), full_page=True)
            browser.close()
        command = [sys.executable, str(REPO / 'scripts/verify_environment_ui.py'), '--url', origin, '--token-file', str(token_file), '--output', str(OUTPUT / 'existing-environment')]
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
        (OUTPUT / 'existing-environment.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
        check('existing environment UI script including drafts and offline reconnect', completed.returncode == 0)
        result['status'] = 'PASS'
    except Exception:
        result['error'] = traceback.format_exc()
        print(result['error'])
    finally:
        server.shutdown(); thread.join(3); server.server_close(); temp.cleanup()
        result['passed'] = len(checks)
        (OUTPUT / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'passed': len(checks), 'page_errors': len(page_errors)}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__': sys.exit(main())
