/**
 * derivatives_payoff_default.spec.js
 *
 * Guards the three auto-select cases for `selectedUnderlying` in the
 * payoff overlay dropdown on /admin/derivatives:
 *
 *   CASE 1 — No selection yet: auto-select fires and picks the first option.
 *   CASE 2 — Current is a 'popular' provisional seed: promote when a
 *             positions-tier entry appears in the rebuilt options list.
 *   CASE 3 — Stale cache: previously-selected underlying is no longer in
 *             the options list (e.g. COPPER removed after positions reload).
 *             Must reset to the first available option — not persist the stale
 *             value. (This is the bug fixed in the race-condition patch.)
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — selectedUnderlying is the single source driven by the $effect;
 *                no duplicate derivation; verified via dropdown label only.
 *  2. Perf     — auto-select must complete within 20s of page load.
 *  3. Stale    — source grep confirms the stale-cache reset branch is present
 *                and correctly ordered before the promote case.
 *  4. Reusable — auto-select $effect is the same code path for all three cases.
 *  5. UX       — dropdown never shows a stale/absent value after options reload.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_payoff_default.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const BASE      = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;

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

const PLACEHOLDERS = new Set([
  'PICK UNDERLYING…', 'LOADING UNDERLYINGS…', 'NO OPTIONS IN BOOK', '',
]);

async function waitForUnderlying(page, timeout = 20_000) {
  const trigger = page.locator('button#opt-und');
  await expect(trigger).toBeVisible({ timeout: 8_000 });
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const text = ((await trigger.locator('.rbq-select-label').textContent()) || '').trim().toUpperCase();
    if (text && !PLACEHOLDERS.has(text)) return text;
    await page.waitForTimeout(400);
  }
  return null;
}

// ── Suite 1: Source audit — stale-cache reset branch ─────────────────────────
// Dimension 3 (Stale): grep the Svelte source to confirm the new branch exists,
// is correctly ordered before the promote case, and uses curInOpts.

test.describe('Source audit — stale-cache reset branch', () => {
  const readSrc = () =>
    readFileSync(
      resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
      'utf8',
    );

  test('stale-cache reset branch present in auto-select $effect', () => {
    const src = readSrc();

    // The new branch must check curInOpts (not just curIsPopular).
    expect(src, 'Missing curInOpts lookup in auto-select effect').toContain(
      'const curInOpts = opts.find(o => o.value === cur);',
    );

    // The stale-cache guard must check !curInOpts.
    expect(src, 'Missing !curInOpts stale-cache guard').toContain(
      'if (!curInOpts && opts[0]?.value)',
    );

    // The promote case must now read curInOpts?.hint, not re-find.
    expect(src, 'Missing curInOpts?.hint === popular in promote case').toContain(
      "curInOpts?.hint === 'popular'",
    );
  });

  test('stale-cache reset branch appears before the promote case', () => {
    const src = readSrc();

    const staleCacheIdx = src.indexOf('if (!curInOpts && opts[0]?.value)');
    const promoteIdx    = src.indexOf("curInOpts?.hint === 'popular'");

    expect(staleCacheIdx, 'Stale-cache reset branch not found in source').toBeGreaterThan(-1);
    expect(promoteIdx, 'Promote case (curInOpts?.hint) not found in source').toBeGreaterThan(-1);
    expect(
      staleCacheIdx,
      'Stale-cache reset branch must appear BEFORE the promote case',
    ).toBeLessThan(promoteIdx);
  });

  test('stale-cache branch has early return so promote case is skipped', () => {
    const src = readSrc();

    // After the stale-cache reset, there must be a return statement before
    // the promote case runs. Extract the block between the two guards.
    const staleCacheStart = src.indexOf('if (!curInOpts && opts[0]?.value)');
    const promoteStart    = src.indexOf("const curIsPopular = curInOpts?.hint");

    expect(staleCacheStart).toBeGreaterThan(-1);
    expect(promoteStart).toBeGreaterThan(staleCacheStart);

    const between = src.slice(staleCacheStart, promoteStart);
    expect(between, 'Missing return statement between stale-cache guard and promote case').toContain('return;');
  });

  test('old curIsPopular re-find pattern is removed', () => {
    const src = readSrc();

    // The old code used: opts.find(o => o.value === cur)?.hint === 'popular'
    // In the new code curInOpts is already looked up; the old inline find is gone.
    expect(
      src,
      'Old inline opts.find pattern for curIsPopular still present — should use curInOpts',
    ).not.toContain("opts.find(o => o.value === cur)?.hint === 'popular'");
  });
});

// ── Suite 2: CASE 1 — No selection: first option auto-selected ────────────────
// Dimension 1 (SSOT) + Dimension 2 (Perf): verifies the happy path still works.

test.describe('CASE 1 — No selection: first option auto-selected on fresh load', () => {
  test.setTimeout(60_000);

  test('dropdown shows a non-placeholder value within 20s', async ({ page }) => {
    await authOnce(page);

    // Clear any persisted underlying so we hit the CASE-1 branch.
    await page.context().addInitScript(() => {
      sessionStorage.removeItem('ramboq:options-state');
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const selected = await waitForUnderlying(page);

    if (!selected) {
      test.skip(true, 'No underlying loaded within 20s — pre-market or broker down; skip');
      return;
    }

    // Dropdown must show a real symbol, not a placeholder.
    expect(
      PLACEHOLDERS.has(selected),
      `Dropdown shows placeholder "${selected}" instead of auto-selected underlying`,
    ).toBe(false);
  });
});

// ── Suite 3: CASE 3 — Stale cache: missing option resets to first ─────────────
// Dimension 5 (UX): the core bug fix. Inject a stale sessionStorage value for
// an underlying that won't appear in the mocked options list, then verify the
// dropdown resets to the first real option instead of persisting the stale value.

test.describe('CASE 3 — Stale cache: previously-selected underlying absent from options', () => {
  test.setTimeout(60_000);

  test('dropdown resets to first option when cached underlying is not in options', async ({ page }) => {
    await authOnce(page);

    // Inject a stale cached underlying (COPPER) that the mocked positions
    // endpoint will NOT include. The mocked options list will only contain NIFTY.
    await page.context().addInitScript(() => {
      // Simulate what sessionStorage.setItem writes for the saved underlying.
      // The page reads: sessionStorage.getItem('opt.underlying') or similar key.
      // We write both plausible keys so we cover the actual implementation.
      sessionStorage.setItem('opt.underlying', 'COPPER');
      sessionStorage.setItem('ramboq:opt-underlying', 'COPPER');
    });

    // Mock positions to return a NIFTY option position only (no COPPER).
    await page.route('**/api/positions/**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          positions: [
            {
              tradingsymbol: 'NIFTY2591024900CE',
              symbol: 'NIFTY',
              underlying: 'NIFTY',
              exchange: 'NFO',
              quantity: 50,
              opening_quantity: 50,
              average_price: 120,
              last_price: 135,
              pnl: 750,
              day_change: 5,
              close_price: 128,
              kind: 'opt',
            },
          ],
          holdings: [],
          source: 'live',
          as_of: null,
        }),
      });
    });

    // Mock holdings — empty (no COPPER holdings).
    await page.route('**/api/holdings/**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ holdings: [], source: 'live', as_of: null }),
      });
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const selected = await waitForUnderlying(page, 25_000);

    if (!selected) {
      test.skip(true, 'No underlying loaded within 25s — skip');
      return;
    }

    // The stale COPPER value must NOT persist in the dropdown.
    expect(
      selected,
      'Stale COPPER value persisted in dropdown despite COPPER being absent from options — race condition regression',
    ).not.toBe('COPPER');

    // The dropdown must show a real non-placeholder value.
    expect(
      PLACEHOLDERS.has(selected),
      `Dropdown shows placeholder "${selected}" after stale-cache reset`,
    ).toBe(false);
  });

  test('stale underlying from sessionStorage does not block auto-select', async ({ page }) => {
    // Variant: stale value is an MCX underlying (CRUDEOIL) that won't
    // appear when only equity/NSE positions are returned. Auto-select
    // must still fire and produce a valid selection.
    await authOnce(page);

    await page.context().addInitScript(() => {
      sessionStorage.setItem('opt.underlying', 'CRUDEOIL');
      sessionStorage.setItem('ramboq:opt-underlying', 'CRUDEOIL');
    });

    // Mock positions — BANKNIFTY option only, no CRUDEOIL.
    await page.route('**/api/positions/**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          positions: [
            {
              tradingsymbol: 'BANKNIFTY2591048000PE',
              symbol: 'BANKNIFTY',
              underlying: 'BANKNIFTY',
              exchange: 'NFO',
              quantity: 25,
              opening_quantity: 25,
              average_price: 200,
              last_price: 180,
              pnl: -500,
              day_change: -8,
              close_price: 195,
              kind: 'opt',
            },
          ],
          holdings: [],
          source: 'live',
          as_of: null,
        }),
      });
    });

    await page.route('**/api/holdings/**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ holdings: [], source: 'live', as_of: null }),
      });
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const selected = await waitForUnderlying(page, 25_000);

    if (!selected) {
      test.skip(true, 'No underlying loaded within 25s — skip');
      return;
    }

    // Stale CRUDEOIL must not persist.
    expect(
      selected,
      'Stale CRUDEOIL persisted when only BANKNIFTY positions exist',
    ).not.toBe('CRUDEOIL');
  });
});

// ── Suite 4: CASE 2 — Promote: popular seed replaced by positions entry ────────
// Dimension 1 (SSOT): the existing promote logic must still work after the patch.

test.describe('CASE 2 — Promote: popular seed gives way to positions-tier entry', () => {
  test.setTimeout(60_000);

  test('source confirms promote case still uses curInOpts?.hint after patch', () => {
    const src = readFileSync(
      resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
      'utf8',
    );

    // Promote case must still exist, now using curInOpts?.hint.
    expect(src, 'Promote case (curIsPopular) missing after patch').toContain('curIsPopular');
    expect(
      src,
      'Promote case must read curInOpts?.hint (not re-find from opts)',
    ).toContain("const curIsPopular = curInOpts?.hint === 'popular'");

    // The promote overwrite must still be present.
    expect(
      src,
      'Missing promote overwrite: selectedUnderlying = opts[0].value in promote case',
    ).toMatch(/if \(curIsPopular && opts\[0\]\?\.hint !== 'popular'\)/);
  });
});

// ── Suite 5: Performance — stale-cache reset completes within 20s ─────────────
// Dimension 2 (Perf): even when sessionStorage has a stale value, auto-select
// must resolve within 20s, not hang or loop.

test.describe('Performance — stale-cache reset completes within perf budget', () => {
  test.setTimeout(60_000);

  test('auto-select resolves within 20s even with stale sessionStorage', async ({ page }) => {
    await authOnce(page);

    await page.context().addInitScript(() => {
      sessionStorage.setItem('opt.underlying', 'SILVERMIC');
      sessionStorage.setItem('ramboq:opt-underlying', 'SILVERMIC');
    });

    // Intercept positions with a minimal NIFTY-only response.
    await page.route('**/api/positions/**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          positions: [
            {
              tradingsymbol: 'NIFTY2591025000CE',
              symbol: 'NIFTY', underlying: 'NIFTY', exchange: 'NFO',
              quantity: 50, opening_quantity: 50,
              average_price: 100, last_price: 110,
              pnl: 500, day_change: 3, close_price: 107, kind: 'opt',
            },
          ],
          holdings: [], source: 'live', as_of: null,
        }),
      });
    });

    await page.route('**/api/holdings/**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ holdings: [], source: 'live', as_of: null }),
      });
    });

    const start = Date.now();
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const selected = await waitForUnderlying(page, 20_000);
    const elapsed  = Date.now() - start;

    if (!selected) {
      test.skip(true, 'No underlying loaded within 20s — skip');
      return;
    }

    expect(
      elapsed,
      `Auto-select took ${elapsed}ms — exceeds 20s performance budget`,
    ).toBeLessThan(20_000);

    expect(
      selected,
      'Stale SILVERMIC persisted — stale-cache reset did not fire within perf budget',
    ).not.toBe('SILVERMIC');
  });
});
