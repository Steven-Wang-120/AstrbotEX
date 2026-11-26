> 下文评测门槛是设计目标，不是运行结果。固定 Jev `jev-1.13.0`、schema v2 连接、live HTTP gate 与 wait-only probe 见 [后端说明](../B07-LAYA-BACKEND.md)；管理和验证见 [B08](../B08-DECISION-MANAGEMENT.md)、[复验入口](../LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。低置信保持原 choice/score、dispatch 前拒绝，经实际停止/持久化退役反馈重新规划未完成后缀，不重放完成前缀。

# B07｜高风险｜Jev后端接入与决策评测

作用：通过模型无关接口接入Jev，并验证它在本项目的调度场景是否有价值。预计3–5人日。依赖B04/B01。Jev不是插件，不进入PluginManager。

## 1. 变更地图与资料

新增 `core/decision/backends/jev.py`；修改后端静态注册与依赖锁定，不改插件业务接口。供应商API/SDK只在此适配器出现。

核实日期2026-09-25：模型可固定`jev-1.13.0`；文本/JSON输入、英文优先；Choice≤255选项，返回选项分布与confidence。资料：[Models](https://docs.typesafe.ai/models)、[Choice](https://docs.typesafe.ai/primitives/choice)、[State](https://docs.typesafe.ai/concepts/state)。版本后续可能变化，开工复核并锁定，不默认追随latest。

Jev对数值精度、长杂乱上下文和恶意state存在限制，不能承担TTL计算、坐标运算或硬安全规则。[Jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)。

## 2. 请求组织

- state包含：当前英文goal、已绑定参数的必要摘要、原JSON与对应小说明、各owner实际执行状态。
- 每个owner一题Choice，criteria来自EX本轮候选映射；option_id和自然语言含义一起给模型。
- instructions明确只选择当前goal下已给出的操作，不猜参数、不推进下一步骤。
- 请求携带本地decision_id、snapshot_id及版本关联；供应商响应映射回固定候选。模型看不见/不选择消息收件地址。
- 每次请求自带必要状态，不假设服务端记得上次说明或当前goal。
- 候选数量上限含wait/keep/cancel/replan；超限必须报清楚。不要静默截断插件列表。
- 同请求多个问题是独立判断，不保证资源兼容；统一交B04仲裁。

## 3. 分步实施

| 步骤 | 工作 | 验收 |
|---|---|---|
| 1 | 锁模型/SDK或HTTP API版本，接口和依赖兼容Python3.10 | x86/arm64导入通过，核心不被SDK污染 |
| 2 | 从快照构造instructions/state/criteria，限制相关输入大小 | 同快照内容可回放；文档字段和JSON均可见 |
| 3 | 响应解析/option验证；缺失问题、未知ID、无效概率拒绝 | 无法解析时零下发，无“选第一项”兜底 |
| 4 | 超时、429、网络错误、SDK默认重试管理 | 总deadline可控；不能后台重试到goal早已变化 |
| 5 | 配置限频和单live请求，影子模式只记录不执行 | shadow下动作计数=0；latest快照合并不积压 |
| 6 | confidence 门槛由评测确定；EX dispatch 前低置信拒绝 | 保留原 choice/score；实际停止/退役反馈，不隐式 wait，不把 confidence 当安全概率 |
| 7 | 状态/日志记录真实model、耗时、拒绝原因、输入hash | 不记录key；当前提示展示与实际请求相符 |
| 8 | 冻结评测集，与确定性规则和LLM直接选择做对照 | 分清模型质量与EX安全门禁的贡献 |

建议模拟起始配置：最多2次/秒、单次总deadline1500ms、并发1；这些是待压测参数，不是对Jev延迟的承诺。云端调用超时后不更新动作租约；本地安全不依赖下一次回答。

## 4. 测试及效果门槛

新增 `test_jev_backend.py/test_decision_backend_contract.py`；真实调用使用单独显式开关，CI默认mock，不偷用API key。

确定性测试：正常解析、全wait、未知option、漏owner、超255项、网络超时、429 Retry-After、取消请求晚返回、模型配置变更、超长state、注入文本、SDK失败。

评测集建议100个冻结场景：正常40、歧义20、陈旧感知10、冲突/取消15、参数缺失/失败恢复15；至少三次重复，真实付费调用前确认预算。保留未参与调prompt的留出集；若调整门槛/提示词，重新报告版本，不挑最好一次。

| 指标 | 初始验收门槛（设计目标） | 说明 |
|---|---|---|
| 确定性接口/故障测试 | 全通过 | 不能skip错误路径 |
| 明确场景选项正确率 | ≥95% | 多个合理答案预先标注集合 |
| 歧义/缺参数保守处理率 | ≥90% | wait或request_replan等预标答案 |
| 不合法命令实际执行数 | 0 | 由门禁保证，另报Jev提出不良选择次数 |
| 影子模式副作用 | 0 | 包括无意续租/启用插件 |
| goal替代后晚到结果下发 | 0 | 即使响应语义正确也拒绝旧版 |
| 请求延迟/成本 | 报p50/p95/max、tokens、超时率 | 不预设云延迟能达到硬实时 |

必须比较“LLM-only调度”和“LLM规划+Jev调度”在同场景的任务成功率、模型调用次数、总耗时、实际账单/估算依据。达到安全门槛但Jev效果不够，可以交付框架与shadow后端，**不能签署Jev可执行模式通过**。

## 5. 回滚

停机后切mock或disabled，不能在真实执行中自动切不同模型继续运动。保留后端接口，插件无需改动。旧模型/提示词/评测数据版本一起保留，避免只回滚model却沿用不匹配阈值。
