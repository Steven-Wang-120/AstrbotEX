"""Four graph nodes; no node opens a device or owns a private bus client."""
import argparse
import signal
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from jetmax_joint_interfaces.action import MoveServoRaw, SetGripperAngle
from jetmax_joint_interfaces.msg import JointState
from jetmax_joint_interfaces.srv import StopJoint

from .session import JOINT_IDS, SUCCESS, SharedSession


def state_message(snapshot):
    msg = JointState()
    now = time.time_ns()
    msg.published_at.sec, msg.published_at.nanosec = divmod(now, 1_000_000_000)
    receipt = snapshot['received_wall_ns']
    msg.received_at.sec, msg.received_at.nanosec = divmod(receipt, 1_000_000_000)
    for key, value in snapshot.items():
        if key != 'received_wall_ns':
            setattr(msg, key, value)
    return msg


class JointNode(Node):
    def __init__(self, name, session, *, context=None):
        super().__init__(name, namespace='/jetmax_hw', context=context)
        self.joint = name
        self.session = session
        self.guard = threading.RLock()
        self.pending = None
        self.operations = {}
        self.action_cancels = set()
        self.group = ReentrantCallbackGroup()
        self.action_type = SetGripperAngle if name == 'gripper' else MoveServoRaw
        self.publisher = self.create_publisher(JointState, f'{name}/state', 10)
        self.timer = self.create_timer(.05, self.publish_state, callback_group=self.group)
        self.service = self.create_service(StopJoint, f'{name}/stop', self.stop_callback, callback_group=self.group)
        suffix = 'set_angle' if name == 'gripper' else 'move'
        self.server = ActionServer(self, self.action_type, f'{name}/{suffix}', self.execute_callback,
                                   goal_callback=self.goal_callback, cancel_callback=self.cancel_callback,
                                   handle_accepted_callback=self.accepted_callback, callback_group=self.group,
                                   result_timeout=60)

    def publish_state(self):
        self.publisher.publish(state_message(self.session.snapshot(self.joint)))

    def goal_callback(self, request):
        with self.guard:
            # Reserve during acceptance, not later in execute: simultaneous requests
            # for one joint cannot both be accepted before the executor schedules them.
            if self.pending is not None:
                return GoalResponse.REJECT
            try:
                if self.joint == 'gripper':
                    self.pending = self.session.reserve(self.joint, request.target_deg)
                else:
                    self.pending = self.session.reserve(self.joint, request.target_raw, request.duration_ms)
            except ValueError as exc:
                self.get_logger().warning('Goal rejected: ' + str(exc))
                return GoalResponse.REJECT
            return GoalResponse.ACCEPT

    def accepted_callback(self, handle):
        with self.guard:
            op, self.pending = self.pending, None
            key = bytes(handle.goal_id.uuid).hex()
            self.operations[key] = op
            self.session.activate(op, key)
        handle.execute()

    def cancel_callback(self, handle):
        with self.guard:
            op = self.operations.get(bytes(handle.goal_id.uuid).hex())
            accepted = op is not None and self.session.cancel(op)
            if accepted:
                self.action_cancels.add(bytes(handle.goal_id.uuid).hex())
            return CancelResponse.ACCEPT if accepted else CancelResponse.REJECT

    def stop_callback(self, request, response):
        response.accepted, response.message = self.session.stop(self.joint)
        state = self.session.snapshot(self.joint)
        response.goal_id = state['goal_id']
        response.stop_state = state['stop_state']
        response.physical_stop_confirmed = False
        return response

    def execute_callback(self, handle):
        key = bytes(handle.goal_id.uuid).hex()
        with self.guard:
            op = self.operations[key]
        while not op.done.wait(.05):
            if not self.context.ok():
                self.session.cancel(op)
                break
            feedback = self.action_type.Feedback()
            feedback.state = state_message(self.session.snapshot(self.joint))
            handle.publish_feedback(feedback)
        result = self.action_type.Result()
        result.outcome, result.message = op.outcome or 'shutdown', op.message
        result.final_state = state_message(op.final_state or self.session.snapshot(self.joint))
        with self.guard:
            action_cancel = key in self.action_cancels
        if result.outcome == 'cancelled' and action_cancel:
            # rclpy transitions to CANCELING after cancel_callback returns. A
            # very fast STOP must not race that transition and abort the goal.
            deadline = time.monotonic() + .5
            while self.context.ok() and not handle.is_cancel_requested and time.monotonic() < deadline:
                time.sleep(.001)
        if self.context.ok():
            if result.outcome == 'cancelled' and handle.is_cancel_requested:
                handle.canceled()
            elif result.outcome in SUCCESS:
                handle.succeed()
            else:
                # A stop service does not put a ROS goal into CANCELING; abort is
                # the valid ROS transition, with outcome=cancelled in our result.
                handle.abort()
        with self.guard:
            self.operations.pop(key, None)
            self.action_cancels.discard(key)
        return result

    def destroy_node(self):
        self.server.destroy()
        return super().destroy_node()


def main(args=None):
    import yaml
    from ament_index_python.packages import get_package_share_directory
    from pathlib import Path
    from rclpy.utilities import remove_ros_args
    from rclpy.signals import SignalHandlerOptions
    from .backends import FakeBackend, RealBackend
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=('real', 'fake'), default='real')
    parser.add_argument('--config')
    parser.add_argument('--enable-control', action='store_true')
    clean = remove_ros_args(args=args)[1:]
    options = parser.parse_args(clean)
    path = Path(options.config) if options.config else Path(get_package_share_directory('jetmax_joint_nodes')) / 'config' / (options.backend + '.yaml')
    config = yaml.safe_load(path.read_text())
    backend = FakeBackend() if options.backend == 'fake' else RealBackend(config)
    session = SharedSession(backend, config, control_enabled=options.enable_control)
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    executor = MultiThreadedExecutor(num_threads=8)
    nodes = []
    quitting = threading.Event()
    previous = {s: signal.signal(s, lambda *_: quitting.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        session.start()
        for name in JOINT_IDS:
            node = JointNode(name, session)
            nodes.append(node)
            executor.add_node(node)
        nodes[0].get_logger().info(f'backend={options.backend}; control_enabled={options.enable_control}; four nodes, one bus owner')
        while rclpy.ok() and not quitting.is_set():
            executor.spin_once(timeout_sec=.1)
    finally:
        if not session.close() and nodes:
            nodes[0].get_logger().error('I/O still in flight; physical state unknown; restart must not replay goals')
        executor.shutdown(timeout_sec=2)
        for node in nodes:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    main()
