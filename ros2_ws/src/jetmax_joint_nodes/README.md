# JetMax 独立关节节点

四个 ROS 2 节点在一个进程内运行，共享一个 UART/GPIO/I²C 访问实例。硬件操作只由一个工作线程执行，ROS 回调不直接读写总线。

目标环境：Jetson Nano、Ubuntu 22.04、ROS 2 Humble、L4T 32.7.6。电脑上的 ROS 2 Jazzy 用于构建和离线测试。当前包不包含 EX、JEV、视觉闭环、CBF 或运动学换算。

## 接口与单位

所有路径以 `/jetmax_hw/` 开头。

| 节点 | 硬件 | Action | Goal |
|---|---|---|---|
| `arm_base` | 总线 ID1 | `arm_base/move`，`MoveServoRaw` | `target_raw`：绝对原始刻度；`duration_ms`：运动时间 |
| `arm_shoulder` | 总线 ID2 | `arm_shoulder/move`，`MoveServoRaw` | 同上 |
| `arm_elbow` | 总线 ID3 | `arm_elbow/move`，`MoveServoRaw` | 同上 |
| `gripper` | PWM2 | `gripper/set_angle`，`SetGripperAngle` | `target_deg`：绝对 PWM 命令角度 |

每个节点还提供 `<节点>/state` Topic 和 `<节点>/stop` Service。所有类型位于 `jetmax_joint_interfaces`。

ID1/2/3 已通过目视点动对应到底座转台、大臂、小臂。PWM2 命令增大为向内夹，减小为向外开。这里的底座是机械臂转台，不是移动底盘。PWM1、M1/M2/M3 不提供入口。

同一关节忙时拒绝新目标，不排队、不替换。不同关节可以并行运动；总线事务仍按顺序发送，不保证同时起动。首版仅支持绝对目标。

## 状态与结果

- 臂体 `position_raw` 来自舵机位置寄存器，只有 `feedback_valid=true` 时可作为当前反馈。没有有效值时数值字段的零是占位，不是位置零点。
- `received_at` 是主机收包的系统时间；新鲜度和期限由单调时钟计算。没有硬件采样时间戳，`query_duration_ms` 和 `receipt_age_ms` 均不等于传感器数据年龄。协议没有事务编号，极晚到达的同 ID 同类型回包无法完全辨别。
- 臂体只有在写入后取得连续有效回读、在容差内保持配置的窗口时返回 `target_observed`。它表示原始位置到达目标窗口，不证明整机任务成功、角度已标定或物体被抓住。
- 夹爪返回 `command_written`，只证明 PWM2 写入调用正常结束。板端使用整数角度，浮点目标取最近整数；`last_written_deg` 给出实际写入值。夹爪无真实角度、开度或力反馈；相应有效性为 false，未知浮点值为 NaN。
- `command_written`、`target_raw/target_deg`、`last_written_deg` 都是命令信息，不作为实测状态。没有向标准弧度 `JointState` 或米制 `GripperCommand` 填入原始量。
- 错误结果包括 `timeout`、`feedback_lost`、`io_error`、`shutdown`。ROS 拒绝 Goal 时会在节点日志注明原因；不会夹紧到范围边界后继续执行。

取消仅作用于该关节。`stop` 响应只确认请求受理，随后查询 `state.stop_state`：

| 状态 | 含义 |
|---|---|
| `withdrawn_before_write` | 尚未开始写入的目标已经撤销 |
| `requested` | 臂体停止帧等待发送 |
| `stop_written_unconfirmed` | 停止帧写入完成，物理停止尚未确认 |
| `write_failed` | 停止帧写入失败或结果不确定 |
| `physical_stop_unsupported` | PWM2 无独立、已验证的物理停止接口 |

`physical_stop_confirmed` 在本版始终为 false，缓慢漂移不能被停止帧或短时间窗口掩盖。停止不卸力、不松爪。PWM2 已写入的目标可能继续执行；取消无法撤回进入驱动的写入。显式 `stop` 服务终止活动 Goal 时，ROS 状态为 ABORTED、业务结果为 `cancelled`；通过 Action 取消时为 CANCELED。

客户端掉线不自动等于取消，正在执行的有限目标受本地期限管理；需要停止时使用取消或 `stop`。重连只查询结果，结果不确定时不能重发运动。节点重启不恢复旧 Goal。UART/I²C 写入超时会锁定整个会话，不自动重试；Python 不能撤回卡在内核中的 I²C 调用，也不会为它再开一个写入线程。

## 构建与离线试用

只选择这两个包构建，避免加载现有仿真控制入口。使用系统 Python，不使用 conda Python。以下命令从仓库根目录开始；Nano 上将 Jazzy 的环境文件换为 `/opt/ros/humble/setup.bash`：

```bash
cd ros2_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select jetmax_joint_interfaces jetmax_joint_nodes \
  --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3
source install/setup.bash
export ROS_DOMAIN_ID=72
ros2 launch jetmax_joint_nodes joints.launch.py backend:=fake enable_control:=true
```

下面的数值只用于 `fake.yaml` 测试后端，不能作为真机范围。另开已加载同一安装目录且同为 domain 72 的终端：

```bash
ros2 node list
ros2 topic echo /jetmax_hw/arm_shoulder/state
ros2 action send_goal /jetmax_hw/arm_shoulder/move \
  jetmax_joint_interfaces/action/MoveServoRaw '{target_raw: 550, duration_ms: 1000}' --feedback
ros2 action send_goal /jetmax_hw/gripper/set_angle \
  jetmax_joint_interfaces/action/SetGripperAngle '{target_deg: 75.0}' --feedback
```

取消某个关节的当前 Goal，或用 `state.goal_id` 限定取消目标：

```bash
ros2 run jetmax_joint_nodes cancel_joint arm_shoulder
ros2 run jetmax_joint_nodes cancel_joint arm_shoulder --goal-id <state.goal_id>
ros2 service call /jetmax_hw/arm_shoulder/stop jetmax_joint_interfaces/srv/StopJoint '{}'
```

取消程序使用标准 Action 的 CancelGoal 服务，不需要新增 EX 客户端。每个 Action 保留结果 60 秒用于断连后的查询；超过缓存时间应报告结果未知，不能自动重发。

```bash
colcon test --packages-select jetmax_joint_interfaces jetmax_joint_nodes --event-handlers console_direct+
colcon test-result --verbose
```

测试执行真实 ROS 图和 Action 通信，底层为显式 fake。没有连接设备、升级系统或修改启动服务。

## 真机配置与后续验收

`real.yaml` 保存已有证据支持的接线和回读参数。UART 为 `/dev/ttyTHS1`、115200；方向为 Tegra GPIO 50/14（BCM17/27）；PWM2 为 I²C1、0x36、寄存器22。原生位置查询保持收发并行接收与校验回显的方式。本次已完成局部范围内的单关节实机验收；完整范围、负载与实机并行控制仍需后续验证。

默认 backend 为 real、控制关闭。导入模块不打开硬件；显式启动才申请设备，启动不回零、不输出初始位置。默认关闭控制时 `stop` 也不发送停止帧。先停止旧硬件控制进程，确保没有预先打开的 UART/I²C 使用者；TIOCEXCL 和会话锁不能阻止不遵守锁且已持有句柄的旧程序。

真机启动应使用 Nano 的 Humble 环境与重新编译的原生库。`fake.yaml` 带有测试标识，真实后端拒绝加载它。真机运动前需在独立配置中填入每关节确认过的 `limits`、原始位置 `tolerance_raw` 与 `settled_ms`；缺失时可继续回读，对应关节拒绝运动。PWM2 的角度限值也需确认。运动时间协议限制为 10–30000 ms；它不是确认过的整机速度范围。

本机测试不替代 Nano/Humble 构建、UART/IOCTL 兼容性、实际位置回读、并行运动及取消/停止验收。不自动部署、启用自启动或把历史整卡备份中的配置写回设备。

## 初版离线验收记录

2026-11-01，从项目内源码在 Ubuntu 24.04／ROS 2 Jazzy、系统 Python 3.12 下构建两个包成功。30 项用例全部通过：执行与取消逻辑21项、协议与原生查询7项、四节点 ROS Action 通信1项、跨进程 launch 与命令行取消1项。`colcon test-result` 连同4个测试组入口计为34项，错误、失败和跳过均为0。

覆盖了单总线所有权、跨关节并行、同关节拒绝重复目标、取消互不影响、反馈缺失、写入超时不重试、夹爪命令与实测分离，以及正常退出。构建与测试日志保留于本机 `/tmp/jetmax_joint_project_check/`，不是运行依赖。

以上为初版离线记录。后续已在 Nano 完成 Humble 构建和小范围实机检查，见下节。

## 2026-11-01 真机补充验收

Jazzy 与 Nano/Humble 最终各 48 个 pytest 用例通过，colcon 含 6 个测试入口计为 54 项，均无错误、失败或跳过。三个臂体关节的往返、停止等待后新 Goal 继续、底座原生取消、主动期限超时，以及 PWM2 开合已取得对应软件证据与现场确认。

底座、大臂使用 1 秒期限；小臂的 1 秒测试未捕获位移，保留为未通过，经用户确认改为 2 秒后重测通过。大臂超时的现场确认因终端笔误而由聊天补录；原始记录保留。停止观察仅覆盖本次空载小范围及约 3 秒窗口，不证明永久静止或全工作空间安全。

完整结果、局部配置和原始事件见[实机验收记录](../../../docs/AstrEX_workingTree/jetmax_real_control/acceptance/20261101-SvhtPO/RESULT.md)。验收进程已经退出，期限注入已关闭。夹爪实测开度仍不可用，角度标定、负载任务和实机并行尚未验证。

## 最小实机验收客户端

`joint_acceptance` 只调用 ROS 接口，不访问 UART/GPIO/I²C。状态、Goal、Result 和现场观察追加到指定目录的 `events.jsonl`；日志中的主机接收时间不表示传感器采样时间。

先在 Nano/Humble 独立工作空间构建这两个包。启动只读节点后运行：

```bash
ros2 run jetmax_joint_nodes joint_acceptance --case baseline --output-dir ./evidence
```

它等待状态就绪，再采集 60 秒；要求三个臂体关节无失效回读、静止波动不超过 3 刻度、最大接收间隔小于 250 ms。`baseline.json` 提供每关节末段位置中位数 `p0`。现场确认 `p0` 到 `p0+40` 路径后，在独立 real 配置中填写局部 limits、`tolerance_raw: 5`、`settled_ms: 400`。夹爪窗口为 45–75 命令度，仍需确认当前安装允许这段行程。

现场已确认路径后，使用该配置显式启用节点控制，再逐个运行验收项。启动后先提示并等待 3 秒，然后执行首个目标；只在动作之后询问实际效果，不重复询问启动授权。未确认效果或出现异常时不会继续下一次运动：

```bash
ros2 run jetmax_joint_nodes joint_acceptance --case reach --joint arm_base --config ./acceptance.yaml --output-dir ./evidence
ros2 run jetmax_joint_nodes joint_acceptance --case stop_wait --joint arm_base --config ./acceptance.yaml --output-dir ./evidence
ros2 run jetmax_joint_nodes joint_acceptance --case cancel --joint arm_base --config ./acceptance.yaml --output-dir ./evidence
```

`reach` 在局部窗口运动并返回下端点；`stop_wait` 中途停止，留出 0.5 秒后观察 3 秒，确认后用新 Goal 继续；`cancel` 验证原生 Action 取消，保持停止后位置。大臂、小臂使用对应节点名。客户端始终检查未选关节的回读变化。

可选 `goal_timeout_cap_s` 默认缺省或 null，仅缩短三个臂体关节从 Goal 接受起的期限，不改变运动时间、不作用于夹爪。正常测试不设置它。空闲时将验收配置改为 `goal_timeout_cap_s: 1.0`，重启节点，再运行 `--case timeout`。它提交 4 秒运动，要求捕获已写入、可分辨位移、`timeout` 结果和停止帧状态。客户端也支持经现场明确确认的 2 秒期限，用于本次小臂在 1 秒内未观察到位移后的重测；这不改变目标或运动时间。只在排队中到期不能通过运动中超时验收。完成后关闭该配置，不自动继续或恢复原目标。

夹爪使用 `--case gripper`，只核对 45°打开、75°闭合的命令与现场反应。它没有物理停止或开度测量能力。`--fake-no-observer` 只能配合显式 fake 后端用于离线测试，real 后端禁止绕过现场观察。

停止窗口极差上限 5 刻度、首尾半秒中位数差上限 2 刻度，只表示本次窗口的有限观察；`physical_stop_confirmed` 始终为 false。任何失败只尝试停止当前关节，不自动返回起点。

## 来源

`SOURCE_PROVENANCE.json` 记录提取文件及源 SHA256。位置查询 C 源码、两根方向引脚适配器和校验函数来自已有 candidate-v8 验证副本；包从源码构建原生库，不携带 `.so`。协议编解码仅提取无硬件副作用的部分。每关节状态机、总线调度和 ROS 接口为本次新增。

没有复制旧 SDK 的自动回零、原控制进程全局取消状态、模型服务或 SSH 操作脚本。新包运行不依赖 `/data/shared`；历史备份与验证副本保持不变。
