"""Installed launch entry and CLI cancellation across processes, loopback only."""
import os
import signal
import shutil
import subprocess
import time
import rclpy
from rclpy.action import ActionClient
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from jetmax_joint_interfaces.action import MoveServoRaw


def test_launch_and_cancel_command(tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '173')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-logs'))
    ros2 = shutil.which('ros2')
    assert ros2
    context = Context()
    rclpy.init(context=context)
    node = Node('joint_launch_smoke', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    client = ActionClient(node, MoveServoRaw, '/jetmax_hw/arm_base/move')
    log_path = tmp_path / 'launch.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([ros2, 'launch', 'jetmax_joint_nodes', 'joints.launch.py',
                                    'backend:=fake', 'enable_control:=true'],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        def wait(future):
            executor.spin_until_future_complete(future, timeout_sec=5)
            assert future.done(), 'ROS request timed out'
            return future.result()
        try:
            assert client.wait_for_server(timeout_sec=8), log_path.read_text()
            expected = {(n, '/jetmax_hw') for n in ('arm_base', 'arm_shoulder', 'arm_elbow', 'gripper')}
            deadline = time.monotonic() + 3
            while not expected <= set(node.get_node_names_and_namespaces()) and time.monotonic() < deadline:
                executor.spin_once(timeout_sec=.05)
            assert expected <= set(node.get_node_names_and_namespaces())
            handle = wait(client.send_goal_async(MoveServoRaw.Goal(target_raw=700, duration_ms=2000)))
            assert handle.accepted
            result = subprocess.run([ros2, 'run', 'jetmax_joint_nodes', 'cancel_joint', 'arm_base',
                                     '--goal-id', bytes(handle.goal_id.uuid).hex()],
                                    capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stderr
            assert 'accepted_goals: 1' in result.stdout, result.stdout
            assert wait(handle.get_result_async()).result.outcome == 'cancelled'
        finally:
            client.destroy()
            executor.shutdown()
            node.destroy_node()
            context.shutdown()
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        assert process.returncode == 0, log_path.read_text()
        assert 'process has finished cleanly' in log_path.read_text()
