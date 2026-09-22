"""Build a deterministic AirSim scenario fixture from the recorded Mission 1 fleet."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_VESSELS_DIR = Path(
    "/data/ccu/sukaih/ONR/onr_physical_runtime/data/harbor_world/vessels"
)
DEFAULT_STATIC_MESHES_PATH = Path(
    "/data/ccu/sukaih/ONR/onr_env/Linux/Harbor5_6/"
    "EnvironmentResourceFiles/StaticMeshes.json"
)
DEFAULT_OUT_DIR = Path("var/demo-video/mission1-20260916-airsim/fixture")
DEFAULT_LEAD_IN_SECONDS = 10.0
SCENARIO_NAME = "mission1-20260916"
EXPECTED_SHIP_IDS = tuple(range(1, 21))
EXPECTED_PASSENGER_IDS = set(range(21, 47))

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
SHIP_MESH_KEYS = {
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

# Meshes that never render into the instance segmentation pass, mapped to the
# nearest seg-proven catalog mesh (same category). See _resolved_mesh.
_SEG_PROVEN_MESH_SUBSTITUTIONS = {
    "Fishing_trawler": "Fishing_boat_2",
    "Fishing_boat": "Fishing_boat_2",
    "Tug_boat": "Fishing_boat_2",
    "SM_SpeedBoat": "SM_SpeedBoat_2_Black_Trim",
    "SM_Boat_Defense_1": "SM_Boat_Defense_3",
}
BOUND_KEYS = {"X", "Y", "Z"}

CANONICAL_PARAMS: dict[str, object] = {
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
TRAJECTORY_NED_OFFSET_M = tuple(
    float(value) for value in CANONICAL_PARAMS["trajectory_ned_offset"]
)
LEAD_IN_NOTE = (
    "rows with t < lead_in_seconds are synthetic (ships: linear backward "
    "extrapolation; passengers: held first pose); canonical trajectory positions "
    "include trajectory_ned_offset at t >= lead_in_seconds"
)


@dataclass(frozen=True, slots=True)
class FixtureResult:
    """Summary of a successfully built reconstruction fixture."""

    out_dir: Path
    scenario_dir: Path
    ship_count: int
    passenger_count: int
    output_count: int


@dataclass(frozen=True, slots=True)
class _MeshCatalogEntry:
    mesh_name: str
    min_bounds: dict[str, float]
    max_bounds: dict[str, float]
    asset_path: str
    mesh_type: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} has keys {sorted(actual)}, expected exactly {sorted(expected)}"
        )


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _require_finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _validate_bounds(value: object, label: str, *, require_floats: bool) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    _require_exact_keys(value, BOUND_KEYS, label)
    for axis in sorted(BOUND_KEYS):
        coordinate = value[axis]
        if require_floats and not isinstance(coordinate, float):
            raise ValueError(f"{label}.{axis} must be a float")
        _require_finite_number(coordinate, f"{label}.{axis}")


def _validate_trajectory(
    pose: object,
    label: str,
    *,
    expected_z: float | None,
) -> list[list[Any]]:
    if not isinstance(pose, list) or len(pose) != 600:
        actual = len(pose) if isinstance(pose, list) else type(pose).__name__
        raise ValueError(f"{label} must contain exactly 600 rows, got {actual}")
    for index, row in enumerate(pose):
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError(f"{label}[{index}] must be [x, y, z, heading, time]")
        for column, value in enumerate(row):
            _require_finite_number(value, f"{label}[{index}][{column}]")
        expected_time = 0.5 * index
        if row[4] != expected_time:
            raise ValueError(
                f"{label}[{index}] time is {row[4]!r}, expected {expected_time!r}"
            )
        if expected_z is not None and row[2] != expected_z:
            raise ValueError(
                f"{label}[{index}] z is {row[2]!r}, expected {expected_z!r}"
            )
    return pose


def _maximum_xy_displacement_m(pose: Sequence[Sequence[object]]) -> float:
    maximum_squared_cm = 0.0
    for index, first in enumerate(pose[:-1]):
        first_x = float(first[0])
        first_y = float(first[1])
        for second in pose[index + 1 :]:
            delta_x = first_x - float(second[0])
            delta_y = first_y - float(second[1])
            maximum_squared_cm = max(
                maximum_squared_cm,
                delta_x * delta_x + delta_y * delta_y,
            )
    return math.sqrt(maximum_squared_cm) / 100.0


def _trajectory_with_ned_offset(
    pose: Sequence[Sequence[Any]],
) -> list[list[Any]]:
    """Translate world-frame trajectory positions from generator to NED coordinates."""

    offset_cm = tuple(value * 100.0 for value in TRAJECTORY_NED_OFFSET_M)
    return [
        [
            float(row[0]) + offset_cm[0],
            float(row[1]) + offset_cm[1],
            float(row[2]) + offset_cm[2],
            *copy.deepcopy(row[3:]),
        ]
        for row in pose
    ]


def _lead_in_row_count(lead_in_s: float) -> int:
    lead_in = _require_finite_number(lead_in_s, "lead_in_s")
    if lead_in < 0:
        raise ValueError("lead_in_s must be non-negative")
    row_count = round(lead_in / 0.5)
    if not math.isclose(lead_in, row_count * 0.5, abs_tol=1e-9):
        raise ValueError("lead_in_s must be an exact multiple of 0.5 seconds")
    return row_count


def _initial_velocity_cm_s(pose: Sequence[Sequence[object]]) -> tuple[float, float, float]:
    return tuple(
        (_require_finite_number(pose[4][axis], f"pose[4][{axis}]")
         - _require_finite_number(pose[0][axis], f"pose[0][{axis}]"))
        / 2.0
        for axis in range(3)
    )


def _validate_output_trajectory(
    pose: list[list[Any]],
    label: str,
    *,
    lead_in_rows: int,
    expected_z: float | None,
) -> None:
    expected_length = 600 + lead_in_rows
    if len(pose) != expected_length:
        raise ValueError(
            f"{label} must contain exactly {expected_length} rows, got {len(pose)}"
        )
    for index, row in enumerate(pose):
        if len(row) != 5:
            raise ValueError(f"{label}[{index}] must be [x, y, z, heading, time]")
        for column, value in enumerate(row):
            _require_finite_number(value, f"{label}[{index}][{column}]")
        expected_time = 0.5 * index
        if row[4] != expected_time:
            raise ValueError(
                f"{label}[{index}] time is {row[4]!r}, expected {expected_time!r}"
            )
        if expected_z is not None and row[2] != expected_z:
            raise ValueError(
                f"{label}[{index}] z is {row[2]!r}, expected {expected_z!r}"
            )


def _ship_pose_with_lead_in(
    pose: list[list[Any]], ship_id: int, lead_in_s: float, lead_in_rows: int
) -> tuple[list[list[Any]], tuple[float, float, float]]:
    velocity = _initial_velocity_cm_s(pose)
    if ship_id in (1, 10, 11):
        speed = math.sqrt(sum(component * component for component in velocity))
        if speed < 100.0:
            raise ValueError(
                f"Ship {ship_id} initial speed is {speed:.6f} cm/s (< 100 cm/s); "
                f"v0={velocity}"
            )
    first = pose[0]
    output = [
        [
            float(first[axis]) + velocity[axis] * (0.5 * index - lead_in_s)
            for axis in range(3)
        ]
        + [first[3], 0.5 * index]
        for index in range(lead_in_rows)
    ]
    output.extend([*row[:4], lead_in_s + float(row[4])] for row in pose)
    _validate_output_trajectory(
        output,
        f"Ship {ship_id} output pose",
        lead_in_rows=lead_in_rows,
        expected_z=-250.0,
    )
    return output, velocity


def _passenger_pose_with_lead_in(
    pose: list[list[Any]], name: str, lead_in_s: float, lead_in_rows: int
) -> list[list[Any]]:
    first = pose[0]
    output = [
        [*copy.deepcopy(first[:4]), 0.5 * index]
        for index in range(lead_in_rows)
    ]
    output.extend([*row[:4], lead_in_s + float(row[4])] for row in pose)
    _validate_output_trajectory(
        output,
        f"Passenger {name} output pose",
        lead_in_rows=lead_in_rows,
        expected_z=None,
    )
    return output


def _events_with_shifted_times(
    events: Sequence[Mapping[str, Any]], lead_in_s: float
) -> list[dict[str, Any]]:
    shifted = copy.deepcopy(events)
    for event in shifted:
        event_time = event.get("time")
        if isinstance(event_time, (int, float)) and not isinstance(event_time, bool):
            event["time"] = event_time + lead_in_s
    return shifted


def _subcategory_type(subcategory: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", subcategory).lower()


def _load_mesh_catalog(path: Path) -> dict[str, _MeshCatalogEntry]:
    catalog = _read_json(path)
    assets = catalog.get("Assets")
    if not isinstance(assets, dict):
        raise TypeError(f"Static mesh catalog {path} has no Assets object")
    boats = assets.get("Boat")
    if not isinstance(boats, dict):
        raise TypeError(f"Static mesh catalog {path} has no Boat category")

    entries: dict[str, _MeshCatalogEntry] = {}
    for subcategory, group in boats.items():
        if subcategory == "AssetNames":
            continue
        if not isinstance(subcategory, str) or not isinstance(group, dict):
            raise TypeError(f"Invalid Boat subcategory in {path}")
        asset_names = group.get("AssetNames")
        if not isinstance(asset_names, list):
            raise TypeError(f"Boat/{subcategory} has no AssetNames list in {path}")
        for index, asset in enumerate(asset_names):
            if not isinstance(asset, dict):
                raise TypeError(
                    f"Boat/{subcategory}/AssetNames[{index}] is invalid"
                )
            mesh_name = asset.get("MeshName")
            if not isinstance(mesh_name, str) or not mesh_name:
                raise ValueError(
                    f"Boat/{subcategory}/AssetNames[{index}] has no MeshName"
                )
            bare_name = mesh_name.split(".", 1)[0]
            if bare_name in entries:
                raise ValueError(f"Ambiguous boat mesh {bare_name!r} in {path}")
            min_bounds = asset.get("MinBounds")
            max_bounds = asset.get("MaxBounds")
            _validate_bounds(min_bounds, f"catalog mesh {mesh_name} MinBounds", require_floats=False)
            _validate_bounds(max_bounds, f"catalog mesh {mesh_name} MaxBounds", require_floats=False)
            assert isinstance(min_bounds, dict)
            assert isinstance(max_bounds, dict)
            entries[bare_name] = _MeshCatalogEntry(
                mesh_name=mesh_name,
                min_bounds={axis: float(min_bounds[axis]) for axis in ("X", "Y", "Z")},
                max_bounds={axis: float(max_bounds[axis]) for axis in ("X", "Y", "Z")},
                asset_path=f"Boat/{subcategory}/{mesh_name}",
                mesh_type=_subcategory_type(subcategory),
            )
    return entries


def _resolved_mesh(
    source_mesh: object,
    catalog: Mapping[str, _MeshCatalogEntry],
    ship_id: int,
) -> dict[str, object]:
    if not isinstance(source_mesh, dict):
        raise TypeError(f"Ship {ship_id} mesh must be an object")
    bare_name = source_mesh.get("Mesh")
    if not isinstance(bare_name, str):
        raise TypeError(f"Ship {ship_id} source mesh has no string Mesh name")
    # Segmentation-blind meshes never render into the engine's instance
    # segmentation pass (verified across the full 600-tick capture on
    # 2026-09-18: Fishing_trawler / Fishing_boat / Tug_boat / SM_SpeedBoat /
    # SM_Boat_Defense_1 produced zero seg pixels mission-wide while the _2/_3
    # variants rendered consistently). Substitute the nearest seg-proven mesh
    # in the same category; trajectories and identity are unaffected.
    bare_name = _SEG_PROVEN_MESH_SUBSTITUTIONS.get(bare_name, bare_name)
    try:
        entry = catalog[bare_name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown boat mesh {bare_name!r} for ship {ship_id}; "
            "no exact bare-name match exists in StaticMeshes.json"
        ) from exc
    weight = _require_finite_number(
        source_mesh.get("weight", 1.0), f"Ship {ship_id} mesh weight"
    )
    mesh: dict[str, object] = {
        "MeshName": entry.mesh_name,
        "MinBounds": copy.deepcopy(entry.min_bounds),
        "MaxBounds": copy.deepcopy(entry.max_bounds),
        "AssetPath": entry.asset_path,
        "type": entry.mesh_type,
        "attributes": [],
        "weight": weight,
    }
    _require_exact_keys(mesh, SHIP_MESH_KEYS, f"Ship {ship_id} converted mesh")
    _validate_bounds(mesh["MinBounds"], f"Ship {ship_id} MinBounds", require_floats=True)
    _validate_bounds(mesh["MaxBounds"], f"Ship {ship_id} MaxBounds", require_floats=True)
    return mesh


def _resolved_static_mesh(
    mesh_name: str, static_meshes_path: Path, label: str
) -> dict[str, object]:
    """Resolve a stationary actor mesh from its own catalog category."""

    catalog = _read_json(static_meshes_path)
    assets = catalog.get("Assets")
    if not isinstance(assets, dict):
        raise TypeError(f"Static mesh catalog {static_meshes_path} has no Assets object")
    for category, group in assets.items():
        if not isinstance(group, dict):
            continue
        for asset in group.get("AssetNames") or []:
            if not isinstance(asset, dict) or asset.get("MeshName") != mesh_name:
                continue
            min_bounds = asset.get("MinBounds")
            max_bounds = asset.get("MaxBounds")
            _validate_bounds(
                min_bounds, f"static mesh {mesh_name} MinBounds", require_floats=False
            )
            _validate_bounds(
                max_bounds, f"static mesh {mesh_name} MaxBounds", require_floats=False
            )
            assert isinstance(min_bounds, dict)
            assert isinstance(max_bounds, dict)
            mesh: dict[str, object] = {
                "MeshName": mesh_name,
                "MinBounds": {
                    axis: float(min_bounds[axis]) for axis in ("X", "Y", "Z")
                },
                "MaxBounds": {
                    axis: float(max_bounds[axis]) for axis in ("X", "Y", "Z")
                },
                "AssetPath": f"{category}/{mesh_name}",
            }
            _require_exact_keys(mesh, PASSENGER_MESH_KEYS, f"Static {label} mesh")
            return mesh
    raise ValueError(
        f"Static {label} mesh {mesh_name!r} not found in "
        f"{static_meshes_path}"
    )


def _validate_embedded_passenger(passenger: object, ship_id: int, index: int) -> dict[str, Any]:
    label = f"Ship {ship_id} passenger[{index}]"
    if not isinstance(passenger, dict):
        raise TypeError(f"{label} must be an object")
    _require_exact_keys(passenger, PASSENGER_KEYS, label)
    name = passenger["name"]
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label}.name must be a non-empty string")
    _require_int(passenger["objectId"], f"{label}.objectId")
    mesh = passenger["mesh"]
    if not isinstance(mesh, dict):
        raise TypeError(f"{label}.mesh must be an object")
    _require_exact_keys(mesh, PASSENGER_MESH_KEYS, f"{label}.mesh")
    if not isinstance(mesh["MeshName"], str) or not isinstance(mesh["AssetPath"], str):
        raise TypeError(f"{label}.mesh names must be strings")
    _validate_bounds(mesh["MinBounds"], f"{label}.mesh.MinBounds", require_floats=False)
    _validate_bounds(mesh["MaxBounds"], f"{label}.mesh.MaxBounds", require_floats=False)
    pose = passenger["pose"]
    if not isinstance(pose, dict):
        raise TypeError(f"{label}.pose must be an object")
    _require_exact_keys(pose, {"position", "rotation"}, f"{label}.pose")
    for component in ("position", "rotation"):
        values = pose[component]
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError(f"{label}.pose.{component} must contain three numbers")
        for value_index, value in enumerate(values):
            _require_finite_number(
                value, f"{label}.pose.{component}[{value_index}]"
            )
    return passenger


def _validate_passenger_file(value: dict[str, Any], path: Path) -> None:
    label = f"Passenger file {path.name}"
    _require_exact_keys(value, PASSENGER_KEYS, label)
    name = value["name"]
    if not isinstance(name, str) or path.stem != name:
        raise ValueError(f"{label} name must match its filename")
    _require_int(value["objectId"], f"{label}.objectId")
    mesh = value["mesh"]
    if not isinstance(mesh, dict):
        raise TypeError(f"{label}.mesh must be an object")
    _require_exact_keys(mesh, PASSENGER_MESH_KEYS, f"{label}.mesh")
    _validate_bounds(mesh["MinBounds"], f"{label}.mesh.MinBounds", require_floats=False)
    _validate_bounds(mesh["MaxBounds"], f"{label}.mesh.MaxBounds", require_floats=False)
    _validate_trajectory(value["pose"], f"{label}.pose", expected_z=None)


def _ship_source_paths(vessels_dir: Path) -> list[Path]:
    paths = sorted(
        (path for path in vessels_dir.glob("*.json") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )
    file_ids = tuple(int(path.stem) for path in paths)
    if file_ids != EXPECTED_SHIP_IDS:
        raise ValueError(
            f"Expected vessel files 1.json through 20.json in {vessels_dir}, "
            f"found IDs {list(file_ids)}"
        )
    return paths


def build_fixture(
    vessels_dir: str | Path,
    static_meshes_path: str | Path,
    out_dir: str | Path,
    lead_in_s: float = DEFAULT_LEAD_IN_SECONDS,
    *,
    scenario_name: str = SCENARIO_NAME,
    static_objects: Sequence[Mapping[str, Any]] = (),
) -> FixtureResult:
    """Build and validated recorded AirSim reconstruction fixture.

    ``static_objects`` adds stationary scenario actors (for example the
    Mission 4 fixture containers) as scenario passengers with constant
    absolute trajectories and stable segmentation object IDs.
    """

    vessels_path = Path(vessels_dir)
    meshes_path = Path(static_meshes_path)
    output_path = Path(out_dir)
    if output_path.exists():
        raise FileExistsError(f"Fixture output directory already exists: {output_path}")
    if not vessels_path.is_dir():
        raise ValueError(f"Vessels directory does not exist: {vessels_path}")
    if not meshes_path.is_file():
        raise ValueError(f"Static mesh catalog does not exist: {meshes_path}")
    lead_in_rows = _lead_in_row_count(lead_in_s)

    source_paths = _ship_source_paths(vessels_path)
    catalog = _load_mesh_catalog(meshes_path)
    source_sha256 = {path.name: _sha256(path) for path in source_paths}

    ships: list[dict[str, Any]] = []
    ship_object_ids: set[int] = set()
    passenger_owners: dict[str, tuple[int, dict[str, Any]]] = {}
    motion: dict[str, float] = {}
    passenger_counts: dict[str, int] = {}
    canonical_events: list[tuple[int, dict[str, Any]]] = []

    for source_path in source_paths:
        source = _read_json(source_path)
        ship_id = _require_int(source.get("id"), f"{source_path.name} id")
        if ship_id != int(source_path.stem):
            raise ValueError(
                f"{source_path.name} contains ship id {ship_id}, expected {source_path.stem}"
            )
        object_id = _require_int(
            source.get("objectId"), f"Ship {ship_id} objectId"
        )
        if not 1 <= object_id <= 0xFFFFFF:
            raise ValueError(f"Ship {ship_id} objectId {object_id} is outside 1..0xFFFFFF")
        if object_id in ship_object_ids:
            raise ValueError(f"Duplicate ship objectId {object_id}")
        ship_object_ids.add(object_id)

        pose = _validate_trajectory(
            source.get("pose"), f"Ship {ship_id} pose", expected_z=-250.0
        )
        pose = _trajectory_with_ned_offset(pose)
        displacement = _maximum_xy_displacement_m(pose)
        if displacement <= 1.0:
            raise ValueError(
                f"Ship {ship_id} is static: maximum XY displacement is "
                f"{displacement:.6f} m"
            )
        motion[str(ship_id)] = displacement
        output_pose, _initial_velocity = _ship_pose_with_lead_in(
            pose, ship_id, lead_in_s, lead_in_rows
        )

        source_passengers = source.get("passengers")
        if not isinstance(source_passengers, list):
            raise TypeError(f"Ship {ship_id} passengers must be a list")
        passengers: list[dict[str, Any]] = []
        for index, passenger_value in enumerate(source_passengers):
            passenger = _validate_embedded_passenger(
                passenger_value, ship_id, index
            )
            name = passenger["name"]
            if name in passenger_owners:
                previous_ship = passenger_owners[name][0]
                raise ValueError(
                    f"Passenger {name!r} is referenced by ships "
                    f"{previous_ship} and {ship_id}"
                )
            passenger_copy = copy.deepcopy(passenger)
            passenger_owners[name] = (ship_id, passenger_copy)
            passengers.append(passenger_copy)
        passenger_counts[str(ship_id)] = len(passengers)

        events = source.get("events")
        if not isinstance(events, list) or not all(
            isinstance(event, dict) for event in events
        ):
            raise ValueError(f"Ship {ship_id} events must be a list of objects")
        canonical_events.extend(
            (ship_id, copy.deepcopy(event)) for event in events
        )
        converted: dict[str, Any] = {
            "id": ship_id,
            "thermalId": source.get("thermalId"),
            "objectId": object_id,
            "type": "Boat",
            "mesh": _resolved_mesh(source.get("mesh"), catalog, ship_id),
            "passengers": passengers,
            "pose": output_pose,
            "events": _events_with_shifted_times(events, lead_in_s),
        }
        digest = hashlib.md5(
            json.dumps(converted, sort_keys=True).encode()
        ).hexdigest()
        converted["name"] = f"{ship_id}_{converted['mesh']['MeshName']}_{digest}"
        _require_exact_keys(converted, SHIP_KEYS, f"Converted ship {ship_id}")
        ships.append(converted)

    for required_ship_id in (1, 2, 3, 10, 11):
        if motion[str(required_ship_id)] <= 1.0:
            raise ValueError(f"Required moving ship {required_ship_id} is static")

    passenger_source_dir = vessels_path / "passengers"
    passenger_paths = sorted(passenger_source_dir.glob("*.json"))
    if len(passenger_paths) != 26:
        raise ValueError(
            f"Expected 26 passenger files in {passenger_source_dir}, "
            f"found {len(passenger_paths)}"
        )

    passenger_files: dict[str, tuple[Path, dict[str, Any]]] = {}
    passenger_object_ids: set[int] = set()
    for passenger_path in passenger_paths:
        passenger = _read_json(passenger_path)
        _validate_passenger_file(passenger, passenger_path)
        name = passenger["name"]
        object_id = passenger["objectId"]
        assert isinstance(name, str)
        assert isinstance(object_id, int)
        if name in passenger_files:
            raise ValueError(f"Duplicate passenger file name {name!r}")
        if object_id in passenger_object_ids:
            raise ValueError(f"Duplicate passenger objectId {object_id}")
        passenger_object_ids.add(object_id)
        passenger_files[name] = (passenger_path, passenger)

    if set(passenger_files) != set(passenger_owners):
        missing_files = sorted(set(passenger_owners) - set(passenger_files))
        unreferenced_files = sorted(set(passenger_files) - set(passenger_owners))
        raise ValueError(
            "Ship/passenger cross-reference mismatch: "
            f"missing files={missing_files}, unreferenced files={unreferenced_files}"
        )
    if passenger_object_ids != EXPECTED_PASSENGER_IDS:
        raise ValueError(
            "Passenger objectIds must be exactly 21 through 46, got "
            f"{sorted(passenger_object_ids)}"
        )
    collisions = ship_object_ids & passenger_object_ids
    if collisions:
        raise ValueError(f"ObjectId collision across ships and passengers: {sorted(collisions)}")

    for name, (_path, passenger) in passenger_files.items():
        ship_id, embedded = passenger_owners[name]
        if passenger["objectId"] != embedded["objectId"]:
            raise ValueError(
                f"Passenger {name!r} objectId differs between ship {ship_id} and file"
            )
        if passenger["mesh"] != embedded["mesh"]:
            raise ValueError(
                f"Passenger {name!r} mesh differs between ship {ship_id} and file"
            )

    ships_dir = output_path / "scenarios" / scenario_name / "ships"
    output_passengers_dir = ships_dir / "passengers"
    output_passengers_dir.mkdir(parents=True)

    for ship in ships:
        _write_json(ships_dir / f"{ship['id']}.json", ship)
    for name in sorted(passenger_files, key=lambda item: passenger_files[item][1]["objectId"]):
        source_path, passenger = passenger_files[name]
        output_passenger = copy.deepcopy(passenger)
        output_passenger["pose"] = _passenger_pose_with_lead_in(
            _trajectory_with_ned_offset(passenger["pose"]),
            name,
            lead_in_s,
            lead_in_rows,
        )
        _write_json(output_passengers_dir / source_path.name, output_passenger)

    static_rows: dict[str, dict[str, Any]] = {}
    for static_object in static_objects:
        name = str(static_object["name"])
        object_id = _require_int(static_object["object_id"], f"Static {name} objectId")
        if not 1 <= object_id <= 0xFFFFFF:
            raise ValueError(f"Static {name} objectId {object_id} is outside 1..0xFFFFFF")
        if object_id in ship_object_ids or object_id in passenger_object_ids:
            raise ValueError(f"Static {name} objectId {object_id} collides with a dynamic actor")
        if object_id in static_rows:
            raise ValueError(f"Duplicate static objectId {object_id}")
        mesh = _resolved_static_mesh(
            str(static_object["mesh"]), meshes_path, name
        )
        ned = [_require_finite_number(value, f"Static {name} ned") for value in static_object["ned_m"]]
        if len(ned) != 3:
            raise ValueError(f"Static {name} ned_m must have three values")
        heading = _require_finite_number(static_object.get("heading_deg", 0.0), f"Static {name} heading")
        total_rows = 600 + lead_in_rows
        pose = [
            [ned[0] * 100.0, ned[1] * 100.0, ned[2] * 100.0, heading, row * 0.5]
            for row in range(total_rows)
        ]
        _write_json(output_passengers_dir / f"{name}.json", {
            "name": name,
            "objectId": object_id,
            "mesh": mesh,
            "pose": pose,
        })
        static_rows[name] = {
            "name": name,
            "object_id": object_id,
            "mesh_name": mesh["MeshName"],
            "asset_path": mesh["AssetPath"],
            "ned_m": ned,
            "heading_deg": heading,
            "source": static_object.get("source", "fixture"),
        }

    flattened_events: list[dict[str, Any]] = []
    for ship_id, source_event in canonical_events:
        event = copy.deepcopy(source_event)
        event.setdefault("entity_id", ship_id)
        flattened_events.append(event)
    flattened_events.sort(key=lambda event: event.get("time", 0.0))
    _write_json(ships_dir / "events.json", flattened_events)

    ship_mapping = {
        str(ship["id"]): {
            "name": ship["name"],
            "object_id": ship["objectId"],
            "thermal_id": ship["thermalId"],
            "mesh_name": ship["mesh"]["MeshName"],
            "asset_path": ship["mesh"]["AssetPath"],
        }
        for ship in ships
    }
    passenger_mapping = {
        str(passenger["objectId"]): {
            "name": name,
            "object_id": passenger["objectId"],
            "ship_id": str(passenger_owners[name][0]),
        }
        for name, (_path, passenger) in sorted(
            passenger_files.items(), key=lambda item: item[1][1]["objectId"]
        )
    }
    mapping = {
        "scenario_name": scenario_name,
        "ships": ship_mapping,
        "passengers": passenger_mapping,
        "static_objects": static_rows,
    }
    _write_json(output_path / "mapping.json", mapping)

    generated_paths = sorted(
        path
        for path in output_path.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    outputs = {
        path.relative_to(output_path).as_posix(): _sha256(path)
        for path in generated_paths
    }
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(),
        "source_dir": str(vessels_path.resolve()),
        "source_sha256": source_sha256,
        "canonical_params": CANONICAL_PARAMS,
        "lead_in_seconds": lead_in_s,
        "lead_in_note": LEAD_IN_NOTE,
        "outputs": outputs,
        "ship_motion_max_displacement_m": motion,
        "passengers_per_ship": passenger_counts,
        "static_objects": {
            row["name"]: {"object_id": row["object_id"], "ned_m": row["ned_m"]}
            for row in static_rows.values()
        },
    }
    _write_json(output_path / "manifest.json", manifest)

    return FixtureResult(
        out_dir=output_path,
        scenario_dir=output_path / "scenarios" / scenario_name,
        ship_count=len(ships),
        passenger_count=len(passenger_files) + len(static_rows),
        output_count=len(outputs),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the recorded Mission 1 AirSim reconstruction fixture."
    )
    parser.add_argument("--vessels", type=Path, default=DEFAULT_VESSELS_DIR)
    parser.add_argument("--static-meshes", type=Path, default=DEFAULT_STATIC_MESHES_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--lead-in-s",
        type=float,
        default=DEFAULT_LEAD_IN_SECONDS,
        help="synthetic trajectory lead-in duration in seconds",
    )
    parser.add_argument("--scenario-name", default=SCENARIO_NAME)
    parser.add_argument(
        "--static-objects",
        type=Path,
        default=None,
        help="JSON list of stationary actors: name, objectId, mesh, ned_m, heading_deg",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove an existing output path before building",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixture builder command-line interface."""

    args = _parser().parse_args(argv)
    out_dir: Path = args.out
    if out_dir.exists():
        if not args.force:
            raise SystemExit(
                f"Refusing to overwrite existing fixture output {out_dir}; use --force"
            )
        if out_dir.is_dir():
            shutil.rmtree(out_dir)
        else:
            out_dir.unlink()
    static_objects: Sequence[Mapping[str, Any]] = ()
    if args.static_objects is not None:
        value = _read_json(args.static_objects)
        if isinstance(value, dict):
            value = value.get("objects")
        if not isinstance(value, list):
            raise SystemExit(
                "--static-objects must point at a JSON list or an object "
                "with an objects list"
            )
        static_objects = value
    result = build_fixture(
        args.vessels,
        args.static_meshes,
        out_dir,
        lead_in_s=args.lead_in_s,
        scenario_name=args.scenario_name,
        static_objects=static_objects,
    )
    print(
        f"Built AirSim reconstruction fixture at {result.out_dir} "
        f"({result.ship_count} ships, {result.passenger_count} passengers, "
        f"{result.output_count} checksummed outputs)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
