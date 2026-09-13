# Mission 2 observation planning

Tracking: [runtime #17](https://github.com/Sino-Huang/onr_physical_runtime/issues/17),
under parent [#15](https://github.com/Sino-Huang/onr_physical_runtime/issues/15).

The runtime selects `mission2` or `joint` and supplies a versioned
`world_model_info.perception_predictions` object. Agent Slim consumes the same
fields for standalone simulation and live Sukai. Simulated probability is
unavailable, not zero or certainty. Predictions are not confirmed collisions.

`onr.application.mission2_planning` builds observation candidates from public
forecast positions, current aircraft position/speed and view range. Candidates
retain source/run/sequence, pair/target identity, sample age, contact deadline,
travel estimate, operational altitude and arrival direction. A currently visible
target can be pursued; an unseen target needs an observation/acquisition view.
Travel feasibility is an estimate; runtime path, visibility and lifecycle
feedback remain authoritative.

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
