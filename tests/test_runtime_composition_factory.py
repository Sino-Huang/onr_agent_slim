from __future__ import annotations

from pathlib import Path

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


class _FakeAdapter:
    def submit(self, command: object) -> object:
        _ = command
        return None


def _runtime() -> RuntimeComposition:
    config = RuntimeConfig(
        llm=LLMConfig("vllm", "http://127.0.0.1:11411/v1", "model", "EMPTY", 0.2),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1), PlannerConfig(Path(__file__), 1)
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", Path("transport")),
        storage=StorageConfig(Path("storage")),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
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
