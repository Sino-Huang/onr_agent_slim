import copy
import json
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
    interpret_worker_text,
    main as planning_main,
)


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


def test_active_area_interrupt_never_selects_the_current_viewpoint():
    state=section();state["objectives"]={"wanted":copy.deepcopy(OBJECTIVE)};state["revision"]=1
    state["observations"]=[observation(0)]
    active={"command_id":"c1","lifecycle":"active","action":"search_area"}
    env=environment(state,2,active)
    env["controlled_vehicle"]["position"]={"x":-4,"y":0,"z":-20}
    decision=Mission4AdaptivePlanner("m4").decide(env)
    assert decision.action=="navigate"
    assert (decision.parameters["x"],decision.parameters["y"])!=(-4,0)


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
