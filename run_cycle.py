#!/usr/bin/env python3
"""
Run one full pipeline step: extract → validate → update DB (+ CVL views).

Format stage is chosen automatically by extract_cycle.py (CVL cascade order).
update_db_cycle.py refreshes apply_validation_views.py automatically.

If validation stops mid-batch (Apify quota), re-run run_cycle_resume.bat.
Each run_cycle validates the current extract once (resumes if partial), updates DB,
then the next run_cycle extracts a new batch.

Usage:
  python run_cycle.py
  python run_cycle.py --limit 500
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def run_script(name: str, extra: list[str] | None = None) -> int:
    cmd = [sys.executable, str(SCRIPT_DIR / name)] + (extra or [])
    print(f"\n>>> {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument(
        "--steps",
        default="extract,validate,db",
        help="Comma-separated: extract, validate, db (views run inside update_db_cycle)",
    )
    p.add_argument("--resume-validation", action="store_true")
    p.add_argument("--extraction-cycle", type=int, default=None)
    p.add_argument("--validation-cycle", type=int, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    code = 0

    if "extract" in steps:
        code = run_script("extract_cycle.py", ["--limit", str(args.limit)])
        if code == 2:
            print("\nPending validation — running validate step, then re-run extract if needed.")
        elif code != 0:
            return code

    if "validate" in steps:
        extra = []
        if args.extraction_cycle:
            extra += ["--extraction-cycle", str(args.extraction_cycle)]
        if args.validation_cycle:
            extra += ["--validation-cycle", str(args.validation_cycle)]
        if args.resume_validation:
            extra.append("--resume")
        code = run_script("validate_cycle.py", extra)
        if code not in (0, 3):
            return code
        if code == 3:
            print(
                "\nValidation still partial (quota or unfinished emails) — "
                "will still try DB update if this batch is complete enough."
            )

    if "db" in steps:
        extra = []
        if args.validation_cycle:
            extra += ["--validation-cycle", str(args.validation_cycle)]
        code = run_script("update_db_cycle.py", extra)
        if code != 0:
            if code == 2:
                print(
                    "\nDB update skipped — finish validation with run_cycle_resume.bat, "
                    "then update_db_cycle.py"
                )
            return code

    if "views" in steps:
        # Legacy alias: views refresh is part of update_db_cycle since 2026-06.
        print("\n(note: 'views' step is included in update_db_cycle — skipping duplicate run)\n")

    print("\nCycle step(s) finished.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
