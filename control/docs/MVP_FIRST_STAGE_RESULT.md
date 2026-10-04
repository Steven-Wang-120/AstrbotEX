# 移动机械臂 MVP 第一阶段实施记录

更新：2026-10-04。**实施中，尚未完成全部 P/L/A/R/E/T/M 第一阶段验收。** 本轮不 commit、不 push。全部机器人工作限于 Isaac 仿真。

## 1. 已有证据与未完成项

| 项目 | 当前事实 | 尚不能声称的能力 |
|---|---|---|
| P1 | RGB＋原生 2D/3D 雷达分别完成至少 30 仿真秒真实 ROS 采集；首次视角已获用户确认 | 视觉定位、精度研究、传感器实时性达标 |
| L1 | 技能目录、任务输入、澄清与正式 Goal 参数绑定已实现；20 条真实模型开发探针完成 | 原 typed-decisions 仅 2/20 正确，不能用于自主运动 |
| T1 | v2 已冻结 100 组 SYNTHETIC 数据，任务／场景划分 70/15/15；排除实际模型输入和开发探针重复 | 尚未训练，也不是物理任务成功率 |
| A1 | xArm6 USD/URDF、夹爪、限位及 MoveIt 已接入；初始 FK/TCP 位置误差约 1.7 µm | 尚未完成 3 个实际姿态和夹爪开闭 |
| R1 及必要保护依赖 | 三个标准控制器已激活；ORACLE GUI 下真实反馈时序、初始 FK 与网关就绪已核对 | 尚未完成真实运动、取消和 watchdog 验收 |
| A2 | 可达、不可达与碰撞拒绝三项真实 MoveIt 探针通过；全程零 Action | 简单绕障证据尚缺，A2 整阶段未完成 |
| A3、E0 | ORACLE 抓放与独立物理评估入口已实现 | 尚无物理抓放或 15 次 E0 结果 |
| M1 | 静止物理状态、RGB／雷达／TF 与 Nav2 实际启动已核对；雷达障碍已进入成本地图 | 完整负载仍有反馈超时；三个布局的实际绕障尚未运行 |

详细结果：[P1 传感器](MVP_SENSOR_FIRST_STAGE_RESULT.md)、[L1/T1 语言与数据](MVP_LANGUAGE_FIRST_STAGE_RESULT.md)。唯一阶段规格仍是[主计划](MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md)。

## 2. 复用环境与新增入口

保留 Isaac Sim 5.1、Isaac Lab 2.3.2、系统 ROS 2 Jazzy、MoveIt 2.12.4 和既有 Laya 环境。首次慢启动等待同一进程，未重装 PyTorch。

Nav2、topic hardware 等缺少的依赖解包到 `runtime/mobile_manipulation_deps/root`，未升级系统环境；包版本和校验值见 [dependencies.lock.json](../config/mobile_manipulation/dependencies.lock.json)。机器人固定厂商提交 `3dc2b5e8294758d96b54b15fa5920d581b7cbb3d`。生成配置位于忽略目录 `runtime/mobile_manipulation_deps/model`，由 [prepare_mobile_robot.py](../scripts/prepare_mobile_robot.py) 生成。

主要入口：

- [Isaac 启动脚本](../scripts/start_mobile_manipulation_sim.sh)：用 `bash scripts/start_mobile_manipulation_sim.sh ...` 调用。
- [系统 ROS 环境](../scripts/mobile_manipulation_env.sh)：在独立 shell 中 `source`，不混入 Isaac Python。
- [统一验收运行器](../scripts/run_mobile_manipulation_trials.py)：observe、arm_checks、fetch、e0、navigation 共用一个入口。
- [ROS 接口说明](../ros2_ws/src/astrex_mobile_manipulation/INTERFACE.md)：关联 ID、Action、租期、反馈与停止边界。

CartPole、MoveCart、SpinPole 仍保持归档；未恢复旧场景或旧服务。

## 3. 已定位的物理反馈问题

原配置下，关节位置静止，但 `joint3` 原始速度约为 −0.0384 rad/s，超过既定 0.02 rad/s 停止阈值。关闭 articulation sleep 没有解决；增加 velocity solver iterations 到 8 也没有解决。这两次诊断保留，不能当成成功修复。

采用本机 5.1 支持的 `physxScene:enableExternalForcesEveryIteration=true`，恢复 solver 32/1 后，静止阶段原始速度最大约 **0.000199 rad/s**，夹爪约 **3.03×10⁻⁷ rad/s**。前后物理步差最大约 1.73×10⁻⁷ rad/s。未替换原始速度，也未放宽停止阈值。该现象与 [PhysX 的 TGS 稳态速度说明](https://nvidia-omniverse.github.io/PhysX/physx/5.8.0/docs/Simulation.html#tgs-steady-state-velocity-and-position-discrepancy)一致。

[静止对照证据](evidence/source_runs/phase1_20261004T160950+0800/arm_dev/tgs_force_iterations/feedback_diagnostic_summary.json)证明此配置下的静止反馈已恢复。它不替代运行中停止或跟踪验收。

位置驱动 effort 上限为 `[50,50,32,32,32,20] Nm`，夹爪 2 Nm。夹爪实际 DOF 速度上限 0.3 rad/s，已从 PhysX 读回。`JointState.effort` 留空：当前没有测得驱动实际输出，配置上限不能冒充实际力矩曲线。

## 4. 当前联调失败

首轮 A2 检查没有提交任何 Action 或 guarded 运动目标。两项请求被 `ROBOT_NOT_STATIONARY` 拒绝，碰撞规划项触发 `RAW_STATE_STALE`；迟到规划结果被丢弃。不能将这些基础设施失败写成“规划器不可达／碰撞拒绝验收通过”。诊断碰撞物已从 MoveIt 场景清除并读回。

15 墙钟秒采样中，原始状态的接收间隙 p95 为 214 ms、最大 285 ms，17 次超过 200 ms。`/clock` 和最终入口状态也在相同时段中断，源时间每次仅推进一个渲染步。证据更支持 Isaac 主循环长帧，不能简单归因于 DDS 丢包。

这些首轮失败原样保留，保护阈值不变。证据：[首轮 A2](evidence/source_runs/phase1_20261004T160950+0800/arm_dev/tgs_ros/a2_plan_only/arm_checks_result.json)、时序采样（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/arm_dev/tgs_ros/raw_timing_15s.json`）、[初始就绪与 FK](evidence/source_runs/phase1_20261004T160950+0800/arm_dev/tgs_ros/readiness_and_initial_fk.json)。

### 4.1 已通过的 ORACLE 配置与三项规划探针

角点计算向量化、关闭雷达调试绘点后，长帧仍存在；单独将 GUI 纹理降至 960×600 后，15 秒窗口仍有一次 raw 间隔约 223 ms。因此没有把这两步写成执行时序通过，也没有重复采样筛选结果。

A/R/E0 本来使用 ORACLE 几何，不依赖实时 RGB 或雷达。最终机械臂开发配置显式设置 `sensor_rendering=false`，保留 GUI、物理接触、实际关节反馈、clock 与控制链。有效配置记录 `ORACLE_CONTROL_ONLY`、无活动 sensor_profile，且不发布传感器 TF。P1 原始采集保持不变，M1 仍使用完整的随车传感器配置。

该 ORACLE GUI 实例的固定 15 秒窗口中，raw p95 **22.82 ms**、最大 **54.98 ms**；最终执行入口状态最大 **96.11 ms**，两者均无超过 200 ms 的间隔。新节点首次 FK 检查因 DDS 服务发现尚未完成而失败；加入有界服务等待后通过，原异常保留，没有重采时序窗口。

A2 的三项探针有因复验通过：可达请求的规划耗时约 **20.88 ms**，记录的交接耗时约 **30.16 ms**；不可达请求和碰撞请求被 MoveIt 拒绝。三项 `ros_goal_uuid=null`、`guarded_count=0`，最大实测关节变化不超过 5.83×10⁻¹¹ rad。诊断碰撞物通过同一 adapter 清除并读回确认。

证据：ORACLE 时序（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/arm_dev/oracle_gui_ros/raw_timing_15s.json`）、[A2 结果](evidence/source_runs/phase1_20261004T160950+0800/arm_dev/oracle_gui_ros/a2_plan_only/arm_checks_result.json)。这些结果不证明 A1 运动或 R 取消通过，也没有覆盖 A2 的有效绕障。绕障证据优先复用 A3/E0 中实际受障碍约束的有效轨迹；如果只有无遮挡直达，再在同一会话补一次 plan_only 绕障，不重跑已有三项。

新增证据检查锁定 session、递增 seq、profile 与 trial；放置成功要求当前连续稳定窗口。EX 成功前先落盘不可变物理快照；结束失败不计 verified success。E0 汇总必须包含同五种子配对的 15 项且每类至少一次研究成功。13 项纯测试通过，实际 E0 尚未运行。

### 4.2 M1 静止模型与随车传感器

初次移动预览因 JSON 中的雷达位置列表不符合原生接口的 `Gf.Vec3d` 类型而失败。修正转换后，第二次预览在接触配置更新时使 PhysX tensor view 失效；其物理采样文件为空。两次启动记录均保留，不计 M1 通过。

接触 API 和刚体包装器已移到 `world.reset()` 前。评估 hook 只读回配置并订阅接触，不再修改运行中的物理 USD。第三次预览持续产生真实状态，无 tensor view 失效。主循环另补回调异常传播：Kit 吞掉回调异常时，入口也会保存失败并关闭，不继续冒充可执行实例。

唯一 15 墙钟秒静态采样的结果如下。没有启动网关、Nav2 或提交运动 Goal。

| 检查 | 实测与结论 |
|---|---|
| 停止反馈 | 四轮均有实际反馈；根刚体速度来自 PhysX；INITIAL_HOLD、profile 与连续停稳 500 ms 均符合 |
| 最大速度 | 根刚体线速度 0.00180 m/s、角速度 0.00572 rad/s；车轮表面速度 0.00205 m/s |
| 整机水平包络 | 本窗口最大半径 0.47529 m，处于 Nav2 0.48 m 圆内；仅代表当前收拢姿态 |
| TF | odom→base_link；base_link→mm_lidar `[.18,0,.20]`；相机光学帧外参已发布 |
| 随车 RGB | 实际 640×480 图像和 CameraInfo 已接收；初始画面为地面与障碍，不等于抓取目标覆盖已通过 |
| raw／最终入口状态 | 最大墙钟间隔 210.84／240.85 ms；分别 1／2 次超过既定 200 ms，时序未通过 |
| 雷达 | 35 帧／15 墙钟秒；最大墙钟间隔 968 ms，最大源时间间隔 300 ms；不宣称实时 10 Hz |

配置和原始证据：[首次类型失败](evidence/source_runs/phase1_20261004T160950+0800/m1_preview/startup_failure.json)、物理视图失败日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_typed_sensor_stdout.log`）、[修正后的初始化读回](evidence/source_runs/phase1_20261004T160950+0800/m1_preview_prepared_physics/contact_reporting.json)、[15 秒只读结果](evidence/source_runs/phase1_20261004T160950+0800/m1_preview_prepared_physics_ros/m1_static_ros/mobile_static_summary.json)、随车图像与原始帧（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_preview_prepared_physics_ros/sensor_mount`）。

这次采样证明移动模型和传感器链已经运行，未证明导航通过。M1 仍需处理完整传感器负载的状态时序及雷达间断，再进行三个布局的实际绕障。200 ms 保护未放宽。三个预览实例均已退出；没有遗留运动任务。

### 4.3 发布调度修复与 Nav2 只读启动

对同一份旧采样离线分析，发现部分状态间隔由发布节流造成：final_status 有一次 202.45 ms 空窗，期间 raw 已产生新状态；truth 最长 365.92 ms 空窗期间也有新的 PhysX 样本。当前 raw 与 final_status 共用新样本发布条件。truth 在 50 ms 仿真时间或 50 ms 墙钟时间任一到期时重新采样，且要求仿真时间严格前进。没有缓存心跳，也没有伪造新时间戳。相关时间逻辑和 Guard 共 35 项纯测试通过。

M1 另外只关闭本机 Isaac Lab performance 预设中的半透明、反射、间接光照和环境遮蔽。设置从 true 到 false 的实际读回已保存。阴影、亮度、AA、RGB 分辨率及传感器频率未改；RGB 外观仍可能变化，因此不能把不同渲染配置的定位精度混为一组。

上述两项改动后的唯一 15 秒静态窗口中，raw／final_status 均为 368 帧，最大墙钟间隔 **242.93／242.53 ms**，各两次超过 200 ms。truth p95 **153.78 ms**、最大 **282.28 ms**；雷达收到 61 帧，最大墙钟间隔 **381.19 ms**。发布节流问题已修复，真实长帧仍存在，不能用平均改善宣称时序通过。

同一批雷达消息离线分析为 61 帧／6.000000313 仿真秒，约 **10 Hz**，最大源时间间隔 **100.000006 ms**，无超过 200 ms 的源间断；墙钟频率约 4.11 Hz。这次开发窗口的雷达源时间连续性已改善，剩余墙钟时序缺口仍需解决。[分析记录](evidence/source_runs/phase1_20261004T160950+0800/m1_effects_and_sampling_ros/scan_existing_samples_analysis.json)、[35 项测试记录](evidence/source_runs/phase1_20261004T160950+0800/m1_effects_and_sampling_ros/sampling_regression.json)。测试记录明确注明转录自原工具输出，未冒充独立捕获的 stdout。

[联合实测](evidence/source_runs/phase1_20261004T160950+0800/m1_effects_and_sampling_ros/m1_static_ros/mobile_static_summary.json)、[渲染读回](evidence/source_runs/phase1_20261004T160950+0800/m1_effects_and_sampling/render_settings.json)。原始失败窗口全部保留，未反复采样挑选通过结果。

随后首次启动 Nav2，只查询生命周期、接口和成本地图。五个主节点及 global/local costmap 均 active，NavigateToPose Action 存在；全局成本地图在原静态自由区新增 75 个 lethal 障碍格，局部地图有 60 个 lethal 格，雷达输入为 `/astrex/mm/scan`。没有启动执行网关，没有 Goal、命令或执行租期。

启动日志保留 Smac 关于 inflation 与优化碰撞检查的 ERROR 提示，以及 BT 默认 error_codes 的 WARN。生命周期 active 不能证明路径规划或实际导航通过；未为消除提示而扩大膨胀半径。[Nav2 只读结果](evidence/source_runs/phase1_20261004T160950+0800/m1_effects_and_sampling_ros/nav_readonly/navigation_readonly_result.json)、首次启动日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/m1_effects_and_sampling_ros/navigation_first_launch.log`）。Nav2 与 Isaac 已正常退出。

**待用户选择：**保持 200 ms 墙钟反馈门限并继续优化渲染，或为纯仿真采用 500 ms 反馈门限。后者尚未实施，不能理解为默认批准；原 effort 上限、500 ms 执行租期、取消与停止证明也不随该提议取消。该选择按主计划“改变执行保护先确认”的约定提出。机械臂工作区与通道确认仍独立待回复。

### 4.4 未通过的 RGB 渲染节流已撤回

尝试每四个 GUI 渲染步才更新一次 RGB render product，同时保持原生时间戳与雷达配置。该方案没有通过：真实采集只收到 clock，RGB、CameraInfo、雷达均无消息；SDK 的相机新帧计数也为零。实例后来以 139 退出，日志记录原生段错误。根因尚未证实，不能归因于磁盘 I/O，也不能将缺少传感器负载时的较短状态间隔写成性能改善。

失败源码、配置 hash、采集结果和日志均保留，活跃代码已撤回该节流逻辑，恢复连续原生渲染与原有 ROS 抽帧。[撤回记录](evidence/source_runs/phase1_20261004T160950+0800/m1_camera_cadence_finite_status/withdrawal_result.json)。之前的 P1 正式结果不变，M1 的 200 ms 墙钟时序问题仍未解决。

此次另发现新诊断字段将初始 lease 的内部 `-inf` 写入 JSON，导致首次回调失败。输出现以 `null` 表示尚无租期，内部 Guard 不变；初始状态序列化回归通过。撤回渲染逻辑时遗漏 `time` 导入的启动失败也已修正，原始记录保留。

修正后的独立恢复窗口收到 RGB／CameraInfo 各 46 帧，覆盖 3.000000157 仿真秒，约 15 Hz；雷达 29 帧，约 9.33 仿真 Hz。时间戳均递增，TF 参考帧为 base_link，实际发布 PID 与实例记录一致。该窗口仅为 PREVIEW_PASS，不是重新验收 P1，也没有重新筛选 200 ms 时序成绩。[恢复采集](evidence/source_runs/phase1_20261004T160950+0800/m1_render_restore_import_ros/sensors/capture_result.json)。恢复实例已通过 stop_requested 正常退出。

### 4.5 R07/R08 故障入口已准备，物理验收未运行

统一运行器新增显式 `R_FAULTS` 区段，默认关闭。R07 只暂停 raw 关节状态发布，保留真实物理采样和最终状态；R08 只对运行器自建且已绑定命令的网关子进程注入退出。最终入口的 effort、过期目标、租期和停止要求均不放宽。COMMAND_STALE 如果先于租期触发，记录实际原因，不关闭该保护以制造 LEASE_EXPIRED。

26 项 Guard／故障入口纯检查通过，包括新增的初始 JSON、身份隔离、过期/重复请求与受控子进程检查。另有传感器、导航证据、E0 计数与发布调度共 38 项检查通过，统一运行器在系统 ROS 环境可正常导入并显示帮助。实际测试日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/r_fault_support_unit.log`）。尚未发送运动或执行故障注入，不能写成真实停止通过。接口与操作边界见 [ROS 接口说明](../ros2_ws/src/astrex_mobile_manipulation/INTERFACE.md)。

## 5. 数据修正与研究边界

旧 T1 数据虽然场景 hash 不同，但部分实际模型输入跨集合重复，fetch 标签还固定指向 `obj-a`。旧目录 `t1_corpus_final` 原样保留并标记失效，不能用于独立成绩。

新版 `t1_corpus_v2` 在已确认词汇和标签规则内，包含 100 组、120 个不同语义任务、230 个不同模型输入；跨集合和原开发探针语义重复均为 0。fetch 目标角色分布为 obj-a 22、obj-b 18。52 项相关测试及官方 Dataset 解析通过；未导入 Torch 训练、未微调。

模型选型的 multilingual 对照仍等待用户选择；没有下载或更换模型。原 2/20 成绩只对应保存的旧请求。新候选文字补充对象 ID 后，不能把新格式成绩直接与旧请求混比。

## 6. 后续顺序与人工确认

1. ORACLE GUI 时序与 A2 三项规划探针已通过；A2 简单绕障仍待补。M1 已测得完整传感器负载下的时序缺口，保留原始窗口并继续定位，不能继承 ORACLE 成绩。
2. 用户确认机器人 GUI 工作区与窄通道配置后，运行 A1 姿态、夹爪及 R 的实际停止测试。
3. 通过 A3 开发抓放后冻结 E0 五个布局，保留全部 3 类任务 × 5 布局结果，不筛选成功样本。
4. M1 使用 7301、7302、7303 三个独立障碍布局，各一条实际绕障路线。每布局使用独立仿真会话；不将同一布局往返三次当成三个独立场景。
5. 汇总工作区／暂存区保护、实际验收与未通过项，再结项。当前不能宣布全部第一阶段完成。

待人工确认：机械臂／夹爪首次 GUI 工作区和窄通道几何；M1 仿真反馈门限的选择；实际停止演示观看。multilingual 对照是另一个待选项，不通过默认下载代替回复。

全部原始证据根目录：

- 传感器／机器人与工作区快照（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800`）
- 语言／模型／数据（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_l_t_20261004T162658`）

本轮首次 colcon 在根目录生成的 `log/` 已移至构建证据目录（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/mobile_manipulation/phase1_20261004T160950+0800/initial_colcon_log`），10 个文件／符号链接逐项校验；未清理其他任务的日志。

工作区核对：[保护结果](evidence/source_runs/phase1_20261004T160950+0800/worktree_protection_latest.json)。HEAD、暂存区、remote、submodule 不变；开始前已有的无关修改保持原值。未 commit、未 push。
