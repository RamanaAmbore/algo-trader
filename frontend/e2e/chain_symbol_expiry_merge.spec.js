/**
 * chain_symbol_expiry_merge.spec.js
 *
 * 2026-11-25, operator: merge the Chain tab's Symbol control (owned by
 * SymbolPanel.svelte's `.oes-picker` row) and its own Expiry picker
 * (OptionChainTab.svelte's `.oct-toolbar` row) into one combined visual
 * element — "SYMBOL (EXPIRY)" — and compress the Chain tab down from
 * three rows (`.oes-picker` / the now-dead empty `.oes-chase-row` /
 * `.oct-toolbar`) to one.
 *
 * Mechanism (see the inline comments at each site for the full
 * rationale):
 *   - SymbolPanel.svelte defines a `{#snippet _chainSymbolPicker()}`
 *     wrapping the exact `.oes-sym-pick` + `SymbolSearchInput` markup
 *     it already rendered inline in `.oes-picker`. Svelte compiles a
 *     snippet's markup (and scoped styles) against the component that
 *     DEFINES it, not the one that renders it, so this snippet's own
 *     `.oes-sym-pick` CSS still applies wherever it's rendered.
 *   - SymbolPanel passes it to `<OptionChainTab symbolPicker={...}>`,
 *     and suppresses its own inline `.oes-sym-pick` copy specifically
 *     while Chain is active (`.oes-picker` keeps rendering Account +
 *     Exchange as before, for every tab).
 *   - OptionChainTab renders the Symbol slot first inside a new
 *     `.oct-sym-expiry` wrapper, followed by its Expiry picker in
 *     literal parentheses — one combined unit, both controls still
 *     fully independent/functional.
 *   - `.oes-chase-row` (the shared CHASE/DRAFT row) now also excludes
 *     the Chain tab at its OUTER gate, removing the dead empty `<div>`
 *     that used to sit between `.oes-picker` and `.oct-toolbar` once
 *     both its children had already individually moved off Chain.
 *   - (2026-11-25/26 original behaviour, REVERSED 2026-11-26 — see the
 *     amendment below) `.oct-toolbar` never wrapped (`flex-wrap:
 *     nowrap`, was `wrap`): `.oct-sym-expiry` was the one flexible/
 *     shrinkable half of the row, and `.oct-toolbar-trailing` (DTE chip
 *     through DRAFT) held every fixed-size control at `flex-shrink: 0`,
 *     scrolling horizontally (`overflow-x: auto`) instead of squeezing
 *     any one of them illegible — "shrink the flexible element, scroll
 *     the rest, never wrap", same convention as the page header
 *     (AlgoTimestamp). This made the single-row outcome a structural
 *     guarantee (enforced by flexbox layout), not merely a coincidence
 *     of one or two tested viewport widths.
 *
 * Test shape — source-scan (static, run on every push, no browser/
 * backend needed) covers the structural/CSS guarantees exhaustively;
 * live (real browser + backend) covers the three behavioural
 * dimensions the task calls out explicitly. This suite's own
 * established precedent (`chain_draft_toggle_on_expiry_row.spec.js`,
 * `chain_grid_and_basket_step_styling.spec.js`'s "Live" describe
 * block, `algo_footer_centering_and_chain_toolbar_alignment.spec.js`)
 * is to drive the real Chain tab via `/orders`'s inline SymbolPanel —
 * reused here rather than invented fresh.
 *
 * AMENDED 2026-11-26 (same day, separate operator instruction: "in
 * chain keep element after symbol expiry in the next row") — the
 * single-row-via-nowrap guarantee above is REVERSED. `.oct-toolbar` is
 * now `flex-wrap: wrap`, and `.oct-toolbar-trailing` forces itself onto
 * a fresh second row via `flex: 0 1 100%` (a 100%-basis flex item
 * always starts a new line in a wrapping container). `.oct-sym-expiry`
 * is unchanged (`flex: 0 0 auto`) and alone occupies row 1. This is NOT
 * a regression of the "dead empty row" bug the 2026-11-25 merge
 * removed — that bug was an empty `<div>` with nothing in it;
 * `.oct-toolbar-trailing` always has real content (DRAFT renders
 * unconditionally at minimum), so a non-empty second row here is the
 * explicit, current intent. The tests below that asserted `nowrap`/
 * single-row/`flex: 0 1 auto` on `.oct-toolbar-trailing` were updated
 * in place to assert the new two-row shape instead of being left to
 * contradict the shipped code — see each test's own inline note.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');

/**
 * Strips `/* ... *\/` block comments before matching a CSS rule's actual
 * declarations. `.oct-toolbar`'s own in-rule comment legitimately quotes
 * its OLD `flex-wrap: nowrap` value for history (2026-11-26 reversal),
 * which would otherwise false-fail a naive "this value must not appear
 * anywhere in the rule" assertion against the rule's full text.
 * @param {string} rule
 */
function _declsOnly(rule) {
  return rule.replace(/\/\*[\s\S]*?\*\//g, '');
}

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural/CSS guarantees (no browser needed)
// ──────────────────────────────────────────────────────────────────

test.describe('Symbol+Expiry merge — OptionChainTab.svelte structure', () => {
  let chainSrc = '';

  test.beforeAll(() => {
    chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('OptionChainTab accepts an optional symbolPicker snippet prop', () => {
    expect(chainSrc).toMatch(/symbolPicker\s*=\s*\/\*\*[^*]*\*\/\s*\(undefined\)/);
    expect(chainSrc).toMatch(/symbolPicker\?:\s*import\('svelte'\)\.Snippet/);
  });

  test('the toolbar gate renders whenever symbolPicker is present, not only when expiries have resolved', () => {
    // The Symbol control must never disappear just because instruments
    // are still loading or the picked underlying has no expiries yet —
    // the operator needs to be able to change symbol regardless.
    const idx = chainSrc.indexOf("{#if symbolPicker || (chainUnderlying && chainExpiries.length)}");
    expect(idx, 'broadened toolbar gate not found').toBeGreaterThan(-1);
    const toolbarIdx = chainSrc.indexOf('<div class="oct-toolbar">', idx);
    expect(toolbarIdx).toBeGreaterThan(idx);
  });

  test('.oct-sym-expiry renders the symbolPicker snippet, then "(" + the Expiry Select + ")"', () => {
    const wrapIdx = chainSrc.indexOf('<div class="oct-sym-expiry">');
    expect(wrapIdx, '.oct-sym-expiry wrapper not found').toBeGreaterThan(-1);
    const renderIdx = chainSrc.indexOf('{@render symbolPicker()}', wrapIdx);
    const openParenIdx = chainSrc.indexOf('class="oct-expiry-paren" aria-hidden="true">(', wrapIdx);
    const pickIdx = chainSrc.indexOf('<div class="oct-expiry-pick">', wrapIdx);
    const selectIdx = chainSrc.indexOf('<Select', pickIdx);
    const closeParenIdx = chainSrc.indexOf('class="oct-expiry-paren" aria-hidden="true">)', wrapIdx);
    expect(renderIdx, 'symbolPicker render call not found').toBeGreaterThan(wrapIdx);
    expect(openParenIdx, 'opening paren not found').toBeGreaterThan(renderIdx);
    expect(pickIdx, '.oct-expiry-pick not found').toBeGreaterThan(openParenIdx);
    expect(selectIdx, 'Expiry Select not found inside .oct-expiry-pick').toBeGreaterThan(pickIdx);
    expect(closeParenIdx, 'closing paren not found').toBeGreaterThan(selectIdx);
  });

  test('the parens are gated on symbolPicker being present — a hypothetical bare mount falls back to a plain "Expiry" label, never bare orphan parens', () => {
    const wrapIdx = chainSrc.indexOf('<div class="oct-sym-expiry">');
    const openParenGuard = chainSrc.indexOf('{#if symbolPicker && chainUnderlying && chainExpiries.length}', wrapIdx);
    expect(openParenGuard, 'opening-paren guard not found').toBeGreaterThan(wrapIdx);
    const fallbackLabelIdx = chainSrc.indexOf('class="oct-toolbar-label"', wrapIdx);
    expect(fallbackLabelIdx, 'fallback label not found').toBeGreaterThan(wrapIdx);
    expect(fallbackLabelIdx).toBeLessThan(openParenGuard);
    // The fallback label CSS rule still exists (for that branch only) —
    // this is a deliberate, documented exception to the general "remove
    // dead CSS" convention, since no live mount reaches it today.
    expect(chainSrc).toMatch(/\.oct-toolbar-label\s*\{/);
  });

  test('DTE chip, Spread%, Hedge, Chase and Draft all render inside .oct-toolbar-trailing, after .oct-sym-expiry closes', () => {
    const symExpiryOpenIdx = chainSrc.indexOf('<div class="oct-sym-expiry">');
    const trailingOpenIdx = chainSrc.indexOf('<div class="oct-toolbar-trailing">', symExpiryOpenIdx);
    expect(trailingOpenIdx, '.oct-toolbar-trailing not found').toBeGreaterThan(symExpiryOpenIdx);
    for (const needle of [
      'class="oct-expiry-dte"',
      'class="oct-spread-field"',
      'class="oct-hedge-toggle"',
      'class="oes-chase-cluster"',
      'class="ot-draft-toggle"',
    ]) {
      const i = chainSrc.indexOf(needle, trailingOpenIdx);
      expect(i, `${needle} not found inside .oct-toolbar-trailing`).toBeGreaterThan(trailingOpenIdx);
    }
  });

  // AMENDED 2026-11-26 (operator: "in chain keep element after symbol
  // expiry in the next row") — reverses the `nowrap` guarantee this
  // test used to pin. `.oct-toolbar` is now `flex-wrap: wrap`, so
  // `.oct-toolbar-trailing` can (and, per its own `flex: 0 1 100%`
  // basis — see the dedicated test below — always does) start a fresh
  // second row below `.oct-sym-expiry`.
  test('.oct-toolbar is flex-wrap: wrap (was nowrap) — .oct-toolbar-trailing is allowed to start a second row', () => {
    const rule = chainSrc.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).toMatch(/flex-wrap:\s*wrap;/);
    expect(decls).not.toMatch(/flex-wrap:\s*nowrap/);
  });

  test('.oct-toolbar still scopes --ctl-h and the same 0.4rem left-padding convention (pre-existing invariants preserved)', () => {
    const rule = chainSrc.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule).toMatch(/--ctl-h:\s*1\.4rem/);
    expect(rule).toMatch(/padding:\s*0\.25rem 0\.4rem 0\.35rem/);
    expect(rule).not.toMatch(/\n\s*border-bottom:\s*\S/);
  });

  // 2026-11-26 — `.oct-sym-expiry` no longer shrinks below its own
  // content (`flex: 0 0 auto`, was `0 1 auto; min-width: 0`). Confirmed
  // live before this change: at 370px with a 9-char symbol
  // ("BANKNIFTY"), the old `min-width: 0` let the flex layout compress
  // the chip to 130px while its own children still needed 186px,
  // spilling 56px of content outside the drawn border — now that the
  // chip has a real border, letting it shrink means letting content
  // spill past that border, strictly worse than the "two boxes" defect
  // this whole fix exists to close. `.oct-toolbar-trailing`'s own
  // `overflow-x: auto` absorbs the squeeze instead (see its own test
  // below).
  test('.oct-sym-expiry never shrinks below its own content, and never grows either (flex: 0 0 auto)', () => {
    const rule = chainSrc.match(/\.oct-sym-expiry\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-sym-expiry rule').not.toBe('');
    // flex-grow MUST be 0 — growing this box on a wide viewport would
    // leave dead space inside it (neither the Symbol snippet nor the
    // Expiry picker grows to fill it), visually pushing DTE→DRAFT
    // rightward and undoing the "DRAFT stays left-aligned" decision.
    // flex-shrink MUST also be 0 (the 2026-11-26 fix) — see above.
    expect(rule).toMatch(/flex:\s*0 0 auto/);
    expect(rule).not.toMatch(/flex:\s*1 1 auto/);
    expect(rule).not.toMatch(/flex:\s*0 1 auto/);
    // No `overflow: hidden`/`auto` here — SymbolSearchInput's own results
    // dropdown is `position: absolute` relative to its wrapper, not
    // `fixed`, so clipping this container would clip that dropdown too.
    expect(rule).not.toMatch(/overflow:/);
  });

  // 2026-11-26 chrome-merge fix — operator reported the prior merge
  // still rendered as TWO visibly boxed controls (Symbol input's own
  // border, Expiry Select's own border) either side of literal "("/")"
  // text, not one combined element. `.oct-sym-expiry` now carries the
  // ONE shared border/background for the whole unit; both children
  // strip their own chrome (this file's `.rbq-select-trigger` override,
  // and SymbolPanel.svelte's `.ssi-input` override, tested separately).
  test('.oct-sym-expiry carries the single shared chip border/background for the whole "SYMBOL (EXPIRY)" unit', () => {
    const rule = chainSrc.match(/\.oct-sym-expiry\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-sym-expiry rule').not.toBe('');
    expect(rule).toMatch(/background:\s*#1d2a44/);
    expect(rule).toMatch(/border:\s*1px solid rgba\(251,\s*191,\s*36,\s*0\.25\)/);
    expect(rule).toMatch(/border-radius:\s*3px/);
  });

  test('.oct-expiry-pick :global(.rbq-select-trigger) strips its own border/background outright, not merely to transparent', () => {
    const rule = chainSrc.match(/\.oct-expiry-pick :global\(\.rbq-select-trigger\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-expiry-pick :global(.rbq-select-trigger) rule').not.toBe('');
    expect(rule).toMatch(/border:\s*none/);
    expect(rule).toMatch(/background:\s*none/);
    expect(rule).toMatch(/background-image:\s*none/);
  });

  // AMENDED 2026-11-26 — `flex: 0 1 auto` (shared row with
  // `.oct-sym-expiry`) is replaced by `flex: 0 1 100%`. A 100%-basis
  // flex item in a `flex-wrap: wrap` container (`.oct-toolbar`, see its
  // own test above) always starts a fresh line — this is the mechanism
  // that forces `.oct-toolbar-trailing` onto its own row below "SYMBOL
  // (EXPIRY)", not an accident of viewport width. Shrink stays 1 (not
  // 0), so the row can still shrink as a whole; `overflow-x: auto`
  // below is the real squeeze-absorber, same as before this change.
  test('.oct-toolbar-trailing is flex: 0 1 100% (was 0 1 auto) — the 100% basis forces it onto its own row below .oct-sym-expiry', () => {
    const rule = chainSrc.match(/\.oct-toolbar-trailing\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-trailing rule').not.toBe('');
    expect(rule).toMatch(/flex:\s*0 1 100%/);
    expect(rule).not.toMatch(/flex:\s*0 1 auto/);
  });

  test('.oct-toolbar-trailing scrolls horizontally and holds every child at flex-shrink: 0', () => {
    const rule = chainSrc.match(/\.oct-toolbar-trailing\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-trailing rule').not.toBe('');
    expect(rule).toMatch(/overflow-x:\s*auto/);
    expect(rule).toMatch(/flex-wrap:\s*nowrap/);
    const childRule = chainSrc.match(/\.oct-toolbar-trailing\s*>\s*\*\s*\{[\s\S]*?\}/)?.[0] ?? '';
    expect(childRule, '.oct-toolbar-trailing > * rule').not.toBe('');
    expect(childRule).toMatch(/flex-shrink:\s*0/);
  });

  test('.rbq-select-trigger / InfoHint popups stay reachable under the new scroll container — only Select.svelte and InfoHint.svelte own in-place popups here, and both are position: fixed', () => {
    const selectSrc = readFileSync(path.resolve(DIR, '../src/lib/Select.svelte'), 'utf8');
    const infoHintSrc = readFileSync(path.resolve(DIR, '../src/lib/InfoHint.svelte'), 'utf8');
    expect(selectSrc).toMatch(/\.rbq-select-panel\s*\{[\s\S]*?position:\s*fixed/);
    expect(infoHintSrc).toMatch(/position:\s*fixed/);
  });
});

test.describe('Symbol+Expiry merge — SymbolPanel.svelte wiring', () => {
  let panelSrc = '';

  test.beforeAll(() => {
    panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
  });

  test('SymbolPanel defines a _chainSymbolPicker snippet with its OWN markup/class (.oes-sym-pick-chain), not a shared extraction of .oes-sym-pick', () => {
    const idx = panelSrc.indexOf('{#snippet _chainSymbolPicker()}');
    expect(idx, '_chainSymbolPicker snippet not found').toBeGreaterThan(-1);
    const endIdx = panelSrc.indexOf('{/snippet}', idx);
    expect(endIdx).toBeGreaterThan(idx);
    const body = panelSrc.slice(idx, endIdx);
    // 2026-11-26 chrome-merge fix added a `style:--sym-ch={...}` binding
    // on this same element (feeds the ch-based content-fit width calc —
    // see the dedicated tests for that below), so the tag no longer
    // closes immediately after the bare class attribute.
    expect(body).toContain('class="oes-sym-pick-chain"');
    expect(body).toContain('<SymbolSearchInput');
    // Deliberately NOT the `.oes-sym-pick` class — see the dedicated
    // test below for why the two need different CSS (opposite anchor
    // position in their respective rows).
    expect(body).not.toContain('class="oes-sym-pick"');
  });

  test('SymbolPanel threads the snippet into <OptionChainTab symbolPicker={_chainSymbolPicker}>', () => {
    const idx = panelSrc.indexOf('<OptionChainTab\n');
    expect(idx, 'real <OptionChainTab invocation tag not found').toBeGreaterThan(-1);
    const closeIdx = panelSrc.indexOf('\n        />', idx);
    expect(closeIdx).toBeGreaterThan(idx);
    const propsSlice = panelSrc.slice(idx, closeIdx);
    expect(propsSlice).toContain('symbolPicker={_chainSymbolPicker}');
  });

  test('.oes-picker keeps its own, separate inline .oes-sym-pick copy, suppressed specifically on Chain — Account + Exchange still render for every tab', () => {
    const pickerIdx = panelSrc.indexOf('<div class="oes-picker">');
    expect(pickerIdx).toBeGreaterThan(-1);
    const inlineSymIdx = panelSrc.indexOf('<div class="oes-sym-pick">', pickerIdx);
    expect(inlineSymIdx, 'inline .oes-sym-pick copy not found inside .oes-picker').toBeGreaterThan(pickerIdx);
    const guardIdx = panelSrc.lastIndexOf('{#if', inlineSymIdx);
    const guardLine = panelSrc.slice(guardIdx, panelSrc.indexOf('\n', guardIdx));
    expect(guardLine).toMatch(/_activeTab !== 'chain'/);

    // Account + Exchange selects are NOT behind that same guard — they
    // must still render unconditionally inside .oes-picker for Chain.
    const acctIdx = panelSrc.indexOf('class="oes-account-pick"', pickerIdx);
    const exchIdx = panelSrc.indexOf('class="oes-exch-filter"', pickerIdx);
    expect(acctIdx).toBeGreaterThan(pickerIdx);
    expect(acctIdx).toBeLessThan(inlineSymIdx);
    expect(exchIdx).toBeGreaterThan(pickerIdx);
    expect(exchIdx).toBeLessThan(inlineSymIdx);
  });

  test('.oes-chase-row (CHASE/DRAFT shared row) excludes Chain at its own OUTER gate — the dead empty row is removed, not just emptied', () => {
    const rowIdx = panelSrc.indexOf('<div class="oes-chase-row">');
    expect(rowIdx, '.oes-chase-row div not found').toBeGreaterThan(-1);
    const gateIdx = panelSrc.lastIndexOf('{#if', rowIdx);
    expect(gateIdx).toBeGreaterThan(-1);
    const gateLine = panelSrc.slice(gateIdx, panelSrc.indexOf('\n', gateIdx));
    expect(gateLine).toMatch(/_activeTab !== 'cli'/);
    expect(gateLine).toMatch(/_activeTab !== 'chart'/);
    expect(gateLine).toMatch(/_activeTab !== 'chain'/);
  });

  test('.oes-sym-pick / .ssi-drop RIGHT-anchor fix (mobile overflow, non-Chain tabs) is untouched', () => {
    expect(panelSrc).toMatch(/\.oes-sym-pick :global\(\.ssi-drop\)\s*\{\s*left:\s*auto;\s*right:\s*0;\s*\}/);
  });

  test('.oes-sym-pick-chain (Chain only) has NO right-anchor override — it stays at SymbolSearchInput\'s own default LEFT anchor, the opposite of .oes-sym-pick above', () => {
    // The Chain copy sits LEFTMOST in its row (the opposite position
    // from `.oes-sym-pick`, which is near the right edge of `.oes-
    // picker`) — reusing the right-anchor override here would clip the
    // dropdown against the modal's right edge instead of fixing it.
    expect(panelSrc).not.toMatch(/\.oes-sym-pick-chain :global\(\.ssi-drop\)/);
  });

  // 2026-11-26 chrome-merge fix — the operator reported the prior merge
  // still rendered as TWO visibly boxed controls (the Symbol input's own
  // border, the Expiry Select's own border) either side of literal "("/
  // ")" text, not one combined element. Fix: the single shared border/
  // background moves to the PARENT `.oct-sym-expiry` container
  // (OptionChainTab.svelte, see that file's own source-scan tests below),
  // and BOTH children strip their own chrome. The 6rem floor this test
  // used to pin on `.oes-sym-pick-chain` itself moves to `.ssi-input`
  // (the element that actually needs it) now that the wrapper no longer
  // forces a fixed 8rem flex-basis — see the ch-based width test below
  // for why.
  test('.oes-sym-pick-chain no longer forces a fixed flex-basis — it hugs the ch-sized .ssi-input instead (no dead-space gap once the border is gone)', () => {
    const rule = panelSrc.match(/\.oes-sym-pick-chain\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-sym-pick-chain rule').not.toBe('');
    expect(rule).toMatch(/flex:\s*0 0 auto/);
    expect(rule).not.toMatch(/flex:\s*1 1 8rem/);
    expect(rule).toMatch(/max-width:\s*20rem/);
  });

  test('the `--sym-ch` CSS var is set on the wrapper from the committed symbol length, feeding the ch-based width calc', () => {
    const idx = panelSrc.indexOf('{#snippet _chainSymbolPicker()}');
    const endIdx = panelSrc.indexOf('{/snippet}', idx);
    const body = panelSrc.slice(idx, endIdx);
    expect(body).toMatch(/style:--sym-ch=\{_localSymbol\.length \|\| 6\}/);
  });

  test('.ssi-input inside .oes-sym-pick-chain has NO border/background of its own — chrome lives on the shared chip only', () => {
    const rule = panelSrc.match(/\.oes-sym-pick-chain :global\(\.ssi-input\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-sym-pick-chain :global(.ssi-input) rule').not.toBe('');
    // Zeroed outright, not merely colour-transparent — see this rule's
    // own inline comment for why that distinction matters against
    // SymbolSearchInput's own :focus rule.
    expect(rule).toMatch(/border:\s*none/);
    expect(rule).toMatch(/background:\s*none/);
    // `(1ch + 0.04em)` per character, not a bare `1ch` — must include
    // this control's own `letter-spacing` or a long symbol clips (live
    // regression, confirmed with "BANKNIFTY" — see the dedicated live
    // test for it, and this rule's own `letter-spacing: 0.04em`
    // re-declaration just below, checked by the next assertion).
    expect(rule).toMatch(/width:\s*calc\(var\(--sym-ch,\s*6\)\s*\*\s*\(1ch \+ 0\.04em\)/);
    expect(rule).toMatch(/letter-spacing:\s*0\.04em/);
    // A small sanity floor only (3rem), not the pre-chrome-merge 6rem
    // fixed-flex-basis floor — 6rem left a visible dead-space gap once
    // this control's own border was removed (measured live — see the
    // "fits its own box at rest" live test below).
    expect(rule).toMatch(/min-width:\s*3rem/);
  });

  test('.ssi-input:focus is overridden too (specificity tie against SymbolSearchInput\'s own :focus rule) — stays chrome-free and widens for typing', () => {
    const rule = panelSrc.match(/\.oes-sym-pick-chain :global\(\.ssi-input:focus\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-sym-pick-chain :global(.ssi-input:focus) rule').not.toBe('');
    expect(rule).toMatch(/background:\s*none/);
    expect(rule).toMatch(/width:\s*11rem/);
  });
});

// ──────────────────────────────────────────────────────────────────
// Live — real browser, drives the actual merged Chain toolbar.
//
// Follows this suite's own established navigation precedent
// (`_seedNiftyAndOpenChain`, duplicated locally per existing convention —
// see chain_ticket_severance_and_mobile_fixes.spec.js /
// order_ticket_row_height_and_palette.spec.js, each of which carries its
// own copy rather than sharing one): /orders' inline SymbolPanel, type
// NIFTY (an index with NFO weekly options, so the Chain tab's own
// `chainDisabled` gate never fires), pick the suggestion, click Chain.
// ──────────────────────────────────────────────────────────────────

/**
 * @param {import('@playwright/test').Page} page
 * @param {string} [sym] defaults to NIFTY, the suite's own established
 *   index pick (NFO weekly options, never hits the Chain tab's
 *   `chainDisabled` gate). 2026-11-26 chrome-merge fix adds an optional
 *   override so the overflow/clipping tests below can drive a real
 *   multi-character root (e.g. "BANKNIFTY") through the same flow
 *   instead of duplicating it.
 */
async function _openNiftyChain(page, sym = 'NIFTY') {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill(sym);
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);

  const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
  await expect(chainTab).toBeEnabled({ timeout: 15_000 });
  await chainTab.click();
}

/**
 * Walks `.oct-sym-expiry` and every descendant inside it, and counts
 * how many elements actually paint visible chrome (a real border, or a
 * real background colour/image) at the moment this runs. 2026-11-26
 * chrome-merge fix: before this fix the Symbol input and the Expiry
 * Select trigger each painted their own border/background, so this
 * count was 3 (container + 2 children). The fix moves the ONE visible
 * border/background onto the container alone, so the count must be
 * exactly 1, and that one element must be `.oct-sym-expiry` itself —
 * not merely "fewer boxes than before".
 * @param {import('@playwright/test').Page} page
 */
async function _chainChromeElements(page) {
  return page.evaluate(() => {
    const root = document.querySelector('.oct-sym-expiry');
    if (!root) return null;
    const all = [root, ...root.querySelectorAll('*')];
    const chromed = [];
    for (const el of all) {
      // `.ssi-drop` (the suggestion dropdown) legitimately opens the
      // instant the symbol input is focused, even with nothing typed
      // (SymbolSearchInput's own `onfocus` sets `_symOpen = true`), and
      // it legitimately has its own box/border — that is a normal,
      // expected popup/overlay, not a second box competing with the
      // "SYMBOL (EXPIRY)" chip this test is about. Exclude it and its
      // descendants from the count.
      if (el.closest('.ssi-drop')) continue;
      const cs = getComputedStyle(el);
      const borderVisible = ['Top', 'Right', 'Bottom', 'Left'].some((side) => {
        const w = parseFloat(cs[`border${side}Width`]) || 0;
        if (w <= 0 || cs[`border${side}Style`] === 'none') return false;
        const m = cs[`border${side}Color`].match(/rgba?\(([^)]+)\)/);
        if (!m) return true;
        const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
        const alpha = parts.length === 4 ? parts[3] : 1;
        return alpha > 0;
      });
      const bg = cs.backgroundColor;
      const bgm = bg.match(/rgba?\(([^)]+)\)/);
      const bgParts = bgm ? bgm[1].split(',').map((s) => parseFloat(s.trim())) : [];
      const bgAlpha = bgParts.length === 4 ? bgParts[3] : (bgm ? 1 : 0);
      const backgroundVisible = bgAlpha > 0 || (cs.backgroundImage && cs.backgroundImage !== 'none');
      if (borderVisible || backgroundVisible) {
        chromed.push({
          tag: el.tagName,
          cls: el.className,
          border: borderVisible,
          background: backgroundVisible,
        });
      }
    }
    return { chromed, isRootChromed: chromed.length === 1 && chromed[0].cls === root.className };
  });
}

/**
 * AMENDED 2026-11-26 (operator: "in chain keep element after symbol
 * expiry in the next row") — `.oct-toolbar-trailing` is now forced onto
 * its own row below `.oct-sym-expiry`, so this helper no longer measures
 * "does everything share one row" (it no longer does, by design).
 * It now splits the toolbar's controls into row-1 candidates
 * (`.oes-sym-pick-chain`, `.oct-expiry-pick` — both live INSIDE
 * `.oct-sym-expiry`) and row-2 candidates (every direct child of
 * `.oct-toolbar-trailing`: DTE chip through DRAFT), and reports both
 * groups' own mutual vertical overlap plus each group's own top/bottom
 * bounds — so a test can assert "row 1 is one row, row 2 is one row,
 * and row 2 sits entirely below row 1" instead of "everything is one
 * row". Still measures mutual vertical overlap (max(top) < min(bottom))
 * rather than equal Y, for the same `align-items: center` reason as
 * before — different-height siblings can still share one visual row.
 * @param {import('@playwright/test').Page} page
 */
async function _toolbarRowOverlap(page) {
  return page.evaluate(() => {
    const toolbar = document.querySelector('.oct-toolbar');
    if (!toolbar) return null;
    const row1Sel = ['.oes-sym-pick-chain', '.oct-expiry-pick'];
    const row2Sel = [
      '.oct-expiry-dte', '.oct-spread-field', '.oct-hedge-toggle',
      '.oes-chase-cluster', '.ot-draft-toggle',
    ];
    const boxOf = (sel) => {
      const el = toolbar.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return (r.width > 0 && r.height > 0) ? { sel, top: r.top, bottom: r.bottom } : null;
    };
    const row1Boxes = row1Sel.map(boxOf).filter(Boolean);
    const row2Boxes = row2Sel.map(boxOf).filter(Boolean);
    const toolbarBox = toolbar.getBoundingClientRect();
    return {
      row1Boxes,
      row2Boxes,
      toolbarHeight: toolbarBox.height,
      overflowsX: toolbar.scrollWidth > toolbar.clientWidth + 1,
    };
  });
}

/**
 * Confirms the element's own centre point actually resolves (via
 * `document.elementFromPoint`) to that element or one of its own
 * descendants — not to a clipping ancestor or an unrelated element
 * painted over it. `toBeVisible()` alone does not catch this: a
 * `position: absolute` popup clipped by an `overflow: hidden`/`auto`
 * ancestor can still report non-zero size and "visible" per Playwright's
 * own actionability check in some cases, while the actual click lands
 * elsewhere.
 * @param {import('@playwright/test').Locator} locator
 */
async function _assertHitTestsSelf(locator) {
  const result = await locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    return { matched: !!hit && (hit === el || el.contains(hit) || hit.contains(el)) };
  });
  expect(result.matched, 'element centre point did not hit-test to itself — likely clipped by an ancestor').toBe(true);
}

for (const [label, viewport] of [
  ['desktop (1440px)', { width: 1440, height: 900 }],
  ['mobile (370px)', { width: 370, height: 800 }],
]) {
  test.describe(`Live: Chain toolbar — "SYMBOL (EXPIRY)" row 1, trailing cluster row 2, at ${label}`, () => {
    test.use({ viewport });

    test('Symbol and Expiry render as one combined "SYMBOL (EXPIRY)" element inside .oct-sym-expiry', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const symExpiry = page.locator('.oct-sym-expiry').first();
      await expect(symExpiry).toBeVisible({ timeout: 15_000 });

      // Both controls present in the ONE shared container.
      const symInput = symExpiry.locator('.ssi-input').first();
      const expiryPick = symExpiry.locator('.oct-expiry-pick').first();
      await expect(symInput).toBeVisible({ timeout: 10_000 });
      await expect(expiryPick).toBeVisible({ timeout: 10_000 });
      await expect(symInput).toHaveValue(/NIFTY/i);

      // "SYMBOL (EXPIRY)" shape — the parens are real text nodes
      // (SymbolSearchInput's own value lives in the <input> element, not
      // in textContent, so the literal shape is asserted by combining
      // the input's value with the container's own paren/label text).
      const text = (await symExpiry.textContent())?.trim() ?? '';
      expect(text, '.oct-sym-expiry text content').toMatch(/\(.*\)/);
      const combined = `${await symInput.inputValue()} ${text}`;
      expect(combined).toMatch(/NIFTY/i);
      expect(combined).toMatch(/\(/);
      expect(combined).toMatch(/\)/);
    });

    // 2026-11-26 chrome-merge fix — the prior "merge" put the Symbol
    // input and the Expiry Select side by side in one row/DOM unit, but
    // each still painted its OWN border/background, so it still visibly
    // READ as two boxed controls either side of plain "("/")" text. The
    // operator rejected that as not "one element". This is the test
    // that actually pins the fix: exactly ONE painted box around the
    // whole "SYMBOL (EXPIRY)" unit, and it must be the container.
    test('exactly ONE element inside .oct-sym-expiry paints visible chrome (border/background), and it is the container itself', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });
      // A true at-rest state — no control focused.
      await page.evaluate(() => { (document.activeElement)?.blur?.(); });

      const info = await _chainChromeElements(page);
      expect(info, 'chrome-element scan').toBeTruthy();
      expect(info.chromed.length, `chromed elements: ${JSON.stringify(info.chromed)}`).toBe(1);
      expect(info.isRootChromed, `the one chromed element must be .oct-sym-expiry itself: ${JSON.stringify(info.chromed)}`).toBe(true);
    });

    test('focusing the Symbol input does not repaint a second box — SymbolSearchInput\'s own :focus chrome stays overridden', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });
      const symInput = page.locator('.oct-sym-expiry .ssi-input').first();
      await symInput.click();
      await expect(symInput).toBeFocused();

      const info = await _chainChromeElements(page);
      expect(info, 'chrome-element scan (focused)').toBeTruthy();
      expect(info.chromed.length, `chromed elements while focused: ${JSON.stringify(info.chromed)}`).toBe(1);
      expect(info.isRootChromed, `the one chromed element while focused must still be .oct-sym-expiry: ${JSON.stringify(info.chromed)}`).toBe(true);
    });

    test('the symbol text fits its own box at rest — no large dead-space gap between the committed value and "("', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);
      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });
      await page.evaluate(() => { (document.activeElement)?.blur?.(); });

      const metrics = await page.evaluate(() => {
        const input = document.querySelector('.oct-sym-expiry .ssi-input');
        if (!input) return null;
        const cs = getComputedStyle(input);
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
        const rawTextWidth = ctx.measureText(input.value || '').width;
        // canvas measureText does NOT apply CSS letter-spacing — add it
        // back per character (the live, 9-char "BANKNIFTY" case below is
        // the real regression this omission caused: a bare `ch`-count
        // calc with no letter-spacing term clipped the input's own text).
        const letterSpacingPx = parseFloat(cs.letterSpacing) || 0;
        const len = (input.value || '').length;
        const textWidth = rawTextWidth + len * letterSpacingPx;
        return { clientWidth: input.clientWidth, textWidth, scrollWidth: input.scrollWidth };
      });
      expect(metrics, 'symbol input metrics').toBeTruthy();
      // Not clipped — the whole value fits inside the box.
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
      // Not a huge unused gap either — this is a loose bound (not
      // pixel-exact: ch-unit sizing is an approximation of a
      // variable-width font, not a canvas measurement), just enough to
      // catch the old fixed-8rem-box-regardless-of-content shape.
      const leftover = metrics.clientWidth - metrics.textWidth;
      expect(leftover, `leftover space inside the symbol box: ${leftover.toFixed(1)}px (clientWidth=${metrics.clientWidth}, textWidth=${metrics.textWidth.toFixed(1)})`)
        .toBeLessThan(40);
    });

    // 2026-11-26 — a 5-char symbol (NIFTY) never exercised the real
    // ch-based width calc: it was always governed by the 3rem/6rem
    // floor, so it could never surface a clipping regression. A 9-char
    // root ("BANKNIFTY") does, and was used to find a real bug live:
    // the width calc omitted CSS `letter-spacing`, clipping the input's
    // own text (confirmed via `scrollWidth` > `clientWidth` before the
    // fix). `scrollLeft` is the more robust clipping probe `clientWidth`
    // vs. `scrollWidth` comparisons on an `<input>` can be unreliable
    // for in some browsers/zoom levels — forcing `scrollLeft` past the
    // content and reading it back to 0 proves there is nothing left to
    // scroll to, i.e. the full value is visible with no clipped tail.
    test('a 9-character symbol ("BANKNIFTY") is not clipped inside the input, and does not overflow the shared chip\'s own border', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page, 'BANKNIFTY');
      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });
      await page.evaluate(() => { (document.activeElement)?.blur?.(); });

      const info = await page.evaluate(() => {
        const chip = document.querySelector('.oct-sym-expiry');
        const input = document.querySelector('.oct-sym-expiry .ssi-input');
        input.scrollLeft = 1e4;
        const scrollLeftAfterForce = input.scrollLeft;
        return {
          value: input.value,
          inputScrollWidth: input.scrollWidth,
          inputClientWidth: input.clientWidth,
          scrollLeftAfterForce,
          chipScrollWidth: chip.scrollWidth,
          chipClientWidth: chip.clientWidth,
        };
      });
      expect(info.value, 'the picked symbol').toMatch(/BANKNIFTY/i);
      // Nothing to scroll to — the whole value is visible, none of it
      // clipped past the input's own right edge.
      expect(info.scrollLeftAfterForce, `input.scrollLeft after forcing it to 10000: ${JSON.stringify(info)}`).toBe(0);
      expect(info.inputScrollWidth, `input scrollWidth vs clientWidth: ${JSON.stringify(info)}`)
        .toBeLessThanOrEqual(info.inputClientWidth + 1);
      // The chip itself must never be compressed smaller than what its
      // own children need — that would spill the expiry text and ")"
      // outside the drawn border instead (confirmed live before this
      // fix: 370px mobile viewport, chip compressed to 130px while its
      // children needed 186px).
      expect(info.chipScrollWidth, `chip scrollWidth vs clientWidth: ${JSON.stringify(info)}`)
        .toBeLessThanOrEqual(info.chipClientWidth + 1);
    });

    test('the merged chip\'s height is within ~2px of the DTE chip (same control-height tier, not an oversized box)', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);
      await expect(page.locator('.oct-expiry-dte')).toBeVisible({ timeout: 15_000 });

      const chipBox = await page.locator('.oct-sym-expiry').first().boundingBox();
      const dteBox = await page.locator('.oct-expiry-dte').first().boundingBox();
      expect(chipBox, '.oct-sym-expiry box').toBeTruthy();
      expect(dteBox, '.oct-expiry-dte box').toBeTruthy();
      expect(Math.abs(chipBox.height - dteBox.height), `chip height ${chipBox.height} vs DTE chip height ${dteBox.height}`)
        .toBeLessThan(3);
    });

    // AMENDED 2026-11-26 (operator: "in chain keep element after symbol
    // expiry in the next row") — this used to pin "everything shares one
    // row". It now pins the opposite, deliberate shape: `.oct-sym-expiry`
    // (row 1) and `.oct-toolbar-trailing` (row 2, DTE chip through DRAFT)
    // are each internally a single row, and row 2 sits entirely BELOW
    // row 1 — never overlapping it, never above it.
    test('"SYMBOL (EXPIRY)" is row 1 and the trailing cluster (DTE→DRAFT) is row 2, directly below it — not a single shared row', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      await expect(page.locator('.oct-toolbar')).toBeVisible({ timeout: 15_000 });
      // Give chain quotes a moment to resolve an expiry so .oct-expiry-pick
      // (and therefore the parens) are present too, not just the symbol.
      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });

      const info = await _toolbarRowOverlap(page);
      expect(info, '.oct-toolbar overlap info').toBeTruthy();
      expect(info.row1Boxes.length, 'row 1 (Symbol+Expiry) controls must be visible').toBeGreaterThanOrEqual(1);
      expect(info.row2Boxes.length, 'row 2 (trailing cluster) controls must be visible').toBeGreaterThanOrEqual(1);

      // Row 1's own controls mutually overlap — they're one visual row.
      const row1MaxTop = Math.max(...info.row1Boxes.map((b) => b.top));
      const row1MinBottom = Math.min(...info.row1Boxes.map((b) => b.bottom));
      expect(row1MaxTop, `row 1 controls do not share one row: ${JSON.stringify(info.row1Boxes)}`)
        .toBeLessThan(row1MinBottom);

      // Row 2's own controls mutually overlap — they're one visual row too.
      const row2MaxTop = Math.max(...info.row2Boxes.map((b) => b.top));
      const row2MinBottom = Math.min(...info.row2Boxes.map((b) => b.bottom));
      expect(row2MaxTop, `row 2 controls do not share one row: ${JSON.stringify(info.row2Boxes)}`)
        .toBeLessThan(row2MinBottom);

      // The two rows do NOT share vertical space — row 2 starts at or
      // after row 1 ends. This is the actual "own row" assertion.
      expect(row2MaxTop, `row 2 (top=${row2MaxTop}) overlaps row 1 (bottom=${row1MinBottom}) instead of sitting below it`)
        .toBeGreaterThanOrEqual(row1MinBottom - 1);

      // The combined toolbar height now tracks TWO control-height tiers
      // (~1.4rem each) plus the inter-row gap, not one — the inverse of
      // the old single-row ceiling this test used to assert.
      expect(info.toolbarHeight).toBeGreaterThan(30);

      // "DRAFT stays left-aligned" is preserved across the row break —
      // row 2's own left edge should sit close to row 1's left edge
      // (both anchored to the toolbar's own left padding), not drift
      // rightward the way a dead-space gap on a SHARED row used to risk.
      const symExpiryBox = await page.locator('.oct-sym-expiry').first().boundingBox();
      const trailingBox = await page.locator('.oct-toolbar-trailing').first().boundingBox();
      expect(symExpiryBox, '.oct-sym-expiry box').toBeTruthy();
      expect(trailingBox, '.oct-toolbar-trailing box').toBeTruthy();
      const leftDrift = Math.abs(trailingBox.x - symExpiryBox.x);
      expect(leftDrift, `row 2's left edge (${trailingBox.x}px) drifted from row 1's left edge (${symExpiryBox.x}px) by ${leftDrift.toFixed(1)}px`)
        .toBeLessThan(4);
    });

    test('both the Symbol search and the Expiry dropdown remain independently clickable/functional after the merge', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      // Expiry dropdown — open it, pick an option (or close it again if
      // there's only one), confirm it's a real, usable control.
      const expiryTrigger = page.locator('.oct-expiry-pick .rbq-select-trigger').first();
      await expect(expiryTrigger).toBeVisible({ timeout: 15_000 });
      const beforeLabel = (await expiryTrigger.textContent())?.trim();
      await expiryTrigger.click();
      const panel = page.locator('.oct-expiry-pick .rbq-select-panel').first();
      await expect(panel).toBeVisible({ timeout: 5000 });
      const options = panel.locator('.rbq-select-option');
      const optCount = await options.count();
      expect(optCount, 'Expiry picker must expose at least one real option').toBeGreaterThan(0);
      // Hit-test, not just toBeVisible() — Select's panel is `position:
      // fixed`, so it can't be clipped by an ancestor's overflow, but
      // this confirms the real pixel the operator would click actually
      // resolves to the option, not to something else painted over it.
      await _assertHitTestsSelf(options.first());
      if (optCount > 1) {
        await options.nth(1).click();
        await expect(panel).toBeHidden({ timeout: 5000 });
        const afterLabel = (await expiryTrigger.textContent())?.trim();
        expect(afterLabel).not.toBe(beforeLabel);
      } else {
        await page.keyboard.press('Escape');
      }

      // Symbol search — still a live, independent control: typing opens
      // its own suggestion dropdown and picking one swaps the value.
      const symInput = page.locator('.oct-sym-expiry .ssi-input').first();
      await expect(symInput).toBeVisible({ timeout: 10_000 });
      await symInput.fill('BANKNIFTY');
      const sugg = page.locator('.oct-sym-expiry .ssi-drop .ssi-row').first();
      await expect(sugg).toBeVisible({ timeout: 10_000 });
      // Hit-test — `.ssi-drop` is `position: absolute` (NOT fixed), so
      // this is the one popup in the merged row that genuinely could be
      // clipped by an ancestor's overflow (the risk this merge's own
      // `.oct-sym-expiry`/`.oct-toolbar-trailing` CSS comments call out
      // explicitly). Confirms the real pixel resolves to the row, not to
      // a clipping ancestor or another element painted over it.
      await _assertHitTestsSelf(sugg);
      await sugg.click({ force: true });
      await expect(symInput).toHaveValue(/BANKNIFTY/i, { timeout: 10_000 });
    });
  });
}
