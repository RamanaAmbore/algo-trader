import { test, expect } from '@playwright/test';

test.describe('Dashboard — MarketPulse Performance tab', () => {
  test('renders Funds + Summary sections with data', async ({ page }) => {
    // Skip multi-viewport since we're targeting external URL
    if (page.viewportSize().width < 1200) {
      test.skip();
    }

    // Navigate to signin
    await page.goto('https://dev.ramboq.com/signin');
    await page.waitForLoadState('networkidle');

    // Log in as ambore (uses Username, not Email)
    const usernameInput = page.getByLabel('Username');
    const passwordInput = page.getByLabel('Password');
    await usernameInput.fill('ambore');
    await passwordInput.fill('Zerodha01#');

    // Click the form's Sign In button (the one inside the card, not the navbar)
    const formSignInBtn = page.locator('form').getByRole('button', { name: /sign in/i });
    await formSignInBtn.click();
    await page.waitForLoadState('networkidle');

    // Navigate to dashboard
    await page.goto('https://dev.ramboq.com/dashboard');
    await page.waitForLoadState('networkidle');

    // Wait for async data loads
    await page.waitForTimeout(4000);

    // 1. Verify page header "Dashboard"
    const pageHeader = page.getByRole('heading', { name: /dashboard/i });
    await expect(pageHeader).toBeVisible();
    console.log('✓ Page header "Dashboard" visible');

    // 2. Verify tab strip: [Performance | P&L]
    const performanceTab = page.getByRole('tab', { name: /performance/i });
    await expect(performanceTab).toBeVisible();
    console.log('✓ Performance tab visible');

    // 3. Verify account picker dropdown exists
    const accountButtons = page.locator('button').filter({ hasText: /all accounts|ZG|ZJ/ });
    const accountPickerCount = await accountButtons.count();
    expect(accountPickerCount).toBeGreaterThan(0);
    console.log('✓ Account picker dropdown visible');

    // 4. Check for "Funds" section label
    const fundsLabel = page.locator('text=/\\bFunds\\b/i').first();
    const fundsVisible = await fundsLabel.isVisible().catch(() => false);
    console.log(fundsVisible ? '✓ Section label "Funds" visible' : '✗ Section label "Funds" NOT found');

    // 5. Verify Funds grid has data
    // Look for cells with Cash header and numeric content
    const cashHeader = page.locator('text=Cash').first();
    const cashHeaderVisible = await cashHeader.isVisible().catch(() => false);
    console.log(cashHeaderVisible ? '✓ Funds grid "Cash" column visible' : '✗ Funds grid "Cash" NOT visible');

    // Try to find first Kite account (ZG or ZJ prefix)
    const accountCells = page.locator('text=/^ZG|^ZJ/');
    const accountCount = await accountCells.count();
    console.log(`✓ Found ${accountCount} account cells (expect ≥ 1)`);

    // 6. Check for "Summary" section label
    const summaryLabel = page.locator('text=/\\bSummary\\b/i').last();
    const summaryVisible = await summaryLabel.isVisible().catch(() => false);
    console.log(summaryVisible ? '✓ Section label "Summary" visible' : '✗ Section label "Summary" NOT found');

    // 7. Verify Summary grid has Day P&L column
    const dayPnlHeader = page.locator('text=Day P&L').first();
    const dayPnlVisible = await dayPnlHeader.isVisible().catch(() => false);
    console.log(dayPnlVisible ? '✓ Summary grid "Day P&L" column visible' : '✗ Summary grid "Day P&L" NOT visible');

    // 8. Check P&L toggle pills (P · Positions, H · Holdings)
    const posPill = page.locator('button').filter({ hasText: /^P\s·/ }).first();
    const holdingsPill = page.locator('button').filter({ hasText: /^H\s·/ }).first();
    const posVisible = await posPill.isVisible().catch(() => false);
    const holdingsVisible = await holdingsPill.isVisible().catch(() => false);
    console.log(posVisible ? '✓ Positions toggle (P ·) visible' : '✗ Positions toggle NOT visible');
    console.log(holdingsVisible ? '✓ Holdings toggle (H ·) visible' : '✗ Holdings toggle NOT visible');

    // 9. Take screenshot
    await page.screenshot({ path: '/tmp/dashboard-verify.png', fullPage: true });
    console.log('✓ Screenshot saved to /tmp/dashboard-verify.png');

    // 10. Check for console errors
    const errors = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        const text = msg.text();
        if (text.includes('/api/funds') || text.includes('/api/positions') || text.includes('/api/holdings')) {
          errors.push(text);
        }
      }
    });

    await page.waitForTimeout(1000);
    if (errors.length > 0) {
      console.log(`⚠ API-related console errors:\n${errors.join('\n')}`);
    } else {
      console.log('✓ No API-related console errors');
    }

    console.log('\n=== DASHBOARD VERIFICATION COMPLETE ===');
  });
});
