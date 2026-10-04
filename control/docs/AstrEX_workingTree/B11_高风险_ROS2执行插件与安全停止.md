# B11｜高风险｜ROS 2 执行插件与安全停止

更新：2026-10-04。状态：**项目 R 第一阶段及 E0 必要依赖实施中，物理验收尚未完成**。

已增加移动机械臂控制插件、ROS 网关、MoveIt 适配及最终执行保护；标准控制器已加载真实 Isaac 状态。实际运动、停止和完整抓放必须分别取得证据，见[第一阶段进展](../MVP_FIRST_STAGE_RESULT.md)。

本轮已同步并验证上游原生 ROS 通信。5 项既有 DDS 测试通过，不等于机械臂执行或 Laya 闭环通过。完整结果见[同步报告](../ASTRBOTEX_SYNC_20261001_RESULT_HISTORY.md)。

## 1. 当前职责与依赖

B11 对应[移动机械臂方案第 19 节](../MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md#19-开发项目-rros-2-执行网关与实际闭环)。该章节是接口、超时、停止和故障矩阵的唯一任务规格；不另建一套机器人执行框架。

| 工作 | 归属 | 依赖 |
|---|---|---|
| 管理 API、权限、请求追踪 | B08 | 已同步 B02/B04/B07 |
| 页面、状态和停止展示 | B09 | B08 接口冻结 |
| 仿真控制插件、ACK/取消/反馈 | B11 / 项目 R | B02/B04、Goal 参数和目标引用 |
| MoveIt 2／OMPL 与物理单臂基线 | 项目 A | 与 R 共用网关和验收 |
| Laya 技能／目标选择与失效处理 | 项目 L | 任务目录／澄清和 R 的执行保护 |
| 微调和移动 GUI | 项目 T/M | 前述实际闭环通过 |

不将 B05/A.E.B. 完整接线或 B10 新 YOLO 作为首轮硬依赖。固定脚本使用正式 Goal 合同，已知物体使用 RGB/几何感知；目标关联仍遵守上游 source/epoch/reference 规则。B10 后续替换感知时复用这些边界。

## 2. 复用与最小新增

保留现有 EnvironmentManager、PluginRosFacade、ros2 adapter 的上下文、队列、绑定代次和 quiesce。`ros2_echo` 只作为通信示例。

只新增一个仿真控制插件及一个控制包中的网关/执行适配。先做固定底盘机械臂，再接等效差速底盘，不预建两个真机插件。插件通过 `context.ros` 声明端口，禁止自行 init/shutdown/spin。

标准 Action 客户端位于网关；EX 与网关通过已有 facade 的有界消息连接。无需为首轮扩展核心 Action client facade。全部 topic 使用独立命名空间和配置的 Domain。

## 3. 必须区分的事实

1. EX admitted：框架允许进入命令处理。
2. 插件 accepted：插件受理，不代表求解或执行完成。
3. queued/tx_published：消息入队或本地发布。
4. 远端 ACK：执行器受理了关联命令。
5. 反馈与物理判据：正在运动、成功、失败或已停止。

第 5 类事实才能支持执行结果。无远端确认时返回 failed/unknown，不从 Twist 发布次数推导到位。

## 4. 开发步骤和停止规则

| 步骤 | 交付 | 完成条件 |
|---|---|---|
| B11.1 | 冻结 command/cancel/lease/ACK/feedback/result 映射 | command_id、会话、epoch 和标准 Action UUID 可关联 |
| B11.2 | 状态缓存、Action 网关、Isaac 执行适配 | 与项目 A 共用一次物理抓放基线 |
| B11.3 | Guard、命令有效期、watchdog | 超限不放行，EX/网关退出后不无限续跑 |
| B11.4 | 取消、Goal 替换、环境切换、重启 | 旧结果失效，停止证据可信，未确认保持 blocked |
| B11.5 | Laya 实际技能选择与 GUI | 完成项目 R 保护验收；T1 语义数据和离线训练无需等待该物理阶段 |

start/cancel 回调预算默认 20 ms，长任务转 worker。环境失活使用已有授权 hook/publish_stop；普通取消走正常接口。

最终输出设置 actuator effort 上限，停止与评分进程分离。停稳后才能回报结构化 StopEvidence；unknown 等既有终态后用受控 reconcile_stop，不改写终态掩盖不确定性。

## 5. 测试、证据与现有缺口

复用第 19 节 R01–R09，不再次运行另一套相同抓放。测试包括真实模型选择、无解、窄间隙、超限、取消、推理卡住、DDS/状态中断、重复/迟到、执行器重启和 GUI。

另核对 QoS 不兼容、缺消息类型或绑定关闭时动作不可执行；只有 bbox 而无米制定位时拒绝抓取。记录命令、反馈、驱动限制、停止时序和物理结果。

2026-10-01 历史框架回归有一项 `test_proof_retries_keep_same_gate_epoch_and_do_not_clear_blocked_goal` 失败。历史失败与本轮测试分别保留。本轮使用可信 StopEvidence 与既有 reconcile_stop，不用私有 pending 标志替代停止完成；物理运动前后仍须实测停止，不能覆盖历史结果。

## 6. 人工介入与回滚

用户确认首次 GUI 工作区、观看一次真实停止演示。此 MVP 只使用 Isaac，不包含硬件控制、香橙派部署或实物标定。

回滚前确认机器人停止，再停 runtime、关闭绑定和插件。保留账本与失败证据，旧插件/队列恢复不能重新触发运动。
