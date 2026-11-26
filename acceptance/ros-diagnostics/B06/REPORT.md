# B06 环境与端口诊断界面 —— 验收报告

| 项 | 值 |
| --- | --- |
| 任务包 | `AstREX_ROS2诊断与日志改造任务包_20261031` |
| 仓库 | `D:\Code\AstrBotEX-work\ros-diag-b04-b06`（AstrBotEX worktree） |
| 分支 | `feat/ros-diag-b04-b06` |
| 基线 commit | `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 操作系统 | Windows-11-10.0.26200-SP0 |
| Python / Playwright | 3.12.10 / 1.61.0（Chromium headless，1100×900 与窄屏两档） |
| 结论等级 | **PASS（模块验收 + 浏览器离线夹具）** |

> 本报告证明环境页在浏览器里的渲染与交互。真实后端诊断数据、真实 ROS 端口属 B07/B08/B10，本次**未执行**。

---

## 1. 交付物

| 文件 | 变化 | 说明 |
| --- | --- | --- |
| `dashboard/environments.js` | +517 / −16（726 行） | 改造。诊断面板、健康分列、刷新按钮、代际防串 |
| `dashboard/environment-diagnostics.css` | 新增 286 行 | 诊断面板与健康芯片样式 |
| `scripts/verify_environment_diagnostics_ui.py` | 新增 890 行 | Playwright 验收，含 mock 后端与 64 条断言 |

`index.html`、`app.js`、`styles.css` **未改动**——容器由 `environments.js` 在挂载时按需创建（也兼容 B08 预置容器）。

## 2. 核心改动

### 2.1 资源状态与收数健康彻底分开

旧版把 `state=ready` 直接写成"收发端点就绪"，等于把"句柄建好了"说成"数据通了"。现在每个端点卡片显示两列：

| 列 | 来源 | 语义 |
| --- | --- | --- |
| 资源状态 | `endpoint.state` | 原生资源是否创建成功 |
| 收数健康 | `sample.data_health` | 这个窗口到底有没有收到数据 |

`data_health` 九个取值全部有独立文案与色档：

| 值 | 文案 | 色调 |
| --- | --- | --- |
| `not_monitored` | 未监测 | 中性 |
| `disabled` | 已禁用 | 中性 |
| `paused` | 已暂停 | 中性 |
| `unavailable` | 资源不可用 | 错误红 |
| `warming_up` | 启动宽限中 | 警告黄 |
| `no_data` | 未收到数据 | 警告黄 |
| `receiving` | 正在收数 | 正常绿 |
| `stale` | 数据陈旧 | 警告黄 |
| `unknown` | 证据不足 | 未知灰 |

关键约束已实测：`state=ready` + `data_health=no_data` 时**同时显示两列且用黄色**，不会因为 ready 就显示绿色；`matched_peer_count=null` 显示"未知"而不是 0。

### 2.2 publish 端口不许声称对端收到

发送端口只写"本地已发布 N 条"，并明写"不代表远端已收到"。实测断言：`publish` 端口文案中不出现"远端接收已证实"类表述，`tx_published` 增长不会被解读为送达。

### 2.3 时间与策略可追溯

- 接收 age、消费 age 使用后端单调时钟值，不在前端用墙钟反推；
- 显示监测策略（mode）与观察窗口时长，让"为什么还没报警"可解释。

### 2.4 诊断面板

`#environmentDiagnostics` 单容器，内部结构：

```
environmentDiagnostics
├── head
│   ├── environmentDiagnosticsMeta             采集年龄 / 刷新状态
│   └── environmentDiagnosticsRefreshButton    重新检查
├── environmentDiagnosticsStatus               结论芯片
├── environmentDiagnosticsSummary              预检摘要（facts 网格）
├── environmentDiagnosticsIssues               活跃问题卡片列表
└── environmentDiagnosticsNote                 说明 / 错误提示
```

- 结论芯片：`全部正常` / `存在告警` / `存在错误` / `证据不足` / `监测已暂停`。
- 每张问题卡：等级 + code + 范围（scope）+ 证据 + 建议 + "查看日志"。
- **"查看日志"只做两件事**：`EXLogs.setFilter({text: issue.code})` + 切到日志页；不发任何 POST。
- 暂停时结论是"监测已暂停"并写明原因，绝不显示"全部正常"；`current=false` 时同理，只说明"尚未采集"。
- `partial=true` 时结论降级为告警，说明"缺失项按未知处理"。

### 2.5 刷新与防串

- 诊断 GET 并入既有 `Promise.allSettled` 轮询，不新增轮询器；
- 按 `session_id + generation + revision` 校验，旧代际响应直接丢弃；晚到的慢响应不覆盖已渲染的新结论；
- 刷新按钮 POST `/api/v1/ex/environments/diagnostics/refresh`，携带 `expected_session` / `expected_revision`，**只 POST 一次**；
- 刷新风暴合并：按钮在飞行中禁用（`busy`），不产生并发请求；
- `202` → 进入完成态等待下一轮轮询；`409` → 提示"环境在请求期间发生变化"并重新读取权威环境状态；`401/403` → 提示重新输入凭据；`503` → 提示服务正在关闭、稍后重试；
- 以上四种错误**都不会触发** `/environments/select` 之类的环境切换 POST。

### 2.6 草稿保护

未保存的环境 namespace 与插件 topic 草稿在轮询/诊断刷新/事件刷新期间保持不变（实测：填入 `/unsaved_acceptance` 后经历两轮失败刷新与一次诊断刷新，输入框内容不变）。

## 3. 运行命令与结果

```
"C:/Users/17088/AppData/Local/Programs/Python/Python312/python" \
  scripts/verify_environment_diagnostics_ui.py --output acceptance/ros-diagnostics/B06/ui
```

- 退出码：`0`
- 结果：**64 passed / 0 failed**
- `page_errors`：`0`
- `scope`：`OFFLINE_BROWSER_FIXTURE`
- 证据：`acceptance/ros-diagnostics/B06/ui/results.json`

### 覆盖分组（全部 PASS）

| 组 | 条数 | 关键断言 |
| --- | --- | --- |
| 端点双列与文案 | 9 | ready+no_data 双列黄、matched=null 显未知、mark 端口默认未监测、publish 不声称送达、age 用单调值、显示策略与观察窗口 |
| 诊断面板结构 | 7 | 单容器、告警结论、问题 code/证据/建议、等级与范围、采集年龄与刷新状态、预检摘要、"查看日志"入口 |
| 查看日志 | 3 | 按 code 设置筛选、导航到日志页、不发 POST |
| 暂停 / 无数据 / 不完整 | 8 | 暂停写原因且不显正常、`current=false` 不显正常、证据不足中性色、partial 降级告警 |
| 恢复 | 3 | 活跃问题清空、显示无活跃问题、不清空日志历史 |
| 失败与草稿 | 5 | 失败前问题已渲染、草稿保持、部分接口失败不清空其它结果、读取失败标过期且保留已渲染问题 |
| 代际与竞态 | 3 | 旧 generation 被拒、不覆盖新数据、晚到旧响应不覆盖新结论 |
| 刷新 | 8 | 刷新中提示、只 POST 一次、带 expected_session/revision、不产生其它 POST、202 完成态、409/401/503 各自提示且不触发 select |
| 安全 | 4 | 诊断 401 提示、恢复后回常规说明、问题文本纯文本渲染、注入脚本未执行 |
| 可用性 | 2 | 键盘可触发刷新、窄屏无横向溢出 |
| 插件面板 | 4 | 收数健康+资源状态、接收/消费 age 与策略、发送端口写本地发布、草稿不被刷新覆盖 |
| 全局约束 | 1 | 全程无 select/start 类 POST |
| 预置容器兼容 | 3 | B08 预置时不重复创建、子节点不重复、预置容器同样渲染结论 |

### 截图

| 文件 | 内容 |
| --- | --- |
| `ui/environment-diagnostics.png` | 环境页整页（诊断面板 + 端点卡片） |
| `ui/environment-diagnostics-narrow.png` | 窄屏整页（验证无横向溢出） |
| `ui/panel-diagnostics.png` | 诊断面板近景 |
| `ui/panel-endpoints.png` | 端点卡片近景（资源状态 / 收数健康 / 对端 三列对齐） |

截图不含凭据；mock 数据中的人名、主机名均为 `mock` 占位。

## 4. 未执行项（NOT_RUN）

| 项 | 原因 |
| --- | --- |
| 真实后端 `/api/v1/ex/environments/diagnostics` 联调 | 依赖 B04 服务在真实进程中启动，本机无运行中的 AstrBotEX 服务 |
| 真实 ROS 端口与 `data_health` 状态迁移 | 无 ROS 2 安装 |
| 真实 SSE 事件触发下的诊断面板刷新 | 依赖 B03/B08 |
| 鉴权中间件对诊断 GET/POST 的真实 401/403 响应 | 属 B08；本次用 mock 响应验证前端分支 |
| 长时间停留（>30 min）下的轮询稳定性 | 未做长时观察 |

以上不计入通过数。

## 5. 与 B08 集成的注意点

1. `environments.js` 会在挂载时自建 `#environmentDiagnostics`；若 `index.html` 已预置同名容器，则复用不重建（两种情形均已验收）。
2. 需要在 `index.html` 的 `<head>` 引入 `environment-diagnostics.css`，位置放在 `styles.css` 之后。
3. "查看日志"按钮依赖 `window.EXLogs`（B05）。若 `EXLogs` 未挂载，按钮仍会切换页面但不会设置筛选——建议 B08 保证 B05 先于本页脚本加载。
4. 诊断接口路径按 C01：GET `/api/v1/ex/environments/diagnostics`、POST `/api/v1/ex/environments/diagnostics/refresh`。若 B08 最终路径不同，只需改 `diagnosticsRequest` 的两处调用。
