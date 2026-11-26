# B08 交接报告：唯一共享入口集成

## 结论：BLOCKED

B08 的全部产出是把 B00—B07 接成可用的诊断 API、告警 SSE、环境页面与黑色日志台。
按 C07 的定义，本任务当前状态为 **BLOCKED**：上游契约尚未交付，无法在不臆造
B00—B06 内部实现的前提下完成集成。

任务书本身也写了“正式集成等上游契约交付；可以先准备集成测试，不复制上游生产实现”。
本报告记录阻塞证据、解除条件和已核对过的接线计划，不做推测性实现。

## 阻塞证据

### 上游分支无提交

| 分支 | HEAD | 说明 |
| --- | --- | --- |
| `main` | `c1a4b89` | 基线 |
| `feat/ros-diag-b01-b03` | `c1a4b89` | **与基线相同，无提交** |
| `feat/ros-diag-b04-b06` | `c1a4b89` | **与基线相同，无提交** |
| `feat/ros-diag-b07-b09-20261031` | `d9c6dee` | 本 agent 的 B07（B09 在独立 YOLO 仓库） |

### 上游产物全部缺失

在 `d9c6dee` 的工作树上逐项检查：

| 产物 | 归属 | 状态 |
| --- | --- | --- |
| `astrbot_ex/core/diagnostics/`（`models.py`/`rules.py`/`__init__.py`） | B00 | **缺失** |
| `astrbot_ex/core/environments/ros2/preflight.py` | B01 | **缺失** |
| `contracts.py` / `plugin_api.py` / `ros2/adapter.py` 的 `data_health` 改动 | B02 | **缺失**（三文件仍为基线哈希） |
| `astrbot_ex/core/event_stream.py` 与 `event_bus.py` 保留字段 | B03 | **缺失** |
| `astrbot_ex/core/diagnostics/service.py` | B04 | **缺失** |
| `dashboard/logs.js`、`dashboard/logs.css` | B05 | **缺失** |
| `dashboard/environment-diagnostics.css` | B06 | **缺失** |
| `environments.js` 的诊断界面改动 | B06 | **缺失**（仍为基线哈希） |

### 共享文件基线未被他人改动

B08 独占的四个文件仍与 C02 编制时记录的 SHA256 完全一致，可以安全地在其上集成：

| 文件 | SHA256 前缀 | 与 C02 一致 |
| --- | --- | --- |
| `astrbot_ex/core/environments/manager.py` | `25f91972` | 是 |
| `astrbot_ex/core/api_server.py` | `53bac255` | 是 |
| `dashboard/app.js` | `1a33465e` | 是 |
| `dashboard/index.html` | `30260dd2` | 是 |

## 为什么不先写推测性实现

三个具体理由，不是“等别人”：

1. **会与上游真实交付冲突。** B08 要调用 `DiagnosticsService(collector, preflight_collector, event_bus, clock_ns=..., tick_sec=...)`、
   `open_stream(last_event_id)`、`set_diagnostics_activity(active, now_ns)`、`diagnostics_sample(now_ns)`。
   C01 冻结了签名，但没有冻结 collector 的返回形状细节、`_Port` 内部字段的可读边界、
   以及 B03 流对象的实际并发行为。按猜测写一遍接线，等上游落地后必须整体重写，
   而且中间态会占用四个独占文件，其他 agent 无法并行。
2. **任务书明确禁止。** “不复制上游生产实现”——如果我现在实现一份 `DiagnosticsService`
   或 `open_stream` 的替身，就是把上游实现抄进 B08。
3. **写了也验证不了。** 全部成功条件（告警从同一 EventBus 进同一条鉴权 SSE、
   端口页与日志 code/scope 一致、20 轮无残留）都要求真实上游存在。
   跳过（skip）的测试不计入通过数，交付一批永久 skip 的文件只是噪声。

因此本任务不提交任何无法执行的测试文件。解除阻塞后，`tests/test_diagnostics_http.py`
与 `tests/test_diagnostics_integration.py` 一次写成可运行的版本。

## 解除条件

按依赖顺序，以下每一项都需要先落地：

| # | 需要的东西 | 谁交付 | 解除后 B08 能做什么 |
| --- | --- | --- | --- |
| 1 | `diagnostics/{models,rules}.py`，`evaluate_port_health` / `evaluate_preflight` | B00 | manager collector 调用纯规则 |
| 2 | `preflight.py::collect_preflight` | B01 | 预检注入 snapshot |
| 3 | `_Port.set_diagnostics_activity/diagnostics_sample`、`status()['diagnostics']`、`data_health` | B02 | manager 采样与 activity 同步 |
| 4 | `event_bus` 保留字段 + `event_stream.py::open_stream` | B03 | `_send_events` 重写、SSE id/control/重放 |
| 5 | `diagnostics/service.py::DiagnosticsService` | B04 | `start_diagnostics` 门面、refresh 合并 |
| 6 | `logs.js`/`logs.css` + `EXLogs` API | B05 | `app.js` 转接、`index.html` 引入 |
| 7 | `environment-diagnostics.css` + `bindEnvironmentActions` | B06 | 环境页诊断容器与按钮 |

解除后必须先在合并分支上跑各上游自己的测试，再开始接线（任务书步骤 1）。

## 已核对的接线计划（解除后直接执行）

### manager.py

- 增加诊断 collector：先取 environment 身份 → 短暂复制 owners/ports → 按 `port.lock`
  复制标量 → **再取一次身份确认未变**，否则标 `partial`。探测 `/sys`、调纯规则、
  发 EventBus 全部在资源锁之外。
- 快照不得包含 native 对象、ROS 节点或图像数据。
- 监听 `runtime_state` **与** `fault`（`runtime._fault` 发的是 `fault`，不是 `runtime_state`）。
  回调只在独立小锁记录 active 与 `activity_epoch`、单调时间，然后通知诊断失效；
  **不在回调里遍历 ports 或探测系统**，避免 runtime 锁 → resources 锁的反向依赖。
- `_transition` / 配置保存 / owner 移除 / `reset_after_restore` 时 `invalidate` 旧身份。
- `close()`：先禁止新采样与新事件，再有限回收；**无论诊断是否异常，都必须继续原有动作停止流程**。
- 构造 `EnvironmentManager` 仍不自动起线程；由 `build_server` 在 runtime_running
  注入后显式 `start_diagnostics`，保持大量直接构造 manager 的单测隔离。

### api_server.py

- composition root 组装 collector + `DiagnosticsService`，替换内嵌 `EventStream` 为 B03 实现。
- `_environment_api` 加两个路由：`GET /diagnostics`（200，只读缓存，不触发探测）、
  `POST /diagnostics/refresh`（严格校验 `expected_session`/`expected_revision`，
  缺 400、旧 409、通过 202）。refresh **不改 environment revision、不 select、不 start**。
- `_send_events`：发送 200 头**之前**用 `Last-Event-ID` 验证游标；`event` 帧走
  现有 `management.sse_payload` 后加 `id:`；`log_control` 帧同样脱敏；保留 10 秒 keepalive；
  `finally` 关流、退订。
- 鉴权在路由前拒绝（未鉴权 401、非法 Host/Origin 403、关闭中 503），沿用现有
  `_authorize_http` 与 `sanitize`。**不新增无鉴权别名。**
- `decision_changed` 白名单保持不变；`recent_events` 播种复用同等白名单投影。

### app.js / index.html

- CSS 顺序 `styles → environment-diagnostics → logs`；`logs.js` 在 `app.js` 之前加载，
  不用 module/import，不引入构建链。
- `app.js` 只保留薄转接：`pushEvent`/`renderEvents` 转发给 `EXLogs`，播种走 `seed`。
  **删除 `MAX_TRACE_EVENTS=200` 与第二份 `state.events` 缓存**，避免双写；
  其它读 `state.events` 的地方改为只读兼容。
- `connectEvents` 维持 `managementFetch`/`AbortController`/凭据 epoch；parser 增加
  `id:`、多行 `data:`、`log_control`；重连带 `Last-Event-ID`；业务事件按 ID 去重后
  再 `DecisionPage.notify` 与环境刷新。
- `ros_diagnostic` 触发诊断缓存刷新但走原调度节流；调用一次
  `bindEnvironmentActions` 启用“重新检查”；移除旧 app 日志按钮监听，由 `EXLogs` 独占。
- `clear` 不清重连游标；服务器 `reset` 换 ID 空间并提示；凭据更换清旧 epoch 的
  连接/游标/待通知。

## 解除后要执行的验证

```text
python -m unittest discover -s tests -p 'test_diagnostics_http.py' -v
python -m unittest discover -s tests -p 'test_diagnostics_integration.py' -v
python -m unittest discover -s tests -p 'test_environment_http.py' -v
python -m unittest discover -s tests -p 'test_api_server.py' -v
python -m unittest discover -s tests -p 'test_decision_management_http.py' -v
python -m unittest discover -s tests -p 'test_decision_secret_redaction.py' -v
python -m unittest discover -s tests -p 'test_environment_boundaries.py' -v
```

UI 侧另有 B05 的 `verify_log_ui.py`、B06 的 `verify_environment_diagnostics_ui.py`
和适配后的 `scripts/verify_environment_ui.py`；无 Playwright 时按 NOT_RUN 记录，
不用静态解析 JS 代替 UI 结论。

## 与 B07 / B09 的关系

- **B07** 已交付（commit `d9c6dee`）：opt-in 部署模板、无观察节点验收脚本、
  三份 ROS 文档、测试 fixture。原生 synthetic/entry 两轮为 NOT_RUN（本机无 ROS），
  且脚本实际轮次依赖 B08 的 `GET /diagnostics`，所以原生联调排在 B08 之后。
- **B09** 已交付（独立仓库 `D:\Code\astrex-yolo`，commit `d803bb4`）：视觉健康信号，
  单测 39 项通过。其 `vision_health` INFO 事件要可见，同样依赖 B03 的保留规则和
  B08 的 SSE 接线。
- **B10** 的合并验收不能在 B08 完成前写“全量通过”。当前可写的结论是
  “B07 模块与脚本通过、B09 插件单测通过、B08 阻塞、原生与视觉验收未完成”。

## 脱敏说明

本报告不含管理凭据、token、私有数据库或现场 IP。基线哈希为公开源码的 SHA256。
