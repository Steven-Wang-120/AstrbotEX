# EX 聊天身份与 Agent 接入

2026-11-11。人使用 astrC，bot 使用 EX Chat；二者持有同一种 Ed25519/X25519 身份，使用 `astrc-mailbox/1` 联系人名片、好友申请和加密文字收发。设备或模拟器不参与协议定义，也不承担中转服务。

```text
astrC（人的身份） ⇄ HTTPS 邮箱 ⇄ EX Chat（bot 的身份） ⇄ OneBot11 ⇄ AstrBot / Agent
```

EX 负责账号私钥、联系人、消息和网络连接；Agent 负责决定回复。邮箱只存密文及联系人许可，不持有解密私钥。普通聊天不启动运动 runtime，也不需要额外手机、局域网直连或另登录一套 EX 账号。

## 创建账号与加好友

安装 EX `.[chat]`，启用 `ASTREX_CHAT_ENABLED=1`，在已有管理页“聊天账号”填写昵称及部署者提供的 HTTPS 聊天网络地址，点击“创建 / 保存身份”。复制名片给好友：对方在 astrC 默认界面“粘贴好友名片”或扫码添加。新版 astrC 会发送签名申请，并在自己的邮箱允许 bot 回信。

EX 默认需手动接受申请：管理页可核对完整指纹后接受，也可交给 OneBot Agent 的 `set_friend_add_request`。明确勾选“自动接受好友申请”后，EX 自动通过申请。陌生人发来申请不等于已获得发消息许可。EX 也可在管理页粘贴人的名片，主动发送好友申请；人在 astrC 菜单“好友申请”中接受。

旧 astrC 0.2.0 可以读同一种 bot 名片和消息，但不会发送申请。该版必须在 EX 中手动添加人的名片；建议升级 0.2.1+5。旧 `astr-chat/0.1-draft` 数字账号入口只留作兼容联调，不是公网机器人联系入口。

启动时也可配置：

```sh
ASTREX_CHAT_ENABLED=1
ASTREX_CHAT_PEER_RELAY=https://mail.example.org
ASTREX_CHAT_PEER_NAME=MyBot
ASTREX_CHAT_PEER_AUTO_ACCEPT=0
ASTREX_CHAT_BOT_ID=3
ASTREX_CHAT_ONEBOT_REVERSE_URL=ws://agent:6199/ws
ASTREX_CHAT_ONEBOT_TOKEN=<shared-agent-token>
```

`BOT_ID` 是已有本地 bot 的适配器编号；空库在管理页创建时自动生成。对 astrC 好友分享的是签名名片和完整指纹，不使用此数字或适配器登录密码。OneBot 的 `self_id`、`user_id`、`message_id` 使用持久数据库编号，映射到网络身份，不改变聊天网络的真实指纹。

身份密钥保存于 EX 数据根目录 `secrets/chat-peer-<bot_id>.json`，Unix 创建权限 0600；昵称、邮箱及策略保存在 `chat/peer-account.json`，联系人/消息/outbox 在聊天 SQLite 中。EX 服务端持有可用私钥和明文供 Agent 处理，其本机保护方式不同于 astrC 的密码加密 vault。只备份到受保护位置，不把私钥、管理凭据、OneBot token 或真实聊天加入版本库。换 EX 主机须迁移完整数据；配置更改不重新生成身份。已有账号不允许通过此界面直接改节点，节点迁移须另作显式处理。

## OneBot 11

参考本机 NapCat `network/websocket-client.ts`、`OB11FriendRequest.ts`、`SetFriendAddRequest.ts` 的协议边界；复用 EX 原有 ActionDispatcher 和反向 Universal WebSocket。包含 `X-Self-ID`、`X-Client-Role: Universal`、Bearer token、生命周期、心跳、重连，响应保留原样 `echo`。

支持私聊文字的 `send_private_msg` / `send_msg`、`get_msg`、`get_login_info`、`get_friend_list`、`get_stranger_info`、`get_status`。新申请上报 `post_type=request, request_type=friend, user_id, comment, flag`；处理动作 `set_friend_add_request(flag, approve)`；通过后上报 `notice_type=friend_add`。回复中 `reply` 可引用 EX 内已有消息；当前 peer 协议仅传文字，引用提示不会跨网保留。图片、语音、群聊和远程撤回尚未实现，相关能力明确拒绝，不声称 NapCat 全量兼容。现有本地旧账号通道仍维持其原有图片/群聊功能。

## 持久性与管理 API

先将收件消息及事件提交 SQLite，再 ACK 邮箱；同 sender+UUID 重投不产生第二条消息。出站完整密文信封先写 `peer_outbox`，网络重试/EX 重启继续使用同一 UUID 与密文。OneBot 持久 offset 是 WebSocket 已发送位置，不是 Agent 消费回执；Agent 端仍应按 message_id 去重，不能承诺崩溃时恰好一次消费。

管理页请求沿用已有同源 Host/Origin 校验和管理 Bearer，不在公开聊天 listener 暴露维护接口：

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/api/v1/ex/chat/account` | 账号状态、公开名片、联系人、待处理申请、OneBot 状态 |
| POST | `/api/v1/ex/chat/account` | `{name,relay,auto_accept}` 创建/保存 |
| POST | `/api/v1/ex/chat/account/contacts` | `{code}` 添加签名名片 |
| POST | `/api/v1/ex/chat/account/requests` | `{flag,approve}` 处理申请 |

验收：`scripts/chat_peer_astrbot_lab.py --image <installed-image> --flutter <flutter> --astrc-root <repo> [--relay https://mail.example.org] --output <report>`。使用隔离真实 AstrBot、真实 Dart PeerController 和实际邮箱，验证加 bot、自动审批、加密请求、内置 `/sid`、加密回复及真人端重启。测试未配置模型，不等于已验第三方插件、Android UI、后台推送或任意地区的网络可达性。

末轮自动验收（2026-11-11）：EX 相关 91 项及 11 个 subtest 通过；Flutter analyze 无问题，默认测试 17 通过、5 项真实服务默认跳过。真实本地邮箱与部署 HTTPS 邮箱的 EX→OneBot→AstrBot /sid 闭环各 9 场景通过；双独立节点好友申请与持久收发 2 项、14 场景通过。匿名证据见 acceptance/20261111/peer-bot-*.json；真实服务测试须显式运行，不以默认跳过项充数。
