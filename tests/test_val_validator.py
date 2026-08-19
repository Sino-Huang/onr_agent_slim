from __future__ import annotations

import sys
from pathlib import Path

from onr.adapters.val import VALPlanValidator
from onr.contracts.planning import PlannerExecutionEvidence


def _evidence(tmp_path: Path) -> PlannerExecutionEvidence:
    directory = tmp_path / "planner-run"
    directory.mkdir()
    paths = []
    for name, content in (
        ("domain.pddl", "(define (domain test))"),
        ("problem.pddl", "(define (problem test))"),
        ("sas_plan", "(survey)"),
    ):
        path = directory / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    stdout = directory / "solver.stdout"
    stderr = directory / "solver.stderr"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return PlannerExecutionEvidence(directory, tuple(paths), stdout, stderr)


def _validator_script(tmp_path: Path, *, valid: bool) -> Path:
    script = tmp_path / f"validator-{valid}.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "domain, problem, plan = map(Path, sys.argv[1:])\n"
        "assert domain.name == 'domain.pddl'\n"
        "assert problem.name == 'problem.pddl'\n"
        "assert plan.name == 'sas_plan'\n"
        f"print({'Plan valid' if valid else 'Plan failed to execute'!r})\n"
        f"raise SystemExit({0 if valid else 1})\n",
        encoding="utf-8",
    )
    return script


def test_val_validator_independently_accepts_only_valid_persisted_plan(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    accepted = VALPlanValidator(
        sys.executable,
        arguments=(str(_validator_script(tmp_path, valid=True)),),
    )
    rejected = VALPlanValidator(
        sys.executable,
        arguments=(str(_validator_script(tmp_path, valid=False)),),
    )

    assert accepted.validate(evidence)
    assert not rejected.validate(evidence)
    assert (evidence.artifact_directory / "validator.stdout").read_text(
        encoding="utf-8"
    ).strip() == "Plan failed to execute"
    assert (evidence.artifact_directory / "validator.stderr").is_file()
