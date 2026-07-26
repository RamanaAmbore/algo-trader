import { test, expect } from '@playwright/test';

/**
 * Showcase page (/about) — verify feature cards render with expected
 * text for order ticket enhancements and adaptive chase aggressiveness.
 * Test runs on desktop viewport only (no mobile-specific changes for showcase).
 */

test.describe('Showcase page — order ticket features', () => {
  test('displays rich order entry context card', async ({ page }) => {
    // Navigate to showcase page (unauthenticated access allowed)
    await page.goto('/showcase');

    // Wait for the page to settle (FOUC gate: .show-ready state)
    await page.waitForSelector('.show-ready', { timeout: 5000 });

    // Check for the "Rich order entry context" card
    const richCardText = page.getByText('Rich order entry context');
    await expect(richCardText).toBeVisible();

    // Verify key features appear in the card bullets
    await expect(page.getByText('Contract LTP displayed in the modal header')).toBeVisible();
    await expect(page.getByText('Underlying spot price shown in header middle')).toBeVisible();
    await expect(page.getByText('Days-to-expiry chip in symbol meta row')).toBeVisible();
    await expect(page.getByText('OI, Volume, bid-ask spread displayed')).toBeVisible();
    await expect(page.getByText('Position entry price + unrealized P&L shown')).toBeVisible();
    await expect(page.getByText('ATM/ITM/OTM classification shown')).toBeVisible();
    await expect(page.getByText('Chase aggressiveness toggle')).toBeVisible();
    await expect(page.getByText('Exchange-closed badge in header')).toBeVisible();
    await expect(page.getByText('Wing-leg warning when a template')).toBeVisible();

    // Verify the card links to /orders
    const richCardLink = page.locator('.show-card').filter({
      hasText: 'Rich order entry context'
    }).getByRole('link', { name: /Open Orders/ });
    await expect(richCardLink).toBeVisible();
    await expect(richCardLink).toHaveAttribute('href', '/orders');
  });

  test('displays adaptive limit chasing card', async ({ page }) => {
    // Navigate to showcase page
    await page.goto('/showcase');

    // Wait for page settle
    await page.waitForSelector('.show-ready', { timeout: 5000 });

    // Check for the "Adaptive limit chasing" card
    const chaseCardText = page.getByText('Adaptive limit chasing');
    await expect(chaseCardText).toBeVisible();

    // Verify key features appear in the card bullets
    await expect(page.getByText('Low mode: re-quote to bid')).toBeVisible();
    await expect(page.getByText('Med mode: re-quote to midpoint')).toBeVisible();
    await expect(page.getByText('High mode: cross the spread')).toBeVisible();
    await expect(page.getByText('Chase chain for LIMIT orders')).toBeVisible();
    await expect(page.getByText('Partial-fill handling')).toBeVisible();
    await expect(page.getByText('Chase can be toggled per-ticket')).toBeVisible();

    // Verify the card links to /orders
    const chaseCardLink = page.locator('.show-card').filter({
      hasText: 'Adaptive limit chasing'
    }).getByRole('link', { name: /Open Orders/ });
    await expect(chaseCardLink).toBeVisible();
    await expect(chaseCardLink).toHaveAttribute('href', '/orders');
  });

  test('cards have correct visual styling', async ({ page }) => {
    // Navigate to showcase page
    await page.goto('/showcase');

    // Wait for page settle
    await page.waitForSelector('.show-ready', { timeout: 5000 });

    // Check that the Rich Order Entry card exists and has proper color accent
    const richCard = page.locator('.show-card').filter({
      hasText: 'Rich order entry context'
    });
    await expect(richCard).toBeVisible();

    // Verify card structure — each card should have tag, title, body, bullets
    const richCardTag = richCard.locator('.show-card-tag');
    await expect(richCardTag).toBeVisible();
    await expect(richCardTag).toHaveText('Trading');

    // Check Adaptive Chasing card also appears with Trading tag
    const chaseCard = page.locator('.show-card').filter({
      hasText: 'Adaptive limit chasing'
    });
    await expect(chaseCard).toBeVisible();
    const chaseCardTag = chaseCard.locator('.show-card-tag');
    await expect(chaseCardTag).toBeVisible();
    await expect(chaseCardTag).toHaveText('Trading');
  });

  test('all feature cards render without console errors', async ({ page }) => {
    // Capture console messages
    const consoleMessages = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleMessages.push(msg.text());
      }
    });

    // Navigate to showcase
    await page.goto('/showcase');
    await page.waitForSelector('.show-ready', { timeout: 5000 });

    // Assert no console errors
    expect(consoleMessages).toEqual([]);

    // Verify the grid renders with expected number of cards (12 total now: 10 original + 2 new)
    const cards = page.locator('.show-card');
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(12);
  });
});
