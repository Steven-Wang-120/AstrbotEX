# ROS 2 部署

本期只交付新框架和 `ros2_echo` 示例，旧插件已归档。
本分支加入 B08 后，管理 HTTP 只监听 `127.0.0.1:8765`。远程访问使用 `ssh -L 8765:127.0.0.1:8765 用户@主机`，再打开本机地址并输入管理凭据。原香橙派部署记录属于上游历史，不代表本轮已部署。
AstrBot 的 6185 和 Napcat 的 6099 保持原用途；`~/astrbotex-test` 留作源码及验收目录，不另开 EX 网页。

## 原生 Linux / 香橙派

基线为 Ubuntu 22.04、ROS 2 Humble、系统 Python 3.10。项目现明确支持 Python >=3.10；
使用与 ROS 二进制匹配的解释器，不能把 ROS 的 Python 库复制给另一版本解释器。

```bash
cd ~/astrbotex-test
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
source /opt/ros/humble/setup.bash
cd ros_interfaces
colcon build --merge-install
cd ..
bash scripts/run_ros2.sh
```

已有非 merge 的 colcon 工作区继续用其原构建布局，不要混用；更换布局应新建工作区。
普通模式只预加载搜索路径，不创建 ROS Context/节点。用户选择 ROS 2 后框架才创建资源。
保存成功的 ROS 模式会在下次启动或存档恢复后重新启用，但 runtime 任务不会自动启动。
原生进程和 Docker 是两种部署方式；同一设备使用其中一种，默认均为 8765。

## Docker

`Dockerfile.ros2` 默认 `ros:humble-ros-base-jammy`，可通过 `ROS_BASE_IMAGE` 选择官方 Jazzy/Noble 镜像。
发行版由基础镜像 `ROS_DISTRO` 决定；依赖、接口包和 entrypoint 同步使用这个值。
镜像创建系统包可见的 venv，并在构建期编译 `astrbotex_demo_interfaces`。

```bash
docker compose -f compose.yml -f compose.ros2.yml build astrbotex
docker compose -f compose.yml -f compose.ros2.yml up -d --no-deps astrbotex
```

生产部署在 `ASTRBOTEX_ROS_BASE_IMAGE` 中固定实际验证过的镜像 digest，记录目标 CPU 架构。
amd64 和 arm64 分别构建，不跨架构复制 `ros_interfaces/install`。
无 ROS 的 `Dockerfile` 保留普通模式的轻量部署路径。

可选的已有 Jazzy 官方镜像摘要（amd64 构建记录使用）：
`public.ecr.aws/docker/library/ros@sha256:c3706ef0a0aa45413c07803cf433602f543b22e45b4855f6fca955c2d8ecc4e8`。
详细验证结果见 [验收记录](ROS2-ACCEPTANCE.md)，摘要对应的架构以构建工具检查结果为准。

## 网络、配置与外部接口

机器人 Linux 使用 host 网络。`ROS_DOMAIN_ID`、网卡、防火墙、RMW 配置必须与对端一致。
Tailscale SSH 可用于管理；ROS multicast 发现是否能跨 VPN 是独立的部署问题，不能从 SSH 连通推断。

本次香橙派保留现有 AstrBot bridge 网络，EX 改用 host 网络，并通过 compose `extra_hosts`
把 `astrbot` 指向现有容器地址。三条 ZMQ 连接配置保留，不迁移旧插件。
若以后重建 AstrBot 导致 bridge IP 改变，应先用 `docker inspect astrbot` 获取新地址，
更新 `~/astrbotex_deploy/.env` 的 `ASTRBOT_CONTAINER_IP`，再仅重建 EX 服务。
当前 compose 位于 `~/astrbotex_deploy/compose.yml`；新数据在 `deploy/astrbotex-ros2/data`。

`ROS_DOMAIN_ID`、`ASTRBOTEX_ROS_NAMESPACE`、`ASTRBOTEX_ROS_NODE_NAME` 在设置时锁定对应字段，
UI 显示有效配置及锁定状态。`RMW_IMPLEMENTATION` 在启动前选择；变化后重启进程。

通过只读挂载固定 overlay，并设置 `ASTRBOTEX_ROS_OVERLAY=/opt/robot_overlay` 加载外部接口。
overlay 必须包括生成代码、typesupport 和嵌套依赖，匹配 CPU/Python/ROS ABI。
框架内置 Target 示例包含 `std_msgs/Header` 和 `geometry_msgs/Point`。
更新包后重启 EX；网页只检查，永不执行 apt、pip、colcon 或任意 setup。

## 部署前预检

环境页和 `GET /api/v1/ex/environments/diagnostics` 会带一份只读预检报告，检查项固定为：
`rclpy_available`、`rosidl_available`、`loopback_multicast`、`rmw_consistency`、
`rmw_effective`、`overlay_paths`、`interface_probe`、`environment_values`。

预检只读：不执行 `sudo`、不调用 `apt/pip/colcon`、不修改网卡、不为取 RMW 值而初始化 ROS。
`rmw_requested` 未设置和 `rmw_effective` 读不到时都显示未知，不会猜成默认实现。
`loopback_multicast` 只对 Linux + `ROS_LOCALHOST_ONLY=1` 且读不到组播标志时给建议，
已有静态单播/发现服务器的部署不能被判失败。`interface_probe` 在非支持平台只标 `unknown`。
预检缓存 30 秒；需要立即重查用 `POST /api/v1/ex/environments/diagnostics/refresh`，
它只重查诊断，不改环境选择。

## JetMax 本机可选配置（opt-in）

`deploy/ros2/jetmax-local/` 是**示例模板，不自动启用**，也不会被任何启动脚本自动加载。
它记录 2026-10-30 在该现场验证通过的一组本机通信配置：

```text
ROS_DOMAIN_ID=0
ROS_LOCALHOST_ONLY=1
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
+ lo 打开 MULTICAST
```

这组配置证明的是“在该主机上这套组合跑通了”，**不证明**所有设备都必须用 Cyclone，
也不证明 Fast DDS 有缺陷，更不证明 lo 标志是唯一根因。
跨机器部署、多播跨 VPN、使用静态单播或发现服务器的现场都不应套用本模板。

文件清单与作用见 `deploy/ros2/jetmax-local/README.md`：`profile.env.example`、
`astrex-loopback-multicast.service`（oneshot，`ip link set dev lo multicast on`）、
`20-loopback-multicast.conf.example`（EX 的 `Wants/After`）、
`30-rmw-cyclonedds.conf.example`（三个 `Environment=`）。
模板里的服务名、用户、`ip` 绝对路径都是 `<PLACEHOLDER>`，必须按现场替换；
`systemd-analyze verify` 只能在目标 Linux 上做，不能在 Windows 宣称单元已验证。

两个容易踩的坑：

- **RMW 必须两端一致。** 只改 EX 服务、不改 `run-camera` / `run-yolo`，
  得到的是半配置部署：两端各自用不同实现，发现和收发都会异常而现象看起来像“相机没数据”。
- **改 RMW 后必须重启进程。** `RMW_IMPLEMENTATION` 在进程启动时读取，
  运行中修改环境文件对已启动进程无效。要停止视觉会话、重启相关进程，
  再对比 requested 与 effective 两个值是否真的一致。

模板使用 `Wants=` 而不是 `Requires=`：`Requires` 会让 lo 组播设置失败时直接阻断 EX 启动，
把“诊断信号缺失”升级成“服务起不来”。oneshot 单元故意不加前导 `-`，
让失败可见，而不是吞掉错误后显示成功。

## 验证与恢复

```bash
source /opt/ros/humble/setup.bash
source ros_interfaces/install/setup.bash
.venv/bin/python scripts/verify_ros2_deployment.py \
  --url http://127.0.0.1:8765 --token-file /实际数据目录/secrets/admin.token
```

真实 ROS 测试使用独立 Domain 73 和唯一 Topic 前缀。无 ROS 时明确 skip，不能视为真实 ROS 通过。
本脚本只在独立、空闲、仅有 echo 示例的实例验证真实 DDS 往返，并恢复配置。更完整的上游系统测试已迁至独立验证仓库，不能把上述命令称为完整机器人验收。

**这个旧脚本会自己建立外部 subscriber。** 它验证的是“两端能互通”，
属于独立 roundtrip 场景；它不能证明“EX 在不加旁路观察节点时能收到数据”，
两者的拓扑不同，不能互相替代。

无观察节点的入口验收用 `scripts/verify_ros2_diagnostics.py`：

```bash
# 只描述动作，不读 token、不写配置、不 import ROS
python3 scripts/verify_ros2_diagnostics.py --scenario synthetic --domain 73 \
  --duration 30 --repeat 2 --dry-run --output acceptance/ros-diagnostics/B07/dry-run

# 原生 synthetic：脚本自己建唯一 publisher，不建 subscriber
python3 scripts/verify_ros2_diagnostics.py --url http://127.0.0.1:8765 \
  --token-file /private/ex-management-token --scenario synthetic --domain 73 \
  --duration 30 --repeat 2 --output acceptance/ros-diagnostics/B07/synthetic

# 原生 entry：调用现场真实 run-yolo 入口，Domain 用现场值
python3 scripts/verify_ros2_diagnostics.py --url http://127.0.0.1:8765 \
  --token-file /private/ex-management-token --scenario entry \
  --entry /home/jetmax/astrex/bin/run-yolo --domain 0 \
  --duration 30 --repeat 2 --output acceptance/ros-diagnostics/B07/entry
```

`--url` 只接受 loopback（`127.0.0.1 / localhost / ::1`）且不得携带凭据；
token 只从文件读取、只走 header，异常和日志都不回显。
`--duration` 必须 >= 30 秒，`--repeat` 必须 >= 2 轮；
`synthetic` 不允许使用 Domain 0（那是现场实际域，不是测试域）。
`--entry` 必须是绝对路径的已存在可执行文件，以参数数组调用，不做 shell 拼接；
真实入口失败时保留 stderr 与退出码，不能自动加 observer 把首轮判成 PASS。
`--with-observer` 只能在单独一轮显式使用，结果独立标记，不能覆盖失败的无观察节点轮次。

脚本用 `try/finally` 停止自己拥有的 publisher/入口、停 runtime、恢复原配置/绑定/插件状态；
每步恢复独立尝试并记录失败，不让第一个异常吞掉后续清理。
**不会自动重启原来运行的机器人任务**——前置检查已要求实例处于 idle。

实例存档包含环境配置与插件绑定，不包含外部 ROS 二进制或镜像。
恢复后重新检查接口和资源，不恢复旧消息、旧节点或运行中的任务。
旧插件归档路径记录在验收文件中；恢复旧插件不属于本期工作。

## 模板回退

启用模板前先备份：EX/插件 commit、服务与 drop-in 和环境文件内容的脱敏副本及其 SHA256、
当前选择的环境 / 配置 revision / 绑定 / 启用插件 / runtime 状态。
管理凭据和私有数据库不进入证据目录。

推荐顺序：停止视觉会话并确认 idle 且动作 gate 关闭 → 备份 → 放模板并显式启用 →
`daemon-reload` → 启动 EX → 预检 → 无观察节点真实入口测试 → 检查退出与残留。

回退按原文件逐个恢复，删除本次新增的 drop-in 后再 `daemon-reload` 并重启 EX。
保留既有软件包，不因回退卸载 `rmw_cyclonedds_cpp`。
**回退 RMW 可能回到已知会失败的配置**——如果本机正是 2026-10-30 那台设备，
退回 Fast DDS 等于退回当时失败的组合，报告必须写明这一点，不能把“回退成功”当成“恢复可用”。
删除只针对本任务创建且已核验绝对路径的文件，不提供递归清空部署目录的命令。
停止服务前要保证诊断线程退出；回滚失败保留证据，不伪报成功。

## 独立接口构建目录

`ASTRBOTEX_ROS_INTERFACES` 可指向独立构建得到的 install 目录。未设置时沿用 `ros_interfaces/install`。`ASTRBOTEX_ROS_OVERLAY` 仍用于额外机器人接口。两个路径都由启动环境提供，不接受 HTTP 输入。

本机 Jazzy 使用系统 Python 3.12 和 `--system-site-packages` 的 EX venv；加载 ROS setup 后保留其 `PYTHONPATH`。本轮只检查 Docker 配置和 shell 语法，未构建或部署镜像。Linux host 网络继续供 DDS 使用，HTTP 不因此开放到全部网卡。
