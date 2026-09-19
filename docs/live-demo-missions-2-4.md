# Model-backed live demos for Missions 2–4

These launchers run the configured vLLM Hyper and Maneuver agents, MiniZinc,
FileTransport and `onr_physical_runtime` together. Mission 3 and Mission 4 use
their standalone visibility-dependent perception fixtures; these commands do
not certify the pending colleague-owned perception or native AirSim gates.

Start one mission at a time in an existing Herdr session:

```bash
bash scripts/live_demo_with_wm/herdr_start_mission2_live_demo.sh <session>
bash scripts/live_demo_with_wm/herdr_start_mission3_live_demo.sh <session>
bash scripts/live_demo_with_wm/herdr_start_mission4_live_demo.sh <session>
```

Mission 2 uses the original `offshore_dock_1/collision/0` recording with a
demo-only initial drone pose near its first forecast risk; the recording,
prediction inputs and evaluator truth remain unchanged.
Mission 3 defaults to the three-ship standalone inspection fixture. Mission 4
defaults to the static ground-team assistance scenario on the assembled
offshore_dock_1 map: the runtime checkout's `config/mission4_offshore_demo.yaml`
(offshore map, scenario ships, 100 m visibility, seed 22) with the generated
package, fixture and answers under its
`docs/mission_desc/mission4_offshore_non_collision_0/`; it replays the three
timed ground-team tasks in `examples/mission4_requests.json` and its third pane
records durable worker receipts. The shared launcher
prints every resolved input and the isolated run directory. Mission 1 retains
`var/live_demo_with_wm/run.*`; Missions 2–4 write to
`var/live_demo_with_wm/mission2/run.*`, `mission3/run.*`, and `mission4/run.*`,
respectively.
The scripted worker allows one hour of wall time by default for queued shared
vLLM deployments; override it with `ONR_DEMO_MISSION4_WORKER_TIMEOUT_SECONDS`.

Mission 2–4 replanning is driven by their collision, inspection and search
gates. They do not run the generic ten-second Hyper timer; unchanged evidence
therefore does not spend another model episode. Their fallback Maneuver cadence
is 300 seconds (override with `ONR_DEMO_MANEUVER_SECONDS`); physical lifecycle
and evidence gates still wake the agents immediately.

All three modes derive current candidates or adaptive decisions from the public
environment file with checked-in Python and emit checked-in MiniZinc/Statechart
shapes. The configured vLLM agent still owns Mission parsing, planner choice,
external solver submission, verification, activation, replanning, and physical
action selection, but it does not hand-author planner syntax during a live run.

Set `ONR_DEMO_DRY_RUN=1` to inspect commands without starting services. Existing
`ONR_DEMO_*` input overrides remain available. If another workspace owns the
default viewer port, set `ONR_DEMO_VIEWER_PORT`, for example:

```bash
ONR_DEMO_VIEWER_PORT=5067 \
  bash scripts/live_demo_with_wm/herdr_start_mission2_live_demo.sh <session>
```

After the Agent pane reaches its terminal JSON result, run the exact audit
command printed by the launcher. The audit writes `live-acceptance.json` and
fails on a nonterminal FSM, missing model/planner/maneuver evidence, Mission-time
movement during inference, dead letters, operational errors, or an incomplete
mission-specific result.

For Mission 2, that same terminal command opens the selected scenario truth
only after execution and writes `mission2-metrics.json`. It reports pair recall,
precision, false positives and false negatives, timely-warning coverage, and
per-pair lead time at the strict sub-1 m contact, sub-5 m collision-warning, and
sub-10 m near-collision boundaries. The metrics are also embedded in
`live-acceptance.json` for one-file presentation and remain measurements rather
than an invented pass threshold.

Each run retains generated configuration, physical state, Agent storage,
planner revisions, immutable environment artifacts, transport records,
`closed-loop-result.json`, and the final audit. Close its Herdr workspace to stop
the owned panes; the run directory remains available for diagnosis.

For Mission 4, the scripted worker remains active until the runtime accepts the
Agent's terminal report. Additional manual requests can use
`python -m onr.adapters.mission4_worker --help` and the run's public environment,
worker session and physical `search_requests/` directory.

The printed audit command passes `--mission4-answers <private answers.json>`
(the generated `answers.json` by default). It runs the runtime-side static
evaluator and writes `mission4-answer-metrics.json` into the run root;
the per-task location error (m), direction error (deg) and animal-type
correctness are embedded in `live-acceptance.json` as informational
`mission4_answer_metrics` and never change the audit's pass/fail status. The
audit's private-key ban on run artifacts now also covers the `answers` key.

A successful Mission 4 run terminates its search with reason `all_found` —
the audit fails any other terminal search state — and retains audited worker
receipts. Its planner is expected to exercise `navigate`, `search_area` and
`investigate` physical commands as public evidence requires. End-to-end
live-run confirmation of the full offshore scenario: run `run.4TD3gn`
(2026-09-20) terminated `all_found` at sim 207.5 s of the 900 s budget
(final FSM `search-complete`, six plan revisions) with the physical chain
navigate → navigate → investigate → search_area → investigate, audit PASS,
and all static-answer metrics resolved — worker:1/worker:2 person locations
at 1.0 m error, worker:3 animal location at 1.0 m with animal type bear
correct and escape-direction error 1.99 deg.
