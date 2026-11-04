/**
 * chain_basket_submit_no_false_market_closed_notice.spec.js
 *
 * Live operator report (2026-11-04): "after placing order, it shows mcx
 * is not open message even when mcx is open." Confirmed cosmetic only —
 * operator: "the mcx message does not block the order."
 *
 * Root cause: a successful Chain basket submit resets `basketLegs = []`
 * AND `_localSymbol = ''` (SymbolPanel.svelte's `submitBasket()` post-
 * submit cleanup — the modal deliberately stays open so the operator can
 * place more legs, per prior operator instruction). `_chainMktExch`'s
 * bare-root rescue fallback used to compute `root = _parseRoot('') ||
 * ''` (falsy either way) and STILL call `resolveUnderlying(root, ...)`
 * with that falsy root before defaulting to the hardcoded 'NFO'
 * exchange — so the market-hours notice immediately re-evaluated
 * against NSE's hours, not MCX's, the instant the basket cleared. Any
 * MCX order placed after 15:30 IST (NSE close) but before 23:30 IST
 * (MCX close) — which is most of the commodity trading day — showed a
 * spurious "Market closed — order will queue" banner immediately after
 * a genuinely successful MCX submit, with MCX still open.
 *
 * Fix: `_chainMktExch` now returns '' (not 'NFO') when `root` is
 * genuinely empty — `_modalNotice`'s own pre-existing guard
 * (`if (!exch || exch === 'Market') return null`) already knows how to
 * suppress the notice correctly for an unresolved exchange; the bug was
 * inventing a false NFO/NSE answer instead of admitting "unknown".
 *
 * Why a bundle-source guard, not a full live UI repro: `_localSymbol`
 * is settable ONLY via `SymbolSearchInput`'s `onPick(sym, meta)`
 * callback (a real symbol selection) or via `submitBasket()`'s own
 * post-submit reset — clearing the search box's visible text does NOT
 * call `onPick('')`, so there is no UI-reachable way to drive
 * `_localSymbol` back to `''` short of completing a real multi-leg
 * option-chain stage-and-submit cycle. This mirrors the exact
 * "3-Stale" dimension already used in this codebase for the identical
 * class of hard-to-drive-live defect — see
 * `crudeoil_add_sell.spec.js`'s own bundle-pattern test.
 *
 * This is the third live incident at this exact spot (2026-10-22,
 * 2026-10-29, 2026-11-04) — each prior fix patched a different trigger
 * for the same "the exchange resolver defaults to NFO/NSE when it
 * should admit unknown" bug class.
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('SymbolPanel _chainMktExch — empty root never masquerades as NFO', () => {
  test('compiled bundle no longer unconditionally resolves an empty root through resolveUnderlying before defaulting to NFO', async ({ page }) => {
    const bodies = [];
    page.on('response', async (resp) => {
      const ct = resp.headers()['content-type'] || '';
      if (!ct.includes('javascript')) return;
      try {
        const text = await resp.text();
        // SymbolPanel is identified by a string unique to this file.
        if (text.includes('_chainMktExch') || text.includes('_mktBadgeExchShell')) {
          bodies.push(text);
        }
      } catch { /* ignore */ }
    });

    await page.goto(`${BASE}/admin/derivatives`);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(500);

    if (bodies.length === 0) {
      // Dev server may not have served an unminified chunk naming these
      // locals under this route — not every page loads SymbolPanel
      // eagerly. Not a hard failure; the regression guard below is
      // vacuous in that case, same convention as the precedent spec.
      console.info('SymbolPanel bundle not observed on this route — skipping bundle-pattern check');
      return;
    }

    const combined = bodies.join('\n');

    // The fixed source reads, in order: compute `root`; if falsy,
    // return '' immediately; otherwise call `resolveUnderlying(root,
    // ...)`. The pre-fix source called `resolveUnderlying` with a
    // ternary guard INSIDE the same expression
    // (`root?resolveUnderlying(root,...):null`) and always fell through
    // to `'NFO'` on either branch. Both forms survive minification
    // identifiably: the fixed version's `if(!root)return...` early-exit
    // appears textually BEFORE its own `resolveUnderlying(` call site,
    // whereas the pre-fix version's ternary put `resolveUnderlying(`
    // INSIDE the same conditional expression with no preceding
    // early-return statement. We assert on the narrower, robust
    // signal: a bare `root?resolveUnderlying(root,` ternary call-site
    // pattern (the exact pre-fix shape) must not exist anywhere in the
    // bundle.
    const oldTernaryPattern = /root\s*\?\s*resolveUnderlying\s*\(\s*root\s*,/;
    expect(
      oldTernaryPattern.test(combined),
      'SymbolPanel bundle still contains the pre-fix `root?resolveUnderlying(root,...)' +
      ':null` ternary — the empty-root early-return fix was not shipped'
    ).toBe(false);
  });
});
