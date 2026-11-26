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
 *   - `.oct-toolbar` never wraps (`flex-wrap: nowrap`, was `wrap`):
 *     `.oct-sym-expiry` is the one flexible/shrinkable half of the row
 *     (reuses `.oes-sym-pick`'s own existing `flex: 1 1 0; min-width:
 *     0;`), and `.oct-toolbar-trailing` (DTE chip through DRAFT) holds
 *     every fixed-size control at `flex-shrink: 0`, scrolling
 *     horizontally (`overflow-x: auto`) instead of squeezing any one
 *     of them illegible — same "shrink the flexible element, scroll
 *     the rest, never wrap" convention already used for the page
 *     header (AlgoTimestamp). This makes the single-row outcome a
 *     structural guarantee (enforced by flexbox layout), not merely a
 *     coincidence of one or two tested viewport widths.
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
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');

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

  test('.oct-toolbar is flex-wrap: nowrap (was wrap) — the row never wraps to a second line', () => {
    const rule = chainSrc.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    expect(rule).toMatch(/flex-wrap:\s*nowrap/);
    expect(rule).not.toMatch(/flex-wrap:\s*wrap;/);
  });

  test('.oct-toolbar still scopes --ctl-h and the same 0.4rem left-padding convention (pre-existing invariants preserved)', () => {
    const rule = chainSrc.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule).toMatch(/--ctl-h:\s*1\.4rem/);
    expect(rule).toMatch(/padding:\s*0\.25rem 0\.4rem 0\.35rem/);
    expect(rule).not.toMatch(/\n\s*border-bottom:\s*\S/);
  });

  test('.oct-sym-expiry shrinks but never grows (flex: 0 1 auto) — no dead space drifting the trailing cluster right', () => {
    const rule = chainSrc.match(/\.oct-sym-expiry\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-sym-expiry rule').not.toBe('');
    // flex-grow MUST be 0 — growing this box on a wide viewport would
    // leave dead space inside it (neither the Symbol snippet nor the
    // Expiry picker grows to fill it), visually pushing DTE→DRAFT
    // rightward and undoing the "DRAFT stays left-aligned" decision.
    expect(rule).toMatch(/flex:\s*0 1 auto/);
    expect(rule).not.toMatch(/flex:\s*1 1 auto/);
    expect(rule).toMatch(/min-width:\s*0/);
    // No `overflow: hidden`/`auto` here — SymbolSearchInput's own results
    // dropdown is `position: absolute` relative to its wrapper, not
    // `fixed`, so clipping this container would clip that dropdown too.
    expect(rule).not.toMatch(/overflow:/);
  });

  test('.oct-toolbar-trailing is also flex: 0 1 auto (no grow) — the whole row never fills width by expanding a middle gap', () => {
    const rule = chainSrc.match(/\.oct-toolbar-trailing\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-trailing rule').not.toBe('');
    expect(rule).toMatch(/flex:\s*0 1 auto/);
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
    expect(body).toContain('<div class="oes-sym-pick-chain">');
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

  test('.oes-sym-pick-chain has a real min-width floor (not 0) so the symbol input stays legible once its parent has shrunk as far as it can', () => {
    const rule = panelSrc.match(/\.oes-sym-pick-chain\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-sym-pick-chain rule').not.toBe('');
    expect(rule).toMatch(/min-width:\s*6rem/);
    expect(rule).not.toMatch(/min-width:\s*0/);
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

/** @param {import('@playwright/test').Page} page */
async function _openNiftyChain(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);

  const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
  await expect(chainTab).toBeEnabled({ timeout: 15_000 });
  await chainTab.click();
}

/**
 * Every visible, non-zero-size direct-descendant-ish control inside
 * `.oct-toolbar` that the operator can see at once. Measures mutual
 * vertical overlap (max(top) < min(bottom)) rather than equal Y —
 * `align-items: center` legitimately gives shorter/taller children
 * different `top` values while still sharing one visual row.
 * @param {import('@playwright/test').Page} page
 */
async function _toolbarRowOverlap(page) {
  return page.evaluate(() => {
    const toolbar = document.querySelector('.oct-toolbar');
    if (!toolbar) return null;
    const candidates = [
      '.oes-sym-pick-chain', '.oct-expiry-pick', '.oct-expiry-dte',
      '.oct-spread-field', '.oct-hedge-toggle', '.oes-chase-cluster',
      '.ot-draft-toggle',
    ];
    const boxes = [];
    for (const sel of candidates) {
      const el = toolbar.querySelector(sel);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) boxes.push({ sel, top: r.top, bottom: r.bottom });
    }
    const toolbarBox = toolbar.getBoundingClientRect();
    return {
      boxes,
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
  test.describe(`Live: Chain toolbar is a single row at ${label}`, () => {
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

    test('the toolbar occupies exactly one visible row — every visible control vertically overlaps', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      await expect(page.locator('.oct-toolbar')).toBeVisible({ timeout: 15_000 });
      // Give chain quotes a moment to resolve an expiry so .oct-expiry-pick
      // (and therefore the parens) are present too, not just the symbol.
      await expect(page.locator('.oct-expiry-pick')).toBeVisible({ timeout: 15_000 });

      const info = await _toolbarRowOverlap(page);
      expect(info, '.oct-toolbar overlap info').toBeTruthy();
      expect(info.boxes.length, 'at least the symbol + expiry controls must be visible').toBeGreaterThanOrEqual(2);

      const maxTop = Math.max(...info.boxes.map((b) => b.top));
      const minBottom = Math.min(...info.boxes.map((b) => b.bottom));
      expect(maxTop, `toolbar controls do not share one row: ${JSON.stringify(info.boxes)}`)
        .toBeLessThan(minBottom);

      // A real single-row guarantee, not an accident of exactly-fitting
      // content — the toolbar's own height should track one control's
      // height (~1.4rem tier), never ~2x it (which would mean a second
      // visible row silently formed).
      expect(info.toolbarHeight).toBeLessThan(60);

      // No dead-space drift — `.oct-sym-expiry` must never grow to
      // absorb leftover row width (which would open a visible gap
      // between the closing ")" and the DTE chip, pushing the whole
      // trailing cluster including DRAFT toward the right edge).
      const symExpiryBox = await page.locator('.oct-sym-expiry').first().boundingBox();
      const trailingBox = await page.locator('.oct-toolbar-trailing').first().boundingBox();
      expect(symExpiryBox, '.oct-sym-expiry box').toBeTruthy();
      expect(trailingBox, '.oct-toolbar-trailing box').toBeTruthy();
      const gap = trailingBox.x - (symExpiryBox.x + symExpiryBox.width);
      expect(gap, `gap between Symbol(Expiry) and the trailing cluster: ${gap.toFixed(1)}px`)
        .toBeLessThan(32);
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
