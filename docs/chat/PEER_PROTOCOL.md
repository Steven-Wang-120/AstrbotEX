# astrC 设备身份与加密邮箱协议

协议 `astrc-mailbox/1`，修订 `2026-11-13.1`。astrC 真人与 EX bot 使用同一设备身份、名片和加密消息协议，支持文字、图片、文件和动静态表情包；EX 经 OneBot11 连接 Agent。原 `astr-chat/0.1-draft` 仅保留兼容联调，历史不会自动合并。机器人账号和管理见 [PEER_BOT.md](PEER_BOT.md)。

## 身份和联系人

设备离线随机生成 Ed25519 签名种子与 X25519 加密种子，各 32 字节。身份 ID 为 Ed25519 公钥的 SHA-256、小写 64 位十六进制。昵称不唯一；无全网用户名、手机号、密码登录中心或服务器找回密码。密码只保护设备文件，节点不会收到它。保管加密备份和原密码才能换机恢复；同时恢复到多台设备会争抢单一邮箱，本版不提供多设备历史同步。

名片严格七字段：`v`(整数 1)、`id`、`name`(1..40 字)、`sign_key`(Base64 32 字节)、`box_key`(Base64 32 字节)、`relay`(HTTPS origin)、`signature`(Base64 64 字节)。签名覆盖除 signature 外所有字段。传递格式 `astrc:contact:` 加 Base64URL 编码的完整 canonical JSON。添加时验证签名和 ID，并由用户通过可信渠道核对完整指纹。

HTTPS origin 不含凭据、非根路径、query、fragment。仅开发 `--allow-loopback` 可用 loopback HTTP。客户端不跟随重定向、不关闭证书验证。好友可使用不同节点；发件设备直接通过 HTTPS 投递到收件人的名片节点，不依赖服务器间转发、同一 Wi-Fi 或设备在线直连。节点不是身份权威，但离线消息须有可用公网存储转发服务。可自行部署或替换节点；更换后须重新分享名片并让好友更新，旧节点未收邮件不会自动搬迁。

## canonical JSON 和消息加密

递归排序 object 的字符串 key；数组保留顺序；无额外空格；UTF-8，非 ASCII 字符不转义。协议字段 key 都是 ASCII，整数版本，不使用浮点数。

每条消息新建 X25519 临时密钥，对收件人的静态 X25519 公钥求共享秘密。头部为 `v,id,sender,recipient,created_at,ephemeral`，其中 id 是规范 UUID，created_at 是发送设备带时区的 RFC3339 时间字符串，ephemeral 为临时公钥 Base64。接收端显示签名覆盖的发送时间，不能以收件时刻替换；节点仍以入库时刻计算保留期限。

HKDF-SHA256 输出 32 字节，salt 为 canonical 头部，info 为 UTF-8 `astrc-message-v1`。AES-256-GCM 加密 UTF-8 JSON，AAD 为相同 canonical 头部，随机 12 字节 nonce，16 字节 MAC。纯文字继续使用 `{"text":"..."}`，文字最多 4000 Unicode 码点。附件正文见下节。最终信封严格为头部加 `nonce,ciphertext,mac,signature`；Ed25519 对除 signature 外的 canonical 信封签名。接收方用已核实名片验签，再执行收件人绑定与 AEAD 验证。

## 加密附件（修订 2026-11-13.1）

正文严格包含 `text` 与可选 `attachments`，附件存在时文字可以为空，两者不能同时为空：

```json
{"text":"你好 😀","attachments":[{"kind":"file","name":"notes.txt","mime":"application/octet-stream","size":3,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","data":"YWJj"}]}
```

每条消息最多 4 个附件，原始字节合计最多 8 MiB；每项严格六字段。`kind` 为 image/file/sticker，`name` 为 1..180 Unicode 码点的文件名，拒绝路径分隔符、控制字符及 `.`/`..`；`mime` 为小写 MIME，`size` 是字节数，`sha256` 是原始字节的小写 SHA-256，`data` 为标准 Base64。接收端必须验证长度和摘要。emoji 直接存在 text 内，不建立附件。

image/sticker 只接受可解码的 PNG/JPEG/GIF/WebP，并匹配真实 MIME；单边最多 8192、总像素最多 1600 万、动图最多 240 帧且像素×帧数最多 6400 万。file 保留任意文件字节，以文件下载处理。此通道保留动图和原始文件，图片可能携带原文件元数据。文件名和附件字节都在密文内，不通过公开附件 URL 交给邮箱。旧客户端仍可收发纯文字，收发附件须升级；节点 health.media=true 标识本修订。

手机“＋”打开相册、拍摄、文件和收藏入口，笑脸打开 Emoji/我的表情；消息长按图片可收藏，收藏页可导入或移除。收藏与附件写入原密码保护的 vault，最多 100 个收藏、原字节合计 24 MiB。导入/导出文件使用系统选择器，相机仅在拍摄或扫码时申请权限。界面以图标和短标签为主，容量说明在文档中。

节点验证信封签名、大小与投递权限，只保存密文。节点可见双方 ID、名片、公钥、时间、流量大小及联系人许可。它可丢弃、延迟、重放消息；客户端去重和验签不能让恶意节点保证可用。此静态收件密钥方案无双棘轮、无前向保密或生产安全审计承诺。

## HTTPS API

前缀 `/api/v1/peer`。除 health/challenge/session 外都用 `Authorization: Bearer <token>`。错误为 `{"error":"code"}`，实际代码以 peer_relay.py 及测试为准。

| 方法 | 路径 | 请求与行为 |
| --- | --- | --- |
| GET | `/health` | 协议、public_url、保留期、media=true、max_packet_bytes=16777216、max_attachment_bytes=8388608；不表示身份已连接 |
| POST | `/challenge` | `{id}` → `{challenge_id,challenge}`，60 秒、一次性 |
| POST | `/session` | `{card,challenge_id,signature}` → `{access_token,expires_in:3600}` |
| PUT | `/allowed/{peer_id}` | 本人允许该联系人投递；重复调用幂等 |
| DELETE | `/allowed/{peer_id}` | 本人撤销以后投递的许可；不删除已存邮件 |
| POST | `/envelopes` | 发件人自身认证，提交已签名信封；201 表示已入邮箱 |
| GET | `/envelopes` | 本人最多取 100 封未 ACK 信封，按入库顺序；每批信封字节合计最多 16 MiB |
| POST | `/ack` | `{items:[{sender,id}]}`，最多 100 项；仅清本人邮件正文 |

会话证明签名覆盖 UTF-8 `astrc-relay-auth-v1\n<challenge_id>\n<challenge>\n<id>`，每处 `\n` 为一个 LF。签名私钥由设备持有。令牌只以 SHA-256 形式留在节点内存，一小时过期；重启后设备重新证明身份。节点固定同一 ID 对应的 box_key，拒绝换钥；昵称与节点名片可更新。节点不提供私钥托管、身份全网检索或远程密码重置。

收件人必须先在其节点允许发件人。当前 UI 流程是双方互加名片；单方添加不会突破对方许可。403 `recipient_has_not_added_you` 保留本机待发并自动重试。401 重新认证；409 `idempotency_conflict` 表示同 sender+UUID 内容不同；413 为包/联系人/邮箱容量；429 为限流；503 为节点总容量。

## 持久性、重试与容量

发件人先提交本机加密 outbox，再发固定的完整信封；重试不重新加密、不更换 UUID。节点以 sender+UUID 唯一，相同内容返回原接受结果，不同内容 409。接收端验签/解密、提交加密记录后 ACK；ACK 清除正文，保留摘要直到 7 天期限，重复投递不会重新出现。客户端按 sender+UUID 去重。损坏包先持久隔离 ID 后 ACK。客户端展示“已入邮箱”，无已读承诺；过期未取会丢弃，发件端不会在此前接受后自动续期。

节点独立 SQLite，RLock 串行数据库访问，单进程运行。每邮箱最多 1000 封未确认、64 MiB，所有未确认密文合计最多 512 MiB；信封 HTTP body 最多 16 MiB，解码密文最多 12 MiB，其他请求 body 仍限 32768 字节。总身份 10000、单人许可 1000、总信封含摘要 100000。挑战 2048、会话 4096；挑战每来源每分钟 30 次，会话 60 次，发信每身份 60 次。期限按服务器入库时刻，客户端时间不作排序或授权依据。请求时惰性清理过期数据；无后台清理任务。

当前客户端前台每 4 秒轮询、每轮最多发 20 条；无后台推送、群聊、已读、搜索或自动节点迁移。设备总记录最多 10000 条，单加密快照最多 128 MiB（包含附件编码和加密开销）；无本版记录清理 UI。附件较多时通过系统文件保存加密备份；剪贴板备份限 512 KiB。

本机使用 PBKDF2-HMAC-SHA256 600000 轮、随机 16 字节盐派生密钥，AES-256-GCM 加密身份、联系人、正文和 outbox；头部版本、代次、盐、迭代数均绑定 AAD。双槽文件每次 flush 写临时文件后替换非活动槽；解锁可回退到另一个可验证槽。此设计防止单槽损坏，无跨设备事务或所有文件系统断电保证。Web 暂不支持设备身份，仍进入 EX UI。

## 验证与运维

`tests/chat/test_peer_relay.py` 覆盖证明重放、权限、篡改、ACK、重启、限额和畸形输入。astrC 的真实 Dart runner 分别测试两个独立节点及已部署 HTTPS；模拟身份用例不能代替 Android 真机、恶意节点审计或公开大规模服务验收。部署和备份见 [PEER_DEPLOYMENT.md](PEER_DEPLOYMENT.md)。


## 签名好友申请（修订 .2）

health 新增 `friend_requests:true` 能力，原消息信封及七字段名片不变。申请严格字段为 `v,id,recipient,card,created_at,comment,signature`；v 为整数1，id 为规范 UUID，recipient 是收件指纹，card 是发件人原签名名片，comment 最多200 Unicode码点。外层 signature 由发件身份对其余字段 canonical JSON 签名。请求方在收件节点证明本人身份，外层 card.id 必须等于认证身份；节点校验两层签名。

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| POST | `/friend-requests` | 提交固定签名申请，201；同 sender+UUID 幂等，改内容409 |
| GET | `/friend-requests` | 本人的待处理申请，最多100 |
| POST | `/friend-requests/{id}/resolve` | 收件人提交 `{sender,approve:boolean}`；接受时允许该身份投递，重复同决策幂等 |

发送前先本机保存固定申请，重试不换 UUID/签名。接收人核对指纹后接受；主动添加名片的人也会先在自己的邮箱允许该联系人。申请只能由收件人处理，不能由第三者授权。已允许的联系人申请直接返回 approved；未知收件人404，伪造/篡改400。申请含公开名片和 comment，不是端到端加密消息，因此不在 comment 填写秘密。

每人每分钟最多30申请，收件人最多100个待处理，总申请100000，7天惰性过期；申请继续使用32768字节HTTP上限。允许联系人仍限1000人。新增独立表，不改变旧信封/ACK；旧客户端可继续互加名片收发，不会自动发出新申请。新客户端如连接旧节点，会保留原互加流程；机器人自动加好友需要节点支持好友申请能力。
