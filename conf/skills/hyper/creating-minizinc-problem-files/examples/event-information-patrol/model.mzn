int: candidate_count;
int: node_count;
int: arc_count;
int: source_node;
int: sink_node;
int: time_scale;
int: score_scale;
int: duration_bound;
int: maneuver_bound;
int: tie_break_bound;
int: report_id_count;

set of int: CANDIDATES = 1..candidate_count;
set of int: NODES = 1..node_count;
set of int: ARCS = 1..arc_count;
set of int: NODE_OFFSETS = 1..node_count + 1;
set of int: CANDIDATE_OFFSETS = 1..candidate_count + 1;
set of int: REPORT_IDS = 1..report_id_count;

% The shared candidate builder admits only timing-feasible arcs, using
% cardinal-grid distance at 0.9 * the advertised vehicle maximum velocity.
array[ARCS] of NODES: arc_from;
array[ARCS] of NODES: arc_to;
array[NODE_OFFSETS] of 1..arc_count + 1: outgoing_start;
array[NODE_OFFSETS] of 1..arc_count + 1: incoming_start;
array[ARCS] of ARCS: incoming_edge;
array[CANDIDATES] of 1..2: candidate_mode;
array[CANDIDATES] of int: candidate_entity_id;
array[CANDIDATES] of int: candidate_start;
array[CANDIDATES] of int: candidate_duration;
array[CANDIDATES] of int: candidate_x;
array[CANDIDATES] of int: candidate_y;
array[CANDIDATES] of int: candidate_recall;
array[CANDIDATES] of int: candidate_estimation;
array[CANDIDATES] of int: candidate_omission;
array[CANDIDATES] of int: candidate_target_risk;
array[CANDIDATES] of int: candidate_omission_probability;
array[CANDIDATES] of int: candidate_public_report_rate;
array[CANDIDATES] of int: candidate_report_span;
array[CANDIDATE_OFFSETS] of 1..report_id_count + 1: candidate_report_start;
array[CANDIDATES] of string: candidate_id;
array[REPORT_IDS] of string: candidate_report_id;

array[CANDIDATES] of int: candidate_combined_score = [
  candidate_recall[candidate]
  + candidate_estimation[candidate]
  + candidate_omission[candidate]
  | candidate in CANDIDATES
];

array[ARCS] of var 0.0..1.0: flow;
array[CANDIDATES] of var 0.0..1.0: selected;

constraint forall(node in NODES)(
  sum(edge in outgoing_start[node]..outgoing_start[node + 1] - 1)(flow[edge])
  - sum(position in incoming_start[node]..incoming_start[node + 1] - 1)(
      flow[incoming_edge[position]]
    )
  = if node = source_node then 1.0 elseif node = sink_node then -1.0 else 0.0 endif
);

constraint forall(candidate in CANDIDATES)(
  selected[candidate] = sum(
    position in incoming_start[candidate + 1]..incoming_start[candidate + 2] - 1
  )(
    flow[incoming_edge[position]]
  )
);

var float: combined_score = sum(candidate in CANDIDATES)(
  candidate_combined_score[candidate] * selected[candidate]
);
function bool: same_fixed_view(int: left, int: right) =
  candidate_mode[left] = 1 /\ candidate_mode[right] = 1 /\
  candidate_x[left] = candidate_x[right] /\ candidate_y[left] = candidate_y[right];
array[ARCS] of bool: continues_view = [
  if arc_from[e] = source_node \/ arc_to[e] = sink_node then false else
    same_fixed_view(arc_from[e] - 1, arc_to[e] - 1)
  endif | e in ARCS
];
array[ARCS] of int: run_maneuvers = [
  if arc_to[e] = sink_node \/ continues_view[e] then 0 else 1 endif | e in ARCS
];
array[ARCS] of int: run_duration = [
  if arc_to[e] = sink_node then 0 else candidate_duration[arc_to[e] - 1] +
    (if continues_view[e] then candidate_start[arc_to[e] - 1]
      - candidate_start[arc_from[e] - 1] - candidate_duration[arc_from[e] - 1] else 0 endif)
  endif | e in ARCS
];
var float: maneuver_count = sum(e in ARCS)(run_maneuvers[e] * flow[e]);
var float: surveillance_duration = sum(e in ARCS)(run_duration[e] * flow[e]);
var float: candidate_order_sum = sum(candidate in CANDIDATES)(
  candidate * selected[candidate]
);
% Apply potentials while coefficients are integers, before floating-point LP
% conversion. Along ANY source-to-sink unit flow these terms telescope to
% potential[source] - potential[sink], a constant. The same routes and exact
% lexicographic preferences remain; no advisory route is imposed as a constraint.
array[NODES] of int: node_objective_potential;
array[ARCS] of int: unreduced_arc_weight = [
  if arc_to[e] = sink_node then 0 else
    candidate_combined_score[arc_to[e] - 1] * maneuver_bound * duration_bound * tie_break_bound
    - run_maneuvers[e] * duration_bound * tie_break_bound
    - run_duration[e] * tie_break_bound - (arc_to[e] - 1)
  endif | e in ARCS
];
array[ARCS] of int: arc_weight = [
  unreduced_arc_weight[edge] + node_objective_potential[arc_from[edge]]
  - node_objective_potential[arc_to[edge]] | edge in ARCS
];
% Verify longest-prefix potentials from the component-derived arc weights.
% Consequently all reduced costs are nonpositive and a zero-cost path exists.
% Penalizing each strictly negative edge by one preserves EXACTLY the optimal
% paths, while avoiding huge floating-point coefficients and tiny tie losses.
constraint assert(
  node_objective_potential[source_node] = 0 /\
  forall(node in NODES where node != source_node)(
    node_objective_potential[node] = max(
      position in incoming_start[node]..incoming_start[node + 1] - 1
    )(
      node_objective_potential[arc_from[incoming_edge[position]]]
      + unreduced_arc_weight[incoming_edge[position]]
    )
  ), "invalid objective potentials"
);
% Candidate-index sums can tie across different paths. Among equal optimal
% prefixes, choose the smallest predecessor recursively, back from the sink.
% Every non-source node has a zero-cost incoming edge by the verified recurrence.
% This defines one canonical optimal path without imposing an advisory route.
array[NODES] of NODES: preferred_predecessor = [
  if node = source_node then source_node else
    min(position in incoming_start[node]..incoming_start[node + 1] - 1
        where arc_weight[incoming_edge[position]] = 0)(
      arc_from[incoming_edge[position]]
    )
  endif | node in NODES
];
var float: objective = -sum(edge in ARCS)(
  bool2int(arc_weight[edge] < 0 \/
    arc_from[edge] != preferred_predecessor[arc_to[edge]]) * flow[edge]
);

solve maximize objective;

% Compact only consecutive selections at one fixed viewpoint. The solver owns
% these sustained assignments, including the holding gaps charged above.
output let {
  array[int] of int: chosen = [c | c in CANDIDATES where fix(selected[c]) > 0.5];
  array[int] of int: run_start = [p | p in index_set(chosen) where
    if p = 1 then true else not same_fixed_view(chosen[p - 1], chosen[p]) endif];
} in [
  "{\"combined_score\":" ++ show(round(fix(combined_score)))
  ++ ",\"score_scale\":" ++ show(score_scale)
  ++ ",\"maneuver_count\":" ++ show(round(fix(maneuver_count)))
  ++ ",\"surveillance_duration\":" ++ show(round(fix(surveillance_duration)))
  ++ ",\"assignments\":["
  ++ join(",", [
    let {
      int: last_position = if run < length(run_start) then run_start[run + 1] - 1 else length(chosen) endif;
      array[int] of int: members = [chosen[p] | p in run_start[run]..last_position];
      int: candidate = members[1];
      int: last = members[length(members)];
      int: duration = candidate_start[last] + candidate_duration[last] - candidate_start[candidate];
    } in
    "{\"candidate_id\":\"" ++ candidate_id[candidate]
    ++ (if length(members) = 1 then "" else "--" ++ candidate_id[last] endif) ++ "\","
    ++ "\"surveillance_mode\":\""
    ++ (if candidate_mode[candidate] = 1 then "fixed_view" else "pursue_ship" endif)
    ++ "\",\"entity_id\":"
    ++ (if candidate_entity_id[candidate] = 0 then "null" else show(candidate_entity_id[candidate]) endif)
    ++ ",\"start\":" ++ show(candidate_start[candidate])
    ++ ",\"duration\":" ++ show(duration)
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ ",\"observation_window\":{\"start\":" ++ show(candidate_start[candidate])
    ++ ",\"duration\":" ++ show(duration)
    ++ ",\"time_scale\":" ++ show(time_scale) ++ "}"
    ++ ",\"parameters\":{\"x\":" ++ show(candidate_x[candidate])
    ++ ",\"y\":" ++ show(candidate_y[candidate])
    ++ ",\"target_posterior_risk\":"
    ++ (if candidate_mode[candidate] = 1 then "null" else show(candidate_target_risk[candidate]) endif)
    ++ ",\"expected_omission_probability\":"
    ++ (if candidate_mode[candidate] = 1 then "null" else show(candidate_omission_probability[candidate]) endif)
    ++ ",\"public_report_rate\":"
    ++ (if candidate_mode[candidate] = 1 then "null" else show(candidate_public_report_rate[candidate]) endif)
    ++ ",\"rate_scale\":" ++ show(score_scale)
    ++ ",\"report_span\":" ++ show(candidate_start[last] + candidate_report_span[last] - candidate_start[candidate])
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ ",\"report_ids\":["
    ++ join(",", [
      "\"" ++ candidate_report_id[position] ++ "\""
      | member in members, position in candidate_report_start[member]..candidate_report_start[member + 1] - 1
    ])
    ++ "],\"utility\":{\"recall\":" ++ show(sum(c in members)(candidate_recall[c]))
    ++ ",\"estimation\":" ++ show(sum(c in members)(candidate_estimation[c]))
    ++ ",\"omission_yield\":" ++ show(sum(c in members)(candidate_omission[c]))
    ++ ",\"combined\":" ++ show(sum(c in members)(candidate_combined_score[c]))
    ++ ",\"scale\":" ++ show(score_scale) ++ "}}}"
    | run in index_set(run_start)
  ])
  ++ "]}"
];
