// Order submission must be disabled (ticket's own footer button AND
// SymbolPanel's shared common-action Submit button) until market depth
// (bid/ask) has loaded for the ACTIVE strike. Every template/ticket is
// LIMIT or GTT (no MARKET) — submitting before depth arrives previously
// reached the server and failed with "limit price required" instead of
// the button being disabled with a clear reason.
// Source-level guard: reads the real component source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const ticketSrc = readFileSync(
  new URL('../src/lib/order/OrderTicket.svelte', import.meta.url).pathname, 'utf8'
);
const panelSrc = readFileSync(
  new URL('../src/lib/SymbolPanel.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('OrderTicket — depth-pending submit gate', () => {
  test('_depthPending derived gates on showLimit + no quote yet', () => {
    expect(ticketSrc).toMatch(
      /const _depthPending = \$derived\(showLimit && !_lastQuote\);/
    );
  });

  test('_lastQuote resets to null on strike/symbol change', () => {
    expect(ticketSrc).toMatch(
      /untrack\(\(\) => \{ _lastQuote = null; \}\);/
    );
  });

  test('submit() refuses while depth is pending, before the broker call', () => {
    const idx = ticketSrc.indexOf('async function submit()');
    expect(idx).toBeGreaterThan(-1);
    const guardIdx = ticketSrc.indexOf('if (_depthPending)', idx);
    const brokerCallIdx = ticketSrc.indexOf('buildPlacePayload', idx);
    expect(guardIdx).toBeGreaterThan(idx);
    expect(guardIdx).toBeLessThan(brokerCallIdx);
    expect(ticketSrc).toMatch(/Waiting for market depth — try again in a moment/);
  });

  test('internal footer Submit button disables on depth-pending (non-draft)', () => {
    expect(ticketSrc).toMatch(
      /disabled=\{_isDemo \? false : \(!!validationErr \|\| submitting \|\| _noSymbol \|\| \(!_draftMode && _depthPending\)\)\}/
    );
  });

  test('depthPending is piped to the host via onTicketStateChange', () => {
    expect(ticketSrc).toMatch(/depthPending: _depthPending,/);
  });

  test('internal footer Submit button switches to gray while depth-pending, LIMIT/SL only', () => {
    expect(ticketSrc).toMatch(
      /class:ot-submit-depth-pending=\{!_isDemo && !_draftMode && _depthPending\}/
    );
    // buy/sell colour classes are suppressed while depth-pending so the
    // gray override isn't fighting a higher-specificity colour rule.
    expect(ticketSrc).toMatch(
      /class:ot-submit-buy=\{_side === 'BUY' && !_draftMode && !\(!_isDemo && _depthPending\)\}/
    );
    expect(ticketSrc).toMatch(/\.ot-submit\.ot-submit-depth-pending \{/);
  });
});

test.describe('SymbolPanel — shared Submit button respects depth-pending', () => {
  test('_ticketDepthPending derived mirrors the ticket state', () => {
    expect(panelSrc).toMatch(/!!_ticketState\.depthPending/);
  });

  test('_modalFireSubmit blocks with a toast before bumping the trigger', () => {
    const idx = panelSrc.indexOf('function _modalFireSubmit()');
    const guardIdx = panelSrc.indexOf('if (_ticketDepthPending)', idx);
    const bumpIdx = panelSrc.indexOf('_modalTriggerSubmit++', idx);
    expect(guardIdx).toBeGreaterThan(idx);
    expect(guardIdx).toBeLessThan(bumpIdx);
  });

  test('common-action Submit button disables on _ticketDepthPending', () => {
    // Button now also disables on `_basketDepthPending` (basket-leg depth
    // gate, see basket_leg_depth_pending_submit_gate.spec.js) — the
    // `_ticketDepthPending` term from the original fix is unchanged.
    expect(panelSrc).toMatch(
      /disabled=\{basketSubmitting\s*\n?\s*\|\|\s*\(basketLegs\.length === 0 && _activeTab === 'chain'\)\s*\n?\s*\|\|\s*_ticketOwnSubmitBusy\s*\n?\s*\|\|\s*_ticketDepthPending\s*\n?\s*\|\|\s*_basketDepthPending\}/
    );
  });

  test('common-action Submit button switches to gray while depth-pending', () => {
    // Class now ORs in `_basketDepthPending` too — same gray override,
    // extended to cover the basket-leg depth gate.
    expect(panelSrc).toMatch(
      /class:oes-common-submit-depth-pending=\{_ticketDepthPending \|\| _basketDepthPending\}/
    );
    expect(panelSrc).toMatch(
      /\.oes-common-submit\.oes-common-submit-depth-pending \{/
    );
  });
});
