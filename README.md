# Ontario Courts Monitor

Checks the Ontario courts portal each weekday morning for new civil cases
involving companies you track, and emails each reporter a digest of anything
new.

Uses the public party-search API at `https://api1.courts.ontario.ca`.
No account or authentication required.

---

## How reporters are configured

Reporters come from two places, and each gets their own separate digest:

- **The watchlist page** (the normal way for colleagues). Reporters sign in
  with their thelogic.co Google account, add companies and search terms, test
  them live, choose whether new companies start quietly or with a full list of
  open cases, and can pause their email. Their lists live in a Google Sheet that
  the monitor reads each morning through a token-protected export. Code is in
  `apps-script/`; setup steps are in `WATCHLIST_PAGE_SETUP.md`.
- **`*companies.toml` files** in this repo. David's file draws its list from
  the shared `beat-companies` roster (`companies_source = "beat_companies"`).
  If someone is on the watchlist page and also has a file under the same email
  address, the page wins and the file is ignored, unless the page's export
  can't be read that day, in which case the file is used as a fallback.

A reporter file has a `[reporter]` block with the recipient's name and email,
followed by one `[[companies]]` block per company:

```toml
[reporter]
name = "Jane Jones"
email = "jane.jones@thelogic.co"

[[companies]]
name = "Telesat"
searches = [
  { term = "Telesat", type = "contains" },
]

[[companies]]
name = "Bell Canada"
searches = [
  { term = "BELL CANADA", type = "exact" },
  { term = "BELL MOBILITY INC.", type = "exact" },
]
```

A file reporter can add `new_company_mode = "baseline"` to `[reporter]` so
that newly added companies start quietly (the default for files is `"full"`,
which reports every open case the first time).

### Search types

Each search has a `type` of either `contains` or `exact`:

- **`contains`** — matches any party whose name includes the term. Good for
  distinctive names unlikely to appear inside unrelated parties
  (e.g. `Telesat`, `Bombardier`). For multi-word terms, the script requires
  the whole phrase to appear (the API alone would OR the words), and matching
  ignores accents and punctuation, so `HYDRO-QUEBEC` matches `HYDRO-QUEBEC`
  and `HYDRO-QUEBEC DISTRIBUTION` but not an unrelated `... QUEBEC INC.`

- **`exact`** — matches only parties whose name equals the term exactly. Use
  this for common words that would otherwise pull in unrelated parties
  (e.g. `Bell Canada`), and for high-volume companies where listing each
  registered legal entity by name is faster and more complete than a broad
  `contains` search that hits the API's 10,000-result cap.

Search terms are case-insensitive on the API side. The courts database stores
names in uppercase, but you can type them however you like.

### Finding the right exact names

`audit_terms.py` runs every search term against the live API and reports hit
counts, flagging terms that match nothing (usually an `exact` term missing a
corporate suffix like `INC.`) and terms that are noisy. For any dead `exact`
term it prints the actual party names in the database containing that phrase,
so you can copy the correct legal name. Run it locally:

```bash
python3 audit_terms.py
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/david-reevely/Court-checker.git
cd Court-checker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure email

Edit `config.toml` with the sending Gmail address. Recipient addresses live in
each reporter's `*companies.toml` file, not here.

Set the Gmail app password as an environment variable:

```bash
export SMTP_PASS="your-gmail-app-password"
```

To create one: Google Account -> Security -> 2-Step Verification -> App passwords.

### 3. Test locally

```bash
python monitor.py
```

On first run, `cache.json` is created and all currently active cases are
treated as new. Subsequent runs report only cases seen for the first time.

---

## GitHub Actions (automated daily runs)

1. Add a repository secret named `SMTP_PASS` (Settings -> Secrets -> Actions)
2. The workflow in `.github/workflows/monitor.yml` runs once each weekday
   morning (09:00 UTC; GitHub's scheduler usually delays it a little, landing
   it in the early-morning Eastern window)
3. You can also trigger it manually from the Actions tab
4. After each run the workflow commits the updated `cache.json` back to the
   repo

---

## How it works

- Queries `api1.courts.ontario.ca` for each search term, paginating results
- Filters for Civil and Small Claims Court (Toronto), active cases only
- For multi-word `contains` terms, post-filters so the full phrase must appear
- Tracks every case UUID ever reported per reporter+company in `cache.json`
  as a permanent ledger -- UUIDs are only ever added, never removed, so a case
  that drops out of the API's result window and later reappears is not
  re-reported
- Emails each reporter a digest: new cases if any, otherwise a "nothing new"
  confirmation; case titles and party names are HTML-escaped
- Each case links directly to the Ontario courts portal

---

## Notes

- Only Toronto (Superior Court / Civil and Small Claims) is covered; other
  court locations are not queried yet
- The script catches companies as **named parties** only
- Because the cache is a permanent ledger, it grows slowly over time; this is
  intended and the size is negligible
- High-volume `contains` searches can hit the API's 10,000-result cap; for
  those companies, listing each registered legal entity as an `exact` search
  is both faster and more complete
