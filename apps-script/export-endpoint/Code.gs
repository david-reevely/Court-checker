/**
 * Ontario Courts Monitor — watchlist export endpoint
 *
 * A second, separate Apps Script project. The GitHub Actions monitor can't sign
 * in to Google, so this script is deployed with
 *     Execute as:     Me
 *     Who has access: Anyone
 * and refuses every request that doesn't carry the secret token. It is
 * read-only: it returns the reporters' watchlists as JSON and changes nothing.
 *
 * One-time setup:
 *   1. Project Settings → Script Properties → add SHEET_ID (from the watchlist
 *      page's setup() log).
 *   2. Run setupExport() once; copy the token it logs into the GitHub secret
 *      COURTS_EXPORT_TOKEN.
 */

function setupExport() {
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('SHEET_ID')) {
    throw new Error('Add SHEET_ID in Project Settings → Script Properties first.');
  }
  let token = props.getProperty('EXPORT_TOKEN');
  if (!token) {
    token = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '');
    props.setProperty('EXPORT_TOKEN', token);
  }
  // Touch the sheet so the authorization prompt covers it.
  SpreadsheetApp.openById(props.getProperty('SHEET_ID')).getName();
  Logger.log('EXPORT_TOKEN (put this in the GitHub secret COURTS_EXPORT_TOKEN): ' + token);
}

/** Run this to replace the token if it ever leaks; then update the GitHub secret. */
function rotateToken() {
  PropertiesService.getScriptProperties().deleteProperty('EXPORT_TOKEN');
  setupExport();
}

function doGet(e) {
  const props = PropertiesService.getScriptProperties();
  const expected = props.getProperty('EXPORT_TOKEN') || '';
  const given = String((e && e.parameter && e.parameter.token) || '');
  if (!expected || !safeEqual_(given, expected)) {
    return json_({ ok: false, error: 'forbidden' });
  }
  try {
    return json_({ ok: true, generated_at: new Date().toISOString(), reporters: export_(props.getProperty('SHEET_ID')) });
  } catch (err) {
    return json_({ ok: false, error: 'export failed: ' + err.message });
  }
}

function export_(sheetId) {
  const ss = SpreadsheetApp.openById(sheetId);
  const reporters = ss.getSheetByName('Reporters').getDataRange().getValues().slice(1);
  const watch = ss.getSheetByName('Watchlist').getDataRange().getValues().slice(1);

  const byEmail = {};
  reporters.forEach(function (r) {
    const email = String(r[0]).toLowerCase().trim();
    if (!email) return;
    byEmail[email] = {
      email: email,
      name: String(r[1] || ''),
      paused: r[2] === true || String(r[2]).toUpperCase() === 'TRUE',
      new_company_mode: String(r[3]) === 'full' ? 'full' : 'baseline',
      companies: [],
      _index: {},
    };
  });

  watch.forEach(function (r) {
    const email = String(r[0]).toLowerCase().trim();
    const company = String(r[1]).trim();
    const term = String(r[2]).trim();
    if (!email || !company || !term || !byEmail[email]) return;
    const rep = byEmail[email];
    const key = company.toLowerCase();
    if (!rep._index[key]) {
      rep._index[key] = { name: company, searches: [] };
      rep.companies.push(rep._index[key]);
    }
    rep._index[key].searches.push({ term: term, type: String(r[3]) === 'exact' ? 'exact' : 'contains' });
  });

  return Object.keys(byEmail).sort().map(function (k) {
    const rep = byEmail[k];
    delete rep._index;
    return rep;
  });
}

function safeEqual_(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
