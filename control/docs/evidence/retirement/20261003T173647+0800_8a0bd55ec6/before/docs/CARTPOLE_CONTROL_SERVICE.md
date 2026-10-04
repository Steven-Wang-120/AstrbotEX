# CartPole 控制服务

本入口把现有 CartPole 控制封装为常驻 ROS 2 Service。调用者只提供滑车的目标坐标。GUI 和服务在一次任务结束后继续运行。

2026-10-01：历史试验入口和中间产物已移入共享归档。活动项目只保留正式服务及公共控制模块。历史位置见 [实验历史索引](CARTPOLE_EXPERIMENT_HISTORY.md)。

## 启动

在项目根目录执行：

```bash
./scripts/start_cartpole_service.sh
```

等待终端输出服务 ready。启动器先启动正式 Isaac ROS GUI，再启动系统 Jazzy 服务节点。`--headless` 关闭窗口，其他配置不变。

启动沿用 Isaac Sim 5.1.0.0、Isaac Lab v2.3.2、CPU PhysX、domain 63 和 Fast DDS。Sim 使用内置 Python 3.11 Jazzy；服务使用系统 Python 3.12 Jazzy。

正式 domain 预检保持不变。未知或应用参与者会阻止启动。只有已确认身份且仅含诊断端点的本地 ros2cli daemon 可以排除；启动器不停止 daemon。

## 调用

在另一个终端加载系统 ROS 和项目 overlay：

```bash
source /opt/ros/jazzy/setup.bash
source /home/sssxy/Projects/AstrEX_project_main/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=63
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

ros2 service call /astrex/cartpole/move_to \
  astrex_interfaces/srv/MoveCartTo \
  "{target_position_m: 0.3}"
```

`target_position_m` 是 `slider_to_cart` 的绝对关节位置，单位为米。有效范围为 `[-0.5, +0.5]`，包含端点。它不表示相对位移或世界 XYZ 坐标。

接口只包含：

```text
float64 target_position_m
---
bool success
string message
```

服务等待整个任务结束才返回。`success=true` 表示移动、独立保持和最终清零全部完成。`false` 的说明区分 BUSY、目标无效、初态不合格、运行故障、超时和清零未确认。

## 一次任务怎样运行

```text
读取当前真实状态 S0
→ 从 S0.x 开始五次平滑参考
→ 到达目标并连续稳定 1 仿真秒
→ 停止 Move 控制，发送零命令
→ 等待晚于 Move 成功状态、且在零命令后新到达的 S1
→ 在目标处独立连续保持 3 仿真秒
→ 多次发送零命令并确认 Controller 输入为零
→ 返回结果，释放执行占用
```

每次请求从当前状态开始。服务不按请求 reset，不把上次目标当作当前位置。一次只有一个任务；执行和清零期间的新请求立即返回 BUSY，不排队、不抢占。

空闲时没有隐藏的保持控制。零力只表示停止施力，不能保证摆杆继续直立。下一次请求仍需满足初态门槛。

## 初态门槛与固定控制

开始前必须收到有效 `/joint_states`。状态年龄不能超过 0.2 本地秒，还须满足：

| 状态 | 准入条件 |
| --- | --- |
| 滑车位置 | `abs(x) <= 0.55 m` |
| 滑车速度 | `abs(x_dot) <= 0.10 m/s` |
| 摆杆角度 | `abs(theta) <= 2°` |
| 摆杆角速度 | `abs(theta_dot) <= 0.20 rad/s` |

Isaac 必须在线，且不能有其他 `/joint_command` 发布源。状态不合格时直接拒绝，不自动扶正。

控制沿用原 K、60 Hz、±5 N 限力和 0.05 m/s 最大参考速度。摆杆 torque 始终为零。稳定条件同时检查目标位置误差 ≤0.05 m、滑车速度 ≤0.10 m/s、杆角 ≤2°和杆角速度 ≤0.20 rad/s。

保护保留：杆角 10°、参考误差 0.25 m、路径走廊、连续限幅 0.5 仿真秒、状态 freshness、时间异常、Isaac boundary/reset 和控制冲突。故障优先于成功。

Move 最多运行参考时长 `T+8` 仿真秒；Hold 最多 10 仿真秒。本地任务期限为 `max(60, 3*(T+18))` 秒。等待 S1 的本地期限为 0.5 秒。

结束时多次发零，再用 Isaac 逐物理步日志确认 Controller 输入清零，最多等待 2 本地秒。Controller 输入是控制图证据，不能写成 PhysX 实测 applied effort。无法确认清零时不能返回成功。

## 中断和输出

客户端退出或等待超时不会取消服务器任务。服务器继续到成功、有界超时或故障，然后清零。本轮没有独立 stop 服务。

在总启动器终端按 Ctrl+C，会先请求服务清零，再关闭该启动器创建的服务和 Isaac 进程。不会按进程名批量终止其他程序。

启动器打印实际输出目录。服务日志在共享数据 `logs/isaac/cartpole_service/<timestamp_uuid>/`；Isaac 日志仍在正式 `logs/isaac/ros/<timestamp_uuid>/`。运行证据不进入 Git，也不创建共享盘 symlink。

## 本轮验收

2026-10-01：本轮服务验收通过。验收结束后已清零并关闭本轮启动的服务和 Isaac。再次使用时运行上面的启动命令。

### 同一 GUI 会话中的真实调用

六次调用逐个等待响应。服务没有为请求执行 reset。下一次参考从收到的真实位置开始。下表的初始位置取自服务准入状态，力取自发布记录和 Isaac 逐物理步日志。

| 次序 | 目标 m | 初始位置 m | 结束位置 m | 最大发布力 / Controller 输入 N | Move / Hold 用时（仿真秒） | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 0 | 0 / 0 | 1.000 / 5.308 | SUCCESS |
| 2 | +0.3 | 0 | +0.2998 | 0.470 / 0.470 | 13.175 / 3.458 | SUCCESS |
| 3 | −0.3 | +0.2998 | −0.2999 | 0.499 / 0.499 | 23.500 / 3.000 | SUCCESS |
| 4 | +0.5 | −0.2999 | +0.4998 | 0.504 / 0.504 | 31.000 / 3.000 | SUCCESS |
| 5 | −0.5 | +0.4998 | −0.4998 | 0.512 / 0.512 | 38.500 / 5.317 | SUCCESS |
| 6 | −0.5 | −0.4998 | −0.4999 | 0.017 / 0.017 | 3.000 / 3.000 | SUCCESS |

Move/Hold 用时包含进入稳定窗口之前的控制过程。每次 Move 的最终连续稳定窗口至少 1 秒，每次 Hold 的独立连续稳定窗口至少 3 秒。所有成功窗口同时满足四状态条件。连续限幅时长均为 0。

每次结束都发送 10 条零命令，并在新的反馈之后确认真实 Controller 输入为 `[0.0, 0.0]`。七次任务的最终清零阶段用时为 0.194～0.429 本地秒，均小于 2 秒。

### 其他检查

| 检查 | 实测或测试结果 |
| --- | --- |
| 服务可发现、持久运行 | 同一服务、同一图完成六次调用，再接受第七次请求 |
| `/clock`、`/joint_states` | 独立客户端各收到 16,194 条；时钟有 16,193 次严格递增，无倒退 |
| 单一控制源 | 验收客户端不发布关节命令；正式服务是唯一 `/joint_command` publisher |
| BUSY | 第二次任务中追加请求，约 0.57 ms 返回 BUSY，原任务不受影响 |
| 无效目标 | `±0.500001`、NaN、Inf 全部拒绝；`±0.5` 已实际执行 |
| 顺序交接 | Move 成功后不再发送 Move 控制力；零命令之后收到新 S1，才运行 Hold |
| 客户端退出 | 第七次请求目标 −0.5 m；客户端约 0.2 秒后退出，服务继续完成并清零，任务总用时约 6.99 本地秒 |
| 状态与物理对应 | 独立评估核对 27,452 条逐物理步记录；16,194 条客户端反馈按仿真时间精确匹配，无插值 |
| reset、边界、故障 | 七次请求期间无 reset、boundary 或故障记录 |
| GUI | 新入口的 CartPole、Play 状态和移动过程可见；截图保留。历史 Step4 人工方向和稳定观察结论不变 |
| 启动器退出 | 向本轮启动器发送 SIGINT；确认清零后关闭所属进程，退出码 0，`cleanup_verified=true`，无所属进程遗留 |
| 构建 | 系统 Python 3.12 定向构建 `astrex_interfaces`、`astrex_ros_bridge` 成功；保留 `SpinPole.srv` |
| 服务开发完成时的相关测试 | 202 项通过；包含后来退休的试验工具测试。归档后的实际测试数量见本文末尾 |

缺失、过期、不合格状态及故障分支主要由单元测试覆盖。本轮没有为了这些测试修改官方场景或再跑旧 B/C 矩阵。

### 证据位置

本轮独立验证目录：

```text
/data/shared/AstrEX_project_data/logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/validation/
```

- `client_03/result.json`、`feedback.jsonl`：六次调用、BUSY、无效目标和独立状态记录。
- `client_disconnect_03.json`：客户端在结果返回前退出的时间记录。
- `assessment_03b.json`：独立评估、四状态初态、参考、稳定窗口、力和清零证据。评估脚本不导入生产任务代码。
- `gui_03_start.png`、`gui_03_move_negative.png`、`gui_03_final.png`：新 GUI 入口的窗口截图。
- `unit_tests_final.txt`、`build_final.txt`：归档前的 202 项相关测试和定向构建结果。
- `before_complete/`、`after/`：环境、源码和暂存状态的保护快照。
- `tools/astrex_service_assess.py`：独立评估工具，不参与正式启动。

正式服务记录位于：

```text
logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/service_run/
```

其中 `service/requests/*/result.json` 保存每次结果；`service/trace.jsonl` 保存四状态、参考、发布力和阶段时间。`service/shutdown.json` 保存退出清零证据。`manifest.json` 的最终状态为 `STOPPED`，清零与进程清理均已确认。

对应 Isaac 记录位于：

```text
logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/isaac_run/
```

`ros_physics_samples.jsonl` 记录实际逐物理步状态和 Controller 输入；`ros_events.jsonl` 记录图、边界和 reset 事件。上述相对路径均以 `/data/shared/AstrEX_project_data/` 为根。

### 保留的失败记录和限制

1. 第一轮未执行控制任务。DDS 端点的 GID 不变，但名称从尚未解析状态变成正式图节点名称，导致服务拒绝请求。启动代码已改为等待身份解析，并连续两次核对完整端点后才 ready。
2. 第二轮前三个目标成功。第四次执行期间用户在 GUI 操作了 Stop；端点消失，服务返回失败且不虚报清零确认。用户确认原因后授权重测。`client_01/`、`client_02/` 和两轮原始运行目录均已无损归档。
3. Move→Hold 交接确认的是“零命令已发布、新 S1 已到达”。DDS 和物理步有延迟，单条交接零可能被后续 Hold 命令覆盖。本轮不能保证每次交接都出现独立的物理零力样本。它不影响“Move 不再计算控制力”的顺序规则。最终返回前的清零则有逐物理步 Controller 输入证据。
4. 正常退出出现一次 `cannot use Destroyable because destruction was requested` 的未领取回调异常警告。本地 Jazzy 执行器源码支持回调与执行器 guard 销毁竞态的解释，但没有完整栈证明根因。退出清零、退出码和所属进程清理均已确认；保留该警告，不声称退出无警告。
5. 既有四项包 metadata warning 保持原样，本轮没有新增或修复依赖冲突。归档前 202 项测试中的两条 NumPy warning 来自刻意输入极大数值的非有限力保护测试；归档后的 130 项测试未输出 warning。
6. 空闲期没有自动平衡，状态可能随时间变化。长时间等待后若初态不合格，下一次调用会被拒绝。本服务没有自动扶正、取消或独立 stop 接口。

## 实现文件与保护检查

正式入口在 `scripts/start_cartpole_service.sh`，进程管理在 `scripts/lib/cartpole_service_launcher.py`。常驻服务节点为 `astrex_ros_bridge/cartpole_service_node.py`。公共任务逻辑为 `cartpole_task.py`。原试验节点、运行器、分析器及专用测试已归档；包入口仅保留 `cartpole_service`、`state_cache`。

接口增量添加 `MoveCartTo.srv`，没有覆盖 `SpinPole.srv` 或已有接口配置。包依赖纠正为 `rclpy` 并添加 `astrex_interfaces`。控制器只修正工作区原有拼写错误，其余用户改动保留；K、限力、速度和容差未调参。

开始和结束的包清单、Conda export、Lab HEAD/状态、受保护文件 SHA256 和用户暂存内容 SHA256 均一致。实际环境清单只有 `base` 和 `isaaclab232_test`；本轮没有创建或删除环境。

以上服务开发和实测阶段没有暂存、commit 或 push。随后按用户授权进行历史归档和一次限定提交。提交范围只含 CartPole 最终服务、必要接口、生产测试、文档和已确认退休文件的删除记录；独立控制器改动及其他项目暂存内容不在提交范围。

## 归档收尾检查

共享归档目录：

```text
/data/shared/AstrEX_project_data/exports/cartpole_archive/20261001T161758+0800_2b96da8862/
```

`manifest.json`、`remainder_manifest.json`、`supplemental_manifest.json`、`late_cache_manifest.json` 和 `git_reports_manifest.json` 记录原路径、新路径、文件大小、SHA256 与归档原因。恢复步骤见归档中的 `README.md`。日志和 JSON 原文未改写；其中旧绝对路径通过清单映射到新位置。

成功会话的完整数据保存在上面的 `cartpole_service_final` 目录。`validation/assessment_03b.json` 保留原评估；`checks/` 保存归档后的定向构建、生产测试和独立复评结果。收尾检查不再运行 GUI、训练或历史实验矩阵。

| 收尾检查 | 结果与证据 |
| --- | --- |
| 归档完整性 | 98 项移动逐项核对 SHA256；三份 Git 历史报告副本一致；无覆盖、无实验数据删除 |
| 定向构建 | `astrex_interfaces`、`astrex_ros_bridge` 成功；见 `checks/build.txt` |
| 生产回归测试 | 130 项通过，无跳过；见 `checks/unit_tests.txt`。202 项只保留为开发阶段历史数量 |
| 接口和入口 | `MoveCartTo`、`SpinPole`、服务节点可导入；安装文件和 console metadata 仅含 `cartpole_service`、`state_cache` |
| 退休模块依赖 | 活动 Python 源码未导入退休的节点、运行器或分析器；旧安装别名已归档 |
| 原始物理证据复评 | PASS，issues 为空；见 `checks/assessment_after_archive.json`，未重新运行模拟器 |
| 环境与源码保护 | base/目标环境包清单、Conda export、Lab HEAD/状态和受保护文件指纹前后一致 |
| 文件与暂存保护 | 非本轮 342 个文件及其他暂存条目保持不变；控制器独立改动不提交 |
| 提交候选检查 | 29 个显式路径；无秘密材料、运行产物、大文件或共享盘 symlink；`git diff --check` 通过 |

完整收尾检查在 `checks/closeout_checks_final.json`，提交后的核对记录在 `checks/commit_receipt.json`。本次不改变控制参数，也不把 Controller 输入当作实测 applied effort。不 push。

本轮交付只覆盖这个 CartPole 服务。到此停止扩展，不增加 Action、Grounder、AstrBotEX、YOLO 或 HTTP 接入。
