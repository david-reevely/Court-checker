# Ontario Courts Monitor

Checks the Ontario courts portal each weekday morning for new civil cases
involving companies you track, and emails a digest of anything new.

Uses the public search API behind `https://courts.ontario.ca/portal/search/party`.
No account or authentication required.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/ontario-courts-monitor.git
cd ontario-courts-monitor
pip install -r requirements.txt
```

### 2. Configure your companies

Edit `companies.toml`. Add one `[[companies]]` block per search term:

```toml
[[companies]]
name = "Telesat"          # appears in your email
search_term = "Telesat"   # sent to the courts API (contains-match)
```

The search is a **contains** match, so `"Telesat"` will find
`TELESAT CANADA`, `TELESAT CORPORATION`, etc.

### 3. Configure email

Edit `config.toml` with your Gmail address and recipient address.

Then set your Gmail app password as an environment variable:

```bash
export SMTP_PASS="your-gmail-app-password"
```

To create a Gmail app password: Google Account → Security → 2-Step Verification → App passwords.

### 4. Test locally

```bash
python monitor.py
```

On first run, `cache.json` will be created and **all currently active cases**
will be treated as new (so you get a full picture of what exists today).
Subsequent runs will only report cases that appear for the first time.

---

## GitHub Actions (automated daily runs)

1. Push this repo to GitHub
2. Add a repository secret named `SMTP_PASS` (Settings → Secrets → Actions)
3. The workflow in `.github/workflows/monitor.yml` runs at 7 AM Eastern on weekdays
4. You can also trigger it manually from the Actions tab

---

## How it works

- Queries `api1.courts.ontario.ca` for each search term
- Filters for Civil and Small Claims Court, active cases only
- Tracks seen case UUIDs in `cache.json`
- Emails a digest: new cases if any, otherwise a "nothing new" confirmation
- Each case links directly to the Ontario courts portal

---

## Notes

- Cases filed outside Toronto (Superior Court) are not covered yet
- The script catches companies as **named parties** only
- Search terms are case-insensitive on the API side
