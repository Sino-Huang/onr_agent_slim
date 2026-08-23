"""Immutable contracts for Missions, Mission Snapshots, and Transport Events."""

from onr.contracts.communication import AgentMessage, AgentMessageKind
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
from onr.contracts.environment import (
    EntityObservation,
    EnvironmentTickResult,
    EventObservation,
)
from onr.contracts.hyper_agent import (
    HyperHeartbeatDecision,
    HyperHeartbeatDisposition,
    HyperHeartbeatInvocation,
    HumanQuestion,
    MissionInput,
    ReplanRequest,
)
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
    ManeuverHeartbeatOutcome,
    ManeuverInvocation,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.planning import PlannerPlan
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    PlannerRevisionEvidence,
    TranslationAttemptOutcome,
)
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.role_context import (
    HYPER_AGENT_ROLE,
    MANEUVER_CONTROL_ROLE,
    RoleSkill,
)

__all__ = [
    "HYPER_AGENT_ROLE",
    "MANEUVER_CONTROL_ROLE",
    "AgentMessage",
    "AgentMessageKind",
    "EntityObservation",
    "EnvironmentTickResult",
    "EventObservation",
    "FSMEvent",
    "FSMExecutionRecord",
    "FSMStatus",
    "HumanQuestion",
    "HyperHeartbeatDecision",
    "HyperHeartbeatDisposition",
    "HyperHeartbeatInvocation",
    "HyperWorkflowOutcome",
    "InvocationOverlay",
    "ManeuverCommand",
    "ManeuverControlDecision",
    "ManeuverDecision",
    "ManeuverFeedback",
    "ManeuverHeartbeatCompletion",
    "ManeuverHeartbeatOutcome",
    "ManeuverInvocation",
    "MissionInput",
    "MissionSnapshot",
    "NonPhysicalChoice",
    "PhysicalAction",
    "PlannerChoiceRecord",
    "PlannerGenerationAttempt",
    "PlannerRevisionEvidence",
    "PlannerPlan",
    "PlanningIntent",
    "ReplanRequest",
    "RoleSkill",
    "Statechart",
    "StatechartTransition",
    "TransitionCandidate",
    "TranslationAttemptOutcome",
]
