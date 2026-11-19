/**
 * swr_derivatives_strategy_spot_reload_survival.spec.js
 *
 * PLAN.md Part B5 (SWR extension), surface 3 of 3 — Derivatives strategy
 * + underlying spot.
 *
 * Features under test:
 *   B3a — /admin/derivatives's `strategy` snapshot moved from raw
 *         sessionStorage (per-tab, dies with the tab) to persistentCache.js
 *         (localStorage, survives reload / tab close) under the key
 *         `rbq.cache.ramboq:options-state`. The payload carries
 *         `strategy` and `selectedUnderlying` together.
 *   B3b — underlyingSpotStore persists each root to `rbq.cache.uspot.<ROOT>`
 *         and hydrates it inside loadUnderlyingSpots() before the network.
 *
 * Why a NEW TAB rather than a plain reload: the pre-B3 snapshot lived in
 * sessionStorage, which a same-tab reload DOES preserve. A same-tab reload
 * would therefore pass against pre-fix code and prove nothing. Opening the
 * page in a fresh tab of the same browser context reproduces the exact
 * gap B3 closes (sessionStorage is empty in the new tab; localStorage is
 * shared across tabs of one origin).
 *
 * Positive test (survival): strategy-analytics and batch-quote are HELD on
 * the fresh-tab load. The payoff chart must still render from the cached
 * strategy, and must not be stuck on "Resolving spot…".
 *
 * Isolation test (B3 invariant, `_strategyStale`): with underlying A cached,
 * the operator switches to underlying B on the fresh tab while B's
 * strategy is still held. The payoff must NOT render A's cached curve for B.
 * The cache is per-underlying in effect: the stored strategy carries its own
 * `underlying`, and `_strategyStale` (strategy.underlying !== selected)
 * gates display.
 *
 * Live data: the dev account holds GOLD MCX option legs, so the strategy
 * is real, not a fixture.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const STRATEGY_ROUTE = /\/api\/options\/strategy-analytics/;
const BATCH_ROUTE = /\/api\/quote\/batch/;
const OPTIONS_STATE_KEY = 'rbq.cache.ramboq:options-state';

/** Read the persisted derivatives snapshot payload (or null). */
async function readOptionsState(page) {
  return page.evaluate((key) => {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return null;
      return JSON.parse(raw).value ?? null;
    } catch { return null; }
  }, OPTIONS_STATE_KEY);
}

async function underlyingLabel(page) {
  return ((await page.locator('#opt-und .rbq-select-label').textContent()) || '').trim().toUpperCase();
}

/**
 * Load the derivatives page in `page` with live data, wait for the payoff
 * chart, and return the underlying it resolved to. Waits until the
 * Tier-2 snapshot actually holds a strategy for that underlying, so the
 * next (fresh-tab) load has something to restore.
 */
async function primeCache(page) {
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.payoff-svg').first()).toBeVisible({ timeout: 60_000 });
  // The auto-selected underlying can still flip (e.g. NIFTY -> GOLD once
  // positions land), so compare the live label and the cached strategy's
  // underlying in the SAME poll rather than reading the label once.
  let underlying = null;
  await expect
    .poll(async () => {
      const label = await underlyingLabel(page);
      const st = await readOptionsState(page);
      const cached = st?.strategy?.underlying ? String(st.strategy.underlying).toUpperCase() : null;
      underlying = label && label === cached ? label : null;
      return underlying;
    }, { timeout: 30_000 })
    .not.toBeNull();
  return underlying;
}

/** Option text is "GOLD OPTIONS" etc.; the trigger label is just the root. */
function rootOf(text) {
  return String(text || '').trim().toUpperCase().split(/\s+/)[0] || '';
}

/**
 * Geometry fingerprint of the rendered payoff chart: every path `d` inside
 * the `.payoff-svg`, joined. Two different underlyings' curves cannot share
 * this string; the same curve drawn under a new label would.
 */
async function payoffFingerprint(page) {
  return page.evaluate(() => {
    const svg = document.querySelector('.payoff-svg');
    if (!svg) return '';
    return [...svg.querySelectorAll('path')].map((p) => p.getAttribute('d') || '').join('|');
  });
}

test.describe('SWR reload survival — Derivatives strategy + spot (B3)', () => {
  test('fresh tab paints the cached payoff chart before strategy-analytics lands', async ({ context }) => {
    // ── 1. Prime Tier-2 in tab 1 ──────────────────────────────────────
    const page1 = await context.newPage();
    await loginAsAdmin(page1);
    const underlyingA = await primeCache(page1);
    // Spot is persisted per root on the same pass (B3b).
    await expect
      .poll(() => page1.evaluate((root) => !!localStorage.getItem(`rbq.cache.uspot.${root}`), underlyingA), { timeout: 20_000 })
      .toBe(true);
    await page1.close();

    // ── 2. Fresh tab, network held ────────────────────────────────────
    const page2 = await context.newPage();
    await loginAsAdmin(page2);
    const held = [];
    await page2.route(STRATEGY_ROUTE, (route) => { held.push(route); });
    await page2.route(BATCH_ROUTE, (route) => { held.push(route); });

    await page2.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    // Cached chart is on screen while the strategy request is still pending.
    await expect(page2.locator('.payoff-svg').first()).toBeVisible({ timeout: 20_000 });
    expect(held.length, 'strategy/quote requests should still be held when cached chart paints').toBeGreaterThan(0);
    await expect(page2.getByText('Resolving spot…')).toHaveCount(0);
    // A placeholder chart is also an .payoff-svg on a cold tab (no cached
    // strategy), so visibility alone proves nothing. The restore is proven
    // by the cached underlying coming back and no payoff placeholder copy.
    expect(await underlyingLabel(page2)).toBe(underlyingA);
    await expect(
      page2.getByText(/Only equity holdings for|Pick legs to see payoff|No payoff data/),
    ).toHaveCount(0);

    // Release everything so the spec leaves no hanging requests.
    await Promise.all(held.map((route) => route.continue().catch(() => {})));
  });

  test('switching underlying on a fresh tab never renders the cached underlying strategy', async ({ context }) => {
    // ── 1. Prime Tier-2 with underlying A ─────────────────────────────
    const page1 = await context.newPage();
    await loginAsAdmin(page1);
    const underlyingA = await primeCache(page1);
    await page1.close();

    // ── 2. Fresh tab, network held, switch to B ───────────────────────
    const page2 = await context.newPage();
    await loginAsAdmin(page2);
    const held = [];
    await page2.route(STRATEGY_ROUTE, (route) => { held.push(route); });
    await page2.route(BATCH_ROUTE, (route) => { held.push(route); });

    await page2.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await expect(page2.locator('.payoff-svg').first()).toBeVisible({ timeout: 20_000 });
    expect(await underlyingLabel(page2)).toBe(underlyingA);
    const cachedFingerprint = await payoffFingerprint(page2);
    expect(cachedFingerprint, 'cached ' + underlyingA + ' chart should have path geometry').not.toBe('');

    // Pick any real option that is not A.
    await page2.locator('button#opt-und').click();
    const options = page2.locator('.rbq-select-panel li[role="option"]');
    await expect(options.first()).toBeVisible({ timeout: 10_000 });
    let target = null;
    const count = await options.count();
    for (let i = 0; i < count; i++) {
      const root = rootOf(await options.nth(i).textContent());
      if (root && root !== underlyingA && !/PICK|LOADING|NO|OPTIONS/.test(root)) { target = options.nth(i); break; }
    }
    expect(target, 'derivatives page should offer a second underlying besides ' + underlyingA).not.toBeNull();
    const targetText = rootOf(await target.textContent());
    await target.click();
    await expect.poll(() => underlyingLabel(page2), { timeout: 10_000 }).toBe(targetText);

    // B's strategy request is pending (held), so B has no strategy of its
    // own yet. The chart must no longer be GOLD's cached curve: either the
    // chart is gone / "Resolving spot…", or its geometry has changed.
    await expect.poll(() => held.length, { timeout: 10_000 }).toBeGreaterThan(0);
    await expect
      .poll(async () => {
        const fp = await payoffFingerprint(page2);
        const resolving = await page2.getByText('Resolving spot…').isVisible().catch(() => false);
        return fp === '' || resolving || fp !== cachedFingerprint;
      }, {
        timeout: 10_000,
        message: `payoff must not keep showing ${underlyingA}'s cached curve while selected underlying is ${targetText}`,
      })
      .toBe(true);

    await Promise.all(held.map((route) => route.continue().catch(() => {})));
  });
});
