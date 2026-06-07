"""
Local SQLite registry for pipeline CSV files and unprocessed validation rows.

Prevents re-billing the same extract batch under a new validation output file.
Tracks partial runs so resume continues the same output path.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cycle_registry import STATE_DIR

PIPELINE_DB = STATE_DIR / "pipeline_registry.db"

DDL = """
CREATE TABLE IF NOT EXISTS processed_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL UNIQUE,
    file_kind TEXT NOT NULL,
    cycle_number INTEGER,
    extraction_cycle INTEGER,
    validation_cycle INTEGER,
    status TEXT NOT NULL,
    rows_total INTEGER DEFAULT 0,
    rows_processed INTEGER DEFAULT 0,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS validation_unprocessed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    extraction_file TEXT NOT NULL,
    validation_file TEXT,
    validation_cycle INTEGER,
    employee_key TEXT,
    company_name TEXT,
    company_domain TEXT,
    email_format TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(email, extraction_file)
);

CREATE INDEX IF NOT EXISTS idx_processed_files_kind_status
    ON processed_files(file_kind, status);
CREATE INDEX IF NOT EXISTS idx_unprocessed_open
    ON validation_unprocessed(resolved_at) WHERE resolved_at IS NULL;
"""


class FileRegistryError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_path(file_path: str) -> str:
    return str(file_path).replace("\\", "/")


def _file_name(file_path: str) -> str:
    return Path(file_path).name


def connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PIPELINE_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def ensure_registry_db() -> None:
    conn = connect()
    conn.close()


def get_file_record(file_path: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM processed_files WHERE file_path = ?",
            (_norm_path(file_path),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def register_processed_file(
    file_path: str,
    *,
    file_kind: str,
    cycle_number: int | None = None,
    extraction_cycle: int | None = None,
    validation_cycle: int | None = None,
    status: str = "registered",
    rows_total: int = 0,
    rows_processed: int = 0,
    notes: str = "",
) -> None:
    """Register a new CSV. Raises if file_path or file_name already exists."""
    path = _norm_path(file_path)
    name = _file_name(path)
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT file_path, status FROM processed_files WHERE file_path = ? OR file_name = ?",
            (path, name),
        ).fetchone()
        if existing:
            raise FileRegistryError(
                f"File already registered: {existing['file_path']} (status={existing['status']})"
            )
        now = _now()
        conn.execute(
            """
            INSERT INTO processed_files (
                file_path, file_name, file_kind, cycle_number,
                extraction_cycle, validation_cycle, status,
                rows_total, rows_processed, registered_at, updated_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                name,
                file_kind,
                cycle_number,
                extraction_cycle,
                validation_cycle,
                status,
                rows_total,
                rows_processed,
                now,
                now,
                notes,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_processed_file(
    file_path: str,
    *,
    status: str | None = None,
    rows_total: int | None = None,
    rows_processed: int | None = None,
    notes: str | None = None,
) -> None:
    path = _norm_path(file_path)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM processed_files WHERE file_path = ?", (path,)
        ).fetchone()
        if not row:
            raise FileRegistryError(f"File not in registry: {path}")
        sets = ["updated_at = ?"]
        vals: list = [_now()]
        if status is not None:
            sets.append("status = ?")
            vals.append(status)
        if rows_total is not None:
            sets.append("rows_total = ?")
            vals.append(rows_total)
        if rows_processed is not None:
            sets.append("rows_processed = ?")
            vals.append(rows_processed)
        if notes is not None:
            sets.append("notes = ?")
            vals.append(notes)
        vals.append(path)
        conn.execute(
            f"UPDATE processed_files SET {', '.join(sets)} WHERE file_path = ?",
            vals,
        )
        conn.commit()
    finally:
        conn.close()


def upsert_processed_file(
    file_path: str,
    *,
    file_kind: str,
    cycle_number: int | None = None,
    extraction_cycle: int | None = None,
    validation_cycle: int | None = None,
    status: str = "registered",
    rows_total: int = 0,
    rows_processed: int = 0,
    notes: str = "",
) -> None:
    if get_file_record(file_path):
        update_processed_file(
            file_path,
            status=status,
            rows_total=rows_total,
            rows_processed=rows_processed,
            notes=notes,
        )
    else:
        register_processed_file(
            file_path,
            file_kind=file_kind,
            cycle_number=cycle_number,
            extraction_cycle=extraction_cycle,
            validation_cycle=validation_cycle,
            status=status,
            rows_total=rows_total,
            rows_processed=rows_processed,
            notes=notes,
        )


def assert_can_validate_extraction(extraction_file: str) -> None:
    """Block starting validation when extract batch is already finished in registry."""
    rec = get_file_record(extraction_file)
    if rec and rec.get("status") in ("db_updated", "validated"):
        raise FileRegistryError(
            f"Extraction already processed ({rec['status']}): {extraction_file}"
        )


def assert_new_output_filename(output_name: str) -> None:
    """Each validation output CSV must have a unique name not seen before."""
    name = _file_name(output_name)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT file_path, status FROM processed_files WHERE file_name = ?",
            (name,),
        ).fetchone()
        if row:
            raise FileRegistryError(
                f"Validation output filename already used: {name} "
                f"(status={row['status']})"
            )
    finally:
        conn.close()


def find_open_validation_output(extraction_file: str) -> dict | None:
    """Registry row for partial/running validation output on this extract."""
    path = _norm_path(extraction_file)
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT * FROM processed_files
            WHERE file_kind = 'validation_output'
              AND notes LIKE ?
              AND status IN ('running', 'partial')
            ORDER BY id DESC LIMIT 1
            """,
            (f"%extract={path}%",),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_unprocessed_rows(
    rows: list[dict],
    merged: dict,
    *,
    extraction_file: str,
    validation_file: str,
    validation_cycle: int,
    reason: str,
) -> int:
    """Persist emails from extract CSV that have no MV result yet."""
    done = {k.lower() for k in merged}
    path = _norm_path(extraction_file)
    now = _now()
    conn = connect()
    n = 0
    try:
        for row in rows:
            email = (
                row.get("_email_to_validate")
                or row.get("email")
                or ""
            ).strip()
            if not email or "@" not in email:
                continue
            if email.lower() in done:
                continue
            conn.execute(
                """
                INSERT INTO validation_unprocessed (
                    email, extraction_file, validation_file, validation_cycle,
                    employee_key, company_name, company_domain, email_format,
                    reason, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(email, extraction_file) DO UPDATE SET
                    validation_file = excluded.validation_file,
                    validation_cycle = excluded.validation_cycle,
                    reason = excluded.reason,
                    created_at = excluded.created_at,
                    resolved_at = NULL
                """,
                (
                    email.lower(),
                    path,
                    _norm_path(validation_file),
                    validation_cycle,
                    (row.get("employee_key") or "").strip(),
                    (row.get("company_name") or "").strip(),
                    (row.get("company_domain") or "").strip(),
                    (row.get("email_format") or "").strip(),
                    reason,
                    now,
                ),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def resolve_unprocessed_for_extraction(extraction_file: str) -> int:
    path = _norm_path(extraction_file)
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE validation_unprocessed
            SET resolved_at = ?
            WHERE extraction_file = ? AND resolved_at IS NULL
            """,
            (_now(), path),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def count_open_unprocessed() -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT count(*) FROM validation_unprocessed WHERE resolved_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def log_unprocessed_summary(logger) -> None:
    n = count_open_unprocessed()
    if n:
        logger.info(
            "  Unprocessed validation backlog: %s email(s) in validation_unprocessed "
            "(table: %s)",
            n,
            PIPELINE_DB,
        )


def mark_db_updated(extraction_file: str, validation_file: str) -> None:
    """After update_db_cycle: close extract + validation files; clear unprocessed."""
    upsert_processed_file(
        extraction_file,
        file_kind="extract",
        status="db_updated",
        notes="db_updated",
    )
    upsert_processed_file(
        validation_file,
        file_kind="validation_output",
        status="db_updated",
        notes=f"db_updated;extract={_norm_path(extraction_file)}",
    )
    resolve_unprocessed_for_extraction(extraction_file)
