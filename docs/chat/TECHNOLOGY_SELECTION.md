# astrEX chat 技术选型

截至 2026-11-11，首版骨架及真人图片已实现，以下版本已在 Python 3.12.10 上运行。

| 职责 | 选择 | 决策 |
| --- | --- | --- |
| 领域 | Python，EX 可选 chat 模块 | 两套入口调用同一 ChatService |
| 原生 API | FastAPI 0.162.2 / Uvicorn 0.54.0 | 独立 ASGI listener，现有 EX 组装点管理 |
| 验证 | Pydantic 2.14.3 | 拒绝额外字段，错误不回显密码 |
| OneBot11 | websockets 15.0.1 | 反向 Universal WS，保持最低 Python 3.10 的依赖可解析性 |
| 密码 | argon2-cffi 25.1.0 | Argon2id；独立设备随机 token，服务端存散列 |
| 存储 | sqlite3 / 编号 SQL 迁移 | 单连接 RLock、短事务、DELETE 回滚日志 |
| 图片 | Pillow 13.0.0 / SQLite BLOB | 限定静态图片、重新编码去元数据、会话鉴权及账号配额 |
| 测试 | pytest 9.2.0 / httpx 0.28.1 | 真实 HTTP/WS socket 和领域回归 |
| 手机 | Flutter 3.47.9 / Dart 3.13.7 | astrC 独立仓库，真人原生 API |

当前 Python SQLite 3.49.1；暂不用 WAL，避免在未经修复版本验证时增加并发风险。版本依据与背景见官方 [WAL 文档](https://www.sqlite.org/wal.html)。单实例内消息、幂等键和事件在同一事务写入。数据库访问放工作线程，不阻塞 ASGI 循环。多实例需要重新设计存储与分发。

EX 基线 Python >=3.10 保留；当前 chat 因 Pillow 13.0.0 要求 Python >=3.11，本轮实测 3.12.10。websockets 维持已经验证的 15.0.1。完整解析记录见 [开发环境锁](requirements-chat-dev.lock)。

```text
astrC -- native HTTP/sync --> EX ChatService + SQLite
                                    |
                          EX OneBot11 actions/events
                                    |
                          reverse Universal WS --> AstrBot
```

ChatService 属于 EX 对话子系统，宿主暴露引用并统一启停。开发命令可单独启动同一模块，不创建第三个业务仓库。默认 loopback:8780 / 禁用；机器人运动 runtime 不随聊天启动。

真人静态图片使用 SQLite BLOB 存储，复用账号、会话权限与事务；限制及未引用清理缺口见协议。Redis、PostgreSQL、Matrix、独立 Go/Rust 服务仍未引入；现有 8765 管理 listener 不受影响。

安全会话、刷新轮换、限流、持久手机 outbox 与通知仍是后续工作。当前一小时 access token、登出撤销、本人写权限与成员读写权限已实现。资料不含 avatar，未读为 null；不把规划项声明成已有能力。

NapCat 的架构参考与边界见 [参考记录](NAPCAT_REFERENCE.md)。它的配置 WebUI 面向协议维护；astrC 面向真人，不承载协议 token 配置。
