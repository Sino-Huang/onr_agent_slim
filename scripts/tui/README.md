# Start the integrated stack from the Operator Console

The launcher builds/runs the release Rust console and bootstraps a Python Runtime Host if none is listening. The Host, not the console, launches and stops each run's Environment Stack. API 1.2 is required.

## Prerequisites

From this checkout:

```bash
cd /data/ccu/sukaih/ONR/onr_agent_slim
source /home/sukaih/miniconda3/etc/profile.d/conda.sh
conda activate onr
bash scripts/vllm/start_vllm.sh  # only if vLLM is not already serving
```

vLLM must list the configured model at `http://127.0.0.1:11411/v1/models`. The launcher uses Python from `onr`; Rust uses the checked-in toolchain. The sibling `onr_physical_runtime` checkout, scenario inputs and planner executables are required even for simulated missions. AirSim additionally requires Harbor, its settings, and the sibling `onr_solution` producer; YOLO requires `yolo_weight/best.pt` and `ultralytics`. Preflight reports the exact missing prerequisite.

## Launch and choose a mission

Use **tmux 3.3 or newer** for interactive TUI sessions and video diagnostics.
Older tmux cannot pass pixel graphics through, so `auto` falls back to
low-resolution halfblocks (node05's system `/usr/bin/tmux` is 2.7). Install a
modern tmux once, in its own conda environment so it does not shadow the
system tmux used by already-running sessions:

```bash
conda create -n tmux -c conda-forge --override-channels tmux
```

From a plain SSH shell (**not** inside another tmux or herdr session), create or
attach the console session:

```bash
./scripts/tui/start_tmux.sh            # session name defaults to onr-tui
```

The launcher uses `scripts/tui/tmux.conf` (passthrough and 24-bit color for
Kitty image placeholders), keeps its socket under `var/tmp`, and puts the modern
tmux first on the panes' PATH. Activate `onr` in the pane and run the console
launcher below. Use a Kitty-graphics-capable outer terminal such as Kitty or
Ghostty, and check that the startup footer reports Kitty pixel graphics.
To move an already running console, first use its **Ctrl+Q** detach (not `q` or
Ctrl+C), then relaunch in tmux under the same user and console state directory.
The Host-owned mission continues, and the console recovers its owned run.

```bash
./scripts/tui/start_operator_console.sh
# A slow cold Host import can be given a longer deadline:
./scripts/tui/start_operator_console.sh --host-ready-timeout 120
# Low-resolution compatibility fallback (not suitable for reading image labels):
./scripts/tui/start_operator_console.sh --image-protocol halfblocks
# Desktop notifications in addition to the bell (default: --notify bell):
./scripts/tui/start_operator_console.sh --notify desktop
```

Use a terminal at least 100×30; 140×40 or larger displays the overview image and side panels. The first release build takes time. Host bootstrap waits up to 60 seconds by default, shows progress, and sends stdout/stderr to `var/runtime-host/host.log`; on failure its last 20 lines are printed.

1. Select `mission1-harbor` for a simulated stack or `mission1-airsim` for the recorded Harbor engine scene. Other presets: `mission2`, `mission3`, `mission4`, `joint`, `joint24`, `joint34`. Enter on Preset opens a named picker with each preset's description, what it offers and its defaults (↑↓ move, Enter selects, Esc cancels).
2. Tab through stack fields; Left/Right changes a selection. Unsupported combinations are excluded and explained. AirSim on starts Harbor in both of its modes: with perception `off` (either Mission 1 preset) the AirSim Follower visualizes the simulated world model; with `ideal`/`yolo` (`mission1-airsim`) the scene clock synchronizes the world model to the engine and the front camera is annotated with the perception output. Perception requires AirSim. The **What this runs** panel under the stack selector follows every change with the Host catalog's descriptions: the mission goal and real LLM calls, which source is authoritative, what AirSim shows, and what Mission time does (coordinator-driven: it pauses while agents reason, so wall time runs ahead). Focusing Updates explains both update modes.
3. Wait for preflight to finish. Warnings do not prevent launch; failures do. Failures are listed first, then warnings, then passes, with counts in the panel title. Tab to Preflight, choose a check with ↑↓ and press Enter for its full detail and hint plus, where the Host supplies one, a read-only diagnostic command to copy (for example `ss -ltnp 'sport = :41451'` shows who holds a busy port); Esc returns, `r` re-runs preflight. The console never runs the command. Stop conflicting services rather than bypassing busy-port checks.
4. Edit the prefilled intent, or press F2 to choose the preset mission or a rejection prompt.
5. Alt+Enter (Ctrl+Enter where reported) reviews. Enter confirms once. Bare Enter in the intent editor inserts a newline.

Simulated Mission 1 prepares surveillance planning views during stack startup; this can take considerably longer than physical-runtime readiness. Real missions perform actual vLLM inference and can take tens of minutes. While the run waits, a banner under the phase stepper shows on every tab what it waits for: the running prep step, or the starting service with the readiness it still needs (for example `the frozen engine`), time waited against the readiness budget (`▲` after 80%), and later any live agent call (`Waiting for the LLM · hyper-agent · …`). When the previous run of the same preset measured that service's readiness, the banner adds it as history (`last run 0:27`); it is never an estimate for this run. The spinner moves while the console is alive; Host liveness stays in the header. Press `w` on any tab to inspect what the banner names: a starting service or running prep step opens the Stack tab with that row selected and its log following; a live LLM/tool call opens the Agents tab with that invocation selected (and following paused, so newer calls do not move the selection).

### Readable world maps and AirSim cameras

Use `--image-protocol auto` with a pixel-graphics-capable terminal. The startup
footer reports the selected renderer: `Kitty pixel graphics` (or Sixel/Iterm2),
or `halfblocks (LOW RESOLUTION)`. Halfblocks uses only two colors per character
cell; increasing the source PNG resolution cannot recover labels in that mode.
F4 gives the image more space, but does not change the graphics protocol.
Explicit protocols also query terminal cell dimensions to preserve aspect ratio.
A `cell 10x20` report means the terminal did not answer that query and the
library used its fallback geometry, typically in old tmux or a nested
multiplexer; fix the terminal chain rather than forcing a protocol.

For **herdr 0.8**, enable graphics in `~/.config/herdr/config.toml` on **both the
remote server and the desktop client** (merge into an existing section):

```toml
[experimental]
kitty_graphics = true
```

On the server, run `herdr --session 05_tui server reload-config` (substitute your
session name). On the desktop, use a Kitty-graphics-capable outer terminal such
as Kitty or Ghostty, detach herdr with its `Ctrl+B`, then `q` shortcut, and
reattach with `herdr --remote node05 --session 05_tui`. Detaching herdr does not
stop the console or mission. A client started with graphics disabled sends zero
cell pixel dimensions, so the console's `auto` mode falls back to halfblocks
even though herdr's inner terminal answers the Kitty capability query.

After reattaching, detach **the console** with `Ctrl+Q` and relaunch:

```bash
./scripts/tui/start_operator_console.sh --image-protocol auto
```

The console recovers the owned run; do not use `q` or Ctrl+C to restart a
console that owns an active mission, since those request cancellation.
If forcing `--image-protocol kitty`, graphics must still be enabled throughout
the terminal/multiplexer chain; forcing the protocol cannot enable herdr.
See [herdr 0.8 configuration](https://github.com/herdrdev/herdr/blob/v0.8.0/docs/next/website/src/content/docs/configuration.mdx#kitty-graphics).
Newer herdr versions have different defaults; consult their versioned settings.

On **5 World**, `s` cycles world → annotated front → raw front → third-person.
AirSim cameras appear only after the Host has captured them. World maps retain
their square aspect ratio; camera views retain their source aspect ratio (not
necessarily square). The world map shows a square of at least 512 m: presets
with 2 km world-model partitions (Mission 1 · Harbor, Mission 2) show the whole
partition; fine-grid presets (Mission 1 · AirSim live, Missions 3 and 4, Joint
2+4 and 3+4: 2 m cells, 128 m partitions) show a 512 m window that follows the
drone, at the same 2 m cells. That window is display-only; agents and planners
still work in their 128 m partitions. It jumps to re-centre once the drone is
128 m from its centre.


### Isolate video FPS and flicker

Use a separate tmux pane; these commands do not connect to the Runtime Host,
start AirSim, or change an owned mission. Activate the `onr` environment first.
The launcher uses `ffmpeg` from PATH or the installed `imageio_ffmpeg` binary.

```bash
# Moving test pattern: direct ratatui-image, no console worker.
python scripts/tui/test_video.py --mode direct --fps 15 --seconds 10

# Same input through the console's asynchronous WorldMedia pipeline.
python scripts/tui/test_video.py --mode console --fps 15 --seconds 10

# Reproduce the console's normal 2-Hz image-update cadence.
python scripts/tui/test_video.py --mode console --fps 2 --seconds 10

# Play the first ten seconds of a real video, without audio.
python scripts/tui/test_video.py /path/to/video.mp4 --mode direct --fps 15 --width 640
```

`q`, Escape or Ctrl+C exits this standalone player, not the mission. The default
clip is ten seconds at 640 pixels wide. `--width` preserves source aspect ratio;
images fit the pane without upscaling. Try `--width 960` or `--fps 30` separately
to identify output-bandwidth/encoding limits. `--image-protocol` accepts the same
renderers as the console except `off`; confirm the header reports Kitty or
another pixel protocol rather than Halfblocks.

The launcher decodes the bounded clip into temporary JPEGs under `var/tmp`
before playback, then removes them on exit. The Rust example
`operator-console/examples/video.rs` reads one frame at a time and skips late
source frames instead of accumulating playback lag. Direct mode decodes,
resizes, and encodes synchronously in the widget. Console mode submits JPEGs
to the existing worker, bypassing only Host fetching and its 500-ms throttle.
Both drive drawing at up to 60 Hz and retain the same source schedule.

Each run writes a JSON report under `var/tui_test/` (override with `--report`):
submitted FPS, skipped source frames, draw/preparation p95, and empty image
draws/transitions after the first rendered image. Submitted FPS is **not**
displayed FPS, especially for the asynchronous pipeline. Empty draws count
missing image-widget content in Ratatui's buffer, not black pixels in the video
or flashes introduced later by the desktop compositor.

The old threaded handoff removed the drawable image while encoding its
replacement. A six-second 640-pixel Kitty/herdr run recorded 89 empty-image
transitions at 15 FPS and 11 transitions across 12 source frames at 2 FPS.
The renderer now retains the completed image independently of pending work and
replaces it only after encoding succeeds. Decode and encode requests remain
bounded/latest-wins; source changes intentionally clear the previous image
rather than showing a world map under a camera label. The production fetch
limit remains 2 Hz.

Post-fix six-second runs in a detached 160×45 tmux 2.7 pane recorded **zero
empty draws/transitions** at 2, 15 and 30 FPS with the Kitty encoder, at 15 FPS
with Halfblocks, and on a recorded video at 15 FPS with Kitty. Reports are under
`var/tui_test/flicker-fixed-*.json`. The detached Kitty runs used forced protocol
selection and fallback 10×20 cell dimensions; they verify application output,
not desktop passthrough or compositor behavior. Inspect playback in your
attached tmux session to check that final display path.

### Attention signals while you look away

The terminal window title always tracks the run: `ONR ● running 00:12:04 · Planning`, then `ONR ✔ succeeded` or `ONR ✖ stack_failed` (the Host's terminal classification). It is set with `OSC 2` and restored on exit (xterm title stack, which tmux also implements). Four events also alert the operator: the stack is ready and planning starts, a stack failure, `awaiting_human_decision`, and the terminal run status. `--notify` chooses what an event does:

| `--notify` | Title | Bell (BEL) | Desktop notification |
| --- | --- | --- | --- |
| `none` | tracks the run | no | no |
| `bell` (default) | tracks the run | once per batch of events | no |
| `desktop` | tracks the run | once per batch of events | one per event: `OSC 777 ; notify` under VTE terminals (`VTE_VERSION`) and urxvt, otherwise `OSC 9` (iTerm2, WezTerm, Ghostty and others); terminals without either ignore it |

Each event fires once per run (run id + event): a second Human Decision Request in the same run, or a status that flaps back, stays silent. A stack failure that is first seen as the terminal status is one event (`✖ stack_failed`). Events are edge-triggered from what this console observes: a console restarted onto a recovered run treats everything already true at its first observation (an open decision request, a ready stack, a terminal status) as history and stays silent; only later transitions alert.

Inside tmux the title becomes the pane title (`#{pane_title}`; `set -g set-titles on` forwards it to the outer terminal, `set -g automatic-rename-format '#{pane_title}'` shows it as the window name), and a bell in a background window sets `#{window_bell_flag}` (`monitor-bell`, default on). tmux drops desktop OSC sequences, so in tmux `desktop` wraps them in DCS passthrough; tmux 3.3+ forwards them only with `set -g allow-passthrough on`, and the outer terminal must still support OSC 9/777.

## Run keys

| Key | Action |
| --- | --- |
| `1`–`7`, Tab / Shift+Tab | Overview, Progress, Agents, Belief & Context, World, Stack, Artifacts |
| `?`/F1, Escape | Help; close overlay |
| `w` | Inspect the current wait (the target the banner names at keypress): Stack row and its log (including the service being stopped during teardown), or the Agents invocation |
| Up/Down or `j`/`k` | Select entries; the Agents and Artifacts lists scroll to keep the selection visible, with a `37/142` position and ▲/▼ marks for hidden rows |
| Progress Left/Right | Collapse/expand |
| Progress `f`, `i`, `/`, `n`/`N` | Follow newest; minimum importance; search; next/previous match |
| World `s`, `p` | Cycle world → annotated front → front → third-person camera, skipping sources without a frame; pause display fetching |
| Stack | Prep-step and service selection with auto-following log tail; completed prep steps show measured durations (`✔ airsim-fixture done 0:03`); during teardown a `◑ stopping` service shows `0:06 / grace 0:30` and a stopped one `graceful` or `forced` |
| Agents Home/End, PageUp/PageDown, `f` | First/last invocation (stays paused; only `f` resumes following); scroll detail; resume following |
| Artifacts Home/End, Enter | First/last artifact; inspect the selected artifact |
| Inspector Up/Down, PageUp/PageDown, Home/End | Scroll the wrapped lines of the current page by line or screen; first/last line (status: `line 12/83 · bytes 4096-8191 of 51234`) |
| Inspector Right/`n`, Left/`p`, Escape | Next/previous 4096-byte page; close |
| `c`, then Enter | Cancel an owned run, leaving the console open |
| `q` or Ctrl+C | Managed exit; an active owned run is cancelled through confirmation |
| Ctrl+Q | Detach: quit without cancelling; the Host, run and owner session survive for recovery |
| Rejection card `e`, Enter | After cleanup, edit a new intent with the same stack; view rejected-run details |
| Failure card `l`, `2`, `y`, Enter, `e` | Log tail in the inspector (failed service's log, else the worker log) · Progress · copy run id + Run Root (OSC 52) · dismiss (`l`/`y` keep working) · new intent once cleanup is confirmed |
| `x` (finished run) | Export the receipt: the Host writes `<run root>/mission-run-receipt.json` and the panel shows the path; pressing again overwrites the same file. Owner console only |
| F3 (Launch or Run) | Run history: ↑↓/PgUp/PgDn/Home/End select, `s` status filter, `p` preset filter, `r` reload, Enter open read-only, Escape close |
| F4 (Run) | Presentation layout over the current tab: `v` next evidence card (milestone · belief entity · agent invocation), `p` freeze/resume, `s` World source, F4 back to the tab (a tab key, `w` or the failure card's `l` also leave it) |
| Escape (historical run) | Back to the current run, or to Launch when history was opened there |

Do not modify global transport/storage paths to run a demo. The Host materializes configs under `var/runtime-host/runs/<mission_run_id>/`, including transport, agent storage, planner/environment artifacts, service logs and results. The Stack tab exposes readiness (start-to-ready time, or time waited against the budget and the pending readiness probe, plus the previous same-preset run's readiness as `Last run:` history) and logs. Prep steps are listed in plan position (`prepare` steps above the services, `post_ready` steps below) with their state and measured duration; their logs (`services/<step>.log`) are served as `service-log-<step>` Artifacts. The step history is read from `stack-status.json`, so it survives a Host restart. The final world frame (`world-frames/latest.png`) is retained after the stack stops.

AirSim frames persist as `world-frames/camera_front.jpg`, `camera_front_annotated.jpg` and `camera_third_person.jpg` with `.json` metadata; a camera source becomes selectable only after its first persisted image, and unchanged pixels do not advance it. With perception off, the `airsim-visualizer` service (Stack tab) steps the frozen scene to the world model's Mission time, places the drone at the world-model pose and draws ideal segmentation boxes; the World tab's `AirSim:` row shows its lag and measured ship phase error. With perception on, the Run Worker captures the `front_center_custom` and `third_person_demo` Scene cameras read-only at about 2 Hz and draws the perception module's boxes only on the frame they belong to. Status and failure reasons are in `world-frames/airsim.json` and `camera-capture.json` (`capturing`, `unavailable`, `stopped`).

The rejection reason can appear while summaries are flushing and services are stopping; re-launch remains disabled until cleanup finishes. Progress search captures ordinary typing until Enter/Escape; Ctrl+C remains managed exit. Terminal evidence refresh waits at 2 s cadence for the final narrative attempt, drains the synchronized final pages, then stops polling.

The AI Run Narrative carries a header with its age and coverage: `AI narrative · 42 s old · through record #763 · 18 newer` (age ticks with wall time between polls; `18 newer` counts the operational records newer than the last one the narrative covers, shown only for a v1.5 Host). It reads `coverage pending` before the first narrative, adds `final` for the run's final attempt, and reads `unavailable` when an attempt failed. The Overview shows at most 4 narrative rows with `2 Progress: full narrative`; on Progress the header sits above the tree and selecting the root shows the full text.

On activation or recovery, all nine evidence sections hydrate once. Recent-first pages are backfilled with `before`, then forward deltas replay from the initial watermark; older pages cannot undo newer summary reparenting. Progress orders nodes by the numeric sequence in their IDs, so recovered history stays chronological. Local queue backpressure releases poll claims for retry instead of marking the Host offline.

Cancellation requests can take up to 9 s while the owned stack is reaped; the console uses a 12 s cancellation HTTP deadline and a 15 s managed wait. If that wait expires, the console exits and stops a Host it bootstrapped; the next Host start finishes reaping. A second Ctrl+C does not silently abandon an owned run; Ctrl+Q is the explicit detach override.

Teardown is visible while it happens (`frames/run-teardown-*.txt`). Once you confirm, the dialog closes and the tabs stay navigable while the Host stops the run. The waiting banner reads `Cancellation requested · waiting for the Run Worker to begin teardown`, then names each service as the Stack Supervisor stops it in reverse start order: `⠹ Stopping airsim-engine (3/3) · 0:06 / grace 0:30`. The grace is the supervisor's SIGTERM-to-SIGKILL period for that service (`▲` after 80%); the Host may reap the whole process tree sooner (about 8 s after the request), and services stopped that way are recorded `forced`. A cancelled run ends `stopped` for the closed loop, not `failed`. The terminal Overview then shows the teardown receipt (`frames/run-teardown-receipt-*.txt`):

```text
 Teardown:  worker stopped · took 0:10
 Services:  3 stopped · 2 graceful, 1 forced
 Harbor:    config restored (guardian reported)
```

`graceful` means the service exited within its grace period; `forced` means SIGKILL after it, or that the Host reaped it with the Run Worker's process tree before the supervisor saw it exit (even if it was already exiting: the Host cannot tell, so it never claims graceful). `Harbor:` says `config restored` only when `live_engine` logged its restoration (it compares the restored `environment.json`/`object_ids.txt` bytes with the originals and raises otherwise) or its guardian logged one after the engine was killed; otherwise `restoration unknown (no report)`, which may change to restored once the guardian finishes. Runs without AirSim read `not applicable (no AirSim engine)`. To check by hand, compare `md5sum /data/ccu/sukaih/ONR/onr_env/Linux/Harbor5_6/EnvironmentConfigFiles/{environment.json,object_ids.txt}` with the copies in `<run_root>/engine/engine-config/*.before.*`.

A finished run's Mission Run panel is its receipt (`frames/run-receipt-*.txt`). Each row has its own authority, so a green lifecycle never reads as mission success:

```text
 Lifecycle: succeeded
 Finished:  2026-08-24T12:05:15Z · 5:12 wall
 Final:     FSM mission-complete · plan r4 · t=120.0 s
 Audit:     FAIL · 1 failure: evidence_replan_not_observed
 Teardown:  worker stopped · took 0:04
 Services:  4 stopped · all graceful
 Harbor:    config restored (engine reported)
 Export:    x writes mission-run-receipt.json
```

`Lifecycle` is what the Run Worker did, not a verdict. `Final` is the last FSM state and plan revision from the latest FSM status record and the last Mission time from the environment evidence. `Audit` reads `PASS`/`FAIL` only from `live-acceptance.json`, which `scripts/audit_live_demo.py` writes into the Run Root; without that file it says `not recorded (no audit artifact)`. `x` asks the Host to write the receipt (these facts plus the teardown, the per-service stop modes and Run Root relative references to the closed-loop result, audit, stack files, observations, narrative, final frame and logs) to `<run root>/mission-run-receipt.json`; the console never writes into the Run Root itself. The panel then shows `✔ wrote` (or `✔ overwrote`) and the Host's absolute path. Each export re-reads the audit and then refreshes the Overview once, so after running the audit press `x` again: both the file and the `Audit` row carry its verdict. After termination the header shows `last <maneuver>` instead of `▶`, and Context labels the FSM state, maneuver and transition intent `last observed`.

F3 opens the run history (`frames/run-history-*.txt`) from Launch or the Run screen: the Host's Mission Runs, newest first, with start time (UTC), preset and toggles, status and classification, wall duration and run id, plus `● current`, `◆ owned` and `✖ no root` tags. The selected row's detail line adds the update ownership, simulation limit and whether the Run Root is on disk. The list never carries a Mission Intent. `s` cycles all/succeeded/failed/cancelled/active and `p` cycles the presets of the loaded rows; moving onto the last loaded row fetches the next older page (`GET /api/v1/mission-runs?limit=50&before=<last run id>`). Enter on a `✖ no root` row explains that the Run Root is missing instead of opening it.

Enter opens the run read-only in the normal seven tabs (`frames/run-historical-overview-*.txt`): the header starts with a `HISTORICAL` badge, the receipt's `Export:` row shows only whether the run was exported, and `c`, `q`, `e` and `x` are refused with a hint. Escape returns to the current run (or Launch). The owner session file and the owned run id are never touched, and the current run keeps being polled (`/current`, Overview and Stack) while history is shown, so its terminal, stack-failure and Human Decision alerts and the window title still follow it; nothing about a historical run reaches attention. Ctrl+C first leaves the history, then acts on the current run as usual. History needs a v1.5 Host; an older one is named in the overlay.

The Host rebuilds a run it has not served since it started from the run's files on disk. For a long run this first read can take longer than the console's 5 s request limit (17 s for a 28-minute run). Until the run's Overview arrives the header says `loading from disk N s` and the footer `Loading from disk · N s · first open rebuilds the Host's view` (`frames/run-historical-loading-*.txt`), adding `· K read timed out, retrying` while the Host is still rebuilding. Any other error still shows as `Host <section> poll failed`. Notices raised while a historical run is shown stay with it: Escape drops them, and the current run's own notice comes back.

F4 shows the presentation layout (`frames/run-presentation-*.txt`) for a presenter: the selected World source scaled up to fill the left, with its AirSim disclosures under it (`AirSim:` follower or scene clock with lag, `Overlay:`, and on the annotated front camera the Host's `Boxes:` provenance sentence), one evidence card on the right, and above them the mission title (the preset's catalog title), `Wall 12:00:43 UTC · run 00:00:40 │ Mission t=143.5 s`, the phase stepper and the waiting banner. The card shows the existing selections: the milestone is the current phase step plus the Progress selection (following the newest record unless you pinned one), then the Belief/Context entity, then the Agents invocation; `v` cycles them and nothing rotates on its own. While shown, the layout's sections (Overview, Progress, Beliefs, Agents, World) are polled instead of the hidden tab's. F4 returns to the tab that was selected; a tab key or `w` goes to that tab instead.

`p` freezes the display: the frame, card, phase stepper and clocks hold (the waiting banner hides), and the clock row reads `FROZEN AT 12:00:43 UTC · t=143.5 s · run 00:00:40 · display held; polling continues` (`frames/run-presentation-frozen-*.txt`). Freezing is display-only: every section keeps polling, so `p` again shows the latest evidence at once. The frame is held by the World tab's `p` pause (no frame fetch while frozen, and a frame reply already in flight is dropped); resuming restores whatever World `p` state the freeze found and fetches the newest frame. The source cannot change while frozen. The title row (lifecycle status, Host liveness) and the alert rows are always live, so these break through a frozen view (`frames/run-presentation-alert-*.txt`): `✖ RUN FAILED · <classification> · <detail>` for a failed run or a failed stack step (with the failure card on top, Enter dismisses the card but not the row), `◆ HUMAN DECISION REQUIRED` for `awaiting_human_decision`, and `✖ HOST OFFLINE` / `▲ HOST STALE` from Host liveness. A historical run (F3) can be presented read-only; its alert rows follow the current run, which keeps being polled.

## Recovery and diagnostics

- The bootstrapped Host runs in its own process group, so killing only the console with SIGKILL, or closing its terminal, leaves the Host and run running and does not erase the persisted Console Session (`~/.local/state/onr/operator-console/session.json`). Relaunch against the same Host to recover the owned run.
- Ctrl+Q has the same effect intentionally. A console relaunched against an already-running Host never stops that Host on exit, so the Host keeps running until it is stopped separately.
- If that persisted run is absent or no longer the Host's current run, recovery returns to Launch without inheriting ownership of the newer run. The previous session file remains until a normal activation replaces it.
- Restarting a killed Host records a Host-Interrupted Failure (`cancelled_by_owner` if it had already accepted cancellation) and reaps the orphaned worker and stack processes it can prove it owns (recorded PID and start time, or the exact run-ownership token). The Harbor restoration guardian is left to restore `environment.json`.
- A run that ends `stack_failed`, `worker_failed` or `host_interrupted` opens the failure card (double red border, `✖ RUN FAILED · <classification>`; `frames/run-failure-*.txt`). It shows only Host data: the stage, the sanitized reason, the failed service, the last completed phase step, the cleanup state from the stack status, the run id and Run Root, and the log's last recorded line labelled as context, not the cause. Press `l` to read the log at its tail in the Artifact inspector: the service's `service-log-<service>` for a stack failure (the Host names it in `terminal_detail.log_artifact_id`), otherwise `worker-log` with the traceback; ↑/PgUp scroll back, ← steps back a byte page, Esc returns to the card. `2` opens Progress. `y` writes an OSC 52 clipboard request with `<run id> <run root>`: the terminal decides whether to accept it, and inside tmux it needs `set -g set-clipboard on` (tmux stores it as a paste buffer) or, on tmux 3.3+, `allow-passthrough on` for the outer terminal; both values are on the card regardless. `e` waits until no stack service is still recorded as starting or ready. An older Host (API < 1.5) names no log and no Run Root: `l` opens the failed service's log from the stack status (or the worker log) and says so. For reference, these logs live at `<run_root>/worker.log` and `<run_root>/services/<service>.log`.
- `NO_COLOR=1` disables semantic colours; importance/state glyphs remain.
- `ONR_CONSOLE_IMAGE_PROTOCOL` provides the same override as `--image-protocol`; `off` disables images.

See [the illustrated user guide](../../docs/operator-console-guide/index.html), [the boss-demo runbook](../../docs/operator-console-demo.md), [the design/contracts](../../docs/design/operator-console/README.md) and [ADR 0016](../../docs/adr/0016-airsim-follower-and-perception-annotations.md).
