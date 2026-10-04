# B07：Laya EX 后端运行与 B08 交接

Laya 在本任务中担任 **EX 决策后端**。它从本次快照的合法动作中选择选项。
当前链路是 `GoalManager → DecisionService → LayaBackend → Dispatcher → 测试 Actor → Ledger`。
此前“Laya 只在控制端评分”的计划不再限定本任务的位置。

B07 历史任务未接 ROS 或 Isaac、未微调。当前分支已有 B08/B09 和通用 ROS 通信；模型驱动机器人闭环仍未验收。
真实模型选择是否正确、是否到达 Actor，以本轮结果报告为准。
热调用 p95≤100 ms 仍是目标。环境安装成功或适配器测试通过不能证明达到该目标。

## 1. 文件与固定身份

| 项目 | 位置或身份 |
|---|---|
| 后端 | `astrbot_ex/core/decision/backends/laya.py` |
| 静态工厂 | `astrbot_ex/core/decision/backends/registry.py`，名称 `laya` |
| 适配器测试 | `tests/test_laya_backend.py` |
| 正式服务测试 | `tests/test_laya_service_integration.py` |
| 真实验证入口 | `scripts/verify_laya_backend.py` |
| Laya 源码 | `0.3.22`，Git SHA `6d942c92081fbc139e736bbd9ac0023223c29b7f` |
| 请求模型别名 | `typed-decisions` |
| 实际模型来源 | bundle `convaiinnovations/laya` 的 `typed-decisions/` |
| 权重 revision | `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` |
| 权重 SHA256 | `4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e` |
| Tokenizer SHA256 | `6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30` |

这个 checkpoint 是约 421M 参数的编码器决策模型。它不是生成任意回答的聊天模型。
原始训练覆盖四类业务工作流。机器人动作选择的效果必须另行测量。

## 2. 独立环境与模型目录

Laya 使用 `runtime/laya/.venv`。EX 继续使用 `.venv`。
Laya 的 torch 和 transformers 不进入 EX、ROS 或 Isaac 环境。
权重缓存位于 `/data/shared/AstrEX_project_data/models/pretrained/laya/hub`，不进入 Git。

本轮环境记录：

| 依赖 | 实际安装版本 |
|---|---|
| torch | `2.8.0+cu128` |
| transformers | `4.57.3` |
| tokenizers | `0.22.2` |
| huggingface_hub | `0.36.2` |
| numpy | `2.5.3` |
| fastapi | `0.142.2` |
| uvicorn | `0.54.0` |
| safetensors | `0.8.0` |

完整依赖冻结为 `../requirements-laya.lock`，由本机成功环境的 pip freeze 生成。
它固定 Laya 的官方源码归档和归档哈希，避免依赖临时绝对路径。
原始 freeze、GPU 检查和五个模型文件的哈希保存在本轮证据目录。
官方依赖只规定下界；这个 lock 是本次 Linux/Python 3.12/CUDA 12.8 的实测组合。

重建独立环境时运行以下命令。已有环境可直接复用。

```bash
cd /home/sssxy/Projects/AstrbotEX
python3.12 -m venv runtime/laya/.venv
runtime/laya/.venv/bin/python -m pip install --timeout 120 \
  -r requirements-laya.lock
```

首次准备模型缓存时运行以下代码。只下载固定 revision 中需要的五个文件。
验证入口本身使用离线模式，缺文件时失败。

```bash
runtime/laya/.venv/bin/python - <<'PYCODE'
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="convaiinnovations/laya",
    revision="55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851",
    cache_dir="/data/shared/AstrEX_project_data/models/pretrained/laya/hub",
    allow_patterns=[
        "typed-decisions/rl_agent_config.json",
        "typed-decisions/encoder/config.json",
        "typed-decisions/model.safetensors",
        "typed-decisions/tokenizer/tokenizer.json",
        "typed-decisions/tokenizer/tokenizer_config.json",
    ],
    max_workers=2,
)
PYCODE
```

服务器加载前还会核对权重与 tokenizer 哈希。
下载等待与 EX 的决策 deadline 分开。网络超时不会自动放宽决策预算。

## 3. 显式启动与进程归属

适配器导入、默认构造和 EX 普通启动均不启动模型服务。
`decide()` 本身也不创建服务器进程。
真实验证入口必须显式传入 `--run-real`。

在 EX 目录运行以下命令。输出目录必须为空，每次使用新的目录。

```bash
cd /home/sssxy/Projects/AstrbotEX
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/verify_laya_backend.py \
  --run-real \
  --laya-python /home/sssxy/Projects/AstrbotEX/runtime/laya/.venv/bin/python \
  --model-cache /data/shared/AstrEX_project_data/models/pretrained/laya/hub \
  --output /tmp/astrex_laya_real_review_01 \
  --port 8769 \
  --device cuda \
  --repetitions 8
```

入口只创建一个官方 `python -m laya.serve` 子进程，不开发第二个推理服务器。
它先检查 `127.0.0.1:8769` 是否被占用。端口冲突时失败，不终止占用者。
服务使用以下环境：

| 环境字段 | 值与目的 |
|---|---|
| `LAYA_HOST` | `127.0.0.1`，只允许本机访问 |
| `LAYA_PORT` | `--port`，默认 `8769` |
| `LAYA_DEVICE` | `--device`，默认 `cuda` |
| `LAYA_MODELS` | `typed-decisions`，只预加载一个 checkpoint |
| `LAYA_PRELOAD` | `1` |
| `LAYA_AUTO_TASK` | `0` |
| `LAYA_MAX_LOADED` | `1` |
| `LAYA_MAX_CONCURRENT` | `1`，多余请求返回 503 |
| `LAYA_MAX_TOKEN_BUDGET` | `1024` |
| `LAYA_REVISION` | 固定 bundle 权重 SHA |
| `HF_HUB_CACHE` | `--model-cache` |
| `HF_HUB_OFFLINE`、`TRANSFORMERS_OFFLINE` | `1`，不下载或换权重 |
| `TORCH_COMPILE_DISABLE` | `1`，首轮保持普通推理路径 |
| `TOKENIZERS_PARALLELISM` | `false` |

入口移除继承的 `LAYA_API_KEY`，并明确设置本 checkpoint 的权重与 tokenizer SHA256。
它不继承其他任务的 digest 配置。
这个仅限本机的验证入口没有新增 Bearer 管理配置。
B08 的管理员凭据与模型服务器凭据不是同一项，本轮不实现后者。

入口等待预加载成功，再执行一次单列的冷调用和预热。
正式热调用与 EX 调用使用配置中的 deadline。
冷调用使用独立记录的 30000 ms 预算，不修改热调用预算。

退出时，入口只关闭自己保存的 `Popen` 子进程。
它先发送 TERM，等待 10 秒。仍未退出时发送 KILL，再等待 5 秒。
只有观察到该子进程退出，才记录服务退出已确认。
它不使用端口查找或任意 PID 列表关闭其他服务。

`--device cpu` 是显式 CPU 工况。
官方也能在 GPU 不可用或 OOM 时回退 CPU。
报告必须读取 `/health` 的实际设备与回退记录，不能仅记录请求的 `cuda`。

## 4. 构造与执行能力

后端提供同步的现有接口：

```python
from astrbot_ex.core.decision.backends.laya import LayaConfig
from astrbot_ex.core.decision.backends.registry import create_backend

config = LayaConfig(enabled=True, allow_live_http=True, port=8769)
backend = create_backend("laya", config=config)
assert backend.execution_allowed is False
```

静态工厂接收代码定义的配置和构造参数，不接收动态 import 路径。
`LayaConfig()` 默认 `enabled=False`、`allow_live_http=False`。
启用模型调用不会授权动作执行。

仅受控测试装配可以使用：

```python
backend = create_backend(
    "laya", config=config, allow_test_execution=True
)
result = service.replace_backend("laya", lambda: backend)
```

`allow_test_execution` 是可信构造参数。
它不属于 Goal、模型请求或模型响应，B08 普通配置表单不能授予它。
EX 默认后端、默认模式和 Jev 的执行限制不改变。

`replace_backend()` 使用已有的 B04 可信切换入口。
切换时服务必须 disabled，且无活动 Goal、动作、模型请求和响应处理。
不能直接赋值 `service.backend`。
切换完成后还要等本次停止操作 `state=proven`，再通过现有模式方法和正式 Goal 入口运行。

### 配置字段

| 字段 | 默认值 | 校验或作用 |
|---|---:|---|
| `enabled` | `False` | 只控制是否允许 `decide` |
| `allow_live_http` | `False` | 真实本机 HTTP 的显式开关 |
| `port` | `8769` | 整数 1–65535，地址固定为 `127.0.0.1` |
| `model` | `typed-decisions` | 只接受这个已核对的别名 |
| `revision` | 固定 bundle SHA | 不接受浮动 revision 或其他模型 SHA |
| `deadline_ms` | `1500` | 1–60000，覆盖准备、health、POST、解析和等待 |
| `max_request_bytes` | `16384` | 1–1048576 |
| `max_response_bytes` | `65536` | 1–1048576 |
| `max_owners` | `4` | 1–4 |
| `max_candidates_per_owner` | `8` | 1–8，候选头仍受预算限制 |
| `max_len` | `1024` | 32–1024 |
| `head_max_len` | `256` | 32–256，且小于 `max_len` |
| `min_interval_ms` | `0` | 0–60000，限制相邻决策开始时间 |

配置冻结为 dataclass，不提供随意替换内部字段的方法。
B08 修改配置时构造新实例，并使用可信后端切换。

## 5. 输入、输出与预算

`decide(snapshot)` 先重新解析 `snapshot.to_dict()`，取得防御性副本。
输入保留 Goal 文本、允许动作、已绑定参数、owner 状态、候选完整含义及观测数据。
观测还保留来源、epoch、seq、age、说明 hash 和 health。
EX 关联 ID 与版本保存在请求记录和返回契约中。

每个 owner 对应 `q0/q1/...` 一题。
每题合法候选对应 `A/B/...`，并保留请求内的原 `option_id` 映射。
start 候选在问题头中使用最长 37 个 ASCII 字符的固定摘要。
完整原始候选含义保留在 state，不用该摘要覆盖任务参数。

state 是 ASCII JSON 字符串，不再让服务器对字典重新序列化。
非 ASCII 内容使用明确的 JSON escape 保留。
`[MASK]` 的原始值通过 JSON escape 保留，避免官方模板删除其字面内容。
这不等于证明英文 checkpoint 能读懂中文任务。

固定 tokenizer 使用 NFC 与 ByteLevel BPE。
ASCII 内容在 NFC 下保持原样，因此字节数可作为保守 token 上界。
适配器同时检查每项候选 48-token 上界、问题头预算和整个 1024-token 上界。
超过预算时返回 `token_budget_exceeded`，不静默裁掉任务或观测。
实际能容纳的候选数会小于配置上限，取决于内容长度。

正式请求显式指定 `model=typed-decisions`、`max_len` 和 `head_max_len`。
每次调用先用 GET health 核对已加载的 checkpoint、revision 和实际设备，再发一个 POST。
无自动重试，无规则回退，也无默认选择第一项。

正式响应必须是 `model/answers/usage/routing`。
顶层 model 必须为 `laya-rl-agent`。
routing 必须精确匹配 typed 别名、bundle 路径和显式路由。
answer 的 owner/question 集合、合法选项及概率键必须精确匹配本次请求。
重复 JSON 字段、非有限值、非 argmax 选择、截断和选项折叠均拒绝。

`confidence` 是归一化熵。
`answer_confidence` 是最大选项概率，适配器把它映射为 EX choice.confidence。
`action.act_probability` 仅进入诊断记录，不能变成动作或权限。
本版本不设置模型侧 `min_confidence`，也不隐式改选 wait。

官方概率保留四位小数。
只有当总和误差不超过 `候选数 × 0.00005 + 1e-12` 时，适配器按原始总和归一化。
请求记录保留原分布、总和、误差界和处理方法。
EX 现有分布校验不放宽。

返回值仍是冻结的 `BackendDecision`。
适配器再次调用 `validate_backend_selection()`。
DecisionService 仍检查当前版本，再决定 shadow 记录或动作下发。

## 6. B08 的只读接口

本节说明 Python 接口，不表示 B08 HTTP 路由已经实现。
B08 复用以下方法，不创建第二套管理服务或执行数据库。

### `status()`

`status()` 不发送 HTTP，不加载模型，也不启动动作。

| 字段 | 含义 |
|---|---|
| `backend` | `laya` |
| `model`、`revision` | 配置的固定模型身份 |
| `enabled` | 是否允许决策 |
| `closed` | 当前适配器是否关闭 |
| `busy` | 本地调用或唯一 transport worker 是否仍活动 |
| `restart_required` | 当前实例是否禁止新的决策 POST |
| `execution_allowed` | 可信构造时的能力 |
| `error_code` | 最近完成的已受理 decide 错误，或 `None` |
| `remote_stop_confirmed` | 固定为 `False`，不把本地取消当作 GPU 停止 |

`busy` 不等于服务端 GPU 的准确活动状态。
`error_code` 不覆盖每次未受理调用的错误。
B08 必须同时记录该次调用返回的异常或 probe 结果，不能用旧状态代替新错误。

### `probe()`

`probe()` 只发送 `GET /health`。
它不创建 Goal，不启动决策 POST，不续租，不更新动作账本。
普通故障返回字典，不要求 B08 从异常消息提取状态。

```python
{"ok": True, "health": {...}, "restart_required": False}
{"ok": False, "error_code": "checkpoint_not_loaded", "restart_required": False}
```

probe 可以在 disabled 或 quarantine 状态下诊断服务。
真实 HTTP 仍需要 `allow_live_http=True`。
已有 transport worker 占用槽位时，probe 返回 `busy`，不新建第二个 worker。
probe 返回 `ok=True` 也不会清除 `restart_required`。

### `last_record`

`last_record` 返回最近一次已受理 decide 记录的防御性副本。
它不是历史数据库，也不是尚未结束的实时请求列表。
probe 不覆盖这份记录。

| 记录字段 | 内容与使用边界 |
|---|---|
| `snapshot_id`、`versions` | EX 关联与版本，不能由模型覆盖 |
| `model`、`revision` | 固定请求身份 |
| `request_body`、`input_sha256` | 实际准备并传递的确定性请求与 hash |
| `owner_mapping` | q/A 键到原 owner/option_id 的映射 |
| `token_budgets` | 字节保守预算，不冒充实际 tokenizer 计数 |
| `health` | 本次调用核对的服务健康、revision、设备与回退记录 |
| `post_attempted` | 进入 POST 路径，不等于服务器已接收 |
| `post_written_to_socket` | 本地 socket 写完标记，仍不证明服务器接收或推理完成 |
| `post_evidence_semantics` | 上述证据的明确含义 |
| `raw_response` | 有界原始响应字符串或解析后的 JSON |
| `http_status` | 有完整 HTTPReply 时记录 |
| `probability_normalization` | 原始概率、处理方法、两个 confidence 与辅助 act_probability |
| `started_monotonic_ns`、`completed_monotonic_ns` | 本地总调用时间 |
| `http_elapsed_ms` | POST 传输耗时，包含本机 HTTP 与响应读取 |
| `server_inference_ms` | 官方 timing header，缺失或非法时为 `None` |
| `phase`、`error_code`、`restart_required` | 本次结束时的位置与结果 |

准备失败时，部分字段为空，或没有生成相应字段。
输入在发送前产生，不代表服务器确实读到它。
B08 必须结合 `post_attempted` 和 socket 证据显示“准备／尝试提交／收到响应”。
模型结果合法也不等于 EX 已受理执行。
B08 还要关联 DecisionService 的受理／丢弃记录，以及 Ledger 中 command_id 的动作事实。

这些数据包含完整任务参数和观测。
B08 把它们作为敏感读取内容，执行统一管理访问检查。
错误响应只显示固定 code，不回显 raw_response 或服务器 traceback。

## 7. 超时、取消、关闭与恢复

适配器最多一个在途调用和一个 transport worker。
超时后仍活动的 worker 保留这个唯一槽位，不不断新建线程。
worker 返回的迟到结果不能通过当前 epoch 检查，也不能直接下发动作。

`cancel()` 使当前调用本地失效。
`close()` 幂等，设置 closed，取消本地等待，并使迟到结果失效。
两个方法均不等待服务端 torch 推理结束，也不终止服务器进程。
模型挂起不能把 EX 停止入口绑定到 HTTP 推理线程。

以下规则决定 quarantine：

1. 尚未尝试 POST 的本地准备或 health 失败，不锁存 `restart_required`。
2. POST 已尝试后发生 timeout、cancel、transport 或响应校验失败，锁存 `restart_required`。
3. `cancel/close` 在已尝试 POST 的当前调用中发生，也锁存该状态。
4. 完整的已知请求拒绝状态 `400/401/403/404/405/413/415/422/503` 不自动锁存。
5. 其他非 200 状态、格式错误、身份错误和协议错误均按 POST 后失败处理。
6. 任意状态都没有自动重试或自动规则回退。

`post_attempted` 是保守边界。
即使连接或写入失败发生得较早，适配器也不猜测服务器是否收到内容。
本版本不区分“服务器已明确算完但答案格式错误”的恢复流程，统一要求重启恢复。

`/health=ok` 不证明旧推理停止。
晚到 HTTP 结果、等待一段时间和重复点击测试也不清除 quarantine。
后端实例没有 reset 方法。

恢复顺序：

1. 通过正式 EX 停止入口取得回执。
2. 等待同一 operation_id 的停止状态为 `proven`。
3. 把 EX 切到 disabled，并完成可信切换要求的空闲检查。
4. 关闭旧后端的本地等待。
5. 终止本任务创建的模型子进程，并观察退出。
6. 用固定身份和单并发环境重新启动该自有服务。
7. 核对已加载 checkpoint、revision、实际设备和预热结果。
8. 构造新的 LayaBackend，通过 `replace_backend()` 安装。
9. 先明确启用目标模式，等待该次停止完成，再提交新的正式 Goal。

B08 不能仅构造新后端便继续请求同一未确认状态的旧服务器。
新后端不知道旧服务是否仍推理，进程归属与恢复证据由可信监督者负责。
停止证明是 Actor 的停止证明，服务器退出证据是模型进程的退出证据，两者不能互换。

EX 停止判定使用：

```python
receipt = service.request_stop("operator_stop")
operation = service.status()["stop"]
stopped = (
    operation["operation_id"] == receipt["operation_id"]
    and operation["state"] == "proven"
)
```

`requested/running` 表示仍处理中。`failed` 表示停止证明失败。
`_stop_pending=False` 不作为停止完成证据。

## 8. 固定错误码

错误异常为 `LayaBackendError`，稳定机器字段为 `.code`。
异常文本只包含固定说明和 code，不加入模型内容、凭据或网络异常文本。
B08 在页面显示 code 和本节说明，不解析错误字符串。

| 分类 | 错误码 | 后续处理 |
|---|---|---|
| 配置 | `invalid_config`、`invalid_limits`、`unpinned_model`、`unpinned_revision` | 拒绝保存或构造，保留现有实例 |
| 调用门禁 | `closed`、`disabled`、`live_http_not_authorized` | 修改受控装配或构造新实例，不启动动作 |
| 单在途与速率 | `busy`、`rate_limited` | 保持当前操作状态，首次调用不自动重试 |
| 必须恢复 | `restart_required` | 按第 7 节恢复，不用健康探测复位 |
| 本地终止 | `canceled`、`deadline_exceeded` | 记录调用失败，结合 POST 边界决定恢复 |
| 快照与输入 | `invalid_snapshot`、`too_many_owners`、`too_many_candidates`、`no_eligible_candidates`、`request_too_large`、`token_budget_exceeded` | 拒绝本次输入，修正上游输入或明确范围 |
| 传输 | `response_too_large`、`transport_failure`、`invalid_http_reply`、`http_rejected`、`http_failure` | 记录 HTTP 状态和 POST 证据，执行 quarantine 规则 |
| 健康与身份 | `invalid_response_json`、`invalid_health`、`checkpoint_not_loaded`、`revision_mismatch`、`device_unverified` | 不提交推理或拒绝结果，查看固定部署身份 |
| 响应协议 | `response_shape`、`routing_mismatch`、`answer_shape`、`unknown_option` | 拒绝选择，无动作回退 |
| 分布 | `invalid_probabilities`、`invalid_confidence`、`choice_not_argmax` | 保留原始数据，拒绝选择 |
| 使用量与契约 | `invalid_usage`、`input_truncated`、`invalid_decision` | 拒绝选择，不放宽冻结 EX 契约 |

`invalid_response_json` 既能来自 health，也能来自 POST 响应。
相同 code 的 quarantine 结果取决于失败阶段，以记录中的 POST 证据为准。

## 9. 验证与结果判读

在 EX 目录运行以下针对性检查。它们不加载模型或连接机器人。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.test_laya_backend \
  tests.test_laya_service_integration \
  tests.test_decision_backend_contract \
  tests.test_decision_runtime_integration -v
```

确定性适配器测试使用合成响应，不加载权重。
正式服务测试使用真实 EX 服务、Dispatcher、Actor 和临时 Ledger，但响应仍为受控 fixture。
这些测试验证协议和调用链，不能证明原始模型选择正确。

真实入口固定三个短场景：move、wait、replan。
默认每个场景重复 8 次，形成 24 个热调用。
预热和正式 EX＋Actor 调用单独记录，使总样本约为 30 次。
错误样本、选错样本和重复场景全部保留，不挑选成功数据。

入口保存：

| 文件 | 内容 |
|---|---|
| `scenarios.json` | 运行前冻结的场景 |
| `calls.jsonl` | 每个热调用的原始记录、选择、正确性及时间 |
| `laya-serve-*.txt` | 本任务模型子进程日志 |
| `actor-integration-partial.json` | 正式 EX＋Actor 的中间和终态证据，异常时也保存 |
| `result.json` | 冷调用、热调用、样本总数、失败数、选择效果、延迟、服务退出证据 |

模型到 Actor 的证据链包含 snapshot_id、原始响应、映射、decision_id、command_id、Actor 接受时间和 Ledger 终态。
`shadow` 只有决策记录，必须无 Actor 命令和动作账本副作用。
停止证据必须关联本次 operation_id。
Goal/config 变化后的旧结果通过确定性测试证明零下发。

报告分别给出模型加载/就绪、预热、HTTP、整体后端调用和快照至 Actor 的耗时。
p50/p95/max 同时给出样本数和失败数。
失败不进入成功调用延迟分位数，但必须另列。

`snapshot_to_actor_ms` 的终点是测试 Actor 回调返回 accepted 的时刻。
它不等于 SQLite 持久化 ACK 的时刻。真实运行保存 succeeded 终态与 event_seq；
确定性正式集成测试逐项验证 accepted、running、succeeded。真实运行未另行导出中间事件列表。

服务仍使用原有 5 Hz 默认调度。
报告把 Goal 等待快照、快照等待模型、模型返回至 Actor 的时间分开。
本任务不为改善数字调整 B04 调度频率。

`model_to_actor_pass=True` 只证明至少一次真实模型选择到达测试 Actor 并写入成功终态。
它不等于所有任务选择正确，不等于 p95 达标，也不等于完整机器人闭环通过。
`selection_quality_pass` 单独记录所有热样本是否都符合冻结场景预期。
当前入口退出码检查调用失败数及至少一次 Actor 成功，不能代替该指标。
正常退出码不是完整产品验收的替代品，必须读取各部分指标。

## 10. 本轮结果与未完成事项

本轮结果见 [根仓库结果报告](B07_LAYA_EX_BACKEND_RESULT.md)。
原始证据见 [evidence/laya_b07_20261001](evidence/laya_b07_20261001/)。

2026-10-01 实测：139 项定向回归通过；31 次真实模型请求，包含一次预热、24 个冻结热样本及 6 个正式 EX 请求。
热组后端整体 p95 为 39.1 ms，5 次 EX 快照至测试 Actor p95 为 44.8 ms。
移动和等待各 8/8 正确，重新规划 0/8，误选 start；任务选择效果未全部通过。
5 次正式 EX 测试动作均为本测试定义的 arm.move.v1，写入 succeeded，未连接机械臂。
这里的替身只有测试回调，未验证物理抓取能力。

B08 的旧任务书尚含“Laya 不是 EX 后端”及“可信切换未实现”的旧描述。
派工前按本交接和前轮 B04 结果修订这两处；本轮未修改 B08 HTTP 或旧任务书。

报告必须分别标明：

- 适配器确定性测试。
- 真实权重推理。
- 正式 EX＋测试 Actor/Ledger 执行。
- 未验证的 ROS／Isaac 物理闭环。

原框架的 Ledger/Dispatcher 超时与 legacy 竞争问题保留原始失败证据。
如果本轮出现同类阻断，先区分适配器问题与框架问题，不删除断言或放宽 timeout。

B08 后续仍需开发管理鉴权、配置 CAS、异步 operation、真实历史追踪和页面接口。
本任务仅交接以上 Python 能力，不开发这些 API。

官方依据：

- [固定源码](https://github.com/NandhaKishorM/laya/tree/6d942c92081fbc139e736bbd9ac0023223c29b7f)。
- [官方服务器](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/serve.py)。
- [官方权重 revision 规则](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/revisions.py)。
- [固定 typed 模型配置](https://huggingface.co/convaiinnovations/laya/blob/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/typed-decisions/rl_agent_config.json)。
