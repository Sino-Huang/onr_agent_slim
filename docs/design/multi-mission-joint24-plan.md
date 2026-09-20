# Multi-Mission Combination (M2+M4, mode `joint24`): Three-Tier Scheduling with PDDL Top Level

## Context

Missions 1–4 run today strictly one-per-run (one `MissionInput` → one Mission Run → one plan-revision chain → one Statechart/FSM). The only existing combination is `joint` = M1+M2 (fused NL intent in `examples/mission1-and-2.json`, alternation via `joint_observation_priority`). The boss wants a harder, combined-mission demonstration. Target: **Mission 2 (collision-risk monitoring) + Mission 4 (worker-requested object search)** in one run on one drone, showcasing agent intelligence via a three-tier neuro-symbolic architecture:

- **TOP — PDDL mission scheduler** (Hyper Agent, symbolic planner profile, Fast Downward + VAL): per plan revision, the LLM authors a small PDDL problem encoding both missions' pending work, deadlines, alert urgency and switch costs; the solver produces the mission ordering; VAL validates it. This is the visible neuro-symbolic scheduling artifact.
- **MIDDLE — per-mission movement planners (unchanged)**: M2's `mission2_planning` candidate CLI + MiniZinc pick-≤1 selection model; M4's `Mission4AdaptivePlanner.decide` (Python) + MiniZinc receipt model. They stay the within-mission movement authority and act as grounding tools whose outputs feed the PDDL problem.
- **LOW — Maneuver Control Agent (unchanged role)**: per-heartbeat transition/maneuver selection; bounded preemption at maneuver boundaries via the existing ADR-0011 replan-reconciliation path.

Scheduling policy (operator-facing, OS analogy: PDDL = scheduler policy engine, replan gates = interrupts, revision = scheduling decision, boundary reconciliation = context switch, one drone = single core): **priority classes + bounded preemption**. Preemptive class: M2 fresh collision alert / imminent higher-priority risk; M4 request with imminent deadline. Preemptible class: routine M2 monitoring, routine M4 search. Preemption happens ONLY as: gate fires → coalesced replan → new PDDL ordering → new revision Statechart → Maneuver reconciliation may cancel/replace the in-flight preemptible maneuver (existing cancel-feedback path). Never mid-maneuver hard aborts from code.

End state: a live-demo-able `joint24` run (runtime + agent, herdr launcher) that terminates with audit PASS and **separate** per-mission metrics (`mission2-metrics.json`, `mission4-answer-metrics.json` — no weighted aggregate, per existing precedent).

Both checkouts are local and in scope: agent `/data/ccu/sukaih/ONR/onr_agent_slim`, runtime `/data/ccu/sukaih/ONR/onr_physical_runtime`.

## Approach

Ordered for a building tree: runtime first (feeds must exist before agent can consume), then agent recognition, then the PDDL layer, then wiring, then demo/verification. Steps 4 and 5 are independent of each other once 1–3 land.

### Step 1 — Runtime: `joint24` mode and combined feed (repo: onr_physical_runtime)

Today feeds are mutually exclusive: `build_public_agent_info` early-returns per mode; `mission4` branch publishes only the ledger; `SearchLedger` creation requires `mission4_package` which is XOR-locked to mode `mission4`; `SimulatedCollisionPredictor` is keyed on `{mission2, joint}`.

1.1 `src/onr_physical_runtime/runtime.py`:
- L122/L129-130: extend the `mission_mode` validated literal set to `{"mission1","mission2","mission3","mission4","joint","joint24"}`.
- L131-148 XOR guards: allow `mission4_package` when mode is `joint24`; allow mission2 scenario args with `joint24`. Keep all other combinations rejected.
- L212-216 `recording_end_time_s`: currently skipped for modes in `{"mission1","mission4"}`; set it for `joint24` (M2 half is recording-bounded).
- L226-248: `SearchLedger` creation is already keyed on `mission4_package is not None` — after the XOR relax it instantiates for `joint24` with no further change. Confirm the `search_requests/` poll loop (L524-529) and `apply_search_request` (L562-565, raises `"search requests require Mission 4"` without a ledger) now pass.
- L300-318: create `SimulatedCollisionPredictor` when `mission_mode in {"mission2","joint","joint24"}` and no external perception provider.
- L296-299 (`joint` restores mission1 check state): do NOT extend to `joint24` — there is no mission1 half.
- L778 re-application of `env.mission_mode` and spool `set_metadata('mission_mode', …)` need no change (literal flows through).

1.2 `src/onr_physical_runtime/world_model/model.py` `build_public_agent_info` (~L5732-5860): add a `joint24` branch that publishes in one payload: `mission_mode: "joint24"`, the mission4 ledger section (same shape as the mission4 branch, L5733-5770), `perception_predictions` from the mission2 predictor (same shape as the mission2 branch), and `mission_end_time_s`. Keep `visible_ships`/`ship_event_reports`/`event_report_checks` empty (M4 consumes its ledger, M2 consumes predictions — neither reads those lists in these modes).
- Confirm-first: trace how mission4 standalone perception (visibility-dependent fixture path used by the M4 offshore demo) reaches the publish payload without an external provider, and keep that path active in the new branch. `config/mission4_live_demo.yaml` (harbor_world + mission3_smoke vessels) proves mission4-on-harbor is a supported configuration.

1.3 `src/onr_physical_runtime/service.py` L158-167, L213, L268: accept `--mission-mode joint24` with `--scenario-config` + `--mission2-scenario-dir` + mission4 package args together; extend the existing per-mode argument guards accordingly.

1.4 Maneuver acceptance: mission4 mode rejects `pursue` (runtime.py ~L797-800). For `joint24`, accept the union of M2 maneuvers (`navigate`, `pursue`, acquisition views) and M4 maneuvers (`navigate`, `search_area`, `investigate`). Gate by mode, not by which mission is currently served (the agent's Statechart owns legality).

1.5 Artifacts (generated, committed under runtime repo):
- Regenerate the M4 static package on the harbor/collision map: `python scripts/build_mission4_static_package.py --scenario-dir <onr_scenario>/offshore_dock_1/collision/0 --output docs/mission_desc/mission4_harbor_collision_0/` (run `--help` first for exact flags; produces public `package.json`, private `fixture.json`/`answers.json`). If the script rejects collision/0, see Contingencies.
- New `config/joint24_demo.yaml`: `harbor_world.yaml` base, mission2-style trajectory rewrite to the collision/0 ships dir (`for_mission2` semantics: coord system `net`, zeroed NED offset, GPS interval ∈ {3,5,10,20}), the regenerated mission4 package paths, repo-owned empty `events_report.json`, 100 m visibility, seed 22 (mission4_offshore_demo precedent).

### Step 2 — Agent: recognize `joint24` (repo: onr_agent_slim)

2.1 `src/onr/runtime/cli.py` `_mission_mode_context` (L162-167): add `"joint24"` to the validated enum. Do NOT treat it as a reporting-reliability mode (no mission1 belief service — mission2-standalone precedent). Limit routing (L327-331): bound the run by the M2 recording end (`mission_end_time_s` from `world_model_info`); confirm the mission4 limit read (`world_model_info['mission4']`) is bypassed or harmonized for joint24 — decide by which value the runtime publishes in step 1.2 (recording end is the single run bound).

2.2 `src/onr/application/context_coordination.py` L528: `specialized_replan_mode` → `mission_mode in {"mission2","mission3","mission4","joint24"}` (both halves have specialized gates; no generic 10 s Hyper timer).

2.3 Gate activation guards:
- `src/onr/application/mission2_planning.py` L113, L498, L748: widen `in {"mission2","joint"}` → `in {"mission2","joint","joint24"}`.
- `src/onr/application/mission4_planning.py` L188: widen `== "mission4"` → `in {"mission4","joint24"}`.
- No change to gate wiring: both gates already instantiate side-by-side (context_coordination.py L509-511) and trigger identities already concatenate with `;` (L659-677). The M4 gate's embedded-decision trigger (`mission4-gate:<json>`, `decision_from_trigger`) flows unchanged.

2.4 `scripts/audit_live_demo.py` L70/L122 exact-equality checks: accept `joint24`; audit requires both missions' evidence (planner revisions, FSM terminal, maneuver lifecycle) and writes BOTH `mission2-metrics.json` and `mission4-answer-metrics.json` by invoking the existing evaluators unchanged; both embed into `live-acceptance.json`.

### Step 3 — Agent: `joint24` planning module (the code-owned combo seam)

New module `src/onr/application/joint24_planning.py` (no existing equivalent — mission2/3/4 each have their own `*_planning.py`; the combo needs its own). Owns:

3.1 Grounding inputs per revision (existing tools, no new code): mission2 candidates via `python -m onr.application.mission2_planning <env> --output mission2-candidates.json` (candidate identity, travel_millis, predicted 10 m entry, score; internally runs the MiniZinc selection model = M2 middle tier) and mission4 state via `python -m onr.application.mission4_planning <env> --output mission4-decision.json` (pending requests, deadlines, next decision; internally the adaptive planner + receipt model = M4 middle tier).

3.2 `materialize_joint24_pddl(environment_event, mission2_candidates, mission4_decision, out_dir) -> (domain_path, problem_path, schedule_metadata)`:
- Checked-in `domain.pddl` (new file under `conf/skills/hyper/creating-pddl-problem-files/examples/joint24-scheduler/`, pattern copied from `survey-return/`): STRIPS + action costs. Objects: missions `m2`, `m4`; locations `drone`, `m2-site`, `m4-site`. Predicates: `(at ?l)`, `(served ?m)`, `(pending ?m)`, `(deferred ?m)`. Action schemas: `serve-m2-from-<loc>` / `serve-m4-from-<loc>` for loc ∈ {drone, m2-site, m4-site} — 6 ground actions, each with fixed cost `travel_millis(from, block-site) + service_millis(mission)`; effect: `(served m)`, `(at site)`. One `defer-m2` / `defer-m4` action with cost below. Goal: `(and (or (served m2) (deferred m2)) (or (served m4) (deferred m4)))`. Metric: `(:metric minimize (total-cost))`.
- `problem.pddl` written per revision from the grounding inputs: `(pending m2)` iff a serviceable M2 candidate exists and recording not ended; `(pending m4)` iff unresolved requests exist; init `(at drone)`; numeric constants from candidates/decision (travel estimates, block service estimate = selected-candidate travel+monitor time for M2, next-decision step estimate for M4).
- **Defer cost rule (decided default)**: `DEFER_COST_PREEMPTIVE = 100000`, `DEFER_COST_ROUTINE = 1000` (same 100000 weight scale as the M2 MiniZinc model). Preemptive-class this revision iff: M2 gate trigger reason ∈ {`new_warning`}, or `higher_priority_risk` with advisory predicted-10 m-entry ≤ `JOINT24_PREEMPTIVE_ENTRY_S = 30.0`; or M4 trigger carries a request deadline within `JOINT24_PREEMPTIVE_DEADLINE_S = 60.0`. Otherwise routine. The defer action lets lmcut skip a low-urgency mission when travel cost outweighs it — this is what makes the ordering urgency-sensitive in plain STRIPS+costs.
- Trigger-reason → class mapping table lives here as code-owned constants (inputs arrive via the concatenated trigger identities already passed to the replan workflow).

3.3 `emit_joint24_statechart(schedule_metadata, revision, out) -> statechart.json`: top-level states `scheduling`, `mission2-block`, `mission4-block`, terminal `joint24-complete`; per-mission regions reuse the existing checked-in mission2/mission4 statechart shapes verbatim (docs/live-demo-missions-2-4.md: shapes are code-owned, agent never hand-authors syntax in live runs). Mission order in the chart follows the VAL-validated plan; a deferred mission's region is omitted this revision. Run terminates at recording end; M4 must have reached `all_found` or published its explicit-unresolved report by then (existing M4 terminal semantics).

3.4 Role-skill reference `conf/skills/hyper/creating-pddl-problem-files/references/joint24-mission-scheduler.md`: instructs Hyper that for `joint24` the PlannerChoice is `symbolic`/`fast-downward`, the problem is authored from the two grounding CLI outputs (never hand-invented numbers), and the checked-in domain is submitted unchanged (mirrors the MiniZinc skill's "code-owned files unchanged" rule).

### Step 4 — Agent: workflow wiring for `joint24` revisions

4.1 `src/onr/runtime/cli.py` `_run_hyper_revision` (L219-305) / replan closure (L436-473): for `joint24`, the revision's `PlannerChoice` is `("symbolic","fast-downward")`; the workflow workspace receives the materialized `domain.pddl`/`problem.pddl` from step 3.2 as pre-materialized assets (mission4 pre-materialization precedent, `src/onr/agents/hyper_workflow.py` L858). The existing symbolic path then applies unchanged: `submit_planner_attempt` (L1365) → `FastDownwardExecutor.check` (`--translate`), `planner_executor` (L1511, symbolic branch L1587-1610) → Fast Downward `--search astar(lmcut())` → `VALPlanValidator.validate` (accepts on rc 0 + `"Plan valid"`). The validated `sas_plan` is the revision's planner-native evidence; `PlannerPlan` envelope unchanged (single `mission_id` — the fused mission).

4.2 Replan mechanics: unchanged. Gates fire per mission → triggers concatenate → HyperSupervisor coalesces (forced `replan` on `reachability_required`) → full fresh revision with latest snapshot/belief → old revision authoritative until replacement verifies → transition intents invalidated → `replan-activated:<rev>` trigger → immediate reconciliation heartbeat (ADR 0011). This IS the preemption mechanism; Maneuver's existing cancel/replace judgment executes the bounded preemption. No new interrupt channel.

4.3 Maneuver Control: no contract change. Its heartbeat already enforces `snapshot.plan_reference == active plan reference` and one FSM transition per heartbeat; the joint24 Statechart gives it the mission-block regions.

### Step 5 — Examples + launcher

5.1 `examples/mission2-and-4.json` (new, exact 3-field MissionInput): `mission_id: "mission:demo"`, `source_authority: "demo-operator"`, `mission_text` fusing both missions with the NL scheduling policy: share the drone; worker search requests with imminent deadlines and fresh collision alerts are preemptive; routine monitoring/search is preemptible; preemption replaces in-flight preemptible work only at maneuver-boundary reconciliation after replan; exploit observations serving both missions; keep collision metrics and search-answer metrics separate; replan as evidence changes. (Pattern: `examples/mission1-and-2.json`.)

5.2 `examples/mission2and4_requests.json` (new): timed worker script `[{at_s, text}]` in the existing constrained grammar (`find … in …`, `at (north, east)`, direction hints), coordinates taken from the regenerated package's `answers.json` (harbor coords). Include one request timed to arrive while an M2 block is expected in flight (drives the preemption demonstration in Verification).

5.3 `scripts/live_demo_with_wm/herdr_start_live_demo.sh` L41-49: add `joint24)` case → default `ONR_DEMO_MISSION_FILE=examples/mission2-and-4.json`; panes: physical-runtime (joint24 service args from step 1.3), agent-slim, mission4-worker replaying `examples/mission2and4_requests.json` (existing `Mission4WorkerSession`/`play_request_script` mechanism). Run dir `var/live_demo_with_wm/joint24/run.*`. `ONR_DEMO_DRY_RUN` support unchanged.

### Step 6 — Tests (behavior-defending only)

6.1 Runtime repo (match its test conventions): combined publish test — boot `joint24` with the demo config, assert one payload contains `perception_predictions` AND the `mission4` ledger AND `mission_end_time_s`; `apply_search_request` accepts an envelope (no "search requests require Mission 4"); predictor produces pairs from the collision/0 recording.
6.2 Agent repo: mode-guard tests — mission2 gate active and mission1 service absent under `joint24`; `joint24_planning.materialize_joint24_pddl` golden test: fixture candidates+decision with a preemptive M4 deadline → solver orders `serve-m4` before `serve-m2` (run real Fast Downward + VAL in the test, mirroring existing planner adapter tests); `defer` chosen when routine M2 travel cost exceeds `DEFER_COST_ROUTINE`.
6.3 Do NOT re-pin existing mission1/2/4 tests; the mode-guard widening must keep them green unchanged.

## Critical files & anchors

- `/data/ccu/sukaih/ONR/onr_physical_runtime/src/onr_physical_runtime/world_model/model.py` — `build_public_agent_info` mission4 early-return branch (~L5733-5770): the merge point for the combined feed; getting both payload halves right is the crux of step 1.
- `/data/ccu/sukaih/ONR/onr_physical_runtime/src/onr_physical_runtime/runtime.py` L122-148, L300-318 — enum/XOR/predictor/ledger construction rules.
- `src/onr/application/context_coordination.py` L509-534, L620-700 — side-by-side gates, `specialized_replan_mode`, trigger concatenation: proves per-mission interrupts already coalesce into one revision.
- `src/onr/agents/hyper_workflow.py` L1365 (`submit_planner_attempt`), L1511 (`planner_executor`), L1587-1610 (symbolic/VAL branch) — the existing symbolic-profile machinery the PDDL scheduler plugs into unchanged.
- `src/onr/application/mission2_planning.py` L113/227/748 and `src/onr/application/mission4_planning.py` L188/394 — mode guards and the two grounding CLIs/models.

## Verification

Prereqs: `conda activate onr`; both checkouts at `/data/ccu/sukaih/ONR/`; MiniZinc at `modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc`; Fast Downward `modules/downward/fast-downward.py`; VAL `modules/VAL/.../Validate`; for the live run, the configured vLLM model service available.

1. **Runtime feed smoke** (after step 1): start the runtime standalone with `config/joint24_demo.yaml` + `--mission-mode joint24 --mission2-scenario-dir <onr_scenario>/offshore_dock_1/collision/0`; inspect the published `world_model_info`: contains `perception_predictions` with ≥1 active pair AND `mission4` ledger with the regenerated package's requests AND `mission_end_time_s`. Drop a valid search-request envelope into `search_requests/` → accepted (ledger revision bumps), no error.
2. **PDDL scheduler unit proof** (after step 3): run the golden test from 6.2 — concrete input: fixture with M2 candidate travel 120 s routine + M4 request deadline in 45 s (preemptive). Expected: `sas_plan` orders `serve-m4-from-drone` first; VAL prints `Plan valid`. Second fixture: both routine, M2 travel 5 s, M4 travel 300 s → plan may defer M4 (defer cost 1000 < marginal travel); assert served order or deferral matches the cost rule.
3. **Closed-loop combined run** (after steps 2–5): `ONR_DEMO_MISSION_MODE=joint24 ONR_DEMO_DRY_RUN=1 bash scripts/live_demo_with_wm/herdr_start_live_demo.sh onr` validates inputs; then the real run. Expected terminal evidence: audit PASS; FSM terminal `joint24-complete`; `mission2-metrics.json` (pair recall/precision/lead times) AND `mission4-answer-metrics.json` (location/direction/animal-type) both written and embedded in `live-acceptance.json`; planner-artifacts contain ≥2 distinct revisions whose `sas_plan` files order the two missions differently across revisions (evidence the scheduler re-orders on evidence change); at least one `replan-activated:` trigger coincides with the timed worker request from 5.2, and maneuver feedback shows the in-flight preemptible maneuver cancelled or completed-then-switched (the preemption demonstration).
4. **Regression**: full existing agent test suite + runtime test suite green; missions 1–4 standalone launchers untouched and their dry-runs still validate.

## Assumptions & contingencies

- **Runtime repo is ours to modify** (user decision: full-stack). A tracking issue on `Sino-Huang/onr_physical_runtime` is still filed describing the `joint24` mode for colleague visibility.
- **Package regeneration**: `scripts/build_mission4_static_package.py --scenario-dir` is reported to accept any scenario, but only offshore artifacts exist today. If it rejects collision/0 (e.g. ship-set assumptions), fallback: verify the agent side end-to-end against a synthetic combined environment feed (test-harness `world_model_info` carrying both payloads) and file the packaging gap as a runtime issue; the live demo then waits on the colleague.
- **Preemption thresholds** `30.0 s` / `60.0 s` and defer costs `100000/1000` are decided defaults chosen for the collision/0 recording's geometry and the M2 model's existing 100000 scale; if the live run shows thrash (excessive mission switching), retune constants in `joint24_planning.py` only — no design change.
- **vLLM availability**: if the model service is unavailable at verification time, steps 1–2 and the dry-run still prove the system; the live-run audit is then recorded as blocked-on-service, not skipped silently.
