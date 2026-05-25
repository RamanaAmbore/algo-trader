// Dump window.__mainRows from dev to see anchor sort state
import { test } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('dump window.__mainRows on dev', async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://dev.ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('https://dev.ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(6000);

  const rows = await page.evaluate(() => /** @type {any} */ (window).__mainRows || []);
  console.log('total mainRows:', rows.length);
  // Find any anchor rows (src.u only, no p/h/w)
  console.log('\nAnchor rows (src contains "u"):');
  for (const r of rows) {
    if (r.src && r.src.includes('u') && !r.src.includes('p') && !r.src.includes('h') && !r.src.includes('w')) {
      console.log(JSON.stringify(r));
    }
  }
  console.log('\nRows around major transitions:');
  let lastMG = null;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.mg !== lastMG) {
      console.log(`  TRANSITION @ #${i}: ${lastMG} → ${r.mg}  | ts=${r.ts} u=${r.u} mo=${r.mo} kind=${r.kind} src=${r.src}`);
      lastMG = r.mg;
    }
  }

  // Specifically look for CRUDEOIL / GOLDM / INDIGO rows
  console.log('\nCRUDEOIL/GOLDM/INDIGO rows in mainRows:');
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.ts && /^(CRUDEOIL|GOLDM|INDIGO)/.test(r.ts)) {
      console.log(`  #${String(i).padStart(3)} ts=${r.ts.padEnd(28)} u=${(r.u||'').padEnd(15)} mg=${r.mg} mo=${r.mo} kind=${r.kind} src=${r.src}`);
    }
  }

  // Now compare against ag-grid's actual rendered row order
  console.log('\n=== ag-grid rendered row indices (DOM) ===');
  const grid = page.locator('.ag-body-viewport').first();
  const domRows = new Map();
  for (let y = 0; y < 8000; y += 200) {
    await grid.evaluate((el, top) => { el.scrollTop = top; }, y);
    await page.waitForTimeout(80);
    const batch = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.ag-pinned-left-cols-container .ag-row').forEach((row) => {
        const idx = parseInt(row.getAttribute('row-index') || '-1', 10);
        const sym = row.querySelector('.ag-col-sym')?.textContent?.trim() || '';
        out.push({ idx, sym });
      });
      return out;
    });
    for (const r of batch) if (r.idx >= 0 && !domRows.has(r.idx)) domRows.set(r.idx, r);
  }
  const domSorted = [...domRows.values()].sort((a, b) => a.idx - b.idx);
  console.log('First 25 DOM rows:');
  for (const r of domSorted.slice(0, 25)) console.log(`  #${String(r.idx).padStart(3)} ${r.sym}`);
  console.log('\nLast 25 DOM rows:');
  for (const r of domSorted.slice(-25)) console.log(`  #${String(r.idx).padStart(3)} ${r.sym}`);
});
