# B08 决策管理 API 与页面

管理层复用 [HTTP 装配](../astrbot_ex/core/api_server.py)、[DecisionManagement](../astrbot_ex/core/decision/management.py)、[配置/SecretStore](../astrbot_ex/core/decision/config.py)、[OwnedLayaService](../astrbot_ex/core/decision/owned_laya.py) 与 [RequestHistory](../astrbot_ex/core/decision/history.py)。正式 Goal 走 [任务契约](DECISION-CONTRACT.md)，选择/执行走原 Action/Actor/Ledger，不另造执行器。

## 1. 访问与秘密边界

从仓库根运行：

```sh
python -B -m astrbot_ex.core.api_server --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/#/decision`，使用启动输出所指本机凭据文件，在现有凭据栏输入。管理 HTTP 只绑定回环，远程经 SSH 隧道。所有 `/api/`（含旧别名、runtime/插件/环境/备份入口）需要管理 Bearer；Host 须合法 loopback，带 Origin 时须同源，无 Origin 的脚本也须鉴权。敏感响应 no-store，无通配 CORS。

admin token、Jev key、Laya key 分离。GET 只返回引用/存在状态，不返回 key 值；页面凭据仅内存，不放 URL、localStorage、日志或导出。secrets/execution 不随 profiles/plugins 配置快照备份，任务 DB/反馈/账本不是可清理的验收垃圾。

Jev `secret_ref` 与 Laya `laya_secret_ref` 各自独立。set 需合法非空 value；keep/clear 不带 value，keep 不写入/增 CAS，set/clear 增 CAS。原子存储采用同目录暂存、文件 fsync、immutable secret no-clobber、回滚/恢复 marker，POSIX 另做目录 fsync。Windows 使用可选 fchmod、明确 fd 关闭和 owned `.exe` preflight；POSIX mode 不证明 Windows ACL，Windows 发布不承诺同等目录崩溃持久性。

`storage_write_uncertain` 保留恢复 fence 并阻止后续写入/backend mapping/owned 启动，不能因 get() 可读就认为已持久化，或删除 marker/key 解锁。`secret_cleanup_failed` 可能发生于 CAS 已提交之后，须刷新而非盲重试旧 revision。旧 WebSocket token 的连接 GET/快照存储风险仍独立存在。

## 2. 保存、生效与 CAS

响应关联 `ex_session/revision/effective_revision/framework_config_revision`。saved 是磁盘配置，effective 是已应用配置，二者不等于已运行。除停止边界外写请求使用最近 GET 的 `ex_session + expected_revision`；409 重新读取并核对，保留草稿，不自动重放。

保存只校验/持久化，不探测、启动模型/runtime、切模式或提交 Goal。影响后端的配置、key 与切换要求 disabled、无活动/待替代 Goal、无未完成动作/旧请求且停止已证明。可信 `replace_backend()` 不接受 HTTP callable/import 路径/shell/allow_test_execution。

schema v2 配置：

- Jev：`mode=disabled|shadow|execute`、live HTTP、deadline/限频/min_confidence；`service_connection={base_url:"https://api.typesafe.ai",model:"jev-1.13.0",auth_mode:"bearer"}`，未核对 alias 拒绝。
- Laya：预算与 enabled/live/execution 字段；`service_connection={mode:"external"|"owned",base_url:"http://127.0.0.1:8769",model:"typed-decisions",auth_mode:"none"|"bearer"}`。external 默认 deployment=null，不依赖本地 Python/cache/CUDA 或 owned generation；owned 部署见 [B07](B07-LAYA-BACKEND.md#2-外部连接与可选自启冻结-v2-合同)。
- URL 前缀追加供应商路径，HTTPS 保持 TLS 验证、拒绝重定向/URL 凭据/query/fragment；明文 HTTP 仅 loopback。
- 唯一 validated 转换入口 `DecisionConfigStore.backend_config()`；owned `laya_deployment(...,output=...,state_path=...)` 的输出/状态路径由可信装配提供。
- v1 Laya 迁到 owned/deployment=null，补部署配置前不可启动；迁移不联网/认领/启动，保存时写回 v2，不凭迁移获得执行。

严格 JSON、大小预算与未知字段拒绝保留，秘密不混入 saved 对象或错误文本。

## 3. HTTP 路由与启停合同

前缀 `/api/v1/ex/decision`：

| 方法与路径 | 行为 |
|---|---|
| GET `/config` | saved/effective、secret presence 与版本 |
| POST `/config` | CAS + 完整 config，保存不生效 |
| POST `/secret` | provider（默认 Jev）、set/keep/clear；set 才含 value |
| POST `/test` | 202 + operation；可测 draft/provider/临时 key，不保存或应用 |
| POST `/mode` | disabled/shadow/execute；execute 是 runtime 激活操作 |
| POST `/stop` | 当前 session；先撤执行，旧 config revision 不阻止停止 |
| GET `/operations/{id}` | 异步状态，未知/过期 404 |
| GET `/view` | 精简供应商/运行/当前任务投影 |
| GET `/status`、`/backends` | 只读诊断/能力，不触发调用 |
| GET `/catalog`、`/snapshot` | 目录资格、Goal、构建/请求/结果诊断 |
| GET `/decisions`、`/actions` | 有界请求历史与 Ledger 只读分页 |
| POST `/service/start`、`/service/stop`、`/service/recover` | 所选 owned 专用，external 不调用这些生命周期 |

`/test` 经 validated draft 工厂捕获所选 key，临时启用 live HTTP、关闭执行并 finally close。Jev/Laya 都用固定 wait-only 合成快照和生产 decoder，不提交业务 Goal/Action/plugin/账本，不保存、不推进 CAS、不授 execute。Laya health 与 inference/鉴权/协议成功分开。probe binding 校验当前 intent/sequence、EX session、provider、saved config 和 credential；临时草稿成功不能标为 saved connection verified。probe 不清 remote unresolved。

execute 应用所选配置，owned 才启动/复用受 generation guard 的自有服务；经 RuntimeController 切 decision control mode、启动 runtime，再启用 decision execute。running 要同时满足上述状态和 production backend gate。无 Goal 时 Dispatcher 动作 gate 仍关闭；Start 不提交 Goal，也不额外推理外部服务。缺 key、runtime/provider 失败撤授权并保留 failed/uncertain，不自动回退 legacy。HTTP 不授 fixture transport 的测试权限。

stop 先撤销新执行、等匹配 framework proof/Goal 退役，再停 runtime，最后结束可证明归属的 owned 句柄；external 不 kill。runtime 关闭必须复核 registry stop_error/state/runtime_started/runtime_starting/stop_proven 和 plugin_fault/fault 事件，IDLE 单独不证明已停。失败显示 uncertain、禁止重启，后续真实 Stop 成功才解除；runtime 停止失败也继续尝试结束自有服务。这是框架生命周期证据，不是硬件物理停车。

生命周期锁顺序为 apply→service；恢复冷启动释放 service 锁后再取 apply→service。旧意图/manager/generation 的清理不能杀掉新操作接管的进程。owned manager 捕获 key 引用和值，只有旧 generation 确认退出、无 active/draining 请求、quarantine 或 ownership_unknown 时才重建；换 key 不解除隔离。进程退出与 Actor StopEvidence 不可互换。

## 4. 精简 view 与可信当前任务

GET `/view` v1：`schema_version/ex_session/revision/effective_revision/provider/connection/ex/task/error`。

- connection：`state/code/message`，state 为 unverified/testing/verified/failed/disconnected。
- ex：`state/can_start/can_stop/message`，state 为 disabled/starting/running/stopping/failed/uncertain；从真实状态和 proof 得出，不从按钮或 probe 推断。
- task：`available/phase/title/current_goal/completed/total/can_cancel:false/updated_at/message`；updated_at 是 epoch seconds，缺投影就 unavailable，不能用旧 Goal 补造 idle/当前任务。
- error：null 或 `{code,message}`；不返回 raw JSON、完整计划、用户身份、参数或 tool trace。

任务经独立 text `task.projection.get` 查询，请求严格为 `{schema_version:1,ex_session:...}`。Host routes 限定 peer/robot/session，歧义或旧会话未决不假报 idle。内部关联 robot/task/generation/turn；管理侧核验响应形状、session、当前 intent/连接身份与请求代次，迟到或范围不明返回 unavailable。task phase 是 Host TaskStore 状态，不是 EX Goal phase。管理 Bearer 不授用户取消权，页面 can_cancel=false。

普通聊天以只读 `decision.capabilities.get` 获取 bounded 声明动作，同一 LLM 选择直接回复/澄清/请求局部 `manage_astrbotex_task`；不新增 router、admin-only 或逐任务审批。Host 从可信 event/routes 提供身份，模型只传业务意图。create 要求 execution-ready 与规划 provider；本人已有任务 update/cancel/review 不因 disabled/provider 不可用而被禁止，但仍校验归属和会话。受理/公开回复回执与 buffered Host 支持边界见 [TECHNICAL](../TECHNICAL.md#host-工具与公开回复边界)。

低置信或 request_replan 在 dispatch 前拒绝整轮、保留原 choice/score，实际停止证明和持久化 draft 后经当前 Goal CAS 发布 failed 事实。currentGoal 只是一当前步骤，HostTask 是多步骤任务；只重规划未完成后缀，完成前缀不重放；取消不自动规划，unknown/timed_out 需 resume_review。

## 5. 异步、停止与错误处理

202 只表示受理，须查询 operation 终态；pending/running/succeeded/failed/blocked/superseded 不混用。匹配的 request_stop operation_id 与 stop.state=proven 才是框架停止事实；HTTP、cancel、连接关闭或 GUI 暂停不是证明。session/revision/gate/generation fencing 阻止迟到重新授权。

稳定 code 配合短 message，不回显供应商异常/凭据：400 修正输入；401 重填管理凭据（供应商 inference401 是独立失败）；403 检查 loopback/Origin；404 查当前记录；409 核对 session/revision/backend gate/归属/停止；429 等已有操作；500 保留脱敏存储诊断。不从错误文本猜已停，不自动重试写。

恢复须停止/旧请求结束、新版本、保持 disabled，不恢复活动 Goal、不回滚 Ledger、不清服务隔离；旧 PID 是审计信息，不能按端口或名称认领/扫杀。

## 6. 诊断证据与页面边界

历史区分快照、实际 body/hash、POST 尝试、本地 socket 写出、响应、模型选择、EX admitted/discarded 和 Ledger 状态。hash 对应完整字节，展示截断不改 hash；缺正文/响应不按快照重建成实调用。RequestHistory 最近 128 项、单项展示 64 KiB、分页最大 100，有截断/丢失/裁剪标记，非永久审计。SSE decision_changed 只通知关联，详情 GET 鉴权。

普通页面只含 Jev/Laya 卡片、连接弹窗、测试、EX 启停、当前任务和短状态；Mock 是诊断/测试后端。草稿独立于轮询，409 保留核对；session/credential/request 代次使旧响应失效，加载/刷新/网络恢复不执行写。外部文本安全渲染，停止不被脏草稿阻塞。浏览器入口见 [页面说明](B09_DECISION_UI_RESULT.md)。

## 7. 具名验证入口与限制

从仓库根运行（临时实例、无机器人）：

```sh
python -B -m unittest tests.test_decision_management_boundaries tests.test_decision_management_history tests.test_decision_management_http tests.test_decision_management_interleaving tests.test_decision_management_operations tests.test_owned_laya tests.test_decision_secret_redaction -v
python -B -m scripts.verify_decision_management --help
python -B -m scripts.verify_decision_management --output /absolute/fresh-external-evidence
```

生命周期/激活/真实 loopback/projection 专项为 `test_decision_management_lifecycle.py`、`test_decision_management_activation.py`、`test_decision_management_provider_http.py`、`test_decision_management_projection.py`。上述管理验证入口为 synthetic-only：真实管理 HTTP/RuntimeController、隔离软件 Actor 与合成 loopback owned 服务，不调用模型/ROS/机器人；不再接受旧模型环境的 `--python/--cache/--device` 参数。真实模型验证另用 [Laya 入口](B07-LAYA-BACKEND.md#3-独立部署与验证用法)及独立环境/固定缓存。测试 Actor 不授权硬件；合成供应商、FakeProcess、合同 fixture 分别不证明模型质量、实际进程或端到端集成。新日志/截图/JSON 只输出外部 evidence/临时目录。

> 验证范围：管理/页面检查分别覆盖配置 CAS、固定 wait-only probe、runtime/registry 停止证明与限定 Host routes 的任务投影；合同替身、合成 loopback 供应商及软件 Actor 不等于真实模型语义、硬件停车或长时稳定性验证。公开回复的去重范围仅限受支持的正常 buffered Host 路径。
