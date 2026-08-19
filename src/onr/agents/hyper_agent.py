"""DeepAgents integration boundary for Hyper Agent intake."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn, cast

from onr.agents.role_context import MissionRoleContext, RoleEpisode
from onr.agents.structured_output import (
    StructuralIssue,
    StructuredOutputFailure,
    invoke_with_structured_output_recovery,
)
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import MissionSpec, SymbolicMissionSpec


MISSION_SPEC_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "title": "TemporalMissionSpec",
            "type": "object",
            "properties": {
                "mission_id": {"type": "string"},
                "objective": {"type": "string"},
                "planner_choice": {
                    "type": "object",
                    "properties": {
                        "planning_profile": {"enum": ["temporal"]},
                        "planner_id": {"type": ["string", "null"]},
                    },
                    "required": ["planning_profile", "planner_id"],
                    "additionalProperties": False,
                },
                "maneuvers": {
                    "type": "array",
                    "items": {
                        "title": "TemporalManeuver",
                        "type": "object",
                        "properties": {
                            "maneuver_id": {"type": "string"},
                            "intent": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string"},
                                    "parameters": {
                                        "type": "object",
                                        "additionalProperties": {
                                            "type": [
                                                "string",
                                                "number",
                                                "boolean",
                                                "null",
                                            ]
                                        },
                                    },
                                },
                                "required": ["action", "parameters"],
                                "additionalProperties": False,
                            },
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "duration": {"type": "integer"},
                        },
                        "required": [
                            "maneuver_id",
                            "intent",
                            "dependencies",
                            "duration",
                        ],
                        "additionalProperties": False,
                    },
                },
                "horizon": {"type": "integer"},
                "source_authority": {"type": "string"},
            },
            "required": [
                "mission_id",
                "objective",
                "planner_choice",
                "maneuvers",
                "horizon",
                "source_authority",
            ],
            "additionalProperties": False,
        },
        {
            "title": "SymbolicMissionSpec",
            "type": "object",
            "properties": {
                "mission_id": {"type": "string"},
                "objective": {"type": "string"},
                "planner_choice": {
                    "type": "object",
                    "properties": {
                        "planning_profile": {"enum": ["symbolic"]},
                        "planner_id": {"type": ["string", "null"]},
                    },
                    "required": ["planning_profile", "planner_id"],
                    "additionalProperties": False,
                },
                "maneuvers": {
                    "type": "array",
                    "items": {
                        "title": "SymbolicManeuver",
                        "type": "object",
                        "properties": {
                            "maneuver_id": {"type": "string"},
                            "intent": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string"},
                                    "parameters": {
                                        "type": "object",
                                        "additionalProperties": {
                                            "type": [
                                                "string",
                                                "number",
                                                "boolean",
                                                "null",
                                            ]
                                        },
                                    },
                                },
                                "required": ["action", "parameters"],
                                "additionalProperties": False,
                            },
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "cost": {"type": "integer"},
                        },
                        "required": [
                            "maneuver_id",
                            "intent",
                            "dependencies",
                            "cost",
                        ],
                        "additionalProperties": False,
                    },
                },
                "source_authority": {"type": "string"},
                "domain_revision": {"type": "integer"},
            },
            "required": [
                "mission_id",
                "objective",
                "planner_choice",
                "maneuvers",
                "source_authority",
                "domain_revision",
            ],
            "additionalProperties": False,
        },
    ]
}


def create_hyper_agent(
    *,
    model: Any,
    system_prompt: str | None = None,
    mission_id: str | None = None,
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
) -> object:
    """Create a Deep Agent configured for structured mission intake.

    Mission Memory and Role Skills are optional so direct callers remain
    compatible.  When supplied, they are mounted for one Mission/role scope.
    Mission Memory writes remain scoped to the role episode, Role Skills remain
    read-only, and neither context source becomes mission authority.
    """

    return _create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        response_format=MISSION_SPEC_SCHEMA,
        mission_id=mission_id,
        role="hyper-agent",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
    )


def _create_deep_agent(
    *,
    model: Any,
    system_prompt: str | None,
    response_format: Any = None,
    mission_id: str | None = None,
    role: str = "hyper-agent",
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
) -> object:
    """Shared DeepAgents construction with role-context wiring."""

    from deepagents import create_deep_agent

    kwargs: dict[str, Any] = {
        "model": model,
    }
    if mission_id is None and (memory_store is not None or skill_catalog is not None):
        raise ValueError("Mission Memory and Role Skills require a Mission ID")
    if response_format is not None:
        # DeepAgents accepts a schema through response_format and returns it as
        # ``structured_response``.  The strict domain parser remains the final
        # validation gate below.
        kwargs["response_format"] = response_format
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt

    memory_agent_path = "/memory/AGENTS.md"
    context: MissionRoleContext | None = None
    if mission_id is not None and memory_store is not None:
        context = MissionRoleContext(mission_id, role, memory_store)
        root_method = getattr(memory_store, "agent_root", None)
        if not callable(root_method):
            raise TypeError("Mission Memory store must expose agent_root")
        root_value = root_method(mission_id, role)
        if not isinstance(root_value, (str, Path)):
            raise TypeError("Mission Memory store returned an invalid agent root")
        root = Path(root_value)
        if backend_root is None:
            backend_root = root
        try:
            relative_root = root.resolve().relative_to(Path(backend_root).resolve())
        except ValueError as exc:
            raise ValueError("Mission Memory root is outside the agent backend root") from exc
        if relative_root.parts:
            memory_agent_path = "/" + relative_root.as_posix() + "/memory/AGENTS.md"
        kwargs["memory"] = [memory_agent_path]

    selected_skills: list[str] = []
    skill_sources: list[str] = []
    if mission_id is not None and skill_catalog is not None:
        select_all = getattr(skill_catalog, "select_all", None)
        if callable(select_all):
            selections = select_all(role, skill_version)
            if not isinstance(selections, tuple) or not selections:
                raise TypeError("Role Skill catalog returned invalid selections")
        else:
            select = getattr(skill_catalog, "select", None)
            if not callable(select):
                raise TypeError("Role Skill catalog must expose select")
            selections = (select(role, skill_version),)
        for selected in selections:
            selected_path = getattr(selected, "path", None)
            if not isinstance(selected_path, Path):
                raise TypeError("Role Skill catalog returned an invalid selection")
            selected_skills.append(_skill_agent_path(selected_path, backend_root))
            source_path = _skill_agent_path(selected_path.parent, backend_root)
            if source_path not in skill_sources:
                skill_sources.append(source_path)

    if selected_skills:
        kwargs["skills"] = skill_sources

    if context is not None or selected_skills:
        from deepagents.backends.filesystem import FilesystemBackend

        kwargs["backend"] = FilesystemBackend(root_dir=backend_root)

    if context is not None or selected_skills:
        from deepagents.middleware.filesystem import FilesystemPermission

        # Rules are first-match.  The current role's Mission Memory is the
        # sole writable scope; Role Skills and every other path are denied.
        memory_scope = memory_agent_path.removesuffix("AGENTS.md") + "**"
        hard_permissions = []
        for skill_path in selected_skills:
            skill_root = skill_path.rstrip("/") or "/"
            hard_permissions.append(
                FilesystemPermission(
                    ["write"], [skill_root, f"{skill_root}/**"], mode="deny"
                )
            )
        if context is not None:
            hard_permissions.append(FilesystemPermission(["write"], [memory_scope], mode="allow"))
        hard_permissions.append(FilesystemPermission(["write"], ["/**"], mode="deny"))
        kwargs["permissions"] = hard_permissions

    agent = create_deep_agent(**kwargs)
    return RoleEpisode(agent, context) if context is not None else agent


def _skill_agent_path(path: Path, backend_root: Path | None) -> str:
    """Return a POSIX path understood by a virtual FilesystemBackend."""

    selected = Path(path).resolve()
    if backend_root is None:
        return selected.as_posix()
    root = Path(backend_root).resolve()
    try:
        return "/" + selected.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("selected Role Skill is outside the agent backend root") from exc


class DeepAgentsMissionInterpreter:
    """Adapt a Deep Agent response to a validated Mission Specification."""

    def __init__(self, agent: object, max_retries: int = 2) -> None:
        self.agent = agent
        self.max_retries = max_retries

    def interpret(self, mission_input: MissionInput) -> MissionSpec | SymbolicMissionSpec:
        if not isinstance(mission_input, MissionInput):
            raise TypeError("mission interpreter requires a MissionInput")
        invoke = getattr(self.agent, "invoke", None)
        if not callable(invoke):
            raise TypeError("Deep Hyper Agent must expose invoke")

        return invoke_with_structured_output_recovery(
            cast(Callable[[Mapping[str, object]], object], invoke),
            mission_input.to_dict(),
            self.max_retries,
            _parse_mission_response,
        )


def _fail(*issues: StructuralIssue) -> NoReturn:
    raise StructuredOutputFailure(issues)


def _object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(StructuralIssue("invalid_type", path, "object"))
    return cast(Mapping[str, Any], value)


def _fields(value: object, expected: set[str], path: str) -> Mapping[str, Any]:
    data = _object(value, path)
    actual = set(data)
    issues = [
        StructuralIssue("missing_required_field", f"{path}.{name}", "required field")
        for name in sorted(expected - actual)
    ]
    if actual - expected:
        issues.append(
            StructuralIssue(
                "unexpected_field",
                path,
                "only the declared fields",
            )
        )
    if issues:
        raise StructuredOutputFailure(issues)
    return data


def _text(value: object, path: str) -> None:
    if not isinstance(value, str):
        _fail(StructuralIssue("invalid_type", path, "string"))
    if not value.strip():
        _fail(StructuralIssue("invalid_value", path, "non-empty string"))


def _integer(value: object, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(StructuralIssue("invalid_type", path, "integer"))


def _parse_mission_response(response: object) -> MissionSpec | SymbolicMissionSpec:
    envelope = _object(response, "$")
    if "structured_response" not in envelope:
        _fail(
            StructuralIssue(
                "missing_required_field",
                "$.structured_response",
                "required field",
            )
        )
    structured = envelope["structured_response"]
    try:
        model_dump = getattr(structured, "model_dump", None)
        if callable(model_dump):
            structured = model_dump()
    except Exception:
        _fail(
            StructuralIssue(
                "malformed_structured_output",
                "$.structured_response",
                "serializable structured output",
            )
        )

    candidate = _object(structured, "$.structured_response")
    candidate_path = "$.structured_response"
    if "mission_spec" in candidate:
        candidate = _fields(candidate, {"mission_spec"}, candidate_path)
        candidate_path += ".mission_spec"
        candidate = _object(candidate["mission_spec"], candidate_path)

    profile = _validate_common_structure(candidate, candidate_path)
    if profile == "temporal":
        _validate_temporal_structure(candidate, candidate_path)
        return MissionSpec.from_dict(candidate)

    _validate_symbolic_structure(candidate, candidate_path)
    return SymbolicMissionSpec.from_dict(candidate)


def _validate_common_structure(candidate: Mapping[str, Any], path: str) -> str:
    common = {"mission_id", "objective", "planner_choice", "maneuvers", "source_authority"}
    missing = common - set(candidate)
    if missing:
        raise StructuredOutputFailure(
            [
                StructuralIssue(
                    "missing_required_field", f"{path}.{name}", "required field"
                )
                for name in sorted(missing)
            ]
        )

    for name in ("mission_id", "objective", "source_authority"):
        _text(candidate[name], f"{path}.{name}")

    choice_path = f"{path}.planner_choice"
    choice = _fields(
        candidate["planner_choice"],
        {"planner_id", "planning_profile"},
        choice_path,
    )
    profile = choice["planning_profile"]
    if not isinstance(profile, str):
        _fail(
            StructuralIssue(
                "invalid_type", f"{choice_path}.planning_profile", "string"
            )
        )
    if profile not in ("temporal", "symbolic"):
        _fail(
            StructuralIssue(
                "invalid_value",
                f"{choice_path}.planning_profile",
                '"symbolic" or "temporal"',
            )
        )
    planner_id = choice["planner_id"]
    if planner_id is not None and not isinstance(planner_id, str):
        _fail(
            StructuralIssue(
                "invalid_type", f"{choice_path}.planner_id", "string or null"
            )
        )
    return profile


def _validate_temporal_structure(candidate: Mapping[str, Any], path: str) -> None:
    _fields(
        candidate,
        {
            "mission_id",
            "objective",
            "planner_choice",
            "maneuvers",
            "horizon",
            "source_authority",
        },
        path,
    )
    _integer(candidate["horizon"], f"{path}.horizon")
    _validate_maneuvers(candidate["maneuvers"], path, "duration")


def _validate_symbolic_structure(candidate: Mapping[str, Any], path: str) -> None:
    _fields(
        candidate,
        {
            "mission_id",
            "objective",
            "planner_choice",
            "maneuvers",
            "source_authority",
            "domain_revision",
        },
        path,
    )
    _integer(candidate["domain_revision"], f"{path}.domain_revision")
    _validate_maneuvers(candidate["maneuvers"], path, "cost")


def _validate_maneuvers(value: object, path: str, measure: str) -> None:
    maneuvers_path = f"{path}.maneuvers"
    if not isinstance(value, (list, tuple)):
        _fail(StructuralIssue("invalid_type", maneuvers_path, "array"))
    for index, raw in enumerate(value):
        maneuver_path = f"{maneuvers_path}[{index}]"
        maneuver = _fields(
            raw,
            {"maneuver_id", "intent", "dependencies", measure},
            maneuver_path,
        )
        maneuver_id = maneuver["maneuver_id"]
        if not isinstance(maneuver_id, str):
            _fail(
                StructuralIssue(
                    "invalid_type", f"{maneuver_path}.maneuver_id", "string"
                )
            )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", maneuver_id) is None:
            _fail(
                StructuralIssue(
                    "invalid_value",
                    f"{maneuver_path}.maneuver_id",
                    "portable semantic identifier",
                )
            )
        _validate_intent(maneuver["intent"], maneuver_path)
        dependencies = maneuver["dependencies"]
        dependencies_path = f"{maneuver_path}.dependencies"
        if not isinstance(dependencies, (list, tuple)):
            _fail(StructuralIssue("invalid_type", dependencies_path, "array"))
        for dependency_index, dependency in enumerate(dependencies):
            if not isinstance(dependency, str):
                _fail(
                    StructuralIssue(
                        "invalid_type",
                        f"{dependencies_path}[{dependency_index}]",
                        "string",
                    )
                )
        _integer(maneuver[measure], f"{maneuver_path}.{measure}")


def _validate_intent(value: object, maneuver_path: str) -> None:
    intent_path = f"{maneuver_path}.intent"
    intent = _fields(value, {"action", "parameters"}, intent_path)
    _text(intent["action"], f"{intent_path}.action")
    parameters_path = f"{intent_path}.parameters"
    parameters = _object(intent["parameters"], parameters_path)
    for name, parameter in parameters.items():
        if not isinstance(name, str) or not name.strip():
            _fail(
                StructuralIssue(
                    "invalid_value", parameters_path, "non-empty string field names"
                )
            )
        parameter_path = f"{parameters_path}.{name}"
        if parameter is not None and not isinstance(parameter, (str, int, float, bool)):
            _fail(
                StructuralIssue(
                    "invalid_type", parameter_path, "JSON scalar"
                )
            )
        if isinstance(parameter, float) and not math.isfinite(parameter):
            _fail(
                StructuralIssue(
                    "invalid_value", parameter_path, "finite JSON number"
                )
            )


__all__ = ["MISSION_SPEC_SCHEMA", "create_hyper_agent", "DeepAgentsMissionInterpreter"]
