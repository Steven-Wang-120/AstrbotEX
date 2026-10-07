> 历史施工背景（2026-10-06 标注）：下文旧 shadow-only、GET-only probe、/mode 不启动 runtime 等为旧基线，非最终 v2 合同。C06 正在整改，当前合同及最终源同步 TODO 见 [B08](../B08-DECISION-MANAGEMENT.md)，历史限制见 [来源说明](../LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

# B08｜高风险｜决策管理 API 与密钥保护

更新：2026-10-02。状态：**B08 已完成；定向验证与一次真实 GPU HTTP 管理闭环通过**。上游同步基线：AstrBotEX `c9624a4`。B09 仍待开发。

2026-10-01 的同步阶段只同步上游、验证已有能力和整理任务书；该阶段的历史报告保持原样。后续已按单独授权实施 B08。以下保留原任务的需求与阶段，用于交接和复核，不把机器人或真实模型质量写成已通过。

历史验证边界见 [技术交接](../B08_DECISION_MANAGEMENT_RESULT.md)。本页保留旧任务背景；C06 v2 与 core/UI 尚在整改，旧定向/GPU 测试结果不能作为新接口通过证据。当前冻结 API 与最终源同步 TODO 见 [B08 说明](../B08-DECISION-MANAGEMENT.md)。

## 1. 可行性、依赖与最小范围

B08 已基于 B02/B04 的 ActionService、Ledger、GoalManager、DecisionService 和生命周期接线实施。B07 已提供 Mock/Jev/Laya 工厂。Jev 的 `execution_allowed=False`；普通装配的 Laya 只允许 shadow。Laya execute 仅由可信、隔离的测试 Actor 装配授予，不能从 HTTP 配置、页面或备份获得。Laya 本轮是 EX 决策后端，不是控制插件中的轨迹评分器。

复用 `astrbot_ex/core/api_server.py` 的 HTTP/SSE 服务和 `build_server()`。复用 `core/decision/service.py` 的可信方法，不创建第二个管理服务器。不修改冻结的 Goal、Action、DecisionSnapshot 线协议。

| 已有接口或组件 | 复用能力 | 本轮管理接线 |
|---|---|---|
| `status()`、`snapshot()` | 缓存状态、最近构建的快照 | 已增加实际请求、模型返回及 EX 受理结果的关联记录 |
| `set_mode()`、`request_stop()`、`review()` | 模式门禁、公开停止回执、停止复核 | 已增加受控入口和异步操作；停止状态为 requested/running/proven/failed |
| `configuration_changed()`、`replace_backend()` | 版本失效、可信后端切换 | `replace_backend()` 已实现；管理端构造新实例并通过它应用，不直接替换内部字段 |
| `DecisionBackend.decide/close`、Laya `probe/status` | 调用、关闭、独立 GET /health | Laya probe 已有；Mock 为本地构造，Jev 真实探测未实现，不假定统一 healthcheck/capabilities |
| `execution/actions.sqlite3` | 动作事实、幂等、停止证据 | 已增加只读分页投影；不能从配置备份恢复执行事实 |
| 当前 HTTP 路由 | 服务、静态页面、SSE | 已加入统一鉴权、Host/Origin、敏感读取和旧入口校验；旧页面仅做鉴权适配 |

新增小型 `core/decision/config.py`、`management.py`、`history.py`、`owned_laya.py`，分别承担配置/密钥、路由/操作、请求追踪和自有 Laya 进程管理。对 service/backend 只增加管理所需的可信方法和有界追踪 hook，不重写动作系统。

2026-10-01 的同步回归曾有停止证明重试后的断言失败，后续 B04 增加公开停止回执与可信 `replace_backend()`，保留业务停止断言。技术边界见 [停止与切换交接](../B04_BOUNDARY_20261001_RESULT.md)。历史失败不代表接口尚未实现；停止成功仍须匹配公开 proven，不能只检查私有 pending。

## 2. 配置与访问规则

配置写入 `profiles/default/decision.json`；只保存 `secret_ref`，不保存密钥。密钥和管理凭据放在 `data_root/secrets/`，目录 0700、文件 0600，位于 profiles/plugins 快照 roots 之外。管理凭据与供应商密钥分开。

本轮不泄密要求覆盖新增管理凭据和 Jev secret。旧连接配置中的 WebSocket token 仍可能在受鉴权的旧 GET/ZIP 中出现；用户已确认暂不迁移，后续另行安排，不能标成已解决。

服务绑定 `127.0.0.1`，远程使用 SSH 隧道。浏览器通过 Authorization 头提交管理凭据，只保存在页面内存；刷新后重新输入。不放在 URL、localStorage、日志或诊断导出中。

管理访问检查覆盖新增敏感读取和写操作，也覆盖本 MVP 可使用的旧 runtime/control/environment/restore 入口，防止从旧路径改变执行状态。管理响应不使用通配 CORS。检查允许的 Host；带 Origin 的请求必须同源。无 Origin 的本地脚本仍需管理凭据。只保护 decision 新路由不足以声称所有控制入口受保护。

SSE 只发送无敏感内容的变更通知，不广播完整模型输入或密钥。详细信息从受控 GET 拉取。管理页不依赖 SSE 作为唯一状态来源。

配置分为 saved 与 effective 两份投影，返回 `revision`、`effective_revision` 和 `ex_session`。写请求带 `expected_revision + ex_session`。版本不符返回 409，不覆盖新配置。

首版仅在 decision disabled、无活动/未确认停止动作、旧后端请求已结束时修改影响后端的配置和密钥；否则返回具体冲突。保存只校验和持久化，不连接、不启用、不启动 runtime。显式模式操作才通过可信服务应用保存配置，并使旧授权失效。

模型、密钥引用、后端或有效配置变化都必须更新可信配置版本。浏览器管理 revision 与框架 `config_revision` 分别展示，不假定两者相等。

## 3. API 合同

以下为**已实施接口**，前缀 `/api/v1/ex/decision`。成功响应携带当前会话和版本；错误响应包含稳定 `code`、简短 `message` 和必要的当前版本，不返回原始供应商异常。

| 方法 / 路径 | 输入与输出 | 行为 |
|---|---|---|
| GET `/status` | 模式、健康、活动/待替代 Goal、blocked、版本 | 只读缓存，不触发探测或执行 |
| GET `/backends` | Mock/Jev/Laya 字段目录、执行能力、限制 | Jev execute 不可用；Laya 是 EX 后端，普通装配 shadow only |
| GET `/config` | saved/effective、secret configured | 不返回真实密钥或可恢复密钥的片段 |
| POST `/config` | expected_revision、ex_session、配置 | 校验、原子保存；409 不产生部分写入 |
| POST `/secret` | set/keep/clear、会话、版本；仅 set 使用 value | 空值不是 clear；keep 不增版本，set/clear 明确修改 |
| POST `/test` | 已保存配置版本、会话 | 独立 probe，返回 202/operation_id；不使用活动 Goal |
| POST `/mode` | disabled/shadow/execute、会话、版本 | 显式应用配置；遵守 execution_allowed；不启动 runtime |
| POST `/stop` | 当前会话、reason | 202，撤销执行授权并取消；不因配置 revision 过旧拒绝停止当前会话 |
| POST `/service/start` | 会话、版本 | 显式启动自有 Laya、GET health 和固定预热；完成仍 disabled |
| POST `/service/stop` | 会话、版本 | 先撤授权和停止证明，再确认自有进程退出 |
| POST `/service/recover` | 会话、版本 | 旧进程退出、新服务预热、新后端应用；不重放，仍需新授权和 Goal |
| GET `/operations/{id}` | 操作 ID | pending/running/succeeded/failed/blocked/superseded、原因与关联版本 |
| GET `/catalog` | 动作目录和资格原因 | 已安装、启用、可执行分别展示；不从缺少 start 猜测原因 |
| GET `/snapshot` | current_goal、pending_goal、last_built、last_submitted、last_result | 构建、请求与已完成结果分开，标明历史/过期状态 |
| GET `/decisions` | cursor、limit（默认 20，最大 100） | 有界决策历史，含最终受理/拒绝结果 |
| GET `/actions` | cursor、limit、状态/command_id 过滤 | 账本只读投影，不补造或重放终态 |

异步操作表保留最近 128 个已完成操作，活动操作独立上限 8，不因裁剪丢失。ID 不重用，过期返回明确不存在。202 只表示接收。stop 只有在服务完成停止复核并取得证明后才能成功；未证明为 blocked/unknown，不伪报 stopped。

Jev 固定地址 `https://api.typesafe.ai/v1/systemone` 只读展示，不做任意 URL 配置。`test` 不提交 Goal、不调用 Dispatcher、不启用插件、不续租。Mock 仅本地构造检查；Jev 报真实探测未实现，不产生付费调用；Laya 使用已有 `probe()`，只请求 GET /health，不进行推理或清除 restart_required。固定快照预热属于显式 service/start，不属于 test。

## 4. 实际请求追踪

`snapshot()` 仍是最近构建快照，不能单独证明实际发送了什么。本轮在调用边界捕获 Laya 的真实序列化 body、hash、返回结果和 EX 受理结果。Jev 仅关联现有 hash/耗时/错误诊断，未新增完整 body/响应追踪或云端调用；缺少实际记录时明确标记，不重建虚假请求。

在真实调用边界捕获请求：

1. 记录快照进入后端的时点与不可变副本。
2. Laya 在调用边界记录实际序列化 body 的 hash 和受限展示内容；Jev 的完整内容缺项如实为空；禁止采集 Authorization 头。
3. 关联模型返回、框架版本复核和最终受理结果。
4. 区分 prepared、post_attempted、post_written_to_socket、response_received；socket 写入不证明远端接收或动作完成。

记录 request/snapshot/command ID、ex_session、Goal/config/catalog/environment/plugin 版本、模型、开始/结束时刻、耗时、错误码。单条记录展示上限 64 KiB、最近 128 项，超出时写明 `record_truncated`、各载荷 `truncated` 和原始字节数；hash 计算基于实际输入，不基于截断文本。原始敏感观测不额外持久化。

本轮 EX Laya 的候选和实际选择通过请求历史关联。未来控制端的独立轨迹评分可通过观测/动作 `details` 投影，但这不是本轮已实现能力。若源信息不存在，返回未接入，不重新构造虚假模型输入。

## 5. 开发步骤和交接

| 步骤 | 工作 | 本步完成条件 |
|---|---|---|
| B08.1 | 固定响应样例、错误码和配置 CAS | 非法配置零写入；并发保存仅一个成功 |
| B08.2 | 管理凭据、secret store、旧入口覆盖 | 未授权无法读取敏感数据或改控制状态 |
| B08.3 | service 受控切换/probe/operation | 保存、探测、启用、runtime 启动互相独立 |
| B08.4 | 实际请求 hook、账本/目录投影 | 页面可区分构建、发送、选择、受理和执行 |
| B08.5 | stop、恢复、并发失效 | 停止依赖证据；恢复不激活旧任务 |
| B08.6 | 测试、接口样例交给 B09 | 包含空态、冲突、stopping、blocked、过期结果 |

原开发步骤与完成条件保留，用于后续交接复核。接口样例见 B08 实现说明和本批测试 fixture，不新增一套文档服务。B09 接口前置已具备，页面仍待单独开发；本轮没有实施 B09 或控制端机器人 MVP。

## 6. 测试与通过标准

原任务建议 `test_decision_http.py`、`test_decision_config.py`、`test_decision_secrets.py`，复用现有 loopback/temp data_root 测试方式。当前按仓库风格实现于 `test_decision_management_http.py`、`test_decision_management_history.py`、`test_decision_management_operations.py`、`test_decision_management_interleaving.py`、`test_owned_laya.py`、`test_decision_management_boundaries.py`、`test_decision_secret_redaction.py`；场景和业务断言继续以下表为准。

| ID | 场景与必须断言 |
|---|---|
| D01 | 配置类型/边界、会话/CAS 冲突、运行中切换；失败不改变 saved/effective |
| D02 | 新管理/Jev 密钥 set/保留/clear；唯一测试标记在响应、SSE、日志、ZIP、导出中出现次数为 0；不把旧 WS token 标成已迁移 |
| D03 | 缺/错凭据、跨源、恶意 Host、旧控制/恢复入口；敏感数据和控制状态不泄露 |
| D04 | probe 成功/失败/超时；Goal、Ledger、续租、runtime/plugin 状态均无变化 |
| D05 | 请求构建未发送、实际发送、过期返回、截断；hash 和关联 ID 对应实际数据 |
| D06 | mode/stop 并发及迟到操作；旧请求不能重新开门，Jev 和普通装配 Laya 不能进入 execute |
| D07 | 备份/恢复和重启；密钥不导出，旧 Goal 不激活，execution Ledger 不回滚 |
| D08 | 发布/ACK/执行终态区分；无 StopEvidence 时保持 stopping/blocked |

回归现有 api_server、backup、decision/runtime 测试中受影响的范围。日志保存测试命令、环境、退出码和原始输出；不因失败修改原断言。

## 7. 人工介入与回滚

开发完成后，用户只需首次输入管理凭据并查看停止状态演示。真实 Jev 云端测试、对局域网开放或新增付费服务不属于本 MVP 默认动作，发生范围变化时再询问。

回滚前先停止动作并取得证据，再切 disabled 和回退服务。保留执行账本；密钥丢失重新输入，不从普通备份恢复旧动作。
