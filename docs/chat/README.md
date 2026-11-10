# astrEX chat 开发准备

日期：2026-11-10。分支：`chat`。当前状态：文档与契约准备，业务尚未实现。

| 文档 | 用途 |
| --- | --- |
| [开发指南](DEVELOPMENT_GUIDE.md) | 开发顺序、模块边界、联调和验收 |
| [技术选型](TECHNOLOGY_SELECTION.md) | 框架、存储、部署及替代方案 |
| [协议草案](CHAT_CONTRACT.md) | 真人原生协议与 OneBot 映射的权威定义 |
| [兼容矩阵](compatibility-matrix.json) | 原插件依赖与实际验证记录模板 |
| [Schema 与示例](contracts/) | 机器可验证的数据结构 |

产品分工：EX 对话模块面向 bot，内置 OneBot11 协议端；astrC 手机版面向真人。自有聊天服务首版也在 EX 仓库内开发，不增加第三个业务仓库。

本目录列出的新模块、API 和测试场景均是待实施设计。尚无聊天后端、OneBot 协议端、手机应用或已验证兼容的插件。astrC `contracts/` 保存本目录协议的带 SHA-256 副本。
