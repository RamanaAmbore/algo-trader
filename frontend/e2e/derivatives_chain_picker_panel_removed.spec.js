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
 * Deliberately NOT removed (still has a live consumer — see the task's own
 * handback report for the full second-pass list): `_mergeIntoBasket`/
 * `_quickKeyOpt`/`_quickKeyFut`/`chainBasket`/`basketError` (live via
 * SymbolPanel's `onAddToBasket`), `placeBasket`/`basketPlacing`/
 * `basketProgress`/`basketJustDone` (pinned by the CRITICAL money-safety
 * regression guard in basket_mode_sim_replay_safety.spec.js even though
 * nothing calls `placeBasket()` on this page today).
 *
 * `chainUnderlying` ITSELF was removed in a later follow-up audit (not this
 * sweep): its own effect just mirrored `underlyingChoices[0]` one tick
 * later, so `onTicketSubmit`'s draft-branch fallback (its one real
 * consumer, confirmed live above) now reads `underlyingChoices[0]`
 * directly instead — same value, no separate `$state`/`$effect` pair to
 * keep in sync. See the "chainUnderlying is gone" test below.
 *
 * A SECOND dead-code sweep (2026-11, follow-up) removed the rest of the
 * vestigial "Option-chain picker (Strategy mode)" quick-add block that this
 * first pass had left behind as out-of-scope: `addOptionToBasket()`,
 * `addOptionToWatchlist()`, `addFutureDraft()`, `chainFutures` (named), plus
 * `addFuturesToBasket()`, `setBasketChaseAgg()`, `basketStepLots()`,
 * `removeFromBasket()`, `clearBasket()`, `chainQuotesMap`, `chainExpiry`,
 * `chainExpiries`, `chainKinds`, `chainSide`, `chainLots`, `quickToast`,
 * `_flashToast()`, `watchToast` (all zero-caller once the named four were
 * gone). See `derivatives_vestigial_chain_picker_removed.spec.js`.
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

  test('chainQuotesMap is gone — its one reader, addOptionToBasket(), was removed in the follow-up sweep', () => {
    // Superseded: this file's own first pass left chainQuotesMap in place
    // because addOptionToBasket() still read it. The follow-up sweep
    // removed addOptionToBasket() too, so chainQuotesMap has zero readers
    // and is gone. See derivatives_vestigial_chain_picker_removed.spec.js.
    expect(src).not.toContain('let chainQuotesMap = $state(null);');
  });

  test('chainUnderlying is gone — onTicketSubmit\'s draft branch now reads underlyingChoices[0] directly', () => {
    // Superseded: this file's own earlier pass kept `chainUnderlying`
    // because onTicketSubmit's draft branch was a live consumer. A later
    // follow-up audit found its own effect just mirrored
    // `underlyingChoices[0]` one tick later, so the fallback now reads
    // that derived value directly — no separate $state/$effect pair.
    // Only the live declaration/usage forms are checked — a historical
    // comment explaining the removal may still name `chainUnderlying`.
    expect(src).not.toMatch(/let chainUnderlying\s*=\s*\$state/);
    expect(src).not.toMatch(/chainUnderlying\s*=\s*list\[0\]/);
    expect(src).not.toMatch(/chainUnderlying\.toUpperCase\(\)/);
    expect(src).not.toMatch(/if \(!selectedUnderlying && chainUnderlying\)/);
    expect(src).toMatch(/selectedUnderlying = underlyingChoices\[0\]\.toUpperCase\(\)/);
  });
});
