# B01 验收报告：ROS 环境只读预检

## 结论

**模块验收通过（PASS），原生 ROS 验收未执行（NOT_RUN）。**

本报告只证明 `collect_preflight` 在假 sysfs / 假环境下按 C01 §4 契约产出报告，且没有任何副作用。
它**不**证明 Linux 原生 lo 组播、真实 rclpy 存在性或真实 RMW 实现——这些属于 B07/B10。

## 基线与环境

| 项目 | 值 |
| --- | --- |
| 仓库 | `D:\Code\AstrBotEX` |
| worktree | `D:\Code\AstrBotEX-work\ros-diag-b01-b03` |
| 分支 | `feat/ros-diag-b01-b03` |
| 基线 commit | `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 工作区状态 | dirty：仅新增文件（见下），无既有文件改动 |
| 操作系统 | Windows 11 10.0.26200 |
| Python | 3.13.14 |
| ROS / RMW | 无（`rclpy` 未安装，`ROS_DISTRO` 未设） |

基线漂移核对（`git cat-file -p HEAD:<path> | sha256sum` 与 C02 逐项比对，**全部一致**）：

| 文件 | C02 记录 | 实测 |
| --- | --- | --- |
| `astrbot_ex/core/event_bus.py` | `b4a5081…a4fa4ec` | 一致 |
| `astrbot_ex/core/environments/contracts.py` | `a941c09…ac70bf` | 一致 |
| `astrbot_ex/core/environments/plugin_api.py` | `65d5efc…42aad2` | 一致 |
| `astrbot_ex/core/environments/ros2/adapter.py` | `fc21bb2…38b6e6` | 一致 |

## 变更

| 操作 | 文件 | 行数 |
| --- | --- | --- |
| 新增 | `astrbot_ex/core/environments/ros2/preflight.py` | 468 |
| 新增 | `tests/test_ros2_preflight.py` | 414 |

未改动 `runtime.py`、`actions/`、`decision/`、`topic_bus.py`，未新增依赖，未改 `models.py`。

## 接口（C01 §4）

```python
collect_preflight(*, environ=None, platform_name=None,
                  interface_reader=None, runtime_rmw=None) -> dict
```

报告固定字段：`schema_version=1`、`platform`、`supported`、`checks[]`、`runtime`、`interfaces`、`partial`。
check 项：`{id,status,message,evidence,remediation}`，status ∈ `pass/warn/fail/unknown`。

`CHECK_IDS` 顺序冻结为 C01 §9 表格顺序：

```
rclpy_available, rosidl_available, loopback_multicast, rmw_consistency,
rmw_effective, overlay_paths, interface_probe, environment_values
```

常量：`SCHEMA_VERSION=1`、`MAX_INTERFACES=64`、`MAX_READ_BYTES=4096`、`MAX_OVERLAY_PATHS=64`、`MAX_TEXT=128`、
`SYS_CLASS_NET="/sys/class/net"`、`IFF_UP=0x1`、`IFF_LOOPBACK=0x8`、`IFF_MULTICAST=0x1000`。

## 只读与副作用

| 约束 | 实现 | 测试证据 |
| --- | --- | --- |
| 不导入 rclpy | 只用 `importlib.util.find_spec` | `DependencyProbeTests` |
| 不执行命令 | 无 `subprocess` / `os.system` | `SideEffectTests` 用 `mock.patch` 断言零调用 |
| 不写文件 | 只以 `r` 模式打开；测试断言无写模式 `open()` | `SideEffectTests` |
| 有界读取 | `_SysfsReader.read_text` 最多读 4096 字节 | `ReaderFailureTests` |
| 不越界路径 | `_valid_interface_name` 先校验再拼 sysfs 路径 | `InterfaceFlagTests` |
| 不回显秘密 | 报告不附全量 `environ`，非法值只报 check 描述 | `EnvironmentValueTests` |
| 无 root / 不改网卡 | 无 `ip link set`、无 `sudo` | 代码层无该能力 |

## 命令与结果

| 项 | 值 |
| --- | --- |
| 命令 | `python -m unittest discover -s tests -p "test_ros2_preflight.py"` |
| 时间 | 2026-10-31 20:38 (+08:00)，耗时 0.083 s |
| 退出码 | 0 |
| 结果 | `Ran 36 tests … OK` |

## 场景覆盖

| 场景 | 断言 |
| --- | --- |
| lo 有 IFF_MULTICAST | `loopback_multicast` = pass |
| lo 无 IFF_MULTICAST + Linux + 明确 localhost_only | `loopback_multicast` = warn（不阻断） |
| localhost_only 未设 / 非 0/1 | 不推断，check 为 unknown |
| flags 文件不可读 | `interfaces[].multicast = null`，`partial = true` |
| flags 文本为负数/垃圾 | 解析为 `None`，不猜 |
| 非 Linux 平台 | `supported=false`，接口探针仅 unknown |
| `rclpy` 缺失 | `rclpy_available` = fail → `ROS_DEPENDENCY_MISSING` 证据 |
| `rosidl` 缺失 | 与上项合并缺失依赖证据 |
| `runtime_rmw` 未注入 | `rmw_effective` 仅 unknown，不猜默认实现 |
| requested ≠ effective 且都可信 | `rmw_consistency` = warn |
| overlay 路径不存在 | `overlay_paths` 列出 missing，去重且 ≤64 项 |
| overlay 未配置 | 不假造缺失路径 |
| Domain 越界/非法 | `environment_values` 报 invalid_keys，不回显长原文 |
| 报告可序列化 | `json.dumps(report)` 成功 |
| 接口名含 `../` | 拒绝，不拼出 sysfs 路径 |

## 未执行（NOT_RUN）

| 项 | 原因 | 解除条件 |
| --- | --- | --- |
| Linux 原生 `/sys/class/net` 读取 | 当前为 Windows | B07 在 Linux 镜像内跑同一命令 |
| 真实 rclpy/rosidl 存在性 | 环境无 ROS | B07 |
| 真实 RMW requested/effective | 需活跃 ROS 进程，值由 B08 注入 | B07/B08 |

## 交接

- 报告 schema 与 `CHECK_IDS` 可直接被 B04 `evaluate_preflight(report)` 消费。
- `runtime.rmw_effective` 留空时 B08 不要填默认值；`supported` 只表示平台探针支持性，不代表 ROS 通信通过。
