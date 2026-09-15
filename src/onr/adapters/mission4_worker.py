"""Durable worker-text requests over the runtime search revision boundary."""
import json
import os
import tempfile
from pathlib import Path

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


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description="Queue one worker Mission 4 request without resetting an active search")
    parser.add_argument("--mission-id",required=True)
    parser.add_argument("--session",required=True)
    parser.add_argument("--request-directory",required=True)
    parser.add_argument("--environment",required=True,help="Current public environment JSON")
    parser.add_argument("--text",required=True)
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args(argv)
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
