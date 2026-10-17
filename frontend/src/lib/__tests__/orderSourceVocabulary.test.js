/**
 * orderSourceVocabulary.test.js
 *
 * Sprint 2b (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §3/§4.4) —
 * "source" plumbing for order placement. TicketOrderRequest/BasketLeg
 * already had a `source` field on the backend schema, but no frontend
 * caller ever sent one. This file covers the placement call sites that
 * build their own request-literal inline (OptionChainTab, SymbolPanel's
 * basket submit) rather than going through the shared `buildPlacePayload`
 * helper (covered separately in orderTicketSubmit.test.js).
 *
 * CommandLineTab.svelte's own `source: 'command'` coverage was removed
 * here when that component was deleted entirely (2026-10-17,
 * operator-confirmed removal of the legacy positional Console CLI
 * fallback — see the 2026-10-17 entry in docs/specs/AUTOMATION_SPEC.md's
 * change log).
 *
 * Neither remaining component has an existing mount harness in this
 * repo (no @testing-library/svelte, vitest runs in the `node`
 * environment per vitest.config.js) — same constraint documented in
 * InfoHint.sourceAudit.test.js. This file follows that file's
 * established source-audit pattern: import the raw `.svelte` text via
 * `?raw`, slice to the specific call-site block (not "anywhere in the
 * file", which would pass even if the literal were added to the wrong
 * function), and assert the exact field.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT   — asserts the one call site per component that actually
 *              reaches placeTicketOrder()/the basket leg builder
 *  2. Perf   — pure text/regex, no DOM, no network
 *  3. Stale  — each slice is bounded to the real function, so a future
 *              edit that moves `source` to the wrong block fails loudly
 *              instead of a loose file-wide regex silently passing
 *  4. Reuse  — no new chip/UI paradigm; reuses existing request shapes
 *  5. UX     — n/a here (covered by orderCardOriginChip.test.js)
 */

import { describe, it, expect } from 'vitest';
import OPTION_CHAIN_SRC from '../order/OptionChainTab.svelte?raw';
import SYMBOL_PANEL_SRC from '../SymbolPanel.svelte?raw';

describe('OptionChainTab.svelte — _placeOneLeg tags source="chain"', () => {
  const start = OPTION_CHAIN_SRC.indexOf('async function _placeOneLeg');
  const end   = OPTION_CHAIN_SRC.indexOf('function _finalizeBasket', start);
  const block = OPTION_CHAIN_SRC.slice(start, end);

  it('_placeOneLeg exists and is non-empty (sanity check on the slice bounds)', () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
  });

  it('the placeTicketOrder() call inside _placeOneLeg sets source: \'chain\'', () => {
    expect(block).toMatch(/placeTicketOrder\(\{[\s\S]*source:\s*'chain'[\s\S]*?\}\);/);
  });
});

describe('SymbolPanel.svelte — submitBasket tags each leg source="basket"', () => {
  const start = SYMBOL_PANEL_SRC.indexOf('async function submitBasket');
  const end   = SYMBOL_PANEL_SRC.indexOf('function handleParsedOrder', start);
  const block = SYMBOL_PANEL_SRC.slice(start, end);

  it('submitBasket exists and is non-empty (sanity check on the slice bounds)', () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
  });

  it('the per-leg group-builder sets source: \'basket\' on every leg', () => {
    expect(block).toMatch(/source:\s*'basket'/);
  });

  // Guard against the literal landing on `onSubmit?.({...})` (the local
  // UI callback after a leg places) instead of the actual `groups` leg
  // builder that reaches the /orders/basket network call.
  it('source: \'basket\' is inside the groups/legs builder, not the onSubmit callback', () => {
    const groupsIdx = block.indexOf('const groups =');
    const placeIdx  = block.indexOf('await placeBasket(groups)');
    const sourceMatch = block.match(/source:\s*'basket'/);
    expect(groupsIdx).toBeGreaterThan(-1);
    expect(placeIdx).toBeGreaterThan(groupsIdx);
    expect(sourceMatch).not.toBeNull();
    const sourceIdx = sourceMatch.index;
    expect(sourceIdx).toBeGreaterThan(groupsIdx);
    expect(sourceIdx).toBeLessThan(placeIdx);
  });
});
