# 2026-11-03 上位控制修复与复验

已修复原验收发现的代码阻断，并通过修正后的 B11 `--scope all`：7 阶段全部 PASS，34 条命令、692 次测试执行，0 失败、0 跳过。另有页面替身 13 项、未修改的独立复现 5 项通过，共 710 次执行；其中包含装配测试在 unit/host 阶段重复执行，不是 710 个独立用例。

这是上位接线范围的通过结论。**尚不将其作为 A.md 全阶段正式合并批准**：完整子进程强杀/恢复窗口、所有部署身份和真实平台/TTS 出口组合仍未独立验收。实际合并与生产部署均未执行。

| 仓库 | 精确测试源码提交 | 候选工作树 |
| --- | --- | --- |
| A.E.B | `274e1df404437b986c0906852e545a9d5d4399d3` | `D:/Code/A.E.B-work/upper-b10-b11` |
| AstrBotEX | `ebd4947abca6d68e0880aa3b9a2922a542f1fb28` | `D:/Code/AstrBotEX-work/upper-b10-b11` |

A.E.B 的 `59c9369` 包含原先尚未提交的 B10/B11 装配、跨模块修复和本轮修复；`274e1df` 进一步修复页面初次读取时序。EX 的 `ebd4947` 统一实际 ingress parser。后续证据提交仅增加 `port_artifacts`，不改变经过测试的源码。源码实际字节 SHA-256 见 `final-source-manifest.json`。

已经处理的缺陷：

1. 两个功能开关关闭、机器人/peer 未配置、EX 离线时，管理仓储与 10 个 API 仍可用。关闭执行接管保留配置能力。
2. Provider ID、超时和 origin 按每次输入的配置快照冻结，保存后下一输入生效；进行中的生成保持原快照。开关保存后影响下一输入，关闭时也抑制迟到输出。
3. EX 语音的公开回复保留原 text peer，执行下发只使用配置绑定的 control peer；两个身份可以不同。
4. EX 实际接收层复用 B00 严格 parser；合法后缀序号被接受，重复段 ID 和路径式 ID 被拒绝。20 个共享样例字节一致、接受结果差异为 0。
5. 必须页面阶段参与总体退出状态。`all/host` 要求 real_host，缺 URL/浏览器/依赖或页面失败都不能判绿；Windows 默认解释器路径保留完整反斜杠和空格。
6. 真实页面额外暴露的管理接线已修复：API 读取 `SettingsSnapshot.settings`；真实目录快照通过适配器转成记录列表；新增描述生成稳定 action_ref，编辑保持身份和修订号。
7. 初次异步读取未完成时锁定编辑区，避免迟到读取覆盖新草稿，新增可控延迟读取回归。
8. 验收脚本源码只读挂载，Host 导入数据写临时目录，镜像明确 `--pull never`；Git 探测失败不再伪装 available；修正失败/错误计数、Host 版本探测、页面证据目录及旧基线读取。部署文档和交付说明已更新。

| B11 阶段 | 最终结果 | 测试执行数 |
| --- | --- | --- |
| unit_aeb | PASS | 285 |
| unit_ex | PASS | 96 |
| loopback_aeb | PASS | 19 |
| loopback_ex | PASS | 12 |
| host | PASS | 87 |
| page_spec | PASS | 1 |
| legacy_regression | PASS | 192 |

真实 Host 页面验证使用实际 AstrBot 4.26.7 Dashboard、Chromium、受限 iframe、原生 bridge 和真实管理 API/仓储。验证关闭功能时的目录保存刷新、设置与提示词持久化、未登录读写拒绝、两个页面的修订冲突，以及注入 503/网络失败后保留草稿。成功请求走实际后端；503/网络故障由浏览器拦截注入。

双端真实实现串联的独立检查保持通过：AEB pipeline/ROUTER → EX ConnectionManager/DEALER/ingress/service/JevBackend 请求构造，3 段选前 2 段，模型请求一次、原文顺序一致，第 3 段保持 pending。本地后端响应为记录替身。

合并树检查：相对原主线基准 A.E.B `b8b448e`、EX `c1a4b89` 均无冲突；使用 `git merge-tree --write-tree`，未移动任何分支或修改主工作区。主工作区 HEAD 保持原值。

环境与边界：Windows 11/Python 3.12.10；Python Host 测试运行于本机已有隔离镜像。真实页面运行于全新数据的临时 Host，Host 内部网络禁止外网，浏览器只经本机代理访问；没有调用付费模型、硬件、生产平台或真实执行。生产容器未修改。原有缓存、用户 data 和历史失败证据均保留。临时测试账号凭据没有放进仓库证据。

可复核文件：

- `b11-final-003/results.json`、`commands.log`、`environment.json`、`logs/`：最终完整矩阵，verdict=wiring_pass，退出 0。
- `b11-final-003/page/real-host.png` 与 `upper_control_evidence.json`：实际 Dashboard 页面证据。
- `fake-ui-final.log`：13 项页面替身测试，含受控延迟读取。
- `final-repros.log`、`final-repros/review-repros-results.json`：原独立复现 5 项通过。
- `fixture-parity-results.json`：20 样例、0 差异。
- `final-source-manifest.json`、`repair-results.json`：提交与字节指纹。
- `aeb-merge-check.log`、`ex-merge-check.log`：无冲突合并树输出。

新证据归档于双方候选 `port_artifacts/upper/B11/repair-20261103/`。A.E.B 的 B00–B03/B10 另有同名补充交付记录，明确标记为本次集成复验，不伪造原任务的历史运行。旧的失败/部分通过记录没有覆盖。

仍需补齐 A.md 全套独立故障恢复及部署/公开出口矩阵，才能给整包最终合并批准。`received/injected` 仅证明投递或推理输入，不能据此声明真实动作完成。
