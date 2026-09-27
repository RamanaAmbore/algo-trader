/**
 * /performance NavCard "as of" timestamp — dual-timezone format.
 *
 * Operator complaint (2026-09-27): the NavCard "as of" line on the public
 * /performance page rendered a raw Python `datetime.isoformat()` string
 * straight from the backend (e.g. "2026-09-25T18:30:18.700965+00:00")
 * instead of the app's normal human dual-timezone format.
 *
 * Root cause: `frontend/src/lib/NavCard.svelte` derived `asOf` directly
 * from `nav.as_of` with no formatter — `backend/api/routes/auth.py`'s
 * `_auth_nav_pnl_fallback()` sets that field via a bare
 * `datetime.now(timezone.utc).isoformat()` (no `timespec` truncation),
 * so anonymous visitors hitting the off-hours NAV fallback path saw the
 * raw ISO string with microseconds + UTC offset.
 *
 * Fix: `asOf` now runs through `formatDualTz(new Date(nav.as_of))` from
 * `$lib/stores` — the same dual-timezone formatter RefreshButton uses for
 * its "Last refreshed" tooltip line (`frontend/src/lib/RefreshButton.svelte`).
 *
 * This spec mocks the public, unauthenticated `/api/auth/firm-nav`
 * endpoint (the anonymous-visitor NAV path NavCard falls back to) so the
 * assertion is deterministic regardless of live market/off-hours state.
 *
 * Five quality dimensions:
 *  1. SSOT     — asserts the single `formatDualTz` call site in NavCard.svelte
 *                renders correctly; no duplicate formatting logic to drift.
 *  2. Perf     — mocked network response, no live broker round-trip.
 *  3. Stale    — negatively asserts the raw-ISO shape (T separator +
 *                microseconds) never reappears — the literal regression.
 *  4. Reuse    — computes the expected string via the real `formatDualTz`
 *                import so the test can't silently drift from the impl.
 *  5. UX       — asserts the element is visible (not vacuously passing on
 *                a hidden card) before checking its text.
 */

import { test, expect } from '@playwright/test';

const RAW_ISO_AS_OF = '2026-09-25T18:30:18.700965+00:00';

test('NavCard "as of" renders dual-timezone format, not raw ISO', async ({ page }) => {
  await page.route('**/api/auth/firm-nav', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        firm_nav: 1_234_567,
        firm_day_pnl: 12_345,
        firm_cum_pnl: 54_321,
        as_of: RAW_ISO_AS_OF,
      }),
    })
  );

  await page.goto('/performance', { waitUntil: 'networkidle' });

  const asOfEl = page.locator('.nav-as-of');
  await expect(asOfEl).toBeVisible({ timeout: 15_000 });

  const text = await asOfEl.textContent();

  // Compute expected via the REAL formatDualTz — imported through the
  // live Vite dev module graph inside the page itself so `$app/*`
  // SvelteKit aliases resolve exactly as they do for the app bundle
  // (a plain Node-side import of stores.js fails: `$app` only resolves
  // inside a SvelteKit/Vite module graph, not bare Node).
  const expected = await page.evaluate(async (isoStr) => {
    const mod = await import('/src/lib/stores.js');
    return `as of ${mod.formatDualTz(new Date(isoStr))}`;
  }, RAW_ISO_AS_OF);

  expect(text).toBe(expected);

  // The literal regression: raw ISO separator + microseconds must never
  // appear in the rendered text again.
  expect(text).not.toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/);
  expect(text).not.toMatch(/\.\d{6}/);
});
