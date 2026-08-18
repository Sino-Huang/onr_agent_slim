"""DeepAgents integration boundary for Hyper Agent intake."""

from __future__ import annotations

from typing import Any

from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import MissionSpec, SymbolicMissionSpec


def create_hyper_agent(*, model: Any, system_prompt: str | None = None) -> object:
    """Create an ephemeral Deep Agent configured for structured mission intake."""

    from deepagents import create_deep_agent

    kwargs: dict[str, Any] = {
        "model": model,
        # DeepAgents accepts a schema through response_format and returns it as
        # ``structured_response``.  The strict domain parser remains the final
        # validation gate below.
        "response_format": dict,
    }
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    # No checkpointer or memory: intake is an ephemeral interpretation.
    return create_deep_agent(**kwargs)


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
