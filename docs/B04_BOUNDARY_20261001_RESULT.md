# B04 停止边界与可信后端切换：技术交接

本页记录 [DecisionService](../astrbot_ex/core/decision/service.py)、Controller/FeedbackJournal 的停止、退役与可信后端切换边界。它们复用原 Dispatcher/Actor/Ledger；管理激活与组件关闭见 [B08](B08-DECISION-MANAGEMENT.md)。

## 停止完成边界

[DecisionService](../astrbot_ex/core/decision/service.py) 的 `request_stop(reason)` 立即撤销执行资格，返回 `operation_id` 回执。`status()["stop"]` 是最新操作副本，含 session、gate epoch、reason、attempts、error。

| 状态 | 含义 | 可否视为框架已停 |
|---|---|---|
| requested | 已登记或等待有界重试 | 否 |
| running | 取消、等待证明或复核中 | 否 |
| proven | 取得可信证明并更新状态 | 仅匹配本次 operation_id 时 |
| failed | 有界尝试或复核失败 | 否 |

```python
receipt = service.request_stop("management_stop")
stop = service.status()["stop"]
completed = (stop is not None
             and stop["operation_id"] == receipt["operation_id"]
             and stop["state"] == "proven")
```

私有 `_stop_pending=False`、HTTP 返回、cancel、本地连接关闭或页面暂停都不是停止证明。自动重试保留 ID；新操作绑定当前 Dispatcher epoch，旧证明/迟到结果不能覆盖新请求。正向证明不自动清除 blocked Goal、不授予新执行。同步 close 记录 proven/failed，关闭后拒绝停止/取消及迟到重新排队。管理层用异步 operation 关联服务的最新停止回执，见 [B08](B08-DECISION-MANAGEMENT.md)。这些是框架证据，不能写成机器人物理停车。

## 低置信与 request_replan 的退役边界

服务保留后端原 choice/score，execute 下任何 choice 低于 `min_confidence` 以 `low_confidence` 拒绝整轮，选择 `request_replan` 以 `backend_requested_replan` 拒绝整轮；均在新 Action dispatch 前拦截，不替换为 wait、不默认第一项。正常阈值内 wait 保留活动 Goal，shadow 只记录原选择。

拒绝先撤授权/关 gate，已有动作须通过实际 Actor/Ledger StopEvidence、资源释放及当前 Dispatcher epoch 复核；unknown/timed_out、缺证明或旧迟到证据不能冒充可重规划终态。Controller 在锁外收集账本前序事实并持久化 completion draft，再以当前 Goal/revision/epoch CAS 退役，发布唯一 failed 反馈。draft 不等于已发布事实，重启不能提升；journal 失败关闭执行，不授任务完成许可。

EX 的 currentGoal 只是一当前步骤，不是 HostTask 全部计划。Host 收到可信退役反馈后只重规划未完成后缀，已完成前缀不得重放；取消不自动规划。EX 不自行生成下一 Goal。停止 proven 不把 failed 变成功，也不自动清 blocked。

## 可信后端切换

`replace_backend(name, trusted_factory)` 仅供可信框架代码，不接受 HTTP callable/import 路径。要求 disabled、无活动/待替代 Goal、无未完成停止/动作、无未证明停止、无旧模型请求或待处理结果。

1. 短锁检查资格，登记切换标记并捕获版本。
2. 锁外检查新鲜 Ledger、构造候选，再检查 Ledger。
3. 短锁重新核对资格/版本，提交后端身份及 config_revision。
4. 撤销旧授权，锁外关闭旧后端。

模型构造、Ledger 等待和旧 close 不占状态锁；期间停止/上下文变化使未提交切换失效。提交前失败保留旧实例/版本并关闭候选；提交后旧 close 失败返回 `applied=True/cleanup_error`，不回退损坏实例。status 显示实际类型/切换/清理错误。切换不自动启用 execute，仍检查新后端 execution_allowed 与新 Goal 授权。管理执行还需 [runtime/control 激活](B08-DECISION-MANAGEMENT.md#3-http-路由与启停合同)，仅换后端不等于已运行。

## 复验入口和已知限制

从仓库根运行（临时 Ledger/测试 Actor，不接机器人）：

```sh
python -B -m unittest tests.test_decision_service -v
python -B scripts/diagnose_dispatcher_stress.py --check-import
python -B scripts/diagnose_dispatcher_stress.py
```

具名测试：[test_decision_service.py](../tests/test_decision_service.py)、[原规模 Dispatcher 测试](../tests/test_action_dispatcher.py)。测试 Laya stub 只证明快照→BackendDecision→EX→Dispatcher→测试 Actor，不能证明权重/延迟/ROS/Isaac 闭环。

退役具名回归为 `tests/test_decision_replanning.py`、`tests/test_decision_aeb_replanning.py`，覆盖低置信、正常 wait、实际 Actor cancel proof、缺证明、持久化反馈、完成前缀和旧代次 fencing。压力诊断保持真实 1000 sequences、原随机种子、assertions 与 Future 时限；定向通过不构成长期稳定保证。平台压力入口见 [来源与检查](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)，详细运行结果只保存到外部 evidence/临时目录。
