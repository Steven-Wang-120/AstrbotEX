# B07 Jev backend — isolated shadow delivery (2026-09-30)

## Delivered / not accepted

A synchronous, standard-library HTTP v1 adapter and static trusted factory are
available. No SDK/dependency change, plugin discovery, hardware control, service
configuration, real API call or paid evaluation was performed. Defaults are
`mode="disabled"`, `allow_live_http=False`, pinned `jev-1.13.0`; only `disabled`
and `shadow` are accepted by this adapter. This is **not acceptance of Jev
quality, billed cost, executable mode, or complete B04 integration**.

The worker used the official protocol verification supplied by the coordinator
on 2026-09-30, not a fresh independent web fetch:

- [Models](https://docs.typesafe.ai/models): explicit pinned model; no latest alias.
- [API](https://docs.typesafe.ai/api) and
  [Choice](https://docs.typesafe.ai/primitives/choice): POST
  `https://api.typesafe.ai/v1/systemone`, Bearer auth, model/state/questions;
  per-answer type/choice/confidence/probabilities and token usage.
- [Model limits](https://docs.typesafe.ai/model-jaggedness/jev-1.13): no numeric,
  TTL, coordinate or safety authority delegated to the model.

## B04 interface / integration points

```python
from astrbot_ex.core.decision.backends.registry import create_backend
from astrbot_ex.core.decision.backends.jev import JevConfig

backend = create_backend("jev", config=JevConfig())  # disabled, no secrets read
# Explicit offline test transport + explicit fake secret provider can evaluate
# JevConfig(mode="shadow"). Never dispatch a shadow result.
# result = backend.decide(snapshot)  # core.decision.models.BackendDecision
backend.close()
```

No import of B04's pending `base.py` or `mock.py`; duck-typed
`decide(DecisionSnapshot) -> BackendDecision` and idempotent `close()`.
`cancel()` drops an in-flight request. `reconfigure(JevConfig)` invalidates the
current epoch; a pinned-model, guide or threshold change cannot accept the old
response. Config is immutable and exposed read-only. B04 must atomically update
its **config_revision**, cancel/reconfigure the backend, and validate every
trusted version immediately before arbitration/admission. Backend-local epoch
is not a substitute for B04's version authority.

In this B04 composition tree the immutable static registry contains `mock` and `jev` only. No dynamic plugin/config import paths are accepted. Jev exposes read-only `execution_allowed=False`, independent of disabled/shadow config; `DecisionService.set_mode('execute')` rejects it and final admission checks it again. Mock inherits trusted `execution_allowed=True` from the base interface. Backend instance construction/injection is trusted framework code, not a wire field or vendor response. Unknown duck backends lacking the capability cannot execute; existing injected Mock subclasses remain compatible.

B04 `reconfigure_backend(JevConfig)` increments EX config_revision and revokes authorization before updating Jev's own epoch under the service state lock. Even a result that already returned from Jev is rejected by EX if goal/config/catalog/environment/session/gate/plugin versions changed. Do not call backend.reconfigure directly in B05/B08; no public transport/config endpoint is added here.

B04 owns async scheduling, latest-snapshot coalescing, trusted freshness,
resource arbitration, goal replacement, stop, lease and execution gates. This
adapter holds no dispatcher, publisher, lease manager or plugin reference and
returns/records selection data only. Cloud timeout never renews a lease.
Returned snapshot/version fields come from this request's private copy. A goal
change outside the adapter still requires B04's cancel + version rejection.

## Request and selection contract

- Reparse a private copy of the mutable B00 model; do not mutate caller data.
  Caller is responsible for producing a consistent snapshot (concurrent edits
  during its initial copy are not an atomic snapshot API).
- State includes the complete normalized snapshot: original observation JSON,
  goal English text, bound params, owner execution status, candidates and versions.
  `decision_id` uses the existing `snapshot_id` because B00 exposes no independent
  decision ID. B04 can associate its own internal ID without changing the schema.
- B00 has `description_hash` but no observation guide text field. Until B05/B04
  supplies a richer projection, explicit immutable `observation_guides` tuples
  `(source_id, description)` are provided through JevConfig and included in state.
  Their content is data, never interpolated into trusted instructions. Changing
  guides is a configuration revision, not a silently updated prompt.
- One Choice question per owner. Question key maps to this request's owner;
  criteria contain existing **eligible** option IDs and descriptions. The full
  state also retains ineligible candidates as explanatory data. Option IDs and
  descriptions are visible to the vendor. Owner/question IDs are not instructions
  or transport routing destinations. Multiple independent answers confer no
  resource compatibility.
- Per-owner limit counts **all candidates**, including wait/keep/cancel/replan
  and ineligible entries. Default 255, maximum 255. Default owner limit 32,
  request/response limits 262144 bytes. No truncation or choosing first on failure.
  No owners, no eligible candidate, no legal conservative candidate, input budget
  overflow or invalid snapshot rejects before HTTP.
- Response must have exact model/answers/usage fields, exact owner set, exact
  answer keys, known eligible option, finite non-boolean confidence and complete
  per-owner eligible distribution (exact keys, finite non-boolean values 0..1,
  sum tolerance `1e-9` absolute). Choice must be an argmax; an exact tie may choose
  either tied candidate. Duplicate JSON keys, NaN/Infinity, malformed UTF-8,
  unknown fields/options, missing question or invented params reject the batch.
- Default confidence threshold 0.6 is **unevaluated configuration**, not a safety
  probability or quality guarantee. Below threshold, choose an explicit eligible
  wait (lexicographic ID tie-break), otherwise explicit request_replan; never
  invent one. Conservative choices omit vendor scores rather than misrepresent
  the original argmax distribution. Count overrides in `last_record`.

## HTTP / authentication / lifecycle boundaries

- Network is enabled only with explicit `mode="shadow"` and
  `allow_live_http=True`. This construction alone is not a budget authorization;
  actual cloud evaluation still requires later concrete user budget approval.
- Credentials use either explicitly named `secret_env` or explicit
  `secret_provider()`. No conventional key name is read, no SDK key discovery,
  no automatic existing-key reuse. Neither config nor record stores a secret.
  The fake credentials in tests are constructed test strings.
- Fixed HTTPS host/path, TLS verification supplied by Python stdlib. No endpoint
  override, proxy auto-discovery or redirect following. A 3xx is rejected, never
  forwarded with authentication to another host. No HTTP error body, original
  auth header, secret-provider exception or arbitrary transport exception is
  incorporated into public diagnostics.
- One total monotonic deadline begins before input construction, includes secret
  resolution, network headers/body, retries, decoding and final validation.
  Socket remaining timeout + bounded `read1` are supplemented by an outer daemon
  worker/result boundary: even a drip-fed header/body, blocked DNS/provider or
  noncooperative injected transport cannot return a late decision.
- Python does **not** forcibly kill arbitrary blocked DNS/callables. A timed-out
  worker is canceled and quarantined; at most one worker per instance, and new
  decide returns `busy` until it exits. close/cancel return promptly. A cloud
  request already sent may still run/be billed; cancellation is not refund or
  server-side termination. Short bounded JSON work may finish past the nominal
  deadline but is rejected before return. This is a bounded decision-wait
  contract, not OS hard real-time or guaranteed remote abort.
- Default max two starts/sec (`min_interval_ms=500`) and no retries.
  Optional explicit `max_retries=1..2` only retries 429. Numeric/date Retry-After
  must fit the **remaining original budget**, and waits are interruptible. No
  retry on 401/422/529, transport failure or changed goal/config.
- `last_record` is an immutable latest-only record: pinned/validated model,
  request SHA-256, latency, rejection code, attempts, usage and overrides. No raw
  state or credentials. No cost estimate is fabricated: usage is reported but
  actual token rates, invoices and billed failed-call usage remain unevaluated.
  B04/B08 can persist this record under their own bounded policy.

## Local checks and evidence

`task-evidence/2026-09-30/B07-backend/` keeps starting dirty status, tracked diff
summary, scoped diff, frozen hashes and raw test stdout/stderr/exit. The scoped
tracked diff is initially empty because these new backend files are untracked;
final scoped new-file patches are captured separately without staging.

Initial round: 24 Jev tests, 4 backend-contract tests, 9 existing frozen snapshot
contract tests; all exit 0, no skips. Final round: 27 Jev tests, 4 backend-contract
and 9 frozen snapshot tests (40 total, no skips), including concurrent busy/retry
cancel, retry-attempt rate limit and blocked secret-provider deadline tests.
Loopback HTTP tests exercise
stdlib request bytes, real reader behavior for slow header/body and Content-Length
or streaming overflow, plus refusal to follow an unknown-host redirect.

Host: Windows x86-64, Python 3.12.10. Uses Python 3.10-compatible syntax/stdlib;
3.10 grammar compilation is checked separately. **No actual Python 3.10 or ARM64
runtime was available/tested.** Only focused suites were run; no full-batch or
hardware/production result is implied.

## 2026-10-01 acceptance follow-up (c9624a4 baseline)

The required official pages were freshly fetched with Tavily on 2026-10-01:
[Models](https://docs.typesafe.ai/models), [Choice](https://docs.typesafe.ai/primitives/choice),
[State](https://docs.typesafe.ai/concepts/state), [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13),
and [HTTP API](https://docs.typesafe.ai/api). They still document `jev-1.13.0`,
`POST /v1/systemone`, text/JSON state, <=255 options and independent questions.
The published 64k total/32k state+longest-question limits are token limits, not our
byte budget; local byte limits do not guarantee the provider tokenizer fits.
Supplier 429/529 backoff advice does not override this adapter's opt-in bounded
429-only retry policy. No SDK was installed or imported; core remains Python 3.10
stdlib compatible. Later *explicit* pinned versions can be tested by trusted
reconfiguration (never a latest alias), but delivery defaults remain 1.13.0.

Two necessary adapter fixes, no service/registry rewrite:

- Reject premature Content-Length EOF (`incomplete_response`) even if the partial
  body happens to be valid complete JSON. A loopback failing test precedes the fix;
  chunked/connection-close responses still use stdlib framing. Invalid framing
  remains fail-closed, never a first-option fallback or automatic retry.
- When cancellation wins a worker-join/retry-wait race, attribute rejection to
  current close/config epoch before generic canceled. This does not authorize
  results or change cancellation semantics; it stabilizes diagnostics.

Additional tests cover inclusive byte boundaries, secret/transport invalid shapes,
pre-secret input rejection, exact retry exhaustion, request-build/response-parse
budget, guide changes after parsing, retry-wait configuration changes, and real
ActionService/SQLite/Actor shadow keep/wait/replan/malformed output effects.
Existing start/cancel/late-goal/config-change composition tests are rerun unchanged;
B04 race tests and B00 files are not edited. Real model injection resistance is
still **unmeasured**; these tests prove data separation and fail-closed local gates.

Historical final acceptance was 69 tests, including original 50; that count and
the archived evidence below are unchanged. After the repository split,
`tests.test_jev_offline_evaluation` is
`validation_tests.evaluation.test_jev_offline_evaluation` in
[AstrBotVLA-tests](https://github.com/Steven-Wang-120/AstrBotVLA-tests). Reproduce
the strict combined offline suite from that repository (including retained EX
units, migrated evaluation and wiring):

```powershell
python -B run_validation.py ex-offline --ex-checkout ../ex --aeb-checkout ../aeb
```

Use its README's pinned-lock/development instructions. The offline driver is
`python -B -m validation_drivers.evaluate_jev_offline --ex-checkout ../ex --aeb-checkout ../aeb`.

This is worker verification, not coordinator acceptance. Platform results, source
SHA before/after, failures, skips and exact commands are recorded in
`task-evidence/2026-10-01-complete/HANDOFF.md` and raw logs. Windows available
runtime is 3.12.10, not 3.10; ARM is checked separately with actual 3.10.12.
No grammar-only check is presented as Windows 3.10 execution.

## Deferred intentionally

No frozen 100-scene/holdout replay benchmark or rule/stub-LLM comparison was built
this round, prioritizing the strict usable adapter over optional evaluation.
Real Jev correctness, repeatability, task success, p50/p95/max cloud latency,
actual invoice, LLM-only vs Jev comparison and execute-mode gates remain future
work under explicit budget and integration review. Input-injection tests prove
instructions/data separation and response allowlisting, **not** real-model
prompt-injection resistance. B04 real composition/shadow action-count/late-goal
integration now has local offline tests in this tree; coordinator must independently review the merged result.

Composition evidence and exact deltas are in `task-evidence/2026-09-30/B04-B07-composition/`: real DecisionService/ActionService/SQLite/Actor with fake transport prove no start/cancel/renew/enable from shadow choices, execute rejection in both Jev config modes, late goal rejection, configuration rejection while transport runs and after backend return. No production Jev quality or paid latency result is implied. The imported HTTP boundary tests run exclusively on loopback, never the vendor endpoint. Python 3.10 runtime validation remains deferred; grammar and absence of ExceptionGroup dependencies are checked locally.
