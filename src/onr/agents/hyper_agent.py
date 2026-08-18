"""DeepAgents integration boundary for Hyper Agent intake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onr.agents.role_context import MissionRoleContext, RoleEpisode
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import MissionSpec, SymbolicMissionSpec


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
        response_format=dict,
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
    if mission_id is not None and skill_catalog is not None:
        select = getattr(skill_catalog, "select", None)
        if not callable(select):
            raise TypeError("Role Skill catalog must expose select")
        selected = select(role, skill_version)
        selected_path = getattr(selected, "path", None)
        if not isinstance(selected_path, Path):
            raise TypeError("Role Skill catalog returned an invalid selection")
        selected_skills.append(_skill_agent_path(selected_path, backend_root))

    if selected_skills:
        kwargs["skills"] = selected_skills

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

    def __init__(self, agent: object) -> None:
        self.agent = agent

    def interpret(self, mission_input: MissionInput) -> MissionSpec | SymbolicMissionSpec:
        if not isinstance(mission_input, MissionInput):
            raise TypeError("mission interpreter requires a MissionInput")
        invoke = getattr(self.agent, "invoke", None)
        if not callable(invoke):
            raise TypeError("Deep Hyper Agent must expose invoke")
        response = invoke({"mission_input": mission_input.to_dict(), **mission_input.to_dict()})
        structured = response.get("structured_response") if isinstance(response, dict) else response
        model_dump = getattr(structured, "model_dump", None)
        if callable(model_dump):
            structured = model_dump()
        if not isinstance(structured, dict):
            raise ValueError("Deep Hyper Agent did not return a structured Mission Specification")
        if "mission_spec" in structured and len(structured) == 1:
            structured = structured["mission_spec"]
        if not isinstance(structured, dict):
            raise ValueError("structured Mission Specification must be an object")

        try:
            return MissionSpec.from_dict(structured)
        except ValueError as temporal_error:
            try:
                return SymbolicMissionSpec.from_dict(structured)
            except ValueError as symbolic_error:
                raise ValueError("structured response is not a valid MissionSpec") from symbolic_error


__all__ = ["create_hyper_agent", "DeepAgentsMissionInterpreter"]
