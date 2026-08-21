// Mock /api/artifact bodies, keyed by ref (refs are relative to the
// planner-artifacts root, matching the backend's path-confined lookup).
// Content is adapted from the real run captured under var/planner-artifacts.

const MODEL_MZN_ATTEMPT_1 = `% Patrol route + dwell schedule — report event accounting (temporal profile)
% attempt 1 (REJECTED: open dwell interval, tie_bound as precomputed constant)

int: event_count;
set of int: EVENTS = 1..event_count;
set of int: OPPORTUNITIES = EVENTS;
int: stop_count;
set of int: STOPS = 1..stop_count;
int: entity_count;
set of int: ENTITIES = 1..entity_count;

int: horizon_ticks;
int: time_scale;
int: dwell_ticks;
array[STOPS] of string: maneuver_id;

array[EVENTS] of float: event_time_s;
array[EVENTS] of float: event_x_m;
array[EVENTS] of float: event_y_m;
array[EVENTS] of int: event_entity;
array[ENTITIES] of float: entity_risk_p;

int: drone_start_time;
int: drone_start_x;
int: drone_start_y;
int: max_velocity;
int: fov_radius;
int: risk_scale;

array[EVENTS] of int: event_time = [
  round(event_time_s[event] * time_scale) | event in EVENTS
];
array[EVENTS] of int: event_x = [round(event_x_m[event]) | event in EVENTS];
array[EVENTS] of int: event_y = [round(event_y_m[event]) | event in EVENTS];
array[EVENTS] of int: event_risk = [
  round(entity_risk_p[event_entity[event]] * risk_scale) | event in EVENTS
];

array[OPPORTUNITIES] of int: opportunity_time = event_time;
array[OPPORTUNITIES] of int: opportunity_x = event_x;
array[OPPORTUNITIES] of int: opportunity_y = event_y;

function int: squared_distance(int: ax, int: ay, int: bx, int: by) =
  (bx - ax) * (bx - ax) + (by - ay) * (by - ay);
function int: minimum_travel_ticks(int: ax, int: ay, int: bx, int: by) =
  ceil(
    sqrt(int2float(squared_distance(ax, ay, bx, by)))
      * int2float(time_scale) / int2float(max_velocity)
  );

array[OPPORTUNITIES] of int: travel_from_start = [
  minimum_travel_ticks(
    drone_start_x, drone_start_y, opportunity_x[o], opportunity_y[o]
  )
  | o in OPPORTUNITIES
];

array[OPPORTUNITIES, OPPORTUNITIES] of int: travel_between =
  array2d(OPPORTUNITIES, OPPORTUNITIES, [
    minimum_travel_ticks(
      opportunity_x[prev], opportunity_y[prev],
      opportunity_x[next], opportunity_y[next]
    )
    | prev in OPPORTUNITIES, next in OPPORTUNITIES
  ]);

array[OPPORTUNITIES] of bool: reachable_from_start = [
  let { int: available = opportunity_time[o] - drone_start_time } in
    available >= 0 /\\
    squared_distance(drone_start_x, drone_start_y, opportunity_x[o], opportunity_y[o])
      * time_scale * time_scale
      <= max_velocity * max_velocity * available * available
  | o in OPPORTUNITIES
];

array[OPPORTUNITIES, OPPORTUNITIES] of bool: can_follow =
  array2d(OPPORTUNITIES, OPPORTUNITIES, [
    let { int: available = opportunity_time[next] - opportunity_time[prev] - dwell_ticks } in
      next > prev /\\
      available >= 0 /\\
      squared_distance(
        opportunity_x[prev], opportunity_y[prev],
        opportunity_x[next], opportunity_y[next]
      ) * time_scale * time_scale
        <= max_velocity * max_velocity * available * available
    | prev in OPPORTUNITIES, next in OPPORTUNITIES
  ]);

% --- REJECTED: half-open dwell interval, boundary events are dropped ---
array[OPPORTUNITIES, EVENTS] of bool: covers =
  array2d(OPPORTUNITIES, EVENTS, [
    event_time[event] >= opportunity_time[opportunity] /\\
    event_time[event] <= opportunity_time[opportunity] + dwell_ticks - 1 /\\
    squared_distance(
      opportunity_x[opportunity], opportunity_y[opportunity],
      event_x[event], event_y[event]
    ) <= fov_radius * fov_radius
    | opportunity in OPPORTUNITIES, event in EVENTS
  ]);

array[STOPS] of var OPPORTUNITIES: selected_opportunity;
constraint reachable_from_start[selected_opportunity[1]];
constraint forall(stop in 1..stop_count - 1)(
  selected_opportunity[stop] < selected_opportunity[stop + 1] /\\
  can_follow[selected_opportunity[stop], selected_opportunity[stop + 1]]
);

array[EVENTS] of var bool: captured;
constraint forall(event in EVENTS)(
  captured[event] <-> exists(stop in STOPS)(
    covers[selected_opportunity[stop], event]
  )
);

var int: information_gain = sum(event in EVENTS)(
  (risk_scale - event_risk[event]) * bool2int(captured[event])
);

% --- REJECTED: validator requires the literal bound expression ---
int: tie_bound = 2401;

solve maximize
  information_gain * tie_bound
    - sum(stop in STOPS)(opportunity_time[selected_opportunity[stop]]);

output ["{\\"assignments\\":["]
  ++ [
    (if stop > 1 then "," else "" endif)
    ++ "{\\"maneuver_id\\":\\"" ++ maneuver_id[stop] ++ "\\","
    ++ "\\"start\\":" ++ show(opportunity_time[selected_opportunity[stop]]) ++ ","
    ++ "\\"duration\\":" ++ show(dwell_ticks) ++ ","
    ++ "\\"x\\":" ++ show(opportunity_x[selected_opportunity[stop]]) ++ ","
    ++ "\\"y\\":" ++ show(opportunity_y[selected_opportunity[stop]]) ++ ","
    ++ "\\"time_scale\\":" ++ show(time_scale) ++ "}"
    | stop in STOPS
  ]
  ++ ["]}"];
`;

const MODEL_MZN_ATTEMPT_2 = MODEL_MZN_ATTEMPT_1.replace(
  "% attempt 1 (REJECTED: open dwell interval, tie_bound as precomputed constant)",
  "% attempt 2 (ACCEPTED: closed dwell interval, symbolic tie_bound)",
).replace(
  "% --- REJECTED: half-open dwell interval, boundary events are dropped ---",
  "% dwell interval is closed at both ends; boundary events count",
).replace(
  "event_time[event] <= opportunity_time[opportunity] + dwell_ticks - 1",
  "event_time[event] <= opportunity_time[opportunity] + dwell_ticks",
).replace(
  "% --- REJECTED: validator requires the literal bound expression ---\nint: tie_bound = 2401;",
  "% symbolic bound the validator can check\nint: tie_bound = stop_count * horizon_ticks + 1;",
);

const DATA_DZN = `% mission:demo — environment-data:fce73297 (snapshot revision 2)
event_count = 12;
stop_count = 4;
entity_count = 6;
horizon_ticks = 600;
time_scale = 2;
dwell_ticks = 2;
maneuver_id = ["patrol-stop-1", "patrol-stop-2", "patrol-stop-3", "patrol-stop-4"];

event_time_s = [0.5, 3.0, 9.0, 9.5, 10.0, 60.5, 61.0, 61.5, 96.0, 98.0, 130.5, 131.0];
event_x_m    = [802.9, 165.8, 148.0, 146.7, 151.2, 306.1, 318.7, 312.4, -675.4, -668.1, 18.2, 25.4];
event_y_m    = [-317.3, 181.3, 187.3, 191.0, 196.2, -16.7, -1.1, -22.9, -1352.9, -1341.5, -318.1, -309.4];
event_entity = [2, 1, 1, 1, 3, 4, 4, 4, 5, 5, 6, 6];

entity_risk_p = [0.219, 0.753, 0.468, 0.383, 0.410, 0.352];

drone_start_time = 0;
drone_start_x = 46;
drone_start_y = -86;
max_velocity = 20;
fov_radius = 30;
risk_scale = 1000;
`;

const STATECHART_ATTEMPT_1 = `{
  "id": "maneuver-execution",
  "revision": 1,
  "initial": "navigate",
  "states": {
    "navigate": {
      "on": { "ARRIVED": "search", "ABORT": "return" }
    },
    "search": {
      "on": { "CONTACT_FOUND": "investigate", "AREA_CLEAR": "advance" }
    },
    "investigate": {
      "on": { "CLASSIFIED": "advance" }
    },
    "advance": {
      "on": { "NEXT_STOP": "navigate", "ROUTE_COMPLETE": "return" }
    },
    "return": {
      "on": { "DOCKED": "complete" }
    },
    "complete": { "type": "final" }
  }
}
`;

const STATECHART_ATTEMPT_2 = `{
  "id": "maneuver-execution",
  "revision": 2,
  "initial": "navigate",
  "states": {
    "navigate": {
      "on": { "ARRIVED": "search", "ABORT": "return" }
    },
    "search": {
      "on": { "CONTACT_FOUND": "investigate", "AREA_CLEAR": "advance" }
    },
    "investigate": {
      "on": { "CLASSIFIED": "advance", "SENSOR_ERROR": "investigate_retry" }
    },
    "investigate_retry": {
      "on": { "CLASSIFIED": "advance", "SENSOR_ERROR": "advance" }
    },
    "advance": {
      "on": { "NEXT_STOP": "navigate", "ROUTE_COMPLETE": "return" }
    },
    "return": {
      "on": { "DOCKED": "complete" }
    },
    "complete": { "type": "final" }
  }
}
`;

const NORMALIZED_PLAN = `{
  "plan_revision": 2,
  "mission_id": "mission:demo",
  "objective_value": 9.853,
  "objective_max": 12.0,
  "assignments": [
    {
      "maneuver_id": "patrol-stop-1",
      "start": 18,
      "duration": 2,
      "parameters": { "wait_start": 0, "wait_duration": 8, "move_start": 8, "move_duration": 10, "x": 148, "y": 188, "time_scale": 2 }
    },
    {
      "maneuver_id": "patrol-stop-2",
      "start": 122,
      "duration": 2,
      "parameters": { "wait_start": 20, "wait_duration": 88, "move_start": 108, "move_duration": 14, "x": 306, "y": -17, "time_scale": 2 }
    },
    {
      "maneuver_id": "patrol-stop-3",
      "start": 196,
      "duration": 2,
      "parameters": { "wait_start": 124, "wait_duration": 34, "move_start": 158, "move_duration": 38, "x": -676, "y": -1353, "time_scale": 2 }
    },
    {
      "maneuver_id": "patrol-stop-4",
      "start": 261,
      "duration": 2,
      "parameters": { "wait_start": 198, "wait_duration": 33, "move_start": 231, "move_duration": 30, "x": 21, "y": -318, "time_scale": 2 }
    }
  ]
}
`;

const ARTIFACTS = {
  "workspace/001/model.mzn": MODEL_MZN_ATTEMPT_1,
  "workspace/001/data.dzn": DATA_DZN,
  "workspace/002/model.mzn": MODEL_MZN_ATTEMPT_2,
  "workspace/002/data.dzn": DATA_DZN,
  "workspace/002/normalized-plan.json": NORMALIZED_PLAN,
  "statechart-attempts/001/statechart.json": STATECHART_ATTEMPT_1,
  "statechart-attempts/002/accepted-statechart.json": STATECHART_ATTEMPT_2,
};

export function mockArtifact(ref) {
  return Object.prototype.hasOwnProperty.call(ARTIFACTS, ref) ? ARTIFACTS[ref] : null;
}
