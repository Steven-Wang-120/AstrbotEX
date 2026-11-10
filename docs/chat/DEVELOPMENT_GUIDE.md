# astrEX chat 开发指南

日期：2026-11-10。分支 `chat`，状态：可运行文字聊天骨架；尚未达到完整 P1 真机与插件验收。

## 阅读和职责

先遵循本机独立项目索引的 START-HERE 最小协议，再读 [选型](TECHNOLOGY_SELECTION.md)、[契约](CHAT_CONTRACT.md) 和 [NapCat 参考](NAPCAT_REFERENCE.md)。astrS 仅作命名参考。

EX 管理聊天服务与协议端；astrC 是真人手机端；AstrBot 继续承载插件。本轮没有改 AstrBot、A.E.B 或本机 Docker。

## 安装与启动

使用 Python 3.10+；实际验证为 Windows Python 3.12.10 / SQLite 3.49.1。最低 Python 尚未做独立运行验证。chat 为可选依赖，默认关闭。

在 EX 根目录：

```powershell
python -m venv .venv-chat
.\.venv-chat\Scripts\python.exe -m pip install -e ".[chat,chat-dev]"
.\.venv-chat\Scripts\python.exe -m astrbot_ex.core.chat --demo
```

开发服务地址 `http://127.0.0.1:8780`，健康检查 `/api/v1/chat/health`，交互文档 `/docs`。默认数据在 `runtime/chat-development/chat/chat.sqlite3`，可通过 `--data-dir` 指定独立目录。`--host`、`--port` 可覆盖默认值。

`--demo` 只允许 loopback，空库创建 alice（ID 1）、bob（ID 2），共同密码 `astrc-demo`，bot astrbot（ID 3）和一个群。示例消息实际入库；bot 密码随机，不开放真人登录。已有库不会重新填充或重置密码。

正常服务不自动创建示例账号。可以通过本地 ChatService.create_user 创建所有者，再设置 `ASTREX_CHAT_INVITE_TOKEN` 开启邀请注册；当前手机只有登录界面。首版会话一小时有效，登出撤销；refresh 返回 501。没有公网防滥用与限流验收，当前定位为本地开发原型。

## EX 宿主接入

现有 `api_server.build_server()` 支持以下进程环境配置：

| 环境变量 | 默认/作用 |
| --- | --- |
| ASTREX_CHAT_ENABLED | false；启用 EX 所有的 chat listener |
| ASTREX_CHAT_HOST / ASTREX_CHAT_PORT | 127.0.0.1 / 8780 |
| ASTREX_CHAT_DEMO | false；显式启用 loopback 示例数据 |
| ASTREX_CHAT_INVITE_TOKEN | 未设置时禁止注册 |
| ASTREX_CHAT_BOT_ID | 反向连接绑定的现有 bot 账号 ID |
| ASTREX_CHAT_ONEBOT_REVERSE_URL | AstrBot 的反向 WS 地址，例如 ws://127.0.0.1:6199/ws |
| ASTREX_CHAT_ONEBOT_TOKEN | 与 AstrBot 配置一致的协议 token |

独立开发入口也读取后三项和邀请 token，监听地址与 demo 由命令行控制。bot 必须先存在并加入目标群。demo 空库可以用 ID 3。不要把 token 放进手机或提交到仓库。

chat 服务归属 `interaction_core.chat_service`，其生命周期由 EX 创建/关闭。8765 管理服务、旧 ZMQ/A.E.B 和安全动作边界保持各自职责。chat 启动不会调用运动 runtime.start。

## 当前目录

```text
astrbot_ex/core/chat/
  models.py, service.py     请求模型和统一账号/权限/消息操作
  repository.py            SQLite 短事务、RLock 串行访问
  migrations/001_initial.sql
  native_api.py            真人 HTTP/WS、OpenAPI
  onebot11/actions.py      动作注册、事件映射
  onebot11/transport.py    反向 Universal WS、重连与持久发送偏移
  lifecycle.py, __main__.py
```

同步是持久事件轮询：原生 WS 同样读取持久事件，没有补拉/订阅切换空窗。阻塞数据库操作在工作线程执行。当前单数据库连接，不支持多实例。

## 已实现与后续

文字消息支持 UUID 幂等、历史 before 分页、成员权限、撤回 tombstone、账号绑定签名游标。群创建后暂不提供成员变更。未读未实现，返回 null；客户端不虚构数字。

OneBot 支持登录/版本/状态、已有私聊对端列表、群/成员查询、文字私聊/群聊、get_msg/delete_msg。can_send_image/record 为 false。未知 action=1404，权限=1403，参数=1400，冲突=1409，内部失败=1500；echo 保留原 JSON 类型。

未实现 @、引用、图片、CQ 解析、媒体、notice、refresh、日志、已读、后台通知、插件级兼容。原生 WS 的 Authorization header 适用于 IO 客户端，浏览器初版使用 HTTP sync。demo 只给 localhost:5173 与 127.0.0.1:5173 配置 CORS；普通模式没有开放跨域。

OneBot 发送偏移仅证明已尝试写入连接，不证明 AstrBot 已处理。连接中断窗口仍可能重复/丢失；不承诺插件端到端恰好一次。bot 自发消息不再投给同一 bot。

## 验证与联调

```powershell
.\.venv-chat\Scripts\python.exe -m pytest tests/chat/test_chat_skeleton.py tests/test_api_server.py tests/test_interaction_core.py -q
```

当前为 35 项通过，包含真实 socket 的反向 WS 模拟对端、EX 启停、权限、重启、游标、幂等与旧对话回归。模拟对端不等于运行过真实 AstrBot 插件。存在一条上游 TestClient/httpx 弃用提示。

真人手机联调与 Dart HTTP 实测工具在 astrC 开发指南。下一阶段固定 AstrBot 和目标插件提交，分别在 QQ+NapCat 与 EX 测同一插件，源码不改，记录动作、消息段、效果和平台判断至兼容矩阵。先补最常用的 @、引用、图片，再开展插件对照。

## 契约和提交

先改 EX 权威文档、Schema/样例并提升 revision，再生成实际 OpenAPI；提交 EX 后同步 astrC contracts 的源提交和 SHA-256。OpenAPI 当前主要描述请求和路由，响应语义以 ChatService 与原生 Schema 为准，不直接用于完整响应客户端生成。

只提交任务文件。数据库、真实消息、令牌、venv 与本机配置不入库。依赖顶层在 pyproject 固定；[开发环境锁](requirements-chat-dev.lock) 保存本轮解析版本，跨平台安装仍需验证。
