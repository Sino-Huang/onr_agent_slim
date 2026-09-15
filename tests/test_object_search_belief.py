import copy
from dataclasses import FrozenInstanceError

import pytest

from onr.application.bayesian_belief import BayesianBeliefManager
from onr.application.object_search_belief import found_match
from onr.adapters.object_search_belief_store import ObjectSearchBeliefStore


PACKAGE={"schema_version":1,"vocabulary":{"type":["container","truck"],"color":["red","blue"]},
         "areas":{"dock":{"polygon":[[-20,-20],[20,-20],[20,20],[-20,20]],"prior":1}},
         "found_threshold":0.1,"mission_time_budget_s":300,"obstacles":[],"keep_out_zones":[]}
OBJECTIVE={"target_id":"wanted","description":"red container","attributes":{"type":"container","color":"red"},"area_ids":["dock"]}


def observation(index, *, u=0.2, color="red", position=None, radius=1):
    return {"observation_id":f"obs:{index}","track_id":"seen:1","acquired_at_s":index,
            "source":"simulated","coordinate_frame":"local_ned","position":position or [0,0,0],
            "position_uncertainty_m":radius,"uncertainty_model":"categorical_symmetric_error_v1",
            "attributes":{"color":{"value":color,"uncertainty":u},"type":{"value":"container","uncertainty":u}}}


def section(observations,revision=1,objectives=None):
    return {"schema_version":1,"package":PACKAGE,"revision":revision,"observations":observations,
            "objectives":objectives if objectives is not None else {"wanted":OBJECTIVE}}


def test_distinct_evidence_complete_match_and_atomic_replay():
    manager=BayesianBeliefManager.for_object_search("m4",PACKAGE)
    previous=0
    for count in [1,2,3]:
        result=manager.ingest(section([observation(i) for i in range(count)]),count)
        assert result.matches[0].probability>previous
        previous=result.matches[0].probability
        assert result.matches[0].found == (count==3)
    before=manager.state()
    assert manager.ingest(section([observation(i) for i in range(3)]),5)==result
    assert manager.state()==before
    assert len(before["reports"])==1
    with pytest.raises(FrozenInstanceError):
        result.matches[0].found=False
    bad=observation(0);bad["attributes"]["color"]["value"]="blue"
    with pytest.raises(ValueError,match="conflict"):
        manager.ingest(section([bad]),5)
    assert manager.state()==before


@pytest.mark.parametrize("p,expected",[(0.899999,False),(0.9,False),(0.900001,True)])
def test_strict_threshold(p,expected):
    assert found_match(p,0.1,True)==expected
    assert not found_match(p,0.1,False)
    assert not found_match(None,0.1,True)
    assert not found_match(0.94,0.05,True)


@pytest.mark.parametrize("kwargs",[{"u":None},{"color":"blue","u":0.001},
                                  {"position":[30,0,0],"u":0.001},
                                  {"radius":None,"u":0.001},{"radius":6,"u":0.001},
                                  {"position":[19.5,0,0],"radius":1,"u":0.001}])
def test_missing_uncertainty_nonmatches_and_unsupported_locations(kwargs):
    manager=BayesianBeliefManager.for_object_search("m4",PACKAGE)
    result=manager.ingest(section([observation(0,**kwargs)]),1)
    assert not result.matches[0].found
    assert not manager.state()["reports"]


def test_later_objectives_reuse_beliefs_and_checkpoint(tmp_path):
    store=ObjectSearchBeliefStore(tmp_path / "belief.json")
    manager=store.load("m4",PACKAGE)
    obs=[observation(i) for i in range(3)]
    manager.ingest(section(obs,objectives={}),3)
    assert not manager.state()["reports"]
    result=manager.ingest(section(obs,revision=2),4)
    assert result.matches[0].found
    assert len(manager.state()["observations"])==3
    store.save(manager)
    restored=store.load("m4",PACKAGE)
    assert restored.state()==manager.state()
    assert restored.ingest(section(obs,revision=2),5)==result
    assert len(restored.state()["reports"])==1
    changed=copy.deepcopy(OBJECTIVE);changed["attributes"]["color"]="blue"
    result=restored.ingest(section(obs,revision=3,objectives={"wanted":changed}),6)
    assert not result.matches[0].found
    assert restored.state()["reports"][-1]["status"]=="withdrawn"
    assert len(restored.state()["observations"])==3


def test_contradictory_certain_observations_do_not_create_confidence():
    manager=BayesianBeliefManager.for_object_search("m4",PACKAGE)
    manager.ingest(section([observation(0,u=0)]),1)
    result=manager.ingest(section([observation(0,u=0),observation(1,u=0,color="blue")]),2)
    assert result.matches[0].probability is None
    assert not result.matches[0].found
    assert manager.state()["reports"][-1]["status"]=="withdrawn"
