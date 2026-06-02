#!/usr/bin/env python3
"""
Ontario Courts Monitor
Checks the Ontario courts portal for new civil cases involving tracked companies
and emails a daily digest.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import tomllib


# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CACHE_FILE = SCRIPT_DIR / "cache.json"
CONFIG_FILE = SCRIPT_DIR / "config.toml"
COMPANIES_FILE = SCRIPT_DIR / "companies.toml"

API_BASE = "https://api1.courts.ontario.ca"
PORTAL_BASE = "https://courts.ontario.ca/portal"

# Search type codes
SEARCH_TYPES = {
    "contains": "300054",
    "exact":    "300012",
}

API_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://courts.ontario.ca",
    "Referer": "https://courts.ontario.ca/",
    "User-Agent": "Mozilla/5.0 (compatible; courts-monitor/1.0)",
}


# ── Config loading ────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)

def load_companies():
    with open(COMPANIES_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("companies", [])


# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ── API ───────────────────────────────────────────────────────────────────────

def search_parties(search_term, search_type_code, court_id):
    """
    Search for a party name in the Ontario courts index.
    Returns a list of result dicts (one per party-in-case row).
    Handles pagination automatically.
    """
    results = []
    page = 0
    page_size = 25

    while True:
        params = {
            "partyHeader.partyActorInstance.displayName": search_term,
            "partyHeader.partyActorInstance.displayNameSearchType": search_type_code,
            "caseHeader.closedFlag": "false",
            "caseHeader.courtID": court_id,
            "page": page,
            "size": page_size,
        }
        resp = requests.get(
            f"{API_BASE}/courts/cms/parties",
            params=params,
            headers=API_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("_embedded", {}).get("results", [])
        results.extend(batch)

        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        total_elements = page_info.get("totalElements", 0)

        # Warn if we're hitting the API cap (10,000 results)
        if total_elements >= 10000:
            print(f"  WARNING: search '{search_term}' hit the 10,000 result cap — "
                  f"results may be incomplete. Consider using 'exact' search type.",
                  file=sys.stderr)

        if page + 1 >= total_pages:
            break
        page += 1

    return results


def case_url(court_id, case_uuid):
    return f"{PORTAL_BASE}/court/{court_id}/case/{case_uuid}"


def parse_filed_date(raw):
    """Return a friendly date string from ISO timestamp, or raw if unparseable."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except Exception:
        return raw


# ── Core logic ────────────────────────────────────────────────────────────────

def check_companies(companies, court_id, cache):
    """
    For each company, run all configured searches and return any cases
    not previously seen. Updates cache in place.
    Returns (findings list, errors list).
    """
    findings = []
    errors = []

    for company in companies:
        name = company["name"]
        searches = company.get("searches", [])

        if not searches:
            errors.append(f"{name}: no searches configured")
            continue

        # Collect all case UUIDs across all searches for this company
        all_cases = {}  # uuid -> case_info dict

        for search in searches:
            term = search.get("term", "")
            stype = search.get("type", "contains")
            type_code = SEARCH_TYPES.get(stype, SEARCH_TYPES["contains"])

            if not term:
                errors.append(f"{name}: empty search term, skipping")
                continue

            try:
                rows = search_parties(term, type_code, court_id)
            except Exception as e:
                errors.append(f"{name} (search '{term}'): {e}")
                continue

            for row in rows:
                case = row.get("caseHeader", {})
                uuid = case.get("caseInstanceUUID")
                if not uuid:
                    continue

                if uuid not in all_cases:
                    all_cases[uuid] = {
                        "case_uuid": uuid,
                        "case_number": case.get("caseNumber", ""),
                        "case_title": case.get("caseTitle", ""),
                        "filed_date": case.get("filedDate", ""),
                        "case_category": case.get("caseCategory", ""),
                        "court_abbreviation": case.get("courtAbbreviation", ""),
                        "parties": [],
                    }

                party = row.get("partyHeader", {})
                party_name = party.get("partyActorInstance", {}).get("displayName", "")
                party_role = party.get("partySubType", "")
                entry = f"{party_name} ({party_role})"
                if entry not in all_cases[uuid]["parties"]:
                    all_cases[uuid]["parties"].append(entry)

        # Compare against cache
        cache_key = f"company:{name}"
        known_uuids = set(cache.get(cache_key, []))
        new_uuids = set(all_cases.keys()) - known_uuids

        for uuid in new_uuids:
            case_info = all_cases[uuid]
            findings.append({
                "company_name": name,
                "court_id": court_id,
                **case_info,
            })

        # Update cache with all currently active UUIDs
        cache[cache_key] = list(all_cases.keys())

    return findings, errors


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_body(findings, errors, run_time):
    """Build plain-text and HTML versions of the digest."""

    date_str = run_time.strftime("%A, %B %d, %Y")

    if not findings and not errors:
        subject_suffix = "Nothing new"
        plain = f"Ontario Courts Monitor — {date_str}\n\nNo new cases found for any tracked company.\n"
        html = f"""
        <p><strong>Ontario Courts Monitor — {date_str}</strong></p>
        <p>No new cases found for any tracked company.</p>
        """
    else:
        subject_suffix = f"{len(findings)} new case(s)" if findings else "Errors only"
        lines_plain = [f"Ontario Courts Monitor — {date_str}", ""]
        html_parts = [f"<p><strong>Ontario Courts Monitor — {date_str}</strong></p>"]

        if findings:
            lines_plain.append(f"{len(findings)} new case(s) found:\n")
            html_parts.append(f"<p>{len(findings)} new case(s) found:</p>")

            # Group by company
            by_company = {}
            for f in findings:
                by_company.setdefault(f["company_name"], []).append(f)

            for company, cases in by_company.items():
                n = len(cases)
                lines_plain.append(f"── {company} ({'1 case' if n == 1 else f'{n} cases'}) ──")
                html_parts.append(f"<h3 style='margin-bottom:4px'>{company}</h3>")

                for c in cases:
                    url = case_url(c["court_id"], c["case_uuid"])
                    filed = parse_filed_date(c["filed_date"])
                    parties_str = " | ".join(c["parties"])

                    lines_plain += [
                        f"  Case:    {c['case_number']}",
                        f"  Title:   {c['case_title']}",
                        f"  Filed:   {filed}",
                        f"  Type:    {c['court_abbreviation']}",
                        f"  Parties: {parties_str}",
                        f"  Link:    {url}",
                        "",
                    ]
                    html_parts.append(f"""
                    <div style='margin-bottom:16px; padding:12px; border-left:3px solid #555; background:#f9f9f9'>
                      <div><strong>{c['case_title']}</strong></div>
                      <div style='color:#555; font-size:0.9em'>{c['case_number']} &nbsp;·&nbsp; Filed {filed} &nbsp;·&nbsp; {c['court_abbreviation']}</div>
                      <div style='margin-top:4px; font-size:0.9em'>{parties_str}</div>
                      <div style='margin-top:6px'><a href='{url}'>{url}</a></div>
                    </div>
                    """)

        if errors:
            lines_plain += ["", "── Errors ──"]
            html_parts.append("<h3>Errors</h3><ul>")
            for e in errors:
                lines_plain.append(f"  {e}")
                html_parts.append(f"<li>{e}</li>")
            html_parts.append("</ul>")

        plain = "\n".join(lines_plain)
        html = "\n".join(html_parts)

    return subject_suffix, plain, html


def send_email(config, subject_suffix, plain_body, html_body):
    cfg = config["email"]
    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("GMAIL_APP_PASSWORD")
    if not smtp_pass:
        raise ValueError("No SMTP password found. Set SMTP_PASS environment variable.")

    subject = f"{cfg.get('subject_prefix', '[Courts]')} {subject_suffix}"
    sender = cfg["smtp_user"]
    recipient = cfg["recipient"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, smtp_pass)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"Email sent: {subject}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    run_time = datetime.now(timezone.utc)

    try:
        config = load_config()
    except Exception as e:
        print(f"ERROR: Could not load config.toml: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        companies = load_companies()
    except Exception as e:
        print(f"ERROR: Could not load companies.toml: {e}", file=sys.stderr)
        sys.exit(1)

    if not companies:
        print("No companies configured. Add entries to companies.toml.")
        sys.exit(0)

    court_id = config.get("settings", {}).get(
        "court_id", "68f021c4-6a44-4735-9a76-5360b2e8af13"
    )

    cache = load_cache()
    findings, errors = check_companies(companies, court_id, cache)
    save_cache(cache)

    subject_suffix, plain_body, html_body = build_email_body(findings, errors, run_time)

    print(f"Run complete: {len(findings)} new case(s), {len(errors)} error(s)")
    if findings:
        for f in findings:
            print(f"  NEW: {f['company_name']} — {f['case_number']} — {f['case_title']}")

    try:
        send_email(config, subject_suffix, plain_body, html_body)
    except Exception as e:
        print(f"ERROR sending email: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
