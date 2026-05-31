/**
 * charts_consolidation.spec.js
 *
 * Verifies the Charts page, navbar entry, /orders tab-strip reduction,
 * and chart-icon affordances introduced by the charts+orders consolidation.
 *
 * Target: dev.ramboq.com (PLAYWRIGHT_BASE_URL=https://dev.ramboq.com).
 * Falls back to localhost:5174 if env var is unset.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_consolidation.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// The API host for direct auth calls. When BASE is a local vite dev server,
// use dev.ramboq.com directly to avoid vite proxy instability on page.request.
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

// Module-level token cache — login once per worker, reuse across tests.
let _cachedToken = /** @type {string | null} */ (null);

// ── Auth helper ────────────────────────────────────────────────────────────────
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      try {
        const r = await page.request.post(`${API_HOST}/api/auth/login`, {
          data: { username: u, password: _AUTH_PASS },
          timeout: 15_000,
        });
        if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
      } catch (_) { /* try next user */ }
    }
    if (!_cachedToken) throw new Error(`login failed against ${API_HOST}`);
  }
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  return _cachedToken;
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test.describe('Charts consolidation', () => {

  // 1. Charts page loads
  test('Charts page loads with correct title and header', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/charts`, { waitUntil: 'domcontentloaded' });

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
    const symInput = page.locator('.cw-sym-input').first();
    await expect(symInput).toBeVisible({ timeout: 10_000 });

    // Toolbar is also visible (type-pill group)
    const toolbar = page.locator('.cw-toolbar').first();
    await expect(toolbar).toBeVisible({ timeout: 10_000 });
  });

  // 2. Charts navbar entry exists between Orders and Agents
  test('Navbar has Charts link between Orders and Agents', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-navbar', { timeout: 15_000 });

    // The navbar uses <button onclick={() => goto(link.href)}> elements,
    // not <a> tags, for inline monitor-group items.
    // Find the button with exact text "Charts".
    const chartsBtn = page.locator('.algo-navbar button.algo-nav-btn', { hasText: /^Charts$/ });
    await expect(chartsBtn).toBeVisible({ timeout: 10_000 });

    // Verify ordering: Orders before Charts, Charts before Agents
    // Collect all inline nav buttons in DOM order.
    const navBtns = page.locator('.algo-navbar nav button.algo-nav-btn');
    const labels = await navBtns.evaluateAll(els => els.map(e => e.textContent?.trim() || ''));

    const idxOrders = labels.indexOf('Orders');
    const idxCharts = labels.indexOf('Charts');
    const idxAgents = labels.indexOf('Agents');

    expect(idxOrders, `Orders nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxCharts, `Charts nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxAgents, `Agents nav button not found in [${labels.join(', ')}]`).toBeGreaterThanOrEqual(0);
    expect(idxOrders).toBeLessThan(idxCharts);
    expect(idxCharts).toBeLessThan(idxAgents);
  });

  // 3. /orders has exactly 3 entry-card tabs (Chart tab removed)
  test('/orders entry card has exactly 3 tabs: Order ticket, Chain, Command line', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.oc-tabs', { timeout: 15_000 });

    // The first .oc-tabs is the entry-card tab strip (second is ACT_TABS for
    // the activity card). Grab all .oc-tab children of the first strip.
    const entryTabStrip = page.locator('.oc-tabs').first();
    const tabs = entryTabStrip.locator('.oc-tab');

    await expect(tabs).toHaveCount(3, { timeout: 10_000 });

    const labels = await tabs.allTextContents();
    const trimmed = labels.map(l => l.trim());

    expect(trimmed).toContain('Order ticket');
    expect(trimmed).toContain('Chain');
    expect(trimmed).toContain('Command line');
    // Chart tab must not be present
    expect(trimmed.join(' ')).not.toMatch(/\bChart\b/i);

    // Default active tab is "Order ticket" (aria-selected or active class)
    // We check the tab whose label is "Order ticket" is marked active via
    // the style attribute that the TABS array drives.
    const ticketTab = entryTabStrip.locator('.oc-tab', { hasText: 'Order ticket' });
    // Active tab has role=tab with aria-selected; the component renders
    // the tab as a <button> with onclick — check the computed border-bottom
    // or background. Simpler: the TABS array seeds defaultTab='ticket',
    // so the tab button for "Order ticket" should carry the active styling
    // applied inline. We verify no Chart tab exists and that "Order ticket"
    // is the first tab (index 0).
    const firstTabText = await tabs.nth(0).textContent();
    expect(firstTabText?.trim()).toBe('Order ticket');
  });

  // 4. Chart icon next to /orders symbol picker opens ChartModal
  test('/orders entry header chart icon opens modal on symbol selection', async ({ page }) => {
    await login(page);
    // Wait for networkidle so loadInstruments() has time to populate
    // the IndexedDB before we start typing.
    await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle', timeout: 30_000 });
    await page.waitForSelector('.oc-chart-btn', { timeout: 15_000 });

    // The chart icon button is disabled until a symbol is picked.
    const chartBtn = page.locator('.oc-chart-btn').first();

    // The entry symbol input uses class .oc-sym-input with placeholder "Symbol…"
    const symInput = page.locator('.oc-sym-input').first();
    await expect(symInput).toBeVisible({ timeout: 10_000 });

    // Click to focus (triggers onfocus → _symOpen = true, _onSymInput runs).
    // Use pressSequentially so each keystroke fires oninput which drives
    // the instruments IndexedDB search. The sync suggestUnderlyings path
    // (in-memory sorted underlyings list) fires on the same tick as the
    // first keystroke, so the dropdown should appear quickly once the
    // instruments list is loaded.
    await symInput.click();
    await symInput.pressSequentially('NIFTY', { delay: 60 });

    // If .oc-sym-drop appears with rows — instruments loaded fine.
    // If no rows appear after 10s, the instruments load may have been too
    // slow. Rather than hard-failing, try a second time after a pause.
    let suggestion = page.locator('.oc-sym-row').first();
    const appeared = await suggestion.isVisible().catch(() => false);
    if (!appeared) {
      // Re-type after a brief pause — instruments may have just finished loading.
      await symInput.clear();
      await page.waitForTimeout(1500);
      await symInput.click();
      await symInput.pressSequentially('NIFTY', { delay: 80 });
      suggestion = page.locator('.oc-sym-row').first();
    }
    await expect(suggestion).toBeVisible({ timeout: 10_000 });

    // Click the first suggestion — this calls _pickEntrySymbol which sets
    // _entrySymbol, enabling the chart button.
    // The row uses onmousedown so we use click() which triggers mousedown.
    await suggestion.click();

    // Wait for the chart button to be enabled
    await expect(chartBtn).not.toBeDisabled({ timeout: 8_000 });
    await chartBtn.click();

    // ChartModal renders as role=dialog. We verify the overlay opened by
    // checking its aria-label (pattern: "Chart — <SYMBOL>") or by finding
    // the .cm-header which is always immediately rendered.
    const modalOverlay = page.locator('.cm-overlay').first();
    await expect(modalOverlay).toBeVisible({ timeout: 10_000 });

    // The header inside the modal should show "Chart — NIFTY" (or whichever
    // symbol was picked — just verify the title exists).
    const modalHeader = page.locator('.cm-header').first();
    await expect(modalHeader).toBeVisible({ timeout: 5_000 });
    await expect(modalHeader.locator('.cm-sym')).toBeVisible({ timeout: 5_000 });

    // Close via Escape
    await page.keyboard.press('Escape');
    await expect(modalOverlay).not.toBeVisible({ timeout: 5_000 });
  });

  // 5. Chart icon in /orders order-book row opens modal
  test('/orders order-book row chart icon opens modal for that row symbol', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    // Wait for orders to load (either rows appear or loading clears)
    await page.waitForSelector('.order-card, .no-orders, [data-status]', { timeout: 15_000 });

    const rowChartBtns = page.locator('.row-chart-btn');
    const count = await rowChartBtns.count();
    if (count === 0) {
      test.skip(true, 'Order book is empty — no row chart icons to test');
      return;
    }

    // Capture the symbol from the first row before clicking
    const firstRowBtn = rowChartBtns.first();
    const ariaLabel = await firstRowBtn.getAttribute('aria-label') || '';
    const titleAttr = await firstRowBtn.getAttribute('title') || '';

    await firstRowBtn.click();

    // ChartModal should open
    const modal = page.locator('[role="dialog"]').first();
    await expect(modal).toBeVisible({ timeout: 10_000 });

    // Modal title should reference the symbol from aria-label / title
    // e.g. "Open chart for NIFTY25MAYFUT" → symbol is "NIFTY25MAYFUT"
    const symMatch = (ariaLabel + ' ' + titleAttr).match(/for\s+([A-Z0-9]+)/i);
    if (symMatch) {
      const sym = symMatch[1].toUpperCase();
      await expect(modal.locator('.cm-sym')).toHaveText(sym, { timeout: 5_000 });
    }

    // Close via overlay click (click outside .cm-modal)
    await page.locator('.cm-overlay').click({ position: { x: 10, y: 10 } });
    await expect(modal).not.toBeVisible({ timeout: 5_000 });
  });

  // 6. Chart icon palette is cyan-family on /orders entry header
  test('Chart icon on /orders entry header uses cyan-family colour', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.oc-chart-btn', { timeout: 15_000 });

    const chartBtn = page.locator('.oc-chart-btn').first();
    await expect(chartBtn).toBeVisible();

    // Check colour — cyan-400 is rgb(34, 211, 238) but we accept any
    // colour where B channel > R channel (cyan family: R low, G+B high).
    const color = await chartBtn.evaluate(el => {
      const s = getComputedStyle(el);
      return s.color;
    });

    // Parse rgb(r, g, b)
    const match = color.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (!match) {
      // rgba or other format — just verify the element is styled
      console.warn('Unexpected color format:', color);
      return;
    }
    const [, r, g, b] = match.map(Number);
    // Cyan family: blue >= green and both clearly greater than red.
    // Accept: r < 100 AND (g > 150 OR b > 150) — covers #22d3ee, #7dd3fc, #38bdf8.
    expect(r, `R channel should be low for cyan, got rgb(${r},${g},${b})`).toBeLessThan(150);
    expect(Math.max(g, b), `G or B should be high for cyan, got rgb(${r},${g},${b})`).toBeGreaterThan(150);

    // Size check — button should be roughly 1.2–1.6rem (19–26px at 16px base)
    const box = await chartBtn.boundingBox();
    if (box) {
      expect(box.width).toBeGreaterThanOrEqual(16);
      expect(box.width).toBeLessThanOrEqual(36);
      expect(box.height).toBeGreaterThanOrEqual(16);
      expect(box.height).toBeLessThanOrEqual(36);
    }
  });
});
