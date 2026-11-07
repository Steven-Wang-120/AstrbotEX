# 合并版 EX + 连续语音：另一台电脑部署指南

版本：语音插件 0.7.1，交付日期 2026-11-02。EX 基线为 bd6b30b，并包含本次前后端语音集成的工作区修改。先将整个 ZIP 解压到全英文路径，例如 `C:\AstrbotEXVoice` 或 `D:\AstrbotEXVoice`，再运行命令；以下命令的工作目录均为解压后的项目根目录。

Windows 的 Vosk 0.3.45 原生库在本次验证中无法读取中文路径中的模型。不要放在中文用户名的桌面、中文目录或其他含非 ASCII 字符的目录；准备脚本会提前检查。空格路径已纳入测试，使用全英文简单路径最方便。

## 一、交付范围

包含完整 EX 源码和前端、连续语音插件及独立安装 ZIP、Vosk 中文小模型、部署和检查脚本、配置模板、接口文档、测试及第三方模型许可说明。

不包含原电脑的密钥、DPAPI 密文、AstrBot 数据库/聊天记录、录音、运行日志、虚拟环境或 Docker 镜像。Python 库和 AstrBot 镜像需联网安装；MiMo、Clef、DeepSeek 也需云端访问，所以这个包不是离线运行包。

当前结构：EX 和音频进程运行在 Windows 主机上，直接使用本机麦克风和扬声器；AstrBot 可以单独放在 Docker 中。不要把整个 EX 直接放入普通 Linux Docker 后期待它自动访问 Windows 音频。

```text
项目根目录/
  astrbot_ex/、dashboard/          已合并的 EX 后端和前端
  plugins/special/astrbotex_voice_jev/  0.7.1 插件源码
  deploy/nano_voice/
    models/vosk-model-small-cn-0.22/  已含中文模型
    setup_windows.ps1             安装依赖、生成本机配置
    prepare_deployment.py         生成/迁移路径，保留已有本地凭据
    start_local_ex.ps1            启动 EX
    check_local_models.py         四个云端模型实际连接自检
    compose.astrbot.voice.yml     可选：只部署 AstrBot
  packages/                       独立语音插件 ZIP
  docs/                           部署、SDK、语音/决策/设备接口文档
  tests/                          回归测试
```

## 二、部署前准备

1. Windows 10/11、64 位 Python 3.10 或以上。源电脑实测 Python 3.14.4；安装时确保 python 可在终端运行。用 `python --version` 和 `python -m pip --version` 检查。Python 安装来源：[Python 官方下载](https://www.python.org/downloads/windows/)。
2. 一组能在系统正常录放音的设备；允许桌面应用访问麦克风。USB/蓝牙先在系统完成识别、配对和驱动配置。
3. 若新装 AstrBot，先安装并启动 Docker Desktop；若已有 AstrBot，可直接使用已有服务，不必启动第二个容器。
4. 准备 MiMo ASR/TTS Key、Cloudflare Workers AI Token 和账户 ID、DeepSeek Key。还要在 AstrBot 中创建一枚独立的 chat 访问 Key。

本包附带的 Vosk 模型用于本地字幕；最终识别仍调用 MiMo。无需在电脑安装 DeepSeek、Clef、MiMo 模型权重或 CUDA。

## 三、安装 EX 和音频依赖

在解压目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\nano_voice\setup_windows.ps1
```

如果 `python` 指向错误版本，指定完整 Python 路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\nano_voice\setup_windows.ps1 -Python "C:\你的Python目录\python.exe"
```

脚本会创建本机 `.venv`，安装项目 requirements.txt 和音频 requirements.voice.txt，生成三份服务配置及 MiMo 凭据文件，并将插件安装到 `deploy/local/data/plugins/special/astrbotex_voice_jev`。它不会复制旧电脑的路径或密钥，也不会启动麦克风。再次运行会保留已有本地服务配置和凭据，但重建已安装插件中的本机路径。

## 四、部署或连接 AstrBot

### 新电脑尚未安装 AstrBot

确认 Docker 已启动，然后在项目根目录运行：

```powershell
docker compose -p exvoice -f deploy/nano_voice/compose.astrbot.voice.yml up -d
```

打开 http://127.0.0.1:6185 ，按 AstrBot 的首次启动流程设置登录。数据在 `deploy/local/astrbot-data`。本 Compose 使用源电脑实际验证过的 AstrBot 镜像摘要，避免 latest 漂移；镜像不包含在 ZIP 中。已有服务占用 6185 时应复用或调整端口，不要强行覆盖。

官方部署说明：[AstrBot Docker 部署](https://docs.astrbot.app/deploy/astrbot/docker.html)。

### 配置 DeepSeek

在 AstrBot 的模型提供商页面配置并启用：

| 项目 | 值 |
|---|---|
| 协议 | OpenAI Chat Completions 兼容 |
| API Base | https://api.deepseek.com/v1 |
| 模型 | deepseek-flash |
| API Key | 部署者自己的 DeepSeek Key |

保存后先在 AstrBot 内发送一句话，确认能够回答。语音模块默认 provider ID 为 `deepseek/deepseek-flash`；如果这台 AstrBot 生成了不同的 provider ID，之后在 EX 语音页的“运行环境设置 → provider_id”填写实际 ID，不能只填模型显示名称。

### 创建供语音模块使用的 AstrBot Key

在 AstrBot `设置 → OpenAPI` 创建 API Key，授予 `chat` scope。用户名使用普通标识 `ex_voice`，不需要管理员权限。这个 Key 用于访问 `POST /api/v1/chat`，与 DeepSeek Key 是两种密钥。[官方 OpenAPI 说明](https://docs.astrbot.app/dev/openapi.html)、[Scope 对照](https://docs.astrbot.app/dev/openapi-scopes.html)。

## 五、填写本机配置（四个文件）

先运行第三步，再编辑以下文件。保持 JSON 双引号和逗号合法。

### 1. MiMo 密钥

编辑 `deploy/nano_voice/mimo.credentials.local.json`：

```json
{"stt": "填MiMo识别Key", "tts": "填MiMo合成Key"}
```

如果两者使用同一个 Key，可填写同一值。`config.mimo.local.json` 已默认设置 ASR=`mimo-v2.5-asr`、TTS=`mimo-v2.5-tts`、API Base=`https://api.xiaomimimo.com/v1`，一般不用修改。

### 2. AstrBot 连接

编辑 `deploy/nano_voice/astrbot.chat.local.json`，将 api_key 的空字符串替换成第四步创建的 AstrBot chat Key：

```json
{
  "base_url": "http://127.0.0.1:6185",
  "username": "ex_voice",
  "token_env": "ASTRBOT_API_KEY",
  "api_key": "填AstrBot的chat访问Key",
  "timeout": 90,
  "provider_id": "deepseek/deepseek-flash"
}
```

注意：worker 会用 EX 插件自己的 provider_id 覆盖这里的 provider_id，所以两处应一致；默认值已经一致。

### 3. Clef 决策

编辑 `deploy/nano_voice/voice.gate.local.json`：

- 将 URL 中的 `YOUR_CLOUDFLARE_ACCOUNT_ID` 替换为实际 Cloudflare 账户 ID。
- 将 api_key 空字符串替换成有该账户 Workers AI 权限的 Cloudflare Token。
- 保留 type=`cloudflare`、model=`clef`。对话门槛默认 0.4，静默门槛 0.85。

实际 URL 形式：

```text
https://api.cloudflare.com/client/v4/accounts/你的账户ID/ai/run/@cf/cloudflare/clef
```

新部署默认使用本机 `.local.json` 保存凭据，方便部署者编辑；文件不进入分发包。也可以改用 MIMO_API_KEY、ASTRBOT_API_KEY、CLOUDFLARE_API_TOKEN 环境变量。MiMo/AstrBot 环境变量会优先于文件；决策配置中非空 api_key 优先于其环境变量，切换时应清空文件 api_key。

原电脑的 Cloudflare DPAPI 文件绑定原 Windows 账户，不能复制到另一台电脑当作可用 Token。此包没有包含它。

### 4. EX 主配置（通常不需要手改）

准备脚本已在 `deploy/local/data/plugins/special/astrbotex_voice_jev/config.json` 写入本机绝对路径。当前 decision_url 留空，服务类型、URL、密钥从 voice.gate.local.json 继承；EX 的 decision_threshold 和 forward_threshold 均为 0.4，并覆盖 gate 文件中的门槛。

更换聊天 provider ID 时，可在 EX 前端高级设置修改这里的 provider_id。更换配置目录或移动整个项目后，重新运行：

```powershell
.\deploy\nano_voice\.venv\Scripts\python.exe -X utf8 deploy/nano_voice/prepare_deployment.py
```

执行前先停止此目录正在运行的 EX。重新生成路径会保留三份服务配置、MiMo 凭据和设备选择。不要把旧电脑保存的设备 ID 当作新电脑的设备；新部署默认系统默认设备。

## 六、先做模型自检

```powershell
.\deploy\nano_voice\.venv\Scripts\python.exe -X utf8 deploy/nano_voice/check_local_models.py deploy/local/data/plugins/special/astrbotex_voice_jev/config.json
```

输出总 ok=true，且 tts、stt、decision、chat 四项均 ok=true 才表示这次四个服务检查通过。此检查实际调用云端 API，使用合成测试语音，不开麦、不外放；报告写入插件目录的 models-check.local.json。网络检查通过仍需继续做真实设备测试。

## 七、启动并使用前端

首次部署建议以前台方式启动，便于看到错误并用 Ctrl+C 退出：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\nano_voice\start_local_ex.ps1 -Foreground
```

保持这个终端打开，再在浏览器访问 http://127.0.0.1:8765/#/voice 。日常也可不加 -Foreground，脚本会后台启动并打开页面；已有语音 EX 在该端口运行时不会重复启动。

操作顺序：

1. 进入“语音”，检查四个模型身份和最近检测结果。
2. 点击“刷新设备”，分别选麦克风与扬声器；点击“检查设备格式”，再“保存设备和设置”。输入要求 16kHz、单声道 PCM16，输出要求 24kHz、单声道 PCM16。
3. 到总览启动 EX 运行时。语音插件默认不录音，第一次开启连续对话时会自动启用插件。
4. 回语音页点击“开启连续对话”，等状态进入 listening 后说“你好，可以陪我聊聊天吗”。停顿约 0.9 秒后提交。
5. 检查“转写与回复”事件，确认有识别文字、决策结果和助手回复，并实际听到声音。
6. 播报时麦克风保持开启。普通插话排队，当前活动会话中完整的“先别回答/别说了”可停止播报。背景话语保留有界会话记忆，不应立即回答。
7. 停止连续对话后才能切换设备或保存配置；退出时先停止连续对话，再停止运行时。前台终端 Ctrl+C 可关闭 EX 服务。

只开启 EX 运行时不会自动开始收音；只开启网页也不会自动开始收音。进程启动失败或退出时，0.7.1 会回退开关并显示错误。

## 八、常见问题

| 现象 | 检查方法 |
|---|---|
| 语音进程启动后立即退出，GBK/®/中文错误 | 确认用本包 0.7.1 的 main.py、worker.py；不要换回旧 0.7.0 ZIP。启动命令和 worker stdio 已强制 UTF-8 |
| 开关开启但没有转写 | 看语音状态/错误；确认 EX runtime=running、worker_running=true、state=listening；检查选中的麦克风和系统隐私权限 |
| 有转写但不回答 | 看 gate_result 分数及门槛、决策服务权限/网络；背景或静默判定不转交；不要把阈值当音量 |
| chat 自检失败 | 确认 6185 可访问、AstrBot chat Key 有权限、provider_id 与实际启用模型一致，并先在 AstrBot 中完成对话 |
| 401/403 | 对照所属服务检查对应 Key、账户权限和 API 额度，四种 Key 不能混用 |
| 播放无声 | 检查输出设备、系统音量、格式检查；部分 USB 线只供电，音频仍需声卡或 3.5mm 线 |
| 热插拔后找不到设备 | 停止连续对话，刷新并明确重选，保存后再开始；明确选择的设备缺失时不会悄悄换麦 |
| 原页面兼容自检显示 STT/TTS 未配置 | 那是另一条 EX 兼容代理链路。以顶部连续语音面板的实际模型、事件和自检结果为准 |
| 打开页面自动用了旧项目 | 确认 8765 没有运行另一份 EX；关闭旧服务后在本解压目录启动 |
| pip 安装失败 | 检查 Python 为 64 位、网络和终端错误；不要把原电脑 .venv 拷过来替代安装 |
| Vosk 报目录没有模型，但文件确实存在 | 检查完整模型路径是否含中文或其他非 ASCII 字符；移动项目到全英文路径后重新运行准备脚本 |
| Docker 弹 Inference manager/dockerInference/Secrets Engine 错误 | 属 Docker 启动故障；不要为此恢复出厂设置。先退出并备份 Docker 设置/数据，按官方问题记录排查临时通信目录和 Docker AI 设置；本包不会自动改 Docker 设置 |

相关 Docker 问题记录：https://github.com/docker/desktop-feedback/issues/531 。

日志位置：`deploy/local/data/ex-voice-error.local.log` 和 `deploy/local/data/plugins/special/astrbotex_voice_jev/worker-error.local.log`。本机状态接口为 http://127.0.0.1:8765/api/v1/ex/voice/status 。

## 九、换服务、远程 AstrBot 和 Linux/Nano

- 通用决策类型支持 cloudflare、laya、systemone、http_json、openai；EX 的 URL 留空时使用 gate 文件。填写直接 URL 时还要设置匹配的协议/模型。契约见 docs/VOICE_DECISION_API.md。
- 使用远端 AstrBot 时，127.0.0.1 表示 worker 自己这台机器。应使用 HTTPS 地址，或建立本地 SSH 隧道后保持回环地址；当前客户端不允许向远程明文 HTTP 发送聊天访问 Key。
- Windows 设备选择只代表 Windows 音频主机；浏览器设备不能自动转给 Nano。Nano/Linux 需重建其架构的 Python 环境、系统音频库、权限和设备映射。
- Linux 可手工创建 deploy/nano_voice/.venv，安装根 requirements.txt 与 requirements.voice.txt，再运行 prepare_deployment.py；随后设置 ASTRBOTEX_DATA_DIR 为项目 deploy/local/data 的绝对路径，从项目根目录运行 `python -X utf8 -m astrbot_ex.core.api_server --host 127.0.0.1 --port 8765`。旧 install.sh 不是当前完整 EX 部署器。
- 本次包以 Windows 迁移为主要路径；没有宣称已经验收 Nano 或任意外置设备。

## 十、验收与版本说明

0.7.1 包含持续收音、后台记忆、通用决策 URL、默认 0.4 门槛、DS Flash 开放话题对话、流式合成、句尾排空、设备选择、前端状态/日志及 GBK 启动修复。当前按停顿分段再最终识别，不是原生音频到音频模型；没有声纹身份识别、注视传感器或声学回声消除。

源电脑已实测四个模型连接和实际设备启动进入监听；回归和迁移准备脚本检查见本包 VALIDATION.md。其他电脑仍须按第六、七步用自己的密钥、设备和网络验收。

## 十一、文件校验与交接

包内 MANIFEST.sha256 列出各文件 SHA-256；外部同名 .sha256 文件校验完整 ZIP。密钥应在新电脑本地填写或通过授权的独立渠道提供，不要把填好凭据后的整个目录再次公开分发。

完整模型地址与文件说明见 docs/VOICE_CONFIGURATION_REFERENCE.md；前端 API 见 docs/VOICE_DASHBOARD.md；外设接口见 docs/VOICE_AUDIO_DEVICE_API.md。
