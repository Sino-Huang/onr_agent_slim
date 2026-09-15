"""Atomic checkpoint for Mission 4 categorical beliefs and report history."""
import json
import os
import tempfile
from pathlib import Path

from onr.application.bayesian_belief import BayesianBeliefManager


class ObjectSearchBeliefStore:
    def __init__(self, path: str | Path):
        self.path=Path(path)

    def load(self, mission_id, package):
        state=json.loads(self.path.read_text()) if self.path.exists() else None
        return BayesianBeliefManager.for_object_search(mission_id,package,state)

    def save(self, manager):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=self.path.parent,delete=False) as stream:
            json.dump(manager.state(),stream,allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
            temporary=stream.name
        os.replace(temporary,self.path)
