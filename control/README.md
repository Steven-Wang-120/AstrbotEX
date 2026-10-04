# 完整移动机械臂 MVP 开发快照

本分支发布 P/L/A/R/E/T/M 的全部**现有**源码、配置、测试、正式 T1 数据和研究记录。它不是全部阶段通过的发行版。EX/Laya 上层位于仓库根目录，Isaac/ROS 工程位于本目录。

- [完整开发计划](docs/MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md)
- [当前阶段结果与待确认项](docs/MVP_FIRST_STAGE_RESULT.md)
- [Laya 与 T1 结果](docs/MVP_LANGUAGE_FIRST_STAGE_RESULT.md)
- [ROS 控制接口](ros2_ws/src/astrex_mobile_manipulation/INTERFACE.md)
- [发布核对](docs/MVP_BRANCH_PUBLICATION.md)

当前 P1 已完成两种原生雷达与 RGB 的真实 ROS 采集。L1 接口/探针、T1 合成数据已完成对应第一阶段；原模型仅 2/20 正确，不能用于自主运动。A/R/E/M 的实际运动、停止、抓放和导航验收尚未完成。M1 的 200 ms 墙钟反馈时序问题仍在。没有进行微调，也没有完成自然语言直接驱动机器人的后续接线。CartPole/MoveCart/SpinPole 仍为退休历史。

## 布局和 EX 接线

`astrbot_ex/core/tasks`、`plugins/control/mobile_manipulation` 和根目录语言脚本包含本次上层实现。控制端会默认找到同一仓库根目录的 EX；原 `apps/AstrBotEX` 布局也兼容。若使用另一个匹配的 EX 检出，设置 `ASTREX_EX_ROOT` 指向包含 `astrbot_ex/` 的目录。显式路径错误时立即报错，不静默使用其他副本。插件默认不启用，加载文件不启动运动。

## 环境准备

全部物理实验仅适用于 Isaac 仿真。记录环境为 Ubuntu 24.04、系统 ROS 2 Jazzy、Isaac Sim 5.1、Isaac Lab 2.3.2、MoveIt 2.12.4、Laya 0.3.22。不要把系统 ROS Python 注入 Isaac Python。

1. EX Python 依赖沿用根目录 `requirements.txt`；Laya 单独使用 `requirements-laya.lock` 及原有部署说明。虚拟环境和权重不随 Git 发布。
2. 从本目录运行控制命令。先按机器修改 `config/isaac_baseline.env` 的 Conda、Isaac Lab 和输出目录；其值是原研究机器的默认值，不表示这些路径已在新机器存在。
3. ROS 私有依赖的版本及原 deb SHA256 见 `config/mobile_manipulation/dependencies.lock.json`。它的绝对 prefix 是历史安装记录；本分支使用 `control/runtime/mobile_manipulation_deps/root`。按清单获取对应 deb，核对 SHA256，用 `dpkg-deb -x PACKAGE.deb runtime/mobile_manipulation_deps/root` 解包。无法取得锁定版本时报告差异，不静默升级并继承旧实验结论。
4. 将官方 xarm_ros2 克隆到 `runtime/mobile_manipulation_deps/xarm_ros2`，检出 `config/mobile_manipulation/robot_source.json` 固定的 revision。xArm6 USD 使用原报告记录的 Isaac 5.1 资产；将 `robot_source.json` 的 `isaac_asset` 改成实际位置。官方资产、第三方模型和安装内容不纳入源码。
5. 在独立系统 ROS shell 中执行 `source scripts/mobile_manipulation_env.sh`、`/usr/bin/python3 scripts/prepare_mobile_robot.py`、`bash scripts/build_mobile_manipulation.sh`。两个开发场景的 profile_path 使用本目录下的相对路径，所以后续命令也从 `control/` 运行。
6. Isaac 启动脚本在独立新 shell 使用。首次加载允许等待同一个进程。首次机械臂 GUI/通道确认及保护门限决策仍是待完成项；这次发布没有执行这些实验。

Laya 原模型来源为 `convaiinnovations/laya`，bundle revision `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`，checkpoint `typed-decisions`，权重 SHA256 见语言报告。通过 Hugging Face 按该 revision 获取模型文件到外部缓存，并向根目录 `scripts/probe_mobile_tasks.py` 显式传 `--laya-python` 和 `--cache`。`--run-real` 才会加载真实模型；此次发布不重新执行。multilingual 只做过元数据核对，尚未下载或批准切换。

## 数据与验证

正式数据仅为 [T1 v2](data/t1_corpus_v2)：100 个任务/场景组，70/15/15 划分，共 230 行。数据是 SYNTHETIC，不是物理成功标签。冻结数据和人工审阅原文保留原 hash；不重写或重新生成覆盖它们。

从仓库根目录运行：

```bash
PYTHONPATH=. python -B -m unittest tests.test_mobile_tasks tests.test_mobile_control_plugin tests.test_owned_laya tests.test_laya_backend tests.test_laya_service_integration -v
PYTHONPATH=control/ros2_ws/src/astrex_mobile_manipulation /usr/bin/python3 -B -m unittest discover -s control/ros2_ws/src/astrex_mobile_manipulation/test -v
source /opt/ros/jazzy/setup.bash
/usr/bin/python3 -B -m unittest discover -s control/tests/isaac -p 'test_*.py' -v
```

第一条用 EX Python 环境，需要本机 loopback socket，使用假 HTTP 服务，不加载真实模型。其余为纯逻辑测试，不发送机器人命令。既有物理证据为导出前实验，不能当成本次发布重跑结果。

大型原始记录仍在原机器。报告中已打包的小型证据使用相对链接，未打包项目明确标注“本机历史引用”，索引见 [local-only-references.json](docs/evidence/local-only-references.json)。失败实验和失效数据审计仍保留，不能拿成功窗口覆盖它们。
