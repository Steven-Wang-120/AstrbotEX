# B05 黑底彩色日志组件 —— 验收报告

| 项 | 值 |
| --- | --- |
| 任务包 | `AstREX_ROS2诊断与日志改造任务包_20261031` |
| 仓库 | `D:\Code\AstrBotEX-work\ros-diag-b04-b06`（AstrBotEX worktree） |
| 分支 | `feat/ros-diag-b04-b06` |
| 基线 commit | `c1a4b89f04b4923611af524f4eb622e6fd011757` |
| 操作系统 | Windows-11-10.0.26200-SP0 |
| Python / Playwright | 3.12.10 / 1.61.0（Chromium headless） |
| 结论等级 | **PASS（模块验收 + 浏览器离线夹具）** |

> 本报告证明日志组件在真实浏览器里的**计算样式与交互**。真实 SSE 长连接、跨实例重连、慢消费者溢出属 B03/B08，本次**未执行**。

---

## 1. 交付物

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `dashboard/logs.js` | 600 | 新增。IIFE，只暴露 `window.EXLogs`，自身不建立任何连接 |
| `dashboard/logs.css` | 268 | 新增。全部选择器限定在 `#page-logs` 作用域内 |
| `scripts/verify_log_ui.py` | 451 | 新增。Playwright 验收脚本，生成夹具 + 断言 + 截图 + `results.json` |

未修改 `index.html`、`styles.css`、`app.js`——挂载点与 `<link>` 引入由 B08 在集成时接入（见交接说明）。

## 2. C05 颜色契约实测

脚本读取**计算样式**（`getComputedStyle`），不是截图目测。

| 语义 | 契约值 | 实测 `rgb()` | 断言 |
| --- | --- | --- | --- |
| 终端背景 | `#111111` | `rgb(17, 17, 17)` | PASS |
| 正常正文（INFO） | `#6CC070` | `rgb(108, 192, 112)` | PASS |
| 警告正文（WARNING） | `#FFD166` | `rgb(255, 209, 102)` | PASS |
| 错误正文（ERROR / CRITICAL） | `#FF6B6B` | `rgb(255, 107, 107)` | PASS |
| 元数据（时间戳等） | `#9CA3AF` | `rgb(156, 163, 175)` | PASS |

**整行正文着色**，不是只给小徽章上色：`.log-level-warning .log-message { color: var(--exl-warning) }`。WARNING 行在展开前就有可辨识标记（左侧色条 + 徽章），不必点开才知道是警告。

## 3. 等级解析优先级（实测）

```
data.log.level  →  data.severity  →  data.level  →  类型下限  →  生命周期 INFO  →  DEBUG
```

- `plugin_fault` 等类型即使不带 `severity` 也判为 ERROR；
- `data.log.level` 覆盖类型推断；
- 带非空 `data.error` 的包不能降到 DEBUG 以下。

## 4. 安全与有界

| 项 | 做法 | 实测 |
| --- | --- | --- |
| HTML 注入 | 全部文本走 `textContent`，绝不拼 `innerHTML` | `<img onerror>` 作为文本显示，注入的 handler 从未执行 |
| ANSI 转义 | 先剥 OSC/CSI 序列，再清 C0/C1 控制字符（保留 `\t\n\r`） | 通过 |
| 超长详情 | 单条详情截断到 16 KiB 并标注"详情已截断" | 通过 |
| 缓存上限 | 1000 条 / 约 2 MiB 文本，超出按最旧淘汰 | 注入 10 000 条后渲染行 ≤ 1000，最新事件仍在 |
| DOM 上限 | 行元素与详情面板一起淘汰，不留孤儿节点 | 通过 |
| 去重 | 按 `eventId`，保留 4000 个近期 id | 重放同 id 被丢弃，同文不同 id 保留 |

## 5. 交互行为

- **自动滚动**：暂停后滚动位置不动，新事件累计"未读"计数；恢复后滚到底并清零。
- **清空**：只清视图，不重放历史（重连不会把清掉的日志再灌回来）。
- **缺口/重置**：`control({type:'gap'|'reset'})` 渲染到 `#logGapNotice`，用 `data-kind` 区分——缺口和重置在视觉上不同。
- **筛选**：按等级、按来源、按文本；文本搜索是**字面匹配**，输入 `[` 不会被当正则炸掉。
- **导出**：导出当前可见行为 CSV（含等级与 code 列），导出内容不含任何标签。
- **主题无关**：切到浅色主题后终端仍是黑底、INFO 仍是绿色——日志区不受全局主题影响。
- **重复挂载**：`mount()` 前先 `destroy()`，反复挂载不会叠加监听器。

## 6. 运行命令与结果

```
"C:/Users/17088/AppData/Local/Programs/Python/Python312/python" \
  scripts/verify_log_ui.py --output acceptance/ros-diagnostics/B05/ui
```

- 退出码：`0`
- 结果：**40 passed / 0 failed**
- `page_errors`：`0`
- `scope`：`OFFLINE_BROWSER_FIXTURE`
- 证据：`acceptance/ros-diagnostics/B05/ui/results.json`

### 覆盖的 40 条断言（全部 PASS）

| 组 | 条数 | 内容 |
| --- | --- | --- |
| 颜色契约 | 9 | 背景、INFO/WARNING/ERROR/CRITICAL 正文、时间戳灰、徽章色、警告行展开前可辨 |
| 等级解析 | 3 | `plugin_fault` 无 severity → ERROR；`data.log.level` 优先；error 包不能降级 |
| 文本安全 | 3 | 标签当文本、注入 handler 未执行、ANSI 与控制字符被剥离 |
| 去重 | 1 | 同 id 去重 / 同文不同 id 保留 |
| 筛选 | 4 | 等级筛选、字面搜索、方括号不当正则、来源筛选 |
| 滚动 | 4 | 暂停保位、未读计数、恢复滚底、未读清零 |
| 清空 | 2 | 清空视图、不重放历史 |
| 缺口/重置 | 2 | 缺口可见且说明、重置可区分 |
| 详情 | 2 | 点击展开、超长截断并标注 |
| 容量 | 2 | 10k 事件 ≤1000 行、最新事件存活 |
| 导出 | 3 | 含可见行、含等级与 code 列、无标签 |
| 主题 | 2 | 深色主题下背景与 INFO 色不变 |
| 生命周期 | 2 | 重复挂载不叠监听、destroy→mount 从干净状态开始 |

### 截图

| 文件 | 内容 |
| --- | --- |
| `ui/logs-terminal.png` | 整页终端（黑底，INFO/WARNING/ERROR 同屏） |
| `ui/logs-levels.png` | 四个等级同屏近景（INFO / WARNING / ERROR / CRITICAL） |

截图内不含任何凭据或真实主机名。

## 7. 未执行项（NOT_RUN）

| 项 | 原因 |
| --- | --- |
| 真实 SSE 断线/重连/跨实例/过期游标 | 依赖 B03 后端日志流，本机无运行中的 AstrBotEX 服务 |
| 慢消费者溢出真实场景 | 同上；本次用 `control()` 注入 gap/reset 帧模拟 |
| UTF-8 被拆 chunk 的流式解码 | 属 B03/B08 传输层，前端只消费已解析事件 |
| 与 `app.js` 的最终挂载联调 | 属 B08 集成；本组件已按 C01 §7 暴露完整 API |

以上不计入通过数。

## 8. 集成方需要注意

1. `logs.css` 必须在 `styles.css` **之后**引入，否则主题变量会覆盖终端配色。
2. 挂载点为 `#page-logs`，容器 id 由 B08 在 `index.html` 中提供；组件不会自建页面。
3. `EXLogs` 不做重连、不做鉴权、不读 `EventSource`——这些是 B08 的职责，组件只接受已解析事件。
