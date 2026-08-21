// Mock /api/run payload — overview + aggregates for the demo mission.
// Aggregates are computed from the mock steps with the same rules as the
// real RunOverview (flattened counts, attempt numbers from artifact refs).

import { buildSteps, MOCK_PHASES } from "./steps.js";

function* walk(steps) {
  for (const step of steps) {
    yield step;
    yield* walk(step.children || []);
  }
}

function attemptNumber(ref) {
  const match = /(\d{3})/.exec(ref);
  return match ? Number(match[1]) : null;
}

export function mockRunPayload(missionId) {
  const steps = buildSteps();
  const all = [...walk(steps)];
  const started = all.map((s) => Date.parse(s.started_at)).filter(Number.isFinite);
  const finished = all.map((s) => Date.parse(s.finished_at)).filter(Number.isFinite);

  const artifactsIndex = [
    { kind: "model.mzn", ref: "workspace/001/model.mzn", label: "MiniZinc model (attempt 1, rejected)" },
    { kind: "data.dzn", ref: "workspace/001/data.dzn", label: "MiniZinc data (attempt 1, rejected)" },
    { kind: "model.mzn", ref: "workspace/002/model.mzn", label: "MiniZinc model (attempt 2, accepted)" },
    { kind: "data.dzn", ref: "workspace/002/data.dzn", label: "MiniZinc data (attempt 2, accepted)" },
    { kind: "statechart.json", ref: "statechart-attempts/001/statechart.json", label: "Statechart (attempt 1, failed validation)" },
    { kind: "accepted-statechart.json", ref: "statechart-attempts/002/accepted-statechart.json", label: "Statechart (attempt 2, accepted)" },
    { kind: "normalized-plan.json", ref: "workspace/002/normalized-plan.json", label: "Normalized plan (revision 2)" },
  ];
  const plannerAttempts = new Set(
    artifactsIndex.filter((a) => a.ref.startsWith("workspace/")).map((a) => attemptNumber(a.ref)),
  );
  const statechartAttempts = new Set(
    artifactsIndex.filter((a) => a.ref.includes("statechart-attempts/")).map((a) => attemptNumber(a.ref)),
  );

  return {
    schema_version: 1,
    mission_id: missionId || "mission:demo",
    generated_at: "2026-08-21T09:47:53.000+00:00",
    warnings: [
      "Debug capture was partial for role 'maneuver-control': 3 llm calls have no recorded reasoning.",
    ],
    mission: {
      mission_id: missionId || "mission:demo",
      title: "Patrol sector, investigate contacts",
      objective:
        "Patrol the assigned sector on a planned route, account for every event in the event report, and investigate any contact detected with confidence ≥ 0.5. Return to base before the battery reserve threshold.",
      source_authority: "demo-operator",
      issued_at: "2026-08-21T09:40:58.000+00:00",
      sector: "sector-7",
      constraints: [
        "Stay within sector bounds (±1600 m).",
        "Investigate contacts with confidence ≥ 0.5; single sensor retry per contact.",
        "Return to base before battery drops to the 25% reserve.",
      ],
    },
    status: "complete",
    aggregates: {
      step_count: all.length,
      llm_call_count: all.filter((s) => s.kind === "llm").length,
      tool_call_count: all.filter((s) => s.kind === "tool").length,
      error_count: all.filter((s) => s.status === "error").length,
      duration_ms: Math.max(...finished) - Math.min(...started),
      planner_attempts: plannerAttempts.size,
      statechart_attempts: statechartAttempts.size,
    },
    components: [...new Set(all.map((s) => s.component))].sort(),
    phases: MOCK_PHASES.slice(),
    summaries: [
      {
        summary_id: "summary:mission:demo:1",
        input_start_sequence: 1,
        input_end_sequence: 38,
        summary:
          "Temporal plan accepted on attempt 2 after a validator rejection (open dwell interval, tie-break constant). Statechart accepted on attempt 2 after adding SENSOR_ERROR handling. Maneuver executed all four stops, captured 11 of 12 events, classified 3 contacts, and docked at 29% battery. One sensor timeout on contact-041 recovered via policy retry.",
      },
    ],
    fsm: {
      statechart: {
        id: "maneuver-execution",
        revision: 2,
        initial: "navigate",
        states: {
          navigate: { on: { ARRIVED: "search", ABORT: "return" } },
          search: { on: { CONTACT_FOUND: "investigate", AREA_CLEAR: "advance" } },
          investigate: { on: { CLASSIFIED: "advance", SENSOR_ERROR: "investigate_retry" } },
          investigate_retry: { on: { CLASSIFIED: "advance", SENSOR_ERROR: "advance" } },
          advance: { on: { NEXT_STOP: "navigate", ROUTE_COMPLETE: "return" } },
          return: { on: { DOCKED: "complete" } },
          complete: { type: "final" },
        },
      },
      execution_record: {
        record_id: "fsm-execution:mission:demo:1",
        statechart_id: "maneuver-execution@2",
        status: "completed",
        started_at: "2026-08-21T09:43:06.000+00:00",
        finished_at: "2026-08-21T09:47:52.000+00:00",
        transitions: [
          { at_tick: 0, from: null, to: "navigate", event: "START", note: "patrol-stop-1" },
          { at_tick: 16, from: "navigate", to: "search", event: "ARRIVED" },
          { at_tick: 21, from: "search", to: "investigate", event: "CONTACT_FOUND", note: "contact-041 (0.81)" },
          { at_tick: 22, from: "investigate", to: "investigate_retry", event: "SENSOR_ERROR", note: "SensorTimeout 3000 ms" },
          { at_tick: 24, from: "investigate_retry", to: "advance", event: "CLASSIFIED", note: "vessel-small (0.72)" },
          { at_tick: 120, from: "advance", to: "navigate", event: "NEXT_STOP", note: "patrol-stop-2" },
          { at_tick: 120, from: "navigate", to: "search", event: "ARRIVED" },
          { at_tick: 124, from: "search", to: "advance", event: "AREA_CLEAR" },
          { at_tick: 195, from: "advance", to: "navigate", event: "NEXT_STOP", note: "patrol-stop-3" },
          { at_tick: 195, from: "navigate", to: "search", event: "ARRIVED" },
          { at_tick: 199, from: "search", to: "investigate", event: "CONTACT_FOUND", note: "contact-088 (0.64)" },
          { at_tick: 201, from: "investigate", to: "advance", event: "CLASSIFIED", note: "vessel-small (0.78)" },
          { at_tick: 259, from: "advance", to: "navigate", event: "NEXT_STOP", note: "patrol-stop-4" },
          { at_tick: 259, from: "navigate", to: "search", event: "ARRIVED" },
          { at_tick: 263, from: "search", to: "investigate", event: "CONTACT_FOUND", note: "contact-112 (0.58)" },
          { at_tick: 265, from: "investigate", to: "advance", event: "CLASSIFIED", note: "floating-debris (0.66)" },
          { at_tick: 266, from: "advance", to: "return", event: "ROUTE_COMPLETE" },
          { at_tick: 302, from: "return", to: "complete", event: "DOCKED", note: "battery 29%" },
        ],
      },
    },
    environment: {
      captured_at: "2026-08-21T09:47:52.400+00:00",
      snapshot_id: "mission-snapshot:mission:demo:2",
      sector: { id: "sector-7", bounds_m: { x: [-1600, 1600], y: [-1600, 1600] } },
      drone: { position: { x: 46, y: -86 }, battery_pct: 29, max_velocity: 20, fov_radius_m: 30 },
      events: { total: 12, captured: 11, missed: [{ event_index: 1, reason: "unreachable from start at max velocity" }] },
      contacts: [
        { contact_id: "contact-041", classification: "vessel-small", confidence: 0.72, status: "classified" },
        { contact_id: "contact-088", classification: "vessel-small", confidence: 0.78, status: "classified" },
        { contact_id: "contact-112", classification: "floating-debris", confidence: 0.66, status: "classified" },
      ],
    },
    artifacts_index: artifactsIndex,
    final_result: {
      status: "success",
      objective_value: 9.853,
      objective_max: 12.0,
      events_captured: 11,
      events_total: 12,
      stops_completed: 4,
      contacts_classified: 3,
      plan_revision: 2,
      statechart_id: "maneuver-execution@2",
      dock_battery_pct: 29,
    },
  };
}
