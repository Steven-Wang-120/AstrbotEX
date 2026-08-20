# AstrBotEX 技术文档

本文面向 AstrBotEX 的开发、集成与部署人员，记录当前代码中的接口契约、核心实现机制和配置项。项目定位、功能概览、启动方法与目录说明见 `README.md`，本文不重复这些入门内容。

> 基准版本：`pyproject.toml` 中的 `0.1.0`  
> 文档核对日期：2026-08-20  
> 行为依据：当前源码、测试、profile，以及 AstrBot/A.E.B/EXplugin 跨仓库契约。旧版无版本接口仍有兼容实现，新集成应优先使用 `/api/v1/ex` 路径。

## API 文档

### 1. HTTP 约定

- 默认服务地址：`http://127.0.0.1:8765`。
- 稳定接口前缀：`/api/v1/ex`。
- JSON 请求应使用 `Content-Type: application/json`。
- Dashboard 使用同一 HTTP 服务，并通过事件流获取运行事件。
- 部分接口仍提供 `/api/...` 无版本别名；这些路径仅用于兼容，不应作为新客户端的首选。
- `/api/v1/ex/llm/context`、`actions` 和 `proposal` 是 bridge 接口的 LLM 别名。

### 2. 运行时与事件

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/status` | 返回运行状态、世界状态、组件和最近活动摘要 |
| `GET` | `/api/v1/ex/events` | 返回运行事件；Dashboard 另使用 SSE 事件流 |
| `POST` | `/api/v1/ex/runtime/start` | 启动运行循环；仅允许从合适的非运行状态进入 |
| `POST` | `/api/v1/ex/runtime/stop` | 停止运行循环并释放当前技能/运动状态 |

运行时状态枚举为：

- `IDLE`：未执行任务。
- `RUNNING`：按 tick 推进任务。
- `PAUSED`：已暂停，可在满足条件时重新启动。
- `FAULT`：运行错误；核心会停止当前动作并记录故障。

### 3. 插件管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/plugins` | 列出已发现插件、能力、启用与运行状态 |
| `GET` | `/api/v1/ex/plugins/{plugin_id}` | 获取单个插件详情 |
| `POST` | `/api/v1/ex/plugins/upload` | 上传并安装插件 ZIP 包 |
| `POST` | `/api/v1/ex/plugins/{plugin_id}/enable` | 启用并加载插件 |
| `POST` | `/api/v1/ex/plugins/{plugin_id}/disable` | 停止并禁用插件 |
| `POST` | `/api/v1/ex/plugins/{plugin_id}/config` | 更新插件配置，受 manifest schema 约束 |
| `POST` | `/api/v1/ex/plugins/{plugin_id}/pubsub` | 更新发布/订阅启用状态 |
| `DELETE` | `/api/v1/ex/plugins/{plugin_id}` | 卸载插件 |
| `GET` | `/api/v1/ex/plugins/{plugin_id}/cover` | 获取插件封面资源 |
| `GET` | `/api/v1/ex/plugins/{plugin_id}/dashboard` | 获取插件 Dashboard 资源 |
| `GET` | `/api/v1/ex/pubsub/publishers` | 查询可用发布者和主题 |

插件身份、入口、能力、配置 schema、主题和动作均来自插件根目录的 `plugin.json`。仅实现 Python 方法而未在 manifest 中声明，不会使该方法自动成为可用动作。

### 4. 视觉源

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/vision/sources` | 列出视觉源及活动源 |
| `POST` | `/api/v1/ex/vision/sources` | 新建视觉源 |
| `PUT` | `/api/v1/ex/vision/sources/{source_id}` | 更新视觉源 |
| `DELETE` | `/api/v1/ex/vision/sources/{source_id}` | 删除视觉源 |
| `POST` | `/api/v1/ex/vision/sources/{source_id}/test` | 拉取一次数据并验证源配置 |
| `GET` | `/api/v1/ex/vision/active-source` | 查询当前活动源 |
| `POST` | `/api/v1/ex/vision/active-source` | 切换活动源 |
| `GET` | `/api/v1/ex/vision/latest` | 返回活动源的最新结果 |
| `POST` | `/api/v1/ex/vision/publish` | 发布视觉数据，必要时转发至视觉业务通道 |

视觉源当前支持 mock 和本地 HTTP 数据源。结果是否可用于决策还取决于源启用状态、响应时间、数据时间戳、过期阈值和最低置信度。

### 5. AstrBot Bridge

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/bridge/context` | 构建一次带 `context_id` 的现场上下文 |
| `GET` | `/api/v1/ex/bridge/actions` | 返回当前动作目录与参数 schema |
| `POST` | `/api/v1/ex/bridge/proposal` | 校验并分派高层动作提案 |

同义路径为 `/api/v1/ex/llm/context`、`/api/v1/ex/llm/actions` 和 `/api/v1/ex/llm/proposal`。

上下文由若干 observation block、动作目录、提案 schema 和 `context_id` 组成。一个 block 的关键字段包括：

| 字段 | 含义 |
|---|---|
| `block_id` | 本次上下文中的块标识 |
| `contract_id` | 数据契约标识，不是认证令牌 |
| `source_plugin` | 数据来源插件 |
| `topic` / `schema` | 数据主题或结构说明 |
| `seq` | 来源消息序号 |
| `timestamp` | 数据产生时间 |
| `ttl_ms` | 数据允许使用的时长 |
| `fresh` | 构建上下文时是否仍有效 |
| `payload` | 结构化观测内容 |

提案至少包含有效 `context_id` 和非空命令列表。每条命令指定已发布的 `action_id`、参数、原因，并可引用 observation block。服务端依次验证：

1. 上下文是否存在且未超过有效期。
2. 动作是否位于本次上下文的允许列表中。
3. 动作所有者是否与声明一致。
4. 当前运行状态是否满足动作要求。
5. 参数是否通过动作的 JSON Schema。
6. 必需 block 是否存在且新鲜。
7. 引用的 block 序号是否与本次上下文一致。

内置动作包括 `runtime.start.v1` 和 `runtime.stop.v1`。插件动作通过声明的命令主题发送给其所有者。

### 6. 交互接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/interaction/status` | 汇总传输、AstrBot、STT/TTS、麦克风和扬声器状态 |
| `POST` | `/api/v1/ex/interaction/message` | 提交文本消息至交互链路 |
| `POST` | `/api/v1/ex/interaction/stt` | 提交音频进行语音识别 |
| `POST` | `/api/v1/ex/interaction/tts` | 提交文本进行语音合成 |
| `POST` | `/api/v1/ex/interaction/reply` | 接收 AstrBot 回复并进入播报流程 |

STT/TTS 优先经已就绪的 ZeroMQ 音频业务连接调用；启用 provider 代理后可使用 HTTP 兼容路径。状态接口会区分业务连接是否就绪、AstrBot 是否可达、provider/proxy 是否可用，以及本地输入输出插件是否在线。

### 7. 连接管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/ex/connections/types` | 返回可创建的连接类型及配置描述 |
| `GET` | `/api/v1/ex/connections` | 返回连接配置和运行状态 |
| `GET` | `/api/v1/ex/connections/business` | 返回 text/audio/vision 业务通道状态 |
| `POST` | `/api/v1/ex/connections` | 创建连接 |
| `PUT` | `/api/v1/ex/connections/{connection_id}` | 更新连接配置 |
| `DELETE` | `/api/v1/ex/connections/{connection_id}` | 删除连接 |
| `POST` | `/api/v1/ex/connections/{connection_id}/start` | 启动连接 |
| `POST` | `/api/v1/ex/connections/{connection_id}/stop` | 停止连接 |
| `POST` | `/api/v1/ex/connections/{connection_id}/send` | 通过指定连接发送诊断消息 |

### 8. ZeroMQ 契约

AstrBot 侧 A.E.B 使用 ROUTER socket，AstrBotEX 使用 DEALER peer。三个业务通道是独立的背压域：

| 通道 | 默认端口 | 当前业务方法 |
|---|---:|---|
| `text` | `8766` | `interaction.message`、`interaction.reply`、`transport.status`、`bridge.context.get`、`bridge.proposal.submit`、`runtime.status`、`vision.json.publish`、`vision.json.status` |
| `audio` | `8767` | `providers.status`、`stt.transcribe`、`tts.synthesize` |
| `vision` | `8768` | `vision.jpeg.publish`、`vision.status`；`vision.publish` 仅用于兼容旧客户端 |

Envelope 约束：

- `protocol` 固定为 `astrbotex-zmq`，`version` 固定为 `1`。
- 请求和事件包含 ID、通道、方法、时间戳和 JSON payload。
- 响应使用 `kind=response`，并以 `reply_to` 关联请求 ID。
- 音频和视觉消息可附加一个二进制 multipart frame。
- 每个通道连接后独立发送 `system.hello`。
- AstrBotEX 连接管理器的协议上限为 64 MiB；A.E.B 文档给出的通道默认上限为文字 8 MiB、音频 25 MiB、视觉 64 MiB，部署时以双方较小限制为准。

YOLO 等视觉转发插件应把结构化 JSON 字段通过 `8766` 的 `vision.json.publish` 发送，不附加二进制帧；JPEG 原始字节通过 `8768` 的 `vision.jpeg.publish` 单独发送。A.E.B 默认分别保留最近 8 条 JSON 和 8 张 JPEG，并向 Bot 注册 `get_astrbotex_vision_json_buffer` 与 `get_astrbotex_vision_jpeg_buffer` 两个按需读取工具。JPEG 通道的元数据只用于帧关联，不应重复携带检测对象字段。

`ASTRBOT_BASE_URL` 仍表示 HTTP 兼容地址，而 `8766` 在当前 A.E.B 设计中又是 ZeroMQ 文字端口。部署前必须确认实际启用的是 ZeroMQ DEALER 连接还是兼容 HTTP 服务，不能仅凭端口号推断协议。

## 内部实现

### 9. 服务装配

`astrbot_ex.core.api_server.build_server()` 是 composition root。它按顺序装配：

1. `EventBus` 与 `TopicBus`。
2. 感知配置、`SceneFusion` 和 `PerceptionCore`。
3. `PluginRegistry` 与 `AstrBotEXRuntime`。
4. `ConnectionManager` 和业务消息处理器。
5. 可选 AstrBot STT/TTS provider 代理与 `InteractionCore`。
6. `RuntimeController`、`VisionSourceManager` 和 `LocalPluginManager`。
7. 已发现且被配置为启用的本地插件。
8. `AstrBotBridge`、HTTP handler、SSE 与 Dashboard 静态服务。

未设置 `ASTRBOTEX_DATA_DIR` 时，数据根目录为项目根目录；设置后，默认 profile 和运行持久化文件从指定目录解析。

### 10. 运行循环与故障处理

`AstrBotEXRuntime.tick()` 仅在 `RUNNING` 状态推进。主要顺序为：

```text
插件 on_tick（合并重复 tick）
 -> 读取 motion bridge 的机器人状态
 -> PerceptionCore 获取视觉和扫描
 -> SceneFusion / WorldBuilder 更新世界状态
 -> 规则基于世界状态评估
 -> 策略选择 Goal
 -> 创建或推进 Skill
 -> SafetyGuard 校验并限幅 MotionIntent
 -> 规则再次基于意图评估
 -> motion bridge 发送动作
 -> InteractionCore.tick()
```

关键行为：

- 视觉或扫描 provider 缺失时，感知层返回空或过期元数据，不阻止 API 服务启动。
- motion bridge 不存在时，运动输出只记录事件，不会假定指令已执行。
- tick 中未处理的异常会使运行时进入 `FAULT`，停止当前技能，并尝试下发停止动作。
- `SafetyGuard` 核心默认限值为 `vx=0.35 m/s`、`vy=0.35 m/s`、`wz=1.2 rad/s`；急停状态强制三个分量为零。
- 核心限幅不是底层硬件保护的替代品，底层控制器仍需独立执行最终安全约束。

### 11. 感知与世界构建

`PerceptionCore` 从当前视觉 provider 获取检测结果，并在存在 scan/telemetry provider 时获取扫描数据。`SceneFusion` 将目标水平像素位置换算为雷达方位角，再在角度容差和时间窗口内选择距离样本；`WorldBuilder` 将融合结果写入当前世界状态。

当前实现以同步 provider 读取为主。异步感知主题、稳定 track ID、风险等级、按需最近帧以及 VLM 快照/embedding 工具属于后续演进项，不能作为已完成接口依赖。

### 12. Bridge 上下文生命周期

`AstrBotBridge` 的上下文默认有效期为 15 秒，普通 observation block 默认 TTL 为 1000 ms。来自 TopicBus 的消息如果自带 `ttl_ms`，优先使用消息 TTL。

上下文构建时组合运行状态、世界状态、事件、感知结果和插件主题块，并快照本次允许的 affordance。提案处理不重新解释自然语言，只对快照中的动作和结构化参数进行校验。这样可以避免 LLM 在上下文生成后使用新增、已撤销或数据已过期的动作。

### 13. 插件生命周期与并发

`LocalPluginManager` 负责发现、校验、安装、配置、加载和卸载插件。`PluginRegistry` 保存运行时能力插槽，`PluginActor` 为每个插件提供隔离执行环境。

- 每个插件由一个受管理工作线程串行执行方法。
- 默认单次 actor 调用超时为 2 秒。
- 高频 `on_tick` 可被合并，避免慢插件导致邮箱无限堆积。
- 插件停止时必须能够在有限时间内退出；设备 I/O 不应使用无限阻塞。
- manifest 的 capability 决定插件被映射到视觉、遥测/扫描、运动、麦克风、扬声器、规则、策略、技能或追踪等插槽。
- manifest、profile 中的启用状态及运行依赖必须同时满足，插件才会真正可用。

### 14. TopicBus 语义

`TopicBus` 是进程内消息总线，不是跨进程传输协议。`TopicMessage` 字段包括：

- `topic`：主题名，插件主题通常使用 `plugin_id.message_name`。
- `timestamp`：消息时间。
- `source`：发布者。
- `payload`：结构化内容。
- `frame`：可选二进制帧。
- `seq`：主题内序号。
- `ttl_ms`：可选有效期。

连续状态应通过 `get_latest()` 消费；必须逐条处理的命令或事件应通过 `subscribe_inbox()` 或 `PluginContext.subscribe()` 使用有界 inbox。最近历史默认每个主题保留 50 条；inbox 满时丢弃最旧消息，以限制内存并保护发布者。

核心交互主题包括：

- `interaction_core.message.incoming`
- `interaction_core.message.outgoing`
- `interaction_core.message.backchannel`
- `interaction_core.message.confirmation`
- `interaction_core.audio.capture`
- `interaction_core.audio.play`
- `interaction_core.audio.stop`

### 15. 交互链路

`InteractionCore` 使用独立工作队列处理可能耗时的文本、STT 和 TTS 操作，避免阻塞 runtime tick。典型音频链路为：

```text
mic_input 插件原始音频
 -> STT provider
 -> AstrBot text interaction
 -> interaction.reply
 -> TTS provider
 -> interaction_core.audio.play
 -> speaker_output 插件
```

麦克风也可直接发布已识别文本。播放期间，核心通过捕获暂停/恢复和回声过滤减少扬声器内容重新进入识别链路；`interaction_core.audio.stop` 用于中止当前播放。

### 16. ConnectionManager

`ConnectionManager` 管理连接配置、适配器生命周期、请求关联、超时、协议校验和业务通道选择。当前适配器覆盖 ZeroMQ 与 WebSocket 类型。

对 `text`、`audio`、`vision` 的业务调用使用 `request_feature()`：管理器只选择已启用、已启动且 feature 匹配的连接。请求在等待窗口内未收到对应 `reply_to` 时抛出超时错误；协议名、版本或通道不匹配的消息会被拒绝。

## 配置参数详解

### 17. 命令行参数

服务入口支持：

| 参数 | 环境变量回退 | 默认值 | 说明 |
|---|---|---:|---|
| `--host` | `ASTRBOTEX_HOST` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `ASTRBOTEX_PORT` | `8765` | HTTP 与 Dashboard 端口 |
| `--tick-hz` | `ASTRBOTEX_TICK_HZ` | `20` | 运行循环目标频率 |

命令行值优先于环境变量。

### 18. AstrBotEX 环境变量

| 参数 | 源码默认值 | 作用与注意事项 |
|---|---|---|
| `ASTRBOTEX_HOST` | `127.0.0.1` | HTTP 监听地址；容器通常显式设为 `0.0.0.0` |
| `ASTRBOTEX_PORT` | `8765` | HTTP/Dashboard 端口 |
| `ASTRBOTEX_TICK_HZ` | `20` | runtime tick 频率；提高前需确认插件与设备 I/O 可承受 |
| `ASTRBOTEX_DATA_DIR` | 项目根目录 | profile、视觉源、插件状态和连接持久化根目录 |
| `ASTRBOT_BASE_URL` | `http://127.0.0.1:8766` | AstrBot HTTP 兼容服务基址；不要与 ZMQ 文字通道混淆 |
| `ASTRBOTEX_SESSION_ID` | `astrbotex_default` | 交互请求使用的 AstrBot 会话标识 |
| `ASTRBOTEX_TIMEOUT_SEC` | `5` | 普通 AstrBot/STT 请求超时；`.env.example` 示例为 `10` |
| `ASTRBOTEX_TTS_TIMEOUT_SEC` | `30` | TTS 合成超时 |
| `ASTRBOTEX_STT_ENABLED` | 空/关闭 | `true` 或 `1` 时创建 STT provider 代理 |
| `ASTRBOTEX_TTS_ENABLED` | 空/关闭 | `true` 或 `1` 时创建 TTS provider 代理 |

`.env.example` 还包含容器镜像、容器名、AstrBot Web/平台端口、MQTT 和 NapCat 等 compose 变量。这些变量由容器编排消费，不一定由 AstrBotEX Python 进程直接读取。

### 19. `perception.json`

默认路径：`profiles/default/perception.json`。

#### `camera`

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `hfov_deg` | `90.0` | 相机水平视场角，单位为度 |
| `image_width_px` | `640` | 用于目标横坐标换算的图像宽度 |
| `image_height_px` | `480` | 图像高度 |
| `to_lidar_yaw_offset_deg` | `0.0` | 相机坐标到雷达坐标的偏航补偿 |
| `x_to_lidar_angle_sign` | `-1.0` | 图像横坐标到雷达角度的方向符号 |

#### `fusion`

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `bearing_tolerance_deg` | `8.0` | 视觉方位与雷达数据允许的角度偏差 |
| `time_window_ms` | `150` | 视觉与雷达观测允许的时间差 |
| `range_min_m` | `0.05` | 可接受的最小距离 |
| `range_max_m` | `12.0` | 可接受的最大距离 |
| `range_window_deg` | `3.0` | 目标方位附近的雷达采样窗口 |
| `range_select_method` | `min` | 窗口内距离选择方式；当前默认取最小值 |

标定参数错误会直接影响目标归属和距离判断。修改后应使用已知方位、已知距离的目标重新验证，不能只检查 JSON 是否可解析。

### 20. `vision_sources.json`

默认路径：`profiles/default/vision_sources.json`。

顶层字段：

- `active_source`：当前使用的视觉源 ID。
- `sources`：视觉源配置数组。

每个 source 支持：

| 参数 | 说明 |
|---|---|
| `id` | 唯一源标识，供活动源和 API 引用 |
| `type` | 当前常用 `mock` 或 `local_api` |
| `enabled` | 是否允许使用该源 |
| `result_endpoint` | 拉取结构化识别结果的地址 |
| `preview_url` | Dashboard 预览地址 |
| `snapshot_url` | 静态快照地址 |
| `timeout_ms` | 单次结果请求超时 |
| `stale_after_ms` | 结果超过该时长后视为过期 |
| `min_confidence` | 目标最低置信度 |
| `metadata` | 源特有扩展信息 |

仓库当前活动源为开发用 `mock_http_vision`，地址指向本机 `8770` 端口。真实部署必须替换或切换到实际视觉服务，并根据网络延迟调整超时与过期阈值。

### 21. `plugins_state.json`

默认路径：`profiles/default/plugins_state.json`。`enabled_plugins` 使用插件 ID 到布尔值的映射，控制发现后的默认加载状态。当前样例启用：

- `rescue_topic_can_controller_plugin`
- `demo_trace_plugin`
- `lidar_pose_plugin`
- `astrbotex_ros2_vision_plugin`

启用标记不保证插件可运行。插件目录、manifest、Python 依赖、系统库、设备权限和外部服务必须同时满足。插件的详细业务参数及 pub/sub 开关也会持久化在该 profile 状态中。

### 22. `connections.json`

默认位置为数据目录下的 `profiles/default/connections.json`。仓库可能不预置此文件；连接通过 API/Dashboard 创建后由运行时持久化。

AstrBotEX 协议连接至少需要正确指定：

- 连接类型及端点地址。
- `protocol_profile=astrbotex`。
- `channel` 为 `text`、`audio` 或 `vision`。
- 是否启用及是否随服务启动。
- 与 A.E.B 一致的协议版本、通道端口和超时策略。

每种业务 feature 应只有一个明确的活动连接，避免请求被路由到非预期 peer。

### 23. 任务 profile

`astrbot_ex/profiles/rescue_ball_2025/mission.json` 当前定义：

- 任务 ID：`rescue_ball_2025`。
- tick 频率：`20`。
- 目标：将 `rescue_target` 移动至 `home_safe_zone`。
- 单次最多处理 3 个实体，其中危险实体最多 1 个。
- 首个实体语义为 `own_normal`。

任务 profile 属于运行行为的一部分。修改目标、限制或频率时，应视为代码级行为变更并执行对应场景测试。

## 维护与验证

- HTTP 路径变更：同步检查 `astrbot_ex/core/api_server.py`、Dashboard 调用方和 `tests/test_api_server.py`。
- Topic 或 manifest 变更：检查所有发布者、消费者、`LocalPluginManager`、插件状态 profile 和相关插件测试。
- proposal/action 变更：检查上下文 TTL、动作 owner、参数 schema、必需 block、新鲜度和运行状态限制。
- ZeroMQ envelope 或 method 变更：同时检查 AstrBotEX `ConnectionManager` 与 A.E.B 的 `zmq_transport.py`/README。
- 环境变量和 profile 变更：同步检查 `.env.example`、`compose.yml`、默认 profile 和部署数据目录。
- 本文记录当前已实现行为；异步视觉上下文、稳定追踪和更多 VLM 帧处理能力应在实现与测试落地后再加入正式接口章节。
