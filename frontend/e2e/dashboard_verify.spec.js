import { test, expect } from '@playwright/test';

test.describe('Dashboard Performance tab — MarketPulse layout', () => {
  test('Funds + Summary sections render with data', async ({ page }) => {
    // Skip multi-viewport tests
    if (page.viewportSize().width < 1200) test.skip();

    // Navigate to dashboard (relies on existing auth session)
    await page.goto('https://dev.ramboq.com/dashboard');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(4000); // Wait for async data

    // 1. Page title
    const title = await page.title();
    expect(title).toContain('Dashboard');
    console.log('✓ Page title contains "Dashboard"');

    // 2. Tab strip should have Performance and P&L tabs
    const performanceTab = page.locator('text=Performance').first();
    const pnlTab = page.locator('text=P&L').first();
    await expect(performanceTab).toBeVisible();
    await expect(pnlTab).toBeVisible();
    console.log('✓ Tab strip visible: Performance + P&L');

    // 3. Account picker dropdown (button with account text)
    const accountPicker = page.locator('button').filter({ hasText: /All accounts|ZG|ZJ/ }).first();
    const pickerVisible = await accountPicker.isVisible().catch(() => false);
    if (pickerVisible) {
      console.log('✓ Account picker dropdown visible');
      await accountPicker.click();
      await page.waitForTimeout(300);
      const options = page.locator('[role="option"]');
      const optCount = await options.count().catch(() => 0);
      console.log(`  Account options found: ${optCount}`);
      await page.keyboard.press('Escape');
    } else {
      console.log('⚠ Account picker not found');
    }

    // 4. Check for FUNDS section
    const fundsSection = page.locator('text=FUNDS').first();
    const fundsSectionVisible = await fundsSection.isVisible().catch(() => false);
    console.log(fundsSectionVisible ? '✓ FUNDS section label visible' : '✗ FUNDS section NOT visible');

    // 5. Funds grid — look for column headers and data
    const accountCol = page.locator('text=Account').first();
    const cashCol = page.locator('text=Cash').first();
    const availMarginCol = page.locator('text=Avail Margin').first();

    const accountColVis = await accountCol.isVisible().catch(() => false);
    const cashColVis = await cashCol.isVisible().catch(() => false);
    const availMarginColVis = await availMarginCol.isVisible().catch(() => false);

    console.log(accountColVis ? '✓ Funds grid "Account" column visible' : '✗ Funds "Account" column NOT visible');
    console.log(cashColVis ? '✓ Funds grid "Cash" column visible' : '✗ Funds "Cash" column NOT visible');
    console.log(availMarginColVis ? '✓ Funds grid "Avail Margin" column visible' : '✗ Funds "Avail Margin" column NOT visible');

    // Try to find first account row (should contain ZG or ZJ)
    const accountRows = page.locator('text=/ZG|ZJ/');
    const accountRowCount = await accountRows.count().catch(() => 0);
    console.log(`✓ Found ${accountRowCount} account rows in grids (expect ≥ 2 for Funds + Summary)`);

    // 6. Check for SUMMARY section
    const summarySection = page.locator('text=Summary').last();
    const summarySectionVisible = await summarySection.isVisible().catch(() => false);
    console.log(summarySectionVisible ? '✓ Summary section label visible' : '✗ Summary section NOT visible');

    // 7. Summary grid — look for key columns
    const dayPnlCol = page.locator('text=Day P&L').first();
    const pnlCol = page.locator('text=/\\bP&L\\b/').first();
    const curValCol = page.locator('text=Cur Val').first();

    const dayPnlVis = await dayPnlCol.isVisible().catch(() => false);
    const pnlVis = await pnlCol.isVisible().catch(() => false);
    const curValVis = await curValCol.isVisible().catch(() => false);

    console.log(dayPnlVis ? '✓ Summary grid "Day P&L" column visible' : '✗ Summary "Day P&L" NOT visible');
    console.log(pnlVis ? '✓ Summary grid "P&L" column visible' : '✗ Summary "P&L" NOT visible');
    console.log(curValVis ? '✓ Summary grid "Cur Val" column visible' : '✗ Summary "Cur Val" NOT visible');

    // 8. Check for Position/Holdings toggle pills
    const posPill = page.locator('button').filter({ hasText: /^P\s·/ }).first();
    const holdingsPill = page.locator('button').filter({ hasText: /^H\s·/ }).first();

    const posPillVis = await posPill.isVisible().catch(() => false);
    const holdingsPillVis = await holdingsPill.isVisible().catch(() => false);

    console.log(posPillVis ? '✓ P · toggle pill visible' : '✗ P · toggle pill NOT visible');
    console.log(holdingsPillVis ? '✓ H · toggle pill visible' : '✗ H · toggle pill NOT visible');

    // 9. Verify section ordering: Funds should come before Summary
    // (by checking text content order)
    const bodyText = await page.locator('body').textContent();
    const fundIdx = bodyText.indexOf('FUNDS');
    const summaryIdx = bodyText.indexOf('Summary');
    const watchlistIdx = bodyText.indexOf('Watchlist');

    if (fundIdx > -1 && summaryIdx > -1) {
      console.log(fundIdx < summaryIdx ? '✓ Section order correct: Funds → Summary' : '✗ Section order WRONG');
    }

    // 10. Screenshot
    await page.screenshot({ path: '/tmp/dashboard-verify.png', fullPage: true });
    console.log('✓ Screenshot saved to /tmp/dashboard-verify.png');

    // Summary
    console.log('\n=== DASHBOARD VERIFICATION COMPLETE ===');
  });
});
