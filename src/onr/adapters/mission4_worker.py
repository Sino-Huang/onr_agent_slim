"""Durable worker-text requests over the runtime search revision boundary."""
import json
import os
import tempfile
import time
from pathlib import Path

from onr.adapters.file_transport import FileTransport
from onr.application.mission4_planning import interpret_worker_text
from onr.application.object_search_belief import plain


def atomic_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as stream:
        json.dump(value,stream,allow_nan=False)
        stream.flush();os.fsync(stream.fileno());temporary=stream.name
    os.replace(temporary,path)


class Mission4WorkerSession:
    def __init__(self, mission_id, state_path, request_directory):
        self.mission_id=mission_id
        self.state_path=Path(state_path)
        self.request_directory=Path(request_directory)
        self.data={"mission_id":mission_id,"sequence":0,"queue":[],"pending":None,"history":[]}
        if self.state_path.exists():
            self.data=json.loads(self.state_path.read_text())
            if self.data["mission_id"]!=mission_id:
                raise ValueError("worker checkpoint mission mismatch")

    def enqueue(self,text):
        if not isinstance(text,str) or not text.strip():
            raise ValueError("worker text is required")
        self.data["queue"].append(text)
        atomic_json(self.state_path,self.data)

    def advance(self, section, now):
        section=plain(section)
        pending=self.data["pending"]
        if pending is not None:
            receipt=next((r for r in section["requests"] if r["request"]["request_id"]==pending["request"]["request_id"]),None)
            if receipt is None:
                # Recover a crash after checkpointing intent but before publication.
                self._publish(pending)
                return {"kind":"awaiting_runtime_receipt"}
            self.data["history"].append({"kind":"accepted","receipt":receipt})
            self.data["pending"]=None
            atomic_json(self.state_path,self.data)
        if not self.data["queue"]:
            return None
        self.data["sequence"]+=1
        worker_text=self.data["queue"].pop(0)
        result=interpret_worker_text(worker_text,section,f"worker:{self.data['sequence']}")
        if result["kind"]=="query":
            result["response"]={"revision":section["revision"],"status":section["status"],
                                "deadline_s":section["deadline_s"],"objectives":section["objectives"],
                                "coverage":section.get("coverage",{})}
            if result["query"]=="evidence":
                result["response"]["observations"]=section["observations"]
        if result["kind"]=="request" and result["request"]["operation"]=="deadline" and result["request"]["deadline_s"]<=now:
            result={"kind":"clarification","message":"Choose a deadline later than current mission time."}
        self.data["history"].append({"text":worker_text,"result":result,"at_s":now})
        if result["kind"]=="request":
            envelope={"mission_id":self.mission_id,"request":result["request"]}
            self.data["pending"]=envelope
        atomic_json(self.state_path,self.data)
        if self.data["pending"] is not None:
            self._publish(self.data["pending"])
        return result

    def _publish(self,envelope):
        revision=envelope["request"]["base_revision"]+1
        path=self.request_directory / f"{revision:08d}.json"
        if path.exists():
            if json.loads(path.read_text())!=envelope:
                raise ValueError("search request filename conflict")
        else:
            atomic_json(path,envelope)


def _request_script(path):
    script=json.loads(Path(path).read_text())
    if not isinstance(script,list) or not script:
        raise ValueError("worker request script must be a non-empty array")
    normalized=[]
    for item in script:
        if not isinstance(item,dict) or set(item)!={"at_s","text"}:
            raise ValueError("worker request entries require at_s and text")
        at_s=item["at_s"]
        text=item["text"]
        if isinstance(at_s,bool) or not isinstance(at_s,(int,float)) or at_s<0:
            raise ValueError("worker request time must be non-negative")
        if not isinstance(text,str) or not text.strip():
            raise ValueError("worker request text is required")
        normalized.append({"at_s":float(at_s),"text":text})
    if [item["at_s"] for item in normalized] != sorted(item["at_s"] for item in normalized):
        raise ValueError("worker request script must be ordered by mission time")
    return normalized


def play_request_script(*,mission_id,session_path,request_directory,transport_root,
                        script_path,ready_path=None,poll_seconds=.1,timeout_seconds=600,
                        transport=None):
    """Replay worker requests against public Mission 4 transport evidence."""
    script=_request_script(script_path)
    session=Mission4WorkerSession(mission_id,session_path,request_directory)
    saved_script=session.data.get("script")
    if saved_script is not None and saved_script!=script:
        raise ValueError("worker request script changed across resume")
    session.data.setdefault("script",script)
    session.data.setdefault("next_script_request",0)
    session.data.setdefault("agent_report_event_id",None)
    atomic_json(session.state_path,session.data)
    transport=transport or FileTransport(transport_root)
    deadline=time.monotonic()+float(timeout_seconds)
    accepted_before=sum(item.get("kind")=="accepted" for item in session.data["history"])
    while time.monotonic()<deadline:
        event=transport.latest_event("environment-data",mission_id,event_kind="environment_data")
        if event is None:
            time.sleep(poll_seconds)
            continue
        environment=plain(event.payload)
        section=environment.get("world_model_info",{}).get("mission4")
        if not isinstance(section,dict):
            raise TypeError("public environment has no Mission 4 state")
        now=float(environment["mission_time_seconds"])
        next_request=int(session.data["next_script_request"])
        while next_request<len(script) and script[next_request]["at_s"]<=now:
            session.enqueue(script[next_request]["text"])
            next_request+=1
            session.data["next_script_request"]=next_request
            atomic_json(session.state_path,session.data)
        result=session.advance(section,now)
        accepted=sum(item.get("kind")=="accepted" for item in session.data["history"])
        if ready_path is not None and accepted>0 and not Path(ready_path).exists():
            atomic_json(Path(ready_path),{"mission_id":mission_id,"accepted_requests":accepted})
        if result is not None or accepted>accepted_before:
            print(json.dumps({"mission_time_s":now,"next_request":next_request,
                              "accepted_requests":accepted,"result":result}),flush=True)
            accepted_before=accepted
        report_event=transport.latest_event("mission4-agent-reports",mission_id,
                                            event_kind="mission4-agent-report")
        if (report_event is not None and section["status"]=="active"
                and report_event.event_id!=session.data["agent_report_event_id"]):
            reason=report_event.payload["reason"]
            report=plain(report_event.payload["report"])
            if reason=="all_found" and (not report.get("targets") or
                    any(item.get("status")!="found" for item in report["targets"])):
                raise ValueError("all_found Agent report has unresolved targets")
            if reason in {"all_found","search_exhausted","execution_failed"}:
                request={"request_id":report_event.event_id,"base_revision":section["revision"],
                         "operation":"finish","reason":reason}
                envelope={"mission_id":mission_id,"request":request}
                path=Path(request_directory) / f"{section['revision']+1:08d}.json"
                if path.exists() and json.loads(path.read_text())!=envelope:
                    raise ValueError("Agent finish request filename conflict")
                if not path.exists():atomic_json(path,envelope)
                session.data["agent_report_event_id"]=report_event.event_id
                session.data["agent_report"]=report
                atomic_json(session.state_path,session.data)
                print(json.dumps({"mission_time_s":now,"agent_finish":reason,
                                  "report_event_id":report_event.event_id}),flush=True)
        if (next_request==len(script) and not session.data["queue"]
                and session.data["pending"] is None and section["status"]!="active"):
            return session.data
        time.sleep(poll_seconds)
    raise TimeoutError("worker request playback timed out")


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description="Queue one worker Mission 4 request without resetting an active search")
    parser.add_argument("--mission-id",required=True)
    parser.add_argument("--session",required=True)
    parser.add_argument("--request-directory",required=True)
    parser.add_argument("--environment",help="Current public environment JSON")
    parser.add_argument("--text")
    parser.add_argument("--transport-root")
    parser.add_argument("--script")
    parser.add_argument("--ready-file")
    parser.add_argument("--poll-seconds",type=float,default=.1)
    parser.add_argument("--timeout-seconds",type=float,default=600)
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args(argv)
    scripted=args.script is not None or args.transport_root is not None
    if scripted:
        if args.script is None or args.transport_root is None or args.environment is not None or args.text is not None:
            parser.error("script playback requires --script and --transport-root only")
        if args.dry_run:
            result={"kind":"script","requests":len(_request_script(args.script))}
        else:
            result=play_request_script(mission_id=args.mission_id,session_path=args.session,
                request_directory=args.request_directory,transport_root=args.transport_root,
                script_path=args.script,ready_path=args.ready_file,poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds)
        print(json.dumps(result),flush=True)
        return 0
    if args.environment is None or args.text is None:
        parser.error("one-shot requests require --environment and --text")
    env=json.loads(Path(args.environment).read_text())
    section=env["world_model_info"]["mission4"]
    if args.dry_run:
        result=interpret_worker_text(args.text,section,"dry-run")
    else:
        session=Mission4WorkerSession(args.mission_id,args.session,args.request_directory)
        session.enqueue(args.text)
        result=session.advance(section,float(env["mission_time_seconds"]))
    print(json.dumps(result),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
