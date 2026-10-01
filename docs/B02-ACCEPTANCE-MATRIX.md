# B02 验收矩阵与 B03/B04 交接入口

更新时间：2026-09-30；对象为 ex-integration 当前工作树，**未最终验收**。协调端已集成 Future/priority、千条分页与 B03 runnable；独立全回归报告 343 passed、1 failed（原 HTTP snapshot restore）、5 Windows ROS skips、423 subtests，169.22s，两组 Dispatcher 1000 campaign 均通过。本轮只修 backup/storage/composition，聚焦回归与源 hash/diff 见 `task-evidence/2026-09-30/B02-snapshot-storage/`；不重跑全套/campaign/DDS/SSH，也不将局部通过写成最终验收。

本表按本树 A 前缀测试及配套组件测试给出 A01–A12 **覆盖映射**，不是“函数存在即验收通过”。A07/A08/A11 在本树没有同名 `test_a07/test_a08/test_a11`，明确映射到已有具体函数，不创造测试名。外部 `B02-协调与验收补充.md` 要求原计划全部必跑及补充边界；最终关闭需协调端核对原计划与实际日志。

## 使用方法与证据要求

以下命令均从分配仓库根执行，用 `python -m unittest discover -s tests -p <file> -k <完整函数名> -v` 精确选例。表中命令是**复现入口，非本轮执行记录**；每次验收保存真实 stdout、stderr、exit、argv/cwd，并核对实际 Ran 数和 skip。缺 ROS/custom package 的 skip 不计 native 成功。任何失败/未跑保留，不删、skip 或降低断言。

“真实 Dispatcher”指本树 ActionDispatcher + PluginActor + SQLite Ledger，末端仍是无硬件 Mock controller。MockDispatcher/FakeDispatcher 测试只能证明局部调用/接线，不能替代事务、执行前 guard、重复 start、恢复与真实队列竞争。

## A01–A12 逐行映射

| ID / 验收关注 | 本树确实存在的函数 | 可复现命令（需另行审批运行） | 覆盖边界 / 不可替代项 |
|---|---|---|---|
| A01：两 owner 各 100 次，无串发 | `tests/test_action_dispatcher.py#DispatcherTests.test_a01_two_owners_100_each_isolated` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a01_two_owners_100_each_isolated -v` | 真实 Dispatcher/Actor/Ledger 检查各 owner 次数及归属；MockDispatcher 无法替代。物理末端是 Mock。 |
| A02：同 ID 十次重试不再 start，异 payload 冲突 | `tests/test_action_dispatcher.py#DispatcherTests.test_a02_retry_ten_times_and_conflict`；`tests/test_action_ledger.py#LedgerTests.test_duplicate_ten_retries_and_changed_payload_or_reservation_conflict` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a02_retry_ten_times_and_conflict -v`；`python -m unittest discover -s tests -p test_action_ledger.py -k test_duplicate_ten_retries_and_changed_payload_or_reservation_conflict -v` | 同时需要业务 start 计数与 durable canonical/resources 冲突；纯幂等模型不能代替真实 Ledger。 |
| A03：有界队列 N+1、超字节，前 N 条不丢 | `tests/test_action_dispatcher.py#DispatcherTests.test_a03_actor_queue_full_and_oversize`；`tests/test_plugin_action_mailbox.py#ActionMailboxTest.test_count_limit_keeps_existing_starts`；`tests/test_plugin_action_mailbox.py#ActionMailboxTest.test_byte_limits_on_both_queues` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a03_actor_queue_full_and_oversize -v`；`python -m unittest discover -s tests -p test_plugin_action_mailbox.py -k test_count_limit_keeps_existing_starts -k test_byte_limits_on_both_queues -v` | 前者验证 ledger 可见 actor_busy 与已有队列执行；后者验证实际 Actor 普通/cancel 容量。无界 cast 不是动作队列替身。 |
| A04：并发资源互斥，重启后 uncertain 仍持有 | `tests/test_action_dispatcher.py#DispatcherTests.test_a04_concurrent_resource_exclusion`；`tests/test_action_ledger.py#LedgerTests.test_concurrent_exclusion_persists_across_restart_and_reconciliation` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a04_concurrent_resource_exclusion -v`；`python -m unittest discover -s tests -p test_action_ledger.py -k test_concurrent_exclusion_persists_across_restart_and_reconciliation -v` | 需真实并发与 SQLite 持久资源；内存 dict 锁不证明恢复语义。 |
| A05：阻塞 handler、取消超时、独立 Mock watchdog、rearm | `tests/test_action_dispatcher.py#DispatcherTests.test_a05_blocked_handler_cancel_timeout_and_independent_watchdog`；`tests/test_action_dispatcher.py#DispatcherTests.test_independent_physical_lease_with_control_worker_blocked` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a05_blocked_handler_cancel_timeout_and_independent_watchdog -k test_independent_physical_lease_with_control_worker_blocked -v` | 真正阻塞 Actor/控制 worker，核对 blocked/资源/证明/review。所谓 physical watchdog 是 Mock 内独立线程，**不是实际物理停车证明**。 |
| A06：同步 report、乱序/终态不可回退 | `tests/test_action_dispatcher.py#DispatcherTests.test_a06_sync_report_and_out_of_order`；`tests/test_action_ledger.py#LedgerTests.test_binding_and_out_of_order_or_duplicate_terminal` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a06_sync_report_and_out_of_order -v`；`python -m unittest discover -s tests -p test_action_ledger.py -k test_binding_and_out_of_order_or_duplicate_terminal -v` | 必须真实 deferred report 与事务状态机；SDK callback stub 只检查 facade。慢 callback/未返回 callback 的终态资源检查还需下节附加项。 |
| A07：生命周期撤销、停止推进、旧实例/新 context 隔离 | `tests/test_plugin_registry_actions.py#PluginRegistryActionTest.test_blocked_unload_retains_actor_ros_and_allows_proof_retry`；`tests/test_plugin_registry_actions.py#PluginRegistryActionTest.test_disable_retries_and_cancel_drain_with_runtime_stopped`；`tests/test_b02_real_composition.py#RealCompositionActionTest.test_disable_enable_requires_fresh_context_and_old_command_does_not_resume`；`tests/test_local_plugin_action_lifecycle.py#LocalActionLifecycleTest.test_blocked_config_disable_and_uninstall_preserve_owner` | `python -m unittest discover -s tests -p test_plugin_registry_actions.py -v`；`python -m unittest discover -s tests -p test_b02_real_composition.py -v`；`python -m unittest discover -s tests -p test_local_plugin_action_lifecycle.py -v` | registry 真实 Actor、MockRos；LocalActionLifecycle 使用 MockDispatcher，proof guard 受测试操控。必须同时保留真实 LocalPluginManager+Dispatcher 组合，不能拿 mock 配置 reload 测试推断所有真实停止边界通过。 |
| A08：Catalog 一致版本、禁用/guide/目录漂移不可执行 | `tests/test_capability_catalog.py#CapabilityCatalogTest.test_atomic_revisions_and_detached_copies`；`tests/test_local_plugin_action_lifecycle.py#LocalActionLifecycleTest.test_disable_enable_and_document_revision`；`tests/test_local_plugin_action_lifecycle.py#LocalActionLifecycleTest.test_starting_and_directory_drift_are_not_executable`；`tests/test_local_plugins_config.py#LocalPluginConfigTest.test_guide_bytes_and_paths` | `python -m unittest discover -s tests -p test_capability_catalog.py -v`；`python -m unittest discover -s tests -p test_local_plugin_action_lifecycle.py -k test_disable_enable_and_document_revision -k test_starting_and_directory_drift_are_not_executable -v`；`python -m unittest discover -s tests -p test_local_plugins_config.py -k test_guide_bytes_and_paths -v` | 真实 Catalog/目录读取；manager 局部测试含 MockDispatcher。catalog executable 不是 goal grant；版本刷新后真实 queued start 撤销另见 A10。Windows symlink 条件分支须另在支持 symlink 的环境核验。 |
| A09：真实子进程四崩溃边界，无 replay | `tests/test_action_dispatcher.py#DispatcherTests.test_a09_real_dispatcher_actor_crash_boundaries_no_replay`；`tests/test_action_ledger.py#LedgerTests.test_real_process_crash_boundaries` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a09_real_dispatcher_actor_crash_boundaries_no_replay -v`；`python -m unittest discover -s tests -p test_action_ledger.py -k test_real_process_crash_boundaries -v` | 必须父测试核对重启库、事件/资源、业务 start marker；helper exit=0 是故意崩溃，不能单凭其 exit 判通过。详细边界见下表。 |
| A10：排队过期、观测老化、context/版本撤销 | `tests/test_action_dispatcher.py#DispatcherTests.test_a10_expired_while_actor_queued_and_observation_ages`；`tests/test_action_dispatcher.py#DispatcherTests.test_context_ttl_expires_while_actor_queued`；`tests/test_action_dispatcher.py#DispatcherTests.test_version_update_revokes_queued_start`；`tests/test_action_dispatcher.py#DispatcherTests.test_lease_measured_from_ingress_not_writer_commit` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a10_expired_while_actor_queued_and_observation_ages -k test_context_ttl_expires_while_actor_queued -k test_version_update_revokes_queued_start -k test_lease_measured_from_ingress_not_writer_commit -v` | 真实执行前 guard；所有时钟为 real monotonic。本组不覆盖 B03 虚拟租约；不可仅用 mock completion 对外声称满足。 |
| A11：不走 TopicBus 动作反馈、慢诊断不锁住安全路径 | `tests/test_plugin_action_mailbox.py#ActionMailboxTest.test_raising_diagnostic_subscriber_cannot_strand_actions`；`tests/test_plugin_action_mailbox.py#ActionMailboxTest.test_slow_diagnostic_subscriber_does_not_block_action_results`；`tests/test_action_dispatcher.py#DispatcherTests.test_emergency_callback_not_under_gate_lock_and_failure_visible`；`tests/test_topic_bus_inbox.py#TopicBusInboxTest.test_inbox_is_bounded_and_keeps_latest_message` | `python -m unittest discover -s tests -p test_plugin_action_mailbox.py -k test_raising_diagnostic_subscriber_cannot_strand_actions -k test_slow_diagnostic_subscriber_does_not_block_action_results -v`；`python -m unittest discover -s tests -p test_action_dispatcher.py -k test_emergency_callback_not_under_gate_lock_and_failure_visible -v`；`python -m unittest discover -s tests -p test_topic_bus_inbox.py -v` | 慢订阅者用 **EventBus 诊断**，TopicBus 用有界 inbox；不是持续 TopicBus 洪水+真实 Dispatcher+生命周期 gate 同时压力测试。该联合覆盖缺口需单独补验，不能把四个局部测试拼成已满足全部洪水要求。 |
| A12：固定 schema/预算、不支持关键词拒绝 | `tests/test_action_dispatcher.py#DispatcherTests.test_a12_schema_extra_range_nan_depth_and_unsupported`；`tests/test_local_plugins_config.py#LocalPluginConfigTest.test_v2_manifest_is_not_legacy_topic_action` | `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_a12_schema_extra_range_nan_depth_and_unsupported -v`；`python -m unittest discover -s tests -p test_local_plugins_config.py -k test_v2_manifest_is_not_legacy_topic_action -v` | 真实 admission 前拒绝额外字段、超范围、bool、NaN、过深、oneOf；v1/v2 隔离不因兼容 topic 元数据变成 v2 控制。B00 完整边界仍由冻结契约套件验收，不改 contracts。 |

## 必须保留的补充验收入口

以下是真实函数，不宣称本轮结果。精确选例命令同上；对同文件可用多个 `-k`（unittest 按 OR 选择）：

- 真实安装插件执行/取消/证据释放：`tests/test_b02_real_composition.py#RealCompositionActionTest.test_real_owner_start_cancel_proof_and_resource_release`；命令 `python -m unittest discover -s tests -p test_b02_real_composition.py -v`。插件仍是临时目录 Mock，无硬件。
- Service list Future 失败/缺 running 记录 fail-closed：`tests/test_action_service_failures.py#ActionServiceFailuresTest.test_status_returns_blocked_diagnostic_when_list_future_fails`、`tests/test_action_service_failures.py#ActionServiceFailuresTest.test_missing_running_record_is_not_stop_proof`、`tests/test_action_service_failures.py#ActionServiceFailuresTest.test_missing_running_record_retains_resource_uncertainty`；命令 `python -m unittest discover -s tests -p test_action_service_failures.py -v`。真实 Ledger + FakeDispatcher；不是 Dispatcher 全故障验收。
- runtime 模式/停止/环境失败：`tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_controller_revokes_gate_before_waiting_for_tick_lock`、`tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_environment_switch_failure_retains_old_adapter`、`tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_unknown_with_no_resources_remains_blocked_until_committed_proof`、`tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_decision_mode_suppresses_legacy_start_worker_and_tick`、`tests/test_action_runtime_integration.py#ActionRuntimeIntegrationTest.test_bridge_rejects_existing_context_after_mode_change`；命令 `python -m unittest discover -s tests -p test_action_runtime_integration.py -v`。这些使用 FakeDispatcher，不能证明 pause/fault/restore 每条真实执行链均已测通。
- SDK binding/revoke/证据校验：`tests/test_plugin_action_api.py#PluginActionAPITest.test_closed_bind_once_and_revoke`、`tests/test_plugin_action_api.py#PluginActionAPITest.test_dispatcher_callback_rejects_stale_generation`、`tests/test_plugin_action_api.py#PluginActionAPITest.test_evidence_and_payload_cannot_be_shortcuts`；命令 `python -m unittest discover -s tests -p test_plugin_action_api.py -v`。callback stub 不能覆盖真实 Dispatcher 的 report 合法状态过滤；实际只允许 running/succeeded/failed/canceled/unknown，**timed_out 不允许插件冒充**。
- 超预算/未返回 callback 不可用 succeeded 释放资源：`tests/test_action_dispatcher.py#DispatcherTests.test_pending_report_fails_if_handler_never_returns_before_timeout`、`tests/test_action_dispatcher.py#DispatcherTests.test_sync_success_report_cannot_release_resource_after_slow_callback`、`tests/test_action_dispatcher.py#DispatcherTests.test_rejected_callback_wins_over_sync_success_report`；命令 `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_pending_report_fails_if_handler_never_returns_before_timeout -k test_sync_success_report_cannot_release_resource_after_slow_callback -k test_rejected_callback_wins_over_sync_success_report -v`。
- priority 饱和/取消 Future：`tests/test_action_dispatcher.py#DispatcherTests.test_priority_saturation_retries_timeout_until_durable`、`tests/test_action_dispatcher.py#DispatcherTests.test_cancel_queued_future_does_not_kill_worker`；命令 `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_priority_saturation_retries_timeout_until_durable -k test_cancel_queued_future_does_not_kill_worker -v`。已由协调端集成并独立回归；仍以原始结果而非接口文档推断 race 正确性。
- 真 SQLite 故障、已提交事件/outbox：`tests/test_action_ledger.py#LedgerTests.test_real_sqlite_write_failure_blocks_admission`、`tests/test_action_ledger.py#LedgerTests.test_committed_only_events_ack_and_immutable_snapshots`；命令 `python -m unittest discover -s tests -p test_action_ledger.py -k test_real_sqlite_write_failure_blocks_admission -k test_committed_only_events_ack_and_immutable_snapshots -v`。

## 1000 条 campaign：三条独立命令

**仅列入口，本轮未执行。** 不用 Ledger-only 序列替代真实 Dispatcher campaign，也不把 Mock 控制器叫作真实设备。

1. `tests/test_action_dispatcher.py#DispatcherTests.test_1000_real_dispatcher_sequences_two_owners_no_duplicate_start`

   `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_1000_real_dispatcher_sequences_two_owners_no_duplicate_start -v`

   seed=20260929；真实 Dispatcher/Actor/Ledger，两 owner 共 1000 个命令，含重试；检查业务 start 总数/唯一性/归属。这条不等价于完整随机状态机。

2. `tests/test_action_dispatcher.py#DispatcherTests.test_1000_real_dispatcher_state_sequences_with_resources_and_rearm`

   `python -m unittest discover -s tests -p test_action_dispatcher.py -k test_1000_real_dispatcher_state_sequences_with_resources_and_rearm -v`

   seed=20260929 XOR 0x5A5A；1000 个命令，shared 资源、错误 owner、重复 running、succeeded/failed/unknown/cancel 四分支、终态拒绝倒退、uncertain 持锁、proof/review/gate+context rearm。

3. `tests/test_action_ledger.py#LedgerTests.test_pagination_and_one_thousand_seeded_status_sequences`

   `python -m unittest discover -s tests -p test_action_ledger.py -k test_pagination_and_one_thousand_seeded_status_sequences -v`

   seed=20260928；真实 SQLite，1000 个 command 的随机状态序列，错误转移原记录不变、uncertain 持锁、分页。这条没有 Dispatcher/Actor，不证明无重复业务 start。Ledger 可以测试框架 timed_out 转移，不能据此授予插件 timed_out report 权限。

## 四崩溃边界：分别验收 Dispatcher 链与 Ledger

父测试命令即 A09 两条；它们调用下列真实 helper 并自行创建隔离临时库、检查重启结果。不要把 helper 当普通 app 启动，不指向现有数据库。

| 边界 | Dispatcher helper `tests/action_dispatcher_crash_helper.py#main` 的参数 | Ledger helper `tests/action_ledger_crash_helper.py` 的参数 | 父测试必须核对 |
|---|---|---|---|
| 接纳前 | before_admission | before_admission | 无 command/resource/event，无业务 start。 |
| 接纳事务内未 commit | inside_admission_transaction | during_admission_transaction | 事务回滚；无可见接纳、资源与事件，无业务 start。 |
| commit 后、下发前 | after_commit_before_delivery | after_commit_before_delivery | 重启精确 unknown/resume_review，保留资源，无业务 start；恢复不得补发。 |
| 业务已 start、终态 commit 前 | after_business_start_before_terminal | running_before_terminal_commit | marker 恰 1；恢复仍 unknown/resume_review 与资源保留，不增加 start；显式证明/review 后也不重放旧 ID。 |

Dispatcher helper 真正调用 Actor 业务回调并写 marker；Ledger helper 最后一边界自行模拟 side-effect marker。两者都需要，后者不能替代前者。`os._exit(0)` 是边界探针设计，不是正常完成；父测试 assertions 和所有 boundary 子断言才是验收证据。

## nativeDDS5 与 native action lifecycle：单独统计

本轮不执行 ROS、网络或 SSH。以下入口仅供**已授权的隔离 ROS 环境**后续复现：已 source ROS2/Humble 和自定义接口，domain 73、随机 namespace、测试临时数据，无生产节点/服务/设备。nativeDDS5 是下列现有五个测试的统计名称，**不是本树脚本名/API**。

`tests/test_ros2_integration.py#NativeRosIntegrationTest` 包含：

1. `tests/test_ros2_integration.py#NativeRosIntegrationTest.test_external_process_late_publisher_dual_owners_and_bidirectional`
2. `tests/test_ros2_integration.py#NativeRosIntegrationTest.test_qos_diagnostics_and_reconfiguration_recover`
3. `tests/test_ros2_integration.py#NativeRosIntegrationTest.test_twenty_switches_release_threads_and_discard_queued_messages`
4. `tests/test_ros2_integration.py#NativeRosIntegrationTest.test_high_rate_slow_consumer_stays_bounded`
5. `tests/test_ros2_integration.py#NativeRosIntegrationTest.test_custom_nested_type_roundtrip_and_missing_package_isolated`

在已 source 的隔离 Linux ROS 环境、仓库根（**本轮不运行**）：

```sh
ROS_DOMAIN_ID=73 ASTRBOTEX_TEST_ROS_DOMAIN_ID=73 python -m unittest discover -s tests -p test_ros2_integration.py -v
ROS_DOMAIN_ID=73 PYTHONPATH=. python tests/native_action_lifecycle_check.py
```

DDS5 验收必须实跑五项且无 skip；本树无 rclpy 时整类 skip，缺 `astrbotex_demo_interfaces` 时 custom 项 skip，不能计为 5/5。source 的构建产物/隔离环境准备由后续验收另行授权，本轮不给硬件或部署命令。

`tests/native_action_lifecycle_check.py#main` 非 test_ 自动发现；严格 import rclpy、domain=73，无 fallback/skip。真实 Dispatcher/Actor/Ledger + SimulatedController 发布 DDS stop 消息，并核对收到消息、Mock moving→stopped、canceled、资源释放。控制器是在发布后本地修改模拟状态并提交 proof；**不是设备反馈到来后物理停车**。DDS5 只证明隔离 DDS/端口生命周期；两类结果均不能替代真实物理安全验证，亦不授权本项目实施工程插件。

## 快照/执行存储隔离专项（2026-09-30）

权威 `execution/actions.sqlite3` 在配置 snapshot roots 外；`profiles/default/actions.sqlite3` 只作首次一致性迁移来源。配置根替换契约不改，旧库不删除，新库不被旧 snapshot 回退，停止未证明时 restore 必须失败。执行库不包含在配置 ZIP，不能用配置 snapshot 作为执行事实灾备。

| 专项 | 真实测试函数 | 聚焦复现命令 |
|---|---|---|
| 原 HTTP create/download/upload/restore，不改原断言 | `tests/test_backup.py#SnapshotHttpApiTest.test_create_download_and_upload_restore_over_http` | `python -m unittest discover -s tests -p test_backup.py -k test_create_download_and_upload_restore_over_http -v` |
| 完成/unknown 的 command/task/event/outbox/proof 不回退，保留原打开 ledger | `tests/test_backup.py#SnapshotActionFactsTest.test_restore_keeps_post_snapshot_execution_facts_and_open_ledger` | `python -m unittest discover -s tests -p test_backup.py -v` |
| admitted/running/unknown 无 proof 阻止 restore，持有资源不丢；失败 reload 回滚只动配置 | `tests/test_backup.py#SnapshotActionFactsTest.test_unproven_admitted_running_and_unknown_restore_fail_closed_without_losing_resources`；`tests/test_backup.py#SnapshotActionFactsTest.test_reload_rollback_does_not_rollback_execution_facts` | 同上，必须保留失败前后事实核对 |
| 真正已提交 WAL 迁移、原文件保留、只迁一次 | `tests/test_action_storage.py#ActionStorageTest.test_wal_committed_facts_migrate_once_and_original_is_retained` | `python -m unittest discover -s tests -p test_action_storage.py -v` |
| 新库权威、旧 profiles snapshot 恢复后重启不重导入/不释放 uncertain 资源 | `tests/test_action_storage.py#ActionStorageTest.test_existing_execution_database_wins_over_older_profile`；`tests/test_action_storage.py#ActionStorageTest.test_restored_old_profile_cannot_replace_newer_execution_facts_on_restart`；`tests/test_action_storage.py#ActionStorageTest.test_live_legacy_commands_are_copied_then_recovered_without_release` | 同上，真实 SQLite backup，不 mock copy |
| 并发、部分/空/损坏库、发布失败、残留 lock fail-closed | `tests/test_action_storage.py#ActionStorageTest.test_concurrent_initializer_fails_closed_and_only_complete_target_is_published`；`tests/test_action_storage.py#ActionStorageTest.test_incomplete_sqlite_target_is_not_treated_as_initialized`；`tests/test_action_storage.py#ActionStorageTest.test_empty_or_invalid_target_fails_closed_without_legacy_fallback`；`tests/test_action_storage.py#ActionStorageTest.test_failed_publication_leaves_no_empty_target_and_retry_migrates`；`tests/test_action_storage.py#ActionStorageTest.test_interrupted_initialization_lock_requires_offline_review` | 同上；中断残留状态不自动清除，离线核对后再启动 |
| 真实 manager/Dispatcher/Actor，snapshot 后运行/unknown/完成事实保留、停止 proof、restore 后关 gate/零自动动作/旧 ID 不再 start | `tests/test_b02_real_composition.py#SnapshotRealCompositionTest.test_restore_stops_live_owner_but_never_replays_or_replaces_execution_facts` | `python -m unittest discover -s tests -p test_b02_real_composition.py -v` |

原 HTTP 用例本轮修前单独复现 HTTP400/WinError5；修后仍使用同一原用例。所有原始 stdout/stderr/exit、首次新增实现 Windows fsync 失败与后续修正日志、before/after sourcehash 和相对本轮基线 diff 均保留在专项 evidence。没有通过关闭 Ledger 或恢复旧数据库绕过目录权限错误。

白名单外 `tests/test_action_runtime_integration.py` 的 `test_build_server_wires_persistent_components_and_closes` 仍断言旧 profile 库路径；本轮不修改该测试，需协调端跟进到 `execution/actions.sqlite3` 并保持持久组件/关闭断言。完整回归尚不能据本轮聚焦结果宣称完成。

## B03/B04 的交接条件与未关闭项

- 按 `docs/B02-ACTION-SDK.md` 使用框架绑定 generation、受信版本/goal context/gate；B04 的 goal replacement 必须等待旧 command stop proof，不从 runtime idle 或 transport ACK 推断停止。
- **虚拟租约缺口**：`astrbot_ex/core/actions/dispatcher.py#ActionDispatcher.__init__` 不接收虚拟 Clock；start ingress、context TTL、观测年龄、cancel deadline、callback budget、watchdog 都是 real monotonic。B03 可以实现虚拟 completion 的 Mock，但其完成时间不驱动当前 Dispatcher 租约。时钟接口/统一时钟域/可控 watchdog 排程及虚拟超时边界须单独设计、实现和验收；不得写“已解决”。
- A11 的持续 TopicBus 洪水与真实安全 gate 联合压力覆盖仍需核对/补验；现有慢诊断测试为 EventBus，不能偷换概念。
- pause/fault/restore/环境切换/配置重载的真实动作执行链及停止 token 验收不能全由 FakeDispatcher/MockDispatcher 证明；保留已存在局部测试与真实组合入口，未覆盖的联合链由协调端明确安排。
- Dispatcher Future/priority 变更已由协调端集成并独立跑过含两 campaign 的全回归。本轮存储隔离之后仍需协调端复核相关整套结果，局部/静态检查不替代完整行为验收。
- 完整 EX 回归、两组 Dispatcher campaign、Ledger 序列、四崩溃边界、nativeDDS5/native lifecycle 的最新原始证据及 skip 统计由协调端验收；本轮不补跑、不编造结果、不写“全部通过”。
