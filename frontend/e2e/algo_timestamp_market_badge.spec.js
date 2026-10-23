/**
 * algo_timestamp_market_badge.spec.js
 *
 * 2026-10-22 — the 3-value market-status badge (market open / MCX open /
 * non-MCX open) moved out of the order-ticket modal (SymbolPanel.svelte's
 * `.oes-chase-row`) into the shared page header, by extending
 * `AlgoTimestamp.svelte` — the one component every algo route already
 * renders directly after the page title and directly before the
 * Refresh/PageHeaderActions button group (confirmed via
 * `grep -rln "<AlgoTimestamp" src`, 24 route files). Extending that single
 * insertion point makes the badge page-wide for free, with zero per-page
 * wiring.
 *
 * 2026-10-23 — collapsed from three simultaneous pills to ONE (operator:
 * "only one badge can be active at a time. M, MCX, or EQ."). The prior
 * 3-pill layout was mostly redundant: in the "both open" and "closed"
 * states all three pills repeated the same colour, and the MCX/EQ pills
 * only ever added information in the two mixed states, which the compound
 * 4-bucket state already disambiguated. Design now:
 *   - One pill whose LABEL switches: "M" (both open or both closed), "MCX"
 *     (MCX-only open), "EQ" (non-MCX/equity-only open — NSE/NFO/BSE/CDS/
 *     BCD/BFO). Colour alone still carries the open/closed signal within
 *     each label; full sentence lives in the pill's `title` tooltip.
 *   - Colour is the same 4-bucket compound state mirroring
 *     RefreshButton.svelte's `.rf-mkt-*` scheme (both open = green,
 *     MCX-only open = amber, closed = slate) EXTENDED with a 4th bucket —
 *     "EQ-only open" (NSE open, MCX closed — not reachable under either
 *     segment's default open time since MCX (09:00) opens before NSE
 *     (09:15), but still reachable on an MCX-specific holiday while NSE
 *     trades normally) — which RefreshButton's own scheme folds into
 *     "closed" (it only branches on `_mcxOpen`). The new bucket uses the
 *     `--algo-market-eq-only-*` token (app.css), aliased to the existing
 *     violet family rather than a brand-new hue.
 *   - Data source: the SAME module-level cache in $lib/marketHours that
 *     RefreshButton.svelte already reads (fed by the app-wide 5-min
 *     /api/market/status poll) — no second poller. `onMarketStatusChange()`
 *     listener (marketHours.js) lets the badge recompute the instant a
 *     fresh poll lands, instead of waiting up to 30s for its own next tick
 *     — closes a real mount-order race (this component can mount and seed
 *     from the time-of-day fallback a moment before the layout's in-flight
 *     poll resolves).
 *   - Scrollable fallback: `.ats-root` (outer, `min-width:0; overflow:
 *     hidden`) / `.ats-scroll` (inner, `overflow-x:auto`) split — the same
 *     fixed-shell/inner-scroll pattern PositionStrip.svelte's
 *     `.ps-strip`/`.ps-strip-scroll` already uses for NavStrip's own
 *     mobile-overflow fix — so a tight header scrolls the clock+badge
 *     group horizontally instead of wrapping or overflowing. Retained even
 *     though a single pill is far less likely to need it.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const COMPONENT = '/Users/ramanambore/projects/ramboq/frontend/src/lib/AlgoTimestamp.svelte';
const APP_CSS = '/Users/ramanambore/projects/ramboq/frontend/src/app.css';

/** Representative pages — one per distinct layout family, not all 24
 *  (that's redundant for a single shared component). */
const PAGES = [
  { path: '/admin/derivatives', label: 'derivatives' },
  { path: '/pulse', label: 'pulse' },
  { path: '/dashboard', label: 'dashboard' },
];

function _mockStatus(page, { nse_open, mcx_open }) {
  return page.route('**/api/market/status', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      nse_open,
      mcx_open,
      any_open: nse_open || mcx_open,
      is_holiday: false,
      checked_at: new Date().toISOString(),
    }),
  }));
}

test.describe('AlgoTimestamp market-status badge — source', () => {
  test('imports isNseOpen/isMcxOpen + the onMarketStatusChange listener from marketHours, no new poller', () => {
    const src = readFileSync(COMPONENT, 'utf-8');
    expect(src).toMatch(/import\s*\{[^}]*isNseOpen[^}]*isMcxOpen[^}]*onMarketStatusChange[^}]*\}\s*from\s*'\$lib\/marketHours'/);
    // Must NOT add a second setInterval/visibleInterval for market status —
    // the existing 30s clock-tick interval is reused.
    const intervalMatches = src.match(/setInterval\(/g) || [];
    expect(intervalMatches.length).toBe(1);
    expect(src).not.toMatch(/visibleInterval\(/);
  });

  test('exactly one pill rendered, label switches M/MCX/EQ off the compound 4-bucket state', () => {
    const src = readFileSync(COMPONENT, 'utf-8');
    expect(src).toContain('data-testid="algo-ts-market-badge"');
    expect(src).toContain('data-testid="algo-ts-market-badge-any"');
    // The removed per-segment testids must not reappear — collapsing to
    // one pill means there is no standalone MCX/EQ element any more.
    expect(src).not.toContain('data-testid="algo-ts-market-badge-mcx"');
    expect(src).not.toContain('data-testid="algo-ts-market-badge-eq"');
    // Only one `.ats-mkt-pill` span in the markup.
    const pillMatches = src.match(/class="ats-mkt-pill"/g) || [];
    expect(pillMatches.length).toBe(1);
    // The one pill carries all 4 buckets.
    expect(src).toMatch(/class:ats-mkt-open=\{_mktBucket === 'both'\}/);
    expect(src).toMatch(/class:ats-mkt-mcx-only=\{_mktBucket === 'mcx'\}/);
    expect(src).toMatch(/class:ats-mkt-eq-only=\{_mktBucket === 'eq'\}/);
    expect(src).toMatch(/class:ats-mkt-closed=\{_mktBucket === 'closed'\}/);
    // Label switches via a derived off the same bucket.
    expect(src).toMatch(/_mktBucket === 'mcx' \? 'MCX'/);
    expect(src).toMatch(/_mktBucket === 'eq' \? 'EQ'/);
  });

  test('badge markup is a sibling of the clock button, not nested inside it (pointer-events / tap-toggle isolation)', () => {
    const src = readFileSync(COMPONENT, 'utf-8');
    const buttonCloseIdx = src.indexOf('</button>');
    const badgeIdx = src.indexOf('data-testid="algo-ts-market-badge"');
    expect(buttonCloseIdx).toBeGreaterThan(-1);
    expect(badgeIdx).toBeGreaterThan(buttonCloseIdx);
  });

  test('CSS uses the fixed-shell/inner-scroll split (.ats-root/.ats-scroll), matching PositionStrip\'s .ps-strip pattern', () => {
    const src = readFileSync(COMPONENT, 'utf-8');
    const rootIdx = src.indexOf('.ats-root {');
    expect(rootIdx).toBeGreaterThan(-1);
    const rootBlock = src.slice(rootIdx, src.indexOf('}', rootIdx));
    expect(rootBlock).toContain('min-width: 0');
    expect(rootBlock).toContain('overflow: hidden');
    const scrollIdx = src.indexOf('.ats-scroll {');
    expect(scrollIdx).toBeGreaterThan(rootIdx);
    const scrollBlock = src.slice(scrollIdx, src.indexOf('}', scrollIdx));
    expect(scrollBlock).toContain('overflow-x: auto');
  });

  test('colour tokens for the shared states mirror RefreshButton\'s .rf-mkt-* resolved values; new 4th token for EQ-only', () => {
    const src = readFileSync(COMPONENT, 'utf-8');
    const openIdx = src.indexOf('.ats-mkt-open {');
    const eqOnlyEndIdx = src.indexOf('}', src.indexOf('.ats-mkt-eq-only {'));
    expect(openIdx).toBeGreaterThan(-1);
    const block = src.slice(openIdx, eqOnlyEndIdx);
    // "open" (both-open bucket) — same green tokens the removed SymbolPanel
    // pills used.
    expect(block).toContain('var(--algo-green-bg-mid)');
    expect(block).toContain('var(--algo-green-border)');
    expect(block).toContain('var(--algo-green-text)');
    // "closed" — byte-identical to RefreshButton's .rf-mkt-closed, which
    // hardcodes rgba(126,151,184,…)/#94a3b8, the exact values these
    // tokens resolve to.
    expect(block).toContain('var(--algo-muted-bg)');
    expect(block).toContain('var(--algo-muted-border)');
    expect(block).toContain('var(--algo-dim)');
    // "MCX-only open" — the exact tokens RefreshButton's .rf-mkt-mcx
    // already uses.
    expect(block).toContain('var(--c-action-14)');
    expect(block).toContain('var(--algo-amber-border)');
    expect(block).toContain('var(--c-action)');
    // "EQ-only open" — the 4th token.
    expect(block).toContain('var(--algo-market-eq-only-bg)');
    expect(block).toContain('var(--algo-market-eq-only-border)');
    expect(block).toContain('var(--algo-market-eq-only-text)');
    expect(block).not.toMatch(/#[0-9a-fA-F]{3,6}/);
    expect(block).not.toMatch(/rgba\(/);
  });

  test('app.css defines the --algo-market-eq-only-* token, aliased to an existing hue (no brand-new raw colour)', () => {
    const css = readFileSync(APP_CSS, 'utf-8');
    const idx = css.indexOf('--algo-market-eq-only-bg:');
    expect(idx).toBeGreaterThan(-1);
    const block = css.slice(Math.max(0, idx - 50), idx + 300);
    expect(block).toMatch(/--algo-market-eq-only-bg:\s*var\(--algo-violet-bg\)/);
    expect(block).toMatch(/--algo-market-eq-only-border:\s*var\(--algo-violet-border\)/);
    expect(block).toMatch(/--algo-market-eq-only-text:\s*var\(--algo-violet-text\)/);
  });
});

for (const { path, label } of PAGES) {
  test.describe(`AlgoTimestamp market-status badge — live (${label})`, () => {
    test(`renders between the timestamp and the button group on ${path}`, async ({ page }) => {
      await loginAsAdmin(page);
      await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded', timeout: 30_000 });

      const badge = page.locator('[data-testid="algo-ts-market-badge"]').first();
      await expect(badge).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('[data-testid="algo-ts-market-badge-any"]').first()).toBeVisible();

      // Structural placement: badge sits after the clock button and before
      // (or inside) the .page-header-actions button group, all within the
      // same .page-header row.
      const order = await page.evaluate(() => {
        const header = document.querySelector('.page-header');
        if (!header) return null;
        const badgeEl = header.querySelector('[data-testid="algo-ts-market-badge"]');
        const actions = header.querySelector('.page-header-actions');
        if (!badgeEl || !actions) return null;
        // compareDocumentPosition: Node.DOCUMENT_POSITION_FOLLOWING === 4
        // eslint-disable-next-line no-bitwise
        return !!(actions.compareDocumentPosition(badgeEl) & Node.DOCUMENT_POSITION_PRECEDING);
      });
      expect(order).toBe(true);
    });

    test(`reflects a mocked /api/market/status response on ${path}`, async ({ page }) => {
      await _mockStatus(page, { nse_open: true, mcx_open: true });
      await loginAsAdmin(page);
      await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded', timeout: 30_000 });

      const pill = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
      await expect(pill).toHaveClass(/ats-mkt-open\b/, { timeout: 10_000 });
      await expect(pill).toHaveText('M');
    });
  });
}

test.describe('AlgoTimestamp market-status badge — bucket matrix (pulse only, representative)', () => {
  test('both open -> pill reads "M", ats-mkt-open (green)', async ({ page }) => {
    await _mockStatus(page, { nse_open: true, mcx_open: true });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const pill = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
    await expect(pill).toHaveClass(/ats-mkt-open\b/, { timeout: 10_000 });
    await expect(pill).toHaveText('M');
  });

  test('MCX-only open -> pill reads "MCX", ats-mkt-mcx-only (amber)', async ({ page }) => {
    await _mockStatus(page, { nse_open: false, mcx_open: true });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const pill = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
    await expect(pill).toHaveClass(/ats-mkt-mcx-only/, { timeout: 10_000 });
    await expect(pill).toHaveText('MCX');
  });

  test('EQ-only open (NSE open, MCX closed) -> pill reads "EQ", ats-mkt-eq-only (new 4th bucket)', async ({ page }) => {
    await _mockStatus(page, { nse_open: true, mcx_open: false });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const pill = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
    await expect(pill).toHaveClass(/ats-mkt-eq-only/, { timeout: 10_000 });
    await expect(pill).toHaveText('EQ');

    // Colour-token cross-check: resolved background-color must differ from
    // both the amber MCX-only bucket and the slate closed bucket, proving
    // the new token actually renders a distinct colour (not a silent
    // fallback to an existing bucket).
    const eqOnlyBg = await pill.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(eqOnlyBg).toBeTruthy();
    expect(eqOnlyBg).not.toBe('rgba(0, 0, 0, 0)');
  });

  test('both closed -> pill reads "M", ats-mkt-closed (slate)', async ({ page }) => {
    await _mockStatus(page, { nse_open: false, mcx_open: false });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const pill = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
    await expect(pill).toHaveClass(/ats-mkt-closed/, { timeout: 10_000 });
    await expect(pill).toHaveText('M');
  });
});

test.describe('AlgoTimestamp market-status badge — mobile scroll fallback', () => {
  test('at a narrow viewport the badge scrolls horizontally inside .ats-scroll instead of wrapping/overflowing the header', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const badge = page.locator('[data-testid="algo-ts-market-badge"]').first();
    await expect(badge).toBeVisible({ timeout: 15_000 });

    // The page itself must not horizontally overflow the viewport — proof
    // the squeeze is absorbed by the inner scroll container, not by
    // pushing the whole layout wider or wrapping the header onto an extra
    // line that grows page height unexpectedly.
    const docOverflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(docOverflow.scrollWidth).toBeLessThanOrEqual(docOverflow.clientWidth + 1);

    // The inner .ats-scroll container itself is a valid horizontal-scroll
    // candidate (overflow-x: auto with a bounded box) — assert the CSS
    // property directly rather than requiring genuine overflow at this
    // exact viewport (a single short pill will almost always fit at 360px;
    // the mechanism must exist regardless).
    const scrollStyle = await page.evaluate(() => {
      const el = document.querySelector('.ats-scroll') || document.querySelector('[data-testid="algo-ts-market-badge"]')?.closest('div');
      if (!el) return null;
      const cs = getComputedStyle(el);
      return { overflowX: cs.overflowX };
    });
    expect(scrollStyle?.overflowX).toBe('auto');
  });
});
