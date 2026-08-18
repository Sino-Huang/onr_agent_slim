"""Pure translation of symbolic Mission Specifications to PDDL assets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from onr.contracts.planning import SymbolicMissionSpec


_INVALID_PDDL_NAME = re.compile(r"[^a-z0-9_-]+")


def translate_pddl(mission_spec: SymbolicMissionSpec) -> Mapping[str, bytes]:
    """Render deterministic action-cost PDDL domain and problem assets."""

    domain_name = f"onr-symbolic-r{mission_spec.domain_revision}"
    predicates = " ".join(
        f"({_completion_predicate(item.maneuver_id)})"
        for item in mission_spec.maneuvers
    )
    actions: list[str] = []
    for maneuver in mission_spec.maneuvers:
        preconditions = " ".join(
            f"({_completion_predicate(dependency)})"
            for dependency in maneuver.dependencies
        )
        actions.append(
            f"  (:action {_pddl_name(maneuver.maneuver_id)}\n"
            "    :parameters ()\n"
            f"    :precondition (and{_prefixed(preconditions)})\n"
            "    :effect (and "
            f"({_completion_predicate(maneuver.maneuver_id)}) "
            f"(increase (total-cost) {maneuver.cost}))\n"
            "  )"
        )

    rendered_actions = "\n".join(actions)
    domain = (
        f"; ONR symbolic domain revision {mission_spec.domain_revision}\n"
        f"(define (domain {domain_name})\n"
        "  (:requirements :strips :action-costs)\n"
        f"  (:predicates {predicates})\n"
        "  (:functions (total-cost))\n"
        f"{rendered_actions}\n"
        ")\n"
    ).encode("ascii")

    goals = " ".join(
        f"({_completion_predicate(item.maneuver_id)})"
        for item in mission_spec.maneuvers
    )
    problem = (
        f"(define (problem {_pddl_name(mission_spec.mission_id)})\n"
        f"  (:domain {domain_name})\n"
        "  (:init (= (total-cost) 0))\n"
        f"  (:goal (and{_prefixed(goals)}))\n"
        "  (:metric minimize (total-cost))\n"
        ")\n"
    ).encode("ascii")
    return MappingProxyType({"domain.pddl": domain, "problem.pddl": problem})


def _pddl_name(value: str) -> str:
    normalized = _INVALID_PDDL_NAME.sub("-", value.strip().lower()).strip("-")
    if not normalized:
        normalized = "mission"
    if not normalized[0].isalnum():
        normalized = f"onr-{normalized}"
    return normalized


def _completion_predicate(maneuver_id: str) -> str:
    return f"completed-{_pddl_name(maneuver_id)}"


def _prefixed(value: str) -> str:
    return f" {value}" if value else ""
