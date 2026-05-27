"""
Cycle file naming, manifest CSVs, and execution state for the email pipeline.

Manifests (under cycles/manifests/):
  extraction_manifest.csv — each extract run
  validation_manifest.csv — each validation run (supports partial)
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_cvl_root() -> Path:
    """
    Sibling folder next to zeroclone: CVL-ScraperLinkedIn_SendMails or CVL.
    Picks the first that has the DB or apply_validation_views.py.
    """
    parent = SCRIPT_DIR.parent
    for name in ("CVL-ScraperLinkedIn_SendMails", "CVL"):
        root = parent / name
        db = root / "data" / "db" / "linkedin_data.db"
        views = root / "scripts" / "apply_validation_views.py"
        if db.is_file() or views.is_file():
            return root
    return parent / "CVL"


def linkedin_db_path() -> Path:
    return resolve_cvl_root() / "data" / "db" / "linkedin_data.db"


def apply_validation_views_script() -> Path:
    return resolve_cvl_root() / "scripts" / "apply_validation_views.py"


CYCLES_DIR = SCRIPT_DIR / "cycles"
DATA_DIR = CYCLES_DIR / "data"
MANIFESTS_DIR = CYCLES_DIR / "manifests"
STATE_DIR = CYCLES_DIR / "state"

EXTRACTION_MANIFEST = MANIFESTS_DIR / "extraction_manifest.csv"
VALIDATION_MANIFEST = MANIFESTS_DIR / "validation_manifest.csv"
EMPLOYEE_STATE_CSV = STATE_DIR / "employee_email_state.csv"

EXTRACTION_FIELDS = [
    "cycle_number",
    "created_at",
    "email_format",
    "extraction_file",
    "row_count",
    "status",
    "notes",
]

VALIDATION_FIELDS = [
    "cycle_number",
    "extraction_cycle_number",
    "created_at",
    "updated_at",
    "email_format",
    "extraction_file",
    "validation_file",
    "rows_total",
    "rows_processed",
    "rows_ok",
    "rows_invalid",
    "status",
    "error_message",
    "notes",
]

EMPLOYEE_STATE_FIELDS = [
    "employee_key",
    "employee_id",
    "company_name",
    "full_name",
    "first_name",
    "last_name",
    "company_domain",
    "email_format",
    "email",
    "validation_status",
    "validation_reason",
    "resolved_valid_email",
    "last_updated",
]


def ensure_dirs() -> None:
    for path in (DATA_DIR, MANIFESTS_DIR, STATE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def timestamp_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def next_cycle_number(manifest_path: Path) -> int:
    ensure_dirs()
    if not manifest_path.exists():
        return 1
    max_n = 0
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                max_n = max(max_n, int(row.get("cycle_number") or 0))
            except ValueError:
                continue
    return max_n + 1


def extraction_filename(email_format: str, cycle_number: int, tag: str | None = None) -> str:
    fmt_slug = email_format.replace(".", "_")
    tag = tag or timestamp_tag()
    return f"extract_{fmt_slug}_{tag}_c{cycle_number:04d}.csv"


def validation_filename(email_format: str, cycle_number: int, tag: str | None = None) -> str:
    fmt_slug = email_format.replace(".", "_")
    tag = tag or timestamp_tag()
    return f"validated_{fmt_slug}_{tag}_c{cycle_number:04d}.csv"


def read_manifest(path: Path, fieldnames: list[str]) -> list[dict]:
    ensure_dirs()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_manifest(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    ensure_dirs()
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_manifest_row(path: Path, fieldnames: list[str], row: dict) -> None:
    rows = read_manifest(path, fieldnames)
    rows.append(row)
    write_manifest(path, fieldnames, rows)


def update_manifest_row(
    path: Path,
    fieldnames: list[str],
    cycle_number: int,
    updates: dict,
) -> bool:
    rows = read_manifest(path, fieldnames)
    found = False
    for row in rows:
        if int(row.get("cycle_number") or 0) == cycle_number:
            row.update(updates)
            found = True
            break
    if found:
        write_manifest(path, fieldnames, rows)
    return found


def relative_data_path(filename: str) -> str:
    return str(Path("cycles") / "data" / filename)


def resolve_data_path(filename_or_path: str) -> Path:
    p = Path(filename_or_path)
    if p.is_absolute():
        return p
    if p.parts[:2] == ("cycles", "data"):
        return SCRIPT_DIR / p
    return DATA_DIR / p.name if p.parent == Path(".") else SCRIPT_DIR / p
