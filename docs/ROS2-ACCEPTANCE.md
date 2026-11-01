# ROS 2 框架交付与验收记录

日期：2026-09-24。正式源码：`D:\Code\AstrBotEX`，基于 `1a9c190` 的未提交工作区。
保留此前用户修改，未创建 Git 提交。旧插件迁移按用户要求取消，只归档。

## 部署结果

- 唯一 EX Dashboard：`http://100.95.122.29:8765`，环境页 `/#/environments`。
- 香橙派 Ubuntu 22.04 / arm64 / Python 3.10 / ROS 2 Humble / Cyclone DDS。
- 镜像：`local/astrbotex:ros2-humble-20260924`；host 网络。
- Humble 基础镜像：`public.ecr.aws/docker/library/ros@sha256:1813d3c85d7f96ff7d3012d8652045832557440182db5d0065f8f8cd029a83138`。
- 部署文件：`/home/orangepi/astrbotex_deploy/compose.yml`。
- 新数据：`/home/orangepi/astrbotex_deploy/deploy/astrbotex-ros2/data`。
- 旧 EX 的主机 6185 测试进程已关闭；未在香橙派保留 18765 EX 服务。
- AstrBot、Napcat 的容器 ID 未改变，保持原 6185 / 6099 服务。
- 原 AstrBot 三条 ZMQ 连接保留；text/audio/vision 握手和查询均通过。
- 最终状态：normal、runtime 停止、`ros2_echo` 禁用、输出 binding 禁用。

## 完成内容

原生每插件 ROS ports、绑定 revision、owner/环境/绑定代际隔离、独立有界消息队列、
条数与字节限制、过期丢弃、runtime 输出 gate、有限期停止授权及失败处理；
异步环境切换、持久化和存档恢复、结构化 SSE；ROS graph、端点真实计数、
QoS 诊断、接口包与 typesupport 检查；环境页和插件 ROS 表单、草稿保护；
Humble/Jazzy 镜像、构建期自定义嵌套接口、SDK/API/部署文档和新示例。

实测修复了 Humble 不提供 subscription matched count、Python 3.10 Future 超时类型、
切换锁阻塞、旧绑定回调串入新队列、关闭授权过期，以及跨 RMW 默认 QoS 的误报。
Humble 无法报告订阅匹配数时显示 unknown，通过实际接收和 graph 判断就绪，不伪造数量。

## 实际执行的验收

| 环境 / 验收 | 结果 |
| --- | --- |
| Windows 无 ROS 全套 unittest | 131 项：126 通过，5 项真实 ROS 测试明确 skip |
| 香橙派原生 Humble（加入最后两个 HTTP 用例前） | 129 项通过，无 skip |
| 香橙派最终 Humble 镜像，Cyclone DDS，Domain 73 | 131 项全部通过，8.831 秒 |
| Jazzy / Python 3.12 / amd64 镜像 | 全套 131 项通过；最终 QoS 修复后 5 项原生 ROS 用例复验通过 |
| Windows Chromium 对香橙派实际 8765 | 导航、环境草稿、插件草稿、SSE 断线恢复、页面刷新通过；无 JS 异常、无意外 select |
| Linux 宿主机 ROS 节点 ↔ EX 容器的示例 actor | String 双向收发通过，发现及收发 0.153 秒，两端 ready / QoS compatible |
| 框架行为 | 晚启动发布者、双 owner、TopicBus 共存、QoS 冲突/恢复、自定义嵌套 Target、缺包隔离、20 次切换清理通过 |

宿主机节点和 Docker EX 是不同进程及文件系统；通过 Linux host 网络通信。
容器外验收使用 Domain 73 和唯一 Topic，完成后恢复原配置；未驱动真实硬件。

## 无观察节点原生验收

本节记录 2026-10-31 诊断与日志改造轮次（分支 `feat/ros-diag-b07-b09-20261031`）新增的验收方式；
上面 2026-09-24 的部署结果属于上一轮，两者数字不能相加或互相引用。

旧脚本 `verify_ros2_deployment.py` 自己建立外部 subscriber，验证的是两端互通，
属于独立 roundtrip 场景。它**不能**证明“EX 在不加旁路观察节点时能收到数据”：
加了订阅者就改变了被测拓扑，结论不可迁移。两者不能互相替代。

无观察节点场景只允许三类参与者：必要的图像生产者（synthetic 由脚本自建唯一 publisher，
entry 由现场入口拥有 camera/YOLO 进程）、EX 自身、HTTP/SSE 测试客户端。
禁止 `ros2 topic list/echo/hz`、临时 rclpy 旁路 subscriber、ROS daemon 辅助探测。
主要证据是 publisher 本地计数/日志和 EX 自身 callback 计数（`rx_received / rx_consumed`）。

时间线必须符合健康规则：publisher 可见但不发数据、越过启动宽限后出现
`ROS_RX_NO_DATA_WITH_PEER`（黄色）；连续至少 10 帧进入 EX callback 并被消费后转 `receiving` 并产生一次恢复；
停止发送越过 `stale_after_sec` 后转 `stale`（黄色）；恢复送帧后产生第二次恢复。
每轮至少 30 秒，至少独立重复 2 轮。

旁路观察者场景另起一轮，用 `--with-observer` 显式开启，结果单独记录，
**不能覆盖无观察节点轮次的失败**。两轮之间清理全部测试参与者。
共享环境里无法排除他人旁路时，把该前置条件标为“未满足”，不去停别人的生产节点或 daemon。

结果等级固定为四级，不得混用：

| 等级 | 含义 |
| --- | --- |
| PASS | 实际执行且全部条件成立 |
| FAIL | 执行过但任一条件不成立，或出现违反安全边界的行为 |
| NOT_RUN | 无环境或未执行，仅有设计；不计入通过数 |
| BLOCKED | 缺必要依赖/权限/基线，需写清解除条件 |

本轮结果：

| 项目 | 环境 | 结果 |
| --- | --- | --- |
| 脚本 `--help` 与参数拒绝（远程 url、`duration<30`、`repeat<2`、缺 `--entry`） | Windows，无 ROS | PASS |
| `--scenario synthetic --dry-run` | Windows，无 ROS | PASS（未读 token、未写配置、未 import ROS） |
| 原生 synthetic 两轮（Domain 73） | 需 Linux + Humble + 专用实例 | NOT_RUN |
| 原生 entry 两轮（Domain 0，现场 `run-yolo`） | 需现场设备与模型环境 | NOT_RUN |
| 真实视觉识别精度 | — | 不在本期范围 |

**当前只能写“模块与脚本验收通过，原生验收未完成”。** Windows/mock 上不可能得到原生 PASS：
没有 `rclpy` 与真实 DDS 时，单元测试通过不等于 ROS 通过。
脚本在没有原生条件时返回 `NOT_RUN`（退出码 3），不会把跳过伪装成通过。

原生执行后证据必须包含：EX 与 publisher 的 PID 及拓扑说明、requested/effective RMW、Domain、
接口标志、端口 generation、每轮输入计数、EX 的 `rx_received/rx_consumed`、诊断时间线、
SSE 事件 ID、停止后状态与清理结果。缺任一项不能判 PASS。

## 压力观测

香橙派最终镜像：2048 字节 String，输入约 1212.4 条/秒，发送 1527 条，
慢消费者每 20 ms 取一条。EX 接收 635 条，队列丢弃 573 条，消费 59 条；
其余消息可能在 DDS/测试结束边界，未将发送数冒充 EX 接收数。
每端口上限 4 条 / 131072 字节，观测队列峰值 8548 字节。
进程 RSS 从 61828 KiB 到 61828 KiB；队列停留 p95 9.980 ms，最大 11.785 ms；
发布仍持续时切回 normal 耗时 0.045 秒。

这些是短时无界积压防护的观测结果，不是实时调度或长时内存稳定性保证。
时延是框架接收后到消费者取出的时间，不含网络或物理执行延迟。

## 归档与回滚

- 本地完整旧插件：`D:\Code\_archives\EXplugin-legacy-20260924`。
- 香橙派旧测试插件及源代码备份：`/home/orangepi/astrbotex-test-archives/20260924-185147`。
- 原生产 EX 的旧数据仍在 `~/astrbotex_deploy/deploy/astrbotex/data`，新实例不加载它。
- 原生产配置、inspect、压缩数据：`~/astrbotex_deploy/backups/pre-ros2-20260924`。
- 原镜像 `local/astrbotex:aeb-0.5.0-arm64` 保留。

需要回滚时，先停当前 EX，将备份目录的 `compose.yml` 和 `.env` 恢复到部署根目录，
再执行 `docker compose up -d --no-deps --no-build astrbotex`。该操作恢复旧版本及旧数据；
按用户当前要求，日常运行不要恢复旧插件。

测试日志位于香橙派 `~/astrbotex-test/acceptance/`；浏览器截图及日志、用户原修改备份位于
`C:\Users\17088\AstrBotEX-work\ros2-completion-20260924`，这些是交付辅助文件。

## 重跑与边界

- 单元及真实 ROS：`python -m unittest discover -s tests -p 'test_*.py'`，须预加载对应 ROS 和自定义接口。
- 浏览器：`python scripts/verify_environment_ui.py --url http://100.95.122.29:8765`，需额外安装 Playwright/Chromium。
- 容器外：宿主机 source Humble 后运行 `scripts/verify_ros2_deployment.py`，仅用于普通模式、空闲、无其他启用插件的验收实例；它自带 subscriber，只证明两端互通，不证明无观察节点入口可用。
- 无观察节点：`scripts/verify_ros2_diagnostics.py`，`--dry-run` 可在 Windows 运行，原生场景需要 Linux + Humble + 专用实例。两轮结果分别写入 `acceptance/ros-diagnostics/B07/synthetic` 与 `entry`。
- 自定义接口在镜像构建期或固定 overlay 部署，重启 EX 后使用；网页不执行安装/编译。
- 未验证真实相机、雷达、CAN、麦克风、扬声器或机器人动作；这些旧插件已退出本期范围。
- 未宣称 ROS 发现可跨 Tailscale 多播；此次验证是同一 Linux 主机的容器外节点。
- 外部节点异常退出的 DDS 租约超时、长时间稳定性尚未作专项量测。
