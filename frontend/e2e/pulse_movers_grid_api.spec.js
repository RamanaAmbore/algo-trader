// Read grid rowData via ag-Grid API to bypass virtualization.

import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse movers grid API [${BASE}]`, async ({ page }) => {
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
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(15000);

  // Use ag-Grid's forEachNode to walk every row (incl. virtualized).
  const summary = await page.evaluate(() => {
    // ag-Grid stores the grid via api.getRowNode etc. Find the grid
    // through the DOM element's __agGridApi or look at internal store.
    // Simpler: trigger the api via the visible grid element.
    const allRows = [];
    /** @type {Record<string, number>} */
    const groups = {};
    // ag-Grid v32+ exposes the API via gridApi prop on the wrapper element.
    // Fallback: read row data attributes.
    const rowsInDom = document.querySelectorAll('.ag-row');
    rowsInDom.forEach((r) => {
      const cls = r.className;
      const m = cls.match(/\bmover-(\w+)/);
      const key = m ? m[1] : null;
      if (key) groups[key] = (groups[key] || 0) + 1;
    });
    // Try ag-grid api accessor
    /** @type {any} */ const w = window;
    const gridDiv = document.querySelector('.unified-grid');
    let totalRows = 'unknown';
    if (gridDiv && /** @type {any} */ (gridDiv).__agComponent) {
      try {
        totalRows = /** @type {any} */ (gridDiv).__agComponent.api.getDisplayedRowCount();
      } catch (e) { /* ignore */ }
    }
    return { groupsInDom: groups, displayedRowCount: totalRows, rowsInDom: rowsInDom.length };
  });
  console.log(`summary: ${JSON.stringify(summary)}`);

  // Scroll the grid viewport to bring later rows into DOM
  const gridViewport = page.locator('.ag-body-viewport').first();
  if (await gridViewport.isVisible().catch(() => false)) {
    for (let i = 0; i < 20; i++) {
      await gridViewport.evaluate((el) => el.scrollTop += 800);
      await page.waitForTimeout(500);
      const summary2 = await page.evaluate(() => {
        /** @type {Record<string, number>} */ const groups = {};
        document.querySelectorAll('.ag-row').forEach((r) => {
          const m = r.className.match(/\bmover-(\w+)/);
          if (m) groups[m[1]] = (groups[m[1]] || 0) + 1;
        });
        return groups;
      });
      console.log(`scroll ${i}: ${JSON.stringify(summary2)}`);
    }
  }
});
