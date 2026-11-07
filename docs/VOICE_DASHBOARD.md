# EX 前端连续语音集成

现有 `dashboard/#/voice` 页面已集成语音插件，保留原 AstrBot 代理自检。连续语音面板使用独立进程完成持续收音、MiMo 最终识别、通用决策、AstrBot DS Flash 对话和流式 TTS。生成与播放期间继续收音；背景话语保留在有界会话内存，决策通过后才提交回答。当前仍按语音停顿分段，不是原生音频到音频模型。

## 本机启动

运行 `deploy/nano_voice/start_local_ex.ps1`，访问 http://127.0.0.1:8765/#/voice 。先在总览启动 EX 运行时，再到语音页刷新设备、检查格式并开启连续对话。停止后可选择独立的输入/输出设备，保存阈值（默认 0.4）、决策协议/URL/模型和高级运行路径。

安装脚本将白名单插件文件部署到被忽略的 `deploy/local/data/plugins/special/astrbotex_voice_jev`，仅通过路径引用现有本地凭据文件，不复制密钥到插件包。重复安装保留已保存设置并将收音设为关闭。源插件默认仍禁用，不会因打开页面自动收音。

决策 URL 留空且设置本地 gate 配置文件时，继承文件内的服务和凭据；页面阈值仍覆盖文件阈值。直接填写 URL 后，使用页面的通用决策协议设置。输入和输出来自音频进程所在主机，浏览器所在设备不会自动成为远端 Nano 的音频设备。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/ex/voice/status` | 插件、运行时、当前决策身份、安全配置与最近 80 条事件 |
| GET | `/api/v1/ex/voice/devices` | 枚举所有主机音频后端设备 |
| POST | `/api/v1/ex/voice/devices/check` | 检查 `{input_device, output_device}` 格式，不录音播放 |
| POST | `/api/v1/ex/voice/config` | 保存 `{config: {...}}` 局部配置；连续语音开启时拒绝 |
| POST | `/api/v1/ex/voice/streaming` | `{enabled: true/false}`，启动需运行时已运行 |

设备选择保存 `{id,name,host_api}` 身份，缺失设备保留原选择并拒绝悄悄切换；同名同后端歧义设备须先在系统改名。驱动安装和硬件发现由实际设备主机负责。详见 `deploy/nano_voice/AUDIO_DEVICE_API.md`。

旧“链路自检”检查 EX 的 AstrBot STT/TTS 代理，和连续语音插件的 MiMo 配置不同；以连续语音面板状态和事件判断新链路。

连续语音面板现显示实际 STT、决策、对话、TTS 四个模型及最近实测结果。可运行音频环境中的 Python 执行 `deploy/nano_voice/check_local_models.py deploy/local/data/plugins/special/astrbotex_voice_jev/config.json` 重新检查。检查真实调用已配置的云端接口，在内存和临时目录使用合成样本，结束后清理音频，不采集麦克风、不播放扬声器。结果仅保存模型身份、成功状态和耗时，不保存凭据或模型回复。

## 验证范围

覆盖既有语音链路回归、设备选择、真实临时插件管理器/actor及 HTTP 接口，并由独立 subagent 验收。实际本机 HTTP 枚举 20 个端点、默认设备格式检查通过，没有录音播放。未验证新接入外置设备热插拔和本次真实人声端到端体验。桌面浏览器自动化受本机 E 盘错误阻碍，页面布局仍需人工确认。
