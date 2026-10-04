# AstrEX Isaac 开发基线

## 切换状态

当前状态：**BASELINE_CUTOVER = PASS，`isaaclab232_test` 是 AstrEX 当前 Isaac 开发默认基线。**

ROS 图生命周期已收敛为“图 = 进程生命周期，reset = episode 生命周期”：OmniGraph、Subscribe、Clock、Joint、Controller 只在初始化时创建一次，`reset()` 不再创建新 UUID 图、不删除任何 ROS 节点。reset 期间用最小 command gate 关闭命令接受，状态验证通过后才恢复。旧的重复图问题（Graph 1 → 3 → 4 → 7、重复 `/clock`、`/joint_states` 发布者、残留 effort 驱动）已消失并被验收证据取代。

| 检查 | 本次结果 |
|---|---|
| 环境检查 `check_isaac_env.sh` | PASS，四项已知 warning，无新增冲突 |
| 启动器参数测试 | PASS，7 项 |
| domain preflight 判定测试 | PASS，9 项（含 §6 六个 CASE；CASE 2 = `PASS_WITH_IGNORED_DIAGNOSTIC_DAEMON`） |
| domain preflight 现场（真实纯诊断 daemon） | `PASS_WITH_IGNORED_DIAGNOSTIC_DAEMON` |
| ROS 新入口启动 | PASS（headless 与 GUI 各一次，`ready` 记录全部启动参数） |
| ROS 控制链 `/clock`、`/joint_states`、0/+5/−5 N | PASS（Δpos ±0.0522 m、Δvel ±0.406 m/s，符号正确） |
| 重复 reset 图生命周期 | `REPEATED_RESET_GRAPH_LIFECYCLE = PASS`（3 次 boundary reset，每次 Graph=1、发布者/订阅者=1） |
| command gate 与 fault-latch | PASS（窗口内命令被消费但不生效；强制 reset 失败时 gate 保持 CLOSED、不自动恢复控制） |
| 新 GUI 入口回归 | PASS（窗口、Stage、截图、±0.375 m 正负方向遥测；人工抽查未执行） |
| RL profile | READY（本轮只跑健康检查与 `--print-config`，未重跑训练） |
| 包版本与源码保护 | PASS（8718 个受保护文件哈希一致，三环境 freeze 一致，`git diff --check` 通过） |
| BASELINE_CUTOVER | **PASS** |

版本配置以 `config/isaac_baseline.env`（本机历史引用：`../config/isaac_baseline.env`） 为准。本文的版本表是验收快照，不是第二份配置。

| 项目 | 目标 |
|---|---|
| 环境 | `isaaclab232_test` |
| Python | 3.11，当前 3.11.16 |
| Isaac Sim | 5.1.0.0 |
| Isaac Lab | v2.3.2，`37ddf626871758333d6ed89cf64ad702aef127d0` |
| Torch / torchvision / torchaudio | 2.7.0+cu128 / 0.22.0+cu128 / 2.7.0+cu128 |
| RSL-RL | 3.1.2 |
| ROS | Jazzy，Fast DDS，domain 63 |

## 本轮最小修改

两处功能缺陷修复 + 一处生命周期修复 + 两处归位/可观测性，均已给出 diff 并验证：

1. `sim/scripts/run_ros_cartpole.py` 的 `zero_effort()`：`SingleArticulation.set_joint_efforts` 收到 numpy 数组时抛 `TypeError: unsqueeze(): argument 'input' (position 1) must be Tensor`，使 ROS profile 在 `graph_created` 之后崩溃。改为在 articulation view 所在设备上的 torch tensor。必须改：否则 ROS 入口永远到不了 `ready`。没有更大改动：调用位置、PhysX 目标语义和 Lab buffer 清理都保持原样。
2. `sim/scripts/run_ros_cartpole.py` 的 `reset()`：用 `self.graph` 对象加相对节点名做后续 OmniGraph 编辑无法解析（实测挂起，或 `OmniGraphError: Could not find OmniGraph node ... 'Controller'`），第一次边界就带崩进程。改为以 graph path 作为编辑目标、属性使用全路径限定。
3. **图生命周期修复（本轮唯一 blocker）**：原 `reset()` 每次边界都删除 Subscribe 节点并 `create_graph()` 新建 UUID 图，导致重复 Graph/发布者与残留 effort。现在 `create_graph()` 只在 `initialize()` 调用一次并带一次性守卫；`reset()` 不创建、不删除任何节点，只用最小 command gate 关闭/恢复命令接受：
   - 闸门实现（Fallback 1，双闸门）：reset 期间断开 `Tick.outputs:step → Subscribe.inputs:execIn` 与 `Subscribe.outputs:execOut → Controller.inputs:execIn`；DDS 订阅本身全程保活，`/joint_command` subscriber 恒为 1。
   - 恢复顺序：先接回 Tick→Subscribe 并推进一步，让订阅节点消费窗口内收到的消息（此时 exec 仍断开，因此不会施加），再接回 Subscribe→Controller。因此 reset 窗口内的命令不会被消费后自动生效。
   - 只有 reset 全流程成功且状态验证（AstrEX graph prim = 1、`graph_path` 未变、`graph_creations` = 1、位置/速度≈初始、articulation effort target 清零、`simulation_time` 不倒退）通过后才恢复闸门。
   - fault-latch：任何 reset 例外或验证失败都再次 `zero_effort`、保持闸门 CLOSED、写 `reset_failed`/`fault.json`、不再尝试 reset、不自动恢复控制。
4. `sim/scripts/ros_domain_check.py`：observability-only，新增 `PASS_WITH_IGNORED_DIAGNOSTIC_DAEMON` 状态与 ignored/blocked 明细（name、namespace、publishers、subscribers、services、clients、actions、reason）。白名单、5 秒窗口、末段稳定性判定和全部 BLOCK 条件未改。
5. 归位：`scripts/lib/ros_domain_check.py` → `sim/scripts/ros_domain_check.py`；`sim/scripts/ros_cartpole.py` → `sim/scripts/run_ros_cartpole.py`；`scripts/lib/isaac_entry.py` 只改这两处路径。顶层 `scripts/` 仍然只暴露三个入口。

可观测性（仅记录，不改变控制语义）：每次 reset 写 `command_gate_closed` / `command_gate_opened`（含连接状态）、`reset_ready` 记录 `graph_path`、`graph_creations`、`astrex_graph_count`、`command_gate`、`verified` 与检查明细；事件带 `wall_time` 便于与外部 ROS 驱动对时。

## 日常入口

在 AstrEX 根目录运行。无需预先激活 Conda。

```bash
./scripts/check_isaac_env.sh
./scripts/start_isaac_rl.sh
./scripts/start_isaac_ros.sh
```

顶层脚本只是入口；正式实现是 `scripts/lib/isaac_common.sh` + `scripts/lib/isaac_entry.py`，Isaac 场景/图/物理逻辑在 `sim/scripts/`，回归测试在 `tests/isaac/`，用户不需要直接调用这些内部文件。

### RL profile

默认运行官方 Cartpole、16 环境、seed 42、20 updates、headless。仿真和策略均须使用 `cuda:0`。

```bash
./scripts/start_isaac_rl.sh --task Isaac-Cartpole-Direct-v0 --num_envs 128 --max_iterations 40
./scripts/start_isaac_rl.sh --gui
./scripts/start_isaac_rl.sh --print-config
```

### ROS profile

默认打开 1 环境 Cartpole GUI，使用 CPU PhysX、完整 `isaaclab.python.kit`、`clone_in_fabric=False`、`OnPhysicsStep` + `pipelineStageOnDemand`。

```bash
./scripts/start_isaac_ros.sh --headless
```

控制链为 `/joint_command` → 官方 JointState Subscriber → Articulation Controller → PhysX → 官方 JointState Publisher → `/joint_states`，官方 Clock publisher 发布 `/clock`。

图生命周期：ROS OmniGraph 与进程同生命周期，Cartpole reset 与 episode 同生命周期。reset 期间闸门关闭，旧命令不会重放；恢复控制必须依赖 reset 之后的新 ROS 消息。reset 失败会进入 fault-latch（闸门保持关闭、写 `fault.json`、不自动恢复）。

### 外部系统 ROS 终端

使用未激活 Isaac Conda 的独立终端。不要把系统 ROS 的 Python 路径加入 Sim。

```bash
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=63
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
ros2 topic echo /joint_states
ros2 topic pub --once /joint_command sensor_msgs/msg/JointState "{name: ['slider_to_cart'], effort: [5.0]}"
```

## domain preflight 的定位与限制

- 当前严格 domain preflight 是 **cutover 验收门禁**：domain 63 上出现任何非白名单参与者就 BLOCK。
- **不得**把“domain 中存在其他 ROS node”永久当作日常开发 blocker。
- 日常策略目标是以 endpoint conflict 为核心：只有 `/clock`、`/joint_states`、`/joint_command`、controller/action/service 出现实际冲突才阻断。本轮没有实现该扩展，仅在此登记为限制与后续方向。
- 实测例证：上一次验收残留的 Sim 进程（其 7 个图的 Clock/Joint/Subscribe 节点仍在 domain 中）会让新入口直接拒绝启动。这类“自己留下的残留节点”正是需要 endpoint-conflict 策略的场景。
- 依然禁止：`ros2 daemon stop`、kill daemon、修改 daemon、换 `ROS_DOMAIN_ID` 规避。

## reset 语义：意图、现状与门槛

意图：cart 超出位置边界或 pole 角度超过 ±π/2 时，先关闭 command gate，清零 effort target，GUI 保留 0.3 秒边界画面，然后 reset 到零位姿并再次清零；状态验证通过后恢复闸门，等待 reset 之后的新命令。旧命令不得重放，仿真时钟不得倒退，图与发布者不得重复。

本次实测（`artifacts/boundary_reset_f1/boundary_reset_result.json`，`REPEATED_RESET_GRAPH_LIFECYCLE = PASS`）：

- 三个分支（cart 边界、pole 正、pole 负）全部通过：判定原因精确匹配、GUI 保持 0.34 秒、状态归零、`graph_path` 全程不变、`graph_creations = 1`、AstrEX graph prim = 1（`/Replicator/SDGPipeline` 等其它 OmniGraph 另记不计入断言）、闸门事件顺序与连接状态正确、静默窗口 2.167 秒 cart 位移与速度均为 0、articulation effort target = 0、同值重发可控（±0.220 m / ±0.594 m/s）、反向命令可控、`/clock` 全程严格递增。
- reset 窗口专项（cart 边界分支）：闸门闭合期间故意发布 +5 N（窗口内 6 条 publish，恢复后 0 条）。订阅节点确实收到并锁存了该命令（`subscriber_effort = [5.0]`），但 articulation effort target 仍为 0、cart 静止 → **窗口内命令被消费但不生效**，恢复控制必须依赖之后的新消息。
- fault-latch 专项：测试侧强制让状态验证失败后，闸门保持 CLOSED、写入 `reset_failed` 与 `fault.json`、不写 `reset_ready`；随后发布的 +5 N 被忽略（articulation target 0、位移 0）。

applied/computed effort：PhysX applied effort 仍无法确认只反映 ROS→Controller 路径，日志中继续记 N/A；reset 的“target 清零”以 articulation API 的回读值为准，功能 gate 以位移/速度静止、articulation target 为 0、无窗口命令生效与命令恢复为准。

普通运行期间没有命令超时机制；停止运动时发送零命令。本功能只用于仿真演示，不是完整安全控制器。

## 输出与互斥

每次运行创建独立目录 `/data/shared/AstrEX_project_data/logs/isaac/<rl或ros>/<UTC时间_UUID>/`，终端打印位置。`health.json`、`manifest.json`、`console.log`、`domain_check.txt`、`ros_events.jsonl`、`ros_samples.jsonl` 都在该目录。RL checkpoint 位于该目录下 `logs/rsl_rl/`。不要把这些产物提交到 Git。两个正式入口共用项目锁。

## 能力与限制

- 环境检查、启动器参数、domain preflight（判定测试 + 真实 daemon 现场）、ROS 启动、控制链（0/+5/−5 N）、重复 reset 图生命周期 + 闸门 + fault-latch、新 GUI 入口均已取得本次证据；BASELINE_CUTOVER = PASS。
- README、目录指南、环境历史已指向本基线；四份历史报告的结论未改，只在文首加 `STATUS: HISTORICAL` 与当前基线链接。
- 本基线的 ROS 用法仍是单环境 CPU PhysX 演示链路：reset 后必须依赖新命令恢复控制；窗口内消息不保证保留。多环境 GPU ROS、复杂 RTX、长训练仍未验证。
- 历史证据继续有效且不重跑：effort 1/16、100 updates、checkpoint reload、600-step evaluation、10 分钟 ROS 稳定性、人工 GUI 验收。
- Known issues 不继续研究：default headless 建图根因、GPU PhysX + ROS Bridge、十分钟 RSS 轻微增长、四项 pip metadata 冲突、Isaac6 effort、Fabric cloner warning。
- 不覆盖复杂 RTX、多机器人、GPU ROS 控制、长训练、自定义 ROS 接口或完整 AstrBotEX 接入。

## 保护与撤销

不要在此环境安装 AstrBotEX requirements；AstrBotEX 保持自己的 `.venv`，系统 ROS 保持自己的 Python。禁止随意升级 Sim、Lab、Torch、CUDA 或已验证依赖；未来升级使用新环境、新源码目录和独立验收。

本次保护检查：三环境 `pip freeze`、目标环境 conda export、三份 Lab 的 HEAD/status、8718 个受保护文件 SHA256 全部与上一轮快照一致；`git diff --check` 通过；没有 stage、commit、push；没有日志/checkpoint 进入 Git。

## 本次证据

验证目录：`/data/shared/AstrEX_project_data/logs/isaac/cutover_20260917T103433Z_6f2a91/`

| 内容 | 位置 |
|---|---|
| preflight 判定测试（9 项） | `preflight/unit_tests_final.txt`；现场：`../ros/20260917T105808Z_54d64da321/domain_check.txt` |
| 重复 reset / 闸门 / fault-latch 验收 | `artifacts/boundary_reset_f1/boundary_reset_result.json` |
| 归因实验（无窗口命令，Primary 闸门单独表现） | `artifacts/attribution_noburst/` |
| headless 控制验收（PASS，8/8 检查） | `../ros/20260917T121134Z_b61fc64946/control_result.json` |
| GUI 入口验收（含截图与方向遥测） | `../ros/20260917T121233Z_0544f3249b/` 与 `artifacts/gui_final/` |
| 保护快照对比 | `protection/protection_result_final.json` |
| 状态审计 / 早退归档 / 诊断脚本 | `method/` |

历史早退（17:21、17:23 两次运行）已归档为 `HISTORICAL_EARLY_EXIT_CAUSE_UNKNOWN`：只读排查确认两次都在场景/图初始化后被 Kit 侧优雅关闭，无边界事件、无 traceback、无信号日志；本次复现并定位到同一个 `zero_effort`/`reset` 缺陷后，该归档不再影响验收。
