/**
 * order_ticket_chain_leg_preview_lots.spec.js
 *
 * 2026-10-20 fix (plan item 7, Chain-tab leg half) — `SymbolPanel.svelte`'s
 * per-leg template-preview effect (the Chain tab's own debounced call to
 * `previewTicketTemplate` for the operator-focused basket leg) sent
 * `quantity: (leg.lots || 1) * (leg.lotSize || 1)` — CONTRACTS. The
 * backend's ticket-preview endpoint (`_rco_preview_resolve_qty` in
 * `backend/api/routes/orders.py`) reads `quantity` as LOTS for F&O and
 * multiplies by lot_size itself. Chain-tab legs are always F&O options,
 * so this doubled the lot multiplier server-side — `parent_qty` became
 * `lot_size²` contracts instead of `lot_size` contracts, inflating the
 * wing qty and every TP/SL/margin chip derived from the plan. Fixed to
 * send `leg.lots || 1`, matching `OptionChainTab.svelte`'s own existing
 * `leg.lots`-only convention for the identical endpoint.
 *
 * Covered as a source-scan test rather than a live Chain-tab
 * interception: constructing a live multi-leg Chain basket (expiry +
 * strike + CE/PE picks for a focused leg, reliably triggering
 * `_focusedLeg`) is a heavier live-UI flow than this mechanical
 * one-line unit-conversion fix warrants; the live `OrderTicket.svelte`
 * call site for the SAME bug class (also fixed in this round) is
 * covered by a full live interception test in
 * `order_ticket_preview_lots_not_contracts.spec.js`.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

test.describe('SymbolPanel — Chain-leg template preview sends lots, not contracts', () => {
  test('source: the per-leg previewTicketTemplate call sends leg.lots, not leg.lots * leg.lotSize', () => {
    const src = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    const idx = src.indexOf('await previewTicketTemplate({');
    expect(idx, 'previewTicketTemplate call not found in SymbolPanel.svelte').toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('});', idx));

    // Old buggy expression (contracts) must be gone.
    expect(block).not.toMatch(/quantity:\s*\(leg\.lots\s*\|\|\s*1\)\s*\*\s*\(leg\.lotSize\s*\|\|\s*1\)/);
    // New expression sends lots only.
    expect(block).toMatch(/quantity:\s*leg\.lots\s*\|\|\s*1/);
  });
});
