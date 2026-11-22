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


# ── Shared master roster (for reporters that opt in) ──────────────────────────

# A reporter's *companies.toml can set, in its [reporter] table:
#     companies_source = "beat_companies"
# to draw its company list from the shared master roster instead of local
# [[companies]] blocks. Files without that key are read exactly as before.
SHARED_TOML_URL = (
    "https://raw.githubusercontent.com/"
    "david-reevely/beat-companies/main/companies.toml"
)

_shared_courts_cache = None  # memoised (companies_list, error_or_None)


def get_shared_courts_companies():
    """Fetch the shared master roster and project it into this tool's company
    shape: a list of {"name", "searches": [{"term","type"}]}.

    This mirrors beat_companies.for_tool("courts") exactly, but is kept inline
    and fetches companies.toml over HTTPS from the public repo at runtime — so
    this GitHub-Actions tool needs no extra install step or workflow change.
    Curated [company.courts] search blocks pass through verbatim (preserving
    exact/contains semantics); companies without one get a synthesised
    contains-search per match string.

    Returns (companies, error). On any failure companies is [] and error is a
    short message to surface in the reporter's digest rather than crash."""
    global _shared_courts_cache
    if _shared_courts_cache is not None:
        return _shared_courts_cache

    all_tools = ("contracts", "news_release", "google_news", "courts")
    try:
        resp = requests.get(SHARED_TOML_URL, timeout=30)
        resp.raise_for_status()
        master = tomllib.loads(resp.text)
    except Exception as e:
        _shared_courts_cache = ([], f"shared roster fetch failed: {e}")
        return _shared_courts_cache

    out = []
    for c in master.get("company", []):
        if "courts" not in c.get("tools", all_tools):
            continue
        match = c.get("match", [c["name"]])
        ct = c.get("courts", {})
        searches = ct.get("searches") or [
            {"term": m, "type": "contains"} for m in match
        ]
        out.append({"name": ct.get("display", c["name"]), "searches": searches})

    _shared_courts_cache = (out, None)
    return _shared_courts_cache


# ── Self-serve reporters (Google Sheet via the watchlist page) ───────────────

# Reporters who manage their own list on the watchlist web page are stored in a
# Google Sheet. A small token-protected Apps Script endpoint exports them as
# JSON. The URL and token come from the environment (GitHub secrets
# COURTS_EXPORT_URL and COURTS_EXPORT_TOKEN). If either is missing, sheet
# reporters are simply skipped and the file-based reporters run as before.

def fetch_sheet_reporters():
    """Returns (reporters, error). reporters is None when not configured.

    Each reporter: {"email","name","paused","new_company_mode","companies"}."""
    url = os.environ.get("COURTS_EXPORT_URL", "").strip()
    token = os.environ.get("COURTS_EXPORT_TOKEN", "").strip()
    if not url or not token:
        return None, None

    def scrub(msg):
        # Never let the token reach a log line or an email.
        return str(msg).replace(token, "***")

    try:
        resp = requests.get(url, params={"token": token}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None, scrub(f"watchlist export fetch failed: {e}")
    if not data.get("ok"):
        return None, scrub(f"watchlist export refused the request: {data.get('error')}")

    reporters = []
    for r in data.get("reporters", []):
        email = str(r.get("email", "")).strip().lower()
        if "@" not in email:
            continue
        companies = []
        for c in r.get("companies", []):
            name = str(c.get("name", "")).strip()
            searches = [
                {"term": str(s.get("term", "")).strip(),
                 "type": "exact" if s.get("type") == "exact" else "contains"}
                for s in c.get("searches", []) if str(s.get("term", "")).strip()
            ]
            if name and searches:
                companies.append({"name": name, "searches": searches})
        reporters.append({
            "email": email,
            "name": str(r.get("name", "")).strip(),
            "paused": bool(r.get("paused")),
            "new_company_mode": "full" if r.get("new_company_mode") == "full" else "baseline",
            "companies": companies,
        })
    return reporters, None


def migrate_cache_prefix(cache, old_prefix, new_prefix):
    """Copy a reporter's cache entries to a new namespace (used once, when a
    file-based reporter moves to the watchlist page), so already-reported cases
    are not reported again. Copies rather than moves, so the old file still
    works as a fallback. Does nothing once the new namespace has entries."""
    if any(k.startswith(new_prefix + ":") for k in cache):
        return 0
    copied = 0
    for k in list(cache):
        if k.startswith(old_prefix + ":"):
            cache[new_prefix + k[len(old_prefix):]] = list(cache[k])
            copied += 1
    return copied


# ── Core logic ────────────────────────────────────────────────────────────────

def _search_key(stype, term):
    return f"{stype}|{_normalize(term)}"


def _resolve_key(cache, prefix, kind, name):
    """Cache key for a company, tolerant of a change in capitalisation (a
    reporter retyping "KOHO" as "Koho" shouldn't make it look brand new)."""
    exact = f"{prefix}:{kind}:{name}"
    if exact in cache:
        return exact
    want = exact.lower()
    for k in cache:
        if k.lower() == want:
            return k
    return exact


def check_companies(companies, court_id, cache, cache_prefix, mode="full"):
    """
    Run all searches for a reporter's company list.
    cache_prefix is used to namespace cache keys per reporter.

    mode decides what happens the first time a search term is run:
      "full"     — every open case it finds is reported (the original behaviour)
      "baseline" — open cases found only by new terms are recorded quietly, and
                   a one-line note says how many; only later filings are reported

    Returns (findings list, errors list, notes list).
    """
    findings = []
    errors = []
    notes = []

    for company in companies:
        name = company["name"]
        searches = company.get("searches", [])

        if not searches:
            errors.append(f"{name}: no searches configured")
            continue

        cache_key = _resolve_key(cache, cache_prefix, "company", name)
        terms_key = _resolve_key(cache, cache_prefix, "terms", name)
        company_known = cache_key in cache
        current_terms = {_search_key(s.get("type", "contains"), s.get("term", ""))
                         for s in searches if s.get("term", "").strip()}

        # Which search terms have already been run at least once. Before this
        # record existed, every term on an already-cached company counts as
        # seen, so switching this feature on never swallows a genuine new case.
        if terms_key in cache:
            seen_terms = set(cache[terms_key])
        elif company_known:
            seen_terms = set(current_terms)
        else:
            seen_terms = set()

        all_cases = {}
        found_by_seen = set()
        found_by_new = set()
        ran_ok = set()

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
                continue

            skey = _search_key(stype, term)
            ran_ok.add(skey)
            bucket = found_by_seen if skey in seen_terms else found_by_new
            for row in rows:
                uuid = row.get("caseHeader", {}).get("caseInstanceUUID")
                if uuid:
                    bucket.add(uuid)

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

        known_uuids = set(cache.get(cache_key, []))

        baselined = set()
        newly_run = ran_ok - seen_terms
        if mode == "baseline" and newly_run:
            # Cases found only through terms running for the first time are
            # the already-open backlog, not new filings. A case also found by
            # an established term is a genuine new filing and is reported.
            baselined = (found_by_new - found_by_seen) - known_uuids
            n = len(baselined)
            backlog = ("no open cases on file yet" if n == 0 else
                       f"{n} open case{'s' if n != 1 else ''} already on file")
            if not company_known:
                notes.append(f"Now watching {name}: {backlog}. "
                             f"You'll be emailed about cases filed from now on.")
            else:
                notes.append(f"New search terms for {name}: {backlog} under them. "
                             f"You'll be emailed about cases filed from now on.")

        new_uuids = set(all_cases.keys()) - known_uuids - baselined

        for uuid in new_uuids:
            findings.append({
                "company_name": name,
                "court_id": court_id,
                **all_cases[uuid],
            })

        # The cache is a permanent ledger of every case UUID we have ever
        # reported for this company — NOT a snapshot of the latest run.
        #
        # This matters because high-volume searches return large result sets
        # that the API paginates inconsistently: a case present one day can be
        # absent the next and reappear the day after. If we overwrote the cache
        # with each run's results, every reappearance would be re-reported as
        # "new", flooding the digest with old lawsuits. By unioning (never
        # removing), a case is reported exactly once, the first time we see it.
        #
        # Trade-off: closed cases are never pruned, so the cache grows slowly.
        # That is acceptable — it is a few KB per hundred cases, and "have we
        # reported this before?" is exactly the question the ledger answers.
        cache[cache_key] = sorted(known_uuids | set(all_cases.keys()))

        # Remember which terms have now run. A term that failed this time stays
        # "new", so its backlog is handled properly on the next successful run;
        # a term the reporter removed drops out, so re-adding it later counts
        # as new again.
        cache[terms_key] = sorted((seen_terms & current_terms) | ran_ok)

    return findings, errors, notes


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_body(findings, errors, run_time, reporter_name, notes=None, edit_url=None):
    """Plain-text and HTML digest. Everything a reporter typed (company names)
    or the courts site returned is HTML-escaped."""
    esc = html_lib.escape
    notes = notes or []
    date_str = run_time.strftime("%A, %B %d, %Y")
    first = reporter_name.split()[0] if reporter_name and reporter_name.split() else ""
    greeting = f"Hi {first}," if first else ""

    if findings:
        subject_suffix = f"{len(findings)} new case(s)"
    elif errors:
        subject_suffix = "Errors only"
    elif notes:
        subject_suffix = "Watchlist updated"
    else:
        subject_suffix = "Nothing new"

    lines_plain = []
    html_parts = []
    if greeting:
        lines_plain += [greeting, ""]
        html_parts.append(f"<p>{esc(greeting)}</p>")
    lines_plain += [f"Ontario Courts Monitor — {date_str}", ""]
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
            html_parts.append(f"<h3 style='margin-bottom:4px'>{esc(company)}</h3>")

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
                  <div><strong>{esc(c['case_title'])}</strong></div>
                  <div style='color:#555; font-size:0.9em'>{esc(c['case_number'])} &nbsp;·&nbsp; Filed {esc(filed)} &nbsp;·&nbsp; {esc(c['court_abbreviation'])}</div>
                  <div style='margin-top:4px; font-size:0.9em'>{esc(parties_str)}</div>
                  <div style='margin-top:6px'><a href='{esc(url)}'>{esc(url)}</a></div>
                </div>
                """)
    elif not errors:
        lines_plain.append("No new cases found for any of your tracked companies.")
        html_parts.append("<p>No new cases found for any of your tracked companies.</p>")

    if notes:
        lines_plain += ["", "── Watchlist updates ──"]
        html_parts.append("<h3>Watchlist updates</h3><ul>")
        for note in notes:
            lines_plain.append(f"  {note}")
            html_parts.append(f"<li>{esc(note)}</li>")
        html_parts.append("</ul>")

    if errors:
        lines_plain += ["", "── Errors ──"]
        html_parts.append("<h3>Errors</h3><ul>")
        for e in errors:
            lines_plain.append(f"  {e}")
            html_parts.append(f"<li>{esc(str(e))}</li>")
        html_parts.append("</ul>")

    if edit_url:
        lines_plain += ["", f"Change what you're watching: {edit_url}"]
        html_parts.append(f"<p style='color:#888; font-size:0.9em; margin-top:24px'>"
                          f"<a href='{esc(edit_url)}'>Change what you're watching</a></p>")

    return subject_suffix, "\n".join(lines_plain) + "\n", "\n".join(html_parts)


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

    sheet_cfg = config.get("watchlist_sheet", {})
    page_url = sheet_cfg.get("page_url", "").strip() or None
    admin_email = sheet_cfg.get("admin_email", "").strip() or sender
    migrations = sheet_cfg.get("migrate_cache", {})

    # ── Gather reporters from both sources ────────────────────────────────────
    # 1. *companies.toml files in the repo (David via beat-companies, and anyone
    #    not yet moved to the watchlist page).
    reporters = []
    for path, data in load_reporter_files():
        info = data.get("reporter", {})
        shared_err = None
        if info.get("companies_source") == "beat_companies":
            companies, shared_err = get_shared_courts_companies()
        else:
            companies = data.get("companies", [])
        reporters.append({
            "label": path.name,
            "name": info.get("name", path.stem),
            "email": str(info.get("email", "")).strip(),
            "companies": companies,
            "shared_err": shared_err,
            "cache_prefix": path.stem,
            "mode": "baseline" if info.get("new_company_mode") == "baseline" else "full",
            "paused": False,
            "edit_url": None,
        })

    # 2. Reporters who manage their own list on the watchlist page.
    sheet_reporters, sheet_err = fetch_sheet_reporters()
    if sheet_err:
        print(f"  WARNING: {sheet_err}", file=sys.stderr)
    if sheet_reporters is None and not sheet_err:
        print("  (watchlist page not configured: COURTS_EXPORT_URL / COURTS_EXPORT_TOKEN unset)")
    if sheet_reporters:
        sheet_emails = {r["email"] for r in sheet_reporters}
        # Someone on the page supersedes an old file with the same address, so
        # they never get two digests. (If the page's export fails, the file
        # still runs as a fallback, because sheet_reporters is None then.)
        for r in reporters:
            if r["email"].lower() in sheet_emails:
                print(f"  {r['label']}: superseded by {r['email']}'s watchlist page entry")
        reporters = [r for r in reporters if r["email"].lower() not in sheet_emails]
        for r in sheet_reporters:
            prefix = f"sheet:{r['email']}"
            reporters.append({
                "label": f"watchlist page ({r['email']})",
                "name": r["name"],
                "email": r["email"],
                "companies": r["companies"],
                "shared_err": None,
                "cache_prefix": prefix,
                "mode": r["new_company_mode"],
                "paused": r["paused"],
                "edit_url": page_url,
                "migrate_from": migrations.get(r["email"]),
            })

    if not reporters:
        print("No reporters configured.")

    cache = load_cache()
    total_findings = 0
    total_errors = 0

    for rep in reporters:
        if not rep["email"]:
            print(f"  SKIP {rep['label']}: no email address configured")
            continue
        if rep["paused"]:
            print(f"  SKIP {rep['label']}: paused by reporter")
            continue
        # Skip only when there's genuinely nothing to do. A shared-roster fetch
        # failure is NOT a silent skip — it falls through to the digest.
        if not rep["companies"] and not rep["shared_err"]:
            print(f"  SKIP {rep['label']}: no companies configured")
            continue

        if rep.get("migrate_from"):
            n = migrate_cache_prefix(cache, rep["migrate_from"], rep["cache_prefix"])
            if n:
                print(f"  Migrated {n} cache entries from {rep['migrate_from']} to {rep['cache_prefix']}")

        print(f"Checking {rep['name']} ({rep['label']}, {len(rep['companies'])} companies)...")
        findings, errors, notes = check_companies(
            rep["companies"], court_id, cache, rep["cache_prefix"], mode=rep["mode"]
        )
        if rep["shared_err"]:
            errors.insert(0, rep["shared_err"])
        total_findings += len(findings)
        total_errors += len(errors)

        subject_suffix, plain_body, html_body = build_email_body(
            findings, errors, run_time, rep["name"], notes=notes, edit_url=rep["edit_url"]
        )
        subject = f"{subject_prefix} {subject_suffix}"

        try:
            send_email(smtp_cfg, smtp_pass, sender, rep["email"], subject, plain_body, html_body)
            print(f"  → {len(findings)} new case(s), {len(notes)} note(s), {len(errors)} error(s) — emailed to {rep['email']}")
        except Exception as e:
            print(f"  ERROR sending to {rep['email']}: {e}", file=sys.stderr)

    save_cache(cache)

    # If the watchlist page's export couldn't be read, the self-serve reporters
    # got nothing today and have no way to know. Tell the maintainer.
    if sheet_err and admin_email:
        body = (f"The courts monitor could not read the reporters' watchlists today, "
                f"so self-serve reporters were not emailed.\n\n{sheet_err}\n\n"
                f"Check the export script's deployment and the COURTS_EXPORT_URL / "
                f"COURTS_EXPORT_TOKEN secrets, then re-run the workflow.\n")
        try:
            send_email(smtp_cfg, smtp_pass, sender, admin_email,
                       f"{subject_prefix} Watchlist fetch failed", body,
                       f"<p>{html_lib.escape(body).replace(chr(10), '<br>')}</p>")
            print(f"  Alert sent to {admin_email}")
        except Exception as e:
            print(f"  ERROR sending alert to {admin_email}: {e}", file=sys.stderr)

    # No non-zero exit on a fetch failure: that would skip the workflow's
    # commit-cache step and today's cases would be re-reported tomorrow. The
    # alert email above is the signal.
    print(f"\nDone. {total_findings} total new case(s), {total_errors} total error(s) across {len(reporters)} reporter(s).")


if __name__ == "__main__":
    main()
