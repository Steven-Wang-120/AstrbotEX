# B04 诊断协调器与恢复告警 —— 验收报告

| 项 | 值 |
| --- | --- |
| 任务包 | `AstREX_ROS2诊断与日志改造任务包_20261031` |
| 仓库 | `D:\Code\AstrBotEX-work\ros-diag-b04-b06`（AstrBotEX worktree） |
| 分支 | `feat/ros-diag-b04-b06` |
| 基线 commit | `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 工作树状态 | 仅本任务新增/修改文件，见文末「交付物清单」 |
| 操作系统 | Windows-11-10.0.26200-SP0 |
| Python | 3.12.10 |
| ROS / RMW | 未安装（`ROS_DISTRO`、`RMW_IMPLEMENTATION` 均未设置） |
| 结论等级 | **PASS（模块验收）** |

> 本报告只证明 B04 协调器的模块级行为。真实 ROS 图与真实端口的端到端验证属 B07/B08/B10，本次**未执行**。

---

## 1. 交付物

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `astrbot_ex/core/diagnostics/service.py` | 986 | 新增。诊断协调器本体 |
| `tests/test_diagnostics_service.py` | 741 | 新增。37 个用例，假时钟 + 假采集器 + 记录型事件总线 |

未改动任何既有文件。`astrbot_ex/core/diagnostics/` 为隐式命名空间包，已确认无需 `__init__.py` 即可导入（与仓库既有约定一致）。

## 2. 与 C01 冻结接口的一致性

| 契约 | 实现 |
| --- | --- |
| `DiagnosticsService(collector, preflight_collector, event_bus, *, clock_ns, tick_sec=1.0)` | 一致；另提供可选关键字参数 `wall_clock`、`evaluator`、`preflight_evaluator`、`max_active_issues`、`max_port_samples`，默认值即契约值 |
| `.start()` / `.snapshot()` / `.request_refresh()` / `.invalidate()` / `.close(timeout=2.0)` | 五个方法全部实现 |
| 构造不启动线程、不采集 | 一致（`test_construction_starts_no_thread_and_reports_unknown`） |
| `snapshot()` 只读、深拷贝、不回调采集器 | 一致（`test_snapshot_never_calls_the_collector`、`test_snapshot_is_a_deep_copy`） |
| 事件 payload `severity/source/code/issue_key/scope/state/evidence/remediation` | 一致；`state ∈ {active, recovered, withdrawn}`，额外附 `reason`（仅 withdrawn）、`reminder`（bool）、`schema_version` |

## 3. 关键实现决策

1. **回调期间不持锁。** `_run_cycle` 在锁外调用采集器、纯规则求值器与事件总线，只把结果搬进锁内合并。因此订阅者在事件回调里同步调用 `snapshot()` 不会自锁死（`test_snapshot_from_inside_a_subscriber_does_not_deadlock`）。
2. **缺失证据永不算健康。** 采集器抛异常时保留上一次的环境身份与运行时标志，不把它当成"环境切走了"，因此既有问题**不会被撤回也不会被恢复**，只标记 `partial`（`test_collector_exception_keeps_issues_and_never_recovers`）。
3. **`collection_age_sec` 与预检 `age_sec` 在读取时按 `clock_ns()` 现算**，不在采集时冻结，避免快照被当成"刚采的"。
4. **撤回 ≠ 恢复。** 运行时停止、监测被暂停、端口消失、代际变更 → `withdrawn`（附 `reason`：`runtime_not_running` / `monitoring_inactive` / `port_gone` / `reclassified`）；只有连续 2 次健康采样才发一次 `ROS_DIAGNOSTIC_RECOVERED`（INFO）。
5. **RX 存活性家族共享 60 秒提醒预算。** `ROS_RX_NO_PEER` / `ROS_RX_NO_DATA_WITH_PEER` / `ROS_RX_STALE` 同属 `rx_liveness`，同一 scope 内至多一条活跃问题；互相切换时旧的以 `reason="reclassified"` 撤回，新的一条在预算内不重复打扰。
6. **去重键** = `session_id + environment_generation + owner_generation + binding_generation + monitoring_epoch + endpoint + code`，`issue_key` 对同 scope 同 code 稳定（`test_issue_key_is_stable_and_code_specific`）。
7. **刷新合并与作废。** 刷新风暴合并为一次请求；刷新期间身份发生位移，则本轮以 `superseded` + `E_SUPERSEDED` 收尾，不覆盖新代际的结论。
8. **全量有界。** 活跃问题 512、端口采样 1024、证据键 24 / 单值 512 字符、scope 文本 128 字符；证据里的 ANSI/控制字符一律剥除。1 万次重绑定后映射表仍有界（`test_ten_thousand_rebinds_keep_the_maps_bounded`）。
9. **关闭不撒谎。** `close()` 幂等；采样线程在超时内没退出时抛 `DiagnosticsServiceTimeout`，不谎报成功（`test_close_refuses_to_claim_success_while_the_thread_runs`）。

## 4. 运行命令与结果

```
"C:/Users/17088/AppData/Local/Programs/Python/Python312/python" -m pytest tests/test_diagnostics_service.py -q
```

- 退出码：`0`
- 结果：**37 passed**（0 failed / 0 skipped / 0 error）
- 用时：约 1.0 s

### 用例分布（全部 PASS）

| 分组 | 数量 | 覆盖点 |
| --- | --- | --- |
| `ConstructionTests` | 5 | 构造不起线程、`start` 幂等且同步采一次、快照深拷贝/不回调/可重入 |
| `AlertLifecycleTests` | 6 | 首次告警 + 60 s 提醒上限、刷新不绕过预算、RX 重分类撤回、家族共享预算、复发拿新 `first_seen_at`、双采样只恢复一次、两端口互不吞并 |
| `PreflightTests` | 3 | 普通环境预检被动、30 s TTL、`age_sec`/`current` 随读取推进 |
| `PartialEvidenceTests` | 7 | 采集异常不恢复、不完整采集标 partial、缺 B00 规则 → partial 而非健康、候选缺 scope 丢弃、证据有界且去控制字符、问题数上限、端口采样上限 |
| `IdentityTests` | 3 | 切到空闲 → 撤回、新代际撤回旧 scope、`invalidate` 作废待处理刷新 |
| `RefreshTests` | 5 | 稳定身份下刷新完成、刷新风暴合并、慢探测被新代际取代、刷新失败不丢问题、完成后新刷新拿新 id |
| `CapacityAndShutdownTests` | 8 | `close` 幂等并停事件、线程未停不谎报、`issue_key` 稳定、恢复阈值常量=2、快照可 JSON 序列化、1 万次重绑定有界、20 轮 start/close 回到线程基线 |

### 相邻回归

```
-m pytest tests/test_diagnostics_service.py tests/test_event_bus.py tests/test_environments.py -q
→ 53 passed
```

`test_event_bus`（3）与 `test_environments`（13）均未受影响。

## 5. 未执行项（NOT_RUN）

| 项 | 原因 |
| --- | --- |
| 真实 ROS 图下的诊断时间线（B07/B08） | 本机无 ROS 2 安装，`ROS_DISTRO` 未设置 |
| 真实端口 generation / owner generation 变化 | 依赖 B02 的真实端口实现与 ROS 运行时 |
| 端到端"graph 有 peer 而 callback 为 0 → 黄色日志 → 收数恢复" | 属 B08/B10，需要真实 publisher |
| 长时间（>1 h）内存稳定性 | 本次只做 1 万次重绑定的有界性证明，未做长时压测 |

以上不计入通过数。

## 6. 脱敏说明

测试与报告均使用假采集器构造数据，不含真实主机名、IP、凭据、话题隐私内容。`讨论区.txt` 交接消息中同样不含凭据。

## 7. 交付物清单（本次 commit 内容）

```
新增  astrbot_ex/core/diagnostics/service.py
新增  tests/test_diagnostics_service.py
新增  acceptance/ros-diagnostics/B04/REPORT.md
```
