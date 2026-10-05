"""Code-owned, deterministic Importance Levels for operator evidence (D5).

The mapping is versioned like Run Activity ``mapping_version``: any change to
which records land in which level bumps ``IMPORTANCE_MAPPING_VERSION``.
LLM output never decides importance; a Mission Log Summary takes the maximum
importance of the operational-log records it covers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

IMPORTANCE_MAPPING_VERSION = 1

CRITICAL = "critical"
WARNING = "warning"
NOTABLE = "notable"
ROUTINE = "routine"
DEBUG = "debug"

# Most severe first.
IMPORTANCE_LEVELS: tuple[str, ...] = (CRITICAL, WARNING, NOTABLE, ROUTINE, DEBUG)
_RANK = {level: rank for rank, level in enumerate(reversed(IMPORTANCE_LEVELS))}

_COMPONENT_BADGES = {
    "hyper-agent": "HYP",
    "planning-command-handler": "HYP",
    "maneuver-control": "MAN",
    "context-coordination": "CC",
    "fsm-runner": "FSM",
    "bayesian-belief": "BEL",
    "reporting-reliability": "BEL",
    "object-search-belief": "BEL",
    "environment": "ENV",
    "physical-runtime": "ENV",
    "perception": "PER",
    "runtime-host": "STK",
    "stack": "STK",
}

# Outcomes that always mean something went wrong for the emitting component.
_FAILURE_OUTCOMES = frozenset({"failed", "crashed", "dead_letter", "dead-letter"})
# Outcomes that mean an attempt was refused or degraded but the run continues.
_DEGRADED_OUTCOMES = frozenset(
    {
        "rejected",
        "cancelled",
        "timeout",
        "error",
        "unsolvable",
        "incomplete",
        "unsupported",
        "missing",
        "unavailable",
        "insufficient_environment_data",
        "gap",
        "conflict",
        "malformed",
        "stale",
    }
)
_CRITICAL_ERROR_TYPES = frozenset({"StructuredOutputRetriesExhausted"})
_WARNING_REPLAY_DISPOSITIONS = frozenset({"gap", "conflict", "malformed", "stale"})
_CRITICAL_REPLAY_DISPOSITIONS = frozenset({"dead_letter", "dead-letter"})
BELIEF_CHANGE_NOTABLE_THRESHOLD = 0.2

# (source, event_kind, outcome) -> level; `None` matches any value.
_RULES: tuple[tuple[str | None, str | None, str | None, str], ...] = (
    # Critical: the mission cannot proceed as asked.
    ("hyper-agent", "planning-intent", "rejected", CRITICAL),
    ("hyper-agent", "workflow", "failed", CRITICAL),
    (None, "error", None, CRITICAL),
    ("perception", None, "failed", CRITICAL),
    # Warning: refused attempts and degraded evidence; the run continues.
    ("runtime", "summary-unavailable", None, WARNING),
    ("runtime", "summary-missing", None, WARNING),
    ("maneuver-control", "heartbeat", "failed", WARNING),
    ("planning-command-handler", "planning", "failed", WARNING),
    ("runtime", "planning-environment-data", "insufficient_environment_data", WARNING),
    # Notable: workflow progress and decisions an operator follows.
    ("hyper-agent", "workflow", "started", NOTABLE),
    ("hyper-agent", "workflow", "completed", NOTABLE),
    ("hyper-agent", "planning-intent", "completed", NOTABLE),
    ("hyper-agent", "planner-choice", None, NOTABLE),
    ("hyper-agent", "planner-assets", "accepted", NOTABLE),
    ("hyper-agent", "planner-execution", "verified", NOTABLE),
    ("hyper-agent", "planner-generation-attempt", "accepted", NOTABLE),
    ("hyper-agent", "statechart-generation", "verified", NOTABLE),
    ("hyper-agent", "heartbeat", "replan", NOTABLE),
    ("hyper-agent", "prior-knowledge", "applied", NOTABLE),
    ("fsm-runner", "fsm", "initialized", NOTABLE),
    ("fsm-runner", "fsm", "transitioned", NOTABLE),
    ("fsm-runner", "fsm", "superseded", NOTABLE),
    ("maneuver-control", "control", "queued", NOTABLE),
    ("maneuver-control", "control", "completed", NOTABLE),
    ("runtime", "agent", "started", NOTABLE),
    ("planning-command-handler", "planning", "completed", NOTABLE),
    # Routine: steady-state cadence with no change.
    ("fsm-runner", "fsm", "updated", ROUTINE),
    ("maneuver-control", "heartbeat", "completed", ROUTINE),
    ("hyper-agent", "heartbeat", None, ROUTINE),
    ("runtime", "heartbeat", None, ROUTINE),
    ("runtime", "planning-environment-data", None, ROUTINE),
    ("planning-command-handler", "solver", None, ROUTINE),
    # Debug: high-volume bookkeeping hidden by default.
    ("context-coordination", "heartbeat", "completed", DEBUG),
    ("hyper-agent", "prior-knowledge", None, DEBUG),
)


def importance_rank(level: str) -> int:
    """Return a sortable rank; higher is more severe."""

    try:
        return _RANK[level]
    except KeyError:
        raise ValueError(f"unknown importance level: {level!r}") from None


def max_importance(levels: Iterable[str], *, default: str = DEBUG) -> str:
    """Return the most severe level, or ``default`` for an empty input."""

    best = default
    for level in levels:
        if importance_rank(level) > importance_rank(best):
            best = level
    return best


def empty_importance_counts() -> dict[str, int]:
    """Return a zeroed ``counts_by_importance`` object in contract key order."""

    return {level: 0 for level in IMPORTANCE_LEVELS}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _details(record: Mapping[str, object]) -> Mapping[str, object]:
    details = record.get("details")
    return details if isinstance(details, Mapping) else {}


def record_importance(record: Mapping[str, object]) -> str:
    """Classify one operational-log record (``OperationalLogRecord.to_dict()``)."""

    source = _text(record.get("source"))
    event_kind = _text(record.get("event_kind"))
    outcome = _text(record.get("outcome"))
    details = _details(record)
    if _text(details.get("error_type")) in _CRITICAL_ERROR_TYPES:
        return CRITICAL
    if "dead-letter" in event_kind or "dead_letter" in event_kind:
        return CRITICAL
    if source in {"runtime-host", "stack"} and outcome in _FAILURE_OUTCOMES:
        return CRITICAL
    if source == "maneuver-control" and event_kind == "heartbeat":
        transitions = details.get("successful_transition_count")
        if outcome == "completed" and type(transitions) is int and transitions > 0:
            return NOTABLE
    for rule_source, rule_kind, rule_outcome, level in _RULES:
        if (
            (rule_source is None or rule_source == source)
            and (rule_kind is None or rule_kind == event_kind)
            and (rule_outcome is None or rule_outcome == outcome)
        ):
            return level
    if outcome in _FAILURE_OUTCOMES or outcome in _DEGRADED_OUTCOMES:
        return WARNING
    if source == "hyper-agent" and event_kind in {
        "planner-assets",
        "planner-execution",
        "planner-generation-attempt",
        "statechart-generation",
    }:
        # Any non-verified planner outcome (for example ``unsolvable``) is a
        # correction-loop attempt that did not succeed.
        return WARNING
    return ROUTINE


def replay_disposition_importance(disposition: object) -> str | None:
    """Importance implied by an observation ``replay_disposition``, if any."""

    if disposition in _CRITICAL_REPLAY_DISPOSITIONS:
        return CRITICAL
    if disposition in _WARNING_REPLAY_DISPOSITIONS:
        return WARNING
    return None


def service_importance(
    state: str,
    *,
    required: bool = True,
    exit_code: int | None = None,
    completes: bool = False,
) -> str:
    """Importance of one Environment Stack service state.

    A service that ``completes`` and exited 0 finished its job: routine.
    """

    if state == "failed" or (state == "exited" and exit_code not in (None, 0)):
        return CRITICAL if required else WARNING
    if state == "exited":
        return WARNING if required and not completes else ROUTINE
    return ROUTINE


def belief_change_importance(delta: float) -> str:
    """A belief move of at least 0.2 is notable; smaller moves are routine."""

    return NOTABLE if abs(delta) >= BELIEF_CHANGE_NOTABLE_THRESHOLD else ROUTINE


def component_badge(source: str, event_kind: str | None = None) -> str:
    """Three-letter (or shorter) component badge for a record ``source``."""

    if source == "runtime":
        return "ENV" if event_kind == "planning-environment-data" else "STK"
    if source in _COMPONENT_BADGES:
        return _COMPONENT_BADGES[source]
    if "belief" in source:
        return "BEL"
    if "perception" in source:
        return "PER"
    return "STK"


def record_title(
    record: Mapping[str, object], *, previous_fsm_state: str | None = None
) -> str:
    """Short human title for one operational-log record.

    ``previous_fsm_state`` is the active state of the preceding ``fsm`` record,
    which lets transitions read ``FSM a → b``.
    """

    source = _text(record.get("source"))
    event_kind = _text(record.get("event_kind"))
    outcome = _text(record.get("outcome"))
    details = _details(record)
    state = _text(details.get("state"))
    revision = details.get("plan_revision")
    rev = f" (rev {revision})" if type(revision) is int else ""
    error_type = _text(details.get("error_type"))
    planner = _text(details.get("planner_id"))
    profile = _text(details.get("planning_profile"))

    if source == "fsm-runner" and event_kind == "fsm":
        if outcome == "transitioned":
            origin = previous_fsm_state or "?"
            return f"FSM {origin} → {state or '?'}"
        if outcome == "initialized":
            return f"FSM initialized at {state or '?'}{rev}"
        if outcome == "superseded":
            return f"FSM superseded by plan{rev}"
        if outcome == "updated":
            return f"FSM {state or '?'} updated"
        return f"FSM {outcome}"
    if source == "context-coordination" and event_kind == "heartbeat":
        return "Context Coordination heartbeat"
    if source == "hyper-agent":
        if event_kind == "workflow":
            status = _text(details.get("status"))
            if outcome == "failed" and error_type:
                return f"Hyper workflow failed: {error_type}"
            return f"Hyper workflow {outcome}" + (f" ({status})" if status else "")
        if event_kind == "planning-intent":
            if outcome == "rejected":
                reason = _text(details.get("reason"))
                return "Mission intent rejected" + (f": {reason}" if reason else "")
            if outcome == "completed":
                choice = ", ".join(item for item in (planner, profile) if item)
                return "Planning intent accepted" + (f" ({choice})" if choice else "")
        if event_kind == "planner-choice":
            suffix = f" ({profile})" if profile else ""
            return f"Planner chosen: {planner or '?'}{suffix}"
        if event_kind == "planner-assets":
            assets = _text(details.get("generated_assets"))
            return f"Planner assets {outcome}" + (f": {assets}" if assets else "")
        if event_kind == "planner-execution":
            name = planner or "planner"
            if outcome == "verified":
                return f"Planner {name} verified plan{rev}"
            return f"Planner {name} execution {outcome}{rev}"
        if event_kind == "statechart-generation":
            attempt = details.get("attempt_number")
            label = f" attempt {attempt}" if type(attempt) is int else ""
            stage = _text(details.get("stage"))
            at_stage = f" at {stage}" if stage else ""
            return f"Statechart{label} {outcome}{at_stage}"
        if event_kind == "heartbeat":
            return f"Hyper heartbeat → {outcome.replace('_', ' ')}{rev}"
        if event_kind == "prior-knowledge":
            return f"Prior knowledge {outcome.replace('_', ' ')}"
    if source == "maneuver-control":
        if event_kind == "control" and outcome == "queued":
            return "Maneuver command queued"
        if event_kind == "control" and outcome == "completed":
            return "Maneuver decision completed"
        if event_kind == "heartbeat":
            if outcome == "failed":
                return "Maneuver heartbeat failed" + (
                    f": {error_type}" if error_type else ""
                )
            transitions = details.get("successful_transition_count")
            if type(transitions) is int and transitions > 0:
                return f"Maneuver heartbeat · {transitions} transition(s)"
            return "Maneuver heartbeat"
    if source == "runtime" and event_kind in {"summary-unavailable", "summary-missing"}:
        return "Mission Log Summary unavailable"
    if event_kind == "error":
        operation = _text(details.get("operation"))
        label = f" in {operation}" if operation else ""
        kind = f": {error_type}" if error_type else ""
        return f"{source or 'component'} error{label}{kind}"
    return " ".join(item for item in (source, event_kind, outcome) if item)


__all__ = [
    "BELIEF_CHANGE_NOTABLE_THRESHOLD",
    "CRITICAL",
    "DEBUG",
    "IMPORTANCE_LEVELS",
    "IMPORTANCE_MAPPING_VERSION",
    "NOTABLE",
    "ROUTINE",
    "WARNING",
    "belief_change_importance",
    "component_badge",
    "empty_importance_counts",
    "importance_rank",
    "max_importance",
    "record_importance",
    "record_title",
    "replay_disposition_importance",
    "service_importance",
]
