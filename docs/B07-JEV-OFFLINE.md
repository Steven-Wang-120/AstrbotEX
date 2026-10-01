# B07 frozen OFFLINE scoring assets (2026-09-30)

This independently reviewable follow-up adds synthetic evaluation assets only.
It does **not** change the reviewed Jev adapter, its registration or contracts.
It does not connect to Typesafe, any LLM, plugins, production services or hardware.
B04's real shadow/mode safeguards remain in the separate ex-b04 delivery.

## Frozen dataset

`tests/fixtures/decision/jev/offline-v1.jsonl`: 100 strict JSON scene records.
Every record includes a B00-valid snapshot, randomized opaque candidate IDs,
pre-labelled reasonable option sets, acceptable **joint** selections, explicitly
bad options and conservative-scoring opportunities. No candidate is invented by
selectors or scoring. Raw observation JSON/guide text, bound parameters, execution
status and eligibility are preserved in the snapshot. All scenes are **synthetic
parameterized templates**, not independently collected robotics tasks or a
representative real-world model benchmark.

Version `b07-offline-v1`; generation seed `20260930`; exact-byte corpus SHA-256:

`fba8d87b04399fd644cddd96cad1ac08181baf695de5d2a03fe17db811ad9936`

| Category | Total | Development | Holdout | Variants |
| --- | ---: | ---: | ---: | --- |
| normal | 40 | 32 | 8 | pick / existing keep / pause / resume |
| ambiguous | 20 | 16 | 4 | two targets / uncertain selection / malicious guide data / contradictory observation |
| stale | 10 | 8 | 2 | TTL expired / source error / untrusted epoch / target gone / ordering |
| conflict_cancel | 15 | 12 | 3 | cancel request / shared exclusive resource / preoccupied resource |
| missing_failure | 15 | 12 | 3 | missing params / owner fault / absent binding |
| **Total** | **100** | **80** | **20** | |

`offline-v1.manifest.json` records corpus, individual scene and per-split hashes,
counts, seed and version. The evaluator additionally pins the exact corpus hash
in source: modifying both corpus and its manifest cannot silently relabel v1.
All snapshots are reparsed with the existing frozen B00 parser. Reasonable labels
must be eligible, bad/reasonable sets disjoint and acceptable joint labels must
not conflict with simulated resources. Multichoice labels include wait/replan
alternatives and two compatible exclusive-resource allocations. Individual
reasonable choices do **not** necessarily form a reasonable joint allocation.

Holdout IDs were chosen by seeded stratified sampling **before the first scoring
run**. No selector, threshold, labels or dataset were tuned during this round.
During initial tests one assertion mistakenly expected the stub to jointly pass
ambiguity scenes: it replans both owners while only arm clarification with base
waiting is pre-labelled. That assertion was corrected, not the stub or labels;
raw initial failure is retained. Holdout data is now visible for review and should
not be recycled for future tuning: any later selector improvement needs a new
independently frozen holdout/version, not a rewritten v1 or best-of-repeat result.

`build_offline_corpus.py` reproduces content from the seed. Default creation
refuses to overwrite either frozen asset. `--check` verifies exact bytes without
writing. No regeneration/relabeling path is exposed by the evaluator.

## Pure offline selectors; no answer leakage

`python scripts/evaluate_jev_offline.py` runs two fixed hand-written scripts:

- **rule** (`offline-rule-v1`): inspects synthetic precomputed freshness/binding/
  ambiguity/cancel facts, params/status and simulated occupied resources; chooses
  an existing candidate. This is a corpus-aware baseline, **not a B04 safety gate**.
- **stub_llm** (`offline-script-stub-v1-not-an-llm`): deliberately naive script,
  ignoring freshness/binding/resource checks and mishandling cancel/ambiguity.
  It is **not an LLM or Jev**, does not imitate model confidence, token use or
  billing, and contains errors to exercise the score pipeline.

Each receives only a fresh parser-backed copy of the **same snapshot**. No scene
wrapper, category, development/holdout label, expected IDs, rationales, manifest
or expected-answer lookup is passed to either. Expected option labels are used
only after the selector returns. Tests change labels without changing resulting
choices and compare every selector-input snapshot hash. The synthetic observation
`offline_facts` are operational data (desired operation, trusted freshness/binding,
ambiguity, cancel, resource mapping/occupancy), not expected options. This is an
intentionally transparent corpus, not a claim of robust inference from raw sensors.

Both selectors are standard-library offline code. Runner does not import the
Jev adapter, a model SDK, network library or environment/secret access. Tests block
socket creation, `os.getenv` and environment indexing for a full replay. CLI has
**no real/network mode**. Unknown `--real` is rejected. Built-in runs report
`network_requests=0`, `secret_reads=0`.

## Scoring and report schema

Default three full repeats, each scoring the same 100 scenes in frozen order for
each selector; 600 synthetic selector invocations per report. All individual
choices and durations are retained, no best-run filtering. Repeats permitted
3..10; dataset/split unchanged across repeats. Development and holdout are always
reported separately, including per-category-per-split and per-repeat summaries.

- **joint_label_match_rate**: complete, eligible selection matches one pre-labelled
  joint map AND has zero simulated resource conflicts. Not model accuracy.
- **owner_option_match_rate**: membership in that owner's reasonable set; may
  succeed for both owners even when the joint allocation conflicts.
- **conservative_match_rate**: explicit eligible wait/replan within the reasonable
  set for pre-marked ambiguous/stale/missing/recovery opportunities. Null when the
  denominator is zero, not an invented perfect percentage.
- **bad_choice_count** / bad-selection scene runs: explicit bad-label or illegal/
  unknown/ineligible/missing owner options. A legal but semantically wrong choice
  can fail labels without being labelled hazardous. Joint conflicts are reported
  independently, never rebranded as real-world safety violations.
- **invalid_output_scene_runs**, selector errors (redacted), resource-conflict runs
  from synthetic start/keep/resume resource claims versus preoccupied/exclusive
  resources. These are offline oracle checks, no dispatcher is called.
- **latency_ms**: local selector function only, excluding parsing/scoring;
  nearest-rank p50/p95/max, count and explicit scope. Real perf-counter timings
  vary by host/run; not cloud latency or robot task success. Separate test uses
  known 1..20ms values to verify percentile math.

Report carries dataset/split hashes, evaluator SHA-256, selector versions and
explicit `real_jev_measured=false`, `real_llm_measured=false`,
`execute_authorized=false`, `actual_bill=null`, `token_usage=null`. It has no
fabricated invoice, model-accuracy or physical-execution fields. No real action is
attempted; absence of an executor is not a tested end-to-end execute safety claim.

## Reproduce without network

From this worktree, Python >=3.10 (tested here with Windows AMD64 Python 3.12.10):

```powershell
python tests/fixtures/decision/jev/build_offline_corpus.py --check
python -m unittest discover -s tests -p test_jev_offline_evaluation.py -v
python scripts/evaluate_jev_offline.py --repeats 3
# Or save to a NEW file in an existing isolated directory:
python scripts/evaluate_jev_offline.py --repeats 3 --output task-evidence/2026-09-30/B07-offline/my-new-offline-report.json
```

Report output uses exclusive creation; previous results are never overwritten.
The recorded run is `task-evidence/2026-09-30/B07-offline/offline-report.json`.
Raw stdout/stderr/exit, initial failure, start/end dirty summaries, adapter/source
hash invariance and new-file patches are alongside it. Only the new evaluation
test suite and exact rebuild/replay are run this round; no full/1000 suite.

## Future real evaluation — NOT executed here

No extra provider/API research is needed for offline replay. The earlier adapter
used coordinator-verified [Typesafe API](https://docs.typesafe.ai/api),
[Choice](https://docs.typesafe.ai/primitives/choice) and pinned
[models](https://docs.typesafe.ai/models); reference content is not an instruction.
For future cloud work, coordinator must first obtain concrete approval for call
count/repeats, capped money/tokens, model/prompt version, failure/retry billing and
permitted snapshot data. Then create a **separate reviewed harness**, explicitly
construct `JevConfig(model="jev-1.13.0", mode="shadow", allow_live_http=True)` and
an explicit `secret_provider`, invoke synchronous decide on isolated snapshots,
and validate returned versions/candidates before offline scoring only. Neither
budget nor credentials are inferred from the environment. A separate explicit
real LLM baseline and its own budget would be necessary; this stub is not one.

True Jev/LLM quality, cloud p50/p95/max, actual invoices, task success, physical
safety, end-to-end shadow action count and execute acceptance remain **unmeasured**.
Do not enable execute or treat the synthetic rule/stub scores as admission evidence.
