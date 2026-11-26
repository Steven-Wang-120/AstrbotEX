# B07 Laya：依赖来源与验证边界

协议、固定身份、输入预算、取消与恢复见 [Laya 后端说明](B07-LAYA-BACKEND.md)。实现入口为 [适配器](../astrbot_ex/core/decision/backends/laya.py)、[静态工厂](../astrbot_ex/core/decision/backends/registry.py)、[服务集成测试](../tests/test_laya_service_integration.py) 与 [真实验证入口](../scripts/verify_laya_backend.py)。EX 主环境不引入 torch/transformers。

## 固定依赖与语义边界

[requirements-laya.lock](../requirements-laya.lock) 固定 Linux/Python 3.12/CUDA 12.8 环境及来源哈希，不是任意 Windows/CPU/GPU 兼容承诺。源码固定 `6d942c92081fbc139e736bbd9ac0023223c29b7f`；`typed-decisions` bundle revision 固定 `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`，不能套用 standalone revision。源码、权重与 tokenizer 身份不证明模型理解机器人任务。

确定性 HTTP fixtures 验证身份、映射、概率、预算、单在途、deadline、取消/迟到失效与 EX/测试 Actor/Ledger 链路，不是模型质量实验。start 与 request_replan 语义区分必须单独评估；正常返回、退出码或 model_to_actor_pass 不等于 selection_quality_pass。

测试 Actor 的 admitted/accepted/running/succeeded 不代表机械臂 ACK 或物理完成。计时区分冷加载、预热、热推理、HTTP、快照到 Actor callback；callback accepted 不是 SQLite 持久化 ACK 或停车时间。多 owner/大观测仍按预算拒绝，不推广单 owner 短样本为泛化、p95 或长期稳定保证。

## 连接与退役

schema v2 external/owned、固定模型、独立 Jev/Laya key、生产 transport gate、原子存储与 wait-only probe 见 [连接合同](B07-LAYA-BACKEND.md#2-外部连接与可选自启冻结-v2-合同)。保存/测试不授执行；POST 后不确定锁存 restart_required，health/新客户端/本地断连不能解除隔离。恢复需要框架停止、自有代次退出或外部服务所有者证明、新后端/授权/Goal，不自动重放。

low_confidence/request_replan 在 dispatch 前拒绝整轮，保留原选择；实际停止证明、持久化退役草稿和当前 Goal CAS 后才发布 failed 反馈。currentGoal 只是一当前步骤，HostTask 多步骤计划归 Host，完成前缀不因重规划重放。

Windows fd/fchmod 和 owned `.exe` 路径不意味着 ACL、目录崩溃持久性或整树/GPU 终止已证明。真实 ROS/Isaac 抓放、模型微调、GUI 显存争用、机器人质量与长时稳定性须独立测量。

## 可重复验证入口

从仓库根运行无需模型/key 的定向测试：

```sh
python -B -m unittest tests.test_laya_backend tests.test_laya_service_integration tests.test_decision_backend_contract tests.test_jev_backend tests.test_decision_service tests.test_decision_runtime_integration tests.test_snapshot_contracts -v
```

真实模型显式运行，独立环境/固定离线缓存/新空外部 output：

```sh
python -B scripts/verify_laya_backend.py --help
python -B scripts/verify_laya_backend.py --run-real --laya-python /absolute/laya-env/bin/python --model-cache /absolute/laya-cache --output /absolute/external-evidence/new-run --device cuda --repetitions 8
```

端口占用失败，不扫杀其他服务。保持 assertions/deadline/压力规模；输出只写外部 evidence/临时目录。框架压力入口与源记录见 [集成说明](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。
