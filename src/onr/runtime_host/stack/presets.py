"""Environment Stack presets loaded from ``conf/stack_presets.yaml``.

A preset names one Mission mode plus the toggles the Operator Console may
offer. Its composition inputs are the mode's defaults (the historical herdr
launcher defaults) overlaid with the preset's own ``inputs``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PRESETS_PATH = (
    Path(__file__).resolve().parents[4] / "conf" / "stack_presets.yaml"
)
DEFAULT_SIMULATION_LIMIT_SECONDS = 600.0
"""The closed-loop CLI and Runtime Host default when a preset sets no limit."""

MISSION_MODES = (
    "mission1",
    "mission2",
    "mission3",
    "mission4",
    "joint",
    "joint24",
    "joint34",
)
PERCEPTION_MODES = ("off", "ideal", "yolo")
UPDATE_OWNERSHIPS = ("coordinator_driven", "environment_driven")
MISSION4_MODES = frozenset({"mission4", "joint24", "joint34"})


class StackRequestError(ValueError):
    """A preset ID or toggle combination the catalog cannot launch."""


@dataclass(frozen=True, slots=True)
class StackToggles:
    """Operator-selected Environment Stack switches."""

    airsim: bool
    perception: str
    update_ownership: str
    simulation_limit_seconds: float = DEFAULT_SIMULATION_LIMIT_SECONDS

    def __post_init__(self) -> None:
        if self.perception not in PERCEPTION_MODES:
            raise StackRequestError(
                f"perception must be one of {', '.join(PERCEPTION_MODES)}"
            )
        if self.update_ownership not in UPDATE_OWNERSHIPS:
            raise StackRequestError(
                f"update_ownership must be one of {', '.join(UPDATE_OWNERSHIPS)}"
            )
        if not self.simulation_limit_seconds > 0:
            raise StackRequestError("simulation_limit_seconds must be positive")
        if self.perception != "off" and not self.airsim:
            raise StackRequestError("perception requires airsim: true")

    def payload(self) -> dict[str, object]:
        """The contract ``toggles`` object (preflight and the stack section)."""

        return {
            "airsim": self.airsim,
            "perception": self.perception,
            "update_ownership": self.update_ownership,
        }


@dataclass(frozen=True, slots=True)
class StackRoots:
    """Checkout locations every composition path resolves against."""

    agent: Path
    physical_runtime: Path
    solution: Path
    scenarios: Path
    conda_init: Path

    def expand(self, value: str) -> Path:
        return Path(
            value.format(
                agent=self.agent,
                physical=self.physical_runtime,
                solution=self.solution,
                scenarios=self.scenarios,
            )
        )


@dataclass(frozen=True, slots=True)
class EngineSettings:
    executable: Path
    airsim_settings: Path
    airsim_vehicle: str
    airsim_camera: str
    airsim_third_person_camera: str
    rpc_port: int


@dataclass(frozen=True, slots=True)
class PerceptionSettings:
    port: int
    calibration: Path
    yolo_device: str
    yolo_weights: Path


@dataclass(frozen=True, slots=True)
class MissionInputs:
    """Every input file a Mission mode's composition reads (``None`` = unused)."""

    mission_file: Path
    scenario_config: Path
    maneuver_seconds: int
    mission1_instance: Path | None = None
    mission2_scenario: Path | None = None
    mission3_selection: Path | None = None
    mission3_fixture: Path | None = None
    mission4_package: Path | None = None
    mission4_fixture: Path | None = None
    mission4_answers: Path | None = None
    mission4_requests: Path | None = None
    engine_scenario: Path | None = None


_INPUT_FIELDS = frozenset(item.name for item in fields(MissionInputs))


@dataclass(frozen=True, slots=True)
class StackPreset:
    preset_id: str
    title: str
    mission_mode: str
    supports_airsim: tuple[bool, ...]
    supports_perception: tuple[str, ...]
    unsupported_reason: str | None
    defaults: StackToggles
    explicit_simulation_limit_seconds: float | None
    inputs: Mapping[str, str | None] = field(default_factory=dict)

    def supports(self, toggles: StackToggles) -> bool:
        return (
            toggles.airsim in self.supports_airsim
            and toggles.perception in self.supports_perception
        )


@dataclass(frozen=True, slots=True)
class StackCatalog:
    """The parsed preset file."""

    path: Path
    schema_version: int
    default_preset_id: str
    presets: tuple[StackPreset, ...]
    roots: Mapping[str, str]
    engine: Mapping[str, Any]
    perception: Mapping[str, Any]
    modes: Mapping[str, Mapping[str, Any]]

    def preset(self, preset_id: str | None = None) -> StackPreset:
        wanted = self.default_preset_id if preset_id is None else preset_id
        for preset in self.presets:
            if preset.preset_id == wanted:
                return preset
        raise StackRequestError(f"unknown stack preset: {wanted}")

    def resolve_roots(
        self,
        repo_root: Path,
        *,
        physical_runtime: Path | None = None,
        solution: Path | None = None,
        scenarios: Path | None = None,
        conda_init: Path | None = None,
    ) -> StackRoots:
        base = self.path.parents[1]

        def root(name: str, override: Path | None) -> Path:
            if override is not None:
                return Path(override)
            return Path(os.path.normpath(base / self.roots[name]))

        return StackRoots(
            agent=Path(repo_root),
            physical_runtime=root("physical_runtime", physical_runtime),
            solution=root("solution", solution),
            scenarios=root("scenarios", scenarios),
            conda_init=root("conda_init", conda_init),
        )

    def engine_settings(self, roots: StackRoots) -> EngineSettings:
        base = self.path.parents[1]
        return EngineSettings(
            executable=Path(
                os.path.normpath(base / roots.expand(self.engine["executable"]))
            ),
            airsim_settings=roots.expand(self.engine["airsim_settings"]),
            airsim_vehicle=str(self.engine["airsim_vehicle"]),
            airsim_camera=str(self.engine["airsim_camera"]),
            airsim_third_person_camera=str(self.engine["airsim_third_person_camera"]),
            rpc_port=int(self.engine["rpc_port"]),
        )

    def perception_settings(self, roots: StackRoots) -> PerceptionSettings:
        return PerceptionSettings(
            port=int(self.perception["port"]),
            calibration=roots.expand(self.perception["calibration"]),
            yolo_device=str(self.perception["yolo_device"]),
            yolo_weights=roots.expand(self.perception["yolo_weights"]),
        )

    def mode_inputs(self, mission_mode: str, roots: StackRoots) -> MissionInputs:
        """The mode's defaults, as the herdr launcher resolved them."""

        if mission_mode not in self.modes:
            raise StackRequestError(f"unknown mission mode: {mission_mode}")
        return _inputs(self.modes[mission_mode], roots)

    def preset_inputs(self, preset: StackPreset, roots: StackRoots) -> MissionInputs:
        return _inputs({**self.modes[preset.mission_mode], **preset.inputs}, roots)

    def toggles(
        self, preset: StackPreset, request: Mapping[str, object] | None
    ) -> StackToggles:
        """Resolve an activation ``stack`` object against the preset defaults.

        ``request`` may omit any toggle; unknown keys and wrong types raise
        :class:`StackRequestError`. Unsupported combinations raise with the
        preset's ``unsupported_reason`` when it has one.
        """

        values = dict(request or {})
        values.pop("preset_id", None)
        unknown = set(values) - {
            "airsim",
            "perception",
            "update_ownership",
            "simulation_limit_seconds",
        }
        if unknown:
            raise StackRequestError(
                f"unknown stack fields: {', '.join(sorted(unknown))}"
            )
        airsim = values.get("airsim", preset.defaults.airsim)
        perception = values.get("perception", preset.defaults.perception)
        ownership = values.get("update_ownership", preset.defaults.update_ownership)
        limit = values.get(
            "simulation_limit_seconds", preset.defaults.simulation_limit_seconds
        )
        if not isinstance(airsim, bool):
            raise StackRequestError("airsim must be a boolean")
        if not isinstance(perception, str) or not isinstance(ownership, str):
            raise StackRequestError("perception and update_ownership must be strings")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            raise StackRequestError("simulation_limit_seconds must be a number")
        toggles = StackToggles(airsim, perception, ownership, float(limit))
        if not preset.supports(toggles):
            reason = (
                preset.unsupported_reason or "the preset does not offer these toggles"
            )
            raise StackRequestError(
                f"{preset.preset_id} does not support airsim={str(airsim).lower()} "
                f"perception={perception}: {reason}"
            )
        return toggles

    def payload(self, repo_root: Path) -> dict[str, object]:
        """``GET /api/v1/stack/presets`` response body."""

        roots = self.resolve_roots(repo_root)
        return {
            "schema_version": self.schema_version,
            "default_preset_id": self.default_preset_id,
            "presets": [
                {
                    "preset_id": preset.preset_id,
                    "title": preset.title,
                    "mission_mode": preset.mission_mode,
                    "default_mission_text": _mission_text(
                        self.preset_inputs(preset, roots).mission_file
                    ),
                    "supports": {
                        "airsim": list(preset.supports_airsim),
                        "perception": list(preset.supports_perception),
                    },
                    "unsupported_reason": preset.unsupported_reason,
                    "defaults": {
                        **preset.defaults.payload(),
                        "simulation_limit_seconds": _number(
                            preset.defaults.simulation_limit_seconds
                        ),
                    },
                }
                for preset in self.presets
            ],
        }


def load_stack_catalog(path: Path | None = None) -> StackCatalog:
    source = Path(path) if path is not None else DEFAULT_PRESETS_PATH
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError(f"{source}: stack presets need schema_version 1")
    modes = document["modes"]
    if set(modes) != set(MISSION_MODES):
        raise ValueError(
            f"{source}: modes must define exactly {', '.join(MISSION_MODES)}"
        )
    presets = tuple(_preset(entry, modes) for entry in document["presets"])
    identifiers = [preset.preset_id for preset in presets]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{source}: duplicate preset_id")
    catalog = StackCatalog(
        path=source.resolve(),
        schema_version=1,
        default_preset_id=str(document["default_preset_id"]),
        presets=presets,
        roots=dict(document["roots"]),
        engine=dict(document["engine"]),
        perception=dict(document["perception"]),
        modes={name: dict(value) for name, value in modes.items()},
    )
    catalog.preset(catalog.default_preset_id)
    return catalog


def _preset(entry: Mapping[str, Any], modes: Mapping[str, Any]) -> StackPreset:
    mode = entry["mission_mode"]
    if mode not in modes:
        raise ValueError(f"preset {entry['preset_id']}: unknown mission_mode {mode}")
    inputs = dict(entry.get("inputs") or {})
    unknown = set(inputs) - _INPUT_FIELDS
    if unknown:
        raise ValueError(
            f"preset {entry['preset_id']}: unknown inputs {sorted(unknown)}"
        )
    defaults = dict(entry["defaults"])
    explicit_limit = defaults.get("simulation_limit_seconds")
    toggles = StackToggles(
        airsim=bool(defaults["airsim"]),
        perception=str(defaults["perception"]),
        update_ownership=str(defaults["update_ownership"]),
        simulation_limit_seconds=float(
            DEFAULT_SIMULATION_LIMIT_SECONDS
            if explicit_limit is None
            else explicit_limit
        ),
    )
    supports = entry["supports"]
    preset = StackPreset(
        preset_id=str(entry["preset_id"]),
        title=str(entry["title"]),
        mission_mode=mode,
        supports_airsim=tuple(bool(value) for value in supports["airsim"]),
        supports_perception=tuple(str(value) for value in supports["perception"]),
        unsupported_reason=entry.get("unsupported_reason"),
        defaults=toggles,
        explicit_simulation_limit_seconds=(
            None if explicit_limit is None else float(explicit_limit)
        ),
        inputs=inputs,
    )
    if not preset.supports(toggles):
        raise ValueError(f"preset {preset.preset_id}: defaults are not in supports")
    return preset


def _inputs(values: Mapping[str, Any], roots: StackRoots) -> MissionInputs:
    resolved: dict[str, Any] = {}
    for name, value in values.items():
        if name not in _INPUT_FIELDS:
            raise ValueError(f"unknown mission input: {name}")
        if name == "maneuver_seconds":
            resolved[name] = int(value)
        else:
            resolved[name] = None if value is None else roots.expand(str(value))
    return MissionInputs(**resolved)


def _mission_text(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["mission_text"])
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(f"preset Mission Input has no mission_text: {path}") from error


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


__all__ = [
    "DEFAULT_PRESETS_PATH",
    "DEFAULT_SIMULATION_LIMIT_SECONDS",
    "MISSION4_MODES",
    "MISSION_MODES",
    "PERCEPTION_MODES",
    "UPDATE_OWNERSHIPS",
    "EngineSettings",
    "MissionInputs",
    "PerceptionSettings",
    "StackCatalog",
    "StackPreset",
    "StackRequestError",
    "StackRoots",
    "StackToggles",
    "load_stack_catalog",
]
