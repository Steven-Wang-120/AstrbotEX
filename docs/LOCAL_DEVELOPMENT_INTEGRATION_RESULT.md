# 集成来源与复验入口

本页保留来源身份、可重复检查入口与验证边界；逐轮计数、平台失败日志和验收进度保存在仓库外的 PROGRESS/evidence，不作为产品运行依赖。

## 1. 来源与边界

| 来源 | 固定版本 / 作用 |
|---|---|
| 独立 EX 起点 | `1f6e4a86f46e661e9a26d522d930fb036cdf156a` |
| 三方内容基线 | `c9624a439874740cde4acc347c979419af120d05` |
| 原 AstrEX 来源 | `4cf7e11a201d82c0332a5ecc4f6bd1864d30444d`，原 `apps/AstrBotEX/` 映为仓库根 |
| B04/B07/B08 集成 | `d92f32c`：stop/replace_backend、Laya、管理 |
| B09 集成 | `95fa6de`：页面与浏览器入口 |
| ROS 管理边界 | `706b90a`：鉴权、loopback、接口目录 |
| 来源记录基线 | `531b7c9` |

只迁明确功能和施工文档，未合并全部 Git 历史；保留独立上游依赖/迁出系统验证的决定。不迁 CartPole、旧 ros2_ws、Isaac workspace/build、用户数据库/凭据/虚拟环境或模型缓存，旧本机归档不是长期依赖。

组合入口是 [api_server.py](../astrbot_ex/core/api_server.py)，停止/切换见 [边界说明](B04_BOUNDARY_20261001_RESULT.md)，供应商和管理见 [B07](B07-LAYA-BACKEND.md)、[B08](B08-DECISION-MANAGEMENT.md)。Goal/Action SDK、TopicBus、contracts/templates/tests 沿用，不造新执行器。HostTask 计划属于 Host；EX currentGoal 只一当前步骤，已完成前缀不重放。

## 2. 压力与平台检查

具名原测试：`tests.test_action_dispatcher.DispatcherTests.test_1000_real_dispatcher_state_sequences_with_resources_and_rearm`。保持真实 Dispatcher/Actor/Ledger、1000 sequences、原随机种子、断言和 Future timeouts，不缩规模筛绿。

[diagnose_dispatcher_stress.py](https://github.com/Steven-Wang-120/AstrBotVLA-tests/blob/main/validation_drivers/diagnose_dispatcher_stress.py) 记录实际 imported source hashes、SQLite 慢 SQL/commit（>0.1s）与 8s 周期线程栈。`--dump-mode auto` 在 Windows 使用 Python watchdog，在其他平台使用 native faulthandler；Python watchdog 无法在别的线程长期持有 GIL 或 native 故障时保证输出。`--check-import` 仅核对准确用例，不运行压力、不算通过。超时/关闭竞争要保留源身份与栈分析，一次未复现不构成长时稳定保证。

Windows provider 的可选 fchmod/明确 fd 关闭、owned `.exe` 路径与 POSIX ACL/目录 fsync/整树终止证明是不同范围；Linux/Windows 基线结果也不能互换。当前运行结果应绑定实际源码，不能把旧失败笼统当新 provider 不支持，也不能用定向通过推断全系统平台支持。

## 3. 稳定复验入口

从仓库根，所有日志/截图/结果 JSON 输出外部 evidence/临时或既有 gitignored runtime，不写 docs/evidence。记录实际命令、exit、计数/skip、源码身份与失败，不复制 key/token/用户全文，不清理用户数据。

```sh
python -B -m unittest discover -s tests -t . -v
# 以下独立脚本从 AstrBotVLA-tests 根运行：
python -B -m validation_drivers.diagnose_dispatcher_stress --ex-checkout ../ex --aeb-checkout ../aeb --help
python -B -m validation_drivers.diagnose_dispatcher_stress --ex-checkout ../ex --aeb-checkout ../aeb --check-import
python -B -m validation_drivers.diagnose_dispatcher_stress --ex-checkout ../ex --aeb-checkout ../aeb
```

独立诊断入口与跨仓集成用例已迁往 [AstrBotVLA-tests](https://github.com/Steven-Wang-120/AstrBotVLA-tests/blob/main/docs/PR-REMEDIATION-20261007.md)。从该仓库根运行上述 module 命令，明确 EX/AEB checkout；stdout/stderr 指向新外部日志。EX 的功能单元测试及必需样例保留。

- 模型协议/质量：[B07 验证](B07-LAYA-BACKEND.md#8-验证与已知限制)、[锁定依赖](../requirements-laya.lock)。生产 decoder、测试 Actor 和真实权重选择质量分开。
- 管理/归属/停止：[B08 验证](B08-DECISION-MANAGEMENT.md#7-具名验证入口与限制)。external 不调用 owned 生命周期，runtime IDLE 不代替 registry proof。
- 页面：[两类浏览器入口](B09_DECISION_UI_RESULT.md#两类浏览器检查)。合同替身、实际 HTTP、真实 A.E.B 投影与模型/物理任务验收分开。
- ROS：[部署](ROS2-DEPLOYMENT.md)、[SDK](ROS2-SDK.md)、[verify_ros2_deployment.py](../scripts/verify_ros2_deployment.py)。保留 ROS setup 的 PYTHONPATH、独立 Domain/唯一 topics、临时 loopback API/token-file；String echo 不是运动控制。

管理服务只绑定 `127.0.0.1:8765`，远程使用 SSH 隧道；页面/保存/health/probe 都不等于 runtime running 或 Goal 授权。可信 Host 工具/公开回复的支持边界见 [TECHNICAL](../TECHNICAL.md#host-工具与公开回复边界)。

## 4. 能力验证限制

confidence 不是物理成功概率；固定 Jev/Laya 身份和 wait-only probe 不证明机器人语义。多 owner/大观测、模型微调、GUI/GPU 争用、真实机器人/Isaac 抓放与长时稳定性须分别测量。受支持 buffered Host 的公开回执是逻辑 at-most-once，不保证送达，也不覆盖任意第三方 live/direct/proactive send。

[施工索引](AstrEX_workingTree/00_施工总索引与派工规则.md)、[移动机械臂计划](MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md)与 [B11](AstrEX_workingTree/B11_高风险_ROS2执行插件与安全停止.md)保持各自设计范围；计划不是当前物理能力。旧 WS token、Windows ACL/目录持久性与整树/GPU 停止限制继续保留。集成验证边界集中于管理验证章节，不以清理报告改变产品能力。
