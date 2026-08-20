"""Immutable contracts for Missions, Mission Snapshots, and Transport Events."""

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import (
    FSMEvent,
    FSMExecutionRecord,
    FSMStatus,
    ManeuverDecision,
    ManeuverFeedback,
    Statechart,
    StatechartTransition,
    TransitionCandidate,
)
from onr.contracts.hyper_agent import (
    HumanQuestion,
    MissionInput,
    ReplanRequest,
)
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.role_context import (
    HYPER_AGENT_ROLE,
    MANEUVER_CONTROL_ROLE,
    RoleSkill,
)

__all__ = [
    "MissionSnapshot",
    "PlanningIntent",
    "PlannerChoiceRecord",
    "PlannerGenerationAttempt",
    "TranslationAttemptOutcome",
    "FSMExecutionRecord",
    "FSMEvent",
    "FSMStatus",
    "ManeuverDecision",
    "ManeuverFeedback",
    "Statechart",
    "StatechartTransition",
    "TransitionCandidate",
    "PhysicalAction",
    "NonPhysicalChoice",
    "ManeuverControlDecision",
    "ManeuverCommand",
    "InvocationOverlay",
    "MissionInput",
    "ReplanRequest",
    "HumanQuestion",
    "HYPER_AGENT_ROLE",
    "MANEUVER_CONTROL_ROLE",
    "RoleSkill",
]
