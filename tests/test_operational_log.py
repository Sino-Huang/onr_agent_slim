from __future__ import annotations

from pathlib import Path

from onr.adapters.operational_log import FileOperationalLog


def test_file_operational_log_replays_after_restart_in_sequence(tmp_path: Path) -> None:
    root = tmp_path / "storage" / "operational-log"
    first = FileOperationalLog(root)
    first.emit("mission-1", "runtime", "agent", "started", details={"operation": "run"})
    second_record = first.emit(
        "mission-1",
        "runtime",
        "transport",
        "received",
        details={"event_id": "event-1"},
    )

    restarted = FileOperationalLog(root)
    replay = restarted.replay("mission-1")

    assert [record.sequence for record in replay] == [1, 2]
    assert restarted.read_after_sequence("mission-1", 1) == (second_record,)
    assert restarted.append(second_record) == second_record
    assert list((root / "mission-1" / "events").glob("*.json"))


def test_file_operational_log_allowlists_details_and_separates_storage(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    logger = FileOperationalLog(root / "operational-log")
    secret = "sk-test-secret-value"
    prompt = "raw model prompt that must not be persisted"
    response = "raw model response that must not be persisted"

    logger.emit(
        "mission-1",
        "solver",
        "solver",
        "failed",
        details={
            "api_key": secret,
            "prompt": prompt,
            "response": response,
            "error_message": "exception text containing " + secret,
            "error_type": "PlannerError",
            "plan_revision": 2,
        },
    )

    event_file = root / "operational-log" / "mission-1" / "events" / "00000000000000000001.json"
    raw = event_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert prompt not in raw
    assert response not in raw
    assert "error_message" not in raw
    assert event_file.parent != root / "mission-memory" / "mission-1"
    assert event_file.parent != root / "transport" / "events" / "mission-1"
