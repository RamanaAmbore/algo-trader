/**
 * order_ticket_row_height_and_palette.spec.js
 *
 * Visual-consistency fixes in the order-entry modal (2026-09-30, "four
 * confirmed UI-consistency fixes" batch). All four are visual-only — no
 * functional/business-logic changes. This file covers fixes #1, #2, and
 * #4 with source-scan + live computed-style assertions; fix #3 (chain
 * grid / order-depth background palette) is covered inside
 * `chain_ticket_severance_and_mobile_fixes.spec.js`'s "Depth ladder +
 * tab-strip divider — surface color consistency" describe block, where
 * a pre-existing test for the same surfaces already lived.
 *
 *  1. Tab-strip row height — `.oes-tab-ltp` / `.oes-common-chase-label`
 *     (SymbolPanel.svelte) and `.cap-pill--panel` (ChaseAggPicker.svelte)
 *     had no explicit height, rendering thinner than the TICKET/CHAIN/
 *     CHART tab buttons in the same `.oes-tabs` row. Fixed with a
 *     `min-height: var(--ctl-h, 1.7rem)` floor (not a rigid height) on
 *     each, matching the row's shared control-height token.
 *  2. "15d" DTE chip vs TP%/SL% param inputs — `.oct-expiry-dte`
 *     (OptionChainTab.svelte) had no explicit height while its row-
 *     sibling `.oes-basket-tpl-param > input` (TemplateBar.svelte) sets
 *     `height: 1.4rem`. Fixed with `min-height: 1.4rem` on the chip —
 *     a local, size-tier-below value, deliberately NOT the shared
 *     `--ctl-h` token. Color difference (chip = muted slate-blue label
 *     = amber) is untouched by design — different UI roles.
 *  4. Side-toggle (BUY/SELL pill) height mechanism — `.ot-side-toggle-
 *     compact` (SideToggle.svelte) set both `height` AND `min-height`
 *     (a rigid box), while its row-sibling `.rbq-select-trigger`
 *     (Select.svelte, Type/Product/Variety/Validity) sets only
 *     `min-height` (a growable floor). Fixed by dropping the rigid
 *     `height` so both settle at the same natural height. Also: dead
 *     code removal (`.ot-side-toggle` + bare `.ot-side-btn`, never
 *     rendered — markup only ever uses `.ot-side-toggle-compact`), and
 *     a stale `1.55rem` var() fallback bumped to `1.7rem` to match
 *     `--ctl-h`'s actual current declared value.
 *
 * ── 2026-09-30 follow-up batch (three more fixes, separate describe
 *    blocks below — own numbering, unrelated to #1/#2/#4 above) ──────
 *
 *  A. Order-placed confirmation chip (`.ot-ok` / `.ot-pending`) mobile
 *     overflow backstop. Live-reproduced at 412px (Pixel-9a-equivalent)
 *     with an intercepted `/api/orders/ticket` response carrying a
 *     genuinely unbreakable 90-char token in place of a real order id —
 *     Chromium's automatic-minimum-size calculation already honours
 *     `word-break: break-word` on a flex item, so no overflow was
 *     reproduced even before this fix. `min-width: 0`, `max-width:
 *     100%`, and `box-sizing: border-box` were added anyway as a
 *     correct-by-construction defensive backstop (task explicitly
 *     asked for it) — not a behaviour change today, a guard against
 *     future browser/content drift.
 *  B. Knobs row (Side / Type / Product / Variety) height-consistency
 *     audit — CONFIRMED CONSISTENT, no change made. Side toggle and
 *     every Select trigger already render at the same computed height
 *     (1.7rem === --ctl-h) both at desktop and mobile widths; the
 *     `Select.svelte` 1.55rem var() fallback never resolves inside the
 *     ticket modal (--ctl-h is always declared on `.ot-modal`).
 *  C. Validity moved from `OrderKnobsRow.svelte` (Type/Product/Variety
 *     only now) into `OrderTicket.svelte`'s own `.ot-lots-price-row`,
 *     as the first item ahead of Lots and Price. Unconditional (not
 *     viewport-scoped) — measured live at 412px: Validity(80px) +
 *     Lots(≈162px, lots-mode with qty suffix) + Price(≈101px) + 2
 *     gaps(≈19px) ≈ 362px fits inside the ~395px usable content width
 *     of the embedded ticket (the only real mount — SymbolPanel always
 *     passes `standalone={false}`) with ~33px headroom. Desktop is
 *     unaffected in practice: at >=1024px `.ot-knobs-price-wrap`
 *     already renders the knobs row and this row side by side on one
 *     visual line, so Validity just moves from "last of row 1" to
 *     "first of row 2" — visually adjacent either way.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
const SYMBOL_PANEL = readFileSync(path.join(dir, 'src/lib/SymbolPanel.svelte'), 'utf8');
const CHASE_AGG_PICKER = readFileSync(path.join(dir, 'src/lib/order/ChaseAggPicker.svelte'), 'utf8');
const CHAIN_TAB = readFileSync(path.join(dir, 'src/lib/order/OptionChainTab.svelte'), 'utf8');
const TEMPLATE_BAR = readFileSync(path.join(dir, 'src/lib/TemplateBar.svelte'), 'utf8');
const SIDE_TOGGLE = readFileSync(path.join(dir, 'src/lib/order/SideToggle.svelte'), 'utf8');
const SELECT = readFileSync(path.join(dir, 'src/lib/Select.svelte'), 'utf8');
const ORDER_TICKET = readFileSync(path.join(dir, 'src/lib/order/OrderTicket.svelte'), 'utf8');
const ORDER_KNOBS_ROW = readFileSync(path.join(dir, 'src/lib/order/OrderKnobsRow.svelte'), 'utf8');

/**
 * Navigate to /orders and type an F&O-eligible symbol (NIFTY). Leaves
 * the Ticket tab active (the page's default) — used by tests that need
 * the tab strip / Side toggle / Select triggers visible.
 * @param {import('@playwright/test').Page} page
 */
async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

/**
 * Navigate to /orders, seed NIFTY, switch to Chain, and ensure the
 * Templ toggle is ON (clicking it if it mounted OFF) so TP%/SL% param
 * inputs are rendered.
 * @param {import('@playwright/test').Page} page
 */
async function _seedNiftyOnChainWithTemplOn(page) {
  await page.addInitScript(() => {
    localStorage.removeItem('ramboq_templ_pref_v1');
  });
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

  const toggle = page.locator('.oes-tpl-button').first();
  await expect(toggle).toBeVisible({ timeout: 15_000 });
  const isActive = await toggle.evaluate((el) => el.classList.contains('active'));
  if (!isActive) await toggle.click();
  await expect(toggle).toHaveClass(/active/, { timeout: 10_000 });
}

test.describe('Fix #1 — tab-strip row height (source)', () => {
  test('.oes-tab-ltp has a min-height floor matching --ctl-h', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-tab-ltp\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-tab-ltp rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*var\(--ctl-h,\s*1\.7rem\)/);
    expect(rule).toMatch(/box-sizing:\s*border-box/);
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    // Floor, not a rigid box — no plain `height:` declaration on this rule.
    expect(rule).not.toMatch(/\n\s*height:\s*var/);
  });

  test('.oes-common-chase-label has a min-height floor matching --ctl-h', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-common-chase-label\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-common-chase-label rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*var\(--ctl-h,\s*1\.7rem\)/);
    expect(rule).toMatch(/box-sizing:\s*border-box/);
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    expect(rule).not.toMatch(/\n\s*height:\s*var/);
  });

  // REVERTED (2026-09-30, same day, explicit operator request) — the
  // --ctl-h floor on .cap-pill--panel made the L/M/H pills look wrong;
  // back to plain content-driven sizing. Asserting the reverted state
  // so a future pass doesn't silently reintroduce it without a
  // deliberate decision.
  test('.cap-pill--panel (ChaseAggPicker, variant="panel") stays content-driven — no --ctl-h floor', () => {
    const rule = CHASE_AGG_PICKER.match(/\.cap-pill--panel\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.cap-pill--panel rule').not.toBe('');
    expect(rule).not.toMatch(/min-height:\s*var\(--ctl-h/);
    expect(rule).not.toMatch(/\n\s*height:\s*var\(--ctl-h/);
  });
});

test.describe('Fix #1 — tab-strip row height (live computed style)', () => {
  test('live: .oes-common-chase-label computes min-height >= --ctl-h; .cap-pill--panel stays content-driven (reverted)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const chaseLabel = page.locator('.oes-common-chase-label').first();
    await expect(chaseLabel).toBeVisible({ timeout: 15_000 });
    const capPill = page.locator('.cap-pill--panel').first();
    await expect(capPill).toBeVisible({ timeout: 10_000 });
    const algoTab = page.locator('.oes-tabs .algo-tab').first();
    await expect(algoTab).toBeVisible({ timeout: 10_000 });

    const [chaseH, pillH, tabH, rootFontPx] = await Promise.all([
      chaseLabel.evaluate((el) => el.getBoundingClientRect().height),
      capPill.evaluate((el) => el.getBoundingClientRect().height),
      algoTab.evaluate((el) => el.getBoundingClientRect().height),
      page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize)),
    ]);
    const expectedFloorPx = 1.7 * rootFontPx;

    // CHASE label meets the shared control-height floor (1.7rem).
    // Reported alongside the tab-button height for context — 1.7rem is
    // taller than desktop --toolbar-h (24px), so the fixed row elements
    // are expected to be taller than (not equal to) .algo-tab here; that
    // is the correct, spec'd outcome, not a bug.
    expect(chaseH, `CHASE label height (tab button is ${tabH}px, floor is ${expectedFloorPx}px)`).toBeGreaterThanOrEqual(expectedFloorPx - 1);
    // L/M/H pill height is NOT asserted against the floor — the fix was
    // reverted same day; pillH is only sampled above for the (now
    // content-driven, expected-shorter) reference.
    expect(pillH).toBeGreaterThan(0);
  });
});

test.describe('Fix #2 — 15d DTE chip vs TP%/SL% input height', () => {
  test('.oct-expiry-dte has min-height: 1.4rem (local value, not --ctl-h)', () => {
    const rule = CHAIN_TAB.match(/\.oct-expiry-dte\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-expiry-dte rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*1\.4rem/);
    expect(rule).toMatch(/box-sizing:\s*border-box/);
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    // Deliberately NOT the shared --ctl-h token — a size tier below.
    // (Prose in the rule's own comment mentions --ctl-h by name to
    // explain the choice — check for the functional usage only.)
    expect(rule).not.toMatch(/min-height:\s*var\(--ctl-h/);
  });

  test('color roles remain distinct — chip stays muted slate-blue, TP%/SL% labels stay amber', () => {
    const dteRule = CHAIN_TAB.match(/\.oct-expiry-dte\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(dteRule).toMatch(/color:\s*var\(--algo-muted\)/);
    const paramLabelRule = TEMPLATE_BAR.match(/\.oes-basket-tpl-param\s*>\s*span,\s*\n?\s*\.oes-basket-tpl-param-label\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(paramLabelRule, '.oes-basket-tpl-param-label rule').not.toBe('');
    expect(paramLabelRule).toMatch(/rgba\(251,\s*191,\s*36,\s*0\.85\)/);
  });

  test('live: .oct-expiry-dte box height matches .oes-basket-tpl-param > input box height (within 1px)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnChainWithTemplOn(page);

    const dteChip = page.locator('.oct-expiry-dte').first();
    await expect(dteChip).toBeVisible({ timeout: 15_000 });
    const tpInput = page.locator('.oes-basket-tpl-param > input').first();
    await expect(tpInput).toBeVisible({ timeout: 10_000 });

    const [dteH, inputH] = await Promise.all([
      dteChip.evaluate((el) => el.getBoundingClientRect().height),
      tpInput.evaluate((el) => el.getBoundingClientRect().height),
    ]);
    expect(Math.abs(dteH - inputH), `15d chip height ${dteH}px vs TP% input height ${inputH}px`).toBeLessThanOrEqual(1);

    // Fix #3 piggybacks on this same page/navigation (already on the
    // Chain tab with NIFTY seeded) rather than adding a separate live
    // NIFTY-symbol-search test elsewhere — each Playwright test gets a
    // fresh browser context with no persisted instruments cache, so
    // every extra live symbol-search test in the SAME spec file compounds
    // `loadInstruments()` reload cost; empirically a 4th consecutive one
    // in the sibling chain_ticket_severance_and_mobile_fixes.spec.js file
    // started intermittently timing out. Proves .chain-grid-wrap and the
    // CE/PE/Strike header cells resolve --card-bg-gradient to a real
    // linear-gradient in the browser (not `none`, which would happen if
    // the token were scoped to a theme class that doesn't reach
    // SymbolPanel's portaled modal), then switches to the Ticket tab
    // (same page, no extra navigation) to check .ot-depth too.
    for (const sel of ['.chain-grid-wrap', '.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const bgImage = await page.locator(sel).first().evaluate((el) => getComputedStyle(el).backgroundImage);
      expect(bgImage, `${sel} background-image`).toMatch(/^linear-gradient/);
    }
    const ticketTab = page.getByRole('tab', { name: /Ticket/i }).first();
    await ticketTab.click();
    const depth = page.locator('.ot-depth').first();
    await expect(depth).toBeVisible({ timeout: 15_000 });
    const depthBgImage = await depth.evaluate((el) => getComputedStyle(el).backgroundImage);
    expect(depthBgImage, '.ot-depth background-image').toMatch(/^linear-gradient/);
  });
});

test.describe('Fix #3 (2026-09-30 follow-up, superseded same day) — Expiry toolbar row height audit', () => {
  // SUPERSEDED — the initial "two deliberate tiers" finding below (Select
  // trigger + Template toggle at 1.55rem vs DTE chip + TP% input at
  // 1.4rem) was re-reviewed the same day and found to be a real, un-
  // intentional inconsistency, not a deliberate design tier. Fixed via
  // `.oct-toolbar { --ctl-h: 1.4rem; }` + a padding override on Select
  // triggers scoped to that toolbar (OptionChainTab.svelte) — all four
  // controls now render at the SAME ~1.4rem height. This describe block
  // now asserts the corrected single-tier state.
  //
  // .oct-mode-btn / .oct-mode-toggle / .oct-controls / .oct-field-mode
  // (mentioned in the original brief as a possible row occupant) turned
  // out to be DEAD CSS — no matching markup exists anywhere in
  // OptionChainTab.svelte, so they never render in this row (or any
  // row) and are excluded from the audit. Flagged here for visibility;
  // removal is out of scope for this visual-only pass.
  test('dead-code check: .oct-mode-btn / .oct-controls / .oct-field-mode / .oct-mode-toggle have CSS rules but no matching markup', () => {
    expect(CHAIN_TAB).toMatch(/\.oct-mode-btn\s*\{/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-controls\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-field-mode\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-mode-toggle\b/);
    expect(CHAIN_TAB).not.toMatch(/class="[^"]*\boct-mode-btn\b/);
  });

  test('live: .oct-toolbar controls (Select trigger, Template toggle, DTE chip, TP% input) all render at the same height', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnChainWithTemplOn(page);

    const selectTrigger = page.locator('.oct-expiry-pick .rbq-select-trigger').first();
    const tplButton = page.locator('.oes-tpl-button').first();
    const dteChip = page.locator('.oct-expiry-dte').first();
    const tpInput = page.locator('.oes-basket-tpl-param > input').first();
    await expect(selectTrigger).toBeVisible({ timeout: 15_000 });
    await expect(tplButton).toBeVisible({ timeout: 10_000 });
    await expect(dteChip).toBeVisible({ timeout: 10_000 });
    await expect(tpInput).toBeVisible({ timeout: 10_000 });

    const [selectH, tplH, dteH, inputH] = await Promise.all([
      selectTrigger.evaluate((el) => el.getBoundingClientRect().height),
      tplButton.evaluate((el) => el.getBoundingClientRect().height),
      dteChip.evaluate((el) => el.getBoundingClientRect().height),
      tpInput.evaluate((el) => el.getBoundingClientRect().height),
    ]);

    expect(Math.abs(selectH - tplH), `Select trigger ${selectH}px vs Template toggle ${tplH}px`).toBeLessThanOrEqual(1);
    expect(Math.abs(dteH - inputH), `DTE chip ${dteH}px vs TP% input ${inputH}px`).toBeLessThanOrEqual(1);
    expect(Math.abs(tplH - dteH), `Template toggle ${tplH}px vs DTE chip ${dteH}px (should now match — single tier)`).toBeLessThanOrEqual(1);
  });
});

test.describe('Fix #4 — Side toggle dead-code removal + height-mechanism parity', () => {
  test('.ot-side-toggle and the bare .ot-side-btn rule are gone; live-only classes remain', () => {
    expect(SIDE_TOGGLE).not.toMatch(/\.ot-side-toggle\s*\{/);
    // The bare selector (not scoped under .ot-side-toggle-compact) must
    // be gone — scoped variants like `.ot-side-toggle-compact .ot-side-btn`
    // must remain.
    expect(SIDE_TOGGLE).not.toMatch(/\n {2}\.ot-side-btn\s*\{/);
    expect(SIDE_TOGGLE).toMatch(/\.ot-side-toggle-compact\s*\{/);
    expect(SIDE_TOGGLE).toMatch(/\.ot-side-toggle-compact \.ot-side-btn\s*\{/);
    // Orphan (unscoped) .on color rules are gone too — the scoped ones
    // under .ot-side-toggle-compact remain the only source of BUY/SELL
    // active-state color.
    expect(SIDE_TOGGLE).not.toMatch(/\n {2}\.ot-side-buy\.on\s*\{/);
    expect(SIDE_TOGGLE).not.toMatch(/\n {2}\.ot-side-sell\.on\s*\{/);
    expect(SIDE_TOGGLE).toMatch(/\.ot-side-toggle-compact \.ot-side-btn\.ot-side-buy\.on\s*\{/);
    expect(SIDE_TOGGLE).toMatch(/\.ot-side-toggle-compact \.ot-side-btn\.ot-side-sell\.on\s*\{/);
  });

  test('centering (display/align-items/justify-content) survives the dead-code removal, now on the scoped rule', () => {
    const rule = SIDE_TOGGLE.match(/\.ot-side-toggle-compact \.ot-side-btn\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.ot-side-toggle-compact .ot-side-btn rule').not.toBe('');
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    expect(rule).toMatch(/justify-content:\s*center/);
  });

  test('.ot-side-toggle-compact drops the rigid height, keeps only min-height (matches Select\'s growable-floor strategy), fallback bumped to 1.7rem', () => {
    const rule = SIDE_TOGGLE.match(/\.ot-side-toggle-compact\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.ot-side-toggle-compact rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*var\(--ctl-h,\s*1\.7rem\)/);
    expect(rule).not.toMatch(/\n\s*height:\s*var\(--ctl-h/);
  });

  test('Select.svelte .rbq-select-trigger is untouched — still min-height-only, own 1.55rem fallback intentionally left as-is', () => {
    // Select.svelte's fallback is NOT "stale" — its own comment (right
    // above the rule) explains it's the deliberate default for every
    // OTHER Select usage in the app outside the order-ticket modal
    // (where --ctl-h is always declared, so this fallback never
    // actually resolves there). Changing it would retroactively grow
    // every unrelated Select trigger app-wide — out of scope for this
    // batch of order-modal-only fixes.
    const rule = SELECT.match(/\.rbq-select-trigger\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.rbq-select-trigger rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*var\(--ctl-h,\s*1\.55rem\)/);
    expect(rule).not.toMatch(/\n\s*height:\s*var\(--ctl-h/);
  });

  test('live: .ot-side-toggle-compact and a Select trigger in the same modal report the same rendered height', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const sideToggle = page.locator('.ot-side-toggle-compact').first();
    await expect(sideToggle).toBeVisible({ timeout: 15_000 });
    // #ot-type-sel specifically — SymbolPanel's OWN header Select
    // triggers (Account / Exchange filter, `.oes-picker`) render
    // outside OrderTicket's `.ot-modal` and are DELIBERATELY excluded
    // from the shared --ctl-h scope (per .ot-modal's own comment) so
    // resizing the ticket's controls doesn't also resize those — using
    // a bare `.rbq-select-trigger.first()` would pick one of those
    // instead of the Type/Product/Variety/Validity knob this fix
    // actually targets.
    const selectTrigger = page.locator('#ot-type-sel').first();
    await expect(selectTrigger).toBeVisible({ timeout: 10_000 });

    const [sideH, selectH] = await Promise.all([
      sideToggle.evaluate((el) => el.getBoundingClientRect().height),
      selectTrigger.evaluate((el) => el.getBoundingClientRect().height),
    ]);
    expect(Math.abs(sideH - selectH), `side toggle height ${sideH}px vs Select trigger height ${selectH}px`).toBeLessThanOrEqual(1);
  });
});

test.describe('Fix A — order-placed confirmation chip mobile overflow backstop', () => {
  test('source: .ot-ok and .ot-pending both carry min-width:0, max-width:100%, box-sizing:border-box alongside word-break', () => {
    for (const cls of ['.ot-ok', '.ot-pending']) {
      const rule = ORDER_TICKET.match(new RegExp(`\\${cls}\\s*\\{[\\s\\S]*?\\n  \\}`))?.[0] ?? '';
      expect(rule, `${cls} rule`).not.toBe('');
      expect(rule).toMatch(/word-break:\s*break-word/);
      expect(rule).toMatch(/min-width:\s*0/);
      expect(rule).toMatch(/max-width:\s*100%/);
      expect(rule).toMatch(/box-sizing:\s*border-box/);
    }
  });

  test('live: a genuinely unbreakable long placement message never overflows .ot-footer-info or .ot-modal at 412px', async ({ page }) => {
    test.setTimeout(60_000);
    // Never touches the real backend — both routes are intercepted.
    let ticketRouteHit = false;
    await page.route('**/api/orders/ticket', async (route) => {
      ticketRouteHit = true;
      // A 90-char unbreakable digit run stands in for an order id — far
      // longer than any real broker order id, deliberately adversarial
      // for word-break: break-word.
      const longId = '9998887776665554443332221190009998887776665554443332221190009998887776665554443332221190';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ order_id: longId, mode: 'paper', status: 'COMPLETE', detail: 'ok' }),
      });
    });
    await page.route('**/api/admin/execution/mode', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'] }),
        });
      } else {
        await route.continue();
      }
    });

    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(1500); // let the mocked execution-mode poll land
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    // MARKET type avoids the "limit price required" validation gate.
    const typeTrigger = page.locator('[aria-label="Order type"]').first();
    await typeTrigger.click();
    const marketOpt = page.locator('.rbq-select-option-label').filter({ hasText: /^MARKET$/ }).first();
    await marketOpt.click();
    await page.waitForTimeout(300);

    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit, .ot-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 8_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();
    await page.waitForTimeout(1500);
    expect(ticketRouteHit, 'the mocked /api/orders/ticket route must have fired').toBe(true);

    const okChip = page.locator('.ot-ok').first();
    await expect(okChip).toBeVisible({ timeout: 10_000 });

    const report = await okChip.evaluate((el) => {
      const chipRect = el.getBoundingClientRect();
      const footer = el.closest('.ot-footer-info');
      const modal = el.closest('.ot-modal');
      const footerRect = footer ? footer.getBoundingClientRect() : null;
      const modalRect = modal ? modal.getBoundingClientRect() : null;
      return {
        vw: window.innerWidth,
        chipRight: chipRect.right,
        footerRight: footerRect?.right,
        footerScrollWidth: footer?.scrollWidth,
        footerClientWidth: footer?.clientWidth,
        modalRight: modalRect?.right,
        docScrollWidth: document.documentElement.scrollWidth,
      };
    });

    // The chip must sit inside its own .ot-footer-info flex container —
    // scrollWidth <= clientWidth means no child forced the container to
    // overflow (this survives .ot-modal's own overflow-x:hidden clip,
    // which would otherwise mask a genuine overflow as a false pass).
    expect(report.footerScrollWidth, JSON.stringify(report)).toBeLessThanOrEqual(report.footerClientWidth + 1);
    expect(report.chipRight, JSON.stringify(report)).toBeLessThanOrEqual(report.modalRight + 1);
    expect(report.docScrollWidth, JSON.stringify(report)).toBeLessThanOrEqual(report.vw + 2);
  });
});

test.describe('Fix B — knobs row (Side / Type / Product / Variety) height audit, 2026-09-30 follow-up', () => {
  // Live-measured (chromium-desktop, 412px viewport, root font-size 16px):
  //   .ot-side-toggle-compact (SideToggle)   27.19px = 1.7rem (--ctl-h)
  //   #ot-type-sel / #ot-product-sel /
  //   #ot-variety-sel (Select triggers)      27.19px = 1.7rem (--ctl-h)
  // Also re-measured at 1400px desktop — same 27.19px across all four.
  // Finding: CONSISTENT, no change made. The Select.svelte 1.55rem
  // var() fallback (see Fix #4 above) never resolves inside the ticket
  // modal because --ctl-h: 1.7rem is always declared on .ot-modal
  // itself (both the standalone shell and SymbolPanel's embedded
  // .oes-ticket-body override leave --ctl-h untouched).
  test('live: SideToggle and every Type/Product/Variety Select trigger render at the same height, at both mobile and desktop widths', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    for (const viewport of [{ width: 412, height: 919 }, { width: 1400, height: 900 }]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(200);

      const sideToggle = page.locator('.ot-side-toggle-compact').first();
      await expect(sideToggle).toBeVisible({ timeout: 10_000 });
      const selects = page.locator('.ot-row-knobs .rbq-select-trigger');
      const selectCount = await selects.count();
      expect(selectCount, 'Type/Product/Variety Selects visible in .ot-row-knobs').toBeGreaterThanOrEqual(3);

      const sideH = await sideToggle.evaluate((el) => el.getBoundingClientRect().height);
      for (let i = 0; i < selectCount; i++) {
        const h = await selects.nth(i).evaluate((el) => el.getBoundingClientRect().height);
        expect(Math.abs(sideH - h), `viewport ${viewport.width}px: side toggle ${sideH}px vs select[${i}] ${h}px`).toBeLessThanOrEqual(1);
      }
    }
  });
});

test.describe('Fix C — Validity moved into .ot-lots-price-row (Validity + Lots + Price merge)', () => {
  test('source: OrderKnobsRow.svelte no longer renders a Validity Select or accepts a validity prop', () => {
    expect(ORDER_KNOBS_ROW).not.toMatch(/ot-validity-sel/);
    expect(ORDER_KNOBS_ROW).not.toMatch(/validity\s*=\s*\$bindable/);
    expect(ORDER_KNOBS_ROW).not.toMatch(/bind:validity/);
    // Type/Product/Variety remain.
    expect(ORDER_KNOBS_ROW).toMatch(/ot-type-sel/);
    expect(ORDER_KNOBS_ROW).toMatch(/ot-product-sel/);
    expect(ORDER_KNOBS_ROW).toMatch(/ot-variety-sel/);
  });

  test('source: OrderTicket.svelte renders Validity inside .ot-lots-price-row, ahead of the Lots cell, and no longer passes bind:validity to OrderKnobsRow', () => {
    expect(ORDER_TICKET).not.toMatch(/bind:validity=\{_validity\}/);
    const rowBlock = ORDER_TICKET.match(/<div class="ot-row ot-lots-price-row">[\s\S]*?<\/div>\s*<\/div><!-- \/\.ot-knobs-price-wrap -->/)?.[0] ?? '';
    expect(rowBlock, '.ot-lots-price-row markup block').not.toBe('');
    const validityIdx = rowBlock.indexOf('ot-validity-sel');
    const lotsIdx = rowBlock.indexOf('ot-lots-cell');
    const priceIdx = rowBlock.indexOf('ot-price-cell');
    expect(validityIdx, 'Validity select present in .ot-lots-price-row').toBeGreaterThan(-1);
    expect(lotsIdx, 'Lots cell present').toBeGreaterThan(-1);
    expect(validityIdx, 'Validity must render before Lots (reading-order continuation from the knobs row above)').toBeLessThan(lotsIdx);
    if (priceIdx > -1) {
      expect(lotsIdx, 'Lots must render before Price').toBeLessThan(priceIdx);
    }
  });

  test('live: Validity + Lots + Price render on one line at 412px (Pixel-9a-equivalent), no horizontal overflow', async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 412, height: 919 });
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('NIFTY');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(800);

    const validitySelect = page.locator('#ot-validity-sel').first();
    const lotsRow = page.locator('.ot-lots-row').first();
    const priceCell = page.locator('.ot-price-cell').first();
    await expect(validitySelect).toBeVisible({ timeout: 10_000 });
    await expect(lotsRow).toBeVisible({ timeout: 10_000 });
    await expect(priceCell).toBeVisible({ timeout: 10_000 });

    const [validityBox, lotsBox, priceBox, modalBox] = await Promise.all([
      validitySelect.boundingBox(),
      lotsRow.boundingBox(),
      priceCell.boundingBox(),
      page.locator('.ot-modal').first().boundingBox(),
    ]);

    // Same y-band — within a few px, allowing for the label-above-control
    // stack's own small alignment slop (Validity's Select sits at the
    // BOTTOM of its label+control stack, same as the Lots stepper cluster;
    // Price's cell is measured at its top since it wraps label+input).
    expect(Math.abs(validityBox.y - lotsBox.y), `Validity y=${validityBox.y} vs Lots y=${lotsBox.y}`).toBeLessThanOrEqual(3);

    // No control's right edge exceeds the modal's own right edge.
    for (const [name, box] of [['Validity', validityBox], ['Lots', lotsBox], ['Price', priceBox]]) {
      expect(box.x + box.width, `${name} right edge vs modal right edge ${modalBox.x + modalBox.width}`)
        .toBeLessThanOrEqual(modalBox.x + modalBox.width + 1);
    }

    const report = await page.evaluate(() => ({
      docScrollWidth: document.documentElement.scrollWidth,
      vw: window.innerWidth,
    }));
    expect(report.docScrollWidth).toBeLessThanOrEqual(report.vw + 2);
  });
});
