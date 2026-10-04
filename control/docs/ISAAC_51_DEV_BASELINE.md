# AstrEX Isaac 开发环境基线

更新：2026-10-03。**环境保留，CartPole 与 ROS/RL 演示入口已归档。** 本文说明当前可用工具；历史运动验收不表示移动机械臂已完成。

## 当前环境与工具

版本配置以 [isaac_baseline.env](../config/isaac_baseline.env) 为准。本次不安装、卸载或升级依赖。

| 项目 | 保留版本或位置 |
|---|---|
| Conda 环境 | `isaaclab232_test`，Python 3.11 |
| Isaac Sim / Lab | 5.1.0.0 / v2.3.2 |
| Torch | 2.7.0+cu128 |
| ROS 2 | 系统 Jazzy；配置为 Fast DDS、domain 63 |
| MoveIt / Laya | 保留现有安装、独立环境和模型 |
| 环境隔离 | `scripts/lib/isaac_common.sh` |
| 健康检查 | `scripts/check_isaac_env.sh`，内部仅支持 `check` |
| ROS 诊断 | `sim/scripts/ros_domain_check.py` 及对应单元测试 |

在项目根目录执行健康检查：

```bash
./scripts/check_isaac_env.sh
```

该命令检查版本、依赖、GPU 和 Lab 源码状态，不创建仿真场景，不发送机器人命令。当前结果见[清理验收报告](ISAAC_ROS_CARTPOLE_RETIREMENT_20261003_RESULT.md)。已有 pip metadata 警告仍按原允许清单报告，不在清理任务中修复。

在独立的系统 ROS 终端加载环境：

```bash
source scripts/dev_env.sh
```

工作空间没有 install 时，该脚本只加载系统 Jazzy。不要把系统 ROS Python 路径加入 Isaac Conda 环境。已打开的旧终端可能仍保留退休 overlay 的环境变量，应新开终端；本次不会修改其他 shell 的环境。

## 已退休的开发内容

CartPole 场景、LQR/BalanceHold、StateCache、MoveCart 服务、SpinPole 接口、两个项目 ROS 包、专用测试和 ROS/RL 启动入口已退出活跃目录。build/install 已清理，构建测试日志已归档。

当前没有新的 ROS 仿真或机械臂启动命令。后续按[移动机械臂计划](MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md)开发。原进程互斥、日志监督与启动前检查实现保存在归档中，供后续按需复用；本次不新增启动框架。

## 历史验收与限制

2026-09-17 的 `BASELINE_CUTOVER = PASS`、CartPole ROS 控制、图生命周期、reset、GUI 与 RL 记录仍是原工况的历史证据，本次不改判定、不复跑。

- [清理前完整基线报告](evidence/retirement/20261003T173647+0800_8a0bd55ec6/before/docs/ISAAC_51_DEV_BASELINE.md)：原文字节不变；其中旧命令只用于历史查证。
- [CartPole 历史索引](CARTPOLE_EXPERIMENT_HISTORY.md)：原始运行目录和当前归档位置。
- [Isaac 更早期历史](ISAAC_HISTORICAL.md)：包括已知失败、未解释现象与限制。
- 原 cutover 证据目录 `/data/shared/AstrEX_project_data/logs/isaac/cutover_20260917T103433Z_6f2a91/` 及 2026-09-17 ROS/RL 基线运行保持原位。

现有 ROS domain 诊断仍采用当时的严格参与者策略。这是历史 cutover 门禁，不是已实现的移动机械臂多节点准入策略。后续接入时需要明确 endpoint 冲突规则，不能因保留该工具就宣称新场景可运行。

CPU PhysX 演示链以外的 GPU ROS、复杂 RTX、多机器人及长期运行限制不因本次清理消失。健康检查通过不等于这些能力已验证。
