# MVP 环境排查结果

**最新结论（2026-10-04）：原 Isaac 环境的 `import torch` 已通过，版本为 `2.7.0+cu128`。** 按用户本轮要求延长观察时间，同一进程完整运行并正常退出；没有重装包或更改环境。完整启动与退出耗时 151.5 秒，Torch 导入语句耗时 51.7 秒。本次仅处理 PyTorch 导入，不扩展到 CUDA、Isaac GUI 或 ROS bridge 验收。

下文第 1–6 节保留 2026-10-03 的历史排查记录；其中“必须先处理系统盘／重启才能继续”的建议不再作为前置条件。NVMe 日志仅保留为当时的观察，不能据此前的短期限直接判定 PyTorch 无法导入。最新证据见第 7 节。

---

日期：2026-10-03。**环境排查尚未完成：已发现系统盘 NVMe I/O 超时，尚未修复；不能判定 Isaac 环境就绪。** 系统 ROS、保留的单元测试和 EX/Laya 依赖隔离检查通过。没有改依赖、控制代码或执行权限，没有启动机器人、训练、commit 或 push。

实际项目路径为 `/home/sssxy/Projects/AstrEX_project_main`，大小写已经核对。证据目录：

`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/`

下文的证据路径均相对该目录；每个 `probes/<名称>/` 保存 `result.json`、`stdout.txt` 和 `stderr.txt`。退出记录包含命令、解释器路径、起止时间、期限、退出码和进程回收结果。诊断源码保存在 `diagnostic_sources/`。

## 1. 已证实的事实与尚未确定的原因

### 已证实

1. 项目启动环境与干净进程环境的 Torch 导入均在 25 秒期限内未完成。二者使用同一个 Isaac Python 3.11.16；未发现 ROS Python、Laya 或错误 Conda 前缀混入。动态库依赖均能解析到目标环境或系统库，没有发现 `not found`。
2. 停顿位置并不总在 Torch 原生扩展。系统调用追踪和后续诊断已经到达 `torch._import_device_backends()`，正在通过 `importlib.metadata` 读取包的 `entry_points.txt`。不能继续把所有停顿都称为 `_C` 初始化失败。
3. PID 47798 的现场状态为 `D`，等待点是 `folio_wait_bit_common`，同时 Python 堆栈在元数据文件读取。`torch_syscalls.txt` 最后记录了打开 `isaacsim_robot-5.1.0.0.dist-info/entry_points.txt` 的未完成系统调用。该路径是普通的 43 字节文件；后续一次有界读取成功，不能据此判断文件损坏。
4. Isaac 环境位于系统盘 `/dev/nvme1n1p6`。内核多次记录 `nvme nvme1: I/O ... timeout, completion polled`，包括 17:40:15、17:41:19、17:44:07、17:45:12，以及本轮 20:21:34、20:24:11、20:26:30、20:35:20。时间覆盖原健康检查和本轮诊断。
5. 不执行 Tensor 运算的 `python -m pip check` 也在 25 秒期限超时；随后出现上述 20:35:20 的 NVMe 日志。该探针没有获取内部堆栈，因此不能单凭它证明具体等待点。
6. 现场有约 22 GiB 可用内存，swap 使用为 0；未见内存压力。系统盘宿主挂载为 `rw`。沙箱只读挂载投影与宿主不同，但本轮诊断在宿主执行也超时，不能归因于沙箱只读权限。

证据：宿主存储记录（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/probes/05_host_storage/stdout.txt`）、进程状态与映射（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/native_process.json`）、Python 堆栈（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/native_probe_stderr.txt`）、后续内核日志（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/kernel_followup.txt`）。

### 判断与限制

当前证据支持：**至少本轮部分停顿发生在文件 I/O 等待，系统 NVMe 完成通知异常是首要排查方向。** 尚未通过修复前后对照证明它解释全部历史超时，也没有证明 Torch 包、GPU、SSD 介质或驱动中的哪一项损坏。

Linux v6.17 主线源码中，这条日志来自超时处理后轮询到请求完成的分支，源码注释指向可能遗漏的中断。这有助于解释日志，但不直接确定本机 OEM 内核的根因，更不能据此自动关闭 APST/ASPM。[Linux NVMe PCI 驱动源码](https://github.com/torvalds/linux/blob/v6.17/drivers/nvme/host/pci.c#L1520-L1531)。

系统盘为 SK hynix PC811 SED 1024GB，固件 `61072141`；内核为 `6.17.0-1032-oem`。共享证据目录位于另一块 `/dev/nvme0n1p2`。首次只读 `sudo -n nvme smart-log /dev/nvme1 -o json` 返回“需要密码”，没有执行设备修改；这不是自动审批拒绝。随后通过 UDisks2 的只读属性和 `SmartGetAttributes` 成功取得健康信息，不再需要用户为此提供 sudo 输出。

UDisks2 的健康缓存更新时间为 20:37:06，读取于 20:42–20:43：`SmartCriticalWarning=[]`、`media_errors=0`、`num_err_log_entries=0`、剩余备用空间 100%、寿命使用估计 0%、温度 299 K（约 26°C）。这些值未显示介质健康告警，不能排除中断、驱动或固件问题。`SmartSelftestStatus=success` 也可能表示从未自检，不能据此宣称自检通过。本轮没有调用刷新、自检或配置方法。[UDisks2 接口定义](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.NVMe.Controller.html)、实际属性输出（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/probes/15_udisks_smart_attributes/stdout.txt`）。

未禁用 Torch backend autoload，未跳过原生扩展，未预热全部磁盘文件来制造通过结果，未延长原健康期限，也未新增警告白名单。

## 2. 环境版本与修改

| 环境 | 本轮读取到的版本／状态 |
|---|---|
| Isaac Python | Conda `isaaclab232_test`，Python 3.11.16 |
| Isaac Sim / Lab | Sim 5.1.0.0；Lab 源码配置 v2.3.2；完整 experience 配置保持不变 |
| Isaac Torch 组合 | torch 2.7.0+cu128、torchvision 0.22.0+cu128、torchaudio 2.7.0+cu128；本轮只完成版本元数据读取 |
| GPU | RTX PRO 1000 Blackwell Generation Laptop GPU；驱动 580.178.04；8151 MiB 显存 |
| 系统 ROS | `/opt/ros/jazzy`；系统 Python 3.12.3 |
| MoveIt / ros2_control | MoveIt 2.12.4；ros2_control 4.48.0；joint_trajectory_controller 4.42.1 |
| EX | 独立 Python 3.12.3；pyzmq 27.2.0、websockets 17.1；未发现 torch/transformers/laya 分发包 |
| Laya | 独立 Python 3.12.3；laya 0.3.22、torch 2.8.0+cu128、transformers 4.57.3；53 项精确版本依赖与 lock 一致 |

**环境修复变更：无。** 现有证据不足以支持重装包；没有更改环境变量配置文件、启动隔离逻辑、内核参数、驱动、固件或任何软件版本。当前只新增本报告，并在清理报告末尾追加链接与结论。

临时探针仅在自身进程设置诊断环境；native 探针只给自己的子进程开放 ptrace 观察，未修改系统 ptrace 配置。诊断子进程已回收。没有需要恢复的持久环境设置。文档回退只需移除本报告及本次追加段，不能回退其他任务文件或 Git index；追加前原文另存于本轮 `before/`。

## 3. 实际检查与失败记录

| 证据目录／命令 | 结果 | 耗时与含义 |
|---|---|---|
| `00_survey`：环境、GPU、依赖来源、显示与磁盘只读查询 | PASS | 0.408 s；元数据和设备枚举，不是 CUDA 运算 |
| `01_project_torch_import`：加载 `isaac_common.sh` 后导入 Torch | TIMEOUT | 25.051 s；保留 8 s 堆栈，期限到达后终止自身进程组 |
| `02_clean_torch_import`：干净环境、同一解释器导入 Torch | TIMEOUT | 25.044 s；排查继承环境污染，未得到成功导入 |
| `03_project_torch_strace`：同入口加受限系统调用追踪 | TIMEOUT | 25.226 s，包括 20 s 运行期限与终止等待；未完成文件打开留有记录 |
| `04_native_torch_stack`：观察进程状态并尝试 native backtrace | 部分取证 | 16.624 s；监视程序退出 0，但 gdb 观察到期、未得到 native backtrace，Torch 导入没有通过 |
| `05_host_storage`：挂载、内核、内存、NVMe 元数据 | PASS | 0.213 s；读取到系统盘 I/O 异常 |
| `06_nvme_smart_readonly` | 未完成 | 0.216 s，退出 1：非交互 sudo 需要密码 |
| `07_system_ros`：干净 shell 加载 `scripts/dev_env.sh` | PASS | 0.413 s；Jazzy／系统 Python 正常，旧 overlay 和两个退休包均不可发现 |
| `08_ros_domain_units`：现有 `test_ros_domain_preflight.py -v` | **9/9 PASS** | 外层 0.403 s；测试本体 0.001 s；构造图快照，没有 DDS 通信 |
| `09_laya_lock_health` | 依赖 PASS；服务未运行 | 0.203 s；53 项版本一致；GET `127.0.0.1:8769/health` 连接被拒绝，无运行中服务可复用 |
| `10_ex_dependency_metadata` | PASS | 0.208 s；EX 依赖与模型环境隔离 |
| `11_isaac_pip_metadata`：原环境 `python -m pip check` | TIMEOUT | 25.029 s；没有输出依赖检查结果 |
| `12_system_ros_package_versions`：dpkg 元数据查询 | 部分已安装／缺包 | 0.208 s；退出 1 因 Nav2 和 topic-based 包未安装，不代表已列出的 MoveIt 包失败 |
| `13_udisks_cached_health` | 部分取证 | 0.209 s；块设备信息可读，临时探针未剥除对象路径的引号，后续属性查询参数错误；保留原记录 |
| `14_udisks_cached_health` | PASS | 0.213 s；只纠正临时探针路径引号，取得控制器缓存健康属性 |
| `15_udisks_smart_attributes` | PASS | 0.208 s；只读 SmartGetAttributes 返回介质／错误计数，没有触发设备修改 |

`03` 的结束记录瞬间仍发现进程组；没有把该字段改写成成功。后续 `/proc` 查询确认 PID 47617/47625 已退出，最终扫描全部诊断组均无残留。`04` 自己创建的诊断子进程也已回收。没有停止其他任务进程。

原始健康检查的四项已知 pip metadata 警告清单保持原样，涉及 wheel/packaging、fastapi/starlette、isaacsim-kernel/psutil 与 typing_extensions。**本轮 pip check 未完成，不能声称已重新确认仅有这四项警告。**

## 4. 分层验收状态与 MVP 影响

| 层次 | 当前状态 | 下一步与实际含义 |
|---|---|---|
| Python / CUDA | **未通过** | Torch 导入超时；vision/audio 导入、CUDA 小张量有限结果及 synchronize 均未执行 |
| 原 `check_isaac_env.sh` | **仍未验收** | 保留旧 120 s／95 s 失败证据；本轮未在前置失败时再跑完整健康检查 |
| 系统 ROS | **通过环境与单元检查** | 新 shell 仅加载系统 Jazzy；没有实际 Isaac DDS 控制证据 |
| 空 Isaac 全 experience | **未运行** | 依赖 Torch 层先通过；不以 metadata 冒充物理／渲染初始化 |
| Isaac → Jazzy `/clock` | **未运行** | 后续使用独立探针 domain 和命名空间，验证实际收到递增时间戳 |
| GUI | **有显示，尚未验证** | `DISPLAY=:1` 且 xdpyinfo 成功；因前置阻塞未启动 Isaac，没有伪造截图 |
| EX/Laya | **依赖保护通过；本次推理未测** | 无可复用运行实例，未新开模型服务；历史 B07 结果保留其原范围 |
| MoveIt | **安装／接口可用** | moveit_msgs 服务和消息可导入，OMPL 包可发现；机器人规划执行未开发 |
| Nav2 | **缺失** | nav2_bringup/nav2_msgs 未安装；列入项目 M 的依赖准备，不在本次自动安装 |
| 机械臂接口 | **后续开发缺口** | topic_based_ros2_control、xArm 配置／场景待 A/R；moveit_py 未安装不影响已规划的标准服务接口路线 |

GPU 空闲取样约占用 1.98–1.99 GiB／总 7.96 GiB。**Isaac GUI 与 Laya 并发未测试，不能据空闲显存推断共存可用，也不能声称已经验证只能分时。** 后续应分别记录单独与同时运行的实际显存；当前不开展 Qwen、多模型或训练压力实验。移动机械臂闭环、技能选择效果、训练与 p95 延迟仍属于对应业务项目。

## 5. 工作区与控制端保护

诊断开始时保存了 HEAD、暂存条目、index 原始字节、remote、submodule，以及 442 项跟踪／保护文件的指纹。写报告前，442 项均与初始快照一致；未发现本轮期间的并行修改。已有清理删除、B09 和 MVP 文档的未提交状态均保留。

本轮仅允许本报告与清理报告追加段发生文档差异。最终核对记录在 protection/after.json（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/20261003T201849+0800_5dd68ecb/protection/after.json`）：index 字节、暂存条目、HEAD、remote、submodule 不变；控制／契约／调度／停止／模型配置和环境脚本未修改。没有恢复 CartPole、SpinPole、MoveCart 或旧 overlay。

## 6. 人工介入与续验条件

当前真实阻塞：**系统盘 I/O 异常尚未定位到可安全修复的具体设置，需要确认下一步系统层操作。** SMART 信息已经通过只读系统服务取得，原先要求用户提供 sudo 输出的步骤取消。

最小下一步建议：用户保存其他开发任务后，正常重启一次；不修改软件版本、内核参数或设备电源设置。重启会中断当前进程和会话，必须先确认可用时间，不能由本任务自行执行。此步骤没有持久配置变更可回退，但无法恢复未保存的其他会话，因此应由用户安排。一次重启后通过也不能单独证明根因，必须记录相同命令的结果及是否再出现 NVMe 超时；若仍发生，再讨论针对系统驱动／固件的具体方案。

依据用户环境任务的边界，任何重启、驱动／固件变化或影响其他任务的设备设置调整均须先确认。本轮未执行这些动作，也未停止其他任务进程。

系统 I/O 原因处理后，按顺序继续一次有界验收：同一 Isaac 解释器的匹配 Torch 组合与 CUDA 运算 → 原健康检查 → 空 Isaac 完整 experience → 实际 `/clock` → GUI 截图及正常退出。按现场服务状态验证受控单实例 Laya，并记录共存条件。保留当前失败证据，不通过重复重跑筛选 PASS。

当前交付是**有证据的阶段性排查报告，不是环境修复完成报告**。没有自动进入任何 MVP 业务开发。


## 7. 2026-10-04：延长观察后的 PyTorch 导入结果

用户将本轮范围收敛为只处理 PyTorch 导入，并明确首次启动可能较慢。本次继续使用 `scripts/lib/isaac_common.sh` 的原环境隔离与激活逻辑，给单个导入进程 900 秒观察上限，每 30 秒记录进程状态；没有因短时无输出或文件页等待重启该进程。

| 项目 | 实测结果 |
|---|---|
| 解释器 | `/home/sssxy/miniconda3/envs/isaaclab232_test/bin/python`，Python 3.11.16 |
| 导入 | `import torch` 成功，`torch.__version__ == 2.7.0+cu128` |
| 来源 | 目标 Conda 环境内 `site-packages/torch/__init__.py` |
| 导入语句计时 | 51.720 秒 |
| 完整启动及退出计时 | 151.541 秒；与导入计时的差值包括环境激活、解释器／探针初始化与退出，未进一步拆分 |
| 结束状态 | 退出码 0，诊断进程组已退出；没有触发 900 秒期限 |
| 持久环境修改 | 无；没有升级、重装、禁用 backend autoload 或跳过导入 |
| 工作区 | 仅更新本报告和清理报告补充说明；暂存区不变；未 commit／push |

**结论：本次 PyTorch 导入可通过等待完成，无需代码或依赖修复。** 2026-10-03 的 25 秒探针和更早的健康检查超时仍是历史事实，但不能据此推断永久挂起或包损坏。当前证据不确定首次读取的底层延迟根因，也不表示 CUDA 运算、完整 Isaac 健康检查或机械臂闭环已经通过。

不再把重启或系统盘修复作为继续导入的必要条件。本轮没有执行重启，也不对两次会话之间的系统操作作推断。首轮启动检查应给冷启动足够时间，持续观察同一进程；不通过修改原健康检查内容降低标准。

本次证据：退出与计时（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/torch_import_20261004T152418+0800/result.json`）、导入版本与来源（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/torch_import_20261004T152418+0800/stdout.txt`）、进程观察（本机历史引用：`/data/shared/AstrEX_project_data/logs/isaac/environment_audit/torch_import_20261004T152418+0800/progress.jsonl`）。临时探针和本次文档修改前的副本均保存在同一目录。
