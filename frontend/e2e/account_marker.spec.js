/**
 * Per-account identity stripe (`.ag-col-acct`).
 *
 * Each row's account cell carries a 3px left border in one of the 7
 * hues from `$lib/account.js::ACCT_PALETTE`. TOTAL rows resolve to
 * transparent. Real account IDs render unmasked for admin sessions
 * (Kite `Z[A-Z]\d{4}`, Dhan `DH\d{4}`, Groww `GR` + 4 alnum shapes).
 *
 * Default stripe rule lives in app.css; the per-row colour is
 * injected via cellStyle's --acct-stripe custom property.
 *
 * Colour assignment (2026-09 audit, A1) — `acctColor()` prefers a
 * deterministic RANK (position in the operator-configured account
 * order, `accountDisplayOrder`) over a djb2 hash, because with only
 * 5 real accounts a hash mod either the old 7-hue or the (now
 * deleted) duplicated 8-hue palette collided — two accounts landing
 * on the identical stripe colour, defeating the whole point of a
 * per-account identity signal. The rank list loads asynchronously
 * (GET /api/admin/brokers/order at app boot) — before it lands,
 * `acctColor()` falls back to the hash, which CAN collide for the
 * real account set. `_waitForDistinctColors()` below polls until the
 * grid settles on distinct colours (rank applied) rather than
 * asserting immediately, so this test isn't flaky against that race.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 25_000;

// rgb() forms of $lib/account.js's ACCT_PALETTE (7 hues, all-hex — no
// CSS var entries, so every caller that does `acctColor(x) + '1a'`
// string-concat stays valid CSS).
const HASH_COLORS = new Set([
  'rgb(251, 191, 36)',  // amber   #fbbf24
  'rgb(125, 211, 252)', // sky     #7dd3fc
  'rgb(167, 139, 250)', // violet  #a78bfa
  'rgb(74, 222, 128)',  // green   #4ade80
  'rgb(244, 114, 182)', // pink    #f472b6
  'rgb(165, 180, 252)', // indigo  #a5b4fc
  'rgb(240, 171, 252)', // fuchsia #f0abfc
]);

// Real account ID shapes across all 3 brokers (Kite / Dhan / Groww).
const REAL_ACCOUNT_RE = /^(Z[A-Z]\d{4}|DH\d{4}|GR[0-9A-Z]{4})$/;

/** Pull every BODY .ag-col-acct cell's text + computed stripe. */
async function _readAcctCells(page) {
  return page.evaluate(() => {
    const out = /** @type {Array<{text:string,bw:string,bc:string}>} */ ([]);
    for (const el of document.querySelectorAll('.ag-row .ag-cell.ag-col-acct')) {
      const cs = getComputedStyle(/** @type {HTMLElement} */ (el));
      out.push({
        text: el.textContent?.trim() || '',
        bw: cs.borderLeftWidth,
        bc: cs.borderLeftColor,
      });
    }
    return out;
  });
}

/**
 * Poll `.ag-col-acct` cells until distinct real-account texts resolve
 * to distinct stripe colours (rank-based assignment has applied), or
 * until `timeoutMs` elapses — whichever first. Guards against the
 * order-map load race described in the file header: reading colours
 * too early can catch the (potentially colliding) hash fallback.
 */
async function _waitForDistinctColors(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = [];
  while (Date.now() < deadline) {
    const cells = await _readAcctCells(page);
    const real = cells.filter(c => c.bw === '3px' && REAL_ACCOUNT_RE.test(c.text));
    last = real;
    if (real.length) {
      const byText = new Map();
      for (const c of real) byText.set(c.text, c.bc);
      const colors = [...byText.values()];
      const distinctTexts = byText.size;
      const distinctColors = new Set(colors).size;
      if (distinctTexts === distinctColors) return { real, cells };
    }
    await page.waitForTimeout(500);
  }
  return { real: last, cells: last };
}

test.describe('Account marker — left-edge stripe', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('non-TOTAL row has 3px coloured stripe + unmasked account text', async ({ page }) => {
    await page.goto('/dashboard');
    // Wait for at least one BODY-row account cell to populate. The
    // header cell also carries `.ag-col-acct` (because the column's
    // headerClass mirrors its cellClass for theme parity), so we
    // scope on `.ag-row .ag-col-acct` to skip the header.
    const bodyCell = page.locator('.ag-row .ag-cell.ag-col-acct').first();
    try {
      await bodyCell.waitFor({ state: 'attached', timeout: TIMEOUT });
    } catch (_) {
      test.skip(true, 'no body rows in any grid — book empty?');
    }

    const { real, cells } = await _waitForDistinctColors(page, TIMEOUT);

    if (!cells.length) {
      test.skip(true, 'no body account cells rendered — book empty? skip');
    }

    // Find a non-TOTAL row with a real account ID. Admins see real
    // values like `ZG0790` (NOT masked).
    const first = real.find(c => REAL_ACCOUNT_RE.test(c.text));
    if (!first) {
      test.skip(true, `no real-account cells at 3px — likely book is all TOTAL or sim/empty; cells=${JSON.stringify(cells.slice(0, 5))}`);
    }

    expect(first.bw).toBe('3px');
    expect(HASH_COLORS.has(first.bc), `borderLeftColor ${first.bc} not in palette`).toBe(true);

    // Distinctness — the actual bug this suite exists to catch. Two
    // DIFFERENT real accounts must never share the same stripe colour
    // (verified regression: djb2 hash collided DH6847 + DH3747 under
    // both the 7-hue and the now-deleted duplicated 8-hue palette).
    const byText = new Map();
    for (const c of real) {
      if (!REAL_ACCOUNT_RE.test(c.text)) continue;
      if (byText.has(c.text)) {
        expect(byText.get(c.text), `account ${c.text} rendered with inconsistent stripe colour across rows`).toBe(c.bc);
      } else {
        byText.set(c.text, c.bc);
      }
    }
    const distinctAccounts = [...byText.keys()];
    const distinctColors = new Set(byText.values());
    expect(distinctColors.size, `expected ${distinctAccounts.length} distinct accounts (${distinctAccounts.join(',')}) to render ${distinctAccounts.length} distinct stripe colours, got ${distinctColors.size}: ${JSON.stringify([...byText.entries()])}`)
      .toBe(distinctAccounts.length);

    // TOTAL rows (if present) → transparent border.
    const total = cells.find(c => c.text === 'TOTAL');
    if (total) {
      // CSS engine renders `transparent` and `rgba(0,0,0,0)` identically.
      expect(['rgba(0, 0, 0, 0)', 'transparent']).toContain(total.bc);
    }
  });
});
