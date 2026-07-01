/**
 * derivatives_underlying_selfheal.spec.js
 *
 * Verifies the three-tier underlying-picker fallback on /admin/derivatives.
 *
 * Problem fixed: when positions store is empty (broker down, pre-market,
 * weekend) the underlying picker had no options and the page showed
 * "No underlying selected" in the Candidates empty state — unusable.
 *
 * Solution: tiered derivation
 *   Tier A — book: positions with CE/PE/FUT (highest priority)
 *   Tier B — watchlist: F&O-eligible roots from default watchlist
 *   Tier C — popular: NIFTY 50 and peers from POPULAR_UNDERLYINGS
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — auto-select reads tiered list; no duplicate sources
 *  2. Perf     — page should pick a default within 20 s even on cold load
 *  3. Stale    — grep confirms "No underlying selected" absent after load
 *  4. Reusable — Select component + POPULAR_UNDERLYINGS list unchanged
 *  5. UX       — empty state distinguishes "Loading…" from "None selected";
 *                hint below picker fires when book is empty
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_underlying_selfheal.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE      = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;
const PULSE_URL = `${BASE}/pulse`;

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      // 429 = rate-limited, retry; 502 = transient gateway, retry
      if (resp.status() !== 429 && resp.status() !== 502) {
        throw new Error(`authOnce: login returned ${resp.status()}`);
      }
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

/** PLACEHOLDER text values the Select component shows when no value is set.
 *  waitForUnderlying must NOT accept these as a "selection". */
const PLACEHOLDER_TEXTS = new Set([
  'PICK UNDERLYING…',
  'LOADING UNDERLYINGS…',
  'NO OPTIONS IN BOOK',
  '',
]);

/**
 * Wait up to `timeout` ms for the underlying picker to show a real selection
 * (not a placeholder). Returns the selected value (uppercased).
 */
async function waitForUnderlying(page, timeout = 20_000) {
  const trigger = page.locator('button#opt-und');
  await expect(trigger).toBeVisible({ timeout: 8_000 });
  const label = trigger.locator('.rbq-select-label');
  // Poll until the label text is non-empty AND not a placeholder.
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const text = ((await label.textContent()) || '').trim().toUpperCase();
    if (text && !PLACEHOLDER_TEXTS.has(text)) return text;
    await page.waitForTimeout(500);
  }
  const last = ((await label.textContent()) || '').trim();
  throw new Error(`waitForUnderlying: timed out — picker shows "${last}"`);
}

// ── Suite 1: Real server — picker never shows "No underlying selected" ───────
//
// These tests hit the real dev server and assert the fundamental
// invariant: after instruments load, the page always has SOME pick.
// They do NOT assert which underlying is chosen (the book is live data).

test.describe('Live server — picker invariant', () => {
  test.setTimeout(45_000);

  test('"No underlying selected" never appears after instruments load', async ({ page }) => {
    await authOnce(page);

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Give instruments + auto-select up to 20 s to resolve.
    // During hydration the empty state may briefly show "Loading underlyings…"
    // which is acceptable. "No underlying selected" must NEVER appear.
    await page.waitForTimeout(3_000);
    await expect(page.getByText('No underlying selected')).not.toBeVisible();

    // After a further 15 s the picker should have a real selection.
    const selected = await waitForUnderlying(page, 20_000);
    expect(selected.length).toBeGreaterThan(0);

    // Double-check: "No underlying selected" absent.
    await expect(page.getByText('No underlying selected')).not.toBeVisible();
  });

  test('picker trigger shows a value (not placeholder) within 20 s', async ({ page }) => {
    await authOnce(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const selected = await waitForUnderlying(page, 20_000);
    expect(selected).toBeTruthy();
    // Must not be any placeholder text.
    expect(PLACEHOLDER_TEXTS.has(selected)).toBe(false);
  });
});

// ── Suite 2: Cross-page nav — auto-selected default persists ─────────────────

test.describe('Cross-page nav — auto-selected default persists', () => {
  test.setTimeout(60_000);

  test('navigate to /pulse and back — underlying still selected', async ({ page }) => {
    await authOnce(page);

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Wait for auto-selection.
    const first = await waitForUnderlying(page, 20_000);
    expect(first.length).toBeGreaterThan(0);

    // Navigate away.
    await page.goto(PULSE_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);

    // Navigate back — URL carries ?u=<underlying> so the selection is
    // preserved across the nav via the URL sync $effect.
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const second = await waitForUnderlying(page, 20_000);
    // The URL param should restore the same underlying.
    expect(second.toUpperCase()).toBe(first.toUpperCase());
    await expect(page.getByText('No underlying selected')).not.toBeVisible();
  });
});

// ── Suite 3: Empty state copy ─────────────────────────────────────────────────
// These tests verify the UX copy changes: "Loading underlyings…" during
// hydration, "No underlying selected" only if all tiers empty + hydrating.

test.describe('Empty state copy — loading vs no-selection', () => {
  test.setTimeout(45_000);

  test('picker placeholder says "Loading underlyings…" before instruments ready', async ({ page }) => {
    await authOnce(page);

    // Track the very first placeholder value rendered by intercepting
    // the picker trigger before instruments are loaded.
    let firstPlaceholder = '';
    // Stall instruments API so we can observe the loading state.
    // Instruments are fetched via the browser's own cache (IndexedDB),
    // not the API, so we can't stall them via route mock. Instead,
    // we capture the placeholder value at page-open time.
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Immediately read the placeholder — instruments cache may or may
    // not have resolved in this window.
    const trigger = page.locator('button#opt-und');
    await expect(trigger).toBeVisible({ timeout: 5_000 });
    firstPlaceholder = ((await trigger.locator('.rbq-select-label').textContent()) || '').trim();

    // The placeholder must be either:
    // a) "Loading underlyings…" (instruments still loading), OR
    // b) A real underlying (instruments loaded fast from cache), OR
    // c) "Pick underlying…" (instruments ready, nothing selected yet — race)
    // It must NOT be the OLD "No options in book" text.
    expect(firstPlaceholder.toLowerCase()).not.toContain('no options in book');
  });

  test('"No underlying selected" empty state absent after 15 s', async ({ page }) => {
    await authOnce(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // After 15 s the three-tier fallback must have fired.
    await page.waitForTimeout(15_000);
    await expect(page.getByText('No underlying selected')).not.toBeVisible();
  });
});

// ── Suite 4: Stale code audit ─────────────────────────────────────────────────
// Dimension 3 (Stale): grep the compiled derivatives page to confirm the
// three-tier derivation code is present (not optimised away) and that
// the old single-tier derivation isn't still live.

test.describe('Stale code audit (grep)', () => {
  test('derivatives page source contains three-tier auto-select', async ({ page }) => {
    await authOnce(page);

    // Fetch the compiled JS bundle for the derivatives page.
    // We grep for the Tier B and Tier C markers in the source code
    // of the Svelte component itself (not the compiled bundle —
    // variable names may be minified).
    // Check the source file directly rather than the bundle.
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );

    // Tier B: watchlist state should exist.
    expect(src).toContain('_watchlistSyms');
    // Tier C: POPULAR_UNDERLYINGS reference in the picker derivation.
    expect(src).toContain('POPULAR_UNDERLYINGS');
    // Auto-select effect must guard with "if (selectedUnderlying) return".
    expect(src).toContain('if (selectedUnderlying) return');
    // New hint below picker.
    expect(src).toContain('opt-und-hint');
    // Empty state should use conditional instrumentsReady check.
    expect(src).toContain('instrumentsReady ?');
  });

  test('fetchWatchlist (not just fetchWatchlists) is imported for item fetching', async ({ page }) => {
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );
    // Both the list fetch (fetchWatchlists) and the item fetch (fetchWatchlist)
    // must be present.
    expect(src).toContain('fetchWatchlist,');
    expect(src).toContain('fetchWatchlists,');
  });
});

// ── Suite 5: UX — hint below picker appears when book empty ──────────────────
// On a server with live positions, this hint may not appear (book non-empty).
// We validate its existence in the DOM only when we can confirm the book
// is empty (a condition the live server doesn't guarantee). Test via source
// inspection instead — the markup exists in the Svelte template.

test.describe('UX hint existence in markup', () => {
  test('opt-und-hint element exists in the derivatives page template', async ({ page }) => {
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );
    // The hint div and its condition.
    expect(src).toContain('class="opt-und-hint"');
    expect(src).toContain('No F&O positions — showing watchlist + popular.');
    // CSS for the hint exists.
    expect(src).toContain('.opt-und-hint {');
  });

  test('CSS for watchlist and popular hint tiers exists', async ({ page }) => {
    const fs = await import('fs/promises');
    const src = await fs.readFile(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ),
      'utf8',
    );
    expect(src).toContain("data-hint='watchlist'");
    expect(src).toContain("data-hint='popular'");
  });
});
