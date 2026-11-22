/**
 * One-time move of Claire Brownell's watchlist from clairebrownellcompanies.toml
 * onto the watchlist page. Run importClaire() once from the editor after setup(),
 * then delete this file. Safe to run twice: it skips terms already present.
 */
function importClaire() {
  const email = 'claire.brownell@thelogic.co';
  const ss = sheet_();
  upsertReporter_(ss, email, { name: 'Claire Brownell', paused: false, mode: 'baseline' });

  const companies = [
    { name: 'Neo Financial', searches: [{ term: 'Neo Financial', type: 'contains' }] },
    { name: 'Wealthsimple',  searches: [{ term: 'Wealthsimple',  type: 'contains' }] },
    { name: 'Koho',          searches: [{ term: 'Koho',          type: 'contains' }] },
  ];

  const have = new Set(readWatchRows_(ss)
    .filter(function (r) { return r.email === email; })
    .map(function (r) { return r.company.toLowerCase() + '|' + r.type + '|' + r.term.toUpperCase(); }));
  const now = new Date();
  const rows = [];
  companies.forEach(function (c) {
    c.searches.forEach(function (s) {
      const k = c.name.toLowerCase() + '|' + s.type + '|' + s.term.toUpperCase();
      if (!have.has(k)) rows.push([email, c.name, s.term, s.type, now]);
    });
  });
  if (rows.length) {
    const sh = ss.getSheetByName(WATCHLIST_TAB);
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, WATCHLIST_HEADERS.length).setValues(rows);
  }
  Logger.log('Imported ' + rows.length + ' search terms for ' + email);
}
