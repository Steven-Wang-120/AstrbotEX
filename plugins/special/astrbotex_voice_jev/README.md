# 0.7.0：外置音频设备选择接口

新增统一设备枚举、方向/格式检查和可保存的选择描述符。当前语音窗口可刷新、选择、检查、保存内置或系统已识别的外置音频端点，保留0.4阈值及正常开放话题对话。EX新增设备管理hook与Topic动作，详见 AUDIO_DEVICE_API.md；没有增加第二个收音开关。

驱动归实际连接设备的操作系统管理，插件不自动安装。设备ID按名称和后端生成，不是物理硬件序列号；明确选择的设备缺失时不会自动换到其他麦克风。同名同后端端点需系统改名或显式使用系统默认。

---

# 0.6.1：正常语音对话与可见阈值

桌面窗口新增“对话判定阈值（非音量）”，默认0.40。点击应用可在持续收音期间实时更新并保存，实际“对我说话”和“需要回应”两项同时生效。EX配置schema两项也默认0.40，范围0～1；静默门槛独立保持0.85。日志显示每句分数/门槛以及回答、背景或静默结果。

默认decision_stage=final：每段有声输入先经已配置MiMo最终识别，再对准确文本进行一次决策，避免本地字幕误识别将普通问候提前拦下。Vosk未产生字幕不再丢弃已经分段的有效音频。此默认模式下旁人语音也会经过已配置MiMo云端识别，但只有决策批准后才调用AstrBot聊天与TTS。若部署需要先筛本地文字再决定是否进行云端识别，可在EX设decision_stage=local_first保留旧路径。

模型处理正常开放话题对话：问候、闲聊、情绪、一般知识与任意请求均可回应，不限制机械臂或其他任务场景；明确对其他人的发言仍可保留背景而不应答。程序没有新增名字或场景关键词白名单。扬声器播放期间和短尾窗内，与当前播报相同的短确认词也参与文字回声过滤；这仍不是声学AEC。

---

# 0.6.0：通用URL决策接口

已移除名字/唤醒词列表和名字匹配。判断依据为语义与近期对话。EX公开配置改为 decision_* 字段，支持Laya/System One、Cloudflare、自定义JSON和OpenAI兼容Chat Completions；另保留通用Topic裁决。详细URL配置、请求响应、鉴权和前端契约见 [DECISION_API.md](DECISION_API.md)。

插件ID保留旧值以兼容已安装状态，运行路径已不再调用专用JEV客户端。旧 jev_* 配置字段不再生效；升级请按配置schema填写新的 decision_* 字段。本机桌面测试配置已迁移到通用URL格式，并继续使用原Cloudflare连接。安装包不含本机凭据、账号URL或录音。

仅保留持续收音开关，默认收音关闭。正常插话排队，背景记忆保持有界；活动会话内的完整静默指令可取消当前播放。未实现声纹身份判断或声学AEC。EX音频worker运行条件及流式语音限制见 DECISION_API.md 与 VALIDATION.md。

## 本机运行

Windows已有音频虚拟环境可直接执行 `./start_windows_streaming.ps1`，或在本目录用 `.venv/Scripts/python.exe -X utf8 windows_streaming.py`。保持 `config.mimo.local.json`、`astrbot.chat.local.json` 和 `voice.gate.local.json` 三份本地配置；最终识别/TTS、聊天和决策服务分别配置。将 `voice.gate.example.json` 复制为本地文件即可开始配置Laya URL，勿覆盖已有凭据。中文Vosk模型位于 `models/vosk-model-small-cn-0.22`。已有环境本次未变更依赖。

EX安装ZIP后，设置 `python_executable`、`vosk_model_path`、`speech_config_path`、`chat_config_path` 为实际音频worker主机的路径，设置 `decision_type` / `decision_url`。默认禁用且不开麦；部署后通过唯一动作 `set_streaming` 启停。Docker需单独映射音频设备，Windows音频不会自动出现在容器中。
# 0.7.1：EX 前端集成与 Windows 启动修复

连续语音可在 EX 语音页启停、选择设备、保存阈值和决策 URL、查看模型与事件。worker 显式使用 UTF-8，修复 Windows GBK 输出带 ® 设备名时退出；异常退出会关闭输入开关并报告错误。跨电脑部署使用完整合并版中的 deploy/nano_voice/prepare_deployment.py 生成目标主机路径，不复制原电脑的 .venv 或凭据。

