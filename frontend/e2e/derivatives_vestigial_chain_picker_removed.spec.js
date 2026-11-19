/**
 * derivatives_vestigial_chain_picker_removed.spec.js
 *
 * Stale-code guard, source-grep only (no live render needed) — confirms the
 * REST of the vestigial "Option-chain picker (Strategy mode)" quick-add
 * block stays removed from /admin/derivatives's `+page.svelte`.
 *
 * `derivatives_chain_picker_panel_removed.spec.js` (first pass, 2026-11)
 * removed `showAddPanel` and everything gated on it, but explicitly left
 * four items as out-of-scope — each a separate, already-dead local
 * function or derived with zero callers ANYWHERE in `frontend/src/`
 * (confirmed via a fresh whole-tree grep, not just this file — this
 * matters because `OptionChainTab.svelte` has its own LIVE, same-named
 * `addOptionToBasket()`/`addFuturesToBasket()`/`chainFutures` — a
 * different function/derived in a different file):
 *
 *  - `addOptionToBasket()` (named, confirmed dead)
 *  - `addOptionToWatchlist()` (named, confirmed dead)
 *  - `addFutureDraft()` (named, confirmed dead)
 *  - `chainFutures` (named, confirmed dead)
 *
 * Removing those four revealed a larger contiguous dead region feeding
 * ONLY them, confirmed dead via the same exhaustive whole-tree grep
 * rigor, also removed in this same follow-up sweep:
 *
 *  - `addFuturesToBasket()` — sibling of `addOptionToBasket()`, zero
 *    callers (its OptionChainTab.svelte same-named twin is untouched).
 *  - `setBasketChaseAgg()` / `basketStepLots()` / `removeFromBasket()` /
 *    `clearBasket()` — zero callers; no markup ever rendered a basket
 *    bar on this page to wire them to.
 *  - `chainQuotesMap` — its one reader was `addOptionToBasket()`.
 *  - `chainExpiry` / `chainExpiries` (+ the chain-expiry auto-pick
 *    `$effect`) — fed only `chainFutures`, `addOptionToBasket()`,
 *    `addOptionToWatchlist()`, and their own auto-pick effect.
 *  - `chainKinds` / `chainLots` — zero references beyond their own
 *    declaration anywhere in the file.
 *  - `chainSide` — its only use beyond its own declaration was the
 *    default-param value inside `addFutureDraft()`.
 *  - `quickToast` / `_flashToast()` — fed only by `addOptionToBasket()`
 *    and `addFuturesToBasket()`.
 *  - `watchToast` — used only inside `addOptionToWatchlist()`.
 *
 * Deliberately NOT removed — each has a genuine live consumer, verified
 * before touching anything:
 *
 *  - `chainUnderlying` + `underlyingChoices` + `_staticUnderlyingChoices`
 *    + the chain-underlying auto-pick `$effect` — `onTicketSubmit`'s
 *    draft branch reads `chainUnderlying` as a `selectedUnderlying`
 *    fallback when a draft order is added with no underlying picked yet.
 *  - `_mergeIntoBasket()` / `_quickKeyOpt()` / `_quickKeyFut()` /
 *    `chainBasket` / `basketError` — all live via the real `onAddToBasket`
 *    callback wired into `<SymbolPanel>` further down this file (the
 *    staged-leg array itself is never rendered or submitted anywhere on
 *    this page today — a separate, pre-existing latent gap, not
 *    introduced or fixed by this sweep).
 *  - `placeBasket()` / `basketPlacing` / `basketProgress` /
 *    `basketJustDone` — `placeBasket()` has zero callers on this page
 *    today, but its exact body is pinned by the CRITICAL money-safety
 *    regression guard in `basket_mode_sim_replay_safety.spec.js`
 *    ("derivatives +page.svelte — standalone basket path reads the real
 *    navbar mode"). That guard is out of scope for this sweep.
 *
 * The real, still-used chain-opening path (row click → SymbolPanel →
 * `OptionChainTab.svelte`'s Chain tab) is unaffected, same reasoning as
 * `derivatives_chain_picker_panel_removed.spec.js`.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const PAGE_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

test.describe('Stale-code: vestigial chain-picker (Strategy mode) quick-add block stays removed', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(PAGE_PATH, 'utf8');
  });

  test('addOptionToBasket() is gone (named)', () => {
    expect(src).not.toMatch(/function addOptionToBasket\(/);
  });

  test('addOptionToWatchlist() is gone (named)', () => {
    expect(src).not.toMatch(/async function addOptionToWatchlist\(/);
  });

  test('addFutureDraft() is gone (named)', () => {
    expect(src).not.toMatch(/function addFutureDraft\(/);
  });

  test('chainFutures derived is gone (named)', () => {
    expect(src).not.toMatch(/const chainFutures = \$derived\.by/);
  });

  test('addFuturesToBasket() is gone — sibling of addOptionToBasket(), zero callers', () => {
    expect(src).not.toMatch(/function addFuturesToBasket\(/);
  });

  test('setBasketChaseAgg() and basketStepLots() are gone — no basket bar ever rendered them', () => {
    expect(src).not.toMatch(/function setBasketChaseAgg\(/);
    expect(src).not.toMatch(/function basketStepLots\(/);
  });

  test('removeFromBasket() and clearBasket() are gone — zero callers', () => {
    expect(src).not.toMatch(/function removeFromBasket\(/);
    expect(src).not.toMatch(/function clearBasket\(\)/);
  });

  test('chainQuotesMap is gone — its one reader (addOptionToBasket) is gone too', () => {
    expect(src).not.toContain('let chainQuotesMap = $state(null);');
  });

  test('chainExpiry / chainExpiries and their auto-pick effect are gone', () => {
    expect(src).not.toMatch(/let chainExpiry\s*=\s*\$state/);
    expect(src).not.toMatch(/const chainExpiries = \$derived\.by/);
    expect(src).not.toMatch(/\/\/ Auto-pick first expiry when chain underlying changes\./);
  });

  test('chainKinds and chainLots are gone — zero references beyond their own declaration', () => {
    expect(src).not.toMatch(/let chainKinds\s*=\s*\$state/);
    expect(src).not.toMatch(/let chainLots\s*=\s*\$state/);
  });

  test('chainSide is gone — its only other use was addFutureDraft\'s default param', () => {
    expect(src).not.toMatch(/let chainSide\s*=\s*\$state/);
  });

  test('quickToast and _flashToast() are gone — fed only the two removed quick-add functions', () => {
    expect(src).not.toMatch(/let quickToast\s*=\s*\$state/);
    expect(src).not.toMatch(/function _flashToast\(/);
  });

  test('watchToast is gone — used only inside addOptionToWatchlist()', () => {
    expect(src).not.toMatch(/let watchToast\s*=\s*\$state/);
  });

  // ── Positive guards — these MUST survive. A future sweep over-removing
  // any of these would silently break a live, wired-up feature. ──────────

  test('chainUnderlying survives — onTicketSubmit\'s draft branch is a live consumer', () => {
    expect(src).toMatch(/let chainUnderlying\s*=\s*\$state/);
    expect(src).toMatch(/selectedUnderlying = chainUnderlying\.toUpperCase\(\)/);
  });

  test('_mergeIntoBasket / _quickKeyOpt / _quickKeyFut / chainBasket survive — live via onAddToBasket', () => {
    expect(src).toMatch(/function _mergeIntoBasket\(/);
    expect(src).toMatch(/function _quickKeyOpt\(/);
    expect(src).toMatch(/function _quickKeyFut\(/);
    expect(src).toMatch(/let chainBasket\s*=\s*\$state/);
  });

  test('onAddToBasket prop wiring into <SymbolPanel> survives', () => {
    expect(src).toContain('onAddToBasket={');
  });

  test('placeBasket() and its three state vars survive — pinned by the money-safety guard spec', () => {
    expect(src).toMatch(/async function placeBasket\(\)/);
    expect(src).toMatch(/let basketPlacing\s*=\s*\$state/);
    expect(src).toMatch(/let basketProgress\s*=\s*\$state/);
    expect(src).toMatch(/let basketJustDone\s*=\s*\$state/);
  });

  test('the now-unused imports were dropped, not just the call sites', () => {
    // findOption / listExpiries / listFutures from $lib/data/instruments —
    // all three had zero readers left once the named-4 + fallout were gone.
    expect(src).not.toMatch(/\bfindOption\b/);
    expect(src).not.toMatch(/\blistExpiries\b/);
    expect(src).not.toMatch(/\blistFutures\b/);
    // getInstrument / getOptionUnderlyingLot / findNearestFuture / hasFNO /
    // instrumentsCacheVersion all have other live readers and must survive.
    const instrumentsImport = src.match(/import\s*\{[^}]*\}\s*from\s*'\$lib\/data\/instruments'/)?.[0] ?? '';
    expect(instrumentsImport).toContain('getInstrument');
    expect(instrumentsImport).toContain('getOptionUnderlyingLot');
    expect(instrumentsImport).toContain('findNearestFuture');
    expect(instrumentsImport).toContain('hasFNO');
    expect(instrumentsImport).toContain('instrumentsCacheVersion');
  });
});
