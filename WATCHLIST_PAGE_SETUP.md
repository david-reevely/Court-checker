# Watchlist page: setup

The watchlist page lets Logic reporters manage their own courts-monitor
watchlist after signing in with their thelogic.co Google account. It has three
parts:

| Part | What it is | Who can reach it |
|---|---|---|
| Watchlist page | Apps Script web app (`apps-script/watchlist-page/`) plus a Google Sheet in your Drive | Anyone signed in to thelogic.co |
| Export endpoint | A second Apps Script web app (`apps-script/export-endpoint/`) | Anyone holding the secret token, meaning the GitHub workflow |
| Monitor | `monitor.py` in this repo, run daily by GitHub Actions | — |

Your own list keeps coming from `beat-companies`. Nothing about it changes.

Do every step signed in as **david.reevely@thelogic.co**. If your browser is
also signed in to a personal Google account, use a Chrome profile (or an
incognito window) that has only your work account. Apps Script web apps
misbehave when several Google accounts are signed in at once.

---

## Part A — The watchlist page

1. Go to https://script.google.com and click **New project**.
2. Click **Untitled project** at the top and rename it
   `Courts Monitor — Watchlist Page`.
3. The editor opens on a file called `Code.gs`. Select everything in it, delete
   it, and paste in the whole of `apps-script/watchlist-page/Code.gs` from this
   repo.
4. Click the **+** next to **Files**, choose **HTML**, and name it `Index`
   (exactly that: capital I, no `.html`). Delete what's in it and paste in
   `apps-script/watchlist-page/Index.html`.
5. Click **+** again, choose **Script**, and name it `Migrate`. Paste in
   `apps-script/watchlist-page/Migrate.gs`.
6. Click the **Save** icon (or Cmd-S).
7. In the toolbar, the function dropdown (next to **Run** and **Debug**)
   probably says `setup`. If not, choose `setup`. Click **Run**.
   - Google asks for authorization. Click **Review permissions**, choose your
     thelogic.co account, and click **Allow**.
   - The **Execution log** at the bottom shows two lines: the sheet's URL, and
     `SHEET_ID (copy this into the export script): 1AbC...`. Copy that ID
     somewhere; you need it in Part B.
8. Choose `testCourtsApi` in the dropdown and click **Run**. The log should end
   with `OK: the Test button on the page will work.`
   - If it says `Problem:` instead, carry on: the page works and reporters can
     still add companies. Only the Test button won't work. Send me the log line
     and I'll look at it.
9. Choose `importClaire` and click **Run**. The log should say
   `Imported 3 search terms for claire.brownell@thelogic.co`.
10. Click **Deploy** (top right) → **New deployment**.
    - Click the gear next to **Select type** and choose **Web app**.
    - Description: `Watchlist page`
    - Execute as: **Me (david.reevely@thelogic.co)**
    - Who has access: **Anyone within The Logic** (it may be labelled with the
      thelogic.co domain instead)
    - Click **Deploy**, then copy the **Web app** URL. It ends in `/exec`.
      This is the link you give reporters.
11. Open that URL. You should see the page with "Signed in as
    david.reevely@thelogic.co". Don't add your own companies here: they'd get
    you a second digest. Your list stays on `beat-companies`.

**Changing the page later:** paste in the new code, then **Deploy → Manage
deployments**, click the pencil, set **Version** to **New version**, and
click **Deploy**. The URL stays the same. (Creating a *new* deployment instead
would give it a new URL.)

---

## Part B — The export endpoint

1. Back at https://script.google.com, click **New project** and rename it
   `Courts Monitor — Export`.
2. Replace the contents of `Code.gs` with `apps-script/export-endpoint/Code.gs`.
   Save.
3. Click the gear icon (**Project Settings**) in the left sidebar. Scroll to
   **Script Properties** → **Add script property**.
   - Property: `SHEET_ID`
   - Value: the ID you copied in Part A, step 7
   - Click **Save script properties**.
4. Go back to the editor (the `<>` icon), choose `setupExport` in the dropdown,
   and click **Run**. Authorize it the same way as before. The log shows
   `EXPORT_TOKEN (put this in the GitHub secret COURTS_EXPORT_TOKEN): 3f9a...`.
   Copy the token.
5. **Deploy → New deployment** → gear → **Web app**.
   - Description: `Export for GitHub Actions`
   - Execute as: **Me**
   - Who has access: **Anyone**
   - Click **Deploy** and copy the **Web app** URL.
   - If **Anyone** isn't offered, your Workspace admin has blocked it. Stop
     there and tell me; the fallback is a Google service account, which takes
     a little more setup.
6. Check it in Terminal. Paste in your URL and token in place of the
   placeholders, keeping the quotation marks:

   ```bash
   curl -sL "PASTE_EXPORT_URL_HERE?token=PASTE_TOKEN_HERE" | python3 -m json.tool | head -30
   ```

   You should see `"ok": true` and Claire's three companies. Then check that it
   refuses a wrong token:

   ```bash
   curl -sL "PASTE_EXPORT_URL_HERE?token=wrong" ; echo
   ```

   That should print `{"ok":false,"error":"forbidden"}`.

If the token ever leaks, run `rotateToken` in this project and update the
GitHub secret with the new token it logs.

---

## Part C — GitHub

1. In the Court-checker repo on github.com: **Settings → Secrets and variables
   → Actions → New repository secret**. Add two:
   - Name `COURTS_EXPORT_URL`, value: the export URL from Part B, step 5
   - Name `COURTS_EXPORT_TOKEN`, value: the token from Part B, step 4
2. Make sure the updated files are in the repo: `monitor.py`, `config.toml`,
   `.github/workflows/monitor.yml` and the `apps-script/` folder. The workflow
   change adds two `env` lines that pass the secrets to the monitor.
3. In `config.toml`, set `page_url` under `[watchlist_sheet]` to the watchlist
   page URL from Part A, step 10. It becomes the "Change what you're watching"
   link at the bottom of reporters' emails.
4. Go to **Actions → Ontario Courts Monitor → Run workflow**. When it finishes,
   open the **Run monitor** step's log. You should see:
   - `clairebrownellcompanies.toml: superseded by claire.brownell@thelogic.co's watchlist page entry`
   - `Migrated 3 cache entries from clairebrownellcompanies to sheet:claire.brownell@thelogic.co`
   - one `emailed to` line for Claire and one for you

   Claire's email should say nothing new (or list only genuinely new cases).
   Nothing she has already been sent is re-sent.

---

## Part D — Switching people over

- Send Claire the page link. Once a few morning runs have gone through
  cleanly, delete `clairebrownellcompanies.toml`. Until then it acts as a
  fallback: if the export ever fails, she gets her old list instead of nothing.
- Replace `signup.html` with a pointer to the page, or delete it.
- Send other reporters the link. They sign in, set their name and preference,
  and add companies. Their first digest arrives the next weekday morning.

## How it behaves day to day

- **New companies.** Each reporter chooses on the page. "Just start watching"
  (the default) records cases already open, notes how many in that morning's
  email, and reports only later filings. "Email me the existing cases too"
  lists every open case the next morning. The same rule applies to a new
  search term added to an existing company.
- **Pause** stops a reporter's email without losing their list.
- **Limits:** 40 search terms per reporter, set by `MAX_TERMS_PER_REPORTER` in
  the page's `Code.gs`.
- **If the export can't be read** on a given morning, self-serve reporters
  get no digest, and you get an email headed `Watchlist fetch failed`. File-based
  reporters, including you, are unaffected.
- **Privacy.** Reporters see only their own list. The sheet stays in your Drive
  and isn't shared with anyone. Don't share it: watchlists show what people
  are working on.
- **Checking terms.** `audit_terms.py` covers the `*companies.toml` files. For
  page users, the Test button does the same job as they type.
