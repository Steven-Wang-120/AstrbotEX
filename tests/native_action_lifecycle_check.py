"""Strict ROS2 native B02 lifecycle check.

This module intentionally is not named test_*.py and has no skip/fallback path.
Run from a sourced ROS2/Humble Python 3.10 environment with domain 73:
    python tests/native_action_lifecycle_check.py

It uses a simulated controller and DDS delivery only as transport. StopEvidence is
committed by the action ledger only after the controller's simulated state changes
to stopped; DDS publication alone never releases the action resource.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.plugin_actor import PluginActor

DOMAIN = 73
NAMESPACE = "/astrbotex_b02_" + uuid.uuid4().hex[:10]


class SimulatedController:
    id = "native_action_owner"

    def __init__(self, publisher, dispatcher, binding):
        self.publisher = publisher
        self.dispatcher = dispatcher
        self.binding = binding
        self.command_id = None
        self.moving = False
        self.stopped = True

    def on_action_command(self, command):
        self.command_id = command.command_id
        self.moving = True
        self.stopped = False
        return "accepted"

    def on_action_cancel(self, command_id, reason):
        if command_id != self.command_id:
            return "rejected"
        message = String()
        message.data = "stop-requested:" + command_id
        self.publisher.publish(message)
        self.moving = False
        self.stopped = True
        self.dispatcher.report(
            command_id, self.binding, ActionStatus.CANCELED,
            stop_evidence=StopEvidence(command_id, True, "sim-controller", "dds-stopped:" + command_id),
        )
        return "requested"


def main() -> None:
    os.environ["ROS_DOMAIN_ID"] = str(DOMAIN)
    context = Context()
    rclpy.init(context=context, domain_id=DOMAIN)
    executor = SingleThreadedExecutor(context=context)
    temp = tempfile.TemporaryDirectory()
    ledger = ActionLedger(os.path.join(temp.name, "actions.sqlite3"))
    dispatcher = ActionDispatcher(ledger)
    binding = OwnerBinding("native_action_owner", 1)
    node = rclpy.create_node("b02_owner_" + uuid.uuid4().hex[:8], namespace=NAMESPACE, context=context)
    peer = rclpy.create_node("b02_peer_" + uuid.uuid4().hex[:8], namespace=NAMESPACE, context=context)
    executor.add_node(node)
    executor.add_node(peer)
    received = []
    peer.create_subscription(String, NAMESPACE + "/stop", lambda msg: received.append(msg.data), 10)
    publisher = node.create_publisher(String, NAMESPACE + "/stop", 10)
    controller = SimulatedController(publisher, dispatcher, binding)
    actor = PluginActor(controller)
    actor.start()
    try:
        dispatcher.register_owner(binding, actor, {
            "id": "native_action_owner", "action_api_version": 2,
            "actions": [{"action_id": "native_action_owner.move.v2", "description": "move",
                         "schema": {"type": "object", "properties": {}, "additionalProperties": False},
                         "resources": ["sim-motion"], "operations": ["start", "cancel"],
                         "cancel_timeout_ms": 1000}],
        })
        dispatcher.update_versions(catalog_revision=1, config_revision=1,
                                   environment_revision=1, runtime_state="ready")
        dispatcher.set_gate(True)
        dispatcher.update_context(
            ex_session="native-session", goal_id="native-goal", goal_revision=1,
            task_id="native-task", allowed_actions=["native_action_owner.move.v2"],
            bound_params={"native_action_owner.move.v2": {}}, runtime_state="ready",
            catalog_revision=1, config_revision=1, environment_revision=1, ttl_ms=10_000)
        command = {"schema_version": 1, "command_id": "native-command",
                   "ex_session": "native-session", "goal_id": "native-goal", "goal_revision": 1,
                   "decision_id": "native-decision", "owner": "native_action_owner", "plugin_generation": 1,
                   "action_id": "native_action_owner.move.v2", "operation": "start", "params": {}, "lease_ms": 5000}
        dispatcher.start(command).result(5)
        deadline = time.monotonic() + 2
        while not controller.moving and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)
        assert controller.moving, "simulated action did not start"
        dispatcher.cancel("native-command", binding, "native stop").result(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)
            snapshot = dispatcher.query("native-command").result(2)
            if snapshot and snapshot.status == ActionStatus.CANCELED:
                break
        snapshot = dispatcher.query("native-command").result(2)
        assert received, "DDS stop message was not delivered"
        assert controller.stopped, "DDS delivery did not transition simulated controller"
        assert snapshot.status == ActionStatus.CANCELED, snapshot
        assert snapshot.held_resources == (), snapshot
        print("native_action_lifecycle_check: PASS")
    finally:
        dispatcher.close()
        actor.stop(timeout=3)
        ledger.close()
        node.destroy_node()
        peer.destroy_node()
        executor.shutdown(timeout_sec=2)
        rclpy.shutdown(context=context)
        temp.cleanup()


if __name__ == "__main__":
    main()
