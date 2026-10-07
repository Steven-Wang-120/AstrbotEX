# B08 管理：生命周期与验证边界

管理实现见 [DecisionManagement](../astrbot_ex/core/decision/management.py)，配置和归属分别见 [config.py](../astrbot_ex/core/decision/config.py)、[owned_laya.py](../astrbot_ex/core/decision/owned_laya.py)。复用 DecisionService、RuntimeController、原 Action/Actor/Ledger，不另造执行器。接口和秘密边界集中于 [管理说明](B08-DECISION-MANAGEMENT.md)。

## 保存、probe 与激活

schema v2 保存受 session/revision CAS 保护，saved 与 effective 分离；保存不连接/推理、启动 runtime、切模式或创建 Goal。key keep 不推进 CAS，set/clear 发布新引用；临时 probe key 不落盘。固定 wait-only `/test` 使用生产 decoder，不提交业务 Goal/Action，不保存或生效配置，不解除隔离。连接测试不替代模型选择质量。

execute 应用所选供应商配置，经 RuntimeController 切 decision control mode、启动 runtime、启用生产后端 gate；只有 owned 才启动或复用受 guard 的自有代次。无 Goal 时动作 gate 仍关闭，不产生任务。失败撤授权，不回退 legacy。

## 完整停止与 owned 凭据

停止先撤销执行并取得匹配 operation_id 的框架证明，再停止 runtime；同时核对 registry 的 stop_error/state/runtime_started/runtime_starting/stop_proven 与生命周期 fault 事件，不能仅凭 runtime IDLE 判成功。证明不足显示 uncertain、禁止重启，后续真实 Stop 成功才能解除。runtime 关闭失败仍尝试结束自有进程；external 服务不被认领或 kill。框架组件证明不是硬件停车证明。

生命周期按 apply→service 顺序串行，恢复的冷启动先释放 service 锁再取 apply→service；意图和 manager/generation fencing 防止旧清理杀掉已接管的新代次。manager 捕获 key 引用和值，确认旧 generation 已退出、请求空闲且无 quarantine/ownership_unknown 后才能重建。换 key 不清隔离，旧 Popen 留存也不等于进程仍存活。

## 持久化与证据限制

同目录暂存、文件 fsync、immutable key no-clobber、回滚/恢复 marker 与 POSIX 目录 fsync 共同保护配置发布。storage_write_uncertain 保留恢复 fence 并阻止后续写入/映射；secret_cleanup_failed 可能在 CAS 已提交后发生，必须刷新而非盲重试。Windows 的 fd/fchmod 与 `.exe` 路径不构成 Windows ACL、POSIX 目录持久性或整树/GPU 停止证明。旧 WebSocket token 的连接 GET/快照存储风险仍独立存在。

请求追踪区分实际 body/hash、POST 尝试、本地 socket 写出、响应、模型选择、EX admitted/discarded 与 Ledger 状态。本地写出不证明远端完成，缺正文不得按快照重建；有界历史/裁剪不是永久审计。测试 Actor 的 accepted/succeeded 不是机器人 ACK；合成 HTTP 与 FakeProcess 不能冒充真实模型/进程效果。

## 复验入口

从仓库根，用临时实例运行确定性管理测试：

```sh
python -B -m unittest tests.test_decision_management_boundaries tests.test_decision_management_history tests.test_decision_management_http tests.test_decision_management_interleaving tests.test_decision_management_operations tests.test_owned_laya tests.test_decision_secret_redaction -v
# 以下独立脚本从 AstrBotVLA-tests 根运行（产品单元测试仍在 EX）：
python -B -m validation_drivers.verify_decision_management --ex-checkout ../ex --aeb-checkout ../aeb --help
python -B -m validation_drivers.verify_decision_management --ex-checkout ../ex --aeb-checkout ../aeb --output /absolute/fresh-external-evidence
```

生命周期专项为 `tests/test_decision_management_lifecycle.py`、`test_decision_management_activation.py`、`test_decision_management_provider_http.py`、`test_decision_management_projection.py`。浏览器的合同替身与实际 HTTP 两类入口见 [页面说明](B09_DECISION_UI_RESULT.md)。上述验证入口是 synthetic-only，装配真实管理 HTTP/RuntimeController、隔离软件 Actor 与合成 loopback owned 服务，不调用模型/ROS/机器人；旧 `--python/--cache/--device` 参数不适用。真实模型另用 [Laya 验证入口](B07-LAYA-BACKEND.md#3-独立部署与验证用法)和独立环境/固定缓存。输出使用新空外部目录，不写 docs/evidence。
