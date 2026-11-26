# 连续语音面板与外部插件接口

`/#/voice` 在原有交互指标和手动测试下方显示连续语音面板。主干提供页面、管理鉴权及通用适配接口；录音、播放、声卡、VAD、模型文件、系统凭据和子进程均由单独安装的插件管理。主干不附带语音设备插件，也不绑定插件 ID、声卡后端或机体。

先安装兼容插件，在插件页配置设备主机的路径和依赖并启用，再在语音页选择插件。开启连续对话要求 EX 运行时已经运行；打开页面、查看状态或保存设置均不启动采集。多个插件必须明确选择，接口请求通过 `plugin_id` 指定目标。禁用或卸载插件沿用框架现有生命周期。

## 外部插件契约 v1

插件在 `plugin.json` 声明 `"voice_dashboard": true`，沿用现有 provides 类型、Actor 和生命周期。声明必须为布尔值，默认 false。插件实现以下方法：

- `get_voice_status()`：返回 `state`、`streaming_enabled`、`worker_running`、`runtime_active`、`pending_decisions`、`last_error`，以及可选 `models`、`decision_profile` 和 `events`。状态方法不得启动设备或模型请求。
- `set_streaming(enabled: bool)`：明确启动或停止连续对话；插件负责取消采集、生成和播放，并在 runtime stop、disable、unload 时停止。
- `query_audio_devices(request)`：处理 `action=list/validate`，返回含严格布尔 `ok` 的对象；插件自行实现设备枚举和格式检查，不采集或播放音频。

`models` 可包含 stt、decision、chat、tts，每项可报告 model、configured、last_check_ok、checked_at；只报告公开身份。`decision_profile` 报告 type、model。`events` 为最近事件数组，最多向页面返回 80 项，每项包含稳定 id、服务端 timestamp、type、payload。页面兼容完整 reply（partial != true）、status 中“你：”开头的转写，以及 gate_result 中明确的整句；partial/status 调试内容保留在原始日志。内容按文本渲染，聊天显示和导出保留最近 400 条。

设备列表 `devices` 每项包含 id、name、host_api、input_channels、output_channels 和可选 label/selectable。设备身份来自插件，id 不限定 PortAudio 前缀。选择保存为 `{id,name,host_api}` 或 null；插件负责判断已保存设备是否仍可用，不能静默换成其他设备。

## 管理 API

新插件优先用 `type=transcript` 发送完整对话，payload 含 `role=user/assistant`、`text`、`partial=false`，无需依赖特定语言的日志前缀。旧插件的 reply/status/gate_result 格式继续兼容。

这些路由沿用主干管理凭据、同源和 Host 校验。GET 可用 `?plugin_id=...`，POST 在 JSON 对象中传 `plugin_id`。仅有一个兼容插件时可省略。

| 方法 | 路径 | 请求 |
| --- | --- | --- |
| GET | `/api/v1/ex/voice/status` | 返回 providers、所选插件状态、公开配置、模型身份与事件 |
| GET | `/api/v1/ex/voice/devices` | 委托所选插件枚举设备 |
| POST | `/api/v1/ex/voice/devices/check` | `{plugin_id, input_device, output_device}` |
| POST | `/api/v1/ex/voice/config` | `{plugin_id, config: {...}}`，运行中拒绝保存 |
| POST | `/api/v1/ex/voice/streaming` | `{plugin_id, enabled: true/false}` |

配置仅允许服务、阈值、模式及设备身份等通用字段；插件自己的 schema 继续校验值。Python、Vosk 和凭据文件路径通过插件自己的配置页管理。状态接口不返回 decision_api_key，仅返回 decision_api_key_configured。页面 Key 留空不修改原凭据；显式 API 更新为空字符串可清除它。配置更新强制 streaming_enabled=false，避免重载后恢复采集。主干不读取外部插件的本地凭据或模型检查文件。

清空记录按服务端事件水位保存，并为各插件分别保存在当前浏览器来源的 localStorage 中；重新轮询或刷新不会恢复已清空的旧事件。切换插件清空当前视图，避免混入另一套设备的记录。

## 验证

`python -X utf8 -m pytest tests/test_voice_dashboard.py tests/test_voice_dashboard_http.py tests/test_portable_voice.py tests/test_api_server.py tests/test_local_plugins_config.py -q`

`python -X utf8 scripts/verify_voice_dashboard.py --output <目录>` 使用已安装的 Playwright/Chromium，在临时数据目录运行真实管理服务及假语音插件，验证面板、配置与事件行为；不使用真实声卡或收费模型。它生成桌面和手机宽度截图及结果记录。
