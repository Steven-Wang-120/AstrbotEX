"""Real rules + real ports + real coordinator; only ROS transport is fake."""
import copy
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from astrbot_ex.core.diagnostics.service import DiagnosticsService
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.plugin_api import PluginRosFacade
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus
from tests.test_environments import FakeAdapter, Message, finish
from tests.test_diagnostic_rules import sample


class Clock:
    def __init__(self): self.ns = 10_000_000_000
    def __call__(self): return self.ns
    def advance(self, seconds): self.ns += round(seconds * 1e9)


def preflight():
    return {"schema_version": 1, "platform": "linux", "supported": True,
            "partial": False, "checks": [], "runtime": {}, "interfaces": []}


class DiagnosticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.clock_patch = patch('astrbot_ex.core.environments.plugin_api.time.monotonic_ns', self.clock)
        self.clock_patch.start()
        self.running = False
        self.bus = EventBus()
        clock = self.clock
        class Adapter(FakeAdapter):
            def attach(self, port, generation):
                super().attach(port, generation)
                if port.native is not None:
                    port.binding_generation += 1
                    port.mark_bound(clock())
                    port.graph_peers = port.matched = 1
        self.manager = EnvironmentManager(data_root=self.temp.name, event_bus=self.bus, topic_bus=TopicBus(),
                                          adapter_factory=lambda mode, config: Adapter())
        self.manager.runtime_running = lambda: self.running
        declaration = {"id": "image", "direction": "subscribe", "message_types": ["std_msgs/msg/String"],
                       "default_topic": "/camera", "diagnostics": {"mode": "periodic", "startup_grace_sec": 10, "stale_after_sec": 5}}
        self.facade = PluginRosFacade(plugin_id='camera', environment_manager=self.manager, ports=[declaration])
        self.port = self.facade.subscribe('image')._port
        finish(self.manager, 'ros2')
        self.service = self.manager.start_diagnostics(clock_ns=self.clock, tick_sec=3600, preflight_collector=preflight)

    def tearDown(self):
        self.running = False
        try: self.manager.close()
        finally:
            self.clock_patch.stop()
            self.temp.cleanup()

    def activity(self, running, kind='runtime_state'):
        self.running = running
        self.bus.emit(kind, 'test lifecycle', state='running' if running else 'idle')

    def cycle(self):
        self.service._run_cycle()
        return self.service.snapshot()

    def fault(self):
        self.activity(True)
        self.cycle()
        self.clock.advance(10)
        return self.cycle()

    def notices(self, state=None):
        events = [event for event in self.bus.recent(1000) if event.type == 'ros_diagnostic']
        return [event for event in events if state is None or event.data.get('state') == state]

    def receive(self):
        self.port.receive(Message(), self.port.generation, self.port.binding_generation)

    def test_zero_callbacks_alert_once_then_two_samples_recover(self):
        snapshot = self.fault()
        issue = snapshot['active_issues'][0]
        self.assertEqual(issue['code'], 'ROS_RX_NO_DATA_WITH_PEER')
        self.assertEqual(self.port.status()['data_health'], 'no_data')
        self.cycle()
        self.assertEqual(len(self.notices('active')), 1)
        self.receive()
        self.cycle()
        self.assertEqual(len(self.notices('recovered')), 0)
        self.cycle()
        self.assertEqual(len(self.notices('recovered')), 1)
        self.assertEqual(self.notices('recovered')[0].data['scope'], issue['scope'])
        self.assertEqual(self.notices('recovered')[0].data['log']['level'], 'INFO')

    def test_unknown_facts_cannot_recover_rx_issue(self):
        self.fault()
        self.port.bound_monotonic_ns = None
        for _ in range(3): self.cycle()
        self.assertTrue(any(issue['code'] == 'ROS_RX_NO_DATA_WITH_PEER' for issue in self.service.snapshot()['active_issues']))
        self.assertEqual(len(self.notices('recovered')), 0)
        self.assertNotEqual(self.service.snapshot()['summary'], 'ok')

    def test_incomplete_sample_breaks_consecutive_recovery(self):
        self.fault()
        self.receive()
        self.cycle()
        collector = self.service._collector
        self.service._collector = Mock(side_effect=RuntimeError('no evidence'))
        self.cycle()
        self.service._collector = collector
        self.cycle()
        self.assertEqual(len(self.notices('recovered')), 0)
        self.cycle()
        rx_recoveries = [event for event in self.notices('recovered') if event.data.get('recovered_code') == 'ROS_RX_NO_DATA_WITH_PEER']
        self.assertEqual(len(rx_recoveries), 1)

    def test_fast_stop_start_and_fault_open_fresh_windows(self):
        self.fault()
        self.receive(); self.cycle(); self.cycle()
        old_epoch = self.port.monitoring_epoch
        self.activity(False)
        self.activity(True)
        snapshot = self.cycle()
        self.assertGreater(self.port.monitoring_epoch, old_epoch)
        self.assertEqual(snapshot['ports'][0]['data_health'], 'warming_up')
        self.activity(False, 'fault')
        self.assertEqual(self.cycle()['ports'][0]['data_health'], 'paused')

    def test_stop_withdraws_instead_of_recovering(self):
        self.fault()
        self.activity(False)
        self.cycle()
        self.assertEqual(len(self.notices('withdrawn')), 1)
        self.assertEqual(len(self.notices('recovered')), 0)
        self.assertEqual(self.service.snapshot()['summary'], 'paused')

    def test_rebinding_rejects_old_callbacks(self):
        self.fault()
        generation, binding = self.port.generation, self.port.binding_generation
        self.receive()
        config = copy.deepcopy(self.facade.bindings_config)
        config['image']['topic'] = '/replacement'
        self.facade.reconfigure(config)
        self.port.receive(Message(), generation, binding)
        self.cycle()
        self.assertEqual(self.port.status()['data_health'], 'warming_up')
        self.assertIsNone(self.port.last_receive_monotonic_ns)

    def test_diagnostic_close_failure_still_runs_stop_protocol(self):
        self.activity(False)
        finish(self.manager, 'normal')
        actions = Mock()
        actions.stop_actions.return_value = True
        self.manager.action_service = actions
        with patch.object(self.service, 'close', side_effect=RuntimeError('diagnostic join failed')):
            with self.assertRaisesRegex(RuntimeError, 'diagnostic join failed'):
                self.manager.close()
        actions.revoke.assert_called()
        actions.stop_actions.assert_called()


class DiagnosticRaceTests(unittest.TestCase):
    def test_invalidating_during_slow_preflight_discards_old_generation(self):
        entered, release = threading.Event(), threading.Event()
        old = {"session_id": "s", "generation": 1, "revision": 1, "active_mode": "ros2", "phase": "idle"}
        current = {"session_id": "s", "generation": 2, "revision": 2, "active_mode": "normal", "phase": "idle"}
        def slow():
            entered.set(); release.wait(2)
            return preflight()
        service = DiagnosticsService(lambda: {"environment": old, "runtime_running": True, "ports": [sample()], "complete": True},
                                     slow, EventBus(), clock_ns=lambda: 20_000_000_000)
        worker = threading.Thread(target=service._run_cycle)
        worker.start()
        self.assertTrue(entered.wait(1))
        service.invalidate(current)
        release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.snapshot()['environment']['generation'], 2)
        self.assertEqual(service.snapshot()['active_issues'], [])
        service.close()

    def test_twenty_manager_lifecycles_release_threads_and_subscriptions(self):
        baseline = {thread.ident for thread in threading.enumerate() if thread.name == 'diagnostics-service'}
        for _ in range(20):
            with tempfile.TemporaryDirectory() as root:
                bus = EventBus()
                manager = EnvironmentManager(data_root=root, event_bus=bus, topic_bus=TopicBus())
                manager.start_diagnostics(tick_sec=3600, preflight_collector=preflight)
                stream = bus.open_stream()
                stream.close()
                manager.close()
                self.assertEqual(bus._subscribers, [])
        self.assertEqual({thread.ident for thread in threading.enumerate() if thread.name == 'diagnostics-service'}, baseline)
