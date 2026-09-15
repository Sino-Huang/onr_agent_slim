#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from onr.application.live_demo_audit import audit_live_demo


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a terminal model-backed live demo")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--mission-mode", choices=("mission2", "mission3", "mission4"), required=True
    )
    args = parser.parse_args()
    audit = audit_live_demo(args.run_root, args.mission_mode)
    print(json.dumps(audit, sort_keys=True), flush=True)
    return 0 if audit["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
