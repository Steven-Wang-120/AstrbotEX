# 可复用语音核心与设备插件边界

本分支从 PR #6 的提交 `36751f8352a846928c99f3ef96922550482fa2a0` 提取通用逻辑，接入主干已有 STTProvider / TTSProvider 接口。主干提供模型服务协议、文字与音频数据处理；实际录音和播放由设备插件执行。

## 归属

| 主干通用模块 | 设备插件或部署环境 |
| --- | --- |
| 语音决策 HTTP 协议、超时、取消、严格响应校验 | 声卡选择、麦克风、扬声器、USB/蓝牙/ROS 音频接入 |
| 对话对象判断、独立静默阈值、近期对话和有界背景记忆 | VAD/本地字幕引擎、Vosk 权重与模型路径 |
| MiMo STT/TTS 请求与响应、WAV 校验、流式 PCM 解码 | PortAudio、sounddevice、驱动、系统权限与音频重采样 |
| 文本分句和口语化清理 | 收音/播放工作线程与进程、设备生命周期、回声处理 |
| 现有 InteractionCore 与 provider 接口 | Python 可执行文件路径、主机启动脚本、设备清单 API |
| 服务 URL、模型、阈值等通用参数与接口说明 | 具体主机的配置值、密钥存储、Windows DPAPI、安装包 |

可复用模块只依赖 Python 标准库，不需要导入 sounddevice、vosk、ctypes 或插件实现。运行仍需 Python 3.10+、有效模型服务与网络连接；本分支验证不表示已在所有硬件和系统实机验收。

这些文件没有将设备能力硬编码到主干：不提取 PR 中绑定 `astrbotex_voice_jev` 的 VoiceDashboard、设备 HTTP 路由或整套前端面板。原有 API 和默认运行配置不变。

## 决策与会话

```python
from astrbot_ex.core.voice import ConversationGate, ConversationMemory, DecisionClient

client = DecisionClient({
    "type": "http_json",
    "url": "https://decision.example/voice/decide",
    "token_env": "DECISION_API_KEY",
    "timeout": 3,
})
gate = ConversationGate(client, threshold=0.4, forward_threshold=0.4, quiet_threshold=0.85)
result = gate.evaluate("请介绍一下这两种方法")
# result 包含 allowed / quiet / scores / thresholds / captured_at。
# 调用方根据结果提交聊天、停止播报或保留背景。
```

支持 systemone、laya、cloudflare、http_json、openai 五种决策协议。此处 openai 指兼容 Chat Completions 的协议。客户端提供 `evaluate(state, cancel_event=None)`；ConversationGate 在其上维护有限近期上下文，并在现有对话内识别直接静默请求。它是语音回应判断，与机器人动作执行的 decision/安全门无关。

http_json 可返回 `scores: {addressed, forward, quiet}`（有限的0～1数值），或严格布尔值 `addressed_to_assistant / should_reply / should_stop`。仅 allowed=true、缺字段、字符串布尔值、NaN、越界、截断、弃权、错误与过期响应均不能放行。request_id 在响应中提供时必须匹配请求。

URL 只接受 HTTPS，或 localhost/私网 IP 的 HTTP；不跟随重定向。凭据由 api_key 或 token_env 提供。机器专属密钥库由设备适配器解密后传入，核心不读取 Windows DPAPI 文件。

ConversationMemory 提供 max_items / max_age / max_chars 有界背景记录及版本确认；voice.text 的 SentenceBuffer / spoken_text 供流式播报适配器复用。这些对象应按语音会话创建，不应跨用户共享。

## STT/TTS

```python
from astrbot_ex.core.providers.mimo_providers import MiMoSTTProvider, MiMoTTSProvider

stt = MiMoSTTProvider({"model": "mimo-v2.5-asr", "api_key_env": "MIMO_API_KEY"})
tts = MiMoTTSProvider({"model": "mimo-v2.5-tts", "api_key_env": "MIMO_API_KEY"})
# 组装 InteractionCore 时传入 stt_provider=stt, tts_provider=tts。
# 主干现有 AstrBot/A.E.B provider 保持原样；这个分支不自动切换默认服务。
```

STT 的 `await get_text(audio_url)` 接收捕获适配器提供的本地 PCM WAV 文件，支持16bit、单/双声道；它不录音，也不下载远程文件。路径是当前进程可读取的音频数据输入，不是绑定某台电脑的配置。

TTS 的 `await get_audio(text)` 返回临时 WAV 路径，沿用现有 provider 的文件所有权约定：InteractionCore 发布播放消息，由消费端根据 delete_after_play 删除。失败时 provider 清理尚未交出的临时文件。

`tts.iter_pcm(text, cancel_event=...)` 可直接输出24kHz、单声道、PCM16LE字节块。块边界不保证恰好对齐采样，接收方应保留半个采样；总流长度会校验。插件负责缓冲、重采样、停止和声卡播放。这是显式字节迭代接口，不改变现有 TTSProvider 的队列流接口或 support_stream 标志。

两种 provider 的 settings 可提供 api_base / model / timeout / api_key_env / api_key；MiMo 特有语言、音色等字段沿用已提取协议实现。api_key 优先于环境变量。核心不会复制部署机器的凭据文件。

## 验证

`python -X utf8 -m pytest tests/test_portable_voice.py -q`

测试使用本地 HTTP/SSE 模拟服务与合成 WAV，不需音频设备、Vosk、密钥或收费模型调用。覆盖五种决策协议、非法响应拒绝、会话静默/恢复、有界背景、provider 与 InteractionCore 集成、失败清理、PCM取消和不完整流拒绝，以及无硬件/平台凭据依赖的导入检查。

设备插件原有部署文件完整性、聊天面板去重问题，应在插件后续 PR 自己的验收范围内解决。
