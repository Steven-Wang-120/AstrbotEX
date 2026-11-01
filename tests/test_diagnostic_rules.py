"""Frozen-rule acceptance, independent of wall clocks and ROS."""
import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from astrbot_ex.core.diagnostics.models import issue_key
from astrbot_ex.core.diagnostics.rules import evaluate_port_health, evaluate_preflight


def sample(**values):
    result = dict(endpoint_id="camera", plugin_id="yolo", port_id="image", session_id="s",
                  direction="subscribe", owner_generation="owner", environment_generation=1,
                  binding_generation=2, monitoring_epoch=3, desired_enabled=True, resource_created=True,
                  state="ready", reason_code=None, diagnostics_policy=dict(mode="periodic", startup_grace_sec=10, stale_after_sec=5),
                  monitoring_active=True, bound_monotonic_ns=0, monitoring_started_monotonic_ns=0,
                  last_receive_monotonic_ns=None, last_consume_monotonic_ns=None, binding_received=0,
                  rx_received=900, rx_consumed=899, graph_peer_count=1, matched_peer_count=1,
                  qos_compatibility=[], topic="/camera", effective_qos={})
    result.update(values)
    return result


class DiagnosticRulesTests(unittest.TestCase):
    def test_exact_grace_and_stale_boundaries(self):
        for now, receive, count, expected in [(9_999_000_000, None, 0, "warming_up"),
                (10_000_000_000, None, 0, "no_data"), (15_000_000_000, 10_000_000_000, 1, "receiving"),
                (15_001_000_000, 10_000_000_000, 1, "stale")]:
            with self.subTest(expected=expected):
                self.assertEqual(evaluate_port_health(sample(last_receive_monotonic_ns=receive, binding_received=count), now_ns=now)["data_health"], expected)

    def test_all_health_priorities(self):
        rows = [({"direction": "publish"}, "not_monitored"),
                ({"diagnostics_policy": {"mode": "off"}}, "not_monitored"),
                ({"desired_enabled": False, "monitoring_active": False}, "disabled"),
                ({"monitoring_active": False, "resource_created": False}, "paused"),
                ({"resource_created": False}, "unavailable"),
                ({"bound_monotonic_ns": None}, "unknown")]
        for changes, expected in rows:
            with self.subTest(expected=expected):
                result = evaluate_port_health(sample(**changes), now_ns=20_000_000_000)
                self.assertEqual(result["data_health"], expected)
                self.assertEqual(result["issues"], [])

    def test_paused_monitoring_preserves_factual_receive_and_consume_ages(self):
        result = evaluate_port_health(sample(monitoring_active=False, last_receive_monotonic_ns=5_000_000_000,
            last_consume_monotonic_ns=6_000_000_000, binding_received=1), now_ns=10_000_000_000)
        self.assertEqual(result['data_health'], 'paused')
        self.assertEqual(result['diagnostics']['receive_age_sec'], 5.0)
        self.assertEqual(result['diagnostics']['consume_age_sec'], 4.0)

    def test_visible_peer_has_no_delivery_guarantee(self):
        result = evaluate_port_health(sample(), now_ns=10_000_000_000)
        self.assertEqual(result["issues"][0]["code"], "ROS_RX_NO_DATA_WITH_PEER")
        self.assertEqual(result["data_health"], "no_data")

    def test_no_peer_and_unknown_matched(self):
        result = evaluate_port_health(sample(graph_peer_count=0, matched_peer_count=None), now_ns=10_000_000_000)
        self.assertEqual(result["issues"][0]["code"], "ROS_RX_NO_PEER")
        result = evaluate_port_health(sample(graph_peer_count=0, matched_peer_count=None,
                  binding_received=1, last_receive_monotonic_ns=10_000_000_000), now_ns=10_000_000_000)
        self.assertEqual(result["data_health"], "receiving")

    def test_old_cumulative_counts_do_not_prove_current_receipt(self):
        result = evaluate_port_health(sample(binding_received=999, monitoring_started_monotonic_ns=9_000_000_000), now_ns=10_000_000_000)
        self.assertEqual(result["data_health"], "warming_up")

    def test_inconsistent_evidence_is_unknown(self):
        for changes in [dict(last_receive_monotonic_ns=11_000_000_000, binding_received=1),
                        dict(last_receive_monotonic_ns=-1), dict(bound_monotonic_ns=True),
                        dict(graph_peer_count=None), dict(matched_peer_count=-1),
                        dict(last_consume_monotonic_ns=1),
                        dict(last_receive_monotonic_ns=1, binding_received=0)]:
            with self.subTest(changes=changes):
                self.assertEqual(evaluate_port_health(sample(**changes), now_ns=10_000_000_000)["data_health"], "unknown")

    def test_partial_scope_does_not_publish_but_health_is_local(self):
        result = evaluate_port_health(sample(session_id=None), now_ns=10_000_000_000)
        self.assertEqual(result["data_health"], "no_data")
        self.assertEqual(result["issues"], [])

    def test_explicit_native_errors_and_qos_evidence(self):
        for state, code in [("missing_interface", "ROS_INTERFACE_MISSING"), ("error", "ROS_ENDPOINT_ERROR")]:
            result = evaluate_port_health(sample(state=state, resource_created=False), now_ns=0)
            self.assertEqual(result["issues"][0]["code"], code)
            self.assertEqual(result["issues"][0]["severity"], "error")
        result = evaluate_port_health(sample(qos_compatibility=[{"state": "incompatible"}]), now_ns=0)
        self.assertEqual(result["issues"][0]["code"], "ROS_QOS_INCOMPATIBLE")

    def test_preflight_mapping_merges_dependencies_and_whitelists(self):
        report = {"platform": "linux", "checks": [
            {"id": "rclpy_available", "evidence": {"available": False, "password": "must-not-leak"}},
            {"id": "rosidl_available", "evidence": {"available": False}},
            {"id": "loopback_multicast", "evidence": {"platform": "linux", "localhost_only": True, "multicast": False}},
            {"id": "rmw_consistency", "evidence": {"requested": "a", "effective": "b"}}]}
        result = evaluate_preflight(report)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["evidence"]["missing"], ["rclpy", "rosidl_runtime_py"])
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_unsupported_or_unknown_preflight_never_invents_fault(self):
        for platform, localhost in [("win32", True), ("linux", None), ("linux", False)]:
            report = {"platform": platform, "checks": [
                {"id": "loopback_multicast", "evidence": {"platform": platform, "localhost_only": localhost, "multicast": False}},
                {"id": "rmw_consistency", "evidence": {"requested": "a", "effective": None}},
                {"id": "rmw_effective", "status": "unknown", "evidence": {"effective": None}}]}
            self.assertEqual(evaluate_preflight(report), [])

    def test_deterministic_json_and_no_mutation_or_io(self):
        original = sample()
        saved = copy.deepcopy(original)
        with patch("builtins.open", side_effect=AssertionError("I/O forbidden")), patch("time.time", side_effect=AssertionError("wall clock forbidden")):
            one = evaluate_port_health(original, now_ns=10_000_000_000)
            two = evaluate_port_health(original, now_ns=10_000_000_000)
        self.assertEqual(one, two)
        self.assertEqual(original, saved)
        json.dumps(one, allow_nan=False)
        self.assertNotEqual(issue_key({"endpoint_id": "a|b", "owner_generation": "c"}, "x"),
                            issue_key({"endpoint_id": "a", "owner_generation": "b|c"}, "x"))

    def test_package_import_has_no_ros_or_heavy_imports(self):
        subprocess.run([sys.executable, "-c", "import sys; import astrbot_ex.core.diagnostics; assert not any(x in sys.modules for x in ('rclpy','numpy','torch','astrbot_ex.core.diagnostics.service'))"], check=True)
