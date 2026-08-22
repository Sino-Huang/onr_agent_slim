include "network_flow.mzn";

int: source_event_count;
int: intersection_count;
int: action_count;
int: node_count;
int: arc_count;
int: source_node;
int: sink_node;
int: objective_scale;
int: time_scale;

set of int: ACTIONS = 1..action_count;
set of int: NODES = 1..node_count;
set of int: ARCS = 1..arc_count;

array[ARCS] of NODES: arc_from;
array[ARCS] of NODES: arc_to;
array[ARCS] of int: arc_cost;
array[NODES] of int: node_balance;

array[ACTIONS] of int: action_x;
array[ACTIONS] of int: action_y;
array[ACTIONS] of int: action_start;
array[ACTIONS] of int: action_end;
array[ACTIONS] of int: action_gain;
array[ACTIONS] of int: action_anchor_event;
array[ACTIONS] of int: action_capture_count;

array[ARCS, 1..2] of NODES: arcs =
  array2d(ARCS, 1..2, [
    if endpoint = 1 then arc_from[edge] else arc_to[edge] endif
    | edge in ARCS, endpoint in 1..2
  ]);

array[ARCS] of var 0..1: flow;
var -sum(action in ACTIONS)(action_gain[action] * objective_scale)..0:
  total_cost;

constraint network_flow_cost(arcs, node_balance, arc_cost, flow, total_cost);

array[ACTIONS] of var 0..1: selected = [
  sum(edge in ARCS where arc_to[edge] = action + 1)(flow[edge])
  | action in ACTIONS
];
var int: information_gain =
  sum(action in ACTIONS)(action_gain[action] * selected[action]);
var 0..intersection_count: stop_count = sum(selected);

constraint -total_cost = information_gain * objective_scale - stop_count;

solve minimize total_cost;

output [
  "{\"information_gain\":" ++ show(fix(information_gain))
  ++ ",\"stop_count\":" ++ show(fix(stop_count))
  ++ ",\"assignments\":["
  ++ join(",", [
    "{\"maneuver_id\":\"patrol-action-" ++ show(action) ++ "\","
    ++ "\"start\":" ++ show(action_start[action]) ++ ","
    ++ "\"duration\":" ++ show(action_end[action] - action_start[action]) ++ ","
    ++ "\"parameters\":{"
    ++ "\"x\":" ++ show(action_x[action]) ++ ","
    ++ "\"y\":" ++ show(action_y[action]) ++ ","
    ++ "\"source_event_index\":" ++ show(action_anchor_event[action]) ++ ","
    ++ "\"captured_event_count\":" ++ show(action_capture_count[action])
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ "}}"
    | action in ACTIONS where fix(selected[action]) = 1
  ])
  ++ "]}"
];
