/**
 * derivatives_greeks_ev_guard_zero_dash.spec.js
 *
 * `_gbs_greeks()` / `expected_value()` (backend/api/algo/derivatives.py)
 * return a literal `0.0` for gamma/theta/vega/rho/EV whenever every leg's
 * time-to-expiry has reached T<=0 — a mathematical GUARD for "nothing left
 * to decay", never a genuine computation. Delta is the one exception: it
 * keeps a real intrinsic-direction value (+1/-1/0) at T=0, which is still
 * meaningful. Before the fix this spec guards, the guard-0.0 rendered
 * identically to a genuinely-computed exact zero — an operator couldn't
 * tell "this position is Greeks-neutral" from "the backend never computed
 * real Greeks for this leg".
 *
 * Fix (frontend-only, no backend contract change — `OptionGreeks` /
 * `StrategyRisk.ev` stay plain non-nullable `float` in the schema, since
 * other consumers of the same Greeks-computation functions — positions.py's
 * per-position Greeks enrichment, the option-chain snapshot IV/Greeks —
 * must keep getting real numbers): `+page.svelte`'s `_allLegsAtExpiry`
 * derived gates on `strategy.days_to_expiry <= 0` (already in the payload,
 * no new field), and `_baseGreeksDisplay` / `_baseEvDisplay` /
 * `_baseEvPctDisplay` null out gamma/theta/vega/rho/ev/ev_pct (never delta)
 * in that case. `format.js`'s existing `pctFmt`/`fmtMoney` already render
 * `null` as `—`.
 *
 * This spec intercepts `/api/options/strategy-analytics` and swaps in a
 * synthetic response with `days_to_expiry: 0` and the real backend's
 * guard-zero shape (gamma/theta/vega/rho/ev/ev_pct all `0.0`, delta a real
 * nonzero number) — the exact payload the real backend sends once every
 * leg has reached its own expiry. The real request still goes out (real
 * account, real legs), so the fixture's `underlying`/`legs[]` match what
 * the page actually asked for; only the numeric analytics fields are
 * overridden.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const STRATEGY_ROUTE = /\/api\/options\/strategy-analytics/;

/** Build a guard-zero StrategyResponse from the real request body, so the
 *  `underlying` / `legs[]` / `payoff` shape matches what the page asked
 *  for while the Greeks/EV fields are forced into the backend's real
 *  T<=0 guard shape. */
function guardZeroResponseFor(reqBody) {
  const legs = (reqBody.legs || []).map((l, i) => ({
    symbol: l.symbol,
    opt_type: /PE$/i.test(l.symbol) ? 'PE' : 'CE',
    strike: 100000 + i * 1000,
    qty: l.qty,
    avg_cost: l.avg_cost ?? 100,
    ltp: l.ltp ?? 100,
    iv: 0.3,
    theoretical: 100,
    discrepancy: 0,
    // Per-leg greeks: same guard-zero shape as the aggregate, since a
    // real backend response at T<=0 nulls every leg this way too.
    greeks: { delta: l.qty > 0 ? 1 : -1, gamma: 0, theta: 0, vega: 0, rho: 0 },
    ltp_source: 'live',
    iv_source: 'calibrated',
  }));
  const spot = 100000;
  return {
    underlying: 'GOLD',
    expiry: '2026-01-01',
    days_to_expiry: 0,
    spot,
    net_cost: 1000,
    net_qty: legs.reduce((s, l) => s + l.qty, 0),
    iv_proxy: 0.3,
    // Guard-zero aggregate: delta carries a real nonzero intrinsic
    // direction, every other Greek is the backend's literal 0.0.
    aggregate_greeks: { delta: 42, gamma: 0, theta: 0, vega: 0, rho: 0 },
    risk: {
      max_profit: 5000,
      max_loss: -5000,
      breakevens: [spot],
      pop: 0.5,
      ev: 0,
      ev_pct: 0,
      rr_ratio: 1,
    },
    payoff: [
      { spot: spot * 0.9, today_value: -5000, expiry_value: -5000 },
      { spot, today_value: 0, expiry_value: 0 },
      { spot: spot * 1.1, today_value: 5000, expiry_value: 5000 },
    ],
    intermediate_curves: [],
    legs,
    spot_prev_close: spot,
    span_pct: 0.1,
    span_sigmas: 3,
    multi_expiry: false,
    spot_anchor_contract: null,
    spot_source: 'live',
  };
}

/** Numeric text of a greek-val-trigger button, by its aria-controls id
 *  ('greek-hint-ev' | '-delta' | '-gamma' | '-theta' | '-vega' | '-rho'). */
async function chipText(page, controlsId) {
  return (await page.locator(`button.greek-val-trigger[aria-controls="${controlsId}"]`).first().textContent())?.trim();
}

test.describe('Derivatives EV/Greeks chips — guard-zero T<=0 renders em-dash, not 0.00', () => {
  test('EV, Gamma, Theta, Vega, Rho show "—"; Delta keeps its real value', async ({ page }) => {
    let sawRequest = false;
    await page.route(STRATEGY_ROUTE, async (route) => {
      const reqBody = route.request().postDataJSON();
      sawRequest = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(guardZeroResponseFor(reqBody)),
      });
    });

    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.payoff-svg').first()).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => sawRequest, { timeout: 30_000 }).toBe(true);

    // The header chip row — the primary surface the task names.
    await expect.poll(() => chipText(page, 'greek-hint-ev'), { timeout: 10_000 }).toBe('—');
    expect(await chipText(page, 'greek-hint-gamma')).toBe('—');
    expect(await chipText(page, 'greek-hint-theta')).toBe('—');
    expect(await chipText(page, 'greek-hint-vega')).toBe('—');
    expect(await chipText(page, 'greek-hint-rho')).toBe('—');
    // Delta is the one exception — real intrinsic direction, never nulled.
    const deltaText = await chipText(page, 'greek-hint-delta');
    expect(deltaText).not.toBe('—');
    expect(Number(deltaText)).toBeCloseTo(42, 0);

    // Second consumer of the SAME `_mergedGreeks`/`_mergedEv` derived
    // values — the Greeks / Risk & EV cards further down the page.
    await expect(page.locator('.opt-kv-greeks .kv-v').nth(1)).toHaveText('—'); // gamma
    await expect(page.locator('.opt-kv-greeks .kv-v').nth(2)).toHaveText('—'); // theta
    await expect(page.locator('.opt-kv-greeks .kv-v').nth(3)).toHaveText('—'); // vega
    await expect(page.locator('.opt-kv-greeks .kv-v').nth(4)).toHaveText('—'); // rho
    await expect(page.locator('.opt-kv-greeks .kv-v').first()).not.toHaveText('—'); // delta
  });
});
