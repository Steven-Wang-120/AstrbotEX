# astr-chat 原生协议与 OneBot 映射草案

协议：`astr-chat/0.1-draft`。revision：`2026-11-10.3`。状态：已实现文字、提及与引用子集，其余仍为设计目标。

权威位置：astrEX `chat` 分支的 `docs/chat/CHAT_CONTRACT.md`。astrC `contracts/` 保存同修订快照。

## 当前实现范围

已实现 login/logout、可选邀请注册、GET/PATCH me、会话列表/创建私聊、建群/查成员、文字发送/分页历史/撤回、sync/events；实际路由见同目录 contracts/openapi.json。

当前支持 text、mention、reply 段；资料 display_name/bio；未读为 null；设备 access token 一小时、无 refresh（501）。posts 也是 501。成员变更、read、媒体、日志、头像、notice、刷新轮换和游标过期清理尚未实现。以下完整清单是后续目标，不能据此调用尚不存在的路由。

OneBot 当前已有私聊对端定义为 friend_list；结构化数组执行 text/at/reply 显式双向映射，尚不解析 CQ 字符串（auto_escape=true 时作为纯文本）；支持基础群/成员查询、受共享会话约束的 get_stranger_info、文字/at/reply 收发、取消息/撤回。can_send_image/record=false，get_image 尚不支持。传输与模拟对端测试不代表真实插件兼容。

## 角色与部署

EX 对话子系统拥有 ChatService、数据存储、真人原生 API 与内置 OneBot11 协议端。astrC 为真人客户端；AstrBot 等机器人程序通过 OneBot11 使用 bot 账号。首版聊天服务由 EX 宿主管理，无需增加第三个业务仓库。

一个部署对应一个聊天空间。跨实例联邦、QQ 用户网络互通、腾讯账号凭据不属于本协议。

## 身份、ID 与时间

- 真人与 bot 共用持久账号 ID 空间；群与消息各有独立 ID 空间，允许不同域数值相同。
- 为兼容 OneBot11 message_id 的 int32，首版所有对外实体 ID 均为 `1..2147483647`；native JSON 使用十进制字符串，OneBot 使用整数。到达上限必须显式迁移，禁止截断、循环和重启重分配。
- conversation_id 为不透明 UUID，仅原生协议使用；成员和群 ID 的绑定由服务端持久化，客户端不能自行拼接。
- event_id、client_message_id 为 UUID。seq 为当前账号可见的持久单调序号，以十进制字符串传输。
- 原生时间为 UTC RFC3339，例子用毫秒精度；OneBot time 为 Unix 整秒。
- 每个 bot 连接绑定 self_id，跨重连/重启稳定；旧 A.E.B 的固定机器人身份不能代替真人账号。

## 原生 API 清单

前缀 `/api/v1/chat`，HTTPS/WSS。此表包括已实现子集和未来目标，以前述范围和实际 OpenAPI 为准。

| 方法 | 路径 | 语义/权限 |
| --- | --- | --- |
| POST | `/auth/register` | 邀请注册；开放注册策略另定 |
| POST | `/auth/login` | 账号密码与 device_id，创建设备会话 |
| POST | `/auth/refresh` | 原子轮换刷新令牌 |
| POST | `/auth/logout` | 撤销当前设备会话及令牌家族 |
| GET/PATCH | `/me` | 本人资料；不允许写角色或他人 ID |
| GET | `/conversations` | 当前账号的会话与未读 |
| POST | `/conversations/direct` | 按 target_user_id 创建/获取允许的私聊 |
| POST | `/groups` | 建群并创建对应会话 |
| GET/POST | `/groups/{group_id}/members` | 查询/邀请成员，写操作按群权限验证 |
| DELETE | `/groups/{group_id}/members/{user_id}` | 退群/移除成员；群主转让流程另定 |
| GET/POST | `/conversations/{id}/messages` | 分页历史/发送；仅允许会话成员 |
| POST | `/conversations/{id}/read` | 已读推进，验证 message_id 所属会话 |
| POST | `/messages/{message_id}/recall` | 本人或有权群管理撤回 |
| GET | `/sync?after={cursor}&limit={n}` | 账号可见持久事件，含 next_cursor/has_more |
| WS | `/events?after={cursor}` | 鉴权、恢复与实时事件 |
| POST | `/media` | multipart 上传，返回 media_id/受控元数据 |
| GET | `/media/{media_id}` | 鉴权下载或短期票据，不暴露磁盘路径 |
| GET/POST | `/posts` | 查询/发布真人日志 |
| PATCH/DELETE | `/posts/{post_id}` | 作者修改/删除；管理规则另定 |

本人资料首版为 display_name、avatar_media_id、bio。日志正文为纯文本和附件；visibility 为 self/contacts。联系人接口在 P4 前补齐，未实现时仅启用 self，contacts 不能解释为全站公开。日志没有 OneBot 核心 API 映射。

## 认证与错误

- HTTP/Flutter IO WS 使用 `Authorization: Bearer <access_token>`，令牌不放 URL。
- 不透明随机令牌绑定账号、设备和期限；服务端只存散列。刷新轮换；重用旧刷新令牌时撤销该会话。
- 登出撤销整条设备会话并关闭 WS。令牌过期/撤销时 WS 关闭码 4401，刷新后携带最后持久游标重连。
- 业务错误：`{"error":{"code":"forbidden","message":"Permission denied","request_id":"..."}}`。
- HTTP 状态包括 400/401/403/404/409/413/429/503；native 不套用 OneBot retcode。
- bot 凭据只授其账号操作和会话可见范围，不共享真人 token、管理 token 或具身动作授权。

## 消息与投递

持久消息结构见 `native-message.schema.json`。发送请求形状：

```json
{
  "client_message_id": "00000000-0000-4000-8000-000000000301",
  "segments": [{"type": "text", "data": {"text": "你好"}}]
}
```

同账号 client_message_id 全局唯一；相同会话/内容重试返回原消息，跨会话或内容冲突返回 409。发送为 1..16 段，所有 text 合计不超过 4000 个 Unicode 码点。mention 仅用于群聊，目标必须是当前群成员；user_id="all" 仅群主或管理员可发。reply 至多一段且必须位于首段，message_id 必须属于同一会话且未撤回；不能只发 reply，需同时含文字或提及。原生 ID 必须为规范十进制字符串且不超过 2147483647。

事务先验证发送者的当前会话权限，再识别相同幂等请求，最后为新消息验证提及/引用目标并入库。已接受请求的同内容重试仍返回原消息，即使其引用目标后来撤回；新请求不得引用已撤回消息。引用只保存 message_id，不复制旧正文；客户端解析已加载历史，撤回后显示 tombstone，未加载时显示明确占位。

消息记录、幂等键、账号投递事件和 outbox 在同一事务提交。HTTP 成功表示服务端已接受，不等于对方已读。bot 离线不能阻止真人消息入库。

撤回保留 tombstone；recalled=true 时 segments 为空，客户端删除旧正文显示。消息编辑与音视频段后续定义。附件上传并完成权限验证后才能发消息，未引用上传按策略清理。

未知或不支持的发送段返回明确失败；接收新版本未知段时客户端显示占位，不崩溃或丢掉其他内容。

## 同步与离线恢复

事件结构见 `native-event.schema.json`。每账号一条持久事件序列，多设备共用，发送者自己的设备也收到事件。

事件类型：message.created、message.recalled、conversation.updated、read.updated、profile.updated、post.created、post.updated、post.deleted。除 message 相关 payload 外，其余模型在对应阶段补齐后冻结。

- `/sync` 和 WS 使用相同游标语义；客户端按 event_id 去重，按 seq 应用并在本地事务提交成功后保存 cursor。
- 恢复必须采用原子游标屏障或等价设计，覆盖“补拉到上界”和“转入实时订阅”之间的窗口。
- WS 允许重复事件；慢客户端超过有界缓冲后断开补拉，禁止无限队列。
- cursor 不透明、绑定账号；跨账号游标拒绝。过期返回 HTTP 409/code=sync_reset_required，客户端重取快照与新游标。
- 新设备获取会话快照、近期消息和一致性游标，较老历史另行分页，不能假定初次同步包含全部历史。
- OneBot 投递使用独立 outbox，原生真人恢复不依赖 bot 当前连接状态。

## OneBot11 映射

| 原生领域 | OneBot11 |
| --- | --- |
| bot 账号 ID | self_id / 反向 WS X-Self-ID |
| 真人发件人 | user_id 与 sender.user_id |
| 群 ID / 权限 | group_id / sender.role |
| 消息 ID | message_id / get_msg / delete_msg 共用映射 |
| text.data.text | text.data.text |
| mention.data.user_id | at.data.qq；all 对应 qq="all" |
| reply.data.message_id | reply.data.id |
| image.data.media_id | image.data.file，入站附允许读取的短期 URL |
| 入退群/撤回 | 对应 notice，按 bot 可见范围生成 |

反向 WS：EX 为连接发起方，AstrBot 为监听方。首版选 Universal，设置 X-Client-Role: Universal、X-Self-ID 和 access token 鉴权 header。EX 发事件、接 action、回传原样 echo；支持并发、生命周期事件与心跳。重连不能更换 self_id。

成功响应 status=ok、retcode=0，返回实际 data 与 echo。未知 action 在 WS 使用 status=failed、retcode=1404、data=null。参数/权限/存储等业务错误码在实现时冻结，不返回虚假 success。echo 可以是任意 JSON 类型，不能强制变成字符串。

优先 action：get_login_info、get_version_info、get_status、get_friend_list、get_group_list、get_group_info、get_group_member_info、get_group_member_list、send_private_msg、send_group_msg、send_msg、get_msg、delete_msg、get_stranger_info、can_send_image、get_image。联系人语义在 P1 冻结。

get_version_info 必须真实返回 app_name=astrEX、实际版本与 protocol_version=11。自有扩展用 `_ex_` 前缀。不能假冒 NapCat。

禁言/踢人 P2 先验证权限后实现；合并转发、群文件和 NapCat 扩展按真实插件依赖增加。QQ cookies/CSRF/空间等腾讯服务不承诺在自有平台实现。

bot 自发消息正常入库，但默认不向同一 bot 再生成入站消息事件，避免自回复回环。

OneBot 无整个机器人消费链的持久已处理确认；重连窗口可能重复/遗漏。EX 记录 outbox 投递尝试并定义重试上限/期限，不能承诺原插件端到端恰好执行一次。手机游标恢复独立验收。

## 媒体边界

手机用 media_id 和鉴权读取；bot 获得绑定媒体的短期签名 URL，可通过鉴权 action 重新取得。票据不授登录或其他媒体权限。

bot 发图先支持受控上传、base64 和受限 URL；file:// 只允许显式的可信本地根目录，禁止任意路径。远程抓取限制协议/目标网段/重定向/大小/超时。

## Schema 证明范围

Schema 只检查形状、枚举与基础格式。ID 数值上限、权限、事件与消息一致性、恢复窗口、媒体访问和真实插件行为需另测。示例通过不代表任何 API 或插件已实现。
