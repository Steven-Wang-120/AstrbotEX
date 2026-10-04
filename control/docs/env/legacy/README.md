# Legacy Isaac environments (HISTORICAL SNAPSHOT)

本目录的文件只是 **HISTORICAL SNAPSHOT**，**不代表当前开发环境**。

当前 AstrEX Isaac 基线是 `isaaclab232_test` / Isaac Sim 5.1 / Isaac Lab v2.3.2，规范见
[`docs/ISAAC_51_DEV_BASELINE.md`](../../ISAAC_51_DEV_BASELINE.md)；当前环境的快照在
`docs/env/current/`（本机历史引用：`../current/`）。

`isaaclab60_final_freeze.txt`、`isaaclab60_6010test_final_freeze.txt`、
`*_final_conda_export.yml` 是删除环境前导出的**纯 package snapshot**，未加任何注释文字。

## 被移除的两个环境（2026-09-17 删除）

| 项目 | `isaaclab60` | `isaaclab60_6010test` |
|---|---|---|
| Python | 3.12.13 | 3.12.14 |
| Isaac Sim | 6.0.0.1 | 6.0.1.0 |
| Isaac Lab | v3.0.0-beta2.patch1 | v3.0.0-beta2.patch1 |
| Lab commit | `ffff603eafc6b74264a5261cc0183d6a65390d78` | `ffff603eafc6b74264a5261cc0183d6a65390d78` |
| Torch / torchvision / torchaudio | 2.10.0+cu128 / 0.25.0+cu128 / 2.11.0 | 2.11.0+cu128 / 0.26.0+cu128 / 2.11.0+cu128 |
| RSL-RL | 5.0.1 | 5.0.1 |
| 当时的 Lab 源码 | `/home/sssxy/Projects/IsaacLab`（工作区 clean） | `/home/sssxy/Projects/isaac6010_validation_peSDykmW/IsaacLab`（工作区 clean） |
| package snapshot | [`isaaclab60_final_freeze.txt`](isaaclab60_final_freeze.txt)、[`isaaclab60_final_conda_export.yml`](isaaclab60_final_conda_export.yml) | [`isaaclab60_6010test_final_freeze.txt`](isaaclab60_6010test_final_freeze.txt)、[`isaaclab60_6010test_final_conda_export.yml`](isaaclab60_6010test_final_conda_export.yml) |

历史验证期间曾导出过一对同类快照（原 `docs/isaaclab60_working_freeze.txt`、
`docs/isaaclab60_working_environment.yml`）。固化时核对发现：pip freeze 与这里的
`isaaclab60_final_freeze.txt` **SHA256 完全一致**（`a5e65288c7072de18278811eb495639ba2dad06f281267656ba2e4d8e51f5bd7`），
conda export 的包集合与 278 条 pip 记录也完全一致，即两个时间点描述的是同一环境状态。
因此按"不保留两套重复 legacy snapshot"处理：只保留本目录的 final 快照，原两份根目录副本已删除，
[`docs/ISAAC_HISTORICAL.md`](../../ISAAC_HISTORICAL.md) 的链接已迁移到本目录对应文件。

## 删除原因与恢复方式

两个环境是 LEGACY / MIGRATION_ONLY，不再是 AstrEX 默认环境；`isaaclab60` 的 editable install 指向旧
Lab 源码、`isaaclab60_6010test` 指向隔离验证克隆，删除环境后两处源码均无引用。删除前已保存上述纯
package snapshot 与版本信息，最终测试结论见
[`docs/ISAAC_HISTORICAL.md`](../../ISAAC_HISTORICAL.md) 的 §3/§4（四份早期报告的合并记录与结论）。

如需恢复：按上表重新安装 Isaac Sim 6.0.0.1 / 6.0.1.0 与 Isaac Lab v3.0.0-beta2.patch1
（commit `ffff603eafc6b74264a5261cc0183d6a65390d78`），再按对应 `*_final_freeze.txt` /
`*_final_conda_export.yml` 重建 Python 依赖。

历史验证的原始大数据（旧 A/B 工作目录、早期隔离验证、早期失败的 ROS run 等）已归档到：

```
/data/shared/AstrEX_project_data/archives/isaac/2026-09-baseline-consolidation/
```

原路径与现路径的映射（含保留在原处与被删除的路径）见该目录的 `MANIFEST.json`。
