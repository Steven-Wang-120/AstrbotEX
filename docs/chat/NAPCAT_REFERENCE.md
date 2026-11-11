# NapCat 参考记录

核对日期：2026-11-10。官方仓库 `NapNeko/NapCatQQ` 已克隆到本机 `D:\Code\NapCatQQ`，本轮获取时 main 提交 `72912f293924f43463b5976b01fc094a8ae11481`。本机 Docker 部署未改动。

| NapCat 源码位置 | 观察 | EX 对应决策 |
| --- | --- | --- |
| packages/napcat-onebot/action/OneBotAction.ts、router.ts | 注册 action、校验、统一响应/echo | ActionDispatcher 与业务服务分开；保持原样 echo 和明确失败 |
| packages/napcat-onebot/network/websocket-client.ts | Universal header、反向连接、生命周期/心跳、重连 | EX 主动连接 AstrBot，配置固定 self_id/token |
| packages/napcat-onebot/config/config.ts | 网络类型分别配置启用、地址、鉴权、心跳 | 首版只实现一个反向连接，通过 EX 环境配置 |
| packages/napcat-webui-backend/src/api/OB11Config.ts | 读取/校验/保存协议配置 | 维护配置留 EX，和真人会话隔离 |
| packages/napcat-webui-frontend/src/pages/dashboard/config/onebot.tsx | 协议设置表单与状态反馈 | 手机不放协议维护表单，仅放真人功能 |

EX 使用自己持有的账号、群、消息和持久事件，OneBot 调用统一 ChatService。QQ/NTQQ 只属于 NapCat 的平台实现，不进入自有网络。没有接入 NapCat Docker、没有复制或嵌入 NapCat 源码，也未添加其运行依赖。参考来源中的自定义许可保留在原仓库。

兼容目标是 OneBot11 的真实语义及插件实测；不会因接口同名就声称 NapCat 全量兼容。当前 @、引用、图片、扩展、notice、真实插件对照仍待实现/测试。


2026-11-11 补核同一已克隆版本的 OB11FriendRequest.ts、SetFriendAddRequest.ts。新增 friend request/approval/notice 映射；EX 使用自己的公网身份账号收发，再适配为 OneBot 数字 ID。保留 echo、Universal反向WS和生命周期/心跳；没有引入QQ/NTQQ依赖。已用真实AstrBot /sid跑通公网私聊，仍不代表NapCat所有扩展兼容。
