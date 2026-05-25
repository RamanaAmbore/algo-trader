import { test } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('dump anchor row data via ag-grid', async ({ page }) => {
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
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(5000);

  const data = await page.evaluate(() => {
    // ag-grid v33: each .ag-root has a __ag_grid_api hidden on a node
    // We can grab grid through the global reference left by ag-grid
    const root = document.querySelector('.ag-root');
    // No direct global — try to find the api on body data
    // Easier: grab row data from the displayed rows via getRowNode walking
    // We'll use this hacky approach — read all cells per row-index
    const rowEls = document.querySelectorAll('.ag-pinned-left-cols-container .ag-row');
    const out = [];
    rowEls.forEach((row) => {
      const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
      const sym = row.querySelector('.ag-col-sym')?.textContent?.trim() || '';
      // The full DOM node has data via comp ref
      const ag = row.__agComponent || row._agComponent;
      let majorGroup = null, majorOrder = null, tradingsymbol = null, underlying = null, srcKey = null;
      if (ag && ag.rowNode && ag.rowNode.data) {
        const d = ag.rowNode.data;
        majorGroup = d._majorGroup;
        majorOrder = d._majorOrder;
        tradingsymbol = d.tradingsymbol;
        underlying = d.underlying;
        srcKey = Object.keys(d.src || {}).filter(k => d.src[k]).join(',');
      }
      out.push({ idx, sym, majorGroup, majorOrder, tradingsymbol, underlying, srcKey, cls: row.className });
    });
    return out.sort((a, b) => a.idx - b.idx);
  });

  // scroll to get every row
  const grid = page.locator('.ag-body-viewport').first();
  const all = new Map();
  for (let y = 0; y < 9000; y += 200) {
    await grid.evaluate((el, top) => { el.scrollTop = top; }, y);
    await page.waitForTimeout(80);
    const batch = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.ag-pinned-left-cols-container .ag-row').forEach((row) => {
        const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
        const ag = row.__agComponent || row._agComponent;
        const d = (ag && ag.rowNode && ag.rowNode.data) || {};
        out.push({
          idx,
          tradingsymbol: d.tradingsymbol,
          underlying: d.underlying,
          majorGroup: d._majorGroup,
          majorOrder: d._majorOrder,
          srcKey: Object.keys(d.src || {}).filter(k => d.src[k]).join(','),
          kind: d.kind,
        });
      });
      return out;
    });
    for (const r of batch) if (r.idx >= 0 && !all.has(r.idx)) all.set(r.idx, r);
  }

  const sorted = [...all.values()].sort((a, b) => a.idx - b.idx);
  console.log('\nRow-index | major | order | src | kind | tradingsymbol | underlying');
  for (const r of sorted) {
    const tag = /CRUDE|GOLDM|INDIGO/.test(r.tradingsymbol || '') ? '★' : ' ';
    console.log(`${tag} #${String(r.idx).padStart(3)} ${(r.majorGroup||'').padEnd(11)} o=${r.majorOrder} src=${(r.srcKey||'').padEnd(5)} kind=${(r.kind||'').padEnd(5)} ${r.tradingsymbol} und=${r.underlying}`);
  }
});
