# AstrBotEX

AstrBotEX 是面向具身智能机器人的本地执行运行时。  
当前落地场景是`智能救援赛道履带小车`，但整体架构目标不是只服务这一台车，而是后续可扩展到轮式、四足、多足、人形等多种机器人平台。

## 1. 当前架构定位

整体分层如下：

```text
AstrBot   = Planner / Supervisor
            上层 LLM、任务规划、分解、复盘、工具调度

AstrBotEX = Executor / Arbiter
            中层世界状态、运行时、规则、策略、技能、插件系统、Dashboard

AstrBotC  = Motion Controller
            下层 STM32 实时控制、电机闭环、IMU、编码器、舵机、急停、失联保护
```

当前已经明确的原则：

- `AstrBot` 是军师，不是司机。
- 比赛得分链路必须能够脱离外网和 LLM 独立运行。
- `AstrBotEX` 不应直接发送底层轮速指令作为最终接口。
- 视觉模型不应直接耦合进核心 runtime。
- 视觉、定位、雷达等感知结果应先经过插件标准化，再进入 EX 内部世界模型。

## 2. 当前项目状态

目前已经完成的部分：

- Python 核心 runtime 骨架
- 世界状态、事件总线、规则、策略、技能、运动桥的运行主链路
- 本地插件系统
- 插件按`vision / perception / control / decision / special`五类管理
- 插件启停、上传、卸载、配置保存
- Dashboard 基础页面与插件控制台
- SSE 运行日志流
- 本地 API Server

当前尚未彻底打通的部分：

- 真实 `YOLO` 视觉链路
- 真实 `雷达 / 定位` 感知链路
- 真实 `motion_bridge -> 下位机` 控制链路
- 面向智能救援任务的正式 `policy / skill / rule` 插件
- 完整的硬件闭环联调

## 3. 当前代码结构

```text
astrbot_ex/
  core/         Runtime、API Server、世界模型、插件管理器
  interfaces/   各类插件稳定接口
  profiles/     配置与任务资料
  plugins/      本地插件目录

dashboard/      前端静态页面
scripts/        本地启动脚本
```

## 4. Runtime 当前行为

当前 runtime 的主链路是：

```text
vision_provider
  -> world_builder
  -> rule plugins
  -> policy plugin
  -> skill plugin
  -> safety guard
  -> motion_bridge
```

实际行为特点：

- `start()` 负责启动本地运行循环，不把真实硬件链路作为启动前置条件
- 缺少 `vision` 插件时，`tick()` 会记录 `vision provider unavailable`，并生成空视觉结果
- 缺少 `motion` 插件时，`tick()` 会记录 `motion bridge unavailable`，并生成 `link_ok=false` 的机器人状态
- `tick()` 会拉取视觉结果与机器人状态，更新世界模型
- `rule` 可以对世界状态或意图进行拦截
- `policy` 负责选目标
- `skill` 负责执行目标
- `motion_bridge` 负责将上层意图转给下游控制系统

这意味着当前 EX 已经不是纯界面原型，而是具备真实执行运行时骨架。

## 5. Mock 现状

历史上项目中存在 mock 闭环，用于早期验证：

```text
MockVisionProvider
MockMotionBridge
BasicRulePlugin
NearestEntityPolicy
ApproachEntitySkill
```

当前状态：

- 默认 runtime 已不再自动注册完整 mock 闭环
- runtime 不会再自动刷 mock 视觉 / mock 技能日志
- 缺少真实 vision 或 motion 插件时不会阻塞 API 层启动；运行循环会用缺失状态事件暴露链路问题

这说明项目方向已经从“演示原型”切到“真实插件接入”。

## 6. 插件系统现状

当前支持的 capability：

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

Dashboard 中按五大类展示：

```text
vision
perception
control
decision
special
```

默认映射关系：

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

支持的插件目录形式：

```text
plugins\<plugin_id>
plugins\vision\<plugin_id>
plugins\perception\<plugin_id>
plugins\control\<plugin_id>
plugins\decision\<plugin_id>
plugins\special\<plugin_id>
```

本地插件系统已经支持：

- 扫描插件目录
- 读取 `plugin.json`
- 读取 `config.schema.json`
- 读取 `config.json`
- `zip` 上传安装插件
- 启用 / 停用插件
- 卸载插件
- 配置更新后重新加载

### 6.1 插件线程模型

每个已加载插件由 Core 分配一个独立的 `PluginActor` 工作线程。插件的生命周期、`on_tick()`、设备读写和运行时方法都在该线程中串行执行。

约束如下：

- 插件不得自行创建 `threading.Thread`
- 阻塞式设备插件通过有超时的 `on_worker_step()` 完成一次 I/O
- 同一物理设备或总线只能由一个插件线程持有
- Runtime 的 tick 投递会合并，慢插件不会无限积压旧 tick
- 插件间数据通过 `TopicBus` 最新值或 `PluginContext.subscribe()` 有界 inbox 交换
- 插件卸载前 Core 会停止运行期 I/O、执行生命周期回调并等待线程退出

`GET /api/status` 的插件状态中会返回 Actor 线程名称、存活状态和最后一次错误。

## 7. Dashboard 当前能力

当前 Dashboard 已具备：

- runtime 状态卡片
- 运行快照
- SSE 运行日志页
- 按分类展示插件
- 单插件控制页面
- 基于 schema 自动生成配置表单
- 插件启用 / 停用
- 插件卸载
- 插件压缩包上传

当前已经不再只是“列表 + 右侧详情”的静态壳子，而是开始向真正的插件控制台演进。

## 8. API Server 当前状态

启动方式：

```powershell
cd D:\Code\AstrBotEX
.\scripts\run_api_server.ps1
```

或者：

```bash
python -m astrbot_ex.core.api_server --host 0.0.0.0 --port 8765 --tick-hz 20
```

环境变量：

```text
ASTRBOTEX_HOST=0.0.0.0
ASTRBOTEX_PORT=8765
ASTRBOTEX_TICK_HZ=20
ASTRBOTEX_DATA_DIR=/app/data
```

`ASTRBOTEX_DATA_DIR` 是可选项。未设置时继续使用项目根目录下的：

```text
plugins/
profiles/
```

设置后，运行数据会改为：

```text
$ASTRBOTEX_DATA_DIR/plugins
$ASTRBOTEX_DATA_DIR/profiles
```

Docker / compose 部署时建议只挂载统一数据目录：

```text
./data:/app/data
```

这样拉取新镜像时，核心代码、Dashboard 和脚本跟随镜像更新；插件、配置、启停状态和 profile 保留在宿主机 `data` 目录。

当前要特别注意的一点：

- API Server 仍然通过 `build_demo_runtime()` 构造 runtime
- 这说明 server 骨架是真的，但默认启动组合仍不是最终智能救援正式组合

当前核心接口：

```text
GET    /api/status
GET    /api/events
POST   /api/runtime/start
POST   /api/runtime/stop
GET    /healthz
```

当前 EX 接口：

```text
GET    /api/v1/ex/status
GET    /api/v1/ex/events

GET    /api/v1/ex/plugins
GET    /api/v1/ex/plugins/{id}
POST   /api/v1/ex/plugins/{id}/enable
POST   /api/v1/ex/plugins/{id}/disable
POST   /api/v1/ex/plugins/{id}/config
DELETE /api/v1/ex/plugins/{id}
POST   /api/v1/ex/plugins/upload
GET    /api/v1/ex/plugins/{id}/cover
GET    /api/v1/ex/plugins/{id}/dashboard

GET    /api/v1/ex/vision/sources
POST   /api/v1/ex/vision/sources
PUT    /api/v1/ex/vision/sources/{id}
DELETE /api/v1/ex/vision/sources/{id}
POST   /api/v1/ex/vision/sources/{id}/test
GET    /api/v1/ex/vision/active-source
POST   /api/v1/ex/vision/active-source
GET    /api/v1/ex/vision/latest
```

## 9. 当前最真实的进度判断

一句话概括：

`AstrBotEX 的平台骨架已经搭好，当前阶段是从“框架成立”进入“真实感知与控制接入”。`

换句话说，现在最值钱的东西已经不是“有没有页面”，而是：

- runtime 的执行链路已经存在
- plugin manager 已经可用
- dashboard 已经具备管理插件的能力
- API server 已经能提供前后端联通的基础能力

当前真正缺的是：

- 真实视觉
- 真实感知 / 定位
- 真实下游控制
- 真实任务技能

## 10. 推荐的下一步方向

建议按下面顺序推进：

1. 完成真实 `vision_provider`
2. 完成真实 `perception` 插件链路
3. 完成真实 `motion_bridge`
4. 补齐智能救援任务的 `rule / policy / skill`
5. 做端到端本地自治闭环验证

## 11. 不建议现在做的事

- 不要恢复默认 mock 自动闭环
- 不要让视觉插件直接承担业务决策
- 不要让 EX 核心直接绑定某一种 YOLO 原始 JSON 格式
- 不要现在就把所有功能一次性插件化到底

## 12. 访问 Dashboard

启动服务后访问：

```text
http://127.0.0.1:8765/
```

如果页面没有更新：

```text
Ctrl + C 停止旧服务
重新执行 run_api_server.ps1
浏览器 Ctrl + F5 强制刷新
```
