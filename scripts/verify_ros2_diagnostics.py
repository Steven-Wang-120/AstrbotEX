"""No-observer acceptance for the ROS 2 diagnostics pipeline (task B07).

The scenario this script exists for: the graph shows a publisher and a matched
subscription, EX receives nothing, and nobody notices because every existing
check says "ready". The script therefore never creates a second subscriber for
the scenario it is measuring - an extra observer would change the very topology
under test. `--with-observer` is a separate, explicitly requested round that is
recorded separately and can never overwrite a failed no-observer round.

Two scenarios:

``synthetic``
    Dedicated EX instance, normal mode, idle runtime, every other plugin
    disabled, action gate closed. The script publishes compressed images itself
    on an isolated domain and a unique topic, and reads EX's own callback
    counters through the management API and SSE.

``entry``
    The site's real entry point (for example ``run-yolo``) owns the camera and
    the inference process. The script starts it, watches EX, and never adds a
    publisher or a subscriber of its own.

``--dry-run`` prints the plan without reading the token, touching configuration
or importing ROS. Everything that would change the machine is described first.

Evidence is written to ``--output``: ``report.json`` plus ``timeline.jsonl``.
The token is only ever read from ``--token-file`` and only ever sent in an
Authorization header; it is never printed, logged, written to the report or
placed in a URL.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

SCHEMA = "astrex.ros2_diagnostics_acceptance.v1"
DEFAULT_DOMAIN = 73
MIN_DURATION_SEC = 30
MIN_REPEAT = 2
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
FRAME_INTERVAL_SEC = 0.2
FRAMES_PER_ROUND = 12
POLL_INTERVAL_SEC = 0.5
SCENARIOS = ("synthetic", "entry")

EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_NOT_RUN = 0, 1, 2, 3

# 285-byte 1x1 JPEG. Generated once; no image library is needed at runtime.
# The acceptance publisher sends these complete JPEG bytes as CompressedImage.
TINY_JPEG_B64 = (
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a'
    'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA'
    'AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEB'
    'AAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJ/AB//Z'
)


class AcceptanceError(RuntimeError):
    """A condition the run cannot continue past. Message never contains a token."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_ros2_diagnostics.py",
        description="Acceptance for ROS 2 receive diagnostics without a bypass observer.",
        epilog=(
            "Exit codes: 0 PASS, 1 FAIL, 2 usage error, 3 NOT_RUN/BLOCKED. "
            "A dry run never reads the token file and never imports ROS."
        ),
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765",
                        help="EX management origin; must be a loopback HTTP origin")
    parser.add_argument("--token-file", type=Path,
                        help="file holding the management credential; required unless --dry-run")
    parser.add_argument("--scenario", choices=SCENARIOS, default="synthetic")
    parser.add_argument("--domain", type=int, default=DEFAULT_DOMAIN,
                        help=f"ROS domain for this run (default {DEFAULT_DOMAIN}; the JetMax "
                             "site itself runs on 0, which must not be reused for synthetic)")
    parser.add_argument("--duration", type=int, default=MIN_DURATION_SEC,
                        help=f"seconds per round, minimum {MIN_DURATION_SEC}")
    parser.add_argument("--repeat", type=int, default=MIN_REPEAT,
                        help=f"number of independent rounds, minimum {MIN_REPEAT}")
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for report.json and timeline.jsonl")
    parser.add_argument("--entry", help="entry point to run in --scenario entry")
    parser.add_argument("--with-observer", action="store_true",
                        help="add a bypass subscriber for this round only; recorded separately")
    parser.add_argument("--dry-run", action="store_true",
                        help="describe the run without reading secrets or changing anything")
    return parser


def validate_args(args) -> list:
    """Every usage rule from the task book, checked before anything runs."""
    problems = []
    address = urlparse(args.url)
    if address.scheme != "http":
        problems.append("--url must use http")
    if address.hostname not in LOOPBACK_HOSTS:
        problems.append("--url must point at a loopback host (127.0.0.1/localhost/::1)")
    if address.username or address.password:
        problems.append("--url must not carry credentials")
    if address.path not in ("", "/"):
        problems.append("--url must be an origin without a path")
    if address.query or address.fragment:
        problems.append("--url must not carry a query or fragment")

    if args.duration < MIN_DURATION_SEC:
        problems.append(f"--duration must be >= {MIN_DURATION_SEC} seconds")
    if args.repeat < MIN_REPEAT:
        problems.append(f"--repeat must be >= {MIN_REPEAT} rounds")
    if not 0 <= args.domain <= 232:
        problems.append("--domain must be a ROS domain id (0-232)")

    if args.scenario == "entry":
        if not args.entry:
            problems.append("--scenario entry requires --entry")
        else:
            entry = Path(args.entry)
            if not entry.is_absolute():
                problems.append("--entry must be an absolute path")
            elif not entry.is_file():
                problems.append("--entry does not exist or is not a regular file")
            elif not os.access(entry, os.X_OK):
                problems.append("--entry is not executable")
    elif args.entry:
        problems.append("--entry is only valid with --scenario entry")

    if args.scenario == "synthetic" and args.domain == 0:
        problems.append("--scenario synthetic must not use domain 0: that is the live JetMax "
                        "site domain, not a test domain")

    if not args.dry_run:
        if args.token_file is None:
            problems.append("--token-file is required unless --dry-run")
        elif not args.token_file.is_file():
            problems.append("--token-file does not exist")
    return problems


def load_token(path: Path) -> str:
    """Read the credential from a file. Never log or echo the value."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AcceptanceError(f"cannot read --token-file ({exc.__class__.__name__})") from None
    if not token:
        raise AcceptanceError("--token-file is empty")
    if any(char.isspace() for char in token):
        raise AcceptanceError("--token-file contains whitespace; expected a single token")
    return token


# --------------------------------------------------------------------------- #
# plan (used by --dry-run and embedded in the report)
# --------------------------------------------------------------------------- #
def build_plan(args) -> dict:
    steps = [
        {"step": "preflight", "detail": "GET environments/status/plugins; require normal mode, "
                                        "idle runtime, action gate closed, other plugins disabled"},
        {"step": "snapshot", "detail": "record environment config, ros2 bindings and plugin "
                                       "enable flags for restore"},
    ]
    if args.scenario == "synthetic":
        steps += [
            {"step": "configure",
             "detail": f"set ros2 domain {args.domain} and a unique image topic; enable "
                       "diag_image_probe; select ros2 mode; start runtime"},
            {"step": "publisher",
             "detail": "create ONE CompressedImage publisher with sensor_data QoS; no subscriber "
                       "is created" + (" except the explicitly requested observer round"
                                       if args.with_observer else "")},
            {"step": "no_data",
             "detail": "publish nothing past startup_grace_sec; expect NO_DATA_WITH_PEER (yellow)"},
            {"step": "receiving",
             "detail": f"publish {FRAMES_PER_ROUND} embedded JPEG frames; expect rx/consume to "
                       "grow and one recovery event (green)"},
            {"step": "stale",
             "detail": "stop publishing past stale_after_sec; expect a stale warning (yellow)"},
            {"step": "recover",
             "detail": "resume frames; expect a second recovery"},
        ]
    else:
        steps += [
            {"step": "entry",
             "detail": f"start {args.entry or '<entry>'} as an argv array; the entry owns camera "
                       f"and inference; domain must equal the site EX domain ({args.domain})"},
            {"step": "observe",
             "detail": "poll EX diagnostics/endpoints and read authenticated SSE; no ros2 CLI, no "
                       "side rclpy node, no extra publisher"},
        ]
    steps.append({"step": "restore",
                  "detail": "stop owned publisher/entry, stop runtime, restore config, bindings "
                            "and plugin enable flags; each restore is attempted independently"})
    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "dry_run": True,
        "scenario": args.scenario,
        "url": args.url,
        "domain": args.domain,
        "duration_sec": args.duration,
        "repeat": args.repeat,
        "with_observer": bool(args.with_observer),
        "entry": args.entry,
        "reads_token": False,
        "imports_ros": False,
        "changes": [],
        "steps": steps,
        "notes": [
            "dry-run does not read --token-file, does not write configuration and does not "
            "import ROS",
            "each round must run at least 30 seconds and at least 2 rounds must be executed",
            "a round with --with-observer is reported separately and cannot replace a failed "
            "no-observer round",
            "results on Windows/mock are NOT_RUN for native ROS; only a Linux instance with "
            "Humble can produce a native PASS",
        ],
    }


# --------------------------------------------------------------------------- #
# management API
# --------------------------------------------------------------------------- #
class Api:
    """Minimal authenticated client. Never puts the token anywhere but the header."""

    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self._token = token
        self._opener = build_opener(ProxyHandler({}))

    def _request(self, method: str, path: str, payload=None, timeout: float = 10.0):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base}/api/v1/ex/{path.lstrip('/')}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._token},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise AcceptanceError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except URLError as exc:
            raise AcceptanceError(f"{method} {path} -> unreachable: {exc.reason}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise AcceptanceError(f"{method} {path} -> response is not JSON") from None

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, payload=None):
        return self._request("POST", path, payload or {})


class SseReader:
    """Reads the authenticated event stream and keeps the frames we care about."""

    def __init__(self, api: Api, path: str = "events"):
        self.api = api
        self.path = path
        self.frames = []
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self._response = None
        self._frame_bytes = 0
        self._lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="b07-sse", daemon=True)
        self._thread.start()

    def _run(self):
        request = Request(
            f"{self.api.base}/api/v1/ex/{self.path}",
            headers={"Accept": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "Authorization": "Bearer " + self.api._token},
        )
        try:
            with self.api._opener.open(request, timeout=30) as response:
                self._response = response
                fields = {}
                for raw in response:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if fields:
                            with self._lock:
                                self.frames.append(fields)
                                self._frame_bytes += len(json.dumps(fields).encode('utf-8'))
                                while len(self.frames) > 1000 or self._frame_bytes > 2 * 1024 * 1024:
                                    self._frame_bytes -= len(json.dumps(self.frames.pop(0)).encode('utf-8'))
                            fields = {}
                        continue
                    if line.startswith(":"):
                        continue
                    key, _, value = line.partition(":")
                    if key == 'data' and key in fields:
                        fields[key] += '\n' + value.lstrip(' ')
                    else:
                        fields[key] = value.lstrip(' ')
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the main flow
            if not self._stop.is_set():
                self.error = exc.__class__.__name__
        finally:
            self._response = None

    def snapshot(self) -> list:
        with self._lock:
            return list(self.frames)

    def close(self):
        self._stop.set()
        response = self._response
        if response is not None:
            try: response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError): pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive(): self.error = 'SSE_CLOSE_TIMEOUT'


# --------------------------------------------------------------------------- #
# evidence helpers
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self, output: Path):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.timeline_path = output / "timeline.jsonl"
        self._lock = threading.Lock()
        self.started_at = utc_now()

    def note(self, kind: str, **fields):
        row = {"at": utc_now(), "kind": kind}
        row.update(fields)
        with self._lock:
            with self.timeline_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_report(self, payload: dict):
        payload.setdefault("schema", SCHEMA)
        payload.setdefault("started_at", self.started_at)
        payload["finished_at"] = utc_now()
        (self.output / "report.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n")


def find_port(diagnostics: dict, port_id: str) -> dict:
    """Pick one port out of a snapshot without assuming nesting beyond the contract."""
    for row in (diagnostics.get("ports") or diagnostics.get('received', []) + diagnostics.get('sent', [])):
        if row.get("port_id") == port_id or row.get("id") == port_id:
            return row
    return {}


def port_health(diagnostics: dict, port_id: str) -> str:
    row = find_port(diagnostics, port_id)
    return (row.get("data_health") or (row.get("diagnostics") or {}).get("data_health")
            or "unknown")


def wait_for(predicate, timeout_s: float, poll_s: float = POLL_INTERVAL_SEC):
    """Poll a predicate until it is true; returns (ok, last_value)."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return True, last
        time.sleep(poll_s)
    return False, last


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def _select_mode(api, mode):
    api.post('environments/select', {'mode': mode})
    ok, _ = wait_for(lambda: (lambda env: env.get('active_mode') == mode and env.get('phase') == 'idle')(
        api.get('environments').get('environment') or {}), 20)
    if not ok: raise AcceptanceError('environment transition did not complete')


def _idle_gate_closed(api):
    status = api.get('status')
    return status.get('runtime_state') == 'idle' and (status.get('actions') or {}).get('gate_open') is False


def _initial_state(api, allowed):
    state = api.get('environments')
    if (state.get('environment') or {}).get('active_mode') != 'normal' or not _idle_gate_closed(api):
        raise AcceptanceError('dedicated instance must start in normal mode, idle, with action gate closed')
    plugins = api.get('plugins').get('plugins', [])
    if any(item.get('enabled') and item.get('id') not in allowed for item in plugins):
        raise AcceptanceError('disable unrelated plugins before native acceptance')
    return state, {item.get('id'): bool(item.get('enabled')) for item in plugins}


def _save_sse(sse, recorder):
    sse.close()
    (recorder.output / 'sse.json').write_text(json.dumps(sse.snapshot(), ensure_ascii=False, indent=2), encoding='utf-8')


def run_synthetic(api: Api, args, recorder: Recorder) -> dict:
    state, enabled = _initial_state(api, {'diag_image_probe'})
    fixture = 'diag_image_probe'
    if fixture not in enabled: raise AcceptanceError('install the disabled periodic_image fixture first')
    original_bindings = api.get(f'plugins/{fixture}/ros2')['ros2']['bindings']
    original_config = state['config']['ros2']
    rounds, cleanup, publisher = [], {}, None
    sse = SseReader(api)
    try:
        api.post('environments/ros2/config', {'config': {'domain_id': args.domain, 'discovery_interval_sec': .2}})
        api.post(f'plugins/{fixture}/enable', {})
        sse.start()
        for index in range(args.repeat):
            # A new native adapter, binding, publisher context and activity window
            # for every round. Cumulative counts can never stand in for new input.
            topic = f'/ex_diag_acceptance_{os.getpid()}_{time.monotonic_ns()}_{index}'
            bindings = json.loads(json.dumps(original_bindings))
            bindings.setdefault('image', {}).update(enabled=True, topic=topic)
            current = api.get(f'plugins/{fixture}/ros2')['ros2']['revision']
            api.post(f'plugins/{fixture}/ros2', {'expected_revision': current, 'bindings': bindings})
            _select_mode(api, 'ros2')
            publisher = _ros_publisher(topic, with_observer=args.with_observer, domain=args.domain)
            api.post('runtime/start', {})
            recorder.note('topology', round=index + 1, ex_pid=api.get('status').get('pid'), publisher_pid=os.getpid(),
                          domain=args.domain, topic=topic, with_observer=args.with_observer)
            rounds.append(_synthetic_round(api, args, recorder, publisher, index + 1))
            api.post('runtime/stop', {'reason': 'independent acceptance round complete'})
            _select_mode(api, 'normal')
            publisher.close(); publisher = None
            rounds[-1]['checks']['round_exit_idle_gate_closed'] = _idle_gate_closed(api)
    finally:
        if publisher is not None:
            try: publisher.close()
            except Exception: cleanup['publisher_close'] = False
        cleanup.update(_restore(api, recorder, fixture, original_bindings, original_config, enabled[fixture]))
        _save_sse(sse, recorder)
    return {'rounds': rounds, 'cleanup': cleanup, 'sse_frames': len(sse.snapshot()), 'sse_error': sse.error}


def _synthetic_round(api: Api, args, recorder: Recorder, publisher, index: int) -> dict:
    started = time.monotonic()
    result = {'round': index, 'checks': {}, 'started_at': utc_now()}
    before = find_port(api.get('environments/ros2/endpoints'), 'image')
    grace = _policy_seconds(api, 'image', 'startup_grace_sec', 2)
    stale = _policy_seconds(api, 'image', 'stale_after_sec', 2)
    def phase(health, code=None):
        value = api.get('environments/diagnostics')
        matched = port_health(value, 'image') == health
        if code: matched = matched and any(issue.get('code') == code for issue in value.get('active_issues', []))
        elif health == 'receiving':
            matched = matched and not any(issue.get('code') in ('ROS_RX_NO_PEER', 'ROS_RX_NO_DATA_WITH_PEER', 'ROS_RX_STALE')
                                         for issue in value.get('active_issues', []))
        if matched: recorder.note('diagnostics', round=index, phase=health, snapshot=value)
        return matched
    ok, _ = wait_for(lambda: phase('no_data', 'ROS_RX_NO_DATA_WITH_PEER'), grace + 10)
    result['checks']['no_data_with_peer'] = ok
    for _ in range(FRAMES_PER_ROUND):
        publisher.publish(); time.sleep(FRAME_INTERVAL_SEC)
    result['checks']['receiving'], _ = wait_for(lambda: phase('receiving'), 10)
    time.sleep(stale + 2)
    result['checks']['stale'], _ = wait_for(lambda: phase('stale', 'ROS_RX_STALE'), 10)
    for _ in range(FRAMES_PER_ROUND):
        publisher.publish(); time.sleep(FRAME_INTERVAL_SEC)
    result['checks']['recovered'], _ = wait_for(lambda: phase('receiving'), 10)
    while time.monotonic() - started < args.duration:
        publisher.publish(); time.sleep(FRAME_INTERVAL_SEC)
    after = find_port(api.get('environments/ros2/endpoints'), 'image')
    result['elapsed_sec'] = time.monotonic() - started
    result['input_count'] = publisher.sent
    result['rx_delta'] = after.get('rx_received', 0) - before.get('rx_received', 0)
    result['consume_delta'] = after.get('rx_consumed', 0) - before.get('rx_consumed', 0)
    result['binding'] = {key: after.get(key) for key in ('endpoint_id', 'owner_generation', 'environment_generation', 'binding_generation')}
    result['checks'].update(duration=result['elapsed_sec'] >= args.duration,
                            new_rx=result['rx_delta'] > 0, new_consumption=result['consume_delta'] > 0,
                            input_count=result['input_count'] > 0)
    result['finished_at'] = utc_now()
    recorder.note('round', **result)
    return result


def _policy_seconds(api: Api, port_id: str, key: str, fallback: float) -> float:
    try:
        policy = find_port(api.get("environments/diagnostics"), port_id).get("diagnostics") or {}
        value = (policy.get("policy") or {}).get(key, policy.get(key))
        return float(value) if value is not None else fallback
    except (AcceptanceError, TypeError, ValueError):
        return fallback


def _stop_entry(process):
    # Only this invocation's process group, created with start_new_session=True.
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=10)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except Exception:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=5)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    return False


def run_entry(api: Api, args, recorder: Recorder) -> dict:
    import subprocess
    state, enabled = _initial_state(api, {'yolo'})
    if 'yolo' not in enabled: raise AcceptanceError('install the reviewed yolo plugin first')
    original_bindings = api.get('plugins/yolo/ros2')['ros2']['bindings']
    original_config = state['config']['ros2']
    entry = Path(args.entry)
    command = [str(entry), '--duration', str(args.duration)]
    rounds, cleanup = [], {}
    sse = SseReader(api)
    try:
        sse.start()
        for index in range(args.repeat):
            before = find_port(api.get('environments/ros2/endpoints'), 'image')
            baseline_rx, baseline_consume = before.get('rx_received', 0), before.get('rx_consumed', 0)
            result = {'round': index + 1, 'checks': {}, 'peak': {'rx_received': 0, 'rx_consumed': 0}}
            log_path = recorder.output / f'entry-{index + 1}.log'
            started, process = time.monotonic(), None
            try:
                with log_path.open('wb') as log:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               cwd=str(entry.parent), shell=False, start_new_session=True)
                    result['entry_pid'] = process.pid
                    while time.monotonic() - started < args.duration:
                        # Counters are endpoint facts, not part of the diagnostic
                        # service's projected port view.
                        row = find_port(api.get('environments/ros2/endpoints'), 'image')
                        for key in result['peak']:
                            result['peak'][key] = max(result['peak'][key], row.get(key) or 0)
                        if process.poll() is not None: break
                        time.sleep(POLL_INTERVAL_SEC)
                    result['elapsed_sec'] = time.monotonic() - started
                    try: result['exit_code'] = process.wait(timeout=5)
                    except subprocess.TimeoutExpired: result['exit_code'] = None
            finally:
                result['checks']['owned_process_group_cleaned'] = process is not None and _stop_entry(process)
                api.post('runtime/stop', {'reason': 'entry acceptance round complete'})
                _select_mode(api, 'normal')
            result['rx_delta'] = result['peak']['rx_received'] - baseline_rx
            result['consume_delta'] = result['peak']['rx_consumed'] - baseline_consume
            result['checks'].update(duration=result.get('elapsed_sec', 0) >= args.duration,
                                    new_rx=result['rx_delta'] > 0, new_consumption=result['consume_delta'] > 0,
                                    entry_exit_zero=result.get('exit_code') == 0,
                                    round_exit_idle_gate_closed=_idle_gate_closed(api))
            result['entry_log'] = log_path.name
            recorder.note('entry_round', **result)
            rounds.append(result)
    finally:
        cleanup.update(_restore(api, recorder, 'yolo', original_bindings, original_config, enabled['yolo']))
        _save_sse(sse, recorder)
    return {'rounds': rounds, 'cleanup': cleanup, 'sse_frames': len(sse.snapshot()), 'sse_error': sse.error}


def _restore(api: Api, recorder: Recorder, fixture: str, bindings, config, original_enabled=False):
    checks = {}
    steps = [
        ('runtime_stop', lambda: api.post('runtime/stop', {'reason': 'acceptance finished'})),
        ('mode_normal', lambda: _select_mode(api, 'normal')),
        ('bindings', lambda: api.post(f'plugins/{fixture}/ros2',
            {'expected_revision': api.get(f'plugins/{fixture}/ros2')['ros2']['revision'], 'bindings': bindings})),
        ('config', lambda: api.post('environments/ros2/config', {'config': config})),
        ('plugin_enabled_restored', lambda: api.post(f"plugins/{fixture}/{'enable' if original_enabled else 'disable'}", {})),
    ]
    for name, action in steps:
        try:
            action(); checks[name] = True
        except Exception as exc:
            checks[name] = False
            recorder.note('restore_error', step=name, error=exc.__class__.__name__)
        recorder.note('restore', step=name, ok=checks[name])
    try: checks['idle_gate_closed'] = _idle_gate_closed(api)
    except Exception: checks['idle_gate_closed'] = False
    return checks


def report_passes(report, *, repeat, duration):
    rows = report.get('rounds') or []
    return (len(rows) == repeat and all(isinstance(row.get('checks'), dict) and bool(row['checks'])
        and all(value is True for value in row['checks'].values()) and row.get('elapsed_sec', 0) >= duration
        and row.get('rx_delta', 0) > 0 and row.get('consume_delta', 0) > 0 for row in rows)
        and bool(report.get('cleanup')) and all(value is True for value in report['cleanup'].values())
        and not report.get('sse_error') and report.get('sse_frames', 0) > 0)


# --------------------------------------------------------------------------- #
# ROS publisher (imported lazily; never touched by --dry-run)
# --------------------------------------------------------------------------- #
class _Publisher:
    def __init__(self, node, publisher, executor, rclpy, context, payload, observer=None):
        self._node = node
        self._publisher = publisher
        self._executor = executor
        self._rclpy = rclpy
        self._context = context
        self._payload = payload
        self._observer = observer
        self.sent = 0

    def publish(self):
        from sensor_msgs.msg import CompressedImage
        message = CompressedImage()
        message.format = "jpeg"
        message.data = self._payload
        self._publisher.publish(message)
        self.sent += 1
        # Spin briefly so the observer (when requested) sees the frame too.
        if self._observer is not None:
            self._executor.spin_once(timeout_sec=0.01)

    def close(self):
        try:
            self._executor.shutdown(timeout_sec=2)
            self._node.destroy_node()
        finally:
            self._rclpy.shutdown(context=self._context)


def _ros_publisher(topic: str, *, with_observer: bool, domain: int) -> _Publisher:
    """Create the only publisher this script owns. No subscriber unless asked."""
    payload = base64.b64decode(TINY_JPEG_B64, validate=True)
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSPresetProfiles
    from sensor_msgs.msg import CompressedImage

    context = Context()
    rclpy.init(args=[], context=context, domain_id=domain)
    node = rclpy.create_node("b07_diag_publisher", context=context)
    publisher = node.create_publisher(
        CompressedImage, topic, QoSPresetProfiles.SENSOR_DATA.value)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    observer = None
    if with_observer:
        node.create_subscription(CompressedImage, topic, lambda _msg: None,
                                 QoSPresetProfiles.SENSOR_DATA.value)
        observer = True
    return _Publisher(node, publisher, executor, rclpy, context,
                      payload, observer)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    problems = validate_args(args)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        plan = build_plan(args)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n")
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return EXIT_PASS

    recorder = Recorder(args.output)
    report = {"scenario": args.scenario, "url": args.url, "domain": args.domain,
              "duration_sec": args.duration, "repeat": args.repeat,
              "with_observer": bool(args.with_observer), "platform": sys.platform,
              "result": "NOT_RUN", "reason": "", "rounds": []}
    try:
        if sys.platform != "linux":
            report["reason"] = ("native ROS acceptance requires Linux with ROS 2 Humble; "
                                f"{sys.platform} can only run --dry-run")
            recorder.write_report(report)
            print(f"NOT_RUN: {report['reason']}", file=sys.stderr)
            return EXIT_NOT_RUN
        token = load_token(args.token_file)
        api = Api(args.url, token)
        if args.scenario == "synthetic":
            report.update(run_synthetic(api, args, recorder))
        else:
            report.update(run_entry(api, args, recorder))
        rounds = report.get("rounds") or []
        passed = report_passes(report, repeat=args.repeat, duration=args.duration)
        report["result"] = "PASS" if passed else "FAIL"
        recorder.write_report(report)
        return EXIT_PASS if passed else EXIT_FAIL
    except AcceptanceError as exc:
        report["result"] = "FAIL"
        report["reason"] = str(exc)
        recorder.write_report(report)
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:
        report['result'] = 'FAIL'
        report['reason'] = exc.__class__.__name__
        recorder.write_report(report)
        return EXIT_FAIL
    except KeyboardInterrupt:
        report["result"] = "FAIL"
        report["reason"] = "interrupted"
        recorder.write_report(report)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
