# B08 决策管理 API 与 B09 交接

本说明描述 B08 的实现接口。测试结果以本轮结果报告和原始日志为准。
本文不声明机器人、ROS 2、Isaac 或微调通过。

本轮验证与已知限制见 [B08 结果报告](B08_DECISION_MANAGEMENT_RESULT.md)。

## 1. 模块边界

B08 管理 EX 的决策配置、后端模式、自有 Laya 服务和查询接口。
B08 不接收新的机器人 Goal，不生成轨迹，也不执行机器人控制循环。
正式 Goal 继续使用冻结的任务契约和现有入口。

```text
管理请求 → 现有 HTTP 服务 → DecisionManagement
                              ├─ DecisionConfigStore / SecretStore
                              ├─ DecisionService 的可信方法
                              ├─ OwnedLayaService
                              └─ RequestHistory

正式 Goal → DecisionService → 后端选择 → 原 Dispatcher / Actor / Ledger
```

主要源码：

- `astrbot_ex/core/api_server.py`：HTTP、旧入口鉴权、SSE 和配置恢复钩子。
- `astrbot_ex/core/decision/config.py`：配置版本、原子保存和密钥文件。
- `astrbot_ex/core/decision/management.py`：管理路由和异步操作。
- `astrbot_ex/core/decision/owned_laya.py`：自有进程、预热、隔离和恢复。
- `astrbot_ex/core/decision/history.py`：实际请求、后端结果和 EX 受理记录。

普通装配中的 Laya 只允许 shadow。HTTP 不能授予 Laya 的执行能力。
隔离测试 Actor 的 execute 权限只能来自可信 `ManagementSettings`。
这个权限不属于 JSON 配置，不进入备份，也不能从 HTTP 恢复。

## 2. 访问与存储

从现有 EX 环境启动管理服务：

```bash
cd /home/sssxy/Projects/AstrbotEX
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  -m astrbot_ex.core.api_server --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/`。启动输出仅列管理凭据文件路径。
读取该本机文件，在原仪表盘的凭据栏输入。
启动 HTTP 和输入凭据都不启动 Laya，也不授权动作。
B08 决策管理使用本说明的 API；一级决策页面已集成，详见 B09 历史报告和本轮集成报告。


HTTP 服务仅绑定 `127.0.0.1` 或 `localhost`。
远程访问使用 SSH 隧道和 HTTP 页面。
旧 `file://` 页面打开方式不属于本轮访问方式。

所有 `/api/` 请求都需要以下请求头：

```http
Authorization: Bearer <本机凭据文件中的值>
```

`Host` 必须是合法回环地址。
存在 `Origin` 时，它必须与 HTTP 来源一致。
没有 `Origin` 的本机脚本仍然需要凭据。
敏感响应使用 `Cache-Control: no-store`，不提供通配 CORS。

凭据位置：

| 文件 | 内容 | HTTP 行为 |
|---|---|---|
| `data_root/secrets/admin.token` | 管理凭据 | 不返回其值；启动输出仅给文件路径 |
| `data_root/secrets/jev-<id>.secret` | Jev 供应商密钥 | 只写；查询仅给是否已配置 |
| `profiles/default/decision.json` | 保存配置和 `secret_ref` | 通过鉴权接口读取或保存 |
| `execution/actions.sqlite3` | 动作账本 | 只查询；不随配置备份恢复 |
| `execution/laya/service-state.json` | 服务代次和退出证据 | 恢复配置不能清除服务隔离 |

密钥目录权限为 `0700`，文件权限为 `0600`。
`secrets` 和 `execution` 位于现有备份 roots 之外。
本轮防泄密范围是新增管理凭据和 Jev 密钥。
旧连接配置中的 WebSocket token 迁移另行安排。
旧 GET 和备份中的这类 token 不因此变成已迁移状态。

旧状态、事件、runtime、动作、插件、连接、环境和备份入口也需要鉴权。
这包括它们的非版本化别名。
旧仪表盘只在页面内存保存凭据。
上传、下载、封面与 SSE 都使用认证请求。
刷新页面后，需要重新输入凭据。

## 3. 配置版本与写请求

标准响应包含四个关联字段：

| 字段 | 含义 |
|---|---|
| `ex_session` | 当前 EX 进程会话 |
| `revision` | 已保存配置版本 |
| `effective_revision` | 实际应用的配置版本；尚未应用时为 `null` |
| `framework_config_revision` | DecisionService 的运行配置版本 |

`saved` 表示磁盘保存的配置。
`effective` 表示已应用的配置。
保存配置不会启动 Laya、请求推理、切换模式、启动 runtime 或提交 Goal。

除 `/stop` 外，POST 必须包含：

```json
{
  "ex_session": "<当前会话>",
  "expected_revision": 3
}
```

使用最近一次 GET 返回的版本，不猜测版本号。
版本或会话冲突返回 `409`。
发生冲突后，读取最新配置，再决定是否重新保存。
不要自动重放配置或控制请求。

`/stop` 必须匹配当前 `ex_session`。
它忽略过期的 `expected_revision`，避免旧配置阻止紧急关门。

配置、密钥和模式应用遵守 disabled 与空闲边界。
有 Goal、未停止动作、待处理请求或停止证明时，接口拒绝应用配置。
Laya 的 `model`、`revision`、`port` 首轮只读。
Python、缓存路径、启动命令和工厂来自可信部署配置。
HTTP 不接收 shell、导入路径、Python callable 或 `allow_test_execution`。

POST 使用严格 JSON 对象和 `Content-Type: application/json`。
请求体上限为 64 KiB。
重复 JSON 字段、非有限数和未知字段属于无效输入。
请求目标最长 4096 字符，不接受片段标记。
管理查询最多 8 个字段，不接受重复查询字段。
重复的 Host、Origin、Authorization 或消息长度头会被拒绝。

## 4. 路由

以下路由都使用前缀 `/api/v1/ex/decision`。

| 方法与路径 | 请求补充字段 | 返回与行为 |
|---|---|---|
| GET `/status` | 无 | 缓存状态、服务状态、操作计数和已知质量限制；不探测或恢复 |
| GET `/backends` | 无 | Mock、Jev、Laya 的真实能力和执行限制 |
| GET `/config` | 无 | `saved`、`effective` 和密钥配置状态 |
| POST `/config` | `config`：完整 saved 对象 | `200`；原子保存新版本，保持运行配置不变 |
| POST `/secret` | `action`；仅 set 使用 `value` | `200`；set、keep、clear 明确分开 |
| POST `/test` | 无 | `202`；独立探测，后续查询 operation |
| POST `/mode` | `mode`：disabled、shadow 或 execute | `202`；按可信边界应用配置与模式 |
| POST `/stop` | 可选 `reason`，1–128 字符 | 先撤授权并请求停止，再返回操作关联 |
| POST `/service/start` | 无 | `202`；显式启动自有 Laya、健康检查和固定预热 |
| POST `/service/stop` | 无 | `202`；先请求停止，证明后结束自有服务 |
| POST `/service/recover` | 无 | `202`；证明、旧进程退出、新进程预热、新后端应用 |
| GET `/operations/{id}` | operation ID | 异步操作状态；未知 ID 返回 `404` |
| GET `/catalog` | 无 | 能力目录、最近候选资格和当前模式 |
| GET `/snapshot` | 无 | 当前及待替代 Goal、最近构建快照、最近请求、最近已完成结果 |
| GET `/decisions` | `cursor`、`limit`、`request_id` | 有界请求记录和下一游标 |
| GET `/actions` | `cursor`、`limit`、`command_id`、`status` | Ledger 中的动作、资源和停止证据 |

`limit` 默认 20，最大 100。
`decisions.cursor` 使用请求序号。
`actions.cursor` 使用 command ID。
这两个游标不能互换。

探测范围因后端不同而不同：

- Mock 只报告本地构造能力，不发送网络请求。
- Jev 报告真实探测未实现，不产生付费云请求。
- Laya 只请求 `GET /health`，不执行模型推理，不续租，也不清除隔离。

`/mode` 不启动模型进程、runtime 或 Goal。
Laya 未就绪时，启用模式返回冲突。
`disabled` 只撤授权，不把保存配置自动应用成新的后端。
模式启用后，仍然需要新的正式 Goal。

## 5. 请求样例

先读取配置。保留完整 `saved` 对象，再修改需要的字段。
以下 JavaScript 只说明调用内容。认证由调用方加入请求头。

```javascript
const current = await get("/api/v1/ex/decision/config");
const saved = structuredClone(current.saved);
saved.backend = "laya";
saved.laya.enabled = true;
saved.laya.allow_live_http = true;
await post("/api/v1/ex/decision/config", {
  ex_session: current.ex_session,
  expected_revision: current.revision,
  config: saved
});
```

保存返回新 `revision`。
后续启动、探测和模式请求使用新版本：

```json
{
  "ex_session": "<当前会话>",
  "expected_revision": 4
}
```

对 `POST /mode` 增加：

```json
{
  "ex_session": "<当前会话>",
  "expected_revision": 4,
  "mode": "shadow"
}
```

普通装配不使用 Laya execute。
服务启动和探测完成后，再显式请求模式应用。

密钥只通过 `/secret` 修改：

```json
{
  "ex_session": "<当前会话>",
  "expected_revision": 4,
  "action": "set",
  "value": "<供应商密钥>"
}
```

`keep` 和 `clear` 不发送 `value`。
空字符串不能表示 clear。
`keep` 不增加版本；set 和 clear 增加版本。
不要把请求中的真实密钥保存进调试日志。

## 6. 异步操作与停止

`202` 只表示操作已受理，不表示模型加载完成或动作已停止。
响应的 `operation_id` 用于查询 `/operations/{id}`。

操作状态：

| 状态 | 含义 |
|---|---|
| `pending` | 已受理，尚未开始处理 |
| `running` | 正在处理 |
| `succeeded` | 本操作完成其指定范围 |
| `failed` | 明确失败 |
| `blocked` | 停止证明、旧请求或服务隔离阻止继续 |
| `superseded` | 更新的配置、停止或其他意图取代本操作 |

操作关联会话、配置版本、后端、服务代次和时间戳。
活动操作最多 8 个；保留最近 128 个已完成操作。
记录裁剪不删除仍在处理的操作。

停止请求先关闭执行门禁，并取消本地后端等待。
加载和健康检查不占用这个关门入口。
后续 operation 只有收到匹配的停止证明后才报告成功。
`cancel()`、HTTP 返回、GUI 暂停和健康检查都不证明物理停止。

停止与模型恢复是两件事。
POST 已尝试后，超时、取消或结果不确定会锁存 `restart_required`。
健康探测不能清除这个状态。
恢复必须确认本模块自有旧进程已退出。
随后启动、预热新服务，并通过可信方法替换后端实例。
恢复完成仍为 disabled，不重放旧请求或 Goal。
再次执行需要明确模式授权和新 Goal。

从磁盘读到的旧 PID 只是审计数据，不是杀进程的权限。
不能确认所有权时，接口保留 `ownership_unknown`，交给人工处理。

## 7. 请求证据与动作事实

记录区分以下阶段：

1. EX 构建快照。
2. 后端收到该快照并准备请求。
3. POST 已尝试。
4. 本机 socket 写入完成。
5. 收到 HTTP 响应。
6. 模型返回候选选择。
7. EX 受理、拒绝或丢弃选择。
8. Ledger 记录实际动作状态。

`prepared`、`post_attempted`、`post_written_to_socket` 和 `response_received` 是不同字段。
socket 写入不证明远端收到，也不证明动作完成。
实际请求缺失时，记录明确标记；不能用重建快照冒充实际输入。

关联字段包括 request ID、snapshot ID、会话和各版本。
Laya 记录还包含短选项到原候选 ID 的映射、模型 revision、耗时和输入 hash。
hash 对应完整实际字节，不因脱敏或展示截断改变。
截断展示只供阅读，不能用于重新执行。

Laya 的完整实际请求/响应已经接线。Jev 目前仅关联其既有 hash、耗时和错误诊断。
Jev 完整正文/响应缺项如实为空；本轮没有真实云端探测或推理。

请求历史仅保留内存中的最近 128 项。
单项记录的展示预算为 64 KiB，不是整页 64 KiB。
读取接口返回 `record_truncated`、原始大小和下一游标。
超预算时，部分字段会改为对应的 `*_display` 展示。
B09 必须读取实际返回的预算与截断标记，不能假定大输入完整展示。

`/actions` 读取 Ledger，不创建或重放动作。
`admitted` 表示门禁受理，`accepted` 表示 Actor 接收。
`running` 表示实际进度，`succeeded` 表示业务成功。
不能用模型选择、Topic 发布或前端 operation 成功代替这些事实。

## 8. 错误处理

HTTP 错误格式：

```json
{
  "ok": false,
  "code": "revision_conflict",
  "message": "revision_conflict",
  "ex_session": "<当前会话>",
  "revision": 4,
  "effective_revision": null,
  "framework_config_revision": 1
}
```

认证和 Host/Origin 错误不要求返回配置版本。
operation 的失败原因位于 `operation.error_code`。
不要从 error 文本推断动作已停止。

| HTTP 状态 | 常见 code | 调用方处理 |
|---|---|---|
| `400` | `invalid_json`、`invalid_body_size`、`unknown_request_field`、`invalid_config` | 修改输入，不自动重试 |
| `400` | `duplicate_security_header`、`invalid_request_target`、`duplicate_query_field`、`invalid_query` | 删除重复字段，缩短请求目标 |
| `400` | `readonly_laya_identity`、`readonly_jev_identity` | 保留只读模型身份 |
| `400` | `invalid_secret_action`、`invalid_secret_value` | 明确使用 set、keep 或 clear |
| `400` | `invalid_pagination`、`invalid_action_cursor` | 修改分页参数 |
| `401` | `unauthorized` | 重新提供本机管理凭据 |
| `403` | `invalid_host_or_origin` | 使用同源回环 HTTP 页面或本机脚本 |
| `404` | `route_not_found`、`operation_not_found` | 检查路径或重新查询当前状态 |
| `405` | `method_not_allowed` | 使用路由指定的方法 |
| `409` | `session_conflict`、`revision_conflict` | 读取最新会话与配置 |
| `409` | `backend_execute_not_allowed` | 遵守真实后端能力，不绕过门禁 |
| `409` | `restart_required`、`ownership_unknown` | 显式恢复；所有权不明时人工处理 |
| `429` | `operation_capacity` | 先查询已有操作，避免重复提交 |
| `500` | `config_write_failed`、`invalid_saved_config`、`management_request_failed` | 保留证据并检查部署或存储 |

后端或停止检查还能返回其固定边界错误码。
例如 `backend_switch_requires_disabled`、`backend_switch_request_in_progress`、`stop_not_proven`。
这些表示当前状态不允许继续，不表示可以忽略保护条件。

## 9. B09 交接

B09 继续使用现有导航和页面结构。
B09 不决定候选，不修改安全门禁，也不私建模型服务。

页面至少区分：

- 保存配置与实际应用配置。
- EX 调度后端与未来控制端轨迹评分器。
- 当前 Goal、最近构建快照、实际请求和实际结果。
- 模型选择、EX 受理结果和 Ledger 的动作状态。
- 停止请求已受理、正在证明和停止已证明。
- 原始模型结果与规则回退结果。

配置草稿独立于轮询状态。
`409` 时保留草稿，并显示服务器新版本。
`202` 时显示处理中，通过 operation 查询终态。
旧响应不能覆盖新会话、新配置或新 operation。
SSE 的 `decision_changed` 只通知关联 ID。
页面通过鉴权 GET 读取详细记录，不能从 SSE 重建实际输入。

刷新、重连或打开页面不发送执行写请求。
模型或插件文本使用安全文本渲染。
管理凭据不进入 URL、localStorage、错误日志或下载文件。

未来控制插件可以把 Grounder、轨迹评分和物理结果放入业务 `details`。
B09 展示这些事实，不把测试 Actor 结果写成机器人任务通过。
Laya 已知 replan 场景 0/8 的失败结果继续保留。
本说明不改变模型提示、候选策略或质量结论。
