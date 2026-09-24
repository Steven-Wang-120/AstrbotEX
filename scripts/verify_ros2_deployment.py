"""Run outside the EX container on an idle, example-only acceptance instance.

Temporarily selects Domain 73 and unique topics, then restores configuration.
Requires sourced rclpy and std_msgs in this host process.
"""
import argparse
import copy
import json
import time
import uuid
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    def api(path, data=None):
        request = Request(args.url.rstrip('/') + '/api/v1/ex/' + path,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={'Content-Type':'application/json'})
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    def switch(mode):
        api('environments/select', {'mode':mode})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = api('environments')['environment']
            if state['phase'] == 'idle' and state['active_mode'] == mode:
                return
            if state['phase'] == 'failed': raise AssertionError(state)
            time.sleep(0.1)
        raise AssertionError('mode switch timed out')
    def save_bindings(bindings):
        current = api('plugins/ros2_echo/ros2')['ros2']
        api('plugins/ros2_echo/ros2', {'expected_revision':current['revision'], 'bindings':bindings})

    original = api('environments')
    assert original['environment']['active_mode'] == 'normal', 'start in normal mode'
    assert api('status')['runtime_state'] != 'running', 'runtime must be idle'
    plugins = api('plugins')['plugins']
    assert all(p['id'] == 'ros2_echo' or not p['enabled'] for p in plugins), 'other plugins enabled'
    example = next(p for p in plugins if p['id'] == 'ros2_echo')
    old_bindings = api('plugins/ros2_echo/ros2')['ros2']['bindings']
    old_config = original['config']['ros2']
    bindings = copy.deepcopy(old_bindings)
    prefix = '/ex_container_acceptance_' + uuid.uuid4().hex[:8]
    for port, topic in [('text_in', prefix + '/input'), ('text_out', prefix + '/output')]:
        bindings[port].update(enabled=True, topic=topic)

    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    context = Context()
    rclpy.init(context=context, domain_id=73)
    node = rclpy.create_node('host_acceptance_' + uuid.uuid4().hex[:8], context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    publisher = node.create_publisher(String, prefix + '/input', 10)
    received = []
    node.create_subscription(String, prefix + '/output', lambda msg: received.append(msg.data), 10)
    try:
        api('environments/ros2/config', {'config':{'domain_id':73, 'discovery_interval_sec':0.2}})
        save_bindings(bindings)
        api('plugins/ros2_echo/enable', {})
        switch('ros2')
        api('runtime/start', {})
        payload = 'host -> EX actor -> host ' + uuid.uuid4().hex
        message = String(); message.data = payload
        started = time.monotonic()
        while time.monotonic() - started < 15 and payload not in received:
            publisher.publish(message)
            executor.spin_once(timeout_sec=0.05)
            time.sleep(0.02)
        assert payload in received, api('environments/ros2/endpoints')
        endpoints = api('plugins/ros2_echo/ros2')['ros2']['endpoints']
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not all(p['state'] == 'ready' for p in endpoints):
            time.sleep(0.1)
            endpoints = api('plugins/ros2_echo/ros2')['ros2']['endpoints']
        assert all(p['state'] == 'ready' for p in endpoints), endpoints
        assert any(p['rx_received'] > 0 for p in endpoints)
        assert any(p['tx_published'] > 0 for p in endpoints)
        print(json.dumps({'container_external_roundtrip':True, 'ros_domain':73,
              'discovery_and_roundtrip_sec':round(time.monotonic()-started,3),
              'endpoints':endpoints}), flush=True)
    finally:
        api('runtime/stop', {'reason':'external ROS acceptance completed'})
        switch('normal')
        save_bindings(old_bindings)
        api('environments/ros2/config', {'config':old_config})
        if not example['enabled']: api('plugins/ros2_echo/disable', {})
        executor.shutdown(timeout_sec=2)
        node.destroy_node()
        rclpy.shutdown(context=context)
    assert api('environments')['environment']['active_mode'] == 'normal'
    print('RESTORED normal mode, original config/bindings, idle runtime', flush=True)


if __name__ == '__main__':
    main()
