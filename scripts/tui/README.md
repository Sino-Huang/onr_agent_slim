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

```bash
./scripts/tui/start_operator_console.sh
# A slow cold Host import can be given a longer deadline:
./scripts/tui/start_operator_console.sh --host-ready-timeout 120
# Portable graphics inside tmux/herdr:
./scripts/tui/start_operator_console.sh --image-protocol halfblocks
```

Use a terminal at least 100×30; 140×40 or larger displays the overview image and side panels. The first release build takes time. Host bootstrap waits up to 60 seconds by default, shows progress, and sends stdout/stderr to `var/runtime-host/host.log`; on failure its last 20 lines are printed.

1. Select `mission1-harbor` for a simulated stack or `mission1-airsim` for the recorded Harbor engine scene. Other presets: `mission2`, `mission3`, `mission4`, `joint`, `joint24`, `joint34`.
2. Tab through stack fields; Left/Right changes a selection. Unsupported combinations are excluded and explained. AirSim on starts Harbor in both of its modes: with perception `off` (either Mission 1 preset) the AirSim Follower visualizes the simulated world model; with `ideal`/`yolo` (`mission1-airsim`) the scene clock synchronizes the world model to the engine and the front camera is annotated with the perception output. Perception requires AirSim.
3. Wait for preflight to finish. Warnings do not prevent launch; failures do. Stop conflicting services rather than bypassing busy-port checks.
4. Edit the prefilled intent, or press F2 to choose the preset mission or a rejection prompt.
5. Alt+Enter (Ctrl+Enter where reported) reviews. Enter confirms once. Bare Enter in the intent editor inserts a newline.

Simulated Mission 1 prepares surveillance planning views during stack startup; this can take considerably longer than physical-runtime readiness. Real missions perform actual vLLM inference and can take tens of minutes.

## Run keys

| Key | Action |
| --- | --- |
| `1`–`7`, Tab / Shift+Tab | Overview, Progress, Agents, Belief & Context, World, Stack, Artifacts |
| `?`, Escape | Help; close overlay |
| Up/Down or `j`/`k` | Select entries |
| Progress Left/Right | Collapse/expand |
| Progress `f`, `i`, `/`, `n`/`N` | Follow newest; minimum importance; search; next/previous match |
| World `s`, `p` | Cycle world → annotated front → front → third-person camera, skipping sources without a frame; pause display fetching |
| Stack | Service selection with auto-following log tail |
| Agents PageUp/PageDown, `f` | Scroll detail; resume following |
| Artifacts Enter | Inspect the selected artifact |
| Inspector Right/`n`, Left/`p`, Escape | Next/previous 4096-byte page; close |
| `c`, then Enter | Cancel an owned run, leaving the console open |
| `q` or Ctrl+C | Managed exit; an active owned run is cancelled through confirmation |
| Ctrl+Q | Detach: quit without cancelling; the Host, run and owner session survive for recovery |
| Rejection card `e`, Enter | After cleanup, edit a new intent with the same stack; view rejected-run details |

Do not modify global transport/storage paths to run a demo. The Host materializes configs under `var/runtime-host/runs/<mission_run_id>/`, including transport, agent storage, planner/environment artifacts, service logs and results. The Stack tab exposes readiness and logs. The final world frame (`world-frames/latest.png`) is retained after the stack stops.

AirSim frames persist as `world-frames/camera_front.jpg`, `camera_front_annotated.jpg` and `camera_third_person.jpg` with `.json` metadata; a camera source becomes selectable only after its first persisted image, and unchanged pixels do not advance it. With perception off, the `airsim-visualizer` service (Stack tab) steps the frozen scene to the world model's Mission time, places the drone at the world-model pose and draws ideal segmentation boxes; the World tab's `AirSim:` row shows its lag and measured ship phase error. With perception on, the Run Worker captures the `front_center_custom` and `third_person_demo` Scene cameras read-only at about 2 Hz and draws the perception module's boxes only on the frame they belong to. Status and failure reasons are in `world-frames/airsim.json` and `camera-capture.json` (`capturing`, `unavailable`, `stopped`).

The rejection reason can appear while summaries are flushing and services are stopping; re-launch remains disabled until cleanup finishes. Progress search captures ordinary typing until Enter/Escape; Ctrl+C remains managed exit. Terminal evidence refresh waits at 2 s cadence for the final narrative attempt, drains the synchronized final pages, then stops polling.

On activation or recovery, all nine evidence sections hydrate once. Recent-first pages are backfilled with `before`, then forward deltas replay from the initial watermark; older pages cannot undo newer summary reparenting. Progress orders nodes by the numeric sequence in their IDs, so recovered history stays chronological. Local queue backpressure releases poll claims for retry instead of marking the Host offline.

Cancellation requests can take up to 9 s while the owned stack is reaped; the console uses a 12 s cancellation HTTP deadline and a 15 s managed wait. If that wait expires, the console exits and stops a Host it bootstrapped; the next Host start finishes reaping. A second Ctrl+C does not silently abandon an owned run; Ctrl+Q is the explicit detach override.

## Recovery and diagnostics

- The bootstrapped Host runs in its own process group, so killing only the console with SIGKILL, or closing its terminal, leaves the Host and run running and does not erase the persisted Console Session (`~/.local/state/onr/operator-console/session.json`). Relaunch against the same Host to recover the owned run.
- Ctrl+Q has the same effect intentionally. A console relaunched against an already-running Host never stops that Host on exit, so the Host keeps running until it is stopped separately.
- If that persisted run is absent or no longer the Host's current run, recovery returns to Launch without inheriting ownership of the newer run. The previous session file remains until a normal activation replaces it.
- Restarting a killed Host records a Host-Interrupted Failure (`cancelled_by_owner` if it had already accepted cancellation) and reaps the orphaned worker and stack processes it can prove it owns (recorded PID and start time, or the exact run-ownership token). The Harbor restoration guardian is left to restore `environment.json`.
- Worker errors have a sanitized reason/stage on the run card; full traceback: `<run_root>/worker.log`. Service logs: `<run_root>/services/`.
- `NO_COLOR=1` disables semantic colours; importance/state glyphs remain.
- `ONR_CONSOLE_IMAGE_PROTOCOL` provides the same override as `--image-protocol`; `off` disables images.

See [the illustrated user guide](../../docs/operator-console-guide/index.html), [the boss-demo runbook](../../docs/operator-console-demo.md), [the design/contracts](../../docs/design/operator-console/README.md) and [ADR 0016](../../docs/adr/0016-airsim-follower-and-perception-annotations.md).
