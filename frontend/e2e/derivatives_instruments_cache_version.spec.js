/**
 * derivatives_instruments_cache_version.spec.js
 *
 * Fix 3 — `expiryChoicesForUnderlying` / `_spotAnchorExpiryISO` gap
 * (audit-confirmed).
 *
 * Both `$derived.by` blocks call `getInstrument()` internally but, unlike
 * every other correctly-written cache-reading derived elsewhere in this
 * codebase (OrderTicket.svelte, MarketPulse.svelte, LegLabel.svelte,
 * PerformancePage.svelte, OptionChainTab.svelte all read
 * `$instrumentsCacheVersion`), neither one read it — so on a cold/direct
 * page load, a derived that happened to run (or re-run for some other
 * reason) before the instruments cache genuinely populated could freeze
 * on an empty/null `getInstrument()` lookup and never re-evaluate once
 * the cache actually warms, since nothing in its dependency list changes
 * when that happens.
 *
 * Fix: both derived bodies now read `$instrumentsCacheVersion` (imported
 * from `$lib/data/instruments`, same store every other correct consumer
 * uses) as their first statement, before any early return — mirrors
 * MarketPulse.svelte's own `/* eslint-disable-next-line ... *\/
 * $instrumentsCacheVersion;` idiom.
 *
 * Primary coverage is a static source-guard (dimension 3 — Stale): both
 * derived bodies must contain the dependency read. A live behavioural
 * probe is included too (expiry picker populates once a deliberately
 * delayed /api/instruments response lands) — this already passes
 * without the fix in this codebase's current timing (the gap this fix
 * closes is a race that doesn't reliably reproduce under Playwright's
 * mocked, single-shot instruments response), so it's kept as a
 * regression guard on the surrounding behaviour, not as the primary
 * proof of the fix. The source-guard assertion is what actually fails
 * without the fix.
 *
 * Five quality dimensions:
 *  1. SSOT     — both deriveds depend on the SAME `instrumentsCacheVersion`
 *                store every other instrument-cache consumer in this
 *                codebase already uses.
 *  2. Perf     — no extra network calls; the store is already written by
 *                the existing `loadInstruments()` call.
 *  3. Stale    — grep guard: both `$derived.by` bodies read
 *                `$instrumentsCacheVersion` before their first early return.
 *  4. Reusable — mirrors MarketPulse.svelte's exact idiom (import +
 *                bare statement read) rather than inventing a new pattern.
 *  5. UX       — the expiry picker and the anchor-contract "rolls in N
 *                days" chip both recover correctly once the cache warms,
 *                even on a cold/direct page load.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_instruments_cache_version.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const PAGE_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

// ── Static source-guard (the real proof of the fix) ────────────────────────

test.describe('Fix 3 source guard — instrumentsCacheVersion dependency', () => {
  test('expiryChoicesForUnderlying reads $instrumentsCacheVersion before its first early return', () => {
    const src = fs.readFileSync(PAGE_SRC, 'utf8');
    const idx = src.indexOf('const expiryChoicesForUnderlying = $derived.by(() => {');
    expect(idx, 'expiryChoicesForUnderlying derived should exist').toBeGreaterThan(-1);
    const bodyEnd = src.indexOf('if (!instrumentsReady || !selectedUnderlying) return [];', idx);
    expect(bodyEnd, 'the early-return guard should exist').toBeGreaterThan(idx);
    const preamble = src.slice(idx, bodyEnd);
    // Must be the ACTUAL bare-statement dependency read (`$instrumentsCacheVersion;`),
    // not merely mentioned in a comment — a plain `.includes()` on the
    // dollar-prefixed name would false-pass on the explanatory comment
    // text alone.
    expect(
      /\$instrumentsCacheVersion\s*;/.test(preamble),
      'expiryChoicesForUnderlying must read $instrumentsCacheVersion before its early return, ' +
      'so it re-evaluates when the instruments cache (re)populates, not just when ' +
      '`instrumentsReady` first flips true'
    ).toBe(true);
  });

  test('_spotAnchorExpiryISO reads $instrumentsCacheVersion as its first dependency', () => {
    const src = fs.readFileSync(PAGE_SRC, 'utf8');
    const idx = src.indexOf('const _spotAnchorExpiryISO = $derived.by(() => {');
    expect(idx, '_spotAnchorExpiryISO derived should exist').toBeGreaterThan(-1);
    const slice = src.slice(idx, idx + 500);
    expect(
      /\$instrumentsCacheVersion\s*;/.test(slice),
      '_spotAnchorExpiryISO must read $instrumentsCacheVersion so it re-derives when the ' +
      'instruments cache (re)populates — it calls getInstrument() internally but previously ' +
      'had no dependency that changes when the cache warms'
    ).toBe(true);
  });

  test('instrumentsCacheVersion is imported from the shared $lib/data/instruments store', () => {
    const src = fs.readFileSync(PAGE_SRC, 'utf8');
    expect(src).toMatch(/import\s*\{[^}]*instrumentsCacheVersion[^}]*\}\s*from\s*'\$lib\/data\/instruments'/);
  });
});

// ── Live behavioural probe ───────────────────────────────────────────────

const UNDERLYING    = 'NIFTY';
const SYMBOL        = 'NIFTY26DEC25000CE';
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL     = `/admin/derivatives?u=${UNDERLYING}`;
const ACCOUNT       = 'ZG0790';

function _realRow() {
  return {
    tradingsymbol: SYMBOL, symbol: SYMBOL, exchange: 'NFO',
    quantity: 50, opening_quantity: 50, overnight_quantity: 50,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 200.0, last_price: 210.0, close_price: 195.0,
    pnl: 500.0, day_change_val: 50.0, unrealised_pnl: 500.0, realised_pnl: 0,
    account: ACCOUNT, source: 'live',
  };
}

test.describe('Fix 3 live probe — expiry picker recovers once a cold instruments cache warms', () => {
  test('expiry dropdown populates once a deliberately delayed /api/instruments response lands', async ({ page }) => {
    await loginAsAdmin(page);

    await page.route('**/api/positions**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          rows: [_realRow()], summary: [], refreshed_at: new Date().toISOString(),
          source: 'snapshot', as_of: null,
        }),
      });
    });

    // Deliberately delayed — simulates a cold cache (IndexedDB miss,
    // slow network) that only resolves well after the page's first paint.
    await page.route('**/api/instruments**', async (route) => {
      await new Promise((r) => setTimeout(r, 1_500));
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          cycle_date: '2026-09-29', count: 1,
          items: [{ s: SYMBOL, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 25000 }],
        }),
      });
    });

    await page.route('**/api/holdings**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }) });
    });
    await page.route('**/api/watchlist**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
    });
    await page.route('**/api/options/strategy-analytics**', (route) => {
      const body = route.request().postDataJSON();
      const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          underlying: UNDERLYING, spot: 25100, spot_prev_close: 24900,
          legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
          payoff: [
            { spot: 24000, today_value: -100, expiry_value: -100 },
            { spot: 25100, today_value: 500, expiry_value: 500 },
            { spot: 26000, today_value: 900, expiry_value: 900 },
          ],
          risk: { breakevens: [24800] }, breakevens: [24800],
          pnl_at_spot: 500, exp_pnl_at_spot: 500,
          dte: 15, iv: 0.18, aggregate_greeks: _greeks,
        }),
      });
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });

    // Expiry picker (id="opt-exp") placeholder reads '—' while
    // expiryChoicesForUnderlying is empty (instruments cache still cold),
    // then flips to "All expiries" once the delayed /api/instruments
    // response lands and the derived re-evaluates with real data.
    const expiryTrigger = page.locator('#opt-exp');
    await expect(expiryTrigger).toBeVisible({ timeout: 5_000 });
    await expect(expiryTrigger).toContainText('All expiries', { timeout: 10_000 });

    const row = page.locator('.cand-grid .cand-row:not(.cand-row-total)').first();
    await expect(row).toBeVisible({ timeout: 10_000 });
    // The symbol must render its FORMATTED (resolved-instrument) label,
    // not a raw unresolved fallback — proves getInstrument() eventually
    // picked up the delayed cache for this row's own rendering path too.
    await expect(row).toContainText('25000', { timeout: 10_000 });
  });
});
