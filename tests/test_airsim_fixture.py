from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from onr.demo.airsim_reconstruction.fixture import (
    DEFAULT_LEAD_IN_SECONDS,
    DEFAULT_STATIC_MESHES_PATH,
    DEFAULT_VESSELS_DIR,
    SCENARIO_NAME,
    TRAJECTORY_NED_OFFSET_M,
    build_fixture,
)

SHIP_KEYS = {
    "id",
    "thermalId",
    "objectId",
    "type",
    "mesh",
    "passengers",
    "pose",
    "events",
    "name",
}
MESH_KEYS = {
    "MeshName",
    "MinBounds",
    "MaxBounds",
    "AssetPath",
    "type",
    "attributes",
    "weight",
}
PASSENGER_KEYS = {"mesh", "name", "objectId", "pose"}
PASSENGER_MESH_KEYS = {"MeshName", "AssetPath", "MinBounds", "MaxBounds"}
LEAD_IN_ROWS = int(DEFAULT_LEAD_IN_SECONDS / 0.5)


def _load(path: Path) -> dict[str, object] | list[object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _md5_name(ship: dict[str, object]) -> str:
    without_name = {key: value for key, value in ship.items() if key != "name"}
    digest = hashlib.md5(
        json.dumps(without_name, sort_keys=True).encode()
    ).hexdigest()
    mesh = ship["mesh"]
    assert isinstance(mesh, dict)
    return f"{ship['id']}_{mesh['MeshName']}_{digest}"


def _scenario_ships(out_dir: Path) -> Path:
    return out_dir / "scenarios" / SCENARIO_NAME / "ships"


def _assert_canonical_window(
    output_pose: list[object], source_pose: list[object]
) -> None:
    assert len(output_pose) == LEAD_IN_ROWS + 600
    canonical = output_pose[LEAD_IN_ROWS:]
    for output_row, source_row in zip(canonical, source_pose, strict=True):
        expected_position = [
            source_row[axis] + TRAJECTORY_NED_OFFSET_M[axis] * 100.0
            for axis in range(3)
        ]
        assert output_row[:3] == pytest.approx(expected_position)
        assert output_row[3] == source_row[3]
        assert output_row[4] == DEFAULT_LEAD_IN_SECONDS + source_row[4]


def _build(tmp_path: Path, name: str = "fixture") -> Path:
    out_dir = tmp_path / name
    result = build_fixture(DEFAULT_VESSELS_DIR, DEFAULT_STATIC_MESHES_PATH, out_dir)
    assert result.ship_count == 20
    assert result.passenger_count == 26
    return out_dir


def test_build_fixture_ship_schema_mapping_manifest_and_determinism(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path, "first")
    second = _build(tmp_path, "second")
    first_ships_dir = _scenario_ships(first)
    second_ships_dir = _scenario_ships(second)
    ship_paths = sorted(
        (path for path in first_ships_dir.glob("*.json") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )
    assert [int(path.stem) for path in ship_paths] == list(range(1, 21))

    mapping = _load(first / "mapping.json")
    manifest = _load(first / "manifest.json")
    assert isinstance(mapping, dict)
    assert isinstance(manifest, dict)
    assert mapping["scenario_name"] == SCENARIO_NAME
    object_ids: set[int] = set()
    names: set[str] = set()
    distinct_source_meshes: set[str] = set()

    for path in ship_paths:
        ship_id = int(path.stem)
        ship = _load(path)
        source = _load(DEFAULT_VESSELS_DIR / path.name)
        assert isinstance(ship, dict)
        assert isinstance(source, dict)
        assert set(ship) == SHIP_KEYS
        assert ship["type"] == "Boat"
        assert isinstance(ship["pose"], list)
        assert isinstance(source["pose"], list)
        _assert_canonical_window(ship["pose"], source["pose"])
        assert len(ship["pose"]) == 620
        assert all(row[4] == 0.5 * index for index, row in enumerate(ship["pose"]))
        assert all(row[2] == -250.0 for row in ship["pose"])
        assert ship["passengers"] == source["passengers"]
        assert isinstance(ship["events"], list)
        assert isinstance(source["events"], list)
        for output_event, source_event in zip(
            ship["events"], source["events"], strict=True
        ):
            expected_event = dict(source_event)
            if isinstance(expected_event.get("time"), (int, float)) and not isinstance(
                expected_event.get("time"), bool
            ):
                expected_event["time"] += DEFAULT_LEAD_IN_SECONDS
            assert output_event == expected_event
        assert ship["id"] == source["id"] == ship_id
        assert ship["thermalId"] == source["thermalId"]
        assert ship["objectId"] == source["objectId"]
        assert isinstance(ship["objectId"], int)
        object_ids.add(ship["objectId"])

        mesh = ship["mesh"]
        source_mesh = source["mesh"]
        assert isinstance(mesh, dict)
        assert isinstance(source_mesh, dict)
        assert set(mesh) == MESH_KEYS
        assert mesh["attributes"] == []
        assert mesh["weight"] == source_mesh.get("weight", 1.0)
        distinct_source_meshes.add(str(source_mesh["Mesh"]))
        for bounds_name in ("MinBounds", "MaxBounds"):
            bounds = mesh[bounds_name]
            assert isinstance(bounds, dict)
            assert set(bounds) == {"X", "Y", "Z"}
            assert all(isinstance(bounds[axis], float) for axis in ("X", "Y", "Z"))

        assert isinstance(ship["name"], str)
        assert ship["name"] == _md5_name(ship)
        names.add(ship["name"])
        mapped = mapping["ships"][str(ship_id)]
        assert mapped == {
            "name": ship["name"],
            "object_id": ship["objectId"],
            "thermal_id": ship["thermalId"],
            "mesh_name": mesh["MeshName"],
            "asset_path": mesh["AssetPath"],
        }
        assert path.read_bytes() == (second_ships_dir / path.name).read_bytes()

    assert object_ids == set(range(1, 21))
    assert len(names) == 20
    assert len(distinct_source_meshes) == 10
    for ship_id in (1, 2, 3, 10, 11):
        assert manifest["ship_motion_max_displacement_m"][str(ship_id)] > 1.0

    expected_source_hashes = {
        f"{ship_id}.json": _sha256(DEFAULT_VESSELS_DIR / f"{ship_id}.json")
        for ship_id in range(1, 21)
    }
    assert manifest["source_sha256"] == expected_source_hashes
    assert manifest["lead_in_seconds"] == DEFAULT_LEAD_IN_SECONDS
    assert manifest["lead_in_note"] == (
        "rows with t < lead_in_seconds are synthetic (ships: linear backward "
        "extrapolation; passengers: held first pose); canonical trajectory positions "
        "include trajectory_ned_offset at t >= lead_in_seconds"
    )
    assert manifest["canonical_params"] == {
        "seed": 5,
        "initial_ned": [0, 0, -25],
        "heading_grid": 2,
        "grid_cell_m": 10,
        "partition_cells": 200,
        "tick_seconds": 0.5,
        "trajectory_ned_offset": [253.7, 45.5, 0],
        "sensing": "world_model",
        "range_m": 300,
    }
    for relative_path, digest in manifest["outputs"].items():
        assert _sha256(first / relative_path) == digest


def test_passengers_are_verbatim_unique_and_cross_referenced(tmp_path: Path) -> None:
    out_dir = _build(tmp_path)
    ships_dir = _scenario_ships(out_dir)
    output_passengers_dir = ships_dir / "passengers"
    passenger_paths = sorted(output_passengers_dir.glob("*.json"))
    source_paths = sorted((DEFAULT_VESSELS_DIR / "passengers").glob("*.json"))
    assert len(passenger_paths) == len(source_paths) == 26

    mapping = _load(out_dir / "mapping.json")
    manifest = _load(out_dir / "manifest.json")
    assert isinstance(mapping, dict)
    assert isinstance(manifest, dict)
    references: dict[str, tuple[int, dict[str, object]]] = {}
    all_object_ids = set(range(1, 21))
    expected_counts: dict[str, int] = {}
    for ship_id in range(1, 21):
        ship = _load(ships_dir / f"{ship_id}.json")
        assert isinstance(ship, dict)
        passengers = ship["passengers"]
        assert isinstance(passengers, list)
        expected_counts[str(ship_id)] = len(passengers)
        for passenger in passengers:
            assert isinstance(passenger, dict)
            assert set(passenger) == PASSENGER_KEYS
            assert isinstance(passenger["mesh"], dict)
            assert set(passenger["mesh"]) == PASSENGER_MESH_KEYS
            assert set(passenger["pose"]) == {"position", "rotation"}
            name = passenger["name"]
            assert isinstance(name, str)
            assert name not in references
            references[name] = (ship_id, passenger)

    assert manifest["passengers_per_ship"] == expected_counts
    passenger_ids: set[int] = set()
    for source_path, output_path in zip(source_paths, passenger_paths, strict=True):
        assert output_path.name == source_path.name
        passenger = _load(output_path)
        source_passenger = _load(source_path)
        assert isinstance(passenger, dict)
        assert isinstance(source_passenger, dict)
        assert set(passenger) == PASSENGER_KEYS
        assert isinstance(passenger["mesh"], dict)
        assert set(passenger["mesh"]) == PASSENGER_MESH_KEYS
        assert len(passenger["pose"]) == 620
        assert isinstance(source_passenger["pose"], list)
        _assert_canonical_window(passenger["pose"], source_passenger["pose"])
        first_source_row = source_passenger["pose"][0]
        expected_first = [
            first_source_row[axis] + TRAJECTORY_NED_OFFSET_M[axis] * 100.0
            for axis in range(3)
        ]
        assert all(
            row[:3] == pytest.approx(expected_first)
            and row[3] == first_source_row[3]
            for row in passenger["pose"][:LEAD_IN_ROWS]
        )
        for index, row in enumerate(passenger["pose"]):
            assert len(row) == 5
            assert row[4] == 0.5 * index
            assert all(math.isfinite(value) for value in row)
        name = passenger["name"]
        object_id = passenger["objectId"]
        assert isinstance(name, str)
        assert isinstance(object_id, int)
        assert name in references
        owner_id, embedded = references[name]
        assert embedded["objectId"] == object_id
        assert embedded["mesh"] == passenger["mesh"]
        assert mapping["passengers"][str(object_id)] == {
            "name": name,
            "object_id": object_id,
            "ship_id": str(owner_id),
        }
        passenger_ids.add(object_id)
        all_object_ids.add(object_id)

    assert passenger_ids == set(range(21, 47))
    assert all_object_ids == set(range(1, 47))
    assert len(references) == 26


def test_lead_in_trajectory_seam_and_canonical_events(tmp_path: Path) -> None:
    out_dir = _build(tmp_path)
    ships_dir = _scenario_ships(out_dir)
    ship = _load(ships_dir / "1.json")
    source = _load(DEFAULT_VESSELS_DIR / "1.json")
    assert isinstance(ship, dict)
    assert isinstance(source, dict)
    output_pose = ship["pose"]
    source_pose = source["pose"]
    assert isinstance(output_pose, list)
    assert isinstance(source_pose, list)
    velocity = tuple(
        (source_pose[4][axis] - source_pose[0][axis]) / 2.0
        for axis in range(3)
    )
    offset_cm = tuple(value * 100.0 for value in TRAJECTORY_NED_OFFSET_M)
    for index, output_row in enumerate(output_pose[:LEAD_IN_ROWS]):
        expected_position = [
            source_pose[0][axis]
            + offset_cm[axis]
            + velocity[axis] * (0.5 * index - DEFAULT_LEAD_IN_SECONDS)
            for axis in range(3)
        ]
        assert output_row[:3] == pytest.approx(expected_position)
        assert output_row[3] == source_pose[0][3]
    expected_seam_distance = math.sqrt(sum(value * value for value in velocity)) * 0.5
    actual_seam_distance = math.dist(
        output_pose[LEAD_IN_ROWS - 1][:3], output_pose[LEAD_IN_ROWS][:3]
    )
    assert actual_seam_distance == pytest.approx(expected_seam_distance, abs=1e-6)

    flattened = _load(ships_dir / "events.json")
    assert isinstance(flattened, list)
    expected_flattened = []
    for ship_id in range(1, 21):
        source_ship = _load(DEFAULT_VESSELS_DIR / f"{ship_id}.json")
        assert isinstance(source_ship, dict)
        for source_event in source_ship["events"]:
            event = dict(source_event)
            event.setdefault("entity_id", ship_id)
            expected_flattened.append(event)
    expected_flattened.sort(key=lambda event: event.get("time", 0.0))
    assert flattened == expected_flattened


def test_first_canonical_row_has_world_model_ned_offset(tmp_path: Path) -> None:
    out_dir = _build(tmp_path)
    ship = _load(_scenario_ships(out_dir) / "1.json")
    source = _load(DEFAULT_VESSELS_DIR / "1.json")
    assert isinstance(ship, dict)
    assert isinstance(source, dict)

    output_row = ship["pose"][LEAD_IN_ROWS]
    source_row = source["pose"][0]

    assert output_row[:2] == pytest.approx(
        [source_row[0] + 25_370.0, source_row[1] + 4_550.0]
    )
    assert output_row[2] == source_row[2] == -250.0
    assert output_row[3] == source_row[3]


def test_required_ship_stationary_initial_velocity_is_rejected(
    tmp_path: Path,
) -> None:
    vessels_dir = tmp_path / "vessels"
    vessels_dir.mkdir()
    for ship_id in range(1, 21):
        source_path = DEFAULT_VESSELS_DIR / f"{ship_id}.json"
        output_path = vessels_dir / source_path.name
        if ship_id == 1:
            vessel = _load(source_path)
            assert isinstance(vessel, dict)
            pose = vessel["pose"]
            assert isinstance(pose, list)
            for index in range(1, 5):
                pose[index][:3] = pose[0][:3]
            output_path.write_text(json.dumps(vessel), encoding="utf-8")
        else:
            output_path.symlink_to(source_path)
    (vessels_dir / "passengers").symlink_to(
        DEFAULT_VESSELS_DIR / "passengers", target_is_directory=True
    )

    with pytest.raises(
        ValueError,
        match=r"Ship 1 initial speed is 0\.000000 cm/s .*v0=\(0\.0, 0\.0, 0\.0\)",
    ):
        build_fixture(vessels_dir, DEFAULT_STATIC_MESHES_PATH, tmp_path / "out")


def test_unknown_mesh_has_clear_error(tmp_path: Path) -> None:
    vessels_dir = tmp_path / "vessels"
    vessels_dir.mkdir()
    for ship_id in range(1, 21):
        source = DEFAULT_VESSELS_DIR / f"{ship_id}.json"
        destination = vessels_dir / source.name
        if ship_id == 1:
            vessel = _load(source)
            assert isinstance(vessel, dict)
            assert isinstance(vessel["mesh"], dict)
            vessel["mesh"]["Mesh"] = "Unknown_Test_Mesh"
            destination.write_text(json.dumps(vessel), encoding="utf-8")
        else:
            destination.symlink_to(source)
    (vessels_dir / "passengers").symlink_to(
        DEFAULT_VESSELS_DIR / "passengers", target_is_directory=True
    )

    with pytest.raises(ValueError, match="Unknown boat mesh 'Unknown_Test_Mesh'"):
        build_fixture(vessels_dir, DEFAULT_STATIC_MESHES_PATH, tmp_path / "out")
