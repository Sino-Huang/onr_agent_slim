from __future__ import annotations

import pytest

from onr.adapters.human_decisions import FileHumanDecisionStore
from onr.application.human_decisions import HumanDecisionCoordinator
from onr.application.hyper_agent import PlanningHeartbeatOutcome
from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionAction,
    HumanDecisionCategory,
    HumanDecisionDisposition,
    RunCheckpoint,
)
from onr.contracts.planner_translation import PlanningTranslationOutcome


@pytest.mark.parametrize(
    ("category", "expected_actions"),
    (
        (
            HumanDecisionCategory.TRANSLATION_REPAIR_EXHAUSTED,
            (
                HumanDecisionAction.RETRY_TRANSLATION,
                HumanDecisionAction.END_MISSION_RUN,
            ),
        ),
        (
            HumanDecisionCategory.UNSOLVABLE,
            (
                HumanDecisionAction.REVISE_MISSION_INTENT,
                HumanDecisionAction.END_MISSION_RUN,
            ),
        ),
        (
            HumanDecisionCategory.INSUFFICIENT_ENVIRONMENT_DATA,
            (
                HumanDecisionAction.WAIT_FOR_ENVIRONMENT_DATA,
                HumanDecisionAction.END_MISSION_RUN,
            ),
        ),
        (
            HumanDecisionCategory.TIMEOUT,
            (
                HumanDecisionAction.RETRY_PLANNER,
                HumanDecisionAction.END_MISSION_RUN,
            ),
        ),
    ),
)
def test_terminal_planning_categories_persist_correct_safe_operator_actions(
    tmp_path,
    category: HumanDecisionCategory,
    expected_actions: tuple[HumanDecisionAction, ...],
) -> None:
    store = FileHumanDecisionStore(tmp_path)
    coordinator = HumanDecisionCoordinator(store)
    checkpoint = RunCheckpoint(
        checkpoint_id=f"checkpoint-{category}",
        mission_id="mission-1",
        mission_run_id=f"run-{category}",
        continuation="planning",
    )

    request = coordinator.pause(
        category,
        checkpoint,
        correlation_id=f"planning-{category}",
        evidence_references=(f"artifacts/mission-1/{category}/planning-evidence.json",),
    )
    restarted = HumanDecisionCoordinator(FileHumanDecisionStore(tmp_path))
    assert restarted.request(request.request_id) == request
    assert request.permitted_actions == expected_actions
    assert request.correlation_id == f"planning-{category}"
    assert request.evidence_references == (
        f"artifacts/mission-1/{category}/planning-evidence.json",
    )


@pytest.mark.parametrize(
    ("outcome", "category"),
    (
        (
            PlanningTranslationOutcome.REPAIR_EXHAUSTED,
            HumanDecisionCategory.TRANSLATION_REPAIR_EXHAUSTED,
        ),
        (
            PlanningTranslationOutcome.UNSOLVABLE,
            HumanDecisionCategory.UNSOLVABLE,
        ),
        (
            PlanningTranslationOutcome.TIMEOUT,
            HumanDecisionCategory.TIMEOUT,
        ),
        (
            PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA,
            HumanDecisionCategory.INSUFFICIENT_ENVIRONMENT_DATA,
        ),
    ),
)
def test_terminal_planning_outcome_directly_creates_correlated_request(
    tmp_path,
    outcome: PlanningTranslationOutcome | PlanningHeartbeatOutcome,
    category: HumanDecisionCategory,
) -> None:
    coordinator = HumanDecisionCoordinator(FileHumanDecisionStore(tmp_path))
    checkpoint = RunCheckpoint(
        f"checkpoint-{category}",
        "mission-outcome",
        f"run-{category}",
        "planning",
    )

    request = coordinator.pause_for_outcome(
        outcome,
        checkpoint,
        correlation_id=f"planning-{category}",
        evidence_references=(f"artifacts/{category}.json",),
    )

    assert request.category is category


def test_recorded_human_decision_resumes_or_ends_deterministically(tmp_path) -> None:
    coordinator = HumanDecisionCoordinator(FileHumanDecisionStore(tmp_path))
    checkpoint = RunCheckpoint(
        "checkpoint-1",
        "mission-1",
        "run-1",
        "planning",
    )
    request = coordinator.pause(
        HumanDecisionCategory.TRANSLATION_REPAIR_EXHAUSTED,
        checkpoint,
        correlation_id="attempt-2",
        evidence_references=("artifacts/mission-1/attempt-2.json",),
    )
    retry = HumanDecision(
        "decision-retry",
        request.request_id,
        request.mission_id,
        request.mission_run_id,
        HumanDecisionAction.RETRY_TRANSLATION,
    )

    resumed = coordinator.record(retry)
    repeated = coordinator.record(retry)

    assert repeated == resumed
    assert resumed.disposition is HumanDecisionDisposition.RESUME
    assert resumed.checkpoint == checkpoint

    revise_checkpoint = RunCheckpoint(
        "checkpoint-revise",
        "mission-revise",
        "run-revise",
        "planning",
    )
    revise_request = coordinator.pause(
        HumanDecisionCategory.UNSOLVABLE,
        revise_checkpoint,
        correlation_id="unsolvable-plan",
        evidence_references=("artifacts/mission-revise/solver.stdout",),
    )
    revised = coordinator.record(
        HumanDecision(
            "decision-revise",
            revise_request.request_id,
            revise_request.mission_id,
            revise_request.mission_run_id,
            HumanDecisionAction.REVISE_MISSION_INTENT,
        )
    )

    assert revised.disposition is HumanDecisionDisposition.RESUME
    assert revised.checkpoint == revise_checkpoint

    end_checkpoint = RunCheckpoint(
        "checkpoint-2",
        "mission-2",
        "run-2",
        "planning",
    )
    end_request = coordinator.pause(
        HumanDecisionCategory.TIMEOUT,
        end_checkpoint,
        correlation_id="solver-timeout",
        evidence_references=("artifacts/mission-2/solver.stderr",),
    )
    ended = coordinator.record(
        HumanDecision(
            "decision-end",
            end_request.request_id,
            end_request.mission_id,
            end_request.mission_run_id,
            HumanDecisionAction.END_MISSION_RUN,
        )
    )

    assert ended.disposition is HumanDecisionDisposition.END
    assert ended.checkpoint is None
