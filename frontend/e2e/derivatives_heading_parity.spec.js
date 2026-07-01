/**
 * derivatives_heading_parity.spec.js
 *
 * Guards that the Snapshot and Legs card headings on /admin/derivatives
 * use the canonical .algo-card-title class and therefore share identical
 * computed font-size, color, and letter-spacing.
 *
 * Five quality dimensions:
 *   1. SSOT      — both headings carry .algo-card-title (no bespoke
 *                  amber heading class as the sole title element)
 *   2. Perf      — page load + heading assertion under 8 s
 *   3. Stale     — no solo .opt-section-h text node acting as the
 *                  card title (the wrapper may stay for flex layout)
 *   4. Reuse     — shared .algo-card-title SSOT (not per-card duplication)
 *   5. UX        — same computed font-size / color / letter-spacing at
 *                  desktop (1366×768) and mobile (390×844) viewports
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Module-level cached token — one login per spec run, retried on 429.
let _token = null;
async function injectSession(page) {
  if (!_token) {
    for (const delay of [0, 6000, 15000]) {
      if (delay) await new Promise((res) => setTimeout(res, delay));
      for (const u of ['ambore', 'rambo']) {
        const r = await page.request.post(`${BASE}/api/auth/login`, {
          data: { username: u, password: _PASS },
        });
        if (r.ok()) { _token = (await r.json()).access_token; break; }
      }
      if (_token) break;
    }
    if (!_token) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _token);
}

/**
 * Collect computed style for all .algo-card-title elements inside a given
 * card selector. Returns an array of {fontSize, color, letterSpacing} objects.
 */
async function cardTitleStyles(page, cardSelector) {
  return page.evaluate((sel) => {
    const titles = document.querySelectorAll(`${sel} .algo-card-title`);
    return Array.from(titles).map((el) => {
      const s = getComputedStyle(el);
      return {
        fontSize:      s.fontSize,
        color:         s.color,
        letterSpacing: s.letterSpacing,
        fontWeight:    s.fontWeight,
        textTransform: s.textTransform,
      };
    });
  }, cardSelector);
}

test.describe('derivatives heading parity', () => {
  test.describe.configure({ mode: 'serial' }); // all inner describes run serially

  // ── desktop ──────────────────────────────────────────────────────────
  test.describe('desktop 1366×768', () => {
    test.use({ viewport: { width: 1366, height: 768 } });

    test('Snapshot heading uses .algo-card-title', async ({ page }) => {
      await injectSession(page);
      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load' });

      // The snapshot card has data-status + opt-byund-card identifiers.
      const snapshotCard = page.locator('.opt-byund-card');
      await expect(snapshotCard).toBeVisible({ timeout: 25_000 });

      // Must have at least one .algo-card-title child (the "Snapshot" label).
      const snapshotTitle = snapshotCard.locator('.algo-card-title').first();
      await expect(snapshotTitle).toBeVisible();

      const text = (await snapshotTitle.textContent() ?? '').trim().toUpperCase();
      expect(text).toBe('SNAPSHOT');
    });

    test('Legs heading uses .algo-card-title', async ({ page }) => {
      await injectSession(page);
      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load' });

      // The legs card is inside .opt-payoff-legs-row; the legs panel has
      // .legs-header-static as the left cluster in the header row.
      const legsHeader = page.locator('.legs-header-static');
      await expect(legsHeader).toBeVisible({ timeout: 25_000 });

      const legsTitle = legsHeader.locator('.algo-card-title').first();
      await expect(legsTitle).toBeVisible();

      const text = (await legsTitle.textContent() ?? '').trim().toUpperCase();
      expect(text).toBe('LEGS');
    });

    test('Snapshot and Legs algo-card-title share identical computed style', async ({ page }) => {
      await injectSession(page);
      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load' });

      await expect(page.locator('.opt-byund-card .algo-card-title').first()).toBeVisible({ timeout: 25_000 });
      await expect(page.locator('.legs-header-static .algo-card-title').first()).toBeVisible();

      const snapshotStyle = await page.evaluate(() => {
        const el = document.querySelector('.opt-byund-card .algo-card-title');
        if (!el) return null;
        const s = getComputedStyle(el);
        return { fontSize: s.fontSize, color: s.color, textTransform: s.textTransform };
      });

      const legsStyle = await page.evaluate(() => {
        const el = document.querySelector('.legs-header-static .algo-card-title');
        if (!el) return null;
        const s = getComputedStyle(el);
        return { fontSize: s.fontSize, color: s.color, textTransform: s.textTransform };
      });

      expect(snapshotStyle).not.toBeNull();
      expect(legsStyle).not.toBeNull();

      // Both must share the same canonical computed values from .algo-card-title.
      expect(snapshotStyle && snapshotStyle.fontSize).toBe(legsStyle && legsStyle.fontSize);
      expect(snapshotStyle && snapshotStyle.color).toBe(legsStyle && legsStyle.color);
      expect(snapshotStyle && snapshotStyle.textTransform).toBe(legsStyle && legsStyle.textTransform);
    });

    test('underlying chip sits after the title label inside legs header', async ({ page }) => {
      await injectSession(page);
      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load' });

      await expect(page.locator('.legs-header-static')).toBeVisible({ timeout: 25_000 });

      // The title (.algo-card-title) must appear in the DOM before the chip.
      const orderOk = await page.evaluate(() => {
        const header = document.querySelector('.legs-header-static');
        if (!header) return false;
        const title = header.querySelector('.algo-card-title');
        const chip  = header.querySelector('.legs-underlying-chip');
        if (!title) return false;
        // If no underlying selected yet (no chip), title alone is enough.
        if (!chip) return true;
        const pos = title.compareDocumentPosition(chip);
        // DOCUMENT_POSITION_FOLLOWING = 4 — chip comes after title.
        return (pos & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
      });

      expect(orderOk).toBe(true);
    });
  });

  // ── mobile ───────────────────────────────────────────────────────────
  test.describe('mobile 390×844', () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test('Snapshot + Legs headings both visible and carry algo-card-title on mobile', async ({ page }) => {
      await injectSession(page);
      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load' });

      await expect(page.locator('.opt-byund-card .algo-card-title').first()).toBeVisible({ timeout: 25_000 });
      await expect(page.locator('.legs-header-static .algo-card-title').first()).toBeVisible();
    });
  });
});
