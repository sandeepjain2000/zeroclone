#!/usr/bin/env python3
"""
Extract up to 500 guessed staff (and optionally info@) addresses from the CVL
LinkedIn DB that do not yet appear in email_attempts. Writes a single-column CSV
for tools like validate_emails.py / ZeroBounce.

DB path: sibling folder ../CVL-ScraperLinkedIn_SendMails/data/db/linkedin_data.db
Output:  unsent_emails_500.csv (column: email)
"""

from __future__ import annotations

import csv
import re
import sqlite3
import unicodedata
from pathlib import Path

ZERONE = Path(__file__).resolve().parent
CVL_DB = ZERONE.parent / "CVL-ScraperLinkedIn_SendMails" / "data" / "db" / "linkedin_data.db"
OUT_CSV = ZERONE / "unsent_emails_500.csv"
LIMIT = 500

EMAIL_FORMATS = [
    "firstname.lastname",
    "firstname",
    "firstinitial.lastname",
    "firstname.lastinitial",
]

_DEGREE_RE_S = re.compile(r"^[🔹·\s]*(1st|2nd|3rd)", re.I)
_CONN_RE_S = re.compile(r"degree connection", re.I)
_FOLLOW_RE_S = re.compile(r"^[\d.,]+\s*[KkMm]?\s*followers", re.I)


def _clean_employee(raw_name: str, raw_title: str) -> tuple[str, str]:
    is_badge = bool(_DEGREE_RE_S.match((raw_name or "").strip()))
    blob_lines = [l.strip() for l in (raw_title or "").split("\n") if l.strip()]
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


def clean_domain(raw_domain: str) -> str:
    d = re.sub(r"^https?://(www\.)?", "", (raw_domain or ""))
    return d.split("/")[0].strip().lower()


def _to_ascii(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


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


def load_companies(conn: sqlite3.Connection) -> list[dict]:
    where = "WHERE (c.company_domain IS NOT NULL AND c.company_domain != '')"
    cur = conn.execute(
        f"""
        SELECT c.company_name, c.company_domain, c.linkedin_url
        FROM companies c
        {where}
        ORDER BY c.company_name
        """
    )
    companies = []
    for row in cur.fetchall():
        company = dict(row)
        emp_cur = conn.execute(
            """
            SELECT employee_name, job_title FROM employees
            WHERE (company_name = ? OR company_linkedin_url = ?)
              AND employee_name IS NOT NULL
              AND length(trim(employee_name)) > 3
              AND employee_name NOT LIKE '%·%'
              AND employee_name NOT LIKE '% 2nd%'
              AND employee_name NOT LIKE '% 3rd%'
              AND instr(trim(employee_name), ' ') > 0
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
            LIMIT 5
            """,
            (company["company_name"], company.get("linkedin_url", "")),
        )
        employees = []
        for emp in emp_cur.fetchall():
            clean_name, clean_title = _clean_employee(
                emp["employee_name"] or "", emp["job_title"] or ""
            )
            name_parts = clean_name.strip().split()
            if len(name_parts) >= 2:
                employees.append(
                    {
                        "first_name": name_parts[0],
                        "last_name": " ".join(name_parts[1:]),
                        "full_name": clean_name,
                        "job_title": clean_title,
                    }
                )
        company["employees"] = employees
        companies.append(company)
    return companies


def main() -> None:
    if not CVL_DB.is_file():
        raise SystemExit(f"Database not found: {CVL_DB}")

    conn = sqlite3.connect(CVL_DB)
    conn.row_factory = sqlite3.Row
    attempted = {
        r[0]
        for r in conn.execute(
            "SELECT lower(trim(email_address)) FROM email_attempts WHERE email_address IS NOT NULL"
        )
    }
    companies = load_companies(conn)
    conn.close()

    collected: list[str] = []
    seen: set[str] = set()

    def add_email(addr: str) -> bool:
        nonlocal collected
        a = addr.strip()
        if not a or "@" not in a:
            return False
        key = a.lower()
        if key in attempted or key in seen:
            return False
        seen.add(key)
        collected.append(a)
        return len(collected) >= LIMIT

    for company in companies:
        if len(collected) >= LIMIT:
            break
        domain = clean_domain(company["company_domain"] or "")
        if not domain:
            continue
        for emp in company["employees"]:
            if len(collected) >= LIMIT:
                break
            first = emp["first_name"]
            last = emp["last_name"].split()[0] if emp["last_name"] else ""
            if not first.isalpha() or not last.isalpha():
                continue
            for fmt in EMAIL_FORMATS:
                if len(collected) >= LIMIT:
                    break
                candidate = build_email_address(first, last, domain, fmt)
                if add_email(candidate):
                    break

    if len(collected) < LIMIT:
        conn = sqlite3.connect(CVL_DB)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """
            SELECT lower(trim(replace(replace(company_domain,'https://',''),'http://',''))) AS d
            FROM companies
            WHERE company_domain IS NOT NULL AND trim(company_domain) != ''
            ORDER BY company_name
            """
        ):
            if len(collected) >= LIMIT:
                break
            dom = clean_domain(row["d"] or "")
            if not dom:
                continue
            info = f"info@{dom}"
            add_email(info)
        conn.close()

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email"])
        for e in collected[:LIMIT]:
            w.writerow([e])

    print(f"Wrote {len(collected[:LIMIT])} rows to {OUT_CSV}")
    if len(collected) < LIMIT:
        print(f"Warning: only {len(collected)} unsent addresses found (requested {LIMIT}).")


if __name__ == "__main__":
    main()
