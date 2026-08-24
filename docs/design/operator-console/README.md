# Operator Console Design (issue #27 slice)

Rust 2024 Ratatui 0.30 Operator Console: a peer client of the loopback Python
Runtime Host (ADR 0001). This slice covers Mission Intent editing, reviewed
Mission Activation, and observation of one current Mission Run. Cancellation,
recovery, Run Activities/Observations, Artifacts, Narratives, and HITL behavior
remain reserved for later issues (#28-#32).

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
stable, empty reserved regions: Run Activities, Observations, Artifacts,
Conversation, Narrative, Human Decisions. Below 100x30 only the
resize-required state is drawn (also enforced as a draw-time guard, not only
via resize events).

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

The console generates the Activation Request ID, Console Session ID, and a
high-entropy Bearer credential before activation; it sends `source_authority:
"operator_console"`.

## Committed terminal-frame fixtures

`frames/` holds readable plain-text captures rendered by Ratatui
`TestBackend` and treated as test fixtures (`operator-console/tests/render.rs`):

- `editing-100x30.txt` - Mission Intent editor
- `review-activation-100x30.txt` - activation review
- `run-dashboard-100x30.txt` - active-run dashboard with reserved regions
- `resize-required-80x24.txt` - below-minimum terminal

Regenerate after an intentional layout change:

```sh
UPDATE_FRAMES=1 cargo test --test render
```

Trailing whitespace is trimmed per line in both the renderer and the fixture
comparison so captures stay diff-friendly.
