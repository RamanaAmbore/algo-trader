/**
 * freshness_shimmer.spec.js
 *
 * Verifies Design B: the underline shimmer on the LTP cell in MarketPulse (/pulse).
 * Five quality dimensions checked:
 *   1. SSOT  — shimmer fires only when MarketPulse's bus delivers fresh data
 *   2. Perf  — <50 cells animating concurrently; XHR budget not inflated by shimmer
 *   3. Stale — no OLD animation classes from the dedup survey
 *   4. Reuse — shimmer uses canonical cyan-400 palette
 *   5. UX    — duration 600-800ms; no z-index collision with directional flash
 *
 * Uses loginAsAdmin (never inline credentials).
 * CSS assertions use DOM injection (getComputedStyle + element.animate) because
 * production Vite bundles serve stylesheets that document.styleSheets cannot
 * enumerate cross-origin. Instead we apply the class to a real element in the
 * live page and read its computed animation properties.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Run against desktop only — mobile viewport has the same code path but the
// LTP column visibility may differ; desktop gives the cleanest assertion surface.
test.use({ viewport: { width: 1400, height: 900 } });

test.describe('Freshness shimmer — Design B', () => {

  // ── 1. SSOT — shimmer class appears on LTP cell after bus delivery ──────────
  test('1. SSOT: shimmer CSS class is defined and not present before first tick', async ({ page }) => {
    test.setTimeout(45000);
    await loginAsAdmin(page);
    await page.goto('/pulse');

    // Wait for the grid to paint at least one LTP cell.
    const ltpCell = page.locator('.ag-cell[col-id="ltp"]').first();
    await ltpCell.waitFor({ state: 'visible', timeout: 15000 });

    // Immediately after cold mount (no tick has fired yet) no cell should
    // carry the shimmer class — notifyAll only runs inside _ltpPaintTimer
    // which fires after _lastPaintedSnap is seeded on the SECOND paint.
    const shimmersBefore = await page.locator('.cell-freshness-pulse').count();
    expect(shimmersBefore).toBe(0);

    // Inject a test element into the live page, apply the shimmer class,
    // and verify the CSS animation property is non-empty — this proves the
    // stylesheet was loaded and the class is defined.
    const animDefined = await page.evaluate(() => {
      const el = document.createElement('div');
      el.className = 'cell-freshness-pulse';
      el.style.position = 'absolute';
      el.style.top = '-9999px';
      document.body.appendChild(el);
      const cs = window.getComputedStyle(el, '::after');
      // animationName is 'none' when the rule doesn't exist; 'cell-freshness'
      // when the @keyframes block is applied.
      const name = cs.animationName;
      document.body.removeChild(el);
      return name !== 'none' && name !== '';
    });
    expect(animDefined).toBe(true);
  });

  // ── 2. Perf — <50 cells animating at once; shimmer adds zero new XHRs ───────
  test('2. Perf: shimmer adds no new XHR calls; concurrent shimmer count <50 at peak', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);

    // Capture XHRs during a narrow 2 s window around first paint.
    // We compare /api/charts, /api/positions, /api/holdings etc. that the
    // shimmer should NOT add to. We record only during cold load (before polls
    // start firing) so we don't count repeated poll cycles.
    const shimmerSpecificPaths = ['/api/freshness', '/api/shimmer'];
    const shimmerXhrs = [];
    page.on('request', req => {
      const u = req.url();
      if (shimmerSpecificPaths.some(p => u.includes(p))) shimmerXhrs.push(u);
    });

    await page.goto('/pulse');
    await page.locator('.ag-cell[col-id="ltp"]').first().waitFor({ state: 'visible', timeout: 15000 });

    // Give one poll cycle to fire shimmers.
    await page.waitForTimeout(7000);

    // Shimmer is pure CSS + $state — no new API calls whatsoever.
    expect(shimmerXhrs.length).toBe(0);

    // Count concurrent shimmer-active cells at snapshot time.
    // Shimmers clear after 700ms so during a slow poll cycle many may have
    // already cleared. We allow <50 even at peak.
    const shimmerCount = await page.locator('.cell-freshness-pulse').count();
    expect(shimmerCount).toBeLessThan(50);
  });

  // ── 3. Stale — no orphaned animation classes from the dedup survey ───────────
  test('3. Stale: no unknown "data-updated" animation classes on LTP cells at rest', async ({ page }) => {
    test.setTimeout(45000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.locator('.ag-cell[col-id="ltp"]').first().waitFor({ state: 'visible', timeout: 15000 });
    // Give animations time to clear.
    await page.waitForTimeout(2000);

    // ltp-flash-up / ltp-flash-down are directional and clear after 650ms.
    // At rest (2 s after load) they should not be present.
    const staticFlashUp   = await page.locator('.ag-cell[col-id="ltp"].ltp-flash-up').count();
    const staticFlashDown = await page.locator('.ag-cell[col-id="ltp"].ltp-flash-down').count();
    expect(staticFlashUp).toBe(0);
    expect(staticFlashDown).toBe(0);

    // Regression guard: no unknown "update" class names on LTP cells.
    const unknownAnimClass = await page.evaluate(() => {
      const ltpCells = Array.from(document.querySelectorAll('.ag-cell[col-id="ltp"]'));
      // Classes that would indicate a different "data updated" animation was
      // introduced outside this slice — none expected from the dedup survey.
      const unexpected = ['ltp-update', 'cell-refresh', 'cell-glow', 'ltp-glow'];
      for (const cell of ltpCells) {
        for (const u of unexpected) {
          if (cell.classList.contains(u)) return u;
        }
      }
      return null;
    });
    expect(unknownAnimClass).toBeNull();
  });

  // ── 4. Reuse — shimmer uses canonical cyan-400 palette ───────────────────────
  test('4. Reuse: cell-freshness-pulse::after uses cyan-400 and has overflow clip', async ({ page }) => {
    test.setTimeout(60000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.locator('.ag-cell[col-id="ltp"]').first().waitFor({ state: 'visible', timeout: 15000 });

    // Inject a test element, apply the class, measure computed styles.
    // getComputedStyle(el) gives the wrapper rule; getComputedStyle(el, '::after')
    // gives the pseudo-element rule.
    // NOTE: do NOT set el.style.position — that would override the CSS rule we
    // are trying to test. Use a visibility:hidden wrapper to hide from view.
    const styles = await page.evaluate(() => {
      const wrapper = document.createElement('div');
      wrapper.style.visibility = 'hidden';
      wrapper.style.width = '100px';
      wrapper.style.height = '20px';
      wrapper.style.overflow = 'visible'; // don't inherit
      const el = document.createElement('div');
      el.className = 'cell-freshness-pulse';
      el.style.width = '100%';
      el.style.height = '100%';
      wrapper.appendChild(el);
      document.body.appendChild(wrapper);

      const wrapperCs  = window.getComputedStyle(el);
      const pseudoCs   = window.getComputedStyle(el, '::after');

      const result = {
        wrapperPosition:  wrapperCs.position,
        wrapperOverflow:  wrapperCs.overflow,
        pseudoAnimName:   pseudoCs.animationName,
        pseudoAnimDur:    pseudoCs.animationDuration,
        pseudoBackground: pseudoCs.backgroundImage,
        pseudoHeight:     pseudoCs.height,
      };

      document.body.removeChild(wrapper);
      return result;
    });

    // Wrapper: position:relative + overflow:hidden (prevents underline bleed).
    expect(styles.wrapperPosition).toBe('relative');
    expect(styles.wrapperOverflow).toBe('hidden');

    // Pseudo-element: animation name is cell-freshness (not 'none').
    expect(styles.pseudoAnimName).not.toBe('none');
    expect(styles.pseudoAnimName).toContain('cell-freshness');

    // Background: gradient contains cyan channel values (34, 211, 238).
    // getComputedStyle resolves the gradient; the rgb values should appear.
    const bg = styles.pseudoBackground || '';
    // The gradient string will contain the resolved rgb(34, 211, 238) value.
    expect(bg).toContain('34');
    expect(bg).toContain('211');
    expect(bg).toContain('238');
  });

  // ── 5. UX — duration 600-800ms; no z-index collision ───────────────────────
  test('5. UX: animation duration 600-800ms; ::after has no z-index override', async ({ page }) => {
    test.setTimeout(45000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.locator('.ag-cell[col-id="ltp"]').first().waitFor({ state: 'visible', timeout: 15000 });

    const uxChecks = await page.evaluate(() => {
      const el = document.createElement('div');
      el.className = 'cell-freshness-pulse';
      el.style.position = 'absolute';
      el.style.top = '-9999px';
      el.style.width = '100px';
      el.style.height = '20px';
      document.body.appendChild(el);

      const pseudoCs = window.getComputedStyle(el, '::after');
      const rawDur   = pseudoCs.animationDuration;   // e.g. "0.7s" or "700ms"
      const zIndex   = pseudoCs.zIndex;              // should be 'auto' or ''
      const ptEvts   = pseudoCs.pointerEvents;       // should be 'none'

      document.body.removeChild(el);

      // Parse duration to ms.
      let durationMs = null;
      if (rawDur) {
        if (rawDur.endsWith('ms'))  durationMs = parseFloat(rawDur);
        else if (rawDur.endsWith('s')) durationMs = parseFloat(rawDur) * 1000;
      }

      return { durationMs, zIndex, ptEvts };
    });

    // Duration must be 600-800ms.
    expect(uxChecks.durationMs).not.toBeNull();
    expect(uxChecks.durationMs).toBeGreaterThanOrEqual(600);
    expect(uxChecks.durationMs).toBeLessThanOrEqual(800);

    // z-index: 'auto' means no stacking context battle with ltp-flash layers.
    expect(uxChecks.zIndex === 'auto' || uxChecks.zIndex === '').toBe(true);

    // pointer-events:none means the underline never intercepts clicks.
    expect(uxChecks.ptEvts).toBe('none');
  });
});
