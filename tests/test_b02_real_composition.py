from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.decision.catalog import CapabilityCatalog
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus


class RealCompositionActionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        plugin_root = root / "plugins" / "control" / "real_action"
        plugin_root.mkdir(parents=True)
        (plugin_root / "guide.md").write_text("mock controller stop guide", encoding="utf-8")
        (plugin_root / "plugin.json").write_text(json.dumps({
            "id": "real_action", "name": "Real Action", "version": "1.0.0",
            "entry": "main.py", "provides": ["action_owner"], "enabled_default": True,
            "action_api_version": 2, "observation_guide": "guide.md",
            "actions": [{"action_id": "real_action.move.v2", "description": "Move",
                         "schema": {"type": "object", "properties": {}, "additionalProperties": False},
                         "resources": ["mock_motor"], "operations": ["start", "cancel"],
                         "cancel_timeout_ms": 200, "max_duration_ms": 1000}],
        }), encoding="utf-8")
        (plugin_root / "main.py").write_text(
            "from astrbot_ex.core.actions.ledger import StopEvidence\n"
            "class Plugin:\n"
            "    def __init__(self, context):\n"
            "        self.context = context\n"
            "        self.started = []\n"
            "        self.stopped = []\n"
            "    def on_action_command(self, command):\n"
            "        self.started.append(command.command_id)\n"
            "        return 'accepted'\n"
            "    def on_action_cancel(self, command_id, reason):\n"
            "        self.stopped.append(command_id)\n"
            "        self.context.actions.report(command_id, 'canceled',\n"
            "            stop_evidence=StopEvidence(command_id, True, 'mock-controller', 'parked-' + command_id))\n"
            "        return 'requested'\n",
            encoding="utf-8")
        self.event_bus = EventBus()
        self.topic_bus = TopicBus()
        self.registry = PluginRegistry()
        self.ledger = ActionLedger(root / "actions.sqlite3")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.catalog = CapabilityCatalog()
        self.service = ActionService(self.ledger, self.dispatcher, self.catalog, stop_timeout=1.0)
        self.registry.set_action_lifecycle_guard(self.service.prove_owner_stop)
        self.manager = LocalPluginManager(
            plugins_root=root / "plugins", state_path=root / "plugins_state.json",
            registry=self.registry, event_bus=self.event_bus, topic_bus=self.topic_bus,
            action_dispatcher=self.dispatcher, capability_catalog=self.catalog)
        self.manager.discover()
        self.manager.load_enabled()
        self.service.update_versions(config_revision=1, environment_revision=1, runtime_state="ready")
        self.record = self.manager.records["real_action"]
        self.binding = self.record.action_binding
        self.assertIsNotNone(self.binding)
        self.dispatcher.set_gate(True)
        self.dispatcher.update_context(
            ex_session="real-session", goal_id="real-goal", goal_revision=1,
            task_id="real-task", allowed_actions=["real_action.move.v2"],
            bound_params={"real_action.move.v2": {}}, runtime_state="ready",
            catalog_revision=self.catalog.snapshot().revision, config_revision=1,
            environment_revision=1, ttl_ms=10_000)
        self.addCleanup(self.close)

    def close(self):
        try:
            if self.registry.get("real_action") is not None:
                self.registry.set_action_lifecycle_guard(lambda slot, reason: True)
                self.registry.unregister("real_action")
        finally:
            self.service.close()
            self.temp.cleanup()

    def command(self, command_id):
        return {
            "schema_version": 1, "command_id": command_id,
            "ex_session": "real-session", "goal_id": "real-goal", "goal_revision": 1,
            "decision_id": "real-decision", "owner": "real_action", "plugin_generation": self.binding.generation,
            "action_id": "real_action.move.v2", "operation": "start", "params": {}, "lease_ms": 900,
        }

    def wait_status(self, command_id, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = self.dispatcher.query(command_id).result(1)
            if snapshot is not None and snapshot.status == status:
                return snapshot
            time.sleep(0.01)
        self.fail(f"{command_id} did not reach {status}: {snapshot}")

    def test_real_owner_start_cancel_proof_and_resource_release(self):
        first = self.dispatcher.start(self.command("real-1")).result(3)
        self.assertEqual(first.status, "admitted")
        self.wait_status("real-1", "accepted")
        self.assertTrue(self.service.stop_actions("real stop"))
        canceled = self.wait_status("real-1", "canceled")
        self.assertEqual(canceled.held_resources, ())
        self.assertEqual(self.record.plugin.started, ["real-1"])
        self.assertEqual(self.record.plugin.stopped, ["real-1"])

    def test_disable_enable_requires_fresh_context_and_old_command_does_not_resume(self):
        self.dispatcher.start(self.command("old-queued")).result(3)
        self.wait_status("old-queued", "accepted")
        self.assertTrue(self.service.stop_actions("disable action"))
        self.dispatcher.review_stops().result(3)
        original_generation = self.record.action_binding.generation
        self.manager.set_enabled("real_action", False)
        self.manager.set_enabled("real_action", True)
        # Disable/enable preserves the loaded owner generation; Dispatcher context
        # and gate, rather than an artificial persisted generation bump, revoke old work.
        self.assertEqual(self.record.action_binding.generation, original_generation)
        with self.assertRaises(Exception):
            self.dispatcher.start(self.command("old-context"))
        self.dispatcher.update_versions(catalog_revision=self.catalog.snapshot().revision,
                                        config_revision=2, environment_revision=1, runtime_state="ready")
        self.dispatcher.set_gate(True)
        self.dispatcher.update_context(
            ex_session="fresh-session", goal_id="fresh-goal", goal_revision=2,
            task_id="fresh-task", allowed_actions=["real_action.move.v2"],
            bound_params={"real_action.move.v2": {}}, runtime_state="ready",
            catalog_revision=self.catalog.snapshot().revision, config_revision=2,
            environment_revision=1, ttl_ms=10_000)
        fresh = self.command("fresh")
        fresh.update(ex_session="fresh-session", goal_id="fresh-goal", goal_revision=2,
                     decision_id="fresh-decision", plugin_generation=self.record.action_binding.generation)
        self.dispatcher.start(fresh).result(3)
        self.wait_status("fresh", "accepted")
        self.assertEqual(self.record.plugin.started.count("old-queued"), 1)
        self.assertEqual(self.record.plugin.started.count("fresh"), 1)


class SnapshotRealCompositionTest(unittest.TestCase):
    def test_restore_stops_live_owner_but_never_replays_or_replaces_execution_facts(self):
        import io
        import os
        import zipfile
        from unittest.mock import patch
        from astrbot_ex.core.api_server import build_server
        from astrbot_ex.core.actions.models import ActionCommand

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_root = root / "plugins/control/snapshot_action"
            plugin_root.mkdir(parents=True)
            (plugin_root / "guide.md").write_text("Mock stop guide", encoding="utf-8")
            (plugin_root / "plugin.json").write_text(json.dumps({
                "id": "snapshot_action", "name": "Snapshot mock", "version": "1.0.0",
                "entry": "main.py", "provides": ["action_owner"], "enabled_default": True,
                "action_api_version": 2, "observation_guide": "guide.md",
                "actions": [{"action_id": "snapshot_action.move.v2", "description": "Mock",
                             "schema": {"type": "object", "properties": {}, "additionalProperties": False},
                             "resources": ["mock-resource"], "operations": ["start", "cancel"],
                             "cancel_timeout_ms": 1000, "max_duration_ms": 30_000}],
            }), encoding="utf-8")
            (plugin_root / "main.py").write_text(
                "from astrbot_ex.core.actions.ledger import StopEvidence\n"
                "class Plugin:\n"
                "    def __init__(self, context):\n"
                "        self.context = context\n"
                "        self.started = []\n"
                "    def on_action_command(self, command):\n"
                "        self.started.append(command.command_id)\n"
                "        return 'accepted'\n"
                "    def on_action_cancel(self, command_id, reason):\n"
                "        self.context.actions.report(command_id, 'canceled',\n"
                "            stop_evidence=StopEvidence(command_id, True, 'mock', 'parked-' + command_id))\n"
                "        return 'requested'\n", encoding="utf-8")
            with patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": temporary,
                                         "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
                server = build_server("127.0.0.1", 0, 20)
            try:
                created = server.snapshot_service.create()
                archive = server.snapshot_service.download_path(created["filename"]).read_bytes()
                with zipfile.ZipFile(io.BytesIO(archive)) as exported:
                    self.assertFalse(any("actions.sqlite3" in name for name in exported.namelist()))
                record = server.local_plugins.records["snapshot_action"]
                old_plugin = record.plugin
                old_actor = server.controller.runtime.registry.get("snapshot_action").actor
                binding = record.action_binding
                dispatcher, ledger, service = server.action_dispatcher, server.action_ledger, server.action_service
                server.controller.change_mode("decision")

                def authorize():
                    service.update_versions(config_revision=1, environment_revision=1, runtime_state="ready")
                    dispatcher.set_gate(True)
                    dispatcher.update_context(
                        ex_session="snapshot-session", goal_id="snapshot-goal", goal_revision=1,
                        task_id="snapshot-task", allowed_actions=["snapshot_action.move.v2"],
                        bound_params={"snapshot_action.move.v2": {}}, runtime_state="ready",
                        catalog_revision=server.capability_catalog.snapshot().revision,
                        config_revision=1, environment_revision=1, ttl_ms=30_000)

                def raw(name):
                    return {"schema_version": 1, "command_id": name, "ex_session": "snapshot-session",
                            "goal_id": "snapshot-goal", "goal_revision": 1, "decision_id": "snapshot-decision",
                            "owner": binding.owner, "plugin_generation": binding.generation,
                            "action_id": "snapshot_action.move.v2", "operation": "start", "params": {},
                            "lease_ms": 30_000}

                def wait(name, status):
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        current = ledger.get(name).result(2)
                        if current is not None and current.status == status:
                            return current
                        time.sleep(0.005)
                    self.fail(f"{name} did not reach {status}")

                authorize()
                for name in ("completed", "uncertain", "running"):
                    service.start(raw(name)).result(3)
                    wait(name, "accepted")
                    if name == "completed":
                        old_plugin.context.actions.report(name, "succeeded").result(3)
                    elif name == "uncertain":
                        old_plugin.context.actions.report(name, "unknown").result(3)
                        self.assertTrue(service.stop_actions("Mock proof"))
                        dispatcher.review_stops().result(3)
                        authorize()
                    else:
                        old_plugin.context.actions.report(name, "running").result(3)
                completed = ledger.get("completed").result(3)
                uncertain = ledger.get("uncertain").result(3)
                proof = ledger.stop_proof("uncertain", binding).result(3)
                self.assertIsNotNone(proof)
                self.assertEqual(uncertain.status, "unknown")
                self.assertEqual(uncertain.held_resources, ())
                self.assertEqual(ledger.get("running").result(3).held_resources, ("mock-resource",))
                events = ledger.events(limit=100).result(3)
                ledger.ack(events[0].event_seq).result(3)
                events = ledger.events(limit=100).result(3)
                server.snapshot_service.restore_upload("snapshot.zip", archive)
                self.assertIs(server.action_ledger, ledger)
                self.assertFalse(ledger.health.closed)
                self.assertEqual(ledger.get("completed").result(3), completed)
                self.assertEqual(ledger.get("uncertain").result(3), uncertain)
                self.assertEqual(ledger.stop_proof("uncertain", binding).result(3), proof)
                self.assertEqual(ledger.events(limit=100).result(3)[:len(events)], events)
                self.assertTrue(ledger.events(limit=100).result(3)[0].acknowledged)
                self.assertTrue(all(event.to_action_event().task_id == "snapshot-task"
                                    for event in ledger.events(limit=100).result(3)))
                self.assertEqual(wait("running", "canceled").held_resources, ())
                self.assertIsNotNone(ledger.stop_proof("running", binding).result(3))
                self.assertFalse(service.status()["gate_open"])
                self.assertEqual(server.controller.runtime.state.value, "idle")
                self.assertFalse(old_actor.alive)
                fresh_plugin = server.local_plugins.records["snapshot_action"].plugin
                self.assertEqual(old_plugin.started, ["completed", "uncertain", "running"])
                self.assertEqual(fresh_plugin.started, [])
                for name in old_plugin.started:
                    with self.assertRaises(RuntimeError):
                        service.start(raw(name))
                    self.assertFalse(ledger.admit(ActionCommand.parse(raw(name)), ("mock-resource",), binding,
                                                  task_id="snapshot-task").result(3).admitted_new)
                self.assertEqual(fresh_plugin.started, [])
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
