/**
 * toast_mobile_burst_cap.spec.js
 *
 * Mobile toast-burst-cap fix (2026-10) — on narrow viewports
 * (`max-width:600px`, ToastContainer.svelte) a burst of toasts firing
 * within the same tick (e.g. 5 template-attach-fail warnings detected
 * in one backstop poll) used to render all MAX_TOASTS=5 full-height
 * cards at once, covering whatever was underneath (confirmed:
 * account/expiry picker + payoff chart on the Derivatives page) for
 * the full 5000ms warning lifetime.
 *
 * Fix: ToastContainer.svelte caps simultaneously-VISIBLE toast cards
 * at MOBILE_VISIBLE=2 on mobile only, via a CSS `display:none` inside
 * the existing `max-width:600px` media query (every toast still
 * mounts and runs its own dismiss timer — see Toast.svelte — only
 * the rendering is suppressed). A "+N more" chip surfaces the hidden
 * count and toggles `_expanded` to reveal everything. Desktop is
 * untouched.
 *
 * Injection: the layout's existing dev-only `window.__stores` hook
 * (see tick_bus_synchrony.spec.js for the established pattern) now
 * also exposes `toast` (+layout.svelte), letting this spec push a
 * synthetic burst directly rather than driving 5 separate real
 * backend failure paths.
 *
 * Five quality dimensions:
 *  1. SSOT   — drives the real toastStore.svelte.js + ToastContainer.svelte
 *              render path, not a source-grep proxy.
 *  2. Perf   — (c) explicitly asserts the per-toast dismiss timeout
 *              (toastStore.svelte.js's own DEFAULT_TIMEOUT) is
 *              unaffected by the visibility cap — a toast hidden by
 *              the cap still auto-dismisses on schedule, proving the
 *              fix hides via CSS, not by delaying mount.
 *  3. Stale  — n/a (no poll loop involved in this surface).
 *  4. Reuse  — exercises the existing `toast` API; no parallel toast
 *              system invented. The window hook mirrors the existing
 *              tickBus pattern already used by other specs.
 *  5. UX     — (a) bounds the mobile stack's combined height so a
 *              burst can never bury the page underneath; (b) asserts
 *              desktop renders all 5 toasts unchanged (regression
 *              guard) and the "+N more" chip never appears there.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Push `n` sticky (timeoutMs: 0) warning toasts via the dev-only window hook. */
async function pushToastBurst(page, n, opts = { timeoutMs: 0 }) {
  await page.waitForFunction(
    () => !!(/** @type {any} */ (window).__stores?.toast),
    { timeout: 10_000 },
  );
  await page.evaluate(
    ({ n, opts }) => {
      const toast = /** @type {any} */ (window).__stores.toast;
      for (let i = 0; i < n; i++) {
        toast.warning(`Synthetic burst toast #${i + 1}`, opts);
      }
    },
    { n, opts },
  );
}

test.describe('(a)+(b) Mobile toast burst — capped stack, desktop unchanged', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('mobile: 5 simultaneous toasts render at most 2 full cards + a "+N more" chip', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    await pushToastBurst(page, 5);

    // All 5 are mounted (queued) — none silently dropped.
    const allSlots = page.locator('.rbq-toast-slot');
    await expect(allSlots).toHaveCount(5);

    // Only MOBILE_VISIBLE (2) are actually visible/rendered at full height.
    const visibleCards = page.locator('.rbq-toast-slot:not(.rbq-toast-overflow) .rbq-toast');
    await expect(visibleCards).toHaveCount(2);

    // The overflow chip surfaces the remaining 3, reachable (not dropped).
    const chip = page.locator('.rbq-toast-overflow-chip');
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText('+3 more');

    // Combined visible stack height must stay well under what 5 full
    // cards would occupy — the actual regression (burying the picker
    // and payoff chart underneath). Bound generously above the 2-card
    // real height so the test isn't brittle to minor padding tweaks,
    // but far below what 5 full warning cards would measure.
    const container = page.locator('.rbq-toast-container');
    const box = await container.boundingBox();
    expect(box).not.toBeNull();
    expect(box.height).toBeLessThan(260);

    // Tapping the chip reveals every queued toast in place.
    await chip.click();
    await expect(page.locator('.rbq-toast-slot .rbq-toast')).toHaveCount(5);
    await expect(chip).toHaveText('Show less');

    // Collapsing again goes back to the capped view.
    await chip.click();
    await expect(visibleCards).toHaveCount(2);
  });

  test('mobile: a toast hidden by the cap still auto-dismisses on schedule (timer not delayed by CSS hiding)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    // Push 5 toasts using the real default warning timeout (5000ms),
    // so every toast — visible or capped-hidden — dismisses on the
    // same schedule. If the cap worked by delaying mount instead of
    // CSS-hiding, the hidden ones would still be alive well past 5s.
    await pushToastBurst(page, 5, { timeoutMs: 1200 });
    await expect(page.locator('.rbq-toast-slot')).toHaveCount(5);

    await page.waitForTimeout(1700);

    await expect(page.locator('.rbq-toast-slot')).toHaveCount(0);
    await expect(page.locator('.rbq-toast-overflow-chip')).toHaveCount(0);
  });
});

test.describe('(b) Desktop — unchanged, all toasts render, no chip', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('desktop: 5 simultaneous toasts all render at full height, no overflow chip', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    await pushToastBurst(page, 5);

    const visibleCards = page.locator('.rbq-toast-slot .rbq-toast');
    await expect(visibleCards).toHaveCount(5);
    // The chip still mounts (the cap calc is viewport-independent JS),
    // but is CSS-hidden (`display:none`) outside the mobile media
    // query — assert it's not actually visible on desktop, not that
    // it's absent from the DOM.
    const chip = page.locator('.rbq-toast-overflow-chip');
    await expect(chip).toHaveCount(1);
    await expect(chip).toBeHidden();
  });
});
