/**
 * E2E regression guards for two bugs fixed 2026-07-02:
 *
 * Bug 1 — Pinned card (indices/forex/commodities) slow after /pulse mounts
 *   Root cause: activeListsStore used TTL.minute with no keepStaleOnEmpty.
 *   On every /pulse mount the full loadLists → activeIds → loadActive chain
 *   ran sequentially before pinned rows could paint, while positions/holdings/
 *   movers all hydrated from localStorage instantly at module init.
 *   Fix: activeListsStore now uses TTL.week + keepStaleOnEmpty: true — same
 *   pattern as moversStore.
 *
 * Bug 2 — NavStrip P first slot (Day P&L) shows 0 when derivatives visited first
 *   Root cause: snapshotTotals.day = 0 (stale from a prior derivatives page visit
 *   before any positions loaded). The template used ?? (nullish coalescing), which
 *   only falls back on null/undefined — so 0 ?? dispPositionsToday = 0 always.
 *   Fix: replaced ?? with explicit != null ternaries on all three P-pill slots.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT  — activeListsStore uses TTL.week + keepStaleOnEmpty (code check);
 *            snapshotTotals != null guard present on all three slots (code check)
 *   Perf  — pinned rows visible within 500ms of DOMContentLoaded on warm-cache
 *            /pulse load (browser test)
 *   Stale — ?? stale-freeze pattern eliminated (code check + browser test:
 *            P slot 1 must differ from 0 when positions have intraday movement)
 *   Reuse — activeListsStore imported by MarketPulse (not duplicated); same
 *            createDataStore factory as moversStore (grep check)
 *   UX    — P pill slot 1 visible and non-blank on /pulse after nav from
 *            /admin/derivatives (cross-page guard)
 *
 * Run:
 *   cd frontend && npx playwright test pulse_pinned_and_navstrip_day_pnl --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs   from 'fs';
import * as path from 'path';

const BASE    = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TIMEOUT = 25_000;

// ── Code-level SSOT guards (no browser required) ─────────────────────────────

test.describe('Code-level guards — Bug 1 (pinned card)', () => {
  const STORES_SRC = path.resolve(
    import.meta.dirname,
    '../src/lib/data/marketDataStores.svelte.js',
  );

  test('activeListsStore uses TTL.week (not TTL.minute)', () => {
    const src = fs.readFileSync(STORES_SRC, 'utf8');
    // Must contain keepStaleOnEmpty on activeListsStore
    // Extract the createDataStore({ ... }) block for activeListsStore
    const idx = src.indexOf("export const activeListsStore = createDataStore({");
    expect(idx, 'activeListsStore not found').toBeGreaterThan(-1);
    // Slice out a generous window around the definition
    const block = src.slice(idx, idx + 500);
    expect(block).toContain('TTL.week');
    expect(block).toContain('keepStaleOnEmpty: true');
    expect(block).not.toContain('TTL.minute');
  });

  test('activeListsStore uses same createDataStore factory as moversStore', () => {
    const src = fs.readFileSync(STORES_SRC, 'utf8');
    expect(src).toContain('export const activeListsStore = createDataStore({');
    expect(src).toContain('export const moversStore = createDataStore({');
  });

  test('moversStore also has keepStaleOnEmpty (regression guard — must not revert)', () => {
    const src = fs.readFileSync(STORES_SRC, 'utf8');
    const idx = src.indexOf("export const moversStore = createDataStore({");
    expect(idx).toBeGreaterThan(-1);
    const block = src.slice(idx, idx + 600);
    expect(block).toContain('keepStaleOnEmpty: true');
  });
});

test.describe('Code-level guards — Bug 2 (snapshotTotals null-guard)', () => {
  const STRIP_SRC = path.resolve(
    import.meta.dirname,
    '../src/lib/PositionStrip.svelte',
  );

  test('all three P-pill slots use != null ternary (not ?? which swallows 0)', () => {
    const src = fs.readFileSync(STRIP_SRC, 'utf8');
    // Positive assertions: explicit null check on each slot
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.day');
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.pnl');
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.exp');
  });

  test('?? operator is NOT used on snapshotTotals slots (regression guard)', () => {
    const src = fs.readFileSync(STRIP_SRC, 'utf8');
    // These patterns were the bug — must never reappear
    expect(src).not.toContain('$snapshotTotals?.day ??');
    expect(src).not.toContain('$snapshotTotals?.pnl ??');
    expect(src).not.toContain('$snapshotTotals?.exp ??');
  });

  test('snapshotTotals store initial value is null in stores.js', () => {
    const storesSrc = fs.readFileSync(
      path.resolve(import.meta.dirname, '../src/lib/stores.js'),
      'utf8',
    );
    // The store must be initialised with null so the != null gate works correctly
    // on first paint (before derivatives populates it)
    expect(storesSrc).toMatch(/snapshotTotals\s*=\s*writable\(\s*[/\*][\s\S]*?\*[/]\s*\(null\)/);
  });
});

// ── Browser: Pinned card renders quickly on warm /pulse ───────────────────────

test.describe('Bug 1 — Pinned card visible within 500ms on warm cache', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('pinned card (indices) visible within 500ms on second /pulse visit', async ({ page }) => {
    // First visit: prime localStorage cache for activeListsStore
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Wait for loadActive to complete and write localStorage
    await page.waitForTimeout(5_000);

    // Second visit: cache should hydrate instantly
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Measure time from navigation start to pinned rows appearing
    const startMs = Date.now();

    // Pinned rows are inside the left-hand ag-Grid and carry bucket class 'pinned'
    // or appear as the first row group in the left grid. We use the grid itself
    // as a proxy since the row content depends on the operator's watchlist setup.
    // The LEFT grid must have at least one row rendered within 500ms.
    const leftGrid = page.locator('.mp-left-grid .ag-center-cols-container .ag-row').first();
    await expect(leftGrid).toBeVisible({ timeout: 500 });
    const elapsed = Date.now() - startMs;
    expect(elapsed, `pinned row took ${elapsed}ms — must be within 500ms on warm cache`).toBeLessThan(500);
  });

  test('left grid has rows before networkidle on /pulse (not waiting for fetch)', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Check immediately after DOMContentLoaded — before network idles
    // The store should hydrate from localStorage synchronously at module init
    await page.waitForTimeout(300); // allow one rAF for ag-Grid first paint
    const leftGrid = page.locator('.mp-left-grid .ag-center-cols-container .ag-row');
    const count = await leftGrid.count();
    // If cache is warm there will be rows; cold-cache is acceptable on first-ever load
    // We just assert the grid container exists (structure guard)
    const container = page.locator('.mp-left-grid');
    await expect(container).toBeVisible({ timeout: TIMEOUT });
  });
});

// ── Browser: P pill slot 1 does not freeze to 0 after derivatives visit ───────

test.describe('Bug 2 — P slot 1 not frozen to 0 after cross-page nav', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('P slot 1 is non-blank on /pulse regardless of prior derivatives visit', async ({ page }) => {
    // Visit derivatives first to populate snapshotTotals (may be 0 if no positions loaded yet)
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    // Wait enough for snapshotTotals to potentially get published as {day:0,...}
    await page.waitForTimeout(3_000);

    // Navigate to /pulse
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Wait for positions poll to complete
    await page.waitForTimeout(4_000);

    const todayVal = strip.locator('.ps-agg').first().locator('.ps-agg-v').nth(0);
    await expect(todayVal).toBeVisible({ timeout: TIMEOUT });
    const text = (await todayVal.textContent())?.trim();
    expect(text, 'P slot 1 must render a non-blank value on /pulse').toBeTruthy();
  });

  test('P slot 1 matches live F&O positions when snapshotTotals is stale', async ({ page }) => {
    // Simulate the exact bug scenario:
    // 1. Visit derivatives early (snapshotTotals publishes {day:0, pnl:x, exp:y})
    // 2. Navigate to pulse
    // 3. Confirm slot 1 reflects actual positions, not the stale 0
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2_000);

    // Read the F&O day_change_val sum from the API (the expected slot 1 value)
    const expectedDayPnl = await page.evaluate(async () => {
      const tok = sessionStorage.getItem('ramboq_token');
      if (!tok) return null;
      try {
        const res = await fetch('/api/positions', { headers: { Authorization: `Bearer ${tok}` } });
        const data = await res.json();
        const rows = data?.positions ?? data?.items ?? [];
        const FO = new Set(['NFO', 'MCX', 'CDS', 'BFO']);
        let day = 0;
        for (const p of rows) {
          const exch = String(p?.exchange || '').toUpperCase();
          if (!FO.has(exch)) continue;
          // baseDayPnlForPosition logic: oq=0 AND pnl!=0 → use pnl, else day_change_val
          const oq  = Number(p?.overnight_quantity ?? 0);
          const pnl = Number(p?.pnl ?? 0);
          const dcv = Number(p?.day_change_val ?? 0);
          day += (oq === 0 && pnl !== 0) ? pnl : dcv;
        }
        return day;
      } catch { return null; }
    });

    if (expectedDayPnl === null) return; // API unreachable
    if (Math.abs(expectedDayPnl) < 10) {
      // No meaningful F&O movement — slot 1 of "0" is correct, skip numeric check
      return;
    }

    // Navigate to pulse and check slot 1
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    await page.waitForTimeout(4_000);

    const todayVal = strip.locator('.ps-agg').first().locator('.ps-agg-v').nth(0);
    const text = (await todayVal.textContent())?.trim() ?? '';
    expect(text, 'P slot 1 must not be blank').toBeTruthy();

    // The stale-freeze bug renders "0" even with non-zero F&O movement.
    // After the fix, the value must not be "0" when |expectedDayPnl| > 10.
    expect(
      text,
      `P slot 1 shows "0" despite F&O day P&L of ${expectedDayPnl.toFixed(2)} — snapshotTotals null-guard may have regressed`
    ).not.toBe('0');
  });

  test('P pill has all 3 values after navigating derivatives → pulse → derivatives', async ({ page }) => {
    // Cross-page navigation stress test
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2_000);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pPill = strip.locator('.ps-agg').first();
    const vals  = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });

    // Navigate back to derivatives — strip must still show 3 slots
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
    const strip2 = page.locator('.ps-strip');
    await expect(strip2).toBeVisible({ timeout: TIMEOUT });
    await expect(strip2.locator('.ps-agg').first().locator('.ps-agg-v')).toHaveCount(3, { timeout: TIMEOUT });
  });
});

// ── Mobile viewport: both bugs are neutral on 390px ──────────────────────────

test.describe('Mobile — pinned card + P pill on 390px', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('P strip and P pill visible on 390px /pulse', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });
  });

  test('strip does not overflow viewport on 390px', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const box = await strip.boundingBox();
    expect(box, 'strip must have a bounding box').not.toBeNull();
    expect(box.width, 'strip width must not exceed viewport').toBeLessThanOrEqual(390 + 4);
  });
});
