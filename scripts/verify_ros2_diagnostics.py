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

# 160-byte 1x1 JPEG (SOI/APP0-JFIF/SOF/DHT/SOS/EOI). The probe counts messages
# and their size; it does not decode pixels. A real entry point that does decode
# still gets a structurally valid JPEG, not a random blob.
TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "h0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
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
                fields = {}
                for raw in response:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if fields:
                            with self._lock:
                                self.frames.append(fields)
                            fields = {}
                        continue
                    if line.startswith(":"):
                        continue
                    key, _, value = line.partition(":")
                    fields[key] = value.lstrip()
        except Exception as exc:  # noqa: BLE001 - recorded, never raised into the main flow
            self.error = f"{exc.__class__.__name__}: {exc}"

    def snapshot(self) -> list:
        with self._lock:
            return list(self.frames)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


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
    for row in (diagnostics.get("ports") or []):
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
def run_synthetic(api: Api, args, recorder: Recorder) -> dict:
    """Publish from this process only; never subscribe. Returns the round results."""
    rounds = []
    state = api.get("environments")
    environment = state.get("environment") or {}
    if environment.get("active_mode") != "normal":
        raise AcceptanceError("instance must start in normal mode")
    if (api.get("status") or {}).get("runtime_state") == "running":
        raise AcceptanceError("runtime must be idle before the run")

    fixture = "diag_image_probe"
    original_bindings = (api.get(f"plugins/{fixture}/ros2") or {}).get("ros2", {}).get("bindings", {})
    original_config = (state.get("config") or {}).get("ros2")
    recorder.note("snapshot", bindings=bool(original_bindings), config=bool(original_config))

    publisher = None
    sse = SseReader(api)
    try:
        api.post("environments/ros2/config",
                 {"config": {"domain_id": args.domain, "discovery_interval_sec": 0.2}})
        bindings = json.loads(json.dumps(original_bindings))
        topic = f"/ex_diag_acceptance_{os.getpid()}_{int(time.time())}"
        bindings.setdefault("image", {}).update(enabled=True, topic=topic)
        current = api.get(f"plugins/{fixture}/ros2")["ros2"]["revision"]
        api.post(f"plugins/{fixture}/ros2",
                 {"expected_revision": current, "bindings": bindings})
        api.post(f"plugins/{fixture}/enable", {})
        api.post("environments/select", {"mode": "ros2"})
        api.post("runtime/start", {})
        sse.start()
        recorder.note("started", domain=args.domain, topic=topic)

        publisher = _ros_publisher(topic, with_observer=args.with_observer)
        for index in range(args.repeat):
            rounds.append(_synthetic_round(api, args, recorder, publisher, index + 1))
    finally:
        sse.close()
        if publisher is not None:
            publisher.close()
        _restore(api, recorder, fixture, original_bindings, original_config)
    return {"rounds": rounds, "sse_frames": len(sse.snapshot()), "sse_error": sse.error}


def _synthetic_round(api: Api, args, recorder: Recorder, publisher, index: int) -> dict:
    """One round: silence past grace, frames, silence past stale, frames again."""
    result = {"round": index, "checks": {}, "started_at": utc_now()}
    grace = _policy_seconds(api, "image", "startup_grace_sec", 2.0)
    stale = _policy_seconds(api, "image", "stale_after_sec", 2.0)

    ok, _ = wait_for(
        lambda: port_health(api.get("environments/diagnostics"), "image") == "no_data",
        timeout_s=grace + 10.0)
    result["checks"]["no_data_with_peer"] = ok
    recorder.note("round", round=index, phase="no_data", ok=ok)

    for _ in range(FRAMES_PER_ROUND):
        publisher.publish()
        time.sleep(FRAME_INTERVAL_SEC)
    ok, health = wait_for(
        lambda: port_health(api.get("environments/diagnostics"), "image") == "receiving",
        timeout_s=10.0)
    result["checks"]["receiving"] = ok
    result["health_after_frames"] = health
    recorder.note("round", round=index, phase="receiving", ok=ok, health=health)

    time.sleep(stale + 2.0)
    ok, health = wait_for(
        lambda: port_health(api.get("environments/diagnostics"), "image") == "stale",
        timeout_s=10.0)
    result["checks"]["stale"] = ok
    result["health_after_silence"] = health
    recorder.note("round", round=index, phase="stale", ok=ok, health=health)

    for _ in range(FRAMES_PER_ROUND):
        publisher.publish()
        time.sleep(FRAME_INTERVAL_SEC)
    ok, health = wait_for(
        lambda: port_health(api.get("environments/diagnostics"), "image") == "receiving",
        timeout_s=10.0)
    result["checks"]["recovered"] = ok
    result["health_after_resume"] = health
    result["finished_at"] = utc_now()
    recorder.note("round", round=index, phase="recovered", ok=ok, health=health)
    return result


def _policy_seconds(api: Api, port_id: str, key: str, fallback: float) -> float:
    try:
        policy = find_port(api.get("environments/diagnostics"), port_id).get("diagnostics") or {}
        value = (policy.get("policy") or {}).get(key, policy.get(key))
        return float(value) if value is not None else fallback
    except (AcceptanceError, TypeError, ValueError):
        return fallback


def run_entry(api: Api, args, recorder: Recorder) -> dict:
    """Start the site's real entry point; it owns camera and inference."""
    import subprocess

    entry = Path(args.entry)
    command = [str(entry), "--duration", str(args.duration)]
    recorder.note("entry", command=[entry.name] + command[1:])
    log_path = recorder.output / "entry.log"
    rounds = []
    process = None
    sse = SseReader(api)
    try:
        sse.start()
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       cwd=str(entry.parent), shell=False)
            for index in range(args.repeat):
                deadline = time.monotonic() + args.duration
                peak = {"rx_received": 0, "rx_consumed": 0}
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    row = find_port(api.get("environments/diagnostics"), "image")
                    peak["rx_received"] = max(peak["rx_received"], row.get("rx_received") or 0)
                    peak["rx_consumed"] = max(peak["rx_consumed"], row.get("rx_consumed") or 0)
                    time.sleep(1.0)
                rounds.append({"round": index + 1, "peak": peak,
                               "exit_code": process.poll()})
                recorder.note("entry_round", round=index + 1, **peak)
                if process.poll() is not None:
                    break
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                process.kill()
        sse.close()
    return {"rounds": rounds, "entry_log": log_path.name, "sse_frames": len(sse.snapshot())}


def _restore(api: Api, recorder: Recorder, fixture: str, bindings, config):
    """Every restore step is attempted independently; failures are recorded, not raised."""
    steps = [
        ("runtime_stop", lambda: api.post("runtime/stop", {"reason": "acceptance finished"})),
        ("mode_normal", lambda: api.post("environments/select", {"mode": "normal"})),
        ("bindings", lambda: api.post(
            f"plugins/{fixture}/ros2",
            {"expected_revision": api.get(f"plugins/{fixture}/ros2")["ros2"]["revision"],
             "bindings": bindings})),
        ("config", lambda: api.post("environments/ros2/config", {"config": config})),
        ("plugin_disable", lambda: api.post(f"plugins/{fixture}/disable", {})),
    ]
    for name, action in steps:
        try:
            action()
            recorder.note("restore", step=name, ok=True)
        except Exception as exc:  # noqa: BLE001 - one failure must not skip the rest
            recorder.note("restore", step=name, ok=False,
                          error=f"{exc.__class__.__name__}")


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

    def publish(self):
        from sensor_msgs.msg import CompressedImage
        message = CompressedImage()
        message.format = "jpeg"
        message.data = self._payload
        self._publisher.publish(message)
        # Spin briefly so the observer (when requested) sees the frame too.
        if self._observer is not None:
            self._executor.spin_once(timeout_sec=0.01)

    def close(self):
        try:
            self._executor.shutdown(timeout_sec=2)
            self._node.destroy_node()
        finally:
            self._rclpy.shutdown(context=self._context)


def _ros_publisher(topic: str, *, with_observer: bool) -> _Publisher:
    """Create the only publisher this script owns. No subscriber unless asked."""
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSPresetProfiles
    from sensor_msgs.msg import CompressedImage

    context = Context()
    rclpy.init(context=context)
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
                      base64.b64decode(TINY_JPEG_B64), observer)


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
        passed = bool(rounds) and all(
            all((row.get("checks") or {}).values()) for row in rounds if "checks" in row)
        report["result"] = "PASS" if passed else "FAIL"
        recorder.write_report(report)
        return EXIT_PASS if passed else EXIT_FAIL
    except AcceptanceError as exc:
        report["result"] = "FAIL"
        report["reason"] = str(exc)
        recorder.write_report(report)
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        report["result"] = "FAIL"
        report["reason"] = "interrupted"
        recorder.write_report(report)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
