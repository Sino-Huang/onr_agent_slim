# Operator Console Design (issues #27-#29 contract slices)

Rust 2024 Ratatui 0.30 Operator Console: a peer client of the loopback Python
Runtime Host (ADR 0001). The committed contract covers Mission Intent editing,
reviewed Mission Activation, observation of one current Mission Run, owner-only
Mission Intent readback, idempotent Mission Run Cancellation, and redacted Run
Activities/Observations. Artifacts, Narratives, and HITL behavior remain
reserved for later issues (#30-#32).

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
key hints plus transient hint/notice), and per-state body. The Run dashboard
presents Mission Run identity/status/timestamps/terminal classification next to
selectable Run Activities and the selected activity's linked Observations.
Artifacts, Conversation, Narrative, and Human Decisions remain stable reserved
regions. A recovered owner's Mission Intent occupies the bottom-left slot in
place of Narrative. Below 100x30 only the resize-required state is drawn (also
enforced as a draw-time guard, not only via resize events).

## HTTP boundary

All host IO lives behind `host::HostClient`; the UI never blocks on HTTP. The
run loop forwards `HostCommand` values to a worker thread (`spawn_worker`) with
bounded per-request timeouts; results return as `HostMessage` values. Mission
Run polling (400 ms cadence, Run state only) is enqueued from the run loop, not
from drawing.

Bounded v1 surface exercised by the fixture contract tests (Rust fixture
server in `operator-console/tests/support/`, no Python process):

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
the console retains the last Run, Activities, and Observations; stale/offline
status is displayed and mutation controls are disabled until both connectivity
and Console Session ownership are available. A later definitive response
restores connectivity without clearing retained evidence.

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

Existing #27 fixture consumers:

- `operator-console/tests/support/` serves these bytes (static bodies:
  health, conflicts, invalid request, empty current run) or these shapes with
  fixture values substituted (accepted activation, active current run), so the
  fixture cannot drift from the committed contract.
- `operator-console/tests/contract_examples.rs` asserts every example
  round-trips through the client DTOs with exact value equality - no missing
  or extra fields - and that the health body matches the issue text byte for
  byte.

Real interoperability against the Python Runtime Host process is validated at
parent level by the Python Host tests (subprocess fixture); this lane does not
spawn a Python process. The exact v1 schema both sides implement:

- `GET /api/v1/health` -> `200 {"status":"ok","api_version":{"major":1,"minor":0}}`.
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

Regenerate after an intentional layout change:

```sh
UPDATE_FRAMES=1 cargo test --test render
```

Trailing whitespace is trimmed per line in both the renderer and the fixture
comparison so captures stay diff-friendly.
