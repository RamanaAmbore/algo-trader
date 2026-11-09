/**
 * chain_ticket_severance_and_mobile_fixes.spec.js
 *
 * Source-scan guards for the 2026-09-29 batch of fixes:
 *
 *  1. (REMOVED T3.3, bracket-removal, 2026-11) Ticket/Chain template
 *     architectural severance — tested Bracket's Default/None templateId
 *     wiring, now deleted entirely along with TemplateBar.svelte. See
 *     CLAUDE.md's "Order Ticket / Bracket-template re-wire" history.
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
 *  7. (REMOVED T3.3) Templ toggle relocation + TemplateBar.svelte's
 *     primary-control history — TemplateBar.svelte is deleted entirely.
 *  8. CE/PE header text-align flipped to match where the +/- buttons
 *     actually sit; header border-bottom darkened; chain font-size
 *     reset to the platform's normal --fs-md/--fs-sm scale (0.78rem
 *     override removed); +/- buttons get a solid rest-state border +
 *     one-tier-stronger background.
 *  9. (REMOVED T3.3) Chain leg badges' truncated template short-label
 *     suffix — the badge now shows only the bare lots count.
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
 * Same navigation as `_seedNiftyAndOpenChain`, but waits for a real,
 * fully-loaded strike chain (>=15 rows) before returning — needed for the
 * inline-mode height-cap regression test below, which only reproduces with
 * genuine chain content (an empty/loading chain gives `.oct-root` nothing
 * to overflow, making the comparison meaningless).
 * @param {import('@playwright/test').Page} page
 */
async function _seedNiftyAndOpenChainWithRows(page) {
  await _seedNiftyAndOpenChain(page);
  await expect(async () => {
    const n = await page.locator('.chain-row').count();
    expect(n, 'strike rows loaded').toBeGreaterThanOrEqual(15);
  }).toPass({ timeout: 20_000 });
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

// The entire "Ticket/Chain template severance" describe block (Bracket's
// Default/None templateId wiring across SymbolPanel/OrderTicket/
// OptionChainTab/TemplateBar.svelte) was removed here — T3.3,
// bracket-removal, 2026-11. `TemplateBar.svelte` is deleted; there is no
// surviving Bracket feature for these tests to guard.

test.describe('Mobile chain height cap REMOVED (2026-09-30 audit) + desktop row gap', () => {
  // REGRESSION FOLLOW-UP (2026-09-30, same day) — removing
  // OptionChainTab.svelte's own max-height cap was correct for the MODAL
  // path (verified below: live popup measurement showed .chain-grid-wrap
  // stays ~140-290px with or without the cap, because `.oes-body`'s own
  // fixed-height flex-shrink + overflow-y:auto one level up already
  // bounds it there) but broke the INLINE path (`/orders` page's Order
  // Entry card, `.oes-modal-inline`), which is DELIBERATELY content-driven
  // with no fixed-height ancestor. Without any cap at all, a real NIFTY
  // chain rendered at its full ~2780px content height right in the page
  // flow, burying the Chases/Order Activity cards ~2500px further down
  // the page than before — operator-reported as "the chain is fully
  // taking available height of viewport ... order status chips and cards
  // are not visible. it is only on mobile." The existing live popup test
  // below never caught this: its own comment (`_openPopupChainOnDashboard`
  // docblock) explicitly flags the inline panel as architecturally
  // different and "trivially satisfied" by the same assertion — correct,
  // but it meant the inline path had NO regression coverage of its own.
  // Fix: SymbolPanel.svelte re-introduces a mobile-only max-height cap
  // (16rem, matching the original pre-audit value) scoped to
  // `.oes-modal-inline` specifically — the modal path keeps the full-
  // height improvement from this same commit.
  test('live: inline /orders Chain tab stays height-capped on mobile — Activity card lands near the fold, not thousands of px down', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChainWithRows(page);

    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 10_000 });
    const wrapBox = await wrap.boundingBox();
    expect(wrapBox, '.chain-grid-wrap box').toBeTruthy();
    // The regression ballooned this to ~2780px (full table content height,
    // no cap at all). A capped, scrollable grid must stay well under that.
    expect(wrapBox.height, 'chain-grid-wrap must stay capped, not grow to full table height')
      .toBeLessThanOrEqual(300);

    const scrollInfo = await wrap.evaluate((el) => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }));
    expect(scrollInfo.scrollHeight, 'grid content must exceed its own box (real internal scroll), proving the cap is active, not coincidental')
      .toBeGreaterThan(scrollInfo.clientHeight);

    // The Activity card (OrderBook — "order status chips and cards" in
    // the operator's report) is a sibling further down the /orders page,
    // not nested inside the Order Entry card. Confirm it lands at a
    // reachable position, not buried behind thousands of px of chain grid.
    const activity = page.locator('.bucket-card-activity').first();
    await expect(activity).toBeVisible({ timeout: 10_000 });
    const activityBox = await activity.boundingBox();
    expect(activityBox, '.bucket-card-activity box').toBeTruthy();
    // The regression pushed this to y≈3126 on a 667px-tall viewport
    // (page scrollHeight 3352px). A bounded chain keeps it within a
    // couple of screenfuls of the top, not buried behind ~5 phone-screens
    // of near-empty chain-grid space.
    expect(activityBox.y, 'Activity card must not be buried thousands of px down the page')
      .toBeLessThanOrEqual(900);
  });

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

  // UPDATED (2026-09-30, same-day regression fix) — this test originally
  // asserted `max-height: none` using `_seedNiftyAndOpenChain`, which
  // drives the INLINE `/orders` panel, not the modal. That assertion was
  // itself the bug: OptionChainTab.svelte's own cap is correctly gone
  // (still true, and still asserted via the source-scan test above), but
  // the INLINE path needed its OWN cap re-introduced one level up in
  // SymbolPanel.svelte (`.oes-modal-inline .oes-body :global(.chain-grid-
  // wrap)`) once it turned out that path has no fixed-height ancestor to
  // bound it — see the "live: inline /orders Chain tab stays height-
  // capped..." test above for the full regression story. Split into two
  // explicit assertions so neither mode's invariant masks the other's.
  test('live: .chain-grid-wrap resolves max-height: none in MODAL mode at a mobile viewport (412px)', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await _openPopupChainOnDashboard(page);

    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 15_000 });
    await expect(wrap).toHaveCSS('max-height', 'none');
  });

  test('live: .chain-grid-wrap resolves a capped max-height (256px) in INLINE /orders mode at a mobile viewport (412px)', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await _seedNiftyAndOpenChain(page);

    const wrap = page.locator('.chain-grid-wrap').first();
    await expect(wrap).toBeVisible({ timeout: 15_000 });
    await expect(wrap).toHaveCSS('max-height', '256px');
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
    // Pre-existing stale assertion (predates T3.3, unrelated to bracket
    // removal — confirmed via empty `git diff` on this block). A later,
    // separate 2026-09-30 operator change moved the row divider into the
    // BASE `.chain-row > td` rule (`border-bottom: 1px solid ...`)
    // instead of overriding it to `none` in this desktop-only block, so
    // the override here no longer carries a border-bottom line at all.
    const desktopBlock = CHAIN_TAB.match(/@media \(min-width: 640px\) \{\s*\.chain-row > td \{[\s\S]{0,900}?\n    \}/)?.[0] ?? '';
    expect(desktopBlock, 'desktop .chain-row > td override block').not.toBe('');
    expect(desktopBlock).toMatch(/padding-top:\s*0\.3rem/);
    expect(desktopBlock).toMatch(/padding-bottom:\s*0\.3rem/);
    expect(CHAIN_TAB).toMatch(/\.chain-row > td \{[\s\S]{0,600}?border-bottom:\s*1px solid/);
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

test.describe('CE/PE header alignment + palette normalization (2026-09-30, text-align flipped then reversed back same day)', () => {
  test('CE header text-align RIGHT, PE header text-align LEFT — back to original (2026-09-30, operator flipped to left/right then said "reverse ce and re label alignment")', () => {
    const ceRule = CHAIN_TAB.match(/\.chain-th-ce\s*\{[^}]*\}/)?.[0] ?? '';
    const peRule = CHAIN_TAB.match(/\.chain-th-pe\s*\{[^}]*\}/)?.[0] ?? '';
    expect(ceRule, '.chain-th-ce rule').not.toBe('');
    expect(peRule, '.chain-th-pe rule').not.toBe('');
    expect(ceRule).toMatch(/text-align:\s*right/);
    expect(peRule).toMatch(/text-align:\s*left/);
    // The row-content alignment (+/- buttons toward Strike column) must
    // stay untouched — a deliberate, explicitly-commented layout choice,
    // never part of either text-align change (operator scoped both to
    // "ce label"/"pe label" specifically).
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-ce\s*\{\s*justify-content:\s*flex-end;\s*\}/);
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-pe\s*\{\s*justify-content:\s*flex-start;\s*\}/);
  });

  test('no font-size: 0.78rem declaration remains anywhere in the chain grid CSS', () => {
    expect(CHAIN_TAB).not.toMatch(/font-size:\s*0\.78rem/);
  });

  test('chain header cells have an amber bottom edge via box-shadow:inset, now the shared --algo-amber-border-soft token (SUPERSEDED 2026-09-30, six times — border-bottom replaced by box-shadow to fix a repaint bug, dialed through several one-off alphas, a top edge added then removed, finally reused verbatim from the Legs grid header)', () => {
    // Was border-bottom (brightened from 0.18 to 0.35 alpha same day),
    // then changed to box-shadow: inset (operator: "again the border
    // shows and disappears" / "...in the money calls and puts, it
    // disappears") — sticky <th> + border-collapse:collapse is a known
    // Chrome/WebKit bug where the border drops on a big repaint; the
    // ITM/OTM background-wash switch-on (elsewhere in this file) is
    // exactly that kind of repaint. box-shadow isn't part of table
    // border-collapse semantics, so it's immune.
    // Final pass (operator: "you can use legs grid header decoration
    // like background, borders, etc to chain and order ticket header.
    // text color can remain the same") replaced the one-off rgba()
    // alpha with the shared --algo-amber-border-soft token (matching
    // .cand-headrow in derivatives/+page.svelte) and dropped the top
    // edge added earlier the same session — the Legs grid header
    // never had one, so this is back to a single box-shadow layer.
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = CHAIN_TAB.match(new RegExp(`\\.${sel.slice(1)}\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule, `${sel} box-shadow present`).toMatch(/box-shadow:/);
      expect(rule, `${sel} box-shadow bottom-edge token`).toMatch(/inset 0 -1px 0 var\(--algo-amber-border-soft\)/);
      expect(rule, `${sel} must not carry a top edge`).not.toMatch(/inset 0 1px 0/);
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

// "Chain leg badge — template short-label suffix" and "TemplateBar —
// single toggle button replaces the primary dropdown/pill" describe
// blocks removed here — T3.3, bracket-removal, 2026-11. Both tested
// TemplateBar.svelte-only behaviour (now deleted).

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
    // Pre-existing stale assertion (predates T3.3, confirmed via git blame
    // on 12c5d07d) — the real call has carried a second `basketMode2` arg
    // since before this sprint touched the file.
    expect(fn).toMatch(/await placeBasket\(groups,\s*basketMode2\)/);
    // No direct broker/order-placement call bypassing the existing
    // placeBasket/placeTicketOrder API-layer helpers was introduced by
    // this batch of changes.
    for (const banned of ['apply_plan_live', 'broker.place_order', 'broker.place_gtt']) {
      expect(SYMBOL_PANEL, `SymbolPanel must not call ${banned}`).not.toContain(banned);
      expect(CHAIN_TAB, `OptionChainTab must not call ${banned}`).not.toContain(banned);
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
// "TemplateBar mount gate" and "Templ toggle — always-enabled +
// unconditional-in-Chain + per-scope remembered pref" describe blocks
// removed here — T3.3, bracket-removal, 2026-11. Both tested
// TemplateBar.svelte-only behaviour, including the `_readTemplPref`/
// `_writeTemplPref` localStorage mechanism, which is also gone (see
// SymbolPanel.svelte's one-time stale-key cleanup in its onMount).

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
  test('chain-th-ce/-pe/-strike header cells reuse the Legs grid two-layer background (distinct from the body\'s --card-bg-gradient), not the flat --algo-bg-elev2 hex (2026-09-30, operator: "you can use legs grid header decoration like background, borders, etc to chain and order ticket header")', () => {
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = CHAIN_TAB.match(new RegExp(`\\${sel}\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule).toMatch(/background:\s*linear-gradient\(rgba\(15,23,42,0\.65\), rgba\(15,23,42,0\.65\)\),\s*#1d2a44/);
      expect(rule).not.toMatch(/background:\s*var\(--chain-header-bg\)/);
      expect(rule).not.toMatch(/background:\s*var\(--chain-depth-bg\)/);
      expect(rule).not.toMatch(/background:\s*var\(--algo-bg-elev2/);
      expect(rule).not.toMatch(/background:\s*var\(--card-bg-elevated\)/);
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

  // `min-height` is a FLOOR, not a cap — Select's own default padding
  // (0.25rem top+bottom) plus --fs-sm text needs ~24.4px on its own,
  // more than the 1.4rem/22.4px floor just added above, so the floor
  // alone never actually governed and the trigger stayed oversized
  // (live-measured, confirmed before this second pass). Fixed with a
  // scoped padding override — Select.svelte itself is untouched; only
  // Select triggers rendered inside .oct-toolbar (the expiry picker)
  // are affected.
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
    // consecutive one). T3.3 (bracket-removal, 2026-11): the Template
    // toggle (.oes-tpl-button) and its TP%/SL% inputs are gone; the
    // always-present trio is now just Select trigger / DTE chip /
    // Spread% input (OptionChainTab's own, replacing TemplateBar's).
    async function _measureToolbarHeights() {
      const heights = {};
      for (const sel of ['.oct-expiry-pick .rbq-select-trigger', '.oct-expiry-dte', '.oct-spread-field > input']) {
        const loc = page.locator(sel).first();
        await expect(loc, sel).toBeVisible({ timeout: 10_000 });
        heights[sel] = await loc.evaluate((el) => el.getBoundingClientRect().height);
      }
      return heights;
    }
    function _assertTight(heights, label) {
      const values = Object.values(heights);
      const maxDelta = Math.max(...values) - Math.min(...values);
      expect(maxDelta, `${label} — expiry-toolbar control heights should be within 1px of each other: ${JSON.stringify(heights)}`).toBeLessThanOrEqual(1);
    }

    _assertTight(await _measureToolbarHeights(), 'Chain tab toolbar');

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

// "Bug 1" and "Bug 2" describe blocks removed here — T3.3,
// bracket-removal, 2026-11. Both guarded `_currentScope()`'s Templ-pref
// resolution and the `_templExplicitThisSession` flag — `_currentScope()`
// itself, the Templ-pref localStorage mechanism, and that flag are all
// deleted along with Bracket/TemplateBar entirely. `_findEnabledOptionButton`
// had no other callers in this file once these two blocks were removed,
// so it is gone too.

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
// ".oct-tpl-demo-note flex-shrink:0 removed", "Template expand toggle",
// and "TP%/SL% param inputs restyled" describe blocks removed here —
// T3.3, bracket-removal, 2026-11. All three were TemplateBar.svelte-only
// (the demo note, the Bracket expand-panel chevron, and the Bracket
// TP%/SL% override inputs), now deleted along with the component.

test.describe('Basket leg pill background uses the stronger -22 token, not the faint -14 rest tier (operator: "order info card background the text is not very user friendly")', () => {
  test('.oes-basket-pill-buy/-sell use var(--c-long-22)/var(--c-short-22)', () => {
    const buyRule = SYMBOL_PANEL.match(/\.oes-basket-pill-buy\s*\{[^}]*\}/)?.[0] ?? '';
    const sellRule = SYMBOL_PANEL.match(/\.oes-basket-pill-sell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(buyRule).toMatch(/background:\s*var\(--c-long-22\)/);
    expect(sellRule).toMatch(/background:\s*var\(--c-short-22\)/);
    // Regression guard — must not have silently reverted to the faint
    // rest-state tier this fix moved away from.
    expect(buyRule).not.toMatch(/background:\s*var\(--c-long-14\)/);
    expect(sellRule).not.toMatch(/background:\s*var\(--c-short-14\)/);
  });
});
