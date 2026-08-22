% Event-information patrol, deliberately written as a small teaching model.
%
% Meaning of the plan:
% - each used stop observes one or more source events;
% - the stop is placed at the coordinates of one assigned source event;
% - its start is the earliest assigned event time;
% - its positive dwell ends one tick after the latest assigned event time;
% - every captured event contributes information gain exactly once.

% ----- Inputs supplied by data.dzn -----
int: event_count;
set of int: EVENTS = 1..event_count;
set of int: STOP_SLOTS = EVENTS; % at most one optional stop slot per event

int: entity_count;
set of int: ENTITIES = 1..entity_count;

array[EVENTS] of float: event_time_s;
array[EVENTS] of float: event_x_m;
array[EVENTS] of float: event_y_m;
array[EVENTS] of int: event_entity;
array[ENTITIES] of float: entity_risk_p;

int: horizon_ticks;
int: time_scale;
int: drone_start_time;
float: drone_start_x_m;
float: drone_start_y_m;
int: max_velocity;
int: fov_radius;
int: risk_scale;

% Keep copied evidence as floats in data.dzn. Scale it here for integer solving.
array[EVENTS] of int: event_time = [
  round(event_time_s[event] * time_scale) | event in EVENTS
];
array[EVENTS] of int: event_x = [round(event_x_m[event]) | event in EVENTS];
array[EVENTS] of int: event_y = [round(event_y_m[event]) | event in EVENTS];
array[EVENTS] of int: event_risk = [
  round(entity_risk_p[event_entity[event]] * risk_scale) | event in EVENTS
];
int: drone_start_x = round(drone_start_x_m);
int: drone_start_y = round(drone_start_y_m);

% ----- Geometry and travel-time lookup tables -----
function int: squared_distance(int: ax, int: ay, int: bx, int: by) =
  (bx - ax) * (bx - ax) + (by - ay) * (by - ay);

function int: minimum_travel_ticks(int: ax, int: ay, int: bx, int: by) =
  ceil(
    sqrt(int2float(squared_distance(ax, ay, bx, by)))
      * int2float(time_scale) / int2float(max_velocity)
  );

array[EVENTS] of int: travel_from_start = [
  minimum_travel_ticks(drone_start_x, drone_start_y, event_x[event], event_y[event])
  | event in EVENTS
];

array[EVENTS, EVENTS] of int: travel_between =
  array2d(EVENTS, EVENTS, [
    minimum_travel_ticks(
      event_x[previous], event_y[previous], event_x[next], event_y[next]
    )
    | previous in EVENTS, next in EVENTS
  ]);

array[EVENTS, EVENTS] of bool: covers_from_location =
  array2d(EVENTS, EVENTS, [
    squared_distance(
      event_x[location], event_y[location], event_x[event], event_y[event]
    ) <= fov_radius * fov_radius
    | location in EVENTS, event in EVENTS
  ]);

% ----- Solver choices -----
% used slots are a prefix, so output IDs are patrol-stop-1, patrol-stop-2, ...
array[STOP_SLOTS] of var bool: used;
array[STOP_SLOTS] of var EVENTS: stop_location_event;
array[STOP_SLOTS] of var drone_start_time..horizon_ticks: stop_start;
array[STOP_SLOTS] of var drone_start_time..horizon_ticks: stop_end;
array[STOP_SLOTS] of var 0..horizon_ticks: stop_dwell;

% 0 means not captured; otherwise the value is the event's one assigned stop.
array[EVENTS] of var 0..event_count: assigned_stop;
array[EVENTS] of var bool: captured;

constraint forall(slot in 1..event_count - 1)(used[slot + 1] -> used[slot]);

% A stop is used exactly when at least one event is assigned to it.
constraint forall(slot in STOP_SLOTS)(
  used[slot] <-> exists(event in EVENTS)(assigned_stop[event] = slot)
);

constraint forall(event in EVENTS)(
  captured[event] <-> assigned_stop[event] > 0
);

% A used stop gets its location and time span from its assigned events.
constraint forall(slot in STOP_SLOTS)(
  if used[slot] then
    assigned_stop[stop_location_event[slot]] = slot /\
    stop_dwell[slot] = stop_end[slot] - stop_start[slot] /\
    stop_dwell[slot] > 0 /\
    stop_end[slot] <= horizon_ticks /\
    stop_start[slot] >=
      drone_start_time + travel_from_start[stop_location_event[slot]] /\
    forall(event in EVENTS)(
      assigned_stop[event] = slot ->
        stop_start[slot] <= event_time[event] /\
        event_time[event] < stop_end[slot] /\
        covers_from_location[stop_location_event[slot], event]
    ) /\
    exists(event in EVENTS)(
      assigned_stop[event] = slot /\ event_time[event] = stop_start[slot]
    ) /\
    exists(event in EVENTS)(
      assigned_stop[event] = slot /\ event_time[event] + 1 = stop_end[slot]
    )
  else
    stop_location_event[slot] = 1 /\
    stop_start[slot] = drone_start_time /\
    stop_end[slot] = drone_start_time /\
    stop_dwell[slot] = 0
  endif
);

% Consecutive used stops cannot overlap and must allow enough travel time.
constraint forall(slot in 1..event_count - 1)(
  used[slot + 1] ->
    stop_start[slot + 1] >= stop_end[slot]
      + travel_between[
          stop_location_event[slot], stop_location_event[slot + 1]
        ]
);

% Preserve the information formula. Multiplying by event_count + 1 makes
% information gain dominate; ties use fewer stops.
var int: information_gain = sum(event in EVENTS)(
  (risk_scale - event_risk[event]) * bool2int(captured[event])
);
var 0..event_count: used_stop_count =
  sum(slot in STOP_SLOTS)(bool2int(used[slot]));

solve maximize information_gain * (event_count + 1) - used_stop_count;

% Compact planner-native output. The Statechart interprets these stop records.
output ["{\"assignments\":["]
  ++ [
    if fix(used[slot]) then
      (if slot > 1 then "," else "" endif)
      ++ "{\"maneuver_id\":\"patrol-stop-" ++ show(slot) ++ "\","
      ++ "\"start\":" ++ show(stop_start[slot]) ++ ","
      ++ "\"duration\":" ++ show(stop_dwell[slot]) ++ ","
      ++ "\"parameters\":{"
      ++ "\"x\":" ++ show(event_x[stop_location_event[slot]]) ++ ","
      ++ "\"y\":" ++ show(event_y[stop_location_event[slot]]) ++ ","
      ++ "\"source_event_index\":" ++ show(stop_location_event[slot]) ++ ","
      ++ "\"time_scale\":" ++ show(time_scale) ++ "}}"
    else "" endif
    | slot in STOP_SLOTS
  ]
  ++ ["]}"];
