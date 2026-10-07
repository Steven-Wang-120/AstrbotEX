# B07：Laya EX 后端与协议边界

Laya 是 EX 的候选选择后端，不是聊天模型或低层控制器。插件声明当前可用动作，LLM 只下发当前步骤 Goal 与必要参数；EX 构造快照，模型仅选已有候选，经版本、参数、观测 TTL、资源及权限校验后走原 Dispatcher / Actor / Ledger。插件间信息仍走 TopicBus，不让模型新造动作、参数或下一步 Goal。

## 1. 实现入口与固定身份

- 后端：[laya.py](../astrbot_ex/core/decision/backends/laya.py)；静态工厂：[registry.py](../astrbot_ex/core/decision/backends/registry.py)。
- 配置与连接映射：[config.py](../astrbot_ex/core/decision/config.py)；进程归属：[owned_laya.py](../astrbot_ex/core/decision/owned_laya.py)。
- 适配器测试：[test_laya_backend.py](../tests/test_laya_backend.py)；正式 EX/测试 Actor/临时 Ledger：[test_laya_service_integration.py](../tests/test_laya_service_integration.py)。后者使用合成 HTTP 回复，不证明模型语义正确。
- 真实权重入口：[verify_laya_backend.py](../scripts/verify_laya_backend.py)；管理入口见 [B08](B08-DECISION-MANAGEMENT.md)。

| 身份 | 固定值 |
|---|---|
| Laya 官方源码 | `0.3.22` / `6d942c92081fbc139e736bbd9ac0023223c29b7f` |
| 请求模型 | `typed-decisions` |
| 来源 | bundle `convaiinnovations/laya` 的 `typed-decisions/` |
| bundle revision | `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` |
| 权重 SHA256 | `4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e` |
| Tokenizer SHA256 | `6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30` |

这是约 421M 参数的 ModernBERT-large 编码器决策模型（固定配置 max_len=1024、head_max_len=256、amp_dtype=bf16），训练覆盖英文合成业务工作流，不承诺机器人任务泛化。standalone typed 仓库 revision `1a793eb568e6718f15941d08f85432581df534e3` 不能套到 bundle。固定权重文件 842,609,220 B，五个必要文件总计 846,195,716 B；stock 模型参数驻留/计算 autocast 与下载 F16 大小不同，不能推算全部显存。实际设备、CPU 回退、显存与延迟要独立测量。官方 Python >=3.10；serve 使用 FastAPI/Uvicorn/python-multipart，不要求 TileLang、ONNX 或 MCP。

## 2. 外部连接与可选自启（冻结 v2 合同）

配置保存 envelope 为 `schema_version=2`，外层 `backend/mock/jev/laya` 保留。Laya 连接是：

```json
{
  "service_connection": {
    "mode": "external",
    "base_url": "http://127.0.0.1:8769",
    "model": "typed-decisions",
    "auth_mode": "none"
  },
  "deployment": null
}
```

- 默认 external：不检查本地 Python/cache/CUDA，不要求 owned generation，不启动/停止外部服务。连接使用普通 `LayaBackend`。
- owned：`deployment={launcher:"subprocess",python:<绝对可执行路径>,cache:<绝对目录>,device:"cpu"|"cuda"}`，base_url 必须为配置端口的 `http://127.0.0.1:<port>`；使用 `ManagedLayaBackend` 与当前 `Popen/generation` 的 decision guard。只有可证明归属的进程可关闭，绝不按端口、旧 PID 或进程名认领。
- `DecisionConfigStore.backend_config()` 是唯一 validated flattening 入口；owned 部署通过 `laya_deployment(..., output=...)` 获取。输出与状态路径由可信装配提供，不接收 HTTP 任意路径、shell 或 callable。
- v1 磁盘 Laya 迁为 owned、deployment=null，显式补部署配置前不能启动；迁移不联网、不认领进程，只在下次保存/secret mutation 写回，CAS 不变。
- URL 前缀追加 `/health`、`/v1/systemone`；TLS 校验开启，不跟重定向，不接受 URL 凭据、query、fragment。明文 HTTP 仅 loopback，远程要求 HTTPS。
- `auth_mode=bearer` 使用独立 `laya_secret_ref`，不混用 admin token 或 Jev key。owned 子进程和启动后端使用同一 Laya secret source；不继承环境里其他任务的 `LAYA_API_KEY`。GET 只返回引用/是否配置，绝不返回 key。

导入、默认构造和 `decide()` 都不创建模型进程。生产执行须显式 enabled/live HTTP/execution 配置与内置 transport gate 同时允许；成功 probe 不授予执行。注入 transport 默认不可执行，`allow_test_execution` 仅可信隔离 fixture 构造参数，不能从 saved config/HTTP 获得。管理 execute 的 runtime/control 激活见 [B08](B08-DECISION-MANAGEMENT.md#3-http-路由与启停合同)。

同一 schema v2 保存对象也包含 Jev：`service_connection={base_url:"https://api.typesafe.ai",model:"jev-1.13.0",auth_mode:"bearer"}`，支持显式 disabled/shadow/execute、live HTTP、deadline/限频/min_confidence，未核对 alias 拒绝。`secret_ref` 与 `laya_secret_ref` 分离；key keep 不写入/增 CAS，set/clear 增 CAS，空 set 或 keep/clear 携 value 拒绝。原子发布使用同目录暂存、文件 fsync、immutable key no-clobber、回滚/恢复 marker，POSIX 另做目录 fsync；`storage_write_uncertain` 保留 fence 并禁止后续配置写入/后端映射/owned 启动，不能删 marker 或 key 来解锁。`secret_cleanup_failed` 可能发生于 CAS 已提交后，须刷新而非盲重试。

Windows 使用可选 fchmod、明确 fd 所有权清理和 owned `.exe` preflight；POSIX 0700/0600 不是 Windows ACL 证明，Windows 存储不承诺 POSIX 目录 fsync 的同等崩溃持久性，实际模型环境与整树停止须独立验证。

## 3. 独立部署与验证用法

模型环境与 EX 分离，torch/transformers 不进入 EX 主环境。保留的 [requirements-laya.lock](../requirements-laya.lock) 是历史 Linux/Python 3.12/CUDA 12.8 组合，固定官方源码归档及哈希；不是任意 Windows/CPU/GPU 的兼容承诺。官方依赖只有下界，重建时须核对实际 torch/CUDA、transformers、tokenizers、huggingface_hub、numpy、fastapi、uvicorn、safetensors 版本。

从仓库根创建独立环境（按部署平台选择可执行路径）：

```sh
python3.12 -m venv runtime/laya/.venv
runtime/laya/.venv/bin/python -m pip install -r requirements-laya.lock
```

首次准备缓存可使用 `huggingface_hub.snapshot_download`，固定 repo/revision，只下载 `typed-decisions/rl_agent_config.json`、`encoder/config.json`、`model.safetensors`、`tokenizer/tokenizer.json`、`tokenizer/tokenizer_config.json`。权重、环境、缓存不入 Git；下载等待不能放宽 EX 决策 deadline。

官方 owned 服务只运行 `python -m laya.serve`（shell=False），预加载单 checkpoint：`LAYA_MODELS=typed-decisions`、`LAYA_PRELOAD=1`、`LAYA_AUTO_TASK=0`、`LAYA_MAX_LOADED=1`、`LAYA_MAX_CONCURRENT=1`、`LAYA_MAX_TOKEN_BUDGET=1024`、固定 `LAYA_REVISION`。空 MODELS 会加载全部模型。验证入口使用离线缓存、固定 digest，端口占用则失败不 kill 占用者。CPU 显式可选；`/health.checkpoint_devices/cpu_fallbacks/device_is_preference` 才是实际设备证据。

具名真实验证入口（新空输出目录，安装 EX 依赖后从仓库根运行；路径替换为实际部署值）：

```sh
python -B scripts/verify_laya_backend.py --help
python -B scripts/verify_laya_backend.py --run-real \
  --laya-python /absolute/laya-env/bin/python --model-cache /absolute/laya-cache \
  --output /absolute/external-evidence/new-run --port 8769 --device cuda --repetitions 8
```

该入口显式调用真实权重，只装配测试 Actor，不授权机器人。冷调用、预热、热调用分别计时；退出只处理自己保存的 Popen，TERM/KILL 有界等待后观察退出。Windows parent handle 退出不能证明整个子孙进程树已停止，必须保留这个限制。

## 4. 输入映射与预算

`decide(snapshot)` 防御复制并保留当前 Goal、已绑定参数、候选完整含义、owner 状态和观测 source/epoch/seq/age/hash/health。本地关联 ID/版本不由模型生成。

每 owner 一题 `q0/q1/...`，候选稳定映为 `A/B/...`，独立保存 `short_key -> original_option_id`；不混 owner、不变参数。候选头采用短 ASCII 摘要，完整含义保留在 state。state 用 `json.dumps(...,ensure_ascii=True,separators=(',',':'),allow_nan=False)` 的 ASCII 字符串，服务器不再 dump 字典。非 ASCII 与 `[MASK]` 的原值可用 JSON escape 保留，但不证明英文模型理解中文 escape。

固定 tokenizer 是 NFC + ByteLevel BPE（无 prefix），ASCII NFC 不扩张，字节长度可作为**保守 token 上界**，不是实际 token 计数。原始 UTF-8 字节数不能无条件限 token：NFC 会展开部分字符。每 owner 独立核算官方模板：

```python
I = len(("choice question: " + instructions).encode("ascii"))
O = [len((" " + key + ": " + desc).encode("ascii")) for key, desc in criteria.items()]
S = sum(1 + size for size in O)  # 每选项一个 MASK
assert max(O) <= 48
assert S <= head_max_len - 16
assert I <= head_max_len - S
assert I + S + 4 + len(state.encode("ascii")) <= max_len
```

4 是 CLS 与三个 SEP；空描述须按官方 render 分支计算。固定 `max_len=1024/head_max_len=256` 是上限，实际 state 容量取决于题头；不能只按字符数说精确 tokenizer 验证。超预算拒绝 `token_budget_exceeded`，不静默删 Goal/观测。响应 `usage.truncated/state_tokens_dropped/truncated_questions/options` 二次核验，任何截断或选项折叠拒绝。精确 tokenizer/build_sequence 检查可在独立模型环境做，不向 EX 引入 torch。

## 5. HTTP 身份、响应与概率

请求 `POST /v1/systemone`，显式 `model=typed-decisions/max_len/head_max_len`。`/health` 无 Bearer 不代表 POST 无鉴权。未知 model 在官方服务可能自动路由，客户端白名单与响应身份验证不能省略。

响应顶层 `model/answers/usage/routing`：`model=laya-rl-agent`，`routing.model=typed-decisions`、`routing.repo=convaiinnovations/laya/typed-decisions`、reason 为显式 model，detection/workflow 为 null。每次决定先检查 health 的已加载 checkpoint、revision 和设备；推理响应本身没有权重 revision，适配器需记录 repo/revision/input hash 与 EX ID。

answer 的 owner/question 集合、选项/概率键精确匹配，拒绝重复 JSON 字段、漏 owner、未知 option、非有限数、非 argmax、身份错误和无效 usage。返回仍走冻结 [BackendDecision / Goal 契约](DECISION-CONTRACT.md) 与 `validate_backend_selection()`，不放宽框架概率总和断言。

- 官方 `confidence = 1 - normalized Shannon entropy`；`answer_confidence=max(probability)` 映为 EX choice.confidence，原始熵字段保留。二者未经独立校准都不是机器人成功概率。
- `action.act_probability` 是辅助 action-head 诊断，不是 EX action_id 或执行权限。
- 四位小数舍入只允许总和误差 `N * 0.00005 + 1e-12` 内按实际 sum 归一化；记录 raw 分布、sum、误差界、normalization 方法，其余失真拒绝。
- 模型低置信标记不改 choice；禁止隐式替换 wait 或默认第一项。Jev 的原 choice/score 保留，execute 下 EX 在 dispatch 前以 `low_confidence` 拒绝整轮；选择 `request_replan` 则以 `backend_requested_replan` 拒绝整轮，均零新 dispatch。达到阈值的模型 wait 是正常选择，shadow 只记录。

拒绝后先关 gate、撤授权，有已运行命令时要求实际 Actor/Ledger 停止证明、资源释放和当前 epoch 复核。FeedbackJournal 先持久化退役草稿，再由当前 Goal/revision/epoch 的 CAS 退役并发布 failed 事实，保留有界原选择与拒绝原因。草稿不是事实，重启不自动提升；缺证明或持久化失败不退成成功、不授新 Goal。HostTask 是多步骤计划，EX currentGoal 只是当前一步；Host 重规划只处理未完成后缀，已完成前缀不得重放。

官方非零 `min_confidence` 可能增加 `low_confidence` 字段；当前 EX Laya decoder 要求 answer 六字段精确相等，**不支持该额外字段**，不会静默接受或据此改 choice。这与 Jev 的 EX dispatch 前阈值合同不同。请求不启用官方 task/lang 自动路由，也不允许未知 alias。

## 6. 状态、probe 与请求证据

`status()` 只读缓存，不联网/加载/派发；busy 是本地 worker，不是 GPU busy 证明。`last_record` 是最近完成 decide 的副本，不是永久历史数据库。

v2 `health_probe()` 仅 readiness GET（owned 启动使用）；`probe()` 为固定合成 wait-only 快照的最小真实推理，复用生产 decoder，不提交业务 Goal，不调 Action/plugin/ledger。返回 `ok/health_ok/inference_ok/inference_called/error_code/model/checked_at_ns/restart_required`。health 成功、推理 401 应分开报告，不能把 health 当协议通过。probe 成功不保存配置、不启用 execute、不清除隔离。

请求记录应区分 prepared、post_attempted、post_written_to_socket、response_received；本地写完不证明远端收到或完成。实际字节 hash、短键映射、模型身份、原始概率/归一化、时间及拒绝阶段可追踪；缺项必须 null/缺失，不由快照重建成“已发送”。有界原始载荷属于敏感管理读取，不回显凭据或服务 traceback。模型选择、EX admitted、Actor accepted/running/succeeded 是不同事实。

## 7. 超时、remote unresolved 与恢复

官方单 worker 与 admission/gate 不能证明所有取消场景的远端工作结束：客户端断连通常继续推理，ASGI 取消也可能先释放 gate 而 torch 线程继续。没有取消路由、请求 ID 或准确 busy 查询。

最多一个在途调用/transport worker，迟到结果按 epoch 失效。`cancel/close` 仅取消本地等待，不声称 GPU 或物理动作已停。固定 deadline 覆盖准备、health、POST、解析与等待，无自动重试/回退。

POST 尝试后 timeout/cancel/transport/响应校验不确定锁存 `restart_required`（remote_work_unresolved 语义），拒绝新决定。发送前失败不锁存；完整已知拒绝 `400/401/403/404/405/413/415/422/503` 不自动锁存，其余未知失败保守隔离。health、迟到结果、等待、重建客户端不能解除锁存。

恢复先由 EX 停止入口取得同 operation_id 的 `state=proven`，disabled/空闲复核后关闭旧后端。owned 只终止并确认归属句柄退出，再固定身份启动、预热新代次、可信 `replace_backend()`。external 由服务所有者提供重启/更强完成证据，EX 不调用 owned start/stop 冒充恢复。恢复不重放旧 Goal/请求，仍需新授权和新 Goal。Actor StopEvidence 与模型进程退出证明不能互换。

## 8. 验证与已知限制

从仓库根运行无需 key/GPU 的确定性测试：

```sh
python -B -m unittest tests.test_laya_backend tests.test_laya_service_integration \
  tests.test_decision_backend_contract tests.test_decision_runtime_integration -v
```

真实入口分别记录加载/预热、HTTP、后端、snapshot→测试 Actor 耗时，样本数/失败数与 p50/p95/max 分开；accepted 时间不是 SQLite 持久化 ACK 或物理停车时间。正常退出码、至少一次 model_to_actor_pass 都不能代替 selection_quality_pass 或机器人验收。

模型质量须独立评估，尤其是 start 与 request_replan 的语义区分；协议 decoder 或 EX gate 通过不证明选项正确。真实机器人/ROS 执行器/Isaac 抓放、模型微调、多 owner/大观测效果、GUI 资源争用与长时稳定性不由单次测试推出。压力诊断见 [来源与检查入口](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。新日志/截图/大 JSON 输出外部 evidence 或临时目录，不写 docs/evidence。

## 9. 官方来源

协议身份以固定 revision 的源码和模型元数据为准；下列 main 分支部署文档仅作参考：

- [固定源码及安装元数据](https://github.com/NandhaKishorM/laya/tree/6d942c92081fbc139e736bbd9ac0023223c29b7f)
- [服务器](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/serve.py)、[Router](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/router.py)
- [输入构造](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/common.py)、[Agent](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/agent.py)
- [Confidence](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/confidence.py)、[revision 规则](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/revisions.py)
- [固定模型元数据](https://huggingface.co/api/models/convaiinnovations/laya/revision/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851?blobs=true)、[模型配置](https://huggingface.co/convaiinnovations/laya/blob/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/typed-decisions/rl_agent_config.json)
- [HTTP 文档](https://github.com/NandhaKishorM/laya/blob/main/docs/http-api.md)、[部署参考](https://github.com/NandhaKishorM/laya/blob/main/docs/docker.md)（EX owned 合同仅 subprocess，参考不等于支持 owned Docker/WSL）。
