"""DeepAgents integration boundary for Hyper Agent intake."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

from langchain.agents.middleware import TodoListMiddleware

from onr.agents.role_context import MissionRoleContext, RoleEpisode
from onr.agents.structured_output import (
    StructuralIssue,
    StructuredOutputFailure,
    invoke_with_structured_output_recovery,
)
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning_intent import PlanningIntent

PLANNING_INTENT_SCHEMA: dict[str, Any] = {
    "title": "PlanningIntentCandidate",
    "type": "object",
    "properties": {
        "mission_id": {"type": "string"},
        "source_authority": {"type": "string"},
        "objective": {"type": "string"},
        "planner_choice": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "planning_profile": {"enum": ["temporal"]},
                        "planner_id": {"enum": ["minizinc"]},
                    },
                    "required": ["planning_profile", "planner_id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "planning_profile": {"enum": ["symbolic"]},
                        "planner_id": {"enum": ["fast-downward"]},
                    },
                    "required": ["planning_profile", "planner_id"],
                    "additionalProperties": False,
                },
            ],
        },
        "rationale": {"type": "string"},
        "details": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Flexible mission context only; exclude planner assets, generated assets, "
                "solver input/output, verification evidence, normalized plans, and "
                "pre-planner specification envelopes."
            ),
        },
    },
    "required": [
        "mission_id",
        "source_authority",
        "objective",
        "planner_choice",
        "rationale",
        "details",
    ],
    "additionalProperties": False,
}


def create_planning_intent_agent(
    *,
    model: Any,
    system_prompt: str | None = None,
    mission_id: str | None = None,
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
) -> object:
    """Create a Deep Agent configured for PlanningIntent interpretation."""

    return _create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        response_format=PLANNING_INTENT_SCHEMA,
        mission_id=mission_id,
        role="hyper-agent",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
        middleware=[TodoListMiddleware()],
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
    middleware: list[Any] | None = None,
    tools: list[Any] | None = None,
    writable_paths: list[str] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: object | None = None,
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
    if middleware is not None:
        kwargs["middleware"] = middleware
    if tools is not None:
        kwargs["tools"] = tools
    if context_schema is not None:
        kwargs["context_schema"] = context_schema
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer

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
    selected_skill_profiles: list[dict[str, str]] = []
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
            selected_role = getattr(selected, "role", None)
            selected_version = getattr(selected, "version", None)
            if not isinstance(selected_role, str) or not isinstance(selected_version, str):
                raise TypeError("Role Skill catalog returned invalid metadata")
            selected_skill_profiles.append(
                {
                    "name": selected_role,
                    "version": selected_version,
                    "path": str(selected_path),
                }
            )
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
        for writable_path in writable_paths or []:
            if not writable_path.startswith("/") or ".." in Path(writable_path).parts:
                raise ValueError("agent writable path must be an absolute virtual path")
            scope = writable_path.rstrip("/") or "/"
            hard_permissions.append(
                FilesystemPermission(["write"], [scope, f"{scope}/**"], mode="allow")
            )
        hard_permissions.append(FilesystemPermission(["write"], ["/**"], mode="deny"))
        kwargs["permissions"] = hard_permissions

    agent = create_deep_agent(**kwargs)
    recorder = getattr(model, "_agent_debug_recorder", None)
    if recorder is not None:
        record_profile = getattr(recorder, "record_profile", None)
        callback_for = getattr(recorder, "callback_for", None)
        if not callable(record_profile) or not callable(callback_for):
            raise TypeError("agent debug recorder is invalid")
        record_profile(role, selected_skill_profiles, [])
        setattr(agent, "_onr_debug_callback", callback_for(role))
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


T = TypeVar("T")


def _interpret_with_structured_output_recovery(
    agent: object,
    mission_input: MissionInput,
    max_retries: int,
    parse: Callable[[object], T],
) -> T:
    invoke = getattr(agent, "invoke", None)
    if not callable(invoke):
        raise TypeError("Deep Hyper Agent must expose invoke")
    callback = getattr(agent, "_onr_debug_callback", None)

    def invoke_with_callback(state: Mapping[str, object]) -> object:
        if callback is None:
            return invoke(state)
        return invoke(state, config={"callbacks": [callback]})

    return invoke_with_structured_output_recovery(
        invoke_with_callback,
        mission_input.to_dict(),
        max_retries,
        parse,
    )


class DeepAgentsPlanningIntentInterpreter:
    """Adapt a Deep Agent response to a trusted PlanningIntent."""

    def __init__(self, agent: object, max_retries: int = 4) -> None:
        self.agent = agent
        self.max_retries = max_retries

    def interpret(self, mission_input: MissionInput) -> PlanningIntent:
        if not isinstance(mission_input, MissionInput):
            raise TypeError("planning intent interpreter requires a MissionInput")
        return _interpret_with_structured_output_recovery(
            self.agent,
            mission_input,
            self.max_retries,
            lambda response: _parse_planning_intent_response(response, mission_input),
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


def _structured_response(response: object) -> Mapping[str, Any]:
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

    return _object(structured, "$.structured_response")


def _parse_planning_intent_response(
    response: object, mission_input: MissionInput
) -> PlanningIntent:
    path = "$.structured_response"
    candidate = _fields(
        _structured_response(response),
        {
            "mission_id",
            "source_authority",
            "objective",
            "planner_choice",
            "rationale",
            "details",
        },
        path,
    )
    for name in ("mission_id", "source_authority", "objective", "rationale"):
        _text(candidate[name], f"{path}.{name}")
    _validate_planning_intent_choice(candidate["planner_choice"], f"{path}.planner_choice")
    details_path = f"{path}.details"
    details = _object(candidate["details"], details_path)
    mission_text = mission_input.mission_text.casefold()
    details_text = json.dumps(details, ensure_ascii=False).casefold()
    if (
        "fov" in mission_text or "field of view" in mission_text
    ) and "fov" not in details_text and "field of view" not in details_text:
        _fail(
            StructuralIssue(
                "invalid_value",
                details_path,
                "the Mission Intent's FoV observation objective",
            )
        )

    if candidate["mission_id"] != mission_input.mission_id:
        raise ValueError("planning intent mission ID does not match mission input")
    if candidate["source_authority"] != mission_input.source_authority:
        raise ValueError("planning intent source authority does not match mission input")

    return PlanningIntent.from_dict(
        {
            **candidate,
            "schema_version": 1,
        }
    )


def _validate_planning_intent_choice(value: object, path: str) -> None:
    choice = _fields(value, {"planner_id", "planning_profile"}, path)
    profile = choice["planning_profile"]
    if not isinstance(profile, str):
        _fail(StructuralIssue("invalid_type", f"{path}.planning_profile", "string"))
    if profile not in ("temporal", "symbolic"):
        _fail(
            StructuralIssue(
                "invalid_value",
                f"{path}.planning_profile",
                '"symbolic" or "temporal"',
            )
        )
    planner_id = choice["planner_id"]
    if planner_id is not None and not isinstance(planner_id, str):
        _fail(StructuralIssue("invalid_type", f"{path}.planner_id", "string or null"))
    if (profile, planner_id) not in (
        ("temporal", "minizinc"),
        ("symbolic", "fast-downward"),
    ):
        _fail(
            StructuralIssue(
                "invalid_value",
                path,
                "a configured planner",
            )
        )


__all__ = [
    "PLANNING_INTENT_SCHEMA",
    "create_planning_intent_agent",
    "DeepAgentsPlanningIntentInterpreter",
]
