import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from apify_client import ApifyClient
    from apify_client.errors import ApifyApiError
    from apify_shared.consts import ActorPermissionLevel
except ImportError:
    ApifyClient = None
    ApifyApiError = None
    ActorPermissionLevel = None


DEFAULT_CONFIG = "provider_config.json"
DEFAULT_INPUT = "emails.csv"
DEFAULT_OUTPUT = "validated_emails.csv"
DEFAULT_BATCH_SIZE = 200
# Pause between Apify actor batches to reduce rate-limit risk (seconds).
BATCH_SLEEP_SECONDS = 60
DEFAULT_ACTOR_ID = "VJ5w50TP6mAbyimyO"
DEFAULT_INPUT_KEY = "emails"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMMON_EMAIL_COLUMNS = {
    "email",
    "emails",
    "email_address",
    "email address",
    "e-mail",
    "mail",
}

from pipeline_logging import get_logger, setup_pipeline_logging

logger = get_logger("validate_emails")


def setup_run_logging() -> Path:
    """Console + timestamped file under ``zeroclone/logs/``."""
    return setup_pipeline_logging("validate_emails")


def load_config(config_path):
    path = Path(config_path)
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def parse_args():
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", default=DEFAULT_CONFIG)
    bootstrap_args, _ = bootstrap.parse_known_args()

    config = load_config(bootstrap_args.config)
    defaults = config.get("defaults", {})

    parser = argparse.ArgumentParser(
        description="Validate email addresses in batches using the Apify Million Verifier actor."
    )
    parser.add_argument("--config", default=bootstrap_args.config)
    parser.add_argument("--input", default=defaults.get("input_file", DEFAULT_INPUT))
    parser.add_argument("--output", default=defaults.get("output_file", DEFAULT_OUTPUT))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(defaults.get("batch_size", DEFAULT_BATCH_SIZE)),
    )
    parser.add_argument(
        "--email-column",
        default=defaults.get("email_column"),
        help="CSV column containing emails. If omitted, the script tries to detect it.",
    )
    parser.add_argument(
        "--force-permission-level",
        default=defaults.get("force_permission_level"),
        choices=("FULL_PERMISSIONS", "LIMITED_PERMISSIONS"),
        help=(
            "Override Apify actor permissions for this run. Use FULL_PERMISSIONS "
            "only after you understand and accept the actor's requested access."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip emails already present in --output with validation_raw_json set.",
    )
    return parser.parse_args()


def normalize_email(value):
    return str(value or "").strip()


def looks_like_email(value):
    return bool(EMAIL_RE.match(normalize_email(value)))


def find_email_column(headers, requested_column=None):
    if requested_column:
        for header in headers:
            if header == requested_column:
                return header
        lowered = {header.lower(): header for header in headers}
        if requested_column.lower() in lowered:
            return lowered[requested_column.lower()]
        raise ValueError(f"Email column '{requested_column}' was not found in the CSV.")

    for header in headers:
        if header.strip().lower() in COMMON_EMAIL_COLUMNS:
            return header

    return None


def read_input_csv(input_path, requested_column=None):
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        raw_rows = [row for row in reader if any(cell.strip() for cell in row)]

    if not raw_rows:
        raise ValueError("Input CSV is empty.")

    first_row = raw_rows[0]
    detected_column = find_email_column(first_row, requested_column)

    if detected_column:
        headers = first_row
        email_index = headers.index(detected_column)
        data_rows = raw_rows[1:]
    else:
        headers = [f"col_{index + 1}" for index in range(len(first_row))]
        email_index = 0
        data_rows = raw_rows
        detected_column = headers[email_index]

    rows = []
    for row_number, row in enumerate(data_rows, start=2 if detected_column in first_row else 1):
        padded_row = row + [""] * (len(headers) - len(row))
        row_dict = dict(zip(headers, padded_row[: len(headers)]))
        email = normalize_email(row_dict.get(detected_column))
        if not email:
            row_dict["_input_row_number"] = row_number
            row_dict["_email_to_validate"] = ""
            rows.append(row_dict)
            continue

        row_dict["_input_row_number"] = row_number
        row_dict["_email_to_validate"] = email
        rows.append(row_dict)

    return headers, detected_column, rows


def chunked(items, batch_size):
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def get_provider_settings(config):
    apify_config = config.get("apify", {})
    token = apify_config.get("api_token") or os.getenv("APIFY_TOKEN")

    if token and token.startswith("PASTE_"):
        token = os.getenv("APIFY_TOKEN")

    if not token:
        raise ValueError(
            "Missing Apify API token. Put it in provider_config.json as apify.api_token "
            "or set the APIFY_TOKEN environment variable."
        )

    return {
        "token": token,
        "actor_id": apify_config.get("actor_id", DEFAULT_ACTOR_ID),
        "input_key": apify_config.get("input_key", DEFAULT_INPUT_KEY),
    }


def parse_permission_level(value):
    if not value:
        return None
    if ActorPermissionLevel is None:
        raise ValueError("apify-shared is required for --force-permission-level.")
    return ActorPermissionLevel[value]


def extract_result_email(item):
    for key in ("email", "Email", "input", "address", "email_address"):
        value = item.get(key)
        if isinstance(value, str) and looks_like_email(value):
            return normalize_email(value)
    return None


def pick_first(item, keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return ""


def summarize_result(item):
    if not item:
        return "", ""

    status = pick_first(
        item,
        (
            "status",
            "result",
            "state",
            "quality",
            "validation_status",
            "email_status",
            "deliverability",
            "isValid",
            "valid",
        ),
    )
    reason = pick_first(
        item,
        (
            "reason",
            "message",
            "description",
            "subresult",
            "sub_status",
            "error",
            "details",
        ),
    )
    return status, reason


def validate_batches(
    client,
    actor_id,
    input_key,
    emails,
    batch_size,
    permission_level=None,
    start_batch_index=0,
    existing_results=None,
    on_actor_finished=None,
):
    """
    Validate emails in Apify batches.

    Returns (results_by_email, ordered_results, completed_fully, error_message).
    On quota/API failure after at least one batch, returns partial results with
    completed_fully=False.
    """
    results_by_email = dict(existing_results or {})
    ordered_results = []
    total_batches = (len(emails) + batch_size - 1) // batch_size
    quota_markers = (
        "quota",
        "limit",
        "insufficient",
        "payment",
        "credit",
        "balance",
        "exceeded",
    )

    for batch_number, batch in enumerate(chunked(emails, batch_size), start=1):
        if batch_number <= start_batch_index:
            continue
        logger.info(
            "Running batch %s/%s (%s emails)...",
            batch_number,
            total_batches,
            len(batch),
        )
        try:
            run = client.actor(actor_id).call(
                run_input={input_key: batch},
                force_permission_level=permission_level,
            )
        except ApifyApiError as error:
            message = str(error).lower()
            if any(m in message for m in quota_markers) and results_by_email:
                logger.warning("API quota/limit hit after partial run: %s", error)
                return results_by_email, ordered_results, False, str(error)
            raise

        if on_actor_finished:
            on_actor_finished(batch_number, total_batches, len(batch))

        dataset_id = run["defaultDatasetId"]
        items = list(client.dataset(dataset_id).iterate_items())

        for item in items:
            ordered_results.append(item)
            result_email = extract_result_email(item)
            if result_email:
                results_by_email[result_email.lower()] = item

        if not any(extract_result_email(item) for item in items):
            for email, item in zip(batch, items):
                results_by_email[email.lower()] = item

        if batch_number < total_batches and BATCH_SLEEP_SECONDS > 0:
            logger.info(
                "Sleeping %ss before next batch (rate-limit spacing)...",
                BATCH_SLEEP_SECONDS,
            )
            time.sleep(BATCH_SLEEP_SECONDS)

    return results_by_email, ordered_results, True, ""


def log_validation_summary(results_by_email: dict) -> None:
    """Aggregate Million Verifier-style outcomes for the run log."""
    counts: Counter[str] = Counter()
    for item in results_by_email.values():
        status, _ = summarize_result(item)
        counts[status or "(blank)"] += 1
    logger.info("--- Validation summary (%s addresses) ---", len(results_by_email))
    for label, n in counts.most_common():
        logger.info("  %s: %s", label, n)


def load_existing_results_from_output(output_path: Path) -> dict:
    """Load prior validation results for --resume."""
    path = Path(output_path)
    if not path.exists():
        return {}
    results: dict = {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            email = normalize_email(row.get("email") or row.get("_email_to_validate", ""))
            raw = (row.get("validation_raw_json") or "").strip()
            if not email or not raw:
                continue
            try:
                results[email.lower()] = json.loads(raw)
            except json.JSONDecodeError:
                continue
    return results


def write_output_csv(output_path, original_headers, rows, results_by_email):
    output_headers = original_headers + [
        "validation_status",
        "validation_reason",
        "validation_raw_json",
    ]

    with Path(output_path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=output_headers, extrasaction="ignore")
        writer.writeheader()

        for row in rows:
            email = row.get("_email_to_validate", "")
            result = results_by_email.get(email.lower()) if email else None
            status, reason = summarize_result(result)

            output_row = {header: row.get(header, "") for header in original_headers}
            output_row["validation_status"] = status
            output_row["validation_reason"] = reason
            output_row["validation_raw_json"] = (
                json.dumps(result, ensure_ascii=False) if result else ""
            )
            writer.writerow(output_row)


def main():
    args = parse_args()
    setup_run_logging()

    if args.batch_size < 1:
        logger.error("--batch-size must be at least 1.")
        return 2

    if ApifyClient is None:
        logger.error(
            "Missing dependency: apify-client. Install it with: pip install apify-client"
        )
        return 2

    config = load_config(args.config)
    provider = get_provider_settings(config)

    original_headers, email_column, rows = read_input_csv(args.input, args.email_column)
    unique_emails = []
    seen = set()
    skipped_invalid = 0

    for row in rows:
        email = row.get("_email_to_validate", "")
        if not email:
            continue
        if not looks_like_email(email):
            skipped_invalid += 1
            continue
        key = email.lower()
        if key not in seen:
            seen.add(key)
            unique_emails.append(email)

    logger.info("Input file: %s", args.input)
    logger.info("Output file: %s", args.output)
    logger.info("Detected email column: %s", email_column)
    logger.info("Rows: %s", len(rows))
    logger.info("Unique emails to validate: %s", len(unique_emails))
    if skipped_invalid:
        logger.info(
            "Skipped invalid-looking emails before API call: %s", skipped_invalid
        )

    existing_results = {}
    if args.resume:
        existing_results = load_existing_results_from_output(args.output)
        if existing_results:
            logger.info("Resume: loaded %s prior results from %s", len(existing_results), args.output)
        unique_emails = [e for e in unique_emails if e.lower() not in existing_results]

    if not unique_emails and existing_results:
        write_output_csv(args.output, original_headers, rows, existing_results)
        logger.info("All emails already validated. Wrote: %s", args.output)
        return 0

    if not unique_emails:
        write_output_csv(args.output, original_headers, rows, {})
        logger.info("No emails to validate. Wrote: %s", args.output)
        return 0

    client = ApifyClient(provider["token"])
    permission_level = parse_permission_level(args.force_permission_level)
    merged_results = dict(existing_results)
    try:
        new_results, _, completed, err = validate_batches(
            client=client,
            actor_id=provider["actor_id"],
            input_key=provider["input_key"],
            emails=unique_emails,
            batch_size=args.batch_size,
            permission_level=permission_level,
            existing_results=merged_results,
        )
        merged_results = new_results
    except ApifyApiError as error:
        message = str(error)
        if "requires full access" in message:
            logger.error(
                "Apify rejected the run because this actor requires full account access.\n"
                "Approve it in the Apify Console once, or rerun with:\n"
                "  --force-permission-level FULL_PERMISSIONS"
            )
            return 2
        if merged_results:
            logger.warning("Stopped with partial results: %s", error)
            write_output_csv(args.output, original_headers, rows, merged_results)
            log_validation_summary(merged_results)
            logger.info("Partial output: %s", args.output)
            return 3
        raise

    log_validation_summary(merged_results)
    write_output_csv(args.output, original_headers, rows, merged_results)
    if not completed:
        logger.warning("Partial validation (quota/limit): %s", err)
        logger.info("Wrote partial results: %s", args.output)
        return 3
    logger.info("Done. Wrote: %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
