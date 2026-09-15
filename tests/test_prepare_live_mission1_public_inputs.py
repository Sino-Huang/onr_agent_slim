from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from onr.adapters.file_transport import FileTransport
from onr.contracts.transport import TransportEvent


def test_materializes_time_zero_environment_and_ordinary_belief(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    transport = FileTransport(tmp_path / "transport")
    mission_id = "mission:test"
    environment = TransportEvent(
        1,
        "environment-data:mission:test:initial",
        mission_id,
        0,
        "environment_data",
        {
            "mission_id": mission_id,
            "mission_time_seconds": 0.0,
            "mission_epoch": "2026-09-15T00:00:00Z",
            "static_info": [
                {"report_id": "report-1", "entity_id": 1},
                {"report_id": "report-2", "entity_id": 2},
            ],
        },
    )
    transport.publish_event("environment-data", environment)
    transport.publish_event(
        "environment-updates",
        TransportEvent(
            1,
            "environment-update:mission:test:initial",
            mission_id,
            0,
            "environment-update",
            {"environment_event_id": environment.event_id},
        ),
    )
    output = tmp_path / "public-inputs"

    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/prepare_live_mission1_public_inputs.py"),
            "--transport-root",
            str(transport.root),
            "--mission-id",
            mission_id,
            "--output",
            str(output),
        ],
        cwd=repository,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((output / "environment.json").read_text()) == (
        environment.to_dict()["payload"]
    )
    belief = json.loads((output / "belief.json").read_text())
    assert belief["belief_revision"] == 1
    assert belief["input_revision"] == 0
    assert [ship["entity_id"] for ship in belief["ships"]] == [1, 2]
    assert len({ship["mean"] for ship in belief["ships"]}) == 1
