> 2026-10-02 集成说明：本文保留原施工任务和历史验收边界；已完成的 B04/B07/B08/B09 以 [当前集成报告](../LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md) 为准，不重复开发。

# B11｜高风险｜ROS 2 执行插件与安全停止

更新：2026-10-02。状态：**PLAN ONLY，执行插件尚未开发**。

本轮保留新上游通用 ROS 框架，并完成 Jazzy 原生 DDS echo 往返、管理鉴权和配置恢复。B08/B09 与 EX Laya 已接入；尚未完成 Laya → ROS 执行器 → Isaac 抓放。当前结果见[集成报告](../LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

## 1. 当前职责与依赖

B11 对应[移动机械臂方案第 19 节](../MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md#19-开发项目-rros-2-执行网关与实际闭环)。该章节是接口、超时、停止和故障矩阵的唯一任务规格；不另建一套机器人执行框架。

| 工作 | 归属 | 依赖 |
|---|---|---|
| 管理 API、权限、请求追踪 | B08 | 已同步 B02/B04/B07 |
| 页面、状态和停止展示 | B09 | B08 接口冻结 |
| 仿真控制插件、ACK/取消/反馈 | B11 / 项目 R | B02/B04、Goal 参数和目标引用 |
| PyRoKi 与物理单臂基线 | 项目 A | 与 R 共用网关和验收 |
| 本地模型评分与失效处理 | 项目 L | A 的候选与 R 的执行保护 |
| 微调和移动 GUI | 项目 T/M | 前述实际闭环通过 |

不将 B05/A.E.B. 完整接线或 B10 新 YOLO 作为首轮硬依赖。固定脚本使用正式 Goal 合同，已知物体使用 RGB/几何感知；目标关联仍遵守上游 source/epoch/reference 规则。B10 后续替换感知时复用这些边界。

本仓库只交付 EX 通用通信和插件侧接口。后续仿真工作空间的归属沿用独立 AstrEX 控制项目，不把旧 CartPole 的 ros2_ws、StateCache 或启动器迁入本仓库。

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
| B11.5 | 原始 Laya 实际接入与 GUI | 完成项目 R 故障矩阵，之后才能微调 |

start/cancel 回调预算默认 20 ms，长任务转 worker。环境失活使用已有授权 hook/publish_stop；普通取消走正常接口。

最终输出设置 actuator effort 上限，停止与评分进程分离。停稳后才能回报结构化 StopEvidence；unknown 等既有终态后用受控 reconcile_stop，不改写终态掩盖不确定性。

## 5. 测试、证据与现有缺口

复用第 19 节 R01–R09，不再次运行另一套相同抓放。测试包括真实模型选择、无解、窄间隙、超限、取消、推理卡住、DDS/状态中断、重复/迟到、执行器重启和 GUI。

另核对 QoS 不兼容、缺消息类型或绑定关闭时动作不可执行；只有 bbox 而无米制定位时拒绝抓取。记录命令、反馈、驱动限制、停止时序和物理结果。

2026-10-01 的停止边界失败已由 B04 修正，本轮该测试及停止回执/blocked 交叉断言通过。历史失败记录保留。当前另有未修改的 Dispatcher 1000 次压力测试超时，上游原版也可重现；详见集成报告，不把该项写成通过。

## 6. 人工介入与回滚

用户确认首次 GUI 工作区、观看一次真实停止演示。此 MVP 只使用 Isaac，不包含硬件控制、香橙派部署或实物标定。

回滚前确认机器人停止，再停 runtime、关闭绑定和插件。保留账本与失败证据，旧插件/队列恢复不能重新触发运动。
