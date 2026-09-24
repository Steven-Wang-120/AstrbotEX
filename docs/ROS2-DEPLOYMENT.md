# ROS 2 部署

本期只交付新框架和 `ros2_echo` 示例，旧插件已归档。
香橙派沿用原来的 EX 入口 `http://100.95.122.29:8765`，只升级三合一部署中的 EX 服务。
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

## 验证与恢复

```bash
source /opt/ros/humble/setup.bash
source ros_interfaces/install/setup.bash
ROS_DOMAIN_ID=73 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

真实 ROS 测试使用独立 Domain 73 和唯一 Topic 前缀。无 ROS 时明确 skip，不能视为真实 ROS 通过。
测试覆盖独立对端进程、双 owner、QoS 修复、自定义嵌套类型、缺包隔离、20 次切换和有界积压。

实例存档包含环境配置与插件绑定，不包含外部 ROS 二进制或镜像。
恢复后重新检查接口和资源，不恢复旧消息、旧节点或运行中的任务。
旧插件归档路径记录在验收文件中；恢复旧插件不属于本期工作。
