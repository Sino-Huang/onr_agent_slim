from __future__ import annotations

import sys
from pathlib import Path

import pytest

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.contracts.planning import PlanningOutcome


def _driver(tmp_path, body: str):
    driver = tmp_path / "driver.py"
    driver.write_text(body, encoding="utf-8")
    return driver


def test_fast_downward_executor_parses_ordered_plan_and_cost(tmp_path) -> None:
    driver = _driver(
        tmp_path,
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "plan_index = sys.argv.index('--plan-file')\n"
        "plan = Path(sys.argv[plan_index + 1])\n"
        "domain = Path(next(arg for arg in sys.argv if arg.endswith('domain.pddl')))\n"
        "problem = Path(next(arg for arg in sys.argv if arg.endswith('problem.pddl')))\n"
        "assert plan_index < sys.argv.index(str(domain)) < sys.argv.index(str(problem))\n"
        "assert Path(os.getcwd()) == plan.parent == domain.parent == problem.parent\n"
        "plan.write_text('(survey)\\n(survey)\\n(return-to-base)\\n; cost = 7 (general cost)\\n')\n",
    )
    result = FastDownwardExecutor(
        executable=sys.executable,
        artifact_root=tmp_path / "artifacts",
        arguments=(str(driver),),
    ).execute(
        {
            "domain.pddl": b"domain",
            "problem.pddl": b"problem",
        }
    )

    assert result.outcome is PlanningOutcome.SOLVED
    assert tuple(call.action for call in result.action_calls) == (
        "survey",
        "survey",
        "return-to-base",
    )
    assert result.total_plan_cost == 7
    assert result.evidence is not None
    assert result.evidence.artifact_directory.parent == (tmp_path / "artifacts").resolve()
    assert {path.name for path in result.evidence.artifact_paths} == {
        "domain.pddl",
        "problem.pddl",
        "sas_plan",
    }
    assert result.evidence.stdout_path.read_text(encoding="utf-8") == ""
    assert result.evidence.stderr_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    (
        (11, PlanningOutcome.UNSOLVABLE),
        (12, PlanningOutcome.INCOMPLETE),
        (21, PlanningOutcome.TIMEOUT),
        (23, PlanningOutcome.TIMEOUT),
        (1, PlanningOutcome.ERROR),
    ),
)
def test_fast_downward_executor_maps_documented_nonzero_exits(
    tmp_path, exit_code: int, expected: PlanningOutcome
) -> None:
    driver = _driver(tmp_path, f"raise SystemExit({exit_code})\n")

    result = FastDownwardExecutor(
        executable=sys.executable,
        artifact_root=tmp_path / "artifacts",
        arguments=(str(driver),),
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is expected
    assert result.action_calls == ()
    assert result.evidence is not None
    assert result.evidence.stdout_path.exists()
    assert result.evidence.stderr_path.exists()


def test_fast_downward_executor_maps_subprocess_timeout(tmp_path) -> None:
    driver = _driver(tmp_path, "import time\ntime.sleep(1)\n")

    result = FastDownwardExecutor(
        executable=sys.executable,
        artifact_root=tmp_path / "artifacts",
        arguments=(str(driver),),
        timeout_seconds=0.01,
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is PlanningOutcome.TIMEOUT
    assert result.evidence is not None
    assert "timed out" in result.evidence.stderr_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "plan",
    (
        "(survey)\n",
        "(survey)\n; cost = seven (general cost)\n",
        "survey\n; cost = 1 (unit cost)\n",
        "(survey (nested))\n; cost = 1 (unit cost)\n",
        "(survey)\n; cost = 1 (unit cost) trailing\n",
    ),
)
def test_fast_downward_executor_rejects_missing_or_malformed_plan_content(
    tmp_path, plan: str
) -> None:
    driver = _driver(
        tmp_path,
        "from pathlib import Path\n"
        "import sys\n"
        "plan = Path(sys.argv[sys.argv.index('--plan-file') + 1])\n"
        f"plan.write_text({plan!r})\n",
    )

    result = FastDownwardExecutor(
        executable=sys.executable,
        artifact_root=tmp_path / "artifacts",
        arguments=(str(driver),),
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is PlanningOutcome.ERROR



def test_fast_downward_static_check_uses_real_pddl_translator(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    fast_downward = repository_root / "modules" / "downward" / "fast-downward.py"
    benchmark = (
        repository_root
        / "modules"
        / "downward"
        / "misc"
        / "tests"
        / "benchmarks"
        / "gripper"
    )
    executor = FastDownwardExecutor(
        executable=fast_downward,
        artifact_root=tmp_path / "artifacts",
        timeout_seconds=10,
    )

    accepted = executor.check(
        {
            "domain.pddl": (benchmark / "domain.pddl").read_bytes(),
            "problem.pddl": (benchmark / "prob01.pddl").read_bytes(),
        }
    )
    rejected = executor.check(
        {
            "domain.pddl": b"this is not PDDL",
            "problem.pddl": b"(define (problem invalid))",
        }
    )
    assert accepted.accepted is True
    assert accepted.return_code == 0
    assert rejected.accepted is False
    assert rejected.return_code != 0
    assert rejected.error_message
    assert rejected.error_message in (rejected.stderr.strip(), rejected.stdout.strip())
