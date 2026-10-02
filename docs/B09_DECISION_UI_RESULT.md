> 历史报告：迁自 AstrEX 原仓库，保留当时日期、路径和验收结论。当前集成状态见 [集成报告](LOCAL_DEVELOPMENT_INTEGRATION_RESULT.md)。

# B09 一级决策页面结果

日期：2026-10-02。**页面已完成，最终浏览器验收 16 组通过，原管理页面回归 7 项通过。** 人工布局查看与隔离实例停止演示留给用户。

入口：`http://127.0.0.1:8765/#/decision`。本轮没有 commit、push 或更新上游。后端业务源码、B04/B07/B08 协议与执行权限保持不变。

## 1. 已交付内容

| 交付 | 实际行为 |
|---|---|
| 一级导航和五个区块 | 状态与控制、配置与 Laya 服务、当前 Goal、实际决策、动作与反馈 |
| 配置管理 | Mock/Jev/Laya 来自真实能力目录。saved/effective 分开。CAS 冲突保留草稿，核对版本后再保存 |
| 显式控制 | 保存、连接测试、应用模式、启动/停止/恢复 Laya 分开操作。停止不受草稿或配置冲突阻塞 |
| 异步操作 | 202 显示处理中，随后查询 operation。终态与错误明确显示。404 不猜测原结果 |
| 请求与动作事实 | 构建快照、实际请求、模型选择、EX 结果和 Ledger 动作分别展示。支持分页、详情、截断和 ID 关联 |
| 停止边界 | 请求停止不等于已有证明。blocked 独立保留。动作失败后取得证明仍显示失败 |
| 刷新与鉴权 | 复用管理凭据和 SSE，凭据仅在内存。隐藏/离页暂停读取。凭据及会话变化使旧关联失效 |
| 展示边界 | 安全文本渲染、JSON 折叠、未知状态保留原值、缺失数据不补造 |

普通 Laya 仍只允许 disabled/shadow。连接测试中的 GET /health 不表示推理通过。Laya replan **0/8** 的已知限制继续展示。

[AstrEX 施工索引](AstrEX_workingTree/00_施工总索引与派工规则.md)和 [B09 任务书](AstrEX_workingTree/B09_低风险_决策前端页面与展示.md)已更新。后者修正了“Laya 只属于控制端评分器”的旧描述。

## 2. 修改范围

- [index.html](../apps/AstrBotEX/dashboard/index.html)：导航、五个区块、未接入区域。新增脚本使用已有 `/dashboard/decision.js` 路由。
- [decision.js](../apps/AstrBotEX/dashboard/decision.js)：针对 B08 的映射、表单、操作、历史及状态管理。
- [app.js](../apps/AstrBotEX/dashboard/app.js)：复用凭据/导航/SSE；一级导航支持浏览器返回。
- [styles.css](../apps/AstrBotEX/dashboard/styles.css)：沿用配色、窄屏布局及有界操作列表。
- [verify_decision_ui.mjs](../apps/AstrBotEX/scripts/verify_decision_ui.mjs) 和 [decision_ui_fixture.py](../apps/AstrBotEX/tests/decision_ui_fixture.py)：一个浏览器入口及小型测试装配。

没有新增管理服务器、HTTP 业务接口或前端框架。没有 Goal 提交、命令重放、直接模型调用、ROS/Isaac 或微调入口。

工作区开始时干净。核对了 405 个已跟踪常规文件。最终仅修改授权的前端和文档，新增文件限于上述功能、验证及证据。暂存区字节、HEAD、remote 和 submodule 状态保持一致，见[保护核对](evidence/b09_20261002/protection-audit.json)。

## 3. 验证环境与结果

环境：EX `.venv` Python 3.12.3、Node v22.15.0、已安装 Chrome 154.0.8037.57。没有安装新依赖。

最终入口：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
node scripts/verify_decision_ui.mjs --output /tmp/astrex_b09_20261002/run-08
```

使用真实的临时 B08 HTTP 服务、SQLite Ledger 和已有隔离 Mock Actor。Laya 服务进程、加载及恢复采用既有 FakeProcess/FakeBackend。普通 Laya 权限没有改变。

少数异常展示和乱序响应使用浏览器侧 HTTP 响应 fixture。它们只存在于验证脚本，不进入正常页面。大记录、未知状态和 Goal 布局数据明确标为 TEST FIXTURE。

| 验收组 | 结果与覆盖 |
|---|---|
| 1–2 路由与鉴权 | 直接 URL、导航、返回、错误凭据、失效后停止读取、SSE 重连；无自动写入 |
| 3 后端与能力 | Mock/Jev/Laya 目录、固定身份只读、普通 Laya/Jev execute 禁用、探测范围准确 |
| 4–5 配置与密钥 | 草稿保持、真实 CAS 409、手工核对、saved/effective、密钥 set/keep/clear 和输入清空 |
| 6–7 Laya 管理 | 202 加载、重复点击、加载中独立停止、superseded、restart_required、显式恢复、服务停止、模式分离 |
| 8–9 动作与停止 | 21 条隔离 Actor 成功动作、账本分页；另一个失败动作进入 blocked，再补可信停止证明，失败与 blocked 均保留 |
| 10 请求记录 | 请求分页及详情、截断、`*_display`、Jev 正文未采集、socket null 为未知、EX 丢弃与模型选择分别显示 |
| 11–12 异常与扩展 | operation failed/blocked/superseded/未知/404、当前及待替代 Goal、空态、恶意文本、未接入区域 |
| 13 刷新边界 | SSE 合并、同类 GET 不重叠、隐藏及离页暂停、真实浏览器断网/恢复、缓存保留、恢复不写入 |
| 14–15 旧响应 | 凭据改变后旧 config 不生效；真实服务器重启后的新会话清除旧操作/关联，草稿需重新核对 |
| 16 页面回归 | 环境表单草稿保持、刷新清除凭据、DOM/URL/浏览器存储不包含新凭据、窄屏无横向溢出 |

最终结果：**16/16 PASS，JavaScript 运行时异常 0**。SSE 通知测试的 3.3 秒窗口内读取状态 3 次，24 个管理 GET 未出现同类重叠。这只是刷新行为检查，不是模型延迟成绩。

原 `verify_dashboard_management.mjs` 另用独立临时实例执行一次：**7/7 PASS**。覆盖既有鉴权、SSE、环境草稿和备份上传/下载。此次测试没有迁移旧 WebSocket token，也不扩大 B08 的凭据保护结论。

`node --check` 覆盖 app.js、decision.js 和新浏览器入口。`git diff --check` 通过。

证据：

- [最终浏览器结果与请求摘要](evidence/b09_20261002/browser-final/result.json)
- [既有页面 7 项回归](evidence/b09_20261002/legacy-regression/result.json)
- [运行环境、命令与源码身份](evidence/b09_20261002/manifest.json)
- [本任务进程及端口清理核对](evidence/b09_20261002/cleanup.json)
- [原始 HTTP/fixture 输出](evidence/b09_20261002/browser-final/fixture.txt)

主动中止读取及浏览器断网时，现有 Python HTTP 处理器会记录 BrokenPipeError。页面保留缓存并标记过期，恢复后继续只读刷新。本轮没有修改后端错误处理。

## 4. 开发期间的失败记录

没有删除失败结果或修改后端断言来取得通过。

| 记录 | 发现与处理 |
|---|---|
| run-01 | 新 fixture 从错误模块导入 OwnerBinding。改为现有 ledger 导入路径 |
| run-02 | 新脚本根路径 `/decision.js` 返回 404。改用已有 `/dashboard/decision.js`，未修改后端路由 |
| run-03 | CDP Runtime.evaluate 期限错误。保留失败，不认定为 B08 根因 |
| run-04 | CDP 期限错误；同时发现测试核对草稿前未等待最新配置版本。补充浏览器错误/对话框诊断，并按新版本条件等待 |
| run-05 | 新停止 fixture 试图改变已终止的动作状态，被账本拒绝。改为先记录 failed，再用已有 reconcile_stop 补停止证明；补齐 fixture 清理 |
| run-06 | 首轮 15 组全部通过 |
| run-07 | 补充 Goal/未知状态、读取不重叠和截图后，16 组全部通过 |
| run-08 | 修正 404 顶部提示、隐藏按钮样式，并加入真实浏览器断网后，最终 16 组全部通过 |

前七轮结果保留在[开发记录目录](evidence/b09_20261002/attempts/)。03/04 的 CDP 期限错误未单独确定根因。后续完整检查未再次出现，不能据此反推它是后端缺陷。

## 5. 截图与未接入事项

截图均带 TEST FIXTURE 标记，不代表模型或机器人表现。

| 截图 | 内容 |
|---|---|
| [状态与停止](evidence/b09_20261002/browser-final/01-stopping-proof.png) | 停止已证明，blocked 仍保留 |
| [桌面首页](evidence/b09_20261002/browser-final/02-decision-desktop.png) | 五区块入口与真实版本状态 |
| [配置与 Goal](evidence/b09_20261002/browser-final/05-config-goal.png) | saved/effective、显式操作与 A.E.B. 占位 |
| [请求与动作](evidence/b09_20261002/browser-final/04-request-evidence.png) | 模型选择、EX discarded、未知 socket 阶段及账本 |
| [Goal 展示 fixture](evidence/b09_20261002/browser-final/06-goal-fixture.png) | 当前/待替代 Goal、未知阶段和恶意文本 |
| [窄屏](evidence/b09_20261002/browser-final/03-decision-narrow.png) | 单列布局，无横向溢出 |

未接入：A.E.B. 任务/步骤队列、反馈序号、公开消息投递、Grounder 候选、选中轨迹、ROS ACK、物理判定。

后续接入只需在真实后端提供 HTTP 投影后扩展映射。当前不轮询不存在的接口，不推算任务百分比，也不发送内部数据到聊天或 TTS。

本轮没有真实模型推理、GPU 实验或机器人运动。隔离测试 Actor 的成功不能写成机械臂物理成功。

## 6. 启动、访问与人工查看

已有 EX 管理实例运行时，刷新并打开 `#/decision`。没有运行实例时：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  -m astrbot_ex.core.api_server --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/#/decision`。从启动信息给出的本地凭据文件读取凭据，在页面输入。不要将凭据放进 URL 或文档。刷新页面后需要重新输入。

人工演示使用临时实例：

```bash
cd /home/sssxy/Projects/AstrEX_project_main/apps/AstrBotEX
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m tests.decision_ui_fixture
```

终端输出临时 `base` 和 `token_file` 路径。打开该 base 的 `/#/decision` 并输入临时凭据。此实例只装配测试替身。

1. 查看布局、saved/effective 和未接入区域。
2. 在终端输入 `{"id":1,"command":"hold"}`，再在页面显式点击“启动 Laya”。这只挂起替身预热。
3. 点击“请求停止”，查看 operation 和停止证明的独立状态。
4. 在终端输入 `{"id":2,"command":"release"}`，查看旧启动被 superseded。
5. 输入 `{"id":3,"command":"quit"}`，退出并清理临时实例。

**需要人工决策的阻塞：无。** 人工查看仅用于界面和隔离管理流程确认，不是机器人停止验收。本轮到此结束，不自动进入控制端开发。
