"""Filesystem adapter for canonical JSON FSM authority records."""

from __future__ import annotations

import os
from pathlib import Path

from onr.contracts.fsm import FSMExecutionRecord, Statechart


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class JsonFSMStateStore:
    """Persist a Statechart and Execution Record as separate canonical JSON files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def load_statechart(self) -> Statechart | None:
        path = self.root / "statechart.json"
        if not path.exists():
            return None
        try:
            return Statechart.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"FSM Statechart storage is corrupt: {path}") from exc

    def load_execution_record(self) -> FSMExecutionRecord | None:
        path = self.root / "execution-record.json"
        if not path.exists():
            return None
        try:
            return FSMExecutionRecord.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"FSM Execution Record storage is corrupt: {path}") from exc

    def save_statechart(self, statechart: Statechart) -> None:
        _atomic_write(self.root / "statechart.json", statechart.to_canonical_json())

    def save_execution_record(self, record: FSMExecutionRecord) -> None:
        _atomic_write(self.root / "execution-record.json", record.to_canonical_json())
