import copy
import json
import math
import subprocess
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from test_object_search_belief import OBJECTIVE, PACKAGE, observation

from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.mission4_worker import (
    Mission4WorkerSession,
    main,
    play_request_script,
)
from onr.application.context_coordination import ContextCoordination
from onr.application.mission4_planning import (
    Mission4AdaptivePlanner,
    Mission4ReplanGate,
    _parse_static_task,
    decision_from_trigger,
    interpret_worker_text,
)
from onr.application.mission4_planning import (
    main as planning_main,
)


def section():
    return {"schema_version":1,"package":copy.deepcopy(PACKAGE),"revision":0,"status":"active","reason":None,
            "deadline_s":300,"objectives":{},"observations":[],"coverage":{},"requests":[]}


STATIC_PACKAGE={"schema_version":1,
    "vocabulary":{"entity":["person_group","person_in_distress","animal"],
                  "intent":["escape","follow"],
                  "species":["bear","boar","deer","deerdoe","deerstag","fox","kangaroo","koala","pig","wolf"]},
    "areas":{"task:1":{"polygon":[[-487.6,-121.86],[-467.6,-121.86],[-467.6,-101.86],[-487.6,-101.86]],"prior":1.0},
             "task:2":{"polygon":[[-54.1,-131.36],[-34.1,-131.36],[-34.1,-111.36],[-54.1,-111.36]],"prior":1.0},
             "task:3":{"polygon":[[-273.6,-404.36],[-253.6,-404.36],[-253.6,-384.36],[-273.6,-384.36]],"prior":1.0}},
    "found_threshold":0.1,"mission_time_budget_s":900.0,"obstacles":[],"keep_out_zones":[]}

DEMO_RESCUE_1=("A party of 4 mechanics are on watchout at (-477.60, -111.86). They heard reports of painful sobs "
               "from a person in need of help. They believe the individual is in the direction of (-0.62, -0.79). "
               "Where is the person in need of assistance located?")
DEMO_RESCUE_2=("A group of 4 students are on standby at (-44.10, -121.36). They stumbled upon blood splatters on "
               "the ground and are in search of an individual in need of assistance. They believe the person is in "
               "the direction of (-0.93, 0.38). Where is the person in need of assistance located?")
DEMO_ANIMAL_3=("An assembly of 2 volunteers are located at (-263.60, -394.36). They were on patrol for an animal. "
               "What direction should they head to in order to escape from the animal, and what type of animal are "
               "they trying to run away from, and where is the animal located?")


def static_section():
    return {"schema_version":1,"package":copy.deepcopy(STATIC_PACKAGE),"revision":0,"status":"active","reason":None,
            "deadline_s":900,"objectives":{},"observations":[],"coverage":{},"requests":[]}


def static_observation(index,position,attributes,*,track="seen:1",radius=1.0):
    return {"observation_id":f"obs:{index}","track_id":track,"acquired_at_s":index,
            "source":"simulated","coordinate_frame":"local_ned","position":position,
            "position_uncertainty_m":radius,"uncertainty_model":"categorical_symmetric_error_v1",
            "attributes":attributes}


def animal_observation(index,position,*,species="bear",species_u=0.2,track="seen:1",radius=1.0):
    return static_observation(index,position,
        {"entity":{"value":"animal","uncertainty":0.01},
         "species":{"value":species,"uncertainty":species_u}},track=track,radius=radius)


def accept(state,request,now=0):
    # Request receipt fixture. Actual runtime acceptance is tested in #33.
    assert request["base_revision"]==state["revision"]
    op=request["operation"]
    if op in {"add","amend"}:state["objectives"][request["objective"]["target_id"]]=request["objective"]
    if op=="remove":del state["objectives"][request["target_id"]]
    if op=="deadline":state["deadline_s"]=request["deadline_s"]
    if op=="cancel":state.update(status="cancelled",reason="worker_cancelled")
    if op=="finish":state.update(status="completed",reason=request["reason"])
    state["revision"]+=1
    state["requests"].append({"request":request,"revision":state["revision"],"accepted_at_s":now})


def environment(state, now=0, lifecycle=None):
    return {"mission_time_seconds":now,"state_version":int(now),
            "controlled_vehicle":{"position":{"x":0,"y":0,"z":-20}},
            "world_model_info":{"mission_mode":"mission4","mission4":state},"maneuver_lifecycle":lifecycle}


def test_text_add_clarify_amend_deadline_remove_cancel():
    state=section()
    first=interpret_worker_text("Please find a red container in dock",state,"wanted")
    accept(state,first["request"])
    accept(state,interpret_worker_text("also find a blue truck",state,"second")["request"],10)
    assert set(state["objectives"])=={"wanted","second"}
    assert state["deadline_s"]==300
    before=copy.deepcopy(state)
    assert interpret_worker_text("find a not red container",state,"bad")["kind"]=="clarification"
    assert interpret_worker_text("find red blue truck",state,"bad")["kind"]=="clarification"
    assert state==before
    accept(state,interpret_worker_text("change wanted to blue container in dock",state,"change")["request"],10)
    assert state["objectives"]["wanted"]["attributes"]["color"]=="blue"
    accept(state,interpret_worker_text("extend deadline to 400 seconds",state,"deadline")["request"],10)
    assert state["deadline_s"]==400
    assert interpret_worker_text("show evidence",state,"query")["kind"]=="query"
    accept(state,interpret_worker_text("remove second",state,"remove")["request"],10)
    accept(state,interpret_worker_text("cancel search",state,"cancel")["request"],11)
    assert interpret_worker_text("find red container",state,"new")["kind"]=="new_run_required"


def test_worker_queue_receipts_and_resume(tmp_path):
    session=Mission4WorkerSession("m4",tmp_path / "session.json",tmp_path / "requests")
    state=section()
    session.enqueue("find red container")
    session.enqueue("find blue truck")
    result=session.advance(state,0)
    assert result["request"]["operation"]=="add"
    pending=json.loads((tmp_path / "requests/00000001.json").read_text())
    resumed=Mission4WorkerSession("m4",tmp_path / "session.json",tmp_path / "requests")
    assert resumed.advance(state,0)["kind"]=="awaiting_runtime_receipt"
    assert len(list((tmp_path / "requests").glob("*.json")))==1
    accept(state,pending["request"])
    second=resumed.advance(state,0)
    accept(state,second["request"])
    resumed.advance(state,0)
    assert len(state["objectives"])==2
    assert resumed.data["pending"] is None


def test_adaptive_search_reuses_active_leg_and_prior_evidence():
    state=section();state["objectives"]={"wanted":copy.deepcopy(OBJECTIVE)};state["revision"]=1
    planner=Mission4AdaptivePlanner("m4")
    first=planner.decide(environment(state))
    assert first.action=="search_area"
    assert planner.decide(environment(state)) is None
    second=copy.deepcopy(OBJECTIVE);second.update(target_id="second",description="blue container")
    second["attributes"]["color"]="blue"
    state["objectives"]["second"]=second;state["revision"]=2
    active={"command_id":"c1","lifecycle":"active","action":"search_area"}
    assert planner.decide(environment(state,1,active)) is None
    assert set(planner.beliefs.data["objectives"])=={"wanted","second"}
    state["observations"]=[observation(0)]
    view=planner.decide(environment(state,2,active))
    assert view.action=="navigate" and view.reason=="additional_useful_view"
    saved=planner.state()
    restored=Mission4AdaptivePlanner("m4",state=saved)
    assert restored.decide(environment(state,2,active)) is None
    assert len(restored.beliefs.data["observations"])==1


def test_multiple_unresolved_targets_choose_joint_area_after_completed_leg():
    state=section();state["objectives"]={"wanted":copy.deepcopy(OBJECTIVE)};state["revision"]=1
    second=copy.deepcopy(OBJECTIVE);second.update(target_id="second",description="blue container")
    second["attributes"]["color"]="blue"
    state["objectives"]["second"]=second;state["revision"]=2
    completed={"command_id":"c1","lifecycle":"completed","action":"navigate"}
    decision=Mission4AdaptivePlanner("m4").decide(environment(state,5,completed))
    assert decision.action=="search_area"
    assert set(decision.target_ids)=={"wanted","second"}


def test_multiple_unresolved_still_investigate_a_precise_uninvestigated_track():
    # The v7 live-run deadlock: two targets unresolved at the strict threshold,
    # both hint views consumed by declined triggers, the swept area completed,
    # and no active leg — the planner must still pursue close-up sharpening
    # instead of re-emitting a declined sweep or freezing.
    state=static_section()
    accept(state,interpret_worker_text(DEMO_RESCUE_2,state,"worker:2")["request"])
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:3")["request"])
    planner=Mission4AdaptivePlanner("m4")
    planner.data["hinted_views"]=["worker:2"]
    planner.data["investigated_tracks"]=["seen:bear"]
    planner.data["completed_areas"]=["task:2"]
    planner.data["handled_commands"]=["c1"]
    state["observations"]=[
        static_observation(0,[-44.1,-121.36],
            {"entity":{"value":"person_in_distress","uncertainty":0.1}},track="seen:person"),
        animal_observation(1,[-263.6,-394.36],species_u=0.1,track="seen:bear"),
    ]
    completed={"command_id":"c1","lifecycle":"completed","action":"search_area"}
    decision=planner.decide(environment(state,500,completed))
    assert decision is not None and decision.action=="investigate"
    assert decision.parameters["track_id"]=="seen:person"
    assert "worker:2" in decision.target_ids


def test_active_area_interrupt_never_selects_the_current_viewpoint():
    state=section();state["objectives"]={"wanted":copy.deepcopy(OBJECTIVE)};state["revision"]=1
    state["observations"]=[observation(0)]
    active={"command_id":"c1","lifecycle":"active","action":"search_area"}
    env=environment(state,2,active)
    env["controlled_vehicle"]["position"]={"x":-4,"y":0,"z":-20}
    decision=Mission4AdaptivePlanner("m4").decide(env)
    assert decision.action=="navigate"
    assert (decision.parameters["x"],decision.parameters["y"])!=(-4,0)


def test_active_area_search_ignores_stale_out_of_area_match():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:1")["request"])
    planner=Mission4AdaptivePlanner("m4")
    first=planner.decide(environment(state,1))
    assert first.action=="search_area"
    active={"command_id":"c1","lifecycle":"active","action":"search_area"}
    # A fully identified track outside the objective's area must neither interrupt nor retrigger the sweep.
    state["observations"]=[animal_observation(0,[-105.1,-82.9,0],species_u=0.01)]
    for now in (2,3,4):
        assert planner.decide(environment(state,now,active)) is None


def test_active_area_search_resumes_after_match_views_are_exhausted():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:1")["request"])
    planner=Mission4AdaptivePlanner("m4")
    first=planner.decide(environment(state,1))
    assert first.action=="search_area"
    planner.data["investigated_tracks"].append("seen:1")
    planner.data["visited_views"].extend([["seen:1",index] for index in range(4)])
    # Precise in-area candidate whose views are exhausted must not retrigger the active sweep.
    state["observations"]=[animal_observation(0,[-260.0,-390.0,0],species_u=0.9)]
    active={"command_id":"c1","lifecycle":"active","action":"search_area"}
    for now in (2,3,4):
        assert planner.decide(environment(state,now,active)) is None


def test_final_reports_and_gate():
    state=section();state["objectives"]={"wanted":OBJECTIVE};state["revision"]=1
    state["observations"]=[observation(i) for i in range(3)]
    planner=Mission4AdaptivePlanner("m4")
    result=planner.decide(environment(state,3))
    assert result.action=="report" and result.reason=="all_found"
    assert result.report["targets"][0]["status"]=="found"
    empty=section();empty["objectives"]={"wanted":OBJECTIVE};empty.update(status="incomplete",reason="mission_deadline")
    result=Mission4AdaptivePlanner().decide(environment(empty,300))
    assert result.report["targets"][0]["status"]=="incomplete"
    gate=Mission4ReplanGate()
    assert gate.assess(environment(state,3)).startswith("mission4-gate:")
    assert gate.assess(environment(state,3)) is None
    assert gate.assess(environment(state,4)) is None
    assert gate.assess({"world_model_info":{"mission_mode":"mission1"}}) is None


def test_gate_refires_for_a_new_view_decision_with_same_action_and_reason():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_RESCUE_1,state,"worker:1")["request"])
    accept(state,interpret_worker_text(DEMO_RESCUE_2,state,"worker:2")["request"],5)
    gate=Mission4ReplanGate()
    first=gate.assess(environment(state,1))
    assert first is not None and first.startswith("mission4-gate:")
    assert gate.last_decision.action=="navigate"
    completed={"command_id":"c1","lifecycle":"completed","action":"navigate"}
    second=gate.assess(environment(state,40,completed))
    assert second is not None and second!=first
    assert gate.last_decision.action=="navigate"
    assert gate.last_decision.reason=="additional_useful_view"
    assert gate.assess(environment(state,40,completed)) is None


def test_gate_trigger_round_trips_the_stateful_decision():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_RESCUE_1,state,"worker:1")["request"])
    accept(state,interpret_worker_text(DEMO_RESCUE_2,state,"worker:2")["request"],5)
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:3")["request"],10)
    gate=Mission4ReplanGate()
    assert gate.assess(environment(state,1)) is not None
    assert gate.last_decision.action=="navigate"
    first_done={"command_id":"c1","lifecycle":"completed","action":"navigate"}
    assert gate.assess(environment(state,40,first_done)) is not None
    assert gate.last_decision.action=="navigate"
    second_done={"command_id":"c2","lifecycle":"completed","action":"navigate"}
    trigger=gate.assess(environment(state,100,second_done))
    assert gate.last_decision.action=="search_area"
    assert decision_from_trigger(trigger)==gate.last_decision
    # The stateless re-derivation replans used to run diverges on the same evidence.
    fresh=Mission4AdaptivePlanner().decide(environment(state,100,second_done))
    assert fresh is not None and fresh.action=="navigate"


def test_gate_trigger_round_trips_report_and_foreign_concatenation():
    state=section();state["objectives"]={"wanted":OBJECTIVE};state["revision"]=1
    state["observations"]=[observation(i) for i in range(3)]
    gate=Mission4ReplanGate()
    trigger=gate.assess(environment(state,3))
    decision=gate.last_decision
    assert decision.action=="report" and decision.report is not None
    parsed=decision_from_trigger(trigger)
    assert parsed is not None and parsed.action=="report" and parsed.reason==decision.reason
    assert parsed.report==json.loads(json.dumps(decision.report,sort_keys=True,default=str))
    assert decision_from_trigger(f"mission2-gate:x;{trigger}")==parsed
    assert decision_from_trigger(f"{trigger};mission2-gate:x")==parsed
    assert decision_from_trigger("mission2-gate:x") is None
    with pytest.raises(ValueError):
        decision_from_trigger("mission4-gate:{")


def test_planning_cli_and_helper_write_verified_adaptive_plan(tmp_path, capsys):
    state = section()
    state["objectives"] = {"wanted": copy.deepcopy(OBJECTIVE)}
    state["revision"] = 1
    source = tmp_path / "environment.json"
    source.write_text(json.dumps(environment(state)))
    manifest = tmp_path / "mission4-decision.json"
    model, data = tmp_path / "model.mzn", tmp_path / "data.dzn"
    assert planning_main([
        str(source), "--mission-id", "m4", "--output", str(manifest),
        "--model", str(model), "--data", str(data),
    ]) == 0
    capsys.readouterr()
    repository = Path(__file__).parents[1]
    executable = repository / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc"
    result = subprocess.run(
        [str(executable), "--json-stream", "--solver", "coin-bc", str(model), str(data)],
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert any(
        json.loads(line).get("status") == "OPTIMAL_SOLUTION"
        for line in result.stdout.splitlines()
    )
    plan = tmp_path / "plan.jsonl"
    plan.write_text(result.stdout)
    chart = tmp_path / "statechart.json"
    helper = repository / "conf/skills/hyper/creating-statechart-files/examples/adaptive-mission/prepare_statechart.py"
    generated = subprocess.run(
        [str(helper), "mission4", str(plan), str(manifest), str(chart)],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert generated.returncode == 0, generated.stderr
    assert json.loads(chart.read_text())["entry_state"] == "search-action"


def test_worker_dry_run_has_no_writes(tmp_path,capsys):
    env=tmp_path / "environment.json";env.write_text(json.dumps(environment(section())))
    assert main(["--mission-id","m4","--session",str(tmp_path / "session.json"),
                 "--request-directory",str(tmp_path / "requests"),"--environment",str(env),
                 "--text","find red container","--dry-run"])==0
    assert not (tmp_path / "session.json").exists()
    assert not (tmp_path / "requests").exists()
    assert '"operation": "add"' in capsys.readouterr().out


def test_worker_script_uses_mission_time_receipts_and_resume(tmp_path):
    state=section()
    current={"now":0.0}
    agent_report={"event":None}

    class PublicTransport:
        def latest_event(self,topic,mission_id,event_kind=None):
            if topic=="mission4-agent-reports":return agent_report["event"]
            assert (topic,mission_id,event_kind)==("environment-data","m4","environment_data")
            return SimpleNamespace(payload=environment(state,current["now"]))

    script=tmp_path / "script.json"
    script.write_text(json.dumps([
        {"at_s":0,"text":"find red container"},
        {"at_s":5,"text":"also find a blue truck"},
    ]))
    errors=[]

    def run():
        try:
            play_request_script(mission_id="m4",session_path=tmp_path / "session.json",
                request_directory=tmp_path / "requests",transport_root=tmp_path / "transport",
                script_path=script,ready_path=tmp_path / "ready.json",poll_seconds=.001,
                timeout_seconds=2,transport=PublicTransport())
        except Exception as exc:  # noqa: BLE001 - propagate worker failure to test thread.
            errors.append(exc)

    worker=Thread(target=run)
    worker.start()
    deadline=time.monotonic()+1
    first=tmp_path / "requests/00000001.json"
    while not first.exists() and time.monotonic()<deadline:time.sleep(.001)
    accept(state,json.loads(first.read_text())["request"])
    while not (tmp_path / "ready.json").exists() and time.monotonic()<deadline:time.sleep(.001)
    assert not (tmp_path / "requests/00000002.json").exists()
    current["now"]=5
    second=tmp_path / "requests/00000002.json"
    while not second.exists() and time.monotonic()<deadline:time.sleep(.001)
    accept(state,json.loads(second.read_text())["request"],5)
    agent_report["event"]=SimpleNamespace(event_id="mission4-agent-report:m4:2:all_found",
        payload={"reason":"all_found","report":{"targets":[
            {"target_id":"worker:1","status":"found"},
            {"target_id":"worker:2","status":"found"}]}})
    third=tmp_path / "requests/00000003.json"
    while not third.exists() and time.monotonic()<deadline:time.sleep(.001)
    accept(state,json.loads(third.read_text())["request"],5)
    worker.join(1)
    assert not worker.is_alive() and not errors
    saved=json.loads((tmp_path / "session.json").read_text())
    assert saved["next_script_request"]==2
    assert sum(item.get("kind")=="accepted" for item in saved["history"])==2

    changed=tmp_path / "changed.json"
    changed.write_text(json.dumps([{"at_s":0,"text":"find a blue truck"}]))
    with pytest.raises(ValueError,match="changed across resume"):
        play_request_script(mission_id="m4",session_path=tmp_path / "session.json",
            request_directory=tmp_path / "requests",transport_root=tmp_path / "transport",
            script_path=changed,transport=PublicTransport())


def test_context_coordination_publishes_idempotent_mission4_agent_report():
    transport=InProcessTransport()
    coordination=SimpleNamespace(_transport=transport)
    report={"request_revision":2,"targets":[{"target_id":"worker:1","status":"found"}]}
    first=ContextCoordination._publish_mission4_report(coordination,"m4","all_found",report)
    second=ContextCoordination._publish_mission4_report(coordination,"m4","all_found",report)
    assert first==second
    assert first.event_kind=="mission4-agent-report"
    assert first.payload["report"]["request_revision"]==2
    assert first.payload["report"]["targets"][0]["status"]=="found"


def test_static_demo_texts_parse_to_objectives():
    state=static_section()
    parsed=[_parse_static_task(text) for text in (DEMO_RESCUE_1,DEMO_RESCUE_2,DEMO_ANIMAL_3)]
    assert [item["kind"] for item in parsed]==["rescue","rescue","animal"]
    assert parsed[0]["group_position"]==[-477.6,-111.86]
    assert parsed[0]["hint_direction"]==[-0.62,-0.79]
    assert parsed[0]["asks"]=={"location"}
    assert parsed[1]["group_position"]==[-44.1,-121.36]
    assert parsed[1]["hint_direction"]==[-0.93,0.38]
    assert parsed[2]["intent"]=="escape"
    assert parsed[2]["species"] is None
    assert parsed[2]["asks"]=={"direction","animal_type","location"}
    objectives=[]
    for index,text in enumerate((DEMO_RESCUE_1,DEMO_RESCUE_2,DEMO_ANIMAL_3),start=1):
        result=interpret_worker_text(text,state,f"worker:{index}")
        assert result["kind"]=="request" and result["request"]["operation"]=="add"
        objectives.append(result["request"]["objective"])
    first,second,third=objectives
    assert first=={"target_id":"worker:1","description":DEMO_RESCUE_1,
                   "attributes":{"entity":"person_in_distress"},"area_ids":["task:1"]}
    assert second["attributes"]=={"entity":"person_in_distress"} and second["area_ids"]==["task:2"]
    assert third["attributes"]=={"entity":"animal"} and third["area_ids"]==["task:3"]


def test_static_phrasing_variants():
    state=static_section()
    named=interpret_worker_text("A group of 2 rangers are standing at (-44.10, -121.36). They need to capture a "
                                "bear. What direction should they take, and what type of animal is it?",
                                state,"worker:1")
    assert named["request"]["objective"]["attributes"]=={"entity":"animal","species":"bear"}
    assert named["request"]["objective"]["area_ids"]==["task:2"]
    assert _parse_static_task("A group of 2 rangers are standing at (-44.10, -121.36). They need to hunt "
                              "a boar. What direction should they take?")["intent"]=="follow"
    rescue_direction=("A group of 3 hikers are waiting at (-477.60, -111.86). They found a person in need of aid. "
                      "Where is the person in need of assistance located, and what direction should they go to "
                      "rescue the person?")
    parsed=_parse_static_task(rescue_direction)
    assert parsed["asks"]=={"location","direction"}
    assert parsed["hint_direction"] is None
    result=interpret_worker_text(rescue_direction,state,"worker:2")
    assert result["request"]["objective"]["attributes"]=={"entity":"person_in_distress"}
    no_hint=_parse_static_task("A party of 2 guards is on duty at (-263.60, -394.36). They heard a person in "
                               "need of help. Where is the person in need of assistance located?")
    assert no_hint["asks"]=={"location"} and no_hint["hint_direction"] is None


def test_static_texts_clarify_when_contradictory_or_unsupported():
    state=static_section()
    contradictory=("A group at (-477.60, -111.86) saw a bear and a person in need of help. What should they do?")
    assert interpret_worker_text(contradictory,state,"worker:1")["kind"]=="clarification"
    no_intent=("An assembly of 2 volunteers are located at (-263.60, -394.36). They were on patrol for an animal. "
               "Where is the animal located?")
    assert interpret_worker_text(no_intent,state,"worker:1")["kind"]=="clarification"
    both_intents=("A group at (-263.60, -394.36) wants to escape and follow the animal. Where is the animal located?")
    assert interpret_worker_text(both_intents,state,"worker:1")["kind"]=="clarification"
    outside=("A party of 4 mechanics are on watchout at (10.00, 10.00). They heard a person in need of help. "
             "Where is the person in need of assistance located?")
    assert interpret_worker_text(outside,state,"worker:1")["kind"]=="clarification"
    old_package=section()
    assert interpret_worker_text(DEMO_ANIMAL_3,old_package,"worker:1")["kind"]=="clarification"
    assert interpret_worker_text("hello there",state,"worker:1")["kind"]=="clarification"


def test_animal_found_requires_species_posterior_peak():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:1")["request"])
    state["observations"]=[animal_observation(0,[-260.0,-390.0,0])]
    planner=Mission4AdaptivePlanner("m4")
    decision=planner.decide(environment(state,1))
    assert decision.action=="investigate"
    assert decision.reason=="additional_useful_view"
    assert decision.parameters=={"track_id":"seen:1","target":{"x":-260.0,"y":-390.0,"z":-20.0},
                                 "standoff_m":10.0,"speed":8.0,"deadline_time":900}
    assert decision.target_ids==("worker:1",)
    resolved=static_section()
    accept(resolved,interpret_worker_text(DEMO_ANIMAL_3,resolved,"worker:1")["request"])
    resolved["observations"]=[animal_observation(0,[-260.0,-390.0,0]),
                              animal_observation(1,[-260.0,-390.0,0])]
    report=Mission4AdaptivePlanner("m4").decide(environment(resolved,2))
    assert report.action=="report" and report.reason=="all_found"


def test_planner_investigates_precise_track_once_then_uses_views():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:1")["request"])
    state["observations"]=[animal_observation(0,[-260.0,-390.0,0])]
    planner=Mission4AdaptivePlanner("m4")
    assert planner.decide(environment(state,1)).action=="investigate"
    active={"command_id":"c1","lifecycle":"active","action":"investigate"}
    assert planner.decide(environment(state,2,active)) is None
    saved=planner.state()
    restored=Mission4AdaptivePlanner("m4",state=saved)
    assert restored.decide(environment(state,2,active)) is None
    completed={"command_id":"c1","lifecycle":"completed","action":"investigate"}
    followup=restored.decide(environment(state,3,completed))
    assert followup is not None and followup.action!="investigate"
    assert "seen:1" in restored.data["investigated_tracks"]


def test_final_report_answers_for_static_and_old_style_objectives():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:1")["request"])
    state["observations"]=[animal_observation(0,[-260.0,-390.0,0]),
                           animal_observation(1,[-260.0,-390.0,0])]
    report=Mission4AdaptivePlanner("m4").decide(environment(state,900))
    answers=report.report["targets"][0]["answers"]
    assert answers["location"] is None
    assert answers["animal_location"]==[-260.0,-390.0]
    assert answers["animal_type"]=="bear"
    dn,de=-263.6-(-260.0),-394.36-(-390.0)
    norm=math.hypot(dn,de)
    assert answers["direction"]==pytest.approx([dn/norm,de/norm])

    follow_state=static_section()
    follow_text=("A group of 2 volunteers are standing at (-263.60, -394.36). They want to follow the animal. "
                 "What direction should they head, and where is the animal located?")
    accept(follow_state,interpret_worker_text(follow_text,follow_state,"worker:1")["request"])
    follow_state["observations"]=[animal_observation(0,[-260.0,-390.0,0]),
                                  animal_observation(1,[-260.0,-390.0,0])]
    follow_report=Mission4AdaptivePlanner("m4").decide(environment(follow_state,900))
    follow_answers=follow_report.report["targets"][0]["answers"]
    assert follow_answers["animal_type"] is None
    assert follow_answers["animal_location"]==[-260.0,-390.0]
    assert follow_answers["direction"]==pytest.approx([-dn/norm,-de/norm])

    rescue_state=static_section()
    rescue_text=("A group of 3 hikers are waiting at (-477.60, -111.86). They found a person in need of aid. "
                 "Where is the person in need of assistance located, and what direction should they go to "
                 "rescue the person?")
    accept(rescue_state,interpret_worker_text(rescue_text,rescue_state,"worker:1")["request"])
    rescue_state["observations"]=[static_observation(0,[-470.0,-105.0,0],
        {"entity":{"value":"person_in_distress","uncertainty":0.01}})]
    rescue_report=Mission4AdaptivePlanner("m4").decide(environment(rescue_state,900))
    rescue_answers=rescue_report.report["targets"][0]["answers"]
    assert rescue_answers["location"]==[-470.0,-105.0]
    assert rescue_answers["animal_type"] is None
    rdn,rde=-470.0-(-477.6),-105.0-(-111.86)
    rnorm=math.hypot(rdn,rde)
    assert rescue_answers["direction"]==pytest.approx([rdn/rnorm,rde/rnorm])

    no_direction=static_section()
    accept(no_direction,interpret_worker_text(DEMO_RESCUE_1,no_direction,"worker:1")["request"])
    no_direction["observations"]=[static_observation(0,[-470.0,-105.0,0],
        {"entity":{"value":"person_in_distress","uncertainty":0.01}})]
    no_direction_report=Mission4AdaptivePlanner("m4").decide(environment(no_direction,900))
    no_direction_answers=no_direction_report.report["targets"][0]["answers"]
    assert no_direction_answers["location"]==[-470.0,-105.0]
    assert no_direction_answers["direction"] is None and no_direction_answers["animal_type"] is None

    old_state=section();old_state["objectives"]={"wanted":copy.deepcopy(OBJECTIVE)};old_state["revision"]=1
    old_state["observations"]=[observation(0)]
    old_report=Mission4AdaptivePlanner("m4").decide(environment(old_state,300))
    assert old_report.report["targets"][0]["answers"]=={"location":None,"animal_type":None,"direction":None}


def test_hint_directed_view_comes_first_and_uses_speed_constant():
    state=static_section()
    for index,text in enumerate((DEMO_RESCUE_1,DEMO_RESCUE_2,DEMO_ANIMAL_3),start=1):
        accept(state,interpret_worker_text(text,state,f"worker:{index}")["request"])
    planner=Mission4AdaptivePlanner("m4")
    first=planner.decide(environment(state,1))
    assert first.action=="navigate" and first.reason=="additional_useful_view"
    assert first.target_ids==("worker:2",)
    assert (first.parameters["x"],first.parameters["y"],first.parameters["z"])==(
        -44.1+75.0*-0.93,-121.36+75.0*0.38,-20.0)
    assert first.parameters["arrival_direction"]==1
    assert first.parameters["speed"]==8.0
    assert first.parameters["deadline_time"]==900
    completed={"command_id":"c1","lifecycle":"completed","action":"navigate"}
    second=planner.decide(environment(state,2,completed))
    assert second.action=="navigate"
    assert (second.parameters["x"],second.parameters["y"])==(-477.6+75.0*-0.62,-111.86+75.0*-0.79)
    assert set(planner.data["hinted_views"])=={"worker:1","worker:2"}
    next_completed={"command_id":"c2","lifecycle":"completed","action":"navigate"}
    third=planner.decide(environment(state,3,next_completed))
    assert third.action=="search_area"
    assert third.parameters["speed"]==8.0


def test_sweep_still_selected_without_hints():
    state=static_section()
    no_hint=("A party of 2 guards is on duty at (-263.60, -394.36). They heard a person in need of help. "
             "Where is the person in need of assistance located?")
    accept(state,interpret_worker_text(no_hint,state,"worker:1")["request"])
    accept(state,interpret_worker_text(DEMO_ANIMAL_3,state,"worker:2")["request"])
    decision=Mission4AdaptivePlanner("m4").decide(environment(state,1))
    assert decision.action=="search_area"
    assert decision.parameters["speed"]==8.0


def test_hint_view_respects_deadline_feasibility_guard():
    state=static_section()
    accept(state,interpret_worker_text(DEMO_RESCUE_2,state,"worker:1")["request"])
    planner=Mission4AdaptivePlanner("m4")
    decision=planner.decide(environment(state,895))
    assert decision.action=="search_area"
    assert planner.data["hinted_views"]==[]
