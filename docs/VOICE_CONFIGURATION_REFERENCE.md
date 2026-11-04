# 语音配置参考（跨电脑版）

所有路径相对于解压后的项目根目录。新电脑先运行 deploy/nano_voice/prepare_deployment.py 或 setup_windows.ps1，随后编辑生成的本地文件。这里不包含原电脑的账户 ID 或密钥。

## 模型 URL

| 环节 | Base/完整 URL | 模型与鉴权 |
|---|---|---|
| MiMo ASR | https://api.xiaomimimo.com/v1；客户端补 /chat/completions | mimo-v2.5-asr；MiMo Key |
| MiMo TTS | https://api.xiaomimimo.com/v1；客户端补 /chat/completions | mimo-v2.5-tts；MiMo Key；流式 PCM |
| Cloudflare 决策 | https://api.cloudflare.com/client/v4/accounts/ACCOUNT_ID/ai/run/@cf/cloudflare/clef | clef；Cloudflare Workers AI Token |
| AstrBot 聊天 | http://127.0.0.1:6185；客户端补 /api/v1/chat | AstrBot chat Key；selected_provider=deepseek/deepseek-flash |
| AstrBot 上游 DeepSeek | https://api.deepseek.com/v1；Chat Completions | deepseek-flash；DeepSeek Key 在 AstrBot 中填写 |

上述请求均为 POST。最终 ASR 非流式；AstrBot 回复和 MiMo TTS 使用 SSE。所有 Key 都是不同服务的凭据，不能互换。

## 本地配置和前置文件

| 路径 | 内容/用途 |
|---|---|
| deploy/local/data/plugins/special/astrbotex_voice_jev/config.json | 已安装插件的实际主配置；不要只修改源码目录的打包默认配置 |
| deploy/nano_voice/config.mimo.local.json | stt/tts backend、api_base、model、voice、timeout、凭据引用 |
| deploy/nano_voice/mimo.credentials.local.json | stt、tts 两项 Key；MIMO_API_KEY 可覆盖 |
| deploy/nano_voice/astrbot.chat.local.json | base_url、username、api_key、token_env、timeout |
| deploy/nano_voice/voice.gate.local.json | type、url、model、api_key/token_env、门槛和超时 |
| deploy/nano_voice/models/vosk-model-small-cn-0.22/ | 完整本地字幕模型，本包已带 |
| deploy/nano_voice/.venv/ | 目标主机自己安装的环境，本包不带 |
| deploy/local/astrbot-data/ | 使用本包 Compose 时的 AstrBot 数据目录，新电脑自己生成 |

当前迁移模板采用本地文件保存 Key；不需要 cloudflare.credentials.local.json。若选择 Windows DPAPI 加密文件方式，需在目标登录用户下重新生成 token_dpapi，并绑定 allowed_origin=https://api.cloudflare.com。旧电脑的密文不能直接迁移。

MiMo 凭据文件路径相对于 speech 配置目录；决策凭据文件路径相对于 gate 配置目录。Python、Vosk、speech/chat/gate 路径使用目标主机的绝对路径。

## 主配置重要字段

| 字段 | 默认/作用 |
|---|---|
| python_executable | 本机音频虚拟环境 Python |
| vosk_model_path | 本机 Vosk 模型目录 |
| speech_config_path / chat_config_path / gate_config_path | 上述三份本地配置 |
| provider_id | deepseek/deepseek-flash；需与 AstrBot 实际 provider ID 一致 |
| decision_mode | http；也可选 topic 外部裁决 |
| decision_url | 默认空，继承 gate 文件连接；直接填写后使用 EX 的 decision_type/model/token_env |
| decision_threshold / forward_threshold | 0.4 / 0.4；覆盖 gate 文件中的同项 |
| quiet_threshold | 0.85 |
| decision_timeout | 3 秒 |
| decision_stage | final：最终识别后决策 |
| input_device / output_device | null=系统默认；前端保存时为 {id,name,host_api} 描述符 |
| streaming_enabled | false；通过唯一连续对话开关开启 |

EX 的默认 decision_type=systemone 在 URL 为空且有 gate 文件时不表示实际使用 System One，实际取 gate 中的 cloudflare。前端会显示有效的决策身份。

设备选择可在前端调整；deploy/nano_voice/audio.devices.local.json 只供独立桌面窗口使用，不是 EX 的必需文件。历史 provider_mimo_stt.json、provider_mimo_tts.json、stt_tts_settings.json、config.patch.example.json 不是当前 worker 的运行前置。

## 前端接口

统一前缀：http://127.0.0.1:8765

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/v1/ex/voice/status | 当前模型、状态、日志、安全配置 |
| GET | /api/v1/ex/voice/devices | 音频主机设备枚举 |
| POST | /api/v1/ex/voice/devices/check | 格式检查，不录音播放 |
| POST | /api/v1/ex/voice/config | {"config": {...}} 局部保存 |
| POST | /api/v1/ex/voice/streaming | {"enabled": true/false} |
| POST | /api/v1/ex/runtime/start | 启动运行时 |
| POST | /api/v1/ex/runtime/stop | 停止运行时 |

没有额外的浏览器 WebRTC 收音服务。真实设备属于音频 worker 主机；外部决策协议见 VOICE_DECISION_API.md，设备契约见 VOICE_AUDIO_DEVICE_API.md。
