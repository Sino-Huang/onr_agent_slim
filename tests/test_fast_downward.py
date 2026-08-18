from __future__ import annotations

import sys

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
        arguments=(str(driver),),
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is expected
    assert result.action_calls == ()


def test_fast_downward_executor_maps_subprocess_timeout(tmp_path) -> None:
    driver = _driver(tmp_path, "import time\ntime.sleep(1)\n")

    result = FastDownwardExecutor(
        executable=sys.executable,
        arguments=(str(driver),),
        timeout_seconds=0.01,
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is PlanningOutcome.TIMEOUT


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
        arguments=(str(driver),),
    ).execute({"domain.pddl": b"domain", "problem.pddl": b"problem"})

    assert result.outcome is PlanningOutcome.ERROR
