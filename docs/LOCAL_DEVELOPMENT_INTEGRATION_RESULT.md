# 本地 EX 开发成果集成结果

日期：2026-10-02。仓库：`/home/sssxy/Projects/AstrbotEX`。分支：`integration/laya-b08-b09-ros`。

**源码迁移、独立环境、页面及通用 ROS 验证已完成，提交仅在本机，未 push。** 全量回归不是全绿：624 项中 623 项通过，1 项未修改的 Dispatcher 压力测试超时；未修改的上游副本也重现该问题，根因仍未确定。

## 1. 来源与保护范围

| 项目 | 固定版本或结果 |
|---|---|
| 新仓库起点 | `1f6e4a86f46e661e9a26d522d930fb036cdf156a` |
| 三方内容基线 | `c9624a439874740cde4acc347c979419af120d05` |
| 原仓库来源 | `4cf7e11a201d82c0332a5ecc4f6bd1864d30444d`，映射 `apps/AstrBotEX/` 到新仓库根目录 |
| 明确迁入的未提交内容 | B09 浏览器脚本、Python fixture、结果报告与证据，以及原施工任务书当前文本 |
| 原仓库保护 | HEAD、工作区状态、index 字节一致；447 个来源文件哈希无变化 |
| 新仓库原有内容 | `.vscode/` 的 1 个文件哈希未变，未纳入提交 |
| 环境与数据 | 创建新 `.venv` 和 `runtime/laya/.venv`；不复制旧虚拟环境，仅复用下载缓存和固定共享权重 |

没有合并两个仓库的完整 Git 历史，没有改主分支或 remote。保留新上游的依赖升级、删除记录和迁出验证代码的决定。未恢复旧 `tests/test_ros2_integration.py` 或旧 Jev 离线数据。三份 Markdown 和四个 PowerShell 文件的纯格式差异未作为功能迁入。

没有迁入 CartPole、旧 `ros2_ws`、StateCache、Isaac 启动器、控制器、build/install、数据库或凭据。后续控制计划引用原仿真项目，不代表这些模块存在于本仓库。

来源差异见 [迁移清单](evidence/integration_20261002/migration-manifest.json)，保护核对见 [审计记录](evidence/integration_20261002/protection-audit.json)。

## 2. 核心冲突的处理

| 文件 | 合并后行为 |
|---|---|
| `core/api_server.py` | 同时装配上游 DecisionController/PublicDelivery 和本地 DecisionManagement，共用 DecisionService。保留管理鉴权、恢复边界、停止未证明时的关闭保护，以及全部组件的正常关闭处理 |
| `core/decision/service.py` | 保留上游取消、租期、终态证明、完成回调及执行/存储 I/O 故障处理；加入本地停止回执、可信后端切换和实际请求追踪 |
| `tests/test_decision_service.py` | 使用公开的 `stop.state == proven` 完成边界，保留原业务断言；不把 `_stop_pending=False` 当作停止完成 |

新增两处交叉断言，验证“取消终态与停止回执一致”和“迟到物理停止证明不清除 blocked、不产生任务成功”。上游证明合格的 FAILED 路径也使用本地停止回执，避免只有 pending 标志而没有可查询操作。

Goal/Action 线协议保持不变。普通 Jev/Laya 仍只允许 disabled/shadow；网页和 HTTP 不能授予 execute。Laya 请求发出后超时、取消或响应不确定，仍进入 `restart_required`，恢复要求确认旧进程退出、新服务预热、可信后端切换和新的 Goal 授权。

## 3. 验证结果与失败记录

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| 现有全量回归 | 624 项：623 通过、1 error、0 skip | [首轮完整日志](evidence/integration_20261002/core-01.log) |
| 冲突交叉边界 | 2 项通过 | [日志](evidence/integration_20261002/cross-boundary-01.log) |
| B09 页面 | 16 组通过 | [结果](evidence/integration_20261002/browser-01/result.json) |
| 原页面兼容 | 7 项通过 | [结果](evidence/integration_20261002/legacy-browser-01/result.json) |
| 新 Laya 环境 | pip check、GPU 健康、固定预热、一次 shadow 通过 | [结果](evidence/integration_20261002/laya-shadow-02/result.json) |
| ROS 示例接口 | 独立临时 build/install 成功，1 个消息包 | [构建日志](evidence/integration_20261002/ros-build.log) |
| 原生 ROS 通信 | Jazzy、Domain 73、唯一 Topic，受鉴权 HTTP 配置后 DDS echo 往返通过，配置恢复 | [结果](evidence/integration_20261002/ros-native-02/result.json)、[往返与恢复](evidence/integration_20261002/ros-native-02/roundtrip.log) |
| Docker | Compose YAML 能解析；启动脚本语法通过，HTTP 默认 loopback，DDS 保留 host 网络 | 本机无 Docker CLI；未运行 compose、构建镜像或部署 |

实际执行的主要命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
node scripts/verify_decision_ui.mjs --output /tmp/astrex_integration_20261002/browser-01
node scripts/verify_dashboard_management.mjs --base <临时实例地址> \
  --token-file <临时凭据文件> --output <证据目录>
runtime/laya/.venv/bin/python -m pip check
.venv/bin/python scripts/verify_ros2_deployment.py \
  --url <临时本机实例地址> --token-file <临时凭据文件>
```

隔离实例的装配脚本保存在[证据目录](evidence/integration_20261002/)，复用已有 B08/ROS 验证设施，不是新增生产控制程序。原始工作目录为 `/tmp/astrex_integration_20261002`；运行实例及其凭据没有复制进 Git。

### 3.1 保留的压力测试问题

失败项：`tests.test_action_dispatcher.DispatcherTests.test_1000_real_dispatcher_state_sequences_with_resources_and_rearm`。

- 首次全量回归在 RUNNING report 的 Future 等待处超时。
- 合并版单项诊断，以及安装完成后的单项诊断，仍出现超时；其中关闭清理也可能报告 `dispatcher did not drain`。
- 原版上游第一次单项运行通过；随后带 SQLite 慢调用计时的对照重现相同 RUNNING report 超时和关闭超时。**不能因为一次通过就排除问题。**
- 线程栈观察到 Ledger 的 SQLite 提交/查询和 Dispatcher 等待链，但不足以证明最终根因。本轮未改 SQLite 参数、等待预算、压力规模或断言。
- Dispatcher、Ledger、Actor 和该压力测试保持上游原样。原 B04 历史报告也记录过此类压力测试超时；本轮仍单独保存证据，不把它直接等同于已定位的旧问题。

对照见 [原版首次单项](evidence/integration_20261002/upstream-fuzz-01.log)、[合并版诊断](evidence/integration_20261002/merged-fuzz-diagnostic-02.log)、[安装结束后诊断](evidence/integration_20261002/merged-fuzz-no-install-03.log)、[原版 SQLite 诊断](evidence/integration_20261002/upstream-sql-diagnostic-02.log)。本轮保留为开放问题，后续单独排查，不声称全部回归通过。

### 3.2 两次验收脚本/环境纠正

ROS 首次验证命令用 `PYTHONPATH=.` 覆盖了 Jazzy setup 路径，导致 `rclpy` 导入失败。修正为保留 ROS 的 PYTHONPATH 后通过，没有改变生产逻辑。

Laya 首次验收在服务启动后、后端切换前检查执行权限，此时服务仍使用 MockBackend，断言不适用于该阶段。将检查放到可信 shadow 切换之后重新验证通过。首次自有模型进程已退出，失败记录保留。成功那轮服务日志包含两次推理 POST：一次固定预热、一次正式 shadow；不是完整模型质量评测。

### 3.3 模型和 ROS 结论的边界

新 Laya 环境为 Python 3.12.3、Laya 0.3.22、Torch 2.8.0+cu128，实测 checkpoint 在 CUDA 上，无 CPU fallback。源码 SHA 和权重 revision 沿用既定固定值。成功那轮 Actor 命令数和动作账本条数均为 0，自有模型进程已确认退出。

本轮没有重测历史 `replan 0/8`，没有微调，没有测机械臂控制效果。checkpoint 的温度校准警告仍保留在服务日志中，不把 confidence 称为物理成功概率。一次 shadow 不能支持 p50/p95 或实时性能结论。

ROS 验证的是示例字符串跨独立进程的真实 DDS 通信。它没有发送运动命令，不能替代 Laya → ROS 执行器 → Isaac → 物理成功的后续验收。

## 4. 本机提交

| 阶段 | 提交 | 内容 |
|---|---|---|
| S1 | `d92f32c` | B04 边界、B07 Laya、B08、三方冲突及独立路径适配 |
| S2 | `95fa6de` | B09 页面、未提交浏览器 fixture/script、文案适配 |
| S3 | `706b90a` | ROS 鉴权、loopback 默认值、独立接口目录和部署兼容 |
| S4 | 本报告所在的 `docs: record local integration provenance and validation` 提交 | 来源、历史报告、当前证据与 MVP/B11 状态；清除迁入脚本一行尾随空白 |

新仓库未设置 Git 作者，首次 commit 未创建；成功提交仅通过本次 Git 命令沿用原仓库已配置的作者身份，没有修改全局配置。没有 push。

## 5. 使用新仓库

普通管理页面：

```bash
cd /home/sssxy/Projects/AstrbotEX
export ASTRBOTEX_DATA_DIR="$PWD/runtime/ex-instance"
.venv/bin/python -m astrbot_ex.core.api_server --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/`。启动输出只显示管理员凭据文件路径；将该文件内容输入页面凭据栏。打开页面、保存配置和启动服务均不等于授权动作。

在“决策”页保存 Laya 配置后，显式启动自有服务，再切换到 shadow。默认 Python 为本仓库 `runtime/laya/.venv/bin/python`，可由 `ASTRBOTEX_LAYA_PYTHON` 覆盖；共享缓存由 `ASTRBOTEX_LAYA_CACHE` 覆盖。普通页面不能开启 Laya execute，也不能凭空生成 Goal。正式任务继续走现有上游任务入口。

本机 Jazzy 启动：

```bash
cd /home/sssxy/Projects/AstrbotEX
export ASTRBOTEX_DATA_DIR="$PWD/runtime/ex-instance"
ROS_DISTRO=jazzy \
ASTRBOTEX_ROS_INTERFACES=/tmp/astrex_integration_20261002/ros-build/install \
bash scripts/run_ros2.sh
```

上述接口目录是本次临时验收产物。长期使用时在选定工作空间重建示例接口，再设置 `ASTRBOTEX_ROS_INTERFACES`。不要加载旧 CartPole overlay。远程管理通过 SSH 隧道访问本机端口；不把管理服务改为 `0.0.0.0`。

检查并由用户 push：

```bash
cd /home/sssxy/Projects/AstrbotEX
git log --oneline main..HEAD
git diff --stat main...HEAD
git push -u origin integration/laya-b08-b09-ros
```

## 6. 后续范围与人工介入

后续任务见[施工索引](AstrEX_workingTree/00_施工总索引与派工规则.md)、[移动机械臂计划](MOBILE_MANIPULATION_PYROKI_MVP_PLAN.md)和 [B11 任务书](AstrEX_workingTree/B11_高风险_ROS2执行插件与安全停止.md)。现有 EX Laya 接入已完成；控制端轨迹评分、机器人网关、Isaac 执行、微调和 GUI 抓放仍未开发。

本轮没有新的资产或算力阻塞。人工工作是审查分支并自行 push；压力测试根因作为单独待排查项保留。旧 WebSocket token 的存储/备份问题保持既定范围，不宣称全系统凭据保护完成。
