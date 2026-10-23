/**
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js
 *
 * 2026-10-23 — operator report: "it is not add to payoff. better to use
 * the same submit button. another thing, when submitting draft order,
 * chase is getting invoked and chase status is shown."
 *
 * Investigated live (prod + dev DB query): no `mode='draft'` AlgoOrder row
 * has ever been created on either environment, and both frontend draft-
 * submit paths (OrderTicket.svelte's single-leg path, SymbolPanel.svelte's
 * submitBasket()) explicitly `return` before any chase/broker call — so
 * this was never a real broker/chase invocation. Two real presentation
 * defects, fixed here, source-grep only (no live render needed for a pure
 * template-condition / string-literal regression guard):
 *
 * 1. The CHASE cluster (toggle + aggressiveness picker) in SymbolPanel's
 *    shared CHASE/BRACKET/DRAFT row stayed visually present — merely
 *    greyed/non-interactive — while Draft mode was on, instead of
 *    disappearing. A draft never reaches chase, so there is nothing for
 *    it to show. Now hidden entirely via `{#if _chaseEnabled &&
 *    !_sharedDraftMode}`. BRACKET deliberately keeps its own grey-not-hide
 *    treatment (coordinator: "no brackets in draft mode") — untouched,
 *    out of scope for this fix.
 * 2. The shared `.oes-common-submit` button (the one real submit button —
 *    OrderTicket's own internal footer is always suppressed via
 *    `actionsHidden` at the real SymbolPanel mount) read "Add to Payoff"
 *    while Draft mode was on, a longer label than the "Submit"/CLOSE-BUY-
 *    format label every other mode uses. The DRAFT toggle already
 *    signals the mode; the button's own `title` tooltip still explains
 *    "no broker order" on hover. The wider "Add to Payoff" string was
 *    also what pushed the button onto its own row below a single-leg
 *    basket pill on a narrow viewport (operator screenshot) instead of
 *    fitting beside it.
 *
 * Follow-up (same day, operator: "use submit draft like close buy format
 * for button when draft is selected") — a single staged Chain-tab leg in
 * Draft mode now gets the same CLOSE/BUY-style formatted label
 * (`formatSubmitLabel`) the Ticket tab's single order already used,
 * instead of a bare "Submit". A multi-leg basket keeps the pre-existing
 * plain "Submit" (2026-09-30 operator decision, unrelated to Draft) since
 * there is no one coherent side to format across mixed-side legs.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);

test.describe('CHASE cluster hidden (not just greyed) while Draft mode is on', () => {
  test('CHASE cluster render condition excludes draft mode', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/\{#if _chaseEnabled && !_sharedDraftMode\}/);
  });

  test('CHASE cluster no longer carries the grey/disabled-while-draft class — it is gone, not styled', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const clusterBlock = src.match(/\{#if _chaseEnabled && !_sharedDraftMode\}([\s\S]*?)\{\/if\}/)?.[1] ?? '';
    expect(clusterBlock).not.toContain('oes-ctl-cluster-disabled');
    expect(clusterBlock).not.toContain('_sharedDraftMode');
  });

  test('BRACKET keeps its own separate grey-while-draft treatment — not touched by this fix', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/oes-bracket-cluster[\s\S]{0,40}class:oes-ctl-cluster-disabled=\{_sharedDraftMode\}/);
  });
});

test.describe('Shared submit button uses one label regardless of Draft mode', () => {
  test('_submitLabel no longer special-cases a distinct "Add to Payoff" string for draft mode', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const derivedBlock = src.match(/const _submitLabel = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(derivedBlock).not.toContain("return 'Add to Payoff'");
  });

  test('basket-submit button text routes through _submitLabel, not a hardcoded draft-vs-non-draft string literal', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).not.toMatch(/_sharedDraftMode \? 'Add to Payoff' : 'Submit'/);
    expect(src).toMatch(/\(basketLegs\.length > 0\s*\n\s*\? _submitLabel/);
  });

  test('a single staged leg in draft mode gets the same CLOSE/BUY-style formatted label as the Ticket tab, not a bare "Submit"', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const derivedBlock = src.match(/const _submitLabel = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(derivedBlock).toMatch(/if \(_sharedDraftMode && basketLegs\.length === 1\) \{/);
    expect(derivedBlock).toMatch(/formatSubmitLabel\(\{ side: leg\.side,/);
    // Must come BEFORE the generic basket/chain "always Submit" branch,
    // or the single-leg-draft case would never be reached.
    const singleLegIdx = derivedBlock.indexOf('_sharedDraftMode && basketLegs.length === 1');
    const genericIdx = derivedBlock.indexOf("basketLegs.length > 0 || _activeTab === 'chain'");
    expect(singleLegIdx).toBeGreaterThan(-1);
    expect(genericIdx).toBeGreaterThan(singleLegIdx);
  });

  test('a multi-leg basket still falls back to plain "Submit" even in draft mode — no single coherent side to format', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const derivedBlock = src.match(/const _submitLabel = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(derivedBlock).toMatch(/if \(basketLegs\.length > 0 \|\| _activeTab === 'chain'\) return 'Submit';/);
  });

  test('the draft explanation survives as a tooltip, not the visible label', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toContain('Add legs to payoff chart (no broker order)');
  });
});
