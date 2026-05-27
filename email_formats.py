"""Shared email format definitions and address builders (aligned with CVL)."""

from __future__ import annotations

import re
import unicodedata

# Validation / extraction cascade order (send_linkedin_campaigns_params.py EMAIL_FORMATS).
FORMAT_ORDER = [
    "firstname.lastname",       # 1 — tried first
    "firstname",                # 2 — when #1 is INVALID
    "firstinitial.lastname",    # 3 — when #1 and #2 are INVALID
    "firstname.lastinitial",    # 4 — when #1, #2, #3 are INVALID
]

EMAIL_FORMATS = FORMAT_ORDER  # alias for CVL parity


def _to_ascii(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def clean_domain(raw_domain: str) -> str:
    d = re.sub(r"^https?://(www\.)?", "", (raw_domain or ""))
    return d.split("/")[0].strip().lower()


def build_email_address(first: str, last: str, domain: str, fmt: str) -> str:
    f = _to_ascii(first.lower().strip())
    l = _to_ascii(last.lower().strip().split()[0])
    if fmt == "firstname.lastname":
        return f"{f}.{l}@{domain}"
    if fmt == "firstname":
        return f"{f}@{domain}"
    if fmt == "firstinitial.lastname":
        return f"{f[0]}.{l}@{domain}"
    if fmt == "firstname.lastinitial":
        return f"{f}.{l[0]}@{domain}"
    return f"{f}.{l}@{domain}"


def employee_key(company_name: str, full_name: str) -> str:
    return f"{(company_name or '').strip().lower()}|{(full_name or '').strip().lower()}"


def format_status_column(fmt: str) -> str:
    return f"format_{fmt.replace('.', '_')}_status"


def format_email_column(fmt: str) -> str:
    return f"format_{fmt.replace('.', '_')}_email"


def format_company_flag_column(fmt: str) -> str:
    """Per-company probe outcome: '' | works | failed."""
    return f"company_format_{fmt.replace('.', '_')}"


def previous_format(email_format: str) -> str | None:
    if email_format not in FORMAT_ORDER:
        raise ValueError(f"Unknown format: {email_format}. Use one of {FORMAT_ORDER}")
    idx = FORMAT_ORDER.index(email_format)
    if idx == 0:
        return None
    return FORMAT_ORDER[idx - 1]


def is_mv_valid(status: str) -> bool:
    return (status or "").strip().lower() in {"ok", "valid", "deliverable"}


def is_mv_invalid(status: str) -> bool:
    """True when a validation result exists and is not valid (cascade to next format)."""
    s = (status or "").strip()
    if not s:
        return False
    return not is_mv_valid(s)


def employee_eligible_for_format(state: dict, email_format: str) -> bool:
    """
    True if this employee should get a candidate for *email_format*.

    - Format 1: no prior format attempts, no resolved valid email.
    - Format N>1: every earlier format has an INVALID result.
    """
    if state.get("resolved_valid_email"):
        return False

    if email_format not in FORMAT_ORDER:
        return False

    idx = FORMAT_ORDER.index(email_format)
    for earlier in FORMAT_ORDER[:idx]:
        earlier_status = (state.get(format_status_column(earlier)) or "").strip()
        if not earlier_status:
            return False
        if not is_mv_invalid(earlier_status):
            return False

    # Format 1: no attempts on any format yet
    if idx == 0:
        for fmt in FORMAT_ORDER:
            if (state.get(format_status_column(fmt)) or "").strip():
                return False
        return True

    return True


def resolve_cycle_format(employee_state: dict[str, dict]) -> str | None:
    """
    Pick the format stage for the next extraction cycle.

    Always prefers an earlier format if any employee still needs it.
    """
    for fmt in FORMAT_ORDER:
        for state in employee_state.values():
            if employee_eligible_for_format(state, fmt):
                return fmt
    return None
