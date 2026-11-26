/**
 * chain_symbol_expiry_merge.spec.js
 *
 * AMENDED 2026-11-26 — this file previously covered the 2026-11-25/26
 * "SYMBOL (EXPIRY)" merge (commits ee946c28, 8abacf3b): the Chain tab's
 * Symbol control (SymbolPanel.svelte's `.oes-picker`) and its own Expiry
 * picker (OptionChainTab.svelte's `.oct-toolbar`) combined into one
 * visual element. That merge is REVERTED here, per a later operator
 * instruction: "revert chain symbol and expiry elements changes and
 * make them two elements in different rows like before." The filename
 * is kept (per the task that drove this revert) even though the merge
 * it originally named no longer exists — this file now guards the
 * REVERTED, two-separate-elements shape instead.
 *
 * What changed back:
 *   - SymbolPanel.svelte's `{#snippet _chainSymbolPicker()}`, its
 *     `symbolPicker={_chainSymbolPicker}` pass-through to
 *     `<OptionChainTab>`, and the `.oes-sym-pick-chain` CSS (including
 *     the 2026-11-26 chrome-merge/`--sym-ch` content-fit-width follow-up
 *     fix) are all deleted. `.oes-sym-pick` (Symbol) is unconditional
 *     again — it renders inside `.oes-picker` for EVERY tab, Chain
 *     included, exactly as it did before `ee946c28`.
 *   - OptionChainTab.svelte's `symbolPicker` prop, `.oct-sym-expiry`
 *     wrapper, `.oct-expiry-paren` parens, and the Select-chrome-strip
 *     override (`.oct-expiry-pick :global(.rbq-select-trigger)`) are
 *     all deleted. The Expiry row is back to a plain `.oct-toolbar-label`
 *     ("Expiry") span + `.oct-expiry-pick` Select, with the Select's own
 *     chrome restored (`.oct-toolbar :global(.rbq-select-trigger) {
 *     padding-block: 0.15rem; }`), and `.oct-expiry-pick`'s min-width
 *     restored to its pre-merge `7.7rem`.
 *   - `.oct-toolbar-trailing` (DTE chip through DRAFT) stays on its OWN
 *     row below the Expiry row — this is commit 916389fb, a separate,
 *     later, independent operator instruction ("in chain keep element
 *     after symbol expiry in the next row") that is unaffected by this
 *     revert and reads correctly against the reverted shape too. See
 *     `chain_toolbar_trailing_own_row.spec.js` for its own dedicated
 *     coverage — this file does not duplicate it.
 *   - `.oes-chase-row`'s outer `_activeTab !== 'chain'` gate (the
 *     "remove the dead empty row" fix, also from `ee946c28`) is KEPT —
 *     it is an unrelated, correct fix for CHASE/DRAFT, not part of the
 *     Symbol+Expiry visual merge this revert targets. This file is
 *     therefore not a literal byte-for-byte restore of the pre-
 *     `ee946c28` state; it restores the Symbol/Expiry split only.
 *   - `_daysToExpiry()`'s DST-aware MCX close-time fix (`8abacf3b`,
 *     `resolveExpiryCloseDateTime`/`MCX_COMMODITIES`) is UNTOUCHED by
 *     this revert — it is a real bugfix unrelated to layout, and is
 *     re-asserted below so a future edit can't silently drop it while
 *     touching this same area again.
 *
 * Resulting Chain-tab row order: `[Account | Exchange | Symbol]`
 * (`.oes-picker`, shared by every tab) → `[Expiry ▾]` (`.oct-toolbar`
 * row 1) → `[DTE | Spread% | HEDGE | CHASE | DRAFT]` (`.oct-toolbar`
 * row 2, `.oct-toolbar-trailing`).
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
// Source-scan — the merge markup/CSS is gone; the pre-merge shape is back
// ──────────────────────────────────────────────────────────────────

test.describe('Symbol+Expiry merge reverted — OptionChainTab.svelte structure', () => {
  let chainSrc = '';

  test.beforeAll(() => {
    chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('no symbolPicker prop, .oct-sym-expiry wrapper, or .oct-expiry-paren markup/CSS remains', () => {
    // A bare historical mention of the retired mechanism's name, inside
    // a code comment explaining the revert, is fine (and expected) —
    // this checks there is no live PROP declaration, destructured
    // default, or markup/CSS selector left behind.
    expect(chainSrc).not.toMatch(/symbolPicker\s*\?:/);
    expect(chainSrc).not.toMatch(/symbolPicker\s*=\s*\/\*\*/);
    expect(chainSrc).not.toMatch(/\{#if symbolPicker/);
    expect(chainSrc).not.toMatch(/\{@render symbolPicker/);
    expect(chainSrc).not.toMatch(/\.oct-sym-expiry/);
    expect(chainSrc).not.toMatch(/oct-expiry-paren/);
  });

  test('the Expiry row gate is back to its pre-merge form: chainUnderlying && chainExpiries.length only', () => {
    const idx = chainSrc.indexOf('{#if chainUnderlying && chainExpiries.length}');
    expect(idx, 'pre-merge toolbar gate not found').toBeGreaterThan(-1);
    const toolbarIdx = chainSrc.indexOf('<div class="oct-toolbar">', idx);
    expect(toolbarIdx).toBeGreaterThan(idx);
    // The gate text itself must not carry the old `symbolPicker ||` prefix.
    const gateLine = chainSrc.slice(idx, chainSrc.indexOf('\n', idx));
    expect(gateLine).not.toMatch(/symbolPicker/);
  });

  test('.oct-toolbar renders a plain "Expiry" label, then the Select, with no parens in between', () => {
    const toolbarIdx = chainSrc.indexOf('<div class="oct-toolbar">');
    expect(toolbarIdx).toBeGreaterThan(-1);
    const labelIdx = chainSrc.indexOf('<span class="oct-toolbar-label">Expiry</span>', toolbarIdx);
    const pickIdx = chainSrc.indexOf('<div class="oct-expiry-pick">', toolbarIdx);
    const selectIdx = chainSrc.indexOf('<Select', pickIdx);
    const trailingIdx = chainSrc.indexOf('<div class="oct-toolbar-trailing">', toolbarIdx);
    expect(labelIdx, 'plain "Expiry" label not found').toBeGreaterThan(toolbarIdx);
    expect(pickIdx, '.oct-expiry-pick not found').toBeGreaterThan(labelIdx);
    expect(selectIdx, 'Expiry Select not found').toBeGreaterThan(pickIdx);
    expect(trailingIdx, '.oct-toolbar-trailing not found').toBeGreaterThan(selectIdx);
    // No literal paren characters between the label and the trailing row.
    const between = chainSrc.slice(labelIdx, trailingIdx);
    expect(between).not.toMatch(/aria-hidden="true">\(/);
    expect(between).not.toMatch(/aria-hidden="true">\)/);
  });

  test('DTE chip, Spread%, Hedge, Chase and Draft all still render inside .oct-toolbar-trailing', () => {
    const trailingIdx = chainSrc.indexOf('<div class="oct-toolbar-trailing">');
    expect(trailingIdx).toBeGreaterThan(-1);
    for (const needle of [
      'class="oct-expiry-dte"',
      'class="oct-spread-field"',
      'class="oct-hedge-toggle"',
      'class="oes-chase-cluster"',
      'class="ot-draft-toggle"',
    ]) {
      const i = chainSrc.indexOf(needle, trailingIdx);
      expect(i, `${needle} not found inside .oct-toolbar-trailing`).toBeGreaterThan(trailingIdx);
    }
  });

  test('the Expiry Select has its own chrome back — no chrome-strip override on .oct-expiry-pick remains', () => {
    expect(chainSrc).not.toMatch(/\.oct-expiry-pick :global\(\.rbq-select-trigger\)/);
    // The original, un-scoped toolbar-wide padding-block override is back.
    const rule = chainSrc.match(/\.oct-toolbar :global\(\.rbq-select-trigger\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar :global(.rbq-select-trigger) rule').not.toBe('');
    expect(rule).toMatch(/padding-block:\s*0\.15rem/);
  });

  test('.oct-expiry-pick min-width is restored to its pre-merge 7.7rem', () => {
    const rule = chainSrc.match(/\.oct-expiry-pick\s*\{[\s\S]*?\}/)?.[0] ?? '';
    expect(rule, '.oct-expiry-pick rule').not.toBe('');
    expect(rule).toMatch(/min-width:\s*7\.7rem/);
    expect(rule).not.toMatch(/min-width:\s*5rem/);
  });

  // 8abacf3b's DST-aware MCX close-time fix is unrelated to the layout
  // merge and must survive this revert untouched.
  test('_daysToExpiry() still resolves the real per-exchange close time (DST-aware), not a flat NSE 15:30', () => {
    expect(chainSrc).not.toMatch(/T15:30:00\+05:30/);
    expect(chainSrc).toMatch(/import \{ KITE_INDEX_QUOTE_KEY_TO_ROOT as _KITE_IDX_TO_ROOT, MCX_COMMODITIES \} from '\$lib\/data\/resolveUnderlying\.js';/);
    expect(chainSrc).toMatch(/import \{ resolveExpiryCloseDateTime \} from '\$lib\/data\/mcxCloseTime\.js';/);
    const fnIdx = chainSrc.indexOf('function _daysToExpiry(');
    expect(fnIdx, '_daysToExpiry function not found').toBeGreaterThan(-1);
    const fnBody = chainSrc.slice(fnIdx, chainSrc.indexOf('\n  }', fnIdx));
    expect(fnBody).toMatch(/MCX_COMMODITIES\.has\(/);
    expect(fnBody).toMatch(/resolveExpiryCloseDateTime\(iso, isMcx\)/);
  });

  test('.oct-toolbar still scopes --ctl-h and the same 0.4rem left-padding convention (pre-existing invariants preserved)', () => {
    const rule = chainSrc.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule).toMatch(/--ctl-h:\s*1\.4rem/);
    expect(rule).toMatch(/padding:\s*0\.25rem 0\.4rem 0\.35rem/);
  });
});

test.describe('Symbol+Expiry merge reverted — SymbolPanel.svelte wiring', () => {
  let panelSrc = '';

  test.beforeAll(() => {
    panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
  });

  test('no _chainSymbolPicker snippet, symbolPicker pass-through, or .oes-sym-pick-chain CSS remains', () => {
    expect(panelSrc).not.toMatch(/_chainSymbolPicker/);
    expect(panelSrc).not.toMatch(/symbolPicker/);
    expect(panelSrc).not.toMatch(/oes-sym-pick-chain/);
    expect(panelSrc).not.toMatch(/--sym-ch/);
  });

  test('.oes-sym-pick (Symbol) renders unconditionally inside .oes-picker — no Chain-tab exclusion', () => {
    const pickerIdx = panelSrc.indexOf('<div class="oes-picker">');
    expect(pickerIdx).toBeGreaterThan(-1);
    const symIdx = panelSrc.indexOf('<div class="oes-sym-pick">', pickerIdx);
    expect(symIdx, '.oes-sym-pick not found inside .oes-picker').toBeGreaterThan(pickerIdx);
    // No Chain-specific {#if} guard sits directly around this div any
    // more — the nearest preceding {#if} (the account-list branch just
    // above it) must not mention 'chain', and there is no second,
    // Chain-only {#if} between it and the div's own opening tag.
    const guardIdx = panelSrc.lastIndexOf('{#if', symIdx);
    const guardLine = panelSrc.slice(guardIdx, panelSrc.indexOf('\n', guardIdx));
    expect(guardLine).not.toMatch(/_activeTab !== 'chain'/);
    const between = panelSrc.slice(pickerIdx, symIdx);
    expect(between).not.toMatch(/_activeTab !== 'chain'/);

    // Account + Exchange still render inside .oes-picker too.
    const acctIdx = panelSrc.indexOf('class="oes-account-pick"', pickerIdx);
    const exchIdx = panelSrc.indexOf('class="oes-exch-filter"', pickerIdx);
    expect(acctIdx).toBeGreaterThan(pickerIdx);
    expect(acctIdx).toBeLessThan(symIdx);
    expect(exchIdx).toBeGreaterThan(pickerIdx);
    expect(exchIdx).toBeLessThan(symIdx);
  });

  test('.oes-chase-row (CHASE/DRAFT shared row) still excludes Chain at its own OUTER gate — unrelated dead-row fix, kept through this revert', () => {
    const rowIdx = panelSrc.indexOf('<div class="oes-chase-row">');
    expect(rowIdx, '.oes-chase-row div not found').toBeGreaterThan(-1);
    const gateIdx = panelSrc.lastIndexOf('{#if', rowIdx);
    expect(gateIdx).toBeGreaterThan(-1);
    const gateLine = panelSrc.slice(gateIdx, panelSrc.indexOf('\n', gateIdx));
    expect(gateLine).toMatch(/_activeTab !== 'cli'/);
    expect(gateLine).toMatch(/_activeTab !== 'chart'/);
    expect(gateLine).toMatch(/_activeTab !== 'chain'/);
  });

  test('.oes-sym-pick / .ssi-drop right-anchor fix (mobile overflow) is untouched', () => {
    expect(panelSrc).toMatch(/\.oes-sym-pick :global\(\.ssi-drop\)\s*\{\s*left:\s*auto;\s*right:\s*0;\s*\}/);
  });

  test('<OptionChainTab> invocation no longer passes a symbolPicker prop', () => {
    const idx = panelSrc.indexOf('<OptionChainTab\n');
    expect(idx, 'real <OptionChainTab invocation tag not found').toBeGreaterThan(-1);
    // Bounded by the invocation's own closing `}} />` (the last prop,
    // onBasketPlace, is a multi-line callback that ends the tag inline
    // rather than on its own standalone `/>` line).
    const closeIdx = panelSrc.indexOf('}} />', idx);
    expect(closeIdx).toBeGreaterThan(idx);
    const propsSlice = panelSrc.slice(idx, closeIdx);
    expect(propsSlice).toContain('symbol={_localSymbol}');
    expect(propsSlice).not.toMatch(/symbolPicker/);
  });
});

// ──────────────────────────────────────────────────────────────────
// Live — real browser, drives the actual (reverted) Chain tab.
// ──────────────────────────────────────────────────────────────────

/**
 * @param {import('@playwright/test').Page} page
 * @param {string} [sym] defaults to NIFTY (an index with NFO weekly
 *   options, so the Chain tab's own `chainDisabled` gate never fires).
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

for (const [label, viewport] of [
  ['desktop (1440px)', { width: 1440, height: 900 }],
  ['mobile (370px)', { width: 370, height: 800 }],
]) {
  test.describe(`Live: Chain tab — Symbol and Expiry are two separate elements, two separate rows, at ${label}`, () => {
    test.use({ viewport });

    test('exactly one visible Symbol input renders on the Chain tab (no duplicate now that .oes-sym-pick is unconditional)', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      await expect(page.locator('.oct-toolbar-label')).toBeVisible({ timeout: 15_000 });
      const visibleSymInputs = page.locator('.ssi-input:visible');
      await expect(visibleSymInputs).toHaveCount(1);
      await expect(visibleSymInputs.first()).toHaveValue(/NIFTY/i);
    });

    test('Symbol (.oes-sym-pick, inside .oes-picker) sits in a row above the Expiry row (.oct-toolbar-label)', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const symPick = page.locator('.oes-sym-pick').first();
      const expiryLabel = page.locator('.oct-toolbar-label').first();
      await expect(symPick).toBeVisible({ timeout: 15_000 });
      await expect(expiryLabel).toBeVisible({ timeout: 15_000 });

      const symBox = await symPick.boundingBox();
      const expiryBox = await expiryLabel.boundingBox();
      expect(symBox, '.oes-sym-pick box').toBeTruthy();
      expect(expiryBox, '.oct-toolbar-label box').toBeTruthy();
      // Symbol's row sits at or above Expiry's row — never overlapping,
      // never below it.
      expect(symBox.y + symBox.height, `Symbol row (bottom=${symBox.y + symBox.height}) overlaps Expiry row (top=${expiryBox.y})`)
        .toBeLessThanOrEqual(expiryBox.y + 1);
    });

    test('the Expiry Select has real chrome (a visible border) — the chrome-strip from the merge is gone', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const trigger = page.locator('.oct-expiry-pick .rbq-select-trigger').first();
      await expect(trigger).toBeVisible({ timeout: 15_000 });
      const borderWidth = await trigger.evaluate((el) => parseFloat(getComputedStyle(el).borderTopWidth) || 0);
      expect(borderWidth, 'Expiry Select trigger border-top-width').toBeGreaterThan(0);
    });

    test('both Symbol search and Expiry dropdown remain independently clickable/functional', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const expiryTrigger = page.locator('.oct-expiry-pick .rbq-select-trigger').first();
      await expect(expiryTrigger).toBeVisible({ timeout: 15_000 });
      const beforeLabel = (await expiryTrigger.textContent())?.trim();
      await expiryTrigger.click();
      const panel = page.locator('.oct-expiry-pick .rbq-select-panel').first();
      await expect(panel).toBeVisible({ timeout: 5000 });
      const options = panel.locator('.rbq-select-option');
      const optCount = await options.count();
      expect(optCount, 'Expiry picker must expose at least one real option').toBeGreaterThan(0);
      if (optCount > 1) {
        await options.nth(1).click();
        await expect(panel).toBeHidden({ timeout: 5000 });
        const afterLabel = (await expiryTrigger.textContent())?.trim();
        expect(afterLabel).not.toBe(beforeLabel);
      } else {
        await page.keyboard.press('Escape');
      }

      const symInput = page.locator('.oes-sym-pick .ssi-input').first();
      await expect(symInput).toBeVisible({ timeout: 10_000 });
      await symInput.fill('BANKNIFTY');
      const sugg = page.locator('.oes-sym-pick .ssi-drop .ssi-row').first();
      await expect(sugg).toBeVisible({ timeout: 10_000 });
      await sugg.click({ force: true });
      await expect(symInput).toHaveValue(/BANKNIFTY/i, { timeout: 10_000 });
    });

    // Live case for the kept-through-this-revert DST close-time fix:
    // a GOLD (MCX) contract must not show "expires today" a day early.
    test('GOLD (MCX) DTE chip does not read "expires today" when the real expiry is still a day or more away', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page, 'GOLD');

      const dteChip = page.locator('.oct-expiry-dte').first();
      const isVisible = await dteChip.isVisible({ timeout: 15_000 }).catch(() => false);
      if (!isVisible) {
        test.info().annotations.push({ type: 'skip', description: 'No GOLD expiry/DTE chip resolved on this run (instruments cache or weekend)' });
        return;
      }
      const text = (await dteChip.textContent())?.trim() ?? '';

      // Read the resolved expiry date straight off the Expiry Select's
      // own label (e.g. "27 Nov 2026 (Fri)") and compare it against
      // today's real IST calendar date — this is the actual ground
      // truth for "is this a genuine same-day expiry", not a time-of-
      // day guess. The old heuristic (skip past 15:00 IST) was wrong:
      // it's perfectly possible for the picked contract to genuinely
      // expire today at ANY hour (including the small hours just after
      // midnight IST, right after a trading-day rollover) — confirmed
      // live during this very test run.
      const selectLabel = (await page.locator('.oct-expiry-pick .rbq-select-trigger').first().textContent())?.trim() ?? '';
      const dateMatch = selectLabel.match(/(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})/);
      const todayIstStr = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric',
      }).format(new Date()); // e.g. "27 Nov 2026"
      const isGenuineSameDayExpiry = dateMatch
        ? `${dateMatch[1].padStart(2, '0')} ${dateMatch[2]} ${dateMatch[3]}` === todayIstStr
        : null;

      if (isGenuineSameDayExpiry === null) {
        test.info().annotations.push({ type: 'skip', description: `could not parse a date out of the Expiry Select label "${selectLabel}"` });
        return;
      }
      if (isGenuineSameDayExpiry) {
        // A real same-day expiry — "expires today" is the CORRECT
        // reading here, the opposite of the bug this fix prevents.
        expect(text).toMatch(/expires today|^0d$/i);
        return;
      }
      // The resolved contract expires on a LATER calendar date than
      // today — the chip must never say "expires today" for it.
      expect(text).not.toMatch(/expires today/i);
    });
  });
}
