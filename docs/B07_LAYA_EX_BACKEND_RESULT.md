> 历史报告：迁自 AstrEX 原仓库，保留当时日期、路径和验收结论。当前集成状态见 [集成报告](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

# B07 Laya EX 后端接入与真实验证结果

日期：2026-10-01。本轮完成 **Laya 适配器、真实权重调用及 EX → 测试 Actor → Ledger 链路**。任务选择效果未全部通过：原模型把重新规划误选为移动。接口接通与模型效果分别验收。

本轮不接 ROS/Isaac、不微调、不开发 B08，不 commit/push，不修改 Git index。前轮 B04 的停止回执、可信后端切换直接复用。

## 1. 改动与边界

| 文件 | 改动 |
|---|---|
| `apps/AstrBotEX/astrbot_ex/core/decision/backends/laya.py` | 新增 stdlib 适配器；固定模型身份、预算、映射、响应校验、单在途、deadline、取消、关闭、诊断记录 |
| `backends/registry.py`、`backends/__init__.py` | 静态注册与导出 Laya；保留 Mock/Jev |
| `tests/test_laya_backend.py` | 18 项确定性协议测试 |
| `tests/test_laya_service_integration.py` | 7 项正式 EX/Actor/Ledger 测试，模型响应为受控 fixture |
| `tests/test_decision_backend_contract.py` | 目录预期增加 laya，原安全断言保留 |
| `scripts/verify_laya_backend.py` | 一个显式真实验证入口；只启动、关闭本任务拥有的官方推理子进程 |
| `apps/AstrBotEX/requirements-laya.lock` | 成功环境的精确依赖版本；固定官方源码归档及 SHA256 |
| `apps/AstrBotEX/docs/B07-LAYA-BACKEND.md` | 独立环境、启动、恢复、配置与 B08 Python 接口交接 |

未改 DecisionService、Dispatcher、Ledger、PluginActor、runtime、冻结协议、Jev 限制及默认应用启动行为。默认 Laya 不联网、不加载模型、不允许执行；仅测试装配显式传入 `allow_test_execution=True`。

保护核对记录见 [protection-audit.json](evidence/laya_b07_20261001/protection-audit.json)。开始时记录了 200 个相关已有文件；结束时只有上述三个授权的已有注册/目录测试文件改变，其余 197 个哈希保持一致。Git index 字节一致，原暂存内容保留。另有新文件与独立虚拟环境，均未暂存。

## 2. 固定环境与模型

| 项目 | 实际身份 |
|---|---|
| Laya 官方代码 | `0.3.22` / `6d942c92081fbc139e736bbd9ac0023223c29b7f` |
| 请求别名 | `typed-decisions`；不是猜测的 `laya-typed-decisions` |
| 权重 | `convaiinnovations/laya`，子目录 `typed-decisions` |
| 权重 revision | `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` |
| 权重 SHA256 | `4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e` |
| 独立 Python | `runtime/laya/.venv`，Python 3.12.3 |
| 核心依赖 | torch `2.8.0+cu128`、transformers `4.57.3`、tokenizers `0.22.2` |
| EX Python | 原有 `apps/AstrBotEX/.venv`，未增加 torch/transformers/Laya |
| GPU | NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU，约 8 GB；CUDA 运算检查通过 |
| 模型缓存 | `/data/shared/AstrEX_project_data/models/pretrained/laya/hub` |
| 推理服务 | 官方 `python -m laya.serve`；`127.0.0.1:8769`；只加载一个 checkpoint，单并发 |

只下载 5 个模型文件，共 846,195,716 字节；下载耗时 126.19 秒。模型文件、缓存和虚拟环境不进入 Git。GPU 与 checkpoint 设备字段为 cuda，已记录的 CPU fallback count 为 0。热样本后及每次正式决策前均核对健康信息；本轮没有 Isaac GUI 并发工况。

初次 PyTorch 安装因依赖下载读取超时退出。经用户确认，保持相同版本，仅延长安装下载等待，第二次安装成功。决策 deadline 未放宽。首次回归命令误用了主仓库不存在的 `.venv`，退出 127；修正为 EX 已有 `.venv` 后完成定向回归。两次原始输出均保留。

完整身份见 [environment.json](evidence/laya_b07_20261001/environment.json)、[weights-manifest.json](evidence/laya_b07_20261001/weights-manifest.json) 与 [依赖锁定](../apps/AstrBotEX/requirements-laya.lock)。固定源码归档哈希见 `source-install-manifest.json`。

## 3. 确定性测试：通过

执行一次受影响回归，139/139 PASS，8.724 秒：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.test_laya_backend tests.test_laya_service_integration \
  tests.test_decision_backend_contract tests.test_jev_backend \
  tests.test_decision_service tests.test_decision_runtime_integration \
  tests.test_snapshot_contracts -v
```

其中新增 25 项覆盖：合法选择、owner/候选映射、防御复制、非法分布/身份/JSON、输入预算、超时、取消、关闭、单在途、默认无副作用，以及正式服务受理/反馈/成功、shadow、StopEvidence、Goal/config 变化后的迟到结果零下发、服务不可用零动作、Jev 执行限制不变。

POST 后不确定会锁存 `restart_required`，即使迟到 worker 返回也不能再提交推理。探测不能解除锁存。关闭客户端连接不能证明 GPU 停止。确定性故障注入验证这些分支，没有反复加载模型制造故障。

原始日志：[affected-tests-02.txt](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/laya_b07_20261001/affected-tests-02.txt)。没有重复旧 1000 次压力、CartPole 或 ROS 验收。

## 4. 真实权重选择：协议通过，任务效果未全部通过

仅运行一次真实实验。场景在首次推理前固定，提示和选项未在看到结果后调整。共 31 次 POST：1 次预热、24 个冻结热样本、1 次正式 shadow、5 次正式 execute。

| 冻结目标 | 预期 | 实际选择 | 符合预期 |
|---|---|---|---:|
| `Move safely.` | start | start | 8/8 |
| `Wait without moving until a new instruction arrives.` | wait | wait | 8/8 |
| `Ask AEB for new goal parameters before moving.` | request_replan | start | 0/8 |

24/24 返回均通过协议、身份及冻结 EX 契约校验，调用失败 0；语义符合预期 16/24。重复次数反映固定输入的稳定结果，不是机器人任务泛化准确率。

重新规划首个输出中，start 概率 0.5456，request_replan 概率 0.2938。适配器严格返回模型选择，没有硬编码 start、选第一项、改标签或规则回退。这个场景只运行离线快照推理，没有派给 Actor。

`selection_quality_pass=False`。退出码 0 仅表示接口正常且真实模型至少一次驱动测试 Actor，不能解释成全部任务效果通过。当前结果支持 B08 接入与观测，不构成模型已可靠理解机器人任务的证据。

### 输入预算

使用固定 tokenizer 和官方静态输入构造核对 31 份实际请求，去重后为 4 种输入：最大 154 tokens，最大保守 ASCII 上界 574，均低于 1024。逐 token 比较证明问题头、选项和 state 完整，选项没有折叠，state 丢失为 0。

核对不实例化模型、不进行第二轮推理。见 [input-budget-check.json](evidence/laya_b07_20261001/input-budget-check.json)。当前样本只有一个 owner 和很短的状态；大观测或多 owner 容量仍以明确预算拒绝为准。

## 5. 正式 EX → 测试 Actor → Ledger：通过

装配链为：

```text
正式 Goal → GoalManager → DecisionService → LayaBackend
          → ActionDispatcher → 测试 PluginActor → 临时 SQLite Ledger
```

通过 `replace_backend("laya", trusted_factory)` 从 disabled Mock 切入；没有直接赋值 service.backend。EX 保留默认 5 Hz 调度。

- 真实 shadow 选择 start，Actor 命令数 0，账本动作数 0。
- 5 个新正式 Goal 均实际选择 start，Actor 接受 `arm.move.v1`、固定参数 `meters=1`，账本终态均 succeeded。
- 每次停止回执的 operation_id 与最终 `state=proven` 匹配。
- callback、RUNNING 和 SUCCEEDED 都来自测试 Actor 装配；不是机械臂反馈或物理成功判定。

可追踪示例：command_id `208e0dd2362e468584f70cf41559177f`，decision_id/snapshot_id `a3c9977b8f2d47d7b2fd9986f32fc3db`。`result.json → integration.cases[1]` 保留快照、实际请求/响应、选项映射、返回决定、正式 Command、账本 canonical_command 及 succeeded 投影。

真实运行保存了每条命令的最终投影和 event_seq=4，没有单独导出 accepted/running 的原始中间事件列表。确定性正式集成测试逐项验证这些状态转换。真实运行的停止查询发生在动作成功后；运行中取消及 StopEvidence 由确定性正式集成测试验证。

模型服务子进程 PID 196460 由本任务创建，实验后观察到退出码 -15，退出已确认。关闭过程只处理该 Popen 子进程，没有终止其他任务服务。没有发生自动重启或原请求重放。

## 6. 性能：本地短样本达到热调用目标

分位数使用 nearest-rank，失败另列。首轮服务冷启动/预加载至健康就绪 29,318.46 ms，首次调用/预热 387.52 ms。模型下载和环境安装均不计入。

| 测量边界 | N | p50 ms | p95 ms | 最大 ms |
|---|---:|---:|---:|---:|
| 24 个固定热样本，整个 decide 外层调用 | 24 | 35.75 | 39.09 | 40.21 |
| 上述 POST HTTP | 24 | 33.12 | 36.50 | 37.62 |
| 官方 inference timing header | 24 | 32.23 | 35.58 | 36.75 |
| 全部热请求，后端记录开始至完成 | 30 | 35.32 | 39.18 | 39.71 |
| EX 快照就绪 → Actor 回调返回 accepted | 5 | 43.66 | 44.85 | 44.85 |
| Goal 提交 → 快照就绪 | 5 | 155.12 | 164.47 | 164.47 |
| 模型返回 → Actor 回调返回 accepted | 5 | 4.82 | 6.26 | 6.26 |
| Goal 提交 → Actor 回调返回 accepted | 5 | 199.12 | 203.27 | 203.27 |

热调用 p95≤100 ms 在本机短输入下达到；不含冷启动，不推广到更大输入、Isaac GUI 争用或真实 ROS 控制。Goal 到 Actor 的约 200 ms 还包含原有调度等待。Actor 时间终点是测试回调受理，不是 SQLite 持久化 ACK 或机器人运动。

汇总见 [verification-summary.json](evidence/laya_b07_20261001/verification-summary.json)，完整样本见 [calls.jsonl](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/laya_b07_20261001/real-run-01/calls.jsonl) 与 [result.json](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/evidence/laya_b07_20261001/real-run-01/result.json)。

## 7. B08 交接与待处理事项

B08 可复用 `LayaConfig`、静态工厂、`status()`、独立 GET `probe()`、固定 `.code` 错误、`cancel/close` 和防御复制的 `last_record`。详细字段与恢复顺序见 [B07-LAYA-BACKEND.md](../apps/AstrBotEX/docs/B07-LAYA-BACKEND.md)。

仍需由 B08 实现：配置 CAS/存储、管理鉴权、异步操作表、实际请求历史、最终 EX 受理与 Ledger 关联。后端 `last_record` 只是最新一次完成记录，不是这些服务的替代品。B08 普通表单不能授予 test execution。

旧 B08 任务书仍有“Laya 不是 EX 后端”“可信切换待补”的旧说明。下一批派工应按本轮及前轮 B04 交接改目录说明，复用已实现 replace_backend 和公开停止回执。本轮没有改旧任务书或实现 B08。

恢复要求已经由用户确认：POST 发出后超时、取消或响应状态不确定，锁存 restart_required；确认本任务拥有的旧服务退出，启动并预热新服务，创建新实例，经可信切换和新的 Goal 授权恢复，不自动重放。不能只关连接、探测健康或构造新客户端。

原模型重新规划效果是后续任务的明确缺口。修正输入或小规模微调需另批验证；本轮没有改提示筛选成绩，也没有授权真实机器人。

前轮已知框架问题仍未解决：Ledger.admit/tearDown 超时、legacy worker/tick 多一次竞争、B02 压力序列 reconcile_stop Future 超时。原始日志保留于 `evidence/laya_b07_20261001/upstream-handoff/`。本轮定向 139 项与正常真实链路未被其阻断；不能据此宣称旧问题已修复，根因仍交上游定位。

本轮完成范围没有待用户决策的阻塞。未验证项：真实 ROS/Isaac 运动、机械臂抓放、GUI 并行显存、模型微调效果，以及模型进程强制挂起后的实进程恢复演练。故障协议通过注入测试验证。

## 8. 复用入口与原始证据

真实执行命令：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/verify_laya_backend.py \
  --run-real \
  --laya-python /home/sssxy/Projects/AstrEX_project_main/runtime/laya/.venv/bin/python \
  --model-cache /data/shared/AstrEX_project_data/models/pretrained/laya/hub \
  --output /tmp/astrex_laya_b07_20261001/real-run-01 \
  --device cuda --repetitions 8
```

重跑时使用新的空 output；入口拒绝覆盖已有结果。上面是本轮已执行命令，不要求再次复跑。

[完整证据目录](evidence/laya_b07_20261001/)包含原始安装/测试日志、环境/模型 manifest、冻结场景、全部真实响应与失败结果、Actor 关联、输入预算核对、工作区保护审计及文件 SHA256 manifest。暂存区快照与其他用户改动没有导出到证据包。

官方依据：[固定源码](https://github.com/NandhaKishorM/laya/tree/6d942c92081fbc139e736bbd9ac0023223c29b7f)、[固定模型配置](https://huggingface.co/convaiinnovations/laya/blob/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/typed-decisions/rl_agent_config.json)。实际成绩来自本机原始记录。


> 2026-10-02 归档补充：原始日志和完整响应已转入共享数据目录，相关证据链接已更新。文中的 `/tmp` 命令路径记录当时的实验环境，不保证临时目录仍存在。参见 [B09 前归档清单](evidence/PRE_B09_ARCHIVE_MANIFEST.json)。
