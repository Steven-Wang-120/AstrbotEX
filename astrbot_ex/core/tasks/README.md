# L1 任务层接口

任务层位于正式 Goal 之前。L1 返回已检查的提议，不提交 Goal。EX 继续使用 Mock；Laya 不获得执行权限。

- `contracts.action_manifest()` 是 `mobile_manipulation.move.v1/fetch.v1` 的唯一动作声明来源。插件与目录均引用此函数。
- `TaskSession.feed(text, observation)` 更新同一会话版本，返回上下文及澄清状态。`accept()` 核对选择与最新版本，返回参数提议或拒绝原因。
- `TaskModelSession(existing_owned_service, execution_idle=...)` 复用 B08 的服务所有者。恢复前必须由可信调用方确认执行端已停止。它不切换 EX 后端。
- `run_cli(session, observation_provider, selector)` 是嵌入现有 EX 进程的输入通道。它不构造第二个 EX runtime。
- L1 不包含 L3 的正式 Goal 提交、L4 的完整模式对比或 L5 的机器人执行。

观测摘要包含 `observation_id/valid/age_ms/max_age_ms/received_monotonic_ns/at_table/stationary/objects`。对象含 `id/color/shape`。缺少有效观测时返回等待。示例中的合成观测必须标为 SYNTHETIC，不充当 P 的传感器结果。

结构化模式给模型任务文本、有限技能说明及观测摘要。文本模式不传对象观测，仅选择一般技能；之后的绑定仍检查真实观测。单次至多8个选项，最多1024 tokens；预算不足直接拒绝，不静默截断。

任务ID、选项集合hash、输入hash及服务代次保留在本地记录。此时没有 `goal_id/command_id`。正式提交映射由后续 L3 增加。

`TaskLayaClient`复用锁定版本B07的健康检查、HTTP、超时和隔离代码，仅覆盖任务请求/响应映射。它依赖内部方法；升级B07实现时运行`tests.test_mobile_tasks`检查兼容性。

## 开发探针

在AstrBotEX目录，使用EX已有Python运行`PYTHONPATH=. .venv/bin/python scripts/probe_mobile_tasks.py --run-real --output <新目录> --laya-python <独立Laya环境>/bin/python --cache <外部模型缓存目录>`。

入口固定20组中文开发输入，再启动一个自有服务。原权重为typed-decisions，冷加载预算1800秒，热请求300毫秒。输出包含真实选择、可信绑定/拒绝、请求与响应、模型身份和进程退出证明。模型错误不改成规则成功，不自动重跑。

该入口不创建EX调度器，不驱动机器人。它单独声明Mock调度目标，但没有正式Goal/Actor调用。它不能证明EX运行中任务入口或机器人闭环已完成。

## T1 数据

用户于2026-10-04确认：移除样例礼貌前缀；“把红色方块放到左托盘”和“在左托盘上放红色方块”等价。两语序在同组共享标签。

100组按任务/场景隔离为70/15/15，允许跨独立场景复用语法，不宣称未见语法泛化。每组保存独立合成场景及hash；同组全部改写与连续决策点不能拆分。数据标签来自固定语义规则和14组人工审阅，不来自模型输出。

`build_mobile_language_corpus.py`校验审阅文件hash后导出`groups.jsonl`及`laya-choice.jsonl`。后者使用`state/questions/gold`，并带官方评估器的`expected`字段；标签与分组元数据不进入state。导出选项顺序按组固定打乱，避免把常量位置当选择能力。


完整仿真工程、冻结数据及阶段限制见 [control/README.md](../../../control/README.md)。
