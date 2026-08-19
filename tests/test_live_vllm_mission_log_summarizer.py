from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from onr.runtime import RuntimeComposition


pytestmark = pytest.mark.live


def _runtime_with_temporary_roots(tmp_path: Path) -> RuntimeComposition:
    repository_root = Path(__file__).resolve().parents[1]
    source = repository_root / "conf" / "onr_agent_params.yaml"
    values = yaml.safe_load(source.read_text(encoding="utf-8"))
    values["transport"]["root"] = str(tmp_path / "transport")
    values["storage"]["root"] = str(tmp_path / "storage")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return RuntimeComposition.create(repo_root=repository_root, config_path=config_path)


def test_live_vllm_mission_log_summarizer_persists_a_heartbeat_chain(tmp_path: Path) -> None:
    runtime = _runtime_with_temporary_roots(tmp_path)
    runtime.verify_llm_reachability()
    mission_id = f"issue24-live-{uuid4().hex}"
    logger = runtime._logger()
    logger.emit(mission_id, "runtime", "heartbeat", "completed", details={"sequence": 1})

    summarizer = runtime.create_mission_log_summarizer()
    first = runtime.heartbeat(mission_id, summarizer=summarizer)
    assert first is not None

    logger.emit(mission_id, "runtime", "heartbeat", "completed", details={"sequence": 2})
    second = runtime.heartbeat(mission_id, summarizer=summarizer)
    assert second is not None
    assert second.prior_summary_ids == (first.summary_id,)
    assert runtime.heartbeat(mission_id, summarizer=summarizer) is None

    summary_dir = runtime.config.storage.root / "summaries" / mission_id
    summary_files = sorted(summary_dir.glob("[0-9]*.json"))
    assert len(summary_files) == 2
    assert summary_files[0].name == "00000000000000000001.json"
    assert summary_files[1].name == "00000000000000000002.json"
    assert (summary_dir / "cursor.json").is_file()
