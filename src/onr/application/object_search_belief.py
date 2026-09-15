"""Evidence accumulation for stationary description-driven object search."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping

from onr.application.bayesian_belief import BayesianBeliefManager
from onr.contracts.object_search import SearchBeliefSnapshot, SearchMatch


def plain(value):
    if isinstance(value, Mapping):
        return {k:plain(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [plain(v) for v in value]
    return value


def location_supported(position, radius, polygons):
    """Require the producer uncertainty disk inside at least one allowed area."""
    if radius is None or not math.isfinite(radius) or radius < 0 or radius > 5:
        return False
    x,y=position[:2]
    for polygon in polygons:
        inside=False
        distance=math.inf
        for (ax,ay),(bx,by) in zip(polygon,polygon[1:]+polygon[:1]):
            dx,dy=bx-ax,by-ay
            t=max(0,min(1,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy)))
            distance=min(distance,math.hypot(x-ax-t*dx,y-ay-t*dy))
            if (ay>y)!=(by>y) and x < dx*(y-ay)/dy+ax:
                inside=not inside
        if (inside or distance<1e-9) and distance+1e-9>=radius:
            return True
    return False


def found_match(probability, threshold, supported):
    # Compare on the probability side so a literal probability of 0.9 does not
    # accidentally pass the strict u < 0.1 boundary through subtraction rounding.
    return supported and probability is not None and probability > 1-threshold


class ObjectSearchBeliefManager:
    def __init__(self, mission_id: str, package: Mapping[str, object], state=None):
        self.mission_id=mission_id
        self.package=plain(package)
        self.data={"mission_id":mission_id,"package":self.package,"belief_revision":0,
                   "request_revision":0,"observations":{},"tracks":{},"objectives":{},
                   "reports":[],"report_signatures":{}}
        if state is not None:
            if state["mission_id"]!=mission_id or state["package"]!=self.package:
                raise ValueError("search belief checkpoint scope mismatch")
            self.data=plain(state)

    def state(self):
        return copy.deepcopy(self.data)

    def ingest(self, section: Mapping[str, object], mission_time_s: float) -> SearchBeliefSnapshot:
        section=plain(section)
        if section["schema_version"]!=1 or section["package"]!=self.package:
            raise ValueError("incompatible search evidence package")
        if section["revision"]<self.data["request_revision"]:
            raise ValueError("search request revision moved backwards")
        changed=section["revision"]!=self.data["request_revision"]
        updated=self.state()
        for obs in section["observations"]:
            oid=obs["observation_id"]
            if oid in updated["observations"]:
                if updated["observations"][oid]!=obs:
                    raise ValueError("observation_id_conflict")
                continue
            if obs["uncertainty_model"]!="categorical_symmetric_error_v1" or obs["coordinate_frame"]!="local_ned":
                raise ValueError("unsupported search evidence model/frame")
            if obs["acquired_at_s"]>mission_time_s:
                raise ValueError("future search evidence")
            updated["observations"][oid]=obs
            track=updated["tracks"].setdefault(obs["track_id"],{"attributes":{},"observations":[]})
            for attr, reading in obs["attributes"].items():
                values=self.package["vocabulary"][attr]
                if reading["value"] not in values:
                    raise ValueError("unsupported attribute value")
                u=reading["uncertainty"]
                if u is None:
                    continue
                prior=track["attributes"].get(attr,{v:1/len(values) for v in values})
                if prior is not None:
                    track["attributes"][attr]=BayesianBeliefManager.categorical_update(prior,reading["value"],u)
            track["observations"].append(oid)
            changed=True
        updated["objectives"]=section["objectives"]
        updated["report_signatures"]={k:v for k,v in updated["report_signatures"].items() if k in updated["objectives"]}
        updated["request_revision"]=section["revision"]
        if changed:
            updated["belief_revision"]+=1
        self.data=updated
        snapshot=self.snapshot()
        for target,obj in self.data["objectives"].items():
            candidates=[m for m in snapshot.matches if m.target_id==target]
            candidates.sort(key=lambda m:(m.found,m.probability or 0),reverse=True)
            best=candidates[0] if candidates else None
            signature={"objective":obj,"track_id":best.track_id if best else None,"found":bool(best and best.found),
                       "position":list(best.position) if best else None,"probability":best.probability if best else None}
            previous=self.data["report_signatures"].get(target)
            if signature!=previous and best and (best.found or (previous and previous["found"])):
                self.data["reports"].append({
                    "report_id":f"search-report:{len(self.data['reports'])+1}","target_id":target,
                    "revision":section["revision"],"track_id":best.track_id,"position":list(best.position),
                    "reported_at_s":mission_time_s,"observed_at_s":best.observed_at_s,
                    "status":"found" if best.found else "withdrawn",
                    "supporting_observation_ids":list(best.supporting_observation_ids),"uncertainty":best.uncertainty,
                })
            self.data["report_signatures"][target]=copy.deepcopy(signature)
        return snapshot

    def snapshot(self) -> SearchBeliefSnapshot:
        matches=[]
        for target,obj in self.data["objectives"].items():
            for tid,track in self.data["tracks"].items():
                probabilities=[]
                for attr,value in obj["attributes"].items():
                    distribution=track["attributes"].get(attr)
                    if distribution is None:
                        break
                    probabilities.append(distribution[value])
                probability=math.prod(probabilities) if len(probabilities)==len(obj["attributes"]) else None
                observations=[self.data["observations"][oid] for oid in track["observations"]]
                best=min(observations,key=lambda o:(o["position_uncertainty_m"] if o["position_uncertainty_m"] is not None else math.inf,-o["acquired_at_s"]))
                supported=location_supported(best["position"],best["position_uncertainty_m"],
                    [self.package["areas"][a]["polygon"] for a in obj["area_ids"]])
                found=found_match(probability,self.package["found_threshold"],supported)
                reason=None if found else "localization_unavailable" if not supported else "attributes_unresolved" if probability is None else "match_uncertain"
                matches.append(SearchMatch(target,tid,probability,tuple(best["position"]),best["position_uncertainty_m"],
                    best["acquired_at_s"],tuple(track["observations"]),found,reason))
        return SearchBeliefSnapshot(self.mission_id,self.data["request_revision"],self.data["belief_revision"],tuple(matches))
