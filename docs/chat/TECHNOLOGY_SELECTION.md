# astrEX chat 技术选型

决策日期：2026-11-10。状态：初选；完成最小原型后冻结依赖版本。

## 目标

EX 对话模块内置类似 NapCat 职责的 OneBot11 协议端，连接 AstrBot 原有 `aiocqhttp` 适配器；自有聊天服务提供账号、群组、消息、媒体与日志。astrC 只服务真人。

首轮按 Android 优先、保留 iOS 评估；此平台优先级尚未得到用户明确确认。

## 现有代码依据

基线提交：`b0d81697069dc82428dfcfb89f323f64db1bdd0a`。

| 位置 | 当前事实 | 对新设计的影响 |
| --- | --- | --- |
| `pyproject.toml` | Python >=3.10，pyzmq 与 websockets | chat 作为可选依赖，保持原有安装路径 |
| `astrbot_ex/core/api_server.py:build_server()` | ThreadingHTTPServer 组装交互、连接与运行时 | 在现有组装点管理 chat；不整体改写管理服务 |
| `astrbot_ex/core/interaction_core.py:send_text()` | 配置业务连接时走 ZMQ，保留 HTTP 回退 | 新 OneBot 接入独立注册，不用旧单会话代表多人聊天 |
| `astrbot_ex/core/topic_bus.py` | 进程内 latest-value/有界 inbox | 不充当持久消息库或手机同步日志 |
| `tests/test_api_server.py` | 已有组装/关闭测试 | 实施后增加 chat 生命周期与资源关闭回归 |

索引的 HTTP/ZMQ 历史提示以当前源码分支为准。

## 初选栈

| 层 | 选择 | 原因与约束 |
| --- | --- | --- |
| 领域服务 | Python，保持 EX >=3.10 | 复用宿主；新增依赖需验证最低 Python |
| 原生 API | FastAPI + Uvicorn | 模型验证、OpenAPI 与 WebSocket；独立 ASGI listener |
| API 模型 | Pydantic v2 | 边界校验；不直接暴露内部 Python 对象 |
| OneBot | 薄适配层 + 已有 websockets | EX 主动连接 AstrBot 的反向 WS；业务落到统一 ChatService |
| 账号/消息 | SQLite + sqlite3，编号 SQL 迁移 | 首版单实例、低运维；事务与恢复先验证 |
| 媒体 | EX 数据目录内受控文件存储 | 限制大小/类型、路径与访问票据；后续按需对象存储 |
| 认证 | Argon2id + 有状态设备会话 | 密码散列、令牌撤销、多设备与刷新轮换 |
| 手机端 | Flutter/Dart | 详见 astrC 技术选型；待 Android 真机原型 |

本轮不修改 `pyproject.toml`，不安装运行依赖。实施时添加可选 `chat` 依赖组，锁定实际验证过的版本。FastAPI/Uvicorn/Pydantic 必须验证 Python 3.10 支持；必要时另行决定是否提高最低版本。

## 部署与模块落位

计划新增 `astrbot_ex/core/chat/`，由 EX 对话子系统拥有。ChatService 提供统一账号/权限/消息操作；原生 API 与 OneBot action 调用同一服务。

```text
astrC -- HTTPS/WSS --> EX chat 原生 API
                           |
                    ChatService + 数据库
                           |
                 EX 内置 OneBot11 协议端
                           |
                 反向 WebSocket --> AstrBot
```

- 首版由 EX `build_server()` 负责组装/关闭 chat，独立事件循环和 listener，默认禁用。
- 候选开发地址 `127.0.0.1:8780`，实施前检查冲突，端口可配置。
- 现有 8765 管理服务和 8766–8769 桥接职责遵循当前代码；chat 用户凭据不授予管理或动作权限。
- 普通聊天无需启动运动 runtime；机器人具身授权继续走已有安全边界。
- 每条 bot 连接绑定确定的账号与凭据；只投递该 bot 可见的会话。
- 首轮只实现反向 Universal WS。HTTP action 和正向 WS 待实际兼容需要增加。
- 旧 A.E.B/ZMQ 文字语音链路独立保留，避免双入口重复回复。

## 存储与并发

初版采用 SQLite 默认回滚日志 DELETE、受控串行写队列、短事务和忙等待超时；阻塞数据库调用不能占据 ASGI 事件循环。消息、幂等键和待分发事件在同一事务提交。

本机 Python 3.12.10 的 SQLite 为 3.49.1。官方 2026 WAL-reset bug 影响包含该版本的区间；多连接 WAL 必须先确认修复版或官方回补版。首版不默认启用 WAL，详见官方 [WAL 说明](https://www.sqlite.org/wal.html)。手机实际打包 SQLite 版本另行验证。

未来多实例需 PostgreSQL、共享媒体与跨实例分发；这属于数据库和架构迁移，不承诺仅改连接串即可完成。

## 替代方案

| 方案 | 本轮结论 | 原因 |
| --- | --- | --- |
| ThreadingHTTPServer 扩展所有聊天功能 | 不选 | 用户 WS、Schema 和 API 文档需自行维护 |
| FastAPI 替换整个 EX 管理服务 | 暂缓 | 不为聊天原型牵动已存在的控制生命周期 |
| Go/Rust 独立服务 | 暂缓 | 增加语言与部署链，先验证业务闭环 |
| Matrix 等现成 IM | 保留候选 | 可减少基础设施工作，但需额外服务与 OneBot 语义映射原型 |
| 直接搬入 NapCat 的 QQ 接入代码 | 不选 | 自有网络由 EX 负责账号、存储与投递，兼容边界是 OneBot |
| 手机直接用 OneBot11 | 不选 | 真人需要设备会话、历史恢复、资料与日志的原生 API |

## 冻结与验证

本轮只固定协议草案 `astr-chat/0.1-draft`。原型完成后记录依赖锁、AstrBot commit、插件 commit、连接方式与实测结果。OneBot 标准子集、NapCat 扩展和插件功能分别验收，不把 Schema 验证当作兼容结论。

## 官方依据

- [OneBot11 标准](https://github.com/botuniverse/onebot-11)。
- [AstrBot OneBot 适配器](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)。
- [FastAPI WebSocket](https://fastapi.tiangolo.com/advanced/websockets/)、[生命周期](https://fastapi.tiangolo.com/advanced/events/)。
- [SQLite WAL](https://www.sqlite.org/wal.html)。
- [Flutter Android 环境](https://docs.flutter.dev/platform-integration/android/setup)、[Flutter iOS 环境](https://docs.flutter.dev/platform-integration/ios/setup)。

资料核对日期：2026-11-10。依赖解析与最低版本验证留在 P1，不使用猜测的最新版本。
