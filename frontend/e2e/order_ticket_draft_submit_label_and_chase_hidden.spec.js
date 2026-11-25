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
 * defects were fixed then:
 *
 * 1. (SUPERSEDED 2026-11-24, see below.) The CHASE cluster was first made
 *    to disappear entirely (`{#if _chaseEnabled && !_sharedDraftMode}`)
 *    instead of staying greyed/non-interactive.
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
 *
 * 2026-11-24 — reversal of item 1 above (operator: "when draft mode
 * selected keep chase protected without hiding it completely... so draft
 * does not shift to left when chase is hidden"). Fully removing the CHASE
 * cluster made every control after it in the shared CHASE/DRAFT row
 * reflow/shift left each time Draft was toggled. The cluster now always
 * renders whenever chase would otherwise apply (stable row width), and
 * while Draft is on it is "protected" instead: deselected, greyed
 * (`.oes-chase-protected`, opacity + pointer-events:none) and
 * non-interactive (no info-panel click, `tabindex=-1`, `aria-disabled`).
 * This also makes BRACKET's former grey-not-hide-while-draft precedent
 * moot for this file — BRACKET itself was removed entirely in the
 * Bracket/Template/GTT removal (T3.1–T3.4, 2026-11), so there is nothing
 * left here for CHASE's new protected treatment to be "the exception"
 * against; CHASE is simply the one remaining cluster that now follows
 * that same deselected-but-present convention on its own.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);

test.describe('CHASE cluster stays rendered (protected, not hidden) while Draft mode is on', () => {
  test('CHASE cluster render condition no longer excludes draft mode — only chain tab / chase-disabled', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/\{#if _chaseEnabled && _activeTab !== 'chain'\}/);
    expect(src).not.toMatch(/\{#if _chaseEnabled && !_sharedDraftMode\}/);
  });

  test('the cluster carries a protected/greyed class while draft is on, not full removal', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/class="oes-chase-cluster"\s+class:oes-chase-protected=\{_sharedDraftMode\}/);
  });

  test('CHASE label deselects (.on removed) and becomes non-interactive while draft is on', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const clusterBlock = src.match(/\{#if _chaseEnabled && _activeTab !== 'chain'\}([\s\S]*?)\n        \{\/if\}/)?.[1] ?? '';
    expect(clusterBlock).toMatch(/class:on=\{!_sharedDraftMode\}/);
    expect(clusterBlock).toMatch(/tabindex=\{_sharedDraftMode \? -1 : 0\}/);
    expect(clusterBlock).toMatch(/aria-disabled=\{_sharedDraftMode\}/);
    expect(clusterBlock).toMatch(/if \(!_sharedDraftMode\) _chaseInfoOpen/);
  });

  test('.oes-chase-protected greys out and disables pointer interaction', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const ruleBlock = src.match(/\.oes-chase-protected\s*\{([\s\S]*?)\}/)?.[1] ?? '';
    expect(ruleBlock).toMatch(/opacity:\s*0\.4/);
    expect(ruleBlock).toMatch(/pointer-events:\s*none/);
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
