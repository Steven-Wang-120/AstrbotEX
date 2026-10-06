# yolo — AstrbotEX 原生视觉插件

面向 AstrbotEX `1f6e4a8` 的真实 SDK，提供 `vision_provider`，无插件自建线程、无自行 ROS init/spin。重型推理放在子进程，Core Actor 调用 `on_worker_step()`，`get_result()` 返回 SDK `VisionResult`。

## 两种输出

| 内容 | EX 内部 TopicBus | 原生 ROS 2 |
|---|---|---|
| 检测 JSON | `yolo.json` | `/yolo/json`，std_msgs/msg/String |
| 标注 JPEG | `yolo.jpg` | `/yolo/jpg`，sensor_msgs/msg/CompressedImage |

TopicBus 是 JSON 可序列化结构，`yolo.jpg` 的 payload 用 `encoding=base64`、`data` 携带 JPEG；订阅插件解码即可得到原始 JPEG。**检测 JSON 中没有图片/Base64**。ROS JPG 与 A.E.B. vision 通道都直接发送 JPEG 字节。两个输出带相同 stream_id/frame_id。ROS JPEG header.frame_id 为 `stream_id:frame_id`。

默认从 `/camera/color/image_raw/compressed` 接收 JPEG/PNG 压缩图像，通过 `context.ros.subscribe('image')` 取最新帧。输入有大小上限，队列为 keep_latest，过期结果不发布为有效目标。

## JSON 字段

```json
{
  "timestamp": 1791250000.125,
  "stream_id": "yolo-实例标识",
  "frame_id": 42,
  "source_stamp": 3.2,
  "source_frame": "camera",
  "width": 640,
  "height": 480,
  "status": "ok",
  "mark_rev": 2,
  "marks": {"目标ID": {"name": "cup", "act": 1, "visible": true, "last_seen": 1791250000.1}},
  "objects": {
    "目标ID": {
      "id": "目标ID", "name": "cup", "color": "red", "conf": 0.93,
      "bbox": {"c": [-0.25, 0.25], "rt": [-0.5, 0.5], "lb": [0, 0], "px": [320, 120, 480, 240]},
      "act": 1
    }
  }
}
```

- `timestamp` 永远是序列化后的首字段，Unix 秒；`source_stamp` 保留 ROS 原始时间。
- `objects` 是 ID→物体字典，ID 为不可复用的随机字符串，不是类别编号。
- `name` 使用模型类别名；`color` 是实例掩码内 HSV 主色；`conf` 为置信度。
- `c/rt/lb` 分别为 bbox 中心、右上、左下，范围 [-1,1]；`px` 保留整数像素框。
- 以图像边界定义 `x=1-2u/W`、`y=1-2v/H`：中心 (0,0)，左/上 +1，右/下 -1。
- `act=0` 默认未标记，`act=1` 已标记。该标记不代表运动命令或模型置信度。
- 无检出 `objects={}`。断流为 `status=stale`，不会继续保留旧检测作为可见目标。

## LLM 持久标记闭环

配套 A.E.B. 增加真实 FunctionTool `set_astrbotex_vision_marks`。LLM 先调用现有 JSON buffer 工具得到准确 ID，再调用：

```json
{"stream_id":"yolo-实例标识","ids":["目标1","目标2"],"marked":true,"request_id":"一次操作的唯一ID"}
```

A.E.B. 根据该 stream 的真实 ZeroMQ peer 发送 `vision.mark.set`。插件在自己的 Actor 中事务性更新 SQLite；确认持久化之后才回复成功。下一帧中所选对象 act=1，黄色轮廓并显示 MARKED。取消只需对指定 ID 调用 marked=false，不影响其他目标。

重试必须复用 request_id；同 ID 同请求返回原收据，不会覆盖后来发生的取消。同 ID 不同请求拒绝。未知 ID、错误 stream 或非布尔 marked 会整批拒绝。

标记保存于插件内 `data/state.sqlite3`（可配置到持久卷）。推理只更新几何/外观，不重置 act；重启/禁用/暂时丢失不清除标记。`marks` 字典保留暂时不可见的被标记物体，`visible=false`。

物理身份关联使用类别、框重叠和颜色直方图，短时连续跟踪及有充分外观证据的重现可复用 ID。外观歧义或长时间消失后不能保证物理身份，因此不自动把旧标记转给新 ID；旧 ID 的标记仍保留，可由 LLM 明确取消并重新标记。这不是任意遮挡下的完美重识别。

其他插件也可通过有界 inbox 订阅配置中的 `aeb.mark`，或使用 ROS `/yolo/mark` 发送同样的控制 JSON。只有 JPEG/JSON 两个输出 Topic；标记命令是输入。

## 安装

将整个 `yolo` 目录（包括 models/ 下权重）安装至 `plugins/vision/yolo`。模型已内置，启动不下载权重；依赖版本见 requirements.txt 和配套镜像。

1. 将 config.python 指向有 torch/ultralytics 的解释器，GPU 用 device="0"，CPU 用 "cpu"。
2. 启用 pubsub.publish_enabled 及所需 enabled_topics。
3. 开启 ros2.bindings 的输入、输出；插件及输出默认均关闭。
4. 如使用 A.E.B.，安装配套工具补丁并设置 aeb.enabled、text_endpoint、vision_endpoint。
5. 通过 EX Dashboard 启用插件，选择 ROS2 环境，再启动 runtime。

状态文件必须保留才能跨部署保留身份和标记。不要把测试用数据库复制到其他机器人。

`models/manifest.json` 记录内置权重来源和 SHA256；Ultralytics 的上游许可证随包提供。配套镜像的 CPU 配置用于可复现实验，原生本机可使用已验证的 CUDA 环境。
