"""DeepAgents boundary for Maneuver Control.

The application service remains deterministic and dependency-free.  This
module is the only place that knows how to construct a Deep Agent; tests and
deployments may instead provide a plain decision provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.maneuver_control import InvocationOverlay, ManeuverControlDecision
from onr.agents.hyper_agent import _create_deep_agent


def create_maneuver_control_agent(
    *,
    model: Any,
    system_prompt: str | None = None,
    mission_id: str | None = None,
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
) -> object:
    """Create a DeepAgents model wrapper with optional role context."""

    return _create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        mission_id=mission_id,
        role="maneuver-control",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
    )


class DeepAgentsDecisionProvider:
    """Adapt a Deep Agent response to the application's validation gate."""

    def __init__(self, agent: object) -> None:
        self.agent = agent

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision | object:
        invoke = getattr(self.agent, "invoke", None)
        if not callable(invoke):
            raise TypeError("Deep Maneuver Control agent must expose invoke")
        response = invoke(
            {
                "snapshot": snapshot.to_dict(),
                "fsm_status": status.to_dict(),
                "overlay": overlay.to_dict() if overlay is not None else None,
            }
        )
        if isinstance(response, ManeuverControlDecision):
            return response
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, ManeuverControlDecision):
            return structured
        if isinstance(structured, dict):
            return ManeuverControlDecision.from_dict(structured)
        if isinstance(response, dict):
            return ManeuverControlDecision.from_dict(response)
        return response


__all__ = [
    "create_maneuver_control_agent",
    "DeepAgentsDecisionProvider",
]
