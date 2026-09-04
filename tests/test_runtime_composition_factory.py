from __future__ import annotations

import json
from pathlib import Path

from deepagents.backends import LocalShellBackend

import onr.runtime.composition as composition_module
from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.val import VALPlanValidator
from onr.agents import (
    DeepAgentsDecisionProvider,
    DeepAgentsHyperWorkflow,
    DeepAgentsPlanningIntentInterpreter,
    HyperWorkflowContext,
)
from onr.application.human_decisions import HumanDecisionCoordinator
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.transport import TransportEvent
from onr.runtime import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeComposition,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)
from onr.runtime.config import AgentConfig, AgentsConfig, OutputStructureRetryConfig


def _runtime(
    *, hyper_max_retries: int = 2, maneuver_max_retries: int = 1
) -> RuntimeComposition:
    config = RuntimeConfig(
        llm=LLMConfig("vllm", "http://127.0.0.1:11411/v1", "model", "EMPTY", 0.2),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1),
            PlannerConfig(Path(__file__), 1, Path(__file__)),
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", Path("transport")),
        storage=StorageConfig(Path("storage")),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=False,
        agent_name="test-agent",
        agents=AgentsConfig(
            hyper_agent=AgentConfig(
                OutputStructureRetryConfig(max_retries=hyper_max_retries)
            ),
            maneuver_control=AgentConfig(
                OutputStructureRetryConfig(max_retries=maneuver_max_retries)
            ),
        ),
    )
    return RuntimeComposition(config, InProcessTransport())


def test_chat_model_factory_uses_runtime_llm(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeChatModel:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(composition_module, "ChatOpenAI", FakeChatModel)
    model = _runtime().create_chat_model()

    assert isinstance(model, FakeChatModel)
    assert calls == [
        {
            "base_url": "http://127.0.0.1:11411/v1",
            "model": "model",
            "api_key": "EMPTY",
            "temperature": 0.2,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "reasoning_effort": "medium",
            "streaming": True,
            "timeout": 800.0,
            "max_retries": 0,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.0,
            },
        }
    ]


def test_composition_uses_factory_only_without_explicit_model(monkeypatch) -> None:
    runtime = _runtime()
    configured_model = object()
    deep_models: list[object] = []
    factory_calls: list[dict[str, object]] = []

    class FakeChatModel:
        pass

    def fake_model_factory(
        *, mission_id: str | None, debug_scope: str
    ) -> FakeChatModel:
        factory_calls.append(
            {"mission_id": mission_id, "debug_scope": debug_scope}
        )
        deep_models.append(configured_model)
        return FakeChatModel()

    def fake_create_chat_model(
        _runtime: RuntimeComposition,
        *,
        mission_id: str | None = None,
        debug_scope: str = "runtime",
    ) -> FakeChatModel:
        return fake_model_factory(
            mission_id=mission_id,
            debug_scope=debug_scope,
        )

    monkeypatch.setattr(
        composition_module.RuntimeComposition,
        "create_chat_model",
        fake_create_chat_model,
    )
    monkeypatch.setattr(
        composition_module,
        "create_planning_intent_agent",
        lambda **kwargs: deep_models.append(kwargs["model"]) or object(),
    )
    monkeypatch.setattr(
        composition_module,
        "create_deep_maneuver_control_agent",
        lambda **kwargs: deep_models.append(kwargs["model"]) or object(),
    )

    runtime.create_hyper_agent()
    runtime.create_maneuver_control(mission_id="mission")
    assert factory_calls == [
        {"mission_id": None, "debug_scope": "hyper-agent"},
        {"mission_id": "mission", "debug_scope": "maneuver-control"},
    ]
    assert len(deep_models) == 4
    assert isinstance(deep_models[1], FakeChatModel)
    assert isinstance(deep_models[3], FakeChatModel)

    explicit = object()
    runtime.create_hyper_agent(model=explicit)
    assert deep_models[-1] is explicit
    provider = object()
    runtime.create_maneuver_control(decision_provider=provider)
    assert len(factory_calls) == 2
    assert len(deep_models) == 5


def test_verify_llm_reachability_uses_configured_values(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_probe(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(composition_module, "probe_vllm_reachability", fake_probe)
    _runtime().verify_llm_reachability(timeout=1.5)

    assert calls == [
        (
            ("http://127.0.0.1:11411/v1", "model"),
            {
                "api_key": "EMPTY",
                "temperature": 0.2,
                "timeout": 1.5,
            },
        )
    ]


def test_create_human_decision_coordinator_uses_runtime_storage() -> None:
    coordinator = _runtime().create_human_decision_coordinator()

    assert isinstance(coordinator, HumanDecisionCoordinator)


def test_create_maneuver_control_passes_optional_system_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        composition_module,
        "create_deep_maneuver_control_agent",
        fake_create_agent,
    )
    _runtime().create_maneuver_control(
        model=object(),
        mission_id="mission",
        memory_store=object(),
        system_prompt="return one physical decision",
    )

    assert captured["system_prompt"] == "return one physical decision"


def test_default_hyper_agent_uses_configured_interpreter_retry_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        composition_module, "create_planning_intent_agent", lambda **_kwargs: object()
    )

    service = _runtime(hyper_max_retries=7, maneuver_max_retries=9).create_hyper_agent(
        model=object()
    )

    assert isinstance(service.planning_intent_interpreter, DeepAgentsPlanningIntentInterpreter)
    assert service.planning_intent_interpreter.max_retries == 7


def test_runtime_composes_one_workflow_level_hyper_deep_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeGraph:
        def invoke(self, *args: object, **kwargs: object) -> object:
            _ = args, kwargs
            return {}

    graph = FakeGraph()

    def fake_create_workflow_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return graph

    monkeypatch.setattr(
        composition_module,
        "create_hyper_workflow_agent",
        fake_create_workflow_agent,
    )
    model = object()
    memory_store = object()
    skill_catalog = object()
    checkpointer = object()

    workflow = _runtime().create_hyper_workflow(
        model=model,
        system_prompt="workflow prompt",
        mission_id="mission",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version="1.0.0",
        backend_root=Path("backend"),
        checkpointer=checkpointer,
        artifact_root=Path("backend/planner-artifacts"),
    )

    assert isinstance(workflow, DeepAgentsHyperWorkflow)
    assert workflow.agent is graph
    assert captured == {
        "model": model,
        "system_prompt": "workflow prompt",
        "mission_id": "mission",
        "memory_store": memory_store,
        "skill_catalog": skill_catalog,
        "skill_version": "1.0.0",
        "backend_root": Path("backend"),
        "checkpointer": checkpointer,
        "planner_workspace_location": "planner-artifacts/workspace",
    }


def test_runtime_builds_direct_external_planner_context(
    tmp_path: Path,
) -> None:
    mission = MissionInput("mission", "Observe the harbor.", "mission-control")
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={"graph": {"entities": []}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 1},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    environment_file = tmp_path / "environment.json"
    environment_file.write_text(
        json.dumps(scene.to_dict()["payload"]),
        encoding="utf-8",
    )

    context = _runtime(hyper_max_retries=7).create_hyper_workflow_context(
        mission,
        snapshot,
        scene,
        environment_file,
        artifact_root=tmp_path / "planner-artifacts",
        backend_root=tmp_path,
    )

    assert isinstance(context, HyperWorkflowContext)
    assert isinstance(context.minizinc_planner, MiniZincExecutor)
    assert isinstance(context.fast_downward_planner, FastDownwardExecutor)
    assert isinstance(context.val_validator, VALPlanValidator)
    assert context.max_planner_attempts == 8
    assert context.artifact_root == (tmp_path / "planner-artifacts").resolve()
    assert context.planner_workspace_location == "/planner-artifacts/workspace"
    assert context.planner_shell_workspace_location == "planner-artifacts/workspace"
    assert context.environment_file_location == "/environment.json"
    assert context.environment_shell_location == "environment.json"

    backend = LocalShellBackend(
        root_dir=context.backend_root,
        virtual_mode=True,
        inherit_env=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    generator_location = f"{context.planner_workspace_location}/001/generate_statechart.py"
    assert backend.write(generator_location, "print('statechart')\n").error is None
    assert (
        backend.execute(
            "test -f "
            f"{context.planner_shell_workspace_location}/001/generate_statechart.py"
        ).exit_code
        == 0
    )


def test_default_maneuver_control_uses_configured_provider_retry_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        composition_module,
        "create_deep_maneuver_control_agent",
        lambda **_kwargs: object(),
    )

    service = _runtime(hyper_max_retries=7, maneuver_max_retries=9).create_maneuver_control(
        model=object(),
        mission_id="mission",
    )

    assert isinstance(service.decision_provider, DeepAgentsDecisionProvider)
    assert service.decision_provider.max_retries == 9


def test_explicit_agent_providers_bypass_default_retry_wiring(monkeypatch) -> None:
    runtime = _runtime(hyper_max_retries=7, maneuver_max_retries=9)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default provider construction must not run")

    monkeypatch.setattr(composition_module, "DeepAgentsPlanningIntentInterpreter", fail)
    monkeypatch.setattr(composition_module, "DeepAgentsDecisionProvider", fail)
    monkeypatch.setattr(composition_module.RuntimeComposition, "create_chat_model", fail)

    interpreter = lambda _mission_input: None
    decision_provider = object()

    hyper_agent = runtime.create_hyper_agent(interpreter=interpreter)
    maneuver_control = runtime.create_maneuver_control(
        decision_provider=decision_provider
    )

    assert hyper_agent.planning_intent_interpreter is interpreter
    assert maneuver_control.decision_provider is decision_provider
