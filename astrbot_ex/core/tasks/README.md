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
