"""Worker request intake and public-evidence Mission 4 search policy."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from collections.abc import Mapping

from onr.application.bayesian_belief import BayesianBeliefManager
from onr.application.object_search_belief import plain, location_supported


@dataclass(frozen=True)
class Mission4Decision:
    action: str
    reason: str
    parameters: dict = field(default_factory=dict)
    target_ids: tuple[str, ...] = ()
    report: dict | None = None

    def to_dict(self):
        return asdict(self)


def interpret_worker_text(text: str, section: Mapping, request_id: str) -> dict:
    """Interpret supported search language; ask for clarification on ambiguity.

    Interpretation is a proposal. Only the runtime receipt accepts a revision.
    """
    section=plain(section)
    original=text.strip()
    text=original.lower().strip().rstrip(".!?")
    if text in {"status","progress","show progress","what have you found","evidence","show evidence"}:
        return {"kind":"query","query":"evidence" if "evidence" in text else "progress"}
    if section["status"]!="active":
        return {"kind":"new_run_required","message":"This search has ended; start a new mission run."}
    req={"request_id":request_id,"base_revision":section["revision"]}
    if text in {"cancel","cancel search","stop searching"}:
        return {"kind":"request","request":{**req,"operation":"cancel"}}
    removal=re.fullmatch(r"(?:remove|cancel target) (\S+)",text)
    if removal:
        target=removal[1]
        if target not in section["objectives"]:
            return {"kind":"clarification","message":"Name an active target ID to remove."}
        return {"kind":"request","request":{**req,"operation":"remove","target_id":target}}
    deadline=re.fullmatch(r"(?:deadline|set deadline to|extend deadline to) ([0-9]+(?:\.[0-9]+)?)(?: seconds)?",text)
    if deadline:
        return {"kind":"request","request":{**req,"operation":"deadline","deadline_s":float(deadline[1])}}
    amended=re.fullmatch(r"(?:change|amend) (\S+) to (.+)",text)
    operation="amend" if amended else "add"
    if amended:
        target=amended[1]; description=amended[2]
        if target not in section["objectives"]:
            return {"kind":"clarification","message":"Name an active target ID to amend."}
    else:
        found=re.fullmatch(r"(?:please )?(?:find|locate|look for|also find) (.+)",text)
        if not found:
            return {"kind":"clarification","message":"Describe a target to find, or request progress, evidence, a deadline change or cancellation."}
        target=request_id;description=found[1]
    parts=re.split(r"\s+in\s+",description,maxsplit=1)
    area_ids=list(section["package"]["areas"]) if len(parts)==1 else [a.strip() for a in re.split(r"\s+and\s+|,",parts[1])]
    if not area_ids or any(a not in section["package"]["areas"] for a in area_ids):
        return {"kind":"clarification","message":"Choose configured search area names."}
    remaining=parts[0]
    attributes={}
    for attr,values in section["package"]["vocabulary"].items():
        matches=[v for v in values if re.search(r"\b"+re.escape(v.lower())+r"\b",remaining)]
        if len(matches)>1:
            return {"kind":"clarification","message":f"Specify one {attr} value per target."}
        if matches:
            attributes[attr]=matches[0]
            remaining=re.sub(r"\b"+re.escape(matches[0].lower())+r"\b"," ",remaining)
    remaining=re.sub(r"\b(?:a|an|the|object|with|and|colored|coloured)\b"," ",remaining)
    if remaining.strip() or not attributes:
        return {"kind":"clarification","message":"Use declared category/attribute values; unsupported or negative descriptions need clarification."}
    objective={"target_id":target,"description":description,"attributes":attributes,"area_ids":area_ids}
    return {"kind":"request","request":{**req,"operation":operation,"objective":objective}}


class Mission4AdaptivePlanner:
    def __init__(self, mission_id="mission4", *, state=None):
        self.mission_id=mission_id
        self.beliefs=None
        self.data={"visited_views":[],"completed_areas":[],"handled_commands":[],"active_choice":None,
                   "last_signature":None,"belief":None}
        if state is not None:
            self.data=plain(state)

    def state(self):
        result=plain(self.data)
        result["belief"]=None if self.beliefs is None else self.beliefs.state()
        return result

    def decide(self, environment: Mapping) -> Mission4Decision | None:
        world=environment.get("world_model_info",{})
        if world.get("mission_mode")!="mission4":
            return None
        section=plain(world["mission4"])
        now=float(environment["mission_time_seconds"])
        if self.beliefs is None:
            self.beliefs=BayesianBeliefManager.for_object_search(self.mission_id,section["package"],self.data["belief"])
        snapshot=self.beliefs.ingest(section,now)
        lifecycle=plain(environment.get("maneuver_lifecycle"))
        signature=[section["revision"],snapshot.belief_revision,section["status"],lifecycle]
        if signature==self.data["last_signature"]:
            return None
        self.data["last_signature"]=signature
        active=lifecycle is not None and lifecycle.get("lifecycle") in {"accepted","active"}
        choice=self.data["active_choice"]
        if lifecycle and lifecycle.get("lifecycle") in {"completed","failed","cancelled"}:
            cid=lifecycle["command_id"]
            if cid not in self.data["handled_commands"]:
                self.data["handled_commands"].append(cid)
                if choice and choice["kind"]=="area" and lifecycle["lifecycle"]!="cancelled":
                    self.data["completed_areas"].append(choice["id"])
        found={m.target_id for m in snapshot.matches if m.found}
        unresolved=[tid for tid in section["objectives"] if tid not in found]
        if section["status"]!="active" or now>=section["deadline_s"]:
            return self.final_report(section,section.get("reason") or "mission_deadline")
        if section["objectives"] and not unresolved:
            return self.final_report(section,"all_found")
        if not unresolved:
            return None
        # Continue a still-useful leg even if a second objective was added.
        if active and choice and any(t in unresolved and section["objectives"][t]==choice["objectives"].get(t) for t in choice["targets"]):
            allowed={a for t in unresolved for a in section["objectives"][t]["area_ids"]}
            if choice["kind"]!="area" or choice["id"] in allowed:
                # Interrupt area coverage when a candidate warrants another view.
                if choice["kind"]=="view" or not any(m.target_id in unresolved for m in snapshot.matches):
                    return None
        position=environment["controlled_vehicle"]["position"]
        current=[float(position[k]) for k in ("x","y","z")]
        options=[]
        for match in snapshot.matches:
            if match.target_id not in unresolved or match.probability is None:
                continue
            obj=section["objectives"][match.target_id]
            polygons=[section["package"]["areas"][a]["polygon"] for a in obj["area_ids"]]
            if not location_supported(match.position,0,polygons):
                continue
            for index,(dn,de) in enumerate([(10,0),(0,10),(-10,0),(0,-10)]):
                key=[match.track_id,index]
                if key in self.data["visited_views"]:
                    continue
                target=[match.position[0]+dn,match.position[1]+de,current[2]]
                if any(location_supported(target,0,[p]) for p in section["package"]["obstacles"]+section["package"]["keep_out_zones"]):
                    continue
                distance=math.dist(current,target)
                if distance/2.5>=section["deadline_s"]-now:
                    continue
                options.append((match.probability/(1+distance),key,target,match))
        if options:
            _,key,target,match=max(options,key=lambda item:item[0])
            self.data["visited_views"].append(key)
            targets=tuple(sorted({m.target_id for m in snapshot.matches if m.track_id==match.track_id and m.target_id in unresolved}))
            self.data["active_choice"]={"kind":"view","id":key,"targets":list(targets),
                                        "objectives":{t:section["objectives"][t] for t in targets}}
            dn,de=match.position[0]-target[0],match.position[1]-target[1]
            direction=(0 if de>0 else 2) if abs(de)>=abs(dn) else (3 if dn>0 else 1)
            return Mission4Decision("navigate","additional_useful_view",
                {**dict(zip(("x","y","z"),target)),"arrival_direction":direction,"speed":2.5,"deadline_time":section["deadline_s"]},targets)
        areas=[]
        for aid,area in section["package"]["areas"].items():
            targets=tuple(t for t in unresolved if aid in section["objectives"][t]["area_ids"])
            if not targets or aid in self.data["completed_areas"]:
                continue
            center=[sum(p[i] for p in area["polygon"])/len(area["polygon"]) for i in (0,1)]
            coverage=section.get("coverage",{}).get(aid,{})
            count=len(coverage.get("observed_cells",[]))
            score=len(targets)*area["prior"]/(1+math.dist(current[:2],center))/(1+count)
            areas.append((score,aid,targets,area))
        if areas:
            _,aid,targets,area=max(areas,key=lambda item:item[0])
            self.data["active_choice"]={"kind":"area","id":aid,"targets":list(targets),
                                        "objectives":{t:section["objectives"][t] for t in targets}}
            return Mission4Decision("search_area","joint_area_search",
                {"polygon":[{"x":n,"y":e} for n,e in area["polygon"]],"speed":2.5,"deadline_time":section["deadline_s"]},targets)
        return self.final_report(section,"search_exhausted")

    def final_report(self,section,reason):
        snapshot=self.beliefs.snapshot()
        rows=[]
        for target in section["objectives"]:
            matches=[m for m in snapshot.matches if m.target_id==target]
            matches.sort(key=lambda m:(m.found,m.probability or 0),reverse=True)
            best=matches[0] if matches else None
            rows.append({"target_id":target,"status":"cancelled" if section["status"]=="cancelled" else "found" if best and best.found else "incomplete",
                         "reason":None if best and best.found else reason,
                         "match":None if best is None else asdict(best)})
        return Mission4Decision("report",reason,report={"reason":reason,"targets":rows,
            "request_revision":section["revision"],"reports":self.beliefs.state()["reports"]})


class Mission4ReplanGate:
    def __init__(self):
        self.planner=Mission4AdaptivePlanner()
        self.last_decision=None

    def assess(self,environment):
        decision=self.planner.decide(environment)
        if decision is None:
            return None
        self.last_decision=decision
        return f"mission4-gate:{decision.reason}:{decision.action}"


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description="Evaluate the Mission 4 public-evidence search policy")
    parser.add_argument("environment",type=Path)
    parser.add_argument("--mission-id",default="mission4")
    parser.add_argument("--dry-run",action="store_true",help="Validate and print a decision without sending it")
    args=parser.parse_args(argv)
    decision=Mission4AdaptivePlanner(args.mission_id).decide(json.loads(args.environment.read_text()))
    print(json.dumps({"status":"PASS","decision":None if decision is None else decision.to_dict()}),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
