# astrEX chat

2026-11-10，`chat` 分支：第一版可运行骨架。

EX 对话子系统拥有 ChatService、SQLite、真人 API 和内置 OneBot11 协议端。astrC 面向真人，AstrBot 通过 OneBot 运行插件。普通聊天不需要启动运动 runtime。

已实现账号登录、可选邀请注册、本人资料、私聊/群聊、文字、历史、幂等、撤回、持久同步与反向 Universal WS。原插件兼容仍待真实 AstrBot/插件对照验收。

| 文档 | 内容 |
| --- | --- |
| [开发指南](DEVELOPMENT_GUIDE.md) | 启动、联调、目录、测试与后续阶段 |
| [技术选型](TECHNOLOGY_SELECTION.md) | 固定依赖、存储与部署边界 |
| [协议草案](CHAT_CONTRACT.md) | 当前实现子集和后续目标 |
| [实际 OpenAPI](contracts/openapi.json) | 由当前 FastAPI 应用生成的路由/请求模型 |
| [NapCat 参考](NAPCAT_REFERENCE.md) | 来源提交、文件位置与采用的设计 |
| [兼容矩阵](compatibility-matrix.json) | 协议实测与尚未验证的插件记录 |

快速开始：安装 `.[chat,chat-dev]`，运行 `python -m astrbot_ex.core.chat --demo`；具体命令与账号见开发指南。astrC 的 contracts 保存本仓库的带哈希快照。
