# JetMax ROS 诊断与日志验收

日期：2026-11-03。结果：**PASS**。

验收源码：`3060ef90d5433299555c44c9e075ed5cec53a4f0`。
这包含诊断集成分支 `86ce391` 及本次发现的验收脚本修复。
测试启动早于修复提交落盘；[source-identity.json](source-identity.json) 记录文件哈希，
已与机械臂实测源码逐项核对。最终汇总见 [summary.json](summary.json)。

## 验证范围与结果

| 项目 | 结果 |
| --- | --- |
| Windows Python 3.12 全套 unittest | 968 项：967 通过、1 项因环境跳过 |
| JetMax Ubuntu / Python 3.10 / Humble 全套 unittest | **968 项全部通过，无跳过** |
| B09 视觉插件 `d803bb4` 单元测试 | 48 项通过（健康信号 39 + marks 9） |
| 日志组件真实 Chromium 验收 | 40 项通过 |
| 环境诊断组件真实 Chromium 验收 | 64 项通过 |
| Dashboard 整页、鉴权、SSE 回放/重连联调 | 23 项通过 |
| 机械臂真实 EX 网页、相机回调、YOLO 就绪日志 | 13 项通过 |
| 原生无旁路观察者，Domain 73 | 独立 2 轮，每轮 ≥30 秒，全部通过 |
| 独立旁路对照，Domain 74 | 独立 2 轮，每轮 ≥30 秒，全部通过 |
| 现有相机 / YOLO 入口，Domain 0 | 独立 2 轮，每轮 ≥30 秒，全部通过 |

原生全套 unittest 的临时数据使用 `TMPDIR=/dev/shm`。
真实 EX、模型、插件状态和图像链路仍在机械臂原有磁盘与软件环境运行；
该测试配置不构成 eMMC 实时吞吐性能结论。

## 原生计数与拓扑

| 场景 / 轮次 | 时长（秒） | EX 新增接收 | EX 新增消费 |
| --- | ---: | ---: | ---: |
| 无旁路 / 1 | 30.12 | 117 | 117 |
| 无旁路 / 2 | 30.18 | 117 | 117 |
| 旁路对照 / 1 | 30.07 | 114 | 114 |
| 旁路对照 / 2 | 30.20 | 115 | 115 |
| 真实相机 / YOLO / 1 | 30.48 | 26 | 26 |
| 真实相机 / YOLO / 2 | 30.49 | 26 | 26 |

无旁路与对照测试均验证：可见发布者但尚无数据 → 接收 → 停发后 stale → 再次接收。
SSE 每种 synthetic 场景各记录 4 次恢复事件（每轮 2 次），两轮均使用独立环境、绑定及发布者。

EX 自身图 API 证实：无旁路时为 **1 publisher / 1 subscriber**；
对照时为 **1 publisher / 2 subscribers**。
真实相机话题的 110 个有效拓扑样本均为 **1 publisher / 1 subscriber**。
未启动 `ros2 topic list/echo/hz`、ROS daemon 或额外的 rclpy 观察者。
对照场景的额外订阅者只存在于明确标记的独立轮次。

真实相机每轮入口日志记录发布 29 帧；报告采样窗口内 EX 新增接收、消费各 26 帧。
采样窗口结束后入口还有退出等待时间，两种计数口径分别保留，不以发布数替代 EX 接收数。
两轮均记录 `YOLO_INFERENCE_READY`，合计 2 个真实视觉就绪事件。

## 复用现有视觉链路

沿用设备已有的 `/home/jetmax/astrex/bin/run-yolo` 入口逻辑、
`run-camera`、USB 相机 `/dev/video0`、已部署推理 Python 环境和现场模型配置。
临时入口仅调整管理端口（8765 → 18765）及管理凭据文件路径；
[entry-adaptation.json](device/entry-adaptation.json) 记录原入口哈希和差异。

待合并 EX 与 B09 视觉诊断插件加载于临时验收目录。
真实入口测试期间，原 EX 暂切普通环境并停用原 YOLO，避免额外订阅者。
完成后恢复原 EX 的 ROS2 模式、Domain、插件启用状态，runtime 保持 idle、动作 gate 关闭。
原 EX 服务 PID 始终为 3542，未替换现有服务源码、模型环境或 systemd 配置。

伴随视觉插件为独立仓库 `astrex-yolo` 的 `d803bb4`；
本次用于临时兼容性验收，该仓库的合并和永久部署不在本次 EX 合并内。
验收未驱动机械臂关节。

## 实测发现与修正

1. **同名端口导致计数误归属。** 停用插件仍可能保留已加载的 ROS 端口及累计计数；
   探针与 YOLO 都有 `image` 端口。原脚本选中第一个同名端口，错误使用探针的历史计数，
   将正常收帧的真实视觉链路判为失败。修复后按 `plugin_id` 取端口、健康状态及策略，
   并用“高历史计数探针 + 新增 YOLO 帧”补回归。修复后的全部原生场景已重跑通过。
2. **队列用例与短租约相互干扰。** 原队列饱和/超大包用例使用 900 ms 租约，
   在 JetMax 校验超大包时命令提前过期。仅给该用例设置独立的较长测试预算；
   队列容量、拒绝原因和执行顺序断言保留，专门的 watchdog/到期测试保持原样。
3. 最早的设备磁盘测试存在压力用例上下文超时；使用内存临时目录复现、隔离后，
   最终 968 项全部通过。初始中断还留下临时探针启用状态，已在后续入口验收前显式复位。

历史失败证据保留于 [device/history](device/history)，未将初始失败或跳过计为通过。

## 证据与复跑

- [机械臂全套结果](device/native-unit-final.json) / [原始日志](device/native-unit-final.log)
- [本地全套结果](local/unittest-full.json) / [原始日志](local/unittest-full.log)
- [无旁路报告](device/synthetic-final/report.json)、[独立旁路报告](device/synthetic-observer-final/report.json)
- [真实入口报告](device/entry-final/report.json)、入口日志、SSE 与 timeline 位于同目录
- [最终设备状态](device/final-device-state.json)：配置恢复、原服务 PID、相机与临时 EX 退出均通过
- [运行环境](device/runtime-environment.json)：Humble、Cyclone DDS、localhost-only、回环 MULTICAST
- 三份 `device/*-topology.jsonl.gz` 保存 EX 自身 API 的原始拓扑和诊断采样
- 四组 `ui-*/results.json` 与截图保存浏览器断言及实际画面

设备脚本保留在 [device/reproduce](device/reproduce)。
测试前需要专用临时目录、隔离 Domain、已安装相机和模型环境，以及关闭的动作 gate。
凭据仅通过私有文件读取，未写入本报告、结果文件或 Git。

原生单元测试命令：

```sh
source /opt/ros/humble/setup.bash
source /home/jetmax/astrex/runtime/ex-interfaces/install/local_setup.bash
cd <验收源码目录>
TMPDIR=/dev/shm <现有EX虚拟环境>/bin/python -m unittest discover -s tests -p 'test_*.py'
```

原生场景使用 `scripts/verify_ros2_diagnostics.py`；
synthetic 分别指定 Domain 73 与 74（后者加 `--with-observer`），
entry 指定 Domain 0 与临时适配后的现有入口。
全部轮次参数为 `--repeat 2 --duration 30`；凭据通过 `--token-file` 指定。
