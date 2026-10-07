// Chain/basket submission must be disabled per-leg (SymbolPanel's shared
// basket submit gate) until a live chain quote has actually arrived for
// that leg's OWN strike — not just "the limit field holds some positive
// number", which a stale prior quote or a hand-typed value can satisfy
// just as easily as a real one. Mirrors the existing OrderTicket
// `_depthPending` invariant (see order_ticket_depth_pending_submit_gate.spec.js)
// applied per-leg instead of to one ticket.
// Source-level guard: reads the real component source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const panelSrc = readFileSync(
  new URL('../src/lib/SymbolPanel.svelte', import.meta.url).pathname, 'utf8'
);
const chainSrc = readFileSync(
  new URL('../src/lib/order/OptionChainTab.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('OptionChainTab — per-leg quote-arrival tracking', () => {
  test('addOptionToBasket stamps quoteArrived from the current chain-quotes map', () => {
    expect(chainSrc).toMatch(/quoteArrived: !!q,/);
  });

  test('a sync effect latches quoteArrived true as the quotes poll resolves', () => {
    const idx = chainSrc.indexOf('Keep `quoteArrived` in sync');
    expect(idx).toBeGreaterThan(-1);
    expect(chainSrc).toMatch(
      /const pending = legs\.filter\(l => !l\.quoteArrived && l\.strike != null && l\.optType\s*\n\s*&& map\[String\(l\.strike\)\]\?\.\[String\(l\.optType\)\.toLowerCase\(\)\]\);/
    );
    // One-way latch — never flips quoteArrived back to false.
    expect(chainSrc).toMatch(
      /onUpdateLeg\(leg\.key, \(l\) => \(l\.quoteArrived \? l : \{ \.\.\.l, quoteArrived: true \}\)\);/
    );
  });
});

test.describe('SymbolPanel — basket submit gate requires per-leg quote arrival', () => {
  test('_legNeedsDepth gates option legs on quoteArrived, futures legs on limit only', () => {
    expect(panelSrc).toMatch(
      /const _legNeedsDepth = \(\/\*\* @type \{any\} \*\/ leg\) =>\s*\n\s*!\(Number\(leg\.limit\) > 0\) \|\| \(leg\.strike != null && leg\.optType != null && !leg\.quoteArrived\);/
    );
  });

  test('submitBasket() validates every leg via _legNeedsDepth before placing', () => {
    const idx = panelSrc.indexOf('async function submitBasket()');
    expect(idx).toBeGreaterThan(-1);
    const guardIdx = panelSrc.indexOf('basketLegs.find(_legNeedsDepth)', idx);
    const brokerCallIdx = panelSrc.indexOf('await placeBasket(groups)', idx);
    expect(guardIdx).toBeGreaterThan(idx);
    expect(guardIdx).toBeLessThan(brokerCallIdx);
  });

  test('_basketDepthPending derived covers any not-ready leg in the basket', () => {
    expect(panelSrc).toMatch(
      /const _basketDepthPending = \$derived\.by\(\(\) =>\s*\n\s*basketLegs\.length > 0 && basketLegs\.some\(_legNeedsDepth\)\s*\n\s*\);/
    );
  });

  test('common-action Submit button disables and grays out on _basketDepthPending too', () => {
    expect(panelSrc).toMatch(
      /class:oes-common-submit-depth-pending=\{_ticketDepthPending \|\| _basketDepthPending\}/
    );
    expect(panelSrc).toMatch(
      /disabled=\{basketSubmitting\s*\n?\s*\|\|\s*\(basketLegs\.length === 0 && _activeTab === 'chain'\)\s*\n?\s*\|\|\s*_ticketOwnSubmitBusy\s*\n?\s*\|\|\s*_ticketDepthPending\s*\n?\s*\|\|\s*_basketDepthPending\}/
    );
  });

  test('per-leg basket pill shows the depth-pending warn state and tooltip', () => {
    expect(panelSrc).toMatch(
      /class:oes-basket-pill-limit-warn=\{_legNeedsDepth\(leg\)\}/
    );
    expect(panelSrc).toMatch(/Waiting for market depth \(bid\/ask\) for this strike/);
  });
});
