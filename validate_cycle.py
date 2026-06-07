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

from file_registry import (
    FileRegistryError,
    assert_can_validate_extraction,
    assert_new_output_filename,
    ensure_registry_db,
    find_open_validation_output,
    log_unprocessed_summary,
    register_processed_file,
    resolve_unprocessed_for_extraction,
    save_unprocessed_rows,
    update_processed_file,
    upsert_processed_file,
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


def find_latest_open_validation(ext_cycle: int) -> dict | None:
    """
    Reuse the newest validation job for this extraction instead of creating
    another Apify run on the same extract CSV (avoids duplicate billing).
    """
    open_rows: list[dict] = []
    for row in read_manifest(VALIDATION_MANIFEST, VALIDATION_FIELDS):
        if int(row.get("extraction_cycle_number") or 0) != ext_cycle:
            continue
        if (row.get("notes") or "").strip() == "db_updated":
            continue
        if (row.get("status") or "").strip() in ("partial", "running", "completed"):
            open_rows.append(row)
    if not open_rows:
        return None
    return max(open_rows, key=lambda r: int(r.get("cycle_number") or 0))


def _resolve_status(
    *,
    api_completed: bool,
    processed: int,
    validatable_total: int,
    err: str,
) -> tuple[str, str]:
    """Mark completed when every validatable email in the extract has a result."""
    if validatable_total > 0 and processed >= validatable_total:
        return "completed", ""
    if api_completed and processed >= validatable_total:
        return "completed", ""
    return "partial", err


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
    ensure_registry_db()
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
        validation_row = find_latest_open_validation(int(extraction_row["cycle_number"]))
        if validation_row:
            logger.info(
                "Reusing validation cycle %s for extraction %s (same batch — "
                "not creating a duplicate Apify run).",
                validation_row.get("cycle_number"),
                extraction_row.get("cycle_number"),
            )

    ext_cycle = int(extraction_row["cycle_number"])
    email_format = extraction_row.get("email_format", "")
    extraction_rel = extraction_row["extraction_file"]
    input_path = resolve_data_path(extraction_rel)
    if not input_path.exists():
        logger.error("Extraction file missing: %s", input_path)
        return 1

    try:
        if not validation_row:
            assert_can_validate_extraction(extraction_rel)
        upsert_processed_file(
            extraction_rel,
            file_kind="extract",
            cycle_number=ext_cycle,
            extraction_cycle=ext_cycle,
            status=(extraction_row.get("status") or "pending_validation"),
            rows_total=int(extraction_row.get("row_count") or 0),
            notes="registered_at_validate",
        )
    except FileRegistryError as exc:
        logger.error("%s", exc)
        return 1

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

    validatable_total = len(seen)

    tag = timestamp_tag()
    validation_rel = ""
    if validation_row:
        val_cycle = int(validation_row["cycle_number"])
        out_name = Path(validation_row["validation_file"]).name
        validation_rel = validation_row["validation_file"]
        out_path = resolve_data_path(validation_rel)
        resume = args.resume or out_path.exists() or (
            (validation_row.get("status") or "") == "partial"
        )
        upsert_processed_file(
            validation_rel,
            file_kind="validation_output",
            validation_cycle=val_cycle,
            extraction_cycle=ext_cycle,
            status=(validation_row.get("status") or "running"),
            rows_total=validatable_total,
            rows_processed=int(validation_row.get("rows_processed") or 0),
            notes=f"extract={extraction_rel}",
        )
    else:
        open_reg = find_open_validation_output(extraction_rel)
        if open_reg:
            logger.error(
                "Partial validation already registered for this extract (%s). "
                "Resume with: validate_cycle.py --validation-cycle %s --resume",
                open_reg["file_path"],
                open_reg.get("validation_cycle"),
            )
            return 1
        val_cycle = next_cycle_number(VALIDATION_MANIFEST)
        out_name = validation_filename(email_format, val_cycle, tag)
        validation_rel = relative_data_path(out_name)
        try:
            assert_new_output_filename(out_name)
        except FileRegistryError as exc:
            logger.error("%s", exc)
            return 1
        out_path = DATA_DIR / out_name
        resume = args.resume
        register_processed_file(
            validation_rel,
            file_kind="validation_output",
            validation_cycle=val_cycle,
            extraction_cycle=ext_cycle,
            status="running",
            rows_total=validatable_total,
            notes=f"extract={extraction_rel}",
        )
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
                "validation_file": validation_rel,
                "rows_total": validatable_total,
                "rows_processed": 0,
                "rows_ok": 0,
                "rows_invalid": 0,
                "status": "running",
                "error_message": "",
                "notes": "",
            },
        )

    existing_results = {}
    if resume and out_path.exists():
        existing_results = load_existing_results_from_output(out_path)
        unique_emails = [e for e in unique_emails if e.lower() not in existing_results]

    rows_total = validatable_total
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
        update_processed_file(
            validation_rel,
            status="completed",
            rows_processed=processed,
            rows_total=validatable_total,
        )
        resolve_unprocessed_for_extraction(extraction_rel)
        logger.info("Already complete. %s", out_path)
        log_unprocessed_summary(logger)
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
    status, err_msg = _resolve_status(
        api_completed=completed,
        processed=processed,
        validatable_total=validatable_total,
        err=err or "",
    )

    update_manifest_row(
        VALIDATION_MANIFEST,
        VALIDATION_FIELDS,
        val_cycle,
        {
            "updated_at": now,
            "rows_total": validatable_total,
            "rows_processed": processed,
            "rows_ok": ok,
            "rows_invalid": invalid,
            "status": status,
            "error_message": err_msg,
        },
    )
    if status == "completed":
        update_manifest_row(
            EXTRACTION_MANIFEST,
            EXTRACTION_FIELDS,
            ext_cycle,
            {"status": "validated"},
        )
        update_processed_file(extraction_rel, status="validated")
        resolve_unprocessed_for_extraction(extraction_rel)
    else:
        n_unproc = save_unprocessed_rows(
            rows,
            merged,
            extraction_file=extraction_rel,
            validation_file=validation_rel,
            validation_cycle=val_cycle,
            reason=err_msg or "partial_validation",
        )
        logger.info(
            "  Saved %s unprocessed email(s) to validation_unprocessed table",
            n_unproc,
        )

    update_processed_file(
        validation_rel,
        status=status,
        rows_processed=processed,
        rows_total=validatable_total,
        notes=f"extract={extraction_rel};ok={ok};invalid={invalid}",
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
    log_unprocessed_summary(logger)
    return 0 if status == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
