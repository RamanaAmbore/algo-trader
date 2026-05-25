// Dump the raw row data from ag-grid (via grid API) to see _majorGroup/Order
import { test } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('dump grid rows with _majorGroup/_majorOrder', async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1200 });
  // Force fresh load — bypass service worker / disk cache
  await page.context().route('**/*', (route) => route.continue());
  await page.goto('https://ramboq.com/pulse?_=' + Date.now(), { waitUntil: 'networkidle' });
  await page.waitForTimeout(5000);

  // Use ag-grid's `__grid_api` if exposed, else fall back to reading via attributes
  const rows = await page.evaluate(() => {
    // Find any cell, walk to the grid root, read row models from internal API
    // Simpler: read all row models via the .ag-row data-id mapping
    const rowEls = document.querySelectorAll('.ag-pinned-left-cols-container .ag-row');
    const out = [];
    rowEls.forEach((row) => {
      const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
      const sym = row.querySelector('.ag-col-sym')?.textContent?.trim() || '';
      const cls = row.className;
      out.push({ idx, sym, cls });
    });
    return out.sort((a, b) => a.idx - b.idx);
  });

  // Scroll to load all
  const grid = page.locator('.ag-body-viewport').first();
  const all = new Map();
  for (let y = 0; y < 8000; y += 200) {
    await grid.evaluate((el, top) => { el.scrollTop = top; }, y);
    await page.waitForTimeout(100);
    const batch = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.ag-pinned-left-cols-container .ag-row').forEach((row) => {
        const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
        const sym = row.querySelector('.ag-col-sym')?.textContent?.trim() || '';
        const cls = row.className;
        out.push({ idx, sym, cls });
      });
      return out;
    });
    for (const r of batch) if (r.idx >= 0 && !all.has(r.idx)) all.set(r.idx, r);
  }

  // Find every row that says "major-XXX"
  console.log('\nRows with major-XXX class:');
  for (const r of [...all.values()].sort((a, b) => a.idx - b.idx)) {
    const m = r.cls.match(/major-(positions|holdings|watchlist|movers|pinned)/);
    if (m) console.log(`  #${String(r.idx).padStart(3)} ${m[0].padEnd(18)} sym=${r.sym}`);
  }
});
