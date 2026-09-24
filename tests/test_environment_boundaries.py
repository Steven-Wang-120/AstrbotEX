from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_environments import FakeAdapter, Message, PORTS, finish
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.models import EnvironmentBusyError
from astrbot_ex.core.environments.plugin_api import PluginRosFacade
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus


class EnvironmentBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events, self.bus = EventBus(), TopicBus()
        self.manager = EnvironmentManager(data_root=self.temp.name, event_bus=self.events,
            topic_bus=self.bus, adapter_factory=lambda mode, config: FakeAdapter())
        self.facade = PluginRosFacade(plugin_id='test', environment_manager=self.manager,
            ports=PORTS, bindings={'output': {'enabled': True}, 'control': {'enabled': True}})

    def tearDown(self):
        self.manager.runtime_running = lambda: False
        self.manager.close()
        self.temp.cleanup()

    def test_late_callback_from_old_binding_is_rejected_in_same_environment(self):
        incoming = self.facade.subscribe('input')
        finish(self.manager, 'ros2')
        generation = incoming._port.generation
        epoch = incoming._port.binding_generation
        bindings = copy.deepcopy(self.facade.bindings_config)
        bindings['input']['topic'] = '/replacement'
        self.facade.reconfigure(bindings)
        incoming._port.receive(Message(), generation, epoch)
        self.assertIsNone(incoming.get_nowait())
        incoming._port.receive(Message(), generation, incoming._port.binding_generation)
        self.assertIsNotNone(incoming.get_nowait())

    def test_oversized_messages_are_rejected_and_queue_bytes_clear_on_switch(self):
        publisher = self.facade.publisher('output')
        publisher._port.declaration['queue'].update(max_message_bytes=1024, max_bytes=1024)
        finish(self.manager, 'ros2')
        message = Message()
        message.data = b'x' * 2048
        self.assertEqual(publisher.publish(message).status, 'rejected_size')
        self.assertEqual(publisher.status()['oversized'], 1)
        self.assertEqual(publisher.publish(Message()).status, 'queued')
        self.assertGreater(publisher.status()['queue_bytes'], 0)
        finish(self.manager, 'normal')
        self.assertEqual(publisher.status()['queue_bytes'], 0)

    def test_save_failure_keeps_ros_resource_and_does_not_claim_normal(self):
        publisher = self.facade.publisher('output')
        finish(self.manager, 'ros2')
        native = publisher._port.native
        with patch.object(self.manager, '_save_config', side_effect=OSError('disk full')):
            self.manager.select('normal')
            self.manager._worker.join(2)
        self.assertEqual(self.manager.snapshot()['phase'], 'failed')
        self.assertEqual(self.manager.snapshot()['active_mode'], 'ros2')
        self.assertIs(publisher._port.native, native)
        self.assertFalse(self.manager.accepting)
        self.assertEqual(json.loads(self.manager.config_path.read_text())['selected_mode'], 'ros2')

    def test_runtime_control_cannot_be_rebound(self):
        publisher = self.facade.publisher('control')
        finish(self.manager, 'ros2')
        self.manager.runtime_running = lambda: True
        bindings = copy.deepcopy(self.facade.bindings_config)
        bindings['control']['topic'] = '/new_control'
        with self.assertRaises(EnvironmentBusyError):
            self.facade.reconfigure(bindings)
        self.assertEqual(publisher.status()['topic'], '/control')

    def test_deactivation_hook_runs_on_actor_and_gets_only_stop_channel(self):
        facade, records = self.facade, []
        class Plugin:
            id = 'test'
            _astrbotex_ros = facade
            def on_load(self):
                self.output = facade.publisher('control')
            def on_environment_deactivating(self, environment_id, reason):
                records.append((threading.current_thread().name, environment_id))
                records.append(self.output.publish(Message()).status)
                records.append(self.output.publish_stop(Message()).status)
                # Fake adapter has no ROS execution lane.
                self.output._port.flush_one()
        registry = PluginRegistry()
        plugin = Plugin()
        registry.register('trace_plugin', plugin)
        try:
            finish(self.manager, 'ros2')
            self.manager.runtime_running = lambda: True
            self.assertEqual(plugin.output.publish_stop(Message()).status, 'stop_not_authorized')
            native = plugin.output._port.native
            finish(self.manager, 'normal')
            self.assertEqual(records[0], ('astrbotex-plugin-test', 'ros2'))
            self.assertEqual(records[1:], ['unavailable', 'queued'])
            self.assertEqual(len(native.published), 1)
        finally:
            registry.unregister('test')

    def test_quiesce_timeout_preserves_path_and_revokes_late_stop_permission(self):
        facade, gate, results = self.facade, threading.Event(), []
        class Plugin:
            id = 'test'
            _astrbotex_ros = facade
            def on_load(self):
                self.output = facade.publisher('control')
            def on_environment_deactivating(self, environment_id, reason):
                gate.wait(1)
                results.append(self.output.publish_stop(Message()).status)
        registry, plugin = PluginRegistry(), Plugin()
        registry.register('trace_plugin', plugin)
        try:
            finish(self.manager, 'ros2')
            self.manager.runtime_running = lambda: True
            self.manager.quiesce_timeout = 0.03
            self.manager.select('normal')
            self.manager._worker.join(1)
            self.assertEqual(self.manager.snapshot()['last_error']['code'], 'quiesce_failed')
            self.assertEqual(self.manager.snapshot()['active_mode'], 'ros2')
            self.assertTrue(plugin.output.status()['resource_created'])
            with self.assertRaises(EnvironmentBusyError):
                self.manager.select('ros2')
            gate.set()
            plugin._astrbotex_ros.actor.call('on_disable')
            self.assertEqual(results, ['stop_not_authorized'])
        finally:
            gate.set()
            registry.unregister('test')

    def test_deployment_locks_are_visible_and_reject_conflicting_config(self):
        with patch.dict(os.environ, {'ROS_DOMAIN_ID': '37'}):
            status = self.manager.status()
            self.assertEqual(status['effective_config']['domain_id'], 37)
            self.assertEqual(status['locked_config']['domain_id']['source'], 'ROS_DOMAIN_ID')
            with self.assertRaises(ValueError):
                self.manager.configure_ros2({'domain_id': 12})

    def test_restored_selected_mode_rebinds_without_starting_runtime(self):
        finish(self.manager, 'ros2')
        self.manager.reset_after_restore()
        self.manager.reload()
        self.manager.restore_selected_mode()
        self.manager._worker.join(2)
        self.assertEqual(self.manager.snapshot()['active_mode'], 'ros2')
        self.assertFalse(self.manager.runtime_running())
        self.assertIs(self.manager.topic_bus, self.bus)

    def test_structural_events_are_not_filtered(self):
        for name in ('environment_changed', 'ros_graph_changed', 'ros_endpoints_changed'):
            self.events.emit(name, 'snapshot available')
        self.assertEqual([e.type for e in self.events.recent()],
                         ['environment_changed', 'ros_graph_changed', 'ros_endpoints_changed'])


if __name__ == '__main__':
    unittest.main()
