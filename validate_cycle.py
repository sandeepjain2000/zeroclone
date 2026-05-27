#!/usr/bin/env python3
"""
Validate extraction cycles registered in extraction_manifest.csv.

- Picks the next extraction with status pending_validation (or --cycle).
- Writes validated_{format}_{tag}_c{NNNN}.csv under cycles/data/.
- Records progress in validation_manifest.csv (partial on API quota).
- Re-run with --resume to continue a partial validation.

Usage:
  python validate_cycle.py
  python validate_cycle.py --extraction-cycle 3 --resume
  python validate_cycle.py --list-pending
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cycle_registry import (
    DATA_DIR,
    EXTRACTION_FIELDS,
    EXTRACTION_MANIFEST,
    VALIDATION_FIELDS,
    VALIDATION_MANIFEST,
    append_manifest_row,
    ensure_dirs,
    next_cycle_number,
    read_manifest,
    relative_data_path,
    resolve_data_path,
    timestamp_tag,
    update_manifest_row,
    validation_filename,
    write_manifest,
)
from email_formats import is_mv_valid

# Reuse validation core from validate_emails.py
from validate_emails import (
    ApifyApiError,
    ApifyClient,
    get_provider_settings,
    load_config,
    load_existing_results_from_output,
    looks_like_email,
    normalize_email,
    parse_permission_level,
    read_input_csv,
    summarize_result,
    validate_batches,
    write_output_csv,
    DEFAULT_CONFIG,
)

from pipeline_logging import get_logger, setup_pipeline_logging

logger = get_logger("validate_cycle")

SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument(
        "--extraction-cycle",
        type=int,
        default=None,
        help="Extraction manifest cycle_number to validate",
    )
    p.add_argument(
        "--validation-cycle",
        type=int,
        default=None,
        help="Resume an existing validation manifest row",
    )
    p.add_argument("--resume", action="store_true", help="Resume partial validation output")
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--list-pending", action="store_true")
    return p.parse_args()


def find_extraction_row(cycle_number: int | None) -> dict | None:
    rows = read_manifest(EXTRACTION_MANIFEST, EXTRACTION_FIELDS)
    if cycle_number is not None:
        for row in rows:
            if int(row.get("cycle_number") or 0) == cycle_number:
                return row
        return None
    for row in rows:
        if (row.get("status") or "").strip() == "pending_validation":
            return row
    return None


def find_validation_row(cycle_number: int) -> dict | None:
    for row in read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS):
        if int(row.get("cycle_number") or 0) == cycle_number:
            return row
    return None


def count_results(results_by_email: dict) -> tuple[int, int, int]:
    ok = invalid = 0
    for item in results_by_email.values():
        status, _ = summarize_result(item)
        if is_mv_valid(str(status)):
            ok += 1
        elif status:
            invalid += 1
    return len(results_by_email), ok, invalid


def list_pending() -> None:
    ext = read_manifest(EXTRACTION_MANIFEST, EXTRACTION_FIELDS)
    val = read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS)
    print("Extraction manifest:")
    for row in ext:
        print(
            f"  cycle {row.get('cycle_number')}: {row.get('status')} "
            f"format={row.get('email_format')} file={row.get('extraction_file')} "
            f"rows={row.get('row_count')}"
        )
    print("Validation manifest:")
    for row in val:
        print(
            f"  cycle {row.get('cycle_number')}: {row.get('status')} "
            f"processed={row.get('rows_processed')}/{row.get('rows_total')} "
            f"file={row.get('validation_file')}"
        )


def main() -> int:
    args = parse_args()
    ensure_dirs()
    setup_pipeline_logging("validate_cycle", also_configure=("validate_emails",))

    if args.list_pending:
        list_pending()
        return 0

    if ApifyClient is None:
        logger.error("Install apify-client: pip install apify-client")
        return 2

    config = load_config(args.config)
    provider = get_provider_settings(config)
    batch_size = args.batch_size or int(config.get("defaults", {}).get("batch_size", 200))

    validation_row = None
    extraction_row = None

    if args.validation_cycle:
        validation_row = find_validation_row(args.validation_cycle)
        if not validation_row:
            logger.error("Validation cycle %s not in manifest", args.validation_cycle)
            return 1
        ext_n = int(validation_row.get("extraction_cycle_number") or 0)
        extraction_row = find_extraction_row(ext_n)
    else:
        extraction_row = find_extraction_row(args.extraction_cycle)
        if not extraction_row:
            logger.error("No pending_validation extraction found.")
            return 1

    ext_cycle = int(extraction_row["cycle_number"])
    email_format = extraction_row.get("email_format", "")
    input_path = resolve_data_path(extraction_row["extraction_file"])
    if not input_path.exists():
        logger.error("Extraction file missing: %s", input_path)
        return 1

    tag = timestamp_tag()
    if validation_row:
        val_cycle = int(validation_row["cycle_number"])
        out_name = Path(validation_row["validation_file"]).name
        out_path = resolve_data_path(validation_row["validation_file"])
        resume = args.resume or (validation_row.get("status") == "partial")
    else:
        val_cycle = next_cycle_number(VALIDATION_MANIFEST)
        out_name = validation_filename(email_format, val_cycle, tag)
        out_path = DATA_DIR / out_name
        resume = args.resume
        append_manifest_row(
            VALIDATION_MANIFEST,
            VALIDATION_FIELDS,
            {
                "cycle_number": val_cycle,
                "extraction_cycle_number": ext_cycle,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": "",
                "email_format": email_format,
                "extraction_file": extraction_row["extraction_file"],
                "validation_file": relative_data_path(out_name),
                "rows_total": extraction_row.get("row_count", 0),
                "rows_processed": 0,
                "rows_ok": 0,
                "rows_invalid": 0,
                "status": "running",
                "error_message": "",
                "notes": "",
            },
        )

    original_headers, _, rows = read_input_csv(str(input_path), "email")
    unique_emails: list[str] = []
    seen: set[str] = set()
    for row in rows:
        email = row.get("_email_to_validate", "")
        if not email or not looks_like_email(email):
            continue
        key = email.lower()
        if key not in seen:
            seen.add(key)
            unique_emails.append(email)

    existing_results = {}
    if resume and out_path.exists():
        existing_results = load_existing_results_from_output(out_path)
        unique_emails = [e for e in unique_emails if e.lower() not in existing_results]

    rows_total = int(extraction_row.get("row_count") or len(seen))
    now = datetime.now(timezone.utc).isoformat()

    if not unique_emails and existing_results:
        write_output_csv(str(out_path), original_headers, rows, existing_results)
        processed, ok, invalid = count_results(existing_results)
        update_manifest_row(
            VALIDATION_MANIFEST,
            VALIDATION_FIELDS,
            val_cycle,
            {
                "updated_at": now,
                "rows_processed": processed,
                "rows_ok": ok,
                "rows_invalid": invalid,
                "status": "completed",
                "error_message": "",
            },
        )
        update_manifest_row(
            EXTRACTION_MANIFEST,
            EXTRACTION_FIELDS,
            ext_cycle,
            {"status": "validated"},
        )
        logger.info("Already complete. %s", out_path)
        return 0

    client = ApifyClient(provider["token"])
    permission_level = parse_permission_level(
        config.get("defaults", {}).get("force_permission_level")
    )
    merged = dict(existing_results)

    try:
        merged, _, completed, err = validate_batches(
            client=client,
            actor_id=provider["actor_id"],
            input_key=provider["input_key"],
            emails=unique_emails,
            batch_size=batch_size,
            permission_level=permission_level,
            existing_results=merged,
        )
    except ApifyApiError as error:
        if merged:
            completed = False
            err = str(error)
        else:
            raise

    write_output_csv(str(out_path), original_headers, rows, merged)
    processed, ok, invalid = count_results(merged)
    status = "completed" if completed and processed >= rows_total else "partial"
    if completed and processed < rows_total:
        status = "partial"

    update_manifest_row(
        VALIDATION_MANIFEST,
        VALIDATION_FIELDS,
        val_cycle,
        {
            "updated_at": now,
            "rows_processed": processed,
            "rows_ok": ok,
            "rows_invalid": invalid,
            "status": status,
            "error_message": err if status == "partial" else "",
        },
    )
    if status == "completed":
        update_manifest_row(
            EXTRACTION_MANIFEST,
            EXTRACTION_FIELDS,
            ext_cycle,
            {"status": "validated"},
        )

    logger.info(
        "Validation cycle %s | extraction %s | %s | %s/%s processed (ok=%s invalid=%s)",
        val_cycle,
        ext_cycle,
        status,
        processed,
        rows_total,
        ok,
        invalid,
    )
    logger.info("Output: %s", out_path)
    return 0 if status == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
