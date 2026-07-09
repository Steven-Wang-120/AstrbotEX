# Rescue Topic CAN Controller Plugin

This plugin subscribes to YOLO and lidar EX topics, runs the first rescue state machine, and sends CAN frames to the lower controller.

## Offline CAN

Default transport:

```text
socketcan / can0
```

SocketCAN is a local Linux CAN interface and does not require Wi-Fi, Ethernet, internet, TCP, or UDP.

Optional transport:

```text
slcan / serial CAN adapter
```

Use this only when RX/TX goes to a UART CAN controller/module that speaks the SLCAN ASCII protocol. A raw UART cannot directly drive a CAN transceiver without a CAN controller.

The serial port is configurable and defaults to:

```text
/dev/ttyS5
```

It does not hardcode `ttyS1` or `ttyS2`.

## Subscribed Topics

```text
astrbotex_ros2_vision_plugin.current_target
lidar_pose_plugin.rescue_perception
```

## Published Topics

```text
rescue_topic_can_controller_plugin.state
rescue_topic_can_controller_plugin.decision
rescue_topic_can_controller_plugin.can_tx
rescue_topic_can_controller_plugin.can_rx
```

## Ignore Own Safe Zone Targets

The controller has this Dashboard setting:

```text
ignore_own_safe_zone_targets = true
```

When `lidar_pose_plugin.rescue_perception` reports any of these fields:

```text
selected_target_ignore = true
selected_target_in_own_safe_zone = true
selected_target_ignore_reason = own_safe_zone
```

the controller does not approach or capture that target. It returns to `SCAN_TARGET` and keeps searching.

## CAN Frames

Heartbeat:

```text
CAN ID 0x100
DLC 8
[protocol_version, mode, runtime_state, seq, flags, 0, 0, 0]
```

Motion command:

```text
CAN ID 0x110
DLC 3 by default
[left_motor_cmd, right_motor_cmd, servo_target]
```

Motor values:

```text
0 STOP
1 FORWARD
2 REVERSE
```

Servo values:

```text
0 release/reset
1 capture
```

## First State Machine

```text
SCAN_TARGET
ALIGN_TO_TARGET
APPROACH_TARGET
CAPTURE_TARGET
RETURN_TURN_TO_HOME
RETURN_DRIVE_TO_HOME
DROP_TARGET
RETREAT
```

The main tunables are in Dashboard plugin config:

```text
target_x_deadband
capture_distance_mm
heading_error_threshold_deg
home_distance_threshold_mm
retreat_duration_sec
target_x_positive_turn
heading_error_positive_turn
scan_turn
ignore_own_safe_zone_targets
```
