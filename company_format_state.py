"""
Company-level email format discovery (probe-then-expand).

Per company and format:
  - untested: not probed yet
  - works:    at least one of 2 probe emails validated ok/valid
  - failed:   both probes returned non-valid (or only invalid results)

When a format is *works*, extraction uses mode=expand for all employees at that company.
When both probes fail, the next format in FORMAT_ORDER is probed (2 new employees).
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from cycle_registry import STATE_DIR
from email_formats import (
    FORMAT_ORDER,
    format_company_flag_column,
    format_status_column,
    is_mv_valid,
)

COMPANY_FORMAT_STATE_CSV = STATE_DIR / "company_format_state.csv"

FORMAT_UNTESTED = ""
FORMAT_WORKS = "works"
FORMAT_FAILED = "failed"

PROBE_SAMPLE_SIZE = 2

COMPANY_STATE_FIELDS = [
    "company_key",
    "company_name",
    "company_domain",
    "resolved_format",
    "last_updated",
] + [format_company_flag_column(f) for f in FORMAT_ORDER]


def company_key(company_name: str, company_domain: str = "") -> str:
    name = (company_name or "").strip().lower()
    if name:
        return name
    return (company_domain or "").strip().lower()


def _blank_company_row(ckey: str, cname: str, domain: str) -> dict:
    row = {
        "company_key": ckey,
        "company_name": cname,
        "company_domain": domain,
        "resolved_format": "",
        "last_updated": "",
    }
    for fmt in FORMAT_ORDER:
        row[format_company_flag_column(fmt)] = FORMAT_UNTESTED
    return row


def load_company_format_state() -> dict[str, dict]:
    if not COMPANY_FORMAT_STATE_CSV.exists():
        return {}
    out: dict[str, dict] = {}
    with COMPANY_FORMAT_STATE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("company_key") or "").strip()
            if key:
                out[key] = row
    return out


def save_company_format_state(state: dict[str, dict]) -> None:
    COMPANY_FORMAT_STATE_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(COMPANY_STATE_FIELDS)
    with COMPANY_FORMAT_STATE_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in sorted(state.values(), key=lambda r: r.get("company_key", "")):
            w.writerow(row)


def get_company_state(
    state: dict[str, dict], company_name: str, company_domain: str
) -> dict:
    ckey = company_key(company_name, company_domain)
    if ckey not in state:
        state[ckey] = _blank_company_row(ckey, company_name, company_domain)
    return state[ckey]


def get_format_flag(cstate: dict, fmt: str) -> str:
    return (cstate.get(format_company_flag_column(fmt)) or "").strip().lower()


def set_format_flag(cstate: dict, fmt: str, flag: str) -> None:
    cstate[format_company_flag_column(fmt)] = flag
    cstate["last_updated"] = datetime.now(timezone.utc).isoformat()


def next_format_to_probe(cstate: dict) -> str | None:
    """First format in cascade not yet marked works or failed."""
    for fmt in FORMAT_ORDER:
        flag = get_format_flag(cstate, fmt)
        if flag not in (FORMAT_WORKS, FORMAT_FAILED):
            return fmt
    return None


def resolved_format(cstate: dict) -> str:
    return (cstate.get("resolved_format") or "").strip()


def decide_extraction_plan(cstate: dict) -> tuple[str, str] | None:
    """
    Returns (email_format, mode) where mode is 'probe' or 'expand'.
    None if this company is exhausted (all formats failed, no resolution).
    """
    resolved = resolved_format(cstate)
    if resolved:
        if get_format_flag(cstate, resolved) == FORMAT_WORKS:
            return resolved, "expand"
        return None

    for fmt in FORMAT_ORDER:
        flag = get_format_flag(cstate, fmt)
        if flag == FORMAT_WORKS:
            cstate["resolved_format"] = fmt
            return fmt, "expand"
        if flag == FORMAT_FAILED:
            continue
        return fmt, "probe"
    return None


def bootstrap_from_employee_state(
    company_state: dict[str, dict],
    employee_state: dict[str, dict],
) -> int:
    """
    Infer company format flags from existing per-employee validation CSV.
    If any employee has a valid result for format F, mark F as works for that company.
    """
    from collections import defaultdict

    by_company: dict[str, list[dict]] = defaultdict(list)
    for row in employee_state.values():
        cname = (row.get("company_name") or "").strip()
        if cname:
            by_company[cname].append(row)

    updated = 0
    for cname, rows in by_company.items():
        domain = ""
        for r in rows:
            if r.get("company_domain"):
                domain = r["company_domain"]
                break
        cstate = get_company_state(company_state, cname, domain)
        for fmt in FORMAT_ORDER:
            col = format_company_flag_column(fmt)
            if get_format_flag(cstate, fmt) in (FORMAT_WORKS, FORMAT_FAILED):
                continue
            for r in rows:
                st = (r.get(format_status_column(fmt)) or "").strip()
                if is_mv_valid(st):
                    set_format_flag(cstate, fmt, FORMAT_WORKS)
                    if not resolved_format(cstate):
                        cstate["resolved_format"] = fmt
                    updated += 1
                    break
    return updated


def apply_probe_validation_results(
    company_state: dict[str, dict],
    company_name: str,
    company_domain: str,
    email_format: str,
    validated_rows: list[dict],
) -> str:
    """
    Update company flags from probe validation rows.
    Returns summary token: works | failed | pending
    """
    cstate = get_company_state(company_state, company_name, company_domain)
    rows = [r for r in validated_rows if (r.get("validation_status") or "").strip()]
    if not rows:
        return "pending"

    if any(is_mv_valid(r.get("validation_status", "")) for r in rows):
        set_format_flag(cstate, email_format, FORMAT_WORKS)
        if not resolved_format(cstate):
            cstate["resolved_format"] = email_format
        return "works"

    # Mark failed when every returned probe has a definitive non-valid result
    if len(rows) >= 1 and all(
        not is_mv_valid(r.get("validation_status", "")) for r in rows
    ):
        set_format_flag(cstate, email_format, FORMAT_FAILED)
        return "failed"
    return "pending"
