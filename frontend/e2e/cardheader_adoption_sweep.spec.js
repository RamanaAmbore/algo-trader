/**
 * cardheader_adoption_sweep.spec.js
 *
 * 2026-10-13 CardHeader + Activity-panel consistency sweep, Part 2 —
 * CardHeader adoption. Verifies that panels previously using hand-rolled
 * header markup (HeldOrdersCard, ChaseCard, PnlAnalysis's "Performance"
 * sub-card, Dashboard's NAV/Capital/Equity tabbed card, Derivatives' Legs
 * card) now render through the shared <CardHeader> component, with zero
 * functionality loss, and now pick up the shared `--ch-bg` shade that
 * `(algo)/+layout.svelte`'s `:global(body)` rule sets.
 *
 * Three checks per migrated panel:
 *   1. Title still renders (same text as before migration).
 *   2. Every control button that existed before migration still renders
 *      AND still works (at least one meaningful click-test per panel).
 *   3. The panel's `.card-header` now shows the shared shade
 *      (computed `backgroundImage` !== 'none').
 *
 * A known-good, already-CardHeader'd panel (OrderBook's "Activity" card)
 * is used as a positive control for the shade check (#3) — if the
 * control itself doesn't show a shade, the assertion is wrong, not the
 * migration.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/cardheader_adoption_sweep.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

/** A `.card-header` picks up the shared shade when `--ch-bg` resolves to
 *  something other than `none` / transparent — checked via the rendered
 *  `backgroundImage` (the shade is a gradient) rather than `background`
 *  shorthand, which Chromium may report piecemeal. */
async function hasSharedShade(locator) {
  return await locator.evaluate((el) => {
    const s = getComputedStyle(el);
    return s.backgroundImage !== 'none' || /rgba?\([^)]*,\s*0?\.\d/.test(s.backgroundColor);
  });
}

test.describe('CardHeader adoption sweep — positive control', () => {
  test('OrderBook "Activity" card already shows the shared shade (sanity-checks the assertion itself)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const activityHeader = page.locator('section.bucket-card-activity .card-header').first();
    await expect(activityHeader).toBeVisible({ timeout: 20_000 });
    expect(await hasSharedShade(activityHeader), 'known-good CardHeader should show a shade').toBe(true);
  });
});

test.describe('CardHeader adoption sweep — HeldOrdersCard', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/orders/held/**', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ held: [{
        id: 'held-1', side: 'BUY', qty: 50, symbol: 'RBQ-HELDTEST', account: 'T1',
        hold: JSON.stringify({ category: 'expiry_close' }),
      }] }),
    }));
    // Keep the rest of the page's data surfaces quiet so this card is
    // the only thing we're asserting on.
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
  });

  test('title renders, Release all button still works, shade present', async ({ page }) => {
    const card = page.locator('.held-card');
    await expect(card).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved.
    await expect(card.locator('.card-header .ch-title')).toHaveText('Held orders');

    // 2. Release all button still present and still wired to
    // ConfirmModal (clicking opens the confirm dialog; we cancel via
    // "Keep held" so no real release fires).
    const releaseAllBtn = card.locator('.held-release-all');
    await expect(releaseAllBtn).toBeVisible();
    await expect(releaseAllBtn).toBeEnabled();
    await releaseAllBtn.click();
    const confirmDialog = page.getByText('Release all held orders?');
    await expect(confirmDialog).toBeVisible({ timeout: 5_000 });
    await page.getByRole('button', { name: 'Keep held' }).click();
    await expect(confirmDialog).toBeHidden();

    // 3. Shade present.
    expect(await hasSharedShade(card.locator('.card-header'))).toBe(true);
  });
});

test.describe('CardHeader adoption sweep — ChaseCard', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    // Single path-inspecting handler for every /api/orders/* endpoint this
    // page hits, to avoid any ambiguity from Playwright's route-matching
    // order across several registered patterns. The plain OPEN-orders list
    // mock gives `_showChases` a STABLE signal (_openOrderCount) — relying
    // solely on ChaseCard's own `activeCount` bind would otherwise
    // oscillate `_showChases` true/false in a tight loop, since that bind
    // resets to 0 on every fresh component mount (pre-existing ChaseCard
    // behavior, unrelated to this header-only migration) before its own
    // fetch resolves.
    await page.route('**/api/orders/**', (route) => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      if (p === '/api/orders/held/' || p === '/api/orders/held') {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ held: [] }) });
      } else if (p === '/api/orders/chases/active') {
        route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify([{
            id: 901, account: 'T1', symbol: 'RBQ-CHASETEST', exchange: 'NFO',
            transaction_type: 'SELL', quantity: 10, initial_price: 100, current_limit: 100,
            fill_price: null, attempts: 1, status: 'OPEN', engine: 'live', mode: 'live',
            detail: null, created_at: new Date().toISOString(), broker_order_id: 'B9001',
          }]),
        });
      } else if (p === '/api/orders/algo/reconcile') {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ scanned: 1, updated: 0, missing: 0 }) });
      } else if (p === '/api/orders/' || p === '/api/orders') {
        route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ rows: [{
            order_id: 'B9001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-CHASETEST',
            transaction_type: 'SELL', quantity: 10, pending_quantity: 10, filled_quantity: 0,
            price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
            order_type: 'LIMIT', product: 'MIS', variety: 'regular',
            order_timestamp: new Date().toISOString(),
          }], refreshed_at: new Date().toISOString() }),
        });
      } else {
        route.continue();
      }
    });
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
  });

  test('title renders, Reconcile button still works, shade present', async ({ page }) => {
    const root = page.locator('.cc-root');
    await expect(root).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved — was `.cc-label` text "Chases in flight" in a
    // separate span; now the CardHeader's own `title` prop.
    await expect(root.locator('.card-header .ch-title')).toHaveText('Chases in flight');

    // Count chip still present alongside the title.
    await expect(root.locator('.cc-count')).toBeVisible();

    // 2. Reconcile button still present and still fires the mocked
    // reconcile endpoint.
    const reconcileBtn = root.locator('.cc-reconcile');
    await expect(reconcileBtn).toBeVisible();
    const [reconcileReq] = await Promise.all([
      page.waitForRequest((req) => req.url().includes('/orders/algo/reconcile') && req.method() === 'POST'),
      reconcileBtn.click(),
    ]);
    expect(reconcileReq).toBeTruthy();

    // 3. Shade present.
    expect(await hasSharedShade(root.locator('.card-header'))).toBe(true);
  });

  test('the host page (/orders) no longer wraps ChaseCard in a second, redundant header', async () => {
    const pageSrc = readFileSync(
      new URL('../src/routes/(algo)/orders/+page.svelte', import.meta.url).pathname, 'utf8'
    );
    // The old page-level wrapper rendered a plain "Chases" label via its
    // own CardHeader — that block is gone; ChaseCard now owns its one
    // header internally.
    expect(pageSrc).not.toMatch(/<CardHeader showControls=\{false\}>\s*\{#snippet left\(\)\}\s*<span class="mp-section-label">Chases<\/span>/);
  });
});

test.describe('CardHeader adoption sweep — PnlAnalysis "Performance" sub-card (Dashboard)', () => {
  test('title + benchmark toggle still work, shade present', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    // Switch the chart card to its Performance tab (mounts PnlAnalysis).
    const chartCard = page.locator('.row1-col-chart');
    await expect(chartCard).toBeVisible({ timeout: 20_000 });
    await chartCard.getByRole('tab', { name: 'Performance' }).click();

    // PnlAnalysis's own chart-card header only renders once its /pnl/range
    // fetch resolves with data — wait generously for a live dev backend.
    const chartSubCard = chartCard.locator('.card.chart-card');
    const header = chartSubCard.locator('.card-header');
    await expect(header).toBeVisible({ timeout: 30_000 });

    // 1. Title preserved (dynamic — includes the from-date).
    await expect(header.locator('.ch-title')).toContainText('Performance — % from');

    // 2. Benchmark toggle buttons still present and still clickable —
    // toggling NIFTY 50 off flips its chip class from bm-on to bm-off.
    const niftyChip = chartSubCard.locator('.bm-chip', { hasText: 'NIFTY 50' });
    await expect(niftyChip).toBeVisible();
    const wasOn = await niftyChip.evaluate((el) => el.classList.contains('bm-on'));
    await niftyChip.click();
    await expect(niftyChip).toHaveClass(wasOn ? /bm-off/ : /bm-on/);

    // 3. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep — Dashboard NAV/Capital/Equity tabbed card', () => {
  test('AlgoTabs identity + per-tab CardControls still work, shade present', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const card = page.locator('.cap-eq-tabbed');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const header = card.locator('.card-header').first();
    await expect(header).toBeVisible();

    // 1. No `title` prop (AlgoTabs is the identity) — confirm the three
    // tabs render inside the header's middle zone.
    await expect(header.getByRole('tab', { name: 'P&L' })).toBeVisible();
    await expect(header.getByRole('tab', { name: 'Capital' })).toBeVisible();
    await expect(header.getByRole('tab', { name: 'Equity' })).toBeVisible();

    // 2. Per-tab CardControls cluster still renders and the Collapse
    // button still works (click-test the most load-bearing control —
    // collapsing hides the ACTIVE tab's own card-body; the section-level
    // `is-collapsed` class is a separate AND-of-all-three-tabs condition,
    // unrelated to this single-tab click, and unaffected by this
    // migration either way).
    await header.getByRole('tab', { name: 'Capital' }).click();
    const capitalBody = card.locator('.card-body').nth(1);
    await expect(capitalBody).toBeVisible();
    const collapseBtn = header.locator('.collapse-btn');
    await expect(collapseBtn).toBeVisible();
    await collapseBtn.click();
    await expect(capitalBody).toBeHidden();
    await collapseBtn.click();
    await expect(capitalBody).toBeVisible();

    // 3. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep — Derivatives Legs card', () => {
  test('underlying chip + Legs/Exp-close tabs + full control cluster still work, shade present', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const card = page.locator('.opt-legs-card');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const header = card.locator('.card-header').first();
    await expect(header).toBeVisible();

    // 1. AlgoTabs identity (no `title` prop) — Legs / Exp close tabs render.
    await expect(header.getByRole('tab', { name: /^Legs/ })).toBeVisible();
    await expect(header.getByRole('tab', { name: /Exp close/ })).toBeVisible();

    // 2. Full CardControls cluster preserved — Collapse button still
    // toggles the card (same behavior as before migration, now reached
    // via CardHeader's own forwarded props instead of a bespoke wrapper).
    const collapseBtn = header.locator('.collapse-btn');
    await expect(collapseBtn).toBeVisible();
    await collapseBtn.click();
    await expect(card).toHaveClass(/is-collapsed/);
    await collapseBtn.click();
    await expect(card).not.toHaveClass(/is-collapsed/);

    // 3. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep — Derivatives Drafts card (source-level)', () => {
  // Drafts only renders once `drafts.length > 0`, which requires routing
  // a real order through OrderTicket's draft-submit flow to populate —
  // too heavy an interaction for this header-focused sweep. Verified at
  // the source level instead (same pattern as held_orders_card_styling.spec.js).
  const pageSrc = readFileSync(
    new URL('../src/routes/(algo)/admin/derivatives/+page.svelte', import.meta.url).pathname, 'utf8'
  );

  test('Drafts card title + meta text render via CardHeader, not the retired .opt-section-h div', () => {
    expect(pageSrc).toMatch(/<CardHeader title="Drafts" showControls=\{false\}/);
    expect(pageSrc).toMatch(/\(\{drafts\.length\}\) — hypothetical positions/);
    // The old hand-rolled title div is gone from the Drafts card.
    expect(pageSrc).not.toMatch(/<div class="opt-section-h" style="padding-bottom: 0\.5rem;">/);
  });

  test('.opt-section-h base CSS rule was retired alongside its only markup user', () => {
    // A pre-existing, already-dead fullscreen override selector
    // (`:global(.opt-payoff.fs-card-on) .opt-section-h { ... }`) is left
    // untouched — out of scope for this sweep (unrelated to Drafts, and
    // was already non-matching before this migration, since Payoff's own
    // header never carried an `.opt-section-h` class). Only the BASE
    // `.opt-section-h { ... }` rule (its real, only-ever-matching user)
    // is asserted gone here.
    expect(pageSrc).not.toMatch(/\n\s*\.opt-section-h\s*\{/);
  });
});
