"""Worker request intake and public-evidence Mission 4 search policy."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from onr.application.bayesian_belief import BayesianBeliefManager
from onr.application.object_search_belief import location_supported, plain

_SELECTION_MODEL = """int: selected_index;
constraint selected_index >= 0 /\\ selected_index <= 1;
solve maximize selected_index;
output ["{\\\"selected_index\\\":", show(selected_index), "}"];
"""

_STATIC_SPECIES=("bear","boar","deer","deerdoe","deerstag","fox","kangaroo","koala","pig","wolf")
_STATIC_ESCAPE=("run away","escape","evade","retreat","get away","flee")
_STATIC_FOLLOW=("follow","capture","catch","hunt","sneak","track")
_STATIC_COORD=r"\((-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)"
_STATIC_RESCUE_PERSON=r"(?:\bperson\b|\bsomeone\b|\bindividual\b)"
_STATIC_RESCUE_NEED=r"(?:\bassist|\bhelp|\baid\b|\bsupport|\bsaving\b|\brescue|\bdistress)"
_STATIC_ANIMAL=r"\banimals?\b"
_STATIC_DIRECTION_ASK=r"(?:what|which) direction"
_STATIC_TYPE_ASK=r"what (?:type|kind) of animal"
_STATIC_WHERE_ASK=r"where is"
_MISSION4_SPEED_MPS=8.0
_HINT_VIEW_DISTANCE_M=75.0


def _parse_static_task(text):
    """Parse a static ground-team assistance task; None when the text is not one."""
    normalized=text.strip().lower().rstrip(".!?")
    pairs=[(float(north),float(east)) for north,east in re.findall(_STATIC_COORD,normalized)]
    if not pairs:
        return None
    species=[name for name in _STATIC_SPECIES if re.search(r"\b"+name+r"\b",normalized)]
    rescue=bool(re.search(_STATIC_RESCUE_PERSON,normalized)) and bool(re.search(_STATIC_RESCUE_NEED,normalized))
    animal=bool(re.search(_STATIC_ANIMAL,normalized)) or bool(species)
    if rescue==animal:
        return None
    hint=None
    hinted=re.search(r"direction of\s*"+_STATIC_COORD,normalized)
    if hinted:
        hint=[float(hinted[1]),float(hinted[2])]
    mentioned={ask for ask,pattern in (("direction",_STATIC_DIRECTION_ASK),("animal_type",_STATIC_TYPE_ASK),("location",_STATIC_WHERE_ASK))
               if re.search(pattern,normalized)}
    if rescue:
        return {"kind":"rescue","group_position":list(pairs[0]),"hint_direction":hint,"intent":None,"species":None,
                "asks":{"location"}|({"direction"} if "direction" in mentioned else set())}
    escape=bool(re.search(r"\b(?:"+"|".join(_STATIC_ESCAPE)+r")\b",normalized))
    follow=bool(re.search(r"\b(?:"+"|".join(_STATIC_FOLLOW)+r")\b",normalized))
    if escape==follow or len(species)>1:
        return None
    return {"kind":"animal","group_position":list(pairs[0]),"hint_direction":hint,
            "intent":"escape" if escape else "follow","species":species[0] if species else None,
            "asks":{"direction"}|({"animal_type"} if "animal_type" in mentioned else set())
                   |({"location"} if "location" in mentioned else set())}


@dataclass(frozen=True)
class Mission4Decision:
    action: str
    reason: str
    parameters: dict = field(default_factory=dict)
    target_ids: tuple[str, ...] = ()
    report: dict | None = None

    def to_dict(self):
        return asdict(self)


_GATE_TRIGGER_PREFIX="mission4-gate:"


def decision_from_trigger(trigger):
    """Rebuild the adaptive decision embedded in one gate trigger identity."""
    index=trigger.find(_GATE_TRIGGER_PREFIX)
    if index<0:
        return None
    payload=trigger[index+len(_GATE_TRIGGER_PREFIX):]
    while True:
        try:
            raw=json.loads(payload)
            break
        except json.JSONDecodeError:
            cut=payload.rfind(";")
            if cut<0:
                raise ValueError("mission4 gate trigger decision payload is invalid")
            payload=payload[:cut]
    if not isinstance(raw,dict):
        raise TypeError("mission4 gate trigger decision payload must be an object")
    try:
        return Mission4Decision(str(raw["action"]),str(raw["reason"]),
                                dict(raw.get("parameters") or {}),
                                tuple(str(item) for item in raw.get("targets") or ()),
                                raw.get("report"))
    except (KeyError,TypeError) as exc:
        raise ValueError("mission4 gate trigger decision payload is invalid") from exc


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
        static=_parse_static_task(text)
        if static is not None:
            attributes={"entity":"person_in_distress"} if static["kind"]=="rescue" else {"entity":"animal"}
            if static["species"]:
                attributes["species"]=static["species"]
            if any(value not in section["package"]["vocabulary"].get(attr,()) for attr,value in attributes.items()):
                return {"kind":"clarification","message":"Use declared category/attribute values; unsupported or negative descriptions need clarification."}
            area_ids=[aid for aid,area in section["package"]["areas"].items()
                      if location_supported([*static["group_position"],0.0],0.0,[area["polygon"]])]
            if not area_ids:
                return {"kind":"clarification","message":"Choose configured search area names."}
            objective={"target_id":request_id,"description":original,"attributes":attributes,"area_ids":area_ids}
            return {"kind":"request","request":{**req,"operation":"add","objective":objective}}
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
                   "last_signature":None,"belief":None,"investigated_tracks":[],"hinted_views":[]}
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
        found=self._resolved_targets(snapshot,section)
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
                # Interrupt area coverage only for a candidate that is actionable now.
                actionable=False
                for match in snapshot.matches:
                    if match.target_id not in unresolved:
                        continue
                    polygons=[section["package"]["areas"][a]["polygon"]
                              for a in section["objectives"][match.target_id]["area_ids"]]
                    if not location_supported(match.position,0,polygons):
                        continue
                    if match.probability is not None or (
                            match.position_uncertainty_m is not None
                            and match.position_uncertainty_m<=2.0
                            and match.track_id not in self.data["investigated_tracks"]):
                        actionable=True
                        break
                if choice["kind"]=="view" or not actionable:
                    return None
        position=environment["controlled_vehicle"]["position"]
        current=[float(position[k]) for k in ("x","y","z")]
        hinted=[]
        for target in unresolved:
            parsed=_parse_static_task(section["objectives"][target]["description"])
            if not parsed or parsed["hint_direction"] is None or target in self.data["hinted_views"]:
                continue
            hint=parsed["hint_direction"]
            goal=[parsed["group_position"][0]+_HINT_VIEW_DISTANCE_M*hint[0],
                  parsed["group_position"][1]+_HINT_VIEW_DISTANCE_M*hint[1],current[2]]
            distance=math.dist(current[:2],goal[:2])
            if distance/_MISSION4_SPEED_MPS>=section["deadline_s"]-now:
                continue
            hinted.append((distance,target,goal,hint))
        if hinted:
            _,target,goal,hint=min(hinted,key=lambda item:item[0])
            self.data["hinted_views"].append(target)
            self.data["active_choice"]={"kind":"view","id":target,"targets":[target],
                                        "objectives":{target:section["objectives"][target]}}
            dn,de=hint
            direction=(0 if de>0 else 2) if abs(de)>=abs(dn) else (3 if dn>0 else 1)
            return Mission4Decision("navigate","additional_useful_view",
                {**dict(zip(("x","y","z"),goal)),"arrival_direction":direction,
                 "speed":_MISSION4_SPEED_MPS,"deadline_time":section["deadline_s"]},(target,))
        for target in unresolved:
            if _parse_static_task(section["objectives"][target]["description"]) is None:
                continue
            matches=[m for m in snapshot.matches if m.target_id==target]
            matches.sort(key=lambda m:(m.found,m.probability or 0),reverse=True)
            for match in matches:
                if (match.position_uncertainty_m is None or match.position_uncertainty_m>2.0
                        or match.track_id in self.data["investigated_tracks"]):
                    continue
                polygons=[section["package"]["areas"][a]["polygon"] for a in section["objectives"][target]["area_ids"]]
                if not location_supported(match.position,0,polygons):
                    continue
                if math.dist(current[:2],match.position[:2])/_MISSION4_SPEED_MPS>=section["deadline_s"]-now:
                    continue
                self.data["investigated_tracks"].append(match.track_id)
                targets=tuple(sorted({m.target_id for m in snapshot.matches if m.track_id==match.track_id and m.target_id in unresolved}))
                self.data["active_choice"]={"kind":"view","id":match.track_id,"targets":list(targets),
                                            "objectives":{t:section["objectives"][t] for t in targets}}
                return Mission4Decision("investigate","additional_useful_view",
                    {"track_id":match.track_id,"target":{"x":match.position[0],"y":match.position[1],"z":current[2]},
                     "standoff_m":10.0,"speed":_MISSION4_SPEED_MPS,"deadline_time":section["deadline_s"]},targets)
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
                if distance < 1.0:
                    continue
                if distance/_MISSION4_SPEED_MPS>=section["deadline_s"]-now:
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
                {**dict(zip(("x","y","z"),target)),"arrival_direction":direction,"speed":_MISSION4_SPEED_MPS,"deadline_time":section["deadline_s"]},targets)
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
            if active and choice and choice["kind"]=="area" and choice["id"]==aid:
                return None
            self.data["active_choice"]={"kind":"area","id":aid,"targets":list(targets),
                                        "objectives":{t:section["objectives"][t] for t in targets}}
            return Mission4Decision("search_area","joint_area_search",
                {"polygon":[{"x":n,"y":e} for n,e in area["polygon"]],"speed":_MISSION4_SPEED_MPS,"deadline_time":section["deadline_s"]},targets)
        return self.final_report(section,"search_exhausted")

    def _species_peak(self,track_id):
        species=self.beliefs.data["tracks"].get(track_id,{}).get("attributes",{}).get("species")
        return None if not species else max(species.values())

    def _resolved_targets(self,snapshot,section):
        threshold=section["package"]["found_threshold"]
        resolved=set()
        for match in snapshot.matches:
            if not match.found:
                continue
            objective=section["objectives"][match.target_id]
            parsed=_parse_static_task(objective["description"])
            if (parsed and parsed["kind"]=="animal" and "animal_type" in parsed["asks"]
                    and "species" not in objective["attributes"]):
                peak=self._species_peak(match.track_id)
                if peak is None or peak<=1-threshold:
                    continue
            resolved.add(match.target_id)
        return resolved

    def _answers(self,objective,best):
        answers={"location":None,"animal_type":None,"direction":None}
        parsed=_parse_static_task(objective["description"])
        if parsed is None:
            return answers
        position=None if best is None else [best.position[0],best.position[1]]
        if "location" in parsed["asks"]:
            if parsed["kind"]=="animal":
                answers["animal_location"]=position
            else:
                answers["location"]=position
        if "animal_type" in parsed["asks"] and best is not None:
            species=self.beliefs.data["tracks"].get(best.track_id,{}).get("attributes",{}).get("species")
            if species:
                answers["animal_type"]=max(species,key=species.get)
        if "direction" in parsed["asks"] and position is not None:
            group=parsed["group_position"]
            if parsed["kind"]=="rescue" or parsed["intent"]=="follow":
                dn,de=position[0]-group[0],position[1]-group[1]
            else:
                dn,de=group[0]-position[0],group[1]-position[1]
            norm=math.hypot(dn,de)
            if norm>0:
                answers["direction"]=[dn/norm,de/norm]
        return answers

    def final_report(self,section,reason):
        snapshot=self.beliefs.snapshot()
        rows=[]
        for target in section["objectives"]:
            matches=[m for m in snapshot.matches if m.target_id==target]
            matches.sort(key=lambda m:(m.found,m.probability or 0),reverse=True)
            best=matches[0] if matches else None
            rows.append({"target_id":target,"status":"cancelled" if section["status"]=="cancelled" else "found" if best and best.found else "incomplete",
                         "reason":None if best and best.found else reason,
                         "match":None if best is None else asdict(best),
                         "answers":self._answers(section["objectives"][target],best)})
        return Mission4Decision("report",reason,report={"reason":reason,"targets":rows,
            "request_revision":section["revision"],"reports":self.beliefs.state()["reports"]})


class Mission4ReplanGate:
    def __init__(self):
        self.planner=Mission4AdaptivePlanner()
        self.last_decision=None
        self._last_trigger=None

    def assess(self,environment):
        decision=self.planner.decide(environment)
        if decision is None:
            return None
        self.last_decision=decision
        digest=json.dumps({"action":decision.action,"reason":decision.reason,
                           "parameters":decision.parameters,"targets":decision.target_ids,
                           "report":decision.report},
                          sort_keys=True,default=str)
        trigger=f"{_GATE_TRIGGER_PREFIX}{digest}"
        if trigger==self._last_trigger:
            return None
        self._last_trigger=trigger
        return trigger


def write_minizinc_problem(decision, model_path, data_path):
    """Write the code-owned MiniZinc receipt for one adaptive decision."""
    model_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(_SELECTION_MODEL, encoding="utf-8")
    data_path.write_text(
        f"selected_index = {0 if decision is None else 1};\n", encoding="utf-8"
    )


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description="Evaluate the Mission 4 public-evidence search policy")
    parser.add_argument("environment",type=Path)
    parser.add_argument("--mission-id",default="mission4")
    parser.add_argument("--dry-run",action="store_true",help="Validate and print a decision without sending it")
    parser.add_argument("--output",type=Path)
    parser.add_argument("--model",type=Path)
    parser.add_argument("--data",type=Path)
    args=parser.parse_args(argv)
    decision=Mission4AdaptivePlanner(args.mission_id).decide(json.loads(args.environment.read_text()))
    result={"status":"PASS","mission_time_seconds":json.loads(args.environment.read_text()).get("mission_time_seconds"),
            "decision":None if decision is None else decision.to_dict()}
    if bool(args.model) != bool(args.data):
        parser.error("--model and --data must be supplied together")
    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    if args.model is not None and not args.dry_run:
        write_minizinc_problem(decision,args.model,args.data)
    print(json.dumps(result),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
