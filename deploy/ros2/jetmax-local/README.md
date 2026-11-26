# JetMax 本机 ROS 2 部署模板（opt-in，示例）

本目录是**示例模板**，仓库不会自动安装、不会自动启用，也不写死任何现场 IP、主机名、
管理凭据或 Wi-Fi 信息。把 `<PLACEHOLDER>` 换成现场值之后再启用；不换就启用等于把占位符
当成配置。

## 这套模板证明了什么、没证明什么

2026-10-30 的 JetMax 现场记录：EX、camera、YOLO 入口统一 `rmw_cyclonedds_cpp`、
Domain 0、`ROS_LOCALHOST_ONLY=1`，并在补齐回环组播后真实链路通过。

- **证明**：这个组合在这台机器上跑通过，可以保存成"JetMax 本机已验证配置"。
- **不证明**：Cyclone 是普遍必需、Fast DDS 存在内部缺陷、或回环组播是唯一根因。
  同一天的记录里，只开组播时真实 `run-yolo` 仍然失败。
- **不适用于跨机器**：`ROS_LOCALHOST_ONLY=1` 把通信限制在同一网络命名空间。
  跨机器部署要另立方案（显式选网、发现/防火墙策略、可路由网络）。

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `profile.env.example` | 环境变量模板：Domain / localhost-only / RMW |
| `astrex-loopback-multicast.service` | 开机把 `lo` 的 MULTICAST 标志打开（oneshot） |
| `20-loopback-multicast.conf.example` | EX 服务的 drop-in：排在 oneshot 之后 |
| `30-rmw-cyclonedds.conf.example` | EX 服务的 drop-in：RMW/Domain/localhost-only |

## 依赖

| 层 | 要求 |
| --- | --- |
| 系统 | Ubuntu 22.04 + ROS 2 Humble（本机基线） |
| RMW 包 | 目标发行版对应的 `rmw_cyclonedds_cpp`（如 `ros-humble-rmw-cyclonedds-cpp`） |
| 工具 | `ip`（`iproute2`）。先确认路径：`command -v ip` 可能是 `/usr/sbin/ip` 或 `/sbin/ip` |
| 权限 | 安装 systemd 单元与 `ip link` 需要 root |

EX **不会**替你 `apt install`。缺少 RMW 包时预检报告 `rmw_effective` 未知，不会假装已生效。

## 安装（逐条显式执行）

```sh
# 0. 记录现场，便于回退
sudo systemctl cat <EX_SERVICE_NAME>.service > /root/astrex-before.txt
ip link show lo > /root/astrex-lo-before.txt

# 1. 环境变量：放到 EX 的 EnvironmentFile
sudo install -d /etc/astrex
sudo cp profile.env.example /etc/astrex/ros2.env
sudoedit /etc/astrex/ros2.env          # 改 <PLACEHOLDER>，确认 Domain/RMW

# 2. 回环组播 oneshot
sudo cp astrex-loopback-multicast.service /etc/systemd/system/
sudoedit /etc/systemd/system/astrex-loopback-multicast.service   # 填 <IP_PATH> 和 <EX_SERVICE_NAME>

# 3. EX 的 drop-in
sudo install -d /etc/systemd/system/<EX_SERVICE_NAME>.service.d
sudo cp 20-loopback-multicast.conf.example \
        /etc/systemd/system/<EX_SERVICE_NAME>.service.d/20-loopback-multicast.conf
sudo cp 30-rmw-cyclonedds.conf.example \
        /etc/systemd/system/<EX_SERVICE_NAME>.service.d/30-rmw-cyclonedds.conf

# 4. 生效
sudo systemctl daemon-reload
sudo systemctl enable --now astrex-loopback-multicast.service
ip link show lo | grep -o MULTICAST        # 必须输出 MULTICAST
```

生产者（`run-camera`、`run-yolo`）必须继承**同一组**环境变量。它们若是独立 unit，
把 `30-rmw-cyclonedds.conf` 的三行 `Environment=` 复制进它们的 drop-in；
否则就是典型的"只配了一半"。

## 启动与验证

```sh
sudo systemctl restart <EX_SERVICE_NAME>.service
sudo systemctl restart <CAMERA_SERVICE_NAME>.service
sudo systemctl restart <YOLO_SERVICE_NAME>.service

# 进程内实际值（不是配置文件的愿望值）
tr '\0' '\n' < /proc/$(pgrep -f astrbot_ex.core.api_server | head -1)/environ \
  | grep -E '^(RMW_IMPLEMENTATION|ROS_DOMAIN_ID|ROS_LOCALHOST_ONLY)='
```

然后打开 EX 的环境诊断，确认：

- `rmw_requested` 与 `rmw_effective` 一致（不一致时预检报 `ROS_RMW_MISMATCH`）；
- `loopback_multicast` 检查项为 pass；
- 端口 `data_health` 从 `warming_up` 走到 `receiving`。

`requested` 与 `effective` 只差一个值就说明有进程没重启——RMW 是启动前选择的，
网页上切换不会生效，也不会被诊断说成已生效。

## 回退

```sh
sudo rm -f /etc/systemd/system/<EX_SERVICE_NAME>.service.d/30-rmw-cyclonedds.conf
sudo rm -f /etc/systemd/system/<EX_SERVICE_NAME>.service.d/20-loopback-multicast.conf
sudo systemctl disable --now astrex-loopback-multicast.service
sudo rm -f /etc/systemd/system/astrex-loopback-multicast.service
sudo systemctl daemon-reload
sudo systemctl restart <EX_SERVICE_NAME>.service
```

- 只删本模板创建的文件，逐条列路径，不给递归清空目录的命令。
- 保留已安装的 `rmw_cyclonedds_cpp` 软件包（不卸载系统包）。
- **回退 RMW 可能回到已知会失败的配置**。回退后要重跑一次验收，并在报告里写明
  "回退后通信状态未知/已知失败"，不能把回退说成修复。
- 回退前先停视觉会话并确认 runtime idle、动作 gate 关闭。

## 与验收脚本的关系

`scripts/verify_ros2_diagnostics.py` 的 `synthetic` 场景**不依赖**本模板：
它用隔离 Domain 73 和唯一 topic，自己起图像 publisher，不需要动现场配置。
`entry` 场景要求 Domain 与实际 EX 配置一致（JetMax 现场为 0），用于真实入口复测。

本目录不提供任何"一键启用"脚本：启用、重启、回退都需要现场确认，属于显式操作。
