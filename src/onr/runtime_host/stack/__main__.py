"""``python -m onr.runtime_host.stack`` — plan, preflight and list Environment Stacks.

``plan`` resolves a preset into a run root and prints ``stack.json``;
``plan --demo-env`` reads the herdr launcher's ``ONR_DEMO_*`` variables and
prints ``name=value`` assignments for the launcher's ``eval``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from onr.runtime_host.stack import herdr
from onr.runtime_host.stack.builder import (
    StackPlanError,
    build_stack_plan,
    materialize_stack_plan,
    stack_request,
    validate_stack_request,
)
from onr.runtime_host.stack.preflight import run_preflight
from onr.runtime_host.stack.presets import (
    PERCEPTION_MODES,
    UPDATE_OWNERSHIPS,
    StackCatalog,
    StackRequestError,
    load_stack_catalog,
)

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m onr.runtime_host.stack", description=__doc__
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo-root", type=Path, default=_DEFAULT_REPO_ROOT)
        command.add_argument("--presets", type=Path, help="stack presets YAML")

    def toggles(command: argparse.ArgumentParser) -> None:
        command.add_argument("--preset", dest="preset_id")
        command.add_argument(
            "--airsim", action=argparse.BooleanOptionalAction, default=None
        )
        command.add_argument("--perception", choices=PERCEPTION_MODES)
        command.add_argument("--update-ownership", choices=UPDATE_OWNERSHIPS)
        command.add_argument("--simulation-limit-seconds", type=float)
        command.add_argument("--viewer-port", type=int)

    presets = commands.add_parser("presets", help="print GET /api/v1/stack/presets")
    common(presets)

    plan = commands.add_parser("plan", help="resolve and materialize one stack plan")
    common(plan)
    toggles(plan)
    plan.add_argument("--mission-id")
    plan.add_argument("--run-root", type=Path)
    plan.add_argument("--no-write", action="store_true", help="print without writing")
    plan.add_argument(
        "--demo-env", action="store_true", help="read ONR_DEMO_* (herdr launcher)"
    )
    plan.add_argument(
        "--physical-root", type=Path, help="--demo-env physical runtime checkout"
    )
    plan.add_argument(
        "--check",
        action="store_true",
        help="--demo-env: validate only, no side effects",
    )

    preflight = commands.add_parser(
        "preflight", help="print GET /api/v1/stack/preflight"
    )
    common(preflight)
    toggles(preflight)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog = load_stack_catalog(args.presets)
    repo_root = args.repo_root
    try:
        if args.command == "presets":
            _print_json(catalog.payload(repo_root))
            return 0
        if args.command == "preflight":
            preset = catalog.preset(args.preset_id)
            report = run_preflight(
                catalog,
                preset.preset_id,
                _stack_fields(args),
                repo_root=repo_root,
                viewer_port=args.viewer_port,
            )
            _print_json(report)
            return 0
        if args.demo_env:
            return _demo_env_plan(args, catalog)
        if args.mission_id is None or args.run_root is None:
            raise StackPlanError("plan needs --mission-id and --run-root", exit_code=2)
        preset = catalog.preset(args.preset_id)
        request = stack_request(
            catalog,
            preset.preset_id,
            catalog.toggles(preset, _stack_fields(args)),
            mission_id=args.mission_id,
            repo_root=repo_root,
            viewer_port=args.viewer_port,
            python=sys.executable,
        )
        plan = build_stack_plan(request, args.run_root.resolve())
        if not args.no_write:
            materialize_stack_plan(plan)
        _print_json(plan.payload())
        return 0
    except StackPlanError as error:
        print(error, file=sys.stderr)
        return error.exit_code
    except StackRequestError as error:
        print(error, file=sys.stderr)
        return 2


def _demo_env_plan(args: argparse.Namespace, catalog: StackCatalog) -> int:
    request = herdr.demo_env_request(
        os.environ,
        catalog=catalog,
        repo_root=args.repo_root,
        physical_root=args.physical_root,
    )
    mode = request.mission_mode
    if args.check:
        validate_stack_request(request)
        sys.stdout.write(
            herdr.shell_assignments(
                {
                    "mission_mode": mode,
                    "workspace_label": herdr.workspace_label(mode),
                    "probe_ports": " ".join(
                        str(port) for port in herdr.probe_ports(request)
                    ),
                }
            )
        )
        return 0
    validate_stack_request(request)
    parent = herdr.run_parent(args.repo_root, mode)
    parent.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run.", dir=parent))
    try:
        plan = materialize_stack_plan(build_stack_plan(request, run_root))
    except BaseException:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    panes = herdr.pane_commands(plan)
    sys.stdout.write(
        herdr.shell_assignments(
            {
                "run_root": run_root,
                "physical_command": panes["physical-runtime"],
                "agent_command": panes["agent"],
                "worker_command": panes.get("mission4-worker", ""),
                "engine_command": panes.get("airsim-engine", ""),
                "perception_command": panes.get("perception", ""),
                "audit_command": shlex.join(plan.audit_argv),
                "prepare_command": herdr.prepare_command(plan),
                "summary": "\n".join(herdr.summary_lines(plan)),
            }
        )
    )
    return 0


def _stack_fields(args: argparse.Namespace) -> dict[str, object]:
    values = {
        "airsim": args.airsim,
        "perception": args.perception,
        "update_ownership": args.update_ownership,
        "simulation_limit_seconds": args.simulation_limit_seconds,
    }
    return {name: value for name, value in values.items() if value is not None}


def _print_json(document: object) -> None:
    sys.stdout.write(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
