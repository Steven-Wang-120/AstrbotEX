"""Inspect live DDS endpoints with system Jazzy, without ros2cli graph cache."""

import json
import os
import socket
import sys
import time
from xmlrpc.client import ServerProxy

import rclpy
import rclpy.action


DIAGNOSTIC_TOPICS = {
    ('/rosout', 'rcl_interfaces/msg/Log'),
    ('/parameter_events', 'rcl_interfaces/msg/ParameterEvent'),
}
DIAGNOSTIC_SERVICES = {
    ('describe_parameters', 'rcl_interfaces/srv/DescribeParameters'),
    ('get_parameter_types', 'rcl_interfaces/srv/GetParameterTypes'),
    ('get_parameters', 'rcl_interfaces/srv/GetParameters'),
    ('list_parameters', 'rcl_interfaces/srv/ListParameters'),
    ('set_parameters', 'rcl_interfaces/srv/SetParameters'),
    ('set_parameters_atomically', 'rcl_interfaces/srv/SetParametersAtomically'),
    ('get_type_description', 'type_description_interfaces/srv/GetTypeDescription'),
}


def local_daemon_identity(domain):
    """Ask the local daemon only for its identity, never for a cached graph."""
    socket.setdefaulttimeout(0.7)
    with ServerProxy(f'http://127.0.0.1:{11511 + domain}/ros2cli/', allow_none=True) as daemon:
        return daemon.get_name(), daemon.get_namespace()


def endpoints(node, name, namespace):
    """Return every direct-graph endpoint owned by a node."""
    functions = {
        'publishers': node.get_publisher_names_and_types_by_node,
        'subscribers': node.get_subscriber_names_and_types_by_node,
        'services': node.get_service_names_and_types_by_node,
        'clients': node.get_client_names_and_types_by_node,
        'action_servers': lambda n, ns: rclpy.action.get_action_server_names_and_types_by_node(node, n, ns),
        'action_clients': lambda n, ns: rclpy.action.get_action_client_names_and_types_by_node(node, n, ns),
    }
    return {kind: sorted((topic, typ) for topic, types in function(name, namespace) for typ in types)
            for kind, function in functions.items()}


def diagnostic_only(node_name, namespace, owned, daemon_identity, domain):
    """An exact local identity and every endpoint must satisfy the allowlist."""
    if daemon_identity != (node_name, namespace):
        return False, 'local daemon identity does not match'
    if namespace != '/' or not node_name.startswith(f'_ros2cli_daemon_{domain}_'):
        return False, 'unexpected daemon name or namespace'
    if owned['action_servers'] or owned['action_clients']:
        return False, 'action endpoint is not diagnostic'
    for kind in ('publishers', 'subscribers'):
        if not set(map(tuple, owned[kind])).issubset(DIAGNOSTIC_TOPICS):
            return False, f'unexpected {kind}'
    for kind in ('services', 'clients'):
        for topic, typ in owned[kind]:
            parts = topic.lstrip('/').split('/')
            if len(parts) < 2 or parts[-2] != node_name or (parts[-1], typ) not in DIAGNOSTIC_SERVICES:
                return False, f'unexpected {kind}'
    return True, 'confirmed local ros2cli diagnostic daemon; no application endpoints'


def inspect(node, identity_query, observed_at, domain):
    names = sorted((n, ns) for n, ns in node.get_node_names_and_namespaces() if n != node.get_name())
    if len(names) != len(set(names)):
        raise RuntimeError('duplicate node name/namespace')
    rows = []
    for name, namespace in names:
        owned = endpoints(node, name, namespace)
        ignored, reason = False, 'unknown/application node'
        if name.startswith('_ros2cli_daemon_'):
            try:
                ignored, reason = diagnostic_only(name, namespace, owned, identity_query(), domain)
            except Exception as exc:
                reason = 'cannot confirm local daemon identity: ' + repr(exc)
        rows.append({'name': name, 'namespace': namespace, 'endpoints': owned,
                     'ignored': ignored, 'reason': reason})
    return {'observed_at': observed_at, 'nodes': rows,
            'conflicts': [r for r in rows if not r['ignored']]}


def node_record(row):
    """Flatten one observed node for the audit trail: identity, every endpoint kind, reason."""
    owned = row['endpoints']
    record = {'name': row['name'], 'namespace': row['namespace']}
    for kind in ('publishers', 'subscribers', 'services', 'clients', 'action_servers', 'action_clients'):
        record[kind] = owned[kind]
    record['ignored'] = row['ignored']
    record['ignored_reason'] = row['reason'] if row['ignored'] else None
    record['blocked_reason'] = None if row['ignored'] else row['reason']
    return record


def classify(stable, nodes):
    """Pure decision over observed rows: the label never depends on sampling mechanics."""
    if not stable:
        return 'FAIL', 'graph discovery was not stable across the last two samples'
    if [row for row in nodes if not row['ignored']]:
        return 'FAIL', 'unknown node, unsafe endpoint, or unconfirmed identity'
    if [row for row in nodes if row['ignored']]:
        return 'PASS_WITH_IGNORED_DIAGNOSTIC_DAEMON', 'only confirmed local ros2cli diagnostic daemon(s) present'
    return 'PASS', 'stable diagnostic-only or empty graph'


def main():
    domain = int(os.environ.get('ROS_DOMAIN_ID', '0'))
    if domain != 63:
        raise RuntimeError(f'configured profile requires ROS_DOMAIN_ID=63, got {domain}')
    rclpy.init()
    node = rclpy.create_node('astrex_domain_preflight', start_parameter_services=False)
    reads = []
    started = time.monotonic()
    deadline = started + 5
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            try:
                reads.append(inspect(node, lambda: local_daemon_identity(domain), time.time(), domain))
            except Exception as exc:
                print(json.dumps({'status': 'FAIL', 'reason': 'direct graph inspection failed',
                                  'error': repr(exc), 'samples': reads}, ensure_ascii=False))
                return 1
            time.sleep(min(0.35, max(0, deadline - time.monotonic())))
        stable = len(reads) >= 2 and reads[-1]['nodes'] == reads[-2]['nodes']
        final = reads[-1] if reads else {'nodes': [], 'conflicts': []}
        status, reason = classify(stable, final['nodes'])
        print(json.dumps({'status': status, 'domain': domain, 'direct_graph': True,
                          'duration_seconds': round(time.monotonic() - started, 3),
                          'stable_last_two': stable, 'sample_count': len(reads),
                          'ignored': [node_record(r) for r in final['nodes'] if r['ignored']],
                          'blocked': [node_record(r) for r in final['conflicts']],
                          'final': final, 'previous': reads[-2] if len(reads) >= 2 else None,
                          'reason': reason}, ensure_ascii=False))
        return status == 'FAIL'
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'reason': 'preflight exception', 'error': repr(exc)}))
        sys.exit(1)
