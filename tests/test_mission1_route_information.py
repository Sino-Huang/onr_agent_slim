"""Route-dependent score diagnostics; not an integration claim for the planner."""

import importlib.util
import json
import subprocess
from collections import Counter
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest
from test_mission1_planning import _belief, _environment, _report

from onr.application.mission1_planning import (
    SCORE_SCALE,
    CandidateDAG,
    Mission1ReplanGate,
    _fixed_view_runs,
    _opportunities,
    allocate_route_information,
    build_candidate_dag,
    expand_information_states,
    longest_path_oracle,
    route_information_oracle,
    route_information_tables,
)
from onr.contracts.fsm import FSMStatus, Statechart

SPEC = importlib.util.spec_from_file_location(
    "route_information_diagnostic", Path("scripts/inspect_mission1_route_information.py"),
)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


def solution(*reports):
    return {"assignments": [{"parameters": {"report_ids": list(reports)}}]}


def history_graph():
    environment = _environment([_report("a",1,10,0,0),_report("b",2,10,20,0),_report("c",1,20,0,0)],fov=1)
    belief = _belief((1,2))
    generated = build_candidate_dag(environment,belief)
    singles = [next(c for c in generated.candidates if c.mode=="fixed_view" and c.report_ids==(r,))
               for r in ("a","b","c")]
    a,b,d = [replace(c,recall_utility=recall,omission_yield=0) for c,recall in zip(singles,(.2,0,0))]
    merge = replace(d,candidate_id="merge",report_ids=(),start_s=15,end_s=15.5,
                    recall_utility=0,estimation_utility=0,omission_yield=0,combined_score=0,
                    scored_observation_windows=((15,15.5),))
    return CandidateDAG((a,b,merge,d),((0,1),(0,2),(1,3),(2,3),(3,4),(4,5)),0,5), _opportunities(environment,belief)


def test_count_label_oracle_keeps_a_weaker_prefix_with_better_future_information():
    graph,opportunities = history_graph()
    assert longest_path_oracle(graph).covered_report_ids == ("a","c")
    # At the report-free merge, observing a has higher score than observing b.
    # The later c observation makes the b prefix better; both must survive.
    selected = route_information_oracle(graph,opportunities)
    assert selected.covered_report_ids == ("b","c")
    assert selected.score == 1
    assert sum(round(c.estimation_utility*SCORE_SCALE) for c in selected.candidates)==SCORE_SCALE


def test_expanded_graph_existing_model_matches_count_label_reference(tmp_path):
    from test_mission1_planning import EXAMPLE_ROOT

    from onr.application.mission1_planning import serialize_minizinc_data

    graph,opportunities=history_graph()
    reference=route_information_oracle(graph,opportunities)
    expanded=expand_information_states(graph,opportunities)
    oracle=longest_path_oracle(expanded)
    assert oracle.covered_report_ids==reference.covered_report_ids
    assert oracle.score==reference.score
    assert oracle.duration_s==reference.duration_s
    assert all(u<v for u,v in expanded.arcs)
    assert len({c.candidate_id for c in expanded.candidates})==len(expanded.candidates)
    data=tmp_path/"lifted.dzn"
    data.write_text(serialize_minizinc_data(expanded))
    native=subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc","--solver","coin-bc",
        str(EXAMPLE_ROOT/"model.mzn"),str(data),
    ],capture_output=True,text=True,check=True,timeout=30)
    result=json.loads(native.stdout.splitlines()[0])
    assert result["combined_score"]==round(reference.score*SCORE_SCALE)
    assert [a["candidate_id"] for a in result["assignments"]]==[c.candidate_id for c in oracle.candidates]
    assert [r for a in result["assignments"] for r in a["parameters"]["report_ids"]]==list(reference.covered_report_ids)
    assert "==========" in native.stdout


def test_expanded_graph_forgets_only_future_irrelevant_counts():
    graph, opportunities = history_graph()
    expanded = expand_information_states(graph, opportunities)
    # Ship 2 never appears after branch b; its count must disappear at merge.
    merges = [c for c in expanded.candidates if c.candidate_id.startswith("merge:counts:")]
    assert {c.candidate_id for c in merges} == {"merge:counts:0,0", "merge:counts:1,0"}
    # Ship 1 still appears later, so its distinct histories must survive.
    assert len(merges) == 2
    reference = route_information_oracle(graph, opportunities)
    result = longest_path_oracle(expanded)
    assert (result.score, result.duration_s, result.covered_report_ids) == (
        reference.score, reference.duration_s, reference.covered_report_ids,
    )


def test_expanded_graph_merges_histories_after_last_observation():
    graph, opportunities = history_graph()
    end = replace(graph.candidates[2], candidate_id="end", start_s=25, end_s=25.5)
    graph = CandidateDAG((*graph.candidates, end),
                         (*graph.arcs[:-1], (4, 5), (5, 6)), 0, 6)
    expanded = expand_information_states(graph, opportunities)
    assert sum(c.candidate_id.startswith("end:counts:") for c in expanded.candidates) == 1
    reference = route_information_oracle(graph, opportunities)
    result = longest_path_oracle(expanded)
    assert result.score == reference.score
    assert result.duration_s == reference.duration_s


def multi_view_environment():
    environment = _environment([_report("a", 1, 10, 0, 0), _report("b", 2, 10, 0, 0)], fov=1)
    environment["observation_window_seconds"] = 4
    environment["controlled_vehicle"]["quarter_turn_seconds"] = 0.5
    environment["surveillance_views"] = [
        {"x": 0, "y": 0, "arrival_direction": 0, "report_ids": ["a"], "observation_delay_s": 0},
        {"x": 0, "y": 0, "arrival_direction": 1, "report_ids": ["b"], "observation_delay_s": 1},
        {"x": 0, "y": 0, "arrival_direction": 2, "report_ids": ["a"], "observation_delay_s": 2},
    ]
    return environment


def test_bounded_multi_view_native_route_observes_two_vessels_at_one_epoch(tmp_path):
    from test_mission1_planning import EXAMPLE_ROOT

    from onr.application.mission1_planning import serialize_minizinc_data

    environment = multi_view_environment()
    belief = _belief((1, 2))
    graph = build_candidate_dag(environment, belief, information_horizon_seconds=20)
    route = longest_path_oracle(graph)
    assert set(route.covered_report_ids) == {"a", "b"}
    assert len(route.covered_report_ids) == 2
    assert len(route.candidates) == 2
    data = tmp_path / "multi-view.dzn"
    data.write_text(serialize_minizinc_data(graph))
    native = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(native.stdout.splitlines()[0])
    assert "==========" in native.stdout
    assert result["combined_score"] == round(route.score * SCORE_SCALE)
    assert [r for a in result["assignments"] for r in a["parameters"]["report_ids"]] == list(route.covered_report_ids)


def test_report_history_keeps_direct_alternative_and_blocks_nonadjacent_batch_reuse():
    from unittest.mock import patch

    from onr.application import mission1_planning as planning

    environment = multi_view_environment()
    # A different report from the same vessel/epoch still owns the same
    # omission cell. It cannot be credited as a second batch after visiting b.
    environment["static_info"].append(_report("c", 1, 10, 0, 0))
    environment["surveillance_views"][-1]["report_ids"] = ["c"]
    belief = _belief((1, 2))
    with patch.object(planning, "expand_information_states", side_effect=lambda g, o: g):
        graph = build_candidate_dag(environment, belief, information_horizon_seconds=20)
    assert all((0, i) in graph.arcs for i in range(1, graph.sink))
    opportunities = _opportunities(environment, belief)
    reference = route_information_oracle(graph, opportunities)
    expanded = longest_path_oracle(expand_information_states(graph, opportunities))
    for route in (reference, expanded):
        assert "b" in route.covered_report_ids
        assert len(set(route.covered_report_ids) & {"a", "c"}) == 1
    assert expanded.score == reference.score
    assert expanded.duration_s == reference.duration_s


def test_history_aware_arcs_reduce_only_after_intermediate_batch_expires():
    from onr.application.mission1_planning import _candidate_arcs

    graph, _ = history_graph()
    a = replace(graph.candidates[0], start_s=10, end_s=10.5,
                observation_delay_s=0, x=0, y=0, end_x=0, end_y=0)
    b = replace(graph.candidates[1], start_s=14, end_s=14.5,
                observation_delay_s=4, x=0, y=0, end_x=0, end_y=0)
    c = replace(graph.candidates[3], start_s=20, end_s=20.5,
                x=0, y=0, end_x=0, end_y=0)
    arcs = _candidate_arcs((a, b, c), 30, information_aware=True, report_history_aware=True)
    # b can still carry a same-epoch alternative: source->b is retained.
    assert (0, 2) in arcs
    # a is available from the empty prefix, and has expired before c; adding
    # it strictly improves utility without restricting anything after c.
    assert (0, 3) not in arcs


def test_history_safe_pruning_uses_valid_chains_not_only_direct_neighbors():
    from onr.application.mission1_planning import _candidate_arcs

    graph, _ = history_graph()
    candidates = tuple(replace(graph.candidates[0], candidate_id=str(i), report_ids=(str(i),),
                              start_s=10 * i, end_s=10 * i + .5, x=0, y=0, end_x=0, end_y=0)
                       for i in range(1, 7))
    arcs = _candidate_arcs(candidates, 30, information_aware=True, report_history_aware=True)
    assert arcs == tuple((i, i + 1) for i in range(7))


def test_pursuit_successor_is_reachable_from_either_endpoint() -> None:
    from onr.application.mission1_planning import _candidate_arcs

    graph, _ = history_graph()
    template = graph.candidates[0]
    pursuit = replace(
        template,
        candidate_id="pursuit",
        mode="pursue_ship",
        entity_id=1,
        report_ids=("pursuit-report",),
        start_s=0,
        end_s=3,
        x=0,
        y=0,
        end_x=100,
        end_y=0,
        arrival_direction=None,
    )
    fixed = replace(
        template,
        candidate_id="fixed",
        report_ids=("fixed-report",),
        start_s=4.5,
        end_s=5,
        x=100,
        y=0,
        end_x=100,
        end_y=0,
        arrival_direction=0,
    )

    arcs = _candidate_arcs((pursuit, fixed), 30, 0.5)

    assert (1, 2) not in arcs


def test_separate_route_horizon_retains_late_recall_without_late_information():
    environment = _environment([_report("early", 1, 10, 0, 0), _report("late", 1, 30, 0, 0)], fov=1)
    belief = _belief((1,))
    bounded = build_candidate_dag(environment, belief, information_horizon_seconds=15)
    assert "late" not in longest_path_oracle(bounded).covered_report_ids
    extended = build_candidate_dag(environment, belief, information_horizon_seconds=15,
                                   route_horizon_seconds=40)
    route = longest_path_oracle(extended)
    assert set(route.covered_report_ids) == {"early", "late"}
    late = [c for c in extended.candidates if c.report_ids == ("late",)]
    assert late and all(c.estimation_utility == 0 and c.recall_utility > 0 for c in late)


def test_information_deadline_uses_capture_finish_not_report_epoch():
    graph, opportunities = history_graph()
    a = graph.candidates[0]
    late = replace(a, observation_delay_s=4, start_s=a.start_s + 4, end_s=a.end_s + 4)
    deadline = a.end_s
    timely = allocate_route_information((a,), opportunities, information_deadline_s=deadline)
    delayed = allocate_route_information((late,), opportunities, information_deadline_s=deadline)
    assert timely[0].estimation_utility > 0
    assert delayed[0].estimation_utility == 0
    assert delayed[0].recall_utility == a.recall_utility


def test_batch_history_ignores_temporally_unreachable_alternatives():
    graph, opportunities = history_graph()
    a = replace(graph.candidates[0], arrival_direction=0)
    b = replace(graph.candidates[1], candidate_id="b", start_s=11, end_s=11.5,
                x=0, y=0, end_x=0, end_y=0, arrival_direction=1, observation_delay_s=1)
    c = replace(a, candidate_id="late-a", start_s=11, end_s=11.5, observation_delay_s=1)
    graph = CandidateDAG((a, b, c), ((0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (1, 4), (2, 4), (3, 4)), 0, 4)
    expanded = expand_information_states(graph, opportunities, information_deadline_s=0)
    # Once b finishes, c cannot follow: both started at 11. No report history
    # needs to distinguish the direct-b prefix from a->b any longer.
    assert sum(c.candidate_id.startswith("b:counts:") for c in expanded.candidates) == 1
    reference = route_information_oracle(graph, opportunities, information_deadline_s=0)
    actual = longest_path_oracle(expanded)
    assert actual.score == reference.score
    assert actual.covered_report_ids == reference.covered_report_ids


def test_route_tail_native_oracle_and_gate_share_information_deadline(tmp_path):
    from unittest.mock import patch

    from test_mission1_planning import EXAMPLE_ROOT

    from onr.application import mission1_planning as planning

    environment = _environment([_report("early", 1, 10, 0, 0), _report("late", 1, 30, 0, 0)], fov=1)
    belief = _belief((1,))
    kwargs = {"information_horizon_seconds": 15, "route_horizon_seconds": 40}
    graph = build_candidate_dag(environment, belief, **kwargs)
    route = longest_path_oracle(graph)
    with patch.object(planning, "expand_information_states", side_effect=lambda g, o, **kw: g):
        base = build_candidate_dag(environment, belief, **kwargs)
    reference = route_information_oracle(base, _opportunities(environment, belief), information_deadline_s=15)
    assert (route.score, route.duration_s, route.covered_report_ids) == (
        reference.score, reference.duration_s, reference.covered_report_ids)
    contexts = {}
    for index, candidate in enumerate(route.candidates):
        contexts[str(index)] = {"candidate_id": candidate.candidate_id, "surveillance_mode": candidate.mode,
            "target_entity_id": candidate.entity_id, "target_report_ids": list(candidate.report_ids),
            "observation_window": {"start": {"seconds": candidate.start_s}, "duration": {"seconds": candidate.duration_s}},
            "planner_item": {"parameters": {"x": candidate.x, "y": candidate.y}}}
    chart = Statechart(mission_id="mission-1", plan_revision=1, mission_snapshot_id="snapshot-1",
        planning_profile="temporal", entry_state="0", states=tuple(contexts), transitions=(),
        terminal_states=tuple(contexts), state_context=contexts)
    status = FSMStatus(mission_id="mission-1", plan_revision=1, statechart_revision=1,
                       active_state="0", active_state_context=contexts["0"])
    decision, advisory = Mission1ReplanGate(**kwargs).assess(environment, belief, chart, status)
    assert decision.current_score == pytest.approx(route.score, abs=1e-6)
    assert advisory.score == route.score
    assert not decision.trigger
    data = tmp_path / "tail.dzn"
    data.write_text(planning.serialize_minizinc_data(graph))
    native = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(native.stdout.splitlines()[0])
    assert "==========" in native.stdout
    assert result["combined_score"] == round(reference.score * SCORE_SCALE)
    assert [r for a in result["assignments"] for r in a["parameters"]["report_ids"]] == list(route.covered_report_ids)


@pytest.mark.parametrize("route_horizon", [0, 14, float("nan"), float("inf")])
def test_invalid_route_horizon_rejected(route_horizon):
    with pytest.raises(ValueError, match="horizon"):
        build_candidate_dag(_environment([_report("a", 1, 10, 0, 0)]), _belief((1,)),
                            information_horizon_seconds=15, route_horizon_seconds=route_horizon)


def test_failed_generation_preserves_public_horizon_inputs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("plan_evaluation", Path("scripts/evaluate_mission1_plan.py"))
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    environment = _environment([_report("a", 1, 10, 0, 0)])
    belief = _belief((1,))
    output = tmp_path / "new-evaluation"

    def fail(*args, **kwargs):
        raise RuntimeError("generation failed")

    monkeypatch.setattr(evaluation, "build_candidate_dag", fail)
    with pytest.raises(RuntimeError, match="generation failed"):
        evaluation.solve_public_plan(environment, belief, Path("unused"), Path("unused"), output,
                                     information_horizon_seconds=15, route_horizon_seconds=40)
    assert json.loads((output / "environment.json").read_text()) == environment
    context = json.loads((output / "planning-context.json").read_text())
    assert context["route_horizon_seconds"] == 40
    assert context["information_deadline_s"] == environment["mission_time_seconds"] + 15
    assert (output / "belief.json").is_file()
    assert not (output / "solution.json").exists()


@pytest.mark.parametrize("seed", range(16))
@pytest.mark.parametrize("information_deadline", [None, 10.5])
def test_history_safe_reduction_matches_dense_independent_reference(seed, information_deadline):
    import random

    from onr.application.mission1_planning import _candidate_arcs, _navigation_time

    rng = random.Random(seed)
    environment = _environment([
        _report(f"r{i}", 1 + i % 2, 5 + 5 * (i // 2), 0, 0) for i in range(6)
    ], fov=1)
    belief = _belief((1, 2))
    opportunities = _opportunities(environment, belief)
    template = build_candidate_dag(environment, belief).candidates[0]
    candidates = []
    for item in opportunities:
        for delay in (0, 1, 4):
            x, y = rng.choice((0, 10)), rng.choice((0, 10))
            covered = ([r for r in opportunities if r.time_s == item.time_s]
                       if delay == 0 and rng.randrange(2) else [item])
            recall = .5 * sum(r.recall for r in covered)
            candidates.append(replace(template, candidate_id=f"{item.report_id}:{delay}",
                report_ids=tuple(r.report_id for r in covered), start_s=item.time_s + delay,
                end_s=item.time_s + delay + .5, x=x, y=y, end_x=x, end_y=y,
                mode="fixed_view", arrival_direction=rng.randrange(4), report_span_s=0,
                observation_delay_s=delay, recall_utility=recall,
                omission_yield=0, estimation_utility=0, combined_score=recall))
    candidates = tuple(sorted(candidates, key=lambda c: (c.start_s, c.end_s, c.candidate_id)))
    sink = len(candidates) + 1
    dense = {(0, sink)} | {(0, i) for i in range(1, sink)} | {(i, sink) for i in range(1, sink)}
    for i, left in enumerate(candidates, 1):
        for j, right in enumerate(candidates, 1):
            if i < j and set(left.report_ids).isdisjoint(right.report_ids) and right.start_s >= left.end_s + _navigation_time(
                left.end_x, left.end_y, right.x, right.y, 30,
                left.arrival_direction, right.arrival_direction, .5,
            ):
                dense.add((i, j))
    reference = route_information_oracle(CandidateDAG(candidates, tuple(sorted(dense)), 0, sink), opportunities,
                                         information_deadline_s=information_deadline)
    arcs = _candidate_arcs(candidates, 30, .5, information_aware=True, report_history_aware=True)
    reduced = route_information_oracle(CandidateDAG(candidates, arcs, 0, sink), opportunities,
                                       information_deadline_s=information_deadline)
    assert (reduced.score, len(reduced.candidates), reduced.duration_s) == (
        reference.score, len(reference.candidates), reference.duration_s)
    assert reduced.covered_report_ids == reference.covered_report_ids


def test_route_information_assignment_rounding_telescopes_across_merged_views():
    graph,opportunities = history_graph()
    path = (graph.candidates[0],graph.candidates[2],graph.candidates[3])
    allocated = allocate_route_information(path,opportunities)
    table = route_information_tables(opportunities)[1]
    assert round(allocated[0].estimation_utility*SCORE_SCALE)==table[1]
    assert allocated[1].estimation_utility==0
    assert round(allocated[2].estimation_utility*SCORE_SCALE)==table[2]-table[1]
    assert sum(round(c.estimation_utility*SCORE_SCALE) for c in _fixed_view_runs(allocated))==table[2]
    assert [(c.recall_utility,c.omission_yield) for c in allocated]==[(c.recall_utility,c.omission_yield) for c in path]


def test_route_information_rejects_duplicate_and_unavailable_credit():
    graph,opportunities = history_graph()
    with pytest.raises(ValueError,match="repeats"):
        allocate_route_information((graph.candidates[0],graph.candidates[0]),opportunities)
    with pytest.raises(ValueError,match="unavailable"):
        allocate_route_information((graph.candidates[0],),[o for o in opportunities if o.report_id!="a"])


def test_count_label_oracle_keeps_lexicographic_preferences_for_equal_information():
    graph,opportunities = history_graph()
    a=graph.candidates[0]
    # Same observation/value; holding one pose beats an extra maneuver.
    b=replace(a,candidate_id="other-view",x=a.x+1)
    d=graph.candidates[3]
    same=CandidateDAG((a,b,d),((0,1),(0,2),(1,3),(2,3),(3,4)),0,4)
    result=route_information_oracle(same,opportunities)
    assert len(result.candidates)==1
    assert result.candidates[0].candidate_id==a.candidate_id+"--"+d.candidate_id


def test_bounded_builder_and_gate_share_selected_route_information():
    environment=_environment([_report("a",1,10,0,0),_report("b",1,20,0,0),_report("later",1,100,0,0)])
    belief=_belief((1,))
    route=longest_path_oracle(build_candidate_dag(environment,belief,information_horizon_seconds=30))
    assert route.covered_report_ids==("a","b")
    assert len(route.candidates)==1
    candidate=route.candidates[0]
    context={"candidate_id":candidate.candidate_id,"surveillance_mode":candidate.mode,
             "target_entity_id":candidate.entity_id,"target_report_ids":list(candidate.report_ids),
             "observation_window":{"start":{"seconds":candidate.start_s},"duration":{"seconds":candidate.duration_s}},
             "planner_item":{"parameters":{"x":candidate.x,"y":candidate.y}}}
    chart=Statechart(mission_id="mission-1",plan_revision=1,mission_snapshot_id="snapshot-1",
                     planning_profile="temporal",entry_state="view",states=("view",),transitions=(),
                     terminal_states=("view",),state_context={"view":context})
    status=FSMStatus(mission_id="mission-1",plan_revision=1,statechart_revision=1,active_state="view",active_state_context=context)
    decision,advisory=Mission1ReplanGate(information_horizon_seconds=30).assess(environment,belief,chart,status)
    assert decision.current_score==pytest.approx(route.score,abs=1e-6)
    assert advisory.score==route.score
    assert not decision.trigger
    assert len(environment["static_info"])==3


@pytest.mark.parametrize("horizon", [0,-1,float("nan")])
def test_invalid_information_horizon_is_rejected(horizon):
    with pytest.raises(ValueError,match="horizon"):
        build_candidate_dag(_environment([_report("a",1,10,0,0)]),_belief((1,)),information_horizon_seconds=horizon)


def test_information_only_intermediate_cannot_prune_a_shorter_saturated_route():
    from onr.application.mission1_planning import _candidate_arcs

    graph,opportunities=history_graph()
    candidates=tuple(replace(c,recall_utility=0,omission_yield=0,estimation_utility=.25)
                     for c in (graph.candidates[0],graph.candidates[3]))
    opportunities=tuple(replace(o,variance=.1,estimation=.1) for o in opportunities if o.entity_id==1)
    arcs=_candidate_arcs(candidates,10,information_aware=True)
    assert (0,2) in arcs
    selected=longest_path_oracle(expand_information_states(CandidateDAG(candidates,arcs,0,3),opportunities))
    assert selected.score==.5
    assert len(selected.covered_report_ids)==1
    assert selected.duration_s==.5


def test_first_selected_check_does_not_pay_for_unobserved_schedule():
    environment = _environment([_report(str(i), 1, 10 + i, 0, 0) for i in range(20)])
    row = diagnostic.inspect_route(environment, _belief((1,)), solution("19"))["ships"][0]
    assert row["selected_route_information_credit"] == pytest.approx(.5)
    assert row["scheduled_information_credit"] < .05
    assert row["first_selected_report_time_s"] == 29


def test_full_selected_schedule_preserves_existing_budget():
    environment = _environment([_report(str(i), 1, 10 + i, 0, 0) for i in range(20)])
    result = diagnostic.inspect_route(environment, _belief((1,)), solution(*map(str, range(20))))
    assert result["scheduled_information_credit"] == pytest.approx(result["selected_route_information_credit"])


def test_distinct_vessels_have_independent_information_budgets():
    environment = _environment([_report("a", 1, 10, 0, 0), _report("b", 1, 20, 0, 0),
                                _report("c", 2, 20, 0, 0)])
    repeated = diagnostic.inspect_route(environment, _belief((1, 2)), solution("a", "b"))
    diverse = diagnostic.inspect_route(environment, _belief((1, 2)), solution("a", "c"))
    assert diverse["selected_route_information_credit"] == pytest.approx(1)
    assert repeated["selected_route_information_credit"] < 1


def test_curve_is_monotone_with_diminishing_marginals_and_variance_cap():
    variance, gain = .1, .03
    curve = diagnostic.information_curve(variance, gain, gain, 20)
    increments = [b - a for a, b in pairwise(curve)]
    assert all(a > b > 0 for a, b in pairwise(increments))
    assert curve[-1] * 2 * gain < variance
    assert diagnostic.information_curve(0, 0, 0, 3) == [0, 0, 0, 0]


def test_unavailable_and_repeated_reports_are_not_credited():
    environment = _environment([_report("old", 1, 1, 0, 0), _report("done", 1, 10, 0, 0),
                                _report("next", 1, 20, 0, 0)])
    environment["mission_time_seconds"] = 5
    environment["world_model_info"]["event_report_checks"] = [{"report_id": "done"}]
    for ids in (("old",), ("done",), ("next", "next")):
        with pytest.raises(ValueError):
            diagnostic.inspect_route(environment, _belief((1,)), solution(*ids))
    result = diagnostic.inspect_route(environment, _belief((1,)), solution("next"))
    assert result["ships"][0]["remaining_public_reports"] == 1


def test_native_route_count_counterexample_to_additive_prefix_pruning(tmp_path):
    # Diamond: 0 -> (A: ship1 or B: ship2) -> C: ship1 -> sink.
    # Uniform schedule credit selects A: 20+33+33 > 50+33.
    # Selected-count credit selects B: 50+50 > 20+67.
    paths = ((1, 0, 1), (0, 1, 1))
    uniform = (33, 50, 33)
    base = (20, 0, 0)
    curve = (0, 50, 67)
    additive = lambda path: sum(x * (a + b) for x, a, b in zip(path, base, uniform))
    route_score = lambda path: sum(x * b for x, b in zip(path, base)) + curve[path[0] + path[2]] + curve[path[1]]
    assert max(paths, key=additive) == paths[0]
    assert max(paths, key=route_score) == paths[1]
    model = tmp_path / "route-count-counterexample.mzn"
    model.write_text('''
array[1..3] of var 0..1: chosen;
array[0..2] of int: info = array1d(0..2, [0, 50, 67]);
constraint chosen[1] + chosen[2] = 1;
constraint chosen[3] = 1;
var int: score = 20 * chosen[1] + info[chosen[1] + chosen[3]] + info[chosen[2]];
solve maximize score;
output ["{\\"chosen\\":" ++ show(chosen) ++ ",\\"score\\":" ++ show(score) ++ "}"];
''')
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model),
    ], capture_output=True, text=True, check=True, timeout=30)
    native = json.loads(result.stdout.splitlines()[0])
    assert tuple(native["chosen"]) == max(paths, key=route_score)
    assert native["score"] == 100
    assert "==========" in result.stdout


@pytest.mark.parametrize("encoding", ["lookup", "envelope"])
@pytest.mark.parametrize("compress", ["none", "suffix", "interval"])
def test_benchmark_native_encodings_match_exhaustive_diamond(tmp_path, monkeypatch, encoding, compress):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    model = tmp_path / "probe.mzn"
    data = tmp_path / "data.dzn"
    model.write_text(benchmark.information_model(encoding))
    arcs = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4)]
    nodes = 5
    if compress != "none":
        compressor = {"suffix": benchmark.compress_arc_suffixes, "interval": benchmark.compress_arc_intervals}[compress]
        nodes, arcs = compressor(3, arcs)
    arrays = {"af": [u for u, _ in arcs], "at": [v for _, v in arcs],
              "ie": [i+1 for i in sorted(range(len(arcs)), key=lambda i: (arcs[i][1], arcs[i][0]))]}
    lines = [f"C=3; A={len(arcs)}; S=2; R=3; K=2; N={nodes};",
             "base=[20,0,0]; rc=[1,2,3]; rs=[1,2,1];",
             "gain=array2d(1..2,0..2,[0,50,67,0,50,67]);"]
    lines.extend(f"{key}={json.dumps(value)};" for key, value in arrays.items())
    for name, counts in (("os", Counter(arrays["af"])), ("ins", Counter(arrays["at"]))):
        offsets = [1]
        for node in range(nodes):
            offsets.append(offsets[-1] + counts[node])
        lines.append(f"{name}=array1d(0..N,{json.dumps(offsets)});")
    data.write_text("\n".join(lines))
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(model), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    native = json.loads(result.stdout.splitlines()[0])
    assert native == {"objective": 100, "selected": [2, 3]}
    assert "==========" in result.stdout


@pytest.mark.parametrize("compression", ["suffix", "interval"])
def test_compression_preserves_every_candidate_path(monkeypatch, compression):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    def paths(arcs, count):
        outgoing = {}
        for u, v in arcs:
            outgoing.setdefault(u, []).append(v)
        def walk(node, visited):
            if node == count + 1:
                return {visited}
            return set().union(*(walk(v, visited + ((v,) if 1 <= v <= count else ()))
                                 for v in outgoing.get(node, [])))
        return walk(0, ())

    # Enumerate all edge subsets of a small ordered graph, including no route.
    edges = [(u, v) for u in range(4) for v in range(u + 1, 5)]
    for mask in range(1 << len(edges)):
        arcs = [edge for i, edge in enumerate(edges) if mask & (1 << i)]
        compressor = {"suffix": benchmark.compress_arc_suffixes, "interval": benchmark.compress_arc_intervals}[compression]
        _, compressed = compressor(3, arcs)
        assert paths(arcs, 3) == paths(compressed, 3)


@pytest.mark.parametrize("speed,expected", [(10.0, {"objective":100,"selected":[2,3]}),
                                            (.5, {"objective":87,"selected":[1,3]})])
def test_epoch_model_information_and_travel(tmp_path, monkeypatch, speed, expected):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark
    model, data = tmp_path / "epoch.mzn", tmp_path / "epoch.dzn"
    model.write_text(benchmark.EPOCH_MODEL)
    data.write_text('''
C=3; S=2; R=3; K=2; E=2; U=3;
base=[20,0,0]; rc=[1,2,3]; rs=[1,2,1]; ru=[1,2,3];
gain=array2d(1..2,0..2,[0,50,67,0,50,67]);
epoch=[1,1,2]; sx=[0.0,10.0,0.0]; sy=[0.0,0.0,0.0];
ex=[0.0,10.0,0.0]; ey=[0.0,0.0,0.0];
start=[10.0,10.0,30.0]; finish=[10.5,10.5,30.5];
direction=[3,3,3]; first_report=[20,20,60]; last_report=[20,20,60];
initial_x=0.0; initial_y=0.0; initial_time=0.0; initial_direction=3;
quarter_turn=0.5; chronological=true;
''' + f"speed={speed};")
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.splitlines()[0]) == expected
    assert "==========" in result.stdout


def test_epoch_turn_formula_matches_python_all_discrete_cases(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    from onr.application.mission1_planning import _navigation_turns
    cases = [(x,y,a,b) for x in (-1,0,1) for y in (-1,0,1) for a in (-1,0,1,2,3) for b in (-1,0,1,2,3)]
    expected = [_navigation_turns(None if x==0 else (3 if x>0 else 1),
                                 None if y==0 else (0 if y>0 else 2),
                                 None if a==-1 else a,None if b==-1 else b) for x,y,a,b in cases]
    functions = benchmark.EPOCH_MODEL.split("function var int: turn",1)[1].split("constraint forall(c in CS)",1)[0]
    expressions = [f"navigation_turns({float(x)},{float(y)},{a},{b})" for x,y,a,b in cases]
    model = tmp_path / "turns.mzn"
    model.write_text("function var int: turn"+functions+"\narray[1..225] of var int: values=["
                     +",".join(expressions)+"]; solve satisfy; output [show(values)];")
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.splitlines()[0]) == expected


def test_probe_verifier_rejects_wrong_score_duplicate_and_unreachable_routes(monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    from onr.application.mission1_planning import _opportunities, build_candidate_dag
    environment = _environment([_report("a",1,10,0,0)])
    belief = _belief((1,))
    candidates = build_candidate_dag(environment,belief).candidates
    opportunities = _opportunities(environment,belief)
    c = candidates[0]
    score = round(c.recall_utility*1_000_000)+round(c.omission_yield*1_000_000)+500_000
    solution = {"selected":[1],"objective":score}
    assert benchmark.verify_probe_route(candidates,solution,environment,opportunities,belief)["travel_and_unique_credit_verified"]
    for bad in ({"selected":[1],"objective":score+1},{"selected":[1,1],"objective":score}):
        with pytest.raises(AssertionError):
            benchmark.verify_probe_route(candidates,bad,environment,opportunities,belief)
    environment["controlled_vehicle"]["position"]["x"]=10_000
    with pytest.raises(AssertionError):
        benchmark.verify_probe_route(candidates,solution,environment,opportunities,belief)
