/**
 * chain_ticket_severance_and_mobile_fixes.spec.js
 *
 * Source-scan guards for the 2026-09-29 batch of fixes:
 *
 *  1. Ticket/Chain template architectural severance — the operator's
 *     final, repeatedly-confirmed decision ("make sure order ticket is
 *     not wired to template"): the Ticket tab places/closes positions
 *     with NO template attach at all; templates only ever apply to the
 *     Chain tab's basket legs.
 *  2. Mobile chain-area height cap — operator reported the Templ row
 *     was invisible on mobile because .chain-grid-wrap's unbounded
 *     flex-grow starved every sibling below it (including the Templ
 *     row) of any box height. Fixed with a mobile max-height cap.
 *     SUPERSEDED (2026-09-30 audit) — the Templ toggle moved INTO the
 *     expiry toolbar row ABOVE the grid the same day (item 7 below),
 *     so the cap no longer protected anything; it just silently
 *     wasted ~200px of real strike-grid space on a typical phone
 *     (measured 412×919: capped wrap height 256px / clientHeight
 *     254px vs a real chain's scrollHeight ~2779px). The cap is
 *     removed entirely; the mobile breakpoint for this rule is also
 *     corrected from 760px to 720px to match SymbolPanel.svelte's
 *     `--chain-depth-h` / `.oct-root` mobile breakpoint exactly. See
 *     the updated tests in the "Mobile chain height cap..." describe
 *     block below (now "Mobile chain height cap REMOVED...").
 *  3. Desktop chain row gap — removing the row border-bottom (an
 *     earlier declutter fix) left rows with zero vertical padding on
 *     desktop, reading as "very close". Fixed with restored padding.
 *  4. Submit button labels — lot size dropped from the Ticket-tab
 *     label; the Chain-tab basket label is now plain "Submit" instead
 *     of "Submit (N)".
 *  5. Chase indicator right-alignment — CHASE was only pushed to the
 *     right edge via the LTP pill's margin-left:auto; when LTP doesn't
 *     render (common on Chain tab, no single-symbol LTP while staging
 *     a multi-leg basket) CHASE lost its right anchor. Fixed with its
 *     own margin-left:auto.
 *  6. Chain +/- buttons — :active pressed-state styling so a tap/click
 *     reads as tactile feedback, not just a hover restatement.
 *
 * 2026-09-30 batch (Templ toggle relocation + CE/PE layout/palette):
 *  7. Templ toggle relocated from a shell-level SymbolPanel row into
 *     OptionChainTab's own expiry toolbar row; TemplateBar.svelte's
 *     primary control changed from a Default/None/named <Select>
 *     dropdown to a compact ON/OFF toggle pill (named-template picker
 *     moved into the expand panel).
 *  8. CE/PE header text-align flipped to match where the +/- buttons
 *     actually sit; header border-bottom darkened; chain font-size
 *     reset to the platform's normal --fs-md/--fs-sm scale (0.78rem
 *     override removed); +/- buttons get a solid rest-state border +
 *     one-tier-stronger background.
 *  9. Chain leg badges show a truncated template short-label suffix
 *     when a template is attached.
 * 10. app.css's global `.oes-common-chase-label` fallback realigned to
 *     match SymbolPanel.svelte's own scoped copy (color drift fix).
 *
 * All source-scan (no browser) — mirrors this session's established
 * pattern for guarding CSS/markup decisions that are cheap to verify
 * from source and expensive/flaky to verify live on every push.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
const SYMBOL_PANEL = readFileSync(path.join(dir, 'src/lib/SymbolPanel.svelte'), 'utf8');
const ORDER_TICKET = readFileSync(path.join(dir, 'src/lib/order/OrderTicket.svelte'), 'utf8');
const CHAIN_TAB = readFileSync(path.join(dir, 'src/lib/order/OptionChainTab.svelte'), 'utf8');
const SUBMIT_HELPERS = readFileSync(path.join(dir, 'src/lib/order/orderTicketSubmit.js'), 'utf8');
const TEMPLATE_BAR = readFileSync(path.join(dir, 'src/lib/TemplateBar.svelte'), 'utf8');
const APP_CSS = readFileSync(path.join(dir, 'src/app.css'), 'utf8');
const CHART_WORKSPACE = readFileSync(path.join(dir, 'src/lib/ChartWorkspace.svelte'), 'utf8');
const ORDER_DEPTH = readFileSync(path.join(dir, 'src/lib/order/OrderDepth.svelte'), 'utf8');

/**
 * Navigate to /orders, type an F&O-eligible symbol (NIFTY — index with
 * NFO weekly options, so the Chain tab's own `chainDisabled` root-
 * eligibility gate never fires — a bare/empty symbol trivially has NO
 * F&O coverage and disables Chain entirely, so "no symbol" isn't a
 * reachable Chain-tab state to test against), then switch to the
 * Chain tab. Shared by the live browser tests below.
 * @param {import('@playwright/test').Page} page
 */
async function _seedNiftyAndOpenChain(page) {
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
 * Open the REAL popup order-entry modal (`.canonical-modal-panel` +
 * `.oes-modal`, a genuinely viewport-bounded fixed-height box — NOT the
 * always-visible `inline` SymbolPanel that `_seedNiftyAndOpenChain` above
 * drives on /orders, whose height is content-driven and grows with the
 * page). This is the actual context the mobile max-height cap bug lived
 * in: `.oes-body` here has a real, fixed available-height budget (bounded
 * by the modal's own `height: calc(100dvh - ...)`), so a height-vs-
 * available-space comparison is meaningful — unlike the inline panel,
 * where both grow together and the comparison is trivially satisfied.
 * Reached via the `.pha-order` ticket button on /dashboard (same trigger
 * as the `t` keyboard shortcut).
 * @param {import('@playwright/test').Page} page
 */
async function _openPopupChainOnDashboard(page) {
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const orderBtn = page.locator('button.pha-order').first();
  await expect(orderBtn).toBeVisible({ timeout: 15_000 });
  await orderBtn.click({ force: true });

  const modal = page.locator('.oes-modal').first();
  await expect(modal).toBeVisible({ timeout: 15_000 });

  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });

  const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
  await expect(chainTab).toBeEnabled({ timeout: 15_000 });
  await chainTab.click();
}

test.describe('Ticket/Chain template severance', () => {
  test('Templ toggle/note visibility gates only ever fire for the Chain tab, never Ticket', () => {
    // 2026-09-30 — the shell-level demo/live if-else-if branch that used
    // to render <TemplateBar> directly was replaced by two $derived
    // booleans (_showTemplateBar / _showDemoTplNote), threaded down
    // into <OptionChainTab> as plain props; the ENCLOSING mount gate
    // (`{#if _activeTab === 'chain'}` around <OptionChainTab> itself)
    // is what scopes both to the Chain tab, not either variable's own
    // definition. `_showDemoTplNote` still has real content gating
    // (demo + action='open' + symbol-or-legs). `_showTemplateBar` was
    // made UNCONDITIONALLY true (2026-09-30 follow-up, operator: "Templ
    // toggle should show unconditionally in Chain") — see the separate
    // `_showTemplatePreview` test below for the preserved old gating on
    // the shell-level preview/cap-warn strip (a different consumer).
    const showTemplateBarDef = SYMBOL_PANEL.match(/const _showTemplateBar = \$derived\([\s\S]{0,60}?\);/)?.[0] ?? '';
    const showDemoNoteDef = SYMBOL_PANEL.match(/const _showDemoTplNote = \$derived\([\s\S]{0,220}?\);/)?.[0] ?? '';
    expect(showTemplateBarDef, '_showTemplateBar definition').not.toBe('');
    expect(showDemoNoteDef, '_showDemoTplNote definition').not.toBe('');
    expect(showTemplateBarDef).toMatch(/\$derived\(true\)/);
    expect(showDemoNoteDef).toMatch(/_isDemo/);
    expect(showDemoNoteDef).toMatch(/action === 'open'/);
    // Both gates are threaded into <OptionChainTab>, which only ever
    // mounts when `_activeTab === 'chain'` (its own enclosing {#if}).
    expect(SYMBOL_PANEL).toMatch(/showTemplateBar=\{_showTemplateBar\}/);
    expect(SYMBOL_PANEL).toMatch(/showDemoTplNote=\{_showDemoTplNote\}/);
    // The old inline demo-note markup + its dedicated CSS rule blocks
    // must be gone (a removal-note comment mentioning the class names
    // in prose is fine) — the note itself now renders inside
    // OptionChainTab.
    expect(SYMBOL_PANEL).not.toMatch(/\.oes-basket-tpl-row-demo\s*\{/);
    expect(SYMBOL_PANEL).not.toMatch(/\.oes-basket-tpl-demo-note\s*\{/);
    expect(SYMBOL_PANEL).not.toMatch(/class="oes-basket-tpl-row-demo"/);
  });

  test('_showTemplatePreview keeps the OLD gating for the shell-level on-fill preview + cap-warn strip', () => {
    // A SEPARATE consumer from the toggle above — genuinely needs
    // templates loaded / action='open' / a real symbol-or-legs before
    // it has anything to preview; making the toggle unconditional must
    // not also make this strip render with nothing to show.
    const def = SYMBOL_PANEL.match(/const _showTemplatePreview = \$derived\([\s\S]{0,220}?\);/)?.[0] ?? '';
    expect(def, '_showTemplatePreview definition').not.toBe('');
    expect(def).toMatch(/_templates\.length > 0/);
    expect(def).toMatch(/action === 'open'/);
    expect(SYMBOL_PANEL).toMatch(/\{#if _showTemplatePreview && !_isDemo && !_shellUsingNone\}/);
    // The old variable name must not still be used for this gate.
    expect(SYMBOL_PANEL).not.toMatch(/\{#if _showTemplateBar && !_isDemo/);
  });

  test('Templ toggle mounts inside OptionChainTab, never SymbolPanel', () => {
    // <TemplateBar (the real Svelte component mount, tag-open) must
    // exist in OptionChainTab.svelte and be entirely absent from
    // SymbolPanel.svelte — only pass-through prop names (showTemplateBar,
    // _showTemplateBar, etc.) may reference the word "TemplateBar" there.
    expect(CHAIN_TAB).toMatch(/<TemplateBar/);
    expect(SYMBOL_PANEL).not.toContain('<TemplateBar');
    // SymbolPanel no longer imports the component directly.
    expect(SYMBOL_PANEL).not.toMatch(/import TemplateBar\s+from/);
  });

  test('OrderTicket mount no longer auto-selects a template', () => {
    const rawMountFn = ORDER_TICKET.match(/onMount\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(rawMountFn, 'onMount body').not.toBe('');
    // Strip comment-only lines — the removal is explained in a code
    // comment that itself mentions the old call names in prose.
    const mountFn = rawMountFn
      .split('\n')
      .filter((line) => !/^\s*(\/\/|\*)/.test(line))
      .join('\n');
    expect(mountFn, 'onMount executable body must not call _autoSelectTemplate()')
      .not.toMatch(/_autoSelectTemplate\(\)/);
    expect(mountFn, 'onMount executable body must not call loadOrderTemplates()')
      .not.toMatch(/loadOrderTemplates\(\)/);
  });

  test('OrderTicket submit payload always sends template_id: null (not just for close orders)', () => {
    expect(ORDER_TICKET).toMatch(/templateId:\s*null,/);
    // The old conditional form (`_isCloseOrder ? null : templateId`) must be gone.
    expect(ORDER_TICKET).not.toMatch(/templateId:\s*_isCloseOrder\s*\?\s*null\s*:\s*templateId/);
  });

  test('side-aware Default resolution survives the severance — still wired into the Chain-only TemplateBar', () => {
    // Replaces the old e2e/template_default_pill.spec.js (removed):
    // that spec drove the 4-scope Default-pill matrix through the
    // Ticket tab's BUY/SELL toggle + the old pill UI, both gone now
    // (Ticket has no template row; the pill toggle became a dropdown
    // in an earlier commit this session). The _sideAwareDefault
    // resolver itself is untouched by the severance — only where it
    // renders changed (Chain tab only) — so this is a structural
    // continuity check, not a behavioural regression test.
    expect(SYMBOL_PANEL).toMatch(/const _sideAwareDefault = \$derived\.by/);
    expect(SYMBOL_PANEL).toContain('sideAwareDefault={_sideAwareDefault}');
  });

  test('side-aware Default falls back to BUY when no side is known yet, without touching _modalSide itself (2026-09-30 fix)', () => {
    // Bug: a fresh/cold order entry starts with _modalSide === null
    // (deliberate — see _modalSide's own declaration comment, preserves
    // the SideToggle's neutral state and the margin-preflight
    // short-circuit). appliesToFor(null, sym) falls through every
    // BUY/SELL branch to 'both', and no template has
    // is_default=true AND applies_to='both' — so Templ rendered
    // disabled ("No default template configured for this side/type")
    // on every fresh order, for every symbol, until the operator
    // explicitly picked a side. Fix: the scope guess (NOT _modalSide
    // itself) falls back to 'BUY' when nothing else is known yet.
    // The fallback ladder was factored into a single shared
    // `_currentScope()` helper (2026-09-30, remember-per-scope fix) —
    // reused by `_sideAwareDefault`, the side-flip auto-swap effect,
    // and the three Templ onSelect* persistence call sites, so they
    // can never disagree on the fallback.
    // Limit bumped 400 → 1800 (2026-09-30, bug-1 fix) — `_currentScope()`
    // grew a documentation block explaining the Chain-tab leg-symbol
    // preference reorder (root symbol was never option-suffixed, so
    // appliesToFor() could never resolve to buy_option/sell_option).
    const scopeFn = SYMBOL_PANEL.match(/function _currentScope\(\) \{[\s\S]{0,1800}?\n  \}/)?.[0] ?? '';
    expect(scopeFn, '_currentScope() helper').not.toBe('');
    expect(scopeFn).toMatch(/_focusedLeg\?\.side \|\| _modalSide \|\| 'BUY'/);
    expect(scopeFn).toMatch(/_appliesToFor\(sideForScope, symForScope\)/);
    // Both consumers call the shared helper rather than re-inlining the
    // fallback ladder themselves.
    const sideAwareDefaultBlock = SYMBOL_PANEL.match(/const _sideAwareDefault = \$derived\.by\([\s\S]{0,1400}?\n  \}\);/)?.[0] ?? '';
    expect(sideAwareDefaultBlock, '_sideAwareDefault derivation block').not.toBe('');
    expect(sideAwareDefaultBlock).toMatch(/_currentScope\(\)/);
    // Limit bumped 2400 → 5000 (2026-09-30, bug-2 fix) — the effect grew
    // the `_templExplicitThisSession` session-scoped explicit-choice
    // flag + its gating comment (prevents a scope change from silently
    // re-applying an unrelated remembered "none" pref over an explicit
    // in-session operator choice).
    const swapEffectBlock = SYMBOL_PANEL.match(/let _lastSideScope[\s\S]{0,5000}?\n  \}\);/)?.[0] ?? '';
    expect(swapEffectBlock, 'side-flip auto-swap effect block').not.toBe('');
    expect(swapEffectBlock).toMatch(/_currentScope\(\)/);
  });

  test('Chain tab still owns the shared templateId binding (severance is Ticket-only)', () => {
    expect(SYMBOL_PANEL).toContain('bind:templateId={_sharedTemplateId}');
    // Only one bind:templateId consumer should exist in the shell markup
    // (OptionChainTab) — OrderTicket's mount no longer receives it.
    const bindCount = (SYMBOL_PANEL.match(/bind:templateId=/g) || []).length;
    expect(bindCount, 'exactly one bind:templateId in SymbolPanel (Chain only)').toBe(1);
  });
});

test.describe('Mobile chain height cap REMOVED (2026-09-30 audit) + desktop row gap', () => {
  // SUPERSEDED — the Templ toggle this cap protected moved into the
  // expiry toolbar row ABOVE the grid the same day (2026-09-30), so the
  // cap had nothing left to protect. It was silently wasting ~200px of
  // real strike-grid space on a typical phone (measured 412×919: capped
  // wrap height 256px / clientHeight 254px vs a real chain's scrollHeight
  // ~2779px for NIFTY). Also fixes the breakpoint mismatch against
  // SymbolPanel.svelte's own `--chain-depth-h` / `.oct-root` mobile
  // override, which uses 720px, not 760px — between 721-760px the grid
  // could end up BOTH capped and un-min-height'd, forcing the wrong size.
  test('chain-grid-wrap has NO mobile max-height — the cap is gone entirely', () => {
    const mobileBlock = CHAIN_TAB.match(/@media \(max-width: 720px\) \{\s*\.chain-grid-wrap \{[\s\S]{0,200}?\n  \}/)?.[0] ?? '';
    expect(mobileBlock, 'mobile .chain-grid-wrap rule').not.toBe('');
    expect(mobileBlock).not.toMatch(/max-height/);
    expect(mobileBlock).toMatch(/flex:\s*1 1 auto/);
  });

  test('mobile .chain-grid-wrap breakpoint is 720px, matching SymbolPanel.svelte exactly (not 760px)', () => {
    // Scoped to THIS rule only — .chain-row > td's own mobile padding
    // override (a separate, unrelated block) is deliberately left at
    // 760px; only the .chain-grid-wrap breakpoint was implicated in the
    // 721-760px mismatch bug.
    expect(CHAIN_TAB).toMatch(/@media \(max-width: 720px\) \{\s*\.chain-grid-wrap \{/);
    expect(CHAIN_TAB).not.toMatch(/@media \(max-width: 760px\) \{\s*\.chain-grid-wrap \{/);
  });

  test('live: .chain-grid-wrap resolves max-height: none at a mobile viewport (412px)', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChain(page);

    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 15_000 });
    await expect(wrap).toHaveCSS('max-height', 'none');
  });

  // Mandatory real height-comparison — a source/flex-value check alone
  // cannot catch "capped at 256px while its parent has 467px of genuinely
  // available fixed space" (exactly what let three earlier same-day flex-
  // tuning attempts ship without anyone noticing the cap was the actual
  // ceiling). Uses the REAL popup modal (_openPopupChainOnDashboard) —
  // .oes-body there has a real fixed-height budget (bounded by the
  // canonical-modal-panel), unlike the inline /orders panel where both
  // grow together and this comparison would be trivially satisfied.
  test('live: .chain-grid-wrap fills its available .oes-body space (within ~40px), scrolls internally, and the submit footer stays fully visible', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await _openPopupChainOnDashboard(page);

    // Real chain data must actually be loaded — an empty chain gives
    // .oct-root nothing to grow into and would make this comparison
    // meaningless (pass by accident).
    await expect(async () => {
      const n = await page.locator('.chain-row').count();
      expect(n, 'strike rows loaded').toBeGreaterThanOrEqual(15);
    }).toPass({ timeout: 20_000 });

    const oesBody = page.locator('.oes-body').first();
    const toolbar = page.locator('.oct-toolbar').first();
    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 10_000 });

    const [oesBodyBox, toolbarBox, wrapBox] = await Promise.all([
      oesBody.boundingBox(),
      toolbar.boundingBox(),
      wrap.boundingBox(),
    ]);
    expect(oesBodyBox, '.oes-body box').toBeTruthy();
    expect(toolbarBox, '.oct-toolbar box').toBeTruthy();
    expect(wrapBox, '.chain-grid-wrap box').toBeTruthy();

    // .oes-body's own box is the true available-height budget (it's the
    // fixed-height flex region in the real popup modal); the grid's
    // budget is that minus the toolbar row above it. A small residual
    // (.oct-root's own flex `gap: 0.5rem` + .oct-toolbar's own
    // margin-bottom/border — measured ~42px) is expected and NOT the bug
    // being guarded against here (the original cap wasted ~200px+).
    const available = oesBodyBox.y + oesBodyBox.height - (toolbarBox.y + toolbarBox.height);
    const deadSpace = available - wrapBox.height;
    expect(deadSpace, `dead space below/around the grid (available=${available.toFixed(0)}px, wrap=${wrapBox.height.toFixed(0)}px)`)
      .toBeLessThanOrEqual(60);
    expect(deadSpace, 'grid must not overflow its own available budget either').toBeGreaterThanOrEqual(-60);

    // The grid must be doing its OWN internal scrolling for the 100+-row
    // real chain (it fills the available box rather than expanding past
    // it) — confirms flex:1 1 auto + overflow-y:auto are both doing real
    // work post-fix, not just "no cap" turning into "unbounded growth".
    const scrollInfo = await wrap.evaluate((el) => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }));
    expect(scrollInfo.scrollHeight, 'grid content must exceed its own box (real internal scroll)').toBeGreaterThan(scrollInfo.clientHeight);

    // The submit footer (a modal-level sibling of .oes-body, not nested
    // inside it) must keep its own real, non-zero, on-screen slot — the
    // exact thing the original (now-superseded) max-height cap claimed to
    // protect. Confirms removing the cap did not starve it.
    const submit = page.locator('.oes-common-submit').first();
    await expect(submit).toBeVisible({ timeout: 10_000 });
    const submitBox = await submit.boundingBox();
    expect(submitBox, '.oes-common-submit box').toBeTruthy();
    expect(submitBox.height, 'submit button must have real height, not starved to 0').toBeGreaterThan(10);
    const modalBox = await page.locator('.oes-modal').first().boundingBox();
    expect(submitBox.y + submitBox.height, 'submit button bottom must stay within the modal viewport')
      .toBeLessThanOrEqual(modalBox.y + modalBox.height + 2);
  });

  // Breakpoint, computed-style — proves the 720px threshold actually
  // governs the cascade, not just that the source text says 720px.
  // .chain-grid-wrap's BASE rule is `flex: 1 1 0` (flex-basis: 0px); the
  // mobile override (<=720px) changes only the flex shorthand to
  // `1 1 auto` (flex-basis: auto) — this is a stronger signal than
  // max-height alone because it distinguishes "the media query didn't
  // fire" from "it fired but this property happens to look the same".
  test('live: flex-basis flips exactly at the 720px threshold (740px desktop vs 712px mobile)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChain(page);
    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 15_000 });

    await page.setViewportSize({ width: 740, height: 900 });
    await expect(wrap).toHaveCSS('flex-basis', '0px');

    await page.setViewportSize({ width: 712, height: 900 });
    await expect(wrap).toHaveCSS('flex-basis', 'auto');
  });

  test('desktop chain rows have restored vertical padding after border-bottom removal', () => {
    const desktopBlock = CHAIN_TAB.match(/@media \(min-width: 640px\) \{\s*\.chain-row > td \{[\s\S]{0,900}?\n    \}/)?.[0] ?? '';
    expect(desktopBlock, 'desktop .chain-row > td override block').not.toBe('');
    expect(desktopBlock).toMatch(/border-bottom:\s*none/);
    expect(desktopBlock).toMatch(/padding-top:\s*0\.3rem/);
    expect(desktopBlock).toMatch(/padding-bottom:\s*0\.3rem/);
  });
});

test.describe('Submit button labels', () => {
  test('formatSubmitLabel drops lot size from Ticket-tab labels', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).not.toBe('');
    expect(fn, 'no qty/qtySuffix should be interpolated into the label anymore')
      .not.toMatch(/qtySuffix/);
  });

  test('formatSubmitLabel returns plain "Submit" for basket mode (Chain tab), not "Submit (N)"', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).toMatch(/if \(ctx\.basketCount > 0\) return 'Submit';/);
  });

  test('SymbolPanel common-action submit button also renders plain "Submit" for basket mode', () => {
    expect(SYMBOL_PANEL).not.toMatch(/Submit \(\$\{basketLegs\.length\}\)/);
    expect(SYMBOL_PANEL).not.toMatch(/`Submit \(\$\{basketLegs\.length\}\)`/);
  });

  // 2026-09-30: operator — "chain button to start with shows submit
  // suffixed by additional text. which should be removed." Root cause:
  // _submitLabel's Chain branch was keyed on basketLegs.length > 0, not
  // on the active tab, so an EMPTY Chain basket fell through to the
  // Ticket-style side/verb label. Fixed by gating on _activeTab directly.
  test('_submitLabel is unconditionally "Submit" on the Chain tab, even with an empty basket', () => {
    const fn = SYMBOL_PANEL.match(/const _submitLabel = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(fn, '_submitLabel derived').not.toBe('');
    expect(fn).toMatch(/if \(basketLegs\.length > 0 \|\| _activeTab === 'chain'\) return 'Submit';/);
  });

  // 2026-09-30: operator — "for order ticket the submit button should
  // not have Submit in the label." formatSubmitLabel's resolved-side
  // branches (cq===0 and cq!==0) no longer prefix "Submit · " — only
  // the side/verb text is returned. The bare-fallback (!ctx.side) case
  // still returns "Submit" since there's nothing else to show there.
  test('formatSubmitLabel drops the "Submit" prefix entirely for resolved-side (Ticket) labels', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).not.toBe('');
    expect(fn).toMatch(/if \(cq === 0\) return ctx\.side;/);
    expect(fn).toMatch(/return `\$\{verb\}\/\$\{ctx\.side\}`;/);
    expect(fn, 'no "Submit ·" prefix should remain in any resolved-side branch')
      .not.toMatch(/`Submit · /);
  });
});

test.describe('Chase indicator right-alignment', () => {
  test('CHASE label owns its own margin-left:auto, independent of the LTP pill', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-common-chase-label \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-common-chase-label rule').not.toBe('');
    expect(rule).toMatch(/margin-left:\s*auto/);
  });
});

test.describe('Chain +/- pressed-state feedback', () => {
  test('chain-btn-buy/-sell have :active rules distinct from :hover', () => {
    expect(CHAIN_TAB).toMatch(/\.chain-btn-buy:active\s*\{/);
    expect(CHAIN_TAB).toMatch(/\.chain-btn-sell:active\s*\{/);
    const buyActive = CHAIN_TAB.match(/\.chain-btn-buy:active\s*\{[\s\S]*?\}/)?.[0] ?? '';
    expect(buyActive, 'pressed state should visually differ from rest/hover (border/box-shadow highlight)')
      .toMatch(/box-shadow|border-color/);
  });
});

test.describe('CE/PE header alignment + palette normalization (2026-09-30)', () => {
  test('CE header text-align right, PE header text-align left — matches the +/- button side', () => {
    const ceRule = CHAIN_TAB.match(/\.chain-th-ce\s*\{[^}]*\}/)?.[0] ?? '';
    const peRule = CHAIN_TAB.match(/\.chain-th-pe\s*\{[^}]*\}/)?.[0] ?? '';
    expect(ceRule, '.chain-th-ce rule').not.toBe('');
    expect(peRule, '.chain-th-pe rule').not.toBe('');
    expect(ceRule).toMatch(/text-align:\s*right/);
    expect(peRule).toMatch(/text-align:\s*left/);
    // The row-content alignment (+/- buttons toward Strike column) must
    // stay untouched — a deliberate, explicitly-commented layout choice.
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-ce\s*\{\s*justify-content:\s*flex-end;\s*\}/);
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-pe\s*\{\s*justify-content:\s*flex-start;\s*\}/);
  });

  test('no font-size: 0.78rem declaration remains anywhere in the chain grid CSS', () => {
    expect(CHAIN_TAB).not.toMatch(/font-size:\s*0\.78rem/);
  });

  test('chain header cells have a visibly bright bottom edge via box-shadow:inset, 0.35 alpha (SUPERSEDED 2026-09-30 — border-bottom replaced by box-shadow to fix sticky+border-collapse repaint bug)', () => {
    // Was border-bottom (brightened from 0.18 to 0.35 alpha same day),
    // then changed to box-shadow: inset (operator: "again the border
    // shows and disappears" / "...in the money calls and puts, it
    // disappears") — sticky <th> + border-collapse:collapse is a known
    // Chrome/WebKit bug where the border drops on a big repaint; the
    // ITM/OTM background-wash switch-on (elsewhere in this file) is
    // exactly that kind of repaint. box-shadow isn't part of table
    // border-collapse semantics, so it's immune.
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = CHAIN_TAB.match(new RegExp(`\\.${sel.slice(1)}\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule, `${sel} box-shadow alpha`).toMatch(/box-shadow:\s*inset 0 -1px 0 rgba\(255,255,255,0\.35\)/);
      expect(rule, `${sel} must not use border-bottom`).not.toMatch(/border-bottom:\s*\S/);
    }
  });

  test('chain-btn-buy/-sell rest state uses the -14 background tier and a solid (non-alpha) border', () => {
    const buyRule = CHAIN_TAB.match(/\.chain-btn-buy\s*\{[^}]*\}/)?.[0] ?? '';
    const sellRule = CHAIN_TAB.match(/\.chain-btn-sell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(buyRule).toMatch(/background:\s*var\(--c-long-14\)/);
    expect(sellRule).toMatch(/background:\s*var\(--c-short-14\)/);
    // Solid border — references the base color token, not an alpha-tinted `-NN` variant.
    expect(buyRule).toMatch(/border-color:\s*var\(--c-long\)/);
    expect(buyRule).not.toMatch(/border-color:\s*var\(--c-long-\d/);
    expect(sellRule).toMatch(/border-color:\s*var\(--c-short\)/);
    expect(sellRule).not.toMatch(/border-color:\s*var\(--c-short-\d/);
  });

  // 2026-09-30 ROOT-CAUSE FIX (supersedes the same-day flex:0 1 auto
  // reversal above): operator reported "no change" on the empty-space
  // complaint even after that flip. Root cause was the PARENT —
  // SymbolPanel.svelte's `.oes-body :global(.oct-root)` was
  // unconditionally forcing `flex: 1 1 0` (full-stretch) on mobile too,
  // with no override next to its own `--chain-depth-h: auto` mobile
  // exception. Whatever `.chain-grid-wrap` did with its own flex value,
  // the leftover slack between the grid's real content height and
  // `.oct-root`'s forced full height had to show up as blank space
  // SOMEWHERE inside `.oct-root` — flipping `.chain-grid-wrap` alone
  // only ever relocated that gap. Fix: `.oct-root` drops its forced
  // full-stretch on mobile (`flex: 0 1 auto`), and `.chain-grid-wrap`
  // is reverted back to `flex: 1 1 auto` so the grid itself grows into
  // genuinely available leftover space (at the time, still capped at
  // 16rem — that cap is separately removed, see the "Mobile chain
  // height cap REMOVED" describe block above).
  test('mobile: .oct-root drops forced full-stretch, .chain-grid-wrap reverts to flex: 1 1 auto (2026-09-30 root-cause fix)', () => {
    const octRootMobileBlock = SYMBOL_PANEL.match(/@media \(max-width: 720px\) \{\s*\.oes-body :global\(\.oct-root\) \{[\s\S]{0,200}?\}/)?.[0] ?? '';
    expect(octRootMobileBlock, 'mobile .oct-root override block').not.toBe('');
    expect(octRootMobileBlock).toMatch(/\n\s*flex:\s*0 1 auto;/);
    // .oes-ticket-body is deliberately NOT part of this mobile override
    // — the Ticket tab's own depth ladder handles mobile differently.
    expect(octRootMobileBlock).not.toContain('oes-ticket-body');

    // .chain-grid-wrap's own mobile breakpoint is 720px (corrected from
    // 760px, 2026-09-30 audit) and no longer carries a max-height cap.
    const mobileBlock = CHAIN_TAB.match(/@media \(max-width: 720px\) \{\s*\.chain-grid-wrap \{[\s\S]{0,1200}?\}/)?.[0] ?? '';
    expect(mobileBlock, 'mobile .chain-grid-wrap block').not.toBe('');
    expect(mobileBlock).not.toMatch(/max-height/);
    expect(mobileBlock).toMatch(/\n\s*flex:\s*1 1 auto;/);
    expect(mobileBlock).not.toMatch(/\n\s*flex:\s*0 1 auto;/);
  });

  // Live-browser computed-style check — the source-scan above proves
  // the CSS rules exist; this proves the cascade actually resolves as
  // intended at a real mobile viewport (mobile-portrait project is
  // 360×800, already <720px/<760px so both media queries are active
  // by default — explicit setViewportSize kept anyway for clarity and
  // so this test is meaningful even when run under chromium-desktop).
  test('live: .oct-root computes flex-grow:0 and .chain-grid-wrap computes flex-grow:1 at mobile viewport width', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChain(page);

    const octRoot = page.locator('.oct-root').first();
    await expect(octRoot).toBeVisible({ timeout: 30_000 });
    await expect(octRoot).toHaveCSS('flex-grow', '0');

    const chainGridWrap = page.locator('.chain-grid-wrap').first();
    await expect(chainGridWrap).toBeVisible({ timeout: 10_000 });
    await expect(chainGridWrap).toHaveCSS('flex-grow', '1');
  });
});

test.describe('Chain leg badge — template short-label suffix', () => {
  test('badge shows a truncated template short-label when tmplAttached', () => {
    expect(CHAIN_TAB).toMatch(/tmplShort\s*=\s*tmplAttached\s*\?\s*String\(templateName\)\.slice\(0,\s*6\)\s*:\s*''/);
    // All 4 leg-badge render sites (ATM CE/PE + non-ATM CE/PE) must
    // interpolate the short-label suffix, not just the bare lots count.
    const badgeMatches = CHAIN_TAB.match(/\{(?:ce|pe)Leg\.lots\}L\{tmplAttached \? ' · ' \+ tmplShort : ''\}/g) ?? [];
    expect(badgeMatches.length, 'expected 4 leg-badge occurrences (ATM CE/PE + non-ATM CE/PE)').toBe(4);
    // The tooltip's own template-name string construction is untouched.
    expect(CHAIN_TAB).toMatch(/tmplAttached \? ' · template: ' \+ templateName : ' · no template'/);
  });
});

test.describe('TemplateBar — single toggle button replaces the primary dropdown/pill (2026-09-30)', () => {
  test('primary control is one toggle button, not a <Select> or the retired two-button pill', () => {
    expect(TEMPLATE_BAR).toMatch(/class="oes-tpl-button"/);
    expect(TEMPLATE_BAR).toMatch(/class:active=\{_toggleOn\}/);
    // Retired two-button pill markup/classes must be gone.
    expect(TEMPLATE_BAR).not.toMatch(/class="oes-tpl-toggle"/);
    expect(TEMPLATE_BAR).not.toMatch(/oes-tpl-toggle-btn-on/);
    expect(TEMPLATE_BAR).not.toMatch(/oes-tpl-toggle-btn-off/);
    // The toggle markup itself (before the expand panel) must not use <Select>.
    const beforeExpandPanel = TEMPLATE_BAR.split('{#if _expanded}')[0] ?? '';
    expect(beforeExpandPanel).not.toMatch(/<Select/);
  });

  test('RE-WIRED (2026-09-30) — button calls the real onSelectDefault/onSelectNone handlers, not a debug-only local toggle', () => {
    // The temporary debug bypass (which isolated whether TemplateBar's
    // own logic was the display problem) was reverted after the actual
    // root cause was found and fixed: loadOrderTemplates() (templates.js)
    // permanently cached a transient fetch failure as an empty template
    // list. See that file's own test coverage
    // (frontend/src/lib/__tests__/data/templates.test.js) for the fix.
    const btn = TEMPLATE_BAR.match(/class="oes-tpl-button"[\s\S]{0,700}?<\/button>/)?.[0] ?? '';
    expect(btn, 'toggle button block').not.toBe('');
    expect(btn).toContain('onSelectDefault?.()');
    expect(btn).toContain('onSelectNone?.()');
    expect(btn).toMatch(/if\s*\(_toggleOn\)/);
    // Button is always clickable/enabled (2026-09-30 follow-up,
    // operator: "make Templ always clickable/enabled — never visually
    // disabled") — the old `disabled={_templBtnDisabled}` attribute
    // is gone; clicking with nothing to activate to is a silent no-op.
    expect(btn).not.toMatch(/disabled=/);
    expect(btn).not.toContain('_debugToggleClick');
    expect(TEMPLATE_BAR).not.toMatch(/let _debugOn/);
    // ON/active display state guards against a null _sharedTemplateId
    // reading as "armed" (the financial-risk-relevant fix) — must
    // require BOTH !shellUsingNone AND a concrete selectedTemplate.
    expect(TEMPLATE_BAR).toMatch(/_toggleOn = \$derived\(!shellUsingNone && !!selectedTemplate\)/);
  });

  test('a specific named-template picker exists inside the expand panel, scoped to nonNoneTemplates', () => {
    const expandPanel = TEMPLATE_BAR.match(/\{#if _expanded\}[\s\S]*$/)?.[0] ?? '';
    expect(expandPanel, 'expand panel block').not.toBe('');
    expect(expandPanel).toMatch(/oes-tpl-pick-specific/);
    expect(expandPanel).toMatch(/<Select/);
    expect(expandPanel).toMatch(/nonNoneTemplates\.map/);
    expect(expandPanel).toMatch(/onSelectTemplate\?\.\(Number\(v\)\)/);
  });

  test('all existing TP%/SL%/Wing/Trail-SL/Scale-ladder override inputs are untouched', () => {
    for (const bindable of ['tpOverride', 'slOverride', 'wingStrikeOffsetOverride', 'wingPremPctOverride', 'slTrailPctOverride', 'tpScalesJsonOverride']) {
      expect(TEMPLATE_BAR, `${bindable} still bind:value`).toMatch(new RegExp(`bind:value=\\{${bindable}\\}`));
    }
    // TP order type is a button-pair toggle (not bind:value) — unchanged shape.
    expect(TEMPLATE_BAR).toMatch(/tpOrderTypeOverride = 'LIMIT'/);
    expect(TEMPLATE_BAR).toMatch(/tpOrderTypeOverride = 'MARKET'/);
  });
});

test.describe('Color audit — .oes-common-chase-label drift fix (app.css)', () => {
  test("app.css global fallback matches SymbolPanel's scoped color values exactly", () => {
    const scopedRule = SYMBOL_PANEL.match(/\.oes-common-chase-label\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    const scopedOnRule = SYMBOL_PANEL.match(/\.oes-common-chase-label\.on\s*\{[^}]*\}/)?.[0] ?? '';
    expect(scopedRule, 'SymbolPanel scoped .oes-common-chase-label rule').not.toBe('');
    expect(scopedOnRule, 'SymbolPanel scoped .oes-common-chase-label.on rule').not.toBe('');
    expect(scopedRule).toMatch(/color:\s*var\(--algo-slate-muted\)/);
    expect(scopedOnRule).toMatch(/color:\s*var\(--c-action\)/);

    const globalRule = APP_CSS.match(/\.oes-common-chase-label\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
    const globalOnRule = APP_CSS.match(/\.oes-common-chase-label\.on\s*\{[^}]*\}/)?.[0] ?? '';
    expect(globalRule, 'app.css global .oes-common-chase-label rule').not.toBe('');
    expect(globalOnRule, 'app.css global .oes-common-chase-label.on rule').not.toBe('');
    expect(globalRule).toMatch(/color:\s*var\(--algo-slate-muted\)/);
    expect(globalOnRule).toMatch(/color:\s*var\(--c-action\)/);
    // Old drifted values must be gone.
    expect(globalRule).not.toMatch(/color-mix\(in srgb, var\(--algo-slate\) 70%, transparent\)/);
    expect(globalOnRule).not.toMatch(/#fbbf24/);
  });
});

test.describe('Guard — no new order-placement call was added to any frontend submit path', () => {
  test('submitBasket still delegates to the existing placeBasket API helper, nothing new', () => {
    const fn = SYMBOL_PANEL.match(/async function submitBasket\(\) \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(fn, 'submitBasket implementation').not.toBe('');
    expect(fn).toMatch(/await placeBasket\(groups\)/);
    // No direct broker/order-placement call bypassing the existing
    // placeBasket/placeTicketOrder API-layer helpers was introduced by
    // this batch of changes.
    for (const banned of ['apply_plan_live', 'broker.place_order', 'broker.place_gtt']) {
      expect(SYMBOL_PANEL, `SymbolPanel must not call ${banned}`).not.toContain(banned);
      expect(CHAIN_TAB, `OptionChainTab must not call ${banned}`).not.toContain(banned);
      expect(TEMPLATE_BAR, `TemplateBar must not call ${banned}`).not.toContain(banned);
    }
  });
});

// 2026-09-30: operator — "remove (l) from chain". The "(L)" no-live-depth
// indicator (shown next to a CE/PE quote when depthAvail is false, meaning
// the price shown is last-traded-price rather than live bid/ask) is removed
// entirely, all 4 occurrences (CE/PE x ATM/non-ATM rows) plus its CSS.
test.describe('Chain "(L)" no-depth indicator removed', () => {
  test('no chain-cell-no-depth markup or CSS remains in OptionChainTab.svelte', () => {
    expect(CHAIN_TAB).not.toContain('chain-cell-no-depth');
    expect(CHAIN_TAB).not.toMatch(/>\(L\)</);
    // depthAvail itself may still exist as a data-shape field (JSDoc type
    // comment) — only the rendered "(L)" indicator and its CSS are removed.
    expect(CHAIN_TAB).not.toMatch(/!ceQ\.depthAvail|!peQ\.depthAvail/);
  });
});

// 2026-09-30: operator — "you can remove rupee symbol from chart y label".
test.describe('Chart Y-axis label has no rupee symbol', () => {
  test('cw-yaxis-label text no longer prefixes priceFmt with ₹', () => {
    const label = CHART_WORKSPACE.match(/class="cw-yaxis-label"[\s\S]{0,300}?<\/text>/)?.[0] ?? '';
    expect(label, 'cw-yaxis-label <text> block').not.toBe('');
    expect(label).not.toContain('₹{priceFmt(tick.v)}');
    expect(label).toContain('{priceFmt(tick.v)}');
  });
});

// 2026-09-30: operator — "price width can be reduced by 20%", narrowed
// a further ~12% same day (9rem -> 7.2rem -> 6.3rem).
test.describe('PRICE input narrowed', () => {
  test('.ot-price-cell .ot-input is 6.3rem (was 9rem / 7.2rem)', () => {
    expect(ORDER_TICKET).toMatch(/\.ot-price-cell \.ot-input \{ width:\s*6\.3rem;\s*\}/);
    expect(ORDER_TICKET).not.toMatch(/\.ot-price-cell \.ot-input \{ width:\s*9rem;\s*\}/);
    expect(ORDER_TICKET).not.toMatch(/\.ot-price-cell \.ot-input \{ width:\s*7\.2rem;\s*\}/);
  });
});

// 2026-09-30: operator — header DTE chip removal ("remove 15d from order
// ticket after symbol. I am reversing my decision.") — reverses the
// earlier same-day "make 15d common across every tab" change.
test.describe('Header DTE chip removed (reversed decision)', () => {
  test('SymbolPanel no longer computes or renders a header days-to-expiry chip', () => {
    expect(SYMBOL_PANEL).not.toMatch(/_headerDte/);
    expect(SYMBOL_PANEL).not.toMatch(/oes-header-dte/);
    expect(SYMBOL_PANEL).not.toMatch(/guessExpiryYmdFromSymbol/);
  });

  test('Chain tab keeps its own Expiry-row DTE chip, unaffected by the reversal', () => {
    expect(CHAIN_TAB).toMatch(/oct-expiry-dte/);
    expect(CHAIN_TAB).toMatch(/_daysToExpiry/);
  });
});

// 2026-09-30: RE-GATED after finding and fixing the actual root cause —
// loadOrderTemplates() (templates.js) permanently caching a transient
// fetch failure as an empty template list. A temporary gate bypass here
// (and a temporary internal-wiring bypass in TemplateBar.svelte, see the
// "RE-WIRED" test above) helped isolate that the bug was NOT in either
// this gate's own logic or TemplateBar's button logic — both now
// reverted to their real, permanent behavior.
test.describe('TemplateBar mount gate — real showDemoTplNote/showTemplateBar conditional restored', () => {
  test('<TemplateBar> is gated by {#if showDemoTplNote}{:else if showTemplateBar}, not unconditional', () => {
    const expiryIdx = CHAIN_TAB.indexOf('oct-toolbar-label">Expiry<');
    expect(expiryIdx, 'Expiry label present').toBeGreaterThan(-1);
    // Search from AFTER the Expiry label — "<TemplateBar" also appears
    // earlier in this file's own JSDoc prop-documentation comment block.
    const templateBarIdx = CHAIN_TAB.indexOf('<TemplateBar', expiryIdx);
    expect(templateBarIdx, '<TemplateBar mount present after Expiry label').toBeGreaterThan(-1);
    expect(templateBarIdx, '<TemplateBar sits within the Expiry toolbar row')
      .toBeLessThan(expiryIdx + 2500);
    const precedingText = CHAIN_TAB.slice(Math.max(0, templateBarIdx - 600), templateBarIdx);
    expect(precedingText).toMatch(/\{#if showDemoTplNote\}/);
    expect(precedingText).toMatch(/\{:else if showTemplateBar\}/);
    expect(CHAIN_TAB).toMatch(/showTemplateBar\s*=\s*false/);
    expect(CHAIN_TAB).toMatch(/showDemoTplNote\s*=\s*false/);
  });
});

// 2026-09-30: Templ toggle batch — (1) remove the disabled gate so the
// button is always clickable, (2) show unconditionally in Chain +
// remember the operator's last on/off/specific-template choice per
// side-scope, persisted to localStorage across sessions.
test.describe('Templ toggle — always-enabled + unconditional-in-Chain + per-scope remembered pref (2026-09-30)', () => {
  test('source: button never renders a `disabled` attribute anywhere in TemplateBar.svelte', () => {
    expect(TEMPLATE_BAR).not.toMatch(/disabled=/);
    expect(TEMPLATE_BAR).not.toContain('_templBtnDisabled');
    expect(TEMPLATE_BAR).not.toContain('_toggleOnDisabled');
  });

  test('source: _readTemplPref/_writeTemplPref exist and are wired into all three onSelect* handlers + the scope-resolution effect', () => {
    expect(SYMBOL_PANEL).toMatch(/const _TEMPL_PREF_KEY = 'ramboq_templ_pref_v1'/);
    expect(SYMBOL_PANEL).toMatch(/function _readTemplPref\(scope\)/);
    expect(SYMBOL_PANEL).toMatch(/function _writeTemplPref\(scope, value\)/);
    // Limits bumped 220 → 600 (2026-09-30, bug-2 fix) — each handler
    // grew a `_templExplicitThisSession = true;` write (+ a comment on
    // the Default handler) marking the operator's explicit in-session
    // choice so a later scope change doesn't silently override it.
    const onSelectDefaultBlock = SYMBOL_PANEL.match(/onSelectDefault=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    const onSelectNoneBlock = SYMBOL_PANEL.match(/onSelectNone=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    const onSelectTemplateBlock = SYMBOL_PANEL.match(/onSelectTemplate=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    expect(onSelectDefaultBlock, 'onSelectDefault handler').not.toBe('');
    expect(onSelectNoneBlock, 'onSelectNone handler').not.toBe('');
    expect(onSelectTemplateBlock, 'onSelectTemplate handler').not.toBe('');
    expect(onSelectDefaultBlock).toMatch(/_writeTemplPref\(_currentScope\(\), 'on'\)/);
    expect(onSelectNoneBlock).toMatch(/_writeTemplPref\(_currentScope\(\), 'none'\)/);
    expect(onSelectTemplateBlock).toMatch(/_writeTemplPref\(_currentScope\(\), id\)/);
    // The scope-resolution effect only READS the pref — it never writes
    // one (writes are confined to the three handlers above).
    // Limit bumped 2400 → 5000 (2026-09-30, bug-2 fix) — see matching
    // note above the sibling `swapEffectBlock` match.
    const effectBlock = SYMBOL_PANEL.match(/let _lastSideScope[\s\S]{0,5000}?\n  \}\);/)?.[0] ?? '';
    expect(effectBlock, 'side-flip/pref-resolution effect').not.toBe('');
    expect(effectBlock).toMatch(/_readTemplPref\(scope\)/);
    expect(effectBlock).not.toMatch(/_writeTemplPref/);
  });

  test('live: a remembered "none" pref for the resolved scope makes the toggle mount OFF (localStorage seeded before load)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.addInitScript(() => {
      // BUY + a non-CE/PE symbol (NIFTY, the underlying — the actual
      // Chain leg picks are CE/PE, but the shell's own scope guess
      // falls through _localSymbol first) resolves to 'buy_any' (see
      // _currentScope()'s fallback ladder). Seed that scope's pref to
      // the explicit opt-out sentinel.
      localStorage.setItem('ramboq_templ_pref_v1', JSON.stringify({ buy_any: 'none' }));
    });
    await _seedNiftyAndOpenChain(page);

    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(toggle).not.toHaveClass(/active/);
  });

  test('live: Templ toggle renders unconditionally on Chain tab (visible + enabled), with nothing remembered', async ({ page }) => {
    await loginAsAdmin(page);
    await page.addInitScript(() => {
      localStorage.removeItem('ramboq_templ_pref_v1');
    });
    await _seedNiftyAndOpenChain(page);

    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(toggle).toBeEnabled();
    await expect(toggle).not.toHaveAttribute('disabled', '');
  });
});

// 2026-09-30: Order Depth / Chain strike background color consistency —
// .ot-depth (Ticket tab's depth ladder) used a generic black overlay
// instead of the app's actual elevation token; .oes-tabs-divider used
// plain white/gray instead of this surface's amber accent family.
//
// Corrected same day (2nd pass): the shared token both were pointed at
// (--algo-bg-elev2) was itself a flat solid navy that read as a visibly
// different (darker) surface from every other card-like surface in the
// app (.algo-card, .bucket-card, chart wrappers — all --card-bg-gradient).
// Both .ot-depth and the Chain grid now reference --card-bg-gradient,
// the app's actual canonical card surface token.
test.describe('Depth ladder + tab-strip divider — surface color consistency (2026-09-30)', () => {
  // Amended same day — operator: after the --card-bg-gradient switch
  // (asserted by the original version of these two tests), the chain
  // grid + depth ladder now read as "almost the same as other areas",
  // too blended into generic .algo-card surfaces. Settled middle
  // ground: --chain-depth-bg (app.css) = --card-bg-gradient + a thin
  // amber wash on top, shared by both surfaces.
  //
  // REVERSED AGAIN same day — operator: "keep the order quote depth in
  // sync with chart background" — the price chart was itself reverted
  // off --chain-depth-bg back to plain --card-bg-gradient the same
  // day, so .ot-depth now follows the chart's token instead of the
  // Chain grid's — the two intentionally diverge again.
  test('.ot-depth background references plain --card-bg-gradient (in sync with the price chart), not --chain-depth-bg or the flat --algo-bg-elev2 hex', () => {
    const rule = ORDER_DEPTH.match(/\.ot-depth\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.ot-depth rule').not.toBe('');
    expect(rule).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
    expect(rule).not.toMatch(/background:\s*var\(--algo-bg-elev2/);
    expect(rule).not.toMatch(/background:\s*rgba\(0,\s*0,\s*0,\s*0\.18\)/);
    // UPDATED (2026-09-30, later same day): the Chain tab's own strike
    // grid wrapper moved OFF --chain-depth-bg onto bare
    // --card-bg-gradient too, per operator: "keep the chain background
    // colors in sync with price chart background..." — .ot-depth and
    // .chain-grid-wrap are now intentionally on the SAME token again,
    // both following the price chart.
    expect(CHAIN_TAB).toMatch(/\.chain-grid-wrap\s*\{[\s\S]*?background:\s*var\(--card-bg-gradient\)/);
  });

  // REVERSED same day — operator: "chain header background should not
  // be same as chain [body]... slight variation for contrast". The
  // header cells referenced their OWN --chain-header-bg token at the
  // time (a stronger amber wash than the body's --chain-depth-bg).
  // UPDATED (2026-09-30, later same day) — operator reported the
  // amber-wash header read as plain "black and gray"; --chain-header-bg
  // was removed (dead token, app.css) in favor of --card-bg-elevated,
  // an actually-lighter navy tier rather than a same-darkness-plus-tint
  // approach. The header/body CONTRAST invariant is unchanged.
  test('chain-th-ce/-pe/-strike header cells reference --card-bg-elevated (distinct from the body\'s --card-bg-gradient), not the flat --algo-bg-elev2 hex', () => {
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = CHAIN_TAB.match(new RegExp(`\\${sel}\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/background:\s*var\(--card-bg-elevated\)/);
      expect(rule).not.toMatch(/background:\s*var\(--chain-header-bg\)/);
      expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
      expect(rule).not.toMatch(/background:\s*var\(--algo-bg-elev2/);
    }
  });

  test('--chain-depth-bg / --chain-header-bg no longer exist in app.css (removed as dead code once every consuming surface moved off the amber-wash idiom)', () => {
    expect(APP_CSS).not.toMatch(/--chain-depth-bg:/);
    expect(APP_CSS).not.toMatch(/--chain-header-bg:/);
  });

  test('--card-bg-elevated (app.css) is defined and distinct from --card-bg-gradient', () => {
    const elevatedRule = APP_CSS.match(/--card-bg-elevated:\s*[\s\S]*?;/)?.[0] ?? '';
    expect(elevatedRule, '--card-bg-elevated declaration').not.toBe('');
    const baseRule = APP_CSS.match(/--card-bg-gradient:\s*[\s\S]*?;/)?.[0] ?? '';
    expect(elevatedRule).not.toBe(baseRule);
  });

  // Live computed-style proof that the gradient cascade actually
  // resolves in the browser (not `none`, which would happen if the
  // --chain-depth-bg token were scoped to a theme class that doesn't
  // reach SymbolPanel's portaled modal) lives in
  // order_ticket_row_height_and_palette.spec.js's "Fix #2" live test —
  // that test already opens the Chain tab with NIFTY seeded (needed for
  // its own TP%/SL% height assertion), so the gradient check piggybacks
  // on the SAME page/navigation there instead of adding a 4th live
  // NIFTY-symbol-search browser test to THIS file. (Empirically, a 4th
  // consecutive live test in this file that types NIFTY and waits on
  // the suggestion dropdown started intermittently timing out —
  // `searchByPrefix()` awaits a per-context `loadInstruments()` full
  // reload every test since Playwright gives each test a fresh browser
  // context with no persisted IndexedDB/cache; this is pre-existing
  // test-infra cost, not a defect in either fix.) The SAME-value-as-
  // each-other + distinct-from-.algo-card live proof (2026-09-30,
  // "middle ground" pass) lives in the same file's new describe block
  // "Fix #1 (middle-ground pass) — chain-depth-bg live parity".

  test('.oes-tabs-divider references the amber accent family, not plain white/gray', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-tabs-divider\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-tabs-divider rule').not.toBe('');
    expect(rule).toMatch(/background:\s*rgba\(251,\s*191,\s*36,\s*0\.18\)/);
    expect(rule).not.toMatch(/background:\s*rgba\(255,\s*255,\s*255,/);
  });
});

test.describe('Fix #3 (2026-09-30 follow-up) — Expiry toolbar row height audit (source)', () => {
  // Original finding (superseded): Select trigger + Template toggle
  // button (both fall through to var(--ctl-h, 1.55rem) with nothing
  // scoped) rendered at 1.55rem, while the DTE chip + TP%/SL%(/Wing)
  // override inputs (explicit 1.4rem) rendered smaller — a real,
  // visible inconsistency in the row (NOT two legitimate tiers as an
  // earlier pass of this fix mistakenly concluded — recounted with the
  // Template toggle in its actual default-ON mount state, the 1.4rem
  // group is the majority: 3-4 controls vs 2). Fixed by scoping
  // --ctl-h: 1.4rem on .oct-toolbar so the two var(--ctl-h, ...)
  // consumers drop to match the row's existing majority, instead of
  // inventing a third value or bumping the minority up.
  test('.oct-toolbar scopes --ctl-h to 1.4rem, matching the DTE chip / TP%-SL% input tier', () => {
    const rule = CHAIN_TAB.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    expect(rule).toMatch(/--ctl-h:\s*1\.4rem/);
  });

  test('.oes-tpl-button and .oct-expiry-pick Select trigger both consume var(--ctl-h, ...) — so the toolbar-scoped token actually reaches them', () => {
    expect(TEMPLATE_BAR).toMatch(/\.oes-tpl-button\s*\{[\s\S]*?height:\s*var\(--ctl-h,\s*1\.55rem\)/);
    // Select.svelte is shared app-wide and intentionally untouched —
    // just confirm it reads the same token name.
    const SELECT = readFileSync(path.join(dir, 'src/lib/Select.svelte'), 'utf8');
    expect(SELECT).toMatch(/\.rbq-select-trigger\s*\{[\s\S]*?min-height:\s*var\(--ctl-h,/);
  });

  // `min-height` is a FLOOR, not a cap — Select's own default padding
  // (0.25rem top+bottom) plus --fs-sm text needs ~24.4px on its own,
  // more than the 1.4rem/22.4px floor just added above, so the floor
  // alone never actually governed and the trigger stayed oversized
  // (live-measured, confirmed before this second pass). Fixed with a
  // scoped padding override — Select.svelte itself is untouched; only
  // Select triggers rendered inside .oct-toolbar (the expiry picker,
  // and TemplateBar's "Specific tmpl" Select in its expand panel) are
  // affected.
  test('.oct-toolbar overrides Select trigger padding-block so the min-height floor actually governs', () => {
    const rule = CHAIN_TAB.match(/\.oct-toolbar :global\(\.rbq-select-trigger\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar :global(.rbq-select-trigger) rule').not.toBe('');
    expect(rule).toMatch(/padding-block:\s*0\.15rem/);
  });

  test('dead-code check: .oct-mode-btn / .oct-controls / .oct-field-mode / .oct-mode-toggle have CSS rules but no matching markup anywhere in the file', () => {
    expect(CHAIN_TAB).toMatch(/\.oct-mode-btn\s*\{/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-controls\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-field-mode\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-mode-toggle\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-mode-btn\b/);
  });
});

test.describe('Fix #1 (middle-ground pass, SUPERSEDED 2026-09-30 later same day) — chain surface background parity', () => {
  // ORIGINAL (middle-ground pass): .chain-grid-wrap and .ot-depth shared
  // a two-layer amber-wash token (--chain-depth-bg) that deliberately
  // differed from a plain .algo-card's single-layer background.
  // SUPERSEDED, in two steps, both 2026-09-30:
  //   1. Operator: "keep the order quote depth in sync with chart
  //      background" — .ot-depth moved to plain --card-bg-gradient,
  //      intentionally diverging from .chain-grid-wrap at the time.
  //   2. Operator: "keep the chain background colors in sync with
  //      price chart background..." — .chain-grid-wrap ALSO moved to
  //      plain --card-bg-gradient, re-converging all three
  //      (.chain-grid-wrap / .ot-depth / .algo-card) onto the exact
  //      same background. The only surface still deliberately DIFFERENT
  //      is the Chain header (.chain-th-ce/-pe/-strike), now on
  //      --card-bg-elevated after the operator reported the header's
  //      former amber-wash token reading as plain "black and gray".
  test('live: .chain-grid-wrap, .ot-depth, and a plain .algo-card all resolve to the IDENTICAL background; the Chain header resolves to a DIFFERENT one; expiry-toolbar controls share one height tier', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChain(page);

    const gridWrap = page.locator('.chain-grid-wrap').first();
    await expect(gridWrap).toBeVisible({ timeout: 15_000 });
    const gridBg = await gridWrap.evaluate((el) => getComputedStyle(el).backgroundImage);

    const headerCell = page.locator('.chain-th-ce').first();
    await expect(headerCell).toBeVisible({ timeout: 10_000 });
    const headerBg = await headerCell.evaluate((el) => getComputedStyle(el).backgroundImage);

    // Probe element for the plain canonical card surface — injected
    // rather than relying on one existing on the page, so this doesn't
    // depend on any particular .algo-card being mounted at the time.
    const probeBg = await page.evaluate(() => {
      const el = document.createElement('div');
      el.className = 'algo-card';
      el.style.position = 'fixed';
      el.style.top = '-9999px';
      document.body.appendChild(el);
      const bg = getComputedStyle(el).backgroundImage;
      el.remove();
      return bg;
    });

    expect(gridBg, '.chain-grid-wrap background-image').toMatch(/^linear-gradient/);
    expect(gridBg, '.chain-grid-wrap must now match a plain .algo-card background (both plain --card-bg-gradient)').toBe(probeBg);
    expect(headerBg, '.chain-th-ce must differ from a plain .algo-card background (--card-bg-elevated, a genuinely different gradient)').not.toBe(probeBg);

    // Expiry-toolbar row height audit (2026-09-30 follow-up) — piggybacks
    // on this same Chain-tab navigation instead of adding a 5th live
    // NIFTY-symbol-search test to this file (this file's own comments
    // above already document intermittent timeouts past the 4th
    // consecutive one). Measured in BOTH toggle states — TP%/SL% inputs
    // only exist while ON, so the OFF state only has the always-present
    // trio (Select trigger / Template button / DTE chip).
    async function _measureToolbarHeights() {
      const heights = {};
      for (const sel of ['.oct-expiry-pick .rbq-select-trigger', '.oes-tpl-button', '.oct-expiry-dte']) {
        const loc = page.locator(sel).first();
        await expect(loc, sel).toBeVisible({ timeout: 10_000 });
        heights[sel] = await loc.evaluate((el) => el.getBoundingClientRect().height);
      }
      const tpInput = page.locator('.oes-basket-tpl-param > input').first();
      if (await tpInput.count()) {
        await expect(tpInput).toBeVisible({ timeout: 5_000 });
        heights['.oes-basket-tpl-param > input'] = await tpInput.evaluate((el) => el.getBoundingClientRect().height);
      }
      return heights;
    }
    function _assertTight(heights, label) {
      const values = Object.values(heights);
      const maxDelta = Math.max(...values) - Math.min(...values);
      expect(maxDelta, `${label} — expiry-toolbar control heights should be within 1px of each other: ${JSON.stringify(heights)}`).toBeLessThanOrEqual(1);
    }

    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 10_000 });
    const mountedActive = await toggle.evaluate((el) => el.classList.contains('active'));

    _assertTight(await _measureToolbarHeights(), mountedActive ? 'ON (default mount)' : 'OFF (default mount)');

    await toggle.click();
    await page.waitForFunction(
      (wasActive) => {
        const el = document.querySelector('.oes-tpl-button');
        return !!el && el.classList.contains('active') !== wasActive;
      },
      mountedActive,
      { timeout: 10_000 }
    );
    _assertTight(await _measureToolbarHeights(), mountedActive ? 'OFF (after toggle click)' : 'ON (after toggle click)');

    const ticketTab = page.getByRole('tab', { name: /Ticket/i }).first();
    await ticketTab.click();
    const depth = page.locator('.ot-depth').first();
    await expect(depth).toBeVisible({ timeout: 15_000 });
    const depthBg = await depth.evaluate((el) => getComputedStyle(el).backgroundImage);

    // .ot-depth, .chain-grid-wrap, and a plain .algo-card all follow
    // the same plain --card-bg-gradient token now (see this
    // describe block's own header comment for the two supersession
    // steps that led here) — all three must resolve identically.
    expect(depthBg, '.ot-depth must match .chain-grid-wrap (both plain --card-bg-gradient)').toBe(gridBg);
    expect(depthBg, '.ot-depth must match a plain .algo-card background (both plain --card-bg-gradient)').toBe(probeBg);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// 2026-09-30 audit fixes — Bug 1 (scope-detection always resolving to the
// root symbol on Chain, never buy_option/sell_option) and Bug 2 (a
// remembered template pref could silently flip Templ OFF mid-order on a
// scope change, discarding the operator's just-made explicit choice).
// ═══════════════════════════════════════════════════════════════════════

/**
 * Poll for an ENABLED option CE/PE buy or sell button in the live chain
 * grid (disabled === no live/frozen bid+ask for that strike — see
 * OptionChainTab.svelte's `disabled={!(q?.bid > 0 || q?.ask > 0)}`).
 * Returns the Locator, or null if none appear within the poll window —
 * callers soft-skip rather than fail, matching this repo's established
 * convention for chain-quote-dependent live tests (chain_tab_api_driven
 * .spec.js, option_chain_order.spec.js: "market-close renders gracefully,
 * tests skip rather than fail" — whether frozen EOD quotes populate
 * bid/ask for any given strike varies run to run and is not something a
 * test should assert control over).
 * @param {import('@playwright/test').Page} page
 * @param {'buy'|'sell'} side
 * @param {'CE'|'PE'} optType
 */
async function _findEnabledOptionButton(page, side, optType) {
  const locator = page.locator(`.chain-btn-${side}[title*="${optType}"]:not([disabled])`);
  for (let i = 0; i < 6; i++) {
    if (await locator.count()) return locator.first();
    await page.waitForTimeout(500);
  }
  return null;
}

test.describe('Bug 1 (2026-09-30) — Chain scope-detection prefers the leg symbol over the root symbol', () => {
  test('source: _currentScope() prefers _focusedLeg/last-basket-leg sym ahead of _localSymbol whenever a leg exists or Chain is active', () => {
    // Limit matches the bump applied above in the earlier fallback test
    // (400 → 1800) for the same function.
    const scopeFn = SYMBOL_PANEL.match(/function _currentScope\(\) \{[\s\S]{0,1800}?\n  \}/)?.[0] ?? '';
    expect(scopeFn, '_currentScope() helper').not.toBe('');
    // The leg symbol (option contract, e.g. "...CE"/"...PE") must be
    // computed and consulted AHEAD of _localSymbol (root symbol on
    // Chain — _parseRoot strips from the first digit) whenever there's
    // a real leg to derive it from.
    expect(scopeFn).toMatch(/const legSym = \(_focusedLeg\?\.sym \|\| ''\)/);
    expect(scopeFn).toMatch(/preferLeg = _activeTab === 'chain' \|\| basketLegs\.length > 0/);
    // Fallback ladder preserved — legSym still falls back to
    // _localSymbol when preferLeg is true and no leg symbol exists yet,
    // and _localSymbol still falls back to legSym on Ticket with legs.
    expect(scopeFn).toMatch(/preferLeg\s*\n\s*\? \(legSym \|\| \(_localSymbol \|\| ''\)\.trim\(\)\)\s*\n\s*: \(\(_localSymbol \|\| ''\)\.trim\(\) \|\| legSym\)/);
  });

  test('live: staging a SELL CE (or PE) option leg on Chain resolves the Templ default to a sell_option-scoped template (wing-eligible), not the buy_any/sell_any fallback', async ({ page }) => {
    await loginAsAdmin(page);
    await page.addInitScript(() => localStorage.removeItem('ramboq_templ_pref_v1'));
    await _seedNiftyAndOpenChain(page);

    const tplBtn = page.locator('.oes-tpl-button').first();
    await expect(tplBtn).toBeVisible({ timeout: 15_000 });
    const titleBeforeAnyLeg = await tplBtn.getAttribute('title');
    // Cold mount, no legs yet, root symbol NIFTY (non-option) → buy_any
    // scope. Confirms the baseline this test flips away from.
    expect(titleBeforeAnyLeg || '', 'cold-mount Templ title').toMatch(/buy_any/);

    const sellCe = await _findEnabledOptionButton(page, 'sell', 'CE');
    const sellPe = sellCe ? null : await _findEnabledOptionButton(page, 'sell', 'PE');
    const sellBtn = sellCe || sellPe;
    if (!sellBtn) {
      test.skip(true, 'No enabled (live/frozen-quote) CE or PE sell button found — chain quotes unavailable right now');
      return;
    }
    await sellBtn.scrollIntoViewIfNeeded();
    await sellBtn.click({ force: true });
    await page.waitForTimeout(800);

    const pill = page.locator('.oes-basket-pill-sell').first();
    await expect(pill, 'a SELL leg pill must land in the basket').toBeVisible({ timeout: 5_000 });

    // Bug-1 fix's observable effect: _sideAwareDefault now resolves a
    // sell_option-scoped template — the description below is this
    // environment's actual sell_option is_default template body, which
    // explicitly mentions the protective-wing auto-pick (the ONE thing
    // that's only ever true for sell_option, per templateScope.js's own
    // docstring: "the only scope that wants a protective wing").
    await expect(tplBtn).toHaveAttribute('title', /wing/i, { timeout: 5_000 });
    const titleAfter = await tplBtn.getAttribute('title');
    expect(titleAfter || '', 'post-SELL-leg Templ title').not.toMatch(/buy_any|non-option/i);
  });
});

test.describe('Bug 2 (2026-09-30) — explicit in-session Templ choice survives a mid-order scope change', () => {
  test('source: _templExplicitThisSession flag gates the remembered-pref lookup and is set by all three onSelect* handlers', () => {
    expect(SYMBOL_PANEL).toMatch(/let _templExplicitThisSession = \$state\(false\)/);
    // The pref lookup is skipped (treated as `undefined`) once the
    // operator has made an explicit choice this session.
    expect(SYMBOL_PANEL).toMatch(
      /const pref = _templExplicitThisSession \? undefined : _readTemplPref\(scope\)/
    );
    const onSelectDefaultBlock = SYMBOL_PANEL.match(/onSelectDefault=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    const onSelectNoneBlock = SYMBOL_PANEL.match(/onSelectNone=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    const onSelectTemplateBlock = SYMBOL_PANEL.match(/onSelectTemplate=\{[\s\S]{0,600}?\}\}/)?.[0] ?? '';
    expect(onSelectDefaultBlock).toMatch(/_templExplicitThisSession = true/);
    expect(onSelectNoneBlock).toMatch(/_templExplicitThisSession = true/);
    expect(onSelectTemplateBlock).toMatch(/_templExplicitThisSession = true/);
  });

  test('source: the flag resets at every genuine fresh-order boundary (clearBasket + submitBasket success), not mid-order', () => {
    const clearBasketFn = SYMBOL_PANEL.match(/function clearBasket\(\) \{[\s\S]{0,1200}?\n  \}/)?.[0] ?? '';
    expect(clearBasketFn, 'clearBasket() body').not.toBe('');
    expect(clearBasketFn).toMatch(/_templExplicitThisSession = false/);
    const submitSuccessBlock = SYMBOL_PANEL.match(/basket cleared`;[\s\S]{0,600}?_stickyResultMsg = msg;/)?.[0] ?? '';
    expect(submitSuccessBlock, 'submitBasket() success branch').not.toBe('');
    expect(submitSuccessBlock).toMatch(/_templExplicitThisSession = false/);
  });

  test('live: Templ toggled ON explicitly for a BUY option leg stays ON after adding a SELL option leg, even with a stale remembered "none" for the new scope', async ({ page }) => {
    await loginAsAdmin(page);
    await page.addInitScript(() => {
      // A completely unrelated prior session explicitly turned Templ
      // OFF for sell_option. This must NOT silently apply mid-order
      // once the operator has made their own explicit choice this
      // session (the Bug-2 fix under test).
      localStorage.setItem('ramboq_templ_pref_v1', JSON.stringify({ sell_option: 'none' }));
    });
    await _seedNiftyAndOpenChain(page);

    const buyCe = await _findEnabledOptionButton(page, 'buy', 'CE');
    if (!buyCe) {
      test.skip(true, 'No enabled (live/frozen-quote) CE buy button found — chain quotes unavailable right now');
      return;
    }
    await buyCe.scrollIntoViewIfNeeded();
    await buyCe.click({ force: true });
    await page.waitForTimeout(600);

    const tplBtn = page.locator('.oes-tpl-button').first();
    await expect(tplBtn).toBeVisible({ timeout: 10_000 });
    // Explicit off → on cycle so the operator's choice is unambiguous
    // (this session's mount may have already auto-selected a default —
    // clicking twice guarantees an EXPLICIT onSelectNone then
    // onSelectDefault call, setting `_templExplicitThisSession`).
    await tplBtn.click();
    await expect(tplBtn).not.toHaveClass(/active/, { timeout: 3_000 });
    await tplBtn.click();
    await expect(tplBtn).toHaveClass(/active/, { timeout: 3_000 });

    const sellPe = await _findEnabledOptionButton(page, 'sell', 'PE');
    if (!sellPe) {
      test.skip(true, 'No enabled (live/frozen-quote) PE sell button found — chain quotes unavailable right now');
      return;
    }
    await sellPe.scrollIntoViewIfNeeded();
    await sellPe.click({ force: true });
    await page.waitForTimeout(800);

    // Bug-2 assertion: the scope flip (buy_option → sell_option) must
    // NOT silently apply the seeded stale 'none' pref over the
    // operator's just-made explicit choice.
    await expect(tplBtn, 'Templ must stay ON across the scope-changing leg add').toHaveClass(/active/);
  });
});

// 2026-09-30 audit — CE/PE quote-vs-stepper overflow at narrow (320-375px)
// widths. .chain-grid-wrap's overflow-x:hidden is a deliberate clipping
// backstop for table-layout:fixed (see that rule's own comment) — but
// without ANY shrink allowance, a CE/PE column's real content (quote text
// + gap + +/- stepper pair) could exceed the column's fixed % width and
// get clipped, sometimes eating into the steppers themselves rather than
// just the quote text. Fixed with `.chain-cell-quote { min-width: 0;
// overflow: hidden; text-overflow: ellipsis; }` (was a fixed `min-width:
// 3.4rem` floor that could never give way) + `.chain-side-action {
// flex-shrink: 0; }` so the buttons are never the side that gives.
test.describe('CE/PE quote/stepper overflow fix — min-width:0 shrink, not overflow-x:auto (2026-09-30 audit)', () => {
  test('source: .chain-cell-quote allows shrink (min-width: 0) with ellipsis truncation, .chain-side-action never shrinks', () => {
    const quoteRule = CHAIN_TAB.match(/\.chain-cell-quote\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(quoteRule, '.chain-cell-quote rule').not.toBe('');
    expect(quoteRule).toMatch(/min-width:\s*0/);
    expect(quoteRule).toMatch(/overflow:\s*hidden/);
    expect(quoteRule).toMatch(/text-overflow:\s*ellipsis/);
    // The old fixed floor must be gone — a 3.4rem min-width would defeat
    // the shrink allowance above it in source order. Anchored to an
    // actual declaration line (not the explanatory comment above it,
    // which mentions the old value in prose).
    expect(quoteRule).not.toMatch(/^\s*min-width:\s*3\.4rem;/m);

    const actionRule = CHAIN_TAB.match(/\.chain-side-action\s*\{[^}]*\}/)?.[0] ?? '';
    expect(actionRule, '.chain-side-action rule').not.toBe('');
    expect(actionRule).toMatch(/flex-shrink:\s*0/);

    // .chain-grid-wrap's overflow-x:hidden is deliberately UNCHANGED —
    // CE cells use justify-content:flex-end (content right-anchored), so
    // overflow-x:auto would let content spill past the unreachable LEFT
    // edge on that side; the min-width:0 shrink approach was chosen
    // instead specifically to avoid that asymmetry.
    const wrapRule = CHAIN_TAB.match(/\.chain-grid-wrap\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(wrapRule).toMatch(/overflow-x:\s*hidden/);
  });

  // Live, worst-case content injection — real quotes may be short/empty
  // in a test environment, so this forces the exact squeeze scenario the
  // audit flagged (long bid/ask digits) rather than hoping live data
  // happens to be wide enough to exercise the fix.
  for (const width of [320, 340, 375]) {
    test(`live @ ${width}px: injected long CE/PE quotes never overlap or cover the +/- steppers`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await loginAsAdmin(page);
      await _seedNiftyAndOpenChain(page);

      const ceRow = page.locator('.chain-cell-row-ce').first();
      const peRow = page.locator('.chain-cell-row-pe').first();
      await expect(ceRow).toBeVisible({ timeout: 15_000 });
      await expect(peRow).toBeVisible({ timeout: 10_000 });

      // Inject worst-case long bid/ask text directly into the first
      // CE and PE quote cells (real long-symbol strike scenario).
      await page.evaluate(() => {
        const ceQuote = document.querySelector('.chain-cell-row-ce .chain-cell-quote');
        const peQuote = document.querySelector('.chain-cell-row-pe .chain-cell-quote');
        const set = (root, cls, txt) => { const el = root?.querySelector(cls); if (el) el.textContent = txt; };
        set(ceQuote, '.chain-cell-bid', '123456789.50');
        set(ceQuote, '.chain-cell-ask', '123456789.25');
        set(peQuote, '.chain-cell-bid', '123456789.50');
        set(peQuote, '.chain-cell-ask', '123456789.25');
      });

      const wrapBox = await page.locator('.chain-grid-wrap').first().boundingBox();
      const ceQuoteBox = await ceRow.locator('.chain-cell-quote').boundingBox();
      const ceActionBox = await ceRow.locator('.chain-side-action').boundingBox();
      const peQuoteBox = await peRow.locator('.chain-cell-quote').boundingBox();
      const peActionBox = await peRow.locator('.chain-side-action').boundingBox();

      function intersects(a, b) {
        return !(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y);
      }
      expect(intersects(ceQuoteBox, ceActionBox), 'CE quote must not overlap its own +/- steppers').toBe(false);
      expect(intersects(peQuoteBox, peActionBox), 'PE quote must not overlap its own +/- steppers').toBe(false);
      // Quote box must stay within the wrap's horizontal bounds (the
      // clipping backstop still applies — this proves the shrink, not
      // the backstop, is what's actually preventing the overlap above).
      expect(ceQuoteBox.x, 'CE quote left edge within wrap').toBeGreaterThanOrEqual(wrapBox.x - 1);
      expect(peQuoteBox.x + peQuoteBox.width, 'PE quote right edge within wrap').toBeLessThanOrEqual(wrapBox.x + wrapBox.width + 1);

      // elementFromPoint at each stepper's own center must resolve to
      // that actual button — proves the steppers are truly reachable/
      // clickable, not just visually un-overlapped underneath something.
      const buttons = await page.locator('.chain-row').first().locator('.chain-btn').all();
      for (const btn of buttons) {
        const box = await btn.boundingBox();
        const cls = await btn.getAttribute('class');
        const cx = box.x + box.width / 2;
        const cy = box.y + box.height / 2;
        const hitClass = await page.evaluate(({ cx, cy }) => document.elementFromPoint(cx, cy)?.className ?? null, { cx, cy });
        expect(String(hitClass), `stepper (${cls}) must be the hit target at its own center`).toContain('chain-btn');
      }
    });
  }
});

// 2026-09-30 audit — .oct-tpl-demo-note's flex-shrink:0 was flagged as a
// dead/incorrect rule (the wrap-to-its-own-line behavior it was presumed
// to control is actually governed by the item's max-content width vs
// the CURRENT line's remaining space, independent of flex-shrink).
// Removed after an isolated before/after render confirmed it's a true
// no-op at every realistic phone viewport (320/375/412px) and an
// improvement below that (180/250px — see the rule's own comment).
test.describe('.oct-tpl-demo-note flex-shrink:0 removed (dead rule, 2026-09-30 audit)', () => {
  test('source: flex-shrink: 0 is gone from .oct-tpl-demo-note', () => {
    const rule = CHAIN_TAB.match(/\.oct-tpl-demo-note\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-tpl-demo-note rule').not.toBe('');
    // Anchored to an actual declaration line — the removal comment
    // itself mentions "flex-shrink: 0" / "flex-shrink:0" in prose.
    expect(rule).not.toMatch(/^\s*flex-shrink:\s*0;/m);
  });
});
