# astrEX chat 开发指南

日期：2026-11-10。分支：`chat`。本轮仅开发准备，运行代码尚未实现。

## 阅读与职责

按本机 `C:\Users\17088\AstrBotVLA-project-index\START-HERE.md` 的最小协议阅读；只查目标模块及直接依赖、调用者和测试。然后读本目录 [选型](TECHNOLOGY_SELECTION.md) 与 [契约](CHAT_CONTRACT.md)。astrS 只用于命名参照，无需读取其仓库。

- `D:\Code\AstrBotEX` / `chat`：聊天服务、存储、权限、内置 OneBot11 协议端与对话集成。
- `D:\Code\astrC` / `main`：真人 Flutter 手机端。
- AstrBot 继续运行插件，连接 EX 协议端。本轮不改 AstrBot/A.E.B，也不另建协议端仓库。

## 当前状态

EX 已有 InteractionCore、业务连接、ZMQ/A.E.B 和语音链路。当前没有本文设计的多人账号/群组库、原生 chat API 或 OneBot 协议端。以下目录、命令和阶段均是实施计划；本轮没有 chat 启动命令。

## 计划目录

```text
astrbot_ex/core/chat/
  service.py           # 统一业务操作、权限
  models.py            # 账号、群组、消息与事件
  repository.py        # SQLite、事务、outbox
  migrations/          # 编号 SQL 升级
  native_api.py        # 真人 HTTP/WS
  onebot11/            # 事件、action、反向 WS
  lifecycle.py         # 启停、排空与资源关闭
tests/chat/            # 领域、契约、连接与兼容回归
docs/chat/             # 本轮已存在的准备材料
```

模块名可以随 P1 微调，职责和外部契约变更必须记录。不要继续把所有业务堆入 interaction_core.py。

## 开发阶段

| 阶段 | 范围 | 完成条件 |
| --- | --- | --- |
| P0 本轮 | 指南、选型、契约、样例、兼容矩阵 | 链接/Schema/样例/哈希通过，未测项明确 |
| P1 闭环 | 可选依赖、生命周期、邀请注册、设备会话、文字私聊/群聊、反向 WS | 两真人与一 bot 同群双向文字；不启动运动 runtime |
| P2 可靠性 | 历史、幂等、恢复同步、权限、撤回、图片与引用 | 重连和重启不串会话；媒体/引用真机可用 |
| P3 插件 | 固定 AstrBot/插件版本，QQ 与 EX 对照 | 原插件不改源码，已声明功能的效果与证据完整 |
| P4 真人功能 | 本人资料、日志、通知 | 可见范围和本人写权限正确；后台通知真机验证 |

## P1 落地顺序

1. 实现不依赖 HTTP/WS 的 ChatService 和数据库事务，先测账号/群权限、幂等与事件提交。
2. 提供原生 API，再让 OneBot action 调用同一服务。
3. EX 协议端主动反向连接 AstrBot 现有 aiocqhttp 平台；不同 bot 使用明确身份。
4. 在 build_server() 注册可选 chat 生命周期；默认关闭，最小 EX 安装仍可启动。
5. chat 停止时先关接入、再排空有界任务、最后关连接和数据库；不阻塞旧 runtime 的安全停止。
6. chat 与旧 A.E.B 的身份和回包独立；默认不把 bot 自发消息再次上报给同一 bot，防止回环。

## 协议变更流程

1. EX `docs/chat/CHAT_CONTRACT.md`、Schema 和例子是权威源，先改这里并更新 revision。
2. 同步到 astrC `contracts/`；manifest 更新来源提交和各文件 SHA-256。
3. 两端验证结构、语义和例子再提交。不兼容修改提升版本。
4. P1 有实际 API 后生成 OpenAPI 并与契约一起评审；本轮未生成伪 OpenAPI。

原生用户使用 `/api/v1/chat/*`；OneBot 使用标准字段和响应外壳。`get_version_info` 真实返回 EX 信息，不能假冒 NapCat。自有扩展用 `_ex_` 前缀。

## 原插件兼容验收

矩阵为 `compatibility-matrix.json`，初始记录全部待测。每个插件记录仓库/commit、平台判断、action、消息段、NapCat 扩展和调用轨迹。也检查 AstrBot 适配器自己触发的 get_msg、成员与媒体查询。

```text
A: 固定 AstrBot + 原插件 + 固定 NapCat + QQ 测试账号/群
B: 同版 AstrBot + 同版原插件 + EX 协议端 + EX 测试账号/群
```

允许改连接和账号映射，插件源码保持不变。比较正确触发、接收对象、持久副作用和手机展示；动态 ID、时间、随机模型文本按规则映射。

必测：文字指令、@、引用、图片、主动发送、多群、多 bot、权限拒绝、未知 action、断线/重启、离线补拉。禁言要验证实际成员状态，不能只看 success。用脱敏轨迹保存证据。

OneBot 无整个消费链的处理确认，重连窗口可能重复/遗漏；EX 单独定义 outbox 重试与有效期，不能承诺原插件端到端恰好执行一次。手机持久游标恢复单独验收。

## 验证

准备阶段只做文档链接、JSON/Schema、例子、快照哈希和 `git diff --check`。没有业务改动，不重复整套硬件测试。

首次改动现有组装或交互时运行目标回归：

```powershell
python -m unittest discover -s tests -p test_api_server.py
python -m unittest discover -s tests -p test_interaction_core.py
```

P1 新增 chat 测试后运行对应目录。涉及动作/ROS 再按索引扩展；不为文档声称执行过实现测试。

## 本机与提交

Python 3.12.10、SQLite 3.49.1 已检测；基线最低 Python 仍为 3.10。手机 SDK 尚未配置，见 astrC 指南。chat 开发 listener 拟用 8780，实施前确认冲突；用户 token 不允许访问 8765 管理端。

小步提交，使用 `docs(chat): ...` / `feat(chat): ...` / `test(chat): ...`。只暂存本任务文件，不修改其他工作树。令牌、真实消息、数据库、签名与本机代理不入库。
