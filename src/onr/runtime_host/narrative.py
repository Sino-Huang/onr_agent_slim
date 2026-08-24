"""Durable optional Run Narrative projection."""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Protocol, cast

NARRATIVE_SCHEMA_VERSION = 1
SUMMARY_UNAVAILABLE_EVIDENCE = {
    "kind": "summary-unavailable",
    "message": "Run Narrative generation failed; Mission Run state is unaffected.",
}


class RunNarrativeSummarizer(Protocol):
    """Summarize issued public observation envelopes for one Mission Run."""

    def summarize_narrative(
        self,
        *,
        mission_id: str,
        mission_run_id: str,
        terminal: bool,
        observations: list[dict[str, object]],
    ) -> str: ...


class RunNarrativeRecord:
    """Durable state for one lazily generated Run Narrative."""

    def __init__(self, path: Path, mission_run_id: str) -> None:
        self.path = Path(path)
        self.mission_run_id = mission_run_id
        self.data = self._load()
        attempt = self.data["attempt_in_progress"]
        if isinstance(attempt, dict):
            self._recover_interrupted_attempt(attempt)

    @property
    def source_watermark(self) -> int:
        return cast(int, self.data["source_watermark"])

    @property
    def last_attempt_at(self) -> str | None:
        value = self.data["last_attempt_at"]
        return value if isinstance(value, str) else None

    @property
    def terminal_generated(self) -> bool:
        return self.data["terminal_generated"] is True

    def begin_attempt(self, *, started_at: str, terminal: bool) -> None:
        self.data["last_attempt_at"] = started_at
        self.data["attempt_in_progress"] = {
            "started_at": started_at,
            "terminal": terminal,
        }
        self._save()

    def public_narrative(self) -> dict[str, object]:
        narrative = self.data["narrative"]
        if not isinstance(narrative, dict):
            raise RuntimeError("runtime host narrative record is invalid")
        result = dict(narrative)
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            result["evidence"] = dict(evidence)
        return result

    def publish_available(
        self,
        *,
        text: str,
        generated_at: str,
        source_watermark: int,
        terminal: bool,
    ) -> None:
        self.data["attempt_in_progress"] = None
        self.data["source_watermark"] = source_watermark
        self.data["last_attempt_at"] = generated_at
        if terminal:
            self.data["terminal_generated"] = True
        self.data["narrative"] = {
            "status": "available",
            "text": text,
            "generated_at": generated_at,
            "source_watermark": source_watermark,
            "terminal": terminal,
            "evidence": None,
        }
        self._save()

    def publish_unavailable(self, *, generated_at: str, terminal: bool) -> None:
        self.data["attempt_in_progress"] = None
        self.data["last_attempt_at"] = generated_at
        if terminal:
            self.data["terminal_generated"] = True
        self.data["narrative"] = {
            "status": "unavailable",
            "text": None,
            "generated_at": generated_at,
            "source_watermark": self.source_watermark,
            "terminal": terminal,
            "evidence": dict(SUMMARY_UNAVAILABLE_EVIDENCE),
        }
        self._save()

    def _load(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema_version": NARRATIVE_SCHEMA_VERSION,
                "mission_run_id": self.mission_run_id,
                "source_watermark": 0,
                "last_attempt_at": None,
                "terminal_generated": False,
                "attempt_in_progress": None,
                "narrative": {
                    "status": "none",
                    "text": None,
                    "generated_at": None,
                    "source_watermark": 0,
                    "terminal": False,
                    "evidence": None,
                },
            }
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("runtime host narrative record is invalid") from exc
        if not _valid_record(raw, self.mission_run_id):
            raise RuntimeError("runtime host narrative record is invalid")
        return dict(raw)

    def _recover_interrupted_attempt(self, attempt: dict[str, object]) -> None:
        started_at = cast(str, attempt["started_at"])
        terminal = attempt["terminal"] is True
        self.data["attempt_in_progress"] = None
        self.data["last_attempt_at"] = started_at
        if terminal:
            self.data["terminal_generated"] = True
        self.data["narrative"] = {
            "status": "unavailable",
            "text": None,
            "generated_at": started_at,
            "source_watermark": self.source_watermark,
            "terminal": terminal,
            "evidence": dict(SUMMARY_UNAVAILABLE_EVIDENCE),
        }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                self.data,
                handle,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass


def sanitize_narrative_text(value: object) -> str | None:
    """Return bounded publishable text, or None for an invalid result."""

    if not isinstance(value, str):
        return None
    cleaned = "".join(
        character
        for character in value
        if character == "\n" or unicodedata.category(character) != "Cc"
    ).strip()
    if not cleaned:
        return None
    return cleaned[:4000]


def _valid_record(raw: object, mission_run_id: str) -> bool:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "mission_run_id",
        "source_watermark",
        "last_attempt_at",
        "terminal_generated",
        "attempt_in_progress",
        "narrative",
    }:
        return False
    watermark = raw["source_watermark"]
    last_attempt_at = raw["last_attempt_at"]
    attempt = raw["attempt_in_progress"]
    narrative = raw["narrative"]
    if (
        raw["schema_version"] != NARRATIVE_SCHEMA_VERSION
        or raw["mission_run_id"] != mission_run_id
        or isinstance(watermark, bool)
        or not isinstance(watermark, int)
        or watermark < 0
        or (last_attempt_at is not None and not _nonblank_text(last_attempt_at))
        or not isinstance(raw["terminal_generated"], bool)
        or not _valid_attempt(attempt, last_attempt_at)
        or not isinstance(narrative, dict)
        or set(narrative) != {
            "status",
            "text",
            "generated_at",
            "source_watermark",
            "terminal",
            "evidence",
        }
        or narrative["source_watermark"] != watermark
        or not isinstance(narrative["terminal"], bool)
    ):
        return False
    status = narrative["status"]
    text = narrative["text"]
    generated_at = narrative["generated_at"]
    evidence = narrative["evidence"]
    terminal = narrative["terminal"]
    terminal_generated = raw["terminal_generated"]
    if attempt is not None and (terminal_generated is True or terminal is True):
        return False
    if terminal is True and terminal_generated is not True:
        return False
    if terminal_generated is True and terminal is not True:
        return False
    if status == "none":
        return (
            text is None
            and generated_at is None
            and evidence is None
            and watermark == 0
            and terminal is False
            and (last_attempt_at is None if attempt is None else True)
            and terminal_generated is False
        )
    if status == "available":
        return (
            _nonblank_text(text)
            and len(text) <= 4000
            and _nonblank_text(generated_at)
            and evidence is None
        )
    if status == "unavailable":
        return (
            text is None
            and _nonblank_text(generated_at)
            and evidence == SUMMARY_UNAVAILABLE_EVIDENCE
        )
    return False


def _nonblank_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_attempt(attempt: object, last_attempt_at: object) -> bool:
    if attempt is None:
        return True
    return (
        isinstance(attempt, dict)
        and set(attempt) == {"started_at", "terminal"}
        and _nonblank_text(attempt["started_at"])
        and isinstance(attempt["terminal"], bool)
        and attempt["started_at"] == last_attempt_at
    )
