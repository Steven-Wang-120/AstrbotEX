"""ROS domain preflight cases: pure decision logic and constructed graph snapshots.

Run with system Jazzy Python (Python 3.12):

    source /opt/ros/jazzy/setup.bash
    /usr/bin/python3 -B tests/isaac/test_ros_domain_preflight.py

No DDS participant is created here: every case builds a graph snapshot and feeds it
to the production policy functions in ``sim/scripts/ros_domain_check.py``. The live
domain 63 acceptance (real ros2cli diagnostic daemon) happens in the launcher run.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / 'sim/scripts/ros_domain_check.py'

spec = importlib.util.spec_from_file_location('ros_domain_check', POLICY_PATH)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

DAEMON = '_ros2cli_daemon_63_a2301b0d591741929c2e33557c5d743d'
EMPTY = {'publishers': [], 'subscribers': [], 'services': [], 'clients': [],
         'action_servers': [], 'action_clients': []}
SAFE = dict(EMPTY, publishers=[('/rosout', 'rcl_interfaces/msg/Log'),
                               ('/parameter_events', 'rcl_interfaces/msg/ParameterEvent')])
PARAMETER_SERVICES = [(f'/{DAEMON}/{name}', kind) for name, kind in (
    ('get_parameters', 'rcl_interfaces/srv/GetParameters'),
    ('set_parameters', 'rcl_interfaces/srv/SetParameters'),
    ('list_parameters', 'rcl_interfaces/srv/ListParameters'),
    ('describe_parameters', 'rcl_interfaces/srv/DescribeParameters'),
    ('get_parameter_types', 'rcl_interfaces/srv/GetParameterTypes'),
    ('get_type_description', 'type_description_interfaces/srv/GetTypeDescription'))]


class FakeNode:
    """Minimal stand-in for a direct-graph query interface."""

    def __init__(self, nodes):
        self.nodes = nodes

    def get_name(self):
        return 'astrex_domain_preflight'

    def get_node_names_and_namespaces(self):
        return list(self.nodes)


def daemon_row(owned=None, identity=None):
    """Constructed snapshot row for a local ros2cli daemon, decided by the production policy."""
    owned = SAFE if owned is None else owned
    identity = (DAEMON, '/') if identity is None else identity
    ignored, reason = policy.diagnostic_only(DAEMON, '/', owned, identity, 63)
    return {'name': DAEMON, 'namespace': '/', 'endpoints': owned, 'ignored': ignored, 'reason': reason}


def app_row(name='other_app', owned=None):
    owned = EMPTY if owned is None else owned
    return {'name': name, 'namespace': '/', 'endpoints': owned, 'ignored': False,
            'reason': 'unknown/application node'}


class PreflightCases(unittest.TestCase):

    def test_case1_empty_domain_passes(self):
        self.assertEqual(policy.classify(True, []), ('PASS', 'stable diagnostic-only or empty graph'))

    def test_case2_confirmed_diagnostic_daemon_passes_with_label(self):
        row = daemon_row(owned=dict(SAFE, services=PARAMETER_SERVICES))
        self.assertTrue(row['ignored'], row['reason'])
        status, reason = policy.classify(True, [row])
        self.assertEqual(status, 'PASS_WITH_IGNORED_DIAGNOSTIC_DAEMON')
        self.assertIn('diagnostic daemon', reason)
        record = policy.node_record(row)
        for key in ('name', 'namespace', 'publishers', 'subscribers', 'services', 'clients',
                    'action_servers', 'action_clients', 'ignored', 'ignored_reason', 'blocked_reason'):
            self.assertIn(key, record)
        self.assertIsNone(record['blocked_reason'])
        self.assertTrue(record['ignored_reason'])
        self.assertEqual(record['name'], DAEMON)
        self.assertEqual(record['namespace'], '/')

    def test_case3_unconfirmed_daemon_identity_blocks(self):
        row = daemon_row(identity=('_ros2cli_daemon_63_other', '/'))
        self.assertFalse(row['ignored'])
        self.assertIn('identity', policy.node_record(row)['blocked_reason'])
        self.assertEqual(policy.classify(True, [row])[0], 'FAIL')
        # Wrong namespace, wrong domain suffix and a name that only looks like a daemon all block.
        self.assertFalse(policy.diagnostic_only(DAEMON, '/sub', SAFE, (DAEMON, '/'), 63)[0])
        self.assertFalse(policy.diagnostic_only('_ros2cli_daemon_62_x', '/', SAFE, ('_ros2cli_daemon_62_x', '/'), 63)[0])
        self.assertFalse(policy.diagnostic_only('ros2cli_daemon_63_x', '/', SAFE, ('ros2cli_daemon_63_x', '/'), 63)[0])

    def test_case4_control_or_unknown_endpoints_block(self):
        cases = (('publishers', [('/joint_command', 'sensor_msgs/msg/JointState')]),
                 ('publishers', [('/joint_states', 'sensor_msgs/msg/JointState')]),
                 ('publishers', [('/clock', 'rosgraph_msgs/msg/Clock')]),
                 ('publishers', [('/astrex/unknown_business_topic', 'std_msgs/msg/String')]),
                 ('subscribers', [('/joint_command', 'sensor_msgs/msg/JointState')]),
                 ('services', [(f'/{DAEMON}/start_controller', 'std_srvs/srv/Trigger')]),
                 ('clients', [(f'/{DAEMON}/do_something', 'std_srvs/srv/Trigger')]),
                 ('action_servers', [('/robot_move', 'control_msgs/action/FollowJointTrajectory')]),
                 ('action_clients', [('/robot_move', 'control_msgs/action/FollowJointTrajectory')]))
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                row = daemon_row(owned=dict(SAFE, **{kind: value}))
                self.assertFalse(row['ignored'], row['reason'])
                self.assertEqual(policy.classify(True, [row])[0], 'FAIL')

    def test_case5_unknown_node_blocks(self):
        self.assertEqual(policy.classify(True, [app_row()])[0], 'FAIL')
        noisy = app_row(name='some_app', owned=dict(EMPTY, publishers=[('/clock', 'rosgraph_msgs/msg/Clock')]))
        self.assertEqual(policy.classify(True, [noisy])[0], 'FAIL')

    def test_case6_unstable_discovery_blocks(self):
        status, reason = policy.classify(False, [])
        self.assertEqual(status, 'FAIL')
        self.assertIn('stable', reason)
        self.assertEqual(policy.classify(False, [daemon_row()])[0], 'FAIL')

    def test_inspect_builds_snapshot_and_reports_conflicts(self):
        node = FakeNode([(DAEMON, '/'), ('other_app', '/')])
        with patch.object(policy, 'endpoints',
                          side_effect=lambda n, name, ns: SAFE if name == DAEMON else EMPTY):
            result = policy.inspect(node, lambda: (DAEMON, '/'), 1.0, 63)
        self.assertEqual([row['name'] for row in result['conflicts']], ['other_app'])
        self.assertTrue(result['nodes'][0]['ignored'])
        self.assertEqual(policy.classify(True, result['nodes'])[0], 'FAIL')

    def test_direct_graph_failure_is_fatal(self):
        with patch.object(policy, 'endpoints', side_effect=RuntimeError('graph query failed')):
            with self.assertRaisesRegex(RuntimeError, 'graph query failed'):
                policy.inspect(FakeNode([(DAEMON, '/')]), lambda: (DAEMON, '/'), 1.0, 63)

    def test_duplicate_identity_is_fatal(self):
        with patch.object(policy, 'endpoints', return_value=SAFE):
            with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                policy.inspect(FakeNode([(DAEMON, '/'), (DAEMON, '/')]), lambda: (DAEMON, '/'), 1.0, 63)


if __name__ == '__main__':
    sys.exit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
