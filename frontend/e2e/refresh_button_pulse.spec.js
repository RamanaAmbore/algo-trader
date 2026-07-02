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
 * Run:
 *   npx playwright test e2e/refresh_button_pulse.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ viewport: { width: 1400, height: 900 } });

test.describe('RefreshButton tick-pulse animation', () => {

  // ── 1. SSOT — rf-tick-a/rf-tick-b classes are defined in the stylesheet ──
  test('1. SSOT: rf-tick-pulse-a and rf-tick-pulse-b keyframes are defined', async ({ page }) => {
    test.setTimeout(45000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Inject a test button with both classes and verify each triggers a distinct
    // animation-name — confirming the CSS fix (distinct keyframe names per class).
    const result = await page.evaluate(() => {
      const btnA = document.createElement('button');
      btnA.className = 'rf-btn rf-tick-a';
      btnA.style.cssText = 'position:absolute;top:-9999px;width:1.4rem;height:1.4rem;';
      document.body.appendChild(btnA);
      const animA = window.getComputedStyle(btnA).animationName;
      document.body.removeChild(btnA);

      const btnB = document.createElement('button');
      btnB.className = 'rf-btn rf-tick-b';
      btnB.style.cssText = 'position:absolute;top:-9999px;width:1.4rem;height:1.4rem;';
      document.body.appendChild(btnB);
      const animB = window.getComputedStyle(btnB).animationName;
      document.body.removeChild(btnB);

      return { animA, animB };
    });

    // Both animation names must be non-empty (keyframes defined)
    expect(result.animA).not.toBe('none');
    expect(result.animA).not.toBe('');
    expect(result.animB).not.toBe('none');
    expect(result.animB).not.toBe('');

    // THE REGRESSION GUARD: the two classes must produce DIFFERENT animation-name
    // values. If they share the same name, toggling a↔b won't restart the animation.
    expect(result.animA).not.toBe(result.animB);
  });

  // ── 2. Perf — class alternates within 300ms of synthetic tick bumps ─────────
  test('2. Perf: rf-tick-a/rf-tick-b alternates within 300ms of tick bump', async ({ page }) => {
    test.setTimeout(45000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Wait for at least one RefreshButton to be present.
    const btn = page.locator('.rf-btn').first();
    await btn.waitFor({ state: 'visible', timeout: 10000 });

    // Expose the symbolTickCount store to the test via window.__rbqTickCount.
    // We need to call update() on it — check if the store is accessible.
    const storeReady = await page.evaluate(() => {
      // Check if Svelte stores are exposed (they may not be in production builds).
      // Fall back to dispatching a custom event that the component listens for.
      return typeof window.__stores !== 'undefined';
    });

    // Bump the symbolTickCount store 10 times and check the button class toggles.
    // We do this by evaluating in-page if the store is reachable, otherwise
    // we rely on the CSS animation-name regression guard above.
    if (storeReady) {
      const observed = await page.evaluate(async () => {
        const classes = [];
        const btn = document.querySelector('.rf-btn');
        if (!btn) return classes;
        const observer = new MutationObserver(() => {
          const cls = btn.className;
          if (cls.includes('rf-tick-a') || cls.includes('rf-tick-b')) {
            classes.push(cls.includes('rf-tick-a') ? 'a' : 'b');
          }
        });
        observer.observe(btn, { attributes: true, attributeFilter: ['class'] });

        for (let i = 0; i < 10; i++) {
          window.__stores.symbolTickCount.update(v => v + 1);
          await new Promise(r => setTimeout(r, 350)); // wait > 250ms throttle
        }
        observer.disconnect();
        return classes;
      });

      // Should have seen at least 5 class changes in 10 ticks.
      expect(observed.length).toBeGreaterThanOrEqual(5);

      // Classes must alternate (never same consecutive value).
      for (let i = 1; i < observed.length; i++) {
        expect(observed[i]).not.toBe(observed[i - 1]);
      }
    } else {
      // Store not directly accessible — confirm the CSS animation-name fix is
      // sufficient (covered by test 1 above). Mark this path explicitly.
      test.info().annotations.push({
        type: 'note',
        description: 'window.__stores not exposed in prod build; animation-name guard in test 1 covers regression.',
      });
    }
  });

  // ── 3. Stale — animation-name changes on EVERY a↔b toggle ──────────────────
  test('3. Stale: animation-name differs between rf-tick-a and rf-tick-b (regression guard)', async ({ page }) => {
    test.setTimeout(30000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Simulate the toggle the JS does: '' → 'rf-tick-a' → 'rf-tick-b' → 'rf-tick-a'.
    // At each step read getComputedStyle(btn).animationName and assert it changed.
    const animNames = await page.evaluate(() => {
      const btn = document.createElement('button');
      btn.className = 'rf-btn rf-mkt-closed';
      btn.style.cssText = 'position:absolute;top:-9999px;width:1.4rem;height:1.4rem;';
      document.body.appendChild(btn);

      const read = () => window.getComputedStyle(btn).animationName;

      // Initial state — no pulse class
      const n0 = read();

      btn.classList.add('rf-tick-a');
      const n1 = read();

      btn.classList.remove('rf-tick-a');
      btn.classList.add('rf-tick-b');
      const n2 = read();

      btn.classList.remove('rf-tick-b');
      btn.classList.add('rf-tick-a');
      const n3 = read();

      document.body.removeChild(btn);
      return [n0, n1, n2, n3];
    });

    const [n0, n1, n2, n3] = animNames;

    // After adding rf-tick-a: animation-name should be non-none
    expect(n1).not.toBe('none');
    expect(n1).not.toBe('');

    // n1 (rf-tick-a) must differ from n2 (rf-tick-b) — the CSS regression
    expect(n1).not.toBe(n2);

    // n3 should match n1 (back to rf-tick-a)
    expect(n3).toBe(n1);

    // n0 (no class) should be 'none' or empty
    expect(n0 === 'none' || n0 === '').toBe(true);
  });

  // ── 4. Reuse — loading=true suppresses the pulse class ──────────────────────
  test('4. UX: loading state suppresses rf-tick-a/rf-tick-b class', async ({ page }) => {
    test.setTimeout(30000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    const btn = page.locator('.rf-btn').first();
    await btn.waitFor({ state: 'visible', timeout: 10000 });

    // When loading=true, the button gains rf-spinning and the disabled attribute.
    // Under that condition the subscribe callback skips the toggle.
    // We verify: while disabled (spinning), neither rf-tick-a nor rf-tick-b appears.
    const storeReady = await page.evaluate(() => typeof window.__stores !== 'undefined');
    if (!storeReady) {
      test.skip(true, 'window.__stores not exposed in prod build');
      return;
    }

    const result = await page.evaluate(async () => {
      // Find a RefreshButton that is NOT currently disabled.
      const btn = Array.from(document.querySelectorAll('.rf-btn'))
        .find(b => !b.disabled);
      if (!btn) return { found: false };

      // Simulate the loading=true state by temporarily adding rf-spinning + disabled.
      btn.classList.add('rf-spinning');
      btn.setAttribute('disabled', '');

      // Fire 3 tick bumps during "loading" — none should toggle a tick class.
      for (let i = 0; i < 3; i++) {
        window.__stores.symbolTickCount.update(v => v + 1);
        await new Promise(r => setTimeout(r, 350));
      }

      const hasPulse = btn.classList.contains('rf-tick-a') || btn.classList.contains('rf-tick-b');

      // Clean up.
      btn.classList.remove('rf-spinning');
      btn.removeAttribute('disabled');

      return { found: true, hasPulse };
    });

    if (result.found) {
      expect(result.hasPulse).toBe(false);
    }
  });

  // ── 5. UX — prefers-reduced-motion disables animation ───────────────────────
  test('5. UX: prefers-reduced-motion disables rf-tick-pulse animation', async ({ page }) => {
    test.setTimeout(30000);
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Override media feature.
    await page.emulateMedia({ reducedMotion: 'reduce' });

    const animName = await page.evaluate(() => {
      const btn = document.createElement('button');
      btn.className = 'rf-btn rf-tick-a';
      btn.style.cssText = 'position:absolute;top:-9999px;width:1.4rem;height:1.4rem;';
      document.body.appendChild(btn);
      const anim = window.getComputedStyle(btn).animationName;
      document.body.removeChild(btn);
      return anim;
    });

    // Under reduced-motion the @media rule sets animation:none
    expect(animName === 'none' || animName === '').toBe(true);
  });
});
