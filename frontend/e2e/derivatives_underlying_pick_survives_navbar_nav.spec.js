/**
 * derivatives_underlying_pick_survives_navbar_nav.spec.js
 *
 * Regression test for a confirmed live bug on /admin/derivatives:
 * navigating away via the navbar's "Derivatives" link (which points at the
 * bare `/admin/derivatives` href, no `?u=` query) and back silently
 * discarded the operator's manually-picked underlying, reverting to
 * whichever underlying the auto-select heuristic (largest position) would
 * choose instead. No error, no indication to the operator. Confirmed via
 * URL: `?u=BHEL` (operator's manual pick) before leaving -> `?u=CRUDEOIL`
 * (auto-selected) after return.
 *
 * Root cause: two mechanisms raced to set `selectedUnderlying` on mount —
 *   1. The auto-select `$effect` (declared ~line 1525) — fires whenever
 *      `selectedUnderlying` is falsy, picking the root with the largest
 *      position. Also contains a "one-time promote" branch that bounces a
 *      *non-active* pick (qtySum === 0 — e.g. a holdings-only root with no
 *      live F&O position, exactly BHEL's situation) to the largest-position
 *      underlying once positions finish loading, UNLESS `_autoSelectDone`
 *      is already true.
 *   2. The sessionStorage restore inside `_loadCache()`, called from the
 *      SECOND onMount (~line 4835+), gated on `!selectedUnderlying`.
 * Svelte 5's top-level effects/onMounts flush in creation order on initial
 * mount: onMount #1 (~line 340, URL-param seed) -> the auto-select $effect
 * (declared next in source) -> onMount #2 (~line 4835, `_loadCache()`). On
 * a bare navbar link (no `?u=`), the auto-select effect's `if (!cur)`
 * branch always won — by the time `_loadCache()` ran, `selectedUnderlying`
 * was already truthy (auto-picked), so the restore's own
 * `!selectedUnderlying` guard was already false and it silently no-opped.
 *
 * Fix: onMount #1 now also peeks the sessionStorage cache
 * (`_readCachedUnderlying()`) and seeds `selectedUnderlying` (+ marks
 * `_autoSelectDone = true`) from it BEFORE the auto-select effect gets its
 * first chance to run — closing the race at its source. The `?u=` URL-param
 * branch of the same onMount is given the identical `_autoSelectDone = true`
 * treatment (a bookmark/shared-link pick is just as explicit as a dropdown
 * click, and was exposed to the same one-time-promote bounce). The
 * auto-select effect's existing `curInOpts` stale-cache validation still
 * runs afterward and correctly rejects a genuinely stale pick (a root no
 * longer in the book), unchanged from before this fix — see
 * derivatives_payoff_default_underlying.spec.js's "Stale sessionStorage
 * fallback" suite, which this test does not duplicate.
 *
 * Test design note: `_saveCache()` (the function that actually persists
 * `selectedUnderlying` to sessionStorage) only fires on a successful
 * strategy fetch or an event-driven `loadPositions()` call (order fill,
 * postback, manual refresh) — never on the dropdown pick itself, and never
 * for a pick with no F&O legs and holdings-inclusion off (BHEL's exact
 * situation). Rather than depend on a live order fill firing during the
 * test (flaky, and not what this regression is actually about), this spec
 * seeds sessionStorage directly with a REAL dropdown value discovered from
 * the live page (not a fabricated symbol) to represent "a prior successful
 * save already happened" — precisely the precondition the bug report
 * describes (`?u=BHEL` was already live before the operator navigated
 * away).
 *
 * Two tests, covering two different mount paths that both touch the race:
 *   Test A — cold mount (tab reopen / hard reload) with the seed already
 *            in sessionStorage before the very first paint.
 *   Test B — the EXACT reported scenario: a REAL navbar `goto()` round
 *            trip (SPA client-side nav, not page.goto()/reload — the bug
 *            is specifically about the derivatives page's component
 *            remounting while module-level stores/sessionStorage stay warm
 *            across the navigation). The seed is written on the
 *            intermediate /pulse page, immediately before clicking back to
 *            Derivatives — after the derivatives component has already
 *            unmounted (its onDestroy tears down loadStrategy/loadPositions
 *            intervals), so nothing can race the seed by re-saving a
 *            different value over it before the return navigation lands.
 *
 * Run (against local dev — proxies /api to dev.ramboq.com, see
 * frontend/vite.config.js):
 *   npx playwright test e2e/derivatives_underlying_pick_survives_navbar_nav.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE      = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DERIV_URL = `${BASE}/admin/derivatives`;

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _token = null;

async function loginAsAdmin(page) {
  if (!_token) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: USER, password: PASS },
      headers: { 'Content-Type': 'application/json' },
    });
    if (!r.ok()) throw new Error(`loginAsAdmin: login failed (${r.status()})`);
    _token = (await r.json()).access_token;
  }
  await page.context().addInitScript((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
  }, _token);
}

const PLACEHOLDER_TEXTS = new Set([
  'PICK UNDERLYING…', 'LOADING UNDERLYINGS…', 'NO OPTIONS IN BOOK', '',
]);

async function getDisplayedUnderlying(page) {
  const label = page.locator('#opt-und .rbq-select-label');
  return (await label.textContent().catch(() => '') || '').trim().toUpperCase();
}

/** Cold-start auto-select is not monotonic: a provisional seed (e.g. the
 *  POPULAR_UNDERLYINGS[0] 'NIFTY' cold-start fallback) can paint BEFORE
 *  positions finish loading and the auto-select $effect promotes to the
 *  real largest-position root. Returns the value once it's stable
 *  (unchanged) for `quietMs`, not just the first non-placeholder sighting. */
async function waitForSettledUnderlying(page, { timeout = 20_000, quietMs = 2_500 } = {}) {
  const deadline = Date.now() + timeout;
  let last = '';
  let lastChangeAt = 0;
  while (Date.now() < deadline) {
    const text = await getDisplayedUnderlying(page);
    if (text && !PLACEHOLDER_TEXTS.has(text)) {
      if (text !== last) { last = text; lastChangeAt = Date.now(); }
      else if (Date.now() - lastChangeAt >= quietMs) return last;
    }
    await page.waitForTimeout(300);
  }
  return last;
}

/** Click a navbar button and confirm the URL actually changed, retrying the
 *  click once. This guards against an intermittent, NOT-fully-diagnosed
 *  navbar nav flake observed in ~1/3 of runs: the click visibly lands (the
 *  button shows `:active`) but `page.url()` never changes — i.e. the
 *  navigation itself is cancelled or superseded after starting, not a
 *  "click missed the element" timing issue. One candidate cause (not yet
 *  confirmed): the derivatives page's own debounced URL-sync effect
 *  (`_urlSyncTimer`, ~150ms) calls `goto(..., {replaceState:true})` and is
 *  only cleared in `onDestroy` — a timer still in flight at the exact
 *  moment of the navbar click could race/supersede the navbar's own
 *  `goto()`. This is unrelated to the fix under test (this helper's own
 *  callers never seed sessionStorage or touch `?u=` before the click that
 *  flakes) and needs separate investigation if it recurs. */
async function clickNavAndWait(page, label, urlPattern, timeout = 10_000) {
  const btn = page.locator(`button.algo-nav-btn:has-text("${label}")`).first();
  await btn.click();
  try {
    await expect.poll(() => page.url(), { timeout: timeout / 2 }).toMatch(urlPattern);
    return;
  } catch (_) { /* fall through to one retry */ }
  await btn.click();
  await expect.poll(() => page.url(), { timeout }).toMatch(urlPattern);
}

/** Poll the displayed underlying for `windowMs`, asserting it stays equal
 *  to `expected` on every tick — catches a late bounce (e.g. the one-time
 *  promote branch firing once positions finish loading), not just the
 *  first paint. */
async function assertStaysOn(page, expected, autoSelected, windowMs = 10_000) {
  const deadline = Date.now() + windowMs;
  while (Date.now() < deadline) {
    const displayed = await getDisplayedUnderlying(page);
    expect(
      displayed,
      `Manual pick "${expected}" must survive — got "${displayed}" (auto-select default would be "${autoSelected}")`,
    ).toBe(expected);
    await page.waitForTimeout(500);
  }
}

/** Phase 0 (shared by both tests): discover a realistic (autoSelected,
 *  manualPick) pair from the LIVE dropdown on a cold mount, nothing seeded
 *  yet. Returns `{ autoSelected, manualPick }`, or `{ manualPick: null }`
 *  when the book doesn't have a second distinct candidate (caller should
 *  skip). Leaves the page on DERIV_URL with the dropdown closed. */
async function discoverPickPair(page, testInfo) {
  await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });
  const trigger = page.locator('#opt-und');
  await expect(trigger).toBeVisible({ timeout: 10_000 });
  const autoSelected = await waitForSettledUnderlying(page);
  if (!autoSelected) return { autoSelected: null, manualPick: null };

  await trigger.click();
  const panel = page.locator('.rbq-select-panel').first();
  await expect(panel).toBeVisible({ timeout: 5_000 });
  const optionEls = panel.locator('li[role="option"]');

  // Prefer another real options/futures position (strongest, most
  // representative case); fall back to a holdings-tier root (the exact
  // BHEL/CRUDEOIL shape from the bug report — a position with no live F&O
  // legs, qtySum === 0, which also exercises the one-time-promote branch)
  // when the book only has one active F&O root. Deliberately EXCLUDES the
  // 'popular' tier: the auto-select effect has a SEPARATE, unconditional
  // (not `_autoSelectDone`-gated) promote rule for `hint === 'popular'` —
  // "a popular-tier pick is provisional, promote to any real position tier
  // the moment one appears" — existing, correct, by-design behaviour
  // (CASE 2 in derivatives_payoff_default.spec.js) that this test must not
  // be confused with the race it's actually guarding against.
  //
  // Holdings load via a slightly different, slower fetch than the
  // position/underlying label itself (pulseHoldingsStore, PositionStrip's
  // own cadence) — poll for up to 15s for a candidate tier to appear rather
  // than taking a single snapshot of the panel.
  let manualPick = null;
  const pickDeadline = Date.now() + 15_000;
  while (!manualPick && Date.now() < pickDeadline) {
    // The list is LIVE (positions/holdings stream in while this loop runs),
    // so a row can detach mid-read — tolerate a stale-element read on any
    // single row and move on to the next, rather than letting one flaky
    // read abort the whole discovery pass.
    let n = 0;
    try { n = await optionEls.count(); } catch (_) { n = 0; }
    for (const wantHints of [['options', 'futures'], ['holdings'], ['pinned', 'watchlist']]) {
      for (let i = 0; i < n; i++) {
        try {
          const labelEl = optionEls.nth(i).locator('.rbq-select-option-label');
          const label = (await labelEl.textContent({ timeout: 1_000 }) || '').trim().toUpperCase();
          const hint  = (await labelEl.getAttribute('data-hint', { timeout: 1_000 })) || '';
          if (label && label !== autoSelected && wantHints.includes(hint)) { manualPick = label; break; }
        } catch (_) { /* row detached mid-read — skip it */ }
      }
      if (manualPick) break;
    }
    if (!manualPick) await page.waitForTimeout(500);
  }
  await page.keyboard.press('Escape').catch(() => {});
  // Wait for the panel to actually detach before returning — a stray open
  // overlay can intercept the navbar click the caller does immediately
  // after this function returns, producing a flaky "click landed on
  // nothing" failure unrelated to the fix under test.
  await panel.waitFor({ state: 'hidden', timeout: 3_000 }).catch(() => {});
  return { autoSelected, manualPick };
}

function skipIfMobile(testInfo) {
  const vp = testInfo.project.use.viewport;
  if (vp && vp.width < 1024) {
    // Desktop-only: the inline `button.algo-nav-btn` links this test
    // clicks are hidden below the `lg` breakpoint (mobile uses a hamburger
    // drawer instead) — mirrors the existing desktop-only pattern in
    // e2e/navigation_feedback.spec.js.
    test.skip(true, 'desktop-only — algo-nav-btn hidden below lg breakpoint');
    return true;
  }
  return false;
}

test.setTimeout(90_000);

test.describe('Manual underlying pick survives the auto-select race', () => {
  test('Test A — cold mount with a previously-saved pick already in sessionStorage', async ({ page }, testInfo) => {
    if (skipIfMobile(testInfo)) return;

    await loginAsAdmin(page);
    const { autoSelected, manualPick } = await discoverPickPair(page, testInfo);
    if (!autoSelected) {
      test.skip(true, 'no underlying auto-selected within 20s — broker/positions unavailable');
      return;
    }
    if (!manualPick) {
      test.skip(true, 'only one underlying option available in the dropdown — cannot test a distinct manual pick');
      return;
    }

    // Seed sessionStorage with `manualPick`, representing "a prior
    // successful _saveCache() already persisted this pick".
    await page.context().addInitScript((pick) => {
      try {
        sessionStorage.setItem('ramboq:options-state', JSON.stringify({
          ts: Date.now(),
          selectedUnderlying: pick,
          strategy: null, drafts: [],
          selectedAccounts: [], selectedExpiries: [],
          enabledSymbols: {},
        }));
      } catch (_) { /* ignore */ }
    }, manualPick);

    // Fresh cold mount with the seed in place — this is the onMount-race
    // under test. Without the fix, the auto-select $effect's `if (!cur)`
    // branch wins and `manualPick` never survives even this first paint.
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 10_000 });
    await assertStaysOn(page, manualPick, autoSelected);
    await expect.poll(() => page.url(), { timeout: 5_000 }).toContain(`u=${manualPick}`);
  });

  test('Test B — a real navbar round trip (bare "Derivatives" link, no ?u=) keeps the pick', async ({ page }, testInfo) => {
    if (skipIfMobile(testInfo)) return;

    await loginAsAdmin(page);
    const { autoSelected, manualPick } = await discoverPickPair(page, testInfo);
    if (!autoSelected) {
      test.skip(true, 'no underlying auto-selected within 20s — broker/positions unavailable');
      return;
    }
    if (!manualPick) {
      test.skip(true, 'only one underlying option available in the dropdown — cannot test a distinct manual pick');
      return;
    }

    // Navigate away via a REAL navbar click (SvelteKit SPA `goto()`, not
    // page.goto()/reload) — this unmounts the derivatives page component,
    // which tears down its own loadStrategy/loadPositions intervals
    // (onDestroy). SvelteKit's `goto()` doesn't fire a traditional `load`
    // navigation event, so poll the URL rather than `page.waitForURL`'s
    // default `waitUntil: 'load'`.
    await clickNavAndWait(page, 'Pulse', /\/pulse/);

    // Seed sessionStorage HERE, on the /pulse page, right before clicking
    // back — the derivatives component is already unmounted, so nothing
    // can race this seed by re-saving a different value over it before the
    // return navigation lands (unlike seeding before the FIRST departure,
    // which would still be racing the still-mounted page's own 5s
    // loadStrategy/loadPositions cycles).
    await page.evaluate((pick) => {
      try {
        sessionStorage.setItem('ramboq:options-state', JSON.stringify({
          ts: Date.now(),
          selectedUnderlying: pick,
          strategy: null, drafts: [],
          selectedAccounts: [], selectedExpiries: [],
          enabledSymbols: {},
        }));
      } catch (_) { /* ignore */ }
    }, manualPick);

    // Navigate back via the BARE "Derivatives" navbar link — no `?u=`
    // query — exactly the regression scenario from the bug report.
    await clickNavAndWait(page, 'Derivatives', /\/admin\/derivatives/);

    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 10_000 });
    await assertStaysOn(page, manualPick, autoSelected);
  });

  test('Test C — a bookmarked ?u= URL param is not discarded by the same one-time-promote bounce', async ({ page }, testInfo) => {
    if (skipIfMobile(testInfo)) return;

    await loginAsAdmin(page);
    const { autoSelected, manualPick } = await discoverPickPair(page, testInfo);
    if (!autoSelected) {
      test.skip(true, 'no underlying auto-selected within 20s — broker/positions unavailable');
      return;
    }
    if (!manualPick) {
      test.skip(true, 'only one underlying option available in the dropdown — cannot test a distinct manual pick');
      return;
    }

    // No sessionStorage seed at all — this is the OTHER explicit-seed path
    // in onMount #1: `?u=<manualPick>` in the URL itself (a bookmark, a
    // shared link, or browser Back to a URL that still carries the query).
    // Same underlying mechanism as Test A/B's `_autoSelectDone` fix, via a
    // different entry point: without it, the auto-select effect's
    // one-time-promote branch (fires once positions load, since this pick's
    // qtySum is 0) would silently bounce it to the largest-position
    // underlying the moment positions resolve — same silent-discard
    // symptom, no sessionStorage race involved at all.
    await page.goto(`${DERIV_URL}?u=${manualPick}`, { waitUntil: 'domcontentloaded' });
    const trigger = page.locator('#opt-und');
    await expect(trigger).toBeVisible({ timeout: 10_000 });
    await assertStaysOn(page, manualPick, autoSelected);
  });
});
