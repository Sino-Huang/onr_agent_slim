# Mission 2 observation planning

For the terminal-audited vLLM/runtime launcher, see
[Model-backed live demos for Missions 2–4](live-demo-missions-2-4.md).

Tracking: [runtime #17](https://github.com/Sino-Huang/onr_physical_runtime/issues/17),
under parent [#15](https://github.com/Sino-Huang/onr_physical_runtime/issues/15).

The runtime selects `mission2` or `joint` and supplies a versioned
`world_model_info.perception_predictions` object. Agent Slim consumes the same
fields for standalone simulation and live Sukai. Simulated probability is
unavailable, not zero or certainty. Predictions are not confirmed collisions.

`onr.application.mission2_planning` builds observation candidates from public
forecast positions, current aircraft position/speed and view range. Candidates
retain source/run/sequence, pair/target identity, sample age, predicted 10 m entry time,
travel estimate, operational altitude and arrival direction. A currently visible
target can be pursued; an unseen target needs an observation/acquisition view.
Travel feasibility is an estimate; runtime path, visibility and lifecycle
feedback remain authoritative. A predicted 10 m entry ranks urgency; it is not
an actual-contact timestamp or an expiry for monitoring an active pair.

The Mission 2 replan gate coalesces identical evidence and wakes Hyper on new
warnings, risk membership/urgency changes, changed observation feasibility and
stale forecasts. It operates independently of Mission 1's utility gate. Hyper
and external planner verification still own plan revisions, and accepted
Statecharts still own execution semantics. The gate does not send flight commands.

The closed-loop CLI recognizes `mission2` from the authoritative environment and
does not construct a Mission 1 reporting-reliability service for it. Joint mode
retains that service and uses the full prediction roster for vessel priors.
Mission 1-only mode keeps its existing path. Runs are bounded by the recording
end supplied by the runtime.

For Hyper's native planner generation, the existing MiniZinc role skill links to
the Mission 2 reference. Inspect authorized evidence with:

```bash
conda activate onr
python -m onr.application.mission2_planning /absolute/path/to/environment.json \
  --output /absolute/path/to/workspace/mission2-candidates.json \
  --joint-priority balanced
```

Joint priority is `balanced`, `mission1` or `mission2` as configured by Mission
Intent and the planner invocation. Balanced advisory selection alternates when
both missions have feasible work. Mission utilities and evaluation scores remain
separate; no fixed weighted aggregate is introduced.

Run the runtime's documented `mission2_interaction` command for a bounded actual
runtime/Agent-service interaction. Its deterministic decision provider is labelled
explicitly and does not claim an LLM-generated plan. Full model-based missions
use the normal runtime CLI/model configuration and updated role skills.

## Launch an actual model-based standalone mission

The existing launcher defaults to Mission 1. For Mission 2, use an explicit mode:

```bash
ONR_DEMO_MISSION_MODE=mission2 ONR_DEMO_DRY_RUN=1 \
  bash scripts/live_demo_with_wm/herdr_start_live_demo.sh onr
```

The dry-run validates input paths, writes isolated run configuration, and prints
the exact commands without starting herdr panes, runtime or models. Remove
`ONR_DEMO_DRY_RUN=1` for the actual run in an existing `onr` herdr session with the
configured model service available. Both panes use unbuffered Python and display
progress; all state stays under the printed run directory. This route uses
standalone world-model predictions and needs no AirSim or solution checkout.

After the terminal result, run the launcher's printed audit command. It binds
the exact selected scenario to the recorded public prediction snapshots and
writes `mission2-metrics.json` beside `live-acceptance.json`. The former contains
the sub-1 m, sub-5 m, and sub-10 m pair metrics specified by Physical Runtime;
the latter embeds the same data with the integration acceptance evidence.
Private trajectories are opened by this terminal evaluator, never supplied to
the live Agent or predictor.

Set `ONR_DEMO_MISSION2_SCENARIO` to another original scenario directory. For joint
mode set `ONR_DEMO_MISSION_MODE=joint` and explicitly supply
`ONR_DEMO_MISSION1_INSTANCE` for that moving scenario. `ONR_DEMO_MISSION_FILE`
can select another Mission Input (retain the launcher's `mission:demo` identity),
including a different joint scheduling preference. The defaults are the committed
Mission 2-only and balanced joint Mission Input examples.
