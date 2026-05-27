#!/usr/bin/env python3
"""
Extract email candidates using company-level format discovery.

Strategy (saves Apify credits):
  1. For each company, probe the current format with 2 employee emails.
  2. If either probe is valid → mark format *works* for that company → later
     cycles extract/validate remaining employees (expand mode).
  3. If both probes fail → mark format *failed* → try next format (2 new probes).

Global --limit caps total emails per run (default 500).

Usage:
  python extract_cycle.py
  python extract_cycle.py --limit 500
  python extract_cycle.py --probe-size 2
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline_logging import get_logger, setup_pipeline_logging
from cycle_registry import (
    DATA_DIR,
    EMPLOYEE_STATE_CSV,
    EXTRACTION_FIELDS,
    EXTRACTION_MANIFEST,
    append_manifest_row,
    ensure_dirs,
    extraction_filename,
    linkedin_db_path,
    next_cycle_number,
    read_manifest,
    relative_data_path,
    timestamp_tag,
)
from company_format_state import (
    PROBE_SAMPLE_SIZE,
    bootstrap_from_employee_state,
    decide_extraction_plan,
    get_company_state,
    load_company_format_state,
    save_company_format_state,
)
from email_formats import (
    build_email_address,
    clean_domain,
    employee_key,
    format_status_column,
)

_DEGREE_RE_S = re.compile(r"^[🔹·\s]*(1st|2nd|3rd)", re.I)
_CONN_RE_S = re.compile(r"degree connection", re.I)
_FOLLOW_RE_S = re.compile(r"^[\d.,]+\s*[KkMm]?\s*followers", re.I)

DEFAULT_DB = linkedin_db_path()
DEFAULT_LIMIT = 500

logger = get_logger("extract_cycle")

EXTRACT_COLUMNS = [
    "email",
    "email_format",
    "extraction_mode",
    "employee_key",
    "employee_id",
    "company_name",
    "full_name",
    "first_name",
    "last_name",
    "company_domain",
    "job_title",
]

_EMPLOYEE_ORDER_SQL = """
    ORDER BY
        CASE WHEN lower(job_title) LIKE '%ceo%'
                  OR lower(job_title) LIKE '%chief%'
                  OR lower(job_title) LIKE '%managing director%'
                  OR lower(job_title) LIKE '%geschäftsführer%'
                  OR lower(job_title) LIKE '%founder%'
                  OR lower(job_title) LIKE '%owner%'
                  OR lower(job_title) LIKE '%director%'
             THEN 0 ELSE 1 END,
        employee_name
"""


def _clean_employee(raw_name: str, raw_title: str) -> tuple[str, str]:
    is_badge = bool(_DEGREE_RE_S.match((raw_name or "").strip()))
    blob_lines = [line.strip() for line in (raw_title or "").split("\n") if line.strip()]
    if is_badge:
        name, title, after = "", "", False
        for line in blob_lines:
            if not name:
                if not _DEGREE_RE_S.match(line):
                    name = line
            elif _CONN_RE_S.search(line) or _DEGREE_RE_S.match(line):
                after = True
            elif after:
                if not _FOLLOW_RE_S.match(line):
                    title = line
                    break
        return name, title
    name, title, after = (raw_name or "").strip(), "", False
    for line in blob_lines:
        if _CONN_RE_S.search(line) or _DEGREE_RE_S.match(line):
            after = True
        elif after:
            if not _FOLLOW_RE_S.match(line):
                title = line
                break
    return name, title or (blob_lines[0] if blob_lines else "")


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


def load_mv_valid_emails(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        """
        SELECT lower(trim(email_address))
        FROM zerobounce_validation
        WHERE lower(trim(COALESCE(mv_status, zb_status, ''))) IN ('ok', 'valid', 'deliverable')
        """
    )
    return {r[0] for r in cur.fetchall() if r[0]}


def load_attempted_emails(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        """
        SELECT lower(trim(email_address))
        FROM email_attempts
        WHERE email_address IS NOT NULL AND trim(email_address) != ''
        """
    )
    return {r[0] for r in cur.fetchall() if r[0]}


def find_pending_extraction() -> dict | None:
    for row in read_manifest(EXTRACTION_MANIFEST, EXTRACTION_FIELDS):
        if (row.get("status") or "").strip() == "pending_validation":
            return row
    return None


def _parse_employee_row(emp_row: sqlite3.Row, company_name: str) -> dict | None:
    clean_name, clean_title = _clean_employee(
        emp_row["employee_name"] or "", emp_row["job_title"] or ""
    )
    parts = clean_name.strip().split()
    if len(parts) < 2:
        return None
    first = parts[0]
    last = " ".join(parts[1:])
    if not first.isalpha() or not last.split()[0].isalpha():
        return None
    return {
        "employee_id": emp_row["id"],
        "first_name": first,
        "last_name": last,
        "full_name": clean_name,
        "job_title": clean_title,
    }


def load_employees_for_company(
    conn: sqlite3.Connection,
    company_name: str,
    linkedin_url: str | None,
    *,
    limit: int | None,
) -> list[dict]:
    sql = f"""
        SELECT id, employee_name, job_title FROM employees
        WHERE (company_name = ? OR company_linkedin_url = ?)
          AND employee_name IS NOT NULL
          AND length(trim(employee_name)) > 3
          AND employee_name NOT LIKE '%·%'
          AND employee_name NOT LIKE '% 2nd%'
          AND employee_name NOT LIKE '% 3rd%'
          AND instr(trim(employee_name), ' ') > 0
        {_EMPLOYEE_ORDER_SQL}
    """
    params: list = [company_name, linkedin_url or ""]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    emp_cur = conn.execute(sql, params)
    out = []
    for emp in emp_cur.fetchall():
        parsed = _parse_employee_row(emp, company_name)
        if parsed:
            out.append(parsed)
    return out


def load_companies(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT c.company_name, c.company_domain, c.linkedin_url
        FROM companies c
        WHERE c.company_domain IS NOT NULL AND trim(c.company_domain) != ''
        ORDER BY c.company_name
        """
    )
    return [dict(row) for row in cur.fetchall()]


def employee_needs_validation(
    est: dict, email_format: str, email: str, attempted: set, mv_valid: set
) -> bool:
    el = email.lower()
    if el in attempted or el in mv_valid:
        return False
    if (est.get(format_status_column(email_format)) or "").strip():
        return False
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument(
        "--probe-size",
        type=int,
        default=PROBE_SAMPLE_SIZE,
        help="Probe emails per company per format (default: 2).",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--cycle-number", type=int, default=None)
    p.add_argument("--tag", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_pipeline_logging("extract_cycle")
    ensure_dirs()

    if not args.db.is_file():
        logger.error("Database not found: %s", args.db)
        return 1

    pending = find_pending_extraction()
    if pending:
        logger.error(
            "Extraction cycle %s (%s) is still pending_validation — run validate_cycle.py first.",
            pending.get("cycle_number"),
            pending.get("extraction_file"),
        )
        return 2

    company_state = load_company_format_state()
    employee_state = load_employee_state()
    if employee_state and len(company_state) < 50:
        n_boot = bootstrap_from_employee_state(company_state, employee_state)
        if n_boot:
            logger.info(
                "Bootstrapped company format flags from employee state (%s company×format updates).",
                n_boot,
            )
            save_company_format_state(company_state)

    tag = args.tag or timestamp_tag()
    cycle_number = args.cycle_number or next_cycle_number(EXTRACTION_MANIFEST)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    attempted = load_attempted_emails(conn)
    mv_valid = load_mv_valid_emails(conn)
    companies = load_companies(conn)

    collected: list[dict] = []
    seen_emails: set[str] = set()
    stats = {
        "probe_companies": 0,
        "expand_companies": 0,
        "probe_emails": 0,
        "expand_emails": 0,
        "skipped_companies": 0,
    }
    formats_in_batch: set[str] = set()

    def try_add(row: dict) -> bool:
        email = row["email"].strip().lower()
        if not email or "@" not in email:
            return False
        if email in attempted or email in mv_valid or email in seen_emails:
            return False
        seen_emails.add(email)
        collected.append(row)
        if row["extraction_mode"] == "probe":
            stats["probe_emails"] += 1
        else:
            stats["expand_emails"] += 1
        return len(collected) >= args.limit

    for company in companies:
        if len(collected) >= args.limit:
            break
        cname = company["company_name"] or ""
        domain = clean_domain(company["company_domain"] or "")
        if not domain:
            continue
        url = company.get("linkedin_url")

        cstate = get_company_state(company_state, cname, domain)
        plan = decide_extraction_plan(cstate)
        if not plan:
            stats["skipped_companies"] += 1
            continue

        email_format, mode = plan
        formats_in_batch.add(email_format)

        if mode == "probe":
            employees = load_employees_for_company(
                conn, cname, url, limit=args.probe_size
            )
            stats["probe_companies"] += 1
        else:
            employees = load_employees_for_company(conn, cname, url, limit=None)
            stats["expand_companies"] += 1

        probes_added = 0
        for emp in employees:
            if len(collected) >= args.limit:
                break
            if mode == "probe" and probes_added >= args.probe_size:
                break
            first = emp["first_name"]
            last = emp["last_name"].split()[0] if emp["last_name"] else ""
            fname = emp["full_name"]
            ekey = employee_key(cname, fname)
            est = employee_state.get(ekey, {})
            candidate = build_email_address(first, last, domain, email_format)
            if not employee_needs_validation(
                est, email_format, candidate, attempted, mv_valid
            ):
                continue
            row = {
                "email": candidate,
                "email_format": email_format,
                "extraction_mode": mode,
                "employee_key": ekey,
                "employee_id": str(emp["employee_id"]),
                "company_name": cname,
                "full_name": fname,
                "first_name": first,
                "last_name": last,
                "company_domain": domain,
                "job_title": emp.get("job_title", ""),
            }
            if try_add(row):
                if mode == "probe":
                    probes_added += 1

    conn.close()
    save_company_format_state(company_state)

    if not collected:
        logger.info(
            "No candidates extracted (companies skipped=%s). Pipeline may be complete.",
            stats["skipped_companies"],
        )
        return 0

    primary_format = sorted(formats_in_batch)[0] if len(formats_in_batch) == 1 else "mixed"
    out_name = extraction_filename(primary_format, cycle_number, tag)
    out_path = DATA_DIR / out_name

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EXTRACT_COLUMNS)
        w.writeheader()
        w.writerows(collected)

    notes = (
        f"probe_co={stats['probe_companies']} expand_co={stats['expand_companies']} "
        f"probe_em={stats['probe_emails']} expand_em={stats['expand_emails']}"
    )
    now = datetime.now(timezone.utc).isoformat()
    append_manifest_row(
        EXTRACTION_MANIFEST,
        EXTRACTION_FIELDS,
        {
            "cycle_number": cycle_number,
            "created_at": now,
            "email_format": primary_format,
            "extraction_file": relative_data_path(out_name),
            "row_count": len(collected),
            "status": "pending_validation",
            "notes": notes,
        },
    )

    logger.info("Cycle %s | company-probe pipeline", cycle_number)
    logger.info(
        "  Probe: %s companies, %s emails | Expand: %s companies, %s emails",
        stats["probe_companies"],
        stats["probe_emails"],
        stats["expand_companies"],
        stats["expand_emails"],
    )
    logger.info("  Skipped (all formats exhausted): %s companies", stats["skipped_companies"])
    logger.info("Wrote %s rows to %s", len(collected), out_path)
    if len(collected) < args.limit:
        logger.warning(
            "Only %s candidates extracted (limit %s).", len(collected), args.limit
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
