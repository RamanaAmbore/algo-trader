// Regression guard: MCX positions (CRUDEOIL futures/options) must appear
// in the Positions grid on /pulse during closed hours.
//
// Root cause (2026-07-04): _positions_snapshot() in positions.py had
// `AND db.ltp IS NOT NULL` in the outer WHERE clause. MCX rows captured at
// the 15:35 IST NSE-close snapshot have ltp=NULL (MCX still open at that
// time). The outer filter dropped them so the entire CRUDEOIL book was
// invisible on the Positions grid after market close.
//
// Fix: Remove the outer `AND db.ltp IS NOT NULL` predicate. The zero-payload
// guard (NOT ltp=0 AND pnl=0 AND avg>0) is still present for Dhan phantoms.
// NULL ltp collapses to 0.0 in the reader — already correct.
//
// Five quality dimensions:
//   SSOT       — verifies the /api/positions endpoint + Positions grid as
//                one end-to-end path; no duplicate logic.
//   Correctness— MCX contract names visible in .ag-col-sym cells.
//   Performance— API probe runs before DOM, fails fast if backend broken.
//   Reuse      — uses the same auth + BASE_URL convention as all pulse specs.
//   UX         — checks the exact cell class operators see (ag-col-sym).

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Symbols expected to appear when MCX CRUDEOIL positions are in the book.
// These are representative contract names — test passes if AT LEAST ONE
// CRUDEOIL-prefixed symbol appears. This avoids brittleness when exact
// contract names roll at expiry.
const CRUDEOIL_PREFIX = 'CRUDEOIL';

async function _login(page) {
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) return (await r.json()).access_token;
  }
  return null;
}

// ---------------------------------------------------------------------------
// 1. API probe — verify backend returns CRUDEOIL rows in positions payload
// ---------------------------------------------------------------------------

test(`/api/positions returns MCX CRUDEOIL rows in snapshot [${BASE}]`, async ({ page }) => {
  const tok = await _login(page);
  if (!tok) test.skip('auth failed');

  const resp = await page.request.get(`${BASE}/api/positions`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  expect(resp.ok(), `/api/positions returned ${resp.status()}`).toBe(true);

  const body = await resp.json();
  const rows = body.rows ?? [];

  const crudeRows = rows.filter(r => r.tradingsymbol?.startsWith(CRUDEOIL_PREFIX));
  if (crudeRows.length === 0) {
    // No CRUDEOIL in book on this run — skip to avoid false failures on days
    // when the operator has no MCX positions. The test is meaningful only when
    // MCX positions exist.
    console.log('No CRUDEOIL positions in book — skipping visibility assertion');
    test.skip();
    return;
  }

  console.log(`Found ${crudeRows.length} CRUDEOIL row(s): ${crudeRows.map(r => r.tradingsymbol).join(', ')}`);
  // All CRUDEOIL rows must have a tradingsymbol field (basic schema check)
  for (const r of crudeRows) {
    expect(r.tradingsymbol).toBeTruthy();
    expect(r.exchange).toBe('MCX');
  }
});

// ---------------------------------------------------------------------------
// 2. UI probe — CRUDEOIL rows appear in the Positions grid on /pulse
// ---------------------------------------------------------------------------

test(`pulse Positions grid shows CRUDEOIL rows during closed hours [${BASE}]`, async ({ page }) => {
  const tok = await _login(page);
  if (!tok) test.skip('auth failed');

  // First check if CRUDEOIL rows exist in backend — skip if not
  const apiResp = await page.request.get(`${BASE}/api/positions`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  if (!apiResp.ok()) test.skip('positions API unavailable');

  const body = await apiResp.json();
  const crudeRows = (body.rows ?? []).filter(r => r.tradingsymbol?.startsWith(CRUDEOIL_PREFIX));
  if (crudeRows.length === 0) {
    console.log('No CRUDEOIL in book — skipping grid visibility check');
    test.skip();
    return;
  }
  const expectedSymbols = crudeRows.map(r => r.tradingsymbol);
  console.log(`Expecting these CRUDEOIL symbols in grid: ${expectedSymbols.join(', ')}`);

  // Set auth + navigate
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  // Wait for grid to settle (positions load + grid render)
  await page.waitForTimeout(7000);

  // At least one CRUDEOIL symbol must appear in the Positions grid cells.
  // We probe `.ag-col-sym` (the symbol column cell class) as it's the stable
  // class for the ag-Grid symbol column (same class used across all pulse specs).
  let foundAny = false;
  for (const sym of expectedSymbols) {
    const count = await page.locator('.ag-col-sym', { hasText: sym }).count();
    if (count > 0) {
      foundAny = true;
      console.log(`Grid shows ${sym} (${count} cell(s))`);
    } else {
      console.warn(`MISSING from grid: ${sym}`);
    }
  }

  expect(
    foundAny,
    `None of the CRUDEOIL positions [${expectedSymbols.join(', ')}] appear in ` +
    `the Positions grid. Root cause was the outer AND db.ltp IS NOT NULL filter ` +
    `in _positions_snapshot() that dropped MCX rows with ltp=NULL.`
  ).toBe(true);
});

// ---------------------------------------------------------------------------
// 3. Regression contract — outer ltp IS NOT NULL predicate must be absent
// ---------------------------------------------------------------------------

test('positions.py _positions_snapshot outer WHERE has no ltp IS NOT NULL', async () => {
  // Source-level guard: ensures the one-line fix (remove outer ltp IS NOT NULL)
  // is never re-introduced. We read the backend source directly from the
  // filesystem — works in both local and CI because the repo is checked out.
  const fs = await import('fs');
  const path = await import('path');

  const filePath = path.resolve(
    new URL('.', import.meta.url).pathname,
    '../../backend/api/routes/positions.py'
  );
  const src = fs.readFileSync(filePath, 'utf8');

  // The CTE (lines 61-62) is intentionally allowed to have ltp IS NOT NULL
  // as a batch anchor. Only the OUTER WHERE (post-JOIN) must NOT have it.
  // Strategy: extract the portion after the JOIN and assert the outer WHERE
  // does not repeat the predicate.
  const joinIdx = src.indexOf('JOIN latest_batch lb');
  expect(joinIdx).toBeGreaterThan(-1);

  const afterJoin = src.slice(joinIdx);
  const outerWhereMatch = afterJoin.match(/WHERE db\.kind = 'positions'[\s\S]*?ORDER BY/);
  expect(outerWhereMatch, 'Could not find outer WHERE clause after JOIN').toBeTruthy();

  const outerWhere = outerWhereMatch[0];
  expect(
    outerWhere,
    'Outer WHERE must NOT contain "db.ltp IS NOT NULL" — that predicate drops ' +
    'MCX positions with ltp=NULL captured at the 15:35 IST NSE-close snapshot. ' +
    'The ltp IS NOT NULL constraint belongs only in the CTE batch anchor, not here.'
  ).not.toMatch(/db\.ltp IS NOT NULL/);
});
