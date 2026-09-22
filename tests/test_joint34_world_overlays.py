"""Behavior tests for the Joint34 world-pane overlay layers.

The evidence fixtures are the real recorded values of ``run.7jruzl`` trimmed
to the fields the pane reads, so the pinned probabilities, positions, and
uncertainties are the agent's own replayed public-evidence belief.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from onr.demo.airsim_reconstruction.world_pane import (
    COUNT_KEYS,
    PaneGeometry,
    PaneState,
    belief_snapshots,
    best_match,
    dock_coverage_pct,
    draw_overlays,
    mission4_sections,
    pane_state,
    section_at,
    ship_fixes,
)

_REPO = Path(__file__).resolve().parents[1]

_PACKAGE = {
    "schema_version": 1,
    "vocabulary": {"type": ["container", "truck"], "color": ["red", "blue"]},
    "areas": {
        "dock": {
            "polygon": [[-20.0, -20.0], [20.0, -20.0], [20.0, 20.0], [-20.0, 20.0]],
            "prior": 1.0,
        }
    },
    "obstacles": [],
    "keep_out_zones": [],
    "mission_time_budget_s": 300.0,
    "found_threshold": 0.1,
}

# Recorded panes: the dock window the run starts in, and the window the drone
# holds while ship 7's GPS fix falls inside it.
_DOCK_PANE = PaneGeometry((-49.0, -98.0), (79.0, 30.0), resolution_m=2.0, tile_size=8)
_SHIP_PANE = PaneGeometry((47.0, -2.0), (175.0, 126.0), resolution_m=2.0, tile_size=8)
_EMPTY_PANE = PaneGeometry((200.0, 200.0), (328.0, 328.0), resolution_m=2.0, tile_size=8)


def _objective(target_id: str, color: str | None, kind: str) -> dict[str, Any]:
    attributes = {"type": kind} if color is None else {"color": color, "type": kind}
    return {
        "target_id": target_id,
        "area_ids": ["dock"],
        "attributes": attributes,
        "description": f"a {color + ' ' if color else ''}{kind} in dock",
    }


_OBJECTIVES: dict[int, dict[str, Any]] = {
    1: {"worker:1": _objective("worker:1", "red", "container")},
    4: {
        "worker:1": _objective("worker:1", "red", "container"),
        "worker:3": _objective("worker:3", "blue", "container"),
    },
    5: {
        "worker:1": _objective("worker:1", "red", "container"),
        "worker:3": _objective("worker:3", "blue", "container"),
        "worker:5": _objective("worker:5", None, "truck"),
    },
}


def _observation(
    observation_id: str,
    track_id: str,
    acquired_at_s: float,
    position: list[float],
    position_uncertainty_m: float,
    attribute_uncertainty: float,
    color: str,
    kind: str,
) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "track_id": track_id,
        "acquired_at_s": acquired_at_s,
        "position": position,
        "position_uncertainty_m": position_uncertainty_m,
        "coordinate_frame": "local_ned",
        "uncertainty_model": "categorical_symmetric_error_v1",
        "source": "simulated",
        "attributes": {
            "color": {"value": color, "uncertainty": attribute_uncertainty},
            "type": {"value": kind, "uncertainty": attribute_uncertainty},
        },
    }


_OBSERVATIONS = {
    "search-view:1": _observation(
        "search-view:1", "observed:1", 0.0, [6, 0, 0], 6, 0.4, "red", "container"
    ),
    "search-view:2": _observation(
        "search-view:2", "observed:2", 0.0, [6, 0, 0], 1, 0.1, "blue", "container"
    ),
    "search-view:3": _observation(
        "search-view:3", "observed:2", 2.0, [11, 0, 0], 6, 0.4, "blue", "container"
    ),
    "search-view:4": _observation(
        "search-view:4", "observed:1", 5.0, [1, 0, 0], 1, 0.1, "red", "container"
    ),
    "search-view:5": _observation(
        "search-view:5", "observed:2", 5.0, [6, 0, 0], 1, 0.1, "blue", "container"
    ),
    "search-view:6": _observation(
        "search-view:6", "observed:3", 91.0, [56, 0, 0], 6, 0.4, "red", "container"
    ),
}
_FIRST_FIVE = tuple(list(_OBSERVATIONS)[:5])

# (publication time, request revision, objective revision, observation ids)
_SECTIONS: tuple[tuple[float, int, int, tuple[str, ...]], ...] = (
    (0.0, 1, 1, ("search-view:1", "search-view:2")),
    (2.0, 2, 1, _FIRST_FIVE[:3]),
    (5.0, 2, 1, _FIRST_FIVE),
    (40.0, 4, 4, _FIRST_FIVE),
    (80.0, 5, 5, _FIRST_FIVE),
    (91.0, 5, 5, tuple(_OBSERVATIONS)),
)


def _section(revision: int, objective_revision: int, ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package": _PACKAGE,
        "revision": revision,
        "objectives": _OBJECTIVES[objective_revision],
        "observations": [_OBSERVATIONS[observation_id] for observation_id in ids],
    }


def _fix(ship_id: int, north: float, east: float, sampled_at_s: float) -> dict[str, Any]:
    return {
        "entity_id": ship_id,
        "position": {"x": north, "y": east, "z": -2.5},
        "sampled_at_s": sampled_at_s,
        "source": "gps",
    }


_RECORDED_FIXES = (
    _fix(7, 173.742, 121.402, 40.0),
    _fix(15, 145.328, 305.259, 40.0),
    _fix(16, 497.213, 119.845, 40.0),
)


def _world_row(
    time_s: float, revision: int, objective_revision: int, ids: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "observation_time_s": time_s,
        "observation_kind": "world_model",
        "world_model_info": {
            "mission4": _section(revision, objective_revision, ids),
            "mission3": {"selected_ship_ids": [15, 16, 7]},
            "public_position_fixes": list(_RECORDED_FIXES) if time_s >= 40.0 else [],
        },
    }


def _worlds() -> dict[float, dict[str, Any]]:
    return {
        time_s: _world_row(time_s, revision, objective_revision, ids)
        for time_s, revision, objective_revision, ids in _SECTIONS
    }


def _coverage_row(observed_cells: int) -> dict[str, Any]:
    row = _world_row(40.0, 4, 4, _FIRST_FIVE)
    row["world_model_info"]["mission4"]["coverage"] = {
        "dock": {
            "grid_resolution_m": 2.0,
            "observed_cells": [[float(index), 0.0] for index in range(observed_cells)],
        }
    }
    return row


def _snapshots() -> dict[float, Any]:
    return belief_snapshots(mission4_sections(_worlds()), "mission:demo")


def _load_derivation_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "derive_joint34_video_bundle",
        _REPO / "scripts/derive_joint34_video_bundle.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestPaneGeometry:
    def test_transform_maps_ned_through_the_engine_grid_convention(self) -> None:
        # Pane 0 of the recorded run: a 64x64-cell, 128 m window at 2 m/cell.
        assert _DOCK_PANE.size_px == (512, 512)
        assert _DOCK_PANE.px_per_m == 4.0
        # North decreases the row index; east increases the column index.
        assert _DOCK_PANE.cell(0.0, 0.0) == (49, 39)
        assert _DOCK_PANE.cell(1.0, 0.0) == (49, 39)
        assert _DOCK_PANE.cell(6.0, 0.0) == (49, 36)
        assert _DOCK_PANE.cell(0.0, 20.0) == (59, 39)
        # Pane-local pixels are the linear NED map: 4 px/m from the cell
        # corner, so a cell centre lands on the engine's drone_pixel.
        assert _DOCK_PANE.pixel(1.0, 0.0) == (392.0, 312.0)
        assert _DOCK_PANE.pixel(6.0, 0.0) == (392.0, 292.0)
        assert _DOCK_PANE.pixel(-20.0, -20.0) == (312.0, 396.0)
        assert _DOCK_PANE.pixel(20.0, 20.0) == (472.0, 236.0)

    def test_cell_centres_round_trip_to_the_engine_drone_pixel(self) -> None:
        for column in (0, 7, 31, 63):
            for row in (0, 5, 40, 63):
                east = _DOCK_PANE.ned_bounds_min[1] + (column + 0.5) * 2.0
                north = _DOCK_PANE.ned_bounds_max[0] - (row + 0.5) * 2.0
                assert _DOCK_PANE.cell(north, east) == (column, row)
                assert _DOCK_PANE.pixel(north, east) == (
                    (column + 0.5) * 8,
                    (row + 0.5) * 8,
                )

    def test_geometry_reads_a_real_partition_metadata(self) -> None:
        from onr_physical_runtime.world_model.model import PartitionMetadata

        metadata = PartitionMetadata(
            partition_id=0,
            ned_bounds_min=(-49.0, -98.0),
            ned_bounds_max=(79.0, 30.0),
            heightmap_indices=(0, 0, 64, 64),
            grid_width=64,
            grid_height=64,
            multigrid_resolution=2.0,
            height_stats={},
            terrain_distribution={},
        )
        geometry = PaneGeometry.from_partition_metadata(metadata, tile_size=8)
        assert geometry == _DOCK_PANE
        assert geometry.cell(1.0, 0.0) == (49, 39)

    def test_geometry_rejects_unusable_windows(self) -> None:
        with pytest.raises(ValueError):
            PaneGeometry((0.0, 0.0), (10.0, 10.0), resolution_m=0.0)
        with pytest.raises(ValueError):
            PaneGeometry((10.0, 10.0), (0.0, 0.0), resolution_m=2.0)


class TestAvailabilityGating:
    def test_gps_fix_is_invisible_before_its_sampled_time(self) -> None:
        assert ship_fixes(_RECORDED_FIXES, [7], 39.5) == ()
        visible = ship_fixes(_RECORDED_FIXES, [7], 40.0)
        assert len(visible) == 1
        assert visible[0].ship_id == 7
        assert (visible[0].north, visible[0].east) == (173.742, 121.402)
        assert visible[0].age_s == 0.0
        assert ship_fixes(_RECORDED_FIXES, [7], 44.5)[0].age_s == 4.5

    def test_latest_fix_per_selected_vessel_wins(self) -> None:
        fixes = (
            _fix(7, 0.0, 0.0, 5.0),
            _fix(7, 40.0, 40.0, 10.0),
            _fix(9, 1.0, 1.0, 10.0),
        )
        visible = ship_fixes(fixes, [7, 15], 12.0)
        assert [(entry.ship_id, entry.north, entry.age_s) for entry in visible] == [
            (7, 40.0, 2.0)
        ]

    def test_truck_evidence_appears_only_from_its_recorded_acquisition(self) -> None:
        worlds = _worlds()
        snapshots = _snapshots()
        # Before the 91.0 s observation the truck objective inherits the red
        # container track, so its potential location is a dock view position.
        early = pane_state(section_at(worlds, 90.5), snapshots, 90.5)
        truck = [marker for marker in early.targets if marker.target_id == "worker:5"]
        assert len(truck) == 1
        assert (truck[0].north, truck[0].east) == (1.0, 0.0)
        assert truck[0].probability == pytest.approx(0.06896551724137931)

        recorded = pane_state(section_at(worlds, 91.0), snapshots, 91.0)
        truck = [marker for marker in recorded.targets if marker.target_id == "worker:5"]
        assert len(truck) == 1
        assert (truck[0].north, truck[0].east) == (56.0, 0.0)
        assert truck[0].probability == pytest.approx(0.4)
        assert truck[0].uncertainty_m == 6.0
        assert truck[0].found is False

    def test_pane_draws_nothing_before_the_first_section(self) -> None:
        worlds = _worlds()
        worlds.pop(0.0)
        assert section_at(worlds, 0.5) is None
        state = pane_state(None, {}, 0.5)
        assert state.areas == () and state.targets == () and state.ship_fixes == ()


class TestBeliefReplay:
    def test_replay_reproduces_the_recorded_belief(self) -> None:
        middle = _snapshots()[40.0]
        red = best_match(middle, "worker:1")
        blue = best_match(middle, "worker:3")
        assert red is not None and blue is not None
        assert red.probability == pytest.approx(0.8668252080856124)
        assert red.position == (1, 0, 0)
        assert red.position_uncertainty_m == 1
        assert red.observed_at_s == 5.0
        assert red.found is False and red.reason == "match_uncertain"
        assert blue.probability == pytest.approx(0.9837401082882132)
        assert blue.position == (6, 0, 0)
        assert blue.observed_at_s == 5.0
        assert blue.found is True and blue.reason is None

    def test_replay_is_deterministic(self) -> None:
        first = _snapshots()
        second = _snapshots()
        assert first.keys() == second.keys()
        for time_s in first:
            assert first[time_s].matches == second[time_s].matches

    def test_future_dated_observation_is_rejected(self) -> None:
        section = _section(2, 1, ("search-view:1",))
        section["observations"] = [
            _observation(
                "search-view:9", "observed:1", 6.0, [1, 0, 0], 1, 0.1, "red", "container"
            )
        ]
        with pytest.raises(ValueError, match="future search evidence"):
            belief_snapshots([(5.0, section)], "mission:demo")


class TestOverlayDrawing:
    def _frame(self, geometry: PaneGeometry) -> Image.Image:
        return Image.new("RGB", geometry.size_px, "#0d1a2b")

    def test_draw_counts_are_reported_per_layer(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        counts = draw_overlays(self._frame(_DOCK_PANE), _DOCK_PANE, state)
        assert set(counts) == set(COUNT_KEYS)
        assert counts["aoi_polygons"] == 1
        assert counts["targets"] == 2
        assert counts["uncertainty_circles"] == 2
        # All three recorded fixes lie outside the dock window.
        assert counts["ship_fixes"] == 0
        assert counts["legend"] == 1

    def test_selected_vessel_fix_is_drawn_inside_its_pane(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        counts = draw_overlays(self._frame(_SHIP_PANE), _SHIP_PANE, state)
        assert counts["ship_fixes"] == 1
        assert counts["aoi_polygons"] == 0
        assert counts["targets"] == 0

    def test_dock_aoi_is_filled_inside_its_polygon(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        frame = self._frame(_DOCK_PANE)
        baseline = frame.copy()
        draw_overlays(frame, _DOCK_PANE, state)
        # The dock polygon spans north/east -20..20 m: x 312..472, y 236..396.
        assert frame.getpixel((360, 300)) != baseline.getpixel((360, 300))
        assert frame.getpixel((100, 450)) == baseline.getpixel((100, 450))
        inside = frame.getpixel((430, 370))
        assert inside[2] > baseline.getpixel((430, 370))[2] + 10
        assert inside[2] > inside[0]

    def test_keep_out_zone_layer_paints_a_recorded_zone_red(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        zone = [[-20.0, 0.0], [20.0, 0.0], [20.0, 20.0], [-20.0, 20.0]]
        zone_state = PaneState(
            areas=state.areas,
            keep_out_zones=(zone,),
            obstacles=(),
            targets=(),
            ship_fixes=(),
        )
        frame = self._frame(_DOCK_PANE)
        baseline = frame.copy()
        counts = draw_overlays(frame, _DOCK_PANE, zone_state)
        assert counts["koz_polygons"] == 1
        # The zone covers the dock's north half: x 312..472, y 236..316.
        samples = [
            (x, y) for x in range(316, 470, 3) for y in range(240, 314, 3)
        ]
        changed = [p for p in samples if frame.getpixel(p) != baseline.getpixel(p)]
        red = [
            p
            for p in samples
            if frame.getpixel(p)[0] > baseline.getpixel(p)[0] + 40
        ]
        assert len(changed) > 100
        assert len(red) > 50
        # South of the zone the pane stays untouched.
        assert frame.getpixel((430, 370)) == baseline.getpixel((430, 370))

    def test_honest_empty_keep_out_layer_and_receipt(self) -> None:
        module = _load_derivation_module()
        worlds = _worlds()
        state = pane_state(section_at(worlds, 40.0), _snapshots(), 40.0)
        counts = draw_overlays(self._frame(_DOCK_PANE), _DOCK_PANE, state)
        assert state.keep_out_zones == ()
        assert counts["koz_polygons"] == 0
        assert counts["obstacle_polygons"] == 0
        receipt = module.overlay_layer_receipt(
            [{"tick": 0, **counts}],
            module.recorded_polygon_counts(worlds),
            {"tile_size": 8, "resolution_m": 2.0, "window_cells": 64, "frames": 1},
        )
        zones = receipt["keep_out_zones"]
        assert zones["recorded_polygons"] == 0
        assert zones["total_draws"] == 0
        assert "honestly empty" in zones["note"]
        assert receipt["dock_aoi"]["recorded_polygons"] == 1
        assert receipt["dock_aoi"]["first_tick"] == 0
        assert receipt["world_pane"]["window_cells"] == 64

    def test_layers_outside_the_pane_window_are_not_drawn(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        frame = self._frame(_EMPTY_PANE)
        baseline = frame.copy()
        counts = draw_overlays(frame, _EMPTY_PANE, state)
        assert counts["aoi_polygons"] == 0
        assert counts["targets"] == 0
        assert counts["ship_fixes"] == 0
        assert counts["legend"] == 1
        # Only the pane key changed; no recorded geometry is invented.
        changed = [
            (x, y)
            for x in range(0, 512, 5)
            for y in range(0, 512, 5)
            if frame.getpixel((x, y)) != baseline.getpixel((x, y))
        ]
        assert changed
        assert all(y <= 80 for _, y in changed)

    def test_overlays_are_byte_identical_for_identical_inputs(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        digests = []
        for _ in range(2):
            frame = self._frame(_DOCK_PANE)
            draw_overlays(frame, _DOCK_PANE, state)
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            digests.append(hashlib.sha256(buffer.getvalue()).hexdigest())
        assert len(set(digests)) == 1

    def test_frame_size_must_match_the_pane_window(self) -> None:
        state = pane_state(section_at(_worlds(), 40.0), _snapshots(), 40.0)
        frame = Image.new("RGB", (256, 256), "#0d1a2b")
        with pytest.raises(ValueError, match="expected"):
            draw_overlays(frame, _DOCK_PANE, state)


class TestDockCoverage:
    def test_coverage_percentage_uses_recorded_observed_cells(self) -> None:
        # The dock polygon is 40 x 40 m at 2 m/cell: 400 cells.
        assert dock_coverage_pct(_coverage_row(183)) == 45.8
        assert dock_coverage_pct(_coverage_row(0)) == 0.0
        assert dock_coverage_pct(_coverage_row(400)) == 100.0

    def test_coverage_is_unknown_without_a_published_package(self) -> None:
        assert dock_coverage_pct(None) is None
        row = _coverage_row(10)
        row["world_model_info"]["mission4"] = {"package": {}, "coverage": {}}
        assert dock_coverage_pct(row) is None


def test_recorded_bundle_overlay_receipt_documents_every_layer() -> None:
    """The derived Phase-1 bundle receipt stays in step with the layer specs."""

    receipt_path = (
        _REPO
        / "var/demo-video/joint34-20260929-airsim/bundle/reconstruction-receipt.json"
    )
    if not receipt_path.is_file():
        pytest.skip("enriched Phase-1 bundle not present")
    module = _load_derivation_module()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    layers = receipt["overlay_layers"]
    assert set(layers) == {"world_pane"} | {
        name for name, _, _, _ in module.OVERLAY_LAYER_SPECS
    }
    for name, key, source, gating in module.OVERLAY_LAYER_SPECS:
        assert layers[name]["source_field"] == source
        assert layers[name]["gating_rule"] == gating
    assert layers["dock_aoi"]["total_draws"] > 0
    assert layers["keep_out_zones"]["recorded_polygons"] == 0
    assert layers["keep_out_zones"]["total_draws"] == 0
    assert layers["target_potential_locations"]["uncertainty_circles_total"] > 0
    assert layers["selected_vessel_gps_fixes"]["total_draws"] > 0
    assert layers["planned_route"]["total_draws"] > 0
