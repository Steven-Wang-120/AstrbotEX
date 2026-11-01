"""Actual DDS/action clients against the four nodes, with fake hardware only."""
import os
os.environ.setdefault('ROS_LOCALHOST_ONLY', '1')
os.environ.setdefault('ROS_DOMAIN_ID', '172')
from pathlib import Path
import threading
import time
import yaml
import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from action_msgs.msg import GoalStatus
from jetmax_joint_interfaces.action import MoveServoRaw, SetGripperAngle
from jetmax_joint_interfaces.srv import StopJoint
from jetmax_joint_interfaces.msg import JointState
from jetmax_joint_nodes.backends import FakeBackend
from jetmax_joint_nodes.nodes import JointNode
from jetmax_joint_nodes.session import JOINT_IDS, SharedSession


def wait(future, timeout=5):
    end = time.monotonic() + timeout
    while not future.done() and time.monotonic() < end:
        time.sleep(.01)
    assert future.done(), 'ROS future timed out'
    return future.result()


def until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_four_nodes_concurrency_cancel_state_and_gripper():
    context = Context()
    rclpy.init(context=context)
    cfg = yaml.safe_load((Path(__file__).parents[1] / 'config/fake.yaml').read_text())
    session = SharedSession(FakeBackend(), cfg, control_enabled=True)
    executor = MultiThreadedExecutor(num_threads=8, context=context)
    nodes = []
    clients = []
    thread = None
    session.start()
    try:
        for name in JOINT_IDS:
            node = JointNode(name, session, context=context)
            nodes.append(node)
            executor.add_node(node)
        client_node = Node('joint_offline_test_client', context=context)
        nodes.append(client_node)
        executor.add_node(client_node)
        thread = threading.Thread(target=executor.spin, daemon=True)
        thread.start()
        states = {}
        subscriptions = [client_node.create_subscription(JointState, f'/jetmax_hw/{n}/state',
                         lambda msg: states.__setitem__(msg.joint, msg), 10) for n in JOINT_IDS]
        arm_clients = {}
        for name in list(JOINT_IDS)[:3]:
            client = ActionClient(client_node, MoveServoRaw, f'/jetmax_hw/{name}/move')
            clients.append(client)
            arm_clients[name] = client
            assert client.wait_for_server(timeout_sec=3)
        gripper = ActionClient(client_node, SetGripperAngle, '/jetmax_hw/gripper/set_angle')
        clients.append(gripper)
        assert gripper.wait_for_server(timeout_sec=3)
        until(lambda: len(states) == 4)
        assert session.backend.open_count == 1 and not session.backend.events
        graph = set(client_node.get_node_names_and_namespaces())
        assert {(n, '/jetmax_hw') for n in JOINT_IDS} <= graph
        until(lambda: states['arm_shoulder'].feedback_valid and states['arm_elbow'].feedback_valid)
        # Leave time for several sequential ROS round trips on the Nano before
        # cancellation; a one-second fake goal can finish during those checks.
        a = wait(arm_clients['arm_shoulder'].send_goal_async(MoveServoRaw.Goal(target_raw=600, duration_ms=4000)))
        b = wait(arm_clients['arm_elbow'].send_goal_async(MoveServoRaw.Goal(target_raw=650, duration_ms=400)))
        assert a.accepted and b.accepted
        busy = wait(arm_clients['arm_shoulder'].send_goal_async(MoveServoRaw.Goal(target_raw=550, duration_ms=100)))
        assert not busy.accepted
        invalid = wait(arm_clients['arm_base'].send_goal_async(MoveServoRaw.Goal(target_raw=9999, duration_ms=100)))
        assert not invalid.accepted
        until(lambda: session.snapshot('arm_shoulder')['command_written'])
        assert wait(a.cancel_goal_async()).goals_canceling
        ar, br = wait(a.get_result_async()), wait(b.get_result_async())
        assert ar.status == GoalStatus.STATUS_CANCELED and ar.result.outcome == 'cancelled'
        assert br.status == GoalStatus.STATUS_SUCCEEDED and br.result.outcome == 'target_observed'
        assert ('stop', 2) in session.backend.events and ('stop', 3) not in session.backend.events
        g = wait(gripper.send_goal_async(SetGripperAngle.Goal(target_deg=75.0)))
        gr = wait(g.get_result_async())
        assert gr.status == GoalStatus.STATUS_SUCCEEDED and gr.result.outcome == 'command_written'
        assert not gr.result.final_state.measured_angle_valid
        # Lost response / result retrieval does not issue another hardware command.
        count = len(session.backend.events)
        assert wait(g.get_result_async()).result.outcome == 'command_written'
        assert len(session.backend.events) == count
        stop = client_node.create_client(StopJoint, '/jetmax_hw/gripper/stop')
        assert stop.wait_for_service(timeout_sec=2)
        response = wait(stop.call_async(StopJoint.Request()))
        assert response.accepted and response.stop_state == 'physical_stop_unsupported'
        assert not response.physical_stop_confirmed
        stop.destroy()
        for sub in subscriptions:
            client_node.destroy_subscription(sub)
    finally:
        session.close()
        executor.shutdown(timeout_sec=3)
        if thread:
            thread.join(timeout=3)
        for client in clients:
            client.destroy()
        for node in nodes:
            node.destroy_node()
        context.shutdown()
