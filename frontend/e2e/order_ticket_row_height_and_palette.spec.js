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

  test('.cap-pill--panel (ChaseAggPicker, variant="panel") has a min-height floor matching --ctl-h', () => {
    const rule = CHASE_AGG_PICKER.match(/\.cap-pill--panel\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.cap-pill--panel rule').not.toBe('');
    expect(rule).toMatch(/min-height:\s*var\(--ctl-h,\s*1\.7rem\)/);
    expect(rule).toMatch(/box-sizing:\s*border-box/);
    expect(rule).toMatch(/display:\s*inline-flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    expect(rule).not.toMatch(/\n\s*height:\s*var/);
  });
});

test.describe('Fix #1 — tab-strip row height (live computed style)', () => {
  test('live: .oes-common-chase-label and .cap-pill--panel both compute min-height >= --ctl-h', async ({ page }) => {
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

    // Both fixed elements meet the shared control-height floor (1.7rem).
    // Reported alongside the tab-button height for context — 1.7rem is
    // taller than desktop --toolbar-h (24px), so the fixed row elements
    // are expected to be taller than (not equal to) .algo-tab here; that
    // is the correct, spec'd outcome, not a bug.
    expect(chaseH, `CHASE label height (tab button is ${tabH}px, floor is ${expectedFloorPx}px)`).toBeGreaterThanOrEqual(expectedFloorPx - 1);
    expect(pillH, `L/M/H pill height (tab button is ${tabH}px, floor is ${expectedFloorPx}px)`).toBeGreaterThanOrEqual(expectedFloorPx - 1);
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
