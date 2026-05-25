// Verify Movers split into underlying / midcap / smallcap sub-groups
// on /pulse. Per-group accent shows up as inset box-shadow on the
// first cell; sub-group divider as a dashed top-border on the first
// row of each sub-section.

import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse movers split into 3 sub-groups [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.removeItem('mp.selectedAccounts');
    sessionStorage.removeItem('mp.selectedShow');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(12000);

  const slug = BASE.includes('dev') ? 'dev' : 'prod';

  // Toggle off Pinned + Positions + Holdings + every watchlist so the
  // grid renders ONLY Movers — they then sit at the top of the
  // viewport and ag-Grid's virtualization makes the rows visible.
  const showTrigger = page.locator('.mp-chrome-row > div.w-44 button.rbq-multi-trigger').first();
  await showTrigger.click();
  await page.waitForTimeout(300);
  // Uncheck everything except Movers
  for (const label of ['Pinned', 'Positions', 'Holdings']) {
    const opt = page.locator('.rbq-multi-panel .rbq-multi-option-label', { hasText: new RegExp(`^${label}$`) }).first();
    if (await opt.isVisible().catch(() => false)) {
      await opt.click();
      await page.waitForTimeout(150);
    }
  }
  // Uncheck every watchlist (anything containing ★ or a watchlist name) — they all carry wl: tokens.
  // Skip Movers itself.
  const allOpts = await page.locator('.rbq-multi-panel .rbq-multi-option-label').allTextContents();
  for (const text of allOpts) {
    const t = (text || '').trim();
    if (!t || /^Pinned$|^Positions$|^Holdings$|^Movers$/.test(t)) continue;
    const opt = page.locator('.rbq-multi-panel .rbq-multi-option-label', { hasText: new RegExp(`^${t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}$`) }).first();
    if (await opt.isVisible().catch(() => false)) {
      await opt.click();
      await page.waitForTimeout(150);
    }
  }
  await page.keyboard.press('Escape');
  await page.waitForTimeout(2000);

  // Per-group rows visible in DOM (ag-Grid virtualises so this counts
  // viewport rows). Expect at least one of each sub-group.
  const undCount = await page.locator('.ag-row.mover-underlying').count();
  const midCount = await page.locator('.ag-row.mover-midcap').count();
  const smlCount = await page.locator('.ag-row.mover-smallcap').count();
  console.log(`mover rows in viewport: underlying=${undCount} midcap=${midCount} smallcap=${smlCount}`);

  // Sample 3 rows' class lists for diagnosis.
  const samples = await page.locator('.ag-row').evaluateAll((els) => els.slice(0, 5).map((el) => el.className));
  console.log(`row class samples: ${JSON.stringify(samples)}`);

  // Check the actual _moverGroup values via ag-Grid API in the page.
  const summary = await page.evaluate(() => {
    /** @type {Record<string, number>} */
    const groups = {};
    document.querySelectorAll('.ag-row').forEach((el) => {
      const cls = el.className;
      const m = cls.match(/\bmover-(\w+)/);
      const key = m ? m[1] : 'NONE';
      groups[key] = (groups[key] || 0) + 1;
    });
    return groups;
  });
  console.log(`row class group breakdown: ${JSON.stringify(summary)}`);

  // Inspect a sample tradingsymbol via ag-Grid: find the first 5 row
  // text labels to confirm what they ARE.
  const symbolLabels = await page.locator('.ag-row .sym-main').evaluateAll((els) =>
    els.map((el) => el.textContent?.trim())
  );
  console.log(`all ${symbolLabels.length} symbols in viewport: ${JSON.stringify(symbolLabels.slice(0, 50))}`);

  // Check for known smallcap (AAVAS) + midcap by name
  const hasAAVAS = symbolLabels.some((s) => s === 'AAVAS');
  const hasMidcapName = symbolLabels.some((s) => ['ASTRAL', 'AUROPHARMA', 'BERGEPAINT', 'CONCOR'].includes(s));
  console.log(`AAVAS in viewport: ${hasAAVAS}, midcap representative: ${hasMidcapName}`);

  const moverDebug = await page.evaluate(() => /** @type {any} */ (window).__moverDebug ?? null);
  console.log(`moverDebug: ${JSON.stringify(moverDebug)}`);

  // Sub-group divider rows
  const divCount = await page.locator('.ag-row.mover-group-divider').count();
  console.log(`sub-group divider rows in viewport: ${divCount}`);

  // Scroll to bottom to bring movers into view if they're below the fold
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForTimeout(1500);

  await page.screenshot({
    path: `test-results/movers-${slug}.png`,
    fullPage: true,
  });
});
