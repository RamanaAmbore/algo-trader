/**
 * derivatives_chain_picker_panel_removed.spec.js
 *
 * Stale-code guard, source-grep only (no live render needed) — confirms the
 * dead inline chain-picker panel wiring stays removed from
 * /admin/derivatives's `+page.svelte`.
 *
 * Commit 0e31eee0 (2026-06-18) removed the dedicated "Toggle chain picker"
 * button and its `.opt-add-btn*` CSS, but left `showAddPanel` ($state(false))
 * and everything gated on it behind: it was never set to `true` anywhere in
 * the file (no template consumer existed), so the spot-fetch effect, the
 * chain-quotes poll effect, the ATM-scroll effect, and `addChainDraft()`
 * (whose only remaining caller was the removed button) were all dead code
 * running real (if always-no-op) broker round-trips and timers on every page
 * load. Removed in a 2026-11 dead-code sweep:
 *
 *  - `showAddPanel` itself (the $state declaration).
 *  - `addChainDraft()` — confirmed zero callers anywhere in `frontend/src/`.
 *  - The three `showAddPanel`-gated `$effect`s (spot-fetch, chain-quotes
 *    poll, ATM-scroll) and everything that existed solely to feed them
 *    (`chainSpotFetched`, `chainSpotKey`, `chainQuotesKey`, `chainQuotesPoll`,
 *    `_refreshChainQuotes()`, its `onDestroy` cleanup, `chainStrikes`,
 *    `chainSpot`, `chainAtmStrike`, `chainAtmRowEl`, `chainAtmRow`).
 *  - The now-unused imports: `fetchOptionsSpot`, `fetchChainQuotes` (from
 *    `$lib/api`), `listStrikes` (from `$lib/data/instruments`), `isMarketOpen`
 *    (from `$lib/marketHours`).
 *
 * Deliberately NOT removed (still has an out-of-scope live or dead-but-
 * untouched consumer — see the task's own handback report for the full
 * second-pass list): `chainUnderlying`/`chainExpiry` (live via
 * `onTicketSubmit`'s draft branch), `chainQuotesMap` (read by the separate,
 * still-present `addOptionToBasket()`), `_mergeIntoBasket`/`_quickKeyOpt`/
 * `_quickKeyFut`/`chainBasket` (live via SymbolPanel's `onAddToBasket`).
 *
 * The real, still-used chain-opening path (row click → SymbolPanel →
 * `OptionChainTab.svelte`'s Chain tab) is covered by
 * `derivatives_chain_picker.spec.js`, `basket_pills.spec.js` and
 * `chain_grid_and_basket_step_styling.spec.js` — unaffected by this removal,
 * since none of it ever rendered through the dead panel.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const PAGE_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

test.describe('Stale-code: dead showAddPanel chain-picker panel wiring stays removed', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(PAGE_PATH, 'utf8');
  });

  test('showAddPanel $state declaration is gone', () => {
    expect(src).not.toContain('showAddPanel = $state(');
  });

  test('addChainDraft() is gone — its only caller was the already-removed toggle button', () => {
    expect(src).not.toContain('function addChainDraft(');
  });

  test('fetchChainQuotes() call (chain-quotes poll effect) is gone', () => {
    expect(src).not.toContain('fetchChainQuotes(');
  });

  test('fetchOptionsSpot() call (spot-fetch effect) is gone', () => {
    expect(src).not.toContain('fetchOptionsSpot(');
  });

  test('listStrikes() call (dead chainStrikes derived) is gone', () => {
    expect(src).not.toContain('listStrikes(');
  });

  test('isMarketOpen() call (chain-quotes poll market-hours gate) is gone', () => {
    expect(src).not.toContain('isMarketOpen(');
  });

  test('the now-unused imports were dropped, not just the call sites', () => {
    // fetchOptionsSpot / fetchChainQuotes from $lib/api
    expect(src).not.toMatch(/import\s*\{[^}]*\bfetchOptionsSpot\b[^}]*\}\s*from\s*'\$lib\/api'/);
    expect(src).not.toMatch(/import\s*\{[^}]*\bfetchChainQuotes\b[^}]*\}\s*from\s*'\$lib\/api'/);
    // listStrikes from $lib/data/instruments
    expect(src).not.toMatch(/import\s*\{[^}]*\blistStrikes\b[^}]*\}\s*from\s*'\$lib\/data\/instruments'/);
    // isMarketOpen from $lib/marketHours — isNseOpen/isMcxOpen on the same
    // line must survive; only isMarketOpen itself must be gone.
    const marketHoursImport = src.match(/import\s*\{[^}]*\}\s*from\s*'\$lib\/marketHours'/)?.[0] ?? '';
    expect(marketHoursImport).not.toContain('isMarketOpen');
    expect(marketHoursImport).toContain('isNseOpen');
    expect(marketHoursImport).toContain('isMcxOpen');
  });

  test('the ATM-scroll apparatus fed only by the dead panel is gone', () => {
    expect(src).not.toMatch(/\bchainAtmRowEl\b/);
    expect(src).not.toMatch(/\bchainAtmStrike\b/);
    expect(src).not.toMatch(/const chainAtmRow = \(node\)/);
    expect(src).not.toMatch(/const chainStrikes = \$derived\.by/);
    expect(src).not.toMatch(/const chainSpot = \$derived\.by/);
  });

  test('chainQuotesMap declaration survives — addOptionToBasket() still reads it', () => {
    // Not part of this removal: addOptionToBasket() (a separate, already-
    // dead-but-out-of-scope local function) is the one remaining reader.
    expect(src).toContain('let chainQuotesMap = $state(null);');
  });

  test('chainUnderlying survives — onTicketSubmit\'s draft branch is a live consumer', () => {
    expect(src).toMatch(/selectedUnderlying = chainUnderlying\.toUpperCase\(\)/);
  });
});
