# astrC 设备身份与加密邮箱协议

协议 `astrc-mailbox/1`，修订 `2026-11-11.1`。这是独立于 `astr-chat/0.1-draft` 的设备身份通道。EX 原生账号、OneBot、群聊与真人图片契约维持原样；两种身份和历史不会自动合并。

## 身份和联系人

设备离线随机生成 Ed25519 签名种子与 X25519 加密种子，各 32 字节。身份 ID 为 Ed25519 公钥的 SHA-256、小写 64 位十六进制。昵称不唯一；无全网用户名、手机号、密码登录中心或服务器找回密码。密码只保护设备文件，节点不会收到它。保管加密备份和原密码才能换机恢复；同时恢复到多台设备会争抢单一邮箱，本版不提供多设备历史同步。

名片严格七字段：`v`(整数 1)、`id`、`name`(1..40 字)、`sign_key`(Base64 32 字节)、`box_key`(Base64 32 字节)、`relay`(HTTPS origin)、`signature`(Base64 64 字节)。签名覆盖除 signature 外所有字段。传递格式 `astrc:contact:` 加 Base64URL 编码的完整 canonical JSON。添加时验证签名和 ID，并由用户通过可信渠道核对完整指纹。

HTTPS origin 不含凭据、非根路径、query、fragment。仅开发 `--allow-loopback` 可用 loopback HTTP。客户端不跟随重定向、不关闭证书验证。好友可使用不同节点；发件设备直接通过 HTTPS 投递到收件人的名片节点，不依赖服务器间转发、同一 Wi-Fi 或设备在线直连。节点不是身份权威，但离线消息须有可用公网存储转发服务。可自行部署或替换节点；更换后须重新分享名片并让好友更新，旧节点未收邮件不会自动搬迁。

## canonical JSON 和消息加密

递归排序 object 的字符串 key；数组保留顺序；无额外空格；UTF-8，非 ASCII 字符不转义。协议字段 key 都是 ASCII，整数版本，不使用浮点数。

每条文字新建 X25519 临时密钥，对收件人的静态 X25519 公钥求共享秘密。头部为 `v,id,sender,recipient,created_at,ephemeral`，其中 id 是规范 UUID，created_at 是发送设备 UTC 时间字符串，ephemeral 为临时公钥 Base64。

HKDF-SHA256 输出 32 字节，salt 为 canonical 头部，info 为 UTF-8 `astrc-message-v1`。AES-256-GCM 加密 UTF-8 JSON `{"text":"..."}`，AAD 为相同 canonical 头部，随机 12 字节 nonce，16 字节 MAC。文字 1..4000 Unicode 码点。最终信封严格为头部加 `nonce,ciphertext,mac,signature`；Ed25519 对除 signature 外的 canonical 信封签名。接收方用已核实名片验签，再执行收件人绑定与 AEAD 验证。

节点验证信封签名、大小与投递权限，只保存密文。节点可见双方 ID、名片、公钥、时间、流量大小及联系人许可。它可丢弃、延迟、重放消息；客户端去重和验签不能让恶意节点保证可用。此静态收件密钥方案无双棘轮、无前向保密或生产安全审计承诺。

## HTTPS API

前缀 `/api/v1/peer`。除 health/challenge/session 外都用 `Authorization: Bearer <token>`。错误为 `{"error":"code"}`，实际代码以 peer_relay.py 及测试为准。

| 方法 | 路径 | 请求与行为 |
| --- | --- | --- |
| GET | `/health` | 协议、public_url、7 天保留、32768 字节包上限；不表示身份已连接 |
| POST | `/challenge` | `{id}` → `{challenge_id,challenge}`，60 秒、一次性 |
| POST | `/session` | `{card,challenge_id,signature}` → `{access_token,expires_in:3600}` |
| PUT | `/allowed/{peer_id}` | 本人允许该联系人投递；重复调用幂等 |
| DELETE | `/allowed/{peer_id}` | 本人撤销以后投递的许可；不删除已存邮件 |
| POST | `/envelopes` | 发件人自身认证，提交已签名信封；201 表示已入邮箱 |
| GET | `/envelopes` | 本人最多取 100 封未 ACK 信封，按入库顺序 |
| POST | `/ack` | `{items:[{sender,id}]}`，最多 100 项；仅清本人邮件正文 |

会话证明签名覆盖 UTF-8 `astrc-relay-auth-v1\n<challenge_id>\n<challenge>\n<id>`，每处 `\n` 为一个 LF。签名私钥由设备持有。令牌只以 SHA-256 形式留在节点内存，一小时过期；重启后设备重新证明身份。节点固定同一 ID 对应的 box_key，拒绝换钥；昵称与节点名片可更新。节点不提供私钥托管、身份全网检索或远程密码重置。

收件人必须先在其节点允许发件人。当前 UI 流程是双方互加名片；单方添加不会突破对方许可。403 `recipient_has_not_added_you` 保留本机待发并自动重试。401 重新认证；409 `idempotency_conflict` 表示同 sender+UUID 内容不同；413 为包/联系人/邮箱容量；429 为限流；503 为节点总容量。

## 持久性、重试与容量

发件人先提交本机加密 outbox，再发固定的完整信封；重试不重新加密、不更换 UUID。节点以 sender+UUID 唯一，相同内容返回原接受结果，不同内容 409。接收端验签/解密、提交加密记录后 ACK；ACK 清除正文，保留摘要直到 7 天期限，重复投递不会重新出现。客户端按 sender+UUID 去重。损坏包先持久隔离 ID 后 ACK。客户端展示“已入邮箱”，无已读承诺；过期未取会丢弃，发件端不会在此前接受后自动续期。

节点独立 SQLite，RLock 串行数据库访问，单进程运行。每邮箱最多 1000 封未确认、16 MiB；单包含 HTTP body 最多 32768 字节，解码密文最多 16384 字节。总身份 10000、单人许可 1000、总信封含摘要 100000。挑战 2048、会话 4096；挑战每来源每分钟 30 次，会话 60 次，发信每身份 60 次。期限按服务器入库时刻，客户端时间不作排序或授权依据。请求时惰性清理过期数据；无后台清理任务。

当前客户端前台每 4 秒轮询、每轮最多发 20 条；无后台推送、群聊、媒体、已读、搜索或自动节点迁移。设备总记录最多 10000 条，单加密快照最多 32 MiB；无本版记录清理 UI，接近容量需保留备份并升级处理。

本机使用 PBKDF2-HMAC-SHA256 600000 轮、随机 16 字节盐派生密钥，AES-256-GCM 加密身份、联系人、正文和 outbox；头部版本、代次、盐、迭代数均绑定 AAD。双槽文件每次 flush 写临时文件后替换非活动槽；解锁可回退到另一个可验证槽。此设计防止单槽损坏，无跨设备事务或所有文件系统断电保证。Web 暂不支持设备身份，仍进入 EX UI。

## 验证与运维

`tests/chat/test_peer_relay.py` 覆盖证明重放、权限、篡改、ACK、重启、限额和畸形输入。astrC 的真实 Dart runner 分别测试两个独立节点及已部署 HTTPS；模拟身份用例不能代替 Android 真机、恶意节点审计或公开大规模服务验收。部署和备份见 [PEER_DEPLOYMENT.md](PEER_DEPLOYMENT.md)。
