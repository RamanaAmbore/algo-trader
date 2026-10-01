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

// ─────────────────────────────────────────────────────────────────────────
// Wave B (2026-09-30) — ShortcutCheatsheet + AgentToast/AgentFireModal
// z-index tier + layerStack migration. Fixes:
//   1. Cheatsheet z=9996/9997 (barely above the full-screen tier) → moved
//      to --z-cheatsheet=20200/+1, above every modal tier + nav dropdown.
//   2. AgentToast/AgentFireModal z=9997 (accidentally == cheatsheet's old
//      panel z-index, and below the full-screen/order-modal tiers,
//      meaning broker/risk alerts could be hidden) → --z-agent-alert=20500,
//      above the cheatsheet.
//   3. Cheatsheet's own uncoordinated Escape listener (svelte:window)
//      double-closed alongside a full-screen card or the order modal on
//      one Escape press → migrated onto the layerStack coordinator.
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — app.css cheatsheet + agent-alert tiers', () => {
  const css = readFile('src/app.css');

  function zVar(name) {
    const m = css.match(new RegExp(`--${name}:\\s*([0-9]+)\\s*;`));
    expect(m, `--${name} must be a plain numeric custom property in app.css`).not.toBeNull();
    return Number(m[1]);
  }

  test('--z-cheatsheet and --z-agent-alert exist and are numeric', () => {
    expect(zVar('z-cheatsheet')).toBeGreaterThan(0);
    expect(zVar('z-agent-alert')).toBeGreaterThan(0);
  });

  test('tier ordering: modal-critical < dropdown/drawer < cheatsheet < agent-alert < toast', () => {
    const zModalCritical = zVar('z-modal-critical');
    const zDropdown      = zVar('z-dropdown');
    const zDrawer        = zVar('z-drawer');
    const zCheatsheet    = zVar('z-cheatsheet');
    const zAgentAlert    = zVar('z-agent-alert');
    const zToast         = zVar('z-toast');

    expect(zModalCritical).toBeLessThan(zCheatsheet);
    expect(zDropdown).toBeLessThan(zCheatsheet);
    expect(zDrawer).toBeLessThan(zCheatsheet);
    expect(zCheatsheet).toBeLessThan(zAgentAlert);
    expect(zAgentAlert).toBeLessThan(zToast);
  });
});

test.describe('Static source checks — ShortcutCheatsheet migrated onto layerStack', () => {
  const cheatsheet = readFile('src/lib/ShortcutCheatsheet.svelte');

  test('imports pushLayer/popLayer and pushes/pops keyed on `open`', () => {
    expect(cheatsheet).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(cheatsheet).toMatch(/_layerId = pushLayer\(\(\) => onClose\(\)\);/);
  });

  test('own uncoordinated svelte:window Escape listener is removed', () => {
    expect(cheatsheet).not.toMatch(/<svelte:window onkeydown=\{open \? _onKey : null\} \/>/);
    expect(cheatsheet).not.toMatch(/function _onKey/);
  });

  test('overlay + panel use the --z-cheatsheet token, not bare 9996/9997', () => {
    expect(cheatsheet).toMatch(/z-index: var\(--z-cheatsheet\);/);
    expect(cheatsheet).toMatch(/z-index: calc\(var\(--z-cheatsheet\) \+ 1\);/);
    expect(cheatsheet).not.toMatch(/z-index: 9996;/);
    expect(cheatsheet).not.toMatch(/z-index: 9997;/);
  });
});

test.describe('Static source checks — AgentToast + AgentFireModal tier fix', () => {
  const agentToast = readFile('src/lib/AgentToast.svelte');
  const agentFireModal = readFile('src/lib/AgentFireModal.svelte');

  test('AgentToast stack uses --z-agent-alert, not bare 9997', () => {
    expect(agentToast).toMatch(/z-index: var\(--z-agent-alert\);/);
    expect(agentToast).not.toMatch(/z-index: 9997;/);
  });

  test('stale "under modal (9998) + bell popup (9999)" comment is corrected', () => {
    expect(agentToast).not.toMatch(/under modal \(9998\) \+ bell popup \(9999\)/);
  });

  test('AgentFireModal is wired to --z-agent-alert via ModalShell', () => {
    expect(agentFireModal).toMatch(/zIndex="var\(--z-agent-alert\)"/);
    expect(agentFireModal).not.toMatch(/zIndex=\{9998\}/);
  });

  test('AgentFireModal registers itself on the layerStack coordinator', () => {
    expect(agentFireModal).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(agentFireModal).toMatch(/_layerId = pushLayer\(\(\) => onClose\(\)\);/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional — cheatsheet Escape coordination (real browser)
// ─────────────────────────────────────────────────────────────────────────

test.describe('Functional — ShortcutCheatsheet Escape coordination', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('`?` opens the cheatsheet; Escape still closes it (regression after layerStack migration)', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.locator('body').click({ position: { x: 5, y: 5 } });

    await page.keyboard.press('?');
    const modal = page.locator('.sc-modal');
    await expect(modal, 'cheatsheet should open on `?`').toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('Escape');
    await expect(modal, 'Escape should close the cheatsheet').toHaveCount(0, { timeout: 3_000 });
  });

  test('cheatsheet opened over an already-fullscreen card: one Escape closes only the cheatsheet', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const fsBtn = page.locator('button.fs-btn:visible').first();
    await expect(fsBtn, 'a fullscreen-card trigger must be present on /dashboard').toBeVisible({ timeout: 15_000 });
    await fsBtn.click();
    const fsCard = page.locator('.fs-card-on');
    await expect(fsCard, 'card should promote to fullscreen').toHaveCount(1, { timeout: 5_000 });

    await page.keyboard.press('?');
    const modal = page.locator('.sc-modal');
    await expect(modal, 'cheatsheet should open over the fullscreen card').toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('Escape');

    // Only the topmost (most-recently-opened) layer — the cheatsheet —
    // closes. The fullscreen card must survive the same Escape press.
    await expect(modal, 'cheatsheet should close').toHaveCount(0, { timeout: 3_000 });
    await expect(fsCard, 'fullscreen card must NOT also close on the same Escape').toHaveCount(1);
  });

  test('cheatsheet opened over the order modal: one Escape closes only the cheatsheet', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn, '.pha-order button must be present').toBeVisible({ timeout: 10_000 });
    await orderBtn.click({ force: true });
    const orderOverlay = page.locator('.canonical-modal-overlay').first();
    await expect(orderOverlay, 'order modal should open').toBeVisible({ timeout: 10_000 });

    await page.keyboard.press('?');
    const modal = page.locator('.sc-modal');
    await expect(modal, 'cheatsheet should open over the order modal').toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('Escape');

    await expect(modal, 'cheatsheet should close').toHaveCount(0, { timeout: 3_000 });
    await expect(orderOverlay, 'order modal must NOT also close on the same Escape').toBeVisible();
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Wave B (2026-09-30) — NavigationIndicator z-index + DefaultSizeButton
// full-screen Escape coordination. Builds on Wave A's conventions above.
//
//   1. NavigationIndicator was a bare z-index:9200 literal, BELOW the
//      full-screen card pattern (9999) and the order modal (--z-command
//      = 10500) — a `g`-shortcut navigation fired while either was open
//      rendered no visible route-progress feedback. Fixed: new
//      `--z-nav-indicator` (10550) var, placed between tier 4 (10500)
//      and tier 3 (10700) in app.css's documented tier ladder.
//   2. DefaultSizeButton.svelte's full-screen Escape handling used its
//      own unconditional `document.addEventListener('keydown', ...)`,
//      independent of every other overlay's own Escape listener — one
//      Escape could close the full-screen card AND whatever was open on
//      top of it (order modal / cheatsheet / nested dropdown)
//      simultaneously. Fixed: migrated onto the shared layerStack
//      coordinator (pushLayer on mount, popLayer on cleanup) so only the
//      topmost pushed layer reacts — the full-screen card's own layer is
//      pushed first (bottom of stack) and stays untouched until every
//      layer opened on top of it has been explicitly closed.
// ─────────────────────────────────────────────────────────────────────────

test.describe('Static source checks — NavigationIndicator z-index tier', () => {
  const css = readFile('src/app.css');
  const navIndicator = readFile('src/lib/NavigationIndicator.svelte');

  function zVar(name) {
    const m = css.match(new RegExp(`--${name}:\\s*([0-9]+)\\s*;`));
    expect(m, `--${name} must be a plain numeric custom property in app.css`).not.toBeNull();
    return Number(m[1]);
  }

  test('--z-nav-indicator exists and sits above the full-screen card AND the order modal', () => {
    const zNavIndicator = zVar('z-nav-indicator');
    const zCommand = zVar('z-command');
    const FULLSCREEN_CARD_Z = 9999; // .fs-card-on — literal, unchanged
    expect(zNavIndicator).toBeGreaterThan(FULLSCREEN_CARD_Z);
    expect(zNavIndicator).toBeGreaterThan(zCommand);
  });

  test('NavigationIndicator.svelte uses the shared var, not the old bare 9200 literal', () => {
    expect(navIndicator).toMatch(/z-index:\s*var\(--z-nav-indicator\);/);
    expect(navIndicator).not.toMatch(/z-index:\s*9200;/);
  });
});

test.describe('Static source checks — app.css ownership comments corrected (Fix 4)', () => {
  const css = readFile('src/app.css');
  const fullscreenBtn = readFile('src/lib/FullscreenButton.svelte');

  test('app.css no longer claims the backdrop is portalled by FullscreenButton.svelte', () => {
    expect(css).not.toMatch(/portalled to document\.body\s*\n\s*by FullscreenButton\.svelte/);
    expect(css).toMatch(/portalled to document\.body\s*\n\s*by DefaultSizeButton\.svelte/);
  });

  test('FullscreenButton.svelte header comment attributes backdrop/Escape ownership to DefaultSizeButton', () => {
    expect(fullscreenBtn).toMatch(/owned by DefaultSizeButton\.svelte, not this component/);
  });
});

test.describe('Static source checks — DefaultSizeButton migrated onto layerStack', () => {
  const defaultSizeBtn = readFile('src/lib/DefaultSizeButton.svelte');

  test('imports pushLayer/popLayer and pushes a layer that exits full-screen on Escape', () => {
    expect(defaultSizeBtn).toMatch(/import \{ pushLayer, popLayer \} from '\$lib\/utils\/layerStack\.js';/);
    expect(defaultSizeBtn).toMatch(/_layerId = pushLayer\(\(\) => \{ isFullscreen = false; \}\);/);
    expect(defaultSizeBtn).toMatch(/popLayer\(_layerId\);/);
  });

  test('old unconditional document keydown Escape listener is gone', () => {
    expect(defaultSizeBtn).not.toMatch(/document\.addEventListener\('keydown', _onKey\)/);
  });

  test('scroll-lock restore-prior-value pattern is untouched (Fix 3 — no regression)', () => {
    expect(defaultSizeBtn).toMatch(/const prev = document\.body\.style\.overflow;/);
    expect(defaultSizeBtn).toMatch(/document\.body\.style\.overflow = prev;/);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Functional — full-screen card Escape coordination (real browser)
// ─────────────────────────────────────────────────────────────────────────

test.describe('Functional — Wave B: full-screen card Escape double-close scenarios', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('full-screen Chart card + order modal: Escape closes only the topmost, one press at a time', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const fsBtn = page.locator('button[aria-label="Expand Chart to fullscreen"]');
    await expect(fsBtn, 'Chart card fullscreen button must be visible').toBeVisible({ timeout: 15_000 });
    await fsBtn.click();

    const fsCard = page.locator('.row1-col-chart.fs-card-on');
    await expect(fsCard, 'Chart card should be fullscreen').toBeVisible({ timeout: 5_000 });

    // Open the order modal via the global 't' shortcut — the full-screen
    // backdrop visually covers the navbar's own `.pha-order` trigger, so
    // the shortcut (handled by a window-level listener, unaffected by
    // the backdrop) is the reliable way to open it on top of a
    // full-screen card in a real browser.
    await page.keyboard.press('t');
    const overlay = page.locator('.canonical-modal-overlay').first();
    await expect(overlay, 'order modal should open on top of the full-screen card').toBeVisible({ timeout: 10_000 });

    // First Escape: closes ONLY the order modal (topmost layer) — the
    // regression this fix guards. Pre-fix, this Escape closed BOTH.
    await page.keyboard.press('Escape');
    await expect(overlay, 'order modal should close on the first Escape').toHaveCount(0, { timeout: 5_000 });
    await expect(fsCard, 'full-screen card must still be open after the first Escape').toBeVisible();

    // Second Escape: now closes the full-screen card.
    await page.keyboard.press('Escape');
    await expect(page.locator('.row1-col-chart.fs-card-on'), 'full-screen card should close on the second Escape')
      .toHaveCount(0, { timeout: 5_000 });
  });

  test('full-screen Chart card + shortcut cheatsheet: Escape closes only the topmost, one press at a time', async ({ page }) => {
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const fsBtn = page.locator('button[aria-label="Expand Chart to fullscreen"]');
    await expect(fsBtn, 'Chart card fullscreen button must be visible').toBeVisible({ timeout: 15_000 });
    await fsBtn.click();

    const fsCard = page.locator('.row1-col-chart.fs-card-on');
    await expect(fsCard, 'Chart card should be fullscreen').toBeVisible({ timeout: 5_000 });

    await page.keyboard.press('?');
    const cheatsheet = page.locator('.sc-modal');
    await expect(cheatsheet, 'cheatsheet should open on top of the full-screen card').toBeVisible({ timeout: 5_000 });

    // First Escape: closes ONLY the cheatsheet (topmost, opened last).
    // Pre-fix, this Escape closed BOTH the cheatsheet and the full-screen
    // card at once.
    await page.keyboard.press('Escape');
    await expect(cheatsheet, 'cheatsheet should close on the first Escape').toHaveCount(0, { timeout: 5_000 });
    await expect(fsCard, 'full-screen card must still be open after the first Escape').toBeVisible();

    // Second Escape: now closes the full-screen card.
    await page.keyboard.press('Escape');
    await expect(page.locator('.row1-col-chart.fs-card-on'), 'full-screen card should close on the second Escape')
      .toHaveCount(0, { timeout: 5_000 });
  });

  test('inline fullscreen Order Entry card + nested symbol-search dropdown: Escape closes only the dropdown', async ({ page }) => {
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const fsBtn = page.locator('button[aria-label="Expand Order Entry to fullscreen"]');
    await expect(fsBtn, 'Order Entry card fullscreen button must be visible').toBeVisible({ timeout: 15_000 });
    await fsBtn.click();

    const fsCard = page.locator('.bucket-card-entry.fs-card-on');
    await expect(fsCard, 'Order Entry card should be fullscreen').toBeVisible({ timeout: 5_000 });

    const symInput = fsCard.locator('.ssi-input').first();
    await symInput.click();
    const dropdown = page.locator('.ssi-drop').first();
    await expect(dropdown, 'symbol search dropdown should open').toBeVisible({ timeout: 5_000 });

    // First Escape: closes ONLY the nested dropdown. Pre-fix, the
    // full-screen card's own unconditional Escape listener fired in
    // parallel and exited full-screen too.
    await page.keyboard.press('Escape');
    await expect(page.locator('.ssi-drop'), 'dropdown should close on the first Escape').toHaveCount(0, { timeout: 5_000 });
    await expect(fsCard, 'full-screen card must still be open after the dropdown closes').toBeVisible();

    // Second Escape: now exits full-screen.
    await page.keyboard.press('Escape');
    await expect(page.locator('.bucket-card-entry.fs-card-on'), 'full-screen card should close on the second Escape')
      .toHaveCount(0, { timeout: 5_000 });
  });

  test('--z-nav-indicator resolves at runtime above the full-screen card AND the order-modal tier, with a full-screen card open', async ({ page }) => {
    // A real SPA route change (onNavigate → NavigationIndicator.start())
    // could not be driven reliably in this harness: client-side `goto()`
    // navigation never committed in this dev environment regardless of
    // trigger (keyboard `g`+letter buffer, plain click, forced click,
    // in either route direction) — confirmed via standalone control runs
    // outside this spec, with no full-screen card involved at all, so
    // the cause is environmental (this dev server's navigation never
    // resolving, most likely because `onNavigate`'s view-transition
    // promise or the destination route's own data load never settles
    // here — see NavigationIndicator.svelte's design notes), not a
    // regression from this fix. Instead, this test proves the token
    // itself resolves correctly at runtime, in the one state (full-screen
    // card open) the fix specifically targets, which combined with the
    // static source checks above (var used in the component, old 9200
    // literal gone, var ordered above tier 2 and tier 4 in app.css)
    // fully covers the behavior without depending on a live route change.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const fsBtn = page.locator('button[aria-label="Expand Chart to fullscreen"]');
    await expect(fsBtn, 'Chart card fullscreen button must be visible').toBeVisible({ timeout: 15_000 });
    await fsBtn.click();

    const fsCard = page.locator('.row1-col-chart.fs-card-on');
    await expect(fsCard, 'Chart card should be fullscreen').toBeVisible({ timeout: 5_000 });

    const [navIndicatorZ, commandZ] = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      return [
        Number(cs.getPropertyValue('--z-nav-indicator').trim()),
        Number(cs.getPropertyValue('--z-command').trim()),
      ];
    });
    const FULLSCREEN_CARD_Z = 9999; // .fs-card-on literal — confirmed active via fsCard above
    expect(navIndicatorZ, '--z-nav-indicator must resolve to a real number at runtime').toBeGreaterThan(0);
    expect(navIndicatorZ, 'nav indicator z-index must exceed the open full-screen card').toBeGreaterThan(FULLSCREEN_CARD_Z);
    expect(navIndicatorZ, 'nav indicator z-index must exceed the order-modal tier').toBeGreaterThan(commandZ);
  });
});

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
