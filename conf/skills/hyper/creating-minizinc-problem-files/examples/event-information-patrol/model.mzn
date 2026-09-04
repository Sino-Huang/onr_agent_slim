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
var float: maneuver_count = sum(candidate in CANDIDATES)(selected[candidate]);
var float: surveillance_duration = sum(candidate in CANDIDATES)(
  candidate_duration[candidate] * selected[candidate]
);
var float: candidate_order_sum = sum(candidate in CANDIDATES)(
  candidate * selected[candidate]
);
var float: objective =
  combined_score * maneuver_bound * duration_bound * tie_break_bound
  - maneuver_count * duration_bound * tie_break_bound
  - surveillance_duration * tie_break_bound
  - candidate_order_sum;

solve maximize objective;

output [
  "{\"combined_score\":" ++ show(round(fix(combined_score)))
  ++ ",\"score_scale\":" ++ show(score_scale)
  ++ ",\"maneuver_count\":" ++ show(round(fix(maneuver_count)))
  ++ ",\"surveillance_duration\":" ++ show(round(fix(surveillance_duration)))
  ++ ",\"assignments\":["
  ++ join(",", [
    "{\"candidate_id\":\"" ++ candidate_id[candidate] ++ "\","
    ++ "\"surveillance_mode\":\""
    ++ (if candidate_mode[candidate] = 1 then "fixed_view" else "pursue_ship" endif)
    ++ "\",\"entity_id\":"
    ++ (if candidate_entity_id[candidate] = 0 then "null" else show(candidate_entity_id[candidate]) endif)
    ++ ",\"start\":" ++ show(candidate_start[candidate])
    ++ ",\"duration\":" ++ show(candidate_duration[candidate])
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ ",\"observation_window\":{\"start\":" ++ show(candidate_start[candidate])
    ++ ",\"duration\":" ++ show(candidate_duration[candidate])
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
    ++ ",\"report_span\":" ++ show(candidate_report_span[candidate])
    ++ ",\"time_scale\":" ++ show(time_scale)
    ++ ",\"report_ids\":["
    ++ join(",", [
      "\"" ++ candidate_report_id[position] ++ "\""
      | position in candidate_report_start[candidate]..candidate_report_start[candidate + 1] - 1
    ])
    ++ "],\"utility\":{\"recall\":" ++ show(candidate_recall[candidate])
    ++ ",\"estimation\":" ++ show(candidate_estimation[candidate])
    ++ ",\"omission_yield\":" ++ show(candidate_omission[candidate])
    ++ ",\"combined\":" ++ show(candidate_combined_score[candidate])
    ++ ",\"scale\":" ++ show(score_scale) ++ "}}}"
    | candidate in CANDIDATES where fix(selected[candidate]) > 0.5
  ])
  ++ "]}"
];
