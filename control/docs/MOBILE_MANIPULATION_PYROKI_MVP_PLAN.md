# 移动机械臂仿真 MVP：Laya 技能选择、参数化 Contract 与 MoveIt 2 受限执行

更新：2026-10-04。状态：**P/L/A/R/E/T/M 第一阶段实施中**。用户已授权本轮完成这些阶段及其必要依赖；不 commit、不 push，保留原有暂存内容与其他任务改动。现有 B04/B07/B08/B09 成果按第 8 节复用。后续章节中的阶段目标不能当作已完成结果。

清理状态：CartPole、MoveCart、SpinPole 与原 ROS/RL 演示入口仍归档，不恢复。原 Isaac 环境 `import torch` 已在同一进程等待后成功，未重装依赖，见[环境检查结果](MVP_ENVIRONMENT_AUDIT_RESULT.md)。本轮新增统一传感器／机器人场景入口、ROS 执行网关和试验运行器；程序存在、导入通过、接口通信与物理验收分别记录。归档背景见[清理报告](ISAAC_ROS_CARTPOLE_RETIREMENT_20261003_RESULT.md)。

本轮范围已确认：P1、L1、A1、R1、**E0 零扰动基线**、T1、M1。为完成 E0，补齐 A2/A3、R2/R3 和最小 L2 服务接线。E0 使用 ORACLE，不依赖 P2/P3 或 A4。T1 只冻结 SYNTHETIC 任务／状态组，不训练；P2/P3、E1/E2、Laya 物理技能选择和完整移动抓放仍留后续。P1 已完成，见[传感器结果](MVP_SENSOR_FIRST_STAGE_RESULT.md)；L1/T1 结果及当前模型能力缺口见[语言与数据结果](MVP_LANGUAGE_FIRST_STAGE_RESULT.md)。A/R/E0/M1 尚须相应实际执行证据。M1 的 Nav2 启动与雷达成本地图已通过只读检查，但完整传感器负载仍有超过既定 200 ms 的反馈间隔；反馈门限选择等待人工确认，尚未修改，见[第一阶段进展](MVP_FIRST_STAGE_RESULT.md)。

本轮采用的人工确认接口如下：

| 项目 | 已确认内容 |
|---|---|
| 动作标识 | owner=`mobile_manipulation`；`mobile_manipulation.move.v1`、`mobile_manipulation.fetch.v1` |
| move 参数 | `target_region_id / position_tolerance_m / yaw_tolerance_rad / success_template`；区域 `table_dock/home`；位置容差 .10/.05 m、朝向 10°/5° |
| fetch 参数 | `object_id / place_region_id / placement_tolerance_m / orientation_mode / yaw_tolerance_rad / success_template`；放置区 `tray_left/tray_right`；容差 .020/.010 m、朝向 free 或 yaw 5° |
| 成功模板 | `move.arrive_and_stop.v1`、`fetch.pick_place_stable.v1`；5 mm 放置精度只统计，不先宣称已支持 |
| 夹爪接口 | `ParallelGripperCommand`，驱动关节 rad、effort N·m；米制开口由固定 URDF 连杆与 STL 内侧面几何显式换算 |
| 语言与标签 | 不使用“请”；“把红色方块放到左托盘”和“在左托盘上放红色方块”等价；缺放置位置必须追问，杯子不能替换成方块 |
| 数据隔离 | 同一任务／场景及全部改写只属于一个集合；不同独立场景允许复用语序，不宣称新语法泛化 |

首次机器人／夹爪工作区、通道配置及一次真实停止演示仍按对应章节人工查看。阶段内普通参数调试保留失败证据，研究集合冻结后不为通过而改阈值。

文件名 `MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md` 暂时保留，以兼容施工索引和 B11 的已有链接。PyRoKi 退出本 MVP 必经路径，不安装 JAX/jaxls，不维护双规划后端；以后有专项优化需求再单独立项。旧任务书中的“PyRoKi 单臂基线”在本方案中对应项目 A 的 MoveIt 2 单臂基线。

**本 MVP 完全基于 Isaac 仿真环境。** RGB、2D/3D 激光雷达、机器人、接触、障碍物及评价真值均来自仿真。ROS 2 节点在本机实际运行，命令只驱动 Isaac 中的机器人。文中的“实际执行”“物理结果”均指仿真内物理执行，不表示真机验证。硬件采购、实物传感器标定、真机部署与迁移效果不属于本计划。

用户已确认同时开展两组实验：实际仿真传感器数据的感知验证，以及受控位姿误差下的任务成功率实验。两组数据分别标记和统计，不以合成误差替代实际感知成绩。

2026-10-01 上游同步的交付与测试限制见[同步验证历史](ASTRBOTEX_SYNC_20261001_RESULT_HISTORY.md)。新增 P/E 的详细派工见第 22、23 节；A（单臂）、L（自然语言与 Laya 技能选择）、R（ROS 闭环）、T（技能选择微调）、M（移动 GUI）继续沿用原章节。轨迹评分保留在第 16 节，作为后续扩展。各项目共用场景、数据接口和运行器。

## 1. 已确认的需求

| 项目 | 后续 MVP 范围 |
| --- | --- |
| 目的 | 验证仿真控制逻辑，并判断当前抓取放置任务是否需要神经网络策略 |
| 硬件 | 尚未选型，使用公开机器人描述和仿真资产，不作采购决定 |
| 底盘 | 履带外形，左右差速的等效运动模型，暂不验证逐节履带接触与打滑 |
| 机械臂 | 首版选 6 自由度，允许调整抓取方向，配平行夹爪 |
| 场景 | 平地，每轮随机生成、轮内静止的障碍物 |
| 物体 | 尺寸范围已知、颜色可区分的刚体方块或圆柱，位置随机 |
| 感知 | 不等待 YOLO，先比较仿真 RGB＋2D 雷达和 RGB＋3D 雷达；用颜色、轮廓及已知几何定位 |
| 精度研究 | 分别测实际感知误差与受控误差对抓取、精确放置、窄间隙搬运成功率的影响 |
| 精细任务 | 更准确的抓取放置、窄间隙避障，首轮不做插接 |
| 自然语言与 Laya | 上游只需提供自然语言；本地适配层澄清、绑定目标和实例化 Contract，Laya 选择技能/目标；复用既有模型环境，保留 EX 后端执行限制 |
| 两个技能 | move 仅移动底盘；fetch 在底盘停稳后抓取、搬运及放置；需要移动时组合 move→fetch |
| 澄清 | 未指定放置位置、目标有歧义或缺少必要信息时必须追问/等待，不默认开始运动 |
| Contract | 开发者固定 schema、硬边界及判定代码，任务可调整目标、容差和支持的成功模板 |
| 模型输入 | 结构化感知为主线，纯文本为对照，Qwen-VL 为小规模可选实验 |
| 机械臂规划 | MoveIt 2＋OMPL RRTConnect；单规划流水线，技能选定后规划，规则选择轨迹，再受限执行 |
| 延迟 | 目标为几十至几百毫秒。分别测技能选择、规划与整个任务交接链延迟 |
| 保护 | 执行前检查、执行端 effort 限制、可独立触发的停止 |
| 执行 | 规划 → ROS 2 控制接口 → Isaac 物理运动 → 状态反馈 → 成功判定 |
| 环境 | 沿用 Isaac Sim 5.1、Isaac Lab 2.3.2、ROS 2 Jazzy |

履带等效模型与简单物体限制均已得到用户确认。首版采用移动到位后停稳操作，不加入边行驶边抓取。本文统一称 Laya；历史报告、源码中的旧后端标识与文件名保留，不据此重命名上游协议。

既有 CartPole 结论按[实验历史索引](CARTPOLE_EXPERIMENT_HISTORY.md)保留，本次不恢复已归档或删除的报告。新项目复用其状态时间规则、单命令源、顺序执行和独立验收方法。不能把 CartPole 的 LQR 或 5 N 上限直接用于机械臂与底盘。

## 2. 后续 MVP 要回答的问题

1. MoveIt 2＋OMPL 能否从实时关节状态出发，生成经检查和时间参数化的机械臂轨迹，并通过 ROS 控制器在 Isaac 执行？
2. 经典导航能否让等效履带车根据雷达观测绕过随机障碍物并到位？
3. 经典感知、几何抓取、规划和反馈控制组合后，能否完成规定的抓取放置任务？
4. RGB＋2D/3D 雷达各自覆盖哪些场景，视觉模块必须提供哪些几何量、标定和误差范围？
5. 不同的位置误差、朝向误差和观测有效率，如何影响抓取、精确放置和窄间隙任务的成功率？
6. Laya 能否根据自然语言、技能目录和结构化观测选择 move/fetch 或需要澄清/等待，并绑定正确的对象与区域？
7. 参数化 Contract 能否适应不同目标与精度要求，同时保持独立成功判定及执行限制？
8. 纯文本与结构化输入在选择正确率、误执行率和延迟上有何差异？可选 Qwen-VL 是否带来值得其开销的语义收益？轨迹评分效果另在后续第 16 节判断。

项目 P 先给出 `SIM_SENSOR_PIPELINE` 与 `SIM_PERCEPTION_PROFILE` 的验证结果。项目 E 再给出按任务和输入来源分组的成功率。传感器输出可用不等于抓放成功，误差注入曲线不等于某款相机或雷达的实测能力。

分别报告 `MOVEIT_ARM_PASS`、`CLASSICAL_NAV_PASS` 和 `NO_NN_PICK_PLACE_PASS`。MoveIt 规划成功、控制器 Action 成功与物理任务成功分别记录；不把感知、导航和抓取接触效果全部归于 OMPL。

“无神经网络能够完成这个限定任务”可以由纯经典基线支持。引入 Laya 后，系统包含学习式技能选择，但仍可不使用神经网络生成轨迹或力矩。分别报告 `CLASSICAL_BASELINE` 与 `LAYA_SKILL_ASSISTED`；后续轨迹选择另标 `LAYA_TRAJECTORY_ASSISTED`，不混用结论。

## 3. 已核对的技术边界

### 3.1 MoveIt 2 与 OMPL 的分工

MoveIt 2 提供机器人状态、IK 插件、PlanningScene、规划流水线和控制器接口。OMPL 是其中的路径规划库；它不负责目标识别、夹爪接触或 ROS 执行器。首版使用 MoveIt 2 已有的 OMPL 集成，不另建直接调用 OMPL 的 SDK。[MoveIt OMPL 接口](https://moveit.picknik.ai/main/doc/examples/ompl_interface/ompl_interface_tutorial.html)。

机械臂首选 `geometric::RRTConnect`，目标是先获得有效路径。RRT*、多规划器并行、MTC、Servo 和底盘/机械臂联合规划不进入首版。OMPL 返回的几何路径仍需时间参数化、限制检查和控制器跟踪；不会自动证明 effort 满足限制。[OMPL 算法](https://ompl.kavrakilab.org/planners.html)、[MoveIt 轨迹处理](https://moveit.picknik.ai/main/doc/examples/time_parameterization/time_parameterization_tutorial.html)。

底盘继续由 Nav2 规划与跟踪。首版停稳后才运行机械臂；将底盘位姿加入机械臂状态空间，不能自动解决差速约束与全身执行协调。感知、Laya 和任务成功判据保持独立。

本轮已完成 xArm6 配置接入，启动实际 MoveIt 与 ros2_control，并通过 A2 的可达、不可达与碰撞拒绝三项零运动探针；简单绕障尚待证据。A1 姿态、夹爪及实际执行验收尚未完成，见[第一阶段进展](MVP_FIRST_STAGE_RESULT.md)。官方 Isaac 5.1 教程提供 Jazzy 工作空间示例，但示例机器人是 Franka；本项目使用独立的 xArm6 配置。[Isaac 5.1 MoveIt 教程](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/tutorial_ros2_moveit.html)。

### 3.2 “代替神经网络”需要按模块判断

| 模块 | 第一版方法 | 是否先训练神经网络 |
| --- | --- | --- |
| 物体识别与定位 | RGB 颜色/轮廓、已知几何、平面约束 | 否 |
| 抓取候选 | 顶抓规则、物体尺寸、少量夹爪朝向候选 | 否 |
| 机械臂 IK/轨迹 | MoveIt 2 IK 插件＋OMPL＋时间参数化 | 否 |
| 底盘路径 | Nav2 的经典规划与跟踪 | 否 |
| 关节跟踪 | 限速的关节轨迹与驱动反馈 | 否 |
| 夹取保持 | 有限夹紧力、物理接触与摩擦 | 否 |
| 任务顺序 | Laya 选择下一技能；小型状态机管理澄清、提交、等待和终态 | MVP 包含一次小规模技能选择微调，不训练运动策略 |

SciSpace 检索到的对象中心任务与运动规划研究，在仿真及实物抓放中展示了规划与反馈控制的组合。这支持路线可行性，但不能替代本项目的传感器和接触实验。[原论文](https://arxiv.org/abs/1911.04679)

若后续困难集中在未知物体、遮挡或纹理变化，优先替换感知或抓取候选模块。不要因一个感知失败就把整套执行器改成强化学习策略。

## 4. 推荐机器人与组件

### 4.1 首选一套机器人，避免重复适配

建议首选 **xArm6 + 平行夹爪 + 等效差速底盘**。xArm 的公开 ROS 2 仓库提供机器人描述、夹爪组合和 Jazzy 分支。Isaac 5.1 资产目录也列出 xArm6 与夹爪。[厂商仓库](https://github.com/xArm-Developer/xarm_ros2)。[Isaac 5.1 资产](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/assets/usd_assets_robots.html)

优先复用厂商 Jazzy 分支的 `xarm_description` 和 `xarm_moveit_config`，固定源码 revision；不连接或启动真机驱动。[厂商 MoveIt 配置](https://github.com/xArm-Developer/xarm_ros2/tree/jazzy/xarm_moveit_config)。这些配置尚未在本机与 Isaac 联合验证。核对 URDF/USD 的关节顺序、轴向、零点、TCP、限位和夹爪 mimic；SRDF 的 arm、gripper 组与实际资产对应。USD 总 DOF 数不能直接作为机械臂维度。

如果 xArm6 的模型一致性或夹爪适配出现明确阻塞，先向用户说明，再考虑 UR5/UR5e＋平行夹爪。只适配一个最终模型，不把两种机器人都完整验收。[UR 描述](https://github.com/UniversalRobots/Universal_Robots_ROS2_Description)

首轮不先做一套 Panda 再换 6 自由度机械臂。公开描述/软件与厂商 USD 资产分别记录来源和版本，不把所有可下载资产统称为开源硬件。

### 4.2 底盘模型

底盘使用刚体车架和左右驱动组。通过轮组驱动和物理接触移动，履带外壳用于外观。不逐帧写机器人世界坐标来伪装行驶。

在后续 MVP 平地低速场景中，采用差速运动近似。导航时机械臂收拢。规划 footprint 包含收拢机械臂和传感器外轮廓。首版可采用保守外接圆，避开狭窄通道问题。

这能验证导航接口和移动操作顺序，不能据此验证履带牵引、爬坡、越障或真机里程计误差。

### 4.3 组件分工

| 组件 | 最小职责 |
| --- | --- |
| Isaac 场景 | 机器人、桌面、物体、随机障碍、RGB、雷达、接触物理 |
| Nav2 | 观测障碍物后的底盘路线与路径跟踪 |
| 感知适配 | 同一接口接 RGB＋2D 扫描或 RGB＋3D 点云，输出目标、障碍、覆盖和未知区域 |
| MoveItAdapter | 同步场景、发起只规划请求、校验并关联 RobotTrajectory，输出 PlanResult |
| move_group / OMPL | 现成 IK、关节路径搜索、时间参数化和场景碰撞检查；禁用自动执行 |
| MoveGrounder / FetchGrounder | 每技能各有一个薄适配，共用状态、感知与规划基础设施；前者给出导航目标，后者给出抓放计划 |
| 任务适配层 | 接自然语言、执行必要澄清、实例化固定 schema 的参数，提交正式 Goal；不生成控制代码 |
| LayaAdapter | 单个模型服务选择已声明的技能/目标选项，返回 option_id；轨迹评分后置 |
| SafetyGuard | 执行准入、最终命令限制、停止锁存和状态超时 |
| ROS 执行适配 | 薄网关关联命令；标准 ros2_control 控制器跟踪；Isaac 最终入口落实租期、限幅和停止 |
| 任务状态机 | 导航、停车、观察、抓取、搬运、放置、结束 |
| 独立评估器 | 从原始物理状态判定到位、碰撞、抬升、掉落及放置 |

底盘优先采用 Nav2 Smac 2D + Regulated Pure Pursuit，适配保守圆形 footprint 和可原地转向的差速底盘。随机障碍保持静止，发现新障碍时停下或重新规划，不开发动态行人预测。[Smac](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/planners_plugins/smac/)。[Nav2 跟踪插件](https://docs.nav2.org/jazzy/configuration_and_development/first_time_robot_setup_guide/navigation_plugins/setup_navigation_plugins/)

## 5. 感知下限与视觉接口

### 5.1 RGB + 2D 雷达：有条件地支持第一版

MoveIt 规划需要机器人状态、目标几何和障碍几何。传感器先形成 SceneSnapshot，再由 Adapter 更新 PlanningScene；MoveIt 和 OMPL 都不直接把 RGB 检测框变成抓取位姿。

首轮先在项目 P 中独立接通仿真传感器，不依赖 B10、Laya 或机械臂执行器。RGB＋2D 雷达用于后续执行时，需满足以下条件：

- 平地障碍必须与雷达扫描平面相交。场景不包含未建模的悬空横梁、低矮障碍或地面坑洞。
- 操作台高度、几何和工作区边界作为显式先验。RGB 从标定、已知物体尺寸和平面约束恢复尺度。
- 随机目标和桌上障碍使用已知几何，位姿从 RGB 估计。不能把随机生成器的真值当作感知结果。
- 导航时机械臂收拢，导航 footprint 覆盖整车收拢外形。机械臂操作时底盘停稳。
- 关节反馈、短程里程计和 TF 仍然必需。两种外部传感器不代替机器人本体状态。

| 场景 | RGB + 2D 雷达能否承担首轮 | 需要补齐的信息 |
| --- | --- | --- |
| 平地绕过落地箱体/柱体 | 可以 | 扫描高度、底盘 footprint、里程计、障碍余量 |
| 已知桌面上抓取可见方块 | 可以 | 相机标定、桌面平面、物体尺寸和朝向约束 |
| 已知夹具形成的窄间隙 | 可以尝试 | 夹具三维几何、定位误差、整条机械臂和携物扫掠体 |
| 随机未知高度的桌上障碍、堆叠、悬空障碍 | 不能仅凭当前观测确认可行 | 多视角或深度观测，或明确、可信的几何先验 |
| 遮挡后目标位置不确定 | 当前帧不能直接执行 | 重新观察或返回感知不足 |

这些是传感器可观测性的工程判断。二维扫描的空白不代表机械臂三维空间为空。最低传感器成本通常意味着更强的场景先验。

项目 P 使用相同布局、小规模比较 RGB＋2D 和 RGB＋3D。2D 的盲区场景用于测适用边界，不要求 2D 在不可观测区域通过抓取验收。依据覆盖、误差和计算代价选择后续默认配置，不同时维护两套执行工程。3D 雷达也需测近距离点数和分辨率，不能预设它更适合小物体精定位。

### 5.2 同场景的 RGB＋3D 雷达对照路线

将点云通过 `PointCloud2` 发布。过滤地面和机器人自身点，按高度及体积形成障碍表示。Nav2 voxel layer 可接收 PointCloud2，并将三维占据信息投影到导航代价地图。[Isaac RTX 雷达](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/tutorial_ros2_rtx_lidar.html)。[Nav2 voxel layer](https://docs.nav2.org/jazzy/configuration_and_development/configuration_guide/core_servers/costmap_2d/costmap_plugins/voxel/)

首轮场地小且边界已知。导航使用轮组反馈积分的短程里程计，初始坐标对齐已知。随机障碍必须来自雷达观测，不能从场景生成器读取其真实坐标填入规划器。

不把这套配置称为完整 SLAM。若短程里程计漂移成为主要失败因素，再补雷达定位。初期不同时建设三维建图、定位和导航全栈。

操作区点云还用于桌面和近场障碍估计。保留三维高度，不能把底盘用的二维栅格当成机械臂完整碰撞世界。首轮用球、圆柱或盒体近似观测障碍并加余量。

随机障碍优先使用雷达易观测的箱体/柱体。未观测的机械臂扫掠区域不自动认作空闲。无法获得足够观测时，移动到底盘候选观察位或拒绝该次操作。

### 5.3 RGB：目标识别与抓取位置

相机固定安装在能看到桌面及夹爪工作区的位置。颜色用于识别指定物体，轮廓用于估计平面方向。已知尺寸与桌面平面提供尺度约束。

第一版采用“相机射线与已估计桌面/物体顶面求交”，或对具备可识别角点的已知物体使用 PnP。PnP 需要三维点、对应图像点和相机标定，不能只输入检测框中心就得到任意三维位姿。[OpenCV PnP](https://docs.opencv.org/4.10.0/d5/d1f/calib3d_solvePnP.html)

3D 雷达主要负责环境与平面。不要默认它能为每个 RGB 像素提供深度，也不要把传感器方案悄悄改为 RGB-D。目标处点云太稀疏时，使用已知几何约束，或重新观察。

必须记录相机内参、雷达/相机外参、TF 和图像/点云时间。底盘停稳后重新观察，再把目标转换至机械臂基座坐标，避免沿用行驶前的抓取位姿。

### 5.4 交给视觉模块的参数清单

| 类别 | 必须明确的内容 | 由谁提供 |
| --- | --- | --- |
| 固定标定 | 相机内参/畸变、相机至底盘外参、机械臂基座和 TCP、雷达扫描平面 | 机器人与传感器配置 |
| 场景先验 | 支撑平面及误差、已知几何尺寸、固定夹具、允许操作区域 | 场景配置，注明先验来源 |
| 在线物体 | ID、类别/颜色、米制位置、可观测朝向、尺寸、可见性、位姿误差估计 | 视觉感知 |
| 在线障碍 | 几何位置、覆盖区域、未知区域、最近观测时间 | 雷达/视觉感知 |
| 本体状态 | 按名称排列的 q、dq、夹爪开度、底盘位姿与速度、持物状态 | 状态反馈 |
| 任务约束 | 抓哪个物体、放哪个区域、位置容差、朝向自由度、允许接触对 | 上游技能请求 |

控制端内部感知输出采用 `SceneSnapshot`，不要求上游所有插件统一业务 schema：`scene_id / stamp / frame_id / objects / obstacles / support_plane / coverage / uncertainty`。状态快照单独携带 `state_id`，以时间戳和 TF 对齐。接收端记录单调时钟时间，用于过期判断。

首版不强制复杂概率模型。报告验证集的位置/角度误差分布、样本最大值及适用条件；有限样本最大值不是保证上界。类别置信度和图像分辨率不能替代米制定位误差。缺少关键尺度、TF 或工作区覆盖时，返回 `INSUFFICIENT_OBSERVATION`。

项目 P 在同一快照中补充 `observation_id / sensor_profile_id / calibration_id / perception_version / input_source / validity / observable_dofs / received_monotonic_ns`。逐样本真值误差保存在评估日志，不传给 Grounder 或 Laya。`uncertainty` 只来自实施前固定的开发集估计或显式模型，不从本次真值反算。

视觉模块可以只估计当前任务需要的自由度。例如直立圆柱的绕轴角度可标为自由，方块顶抓允许数个等价夹爪方向。不要把不可观测角度伪造成精确的六维位姿。

窄间隙先做误差预算：可用间隙必须覆盖定位误差、标定误差、跟踪误差、几何近似误差和停止扫掠余量。不能只用“检测到了物体”作为开动条件。

视觉团队的交付先固定三项：任务需要哪些可观测自由度、在工作区内的米制误差、结果更新周期与最大年龄。控制侧依据这些误差确定间隙和速度，避免先承诺毫米级任务，再要求感知无条件满足。

### 5.5 真值使用边界

单臂调试阶段可以使用物体真值，明确标为 `ORACLE_POSE`。它只隔离规划和接触问题。

本轮 A/R/E0 的 ORACLE 配置保留 GUI 与真实物理／ROS 反馈，关闭未被规划器使用的 RGB／雷达 render products（`sensor_rendering=false`）。实际配置明确标记 `ORACLE_CONTROL_ONLY`，不宣称传感器参与控制。P1 使用完整传感器并独立验收；M1 保留随车传感器。后续 E1 必须重新验证完整感知负载下的时序，不能直接继承 ORACLE 的执行性能。

正式评估标为 `SENSOR_POSE`。规划器只接收传感器估计、机器人反馈和任务给定的操作台区域。物体、随机障碍和底盘世界真值只供独立评估器使用。

新增诊断模式 `ORACLE_PERTURBED`：独立诊断输入生成器从真值构造几何输入，再施加已知位姿误差。它专用于第 23 节受控敏感性实验，零扰动复用 ORACLE 基线。其余未扰动几何来自真值，须在配置中明列。该模式不能标为 `SENSOR_POSE`，也不能进入正式感知或 Laya 训练成绩。

最终报告分别给出 ORACLE、SENSOR 与 ORACLE_PERTURBED 结果。SENSOR 不读取 USD 物体位姿、实例/语义标签、相机深度图或随机生成器坐标。已声明的尺寸、支撑平面和固定标定可以作为先验；未知对象位置仍须估计。传感器挂在机器人上但规划仍读取真值，不能算感知闭环通过。

## 6. MoveIt 2＋OMPL 的最小接入规格

### 6.1 环境、机器人配置与复用范围

下表保留系统安装基线。本轮另在项目私有前缀补齐依赖并启动实际 ROS 栈；运行证据见[第一阶段进展](MVP_FIRST_STAGE_RESULT.md)，安装版本本身不作为物理验收。

| 组件 | 本机安装元数据 | 后续处理 |
|---|---|---|
| moveit_ros_move_group / moveit_planners_ompl | 2.12.4 | 复用现有 Jazzy 安装，冻结实际包版本 |
| moveit_msgs | 2.6.0 | 使用已有服务与 RobotTrajectory 消息 |
| ros2_control | 4.48.0 | 复用标准控制框架 |
| joint_trajectory_controller / gripper_controllers | 4.42.1 | 配置 arm/夹爪 Action，不自行实现通用轨迹插值器 |
| topic-based hardware 接口 | 系统前缀原先没有；本轮私有前缀已补齐 `joint_state_topic_hardware_interface` | 已加载真实 Isaac 关节反馈；运动与停止验收单列 |

MoveIt、网关和控制器在系统 Jazzy 环境运行；Isaac 保持 `isaaclab232_test` 的 Python 3.11；Laya 保持已有 `runtime/laya/.venv`。不把 ROS 系统 Python 路径或模型依赖加入 Isaac 环境，也不新建 PyRoKi/JAX 规划环境。

项目 A 固定 URDF、SRDF、`kinematics.yaml`、`joint_limits.yaml`、`ompl_planning.yaml`、控制器配置和 TF。只启用 arm 规划组与 gripper 执行组；移动底盘在操作期间保持停稳。优先复用厂商 IK 配置，不编写 IK 求解器。关节按名称映射，单位与 TCP 显式核对；碰撞矩阵不能因方便规划而全局放行。

### 6.2 只规划服务、反馈起点与结果合同

首版由 ROS 网关内的薄 MoveItAdapter 异步调用 `moveit_msgs/srv/GetMotionPlan`，服务通常名为 `plan_kinematic_path`，实际使用独立命名空间配置。独立 `move_group` 设置 `allow_trajectory_execution=false`。不使用 RViz 的 Execute、MoveGroup 自动重规划执行或第二条命令发布路径绕过 Guard。[规划服务源码](https://github.com/moveit/moveit2/blob/jazzy/moveit_ros/move_group/src/default_capabilities/plan_service_capability.cpp)。

MotionPlanRequest 显式设置 `group_name / pipeline_id / planner_id / start_state / goal_constraints / allowed_planning_time / max_velocity_scaling_factor / max_acceleration_scaling_factor`。`start_state` 来自新鲜、按名称映射的原始 Isaac 关节反馈，保留当前附着物状态；不得用上次命令值或默认零位代替。

响应必须为成功码，且包含完整有效的 `trajectory_start` 和 `RobotTrajectory`。规划前后记录时间与版本。发出执行前再次核对起点、附着物、场景版本和限制；普通 JointState 序号递增本身不导致失效。

```text
PlanResult
  status / failure_reason / moveit_error_code
  candidate_id / trajectory_id / trajectory_hash
  RobotTrajectory（joint_names / positions / velocities / accelerations / time_from_start）
  start_state_id / start_state_stamp / scene_id / planning_scene_revision
  robot_config_hash / collision_config_hash / calibration_id / input_source
  group_name / pipeline_id / planner_id / planner_seed（若接口可设置）
  target_tcp_pose / task_tolerances / allowed_contact_phase / attached_object_ids
  limit_check / collision_check / complete_path / planning_time / total_wall_time
```

ROS 四元数使用 `xyzw`；不再保留旧 PyRoKi `wxyz` 转换。时间须有限且后续点严格递增，关节名及维数匹配。只有已校验的不可变轨迹才能进入 CandidateSet；失败码、空轨迹、部分路径都不能包装成成功。

GetMotionPlan 是服务，没有远程取消保证。最多一项在途求解和一项最新待处理请求；超时立即废弃其 request/epoch，但在旧响应返回或确认旧进程退出前不叠加求解。停止独立执行，迟到结果不能触发运动；健康恢复不重放旧请求。

### 6.3 SceneSnapshot 到 PlanningScene

MoveItAdapter 是机器人工作区的唯一场景写入者。将桌面、目标、障碍和已知夹具转换成带稳定 ID 的 CollisionObject，保留尺寸、来源、估计误差和删除语义。SENSOR 仅使用观测和显式先验，未知区域不能被填成空闲。首版用标准 SolidPrimitive 的盒体/球/圆柱，不先建完整 OctoMap 系统。

通过同步 `ApplyPlanningScene` 获取成功应答后，记录应用侧 `planning_scene_revision` 与几何 hash，再发起规划。同一候选集固定相同场景和起点。该 revision 由本项目管理，MoveIt 服务没有替项目提供 CAS。[PlanningSceneMonitor](https://moveit.picknik.ai/main/doc/examples/planning_scene_monitor/planning_scene_monitor_tutorial.html)。

规划期间新观测可进入有界队列，但不在同一批候选中混用不同障碍世界。与计划相关的障碍变化、附着状态变化或底盘移动立即使旧候选失效；停止/取消不等待场景锁。普通无实质变化的刷新可沿用原几何版本。规划节点重启后重建场景并变更会话，旧轨迹不可恢复执行。

抓取分为预抓取、接近、闭爪、抬升、搬运、放置、撤离。只在对应阶段允许手指与目标接触，其他碰撞检查保持启用。闭爪接触成立后，以临时 AttachedCollisionObject 将目标计入抬升段的碰撞模型；抬升物理判据通过后确认持物。抓取失败清除临时附着并停止，放置完成后解除附着。附着只影响规划，Isaac 中继续靠接触和摩擦持物，不能用 fixed joint 或 teleport 代替抓取。

夹取后使用反馈重新规划下一运动段；改变附着或允许接触对就更新场景版本。未验证的后续段不得因为前段成功而直接连发。重试仍受原任务时限和授权范围约束。

### 6.4 OMPL、时间参数化与短直线运动

默认一条 `ompl` 流水线，planner 配置项对应 `geometric::RRTConnect`，例如 `RRTConnectkConfigDefault`。候选差异优先来自抓取方向或预抓取位姿，不依靠对完全相同请求无限随机重跑。

本机默认 response adapters 已包含 `AddTimeOptimalParameterization → ValidateSolution`。沿用该顺序，核对结果确有时间、速度与加速度；不能把 OMPL 路径数组直接发送给控制器。时间参数化可能改变路径，需要对最终轨迹作限制与碰撞校验。[时间参数化说明](https://moveit.picknik.ai/main/doc/examples/time_parameterization/time_parameterization_tutorial.html)。

固定 OMPL 的运动检查分辨率、碰撞余量与物体近似配置。默认离散检查不能宣称为连续碰撞证明；窄间隙验收包含中间运动段与最终时间轨迹，不能只看起终点。复用 MoveIt 的检查能力，不另写碰撞引擎。若配置的 ValidateSolution 只检查已有采样点，补足待检查采样密度后再验收。[OMPL 碰撞检查设置](https://moveit.picknik.ai/main/doc/examples/ompl_interface/ompl_interface_tutorial.html)。

OMPL 关节路径不保证 TCP 沿直线接近或抬升。需要短直线段时，复用现有 `GetCartesianPath`，开启碰撞检查，要求 `fraction` 完整并拒绝关节跳变；核对时间参数及速度/加速度缩放。部分路径返回不执行。首版不因此引入 MTC、Servo 或第二套任务状态机。

### 6.5 候选预算、超时与失败

首版先用单候选和固定规则选择器；后续第 16 节需要比较轨迹时才扩为 2–4 个，共享一次规划总预算。在时限前没有获得两个有效不同候选时，如实标记为单候选，不能声称 Laya 已完成多轨迹选择。

初始/演示规划总预算起点为 2 s；热重规划的规划部分目标预算为 150 ms，给选择、Guard 和交接保留时间。两种模式显式标记，原全链 p95≤300 ms 目标保留；不能把四个候选各自 300 ms 算成一次 300 ms 请求。预算是请求配置，不是硬实时保证。

每条请求的 `allowed_planning_time` 不大于剩余规划预算。预算到期不再启动新候选；实际服务响应、后处理和排队耗时仍单列。规划失败分别记录起点无效、目标不可达、超时、碰撞/约束失败和空结果；它们不自动证明场景无解。

### 6.6 与现有项目的职责划分

MoveIt 替代原 PyRoKiAdapter、JAX 求解环境及自研几何规划部分。标准 ros2_control 替代拟自研的通用轨迹插值跟踪；B11 网关继续负责 EX 命令关联、选择后准入、租期、取消和 StopEvidence。感知 P、精度 E、规则/Laya 对照、物理接触判据和 Nav2 路线全部保留。

不把 MoveIt 节点的存在视为机械臂已可用。项目 A/R 仍需验证模型一致性、原始反馈、实际驱动、停止及失败路径；不复跑 CartPole，也不先完整验收官方 Franka 再换 xArm6。

## 7. 自然语言、Laya 技能选择与控制端的边界

```text
上游自然语言（首轮本地 CLI/任务脚本；以后替换为 A.E.B.）
        ↓ 任务会话：必要信息检查 ↔ 用户澄清
技能目录 + 任务相关结构化观测 → Laya：选择下一技能/目标
        ↓ 任务适配层：绑定、校验、冻结参数化 Contract
正式 GoalSubmit → EX GoalManager / DecisionService（首轮 MockBackend）
        ↓ Dispatcher → Actor → 仿真控制插件
        ↓ context.ros 有界 command/cancel/lease/feedback
MoveGrounder → Nav2 → 受限底盘执行 → 停稳
FetchGrounder ← 新 SceneSnapshot + JointState / odom / TF
        ↓ MoveItAdapter：场景同步 + GetMotionPlan（只规划）
move_group / IK / OMPL / 时间参数化 → 固定规则选择有效轨迹
        ↓ Guard：版本、起点、授权、碰撞及限制复核
FollowJointTrajectory / GripperCommand → ros2_control
        ↓ topic-based hardware → Isaac 最终命令闸门 / drive
实际反馈 / 独立物理判据 → 插件 report → Ledger / B09 → 任务会话

STOP / watchdog → 最终执行端，不等待 Laya 或规划服务
后续扩展：在有效轨迹之间增加 Laya 评分，不改变上述执行边界
```

首轮仍由本地入口调用已有可信 `submit_goal/renew_goal/cancel_goal`，但入口接受自然语言和澄清回复，不能预先替模型写好技能答案。完整 A.E.B. 接线后替换输入适配即可。旧 proposal/ZMQ 入口不能绕过正式 Goal 校验。

**Laya 位于正式 Goal 之前选择技能；EX 仍负责授权和动作生命周期。** 当前 `GoalSubmit.parameters[action_id]` 必须符合已声明 schema，裸自然语言不能直接变成可执行 Goal。任务适配层负责把选择结果转成完整参数；具体几何与关节轨迹由技能 Grounder 求解。每轮只提交当前一个已授权宏动作，后续步骤留在任务会话，前一步终态、资源释放及必要停止证明成立后才能提交下一轮。

EX 已有 Laya 后端仍受 disabled/shadow 限制；隔离测试的 `allow_test_execution=True` 不能用于机器人。首版用 `MockBackend(kind="start")` 确定性调度唯一已选动作。新增任务适配层使用共享模型服务提出选择，经过可信任务入口授权，不切换 EX 执行能力或绕过 Guard。模型自己不能授予插件权限。[B07 已有结果](../../docs/B07_LAYA_EX_BACKEND_RESULT.md)只证明既有接入，不证明中文任务选择或机器人控制可用；其中 replan 0/8 仍如实保留。

复用现有 `ActionDeclarationV2` 的动作描述、参数 schema、资源和观测要求，构造 Laya 可读的技能目录。一个插件可以声明多个动作，不增加另一套技能注册平台。对外只暴露 `move`、`fetch`；澄清、等待观测、无可用技能和完成是任务状态，不是新增运动技能。模型面对信息不足时无需被迫选择运动。

Laya 可在本次任务授权内选择目标及下一步，不能自行更换用户指定物体、提高 effort 上限或放宽已冻结成功条件。目标/容差/成功模板修改时产生新任务版本；普通传感器刷新不每帧产生新 Goal。取消、任务版本或模型服务代次变化使旧选择失效，执行层继续使用 command_id、Goal 版本和 execution_epoch 拒绝旧轨迹。

机械臂采用受限位置轨迹。Action 客户端放 ROS 网关，跟踪交标准控制器；EX facade 仍只使用发布/订阅，不在插件中私建 ROS executor。MoveIt 自动执行关闭，计划只有经 Guard 才能下发；Action 接受不等于物理成功。底盘停止输出零速度，机械臂在有限 effort 下停止/保持，持物夹爪保留有限夹紧力；不照搬 CartPole 的结束零力。

### 7.1 SafetyGuard 必须覆盖最终执行通道

现有 legacy `safety.py` 主要限制底盘速度与持续时间，并响应 estop。新 B02/B04 增加了命令授权和生命周期门禁，但仍不等于机械臂最终驱动 effort 检查。机械臂保护放在项目 R 的最终执行通道。

本 MVP 在新机器人的最终执行适配中落实保护，不为此重构全部旧插件。

| 层次 | 最小检查与行为 |
| --- | --- |
| 执行前 | 数值有限、关节/速度/加速度范围、碰撞、状态新鲜度、轨迹起点、允许接触、机器人配置版本 |
| 显式 effort 控制 | 对最终总输出检查和限幅，包括反馈与前馈。无效输入触发停止，持续饱和返回执行失败 |
| 位置/速度控制 | 检查轨迹目标，并配置 Isaac drive 的最大 effort。只限制消息中的 effort 字段不够 |
| 执行期间 | 每个控制周期检查状态超时、跟踪误差、停止锁存与命令有效期 |
| 物理记录 | 分开记录请求 effort、最终命令 effort、驱动实际 effort。关节反力/接触冲量单列 |

旋转关节 effort 单位是 N·m，直线关节是 N。上限来自当前模型和 actuator 配置。Laya 和任务请求不能提高上限。Isaac 文档提供驱动力限制；ROS 轨迹控制器在 effort 模式下还可能叠加反馈输出。[Isaac drive 调参](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/robot_setup_tutorials/joint_tuning.html)。[ROS 轨迹控制器](https://control.ros.org/jazzy/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html)

执行前可以拒绝明显超限的 effort 请求。但对位置轨迹，若没有完整动力学及接触预测，就不能预先证明全程所需力矩。首轮用限速、限加速度、驱动硬上限和跟踪监测实现受限执行。若限幅后无法完成任务，则返回失败，不自动提高上限。

驱动 effort 上限不等于所有碰撞反力都有同样上限。必须注明测量语义。若实际驱动 effort 无法读取，记录为未知，只能宣称命令/配置限制通过。

### 7.2 可中断与停止

停止入口放在执行进程。规划与 Laya 在其他进程运行。GUI 按钮、取消请求或 watchdog 均可触发停止，不能排在一次长推理后面等待处理。

停止时取消活动目标，增加 `execution_epoch`，使旧轨迹、旧评分和队列命令失效。底盘停止驱动，机械臂由 Isaac 最终入口切换到有限 effort 的保持状态；分别实测停止时间与位移，不承诺停止切换满足正常轨迹的加速度界限。夹爪按持物状态保持有限夹紧力，具体实现与验收见第 19 节。恢复需要新的有效执行请求，旧队列不能自行恢复。

“随时可发起停止”不等于机器人能瞬间静止。记录请求到接收、旧命令失效、开始制动、实际停稳四个时点，以及停止期间位移。正常停止也必须满足 effort 上限和可用停止空间。

watchdog 使用单调墙钟，避免仿真暂停冻结超时检测。Isaac 卡死时外部进程不能保证继续执行物理减速，仿真暂停属于独立兜底，不计作控制器停止通过。

底盘避碰优先复用 Nav2 Collision Monitor，置于速度输出链最后。停止锁存、关节保护和命令过期检查复用同一个执行适配。[Nav2 官方接法](https://docs.nav2.org/jazzy/tutorials/general_tutorials/using_collision_monitor/using_collision_monitor/)

## 8. 后续派工顺序与依赖

本轮按文首范围实施第一阶段及必要依赖，不 commit、不 push，保留现有 B08/B09 成果。Torch 导入已验证通过；后续环境首次加载较慢时等待同一进程，不以短时无输出判定失败。

| 项目 | 详细章节 | 依赖与完成条件 |
|---|---|---|
| 环境检查 | [环境结果](MVP_ENVIRONMENT_AUDIT_RESULT.md)、[清理报告](ISAAC_ROS_CARTPOLE_RETIREMENT_20261003_RESULT.md) | 原 Torch 导入已通过；保持 Isaac/ROS/Laya 环境隔离，按实际阶段验证运行 |
| B08/B09 | [管理结果](../../docs/B08_DECISION_MANAGEMENT_RESULT.md)、[页面结果](../../docs/B09_DECISION_UI_RESULT.md) | 复用管理、服务与页面；以后补真实任务/控制投影，不重建 |
| P 独立感知 | 第 22 节 | Isaac 环境可用；无 YOLO、机械臂、Laya 前置 |
| L 自然语言与技能选择 | 第 14、15 节 | 复用 EX 目录/Goal 服务和 Laya；先离线澄清、选技能、Contract 校验，不等待机械臂 |
| A 机械臂经典基线 | 第 18 节 | 与 R 共用网关；ORACLE 后接 P 的 SENSOR 输入，固定规则规划 |
| R ROS 2 实际闭环 | 第 19 节 / B11 | 先支持 A 的受限执行，再接 L/T 的已验证选择；模型不阻塞停止 |
| E 精度与成功率 | 第 23 节 | P＋A/R；固定规则和成功判据，实际感知/合成误差分别评价 |
| T 技能选择微调 | 第 20 节 | L 的输入/标签/服务边界固定后可训练；上线仍须 A/R 保护验收 |
| M 移动抓放与 GUI | 第 21 节 | SENSOR 单臂、Nav2、L/T 及 R；完整自然语言→物理结果证据 |
| 后续轨迹评分 | 第 16 节 | 核心链完成后另行安排；不作为首版阻塞项 |

建议先解除环境问题，再开展 P 与 L 的独立子集；A/R 共建一条执行链，E 复用它。T 可先做离线技能选择训练，不能要求尚不能识别任务的原模型先强行驱动机器人。上线比较只执行通过同一契约与保护检查的选择，原模型失败也保留。M 最后汇合并完成 GUI 验收。

共用一个运行器：`sensor_only/control`、`ORACLE/SENSOR/ORACLE_PERTURBED`、`text/structured/qwen_vl`、`rule/base_laya/finetuned_laya`、`GUI/headless` 是互相独立的记录字段，不做全部组合。E 固定规则技能输入与轨迹选择；L/T 固定感知条件；Qwen-VL 先做录制帧实验。sensor_only 不启动 move_group、Laya 或执行器。

B04 已补公开停止回执，原 `_stop_pending` 等待竞态按[B04 报告](../../docs/B04_BOUNDARY_20261001_RESULT.md)修正；不再重复列为未知前置。B02 的压力/生命周期限制仍按报告保留，不能写成全框架稳定。所有新工作按最小独立阶段验收，不复跑 CartPole，不以已有 mock 测试替代新 ROS/Isaac 证据。

## 9. 第一个完整演示

场景为一块平地和一个操作台。任务从自然语言开始，不给出随机物体与障碍真值。物体和放置区在同一操作台上。正常示例为“把红色方块放到左边托盘”；“抓取红色杯子”若无放置位置必须追问，场景没有杯子时还需明确目标，不能把方块当成杯子。

```text
用户自然语言 → 缺项/歧义检查 → 必要时追问，回复前无运动
→ Laya 根据技能目录与结构化感知选择 move 到操作台区域
→ 任务适配层冻结 move Contract，提交正式 Goal
→ 车体从起点出发
→ 雷达观测随机箱体/柱体
→ Nav2 绕障到操作台附近
→ 线速度和角速度降至停车阈值
→ RGB 重新定位指定物体和放置区
→ Laya 选择 fetch；校验新快照与既定任务，冻结 fetch Contract
→ Move 终态与资源释放成立后，提交 fetch Goal
→ FetchGrounder 选择预抓取位姿及夹爪方向
→ MoveIt 2＋OMPL 从当前关节状态规划并生成带时间的轨迹
→ 机械臂接近，夹爪闭合
→ 物体抬升并保持
→ 搬运到同台放置区，放下并开爪
→ 夹爪撤离，物体留在区域内
→ 独立判据给出物理结果，关联自然语言、选择、Contract、Goal 和 ROS 命令
```

首轮不携物跨区域行驶。这样已经覆盖底盘导航、机械臂规划、感知定位和物理抓放，同时避免再引入携物底盘稳定性。

操作台周围预设少量可选停车位。Nav2 判断导航可达性，MoveIt IK/规划判断操作可达性。最多尝试 3 个停车位；每次移动后更新 TF 和场景，不复用旧轨迹。

最终选一个冻结评估工况在 GUI 中运行，同时显示 RGB 和雷达/代价地图。它计入正式评估，不额外重跑一整套录像工况。

## 10. MVP 场景与通过标准

以下是建议的初始配置，实施前写入实验配置。它们不是实测成绩。依据最终模型尺寸完成一次调整后冻结，不在看到评估失败后临时放宽。

- 单次一个目标物，首版优先 4–6 cm 方块。物体必须在夹爪开度和载荷能力内。
- 每轮约 5–8 个静态障碍，位置随机。生成器避免初始穿透，并保留基本操作空间。
- 规划器看不到随机种子对应的障碍真值。生成规则可以保证大类任务可行，但不能把解路径注入导航。
- 机器人低速运行。底盘速度先限制在 0.15 m/s，机械臂速度按模型限位取保守比例。

| 指标 | 首版建议判据 |
| --- | --- |
| IK/FK | 末端位置误差 ≤5 mm，受约束的朝向误差 ≤5° |
| 导航到位 | 位置误差 ≤0.10 m，朝向误差 ≤10°，并通过后续可达性检查 |
| 停车 | 实测线速度 ≤0.02 m/s、角速度 ≤0.05 rad/s，连续 0.5 秒 |
| 机械臂执行 | 关节/速度限制满足，无长期跟踪误差，无非预期碰撞 |
| 抓取 | 目标真实抬升至少 8 cm，持续 1 秒，期间没有依靠外部附着 |
| 放置 | 开爪并撤离后，物体仍在指定区域，稳定至少 1 秒 |
| 演示通过 | 10 个冻结可行场景至少 8 个完整成功，全部结果保留 |
| 失败处理 | 不可达和封路两个场景均返回有原因的失败，不伪报成功 |

语言与 Contract 验收另外包含：缺放置位置、同色多目标、未知技能、过期观测、越界容差、取消后的迟到选择；这些先离线检查，不各自再跑完整抓放。缺必要信息时必须零运动提交，错目标或绕过限制不能因模型平均正确率达标而通过。L/T 的冻结语言测试与最终物理场景按各自分母报告。

10 次试验是 MVP 初筛，不是可靠性认证。碰撞、掉落、超时和拒绝执行均记录，不能把失败从分母中删除。真实发生的非预期碰撞不能因总成功率达标而被忽略。

独立评估器可以使用仿真真值判定抬升、落点和碰撞。正式 SENSOR 规划与控制不能读取这些评估真值。接触判定区分手指/目标的预期接触和机器人/环境的非预期接触。

上述 10＋2 工况及 8/10 门槛属于最终演示，保持不变。第 23 节的精度研究单独统计，不要求每个误差档位达到 8/10。误差增大后的失败、合理拒绝和任务退化均是研究结果。

## 11. 如何决定是否引入神经网络

| 观察到的结果 | 后续结论或下一步 |
| --- | --- |
| ORACLE 和 SENSOR 均稳定通过 | 当前限定任务不需要先训练抓取控制策略，继续经典方案 |
| ORACLE 通过，SENSOR 经常失败 | 先定位标定、遮挡和物体位姿误差，必要时只引入学习式感知 |
| 位姿正确，但夹取经常滑脱 | 先检查夹爪几何、接近方向、摩擦和驱动力，再判断是否需要抓取质量模型 |
| IK/轨迹经常失败，执行器正常 | 检查 IK、起点、场景模型、OMPL 预算与采样检查分辨率；先定位当前规划配置 |
| 轨迹有效但关节执行偏差大 | 修正执行适配与驱动，不把问题归因于感知或规划网络 |
| 未知对象、接触丰富任务仍是主要需求 | 单独设计学习式抓取或策略对照，复用现有传感器和执行接口 |

不增加端到端抓取策略训练。沿用一次小规模微调方向，但首轮训练对象改为 Laya 的技能/目标选择，见第 20 节；不再以原始轨迹评分模型先完成机器人闭环作为训练前置。任务解释、感知、规划和控制分别归因：模型选错技能不等于 MoveIt 失败，规则修正后的执行也不算模型选择成功。

更精细不自动意味着神经网络更好。窄间隙首先依赖几何精度、误差余量和可跟踪轨迹。学习式方法可以改善复杂场景中的初值和候选排序。DiffusionSeeder 与 PRESTO 均采用学习式轨迹提案，再由优化器修正。这支持混合路线，但不能证明 Laya 已具备机器人轨迹评价能力。[DiffusionSeeder](https://arxiv.org/abs/2410.16727)。[PRESTO](https://arxiv.org/abs/2409.16012)

MoveIt/OMPL 的规划可用性单独判定。Nav2 成功不代表机械臂规划成功，感知失败也不能直接归为规划算法失败。

## 12. 最小工程边界与交付

保留一个仿真入口、一个 MoveItAdapter、一个感知适配、一个 EX/ROS 网关、一个带澄清的顺序任务入口和一个评估器。复用 move_group、ros2_control 和 Nav2；LayaAdapter 首轮只处理技能/目标选择。MoveGrounder 与 FetchGrounder 是薄模块，不各自复制服务、状态缓存或运行器。独立进程边界服务于规划、模型与执行隔离，不建设通用任务平台。

沿用当前目录分层：仿真放 `sim/`，ROS 组件放 `ros2_ws/src/`，入口放 `scripts/`，文档放 `docs/`。运行数据放共享数据目录的新子目录。Cartesian/TCP、关节名和模型限位由机器人配置描述，避免写死 Panda/UR5 的名称。

每次保存：机器人与软件版本、场景种子、ORACLE/SENSOR/ORACLE_PERTURBED 来源、传感器与标定配置、原始观测引用、状态时间戳、计划摘要、执行反馈和独立物理判据。E 另记扰动字段、坐标系、幅值和随机种子；完整字段见第 23 节。

CartPole 已完成的证据保持不变，相关源码退出活跃目录。新任务按需参考归档代码，不恢复退休脚本，不让新场景使用 CartPole 的全局话题或实验专用阈值。

每轮另记原始自然语言与澄清回复、task_session/task_revision、候选目录、模型实际输入与 option_id、绑定后的 Contract/hash、Goal/command 关联。技能选择与轨迹选择用不同字段，避免把规则生成的轨迹写成模型输出。

人工介入集中在首次 GUI/传感器视角确认、视觉模块接口对接、机器人候选出现实际阻塞时的选型调整，以及最终演示观看。Laya 训练若涉及新增硬件或付费服务，再另行确认。已授权的常规开发不逐项询问；本轮第一阶段及必要依赖已获实施授权，不 commit、不 push。

## 13. 实施前仍需固定的配置

这些是实现配置，不需要重新讨论已确认的开发方向：

1. 核对 xArm6 的 URDF/USD/SRDF、实际夹爪、IK 插件和控制器关节映射。
2. P 先固定临时传感器支架、桌面高度及视角，记录标定版本；接入机械臂后按安装位更新外参，只做受影响的检查，不重跑整套台架。
3. 固定开发种子、评估种子、任务时限和碰撞余量。
4. 冻结现有 MoveIt/OMPL/ros2_control 版本和一个 topic-based hardware 接口，补齐 xArm 配置；不升级 Isaac 或重装其依赖。
5. P 冻结两套传感器参数及图像处理版本；E 冻结任务容差、研究场景、扰动档位与失败分类。参数初值见第 22、23 节，均不是实测能力。
6. 保留本地 CLI/任务脚本先行的既定入口；正式 A.E.B. 接线后替换入口，不改执行链。明确 move/fetch 的 action_id、参数边界、成功模板及地区/对象别名。
7. 核对现有 Laya 中文任务表现；先小样本比较已缓存英文 checkpoint 与一个固定版本的 multilingual 候选，再固定一种用于技能微调。不得默改 B07 的 EX 默认模型或一次运行多个模型。

若上述候选带来明显额外开发，例如必须实现复杂履带接触、未知物体六维姿态或全身同步控制，先向用户说明原因并确认范围，不自动扩大 MVP。

## 14. 开发项目 L：自然语言澄清与 Laya 技能选择

状态：L1 的任务目录、澄清和受控模型调用已实现，20 条真实中文开发探针仅 2 条选择正确。接口运行通过不代表模型可用于机器人执行；后续 L2–L4 尚未验收。见[语言结果](MVP_LANGUAGE_FIRST_STAGE_RESULT.md)。

### 14.1 目标、复用和最小边界

Laya 接收文本/JSON 状态及给定选项，返回 choice/score 等结构化判断；它不是任意文本或接口代码生成器，也不是图像编码器。官方英文与多语言模型的适用范围不同，需要验证中文输入。[固定版本 README](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/README.md)。

保留代码 `0.3.22`、B07 已缓存的 `convaiinnovations/laya/typed-decisions` 及 revision `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`。先用约 20 条中文开发样例检查 move、fetch、歧义和缺项；不足时在独立配置下比较一个固定 revision 的 multilingual checkpoint。仅加载一个模型，记录代码/依赖/权重 hash，不因中文任务静默升级 Torch、覆盖原权重或改变 EX 默认注册。测试集不用于此选型。

技能目录从现有 ActionDeclarationV2 生成：描述、参数/成功模板说明、资源需求和必要观测。Grounder 的几何细节不填进目录。机器人可执行性来自简单状态检查和规划结果，不先训练另一套可执行性网络。

| 技能 | 必需参数及前提 | 成功含义 |
|---|---|---|
| move | target_region_id；已有地图/TF、收拢状态、资源授权；导航目标由 Grounder 求出 | 底盘进入区域/容差且停稳，符合固定到位模板 |
| fetch | object_id、place_region_id、支持的精度/成功模板；底盘停稳、新鲜观测、夹爪可用 | 指定物体被实际抓起、搬运、释放到指定区域且稳定，符合固定抓放模板 |

需要先 move 时，任务可先保留“红色方块”等语义目标描述，move Contract 只绑定已知操作区域；到位重新观察后才绑定 fetch 的 object_id。放置位置仍必须在首次移动前问清。操作台区域来自任务或显式已知区域表，无法确定时追问，不凭空猜导航坐标。

首版只支持目录声明的词汇、对象/区域和条件。任务适配层做小范围颜色/类别/区域/数值解析与引用绑定；遇到无法可靠解析的说法明确追问，不暗中加入第二个大语言模型。Laya 在选项中选技能/目标，不自由生成米制坐标。开发规则只生成合法选项与检查完整性，不能提前按意图只留下唯一正确答案再称为模型选择。

### 14.2 三种输入方式与最小对照

| 模式 | Laya 输入 | 验证方式与边界 |
|---|---|---|
| text，对照 | 自然语言、技能描述及参数说明，无对象观测 | 测语言匹配和澄清；Grounder 执行时仍用感知，不称机器人无视觉。视觉筛选的候选另标 indirect_perception |
| structured，主线 | 上述内容＋任务相关 objects/regions、语义关系、可见性/不确定度、观测年龄、停稳/持物/资源摘要 | 判断正确目标、下一技能与等待观测；完整几何、TF 和 q/dq 留给 Grounder/Guard |
| qwen_vl，可选 | RGB→Qwen-VL 语义/指代→与既有 object_id 绑定→同一结构化输入→Laya | 先用 10–20 个录制帧任务比较颜色/空间指代；有收益再做一次 GUI 共存测量，不建设第二套执行器 |

Qwen-VL 的框、点或描述须经过标定几何和对象关联；无可靠对应就返回未知，不能直接把图像坐标当机器人坐标。[Qwen-VL 官方实现](https://github.com/QwenLM/Qwen3-VL)。约 8 GB 显存下先记录单模型开销，再决定是否值得做并发；可选实验不阻塞核心 MVP，不自动购买算力。

比较复用同一冻结任务与录制观测，记录模式实际看到了哪些信息。文本技能路由、结构化目标选择和 Qwen 语义绑定分别计分；缺少视觉的文本模式不能因无法区分两个同色对象而被算成“机械臂失败”。只有单个合法选项的样本单列；证明选择能力的样本须有至少两个有意义的选项。使用同一训练/评估分组，不把模式和传感器配置做全组合。

### 14.3 输入输出、澄清与服务边界

任务层接口：`select_task(TaskContext, SkillCatalog, PerceptionSummary?) -> TaskSelection`。TaskContext 带原始文本、澄清记录、task_session/task_revision、request_id、catalog_revision、snapshot 引用/有效期、options 及输入 hash。结构化模式明确 observation_source。此时还没有正式 Goal/command_id，不能伪造执行身份。

TaskSelection 返回原 request_id、选项集合 ID、option_id、模型 revision/hash、耗时及原始结果引用；适配层校验后解释为选择 move/fetch 或 clarification_required/wait_observation/unsupported/done。澄清问题由固定模板生成。`done` 只有全部既定成功条件被实际证据满足时才接受，不能由模型单方面宣布任务成功。

“抓取红色杯子”缺放置位置时，适配层必须询问“放到哪个区域？”，回复前不提交任何运动 Goal；不能先 move 后再问。目标有歧义则询问对象，观测不足则等待/重观察，未知技能明确不支持。规则完整性检查无条件生效，不依赖模型是否选对澄清项。用户回复合并到同一任务会话的新版本，只有有效完整绑定才提交 Goal。

初始输入预算 1024 tokens，至多 8 个任务选项（不等于最多 4 条轨迹）；超预算先按确定规则保留任务相关信息，仍超则拒绝并留截断标记，不能漏掉必需条件。choice 概率只记为偏好，未校准前不用作物理成功率或保护豁免。

独立 `laya.serve` 进程沿用 B07/B08 生命周期与单在途保护。EX 使用 Mock；任务 Laya 调用仍需登记到既有 OwnedLayaService 所有者。现有 `/service/recover` 依赖 EX backend=laya，直接 HTTP POST 也不自动进入 decision_guard；因此需补一个公开、受控的任务调用/恢复薄接线，恢复任务模型会话不得改变 EX backend 或开启其 execute。复用一个服务，不再起第二个 GPU 实例；其他任务占用时等待释放。

最多一个在途和一个最新待处理请求；热推理等待初值 300 ms（冷加载在接任务前完成），记录排队与实际推理时间。POST 写出后超时/状态不明就锁存 restart_required，健康 GET 不能清除；确认机器人停稳后再显式恢复模型会话。用户取消、澄清/任务版本、目录、相关观测或模型服务代次变化均使旧结果失效。推理不阻塞 ROS 停止，也不作为执行租期续租依据。

任务层超时不创建下一 Goal；若独立运动正在执行，首版取消当前任务并走 R 的停止流程。诊断用规则回退明确记录，不计作 Laya 成功。服务恢复后须重新检查当前任务、观测和版本，不回放旧请求。

### 14.4 开发步骤、验收与人工介入

| 阶段 | 最小开发 | 独立验收 |
|---|---|---|
| L1 | 目录投影、TaskContext/TaskSelection、CLI 澄清会话；复用已有服务 | 未完整绑定不提交 Goal；目录与 action schema 对应；20 条开发样例暴露中文能力缺口 |
| L2 | 任务调用与 OwnedLayaService 的薄接线 | 单在途、超时隔离、取消和旧版本失效；不改变 EX Mock 或启动重复模型 |
| L3 | move/fetch Contract 绑定及正式 Goal 提交 | 正确目标/区域/模板；越界与缺项拒绝；多轮澄清不重放旧动作 |
| L4 | text/structured 冻结回放对照，接 T | 正确率、澄清率、错误/拒绝/超时及延迟可比较；原模型不足时先微调，不强迫机器人执行 |
| L5 | 经 A/R 保护验收后接实际仿真 | 至少一次真实模型选择→正式 Goal→ROS→Isaac；move→fetch 在 M 验收 |

代码复用仿真控制插件的任务入口模块（与 Actor 回调分开）和既有模型服务客户端，不把业务选择写入 ROS 跟踪控制器。任务入口在 EX runtime 所在进程调用可信服务；CLI 是输入通道，不另起一个 EX 调度器。R 网关只接已校验的动作参数。供 B09 展示的 TaskSelection 与控制轨迹日志分别投影，不冒充 EX 后端的 DecisionSnapshot。

必测未知 option_id、空/NaN 结果、缺目的地、同色多目标、越界容差、模型超时/崩溃、取消、旧回复、Goal 替换；通过条件是无越权提交、无旧结果执行、日志可关联。选择质量门槛与微调比较见 T，ROS 物理证据见 R/M，不重复跑整套故障矩阵。

人工介入是回答任务本身的澄清、确认首次词汇/成功模板和查看选择对照；模型需要升级依赖或新增付费资源时先询问。Laya 无改善可如实结项，但不能把规则回退演示称为 Laya 已可用。

## 15. 参数化 Contract 与每技能 Grounder 的输入输出

上游保持已同步的 `GoalSubmit`：schema_version、request_id、ex_session、task_id、step_id、goal_id、goal_text_en、allowed_actions、parameters[action_id]、completion.required_success_actions、lease_ms 和可选 expected_revision。保留 wire 字段名 `goal_text_en`，任务层另存原始中文；按现有校验生成摘要，不更改冻结协议。每个 action_id 必须有完整且 schema 合法的参数，Laya 的裸 choice 不是可执行合同。

### 15.1 谁提供 Contract，允许改哪些内容

| 责任方 | 提供内容 | 不能做什么 |
|---|---|---|
| 技能开发者 | 版本化输入 schema、字段单位/边界、支持的 success_template、判据实现、机器人硬限制 | 不让模型生成新接口或任意判定代码 |
| 用户/自然语言任务 | 指定目标、目的地区域、允许的精度与成功要求；通过澄清补齐 | 不直接提供未经检查的关节/effort 命令 |
| Laya | 在已声明技能、对象/区域引用、成功/精度选项中选择建议 | 不增加字段、放宽硬限制或自行宣告成功 |
| 任务适配层 | 绑定并校验参数，采用公开默认值或用户确认值，冻结 contract_revision/hash，再提交正式 Goal | 不静默补缺失的放置位置或更换任务目标 |
| Move/FetchGrounder | 从语义目标、观测及本体状态计算可执行导航/抓放参数 | 不把几何失败转成成功标准降级 |
| 独立评估器 | 按冻结模板和参数读取实际结果，输出证据 | 不使用 Laya 分数或 Action ACK 代替物理成功 |

首版模板固定两类：`move.arrive_and_stop.v1` 与 `fetch.pick_place_stable.v1`。move 可参数化区域及允许的位置/朝向容差；fetch 可参数化 object_id、place_region_id、支持的放置精度/朝向约束。抬升、释放、稳定观察等判据由模板定义。可支持 standard/precision 等预先声明的配置，不支持任意逻辑表达式。

第 10 节给出默认值；E 的 20/10/5 mm 用同次落点分别统计，不能执行后择宽松档位宣告原任务成功。用户要求在支持边界内可收紧或放宽下一任务的容差；超出能力或安全边界时明确拒绝/澄清，不静默裁剪。首次执行前记录实际采用值。

合同只在任务提交/明确改需求时变更，执行期间冻结。目标或成功标准变更须取消/完成当前动作并确认资源与停止边界，再提交新版本。普通观测刷新只更新观测/计划版本；不持续修改 Goal 参数。失败后不能放宽容差把旧失败改成成功。

正式 Goal 的 `completion.required_success_actions` 继续只表示所需动作成功集合。具体 success_template/tolerance 放在本次新技能已声明的 parameters 中，由技能物理评估结果回报；不把新条件塞进上游 completion 协议。多对象候选在任务层选择并绑定，再提交选中的动作参数，不扩写 EX 固定候选结构。

### 15.2 输入输出与字段关联

| 内部结构 | 生产者与最小内容 |
|---|---|
| TaskContext / TaskSelection | 任务层：原文/澄清、会话与版本、技能目录、观测引用、选项/选择、模型和实际输入 hash；位于正式 Goal 之前 |
| SkillRequest | 插件：command_id、Goal 版本、技能、对象/区域、容差、模板引用、contract hash、时限与权限 |
| CandidateSet / PlanResult | 对应 Grounder：场景/状态 ID、planning_scene_revision、配置 hash、轨迹/导航计划引用及不可行原因 |
| TrajectorySelection | 首版固定规则，后续可选 Laya：候选集/候选 ID、选择器、依据与耗时；与 TaskSelection 分开 |
| ExecutionResult | 执行/评估：实际终态、失败/停止原因、反馈、每项物理判据及证据引用 |

任务层使用 task_session/task_revision/request_id。正式提交后补 goal_id/goal_revision/command_id；规划再补 candidate_set_id/trajectory_id，ROS 再补 Action UUID。保留完整映射，不在提交前假装已有 command_id。规划与动作字段继续复用现有 R 合同，不另建一套线协议。

### 15.3 每技能 Grounder 的最小实现

`MoveGrounder.ground(SkillRequest, MapSnapshot, RobotState, RobotProfile)` 将区域约束转为 Nav2 导航目标；根据实际地图/TF 检查可达性，到位后仍需停车证据。

`FetchGrounder.ground(SkillRequest, SceneSnapshot, RobotState, RobotProfile)` 将对象、放置区域和容差转为抓取/放置姿态、允许接触、携物几何及 MoveIt 规划请求。RobotState 包含 q/dq、底盘速度、夹爪/持物状态和阶段；RobotProfile 提供 URDF/SRDF/TCP、限制、规划和标定版本。

两者共享状态缓存、观测接口、错误分类及日志。一个技能一个 Grounder 模块，不等于每技能一个模型、进程或状态机。前置检查只判断信息完整及当前已知条件，不冒充完整规划证明。技能语义匹配也不保证当前机器人可执行。

输出保留不可变计划 ID/hash、起点/有效期、版本、检查结果和预计时长；完整轨迹仅给 Guard/Controller。后续 Laya 轨迹评分只看有单位的摘要，不负责重新计算 FK/碰撞。无效场景、过期反馈、起点超限或取消使旧计划失效。无实质变化的普通状态更新不无条件推翻全部候选。

### 15.4 一次完整任务与失败处理

```text
自然语言 → 完整性/歧义检查 ↔ 必要澄清
→ Laya 选技能/目标 → 校验并冻结 Contract → 正式 Goal
→ 对应 Grounder → Nav2 / MoveIt → 固定规则选轨迹 → Guard
→ ROS / Isaac → 独立物理成功判定 → 下一技能或结束
```

尚缺目的地时停在澄清；无可用观测时等待或有界重观察；无有效技能时返回不支持；规划不可达时返回原因。运行中取消、信息失效或超时按 R 停止，旧任务不能因迟到模型结果恢复。按模板允许的一次重观察/重规划计入同一总时限，不无限重试。

## 16. 后续扩展：Laya 轨迹评分与有限优化

本章保留原轨迹评分方向，**不列为首版核心前置**。首版 Laya 选择技能，Grounder 先给单条有效轨迹，规则选择器负责几何候选排序。核心自然语言→Contract→ROS/Isaac 链通过后，另行安排本章。

扩展时改变抓取方向或预抓取距离，以相同起点、场景版本、OMPL 流水线生成 2–4 条有效不同候选，共享总预算。Laya 在摘要中选择 candidate_id，Guard 再核对；不在 OMPL 每次采样或碰撞检查中调用模型。

`select_trajectory(SkillRequest, SceneSnapshot, RobotState, CandidateSet) -> TrajectorySelection` 与第 14 节技能接口分开。记录 command_id、candidate_set_id、scene/config/epoch、输入 hash、模型版本和耗时；未知 ID、过期或取消结果不得执行。复用单模型服务串行处理，无需另训四个模型。

优化仅限有效候选集中的选择，不保证全局最优。首版每次最多一组候选；无解时先停稳，再允许基于新状态的一次重规划，计入任务时限。模型高分不能修改目标、放宽碰撞或 effort 条件。连续运动切换、学习采样器、RRT* 对照及全身控制另行研究。

原计划的“100 组双轨迹、同初态分别执行、按实际成功/精度/时长排序”保留为本阶段数据方案，不与第 20 节的语言技能标签混用。未执行的候选标 unknown，不能推断成失败；训练/验证/测试仍按独立场景分组，真值仅用于标签。若实施本章，再对比规则、原模型、微调模型，报告离线效用与在线物理成功率，不能拿技能选择成绩代替轨迹评价成绩。

## 17. 延迟测量与精细任务

### 17.1 计时口径

分别记录任务解析、Laya 技能选择、绑定/校验/Goal 提交，以及 move_group 启动、机器人/场景加载、首次规划、感知、Grounder、Guard 和 ROS 交接。Laya 冷加载单列；后续轨迹评分另设字段。用户等待澄清的时间属于交互时间，不算模型计算延迟。`T_task_decision` 从任务信息和所需快照齐备计到正式 Goal 受理；`T_decision` 从当前技能的结构化快照可用计到执行器接受新计划。首次全链另计技能选择、绑定和交接，不能把它与只做局部重规划的时间混为一谈。`T_sensor_to_action` 从原始传感器采集计到动作真正开始，覆盖感知处理。两者均使用同一墙钟口径，不能将仿真时间戳直接与墙钟相减。若采集端只有仿真时间戳，先从接收端计时并注明缺少采集/传输延迟。执行完成耗时单列。

使用单调墙钟。规划计时从快照准备完成开始，覆盖场景应用应答、ROS 请求排队、全部候选求解、时间参数化和检查；收到完整响应才结束。MoveIt 返回的 planning_time 另列，不能代替端到端墙钟耗时。

不把仿真时间当计算延迟。记录硬件、MoveIt/OMPL/控制器版本、planner_id、碰撞分辨率、规划预算、候选数、障碍数、轨迹节点数、输入 token 数、GUI/传感器状态和仿真实时因子。

| 工况 | 要区分的问题 |
| --- | --- |
| 无障碍、宽松朝向、单候选 | 求解基础开销 |
| 同一目标、收紧位置/朝向容差 | 精度约束带来的开销 |
| 普通障碍、首版单候选；后续扩到 4 个 | 分开测基础规划与可选多候选/轨迹评分开销 |
| 已知几何的窄间隙 | 初值、碰撞检查、失败重试开销 |
| 执行中目标/障碍发生一次受控变化 | 停止、刷新、重规划与恢复延迟 |
| 故意延迟 Laya 或中断状态反馈 | 超时与停止是否独立生效 |

前四类先各测 30 次轻量规划调用；轨迹评分未开发时记 N/A，不伪造耗时。技能选择延迟复用 L/T 冻结回放，不另跑完整抓放。变化目标或初值，避免只测缓存。复用已有状态快照，不重复执行 120 次完整抓放。后两类各做一次物理故障/变化注入，再按失败原因补测。

报告 p50、p95、最大值、失败率和超时率。失败与超时保留在总数中，另列成功请求的求解耗时，不能只报告成功快速样本。

这组样本用于 MVP 性能筛查，不证明最坏延迟上界。若目标相对夹爪以 0.1 m/s 运动，300 ms 内可变化 3 cm。窄间隙需要低速、最新反馈和独立停止，不能只依靠上层选择与重规划追赶变化。

### 17.2 暂定预算，全部需要实测

| 项目 | MVP 目标 |
| --- | --- |
| 单次 Laya 热技能选择 | p95 ≤100 ms；后续轨迹评分单独统计，不能相互替代 |
| 简单场景热重规划至执行器接受 | `T_decision` p95 ≤300 ms，包含 Grounder、规则选择、Guard 和交接；后续启用轨迹模型时必须把评分计入。含感知的 `T_sensor_to_action` 另报 |
| 冷启动与首次规划 | 节点/机器人/场景/模型准备在运动前完成，时间单列 |
| Controller/最终保护 | 独立周期执行，起步按 100 Hz 级设计并与物理步长匹配 |
| Laya 技能选择 | 新请求、澄清、阶段完成或相关环境变化触发；无需每个控制周期重算 |
| 局部规划/候选更新 | 事件触发，最高约 5–10 Hz 只是待测上限；首版停稳重规划，无连续切换承诺 |

这些数值是工程目标，不是已获得的成绩。初次规划和窄间隙规划单独报告。如果全链超过 300 ms，即使相关 Laya 调用只花 30 ms，也不能宣称满足全链目标。顺序演示可以停稳后等待计划，但必须注明此限制。

最终 GUI 工况同时测时间，包含渲染、传感器与模型竞争。MoveIt/OMPL 默认使用 CPU，GPU 留给 Isaac 渲染和现有 Laya；不为此引入 GPU 规划器。预算不足时先减少候选数量或允许停稳等待，并如实报告未达到热重规划目标。B07 短文本延迟不能直接搬作当前技能/轨迹成绩。text/structured/qwen_vl 分开报告；Qwen-VL 的图像处理、对象绑定、模型加载与排队均计入自身链路，不能只报最后一次 Laya 调用。

### 17.3 精细任务的最小扩展

沿用同一机器人和运行器。普通抓放通过后，只加“较小落点容差”和“已知夹具窄通道”两种配置。以夹爪和携物外形定义通道，检查整臂扫掠，避免只检查 TCP。

具体精度研究统一放在第 23 节：ORACLE 基线、两种 SENSOR 配置和 ORACLE_PERTURBED 干预分别记录。A5 直接复用 E 的精确放置与窄间隙工况，不再另建精细任务实验。记录落点误差、实际间隙、跟踪误差、失败阶段和延迟。

若 ORACLE 已满足精度而 SENSOR 不满足，先改感知。如果候选轨迹有效但跟踪不准，先改控制。如果候选经常卡在局部解，才评估学习式初值或其他规划器。Laya 排序本身不能提高编码器分辨率或消除标定误差。

## 18. 开发项目 A：MoveIt 2 机械臂经典基线

状态：PLAN ONLY。目标：固定底盘，以 xArm6/夹爪、MoveIt 2＋OMPL、Guard、ROS 控制器和 Isaac 完成物理抓放。此阶段采用 rule，不加载 Laya，不训练模型。

### 18.1 依赖、复用与缺口

- 复用现有 Isaac 环境与隔离工具；原进程锁、日志监督和状态新鲜度实现已随 CartPole 归档，后续入口按需复用。已退休的 `MoveCartTo.srv` 和 CartPole service/controller 只覆盖一维滑轨，不扩成机械臂接口，不移植 LQR 或 5 N 阈值。
- 复用已安装 MoveIt/OMPL/ros2_control，以及厂商 xArm Jazzy 描述和配置。补 URDF/USD/SRDF/TCP/夹爪一致性、场景转换、规划调用与结果关联。
- 与 R 共用标准控制器、topic-based hardware 和最终执行保护。A/R 只做一套物理基线，不再自研通用轨迹执行器。
- A1–A3 可先使用 ORACLE；A4 接 P1–P3；A5 由 E 的同一批精细任务完成。无需等待 B10 或完整 A.E.B.。
- B08/B09 已有管理和页面，不重建；后续通过已有动作 details、观测和真实后端投影展示机器人结果。

### 18.2 最小代码位置与调用关系

后续只建一个应用包 `ros2_ws/src/astrex_mobile_manipulation/`，容纳执行内部合同、感知适配、Move/FetchGrounder、MoveItAdapter、ROS 网关和评价。任务层 Laya 客户端放 EX 插件的任务入口模块，见第 14 节，不塞入轨迹跟踪控制器。MoveItAdapter 使用 Jazzy 的 rclpy 服务客户端；规划算法在现成 move_group 中，不另建求解 worker。

另建纯配置包 `ros2_ws/src/astrex_mobile_manipulation_moveit_config/`，安装组合 URDF/SRDF、IK/OMPL/限位/控制器配置及 launch。厂商描述保持独立依赖与固定版本，不在其源码中改业务逻辑。不要同时维护配置包内和 `config/` 下两份机器人限位。

`sim/scripts/run_mobile_manipulation.py` 是同一仿真场景，支持 sensor_only/control；`scripts/run_mobile_manipulation_trial.py` 是同一任务入口。场景、实验种子和 P/E 配置放 `config/mobile_manipulation/`。仿真控制插件放 `apps/AstrBotEX/plugins/control/astrex_sim_control/`，默认禁用，不改现有插件，不恢复已归档的 CartPole 场景。

原内部入口保持：`ground(SkillRequest, SceneSnapshot, RobotState, RobotProfile) -> CandidateSet`。内部替换为 MoveItAdapter，外部 EX Goal/Action 合同不变。Laya 继续只看到候选摘要，长轨迹留在 ROS 网关内以 ID/hash 关联。

### 18.3 开发步骤与可独立验收结果

| 步骤 | 最小工作 | 完成条件 |
|---|---|---|
| A1 模型与状态 | 加载 xArm 配置；核对 3 个姿态 FK/TCP/关节、夹爪开闭及原始反馈 | URDF/USD 一致；状态来自 Isaac，不来自 fake/mock hardware |
| A2 场景与单候选 | ORACLE 下同步碰撞世界；GetMotionPlan＋RRTConnect；检查时间参数 | 可达、不可达、简单绕障结果明确；无自动执行，无效轨迹不下发 |
| A3 物理抓放 | 与 R1–R3 共用网关和控制器；短接近、闭爪、抬升、搬运、放置 | 真实接触持物；阶段场景同步；满足第 10 节物理判据 |
| A4 感知接线 | 接 P 的 SENSOR 输入；3 个开发场景检查来源、版本、过期处理 | 不读随机对象真值；场景/附着/起点变化使旧候选失效 |
| A5 精细配置 | 直接使用 E 的精确放置与窄间隙任务 | 相同规划配置下输出精度—成功率数据，不重复采样 |

首版 A2 单候选打通即可；第 16 节后续需要轨迹对照时才扩为 2–4 个。rule 固定排序：先满足约束，再选预计执行时间较短者，持平按 candidate_id。不能通过调整规则掩盖模型比较结果。

### 18.4 验收、证据与人工介入

必测场景包括正常抓放、不可达、起点超限、场景应用失败、场景变更后的旧计划、持物后碰撞几何、部分 Cartesian 路径和规划超时。机器人运动/停止故障沿用 R01–R09，不另开重复矩阵。

证据包含机器人与 MoveIt 配置 hash、规划请求/响应码、scene revision、原始起点、候选/选中轨迹 hash、Action UUID、驱动限制、反馈及物理结果。阶段边界重新取反馈；持物不能只根据 GripperCommand 的成功码判断。

人工介入：首次 GUI 确认夹爪、碰撞几何与相机工作区。资产确实无法匹配、必须替换机器人或升级仿真环境时，先问用户。包配置、命名空间和常规规划参数按本方案执行，不另加选型轮次。

### 18.5 与现有施工任务的适配

| 现有工作 | 本轮规划采用方式 |
|---|---|
| B04 Goal/停止/切换 | 复用公开方法与操作回执，不再读取私有 pending 标志判定完成 |
| B07 EX Laya | 保留模型/服务与测试边界；L 在任务入口做技能选择，不放开普通 EX execute |
| B08/B09 | 复用管理、SSE 和页面；控制数据由真实后端接入，不新建管理系统 |
| B10 | 仍非首轮前置；以后替换识别端，沿用 P 的几何与观测引用 |
| B11 / 第 19 节 | 同一网关与停止工作；旧任务书的 PyRoKi 名称映射为 MoveIt，不重复派工 |
| CartPole/MoveCartTo | 源码、接口、入口与专用测试已归档；保留证据和工程参考，不再作为活跃依赖 |
| P/E/L/T/M | P/E 保留原实验；L/T 改为技能选择与参数化 Contract；M 汇合整链，MoveIt 规划与模型选择分别记录 |

本计划的机器人开发仍待实施。B11 的章节链接和原文件名保持有效；CartPole 清理不覆盖施工索引及 B09 等其他任务的工作区修改。

## 19. 开发项目 R：ROS 2 执行网关与实际闭环

状态：机器人执行 PLAN ONLY。既有上游 ROS 测试只证明通信，B04 停止回执和 B08/B09 管理已有；机械臂状态、驱动、取消及物理证明仍由本项目实施。B11 与本章是同一工作。

### 19.1 复用与边界

复用 EX `context.ros` 的原生发布/订阅、绑定代次、队列与 quiesce。当前 facade 无完整 Action 客户端能力，使用薄网关转换；插件不自行 init/spin ROS，也不扩建通用 Action SDK。

机械臂 Action 使用 `control_msgs/action/FollowJointTrajectory`，由现成 joint_trajectory_controller 提供；夹爪使用 `control_msgs/action/GripperCommand`，由匹配实际夹爪的标准控制器提供。底盘后续调用 Nav2 的 `NavigateToPose`。不导入真机驱动，不用 fake/mock hardware 的目标回显作为 Isaac 反馈。

执行主线为外部 Jazzy `controller_manager → 标准控制器 → topic-based hardware → Isaac ROS 关节入口`。优先采用 Jazzy 的 `joint_state_topic_hardware_interface/JointStateTopicSystem`；若 R1 确认只能复用锁定旧示例，则用经 Jazzy 构建验证的 `topic_based_ros2_control/TopicBasedSystem`，二选一，不并行维护。[Jazzy topic hardware](https://control.ros.org/jazzy/doc/topic_based_hardware_interfaces/doc/index.html)。系统安装目录原先没有这两个包；本轮已在项目私有前缀补齐前者并激活标准控制器。不能将最新 Isaac 的进程内 ros2_control 扩展当作 5.1 已有能力。

arm 与 gripper 的资源和命名空间明确。hardware 输出内部 `/astrex/mm/joint_command_raw`；同一网关内薄转发器形成 `/astrex/mm/guarded_joint_command`，只有后者可进入 Isaac drive。原始反馈为 `/astrex/mm/joint_states_raw`，名称在配置中集中映射。只允许一个最终命令源；关节广播器发布的状态与原始反馈区分，不混入 CartPole 话题。

硬件接口会缓存最近状态，joint_state_broadcaster 的新时间戳不能证明 Isaac 提供了新观测。Guard/watchdog 必须同时核对原始消息 stamp 和本机接收单调时间。命令变化阈值可能抑制重复发布，因此独立 lease/heartbeat 不依赖位置命令频率。

插件 start/cancel 回调只受理转交，预算沿用上游默认 20 ms。推理、求解和等待结果不能阻塞 Actor 或 ROS 状态回调。

### 19.2 最小通信合同

EX 与网关使用独立 `/astrex/mobile_mvp` 命名空间，通过现有 facade 支持的原生消息交换。首版采用 `std_msgs/msg/String` 承载严格校验的有界 JSON，避免另建自定义消息包。ROS 内部轨迹仍使用标准 Action。

| 通道 | 内容 | 行为 |
|---|---|---|
| `command` | schema、command_id、ex_session、goal_revision、execution_epoch、deadline、参数/轨迹标识 | reliable/volatile；无 transient 重放；每次只受理一个动作 |
| `cancel` | 同一身份与取消原因 | 独立入口；取消使旧 epoch 失效 |
| `lease` | 活动 command_id、会话/epoch、递增续租序号 | 活动且授权有效时每 100 ms 续租；重复旧序号不能续期 |
| `ack` | 受理/拒绝、原因、执行器会话 | ACK 不等于执行成功 |
| `feedback` | 状态序号、command_id、阶段、误差、关节/夹爪/底盘摘要 | 只保留最新反馈；执行器重启可识别 |
| `result` | 终态、选中候选、物理判据、停止引用 | 一次终态，重复结果幂等处理 |

JSON 上限 64 KiB，队列有界；超预算明确拒绝，不能截断轨迹。完整 RobotTrajectory 保留在网关内，以 ID/hash 关联，模型只读摘要。规划使用标准 ROS 服务；任务 Laya 在正式命令之前调用已有本机 HTTP 服务，网关首版不等待轨迹模型。不新增通用 IPC 规划协议。每条通路最多一项活动请求和一项最新待处理请求。

标准 JointState 命令没有 command_id/租期。网关保存 command_id、轨迹 hash 与 Action UUID 的映射；薄转发器只给控制器输出加 `execution_session / execution_epoch / command_id / seq / source_stamp` 封套，以受限 String/JSON 送到最终入口，不插值或重算轨迹。最终入口验证身份、递增序号、新鲜度和独立租期，不直接订阅未封套的 raw 命令。

EX 只向网关续上游授权。网关仅在上游授权、当前 Action/执行会话及自身健康有效时续最终租期；EX 不能绕过网关续租。此心跳不排在规划或模型调用后面。停止/保持阶段使用有界的停止会话，不能继续授权旧任务运动。

租期失效、会话变化或 STOP 时，最终入口屏蔽旧位置目标，以 Isaac 本地 q/dq 进入有限 drive effort 和阻尼下的保持/停止。即使网关退出而 JTC 仍发旧命令，也不得无限执行。有限 effort 不保证特定减速度，实际停止时间、位移与限制是否满足均需测量。

恢复转发前，确认旧 Action 终止、清空 raw 缓存、新 Action 已受理并产生对应 Goal 的新反馈。只转交该反馈时间之后的 raw 命令，不能将旧缓存重新包装成新 epoch。仿真时间回退或执行器重启立即闭门，重新建立会话。上述身份和租期边界在 R2 实测，不假定标准硬件插件自带。

对 command_id 保留当前执行器会话内的去重结果。执行器重启后进入无活动命令状态，旧消息不恢复运动；EX 将未确认旧动作保持 unknown/blocked，取得停止证明后才允许新授权。

租期初值 500 ms，状态最大接收年龄 200 ms，ACK 等待上限 1 s；这些是仿真配置，不是硬实时保证。动作结束时限由 Goal 和动作配置中较紧的一项决定。最终执行端每控制周期检查租期，不依赖上游正常发送 stop。

### 19.3 执行与停止

1. 插件验证受理条件，经 context.ros 转交网关并返回 accepted，不等待规划。
2. 网关同步 PlanningScene 并调用只规划服务；固定规则选择后（后续第 16 节才可加入轨迹 Laya），由 Guard 复核原始反馈、scene revision 和轨迹 hash。
3. 网关提交已选中的标准 Action，关联 command_id 与 Goal UUID；MoveIt 不另行执行或重规划替换选中轨迹。
4. 标准控制器跟踪，最终 Isaac 入口执行租期与驱动限制；网关根据真实反馈报告 running 和阶段状态。
5. 独立评估器判断抓放结果。Action 成功但物理判据未满足，任务仍失败。

Guard 检查目标、模型/config hash、有限值、碰撞、关节/速度/加速度及起点。最新 q 与计划起点容差初值为每关节 0.02 rad；首版只从停稳状态开始新轨迹。状态序号正常递增不会自动使所有候选失效。

普通取消通过正常受控接口。`publish_stop` 仅在现有环境失活授权 hook 中使用，不将其当作通用取消捷径。

停止先失效旧 epoch，阻断旧任务目标，并取消活动 Action。底盘停止，机械臂由最终入口在有限 effort 下停止/保持；夹爪持物时保持有限夹紧力。正常取消和断租首版共用这条本地停止路径，减少分支，不依赖远端 JTC 继续输出，也不宣称满足普通轨迹的加速度指标。

本机 JTC 4.42.1 头文件已有 `constraints.decelerate_on_cancel` 和各关节 `max_deceleration_on_cancel`，默认未启用。最终入口关闭转发后，不能再以该选项证明受控减速。仅当 R2 实测表明本地停止位移不满足场景要求，才追加有界制动会话与 JTC 减速配置；不要为首版预建两套停止策略。

关节 `abs(dq) <= 0.02 rad/s` 且底盘满足第 10 节停车阈值，连续 0.5 s，才生成停稳证据。ROS 反馈中断时，Isaac 可在本地执行停止，但上层取得可验证的新鲜测量前不能报告 proven。停止测量记录 command_id、执行会话、采样窗口和实际制动时序。

插件取得证据后才调用 `context.actions.report(..., stop_evidence=StopEvidence(...))`。冻结 StopEvidence 字段仍为 `command_id / stopped / source / reference`；执行器会话、采样窗口、q/dq、制动与取消时点存证据文件，以 reference 关联，不扩写上游 dataclass。

上层停止使用现有 `request_stop()` 回执，并核对 `status()["stop"]` 的 operation_id、ex_session、gate_epoch 与 `state="proven"`。Action 取消 ACK、MoveIt 返回或 `_stop_pending=False` 均不替代物理停稳。unknown/failed/timed_out 后续通过可信 reconcile_stop 释放资源，不改写原终态；GUI 暂停不算停止证明。

### 19.4 开发与必测矩阵

| 步骤 | 范围 | 通过条件 |
|---|---|---|
| R1 | 补一个 topic hardware；配置 controller_manager、标准 Action、原始状态与端口 | ACK/反馈/结果可关联；实际反馈来自 Isaac，重复请求不重复执行 |
| R2 | Guard、租期、取消、Isaac 最终入口 watchdog | 网关退出/JTC 继续发布仍能停止；陈旧原始反馈和旧命令不能恢复执行 |
| R3 | 与项目 A 合并完成真实抓放 | ROS 交接、Isaac 运动及物理判据完整 |
| R4 | 接入已通过 L/T 的任务 Laya 后运行故障工况 | 模型选择技能→正式 Goal→ROS 可追踪；任务旧选择和轨迹旧结果都不会误执行 |

| ID | 场景 | 必须观察的结果 |
|---|---|---|
| R01 | Laya 正常选择 fetch 并实际抓放 | TaskSelection→Contract→EX Goal/command→ROS Action→物理结果可追踪；标明原始/微调模型，不以规则回退充数 |
| R02 | 无解/无有效候选/窄间隙 | 明确拒绝或受限完成，无伪成功或关闭碰撞检查 |
| R03 | 超限、NaN、错误关节/起点 | 执行前拒绝；最终驱动限制仍有效 |
| R04 | 运动中取消 | 独立制动并保持，StopEvidence 对应本次命令 |
| R05 | Laya 或 MoveIt 超时、进程崩溃 | ROS 停止仍响应；规划服务迟到和模型旧选择均不能执行，不叠加后台请求 |
| R06 | 旧 Goal、旧评分晚到 | 旧 epoch/config/candidate 不能覆盖当前任务 |
| R07 | 原始 Isaac 状态中断，但 broadcaster 仍发布 | 按原始反馈年龄触发 watchdog，不能被缓存状态的新时间戳掩盖 |
| R08 | EX/网关退出、JTC 继续发旧目标、执行器重启 | 最终入口按租期停止；恢复时拒绝旧缓存和迟到命令，未确认保持 unknown |
| R09 | Isaac GUI 开启、模型同时推理 | 实际运动和所有延迟口径可测，不只记录离线速度 |

watchdog 验收记录“过期到发起制动”，目标不超过一个配置控制周期；实际停稳时间与位移单列，不承诺瞬间停止。非实时 Linux 的超时也计入报告。

B09 当前控制端投影尚未接通。由同一日志/观测及动作 details 提供 `task_session / task_revision / skill_option_id / contract_hash / candidate_set_id / trajectory_id / planning_scene_revision / ros_goal_uuid / physical_result`，经真实管理后端投影到页面；不假设新增 details 会自动填充所有展示区。

人工介入：用户查看一次执行中停止演示。停止或驱动限制没有证据时，不开启后续 Laya 物理执行；先反馈具体缺口。

## 20. 开发项目 T：一次小规模 Laya 技能选择微调

状态：本轮实施 T1 数据冻结与分组检查，不运行训练。沿用既定的一次小规模微调要求，将目标改为自然语言/结构化状态下的技能与目标选择；轨迹成对评分训练留在第 16 节。前置为 L 的固定输入、可复查标签和模型服务边界；训练不必等原始模型先完成机器人任务，训练后的运动验收仍必须通过 A/R。

### 20.1 数据与固定版本

代码沿用 `0.3.22` / `6d942c92081fbc139e736bbd9ac0023223c29b7f`。根据 L 的中文开发探针固定一个 checkpoint 及精确 revision/hash；如果采用 multilingual，单独登记，不覆盖 B07 原 typed-decisions 权重或伪装其身份。依赖以现有 `requirements-laya.lock` 为基线，不兼容则先报告，不能自动升级共享环境。[官方训练 notebook](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb)。

固定 **100 个独立任务/场景组，70/15/15** 分训练/验证/测试。一组可以含一次完整澄清和 move→fetch 的多个决策点；同一任务的全部改写、同布局、连续帧及不同输入模式不能跨集合。按用户 2026-10-04 确认，不同独立任务／场景允许复用语序；结果只验证已支持表达下的选择能力，不宣称新语法泛化。划分先冻结并保存 hash，P/E/M 最终评估种子不进入训练或验证。模型选型的 20 条开发探针不进入独立测试。

覆盖直接 move、到位 fetch、先 move 后 fetch、缺放置位置、同色多目标、未知请求和观测不足；测试组覆盖所有关键类别。人工核对标签对应用户意图与已声明前置条件。无唯一正确选项时标可接受集合或 clarification/wait，不能随意指定对象。首轮不以模型自己输出作为 gold。

技能标签来自任务语义和已知状态，不需要为 100 组各跑两条物理轨迹。可复用 SENSOR 录制摘要和固定规则构造负例；合成语言/状态与实际观测分别标来源，不能把其分类准确率写成机器人成功率。模型只见当时可获得的文本/观测；未来动作结果、理想动作标记和评估真值不进入输入。

按官方 API 映射 `state + questions + gold`，使用 choice 头进行技能/目标训练。固定两个运动技能与任务状态，不扩成动态技能生成平台。skill、object、region、success_template 各项正确性分别记录，不能只统计“JSON 可解析”。

### 20.2 一次训练、离线对照与实际接线

| 步骤 | 工作 | 完成条件 |
|---|---|---|
| T1 | 校验 100 组语义/状态标签与分组 | 无同场景/改写泄漏，缺项/歧义标签可复查 |
| T2 | 冻结 encoder，训练 decision head | encoder 不更新，损失有限；产出一次记录和 checkpoint |
| T3 | 验证集选 checkpoint | 测试集不参与选 epoch、提示词或阈值 |
| T4 | 固定输入比较 rule / 原始 Laya / 微调 Laya | 分别报告 exact skill/target、澄清/等待、误选、拒绝/超时、延迟及分母 |
| T5 | 在 A/R 已通过的同一链上执行合格选择 | 明确实际模型身份；至少一条完整模型选择→ROS→物理结果；M 验收 move→fetch |

规则基线采用相同目录和参数检查，保留其能力边界；规则修正、拒绝和回退单列，不冒充模型答对。可接受的语言/目标选择正确率初始门槛为独立测试决策点 ≥80%，样本小需同时给分子/分母；缺必需信息时零运动提交、无旧结果执行和无越界 Contract 是硬要求，不用平均正确率抵消。通过分类门槛也不等于通过物理执行。

初始训练：seed 20261003、最多 10 epochs、batch size 2、梯度累积 4、head learning rate 1e-4；按验证损失选 checkpoint，连续 3 epochs 无改善提前停止。不做超参数搜索。训练暂停 Isaac GUI，显存不足先降 batch 至 1，再考虑 CPU 并如实记录耗时；付费算力先问。

部署通过既有服务所有者增加受限的 checkpoint 登记，记录本地路径、独立模型名和实际 hash。停止旧推理、核对新服务代次/身份后才接受新请求，不能让微调文件冒充原始固定 revision。保持 EX 默认后端和普通 execute 限制，单模型加载，不开放任意路径执行或自动下载。

原模型离线选择不合格时如实记失败，不强行送机器人；微调模型也须经相同 Contract/Guard。若没有模型达到标准，交付训练与失败结论，保留规则链可运行，但标注 Laya 核心演示尚未通过。允许“微调完成但无改善”，不为制造提升修改最终场景。

### 20.3 产物与人工介入

数据、权重和日志放 `/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/` 的对应 run；权重不入 Git。仓库只保存配置、分组/hash 清单和结果报告。冷启动、热推理与 GUI 共存开销按第 17 节分别测。

用户查看标签小样、三组对照和是否采用微调 checkpoint。引入新依赖版本、付费资源或改变控制保护前必须询问；常规小样本训练与记录沿本计划执行。

## 21. 开发项目 M：移动抓放与 GUI 演示

状态：PLAN ONLY。依赖：项目 A/R 的 SENSOR 单臂闭环；Laya 和微调能力按项目 L/T 分别记录，不以导航成功替代。

### 21.1 输入、输出与最小实现

任务入口接收自然语言和必要澄清，Laya 选择后由适配层提交正式 Goal；控制端接目标颜色/已知几何、操作台区域、冻结的任务容差与成功模板、RGB、LaserScan 或 PointCloud2、odom/TF 和关节状态。传感器与定位代码复用 P，默认配置依据 P/E 结果选择。输出关联自然语言/澄清、TaskSelection、Contract/Goal、Nav2 结果、停车证据、新场景快照、抓放结果和最终物理判据。

底盘使用履带外观加差速等效轮组，物理接触驱动；不逐帧写世界坐标。两套感知配置的可观测性约束遵守第 5 节，3D 数据需保留机械臂使用的高度信息。Nav2 采用 Smac 2D 与 Regulated Pure Pursuit，近场速度保护复用 Collision Monitor。

首轮任务保持“导航 → 停稳 → 重观察 → 同台抓放 → 撤离”。操作台附近最多 3 个停车候选，拒绝后有限重试。移动时机械臂收拢，首版不携物跨区行驶。

### 21.2 开发步骤与故障处理

| 步骤 | 工作 | 验收 |
|---|---|---|
| M1 | 差速模型、雷达、odom/TF、Nav2 配置 | 3 个开发场景绕障到位，不读取随机障碍真值 |
| M2 | 顺序任务与资源约束 | 底盘未停稳不允许伸臂；导航后使用新观测 |
| M3 | 接 L 的自然语言/澄清与 Laya move→fetch，沿用 EX/Ledger/B09 | 缺放置位置先追问；每步选择、Contract、反馈/失败可关联，无第二套任务状态机 |
| M4 | 冻结 10 个可行种子和 2 个失败种子 | 正常场景至少 8/10 完成；失败场景正确结束 |
| M5 | GUI 中展示冻结场景及停止 | GUI 工况计入 M4，保留 RGB、雷达/代价地图和运行日志 |

导航封路或超时返回有原因的失败；不瞬移到操作台。目标观测不足则重观察一次，仍不足结束。底盘停车、机械臂执行、停止均依据反馈，不能仅靠固定 sleep。

上游暂不支持运行中追加独立动作轮次，因此任务适配层在每个宏动作完成并释放资源后，再让 Laya 根据新状态选择并提交下一轮；旧目标身份或任务约束失效时先澄清。一个抓放动作内部可包含多个有限阶段，但不能生成新的上游 Goal 或扩大原任务授权。

### 21.3 最终证据与人工介入

输出每个冻结场景的输入来源、模型/软件版本、选中轨迹、ROS 交接、物理结果、失败原因、阶段耗时和实时因子。记录碰撞、掉落和未知状态，不能因 8/10 达标而隐去失败。

用户查看一次完整移动抓放 GUI 演示和微调对比。本计划已经包含仿真 RGB＋2D/3D 雷达比较，无需再次确认这一范围。若要加入 RGB-D、实物传感器、替换机器人资产、全身联合控制或付费算力，先说明原因并询问。


## 22. 开发项目 P：不依赖 YOLO 的独立仿真感知验证

状态：PLAN ONLY。目标：在 Isaac 中比较 RGB＋2D 激光雷达与 RGB＋3D 激光雷达，明确各自的观测范围、定位误差和 ROS 数据延迟。这里的雷达指 LiDAR，不引入毫米波雷达。P 完成后可以交付感知结论，不必等待机械臂或 Laya。

### 22.1 已有基础、缺口与最小范围

| 项目 | 当前基础与后续工作 |
|---|---|
| 仿真环境 | 复用 `config/isaac_baseline.env` 的 Sim 5.1 / Lab 2.3.2 / Jazzy 与环境隔离；原进程锁和日志监督实现保存在归档中 |
| 现有入口 | `scripts/lib/isaac_entry.py` 仅支持环境健康检查；原 ROS/RL 入口已归档，传感器场景入口待开发 |
| 场景基础 | 原 CartPole 场景已归档，只作为时钟、关节状态和控制图的历史参考；新场景需显式创建并发布 RGB/雷达 |
| 现有雷达工具 | `apps/AstrBotEX/scripts/lidar_scan_visualizer.py` 是 LaserScan 消费者。它不生成雷达，也不提供经过 TF/里程计补偿的移动地图 |
| 新增最小内容 | 一个场景的 sensor_only 模式、两套传感器配置、一个经典感知适配和同一个评价器 |
| 不作为前置 | B10 YOLO、B08/B09、A.E.B.、MoveIt/OMPL、Laya、完整底盘、Nav2、机械臂 Action |

沿用第 18 节拟定位置：`sim/scripts/run_mobile_manipulation.py`、`scripts/run_mobile_manipulation_trial.py`、`config/mobile_manipulation/` 和 `ros2_ws/src/astrex_mobile_manipulation/`。先只实现其感知子集。sensor_only 不创建控制器或发送运动命令，后续 A/R 扩展同一入口，避免建设第二套实验工程。这些文件与参数均为待开发设计，不是当前可执行命令。

场景仅需地面、操作台、彩色方块/圆柱、少量箱体和可固定安装的相机/雷达支架。首轮保持支架静止，物体位置按种子变化。放置区使用已知尺寸的彩色矩形标记，夹具使用可从 RGB 识别的颜色边界或角点。SENSOR 的在线中心和朝向来自观测；ID、尺寸可作先验，配置中的真实位姿只供评估。改变支架安装位属于单独配置，不能只在一套传感器方案中改变有利视角。

### 22.2 两套传感器配置与数据链

| 配置 | 发布数据 | 几何来源与边界 |
|---|---|---|
| `RGB_LIDAR_2D` | RGB Image、CameraInfo、LaserScan | 地面障碍来自扫描；目标来自颜色/轮廓＋显式桌面与物体几何 |
| `RGB_LIDAR_3D` | 相同 RGB/CameraInfo、PointCloud2 | 目标识别方法相同；点云可补平面和三维障碍，点数不足时明确无效 |

Isaac 官方提供 RGB/CameraInfo 和 2D/3D RTX LiDAR 的 ROS 发布示例。优先采用原生 `Example_Rotary_2D` 与通用 3D 配置，不自行模拟激光传播。[相机发布](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/tutorial_ros2_camera.html)、[雷达发布](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/tutorial_ros2_rtx_lidar.html)。固定实际配置及其 hash；仿真资产不等于未来已选硬件。

拟用话题为 `/astrex/mm/camera/image_raw`、`/astrex/mm/camera/camera_info`、`/astrex/mm/scan` 或 `/astrex/mm/points`，以及对应 TF。进程只维护一个 `/clock` 发布源，ROS 消费端使用仿真时间对齐。相机光学坐标、雷达坐标、场景基准坐标之间的外参均显式保存。数据通过实际 ROS 2 通信到系统 ROS 进程，不以进程内数组替代发布链路。

起步配置为 RGB 640×480 / 15 Hz、雷达 10 Hz。记录雷达视场、扫描高度、最小测距、角分辨率和完整扫描/逐帧发布方式。图像与扫描配对时间差初值不超过 50 ms，接收年龄沿用 200 ms 初值。以上均为开发起点；第一次 GUI 检查后冻结，并记录实际帧率和配对失败率。使用有界最新帧队列，避免推理变慢时持续积压。

两配置顺序运行，保持相机、布局、光照、随机种子及处理算法一致。第一轮使用共同的已知桌面先验，比较扫描平面与三维覆盖；若再研究点云估计桌面，单列配置，不将先验变化归因为雷达维度。点云与图像融合必须使用对应时间的 TF 和相机投影；目标区点数不足或前后景遮挡不清时，不能随意把最近点赋给目标。

RGB 路线不使用相机深度图、实例 mask 或语义标签。使用颜色阈值和轮廓，不下载或训练检测模型。若由 3D 点云裁切产生二维扫描，标记 `PROJECTED_SCAN`，不能作为原生 2D 雷达配置的同名结果。

### 22.3 分阶段实施与验收

| 步骤 | 最小工作 | 完成条件 |
|---|---|---|
| P1 传感器链 | 创建场景和 RGB/雷达/TF/clock；通过 ROS 在 GUI/RViz 对照 | 两配置各连续采集至少 30 秒；能关联消息、时间、坐标和配置，无重复时钟发布 |
| P2 经典感知 | 颜色与轮廓定位，射线/平面几何，雷达障碍提取，SceneSnapshot | 输出米制位置、可观测方向、覆盖和未知状态；缺 TF、过期或不可定位时不伪造位置 |
| P3 配对比较 | 固定 5 个研究布局，改变目标远近/朝向；两配置各取同样观测窗口 | 保存全部误差、有效率、覆盖与耗时；可以结论为某配置不满足某类任务 |
| P4 交接 | 将同一感知适配接到 A4，再由 E 做任务实验 | 不复制视觉程序；相同 observation_id 可追到后续计划与结果 |

P1–P3 先交付，不等待 P4。数据中至少包含无遮挡目标、桌面边缘目标和局部遮挡。另做扫描平面以下/以上障碍、过期图像、缺 TF 三类边界检查。盲区检查只测观测能力，未知区域要保留，不能为了让 2D 通过而宣称空间空闲。

每个布局保存固定时窗的原始数据或有索引的选定帧，不把连续帧当作独立场景。采样时窗与取帧规则先冻结，不能挑误差最小的一帧报告。遥测指标为有效观测率、目标位置/方向误差 p50/p95/样本最大值、障碍漏检与未知覆盖、ROS 收到数据至快照生成的墙钟延迟和仿真实时率。未知目标高度或不可观测方向单独标记。

P 的工程验收看数据链和评价是否完整、错误是否如实报告。感知能力按每项任务的容差判断，不预写“厘米级已通过”。传感器方案未达到所需精度属于有效结果；程序崩溃、坐标错误或评价缺失则属于未完成。

### 22.4 交付、后续接口与人工介入

交付包括传感器/标定配置、SceneSnapshot 字段说明、原始观测索引、逐场景指标和两配置对照表。运行数据放统一日志目录的 `perception/` 子目录；只将配置和精简报告纳入仓库，不提交图像包或点云包。

B10 后续替换目标检测入口，复用 CameraInfo、TF、几何重建和时间规则。经典颜色结果标记 `perception_backend=classical_rgb`，不能登记成 YOLO 验收。Grounder/Laya 继续读结构化快照，不承担相机解码或雷达驱动。

人工介入：首次 GUI 中确认相机能看到目标及未来夹爪工作区，并查看扫描平面/点云范围。临时支架通过不代表机械臂安装视角通过；安装位变化后只检查外参及覆盖变化。若需要 RGB-D、多相机、实物设备或升级现有 Isaac 环境，先向用户说明证据和代价再决定。普通阈值与采样参数按本节冻结，不反复询问。

## 23. 开发项目 E：感知精度对任务成功率的仿真研究

状态：本轮实施 **E0 ORACLE 零扰动基线**，前置为 A1–A3 与 R1–R3；E1/E2 后续再接 P1–P3、A4 的实际感知。用户已确认实际感知与受控误差两组最终都做，但本轮不运行精度扫描。E 不依赖 A5 或 Laya；E 的精细任务结果直接用于完成 A5。首轮固定底盘、rule 选择器、MoveIt/OMPL/控制器版本、IK 插件、规划预算、碰撞分辨率、速度、重试规则和保护参数，不加载 Laya。全程保留 effort 限制、watchdog 与取消能力。

### 23.1 精度定义与两个实验问题

“视觉精度”在此指供控制使用的几何估计误差，不直接以像素数、检测置信度或类别准确率代替。

| 量 | 度量与用途 |
|---|---|
| 目标位置 | 机器人基座系中估计与真值的距离，另列平面/高度分量，单位 mm |
| 受约束朝向 | 只比较任务要求的旋转自由度；圆柱自由 yaw 不计，方块等价抓取方向按对称性处理 |
| 放置区与障碍几何 | 分别计算区域中心/朝向、夹具位姿或表面距离误差，不与目标误差合并 |
| 观测有效性 | 无目标、遮挡、过期、未知区域、图像/点云配对失败率 |
| 执行偏差 | 关节/TCP 跟踪、最终落点与真实最小间隙，独立于感知误差记录 |

**SENSOR 组回答“实际仿真配置能做到什么”。** 同一布局和初态，配对运行 RGB＋2D 与 RGB＋3D，以真实图像/扫描估计参与执行。按测得的误差记录成功率，并保留布局、遮挡、覆盖和延迟。误差与场景难度可能同时变化，这组关系不能单独证明误差的因果作用。

**ORACLE_PERTURBED 组回答“控制链能容忍多少输入误差”。** 场景几何保持不变，只在输入适配边界改变指定几何估计。固定其他条件，每次重新规划并在 Isaac 执行，而非只做离线 IK。这是合成几何敏感性实验，不代表某款低精度相机的表现。

实际感知组先比较两种雷达配置，不扩大为分辨率、光照、雷达线数、模型和控制器的全组合。若两配置没有形成不同误差分布，如实报告“未覆盖足够精度差异”；不得按设备名称强行命名为高/低精度。后续若需调整单个采集参数，再单列配对实验。

### 23.2 三类任务与固定成功判据

| 任务 | 受控误差施加位置 | 物理成功判据 |
|---|---|---|
| G 抓取与抬升 | 目标物估计位置；其余几何固定 | 夹爪接触抓起目标，抬升至少 8 cm 并保持 1 秒，无掉落或非预期碰撞 |
| PLACE 精确放置 | 放置区域的估计坐标系；诊断组先用零扰动完成真实抓取 | 同台放置、开爪撤离后稳定 1 秒；真实中心误差按 20/10/5 mm 三个阈值分别统计，完整包络仍在指定区域 |
| C 窄间隙携物搬运 | 一组夹具/障碍的估计位姿；目标终点固定 | 持物通过预先固定的通道并到达终点，无非预期接触、掉落和限制超出；记录整臂与携物真实最小间隙 |

PLACE 沿用第 10 节的 5 mm IK 目标，物理放置另按 20/10/5 mm 分层评分。三个阈值复用同一次执行，不分别重跑，也不在看到结果后修改任务目标。朝向仅在任务要求时计分，初值 5°；对称等价朝向不算失败。若机器人基线不能达到某档位置精度，该档记录为基线不足，不能据此声称是视觉问题。

C 使用一套固定通道，宽度按夹爪与持物尺寸设置，并满足既有误差预算和停止空间；不扫描全部通道宽度。PLACE/C 的预备抓取仍通过物理接触完成，不用隐藏吸附、关节固定或 teleport。零扰动预备抓取只用于 ORACLE 与 ORACLE_PERTURBED；SENSOR 从抓取到结束全部使用传感器估计，不能中途用真值补齐。预备步骤失败记为前置阶段失败，不从整个任务的分母删除；另报到达研究阶段后的条件成功率。

合成组的 G/PLACE/C 各自只改变表中一类估计。PLACE 的任务参数指向同一个物理放置区，扰动的是感知到的区域坐标，而不是用户授权的目标。C 改变估计夹具，不能同时移动真实夹具。扰动不修改机器人 q/dq、物理模型或执行反馈。导航精度研究留到 M，用已有导航工况记录数据，本阶段不要求先开发 Nav2。

### 23.3 轻量试验矩阵与误差施加规则

从原计划 10 个冻结可行种子中，在查看精度评估结果前预选 5 个布局作为首轮研究集。保留原有 3 个开发场景做配置调整；研究集不用于调阈值。各任务、各输入配置采用相同的 5 个布局与明确初态，运行顺序交错，避免所有高负载配置集中在同一时段。

| 轮次 | 最小矩阵 | 用途 |
|---|---|---|
| E0 零扰动基线 | 3 类任务 × 5 个布局；复用符合相同配置的 A/R 证据 | 分离规划、控制与接触能力；也是 E2 的 0 mm 档，不重复执行 |
| E1 实际感知 | 2 配置 × 3 类任务 × 5 个布局，最多 30 次任务 | 比较实际观测精度、覆盖、延迟与成功率 |
| E2 位置敏感性 | 0/5/10/20 mm × 3 类任务 × 5 个布局，合计 60 次，含 E0 | 先观察误差增加时的任务退化；E0/E1/E2 合计最多 90 次基础任务 |
| E3 朝向补充 | 仅对有朝向约束且零误差可执行的任务，加 2°、5° 两档 | 与位置实验分开，三类任务最多新增 30 次；若朝向自由则标不适用 |

上述次数是初筛预算，不是要求无条件跑满。某类任务的零误差基线未就绪时，记录原因，先不扫其非零档。不能反复替换失败种子直到全部通过。各档只有 5 个场景，成功率粒度为 20%；报告 n/N 与样本局限，不把曲线当成可靠性证明。

位置扰动首轮限定为桌面平面中的固定偏差。每个布局由独立 `perturbation_seed` 生成单位方向，同一布局各幅值复用该方向，记录 dx/dy/dz；不在每帧重新随机。朝向补充只绕任务指定轴旋转，位置扰动设为零，四元数保持有效。场景复位、扰动种子与 OMPL 随机设置分别记录。锁定接口可设置 seed 时保持配对一致；若不能控制全部规划随机源，明确记录该限制，不宣称逐次规划完全相同，也不择优重跑。

这组固定偏差模拟几何估计偏移，不能等同于每帧噪声、外参漂移、丢帧或时延。高度误差、真实外参偏差和时间扰动只在现有结果指向对应问题时单独扩展，不同时注入。若相邻档位出现退化转折，只允许事先声明的一次中间档补测；该结果标为探索性，不并入原冻结门槛。

每次输入明确记录 `input_source` 和被扰动字段，评估器另记真值与实际误差。Guard 的策略、配置和不确定性处理规则在研究前固定；不得借助该次真值减小误差余量、改选候选或兜底目标位置。预先声明的误差预算可以作为诊断元数据，但逐次真实误差只能写入评估侧。若保护拒绝执行，如实记为拒绝，不降低门槛以追求成功率。

### 23.4 执行、失败与统计口径

1. 根据场景种子恢复同一物理初态，并通过状态反馈确认准备完成。此时接受任务、分配 trial_id 并计入启动数，发生在采集首个新观测之前。
2. SENSOR 获取新观测；诊断模式由输入生成器构造估计。记录来源、时间、坐标和配置版本。
3. 固定 Grounder、规则选择器和 Guard，生成并执行计划；使用相同的超时及最多一次重观察规则。
4. 独立评估器读取物理结果，关联 observation_id、candidate_id、command_id 和 trial_id。
5. 每次任务保留一个终态及阶段事件，不用成功重试覆盖第一次失败。首次成功率与有限重试后的成功率分别给出。

主要成功率为物理成功次数 / 按上述时点启动的任务数。首次观测失败、感知拒绝、规划失败、超时、碰撞、掉落与运行中数据失联都在分母中；另列每种失败原因。启动后发生人工中断或基础设施故障时，记未成功并披露原因。复位失败等未启动项目另列，不冒充已完成样本。若重试，保留原 attempt，不能用重试成绩替换原始分母。

分别给出整个任务成功率和到达研究阶段后的条件成功率。不能只统计“已下发轨迹中的成功率”。SENSOR 的误差仅在可定位样本上计算，但必须同时报告不可定位的任务数。连续帧不增加任务样本量。

2D 扫描盲区、完全遮挡、缺 TF 和过期数据属于单独的能力边界/故障表。正确拒绝可计为保护检查通过，不能算作抓放成功，也不与可行任务成功率混合。effort 限制和停止仍按 R 的标准验收；感知精度变差不是忽略保护失效的理由。

每次记录末端/关节跟踪偏差、抓取接触、真实落点和间隙，区分感知、规划、跟踪和接触失败。E1 的延迟与精度一起报告；不得把图像变清晰但处理变慢造成的失败全部归为几何误差。计算耗时、数据年龄与仿真时间按第 17 节分开。

### 23.5 交付、完成条件与人工决策

逐任务记录至少包含 `trial_id / scene_seed / task_kind / sensor_profile_id / input_source / calibration_id / config_hash / perturbation_seed / perturbation_field / perturbation_value / measured_error / observation_age / stage / outcome / failure_reason / wall_latency / real_time_factor`。未产生观测或未执行的字段用 null 和原因表示，不填零。

输出两组独立图表：SENSOR 配置与实测误差/成功率对照；ORACLE_PERTURBED 的已知扰动与任务成功率曲线。位置和朝向分开画，标记样本数；实际感知误差分桶只作描述。另给一张任务需求表，列出在已测场景中可接受的精度范围、超出时的失败或拒绝方式，以及未覆盖的条件。不将有限样本最大值写成保证边界。

实验完成条件是两组证据可追溯、配对条件明确、任务结果完整且结论不混用。无需证明 3D 优于 2D，也无需证明 Laya 或神经网络有优势。如果零误差也失败，先处理规划/控制；如果只在 SENSOR 失败，依据误差、覆盖与延迟定位感知缺口。后续 Laya 比较固定本轮选定的感知条件，不同时改视觉与选择器。

人工介入：查看 P 的首次 GUI 视角，以及 E 的任务容差、窄通道配置和结果对照。两组实验路线已经确认，不重复询问。出现需要扩大物体种类、增加传感器类型、改变执行保护、换机器人或增加付费资源的实际问题时，先询问再决定。本轮实施中的人工确认项以文首与第一阶段报告为准。研究数值冻结前按实际模型检查，不把计划初值当作已验收结果。

以上 P/E/A/L/R/T/M 按对应章节派工。本轮第一阶段范围以文首为准，安装元数据不等于运行验收，计划阈值不等于实测成绩。保留文件路径和 23 章结构；不重建 B08/B09，不恢复 CartPole，不 commit、不 push。
