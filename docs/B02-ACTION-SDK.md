# B02 Direct Action SDK：B03/B04 交接

更新：2026-09-30。本文描述 **ex-integration 当前树**，不是最终验收声明。协调端已集成 Dispatcher Future/priority、千条分页及 B03 runnable；其独立全回归报告为 343 passed、1 failed（HTTP snapshot restore）、5 Windows ROS skips、423 subtests，169.22s，包含两组 1000 campaign 通过。本轮仅修快照执行存储隔离并跑聚焦回归，完整验收仍由协调端复核。验收入口、覆盖边界和待验项见 `docs/B02-ACCEPTANCE-MATRIX.md`。

## 1. 组合与信任边界

`astrbot_ex/core/api_server.py#build_server` 创建持久 SQLite Ledger、真实 Dispatcher、Catalog 和 ActionService；同一服务接入 Runtime、Controller、EnvironmentManager，registry 的 action lifecycle guard 为 `ActionService.prove_owner_stop`。LocalPluginManager 获得同一 Dispatcher/Catalog。实际无硬件组合参考 `tests/test_b02_real_composition.py#RealCompositionActionTest`，不是 MockDispatcher。

- 默认 `control_mode="legacy"`，Dispatcher gate 默认关闭；decision 是显式选择，不能因安装/启用 v2 插件自动开放。
- `RuntimeController.change_mode("decision")` 先撤销 gate、停止 runtime、核对停止，再改变模式；HTTP 对应 `POST /api/v1/ex/runtime/control-mode`，body 为 `{"control_mode":"decision"}`。本轮不启动服务或调用 HTTP。
- decision 模式隔离旧 policy/skill/motion 的 runtime start、worker/tick 与旧 bridge proposal；action owner、感知/交互及必要停止/drain 不等于旧 motion 输出。参见 `tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_decision_mode_suppresses_legacy_start_worker_and_tick`。
- 本 SDK 是执行基底，不提供 B03 模拟器、B04 GoalManager 或模型决策循环。`update_context` 安装**框架已决定并授权的 goal 快照**，不是把 wire/model payload 当授权。
- Python 插件是受信安装代码，不是安全沙箱。只框架/Mock；不交付生产工程插件，不操作真实硬件。
- **不得用 TopicBus 发 v2 控制命令或动作结果。** TopicBus 可承载观测；动作进入 Dispatcher，状态进入 Ledger。可选 v2 topic 元数据也不会成为控制通道。

## 2. 正常 LocalPluginManager 加载与授权顺序

`astrbot_ex/core/local_plugins.py#LocalPluginManager._before_action_load` 在 `on_load` 前从 registry slot 构造 `OwnerBinding(slot.id, slot.generation)`，注册真实 actor/manifest，并将 `context.actions` 绑定到 `dispatcher.report`。插件构造时 facade 尚未绑定；`on_load` 时已绑定不代表存在可报告的 command。框架读取 `manager.records[owner].action_binding`，不要让插件/model 自报 generation，也不要再次手工 register 同一 owner。

- `manager.discover()`、`manager.load_enabled()` 后检查 record 已加载、enabled、无 error，slot 为 ready，且 catalog 可执行项包含同一 owner/generation。
- 已加载实例 disable/enable **保留 generation**；配置重载/卸载再加载生成新 binding。失败代际不复用。不得写死 generation=2。
- `manager.refresh_capabilities()` 捕获当前已加载 manifest/config、guide、slot 状态与目录漂移。`catalog.snapshot().executable()` 排除 disabled、blocked、缺 guide、目录漂移项；它不是授权 grant。
- B03/B04 负责串行化“采样版本 → 选择 goal/action/绑定参数 → 授权 → 下发”。refresh/目录变化之后显式同步版本；不要假定赋值 `catalog.on_change` 会替代当前 `CapabilityCatalog.refresh` 的行为。
- 所有版本/会话/goal/task/观测接收时间都来自可信框架；命令 payload 只供比较。不要在 runtime 主锁内等待 Ledger Future 或停止。

下面是**受信框架侧**的 Mock 接入片段，假定已有上述组合对象和框架生成的 session/goal/task/版本；并非插件 API 或 HTTP commands endpoint：

```python
from astrbot_ex.core.actions.models import ActionCommand

# 若 runtime 还在 legacy，先通过 controller.change_mode("decision") 停机切换。
# 需要 worker 推进时，再显式 controller.start()；其版本变化也会撤销旧 context。
assert service.control_mode == "decision"
manager.refresh_capabilities()
record = manager.records[owner_id]
binding = record.action_binding
assert binding is not None
snapshot = catalog.snapshot()
assert any(entry["owner"] == binding.owner and entry["generation"] == binding.generation
           for entry in snapshot.executable())

# runtime_state 是此刻可信 runtime.state.value，不是从 command 反推。
service.update_versions(config_revision=config_revision,
                        environment_revision=environment_revision,
                        runtime_state=runtime_state)
# 顺序重要：版本更新可能 revoke；set_gate 会清除 context 并增加 epoch。
# 不得把 gate_open 当作“已经有授权”。若 blocked，这一步会明确失败。
dispatcher.set_gate(True)
dispatcher.update_context(
    ex_session=session_id, goal_id=goal_id, goal_revision=goal_revision,
    task_id=task_id, allowed_actions=[action_id], bound_params={action_id: params},
    runtime_state=runtime_state, catalog_revision=snapshot.revision,
    config_revision=config_revision, environment_revision=environment_revision,
    ttl_ms=context_ttl_ms, observations=observations,
)
command = ActionCommand.parse({
    "schema_version": 1, "command_id": command_id, "ex_session": session_id,
    "goal_id": goal_id, "goal_revision": goal_revision, "decision_id": decision_id,
    "owner": binding.owner, "plugin_generation": binding.generation,
    "action_id": action_id, "operation": "start", "params": params, "lease_ms": lease_ms,
})
admission = service.start(command).result(timeout=5)
current = service.query(command_id).result(timeout=5)
# admission 不是 accepted/running，更不是完成；query None 不是停止证明。
stop_request = service.cancel(command_id, binding, "mock stop").result(timeout=5)
# stop_request 仅确认请求/当前账本状态；需另行核对 committed StopEvidence。
```

实际签名：`astrbot_ex/core/actions/dispatcher.py#ActionDispatcher.update_context`。`allowed_actions` 无重复，`bound_params` 的 key 必须与之完全相同；每个 start 的参数必须等于绑定的 JSON 参数且满足 manifest schema。`observations` 每个 source 为 `{"topic": ..., "fields": {...}, "age_ms": ...}`，与声明 topic/required_fields 匹配；快照采样年龄应由框架提供，Dispatcher 用接收 monotonic 时间加排队时间继续老化。

`set_gate`、`update_versions`、`update_context`、register/remove owner、recovery/review 是框架能力，不暴露给模型或插件。Dispatcher `start` 只接受 operation=start；取消走 `cancel(command_id, binding, reason)`。B00 类型能解析 pause/resume 不代表本 Dispatcher 有这两个执行 API。本树也没有可在本文宣称的 REST commands/query/cancel endpoint。

## 3. manifest v2 与固定 schema 子集

本地 `plugin.json` 示例（仅 Mock，不提供工程控制逻辑）：

```json
{
  "id": "mock_owner", "name": "Mock owner", "version": "1.0.0",
  "entry": "main.py", "provides": ["action_owner"], "enabled_default": false,
  "action_api_version": 2, "observation_guide": "guide.md",
  "actions": [{
    "action_id": "mock_owner.move.v2", "description": "Mock bounded move",
    "schema": {"type": "object", "properties": {
      "meters": {"type": "integer", "minimum": 0, "maximum": 100}
    }, "required": ["meters"], "additionalProperties": false},
    "resources": ["mock_joint"], "operations": ["start", "cancel"],
    "requires_runtime_state": ["running"], "requires_observations": [],
    "max_duration_ms": 1000, "cancel_timeout_ms": 200, "danger": "low"
  }]
}
```

`astrbot_ex/core/local_plugins.py#LocalPluginManager._manifest_from_bytes` 将本地安装字段投影到 `astrbot_ex/core/actions/models.py#parse_action_manifest`；不要直接向严格 action parser 传完整安装 JSON。已有 ROS 端口声明仍走本地 `ros2.ports` 解析/投影，不创建另一 ROS context。

- `action_api_version` 必须为整数 2（不是 bool/2.0）；actions 非空且 ID 唯一，ID 为 owner 命名空间的 `owner.name.vN`。`action_owner` 对应独立 action runtime kind，不借用 motion_bridge。
- resources 是 manifest 声明的互斥资源，不能让模型生成。operations 必须非空且唯一；声明 cancel 必须有正数 `cancel_timeout_ms`，未声明 cancel 不得填该字段。
- runtime state 为 idle/ready/running/paused/fault/finished；danger 为 low/medium/high。需要的 observation source 必须声明 topic、正数 max_age_ms、唯一 required_fields。
- schema 根必须 `type: object`。支持单字符串 type（object/array/string/number/integer/boolean/null），properties、required、additionalProperties（bool 或 schema）、enum、items（单 schema）、min/maxLength、min/maxItems、uniqueItems、minimum/maximum、exclusiveMinimum/Maximum、multipleOf、pattern、format、title、description。
- format 仅 uuid。pattern 最长 256，只允许可选首尾 `^`/`$` 和字面字符子集 `[A-Za-z0-9 _.,:/-]`，不是任意正则。未知关键词（如 `$ref`、oneOf/anyOf、const）拒绝，不默默忽略；无 type 的子 schema 仅支持 enum/描述元数据。矛盾边界拒绝；bool 不是 number/integer。
- JSON/schema/params/details：最大嵌套深度 32、节点 20,000、JSON 字节 1,048,576，额外校验 work budget 20,000；拒绝 cycle、非字符串 object key、NaN/Infinity。ID/短 reason 上限 256；generation/revision 范围 0..2^53−1；lease/context TTL/max_duration/cancel_timeout 上限 600,000ms，要求正整数。命令 lease 不得超 action max_duration。
- guide 使用插件根内相对路径，拒绝绝对/drive/UNC/反斜线/`..` 与 resolve 后 symlink 逃逸；最多读 8193 字节以检测 8192 上限，严格 UTF-8/纯文本，可含正常 Markdown。缺失为 unavailable，违规为 rejected，不进入 executable。

## 4. 短 callback、worker 与合法 report

固定回调 `on_action_command(command)`、`on_action_cancel(command_id, reason)`，不是 payload 指定方法名。start 回调应只验证/准备本地状态，返回 `"accepted"`/`"rejected"`（或带对应 status 的 dict）。进度在有界 `on_worker_step`/受控独立 Mock 执行器推进；停止/drain 中仍能继续 worker，但不能因此恢复新 start。

Actor 默认独立 start 队列 64 条/1MiB、cancel 队列 16 条/64KiB；N+1 明确失败，不覆盖已接收调用。Dispatcher 普通队列默认 512，优先队列独立有界，Ledger writer 默认 128。取消优先只抢占未执行调用，不能杀死阻塞 Python handler。Actor 默认 callback 预算 20ms，Dispatcher 亦检查 20ms；这是故障诊断/关闭授权边界，不是实时抢占保证。

插件正常上报：

```python
from astrbot_ex.core.actions.ledger import StopEvidence

# worker 的一次有界推进；不要在 start/cancel callback 里等待此 Future。
progress = self.context.actions.report(command_id, "running", details={"progress": 0.5})
# 只有 Mock 控制器自身确认 stopped 后，且 Dispatcher 已请求 cancel：
finished = self.context.actions.report(
    command_id, "canceled",
    stop_evidence=StopEvidence(command_id, True, "mock-controller", stop_reference),
)
# 后续 worker 在 future.done() 后检查 future.result()，失败也必须可见。
```

`astrbot_ex/core/actions/plugin_api.py#PluginActionAPI.report` 不接受 owner/generation 参数；二者来自不可重绑的 facade。真实 `astrbot_ex/core/actions/dispatcher.py#ActionDispatcher.report` 仅允许 **running、succeeded、failed、canceled、unknown**。facade 的状态语法检查比 Dispatcher 宽；不能据此让插件上报 admitted/accepted/rejected/**timed_out**。accepted/rejected 来自 start 回调，admitted 来自持久接纳，timed_out 由框架 watchdog 产生。合法状态仍须满足 Ledger 转移关系；终态不可倒退/互相覆盖，同状态重报幂等。

同步提交 report 可以发生，但不要在同一 callback 中 `.result()` 等待终态：部分终态被延后到 callback 结束，且超预算/超时不能靠同步 succeeded 释放资源。绑定检查、乱序、慢 callback 与 Future 异常必须保留可见失败；已集成的 Future/priority 修复仍以协调端实际回归证据为准。

## 5. 幂等、证明、恢复和 rearm

- 原子提交 command 的完整规范化 payload、可信 task_id、manifest resources、初始事件后才调用 actor。相同 command_id/相同 canonical payload/resources/task_id 返回原记录，不重新执行业务 start；任一不同则冲突。重试仍受当前 gate/context/session/revision/deadline 检查，**不是**用旧 ID绕过新授权。
- `ActionSnapshot` 含 status/event_seq/held_resources 等；`query()` 返回 Future，`None` 不是完成。Ledger `events()`/`ack()` 是已提交反馈/outbox入口；`LedgerEvent.to_action_event()` 只对含可信 task/event identity 的记录成立，旧记录不得伪造 task_id。分页不能只看第一批。
- canceled 需要匹配 command_id、stopped=True、非空且有界 source/reference 的结构化 `StopEvidence`。cancel callback 返回、transport ACK、DDS publish、空资源列表或自由文本都不能证明停车。
- unknown/timed_out/failed 是终态但可能尚未停止，保留资源。live 记录用 `dispatcher.reconcile_stop(command_id, binding, evidence)`；重启后不在 live 的旧 owner 由框架用 `reconcile_recovered_stop`。证明与释放持久化，**不把原 unknown/timed_out/failed 改写成 canceled**。
- 重启将 admitted/accepted/running 转为 unknown/resume_review，保留资源；恢复只查询、不重发。旧 generation 的证明按旧记录核对，不能让新插件冒充旧 owner 报告。
- rearm 顺序：撤销新 start → 请求停止 → committed proof/reconcile → `service.await_stop_proof()` 确认（失败诊断不得忽略）→ `dispatcher.review_stops().result(...)` → 重新采样 Catalog/runtime/config/environment → `service.update_versions(...)` → `dispatcher.set_gate(True)` → **安装新 update_context** → 新 command_id。review 不打开 gate，set_gate 会清空 context。Ledger fault/queue saturation 的 admission latch 不能用 review 假清除，需恢复后的 Ledger 和再次核对。
- Registry lifecycle gate 与 Dispatcher execution gate 独立；disable/enable 转 ready 不恢复旧 goal context。停止不确定时保留旧 actor/ROS 实例和 blocked，不抢换代际。

## 6. 默认关闭、回滚与停机核对

### 配置快照与执行事实必须隔离

`astrbot_ex/core/actions/storage.py#prepare_action_ledger` 将权威执行库置于实例 data root 下的 **`execution/actions.sqlite3`**，在 `backup.SNAPSHOT_ROOTS=(profiles, plugins)` 之外。build_server 始终打开该库，配置快照恢复/失败回滚不关闭、替换或恢复它；command/task/event_seq/event_id、资源、stop proof、outbox/ack 是已经发生的事实，不是可回退配置。备份配置 ZIP **不等于执行库灾备**；执行事实需另行停机一致性备份方案，不能删除 execution 目录后用旧配置 snapshot 代替。

兼容旧 `profiles/default/actions.sqlite3`：仅当新执行库尚不存在时，用只读 SQLite connection 的 backup API 复制到独立临时库（含 WAL 中已提交事务），验证 schema/quick_check/外键，fsync 后以同目录硬链接原子、不可覆盖地发布。新实例先在临时库完成 ActionLedger 初始化，再发布；不存在对外可见的空新库。旧主文件不删除、不覆盖；profile 快照可能仍携带旧文件，但新库一旦存在便始终权威，恢复旧 profiles 后重启也不再次导入。开库的既有 ActionLedger 恢复规则仍将 live 状态变为 unknown/resume_review，保留资源与旧事件。

初始化 `.actions-init.lock` 用跨进程 O_EXCL 抢占；并发初始化立即 fail-closed。中断后残留 lock/临时文件需**离线核对**，不自动删除/重导入；完整新库、部分新库和旧库须先辨认，不能靠删目标绕过。已有空/损坏/不完整新库拒绝启动，不回落到旧 profiles。该锁仅保护初始化，不授权同 data root 同时运行多个 runtime；迁移必须在旧实例停止写入、无生产服务并发时执行，backup 含 WAL 的能力不授权热切换活跃旧执行器。文件系统不支持硬链接时初始化可见失败，不退回裸 copy/replace。

原 SnapshotImporter 的根目录替换机制与 ZIP roots 契约不改。恢复前仍 revoke、请求停止、要求 committed proof；未证明的 admitted/running/unknown 阻止恢复并保留资源。恢复后 runtime idle、gate 关闭、新插件零自动 start；不能通过关闭账本再恢复旧库让 Windows WinError5 表面通过。

对应回归：`tests/test_action_storage.py`（WAL、一迁移、新库权威、并发/失败/残留 lock、旧快照恢复重启）；`tests/test_backup.py#SnapshotActionFactsTest`（完成/unknown 事实与 outbox 保留、未证明资源阻止恢复、reload rollback）；`tests/test_b02_real_composition.py#SnapshotRealCompositionTest`（真实 manager/Dispatcher/Actor 运行后安全停止、旧 ID 不再 start）。

通过 controller 切回 legacy 前撤销新 start、请求停止、核对 committed proof 和资源；`change_mode` 失败时不得当回滚成功。切回不会自动启动旧 runtime、恢复旧运动或打开 direct gate。生产部署不属于本轮。

检查 `GET /api/v1/ex/status` 的独立 `actions`：gate_open、blocked、unresolved（含资源）、error、faults、版本。runtime idle 不证明动作停止。`tests/test_action_service_failures.py#ActionServiceFailuresTest` 覆盖 list Future 失败和已列 running 记录随后 get=None：必须 blocked/诊断，后者不得返回停止证明。`tests/test_action_runtime_integration.py` 使用 FakeDispatcher，只证明所覆盖的接线/模式/故障行为，不能代替真实执行链。

## 7. B03/B04 明确缺口：时间与验收

当前 `astrbot_ex/core/actions/dispatcher.py#ActionDispatcher.__init__` **没有 Clock/虚拟时钟注入接口**。ingress lease、context TTL、observation 老化、cancel deadline、callback budget 和 watchdog 全部使用真实 `time.monotonic_ns()`；Service 的等待使用真实 `time.monotonic()`。`watchdog_interval` 不是 time scale。

B03 可让 Mock worker 的 completion 使用模拟时间，但这**不会**推进当前 Dispatcher 的租约/超时，也不会形成虚拟租约验收。若后续要求可控虚拟租约，应单独定义时钟域、watchdog调度/唤醒、边界排序及观测年龄语义，再通过真实执行链验证“推进虚拟时间→拒绝排队过期/可见 timeout/资源保留/无重放”。本轮仅记录确切缺口，不新增 API，也不把该要求标成已解决。

A01–A12、两组真实 Dispatcher campaign、Ledger 序列、崩溃与 native DDS 均需协调端逐项审查日志；静态引用存在性或先前局部 3+39 项通过不是完整 B02 验收。
