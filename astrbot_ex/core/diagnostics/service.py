"""Diagnostics coordination service (B04).

Turns port/preflight facts into a cached issue snapshot plus low-frequency
``ros_diagnostic`` events. The service never touches ROS, the OS or the
environment directly: everything comes from the two injected collectors, and
all pure judgement lives in B00's rules (``astrbot_ex.core.diagnostics.rules``).

Contract references (this task set):

* C01 section 5 - constructor, ``start/snapshot/request_refresh/invalidate/close``,
  the snapshot fields, the issue fields and the ``ros_diagnostic`` payload.
* C04 - observation windows, dedup key, the RX liveness family, the 60 s
  reminder budget, two-consecutive-healthy recovery, withdrawal vs recovery,
  and the capacity limits.

Three rules drive most of the design:

1. **No lock is held while calling out.** The collector, the evaluator and the
   event bus are all foreign code. A subscriber that calls ``snapshot()`` from
   inside ``emit()`` must not deadlock, so events are emitted after the state
   lock has been released.
2. **Missing evidence is never health.** A failed or incomplete collection keeps
   the previous issues, marks the snapshot non-current and raises a bounded
   ``ROS_DIAGNOSTICS_PARTIAL`` warning. It never counts as a healthy sample, so
   it cannot produce a recovery event.
3. **Everything is bounded.** Active issues, port samples, evidence and the
   reminder/streak maps have hard ceilings, and keys are pruned when their scope
   disappears. A long rebinding session cannot grow memory forever.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict

SCHEMA_VERSION = 1

# C04 capacity and timing constants. Module level so tests and the handoff
# report can quote the same numbers the code uses.
MAX_ACTIVE_ISSUES = 512
MAX_PORT_SAMPLES = 1024
MAX_EVIDENCE_KEYS = 24
MAX_EVIDENCE_TEXT = 512
MAX_SCOPE_TEXT = 128
REMIND_INTERVAL_SEC = 60.0
PREFLIGHT_TTL_SEC = 30.0
RECOVERY_STREAK = 2

RX_LIVENESS_FAMILY = "rx_liveness"
RX_LIVENESS_CODES = frozenset({
    "ROS_RX_NO_PEER",
    "ROS_RX_NO_DATA_WITH_PEER",
    "ROS_RX_STALE",
})

RECOVERED_CODE = "ROS_DIAGNOSTIC_RECOVERED"
PARTIAL_CODE = "ROS_DIAGNOSTICS_PARTIAL"

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}

# Issue scope field order. C01 fixes the names; ``issue_key`` is their
# normalised concatenation plus the code, so the same fault in the same
# observation window maps to one issue and different ports never share one.
SCOPE_FIELDS = (
    "session_id",
    "environment_generation",
    "endpoint_id",
    "owner_generation",
    "binding_generation",
    "monitoring_epoch",
)

REFRESH_IDLE = "idle"
REFRESH_PENDING = "pending"
REFRESH_RUNNING = "running"
REFRESH_COMPLETED = "completed"
REFRESH_FAILED = "failed"
REFRESH_SUPERSEDED = "superseded"


class DiagnosticsServiceError(RuntimeError):
    """Raised for misuse of the service rather than for a diagnostic condition."""


class DiagnosticsServiceTimeout(DiagnosticsServiceError):
    """``close()`` could not join the sampling thread inside its budget."""


def _bounded_text(value, limit=MAX_EVIDENCE_TEXT):
    """Return a JSON-safe, length-bounded value. Never raises."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    # Control characters would corrupt the log view; the UI strips them too,
    # but evidence should not carry them in the first place.
    text = "".join(ch if ch >= " " or ch in "\n\t" else " " for ch in text)
    if len(text) > limit:
        return text[: limit - 1] + "\u2026"
    return text


def _bounded_evidence(evidence):
    """Whitelist-shaped, size-limited evidence mapping."""
    if not isinstance(evidence, dict):
        return {} if evidence is None else {"value": _bounded_text(evidence)}
    bounded = {}
    for index, (key, value) in enumerate(evidence.items()):
        if index >= MAX_EVIDENCE_KEYS:
            bounded["truncated"] = True
            break
        if isinstance(value, (list, tuple)):
            bounded[str(key)] = [_bounded_text(item) for item in list(value)[:MAX_EVIDENCE_KEYS]]
        elif isinstance(value, dict):
            # One nesting level only: enough for {state: ...} style evidence and
            # bounded, so a caller cannot smuggle a whole report in here.
            bounded[str(key)] = {
                str(inner_key): _bounded_text(inner_value)
                for inner_key, inner_value in list(value.items())[:MAX_EVIDENCE_KEYS]
            }
        else:
            bounded[str(key)] = _bounded_text(value)
    return bounded


def _normalize_scope(scope):
    normalized = {}
    for name in SCOPE_FIELDS:
        value = None if not isinstance(scope, dict) else scope.get(name)
        if value is None:
            normalized[name] = None
        elif isinstance(value, bool):
            normalized[name] = int(value)
        elif isinstance(value, (int, float)):
            normalized[name] = int(value) if float(value).is_integer() else float(value)
        else:
            normalized[name] = _bounded_text(value, MAX_SCOPE_TEXT)
    # Display-only extras are allowed but kept out of the identity key.
    for extra in ("plugin_id", "port_id", "topic"):
        value = None if not isinstance(scope, dict) else scope.get(extra)
        if value is not None:
            normalized[extra] = _bounded_text(value, MAX_SCOPE_TEXT)
    return normalized


def issue_key(scope, code):
    """Stable dedup key: scope identity fields plus the code (C04)."""
    normalized = _normalize_scope(scope)
    parts = ["" if normalized.get(name) is None else str(normalized[name])
             for name in SCOPE_FIELDS]
    parts.append(str(code))
    return "|".join(parts)


def scope_key(scope):
    """Identity of the scope without the code, used for family bookkeeping."""
    normalized = _normalize_scope(scope)
    return "|".join("" if normalized.get(name) is None else str(normalized[name])
                    for name in SCOPE_FIELDS)


def _family_of(code):
    return RX_LIVENESS_FAMILY if code in RX_LIVENESS_CODES else str(code)


def _severity(value, default="warning"):
    text = str(value or "").strip().lower()
    if text == "warn":
        text = "warning"
    if text == "fatal":
        text = "critical"
    return text if text in SEVERITY_RANK else default


class DiagnosticsService:
    """Periodic diagnostics coordinator with a cached, read-only snapshot.

    ``collector`` returns the environment identity, the runtime flag, the port
    samples and a completeness flag. ``preflight_collector`` returns B01's
    report. ``event_bus`` only needs an ``emit(type, message, **data)`` method.
    """

    def __init__(
        self,
        collector,
        preflight_collector,
        event_bus,
        *,
        clock_ns=time.monotonic_ns,
        tick_sec=1.0,
        wall_clock=time.time,
        evaluator=None,
        preflight_evaluator=None,
        preflight_ttl_sec=PREFLIGHT_TTL_SEC,
        remind_interval_sec=REMIND_INTERVAL_SEC,
        max_active_issues=MAX_ACTIVE_ISSUES,
        max_port_samples=MAX_PORT_SAMPLES,
    ) -> None:
        self._collector = collector
        self._preflight_collector = preflight_collector
        self._bus = event_bus
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock
        self._tick_sec = max(0.01, float(tick_sec))
        self._evaluator = evaluator
        self._preflight_evaluator = preflight_evaluator
        self._preflight_ttl_ns = int(max(0.0, float(preflight_ttl_sec)) * 1_000_000_000)
        self._remind_ns = int(max(0.0, float(remind_interval_sec)) * 1_000_000_000)
        self._max_issues = max(1, int(max_active_issues))
        self._max_ports = max(1, int(max_port_samples))

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._started = False
        self._closed = False

        self._issues = {}
        self._healthy_streak = {}
        # Bounded FIFO so a rebinding storm cannot grow the reminder map.
        self._family_alerted_at = OrderedDict()
        self._family_limit = self._max_issues * 4

        self._environment = {}
        self._current = False
        self._partial = True
        self._partial_reason = "not collected yet"
        self._collected_at_ns = None
        self._collected_at_wall = None
        self._runtime_running = False
        self._ports = []

        self._preflight = None
        self._preflight_ns = None
        self._preflight_wall = None
        self._preflight_error = None
        self._preflight_identity = None

        self._refresh = {"id": None, "state": REFRESH_IDLE, "error_code": None}
        self._refresh_seq = 0
        # Identity the outstanding refresh was requested against. If the
        # environment moves before the (possibly slow) probe returns, the result
        # belongs to a generation nobody is looking at any more.
        self._refresh_identity = None

        self._snapshot_cache = self._build_snapshot()
        self._emit_count = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Start the sampling thread. Idempotent; never starts two threads."""
        with self._lock:
            if self._closed:
                raise DiagnosticsServiceError("service is closed; create a new instance")
            if self._started:
                return {"started": True, "already_running": True}
            self._started = True
            thread = threading.Thread(target=self._run_loop, name="diagnostics-service",
                                      daemon=True)
            self._thread = thread
        thread.start()
        # First collection is synchronous so a GET right after start() does not
        # report "unknown" for a whole tick. Failures are captured, not raised.
        self._safe_cycle(refresh=False)
        return {"started": True, "already_running": False}

    def close(self, timeout=2.0):
        """Stop the thread and refuse further events. Idempotent."""
        with self._lock:
            already = self._closed
            self._closed = True
            thread = self._thread
            self._thread = None
        self._wake.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(float(timeout))
            if thread.is_alive():
                # Never claim a clean shutdown while the sampler still runs.
                raise DiagnosticsServiceTimeout(
                    "diagnostics thread did not stop within %.1fs; it is still running"
                    % float(timeout))
        return {"closed": True, "already_closed": already,
                "thread_alive": bool(thread is not None and thread.is_alive())}

    @property
    def closed(self):
        with self._lock:
            return self._closed

    # -- public contract ---------------------------------------------------
    def snapshot(self):
        """Deep copy of the cached snapshot. Never collects, never blocks on I/O."""
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot_cache)
            # Ages are derived at read time: a cache frozen at collection time
            # would report "0 s old" forever if sampling stalled.
            now_ns = int(self._clock_ns())
            if self._collected_at_ns is not None:
                snapshot["collection_age_sec"] = max(
                    0.0, (now_ns - self._collected_at_ns) / 1_000_000_000.0)
            preflight = snapshot.get("preflight")
            if isinstance(preflight, dict) and self._preflight_ns is not None:
                age = max(0.0, (now_ns - self._preflight_ns) / 1_000_000_000.0)
                preflight["age_sec"] = age
                preflight["current"] = age * 1_000_000_000 <= self._preflight_ttl_ns
            return snapshot

    def request_refresh(self):
        """Ask for one merged refresh. Returns the current refresh state."""
        with self._lock:
            if self._closed:
                return {"refresh_id": None, "state": REFRESH_FAILED,
                        "error_code": "E_CLOSED", "merged": False}
            if self._refresh["state"] in (REFRESH_PENDING, REFRESH_RUNNING):
                # Storm collapsing: one outstanding refresh per service, always
                # the same id, never an extra thread.
                return {"refresh_id": self._refresh["id"], "state": self._refresh["state"],
                        "error_code": None, "merged": True}
            self._refresh_seq += 1
            refresh_id = "refresh-%d" % self._refresh_seq
            self._refresh = {"id": refresh_id, "state": REFRESH_PENDING, "error_code": None}
            self._refresh_identity = self._identity_text(self._environment)
            self._snapshot_cache["refresh"] = dict(self._refresh)
        self._wake.set()
        return {"refresh_id": refresh_id, "state": REFRESH_PENDING, "error_code": None,
                "merged": False}

    def invalidate(self, identity=None):
        """Drop state belonging to a superseded identity.

        Used when the environment switches, restores or unloads. Active issues
        are withdrawn (they are not recovered), the preflight cache is dropped
        and an in-flight refresh is marked superseded.
        """
        with self._lock:
            if self._closed:
                return {"invalidated": False, "reason": "closed"}
            withdrawn = self._withdraw_all("scope_invalidated")
            self._preflight = None
            self._preflight_ns = None
            self._preflight_identity = None
            superseded = None
            if self._refresh["state"] in (REFRESH_PENDING, REFRESH_RUNNING):
                superseded = self._refresh["id"]
                self._refresh = {"id": superseded, "state": REFRESH_SUPERSEDED,
                                 "error_code": "E_SUPERSEDED"}
            if identity:
                self._environment = self._normalize_environment(identity)
            self._current = False
            self._snapshot_cache = self._build_snapshot()
        for event in withdrawn:
            self._emit(event)
        return {"invalidated": True, "withdrawn": len(withdrawn),
                "superseded_refresh": superseded}

    # -- sampling thread ---------------------------------------------------
    def _run_loop(self):
        while True:
            with self._lock:
                if self._closed:
                    return
            self._wake.wait(self._tick_sec)
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
                is_refresh = self._refresh["state"] == REFRESH_PENDING
                if is_refresh:
                    self._refresh["state"] = REFRESH_RUNNING
                    self._snapshot_cache["refresh"] = dict(self._refresh)
            self._safe_cycle(refresh=is_refresh)

    def _safe_cycle(self, *, refresh):
        try:
            self._run_cycle()
        except Exception as exc:  # a cycle bug must not kill the thread silently
            with self._lock:
                self._partial = True
                self._partial_reason = "cycle failed: %s" % (type(exc).__name__,)
                self._snapshot_cache = self._build_snapshot()
            if refresh:
                self._finish_refresh(REFRESH_FAILED, "E_CYCLE_FAILED")
            return False
        if refresh:
            with self._lock:
                moved = (self._refresh_identity is not None
                         and self._identity_text(self._environment) != self._refresh_identity)
            if moved:
                # The probe answered for a generation that is already gone.
                self._finish_refresh(REFRESH_SUPERSEDED, "E_SUPERSEDED")
            else:
                self._finish_refresh(REFRESH_COMPLETED, None)
        return True

    def _finish_refresh(self, state, error_code):
        with self._lock:
            if self._refresh["state"] in (REFRESH_PENDING, REFRESH_RUNNING):
                self._refresh["state"] = state
                self._refresh["error_code"] = error_code
            self._snapshot_cache["refresh"] = dict(self._refresh)

    # -- one sampling cycle ------------------------------------------------
    def _run_cycle(self):
        now_ns = int(self._clock_ns())
        wall = float(self._wall_clock())

        collection = self._collect()
        raw_environment = collection.get("environment")
        # A failed collection carries no identity. Keeping the previous one is
        # what makes "partial" meaningful: an empty environment would look like
        # a switch to a scope nobody asked about and would withdraw every issue.
        environment = (self._normalize_environment(raw_environment) if raw_environment
                       else dict(self._environment))
        identity_changed = bool(raw_environment) and self._identity_changed(environment)

        if identity_changed:
            # Old issues belong to a scope that no longer exists: withdraw, do
            # not recover. Recovery would claim the fault cleared.
            stale = self._withdraw_all("scope_changed")
            for event in stale:
                self._emit(event)
            self._preflight = None
            self._preflight_ns = None
            self._preflight_identity = None

        ports = collection.get("ports") or []
        complete = bool(collection.get("complete", True))
        partial_reason = collection.get("partial_reason")
        truncated_ports = False
        if len(ports) > self._max_ports:
            ports = ports[: self._max_ports]
            truncated_ports = True

        rules_error = None
        try:
            port_health, candidates, port_scopes = self._evaluate_ports(ports, now_ns)
        except DiagnosticsServiceError as exc:
            # B00 rules are not installed on this branch: stay alive, report the
            # gap as partial evidence instead of pretending ports are healthy.
            rules_error = str(exc)
            port_health, candidates, port_scopes = [], [], {}
            complete = False
            partial_reason = "rules unavailable: %s" % (exc,)
        if not complete:
            candidates = []  # no evidence: keep issues, never recover on this cycle

        preflight_report, preflight_candidates = self._maybe_collect_preflight(
            environment, now_ns, wall,
            force=self._refresh["state"] == REFRESH_RUNNING,
        )

        active_mode = environment.get("active_mode")
        ros_active = active_mode == "ros2"
        # A failed collection reports None: keep the last known flag instead of
        # silently claiming the runtime stopped.
        reported_running = collection.get("runtime_running")
        runtime_running = bool(self._runtime_running if reported_running is None
                               else reported_running)

        if ros_active:
            candidates = list(candidates) + list(preflight_candidates)
        if not runtime_running:
            # Monitoring is off while the runtime is not running: port issues are
            # withdrawn below, so do not re-create them from this sample.
            candidates = [item for item in candidates
                          if item["scope"].get("endpoint_id") is None]
        # A normal environment has no ROS dependency: its preflight report stays
        # visible but never becomes an active issue (C04 preflight rules).

        events = []
        with self._lock:
            if raw_environment:
                self._environment = environment
            self._runtime_running = runtime_running
            self._current = True
            self._collected_at_ns = now_ns
            self._collected_at_wall = wall
            self._ports = port_health
            partial = (not complete) or truncated_ports
            partial_candidates = []
            if partial:
                if partial_reason:
                    reason = partial_reason
                elif truncated_ports:
                    reason = "port sample limit %d reached" % self._max_ports
                else:
                    reason = "collector incomplete"
                self._partial_reason = _bounded_text(reason)
                if ros_active:
                    partial_candidates = [self._partial_candidate(environment, reason)]
            else:
                self._partial_reason = None
            self._partial = partial

            # The partial warning is evidence about the collection itself, so it
            # is raised even when the rest of the sample is unusable.
            events.extend(self._apply_candidates(partial_candidates, now_ns, wall))
            if complete:
                events.extend(self._apply_candidates(candidates, now_ns, wall))
                events.extend(self._withdraw_inactive(port_scopes, runtime_running, wall))
                events.extend(self._recover_missing(candidates, wall))
            else:
                # Keep issues, mark them non-current. No streak progress.
                for issue in self._issues.values():
                    issue["current"] = False
            self._snapshot_cache = self._build_snapshot()

        for event in events:
            self._emit(event)
        return {"partial": self._partial, "rules_error": rules_error}

    def _collect(self):
        try:
            collection = self._collector()
        except Exception as exc:
            return {"environment": {}, "runtime_running": None, "ports": [],
                    "complete": False,
                    "partial_reason": "collector failed: %s" % (type(exc).__name__,)}
        if not isinstance(collection, dict):
            return {"environment": {}, "runtime_running": None, "ports": [],
                    "complete": False,
                    "partial_reason": "collector returned %s" % (type(collection).__name__,)}
        return collection

    def _evaluate_ports(self, ports, now_ns):
        evaluator = self._resolver()
        health, candidates = [], []
        # scope_key -> whether this port is currently expected to be monitored
        scopes = {}
        for raw in ports:
            if not isinstance(raw, dict):
                continue
            sample = dict(raw)
            try:
                result = evaluator(sample, now_ns=now_ns) or {}
            except Exception:
                # One bad port must not abort the cycle; it becomes unknown.
                result = {"data_health": "unknown",
                          "diagnostics": {"reason_code": "E_RULE_FAILED"},
                          "issues": []}
            if not isinstance(result, dict):
                result = {"data_health": "unknown", "diagnostics": {}, "issues": []}
            health.append(self._port_view(sample, result))
            scopes[scope_key(sample)] = result.get("data_health") not in (
                "paused", "disabled", "not_monitored")
            for candidate in result.get("issues") or []:
                normalized = self._normalize_candidate(candidate, sample)
                if normalized is not None:
                    candidates.append(normalized)
        return health, candidates, scopes

    @staticmethod
    def _port_view(sample, result):
        diagnostics = result.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        return {
            "endpoint_id": sample.get("endpoint_id"),
            "plugin_id": sample.get("plugin_id"),
            "port_id": sample.get("port_id"),
            "topic": sample.get("topic"),
            "direction": sample.get("direction"),
            "state": sample.get("state"),
            "data_health": result.get("data_health") or "unknown",
            "diagnostics": copy.deepcopy(diagnostics),
        }

    def _normalize_candidate(self, candidate, sample):
        if not isinstance(candidate, dict):
            return None
        code = candidate.get("code")
        if not isinstance(code, str) or not code.strip():
            return None
        scope = candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {}
        merged_scope = dict(scope)
        # Port candidates may omit the identity fields; fill them from the sample
        # so two ports can never collapse into one issue key.
        for name in SCOPE_FIELDS:
            if merged_scope.get(name) is None and sample.get(name) is not None:
                merged_scope[name] = sample.get(name)
        for name in ("plugin_id", "port_id", "topic"):
            if merged_scope.get(name) is None and sample.get(name) is not None:
                merged_scope[name] = sample.get(name)
        normalized_scope = _normalize_scope(merged_scope)
        if normalized_scope.get("session_id") is None and normalized_scope.get("endpoint_id") is None:
            # C01 section 9: an incomplete scope must not produce a publishable
            # candidate; the manager fills session_id before the service sees it.
            return None
        return {
            "code": _bounded_text(code, MAX_SCOPE_TEXT),
            "severity": _severity(candidate.get("severity")),
            "source": _bounded_text(candidate.get("source") or "ros.port", MAX_SCOPE_TEXT),
            "scope": normalized_scope,
            "message": _bounded_text(candidate.get("message") or code),
            "evidence": _bounded_evidence(candidate.get("evidence")),
            "remediation": self._bounded_remediation(candidate.get("remediation")),
        }

    @staticmethod
    def _bounded_remediation(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [_bounded_text(value)]
        if isinstance(value, (list, tuple)):
            return [_bounded_text(item) for item in list(value)[:MAX_EVIDENCE_KEYS]]
        return [_bounded_text(value)]

    def _partial_candidate(self, environment, reason):
        return {
            "code": PARTIAL_CODE,
            "severity": "warning",
            "source": "ros.diagnostics",
            "scope": {
                "session_id": environment.get("session_id"),
                "environment_generation": environment.get("generation"),
            },
            "message": "diagnostics collection is incomplete: %s" % (reason,),
            "evidence": {"reason": reason},
            "remediation": ["检查 EX 进程日志与 ROS 运行状态；证据恢复前不要据此判断端口健康"],
        }

    def _maybe_collect_preflight(self, environment, now_ns, wall, *, force):
        identity = self._identity_text(environment)
        age_ns = None if self._preflight_ns is None else now_ns - self._preflight_ns
        due = (
            force
            or self._preflight is None
            or identity != self._preflight_identity
            or age_ns is None
            or age_ns >= self._preflight_ttl_ns
        )
        if not due:
            return self._preflight, self._preflight_candidates(self._preflight, environment)
        try:
            report = self._preflight_collector()
        except Exception as exc:
            with self._lock:
                self._preflight_error = "preflight failed: %s" % (type(exc).__name__,)
            return self._preflight, []
        if not isinstance(report, dict):
            with self._lock:
                self._preflight_error = "preflight returned %s" % (type(report).__name__,)
            return self._preflight, []
        with self._lock:
            self._preflight = report
            self._preflight_ns = now_ns
            self._preflight_wall = wall
            self._preflight_identity = identity
            self._preflight_error = None
        return report, self._preflight_candidates(report, environment)

    def _preflight_candidates(self, report, environment):
        if not report:
            return []
        try:
            evaluator = self._preflight_resolver()
            candidates = evaluator(report) or []
        except Exception:
            return []
        out = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            scope = dict(item.get("scope") or {})
            scope.setdefault("session_id", environment.get("session_id"))
            scope.setdefault("environment_generation", environment.get("generation"))
            item["scope"] = scope
            normalized = self._normalize_candidate(item, {})
            if normalized is not None:
                out.append(normalized)
        return out

    # -- issue state machine ----------------------------------------------
    def _apply_candidates(self, candidates, now_ns, wall):
        """Create or refresh issues. Returns the events to emit after unlocking."""
        events = []
        seen_keys = set()
        by_scope = {}
        for candidate in candidates:
            key = issue_key(candidate["scope"], candidate["code"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            by_scope.setdefault(scope_key(candidate["scope"]), []).append(candidate)

        for scope_id, group in by_scope.items():
            family = _family_of(group[0]["code"])
            incoming_codes = {item["code"] for item in group}
            # C04: one active RX issue per scope. Reclassification withdraws the
            # old code instead of reporting a recovery.
            for key, issue in list(self._issues.items()):
                if issue["scope_key"] != scope_id:
                    continue
                if issue["family"] == family and issue["code"] not in incoming_codes:
                    self._issues.pop(key, None)
                    self._healthy_streak.pop(key, None)
                    events.append(self._issue_event(issue, "withdrawn", wall,
                                                    reason="reclassified"))
            for candidate in group:
                key = issue_key(candidate["scope"], candidate["code"])
                issue = self._issues.get(key)
                if issue is None:
                    if len(self._issues) >= self._max_issues:
                        self._partial = True
                        self._partial_reason = self._partial_reason or (
                            "active issue limit %d reached" % self._max_issues)
                        continue
                    issue = {
                        "issue_key": key,
                        "code": candidate["code"],
                        "severity": candidate["severity"],
                        "source": candidate["source"],
                        "scope": candidate["scope"],
                        "scope_key": scope_id,
                        "family": family,
                        "message": candidate["message"],
                        "evidence": candidate["evidence"],
                        "remediation": candidate["remediation"],
                        "first_seen_at": wall,
                        "last_seen_at": wall,
                        "occurrences": 1,
                        "current": True,
                    }
                    self._issues[key] = issue
                    self._healthy_streak.pop(key, None)
                    if self._family_budget_left(scope_id, family, now_ns):
                        self._mark_family_alert(scope_id, family, now_ns)
                        events.append(self._issue_event(issue, "active", wall))
                else:
                    issue.update({
                        "severity": candidate["severity"],
                        "message": candidate["message"],
                        "evidence": candidate["evidence"],
                        "remediation": candidate["remediation"],
                        "last_seen_at": wall,
                        "occurrences": issue["occurrences"] + 1,
                        "current": True,
                    })
                    self._healthy_streak.pop(key, None)
                    if self._family_budget_left(scope_id, family, now_ns):
                        self._mark_family_alert(scope_id, family, now_ns)
                        events.append(self._issue_event(issue, "active", wall, reminder=True))
        return events

    def _family_budget_left(self, scope_id, family, now_ns):
        last = self._family_alerted_at.get(scope_id + "|" + family)
        return last is None or now_ns - last >= self._remind_ns

    def _mark_family_alert(self, scope_id, family, now_ns):
        key = scope_id + "|" + family
        self._family_alerted_at[key] = now_ns
        self._family_alerted_at.move_to_end(key)
        while len(self._family_alerted_at) > self._family_limit:
            self._family_alerted_at.popitem(last=False)

    def _withdraw_inactive(self, port_scopes, runtime_running, wall):
        """Withdraw port issues whose monitoring ended (C04).

        Stopping the runtime, disabling a port or unloading its plugin is the end
        of observation, not a device recovery. A port that vanished from the
        sample is treated the same way, otherwise it would be "recovered" by
        absence after two cycles.
        """
        events = []
        for key, issue in list(self._issues.items()):
            if issue["scope"].get("endpoint_id") is None:
                continue
            monitored = port_scopes.get(issue["scope_key"])
            if runtime_running and monitored is True:
                continue
            if monitored is None:
                reason = "port_gone"
            elif monitored is False:
                reason = "monitoring_inactive"
            else:
                reason = "runtime_not_running"
            self._issues.pop(key, None)
            self._healthy_streak.pop(key, None)
            events.append(self._issue_event(issue, "withdrawn", wall, reason=reason))
        return events

    def _recover_missing(self, candidates, wall):
        """Two consecutive healthy samples recover an issue (C04)."""
        events = []
        present = {issue_key(candidate["scope"], candidate["code"]) for candidate in candidates}
        present_families = {(scope_key(candidate["scope"]), _family_of(candidate["code"]))
                            for candidate in candidates}
        for key, issue in list(self._issues.items()):
            if key in present:
                continue
            if (issue["scope_key"], issue["family"]) in present_families:
                # Same family still active under another code: handled above.
                continue
            streak = self._healthy_streak.get(key, 0) + 1
            if streak >= RECOVERY_STREAK:
                self._issues.pop(key, None)
                self._healthy_streak.pop(key, None)
                events.append(self._issue_event(issue, "recovered", wall))
            else:
                self._healthy_streak[key] = streak
        return events

    def _withdraw_all(self, reason):
        events = []
        wall = float(self._wall_clock())
        for key, issue in list(self._issues.items()):
            self._issues.pop(key, None)
            events.append(self._issue_event(issue, "withdrawn", wall, reason=reason))
        self._family_alerted_at.clear()
        self._healthy_streak.clear()
        return events

    @staticmethod
    def _issue_event(issue, state, wall, *, reason=None, reminder=False):
        if state == "recovered":
            payload = {
                "severity": "info",
                "source": issue["source"],
                "code": RECOVERED_CODE,
                "recovered_code": issue["code"],
                "issue_key": issue["issue_key"],
                "scope": copy.deepcopy(issue["scope"]),
                "state": "recovered",
                "evidence": copy.deepcopy(issue["evidence"]),
                "remediation": [],
            }
            message = "恢复：%s" % (issue["code"],)
        elif state == "withdrawn":
            payload = {
                "severity": "info",
                "source": issue["source"],
                "code": issue["code"],
                "issue_key": issue["issue_key"],
                "scope": copy.deepcopy(issue["scope"]),
                "state": "withdrawn",
                "evidence": copy.deepcopy(issue["evidence"]),
                "remediation": [],
                "reason": reason or "withdrawn",
            }
            message = "监测已结束：%s（不是设备恢复）" % (issue["code"],)
        else:
            payload = {
                "severity": issue["severity"],
                "source": issue["source"],
                "code": issue["code"],
                "issue_key": issue["issue_key"],
                "scope": copy.deepcopy(issue["scope"]),
                "state": "active",
                "evidence": copy.deepcopy(issue["evidence"]),
                "remediation": copy.deepcopy(issue["remediation"]),
                "occurrences": issue["occurrences"],
            }
            message = issue["message"]
            if reminder:
                payload["reminder"] = True
        payload["detected_at"] = wall
        return {"type": "ros_diagnostic", "message": message, "data": payload}

    def _emit(self, event):
        with self._lock:
            if self._closed:
                return
            self._emit_count += 1
        # No service lock held here: a subscriber may call snapshot().
        self._bus.emit(event["type"], event["message"], **event["data"])

    # -- snapshot ----------------------------------------------------------
    def _build_snapshot(self):
        now_ns = int(self._clock_ns())
        age = None
        if self._collected_at_ns is not None:
            age = max(0.0, (now_ns - self._collected_at_ns) / 1_000_000_000.0)
        issues = sorted(self._issues.values(),
                        key=lambda item: (item["first_seen_at"], item["issue_key"]))
        active = []
        for issue in issues:
            active.append({
                "issue_key": issue["issue_key"],
                "code": issue["code"],
                "severity": issue["severity"],
                "source": issue["source"],
                "scope": copy.deepcopy(issue["scope"]),
                "message": issue["message"],
                "evidence": copy.deepcopy(issue["evidence"]),
                "remediation": copy.deepcopy(issue["remediation"]),
                "first_seen_at": issue["first_seen_at"],
                "last_seen_at": issue["last_seen_at"],
                "occurrences": issue["occurrences"],
                "current": issue.get("current", True),
            })
        preflight = None
        if self._preflight is not None:
            preflight = copy.deepcopy(self._preflight)
            preflight["collected_at"] = self._preflight_wall
            preflight["age_sec"] = None if self._preflight_ns is None else max(
                0.0, (now_ns - self._preflight_ns) / 1_000_000_000.0)
            preflight["current"] = self._preflight_ns is not None and (
                (now_ns - self._preflight_ns) <= self._preflight_ttl_ns)
        elif self._preflight_error:
            preflight = {"collected_at": None, "age_sec": None, "current": False,
                         "error_code": "E_PREFLIGHT_UNAVAILABLE",
                         "error": self._preflight_error}
        return {
            "schema_version": SCHEMA_VERSION,
            "environment": copy.deepcopy(self._environment),
            "collected_at": self._collected_at_wall,
            "collection_age_sec": age,
            "current": self._current,
            "partial": self._partial,
            "partial_reason": self._partial_reason,
            "summary": self._summary(),
            "preflight": preflight,
            "ports": copy.deepcopy(self._ports),
            "active_issues": active,
            "refresh": dict(self._refresh),
        }

    def _summary(self):
        if not self._current:
            # Never report "ok" from an empty cache (C01: current=false 不是正常).
            return "unknown"
        if self._environment.get("active_mode") != "ros2" or not self._runtime_running:
            return "paused"
        if self._partial:
            return "unknown"
        severity = None
        for issue in self._issues.values():
            if not issue.get("current", True):
                continue
            if severity is None or SEVERITY_RANK[issue["severity"]] > SEVERITY_RANK[severity]:
                severity = issue["severity"]
        if severity in ("error", "critical"):
            return "error"
        if severity == "warning":
            return "warning"
        return "ok"

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _normalize_environment(environment):
        if not isinstance(environment, dict):
            return {}
        return {
            "session_id": _bounded_text(environment.get("session_id"), MAX_SCOPE_TEXT),
            "generation": environment.get("generation"),
            "revision": environment.get("revision"),
            "active_mode": _bounded_text(environment.get("active_mode"), MAX_SCOPE_TEXT),
            "phase": _bounded_text(environment.get("phase"), MAX_SCOPE_TEXT),
        }

    def _identity_text(self, environment):
        return "%s|%s|%s|%s" % (environment.get("session_id"), environment.get("generation"),
                                environment.get("revision"), environment.get("active_mode"))

    def _identity_changed(self, environment):
        if not self._environment:
            return False
        return self._identity_text(environment) != self._identity_text(self._environment)

    def _resolver(self):
        if self._evaluator is not None:
            return self._evaluator
        try:
            from astrbot_ex.core.diagnostics import rules
        except Exception as exc:  # B00 has not landed on this branch yet
            raise DiagnosticsServiceError(
                "astrbot_ex.core.diagnostics.rules is unavailable (B00): %s" % (exc,))
        self._evaluator = rules.evaluate_port_health
        return self._evaluator

    def _preflight_resolver(self):
        if self._preflight_evaluator is not None:
            return self._preflight_evaluator
        try:
            from astrbot_ex.core.diagnostics import rules
        except Exception as exc:
            raise DiagnosticsServiceError(
                "astrbot_ex.core.diagnostics.rules is unavailable (B00): %s" % (exc,))
        self._preflight_evaluator = rules.evaluate_preflight
        return self._preflight_evaluator
