/**
 * chain_draft_toggle_on_expiry_row.spec.js
 *
 * 2026-11-03, operator: "move draft to days before expiry which is
 * presently 14d. this is the next row to where draft is presently
 * displayed." Confirmed scope (AskUserQuestion): Chain tab only — the
 * DRAFT toggle moves off the shared CHASE/BRACKET/DRAFT row and onto
 * the Chain tab's own Expiry row (the one showing the `{dte}d` chip,
 * inside `OptionChainTab.svelte`'s `.oct-toolbar`). The Ticket tab has
 * no Expiry/dte row to move the toggle onto, so it keeps rendering its
 * own copy in the shared row exactly as before.
 *
 * Source-level regression guard, same precedent as every other spec in
 * this suite touching `OptionChainTab`/`SymbolPanel`'s shared template
 * state (`chain_live_wing_confirmation_badge.spec.js`,
 * `bracket_scope_reset_pending_label_and_held_toast.spec.js`) — driving
 * a real Chain-tab interaction needs live quotes + a logged-in session
 * + a real template catalog, documented elsewhere in this suite as
 * impractical in CI.
 *
 * Covers:
 *  1. Exactly one `.ot-draft-toggle` definition exists per component —
 *     SymbolPanel's shared-row copy, now gated to exclude the Chain
 *     tab, and OptionChainTab's new copy inside `.oct-toolbar`.
 *  2. Svelte scopes `<style>` per component — OptionChainTab needed its
 *     own `.ot-draft-toggle`/`.ot-draft-label` CSS, not a reference to
 *     SymbolPanel's.
 *  3. The two copies are never both visible at once: SymbolPanel's
 *     guard excludes exactly the tab OptionChainTab's copy covers.
 *  4. State/handler are threaded through props, not re-derived —
 *     `draftMode`/`onDraftToggleChange` flow from SymbolPanel's
 *     `_sharedDraftMode`/`_onSharedDraftToggleChange` into
 *     `<OptionChainTab>`, so Draft state still carries over between
 *     tabs exactly as it did before the move.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH    = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');

test.describe('Chain DRAFT toggle moved onto the Expiry row (source-scan)', () => {
  let chainSrc = '';
  let panelSrc = '';

  test.beforeAll(() => {
    chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
  });

  test('OptionChainTab accepts draftMode/onDraftToggleChange props', () => {
    expect(chainSrc).toMatch(/draftMode\s*=\s*false,/);
    expect(chainSrc).toMatch(/onDraftToggleChange\s*=/);
  });

  test('the DRAFT toggle renders inside .oct-toolbar, after the Expiry dte chip', () => {
    const toolbarIdx = chainSrc.indexOf('<div class="oct-toolbar">');
    const dteIdx = chainSrc.indexOf('class="oct-expiry-dte"', toolbarIdx);
    const draftIdx = chainSrc.indexOf('class="ot-draft-toggle"', toolbarIdx);
    const toolbarCloseIdx = chainSrc.indexOf('\n    </div>', toolbarIdx);
    expect(toolbarIdx).toBeGreaterThan(-1);
    expect(dteIdx).toBeGreaterThan(toolbarIdx);
    expect(draftIdx).toBeGreaterThan(dteIdx);
    expect(draftIdx).toBeLessThan(toolbarCloseIdx);
  });

  test('the toolbar copy is wired to the props, not local state', () => {
    const idx = chainSrc.indexOf('class="ot-draft-toggle"');
    const slice = chainSrc.slice(idx, idx + 500);
    expect(slice).toContain('checked={draftMode}');
    expect(slice).toMatch(/onDraftToggleChange\?\.\(/);
  });

  test('OptionChainTab carries its own .ot-draft-toggle/.ot-draft-label CSS (Svelte scopes <style> per component)', () => {
    expect(chainSrc).toMatch(/\.ot-draft-toggle\s*\{/);
    expect(chainSrc).toMatch(/\.ot-draft-label\s*\{/);
    expect(chainSrc).toMatch(/\.ot-draft-label\.on\s*\{/);
  });

  test('the DRAFT toggle is left-aligned, not pushed to the right edge (2026-11-03, operator: "left aligned")', () => {
    // `.oct-toolbar` is a plain flex row with no `justify-content` spread,
    // so a bare `.ot-draft-toggle` with no auto-margin sits naturally left,
    // immediately after the dte chip — margin-left:auto was the earlier
    // 2026-11-03 "move to Expiry row" fix's own right-push, now reverted.
    const idx = chainSrc.indexOf('.ot-draft-toggle {');
    expect(idx).toBeGreaterThan(-1);
    const rule = chainSrc.slice(idx, chainSrc.indexOf('}', idx));
    expect(rule).not.toMatch(/margin-left:\s*auto/);
  });

  test('SymbolPanel suppresses its own shared-row DRAFT copy specifically while Chain is active', () => {
    // 2026-11-25 (Symbol+Expiry merge) — the per-control `_activeTab !==
    // 'chain'` guard that used to sit directly on `{#if _draftToggleVisible
    // ...}` moved to the OUTER `.oes-chase-row` gate, so the whole row
    // (CHASE + DRAFT) is skipped on Chain instead of rendering an empty
    // `<div>` there (the dead middle row this same merge also removes).
    // `_draftToggleVisible` itself carries no Chain exclusion any more —
    // it doesn't need to, since the entire row is already gone by then.
    const draftIdx = panelSrc.indexOf('{#if _draftToggleVisible');
    expect(draftIdx, 'DRAFT toggle if-block not found').toBeGreaterThan(-1);
    const draftLine = panelSrc.slice(draftIdx, panelSrc.indexOf('\n', draftIdx));
    expect(draftLine).not.toMatch(/_activeTab !== 'chain'/);

    const rowIdx = panelSrc.indexOf('<div class="oes-chase-row">');
    expect(rowIdx, '.oes-chase-row div not found').toBeGreaterThan(-1);
    // The guarding {#if} is the nearest one above the div's own opening tag.
    const gateIdx = panelSrc.lastIndexOf('{#if', rowIdx);
    expect(gateIdx).toBeGreaterThan(-1);
    const gateLine = panelSrc.slice(gateIdx, panelSrc.indexOf('\n', gateIdx));
    expect(gateLine).toMatch(/_activeTab !== 'chain'/);
    expect(draftIdx).toBeGreaterThan(gateIdx);
  });

  test("SymbolPanel threads _sharedDraftMode/_onSharedDraftToggleChange into <OptionChainTab> as draftMode/onDraftToggleChange", () => {
    // The real invocation tag, not a prose mention of "<OptionChainTab>"
    // inside a comment elsewhere in this same file.
    const idx = panelSrc.indexOf('<OptionChainTab\n');
    expect(idx, 'real <OptionChainTab invocation tag not found').toBeGreaterThan(-1);
    const closeIdx = panelSrc.indexOf('\n        />', idx);
    expect(closeIdx).toBeGreaterThan(idx);
    const propsSlice = panelSrc.slice(idx, closeIdx);
    expect(propsSlice).toContain('draftMode={_sharedDraftMode}');
    expect(propsSlice).toContain('onDraftToggleChange={_onSharedDraftToggleChange}');
  });
});
