from __future__ import annotations

import json
import math
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from astrbot_ex.core.models import Intent, RobotState

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    serial = None


MODE_VALUE = {
    "IDLE": 0,
    "AUTO": 1,
    "MANUAL": 2,
    "TEST": 3,
}

STATE_VALUE = {
    "STOPPED": 0,
    "RUNNING": 1,
}

MOTOR_STOP = 0
MOTOR_FORWARD = 1
MOTOR_REVERSE = 2


@dataclass(slots=True)
class CanFrame:
    can_id: int
    data: bytes


class CanTransport:
    def open(self) -> None: ...

    def close(self) -> None: ...

    def send(self, frame: CanFrame) -> None: ...

    def recv(self, timeout_sec: float = 0.0) -> CanFrame | None: ...


class DryRunTransport:
    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, frame: CanFrame) -> None:
        pass

    def recv(self, timeout_sec: float = 0.0) -> CanFrame | None:
        return None


class SocketCanTransport:
    CAN_EFF_FLAG = 0x80000000

    def __init__(self, interface: str) -> None:
        self.interface = interface
        self.sock: socket.socket | None = None

    def open(self) -> None:
        sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        sock.bind((self.interface,))
        sock.setblocking(False)
        self.sock = sock

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()
        finally:
            self.sock = None

    def send(self, frame: CanFrame) -> None:
        if self.sock is None:
            raise RuntimeError("SocketCAN transport is not open")
        data = bytes(frame.data[:8])
        packet = struct.pack("=IB3x8s", frame.can_id, len(data), data.ljust(8, b"\x00"))
        self.sock.send(packet)

    def recv(self, timeout_sec: float = 0.0) -> CanFrame | None:
        if self.sock is None:
            return None
        if timeout_sec > 0:
            self.sock.settimeout(timeout_sec)
        else:
            self.sock.setblocking(False)
        try:
            packet = self.sock.recv(16)
        except (BlockingIOError, TimeoutError, socket.timeout):
            return None
        can_id, dlc, data = struct.unpack("=IB3x8s", packet)
        return CanFrame(can_id=can_id & 0x1FFFFFFF, data=bytes(data[:dlc]))


class SlcanTransport:
    def __init__(self, port: str, baudrate: int, setup_enabled: bool, bitrate_code: str) -> None:
        self.port = port
        self.baudrate = baudrate
        self.setup_enabled = setup_enabled
        self.bitrate_code = bitrate_code
        self.ser: Any = None

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed; install it or use socketcan mode")
        self.ser = serial.Serial(self.port, self.baudrate, timeout=0, write_timeout=0.2)
        if self.setup_enabled:
            self.ser.write(b"C\r")
            self.ser.write((self.bitrate_code.strip() + "\r").encode("ascii"))
            self.ser.write(b"O\r")

    def close(self) -> None:
        if self.ser is None:
            return
        try:
            if self.setup_enabled:
                self.ser.write(b"C\r")
            self.ser.close()
        finally:
            self.ser = None

    def send(self, frame: CanFrame) -> None:
        if self.ser is None:
            raise RuntimeError("SLCAN transport is not open")
        data = bytes(frame.data[:8])
        line = f"t{frame.can_id:03X}{len(data):1X}{data.hex().upper()}\r"
        self.ser.write(line.encode("ascii"))

    def recv(self, timeout_sec: float = 0.0) -> CanFrame | None:
        if self.ser is None:
            return None
        deadline = time.time() + timeout_sec
        buffer = bytearray()
        while True:
            chunk = self.ser.read(1)
            if chunk:
                if chunk == b"\r":
                    break
                buffer.extend(chunk)
                continue
            if timeout_sec <= 0 or time.time() >= deadline:
                return None
            time.sleep(0.001)
        try:
            text = buffer.decode("ascii")
            if not text or text[0] not in {"t", "T"}:
                return None
            if text[0] == "t":
                can_id = int(text[1:4], 16)
                dlc = int(text[4], 16)
                data_hex = text[5:5 + dlc * 2]
            else:
                can_id = int(text[1:9], 16)
                dlc = int(text[9], 16)
                data_hex = text[10:10 + dlc * 2]
            return CanFrame(can_id=can_id, data=bytes.fromhex(data_hex))
        except Exception:
            return None


class Plugin:
    id = "rescue_topic_can_controller_plugin"
    name = "Rescue Topic CAN Controller Plugin"

    def __init__(self, context) -> None:
        self.context = context
        self.config = dict(context.config or {})
        self.enabled = False
        self._transport: CanTransport | None = None
        self._state = "SCAN_TARGET"
        self._state_entered_at = time.time()
        self._heartbeat_seq = 0
        self._command_seq = 0
        self._last_heartbeat_at = 0.0
        self._last_command_at = 0.0
        self._last_motion = (MOTOR_STOP, MOTOR_STOP, self._servo_release())
        self._last_note = "init"
        self._link_ok = False
        self._last_can_rx: dict[str, Any] | None = None

    def on_load(self) -> None:
        self.context.event_bus.emit("plugin", "rescue topic CAN controller loaded", plugin=self.id)

    def on_enable(self) -> None:
        self.enabled = True

    def on_disable(self) -> None:
        self.enabled = False
        self._close_transport("plugin disabled")

    def on_unload(self) -> None:
        self.enabled = False
        self._close_transport("plugin unloaded")

    def on_runtime_start(self) -> None:
        try:
            self._open_transport()
        except Exception as exc:
            self._link_ok = False
            self.context.event_bus.emit(
                "control",
                "CAN transport open failed",
                plugin=self.id,
                error=str(exc),
            )
        self._state = "SCAN_TARGET"
        self._state_entered_at = time.time()
        self._last_note = "runtime started"

    def on_runtime_stop(self, reason: str) -> None:
        self.stop(reason)
        self._close_transport(reason)

    def on_tick(self, world) -> None:
        if not self.enabled:
            return
        now = time.time()
        try:
            self._ensure_transport()
        except Exception as exc:
            self._link_ok = False
            self.context.event_bus.emit_throttled(
                "control",
                "CAN transport open failed",
                interval_sec=1.0,
                key=f"{self.id}:can_open_failed",
                plugin=self.id,
                error=str(exc),
            )
        self._send_heartbeat_if_due(now, running=True)
        self._run_state_machine(now)

    def on_worker_step(self) -> None:
        transport = self._transport
        if transport is None:
            time.sleep(0.02)
            return
        try:
            frame = transport.recv(timeout_sec=0.02)
        except Exception as exc:
            self._link_ok = False
            self.context.event_bus.emit_throttled(
                "control",
                "CAN receive failed",
                interval_sec=1.0,
                key=f"{self.id}:can_receive_failed",
                plugin=self.id,
                error=str(exc),
            )
            time.sleep(0.02)
            return
        if frame is None:
            time.sleep(0.005)
            return
        payload = self._frame_payload(frame, kind="rx", note="")
        self._last_can_rx = payload
        self._link_ok = True
        self._publish_can("rescue_topic_can_controller_plugin.can_rx", frame, kind="rx", note="")

    def send(self, intent: Intent) -> None:
        # Runtime compatibility hook. The rescue first version is topic-driven,
        # so this only honors explicit stop intents from other runtime layers.
        if intent.motion is not None and intent.motion.vx == 0 and intent.motion.wz == 0:
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._last_motion[2], "runtime zero intent")

    def stop(self, reason: str) -> None:
        self._last_note = reason
        self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._last_motion[2], reason, force=True)
        self._send_heartbeat_if_due(time.time(), running=False, force=True)

    def read_state(self) -> RobotState:
        return RobotState(
            link_ok=self._link_ok,
            metadata={
                "source": self.id,
                "controller_state": self._state,
                "last_motion": self._last_motion,
                "last_note": self._last_note,
                "can_rx": self._last_can_rx,
            },
        )

    def _run_state_machine(self, now: float) -> None:
        target, target_stale = self._latest_topic(str(self.config.get("vision_target_topic", "")), int(self.config.get("vision_stale_ms", 700)), now)
        perception, perception_stale = self._latest_topic(str(self.config.get("perception_topic", "")), int(self.config.get("perception_stale_ms", 900)), now)

        if bool(self.config.get("stop_on_boundary_risk", True)) and perception and perception.get("boundary_risk"):
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._last_motion[2], "boundary risk")
            self._publish_state(now, target, perception, "STOP_BOUNDARY")
            return

        if self._state in {"CAPTURE_TARGET", "DROP_TARGET", "RETREAT"}:
            self._run_timed_state(now, target, perception)
            return

        if self._state in {"RETURN_TURN_TO_HOME", "RETURN_DRIVE_TO_HOME"}:
            self._run_return_state(now, target, perception, perception_stale)
            return

        if not perception_stale and self._perception_ignores_target(perception):
            self._transition("SCAN_TARGET", now, "selected target ignored")
            left, right = self._turn_pair(str(self.config.get("scan_turn", "right")))
            self._queue_motion(left, right, self._servo_release(), "ignore target in own safe zone")
            self._publish_state(now, target, perception, "IGNORE_OWN_SAFE_ZONE_TARGET")
            return

        if target_stale or not self._target_valid(target):
            self._transition("SCAN_TARGET", now, "no valid target")
            left, right = self._turn_pair(str(self.config.get("scan_turn", "right")))
            self._queue_motion(left, right, self._servo_release(), "scan target")
            self._publish_state(now, target, perception, "SCAN_TARGET")
            return

        target_x = self._parse_float(target.get("target_x", target.get("x"))) if target else None
        distance = self._target_distance(target, perception)
        deadband = float(self.config.get("target_x_deadband", 0.1))
        capture_distance = float(self.config.get("capture_distance_mm", 120.0))

        if distance is not None and distance <= capture_distance:
            self._transition("CAPTURE_TARGET", now, "target in capture distance")
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_capture(), "capture target")
            self._publish_state(now, target, perception, "CAPTURE_TARGET")
            return

        if target_x is None or abs(target_x) > deadband:
            self._transition("ALIGN_TO_TARGET", now, "target not aligned")
            turn_direction = self._turn_direction_for_target_x(target_x or 0.0)
            left, right = self._turn_pair(turn_direction)
            self._queue_motion(left, right, self._servo_release(), "align target")
            self._publish_state(now, target, perception, "ALIGN_TO_TARGET")
            return

        self._transition("APPROACH_TARGET", now, "target aligned")
        self._queue_motion(MOTOR_FORWARD, MOTOR_FORWARD, self._servo_release(), "approach target")
        self._publish_state(now, target, perception, "APPROACH_TARGET")

    def _run_timed_state(self, now: float, target: dict[str, Any] | None, perception: dict[str, Any] | None) -> None:
        elapsed = now - self._state_entered_at
        if self._state == "CAPTURE_TARGET":
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_capture(), "capture hold")
            if elapsed >= float(self.config.get("capture_hold_sec", 0.8)):
                self._transition("RETURN_TURN_TO_HOME", now, "capture done")
            self._publish_state(now, target, perception, self._state)
            return
        if self._state == "DROP_TARGET":
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_release(), "drop hold")
            if elapsed >= float(self.config.get("drop_hold_sec", 0.8)):
                self._transition("RETREAT", now, "drop done")
            self._publish_state(now, target, perception, self._state)
            return
        if self._state == "RETREAT":
            self._queue_motion(MOTOR_REVERSE, MOTOR_REVERSE, self._servo_release(), "retreat")
            if elapsed >= float(self.config.get("retreat_duration_sec", 3.0)):
                self._transition("SCAN_TARGET", now, "retreat done")
            self._publish_state(now, target, perception, self._state)

    def _run_return_state(
        self,
        now: float,
        target: dict[str, Any] | None,
        perception: dict[str, Any] | None,
        perception_stale: bool,
    ) -> None:
        if perception_stale or not perception or not perception.get("pose_valid"):
            self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_capture(), "pose unavailable for return")
            self._publish_state(now, target, perception, "RETURN_WAIT_POSE")
            return

        heading_error = self._parse_float(perception.get("heading_error_to_home_rad"))
        home_distance = self._parse_float(perception.get("home_distance_mm"))
        heading_threshold = math.radians(float(self.config.get("heading_error_threshold_deg", 8.0)))
        home_threshold = float(self.config.get("home_distance_threshold_mm", 250.0))

        if self._state == "RETURN_TURN_TO_HOME":
            if heading_error is None:
                self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_capture(), "heading unavailable")
            elif abs(heading_error) > heading_threshold:
                direction = self._turn_direction_for_heading_error(heading_error)
                left, right = self._turn_pair(direction)
                self._queue_motion(left, right, self._servo_capture(), "turn to home")
            else:
                self._transition("RETURN_DRIVE_TO_HOME", now, "heading aligned")
                self._queue_motion(MOTOR_FORWARD, MOTOR_FORWARD, self._servo_capture(), "drive home")
            self._publish_state(now, target, perception, self._state)
            return

        if self._state == "RETURN_DRIVE_TO_HOME":
            if home_distance is not None and home_distance <= home_threshold:
                self._transition("DROP_TARGET", now, "home reached")
                self._queue_motion(MOTOR_STOP, MOTOR_STOP, self._servo_release(), "drop target")
            else:
                self._queue_motion(MOTOR_FORWARD, MOTOR_FORWARD, self._servo_capture(), "drive home")
            self._publish_state(now, target, perception, self._state)

    def _transition(self, state: str, now: float, reason: str) -> None:
        if self._state == state:
            return
        self._state = state
        self._state_entered_at = now
        self.context.event_bus.emit("decision", "controller state changed", plugin=self.id, state=state, reason=reason)

    def _queue_motion(self, left: int, right: int, servo_target: int, note: str, *, force: bool = False) -> None:
        now = time.time()
        command_hz = max(1.0, float(self.config.get("command_hz", 20)))
        motion = (int(left), int(right), int(servo_target))
        if not force and motion == self._last_motion and now - self._last_command_at < 1.0 / command_hz:
            return
        self._last_motion = motion
        self._last_note = note
        self._last_command_at = now
        dlc = int(self.config.get("motion_dlc", 3))
        data = bytes([motion[0], motion[1], motion[2]])
        if dlc == 8:
            data = data + b"\x00\x00\x00\x00\x00"
        self._send_can(CanFrame(self._can_id("can_id_motion", 0x110), data), "motion", note)

    def _send_heartbeat_if_due(self, now: float, *, running: bool, force: bool = False) -> None:
        heartbeat_hz = max(1.0, float(self.config.get("heartbeat_hz", 10)))
        if not force and now - self._last_heartbeat_at < 1.0 / heartbeat_hz:
            return
        self._last_heartbeat_at = now
        flags = 0
        if bool(self.config.get("enable_motion", True)):
            flags |= 0x01
        if bool(self.config.get("enable_servo", True)):
            flags |= 0x02
        mode = MODE_VALUE.get(str(self.config.get("mode", "AUTO")).strip().upper(), 1)
        runtime_state = STATE_VALUE["RUNNING" if running else "STOPPED"]
        data = bytes([
            int(self.config.get("protocol_version", 1)) & 0xFF,
            mode & 0xFF,
            runtime_state & 0xFF,
            self._heartbeat_seq & 0xFF,
            flags & 0xFF,
            0,
            0,
            0,
        ])
        self._heartbeat_seq = (self._heartbeat_seq + 1) & 0xFF
        self._send_can(CanFrame(self._can_id("can_id_heartbeat", 0x100), data), "heartbeat", "heartbeat")

    def _send_can(self, frame: CanFrame, kind: str, note: str) -> None:
        try:
            self._ensure_transport()
            if not bool(self.config.get("dry_run", False)) and self._transport is not None:
                self._transport.send(frame)
            self._link_ok = True
            self._publish_can("rescue_topic_can_controller_plugin.can_tx", frame, kind=kind, note=note)
        except Exception as exc:
            self._link_ok = False
            self.context.event_bus.emit_throttled(
                "control",
                "CAN send failed",
                interval_sec=1.0,
                key=f"{self.id}:can_send_failed",
                plugin=self.id,
                error=str(exc),
            )

    def _ensure_transport(self) -> None:
        if bool(self.config.get("dry_run", False)) or str(self.config.get("can_transport", "")).lower() == "dry_run":
            if self._transport is None:
                self._transport = DryRunTransport()
                self._transport.open()
                self._link_ok = True
            return
        if self._transport is None:
            self._open_transport()

    def _open_transport(self) -> None:
        self._close_transport("reopen")
        mode = str(self.config.get("can_transport", "socketcan")).strip().lower()
        if bool(self.config.get("dry_run", False)) or mode == "dry_run":
            transport: CanTransport = DryRunTransport()
        elif mode == "socketcan":
            transport = SocketCanTransport(str(self.config.get("socketcan_interface", "can0")).strip() or "can0")
        elif mode == "slcan":
            transport = SlcanTransport(
                str(self.config.get("serial_port", "/dev/ttyS5")).strip(),
                int(self.config.get("serial_baudrate", 1000000)),
                bool(self.config.get("slcan_setup_enabled", False)),
                str(self.config.get("slcan_bitrate_code", "S6")),
            )
        else:
            raise ValueError(f"unsupported can_transport: {mode}")
        transport.open()
        self._transport = transport
        self._link_ok = True
        self.context.event_bus.emit("control", "CAN transport opened", plugin=self.id, transport=mode)

    def _close_transport(self, reason: str) -> None:
        transport = self._transport
        self._transport = None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        self._link_ok = False

    def _publish_state(
        self,
        now: float,
        target: dict[str, Any] | None,
        perception: dict[str, Any] | None,
        decision: str,
    ) -> None:
        pubsub = dict(self.config.get("pubsub", {}) or {})
        if not bool(pubsub.get("publish_enabled", True)):
            return
        enabled = set(str(item) for item in pubsub.get("enabled_topics", []))
        state_payload = {
            "timestamp": now,
            "state": self._state,
            "state_elapsed_sec": round(now - self._state_entered_at, 3),
            "last_motion": {
                "left": self._last_motion[0],
                "right": self._last_motion[1],
                "servo": self._last_motion[2],
            },
            "last_note": self._last_note,
            "link_ok": self._link_ok,
        }
        decision_payload = {
            "timestamp": now,
            "decision": decision,
            "target": target,
            "perception": perception,
            "state": self._state,
        }
        if "rescue_topic_can_controller_plugin.state" in enabled:
            self.context.topic_bus.publish_payload(
                "rescue_topic_can_controller_plugin.state",
                timestamp=now,
                source=self.id,
                payload=state_payload,
            )
        if "rescue_topic_can_controller_plugin.decision" in enabled:
            self.context.topic_bus.publish_payload(
                "rescue_topic_can_controller_plugin.decision",
                timestamp=now,
                source=self.id,
                payload=decision_payload,
            )

    def _publish_can(self, topic: str, frame: CanFrame, *, kind: str, note: str) -> None:
        pubsub = dict(self.config.get("pubsub", {}) or {})
        if not bool(pubsub.get("publish_enabled", True)):
            return
        enabled = set(str(item) for item in pubsub.get("enabled_topics", []))
        if topic not in enabled:
            return
        now = time.time()
        self.context.topic_bus.publish_payload(
            topic,
            timestamp=now,
            source=self.id,
            payload=self._frame_payload(frame, kind=kind, note=note),
        )

    def _frame_payload(self, frame: CanFrame, *, kind: str, note: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "can_id": f"0x{frame.can_id:X}",
            "dlc": len(frame.data),
            "data": list(frame.data),
            "data_hex": frame.data.hex().upper(),
            "note": note,
        }

    def _latest_topic(self, topic: str, stale_ms: int, now: float) -> tuple[dict[str, Any] | None, bool]:
        if not topic:
            return None, True
        message = self.context.topic_bus.get_latest(topic)
        if message is None:
            return None, True
        return dict(message.payload), now - message.timestamp > max(1, stale_ms) / 1000.0

    def _target_valid(self, payload: dict[str, Any] | None) -> bool:
        if not payload:
            return False
        raw_x = payload.get("target_x", payload.get("x"))
        if isinstance(raw_x, str) and raw_x.strip().lower() == "empty":
            return False
        if payload.get("target_valid") is False:
            return False
        color = str(payload.get("color", "")).strip().lower()
        if color in {"empty", "none", "null"}:
            return False
        return self._parse_float(raw_x) is not None

    def _perception_ignores_target(self, perception: dict[str, Any] | None) -> bool:
        if not bool(self.config.get("ignore_own_safe_zone_targets", True)):
            return False
        if not perception:
            return False
        return bool(
            perception.get("selected_target_ignore")
            or perception.get("selected_target_in_own_safe_zone")
            or perception.get("selected_target_ignore_reason") == "own_safe_zone"
        )

    def _target_distance(self, target: dict[str, Any] | None, perception: dict[str, Any] | None) -> float | None:
        if perception:
            value = self._parse_float(perception.get("selected_target_distance_mm"))
            if value is not None:
                return value
        if target:
            value = self._parse_float(target.get("target_distance", target.get("distance")))
            if value is not None:
                return value
        return None

    def _turn_direction_for_target_x(self, target_x: float) -> str:
        positive = str(self.config.get("target_x_positive_turn", "right")).strip().lower()
        negative = "left" if positive == "right" else "right"
        return positive if target_x > 0 else negative

    def _turn_direction_for_heading_error(self, heading_error: float) -> str:
        positive = str(self.config.get("heading_error_positive_turn", "left")).strip().lower()
        negative = "right" if positive == "left" else "left"
        return positive if heading_error > 0 else negative

    @staticmethod
    def _turn_pair(direction: str) -> tuple[int, int]:
        if direction.strip().lower() == "left":
            return MOTOR_REVERSE, MOTOR_FORWARD
        return MOTOR_FORWARD, MOTOR_REVERSE

    def _servo_release(self) -> int:
        return int(self.config.get("servo_release_value", 0)) & 0xFF

    def _servo_capture(self) -> int:
        return int(self.config.get("servo_capture_value", 1)) & 0xFF

    def _can_id(self, key: str, default: int) -> int:
        raw = self.config.get(key, default)
        if isinstance(raw, int):
            return raw
        return int(str(raw), 0)

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number
