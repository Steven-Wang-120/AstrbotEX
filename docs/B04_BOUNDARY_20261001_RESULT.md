> 历史报告：迁自 AstrEX 原仓库，保留当时日期、路径和验收结论。当前集成状态见 [集成报告](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

# B04 停止边界、可信后端切换与 Laya 接口验证结果

日期：2026-10-01。本轮仅修改 DecisionService 和对应测试；未 commit、未 push。

## 1. 本轮结论

两项服务能力已实现，并通过最终的 57 项 DecisionService 测试。未来 Laya 后端的接口调用链使用测试后端验证通过；没有加载 Laya 权重，也没有实现 B07 Laya 适配器。

最终完整回归运行 491 项：490 项通过，1 项 B02 压力测试超时，0 项跳过。因此本报告不宣称完整回归全部通过。

B08 可以使用新的公开停止状态和可信切换方法。B02 压力测试的问题仍开放，需要单独决定后续定位范围。

## 2. 实际修改范围

- [service.py](/home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX/astrbot_ex/core/decision/service.py)：停止操作状态、旧操作完成保护、可信后端切换及相关并发边界。
- [test_decision_service.py](/home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX/tests/test_decision_service.py)：新增 15 项测试，保留原 42 项测试；其中 7 项增加状态断言或调整事件等待条件。

没有修改 Dispatcher、Ledger、B07 后端适配器、后端注册表、HTTP 管理接口、前端、ROS 或 Isaac 源码。B04 原文档也未修改。

[工作区审计](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/audit.after.json)：暂存区 index 字节、cached diff、以相同选项取得的 Git status 均与本轮开始时一致。两个修改文件原先即为未跟踪文件，Git status 一致并不代表文件内容未变；内容差异记录在 [changes.patch](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/changes.patch)。另一个任务的已有开发内容保留。

## 3. 停止完成边界

`request_stop(reason)` 立即撤销执行资格，并返回带 `operation_id` 的请求回执。`status()["stop"]` 返回最新停止操作的副本，包含 session、gate epoch、reason、attempts 和 error。

| 状态 | 含义 | B08 可否显示已停止 |
|---|---|---|
| requested | 请求已登记，或等待有界重试 | 否 |
| running | 正在取消、等待证明或复核 | 否 |
| proven | 本次操作已取得可信停止证明并更新状态 | 是，须先匹配 operation_id |
| failed | 有界尝试或复核失败，停止尚未证明 | 否 |

B08 调用示意：

```python
receipt = service.request_stop("management_stop")
stop = service.status()["stop"]
completed = (
    stop is not None
    and stop["operation_id"] == receipt["operation_id"]
    and stop["state"] == "proven"
)
```

这里展示的是服务方法；本轮没有新增 HTTP 路由或 operation 查询表。服务只保留最新停止操作，B08 后续需要按回执 ID 关联自己的管理操作。

- `_stop_pending=False` 只表示队列请求已被工作线程取走，仍不能作为完成依据。
- 自动重试保留操作 ID。迟到的停止结果、显式复核结果和激活复核结果不能覆盖新请求。
- 新停止操作绑定当前 Dispatcher epoch，避免将以前的证明用于新的 SDK 动作。
- 正向停止证明不会自动清除 blocked Goal，也不会代替新的授权。
- 同步关闭记录 proven/failed；关闭后拒绝停止和取消请求，迟到观测也不能重新排队停止。

原失败测试用事件固定“第二次证明仍在处理”的时间窗口，再等待公开 proven 状态。资源、Goal、gate epoch 和 stop_error 等原业务断言保留。

## 4. 可信后端切换

新增 `replace_backend(name, trusted_factory)`。它只供可信框架代码使用。B08 后续应由后端目录提供 factory，不接受 HTTP 请求中的 Python callable 或导入路径。

切换条件：disabled、无活动或待替代 Goal、无未完成停止、无活动动作或未证明停止、无旧模型请求或待处理结果。

切换过程：

1. 在状态锁内检查空闲条件，登记切换标记并捕获版本。
2. 在锁外读取新鲜 Ledger 状态，构造候选后端，再检查 Ledger。
3. 在短锁内重新核对资格和版本，同时提交实际后端身份及 config_revision。
4. 请求撤销旧授权，在锁外关闭旧后端。

模型构造、Ledger 等待和旧后端关闭均不占用服务状态锁。加载期间仍可查询状态、请求停止；停止或其他上下文变化会使未提交切换失效。

- 提交前失败：保留原后端和原配置版本，关闭候选实例，返回明确异常。
- 提交后旧后端 close 失败：保留已生效的新实例，返回 `applied=True` 和 `cleanup_error`，不回退到可能已损坏的旧实例。
- `status()["backend"]` 显示名称、实际类型、切换中状态和清理错误。
- 切换不自动启用 execute。仍检查新后端的 `execution_allowed`，随后需要模式设置和新的 Goal 授权。
- 方法是同步调用，但耗时工作在状态锁外；B08 应放入其异步管理操作，不能阻塞 HTTP 请求处理。

## 5. Laya 验证边界

测试后端 `LayaProtocolStub` 经可信切换进入 EX，然后使用正式 `decide(DecisionSnapshot)` 返回 `BackendDecision`。结果通过 EX 契约检查、ActionDispatcher 和测试 Actor，旧 config revision 的结果被拒绝。

已验证：Mock→测试 Laya 接口实例、正式调用、候选选择、版本失效、停止、加载失败与切换冲突。

尚未验证：真实 Laya 模型输出、推理延迟、权重加载、微调、Laya→ROS→Isaac 的物理闭环。完整回归中的 5 项原生 ROS 测试只证明已有 ROS 通信能力，不能代表上述模型控制闭环。

## 6. 测试证据

环境：AstrBotEX `.venv` Python 3.12.3、ROS 2 Jazzy；本机回环域 173。ROS 测试使用已构建接口并加入 `/usr/lib/python3/dist-packages`，未驱动机器人。

| 记录 | 结果 | 说明 |
|---|---|---|
| [focused-01](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/focused-01.txt) | 55/55 PASS | 初版修改，尚非最终源码 |
| [regression-01](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/regression-01.txt) | 490 项，1 failure、3 error 条目 | 含一个失败测试的 teardown 错误 |
| [affected-02](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/affected-02.txt) | 114/114 PASS | 修正兼容性与故障注入等待后 |
| [regression-02](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/regression-02.txt) | 490/490 PASS | 关闭后保护增加前的源码 |
| [affected-03](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/affected-03.txt) | 115 项，1 failure、2 error 条目 | 两项旧 ActionRuntime 测试出问题，其中一个含 teardown 错误 |
| [focused-04](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/focused-04.txt) | 57/57 PASS | 最终源码，覆盖新增关闭边界 |
| [regression-03](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/regression-03.txt) | 491 项，490 PASS、1 ERROR | 最终源码；B02 压力测试超时，5 项原生 ROS 均通过 |

针对性命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_decision_service -v
```

完整回归命令：

```bash
source /opt/ros/jazzy/setup.bash
source /tmp/astrex_sync_20261001/ros_install/setup.bash
export PYTHONPATH="/usr/lib/python3/dist-packages:${PYTHONPATH}"
export ROS_DOMAIN_ID=173 ASTRBOTEX_TEST_ROS_DOMAIN_ID=173
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_LOG_DIR=/tmp/astrex_b04_boundary_20261001/ros_logs
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

源文件哈希、命令与运行时间见 [regression-03.json](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/regression-03.json)。Python 3.10 语法解析和 `git diff --check` 通过；这不是 Python 3.10 运行验证。

## 7. 失败处理与剩余问题

首轮发现本次修改对 FakeDispatcher 的 `_epoch` 访问兼容性问题，已恢复缺省值兼容。另一个 DecisionService 故障注入测试存在等待窗口竞态，已用阻塞后端 entered 事件固定故障路径，业务断言保留。[受控定位证据](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/worker_io_race_probe.json)。

最终唯一错误：`test_action_dispatcher.DispatcherTests.test_1000_real_dispatcher_state_sequences_with_resources_and_rearm` 在 `tests/test_action_dispatcher.py:601` 等待 `reconcile_stop(...).result(10)` 超时。早前完整回归在同一测试的另一 Future 等待处超时；中间单项运行通过，另一次完整回归也通过。这些结果全部保留，没有修改 timeout、sleep 或断言来筛选通过。

该测试只构造 Dispatcher、Ledger、PluginActor，未使用 DecisionService；上述源码和该测试与同步基线哈希一致。[单项证据](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/dispatcher_1000_isolated.json)、[源码身份](/data/shared/AstrEX_project_data/logs/app/ex_pre_b09_20261002/workspaces/astrex_b04_boundary_20261001/dispatcher_1000_source_identity.json)。目前无证据证明本次两项修改导致它失败，也没有证据证明超时根因已经消除。

affected-03 的 Ledger.admit/关闭超时与旧插件 worker/tick 计数失败也未调用 DecisionService。它们在其他完整回归通过，原因仍未确认；本轮未扩大修改范围。

后续若安排 B02 定位，最小范围是：记录 Future/Dispatcher/Ledger/Actor 等待位置，区分测试等待窗口与真实处理停滞；必要时做一项事件控制的诊断。先取得证据，再决定是否修正 B02，不能直接放宽断言或超时。

人工决定：是否单独安排 B02 定位。它不阻止 B08 配置、查询和管理接口开发；本轮不能承诺原有压力/生命周期测试始终稳定，也不能宣称机器人闭环已通过。


> 2026-10-02 归档说明：本文为 2026-10-01 的历史结果，不代表当前完整回归状态。原报告和全部证据保存在共享归档；本副本仅调整证据链接。
