"""B04 tests: diagnostics coordination, issue lifecycle and bounded state.

The service is exercised with a fake collector, a fake monotonic clock and a
recording event bus. The evaluator is a small stand-in for B00's rules that
follows the C01 shape, because B00 is a separate task and is not merged into
this branch.

Most tests drive ``_run_cycle()`` directly instead of waiting for the sampling
thread: the timing rules (grace, 60 s reminders, 30 s preflight TTL) must be
reproducible without real sleeps (C04: 单测用假时钟，不实睡10秒). Thread
lifecycle, refresh merging and shutdown are tested against the real thread.

Run from the repository root:
    python -m unittest discover -s tests -p 'test_diagnostics_service.py' -v
"""
from __future__ import annotations

import json
import threading
import unittest

from astrbot_ex.core.diagnostics.service import (
    MAX_EVIDENCE_TEXT,
    PARTIAL_CODE,
    RECOVERED_CODE,
    RECOVERY_STREAK,
    REFRESH_COMPLETED,
    REFRESH_FAILED,
    REFRESH_PENDING,
    REFRESH_RUNNING,
    REFRESH_SUPERSEDED,
    DiagnosticsService,
    issue_key,
)

SESSION = "session-1"


# -- fakes -----------------------------------------------------------------
class FakeClock:
    """One monotonic nanosecond clock plus a matching wall clock."""

    def __init__(self, start_ns=10_000_000_000):
        self.ns = int(start_ns)
        self.wall_seconds = 1_700_000_000.0

    def __call__(self):
        return self.ns

    def wall(self):
        return self.wall_seconds

    def advance(self, seconds):
        self.ns += int(round(float(seconds) * 1_000_000_000))
        self.wall_seconds += float(seconds)
        return self.ns


class RecordingBus:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event_type, message, **data):
        with self.lock:
            self.events.append({"type": event_type, "message": message, "data": data})

    def diagnostics(self):
        with self.lock:
            return [event for event in self.events if event["type"] == "ros_diagnostic"]

    def clear(self):
        with self.lock:
            self.events.clear()


class FakeCollector:
    def __init__(self, ports=None, environment=None, runtime_running=True, complete=True):
        self.ports = ports or []
        self.environment = environment or ros2_environment()
        self.runtime_running = runtime_running
        self.complete = complete
        self.partial_reason = None
        self.error = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "environment": self.environment,
            "runtime_running": self.runtime_running,
            "ports": self.ports,
            "complete": self.complete,
            "partial_reason": self.partial_reason,
        }


def ros2_environment(generation=1, revision=3, active_mode="ros2", phase="ready"):
    return {"session_id": SESSION, "generation": generation, "revision": revision,
            "active_mode": active_mode, "phase": phase}


def sample(endpoint="cam", scenario="healthy", **extra):
    """One C01 section 9 shaped port sample."""
    value = {
        "endpoint_id": endpoint,
        "plugin_id": "yolo",
        "port_id": "image_in",
        "topic": "/camera/image_raw",
        "direction": "subscribe",
        "state": "ready",
        "session_id": SESSION,
        "environment_generation": 1,
        "owner_generation": 1,
        "binding_generation": 2,
        "monitoring_epoch": 5,
        "monitoring_active": True,
        "desired_enabled": True,
        "resource_created": True,
        "scenario": scenario,
    }
    value.update(extra)
    return value


def fake_evaluate(sample_value, *, now_ns):
    """Stand-in for B00 ``evaluate_port_health`` following the C01 shape."""
    scenario = sample_value.get("scenario", "healthy")
    scope = {name: sample_value.get(name) for name in
             ("session_id", "environment_generation", "endpoint_id", "owner_generation",
              "binding_generation", "monitoring_epoch")}
    base = {"data_health": "receiving", "diagnostics": {"reason_code": None}, "issues": []}
    table = {
        "healthy": ({"data_health": "receiving"}, []),
        "warming": ({"data_health": "warming_up"}, []),
        "no_peer": ({"data_health": "no_data"}, [
            {"code": "ROS_RX_NO_PEER", "severity": "warning", "source": "ros.port",
             "scope": scope, "message": "端口未收到数据且图中无发布者",
             "evidence": {"graph_peer_count": 0, "matched_peer_count": 0},
             "remediation": ["确认发布者已启动并处于同一 Domain"]}]),
        "no_data_peer": ({"data_health": "no_data"}, [
            {"code": "ROS_RX_NO_DATA_WITH_PEER", "severity": "warning", "source": "ros.port",
             "scope": scope, "message": "图上有发布者但端口未收到数据",
             "evidence": {"graph_peer_count": 1, "matched_peer_count": 1},
             "remediation": ["核对 QoS 兼容性与 Domain 设置"]}]),
        "stale": ({"data_health": "stale"}, [
            {"code": "ROS_RX_STALE", "severity": "warning", "source": "ros.port",
             "scope": scope, "message": "端口数据已过期",
             "evidence": {"receive_age_sec": 12.5},
             "remediation": ["检查发布频率与网络"]}]),
        "interface_missing": ({"data_health": "unavailable"}, [
            {"code": "ROS_INTERFACE_MISSING", "severity": "error", "source": "ros.port",
             "scope": scope, "message": "缺少接口支持",
             "evidence": {"state": "missing_interface"}, "remediation": ["安装消息接口包"]}]),
    }
    health, issues = table.get(scenario, ({}, []))
    base.update(health)
    base["issues"] = issues
    base["diagnostics"] = dict(base.get("diagnostics") or {})
    return base


def fake_preflight(report):
    """Stand-in for B00 ``evaluate_preflight``."""
    out = []
    for check in report.get("checks", []):
        if check.get("status") == "fail" and check.get("code"):
            out.append({"code": check["code"], "severity": check.get("severity", "warning"),
                        "source": "ros.preflight", "message": check.get("message", check["code"]),
                        "evidence": check.get("evidence", {}),
                        "remediation": check.get("remediation", [])})
    return out


class FakePreflightCollector:
    def __init__(self, checks=None):
        self.checks = checks if checks is not None else []
        self.calls = 0
        self.error = None

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"schema_version": 1, "platform": "linux", "supported": True,
                "checks": list(self.checks), "runtime": {}, "interfaces": [],
                "partial": False}


def build(collector=None, preflight=None, *, evaluator=fake_evaluate, tick_sec=3600.0,
          clock=None, **kwargs):
    clock = clock or FakeClock()
    collector = collector or FakeCollector()
    preflight = preflight or FakePreflightCollector()
    bus = RecordingBus()
    service = DiagnosticsService(
        collector, preflight, bus,
        clock_ns=clock, tick_sec=tick_sec, wall_clock=clock.wall,
        evaluator=evaluator, preflight_evaluator=fake_preflight, **kwargs)
    return service, collector, preflight, bus, clock


# -- construction and cache ------------------------------------------------
class ConstructionTests(unittest.TestCase):
    def test_construction_starts_no_thread_and_reports_unknown(self):
        before = threading.active_count()
        service, collector, _preflight, bus, _clock = build()
        self.assertEqual(threading.active_count(), before)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertFalse(snapshot["current"])
        self.assertEqual(snapshot["summary"], "unknown")
        self.assertEqual(snapshot["active_issues"], [])
        self.assertEqual(snapshot["refresh"]["state"], "idle")
        self.assertEqual(collector.calls, 0)
        self.assertEqual(bus.events, [])

    def test_start_is_idempotent_and_collects_once_synchronously(self):
        service, collector, _preflight, _bus, _clock = build()
        first = service.start()
        self.assertFalse(first["already_running"])
        self.assertTrue(service.snapshot()["current"])
        second = service.start()
        self.assertTrue(second["already_running"])
        self.assertEqual(collector.calls, 1)
        service.close()

    def test_snapshot_never_calls_the_collector(self):
        service, collector, preflight, _bus, clock = build()
        service.start()
        service.close()
        calls = (collector.calls, preflight.calls)
        for _ in range(20):
            service.snapshot()
        self.assertEqual((collector.calls, preflight.calls), calls)

    def test_snapshot_is_a_deep_copy(self):
        service, _collector, _preflight, _bus, _clock = build()
        service.start()
        service.close()
        first = service.snapshot()
        first["active_issues"].append({"code": "TAMPERED"})
        first["environment"]["session_id"] = "tampered"
        second = service.snapshot()
        self.assertEqual(second["active_issues"], [])
        self.assertEqual(second["environment"]["session_id"], SESSION)

    def test_snapshot_from_inside_a_subscriber_does_not_deadlock(self):
        service, collector, _preflight, bus, _clock = build()
        seen = []

        class ReentrantBus(RecordingBus):
            def emit(self, event_type, message, **data):
                seen.append(service.snapshot()["schema_version"])
                RecordingBus.emit(self, event_type, message, **data)

        bus2 = ReentrantBus()
        service._bus = bus2
        collector.ports = [sample(scenario="no_peer")]
        service.start()
        finished = threading.Event()
        worker = threading.Thread(target=lambda: (service._run_cycle(), finished.set()))
        worker.start()
        worker.join(5.0)
        self.assertTrue(finished.is_set(), "snapshot() inside a subscriber deadlocked")
        self.assertTrue(seen)
        service.close()


# -- alerts, reminders and recovery ---------------------------------------
class AlertLifecycleTests(unittest.TestCase):
    def test_first_fault_alerts_then_reminds_at_most_every_60s(self):
        service, collector, _preflight, bus, clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        active = [event for event in bus.diagnostics() if event["data"]["state"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["data"]["code"], "ROS_RX_NO_PEER")
        self.assertEqual(active[0]["data"]["severity"], "warning")

        clock.advance(30)
        service._run_cycle()
        self.assertEqual(len([e for e in bus.diagnostics() if e["data"]["state"] == "active"]), 1,
                         "a second sample inside the reminder window must stay quiet")

        clock.advance(31)
        service._run_cycle()
        reminders = [e for e in bus.diagnostics()
                     if e["data"]["state"] == "active" and e["data"].get("reminder")]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["data"]["occurrences"], 3)

    def test_refresh_does_not_bypass_the_reminder_window(self):
        service, collector, _preflight, bus, clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        clock.advance(5)
        service._refresh["state"] = REFRESH_RUNNING
        service._safe_cycle(refresh=True)
        active = [e for e in bus.diagnostics() if e["data"]["state"] == "active"]
        self.assertEqual(len(active), 1, "refresh must not re-alert inside the budget")

    def test_two_consecutive_healthy_samples_recover_once(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        collector.ports = [sample(scenario="healthy")]

        service._run_cycle()
        self.assertEqual([e for e in bus.diagnostics() if e["data"]["state"] == "recovered"], [])
        self.assertEqual(len(service.snapshot()["active_issues"]), 1)

        service._run_cycle()
        recovered = [e for e in bus.diagnostics() if e["data"]["state"] == "recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["data"]["code"], RECOVERED_CODE)
        self.assertEqual(recovered[0]["data"]["recovered_code"], "ROS_RX_NO_PEER")
        self.assertEqual(recovered[0]["data"]["severity"], "info")
        self.assertEqual(service.snapshot()["active_issues"], [])

        service._run_cycle()
        self.assertEqual(len([e for e in bus.diagnostics()
                              if e["data"]["state"] == "recovered"]), 1,
                         "recovery is announced exactly once")

    def test_same_fault_returning_gets_a_new_first_seen(self):
        service, collector, _preflight, bus, clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        first_seen = service.snapshot()["active_issues"][0]["first_seen_at"]

        collector.ports = [sample(scenario="healthy")]
        service._run_cycle()
        service._run_cycle()
        # Past the shared 60 s reminder budget, otherwise the family would
        # (correctly) stay quiet and the new first_seen could not be observed.
        clock.advance(61)

        bus.clear()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        issue = service.snapshot()["active_issues"][0]
        self.assertGreater(issue["first_seen_at"], first_seen)
        self.assertEqual(issue["occurrences"], 1)
        self.assertEqual(len([e for e in bus.diagnostics() if e["data"]["state"] == "active"]), 1)

    def test_two_ports_do_not_swallow_each_other(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(endpoint="cam", scenario="no_peer"),
                           sample(endpoint="scan", scenario="stale")]
        service._run_cycle()
        codes = {issue["code"] for issue in service.snapshot()["active_issues"]}
        self.assertEqual(codes, {"ROS_RX_NO_PEER", "ROS_RX_STALE"})
        self.assertEqual(len(service.snapshot()["active_issues"]), 2)

        collector.ports = [sample(endpoint="cam", scenario="healthy"),
                           sample(endpoint="scan", scenario="stale")]
        service._run_cycle()
        service._run_cycle()
        remaining = service.snapshot()["active_issues"]
        self.assertEqual([issue["code"] for issue in remaining], ["ROS_RX_STALE"])
        self.assertEqual(remaining[0]["scope"]["endpoint_id"], "scan")

    def test_rx_reclassification_withdraws_instead_of_recovering(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        bus.clear()

        collector.ports = [sample(scenario="stale")]
        service._run_cycle()
        diagnostics = bus.diagnostics()
        states = [event["data"]["state"] for event in diagnostics]
        self.assertIn("withdrawn", states)
        self.assertNotIn("recovered", states)
        withdrawn = [e for e in diagnostics if e["data"]["state"] == "withdrawn"][0]
        self.assertEqual(withdrawn["data"]["code"], "ROS_RX_NO_PEER")
        self.assertEqual(withdrawn["data"]["reason"], "reclassified")

        active = service.snapshot()["active_issues"]
        self.assertEqual([issue["code"] for issue in active], ["ROS_RX_STALE"],
                         "the RX family keeps at most one active issue per scope")

    def test_rx_family_reclassification_respects_the_shared_budget(self):
        service, collector, _preflight, bus, clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        bus.clear()
        clock.advance(10)
        collector.ports = [sample(scenario="stale")]
        service._run_cycle()
        self.assertEqual([e for e in bus.diagnostics() if e["data"]["state"] == "active"], [],
                         "peer flapping must not produce a new first alert inside 60s")
        clock.advance(51)
        collector.ports = [sample(scenario="no_data_peer")]
        service._run_cycle()
        self.assertEqual(len([e for e in bus.diagnostics() if e["data"]["state"] == "active"]), 1)


# -- preflight -------------------------------------------------------------
class PreflightTests(unittest.TestCase):
    def test_preflight_is_cached_for_30_seconds(self):
        checks = [{"id": "loopback_multicast", "status": "fail",
                   "code": "ROS_LOOPBACK_MULTICAST_DISABLED", "severity": "warning",
                   "message": "lo 组播被关闭", "evidence": {"platform": "linux"}}]
        service, _collector, preflight, _bus, clock = build(preflight=FakePreflightCollector(checks))
        service._run_cycle()
        self.assertEqual(preflight.calls, 1)
        self.assertIn("ROS_LOOPBACK_MULTICAST_DISABLED",
                      {issue["code"] for issue in service.snapshot()["active_issues"]})

        clock.advance(10)
        service._run_cycle()
        self.assertEqual(preflight.calls, 1, "the TTL has not expired yet")

        clock.advance(25)
        service._run_cycle()
        self.assertEqual(preflight.calls, 2, "a manual fix must be discovered after the TTL")

    def test_preflight_snapshot_carries_age_and_current(self):
        service, _collector, _preflight, _bus, clock = build()
        service._run_cycle()
        clock.advance(12)
        preflight = service.snapshot()["preflight"]
        self.assertTrue(preflight["current"])
        self.assertAlmostEqual(preflight["age_sec"], 12.0, places=3)
        self.assertIsNotNone(preflight["collected_at"])

    def test_normal_environment_keeps_preflight_passive(self):
        checks = [{"id": "rclpy_available", "status": "fail", "code": "ROS_DEPENDENCY_MISSING",
                   "severity": "error", "message": "rclpy 缺失"}]
        collector = FakeCollector(environment=ros2_environment(active_mode="normal"))
        service, _collector, _preflight, _bus, _clock = build(
            collector=collector, preflight=FakePreflightCollector(checks))
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertEqual(snapshot["summary"], "paused")
        self.assertEqual(snapshot["active_issues"], [],
                         "a normal environment has no ROS dependency to report")
        self.assertIsNotNone(snapshot["preflight"], "the report stays visible")


# -- partial evidence ------------------------------------------------------
class PartialEvidenceTests(unittest.TestCase):
    def test_collector_exception_keeps_issues_and_never_recovers(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        self.assertEqual(len(service.snapshot()["active_issues"]), 1)

        bus.clear()
        collector.error = RuntimeError("graph read failed")
        service._run_cycle()
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertTrue(snapshot["partial"])
        self.assertEqual(snapshot["summary"], "unknown")
        codes = {issue["code"] for issue in snapshot["active_issues"]}
        self.assertIn("ROS_RX_NO_PEER", codes, "the port issue is kept, not cleared")
        port_issue = [i for i in snapshot["active_issues"] if i["code"] == "ROS_RX_NO_PEER"][0]
        self.assertFalse(port_issue["current"], "a stale issue must not read as current")
        self.assertIn(PARTIAL_CODE, codes, "the collection gap itself is reported")
        self.assertEqual([e for e in bus.diagnostics() if e["data"]["state"] == "recovered"], [],
                         "a failed collection must not fake a recovery")
        self.assertIn(PARTIAL_CODE, {e["data"]["code"] for e in bus.diagnostics()})

    def test_incomplete_collection_marks_partial_and_warns(self):
        collector = FakeCollector(complete=False)
        collector.partial_reason = "graph query timed out"
        service, _collector, _preflight, bus, _clock = build(collector=collector)
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertTrue(snapshot["partial"])
        self.assertEqual(snapshot["partial_reason"], "graph query timed out")
        self.assertEqual(snapshot["summary"], "unknown")
        self.assertIn(PARTIAL_CODE, {e["data"]["code"] for e in bus.diagnostics()})

    def test_missing_rules_module_is_partial_not_healthy(self):
        service, _collector, _preflight, bus, _clock = build(evaluator=None)
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertTrue(snapshot["partial"])
        self.assertNotEqual(snapshot["summary"], "ok")
        self.assertIn(PARTIAL_CODE, {e["data"]["code"] for e in bus.diagnostics()})

    def test_active_issue_cap_marks_partial(self):
        ports = [sample(endpoint="cam%03d" % index, scenario="no_peer") for index in range(8)]
        service, collector, _preflight, _bus, _clock = build(max_active_issues=4)
        collector.ports = ports
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertEqual(len(snapshot["active_issues"]), 4)
        self.assertTrue(snapshot["partial"])
        self.assertNotEqual(snapshot["summary"], "ok")

    def test_port_sample_cap_marks_partial(self):
        ports = [sample(endpoint="cam%03d" % index) for index in range(6)]
        service, collector, _preflight, _bus, _clock = build(max_port_samples=3)
        collector.ports = ports
        service._run_cycle()
        snapshot = service.snapshot()
        self.assertEqual(len(snapshot["ports"]), 3)
        self.assertTrue(snapshot["partial"])
        self.assertIn("port sample limit", snapshot["partial_reason"])

    def test_evidence_is_bounded_and_control_characters_removed(self):
        def evaluator(sample_value, *, now_ns):
            return {"data_health": "no_data", "diagnostics": {}, "issues": [{
                "code": "ROS_RX_NO_PEER", "severity": "warning", "source": "ros.port",
                "message": "x" * 5000,
                "evidence": {"long": "y" * 5000, "escape": "bad\x1b[31mtext",
                             **{"k%d" % index: index for index in range(60)}},
                "remediation": "z" * 900,
            }]}

        service, collector, _preflight, _bus, _clock = build(evaluator=evaluator)
        collector.ports = [sample()]
        service._run_cycle()
        issue = service.snapshot()["active_issues"][0]
        self.assertLessEqual(len(issue["message"]), MAX_EVIDENCE_TEXT)
        self.assertLessEqual(len(issue["evidence"]["long"]), MAX_EVIDENCE_TEXT)
        self.assertNotIn("\x1b", issue["evidence"]["escape"])
        self.assertLessEqual(len(issue["evidence"]), 25)
        self.assertLessEqual(len(issue["remediation"]), 1)

    def test_candidate_without_scope_is_dropped(self):
        def evaluator(sample_value, *, now_ns):
            return {"data_health": "no_data", "diagnostics": {}, "issues": [
                {"code": "ROS_RX_NO_PEER", "severity": "warning", "scope": {}}]}

        bare = sample()
        bare.pop("session_id")
        bare.pop("endpoint_id")
        service, collector, _preflight, _bus, _clock = build(evaluator=evaluator)
        collector.ports = [bare]
        service._run_cycle()
        self.assertEqual(service.snapshot()["active_issues"], [])


# -- identity, withdrawal and windows --------------------------------------
class IdentityTests(unittest.TestCase):
    def test_switching_to_idle_withdraws_instead_of_recovering(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        bus.clear()

        collector.runtime_running = False
        service._run_cycle()
        diagnostics = bus.diagnostics()
        states = [event["data"]["state"] for event in diagnostics]
        self.assertIn("withdrawn", states)
        self.assertNotIn("recovered", states,
                         "pausing monitoring is not a device recovery")
        self.assertEqual(service.snapshot()["summary"], "paused")

    def test_new_generation_withdraws_the_old_scope(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        bus.clear()

        collector.environment = ros2_environment(generation=2)
        collector.ports = [sample(environment_generation=2, scenario="healthy")]
        service._run_cycle()
        diagnostics = bus.diagnostics()
        withdrawn = [e for e in diagnostics if e["data"]["state"] == "withdrawn"]
        self.assertTrue(withdrawn)
        self.assertEqual(withdrawn[0]["data"]["reason"], "scope_changed")
        self.assertEqual([e for e in diagnostics if e["data"]["state"] == "recovered"], [])
        self.assertEqual(service.snapshot()["active_issues"], [])

    def test_invalidate_drops_scope_and_supersedes_a_pending_refresh(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        service.request_refresh()
        bus.clear()

        outcome = service.invalidate({"session_id": "session-2", "generation": 1,
                                      "revision": 1, "active_mode": "ros2"})
        self.assertTrue(outcome["invalidated"])
        self.assertIsNotNone(outcome["superseded_refresh"])
        snapshot = service.snapshot()
        self.assertEqual(snapshot["refresh"]["state"], REFRESH_SUPERSEDED)
        self.assertEqual(snapshot["active_issues"], [])
        self.assertFalse(snapshot["current"])
        self.assertEqual(snapshot["summary"], "unknown")
        self.assertIn("withdrawn", [e["data"]["state"] for e in bus.diagnostics()])


# -- refresh state machine -------------------------------------------------
class RefreshTests(unittest.TestCase):
    def test_refresh_storm_merges_into_one_request(self):
        service, _collector, _preflight, _bus, _clock = build()
        service.start()
        try:
            before = threading.active_count()
            results = [service.request_refresh() for _ in range(50)]
            ids = {result["refresh_id"] for result in results}
            self.assertEqual(len(ids), 1)
            self.assertTrue(all(result["merged"] or index == 0
                                for index, result in enumerate(results)))
            self.assertEqual(threading.active_count(), before,
                             "collapsing a storm must not create threads")
        finally:
            service.close()

    def test_new_refresh_after_completion_gets_a_new_id(self):
        service, _collector, _preflight, _bus, _clock = build()
        first = service.request_refresh()
        service._finish_refresh(REFRESH_COMPLETED, None)
        second = service.request_refresh()
        self.assertNotEqual(first["refresh_id"], second["refresh_id"])
        self.assertEqual(second["state"], REFRESH_PENDING)

    def test_slow_probe_for_an_old_generation_is_superseded(self):
        service, collector, _preflight, _bus, _clock = build()
        service._run_cycle()
        service.request_refresh()

        collector.environment = ros2_environment(generation=2)
        service._refresh["state"] = REFRESH_RUNNING
        service._safe_cycle(refresh=True)

        snapshot = service.snapshot()
        self.assertEqual(snapshot["refresh"]["state"], REFRESH_SUPERSEDED)
        self.assertEqual(snapshot["refresh"]["error_code"], "E_SUPERSEDED")

    def test_refresh_completes_on_a_stable_identity(self):
        service, _collector, _preflight, _bus, _clock = build()
        service._run_cycle()
        service.request_refresh()
        service._refresh["state"] = REFRESH_RUNNING
        service._safe_cycle(refresh=True)
        self.assertEqual(service.snapshot()["refresh"]["state"], REFRESH_COMPLETED)

    def test_refresh_failure_is_reported_without_dropping_issues(self):
        service, collector, _preflight, _bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        service.request_refresh()

        def boom(*_args, **_kwargs):
            raise RuntimeError("cycle exploded")

        original = service._run_cycle
        service._run_cycle = boom
        service._refresh["state"] = REFRESH_RUNNING
        service._safe_cycle(refresh=True)
        service._run_cycle = original

        snapshot = service.snapshot()
        self.assertEqual(snapshot["refresh"]["state"], REFRESH_FAILED)
        self.assertEqual(snapshot["refresh"]["error_code"], "E_CYCLE_FAILED")
        self.assertEqual(len(snapshot["active_issues"]), 1)


# -- capacity and shutdown -------------------------------------------------
class CapacityAndShutdownTests(unittest.TestCase):
    def test_ten_thousand_rebinds_keep_the_maps_bounded(self):
        service, collector, _preflight, _bus, _clock = build()
        for index in range(10_000):
            collector.ports = [sample(binding_generation=index,
                                      monitoring_epoch=index,
                                      scenario="no_peer" if index % 2 else "healthy")]
            service._run_cycle()
        snapshot = service.snapshot()
        self.assertLessEqual(len(snapshot["active_issues"]), 1)
        self.assertLessEqual(len(service._issues), 2)
        self.assertLessEqual(len(service._healthy_streak), 2)
        self.assertLessEqual(len(service._family_alerted_at), service._family_limit)
        json.dumps(snapshot)  # the cache must stay JSON compatible

    def test_close_is_idempotent_and_stops_events(self):
        service, collector, _preflight, bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service.start()
        first = service.close()
        self.assertTrue(first["closed"])
        self.assertFalse(first["already_closed"])
        bus.clear()
        second = service.close()
        self.assertTrue(second["already_closed"])
        service._emit({"type": "ros_diagnostic", "message": "late", "data": {}})
        self.assertEqual(bus.events, [], "a closed service must not publish")

    def test_close_refuses_to_claim_success_while_the_thread_runs(self):
        service, _collector, _preflight, _bus, _clock = build()
        service.start()
        blocker = threading.Event()
        entered = threading.Event()

        def stuck_cycle():
            entered.set()
            blocker.wait(2.0)

        service._run_cycle = stuck_cycle
        service._wake.set()
        self.assertTrue(entered.wait(2.0), "the sampling thread never started the cycle")
        try:
            with self.assertRaises(Exception):
                service.close(timeout=0.01)
        finally:
            blocker.set()
            try:
                service.close(timeout=2.0)
            except Exception:
                pass

    def test_twenty_start_close_rounds_return_to_the_thread_baseline(self):
        baseline = threading.active_count()
        for _ in range(20):
            service, _collector, _preflight, _bus, _clock = build()
            service.start()
            service.close()
        self.assertLessEqual(threading.active_count(), baseline)

    def test_snapshot_is_json_serialisable(self):
        service, collector, _preflight, _bus, _clock = build()
        collector.ports = [sample(scenario="no_peer")]
        service._run_cycle()
        encoded = json.dumps(service.snapshot())
        self.assertIn("active_issues", encoded)

    def test_issue_key_is_stable_and_code_specific(self):
        scope = {"session_id": "s", "environment_generation": 1, "endpoint_id": "cam",
                 "owner_generation": 1, "binding_generation": 1, "monitoring_epoch": 1}
        self.assertEqual(issue_key(scope, "A"), issue_key(dict(scope), "A"))
        self.assertNotEqual(issue_key(scope, "A"), issue_key(scope, "B"))
        other = dict(scope, endpoint_id="scan")
        self.assertNotEqual(issue_key(scope, "A"), issue_key(other, "A"))

    def test_recovery_streak_constant_is_two(self):
        self.assertEqual(RECOVERY_STREAK, 2)


if __name__ == "__main__":
    unittest.main()
