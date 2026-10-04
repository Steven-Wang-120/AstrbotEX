> 历史报告：迁自 AstrEX 原仓库，保留当时日期、路径和验收结论。当前集成状态见 [集成报告](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

# B08 决策管理 API 与 Laya 管理闭环结果

实施时间：2026-10-01 至 2026-10-02（北京时间）。本轮完成 B08；不 commit、不 push，原暂存区保持不变。

## 1. 结论与范围

配置保存、统一鉴权、自有 Laya 启动与预热、独立健康探测、可信后端应用、请求历史、动作查询、停止和显式恢复均已接通。一次真实 HTTP 管理闭环通过；使用已有离线权重和隔离测试 Actor，没有接入机器人。

| 验证 | 本机结果 | 证据 |
|---|---|---|
| 受影响模块回归 | 199/199 PASS，20.906 秒 | [命令与源码身份](evidence/b08_20261001/affected-tests-01.json)、[原始输出](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/b08_20261001/affected-tests-01.txt) |
| 最后补充的输入、通知和记录预算边界 | 7/7 PASS，0.415 秒 | [原始输出](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/b08_20261001/boundaries-tests-01.txt) |
| 含引号、反斜杠、中文的密钥脱敏 | 1/1 PASS，0.005 秒 | [原始输出](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/b08_20261001/secret-redaction-tests-01.txt) |
| 已安装 Chrome 的实际页面检查 | 7/7 PASS | [浏览器请求与结果](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/b08_20261001/browser-run-01/result.json) |
| 一次真实 Laya HTTP 管理闭环 | PASS，14.382 秒，测试 Actor 两条命令 | [执行命令](evidence/b08_20261001/real-run-01-command.json)、[完整结果](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/b08_20261001/real-run-01/result.json)、[证据核对](evidence/b08_20261001/real-run-01-summary.json) |

199 项运行后，只修改了重复空查询字段检查、密钥 JSON 转义脱敏和 EventBus 通知白名单。这些修改由后续 7 项及 1 项针对性测试覆盖，真实模型运行使用最终源码。没有为筛选通过结果重复模型实验，也没有扩展到旧 B02 压力、CartPole 或 ROS 验收。

普通装配仍禁止 Laya execute，Jev execute 限制不变。测试执行能力只能由可信代码在临时目录、无机器人插件的装配中授予，不是可保存或恢复的 HTTP 配置。原模型 replan 场景的 **0/8** 失败结果保留；本轮通过不代表模型任务质量通过。

## 2. 实际改动

| 范围 | 改动与用途 |
|---|---|
| `core/decision/config.py` | saved/effective、CAS、原子保存；管理凭据与 Jev 密钥分离；set/keep/clear；秘密不进入 profiles |
| `core/decision/management.py` | 复用现有 HTTP 的管理路由；有界异步操作；可信模式应用；停止优先撤授权；恢复及备份回调 |
| `core/decision/owned_laya.py` | 从原验证脚本提取自有服务管理；单服务代次、离线加载、固定预热、退出证明、跨实例隔离 |
| `core/decision/history.py` | 在真实调用边界关联快照、实际输入、后端输出、EX 结果和 command ID；有界队列和内存记录 |
| `decision/service.py` | 可选队列追踪；受控停止/模式/复核方法；后端替换增加可选版本门禁，防止旧切换覆盖新停止 |
| `decision/backends/laya.py` | 可选队列进度追踪；保留原提示、协议、选择、预算和执行限制 |
| `core/api_server.py` | 所有 `/api/` 和旧别名统一鉴权；管理路由、SSE 脱敏、配置恢复边界、循环关闭 |
| `core/event_bus.py` | 仅将 `decision_changed` 加入现有通知白名单；不广播完整模型输入 |
| `dashboard/app.js`、`index.html`、`styles.css` | 最小凭据输入、认证 fetch/SSE、上传和下载；保留原导航和草稿；没有开发 B09 |
| 现有测试与验证入口 | 旧 HTTP fixture 增加认证；原业务断言保留。B07 脚本复用提取的进程模块；新增一个 B08 真实闭环入口 |

没有修改 Dispatcher、Ledger、Actor 核心、冻结 Goal/Action 协议、Jev 后端实现、connection_manager、ROS/Isaac、模型提示或选择策略。未重新安装环境或下载模型。

接口、完整错误码、请求样例及 B09 交接见 [B08 API 说明](../apps/AstrBotEX/docs/B08-DECISION-MANAGEMENT.md)。[任务书](AstrEX_workingTree/B08_高风险_决策管理API与密钥保护.md)保留需求和验收条件；[施工索引](AstrEX_workingTree/00_施工总索引与派工规则.md)区分 B08 完成与 B09 待开发。

## 3. 配置、鉴权与恢复

- 配置位于 `profiles/default/decision.json`，供应商密钥只保存 `secret_ref`。管理凭据和密钥位于 `data_root/secrets`，目录 0700、文件 0600，备份 roots 不包含该目录。
- 响应区分保存版本、实际应用版本、EX 会话和框架配置版本。写请求必须匹配会话与版本；停止可接受当前会话的旧配置版本。
- 保存不会启动进程、推理、runtime、模式或 Goal。后端应用通过可信 `replace_backend()`，不从 HTTP 接受工厂、导入路径或执行权限。
- 所有 API，包括旧状态、事件、runtime、环境、插件、连接、动作、备份和恢复入口，均要求管理鉴权。Host/Origin 受限，敏感响应 no-store，无通配 CORS。
- 浏览器凭据只保存在页面内存；刷新后清除，SSE 使用认证 fetch 流。页面打开、重连和输入凭据没有执行写请求。
- 配置恢复先要求停止证明和旧请求结束；恢复后建立新配置版本并保持 disabled，不恢复活动 Goal、不重放命令、不回滚 Ledger 或清除服务隔离。
- 活动操作最多 8 个，保留最近 128 个已完成操作。更新的停止、配置和上下文使旧操作 superseded，不能迟到重新授权。

**凭据验收范围按用户确认限定为新管理凭据和 Jev 密钥。** 旧 WebSocket token 仍在旧连接配置中；受鉴权的配置 GET 和 ZIP 仍可能包含它。未做旧 token 脱敏或迁移，不宣称全系统凭据保护完成。现有依据是 `connection_manager.py` 的配置投影/保存，以及 `backup.py` 的 `profiles/plugins` roots；这些模块未改。

## 4. 真实模型管理闭环

沿用 `runtime/laya/.venv`、Laya 0.3.22、`typed-decisions`、权重 revision `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` 和原缓存。两个服务代次的健康信息均确认 checkpoint 位于 CUDA，CPU fallback count 为 0。已有模型身份和环境依据见 [B07 报告](B07_LAYA_EX_BACKEND_RESULT.md)。

本次按顺序完成：

1. 缺失凭据返回 401；保存 Laya 配置后没有服务、Goal 或执行副作用。
2. 显式 `/service/start` 创建自有进程，完成 GET health 与固定预热；仍为 disabled。`/test` 仅 GET health，没有模型推理。
3. 显式 shadow 后提交正式 Goal；真实模型选择 start，Actor 和 Ledger 命令数均为 0。
4. 显式测试 execute 后提交新 Goal；真实 Laya 经 EX 和 Dispatcher 下发一条测试命令，Ledger 依次保存 admitted、accepted、running、succeeded。
5. 新 Goal 的请求写入本地 socket 后，对保存的自有 Popen 句柄发送 SIGSTOP。原 1500 ms deadline 不变，约 1503.18 ms 后记录 deadline_exceeded、未收到响应、零下发，并锁存 restart_required。
6. 显式 stop 取得匹配的 proven 回执。保存相同配置后再启用返回 409 restart_required；新建客户端同样在提交推理前被服务代次门禁拒绝。
7. 显式 recover 取得停止/复核回执，终止旧进程并确认退出，等待旧请求结束，再启动和预热新代次；可信替换后端后仍为 disabled，旧 Goal 没有重放。
8. 新模式授权和新 Goal 完成第二条测试命令；随后显式停止服务和关闭 HTTP。两代进程均已退出，端口 8769 已关闭。

| 实际记录 | 结果 |
|---|---|
| POST 提交尝试 | 6 次：2 次固定预热、4 次正式 EX 请求 |
| 服务日志中的成功 POST | 5 次 200；故障请求只有本地写出证据 |
| 正式请求结果 | 1 次 shadow、2 次 admitted、1 次超时 discarded |
| 测试账本 | 2 条 command、8 条按序事件；两条终态均 succeeded |
| 旧进程 | PID 210698，挂起后 TERM 等待结束，KILL，退出码 -9，退出已确认 |
| 新进程 | PID 211197，新代次预热通过；最终退出码 -15，退出已确认 |
| 清理核对 | 无本任务模型 PID，回环端口关闭；[原始核对](evidence/b08_20261001/owned-process-cleanup.json) |

故障通过真实 transport 的写入回调注入。适配器将该测试 transport 的 `post_written_to_socket` 保留为 null，没有伪造为 true；独立故障记录保存实际写入时间和 hash。四个正式请求的 body 相同，hash 不能唯一识别一次调用；用 request/snapshot ID、服务代次和时间窗口共同关联。

本地 socket 写完不能证明服务端已收到或 GPU 推理完成。实际命令中的成功、进度和反馈均来自测试 Actor。真实演练的 stop 发生在动作已经成功或无活动动作时，未产生运行中动作的逐命令 StopEvidence；该分支由确定性测试覆盖，不能写成机器人物理停止证明。

两个新进程启动至健康就绪分别为 3945.88/3952.02 ms，固定预热为 346.86/350.30 ms。三个成功正式调用的后端 elapsed 为 24.38/27.24/28.24 ms；两次快照至 Actor 回调受理为 41.14/40.48 ms。这里只保存附带样本，不估算 p95、不推广至多 owner、大输入、ROS 或 Isaac GUI。故障样本单独保留。

官方服务日志仍有 `choice:11+` 温度被钳制的警告；本次仅使用三个选项。没有调整模型参数，也不宣称概率已全局校准。

## 5. 测试覆盖与失败保留

确定性测试覆盖配置类型/预算、CAS 和保存失败、密钥 set/keep/clear、鉴权旧别名、Host/Origin、ZIP 和 SSE、不启动执行的探测、操作上限与裁剪、加载中停止、重复启动、恢复失效、进程所有权/端口占用/退出失败、跨实例隔离、旧请求串单、迟到结果拒绝、实际 hash、展示截断，以及普通装配无 execute。

本轮早期的失败输出全部保留于[证据目录](evidence/b08_20261001/)：

| 记录 | 原因和处理 |
|---|---|
| `existing-http-tests-01.txt` | 命令缺少项目 PYTHONPATH，导入失败；仅修正运行环境 |
| `http-tests-01.txt` | 环境代理拦截测试的恶意 Host，fixture 改用无代理 opener；403 业务断言不变 |
| `history-tests-01.txt` | 旧记录不匹配标记被覆盖；修正合并标记，不附会旧请求 |
| `management_operations_tests.txt` | 按创建时间裁剪，刚完成的长期操作丢失；改按完成时间保留最近 128 项 |
| `interleaving-tests-01.txt` | 旧切换覆盖新停止；可信替换入口增加版本门禁。另修正 fixture 的 v2 manifest 构造 |
| `legacy-http-01.txt` | 新恢复钩子改变旧业务错误文本；恢复原 SnapshotError 表达，原断言保留。该失败还阻断 fixture 清理，产生 teardown 错误 |

上述修正进入 199 项回归。没有修改模型提示、增加 sleep、放宽业务断言或正式决策 deadline。沙箱不允许创建回环 socket 的初始检查，经授权在宿主环境执行；这不是功能失败。验证入口一次缺少 PYTHONPATH 的 `--help` 检查在导入前退出，未产生模型调用。

最终输入、脱敏和 EventBus 边界补充测试通过；源码 manifest 区分 199 项运行时与真实模型运行时的身份。没有把中间版本的测试结果冒充同一轮最终全量回归。

## 6. 请求追踪与 B09 交接

Laya 记录原快照、实际请求和响应、完整输入 hash、短选项映射、原始概率与归一化记录、EX 受理结果和 command ID。Jev 仅复用现有诊断中的 hash/耗时/错误；没有新增 Jev body/响应捕获，也没有云端调用。缺项返回 null 或明确缺失，不重建为实际请求。

请求历史保留内存中的最近 128 条，队列 256；展示单条最多 64 KiB，分页最多 100。超预算有截断/原始大小标记，hash 始终基于完整实际字节。队列丢失、历史裁剪和事件窗口不足如实暴露，不宣称任意流量下永久完整保存。没有把原任务和观测自动持久化为训练数据。

B09 可以直接使用当前路由、版本字段、operation 状态和鉴权 fetch。页面必须区分 saved/effective、构建/实际调用、模型选择/EX 受理、Actor accepted/running/succeeded、请求停止/proven，以及测试结果/物理结果。当前旧页面只有认证兼容，没有一级决策页面。

## 7. 工作区保护与剩余范围

[保护审计](evidence/b08_20261001/protection-audit.json)保存开始/结束哈希、授权修改和新增文件清单。Git index 字节、cached diff、remote 和 submodule 状态均与开始一致。ROS/并行任务位于被记录的源码范围内，没有修改它们。dashboard 的已有未提交改动是在其现有内容上增量修改；初始哈希清单未覆盖 dashboard，因此不对它声称完整的前后哈希证明。

原 B04/B07 已保留的 Ledger.admit/关闭超时、legacy worker/tick 竞争、B02 压力 Future 超时仍开放。本轮没有重跑压力验收或修改这些组件，定向测试与真实管理流程未被它们阻断；不能据此宣称这些根因消失。

未实施：B09、A.E.B. 新 Goal/反馈接线、控制端轨迹评分、Laya 微调、ROS/Isaac 实际运动、抓放及 GUI 争用验收。没有新付费请求、自动重启、旧 Goal 重放或自动进入下一阶段。

**本轮需要人工决策的阻塞：无。** 后续单独安排旧 WebSocket token 迁移、模型 replan 质量改进与原框架问题定位。日常使用时，首次输入本机管理凭据；若重启后服务进程归属不明，先人工确认处理，不能凭旧 PID 自动认领。

## 8. 复用入口

已执行的真实管理验证命令如下，不要求重复运行：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
PYTHONPATH=.:tests PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  scripts/verify_decision_management.py \
  --output /tmp/astrex_b08_20261001/real-run-01 --port 8769 --device cuda
```

入口要求新的空输出目录，只注册隔离测试 Actor，最终清理自己创建的服务。B07 原验证入口复用同一进程管理模块；本轮未重跑 B07 的 24 个热样本。

[证据 SHA256 清单](evidence/b08_20261001/SHA256SUMS.json)包含测试、浏览器、真实模型日志和源码身份。证据包不包含管理凭据、供应商密钥、临时 instance、用户暂存区内容、权重或虚拟环境。


> 2026-10-02 归档补充：原始日志和完整响应已转入共享数据目录，相关证据链接已更新。文中的 `/tmp` 命令路径记录当时的实验环境，不保证临时目录仍存在。参见 [B09 前归档清单](evidence/PRE_B09_ARCHIVE_MANIFEST.json)。
