# Operator Console Design

Rust 2024 Ratatui 0.30 Operator Console: a peer client of the loopback Python
Runtime Host (ADR 0001). The committed contract covers Mission Intent editing,
reviewed Mission Activation, observation of one current Mission Run, owner-only
Mission Intent readback, idempotent Mission Run Cancellation, redacted Run
Activities/Observations, public Artifact/Conversation browsing, and optional
non-authoritative Run Narratives. Runtime Host v1.1 adds the Operator Debug View
from issue #56: four operator-centric tabs backed by an incremental shared
projection. Issue #32 delivers the status-only HITL view:
the permanent Human Decisions pane truthfully represents the empty and
awaiting-human-decision Mission Run states without decision-submission
controls.

## States

```
Connecting ──health ok──> Editing ──Alt+Enter──> ReviewActivation
    │                       ^   │                    │ Enter (once)   │ Esc
    │ health fails          │   └───── Esc ──────────┘                v
    v                       │   └─────────────── Esc ── Error <── Submitting
 Error (retry)              └──────────── Esc ──────────  │ 202
                          Run <───────────────────────────┘
```

- `ResizeRequired` overlays any state below 100x30 and resumes it on resize;
  host polling continues underneath.
- Bare Enter inserts a newline in the editor; Alt+Enter (or Ctrl+Enter where
  reported) opens review; Enter in review submits exactly once; Escape returns
  to editing; Ctrl+C quits from anywhere.
- The Activation Request ID is assigned when review opens and is reused for
  retries of the unchanged intent, so a lost HTTP response cannot create a
  second Mission Run. Editing the intent and re-reviewing assigns a fresh ID.

## Fixed 100x30 layout

Header (3 rows: console, host, API version, short session id), footer (3 rows:
key hints plus transient hint/notice), and per-state body. A v1.1 Run body has a
persistent tab bar and one active section:

1. **Overview** shows the authoritative Mission Run Record, latest Hyper and
   Maneuver progress, FSM/environment state, active maneuver, compact narrative
   disposition, evidence counts, significant activity, and HITL status.
2. **Agents** shows Hyper and Maneuver invocations with Recorded Debug Reasoning,
   response content, decisions, tool arguments/results/errors, and timing.
3. **Environment** keeps the latest authoritative state visible above an
   operational timeline. Heartbeats, snapshots, source facts, duplicates, and
   replay markers are filtered by default.
4. **Artifacts** merges Public Artifact Inbox descriptors with allowlisted,
   run-scoped planner outputs and opens both through the paged inspector.

Keys `1`-`4` select tabs directly; Tab and Shift+Tab cycle. Agents follows the
newest invocation until Up/k or Down/j moves selection; `f` resumes following
and PgUp/PgDn scroll detail. Environment Up/k and Down/j browse history while
`r` toggles raw evidence. Artifacts uses Up/k, Down/j, and Enter. In the
inspector, Right/n fetches the next 4096-byte page, Left/p returns to the
previous offset, and Esc closes it. `c` requests cancellation and `q` performs
the managed exit flow from every tab.

A v1.0 Host retains the transport-oriented Activities/Observations dashboard
and displays a `LEGACY VIEW` banner instead of treating the missing v1.1 route
as a failure. Below 100x30 only the resize-required state is drawn.

### HITL status view (issue #32)

The Human Decisions pane is the permanent HITL tab: it is always present in the
Run dashboard, in both empty and awaiting states. Its only input is the Host's
public, versioned Mission Run Status from `GET /api/v1/mission-runs/current`.
The view is status-only: it binds no keys, renders no control affordances,
emits no `HostCommand`, and presents identically to owner and observer
consoles. This delivery adds no Human Decision Request endpoint,
permitted-action payload, checkpoint identity, submission command, or resume
operation, and it does not wire the planner's Python `HumanDecision*`
implementation into the Host or Console.

Empty (no current run, or any status other than `awaiting_human_decision`):

```text
 No Human Decision Requests require action.
 Status-only view: no decision controls.
```

Awaiting (the Host reports Mission Run Status `awaiting_human_decision`):

```text
 AWAITING HUMAN DECISION
 Status: awaiting_human_decision
 The Mission Run is paused, awaiting a Human
 Decision.
 Status-only view: no decision controls.
```

The awaiting headline uses the same Magenta status accent as the Mission Run
pane; explanatory lines are dimmed. When the Host later reports a non-awaiting
status, the placeholder clears back to the empty state. Acceptance tests live
in `operator-console/tests/hitl.rs`; the 100x30 captures below are their
fixtures.

## HTTP boundary

All host IO lives behind `host::HostClient`; the UI never blocks on HTTP. The
run loop forwards `HostCommand` values to a worker thread (`spawn_worker`) with
bounded per-request timeouts; results return as `HostMessage` values. Mission
Run polling (400 ms cadence, Run state only) is enqueued from the run loop, not
from drawing.

Bounded v1.0 compatibility surface exercised by the fixture contract tests
(Rust fixture server in `operator-console/tests/support/`):

- `GET /api/v1/health` -> `200 {"status":"ok","api_version":{"major":1,"minor":0}}`
- `POST /api/v1/mission-activations` (`Authorization: Bearer <credential>`) ->
  `202` queued acceptance with generated Mission/Mission Run IDs; replay of the
  same request id/body/credential returns the original acceptance; conflicting
  reuse -> `409 activation_request_conflict`; another non-terminal run ->
  `409 mission_run_active`; invalid strict JSON -> `422` with a stable
  machine-readable code.
- `GET /api/v1/mission-runs/current` -> `{"mission_run":null}` or identifiers,
  status, timestamps, and nullable terminal classification.
- `GET /api/v1/mission-runs/{mission_run_id}/observations?cursor=&limit=` -> a
  public `200` page with `schema_version`, Mission/Mission Run IDs, redacted
  observation envelopes, and an opaque `next_cursor`. Each envelope contains
  `observation_sequence`, `observed_at`, and a redacted `TraceViewItem`.
- `GET /api/v1/mission-runs/{mission_run_id}/activities?cursor=&limit=` -> a
  public `200` page with `schema_version`, Mission/Mission Run IDs,
  `mapping_version: 1`, activity projections, and an opaque `next_cursor`.
- `GET /api/v1/mission-runs/{mission_run_id}/narrative` -> a public `200`
  optional Run Narrative with `none`, `available`, or `unavailable` status,
  nullable text and generation time, a source watermark, terminal flag, and
  nullable unavailability evidence. Available Run Narratives are displayed as
  non-authoritative. An unknown Mission Run returns `404 mission_run_not_found`.
- `GET /api/v1/mission-runs/{mission_run_id}/artifacts?cursor=&limit=` -> a
  public `200` page with `schema_version`, Mission/Mission Run IDs, Artifact
  descriptors sorted by `artifact_id`, and opaque `next_cursor`. A descriptor
  contains `schema_version`, `artifact_id`, `kind`, `media_type`, nullable
  `byte_size` and `content_digest`, `display {title, summary}`, `published_at`,
  and `classification`. Limits are 1..500, default 100; an empty page has
  `next_cursor: null`. Errors are `404 mission_run_not_found` and `422
  invalid_cursor`.
- `GET /api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/content?offset=&limit=`
  -> public `200` content metadata with `schema_version`, Mission/Mission Run
  IDs, `artifact_id`, `classification`, `media_type`, nullable `byte_size`,
  `offset`, nullable `next_offset`, `eof`, `truncated`, and nullable `content`.
  Text content uses byte offsets and limits 1..16384 (default 4096); the console
  always requests 4096. Binary content is metadata-only with `content: null`.
  Errors are `404 mission_run_not_found`, `404 artifact_not_found`, `404
  artifact_unavailable`, and `422 invalid_request` for invalid offset/limit.
- `GET /api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/entries?cursor=&limit=`
  -> public conversation page with `schema_version`, Mission/Mission Run IDs,
  `artifact_id`, ordered entries, and opaque `next_cursor`. Each entry contains
  `sequence`, `author`, `time`, `audience`, `kind`, nullable `content`, and
  nullable `content_ref {path, media_type, byte_size, content_digest}`. Sequence
  gaps and content-reference entries are valid. Limits are 1..500, default 100;
  an empty page has `next_cursor: null`. Errors are `404 mission_run_not_found`,
  `404 artifact_not_found`, and `422 invalid_cursor`.
- `GET /api/v1/mission-runs/{mission_run_id}/mission-intent`
  (`Authorization: Bearer <credential>`) -> owner-only `200` with Mission Run
  ID, Mission Intent, and source authority.
- `POST /api/v1/mission-runs/{mission_run_id}/cancellations`
  (`Authorization: Bearer <credential>`) with `cancellation_request_id` ->
  owner-only `202` with `disposition: "cancellation_requested"`, current Run
  status, and request time. Replaying the same request ID returns the original
  result; conflicting reuse -> `409 cancellation_request_conflict`.
- Missing, stale, or non-owner credentials on either owner endpoint -> the same
  fixed `403 authorization_failed` response, without Mission Intent or
  credential/verifier data.

### Operator Debug View (Runtime Host v1.1)

`GET /api/v1/mission-runs/{mission_run_id}/operator-view` requires exactly one
`section=overview|agents|environment|artifacts`. `limit` defaults to 50 and is
bounded to 1..100. `cursor` requests changes newer than a section high-water
mark; `before` pages bounded history, and the two are mutually exclusive.
`raw=true|false` is accepted only for Environment. Unknown or repeated query
parameters, invalid bounds, and invalid combinations return `422
invalid_request`; malformed, foreign, future, or cross-section cursors return
`422 invalid_cursor`.

Every response carries schema version, Mission/Mission Run identity, current
Mission Run Status, section, debug disposition, `next_cursor`, optional
`before_cursor`, and `has_more`. Section payloads are `overview`, `agents`,
`environment`, or `artifacts`. Stable invocation, timeline, and Artifact
identities let the console merge incremental changes in place. The console
polls `/current` plus only its active section, rejects stale request generations,
and retains all previously received tab evidence across transport failures.

The endpoint requires no Console Session credential because it is available
only through the loopback Runtime Host boundary. With `debug: true`, agent
records may include detailed tool arguments/results/errors and explicitly
labeled **Recorded Debug Reasoning**. With debug disabled, high-level agent
progress remains visible and both response and invocation records say
`debug_evidence_unavailable`. Recorded Debug Reasoning is experimental,
non-authoritative evidence: it never enters public Run Observations and cannot
determine Mission Run, planner, FSM, or environment state.

Artifacts combines Public Artifact Inbox items with allowlisted planner names:
`model.mzn`, `data.dzn`, `domain.pddl`, `problem.pddl`, `minizinc.plan`,
`sas_plan`, `generate_statechart.py`, `statechart.json`,
`accepted-statechart.json`, and `statechart-error.txt`. Planner files must be
regular, non-symlink files beneath the Mission Run planner root and no larger
than 1 MiB. Their content uses the existing Artifact content response with a
maximum 4096-byte page. Traversal, symlink, outside-root, non-allowlisted, and
oversized files are unavailable.

The public evidence endpoints require no Authorization header. Their default
limit is 100 and maximum limit is 500. A cursor is opaque base64url and belongs
to one Mission Run; an empty page always returns `next_cursor: null`.
Malformed, foreign, expired, or future cursors return `422 invalid_cursor`, and
an unknown Mission Run returns `404 mission_run_not_found`. A non-empty page
always carries a `next_cursor` for the last returned sequence, so a live
Mission Run has no end-of-stream marker; an empty page means "no new evidence
since this cursor". The Host durably retains a Mission Run's observation log
for as long as it retains the Mission Run itself; there is no time-based
expiry in v1, and issued observation sequences never change, so cursors stay
valid across retries and Host restarts.

Artifact IDs are defensively percent-encoded when placed in URL paths. Artifact
and Conversation collectors use the same 100-page client cap as Activities and
Observations and expose truncation in the dashboard notice.

Activity mapping version 1 defines the kinds `maneuver_command`, `correlated`,
`operational`, `observation`, and `evidence_marker`. Activities and
Observations are redacted projections for operator evidence display. They are
never authority for Mission Run lifecycle state; lifecycle authority remains
the current Mission Run endpoint.

Runtime Host liveness is based only on definitive Host HTTP response receipt,
not on whether new observations arrived. Successful responses and modeled HTTP
errors refresh liveness; transport failures and malformed response bodies do
not. The default inclusive thresholds are stale at 5 seconds and offline at 30
seconds. Successful empty polls keep the console live. During a response gap,
the console retains the last Run, Activities, Observations, Artifacts, and
Conversation entries; stale/offline status is displayed and mutation controls
are disabled until both connectivity and Console Session ownership are
available. A later definitive response restores connectivity without clearing
retained evidence.

The console generates the Activation Request ID, Console Session ID, and a
high-entropy Bearer credential before activation; it sends `source_authority:
"operator_console"`.

### Contract provenance and interoperability

`contract/v1/*.json` are the committed machine-readable wire examples for the
bounded v1 surface, transcribed from issues #27 through #29. They are the single
source of truth on the Rust side. The #28 additions are:

- `mission-intent.response.json` - owner Mission Intent readback.
- `mission-run-cancellation.request.json` - idempotency key request body.
- `mission-run-cancellation.accepted.response.json` - original/replayed `202`.
- `mission-run-cancellation.conflict.response.json` - conflicting-ID `409`.
- `mission-run-owner.authorization-failed.response.json` - fixed `403` shared
  by missing, stale, and non-owner bearer credentials.

The #29 additions are the observation page, empty page, invalid-cursor error,
Mission Run not-found error, activity page, and activity empty-page examples.

The #30 additions are:

- `mission-run-artifacts.page.response.json`
- `mission-run-artifacts.empty.response.json`
- `mission-run-artifact-content.text-page.response.json`
- `mission-run-artifact-content.text-final.response.json`
- `mission-run-artifact-content.binary.response.json`
- `mission-run-artifact-entries.page.response.json`
- `mission-run-artifact-entries.empty.response.json`
- `mission-run-artifact.not-found.response.json`
- `mission-run-artifact.unavailable.response.json`

The #32 addition is
`mission-runs.current.awaiting-human-decision.response.json` - the public
current-Mission-Run example carrying the `awaiting_human_decision` status. No
runtime transition into that status is implemented; the example exists so the
console client and reducer are tested against the versioned status.

`contract/v1.1/*.json` pins the exact Overview, Agents, Environment, and
Artifacts Operator Debug View responses from issue #56. The Rust fixture serves
all four, and `operator-console/tests/python_interop.rs` also starts a real
Python Runtime Host and decodes every section through the production Rust HTTP
client.

Existing #27 fixture consumers:

- `operator-console/tests/support/` serves these bytes (static bodies:
  health, conflicts, invalid request, empty current run) or these shapes with
  fixture values substituted (accepted activation, active current run), so the
  fixture cannot drift from the committed contract.
- `operator-console/tests/contract_examples.rs` asserts every example
  round-trips through the client DTOs with exact value equality - no missing
  or extra fields - and that the health body matches the issue text byte for
  byte.

Real interoperability against the Python Runtime Host process is validated by
the Rust subprocess test as well as the Python Host contract tests. The current
Host advertises API v1.1; the committed v1.0 health fixture remains intentionally
available to verify legacy fallback. Existing v1.0 endpoints are unchanged.

- `GET /api/v1/health` -> `200 {"status":"ok","api_version":{"major":1,"minor":1}}`.
- `POST /api/v1/mission-activations` with `Authorization: Bearer ...` and body
  `activation_request_id`, `console_session_id`, `mission_intent`,
  `source_authority` -> `202` with `activation_request_id`, `mission_id`,
  `mission_run_id`, `status` (`queued`), `created_at`; conflicts ->
  `409 {"error":{"code":"activation_request_conflict"|"mission_run_active",...}}`;
  invalid strict JSON -> `422 {"error":{"code":"invalid_request",...}}`.
- `GET /api/v1/mission-runs/current` -> `{"mission_run":null}` or
  `mission_id`, `mission_run_id`, `status`, `created_at`, `started_at`,
  `finished_at`, and nullable `terminal_classification`.
- `GET /api/v1/mission-runs/{mission_run_id}/mission-intent` with the owning
  bearer credential -> `200` with `mission_run_id`, `mission_intent`, and
  `source_authority`.
- `POST /api/v1/mission-runs/{mission_run_id}/cancellations` with the owning
  bearer credential and `cancellation_request_id` -> `202` with
  `mission_run_id`, `cancellation_request_id`, `disposition`, current `status`,
  and `requested_at`; conflicting reuse -> `409
  cancellation_request_conflict`.
- Owner recovery is client-side through the local session file specified by
  issue #28. It does not add an HTTP recovery endpoint.
- `GET /api/v1/mission-runs/{mission_run_id}/observations` and `/activities`
  are public, cursor-paginated, redacted evidence projections with the status
  and cursor behavior described above.
- `GET /api/v1/mission-runs/{mission_run_id}/artifacts`, Artifact `/content`,
  and conversation `/entries` are public and implement the #30 shapes, bounds,
  paging, and stable errors described above.

## Committed terminal-frame fixtures

`frames/` holds readable plain-text captures rendered by Ratatui
`TestBackend` and treated as test fixtures (`operator-console/tests/render.rs`):

- `editing-100x30.txt` - Mission Intent editor
- `review-activation-100x30.txt` - activation review
- `run-dashboard-100x30.txt` - active-run dashboard with empty evidence panes
- `activity-detail-100x30.txt` - two activities with linked observation detail
- `run-stale-100x30.txt` - stale Host badge with retained evidence
- `run-offline-100x30.txt` - offline Host badge with retained evidence
- `cancellation-confirmation-100x30.txt` - cancellation confirmation
- `cancellation-requested-100x30.txt` - cancellation-requested run state
- `recovered-owner-100x30.txt` - recovered owner session and active run
- `resize-required-80x24.txt` - below-minimum terminal
- `artifact-text-page-100x30.txt` - text Artifact inspector page
- `artifact-binary-metadata-100x30.txt` - binary Artifact metadata inspector
- `conversation-entries-100x30.txt` - Artifact selection and Conversation entries
- `hitl-empty-100x30.txt` - HITL pane empty state: no Human Decision Requests require action
- `hitl-awaiting-100x30.txt` - HITL pane awaiting-human-decision status placeholder
- `operator-overview-100x30.txt` - authoritative status and significant progress
- `operator-agents-100x30.txt` - invocation selection and Recorded Debug Reasoning detail
- `operator-environment-100x30.txt` - latest environment state and filtered timeline
- `operator-artifacts-100x30.txt` - merged Public Inbox and planner Artifact view

Regenerate after an intentional layout change:

```sh
UPDATE_FRAMES=1 cargo test --test render
```

Trailing whitespace is trimmed per line in both the renderer and the fixture
comparison so captures stay diff-friendly.
