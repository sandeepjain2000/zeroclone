"""
Apply Million Verifier (Apify) CSV results to linkedin_data.db → zerobounce_validation.

- Upserts by email: new rows get MV fields + optional source_batch; existing rows
  get MV columns updated only (zb_* from ZeroBounce is left unchanged on conflict).
- Default input: validated_unsent_500.csv next to this script.

Usage:
  python update_db_with_validation.py
  python update_db_with_validation.py --csv validated_unsent_500.csv
  python update_db_with_validation.py --csv other.csv --source-batch my_batch
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

from cycle_registry import linkedin_db_path

_SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = linkedin_db_path()
DEFAULT_CSV = _SCRIPT_DIR / "validated_unsent_500.csv"
TABLE_NAME = "zerobounce_validation"

NEW_COLUMNS = {
    "mv_status": "TEXT",
    "mv_quality": "TEXT",
    "mv_resultcode": "INTEGER",
    "mv_subresult": "TEXT",
    "mv_free": "INTEGER",
    "mv_role": "INTEGER",
    "mv_didyoumean": "TEXT",
    "mv_error": "TEXT",
    "mv_validated_at": "TEXT",
    "mv_raw_json": "TEXT",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"Validation CSV (default: {DEFAULT_CSV.name})",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to linkedin_data.db",
    )
    p.add_argument(
        "--source-batch",
        default="mv_csv_validated_unsent_500",
        help="source_batch value for newly inserted rows",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not copy DB to .backup_before_mv_* before writing",
    )
    return p.parse_args()


def parse_row_to_record(row: dict) -> dict | None:
    email = (row.get("email") or "").strip()
    if not email:
        return None

    raw_s = (row.get("validation_raw_json") or "").strip()
    if raw_s:
        try:
            raw = json.loads(raw_s)
        except json.JSONDecodeError:
            raw = {
                "email": email,
                "result": row.get("validation_status") or "",
                "subresult": row.get("validation_reason") or "",
                "error": "invalid_json_in_csv",
            }
    else:
        raw = {
            "email": email,
            "result": row.get("validation_status") or "",
            "subresult": row.get("validation_reason") or "",
        }

    rc = raw.get("resultcode")
    if rc is not None and rc != "":
        try:
            rc = int(rc)
        except (TypeError, ValueError):
            rc = None
    else:
        rc = None

    return {
        "email": email,
        "mv_status": (raw.get("result") or row.get("validation_status") or "").strip(),
        "mv_quality": (raw.get("quality") or "").strip(),
        "mv_resultcode": rc,
        "mv_subresult": (
            raw.get("subresult") or row.get("validation_reason") or ""
        ).strip(),
        "mv_free": 1 if raw.get("free") else 0,
        "mv_role": 1 if raw.get("role") else 0,
        "mv_didyoumean": (raw.get("didyoumean") or "").strip(),
        "mv_error": (raw.get("error") or "").strip(),
        "mv_raw_json": raw_s if raw_s else json.dumps(raw, ensure_ascii=False),
    }


def read_results(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    out: list[dict] = []
    for row in rows:
        rec = parse_row_to_record(row)
        if rec:
            out.append(rec)
    return out


def ensure_columns(cursor: sqlite3.Cursor) -> list[str]:
    existing_columns = {r[1] for r in cursor.execute(f"PRAGMA table_info({TABLE_NAME})")}
    added = []
    for column_name, column_type in NEW_COLUMNS.items():
        if column_name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {column_type}"
            )
            added.append(column_name)
    return added


def upsert_mv_results(
    cursor: sqlite3.Cursor,
    results: list[dict],
    validated_at: str,
    source_batch: str,
    imported_at: str,
) -> tuple[int, int]:
    """
    Returns (rows_affected_by_upsert, rows_read).
    SQLite counts each INSERT OR REPLACE... conflict update as 1 change.
    """
    affected = 0
    for result in results:
        cursor.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                email_address,
                zb_status,
                zb_sub_status,
                zb_account,
                zb_free_email,
                source_batch,
                imported_at,
                mv_status,
                mv_quality,
                mv_resultcode,
                mv_subresult,
                mv_free,
                mv_role,
                mv_didyoumean,
                mv_error,
                mv_validated_at,
                mv_raw_json
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email_address) DO UPDATE SET
                mv_status = excluded.mv_status,
                mv_quality = excluded.mv_quality,
                mv_resultcode = excluded.mv_resultcode,
                mv_subresult = excluded.mv_subresult,
                mv_free = excluded.mv_free,
                mv_role = excluded.mv_role,
                mv_didyoumean = excluded.mv_didyoumean,
                mv_error = excluded.mv_error,
                mv_validated_at = excluded.mv_validated_at,
                mv_raw_json = excluded.mv_raw_json
            """,
            (
                result["email"].lower(),
                source_batch,
                imported_at,
                result["mv_status"],
                result["mv_quality"],
                result["mv_resultcode"],
                result["mv_subresult"],
                result["mv_free"],
                result["mv_role"],
                result["mv_didyoumean"],
                result["mv_error"],
                validated_at,
                result["mv_raw_json"],
            ),
        )
        affected += cursor.rowcount
    return affected, len(results)


def main() -> int:
    args = parse_args()
    csv_path = args.csv.resolve()
    db_path = args.db.resolve()

    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1
    if not csv_path.exists():
        print(f"Validation CSV not found: {csv_path}", file=sys.stderr)
        return 1

    results = read_results(csv_path)
    if not results:
        print("No rows to apply (missing email or unparsable data).", file=sys.stderr)
        return 1

    validated_at = datetime.now(timezone.utc).isoformat()
    imported_at = datetime.now(timezone.utc).isoformat()

    backup_path = None
    if not args.no_backup:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.backup_before_mv_{timestamp}")
        shutil.copy2(db_path, backup_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        added_columns = ensure_columns(cursor)
        affected, n_read = upsert_mv_results(
            cursor,
            results,
            validated_at=validated_at,
            source_batch=args.source_batch,
            imported_at=imported_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "csv": str(csv_path),
        "rows_read": n_read,
        "sqlite_changes_reported": affected,
        "columns_added_this_run": added_columns,
        "mv_validated_at": validated_at,
        "source_batch_new_rows": args.source_batch,
        "backup_path": str(backup_path) if backup_path else None,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
