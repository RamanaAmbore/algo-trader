/**
 * charts_consolidation.spec.js
 *
 * Verifies the Charts page, navbar entry, /orders tab-strip reduction,
 * and chart-icon affordances introduced by the charts+orders consolidation.
 *
 * 2026-11-03 — replaced this file's own hand-rolled, hardcoded-credential
 * login() (POST /api/auth/login against ['ambore','rambo','admin'] /
 * 'admin1234', hardcoded to always hit https://dev.ramboq.com when the
 * base URL is localhost) with the shared `loginAsAdmin` fixture every
 * other spec in this suite uses — it was failing outright ("login failed
 * against https://dev.ramboq.com") against the current environment,
 * taking every test in this file down with it regardless of the chart
 * functionality itself. `loginAsAdmin` reuses globalSetup's cached token
 * against whichever base URL Playwright is actually configured for (no
 * hardcoded remote host), the same pattern this repo's other chart specs
 * (agent_cli_click_to_describe.spec.js, automation_edit_decompile_resave.
 * spec.js) already rely on.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe('Charts consolidation', () => {

  // 1. Charts page loads
  test('Charts page loads with correct title and header', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/charts', { waitUntil: 'domcontentloaded' });

    // <title> in svelte:head
    await expect(page).toHaveTitle('Charts | RamboQuant Analytics', { timeout: 15_000 });

    // Page-title chip
    await expect(page.locator('h1.page-title-chip')).toHaveText('Charts', { timeout: 10_000 });

    // RefreshButton is present in the page header
    // It renders as a <button> — we locate it by its aria-label pattern or
    // by the refresh-btn class that RefreshButton always uses.
    const refreshBtn = page.locator('button.refresh-btn, button[aria-label*="efresh"]').first();
    await expect(refreshBtn).toBeVisible({ timeout: 10_000 });

    // ChartWorkspace shell: the symbol picker input is the primary anchor.
    // `.cw-sym-input` was a raw <input> class from before SymbolSearchInput
    // replaced it — the real input now lives in that shared component as
    // `.ssi-input` (confirmed via grep: `.cw-sym-input` matches no markup
    // anywhere, only a stale, now-dead CSS rule).
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 10_000 });

    // Toolbar strip is also visible — `.cw-toolbar` never existed in the
    // markup; `.cw-header` is the real wrapper hosting the picker +
    // controls row (see ChartWorkspace.svelte).
    const toolbar = page.locator('.cw-header').first();
    await expect(toolbar).toBeVisible({ timeout: 10_000 });
  });

  // 2. Charts navbar entry exists between Orders and Automation
  test('Navbar has Charts link between Orders and Automation', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-navbar', { timeout: 15_000 });

    // The navbar uses <button onclick={() => goto(link.href)}> elements,
    // not <a> tags, for inline monitor-group items.
    // Find the button with exact text "Charts".
    const chartsBtn = page.locator('.algo-navbar button.algo-nav-btn', { hasText: /^Charts$/ });
    await expect(chartsBtn).toBeVisible({ timeout: 10_000 });

    // Verify ordering: Orders before Charts, Charts before Automation
    // Collect all inline nav buttons in DOM order.
    const navBtns = page.locator('.algo-navbar nav button.algo-nav-btn');
    const labels = await navBtns.evaluateAll(els => els.map(e => e.textContent?.trim() || ''));

    const idxOrders = labels.indexOf('Orders');
    const idxCharts = labels.indexOf('Charts');
    const idxAutomation = labels.indexOf('Automation');

    expect(idxOrders, `Orders nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxCharts, `Charts nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxAutomation, `Automation nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxOrders).toBeLessThan(idxCharts);
    expect(idxCharts).toBeLessThan(idxAutomation);
  });

  // 3. /orders entry-card tab strip — 2026-11-03: a later redesign put
  // charts BACK into the entry card as a real "Chart" tab (alongside
  // Ticket/Chain/CLI), reached via the SymbolPanel-style oes-tabs strip —
  // the OPPOSITE of what this test originally asserted ("Chart tab must
  // not be present"). Live-verified via DOM probe: 4 real role=tab
  // buttons, labelled Ticket/Chain/Chart/CLI (`.algo-tab` class, not the
  // old `.oc-tab`/`.oc-tabs`, which no longer exist anywhere in the
  // codebase).
  test('/orders entry card has 4 tabs including Ticket, Chain, Chart, CLI', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.oes-tabs', { timeout: 15_000 });

    const entryTabStrip = page.locator('.oes-tabs').first();
    const tabs = entryTabStrip.locator('[role="tab"]');

    await expect(tabs).toHaveCount(4, { timeout: 10_000 });

    const labels = await tabs.allTextContents();
    const trimmed = labels.map(l => l.trim());

    expect(trimmed).toContain('Ticket');
    expect(trimmed).toContain('Chain');
    expect(trimmed).toContain('Chart');
    expect(trimmed).toContain('CLI');

    // Ticket is still the first/default tab.
    const firstTabText = await tabs.nth(0).textContent();
    expect(firstTabText?.trim()).toBe('Ticket');
  });

  // 4. The entry card's own "Chart" tab renders an inline chart on
  // symbol selection — 2026-11-03: replaces the old always-visible
  // .oc-chart-btn icon + .cm-overlay modal flow with an inline tab panel
  // (confirmed via live probe: clicking Chart shows a "Select a symbol
  // to view chart" placeholder, then embeds a chart once a symbol is
  // picked — no modal involved for this entry point).
  test('/orders entry card Chart tab renders a chart once a symbol is picked', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'networkidle', timeout: 30_000 });

    const chartTab = page.locator('.oes-tabs').getByText('Chart', { exact: true }).first();
    await expect(chartTab).toBeVisible({ timeout: 15_000 });
    await chartTab.click();

    // Placeholder shown before any symbol is selected.
    await expect(page.getByText('Select a symbol to view chart')).toBeVisible({ timeout: 10_000 });

    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 10_000 });
    await symInput.click();
    await symInput.pressSequentially('NIFTY', { delay: 60 });

    let suggestion = page.locator('.ssi-row').first();
    const appeared = await suggestion.isVisible().catch(() => false);
    if (!appeared) {
      // Re-type after a brief pause — instruments may have just finished loading.
      await symInput.clear();
      await page.waitForTimeout(1500);
      await symInput.click();
      await symInput.pressSequentially('NIFTY', { delay: 80 });
      suggestion = page.locator('.ssi-row').first();
    }
    await expect(suggestion).toBeVisible({ timeout: 10_000 });
    await suggestion.click();

    // The placeholder is gone and the real chart toolbar strip renders.
    await expect(page.getByText('Select a symbol to view chart')).not.toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.cw-header').first()).toBeVisible({ timeout: 10_000 });
  });

  // 5. Right-click (or long-press) the symbol in an order-book row opens
  // the shared SymbolContextMenu, whose "Chart" item opens ChartModal —
  // 2026-11-03: replaces the old dedicated, always-visible .row-chart-btn
  // icon (confirmed via grep: that class matches nothing in the codebase
  // any more, only a stale comment) with the app-wide unified
  // long-press/right-click context menu (see CLAUDE.md's "Draft gestures
  // moved into the unified context menu" entry for the same convention
  // applied elsewhere). Live-verified: right-click on `.oc-sym-btn`
  // (OrderCard.svelte's own symbol span, wired via `onSymbolContext`)
  // shows a menu with a "Chart" item, and ChartModal (`.cm-overlay`)
  // opens with that row's symbol.
  test('/orders order row: right-click the symbol opens the context menu, Chart opens ChartModal for that symbol', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    // Order rows render asynchronously after the broker/algo fetch
    // resolves — a bare .count() right after domcontentloaded races that
    // fetch and reads 0 regardless of whether the book is actually
    // empty. Wait briefly for a real row to appear before deciding.
    const symBtn = page.locator('.oc-sym-btn').first();
    const appeared = await symBtn.waitFor({ state: 'visible', timeout: 10_000 }).then(() => true).catch(() => false);
    if (!appeared) {
      test.skip(true, 'Order book is empty — no row symbol to right-click');
      return;
    }
    const sym = (await symBtn.textContent())?.trim() || '';

    await symBtn.click({ button: 'right' });
    const chartMenuItem = page.locator('[role="menuitem"][data-action="chart"]').first();
    await expect(chartMenuItem).toBeVisible({ timeout: 10_000 });
    await chartMenuItem.click();

    const modalOverlay = page.locator('.cm-overlay').first();
    await expect(modalOverlay).toBeVisible({ timeout: 10_000 });
    if (sym) {
      // OrderCard's own row label and ChartModal's symbol text format the
      // same instrument with the SAME tokens in a DIFFERENT order (e.g.
      // "CRUDEOIL-NOV26-8500-PE" vs "CRUDEOIL26NOV8500PE" — month/year
      // swapped). Split into alphanumeric tokens and check each is
      // present, rather than coupling to either side's exact ordering —
      // this still proves the SAME contract opened, which is the real
      // thing this test is checking.
      // Split on non-alphanumerics AND on letter/digit boundaries so a
      // compound token like "NOV26" becomes separate "NOV"/"26" checks —
      // the modal's own format interleaves them differently ("26NOV").
      const tokens = (sym.toUpperCase().match(/[A-Z]+|[0-9]+/g) || []);
      await expect(async () => {
        const text = ((await modalOverlay.textContent()) || '').toUpperCase();
        for (const t of tokens) expect(text).toContain(t);
      }).toPass({ timeout: 10_000 });
    }

    await page.keyboard.press('Escape');
    await expect(modalOverlay).not.toBeVisible({ timeout: 5_000 });
  });

  // 6. Chart access affordance is cyan-family — 2026-11-03: the old
  // per-entry-card `.oc-chart-btn` icon this test targeted no longer
  // exists (see tests 3/4 above); the page-header "Open Charts" button
  // (`.pha-chart`, aria-label "Open Charts") is the current equivalent
  // always-visible chart affordance on /orders. Live-verified computed
  // color: rgb(34, 211, 238) — algo-cyan-400 exactly.
  test('Open Charts header button on /orders uses cyan-family colour', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const chartBtn = page.locator('.pha-chart').first();
    await expect(chartBtn).toBeVisible({ timeout: 10_000 });

    const color = await chartBtn.evaluate(el => getComputedStyle(el).color);
    const match = color.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    expect(match, `expected an rgb() color, got ${color}`).not.toBeNull();
    const [, r, g, b] = match.map(Number);
    // Cyan family: R low, G/B high. Accept #22d3ee/#7dd3fc/#38bdf8.
    expect(r, `R channel should be low for cyan, got rgb(${r},${g},${b})`).toBeLessThan(150);
    expect(Math.max(g, b), `G or B should be high for cyan, got rgb(${r},${g},${b})`).toBeGreaterThan(150);
  });
});
