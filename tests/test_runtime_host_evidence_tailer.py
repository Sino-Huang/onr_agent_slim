"""Incremental evidence ingestion matches a full rescan of the same evidence.

The fixture is a trimmed copy of the accepted Mission 1 live run
(``run.a6CqxX``): 60 operational-log records, the first files of every
transport topic (including one bulky ``environment-planning`` snapshot), and
four maneuver commands with their receipts.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from onr.runtime_host.observations import (
    EvidenceTailer,
    RunObservations,
    project_evidence,
)

FIXTURE = Path(__file__).parent / "fixtures" / "run_a6cqxx_evidence"
MISSION = "mission:demo"
OPERATIONAL = Path("agent-storage/operational-log") / MISSION / "events"
COMMANDS = Path("transport/commands/maneuver-adapter/mission%3Ademo")
RECEIPTS = Path("transport/receipts")


def _copy(
    relative: Path, target: Path, select: Callable[[list[str]], list[str]]
) -> None:
    source = FIXTURE / relative
    destination = target / relative
    destination.mkdir(parents=True, exist_ok=True)
    for name in select(sorted(path.name for path in source.iterdir())):
        shutil.copy2(source / name, destination / name)


def _topic_dirs() -> list[Path]:
    topics = FIXTURE / "transport" / "topics"
    return sorted(
        Path("transport/topics") / topic.name / "missions" / "mission%3Ademo"
        for topic in topics.iterdir()
    )


def _tailer(root: Path) -> EvidenceTailer:
    return EvidenceTailer(
        MISSION,
        operational_log_root=root / "agent-storage" / "operational-log",
        transport_root=root / "transport",
    )


def _canonical(records: Sequence[object]) -> list[str]:
    return sorted(json.dumps(record, sort_keys=True) for record in records)


def _items(entries: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {str(entry["event_id"]): entry["item"] for entry in entries}


def test_incremental_ingestion_equals_full_rescan(tmp_path: Path) -> None:
    live = tmp_path / "live"
    tailer = _tailer(live)
    incremental = RunObservations(tmp_path / "incremental.json", MISSION, tailer=tailer)

    # Stage 1: the run has just started; one command still awaits its receipt.
    _copy(OPERATIONAL, live, lambda names: names[:30])
    for topic in _topic_dirs():
        _copy(topic, live, lambda names: names[:1])
    _copy(COMMANDS, live, lambda names: names[:2])
    _copy(RECEIPTS, live, lambda names: names[:1])
    incremental.refresh(observed_at="2026-08-24T12:00:01+00:00")
    first_poll_records = len(tailer.records())

    # Stage 2: operational-log records published past a sequence gap wait for it.
    _copy(OPERATIONAL, live, lambda names: names[40:])
    incremental.refresh(observed_at="2026-08-24T12:00:02+00:00")
    assert [record["sequence"] for record in tailer.operational_records()] == list(
        range(1, 31)
    )

    # Stage 3: everything else, including the late receipt and the gap records.
    _copy(OPERATIONAL, live, lambda names: names)
    for topic in _topic_dirs():
        _copy(topic, live, lambda names: names)
    _copy(COMMANDS, live, lambda names: names)
    _copy(RECEIPTS, live, lambda names: names)
    incremental_entries = incremental.refresh(observed_at="2026-08-24T12:00:03+00:00")

    assert tailer.poll() == []
    assert len(tailer.records()) > first_poll_records

    rescan_tailer = _tailer(live)
    rescanned = RunObservations(
        tmp_path / "rescan.json", MISSION, tailer=rescan_tailer
    ).refresh(observed_at="2026-08-24T12:00:03+00:00")

    assert _canonical(tailer.records()) == _canonical(rescan_tailer.records())
    assert [record["sequence"] for record in tailer.operational_records()] == list(
        range(1, 61)
    )
    assert _items(incremental_entries) == _items(rescanned)
    plain_projection = {
        item.event_id: item.to_dict()
        for item in project_evidence(rescan_tailer.records())
    }
    assert _items(incremental_entries) == plain_projection
    assert [entry["observation_sequence"] for entry in incremental_entries] == list(
        range(1, len(incremental_entries) + 1)
    )
    kinds = {
        item["event_kind"]  # type: ignore[index]
        for item in _items(incremental_entries).values()
    }
    assert {"command", "command-receipt", "mission-snapshot"} <= kinds


def test_dropped_payload_keys_do_not_change_projected_evidence(tmp_path: Path) -> None:
    tailer = _tailer(FIXTURE)
    tailer.poll()
    full_records: list[dict[str, object]] = []
    for record in tailer.records():
        if (
            "event_id" in record
            and "record_id" not in record
            and "command_id" not in record
        ):
            topic_files = (FIXTURE / "transport" / "topics").glob(
                f"*/missions/mission%3Ademo/{int(str(record['sequence'])):020d}-*.json"
            )
            matching = [
                json.loads(path.read_text(encoding="utf-8")) for path in topic_files
            ]
            full_records.extend(
                raw for raw in matching if raw["event_id"] == record["event_id"]
            )
        else:
            full_records.append(dict(record))

    reduced = {
        item.event_id: item.to_dict() for item in project_evidence(tailer.records())
    }
    full = {item.event_id: item.to_dict() for item in project_evidence(full_records)}

    def accepted(items: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
        return {
            key: value for key, value in items.items() if value["event_kind"] != "error"
        }

    assert len(full_records) == len(tailer.records())
    assert accepted(reduced) == accepted(full)
    assert len(reduced) == len(full)
