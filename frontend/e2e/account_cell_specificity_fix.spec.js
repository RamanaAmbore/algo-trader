/**
 * account_cell_specificity_fix.spec.js
 *
 * Covers the 2026-09-30 Wave-1 consistency-pass fix for the account-cell
 * background specificity bug (app.css):
 *
 *   `.ag-theme-algo .ag-cell.ag-col-fill` (3-class compound, `!important`)
 *   was unconditionally painting a flat 8% amber tint over any cell that
 *   ALSO carried `ag-col-acct` (NavBreakdown's P/M/C/H account column,
 *   `cellClass: 'ag-col-fill ag-col-acct'`), because the bare
 *   `.ag-theme-algo .ag-col-acct` rule's background-color declaration had
 *   no `!important` and lower specificity — so it always lost. Only the
 *   3px stripe (which IS `!important`) ever showed through. Fixed via a
 *   new `.ag-theme-algo .ag-cell.ag-col-fill.ag-col-acct` override rule
 *   (4-class compound, `!important`) that wins on specificity.
 *
 * Also covers the companion OrderBook.svelte GTT-card fix: `.oc-acct`
 * (shared with OrderCard.svelte) needs `--acct-color` set inline — it was
 * missing on the GTT card, so the stripe rendered transparent.
 *
 * Test 1 is a DETERMINISTIC synthetic-DOM check (injects real markup
 * shapes into a live page that already loaded app.css) — it does not
 * depend on the operator's book having NavBreakdown data, so it can't be
 * skipped the way a live-data test can. It also directly verifies the
 * three cell shapes this bug class touches: NavBreakdown's shape (both
 * classes), BrokerHealthBadge's shape (acct only, must stay unchanged),
 * and a header cell with both classes (must stay flat amber — the new
 * override rule is scoped to `.ag-cell`, never `.ag-header-cell`).
 *
 * Test 2 is a best-effort LIVE check against NavBreakdown on /dashboard
 * (skips, not fails, if the operator's book has no account rows — same
 * convention as account_marker.spec.js's `_waitForDistinctColors`).
 *
 * Test 3 confirms BrokerHealthBadge's live rendering is unchanged by
 * this fix (it never carries `ag-col-fill`).
 *
 * Test 4 covers the OrderBook GTT-card stripe fix with mocked data,
 * reusing orderbook_gtt_chip.spec.js's auth + route-mock idiom.
 *
 * Five quality dimensions:
 *   1. SSOT   — asserts the actual computed CSS result of app.css's cascade,
 *               not component-internal state
 *   2. Perf   — synthetic test needs only one page load; live tests reuse
 *               existing polling, no extra network round-trips
 *   3. Stale  — explicitly checks the NOT-touched cases (BrokerHealthBadge,
 *               header cells) alongside the fixed case, so a future
 *               regression that over-broadens the new selector is caught
 *   4. Reuse  — loginAsAdmin fixture + the account_marker.spec.js /
 *               orderbook_gtt_chip.spec.js established idioms
 *   5. UX     — verifies the actual rendered colour (alpha + hue), not just
 *               presence of a CSS class
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/account_cell_specificity_fix.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 25_000;

// rgb() forms of $lib/account.js's ACCT_PALETTE (7 hues) — used to
// recognise a real per-account colour landed on --acct-color.
const PALETTE_RGB = new Set([
  'rgb(251, 191, 36)',  // amber   #fbbf24
  'rgb(125, 211, 252)', // sky     #7dd3fc
  'rgb(167, 139, 250)', // violet  #a78bfa
  'rgb(74, 222, 128)',  // green   #4ade80
  'rgb(244, 114, 182)', // pink    #f472b6
  'rgb(165, 180, 252)', // indigo  #a5b4fc
  'rgb(240, 171, 252)', // fuchsia #f0abfc
]);

const REAL_ACCOUNT_RE = /^(Z[A-Z]\d{4}|DH\d{4}|GR[0-9A-Z]{4})$/;

/**
 * Parse an alpha channel out of either `rgba(r, g, b, a)` or the
 * `color(srgb r g b / a)` form some Chromium versions serialise
 * `color-mix()` results to.
 * @param {string} colorStr
 * @returns {number | null}
 */
function _alphaOf(colorStr) {
  let m = colorStr.match(/rgba?\([^)]*,\s*([\d.]+)\s*\)/);
  if (m) return parseFloat(m[1]);
  m = colorStr.match(/\/\s*([\d.]+)\s*\)/);
  if (m) return parseFloat(m[1]);
  // Opaque rgb()/color() with no alpha segment at all = alpha 1.
  if (/^rgb\(/.test(colorStr) || /^color\(/.test(colorStr)) return 1;
  return null;
}

test.describe('Account-cell background specificity fix', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('synthetic: ag-col-fill + ag-col-acct cell shows account tint, not flat amber', async ({ page }) => {
    // Any logged-in algo page loads app.css — /dashboard is a safe,
    // already-authenticated landing point. Wait for a real ag-theme-algo
    // element to attach first — confirms app.css's rules are actually
    // active, not just that the DOM is parsed (stylesheets can still be
    // loading at the `domcontentloaded` event in a Vite dev server).
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    await page.waitForSelector('.ag-theme-algo', { timeout: TIMEOUT });

    const result = await page.evaluate(() => {
      const root = document.createElement('div');
      root.className = 'ag-theme-algo';
      root.style.position = 'fixed';
      root.style.top = '-9999px';
      document.body.appendChild(root);

      const row = document.createElement('div');
      row.className = 'ag-row';
      root.appendChild(row);

      // Shape 1 — NavBreakdown's P/M/C/H account column: BOTH classes,
      // non-amber accent so the amber-vs-tint distinction is unambiguous.
      const both = document.createElement('div');
      both.className = 'ag-cell ag-col-fill ag-col-acct';
      both.style.setProperty('--acct-color', '#7dd3fc'); // sky
      row.appendChild(both);

      // Shape 2 — BrokerHealthBadge's account column: acct ONLY, no fill.
      // Must render identically before and after this fix.
      const acctOnly = document.createElement('div');
      acctOnly.className = 'ag-cell ag-col-acct';
      acctOnly.style.setProperty('--acct-color', '#7dd3fc');
      row.appendChild(acctOnly);

      // Shape 3 — a plain fill cell with no account identity at all
      // (most ag-theme-algo numeric columns under a TOTAL-style fill).
      const fillOnly = document.createElement('div');
      fillOnly.className = 'ag-cell ag-col-fill';
      row.appendChild(fillOnly);

      // Shape 4 — a HEADER cell carrying both classes (mirrored
      // headerClass) — must stay flat amber; the new override rule is
      // scoped to `.ag-cell`, never `.ag-header-cell`.
      const headerBoth = document.createElement('div');
      headerBoth.className = 'ag-header-cell ag-col-fill ag-col-acct';
      headerBoth.style.setProperty('--acct-color', '#7dd3fc');
      root.appendChild(headerBoth);

      const out = {
        both: getComputedStyle(both).backgroundColor,
        acctOnly: getComputedStyle(acctOnly).backgroundColor,
        fillOnly: getComputedStyle(fillOnly).backgroundColor,
        headerBoth: getComputedStyle(headerBoth).backgroundColor,
      };
      root.remove();
      return out;
    });

    const alphaBoth = _alphaOf(result.both);
    const alphaAcctOnly = _alphaOf(result.acctOnly);
    const alphaFillOnly = _alphaOf(result.fillOnly);
    const alphaHeaderBoth = _alphaOf(result.headerBoth);

    // The fixed cell (both classes) must match the acct-only cell's tint
    // (14%, ~0.14) — NOT the flat fill's 8% (~0.08).
    expect(alphaBoth, `both=${result.both}`).not.toBeNull();
    expect(alphaAcctOnly, `acctOnly=${result.acctOnly}`).not.toBeNull();
    expect(alphaFillOnly, `fillOnly=${result.fillOnly}`).not.toBeNull();
    expect(Math.abs(alphaBoth - alphaAcctOnly), `both=${result.both} vs acctOnly=${result.acctOnly}`).toBeLessThan(0.01);
    expect(alphaBoth, `expected 14% tint, got both=${result.both} (flat-fill alpha was ${alphaFillOnly})`).toBeGreaterThan(alphaFillOnly + 0.02);

    // BrokerHealthBadge's shape (acct-only) is untouched by construction —
    // same value with or without the fix.
    expect(Math.abs(alphaAcctOnly - 0.14)).toBeLessThan(0.02);

    // The header cell (both classes) must stay its OWN unmodified flat
    // amber header-fill colour (`.ag-header-cell.ag-col-fill`'s own 14%
    // amber rule is untouched by this fix — it is a DIFFERENT alpha from
    // the body fill's 8% by original design) — confirms the new
    // `.ag-cell.ag-col-fill.ag-col-acct` override selector never reaches
    // `.ag-header-cell` (it requires `.ag-cell`). The real regression this
    // guards: the header rendering the SKY account tint instead of amber.
    expect(result.headerBoth, `headerBoth=${result.headerBoth}`).toMatch(/^rgba\(251,\s*191,\s*36,/);
    expect(result.headerBoth, 'header must not pick up the sky account tint').not.toBe(result.both);
  });

  test('live: NavBreakdown P-slot account cell shows account tint, not flat amber (skips if book empty)', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

    // Default NAV tab renders NavBreakdown's P slot without needing any
    // tab click (dashboard/+page.svelte mounts it unconditionally).
    const cell = page.locator('.ag-row .ag-cell.ag-col-fill.ag-col-acct').first();
    try {
      await cell.waitFor({ state: 'attached', timeout: TIMEOUT });
    } catch (_) {
      test.skip(true, 'no NavBreakdown P-slot rows rendered — book empty?');
    }

    const cells = await page.evaluate(() => {
      const out = [];
      for (const el of document.querySelectorAll('.ag-row .ag-cell.ag-col-fill.ag-col-acct')) {
        const cs = getComputedStyle(/** @type {HTMLElement} */ (el));
        out.push({ text: el.textContent?.trim() || '', bg: cs.backgroundColor });
      }
      return out;
    });

    const real = cells.filter(c => REAL_ACCOUNT_RE.test(c.text));
    if (!real.length) {
      test.skip(true, `no real-account P-slot rows — likely TOTAL-only or empty; cells=${JSON.stringify(cells.slice(0, 5))}`);
    }

    for (const c of real) {
      const alpha = _alphaOf(c.bg);
      // Must NOT be the flat 8% amber fill — must be the ~14% account tint.
      expect(alpha, `account ${c.text} bg=${c.bg}`).not.toBeNull();
      expect(alpha, `account ${c.text} rendered flat-fill amber (bg=${c.bg}) instead of its account tint`).toBeGreaterThan(0.10);
    }
  });

  test('live: BrokerHealthBadge account cell rendering is unchanged', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await chip.click();
    await expect(page.locator('.bh-modal')).toBeVisible({ timeout: 5_000 });

    const cell = page.locator('.bh-modal .ag-row .ag-cell.ag-col-acct').first();
    try {
      await cell.waitFor({ state: 'attached', timeout: 10_000 });
    } catch (_) {
      test.skip(true, 'no broker-health account rows rendered');
    }

    const cells = await page.evaluate(() => {
      const out = [];
      for (const el of document.querySelectorAll('.bh-modal .ag-row .ag-cell.ag-col-acct')) {
        const cs = getComputedStyle(/** @type {HTMLElement} */ (el));
        out.push({ text: el.textContent?.trim() || '', bg: cs.backgroundColor, bw: cs.borderLeftWidth });
      }
      return out;
    });
    const real = cells.filter(c => REAL_ACCOUNT_RE.test(c.text));
    if (!real.length) {
      test.skip(true, `no real-account rows — cells=${JSON.stringify(cells.slice(0, 5))}`);
    }
    for (const c of real) {
      expect(c.bw).toBe('3px');
      const alpha = _alphaOf(c.bg);
      // BrokerHealthBadge cells never carry ag-col-fill — this fix cannot
      // change their rendering; still 14% tint, same as before the fix.
      expect(alpha, `account ${c.text} bg=${c.bg}`).not.toBeNull();
      expect(Math.abs(alpha - 0.14), `account ${c.text} bg=${c.bg}`).toBeLessThan(0.03);
    }
  });
});

test.describe('MarketPulse account-cell font-weight (700 -> 600 consistency fix)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('right-grid .mp-acct-cell renders at font-weight 600 (skips if book empty)', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

    const cell = page.locator('.ag-theme-algo .mp-acct-cell').first();
    try {
      await cell.waitFor({ state: 'attached', timeout: TIMEOUT });
    } catch (_) {
      test.skip(true, 'no mp-acct-cell rows rendered — positions/holdings empty?');
    }

    const fw = await cell.evaluate((el) => getComputedStyle(el).fontWeight);
    expect(fw).toBe('600');
  });
});

// ── OrderBook GTT-card stripe fix ──────────────────────────────────────
// Mirrors orderbook_gtt_chip.spec.js's auth + route-mock idiom exactly.
const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

async function authOnce(page) {
  if (!_cachedAuth) {
    const envToken = process.env.PLAYWRIGHT_AUTH_TOKEN;
    let tok = envToken || null;
    if (!tok) {
      for (const delay of [0, 20000, 65000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: _AUTH_USER, password: _AUTH_PASS },
        });
        if (resp.ok()) { tok = (await resp.json()).access_token; break; }
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()}`);
      }
    }
    if (!tok) throw new Error('authOnce: login rate-limited');
    _cachedAuth = { token: tok, user_id: _AUTH_USER };
  }
  const { token, user_id } = _cachedAuth;
  await page.goto('/');
  await page.evaluate(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
  }, { tok: token, usr: user_id });
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });
}

const _NOW_ISO = '2026-09-30T04:30:00.000Z';
const _GTT_ROWS = [
  { gtt_id: '9501', account: 'T1', broker_id: 'kite', status: 'active',
    trigger_type: 'single', tradingsymbol: 'RBQ-STRIPE', exchange: 'NSE',
    trigger_values: [101.5], last_price: 100.2, orders: [], created_at: '2026-09-30T08:30:00' },
];

async function mockGttEndpoints(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: _GTT_ROWS, count: _GTT_ROWS.length }),
      });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook GTT card — account stripe colour', () => {
  test.setTimeout(60_000);

  test('.oc-acct on the GTT card shows a real colour, not transparent', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockGttEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    const gttChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^GTT$/ }) });
    await expect(gttChip).toHaveCount(1);
    await gttChip.click();

    const stripe = page.locator('.gtt-card .oc-acct').first();
    await expect(stripe).toBeVisible({ timeout: 10_000 });

    const { bw, bc } = await stripe.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { bw: cs.borderLeftWidth, bc: cs.borderLeftColor };
    });

    expect(bw).toBe('2px');
    // Must be a real palette colour, never transparent/currentColor-on-nothing.
    expect(['rgba(0, 0, 0, 0)', 'transparent']).not.toContain(bc);
    expect(PALETTE_RGB.has(bc), `borderLeftColor ${bc} not in ACCT_PALETTE`).toBe(true);
  });
});
