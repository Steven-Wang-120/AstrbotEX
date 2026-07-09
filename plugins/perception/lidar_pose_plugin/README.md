# Lidar Pose Perception Plugin

This perception plugin can directly subscribe ROS2 topics, or receive the same data through Unix Socket IPC when AstrBotEX runs in a container without ROS2 Python packages.

## Inputs

Default direct ROS2 mode:

```text
/scan                    sensor_msgs/msg/LaserScan
/astrbotex/robot_pose    geometry_msgs/msg/Pose2D
```

Supported pose topic message types:

```text
geometry_msgs/msg/Pose2D
geometry_msgs/msg/PoseStamped
nav_msgs/msg/Odometry
std_msgs/msg/String JSON
```

It also reads the latest EX topic from the YOLO plugin:

```text
astrbotex_ros2_vision_plugin.current_target
```

## Fusion Rule

YOLO selects which target is active. Lidar only supplies the range around that visual direction.

```text
lidar_angle = target_x * camera_hfov / 2 * camera_x_to_lidar_angle_sign
            + camera_to_lidar_yaw_offset
```

Default sign is `-1` because camera `target_x > 0` normally means image right, while ROS `LaserScan` positive angle normally points left.

## Published Topics

```text
lidar_pose_plugin.pose
lidar_pose_plugin.selected_target_range
lidar_pose_plugin.boundary
lidar_pose_plugin.rescue_perception
lidar_pose_plugin.raw_packet
```

The CAN controller should usually subscribe to:

```text
lidar_pose_plugin.rescue_perception
```

Important payload fields:

```text
pose_valid
robot_x_mm
robot_y_mm
robot_yaw_rad
home_distance_mm
heading_error_to_home_rad
selected_target_distance_mm
selected_target_world_x_mm
selected_target_world_y_mm
selected_target_in_own_safe_zone
selected_target_ignore
selected_target_ignore_reason
front_distance_mm
boundary_risk
```

## Own Safe Zone Ignore

The plugin can mark the YOLO-selected target as ignored when its estimated field coordinate is inside the own safe zone.

Default safe-zone config follows the race parameter document:

```text
own safe zone inner size: 600mm x 300mm
(0,0): midpoint of the own safe-zone outer-side edge
default rectangle:
  x = 0mm ~ 300mm
  y = -300mm ~ 300mm
margin = 20mm
```

Dashboard config keys:

```text
ignore_own_safe_zone_targets
own_safe_zone_min_x_mm
own_safe_zone_max_x_mm
own_safe_zone_min_y_mm
own_safe_zone_max_y_mm
own_safe_zone_margin_mm
target_projection_angle_mode
```

If the selected target is inside that rectangle, `lidar_pose_plugin.rescue_perception` includes:

```json
{
  "selected_target_valid": false,
  "selected_target_detected": true,
  "selected_target_in_own_safe_zone": true,
  "selected_target_ignore": true,
  "selected_target_ignore_reason": "own_safe_zone"
}
```

## IPC JSON Shape

When using `ipc_unix_socket`, send newline-delimited JSON:

```json
{
  "timestamp": 1720000000.123,
  "frame_id": "lidar_front",
  "robot_x_mm": 1000.0,
  "robot_y_mm": 500.0,
  "robot_yaw_rad": 1.57,
  "ranges_m": [1.2, 1.18, 0.9],
  "angle_min_rad": -3.14159,
  "angle_increment_rad": 0.00436,
  "range_min_m": 0.05,
  "range_max_m": 8.0
}
```
