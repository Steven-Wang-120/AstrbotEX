# B03 验收报告：事件等级保留与有限可靠日志流

## 结论

**模块验收通过（PASS），SSE/HTTP 端到端验收未执行（NOT_RUN）。**

本报告证明：中文普通 warning 不再被过滤、等级统一、每个留存事件有稳定 ID、游标重放不会静默丢历史、
慢消费者溢出可见。**不**证明浏览器端渲染、鉴权、Host/Origin 校验——那是 B05/B08 的范围。
这不是永久无损审计日志，只是有限实时事件台。

## 基线与环境

| 项目 | 值 |
| --- | --- |
| 仓库 / worktree | `D:\Code\AstrBotEX` / `D:\Code\AstrBotEX-work\ros-diag-b01-b03` |
| 分支 / commit | `feat/ros-diag-b01-b03` / `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 操作系统 | Windows 11 10.0.26200 |
| Python | 3.13.14 |
| ROS / RMW | 无（与本任务无关） |

`astrbot_ex/core/event_bus.py` 基线 SHA256 `b4a5081…a4fa4ec` 与 C02 一致。

## 变更

| 操作 | 文件 | 增删 |
| --- | --- | --- |
| 修改 | `astrbot_ex/core/event_bus.py` | 重写为 250 行：唯一等级规范化、保留字段、`open_stream`、节流键上限 |
| 新增 | `astrbot_ex/core/event_stream.py` | 291 行：`EventStream`、`parse_cursor`、帧构造、投影 |
| 修改 | `tests/test_event_bus.py` | 3 → 26 用例（原 3 个用例断言未改动） |
| 新增 | `tests/test_event_stream.py` | 425 行 / 36 用例 |

未修改 `api_server.py`（B08 接线时替换其本地 `EventStream`），未修改 `models.py` 的 `RuntimeEvent` 四字段形状。
`emit` 仍返回 `RuntimeEvent`，`subscribe/recent/emit_throttled` 签名与语义不变。

## 等级规范化（C01 §7）

唯一入口 `normalize_level(event)`，优先级：

1. `type ∈ {fault, plugin_fault, rule_rejected}` 或非空 `data.error` → **至少 ERROR**（即使生产者写 `severity=debug`）。
2. 显式 `severity`/`level`：`debug/trace/verbose→DEBUG`、`info/information/notice→INFO`、
   `warn/warning→WARNING`、`error/err→ERROR`、`fatal/critical/crit→CRITICAL`。
3. 其余：`_ALWAYS_TYPES`（结构/生命周期/`ros_diagnostic`/`vision_health`）或生命周期关键字 → INFO，否则 DEBUG。

`_should_publish` 判定：等级 ≥ WARNING 一律保留；`_ALWAYS_TYPES` 保留；生命周期关键字保留；其余过滤。
**不是** `return True`。

固定保留的 `_ALWAYS_TYPES`：

```
environment, environment_changed, ros_graph_changed, ros_endpoints_changed,
decision_changed, fault, plugin_fault, rule_rejected, runtime_state,
ros_diagnostic, vision_health
```

仍被过滤的高频噪声：`frame`、`tick`、`vision_plugin` 的每帧包、`motion` 意图。

## 保留字段

```python
data.log = {"level": "WARNING", "source": "ros.port", "stream_id": "<uuid hex>",
            "sequence": 12, "event_id": "<stream_id>:12"}
```

- `stream_id` 每个 `EventBus` 实例随机生成（可用构造参数注入以便测试）。
- `sequence` 从 1 开始，**只给通过过滤且已入环的事件分配**；序号与入环在同一把锁内完成。
- 生产者传入的 `data.log` 一律被覆盖；等级与来源在写入保留块**之前**计算，所以伪造块既不能抬高也不能降低等级。
- `source` 优先级：`source` → `plugin_id` → `plugin` → `environment` → `session_id` → `type`；
  去控制字符、合并空白、截断 64 字符，不读取任何凭据字段。

## 帧与游标

帧形状（C01 §7）：

```python
{"kind": "event",   "event": RuntimeEvent, "event_id": str}
{"kind": "control", "control": {kind, reason, stream_id, oldest_sequence, newest_sequence, dropped_count}}
```

`recent()` 只返回历史帧；`get(timeout)` 只返回后续 live/control 帧或 `None`（B03 §5）。

`open_stream(last_event_id=None)` 在一把锁内完成"游标校验 + 历史快照 + 订阅注册"，返回 `EventStream`
（暴露 `recent()/get()/close()`，另有 `stats()`、`closed`、`stream_id`、`head`）。

| 游标 | 行为 |
| --- | --- |
| 无 | 回最近 50 条，无 control 帧 |
| `<sid>:<n>` 有效 | 回 `n` 之后的全部留存事件（≤1000） |
| `n == head` | 不回历史，只接 live |
| 其他 stream_id | `reset` / `cross_stream`，仍回留存历史，`dropped_count=null` |
| `n > head` | `reset` / `cursor_ahead`，不等待未来序号 |
| `n < oldest-1` | `gap` / `history_expired`，`dropped_count = oldest-1-n`（精确） |
| 非法 / >128 字符 / 序号 ≤0 / 非十进制 | `ValueError`（HTTP 层映射 400） |

## 并发与有序算法

- 回调在 EventBus 锁**外**执行；`EventStream._on_event` 只做有界复制与入队，且整体 try/except，
  不会把异常抛回生产者或其它订阅者。
- 实时缓冲是 `{sequence: frame}` 字典 + `next_expected` 指针：只释放下一个期望序号，因此乱序到达会被重排，
  重复序号被丢弃（内容相同但 ID 不同则都保留）。
- 早序号缺失时**不立即报丢**：先等 `reorder_grace`（默认 0.25 s）；只有确认永久缺失才发
  `gap` / `sequence_hole`（`dropped_count=null`，未知不猜）。
- 溢出：帧数 >200 或估算字节 >2 MiB 时丢最旧，记录 `dropped_through`，下一次 `get()` 先发
  `gap` / `queue_overflow` 并给出**精确** `dropped_count`，不静默断档。
- 锁顺序：`emit` 持 `bus._lock` 后调回调 → `stream._condition`；`close()` 先在 condition 内置位、
  **释放后**才 `bus._detach`，因此不存在 `condition → bus._lock` 的反向获取，无死锁。
- 历史/live 交叉去重靠 `sequence <= head` 直接丢弃，O(1)，不需要无界 ID 集合。

## 投影预算

`project_event(event, budget=65536)` 给 B08 的传输投影：message 截断到 16 KiB、data 字符串 4 KiB、
嵌套深度 3、每层 64 项，超预算则把 `data` 换成截断标记；`event_id` 原样保留，脱敏/截断不改变流标识。
原事件不被修改（有专门用例断言 `json` 前后一致）。

## 命令与结果

| 命令 | 时间 | 退出码 | 结果 |
| --- | --- | --- | --- |
| `python -m unittest discover -s tests -p "test_event_bus.py"` | 2026-10-31 20:38 | 0 | `Ran 26 tests … OK` |
| `python -m unittest discover -s tests -p "test_event_stream.py"` | 同上 | 0 | `Ran 36 tests … OK`（2.44 s） |
| `python -m unittest discover -s tests`（全量回归） | 20:32–20:38，307.7 s | 0 | `Ran 885 tests … OK (skipped=4)` |

## 确定性场景覆盖

| 要求 | 用例 |
| --- | --- |
| 中文"未收到图像" severity=warning 必须入环 | `test_chinese_warning_without_lifecycle_keyword_is_kept` |
| plugin_fault = ERROR | `test_error_types_are_errors_whatever_the_producer_claims` |
| ros_diagnostic recovered INFO 保留 | `test_ros_diagnostic_recovery_info_is_kept` |
| frame/tick 仍过滤 | `test_frame_and_tick_noise_stays_filtered` |
| 伪造 log 字段被覆盖 | `test_producer_cannot_forge_the_reserved_block` |
| 被过滤事件不占 sequence | `test_filtered_events_do_not_consume_a_sequence` |
| 50 条初始 / 1000 环上限 | `test_without_a_cursor_only_the_recent_tail_is_replayed`、`test_an_expired_cursor_reports_an_exact_gap` |
| 200 实时队列溢出 / 字节超限 | `test_frame_budget_overflow_drops_oldest_with_an_exact_count`、`test_byte_budget_overflow_drops_oldest` |
| 过期 / 非法 / 跨实例 / 未来游标 | `test_an_expired_cursor_reports_an_exact_gap`、`test_malformed_cursor_raises`、`test_a_cursor_from_another_instance_resets`、`test_a_cursor_beyond_head_resets_instead_of_waiting` |
| 多线程反序回调 | `test_reverse_order_callbacks_from_many_threads_stay_ordered` |
| barrier 同时取历史/订阅/emit | `test_open_snapshot_and_emit_never_lose_the_boundary_event` |
| close 与 emit 竞态 | `test_close_racing_emit_does_not_deadlock`、`test_close_is_idempotent_and_detaches` |
| 回调不在 EventBus 锁里 | `EventBusConcurrencyTest.test_subscriber_callbacks_run_outside_the_bus_lock` |
| 内容重复但 ID 不同保留 | `test_identical_content_with_distinct_ids_is_kept`、`test_identical_content_keeps_distinct_ids` |

## 需要其它 owner 知晓的跨模块改动（重要）

1. **`tests/test_decision_management_boundaries.py` 断言已按 C01 §7 更新**（+7/−2 行）。
   原断言 `set(notice[0].data) == {request_id, snapshot_id, sequence, kind}` 与"每个留存事件都有 `data.log`"
   直接冲突。新断言保持原安全意图：业务键仍**恰好**是那 4 个，保留块恰好是 5 个键，
   且 marker 仍不出现在 `json.dumps(data)` 中。`sse_payload` 对 `decision_changed` 的白名单未放宽。
2. `api_server.py` 自带的本地 `EventStream` 类**未被修改**；B08 应改为消费 `EventBus.open_stream()`，
   并用帧里的 `event_id` 写 SSE `id:`（在 `sse_payload` 脱敏之后单独发送）。
3. 观察（非本任务回归）：`tests/test_api_server.py` 第 91 行使用 `unittest.mock.patch` 但未 `import unittest.mock`，
   单独运行该文件会 `AttributeError`，全量 discover 时因其它模块先导入而通过。属既有问题，未修改。

## 未执行（NOT_RUN）

| 项 | 原因 | 解除条件 |
| --- | --- | --- |
| SSE 端到端（`Last-Event-ID`、断线重连、凭据 epoch） | 需 B08 接线 `api_server.py` | B08 |
| 401/403/409 与 Host/Origin 校验 | B08 范围 | B08 |
| 浏览器端去重、滚动、筛选、导出 | B05/B06 | B05/B08 |
| 真实 ROS 故障注入（peer 有而回调为 0） | 无 ROS | B07/B10 |

## 明确不声称

- 这不是永久无损审计日志：环形 1000 条之外的历史会以 `gap` 明示缺失。
- `dropped_count` 在无法精确统计时返回 `null`，不猜数字。
- 未验证任何浏览器渲染或真实 SSE 网络行为。
