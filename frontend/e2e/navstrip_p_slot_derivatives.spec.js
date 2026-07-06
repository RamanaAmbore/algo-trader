/**
 * navstrip_p_slot_derivatives.spec.js
 *
 * Regression guard: NavStrip P pill slot 1 ("today" Day P&L) must NOT
 * drop to 0 when /admin/derivatives is opened, when the user picks a
 * different underlying in the picker, or after navigating away.
 *
 * Root causes fixed:
 *
 *   2026-07-04 (mount-time zero):
 *   derivatives/+page.svelte wrote to the shared `snapshotTotals` store
 *   inside a `$effect` that ran BEFORE the first `loadPositions()` call
 *   completed.  At that point `positions = []`, so all three derived
 *   totals were 0.  PositionStrip reads `$snapshotTotals.day` when the
 *   store is non-null, so it displayed 0 instead of the real intraday
 *   P&L.  Additionally, `onDestroy` never cleared the store, so the
 *   filtered F&O-only value lingered on subsequent pages.
 *   Fix: gate the `$effect` publish on `_positionsLoaded`; clear the
 *   store to null in `onDestroy`.
 *
 *   2026-07-04 (symbol-select zero — dead code SSOT violation):
 *   `_byUnderlyingDay` was a `$derived.by()` that read raw
 *   `p.day_change_val` without routing through `baseDayPnlForPosition`.
 *   It was never wired to any consumer in the template but was a latent
 *   SSOT violation.  Removed in refactor(derivatives) commit.
 *
 * Five quality dimensions:
 * 1. SSOT   — `snapshotTotals` has exactly one non-null write site
 *             (derivatives page $effect, guarded by _positionsLoaded);
 *             `_byUnderlyingDay` dead-code with SSOT violation is absent.
 * 2. Perf   — no new long-task (>200 ms) introduced during route
 *             transitions that touch the derivatives page.
 * 3. Stale  — grep confirms no second non-null write site outside
 *             the derivatives page; `_byUnderlyingDay` fully removed.
 * 4. Reuse  — derivatives page imports `livePositionDayPnl` (which
 *             wraps `baseDayPnlForPosition`) rather than re-implementing
 *             the Day P&L formula inline.
 * 5. UX     — P slot 1 has a direction class (ps-pos/ps-neg/ps-flat),
 *             is never blank when visible, and does NOT drop to ₹0
 *             when the operator changes the underlying picker selection.
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

  test('_byUnderlyingDay dead-code SSOT-violation is fully removed from derivatives page', () => {
    // _byUnderlyingDay was a $derived.by() that read raw p.day_change_val
    // without routing through baseDayPnlForPosition — an SSOT violation.
    // It was never consumed by any template expression, making it dead code.
    // This guard ensures it is not re-introduced.
    const derivFile = path.join(
      __dirname, '..', 'src', 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte',
    );
    const content = readFileSync(derivFile, 'utf8');
    // The identifier must not appear as a declaration (= $derived.by).
    // A comment reference is acceptable only if it doesn't declare or assign.
    const declarationPattern = /const\s+_byUnderlyingDay\s*=/;
    expect(declarationPattern.test(content)).toBe(false);
    // The identifier must not appear in any template expression.
    const templatePattern = /\{[^}]*_byUnderlyingDay[^}]*\}/;
    expect(templatePattern.test(content)).toBe(false);
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

  test('P slot 1 does NOT drop to ₹0 when underlying picker changes symbol', async ({ page }) => {
    test.skip(!_sharedJwt, 'Server unreachable — skipping browser test');

    await seedToken(page);

    // ── 1. Open derivatives and wait for positions to load ─────────
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
    // Extra tick so the gated $effect publishes snapshotTotals.
    await page.waitForTimeout(500);

    const slot1 = getPSlot1(page);
    await expect(slot1).toBeVisible({ timeout: 5_000 });

    // Capture BOTH text and direction class BEFORE picking.
    // fmtMoney(0) returns '₹0.00' (not '₹0'), so we compare the
    // actual rendered text string directly rather than guessing format.
    const beforePick = (await slot1.textContent())?.trim() ?? '';
    const beforeCls  = await slot1.getAttribute('class') ?? '';
    // Extract only the direction token (ps-pos / ps-neg / ps-flat).
    const beforeDir  = (beforeCls.match(/\bps-(?:pos|neg|flat)\b/) || ['ps-flat'])[0];

    // ── 2. Open the Underlying picker and pick the SECOND option ──
    // Select renders the id prop directly on the <button class="rbq-select-trigger">
    // so #opt-und IS the trigger button — no descendant selector needed.
    // Options live in a sibling .rbq-select-panel inside .opt-und-row.
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    await trigger.click();

    // Wait for the panel to open (options list visible).
    const options = page.locator('.opt-und-row .rbq-select-option');
    await expect(options.first()).toBeVisible({ timeout: 3_000 });
    const count = await options.count();

    // Skip test gracefully when the picker has fewer than 2 options
    // (no live F&O book + watchlist too small).
    if (count < 2) {
      test.skip(true, 'Picker has < 2 underlying options — skipping symbol-select regression');
      return;
    }

    // Pick the second option (index 1) — different from whatever was
    // auto-selected on load.
    await options.nth(1).click();

    // Allow the Svelte reactive graph to flush: _dayPnlByRootMap re-derives
    // → _snapshotTotalDay re-derives → $effect publishes snapshotTotals.
    await page.waitForTimeout(400);

    // ── 3. Core assertion: both text AND direction class must be stable ──
    const afterPick = (await slot1.textContent())?.trim() ?? '';
    const afterCls  = await slot1.getAttribute('class') ?? '';
    const afterDir  = (afterCls.match(/\bps-(?:pos|neg|flat)\b/) || ['ps-flat'])[0];

    // Text must not change (symbol-select is view-only; _dayPnlByRootMap
    // sums ALL positions, not just the selected underlying).
    expect(afterPick).toBe(beforePick);

    // Direction class must not change — this is the core regression check.
    // Previously, picking a symbol could cause $snapshotTotals.day to
    // briefly flip to 0, turning ps-pos/ps-neg into ps-flat.
    expect(afterDir).toBe(beforeDir);

    // Sanity: a direction class must always be present.
    expect(afterDir).toMatch(/^ps-(?:pos|neg|flat)$/);
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
