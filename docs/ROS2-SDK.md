# ROS 2 插件 SDK

旧 EXplugin 已归档，不再迁移或支持。新插件从 `examples/ros2_echo` 开始。
`publishes/subscribes/pubsub` 仍是内部 TopicBus；`ros2.ports` 独立声明原生 ROS 端口。

```json
{"ros2":{"schema_version":1,"ports":[
  {"id":"input","direction":"subscribe","message_types":["std_msgs/msg/String"],
   "default_topic":"/ex_demo/input","qos_preset":"reliable_volatile",
   "queue":{"capacity":8,"overflow":"reject_new","max_message_bytes":8388608,"max_bytes":16777216}},
  {"id":"output","direction":"publish","message_types":["std_msgs/msg/String"],
   "default_topic":"/ex_demo/output","qos_preset":"reliable_volatile"}
]}}
```

插件默认禁用；发布端口默认关闭。在插件详情页启用输出绑定，并显式启用插件。
示例的业务处理遵守 runtime 生命周期：点击运行后才消费输入、回发文字。
开启 ROS 环境本身不启动 runtime，不发布控制测试消息。

```python
def on_load(self):
    self.input = self.context.ros.subscribe("input")
    self.output = self.context.ros.publisher("output")

def on_worker_step(self):
    packet = self.input.get_nowait()
    if packet is None:
        time.sleep(0.01)
        return
    if self.output.status()["resource_created"]:
        message = self.output.new_message()
        message.data = packet.message.data
        result = self.output.publish(message)
```

每个端口有独立队列、owner、环境代次和绑定代次。句柄在环境切换后复用，插件卸载后永久失效。
回调仅入队，业务解释在插件 actor 中完成；插件不得自行 init、shutdown 或 spin ROS。

`get_nowait()` / `take_latest()` 返回带原生 `message`、单调接收时间和代次的数据包。
`new_message()` 在没有可用类型时抛 `RosUnavailableError`。
`publish()` 返回结构化结果：`queued / unavailable / rejected_full / rejected_size / invalid_type / runtime_inactive`。
入队后插件不得继续修改消息对象。`queued` 不代表远端收到；`tx_published` 只记录本地 publish 成功。

队列同时限制条数、单条对象内存和总内存估算；默认单条 8 MiB、总计 16 MiB。
大型原生数组按内存占用估计，不复制消息做 JSON 转换。超过复杂度预算的嵌套容器按超大消息拒绝。
配置可设置 `max_age_ms`；控制端口必须有限 TTL 和 volatile durability。
`keep_latest` 用于连续状态；`reject_new` 用于不允许静默覆盖的离散消息。

`execution_lane` 支持 `sensor / general / control`；控制端口默认要求 runtime running。
插件可同时使用内部 TopicBus，ROS 状态不改变内部 TopicBus 的实例或内容。

## 停止钩子

有 ROS 控制输出的插件应实现以下钩子，在 actor 内停止自己的任务：

```python
def on_environment_deactivating(self, environment_id, reason):
    self.task_active = False
    stop = self.velocity_output.new_message()  # 业务定义合法的停止消息
    result = self.velocity_output.publish_stop(stop)
    return result.accepted
```

`publish_stop()` 只允许在框架此次停止操作授予的 actor 钩子内调用，只能使用声明为控制输出的端口。
停止阶段拒绝普通发送、清空旧队列，并在 deadline 内等待停止发送的本地调用结束。
钩子拒绝、超时或发送失败时保留必要 ROS 资源，状态为 `quiesce_failed`，普通发送继续关闭。
没有停止钩子的运行中 ROS 控制插件必须先停止 runtime。失败后需完成切回普通模式才能重新启用 ROS。
这个机制不提供下位机动作完成确认；业务 ACK 和硬件超时仍由控制协议实现。

## 配置和诊断

绑定保存在插件 `config.json` 的 `ros2.bindings` 中，带独立 revision。
普通配置保存保留 ROS 与 pubsub 配置；ROS 保存只重建改变的端口。
运行中的控制输出不能热改 Topic。静态校验失败不写盘，缺失接口允许保存为 pending。

QoS 使用当前 ROS 发行版的 `qos_check_compatible`。不兼容时显示对端与原因，不自动改写配置。
Humble 的某些订阅 API 没有实际匹配数，返回 `null`；graph 数量不会冒充匹配数。
同一 Topic 可供多个 owner 使用；`rx_received/rx_consumed/tx_queued/tx_published` 分别记录实际阶段。

环境 API 前缀 `/api/v1/ex/environments`：

| 请求 | 含义 |
|---|---|
| GET 根路径 | 当前模式、可用库、配置、有效配置、部署锁定值 |
| POST `/select` | mode、expected_revision、expected_session；异步操作返回 202 |
| GET `/operations/{id}` | 操作阶段和错误 |
| GET `/ros2/graph`、`/ros2/endpoints` | 网络发现和本地真实端点 |
| POST `/ros2/config` | 保存配置；活动环境需切回普通再启用以应用 |
| POST `/ros2/discovery/refresh` | 合并触发后台发现 |
| GET `/ros2/interfaces` | 接口包版本/路径、已发现类型的本地支持 |
| POST `/ros2/interfaces/check` | 检查消息类和 typesupport，不安装依赖 |

`GET/POST /api/v1/ex/plugins/{id}/ros2` 读取/保存插件绑定。
SSE 事件为 `environment_changed / ros_graph_changed / ros_endpoints_changed`，仅推送变更提示。
网页合并请求、保护未保存草稿，重新可见或 SSE 重连后刷新；统计事件按配置限频。
