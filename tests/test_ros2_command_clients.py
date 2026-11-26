"""Framework-owned command clients: asynchronous protocol evidence and epochs."""
from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_ex.core.environments.contracts import parse_ports, normalize_bindings
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.plugin_api import PluginRosFacade, RosUnavailableError
from astrbot_ex.core.environments.ros2.adapter import Ros2EnvironmentAdapter, _subscription_callback
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus
from tests.test_environments import finish
from tests.test_ros2_port_health import FakeTime


class Goal:
    def __init__(self): self.positions = [0.1, 0.2, 0.3]


class Request:
    def __init__(self): self.reason = 'operator_stop'


Action = SimpleNamespace(Goal=Goal)
Service = SimpleNamespace(Request=Request)


class NativeGoal:
    def __init__(self, goal_id, accepted=True):
        self.accepted = accepted
        self.goal_id = goal_id
        self.result_future = Future()
        self.cancel_future = Future()
        self.cancel_calls = 0

    def get_result_async(self): return self.result_future

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return self.cancel_future


class NativeAction:
    def __init__(self, *args, **kwargs):
        self.ready = True
        self.sent = []
        self.destroyed = False

    def server_is_ready(self): return self.ready

    def send_goal_async(self, request, *, goal_uuid, feedback_callback):
        item = SimpleNamespace(request=request, goal_id=goal_uuid,
                               feedback=feedback_callback, future=Future())
        self.sent.append(item)
        return item.future

    def destroy(self): self.destroyed = True


class NativeService:
    def __init__(self):
        self.ready = True
        self.sent = []

    def service_is_ready(self): return self.ready

    def call_async(self, request):
        item = SimpleNamespace(request=request, future=Future())
        self.sent.append(item)
        return item.future


PORTS = [
    {'id': 'arm', 'direction': 'action', 'message_types': ['jetmax_control/action/MoveJoints'],
     'default_topic': '/jetmax_control/move_joints', 'response_timeout_sec': 30},
    {'id': 'stop', 'direction': 'service', 'message_types': ['jetmax_control/srv/Stop'],
     'default_topic': '/jetmax_control/stop', 'safety_stop': True},
]


class CommandAdapter:
    def __init__(self): self.ports = []
    def start(self): pass

    def attach(self, port, generation):
        if not port.binding['enabled']: return
        action = port.declaration['direction'] == 'action'
        port.native = NativeAction() if action else NativeService()
        port.message_type = Action if action else Service
        port.generation = generation
        port.binding_generation += 1
        port.mark_bound()
        port.state = 'ready'
        self.ports.append(port)

    def detach(self, port):
        if port in self.ports: self.ports.remove(port)
        port.unavailable()

    def close(self, reason):
        for port in list(self.ports): self.detach(port)

    def status(self): return {'health': 'ok'}
    def graph(self): return {'topics': [], 'nodes': []}


class CommandClientsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = EnvironmentManager(data_root=self.temp.name, event_bus=EventBus(),
            topic_bus=TopicBus(), adapter_factory=lambda mode, config: CommandAdapter())
        self.facade = PluginRosFacade(plugin_id='robot', environment_manager=self.manager,
            ports=PORTS, bindings={'arm': {'enabled': True}, 'stop': {'enabled': True}})
        self.arm = self.facade.action_client('arm')
        self.stop = self.facade.service_client('stop')
        finish(self.manager, 'ros2')
        self.manager.runtime_running = lambda: True
        self.uuid_patch = patch('astrbot_ex.core.environments.ros2.interfaces.goal_uuid_message',
                                side_effect=lambda value: SimpleNamespace(uuid=list(bytes.fromhex(value))))
        self.uuid_patch.start()

    def tearDown(self):
        self.uuid_patch.stop()
        self.manager.runtime_running = lambda: False
        self.facade.close()
        self.manager.close()
        self.temp.cleanup()

    def send(self):
        op = self.arm.send_goal(self.arm.new_goal())
        self.assertEqual(op.snapshot()['state'], 'queued')
        self.arm._port.flush_one()
        native = self.arm._port.native
        item = native.sent[-1]
        self.assertEqual(op.snapshot()['state'], 'submitted')
        return op, item

    def accept(self):
        op, item = self.send()
        handle = NativeGoal(item.goal_id)
        item.future.set_result(handle)
        self.assertEqual(op.snapshot()['state'], 'accepted')
        return op, item, handle

    def test_declarations_are_typed_and_existing_topics_unchanged(self):
        parsed = parse_ports({'ports': PORTS})
        self.assertEqual(parsed[0]['execution_lane'], 'control')
        self.assertTrue(parsed[0]['requires_runtime_running'])
        self.assertFalse(normalize_bindings(parsed)['arm']['enabled'])
        for change in ({'message_types': ['std_msgs/msg/String']},
                       {'requires_runtime_running': False}, {'safety_stop': True},
                       {'queue': {'overflow': 'keep_latest'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_ports({'ports': [{**PORTS[0], **change}]})

    def test_send_is_queued_and_owns_message_until_lane_transmission(self):
        goal = self.arm.new_goal()
        op = self.arm.send_goal(goal)
        goal.positions[0] = 999
        self.assertFalse(self.arm._port.native.sent)
        self.assertIsNone(op.snapshot()['accepted'])
        self.assertFalse(op.snapshot()['submitted'])
        self.arm._port.flush_one()
        item = self.arm._port.native.sent[0]
        self.assertEqual(item.request.positions[0], 0.1)
        self.assertEqual(bytes(item.goal_id.uuid).hex(), op.snapshot()['goal_id'])
        self.assertEqual(op.snapshot()['environment_generation'], self.arm._port.generation)
        self.assertTrue(op.snapshot()['submitted'])

    def test_feedback_and_terminal_result_are_separate_from_physical_proof(self):
        op, item, handle = self.accept()
        body = SimpleNamespace(positions=[0.2, 0.3, 0.4])
        item.feedback(SimpleNamespace(feedback=body))
        self.assertIs(op.take_feedback(), body)
        self.assertIsNone(op.take_feedback())
        self.assertIsNone(op.result())
        result = SimpleNamespace(final_state=[0.3, 0.4, 0.5], outcome='completed')
        handle.result_future.set_result(SimpleNamespace(status=4, result=result))
        self.assertIs(op.result(), result)
        self.assertEqual(op.snapshot()['result_status'], 4)
        self.assertEqual(op.snapshot()['state'], 'succeeded')
        self.assertFalse(op.snapshot()['physical_stop_confirmed'])

    def test_runtime_gate_is_checked_on_enqueue_and_again_on_flush(self):
        self.manager.runtime_running = lambda: False
        self.assertEqual(self.arm.send_goal(Goal()).snapshot()['reason'], 'runtime_inactive')
        self.manager.runtime_running = lambda: True
        op = self.arm.send_goal(Goal())
        self.manager.runtime_running = lambda: False
        self.arm._port.flush_one()
        self.assertEqual(op.snapshot()['state'], 'rejected')
        self.assertFalse(self.arm._port.native.sent)

    def test_cancel_after_gate_closes_is_async_and_ack_is_not_stopped(self):
        op, _, handle = self.accept()
        self.manager.runtime_running = lambda: False
        self.assertTrue(op.cancel().accepted)
        self.assertEqual(handle.cancel_calls, 0)
        self.arm._port.flush_one()
        handle.cancel_future.set_result(SimpleNamespace(return_code=0,
            goals_canceling=[SimpleNamespace(goal_id=handle.goal_id)]))
        self.assertEqual(op.snapshot()['cancel_state'], 'accepted')
        self.assertFalse(op.snapshot()['done'])
        self.assertEqual(op.snapshot()['state'], 'accepted')
        self.assertFalse(op.snapshot()['physical_stop_confirmed'])
        handle.result_future.set_result(SimpleNamespace(status=5, result=SimpleNamespace(outcome='canceled')))
        self.assertEqual(op.snapshot()['state'], 'canceled')

    def test_cancel_while_goal_ack_pending_sends_once_after_acceptance(self):
        op, item = self.send()
        op.cancel()
        op.cancel()
        handle = NativeGoal(item.goal_id)
        item.future.set_result(handle)
        self.arm._port.flush_one()
        self.arm._port.flush_one()
        self.assertEqual(handle.cancel_calls, 1)

    def test_cancel_unsent_goal_removes_it_without_native_write(self):
        op = self.arm.send_goal(Goal())
        op.cancel()
        self.arm._port.flush_one()
        self.assertFalse(self.arm._port.native.sent)
        self.assertEqual(op.snapshot()['reason'], 'canceled_before_submission')
        self.assertFalse(op.snapshot()['accepted'])
        self.assertFalse(op.snapshot()['submitted'])

    def test_stop_service_is_allowed_with_runtime_inactive_but_not_new_goal(self):
        self.manager.runtime_running = lambda: False
        op = self.stop.call(self.stop.new_request())
        self.assertEqual(op.snapshot()['state'], 'queued')
        self.assertFalse(self.stop._port.native.sent)
        self.stop._port.flush_one()
        response = SimpleNamespace(ok=False, outcome='stop_not_proven')
        self.stop._port.native.sent[0].future.set_result(response)
        self.assertIs(op.result(), response)
        self.assertEqual(op.snapshot()['state'], 'succeeded')  # response received only
        self.assertFalse(op.snapshot()['physical_stop_confirmed'])

    def test_stop_during_environment_transition_requires_existing_hook_token(self):
        self.manager.accepting = False
        self.assertEqual(self.stop.call(Request()).snapshot()['state'], 'unavailable')
        import time
        self.manager._stop_token = 'transition'
        self.manager._stop_owners = {self.facade.owner_id}
        self.manager._stop_deadline = time.monotonic() + 5
        self.facade._hook_local.token = 'transition'
        op = self.stop.call(Request())
        del self.facade._hook_local.token
        self.stop._port.flush_one()
        self.assertEqual(op.snapshot()['state'], 'submitted')
        self.assertEqual(len(self.stop._port.native.sent), 1)

    def test_expired_hook_cannot_send_delayed_stop(self):
        self.manager._stop_token = 'transition'
        self.manager._stop_owners = {self.facade.owner_id}
        import time
        self.manager._stop_deadline = time.monotonic() + 5
        self.facade._hook_local.token = 'transition'
        op = self.stop.call(Request())
        self.manager._stop_token = None
        del self.facade._hook_local.token
        self.stop._port.flush_one()
        self.assertEqual(op.snapshot()['state'], 'rejected')
        self.assertFalse(self.stop._port.native.sent)

    def test_rebind_revokes_pending_result_and_late_feedback(self):
        op, item, handle = self.accept()
        old_epoch = op.snapshot()['binding_generation']
        self.manager.runtime_running = lambda: False
        config = copy.deepcopy(self.facade.bindings_config)
        config['arm']['topic'] = '/replacement'
        self.facade.reconfigure(config)
        self.assertEqual(op.snapshot()['state'], 'unknown')
        self.assertGreater(self.arm.status()['binding_generation'], old_epoch)
        item.feedback(SimpleNamespace(feedback=SimpleNamespace(positions=[99])))
        handle.result_future.set_result(SimpleNamespace(status=4, result='old result'))
        self.assertIsNone(op.take_feedback())
        self.assertIsNone(op.result())
        self.assertEqual(op.snapshot()['state'], 'unknown')
        self.assertFalse(self.arm._port.native.sent)

    def test_close_before_acceptance_ignores_late_ack_without_replay(self):
        op, item = self.send()
        self.arm.close()
        item.future.set_result(NativeGoal(item.goal_id))
        self.assertEqual(op.snapshot()['state'], 'unknown')
        self.assertIsNone(op.snapshot()['accepted'])
        self.assertIsNone(op.result())
        self.assertEqual(op.cancel().status, 'unavailable')

    def test_environment_generation_change_rejects_old_service_response(self):
        op = self.stop.call(Request())
        self.stop._port.flush_one()
        item = self.stop._port.native.sent[0]
        self.stop._port.unavailable()
        self.manager._adapter.attach(self.stop._port, self.stop._port.generation + 1)
        item.future.set_result(SimpleNamespace(ok=True))
        self.assertEqual(op.snapshot()['state'], 'unknown')
        self.assertIsNone(op.result())

    def test_timeout_is_unknown_blocks_retransmission_and_ignores_late_result(self):
        from astrbot_ex.core.environments import plugin_api
        clock = FakeTime()
        with patch.object(plugin_api, 'time', clock):
            op, _, handle = self.accept()
            clock.advance(31)
            self.arm._port.flush_one()
            self.assertEqual(op.snapshot()['state'], 'unknown')
            self.assertTrue(op.snapshot()['submitted'])
            self.assertEqual(self.arm.send_goal(Goal()).snapshot()['reason'], 'previous_command_outcome_unknown')
            handle.result_future.set_result(SimpleNamespace(status=4, result='late'))
            self.assertIsNone(op.result())
            self.assertEqual(len(self.arm._port.native.sent), 1)

    def test_unknown_blocks_action_and_stop_resubmission_even_with_spare_capacity(self):
        from astrbot_ex.core.environments import plugin_api
        clock = FakeTime()
        with patch.object(plugin_api, 'time', clock):
            for endpoint in (self.arm, self.stop):
                endpoint._port.declaration['queue']['capacity'] = 2
            action, _ = self.send()
            stop = self.stop.call(Request())
            self.stop._port.flush_one()
            clock.advance(61)
            self.arm._port.flush_one()
            self.stop._port.flush_one()
            self.assertEqual(action.snapshot()['state'], 'unknown')
            self.assertEqual(stop.snapshot()['state'], 'unknown')
            for op in (self.arm.send_goal(Goal()), self.stop.call(Request())):
                self.assertEqual(op.snapshot()['reason'], 'previous_command_outcome_unknown')
                self.assertFalse(op.snapshot()['submitted'])
            self.assertEqual(len(self.arm._port.native.sent), 1)
            self.assertEqual(len(self.stop._port.native.sent), 1)

    def test_queued_goal_is_not_sent_after_earlier_send_became_unknown(self):
        self.arm._port.declaration['queue']['capacity'] = 2
        first = self.arm.send_goal(Goal())
        second = self.arm.send_goal(Goal())
        native = self.arm._port.native
        original = native.send_goal_async
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('transport failed after submission')
        with patch.object(native, 'send_goal_async', side_effect=uncertain):
            self.arm._port.flush_one()
        self.arm._port.flush_one()
        self.assertEqual(first.snapshot()['state'], 'unknown')
        self.assertTrue(first.snapshot()['submitted'])
        self.assertEqual(second.snapshot()['reason'], 'previous_command_outcome_unknown')
        self.assertFalse(second.snapshot()['submitted'])
        self.assertEqual(len(native.sent), 1)

    def test_unavailable_server_rejects_without_automatic_retry(self):
        native = self.arm._port.native
        native.ready = False
        op = self.arm.send_goal(Goal())
        self.arm._port.flush_one()
        native.ready = True
        self.arm._port.flush_one()
        self.assertEqual(op.snapshot()['state'], 'unavailable')
        self.assertFalse(op.snapshot()['submitted'])
        self.assertFalse(native.sent)

    def test_unsent_queue_expiry_is_explicit_and_does_not_claim_submitted(self):
        from astrbot_ex.core.environments import plugin_api
        clock = FakeTime()
        with patch.object(plugin_api, 'time', clock):
            op = self.arm.send_goal(Goal())
            clock.advance(0.201)
            self.arm._port.flush_one()
        self.assertEqual(op.snapshot()['state'], 'expired')
        self.assertFalse(op.snapshot()['submitted'])
        self.assertFalse(self.arm._port.native.sent)

    def test_mismatched_goal_ack_and_cancel_ack_cannot_claim_our_goal_stopped(self):
        op, item = self.send()
        item.future.set_result(NativeGoal(SimpleNamespace(uuid=[99] * 16)))
        self.assertEqual(op.snapshot()['state'], 'unknown')
        self.assertEqual(op.snapshot()['reason'], 'goal_ack_identity_mismatch')
        self.arm._port.unavailable()
        self.manager._adapter.attach(self.arm._port, 23)
        op, _, handle = self.accept()
        op.cancel()
        self.arm._port.flush_one()
        handle.cancel_future.set_result(SimpleNamespace(return_code=0,
            goals_canceling=[SimpleNamespace(goal_id=SimpleNamespace(uuid=[77] * 16))]))
        self.assertEqual(op.snapshot()['cancel_state'], 'rejected')
        self.assertFalse(op.snapshot()['done'])

    def test_goal_rejection_and_cancel_rejection_remain_distinct(self):
        op, item = self.send()
        item.future.set_result(NativeGoal(item.goal_id, accepted=False))
        self.assertEqual(op.snapshot()['state'], 'rejected')
        self.assertFalse(op.snapshot()['accepted'])
        op, _, handle = self.accept()
        op.cancel()
        self.arm._port.flush_one()
        handle.cancel_future.set_result(SimpleNamespace(return_code=1, goals_canceling=[]))
        self.assertEqual(op.snapshot()['cancel_state'], 'rejected')
        self.assertEqual(op.snapshot()['state'], 'accepted')

    def test_outstanding_goal_prevents_successful_environment_teardown(self):
        op, _, _ = self.accept()
        self.manager.runtime_running = lambda: False
        self.manager.quiesce_timeout = 0.02
        self.manager.select('normal')
        self.manager._worker.join(1)
        self.assertEqual(self.manager.snapshot()['phase'], 'failed')
        self.assertEqual(self.manager.snapshot()['active_mode'], 'ros2')
        self.assertIsNotNone(self.arm._port.native)
        self.assertFalse(op.snapshot()['physical_stop_confirmed'])

    def test_current_publisher_gid_is_in_subscription_envelope(self):
        facade = PluginRosFacade(plugin_id='observer', ports=[{'id': 'state', 'direction': 'subscribe',
            'message_types': ['sensor_msgs/msg/JointState']}])
        sub = facade.subscribe('state')
        port = sub._port
        port.native = object()
        port.generation, port.binding_generation = 7, 3
        cb = _subscription_callback(port, 7, 3)
        cb(Goal(), SimpleNamespace(publisher_gid=bytes(range(24))))
        packet = sub.get_nowait()
        self.assertEqual(packet.source_id, bytes(range(24)).hex())
        self.assertEqual((packet.generation, packet.binding_generation), (7, 3))
        cb(Goal(), {'publisher_gid': list(range(24))})
        self.assertEqual(sub.get_nowait().source_id, bytes(range(24)).hex())
        cb(Goal(), SimpleNamespace(publisher_gid=bytes(24)))
        self.assertIsNone(sub.get_nowait().source_id)
        humble_callback = _subscription_callback(port, 7, 3, with_message_info=False)
        msg = Goal()
        humble_callback(msg)
        message_only = sub.get_nowait()
        self.assertIs(message_only.message, msg)
        self.assertIsNone(message_only.source_id)
        self.assertEqual((message_only.generation, message_only.binding_generation), (7, 3))
        port.binding_generation += 1
        humble_callback(msg)
        self.assertIsNone(sub.get_nowait())
        cb(Goal(), SimpleNamespace(publisher_gid=b'old'))
        self.assertIsNone(sub.get_nowait())
        facade.close()


class NativeAdapterCommandTest(unittest.TestCase):
    def test_action_and_service_use_framework_lane_and_destroy_native_resources(self):
        facade = PluginRosFacade(plugin_id='robot', ports=PORTS,
            bindings={'arm': {'enabled': True}, 'stop': {'enabled': True}})
        arm, stop = facade.action_client('arm'), facade.service_client('stop')
        destroyed = []
        node = SimpleNamespace(
            create_client=lambda cls, name, **kwargs: NativeService(),
            resolve_topic_name=lambda name: name,
            destroy_client=lambda native: destroyed.append(native),
            destroy_node=lambda: destroyed.append('node'))
        lane = SimpleNamespace(lock=threading.RLock(), ports={}, executor=SimpleNamespace(
            add_node=lambda value: None, remove_node=lambda value: None))
        adapter = Ros2EnvironmentAdapter()
        adapter._context = object()
        adapter._rclpy = SimpleNamespace(create_node=lambda *args, **kwargs: node)
        adapter._lanes['control'] = lane
        fake_action = SimpleNamespace(ActionClient=NativeAction)
        modules = {'rclpy': SimpleNamespace(action=fake_action), 'rclpy.action': fake_action,
                   'rclpy.validate_topic_name': SimpleNamespace(validate_topic_name=lambda name: None)}
        with patch.dict('sys.modules', modules), \
             patch('astrbot_ex.core.environments.ros2.adapter.load_interface',
                   side_effect=lambda name: Action if '/action/' in name else Service), \
             patch('astrbot_ex.core.environments.ros2.adapter._qos_profile', return_value=object()):
            adapter.attach(arm._port, 12)
            adapter.attach(stop._port, 12)
        self.assertEqual(len(adapter._nodes), 1)
        self.assertEqual(len(lane.ports), 2)
        native_action, native_service = arm._port.native, stop._port.native
        self.assertIsInstance(native_action, NativeAction)
        self.assertIsInstance(native_service, NativeService)
        adapter.detach(arm._port)
        self.assertTrue(native_action.destroyed)
        self.assertEqual(len(adapter._nodes), 1)
        adapter.detach(stop._port)
        self.assertIn(native_service, destroyed)
        self.assertIn('node', destroyed)
        self.assertFalse(lane.ports)


if __name__ == '__main__': unittest.main()
