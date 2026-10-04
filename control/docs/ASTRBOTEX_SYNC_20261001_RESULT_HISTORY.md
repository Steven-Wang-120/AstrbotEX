# AstrBotEX 固定版本同步与本机验证报告

日期：2026-10-01。范围：上游同步、已有测试、功能说明及开发任务书。**没有开发新功能，没有启动机器人运动，没有微调，没有 commit/push。**

## 1. 结论

| 项目 | 结果 |
|---|---|
| 固定版本同步 | 完成：`1a9c190` → `c9624a4`，116 新增、19 修改、0 删除 |
| 三方比较 | 本地只有三份 Markdown 格式差异；全部保留，无冲突 |
| 同步后文件核对 | 216 个上游文件逐一比对；除三份保留格式差异外与目标一致 |
| 工作区保护 | 原索引字节、cached diff、HEAD、remote 和 submodule 提交保持不变；另检测到其他任务的范围外变化，已保留 |
| 完整框架回归 | 476 项：470 通过、1 失败、5 环境错误、0 跳过；退出码 1 |
| ROS 环境问题处理 | 仅在重试进程加入已有系统依赖路径，无安装/升级 |
| 原生 ROS 定向重跑 | 5/5 通过、0 跳过；退出码 0 |
| B08/B09、机械臂、Laya、微调 | 只更新详细计划，尚未实现或运行 |

**同步已完成，完整框架回归未全通过。** ROS 重试消除了 5 项导入错误，仍保留 1 项停止状态断言失败。不能把两次运行合并写成单次 476 PASS。

## 2. 来源、同步方法与保护证据

- 固定上游：[c9624a439874740cde4acc347c979419af120d05](https://github.com/Steven-Wang-120/AstrbotEX/commit/c9624a439874740cde4acc347c979419af120d05)。
- 比较基线：`1a9c190cfb1b7d6ebcd747cec87f44ab5c71bcb1`。
- 临时目录：`/tmp/astrex_sync_20261001/`，含只读克隆、base/target、本地备份和三方合并结果。
- 原目录：`apps/AstrBotEX/`，仍为主仓库源码目录；没有转换 submodule 或修改 remote。
- A.E.B. 保持 `7f9790d823dc42af91cbc928cdb98c48f3eef737`；AstrBot 保持 `ab42c0d9b726d82ad0f9563e04c53a4460c00d61`，两者工作区干净。
- 同步前索引 SHA-256：`36b4c03b44deb9417dfa53ccad06463fc1430eef42f6741f59696957aabf93fe`；最终核对相同。

本地 `README.md`、`README_AstrBotEX插件系统规范.md`、`TECHNICAL.md` 共四处去除行尾空格的差异保留。上游源码和测试没有本地修补。按每个文件的 hash 检查，不依赖版本字符串推断同步完成。

原来已暂存的 AstrBotEX/A.E.B./ROS 变更和未暂存控制器/历史文档改动保留。新同步留在工作区，新增文件保持未暂存。原有历史文件没有重新创建或清理。

### 2.1 工作期间的范围外变化

最终核对发现以下文件在本轮期间发生变化；它们不在本轮任何写入命令的目标范围内，没有回滚或覆盖：

- `ros2_ws/src/astrex_interfaces/CMakeLists.txt`
- `ros2_ws/src/astrex_ros_bridge/astrex_ros_bridge/balance_hold_controller.py`
- `ros2_ws/src/astrex_ros_bridge/package.xml`
- `ros2_ws/src/astrex_ros_bridge/setup.py`
- 新增 `ros2_ws/src/astrex_interfaces/srv/MoveCartTo.srv`

其中新增服务和 setup.py 的 mtime 为本地时间 15:08:38，晚于本轮测试结束。用户已确认另一项 ROS 开发任务正在进行。收尾时还观察到新增 `scripts/lib/cartpole_service_launcher.py` 和 `scripts/start_cartpole_service.sh`，同样未写入或覆盖。全部范围外变化以最终 `concurrent_changes.json` / `protection_check.json` 的时点记录为准，不归入本轮成果。因此不能声称所有范围外文件与开始时完全相同。

初次 git clone 因沙箱代理不可达失败，获准网络访问后完成临时克隆。实际目录名为 `AstrEX_project_main`，与沙箱配置的 `AstrEx_project_main` 大小写不同；获准后才写入用户指定目录。没有收到自动审批拒绝。

## 3. 本机环境与命令

| 项目 | 实际值 |
|---|---|
| 系统 | Linux x86_64，kernel 6.17.0-1032-oem |
| 测试 Python | 现有 `apps/AstrBotEX/.venv/bin/python`，3.12.3 |
| 已有依赖 | pyzmq 27.2.0、websockets 17.1 |
| ROS | Jazzy，`/opt/ros/jazzy/setup.bash` |
| DDS 隔离 | Domain 173，`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`，随机 `/ex_test_*` 话题 |
| 测试数据 | 上游测试自己的 TemporaryDirectory；ROS 构建/日志在 /tmp |

交互 shell 默认 Python 为 Conda 3.14 且无 pytest；未使用它运行框架测试。使用现有 EX Python 3.12 与标准库 unittest，无需安装测试依赖。

先构建上游已有 `ros_interfaces/astrbotex_demo_interfaces`，供既有自定义消息测试使用。只生成 /tmp 构建产物，不开发或改写消息定义：

```bash
source /opt/ros/jazzy/setup.bash
/usr/bin/colcon --log-base /tmp/astrex_sync_20261001/ros_logs build \
  --base-paths /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX/ros_interfaces \
  --build-base /tmp/astrex_sync_20261001/ros_build \
  --install-base /tmp/astrex_sync_20261001/ros_install \
  --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3 \
  -DPYTHON_EXECUTABLE=/usr/bin/python3 -DBUILD_TESTING=OFF
```

构建结果：1 package finished，退出码 0，约 67 秒。

以下测试在 `apps/AstrBotEX/` 中运行；日志文件保存 stdout/stderr 原文：

```bash
source /opt/ros/jazzy/setup.bash
source /tmp/astrex_sync_20261001/ros_install/setup.bash
export ASTRBOTEX_TEST_ROS_DOMAIN_ID=173
export ROS_DOMAIN_ID=173
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_LOG_DIR=/tmp/astrex_sync_20261001/ros_runtime_logs
export PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -m unittest discover -s tests -v

# 仅处理 ROS 导入环境，并只重跑受影响的 5 项。
export PYTHONPATH="/usr/lib/python3/dist-packages:${PYTHONPATH}"
.venv/bin/python -m unittest discover -s tests -p test_ros2_integration.py -v
```

首轮耗时 117.708 秒；ROS 定向重试 4.127 秒。重试通过导入检查确认使用已有系统 yaml/numpy；未修改 venv、ROS 安装或 Isaac 环境。Jev 后端测试使用 fake transport/loopback HTTP，没有付费调用。

## 4. 失败、重试与未覆盖事项

### 4.1 已解决的 ROS 测试环境错误

首轮能发现 rclpy，但 EX venv 没有 `/usr/lib/python3/dist-packages`，导入 rclpy.parameter 时缺少 yaml。5 项均在 setUp 失败，不能归因为 DDS 或控制器失败。

仅调整重试进程 PYTHONPATH 后，以下既有测试全部通过：

1. 自定义嵌套 Target 消息往返与缺失类型隔离。
2. 外部进程晚启动、双 owner、双向原生通信。
3. 高频输入和慢消费时队列有界。
4. QoS 不兼容诊断与重配恢复。
5. 20 次 normal/ros2 切换后的队列和线程清理。

压力测试原始统计：发送 1517 条、接收 762 条、丢弃 712 条，峰值队列 8660 bytes；消费样本队列年龄 p95 5.514 ms、最大 6.743 ms；负载下环境切换 0.006 s。队列丢弃是 keep_latest 策略的预期观测。

这些数字仅对应短时测试负载。环境切换耗时不是机器人停稳时间，队列年龄不是 Laya 或端到端规划延迟。

### 4.2 保留的停止状态断言失败

测试：`test_decision_service.DecisionServiceTests.test_proof_retries_keep_same_gate_epoch_and_do_not_clear_blocked_goal`。

位置：`tests/test_decision_service.py:489`。期望 `service.status()["stop_error"] == ""`，实际仍为 `stop proof pending: <command_id>`。

静态定位发现：测试等待 `len(epochs) >= 2 and not service._stop_pending`；`_process_control()` 在开始证明尝试前先清除 `_stop_pending`，之后等待证明返回，再更新 `_stop_error`。因此该等待条件可能早于状态更新完成。**这是基于源码的可能解释，不是已证实根因。**

没有重跑该项来筛选通过结果，没有修改源码、sleep 或断言。后续需要区分测试等待条件竞态与生产状态可观察性问题。上游提交也说明存在 B04 时序相关失败，但不能把这次失败直接等同于上游已定位问题。

### 4.3 未验证

- A.E.B. 新 Goal/反馈和公开回复隔离接线。
- 真实云端 Jev、Laya 模型推理与微调。
- 机器人 ROS Action、机械臂轨迹、effort/物理停止和真实抓放。
- PyRoKi 性能、RGB/雷达感知闭环、移动导航、GUI 演示。
- B08/B09 新功能和浏览器验收。
- Docker 镜像、Python 3.10、ARM64 或真机部署。

## 5. 文档交付与后续决策

更新了功能说明、施工索引、B08/B09 详细任务书和 B11 职责映射。移动机械臂文档按项目 A/L/R/T/M 分章，包含依赖、接口、步骤、故障、验收和人工介入点。Laya 原始模型实际 ROS 闭环排在微调前，旧的可选微调表述已替换。

**本轮同步与计划整理没有待决阻塞。** 保留的测试失败是后续执行开发的前置问题：由上游修复或另行安排本地诊断/修复，需要后续确定。本轮不扩大到修复开发，也没有开放新的执行能力。

原始证据保存在 evidence/astrbotex_sync_20261001（本机历史引用：`evidence/astrbotex_sync_20261001/`）。关键文件：

| 文件 | 内容 |
|---|---|
| `sync_manifest.json` | 135 个增量文件的前后 hash、上游 hash、保留差异与冲突 |
| `comparison.json`、`merges.json` | 三方比较及 Markdown 干净合并 |
| `before.json`、`protection_check.json`、`concurrent_changes.json` | 同步前状态、索引/版本保护核对，以及未覆盖的范围外变化 |
| `vendor_verification.json` | 216 个文件核对结果 |
| `framework_tests.txt`、`framework_tests.exit` | 首轮完整测试原始输出与退出码 |
| `ros_tests_retry.txt`、`ros_tests_retry.exit` | 仅 ROS 定向重试输出与退出码 |
| `test_environment.json`、`ros_build.txt` | 测试环境、既有示例接口构建结果 |

临时备份仍在 /tmp；长期证据以上述仓库文档目录为准。没有新增长期实验程序。


> 2026-10-02 归档补充：原始日志和完整响应已转入共享数据目录，相关证据链接已更新。文中的 `/tmp` 命令路径记录当时的实验环境，不保证临时目录仍存在。参见 [B09 前归档清单](../../docs/evidence/PRE_B09_ARCHIVE_MANIFEST.json)。
