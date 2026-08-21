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
array[OPPORTUNITIES] of int: source_event_index = [event | event in EVENTS];

int: drone_start_time;
int: drone_start_x;
int: drone_start_y;
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

array[OPPORTUNITIES] of int: travel_from_start = [
  minimum_travel_ticks(
    drone_start_x, drone_start_y, opportunity_x[opportunity], opportunity_y[opportunity]
  )
  | opportunity in OPPORTUNITIES
];

array[OPPORTUNITIES, OPPORTUNITIES] of int: travel_between =
  array2d(OPPORTUNITIES, OPPORTUNITIES, [
    minimum_travel_ticks(
      opportunity_x[previous], opportunity_y[previous],
      opportunity_x[next], opportunity_y[next]
    )
    | previous in OPPORTUNITIES, next in OPPORTUNITIES
  ]);

array[OPPORTUNITIES] of bool: reachable_from_start = [
  let {
    int: available_ticks = opportunity_time[o] - drone_start_time
  } in
    available_ticks >= 0 /\
    squared_distance(drone_start_x, drone_start_y, opportunity_x[o], opportunity_y[o])
      * time_scale * time_scale
      <= max_velocity * max_velocity * available_ticks * available_ticks
  | o in OPPORTUNITIES
];

array[OPPORTUNITIES, OPPORTUNITIES] of bool: can_follow =
  array2d(OPPORTUNITIES, OPPORTUNITIES, [
    let {
      int: available_ticks = opportunity_time[next] - opportunity_time[previous]
        - dwell_ticks
    } in
      next > previous /\
      available_ticks >= 0 /\
      squared_distance(
        opportunity_x[previous], opportunity_y[previous],
        opportunity_x[next], opportunity_y[next]
      ) * time_scale * time_scale
        <= max_velocity * max_velocity * available_ticks * available_ticks
    | previous in OPPORTUNITIES, next in OPPORTUNITIES
  ]);

array[OPPORTUNITIES, EVENTS] of bool: covers =
  array2d(OPPORTUNITIES, EVENTS, [
    event_time[event] >= opportunity_time[opportunity] /\
    event_time[event] <= opportunity_time[opportunity] + dwell_ticks /\
    squared_distance(
      opportunity_x[opportunity], opportunity_y[opportunity],
      event_x[event], event_y[event]
    ) <= fov_radius * fov_radius
    | opportunity in OPPORTUNITIES, event in EVENTS
  ]);

array[STOPS] of var OPPORTUNITIES: selected_opportunity;
constraint reachable_from_start[selected_opportunity[1]];
constraint forall(stop in 1..stop_count - 1)(
  selected_opportunity[stop] < selected_opportunity[stop + 1] /\
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
int: tie_bound = stop_count * horizon_ticks + 1;
solve maximize
  information_gain * tie_bound
    - sum(stop in STOPS)(opportunity_time[selected_opportunity[stop]]);

output ["{\"assignments\":["]
  ++ [
    (if stop > 1 then "," else "" endif)
    ++ "{\"maneuver_id\":\"" ++ maneuver_id[stop] ++ "\","
    ++ "\"start\":" ++ show(opportunity_time[selected_opportunity[stop]]) ++ ","
    ++ "\"duration\":" ++ show(dwell_ticks) ++ ","
    ++ "\"parameters\":{"
    ++ "\"wait_start\":" ++ show(
      if stop = 1 then drone_start_time
      else opportunity_time[selected_opportunity[stop - 1]] + dwell_ticks
      endif
    ) ++ ","
    ++ "\"wait_duration\":" ++ show(
      if stop = 1 then
        opportunity_time[selected_opportunity[stop]]
          - travel_from_start[selected_opportunity[stop]] - drone_start_time
      else
        opportunity_time[selected_opportunity[stop]]
          - travel_between[selected_opportunity[stop - 1], selected_opportunity[stop]]
          - opportunity_time[selected_opportunity[stop - 1]] - dwell_ticks
      endif
    ) ++ ","
    ++ "\"move_start\":" ++ show(
      if stop = 1 then
        opportunity_time[selected_opportunity[stop]]
          - travel_from_start[selected_opportunity[stop]]
      else
        opportunity_time[selected_opportunity[stop]]
          - travel_between[selected_opportunity[stop - 1], selected_opportunity[stop]]
      endif
    ) ++ ","
    ++ "\"move_duration\":" ++ show(
      if stop = 1 then travel_from_start[selected_opportunity[stop]]
      else travel_between[selected_opportunity[stop - 1], selected_opportunity[stop]]
      endif
    ) ++ ","
    ++ "\"move_from_x\":" ++ show(
      if stop = 1 then drone_start_x
      else opportunity_x[selected_opportunity[stop - 1]]
      endif
    ) ++ ","
    ++ "\"move_from_y\":" ++ show(
      if stop = 1 then drone_start_y
      else opportunity_y[selected_opportunity[stop - 1]]
      endif
    ) ++ ","

    ++ "\"x\":" ++ show(opportunity_x[selected_opportunity[stop]]) ++ ","
    ++ "\"y\":" ++ show(opportunity_y[selected_opportunity[stop]]) ++ ","
    ++ "\"source_event_index\":"
      ++ show(source_event_index[selected_opportunity[stop]]) ++ ","
    ++ "\"time_scale\":" ++ show(time_scale) ++ "}}"
    | stop in STOPS
  ]
  ++ ["]}"];
