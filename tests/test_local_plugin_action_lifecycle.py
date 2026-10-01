from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.environments.models import EnvironmentRevisionConflict
from astrbot_ex.core.actions.plugin_api import PluginActionAPI

from astrbot_ex.core.decision.catalog import CapabilityCatalog
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus


class NoAddNoteError(Exception):
    __slots__ = ()

    def __getattribute__(self, name):
        if name == "add_note":
            raise AttributeError(name)
        return super().__getattribute__(name)


class MockDispatcher:
    def __init__(self):
        self.owners = {}
        self.reports = []
        self.removed = []

    def register_owner(self, binding, actor, manifest):
        self.owners[binding] = (actor, manifest)

    def remove_owner(self, binding):
        self.removed.append(binding)
        self.owners.pop(binding, None)

    def report(self, command_id, binding, status, **kwargs):
        result = Future()
        if command_id != "existing" or binding not in self.owners:
            result.set_exception(ValueError("unknown command or owner"))
        else:
            self.reports.append((command_id, binding, status, kwargs))
            result.set_result("accepted")
        return result


class LocalActionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.plugin_root = root / "plugins" / "control" / "test_action"
        self.plugin_root.mkdir(parents=True)
        self.manifest_path = self.plugin_root / "plugin.json"
        self.manifest = {
            "id": "test_action", "name": "Test Action", "version": "1.0.0",
            "entry": "main.py", "provides": ["action_owner"],
            "enabled_default": True, "action_api_version": 2,
            "observation_guide": "guide.md",
            "actions": [{"action_id": "test_action.check.v2", "description": "Check",
                         "schema": {"type": "object", "properties": {}}, "operations": ["start"]}],
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path = self.plugin_root / "guide.md"
        self.guide_path.write_text("guide one", encoding="utf-8")
        self.config_path = self.plugin_root / "config.json"
        self.config_path.write_bytes(b'{"rate": 1}\n')
        self.entry = self.plugin_root / "main.py"
        self.entry.write_text(
            "class Plugin:\n"
            "    def __init__(self, context):\n"
            "        self.context = context\n"
            "        self.events = []\n"
            "    def on_load(self):\n"
            "        self.events.append('load')\n"
            "        self.context.actions.report('existing', 'running').result(1)\n"
            "    def on_unload(self):\n"
            "        self.events.append('unload')\n", encoding="utf-8")
        self.registry = PluginRegistry()
        self.proof = True
        self.registry.set_action_lifecycle_guard(lambda slot, reason: self.proof)
        self.dispatcher = MockDispatcher()
        self.catalog = CapabilityCatalog()
        self.manager = LocalPluginManager(
            plugins_root=root / "plugins", state_path=root / "state.json",
            registry=self.registry, event_bus=EventBus(), topic_bus=TopicBus(),
            action_dispatcher=self.dispatcher, capability_catalog=self.catalog)
        self.manager.discover()
        self.manager.load_enabled()
        self.addCleanup(self.cleanup_owner)

    def cleanup_owner(self):
        if self.registry.get("test_action") is not None:
            self.registry.set_action_lifecycle_guard(lambda slot, reason: True)
            self.registry.unregister("test_action")

    def test_binding_before_load_and_reload_generation(self):
        old = self.manager.records["test_action"].plugin
        old_binding = self.manager.records["test_action"].action_binding
        self.assertEqual(old_binding.owner, "test_action")
        self.assertEqual(old_binding.generation, 1)
        self.assertEqual(self.dispatcher.reports[0][:3], ("existing", old_binding, "running"))
        self.assertEqual(self.registry.get("test_action").kind, "action")
        with self.assertRaisesRegex(ValueError, "unknown command"):
            old.context.actions.report("not-issued", "running").result(1)
        self.manager.update_config("test_action", {"rate": 2})
        new_binding = self.manager.records["test_action"].action_binding
        self.assertEqual(new_binding.generation, 2)
        self.assertIn(old_binding, self.dispatcher.removed)
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            old.context.actions.report("existing", "running")
        self.assertEqual(self.catalog.snapshot().entries[0]["generation"], 2)

    def test_blocked_config_disable_and_uninstall_preserve_owner(self):
        old = self.manager.records["test_action"].plugin
        binding = self.manager.records["test_action"].action_binding
        before = self.config_path.read_bytes()
        self.proof = False
        with self.assertRaisesRegex(RuntimeError, "blocked"):
            self.manager.update_config("test_action", {"rate": 2})
        self.assertEqual(self.config_path.read_bytes(), before)
        for operation in (lambda: self.manager.set_enabled("test_action", False),
                          lambda: self.manager.uninstall("test_action")):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                operation()
        record = self.manager.records["test_action"]
        self.assertIs(record.plugin, old)
        self.assertTrue(record.enabled)
        self.assertFalse(record.ros.closed)
        self.assertEqual(self.registry.get("test_action").state, "blocked")
        self.assertTrue(self.plugin_root.is_dir())
        self.assertIn(binding, self.dispatcher.owners)
        self.assertEqual(old.context.actions.report("existing", "running").result(1), "accepted")
        self.assertEqual(self.catalog.snapshot().executable(), ())

    def test_disable_enable_and_document_revision(self):
        start = self.catalog.snapshot()
        self.assertEqual(len(start.executable()), 1)
        self.guide_path.write_text("guide two", encoding="utf-8")
        self.assertEqual(self.catalog.snapshot().revision, start.revision)
        self.manager.refresh_capabilities()
        self.assertGreater(self.catalog.snapshot().revision, start.revision)
        self.assertEqual(self.catalog.snapshot().entries[0]["guide"]["text"], "guide two")
        self.manifest_path.write_text(json.dumps({**self.manifest, "version": "2.0.0"}), encoding="utf-8")
        self.manager.refresh_capabilities()
        self.assertEqual(self.catalog.snapshot().entries[0]["unavailable_reason"], "version_changed")
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.manager.set_enabled("test_action", False)
        self.assertEqual(self.catalog.snapshot().entries[0]["unavailable_reason"], "disabled")
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.manager.set_enabled("test_action", True)
        self.assertEqual(self.registry.get("test_action").generation, 1)
        self.assertEqual(len(self.catalog.snapshot().executable()), 1)

    def test_starting_and_directory_drift_are_not_executable(self):
        slot = self.registry.get("test_action")
        initial = self.catalog.snapshot()
        slot.state = "starting"
        self.manager.refresh_capabilities()
        self.assertEqual(self.catalog.snapshot().entries[0]["unavailable_reason"], "starting")
        self.assertEqual(self.catalog.snapshot().executable(), ())
        slot.state = "ready"
        self.config_path.write_bytes(b'{"rate": 99}\n')
        self.manager.refresh_capabilities()
        entry = self.catalog.snapshot().entries[0]
        self.assertGreater(self.catalog.snapshot().revision, initial.revision)
        self.assertEqual(entry["unavailable_reason"], "config_changed")
        self.assertEqual(entry["config"], {"rate": 1})
        self.assertEqual(slot.plugin.context.config, {"rate": 1})
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.config_path.write_bytes(b'{"rate": 1}\n')
        changed = json.loads(json.dumps(self.manifest))
        changed["actions"][0]["schema"]["properties"]["target"] = {"type": "string"}
        self.manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        self.manager.refresh_capabilities()
        entry = self.catalog.snapshot().entries[0]
        self.assertEqual(entry["version"], "1.0.0")
        self.assertEqual(entry["unavailable_reason"], "manifest_changed")
        self.assertEqual(entry["manifest"]["actions"][0]["schema"]["properties"], {})
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide fresh", encoding="utf-8")
        self.manager.refresh_capabilities()
        self.assertEqual(self.catalog.snapshot().entries[0]["guide"]["text"], "guide fresh")
        self.assertEqual(len(self.catalog.snapshot().executable()), 1)

    def test_action_ros_reload_and_failed_proof(self):
        old = self.manager.records["test_action"].plugin
        old_ros = old.context.ros
        before = self.config_path.read_bytes()
        old_binding = self.manager.records["test_action"].action_binding
        with self.assertRaises(EnvironmentRevisionConflict):
            self.manager.update_ros2("test_action", {}, 1)
        with self.assertRaises(ValueError):
            self.manager.update_ros2("test_action", {"unknown": {}}, 0)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.proof = False
        with self.assertRaisesRegex(RuntimeError, "blocked"):
            self.manager.update_ros2("test_action", {}, 0)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIs(self.manager.records["test_action"].plugin, old)
        self.assertIs(self.manager.records["test_action"].ros, old_ros)
        self.assertFalse(old_ros.closed)
        self.assertEqual(self.manager.get_ros2("test_action")["revision"], 0)
        self.assertEqual(self.manager.records["test_action"].action_binding, old_binding)
        self.assertEqual(self.registry.get("test_action").generation, 1)
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.proof = True
        result = self.manager.update_ros2("test_action", {}, 0)
        self.assertEqual(result["ros2"]["revision"], 1)
        self.assertEqual(self.registry.get("test_action").generation, 2)
        self.assertTrue(old_ros.closed)
        self.assertEqual(len(self.catalog.snapshot().executable()), 1)

    def test_action_ros_reload_with_existing_resource_handle(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        declaration = {"id": "output", "direction": "publish",
                       "message_types": ["std_msgs/msg/String"], "default_topic": "/output",
                       "execution_lane": "control"}
        self.manifest_path.write_text(json.dumps({**self.manifest,
            "ros2": {"ports": [declaration]}}), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text(
            "class Plugin:\n"
            "    def __init__(self, context):\n"
            "        self.context = context\n"
            "        self.publisher = context.ros.publisher('output')\n",
            encoding="utf-8")
        self.manager.discover()
        self.manager.load_enabled()
        old = self.manager.records["test_action"].plugin
        old_ros = old.context.ros
        old_generation = self.registry.get("test_action").generation
        before = self.config_path.read_bytes() if self.config_path.exists() else None
        old.publisher._port.native = object()
        self.proof = False
        with patch.object(old_ros, "validate_reconfigure", side_effect=AssertionError("old resource touched")):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                self.manager.update_ros2("test_action", {"output": {"topic": "/new"}}, 0)
            self.assertIs(self.manager.records["test_action"].plugin, old)
            self.assertFalse(old_ros.closed)
            self.assertEqual(self.config_path.read_bytes() if self.config_path.exists() else None, before)
            self.proof = True
            result = self.manager.update_ros2("test_action", {"output": {"topic": "/new"}}, 0)
        self.assertEqual(result["ros2"]["bindings"]["output"]["topic"], "/new")
        self.assertEqual(self.registry.get("test_action").generation, old_generation + 1)
        self.assertTrue(old_ros.closed)
        self.assertIsNot(self.manager.records["test_action"].ros, old_ros)

    def test_action_ros_reload_failure_restores_original_config(self):
        old = self.manager.records["test_action"].plugin
        before = self.config_path.read_bytes()
        self.entry.write_text('raise RuntimeError("new ROS load failed")\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config reload failed"):
            self.manager.update_ros2("test_action", {}, 0)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIsNone(self.registry.get("test_action"))
        self.assertTrue(old.context.ros.closed)
        self.assertEqual(self.manager.get_ros2("test_action")["revision"], 0)
        self.assertEqual(self.catalog.snapshot().executable(), ())

    def test_reload_load_failure_restores_config_without_restart(self):
        old = self.manager.records["test_action"].plugin
        before = self.config_path.read_bytes()
        self.entry.write_text('raise RuntimeError("new load failed")\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config reload failed"):
            self.manager.update_config("test_action", {"rate": 2})
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIn("unload", old.events)
        self.assertIsNone(self.registry.get("test_action"))
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.assertIn("config reload failed", self.manager.records["test_action"].error)

    def test_partial_dispatcher_registration_failure_cleans_owner(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text("class Plugin:\n    pass\n", encoding="utf-8")
        self.manager.discover()
        register = self.dispatcher.register_owner

        def partial_register(binding, actor, manifest):
            register(binding, actor, manifest)
            raise RuntimeError("dispatcher partially registered")

        with patch.object(self.dispatcher, "register_owner", side_effect=partial_register):
            self.manager.load_enabled()
        record = self.manager.records["test_action"]
        self.assertIn("dispatcher partially registered", record.error)
        self.assertIsNone(self.registry.get("test_action"))
        self.assertFalse(self.dispatcher.owners)
        self.assertIsNone(record.action_binding)
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            record.plugin._astrbotex_context.actions.report("existing", "running")

    def test_partial_binding_failure_keeps_blocked_instance_stoppable(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text("class Plugin:\n    pass\n", encoding="utf-8")
        self.manager.discover()
        self.proof = False
        with patch.object(PluginActionAPI, "_bind", side_effect=RuntimeError("facade bind failed")):
            self.manager.load_enabled()
        record = self.manager.records["test_action"]
        slot = self.registry.get("test_action")
        self.assertIn("facade bind failed", record.error)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.state, "blocked")
        self.assertIs(slot.plugin, record.plugin)
        self.assertFalse(record.ros.closed)
        self.assertFalse(self.dispatcher.owners)
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.proof = True
        self.manager.uninstall("test_action")
        self.assertIsNone(self.registry.get("test_action"))

    def test_partial_dispatcher_cleanup_retry_preserves_original_error(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text("class Plugin:\n    pass\n", encoding="utf-8")
        self.manager.discover()
        register = self.dispatcher.register_owner
        remove = self.dispatcher.remove_owner
        attempts = []

        def partial_register(binding, actor, manifest):
            register(binding, actor, manifest)
            raise RuntimeError("original registration error")

        def flaky_remove(binding):
            attempts.append(binding)
            if len(attempts) == 1:
                raise NoAddNoteError("transient remove error")
            remove(binding)

        with patch.object(self.dispatcher, "register_owner", side_effect=partial_register), patch.object(
                self.dispatcher, "remove_owner", side_effect=flaky_remove):
            self.manager.load_enabled()
        record = self.manager.records["test_action"]
        self.assertIn("original registration error", record.error)
        self.assertIn("transient remove error", record.error)
        self.assertFalse(self.dispatcher.owners)
        self.assertIsNone(record.action_binding)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.registry.get("test_action"))

    def test_failed_on_load_without_proof_retains_blocked_slot(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text('class Plugin:\n    def on_load(self):\n        raise RuntimeError("load failed")\n', encoding="utf-8")
        self.manager.discover()
        self.proof = False
        self.manager.load_enabled()
        record = self.manager.records["test_action"]
        slot = self.registry.get("test_action")
        self.assertIsNotNone(slot)
        self.assertIs(record.plugin, slot.plugin)
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(record.loaded)
        self.assertFalse(record.ros.closed)
        self.assertEqual(self.catalog.snapshot().executable(), ())
        self.assertEqual(self.catalog.snapshot().entries[0]["unavailable_reason"], "blocked")

    def test_failed_on_load_releases_bound_owner_after_proven_teardown(self):
        self.manager.uninstall("test_action")
        self.plugin_root.mkdir(parents=True)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.guide_path.write_text("guide", encoding="utf-8")
        self.entry.write_text('class Plugin:\n    def on_load(self):\n        raise RuntimeError("load failed")\n', encoding="utf-8")
        self.manager.discover()
        self.manager.load_enabled()
        self.assertIsNone(self.registry.get("test_action"))
        self.assertEqual(self.dispatcher.owners, {})
        self.assertEqual(self.catalog.snapshot().executable(), ())


if __name__ == "__main__":
    unittest.main()
