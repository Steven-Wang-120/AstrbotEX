# 完整 MVP 独立分支发布记录

本次发布目标为 `sssxy-Mr2p5pi/AstrbotEX` 的 `control/mobile-manipulation-mvp`，基线固定 `531b7c9a4feef93599a51f5565fe65e7a98bcc38`。原 PR #2、原开发目录和暂存区保持不变。

## 内容

P/L/A/R/E/T/M 的现有成果全部纳入：根目录的 Laya、任务/Contract、控制插件、探针和数据构建程序，以及 control/ 下的 Isaac、ROS、MoveIt、Nav2、Guard、统一运行器和全部相关测试。已有 B07/B08/B09 从基线继承；没有整包覆盖 EX。现有源文件与导出路径、源/目标 hash 及适配分类见 [source-export-manifest.json](evidence/publication/source-export-manifest.json)。

目录适配仅包含 EX 路径解析、开发 shell 的项目根定位、两个场景的 profile 相对路径和文档链接。`ASTREX_EX_ROOT` 可显式指定 EX；未设置时先支持原 apps/AstrBotEX 布局，再识别发布布局的仓库根。冻结 T1 v2 文件与标签审阅文档未改写。

原始数据和实验记录中的本机路径是历史 provenance，不是本分支的运行依赖定位方式。报告已用相对链接指向发布的小型证据；大型记录标为本机历史引用。模型权重、数据库、缓存、虚拟环境、第三方安装与 build/install 不随 Git 发布。旧数据审计保留，但只发布 v2 正式训练数据。

## 本次发布验证

| 检查 | 结果 |
|---|---|
| L1/T1、EX 控制插件、OwnedLaya、Laya backend/服务接线 | 77 项通过；HTTP fixture，不加载真实模型 |
| ROS Guard 与故障测试边界 | 26 项通过；不发送真实进程信号或运动 |
| 传感器、导航/E0 证据、目录迁移、ROS domain 逻辑 | 50 项通过；Jazzy 环境 |
| 导出控制端 ROS 模块导入、运行器 --help | 通过；实际导入路径均在本检出内 |
| Python AST / Shell 语法 | 通过 |
| T1 v2 和人工审阅原文 | 字节与 hash 一致；100 组，70/15/15；230 行；manifest 与唯一技能 schema 一致 |

日志见 [publication/](evidence/publication/)。首次上层测试因沙箱不能建立 loopback socket 失败；首次 ROS domain 测试因未加载 Jazzy 而缺少 rclpy。原日志保留。解决运行环境后复验通过，没有修改断言、跳过测试或加载机器人场景。

## 保留的开发缺口

P1 的物理传感器采集、L1 接口/探针、T1 数据属于既有第一阶段成果。原模型仍为 2/20，尚未微调或切换 multilingual。L3 自然语言到正式 Goal 的后续入口尚未完成；固定脚本与自然语言链须区分。

A1 实际姿态/夹爪、R 的物理停止与故障注入、A2 有效绕障、A3/E0 抓放、M1 三布局导航尚未完成。M1 200 ms 墙钟反馈时序仍有失败。机器人首次 GUI/通道和门限变更待原计划的人工确认。发布分支不代表这些验收通过。

本轮只验证导出与现有逻辑，不重新运行模型、训练或 Isaac 实验。Git 推送后的提交 SHA 和分支链接由发布交付消息提供。
