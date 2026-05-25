// Capture the actual row index of anchors vs option rows in mainRows.
import { test, expect } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('pulse anchor position vs options — sorted order check', async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');

  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(5000);

  // Read row-index from each rendered ag-row's `row-index` attr.
  // ag-grid sets `row-index` on every .ag-row so we can read the
  // canonical sort order regardless of scroll/recycle.
  const collect = async () => page.evaluate(() => {
    // sym column is pinned LEFT, so cell lives in pinned-left container
    const out = [];
    const symByIdx = new Map();
    document.querySelectorAll('.ag-pinned-left-cols-container .ag-row').forEach((row) => {
      const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
      const sym = row.querySelector('.ag-col-sym')?.textContent?.trim() || '';
      if (idx >= 0) symByIdx.set(idx, sym);
    });
    document.querySelectorAll('.ag-center-cols-container .ag-row').forEach((row) => {
      const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
      const cls = row.className;
      const sym = symByIdx.get(idx) || '';
      out.push({ idx, sym, cls });
    });
    return out;
  });

  // Scroll through the grid and accumulate
  const grid = page.locator('.ag-body-viewport').first();
  let all = [];
  for (let y = 0; y < 8000; y += 300) {
    await grid.evaluate((el, top) => { el.scrollTop = top; }, y);
    await page.waitForTimeout(150);
    all.push(...(await collect()));
  }
  // Dedupe by row-index
  const seen = new Map();
  for (const r of all) {
    if (r.idx >= 0 && !seen.has(r.idx)) seen.set(r.idx, r);
  }
  const sorted = [...seen.values()].sort((a, b) => a.idx - b.idx);

  // Dump EVERY row's major + sym so we can find where anchors land
  console.log('\n=== All rows (full grid) ===');
  let _lastMajor2 = '';
  for (const r of sorted) {
    let major = 'continued';
    if (r.cls.includes('major-watchlist')) major = '— WATCHLIST —';
    else if (r.cls.includes('major-positions')) major = '— POSITIONS —';
    else if (r.cls.includes('major-holdings')) major = '— HOLDINGS —';
    else if (r.cls.includes('major-movers')) major = '— MOVERS —';
    if (major !== 'continued') console.log(`\n${major} (row ${r.idx})`);
    const tag = r.cls.includes('row-und') ? '[U]'
              : r.cls.includes('pos-short') ? '[s]'
              : r.cls.includes('pos-long') ? '[L]'
              : r.cls.includes('row-hold') ? '[H]'
              : r.cls.includes('row-watch') ? '[W]'
              : '[ ]';
    console.log(`  #${String(r.idx).padStart(3)} ${tag} ${r.sym}`);
  }

  // suppressed below
  if (false) for (const r of sorted) {
    const interesting = /INDIGO|CRUDE|GOLDM/.test(r.sym);
    if (interesting) {
      const isAnchor = !/\d/.test(r.sym.split(/[^A-Z0-9]/)[0]) || r.cls.includes('row-und');
      const tag = r.cls.includes('major-positions') ? '[POS-FIRST]'
                : r.cls.includes('row-und') ? '[ANCHOR]'
                : r.cls.includes('pos-short') ? '[short-pos]'
                : r.cls.includes('pos-long') ? '[long-pos]'
                : '[?]';
      console.log(`  #${String(r.idx).padStart(3)} ${tag.padEnd(13)} ${r.sym}`);
    }
  }
});
