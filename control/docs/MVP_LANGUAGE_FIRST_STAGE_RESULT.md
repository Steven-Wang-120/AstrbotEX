# L1 / T1 第一阶段结果

日期：2026-10-04。范围：自然语言接口、原始 Laya 中文开发探针、合成标签集冻结，以及共用 EX 控制接线。未训练模型，未把本报告的模型选择成绩视为机器人成功率。

**L1 接口与 20 条真实开发探针已完成。当前 `typed-decisions` 在本次输入格式下仅答对 2/20，不能作为可用的中文任务策略。T1 已冻结 100 个 SYNTHETIC 任务/场景组，尚未训练；后续训练基座取决于模型比较决策。**

全部证据位于 L/T 本轮目录（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_l_t_20261004T162658`）。目录中的 `t1_corpus_v2` 是本次正式冻结版。`t1_corpus_final` 已因模型可见输入跨集合重复而失效，连同更早的 `t1_corpus` 原样保留，不作为训练输入。

## 1. L1 已实现的接口

[core/tasks/contracts.py](../../astrbot_ex/core/tasks/contracts.py) 是唯一技能 schema 来源。插件 manifest 由该文件生成。

| 项目 | 本次实现 |
|---|---|
| 技能 | owner=`mobile_manipulation`；`mobile_manipulation.move.v1`、`mobile_manipulation.fetch.v1` |
| move | 底盘移动至 `table_dock` 或 `home` 后停稳；位置容差 0.10/0.05 m，角度容差 10/5° |
| fetch | 停稳后夹取指定对象，放到 `tray_left` 或 `tray_right`；容差 0.020/0.010 m；朝向 free/yaw，要求 yaw 时为 5° |
| 对象 | 红、蓝、绿方块及直立圆柱；必须绑定已有 object_id |
| 成功模板 | `move.arrive_and_stop.v1`、`fetch.pick_place_stable.v1` |
| 目录 | 投影真实 EX CapabilitySnapshot，核对 action schema，保留 available、拒绝原因与原目录版本 |
| 会话 | TaskContext/TaskSelection、同会话澄清、取消、版本及输入 hash 检查、过期观测与迟到模型结果拒绝 |
| CLI | 可嵌入现有进程的 `run_cli`；不另建 EX runtime；缺放置位置先追问 |
| 模型入口 | TaskModelSession 使用单个 OwnedLayaService 的请求许可、取消与隔离机制；不切换 EX 后端 |

实现见 [session.py](../../astrbot_ex/core/tasks/session.py) 与 [laya_client.py](../../astrbot_ex/core/tasks/laya_client.py)。L1 产生检查后的任务提案，`execution_enabled=false`、`goal_submissions=0`。自然语言提案到正式 Goal 的 L3 接线仍属于后续阶段。

模型收到多项真实选项：两个 move 目标、观察到的不同 fetch 对象，以及 clarification/wait/unsupported/done。没有用“只保留唯一正确候选”替代模型选择。规则拒绝和澄清不计作模型答对。

用户确认的两种语序具有同一语义：`把红色方块放到左托盘` 与 `在左托盘上放红色方块`。颜色、形状和左右托盘替换沿用该规则。缺目标或同色多目标时，两种语序仍须澄清。新样例不使用“请”。

## 2. 原始模型的真实开发探针

运行入口：[probe_mobile_tasks.py](../../scripts/probe_mobile_tasks.py)。20 组输入在模型启动前冻结。最终 T1 v2 已排除这些开发任务的语义输入；旧 T1 v1 曾与其中部分输入重复，已失效。每组只调用一个预定决策点；未重试筛选成绩，未根据测试结果改写标签。此次 2/20 对应原请求文字。后续为显式对象绑定在候选文字中加入 object_id；新文字格式尚未再次跑模型。若比较 multilingual，须回放原始 state/questions 才能与 2/20 直接比较。

| 指标 | 实测结果 |
|---|---:|
| 完成请求 | 20/20 |
| HTTP / 返回结构验证 | 20/20 通过 |
| 模型语义选择正确 | **2/20（10%）** |
| 热请求 p50 / p95 / 最大值 | 21.17 / 25.52 / 26.01 ms |
| 冷加载至 ready | 271.02 s |
| 首次 warmup | 13.72 s |
| 冷启动总耗时 | 284.75 s |
| Goal / 运动命令 | 0 / 0 |
| 请求截断、输入丢弃、CPU fallback | 均未发生 |
| 子进程结束 | 已由 OwnedLayaService 确认退出 |

分类成绩：move 0/3，fetch 0/3，move→fetch 的首个决策 2/3，缺放置位置 0/3，同色歧义 0/3，未知技能 0/2，缺有效观测 0/3。

证据：[result.json](evidence/language/l1_real_probe/result.json)、[逐次真实请求与返回](evidence/language/l1_real_probe/calls.jsonl)、[冻结开发输入](evidence/language/l1_real_probe/frozen-probes.json)。服务日志保留在同目录的 `service/`。

本轮等待了同一模型进程的首次文件 I/O。未因冷启动慢而终止或重启。可信部署可单独配置较长冷加载和首次 warmup 预算；本次均为 1800 秒。热请求上限保持 300 ms，公共 LayaConfig 的限制没有放宽。

### 协议与模型适用性核对

实际请求解码后的 state 包含原生中文。20 次请求均使用官方 choice 结构；A–H 与候选 ID 的映射、argmax、概率和返回身份一致。实际输入为 167–180 tokens，没有截断。没有把 score、noul 或 `act_probability` 误作动作选择。

本地固定配置的 encoder 为 `answerdotai/ModernBERT-large`，当前 checkpoint 是 `typed-decisions`。其 ByteLevel BPE 能编码这两句中文，分别为 16/15 tokens，未知词数均为 0。因此不能将 2/20 解释为“中文无法编码”。准确结论是：**当前 checkpoint 与当前输入表达的组合表现不足**；本轮没有隔离语言、提示措辞及训练任务分布各自的影响。

模型版本：Laya 0.3.22；bundle revision `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`；本地权重 SHA256 `4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e`。

只读核对发现，同一固定 revision 已包含官方 `multilingual` 子目录：encoder 为 `jhu-clsp/mmBERT-base`，权重 643,835,514 字节，tokenizer 34,363,188 字节。它使用相同 choice API；后续可冻结它自己的 encoder 并训练自己的 head。两种 encoder 的 hidden size 不同，不能直接迁移旧 typed-decisions head。配置与文件清单来自[官方固定模型配置](https://huggingface.co/convaiinnovations/laya/blob/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/multilingual/rl_agent_config.json)及[固定文件树](https://huggingface.co/convaiinnovations/laya/tree/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/multilingual)。

本次仅获取小型配置和元数据；尚未下载多语言权重、切换模型或开始训练。只读核对证据见 laya_checkpoint_audit（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_l_t_20261004T162658/laya_checkpoint_audit`）。

## 3. T1 标签与划分冻结

用户审阅了 14 个代表性任务/场景组，并要求删除礼貌词、支持等价语序。随后确认采用**任务/场景隔离，允许语法复用**。本报告不宣称测试集检验了新语法泛化。原始规则和人工确认记录见[标签审阅文档](MVP_LANGUAGE_LABEL_REVIEW.md)。

| 项目 | 冻结值 |
|---|---:|
| 独立任务/场景组 | 100 |
| 训练 / 验证 / 测试组 | 70 / 15 / 15 |
| 决策点 | 120 |
| 含等价改写的导出行 | 230 |
| 数据来源 | 全部 SYNTHETIC |
| 模型训练 / 物理执行 | 均为 0 |

move、fetch、move→fetch 各 20 组；缺放置位置、同色歧义、未知技能、缺有效观测各 10 组。move→fetch 的前后两个决策点留在同一组。等价改写共享 gold、group 和 split。

最终审查发现 v1 的随机场景几何没有进入模型输入：验证集 35 行中 29 行、测试集 34 行中 28 行与训练输入重复；9 个已运行开发输入也出现在旧测试集中。全部 40 个 fetch 决策都指向 obj-a。这不能证明独立任务选择能力。旧文件、hash 和失效原因保留在 [v1 失效审计](evidence/language/t1_v1_invalidated_audit.json)，没有删除失败证据。

v2 沿用用户已确认的词汇、标签规则和两种语序，改变模型实际可见的对象角色、颜色/类别组合、目标区域、精度档及任务状态。它包含 120 个不同语义任务状态、230 个不同实际模型输入；跨组、跨集合和原开发任务的重复均为 0。40 个 fetch 标签分为 obj-a 22 个、obj-b 18 个。候选描述包含 object_id，两个同色对象在用户明确编号后也能区分。

隔离检查同时比较模型实际 state＋候选集合，以及忽略语序、选项排列、观测 ID 和几何随机数后的语义任务签名。没有通过新增 nonce 或无关随机几何伪装独立样本。对象 ID 固定为 obj-a/obj-b 两个绑定角色，并非每组新建 ID。审计见 [v2 隔离结果](evidence/language/t1_v2_isolation_audit.json)。原 14 组人工审阅作为代表性规则依据，不能表述为用户逐条审阅了这 100 个修订组。

生成入口：[build_mobile_language_corpus.py](../../scripts/build_mobile_language_corpus.py)。实际使用官方 Laya 0.3.22 的 `Dataset.from_jsonl` 成功读取全部 230 行；这一检查未导入 Torch、未加载模型。证据：[official-dataset-parse-v2.json](evidence/language/official-dataset-parse-v2.json)。

正式文件：[groups.jsonl](../data/t1_corpus_v2/groups.jsonl)、[laya-choice.jsonl](../data/t1_corpus_v2/laya-choice.jsonl)、[manifest.json](../data/t1_corpus_v2/manifest.json)。

- review SHA256：`9126f4770955ec9b919add04e31a7f0284743f2d10e7688e1811b1763ca870f0`
- corpus hash：`f1a3ba897196f590148cef3850b0cd3bfb1f184019c2a0e1b8d6f2e58ea97dc4`
- split hash：`7a76305fb0e09249593ee237e3c8e32ae4573919c6517846c213128898988506`
- export hash：`13ef56d8024898ad213aa6767d60d84b24076f3d7983894d47bc1caa05beca04`

### 与 P/E/M 实验种子的边界

T1 v2 的 100 组全部由语义组合生成，没有 `scene_seed` 字段，也没有读取 Isaac 采集或执行记录。合成几何 RNG 使用 `T1v2-scene + group_id` 的 hash，100 个派生值各不相同，与已知 P/开发种子 `101`、M1 开发布局 `7301/7302/7303` 没有数值交集。输入隔离仍以实际模型输入和语义任务签名为准，不能仅靠种子不同证明无泄漏。

E0 的五个布局和 M4 的最终 10 个可行场景、2 个失败场景尚未冻结。因此本轮不能声称已经比对了未来全部评估种子。后续冻结这些场景时须记录独立来源；若引入真实数据训练，继续排除这些评估场景及其连续帧、改写和反馈。当前 T1 只是限定语义任务数据，不含物理成功标签。

## 4. 共用 EX 接线与验证边界

[mobile_ex_runtime.py](../scripts/lib/mobile_ex_runtime.py) 在系统 Jazzy Python 3.12 内组合真实 EX Runtime、MockBackend、GoalManager、Dispatcher、Actor 和原生 ROS 端口，仅启用本次控制插件，不接 A.E.B. 或第二个模型服务。

[控制插件](../../plugins/control/mobile_manipulation/main.py) 的 start/cancel 回调只受理和转交。一个有界纯回调线程处理 recipe、观测与物理评价；Actor 持续处理取消及 ROS 反馈。所有通信使用 `context.ros`，没有私建 ROS executor。

宏命令与子阶段保留父子 command_id、EX session、Goal revision 和执行 epoch。controller_stage 成功不等于物理任务成功；只有独立物理判定能完成 fetch。阶段间可按 `source_stamp` 等待最多 2 秒仿真时间，等待期间仍能取消。普通取消使用常规 cancel publisher；环境失活使用受限停止 hook。

failed/timed_out/unknown 保持原账本终态。收到可信停止证据后，运行器经已有 `dispatcher.reconcile_stop` 释放资源；迟到证据不会改写失败为成功。取消后的旧规划结果被丢弃。

本次纯测试 **52/52 通过**，包含实际模型输入隔离、同色对象显式编号、同会话澄清、真实目录不可用、输入契约、时效与取消、Owner 服务隔离，以及控制通信与迟到停止证据。证据：[tests-final-v6.json](evidence/source_runs/phase1_l_t_20261004T162658/tests-final-v6.json)、[完整测试输出](evidence/source_runs/phase1_l_t_20261004T162658/tests-final-v6.stderr.txt)。

另已在 `scripts/mobile_manipulation_env.sh` 下实测原生 EX ROS 环境启动和关闭：runtime=running、environment=ros2/idle、health=ok，零 Goal、零运动。证据：[ex-native-check.json](evidence/source_runs/phase1_l_t_20261004T162658/ex-native-check.json)。这仅证明框架和原生端口可启动；真实 Goal→机械臂物理抓放由 A/R/E 的运行证据单独判定。

## 5. 后续决策

待用户确认：是否在独立缓存中获取上述固定 multilingual checkpoint，使用相同的 20 条开发探针比较，再确定 T2 微调基座。比较不会覆盖 B07 权重、改变共享 Torch 或自动开始训练；仍与 Isaac GUI 分时使用 GPU。

本轮没有 commit 或 push。机器人实验、独立测试集成绩及微调收益均未在此报告中预写为通过。

## 6. 最终证据索引与未完成项

| 证据 | 当前用途 |
|---|---|
| `l1_real_probe/` | 原始 checkpoint 的 20 次真实请求；2/20，零 Goal、零运动；原输入保留供后续比较 |
| `t1_corpus_v2/`、`t1_v2_isolation_audit.json`、`official-dataset-parse-v2.json` | 唯一有效 T1 冻结数据、隔离检查和官方读取结果 |
| `t1_corpus_final/`、`t1_corpus/`、`t1_v1_invalidated_audit.json` | 保留的旧数据及失败审计，不得作为独立训练/评估输入 |
| `tests-final-v6.json`、`ex-native-check.json` | 52 项 L/T/插件纯测试及零运动原生 ROS 启停证据 |
| `trial-evidence-regression-v2.json` | 后续 13 项物理证据边界纯测试；旧 12 项记录保留，不能累计为额外验收样本 |

最后一次只读核对确认：上述文件存在，正式 manifest 中 corpus/split/export/review 的 hash 与本报告一致。冻结的[标签审阅文档](MVP_LANGUAGE_LABEL_REVIEW.md)保留原样；其中 14 个小样是标签规则审阅依据，不是 v2 的逐项人工验收，也不应沿用旧几何 hash 作为独立性证明。

L1/T1 没有尚需实施的独立无运动工作。待决定的是 multilingual 比较和后续 T2 基座；下载、模型复测、训练与 L3 自然语言运动接线均未执行。A/R/E/M 的真实运动结果仍由各自证据判定，不能由本报告的接口或纯测试成绩替代。
