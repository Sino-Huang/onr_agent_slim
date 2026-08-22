include "globals.mzn";

int: event_count;
set of int: EVENTS = 1..event_count;
set of int: STOP_SLOTS = EVENTS;
set of int: STOP_INDEX = 0..event_count;
int: entity_count;
set of int: ENTITIES = 1..entity_count;

int: horizon_ticks;
int: time_scale;

array[EVENTS] of float: event_time_s;
array[EVENTS] of float: event_x_m;
array[EVENTS] of float: event_y_m;
array[EVENTS] of int: event_entity;
array[ENTITIES] of float: entity_risk_p;

array[EVENTS] of int: event_time = [
  round(event_time_s[event] * time_scale) | event in EVENTS
];
array[EVENTS] of int: event_x = [round(event_x_m[event]) | event in EVENTS];
array[EVENTS] of int: event_y = [round(event_y_m[event]) | event in EVENTS];
array[EVENTS] of int: event_risk = [
  round(entity_risk_p[event_entity[event]] * risk_scale) | event in EVENTS
];
array[EVENTS] of int: event_in_time_order = arg_sort(event_time);

int: drone_start_time;
float: drone_start_x_m;
float: drone_start_y_m;
int: drone_start_x = round(drone_start_x_m);
int: drone_start_y = round(drone_start_y_m);
int: max_velocity;
int: fov_radius;
int: risk_scale;

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

array[STOP_SLOTS] of var bool: used;
array[STOP_INDEX] of var EVENTS: stop_location_event;
array[STOP_INDEX] of var EVENTS: earliest_assigned_event;
array[STOP_INDEX] of var EVENTS: latest_assigned_event;
array[STOP_INDEX] of var int: stop_start = array1d(STOP_INDEX, [
  event_time[earliest_assigned_event[slot]] | slot in STOP_INDEX
]);
array[STOP_INDEX] of var int: stop_end = array1d(STOP_INDEX, [
  event_time[latest_assigned_event[slot]] + 1 | slot in STOP_INDEX
]);
array[STOP_INDEX] of var int: stop_dwell = array1d(STOP_INDEX, [
  stop_end[slot] - stop_start[slot] | slot in STOP_INDEX
]);
array[EVENTS] of var 0..event_count: assigned_stop;
array[EVENTS] of var bool: captured;
array[1..event_count + 1] of var 0..event_count: assignment_count;
array[STOP_INDEX] of var 0..event_count: last_assigned_in_time_order;

constraint forall(slot in 1..event_count - 1)(used[slot + 1] -> used[slot]);
constraint global_cardinality(
  assigned_stop, [slot | slot in STOP_INDEX], assignment_count
);
constraint forall(slot in STOP_SLOTS)(
  used[slot] <-> assignment_count[slot + 1] > 0
);
constraint forall(event in EVENTS)(
  captured[event] <-> assigned_stop[event] > 0
);
constraint last_assigned_in_time_order[0] = 0;
constraint forall(rank in EVENTS)(
  let {
    int: event = event_in_time_order[rank]
  } in
    (captured[event] ->
      assigned_stop[event] >= last_assigned_in_time_order[rank - 1]) /\
    last_assigned_in_time_order[rank] = max(
      last_assigned_in_time_order[rank - 1], assigned_stop[event]
    )
);
constraint stop_location_event[0] = 1 /\
  earliest_assigned_event[0] = 1 /\ latest_assigned_event[0] = 1;

constraint forall(slot in STOP_SLOTS)(
  if used[slot] then
    assigned_stop[stop_location_event[slot]] = slot /\
    assigned_stop[earliest_assigned_event[slot]] = slot /\
    assigned_stop[latest_assigned_event[slot]] = slot /\
    stop_dwell[slot] > 0 /\
    stop_end[slot] <= horizon_ticks /\
    stop_start[slot] >=
      drone_start_time + travel_from_start[stop_location_event[slot]]
  else
    stop_location_event[slot] = 1 /\
    earliest_assigned_event[slot] = 1 /\
    latest_assigned_event[slot] = 1
  endif
);

constraint forall(event in EVENTS)(
  captured[event] ->
    stop_start[assigned_stop[event]] <= event_time[event] /\
    event_time[event] < stop_end[assigned_stop[event]] /\
    covers_from_location[stop_location_event[assigned_stop[event]], event]
);

constraint forall(slot in 1..event_count - 1)(
  used[slot + 1] ->
    stop_start[slot + 1] >= stop_end[slot]
      + travel_between[
          stop_location_event[slot], stop_location_event[slot + 1]
        ]
);

var 0..event_count: used_stop_count =
  sum(slot in STOP_SLOTS)(bool2int(used[slot]));
var int: information_gain = sum(event in EVENTS)(
  (risk_scale - event_risk[event]) * bool2int(captured[event])
);

solve :: seq_search([
  int_search(assigned_stop, input_order, indomain_min, complete),
  int_search(earliest_assigned_event, first_fail, indomain_min, complete),
  int_search(latest_assigned_event, first_fail, indomain_min, complete),
  int_search(stop_location_event, first_fail, indomain_min, complete)
]) maximize information_gain * (event_count + 1) - used_stop_count;

output ["{\"assignments\":["]
  ++ [
    if fix(used[slot]) then
      (if slot > 1 then "," else "" endif)
      ++ "{\"maneuver_id\":\"patrol-stop-" ++ show(slot) ++ "\","
      ++ "\"start\":" ++ show(stop_start[slot]) ++ ","
      ++ "\"duration\":" ++ show(stop_dwell[slot]) ++ ","
      ++ "\"parameters\":{"
      ++ "\"wait_start\":" ++ show(
        if slot = 1 then drone_start_time else stop_end[slot - 1] endif
      ) ++ ","
      ++ "\"wait_duration\":" ++ show(
        if slot = 1 then
          stop_start[slot]
            - travel_from_start[stop_location_event[slot]] - drone_start_time
        else
          stop_start[slot]
            - travel_between[
                stop_location_event[slot - 1], stop_location_event[slot]
              ] - stop_end[slot - 1]
        endif
      ) ++ ","
      ++ "\"move_start\":" ++ show(
        if slot = 1 then
          stop_start[slot] - travel_from_start[stop_location_event[slot]]
        else
          stop_start[slot]
            - travel_between[
                stop_location_event[slot - 1], stop_location_event[slot]
              ]
        endif
      ) ++ ","
      ++ "\"move_duration\":" ++ show(
        if slot = 1 then travel_from_start[stop_location_event[slot]]
        else travel_between[
          stop_location_event[slot - 1], stop_location_event[slot]
        ] endif
      ) ++ ","
      ++ "\"move_from_x\":" ++ show(
        if slot = 1 then drone_start_x
        else event_x[stop_location_event[slot - 1]] endif
      ) ++ ","
      ++ "\"move_from_y\":" ++ show(
        if slot = 1 then drone_start_y
        else event_y[stop_location_event[slot - 1]] endif
      ) ++ ","
      ++ "\"x\":" ++ show(event_x[stop_location_event[slot]]) ++ ","
      ++ "\"y\":" ++ show(event_y[stop_location_event[slot]]) ++ ","
      ++ "\"source_event_index\":" ++ show(stop_location_event[slot]) ++ ","
      ++ "\"time_scale\":" ++ show(time_scale) ++ "}}"
    else "" endif
    | slot in STOP_SLOTS
  ]
  ++ ["]}"];
