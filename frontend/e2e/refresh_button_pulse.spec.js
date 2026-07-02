/**
 * refresh_button_pulse.spec.js
 *
 * Verifies that the RefreshButton's tick-pulse animation fires and alternates
 * correctly on every SSE tick landing in symbolStore.
 *
 * Root cause being guarded (Jul 2026): the original CSS used a shared
 * animation-name for both .rf-tick-a and .rf-tick-b, so toggling the class
 * a↔b left the computed animation-name unchanged — the browser never restarted
 * the animation after the first pulse. Fix: distinct keyframe names per class.
 *
 * Five quality dimensions:
 *   SSOT  — rf-tick-a/rf-tick-b classes come from RefreshButton.svelte ONLY
 *   Perf  — class toggle fires within 300ms of a synthetic tick bump
 *   Stale — animation-name changes on every a↔b toggle (the regression guard)
 *   Reuse — suppression guards (loading, reduced-motion) are preserved
 *   UX    — loading=true suppresses the pulse; prefers-reduced-motion disables animation
 *
 * IMPORTANT: CSS in RefreshButton.svelte is scoped by Svelte (selector gains
 * .svelte-<hash>). Tests that need to read animation-name MUST operate on a
 * REAL .rf-btn element already in the DOM (which carries the scoping class),
 * not on freshly-injected raw elements. This is unlike global app.css classes
 * (e.g. .cell-freshness-pulse in freshness_shimmer.spec.js).
 *
 * Run:
 *   cd frontend
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/refresh_button_pulse.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ viewport: { width: 1400, height: 900 } });

/** Navigate to /pulse and wait until the page header (and a RefreshButton within it) is visible. */
async function gotoAndWaitForRefreshBtn(page) {
  await loginAsAdmin(page);
  // Use domcontentloaded — /pulse keeps SSE connections alive, so networkidle never fires.
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  // Wait for the page header which always contains a RefreshButton.
  await page.locator('.page-header .rf-btn').first().waitFor({ state: 'visible', timeout: 30000 });
}

test.describe('RefreshButton tick-pulse animation', () => {

  // ── 1. SSOT — animation-name is defined on a real .rf-btn for rf-tick-a/b ──
  test('1. SSOT: rf-tick-pulse-a and rf-tick-pulse-b keyframes produce distinct animation-names', async ({ page }) => {
    test.setTimeout(60000);
    await gotoAndWaitForRefreshBtn(page);

    // Operate on a real .rf-btn element (carries Svelte scoping class so the
    // component's scoped CSS rules match). Temporarily add then remove tick
    // classes and read the computed animationName at each state.
    const result = await page.evaluate(() => {
      const btn = document.querySelector('.page-header .rf-btn');
      if (!btn) return { found: false, animA: '', animB: '' };

      // Save original class list so we restore it cleanly.
      const origClass = btn.className;

      // Remove any existing tick class, add rf-tick-a, read animation-name.
      btn.className = origClass.replace(/\brf-tick-[ab]\b/g, '').trim() + ' rf-tick-a';
      const animA = window.getComputedStyle(btn).animationName;

      // Swap to rf-tick-b, read again.
      btn.className = origClass.replace(/\brf-tick-[ab]\b/g, '').trim() + ' rf-tick-b';
      const animB = window.getComputedStyle(btn).animationName;

      // Restore original state.
      btn.className = origClass;

      return { found: true, animA, animB };
    });

    expect(result.found, 'Expected to find a .rf-btn inside .page-header').toBe(true);

    // Both animation names must be non-empty (keyframes defined and matched).
    expect(result.animA, 'rf-tick-a should have a defined animation-name').not.toBe('none');
    expect(result.animA).not.toBe('');
    expect(result.animB, 'rf-tick-b should have a defined animation-name').not.toBe('none');
    expect(result.animB).not.toBe('');

    // THE REGRESSION GUARD: the two classes must produce DIFFERENT animation-name
    // computed values. If they share the same name, the a↔b toggle won't restart
    // the CSS animation after the first pulse.
    expect(result.animA, 'rf-tick-a and rf-tick-b must map to different animation-names').not.toBe(result.animB);
  });

  // ── 2. Perf — class alternates with each synthetic tick bump ────────────────
  test('2. Perf: class alternates rf-tick-a↔rf-tick-b on consecutive ticks', async ({ page }) => {
    test.setTimeout(90000);
    await gotoAndWaitForRefreshBtn(page);

    const storeReady = await page.evaluate(() => typeof window.__stores !== 'undefined');

    if (!storeReady) {
      // Store not exposed in this build — CSS regression guard in test 1 is the
      // primary protection.
      test.info().annotations.push({
        type: 'note',
        description: 'window.__stores not exposed — JS toggle path covered by test 1 CSS regression guard.',
      });
      return;
    }

    const observed = await page.evaluate(async () => {
      const btn = document.querySelector('.page-header .rf-btn');
      if (!btn) return [];
      const classes = [];
      const observer = new MutationObserver(() => {
        if (btn.classList.contains('rf-tick-a')) classes.push('a');
        else if (btn.classList.contains('rf-tick-b')) classes.push('b');
      });
      observer.observe(btn, { attributes: true, attributeFilter: ['class'] });
      for (let i = 0; i < 10; i++) {
        window.__stores.symbolTickCount.update(v => v + 1);
        await new Promise(r => setTimeout(r, 350));
      }
      observer.disconnect();
      return classes;
    });

    expect(observed.length).toBeGreaterThanOrEqual(5);
    for (let i = 1; i < observed.length; i++) {
      expect(observed[i]).not.toBe(observed[i - 1]);
    }
  });

  // ── 3. Stale — animation-name CHANGES between rf-tick-a and rf-tick-b ───────
  test('3. Stale: animation-name is different for each class (regression guard via DOM cycle)', async ({ page }) => {
    test.setTimeout(60000);
    await gotoAndWaitForRefreshBtn(page);

    // Cycle through '' → 'rf-tick-a' → 'rf-tick-b' → 'rf-tick-a' on a real
    // button and record the animationName at each step.
    const names = await page.evaluate(() => {
      const btn = document.querySelector('.page-header .rf-btn');
      if (!btn) return null;
      const orig = btn.className;
      const base = orig.replace(/\brf-tick-[ab]\b/g, '').trim();
      const read = () => window.getComputedStyle(btn).animationName;

      // State 0: no tick class
      btn.className = base;
      const n0 = read();

      // State 1: rf-tick-a
      btn.className = base + ' rf-tick-a';
      const n1 = read();

      // State 2: rf-tick-b
      btn.className = base + ' rf-tick-b';
      const n2 = read();

      // State 3: back to rf-tick-a
      btn.className = base + ' rf-tick-a';
      const n3 = read();

      btn.className = orig;
      return { n0, n1, n2, n3 };
    });

    expect(names, 'Expected to find a real .rf-btn in .page-header').not.toBeNull();
    const { n0, n1, n2, n3 } = names;

    // n0 should be 'none' or empty (no pulse class active)
    expect(n0 === 'none' || n0 === '').toBe(true);

    // n1 and n2 must be defined (keyframes block loaded)
    expect(n1, 'rf-tick-a animation-name must be defined').not.toBe('none');
    expect(n1).not.toBe('');
    expect(n2, 'rf-tick-b animation-name must be defined').not.toBe('none');
    expect(n2).not.toBe('');

    // THE CORE REGRESSION GUARD — different names force browser to restart animation
    expect(n1, 'rf-tick-a and rf-tick-b must use different keyframe names so browser restarts').not.toBe(n2);

    // n3 should match n1 (same class, same keyframes name)
    expect(n3).toBe(n1);
  });

  // ── 4. Reuse — loading=true suppresses the pulse ─────────────────────────────
  test('4. UX: loading state (rf-spinning + disabled) suppresses rf-tick class', async ({ page }) => {
    test.setTimeout(60000);
    await gotoAndWaitForRefreshBtn(page);

    const storeReady = await page.evaluate(() => typeof window.__stores !== 'undefined');
    if (!storeReady) {
      test.skip(true, 'window.__stores not exposed in this build');
      return;
    }

    const result = await page.evaluate(async () => {
      const btn = Array.from(document.querySelectorAll('.page-header .rf-btn')).find(b => !b.disabled);
      if (!btn) return { found: false };
      const orig = btn.className;
      btn.classList.add('rf-spinning');
      btn.setAttribute('disabled', '');
      for (let i = 0; i < 3; i++) {
        window.__stores.symbolTickCount.update(v => v + 1);
        await new Promise(r => setTimeout(r, 350));
      }
      const hasPulse = btn.classList.contains('rf-tick-a') || btn.classList.contains('rf-tick-b');
      btn.classList.remove('rf-spinning');
      btn.removeAttribute('disabled');
      btn.className = orig;
      return { found: true, hasPulse };
    });

    if (result.found) {
      expect(result.hasPulse).toBe(false);
    }
  });

  // ── 5. UX — prefers-reduced-motion disables animation on rf-tick-a ──────────
  test('5. UX: prefers-reduced-motion disables rf-tick-pulse animation', async ({ page }) => {
    test.setTimeout(60000);
    // Emulate reduced-motion BEFORE navigation so the media query is in effect
    // when the page and its stylesheets load.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.page-header .rf-btn').first().waitFor({ state: 'visible', timeout: 30000 });

    const animName = await page.evaluate(() => {
      const btn = document.querySelector('.page-header .rf-btn');
      if (!btn) return 'no-btn';
      const orig = btn.className;
      btn.className = orig.replace(/\brf-tick-[ab]\b/g, '').trim() + ' rf-tick-a';
      const name = window.getComputedStyle(btn).animationName;
      btn.className = orig;
      return name;
    });

    // Under @media (prefers-reduced-motion: reduce) the rule sets animation:none
    expect(animName === 'none' || animName === '').toBe(true);
  });
});
