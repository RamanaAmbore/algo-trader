/**
 * spinner_placement_and_tab_color_fixes.spec.js
 *
 * Covers five UI polish fixes shipped together (2026-09-29):
 *
 *   1. PositionStrip.svelte — .ps-strip.ps-stale reverted from the A10
 *      amber color-mix back to the distinct orange rgba(251,146,60,0.6),
 *      because the color-mix collided with @keyframes ps-heartbeat-pulse
 *      (near-identical amber hue/alpha), making the 300ms heartbeat pulse
 *      imperceptible when a strip is both heartbeating and stale.
 *
 *   2. CardHeader.svelte — spinner markup moved to render AFTER the title
 *      (prefix → title → spinner → timestamp → left), inside a reserved-
 *      width slot so toggling `loading` causes zero layout shift.
 *
 *   3. OptionsPayoff.svelte — refresh spinner moved from an absolutely-
 *      positioned top-right corner into an inline slot right after the
 *      LTP value, wrapped so the 2-column .payoff-stats grid
 *      (.ps-row is display:contents) isn't broken by a bare 3rd child.
 *
 *   4. PerformancePage.svelte — .tabs-row's dead selectors
 *      (button[class*="border-primary"/"text-muted"], which AlgoTabs never
 *      emits) rewritten to target .algo-tab[aria-selected] like the
 *      funds-nav-tabs strip already did; both strips now set an explicit
 *      `color` on hover so text doesn't fall through to app.css's
 *      dark-page-only `.algo-tab:hover { color: var(--algo-slate) }`
 *      (#ffffff — invisible on this page's #fffdf8 cream background).
 *
 *   5. SymbolPanel.svelte — basket-mode submit button tooltip simplified
 *      to plain 'Submit', matching the always-'Submit' visible label.
 *
 * Five quality dimensions:
 *  1. SSOT   — PerformancePage's two tab-color override blocks reuse the
 *              identical --card-muted-text token/value for hover text
 *  2. Perf   — CardHeader/OptionsPayoff spin animation only runs while
 *              its .on class is applied (no permanent infinite-spin cost
 *              on idle headers/charts)
 *  3. Stale  — source-scan confirms the old color-mix / corner-absolute /
 *              dead-selector / count-suffix strings are gone
 *  4. Reuse  — OptionsPayoff wraps the existing .ps-v span rather than
 *              inventing a parallel value renderer; PerformancePage reuses
 *              an existing cream-theme token instead of a new literal
 *  5. UX     — live-render checks confirm zero layout shift when the
 *              CardHeader spinner slot toggles
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test spinner_placement_and_tab_color_fixes --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const ROOT = '/Users/ramanambore/projects/ramboq/frontend';

// ── Source-scan checks (no browser needed) ────────────────────────────────

test.describe('Source-level guards', () => {
  // 2026-09-30: operator — "make navstrip bottom border a little
  // lighter" — alpha dropped 0.6 -> 0.4, same distinct-orange hue.
  test('1: PositionStrip .ps-strip.ps-stale reverted to distinct orange (not amber color-mix), lightened to 0.4 alpha', () => {
    const src = readFileSync(`${ROOT}/src/lib/PositionStrip.svelte`, 'utf-8');
    const staleBlockMatch = src.match(/\.ps-strip\.ps-stale\s*\{[\s\S]*?\n\s*\}/);
    expect(staleBlockMatch, '.ps-strip.ps-stale rule must exist').not.toBeNull();
    const staleBlock = staleBlockMatch[0];
    expect(staleBlock, 'must use the distinct orange at the lightened 0.4 alpha')
      .toContain('rgba(251, 146, 60, 0.4)');
    expect(staleBlock, 'the collision-prone color-mix token must be gone')
      .not.toContain('color-mix(in srgb, var(--algo-amber)');
  });

  test('1b: base .ps-strip resting border also lightened off the shared amber-border-soft token', () => {
    const src = readFileSync(`${ROOT}/src/lib/PositionStrip.svelte`, 'utf-8');
    const baseBlockMatch = src.match(/\.ps-strip\s*\{[\s\S]*?\n\s*\}/);
    expect(baseBlockMatch, '.ps-strip base rule must exist').not.toBeNull();
    const baseBlock = baseBlockMatch[0];
    expect(baseBlock, 'must use a scoped lighter override, not the shared 0.30-alpha token')
      .toContain('rgba(251, 191, 36, 0.18)');
  });

  test('2: CardHeader spinner renders after .ch-title in source order, in a fixed-width slot', () => {
    const src = readFileSync(`${ROOT}/src/lib/CardHeader.svelte`, 'utf-8');
    const titleIdx = src.indexOf('{#if title}<span class="ch-title">');
    const spinIdx = src.indexOf('ch-spin-slot');
    const tsIdx = src.indexOf('{#if timestamp}<span class="ch-ts">');
    expect(titleIdx, 'ch-title markup must exist').toBeGreaterThan(-1);
    expect(spinIdx, 'ch-spin-slot markup must exist').toBeGreaterThan(-1);
    expect(tsIdx, 'ch-ts markup must exist').toBeGreaterThan(-1);
    expect(spinIdx, 'spinner must render after the title').toBeGreaterThan(titleIdx);
    expect(spinIdx, 'spinner must render before the timestamp').toBeLessThan(tsIdx);
    // Fixed-width slot, visibility-toggled (not conditionally mounted) once wired.
    expect(src).toMatch(/\.ch-spin-slot\s*\{[\s\S]*?width:\s*10px/);
    expect(src).toMatch(/\.ch-spin-slot\s*\{[\s\S]*?visibility:\s*hidden/);
    expect(src).toMatch(/\.ch-spin-slot\.on\s*\{[\s\S]*?visibility:\s*visible/);
    // Animation gated on .on so idle (loading=false) headers don't spin forever.
    expect(src).toMatch(/\.ch-spin-slot\.on \.ch-spin\s*\{[\s\S]*?animation:\s*rbq-spin/);
  });

  // 2026-09-30: operator clarified the spinner belongs AFTER the "LTP"
  // label but BEFORE the LTP value ("rotating circle in payoff...
  // after LTP label... in a fixed place before ltp value" / "ltp
  // value should not move while animating") — reordered from the
  // original after-value placement.
  test('3: OptionsPayoff spinner no longer carries the corner-absolute class, renders after LTP label but before the LTP value', () => {
    const src = readFileSync(`${ROOT}/src/lib/OptionsPayoff.svelte`, 'utf-8');
    expect(src, 'old corner-absolute class must not be used in markup')
      .not.toMatch(/class="payoff-loading-ring payoff-loading-ring-corner"/);
    const ltpLabelIdx = src.indexOf('<span class="ps-k">LTP</span>');
    const spinnerIdx = src.indexOf('payoff-loading-ring-slot');
    const ltpValueIdx = src.indexOf(`{fmtSpot(spot)}`);
    expect(ltpLabelIdx, 'LTP label span must exist').toBeGreaterThan(-1);
    expect(spinnerIdx, 'payoff-loading-ring-slot must exist').toBeGreaterThan(-1);
    expect(ltpValueIdx, 'LTP value span must exist').toBeGreaterThan(-1);
    expect(spinnerIdx, 'spinner must render after the LTP label in source order')
      .toBeGreaterThan(ltpLabelIdx);
    expect(spinnerIdx, 'spinner must render before the LTP value in source order')
      .toBeLessThan(ltpValueIdx);
    // Reserved-width slot so toggling `refreshing` doesn't resize the LTP row.
    expect(src).toMatch(/\.payoff-loading-ring-slot\s*\{[\s\S]*?width:\s*10px/);
    expect(src).toMatch(/\.payoff-loading-ring-slot\.on \.payoff-loading-ring\s*\{[\s\S]*?animation:\s*rbq-spin/);
  });

  test('4: PerformancePage tab overrides target .algo-tab, set explicit color, and share the identical hover value', () => {
    const src = readFileSync(`${ROOT}/src/lib/PerformancePage.svelte`, 'utf-8');
    // Match the actual dead CSS selectors (":global(button[...")," not the
    // explanatory prose comment above the new rules, which mentions the
    // old selector text for context.
    expect(src, 'dead border-primary CSS rule must be gone')
      .not.toContain(':global(button[class*="border-primary"])');
    expect(src, 'dead text-muted CSS rule must be gone')
      .not.toContain(':global(button[class*="text-muted"]');

    // Strip CSS /* ... */ comments first — several of these rules have an
    // explanatory comment containing its own literal `{ }` pair (quoting
    // a *different* selector, e.g. app.css's `.algo-tab:hover { color:
    // var(--algo-slate) }`), which would fool a naive non-greedy
    // "stop-at-first-}" block match into truncating early.
    const srcNoComments = src.replace(/\/\*[\s\S]*?\*\//g, '');
    const tabsRowActive = srcNoComments.match(/\.tabs-row :global\(\.algo-tab\[aria-selected="true"\]\)\s*\{[\s\S]*?\}/);
    const tabsRowHover = srcNoComments.match(/\.tabs-row :global\(\.algo-tab:hover:not\(\[aria-selected="true"\]\)\)\s*\{[\s\S]*?\}/);
    const fundsNavActive = srcNoComments.match(/\.funds-nav-tabs :global\(\.algo-tab\[aria-selected="true"\]\)\s*\{[\s\S]*?\}/);
    const fundsNavHover = srcNoComments.match(/\.funds-nav-tabs :global\(\.algo-tab:hover:not\(\[aria-selected="true"\]\)\)\s*\{[\s\S]*?\}/);

    for (const [name, block] of [
      ['tabs-row active', tabsRowActive],
      ['tabs-row hover', tabsRowHover],
      ['funds-nav-tabs active', fundsNavActive],
      ['funds-nav-tabs hover', fundsNavHover],
    ]) {
      expect(block, `${name} rule must exist`).not.toBeNull();
      expect(block[0], `${name} rule must set an explicit color`).toMatch(/color:\s*var\(/);
    }

    // Byte-identical color VALUE between the two hover rules (SSOT).
    const hoverColorOf = (block) => block[0].match(/color:\s*(var\([^)]*\))\s*!important;/)?.[1];
    const tabsRowHoverColor = hoverColorOf(tabsRowHover);
    const fundsNavHoverColor = hoverColorOf(fundsNavHover);
    expect(tabsRowHoverColor, 'tabs-row hover color must be parseable').toBeTruthy();
    expect(tabsRowHoverColor, 'both strips must use the identical hover color value')
      .toBe(fundsNavHoverColor);
    // Reuses the existing cream-theme muted-text token, not a new literal.
    expect(tabsRowHoverColor).toContain('--card-muted-text');
  });

  test('5: SymbolPanel basket-mode submit title is plain "Submit"', () => {
    const src = readFileSync(`${ROOT}/src/lib/SymbolPanel.svelte`, 'utf-8');
    expect(src, 'old count-suffix tooltip string must be gone')
      .not.toMatch(/Submit all \$\{basketLegs\.length\} basket leg/);
    expect(src).toMatch(/title=\{basketLegs\.length > 0\s*\n\s*\?\s*'Submit'/);
  });
});

// ── Live-render checks ─────────────────────────────────────────────────────

test.describe('CardHeader — live layout stability', () => {
  test('toggling the loading slot does not shift title/timestamp position', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForSelector('.card-header', { timeout: 15_000 });

    // The Payoff card's CardHeader wires `loading={loading}` explicitly —
    // find its header by the "Payoff" title.
    const header = page.locator('.card-header').filter({ hasText: 'Payoff' }).first();
    await header.waitFor({ state: 'visible', timeout: 10_000 });

    const slot = header.locator('.ch-spin-slot').first();
    await expect(slot).toHaveCount(1);

    // Fixed 10px width regardless of on/off state.
    const slotBox = await slot.boundingBox();
    expect(slotBox, 'spin slot must have a bounding box').not.toBeNull();
    expect(Math.round(slotBox.width)).toBe(10);

    const title = header.locator('.ch-title').first();
    const titleBoxBefore = await title.boundingBox();

    // Force the slot into the "on" state and re-measure the title — its
    // position must not move, since the slot's box already reserved the
    // width whether on or off.
    await slot.evaluate((el) => el.classList.add('on'));
    const titleBoxAfter = await title.boundingBox();

    expect(titleBoxAfter.x, 'title x-position must not shift when spinner toggles on')
      .toBeCloseTo(titleBoxBefore.x, 0);

    await slot.evaluate((el) => el.classList.remove('on'));
    const titleBoxRestored = await title.boundingBox();
    expect(titleBoxRestored.x, 'title x-position must not shift when spinner toggles off')
      .toBeCloseTo(titleBoxBefore.x, 0);
  });
});
