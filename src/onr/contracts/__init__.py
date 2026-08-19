"""Immutable contracts for Missions, Mission Snapshots, and Transport Events."""

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
from onr.contracts.fsm import (
    FSMExecutionRecord,
    FSMEvent,
    FSMStatus,
    ManeuverDecision,
    ManeuverFeedback,
    Statechart,
    StatechartTransition,
    TransitionCandidate,
)
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.hyper_agent import (
    FrozenMissionSpec,
    HumanQuestion,
    MissionAuthorityRecord,
    MissionInput,
    ReplanRequest,
)
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
    "FrozenMissionSpec",
    "MissionAuthorityRecord",
    "ReplanRequest",
    "HumanQuestion",
    "HYPER_AGENT_ROLE",
    "MANEUVER_CONTROL_ROLE",
    "RoleSkill",
]
