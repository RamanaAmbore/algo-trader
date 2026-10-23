/**
 * context_menu_parity.spec.js — 2026-10-22 context-menu parity work.
 *
 * Operator: "make sure all the values available in all context menus" —
 * the four MarketPulse-only extras (Open in Options, Copy symbol,
 * Detach/Re-attach group, Add/Remove watchlist) are now wired on every
 * SymbolContextMenu host: MarketPulse, OrderBook, LogPanel,
 * PerformancePage, the derivatives Legs grid, and /orders.
 *
 * Two kinds of coverage:
 *  A. Real-browser checks against MarketPulse (/pulse) — the one host
 *     with existing login + grid fixtures. Right-clicks whatever row
 *     actually renders (dev DB data varies), so assertions are scoped to
 *     what's deterministic regardless of which symbols happen to be in
 *     the account: Copy symbol + Open in Options always render for a
 *     non-draft row; exactly one of Add/Remove-watchlist always renders
 *     once membership resolves. A second test specifically targets an
 *     option/future-looking row (symbol ending CE/PE/FUT) to also assert
 *     the Detach/Re-attach item — skipped gracefully if no such row
 *     renders within the wait (dev data may hold none).
 *  B. Source-level structural checks (same convention already used by
 *     null_guard_scheduler.spec.js's "reuse" test and
 *     card_chrome_radius_consistency.spec.js) confirming OrderBook,
 *     LogPanel, PerformancePage, the derivatives Legs grid, and /orders
 *     each wire the new props/handler — full real-browser coverage of
 *     all five non-Pulse hosts would need a dedicated mock per surface
 *     (none exists yet for live orders/positions data); the structural
 *     check catches the regression class that matters most here (a prop
 *     silently dropped, or `isSharedContextAction` not consulted).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

async function gotoPulse(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 30_000 });
}

const _root = resolve(new URL('.', import.meta.url).pathname, '..');
function _read(relPath) {
  return readFileSync(resolve(_root, relPath), 'utf-8');
}

// ─────────────────────────────────────────────────────────────────────────────
// A. Real-browser — MarketPulse
// ─────────────────────────────────────────────────────────────────────────────

test.describe('MarketPulse context menu — universal extras (real browser)', () => {
  test('any real row: Copy symbol + Open in Options always render; watchlist resolves to exactly one state', async ({ page }) => {
    await loginAsAdmin(page);
    await gotoPulse(page);

    // First non-TOTAL symbol cell to render, from any bucket.
    const cell = page.locator('.ag-cell[col-id="tradingsymbol"]').first();
    await cell.waitFor({ state: 'visible', timeout: 60_000 }).catch(() => {});
    test.skip(!(await cell.isVisible().catch(() => false)), 'No MarketPulse row rendered within the wait — dev data unavailable.');

    await cell.click({ button: 'right' });
    const menu = page.locator('.scm-menu');
    await expect(menu).toBeVisible({ timeout: 10_000 });

    await expect(menu.locator('[data-action="copy-symbol"]')).toBeVisible();
    await expect(menu.locator('[data-action="open-options"]')).toBeVisible();
    // Watchlist membership resolves synchronously from already-loaded
    // state on MarketPulse (unlike the generic fallback other hosts use),
    // so exactly one of the two watchlist items is present immediately.
    await expect(menu.locator('[data-action="add-watchlist"], [data-action="remove-watchlist"]')).toHaveCount(1);
  });

  test('an option/future row additionally shows Detach from group', async ({ page }) => {
    await loginAsAdmin(page);
    await gotoPulse(page);

    const foCell = page.locator('.ag-cell[col-id="tradingsymbol"]')
      .filter({ hasText: /(CE|PE|FUT)$/ })
      .first();
    const rendered = await foCell.waitFor({ state: 'visible', timeout: 30_000 }).then(() => true, () => false);
    test.skip(!rendered, 'No option/future row rendered within the wait — dev account holds no F&O position.');

    await foCell.click({ button: 'right' });
    const menu = page.locator('.scm-menu');
    await expect(menu).toBeVisible({ timeout: 10_000 });
    // Freshly-held F&O row is attached by default — Detach is the item
    // shown (Re-attach only appears once the operator has detached it,
    // a per-browser localStorage override this test doesn't assume).
    await expect(menu.locator('[data-action="detach"], [data-action="reattach"]')).toHaveCount(1);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// B. Source-level structural parity — the five non-MarketPulse-reference
//    hosts wire the new props and the shared dispatcher.
// ─────────────────────────────────────────────────────────────────────────────

const _HOSTS = [
  { label: 'OrderBook',               path: 'src/lib/OrderBook.svelte' },
  { label: 'LogPanel',                path: 'src/lib/LogPanel.svelte' },
  { label: 'PerformancePage',         path: 'src/lib/PerformancePage.svelte' },
  { label: 'derivatives Legs grid',   path: 'src/routes/(algo)/admin/derivatives/+page.svelte' },
  { label: '/orders page',            path: 'src/routes/(algo)/orders/+page.svelte' },
];

test.describe('context-menu parity — structural wiring (source-level)', () => {
  for (const { label, path } of _HOSTS) {
    test(`${label} wires showCopySymbol, showOpenInOptions, group, watchlist, and the shared dispatcher`, () => {
      const src = _read(path);
      expect(src, `${label}: missing showCopySymbol`).toMatch(/showCopySymbol/);
      expect(src, `${label}: missing showOpenInOptions`).toMatch(/showOpenInOptions/);
      expect(src, `${label}: missing group= prop`).toMatch(/group=\{/);
      expect(src, `${label}: missing watchlist= prop`).toMatch(/watchlist=\{/);
      expect(src, `${label}: does not consult isSharedContextAction`).toMatch(/isSharedContextAction\(/);
      expect(src, `${label}: does not call dispatchSharedContextAction`).toMatch(/dispatchSharedContextAction\(/);
    });
  }

  test('PerformancePage gates Open-in-Options and watchlist behind a designated/trader role check — it is the public page', () => {
    const src = _read('src/lib/PerformancePage.svelte');
    expect(src).toMatch(/_perfCtxOpsAllowed/);
    expect(src).toMatch(/showOpenInOptions=\{_perfCtxOpsAllowed\}/);
  });

  test('the derivatives Legs grid suppresses Open-in-Options by comparing the derived root to the selected underlying, not a hardcoded page check', () => {
    const src = _read('src/routes/(algo)/admin/derivatives/+page.svelte');
    expect(src).toMatch(/showOpenInOptions=\{rootForOptionsLink\([^)]*\)[^!]*!== String\(selectedUnderlying/);
  });

  test('MarketPulse keeps its own richer watchlist/detach handling rather than delegating to the generic shared dispatcher', () => {
    const src = _read('src/lib/MarketPulse.svelte');
    // MarketPulse's ctxAddWatch/ctxRemoveWatch/detachSymbol/reattachSymbol
    // stay wired to its own functions in the onAction switch, not to
    // dispatchSharedContextAction (which targets the operator's generic
    // default list, not whichever list is focused in MarketPulse's UI).
    expect(src).toMatch(/case 'add-watchlist':\s*ctxAddWatch\(row\); break;/);
    expect(src).toMatch(/case 'remove-watchlist':\s*ctxRemoveWatch\(row\); break;/);
    expect(src).toMatch(/case 'detach':\s*detachSymbol\(row\); break;/);
    expect(src).toMatch(/case 'reattach':\s*reattachSymbol\(row\); break;/);
  });

  test('pulseDetached.svelte.js is the single shared owner — MarketPulse no longer keeps a private detached-symbol $state array', () => {
    const src = _read('src/lib/MarketPulse.svelte');
    expect(src).not.toMatch(/let detachedSymbols\s*=\s*\$state/);
    expect(src).toMatch(/from '\$lib\/data\/pulseDetached\.svelte\.js'/);
  });
});
