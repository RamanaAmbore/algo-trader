import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`holdings API direct [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  const r = await page.request.get(`${BASE}/api/holdings?fresh=1`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  const data = await r.json();
  const rows = data.rows || [];
  const accts = [...new Set(rows.map(r => r.account))].sort();
  console.log(`status=${r.status()} rows=${rows.length} accts=${JSON.stringify(accts)}`);
  // Find a TEJASNET row to confirm DH3747 surfaces
  const tejas = rows.find(r => r.tradingsymbol === 'TEJASNET');
  console.log(`TEJASNET row: ${tejas ? JSON.stringify(tejas) : 'NOT FOUND'}`);
});
