# P1 仿真传感器链第一阶段结果

记录日期：2026-10-04。范围：完全基于 Isaac Sim 的 RGB＋原生 2D／3D 雷达与真实 ROS 2 通信。

**P1 传感器链通过。原生雷达输出尚未达到配置的完整 10 Hz，尤其 3D 点云仅为 5.500 Hz。** 本报告不把传感器链通过写成定位精度、抓放或实时控制通过。

## 1. 完成范围与验收口径

[主计划第 22.3 节](MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md#223-分阶段实施与验收)要求两配置各连续采集至少 30 秒，并关联消息、时间、坐标和配置。只有一个节点发布 `/clock`。本轮用至少 30 **仿真秒**的真实 ROS 消息窗口验收，墙钟耗时单独记录。

| 项目 | 结果 |
|---|---|
| Isaac 原生 RGB／CameraInfo、LaserScan／PointCloud2 | 已创建并经 ROS 2 接收 |
| 两配置各至少 30 仿真秒的采集窗口 | 通过 |
| CameraInfo、TF、时间戳、配置 hash 和原始帧索引 | 已记录 |
| 唯一时钟发布者 | `/astrex_mm_sim`，未发现第二个发布者 |
| 首次 GUI 与 ROS 收到的 RGB 对照 | 用户确认后才开始正式采集 |
| 2D／3D 雷达精确达到名义 10 Hz | 未通过此性能目标 |
| P2 颜色／轮廓定位、米制位姿、障碍提取、SceneSnapshot | 未实现 |
| P3 五布局配对误差与覆盖比较 | 未运行 |
| 视觉精度对任务成功率的研究 | P1 不提供该结论 |
| M1 实际绕障导航 | 随车静止链已实测；反馈时序仍有缺口，未提交导航 Goal |

代码使用同一[Isaac 入口](../sim/scripts/run_mobile_manipulation.py)、[场景模块](../sim/mobile_manipulation/scene.py)、[隔离启动脚本](../scripts/start_mobile_manipulation_sim.sh)和[ROS 采集器](../scripts/capture_mobile_sensors.py)。传感器采集未加载机器人，也未发送运动命令。

## 2. 环境与固定配置

Isaac Sim 5.1、Isaac Lab 2.3.2。Isaac 使用其环境内的 Python 3.11 与内置 Jazzy bridge。采集器使用系统 ROS 2 Jazzy 和 Python 3.12。两进程使用 `ROS_DOMAIN_ID=63` 与 `rmw_fastrtps_cpp`。没有将系统 ROS Python 路径混入 Isaac。

| 参数 | 两配置的共同值 |
|---|---|
| 场景与物体 | 地面、实体操作台、5 cm 红色方块、5 cm 蓝色圆柱、绿色放置区、3 个地面箱体 |
| 场景种子 | `101` |
| RGB | 640×480，配置 15 Hz |
| 物理／渲染步频 | 120 Hz／60 Hz |
| 雷达扫描配置 | `scanRateBaseHz=10`，最终 `tickRate=60`，完整扫描发布 |
| 固定相机位置 | world 中 `[1.25, -1.25, 1.60] m` |
| 固定雷达位置 | world 中 `[0.0, -0.8, 0.30] m` |
| 标定标识 | `fixed_rig_v1` |
| 数据来源 | `RAW_SENSOR`，`perception_backend=null` |
| 帧名 | `mm_camera_optical`、`mm_lidar`，共同父帧 `world` |

2D 配置实际加载 `Example_Rotary_2D`，发布 `/astrex/mm/scan`。3D 配置实际加载 `Example_Rotary`，发布 `/astrex/mm/points`。2D 结果不是从点云投影出的扫描。

两个原生示例的 `nearRangeM=1.0`、`farRangeM=200.0`。这些是本轮真实参数，近距离盲区不能忽略。原生 `rangeAccuracyM≈0.02` 是配置属性，不是本项目测得的定位误差。后续近场任务必须另行核对量程、点数和精度。

相机没有向感知端提供深度图、实例 mask 或语义标签。雷达原生输出坐标为 `SENSOR`。固定传感器没有启用 MotionBVH，启动日志保留其警告。移动配置将启用 MotionBVH，并重新检查随车外参。

配置证据：[最终 2D 配置](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/simulation_config.json)、[2D 标定](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/calibration.json)、[最终 3D 配置](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/simulation_config.json)、[3D 标定](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/calibration.json)。

## 3. 正式采集结果

频率按相邻消息计算：`(count - 1) / 首末时间跨度`。仿真频率使用消息时间戳，墙钟频率使用接收时的单调时钟。

| 数据流 | 2D 消息数 | 2D 仿真跨度 s | 2D 仿真 Hz | 2D 墙钟 Hz | 3D 消息数 | 3D 仿真跨度 s | 3D 仿真 Hz | 3D 墙钟 Hz |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| RGB | 455 | 30.267 | 15.000 | 4.782 | 455 | 30.267 | 15.000 | 5.742 |
| CameraInfo | 454 | 30.200 | 15.000 | 4.771 | 455 | 30.267 | 15.000 | 5.741 |
| 雷达 | 288 | 30.183 | **9.509** | 3.028 | 166 | 30.000 | **5.500** | 2.106 |
| clock | 1819 | 30.300 | 60.000 | 19.114 | 1826 | 30.417 | 60.000 | 22.953 |

| 结果维度 | 最终 2D | 最终 3D |
|---|---|---|
| 原计划 P1 传感器链验收 | PASS | PASS |
| 采集进程墙钟总耗时 | 95.305 s | 86.387 s |
| 采集窗口 RTF | 0.319 | 0.383 |
| 雷达最大相邻仿真时间间隔 | 200.000011 ms | **800.000042 ms** |
| 非递增时间戳数 | 各数据流均为 0 | 各数据流均为 0 |
| RGB 最近帧配对，诊断阈值 50 ms | 287/288，最大 50.000003 ms | 166/166，最大 33.333335 ms |
| 额外频率诊断 | 实测比名义 10 Hz 低约 4.9% | 实测比名义 10 Hz 低约 45% |

RTF 使用同一 clock 消息窗口的“仿真跨度／墙钟跨度”。等价计算为 `clock.wall_hz / clock.simulation_hz`。采集进程总耗时包含启动、发现和等待，因此不能直接用总耗时替代该窗口的墙钟跨度。

2D 有一帧的配对差超过 50 ms 约 3 ns。报告保留原计数，没有改写阈值或原始时间戳。该阈值是数据配对诊断，不是原计划规定的业务通过线。

3D 的 0.8 仿真秒间隙必须保留。当前证据说明数据链可用，不能据此宣称 3D 满足 10 Hz 或后续控制的观测年龄要求。

正式结果：[2D capture_result.json](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/formal/capture_result.json)、[3D capture_result.json](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/formal/capture_result.json)。

## 4. 首轮失败、源端调整与验收口径修正

以下原始结果全部保留，没有用后续 PASS 覆盖。

| 记录 | 当时结果 | 处理与边界 |
|---|---|---|
| 首次 3 秒预览 | RGB 实收 11 Hz，FAIL | 调整记录器首帧 PNG 处理和 RGB 订阅 QoS。它不是正式 30 秒验收 |
| 同一 Isaac 进程的 5 秒预览 | RGB 15 Hz、雷达 9.4 Hz，PREVIEW_PASS | 只确认采集器调整，仍不算 P1 完成 |
| 首次 30 秒 2D | 雷达 **8.467 Hz**，原文件保持 FAIL | 唯一 errors 项是额外频率门槛。其最大扫描间隔为 316.667 ms |
| 并行 QoS 诊断 | reliable 与 best_effort 都收到 45 帧／5.083 仿真秒 | 两者间隙分布相同，不支持“仅接收 QoS 丢帧”的解释 |
| 一次原生源端调整 | `tickRate` 从 10 改为 60，`scanRateBaseHz` 保持 10 | 完整扫描不再额外抽帧。新采集的 2D 为 9.509 Hz |
| 最终 3D | 5.500 Hz，保留频率诊断 | 没有继续调频或重复筛选直到通过 |

最初采集器自行加入“实测频率偏差不超过 ±15%”的硬门槛。原计划 P1 没有这项业务门槛。执行中核对计划后，将该门槛撤出 P1 通过判断，只保留为额外频率诊断。

这次口径修正没有修改雷达目标、原始数据或首轮 FAIL。两类结论分别记录：**原计划的数据链验收通过；名义频率性能没有全部达到。** 后续任务若需要 10 Hz，必须单独解决并验收，不能沿用 P1 PASS 作为证明。

证据：[首次预览](evidence/source_runs/phase1_20261004T160950+0800/p1_preview_2d/ros_preview/capture_result.json)、[修正记录器后的预览](evidence/source_runs/phase1_20261004T160950+0800/p1_preview_2d/ros_preview_after_recorder_fix/capture_result.json)、[首轮 8.467 Hz FAIL](evidence/source_runs/phase1_20261004T160950+0800/p1_formal_2d/capture_result.json)、[QoS 同窗诊断](evidence/source_runs/phase1_20261004T160950+0800/p1_lidar_qos_probe.json)。

## 5. GUI、原始数据和关联方式

首次实际 GUI 与通过 ROS 收到的 RGB 已在会话中展示。用户确认视角后，才开始两配置的正式采集。确认是本轮会话记录，不是用截图自动推断出的批准。

- [首次 Isaac GUI](evidence/source_runs/phase1_20261004T160950+0800/p1_preview_2d/gui_preview.png)
- [首次 ROS RGB 图像](evidence/source_runs/phase1_20261004T160950+0800/p1_preview_2d/ros_preview/camera_preview.png)
- [正式 2D RGB 图像](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/formal/camera_preview.png)
- [正式 3D RGB 图像](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/formal/camera_preview.png)

每次采集的 `messages.jsonl` 保存全部收到消息的时间与帧信息。每种传感器先保存首帧，再保存距上次已保存帧至少 1 仿真秒的首个新帧。没有按图像质量选帧。

| 目录 | 消息索引行数 | 原始 RGB | 原始 CameraInfo | 原始雷达 |
|---|---:|---:|---:|---:|
| 最终 2D | 3016 | 31 | 31 | 30 |
| 最终 3D | 2902 | 31 | 31 | 28 |

原始帧使用对应 ROS 消息的 CDR 序列化格式。索引中的 `raw_path` 相对于采集目录。已检查：两个正式目录中，索引引用的原始文件全部存在。该交付是“全部消息元数据＋有索引的选定原始帧”，不是保存每一帧的完整 rosbag。

原始索引：[2D messages.jsonl](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/formal/messages.jsonl)、[3D messages.jsonl](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/formal/messages.jsonl)。原始帧目录：2D raw（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/p1_tick60_2d/formal/raw`）、3D raw（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/p1_final_3d/formal/raw`）。

消息使用 `stamp_ns`、`received_monotonic_ns`、`frame_id` 关联。清单保存配置 hash、场景种子和传感器类型。最终 2D hash 为 `80cb8c331794bdbbf71a7d04b1db1e7714449b84f7e84ab468110d3e657213b2`。最终 3D hash 为 `136960d1f3e3c4ef6d3699bd39d055d1d5372bc795456c0ecf90f08114649f25`。

## 6. 冷启动、退出问题与保留警告

首次 Isaac 进程持续等待完成。日志记录 Simulation App Startup Complete 为 **648.387 s**，场景就绪约 **699.1 s**。没有将首次 I/O 等待当作环境失败，也没有重新安装 PyTorch。后续热启动约 10 秒。

首次旧入口在 SIGINT 后出现 `publisher's context is invalid`。随后再次调用 shutdown，出现 `rcl_shutdown already called`。该进程退出码为 139。原因与旧入口中 rclpy 覆盖信号处理、提前关闭 context 的行为一致。

入口改为 `SignalHandlerOptions.NO`、`try_shutdown()` 和 `stop_requested` 文件退出。之后 2D 通过窗口关闭退出，3D 通过停止文件退出，两次执行器回执均为退出码 0。

- 首次启动与退出错误日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/p1_preview_2d/simulation.log:350`）
- 最终 2D 正常关闭日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/p1_tick60_2d/simulation.log:780`）
- 最终 3D 正常关闭日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/p1_final_3d/simulation.log:372`）
- [3D simulation_result.json](evidence/source_runs/phase1_20261004T160950+0800/p1_final_3d/simulation_result.json)
- [当前统一入口的退出处理](../sim/scripts/run_mobile_manipulation.py)

两次正常退出的执行会话分别为 `98607`、`18489`。首次异常退出的执行会话为 `99331`。退出码来自执行工具回执，现有日志文件本身不包含该字段。

2D 关闭窗口时，timeline 已复位，因此 [2D simulation_result.json](evidence/source_runs/phase1_20261004T160950+0800/p1_tick60_2d/simulation_result.json)中的 `simulation_seconds=0` 不能代表采集窗口。P1 时长依据保存的消息时间戳。3D 的停止文件退出没有设置信号标志，所以 `requested_stop=false` 也不代表无退出请求。这两个运行尾记录的局限已保留。

CameraInfo 的物理畸变模型警告、静止雷达的 MotionBVH 警告及原生性能提示仍留在日志中。P1 不将“未抛异常”等同于噪声、覆盖或精度合格。

## 7. 本机代码检查

运行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/isaac -p 'test_mobile*py'
```

当时结果：**12 项通过**。其中 5 项覆盖传感器配置、时间统计、时间戳重复和 PNG 内容。另 7 项覆盖 M1 开发代码的导航物理判据与确定性障碍布局。此处保留原始检查次数，不用后来新增的测试改写历史。

统一入口、场景、配置模块和导航验收模块通过 Python AST 语法检查。启动脚本通过 shell 语法检查。纯测试不能替代 P1 的真实采集，也不能替代尚未运行的 M1。

测试代码：[传感器测试](../tests/isaac/test_mobile_sensor_contracts.py)、[导航证据测试](../tests/isaac/test_mobile_navigation_evidence.py)。

## 8. 下一阶段边界与 M1 范围核对

P2 尚未实现颜色定位、平面求交、PnP 或障碍提取。P3 尚未完成五布局误差与覆盖比较。没有任何定位误差 p50／p95 或抓放成功率可由本报告推出。

M1 已准备随车 RGB＋2D 雷达配置、固定随机障碍、正式 EX move Goal 和独立物理评价。M1 尚未启动实际绕障。移动配置使用独立外参 `mobile_rig_v1`，近量程改为 0.1 m，不能把固定支架的 P1 外参直接当作移动验收。

M1 最初草拟为同一布局上的三条连续路线，尚未运行。最终准备方案改为三个独立种子布局 `7301/7302/7303`，每个布局从初始位置绕障到 `table_dock`。三条直线均被障碍挡住；绕行空间仅做过静态核对，不能代替实际 Nav2 验收。每个布局使用独立仿真会话和配置，三份结果全部通过才完成 M1。M4 的 10 个可行种子与 2 个失败种子仍留后续。

运行 M1 前，必须用实际整机投影检查 Nav2 的几何包络。当前半径为 0.48 m，覆盖已核对的车轮几何；固定姿态下机械臂投影约 0.464 m。移动模型加载后仍需检查实际包络、随车 TF、雷达自体遮挡与最大观测间隙。移动时只使用 `map→odom→base_link→sensor`，不启动固定底座的 arm.launch.py。

### 8.1 随车传感器与导航输入的静态核对

随车传感器挂在 `/World/Robot/base_link/SensorRig`。SensorRig 没有额外变换。相机使用 `get_local_pose(camera_axes="ros")` 生成外参，因此公布的是 `base_link→mm_camera_optical`，没有把世界位姿误用作随车外参。

| 核对项 | 源码／配置结果 | 尚需实际核对 |
|---|---|---|
| TF | map→odom 为已知地图对齐；odom→base_link 来自真实轮反馈；base_link→传感器为静态外参 | 车体运动时相机和雷达随体运动，时间与姿态一致 |
| 扫描高度 | base_link 高 0.23 m，雷达相对高 0.20 m，水平扫描面为 0.43 m | 真实扫描面与挂载姿态一致 |
| 底盘自遮挡 | 车体顶约 0.32 m、履带外壳顶约 0.31 m、车轮顶约 0.24 m，均低于扫描面 | 折叠机械臂、夹爪和其他可见几何是否产生近距自身点 |
| 障碍可见性 | 开发布局箱体高 0.50／0.60 m，实体桌面也覆盖扫描高度 | 原始 LaserScan 中实际点数、近距扇区和时间间隙 |
| 近量程 | 移动配置为 0.10 m；没有自行过滤机器人点 | 不能用扩大近盲区的方式掩盖自遮挡 |
| 导航地图 | PGM 只含已知边界；随机障碍由实际 2D 雷达加入 costmap | 不向 Nav2 注入场景真值障碍 |
| 当前停靠方向 | table_dock 的 yaw=0 用于 M1 move 验证；RGB 朝车前下方 | 该方向未保证看到桌面红块，不能据此通过 M2 抓取感知覆盖 |

整个机器人投影半径仍须以移动实例的 `whole_robot_xy_envelope_radius_m` 为准。如果它超过 Nav2 的 0.48 m，须先修正几何包络再发送运动任务。CollisionMonitor 的 0.50 m 圈不能替代规划器对整机尺寸的正确建模。

### 8.2 布局证据关联与补充代码检查

统一运行器的 navigation 模式必须传入 `--simulation-config`，指向当前 Isaac 实例写出的 `simulation_config.json`。验证器核对以下内容：

- 重算有效配置 hash，核对实际种子、完整 scene／robot／execution、机器人 profile 路径和 USD。
- 核对显式传感器参数；M1 必须启用原生 2D 雷达和传感器渲染，不能使用关闭传感器的 ORACLE 控制配置。
- 运行前匹配真实 ROS evaluation truth 中的 simulator_pid、effective_config_hash、profile_hash 和 sim_session。执行期间持续核对这些字段。
- 汇总时重新检查配置文件 SHA256、实际布局种子和会话。不能只改结果中的种子标签，把同一布局当作三个场景。

这些检查用于防止错实例、旧反馈和错文件关联；不是防止人为同时篡改全部本地文件的密码学认证。导航成功还必须满足独立 PhysX 位姿、逐轮反馈、连续停稳、无意外接触和无保护失败。Nav2 返回 succeeded 本身不构成物理成功。

检查次数按实际执行分别保留：

| 执行时点 | 命令／范围 | 结果 |
|---|---|---|
| 最初代码检查 | 第 7 节 discover 命令 | 12 项通过：5 项传感器＋7 项导航 |
| 独立布局汇总与结束碰撞否决加入后 | 同一 discover 范围 | 15 项通过：5 项传感器＋10 项导航 |
| 配置文件与实际实例绑定加入后 | `python3 -B tests/isaac/test_mobile_navigation_evidence.py` | **16 项导航证据测试通过**；本次没有重跑传感器测试 |

最近一次导航测试覆盖：真实配置关联、错种子／障碍几何、错 PID／旧配置 hash、关闭传感器、同布局改标签，以及采集后修改配置文件等情况。统一运行器与导航 helper 的 AST 检查通过。以上均为代码检查，**M1 三个布局的真实导航仍未验收**。

### 8.3 首次移动预览启动失败与最小修复

首次移动预览在创建嵌套 RTX 雷达时失败，尚未到 ready。失败信息为 USD translate 属性要求 `GfVec3d`，实际收到 `vector<VtValue>`。

原生 `IsaacSensorCreateRtxLidar` 接口直接使用传入的 translation。默认 P1 坐标为 tuple，而移动 JSON 覆盖值为 list。修复仅将该参数转换为 `Gf.Vec3d(*map(float, spec.lidar_position))`；坐标数值、传感器分辨率、扫描率和已完成的 P1 证据均未改变。这是参数类型修复，不是调整验收条件。

证据：[首次移动预览 startup_failure.json](evidence/source_runs/phase1_20261004T160950+0800/m1_preview/startup_failure.json)、包含原生类型异常的完整日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_stdout.log`）、[传感器创建实现](../sim/mobile_manipulation/scene.py)。

修复后场景源码通过 AST 检查。未加载 Kit 路径的独立 Isaac Python 无法 import pxr，因此没有将该次独立探针写成原生运行通过，也没有为它启动额外实例。

本次补记时，新预览进程 PID `47019`、执行会话 `1491` 仍在原进程内启动等待。已出现 app ready，但这不是项目场景的 `ASTREX_MM_READY`，更不是 M1 验收。继续等待同一进程完成首次加载，不因 CPU 忙或 I/O 等待重启。后续状态记录在新预览目录（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_typed_sensor`）与启动日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_typed_sensor_stdout.log`）。本段保留当时状态，不预写正式导航通过。

本轮未 commit、未 push。证据目录保持原位，原失败记录没有删除。

### 8.4 移动实例的静止读回

后续修复把接触 API 与刚体包装器初始化移到 reset 前，移动实例持续产生真实物理状态。一次 15 墙钟秒采样证明轮速、根刚体速度、odom/TF 与随车传感器已接通；未启动 Nav2 或发送运动目标。

raw 最大间隔 210.84 ms、最终入口状态最大 240.85 ms，均有超过 200 ms 的记录。雷达最大源时间间隔 300 ms，不能写成 M1 时序通过。一次随车 RGB 截图只看到初始朝向的地面与障碍，未验证停靠后的抓取工作区覆盖。

[只读结果](evidence/source_runs/phase1_20261004T160950+0800/m1_preview_prepared_physics_ros/m1_static_ros/mobile_static_summary.json)、相机与雷达原始帧（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_prepared_physics_ros/sensor_mount`）、[详细失败与修复](MVP_FIRST_STAGE_RESULT.md#42-m1-静止模型与随车传感器)。这些记录不改变前面的 P1 固定支架采集结果，也不替代 M1 三个场景的绕障验收。

随车原始 LaserScan 另有一个需要保留的数据边界：该帧含 3200 个角槽、578 个合法量程命中，其余 2622 个为 `-1`，不是 `+inf`。本机 SDK 的 FlatScan 接口在累计扫描后触发；`fullScan` 参数不影响 LaserScan。完整角域不能证明每个方向都测量成功，当前不把 `-1` 改成自由空间。P1 记录器中的 `finite_ranges` 只计数学有限值，不等于合法量程命中数。后续感知与导航覆盖分析必须按 `range_min/range_max` 过滤。

### 8.5 发布与渲染调整后的开发窗口

M1 后续关闭四项非必需光照效果，保留阴影、亮度、AA、RGB 分辨率与采样频率；同时修复控制状态和真值的发布节流。唯一 15 秒窗口收到 61 条雷达消息，但 raw/final 仍有约 243 ms 的墙钟间隔。该开发窗口不替换 P1 数据，也不证明 M1 时序通过。

Nav2 的五个主节点和两个成本地图随后实际进入 active，雷达在全局静态自由区新增 75 个障碍格；未提交导航 Goal。详细配置、原始采样和尚待人工选择的反馈门限见[第一阶段进展 §4.3](MVP_FIRST_STAGE_RESULT.md#43-发布调度修复与-nav2-只读启动)。


### M1 后续渲染试验（不改变 P1 结论）

RGB render product 按四步更新一次的试验没有产生 RGB／CameraInfo／雷达消息，后续发生原生段错误，已保存源码快照并撤回。恢复后独立的三仿真秒窗口再次收到全部传感器流，RGB／CameraInfo 约 15 Hz，雷达约 9.33 Hz。此项只证明恢复发布，未宣称 M1 的 200 ms 墙钟保护时序通过。完整过程见[第一阶段记录 §4.4](MVP_FIRST_STAGE_RESULT.md#44-未通过的-rgb-渲染节流已撤回)。
