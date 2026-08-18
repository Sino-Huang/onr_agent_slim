from __future__ import annotations

from pathlib import Path

from onr.adapters.inprocess_transport import InProcessTransport
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
    class FakeAdapter:
        def submit(self, command: object) -> object:
            _ = command
            return None

    runtime.create_maneuver_control(FakeAdapter(), mission_id="mission")
    assert len(deep_models) == 4
    assert isinstance(deep_models[1], FakeChatModel)
    assert isinstance(deep_models[3], FakeChatModel)

    explicit = object()
    runtime.create_hyper_agent(model=explicit)
    assert deep_models[-1] is explicit
    provider = object()
    runtime.create_maneuver_control(FakeAdapter(), decision_provider=provider)
    assert len(deep_models) == 5
