# B02 验收报告：端口单调时钟与当前窗口收数健康

## 结论

**模块验收通过（PASS），原生 DDS 验收未执行（NOT_RUN）。**

Windows 上的 fake native / 假时钟结果**不能**称为原生 DDS 通过。本报告只证明：
端口能独立回答"资源是否建好 / 图上是否有 peer / 当前窗口是否真收到回调 / 是否真被消费"，
且旧 `state`、旧累计 stats、旧队列语义、旧 publish 返回值全部保持不变。

## 基线与环境

| 项目 | 值 |
| --- | --- |
| 仓库 / worktree | `D:\Code\AstrBotEX` / `D:\Code\AstrBotEX-work\ros-diag-b01-b03` |
| 分支 / commit | `feat/ros-diag-b01-b03` / `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 操作系统 | Windows 11 10.0.26200 |
| Python | 3.13.14 |
| ROS / RMW | 无（`rclpy` 未安装） |

四个基线文件 SHA256 与 C02 逐项一致（见 B01 报告表格），无基线漂移。

## 变更

| 操作 | 文件 | 增删 |
| --- | --- | --- |
| 修改 | `astrbot_ex/core/environments/contracts.py` | +29：`DIAGNOSTICS_MODES`、`DIAGNOSTICS_DEFAULTS`、`number()`、`diagnostics_policy()`；`parse_ports` 规范化 `diagnostics` |
| 修改 | `astrbot_ex/core/environments/plugin_api.py` | +155：`DATA_HEALTH`、`health_evaluator()`、`_Port` 窗口状态与四个新方法；`status/receive/get_nowait/unavailable` 扩展 |
| 修改 | `astrbot_ex/core/environments/ros2/adapter.py` | +5：`attach` 在 native 建立后调用 `port.mark_bound()`；`_discover` 加注释，逻辑不变 |
| 新增 | `tests/test_ros2_port_health.py` | 545 行 / 39 用例 |

未删除任何既有字段或方法：`last_message_at`、累计 `stats`、`state/status`、`queue` 语义、`publish` 返回值、
runtime gate、stop token 全部保留。

## 新增接口

### manifest 声明（C01 §1）

```json
"diagnostics": {"mode": "periodic", "startup_grace_sec": 10.0, "stale_after_sec": 5.0}
```

- 不声明 → 规范化为 `{"mode":"off","startup_grace_sec":10.0,"stale_after_sec":5.0}`；旧 manifest 无需改动。
- `mode` 仅 `off/periodic`；数值必须有限 int/float（排除 bool）；grace ∈ [0,300]，stale ∈ [0.2,300]。
- `periodic` 仅允许 `subscribe`；`publish` 声明 periodic 直接 `ValueError`。
- 未知 `diagnostics` 字段拒绝；`normalize_bindings` 白名单**未扩大**，binding 里伪造 `diagnostics` 报
  `unknown ROS binding field`。

### `_Port` 新方法

```python
_Port.mark_bound(now_ns=None) -> int
_Port.set_diagnostics_activity(active: bool, now_ns=None) -> int   # 返回 monitoring_epoch
_Port.diagnostics_sample(now_ns=None) -> dict                      # C01 §9 扁平 26 键
_Port.monitored -> bool                                            # property
```

### `status()` 新增字段

`status()` 返回 dict 的**旧键一个不少**，另加：

- `data_health` ∈ `not_monitored/disabled/paused/unavailable/warming_up/no_data/receiving/stale/unknown`
- `diagnostics` = `{policy, monitoring_active, monitoring_epoch, binding_received, receive_age_sec,
  consume_age_sec, monitoring_age_sec, reason_code}`

age 由后端用 `monotonic_ns` 算好并截断为非负数或 `null`；前端不需要比较单调时刻。

## 窗口语义

- 窗口起点 = `max(绑定成功时刻, 本轮 RUNNING 开始时刻)`。attach 晚于 runtime start 时不继承已消耗的宽限。
- `set_diagnostics_activity(True)` 只在 `False→True` 时新开窗口并 `monitoring_epoch += 1`；重复 `True` 不延长宽限。
- `False` 撤销窗口：`monitoring_started=None`，清本窗口接收/消费证据，但 `monitoring_active` 仍表示"预期要工作"。
- `unavailable()/detach/close` 清绑定起点、`binding_received`、窗口证据；累计 `rx_received` 保留。
- 资源缺失时 `monitoring_active` 允许仍为 `true`（用于呈现 `unavailable`），但 `monitoring_started` 为 `null`，
  即"无数据计时只在 `resource_created=true` 后开始"。

### 示例时间线（假时钟，grace=10 s，stale=5 s）

| t (s) | 动作 | 结果 |
| --- | --- | --- |
| 0.000 | `set_diagnostics_activity(True)` | epoch=1，`monitoring_started=null`（尚未 attach） |
| 0.400 | `mark_bound()` | `bound=0.4`，`monitoring_started=0.4`，`binding_received=0` |
| 3.000 | `receive()` | `last_receive=3.0`，`binding_received=1` |
| 8.000 | `status()` | `receive_age=5.000`（等于阈值不算 stale） |
| 8.001 | `status()` | `receive_age=5.001` → stale |
| 9.000 | `set_diagnostics_activity(False)` | 窗口撤销，证据清空，`monitoring_started=null` |
| 9.100 | `set_diagnostics_activity(True)` | epoch=2，`monitoring_started=max(0.4,9.1)=9.1`，重新宽限 |
| 9.500 | 旧 epoch 回调晚到 | 被 `binding_generation` 拒绝，不计数 |

## 锁说明

- 所有窗口字段的读写都在**既有** `port.lock`（`RLock`）内；`mark_bound`/`set_diagnostics_activity` 自己取锁，
  adapter 在已持有 `port.lock` 时调用它属于重入，不引入新的锁顺序。
- `status()` 在锁内构造 sample 与 diagnostics，**释放锁之后**才调用 B00 纯规则；规则不触碰 runtime/manager。
- 未新增任何锁，`manager.resources_lock → port.lock` 仍是唯一顺序。
- ROS callback 内不做诊断事件、不读 sysfs、不做 JSON 大复制；`receive()` 只多了一次 `monotonic_ns()` 与两个标量赋值。

## B00 依赖与降级

`status()` 通过 `health_evaluator()` 惰性导入 `astrbot_ex.core.diagnostics.rules.evaluate_port_health`
（B00 的所有权文件，本分支不创建、不 stub）。

| 情形 | `data_health` | `diagnostics.reason_code` |
| --- | --- | --- |
| mode=off 或 publish 端口 | `not_monitored`（本地结构性判定，不调规则） | 原 `reason_code` |
| B00 未合并 | `unknown` | `health_rules_unavailable` |
| 规则抛异常 | `unknown` | `health_rules_failed` |
| 规则返回非枚举值 | `unknown` | `health_rules_invalid` |
| 规则正常 | 规则值 | 规则值（本地 age 仍保留，规则键覆盖同名键） |

**因此本分支单独运行时周期端口的 `data_health` 为 `unknown`——这是有意为之，不能涂绿。**
B00 合并后无需改本文件即可变绿。

## 命令与结果

| 命令 | 时间 | 退出码 | 结果 |
| --- | --- | --- | --- |
| `python -m unittest discover -s tests -p "test_ros2_port_health.py"` | 2026-10-31 20:38 | 0 | `Ran 39 tests … OK` |
| `python -m unittest discover -s tests -p "test_environments.py"` | 同上 | 0 | `Ran 13 tests … OK` |
| `python -m unittest discover -s tests -p "test_environment_boundaries.py"` | 同上 | 0 | `Ran 9 tests … OK` |
| `python -m unittest discover -s tests`（全量回归） | 20:32–20:38，307.7 s | 0 | `Ran 885 tests … OK (skipped=4)` |

## 场景覆盖（B02 验收清单逐项）

| 要求 | 用例 |
| --- | --- |
| 有 peer 零回调 | `test_peer_visible_with_zero_callbacks_shows_no_receive_evidence` |
| matched 未知但当前回调有效 | `test_matched_unknown_but_current_callback_is_strong_evidence`、`test_zero_matched_stays_a_known_zero` |
| 重绑定后旧 rx 非零 | `test_rebind_clears_binding_evidence_and_keeps_cumulative_stats` |
| 旧闭包回调晚到 | `test_late_callback_from_the_previous_binding_is_rejected`、`test_callback_from_the_previous_environment_generation_is_rejected` |
| 接收后停流 | `test_stream_stopping_after_receive_shows_age_growth_not_health_loss` |
| runtime idle / 快速 stop→start 重开窗口 | `test_idle_then_fast_restart_opens_a_fresh_window`、`test_idle_window_is_revoked`、`test_repeated_activity_does_not_extend_the_grace_window` |
| 墙钟跳变 | `test_wall_clock_jumps_do_not_move_the_health_window` |
| 超大消息只证明 transport | `test_oversized_message_proves_transport_but_not_consumption` |
| expired 消息不更新消费时间 | `test_expired_message_never_updates_the_consume_clock`、`test_queue_is_empty_so_nothing_is_consumed` |
| publish / mark 默认不监测 | `test_publish_and_mark_ports_are_not_monitored`、`test_off_mode_never_calls_the_health_rules` |
| 窗口起点不早于 attach | `test_window_starts_at_binding_not_at_runtime_start`、`test_no_data_timer_waits_for_the_native_resource` |
| 声明非法值 | `DiagnosticsPolicyContractTest`（NaN/Inf/bool/负数/越界/未知 mode/未知字段/publish periodic/binding 伪造） |
| 旧语义保持 | `test_status_keeps_every_legacy_field_and_adds_health`、`test_diagnostics_never_changes_queue_semantics`、`test_control_outputs_still_require_volatile_and_finite_ttl` |

## 未执行（NOT_RUN）

| 项 | 原因 | 解除条件 |
| --- | --- | --- |
| 原生 `create_subscription` 回调到达 | 无 ROS | B07 补证据 |
| 真实 DDS matched 计数与 QoS 兼容 | 无 ROS | B07 |
| B00 规则联调（`data_health` 全枚举） | B00 未合并 | B00 合并后 B04/B10 复跑 |
| `set_diagnostics_activity` 由真实 runtime 事件驱动 | B08 负责接线 | B08 |

## 交接给 B04 / B08 / B09

- 采样方法：`port.diagnostics_sample(now_ns)`，返回 C01 §9 的 26 个扁平键，`json.dumps` 安全，不含 native/node/message 对象。
  `manager` 需在 port 锁外补 `session_id`、环境身份与采集完整性（本任务不越界取 manager 资源锁）。
- 切窗方法：`port.set_diagnostics_activity(active, now_ns)`；B08 在 runtime 状态边沿调用，快速 stop→start 时
  即使当前仍为 true 也要先 `False` 再 `True`，返回值即新的 `monitoring_epoch`。
- 旧 callback 拒绝证据：`binding_generation` 在 `attach` 与 `unavailable` 各 +1，闭包捕获的 epoch 不匹配即丢弃
  （`tests/test_ros2_port_health.py::test_late_callback_from_the_previous_binding_is_rejected`）。
- B08 不需要改本文件即可接线：只需在 runtime RUNNING/非 RUNNING 边沿调用 activity 方法，并消费 `status()`。
