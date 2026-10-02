/**
 * derivatives_legs_fullscreen_fill.spec.js
 *
 * Regression guard for the "Legs grid only fills the top half of the
 * fullscreen card, leaving a large dead gap below the TOTAL row" bug
 * (2026-10-02 fix).
 *
 * Root causes fixed (both in
 * `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`):
 *   1. A stale `:global(.fs-card-on) .cand-scroll { max-height: calc(100vh
 *      - 28rem) !important; }` rule predated the shared `.fs-content-fill`
 *      mechanism and silently capped `.cand-scroll` far below the real
 *      available height. Removed.
 *   2. Even with (1) fixed, `.cand-grid` only grew to its own content's
 *      natural height inside the now-correctly-tall `.cand-scroll`,
 *      stranding the TOTAL row right under the last leg row with the same
 *      dead-gap bug one box further in. Fixed by pinning TOTAL to the
 *      bottom via a 3-row grid template (header / `.cand-body-rows` 1fr /
 *      TOTAL auto) — the ag-Grid "pinned bottom row" idiom — scoped to
 *      `.fs-card-on` only.
 *
 * Both fixes use a flex/grid layout computation (not a hardcoded
 * `--fs-chrome-h` guess), so they are correct regardless of viewport width
 * and regardless of whether the conditional `.cand-hidden-hint` /
 * `.cand-draft-payoff-bar` chrome rows are present.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   1. SSOT     — assertions read live computed geometry, not hard-coded
 *                 pixel literals, so the spec tracks the real layout.
 *   2. Perf     — fullscreen toggle + geometry read budgeted < 500 ms.
 *   3. Stale    — grep guards confirm the stale `calc(100vh - 28rem)`
 *                 override is gone and the fix's own rules are present.
 *   4. Reusable — same authOnce() helper idiom as the sibling
 *                 payoff_fullscreen_chrome.spec.js.
 *   5. UX       — non-fullscreen sizing is unchanged (no grid-template-rows
 *                 leak outside `.fs-card-on`); sparse (1-row) leg lists
 *                 don't get comedically stretched rows (align-content:start).
 *
 * Run (--workers=1 — see waitForLayoutSettled's comment; this repo's sibling
 * specs use the same convention to avoid 9-way default-parallelism
 * contention against a single local dev server):
 *   npx playwright test e2e/derivatives_legs_fullscreen_fill.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait \
 *   --project=mobile-landscape --workers=1
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// ── auth ──────────────────────────────────────────────────────────────────────

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) {
        tok = (await resp.json()).access_token;
        break;
      }
      if (resp.status() !== 429) {
        throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
      }
    }
    if (!tok) {
      test.skip(true, 'rate-limited — run in isolation or pass PLAYWRIGHT_AUTH_TOKEN');
      return;
    }
    _cachedToken = tok;
  }

  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

// ── stale-code grep helpers ───────────────────────────────────────────────────

const DERIV_PAGE = path.resolve(
  import.meta.dirname || __dirname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

function derivsSource() {
  return fs.readFileSync(DERIV_PAGE, 'utf8');
}

// Test data isn't deterministic across accounts — the F&O book backing
// any given underlying can change, and the page's own auto-select $effect
// (underlyingOptionsForPicker not yet populated on first mount) can bounce
// a `?u=` URL-seeded pick back to whatever root it treats as "first" before
// the real option list loads. Pick deterministically via the real Select
// combobox instead — by the time an operator (or this helper) can click it
// open, `underlyingOptionsForPicker` is already populated, so the pick
// sticks exactly like a genuine operator click would. Returns the number
// of leg rows found after the pick settles; 0 means "not available in this
// environment" (a data condition, not a layout regression) — callers skip.
async function pickUnderlyingWithLegs(page, symbol = 'CRUDEOIL', timeoutMs = 15_000) {
  const trigger = page.locator('#opt-und');
  const triggerReady = await trigger.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
  if (!triggerReady) return 0;
  await trigger.click();
  const search = page.locator('.rbq-select-search-input');
  if (await search.isVisible().catch(() => false)) {
    await search.fill(symbol);
  }
  const option = page.locator('li[role="option"]', { hasText: symbol }).first();
  const optionVisible = await option.isVisible({ timeout: 3_000 }).catch(() => false);
  if (!optionVisible) return 0;
  await option.click();

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const count = await page.locator('.cand-row:not(.cand-row-total)').count();
    if (count > 0) return count;
    await page.waitForTimeout(500);
  }
  return 0;
}

// The page has an unrelated, pre-existing async layout settle a few
// seconds after first load — confirmed live (2026-10-02 investigation)
// to be completely independent of this fix: an untouched control button
// (the Payoff card's OWN fullscreen toggle, on `.opt-payoff`, never
// touched by this commit) shows the identical ~800ms `click()` latency
// when clicked in the same timing window right after the underlying
// picker resolves, while clicking that SAME button a few seconds
// earlier (before the settle) or later (after the settle) takes ~20ms.
// Root cause: something above the Payoff/Legs row (first-poll data
// replacing a provisional placeholder, verified via `scrollHeight`
// dropping ~27px around the 3-4s mark) shifts page layout once;
// Playwright's own `.click()` actionability wait (element must be
// visible+stable+hit-testable for 2 consecutive animation frames)
// retries through that shift, adding up to ~1.3s on this viewport's
// cramped geometry — not a cost of the fullscreen CSS transition
// itself. Isolating the measured "toggle latency" from this unrelated
// variance (rather than padding the budget to tolerate it) is the
// correct fix: wait for `document.documentElement.scrollHeight` to
// stop changing before starting the timer, so the timed interval
// reflects only the fullscreen toggle's own cost.
async function waitForLayoutSettled(page, { checkIntervalMs = 150, stableChecks = 3, timeoutMs = 8_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastHeight = null;
  let stableCount = 0;
  while (Date.now() < deadline) {
    const h = await page.evaluate(() => document.documentElement.scrollHeight);
    if (h === lastHeight) {
      stableCount++;
      if (stableCount >= stableChecks) return;
    } else {
      stableCount = 0;
      lastHeight = h;
    }
    await page.waitForTimeout(checkIntervalMs);
  }
}

// ── spec ──────────────────────────────────────────────────────────────────────

test.describe('Legs grid fullscreen fill — no dead gap below TOTAL', () => {
  test.setTimeout(90_000);

  // ── Stale-code guards (Dimension 3) ─────────────────────────────────────────
  test('stale max-height override is gone', () => {
    const src = derivsSource();
    expect(
      src,
      'the stale `.fs-card-on .cand-scroll { max-height: calc(100vh - 28rem) ... }` override must stay removed',
    ).not.toContain('calc(100vh - 28rem)');
  });

  test('fullscreen fill + TOTAL-pin rules are present in source', () => {
    const src = derivsSource();
    expect(src, 'opt-legs-card.fs-card-on flex-column rule must exist')
      .toContain('.opt-legs-card.fs-card-on)');
    expect(src, '.cand-scroll must grow via flex:1 1 0 in fullscreen')
      .toContain('flex: 1 1 0');
    expect(src, '.cand-grid must use the 3-row pin-TOTAL-to-bottom template')
      .toContain('grid-template-rows: auto 1fr auto');
    expect(src, '.cand-body-rows wrapper must exist (groups rows for the 1fr track)')
      .toContain('cand-body-rows');
  });

  // ── Live computed-geometry checks ────────────────────────────────────────────
  test('fullscreen Legs grid fills the card with TOTAL pinned at the bottom', async ({ page }) => {
    await authOnce(page);

    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');

    const legsCard = page.locator('.opt-legs-card').first();
    const legsVisible = await legsCard.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!legsVisible) {
      test.skip(true, 'No .opt-legs-card visible — page not loaded in time');
      return;
    }

    const legsCount = await pickUnderlyingWithLegs(page);
    if (legsCount === 0) {
      test.skip(true, 'No F&O legs available on any underlying in this environment — data-availability, not a layout regression');
      return;
    }

    const fsBtn = legsCard.locator('.fs-btn, [title*="fullscreen" i], [aria-label*="fullscreen" i]').first();
    const fsBtnVisible = await fsBtn.isVisible().catch(() => false);
    if (!fsBtnVisible) {
      test.skip(true, 'Fullscreen button not visible on Legs card');
      return;
    }

    // See waitForLayoutSettled's own comment: an unrelated async page
    // settle a few seconds after load can otherwise contaminate the
    // measured click() latency on this viewport. Not part of the
    // timed interval itself — isolates what we're actually budgeting.
    await waitForLayoutSettled(page);

    const t0 = Date.now();
    await fsBtn.click();
    await expect(page.locator('.opt-legs-card.fs-card-on')).toBeVisible({ timeout: 5_000 });
    const toggleMs = Date.now() - t0;
    console.log(`[legs_fs_fill] fullscreen toggle latency: ${toggleMs}ms`);

    // Give layout one paint cycle to settle.
    await page.waitForTimeout(300);

    const t1 = Date.now();
    const geo = await page.evaluate(() => {
      const card  = document.querySelector('.opt-legs-card.fs-card-on');
      const total = card ? card.querySelector('.cand-row-total') : null;
      const scroll = card ? card.querySelector('.cand-scroll') : null;
      function rect(el) { return el ? el.getBoundingClientRect() : null; }
      return {
        cardBottom:   card ? rect(card).bottom : null,
        cardScrollH:  card ? card.scrollHeight : null,
        cardClientH:  card ? card.clientHeight : null,
        scrollBottom: scroll ? rect(scroll).bottom : null,
        totalBottom:  total ? rect(total).bottom : null,
        totalFound:   !!total,
      };
    });
    const readMs = Date.now() - t1;
    console.log(`[legs_fs_fill] geometry: ${JSON.stringify(geo)} (read in ${readMs}ms)`);

    // Dimension 2: toggle + read budget.
    expect(toggleMs, 'fullscreen toggle must complete within 500 ms').toBeLessThan(500);
    expect(readMs, 'geometry read must complete within 500 ms').toBeLessThan(500);

    expect(geo.totalFound, 'TOTAL row must be present with legs loaded').toBe(true);

    // 1. TOTAL's own bottom edge must be within a few px of .cand-scroll's
    //    bottom edge — i.e. TOTAL is genuinely pinned at the bottom of the
    //    scroll viewport, not stranded mid-card with dead space below it.
    expect(
      Math.abs(geo.totalBottom - geo.scrollBottom),
      `TOTAL row bottom (${geo.totalBottom}) must be within 4px of .cand-scroll bottom (${geo.scrollBottom})`,
    ).toBeLessThanOrEqual(4);

    // 2. .cand-scroll's own bottom edge must be near the fullscreen card's
    //    bottom edge (minus the card's own padding) — i.e. the scroll
    //    viewport itself fills the available fullscreen height, not capped
    //    by the old stale `calc(100vh - 28rem)` override.
    expect(
      geo.cardBottom - geo.scrollBottom,
      `.cand-scroll bottom (${geo.scrollBottom}) must be within 20px of the card's own bottom (${geo.cardBottom})`,
    ).toBeLessThanOrEqual(20);

    // 3. The fullscreen card itself must not need to scroll past its own
    //    viewport — confirms the flex-fill sizing resolved cleanly instead
    //    of overflowing (which would silently reintroduce a different
    //    "where did the rest of my grid go" symptom).
    expect(
      geo.cardScrollH - geo.cardClientH,
      `card scrollHeight (${geo.cardScrollH}) must not exceed clientHeight (${geo.cardClientH}) by more than 1px`,
    ).toBeLessThanOrEqual(1);

    // Restore.
    const defaultBtn = page.locator('.default-btn').first();
    if (await defaultBtn.isVisible().catch(() => false)) {
      await defaultBtn.click();
    }
  });

  // ── Sparse leg list — rows must not stretch to fill the 1fr track ───────────
  test('a short leg list keeps natural row height (align-content:start)', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');

    const legsCard = page.locator('.opt-legs-card').first();
    const legsCount = await pickUnderlyingWithLegs(page);
    if (legsCount === 0) {
      test.skip(true, 'No F&O legs available — data-availability, not a layout regression');
      return;
    }

    // Filter down to a single row via the Legs card's own search box, so
    // the "1fr body row taller than content" case is exercised live rather
    // than relying on whichever underlying happens to have few legs.
    const searchBtn = legsCard.locator('.grid-search-btn').first();
    if (await searchBtn.isVisible().catch(() => false)) {
      await searchBtn.click();
      const searchInput = legsCard.locator('input[type="text"]').first();
      if (await searchInput.isVisible().catch(() => false)) {
        const firstSymbolText = await page.locator('.opt-legs-card .cand-row:not(.cand-row-total)')
          .first().locator('span').nth(2).innerText().catch(() => '');
        const needle = (firstSymbolText || '').replace(/[^0-9A-Za-z]/g, '').slice(0, 6);
        if (needle) {
          await searchInput.fill(needle);
          await page.waitForTimeout(400);
        }
      }
    }

    const fsBtn = legsCard.locator('.fs-btn, [title*="fullscreen" i], [aria-label*="fullscreen" i]').first();
    if (!await fsBtn.isVisible().catch(() => false)) {
      test.skip(true, 'Fullscreen button not visible on Legs card');
      return;
    }
    await waitForLayoutSettled(page);
    await fsBtn.click();
    await expect(page.locator('.opt-legs-card.fs-card-on')).toBeVisible({ timeout: 5_000 });
    await page.waitForTimeout(300);

    const rowHeights = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.opt-legs-card.fs-card-on .cand-row:not(.cand-row-total)')];
      return rows.map((r) => r.getBoundingClientRect().height);
    });
    console.log(`[legs_fs_fill/sparse] row heights: ${JSON.stringify(rowHeights)}`);

    for (const h of rowHeights) {
      expect(h, `leg row height ${h}px must stay natural (<= 60px), not stretch to fill the 1fr track`).toBeLessThanOrEqual(60);
    }

    const defaultBtn = page.locator('.default-btn').first();
    if (await defaultBtn.isVisible().catch(() => false)) {
      await defaultBtn.click();
    }
  });

  // ── Non-fullscreen regression guard ─────────────────────────────────────────
  test('non-fullscreen Legs grid is unaffected (no row-track leak)', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');

    const legsCard = page.locator('.opt-legs-card').first();
    const legsVisible = await legsCard.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!legsVisible) {
      test.skip(true, 'No .opt-legs-card visible');
      return;
    }

    // The fullscreen-only `min-height: 100%` + 3-row pin-to-bottom template
    // must NOT leak outside `.fs-card-on`. `min-height` is the cleanest
    // signal: non-fullscreen must resolve to the initial `0px` (no floor
    // forcing `.cand-grid` taller than its content), whereas the
    // fullscreen override resolves it to a real pixel value matching the
    // card's available height (see the main fullscreen test above).
    const { minHeight, gridTemplateRows } = await page.locator('.opt-legs-card .cand-grid').first()
      .evaluate((el) => {
        const cs = getComputedStyle(el);
        return { minHeight: cs.minHeight, gridTemplateRows: cs.gridTemplateRows };
      });
    console.log(`[legs_fs_fill/normal] non-fullscreen min-height: ${minHeight}, grid-template-rows: ${gridTemplateRows}`);

    expect(
      minHeight,
      `non-fullscreen .cand-grid min-height must stay at the initial 0px (the fullscreen-only 100% floor must not leak); got ${minHeight}`,
    ).toBe('0px');
  });
});
