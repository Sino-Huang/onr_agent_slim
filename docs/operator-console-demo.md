# Operator Console boss demo

Use one terminal to launch, monitor and cancel the full integrated stack. The console is the operator path; the Runtime Host owns process composition. The herdr scripts remain available for scripted capture and render the same Stack Builder plan.

## Before the audience arrives

```bash
cd /data/ccu/sukaih/ONR/onr_agent_slim
source /home/sukaih/miniconda3/etc/profile.d/conda.sh
conda activate onr
curl --fail http://127.0.0.1:11411/v1/models
# Start vLLM only if absent (defaults to GPUs 2,3):
# bash scripts/vllm/start_vllm.sh
./scripts/tui/start_operator_console.sh --image-protocol auto
```

Use tmux 3.3+ at 160×45 (minimum 100×30), started from a plain SSH shell with `./scripts/tui/start_tmux.sh`; node05's system tmux 2.7 cannot pass pixel graphics through and yields halfblocks. For readable maps and cameras, use a Kitty-graphics-capable outer terminal. `auto` queries the terminal; check its startup renderer notice reports Kitty pixel graphics with a measured cell size, not `cell 10x20`. `halfblocks` is a low-resolution compatibility fallback, not a presentation-quality renderer. See [the terminal and graphics setup](../scripts/tui/README.md#launch-and-choose-a-mission). Warm the release build before presenting. Preflight must finish without failures; warnings such as low disk/GPU headroom remain visible.

Select **mission1-harbor**, AirSim off, perception off, coordinator-driven updates (Enter on Preset opens the named picker; Left/Right also cycles). This simulated preset still runs the real physical-runtime world model, planners, belief service and vLLM agents. It avoids Harbor startup for the rejection demonstration. Mission 1 surveillance-view preparation is a real startup step, not agent reasoning; its measured boot time is recorded with the issue's verification results. Point the audience at **What this runs** under the stack selector: the Host's own description of the mission goal, real LLM calls, simulated versus perception-fed truth, what AirSim shows, and that coordinator-driven Mission time pauses while the agents reason. Switching AirSim on there changes it to the AirSim Follower, which visualizes the simulated world with no agent perception.

## Demo 1 — reject an out-of-scope input, then run a mission

1. F2 → **buy me a coffee**. Alt+Enter → review; Enter → launch.
2. Tab **6 Stack** while booting (or press **w** on any tab to jump to what the waiting banner names). Explain required services, prep steps and readiness; select the physical runtime to show the live log tail.
3. As soon as Hyper rejects the intent, the prominent **MISSION REJECTED** card shows its actual reason, intent and stage. The run remains owned during final summary flush and teardown, then becomes `failed` / `mission_rejected`; this is not a process crash.
4. Enter shows run details. Tab **3 Agents** to inspect the recorded `reject_mission_intent` tool call. Recorded Debug Reasoning is not mission authority.
5. After cleanup finishes, press **e** on the rejection card to return to Launch with the same preset and toggles. F2 → preset mission. Review and launch. If Enter dismissed the card, the run-screen `e` key opens the same new-intent flow.
6. Watch **1 Overview**: Stack → Intent → Planning → Statechart → Executing. Mission time may remain frozen while the agents reason in coordinator-driven mode.

Keep both the rejection card and the normal run screenshots/captures. Committed representative render frames are in `docs/design/operator-console/frames/`; live demo captures should be labelled as live rather than fixture snapshots.

## Demo 2 — three levels of live progress

1. Open **2 Progress**. The hierarchy is **Run Narrative → Mission Log Summaries → raw records**.
2. Let at least three summary windows finish (default summary heartbeat 30 s, plus model latency). Expand a summary with Right. Its children are precisely the covered operational-log sequence range, not hand-picked anecdotes.
3. Show **Live — not yet summarized**. When the next summary arrives these records move beneath their summary without duplication.
4. Select a summary to read its full text, then a record to inspect its details. The narrative and summaries are labelled **AI / non-authoritative**; run/FSM/environment state has its own authority.
5. Use **i** to expose debug heartbeats, then restore the normal filter. Importance is deterministic mapping version 1; the model does not choose severity. A summary inherits the highest importance of its records.
6. **f** follows newest; Up/Down pauses follow to inspect history. **/** searches, **n/N** moves through matches.
7. Return to Overview and observe a refreshed Run Narrative, generated in the background on the 30 s cadence and once at run termination.

## Belief, context and world walkthrough

- **4 Belief & Context**: inspect reporting-reliability means, credible intervals, previous/prior deltas and revision sparklines. The table scrolls; Up/Down or **j**/**k** selects an entity, and the selected entity's gauge, deltas and sparkline stay visible below it (selection follows the entity across revisions). The reference Mission 1 replay moves ship 2 from about 0.133 to 0.623; a new live run's path depends on observed evidence. The right pane shows Mission Snapshot source health/freshness, active FSM state and enabled candidates, Active Maneuver, latest Transition Intent and Hyper outcome. Do not claim a percent-complete navigation gauge when the environment only reports remaining distance.
- **5 World**: see the live world-model overlays. **s** cycles world → annotated front → front camera → third-person camera, skipping sources without a frame; **p** pauses only display fetching, not the mission. Finished runs retain the final world frame and any captured camera frames. For the audience, **F4** presents the selected source large (below).
- **6 Stack**: show prep steps with measured durations, process readiness (with the previous same-preset run's readiness as history), ports and service-log tails. **w** from any tab opens the service or prep step the banner is waiting on, log following. Logs are run-scoped allowlisted Artifacts, not unrestricted file access.
- **7 Artifacts**: inspect planner files and verification logs using the existing byte-paged inspector.
- Mission 2–4 show an explicit **no Bayesian belief for this mission mode** state, not an empty or fabricated gauge.

## Presenting: keep the audience on the scene (F4)

Instead of switching among Progress, Agents, Belief and World while you talk, press **F4** on the Run screen. The presentation layout shows the selected World source large (scaled up) with its AirSim disclosures under it, one evidence card, the mission title, and both clocks: `Wall 12:00:43 UTC · run 00:00:40 │ Mission t=143.5 s`. Nothing rotates by itself; you choose what is on screen.

1. Before the audience: on **5 World** press **s** until the source you want is selected (for the AirSim Follower, the annotated front camera); select the entity on **4 Belief & Context** and, if you want a specific call, the invocation on **3 Agents** (**f** keeps following the newest). These selections are what the card shows.
2. **F4**. The card starts on the milestone: the current phase step plus the Progress selection, which follows the newest record. **v** cycles to the belief entity, then the agent invocation, then back. **s** still changes the World source.
3. To talk over one moment, press **p**. The frame, card, phase stepper and clocks hold and the clock row reads `FROZEN AT 12:01:13 UTC · t=152.0 s`. The console keeps polling the Host; **p** again shows the latest evidence at once. Mission time may sit still while the agents reason in coordinator-driven mode; the frozen label is the console's, not the mission's.
4. You cannot miss an event while presenting or frozen: a run failure (with its failure card), a Human Decision Request and a stale or offline Host appear as alert rows under the stepper, and the title row shows the live lifecycle status. Read them out; Enter dismisses the failure card, **l** leaves the layout for the failure log.
5. **F4** returns to the tab you were on (a tab key or **w** also leaves). Cancel (**c**) and managed exit (**q**) work from the layout as usual.

The AirSim Follower and annotation disclosures stay visible in the layout (`AirSim: world-model follower · … lag 0.5 s`, and on the annotated camera *Boxes are ideal instance segmentation, not agent perception*): say so when you show the boxes. A historical run (F3) can be presented the same way, read-only; its alert rows still follow the current run. Golden frames: `run-presentation-*.txt` (layout, frozen, failure breaking through) in [frames](design/operator-console/frames/).

## Optional AirSim demos

Every AirSim run starts Harbor. Which mechanism runs depends on perception (ADR [0016](adr/0016-airsim-follower-and-perception-annotations.md)).

### AirSim without perception: the world model drives AirSim

Select **mission1-harbor** (or **mission1-airsim**), AirSim **on**, perception **off**. The world model runs on simulated information. Before the services start, the `airsim-fixture` step builds a 30 s lead-in engine scene from the world model's own ship trajectories; the Stack tab then shows `airsim-engine`, `physical-runtime` and the optional `airsim-visualizer`. For each new Mission time the visualizer steps the frozen scene, puts the drone at the world-model pose and captures the cameras. On **5 World**, the annotated front frame shows ideal instance-segmentation boxes labelled `ship N` with the disclosure *AirSim follows the world model. Boxes are ideal instance segmentation, not agent perception.* The `AirSim:` row reports the lag behind the newest world-model state and the measured ship phase error. A visualizer failure after startup is a warning; it never fails the Mission Run.

For scripted capture without the console, `bash scripts/live_demo_with_wm/herdr_start_mission1_follower_demo.sh <session>` renders the same Stack Builder plan (`mission1-harbor` with `ONR_DEMO_AIRSIM=1`): it runs the `airsim-fixture` prep step, then opens `airsim-engine` and `airsim-visualizer` panes beside `physical-runtime` and `agent-slim`. `ONR_DEMO_AIRSIM=1` works with any Mission 1 preset whose AirSim toggle is offered, and it is rejected together with `ONR_DEMO_PERCEPTION=yolo|ideal`. `ONR_DEMO_DRY_RUN=1` prints the prep, engine, visualizer, physical-runtime and agent commands without running any of them.

### AirSim with perception: scene clock and perception annotations

Select **mission1-airsim**, perception **yolo** (or **ideal**). Preflight checks Harbor, settings, weights, `ultralytics`, and ports 41451/8766 plus the allocated viewer port. YOLO normally uses GPU1; vLLM uses GPUs 2,3.

Launch the preset mission. The physical runtime's scene clock owns AirSim time and the perception module feeds the world model. Camera sources appear on World once the Run Worker's read-only 2 Hz capture has persisted a first image (`front_center_custom` and `third_person_demo` on `SimpleFlight`). The annotated front frame draws the perception module's output for that same frame only: YOLO detections (`ship N · score`, or amber `unidentified (reason)`), or the ships ideal perception reported. Between perception samples it says *no perception sample for this frame*. Press **s** to cycle the sources. If a camera never appears, read `world-frames/airsim.json` and `world-frames/camera-capture.json` under the run root: `state` is `capturing`, `unavailable` or `stopped`, and `reason` names the failure (for example `camera_not_configured: …` or `airsim_capture_failed: …`). Unchanged images do not advance camera sequences. After the run terminates, audit the Host run root:

```bash
python scripts/audit_live_demo.py \
  --run-root var/runtime-host/runs/<mission_run_id> \
  --mission-mode mission1 --perception yolo
```

Require `live-acceptance.json` PASS; a green TUI lifecycle alone is not the AirSim/YOLO acceptance gate. The terminal receipt's **Audit** row on Overview reflects the `live-acceptance.json` that this audit script writes under the run root (PASS, or FAIL with its failures), and reads `not recorded` when no audit has been run; **x** exports the receipt with the current verdict. See [the full-stack prerequisites and audit](mission1-airsim-yolo-live-demo.md).

## End and recovery

Use **c**, Enter to cancel while keeping the console open, or **q**/Ctrl+C for managed exit. Do not kill the Host for a normal demo exit. Cancellation tears down only this run's stack; the engine guardian restores Harbor's `environment.json` even after forced teardown.

Recovery trade-offs:

- **Ctrl+Q** detaches without cancelling. The run continues, the Host keeps running, and relaunching the console recovers the owned run. That relaunched console did not bootstrap the Host, so it will not stop the Host on exit.
- If the console is accidentally killed (including `kill -9`) or its terminal closes, the Host and run survive because the bootstrapped Host has its own process group. Relaunch against the same Host to recover the persisted Console Session.
- If the Host is killed, the run cannot resume. Restarting the Host reaps the orphaned worker and stack it can prove it owns, then records a Host-Interrupted Failure (`cancelled_by_owner` if cancellation was already accepted).

Diagnostics are under `<run_root>/worker.log` and `services/*.log`; bootstrap diagnostics are `var/runtime-host/host.log`.

Existing Host history is migrated once at startup: persisted runs without `run_root` keep their existing historical artifact directory. Migration adds the recorded path; it does not copy evidence or maintain a second reader path. Service monitoring stops before the mission session's final model-summary flush, so normal teardown is not reported as a service crash.

## Verification evidence

Issue [#75](https://github.com/Sino-Huang/onr_agent_slim/issues/75) records exercised checks, live run roots, timing, evidence-ingestion performance, UI draw latency, audit results, teardown/restoration and session recovery. The exact launch/navigation instructions are also in [scripts/tui/README.md](../scripts/tui/README.md).

Final current-tree verification: Python **1,428 passed, 11 skipped, 22 deselected** (262.04 s); Rust **145 tests passed**, formatting and warning-denying Clippy passed, and the release binary built successfully. Live checks below exercised the actual Host, console and simulator surfaces rather than substituting tests for smoke proof.

Static checks are not globally clean: 14 existing Ruff findings remain, and unchanged portions of the Mission 4 planning test file retain 109 Pyright diagnostics. The issue records the checked scopes; no suppressions or changed public exception types were used to conceal this debt.

### Exercised live runs

These are single-run observations, not CI timing assertions. Canonical evidence remains under `var/runtime-host/runs/<mission_run_id>/`.

| Check | Mission Run | Observed result |
| --- | --- | --- |
| Coffee rejection | `run-33bc80c9-6bf9-4561-8ae8-49b09b6ecdb5` | Real rejection reason appeared about 26.7 s after the simulated stack was ready; no planner/statechart execution. |
| Simulated default mission | `run-998f35fe-690d-4127-8eec-b8e040f23902` | `succeeded`; 25 Mission Log Summaries, 824 operational records and updating world frames. |
| AirSim + YOLO | `run-5ce0eccf-9974-4048-8e64-202717b9535d` | `succeeded`; strict `live-acceptance.json` audit PASS. The auditor reads the recorded UUID Mission ID rather than assuming `mission:demo`. |
| Owner cancellation | `run-9006b0d6-48b0-4db4-9ad5-404c70068cab` | `cancelled`; no live owned service processes after 9.176 s; both Harbor configuration files restored to their baseline MD5s. |
| Console recovery | Same cancellation run | SIGKILL did not stop the run; the recovered owner session retained its credential and could open cancellation confirmation. |
| Host interruption | `run-8d3d7dbc-8bbc-49d1-b44b-08e454b7e1d2` | Host SIGKILL after all four AirSim/YOLO services were ready; restart recorded `failed` / `host_interrupted`, reaped the owned stack, marked services stopped and restored both configuration files. |

Real front and third-person camera images rendered as halfblocks in a 160×45 herdr pane. Display pause held frame 5639 while backend capture advanced from 5640 to 5645; resume displayed 5645. Across 20,853 instrumented terminal draws, p95 was 1.336 ms and the maximum was 9.864 ms, with no draw above 50 ms. Terminal runs retained the final frame.

A separate fresh-process bootstrap reached health in 5.37 s with the startup spinner and log path visible. The same Host PID remained healthy after console SIGKILL and its tmux PTY hangup; reconnection and Ctrl+Q detachment were also exercised. This used a warm OS page cache and did not activate a mission; active owner recovery is the separate run above.

### Reference replay and controlled overlay checks

The recorded `run.a6CqxX` belief fixture was replayed through a production Host and the release console. Ship 2's first five means were 0.133, 0.133, 0.133, 0.623 and 0.623 (delta from the first revision +0.4902). With 20 entities, the selected-entity gauge, delta and `▁▁▁▄▄` history remained visible at both 100×30 and 160×45; the entity table scrolled to the twentieth entry. Mission Snapshot v397 showed five healthy/fresh sources and the enabled FSM Transition Candidate. See the labelled `replay-belief-selected-ship2-*` captures in [frames](design/operator-console/frames/).

A separate live native simulation used real `ManeuverCommand`s and coordinator-driven environment updates, not LLM-generated planning. Its Host World section and halfblocks pane showed **navigate**, **search_area**, **investigate** and **pursue**, with the native PNG overlays visible for all four. The final cached frame was sequence 159 with maneuver `pursue`; 2,047 terminal draws measured p95 0.655 ms / maximum 5.948 ms. See `controlled-world-*-160x45.txt` in [frames](design/operator-console/frames/). This proves the four visualization paths; it does not claim that each maneuver completed or that this controlled driver was an LLM-live mission.

The active-maneuver label reads the native command's `intent.action`. A regression now uses that real nested shape, rather than a fictitious top-level `action` field.

### Evidence-ingestion performance

The exact `run.a6CqxX` fixture contained 11,674 files / 206,012,548 bytes. With debug enabled, 300 steady calls per section to production `RuntimeHost.operator_view` measured p95: Overview 46.667 ms, Agents 36.373 ms, Environment 27.609 ms, Artifacts 31.089 ms, Progress 50.152 ms, Beliefs 39.910 ms and Context 40.334 ms. Every section met the 100 ms p95 gate.

These are in-process projection calls, excluding HTTP and JSON encoding; the OS page cache was warm. Fresh Host/tailer ingestion took 11.766 s. A 100-stage append replay produced the same 5,017 evidence records, contiguous observations and 933 operational records as a fresh rescan and single full ingest. Completed steady polls re-read zero transport or operational files; the original fixture was unchanged. Machine load differed from the before-cache measurement, so wall-time ratios alone are not a causal speedup estimate.

### vLLM contention qualification

Smoke B's four Hyper workflows averaged 103.255 s, including 92.480 s of model calls. The older `run.a6CqxX` comparison averaged about 78 s per Hyper workflow, but it had different intent, runtime/perception mode, replans and request counts. The approximately 25 s difference is **not** attributable to the new summaries from those two runs alone. Saved prompt/call and summary-generation measurements are descriptive; changing `heartbeats.summary_seconds` remains the operator control for summary cadence.

An additional eight-trial, fixed-prompt ABBAABBA probe replayed the same 18,830-token Hyper prompt with a deterministic seed and 512 output tokens. Baseline calls averaged 32.986 s; calls coincident with one production-prompt Mission Log Summary and one bounded Run Narrative averaged 41.507 s (observed difference +8.522 s; paired differences −8.884, +8.378, +38.460 and −3.867 s). Server counters recorded external completions in seven of eight trials, so this is **not an isolated causal estimate**. It measures a bounded coincident-request diagnostic, not the full mission's 30 s summary cadence.

The Mission 4 worker receipt/resume regression drives external receipts at public-evidence boundaries rather than racing three transitions against one reused one-second deadline. It uses an arbitrary `mission-<uuid>`, checks mission-time gating and preserves the saved session after a mismatched resume script. Performance timings remain observations outside CI assertions.

The terminal-session credential-persistence regression uses the Host's injected-worker seam, not real scene/stack preparation. It proves the run succeeded before restart, then checks that a different credential remains rejected and its plaintext is not persisted.
