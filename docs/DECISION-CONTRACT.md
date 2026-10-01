# B00 Decision Contract

Status: B00 parser-backed wire, observation and backend-selection shapes are
specified and under final cross-endpoint review. B02/B04 runtime ownership and
B08 management API remain planned, not executable here. B00 does not execute
actions, own runtime locks, or connect to ROS, hardware, transports, or cloud models.

## Wire Boundary

Business payloads use integer `schema_version: 1` inside the unchanged
`astrbotex-zmq` envelope version 1. The decision methods are:

- `decision.context.get`
- `decision.goal.submit`
- `decision.goal.cancel`
- `decision.goal.renew`
- `decision.state.get`
- `decision.events.get`
- `decision.feedback`

Unknown methods return `unsupported_method`; they never fall back to the legacy
proposal path. Bootstrap context/state reads may omit `ex_session`. Goal,
event, and feedback messages require their declared session field. Identity and
owner admission come from trusted connection/plugin context, never from an
unverified payload claim.

| Method (direction) | Parsed request | Response contract/status |
|---|---|---|
| `decision.context.get` (AEB -> EX) | `{schema_version, ex_session?}` | Read-only context **planned**; no response parser here |
| `decision.goal.submit` (AEB -> EX) | `GoalSubmit` | `GoalSubmitResult` serializer (below); no response parser |
| `decision.goal.cancel` (AEB -> EX) | `GoalCancel` | Admission response **planned**; accepted does not mean stopped |
| `decision.goal.renew` (AEB -> EX) | `GoalRenew` | Lease renewal response **planned**; does not create a goal |
| `decision.state.get` (AEB -> EX) | `{schema_version, ex_session?}` | `DecisionState.parse` |
| `decision.events.get` (AEB -> EX) | `EventsRequest` | `EventsReply.parse` / `events_reply` |
| `decision.feedback` (EX -> AEB) | `Feedback` | Durable `event_seq` acknowledgement **planned**; no acknowledgement parser |

The unchanged v1 envelope supplies transport request ID and `reply_to`. The
standalone `DecisionSnapshot`/`BackendDecision` types are B00 parser-backed
structures for B04 to consume; they are **not** additional `decision.*` methods
or transport-routed response payloads. Runtime/backend process integration
remains planned. Transport ACK, submit admission, goal activation, and action
completion are distinct facts. System stop/cancel does not depend on an LLM's
`allowed_actions` list.

## Limits And Errors

All parsers reject unknown fields, bool-as-int values, non-finite numbers,
cycles, non-string object keys, overlong IDs/text, excessive depth, nodes,
serialized UTF-8 bytes, or validation work. The current limits are:

- IDs and diagnostic reason text: 256 characters
- goal text: 4096 characters
- lease and action duration: positive integer milliseconds, at most 600000
- sequence/revision values: integer `0..2**53-1`; event and feedback sequence
  values are positive when emitted
- validation depth: 32; nodes/work: 20000; encoded value bytes: 1048576;
  JSON integer digits: at most 4096
- supported pattern length: 256 characters

Rejections use `ValidationError(code, path, message)`. Important codes include
`missing_field`, `unknown_field`, `invalid_type`, `non_finite_number`,
`value_budget_exceeded`, `schema_depth_exceeded`, `range_violation`,
`enum_violation`, `owner_mismatch`, `revision_conflict`,
`stale_ex_session`, `duplicate_request_id_conflict`, and
`legacy_isolation`. Wrong-type method is `invalid_type`, empty method is
`empty_string`, and a method over 256 characters is `text_too_long`.
`decision.events.get` without `ex_session` is `missing_field` at `ex_session`.
At the decision wire boundary, invalid field values report root-relative paths
such as `parameters.arm.pick.v1.target.depth_m`; whole-message byte/depth/digit
budget errors report `goal_submit`. Numeric `multipleOf` uses exact decimal JSON
representations (so `0.3` is a multiple of `0.1` but `1.0000000001` is not a
multiple of `1`), with operand sizes bounded by the JSON input budget and exact
integer modulo. Very large but valid JSON numbers are not refused merely because
their decimal ratio exceeds a separate arithmetic threshold.

Parsed mutable values are defensive copies. Idempotency stores a canonical copy:
the same request ID and equivalent JSON payload replays; a different payload
conflicts before any dispatch side effect.

## Parser-Backed Wire Fields

`R` means parsing requires a valid value; `O` means the field may be omitted.
All IDs below are nonblank strings of at most 256 characters; all `seq` and
revision fields are strict integers `0..2**53-1` unless stated otherwise.
Missing fields checked via `raw.get` may produce `invalid_type` rather than
`missing_field`; do not infer the code from this table. `to_dict()` serializes
defaulted fields even if the input omitted them unless explicitly noted.

| ActionCommand field | Presence/type and constraint |
|---|---|
| `schema_version` | R; integer `1` |
| `command_id`, `ex_session`, `goal_id`, `decision_id`, `owner`, `action_id` | R; IDs |
| `goal_revision`, `plugin_generation` | R; sequence integers |
| `operation` | R; `start|cancel|pause|resume` |
| `params` | R; JSON object; missing/null => `missing_params` |
| `lease_ms` | R; integer `1..600000` |

`ActionCommand.parse` checks shape; `validate_command_against_manifest` separately
checks trusted session, revision, owner, runtime state, declared/granted operation,
and start parameters. Internal monotonic deadlines, resource tokens and environment
generation are **not** wire fields and must be stamped by the framework.

| ActionEvent field | Presence/type and constraint |
|---|---|
| `event_id`, `ex_session`, `task_id`, `goal_id`, `command_id`, `owner` | R; IDs |
| `event_seq` | R; integer `1..2**53-1` |
| `goal_revision` | R; sequence integer |
| `status` | R; action status from the transition table below |
| `reason_code` | O; string `0..256`, default `""` |
| `details` | O; JSON object, default `{}`; canceled requires `stop_evidence` below |

`ActionEvent` has no top-level `schema_version` or `stop_evidence`; adding either
is an unknown-field error. A page event's parser requires the same identifiers,
status and sequence but preserves omitted `reason_code`/`details` in its nested
copy. `ActionEvent.to_dict()` materializes both defaults. A canceled report
requires `details.stop_evidence: {"stopped": true}`. Optional `source` and
`reference` are bounded nonblank strings; no other evidence keys are accepted.

| GoalSubmit field | Presence/type and constraint |
|---|---|
| `schema_version` | R; integer `1` |
| `request_id`, `ex_session`, `task_id`, `step_id`, `goal_id` | R; IDs |
| `goal_text_en` | R; nonblank trimmed text at most 4096 characters; language marker advisory only |
| `allowed_actions` | O; list of unique nonblank strings, default `[]`; each ID at most 256 |
| `parameters` | O; map of allowed action IDs to JSON objects, default `{}` |
| `completion` | O; object, default `{}`; only `required_success_actions` (list of allowed action IDs) permitted |
| `lease_ms` | R; integer `1..600000` |
| `expected_revision` | O; sequence integer; omit for no CAS, explicit null is invalid; omitted on output |

The goal and bound parameters advance atomically. `check_revision(None, current)`
and `check_revision(current, current)` both return `current + 1` (up to the
sequence limit); a mismatched CAS returns `revision_conflict`. A parameter key
outside `allowed_actions`, or a completion action outside it, is `unknown_action`.

`GoalSubmitResult` is a **serializer only**, not a response parser: `ok` (bool),
`request_id`, `ex_session`, `goal_id` (IDs), `revision` (sequence), `phase`
(`accepted|pending_cancel|active|blocked|rejected`) are its expected fields;
optional `error` is `{code, path, message}`. Its dataclass does not revalidate
constructed values. ACK/accepted does not imply active or stopped.

| Goal control request | Presence/type and constraint |
|---|---|
| `GoalCancel`: `schema_version`, `request_id`, `ex_session`, `goal_id`, `goal_revision` | R; version `1`, IDs and sequence integer |
| `GoalCancel.reason_code` | O; string `0..256`, default `""` and materialized on output |
| `GoalRenew`: `schema_version`, `request_id`, `ex_session`, `goal_id`, `goal_revision`, `lease_ms` | R; version `1`, IDs, sequence integer, positive lease `1..600000` |

Cancel acceptance is not confirmation of stopping. Renew is a lease operation on
the current goal, never permission to create a new goal. Neither response has a
B00 parser; response envelopes/semantics beyond admission are planned.

| State/replay/feedback | Presence/type and constraint |
|---|---|
| `DecisionState.schema_version`, `ex_session`, `revision` | R; version `1`, ID, sequence integer |
| `DecisionState.active_goal_id` + `active_phase` | O paired IDs/phase; only `active` with active goal; absent serializes as null |
| `DecisionState.pending_goal_id` + `pending_phase` | O paired IDs/phase; pending phase `accepted|pending_cancel|blocked`; absent serializes as null; pending ID differs from active |
| `DecisionState.execution`, `event_seq` | O; JSON object `{}` and sequence integer `0` by default |
| `EventsRequest.schema_version`, `ex_session`, `since_event_seq` | R version `1` and ID; O cursor integer `0..2**53-1`, default `0` |
| `EventsReply.schema_version`, `ex_session`, `events`, `oldest_available_seq`, `latest_event_seq`, `resync_required` | All R on parse; version `1`, ID, list of events, two sequence integers, strict bool |
| `Feedback.schema_version`, `ex_session`, `task_id`, `goal_id`, `goal_revision`, `event_seq`, `status` | R; version `1`, IDs, revision sequence, event integer `1..2**53-1`, action status |
| `Feedback.reason_code`, `details` | O; string `0..256` default `""`, JSON object default `{}`; both materialized on output |

State IDs/phases must be paired (`invalid_type` if missing partner). `EventsReply`
checks ascending unique event sequences, matching session and inclusive
`oldest_available_seq..latest_event_seq`; oldest may be latest + 1 for empty
history. `events_reply` validates **all** buffered entries even when resync
will return `[]`; if `since_event_seq < oldest_available_seq - 1`, it sets
`resync_required: true` and returns no events. Event errors use indexed
`events[0].event_seq` paths. An acknowledged feedback event sequence means
*durable receipt*, not execution success. Canceled Feedback and page events
require the same `details.stop_evidence` as ActionEvent.

## Action Manifest V2

A v2 manifest has `action_api_version: 2` and non-empty `actions`. Each action
has a qualified owner-prefixed `action_id`, bounded description, supported JSON
Schema subset, unique resources and operations, runtime-state requirements,
optional duration, cancel timeout, and `danger` in `low|medium|high`.

| Manifest field | Presence/type and constraint |
|---|---|
| `action_api_version`, `actions` | R; exact integer `2`; nonempty list of action declarations |
| `id` | O; plugin ID, must match trusted `owner` when supplied |
| `observation_sources` | O; map from source ID to `{topic, max_age_ms, required_fields}`, default `{}` |
| `observation_guide` | O; string at most 256 characters, default `""` |
| `provides`, `ros2_ports` | O; unique nonblank string list `[]`; list of objects `[]` (metadata only) |

| Action declaration field | Presence/type and constraint |
|---|---|
| `action_id`, `description`, `schema` | R; owner-prefixed qualified ID, nonblank description, validated object-root schema |
| `operations` | R; nonempty unique list of `start|cancel|pause|resume` |
| `resources`, `requires_runtime_state`, `requires_observations` | O; unique lists, default `[]`; known runtime states `idle|ready|running|paused|fault|finished` |
| `max_duration_ms`, `cancel_timeout_ms` | O; if present `1..600000`; cancel operation *requires* cancel timeout, timeout without cancel is invalid |
| `danger` | O; `low|medium|high`, default `low` |

`requires_observations` names must resolve through manifest
`observation_sources`. A real plugin manifest may also include `id`; when
present the parser validates its match to the trusted owner and action IDs.
Each source is:

```json
{
  "topic": "front_sensor.clearance",
  "max_age_ms": 200,
  "required_fields": ["distance_m", "valid"]
}
```

The map key is a nonblank source ID at most 256 characters using the resource
name alphabet. `topic` is a nonblank qualified internal TopicBus name with a
dot, at most 256 characters; `max_age_ms` is integer `1..600000`, and
`required_fields` defaults to a unique list `[]` of nonblank strings. Unknown
fields in source mappings are rejected. `requires_observations` names must have
a corresponding source mapping.

The topic is an internal qualified TopicBus name; ROS names require an explicit
adapter. Observation payload shape is not normalized by B00. `provides` and
`ros2_ports` remain manifest metadata. A cancel operation requires a positive
`cancel_timeout_ms`; a timeout without cancel is rejected. Legacy manifests are
parsed only by the legacy parser, and v2 manifests are rejected there.

The supported schema subset checks nested `properties`, `required`,
`additionalProperties` (boolean or schema), `items`, `enum`, string/array lengths,
`uniqueItems`, bounded literal `pattern`, UUID `format`, and numeric bounds and
`multipleOf`; `description`/`title` are metadata. Allowed `type` values are
`object|array|string|number|integer|boolean|null`. A nested schema without
`type` supports only `enum` and optional `title`/`description`: combining
`enum` with `minimum`, `minLength`, `minItems`, `required`, `properties`, `items`,
`uniqueItems`, `pattern`, `format` or any other constraint is rejected with
`schema_unsupported_keyword`, never silently ignored. Unknown JSON Schema
keywords and null keyword values are rejected; this is not full JSON Schema.
Numeric bounds use their effective inclusive/exclusive endpoints. Enums use JSON
equality where booleans are not numbers; huge integers are compared without
float conversion. Pattern strings are at most 256 characters and use the
validator's limited literal-safe alphabet, not arbitrary regex constructs.

Direct command admission always requires trusted `current_ex_session`,
`current_revision`, `runtime_state`, and `trusted_owner` arguments. Admission
checks session, revision, owner, declared operation, granted operations,
runtime-state requirements, and start parameters before a dispatcher can act.
This function is validation only; it does not publish or execute a command.

## Decision State And Feedback

Goal phases are `accepted`, `pending_cancel`, `active`, `blocked`, and
`rejected`; only `active` enters decision. `expected_revision` is optional:
when omitted, `check_revision` still advances the current revision by one; when
present it must match exactly. `source_epoch` in observation wrappers is a
bounded string, not an integer.

Action statuses are `admitted -> accepted -> running -> terminal`. Terminal
statuses are `rejected`, `succeeded`, `failed`, `canceled`, `timed_out`, and
`unknown`; they cannot regress. A `canceled` ActionEvent or Feedback requires
`details.stop_evidence.stopped` to be the JSON boolean `true`. Missing evidence,
`false`, numeric `1`, or a top-level `stop_evidence` cannot establish cancellation.
The optional `details.stop_evidence.source` and `.reference` fields are bounded
nonempty strings (at most 256 characters). The B02 ledger must independently
verify the trusted owner and generation and bind source/reference to the actual
command; a parsed payload is not proof of trust. A stop timeout is `timed_out`
and does not release uncertain ownership.

The event wire fields are `event_id`, `event_seq`, `ex_session`, `task_id`,
`goal_id`, `goal_revision`, `command_id`, `owner`, `status`, `reason_code`, and
`details`; stop evidence lives inside `details`, never as a new top-level field.
Event sequences and feedback sequences are positive and strictly bounded.
Event replay validates the full buffer, rejects foreign sessions, duplicates,
out-of-range pages, and false `resync_required` payloads. Event diagnostics use
`events[0].event_seq`-style indexed paths even for buffered history;
`resync_required` returns no events.

| Current status | Allowed next status |
|---|---|
| `admitted` | `accepted`, `rejected`, `canceled`, `timed_out`, `unknown` |
| `accepted` | `running`, `succeeded`, `failed`, `canceled`, `timed_out`, `unknown` |
| `running` | `succeeded`, `failed`, `canceled`, `timed_out`, `unknown` |
| `rejected`, `succeeded`, `failed`, `canceled`, `timed_out`, `unknown` | same status only (terminal) |

Re-reporting the current status is idempotent. A terminal status cannot regress.
`cancel_outcome(has_stop_evidence=False, timed_out=False)` remains pending;
with timeout it is `timed_out`; only positive evidence yields `canceled`.
Replacement phases use `accepted`, `pending_cancel`, `active`, `blocked`, and
`rejected`; `decide_phase` enters `active` only for an accepted non-replacement
or an accepted replacement whose old action is stopped. A failed/uncertain stop
leaves the new goal blocked and the decision gate closed.

## Goal, Command And Event Examples

The following three business payloads are accepted by `GoalSubmit.parse`,
`ActionCommand.parse`, and `ActionEvent.parse`, respectively. A corresponding
`Feedback.parse` form is specified immediately after them. Transport envelopes
supply IDs and replies independently.

```json
{"schema_version":1,"request_id":"req-17","ex_session":"ex-boot-a","task_id":"task-4","step_id":"step-2","goal_id":"goal-9","expected_revision":8,"goal_text_en":"Pick the selected cup using the arm. Keep the base stopped.","allowed_actions":["new_arm.pick_selected.v1"],"parameters":{"new_arm.pick_selected.v1":{"target":{"observation_id":"obs-40","object_id":"track-8","selected":true}}},"completion":{"required_success_actions":["new_arm.pick_selected.v1"]},"lease_ms":10000}
```

```json
{"schema_version":1,"command_id":"cmd-21","ex_session":"ex-boot-a","goal_id":"goal-9","goal_revision":9,"decision_id":"dec-12","owner":"new_arm","plugin_generation":3,"action_id":"new_arm.pick_selected.v1","operation":"start","params":{"target":{"observation_id":"obs-40","object_id":"track-8","selected":true}},"lease_ms":1000}
```

```json
{"event_id":"evt-22","event_seq":22,"ex_session":"ex-boot-a","task_id":"task-4","goal_id":"goal-9","goal_revision":9,"command_id":"cmd-21","owner":"new_arm","status":"canceled","details":{"stop_evidence":{"stopped":true,"source":"new_arm","reference":"cmd-21"}}}
```

Omitted `reason_code` in the event is accepted; `ActionEvent.to_dict()` emits
`"reason_code":""` and preserves the nested evidence. A corresponding
`Feedback.parse` input adds `schema_version:1`, removes `event_id`, `command_id`
and `owner`, and otherwise keeps the same session, task/goal, revision, sequence,
status and `details`; `Feedback.to_dict()` also emits `reason_code:""`.
Neither parser proves trusted source, generation, or physical stop by itself.

## B00 Observation And Backend Types

The pure `ObservationEnvelope`, `VersionSet`, `DecisionSnapshot` and
`BackendDecision` parsers are now implemented in `core/decision/models.py` and
its generated standalone mirror. These are data validation only: no observation
producer, backend call, freshness store or dispatcher is implemented by B00.
Every object rejects unknown fields, invalid JSON (non-finite values, cycles,
non-string keys) and depth/node/UTF-8 byte budget overflow. `to_dict()` copies
mutable values. `DecisionSnapshot` and `BackendDecision` require integer
`schema_version:1`; `VersionSet` is nested and has no schema version. Nested
parser errors include observation indexes and the `versions` location (for
example, `observations[1].health.status` and
`versions.plugin_generations.owner`); standalone parsers retain local paths.
`validate_backend_selection` reparses mutable inputs at use time, including
all version/finite fields, and requires exactly one choice for every owner.
Selected candidates are defensive copies.

| `ObservationEnvelope` required field | Type/bound |
|---|---|
| `schema_version` | Exact integer `1` |
| `observation_id`, `source_id`, `source_epoch`, `description_hash` | Nonblank bounded strings (`source_epoch` is **not** an integer) |
| `seq` | Integer `0..2**53-1` |
| `received_monotonic_ns` | Integer `0..2**63-1` (not a 53-bit sequence) |
| `age_ms` | Finite, nonnegative number; fractional values such as `12.5` accepted |
| `health` | Exact `{status: ok|stale|error, reason_code: string 0..256}` |
| `data` | Original source-specific JSON object; deep copied, never renamed |

Parsing a timestamp or age grants no freshness authority: EX must stamp trusted
receive time and recompute age on admission. Sensor generations and source epochs
must be checked against current trusted context.

| `VersionSet` required field | Type/bound |
|---|---|
| `ex_session` | Bounded nonblank ID |
| `goal_revision`, `config_revision`, `catalog_revision`, `gate_epoch` | Integer `0..2**53-1` |
| `environment_generation` | Integer `0..2**53-1`, matching environment manager generation; **not a string** |
| `plugin_generations` | Map of bounded owner IDs to sequence integers `0..2**53-1` |

| `DecisionSnapshot` required field | Type/bound |
|---|---|
| `schema_version`, `snapshot_id`, `created_monotonic_ns` | Version `1`, bounded ID, integer `0..2**63-1` |
| `versions` | Exact `VersionSet` above |
| `goal` | Exact `{task_id, goal_id, goal_text_en, allowed_actions, parameters}`; IDs nonblank, goal text nonblank <=4096, unique allowed IDs; parameters map only allowed IDs to JSON objects |
| `observations` | List of validated `ObservationEnvelope` values |
| `owners` | List of exact `{owner, plugin_generation, status, candidates}`; unique owner IDs; generation must match `versions.plugin_generations[owner]` |

Owner `status` is a bounded nonblank diagnostic string. Candidate fields are
`option_id` (globally unique bounded ID), `kind`, nonblank `description` <=4096,
strict bool `eligible`, optional bounded `reason_code` default `""` (emitted by
`to_dict()`), optional `action_id` and optional `command_id`. `kind` is
`start|cancel|pause|resume|keep|wait|request_replan`: start requires an
`action_id` allowed by the goal with the same owner prefix and **no** existing
`command_id`; cancel/pause/resume/keep require an existing `command_id` and any
supplied `action_id` must have the owner prefix; wait/replan carry neither ID.
Omitted optional IDs stay omitted on serialization; explicit null is invalid.
Eligibility is descriptive, not admission authority.

| `BackendDecision` required field | Type/bound |
|---|---|
| `schema_version`, `snapshot_id`, `versions` | Version `1`, bounded ID, exact `VersionSet` |
| `backend`, `model`, `elapsed_ms` | Bounded nonblank IDs and finite nonnegative number |
| `choices` | List of exact `{owner, option_id, confidence?, probabilities?}` with unique owner IDs |

`confidence` is optional finite `0..1`. `probabilities` is an optional map from
**option IDs** to finite `0..1` numbers, not a map keyed by generic kinds like
`start` or `wait`. `validate_backend_selection(snapshot, decision,
current_versions)` checks exact snapshot ID and all version fields against the
trusted current set, exactly one choice per owner, and that each chosen option
belongs to that owner and is eligible. A supplied probability distribution must
be nonempty, contain only eligible options of that owner, and sum to 1 within
absolute tolerance `1e-9` (no relative tolerance). It returns defensive copies
of normalized selected candidates; it has no side effects or execution authority.
The backend cannot invent a goal, parameter, method, handler, topic or new
candidate; B04 must still recheck live freshness, resources and trusted
admission immediately before any execution. `keep` does not replay `start`;
`request_replan` cannot write a new goal. Backend overload must never silently
drop stop/wait candidates. Cloud wait/timeout cannot extend an action lease.

The next four examples are parsed by `ObservationEnvelope`, `VersionSet`,
`DecisionSnapshot` and `BackendDecision`. The versioned snapshot/decision also
bind an owner generation and option IDs; the omitted optional candidate
`reason_code` serializes as `""`.

```json
{"schema_version":1,"observation_id":"obs-40","source_id":"front","source_epoch":"camera-boot-a","seq":40,"received_monotonic_ns":9007199254740993,"age_ms":12.5,"description_hash":"hash-40","health":{"status":"ok","reason_code":""},"data":{"object_id":"track-8","selected":true}}
```

```json
{"ex_session":"ex-boot-a","goal_revision":9,"config_revision":1,"catalog_revision":2,"environment_generation":3,"gate_epoch":4,"plugin_generations":{"new_arm":5}}
```

```json
{"schema_version":1,"snapshot_id":"snap-1","created_monotonic_ns":9007199254740994,"versions":{"ex_session":"ex-boot-a","goal_revision":9,"config_revision":1,"catalog_revision":2,"environment_generation":3,"gate_epoch":4,"plugin_generations":{"new_arm":5}},"goal":{"task_id":"task-4","goal_id":"goal-9","goal_text_en":"Pick the selected cup.","allowed_actions":["new_arm.pick_selected.v1"],"parameters":{"new_arm.pick_selected.v1":{"target":{"observation_id":"obs-40"}}}},"observations":[{"schema_version":1,"observation_id":"obs-40","source_id":"front","source_epoch":"camera-boot-a","seq":40,"received_monotonic_ns":9007199254740993,"age_ms":12.5,"description_hash":"hash-40","health":{"status":"ok","reason_code":""},"data":{"object_id":"track-8","selected":true}}],"owners":[{"owner":"new_arm","plugin_generation":5,"status":"ready","candidates":[{"option_id":"opt-pick","kind":"start","description":"Pick cup","eligible":true,"action_id":"new_arm.pick_selected.v1"},{"option_id":"opt-wait","kind":"wait","description":"Wait","eligible":true}]}]}
```

```json
{"schema_version":1,"snapshot_id":"snap-1","versions":{"ex_session":"ex-boot-a","goal_revision":9,"config_revision":1,"catalog_revision":2,"environment_generation":3,"gate_epoch":4,"plugin_generations":{"new_arm":5}},"backend":"mock","model":"policy-1","elapsed_ms":12.5,"choices":[{"owner":"new_arm","option_id":"opt-pick","confidence":0.75,"probabilities":{"opt-pick":0.75,"opt-wait":0.25}}]}
```

## Planned Runtime Ownership (B02/B04)

The goal coordinator owns atomic goal revisions and replacement phases;
Dispatcher owns command admission and trusted version/gate checks; Ledger owns
command/event/idempotency records and verifies the owner and plugin generation
bound by `PluginContext`, plus command-specific stop source/reference. Actor
threads own bounded callback queues; a single serialized persistence writer owns
durable ordering. All queues must be bounded and locks held only for short state
transitions. Network, runtime and actor locks must not be held while awaiting a
backend/cloud call, database write, callback or physical stop. These are
**commitments for B02/B04**, not working services in B00. A restart must never
replay a physical start; pending physical ownership remains blocked/unknown
until reconciliation. `ex_session` invalidates replies from a previous boot.

## B08/B02 Boundary Notes

The planned management API has the exact twelve B08 paths below. HTTP/API
implementation is outside B00.

| Method | Path |
|---|---|
| GET | `/api/v1/ex/decision/status` |
| GET | `/api/v1/ex/decision/backends` |
| GET | `/api/v1/ex/decision/config` |
| POST | `/api/v1/ex/decision/config` |
| POST | `/api/v1/ex/decision/secret` |
| POST | `/api/v1/ex/decision/test` |
| POST | `/api/v1/ex/decision/mode` |
| GET | `/api/v1/ex/decision/catalog` |
| GET | `/api/v1/ex/decision/snapshot` |
| GET | `/api/v1/ex/decision/actions` |
| GET | `/api/v1/ex/decision/decisions` |
| POST | `/api/v1/ex/decision/stop` |

B02's planned decision mode is `disabled|shadow|execute`: disabled performs no
decision execution; shadow may evaluate without dispatch; execute still requires
trusted admission. It is **not** the legacy compatibility selector. A separate
`control_mode=legacy` default is a B02 compatibility commitment, orthogonal to
decision mode; the mode and gate are not implemented in B00. Pure action owners
are assigned the `action_owner` capability (`runtime_kind=action`, category
`control`), never borrowed from `motion_bridge`. Legacy v1 topic actions remain
isolated from v2; new execution remains gate-closed by default pending B02
implementation and authorization. Saving configuration, testing a backend,
enabling decision mode, and starting the runtime are separate planned operations.
B08 management is proposed, not yet an implemented secure deployment surface;
secret input must never be echoed in GET, SSE, logs or backups, and remote
management needs an independently reviewed authorization boundary.

## Acceptance Evidence And Outstanding Items

- Golden scenario coverage is asserted by IDs in `tests/test_decision_contracts.py`
  and generated in `tests/fixtures/decision_contracts/golden.json`; the fixture
  currently contains 81 cases, preserving the prior 72 IDs and adding the
  snapshot/backend cases. The generated fixture is compared structurally with
  `scripts/gen_contract_fixtures.py` by the contract tests; it contains no bare
  JSON NaN/Infinity values.
- Canonical generation is checked by `python scripts/build_contracts.py --check`;
  the generated `astrbot_ex/core/contracts.py` is rebuilt from the two authored
  modules and must be current before any mirror synchronization. A mirror is
  written only when an explicit `--mirror PATH` is supplied to
  `scripts/sync_contract_mirror.py`.
- The focused and full raw results for the current B00 review are preserved in
  `task-evidence/2026-09-29/`; the full result includes only the five existing
  ROS dependency skips. No A.E.B path is written by these scripts in this tree.



`astrbot_ex/core/actions/models.py` and `astrbot_ex/core/decision/models.py`
are the authored modules. `astrbot_ex/core/contracts.py` is generated by
`scripts/build_contracts.py`; the generator fails on duplicate top-level names
and on divergent shared constants. The exact schema fields/defaults and
parser-backed behavior are summarized above; B00 still does not implement a
transport router, response dispatcher, observation producer or backend engine,
runtime gate, decision manager API, or secret store. A mirror is written only
when an explicit `--mirror PATH` is supplied to `scripts/sync_contract_mirror.py`.

Golden fixtures are strict JSON. NaN and Infinity scenarios use explicit
`__fixture_non_finite__` markers and are converted to Python non-finite values
only by the fixture runner. The file preserves all B00 scenario IDs and has no
bare JSON NaN/Infinity.
