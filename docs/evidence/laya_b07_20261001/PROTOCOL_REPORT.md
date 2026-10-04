# Laya 0.3.22 官方协议核对（只读）

核对代码固定为 `6d942c92081fbc139e736bbd9ac0023223c29b7f`。
本报告仅下载官方源码和模型 JSON 元数据到 `/tmp`。没有权重下载、安装、服务启动或真实推理。

## 安装和运行

`pyproject.toml`：Python >=3.10；torch>=2.0.0、transformers>=4.48.0、safetensors>=0.4.0、huggingface_hub>=0.20.0、numpy>=1.20.0。
serve extra：fastapi>=0.110.0、uvicorn>=0.27.0、python-multipart>=0.0.9。
CLI `laya-serve` 对应官方 `laya.serve:main`。不需要 TileLang、ONNX、MCP 或通用训练依赖。

在独立 Python 3.12 环境中安装固定源码的 `[serve]` extra，随后冻结实际依赖版本。
建议沿用官方 CLI（端口只是例子，启动前检查占用）：

```bash
LAYA_HOST=127.0.0.1 LAYA_PORT=18080 LAYA_DEVICE=cuda LAYA_MODELS=typed-decisions LAYA_PRELOAD=1 LAYA_AUTO_TASK=0 LAYA_MAX_LOADED=1 LAYA_MAX_CONCURRENT=1 LAYA_MAX_TOKEN_BUDGET=1024 LAYA_REVISION=55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851 laya-serve
```

空 `LAYA_MODELS` 会预加载全部模型，必须显式指定。`LAYA_AUTO_TASK=0` 避免工作流自动路由，但每个请求仍须显式指定 `model=typed-decisions`。
CLI 默认 Router 在 bundle `convaiinnovations/laya` 中加载 `typed-decisions/`，不是 standalone 仓库。
官方 `revisions.py` 支持 `LAYA_REVISION`。上面的 bundle SHA 已通过 Hugging Face 固定 revision 元数据核对存在。
standalone typed 仓库的 reviewed SHA 为 `1a793eb568e6718f15941d08f85432581df534e3`，不能把它应用到 bundle。

## 模型身份、大小与预算

固定 bundle 模型配置：ModernBERT-large，`max_len=1024`、`head_max_len=256`、`amp_dtype=bf16`。
HF 元数据列出总参数 421,293,830（F16 421,293,827，F32 3）。
`typed-decisions/model.safetensors` 是 842,609,220 B；5个必要文件总计846,195,716 B。
权重 LFS SHA256：`4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e`。

这不是自回归聊天大模型。它对给定选项进行编码和评分。typed checkpoint是在四类合成业务工作流上训练的英文模型，机器人动作选择能力尚未验证。
不能把官方业务 benchmark 或GPU延迟数字当成本机机器人结果。

CPU可以运行；`LAYA_THREADS` 应按实际CPU限制。GPU请求可能在加载或OOM时回退CPU。
`/health` 的 `checkpoint_devices`、`cpu_fallbacks`、`device_is_preference` 是实际设备证据。
Stock模型驻留使用默认F32参数，再用autocast做计算，不能用下载文件0.84GB推断全部显存占用；实际显存和延迟需要测量。
CUDA能力<8时官方自动用fp16，其余读取checkpoint bf16偏好。

`head_max_len` 是上限，实际状态空间取决于实际问题和选项头：`max_len - actual_head_len - 1`。
每个选项文字最多48 tokens；多选项会进一步均分截短；长instructions也会截短。
不能只靠字符数量声称完整token预算已验证。
官方返回 `usage.truncated`、`state_tokens_dropped`、`truncated_questions`；有选项折叠时返回 `usage.options`。
第一版应拒绝任何状态截断和选项折叠。候选文字用可回放的短摘要，必要字段放在摘要前部。
若要发请求前精确预算，可用固定模型tokenizer和官方 `build_sequence`；不要为此把torch推理依赖引入EX主环境。

## HTTP请求及响应

`POST /v1/systemone`；显式 `model="typed-decisions"`。可同时指定 `max_len=1024`、`head_max_len=256`。
HTTP还可接受 `task/lang/lang_guess/min_confidence`；首轮不需要自动语言或任务路由。
配置 `LAYA_API_KEY` 时Bearer鉴权；`/health` 无Bearer要求。

下面是**接口形状示例，不是真实推理成绩**：

```json
{
  "model": "typed-decisions",
  "max_len": 1024,
  "head_max_len": 256,
  "state": {"goal": "Move the test token to bin A", "token_ready": true},
  "questions": {
    "test_owner": {
      "type": "choice",
      "instructions": "Choose one permitted action for the current goal.",
      "criteria": {"A": "Move the ready token to bin A", "B": "Wait without moving"}
    }
  }
}
```

实际响应顶层是 `model/answers/usage/routing`。
顶层 `model` 为固定字符串 `laya-rl-agent`，不是请求别名。
默认CLI对应 `routing.model=typed-decisions`、`routing.repo=convaiinnovations/laya/typed-decisions`。
显式路由的 `routing.reason` 为 `explicit model='typed-decisions'`，另有 `detection=null`、`workflow=null`。
`GET /health` 的 `revisions[typed-decisions]` 核对权重SHA；响应本身没有权重revision字段。
因此输入hash、模型repo、revision和EX关联ID必须由适配器记录；不能相信模型创造版本字段。

Choice answer字段：`type/choice/probabilities/confidence/answer_confidence/action`。
`action` 是 `{"act_probability": number}`，由辅助action-head返回，不是EX action_id，必须仅作诊断。
`min_confidence` 非零时还可能有 `low_confidence=true`，它没有替换choice。
适配器若拒绝低信心结果应明确拒绝，不使用隐式规则wait继续动作。

`confidence` = 1 - normalized Shannon entropy；`answer_confidence` = max(probability)。
前者不是选中答案正确概率。后者也不能在未做独立校准时等同机器人可靠性。
若EX choice.confidence保留后者，应在说明中声明其语义，并记录原始entropy字段。

概率及confidence按4位小数舍入。分布和可能与1相差最多N*0.00005（加浮点epsilon）。
先校验键精确匹配、每项finite且0..1、choice在该owner且为rounded argmax。
仅在此舍入误差内按实际sum归一化，记录raw分布、sum、normalization方法；其他失真直接拒绝。
随后使用现有冻结 `BackendDecision` parse/selection校验，不放宽框架1e-9总和断言。

未知HTTP model会被 `_resolve_model` 变成None并自动路由，不能指望服务端拒绝。
适配器必须用固定别名白名单，并核对顶层model、routing.model/repo及健康端点revision。

## 候选映射和输入

每个owner一题，仅放本次快照合法候选。按照快照稳定顺序给A/B/C等键；每题维护独立 `short_key -> original_option_id`。
不能将不同owner候选混入一题。回答owner集合必须精确相等；unknown key、漏owner、重复JSON字段均拒绝。
映射回原option_id；Goal/Action/DecisionSnapshot/BackendDecision协议不变。
不要改变已绑定参数，不让Laya新造action/owner/Goal/effort/坐标。
状态摘要至少保留Goal语义、候选参数摘要、实际owner状态及有关观测；所有删减摘要和映射可回放。

## 并发、超时和关闭

官方服务：一个 `ThreadPoolExecutor(max_workers=1)`、async inference gate、请求admission semaphore。
设置 `LAYA_MAX_CONCURRENT=1`：超额请求返回503和Retry-After=1；适配器首轮不自动重试。
官方没有取消路由、请求ID或当前推理busy查询。`/health=ok` 只表示服务健康，不能证明GPU空闲。
停止客户端HTTP等待不会停止正在执行的torch线程。
正常客户端断连时server通常继续handler，admission仍占用；若ASGI task被取消，finally可能释放gate/admission而线程继续。
所以仅MAX_CONCURRENT=1不能覆盖所有取消情况下的远端工作完成证明。

严格最小策略：已发送HTTP后timeout/cancel且没有完整响应时，适配器把remote_work_unresolved锁存，拒绝新decide，不自动重试/回退。
独立probe只读，不能以health恢复该锁存。
只有显式重启本任务服务并重建后端（或取得更强完成证据）才恢复。
此策略不修改官方服务器、阻止任务堆积、保持EX停止入口响应；但会要求人工恢复，应先向用户说明与确认。
普通完整200响应或明确请求拒绝不等同挂起，需要区分发送前/发送后/完整返回。
close只取消本地等待并让迟到结果失效，不声称GPU已停止。官方ASGI关闭使用pool.shutdown(wait=True)，真实服务进程清理另设有界TERM/KILL并仅作用本任务进程。

## 官方来源

- [pyproject.toml](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/pyproject.toml)
- [serve.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/serve.py)
- [router.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/router.py)
- [agent.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/agent.py)
- [common.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/common.py)
- [revisions.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/revisions.py)
- [confidence.py](https://github.com/NandhaKishorM/laya/blob/6d942c92081fbc139e736bbd9ac0023223c29b7f/laya/confidence.py)
- [固定权重元数据](https://huggingface.co/api/models/convaiinnovations/laya/revision/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851?blobs=true)
- [固定typed配置](https://huggingface.co/convaiinnovations/laya/blob/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851/typed-decisions/rl_agent_config.json)


## 固定tokenizer与零EX推理依赖的预算证明

已读取固定revision的tokenizer JSON（非权重）：
- `normalizer={"type":"NFC"}`。
- `pre_tokenizer={"type":"ByteLevel","add_prefix_space":false,"trim_offsets":true,"use_regex":true}`。
- `model.type=BPE`，dropout/unk_token/subword_prefix/end_of_word_suffix均null。
- 自动special处理只在add_special_tokens=true时生效，官方build_sequence所有文本编码为false，再手动拼装。
- tokenizer文件SHA256见本目录artifact_manifest.json。

**不能宣称原始UTF8字节数无条件约束token数。** NFC会展开某些字符，例如U+0344原2字节变两个组合字符共4字节。

最小明确策略：state传ASCII字符串，由 `json.dumps(summary,ensure_ascii=True,separators=(',',':'),allow_nan=False)` 构造。
server收到str后直接用该字符串，而不是再dump一次。criteria描述和instructions同样要求ASCII；用户非ASCII字段可使用显式可回放的ASCII JSON摘要。
此策略保障信息编码保留，但不证明typed英文模型读懂中文escape。首轮预冻结任务用英文，其他语言效果不承诺。

ByteLevel预tokenizer不添加prefix，BPE只能把字节合并；ASCII为NFC不变、无tokenization normalization扩张。
对于第一个正常choice问题：

```python
I = len(("choice question: " + instructions).encode("ascii"))
O = [len((" " + key + ": " + desc).encode("ascii")) for key, desc in criteria.items()]
S = sum(1 + size for size in O)  # 每个option一个MASK
assert max(O) <= 48             # 第一层option token截短不会触发
assert S <= 256 - 16            # 第二层per-option切分不会触发
assert I <= 256 - S             # instruction也完整
assert I + S + 4 + len(state.encode("ascii")) <= 1024
```

4是一个CLS、head SEP、options SEP、state SEP。每个owner单独计算，不能按全体问题总token误判单问题预算。
空desc时官方render_options只呈现key，需要按精确render分支计算；推荐摘要非空。
此数值是保守token上界，可能拒绝实际上能放下的长文本；报告应说“byte-based保守预算”，不是实际token数量。
服务响应usage截断/选项折叠仍须拒绝，作为部署模型/tokenizer一致性的二次核对。

## 依赖版本锁定限制

官方固定pyproject只有依赖下界，没有完整dependency lock。不能给尚未安装/实测的任意torch/transformers版本号并声称受官方验证。
可选兼容安装由主任务在独立环境解析后冻结；报告记录torch/CUDA、transformers、tokenizers、huggingface_hub、numpy、fastapi、uvicorn、safetensors实际版本。
官方revision、checkpoint revision和LFS SHA已有精确身份，但依赖实际pins尚未取得，因为此只读任务没有安装。
