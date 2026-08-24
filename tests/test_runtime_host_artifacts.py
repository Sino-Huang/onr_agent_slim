from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from onr.runtime import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)
from onr.runtime_host import RuntimeHost, create_app
from onr.runtime_host.artifacts import (
    MAX_ENTRY_BYTES,
    MAX_ENTRY_CONTENT_BYTES,
    MAX_ENVELOPE_BYTES,
)
from onr.runtime_host.observations import encode_cursor


class ScriptedEvidenceSource:
    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]:
        del mission_id
        return ()


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        llm=LLMConfig("openai", "http://127.0.0.1:1/v1", "offline", "EMPTY", 0),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1),
            PlannerConfig(Path(__file__), 1, Path(__file__)),
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", tmp_path / "transport"),
        storage=StorageConfig(tmp_path / "storage"),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=False,
        agent_name="test-agent",
    )


def _clock() -> str:
    return datetime(2026, 8, 24, 12, 0, tzinfo=UTC).isoformat()


def _ids() -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def generate(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"{kind}-{counts[kind]}"

    return generate


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    config = _config(tmp_path)
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        config,
        clock=_clock,
        generate_id=_ids(),
        launch_worker=pending.append,
        evidence_source=ScriptedEvidenceSource(),
    )
    client = TestClient(create_app(host=host))
    response = client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": "Bearer console-secret"},
        json={
            "activation_request_id": "request-1",
            "console_session_id": "session-1",
            "mission_intent": "Survey sector seven",
            "source_authority": "operator_console",
        },
    )
    assert response.status_code == 202
    assert response.json()["mission_run_id"] == "run-1"
    return client, config.storage.root / "artifact-inbox" / "run-1"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _envelope(
    artifact_id: str,
    *,
    kind: str = "log",
    media_type: str = "text/plain",
    content: bytes | None = b"planner output",
    path: str = "content.bin",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "kind": kind,
        "media_type": media_type,
        "display": {"title": f"Title {artifact_id}", "summary": "Public summary"},
        "published_at": "2026-08-24T12:00:05Z",
        "content": (
            None
            if kind == "conversation"
            else {
                "path": path,
                "byte_size": len(content or b""),
                "content_digest": _digest(content or b""),
            }
        ),
    }


def _publish(
    inbox: Path,
    artifact_id: str,
    *,
    kind: str = "log",
    media_type: str = "text/plain",
    content: bytes | None = b"planner output",
    path: str = "content.bin",
    envelope: Mapping[str, object] | None = None,
) -> tuple[Path, dict[str, object]]:
    artifact_dir = inbox / artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    selected = dict(
        envelope
        or _envelope(
            artifact_id,
            kind=kind,
            media_type=media_type,
            content=content,
            path=path,
        )
    )
    if kind != "conversation" and content is not None:
        target = artifact_dir.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (artifact_dir / "artifact.json").write_text(
        json.dumps(selected), encoding="utf-8"
    )
    return artifact_dir, selected


def _entry(
    sequence: int,
    *,
    kind: str = "message",
    content: str | None = "Public message",
    content_ref: Mapping[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "sequence": sequence,
        "author": "operator",
        "time": "2026-08-24T12:00:10Z",
        "audience": "public",
        "kind": kind,
    }
    if content is not None:
        value["content"] = content
    if content_ref is not None:
        value["content_ref"] = dict(content_ref)
    return value


def _write_entry(artifact_dir: Path, filename: str, entry: object) -> Path:
    entries = artifact_dir / "entries"
    entries.mkdir(exist_ok=True)
    path = entries / filename
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def _artifacts(client: TestClient, query: str = "") -> Any:
    return client.get(f"/api/v1/mission-runs/run-1/artifacts{query}")


def _content(client: TestClient, artifact_id: str, query: str = "") -> Any:
    return client.get(
        f"/api/v1/mission-runs/run-1/artifacts/{artifact_id}/content{query}"
    )


def _entries(client: TestClient, artifact_id: str, query: str = "") -> Any:
    return client.get(
        f"/api/v1/mission-runs/run-1/artifacts/{artifact_id}/entries{query}"
    )


def _not_found(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def test_empty_inbox_and_valid_text_descriptor(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    assert _artifacts(client).json() == {
        "schema_version": 1,
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "artifacts": [],
        "next_cursor": None,
    }

    content = b"planner output"
    _publish(inbox, "planner-log", content=content)
    response = _artifacts(client)
    assert response.status_code == 200
    body = response.json()
    assert body["artifacts"] == [
        {
            "schema_version": 1,
            "artifact_id": "planner-log",
            "kind": "log",
            "media_type": "text/plain",
            "byte_size": len(content),
            "content_digest": _digest(content),
            "display": {
                "title": "Title planner-log",
                "summary": "Public summary",
            },
            "published_at": "2026-08-24T12:00:05Z",
            "classification": "text",
        }
    ]
    assert body["next_cursor"] == encode_cursor("run-1", 1)


@pytest.mark.parametrize(
    "case",
    [
        "dot-envelope",
        "envelope-without-content",
        "content-without-envelope",
        "symlink-content",
        "symlink-artifact-dir",
        "traversal-parent",
        "traversal-absolute",
        "schema-version",
        "schema-version-float",
        "oversized-envelope",
        "digest-mismatch",
        "size-mismatch",
        "directory-content",
        "mission-id-mismatch",
        "run-id-mismatch",
    ],
)
def test_discovery_omits_invalid_or_incomplete_publications(
    tmp_path: Path, case: str
) -> None:
    client, inbox = _client(tmp_path)
    artifact_id = "invalid-artifact"
    artifact_dir = inbox / artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    content = b"valid bytes"
    envelope = _envelope(artifact_id, content=content)

    if case == "dot-envelope":
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / ".artifact.json.tmp-1").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "envelope-without-content":
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "content-without-envelope":
        (artifact_dir / "content.bin").write_bytes(content)
    elif case == "symlink-content":
        outside = tmp_path / "outside.bin"
        outside.write_bytes(content)
        (artifact_dir / "content.bin").symlink_to(outside)
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "symlink-artifact-dir":
        artifact_dir.rmdir()
        outside = tmp_path / "outside-artifact"
        outside.mkdir()
        (outside / "content.bin").write_bytes(content)
        (outside / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
        artifact_dir.symlink_to(outside, target_is_directory=True)
    elif case.startswith("traversal"):
        bad_path = "../outside.bin" if case.endswith("parent") else "/tmp/outside.bin"
        envelope["content"] = {
            "path": bad_path,
            "byte_size": len(content),
            "content_digest": _digest(content),
        }
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case in {"schema-version", "schema-version-float"}:
        envelope["schema_version"] = 2 if case == "schema-version" else 1.0
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "oversized-envelope":
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / "artifact.json").write_bytes(b" " * (MAX_ENVELOPE_BYTES + 1))
    elif case == "digest-mismatch":
        cast_content = dict(cast(Mapping[str, object], envelope["content"]))
        cast_content["content_digest"] = _digest(b"different")
        envelope["content"] = cast_content
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "size-mismatch":
        cast_content = dict(cast(Mapping[str, object], envelope["content"]))
        cast_content["byte_size"] = len(content) + 1
        envelope["content"] = cast_content
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    elif case == "directory-content":
        (artifact_dir / "content.bin").mkdir()
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    else:
        envelope["mission_id" if case == "mission-id-mismatch" else "mission_run_id"] = (
            "wrong"
        )
        (artifact_dir / "content.bin").write_bytes(content)
        (artifact_dir / "artifact.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )

    assert _artifacts(client).json()["artifacts"] == []


def test_unknown_kind_is_generic_and_listing_pages_by_artifact_id(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    for artifact_id in ("z-last", "a-first", "m-middle"):
        _publish(
            inbox,
            artifact_id,
            kind="future_kind",
            media_type="application/x-future",
            content=artifact_id.encode(),
        )

    first = _artifacts(client, "?limit=2").json()
    assert [item["artifact_id"] for item in first["artifacts"]] == [
        "a-first",
        "m-middle",
    ]
    assert all(item["kind"] == "future_kind" for item in first["artifacts"])
    assert all(item["classification"] == "binary" for item in first["artifacts"])
    second = _artifacts(client, f"?cursor={first['next_cursor']}&limit=2").json()
    assert [item["artifact_id"] for item in second["artifacts"]] == ["z-last"]


def test_list_cursor_errors_and_unknown_run(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    _publish(inbox, "one")
    expected_cursor = _not_found(
        "invalid_cursor",
        "cursor is malformed, expired, or does not belong to this Mission Run",
    )
    assert _artifacts(client, "?cursor=bad").json() == expected_cursor
    foreign = encode_cursor("run-other", 0)
    assert _artifacts(client, f"?cursor={foreign}").json() == expected_cursor

    response = client.get("/api/v1/mission-runs/run-missing/artifacts")
    assert response.status_code == 404
    assert response.json() == _not_found(
        "mission_run_not_found", "Mission Run is unknown to this Runtime Host"
    )


def test_text_content_pages_chain_to_eof_and_validate_offsets(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    content = b"abcdefghij"
    _publish(inbox, "text", content=content)

    first = _content(client, "text", "?limit=4")
    assert first.status_code == 200
    assert first.json() == {
        "schema_version": 1,
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "artifact_id": "text",
        "classification": "text",
        "media_type": "text/plain",
        "byte_size": 10,
        "offset": 0,
        "next_offset": 4,
        "eof": False,
        "truncated": False,
        "content": "abcd",
    }
    second = _content(client, "text", "?offset=4&limit=6").json()
    assert second["content"] == "efghij"
    assert second["next_offset"] is None
    assert second["eof"] is True

    at_end = _content(client, "text", "?offset=10").json()
    assert at_end["offset"] == 10
    assert at_end["content"] == ""
    assert at_end["eof"] is True
    assert at_end["next_offset"] is None
    assert _content(client, "text", "?offset=11").status_code == 422
    assert _content(client, "text", "?limit=0").status_code == 422
    assert _content(client, "text", "?limit=16385").status_code == 422


def test_binary_content_is_metadata_only(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    content = bytes(range(32))
    _publish(
        inbox,
        "frame",
        kind="perception_frame",
        media_type="application/octet-stream",
        content=content,
    )
    assert _content(client, "frame").json() == {
        "schema_version": 1,
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "artifact_id": "frame",
        "classification": "binary",
        "media_type": "application/octet-stream",
        "byte_size": len(content),
        "offset": 0,
        "next_offset": None,
        "eof": True,
        "truncated": False,
        "content": None,
    }
    assert _content(client, "frame", "?offset=1").status_code == 422


def test_content_not_found_and_unavailable_paths(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    conversation, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    assert conversation.is_dir()
    expected_not_found = _not_found(
        "artifact_not_found", "Artifact is unknown to this Mission Run"
    )
    for artifact_id in ("missing", "invalid$id", "conversation"):
        response = _content(client, artifact_id)
        assert response.status_code == 404
        assert response.json() == expected_not_found

    artifact_dir, _ = _publish(inbox, "mutable", content=b"committed")
    assert _artifacts(client).status_code == 200
    replacement = artifact_dir / "replacement.bin"
    replacement.write_bytes(b"different")
    os.replace(replacement, artifact_dir / "content.bin")
    unavailable = _content(client, "mutable")
    assert unavailable.status_code == 404
    assert unavailable.json() == _not_found(
        "artifact_unavailable",
        "Artifact content failed validation or is unavailable",
    )
    assert _artifacts(client).json()["artifacts"][0]["artifact_id"] == "conversation"


def test_invalid_utf8_text_is_unavailable(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    _publish(inbox, "bad-text", content=b"valid-prefix\xff")
    response = _content(client, "bad-text")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_unavailable"


def test_text_windows_snap_to_utf8_boundaries(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    text = "ab€cd"
    _publish(inbox, "unicode", content=text.encode())

    first = _content(client, "unicode", "?limit=3").json()
    assert first["content"] == "ab"
    assert first["offset"] == 0
    assert first["next_offset"] == 2
    assert first["truncated"] is True
    second = _content(client, "unicode", "?offset=2&limit=5").json()
    assert second["content"] == "€cd"
    assert first["content"] + second["content"] == text

    middle = _content(client, "unicode", "?offset=3&limit=4").json()
    assert middle["offset"] == 5
    assert middle["content"] == "cd"

    tiny_middle = _content(client, "unicode", "?offset=3&limit=1").json()
    assert tiny_middle["offset"] == 5
    assert tiny_middle["content"] == ""
    assert tiny_middle["next_offset"] == 5


def test_conversation_entries_round_trip_order_duplicates_gaps_and_unknown_kind(
    tmp_path: Path,
) -> None:
    client, inbox = _client(tmp_path)
    artifact_dir, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    _write_entry(artifact_dir, "1.json", _entry(1, content="first"))
    _write_entry(artifact_dir, "03.json", _entry(3, content="filename first"))
    _write_entry(artifact_dir, "3.json", _entry(3, content="duplicate"))
    _write_entry(artifact_dir, "7.json", _entry(7, kind="future_kind", content="future"))

    response = _entries(client, "conversation")
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"] == "mission-1"
    assert body["artifact_id"] == "conversation"
    assert [entry["sequence"] for entry in body["entries"]] == [1, 3, 7]
    assert body["entries"][1]["content"] == "filename first"
    assert body["entries"][2]["kind"] == "future_kind"
    assert body["entries"][0] == {
        "sequence": 1,
        "author": "operator",
        "time": "2026-08-24T12:00:10Z",
        "audience": "public",
        "kind": "message",
        "content": "first",
        "content_ref": None,
    }


def test_entries_omit_malformed_oversized_temporary_and_ambiguous_content(
    tmp_path: Path,
) -> None:
    client, inbox = _client(tmp_path)
    artifact_dir, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    _write_entry(artifact_dir, "1.json", _entry(1, content="valid"))
    _write_entry(artifact_dir, "2.json", {"sequence": 2})
    _write_entry(
        artifact_dir,
        "3.json",
        _entry(3, content="both", content_ref={"path": "x"}),
    )
    _write_entry(artifact_dir, "4.json", _entry(4, content=None))
    _write_entry(
        artifact_dir,
        "5.json",
        _entry(5, content="x" * (MAX_ENTRY_CONTENT_BYTES + 1)),
    )
    oversized = _write_entry(artifact_dir, "6.json", _entry(6, content="oversized"))
    oversized.write_bytes(b" " * (MAX_ENTRY_BYTES + 1))
    _write_entry(artifact_dir, ".7.json.tmp", _entry(7, content="temporary"))
    _write_entry(artifact_dir, "wrong.json", _entry(8, content="wrong filename"))

    assert [entry["sequence"] for entry in _entries(client, "conversation").json()["entries"]] == [1]


def test_valid_content_ref_is_metadata_only(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    artifact_dir, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    content = b"redacted rationale"
    evidence = artifact_dir / "evidence"
    evidence.mkdir()
    (evidence / "rationale.txt").write_bytes(content)
    content_ref = {
        "path": "evidence/rationale.txt",
        "media_type": "text/plain",
        "byte_size": len(content),
        "content_digest": _digest(content),
    }
    _write_entry(artifact_dir, "1.json", _entry(1, content=None, content_ref=content_ref))

    entry = _entries(client, "conversation").json()["entries"][0]
    assert entry["content"] is None
    assert entry["content_ref"] == content_ref


@pytest.mark.parametrize("case", ["digest", "size", "traversal", "symlink"])
def test_invalid_content_refs_are_omitted(tmp_path: Path, case: str) -> None:
    client, inbox = _client(tmp_path)
    artifact_dir, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    content = b"reference"
    target = artifact_dir / "reference.txt"
    if case == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_bytes(content)
        target.symlink_to(outside)
    else:
        target.write_bytes(content)
    content_ref = {
        "path": "../reference.txt" if case == "traversal" else "reference.txt",
        "media_type": "text/plain",
        "byte_size": len(content) + (1 if case == "size" else 0),
        "content_digest": _digest(b"wrong" if case == "digest" else content),
    }
    _write_entry(artifact_dir, "1.json", _entry(1, content=None, content_ref=content_ref))
    assert _entries(client, "conversation").json()["entries"] == []


def test_entries_page_cursor_errors_and_resource_not_found(tmp_path: Path) -> None:
    client, inbox = _client(tmp_path)
    artifact_dir, _ = _publish(inbox, "conversation", kind="conversation", content=None)
    for sequence in (1, 2, 4):
        _write_entry(artifact_dir, f"{sequence}.json", _entry(sequence, content=str(sequence)))
    _publish(inbox, "text", content=b"text")

    first = _entries(client, "conversation", "?limit=2").json()
    assert [entry["sequence"] for entry in first["entries"]] == [1, 2]
    second = _entries(
        client, "conversation", f"?cursor={first['next_cursor']}&limit=2"
    ).json()
    assert [entry["sequence"] for entry in second["entries"]] == [4]

    invalid = _entries(client, "conversation", "?cursor=bad")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_cursor"
    for artifact_id in ("text", "missing"):
        response = _entries(client, artifact_id)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"
    unknown_run = client.get(
        "/api/v1/mission-runs/run-missing/artifacts/conversation/entries"
    )
    assert unknown_run.status_code == 404
    assert unknown_run.json()["error"]["code"] == "mission_run_not_found"
