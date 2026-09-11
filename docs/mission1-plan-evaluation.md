# Fast Mission 1 plan evaluation

Use `scripts/evaluate_mission1_plan.py` to compare public-input MiniZinc plans
without starting an LLM, AirSim, or the live Agent system. Tracking: [issue #58](https://github.com/Sino-Huang/onr_agent_slim/issues/58).

The helper first solves every requested radius using only a supplied public
environment snapshot and Bayesian Belief Snapshot. Each real COIN-BC result must
be optimal and match the Python oracle's route, score, modes, and targets. Only
then does the evaluator load the private scenario and Mission truth. It uses
Physical Runtime's event/report pairing, discrepancy windows, and unique issue
latching to calculate recall. It also reports balanced MSE from the shared prior,
the supplied check ledger, and checks obtained during the ideal observation windows.

## Assumptions

This is **ideal scheduled-coverage recall**, not measured live recall. It assumes
perfect arrival at each assignment, omnidirectional coverage within the stated
radius with no terrain occlusion, and perfect tracking during pursuit. Other
ships inside that ideal footprint can also produce checks. Sensing uses the
scenario's tick size and half-open assignment intervals; it does not add a tick
after departure. No transit sightings, early-arrival observations, or subsequent
replans are included. Consequently this is not a strict upper bound on the full
closed-loop mission, which may gather additional evidence between assignments.

The denominator covers the entire hidden instance, including unobserved events.
Do not feed evaluator truth, issue identities, or these scores back into a live
planning snapshot. Use separate instances to validate generality after tuning;
a favorable score on one known scenario is not a general-performance guarantee.

## Invocation

Activate the `onr` environment. All inputs below are caller-selected paths;
`OUTPUT` must be a fresh directory under the Agent repository's `var/` directory.
The Physical Runtime package and scenario data must be available locally.

```bash
python scripts/evaluate_mission1_plan.py \
  --environment "$PUBLIC_ENVIRONMENT" --belief "$BELIEF_SNAPSHOT" \
  --model "$MODEL_MZN" --minizinc "$MINIZINC_BINARY" \
  --scenario "$SCENARIO_YAML" --reports "$REPORTS_JSON" \
  --truth "$TRUTH_JSON" --output "$OUTPUT" --radii 100 300 500
```

Each case writes its public inputs, exact solver output, parsed assignments, and
evaluation ledger. `summary.json` compares recall, MSE, modes, and measured
generation/validation/solver/evaluation times. Original runs are never changed.

## Visibility configuration

For the external Physical Runtime integration, the supplied scenario YAML owns
`runtime.max_visibility_distance_m`. The shipped harbor scenario now uses 300 m.
Physical Runtime advertises this as `controlled_vehicle.fov_radius` in initial
and subsequent snapshots; Agent candidate generation consumes that value. The
Agent's fake-environment `sensing_radius` does not control the live integration.

Actual visibility still depends on camera direction, pitch, and terrain. The old
additional `3 × altitude` hard cap was removed because it silently reduced a
100 m advertised range to 75 m at the shipped 25 m altitude and made a radius-only
configuration increase ineffective. This change does not turn the real camera
into the ideal omnidirectional sensor used by this diagnostic.

## Initial controlled result

Using the recorded prior/public input from `run.uUVtyt`, ideal recall was 1/39
at 100 m, 7/39 at 300 m, and 11/39 at 500 m. Solves took approximately 3–5 s and
scoring less than one second per case. These are single-plan offline results,
not a new successful live Mission Run.

Replaying the old accepted commands with the old visibility reproduced every
recorded drone pose, visible-ID list, check ledger, and public GPS snapshot.
Changing only the configured radius while retaining the old altitude cap made
no difference. With the configured 300 m cap actually enabled, the old commands
found zero issues: changed visibility also changed pursuit motion. Thus a larger
radius alone is not a recall fix; fresh planning and further sensor/route
verification remain necessary. Speed and warmup were not changed.

## Viewpoint reuse comparison (2026-09-11)

The builder now considers every sampled viewpoint at other visible future
report times, not just at the time of the report that supplied its coordinates.
This fixes a missing feasible option: an earlier report's viewpoint can see a
later ship whose own position is unreachable by its deadline. Fixed-view dwell,
public-opportunity utility, pursuit omission yield, and the 10% replan threshold
are unchanged. MiniZinc also resolves residual equal-index-sum route ties using
the smallest optimal predecessor recursively, matching the oracle.

Against commit `a40d771`, with the same public prior and 300 m capability:

- Demo ideal scheduled recall: **10/39 → 12/39 (30.77%)**; covered public
  reports: **41 → 56**. Balanced MSE: **0.070201 → 0.069182**.
- All 30 seed-100 corruption profiles: mean ideal recall
  **19.13% → 24.47%**, with **23 improvements, 7 unchanged, 0 regressions**.
  Every real COIN-BC solve was optimal and exactly matched oracle route, score,
  mode and entity. Maximum solver time was **5.97 s**; candidate generation
  took about **10 s** per corpus case. The profiles share harbor trajectories,
  so this is not independent-map generalization.
- A longer-fixed-window prototype also improved mean recall to **23.82%**,
  but was not adopted. Reusing viewpoints with short windows explains the demo
  gain without changing the observation-window semantics. Circle-intersection
  viewpoints and a speed-only 30→45 m/s trial both stayed at 10/39 on the demo.
- Continuous ideal replay of the selected demo route remains at 12/39 for
  either shortest cardinal axis order, with all arrivals feasible. An offline
  adaptive shared-viewpoint trial also reached 12/39 with one gate-triggered
  replan at 247.5 s. These are not live controller/camera results.

There is a cost: the demo's selected assignments rise from 14 to 30. Further
work should preserve coverage while reducing execution overhead and improving
observation utility. No human-expert schedule has been supplied; these results
do not establish human-level performance or completion of the high-recall goal.

Detailed local receipts are under `var/mission1-recall/global-short-corpus-002/`,
`reused-viewpoints-integrated/`, `windows-corpus-001/`,
`continuous-reused-viewpoints-001/`, and `adaptive-global-short-001/`.
