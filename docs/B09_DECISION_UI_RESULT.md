# B09 页面：安全展示与验证入口

页面：[index.html](../dashboard/index.html)、[decision.js](../dashboard/decision.js)、[app.js](../dashboard/app.js)、[styles.css](../dashboard/styles.css)。入口 `http://127.0.0.1:8765/#/decision`，复用管理 HTTP、内存凭据、认证 fetch/SSE，不新增服务器/执行器/登录框架。数据合同见 [管理说明](B08-DECISION-MANAGEMENT.md)。

## 展示与草稿边界

普通页面只有 Jev/Laya 卡片、连接配置弹窗、测试、EX 启停、当前任务与短状态；Mock 只作诊断/测试。不展示 raw JSON、质量巨表、未来完整计划、tool trace 或没有 Host 归属的取消按钮。

- saved/effective/running 分离。测试固定 wait-only 推理，成功不代表已保存、runtime 启动或执行授权；Laya health200/inference401 是供应商认证失败，不清管理凭据。
- 草稿独立于轮询；配置/session 变化和 409 保留输入并要求显式核对，不能自动重放保存。关闭弹窗、离页、换 provider 等会丢草稿的操作必须先处理草稿；拒绝丢弃保持原页面/焦点。
- 202 operation 查询终态；404/未知/failed/blocked/superseded 如实显示。供应商测试成功不是模型质量，停止请求不是停止证明。
- 主页面与脏配置弹窗都有停止入口，停止不受草稿、失败配置读取或其他非停止操作阻塞；Start 依赖当前 can_start 与真实状态，不由连接测试推断。
- session/credential/request 代次 fencing，重启/换凭据后旧响应不生效；隐藏/离页暂停读，刷新和网络恢复只读，不自动 start/save/execute，同类 GET 不重叠。
- 管理凭据仅内存，刷新清除，不入 URL/storage/日志/导出。外部文本安全渲染；缺数据、未识别 phase 或过期投影显示暂不可确认，不补造成功。

当前任务来自 Host routes/peer/robot/session 限定的只读 projection，不从旧 Goal 或百分比推算。task.current_goal 是当前步骤摘要，completed/total 是 HostTask 计划进度；不展示完整计划和用户身份。管理 Bearer 不授本人取消权，can_cancel=false。runtime running 与无 Goal 的零动作可以同时成立；框架停止、模型进程退出、Actor 状态与物理成功是不同事实。

## 两类浏览器检查

[verify_decision_ui.mjs](../scripts/verify_decision_ui.mjs) 区分：

- `--mode fixture`：冻结管理合同替身，检查布局、草稿、恶意文本、乱序/拒绝等页面边界，不能证明真实后端激活。
- `--mode actual`：`tests/decision_ui_real_fixture.py` 用实际 build_server、管理 HTTP 与生产 builtin transport；供应商是 loopback 合成 HTTP，临时 SQLite/凭据，不调用付费模型、owned 进程或物理插件。该模式的完整检查需要通过 CLI 参数 `--aeb-root` 显式指定已核对的 A.E.B companion；两项投影检查由该参数启用，仅预设 `ASTRBOTEX_AEB_TEST_ROOT` 不能替代该参数，不能把省略参数或缺 companion 当成受支持的完整 actual 检查。实际 A.E.B handler/TaskStore 经 ROUTER/DEALER 和嵌套 capabilities 查询提供投影，不以字典 stub 冒充跨端。

从仓库根，显式指定已安装的 Node/Chromium/Python；所选 Python 必须能导入实际 Host SDK 与 pyzmq，不能仅安装 EX 依赖。使用新空外部 output：

```sh
node --check dashboard/decision.js
node --check scripts/verify_decision_ui.mjs
node scripts/verify_decision_ui.mjs --mode fixture --python /absolute/ex-python --chrome /absolute/chromium --output /absolute/external-evidence/browser-fixture
node scripts/verify_decision_ui.mjs --mode actual --python /absolute/ex-python --chrome /absolute/chromium --aeb-root /absolute/aeb-checkout --output /absolute/external-evidence/browser-actual
```

实际检查应观察匿名读写401、draft probe 无保存/CAS/Goal/Action 副作用、供应商 config/key 保存、runtime decision/execute 激活、Stop、业务409与草稿、health/inference 区分、管理凭据刷新清除、投影范围/断线不可用。不得仅凭页面缓存或按钮文案推断后台状态；源码身份和真实状态采样应随输出保存。替身和 actual 模式的结果不互换，不构成模型语义/GPU/ROS/机器人验收。

## 验证限制

临时测试资源只能清理由本次运行明确创建并核对的范围，不能递归清理用户数据。Windows 使用明确解释器路径；进程/整树退出、ACL 与模型环境须单独核验。旧 WebSocket token 风险不因新页面凭据仅内存而消失。API/UI 的联合验证边界集中于 [管理验证章节](B08-DECISION-MANAGEMENT.md#7-具名验证入口与限制)，日志/截图/结果只保存在外部 evidence 或临时目录。
