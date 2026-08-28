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

var int: information_gain =
  sum(edge in ARCS where arc_to[edge] > source_node /\ arc_to[edge] < sink_node)(
    action_gain[arc_to[edge] - 1] * flow[edge]
  );
var 0..intersection_count: stop_count =
  sum(edge in ARCS where arc_to[edge] > source_node /\ arc_to[edge] < sink_node)(
    flow[edge]
  );

constraint -total_cost = information_gain * objective_scale - stop_count;

solve minimize total_cost;

output [
  "{\"information_gain\":" ++ show(fix(information_gain))
  ++ ",\"stop_count\":" ++ show(fix(stop_count))
  ++ ",\"assignments\":["
  ++ join(",", [
    "{\"maneuver_id\":\"patrol-action-" ++ show(arc_to[edge] - 1) ++ "\","
    ++ "\"start\":" ++ show(action_start[arc_to[edge] - 1]) ++ ","
    ++ "\"duration\":" ++ show(action_end[arc_to[edge] - 1] - action_start[arc_to[edge] - 1]) ++ ","
    ++ "\"parameters\":{"
    ++ "\"x\":" ++ show(action_x[arc_to[edge] - 1]) ++ ","
    ++ "\"y\":" ++ show(action_y[arc_to[edge] - 1]) ++ ","
    ++ "\"source_event_index\":" ++ show(action_anchor_event[arc_to[edge] - 1]) ++ ","
    ++ "\"captured_event_count\":" ++ show(action_capture_count[arc_to[edge] - 1])
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ "}}"
    | edge in ARCS
      where arc_to[edge] > source_node /\ arc_to[edge] < sink_node
        /\ fix(flow[edge]) = 1
  ])
  ++ "]}"
];
