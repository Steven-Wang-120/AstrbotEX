# CartPole 历史归档

归档时间：2026-10-01，Asia/Shanghai。任务开始 HEAD：`c90321b`。

本目录保存历史中间产物、成功及失败实验、临时工具和退休入口。没有删除实验数据。正式服务完整成功证据在：

```text
/data/shared/AstrEX_project_data/logs/isaac/cartpole_service_final/20261001T161758+0800_2b96da8862/
```

## 清单

- `manifest.json`：移动前的原路径、新路径、归属证据、原因及每个文件的大小和 SHA256。
- `remainder_manifest.json`：成功证据提取后，旧服务验证目录的剩余内容。
- `git_reports_manifest.json`：三份用户已移除的 Step 3 报告，来自原 HEAD。副本未恢复到工作区。
- `supplemental_manifest.json`：定向构建后遗留的旧安装别名，以及本次归档工具。
- `late_cache_manifest.json`：公共 ROS 包的剩余专用 Python 缓存。
- `move_journal.jsonl`：已完成且核验一致的移动记录。
- `protection/`：原暂存区副本、用户文件和暂存条目指纹，以及环境和受保护源码快照。

目录节点也列入清单。symlink 的 SHA256 对象是链接文本，不是链接目标。归档中的旧构建 symlink 不保证在新位置可直接运行。

原 JSON、日志、报告内的路径和结论没有重写。要查找旧路径，先在 `manifest.json` 中查对应 `source`，再将相对后缀加到 `destination`。

## 恢复方法

恢复前先核对目标文件的 SHA256。确认原路径不存在，也没有新工作覆盖该位置。不要直接覆盖活动源码或运行产物。

单项恢复时，创建原路径的父目录，再对明确的一个路径执行：

```text
mv --no-clobber -T <清单中的 destination> <清单中的 source>
```

执行后再核对内容。恢复多个条目时逐项处理。不要使用批量删除、全仓 reset 或未解析的通配符。

恢复原 `cartpole_service_validation/20261001_service` 时，先恢复 `remainder_manifest.json` 所列目录，再恢复 `manifest.json` 中抽出的最终验证子项。这样可以先建立目录，再填回子项。恢复其余条目时遵守不覆盖规则。

退休源码在 `retired_source/`。重新启用旧入口还需要有意恢复包入口配置并定向构建；只搬回文件不会自动启用入口。三份 Step 3 报告也可从记录的 Git 提交恢复，但本次保留用户删除决定。

没有归档无法确认归属的 ROS 目录，没有整体清理 build/install/log，也没有处理 AstrBotEX 临时探针和运行锁。

归档工具位于 `maintenance/astrex_cartpole_archive_20261001.py`。在系统 Python 下运行该工具的 `verify --archive <本目录>` 可复核主清单和补充清单。最终证据 `checks/archive_closeout_check.py` 同时核对剩余缓存清单、生产入口、环境保护和提交候选。
