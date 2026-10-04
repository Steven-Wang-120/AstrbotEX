# CartPole 实验历史索引

更新：2026-10-01。本索引保留历史结论，不重新运行 Step 3、Step 4 或 GUI 实验。

## 当前入口

当前入口为 `scripts/start_cartpole_service.sh`，调用服务 `/astrex/cartpole/move_to`。启动、调用和限制见 [CartPole 控制服务](CARTPOLE_CONTROL_SERVICE.md)。ROS 包仅提供 `cartpole_service`、`state_cache` 两个可执行入口。

活动源码保留 Controller、StateCache、参考曲线、公共任务逻辑、正式服务和生产回归测试。旧运行器、分析器、单次试验节点及专用测试已退休。底层 Isaac 启动器和 ROS 专项回归测试保持原样。

## 归档与最终证据

本次两个目录使用相同的本地时间戳和 UUID：

```text
历史归档：
/data/shared/AstrEX_project_data/exports/cartpole_archive/20261001T161758+0800_2b96da8862/

最终服务证据：
/data/shared/AstrEX_project_data/logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/
```

归档清单记录原路径、新路径、大小、SHA256、归档原因和归属证据。所有移动都使用无覆盖操作。日志、JSON 和历史结论原文不变；其中的旧路径通过清单查找。恢复说明见 [归档 README](../../../20261001T161758+0800_2b96da8862/README.md)。

归档后定向构建成功，保留的 130 项生产测试全部通过，完整服务物理证据独立复评 PASS。归档前 202 项含已退休测试，仅作为历史数量。

| 目录 | 内容 |
| --- | --- |
| 归档 `historical_outputs/` | Step 3 小脉冲、BalanceHold B/C、Step 4 的成功和失败记录，包括此前退休的工具 |
| 归档 `historical_ros/` | 由 CartPole 报告、配置或 manifest 确认归属的 Isaac ROS 运行目录 |
| 归档 `historical_reports/docs/` | 已被用户移除的三份 Step 3 报告，从原 HEAD `c90321b` 保存的副本；未恢复到工作区 |
| 归档 `retired_source/` | 本次退休的六个源码/测试文件和 Step 4 执行计划 |
| 归档 `service_attempts/`、`service_validation_intermediate/` | 服务失败尝试、用户 GUI Stop 中断、临时脚本和中间评估 |
| 归档 `cache/`、`temporary/` | 已确认属于 CartPole 的缓存、临时工具和测试输出 |
| 最终 `service_run/`、`isaac_run/` | 完整成功服务会话及对应逐物理步状态、事件和退出记录 |
| 最终 `validation/` | 六次调用、客户端退出、原独立评估工具/结果、截图、构建测试及保护快照 |
| 最终 `checks/` | 归档后的定向构建、生产测试、接口检查和独立复评 |

## 已完成阶段

| 阶段 | 报告与代码版本 | 当前证据位置 | 状态 |
| --- | --- | --- | --- |
| 方向、显式清零与小脉冲 | 归档 `historical_reports/docs/CARTPOLE_STEP3C5_PULSE_DEBUG.md` | 归档 `historical_outputs/cartpole_step3c5_*`、`historical_ros/` | HISTORICAL |
| 时间戳状态缓存 | `8be7aa8` | 活动源码与 Git 历史 | 正式服务继续复用 |
| BalanceHold B | 归档 B 报告，`104f78b` / `9c51837` | 归档 `historical_outputs/balance_hold_step3d/` | HISTORICAL；只覆盖原工况 |
| BalanceHold C | 归档 C 报告，`3ed7aa4` | 同上 `c_runs/` | HISTORICAL；只覆盖原工况 |
| Step 4 | GUI 最终报告（本机历史引用：`CARTPOLE_STEP4_GUI_RESULT.md`），`66f7571` / `c90321b` | 归档 `historical_outputs/cartpole_step4/` | HISTORICAL；原 PASS 与失败记录均保留 |
| 最终服务 | [服务报告](CARTPOLE_CONTROL_SERVICE.md) | 最终服务证据目录 | 六次调用和客户端退出均成功；限制见服务报告 |

`tests/isaac/ros_system_regression.py` 与 `validate_ros_cartpole_boundary_reset.py` 保留为专项测试，不因入口退休而删除，也不在本次重新运行。

## 保护快照

## 保护与范围

本次归档的保护记录在归档 `protection/` 中。包含原 HEAD、暂存区副本、非本轮文件与暂存条目指纹、包清单、Conda export、Lab 状态及受保护源码 SHA256。原阶段的保护快照仍随各阶段完整目录保存。

未整体移动 `logs/isaac/ros`，没有清空 build/install/log。无法确认归属的 ROS 输出、AstrBotEX/YOLO 文件、`/tmp/astrex_stop_probe_20261001.json` 和运行锁文件均不处理。独立的控制器 `np.errstate` 改动留在用户工作区，不进入本次提交。

当前磁盘环境清单只有 `base`、`isaaclab232_test`；此前已未发现两个旧 Isaac 6 环境。本次不创建、修改或删除环境，也不把旧 effort FAIL 写成已确认的 Sim 回归。
