"""Operator-view ``beliefs`` section over a trimmed ``run.a6CqxX`` run root.

The fixture holds the accepted Mission 1 run's reporting-reliability
snapshots for revisions 1-5 and a committed state at revision 5. Its state
file omits the bulky checkpoint (quadrature grids); the projection reads only
the committed ``snapshot``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot
from onr.runtime_host import beliefs
from onr.runtime_host.beliefs import HISTORY_LIMIT


def beliefs_section(run_root: Path, mission_id: str, mission_mode: str | None) -> Any:
    return beliefs.beliefs_section(run_root, mission_id, mission_mode)


FIXTURE = Path(__file__).parent / "fixtures" / "run_a6cqxx_belief_context"
MISSION = "mission:demo"
BELIEF_DIR = Path("agent-storage") / "bayesian-beliefs" / "mission%3Ademo"
STATE_FILE = "reporting-reliability-state-v1.json"


def _entity(section: dict, entity_id: str) -> dict:
    return next(item for item in section["entities"] if item["entity_id"] == entity_id)


def _committed(run_root: Path) -> ReportingReliabilitySnapshot:
    state = json.loads((run_root / BELIEF_DIR / STATE_FILE).read_text(encoding="utf-8"))
    return ReportingReliabilitySnapshot.from_dict(state["snapshot"])


def _next_revision(
    snapshot: ReportingReliabilitySnapshot, *, entity_id: int, mean: float
) -> ReportingReliabilitySnapshot:
    ships = tuple(
        dataclasses.replace(ship, mean=mean) if ship.entity_id == entity_id else ship
        for ship in snapshot.ships
    )
    return ReportingReliabilitySnapshot.create(
        mission_id=snapshot.mission_id,
        belief_revision=snapshot.belief_revision + 1,
        input_event_id=f"test-input:{snapshot.belief_revision + 1}",
        input_revision=snapshot.input_revision + 1,
        created_at=snapshot.created_at,
        ships=ships,
        omission=snapshot.omission,
    )


def _write(
    run_root: Path, snapshot: ReportingReliabilitySnapshot, *, commit: bool
) -> None:
    mission_root = run_root / BELIEF_DIR
    mission_root.mkdir(parents=True, exist_ok=True)
    artifact = mission_root / f"reporting-reliability-{snapshot.content_sha256}.json"
    artifact.write_text(snapshot.to_canonical_json() + "\n", encoding="utf-8")
    if commit:
        state = {"pending": None, "schema_version": 1, "snapshot": snapshot.to_dict()}
        (mission_root / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    shutil.copytree(FIXTURE, root)
    return root


def test_reporting_reliability_revisions_show_ship_2_rising() -> None:
    section = beliefs_section(FIXTURE, MISSION, "mission1")

    assert section["belief_kind"] == "reporting_reliability"
    assert section["reason"] is None
    assert section["revision"] == 5
    assert [item["revision"] for item in section["history"]] == [1, 2, 3, 4, 5]

    ship_2 = _entity(section, "2")
    assert [round(mean, 3) for mean in ship_2["means_by_revision"]] == [
        0.133,
        0.133,
        0.133,
        0.623,
        0.623,
    ]
    assert round(ship_2["mean"], 3) == 0.623
    assert ship_2["delta_since_prior"] == pytest.approx(0.623 - 0.133, abs=1e-3)
    assert ship_2["delta_since_previous"] == pytest.approx(0.0)
    assert ship_2["outcome_counts"] == {"altered": 1, "clean": 0, "omitted": 0}
    lower, upper = ship_2["credible_interval"]
    assert lower <= ship_2["mean"] <= upper
    # The most suspicious ship leads the table.
    assert section["entities"][0]["entity_id"] == "2"


def test_history_names_the_entity_each_revision_moved() -> None:
    history = beliefs_section(FIXTURE, MISSION, "mission1")["history"]

    assert history[0]["top_changes"] == []
    rise = history[3]
    assert rise["revision"] == 4
    assert [change["entity_id"] for change in rise["top_changes"]] == ["2"]
    assert rise["top_changes"][0]["delta"] == pytest.approx(0.49, abs=1e-2)
    # Revisions 2, 3 and 5 each cleared one ship after a clean check.
    assert [history[index]["top_changes"][0]["entity_id"] for index in (1, 2, 4)] == [
        "14",
        "10",
        "15",
    ]
    assert all(
        change["delta"] < 0
        for index in (1, 2, 4)
        for change in history[index]["top_changes"]
    )


def test_uncommitted_artifact_is_ignored_until_state_commits_it(run_root: Path) -> None:
    successor = _next_revision(_committed(run_root), entity_id=3, mean=0.5)
    _write(run_root, successor, commit=False)

    pending = beliefs_section(run_root, MISSION, "mission1")
    assert pending["revision"] == 5
    assert _entity(pending, "3")["mean"] == pytest.approx(0.133, abs=1e-3)

    _write(run_root, successor, commit=True)
    committed = beliefs_section(run_root, MISSION, "mission1")
    assert committed["revision"] == 6
    ship_3 = _entity(committed, "3")
    assert ship_3["mean"] == 0.5
    assert ship_3["delta_since_previous"] == pytest.approx(0.5 - 0.133, abs=1e-3)
    assert committed["history"][-1]["top_changes"] == [
        {"entity_id": "3", "mean": 0.5, "delta": ship_3["delta_since_previous"]}
    ]


def test_history_window_is_bounded_but_prior_delta_keeps_revision_1(
    run_root: Path,
) -> None:
    snapshot = _committed(run_root)
    for step in range(HISTORY_LIMIT + 5):
        snapshot = _next_revision(snapshot, entity_id=7, mean=0.2 + 0.01 * step)
        _write(run_root, snapshot, commit=True)

    section = beliefs_section(run_root, MISSION, "mission1")
    latest = snapshot.belief_revision
    assert section["revision"] == latest
    assert [item["revision"] for item in section["history"]] == list(
        range(latest - HISTORY_LIMIT + 1, latest + 1)
    )
    ship_7 = _entity(section, "7")
    assert len(ship_7["means_by_revision"]) == HISTORY_LIMIT
    assert ship_7["means_by_revision"][-1] == ship_7["mean"]
    assert ship_7["delta_since_prior"] == pytest.approx(
        ship_7["mean"] - 0.133, abs=1e-3
    )


@pytest.mark.parametrize("mission_mode", ["mission2", "mission3", "mission4"])
def test_modes_without_belief_service_report_explicit_none(
    tmp_path: Path, mission_mode: str
) -> None:
    section = beliefs_section(tmp_path, "mission-0001", mission_mode)

    assert section == {
        "belief_kind": None,
        "reason": f"no Bayesian belief for this mission mode ({mission_mode})",
        "revision": None,
        "created_at": None,
        "entities": [],
        "history": [],
    }


def test_belief_mode_before_first_revision_is_not_called_unsupported(
    tmp_path: Path,
) -> None:
    section = beliefs_section(tmp_path, MISSION, "mission1")

    assert section["belief_kind"] is None
    assert section["reason"] == "no belief revision recorded yet"


def test_contract_examples_match_section_shape() -> None:
    contract = (
        Path(__file__).parents[1]
        / "docs"
        / "design"
        / "operator-console"
        / "contract"
        / "v1.2"
    )
    example = json.loads(
        (contract / "mission-run-operator-beliefs.response.json").read_text()
    )["beliefs"]
    none = json.loads(
        (contract / "mission-run-operator-beliefs.none.response.json").read_text()
    )["beliefs"]
    section = beliefs_section(FIXTURE, MISSION, "mission1")

    assert set(section) == set(example) == set(none)
    assert set(section["entities"][0]) == set(example["entities"][0])
    assert set(section["history"][3]) == set(example["history"][0])
    assert set(section["history"][3]["top_changes"][0]) == set(
        example["history"][0]["top_changes"][0]
    )
    assert none == beliefs_section(
        Path(__file__).parent / "no-such-run", "m", "mission2"
    )
