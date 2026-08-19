"""File-backed mission operational-log summarization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.ports.operational_log import OperationalLog, OperationalLogRecord


class SummarizationError(RuntimeError):
    """The configured summarization model could not produce a summary."""


class FileMissionLogSummarizer:
    """Summarize new mission-log records and persist an immutable history."""

    def __init__(self, operational_log: OperationalLog, storage_root: Path, model: Any) -> None:
        self.operational_log = operational_log
        self.storage_root = Path(storage_root)
        self.model = model
        self._lock = RLock()

    @staticmethod
    def _mission_dir(root: Path, mission_id: str) -> Path:
        if not mission_id or Path(mission_id).name != mission_id or mission_id in {".", ".."}:
            raise ValueError("mission ID must be one path component")
        return root / "summaries" / mission_id

    @staticmethod
    def _atomic_write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    def _load_artifacts(self, mission_id: str) -> tuple[SummaryArtifact, ...]:
        mission_dir = self._mission_dir(self.storage_root, mission_id)
        if not mission_dir.exists():
            return ()
        artifacts: list[SummaryArtifact] = []
        for path in sorted(mission_dir.glob("[0-9]*.json")):
            with path.open(encoding="utf-8") as handle:
                artifact = SummaryArtifact.from_dict(json.load(handle))
            if artifact.mission_id != mission_id:
                raise ValueError("summary directory contains another mission")
            artifacts.append(artifact)
        artifacts.sort(key=lambda item: item.sequence)
        if tuple(item.sequence for item in artifacts) != tuple(
            range(1, len(artifacts) + 1)
        ):
            raise ValueError("summary history contains a sequence gap")
        return tuple(artifacts)

    def _last_consumed_sequence(
        self, mission_id: str, artifacts: tuple[SummaryArtifact, ...]
    ) -> int:
        artifact_cursor = artifacts[-1].input_end_sequence if artifacts else 0
        cursor_path = self._mission_dir(self.storage_root, mission_id) / "cursor.json"
        if not cursor_path.exists():
            return artifact_cursor
        with cursor_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        cursor = value.get("last_sequence") if isinstance(value, dict) else None
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("summary cursor must contain a non-negative integer")
        return max(artifact_cursor, cursor)

    @staticmethod
    def _prompt(
        mission_id: str,
        records: tuple[OperationalLogRecord, ...],
        previous: tuple[SummaryArtifact, ...],
    ) -> str:
        new_records = [record.to_dict() for record in records]
        prior_summaries = [
            {
                "summary_id": artifact.summary_id,
                "sequence": artifact.sequence,
                "created_at": artifact.created_at,
                "summary": artifact.summary,
            }
            for artifact in previous
        ]
        return (
            "Summarize the mission's new operational log records. Return only the summary text.\n"
            f"MISSION_ID: {mission_id}\n"
            "PREVIOUS SUMMARIES (oldest to newest, at most 3):\n"
            f"{json.dumps(prior_summaries, sort_keys=True, separators=(",", ":"))}\n"
            "NEW LOG RECORDS (not included in previous summaries):\n"
            f"{json.dumps(new_records, sort_keys=True, separators=(",", ":"))}"
        )

    @staticmethod
    def _response_text(response: object) -> str:
        value = response if isinstance(response, str) else getattr(response, "content", None)
        if not isinstance(value, str) or not value.strip():
            raise SummarizationError("summarization model returned no text")
        return value.strip()

    def heartbeat(self, mission_id: str) -> SummaryArtifact | None:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        with self._lock:
            artifacts = self._load_artifacts(mission_id)
            cursor = self._last_consumed_sequence(mission_id, artifacts)
            records = self.operational_log.read_after_sequence(mission_id, cursor)
            if not records:
                return None

            prompt = self._prompt(mission_id, records, artifacts[-3:])
            invoke = getattr(self.model, "invoke", None)
            if not callable(invoke):
                raise SummarizationError("configured summarization model has no invoke method")
            try:
                response = invoke(
                    prompt,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                summary = self._response_text(response)
            except SummarizationError:
                raise
            except Exception as exc:
                raise SummarizationError(f"summarization model invocation failed: {exc}") from exc

            artifact = SummaryArtifact.create(
                mission_id,
                len(artifacts) + 1,
                records[0].sequence,
                records[-1].sequence,
                tuple(item.summary_id for item in artifacts[-3:]),
                summary,
            )
            mission_dir = self._mission_dir(self.storage_root, mission_id)
            self._atomic_write(
                mission_dir / f"{artifact.sequence:020d}.json", artifact.to_dict()
            )
            self._atomic_write(
                mission_dir / "cursor.json",
                {"schema_version": 1, "last_sequence": artifact.input_end_sequence},
            )
            return artifact


__all__ = ["FileMissionLogSummarizer", "SummarizationError"]
