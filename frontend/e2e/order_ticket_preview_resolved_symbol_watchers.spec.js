/**
 * order_ticket_preview_resolved_symbol_watchers.spec.js
 *
 * 2026-10-20 fix (plan item 9.3) — the template-preview `$effect` in
 * `OrderTicket.svelte` (debounced call to `previewTicketTemplate` /
 * `POST /api/orders/ticket/preview`) sent `tradingsymbol: symbol` — the
 * bare ROOT symbol prop — instead of `_resolvedSymbol || symbol`, the
 * actual picked contract once the operator builds one via the
 * bare-underlying picker (Symbol Type=OPT/FUT → pick Expiry/Strike/
 * CE/PE → e.g. NIFTY → NIFTY26JUNC22000CE). The margin preview and the
 * real submit path both already use the `_resolvedSymbol || symbol`
 * fallback; this template-preview plan call was the one site still
 * sending the root. The effect's watcher list also didn't track
 * `_resolvedSymbol`/`_exchange`/`_resolvedExchange`/`_product` at all,
 * so even after picking a strike, nothing would re-trigger a re-plan
 * until some OTHER watched field (qty/price/side) also happened to
 * change.
 *
 * NOT dynamically reachable via the real UI as currently wired: the
 * `symType` prop that gates `_isBareUnderlying`/`_wantsOpt`/`_wantsFut`
 * (and therefore makes `_resolvedSymbol` diverge from the bare `symbol`
 * prop at all) is a static `$state('ALL')` in `SymbolPanel.svelte` with
 * no UI control that ever changes it away from `'ALL'` (confirmed by
 * grep — no `_symType =` assignment site exists outside its own
 * declaration). In every reachable `/orders` flow today,
 * `_resolvedSymbol` is therefore always IDENTICAL to `symbol` (picked
 * directly via the concrete-contract search), so a live interception
 * test cannot discriminate the bug — same class of finding as the
 * already-documented dead Exchange-picker plumbing
 * (`order_ticket_exchange_submit_payload.spec.js`'s own comment). This
 * is a source-level regression guard per the task's documented
 * fallback for this case.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

test.describe('Order Ticket — template preview uses resolved symbol and tracks it', () => {
  test('source: previewTicketTemplate sends _resolvedSymbol || symbol, not the bare symbol prop', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const idx = src.indexOf("await previewTicketTemplate({");
    expect(idx, 'previewTicketTemplate call not found').toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('});', idx));

    expect(block).not.toMatch(/tradingsymbol:\s*symbol,/);
    expect(block).toMatch(/tradingsymbol:\s*_resolvedSymbol\s*\|\|\s*symbol/);
  });

  test('source: the template-preview $effect watcher list tracks _resolvedSymbol, _exchange, _resolvedExchange, _product', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const effectIdx = src.indexOf('// Track the inputs that affect the preview.');
    expect(effectIdx, 'template-preview effect comment not found').toBeGreaterThan(-1);
    // Scope to the `inputs` array declaration + the follow-up push()
    // call that extends it (both execute synchronously at the top of
    // the $effect, so both participate in dependency tracking).
    const block = src.slice(effectIdx, src.indexOf('if (!symbol ||', effectIdx));

    expect(block).toMatch(/_resolvedSymbol\b/);
    expect(block).toMatch(/_exchange\b/);
    expect(block).toMatch(/_resolvedExchange\b/);
    expect(block).toMatch(/_product\b/);
  });
});
