# B11 upper control acceptance report

- scope: `all`
- verdict: **wiring_pass**
- started: 2026-11-03T21:44:46+0800
- finished: 2026-11-03T21:47:04+0800
- duration: 137.255s

## Stage matrix

| Stage | Status | Gating | Reason | Commands |
|---|---|---|---|---|
| unit_aeb | PASS | yes | - | 11 |
| unit_ex | PASS | yes | - | 3 |
| loopback_aeb | PASS | yes | - | 1 |
| loopback_ex | PASS | yes | - | 1 |
| host | PASS | yes | - | 4 |
| page_spec | PASS | yes | - | 1 |
| legacy_regression | PASS | yes | - | 15 |

## Commands

| # | Command | CWD | Exit | Tests | Skipped | Status | Log |
|---|---|---|---|---|---|---|---|
| 1 | `aeb:test_upper_contracts` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 26 | 0 | PASS | `logs\001-aeb_test_upper_contracts.log` |
| 2 | `aeb:test_upper_settings` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 23 | 0 | PASS | `logs\002-aeb_test_upper_settings.log` |
| 3 | `aeb:test_action_catalog` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 35 | 0 | PASS | `logs\003-aeb_test_action_catalog.log` |
| 4 | `aeb:test_upper_prompts` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 36 | 0 | PASS | `logs\004-aeb_test_upper_prompts.log` |
| 5 | `aeb:test_upper_models` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 45 | 0 | PASS | `logs\005-aeb_test_upper_models.log` |
| 6 | `aeb:test_upper_store` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 19 | 0 | PASS | `logs\006-aeb_test_upper_store.log` |
| 7 | `aeb:test_upper_pipeline` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 22 | 0 | PASS | `logs\007-aeb_test_upper_pipeline.log` |
| 8 | `aeb:test_upper_output_isolation` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 12 | 0 | PASS | `logs\008-aeb_test_upper_output_isolation.log` |
| 9 | `aeb:test_control_channel` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 19 | 0 | PASS | `logs\009-aeb_test_control_channel.log` |
| 10 | `aeb:test_upper_composition` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 37 | 0 | PASS | `logs\010-aeb_test_upper_composition.log` |
| 11 | `aeb:test_upper_verify_runner` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 11 | 0 | PASS | `logs\011-aeb_test_upper_verify_runner.log` |
| 12 | `ex:test_step_input_contracts` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 24 | 0 | PASS | `logs\012-ex_test_step_input_contracts.log` |
| 13 | `ex:test_step_input` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 51 | 0 | PASS | `logs\013-ex_test_step_input.log` |
| 14 | `ex:test_step_input_integration` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 21 | 0 | PASS | `logs\014-ex_test_step_input_integration.log` |
| 15 | `aeb:test_upper_e2e` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 19 | 0 | PASS | `logs\015-aeb_test_upper_e2e.log` |
| 16 | `ex:test_upper_control_e2e` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 12 | 0 | PASS | `logs\016-ex_test_upper_control_e2e.log` |
| 17 | `host:composition` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 37 | 0 | PASS | `logs\017-host_composition.log` |
| 18 | `host:test_upper_api` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 50 | 0 | PASS | `logs\018-host_test_upper_api.log` |
| 19 | `host:test_upper_acceptance` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 7 | 0 | PASS | `logs\019-host_test_upper_acceptance.log` |
| 20 | `host:test_upper_crash_recovery` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 4 | 0 | PASS | `logs\020-host_test_upper_crash_recovery.log` |
| 21 | `page:upper_control` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 2 | 0 | PASS | `logs\021-page_upper_control.log` |
| 22 | `legacy:aeb:test_plugin_import` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 1 | 0 | PASS | `logs\022-legacy_aeb_test_plugin_import.log` |
| 23 | `legacy:aeb:test_plugin_channels` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 5 | 0 | PASS | `logs\023-legacy_aeb_test_plugin_channels.log` |
| 24 | `legacy:aeb:test_zmq_transport` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 2 | 0 | PASS | `logs\024-legacy_aeb_test_zmq_transport.log` |
| 25 | `legacy:aeb:test_task_contracts` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 18 | 0 | PASS | `logs\025-legacy_aeb_test_task_contracts.log` |
| 26 | `legacy:aeb:test_task_store` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 6 | 0 | PASS | `logs\026-legacy_aeb_test_task_store.log` |
| 27 | `legacy:aeb:test_public_output_router` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 5 | 0 | PASS | `logs\027-legacy_aeb_test_public_output_router.log` |
| 28 | `legacy:aeb:test_output_isolation` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 3 | 0 | PASS | `logs\028-legacy_aeb_test_output_isolation.log` |
| 29 | `legacy:aeb:test_host_foundation` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 9 | 0 | PASS | `logs\029-legacy_aeb_test_host_foundation.log` |
| 30 | `legacy:aeb:test_host_chat_routing` | `D:\Code\A.E.B-work\upper-b10-b11` | 0 | 13 | 0 | PASS | `logs\030-legacy_aeb_test_host_chat_routing.log` |
| 31 | `legacy:ex:test_connection_manager` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 15 | 0 | PASS | `logs\031-legacy_ex_test_connection_manager.log` |
| 32 | `legacy:ex:test_jev_backend` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 47 | 0 | PASS | `logs\032-legacy_ex_test_jev_backend.log` |
| 33 | `legacy:ex:test_decision_service` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 57 | 0 | PASS | `logs\033-legacy_ex_test_decision_service.log` |
| 34 | `legacy:ex:test_decision_backend_contract` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 4 | 0 | PASS | `logs\034-legacy_ex_test_decision_backend_contract.log` |
| 35 | `legacy:ex:test_decision_transport` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 4 | 0 | PASS | `logs\035-legacy_ex_test_decision_transport.log` |
| 36 | `legacy:ex:test_decision_management_interleaving` | `D:\Code\AstrBotEX-work\upper-b10-b11` | 0 | 3 | 0 | PASS | `logs\036-legacy_ex_test_decision_management_interleaving.log` |

## Scope and limits

- `unit_fake`: module/substitute level, no real Host or socket.
- `real_zmq_loopback`: real ROUTER/DEALER on temporary ports.
- `backend_request_capture`: the actual backend request body, captured locally.
- `host`: Python composition/API tests using Host libraries; these do not start a Dashboard.
- `page_spec`: mandatory browser check; all/host scopes require a real Dashboard iframe and bridge.
- A stage is PASS only when every command is PASS and no dependency was skipped.
- No stage here proves execution completion, uplink feedback, ROS motion or a live model.

## Notes

- page_mode=real_host
- baseline=C:\Users\17088\AstrBot-upper-review\20261103-fix01\b11-final-003\legacy_regression.json new_failures=0
