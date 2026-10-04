# CartPole 完整退休归档

日期：2026-10-03。来源工作区：`/home/sssxy/Projects/AstrEX_project_main`。

本目录不参与正常构建、启动或实验发现。根目录含 COLCON_IGNORE。所有文件以清理时工作区版本保存，包含已有修改，不用 Git HEAD 覆盖工作区内容。

## 内容与校验

- `retired_source/`：按原相对路径保存两个 ROS 包、场景、控制、服务、入口、专用测试和工作空间日志。
- `retired_cache/`：三个旧启动器／CartPole 场景的字节码缓存，仅作历史快照，不恢复使用。
- `before/`：保留共享工具、配置和文档的清理前版本。
- `runs/ros/`：由 manifest 确認归属的六个非基线 CartPole 运行目录。
- `MANIFEST.json`：每项 source、destination、operation、kind、size 和 SHA256；符号链接的 SHA256 对链接文本计算，不跟随链接。
- `protection/`：清理前 HEAD、状态、文件哈希和暂存区原始副本。它们用于审计，不应直接覆盖当前 Git index。
- `checks/`：归档、删除、环境检查和最终保护核对结果。

`delete_generated` 项仅留清单，不保留 build/install 二进制副本。目录项没有内容 SHA256；其文件和符号链接逐项记录。原始日志不改写，因此其中旧绝对路径应通过 MANIFEST 映射查找。

## 保持原位

旧历史归档、最终服务证据、cutover 证据和 2026-09-17 基线 ROS/RL 运行未搬迁，位置见 MANIFEST 的 retained。运行锁、第三方环境、模型和其他项目文件不清理。

## 显式恢复

1. 先读取 `protection/before.json` 的 HEAD、状态及本目录环境配置快照，确认需要恢复的具体版本。
2. 根据 MANIFEST 的 source/destination 选择恢复集合，并校验文件 SHA256。目的路径已有内容时停止比较，不覆盖后续开发。
3. 恢复完整 ROS 包、配套场景/入口及必要的 `before/` 共享启动器。部分恢复不能视为可运行；所有目录保持原相对路径。不要整体还原 README 或后续 MVP 文档。
4. 若只恢复 MoveCart，明确从恢复的接口包移除未完成 SpinPole 注册及源码。恢复原始实验快照时可以保留原样，但不将其标为已验证功能。
5. 在保留的系统 Jazzy 环境重新构建需要的包。不要恢复 build/install 或归档中的缓存符号链接。重建后在新的 shell 中加载 overlay。
6. 恢复操作不自动启动仿真、训练、ROS 节点或旧命令。需要运行时另行明确启动与验收范围。

共享环境仍是 Isaac Sim 5.1.0.0、Isaac Lab v2.3.2、Python 3.11 与 ROS 2 Jazzy；完整配置见 `before/config/isaac_baseline.env`。本轮不 commit、不 push。

## 本轮验收限制

归档和清理检查通过；完整 Isaac 环境健康检查未通过，停在 Torch 原生扩展导入。原命令 120 秒超时，临时诊断 95 秒超时；根因未确认。详见 checks 中的 isaac_health_initial.json、isaac_health_diagnostic.txt 和 isaac_health_diagnostic.json。
