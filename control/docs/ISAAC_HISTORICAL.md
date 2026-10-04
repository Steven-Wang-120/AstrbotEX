# AstrEX Isaac 历史验证（合并记录）

STATUS: HISTORICAL
Current baseline: [docs/ISAAC_51_DEV_BASELINE.md](ISAAC_51_DEV_BASELINE.md)
Historical raw evidence was archived to:
`/data/shared/AstrEX_project_data/archives/isaac/2026-09-baseline-consolidation/`
See `MANIFEST.json` for original-path mapping (includes paths kept in place and paths deleted).

本文件是四份早期独立报告的合并记录，只保留结论、限制与证据位置；四份报告全文归档在该归档目录的
`historical_reports/`。逐字原文与当时的判定均未被改写。

## 1. Isaac 5.1 / Isaac Lab 2.3.2 基线验收（2026-09-17）

- **结论**：本机 `isaaclab232_test`（Isaac Sim `5.1.0.0`、Isaac Lab `v2.3.2`
  `37ddf626871758333d6ed89cf64ad702aef127d0`、Python 3.11、Torch `2.7.0+cu128`、系统 ROS 2 Jazzy
  domain 63）达到"本机已测"门槛：官方 `/clock`、`/joint_states`、`/joint_command` →
  Subscriber → ArticulationController → CPU PhysX 闭环、RL（20/100 updates、checkpoint reload、
  600-step evaluation）、10 分钟通信与 GUI 人工验收均 PASS。
- **限制**：默认 headless `isaaclab.python.headless.kit` 的建图失败根因**未确认**（同机同包改用完整
  `isaaclab.python.kit` 即可建图，因此只把完整 experience + OnPhysicsStep + OnDemand + CPU PhysX
  记为已验证 workaround，不当作根因修复）；GPU PhysX + ROS Bridge 未通过；十分钟 RSS 约 2.9 MB
  单调增长未解释；四项 pip metadata 冲突保留不修；Fabric cloner warning 保留；Isaac Sim 5.1
  已停止维护。自定义 ROS 接口、多环境 GPU ROS 控制、长期维护均未验证。
- **证据**：全文 `historical_reports/ISAAC_51_BASELINE_FIX_RESULT.md`；原始 run 证据（报告里的 `E/`）
  已归档到 `isaac51_fix_evidence/`（逐文件 SHA256 见其 `CHECKSUMS.sha256`），可复用脚本迁到
  `/home/sssxy/Projects/isaac51-baseline-evidence/`；两份 10 分钟稳定性运行的逐条消息 dump 未归档。

## 2. Isaac 5.1 / Isaac Lab 2.3.2 隔离验证（当时的判定）

- **当时结论**：物理与标准消息 DDS 双向通信 PASS；官方 ROS 2 OmniGraph 发布图在默认 headless
  路径无法创建 → 判定"**不推荐现在替换 AstrEX 的现有开发基线**"，仅保留为对照环境。
- **与当前的关系**：该判定已被第 1 节的 workaround 与后续 baseline cutover 取代（完整 experience +
  CPU PhysX + OnDemand 后闭环通过）。本文件不改写当时的结论。
- **证据**：全文 `historical_reports/ISAAC_51_LAB232_VALIDATION.md`；原始证据
  `isaac51_validation_c30sx014/`（已归档到归档根下同名目录）。

## 3. 环境修复与包保护记录

- **内容**：两个 Isaac 6 环境的安装/修复过程、修复前后的 `pip freeze` SHA256、受保护官方文件哈希，
  以及"不修改正式 ROS workspace、Conda 原环境、系统 ROS、驱动或 kernel"的逐项核对记录。
- **与当前基线的差异**：当时保留的两个 Isaac 6 环境已随 2026-09 基线固化删除；删除前的纯 package
  snapshot 与版本表见 [`docs/env/legacy/`](env/legacy/README.md)。
- **证据**：全文 `historical_reports/ISAAC_ENV_FIX_RESULT.md`。

## 4. Isaac 6 PhysX joint-effort A/B

- **结论**：`isaaclab60`（Sim 6.0.0.1 + Lab v3.0.0-beta2.patch1）与 `isaaclab60_6010test`
  （Sim 6.0.1.0 + 同一 Lab）在同条件 effort 测试下**均 FAIL**；该结果没有证明 Sim 版本是原因，
  不作为版本选择依据。两个环境已删除，快照见 [`docs/env/legacy/`](env/legacy/README.md)。
- **证据**：全文 `historical_reports/ISAAC_PHYSX_AB_RESULT.md`；原始 A/B 数据
  `physx_ab_JPThAicl/`（已归档）。

## 归档与保留位置一览

| 内容 | 位置 |
|---|---|
| 归档根（含 `historical_reports/`、`physx_ab_JPThAicl/`、`isaac51_validation_c30sx014/`、`early_exit_runs/`、`isaac6010_validation_summary/`） | `/data/shared/AstrEX_project_data/archives/isaac/2026-09-baseline-consolidation/` |
| 归档的历史原始证据（报告里的 `E/`） | `/data/shared/AstrEX_project_data/archives/isaac/2026-09-baseline-consolidation/isaac51_fix_evidence/` |
| 可复用的历史脚本 | `/home/sssxy/Projects/isaac51-baseline-evidence/` |
| 当前基线与该轮 cutover 证据 | [docs/ISAAC_51_DEV_BASELINE.md](ISAAC_51_DEV_BASELINE.md)、`logs/isaac/cutover_20260917T103433Z_6f2a91/` |
| 当前/历史环境快照 | `docs/env/current/`（本机历史引用：`env/current/`）、[`docs/env/legacy/`](env/legacy/README.md) |
