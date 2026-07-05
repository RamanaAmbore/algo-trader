/**
 * navstrip_p_slot_derivatives.spec.js
 *
 * Regression guard: NavStrip P pill slot 1 ("today" Day P&L) must NOT
 * drop to 0 when /admin/derivatives is opened, and must recover after
 * navigating away from it.
 *
 * Root cause (fixed 2026-07-04):
 *   derivatives/+page.svelte wrote to the shared `snapshotTotals` store
 *   inside a `$effect` that ran BEFORE the first `loadPositions()` call
 *   completed.  At that point `positions = []`, so all three derived
 *   totals were 0.  PositionStrip reads `$snapshotTotals.day` when the
 *   store is non-null, so it displayed 0 instead of the real intraday
 *   P&L.  Additionally, `onDestroy` never cleared the store, so the
 *   filtered F&O-only value lingered on subsequent pages.
 *
 * Fix: gate the `$effect` publish on `_positionsLoaded`; clear the
 * store to null in `onDestroy`.
 *
 * Five quality dimensions:
 * 1. SSOT   — `snapshotTotals` has exactly one non-null write site
 *             (derivatives page $effect, guarded by _positionsLoaded).
 * 2. Perf   — no new long-task (>200 ms) introduced during route
 *             transitions that touch the derivatives page.
 * 3. Stale  — grep confirms no second non-null write site outside
 *             the derivatives page.
 * 4. Reuse  — derivatives page imports `livePositionDayPnl` (which
 *             wraps `baseDayPnlForPosition`) rather than re-implementing
 *             the Day P&L formula inline.
 * 5. UX     — P slot 1 has a direction class (ps-pos/ps-neg/ps-flat)
 *             and is never blank when visible.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';
import path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || process.env.BASE_URL || 'http://localhost:5174';

// Resolve project root from this spec's location (e2e/ → frontend/ → project root).
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.join(__dirname, '..', 'src');

// ── Dimension 3 + 4: static file checks (no browser needed) ───────────────

test.describe('snapshotTotals static guards', () => {
  test('snapshotTotals has exactly one non-null write site (derivatives page $effect)', () => {
    // Count lines containing `snapshotTotals.set({` (the data-publishing call).
    // The `set(null)` in onDestroy is intentional cleanup — it is NOT a
    // competing write site, so we grep for set({ specifically.
    const hits = execSync(
      `grep -r "snapshotTotals\\.set({" "${FRONTEND_SRC}" --include="*.svelte" --include="*.js" -l`,
      { encoding: 'utf8' },
    ).trim().split('\n').filter(Boolean);

    // stores.js exports the writable but never writes to it.
    const writers = hits.filter(f => !f.endsWith('stores.js'));
    expect(writers).toHaveLength(1);
    expect(writers[0]).toContain(path.join('admin', 'derivatives'));
  });

  test('derivatives page imports livePositionDayPnl (wraps baseDayPnlForPosition — SSOT)', () => {
    const derivFile = path.join(
      __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
    );
    const content = readFileSync(derivFile, 'utf8');
    // livePositionDayPnl wraps baseDayPnlForPosition — importing either satisfies SSOT.
    const hasImport = content.includes('baseDayPnlForPosition') || content.includes('livePositionDayPnl');
    expect(hasImport).toBe(true);
  });

  test('derivatives onDestroy clears snapshotTotals to null', () => {
    const derivFile = path.join(
      __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
    );
    const content = readFileSync(derivFile, 'utf8');
    // The onDestroy block must contain snapshotTotals.set(null) to release
    // the strip so it falls back to its own computed value after nav-away.
    expect(content).toContain('snapshotTotals.set(null)');
  });

  test('$effect that publishes snapshotTotals is guarded by _positionsLoaded', () => {
    const derivFile = path.join(
      __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
    );
    const content = readFileSync(derivFile, 'utf8');
    // Check that the guard exists in the same effect block as the set({).
    // Strategy: find the set({ call and verify _positionsLoaded appears
    // within the surrounding 30 lines (the effect is short).
    const setIdx = content.indexOf('snapshotTotals.set({');
    expect(setIdx).toBeGreaterThan(0);
    const surrounding = content.slice(Math.max(0, setIdx - 600), setIdx + 200);
    expect(surrounding).toContain('_positionsLoaded');
  });
});

// ── Dimension 1 + 2 + 5: browser tests ────────────────────────────────────

/** @type {string | null} */
let _sharedJwt = null;

test.describe('NavStrip P slot 1 — derivatives page regression', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    try {
      const result = await loginAsAdmin(page);
      _sharedJwt = result.token;
    } catch (e) {
      // Server unreachable in offline CI — skip browser tests gracefully.
      _sharedJwt = null;
    } finally {
      await page.close();
    }
  });

  /** Inject JWT so each test skips the login round-trip. */
  async function seedToken(page) {
    if (!_sharedJwt) return;
    await page.context().addInitScript((t) => {
      sessionStorage.setItem('ramboq_token', t);
    }, _sharedJwt);
  }

  /**
   * Locate the P pill's first value span (slot 1 = today Day P&L).
   * Structure: <span class="ps-agg" title="Positions: ...">
   *              <span class="ps-agg-k">P</span>
   *              <span class="ps-agg-v ...">TODAY</span>  ← slot 1
   *              <span class="ps-agg-sep">/</span>
   *              ...
   */
  function getPSlot1(page) {
    return page
      .locator('.ps-agg')
      .filter({ has: page.locator('.ps-agg-k', { hasText: /^P$/ }) })
      .locator('.ps-agg-v')
      .first();
  }

  test('P slot 1 stays equal to baseline after /admin/derivatives opens', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // ── 1. Baseline on /pulse ──────────────────────────────────────
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 15_000 });

    // Let the strip hydrate at least one poll cycle.
    await page.waitForTimeout(500);
    const baselineText = (await slot1.textContent())?.trim() ?? '';

    // ── 2. Navigate to /admin/derivatives ─────────────────────────
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // Wait for loadPositions to complete (the Snapshot section renders
    // only after _positionsLoaded = true, which gates the $effect).
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    // One extra tick to let Svelte flush the $effect.
    await page.waitForTimeout(300);

    const derivText = (await slot1.textContent())?.trim() ?? '';

    // Core assertion: value must not drop to 0 unless baseline was also 0.
    if (baselineText !== '₹0') {
      expect(derivText).not.toBe('₹0');
    }
    // And the values must match (same data source).
    expect(derivText).toBe(baselineText);
  });

  test('P slot 1 has direction class (not bare text) on /orders after leaving derivatives', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // Start on derivatives so the $effect + onDestroy both run.
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(300);

    // Navigate away — onDestroy fires → snapshotTotals.set(null).
    await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle' });
    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // UX dimension: must carry exactly one direction class.
    const cls = await slot1.getAttribute('class');
    expect(cls).toMatch(/ps-pos|ps-neg|ps-flat/);

    // Non-blank.
    const txt = (await slot1.textContent())?.trim();
    expect(txt).toBeTruthy();
  });

  test('no long-task >200 ms during pulse → derivatives → orders nav', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    await page.addInitScript(() => {
      window.__longTasks = [];
      const obs = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__longTasks.push(e.duration);
      });
      obs.observe({ type: 'longtask', buffered: true });
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(300);

    const tasks = await page.evaluate(() => window.__longTasks ?? []);
    const worstMs = tasks.length ? Math.max(...tasks) : 0;
    // Budget: 200 ms (allows SvelteKit route-transition overhead; regression
    // threshold sits well above the O(1) guard we added).
    expect(worstMs).toBeLessThan(200);
  });
});
