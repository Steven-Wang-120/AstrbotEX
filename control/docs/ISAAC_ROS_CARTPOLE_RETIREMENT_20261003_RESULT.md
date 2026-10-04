# Isaac／ROS 清理与 CartPole 完整归档结果

日期：2026-10-03。**归档与清理完成；环境健康验收有一项未通过。** `import torch` 原生扩展加载挂起，原命令 120 秒超时，诊断执行 95 秒超时。没有启动仿真、训练或运动命令，没有安装／卸载依赖，没有 commit／push。

## 1. 当前开发状态

CartPole 场景、LQR/BalanceHold、StateCache、MoveCart 服务、SpinPole 接口、两个项目 ROS 包、ROS/RL 演示入口和专用测试已退出活跃工作区。三个旧 Python 缓存也在校验归档后移除。

保留 Isaac Sim 5.1.0.0、Isaac Lab v2.3.2、系统 Jazzy、MoveIt、Laya 环境及模型。环境版本配置不变。`ros2_ws/src/` 保留占位文件，尚无活跃项目 ROS 包；没有新建业务包或机械臂场景。

仍可使用：

```bash
# 环境诊断；当前 Torch 导入存在下述超时问题
./scripts/check_isaac_env.sh

# 在新的独立终端加载系统 ROS；没有 workspace install 也能使用
source scripts/dev_env.sh
```

`scripts/lib/isaac_entry.py` 仅接受 `check`。原 `health / setting / command` 函数 AST 与清理前完全一致，只有退休启动分支移除。ROS domain 诊断工具及单元测试保留。新场景的进程锁、日志和 ROS 启动接线仍待开发，旧实现可从归档参考。

## 2. 归档与空间

归档目录：`/data/shared/AstrEX_project_data/exports/cartpole_archive/20261003T173647+0800_8a0bd55ec6`。

- [恢复说明](evidence/retirement/20261003T173647+0800_8a0bd55ec6/README.md)
- [逐文件清单](evidence/retirement/20261003T173647+0800_8a0bd55ec6/MANIFEST.json)
- [清理前保护快照](evidence/retirement/20261003T173647+0800_8a0bd55ec6/protection/before.json)
- [移除清单与空间统计](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/removal.json)

| 分类 | 处理结果 |
|---|---|
| 源码、两个 ROS 包、入口、专用测试 | `retired_source/`，保持原相对路径 |
| 共享工具与文档原文 | `before/`，包括用户已有修改的 MVP 文档 |
| 工作空间构建／测试日志 | `retired_source/ros2_ws/log/` |
| 六个确认属于 CartPole 的非基线运行目录 | `runs/ros/`，原目录在校验后移除 |
| 三个旧字节码缓存 | `retired_cache/` |
| build/install | 删除可重建产物，保留原文件清单和哈希，不归档二进制副本 |
| 既有归档、最终服务、cutover、2026-09-17 基线运行 | 原位保留，MANIFEST 的 retained 提供位置 |

共校验 **314 项归档条目**，其中 **253 个文件／符号链接**；目录项只记录类型，文件记录内容 SHA256，符号链接记录链接文本及其 SHA256。原始日志和历史报告正文未改写。没有无法确认归属的待清理运行目录。

空间按移除前 `st_blocks × 512` 统计：

- 删除 build/install 的已分配块：**8,425,472 字节（8.04 MiB）**。这是可重建产物的清理量。
- 从活跃位置迁入归档：**189,607,936 字节（180.82 MiB）**。迁移不计作磁盘总容量释放。
- 本轮新增了保护快照和验收日志。没有用受其他进程影响的全盘空闲量变化宣称净释放值；全盘净收益小于上述生成产物清理量。

旧报告中的路径通过 MANIFEST 的 source/destination 查找。归档根含 COLCON_IGNORE，不参与构建。恢复须显式选择文件、核对冲突并重新构建；不能复制暂存区快照覆盖当前 Git index。

## 3. 本机检查

| 检查 | 结果与证据 |
|---|---|
| 归档副本与预期移除 | PASS；[完整性核对](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/archive_integrity.json) |
| 保留的 ROS domain 测试 | **9/9 PASS**；[原始输出](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/ros_domain_unit.txt)，仅构造图快照，不创建 DDS 节点 |
| 干净系统 ROS shell | PASS；Jazzy 正常加载，AMENT_PREFIX_PATH 仅 `/opt/ros/jazzy`，两个退休包及 Python 模块均不可发现；[输出](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/clean_ros_environment.txt) |
| 空工作空间发现 | PASS；colcon 不发现项目包，旧 overlay 已删除 |
| 旧入口与内部 profile | PASS；三个启动脚本已移除，`ros`、`rl` 参数在健康检查前返回用法错误；[记录](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/entry_retirement.json) |
| Shell 语法、Python AST、活跃引用、文档链接、差异 | PASS；[最终静态检查](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/final_static.json) |
| 原暂存区与其他任务保护 | PASS；[保护核对](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/protection.json) |
| Conda 激活与 Python | PASS；Python 3.11.16；[诊断输出](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/activation_probe.txt) |
| Isaac 完整健康检查 | **未通过：超时**；详情如下 |

首次运行原命令，外层等待 120 秒后收到 TimeoutExpired。该包装器未在异常前落盘子进程输出，因此没有首次健康结果 JSON；[首次失败记录](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/isaac_health_initial.json)明确记录此限制，不补造输出。

后续先单独确认 Conda 激活正常，再运行一次临时进度诊断。它使用相同健康逻辑，只增加进度打印和 30 秒堆栈，不修改仓库函数。堆栈显示停在 Torch `__init__.py:409` 导入原生扩展、importlib `create_module`，随后在 95 秒期限终止诊断自身的进程组。[诊断日志](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/isaac_health_diagnostic.txt)、[退出记录](evidence/retirement/20261003T173647+0800_8a0bd55ec6/checks/isaac_health_diagnostic.json)。

该证据定位了停顿位置，**没有证明根因**。本次未完成 CUDA/GPU 可用性和后续 pip check，不能报告整个环境健康 PASS。已知 pip 警告清单原样保留，但未在本次重新验证。没有循环重跑或跳过 Torch 检查来筛选通过结果。

## 4. 文档、保护与未处理事项

README、环境基线、目录指南、CartPole 历史索引、服务退休说明和 MVP 计划已更新。MVP 只修正归档状态与可复用代码的位置，不纳入本轮之外的 Laya 头脑风暴改动。

清理前 HEAD 为 `4cf7e11a201d82c0332a5ecc4f6bd1864d30444d`。未写入原有施工索引、B09 改动与未跟踪内容；受保护的无关跟踪文件哈希一致；已被用户删除的 GUI 报告未恢复。暂存区原始字节、暂存条目、remote 和 submodule 状态保持一致。

未处理事项只有 **Torch 原生扩展导入挂起**。需要后续独立环境诊断；本轮不升级驱动、不重装依赖、不清空全局缓存。该问题不影响归档完整性，但完整环境健康验收尚未达成，开始新 Isaac 开发前应解决。

未运行 GUI、CartPole 运动回归、RL 训练或真实 ROS 控制。清理完成后停止，移动机械臂开发另行安排。


## 5. 后续独立环境排查（2026-10-03）

新证据见 [MVP 环境排查报告](MVP_ENVIRONMENT_AUDIT_RESULT.md)。系统 ROS 与 9 个保留单元测试通过，EX/Laya 依赖隔离保持；Isaac 环境仍未完成验收。新诊断发现进程在等待文件页，同时系统盘 nvme1 多次出现 I/O 超时。系统盘完成通知／驱动路径成为首要排查方向，但尚未证明具体根因或完成修复。

目前等待本机只读 SMART 输出；未更改依赖、驱动、控制代码或执行权限。CUDA、空 Isaac、实际 ROS clock 与 GUI 尚未运行。以上是后续阶段性结论，不改写本报告原有超时记录，也不将历史 FAIL 改为 PASS。

后续补充（同日 20:43）：已从 UDisks2 只读接口取得健康缓存，未显示严重告警、介质错误或设备错误日志计数；不再需要手动 sudo 输出。NVMe I/O 超时仍未解释，拟由用户确认正常重启的时间后继续验证，不据缓存无告警宣称系统已修复。


### 2026-10-04 补充：PyTorch 导入通过

按用户要求延长等待后，原 Isaac 环境的 `import torch` 成功，版本为 `2.7.0+cu128`，进程正常退出。导入语句耗时 51.7 秒，完整启动与退出耗时 151.5 秒；没有更改包或启动配置。本轮不再要求先重启／处理系统盘才继续导入。原超时记录保留为历史，不能据此判定 PyTorch 永久挂起。详见[环境排查报告第 7 节](MVP_ENVIRONMENT_AUDIT_RESULT.md)。本次范围仅为 PyTorch 导入，其余环境运行层未复验。
