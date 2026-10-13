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
 * ── Round 2 (2026-10-13 follow-up) ──────────────────────────────────
 * Adds the /admin tree + /strategies/[id] migrations found by round 2's
 * enumeration: RecordingsPanel (admin/execution → SimulatorPanel),
 * admin/brokers' "Accounts"/"Connection Log" headers, admin/+page's
 * "Email Partners" header, admin/tokens' create/edit form header,
 * admin/mcp's research-thread-viewer header, and /strategies/[id]'s
 * header card + Lot ledger header. Same three-check pattern as round 1.
 * Border-contrast/`.ch-sep`/LogPanel-reactive-safety items from round 2
 * belong to companion agents — not re-tested here.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/cardheader_adoption_sweep.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

// Round 2's admin/mcp research-thread header and strategies/[id]'s
// header-card Pause/Activate button sit behind capabilities the default
// PLAYWRIGHT_USER (admin role) doesn't hold (`view_research` / `use_
// research_chat` / `manage_own_strategies` — designated/trader only per
// rbac.js). Same env-var + skip-with-reason convention already
// established by lab_chat_panel.spec.js.
const DESIGNATED_USER = process.env.PLAYWRIGHT_DESIGNATED_USER || '';
const DESIGNATED_PASS = process.env.PLAYWRIGHT_DESIGNATED_PASS || '';
const HAS_DESIGNATED = Boolean(DESIGNATED_USER && DESIGNATED_PASS);
const NO_DESIGNATED_REASON =
  'No designated login available. Set PLAYWRIGHT_DESIGNATED_USER and ' +
  'PLAYWRIGHT_DESIGNATED_PASS to run this capability-gated check.';

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

  test('OrderBook "Activity" card has no Collapse button; Download/Fullscreen still work', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const card = page.locator('section.bucket-card-activity');
    const header = card.locator('.card-header').first();
    await expect(header).toBeVisible({ timeout: 20_000 });

    // Collapse removed 2026-10-13 (header button-group standardization) —
    // no caret/collapse button renders anywhere on this card.
    await expect(header.locator('.collapse-btn')).toHaveCount(0);

    // Regression guard — Download + Fullscreen (the buttons that
    // coexisted with Collapse on this card) still render and work.
    await expect(header.locator('.grid-download-btn')).toBeVisible();
    const fsBtn = header.locator('.fs-btn, .default-btn');
    await expect(fsBtn.first()).toBeVisible();
    await fsBtn.first().click();
    await expect(card.locator('.ob-root')).toHaveClass(/fs-card-on/);
    await header.locator('.default-btn').first().click();
    await expect(card.locator('.ob-root')).not.toHaveClass(/fs-card-on/);
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
  test('title + AlgoTabs identity + per-tab CardControls still work, no Collapse/Fullscreen, shade present', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const card = page.locator('.cap-eq-tabbed');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const header = card.locator('.card-header').first();
    await expect(header).toBeVisible();

    // 1. Header button-group standardization Part 5 (2026-10-13) — this
    // card previously had neither `title` nor `label`; it now carries a
    // visible "Portfolio" title distinct from its own P&L/Capital/Equity
    // tab names, alongside the AlgoTabs identity in the middle zone.
    await expect(header.locator('.ch-title')).toHaveText('Portfolio');
    await expect(header.getByRole('tab', { name: 'P&L' })).toBeVisible();
    await expect(header.getByRole('tab', { name: 'Capital' })).toBeVisible();
    await expect(header.getByRole('tab', { name: 'Equity' })).toBeVisible();

    // 2. Collapse removed 2026-10-13 (header button-group standardization,
    // Part 1) — no caret/collapse button renders on any of the three
    // per-tab CardControls clusters, and the section can never carry
    // `is-collapsed` (the binding was deleted, not just hidden).
    await expect(header.locator('.collapse-btn')).toHaveCount(0);
    await expect(card).not.toHaveClass(/is-collapsed/);

    // 3. Fullscreen removed too, Dashboard-specific (Part 3) — unlike
    // most other migrated cards, Dashboard panels lose Fullscreen
    // entirely, via hideFullscreen={true} on each per-tab CardControls.
    await expect(header.locator('.fs-btn, .default-btn')).toHaveCount(0);
    await expect(card).not.toHaveClass(/fs-card-on/);

    // 4. Regression guard — Download (the control that coexisted with
    // Collapse/Fullscreen on this card) still renders per active tab.
    await header.getByRole('tab', { name: 'Capital' }).click();
    const capitalBody = card.locator('.card-body').nth(1);
    await expect(capitalBody).toBeVisible();
    await expect(header.locator('.grid-download-btn')).toBeVisible();
    await header.getByRole('tab', { name: 'Equity' }).click();
    const equityBody = card.locator('.card-body').nth(2);
    await expect(equityBody).toBeVisible();
    await expect(header.locator('.grid-download-btn')).toBeVisible();

    // 5. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep — Derivatives Legs card', () => {
  test('underlying chip + Legs/Exp-close tabs + Search/Download still work, no Collapse button, shade present', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const card = page.locator('.opt-legs-card');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const header = card.locator('.card-header').first();
    await expect(header).toBeVisible();

    // 1. AlgoTabs identity (no `title` prop) — Legs / Exp close tabs render.
    await expect(header.getByRole('tab', { name: /^Legs/ })).toBeVisible();
    await expect(header.getByRole('tab', { name: /Exp close/ })).toBeVisible();

    // 2. Collapse removed 2026-10-13 (header button-group standardization)
    // — no caret/collapse button renders anywhere on this card, and the
    // card can never carry `is-collapsed` (the binding was deleted, not
    // just hidden).
    await expect(header.locator('.collapse-btn')).toHaveCount(0);
    await expect(card).not.toHaveClass(/is-collapsed/);

    // 3. Regression guard — the other control-cluster buttons (Search,
    // Download) that coexisted with Collapse still render and still
    // work after its removal.
    const searchBtn = header.locator('.grid-search-btn');
    await expect(searchBtn).toBeVisible();
    await searchBtn.click();
    await expect(searchBtn).toHaveClass(/on/);
    await searchBtn.click();

    await expect(header.locator('.grid-download-btn')).toBeVisible();

    // 4. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });

  test('Fullscreen button is always present, regardless of grid row count (2026-10-13 fix)', async ({ page }) => {
    // detectOverflow={false} was added to this card's CardHeader after
    // confirming (live, via the sibling Pulse-Positions investigation
    // earlier this session) that CardHeader's own overflow-auto-hide
    // heuristic (hideFullscreen = detectOverflow && !_hasOverflow &&
    // !isFullscreen) silently hides Fullscreen whenever a grid doesn't
    // have enough rows to overflow its own viewport — exactly the state
    // this card is often in. The PRIOR test in this file deliberately
    // avoided asserting Fullscreen here for that reason; this test now
    // asserts it positively, since the heuristic is disabled.
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const card = page.locator('.opt-legs-card');
    await expect(card).toBeVisible({ timeout: 20_000 });
    const header = card.locator('.card-header').first();

    const fsBtn = header.locator('button[aria-label="Expand Legs to fullscreen"]');
    await expect(fsBtn, 'Fullscreen button must always be visible on the Legs card, regardless of row count').toBeVisible({ timeout: 10_000 });
    await fsBtn.click();
    await expect(card, 'card should enter fullscreen').toHaveClass(/fs-card-on/);
    const restoreBtn = header.locator('.default-btn');
    await expect(restoreBtn).toBeVisible();
    await restoreBtn.click();
    await expect(card, 'card should exit fullscreen').not.toHaveClass(/fs-card-on/);
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

// ════════════════════════════════════════════════════════════════════
// Round 2 (2026-10-13 follow-up) — /admin tree + /strategies/[id]
// ════════════════════════════════════════════════════════════════════

test.describe('CardHeader adoption sweep round 2 — RecordingsPanel (admin/execution → SimulatorPanel)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/simulator/recordings**', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([
        { id: 1, label: 'RBQ-REC-1', scenario: 'trend_up', event_count: 40, duration_sec: 12.5, tick_count: 40, started_at: new Date().toISOString() },
        { id: 2, label: 'RBQ-REC-2', scenario: 'chop', event_count: 15, duration_sec: 4.1, tick_count: 15, started_at: new Date().toISOString() },
      ]),
    }));
    await page.route('**/api/simulator/replay/status', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ active: false }),
    }));
    await page.goto('/admin/execution?tab=sim', { waitUntil: 'domcontentloaded' });
  });

  test('title + count + refresh button still work, shade present', async ({ page }) => {
    const card = page.locator('.rec-card');
    await expect(card).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved.
    await expect(card.locator('.card-header .ch-title')).toHaveText('Recordings');

    // Count badge preserved alongside the title.
    await expect(card.locator('.rec-count')).toHaveText('2 saved');

    // 2. Refresh (↻) button still present and still re-fetches.
    const refreshBtn = card.locator('.rec-refresh');
    await expect(refreshBtn).toBeVisible();
    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes('/simulator/recordings')),
      refreshBtn.click(),
    ]);
    expect(req).toBeTruthy();

    // 3. Shade present.
    expect(await hasSharedShade(card.locator('.card-header'))).toBe(true);
  });
});

test.describe('CardHeader adoption sweep round 2 — admin/brokers "Accounts" + "Connection Log"', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/admin/brokers', (route) => {
      if (route.request().method() !== 'GET') { route.continue(); return; }
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([
          { account: 'RBQT1', broker_id: 'kite', api_key: 'abc123', source_ip: '1.2.3.4', is_active: true, loaded: true, historical_data_enabled: true },
          { account: 'RBQT2', broker_id: 'kite', api_key: 'def456', source_ip: '1.2.3.5', is_active: true, loaded: true, historical_data_enabled: true },
        ]),
      });
    });
    await page.route('**/api/admin/brokers/order', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({}),
    }));
    await page.route('**/api/admin/broker-connection-events**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [] }),
    }));
    await page.goto('/admin/brokers', { waitUntil: 'domcontentloaded' });
  });

  test('Accounts header: count title + "+ New account" button still work, shade present', async ({ page }) => {
    // Scope to the <CardHeader> itself, not `.algo-card` — the layout
    // ALSO uses `.algo-card` as an outer page-wrapper class, so that
    // broader ancestor would match too (see the Connection Log test
    // below for the same pitfall, hit first there).
    const card = page.locator('.card-header').filter({ has: page.locator('.brokers-h', { hasText: 'Accounts' }) }).first();
    await expect(card).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved (dynamic count badge).
    await expect(card.locator('.brokers-h')).toContainText('Accounts');
    await expect(card.locator('.brokers-h')).toContainText('(2)');

    // 2. "+ New account" button still opens the create form.
    const newBtn = card.getByRole('button', { name: '+ New account' });
    await expect(newBtn).toBeVisible();
    await newBtn.click();
    await expect(page.getByRole('button', { name: '+ New account' })).toBeDisabled();

    // 3. Shade present.
    expect(await hasSharedShade(card)).toBe(true);
  });

  test('Connection Log header: title + Refresh button still work, shade present', async ({ page }) => {
    // Scope to the <CardHeader> itself (not `.algo-card`, which the
    // layout ALSO uses as an outer page wrapper class — matching that
    // broader ancestor would pull in the unrelated page-header "Refresh
    // brokers" button too). The Refresh button lives inside this same
    // CardHeader's own `right` snippet, so no wider scope is needed.
    const card = page.locator('.card-header').filter({ has: page.locator('.ch-title', { hasText: 'Connection Log' }) }).first();
    await expect(card).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved — now via CardHeader's own title prop.
    await expect(card.locator('.ch-title')).toHaveText('Connection Log');

    // 2. Refresh button still present and still re-fetches the conn log.
    const refreshBtn = card.getByRole('button', { name: 'Refresh', exact: true });
    await expect(refreshBtn).toBeVisible();
    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes('/admin/broker-connection-events')),
      refreshBtn.click(),
    ]);
    expect(req).toBeTruthy();

    // 3. Shade present.
    expect(await hasSharedShade(card)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep round 2 — admin "Email Partners" header', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/admin/users', (route) => {
      if (route.request().method() !== 'GET') { route.continue(); return; }
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ users: [{
          id: 1, username: 'rambo', display_name: 'Rambo', role: 'admin',
          is_active: true, is_approved: true, account_id: 'T1', email: 'rambo@example.com',
        }] }),
      });
    });
    await page.route('**/api/admin/email-events**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }),
    }));
    await page.goto('/admin', { waitUntil: 'domcontentloaded' });
  });

  test('title + show/hide history toggle still work, shade present', async ({ page }) => {
    const panel = page.locator('section.email-panel');
    await expect(panel).toBeVisible({ timeout: 20_000 });

    // 1. Title preserved.
    await expect(panel.locator('.card-header .ch-title')).toHaveText('Email Partners');

    // 2. Toggle button still flips label + still fires the mocked
    // email-events fetch when opened.
    const toggleBtn = panel.getByRole('button', { name: /show history|hide history/ });
    await expect(toggleBtn).toHaveText(/show history/);
    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes('/admin/email-events')),
      toggleBtn.click(),
    ]);
    expect(req).toBeTruthy();
    await expect(toggleBtn).toHaveText(/hide history/);

    // 3. Shade present.
    expect(await hasSharedShade(panel.locator('.card-header'))).toBe(true);
  });
});

test.describe('CardHeader adoption sweep round 2 — admin/tokens create/edit form header', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/tokens', { waitUntil: 'domcontentloaded' });
  });

  test('"New token" title + Cancel button still work, shade present', async ({ page }) => {
    // Form is hidden until "+ New token" (page-header button, out of
    // this sweep's scope) is clicked.
    await page.getByRole('button', { name: '+ New token' }).click();

    const card = page.locator('.token-form-card');
    await expect(card).toBeVisible({ timeout: 10_000 });

    // 1. Title preserved (dynamic "New token" / "Edit token #N").
    await expect(card.locator('.card-header .ch-title')).toHaveText('New token');

    // 2. Cancel button still present and still closes the form.
    const cancelBtn = card.getByRole('button', { name: 'Cancel' });
    await expect(cancelBtn).toBeVisible();
    await cancelBtn.click();
    await expect(card).toBeHidden();

    // 3. Shade present — re-open to check since the card unmounts on Cancel.
    await page.getByRole('button', { name: '+ New token' }).click();
    await expect(card).toBeVisible();
    expect(await hasSharedShade(card.locator('.card-header'))).toBe(true);
  });
});

test.describe('CardHeader adoption sweep round 2 — admin/mcp research-thread viewer header', () => {
  test.beforeEach(async ({ page }) => {
    // /admin/mcp's Research tab is gated on the `view_research`
    // capability (designated/trader/risk/demo only — NOT admin, per
    // rbac.js) — same convention lab_chat_panel.spec.js already
    // established for this exact page.
    test.skip(!HAS_DESIGNATED, NO_DESIGNATED_REASON);
    await loginAsAdmin(page, { user: DESIGNATED_USER, pass: DESIGNATED_PASS });
    await page.route('**/api/research/threads/1', (route) => {
      if (route.request().method() === 'DELETE') { route.continue(); return; }
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          id: 1, symbol: 'RELIANCE', confidence: 'high', title: 'RBQ test thesis',
          updated_at: new Date().toISOString(), thesis_text: 'Test thesis body.',
          transcript: [],
        }),
      });
    });
    await page.route('**/api/research/threads?*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([{
        id: 1, symbol: 'RELIANCE', confidence: 'high', title: 'RBQ test thesis',
        updated_at: new Date().toISOString(), transcript_len: 0,
      }]),
    }));
    await page.route('**/api/research/drafts**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/admin/mcp', { waitUntil: 'domcontentloaded' });
    const threadBtn = page.getByRole('button', { name: /RELIANCE/ }).first();
    const denied = page.getByText('Access denied').first();
    await threadBtn.or(denied).first().waitFor({ timeout: 15_000 }).catch(() => {});
    test.skip(await denied.isVisible().catch(() => false),
      'Designated login cannot reach /admin/mcp Research tab (view_research missing).');
    await threadBtn.click();
  });

  test('symbol/confidence/title + time/delete button still work, shade present', async ({ page }) => {
    const main = page.locator('section.lab-main');
    await expect(main).toBeVisible({ timeout: 20_000 });
    const header = main.locator('.card-header').first();
    await expect(header).toBeVisible();

    // 1. No `title` prop used (free-text thread title keeps its own
    // .thr-title styling) — confirm symbol + title text still render.
    await expect(header.locator('.thr-sym')).toHaveText('RELIANCE');
    await expect(header.locator('.thr-title')).toHaveText('RBQ test thesis');

    // 2. Delete (✕) button still present, still gated behind the
    // ConfirmModal (clicking opens the dialog; Cancel so no real
    // delete fires against this mocked thread).
    const delBtn = header.locator('.thr-del');
    await expect(delBtn).toBeVisible();
    await delBtn.click();
    const confirmDialog = page.getByText('Delete research thread?');
    await expect(confirmDialog).toBeVisible({ timeout: 5_000 });
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(confirmDialog).toBeHidden();

    // 3. Shade present.
    expect(await hasSharedShade(header)).toBe(true);
  });
});

test.describe('CardHeader adoption sweep round 2 — /strategies/[id] header card + Lot ledger', () => {
  const SID = 9999;

  /** @param {import('@playwright/test').Page} page */
  async function mockStrategyRoutes(page, isActiveRef) {
    await page.route(`**/api/strategies/${SID}`, (route) => {
      if (route.request().method() === 'PATCH') {
        const body = JSON.parse(route.request().postData() || '{}');
        if ('is_active' in body) isActiveRef.value = !!body.is_active;
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
        return;
      }
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          id: SID, slug: 'rbq-test', name: 'RBQ Test Strategy', description: 'Sweep test fixture',
          owner_username: 'rambo', is_active: isActiveRef.value,
          realised_pnl: 1000, unrealised_pnl: -200,
          capacity_cap_inr: 500000, target_volatility: 0.15,
        }),
      });
    });
    await page.route(`**/api/strategies/${SID}/lots**`, (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rows: [], total_open: 0, total_closed: 0 }),
    }));
    await page.route(`**/api/strategies/${SID}/snapshots**`, (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [] }),
    }));
    await page.route(`**/api/strategies/${SID}/metrics**`, (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(null),
    }));
  }

  test.describe('header card (Pause/Activate — manage_own_strategies: designated/trader only)', () => {
    test.beforeEach(async ({ page }) => {
      // Pause/Activate is gated on `manage_own_strategies`
      // (designated/trader only per rbac.js — NOT admin), same
      // convention as the admin/mcp block above.
      test.skip(!HAS_DESIGNATED, NO_DESIGNATED_REASON);
      await loginAsAdmin(page, { user: DESIGNATED_USER, pass: DESIGNATED_PASS });
      await mockStrategyRoutes(page, { value: true });
      await page.goto(`/strategies/${SID}`, { waitUntil: 'domcontentloaded' });
    });

    test('name + pill + Pause/Activate button still work, shade present', async ({ page }) => {
      const head = page.locator('section.strat-detail-head');
      await expect(head).toBeVisible({ timeout: 20_000 });
      const header = head.locator('.card-header').first();

      // 1. Name + status pill preserved (left-grouped, not CardHeader's
      // `title` prop — strat-head-name keeps its own fs-xl/800 styling).
      await expect(header.locator('.strat-head-name')).toHaveText('RBQ Test Strategy');
      await expect(header.locator('.pill-active')).toHaveText('active');

      // 2. Pause/Activate button still present, still toggles, and stays
      // adjacent to the pill (left-grouped layout preserved, not pushed
      // to the row's far right).
      const toggleBtn = header.getByRole('button', { name: 'Pause' });
      await expect(toggleBtn).toBeVisible({ timeout: 10_000 });
      const [req] = await Promise.all([
        page.waitForRequest((r) => r.url().endsWith(`/strategies/${SID}`) && r.request().method() === 'PATCH'),
        toggleBtn.click(),
      ]);
      expect(req).toBeTruthy();
      await expect(header.locator('.pill-inactive')).toHaveText('paused');
      await expect(header.getByRole('button', { name: 'Activate' })).toBeVisible();

      // 3. Shade present.
      expect(await hasSharedShade(header)).toBe(true);
    });
  });

  test.describe('Lot ledger header ("Show closed" is ungated — regular admin login)', () => {
    test.beforeEach(async ({ page }) => {
      await loginAsAdmin(page);
      await mockStrategyRoutes(page, { value: true });
      await page.goto(`/strategies/${SID}`, { waitUntil: 'domcontentloaded' });
    });

    test('title + "Show closed" checkbox still work, shade present', async ({ page }) => {
      const section = page.locator('section.strat-detail-lots');
      await expect(section).toBeVisible({ timeout: 20_000 });
      const header = section.locator('.card-header').first();

      // 1. Title preserved.
      await expect(header.locator('.ch-title')).toHaveText('Lot ledger');

      // 2. "Show closed" checkbox still present and still re-fetches
      // lots with include_closed=0 when unchecked.
      const checkbox = header.locator('.show-closed input[type="checkbox"]');
      await expect(checkbox).toBeChecked();
      const [req] = await Promise.all([
        page.waitForRequest((r) => r.url().includes(`/strategies/${SID}/lots`) && r.url().includes('include_closed=0')),
        checkbox.uncheck(),
      ]);
      expect(req).toBeTruthy();

      // 3. Shade present.
      expect(await hasSharedShade(header)).toBe(true);
    });
  });
});
