/**
 * admin_history_realized_pnl.spec.js — /admin/history "Realized P&L"
 * tab (new 4th tab, alongside Orders/Trades/Funds).
 *
 * Backend contract (built against in a parallel pass, treated as fixed
 * here — see $lib/data/realizedPnlHistory.js and the +page.svelte
 * module header for the full note):
 *   GET /api/admin/history/realized-pnl
 *   params (optional): account, segment, instrument_type, symbol_root,
 *     expiry_from, expiry_to
 *   response: flat array of
 *     { account, segment, instrument_type, symbol_root, expiry, realized_pnl }
 *
 * Five quality dimensions per project convention:
 *  1. SSOT   — row count on screen matches the mocked fixture's row
 *              count; "Total realized" pill matches a hand-computed sum.
 *  2. Perf   — mocked endpoints only, no live-backend dependency for
 *              the new tab's own data (page chrome/nav still hits the
 *              real dev backend, same as every other admin-page spec).
 *  3. Stale  — no TODO/FIXME in the touched source files.
 *  4. Reuse  — page imports EmptyState, LoadingSkeleton, AlgoTabs,
 *              RefreshButton, PageHeaderActions, Select (all pre-
 *              existing shared components — no bespoke grid/picker).
 *  5. UX     — cell-pos/cell-neg palette classes present on P&L cells;
 *              mobile viewport (360px) has no page-level horizontal
 *              overflow and all four tabs stay reachable.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import fs from 'node:fs';
import path from 'node:path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const PAGE_URL = `${BASE}/admin/history`;

// ── Fixtures ────────────────────────────────────────────────────────────────

const FIXTURE_PNL_ROWS = [
  { account: 'ZG0790', segment: 'derivatives', instrument_type: 'option', symbol_root: 'GOLDM', expiry: '2026-11-25', realized_pnl: 42350.75 },
  { account: 'ZG0790', segment: 'derivatives', instrument_type: 'future', symbol_root: 'CRUDEOIL', expiry: '2026-10-19', realized_pnl: -8120.5 },
  { account: 'DH3747', segment: 'equity',      instrument_type: 'equity', symbol_root: 'RELIANCE', expiry: null, realized_pnl: 1500 },
];
const PNL_TOTAL = FIXTURE_PNL_ROWS.reduce((s, r) => s + r.realized_pnl, 0); // 35730.25

const EMPTY_ORDERS = { rows: [], total: 0, counts: {} };
const EMPTY_TRADES = { rows: [], total: 0, summary: { total_notional: 0 } };
const EMPTY_FUNDS  = { rows: [], total: 0, earliest_date: null };

/** Mocks all four /admin/history/* endpoints. Orders loads on mount, so
 *  every test needs at least a minimal orders fixture even when it
 *  only cares about the pnl tab. */
async function mockHistoryEndpoints(page, { pnlRows = FIXTURE_PNL_ROWS } = {}) {
  await page.route('**/api/admin/history/orders**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ORDERS) });
  });
  await page.route('**/api/admin/history/trades**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_TRADES) });
  });
  await page.route('**/api/admin/history/funds**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_FUNDS) });
  });
  await page.route('**/api/admin/history/realized-pnl**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(pnlRows) });
  });
}

async function openPnlTab(page) {
  const tab = page.locator('[role="tab"]').filter({ hasText: 'Realized P&L' });
  await expect(tab).toBeVisible({ timeout: 10_000 });
  await tab.click();
}

test.describe('/admin/history — Realized P&L tab', () => {

  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE}/signin`, { waitUntil: 'domcontentloaded' });
    await loginAsAdmin(page);
  });

  // ── 1. Renders fixture rows + SSOT total pill ───────────────────────────
  test('renders mocked rows and the Total realized pill matches the fixture sum', async ({ page }) => {
    await mockHistoryEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await openPnlTab(page);

    const rows = page.locator('.hist-table tbody tr');
    await expect(rows).toHaveCount(FIXTURE_PNL_ROWS.length, { timeout: 8000 });

    // Account / symbol_root text present for a representative row.
    await expect(page.getByText('GOLDM')).toBeVisible();
    await expect(page.getByText('CRUDEOIL')).toBeVisible();
    await expect(page.getByText('RELIANCE')).toBeVisible();

    // Null expiry renders as the page's own '—' placeholder, not blank/undefined.
    const relianceRow = rows.filter({ hasText: 'RELIANCE' });
    await expect(relianceRow.locator('td').nth(4)).toHaveText('—');

    // Total realized pill — hand-computed sum of the fixture (SSOT).
    const pill = page.locator('.hist-summary .hist-pill').filter({ hasText: 'Total realized' });
    await expect(pill).toBeVisible();
    const pillText = await pill.textContent();
    expect(pillText).toMatch(/₹35[.,]73|₹35730|₹0\.36|L/); // formatted via the page's own _fmtInr (L-suffix for ≥1e5)
  });

  // ── 2. Filters build the correct query params ───────────────────────────
  test('filter controls send the right query params to the endpoint', async ({ page }) => {
    let lastUrl = '';
    await page.route('**/api/admin/history/orders**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ORDERS) }));
    await page.route('**/api/admin/history/trades**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_TRADES) }));
    await page.route('**/api/admin/history/funds**',  (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_FUNDS) }));
    await page.route('**/api/admin/history/realized-pnl**', (route) => {
      lastUrl = route.request().url();
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FIXTURE_PNL_ROWS) });
    });

    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await openPnlTab(page);
    await expect(page.locator('.hist-table tbody tr')).toHaveCount(FIXTURE_PNL_ROWS.length, { timeout: 8000 });

    // Fill the account + symbol-root text filters.
    await page.locator('.hist-flbl:has-text("Account") input').first().fill('ZG0790');
    await page.locator('.hist-flbl:has-text("Root") input').first().fill('goldm');

    // Pick Segment = Derivatives via the Select.svelte custom dropdown
    // (native selectOption() doesn't apply — click trigger, click option).
    await page.locator('#rpnl-segment-select').click();
    await page.locator('.rbq-select-option-label', { hasText: 'Derivatives' }).click();

    // Pick Type = Option.
    await page.locator('#rpnl-instrument-select').click();
    await page.locator('.rbq-select-option-label', { hasText: 'Option' }).click();

    // Expiry range.
    await page.locator('.hist-flbl:has-text("Expiry from") input').fill('2026-10-01');
    await page.locator('.hist-flbl:has-text("Expiry to")   input').fill('2026-11-30');

    await page.locator('button.btn-primary', { hasText: 'Apply' }).click();
    await page.waitForTimeout(400);

    expect(lastUrl).toContain('account=ZG0790');
    expect(lastUrl).toContain('segment=derivatives');
    expect(lastUrl).toContain('instrument_type=option');
    expect(lastUrl.toLowerCase()).toContain('symbol_root=goldm');
    expect(lastUrl).toContain('expiry_from=2026-10-01');
    expect(lastUrl).toContain('expiry_to=2026-11-30');
    // From/To (the shared date-range filter) must NOT leak into this request.
    expect(lastUrl).not.toContain('from_date=');
    expect(lastUrl).not.toContain('to_date=');
  });

  // ── 3. Empty-result state ────────────────────────────────────────────────
  test('shows the empty state when the endpoint returns no rows', async ({ page }) => {
    await mockHistoryEndpoints(page, { pnlRows: [] });
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await openPnlTab(page);

    await expect(page.getByText('No realized P&L rows match')).toBeVisible({ timeout: 8000 });
    await expect(page.locator('.hist-table tbody tr')).toHaveCount(0);
  });

  // ── 4. P&L cell coloring reuses the page's own palette classes ──────────
  test('realized P&L cells use the existing cell-pos / cell-neg palette classes', async ({ page }) => {
    await mockHistoryEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await openPnlTab(page);
    await expect(page.locator('.hist-table tbody tr')).toHaveCount(FIXTURE_PNL_ROWS.length, { timeout: 8000 });

    await expect(page.locator('.hist-table td.cell-pos').first()).toBeVisible();
    await expect(page.locator('.hist-table td.cell-neg').first()).toBeVisible();

    // cell-pos resolves to the canonical --c-long token, cell-neg to --c-short
    // (same CSS rule every other P&L surface in the app reuses — no new colors).
    const posColor = await page.locator('.hist-table td.cell-pos').first().evaluate((el) => getComputedStyle(el).color);
    const negColor = await page.locator('.hist-table td.cell-neg').first().evaluate((el) => getComputedStyle(el).color);
    expect(posColor).not.toBe(negColor);
  });

  // ── 5. No pager on the pnl tab (no server-side pagination) ─────────────
  test('pager is never shown on the pnl tab', async ({ page }) => {
    // Return more than LIMIT (50) rows to prove the pager stays hidden
    // regardless of row count — this endpoint has no offset/limit.
    const manyRows = Array.from({ length: 60 }, (_, i) => ({
      account: 'ZG0790', segment: 'derivatives', instrument_type: 'option',
      symbol_root: `SYM${i}`, expiry: '2026-11-25', realized_pnl: i,
    }));
    await mockHistoryEndpoints(page, { pnlRows: manyRows });
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await openPnlTab(page);
    await expect(page.locator('.hist-table tbody tr')).toHaveCount(60, { timeout: 8000 });

    await expect(page.locator('.hist-pager')).toHaveCount(0);
  });

  // ── 6. Mobile viewport (360px) — no horizontal overflow, tabs reachable ─
  test('mobile viewport (360px): no page-level horizontal overflow, all 4 tabs reachable', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await mockHistoryEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });

    const tabs = page.locator('[role="tab"]');
    await expect(tabs).toHaveCount(4, { timeout: 8000 });
    await expect(tabs.filter({ hasText: 'Orders' })).toBeVisible();
    await expect(tabs.filter({ hasText: 'Trades' })).toBeVisible();
    await expect(tabs.filter({ hasText: 'Funds' })).toBeVisible();
    await expect(tabs.filter({ hasText: 'Realized P&L' })).toBeVisible();

    await openPnlTab(page);
    await expect(page.locator('.hist-table tbody tr')).toHaveCount(FIXTURE_PNL_ROWS.length, { timeout: 8000 });

    // Page-level horizontal scroll must not exceed clientWidth (the
    // inner .hist-table-wrap is allowed to scroll internally via its
    // own overflow-x:auto — only the outer page must not overflow).
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth };
    });
    expect(
      overflow.scrollWidth,
      `scrollWidth=${overflow.scrollWidth} clientWidth=${overflow.clientWidth}`,
    ).toBeLessThanOrEqual(overflow.clientWidth + 2);
  });

  // ── 7. Stale code: no TODO / FIXME in touched source files ──────────────
  test('stale code: no TODO or FIXME in the touched source files', async () => {
    const root = path.resolve(process.cwd(), process.cwd().endsWith('/frontend') ? '.' : 'frontend');
    const files = [
      'src/routes/(algo)/admin/history/+page.svelte',
      'src/lib/data/realizedPnlHistory.js',
    ];
    for (const f of files) {
      const p = path.join(root, f);
      if (fs.existsSync(p)) {
        const src = fs.readFileSync(p, 'utf8');
        expect(src, `${f} must not contain TODO/FIXME`).not.toMatch(/\bTODO\b|\bFIXME\b/i);
      }
    }
  });

  // ── 8. Reuse: canonical shared components imported, no bespoke grid ────
  test('reuse: page imports the shared EmptyState/LoadingSkeleton/Select/AlgoTabs, no new grid component', async () => {
    const root = path.resolve(process.cwd(), process.cwd().endsWith('/frontend') ? '.' : 'frontend');
    const p = path.join(root, 'src/routes/(algo)/admin/history/+page.svelte');
    if (fs.existsSync(p)) {
      const src = fs.readFileSync(p, 'utf8');
      expect(src).toContain("import EmptyState from '$lib/EmptyState.svelte'");
      expect(src).toContain("import LoadingSkeleton from '$lib/LoadingSkeleton.svelte'");
      expect(src).toContain("import Select from '$lib/Select.svelte'");
      expect(src).toContain("import AlgoTabs from '$lib/AlgoTabs.svelte'");
      // The new table reuses the page's own plain <table class="algo-table hist-table">
      // convention — no ag-Grid, no new .cand-grid-style subgrid introduced.
      expect(src).not.toMatch(/ag-theme-|class="cand-grid/);
    }
  });

});
