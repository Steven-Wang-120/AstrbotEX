# 外置音频设备接口（0.7.0）

## 已实现的范围

USB麦克风、USB音响、蓝牙音频设备、无线麦克风接收器、3.5mm声卡输入输出等，只要系统把它们暴露为PortAudio可见的音频端点，就使用同一套枚举/选择接口。输入与输出可以来自不同设备。程序不按MME过滤，保留Windows的MME/DirectSound/WASAPI/WDM-KS，以及Linux环境可见的ALSA等后端。设备功能以系统报告的输入/输出通道为准，不按名称猜测它一定是USB、蓝牙或物理设备。

scope固定为audio_worker_host：列表属于实际执行音频Python的主机/容器，不是打开网页的浏览器所在电脑。在Nano上运行worker得到Nano设备；Docker需映射音频设备；远程网页的麦克风若未暴露给worker，需另做浏览器/WebRTC或网络音频通道，本版没有该传输通道。

当前交互窗口已有录音输入/播放输出下拉框，以及刷新设备、检查格式、保存设备按钮。运行中继续保留0.4对话阈值与持续收音功能；切换设备前先停止，避免重置正在工作的音频流。

## 驱动在什么位置

驱动由连接设备的操作系统负责，不部署在网页前端。符合USB Audio Class的设备可使用系统类驱动：Windows提供USB Audio类驱动，Linux内核提供snd-usb-audio；厂商专有设备可能需要厂商驱动、固件或对应Linux内核支持。蓝牙需要系统蓝牙栈、配对及可用的音频配置。3.5mm耳机/音响通常显示为电脑已有声卡的端口，而不一定新增一个USB设备。

- Windows官方资料：https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/usb-2-0-audio-drivers
- Linux内核ALSA配置资料：https://docs.kernel.org/sound/alsa-configuration.html
- 音频后端查询/格式检查API：https://python-sounddevice.readthedocs.io/en/0.5.3/api/checking-hardware.html

插件不自动下载/安装厂商驱动。列表缺少某设备不能直接认定缺驱动，也可能是没有连接、USB线仅供电、未配对、权限、音频服务、后端不支持或容器未映射设备。串口/网络模块也不会自动变成系统声卡。

## 后端接口：真实可调用的SDK/JSON入口

这一版预留的是后端SDK、JSON子进程入口和EX Topic契约，未新增独立HTTP服务器或假定某个设备REST路由已存在。后续网页前端由自己的后端调用这些入口，再把JSON结果返回浏览器即可；EX保存选择仍使用已有配置API。

### Python SDK

```python
from audio_devices import request

catalog = request({"action": "list", "request_id": "device-list-1"})
check = request({
    "action": "validate",
    "input_device": "从catalog选择的输入id",
    "output_device": "从catalog选择的输出id",
})
```

SDK需要运行在有sounddevice/PortAudio的音频主机上。枚举与格式检查不录音、不播报、不安装驱动。

### EX插件后端hook

```python
catalog = plugin.query_audio_devices({"action": "list"})
check = plugin.query_audio_devices({"action": "validate"})
```

query_audio_devices使用配置的python_executable启动独立设备查询进程，默认8秒预算；不启动完整语音链路，也不加载Vosk/MiMo/大模型。validate未指定输入输出时检查插件当前配置，显式传null表示系统默认。后端线程可调用这个同步hook；异步Web服务请放入自己的执行器，避免阻塞事件循环。

独立子进程也让EX在当前语音worker运行时查询新设备，而不重新初始化现有音频流。

### CLI JSON

在音频Python环境中把请求写入标准输入：

```text
python audio_devices.py
```

输入一条JSON后关闭stdin：

```json
{"action":"list","request_id":"device-list-1"}
```

程序向stdout返回一个JSON对象。validate使用相同入口并填写input_device/output_device。

## 列表与选择语义

返回示意（id应使用实际返回值，不要手工照抄占位值）：

```json
{
  "ok": true,
  "schema_version": "audio-devices.v1",
  "scope": "audio_worker_host",
  "devices": [
    {
      "id": "pa:设备名称与音频后端生成的标识",
      "index": 3,
      "name": "USB Audio Device",
      "host_api": "MME",
      "input_channels": 0,
      "output_channels": 2,
      "default_sample_rate": 48000,
      "identity": "name_and_host_api",
      "persistent": true,
      "selectable": true
    }
  ]
}
```

前端按input_channels/output_channels大于0分别生成输入和输出下拉框，显示name与host_api。selectable=false的同名同后端设备不可显式选择，需在系统区分名称后再使用。可另提供“系统默认”选项。

推荐保存以下描述符：

```json
{"id":"实际设备id","name":"USB Audio Device","host_api":"MME"}
```

- null或字符串default：明确选择系统默认设备，允许随系统默认改变。
- 设备id或描述符：名称/后端匹配后再解析当前索引；消失时明确报错，不换到其他麦克风。
- 数字索引：仅兼容旧配置，可能随设备变化而指向不同端点；新前端不应持久化它。
- 名称：仅在匹配唯一时支持；跨后端同名应选择带后端的具体id。

id是名称+hostAPI的可复用标识，不是USB序列号，也不保证区分两台名称完全相同的物理设备。同名同后端记录虽然可显示，但不允许以其临时index后缀ID进行明确路由或保存。改名、换后端或蓝牙配置变化后，可能需要重新选择。

旧列表不能作为当前索引的权威。SDK校验会把旧身份重新绑定到当前列表；本机窗口在开始、检查、保存前还会重新扫描。所选设备丢失时保留缺失状态，让用户明确重选，不悄悄切到默认设备。

## EX Topic接口

控制topic：astrbotex_voice_jev.control。新增的是设备管理动作，持续输入开关仍只有set_streaming。

```json
{"action":"refresh_audio_devices","request_id":"list-1"}
```

```json
{"action":"check_audio_devices","request_id":"check-1","input_device":"实际输入id","output_device":"实际输出id"}
```

也接受既有action_id/params包装。消息结果发布到astrbotex_voice_jev.audio_devices：operation为list、validate或selected，附带结果或当前实际选择。查询请求可携带1～100字符request_id；正常结果回传该ID。并发请求遇到上一项尚未完成时返回device_query_busy，便于前端恢复按钮状态。

启用插件、运行EX actor后才消费Topic动作。原生后端直接调用query_audio_devices则不依赖收音开启。plugin.get_status()["audio"]保留最近查询结果和实际选择，供后端集成；现有EX HTTP状态接口未自动承诺暴露这个自定义对象。

## 保存到EX配置

前端先查询设备，再检查选定组合，然后使用已有接口：

```text
POST /api/v1/ex/plugins/astrbotex_voice_jev/config
```

```json
{
  "input_device": {"id":"实际输入id","name":"USB Microphone","host_api":"MME"},
  "output_device": {"id":"实际输出id","name":"USB Speaker","host_api":"MME"}
}
```

config.schema.json中提供format=audio-device和x-audio-direction字段，方便前端映射自定义下拉控件；anyOf列出兼容的选择值形态。worker启动时仍会独立严格解析与校验，不能只相信前端。

保存EX配置可能触发插件重载；先停止聆听和播放，保存后按EX正常流程重新开启。当前桌面测试窗口则保存到忽略的audio.devices.local.json，仅包含音频选择，不改语音/聊天/决策服务配置。

## 格式检查与诊断

内部链路要求：输入16kHz、单声道、PCM16；流式输出24kHz、单声道、PCM16。后端若能由系统音频服务转换可通过检查；如果所选底层端点只支持其他采样率/声道，本版会报告settings_rejected，未实现任意硬件格式的自动软件重采样。可尝试系统支持转换的音频后端，或后续扩展格式适配器。

格式检查不代表已经实际开流、录音或播报成功，也不能保证设备不会随后断开。

| code | 含义 |
| --- | --- |
| backend_unavailable | 音频Python缺sounddevice/PortAudio或系统音频库 |
| enumeration_failed | 系统设备枚举失败，检查服务、权限和容器映射 |
| device_not_found | 选择缺失或不支持该输入/输出方向 |
| ambiguous_device | 名称/后端无法可靠区分，需要明确选择或系统改名 |
| device_identity_changed | 保存的名称/后端与所指端点不符 |
| settings_rejected | 系统能看到设备，但不接受本链路格式；也可能涉及占用/权限等 |
| requires_stop | 当前进程仍在收音/播放，不允许重置后端 |
| restart_required | 后端不支持当前重新扫描方式，需重启音频程序 |
| device_query_timeout | EX设备查询超过8秒预算 |
| device_query_busy | 上一项异步查询还未消费完成 |

## 刷新实现与已验证范围

EX设备查询使用全新子进程。当前Windows窗口使用已固定的sounddevice 0.5.3；该版本没有公开热插拔重扫API，窗口只在自身语音线程和录音流已释放时使用其PortAudio重新初始化入口。缺少入口时明确提示重启，不会在播放中强制重置。

本机已实际枚举20个音频端点并检查默认输入/输出格式，未通过这些查询录音或播放。独立测试覆盖USB风格设备、跨索引变化、缺失、同名、方向、格式、配置保存与EX消息入口。真正USB/蓝牙外置硬件的热插拔、各厂商专有驱动和Nano实机仍待连接设备后验证。
