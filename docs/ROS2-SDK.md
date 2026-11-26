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

## 周期输入声明（本轮新增）

订阅端口可声明持续数据健康策略。不声明与声明 `mode=off` 等价，旧 manifest 不需要修改：

```json
{"id":"image","direction":"subscribe",
 "message_types":["sensor_msgs/msg/CompressedImage"],
 "default_topic":"/ex_diag/image","qos_preset":"sensor_data",
 "diagnostics":{"mode":"periodic","startup_grace_sec":10.0,"stale_after_sec":5.0}}
```

- `mode` 仅 `off / periodic`；缺省规范化为 `off`，两个数值仍规范化为 10 / 5。
- `startup_grace_sec` 取值 `[0, 300]`，`stale_after_sec` 取值 `[0.2, 300]`；必须是有限数值且不接受 `true/false`。
  未知 `diagnostics` 字段按配置错误拒绝，不做静默丢弃。
- 只有 `direction=subscribe` 可声明 `periodic`。`publish` 端口声明 `periodic` 被拒绝：
  本地 `tx_published` 增长不能证明对端收到，用发送计数推断“数据健康”是错误结论。
- 离散语义的端口（mark、按键、偶发控制指令）不声明 `periodic`；它们本来就没有“持续有数据”的预期。
- 策略属于 manifest，不是 binding 字段。`ros2.bindings` 的字段白名单不因本功能扩大，
  不能通过保存绑定把 `diagnostics` 偷偷注入。

周期检查只在 runtime `RUNNING`、端口 enabled、原生资源有效、环境处于 ros2 且 idle 时启用。
idle / paused / fault、环境切换和插件卸载期间检查暂停，不报“相机掉线”。本功能不新增自动启停。

## 端口健康 `data_health`（本轮新增）

`status()` 原有 `state/status` 保持不变，新增 `data_health` 与 `diagnostics` 对象。
`data_health` 枚举固定，不接受自定义值：

| 值 | 含义 |
| --- | --- |
| `not_monitored` | `mode=off`，或 publish 端口；不推断持续数据健康 |
| `disabled` | 周期订阅当前被禁用 |
| `paused` | runtime 非 RUNNING 或环境操作中，周期检查暂停 |
| `unavailable` | 预期运行，但原生资源/接口当前不可用 |
| `warming_up` | 观察窗口内尚无回调，仍在启动宽限内 |
| `no_data` | 当前窗口从未收到回调，宽限已过 |
| `receiving` | 当前绑定最近收到回调，age <= `stale_after_sec` |
| `stale` | 当前窗口曾收到回调，但最新 age > `stale_after_sec` |
| `unknown` | 证据不完整或采集失败；`unknown` 不等于正常，不能涂绿 |

`diagnostics` 对象字段：`policy`、`monitoring_active`、`binding_received`、
`receive_age_sec`、`consume_age_sec`、`monitoring_age_sec`、`reason_code`，以及
`monitoring_epoch`（每个新活动窗口递增，供插件关联 scope）。

三个 age 都是后端单调时钟算出的非负数或 `null`。**前端不得用墙钟 `last_message_at` 计算健康**：
系统时间前后跳变时，墙钟差值会给出完全错误的结论，而单调年龄不受影响。
`monitoring_active` 表示“按当前 runtime/环境/启用状态预期需要工作”，
所以资源缺失时它仍可为 `true`，同时 `data_health=unavailable`——这两种信息不能合并。
无数据计时只有 `resource_created=true` 才开始；窗口起点取绑定成功时刻与本轮 RUNNING 开始时刻的较晚值，
晚 attach 的端口不能继承已经被消耗掉的宽限。

历史累计计数（`rx_received / rx_consumed / tx_*`）保留原语义，不因窗口切换清零；
窗口只影响健康判定，不影响累计量。旧代次回调不能把新窗口判成 `receiving`。

## 诊断快照、预检与事件（本轮新增）

`GET /api/v1/ex/environments/diagnostics` 返回只读缓存快照，普通 GET 不触发重新探测。
固定字段：`schema_version`、`environment`、`collected_at`（墙钟，仅展示）、
`collection_age_sec`（单调年龄）、`current`、`partial`、`summary`、`preflight`、
`ports`、`active_issues`、`refresh`。

`summary` 取 `ok / warning / error / unknown / paused`。没有 ROS 活动时是 `paused`，
不能把“没在跑 ROS”显示成“通过 ROS 检测”。初次采集前 `current=false`、`summary=unknown`。
`refresh` 为 `{id, state, error_code}`，`state` 取 `idle / pending / running / completed / failed / superseded`。

`POST /api/v1/ex/environments/diagnostics/refresh` 请求 `{expected_session, expected_revision}`：
缺字段 400、旧值 409，通过后 202 返回 `refresh_id`。它不修改环境 revision，也不触发环境选择；
并发点击合并成一次，不创建无限工作线程。刷新只重查诊断，前端“重新检查”按钮不应顺带 select 环境。

预检报告 `collect_preflight()` 字段：`schema_version=1`、`platform`、`supported`、
`checks[]`、`runtime`、`interfaces`、`partial`。check 项为 `{id, status, message, evidence, remediation}`，
`status` 取 `pass / warn / fail / unknown`。`runtime` 白名单仅
`ros_distro / python_executable / rmw_requested / rmw_effective / domain_id / localhost_only /
overlay_paths / rclpy_available / rosidl_available`。
路径只返回路径与存在性，不返回文件内容；报告不附全量 `environ`。
`rmw_requested` 未设置、`rmw_effective` 无法读取时都返回 `null`，
**不能猜成默认实现**；`supported` 只表示平台探针支持性，不代表 ROS 通信通过。

预检缓存默认 TTL 30 秒。诊断 issue 事件 `type=ros_diagnostic`，
`data` 含 `severity / source / code / issue_key / scope / state=active|recovered|withdrawn /
evidence / remediation`。恢复用固定 `code=ROS_DIAGNOSTIC_RECOVERED` 并附 `recovered_code`；
观察结束是 `withdrawn`，不能当 `recovered` 报。同 scope 同 code 只保留一个活跃项，重复采样只增加 `occurrences`。

`vision_health` 是低频业务状态事件，沿用同一 `severity/source/code` 形状与可用的 port scope，
但**不进入**通用 `active_issues` 状态机，也不改写端口 `data_health`。
视觉业务状态与端口收数健康是两件事：推理正常但 `objects` 为空是正常结果，不是 ROS 断流。

## 结果与未知值

诊断链路的结论分四级：`PASS`（实际执行且条件全部成立）、`FAIL`（执行过但条件不成立）、
`NOT_RUN`（没有环境或未执行，不计入通过数）、`BLOCKED`（缺依赖/权限/基线，需写清解除条件）。
Windows 与 mock 上只能得到 `NOT_RUN`：没有 `rclpy` 和真实 DDS 时，
单元测试通过不能写成“ROS 通过”，原生结论只能在 Linux + Humble 上产生。
