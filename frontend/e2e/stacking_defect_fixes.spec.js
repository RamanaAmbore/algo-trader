/**
 * stacking_defect_fixes.spec.js
 *
 * Wave A of the modal/menu/toast stacking-defect audit (2026-09-30) —
 * covers the shared Escape-stack coordinator, the reworked z-index
 * scale, and the five concrete bugs fixed in this pass:
 *
 *   1. Duplicate mode-dropdown render (desktop + mobile both portalled
 *      an identical `.mode-combo-overlay` / `.mode-combo-dropdown`).
 *   2. Mode dropdown had no Escape handling at all.
 *   3. LIVE-mode-switch confirm rendered at z=400, behind the order
 *      modal (z=10500) — now at the new `--z-modal-critical` tier.
 *   4. SymbolPanel's scroll-lock cleanup wiped a PRIOR body-overflow
 *      lock (e.g. from an already-open full-screen card) instead of
 *      restoring it.
 *   5. SymbolPanel's click-outside-to-close was dead code
 *      (`.canonical-modal-overlay` is `pointer-events: none`).
 *
 * The Escape-stack coordinator itself (`frontend/src/lib/utils/layerStack.js`)
 * has a dedicated, thorough Vitest unit suite —
 * `frontend/src/lib/__tests__/layerStack.test.js` (push/pop LIFO
 * ordering, listener de-dup, no-op on empty stack) — pure logic with no
 * DOM dependency, matching this repo's convention for `src/lib/__tests__`
 * (e.g. tickFlash.test.js, withGuard.test.js: non-`data/` utility logic
 * tested via Vitest, not Playwright). This file additionally exercises
 * the coordinator end-to-end in a real browser via the mode-dropdown
 * Escape test below (bug #2), so the coordinator is covered at both the
 * unit level and in situ.
 *
 * Five quality dimensions:
 *   1. SSOT   — z-index tiers read from app.css / computed style, not
 *               re-hardcoded; mode-dropdown dedup asserted via DOM count
 *   2. Perf   — static source checks run with zero network/page loads
 *   3. Stale  — grep guards confirm the OLD duplicate block / dead
 *               click-outside code is actually gone, not just untested
 *   4. Reuse  — shared loginAsAdmin fixture; reuses the existing
 *               `.pha-order` / `.canonical-modal-overlay` entry points
 *               established by mobile_hamburger_over_order_modal.spec.js
 *   5. UX     — functional checks (Escape closes, click-outside closes,
 *               scroll-lock survives a nested close) run in a real page,
 *               not just source greps
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — app.css z-index scale
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — app.css z-index scale (tier ordering)', () => {
  const css = readFile('src/app.css');

  /** Pull a `--z-foo: 1234;` numeric value out of the :root block. */
  function zVar(name) {
    const m = css.match(new RegExp(`--${name}:\\s*([0-9]+)\\s*;`));
    expect(m, `--${name} must be a plain numeric custom property in app.css`).not.toBeNull();
    return Number(m[1]);
  }

  test('new tier vars exist and are numeric', () => {
    expect(zVar('z-modal-nested')).toBeGreaterThan(0);
    expect(zVar('z-modal-in-command')).toBeGreaterThan(0);
    expect(zVar('z-modal-critical')).toBeGreaterThan(0);
  });

  test('tier ordering: nav < fullscreen-card(9999) < modal-nested < command < modal-in-command < modal-critical', () => {
    const zNav            = zVar('z-nav');
    const zModalNested     = zVar('z-modal-nested');
    const zCommand          = zVar('z-command');
    const zModalInCommand   = zVar('z-modal-in-command');
    const zModalCritical    = zVar('z-modal-critical');
    const FULLSCREEN_CARD_Z = 9999; // .fs-card-on — literal, not a var (unchanged this pass)

    expect(zNav).toBeLessThan(FULLSCREEN_CARD_Z);
    expect(FULLSCREEN_CARD_Z).toBeLessThan(zModalNested);
    expect(zCommand).toBeLessThan(zModalInCommand);
    expect(zModalNested).toBeLessThan(zModalInCommand);
    expect(zModalInCommand).toBeLessThan(zModalCritical);
  });

  test('--z-toast is the absolute top of every tier defined in this scale', () => {
    const zToast = zVar('z-toast');
    for (const name of ['z-nav', 'z-command', 'z-modal-nested', 'z-modal-in-command', 'z-modal-critical', 'z-dropdown', 'z-drawer']) {
      expect(zToast, `--z-toast must exceed --${name}`).toBeGreaterThan(zVar(name));
    }
  });

  test('stale "z-index 10000" claim for the PageHeaderActions modal comment is gone', () => {
    expect(css).not.toMatch(/modal overlay itself is\s*\n\s*position: fixed at z-index 10000/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — LIVE-mode confirm z-index + ConfirmModal prop
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — LIVE-mode confirm critical tier', () => {
  test('the LIVE-confirm ConfirmModal instance is wired to --z-modal-critical', () => {
    const layout = readFile('src/routes/(algo)/+layout.svelte');
    expect(layout).toMatch(/<ConfirmModal bind:this=\{_liveConfirmRef\} zIndex="var\(--z-modal-critical\)" \/>/);
  });

  test('ConfirmModal accepts a zIndex prop (no longer hardcoded to 400 for every instance)', () => {
    const confirmModal = readFile('src/lib/ConfirmModal.svelte');
    expect(confirmModal).toMatch(/let \{ zIndex = 400 \} = \$props\(\);/);
    expect(confirmModal).toMatch(/<ModalShell open=\{_open\} onClose=\{_cancel\} ariaLabel="Confirm action" \{zIndex\}>/);
  });

  test('other ConfirmModal call sites are untouched (still bare, default to 400)', () => {
    const strategies = readFile('src/routes/(algo)/strategies/+page.svelte');
    expect(strategies).toMatch(/<ConfirmModal bind:this=\{confirmRef\} \/>/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — demo-mode submit modal tier (OrderTicket.svelte)
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — OrderTicket demo-mode submit modal tier', () => {
  const orderTicket = readFile('src/lib/order/OrderTicket.svelte');

  test('demo submit modal uses --z-modal-in-command, not a bare 300', () => {
    expect(orderTicket).toMatch(/zIndex="var\(--z-modal-in-command\)"/);
    // Standalone attribute line only — the surrounding explanatory
    // comment legitimately mentions the OLD literal `zIndex={300}` in
    // prose, which must not trip this guard.
    expect(orderTicket).not.toMatch(/^\s*zIndex=\{300\}\s*$/m);
  });

  test('stale --z-drawer=200 claim is corrected (real value is 20001)', () => {
    expect(orderTicket).not.toMatch(/above the OrderTimelineDrawer \(var\(--z-drawer\)=200\)/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — mode-dropdown duplicate removed from source
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — mode-dropdown duplicate removed', () => {
  const layout = readFile('src/routes/(algo)/+layout.svelte');

  test('exactly one `.mode-combo-overlay` markup block exists in source', () => {
    const matches = layout.match(/<div class="mode-combo-overlay"/g) || [];
    expect(matches.length).toBe(1);
  });

  test('exactly one `.mode-combo-dropdown` markup block exists in source', () => {
    const matches = layout.match(/<ul class="mode-combo-dropdown"/g) || [];
    expect(matches.length).toBe(1);
  });

  test('mode dropdown is wired through the layerStack coordinator', () => {
    expect(layout).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(layout).toMatch(/_modeLayerId = pushLayer\(\(\) => \{ modeOpen = false; \}\);/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Static source checks — SymbolPanel fixes
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — SymbolPanel scroll-lock + click-outside + Escape', () => {
  const symbolPanel = readFile('src/lib/SymbolPanel.svelte');

  test('scroll-lock cleanup restores the PRIOR overflow value, not an unconditional empty string', () => {
    expect(symbolPanel).toMatch(/const _prevOverflow = document\.body\.style\.overflow;/);
    expect(symbolPanel).toMatch(/document\.body\.style\.overflow = _prevOverflow;/);
    // Old bug: unconditional reset wiped any lock a full-screen card held.
    expect(symbolPanel).not.toMatch(/document\.body\.style\.overflow = '';\s*\n\s*\}\s*\n\s*\};/);
  });

  test('overlay pointer-events overridden to auto so click-outside can fire', () => {
    // Can't use a `[^}]*` regex here — the explanatory comment inside
    // this rule legitimately quotes `onclick={onClose}` style snippets
    // containing literal `}` characters. Slice to the rule's own
    // closing brace (2-space indented, matching this file's style)
    // instead of relying on a single brace-balanced regex.
    const start = symbolPanel.indexOf('.oes-overlay {');
    expect(start, '.oes-overlay rule must exist').toBeGreaterThan(-1);
    const close = symbolPanel.indexOf('\n  }', start);
    expect(close, '.oes-overlay rule must close').toBeGreaterThan(start);
    const ruleBody = symbolPanel.slice(start, close);
    expect(ruleBody).toMatch(/pointer-events:\s*auto;/);
  });

  test('SymbolPanel is migrated onto the layerStack coordinator', () => {
    expect(symbolPanel).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(symbolPanel).toMatch(/const _layerId = inline \? null : pushLayer\(onClose\);/);
  });

  test('own unconditional Escape-closes-modal branch is removed from onKey', () => {
    // The old bug: any Escape (even one meant for a nested dropdown)
    // called onClose() directly from this component's own listener.
    expect(symbolPanel).not.toMatch(/if \(e\.key === 'Escape'\) \{\s*\n\s*\/\/ Fullscreen exits first/);
  });
});

test.describe('Static source checks — Select.svelte / SymbolSearchInput.svelte migrated', () => {
  test('Select.svelte dropdown pushes/pops its own layer', () => {
    const select = readFile('src/lib/Select.svelte');
    expect(select).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(select).toMatch(/_layerId = pushLayer\(\(\) => \{ open = false; \}\);/);
  });

  test('SymbolSearchInput.svelte dropdown pushes/pops its own layer', () => {
    const ssi = readFile('src/lib/SymbolSearchInput.svelte');
    expect(ssi).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(ssi).toMatch(/_layerId = pushLayer\(\(\) => \{ _symOpen = false; _symSuggestions = \[\]; \}\);/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional — mode dropdown: single render + Escape coordinator in situ
// ─────────────────────────────────────────────────────────────────────────

test.describe('Functional — mode dropdown duplicate + Escape (real browser)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  for (const vp of [
    { label: 'desktop', width: 1280, height: 800 },
    { label: 'mobile',  width: 390,  height: 844 },
  ]) {
    test(`only ONE mode-combo-overlay renders when open [${vp.label}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

      // Both the desktop (hidden lg:flex, DOM-first) and mobile
      // (lg:hidden, DOM-second) nav rows render TWO `button.mode-trigger`
      // elements at all times — only one is visible per breakpoint, via
      // CSS. `:visible` picks whichever one the current viewport shows.
      const trigger = page.locator('button.mode-trigger:visible').first();
      await expect(trigger, 'mode trigger chip must be visible').toBeVisible({ timeout: 15_000 });
      await trigger.click();

      const dropdown = page.locator('ul.mode-combo-dropdown');
      await expect(dropdown.first(), 'dropdown should open').toBeVisible({ timeout: 5_000 });

      // The actual regression guard: exactly one of each, never two
      // duplicate portals rendered at identical coordinates.
      await expect(page.locator('.mode-combo-overlay')).toHaveCount(1);
      await expect(page.locator('ul.mode-combo-dropdown')).toHaveCount(1);
    });
  }

  test('Escape closes the mode dropdown (previously had no handling at all)', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const trigger = page.locator('button.mode-trigger:visible').first();
    await expect(trigger).toBeVisible({ timeout: 15_000 });
    await trigger.click();

    const dropdown = page.locator('ul.mode-combo-dropdown').first();
    await expect(dropdown).toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('Escape');
    await expect(page.locator('ul.mode-combo-dropdown')).toHaveCount(0, { timeout: 3_000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional — SymbolPanel scroll-lock restore + click-outside
// ─────────────────────────────────────────────────────────────────────────

test.describe('Functional — SymbolPanel scroll-lock restore + click-outside-to-close', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('closing the order modal restores a PRIOR body-overflow lock instead of wiping it', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Simulate an already-open full-screen card having locked scroll
    // BEFORE the order modal opens (the exact nested scenario the bug
    // report described) — set it directly rather than depending on a
    // real full-screen card flow, which keeps this test independent of
    // any one page's specific fullscreen-card markup.
    await page.evaluate(() => { document.body.style.overflow = 'hidden'; });

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn, '.pha-order button must be present').toBeVisible({ timeout: 10_000 });
    await orderBtn.click({ force: true });

    const overlay = page.locator('.canonical-modal-overlay').first();
    await expect(overlay, 'order modal should open').toBeVisible({ timeout: 10_000 });
    await expect.poll(async () => page.evaluate(() => document.body.style.overflow))
      .toBe('hidden');

    // Close the order modal (click-outside — also exercises bug #5).
    await overlay.click({ position: { x: 5, y: 5 } });
    await expect(overlay, 'order modal should close').toHaveCount(0, { timeout: 5_000 });

    // The prior lock (simulating the still-open full-screen card) must
    // survive — this is the regression this test guards.
    const overflowAfterClose = await page.evaluate(() => document.body.style.overflow);
    expect(overflowAfterClose, 'prior scroll-lock must be restored, not wiped to \'\'').toBe('hidden');
  });

  test('clicking the overlay backdrop (outside the panel) closes the order modal', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn).toBeVisible({ timeout: 10_000 });
    await orderBtn.click({ force: true });

    const overlay = page.locator('.canonical-modal-overlay').first();
    await expect(overlay).toBeVisible({ timeout: 10_000 });

    // Click near the very top-left corner of the overlay — outside the
    // panel content (the panel is top-anchored with side margins at
    // desktop widths; this point is reliably backdrop, not panel).
    await overlay.click({ position: { x: 5, y: 5 } });
    await expect(overlay, 'clicking the backdrop must close the modal').toHaveCount(0, { timeout: 5_000 });
  });

  test('clicking INSIDE the panel does not close the modal (stopPropagation still works)', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn).toBeVisible({ timeout: 10_000 });
    await orderBtn.click({ force: true });

    const panel = page.locator('.canonical-modal-panel').first();
    await expect(panel).toBeVisible({ timeout: 10_000 });
    await panel.click({ position: { x: 20, y: 20 } });

    // Still open — a click that lands on the panel must not bubble to
    // the overlay's onclick={onClose}.
    await expect(page.locator('.canonical-modal-overlay')).toBeVisible();
  });
});
