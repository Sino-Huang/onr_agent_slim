"""Runtime-facing seam for mission operational-log summarization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol, cast


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class SummaryArtifact:
    """One durable summary produced from one incremental log range."""

    schema_version: int
    summary_id: str
    mission_id: str
    sequence: int
    created_at: str
    input_start_sequence: int
    input_end_sequence: int
    prior_summary_ids: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported summary artifact schema version")
        if not self.summary_id.strip() or not self.mission_id.strip():
            raise ValueError("summary identity fields must be non-empty")
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("summary sequence must be positive")
        if isinstance(self.input_start_sequence, bool) or self.input_start_sequence < 1:
            raise ValueError("summary input start sequence must be positive")
        if isinstance(self.input_end_sequence, bool):
            raise ValueError("summary input end sequence must be an integer")
        if self.input_end_sequence < self.input_start_sequence:
            raise ValueError("summary input range is invalid")
        if not self.summary.strip():
            raise ValueError("summary text must be non-empty")
        if not self.created_at.strip():
            raise ValueError("summary creation time must be non-empty")
        if any(not summary_id.strip() for summary_id in self.prior_summary_ids):
            raise ValueError("prior summary IDs must be non-empty")

    @classmethod
    def create(
        cls,
        mission_id: str,
        sequence: int,
        input_start_sequence: int,
        input_end_sequence: int,
        prior_summary_ids: tuple[str, ...],
        summary: str,
        *,
        created_at: str | None = None,
        summary_id: str | None = None,
    ) -> "SummaryArtifact":
        return cls(
            schema_version=1,
            summary_id=summary_id or f"{mission_id}:summary:{sequence}",
            mission_id=mission_id,
            sequence=sequence,
            created_at=created_at or _utc_now(),
            input_start_sequence=input_start_sequence,
            input_end_sequence=input_end_sequence,
            prior_summary_ids=prior_summary_ids,
            summary=summary,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "summary_id": self.summary_id,
            "mission_id": self.mission_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "input_start_sequence": self.input_start_sequence,
            "input_end_sequence": self.input_end_sequence,
            "prior_summary_ids": list(self.prior_summary_ids),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SummaryArtifact":
        prior_ids = value.get("prior_summary_ids", [])
        if not isinstance(prior_ids, (list, tuple)):
            raise ValueError("prior summary IDs must be a list")
        schema_version = value.get("schema_version")
        sequence = value.get("sequence")
        input_start = value.get("input_start_sequence")
        input_end = value.get("input_end_sequence")
        if any(
            isinstance(item, bool) or not isinstance(item, (str, int))
            for item in (schema_version, sequence, input_start, input_end)
        ):
            raise ValueError("summary numeric fields must be integers")
        return cls(
            schema_version=int(cast(str | int, schema_version)),
            summary_id=str(value["summary_id"]),
            mission_id=str(value["mission_id"]),
            sequence=int(cast(str | int, sequence)),
            created_at=str(value["created_at"]),
            input_start_sequence=int(cast(str | int, input_start)),
            input_end_sequence=int(cast(str | int, input_end)),
            prior_summary_ids=tuple(str(item) for item in prior_ids),
            summary=str(value["summary"]),
        )


class MissionLogSummarizer(Protocol):
    """Independent heartbeat operation for one mission's operational log."""

    def heartbeat(self, mission_id: str) -> SummaryArtifact | None: ...


__all__ = ["MissionLogSummarizer", "SummaryArtifact"]
