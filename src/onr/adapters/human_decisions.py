"""Filesystem persistence for Human Decision Requests and decisions."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionRequest,
    RunCheckpoint,
)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class FileHumanDecisionStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save_pause(
        self, request: HumanDecisionRequest, checkpoint: RunCheckpoint
    ) -> None:
        directory = self._directory(request.mission_id, request.mission_run_id)
        self._save_immutable(directory / "request.json", request.to_canonical_json())
        self._save_immutable(
            directory / "checkpoint.json", checkpoint.to_canonical_json()
        )

    def load_request(
        self, mission_id: str, mission_run_id: str
    ) -> HumanDecisionRequest | None:
        path = self._directory(mission_id, mission_run_id) / "request.json"
        return (
            HumanDecisionRequest.from_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def load_request_by_id(self, request_id: str) -> HumanDecisionRequest | None:
        for path in self.root.glob("*/*/request.json"):
            request = HumanDecisionRequest.from_json(path.read_text(encoding="utf-8"))
            if request.request_id == request_id:
                return request
        return None

    def load_checkpoint(
        self, mission_id: str, mission_run_id: str
    ) -> RunCheckpoint | None:
        path = self._directory(mission_id, mission_run_id) / "checkpoint.json"
        return (
            RunCheckpoint.from_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def save_decision(self, decision: HumanDecision) -> HumanDecision:
        path = (
            self._directory(decision.mission_id, decision.mission_run_id)
            / "decision.json"
        )
        self._save_immutable(path, decision.to_canonical_json())
        return HumanDecision.from_json(path.read_text(encoding="utf-8"))

    def load_decision(
        self, mission_id: str, mission_run_id: str
    ) -> HumanDecision | None:
        path = self._directory(mission_id, mission_run_id) / "decision.json"
        return (
            HumanDecision.from_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def _directory(self, mission_id: str, mission_run_id: str) -> Path:
        return (
            self.root
            / quote(mission_id, safe="._-")
            / quote(mission_run_id, safe="._-")
        )

    @staticmethod
    def _save_immutable(path: Path, content: str) -> None:
        if path.is_file():
            if path.read_text(encoding="utf-8") != content:
                raise ValueError("durable Human Decision identity conflict")
            return
        _atomic_write(path, content)


__all__ = ["FileHumanDecisionStore"]
