"""B02: monotonic port clocks and current-window receive health.

The suite separates four facts that used to collapse into `state=ready`: the
native resource exists, the graph shows a peer, a callback actually arrived in
the *current* binding and window, and the plugin actually consumed a message.
Nothing here touches ROS; the native endpoint is a fake and the clock is
injected, so every assertion is deterministic and no test sleeps.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from astrbot_ex.core.environments import plugin_api as plugin_api_module
from astrbot_ex.core.environments.contracts import (
    DIAGNOSTICS_DEFAULTS,
    diagnostics_policy,
    normalize_bindings,
    parse_ports,
)
from astrbot_ex.core.environments.plugin_api import DATA_HEALTH, PluginRosFacade


class Message:
    def __init__(self, data=""):
        self.data = data


class FakeNative:
    def __init__(self, topic):
        self.topic_name = topic
        self.published = []

    def publish(self, message):
        self.published.append(message)


class FakeTime:
    """Deterministic monotonic clock with an independently jumpable wall clock."""

    def __init__(self, start_ns=5_000_000_000):
        self.now_ns = start_ns
        self.wall = 1_700_000_000.0

    def monotonic_ns(self):
        return self.now_ns

    def monotonic(self):
        return self.now_ns / 1_000_000_000

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.now_ns += int(round(seconds * 1_000_000_000))


SUBSCRIPTION = {
    "id": "input",
    "direction": "subscribe",
    "message_types": ["std_msgs/msg/String"],
    "default_topic": "/input",
    "queue": {"capacity": 4, "overflow": "keep_latest", "max_age_ms": 0},
    "diagnostics": {"mode": "periodic", "startup_grace_sec": 10.0, "stale_after_sec": 5.0},
}

SAMPLE_KEYS = {
    "endpoint_id", "plugin_id", "port_id", "direction", "owner_generation",
    "environment_generation", "binding_generation", "monitoring_epoch",
    "desired_enabled", "resource_created", "state", "reason_code",
    "diagnostics_policy", "monitoring_active",
    "bound_monotonic_ns", "monitoring_started_monotonic_ns",
    "last_receive_monotonic_ns", "last_consume_monotonic_ns",
    "binding_received", "rx_received", "rx_consumed",
    "graph_peer_count", "matched_peer_count", "qos_compatibility", "topic",
    "effective_qos",
}

LEGACY_STATUS_KEYS = {
    "endpoint_id", "owner_generation", "plugin_id", "port_id", "direction",
    "environment_generation", "binding_generation", "requested_topic",
    "resolved_topic", "topic", "message_type", "desired_enabled",
    "resource_created", "state", "status", "message", "reason_code",
    "effective_qos", "local_queue_policy", "matched_peer_count", "queue_depth",
    "graph_peer_count", "qos_compatibility", "queue_bytes", "rate_hz",
    "in_flight", "last_message_at", "rx_received", "rx_consumed", "tx_queued",
    "tx_published", "queue_dropped", "expired", "rejected", "error_count",
    "oversized", "queue_peak_bytes",
}

DIAGNOSTICS_KEYS = {
    "policy", "monitoring_active", "monitoring_epoch", "binding_received",
    "receive_age_sec", "consume_age_sec", "monitoring_age_sec", "reason_code",
}


def build(declaration, bindings=None):
    facade = PluginRosFacade(plugin_id="demo", ports=[declaration], bindings=bindings)
    handle = (facade.subscribe(declaration["id"]) if declaration["direction"] == "subscribe"
              else facade.publisher(declaration["id"]))
    return facade, handle._port


def bind(port, generation=7):
    """Mirror Ros2EnvironmentAdapter.attach: native first, then reset binding evidence."""
    port.native = FakeNative(port.binding["topic"])
    port.message_type = Message
    port.generation = generation
    port.binding_generation += 1
    port.state = "waiting_peer"
    port.mark_bound()
    return port.binding_generation


class PortHealthTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeTime()
        patcher = mock.patch.object(plugin_api_module, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        facade, self.port = build(SUBSCRIPTION)
        self.addCleanup(facade.close)
        self.generation = 7

    def receive(self, message=None):
        self.port.receive(message or Message(), self.generation, self.port.binding_generation)

    # --- no callbacks yet -------------------------------------------------

    def test_peer_visible_with_zero_callbacks_shows_no_receive_evidence(self):
        self.port.graph_peers = 3
        self.port.matched = 3
        self.port.set_diagnostics_activity(True)
        bind(self.port, self.generation)
        self.clock.advance(12)

        status = self.port.status()
        self.assertEqual(status["graph_peer_count"], 3)
        self.assertEqual(status["matched_peer_count"], 3)
        self.assertEqual(status["diagnostics"]["binding_received"], 0)
        self.assertIsNone(status["diagnostics"]["receive_age_sec"])
        self.assertEqual(status["diagnostics"]["monitoring_age_sec"], 12.0)
        self.assertNotEqual(status["data_health"], "receiving")

    def test_matched_unknown_but_current_callback_is_strong_evidence(self):
        bind(self.port, self.generation)
        self.port.matched = None
        self.port.graph_peers = 0
        self.receive()

        sample = self.port.diagnostics_sample()
        self.assertIsNone(sample["matched_peer_count"], "matched=null must never become 0")
        self.assertEqual(sample["graph_peer_count"], 0)
        self.assertEqual(sample["binding_received"], 1)
        self.assertEqual(sample["last_receive_monotonic_ns"], self.clock.now_ns)
        self.assertEqual(self.port.status()["diagnostics"]["receive_age_sec"], 0.0)

    def test_zero_matched_stays_a_known_zero(self):
        bind(self.port, self.generation)
        self.port.matched = 0
        self.assertEqual(self.port.diagnostics_sample()["matched_peer_count"], 0)

    # --- binding generations ---------------------------------------------

    def test_rebind_clears_binding_evidence_and_keeps_cumulative_stats(self):
        bind(self.port, self.generation)
        self.receive()
        self.assertEqual(self.port.stats["rx_received"], 1)
        first_binding = self.port.binding_generation

        self.clock.advance(1)
        self.port.unavailable()
        bind(self.port, 8)

        sample = self.port.diagnostics_sample()
        self.assertEqual(sample["rx_received"], 1, "cumulative stats are history, not health")
        self.assertEqual(sample["binding_received"], 0, "a new binding starts with no callbacks")
        self.assertIsNone(sample["last_receive_monotonic_ns"])
        self.assertEqual(sample["binding_generation"], first_binding + 2)

    def test_late_callback_from_the_previous_binding_is_rejected(self):
        bind(self.port, self.generation)
        stale_epoch = self.port.binding_generation
        self.clock.advance(1)
        self.port.unavailable()
        bind(self.port, 8)

        self.port.receive(Message(), self.generation, stale_epoch)

        self.assertEqual(self.port.stats["rx_received"], 0)
        self.assertEqual(self.port.binding_received, 0)
        self.assertIsNone(self.port.last_receive_monotonic_ns)
        self.assertEqual(len(self.port.queue), 0)

    def test_callback_from_the_previous_environment_generation_is_rejected(self):
        bind(self.port, self.generation)
        self.port.receive(Message(), self.generation - 1, self.port.binding_generation)
        self.assertEqual(self.port.stats["rx_received"], 0)

    def test_disabled_binding_never_counts(self):
        facade, port = build(SUBSCRIPTION, bindings={"input": {"enabled": False}})
        self.addCleanup(facade.close)
        # A disabled binding is never attached, so no native endpoint exists.
        port.generation = 3
        port.binding_generation += 1
        port.receive(Message(), 3, port.binding_generation)
        self.assertEqual(port.stats["rx_received"], 0)
        self.assertIsNone(port.diagnostics_sample()["last_receive_monotonic_ns"])

    # --- stream stops after receiving ------------------------------------

    def test_stream_stopping_after_receive_shows_age_growth_not_health_loss(self):
        bind(self.port, self.generation)
        self.receive()
        self.clock.advance(5.0)
        self.assertEqual(self.port.status()["diagnostics"]["receive_age_sec"], 5.0)
        self.clock.advance(0.001)
        status = self.port.status()
        self.assertAlmostEqual(status["diagnostics"]["receive_age_sec"], 5.001, places=6)
        self.assertEqual(status["diagnostics"]["binding_received"], 1,
                         "the window did receive; it only stopped")

    # --- observation windows ---------------------------------------------

    def test_repeated_activity_does_not_extend_the_grace_window(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        started = self.port.monitoring_started_monotonic_ns
        epoch = self.port.monitoring_epoch
        self.clock.advance(8)
        self.port.set_diagnostics_activity(True)
        self.assertEqual(self.port.monitoring_started_monotonic_ns, started)
        self.assertEqual(self.port.monitoring_epoch, epoch)
        self.assertEqual(self.port.status()["diagnostics"]["monitoring_age_sec"], 8.0)

    def test_idle_then_fast_restart_opens_a_fresh_window(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        self.receive()
        first_epoch = self.port.monitoring_epoch
        self.assertEqual(self.port.window_received, 1)

        self.port.set_diagnostics_activity(False)   # runtime stop
        self.port.set_diagnostics_activity(True)    # immediate restart
        self.assertEqual(self.port.monitoring_epoch, first_epoch + 1)
        self.assertEqual(self.port.window_received, 0)
        self.assertIsNone(self.port.last_receive_monotonic_ns)
        self.assertEqual(self.port.status()["diagnostics"]["monitoring_age_sec"], 0.0)

    def test_idle_window_is_revoked(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        self.port.set_diagnostics_activity(False)
        sample = self.port.diagnostics_sample()
        self.assertFalse(sample["monitoring_active"])
        self.assertIsNone(sample["monitoring_started_monotonic_ns"])
        self.assertNotEqual(self.port.status()["data_health"], "receiving")

    def test_window_starts_at_binding_not_at_runtime_start(self):
        self.port.set_diagnostics_activity(True)
        self.clock.advance(30)          # runtime was already running well past the grace
        bind(self.port, self.generation)
        status = self.port.status()
        self.assertEqual(status["diagnostics"]["monitoring_age_sec"], 0.0)
        self.assertEqual(self.port.monitoring_started_monotonic_ns, self.port.bound_monotonic_ns)

    def test_no_data_timer_waits_for_the_native_resource(self):
        self.port.set_diagnostics_activity(True)
        sample = self.port.diagnostics_sample()
        self.assertTrue(sample["monitoring_active"], "expected work stays true when the resource is missing")
        self.assertFalse(sample["resource_created"])
        self.assertIsNone(sample["monitoring_started_monotonic_ns"])

    def test_activity_flag_must_be_boolean(self):
        for value in (1, 0, "true", None):
            with self.assertRaises(ValueError):
                self.port.set_diagnostics_activity(value)

    # --- clock independence ----------------------------------------------

    def test_wall_clock_jumps_do_not_move_the_health_window(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        self.receive()
        self.clock.advance(2)
        before = self.port.status()["diagnostics"]
        self.clock.wall += 3600
        self.assertNotEqual(self.clock.time(), 1_700_000_000.0, "wall clock did move")
        self.assertEqual(before["receive_age_sec"],
                         self.port.status()["diagnostics"]["receive_age_sec"])
        self.clock.wall -= 7200
        self.assertEqual(before["receive_age_sec"],
                         self.port.status()["diagnostics"]["receive_age_sec"])
        self.assertEqual(before["monitoring_age_sec"],
                         self.port.status()["diagnostics"]["monitoring_age_sec"])

    # --- transport versus consumption ------------------------------------

    def test_oversized_message_proves_transport_but_not_consumption(self):
        declaration = {**SUBSCRIPTION,
                       "queue": {"capacity": 4, "overflow": "keep_latest", "max_age_ms": 0,
                                 "max_message_bytes": 64, "max_bytes": 128}}
        facade, port = build(declaration)
        self.addCleanup(facade.close)
        bind(port, 5)
        port.receive(Message("x" * 4096), 5, port.binding_generation)

        self.assertEqual(port.stats["rx_received"], 1)
        self.assertEqual(port.binding_received, 1)
        self.assertEqual(port.stats["oversized"], 1)
        self.assertEqual(len(port.queue), 0)
        self.assertIsNone(port.get_nowait())
        self.assertIsNone(port.last_consume_monotonic_ns)
        self.assertIsNone(port.status()["diagnostics"]["consume_age_sec"])

    def test_expired_message_never_updates_the_consume_clock(self):
        declaration = {**SUBSCRIPTION,
                       "queue": {"capacity": 4, "overflow": "keep_latest", "max_age_ms": 100}}
        facade, port = build(declaration)
        self.addCleanup(facade.close)
        bind(port, 5)
        port.receive(Message(), 5, port.binding_generation)

        self.clock.advance(0.101)
        self.assertIsNone(port.get_nowait())
        self.assertEqual(port.stats["expired"], 1)
        self.assertIsNone(port.last_consume_monotonic_ns)

        port.receive(Message(), 5, port.binding_generation)
        self.assertIsNotNone(port.get_nowait())
        self.assertEqual(port.last_consume_monotonic_ns, self.clock.now_ns)

    def test_take_latest_counts_one_consumption(self):
        bind(self.port, self.generation)
        for _ in range(3):
            self.receive()
        self.assertIsNotNone(self.port.take_latest())
        self.assertEqual(self.port.stats["rx_consumed"], 1)
        self.assertEqual(self.port.last_consume_monotonic_ns, self.clock.now_ns)

    def test_queue_is_empty_so_nothing_is_consumed(self):
        bind(self.port, self.generation)
        self.assertIsNone(self.port.get_nowait())
        self.assertEqual(self.port.stats["rx_consumed"], 0)
        self.assertIsNone(self.port.last_consume_monotonic_ns)

    # --- not monitored ----------------------------------------------------

    def test_publish_and_mark_ports_are_not_monitored(self):
        ports = [
            {"id": "output", "direction": "publish",
             "message_types": ["std_msgs/msg/String"], "default_topic": "/output"},
            {**SUBSCRIPTION, "id": "mark", "default_topic": "/mark", "diagnostics": {"mode": "off"}},
        ]
        facade = PluginRosFacade(plugin_id="demo", ports=ports)
        self.addCleanup(facade.close)
        publisher = facade.publisher("output")._port
        mark = facade.subscribe("mark")._port
        for port in (publisher, mark):
            self.assertFalse(port.monitored)
            self.assertEqual(port.status()["data_health"], "not_monitored")

    def test_off_mode_never_calls_the_health_rules(self):
        facade, port = build({**SUBSCRIPTION, "diagnostics": {"mode": "off"}})
        self.addCleanup(facade.close)

        def explode():
            raise AssertionError("rules must not run for unmonitored ports")

        with mock.patch.object(plugin_api_module, "health_evaluator", explode):
            self.assertEqual(port.status()["data_health"], "not_monitored")

    # --- sample and status contracts -------------------------------------

    def test_sample_is_flat_bounded_and_json_safe(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        self.receive()
        sample = self.port.diagnostics_sample()

        self.assertEqual(set(sample), SAMPLE_KEYS)
        json.dumps(sample)
        self.assertNotIn("native", sample)
        self.assertNotIn("message_type", sample)
        self.assertEqual(sample["monitoring_epoch"], 1)
        self.assertEqual(sample["diagnostics_policy"],
                         {"mode": "periodic", "startup_grace_sec": 10.0, "stale_after_sec": 5.0})

    def test_sample_does_not_hand_out_mutable_internals(self):
        bind(self.port, self.generation)
        sample = self.port.diagnostics_sample()
        sample["effective_qos"]["depth"] = 999
        sample["diagnostics_policy"]["mode"] = "off"
        fresh = self.port.diagnostics_sample()
        self.assertNotEqual(fresh["effective_qos"]["depth"], 999)
        self.assertEqual(fresh["diagnostics_policy"]["mode"], "periodic")

    def test_status_keeps_every_legacy_field_and_adds_health(self):
        bind(self.port, self.generation)
        status = self.port.status()
        self.assertLessEqual(LEGACY_STATUS_KEYS, set(status))
        self.assertLessEqual(DIAGNOSTICS_KEYS, set(status["diagnostics"]))
        self.assertEqual(status["state"], status["status"])
        self.assertIn(status["data_health"], DATA_HEALTH)
        self.assertEqual(status["diagnostics"]["monitoring_epoch"], self.port.monitoring_epoch)

    def test_status_ages_are_non_negative(self):
        bind(self.port, self.generation)
        self.port.set_diagnostics_activity(True)
        self.receive()
        self.clock.now_ns -= 1_000_000_000        # evidence stamped in the "future"
        diagnostics = self.port.status()["diagnostics"]
        for key in ("receive_age_sec", "consume_age_sec", "monitoring_age_sec"):
            if diagnostics[key] is not None:
                self.assertGreaterEqual(diagnostics[key], 0.0)

    # --- health rule wiring ----------------------------------------------

    def test_health_is_unknown_when_rules_are_unavailable(self):
        bind(self.port, self.generation)
        with mock.patch.object(plugin_api_module, "health_evaluator", lambda: None):
            status = self.port.status()
        self.assertEqual(status["data_health"], "unknown", "missing rules must never look green")
        self.assertEqual(status["diagnostics"]["reason_code"], "health_rules_unavailable")

    def test_health_uses_rules_on_the_frozen_sample(self):
        bind(self.port, self.generation)
        captured = {}

        def evaluator(sample, *, now_ns):
            captured["sample"] = sample
            captured["now_ns"] = now_ns
            return {"data_health": "receiving", "diagnostics": {"reason_code": "rx_ok"}}

        with mock.patch.object(plugin_api_module, "health_evaluator", lambda: evaluator):
            status = self.port.status()

        self.assertEqual(status["data_health"], "receiving")
        self.assertEqual(status["diagnostics"]["reason_code"], "rx_ok")
        self.assertIn("receive_age_sec", status["diagnostics"], "local ages survive the merge")
        self.assertEqual(set(captured["sample"]), SAMPLE_KEYS)
        self.assertEqual(captured["now_ns"], self.clock.now_ns)

    def test_broken_rule_output_degrades_to_unknown(self):
        bind(self.port, self.generation)
        cases = [
            ("health_rules_invalid", lambda sample, *, now_ns: {"data_health": "healthy"}),
            ("health_rules_invalid", lambda sample, *, now_ns: None),
            ("health_rules_failed", lambda sample, *, now_ns: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        for reason, evaluator in cases:
            with mock.patch.object(plugin_api_module, "health_evaluator", lambda e=evaluator: e):
                status = self.port.status()
            self.assertEqual(status["data_health"], "unknown")
            self.assertEqual(status["diagnostics"]["reason_code"], reason)

    def test_health_rules_are_optional_at_import_time(self):
        """B00 merges separately; a missing rules package must not break the port."""
        self.assertTrue(callable(plugin_api_module.health_evaluator))
        result = plugin_api_module.health_evaluator()
        self.assertTrue(result is None or callable(result))


class DiagnosticsPolicyContractTest(unittest.TestCase):
    def test_legacy_manifest_normalises_to_off(self):
        ports = parse_ports({"ports": [{"id": "input", "direction": "subscribe",
                                       "message_types": ["std_msgs/msg/String"]}]})
        self.assertEqual(ports[0]["diagnostics"], DIAGNOSTICS_DEFAULTS)
        self.assertEqual(ports[0]["queue"]["capacity"], 1)
        self.assertEqual(ports[0]["queue"]["overflow"], "keep_latest")
        self.assertEqual(ports[0]["queue"]["max_age_ms"], 0)

    def test_periodic_is_accepted_for_subscriptions(self):
        ports = parse_ports({"ports": [SUBSCRIPTION]})
        self.assertEqual(ports[0]["diagnostics"]["mode"], "periodic")
        self.assertIsInstance(ports[0]["diagnostics"]["startup_grace_sec"], float)

    def test_periodic_is_rejected_for_publishers(self):
        with self.assertRaises(ValueError):
            parse_ports({"ports": [{"id": "output", "direction": "publish",
                                    "message_types": ["std_msgs/msg/String"],
                                    "diagnostics": {"mode": "periodic"}}]})

    def test_unknown_mode_and_fields_are_rejected(self):
        for raw in ({"mode": "always"}, {"mode": "off", "interval_sec": 1},
                    {"mode": "off", "startup_grace_sec": 10, "extra": True}):
            with self.assertRaises(ValueError):
                diagnostics_policy(raw)

    def test_non_finite_boolean_and_out_of_range_values_are_rejected(self):
        bad = [
            {"startup_grace_sec": float("nan")}, {"startup_grace_sec": float("inf")},
            {"startup_grace_sec": True}, {"startup_grace_sec": "10"},
            {"startup_grace_sec": -0.1}, {"startup_grace_sec": 300.1},
            {"stale_after_sec": float("nan")}, {"stale_after_sec": False},
            {"stale_after_sec": -1}, {"stale_after_sec": 0.1}, {"stale_after_sec": 300.5},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=repr(raw)):
                diagnostics_policy(raw)

    def test_inclusive_boundaries(self):
        low = diagnostics_policy({"startup_grace_sec": 0, "stale_after_sec": 0.2})
        self.assertEqual((low["startup_grace_sec"], low["stale_after_sec"]), (0.0, 0.2))
        high = diagnostics_policy({"startup_grace_sec": 300, "stale_after_sec": 300})
        self.assertEqual((high["startup_grace_sec"], high["stale_after_sec"]), (300.0, 300.0))

    def test_diagnostics_cannot_be_smuggled_through_bindings(self):
        ports = parse_ports({"ports": [SUBSCRIPTION]})
        with self.assertRaises(ValueError):
            normalize_bindings(ports, {"input": {"diagnostics": {"mode": "periodic"}}})
        with self.assertRaises(ValueError):
            normalize_bindings(ports, {"input": {"mode": "periodic"}})

    def test_binding_whitelist_is_unchanged(self):
        ports = parse_ports({"ports": [SUBSCRIPTION]})
        binding = normalize_bindings(ports, {"input": {"enabled": True, "topic": "/x",
                                                       "message_type": "std_msgs/msg/String",
                                                       "qos": {"preset": "sensor_data"}}})["input"]
        self.assertEqual(set(binding), {"enabled", "topic", "message_type", "qos"})
        self.assertNotIn("diagnostics", binding)

    def test_control_outputs_still_require_volatile_and_finite_ttl(self):
        with self.assertRaises(ValueError):
            parse_ports({"ports": [{"id": "control", "direction": "publish",
                                    "message_types": ["std_msgs/msg/String"],
                                    "execution_lane": "control",
                                    "queue": {"max_age_ms": 0}}]})
        ports = parse_ports({"ports": [{"id": "control", "direction": "publish",
                                       "message_types": ["std_msgs/msg/String"],
                                       "execution_lane": "control"}]})
        self.assertEqual(ports[0]["queue"]["max_age_ms"], 200)
        with self.assertRaises(ValueError):
            normalize_bindings(ports, {"control": {"qos": {"durability": "transient_local"}}})

    def test_diagnostics_never_changes_queue_semantics(self):
        plain = parse_ports({"ports": [{k: v for k, v in SUBSCRIPTION.items() if k != "diagnostics"}]})
        monitored = parse_ports({"ports": [SUBSCRIPTION]})
        self.assertEqual(plain[0]["queue"], monitored[0]["queue"])
        self.assertEqual(plain[0]["qos_preset"], monitored[0]["qos_preset"])


if __name__ == "__main__":
    unittest.main()
