#!/usr/bin/env python3
"""
Search term audit for the Ontario Courts Monitor.

Runs every search term in every *companies.toml file against the courts API
and reports the hit count, so you can spot:
  - DEAD terms (0 hits) — usually an 'exact' term that doesn't match the
    full registered legal name, e.g. "LOCKHEED MARTIN" when the party is
    filed as "LOCKHEED MARTIN CORPORATION".
  - NOISY terms (large counts) — usually a short 'contains' term picking
    up unrelated parties.

For dead exact terms, the script also runs a contains search on the same
words and shows you the distinct party names that DO exist, so you can fix
the term.

Usage:  python3 audit_terms.py
"""

import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import tomllib


def _normalize(text):
    """
    Canonicalize a name or term for phrase matching: strip accents, uppercase,
    collapse every run of non-alphanumeric characters (spaces, hyphens, commas,
    periods, etc.) to a single space. Mirrors monitor.py exactly so the audit's
    counts match what the live monitor actually reports.

      "HYDRO-QUEBEC" / "HYDRO-QUÉBEC" / "Hydro Quebec"  -> "HYDRO QUEBEC"
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", " ", no_accents).upper().strip()

SCRIPT_DIR = Path(__file__).parent
API_BASE = "https://api1.courts.ontario.ca"

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


def fetch_page(term, type_code, court_id, page=0, size=25):
    params = {
        "partyHeader.partyActorInstance.displayName": term,
        "partyHeader.partyActorInstance.displayNameSearchType": type_code,
        "caseHeader.closedFlag": "false",
        "caseHeader.courtID": court_id,
        "page": page,
        "size": size,
    }
    resp = requests.get(f"{API_BASE}/courts/cms/parties", params=params,
                        headers=API_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def count_matches(term, stype, court_id):
    """Return (raw API count, phrase-filtered count, sample matching names)."""
    type_code = SEARCH_TYPES[stype]
    data = fetch_page(term, type_code, court_id)
    total = data.get("page", {}).get("totalElements", 0)

    # A term is "multi-word" if it normalizes to more than one token. This
    # catches hyphenated terms like "HYDRO-QUEBEC" that contain no literal
    # space — exactly the case the monitor's phrase filter applies to.
    phrase_norm = _normalize(term)
    is_multiword = " " in phrase_norm

    if stype == "exact" or not is_multiword:
        # No phrase post-filtering applies; sample names from first page.
        names = []
        for r in data.get("_embedded", {}).get("results", []):
            n = r.get("partyHeader", {}).get("partyActorInstance", {}).get("displayName", "")
            if n not in names:
                names.append(n)
        return total, total, names[:5]

    # Multi-word contains: count phrase-filtered matches across pages, using
    # the SAME normalized matching the monitor uses, so counts agree.
    filtered = 0
    names = []
    page = 0
    while True:
        if page > 0:
            data = fetch_page(term, type_code, court_id, page=page)
        for r in data.get("_embedded", {}).get("results", []):
            n = r.get("partyHeader", {}).get("partyActorInstance", {}).get("displayName", "")
            if phrase_norm in _normalize(n):
                filtered += 1
                if n not in names:
                    names.append(n)
        page_info = data.get("page", {})
        if page + 1 >= page_info.get("totalPages", 1):
            break
        page += 1
        if page > 400:  # safety stop (10,000 / 25)
            break
    return total, filtered, names[:5]


def suggest_for_dead_exact(term, court_id):
    """For a 0-hit exact term, run contains on the same words and show what exists."""
    try:
        data = fetch_page(term, SEARCH_TYPES["contains"], court_id)
    except Exception:
        return []
    phrase_norm = _normalize(term)
    names = []
    page = 0
    while True:
        if page > 0:
            try:
                data = fetch_page(term, SEARCH_TYPES["contains"], court_id, page=page)
            except Exception:
                break
        for r in data.get("_embedded", {}).get("results", []):
            n = r.get("partyHeader", {}).get("partyActorInstance", {}).get("displayName", "")
            if phrase_norm in _normalize(n) and n not in names:
                names.append(n)
        page_info = data.get("page", {})
        if page + 1 >= page_info.get("totalPages", 1):
            break
        page += 1
        if page > 400:
            break
    return names[:10]


def main():
    # Load court_id from config.toml
    with open(SCRIPT_DIR / "config.toml", "rb") as f:
        config = tomllib.load(f)
    court_id = config.get("settings", {}).get(
        "court_id", "68f021c4-6a44-4735-9a76-5360b2e8af13"
    )

    dead = []
    noisy = []

    for path in sorted(SCRIPT_DIR.glob("*companies.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        print(f"\n{'='*70}\n{path.name}\n{'='*70}")

        for company in data.get("companies", []):
            name = company["name"]
            for search in company.get("searches", []):
                term = search.get("term", "").strip()
                stype = search.get("type", "contains")
                if not term:
                    continue
                try:
                    total, filtered, names = count_matches(term, stype, court_id)
                except Exception as e:
                    print(f"  ERROR  {name} — '{term}' ({stype}): {e}")
                    continue

                flag = ""
                if filtered == 0:
                    flag = "  ← DEAD (0 hits)"
                    dead.append((path.name, name, term, stype))
                elif total >= 10000:
                    flag = "  ← AT API CAP, results incomplete"
                    noisy.append((path.name, name, term, stype, total))
                elif stype == "contains" and " " not in term and filtered > 100:
                    flag = "  ← noisy?"
                    noisy.append((path.name, name, term, stype, filtered))

                shown = f"{filtered}"
                if filtered != total:
                    shown = f"{filtered} (of {total} raw)"
                print(f"  {shown:>16}  {name} — '{term}' ({stype}){flag}")
                if names and (flag or stype == "exact"):
                    for n in names[:3]:
                        print(f"{'':>20}e.g. {n}")
                time.sleep(0.3)  # be polite to the API

    if dead:
        print(f"\n{'='*70}\nDEAD TERMS — these match nothing and need fixing\n{'='*70}")
        for fname, name, term, stype in dead:
            print(f"\n  {name} — '{term}' ({stype}) in {fname}")
            if stype == "exact":
                suggestions = suggest_for_dead_exact(term, court_id)
                if suggestions:
                    print(f"    Party names in the database containing this phrase:")
                    for s in suggestions:
                        print(f"      {s}")
                else:
                    print(f"    No party names contain this phrase either — "
                          f"the company may simply have no active Toronto cases.")
            time.sleep(0.3)

    print(f"\nDone. {len(dead)} dead term(s), {len(noisy)} noisy term(s).")


if __name__ == "__main__":
    main()
