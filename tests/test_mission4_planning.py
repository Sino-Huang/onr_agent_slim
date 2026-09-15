import copy
import json

from test_object_search_belief import PACKAGE, OBJECTIVE, observation
from onr.application.mission4_planning import interpret_worker_text, Mission4AdaptivePlanner, Mission4ReplanGate
from onr.adapters.mission4_worker import Mission4WorkerSession, main


def section():
    return {"schema_version":1,"package":copy.deepcopy(PACKAGE),"revision":0,"status":"active","reason":None,
            "deadline_s":300,"objectives":{},"observations":[],"coverage":{},"requests":[]}


def accept(state,request,now=0):
    # Request receipt fixture. Actual runtime acceptance is tested in #33.
    assert request["base_revision"]==state["revision"]
    op=request["operation"]
    if op in {"add","amend"}:state["objectives"][request["objective"]["target_id"]]=request["objective"]
    if op=="remove":del state["objectives"][request["target_id"]]
    if op=="deadline":state["deadline_s"]=request["deadline_s"]
    if op=="cancel":state.update(status="cancelled",reason="worker_cancelled")
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
    assert gate.assess({"world_model_info":{"mission_mode":"mission1"}}) is None


def test_worker_dry_run_has_no_writes(tmp_path,capsys):
    env=tmp_path / "environment.json";env.write_text(json.dumps(environment(section())))
    assert main(["--mission-id","m4","--session",str(tmp_path / "session.json"),
                 "--request-directory",str(tmp_path / "requests"),"--environment",str(env),
                 "--text","find red container","--dry-run"])==0
    assert not (tmp_path / "session.json").exists()
    assert not (tmp_path / "requests").exists()
    assert '"operation": "add"' in capsys.readouterr().out
