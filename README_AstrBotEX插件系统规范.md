# AstrBotEX 插件系统规范

本文档用于统一 `AstrBotEX` 当前插件系统的结构、语法、分类方式，以及后续插件间 `pub/sub` 的设计约定。

当前目标不是复刻 ROS，而是做一套更轻、更适合智能救援项目的本地插件执行框架。

---

## 1. 总体设计目标

AstrBotEX 插件系统的目标是：

- 让视觉、感知、控制、决策能力以插件形式接入
- 让每一类插件职责清晰，不互相乱调用
- 允许插件之间通过 `pub/sub` 交换事实和状态
- 最终控制命令仍由 runtime 中央裁决，不允许多插件直接抢底盘

一句话概括：

`插件负责发布事实，runtime 负责裁决动作。`

---

## 2. 五类插件分类

当前插件系统统一分为五类：

```text
vision
perception
control
decision
special
```

### 2.1 `vision`

职责：

- 相机识别
- 外部视觉模型桥接
- 目标检测结果标准化

典型插件：

- `astrbotex_zmq_vision_plugin`

### 2.2 `perception`

职责：

- 雷达
- 定位
- 位姿
- 里程计
- 前向距离
- 场地边界

典型插件：

- `lidar_pose_plugin`

### 2.3 `control`

职责：

- 运动意图下发
- 协议编解码
- CAN / 串口 / 总线传输
- 下位机状态回读

典型插件：

- `astrbotc_motion_bridge_can_plugin`
- `can_codec_plugin`
- `can_transport_plugin`

### 2.4 `decision`

职责：

- 比赛规则
- 目标选择
- 技能执行
- 状态机推进

典型插件：

- `rescue_rule_plugin`
- `rescue_policy_plugin`
- `scan_target_skill_plugin`
- `align_target_skill_plugin`
- `approach_target_skill_plugin`
- `capture_target_skill_plugin`
- `deliver_target_skill_plugin`
- `retreat_skill_plugin`

### 2.5 `special`

职责：

- 调试
- 追踪
- 诊断
- 辅助桥接

典型插件：

- `trace_plugin`
- `telemetry_plugin`

---

## 3. 当前支持的 capability

插件 `plugin.json` 中的 `provides` 当前支持：

```text
motion_bridge
vision_provider
transport
protocol_codec
telemetry_provider
rule_plugin
policy_plugin
skill_plugin
tool_plugin
trace_plugin
```

默认映射关系如下：

```text
vision_provider    -> vision
motion_bridge      -> control
transport          -> control
protocol_codec     -> control
telemetry_provider -> perception
rule_plugin        -> decision
policy_plugin      -> decision
skill_plugin       -> decision
tool_plugin        -> decision
trace_plugin       -> special
```

说明：

- `telemetry_provider` 当前默认归到 `perception`
- 这是因为它更接近“状态感知”而不是“动作执行”

---

## 4. 目录结构

插件目录支持两种形式：

```text
plugins\<plugin_id>
plugins\<category>\<plugin_id>
```

推荐形式是按分类存放：

```text
plugins\vision\<plugin_id>
plugins\perception\<plugin_id>
plugins\control\<plugin_id>
plugins\decision\<plugin_id>
plugins\special\<plugin_id>
```

单个插件目录推荐结构：

```text
<plugin_id>/
  plugin.json
  main.py
  config.json
  config.schema.json
  README.md
  pages/
  assets/
```

其中：

- `plugin.json`：插件声明文件
- `main.py`：插件入口
- `config.json`：运行配置
- `config.schema.json`：Dashboard 配置表单描述
- `README.md`：插件说明

---

## 5. `plugin.json` 基础语法

最小示例：

```json
{
  "id": "lidar_pose_plugin",
  "name": "雷达定位插件",
  "version": "0.1.0",
  "entry": "main.py",
  "provides": ["telemetry_provider"],
  "description": "输出位姿、距离和边界信息",
  "author": "team",
  "enabled_default": false
}
```

### 5.1 基础字段

- `id`
  插件唯一标识，只允许字母、数字、`_`、`-`

- `name`
  插件显示名称

- `version`
  插件版本

- `entry`
  插件入口文件，通常为 `main.py`

- `provides`
  capability 列表，至少要有一个

- `description`
  插件说明

- `author`
  作者

- `requires`
  依赖 capability 或依赖插件的预留字段

- `config_schema`
  Dashboard 配置 schema 文件路径

- `enabled_default`
  默认是否启用

- `cover`
  封面图

- `dashboard`
  插件自定义 Dashboard 页面

### 5.2 执行线程约定

每个已加载插件对应一个由 AstrBotEX Core 管理的 `PluginActor` 线程。插件作者不直接创建或销毁线程。

普通生命周期和业务方法会通过 Actor mailbox 投递，并在该插件线程中串行执行。需要持续读取阻塞设备的插件实现：

```python
def on_worker_step(self) -> None:
    data = self.device.read(timeout=0.05)
    if data is not None:
        self.publish(data)
```

`on_worker_step()` 必须使用有界超时，保证配置重载、停用和卸载可以及时完成。一个插件如果同时面对多个无法由同一轮询接口管理的独立阻塞设备，应拆成多个插件；共享一条 CAN 或串口总线的多个逻辑节点，则由一个总线插件线程统一路由。

插件之间不得直接跨线程调用。连续状态使用 `TopicBus.get_latest()`；需要逐条消费消息时，使用 `PluginContext.subscribe()` 获取有界 inbox。

---

## 6. 推荐扩展字段：Pub/Sub

为了支持插件间消息通信，建议在 `plugin.json` 中新增两类声明：

- `publishes`
- `subscribes`

推荐写法：

```json
{
  "id": "lidar_pose_plugin",
  "name": "雷达定位插件",
  "version": "0.1.0",
  "entry": "main.py",
  "provides": ["telemetry_provider"],
  "publishes": [
    {
      "topic": "lidar_pose_plugin.pose",
      "label": "位姿",
      "schema": "pose"
    },
    {
      "topic": "lidar_pose_plugin.front_distance",
      "label": "前向距离",
      "schema": "distance"
    },
    {
      "topic": "lidar_pose_plugin.boundary",
      "label": "边界信息",
      "schema": "boundary"
    }
  ],
  "subscribes": [
    {
      "topic": "rescue_policy_plugin.goal",
      "label": "策略目标",
      "schema": "goal"
    }
  ]
}
```

### 6.1 为什么 topic 不能只等于插件名

不建议把 topic 直接写成：

```text
lidar_pose_plugin
```

因为一个插件往往不止一种消息。

更合理的形式是：

```text
plugin_id.message_name
```

例如：

```text
lidar_pose_plugin.pose
lidar_pose_plugin.front_distance
astrbotex_zmq_vision_plugin.current_target
astrbotex_zmq_vision_plugin.detections
```

这样既保留插件命名空间，又能表达具体语义。

---

## 7. `config.json` 运行期 Pub/Sub 配置

`plugin.json` 声明的是“这个插件能发什么、能订什么”。  
真正当前是否启用，则建议放在 `config.json` 中。

推荐结构：

```json
{
  "pubsub": {
    "publish_enabled": true,
    "enabled_topics": [
      "lidar_pose_plugin.pose",
      "lidar_pose_plugin.front_distance"
    ],
    "subscriptions": [
      {
        "plugin_id": "astrbotex_zmq_vision_plugin",
        "topic": "astrbotex_zmq_vision_plugin.current_target"
      },
      {
        "plugin_id": "rescue_policy_plugin",
        "topic": "rescue_policy_plugin.goal"
      }
    ]
  }
}
```

说明：

- `publish_enabled`
  插件发布总开关

- `enabled_topics`
  当前实际开启发布的 topic

- `subscriptions`
  当前订阅配置

---

## 8. Topic 消息结构

建议所有 topic 消息统一包一层信封。

推荐格式：

```json
{
  "topic": "lidar_pose_plugin.pose",
  "timestamp": 1720000000.123,
  "source": "lidar_pose_plugin",
  "frame": "world",
  "payload": {
    "x": 1200.0,
    "y": 300.0,
    "yaw": 1.57
  }
}
```

最少应统一：

- `topic`
- `timestamp`
- `source`
- `payload`

可选字段：

- `frame`
- `seq`
- `ttl_ms`

---

## 9. 建议内置的 schema 名称

为了让不同插件容易互通，建议先约定一批标准 schema：

```text
pose
distance
boundary
target
detections
goal
skill_state
health
trace
```

例如：

- `pose`
  `x / y / yaw`

- `distance`
  `value_mm`

- `target`
  `track_id / color / shape / target_kind / center`

- `goal`
  `type / target_id / params`

---

## 10. Dashboard 工学约定

为了让前端使用简单，Dashboard 不应该直接丢出全量 topic 列表，而应做成两级交互。

### 10.1 每个插件都有两个入口

- `发布`
- `订阅`

### 10.2 发布配置

点击 `发布` 后，展示：

- 发布总开关
- 当前插件声明过的 topic 列表
- 每个 topic 的单独开关

例如：

```text
发布
[x] lidar_pose_plugin.pose
[x] lidar_pose_plugin.front_distance
[ ] lidar_pose_plugin.boundary
```

### 10.3 订阅配置

点击 `订阅` 后，按两步走：

第一步：

- 选择订阅哪个插件

第二步：

- 选择该插件的哪个 topic

这比直接列出一大堆 topic 更符合工学，因为用户先认识“来源插件是谁”，再认识“它发什么”。

### 10.4 前端显示建议

订阅弹窗里，每个可选来源插件都应显示：

- 插件中文名
- 插件 id
- 是否有发布能力
- 当前是否启用发布

每个 topic 都应显示：

- topic 技术名
- topic 中文 label
- schema 名称

---

## 11. 后端注册表要求

为了支撑前端两步选择，后端必须维护一份统一的发布能力注册表。

逻辑上至少要能回答：

- 哪些插件有发布能力
- 每个插件声明了哪些 topic
- 每个 topic 当前是否启用
- 每个 topic 的 schema 是什么

推荐返回结构：

```json
{
  "lidar_pose_plugin": {
    "name": "雷达定位插件",
    "publish_enabled": true,
    "topics": [
      {
        "topic": "lidar_pose_plugin.pose",
        "label": "位姿",
        "schema": "pose",
        "enabled": true
      },
      {
        "topic": "lidar_pose_plugin.front_distance",
        "label": "前向距离",
        "schema": "distance",
        "enabled": true
      }
    ]
  }
}
```

---

## 12. 插件间通信原则

插件之间允许通过 `pub/sub` 交换消息，但必须遵守以下原则：

### 12.1 允许发布事实，不允许抢控制权

允许发布：

- 位姿
- 距离
- 目标
- 边界
- 当前阶段
- 技能状态

不建议任何插件直接通过 topic 下发底盘控制命令。

### 12.2 最终动作由 runtime 裁决

正确链路应为：

```text
vision / perception plugins
  -> pub/sub facts
decision / skill plugins
  -> candidate intent
runtime
  -> arbitration
motion_bridge
  -> lower controller
```

原因很简单：

- 对准插件可能想左转
- 脱离插件可能想倒车
- 安全插件可能想停车

如果不收口，系统一定打架。

---

## 13. 建议的最小 topic 集

第一版先不要做太大，建议先只稳定这几个 topic：

```text
lidar_pose_plugin.pose
lidar_pose_plugin.front_distance
astrbotex_zmq_vision_plugin.current_target
rescue_policy_plugin.goal
skill.state
control.feedback
```

只要这几个跑通，搜索、接近、回收主链路就能建立起来。

---

## 14. 插件开发建议

开发新插件时，建议按下面顺序来：

1. 先写好 `plugin.json`
2. 明确它属于哪一类插件
3. 明确它 `provides` 什么 capability
4. 明确它 `publishes` 哪些 topic
5. 明确它 `subscribes` 哪些 topic
6. 再写 `config.schema.json`
7. 最后写 `main.py`

不要先写代码再倒推协议，否则很容易把结构写乱。

---

## 15. 当前推荐结论

当前 AstrBotEX 插件系统推荐坚持以下几个约束：

- 插件分类固定为五类：`vision / perception / control / decision / special`
- topic 命名固定为：`plugin_id.message_name`
- `plugin.json` 声明能力边界
- `config.json` 保存运行期开关和订阅关系
- Dashboard 用“发布 / 订阅”两入口
- 订阅交互用“两步选择”：先选插件，再选 topic
- 所有控制动作最终由 runtime 中央裁决

这套规范的目标不是做最复杂的系统，而是做最不容易乱、最适合你当前智能救援项目推进的系统。
