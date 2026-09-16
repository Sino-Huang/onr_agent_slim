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
defaults to the standalone search package/fixture and replays the two timed
requests in `examples/mission4_requests.json`; its third pane records durable
worker receipts. Its demo profile uses a 15 m sensor radius so dock coverage
requires physical traversal and distinct fixture views. The shared launcher
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

Each run retains generated configuration, physical state, Agent storage,
planner revisions, immutable environment artifacts, transport records,
`closed-loop-result.json`, and the final audit. Close its Herdr workspace to stop
the owned panes; the run directory remains available for diagnosis.

For Mission 4, the scripted worker remains active until the runtime accepts the
Agent's terminal report. Additional manual requests can use
`python -m onr.adapters.mission4_worker --help` and the run's public environment,
worker session and physical `search_requests/` directory.
