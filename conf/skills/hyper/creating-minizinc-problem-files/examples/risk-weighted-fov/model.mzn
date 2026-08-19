int: ship_count;
set of int: SHIPS = 1..ship_count;

int: horizon;
set of int: TIMES = 0..horizon;
array[SHIPS] of int: event_time;
array[SHIPS] of int: ship_x;
array[SHIPS] of int: ship_y;
array[SHIPS] of int: risk_weight;

int: coordinate_bound;
int: drone_start_x;
int: drone_start_y;
int: max_speed;
int: fov_radius;

array[TIMES] of var -coordinate_bound..coordinate_bound: drone_x;
array[TIMES] of var -coordinate_bound..coordinate_bound: drone_y;
array[SHIPS] of var bool: observed;

constraint drone_x[0] = drone_start_x;
constraint drone_y[0] = drone_start_y;
constraint forall(t in 0..horizon - 1)(
  abs(drone_x[t + 1] - drone_x[t]) + abs(drone_y[t + 1] - drone_y[t])
    <= max_speed
);
constraint forall(s in SHIPS)(
  observed[s] <->
    abs(drone_x[event_time[s]] - ship_x[s])
      + abs(drone_y[event_time[s]] - ship_y[s])
      <= fov_radius
);

var int: covered_risk = sum(s in SHIPS)(
  risk_weight[s] * bool2int(observed[s])
);

solve maximize covered_risk;

output [
  "{\"covered_risk\":" ++ show(covered_risk)
  ++ ",\"observed\":[" ++ join(",", [show(observed[s]) | s in SHIPS]) ++ "]}"
];
