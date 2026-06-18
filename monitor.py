#!/usr/bin/env python3
"""
Ontario Courts Monitor
Checks the Ontario courts portal for new civil cases involving tracked companies
and emails a daily digest to each configured reporter.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import html as html_lib
import re
import unicodedata

import requests
import tomllib


# ── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CACHE_FILE = SCRIPT_DIR / "cache.json"
CONFIG_FILE = SCRIPT_DIR / "config.toml"

API_BASE = "https://api1.courts.ontario.ca"
PORTAL_BASE = "https://courts.ontario.ca/portal"

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

def load_reporter_files():
    """
    Find all *companies.toml files in the script directory.
    Each file represents one reporter's watchlist.
    Returns list of (path, data) tuples.
    """
    reporters = []
    for path in sorted(SCRIPT_DIR.glob("*companies.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        reporters.append((path, data))
    return reporters


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

def _normalize(text):
    """
    Fold a party name or search term to a canonical form for phrase matching:
    strip accents, uppercase, and collapse every run of non-alphanumeric
    characters to a single space. This makes hyphens, commas, periods and
    accented characters irrelevant to the comparison.

      "HYDRO-QUÉBEC"  -> "HYDRO QUEBEC"
      "Hydro Quebec"  -> "HYDRO QUEBEC"
      "9322-4558 QUEBEC INC." -> "9322 4558 QUEBEC INC"
    """
    if not text:
        return ""
    # Decompose accents and drop the combining marks.
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Collapse any non-alphanumeric run to a single space, uppercase, trim.
    return re.sub(r"[^A-Za-z0-9]+", " ", no_accents).upper().strip()


def search_parties(search_term, search_type_code, court_id):
    """
    Search for a party name in the Ontario courts index.
    Returns a list of result dicts. Handles pagination.
    Post-filters multi-word contains searches to require the full phrase.
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

        # The API splits the search term on any non-alphanumeric character
        # (spaces AND hyphens, etc.) and OR-matches the resulting words. So
        # "HYDRO-QUEBEC" matches any party containing "HYDRO" *or* "QUEBEC".
        # For any multi-word term we post-filter to require all the words to
        # appear together as a contiguous phrase in the party name.
        #
        # Matching is done on a normalized form (accents folded, punctuation
        # collapsed to single spaces) so "HYDRO-QUEBEC", "HYDRO QUEBEC" and
        # "HYDRO-QUÉBEC" are all treated as the same phrase.
        if search_type_code == SEARCH_TYPES["contains"]:
            phrase_norm = _normalize(search_term)
            # Only filter when the term is genuinely multi-word; a single
            # token like "TELESAT" needs no phrase check.
            if " " in phrase_norm:
                batch = [
                    r for r in batch
                    if phrase_norm in _normalize(
                        r.get("partyHeader", {})
                         .get("partyActorInstance", {})
                         .get("displayName", "")
                    )
                ]

        results.extend(batch)

        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        total_elements = page_info.get("totalElements", 0)

        if total_elements >= 10000:
            print(f"  WARNING: search '{search_term}' hit the 10,000 result cap. "
                  f"Consider switching to 'exact' search type.", file=sys.stderr)

        if page + 1 >= total_pages:
            break
        page += 1

    return results


def case_url(court_id, case_uuid):
    return f"{PORTAL_BASE}/court/{court_id}/case/{case_uuid}"


def parse_filed_date(raw):
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except Exception:
        return raw


# ── Core logic ────────────────────────────────────────────────────────────────

def check_companies(companies, court_id, cache, cache_prefix):
    """
    Run all searches for a reporter's company list.
    cache_prefix is used to namespace cache keys per reporter.
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

        all_cases = {}
        company_had_errors = False

        for search in searches:
            term = search.get("term", "").strip()
            stype = search.get("type", "contains")
            type_code = SEARCH_TYPES.get(stype, SEARCH_TYPES["contains"])

            if not term:
                continue

            try:
                rows = search_parties(term, type_code, court_id)
            except Exception as e:
                errors.append(f"{name} (search '{term}'): {e}")
                company_had_errors = True
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

        cache_key = f"{cache_prefix}:company:{name}"
        known_uuids = set(cache.get(cache_key, []))
        new_uuids = set(all_cases.keys()) - known_uuids

        for uuid in new_uuids:
            findings.append({
                "company_name": name,
                "court_id": court_id,
                **all_cases[uuid],
            })

        if company_had_errors:
            # A search failed, so all_cases may be incomplete. Merge new UUIDs
            # into the existing cache rather than overwriting it — otherwise
            # cases missing from this partial run would be re-reported as
            # "new" on the next successful run.
            cache[cache_key] = list(known_uuids | set(all_cases.keys()))
        else:
            # Full success: overwrite, which also lets closed cases age out.
            cache[cache_key] = list(all_cases.keys())

    return findings, errors


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_body(findings, errors, run_time, reporter_name):
    date_str = run_time.strftime("%A, %B %d, %Y")
    greeting = f"Hi {reporter_name.split()[0]}," if reporter_name else ""

    if not findings and not errors:
        subject_suffix = "Nothing new"
        plain = f"Ontario Courts Monitor — {date_str}\n\n{greeting}\n\nNo new cases found for any of your tracked companies.\n"
        html = f"<p>{greeting}</p><p><strong>Ontario Courts Monitor — {date_str}</strong></p><p>No new cases found for any of your tracked companies.</p>"
    else:
        subject_suffix = f"{len(findings)} new case(s)" if findings else "Errors only"
        lines_plain = [f"Ontario Courts Monitor — {date_str}", ""]
        if greeting:
            lines_plain = [greeting, ""] + lines_plain
        html_parts = []
        if greeting:
            html_parts.append(f"<p>{greeting}</p>")
        html_parts.append(f"<p><strong>Ontario Courts Monitor — {date_str}</strong></p>")

        if findings:
            lines_plain.append(f"{len(findings)} new case(s) found:\n")
            html_parts.append(f"<p>{len(findings)} new case(s) found:</p>")

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
                    title_esc = html_lib.escape(c["case_title"])
                    parties_esc = html_lib.escape(parties_str)

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
                      <div><strong>{title_esc}</strong></div>
                      <div style='color:#555; font-size:0.9em'>{c['case_number']} &nbsp;·&nbsp; Filed {filed} &nbsp;·&nbsp; {c['court_abbreviation']}</div>
                      <div style='margin-top:4px; font-size:0.9em'>{parties_esc}</div>
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


def send_email(smtp_cfg, smtp_pass, sender, recipient, subject, plain_body, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_cfg["smtp_host"], smtp_cfg["smtp_port"]) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, smtp_pass)
        server.sendmail(sender, recipient, msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    run_time = datetime.now(timezone.utc)

    try:
        config = load_config()
    except Exception as e:
        print(f"ERROR: Could not load config.toml: {e}", file=sys.stderr)
        sys.exit(1)

    smtp_cfg = config.get("email", {})
    sender = smtp_cfg.get("smtp_user", "")
    subject_prefix = smtp_cfg.get("subject_prefix", "[Courts]")
    court_id = config.get("settings", {}).get(
        "court_id", "68f021c4-6a44-4735-9a76-5360b2e8af13"
    )

    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("GMAIL_APP_PASSWORD")
    if not smtp_pass:
        print("ERROR: No SMTP password found. Set SMTP_PASS environment variable.", file=sys.stderr)
        sys.exit(1)

    reporter_files = load_reporter_files()
    if not reporter_files:
        print("No *companies.toml files found.")
        sys.exit(0)

    cache = load_cache()
    total_findings = 0
    total_errors = 0

    for path, data in reporter_files:
        reporter_info = data.get("reporter", {})
        reporter_name = reporter_info.get("name", path.stem)
        recipient = reporter_info.get("email", "")
        companies = data.get("companies", [])

        if not recipient:
            print(f"  SKIP {path.name}: no email address configured")
            continue

        if not companies:
            print(f"  SKIP {path.name}: no companies configured")
            continue

        # Use the filename stem as cache namespace to keep reporters separate
        cache_prefix = path.stem

        print(f"Checking {reporter_name} ({path.name}, {len(companies)} companies)...")
        findings, errors = check_companies(companies, court_id, cache, cache_prefix)
        total_findings += len(findings)
        total_errors += len(errors)

        subject_suffix, plain_body, html_body = build_email_body(
            findings, errors, run_time, reporter_name
        )
        subject = f"{subject_prefix} {subject_suffix}"

        try:
            send_email(smtp_cfg, smtp_pass, sender, recipient, subject, plain_body, html_body)
            print(f"  → {len(findings)} new case(s), {len(errors)} error(s) — emailed to {recipient}")
        except Exception as e:
            print(f"  ERROR sending to {recipient}: {e}", file=sys.stderr)

    save_cache(cache)
    print(f"\nDone. {total_findings} total new case(s), {total_errors} total error(s) across {len(reporter_files)} reporter(s).")


if __name__ == "__main__":
    main()
