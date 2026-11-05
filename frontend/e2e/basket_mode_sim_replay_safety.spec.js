/**
 * basket_mode_sim_replay_safety.spec.js
 *
 * CRITICAL money-safety audit finding (2026-11-05): a Chain basket
 * submitted while the navbar showed SIM or REPLAY placed real LIVE
 * broker orders on prod. `_resolveBasketMode()` (SymbolPanel.svelte)
 * passed sim/replay/shadow straight through unchanged, and the
 * resulting value was sent as a per-GROUP `mode` field that
 * `BasketGroup` never had — msgspec silently dropped it, and the
 * backend's `eff_mode` was computed entirely from server-side settings
 * with zero visibility into the navbar's real state.
 *
 * A second, independently-discovered instance of the identical bug
 * class existed in the derivatives page's own separate basket path
 * (not in the original audit's explicit finding list): its `placeBasket()`
 * never read `$executionMode` at all.
 *
 * Fixed:
 *  - SymbolPanel.svelte: `_resolveBasketMode` now normalises sim/replay/
 *    shadow to 'paper' first (matching OrderTicket's own
 *    `_resolveInitialMode`), and the resolved mode is threaded to
 *    `placeBasket(groups, basketMode2)` as a real top-level argument —
 *    the old dead per-group `mode: basketMode2` field is gone.
 *  - api.js: `placeBasket(groups, mode)` sends `mode` at the TOP LEVEL
 *    of the request body, matching the new `BasketOrderRequest.mode`
 *    field (backend/api/schemas.py).
 *  - consoleDispatch.js: the Console CLI's own basket-submit branch now
 *    threads its already-resolved `mode` through to `placeBasket` too
 *    (it used to silently drop it, unlike the sibling ticket branch).
 *  - derivatives/+page.svelte: its own standalone `placeBasket()` now
 *    reads `$executionMode` before ever attempting a live placement.
 *
 * Backend-side resolution (`_bk_resolve_effective_mode`, orders_basket.py)
 * and the full `basket_order_handler` end-to-end money-safety guard are
 * covered by backend/tests/test_basket_mode_sim_replay_safety_2026_11_05.py.
 * Source-grep regression guards only — same precedent as
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js: driving a
 * real SIM-mode Chain basket submit through a live broker-call
 * assertion is impractical to automate deterministically in CI.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');
const API_PATH          = path.resolve(DIR, '../src/lib/api.js');
const CONSOLE_DISPATCH_PATH = path.resolve(DIR, '../src/lib/command/consoleDispatch.js');
const DERIV_PAGE_PATH   = path.resolve(DIR, '../src/routes/(algo)/admin/derivatives/+page.svelte');

test.describe('SymbolPanel — Chain-tab basket mode resolution', () => {
  test('_resolveBasketMode normalises sim/replay/shadow to paper before anything else', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const fn = src.match(/async function _resolveBasketMode\([\s\S]*?\n  \}/)?.[0] ?? '';
    expect(fn).toContain("mode === 'sim' || mode === 'replay' || mode === 'shadow'");
    expect(fn).toContain("return 'paper';");
  });

  test('submitBasket threads the resolved mode into placeBasket as a real argument', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toContain('await placeBasket(groups, basketMode2)');
    // The old per-group field — BasketGroup has no `mode` field, so this
    // was always silently dropped by the backend.
    expect(src).not.toMatch(/mode:\s*basketMode2,\s*\n\s*legs:/);
  });
});

test.describe('api.js — placeBasket sends mode at the top level', () => {
  test('placeBasket builds { groups, mode } when a mode is given', () => {
    const src = readFileSync(API_PATH, 'utf8');
    const fn = src.match(/export async function placeBasket\([\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).toContain('mode ? { groups, mode } : { groups }');
  });
});

test.describe('consoleDispatch.js — Console CLI basket submit threads the resolved mode', () => {
  test('submitResolvedConsoleOrder no longer drops mode for the basket branch', () => {
    const src = readFileSync(CONSOLE_DISPATCH_PATH, 'utf8');
    expect(src).toContain('await placeBasket(resolved.basket.groups, mode)');
  });
});

test.describe('derivatives +page.svelte — standalone basket path reads the real navbar mode', () => {
  test('placeBasket only attempts live when $executionMode itself says live', () => {
    const src = readFileSync(DERIV_PAGE_PATH, 'utf8');
    const fn = src.match(/async function placeBasket\(\) \{[\s\S]*?\n  \}\n/)?.[0] ?? '';
    expect(fn).toContain('$executionMode');
    expect(fn).toContain("navbarMode === 'live'");
  });

  test('executionMode store is imported', () => {
    const src = readFileSync(DERIV_PAGE_PATH, 'utf8');
    expect(src).toMatch(/import\s*\{[^}]*\bexecutionMode\b[^}]*\}\s*from\s*'\$lib\/stores'/);
  });
});
