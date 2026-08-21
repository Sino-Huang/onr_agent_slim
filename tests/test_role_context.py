from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import yaml

from onr.adapters.mission_memory import FileMissionMemoryStore
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.agents.hyper_agent import (
    DeepAgentsPlanningIntentInterpreter,
    create_planning_intent_agent,
)
from onr.agents.hyper_workflow import create_hyper_workflow_agent
from onr.agents.maneuver_control import create_maneuver_control_agent
from onr.agents.role_context import RoleEpisode
from onr.contracts.hyper_agent import MissionInput

_REPO_ROOT = Path(__file__).resolve().parents[1]


class PublicFakeDeepAgent:
    """Deterministic DeepAgents boundary exposing public state contents."""

    def __init__(self, skills: object = ()) -> None:
        self.read_context: Callable[[], str | None] = lambda: None
        self.skills_metadata = skills

    def invoke(self, state: object) -> dict[str, object]:
        _ = state
        return {
            "memory_contents": self.read_context(),
            "skills_metadata": self.skills_metadata,
        }


def _install_skills(root: Path) -> FilesystemRoleSkillCatalog:
    for role, version in (("hyper-agent", "2.0.0"), ("maneuver-control", "3.0.0")):
        skill = root / role / version
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {role}\ndescription: Support {role}.\nversion: '{version}'\n---\nUse {role}.\n",
            encoding="utf-8",
        )
    return FilesystemRoleSkillCatalog(root)


def test_shipped_catalog_selects_all_role_skills_in_operational_order() -> None:
    catalog = FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills")

    hyper = catalog.select_all("hyper-agent")
    maneuver = catalog.select_all("maneuver-control")

    assert [skill.role for skill in hyper] == [
        "mission-parsing",
        "planner-selection",
        "creating-minizinc-problem-files",
        "creating-pddl-problem-files",
        "detect-and-replan",
        "creating-statechart-files",
    ]
    assert [skill.role for skill in maneuver] == [
        "decision-cycle",
        "physical-maneuver-selection",
        "hyper-coordination",
    ]
    assert [skill.version for skill in (*hyper, *maneuver)] == [
        "1.6.0",
        "1.4.0",
        "1.22.0",
        "1.2.0",
        "1.1.0",
        "1.1.0",
        "1.1.0",
        "1.1.0",
        "1.1.0",
    ]
    assert [skill.path.relative_to(catalog.root).as_posix() for skill in hyper] == [
        "hyper/mission-parsing",
        "hyper/planner-selection",
        "hyper/creating-minizinc-problem-files",
        "hyper/creating-pddl-problem-files",
        "hyper/detect-and-replan",
        "hyper/creating-statechart-files",
    ]
    assert [skill.path.relative_to(catalog.root).as_posix() for skill in maneuver] == [
        "maneuver-control/decision-cycle",
        "maneuver-control/physical-maneuver-selection",
        "maneuver-control/hyper-coordination",
    ]
    for skill in (*hyper, *maneuver):
        _, front_matter, _ = (skill.path / "SKILL.md").read_text(encoding="utf-8").split(
            "---", 2
        )
        metadata = yaml.safe_load(front_matter)
        assert isinstance(metadata, dict)
        assert isinstance(metadata.get("description"), str)
        assert metadata["description"].strip()


def test_deep_agents_receive_all_shipped_role_skill_paths(monkeypatch) -> None:
    import deepagents

    created: list[dict[str, object]] = []

    def fake_create_deep_agent(**kwargs: object) -> PublicFakeDeepAgent:
        created.append(kwargs)
        return PublicFakeDeepAgent(kwargs.get("skills", ()))

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)
    catalog = FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills")
    create_planning_intent_agent(
        model=object(),
        mission_id="mission-1",
        skill_catalog=catalog,
        backend_root=_REPO_ROOT,
    )
    create_maneuver_control_agent(
        model=object(),
        mission_id="mission-1",
        skill_catalog=catalog,
        backend_root=_REPO_ROOT,
    )

    hyper_skills = [
        "/conf/skills/hyper/mission-parsing",
        "/conf/skills/hyper/planner-selection",
        "/conf/skills/hyper/creating-minizinc-problem-files",
        "/conf/skills/hyper/creating-pddl-problem-files",
        "/conf/skills/hyper/detect-and-replan",
        "/conf/skills/hyper/creating-statechart-files",
    ]
    maneuver_skills = [
        "/conf/skills/maneuver-control/decision-cycle",
        "/conf/skills/maneuver-control/physical-maneuver-selection",
        "/conf/skills/maneuver-control/hyper-coordination",
    ]
    assert created[0]["skills"] == ["/conf/skills/hyper"]
    assert created[1]["skills"] == ["/conf/skills/maneuver-control"]
    for kwargs, selected_skills in zip(created, (hyper_skills, maneuver_skills)):
        skill_sources = kwargs["skills"]
        permissions = kwargs["permissions"]
        assert isinstance(skill_sources, list) and isinstance(permissions, list)
        assert [permission.paths[0] for permission in permissions[:-1]] == selected_skills
        assert all(permission.mode == "deny" for permission in permissions)


def test_only_hyper_agent_receives_todo_list_middleware(monkeypatch) -> None:
    import deepagents
    from langchain.agents.middleware import TodoListMiddleware

    created: list[dict[str, object]] = []

    def fake_create_deep_agent(**kwargs: object) -> object:
        created.append(kwargs)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)

    create_planning_intent_agent(model=object())
    create_maneuver_control_agent(model=object())

    hyper_middleware = created[0].get("middleware")
    maneuver_middleware = created[1].get("middleware", [])
    assert isinstance(hyper_middleware, list)
    assert isinstance(maneuver_middleware, list)
    assert [type(middleware) for middleware in hyper_middleware] == [TodoListMiddleware]
    assert not any(
        isinstance(middleware, TodoListMiddleware) for middleware in maneuver_middleware
    )


def test_debug_agent_profile_uses_selected_skill_metadata_and_interpreter_callback(
    monkeypatch, tmp_path: Path
) -> None:
    import deepagents

    callback = object()
    profiles: list[tuple[str, list[dict[str, str]], list[str]]] = []

    class Recorder:
        def record_profile(
            self, role: str, skills: list[dict[str, str]], tools: list[str]
        ) -> None:
            profiles.append((role, skills, tools))

        def callback_for(self, role: str) -> object:
            assert role == "hyper-agent"
            return callback

    class Agent:
        def __init__(self) -> None:
            self.config: object = None

        def invoke(self, _: object, *, config: object = None) -> dict[str, object]:
            self.config = config
            return {
                "structured_response": {
                    "mission_id": "mission-1",
                    "source_authority": "mission-control",
                    "objective": "Survey",
                    "planner_choice": {
                        "planning_profile": "temporal",
                        "planner_id": "minizinc",
                    },
                    "rationale": "The Mission requires temporal planning.",
                    "details": {},
                }
            }

    created = Agent()
    monkeypatch.setattr(deepagents, "create_deep_agent", lambda **_: created)
    model = SimpleNamespace(_agent_debug_recorder=Recorder())
    skills = _install_skills(tmp_path / "skills")
    agent = create_planning_intent_agent(
        model=model,
        mission_id="mission-1",
        skill_catalog=skills,
        backend_root=tmp_path,
    )

    result = DeepAgentsPlanningIntentInterpreter(agent).interpret(
        MissionInput("mission-1", "Survey", "mission-control")
    )

    selected_path = tmp_path / "skills/hyper-agent/2.0.0"
    assert profiles == [
        (
            "hyper-agent",
            [
                {
                    "name": "hyper-agent",
                    "version": "2.0.0",
                    "path": str(selected_path),
                }
            ],
            [],
        )
    ]
    assert result.mission_id == "mission-1"
    assert created.config == {"callbacks": [callback]}
    assert cast(Any, agent)._onr_debug_callback is callback


def _make_role_agents(
    monkeypatch,
    memory: FileMissionMemoryStore,
    skills: FilesystemRoleSkillCatalog,
    mission_id: str,
) -> tuple[RoleEpisode, RoleEpisode, list[PublicFakeDeepAgent]]:
    import deepagents

    created: list[PublicFakeDeepAgent] = []

    def fake_create_deep_agent(**kwargs: object) -> PublicFakeDeepAgent:
        agent = PublicFakeDeepAgent(kwargs.get("skills", ()))
        created.append(agent)
        return agent

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)
    hyper = create_planning_intent_agent(
        model=object(),
        mission_id=mission_id,
        memory_store=memory,
        skill_catalog=skills,
        backend_root=memory.root.parent,
    )
    maneuver = create_maneuver_control_agent(
        model=object(),
        mission_id=mission_id,
        memory_store=memory,
        skill_catalog=skills,
        backend_root=memory.root.parent,
    )
    hyper_episode = cast(RoleEpisode, hyper)
    maneuver_episode = cast(RoleEpisode, maneuver)
    hyper_agent = created[-2]
    maneuver_agent = created[-1]
    hyper_agent.read_context = hyper_episode.read_memory
    maneuver_agent.read_context = maneuver_episode.read_memory
    return hyper_episode, maneuver_episode, [hyper_agent, maneuver_agent]


def _state(episode: RoleEpisode) -> dict[str, object]:
    return cast(dict[str, object], episode.invoke({}))


def _memory_state(episode: RoleEpisode) -> str:
    value = _state(episode)["memory_contents"]
    assert isinstance(value, str)
    return value


def test_role_agents_persist_public_memory_and_isolate_two_missions_and_roles(
    monkeypatch, tmp_path: Path
) -> None:
    memory = FileMissionMemoryStore(tmp_path / "memory")
    skills = _install_skills(tmp_path / "skills")
    first_hyper, first_maneuver, _ = _make_role_agents(monkeypatch, memory, skills, "mission-1")
    second_hyper, second_maneuver, _ = _make_role_agents(monkeypatch, memory, skills, "mission-2")

    first_hyper.write_memory("mission-1 hyper ground truth")
    first_maneuver.write_memory("mission-1 maneuver context")
    second_hyper.write_memory("mission-2 hyper context")
    second_maneuver.write_memory("mission-2 maneuver context")

    restarted = FileMissionMemoryStore(tmp_path / "memory")
    resumed_hyper, resumed_maneuver, resumed_agents = _make_role_agents(
        monkeypatch, restarted, skills, "mission-1"
    )
    resumed_other_hyper, resumed_other_maneuver, _ = _make_role_agents(
        monkeypatch, restarted, skills, "mission-2"
    )

    assert _memory_state(resumed_hyper) == "mission-1 hyper ground truth"
    assert _memory_state(resumed_maneuver) == "mission-1 maneuver context"
    assert _memory_state(resumed_other_hyper) == "mission-2 hyper context"
    assert _memory_state(resumed_other_maneuver) == "mission-2 maneuver context"
    assert "mission-1" not in _memory_state(resumed_other_hyper)
    assert "mission-1" not in _memory_state(resumed_other_maneuver)
    assert "mission-1 maneuver" not in _memory_state(resumed_hyper)
    assert "mission-1 hyper" not in _memory_state(resumed_maneuver)
    assert _state(resumed_hyper)["skills_metadata"] == ["/skills/hyper-agent"]
    assert _state(resumed_maneuver)["skills_metadata"] == ["/skills/maneuver-control"]
    assert resumed_agents[0].read_context() == "mission-1 hyper ground truth"


def test_role_context_policy_allows_only_current_memory_and_denies_skills_and_other_writes(
    monkeypatch, tmp_path: Path
) -> None:
    import deepagents

    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs: object) -> PublicFakeDeepAgent:
        captured.update(kwargs)
        return PublicFakeDeepAgent(kwargs.get("skills", ()))

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)
    memory = FileMissionMemoryStore(tmp_path / "memory")
    create_planning_intent_agent(
        model=object(),
        mission_id="mission-1",
        memory_store=memory,
        skill_catalog=_install_skills(tmp_path / "skills"),
        backend_root=tmp_path,
    )

    permissions = captured["permissions"]
    assert isinstance(permissions, list)
    assert any(permission.mode == "allow" for permission in permissions)
    assert any(permission.paths == ["/skills/hyper-agent/2.0.0", "/skills/hyper-agent/2.0.0/**"] for permission in permissions)
    assert captured["skills"] == ["/skills/hyper-agent"]
    assert permissions[-1].mode == "deny"
    assert permissions[-1].paths == ["/**"]
    assert permissions[-2].mode == "allow"


def test_hyper_workflow_allows_only_its_planner_workspace_write_scope(
    tmp_path: Path, monkeypatch
) -> None:
    import deepagents

    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs: object) -> PublicFakeDeepAgent:
        captured.update(kwargs)
        return PublicFakeDeepAgent(kwargs.get("skills", ()))

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)
    create_hyper_workflow_agent(
        model=object(),
        system_prompt="Hyper workflow prompt.",
        mission_id="mission-1",
        memory_store=FileMissionMemoryStore(tmp_path / "memory"),
        skill_catalog=_install_skills(tmp_path / "skills"),
        backend_root=tmp_path,
        planner_workspace_location="/planner-artifacts/workspace",
    )

    permissions = captured["permissions"]
    assert isinstance(permissions, list)
    assert any(
        permission.mode == "allow"
        and permission.paths
        == [
            "/planner-artifacts/workspace",
            "/planner-artifacts/workspace/**",
        ]
        for permission in permissions
    )
    assert permissions[-1].mode == "deny"
    assert permissions[-1].paths == ["/**"]


def test_event_accounting_patrol_routes_to_information_gain_example() -> None:
    mission_skill = (
        _REPO_ROOT / "conf/skills/hyper/mission-parsing/SKILL.md"
    ).read_text(encoding="utf-8")
    minizinc_skill = (
        _REPO_ROOT
        / "conf/skills/hyper/creating-minizinc-problem-files/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "logical role" in mission_skill
    assert "without predicting their" in mission_skill
    assert "field names or nesting" in mission_skill
    assert "Do not put example JSON paths" in mission_skill
    assert "1 - probability_risk" in mission_skill
    assert "examples/event-information-patrol/model.mzn" in minizinc_skill
    assert "examples/event-information-patrol/data.dzn" in minizinc_skill
    assert "30 m FoV radius" in minizinc_skill
    assert "20 m/s maximum velocity" in minizinc_skill


def test_planner_generation_skills_forbid_known_invalid_file_patterns() -> None:
    minizinc_skill = (
        _REPO_ROOT
        / "conf/skills/hyper/creating-minizinc-problem-files/SKILL.md"
    ).read_text(encoding="utf-8")
    pddl_skill = (
        _REPO_ROOT / "conf/skills/hyper/creating-pddl-problem-files/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "## Must Not Do" in minizinc_skill
    assert "int: max_velocity;" in minizinc_skill
    assert "exact `correction_message`" in minizinc_skill
    assert "names and nesting are flexible" in minizinc_skill
    assert "Do not assume environment keys" in minizinc_skill
    assert "## Must Not Do" in pddl_skill
    assert "undeclared predicates" in pddl_skill
    assert "exact correction message" in pddl_skill
