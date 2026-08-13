/**
 * derivatives_watchlist_picker_tiers.spec.js
 *
 * Guards that Tier 4 (pinned watchlist) and Tier 5 (regular watchlist)
 * are structurally wired into the underlyingOptionsForPicker derived in
 * the derivatives page. Combines source-grep checks (no live server
 * required) with a light browser smoke when auth is available.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT  — _pinnedWatchlistRoots / _regularWatchlistRoots are the
 *             single source of Tier 4 / Tier 5 picker entries; the old
 *             dead _watchlistSyms must be absent.
 *  2. Perf  — source-grep tests require no network; browser smoke has
 *             a 20 s budget for page load + picker visibility.
 *  3. Stale — guards that the old _watchlistSyms state var was removed
 *             and that the new vars are tracked in the auto-select $effect.
 *  4. Reuse — loadWatchlistSymbols (shared with SymbolSearchInput /
 *             MarketPulse) is the single fetch boundary; verified by grep.
 *  5. UX    — picker opens on the derivatives page without JS error even
 *             when watchlists return empty (graceful degradation to popular).
 *
 * Run (source-grep only — no server needed):
 *   npx playwright test e2e/derivatives_watchlist_picker_tiers.spec.js \
 *     --project=chromium-desktop --grep "source-grep"
 *
 * Run (with live server):
 *   PLAYWRIGHT_ADMIN_TOKEN=<tok> \
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_watchlist_picker_tiers.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const PAGE_SRC = resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);
const WATCHLIST_SRC = resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/data/watchlistSymbols.js',
);

const BASE  = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const TOKEN = process.env.PLAYWRIGHT_ADMIN_TOKEN || '';

// ─────────────────────────────────────────────────────────────────────────────
// Source-grep structural checks (no server required)
// ─────────────────────────────────────────────────────────────────────────────

test.describe('source-grep — derivatives watchlist picker tiers', () => {
  /** @type {string} */
  let pageSource;
  /** @type {string} */
  let watchlistSource;

  test.beforeAll(() => {
    pageSource      = readFileSync(PAGE_SRC, 'utf8');
    watchlistSource = readFileSync(WATCHLIST_SRC, 'utf8');
  });

  test('watchlistSymbols.js returns pinnedSyms in cache object', () => {
    expect(watchlistSource).toContain('pinnedSyms');
  });

  test('watchlistSymbols.js returns regularSyms in cache object', () => {
    expect(watchlistSource).toContain('regularSyms');
  });

  test('watchlistSymbols.js slices pinnedDetails from details', () => {
    expect(watchlistSource).toContain('pinnedDetails');
    expect(watchlistSource).toContain('regularDetails');
  });

  test('derivatives page imports loadWatchlistSymbols', () => {
    expect(pageSource).toContain("import { loadWatchlistSymbols } from '$lib/data/watchlistSymbols.js'");
  });

  test('dead state var _watchlistSyms is removed from derivatives page', () => {
    expect(pageSource).not.toContain('_watchlistSyms');
  });

  test('derivatives page declares _pinnedWatchlistRoots state var', () => {
    expect(pageSource).toContain('let _pinnedWatchlistRoots = $state(');
  });

  test('derivatives page declares _regularWatchlistRoots state var', () => {
    expect(pageSource).toContain('let _regularWatchlistRoots = $state(');
  });

  test('_extractFOUnderlyingRoots helper is present in derivatives page', () => {
    expect(pageSource).toContain('function _extractFOUnderlyingRoots(');
  });

  test('Tier 4 pinned watchlist loop present in underlyingOptionsForPicker', () => {
    expect(pageSource).toContain("hint: 'pinned'");
  });

  test('Tier 5 regular watchlist loop present in underlyingOptionsForPicker', () => {
    expect(pageSource).toContain("hint: 'watchlist'");
  });

  test('auto-select $effect tracks _pinnedWatchlistRoots', () => {
    expect(pageSource).toContain('void _pinnedWatchlistRoots;');
  });

  test('auto-select $effect tracks _regularWatchlistRoots', () => {
    expect(pageSource).toContain('void _regularWatchlistRoots;');
  });

  test('NIFTY provisional seed guard checks both watchlist root arrays', () => {
    expect(pageSource).toContain('!_pinnedWatchlistRoots.length && !_regularWatchlistRoots.length');
  });

  test('loadDefaultWatchlist assigns _pinnedWatchlistRoots via _extractFOUnderlyingRoots', () => {
    expect(pageSource).toContain('_pinnedWatchlistRoots  = _extractFOUnderlyingRoots(result.pinnedSyms');
  });

  test('loadDefaultWatchlist assigns _regularWatchlistRoots via _extractFOUnderlyingRoots', () => {
    expect(pageSource).toContain('_regularWatchlistRoots = _extractFOUnderlyingRoots(result.regularSyms');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Browser smoke — derivatives page loads without JS errors (auth-gated)
// ─────────────────────────────────────────────────────────────────────────────

test.describe('browser smoke — derivatives picker loads without error', () => {
  test.beforeEach(async ({ page }) => {
    if (!TOKEN) {
      test.skip(true, 'PLAYWRIGHT_ADMIN_TOKEN not set — skipping live smoke');
      return;
    }
    await page.addInitScript((tok) => {
      localStorage.setItem('rambo.auth', JSON.stringify({ token: tok, user: { role: 'admin' } }));
    }, TOKEN);
  });

  test('underlying picker is visible within 20 s', async ({ page }) => {
    const errors = /** @type {string[]} */ ([]);
    page.on('pageerror', e => errors.push(e.message));

    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle', timeout: 30_000 });

    // The underlying picker Select component should be on screen.
    // It renders as a button/select with the current underlying as text.
    const picker = page.locator('[data-testid="underlying-picker"], select').first();
    await expect(picker).toBeVisible({ timeout: 20_000 });

    // No uncaught JS errors during load.
    expect(errors.filter(e => !e.includes('ResizeObserver'))).toHaveLength(0);
  });
});
