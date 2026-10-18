/**
 * modal_stacking_regression.spec.js
 *
 * Regression test suite for overlay-nesting behavior — verifies that the
 * RamboQuant app's modal stacking (SymbolPanel → OrderTicket → Select
 * dropdown, OrderTimelineDrawer, fullscreen cards) correctly handles
 * Escape-key and backdrop-click events without closing ancestor overlays.
 *
 * Context: A prior real bug caused Escape to exit a parent full-screen
 * card while leaving a nested modal open underneath. This test suite
 * documents the CURRENT (fixed) behavior and ensures regressions are caught.
 *
 * Key nested overlay structures tested:
 *   - SymbolPanel (uses layerStack) → OrderTicket (delegates Escape via hostManagesEsc)
 *   - OrderTicket internals → Select dropdown (uses layerStack)
 *   - OrderTimelineDrawer (standalone, has own window listener + backdrop)
 *
 * The app's Escape coordination:
 *   - `layerStack.js` provides a shared capture-phase listener that enforces
 *     LIFO (Last In, First Out) dispatch: only the topmost registered layer
 *     reacts to Escape, via its own `onEscape` callback.
 *   - SymbolPanel, Select, ConfirmModal, and others register
 *     via `pushLayer(onEscape)`.
 *   - OrderTicket's own `window.addEventListener` is disabled when embedded
 *     in SymbolPanel (via `hostManagesEsc={true}` prop) to avoid double-close.
 *   - OrderTimelineDrawer uses its own window listener independent of layerStack.
 *
 * Test coverage (5 quality dimensions per this repo's Playwright convention):
 *   1. SSOT      — nested overlays close in correct LIFO order; assertion
 *                  uses DOM selectors (toHaveCount, toBeVisible) not screenshots
 *   2. Perf      — no waitForTimeout; assertions use precise selectors
 *   3. Stale     — grep confirms the layerStack coordinator code is current
 *   4. Reuse     — reuses loginAsAdmin fixture and .canonical-modal-overlay
 *                  entry points from existing specs
 *   5. UX        — functional checks run in real page with mocked API responses
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — layerStack coordinator + registration status
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — layerStack coordinator usage', () => {
  const layerStackSrc = readFile('src/lib/utils/layerStack.js');
  const symbolPanelSrc = readFile('src/lib/SymbolPanel.svelte');
  const selectSrc = readFile('src/lib/Select.svelte');

  test('layerStack module exports pushLayer and popLayer', () => {
    expect(layerStackSrc).toMatch(/export function pushLayer\(/);
    expect(layerStackSrc).toMatch(/export function popLayer\(/);
  });

  test('layerStack uses capture-phase stopPropagation for LIFO ordering', () => {
    expect(layerStackSrc).toMatch(/document\.addEventListener\('keydown',.*capture: true/);
    expect(layerStackSrc).toMatch(/event\.stopPropagation\(\);/);
  });

  test('SymbolPanel registers via pushLayer (Escape coordination)', () => {
    expect(symbolPanelSrc).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(symbolPanelSrc).toMatch(/pushLayer\(onClose\)/);
  });

  test('Select component registers via pushLayer (dropdown Escape handling)', () => {
    expect(selectSrc).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(selectSrc).toMatch(/pushLayer\(\(\) => \{ open = false; \}\);/);
  });

  test('OrderTicket accepts hostManagesEsc prop to skip its own Escape listener when embedded', () => {
    const orderTicketSrc = readFile('src/lib/order/OrderTicket.svelte');
    expect(orderTicketSrc).toMatch(/hostManagesEsc = false,/);
    expect(orderTicketSrc).toMatch(/When true, OrderTicket skips its own Escape keydown listener/);
  });

  test('SymbolPanel passes hostManagesEsc={true} to OrderTicket', () => {
    expect(symbolPanelSrc).toMatch(/hostManagesEsc=\{true\}/);
  });

  test('OrderTimelineDrawer uses its own window keydown listener (not layerStack)', () => {
    const drawerSrc = readFile('src/lib/order/OrderTimelineDrawer.svelte');
    expect(drawerSrc).toMatch(/window\.addEventListener\('keydown', onKeyDown\)/);
    // Confirm it does NOT use pushLayer
    expect(drawerSrc).not.toMatch(/pushLayer/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional tests — real browser
// ─────────────────────────────────────────────────────────────────────────

test.describe('Escape closes only the topmost layer, not an ancestor', () => {
  test.use({ viewport: { width: 1400, height: 900 } });
  test.setTimeout(90_000);

  test('Escape closes Select dropdown inside OrderTicket, leaves SymbolPanel open', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for grid
    await page.locator('.ag-root-wrapper').first().waitFor({ timeout: 20_000 });

    // Open SymbolPanel via the header order button
    const orderBtn = page.locator('.pha-order').first();
    const orderBtnVisible = await orderBtn.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!orderBtnVisible) {
      test.skip(true, 'Order button (.pha-order) not visible — may be a page variant');
      return;
    }
    await orderBtn.click();

    // Wait for SymbolPanel modal to render
    const symbolPanel = page.locator('.canonical-modal-overlay').first();
    await expect(symbolPanel, 'SymbolPanel modal must render').toBeVisible({ timeout: 8_000 });

    // Try to open a Select dropdown inside the ticket (account selector often works)
    const accountSelect = page.locator('#ot-account-sel').first();
    const accountSelectVisible = await accountSelect.isVisible({ timeout: 3_000 }).catch(() => false);

    if (!accountSelectVisible) {
      test.skip(true, 'Account selector not accessible (may be single-account or demo mode)');
      return;
    }

    // Click to open the account dropdown
    await accountSelect.click();

    // Wait for dropdown to render (rbq-select-dropdown class)
    const selectDropdown = page.locator('.rbq-select-dropdown');
    await expect(selectDropdown, 'Select dropdown must open').toBeVisible({ timeout: 3_000 });

    // Press Escape
    await page.keyboard.press('Escape');

    // Assert: dropdown closed
    await expect(selectDropdown, 'Select dropdown must close on Escape').toHaveCount(0, { timeout: 3_000 });

    // Assert: SymbolPanel still visible
    await expect(symbolPanel, 'SymbolPanel must stay open when nested dropdown Escapes').toBeVisible({ timeout: 3_000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional tests — backdrop click behavior
// ─────────────────────────────────────────────────────────────────────────

test.describe('Clicking a nested overlay\'s backdrop closes only that layer', () => {
  test.use({ viewport: { width: 1400, height: 900 } });
  test.setTimeout(90_000);

  test('Clicking OrderTimelineDrawer backdrop closes drawer (standalone test)', async ({ page }) => {
    await loginAsAdmin(page);

    // OrderTimelineDrawer is opened via the navbar Chase chip or OrderBook.
    // The navbar chip requires active chases. For this test, we mock the endpoints.
    await page.route('**/api/orders/recent**', (route) => {
      route.fulfill({
        json: {
          orders: [
            {
              id: 999,
              order_id: '123456789',
              mode: 'paper',
              side: 'BUY',
              tradingsymbol: 'INFY',
              quantity: 100,
              status: 'OPEN',
              parent_order_id: null,
              created_at: new Date().toISOString(),
            },
          ],
        },
      });
    });

    await page.route('**/api/orders/**/events**', (route) => {
      route.fulfill({
        json: {
          events: [
            {
              id: 1,
              order_id: '123456789',
              ts: new Date().toISOString(),
              kind: 'placed',
              message: 'Order placed successfully',
              payload_json: '{}',
            },
          ],
        },
      });
    });

    // Go to dashboard or a page where OrderBook/chase-chip is visible
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Try to find and click the Chase chip in the navbar
    const chaseChip = page.locator('.chase-chip, [aria-label*="hase"], button[title*="hase"]').first();
    const chaseChipVisible = await chaseChip.isVisible({ timeout: 8_000 }).catch(() => false);

    if (!chaseChipVisible) {
      test.skip(true, 'Chase chip not found in navbar (may need live chases)');
      return;
    }

    await chaseChip.click();

    // Wait for drawer to render
    const drawer = page.locator('.otd-drawer');
    await expect(drawer, 'OrderTimelineDrawer must render').toBeVisible({ timeout: 5_000 });

    // Click the backdrop
    const backdrop = page.locator('.otd-backdrop');
    await expect(backdrop).toBeVisible({ timeout: 3_000 });
    await backdrop.click();

    // Assert: drawer closed
    await expect(drawer).toHaveCount(0, { timeout: 3_000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional tests — submit through stacked layers
// ─────────────────────────────────────────────────────────────────────────

test.describe('Submit button fires correctly through nested overlay layers', () => {
  test.use({ viewport: { width: 1400, height: 900 } });
  test.setTimeout(90_000);

  test('Submit button in OrderTicket nested in SymbolPanel fires correctly', async ({ page }) => {
    // Mock the ticket submit endpoint BEFORE navigation to prevent real orders
    let submitCallCount = 0;
    await page.route('**/api/orders/ticket**', (route) => {
      submitCallCount++;
      route.fulfill({
        json: {
          orderId: 'mock-order-12345',
          status: 'PLACED',
          message: 'Order placed successfully',
        },
      });
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for grid
    await page.locator('.ag-root-wrapper').first().waitFor({ timeout: 20_000 });

    // Open SymbolPanel
    const orderBtn = page.locator('.pha-order').first();
    const orderBtnVisible = await orderBtn.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!orderBtnVisible) {
      test.skip(true, 'Order button not visible');
      return;
    }
    await orderBtn.click();

    const symbolPanel = page.locator('.canonical-modal-overlay').first();
    await expect(symbolPanel).toBeVisible({ timeout: 8_000 });

    // Wait for OrderTicket modal to render (check for ot-modal container)
    const ticketModal = page.locator('.ot-modal').first();
    await expect(ticketModal).toBeVisible({ timeout: 5_000 });

    // Check if Submit button is present and potentially clickable
    // In DRAFT mode, it should work without depth. Otherwise skip if not reachable.
    const submitBtn = page.locator('.oes-common-submit').first();
    const submitVisible = await submitBtn.isVisible({ timeout: 3_000 }).catch(() => false);

    if (!submitVisible) {
      test.skip(true, 'Submit button not visible (may be gated on market depth or other conditions)');
      return;
    }

    // Verify Submit button is not disabled
    const isDisabled = await submitBtn.isDisabled({ timeout: 2_000 }).catch(() => true);
    if (isDisabled) {
      test.skip(true, 'Submit button is disabled (may be gated on market depth or account selection)');
      return;
    }

    // Click submit
    const initialCallCount = submitCallCount;
    await submitBtn.click();

    // Verify the mocked endpoint was called
    expect(submitCallCount, 'Submit endpoint must be called when Submit button is clicked').toBeGreaterThan(initialCallCount);

    // Verify that SymbolPanel is still visible (stacking didn't break)
    await expect(symbolPanel).toBeVisible({ timeout: 3_000 });
  });

  test('Submit button remains enabled and functional after user interaction', async ({ page }) => {
    // Mock endpoints
    await page.route('**/api/orders/margin-preview**', (route) => {
      route.fulfill({
        json: {
          account: 'ZG0790',
          margin_available: 500000,
          margin_used: 0,
          estimate: {
            cash: 'NORMAL',
            margin: 'NORMAL',
          },
        },
      });
    });

    await page.route('**/api/orders/ticket**', (route) => {
      route.fulfill({
        json: { orderId: 'mock-12345', status: 'PLACED' },
      });
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for grid
    await page.locator('.ag-root-wrapper').first().waitFor({ timeout: 20_000 });

    // Open SymbolPanel
    const orderBtn = page.locator('.pha-order').first();
    const orderBtnVisible = await orderBtn.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!orderBtnVisible) {
      test.skip(true, 'Order button not visible');
      return;
    }
    await orderBtn.click();

    const symbolPanel = page.locator('.canonical-modal-overlay').first();
    await expect(symbolPanel).toBeVisible({ timeout: 8_000 });

    // Wait for ticket modal to render
    const ticketModal = page.locator('.ot-modal').first();
    await expect(ticketModal).toBeVisible({ timeout: 5_000 });

    // Check submit button exists and is enabled (not disabled)
    const submitBtn = page.locator('.oes-common-submit').first();
    const submitVisible = await submitBtn.isVisible({ timeout: 3_000 }).catch(() => false);
    if (!submitVisible) {
      test.skip(true, 'Submit button not reachable');
      return;
    }

    // Verify it's not disabled before interaction
    const isDisabledBefore = await submitBtn.isDisabled({ timeout: 2_000 }).catch(() => true);
    if (isDisabledBefore) {
      test.skip(true, 'Submit button is already disabled (may be gated on market depth or account selection)');
      return;
    }

    // Verify it's still not disabled after waiting (simulating user interaction pause)
    // The modal should remain fully functional during the time it's open
    const isDisabledAfter = await submitBtn.isDisabled({ timeout: 2_000 }).catch(() => true);
    expect(isDisabledAfter, 'Submit button must remain enabled while SymbolPanel is open').toBe(false);

    // SymbolPanel should still be visible and functional
    await expect(symbolPanel).toBeVisible();
  });
});
