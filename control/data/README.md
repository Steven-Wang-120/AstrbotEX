# T1 冻结数据

唯一有效版本为 `t1_corpus_v2/`。三个文件从原实验目录逐字节复制。100 组按独立任务/场景划分 train/validation/test 为 70/15/15，230 行共享组内划分，未训练。禁止将旧 v1 数据或开发探针作为独立测试集。

- `groups.jsonl`：原任务、场景、分组、标签。
- `laya-choice.jsonl`：官方 choice 输入与标签，附加元数据不得加入模型输入。
- `manifest.json`：原冻结摘要、审阅 hash 和隔离结果。

生成代码位于根目录 `scripts/build_mobile_language_corpus.py`，仅向新的输出目录导出。人工审阅原文为 `control/docs/MVP_LANGUAGE_LABEL_REVIEW.md`。本次发布只校验，不重新冻结，也不实施 T2 微调。
