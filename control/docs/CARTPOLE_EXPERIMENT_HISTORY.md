# CartPole 实验历史索引

更新：2026-10-03。状态：**完整归档，无活跃 CartPole 入口**。本次不复跑 Step 3、Step 4、服务或 GUI 实验。

## 当前归档

- [本轮归档说明](evidence/retirement/20261003T173647+0800_8a0bd55ec6/README.md)
- [逐文件清单及 SHA256](evidence/retirement/20261003T173647+0800_8a0bd55ec6/MANIFEST.json)
- [清理验收报告](ISAAC_ROS_CARTPOLE_RETIREMENT_20261003_RESULT.md)

`retired_source/` 按原相对路径保存场景、控制器、MoveCart 服务、StateCache、SpinPole/MoveCart 接口、两个 ROS 包、ROS/RL 入口、专用测试和工作空间日志。`before/` 保存共享工具与文档的清理前快照；`runs/ros/` 保存本次搬迁的六个已确认 CartPole 运行目录。

build/install 属于可重建产物，只记录原始清单和哈希后删除。恢复时重新构建，不复用旧生成目录。原工作区已删除的 `CARTPOLE_STEP4_GUI_RESULT.md` 未恢复；需要旧正文时查 Git 历史，物理证据仍在下述历史归档中。

## 保持原位的历史证据

| 内容 | 位置 |
|---|---|
| 2026-10-01 历史归档 | `/data/shared/AstrEX_project_data/exports/cartpole_archive/20261001T161758+0800_2b96da8862/` |
| 最终服务证据 | `/data/shared/AstrEX_project_data/logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/` |
| Isaac cutover 证据 | `/data/shared/AstrEX_project_data/logs/isaac/cutover_20260917T103433Z_6f2a91/` |
| 2026-09-17 基线运行 | `logs/isaac/ros/` 的五个当日目录，以及 `logs/isaac/rl/20260917T054055Z_605f24633c/` |
| 更早期 Isaac 基线归档 | `/data/shared/AstrEX_project_data/archives/isaac/2026-09-baseline-consolidation/` |

上述目录不重复搬运。逐项原路径见本轮 MANIFEST 的 `retained`；本次迁移文件由 `entries` 中的 source/destination 映射定位。历史报告中的旧绝对路径不改写。

## 历史结论

| 阶段 | 原版本／报告 | 保留结论与限制 |
|---|---|---|
| 方向与小扰动 | 原历史归档的 Step 3C.5 报告 | 原 PASS、失败及限制保留 |
| 时间戳状态缓存 | `8be7aa8`；本轮源码归档 | 原 CartPole 两关节实现，不能当通用机械臂缓存 |
| BalanceHold B | `104f78b` / `9c51837` | 只覆盖原工况 |
| BalanceHold C | `3ed7aa4` | 只覆盖原工况 |
| Step 4 | `66f7571` / `c90321b`；旧历史归档 `historical_outputs/cartpole_step4/` | 原 GUI 与实验结论保留 |
| 最终服务 | [原服务报告](evidence/retirement/20261003T173647+0800_8a0bd55ec6/before/docs/CARTPOLE_CONTROL_SERVICE.md) | 六次调用和客户端退出均成功；限制沿用原报告 |

2026-10-01 归档后的定向构建、130 项生产测试和服务证据复评是历史成绩，不是本次重测结果。[清理前索引全文](evidence/retirement/20261003T173647+0800_8a0bd55ec6/before/docs/CARTPOLE_EXPERIMENT_HISTORY.md)保留原阶段说明。

本次保留现有 Isaac、ROS、MoveIt、Laya 环境与其他任务修改，不 commit、不 push。归档只改变开发入口和证据位置，不改变已有结论。
