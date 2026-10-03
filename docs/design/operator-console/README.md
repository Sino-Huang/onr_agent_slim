# Operator Console v2

The Rust ratatui console is a loopback HTTP client of the Python Runtime Host, not a process launcher. The Host launches one per-run Environment Stack through the Run Worker (ADR [0015](../../adr/0015-runtime-host-per-run-environment-stack.md)); environment state stays environment-authoritative. The console requires Runtime Host API **1.2 or newer in major version 1**; the Host serves **1.3**, whose AirSim additions a 1.2 Host simply lacks. There is no legacy dashboard fallback. The Host keeps its v1.0 endpoints for other clients.

## States and ownership

```text
Connecting → Launch / Editing → ReviewActivation → Submitting → Run
                  ↑                    │                       │
                  └──── Escape ────────┘                       │
                  └──── e (rejected or finished run) ──────────┘
```

- A Console Session is persisted under `~/.local/state/onr/operator-console/session.json` (respecting the platform state-directory override). Its credential authorizes intent readback, activation and cancellation. Observers cannot cancel another session's run.
- Review assigns an Activation Request ID. A retry of the same intent **and stack selection** reuses that identity; editing either creates a new activation identity.
- The Host permits one nonterminal Mission Run. Cancellation stops only processes launched for that run, never an independently started environment.
- `q` and Ctrl+C use managed exit during an active owned run. The cancel confirmation is not bypassed by Ctrl+C. While the Host is stale or offline, Ctrl+C cannot cancel and says so.
- Ctrl+Q is the explicit detach: the console quits without cancelling, releases a Host it bootstrapped without stopping it, and keeps the persisted owner credential, so a relaunch recovers the run.
- A bootstrapped Host runs in its own process group with stdin closed and output in `var/runtime-host/host.log`, so a console SIGKILL or terminal hangup leaves the Host and run alive; relaunch recovers the persisted session. A relaunched console finds that Host already healthy and never stops it on exit.
- If a managed exit's 15 s cancellation wait expires, the console quits and stops a Host it bootstrapped. The next Host start finishes reaping and records `cancelled_by_owner` if the Host had accepted the cancellation, otherwise a Host-Interrupted Failure.
- A Host killed unexpectedly leaves the Run Worker (its own session) orphaned. On restart, the Host proves ownership by the persisted worker PID plus process start time or by the exact run-ownership token, reaps the worker group and token-carrying detached groups (never the Harbor restoration guardian), and records a Host-Interrupted Failure, or `cancelled_by_owner` when cancellation was already requested. Zombie-only process groups are not live.
- An absent or superseded recovered owner run returns to actionable Launch. It never grants cancellation authority over a different run or strands the console in Connecting.
- Stale/offline liveness and terminal restoration remain separate from run status. See [terminal-lifecycle.md](terminal-lifecycle.md).

## Launch

The Launch screen presents preset, AirSim, perception (`off`, `ideal`, `yolo`), update ownership, simulation limit, Mission Intent and live Preflight Checks. The intent starts with the preset's actual default mission text. Changing a stack selection schedules a debounced preflight refresh (300 ms); launch is disabled while checks are pending or failed.

Preset support is authoritative: perception requires AirSim. AirSim has two mechanisms (ADR [0016](../../adr/0016-airsim-follower-and-perception-annotations.md)): with perception off the AirSim Follower visualizes the simulated world model (`mission1-harbor` and `mission1-airsim`); with `ideal`/`yolo` the scene clock synchronizes the world model to the recorded engine scene (`mission1-airsim` only). Unsupported combinations are excluded and the preset's `unsupported_reason` explains why. `joint` requires a configured Mission 1 report instance; preflight reports this prerequisite rather than inventing an input.

F2 opens the demo-prompt picker: the preset mission, `buy me a coffee`, `What is the capital of France?`, and gibberish. Alt+Enter (Ctrl+Enter where reported) reviews; Enter in review launches. Bare Enter in the intent editor adds a newline. Tab selects a field; Left/Right changes a stack field.

## Run surface

The header combines Mission Run lifecycle, mission time, FSM state, Active Maneuver and stack health. The phase stepper uses code-owned evidence mapping: Stack → Intent → Planning → Statechart → Executing → Done. A rejected mission fails the Intent step; later steps remain pending.

| Tab | Surface |
| --- | --- |
| 1 Overview | Run Narrative, progress preview, world preview, compact belief/context and authoritative run state |
| 2 Progress | Run Narrative → windowed Mission Log Summaries → raw operational-log records; detail pane for the selected node |
| 3 Agents | Hyper/Maneuver invocation list, tool calls and results; Recorded Debug Reasoning explicitly non-authoritative |
| 4 Belief & Context | Scrollable belief table with prior/previous deltas; Up/Down or `j`/`k` selects an entity by stable ID, whose gauge, deltas and revision sparkline stay visible below the table; Mission Snapshot source health, FSM Transition Candidates, Active Maneuver and latest transition/replan evidence |
| 5 World | Inline world-model frame, annotated AirSim front camera, raw front or third-person camera, with viewer state, AirSim mechanism/lag and overlay provenance; source cycling and pause |
| 6 Stack | Service lifecycle/readiness, importance, and auto-following allowlisted service-log tail |
| 7 Artifacts | Published and allowlisted planner artifacts, with the existing paged content inspector |

Human Decision status remains read-only. No decision-submission controls are added.

### Progress and importance

The narrative is the tree root. Each summary is a parent over its `input_start_sequence`–`input_end_sequence` records. Uncovered records sit under **Live — not yet summarized**. When a summary arrives, the Host emits updated nodes with the same IDs and new parents; the console merges in place. The narrative and summaries carry an **AI** badge and are non-authoritative.

Initial activation and recovery hydrate all nine sections, drain older pages using `before_cursor`, then replay forward deltas from the initial watermark. Backward pages add only unseen IDs, so historical snapshots cannot undo newer reparenting or paused selection. Terminal completion waits for both history and forward pages. Progress nodes are ordered by their semantic ID's kind and numeric sequence (`log:9` before `log:10`), so newest-first recovered pages still navigate chronologically.

Importance is mapping version 1, assigned by Python code, not by the model. A summary takes the highest importance of its covered records.

| Level | Glyph | Colour |
| --- | --- | --- |
| critical | ✖ | red, bold |
| warning | ▲ | yellow |
| notable | ● | green |
| routine | · | default |
| debug | ∙ | dark grey, hidden by default |

Component badges: HYP, MAN, CC, FSM, BEL, ENV, PER, STK. Glyphs remain visible with `NO_COLOR`.

### Belief and context truthfulness

Reporting-reliability entities include mean, honest probability, credible interval, variance, outcome counts, deltas and bounded revision history. Mission 2–4 report explicitly that no Bayesian belief service is composed for that mission mode. Mission 4's environment-owned object-search belief is not presented as agent-storage evidence.

Mission Snapshot source freshness is a boolean per source, not an invented age. FSM candidates are the actual enabled-candidate list. A maneuver gauge is shown only when progress provides a computable fraction (for example cleared search cells); distance-only navigation remains a distance readout. Pending perception count is omitted because it is not persisted in transport.

### Layout and keys

Compact at 100×30 (image hidden), standard at 140×40, wide at ≥180×50. Panes grow with the terminal; there is no fixed 100×30 drawing canvas.

- `1`–`7`, Tab/Shift+Tab: switch run tabs.
- `?`: help; Escape closes overlays.
- Up/Down or `j`/`k`: select; Left/Right: fold/unfold the progress tree.
- Progress: `f` follow newest, `i` minimum importance, `/` search, `n`/`N` next/previous match. Search editing captures letters, digits and Tab until Enter/Escape; Ctrl+C remains managed exit.
- World: `s` cycles world → annotated front → front → third-person, skipping sources without a frame; `p` pauses display fetching.
- Agents: `f` follow; PageUp/PageDown scroll detail.
- Artifacts: Enter inspect; Right/`n` next 4096-byte page, Left/`p` previous page; Escape close.
- `c`: owner-scoped cancellation; `q`/Ctrl+C: managed exit; Ctrl+Q: detach without cancelling.
- Rejection card: `e` edit a new intent with the same preset/toggles once cleanup ends; Enter inspect the rejected run. The card can appear while final summaries and teardown are still in progress.

## HTTP and concurrency

Control, evidence and media workers use std threads and bounded/coalesced request queues; ureq HTTP never runs in drawing. On terminal status, all sections are drained once; Overview then refreshes at 2 s while the terminal narrative attempt finishes (`available` or `unavailable`). A synchronized final section wave drains every remaining page, then polling stops. Media is fetched only while visible, at roughly 2 Hz; decoding and protocol resize/encoding are off the UI thread. Cancellation has a 12 s HTTP deadline and a 15 s managed wait because owned-stack teardown can take up to 9 s; ordinary requests keep their 5 s deadline.

Additional v1.2 surfaces:

- `GET /api/v1/stack/presets`
- `GET /api/v1/stack/preflight?preset_id=…&airsim=…&perception=…&update_ownership=…`
- Activation's optional `stack` object (`preset_id`, `airsim`, `perception`, `update_ownership`, `simulation_limit_seconds`), included in request identity.
- `/current` adds resolved `stack` and nullable `terminal_detail`: mission rejection reason, sanitized worker error (≤500 characters), or service readiness/crash failure. A rejection reason is published immediately while status stays `running` during summary flush and teardown; new activation remains blocked until the final `failed` state. Full traceback stays in `worker.log`.
- `operator-view?section=overview|progress|agents|beliefs|context|world|stack|artifacts` (the Host retains `environment` too). Common metadata and opaque cursors remain. Unknown/repeated query or body fields return `422 invalid_request`.
- `GET /api/v1/mission-runs/{id}/world-frame?source=world|camera_front|camera_third_person`: binary image, `ETag`, `X-Frame-Sequence`, `X-Mission-Time`; conditional requests return 304; missing images return `404 frame_unavailable`.
- The `world` section lists only sources that have a frame. AirSim runs add an optional `camera_capture` object (`state`: `capturing`, `unavailable` or `stopped`; nullable `reason`; configured `cameras`). A terminal run never reports `capturing`.

Additional v1.3 surfaces ([contract/v1.3](contract/v1.3)):

- `health` reports `api_version` 1.3.
- `/stack/presets`: `mission1-harbor` supports AirSim `[false, true]` with perception `off`; `mission1-airsim` supports perception `off`, `ideal` and `yolo`.
- Frame source `camera_front_annotated` (`image/jpeg`, same route and headers), listed in display order world → annotated front → front → third-person.
- Optional `world.airsim`: `mode` (`world_model_follower` | `scene_clock`), `perception`, `state` (`capturing` | `unavailable` | `stopped`; terminal runs never report `capturing`), nullable `reason`, `frame_mission_time_seconds`, `world_mission_time_seconds`, `lag_seconds`, `ship_phase_error_seconds`, and nullable `annotation` (`kind` `ideal_segmentation` | `perception_ideal` | `perception_yolo`, `disclosure`, `match` `exact` | `none`, `objects`, `perception_mission_time_seconds`).
- Run-scoped `services/*.log` and `worker.log` use the existing artifact content route with classification `service_log`, arbitrary byte offsets and no planner 1 MiB cap. They are available only through the loopback operator surface.

Contract examples live in [contract/v1.2](contract/v1.2) and [contract/v1.3](contract/v1.3); Rust serialization round-trips both. Python owns importance, phase and run status. Narrative generation is asynchronous, on a 30 s cadence plus a terminal attempt; requests only read stored narrative records. Evidence ingestion is incremental and outside the Host's global state lock.

Projection caches are per Mission Run. The debug-artifact catalog reparses only discovered files whose metadata identity changed; the planner inventory lstats without following symlinks or reading content; environment evidence folds the latest FSM status, Maneuver Feedback and last ten belief events; JSON evidence reads are size-capped and LRU-bounded. The Host keeps this state only for the current run, runs awaiting a terminal narrative, and one requested historical run; it prunes at lifecycle boundaries or a cache miss, never on steady polls. Pruned runs reload from their Run Root.

## Images

`ratatui-image` protocol selection: `--image-protocol auto|kitty|sixel|iterm2|halfblocks|off` or `ONR_CONSOLE_IMAGE_PROTOCOL`. Auto queries the terminal after initialization; halfblocks is the portable fallback inside tmux/herdr. Finished runs retain their cached final world frame under `world-frames/latest.png`; the Run Worker captures it after stopping camera capture and before stopping the stack.

For draw-latency measurements, set `ONR_CONSOLE_DRAW_LOG` to a file under repository `var/tmp`; each frame records elapsed drawing time in microseconds. This is opt-in diagnostic output, not mission evidence.

### AirSim cameras

Every AirSim run starts Harbor. Camera frames are persisted atomically as `world-frames/{camera_front,camera_front_annotated,camera_third_person}.jpg` with `.json` metadata (source, sequence, SHA-256 ETag, Mission time, camera and vehicle names, AirSim `sensor_timestamp_ns`; annotated frames add the `annotation` status and their `boxes`). State transitions go to `world-frames/camera-capture.json`; the `world.airsim` status is `world-frames/airsim.json`. Identical pixels, such as a paused scene, do not create a new generation. Live and terminal runs serve these files without network access; a camera image the viewer itself provides takes precedence while the run is live.

**Perception off — AirSim Follower.** The `airsim-visualizer` service owns the frozen engine's clock (ADR 0016). For each new world-model Mission time it steps the scene to `lead_in + mission_time`, re-measures ship playback against the fixture trajectories, teleports the drone to the world-model pose, flushes one 0.1 s render step and captures front Scene + Segmentation and third-person Scene at 960×540. The annotated frame draws ideal instance-segmentation boxes labelled `ship N` and the disclosure "AirSim follows the world model. Boxes are ideal instance segmentation, not agent perception." `lag_seconds` is how far the newest world-model state is ahead of the shown frame; `ship_phase_error_seconds` is the largest measured ship playback error in that frame. Visualization never gates the world model.

**Perception ideal/yolo — scene clock.** The Run Worker starts a read-only `CameraCapture` once every stack service and post-ready step has finished. A dedicated daemon thread calls only `simGetImages` at a nominal 2 Hz for vehicle `SimpleFlight`: `front_center_custom` → `camera_front` and `third_person_demo` → `camera_third_person` (`engine.airsim_camera` and `engine.airsim_third_person_camera` in `conf/stack_presets.yaml`), plus the front Segmentation image for ideal perception. It never enables API control, arms, moves cameras, pauses or writes the clock. The annotated frame shows the perception module's output for that frame only: YOLO boxes from `perception_audit.jsonl` matched by AirSim image timestamp (fallback: Mission time within the 0.25 s scene-clock sensor lag), or the ships ideal perception reported, outlined from the frame's segmentation. Otherwise YOLO frames read "no perception sample for this frame"; ideal frames read "ideal perception reported no ships for this frame", because ideal perception publishes only the ships it saw and an empty sample cannot be told apart from a frame between samples. Cameras absent from the AirSim settings report `camera_not_configured`. Teardown stops capture (bounded by one in-flight SDK request) before the final world frame and the stack stop; nothing is persisted after stop.

## Validation and demo

See [the demo runbook](../../operator-console-demo.md). The verification matrix includes production Host/Rust interoperability, contract round-trips, responsive render snapshots, incremental reparenting, supervisor readiness/crash/hang, preflight, belief/context fixtures and conditional frame proxy behavior. Live acceptance includes coffee rejection, a simulated mission with three summary windows and a refreshed narrative, AirSim+YOLO audit, cancellation/restoration, console-session recovery and herdr halfblocks rendering. Performance is measured in live runs, never asserted by wall-clock CI tests.
