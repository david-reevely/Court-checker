/**
 * Ontario Courts Monitor — reporter watchlist page
 *
 * A Google Apps Script web app that lets Logic reporters manage their own
 * courts-monitor watchlist. Deploy it with:
 *     Execute as:     Me
 *     Who has access: Anyone within The Logic
 * so Google requires a thelogic.co sign-in, and the script runs with David's
 * permissions. Reporters never get access to the backing sheet itself; they
 * can only read and change their own rows through the functions below.
 *
 * The daily monitor (GitHub Actions) reads the same sheet through a separate,
 * token-protected export script (see ../export-endpoint/Code.gs).
 *
 * One-time setup: run setup() once from the editor. It creates the sheet and
 * stores its ID in Script Properties. Run testCourtsApi() to confirm Google's
 * servers can reach the courts API (the "Test" button depends on it).
 */

// ── Settings ──────────────────────────────────────────────────────────────────

const ALLOWED_DOMAIN = 'thelogic.co';
const MAX_TERMS_PER_REPORTER = 40;   // keeps the morning run quick for everyone
const MAX_TERM_LENGTH = 120;
const MAX_NAME_LENGTH = 100;

const COURT_ID = '68f021c4-6a44-4735-9a76-5360b2e8af13';  // Toronto civil
const API_URL = 'https://api1.courts.ontario.ca/courts/cms/parties';
const SEARCH_TYPES = { contains: '300054', exact: '300012' };

const REPORTERS_TAB = 'Reporters';
const WATCHLIST_TAB = 'Watchlist';
const REPORTER_HEADERS = ['email', 'name', 'paused', 'new_company_mode', 'updated_at'];
const WATCHLIST_HEADERS = ['email', 'company', 'term', 'type', 'added_at'];

const MODES = ['baseline', 'full'];  // baseline = just start watching


// ── One-time setup (run from the editor) ──────────────────────────────────────

function setup() {
  const props = PropertiesService.getScriptProperties();
  let id = props.getProperty('SHEET_ID');
  let ss;
  if (id) {
    ss = SpreadsheetApp.openById(id);
  } else {
    ss = SpreadsheetApp.create('Courts Monitor — Reporter Watchlists');
    id = ss.getId();
    props.setProperty('SHEET_ID', id);
  }
  ensureTab_(ss, REPORTERS_TAB, REPORTER_HEADERS);
  ensureTab_(ss, WATCHLIST_TAB, WATCHLIST_HEADERS);
  const stray = ss.getSheetByName('Sheet1');
  if (stray && ss.getSheets().length > 1) ss.deleteSheet(stray);
  Logger.log('Sheet ready: ' + ss.getUrl());
  Logger.log('SHEET_ID (copy this into the export script): ' + id);
}

/** Confirms Google's servers can reach the courts API with the headers it expects. */
function testCourtsApi() {
  const r = queryCourts_('Telesat', 'contains', 0);
  Logger.log('HTTP ' + r.code + ' — ' + r.total + ' party records for "Telesat".');
  if (r.code === 200 && r.total > 0) {
    Logger.log('OK: the Test button on the page will work.');
  } else {
    Logger.log('Problem: the page will still work, but its Test button will not. Body: ' +
               String(r.body).slice(0, 300));
  }
}

function ensureTab_(ss, name, headers) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}


// ── Web app entry point ───────────────────────────────────────────────────────

function doGet() {
  const t = HtmlService.createTemplateFromFile('Index');
  return t.evaluate()
    .setTitle('Courts Monitor — My Watchlist')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


// ── Functions the page calls (google.script.run) ──────────────────────────────

/** Everything the page needs to draw itself for the signed-in reporter. */
function getState() {
  const email = currentEmail_();
  const ss = sheet_();
  const reporter = findReporter_(ss, email);
  return {
    email: email,
    isNew: !reporter,
    name: reporter ? reporter.name : defaultName_(email),
    paused: reporter ? reporter.paused : false,
    mode: reporter ? reporter.mode : 'baseline',
    companies: companiesFor_(ss, email),
    maxTerms: MAX_TERMS_PER_REPORTER,
  };
}

/** Name, pause and new-company preference. Also creates the reporter row on first save. */
function saveSettings(settings) {
  const email = currentEmail_();
  const name = cleanText_(settings && settings.name, MAX_NAME_LENGTH, 'Your name');
  const paused = !!(settings && settings.paused);
  const mode = MODES.indexOf(settings && settings.mode) >= 0 ? settings.mode : 'baseline';
  return withLock_(function () {
    const ss = sheet_();
    upsertReporter_(ss, email, { name: name, paused: paused, mode: mode });
    return getState();
  });
}

/**
 * Add a company with one or more search terms. If a company with the same name
 * (case-insensitive) is already on the list, the new terms are added to it.
 * input: { company: "Shopify", searches: [{ term: "Shopify", type: "contains" }] }
 */
function addCompany(input) {
  const email = currentEmail_();
  const company = cleanText_(input && input.company, MAX_NAME_LENGTH, 'Company name');
  const searches = cleanSearches_(input && input.searches);
  return withLock_(function () {
    const ss = sheet_();
    ensureReporterExists_(ss, email);
    const existing = companiesFor_(ss, email);
    const match = existing.find(function (c) { return c.name.toLowerCase() === company.toLowerCase(); });
    const canonical = match ? match.name : company;
    const have = new Set((match ? match.searches : []).map(searchKey_));
    const fresh = searches.filter(function (s) { return !have.has(searchKey_(s)); });
    if (!fresh.length) throw new Error('Those search terms are already on your list for ' + canonical + '.');
    const total = existing.reduce(function (n, c) { return n + c.searches.length; }, 0);
    if (total + fresh.length > MAX_TERMS_PER_REPORTER) {
      throw new Error('That would take you past the limit of ' + MAX_TERMS_PER_REPORTER +
                      ' search terms. Remove some first, or ask David.');
    }
    const now = new Date();
    const rows = fresh.map(function (s) { return [email, safe_(canonical), safe_(s.term), s.type, now]; });
    const sh = ss.getSheetByName(WATCHLIST_TAB);
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, WATCHLIST_HEADERS.length).setValues(rows);
    touchReporter_(ss, email);
    return getState();
  });
}

/** Remove one search term from a company (removes the company if it was the last term). */
function removeTerm(company, term, type) {
  const email = currentEmail_();
  return withLock_(function () {
    const ss = sheet_();
    deleteWatchRows_(ss, function (r) {
      return r.email === email && r.company.toLowerCase() === String(company).toLowerCase() &&
             r.term === String(term) && r.type === String(type);
    });
    touchReporter_(ss, email);
    return getState();
  });
}

/** Remove a company and all its search terms. */
function removeCompany(company) {
  const email = currentEmail_();
  return withLock_(function () {
    const ss = sheet_();
    deleteWatchRows_(ss, function (r) {
      return r.email === email && r.company.toLowerCase() === String(company).toLowerCase();
    });
    touchReporter_(ss, email);
    return getState();
  });
}

/**
 * Live check of one search term against the courts API, mirroring how the
 * monitor matches: multi-word "contains" terms must appear as a whole phrase.
 * Returns { ok, count, atLeast, sample, warning } or { ok:false, message }.
 */
function testTerm(term, type) {
  currentEmail_();  // signed-in reporters only
  const t = cleanText_(term, MAX_TERM_LENGTH, 'Search term');
  const ty = SEARCH_TYPES[type] ? type : 'contains';
  let first;
  try {
    first = queryCourts_(t, ty, 0);
  } catch (e) {
    return { ok: false, message: 'Could not reach the courts site right now.' };
  }
  if (first.code !== 200) {
    return { ok: false, message: 'The courts site did not answer the test (HTTP ' + first.code + ').' };
  }

  const phrase = normalize_(t);
  const multiword = ty === 'contains' && phrase.indexOf(' ') >= 0;
  let count = first.total;
  let atLeast = false;
  let names = first.rows.map(partyName_);

  if (multiword) {
    // The API ORs the words together, so filter like the monitor does. Look at
    // up to the first 100 raw records; beyond that, report a lower bound.
    let rows = first.rows.slice();
    const pages = Math.min(first.totalPages, 4);
    for (let p = 1; p < pages; p++) rows = rows.concat(queryCourts_(t, ty, p).rows);
    names = rows.map(partyName_).filter(function (n) { return normalize_(n).indexOf(phrase) >= 0; });
    count = names.length;
    atLeast = first.total > rows.length && count > 0;
  }

  const sample = Array.from(new Set(names)).slice(0, 6);
  let warning = '';
  if (count === 0) {
    warning = ty === 'exact'
      ? 'Nothing matches that exact name. Exact names must include suffixes like "INC." — try Contains to see the variants on file. (It may also simply have no open Toronto cases yet, which is fine: you will hear about the first one.)'
      : 'Nothing matches right now. That can be fine: the monitor will email you when the first case appears.';
  } else if (first.total >= 10000) {
    warning = 'This hits the courts site\'s 10,000-record cap, so results will be incomplete. Use Exact with the company\'s full legal names instead.';
  } else if (ty === 'contains' && !multiword && first.total > 300) {
    warning = 'This matches a lot of records. Check the examples: if unrelated parties show up, use Exact with full legal names.';
  }
  return { ok: true, count: count, atLeast: atLeast, sample: sample, warning: warning, raw: first.total };
}


// ── Courts API ────────────────────────────────────────────────────────────────

function queryCourts_(term, type, page) {
  const q = {
    'partyHeader.partyActorInstance.displayName': term,
    'partyHeader.partyActorInstance.displayNameSearchType': SEARCH_TYPES[type],
    'caseHeader.closedFlag': 'false',
    'caseHeader.courtID': COURT_ID,
    'page': String(page),
    'size': '25',
  };
  const qs = Object.keys(q).map(function (k) {
    return encodeURIComponent(k) + '=' + encodeURIComponent(q[k]);
  }).join('&');
  const resp = UrlFetchApp.fetch(API_URL + '?' + qs, {
    method: 'get',
    muteHttpExceptions: true,
    headers: {
      'Accept': 'application/json',
      'Origin': 'https://courts.ontario.ca',
      'Referer': 'https://courts.ontario.ca/',
    },
  });
  const code = resp.getResponseCode();
  const body = resp.getContentText();
  if (code !== 200) return { code: code, total: 0, totalPages: 0, rows: [], body: body };
  const data = JSON.parse(body);
  const pageInfo = data.page || {};
  return {
    code: code,
    total: pageInfo.totalElements || 0,
    totalPages: pageInfo.totalPages || 0,
    rows: (data._embedded && data._embedded.results) || [],
    body: '',
  };
}

function partyName_(row) {
  return (row && row.partyHeader && row.partyHeader.partyActorInstance &&
          row.partyHeader.partyActorInstance.displayName) || '';
}

/** Same folding as monitor.py's _normalize: accents off, punctuation to spaces, uppercase. */
function normalize_(text) {
  return String(text || '')
    .normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9]+/g, ' ')
    .toUpperCase().trim();
}


// ── Identity and validation ───────────────────────────────────────────────────

function currentEmail_() {
  const email = String(Session.getActiveUser().getEmail() || '').toLowerCase();
  if (!email || !email.endsWith('@' + ALLOWED_DOMAIN)) {
    throw new Error('Please sign in with your @' + ALLOWED_DOMAIN + ' Google account.');
  }
  return email;
}

function defaultName_(email) {
  return email.split('@')[0].split(/[._-]+/)
    .map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1); }).join(' ');
}

function cleanText_(value, maxLen, label) {
  const s = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
  if (!s) throw new Error(label + ' can\'t be empty.');
  if (s.length > maxLen) throw new Error(label + ' is too long (limit ' + maxLen + ' characters).');
  return s;
}

function cleanSearches_(searches) {
  if (!Array.isArray(searches) || !searches.length) throw new Error('Add at least one search term.');
  const out = [];
  const seen = new Set();
  searches.forEach(function (s) {
    const term = String((s && s.term) || '').replace(/\s+/g, ' ').trim();
    if (!term) return;
    if (term.length > MAX_TERM_LENGTH) throw new Error('A search term is too long (limit ' + MAX_TERM_LENGTH + ').');
    const type = (s && s.type) === 'exact' ? 'exact' : 'contains';
    const item = { term: term, type: type };
    const k = searchKey_(item);
    if (!seen.has(k)) { seen.add(k); out.push(item); }
  });
  if (!out.length) throw new Error('Add at least one search term.');
  return out;
}

function searchKey_(s) { return s.type + '|' + String(s.term).toUpperCase(); }

/** Stop a value from being read as a formula when written to the sheet. */
function safe_(v) {
  const s = String(v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}


// ── Sheet access ──────────────────────────────────────────────────────────────

function sheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('SHEET_ID');
  if (!id) throw new Error('The watchlist sheet is not set up yet. (David: run setup() in the script editor.)');
  return SpreadsheetApp.openById(id);
}

function withLock_(fn) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) throw new Error('Busy — please try again in a moment.');
  try { return fn(); } finally { lock.releaseLock(); }
}

function readReporters_(ss) {
  const values = ss.getSheetByName(REPORTERS_TAB).getDataRange().getValues();
  return values.slice(1).map(function (r, i) {
    return {
      row: i + 2,
      email: String(r[0]).toLowerCase().trim(),
      name: String(r[1]),
      paused: r[2] === true || String(r[2]).toUpperCase() === 'TRUE',
      mode: MODES.indexOf(String(r[3])) >= 0 ? String(r[3]) : 'baseline',
    };
  }).filter(function (r) { return r.email; });
}

function findReporter_(ss, email) {
  return readReporters_(ss).find(function (r) { return r.email === email; }) || null;
}

function upsertReporter_(ss, email, fields) {
  const sh = ss.getSheetByName(REPORTERS_TAB);
  const existing = findReporter_(ss, email);
  const row = [email, safe_(fields.name), fields.paused, fields.mode, new Date()];
  if (existing) {
    sh.getRange(existing.row, 1, 1, row.length).setValues([row]);
  } else {
    sh.getRange(sh.getLastRow() + 1, 1, 1, row.length).setValues([row]);
  }
}

function ensureReporterExists_(ss, email) {
  if (!findReporter_(ss, email)) {
    upsertReporter_(ss, email, { name: defaultName_(email), paused: false, mode: 'baseline' });
  }
}

function touchReporter_(ss, email) {
  const r = findReporter_(ss, email);
  if (r) ss.getSheetByName(REPORTERS_TAB).getRange(r.row, 5).setValue(new Date());
}

function readWatchRows_(ss) {
  const values = ss.getSheetByName(WATCHLIST_TAB).getDataRange().getValues();
  return values.slice(1).map(function (r, i) {
    return {
      row: i + 2,
      email: String(r[0]).toLowerCase().trim(),
      company: String(r[1]).trim(),
      term: String(r[2]).trim(),
      type: String(r[3]) === 'exact' ? 'exact' : 'contains',
    };
  }).filter(function (r) { return r.email && r.company && r.term; });
}

function companiesFor_(ss, email) {
  const byName = {};
  const order = [];
  readWatchRows_(ss).forEach(function (r) {
    if (r.email !== email) return;
    const k = r.company.toLowerCase();
    if (!byName[k]) { byName[k] = { name: r.company, searches: [] }; order.push(k); }
    byName[k].searches.push({ term: r.term, type: r.type });
  });
  return order.map(function (k) { return byName[k]; })
    .sort(function (a, b) { return a.name.localeCompare(b.name); });
}

function deleteWatchRows_(ss, predicate) {
  const sh = ss.getSheetByName(WATCHLIST_TAB);
  const rows = readWatchRows_(ss).filter(predicate).map(function (r) { return r.row; });
  rows.sort(function (a, b) { return b - a; }).forEach(function (n) { sh.deleteRow(n); });
}

/** Exposed for the HTML template to inline files if ever needed. */
function include(name) {
  return HtmlService.createHtmlOutputFromFile(name).getContent();
}
