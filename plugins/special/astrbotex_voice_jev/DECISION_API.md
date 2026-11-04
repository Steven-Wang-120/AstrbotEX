# 通用语音决策接口 v1（0.6.1）

不使用名字列表、名字前缀、唤醒词或必须点名恢复的条件。决策服务根据当前发言和有界近期上下文判断是否需要回答。未回应的内容仍保留背景记忆，录音和流式播放继续沿用原链路。

## 前端配置字段

已有 EX 插件配置 API 可以保存以下 schema 字段，不需要修改 Python 源码。保存后按 EX 的正常配置重载/停用再启用流程生效；不要在已有会话中悄悄切换服务。

| 字段 | 用途 |
| --- | --- |
| decision_mode | http（URL服务）或 topic（EX外部裁决） |
| decision_type | systemone、laya、cloudflare、http_json、openai |
| decision_url | 用户填写 URL |
| decision_model | 可选模型；Laya 默认 multilingual，OpenAI兼容接口必填 |
| decision_token_env | 可选 Bearer Key 的环境变量名，默认 DECISION_API_KEY |
| decision_json_mode | OpenAI兼容服务的 JSON 输出模式开关 |
| decision_timeout | 整次决策等待预算，默认3秒，范围0.2～30秒 |
| decision_threshold / forward_threshold | 对助手发言/需要回答的概率门槛，默认0.40/0.40 |
| quiet_threshold | 独立静默门槛，默认0.85，不随对话判定阈值降低 |
| decision_stage | final默认先最终识别再单次决策；local_first兼容本地转写先判断 |
| decision_source / decision_topic | Topic模式裁决来源与topic，默认 decision / voice.decision |
| gate_config_path | 可选本地配置文件；用于凭据和默认值。decision_url非空时使用前端这组直接字段，URL为空时使用本地文件。纯URL配置无需填写它 |

默认收音关闭。音频Python/模型路径/MiMo/AstrBot配置为已有部署条件；填写决策URL只是在已有语音系统中替换门控服务。

HTTP可填写localhost或私网IP，远程服务使用HTTPS。拒绝URL内嵌用户名/密码，不跟随重定向，不把服务端错误正文或凭据输出到日志。Windows加密凭据绑定服务源，改URL不会把原Cloudflare凭据自动发给另一个域名。密钥环境变量由运行worker的主机/容器提供，不是浏览器环境变量。

## 五种协议

| 类型 | URL示例 | 行为 |
| --- | --- | --- |
| laya | http://127.0.0.1:8000 | 自动补 /v1/systemone；默认model=multilingual，max_len=8192 |
| systemone | https://your-service.example/v1/systemone | 发送 state/questions，可选model，解析 answers 中的三个 noul |
| cloudflare | https://api.cloudflare.com/client/v4/accounts/ACCOUNT/ai/run/@cf/cloudflare/clef | 完整Workers AI Clef URL，解析Cloudflare result外层；需要Bearer Key |
| http_json | https://your-service.example/voice/decide | 使用下面的标准JSON契约 |
| openai | http://127.0.0.1:1234/v1 | 自动补 /v1/chat/completions；非流式、JSON分类结果，需填写model |

仅空根路径或 /v1 会自动补齐常用路径。自定义路径原样保留，不会猜测网关的路径；此时请填写完整调用URL。OpenAI兼容指 Chat Completions 协议，不包括 Responses API；服务不支持 response_format 时可关闭 decision_json_mode。

Laya 项目：https://github.com/NandhaKishorM/laya 。官方自托管HTTP服务支持 POST /v1/systemone，兼容JEV的typed questions格式；可选 LAYA_API_KEY 开启Bearer鉴权。先在适合运行模型的独立环境启动 laya-serve，再把URL填进插件。仅安装这个EX插件不会下载或启动Laya模型。当前验证了协议适配和本地HTTP请求，未部署真实Laya权重，因此不宣称已验证Laya的判断准确率或Nano推理性能。建议使用默认非strict服务模式，避免截断信号被服务端隐藏；客户端会拒绝显式截断和abstention。

## http_json 请求与响应

POST decision_url，Content-Type: application/json；request_id 每次请求唯一。

```json
{"schema_version":"voice-decision.v1","request_id":"request-123","state":{"latest_utterance":"那第二种方法怎么做？","recent_assistant_exchange":[{"user":"介绍两种识别方法","assistant":"第一种颜色识别，第二种目标检测。"}],"recent_background_speech":[],"active_exchange":true,"quiet_requested":false,"speaker_identity_and_gaze":"unknown"},"questions":{"addressed":{"type":"noul","instructions":"..."},"forward":{"type":"noul","instructions":"..."},"quiet":{"type":"noul","instructions":"..."}}}
```

可返回三个严格布尔值：

```json
{"request_id":"request-123","addressed_to_assistant":true,"should_reply":true,"should_stop":false}
```

或者三个0～1数值概率：

```json
{"request_id":"request-123","scores":{"addressed":0.97,"forward":0.95,"quiet":0.02}}
```

request_id 在HTTP响应中可省略；一旦提供必须匹配当前请求。只有 addressed 和 forward 都达到门槛且未请求静默才回答；quiet 达到门槛且 addressed 合格时取消旧播报并保持收音。不能只返回 allowed=true，不能把字符串true当布尔值。字段缺失、NaN、越界、过期及错误全部不回答，不自动切换另一服务绕过裁决。

机器可读schema在 decision-api.schema.json 的 #/$defs/request 和 #/$defs/response。

## Topic外部组件

为保留已安装插件的状态/控制动作兼容性，插件ID暂保留 astrbotex_voice_jev；这个ID不再限制决策提供商。仅流式输入开关 set_streaming。

订阅 astrbotex_voice_jev.candidate：

```json
{"request_id":"request-123","epoch":"worker-epoch","state":{"latest_utterance":"然后呢？","active_exchange":true},"ttl_ms":3000}
```

将裁决发布到配置的 decision_topic，消息 source 必须匹配 decision_source：

```json
{"request_id":"request-123","epoch":"worker-epoch","addressed_to_assistant":true,"should_reply":true,"should_stop":false}
```

三项必须全为严格布尔值，request_id和epoch匹配且在TTL内。错误来源、旧代次、重复或迟到裁决不会放行。source校验是组件协议，不是针对恶意插件的权限隔离。

## 运行边界

活动对话中的完整“先别回答/别说了”可本地立即静默，无需名字。静默之后的新明确请求仍交由配置的决策服务判断；不要求叫名字。没有活动对话时不靠本地静默正则猜测对象。引用、解释静默短语等仍交给上下文判断。

文字没有声纹、注视或真实朝向证据；服务需更可靠地识别对象时可在自身URL服务中融合这些信号。前端不应把拾音本身当成对助手说话。URL接口可传给任意遵守这些协议的服务，无须以JEV命名。
