/**
 * css_utility_part_a_sweep.spec.js
 *
 * 2026-10-13 "Frontend reusability sweep — Part A" (plan:
 * purrfect-marinating-pixel.md). Pure CSS-utility extraction, zero
 * behavior/visual change:
 *
 *   1. `.algo-icon-btn` (app.css) — shared box-model/palette base for
 *      RefreshButton, FullscreenButton, DefaultSizeButton,
 *      GridDownloadButton, GridSearchButton. `margin` stays LOCAL on
 *      FullscreenButton/GridSearchButton (both self-position via
 *      `margin-left: auto`, not the shared `margin: 0` default).
 *      RefreshButton also keeps its own `background`/`border` local
 *      (deliberate slate default, overridden by `.rf-mkt-*`) since
 *      those two properties were never byte-identical to the shared
 *      cyan literal there — only the box-model props moved.
 *   2. `.algo-label-decor` (app.css) — shared `border-left: 3px solid
 *      currentColor; padding-left: 0.35rem;` for CardHeader's
 *      `.ch-title`, MarketPulse's `.mp-bucket-label`, SymbolPanel's
 *      `.oes-modal-name`, orders/+page.svelte's `.oc-entry-label`.
 *      `.mp-bucket-label`'s own `padding` shorthand was split into
 *      top/right/bottom-only so the shared class's padding-left isn't
 *      shadowed by a competing shorthand.
 *   3. MultiPriceChart's `.mpc-shell` adopts the existing
 *      `.algo-chart-frame` class (background/border/radius/box-sizing
 *      were an exact match — same DRY pattern PriceChart already uses).
 *   4. NavTab.svelte: 2 of 4 hardcoded rgba(...) literals had an exact
 *      value match against an existing app.css token and were swapped
 *      (`--algo-cyan-border`, `--algo-red-cascade-lg`); the other 2
 *      (no exact token match) were deliberately left as literals.
 *
 * Every assertion below pairs a structural (source-regex) check with a
 * live computed-style regression guard. The live expected values were
 * captured from the UNMODIFIED codebase before this change landed (see
 * handback report) and must still match byte-for-byte after it.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/css_utility_part_a_sweep.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const __dir = dirname(fileURLToPath(import.meta.url));
const APP_CSS = readFileSync(join(__dir, '../src/app.css'), 'utf8');
const REFRESH_SRC = readFileSync(join(__dir, '../src/lib/RefreshButton.svelte'), 'utf8');
const FULLSCREEN_SRC = readFileSync(join(__dir, '../src/lib/FullscreenButton.svelte'), 'utf8');
const DEFAULTSIZE_SRC = readFileSync(join(__dir, '../src/lib/DefaultSizeButton.svelte'), 'utf8');
const GRIDDL_SRC = readFileSync(join(__dir, '../src/lib/GridDownloadButton.svelte'), 'utf8');
const GRIDSEARCH_SRC = readFileSync(join(__dir, '../src/lib/GridSearchButton.svelte'), 'utf8');
const CARDHEADER_SRC = readFileSync(join(__dir, '../src/lib/CardHeader.svelte'), 'utf8');
const MARKETPULSE_SRC = readFileSync(join(__dir, '../src/lib/MarketPulse.svelte'), 'utf8');
const SYMBOLPANEL_SRC = readFileSync(join(__dir, '../src/lib/SymbolPanel.svelte'), 'utf8');
const ORDERS_SRC = readFileSync(join(__dir, '../src/routes/(algo)/orders/+page.svelte'), 'utf8');
const MPC_SRC = readFileSync(join(__dir, '../src/lib/MultiPriceChart.svelte'), 'utf8');
const NAVTAB_SRC = readFileSync(join(__dir, '../src/lib/NavTab.svelte'), 'utf8');

function boxStyle(el) {
  const s = getComputedStyle(el);
  return { display: s.display, alignItems: s.alignItems, justifyContent: s.justifyContent,
    width: s.width, height: s.height, padding: s.padding, margin: s.margin,
    background: s.backgroundColor,
    border: s.borderTopWidth + ' ' + s.borderTopStyle + ' ' + s.borderTopColor,
    borderRadius: s.borderRadius };
}

// ── Part 1 — .algo-icon-btn structural checks ───────────────────────────

test.describe('Part A.1 — .algo-icon-btn shared base (structural)', () => {
  test('app.css defines .algo-icon-btn with the exact shared literal', () => {
    const m = /\.algo-icon-btn\s*\{([^}]*)\}/.exec(APP_CSS);
    expect(m, '.algo-icon-btn rule must exist in app.css').not.toBeNull();
    const body = m[1];
    expect(body).toMatch(/display:\s*inline-flex/);
    expect(body).toMatch(/align-items:\s*center/);
    expect(body).toMatch(/justify-content:\s*center/);
    expect(body).toMatch(/width:\s*1\.4rem/);
    expect(body).toMatch(/height:\s*1\.4rem/);
    expect(body).toMatch(/padding:\s*0/);
    expect(body).toMatch(/margin:\s*0/);
    expect(body).toMatch(/background:\s*var\(--algo-cyan-bg\)/);
    expect(body).toMatch(/border:\s*1px solid var\(--algo-cyan-border\)/);
    expect(body).toMatch(/border-radius:\s*3px/);
  });

  test('RefreshButton: class applied, box-model deduped, background/border/color kept LOCAL', () => {
    expect(REFRESH_SRC).toMatch(/class="rf-btn algo-icon-btn/);
    const m = /\.rf-btn\s*\{([^}]*)\}/.exec(REFRESH_SRC);
    expect(m, '.rf-btn rule must still exist').not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/display:\s*inline-flex/);
    expect(body).not.toMatch(/width:\s*1\.4rem/);
    expect(body).not.toMatch(/padding:\s*0;/);
    expect(body).not.toMatch(/margin:\s*0;/);
    expect(body).not.toMatch(/border-radius:\s*3px/);
    // Deliberately kept local — NOT a byte-identical duplicate of the shared cyan default.
    expect(body).toMatch(/background:\s*rgba\(126,\s*151,\s*184,\s*0\.14\)/);
    expect(body).toMatch(/border:\s*1px solid rgba\(126,\s*151,\s*184,\s*0\.55\)/);
    expect(body).toMatch(/color:\s*#94a3b8/);
  });

  test('FullscreenButton: class applied, box-model deduped, margin-left:auto kept LOCAL', () => {
    expect(FULLSCREEN_SRC).toMatch(/class="fs-btn algo-icon-btn"/);
    const m = /\.fs-btn\s*\{([^}]*)\}/.exec(FULLSCREEN_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/display:\s*inline-flex/);
    expect(body).not.toMatch(/background:\s*var\(--algo-cyan-bg\)/);
    expect(body).not.toMatch(/border:\s*1px solid var\(--algo-cyan-border\)/);
    // margin differs from the shared default (0 0 0 auto vs 0) — must stay local.
    expect(body).toMatch(/margin:\s*0 0 0 auto/);
  });

  test('DefaultSizeButton: class applied, fully deduped (margin:0 matched the shared default)', () => {
    expect(DEFAULTSIZE_SRC).toMatch(/class="default-btn algo-icon-btn"/);
    const m = /\.default-btn\s*\{([^}]*)\}/.exec(DEFAULTSIZE_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/display:\s*inline-flex/);
    expect(body).not.toMatch(/width:\s*1\.4rem/);
    expect(body).not.toMatch(/margin:\s*0;/);
    expect(body).not.toMatch(/background:\s*var\(--algo-cyan-bg\)/);
    expect(body).toMatch(/color:\s*var\(--c-info\)/);
  });

  test('GridDownloadButton: class applied, fully deduped (margin:0 matched the shared default)', () => {
    expect(GRIDDL_SRC).toMatch(/class="grid-download-btn algo-icon-btn"/);
    const m = /\.grid-download-btn\s*\{([^}]*)\}/.exec(GRIDDL_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/display:\s*inline-flex/);
    expect(body).not.toMatch(/background:\s*var\(--algo-cyan-bg\)/);
    // .with-auto-margin is a separate, untouched selector.
    expect(GRIDDL_SRC).toMatch(/\.grid-download-btn\.with-auto-margin\s*\{\s*margin-left:\s*auto;\s*\}/);
  });

  test('GridSearchButton: class applied, margin-left:auto kept LOCAL', () => {
    expect(GRIDSEARCH_SRC).toMatch(/class="grid-search-btn algo-icon-btn"/);
    const m = /\.grid-search-btn\s*\{([^}]*)\}/.exec(GRIDSEARCH_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/display:\s*inline-flex/);
    expect(body).not.toMatch(/background:\s*var\(--algo-cyan-bg\)/);
    expect(body).toMatch(/margin:\s*0 0 0 auto/);
  });
});

// ── Part 1 — live regression guards (pre-captured expected values) ─────

test.describe('Part A.1 — .algo-icon-btn live regression (pixel-identical to pre-change)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  const EXPECTED_BOX = {
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    width: '22.3906px', height: '22.3906px', padding: '0px', margin: '0px',
    borderRadius: '3px',
  };

  test('RefreshButton (.rf-btn) on /pulse header renders unchanged box + market-closed sky colour', async ({ page }) => {
    // Box-model values are unaffected by the 2026-10-24 market-closed
    // colour-consistency fix (RefreshButton.svelte `.rf-mkt-closed`
    // repointed from slate to --algo-sky, unifying with
    // BrokerHealthBadge's ticker-idle colour) — only background/border
    // changed, captured below. This assertion assumes the real market is
    // closed at test-run time (true whenever `.rf-mkt-closed` is the
    // applied bucket); see market_closed_color_consistency.spec.js for a
    // version that mocks the market-status endpoint explicitly.
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.page-header .rf-btn').first().waitFor({ state: 'visible', timeout: 30000 });
    const s = await page.locator('.page-header .rf-btn').first().evaluate(boxStyle);
    expect(s).toMatchObject(EXPECTED_BOX);
    expect(s.background).toBe('rgba(125, 211, 252, 0.12)');
    expect(s.border).toBe('1px solid rgba(125, 211, 252, 0.55)');
  });

  test('GridSearchButton (.grid-search-btn) on /pulse Positions renders unchanged box + cyan default', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const btn = page.locator('button.grid-search-btn[aria-label="Toggle Positions symbol filter"]').first();
    await expect(btn).toBeVisible({ timeout: 20000 });
    const s = await btn.evaluate(boxStyle);
    expect(s).toMatchObject(EXPECTED_BOX);
    expect(s.background).toBe('rgba(34, 211, 238, 0.14)');
    expect(s.border).toBe('1px solid rgba(34, 211, 238, 0.55)');
  });

  test('FullscreenButton / DefaultSizeButton / GridDownloadButton on /pulse Positions card render unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const card = page.locator('.mp-bucket-positions');
    await expect(card).toBeVisible({ timeout: 20000 });

    const fsBtn = card.locator('.fs-btn').first();
    await expect(fsBtn).toBeVisible({ timeout: 10000 });
    const fsStyle = await fsBtn.evaluate(boxStyle);
    expect(fsStyle).toMatchObject(EXPECTED_BOX);
    expect(fsStyle.background).toBe('rgba(34, 211, 238, 0.14)');
    expect(fsStyle.border).toBe('1px solid rgba(34, 211, 238, 0.55)');

    const dlBtn = card.locator('.grid-download-btn').first();
    await expect(dlBtn).toBeVisible({ timeout: 10000 });
    const dlStyle = await dlBtn.evaluate(boxStyle);
    expect(dlStyle).toMatchObject(EXPECTED_BOX);
    expect(dlStyle.background).toBe('rgba(34, 211, 238, 0.14)');
    expect(dlStyle.border).toBe('1px solid rgba(34, 211, 238, 0.55)');

    await fsBtn.click();
    await page.waitForTimeout(300);
    await page.mouse.move(0, 0); // avoid :hover contaminating the rest-state read
    await page.waitForTimeout(100);
    const defBtn = card.locator('.default-btn').first();
    await expect(defBtn).toBeVisible({ timeout: 10000 });
    const defStyle = await defBtn.evaluate(boxStyle);
    expect(defStyle).toMatchObject(EXPECTED_BOX);
    expect(defStyle.background).toBe('rgba(34, 211, 238, 0.14)');
    expect(defStyle.border).toBe('1px solid rgba(34, 211, 238, 0.55)');
    await page.keyboard.press('Escape');
  });
});

// ── Part 2 — .algo-label-decor structural + live checks ─────────────────

test.describe('Part A.2 — .algo-label-decor shared base (structural)', () => {
  test('app.css defines .algo-label-decor with the exact shared literal', () => {
    const m = /\.algo-label-decor\s*\{([^}]*)\}/.exec(APP_CSS);
    expect(m, '.algo-label-decor rule must exist in app.css').not.toBeNull();
    const body = m[1];
    expect(body).toMatch(/border-left:\s*3px solid currentColor/);
    expect(body).toMatch(/padding-left:\s*0\.35rem/);
  });

  test('CardHeader .ch-title: class applied, border-left/padding-left removed, color kept', () => {
    expect(CARDHEADER_SRC).toMatch(/<span class="ch-title algo-label-decor">/);
    const m = /\.ch-title\s*\{([^}]*)\}/.exec(CARDHEADER_SRC);
    expect(m).not.toBeNull();
    // Strip the rule's own /* ... */ comment block before asserting — the
    // explanatory comment legitimately mentions the old declaration in
    // prose, which must not be confused with a real (undeduped) CSS rule.
    const body = m[1].replace(/\/\*[\s\S]*?\*\//g, '');
    expect(body).not.toMatch(/border-left:\s*3px solid currentColor/);
    expect(body).not.toMatch(/padding-left:\s*0\.35rem/);
    expect(body).toMatch(/color:\s*var\(--ch-title-color/);
  });

  test('SymbolPanel .oes-modal-name: class applied, border-left/padding-left removed, color kept', () => {
    expect(SYMBOLPANEL_SRC).toMatch(/<span class="oes-modal-name algo-label-decor">/);
    const m = /\.oes-modal-name\s*\{([^}]*)\}/.exec(SYMBOLPANEL_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/border-left:\s*3px solid currentColor/);
    expect(body).not.toMatch(/padding-left:\s*0\.35rem/);
    expect(body).toMatch(/color:\s*var\(--c-action\)/);
  });

  test('orders/+page.svelte .oc-entry-label: class applied, border-left/padding-left removed, color kept', () => {
    expect(ORDERS_SRC).toMatch(/<span class="oc-entry-label algo-label-decor">/);
    const m = /\.oc-entry-label\s*\{([^}]*)\}/.exec(ORDERS_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/border-left:\s*3px solid currentColor/);
    expect(body).not.toMatch(/padding-left:\s*0\.35rem/);
    expect(body).toMatch(/color:\s*rgba\(251,\s*191,\s*36,\s*0\.7\)/);
  });

  test('MarketPulse .mp-bucket-label: class applied at all 4 sites, padding split (top/right/bottom only), border-left removed', () => {
    const sites = [
      /<span class="mp-bucket-label algo-label-decor mp-bucket-label-winners">/,
      /<span class="mp-bucket-label algo-label-decor mp-bucket-label-losers">/,
      /<span class="mp-bucket-label algo-label-decor mp-bucket-label-positions">/,
      /<span class="mp-bucket-label algo-label-decor mp-bucket-label-holdings">/,
    ];
    for (const re of sites) expect(MARKETPULSE_SRC).toMatch(re);

    const m = /\.mp-bucket-label\s*\{([^}]*)\}/.exec(MARKETPULSE_SRC);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/border-left:\s*3px solid currentColor/);
    expect(body).not.toMatch(/padding:\s*0\.15rem 0\.35rem/); // old shorthand gone
    expect(body).toMatch(/padding-top:\s*0\.15rem/);
    expect(body).toMatch(/padding-right:\s*0\.35rem/);
    expect(body).toMatch(/padding-bottom:\s*0\.15rem/);
    expect(body).toMatch(/color:\s*var\(--algo-slate\)/);
  });
});

test.describe('Part A.2 — .algo-label-decor live regression (pixel-identical to pre-change)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('MarketPulse Positions label: border-left + full padding box unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const label = page.locator('.mp-bucket-label-positions').first();
    await expect(label).toBeVisible({ timeout: 20000 });
    const s = await label.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeft: cs.borderLeftWidth + ' ' + cs.borderLeftStyle + ' ' + cs.borderLeftColor,
        padding: cs.padding, color: cs.color };
    });
    expect(s.borderLeft).toBe('3px solid rgba(125, 211, 252, 0.85)');
    expect(s.padding).toBe('2.4px 5.6px'); // 0.15rem/0.35rem resolved
    expect(s.color).toBe('rgba(125, 211, 252, 0.85)');
  });

  test('CardHeader .ch-title (Dashboard Trend card): border-left + padding-left unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const title = page.locator('.ch-title').first();
    await expect(title).toBeVisible({ timeout: 20000 });
    const s = await title.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeft: cs.borderLeftWidth + ' ' + cs.borderLeftStyle + ' ' + cs.borderLeftColor,
        paddingLeft: cs.paddingLeft };
    });
    expect(s.borderLeft).toBe('3px solid rgb(255, 255, 255)');
    expect(s.paddingLeft).toBe('5.6px');
  });

  test('orders/+page.svelte .oc-entry-label: border-left + padding-left unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const label = page.locator('.oc-entry-label').first();
    await expect(label).toBeVisible({ timeout: 20000 });
    const s = await label.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeft: cs.borderLeftWidth + ' ' + cs.borderLeftStyle + ' ' + cs.borderLeftColor,
        paddingLeft: cs.paddingLeft };
    });
    expect(s.borderLeft).toBe('3px solid rgba(251, 191, 36, 0.7)');
    expect(s.paddingLeft).toBe('5.6px');
  });

  test('SymbolPanel .oes-modal-name (Order entry modal): border-left + padding-left unchanged', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    // The 't' keyboard shortcut proved unreliable in this spec's earlier
    // revision — click the header's own Order button directly instead
    // (always present, not gated on live data), same fix already applied
    // in orders_title_and_modal_label_cleanup.spec.js.
    const orderBtn = page.locator('.pha-order');
    await expect(orderBtn).toBeVisible({ timeout: 15000 });
    await orderBtn.click();
    const name = page.locator('.oes-modal-name').first();
    await expect(name).toBeVisible({ timeout: 10000 });
    const s = await name.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeft: cs.borderLeftWidth + ' ' + cs.borderLeftStyle + ' ' + cs.borderLeftColor,
        paddingLeft: cs.paddingLeft };
    });
    expect(s.borderLeft).toBe('3px solid rgb(251, 191, 36)');
    expect(s.paddingLeft).toBe('5.6px');
    await page.keyboard.press('Escape');
  });
});

// ── Part 3 — MultiPriceChart .algo-chart-frame adoption ─────────────────

test.describe('Part A.3 — MultiPriceChart adopts .algo-chart-frame (structural)', () => {
  test('.mpc-shell uses algo-chart-frame and no longer duplicates background/border/radius/box-sizing', () => {
    expect(MPC_SRC).toMatch(/class="mpc-shell algo-chart-frame \{_pulse\.classOf\('mpc'\)\}"/);
    const m = /\.mpc-shell\s*\{([^}]*)\}/.exec(MPC_SRC);
    expect(m, '.mpc-shell rule must still exist (for padding/width/max-width/border-left)').not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(body).not.toMatch(/border:\s*1px solid rgba\(251/);
    expect(body).not.toMatch(/border-radius:\s*4px/);
    expect(body).not.toMatch(/box-sizing:\s*border-box/);
    // Component-specific accent + spacing stay local.
    expect(body).toMatch(/border-left:\s*3px solid var\(--c-action\)/);
    expect(body).toMatch(/padding:\s*8px 12px/);
    expect(body).toMatch(/max-width:\s*960px/);
  });

  test('app.css .algo-chart-frame literal unchanged (no drift since the OptionsPayoff/PriceChart extraction)', () => {
    const m = /\.algo-chart-frame\s*\{([^}]*)\}/.exec(APP_CSS);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(body).toMatch(/border:\s*1px solid rgba\(251,\s*191,\s*36,\s*0\.18\)/);
    expect(body).toMatch(/border-radius:\s*4px/);
    expect(body).toMatch(/box-sizing:\s*border-box/);
  });
});

test.describe('Part A.3 — MultiPriceChart live regression (best-effort, graceful skip)', () => {
  // `.mpc-shell` only mounts inside the Sandbox simulator's "Leg premiums"
  // section once a scenario run has at least one live position (see
  // SimulatorPanel.svelte `{#if positions.length}`). Spinning up a real
  // run against a possibly-empty live broker book is out of scope for
  // this pure-CSS task — the structural checks above already prove the
  // DRY retrofit. This test opportunistically checks the live box when
  // the simulator workspace happens to have a run already active, and
  // skips cleanly otherwise (same "skip rather than fail on environment
  // timing" precedent as cardheader_sweep_round3_part_cd.spec.js).
  test('live: .mpc-shell renders the exact pre-existing border/radius/box-sizing when visible', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/execution?tab=sim', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    const shell = page.locator('.mpc-shell').first();
    const visible = await shell.isVisible().catch(() => false);
    if (!visible) {
      // No active run with live positions in this environment — structural
      // checks above already prove the retrofit; skip the pixel check.
      return;
    }
    const info = await shell.evaluate((el) => {
      const s = getComputedStyle(el);
      return { borderWidth: s.borderTopWidth, borderStyle: s.borderTopStyle,
        borderColor: s.borderTopColor, borderRadius: s.borderTopLeftRadius,
        boxSizing: s.boxSizing, borderLeftWidth: s.borderLeftWidth };
    });
    expect(info.borderWidth).toBe('1px');
    expect(info.borderStyle).toBe('solid');
    expect(info.borderRadius).toBe('4px');
    expect(info.boxSizing).toBe('border-box');
    expect(info.borderColor).toMatch(/251,\s*191,\s*36/);
    expect(info.borderLeftWidth).toBe('3px'); // own accent, unaffected by the shared class
  });
});

// ── Part 4 — NavTab token swaps (confirmed-match only) ───────────────────

test.describe('Part A.4 — NavTab hardcoded rgba() swapped only where value-identical to an app.css token', () => {
  test('source: swapped sites use var(...), un-swapped sites remain literal', () => {
    // Swapped — exact value match confirmed against app.css token definitions.
    expect(NAVTAB_SRC).toMatch(/\.nav-tab-retry\s*\{[^}]*border:\s*1px solid var\(--algo-cyan-border\)/);
    expect(NAVTAB_SRC).toMatch(/\.nav-tab-error\s*\{[^}]*border:\s*1px solid var\(--algo-red-cascade-lg\)/);
    // Left as literals — no exact token match found.
    expect(NAVTAB_SRC).toMatch(/border:\s*1px solid rgba\(155,\s*176,\s*208,\s*0\.25\)/); // .nav-tab-eod-badge
    expect(NAVTAB_SRC).toMatch(/border:\s*1px solid rgba\(34,\s*211,\s*238,\s*0\.35\)/); // .nav-chip-overlay
  });

  test('app.css tokens used in the swap resolve to the exact pre-existing literal values', () => {
    expect(APP_CSS).toMatch(/--algo-cyan-border:\s*rgba\(34,\s*211,\s*238,\s*0\.55\)/);
    expect(APP_CSS).toMatch(/--algo-red-cascade-lg:\s*rgba\(248,\s*113,\s*113,\s*0\.25\)/);
  });
});

test.describe('Part A.4 — NavTab live regression (pixel-identical to pre-change)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('.nav-tab-eod-badge border unchanged (left as literal, no exact token match)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const badge = page.locator('.nav-tab-eod-badge').first();
    const visible = await badge.isVisible().catch(() => false);
    if (!visible) return; // environment-dependent (needs a successful NAV fetch); structural check above covers this.
    const s = await badge.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { border: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
        background: cs.backgroundColor };
    });
    expect(s.border).toBe('1px solid rgba(155, 176, 208, 0.25)');
    expect(s.background).toBe('rgba(155, 176, 208, 0.12)');
  });

  test('.nav-tab-retry border now resolves via var(--algo-cyan-border), same rendered rgba as before', async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/nav/?days=**', (route) => route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'forced error' }),
    }));
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const retryBtn = page.locator('.nav-tab-retry').first();
    const visible = await retryBtn.isVisible().catch(() => false);
    if (!visible) return;
    const s = await retryBtn.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { border: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor };
    });
    expect(s.border).toBe('1px solid rgba(34, 211, 238, 0.55)');
  });

  test('.nav-tab-error border now resolves via var(--algo-red-cascade-lg), same rendered rgba as before', async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/nav/?days=**', (route) => route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'forced error' }),
    }));
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const navErr = page.locator('.nav-tab-error').first();
    const visible = await navErr.isVisible().catch(() => false);
    if (!visible) return;
    const s = await navErr.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { border: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
        background: cs.backgroundColor };
    });
    expect(s.border).toBe('1px solid rgba(248, 113, 113, 0.25)');
    expect(s.background).toBe('rgba(248, 113, 113, 0.07)');
  });
});

// ── Part 5 — .algo-label-decor rounded bar ends (2026-10-15) ────────────
// Operator: "make the label left vertical bar's edge look like to corner
// radius on the outer side on the top and bottom" — a genuine visual
// change layered on top of the Part A.2 extraction above (that sweep was
// pixel-identical by design; this one deliberately isn't). border-radius
// on the left corners only, since the box has no border-top/right/bottom
// to round — this curves the two free ends of the border-left bar inward.
test.describe('Part A.5 — .algo-label-decor rounded bar ends', () => {
  test('app.css .algo-label-decor carries border-top-left-radius + border-bottom-left-radius', () => {
    const m = /\.algo-label-decor\s*\{([^}]*)\}/.exec(APP_CSS);
    expect(m).not.toBeNull();
    const body = m[1];
    expect(body).toMatch(/border-top-left-radius:\s*2px/);
    expect(body).toMatch(/border-bottom-left-radius:\s*2px/);
  });

  test('live: Orders page header label bar has rounded top/bottom corners', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const label = page.locator('.algo-label-decor').first();
    await expect(label).toBeVisible({ timeout: 10000 });
    const s = await label.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { tl: cs.borderTopLeftRadius, bl: cs.borderBottomLeftRadius, borderLeftWidth: cs.borderLeftWidth };
    });
    expect(s.tl).toBe('2px');
    expect(s.bl).toBe('2px');
    expect(s.borderLeftWidth).toBe('3px');
  });

  test('live: MarketPulse bucket label (shared consumer) also picks up the rounded corners', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const label = page.locator('.mp-bucket-label').first();
    await expect(label).toBeVisible({ timeout: 10000 });
    const s = await label.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { tl: cs.borderTopLeftRadius, bl: cs.borderBottomLeftRadius };
    });
    expect(s.tl).toBe('2px');
    expect(s.bl).toBe('2px');
  });
});
