/**
 * Focused probe: does the /charts page's refresh button ever clear its
 * spinner? Watches the network for repeated /api/options/historical calls
 * to distinguish a backend hang from an effect re-entry loop.
 */
import { test } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
  }
  await page.context().addInitScript((t) => sessionStorage.setItem('ramboq_token', t), _cachedToken);
}

test('refresh-button forensic — 35s observation', async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);

  page.on('console', (msg) => {
    const t = msg.type();
    if (t === 'error' || t === 'warning') {
      console.log(`  [console.${t}] ${msg.text().slice(0, 200)}`);
    }
  });
  page.on('pageerror', (e) => console.log(`  [pageerror] ${String(e).slice(0, 200)}`));

  const histCalls = [];
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/')) {
      histCalls.push({
        ts: Date.now(),
        status: r.status(),
        url: u.slice(u.indexOf('/api/')),
      });
    }
  });

  await page.goto(`${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`, { waitUntil: 'domcontentloaded' });
  console.log(`\n[t=0] navigated`);

  // Sample button state every 2.5s
  for (let i = 1; i <= 14; i++) {
    await page.waitForTimeout(2500);
    const btn = page.locator('button[title*="efresh" i], button[aria-label*="efresh" i]').first();
    const c = await btn.count();
    if (c === 0) { console.log(`[t=${i*2.5}s] no refresh button yet`); continue; }
    const state = await btn.evaluate((el) => ({
      title: el.getAttribute('title'),
      disabled: el.disabled,
      ariaLabel: el.getAttribute('aria-label'),
      cls: typeof el.className === 'string' ? el.className.slice(0, 80) : '<svg>',
    }));
    console.log(`[t=${i*2.5}s] btn:`, JSON.stringify(state), `histCalls=${histCalls.length}`);
    if (i === 6) {
      // Click refresh manually at t=15s to test the click path
      console.log('  >>> CLICKING refresh now <<<');
      await btn.click({ force: true }).catch(e => console.log('  click err:', e.message));
    }
  }
  console.log('\n=== ALL /api/options/historical or /api/charts CALLS ===');
  for (const c of histCalls) console.log(`  +${c.ts - histCalls[0]?.ts}ms ${c.status} ${c.url}`);
});
