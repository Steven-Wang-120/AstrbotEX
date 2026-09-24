"""Real DDS acceptance; skipped explicitly on a host without ROS.

Run in an already sourced ROS environment. Uses isolated domain 73 by default.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.plugin_api import PluginRosFacade
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus

ROS_AVAILABLE = importlib.util.find_spec('rclpy') is not None


@unittest.skipUnless(ROS_AVAILABLE, 'real ROS integration requires sourced rclpy and message packages')
class NativeRosIntegrationTest(unittest.TestCase):
    def setUp(self):
        import rclpy
        from rclpy.context import Context
        from std_msgs.msg import String
        self.rclpy, self.String = rclpy, String
        self.domain = int(os.getenv('ASTRBOTEX_TEST_ROS_DOMAIN_ID', '73'))
        self.env = patch.dict(os.environ, {'ROS_DOMAIN_ID': str(self.domain)})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.prefix = '/ex_test_' + uuid.uuid4().hex[:10]
        self.bus = TopicBus()
        self.manager = EnvironmentManager(data_root=self.temp.name, event_bus=EventBus(), topic_bus=self.bus)
        self.manager.configure_ros2({'discovery_interval_sec': 0.2, 'statistics_interval_sec': 1.0})
        self.peer_context = Context()
        rclpy.init(context=self.peer_context, domain_id=self.domain)
        self.peer = rclpy.create_node('peer_' + uuid.uuid4().hex[:8], context=self.peer_context)
        from rclpy.executors import SingleThreadedExecutor
        self.peer_executor = SingleThreadedExecutor(context=self.peer_context)
        self.peer_executor.add_node(self.peer)
        self.children = []

    def tearDown(self):
        for child in self.children:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)
        self.manager.close()
        self.peer_executor.shutdown(timeout_sec=2.0)
        self.peer.destroy_node()
        self.rclpy.shutdown(context=self.peer_context)
        self.temp.cleanup()
        self.env.stop()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.peer_executor.spin_once(timeout_sec=0.01)
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail('DDS condition did not converge before deadline: ' + json.dumps({
            'graph':self.manager.graph(), 'endpoints':self.manager.endpoints()}, default=str))

    def switch(self, mode):
        self.manager.select(mode)
        if self.manager._worker:
            self.manager._worker.join(8)
        self.assertEqual(self.manager.snapshot()['phase'], 'idle', self.manager.snapshot())

    def facade(self, name, message_type='std_msgs/msg/String', qos='reliable_volatile'):
        ports = [
            {'id':'input', 'direction':'subscribe', 'message_types':[message_type],
             'default_topic':self.prefix + '/input', 'qos_preset':qos,
             'queue':{'capacity':4, 'overflow':'keep_latest', 'max_message_bytes':65536, 'max_bytes':131072}},
            {'id':'output', 'direction':'publish', 'message_types':[message_type],
             'default_topic':self.prefix + '/output', 'qos_preset':'reliable_volatile'},
        ]
        facade = PluginRosFacade(plugin_id=name, environment_manager=self.manager, ports=ports,
                                 bindings={'output':{'enabled':True}})
        return facade, facade.subscribe('input'), facade.publisher('output')

    def test_external_process_late_publisher_dual_owners_and_bidirectional(self):
        first, incoming, outgoing = self.facade('first')
        second, other, _ = self.facade('second')
        self.switch('ros2')
        # Start an independent process only after EX has subscribed.
        code = """
import rclpy, sys, time
from std_msgs.msg import String
rclpy.init()
node=rclpy.create_node('external_ex_acceptance')
publisher=node.create_publisher(String,sys.argv[1],10)
message=String(); message.data='external DDS payload'
deadline=time.monotonic()+30
while time.monotonic()<deadline:
    publisher.publish(message)
    rclpy.spin_once(node,timeout_sec=0.03)
    time.sleep(0.02)
node.destroy_node();rclpy.shutdown()
"""
        child = subprocess.Popen([sys.executable, '-c', code, self.prefix + '/input'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        self.wait_for(lambda: incoming.status()['rx_received'] > 0 and other.status()['rx_received'] > 0)
        self.assertEqual(incoming.get_nowait().message.data, 'external DDS payload')
        first.close()
        before = other.status()['rx_received']
        self.wait_for(lambda: other.status()['rx_received'] > before)
        replies = []
        self.peer.create_subscription(self.String, self.prefix + '/output', lambda msg: replies.append(msg.data), 10)
        publisher = second.publisher('output')
        self.wait_for(lambda: publisher.status()['matched_peer_count'])
        message = publisher.new_message(); message.data = 'EX native reply'
        self.assertEqual(publisher.publish(message).status, 'queued')
        self.wait_for(lambda: 'EX native reply' in replies)
        self.assertGreater(publisher.status()['tx_published'], 0)
        inbox = self.bus.subscribe_inbox('acceptance.internal')
        self.bus.publish_payload('acceptance.internal', timestamp=time.time(), source='test', payload={'ok':True})
        self.assertTrue(inbox.get_nowait().payload['ok'])

    def test_qos_diagnostics_and_reconfiguration_recover(self):
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        facade, incoming, _ = self.facade('qos')
        peer = self.peer.create_publisher(self.String, self.prefix + '/input',
            QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT))
        self.switch('ros2')
        self.wait_for(lambda: incoming.status()['state'] == 'qos_incompatible')
        self.assertGreater(incoming.status()['graph_peer_count'], 0)
        self.assertIn(incoming.status()['matched_peer_count'], (0, None))
        bindings = copy.deepcopy(facade.bindings_config)
        bindings['input']['qos']['reliability'] = 'best_effort'
        facade.reconfigure(bindings)
        self.wait_for(lambda: incoming.status()['graph_peer_count'] and incoming.status()['state'] != 'qos_incompatible')
        message = self.String(); message.data = 'compatible'
        peer.publish(message)
        self.assertEqual(self.wait_for(incoming.get_nowait).message.data, 'compatible')

    def test_twenty_switches_release_threads_and_discard_queued_messages(self):
        _, incoming, outgoing = self.facade('cycles')
        baseline = {thread.ident for thread in threading.enumerate() if thread.name.startswith('ex-ros-')}
        for _ in range(20):
            self.switch('ros2')
            message = outgoing.new_message(); message.data = 'must not replay'
            outgoing.publish(message)
            self.switch('normal')
            self.assertEqual(outgoing.status()['queue_depth'], 0)
            self.assertIsNone(incoming.get_nowait())
        remaining = {thread.ident for thread in threading.enumerate() if thread.name.startswith('ex-ros-')}
        self.assertEqual(baseline, remaining)
        self.assertIs(self.manager.topic_bus, self.bus)

    def test_high_rate_slow_consumer_stays_bounded(self):
        from pathlib import Path
        _, incoming, _ = self.facade('pressure', qos='sensor_data')
        publisher = self.peer.create_publisher(self.String, self.prefix + '/input', 10)
        self.switch('ros2')
        self.wait_for(lambda: incoming.status()['graph_peer_count'])
        message = self.String(); message.data = 'x' * 2048
        def rss_kib():
            path = Path('/proc/self/status')
            if not path.exists(): return None
            return int(next(line.split()[1] for line in path.read_text().splitlines()
                            if line.startswith('VmRSS:')))
        started, rss_before = time.monotonic(), rss_kib()
        stop, latencies, sent = threading.Event(), [], [0]
        def produce():
            while not stop.is_set():
                publisher.publish(message)
                sent[0] += 1
                stop.wait(0.0005)
        def consume():
            while not stop.wait(0.02):
                packet = incoming.get_nowait()
                if packet:
                    latencies.append((time.monotonic_ns() - packet.received_monotonic_ns) / 1e6)
        producer = threading.Thread(target=produce)
        consumer = threading.Thread(target=consume)
        producer.start(); consumer.start()
        try:
            self.wait_for(lambda: sent[0] >= 1500 and incoming.status()['queue_dropped'] > 0)
            status, rss_loaded = incoming.status(), rss_kib()
            self.assertLessEqual(status['queue_depth'], 4)
            self.assertLessEqual(status['queue_bytes'], 131072)
            stop_started = time.monotonic()
            self.switch('normal')  # Publisher is still sending during shutdown.
            stop_elapsed = time.monotonic() - stop_started
            self.assertLess(stop_elapsed, 3.0)
        finally:
            stop.set(); producer.join(3); consumer.join(3)
        self.assertTrue(latencies)
        latencies.sort()
        elapsed = time.monotonic() - started
        print('ROS_PRESSURE ' + json.dumps({'sent':sent[0],'elapsed_sec':round(elapsed,3),
            'input_hz':round(sent[0]/elapsed,1),'payload_bytes':2048,
            'received':status['rx_received'],'dropped':status['queue_dropped'],
            'peak_queue_bytes':status['queue_peak_bytes'],'rss_before_kib':rss_before,
            'rss_loaded_kib':rss_loaded,'consumed':len(latencies),
            'queue_age_p95_ms':round(latencies[int((len(latencies)-1)*0.95)],3),
            'queue_age_max_ms':round(max(latencies),3),'stop_under_load_sec':round(stop_elapsed,3)}))

    def test_custom_nested_type_roundtrip_and_missing_package_isolated(self):
        try:
            from astrbotex_demo_interfaces.msg import Target
        except ImportError:
            self.skipTest('build and source ros_interfaces before custom-type acceptance')
        _, incoming, outgoing = self.facade('custom', 'astrbotex_demo_interfaces/msg/Target')
        bad_ports = [{'id':'missing','direction':'subscribe','message_types':['absent_ex_test/msg/Missing']}]
        missing = PluginRosFacade(plugin_id='missing', environment_manager=self.manager, ports=bad_ports).subscribe('missing')
        publisher = self.peer.create_publisher(Target, self.prefix + '/input', 10)
        replies = []
        self.peer.create_subscription(Target, self.prefix + '/output', replies.append, 10)
        self.switch('ros2')
        self.assertEqual(missing.status()['state'], 'missing_interface')
        self.assertTrue(self.manager.check_interface('astrbotex_demo_interfaces/msg/Target')['available'])
        self.wait_for(lambda: incoming.status()['graph_peer_count'] and outgoing.status()['matched_peer_count'])
        message = Target(); message.label='nested'; message.position.x=1.25; message.header.frame_id='test'
        publisher.publish(message)
        packet = self.wait_for(incoming.get_nowait)
        self.assertEqual(packet.message.position.x, 1.25)
        self.assertEqual(outgoing.publish(packet.message).status, 'queued')
        self.wait_for(lambda: replies)
        self.assertEqual(replies[0].header.frame_id, 'test')


if __name__ == '__main__':
    unittest.main()
