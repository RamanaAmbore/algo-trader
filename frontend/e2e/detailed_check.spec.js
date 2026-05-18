import { test, expect } from '@playwright/test';

test('dashboard detailed verification', async ({ page }) => {
  if (page.viewportSize().width < 1200) test.skip();

  await page.goto('https://dev.ramboq.com/dashboard');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000);

  // Get all text content in the Funds section
  const fundsLabel = page.locator('text=FUNDS').first();
  const fundsBounds = await fundsLabel.boundingBox();
  console.log('Funds section found at Y:', fundsBounds?.y);

  // Get cells with numeric values in the Funds area
  const cells = await page.locator('[role="gridcell"]').all();
  let fundsDataFound = false;

  for (let i = 0; i < Math.min(cells.length, 20); i++) {
    const text = await cells[i].textContent();
    const box = await cells[i].boundingBox();
    
    // Check if this cell is in the Funds grid area (roughly)
    if (box && box.y > (fundsBounds?.y || 0) && box.y < (fundsBounds?.y || 0) + 150) {
      if (text && text.trim()) {
        console.log(`Funds cell: "${text.trim().substring(0, 20)}"`);
        if (text.includes('ZG') || text.includes('₹') || text.includes('TOTAL')) {
          fundsDataFound = true;
        }
      }
    }
  }

  console.log('Funds grid has data:', fundsDataFound);

  // Now check Summary section
  const summaryLabel = page.locator('text=Summary').last();
  const summaryBounds = await summaryLabel.boundingBox();
  console.log('Summary section found at Y:', summaryBounds?.y);

  let summaryDataFound = false;
  for (let i = 0; i < Math.min(cells.length, 40); i++) {
    const text = await cells[i].textContent();
    const box = await cells[i].boundingBox();
    
    if (box && box.y > (summaryBounds?.y || 0) - 50 && box.y < (summaryBounds?.y || 0) + 150) {
      if (text && text.trim() && (text.includes('ZG') || text.includes('Day P&L') || text.includes('%'))) {
        console.log(`Summary cell: "${text.trim().substring(0, 25)}"`);
        summaryDataFound = true;
      }
    }
  }

  console.log('Summary grid has data:', summaryDataFound);

  // Get specific cell values for reporting
  const cash = page.locator('text=Cash').first();
  const availMargin = page.locator('text=Avail Margin').first();
  const dayPnl = page.locator('text=Day P&L').first();

  const cashVis = await cash.isVisible().catch(() => false);
  const availVis = await availMargin.isVisible().catch(() => false);
  const dayPnlVis = await dayPnl.isVisible().catch(() => false);

  console.log('\nColumn visibility:');
  console.log('  Cash:', cashVis);
  console.log('  Avail Margin:', availVis);
  console.log('  Day P&L:', dayPnlVis);

  // Take a screenshot of just the top portion
  const topSection = page.locator('main');
  await topSection.screenshot({ path: '/tmp/dashboard-top.png' });
  console.log('Screenshot saved: /tmp/dashboard-top.png');
});
