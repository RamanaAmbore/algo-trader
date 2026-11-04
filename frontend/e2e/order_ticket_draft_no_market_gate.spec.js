/**
 * order_ticket_draft_no_market_gate.spec.js
 *
 * 2026-11-04 — operator: "when you place a draft order, it does not need
 * market to be open. It just needs to set entry price as ltp. submit
 * button to be active when draft is set on in order ticket and chain."
 *
 * Root cause (three independent gates, all unaware of Draft mode):
 *
 * 1. OrderTicket.svelte's `validationErr` always ran
 *    `_validatePriceTrigger()`, which returns "Limit price required"
 *    whenever `_price` is empty. `_price` is only ever populated from a
 *    live depth quote, a caller-supplied prop, or a manual type — all of
 *    which routinely never arrive while market is closed. SymbolPanel's
 *    `_modalFireSubmit()` reads this error (via `onValidationChange` ->
 *    `_ticketValidationErr`) and refuses to even increment the submit
 *    trigger, showing a toast instead — so Submit silently never fired
 *    for a draft with no live quote, which is the routine case when
 *    market is closed. Fixed: skipped entirely when `draftMode` is true
 *    (a draft never places a broker order, so no limit/trigger price is
 *    ever required).
 * 2. OrderTicket.svelte's draft-submit branch computed `avg_cost` from
 *    `_price != null ? Number(_price) : null` as its fallback when no
 *    live tick exists yet. Once (1) above stops blocking Submit, `_price`
 *    routinely IS the empty string, and `Number('')` is 0 — silently
 *    recording a ₹0 cost basis instead of a genuinely unresolved one.
 *    Fixed: `_price !== '' && _price != null`.
 * 3. OptionChainTab.svelte's 8 add-leg buttons (+CE/-CE/+PE/-PE, each
 *    duplicated across the ATM/non-ATM row markup) were
 *    `disabled={!(bid>0||ask>0)}` unconditionally — so with market
 *    closed (no live bid/ask), a leg could never even be STAGED on the
 *    Chain tab, draft or not. `addOptionToBasket()` itself never needed
 *    the quote (it resolves the symbol via the local instruments cache),
 *    and the draft-pricing path already prices from `liveSnap()?.ltp`.
 *    Fixed: `disabled={!draftMode && !(bid>0||ask>0)}`.
 *
 * Also fixed: SymbolPanel.svelte's `_modalNotice` ("Market/MCX closed —
 * order will queue... LIVE orders are deferred") is actively misleading
 * for a draft — a draft never queues or defers anything. Suppressed
 * entirely while `_sharedDraftMode` is true.
 *
 * Source-grep regression guards only — same precedent as
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js's own header
 * comment: live automation of "market genuinely closed, no live quote"
 * is impractical to drive deterministically in CI, so these pin the
 * exact conditional text instead.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const ORDER_TICKET_PATH   = path.resolve(DIR, '../src/lib/order/OrderTicket.svelte');
const SYMBOL_PANEL_PATH   = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');
const OPTION_CHAIN_PATH   = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');

test.describe('OrderTicket — price/trigger validation never blocks Submit while Draft is on', () => {
  test('validationErr skips _validatePriceTrigger when draftMode is true', () => {
    const src = readFileSync(ORDER_TICKET_PATH, 'utf8');
    const block = src.match(/const validationErr = \$derived\.by\(\(\) =>[\s\S]*?\n  \);/)?.[0] ?? '';
    expect(block).toMatch(
      /\(draftMode \? null : _validatePriceTrigger\(showLimit, showTrigger, Number\(_price\), Number\(_trigger\), _tickSize, _tickDecimals\)\)/,
    );
  });

  test('a bare unconditional _validatePriceTrigger call (the pre-fix shape) is gone', () => {
    const src = readFileSync(ORDER_TICKET_PATH, 'utf8');
    const block = src.match(/const validationErr = \$derived\.by\(\(\) =>[\s\S]*?\n  \);/)?.[0] ?? '';
    expect(block).not.toMatch(/\n\s*_validatePriceTrigger\(showLimit/);
  });
});

test.describe('OrderTicket — draft entry avg_cost never silently coerces a blank price field to ₹0', () => {
  test('the no-live-tick fallback checks for the empty string explicitly, not just != null', () => {
    const src = readFileSync(ORDER_TICKET_PATH, 'utf8');
    expect(src).toMatch(/: \(_price !== '' && _price != null \? Number\(_price\) : null\),/);
    expect(src).not.toMatch(/: \(_price != null \? Number\(_price\) : null\),/);
  });
});

test.describe('SymbolPanel — "market closed / order will queue" notice is suppressed for drafts', () => {
  test('_modalNotice returns null early when _sharedDraftMode is true, before resolving the exchange', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const block = src.match(/const _modalNotice = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(block).toMatch(/if \(_sharedDraftMode\) return null;/);
    // Must come BEFORE the exchange-resolution try block, or a draft
    // ticket with a resolved exchange would still render the notice.
    const draftGateIdx = block.indexOf('if (_sharedDraftMode) return null;');
    const exchResolveIdx = block.indexOf('const exch = _mktBadgeExchShell;');
    expect(draftGateIdx).toBeGreaterThan(-1);
    expect(exchResolveIdx).toBeGreaterThan(draftGateIdx);
  });
});

test.describe('OptionChainTab — add-leg buttons never require a live quote while Draft is on', () => {
  test('all 8 +/- add-leg buttons (CE/PE buy/sell, ATM + non-ATM rows) gate disabled on draftMode', () => {
    const src = readFileSync(OPTION_CHAIN_PATH, 'utf8');
    const ceDisabled = [...src.matchAll(/disabled=\{!draftMode && !\(ceQ\?\.bid > 0 \|\| ceQ\?\.ask > 0\)\}/g)];
    const peDisabled = [...src.matchAll(/disabled=\{!draftMode && !\(peQ\?\.bid > 0 \|\| peQ\?\.ask > 0\)\}/g)];
    expect(ceDisabled.length).toBe(4); // buy+sell x ATM+non-ATM
    expect(peDisabled.length).toBe(4);
    // No remaining button still uses the unconditional pre-fix form.
    expect(src).not.toMatch(/disabled=\{!\(ceQ\?\.bid > 0 \|\| ceQ\?\.ask > 0\)\}/);
    expect(src).not.toMatch(/disabled=\{!\(peQ\?\.bid > 0 \|\| peQ\?\.ask > 0\)\}/);
  });

  test('button titles reflect draftMode too, so a clickable draft button never says "No quote — price unknown"', () => {
    const src = readFileSync(OPTION_CHAIN_PATH, 'utf8');
    const ceTitles = [...src.matchAll(/title=\{\(draftMode \|\| ceQ\?\.bid > 0 \|\| ceQ\?\.ask > 0\)/g)];
    const peTitles = [...src.matchAll(/title=\{\(draftMode \|\| peQ\?\.bid > 0 \|\| peQ\?\.ask > 0\)/g)];
    expect(ceTitles.length).toBe(4);
    expect(peTitles.length).toBe(4);
  });

  test('addOptionToBasket itself resolves the symbol from the instruments cache, independent of the live quote', () => {
    const src = readFileSync(OPTION_CHAIN_PATH, 'utf8');
    const fn = src.match(/function addOptionToBasket\([\s\S]*?\n  \}\n/)?.[0] ?? '';
    expect(fn).toMatch(/const inst = findOption\(/);
  });
});
