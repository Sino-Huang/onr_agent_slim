// Mock /api/steps payload for the demo mission.
//
// Story: hyper-agent runs the phase-gated planning workflow (intent → context
// → assets → planner execution with one rejected attempt + repair feedback →
// statechart generation with one failed draft → handoff), then maneuver-
// control runs heartbeat-driven execution with physical tools, transport
// feedback, and one tool error that is retried.
//
// The shape mirrors the real StepProjection: decisions, feedback entries and
// artifact references merge onto the correlated llm step; records without
// debug evidence degrade to standalone decision/feedback steps. A builder
// keeps timestamps and seq assignment consistent.

const BASE = Date.parse("2026-08-21T09:41:12.000+00:00");
let cursor = BASE;

function iso(ms) {
  return new Date(ms).toISOString();
}

// Advance the global mission clock: occupies `durationMs`, then a small gap.
function span(durationMs, gapMs = 140) {
  const started = cursor;
  cursor += durationMs;
  const finished = cursor;
  cursor += gapMs;
  return { started_at: iso(started), finished_at: iso(finished), duration_ms: durationMs };
}

// A child span strictly inside a parent window.
function within(parent, offsetMs, durationMs) {
  const started = Date.parse(parent.started_at) + offsetMs;
  return {
    started_at: iso(started),
    finished_at: iso(started + durationMs),
    duration_ms: durationMs,
  };
}

let seqCounter = 0;
const pending = [];

function assignSeq(step) {
  seqCounter += 1;
  step.seq = seqCounter;
  step.step_id = (step.role || step.component) + ":" + step.seq;
  (step.children || []).forEach(assignSeq);
  return step;
}

function llm(opts) {
  const timing = span(opts.ms ?? 3000, opts.gap ?? 160);
  const role = opts.role || opts.component || "hyper-agent";
  const step = {
    step_id: "",
    seq: 0,
    component: opts.component || role,
    role,
    phase: opts.phase,
    kind: "llm",
    name: opts.name || "Qwen/Qwen3.8-27B-FP8",
    title: opts.title,
    ...timing,
    status: opts.status || "ok",
    outcome: opts.outcome || "completed",
    reasoning: opts.reasoning === undefined ? null : opts.reasoning,
    content: opts.content || null,
    model: opts.model || "Qwen/Qwen3.8-27B-FP8",
    finish_reason: opts.finish_reason || (opts.tool ? "tool_calls" : "stop"),
    decision: opts.decision || null,
    tool_calls: [],
    feedback: opts.feedback || [],
    artifacts: opts.artifacts || [],
    children: [],
  };
  if (opts.tool) {
    const toolMs = opts.tool.ms ?? 220;
    const toolTiming = within(step, opts.toolOffset ?? Math.max(400, (opts.ms ?? 3000) - toolMs - 120), toolMs);
    // Long-running tools (planner solve, navigation) push the mission clock
    // forward so later steps never overlap the tool window.
    cursor = Math.max(cursor, Date.parse(toolTiming.finished_at) + 90);
    step.tool_calls.push({
      name: opts.tool.name,
      args: opts.tool.args || {},
      result: opts.tool.error ? null : opts.tool.result ?? null,
      error: opts.tool.error || null,
      duration_ms: toolTiming.duration_ms,
    });
    step.children.push({
      step_id: "",
      seq: 0,
      component: step.component,
      role: step.role,
      phase: step.phase,
      kind: "tool",
      name: opts.tool.name,
      title: opts.tool.name,
      ...toolTiming,
      status: opts.tool.error ? "error" : "ok",
      outcome: opts.tool.error ? "error" : "completed",
      reasoning: null,
      content: null,
      model: null,
      finish_reason: null,
      decision: null,
      tool_calls: [],
      feedback: [],
      artifacts: opts.tool.artifacts || [],
      children: [],
    });
  }
  pending.push(step);
  return step;
}

// Standalone op-log decision (no matching debug record).
function decision(opts) {
  const timing = span(opts.ms ?? 40, opts.gap ?? 120);
  const step = {
    step_id: "",
    seq: 0,
    component: opts.component,
    role: opts.component,
    phase: opts.phase,
    kind: "decision",
    name: opts.name,
    title: opts.title,
    ...timing,
    status: "ok",
    outcome: opts.outcome,
    reasoning: null,
    content: null,
    model: null,
    finish_reason: null,
    decision: { event_kind: opts.name, outcome: opts.outcome, details: opts.details || {} },
    tool_calls: [],
    feedback: [],
    artifacts: [],
    children: [],
  };
  pending.push(step);
  return step;
}

// Standalone transport feedback (no matching debug record).
function feedback(opts) {
  const timing = span(opts.ms ?? 30, opts.gap ?? 700);
  const step = {
    step_id: "",
    seq: 0,
    component: opts.component || "maneuver-control",
    role: opts.component || "maneuver-control",
    phase: opts.phase,
    kind: "feedback",
    name: opts.name || "maneuver-feedback",
    title: opts.title,
    ...timing,
    status: "ok",
    outcome: opts.outcome || "completed",
    reasoning: null,
    content: null,
    model: null,
    finish_reason: null,
    decision: null,
    tool_calls: [],
    feedback: opts.entries,
    artifacts: [],
    children: [],
  };
  pending.push(step);
  return step;
}

/* ------------------------------------------------------------------ */
/* planning-intent                                                    */
/* ------------------------------------------------------------------ */

llm({
  phase: "planning-intent",
  title: "Plan the mission approach",
  ms: 4210,
  reasoning: `Let me start by understanding what this mission actually asks for. The operator message says "Patrol sector, investigate contacts" — that is a patrol pattern where the value comes from accounting for events reported in the sector, not from covering area uniformly. Before I commit to a planner profile I want to set up the workflow todos so each gated stage is explicit: parse the mission, read the relevant skills, record the planning intent, load context, generate assets, run the planner, generate the statechart, then hand off.

The mission file should tell me the sector, the event report, and any hard constraints (battery reserve, confidence thresholds for investigation). I already know from the operator pre-brief that this is mission:demo with authority demo-operator, but I will not rely on that — the mission file is the source of truth and the intent record has to hash against it.

Plan: (1) write the todo list, (2) read the mission-parsing skill, (3) read the temporal-planning skill, (4) call record_planning_intent with planner minizinc and profile temporal. If the intent record comes back rejected, the fallback is profile "coverage" with the same planner, but I expect temporal to be accepted because feasibility here depends on event times and dwell windows, not just geometry.`,
  content: "I'll start by setting up the workflow todo list and reading the mission parsing skill before recording the planning intent.",
  tool: {
    name: "write_todos",
    ms: 46,
    args: {
      todos: [
        { stage: 1, action: "Parse mission file and confirm planning pattern", status: "in_progress" },
        { stage: 2, action: "Record planning intent (planner + profile)", status: "pending" },
        { stage: 3, action: "Load planning context snapshot", status: "pending" },
        { stage: 4, action: "Generate and persist planner assets (model.mzn, data.dzn)", status: "pending" },
        { stage: 5, action: "Execute planner with retry loop", status: "pending" },
        { stage: 6, action: "Generate maneuver statechart", status: "pending" },
        { stage: 7, action: "Hand off to maneuver control", status: "pending" },
      ],
    },
    result: { ok: true, todos_written: 7 },
  },
});

llm({
  phase: "planning-intent",
  title: "Read the mission parsing skill",
  ms: 2380,
  reasoning: `Stage 1. I need the mission-parsing skill before I interpret the mission file, because the intent record requires a mission_pattern string and the skill defines the controlled vocabulary. If I guess the pattern name the intent gate will reject it later and I will burn a planner attempt on a bad premise.

I will read skills/mission-parsing/SKILL.md first, then the mission file itself. One consideration: the skill may reference the temporal-planning skill for the actual constraint encoding — if so I will read that next, before recording intent, so the rationale I write into the intent record reflects the real capture rule rather than a paraphrase.`,
  content: "Reading the mission-parsing skill to get the controlled vocabulary for the intent record.",
  tool: {
    name: "read_file",
    ms: 38,
    args: { file_path: "/opt/onr/skills/mission-parsing/SKILL.md" },
    result: {
      path: "/opt/onr/skills/mission-parsing/SKILL.md",
      lines: 84,
      excerpt: "# Mission parsing\n\nClassify the mission into exactly one pattern: report_event_accounting_patrol | area_coverage_patrol | convoy_escort | point_intercept …",
    },
  },
});

llm({
  phase: "planning-intent",
  title: "Record the planning intent",
  ms: 5140,
  reasoning: `This mission is a patrol for event report accounting. According to the mission parsing skill this maps to the temporal MiniZinc intent: select a route and dwell schedule that maximizes captured information gain. The capture rule is strict — an event is captured only if its time falls within a selected dwell interval AND its position is within the FoV radius of that stop. The scaled value of a captured event is 1 - probability_risk, and the objective is the sum over captured events.

That rules out the coverage profile: coverage maximizes swept area and ignores event timing entirely, which would miss the 40% of events that cluster in the first ninety seconds. The temporal profile with planner minizinc is the right call. Stop count is fixed at 4 by the mission constraints; dwell_ticks at 2 (four seconds) matches the sensor integration time in the environment datasheet.

Now I need to call record_planning_intent with: mission_id "mission:demo", source_authority "demo-operator", a concise objective, planning_profile "temporal", planner_id "minizinc", a public rationale, details carrying mission_pattern / capture_rule / value_rule / source_roles, and a short reflection. The details block is what the planner-execution gate replays later, so I am keeping it factual — no prose, only the facts the executor needs to validate my later assets against.`,
  content: "Mission parsed as a report event-accounting patrol. Recording planning intent with the temporal profile and the minizinc planner.",
  tool: {
    name: "record_planning_intent",
    ms: 312,
    args: {
      mission_id: "mission:demo",
      source_authority: "demo-operator",
      objective: "Patrol the sector and select a route and dwell schedule that maximizes captured information gain so the events in the report are accounted for.",
      planning_profile: "temporal",
      planner_id: "minizinc",
      rationale: "Feasibility and objective value depend on the drone's position at event times, travel timing, and FoV coverage, which requires temporal optimization rather than area coverage.",
      details: {
        mission_pattern: "report_event_accounting_patrol",
        capture_rule: "An event is captured only when its time lies in a selected dwell interval and its position is within the FoV radius of that stop.",
        value_rule: "Each captured event has scaled value 1 - probability_risk; maximize the sum over captured events.",
        source_roles: ["hyper-agent"],
        stop_count: 4,
      },
      reflection: "Intent is grounded in the mission file and the parsing skill vocabulary; no assumptions beyond the operator constraints.",
    },
    result: {
      status: "accepted",
      decision_id: "planner-choice:mission:demo:8f2071e6914bfe",
      planning_intent_sha256: "9c2f…a41d",
    },
  },
  decision: {
    event_kind: "planning-intent",
    outcome: "completed",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      decision_id: "planner-choice:mission:demo:8f2071e6914bfe",
      planner_id: "minizinc",
      planning_profile: "temporal",
      mission_pattern: "report_event_accounting_patrol",
      stop_count: 4,
    },
  },
});

/* ------------------------------------------------------------------ */
/* planning-context                                                   */
/* ------------------------------------------------------------------ */

llm({
  phase: "planning-context",
  title: "Load the planning context",
  ms: 2870,
  reasoning: `Intent is recorded and accepted. Stage 3 is the context load — this is a gated tool, so it will only return the snapshot if my intent record is on file. The context gives me three things I cannot invent: the drone's start state (position, time zero), the validated environment data reference for the event report, and the operational scene graph revision I must plan against.

I am not going to touch the planner assets until this returns, because the data.dzn arrays (event times, positions, entity risk) all derive from the environment snapshot. If the snapshot revision is stale relative to the transport topic, the load will say so and I will wait for the newer revision rather than plan against old events.`,
  content: "Loading the planning context snapshot and scene graph authorized by the recorded intent.",
  tool: {
    name: "load_planning_context",
    ms: 388,
    args: { mission_id: "mission:demo" },
    result: {
      mission_id: "mission:demo",
      snapshot_id: "mission-snapshot:mission:demo:2",
      environment_data_reference: "environment-data:mission:demo:fce73297",
      drone_start: { time: 0, x: 46, y: -86 },
      event_count: 12,
      entity_count: 6,
      constraints: { max_velocity: 20, fov_radius: 30, dwell_seconds: 4, battery_reserve_pct: 25 },
    },
  },
  decision: {
    event_kind: "planning-context",
    outcome: "completed",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      snapshot_id: "mission-snapshot:mission:demo:2",
      environment_data_reference: "environment-data:mission:demo:fce73297",
      event_count: 12,
      revision: 2,
    },
  },
});

llm({
  phase: "planning-context",
  title: "Read the temporal planning skill",
  ms: 2210,
  gap: 200,
  reasoning: `Before writing model.mzn I am reading the temporal-planning skill end to end. The capture-rule encoding has one subtlety I keep getting wrong from memory: the dwell interval is [t, t + dwell_ticks], inclusive at both ends, and an event exactly at the boundary counts. If I encode it as a half-open interval the solver will legally drop boundary events and inflate the objective versus what the executor's validator accepts — that mismatch is exactly what the attempt gate checks, so I want the model and the validator to agree by construction.

The skill also prescribes the output contract: a single JSON object with an assignments array, one entry per stop, carrying maneuver_id, start, duration, and a parameters block with wait/move decomposition. The maneuver agent consumes that shape directly, so I will use the skill's output statement verbatim rather than designing my own.`,
  content: "Reading the temporal-planning skill to match the model encoding to the executor's validator.",
  tool: {
    name: "read_file",
    ms: 34,
    args: { file_path: "/opt/onr/skills/temporal-planning/SKILL.md" },
    result: {
      path: "/opt/onr/skills/temporal-planning/SKILL.md",
      lines: 132,
      excerpt: "# Temporal planning\n\nDwell interval is closed: [t, t + dwell_ticks]. Boundary events count as captured …",
    },
  },
});

/* ------------------------------------------------------------------ */
/* planner-assets                                                     */
/* ------------------------------------------------------------------ */

llm({
  phase: "planner-assets",
  title: "Draft model.mzn",
  ms: 6120,
  reasoning: `Now the model. Decision variables: selected_opportunity[stop] ∈ OPPORTUNITIES for each of the 4 stops, plus captured[event] ∈ bool derived from coverage. I am treating opportunities as synonymous with events — each event defines one candidate dwell position/time — because the report accounting pattern never benefits from dwelling where nothing happens. That keeps the search space at 12 candidates choose 4 ordered stops, trivially solvable, and the travel-time precomputation (travel_from_start, travel_between) makes the reachability constraints linear lookups rather than nonlinear arithmetic in the solver.

Constraints, in order: the first stop must be reachable from the drone start; consecutive stops must satisfy can_follow (strictly increasing event index, enough ticks to travel after dwell); captured[e] ↔ ∃stop covering e, where covers uses the CLOSED dwell interval per the skill; objective = Σ (risk_scale − event_risk) · captured, tie-broken by preferring earlier stops via the tie_bound trick so the plan is deterministic across solver runs.

The output statement emits the maneuver agent's assignment JSON directly. I am writing the full file in one write_file call rather than incrementally — the model is small enough that a rewrite costs nothing and avoids half-applied edits.`,
  content: "model.mzn drafted: 4 stops over 12 event-opportunities, closed dwell intervals, tie-broken objective, assignment JSON output.",
  tool: {
    name: "write_file",
    ms: 96,
    args: { file_path: "var/planner-artifacts/workspace/001/model.mzn", bytes: 5422 },
    result: { ok: true, path: "var/planner-artifacts/workspace/001/model.mzn", bytes_written: 5422 },
  },
  artifacts: [{ kind: "model.mzn", ref: "workspace/001/model.mzn", label: "MiniZinc model (attempt 1 draft)" }],
});

llm({
  phase: "planner-assets",
  title: "Draft data.dzn",
  ms: 4480,
  reasoning: `Data file next. From the context snapshot: event_count 12, stop_count 4, entity_count 6, horizon_ticks 600 at time_scale 2 (so 300 seconds of mission horizon), dwell_ticks 2. Event arrays come straight from the environment data reference — times in seconds, positions in meters, entity indices 1-based. Entity risk probabilities are the validated belief outputs, not raw sensor confidences; the context load already resolved that reference so I am copying values, not re-deriving them.

One decision: maneuver ids are symbolic ("patrol-stop-1..4") because the maneuver agent maps ids to concrete commands at handoff. Drone start (t=0, x=46, y=−86) from the snapshot. max_velocity 20 m/s and fov_radius 30 m from the platform constraints. risk_scale 1000 to keep the objective integral.

I will write the scalars and the maneuver_id array first, then fill the four event arrays in one edit pass each. If the executor rejects the data, the most likely culprit is an array length mismatch against event_count, so I am counting entries as I go.`,
  content: "data.dzn drafted: scalars and maneuver ids first, then the event arrays straight from the environment snapshot.",
  tool: {
    name: "write_file",
    ms: 74,
    args: { file_path: "var/planner-artifacts/workspace/001/data.dzn", bytes: 1284 },
    result: { ok: true, path: "var/planner-artifacts/workspace/001/data.dzn", bytes_written: 1284 },
  },
  artifacts: [{ kind: "data.dzn", ref: "workspace/001/data.dzn", label: "MiniZinc data (attempt 1 draft)" }],
});

llm({
  phase: "planner-assets",
  title: "Persist planner assets",
  ms: 1860,
  reasoning: `Both files are drafted. Persisting them through the gated tool rather than leaving them in the scratch workspace — persist_planner_assets hashes the pair, registers them under the mission, and returns the asset references the executor will resolve. This is the point of no return for attempt 1: whatever I persist now is what the validator sees.

Quick self-check before persisting: the model declares all six scalar parameters the data file provides; every array in data.dzn is indexed by a set declared in the model; the output statement references only declared symbols. All three check out.`,
  content: "Persisting the model/data pair and registering the asset references for the executor.",
  tool: {
    name: "persist_planner_assets",
    ms: 268,
    args: {
      mission_id: "mission:demo",
      model_path: "var/planner-artifacts/workspace/001/model.mzn",
      data_path: "var/planner-artifacts/workspace/001/data.dzn",
    },
    result: {
      status: "accepted",
      generated_assets: [
        { kind: "model.mzn", ref: "workspace/001/model.mzn", sha256: "b3c1…77e0" },
        { kind: "data.dzn", ref: "workspace/001/data.dzn", sha256: "51ad…c2f9" },
      ],
    },
  },
  artifacts: [
    { kind: "model.mzn", ref: "workspace/001/model.mzn", label: "MiniZinc model (attempt 1)" },
    { kind: "data.dzn", ref: "workspace/001/data.dzn", label: "MiniZinc data (attempt 1)" },
  ],
});

/* ------------------------------------------------------------------ */
/* planner-execution — attempt 1 rejected, repaired, attempt 2 ok      */
/* ------------------------------------------------------------------ */

llm({
  phase: "planner-execution",
  title: "Planner execution (attempt 1)",
  ms: 3240,
  gap: 240,
  reasoning: `Assets persisted. Attempt 1 of at most 3. The executor will validate the model against the intent record, solve with the bundled MiniZinc, and normalize the output into the maneuver assignment shape. Expected solve time is small — 12 opportunities, 4 stops — so anything over ~30 seconds would itself be a signal something is wrong with the encoding.

If it comes back rejected, the rejection payload will name the failed checks and I repair exactly those — no speculative rewrites.`,
  content: "Submitting attempt 1 to the planner executor.",
  tool: {
    name: "planner_executor",
    ms: 9640,
    toolOffset: 400,
    args: { mission_id: "mission:demo", attempt: 1, planner_id: "minizinc" },
    result: {
      status: "rejected",
      attempt: 1,
      solve_ms: 1180,
      violations: [
        { check: "dwell_interval_closed", detail: "covers[] uses event_time <= opportunity_time + dwell_ticks - 1; validator requires the closed interval [t, t + dwell_ticks]." },
        { check: "objective_tie_break", detail: "tie_bound must be the literal expression stop_count * horizon_ticks + 1 so the bound can be checked symbolically." },
      ],
    },
  },
  outcome: "rejected",
  decision: {
    event_kind: "planner-execution",
    outcome: "rejected",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      attempt: 1,
      max_attempts: 3,
      checks_failed: ["dwell_interval_closed", "objective_tie_break"],
      checks_passed: ["intent_hash_match", "asset_schema", "solve_completed", "output_parse"],
    },
  },
  feedback: [
    {
      kind: "planner-repair",
      payload: {
        attempt: 1,
        guidance: "Make the dwell interval closed at both ends and write tie_bound as stop_count * horizon_ticks + 1. Both fixes are in model.mzn; data.dzn is unchanged.",
        violations: [
          { check: "dwell_interval_closed", hint: "Use event_time[event] <= opportunity_time[opportunity] + dwell_ticks (drop the - 1)." },
          { check: "objective_tie_break", hint: "tie_bound = stop_count * horizon_ticks + 1, written literally." },
        ],
      },
    },
  ],
  artifacts: [
    { kind: "model.mzn", ref: "workspace/001/model.mzn", label: "MiniZinc model (attempt 1, rejected)" },
    { kind: "data.dzn", ref: "workspace/001/data.dzn", label: "MiniZinc data (attempt 1, rejected)" },
  ],
});

llm({
  phase: "planner-execution",
  title: "Repair model.mzn",
  ms: 3940,
  reasoning: `The gate rejected attempt 1 on two checks, and honestly both are fair. First: I encoded the dwell interval as event_time ≤ opportunity_time + dwell_ticks − 1 — a half-open interval — despite having read the skill that morning precisely to avoid this. The skill is unambiguous: closed interval, boundary events count. The fix is deleting the "− 1" in the covers[] comprehension.

Second: tie_bound. I set it to the precomputed constant 2401, which numerically dominates any sum of 4 opportunity times. But the validator recomputes the bound from the revision-2 snapshot and wants the literal expression stop_count * horizon_ticks + 1 in the model so it can be checked symbolically, not my constant. Fair enough: replace the constant with the expression.

Two surgical edits to model.mzn. data.dzn is untouched — the rejection named no data checks. Then attempt 2.`,
  content: "Two repairs to model.mzn: closed dwell interval in covers[], and tie_bound as the literal expression the validator checks.",
  tool: {
    name: "edit_file",
    ms: 84,
    args: {
      file_path: "var/planner-artifacts/workspace/001/model.mzn",
      edits: [
        { find: "event_time[event] <= opportunity_time[opportunity] + dwell_ticks - 1", replace: "event_time[event] <= opportunity_time[opportunity] + dwell_ticks" },
        { find: "int: tie_bound = 2401;", replace: "int: tie_bound = stop_count * horizon_ticks + 1;" },
      ],
    },
    result: { ok: true, edits_applied: 2 },
  },
});

llm({
  phase: "planner-execution",
  title: "Planner execution (attempt 2)",
  ms: 2980,
  gap: 240,
  reasoning: `Attempt 2 with the repaired model. The two failed checks now hold by construction: covers[] matches the closed interval from the skill verbatim, and tie_bound is the exact expression the validator symbolically compares. Everything else passed on attempt 1, so unless the repair introduced a syntax error this should be accepted.

After acceptance the executor normalizes the solution into maneuver assignments and publishes the plan on the normalized-plans topic. I will read the objective value to sanity-check coverage before moving to the statechart: anything under ~9.5 of the 12.0 maximum would mean the route is leaving easy events on the table.`,
  content: "Submitting attempt 2 to the planner executor.",
  tool: {
    name: "planner_executor",
    ms: 11420,
    toolOffset: 380,
    args: { mission_id: "mission:demo", attempt: 2, planner_id: "minizinc" },
    result: {
      status: "accepted",
      attempt: 2,
      solve_ms: 1294,
      objective_value: 9.853,
      objective_max: 12.0,
      stops: 4,
      plan_revision: 2,
      solution: {
        assignments: [
          { maneuver_id: "patrol-stop-1", start: 18, duration: 2, captures: [3, 4, 5] },
          { maneuver_id: "patrol-stop-2", start: 122, duration: 2, captures: [6, 7, 8] },
          { maneuver_id: "patrol-stop-3", start: 196, duration: 2, captures: [9, 10] },
          { maneuver_id: "patrol-stop-4", start: 261, duration: 2, captures: [11, 12] },
        ],
      },
    },
  },
  outcome: "accepted",
  decision: {
    event_kind: "planner-execution",
    outcome: "accepted",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      attempt: 2,
      objective_value: 9.853,
      objective_max: 12.0,
      events_captured_planned: 11,
      events_total: 12,
      plan_revision: 2,
    },
  },
  artifacts: [
    { kind: "model.mzn", ref: "workspace/002/model.mzn", label: "MiniZinc model (attempt 2, accepted)" },
    { kind: "data.dzn", ref: "workspace/002/data.dzn", label: "MiniZinc data (attempt 2, accepted)" },
  ],
});

/* ------------------------------------------------------------------ */
/* statechart-generation — draft 1 failed, fixed, accepted             */
/* ------------------------------------------------------------------ */

llm({
  phase: "statechart-generation",
  title: "Statechart generation (attempt 1)",
  ms: 5360,
  reasoning: `Plan accepted with 11 of 12 events covered — good enough; the one miss is event 1 at t=0.5s which is unreachable from the start position at max velocity, so no route could have captured it. Now the statechart that maneuver-control will execute. The shape follows the patrol loop: navigate → search → (contact ? investigate → classify) → advance to next stop → return when the route is exhausted.

States: navigate, search, investigate, advance, return, complete. Events: ARRIVED, CONTACT_FOUND, AREA_CLEAR, CLASSIFIED, NEXT_STOP, ROUTE_COMPLETE, DOCKED, plus ABORT from navigate for safety. The handoff requires the chart to reference the four maneuver ids from the plan so the executor can bind them.

Writing draft 1 now. The validator checks reachability of the final state and that every event has a defined transition in every state that can raise it — the second rule is where drafts usually fail.`,
  content: "Statechart draft 1 written: 6 states covering the patrol loop.",
  tool: {
    name: "write_file",
    ms: 88,
    args: { file_path: "var/planner-artifacts/statechart-attempts/001/statechart.json", bytes: 1188 },
    result: { ok: true, path: "var/planner-artifacts/statechart-attempts/001/statechart.json", bytes_written: 1188 },
  },
  outcome: "failed",
  decision: {
    event_kind: "statechart-generation",
    outcome: "failed",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      attempt: 1,
      validator: "fsm-statechart-schema@1",
      error: "State 'investigate' can raise SENSOR_ERROR but defines no transition for it; the event is unhandled on that path.",
      unhandled: [{ state: "investigate", event: "SENSOR_ERROR" }],
    },
  },
  artifacts: [{ kind: "statechart.json", ref: "statechart-attempts/001/statechart.json", label: "Statechart (attempt 1, failed validation)" }],
});

llm({
  phase: "statechart-generation",
  title: "Statechart generation (attempt 2)",
  ms: 3620,
  reasoning: `The validator caught a real gap: investigate raises SENSOR_ERROR when the payload times out, and draft 1 had no transition for it — the chart would have wedged in investigate with the mission clock running. That is exactly the failure mode the statechart gate exists for.

Fix: add investigate_retry and wire SENSOR_ERROR → investigate_retry from investigate, CLASSIFIED → advance and SENSOR_ERROR → advance from investigate_retry (one retry, then accept the miss and move on). I am deliberately not looping SENSOR_ERROR back into investigate — a retry storm against a faulty payload is how you drain a battery at stop 3 of 4. The rest of the chart validated clean, so this is one transition table edit plus the new state.`,
  content: "Added investigate_retry and wired SENSOR_ERROR handling; single retry, then advance on persistent failure.",
  tool: {
    name: "edit_file",
    ms: 66,
    args: {
      file_path: "var/planner-artifacts/statechart-attempts/002/accepted-statechart.json",
      edits: [
        { add_state: "investigate_retry", on: { CLASSIFIED: "advance", SENSOR_ERROR: "advance" } },
        { add_transition: { state: "investigate", event: "SENSOR_ERROR", target: "investigate_retry" } },
      ],
    },
    result: { ok: true, edits_applied: 2 },
  },
  outcome: "accepted",
  decision: {
    event_kind: "statechart-generation",
    outcome: "accepted",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      attempt: 2,
      statechart_id: "maneuver-execution@2",
      states: 7,
      transitions: 11,
      final_state_reachable: true,
    },
  },
  artifacts: [{ kind: "accepted-statechart.json", ref: "statechart-attempts/002/accepted-statechart.json", label: "Statechart (attempt 2, accepted)" }],
});

/* ------------------------------------------------------------------ */
/* maneuver-handoff                                                   */
/* ------------------------------------------------------------------ */

llm({
  phase: "maneuver-handoff",
  title: "Hand off to maneuver control",
  ms: 4120,
  reasoning: `Everything maneuver-control needs is now on record: the accepted plan (revision 2), the accepted statechart (maneuver-execution@2), and the environment data reference. The handoff call bundles them into one normalized plan envelope, publishes it on the normalized-plans topic, and transfers decision authority. After this call the hyper-agent's job is done unless a replan request comes back.

I am including the investigation policy in the handoff parameters — investigate contacts with confidence ≥ 0.5, single sensor retry, return-to-base at 25% battery reserve — because those came from operator constraints and the maneuver agent must not have to guess them. The execution record on the fsm topic will tell me how the run actually tracked the plan.`,
  content: "Handing off: normalized plan revision 2, statechart maneuver-execution@2, and the operator investigation policy.",
  tool: {
    name: "handoff_to_maneuver",
    ms: 402,
    args: {
      mission_id: "mission:demo",
      plan_revision: 2,
      statechart_id: "maneuver-execution@2",
      policy: { investigate_confidence_min: 0.5, sensor_retries: 1, battery_reserve_pct: 25 },
    },
    result: { status: "accepted", maneuver_id: "maneuver:mission:demo:1", topic: "normalized-plans" },
  },
  decision: {
    event_kind: "maneuver-handoff",
    outcome: "completed",
    details: {
      correlation_id: "planning-run:mission:demo:1",
      maneuver_id: "maneuver:mission:demo:1",
      plan_revision: 2,
      statechart_id: "maneuver-execution@2",
      stops: 4,
    },
  },
  artifacts: [{ kind: "normalized-plan.json", ref: "workspace/002/normalized-plan.json", label: "Normalized plan (revision 2)" }],
});

/* ------------------------------------------------------------------ */
/* heartbeat — maneuver-control execution                              */
/* ------------------------------------------------------------------ */

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Navigate to patrol-stop-1",
  ms: 2840,
  reasoning: `Authority received. Plan revision 2 has four stops; the first dwell opens at t=18 ticks and the transit from the start position is short, so I am navigating now to arrive with margin. Speed capped at 20 m/s per platform limits; the route threads between the two belief-mapped hazard cells rather than around them, which is what the plan's wait/move parameters assume.

After arrival I will run search_area for the full dwell window — the plan captures events 3, 4 and 5 at this stop, all low-risk entities, so the expected yield is high.`,
  content: "Navigating to patrol-stop-1 to open the first dwell window at t=18.",
  tool: {
    name: "navigate",
    ms: 1640,
    toolOffset: 600,
    args: { maneuver_id: "patrol-stop-1", target: { x: 148, y: 188 }, max_velocity: 20, arrive_by_tick: 18 },
    result: { ok: true, arrived_tick: 16, position: { x: 148, y: 188 }, distance_m: 102.4 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Position update — on station at stop 1",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "on_station", position: { x: 148, y: 188 }, tick: 16, battery_pct: 82 } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Search area at stop 1",
  ms: 3120,
  reasoning: null,
  content: "Running search_area across the dwell window at patrol-stop-1.",
  tool: {
    name: "search_area",
    ms: 2210,
    toolOffset: 500,
    args: { maneuver_id: "patrol-stop-1", radius_m: 30, dwell_ticks: 2, sensor: "eo_wide" },
    result: {
      ok: true,
      contacts: [{ contact_id: "contact-041", position: { x: 151, y: 196 }, confidence: 0.81, bearing_deg: 24 }],
      events_captured: [3, 4, 5],
    },
  },
});

feedback({
  phase: "heartbeat",
  title: "Contact detected — contact-041 (0.81)",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "contact_detected", contact_id: "contact-041", confidence: 0.81, sensor: "eo_wide", position: { x: 151, y: 196 }, tick: 21 } },
  ],
});

decision({
  phase: "heartbeat",
  component: "maneuver-control",
  name: "investigate-contact",
  title: "Investigate contact-041 — confidence 0.81 ≥ 0.5",
  outcome: "proceed",
  details: { contact_id: "contact-041", confidence: 0.81, threshold: 0.5, retries_available: 1 },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Investigate contact-041",
  ms: 3480,
  status: "error",
  outcome: "tool-error",
  reasoning: `Contact-041 came in at confidence 0.81, well over the 0.5 investigation threshold, and it sits inside the stop-1 FoV radius so investigating does not break the dwell schedule. Switching to the classification sensor and closing to investigation standoff.

This is the first physical-world test of the plan — the planner assumed events here were low-risk, so a clean classification would confirm the belief model's risk ranking for this sector.`,
  content: "Investigating contact-041 with the classification sensor.",
  tool: {
    name: "investigate",
    ms: 3000,
    toolOffset: 420,
    args: { contact_id: "contact-041", sensor: "eo_classify", dwell_s: 3, standoff_m: 12 },
    error: "SensorTimeout: eo_classify produced no frame within 3000 ms (payload busy, retry budget 1)",
  },
});

feedback({
  phase: "heartbeat",
  title: "Tool error acknowledged — retry scheduled",
  outcome: "actionable",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "tool_error", tool: "investigate", contact_id: "contact-041", error: "SensorTimeout", retries_remaining: 1, policy: "single retry with reduced dwell, then advance" } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Retry investigate contact-041 (reduced dwell)",
  ms: 3260,
  reasoning: `The classify payload timed out — no frame in 3 seconds, payload reported busy. Policy allows exactly one retry, at reduced dwell so the stop-1 schedule does not slip: the dwell window closes at t=20 and I have until the next leg's travel deadline to either classify or accept the miss.

Dropping dwell_s from 3 to 1.5 and keeping standoff. If this retry also times out I mark contact-041 as unclassified-but-logged and advance — the statechart's SENSOR_ERROR → advance path exists for exactly this, and chasing a stuck sensor is how a mission trades four stops for one.`,
  content: "Retrying investigate with reduced dwell (1.5 s); will advance on a second timeout.",
  tool: {
    name: "investigate",
    ms: 1980,
    toolOffset: 400,
    args: { contact_id: "contact-041", sensor: "eo_classify", dwell_s: 1.5, standoff_m: 12 },
    result: { ok: true, classification: "vessel-small", confidence: 0.72, frames: 3 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Contact-041 classified — vessel-small (0.72)",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "contact_classified", contact_id: "contact-041", classification: "vessel-small", confidence: 0.72, tick: 24 } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Navigate to patrol-stop-2",
  ms: 2620,
  reasoning: null,
  content: "Transiting to patrol-stop-2 for the second dwell window at t=122.",
  tool: {
    name: "navigate",
    ms: 2480,
    toolOffset: 500,
    args: { maneuver_id: "patrol-stop-2", target: { x: 306, y: -17 }, max_velocity: 20, arrive_by_tick: 122 },
    result: { ok: true, arrived_tick: 120, position: { x: 306, y: -17 }, distance_m: 266.9 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Stop 1 area cleared",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "area_cleared", maneuver_stop: "patrol-stop-1", events_captured: [3, 4, 5], contacts: ["contact-041"], departed_tick: 26 } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Search area at stop 2",
  ms: 2880,
  reasoning: `On station at stop 2 with two ticks of margin. This window should capture events 6, 7 and 8 — all from the same low-risk entity cluster per the belief map, so I expect contacts at low confidence or none at all. Running the full dwell sweep regardless: the capture rule credits events inside the FoV whether or not a contact is declared, and skipping the sweep would show up as a gap in the execution record.`,
  content: "Sweeping the stop-2 window for the full dwell.",
  tool: {
    name: "search_area",
    ms: 2140,
    toolOffset: 480,
    args: { maneuver_id: "patrol-stop-2", radius_m: 30, dwell_ticks: 2, sensor: "eo_wide" },
    result: { ok: true, contacts: [], events_captured: [6, 7, 8] },
  },
});

decision({
  phase: "heartbeat",
  component: "maneuver-control",
  name: "skip-investigation",
  title: "No contacts at stop 2 — advance",
  outcome: "skip",
  details: { maneuver_stop: "patrol-stop-2", contacts: 0, events_captured: [6, 7, 8] },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Navigate to patrol-stop-3",
  ms: 2740,
  reasoning: `Two stops down, two to go, battery at 64% — well above the reserve floor. Stop 3 is the longest leg of the route and the window opens at t=196, so I am leaving immediately and cruising at the plan's assumed speed rather than sprinting; the plan's wait parameters already priced in the transit time and arriving early buys nothing.`,
  content: "Transiting to patrol-stop-3; window opens at t=196.",
  tool: {
    name: "navigate",
    ms: 3120,
    toolOffset: 500,
    args: { maneuver_id: "patrol-stop-3", target: { x: -676, y: -1353 }, max_velocity: 20, arrive_by_tick: 196 },
    result: { ok: true, arrived_tick: 195, position: { x: -676, y: -1353 }, distance_m: 1488.2 },
  },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Search area at stop 3",
  ms: 2960,
  reasoning: null,
  content: "Sweeping the stop-3 window.",
  tool: {
    name: "search_area",
    ms: 2260,
    toolOffset: 480,
    args: { maneuver_id: "patrol-stop-3", radius_m: 30, dwell_ticks: 2, sensor: "eo_wide" },
    result: {
      ok: true,
      contacts: [{ contact_id: "contact-088", position: { x: -668, y: -1341 }, confidence: 0.64, bearing_deg: 311 }],
      events_captured: [9, 10],
    },
  },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Investigate contact-088",
  ms: 2540,
  reasoning: `Contact-088 at 0.64 confidence — above threshold, inside the FoV, and the classify payload has been healthy since the stop-1 timeout. One consideration: we are far from base with two stops to go, so every extra second on station is battery I am not spending on the return leg. The investigation fits inside the dwell margin, so proceed.`,
  content: "Investigating contact-088.",
  tool: {
    name: "investigate",
    ms: 1740,
    toolOffset: 420,
    args: { contact_id: "contact-088", sensor: "eo_classify", dwell_s: 3, standoff_m: 12 },
    result: { ok: true, classification: "vessel-small", confidence: 0.78, frames: 5 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Contact-088 classified — vessel-small (0.78)",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "contact_classified", contact_id: "contact-088", classification: "vessel-small", confidence: 0.78, tick: 201 } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Navigate to patrol-stop-4",
  ms: 2480,
  reasoning: `Final stop. The leg is short and the window opens at t=261. After this stop the route is exhausted and the statechart takes ROUTE_COMPLETE → return. Battery projection has us docking at about 29%, above the 25% reserve but with no room for a second sensor timeout — if the classify payload stalls again at stop 4, policy says advance immediately rather than retry.`,
  content: "Transiting to patrol-stop-4, the final stop.",
  tool: {
    name: "navigate",
    ms: 1820,
    toolOffset: 480,
    args: { maneuver_id: "patrol-stop-4", target: { x: 21, y: -318 }, max_velocity: 20, arrive_by_tick: 261 },
    result: { ok: true, arrived_tick: 259, position: { x: 21, y: -318 }, distance_m: 1113.0 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Battery advisory — 31% remaining",
  outcome: "actionable",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "resource_advisory", battery_pct: 31, reserve_pct: 25, projected_dock_pct: 29, guidance: "No retries on stop 4; proceed directly to return on ROUTE_COMPLETE." } },
  ],
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Search area at stop 4",
  ms: 2760,
  reasoning: `Final dwell window. The plan captures events 11 and 12 here — the last two in the report. With the battery advisory in effect this is a by-the-book sweep: full dwell, no extended loiter, classify only what clears the threshold.`,
  content: "Sweeping the stop-4 window.",
  tool: {
    name: "search_area",
    ms: 2180,
    toolOffset: 460,
    args: { maneuver_id: "patrol-stop-4", radius_m: 30, dwell_ticks: 2, sensor: "eo_wide" },
    result: {
      ok: true,
      contacts: [{ contact_id: "contact-112", position: { x: 25, y: -309 }, confidence: 0.58, bearing_deg: 102 }],
      events_captured: [11, 12],
    },
  },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Investigate contact-112",
  ms: 2380,
  reasoning: `Contact-112 at 0.58 — barely over threshold, but over threshold. The advisory bars retries, not the first attempt, and the payload has been clean for two investigations running. One quick classification, then home.`,
  content: "Investigating contact-112, then returning to base.",
  tool: {
    name: "investigate",
    ms: 1520,
    toolOffset: 400,
    args: { contact_id: "contact-112", sensor: "eo_classify", dwell_s: 2, standoff_m: 12 },
    result: { ok: true, classification: "floating-debris", confidence: 0.66, frames: 4 },
  },
});

llm({
  phase: "heartbeat",
  component: "maneuver-control",
  role: "maneuver-control",
  title: "Return to base",
  ms: 2240,
  reasoning: `Route complete: 11 of 12 events captured, three contacts classified, zero retries outstanding. The only miss is the unreachable t=0.5s event the planner already priced in. Raising ROUTE_COMPLETE and taking the return leg — direct line, no sightseeing.`,
  content: "Route complete. Returning to base.",
  tool: {
    name: "navigate",
    ms: 2680,
    toolOffset: 460,
    args: { maneuver_id: "return", target: { x: 46, y: -86 }, max_velocity: 20 },
    result: { ok: true, docked_tick: 302, battery_pct: 29 },
  },
});

feedback({
  phase: "heartbeat",
  title: "Mission complete — docked at 29% battery",
  entries: [
    { kind: "maneuver-feedback", payload: { maneuver_id: "maneuver:mission:demo:1", lifecycle: "completed", events_captured: 11, events_total: 12, contacts_classified: 3, stops_completed: 4, docked_tick: 302, battery_pct: 29 } },
  ],
});

decision({
  phase: "heartbeat",
  component: "maneuver-control",
  name: "maneuver-complete",
  title: "Maneuver execution complete",
  outcome: "completed",
  details: { maneuver_id: "maneuver:mission:demo:1", plan_revision: 2, events_captured: 11, events_total: 12, deviations: 0 },
});

/* ------------------------------------------------------------------ */

export const MOCK_PHASES = [
  "planning-intent",
  "planning-context",
  "planner-assets",
  "planner-execution",
  "statechart-generation",
  "maneuver-handoff",
  "heartbeat",
];

export function buildSteps() {
  seqCounter = 0;
  return pending.map(assignSeq);
}

export function mockStepsPayload(missionId) {
  const steps = buildSteps().map((s) => ({ ...s }));
  return {
    schema_version: 1,
    mission_id: missionId || "mission:demo",
    generated_at: iso(cursor),
    warnings: [
      "Debug capture was partial for role 'maneuver-control': 3 llm calls have no recorded reasoning.",
    ],
    phases: MOCK_PHASES.slice(),
    steps,
  };
}
