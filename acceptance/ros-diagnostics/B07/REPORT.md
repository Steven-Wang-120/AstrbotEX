# B07 交接报告：可选部署配置与无观察节点原生验收

## 环境与基线

| 项 | 值 |
| --- | --- |
| 仓库 | `D:\Code\AstrBotEX`，工作树 `D:\Code\AstrBotEX-agents\B07-09` |
| 分支 | `feat/ros-diag-b07-b09-20261031` |
| 基线 commit | `c1a4b89f04b4923611af524f4eb622e6fd011757`（= `main`） |
| 基线漂移 | 无。C02 列出的 9 个文件 SHA256 前缀全部一致 |
| 操作系统 | Windows 11（MINGW64_NT-10.0-26200），x86_64 |
| Python | 3.13.14 |
| ROS 2 | **未安装**（`import rclpy` → `ModuleNotFoundError`） |
| RMW | 不适用（本机无 ROS） |

本机无 ROS，因此本报告只能产出离线结论。原生结论全部为 `NOT_RUN`，
不得据此宣称 ROS 通过。

## 交付物

| 路径 | 说明 |
| --- | --- |
| `deploy/ros2/jetmax-local/profile.env.example` | Domain 0 / localhost-only / Cyclone 三行 env 模板 |
| `deploy/ros2/jetmax-local/astrex-loopback-multicast.service` | lo 组播 oneshot 单元 |
| `deploy/ros2/jetmax-local/20-loopback-multicast.conf.example` | EX 的 `Wants/After` drop-in |
| `deploy/ros2/jetmax-local/30-rmw-cyclonedds.conf.example` | 三个 `Environment=` drop-in |
| `deploy/ros2/jetmax-local/README.md` | 安装、依赖检查、验证、回退 |
| `scripts/verify_ros2_diagnostics.py` | 无观察节点验收脚本（约 600 行） |
| `tests/fixtures/ros_diagnostics/periodic_image/plugin.json` | `diag_image_probe` 清单 |
| `tests/fixtures/ros_diagnostics/periodic_image/main.py` | 只订阅并计数的 trace_plugin |
| `docs/ROS2-SDK.md` | 新增周期声明、`data_health`、快照/预检/事件、结果等级 |
| `docs/ROS2-DEPLOYMENT.md` | 新增预检、opt-in profile、RMW 重启、模板回退 |
| `docs/ROS2-ACCEPTANCE.md` | 新增无观察节点验收、四级结果、本轮结果表 |
| `.gitattributes` | 新增 `*.service` / `*.conf.example` / `*.env.example` 强制 LF |

## 实际执行的命令与退出码

全部在 `D:\Code\AstrBotEX-agents\B07-09` 下执行。

### 1. help

```text
python scripts/verify_ros2_diagnostics.py --help
```

退出码 **0**。usage 行包含全部冻结参数：
`--url --token-file --scenario{ synthetic,entry } --domain --duration --repeat --output --entry --with-observer --dry-run`。

### 2. dry-run（任务书指定命令）

```text
python scripts/verify_ros2_diagnostics.py --scenario synthetic --domain 73 \
  --duration 30 --repeat 2 --dry-run \
  --output acceptance/ros-diagnostics/B07/dry-run
```

退出码 **0**。证据：`acceptance/ros-diagnostics/B07/dry-run/plan.json`（脚本自身产出；
脚本同时把同一份 JSON 打到 stdout，因此不另存重复副本）。

输出关键字段：

```json
{"schema":"astrex.ros2_diagnostics_acceptance.v1","dry_run":true,
 "scenario":"synthetic","domain":73,"duration_sec":30,"repeat":2,
 "reads_token":false,"imports_ros":false,"changes":[]}
```

`changes` 为空数组，`reads_token`/`imports_ros` 均为 `false`：dry-run 不读 token、
不写配置、不改网络、不 import ROS。已核对脚本模块级 import 只有
`argparse/base64/json/os/sys/threading/time/datetime/pathlib/urllib` 等标准库；
`rclpy` 与 `sensor_msgs` 的 import 全部在函数体内（`_make_publisher` 等），
dry-run 路径不会触达。

dry-run 唯一写入是 `--output` 指定的验收产物目录，属于证据而非配置。

### 3. 参数拒绝（全部退出码 **2**）

| 输入 | 错误信息 |
| --- | --- |
| `--duration 10` | `--duration must be >= 30 seconds` |
| `--repeat 1` | `--repeat must be >= 2 rounds` |
| `--scenario entry`（无 `--entry`） | `--scenario entry requires --entry` |
| `--url http://192.168.1.50:8765` | `--url must point at a loopback host (127.0.0.1/localhost/::1)` |
| `--domain 0`（synthetic） | `--scenario synthetic must not use domain 0: that is the live JetMax site domain, not a test domain` |
| `--url http://user:pw@127.0.0.1:8765` | `--url must not carry credentials` |
| `--entry ./run.sh` | `--entry must be an absolute path` |
| `--entry /nonexistent/run.sh` | `--entry does not exist or is not a regular file` |
| `--scenario synthetic --entry /bin/sh` | `--entry is only valid with --scenario entry` |

退出码约定：`0 PASS`、`1 FAIL`、`2 用法错误`、`3 NOT_RUN/BLOCKED`。

### 4. 模板行尾与占位符

- 五个模板文件全部为 **LF**，无 CR、无 BOM（逐字节核对 `CRLF=0 CR=0`）。
  初版由编辑器写成 CRLF，已修正；CRLF 会让 systemd 报 `Exec format error`、
  让被 source 的 env 值带进不可见字符。
- 占位符只出现 `<PLACEHOLDER> <EX_SERVICE_NAME> <CAMERA_SERVICE_NAME> <YOLO_SERVICE_NAME> <IP_PATH>`。
- 私有数据扫描（`100.* / 192.168.* / 10.0.* / orangepi / /home/*`）无命中。
- `systemd-analyze verify` **未执行**：本机无 systemd，且 Windows 上验证 systemd 单元
  的结论无意义。该步骤留给目标 Linux，状态 `NOT_RUN`。
- 脚本写 `plan.json` / `report.json` / 时间线时显式使用 `newline="\n"`，
  避免在 Windows 上产出的证据文件带 CRLF。修正前首次 dry-run 的 `plan.json` 是 CRLF，
  已重新生成并核对 `CR=0`。

## 验收矩阵

| 项目 | 环境 | 结果 |
| --- | --- | --- |
| `--help` | Windows，无 ROS | **PASS** |
| dry-run 不读 token / 不写配置 / 不 import ROS | Windows，无 ROS | **PASS** |
| 参数拒绝（9 例） | Windows，无 ROS | **PASS** |
| 模板 LF / 占位符 / 无私有数据 | Windows | **PASS** |
| `systemd-analyze verify` 替换后单元 | 需目标 Linux | **NOT_RUN** |
| 原生 synthetic 两轮，Domain 73 | 需 Linux + Humble + 专用实例 | **NOT_RUN** |
| 原生 entry 两轮，Domain 0，现场 `run-yolo` | 需现场设备 + 模型环境 | **NOT_RUN** |
| 真实视觉识别精度 | — | 不在本期范围 |

**结论：模块与脚本验收通过；原生验收未完成。**

## 未跑项与解除条件

| 未跑项 | 解除条件 |
| --- | --- |
| 原生 synthetic 两轮 | Linux + ROS 2 Humble + `rclpy`/`sensor_msgs` + `rmw_cyclonedds_cpp`；一个专用 EX 实例（normal / idle / 动作 gate 关闭 / 其它插件禁用）；`--token-file` 指向真实管理凭据 |
| 原生 entry 两轮 | 同上，另需现场 `run-yolo` 入口可执行文件与相机/模型环境；Domain 用现场值 0 |
| `systemd-analyze verify` | 目标 Linux，替换占位符后执行 |
| 无旁路前置条件证明 | 隔离实例中确认无 ROS daemon、无他人 subscriber；共享环境无法排除时标“条件未满足”，不停他人节点 |

B08 落地 `manager/api_server` 的诊断 GET 与 refresh 之前，脚本的实际轮次也无法跑通；
原生联调排期在 B08 之后，与 C03 的依赖顺序一致。

## 与旧脚本的边界

`scripts/verify_ros2_deployment.py` 保留未改。它自己建立外部 subscriber，
属于独立 roundtrip 场景，**不能**当作无观察节点测试复用。
三份 ROS 文档中“它单独证明真实入口成功”的暗示已改写为明确的拓扑差异说明。

## 已知风险

1. **本机无 ROS**，`verify_ros2_diagnostics.py` 的 ROS 分支（`_make_publisher`、
   `_synthetic_round` 的时间线）从未被真实执行过。语法与参数层已验证，
   运行期正确性依赖首次原生执行。
2. **Domain 0 与 73 的区别**：73 只用于 synthetic 隔离测试；entry 场景必须用现场 0。
   用 73 的成功替代现场 0 是错误结论，脚本不提供这种替代路径。
3. **RMW 回退可能回到已知失败配置**：该现场退回 Fast DDS 等于退回 2026-10-30 失败的组合，
   回退文档已写明，不能把“回退成功”说成“恢复可用”。
4. 模板是示例，`<IP_PATH>` 需按现场 `command -v ip` 的实际路径替换；
   未替换直接启用会失败（这是有意的，避免静默失效）。

## 脱敏说明

本报告不含管理凭据、token 内容、私有数据库或现场 IP。
`--token-file` 只记录路径角色（“真实管理凭据”），不记录实际路径与值。
