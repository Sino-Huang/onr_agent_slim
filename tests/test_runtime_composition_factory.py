from __future__ import annotations

from pathlib import Path

from onr.agents import DeepAgentsDecisionProvider, DeepAgentsMissionInterpreter
from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.minizinc import MiniZincExecutor
from onr.application.symbolic_planning import SymbolicPlanning
from onr.application.temporal_planning import TemporalPlanning
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
import onr.runtime.composition as composition_module
from onr.runtime.config import AgentConfig, AgentsConfig, OutputStructureRetryConfig


class _FakeAdapter:
    def submit(self, command: object) -> object:
        _ = command
        return None


def _runtime(
    *, hyper_max_retries: int = 2, maneuver_max_retries: int = 1
) -> RuntimeComposition:
    config = RuntimeConfig(
        llm=LLMConfig("vllm", "http://127.0.0.1:11411/v1", "model", "EMPTY", 0.2),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1), PlannerConfig(Path(__file__), 1)
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", Path("transport")),
        storage=StorageConfig(Path("storage")),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=False,
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
            "timeout": 120.0,
            "max_retries": 1,
        }
    ]


def test_composition_uses_factory_only_without_explicit_model(monkeypatch) -> None:
    runtime = _runtime()
    configured_model = object()
    deep_models: list[object] = []

    class FakeChatModel:
        pass

    def fake_model_factory() -> FakeChatModel:
        deep_models.append(configured_model)
        return FakeChatModel()

    monkeypatch.setattr(
        composition_module.RuntimeComposition,
        "create_chat_model",
        lambda _runtime: fake_model_factory(),
    )
    monkeypatch.setattr(
        composition_module,
        "create_deep_hyper_agent",
        lambda **kwargs: deep_models.append(kwargs["model"]) or object(),
    )
    monkeypatch.setattr(
        composition_module,
        "create_deep_maneuver_control_agent",
        lambda **kwargs: deep_models.append(kwargs["model"]) or object(),
    )

    runtime.create_hyper_agent()
    runtime.create_maneuver_control(_FakeAdapter(), mission_id="mission")
    assert len(deep_models) == 4
    assert isinstance(deep_models[1], FakeChatModel)
    assert isinstance(deep_models[3], FakeChatModel)

    explicit = object()
    runtime.create_hyper_agent(model=explicit)
    assert deep_models[-1] is explicit
    provider = object()
    runtime.create_maneuver_control(_FakeAdapter(), decision_provider=provider)
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


def test_create_planners_uses_configured_executables_and_artifact_roots(tmp_path) -> None:
    planners = _runtime().create_planners(tmp_path / "planner-artifacts")

    assert isinstance(planners["temporal"], TemporalPlanning)
    assert isinstance(planners["symbolic"], SymbolicPlanning)
    temporal_executor = planners["temporal"]._executor
    symbolic_executor = planners["symbolic"]._executor
    assert isinstance(temporal_executor, MiniZincExecutor)
    assert isinstance(symbolic_executor, FastDownwardExecutor)
    assert temporal_executor.executable == Path(__file__)
    assert symbolic_executor.executable == Path(__file__)
    assert temporal_executor.artifact_root == (tmp_path / "planner-artifacts" / "temporal").resolve()
    assert symbolic_executor.artifact_root == (tmp_path / "planner-artifacts" / "symbolic").resolve()


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
        _FakeAdapter(),
        model=object(),
        mission_id="mission",
        memory_store=object(),
        system_prompt="return one physical decision",
    )

    assert captured["system_prompt"] == "return one physical decision"


def test_default_hyper_agent_uses_configured_interpreter_retry_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        composition_module,
        "create_deep_hyper_agent",
        lambda **_kwargs: object(),
    )

    service = _runtime(hyper_max_retries=7, maneuver_max_retries=9).create_hyper_agent(
        model=object()
    )

    assert isinstance(service.interpreter, DeepAgentsMissionInterpreter)
    assert service.interpreter.max_retries == 7


def test_default_maneuver_control_uses_configured_provider_retry_limit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        composition_module,
        "create_deep_maneuver_control_agent",
        lambda **_kwargs: object(),
    )

    service = _runtime(hyper_max_retries=7, maneuver_max_retries=9).create_maneuver_control(
        _FakeAdapter(),
        model=object(),
        mission_id="mission",
    )

    assert isinstance(service.decision_provider, DeepAgentsDecisionProvider)
    assert service.decision_provider.max_retries == 9


def test_explicit_agent_providers_bypass_default_retry_wiring(monkeypatch) -> None:
    runtime = _runtime(hyper_max_retries=7, maneuver_max_retries=9)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default provider construction must not run")

    monkeypatch.setattr(composition_module, "DeepAgentsMissionInterpreter", fail)
    monkeypatch.setattr(composition_module, "DeepAgentsDecisionProvider", fail)
    monkeypatch.setattr(composition_module.RuntimeComposition, "create_chat_model", fail)

    interpreter = lambda _mission_input: None
    decision_provider = object()

    hyper_agent = runtime.create_hyper_agent(interpreter=interpreter)
    maneuver_control = runtime.create_maneuver_control(
        _FakeAdapter(), decision_provider=decision_provider
    )

    assert hyper_agent.interpreter is interpreter
    assert maneuver_control.decision_provider is decision_provider
