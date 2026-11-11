# astrEX chat 开发指南

日期：2026-11-10。分支 `chat`，状态：可运行文字、@ 和引用聊天；尚未达到完整 P1 真机与插件验收。

## 阅读和职责

先遵循本机独立项目索引的 START-HERE 最小协议，再读 [选型](TECHNOLOGY_SELECTION.md)、[契约](CHAT_CONTRACT.md) 和 [NapCat 参考](NAPCAT_REFERENCE.md)。astrS 仅作命名参考。

EX 管理聊天服务与协议端；astrC 是真人手机端；AstrBot 继续承载插件。本轮没有改 AstrBot、A.E.B 或本机 Docker。

## 安装与启动

基础 EX 使用 Python 3.10+；当前 chat 依赖 Pillow 13.0.0，chat 环境需要 Python 3.11+；实际验证为 Windows Python 3.12.10 / SQLite 3.49.1。最低 Python 尚未做独立运行验证。chat 为可选依赖，默认关闭。

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

文字、mention 和 reply 消息支持 UUID 幂等、历史 before 分页、成员权限、撤回 tombstone、账号绑定签名游标。群创建后暂不提供成员变更。未读未实现，返回 null；客户端不虚构数字。

OneBot 支持登录/版本/状态、已有私聊对端列表、群/成员查询、文字私聊/群聊、群 at、reply、get_msg/delete_msg、共享会话账号的 get_stranger_info。can_send_image/record 为 false。未知 action=1404，权限=1403，参数=1400，冲突=1409，内部失败=1500；echo 保留原 JSON 类型。

真人静态图片已实现；未实现机器人图片、CQ 解析、notice、refresh、日志、已读、后台通知、插件级兼容。原生 WS 的 Authorization header 适用于 IO 客户端，浏览器初版使用 HTTP sync。demo 只给 localhost:5173 与 127.0.0.1:5173 配置 CORS；普通模式没有开放跨域。

OneBot 发送偏移仅证明已尝试写入连接，不证明 AstrBot 已处理。连接中断窗口仍可能重复/丢失；不承诺插件端到端恰好一次。bot 自发消息不再投给同一 bot。

## 验证与联调

```powershell
.\.venv-chat\Scripts\python.exe -m pytest tests/chat tests/test_api_server.py tests/test_interaction_core.py tests/test_interaction_status.py -q
```

当前为 72 项及 11 个 subtest 通过，包含真实 socket 的反向 WS 模拟对端、EX 启停、权限、重启、游标、幂等与旧对话回归。模拟对端不等于运行过真实 AstrBot 插件。存在一条上游 TestClient/httpx 弃用提示。

真人手机联调与 Dart HTTP 实测工具在 astrC 开发指南。下一阶段固定 AstrBot 和目标插件提交，分别在 QQ+NapCat 与 EX 测同一插件，源码不改，记录动作、消息段、效果和平台判断至兼容矩阵。@、引用已通过真实适配器联测；真人图片已完成，随后补机器人图片并开展插件对照。

## 契约和提交

先改 EX 权威文档、Schema/样例并提升 revision，再生成实际 OpenAPI；提交 EX 后同步 astrC contracts 的源提交和 SHA-256。OpenAPI 当前主要描述请求和路由，响应语义以 ChatService 与原生 Schema 为准，不直接用于完整响应客户端生成。

只提交任务文件。数据库、真实消息、令牌、venv 与本机配置不入库。依赖顶层在 pyproject 固定；[开发环境锁](requirements-chat-dev.lock) 保存本轮解析版本，跨平台安装仍需验证。

## 真实 AstrBot 内置指令验收

2026-11-10 已使用隔离的 AstrBot 4.26.7 镜像，保持其适配器和 builtin_commands 源码不变。双真人群聊 /sid、bot 私聊、身份、私聊访问隔离、机器人重启重连、astrC 实际 Dart 客户端均通过。详见 acceptance/20261110/real-astrbot-roundtrip.json；它固定镜像完整 ID 和关键文件 SHA-256，而非本机 AstrBot-latest HEAD。

```powershell
.\.venv-chat\Scripts\python.exe scripts/chat_astrbot_lab.py --image <installed-image> --output <evidence.json>
```

加入 --flutter <flutter.bat> --astrc-root ..\astrC 可同时运行手机仓库的真实客户端测试。工具只使用已安装镜像，不拉取新镜像，不读取生产配置；创建临时数据库和容器，finally 清理。随机 host port 会在 Docker restart 后改变，因此工具先选择端口再明确绑定，让同一轮重连目标保持稳定。

--serve --data-dir <isolated-directory> --host <LAN-IP> --port 8780 可供手机手测。此模式为普通服务，首次创建 alice、bob、bot 和测试群，使用随机测试密码保存在该目录的 TEST_ACCOUNTS.json；该文件和数据库不得提交。--check-only 在自检后退出，未带该选项则等待 Ctrl+C。astrC 提供 tools/start_manual_lab.ps1 便捷入口。

本轮只证明内置 /sid 场景，不代替 QQ/NapCat 对照、第三方插件、断线窗口恰好一次或 Android 真机验收。

提及与引用补充验收见 acceptance/20261110/segments-astrbot-roundtrip.json：开启真实 AstrBot reply_with_mention/reply_with_quote，验证输入段解析、实际 get_msg/get_group_member_info/get_stranger_info 查询及原生回复段。

## 真人图片

原始图片以 POST /api/v1/chat/media?conversation_id=<id>&client_media_id=<uuid> 上传，body 为 application/octet-stream，返回 media_id、content_type、size_bytes、width、height、sha256。再用 image.data.media_id 发送到同一会话；GET /api/v1/chat/media/{id} 携带真人 Bearer 读取。无公开文件路径和下载 URL。

当前限制：静态 PNG/JPEG/WebP，8 MiB、8192 单边、1600 万像素、64 MiB/账号；规范化清除元数据，未引用上传仍占配额。数据库迁移至 user_version=2。完整权限及重试语义见契约。机器人图片、未引用清理尚未实现，手机真机/iOS 仍待验收。
