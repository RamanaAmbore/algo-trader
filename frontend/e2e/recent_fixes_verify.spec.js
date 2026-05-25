// Verify the four "fixed" items from this session against deployed dev.
// User reports only the agent fire_at_time field actually landed — the
// rest may be broken. Each test isolates one claim.
import { test, expect } from '@playwright/test';

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    if (await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'))) break;
    await new Promise(r => setTimeout(r, 300));
  }
}

// ─── 1. EQ / Spot button label on pulse option picker ───────────────────────
test('pulse: option picker shows EQ button for stock underlyings', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Find the add-symbol input + type RELIANCE
  const symInput = page.locator('input[placeholder*="symbol"i], input.field-input').first();
  await symInput.click();
  await symInput.fill('RELIANCE');
  await page.waitForTimeout(800);

  // Click the first typeahead row
  const tyaRow = page.locator('.typeahead-row, [class*="typeahead"] li, [class*="typeahead"] button').first();
  if (await tyaRow.count() > 0) {
    await tyaRow.click();
    await page.waitForTimeout(500);
  }

  // Option picker should open; look for the EQ button (amber for stock)
  const eqBtn = page.locator('button:has-text("EQ")').first();
  const spotBtn = page.locator('button:has-text("Spot")').first();
  const eqVisible = await eqBtn.isVisible().catch(() => false);
  const spotVisible = await spotBtn.isVisible().catch(() => false);
  console.log('[pulse RELIANCE] EQ visible:', eqVisible, 'Spot visible:', spotVisible);
  // RELIANCE is a stock → expect EQ button, not Spot
  await page.screenshot({ path: 'test-results/pulse-eq-button.png', fullPage: false });
  // Soft assertion — report state, don't fail (so the rest of the suite runs)
  if (!eqVisible) console.error('[FAIL] EQ button not visible for RELIANCE');
});

// ─── 2. Underlying anchors for GOLD/CRUDEOIL/INDIGO ─────────────────────────
test('pulse: GOLD/CRUDEOIL/INDIGO underlying anchors appear in grid', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);  // wait for buildUnified + ag-grid mount

  // Scroll the grid up to ensure top rows are rendered (ag-Grid
  // virtualises — only viewport rows + buffer are in the DOM).
  // Then pull DOM cells AND complement with ag-Grid's rowModel
  // which knows about every row regardless of render state.
  const symbols = await page.evaluate(async () => {
    const grid = document.querySelector('.unified-grid');
    if (!grid) return [];
    // Scroll grid container to top to force re-render
    const viewport = grid.querySelector('.ag-body-viewport');
    if (viewport) viewport.scrollTop = 0;
    await new Promise(r => setTimeout(r, 200));
    const out = new Set();
    // First: scrape currently-rendered cells (the original method)
    for (const c of grid.querySelectorAll('.ag-cell:first-child')) {
      const t = c.textContent?.trim();
      if (t) out.add(t);
    }
    // Then: scroll through the entire grid in chunks, scraping at each step
    const total = viewport?.scrollHeight || 0;
    const step = (viewport?.clientHeight || 200) - 50;
    for (let y = 0; y < total; y += step) {
      if (viewport) viewport.scrollTop = y;
      await new Promise(r => setTimeout(r, 120));
      for (const c of grid.querySelectorAll('.ag-cell:first-child')) {
        const t = c.textContent?.trim();
        if (t) out.add(t);
      }
    }
    return [...out];
  });
  console.log('[pulse total rows]', symbols.length);
  console.log('[pulse all symbols]');
  for (const s of symbols) console.log('  ', JSON.stringify(s));

  // Distinguish 'CRUDEOIL → CRUDEOIL26JUNFUT' (anchor row) from
  // 'CRUDEOIL26JUN9000CE P -1,000' (option position row). Anchor rows
  // contain ' → ' as the underlying→nearest-fut/spot connector.
  const anchorFor = (root) => symbols.find(s => s.startsWith(root + ' → '))
    || symbols.find(s => s.startsWith(root + 'W'));  // index-style anchor
  const positionFor = (root) => symbols.find(s => s.startsWith(root)
    && !s.startsWith(root + ' → ') && !s.startsWith(root + 'W'));

  for (const w of ['GOLD', 'CRUDEOIL', 'INDIGO', 'GOLDM']) {
    const anchor = anchorFor(w);
    const pos    = positionFor(w);
    console.log(`  ${w}: anchor=${anchor || '✗ MISSING'} | pos=${pos || 'none'}`);
    // Full enumeration — every row that contains this prefix
    const all = symbols.filter(s => s.toUpperCase().includes(w));
    console.log(`    all ${w} rows (${all.length}):`, all.slice(0, 10));
  }
  await page.screenshot({ path: 'test-results/pulse-underlyings.png', fullPage: true });
});

// ─── 3. Dashboard equity curve fetches + shows data ─────────────────────────
test('dashboard: intraday equity curve fetches & renders points', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });

  // Intercept the API call to see what backend returns
  let lastEquityResp = null;
  await page.route('**/api/charts/intraday-equity*', async (route) => {
    const resp = await route.fetch();
    lastEquityResp = await resp.json().catch(() => null);
    await route.fulfill({ response: resp });
  });

  await signIn(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  console.log('[equity API response]', JSON.stringify(lastEquityResp).slice(0, 200));
  const pointsCount = lastEquityResp?.points?.length ?? 0;
  console.log('[equity points returned by API]', pointsCount);

  // Check if the SVG polyline has data (rendered)
  const polyPoints = await page.evaluate(() => {
    const poly = document.querySelector('.eq-svg polyline');
    return poly?.getAttribute('points') || '';
  });
  console.log('[equity polyline points string length]', polyPoints.length);

  // Check the empty-message
  const emptyTxt = await page.locator('.eq-empty').textContent().catch(() => null);
  console.log('[equity empty message]', emptyTxt);

  await page.screenshot({ path: 'test-results/dashboard-equity-curve.png', fullPage: false, clip: { x: 0, y: 100, width: 1440, height: 400 } });
});

// ─── 4. Agent fire_at_time field present + savable ──────────────────────────
test('agents: fire_at_time field present in edit form (control check)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/agents', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  // Open the first agent's edit form
  const firstRow = page.locator('.algo-status-card').first();
  await firstRow.click();
  await page.waitForTimeout(400);

  // Some pages have an explicit Edit button after expanding
  const editBtn = page.locator('button:has-text("Edit")').first();
  if (await editBtn.count() > 0) {
    await editBtn.click();
    await page.waitForTimeout(500);
  }

  // Look for the time input
  const timeInput = page.locator('input[type="time"]').first();
  const timeVisible = await timeInput.isVisible().catch(() => false);
  console.log('[agents fire_at_time input visible]', timeVisible);
  await page.screenshot({ path: 'test-results/agents-fire-at-time.png', fullPage: false });
});
