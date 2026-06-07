#!/usr/bin/env python3
"""
Apply completed validation cycles to linkedin_data.db and employee_email_state.csv.

- Reads validation_manifest.csv for status=completed (or --validation-cycle N).
- Upserts Million Verifier fields into zerobounce_validation.
- Updates per-employee format status for the next extraction stage.
- Refreshes CVL SQLite views (apply_validation_views.py) unless --skip-views.

Usage:
  python update_db_cycle.py
  python update_db_cycle.py --validation-cycle 2
  python update_db_cycle.py --list-ready
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cycle_registry import (
    EMPLOYEE_STATE_CSV,
    EMPLOYEE_STATE_FIELDS,
    EXTRACTION_FIELDS,
    EXTRACTION_MANIFEST,
    VALIDATION_FIELDS,
    VALIDATION_MANIFEST,
    apply_validation_views_script,
    linkedin_db_path,
    read_manifest,
    resolve_data_path,
    update_manifest_row,
    write_manifest,
)
from email_formats import (
    FORMAT_ORDER,
    format_email_column,
    format_status_column,
    is_mv_valid,
)
from file_registry import (
    count_open_unprocessed,
    ensure_registry_db,
    log_unprocessed_summary,
    mark_db_updated,
)
from company_format_state import (
    apply_probe_validation_results,
    load_company_format_state,
    save_company_format_state,
)
from pipeline_logging import get_logger, setup_pipeline_logging

# Reuse DB upsert logic
from update_db_with_validation import (
    ensure_columns,
    parse_row_to_record,
    upsert_mv_results,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = linkedin_db_path()

logger = get_logger("update_db_cycle")


def load_employee_state() -> dict[str, dict]:
    if not EMPLOYEE_STATE_CSV.exists():
        return {}
    out: dict[str, dict] = {}
    with EMPLOYEE_STATE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = row.get("employee_key", "")
            if key:
                out[key] = row
    return out


def save_employee_state(state: dict[str, dict]) -> None:
    EMPLOYEE_STATE_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Ensure all dynamic format columns exist in output
    fieldnames = list(EMPLOYEE_STATE_FIELDS)
    for fmt in FORMAT_ORDER:
        for col in (format_status_column(fmt), format_email_column(fmt)):
            if col not in fieldnames:
                fieldnames.append(col)

    with EMPLOYEE_STATE_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(state.values(), key=lambda r: r.get("employee_key", "")):
            writer.writerow(row)


def read_validation_csv(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def apply_row_to_employee_state(state: dict[str, dict], row: dict) -> None:
    ekey = (row.get("employee_key") or "").strip()
    if not ekey:
        return
    fmt = (row.get("email_format") or "").strip()
    email = (row.get("email") or "").strip()
    status = (row.get("validation_status") or "").strip()
    reason = (row.get("validation_reason") or "").strip()
    now = datetime.now(timezone.utc).isoformat()

    rec = state.get(ekey, {})
    rec.setdefault("employee_key", ekey)
    for field in (
        "employee_id",
        "company_name",
        "full_name",
        "first_name",
        "last_name",
        "company_domain",
    ):
        if row.get(field):
            rec[field] = row[field]
    rec["last_updated"] = now

    if fmt:
        rec[format_email_column(fmt)] = email
        rec[format_status_column(fmt)] = status
        rec["email_format"] = fmt
        rec["email"] = email
        rec["validation_status"] = status
        rec["validation_reason"] = reason

    if is_mv_valid(status):
        rec["resolved_valid_email"] = email

    state[ekey] = rec


def refresh_cvl_validation_views() -> int:
    """Sync employee_email_state.csv into linkedin_data.db and rebuild SQL views."""
    script = apply_validation_views_script()
    if not script.is_file():
        logger.error("CVL views script not found: %s", script)
        return 1
    cmd = [sys.executable, str(script)]
    logger.info("Refreshing CVL validation views: %s", script)
    print(f"\n>>> {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--validation-cycle", type=int, default=None)
    p.add_argument("--source-batch-prefix", default="mv_cycle")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--list-ready", action="store_true")
    p.add_argument(
        "--skip-views",
        action="store_true",
        help="Do not run apply_validation_views.py after DB update",
    )
    return p.parse_args()


def _validation_ready_for_db(row: dict) -> bool:
    """Completed, or partial only when every validatable email has a result."""
    notes = (row.get("notes") or "").strip()
    if notes == "db_updated":
        return False
    st = (row.get("status") or "").strip()
    if st == "completed":
        return True
    if st == "partial":
        processed = int(row.get("rows_processed") or 0)
        total = int(row.get("rows_total") or 0)
        return total > 0 and processed >= total
    return False


def find_validation_target(cycle_number: int | None) -> dict | None:
    rows = read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS)
    if cycle_number is not None:
        for row in rows:
            if int(row.get("cycle_number") or 0) == cycle_number:
                return row
        return None
    ready = [r for r in rows if _validation_ready_for_db(r)]
    if not ready:
        return None
    return max(ready, key=lambda r: int(r.get("cycle_number") or 0))


def main() -> int:
    args = parse_args()
    setup_pipeline_logging("update_db_cycle")
    ensure_registry_db()

    if args.list_ready:
        for row in read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS):
            logger.info(
                "cycle %s: status=%s notes=%s file=%s",
                row.get("cycle_number"),
                row.get("status"),
                row.get("notes"),
                row.get("validation_file"),
            )
        n_open = count_open_unprocessed()
        if n_open:
            logger.info("Unprocessed validation backlog: %s row(s)", n_open)
        return 0

    val_row = find_validation_target(args.validation_cycle)
    if not val_row:
        logger.error(
            "No validation pending DB update (need status=completed, or partial "
            "with all validatable emails processed)."
        )
        return 2 if args.validation_cycle is None else 1

    if not _validation_ready_for_db(val_row):
        logger.error(
            "Validation cycle %s is partial (%s/%s) — resume validate_cycle.py first.",
            val_row.get("cycle_number"),
            val_row.get("rows_processed"),
            val_row.get("rows_total"),
        )
        return 1

    val_cycle = int(val_row["cycle_number"])
    csv_path = resolve_data_path(val_row["validation_file"])
    if not csv_path.exists():
        logger.error("Validation CSV not found: %s", csv_path)
        return 1

    db_path = args.db.resolve()
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    rows = read_validation_csv(csv_path)
    db_records = []
    for row in rows:
        rec = parse_row_to_record(row)
        if rec:
            db_records.append(rec)

    if not db_records:
        logger.error("No parsable validation rows.")
        return 1

    validated_at = datetime.now(timezone.utc).isoformat()
    imported_at = validated_at
    source_batch = f"{args.source_batch_prefix}_{val_cycle}"

    backup_path = None
    if not args.no_backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.backup_before_mv_{ts}")
        shutil.copy2(db_path, backup_path)
        logger.info("Database backup: %s", backup_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        ensure_columns(cursor)
        affected, n_read = upsert_mv_results(
            cursor,
            db_records,
            validated_at=validated_at,
            source_batch=source_batch,
            imported_at=imported_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    employee_state = load_employee_state()
    company_state = load_company_format_state()
    valid_count = 0
    probe_groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        apply_row_to_employee_state(employee_state, row)
        if is_mv_valid(row.get("validation_status", "")):
            valid_count += 1
        mode = (row.get("extraction_mode") or "probe").strip().lower()
        if mode == "probe":
            cname = (row.get("company_name") or "").strip()
            fmt = (row.get("email_format") or "").strip()
            domain = (row.get("company_domain") or "").strip()
            probe_groups.setdefault((cname, domain, fmt), []).append(row)

    probe_works = 0
    probe_failed = 0
    for (cname, domain, fmt), group in probe_groups.items():
        outcome = apply_probe_validation_results(
            company_state, cname, domain, fmt, group
        )
        if outcome == "works":
            probe_works += 1
            logger.info(
                "  Company probe OK — %s @ %s format=%s (%s/%s valid)",
                cname,
                domain,
                fmt,
                sum(1 for r in group if is_mv_valid(r.get("validation_status", ""))),
                len(group),
            )
        elif outcome == "failed":
            probe_failed += 1
            logger.info(
                "  Company probe failed — %s @ %s format=%s → try next format",
                cname,
                domain,
                fmt,
            )

    save_employee_state(employee_state)
    save_company_format_state(company_state)
    if probe_groups:
        logger.info(
            "  Company probes: %s works, %s failed (from %s companies)",
            probe_works,
            probe_failed,
            len(probe_groups),
        )

    now = datetime.now(timezone.utc).isoformat()
    update_manifest_row(
        VALIDATION_MANIFEST,
        VALIDATION_FIELDS,
        val_cycle,
        {"notes": "db_updated", "updated_at": now},
    )
    ext_cycle = int(val_row.get("extraction_cycle_number") or 0)
    if ext_cycle:
        update_manifest_row(
            EXTRACTION_MANIFEST,
            EXTRACTION_FIELDS,
            ext_cycle,
            {"status": "db_updated"},
        )

    mark_db_updated(
        val_row.get("extraction_file") or "",
        val_row.get("validation_file") or "",
    )
    log_unprocessed_summary(logger)

    summary = {
        "validation_cycle": val_cycle,
        "csv": str(csv_path),
        "rows_read": n_read,
        "sqlite_changes": affected,
        "valid_emails_in_csv": valid_count,
        "source_batch": source_batch,
        "backup": str(backup_path) if backup_path else None,
        "employee_state": str(EMPLOYEE_STATE_CSV),
        "company_probe_works": probe_works,
        "company_probe_failed": probe_failed,
    }
    logger.info("DB update summary: %s", json.dumps(summary, indent=2))

    if not args.skip_views:
        code = refresh_cvl_validation_views()
        if code != 0:
            logger.error("CVL validation views refresh failed (exit %s)", code)
            return code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
