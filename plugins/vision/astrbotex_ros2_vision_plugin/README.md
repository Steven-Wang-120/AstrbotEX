# AstrBotEX ROS2 Vision Plugin

视觉分类插件。用于接入 YOLO 视觉结果，按颜色过滤后转换为 AstrBotEX 的 `VisionResult`，并发布 EX Topic。

插件支持两种输入模式：

```text
ros2             插件直接订阅 ROS2 topic，需要 AstrBotEX 运行环境安装 rclpy / std_msgs。
ipc_unix_socket  插件监听本地 Unix Domain Socket，由宿主机 bridge 订阅 ROS2 后写入 JSON；容器内不需要 ROS2，也不走 TCP/IP。
```

当前默认模式是 `ipc_unix_socket`。

## 功能

- 订阅 ROS2 topic，或通过本地 Unix Socket 接收桥接消息。
- 支持 `std_msgs/msg/String` JSON 主路径。
- 兼容 `vision_msgs/msg/Detection2DArray` 基础字段解析。
- 将视觉消息转换为 `VisionResult`。
- 发布 EX Topic：
  - `astrbotex_ros2_vision_plugin.current_target`
  - `astrbotex_ros2_vision_plugin.detections`
  - `astrbotex_ros2_vision_plugin.raw_packet`
- Dashboard 可配置输入模式、订阅 topic、IPC socket 路径、关注颜色、过期时间、置信度阈值和 EX Topic 发布开关。

## 推荐 ROS2 JSON

第一版推荐 YOLO 节点发布 `std_msgs/msg/String`，内容为 JSON：

```json
{
  "target_valid": true,
  "color": "red",
  "target_x": -0.25,
  "target_distance": 650,
  "confidence": 0.92,
  "bbox_xyxy": [280, 180, 340, 240],
  "frame_id": "camera_front",
  "timestamp": 1720000000.123
}
```

也支持数组形式：

```json
{
  "frame_id": "camera_front",
  "objects": [
    {
      "track_id": "ball-1",
      "class": "ball",
      "color": "red",
      "target_x": -0.25,
      "distance": 650,
      "score": 0.92,
      "bbox_xyxy": [280, 180, 340, 240]
    }
  ]
}
```

## 颜色过滤

Dashboard 中 `focus_color` 只能选择：

```text
red
blue
```

过滤规则：

```text
选 red：转发 red / yellow / black / empty，忽略 blue。
选 blue：转发 blue / yellow / black / empty，忽略 red。
```

`yellow`、`black`、`empty` 默认始终转发。

## 推荐部署方式

如果 AstrBotEX 继续运行在 Docker 里，而 ROS2 / YOLO / 雷达运行在宿主机上，推荐使用：

```text
ROS2 YOLO Node
  -> /astrbotex/vision_target
  -> tools/ros2_to_ipc_bridge.py
  -> Unix Socket
  -> astrbotex_ros2_vision_plugin
  -> VisionResult / EX Topic
```

插件配置：

```json
{
  "input_mode": "ipc_unix_socket",
  "ipc_socket_path": "/app/data/ipc/astrbotex_vision.sock"
}
```

宿主机 bridge 连接的是同一个 Docker 挂载目录下的 socket。当前香橙派部署默认可用：

```bash
python3 tools/ros2_to_ipc_bridge.py \
  --topic /astrbotex/vision_target \
  --socket-path /home/orangepi/astrbotex_deploy/deploy/astrbotex/data/ipc/astrbotex_vision.sock
```

这条链路不依赖外网，也不使用 localhost TCP/UDP。

## 运行依赖

### ipc_unix_socket 模式

AstrBotEX 容器内只需要 Python 标准库，不需要 ROS2。

宿主机运行 `tools/ros2_to_ipc_bridge.py` 时需要：

```text
rclpy
std_msgs
```

### ros2 模式

启用插件的 Python 环境必须能 import：

```text
rclpy
std_msgs
```

如果使用 `vision_msgs/msg/Detection2DArray`，还需要：

```text
vision_msgs
```

没有 ROS2 Python 环境时，`ros2` 模式会失败并在运行日志中显示错误；`ipc_unix_socket` 模式不受影响。

## 安装

上传打包好的 zip 到 AstrBotEX 插件页，并选择视觉分类。

推荐上传包：

```text
astrbotex_ros2_vision_plugin_upload.zip
```
