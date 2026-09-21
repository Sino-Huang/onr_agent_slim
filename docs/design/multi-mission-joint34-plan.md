# Multi-Mission Combination (M3+M4, mode `joint34`): Three-Tier Scheduling with PDDL Top Level

## Context

`joint24` (M2+M4) shipped and is live-verified (issue Sino-Huang/onr_agent_slim#67). Mission 2 is too large to keep in the loop: its 182.5 s collision recording plus the per-revision MiniZinc candidate selection drove 1.6–5.1 h live runs. Next combination, explicitly excluding Mission 2: **Mission 3 (gate/vessel inspection) + Mission 4 (worker-requested object search)** in one run on one drone, with the same three-tier neuro-symbolic architecture as joint24:

- **TOP — PDDL mission scheduler** (Hyper Agent, symbolic/fast-downward + VAL), per plan revision, code-owned combo module `joint34_planning.py`.
- **MIDDLE — per-mission planners unchanged**: `Mission3AdaptivePlanner.decide` + MiniZinc receipt model; `Mission4AdaptivePlanner.decide` + receipt model. Their CLI outputs are the scheduler's grounding.
- **LOW — Maneuver Control Agent unchanged**: bounded preemption at maneuver boundaries via the existing ADR-0011 replan-reconciliation path.

Scheduling policy identical to joint24: priority classes + bounded preemption. Preemptive class: M4 request with imminent deadline; M3 fresh inconclusive evidence / moved target. Preemptible class: routine M3 inspection, routine M4 search.

Why joint34 is cheap to build: both missions already co-exist on the harbor scene — `config/mission4_live_demo.yaml` (data/harbor_world/vessels, 299.5 s) reuses `config/mission3_smoke/vessels`, and the harbor M4 smoke package (`docs/mission_desc/mission4_package.json` + `mission4_fixture.json`) already ran on that scenario (`docs/mission_desc/mission4_acceptance.md`). No package/map regeneration. The run is bounded by the M3 time budget (default 120 s sim), not a long recording.

End state: live-demo-able `joint34` run (runtime + agent, herdr launcher) terminating with audit PASS, FSM terminal `joint34-complete`, and separate per-mission evidence: `mission4-answer-metrics.json` (existing evaluator) + M3 inspection evidence embedded in `live-acceptance.json` (M3 has no metrics evaluator — mission3-standalone audit precedent). The implementation + test plan is published as a GitHub issue on `Sino-Huang/onr_agent_slim` (full plan) and a tracking issue on `Sino-Huang/onr_physical_runtime` (joint24 precedent).

Both checkouts in scope: agent `/data/ccu/sukaih/ONR/onr_agent_slim`, runtime `/data/ccu/sukaih/ONR/onr_physical_runtime`. Another agent works in both repos concurrently: NEVER revert or stage files outside the ones this plan names; commit only named files.

## Approach

Ordered for a building tree: runtime first (feeds before agent consumes), then agent recognition, then the PDDL layer, then wiring, then demo/verification. The joint24 commit `169691d` (runtime) and `src/onr/application/joint24_planning.py` (agent) are the copy-adapt templates throughout; mirror their shapes, do not invent new structure. Steps 5 and 6 are independent once 1–4 land.

### Step 1 — Runtime: `joint34` mode and combined feed (repo: onr_physical_runtime)

Today: M3 objects/guards are exact `== "mission3"`; M4 package XOR-locked to `{mission4, joint24}`; `build_public_agent_info` early-returns per mode. Diff shape mirrors joint24 commit `169691d`.

1.1 `src/onr_physical_runtime/runtime.py`:
- L129 mode whitelist: add `"joint34"`.
- L131 `mission4_package` XOR: allow for `joint34`.
- L133 external-perception flag: widen `mission_mode in {"mission4","joint24"}` → add `"joint34"` (M4-half perception suppression semantics, same as joint24).
- L137 `mission4_fixture` guard: allow for `joint34`.
- L139–148 `mission3_selection`/`mission3_fixture`/`mission3_time_budget` guards: allow `joint34` (selection + time_budget required; fixture optional — the demo uses live perception for the M3 half; keep the selection-change-on-restart rejection and the live-perception-XOR-fixture guard).
- L212–219: no change. `recording_end_time_s` = min trajectory end (299.5 s for harbor vessels); `mission_end_time_s = min(recording_end, mission3_time_budget_s)` already keys on budget presence — with the joint34 selection's budget this yields the intended run bound unchanged.
- L225–248 M4 objects (`SearchLedger`, `SimulatedSearchSensor`, `install_constraints`, `search_requests/`): built whenever `mission4_package` present — no change after the XOR relax.
- L249–278 M3 objects (`Mission3InspectionLedger`, `SimulatedInspectionSensor` only with fixture, `Mission3TargetTracker`, `mission3_target_max_age_s`): widen the `mode == "mission3"` construction guard to `in {"mission3","joint34"}`.
- L296–299 (`joint` restores mission1 check state) and L300–318 (M2 predictor): do NOT extend — no M1/M2 half.
- L577–601 tick() early-M4-terminal block: the `joint24` condition (cancel only in-flight `SEARCH_AREA`/`INVESTIGATE`; do not `return False`) widens to `{joint24, joint34}` — an early-completed M4 ledger ends only the M4 half; the run continues for M3.
- L602–630 `mission_end_time_s` exhaustion: unchanged. For joint34 this fires once at the run bound (min(299.5, m3 budget)), identical to mission3 standalone.
- Maneuver dispatch:
  - L807–810 PURSUE InterceptController "unless mission3": widen exclusion to `{mission3, joint34}` (joint34 pursue = M3 tracker flavor; M4 never pursues).
  - L838 PURSUE rejected only `== "mission4"`: unchanged (joint34 accepts pursue).
  - L968 mission3 PURSUE → `_tick_mission3_pursue`: widen condition to `{mission3, joint34}`.
  - L1137 INVESTIGATE dispatch — the one genuinely new branch: joint34 carries BOTH investigate flavors (M3: deadline + tracker; M4: explicit target centre from parameters, the `{mission4, joint24}` path). For `joint34`, dispatch on the command's parameters, not the mode: if the intent parameters carry the M4-style target centre (same keys the `{mission4, joint24}` branch reads), take that branch; otherwise take the mission3 branch. Confirm-first: read L1130–1160 for the exact parameter keys; the M4 block's `planner_item` decision (Mission4Decision investigate) always carries them, the M3 block's (Mission3Decision) never does.
- L1840 event-observation-payload suppression set `{mission2,mission3,mission4}`: do NOT add `joint34` (joint24 precedent — suppressing broke nothing there and the merged feed path is simpler unsuppressed). Confirm-first during step 1.2 that neither the M3 tracker ingest nor the M4 search sensor reads event observation payloads in this mode; only then optionally add it — default stays unsuppressed.
- L1855–1864 checkpoint payload (mission1 check state for `joint`): unchanged.

1.2 `src/onr_physical_runtime/world_model/model.py` `build_public_agent_info` (~L5771–5861): add a `joint34` branch beside the `{mission4, joint24}` early branch that publishes in one payload: `mission_mode: "joint34"`; the mission4 ledger section (same shape as the mission4 branch, incl. the observe-coverage/sensor path); `info["mission3"]` = `Mission3InspectionLedger.public_state()` + `target_observations` (`Mission3TargetTracker.public_estimates`) + `target_observation_max_age_s` (reuse the L5818 mission3 block verbatim); `mission_end_time_s`; and `converter.get_public_gps_info` (the M3 tracker tracks ships from GPS fixes — joint24 included this for its predictor). Keep `visible_ships`/`ship_event_reports`/`event_report_checks` empty (both halves consume their ledger/tracker sections).
- Confirm-first: trace the mission3 no-fixture path — with `SimulatedInspectionSensor` absent, what feeds `tracker.ingest` in the L5818 block — and keep that path active in the joint34 branch. If the M3 tracker cannot ingest without a fixture (unverified), see Contingencies.

1.3 Service entry points: add `joint34` to `--mission-mode` choices in `src/onr_physical_runtime/service.py` (top-level parser + validation L108–111/L173–176; extend `_load_mission3_time_budget` loading to joint34) and `src/onr_physical_runtime/agent/service.py` (L94 choices; per-mode arg guards). joint34 requires together: `--scenario-config` + `--mission3-selection` (+ optional `--mission3-fixture`) + `--mission4-package` (+ optional `--mission4-fixture`). Do NOT apply the `for_mission2` scenario rewrite or M2 args (M2 family only); do NOT set `entity_ids=set()` / `perception.mission4` (those stay exact `=="mission4"` — the M3 tracker needs entity perception).

1.4 Artifacts:
- New `config/joint34_demo.yaml`: copy `config/mission4_live_demo.yaml` (data/harbor_world/vessels recording, harbor map, seed, visibility) unchanged except the header comment documenting the joint34 arg set (selection + package paths).
- New agent-side selection `examples/mission3and4_selection.json` (repo: onr_agent_slim, step 5) is passed as `--mission3-selection`; runtime repo adds no M3 asset.

### Step 2 — Agent: recognize `joint34` (repo: onr_agent_slim)

2.1 `src/onr/runtime/cli.py`:
- L165–167 `_mission_mode_context` enum: add `"joint34"`.
- L168–180 per-mode roster read: confirm-first what mission3/mission4 standalone read here; give joint34 the mission4 treatment (the M4 ledger roster; the M3 selection targets already arrive in the mission3 payload). If both have roster reads, union them.
- Limit routing L333–337: bound by `mission_end_time_s` from `world_model_info` — same as joint24, no new code.
- Belief service L348–360: do NOT extend (no reporting-reliability half — mission2-standalone precedent).
- Initial revision L402: `joint34_trigger_identities=()` when mode is joint34; replan path L446–477 passes trigger identities unchanged.

2.2 `src/onr/application/context_coordination.py` L528–533 `specialized_replan_mode`: add `"joint34"`. Gates already instantiate side-by-side (Mission3ReplanGate + Mission4 gate, L509–511) and triggers concatenate with `;` (L659–677) — no wiring change.

2.3 Gate activation guards:
- `src/onr/application/mission3_planning.py` L136 `_mission3()`: `!= "mission3"` → `not in {"mission3","joint34"}`.
- `src/onr/application/mission4_planning.py` L188: `in {"mission4","joint24"}` → add `"joint34"`.
- `src/onr/application/mission4_planning.py` L200–214 recording-end deadline clamp (reports `search_exhausted` at the run bound in the ledger's vocabulary): widen the `mission_mode == "joint24"` condition to `in {"joint24","joint34"}`. This is what guarantees the M4 explicit-unresolved report by the joint34 run bound — the joint24 live-run fix, inherited from day one.
- No change to `mission1_planning.py`/`mission2_planning.py` (no M1/M2 half).

2.4 Audit: `scripts/audit_live_demo.py` and `src/onr/application/live_demo_audit.py`:
- Accept `--mission-mode joint34` (mode sets: audit script L51, engine L70–71).
- M3 evidence for joint34: adapt the mission3 branch (L144–147) — our demo has no M3 fixture, so require: mission3 report event on `mission3-agent-reports` topic (or roster all-resolved in the final payload) + FSM passed through `mission3-block`. Confirm-first the exact mission3-standalone assertions and reuse everything not fixture-bound; NEVER assert fixture evidence for joint34.
- M4 evidence: reuse the joint24 branch verbatim (explicit-unresolved/all_found report + transport fallback).
- Metrics: invoke `evaluate_mission4_static.py` unchanged → `mission4-answer-metrics.json`; embed both missions' evidence in `live-acceptance.json`. M3 has no metrics evaluator (none exists for mission3 standalone either) — no new evaluator.

### Step 3 — Agent: `joint34_planning` module (the code-owned combo seam)

New module `src/onr/application/joint34_planning.py`, copy-adapted from `joint24_planning.py` (same public surface, M2 symbols swapped for M3). Owns:

3.1 Grounding per revision (existing tools, no new code): M3 via `python -m onr.application.mission3_planning <env> --output mission3-decision.json --model model.mzn --data data.dzn` (next inspection decision; receipt model = M3 middle tier) and M4 via `python -m onr.application.mission4_planning <env> --output mission4-decision.json`.

3.2 `materialize_joint34_pddl(environment, mission3_decision, mission4_decision, out_dir, *, trigger_identities=()) -> (domain_path, problem_path, schedule_metadata)`:
- Checked-in `conf/skills/hyper/creating-pddl-problem-files/examples/joint34-scheduler/domain.pddl` (copy `joint24-scheduler/domain.pddl`, rename m2→m3): STRIPS + action costs; objects `m3`,`m4`; locations `drone`,`m3-site`,`m4-site`; `serve-m3-from-<loc>`/`serve-m4-from-<loc>`; `defer-m3`/`defer-m4`; goal served-or-deferred both; `(:metric minimize (total-cost))`.
- `problem.pddl` per revision: `(pending m3)` iff unresolved selection targets remain and `mission_time < m3 budget`; `(pending m4)` iff unresolved ledger objectives exist; init `(at drone)`; costs from the grounding decisions (travel estimates; M3 block service estimate = decision step estimate; M4 = `JOINT34_M4_SERVICE_MILLIS = 60000`).
- Constants (decided defaults, same scale rationale as joint24): `DEFER_COST_PREEMPTIVE = 100000`, `DEFER_COST_ROUTINE = 1000`, `JOINT34_PREEMPTIVE_DEADLINE_S = 60.0`.
- `joint34_revision_class(environment, trigger_identities)`: **preemptive** iff an M4 trigger carries a request deadline within `JOINT34_PREEMPTIVE_DEADLINE_S`, or an M3 trigger reason ∈ {`new_inconclusive_evidence`, `target_moved`}; otherwise **routine**.

3.3 `create_statechart(schedule, plan_text)` / `emit_joint34_statechart(schedule_metadata, plan_path, out)`: top-level `scheduling`, `mission3-block`, `mission4-block`, terminal `joint34-complete`; mission order follows the VAL-validated plan; deferred mission's block omitted this revision. Block shapes are code-owned (conf/skills/hyper/creating-statechart-files/examples/adaptive-mission/prepare_statechart.py handles `{mission3, mission4}` — reuse those shapes; the agent never hand-authors syntax). M3 block context = the Mission3Decision `planner_item`; M4 block = Mission4Decision `planner_item`. Readiness rules copy the joint24 module's L364–386 pattern including the `not_before` bounds that keep M3/M4 block entries OUT of the M1/M2 deterministic entry dispatch (joint24 live-run bug a524fa7 — inherited from day one) and `matching_maneuver_lifecycle_terminal` per block. M3 terminal readiness: M3 report decision issued (block completes) or `mission_time >= budget` → then the chart serves M4 until the run bound.

3.4 Role-skill reference `conf/skills/hyper/creating-pddl-problem-files/references/joint34-mission-scheduler.md` (copy the joint24 reference, swap literals) + branch in that skill's SKILL.md L29 area: PlannerChoice `symbolic`/`fast-downward`, grounding from the two CLI outputs (never hand-invented numbers), checked-in domain submitted unchanged.

### Step 4 — Agent: workflow wiring for `joint34` revisions

4.1 `src/onr/agents/hyper_workflow.py` `record_planning_intent` (joint24 block L876–901): add the joint34 branch — pre-materialize `domain.pddl`/`problem.pddl` into the revision workspace and emit the `joint34_lines` PlannerChoice instruction (copy the joint24 block, swap literals). The symbolic path downstream (`submit_planner_attempt` → `FastDownwardExecutor.check` → `astar(lmcut())` → `VALPlanValidator.validate` → `submit_statechart_draft`) is mode-agnostic and unchanged.

4.2 `src/onr/runtime/composition.py` L926–987 `create_hyper_workflow_context`: pass `joint34_trigger_identities` alongside the existing fields (mirror the joint24 plumbing).

4.3 Replan mechanics and Maneuver Control: unchanged (ADR-0011 path; the joint34 statechart gives the mission-block regions). Gate triggers from both missions coalesce exactly as in joint24.

### Step 5 — Examples + launcher (repo: onr_agent_slim)

5.1 `examples/mission3-and-4.json`: 3-field MissionInput (`mission_id: "mission:demo"`, `source_authority: "demo-operator"`), `mission_text` fusing both missions with the NL scheduling policy — copy `examples/mission2-and-4.json`'s text, swap M2-collision clauses for M3-inspection clauses (inspect the selected vessels within the time budget; worker requests with imminent deadlines and fresh inconclusive/moved-target evidence are preemptive; routine inspection/search preemptible).

5.2 `examples/mission3and4_selection.json`: M3 runtime selection `{schema_version: 1, target_ids: [...], mission_time_budget_s: 120.0}` (pattern: `examples/mission3_description.json`). Confirm-first: read one trajectory file under `/data/ccu/sukaih/ONR/onr_physical_runtime/data/harbor_world/vessels/` and choose 2–3 vessel ids actually present there.

5.3 `examples/mission3and4_requests.json`: timed worker script `[{at_s, text}]` in the constrained grammar (pattern: `examples/mission2and4_requests.json`), coordinates from the harbor M4 package's answer key region (dock ±20 m). Confirm-first: locate the harbor package's answers file (see Contingencies). Time requests within 0–100 s so all land before the 120 s run bound; include one timed to arrive while an M3 block is expected in flight.

5.4 `scripts/live_demo_with_wm/herdr_start_live_demo.sh`: add `joint34)` case beside joint24 — default `ONR_DEMO_MISSION_FILE=examples/mission3-and-4.json`; runtime args `--mission-mode joint34 --scenario-config <runtime>/config/joint34_demo.yaml --mission3-selection examples/mission3and4_selection.json --mission4-package <runtime>/docs/mission_desc/mission4_package.json --mission4-fixture <runtime>/docs/mission_desc/mission4_fixture.json`; mission4-worker pane replaying `examples/mission3and4_requests.json` (worker gate already covers joint modes — extend its mode condition to joint34); run dir `var/live_demo_with_wm/joint34/run.*`; audit args `--mission-mode joint34 --mission4-answers <harbor answers>`; `ONR_DEMO_MISSION4_WORKER_TIMEOUT_SECONDS` knob unchanged (use 21600 for live runs — joint24 lesson). `ONR_DEMO_DRY_RUN` support unchanged.

### Step 6 — Tests (behavior-defending only)

6.1 Runtime repo `tests/test_joint34.py` (mirror `tests/test_joint24.py`): boot `joint34` with the demo config → one payload contains `mission3` (ledger + target_observations) AND `mission4` ledger AND `mission_end_time_s`; `apply_search_request` accepts an envelope (no "search requests require Mission 4"); mission3 selection accepted (ledger built with the selection's target ids); joint34 INVESTIGATE dispatches M4-flavour on centre-parameters and M3-flavour otherwise.

6.2 Agent repo `tests/test_joint34_planning.py` (mirror `tests/test_joint24_planning.py`): golden test running real Fast Downward + VAL — fixture with routine M3 (travel 120 s) + preemptive M4 (deadline in 45 s) → plan orders `serve-m4-from-drone` first, VAL `Plan valid`; both-routine fixture → defer per the cost rule. Mode-guard tests: mission3 gate active under joint34, mission1/mission2 services absent. Audit test: joint34 accepted, both evidence branches evaluated.

6.3 Do NOT re-pin existing mission1/2/3/4/joint24 tests; guard widening must keep them green unchanged.

### Step 7 — Publication + durable doc

7.1 Commit `docs/design/multi-mission-joint34-plan.md` (this plan, durable in-repo copy — joint24 precedent) with the first implementation commit.

7.2 File the implementation + test plan as a GitHub issue on `Sino-Huang/onr_agent_slim` via `gh issue create`: title `Multi-mission combination (M3+M4, mode joint34): three-tier PDDL scheduling plan`, body = this plan (Context/Approach/Verification), label `ready-for-agent`.

7.3 File a tracking issue on `Sino-Huang/onr_physical_runtime` describing the `joint34` runtime mode for colleague visibility (joint24 precedent).

## Critical files & anchors

- `/data/ccu/sukaih/ONR/onr_physical_runtime/src/onr_physical_runtime/runtime.py` L129–148, L249–278, L577–630, L968, L1137 — mode guards, M3/M4 object construction, tick terminal semantics, and the one new logic branch (parameter-based INVESTIGATE dispatch for joint34).
- `/data/ccu/sukaih/ONR/onr_physical_runtime/src/onr_physical_runtime/world_model/model.py` L5771–5861 — `build_public_agent_info` merge point: the joint24/mission4 early branch + the L5818 mission3 block to fuse.
- `src/onr/application/joint24_planning.py` — the combo-module template (grounding → PDDL → plan-order → statechart → CLI); joint34_planning.py is a copy-adapt, not a new design.
- `src/onr/application/mission4_planning.py` L188–219 — mode guard + the recording-end clamp that must cover joint34 (carries the joint24 live-run report fix).
- `src/onr/agents/hyper_workflow.py` L876–901 — the joint24 pre-materialization block to mirror for joint34.

## Verification

Prereqs: `conda activate onr`; both checkouts at `/data/ccu/sukaih/ONR/`; Fast Downward `modules/downward/fast-downward.py`; VAL `modules/VAL/build/linux64/Release/bin/Validate`; MiniZinc `modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc`; for the live run, the vLLM service on port 11411.

1. **Runtime feed smoke** (after step 1): start the runtime standalone with `config/joint34_demo.yaml` + joint34 arg set; assert the published `world_model_info` contains `mission3` (ledger public_state + target_observations), the `mission4` ledger with the harbor package's objectives, and `mission_end_time_s == min(299.5, 120) == 120.0`. Drop a valid search-request envelope into `search_requests/` → accepted, ledger revision bumps.
2. **PDDL scheduler unit proof** (after step 3): the 6.2 golden test — concrete input: routine M3 decision (travel 120 s) + M4 request deadline in 45 s. Expected: `sas_plan` orders `serve-m4-from-drone` first; VAL prints `Plan valid`. Both-routine fixture: served order or deferral matches the cost rule.
3. **Closed-loop combined run** (after steps 2–5): `ONR_DEMO_MISSION_MODE=joint34 ONR_DEMO_DRY_RUN=1 bash scripts/live_demo_with_wm/herdr_start_live_demo.sh onr` validates inputs; then the real run (worker timeout 21600 s). Expected terminal evidence: audit PASS; FSM terminal `joint34-complete`; `mission4-answer-metrics.json` written (evaluator, worker-session or transport fallback); M3 evidence embedded in `live-acceptance.json`; planner-artifacts contain ≥2 distinct revisions whose `sas_plan` files order the two missions differently; ≥1 `replan-activated:` trigger coincides with the timed worker request from 5.3. Budget note: 120 s sim at the observed joint24 heartbeat cost ≈ 1–3.5 h wall on this rig — shorter than joint24 by construction (no M2 candidate generation, 2/3 the sim length); do not "fix" slowness by widening scope.
4. **Regression**: full agent suite + runtime suite green; missions 1–4 and joint24 standalone dry-runs still validate.

## Assumptions & contingencies

- **Harbor M4 answers file**: the harbor smoke package (`docs/mission_desc/mission4_package.json`/`mission4_fixture.json`) is assumed to have a sibling `answers.json` produced by `build_mission4_static_package.py`. If absent: regenerate the triple with `scripts/build_mission4_static_package.py` for the harbor vessel set (run `--help` first); if the builder rejects harbor vessels, fall back to scene C — `config/mission4_offshore_demo.yaml` base (offshore_dock_1 non_collision/0, 299.5 s) + existing `docs/mission_desc/mission4_offshore_non_collision_0/` package-with-answers + an M3 selection authored for offshore vessel ids (budget 120 s). Everything else in this plan is scene-independent.
- **M3 tracker without fixture** (step 1.2 confirm-first): if the M3 target tracker cannot ingest under live perception in the merged branch, author an M3 private fixture for the harbor vessels (pattern: `config/mission3_smoke/private_fixture.json`) and pass `--mission3-fixture`; the runtime guards already permit fixture+joint34 after step 1.1.
- **Preemptive thresholds / defer costs** are decided defaults; if the live run shows thrash, retune constants in `joint34_planning.py` only — no design change.
- **vLLM availability**: if the model service is unavailable at verification time, steps 1–2 and the dry-run still prove the system; record the live-run audit as blocked-on-service, not skipped silently.
- **Concurrent agent**: another coding agent works both repos. Stage and commit only the files this plan names; if a named file was concurrently modified, re-read and re-apply on top — never revert.

## Recovery and acceptance notes (2026-09-21)

Issue #68 resumes implementation from the interrupted session; runtime tracking
is [onr_physical_runtime#36](https://github.com/Sino-Huang/onr_physical_runtime/issues/36).
The user clarified that acceptance at this stage is the world-model + agent
integration launched through `scripts/live_demo_with_wm`, without AirSim.

- Restore the requested 120 s selection budget and full 299.5 s harbor recording.
  The standalone `mission4_live_demo.yaml` actually uses a 20 s smoke recording;
  copying that path would silently shorten this demonstration.
- Keep the prior session's preemptive defer price of 1,000,000 milliseconds;
  routine deferral remains 1,000. This makes serving both missions affordable at
  harbor-scale travel distances. The real-solver golden case checks the optimal
  served order, and the routine case checks deferral.
- Select actual harbor vessels 7, 15, and 16. Their changing geometry permits
  inspection-first as well as dock-search-first scheduling during the run.
- The worker script requests a 60 s deadline at t=1, adds a search at t=40,
  extends the deadline to 100 s at t=40, and adds another search at t=80.
  A generic add alone does not make a 300 s deadline imminent; both missions
  correctly defer initially. Deadline commands occupy worker sequences 2 and
  4, so search answer keys are 1, 3, and 5.
- Decode both gates from semicolon-coalesced trigger identities. Otherwise an
  M3 prefix hides a simultaneous imminent M4 deadline from the scheduler.
- Urgency is a property of pending work, not just the latest trigger. In live
  revision 5 an M3-only trigger erased an outstanding imminent M4 deadline and
  incorrectly deferred both missions. Keep the active M4 ledger's deadline
  preemptive across sibling-triggered revisions, until completion or expiry.
  This corrects the trigger-only formulation in step 3.2 above.
- Ground M4 navigation travel from its `x`/`y` parameters, as well as supporting
  investigation centres and search polygons. A real Fast Downward + VAL case
  checks that a distant navigation goal changes the selected mission order.
- M4 must distinguish its own maneuver lifecycle from M3's. All M4 decisions
  carry `deadline_time`; M3 decisions do not. Treating the sibling's active
  navigation as continuation of a prior search suppressed timed worker
  requests in the first live attempt. The regression reproduces that starvation
  before the ownership fix and passes after it.
- Publish actual `mission3-agent-reports` and embed inspection/report evidence
  in `live-acceptance.json`. A proposed report trigger alone is not acceptance.
- Keep lifecycle-based block completion and add a budget-exhaustion edge from
  M3 to M4, so an unfinished M3 maneuver cannot strand the chart at the bound.
- Terminal edges carry both `mission_time_at_or_after` (automatic dispatch) and
  `not_before` (transition-tool enforcement). The first key alone allowed a
  model-requested terminal transition at t=15.5 despite the 120 s budget.
  The joint34 audit now rejects early completion and the wrong terminal state.
- The `scheduling` context describes a **committed** validated decision, even
  for an empty order. Describing it as an unfinished commit caused Hyper to
  decline the urgent t=1.5 replan. Replaying that same recorded supervisor
  request with corrected context returned `replan` from the real vLLM model.
- Preserve the joint scheduling policy inside both mission blocks, with the
  worker revision used to ground the plan. Otherwise Hyper treated the active
  inspection block as authority to ignore later deadline-urgent worker
  revisions, and independently misread per-attribute uncertainty as search
  completion. Within-mission decisions remain owned by the M3/M4 middle tiers.
  Real solver replay of the t=40 live feed selects M3→M4, whereas the initial
  urgent revision selected M4→M3; Hyper must request that new PDDL revision.
- Joint34 gates consume the same Mission Snapshot as the Hyper heartbeat.
  Calling the live planning view just for gate assessment published a newer
  worker revision before queued older updates were drained. The t=40 replan
  was then rejected as not snapshot-authorized, delaying scheduling until
  different vessel geometry arrived. The captured-revision regression checks
  that a gate cannot propose targets from a newer, unauthorized worker view.
- Distinguish a new urgent worker revision from another viewpoint for the
  already-scheduled request. Run `run.6pTGpV` reached the correct 120 s terminal
  state and audit PASS, but repeatedly restarting M4 completed its only
  request at 16.5 s; later scripted requests correctly required a new run.
  The joint scheduling policy now preserves the committed next mission block
  for ordinary next-view progress. Replaying its t=5.5 supervisor input with
  the real model returned `no_change`; replaying the t=40.5 urgent worker
  arrival from `run.z782wG` still returned `replan`.
- Restore the standalone harbor demo's 15 m camera range instead of the
  interrupted session's 100 m override. Vessel positions remain public GPS
  observations; camera range governs actual visual/search evidence.
- Scope planner execution instructions to the selected planner; Fast Downward
  receives `minizinc_solver=null`. Keep the executor's validation intact.
- Remove the interrupted session's unconditional CLI traceback: exception text
  can expose Mission Input or credentials. The safe-failure regression passes.

Full-suite baseline observed during recovery: agent 1,182 passed / 2 failed
before the CLI correction (the other failure is the pre-existing modified vLLM
launcher's two-GPU default against a four-GPU default assertion). Runtime 662
passed / 18 failed / 4 errors: unavailable live AirSim gates plus the seven
M2/M4 loop failures already recorded on runtime #36. These are recorded, not
re-pinned or silently suppressed; unrelated concurrent edits remain untouched.

### Final combined-run acceptance (2026-09-22)

Run: `var/live_demo_with_wm/joint34/run.7jruzl` (world model + agent,
without AirSim). The terminal audit returned **PASS**, with no failures.
The FSM reached `joint34-complete` at exactly 120.0 simulated seconds.
There were four committed revisions and six physical navigation commands.

| Revision | Mission time | Accepted worker revision | Validated served order |
| --- | ---: | ---: | --- |
| 1 | 0.0 | 1 | both deferred |
| 2 | 1.5 | 2 | M4 → M3 |
| 3 | 40.5 | 4 | M3 → M4 |
| 4 | 97.0 | 5 | M4 → M3 |

All five scripted worker operations were accepted. The new objective and
deadline extension were accepted at t=40.0; Maneuver Control received
`replan-activated:3` at t=40.5 while reconciling the in-flight M3 navigation.
The final new search request was accepted at t=80.0.

Evidence under the run directory:

- `closed-loop-result.json`: terminal state, duration, revisions, and actions.
- `live-acceptance.json`: audit PASS and both mission evidence branches.
- `planner-artifacts/revision-00{2,3,4}/workspace/001/sas_plan`: both served
  orders from the verified planner pipeline.
- `debug/llm/maneuver-control/mission%3Ademo/00000000000000000010.json`:
  t=40.5 activation of revision 3.
- `mission4-worker-session.json`: accepted request/deadline receipts.
- `mission4-answer-metrics.json`: evaluator output for three search targets.

This is scheduling/integration acceptance, not a claim that every target was
resolved. The M3 budget report explicitly records all three ships unresolved
with `mission_budget`; no visual inspection evidence was obtained. M4 reports
the blue container found and the red container and truck incomplete. The
answer evaluator records zero answered location/direction/type questions.

Final focused agent regression: **86 passed** across joint34, joint24,
Mission 4 planning, closed-loop/gate coordination, audit, and launcher tests.
Real-model policy replays separately verified `no_change` for ordinary
same-request viewpoint progress and `replan` for the urgent t=40 worker
revision. Full-suite baseline limitations are recorded above; this is not a
claim that the unrelated repository-wide failures were repaired.
Final focused runtime regression: **8 passed** (joint34, joint24, and actual
FoV persistence), with one existing `pkg_resources` deprecation warning.
