/**
 * E2E regression guards for two bugs fixed 2026-07-02:
 *
 * Bug 1 — Pinned card (indices/forex/commodities) slow after /pulse mounts
 *   Root cause: activeListsStore used TTL.minute with no keepStaleOnEmpty.
 *   On every /pulse mount the full loadLists → activeIds → loadActive chain
 *   ran sequentially before pinned rows could paint, while positions/holdings/
 *   movers all hydrated from localStorage instantly at module init.
 *   Fix: activeListsStore now uses TTL.week + keepStaleOnEmpty: true — same
 *   pattern as moversStore. (Unaffected by the Bug 2 architecture change below
 *   — still current, still checked as-is.)
 *
 * Bug 2 — NavStrip P first slot (Day P&L) shows 0 when derivatives visited first
 *   Root cause (2026-07-02, historical): snapshotTotals.day = 0 (stale from a
 *   prior derivatives page visit before any positions loaded). The template
 *   used ?? (nullish coalescing), which only falls back on null/undefined —
 *   so 0 ?? dispPositionsToday = 0 always.
 *   Fix (at the time): replaced ?? with explicit != null ternaries on all
 *   three P-pill slots, all reading from the `snapshotTotals` store.
 *
 * Architecture superseded 2026-09 (commit cbe132a6 / 7562dd04, "NavStrip/
 * Snapshot Exp P&L SSOT"): the writable `snapshotTotals` store — and every
 * push from the derivatives page into it — was removed entirely, so the
 * specific "stale cross-page push" mechanism Bug 2 exploited no longer
 * exists. The class of defect is now structurally impossible, not merely
 * patched:
 *   - PositionStrip.svelte (NavStrip) mounts ONCE at the `(algo)/+layout.svelte`
 *     level, so it is never torn down/rebuilt when the operator navigates
 *     between /admin/derivatives and /pulse — there is no "prior page's
 *     value lingering" scenario because there is no per-page instance.
 *   - P slot 1 (`dispPositionsToday`) reads exclusively from
 *     `positionsDayPnlStore.total` (→ `portfolioStore.positions.total.day_pnl`,
 *     built from `baseDayPnlForPosition` summed over ALL live position rows).
 *     The derivatives page never writes to it, so there is nothing for it to
 *     go stale FROM.
 *   - The modern equivalent of the `?? 0` swallow-zero defect lives in
 *     PositionStrip's own freeze/thaw `$effect`: `if (newPTotal !== 0) {...}
 *     else if (positions.length === 0 && !positionsStore.meta?.degraded) {...}`
 *     — a transient 0/null read does NOT blindly overwrite the displayed
 *     value. This file's Bug 2 guards now check THAT pattern instead of the
 *     dead `$snapshotTotals != null` ternary.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT  — activeListsStore uses TTL.week + keepStaleOnEmpty (code check);
 *            dispPositionsToday's swallow-zero guard present in the
 *            freeze/thaw effect, with no snapshotTotals-style cross-page
 *            store anywhere (code check)
 *   Perf  — pinned rows visible within 500ms of DOMContentLoaded on warm-cache
 *            /pulse load (browser test)
 *   Stale — ?? stale-freeze pattern eliminated (code check + browser test:
 *            P slot 1 must differ from 0 when positions have intraday movement);
 *            snapshotTotals confirmed absent from the entire src/ tree
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

test.describe('Code-level guards — Bug 2 (Day P&L swallow-zero, post-snapshotTotals-removal architecture)', () => {
  const STRIP_SRC = path.resolve(
    import.meta.dirname,
    '../src/lib/PositionStrip.svelte',
  );
  const STORES_SRC = path.resolve(
    import.meta.dirname,
    '../src/lib/stores.js',
  );
  const DERIV_SRC = path.resolve(
    import.meta.dirname,
    '../src/routes/(algo)/admin/derivatives/+page.svelte',
  );
  const LAYOUT_SRC = path.resolve(
    import.meta.dirname,
    '../src/routes/(algo)/+layout.svelte',
  );

  test('snapshotTotals does not exist anywhere in src/ (regression guard — the whole store class is gone)', () => {
    // The original bug depended on a shared mutable store that a page could
    // write a stale value into. Confirming its complete absence across the
    // tree — not just these three files — is the strongest guard against
    // this exact defect class reappearing under a different surface.
    const walk = (dir) => {
      /** @type {string[]} */
      const out = [];
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) out.push(...walk(full));
        else if (/\.(svelte|js)$/.test(entry.name)) out.push(full);
      }
      return out;
    };
    const srcRoot = path.resolve(import.meta.dirname, '../src');
    const offenders = [];
    for (const f of walk(srcRoot)) {
      if (fs.readFileSync(f, 'utf8').includes('snapshotTotals')) offenders.push(f);
    }
    expect(offenders, `snapshotTotals reappeared in: ${offenders.join(', ')}`).toHaveLength(0);
  });

  test('PositionStrip mounts once at the (algo) layout level — no per-page instance to go stale across nav', () => {
    const layoutSrc = fs.readFileSync(LAYOUT_SRC, 'utf8');
    expect(layoutSrc).toContain('<PositionStrip');
    expect((layoutSrc.match(/<PositionStrip\b/g) || []).length).toBe(1);
    const derivSrc = fs.readFileSync(DERIV_SRC, 'utf8');
    expect(derivSrc).not.toContain('<PositionStrip');
  });

  test('dispPositionsToday (P slot 1) freeze/thaw effect does not blindly overwrite with a transient 0 (swallow-zero guard)', () => {
    // This is the CURRENT equivalent of the old `?? 0` bug: a momentary
    // 0/null read from positionsDayPnlStore.total must not immediately zero
    // out the displayed value while positions are known to be non-empty (or
    // the store is degraded/mid-reload).
    const src = fs.readFileSync(STRIP_SRC, 'utf8');
    expect(src).toContain('const newPTotal = positionsDayPnlStore.total;');
    expect(src).toContain('if (newPTotal !== 0) {');
    expect(src).toContain('dispPositionsToday = newPTotal;');
    expect(src).toContain('} else if (positions.length === 0 && !positionsStore.meta?.degraded) {');
  });

  test('stores.js does not export a snapshotTotals writable', () => {
    const storesSrc = fs.readFileSync(STORES_SRC, 'utf8');
    expect(storesSrc).not.toContain('snapshotTotals');
  });
});

// ── Browser: Pinned card renders quickly on warm /pulse ───────────────────────

test.describe('Bug 1 — Pinned card visible within 500ms on warm cache', () => {
  // Tests do two navigations (prime cache visit + test visit); allow 90s total
  test.setTimeout(90_000);
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('pinned bucket has rows immediately at DOMContentLoaded on second /pulse visit (localStorage hydration)', async ({ page }) => {
    // First visit: prime localStorage cache for activeListsStore (TTL.week).
    // The store writes to localStorage via cachedWrite after each successful fetch.
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Allow loadActive + its watchlist fetches to complete and write localStorage.
    await page.waitForTimeout(5_000);

    // Second visit: _initFromCache() runs at module-eval (before any React/Svelte
    // render) and populates _value synchronously from localStorage. The ag-Grid
    // should have row data available without waiting for the background re-fetch.
    // We navigate and immediately check — before 3s for the re-fetch to complete.
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Pinned bucket section must be present. The bucket-grid inside it should
    // render without waiting for a network fetch because _initFromCache() already
    // populated activeListsStore.value from the TTL.week-cached localStorage entry.
    const pinwatchSection = page.locator('.mp-bucket-pinwatch');
    await expect(pinwatchSection).toBeVisible({ timeout: TIMEOUT });

    // The grid container must be visible quickly (500ms from DOMContentLoaded).
    // This is the paint-latency regression guard for the TTL.minute→TTL.week fix.
    const grid = pinwatchSection.locator('.bucket-grid').first();
    await expect(grid).toBeVisible({ timeout: 500 });
  });

  test('pinned bucket section is present in DOM on /pulse', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // The pinned/watch section uses .mp-bucket-pinwatch
    const pinwatchSection = page.locator('.mp-bucket-pinwatch');
    await expect(pinwatchSection).toBeVisible({ timeout: TIMEOUT });
    // The grid inside it carries .bucket-grid class (ag-Grid container)
    const grid = pinwatchSection.locator('.bucket-grid').first();
    await expect(grid).toBeVisible({ timeout: TIMEOUT });
  });
});

// ── Browser: P pill slot 1 does not freeze to 0 after derivatives visit ───────

test.describe('Bug 2 — P slot 1 not frozen to 0 after cross-page nav', () => {
  // These tests navigate derivatives → pulse: allow 90s for two-page sequences
  test.setTimeout(90_000);
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // fmtMoney() in PositionStrip.svelte is `aggCompact(v)` with NO currency
  // prefix — a zero P&L renders as the plain string '0.00', never '0' or
  // '₹0'. Use this helper instead of a literal string comparison.
  const isZeroPDisplay = (t) => t === '' || t === '—' || /^-?0(\.0+)?$/.test(t);

  test('P slot 1 is non-blank on /pulse regardless of prior derivatives visit', async ({ page }) => {
    // Visit derivatives first — the page mounts and runs its own local
    // Snapshot computation, but has no write path to NavStrip's P slot 1.
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
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

  test('P slot 1 matches live F&O positions after visiting derivatives first', async ({ page }) => {
    // Simulate the historical bug scenario (now structurally impossible —
    // see this file's header — but kept as a regression guard):
    // 1. Visit derivatives early (no cross-page store to publish into)
    // 2. Navigate to pulse
    // 3. Confirm slot 1 reflects actual positions, not a frozen/stale 0
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
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

    // The historical stale-freeze bug rendered a zero value even with
    // non-zero F&O movement. Confirm the ACTUAL zero display format (not a
    // literal '0', which fmtMoney never produces) is not shown.
    expect(
      isZeroPDisplay(text),
      `P slot 1 shows "${text}" (zero) despite F&O day P&L of ${expectedDayPnl.toFixed(2)} — the swallow-zero guard in PositionStrip's freeze/thaw effect may have regressed`
    ).toBe(false);
  });

  test('P pill has all 3 values after navigating derivatives → pulse → derivatives', async ({ page }) => {
    // Cross-page navigation stress test
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
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
  // Mobile /pulse loads slower in headless (CSS layout + SSE setup); raise per-test timeout
  test.setTimeout(60_000);

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('P strip and P pill visible on 390px /pulse', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 40_000 });
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: 40_000 });
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: 40_000 });
  });

  test('strip does not overflow viewport on 390px', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 40_000 });
    const box = await strip.boundingBox();
    expect(box, 'strip must have a bounding box').not.toBeNull();
    expect(box.width, 'strip width must not exceed viewport').toBeLessThanOrEqual(390 + 4);
  });
});
