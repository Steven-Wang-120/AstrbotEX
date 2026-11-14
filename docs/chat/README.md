# astrEX chat

2026-11-14，`chat` 分支：真人与 EX bot 同网，OneBot11 接 Agent；媒体协议修订 `2026-11-13.1`。

EX 对话子系统拥有 ChatService、SQLite、持久 bot 设备身份与内置 OneBot11 协议端。人用 astrC；bot 用 EX Chat，双方通过名片及 HTTPS 加密邮箱通信；EX 再经 OneBot11 接 AstrBot 或其他 Agent。应用宝只是测试设备。普通聊天不需要启动运动 runtime。

已实现账号登录、可选邀请注册、本人资料、私聊/群聊、文字/@/引用、图片、文件、动静态表情包、历史、幂等、撤回、持久同步与反向 Universal WS。peer 通道支持加密媒体私聊和持久表情收藏；管理页提供 ＋ 附件面板、笑脸面板和逐条时间戳，使用图标及必要短标签。

真实 AstrBot 4.26.7 的 `/sid`、图片、文件及 GIF 回环已通过。媒体验收使用隔离合成身份和专用探针；普通插件的 `file://` 输出仍需适配，第三方插件、QQ/NapCat 对照和 Android 真机仍待验收。新代码尚未切换现用公网邮箱及 EX 服务。

| 文档 | 内容 |
| --- | --- |
| [开发指南](DEVELOPMENT_GUIDE.md) | 启动、联调、目录、测试与后续阶段 |
| [媒体升级验证](PROGRESS_20261113.md) | 0.3.0 对接、真实媒体回环、上线与回退边界 |
| [进度与后续计划](PROGRESS_20261111.md) | 本轮证据、Android 手测配合与后续顺序 |
| [技术选型](TECHNOLOGY_SELECTION.md) | 固定依赖、存储与部署边界 |
| [协议草案](CHAT_CONTRACT.md) | 当前实现子集和后续目标 |
| [实际 OpenAPI](contracts/openapi.json) | 由当前 FastAPI 应用生成的路由/请求模型 |
| [NapCat 参考](NAPCAT_REFERENCE.md) | 来源提交、文件位置与采用的设计 |
| [兼容矩阵](compatibility-matrix.json) | 协议实测与尚未验证的插件记录 |

快速开始：安装 `.[chat,chat-dev]`，运行 `python -m astrbot_ex.core.chat --demo`；具体命令与账号见开发指南。astrC 的 contracts 保存本仓库的带哈希快照。

## 设备身份与全球邮箱通道

独立 [加密邮箱协议](PEER_PROTOCOL.md) 与 [部署指南](PEER_DEPLOYMENT.md)。设备自行生成身份，使用可替换 HTTPS 节点存储转发，不依赖同一局域网。真人和 EX bot 在此通道使用同一种身份与加密文字/附件，EX 通过 OneBot11 连接 Agent；见 [机器人身份](PEER_BOT.md)。旧数字账号通道保留兼容联调。
