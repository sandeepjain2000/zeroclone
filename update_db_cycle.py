#!/usr/bin/env python3
"""
Apply completed validation cycles to linkedin_data.db and employee_email_state.csv.

- Reads validation_manifest.csv for status=completed (or --validation-cycle N).
- Upserts Million Verifier fields into zerobounce_validation.
- Updates per-employee format status for the next extraction stage.

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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--validation-cycle", type=int, default=None)
    p.add_argument("--source-batch-prefix", default="mv_cycle")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--list-ready", action="store_true")
    return p.parse_args()


def find_validation_target(cycle_number: int | None) -> dict | None:
    rows = read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS)
    if cycle_number is not None:
        for row in rows:
            if int(row.get("cycle_number") or 0) == cycle_number:
                return row
        return None
    for row in rows:
        st = (row.get("status") or "").strip()
        notes = (row.get("notes") or "").strip()
        if st == "completed" and notes != "db_updated":
            return row
    return None


def main() -> int:
    args = parse_args()
    setup_pipeline_logging("update_db_cycle")

    if args.list_ready:
        for row in read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS):
            logger.info(
                "cycle %s: status=%s notes=%s file=%s",
                row.get("cycle_number"),
                row.get("status"),
                row.get("notes"),
                row.get("validation_file"),
            )
        return 0

    val_row = find_validation_target(args.validation_cycle)
    if not val_row:
        logger.error("No completed validation pending DB update.")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
