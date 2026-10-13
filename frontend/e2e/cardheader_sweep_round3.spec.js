/**
 * cardheader_sweep_round3.spec.js
 *
 * 2026-10-13 "CardHeader sweep, round 3 — label clarity" — Parts A and B
 * only (Parts C/D are a companion agent's disjoint work on dashboard's
 * chart card border/Y-axis-label fix + a title rename, in
 * dashboard/+page.svelte / NavTab.svelte / OptionsPayoff.svelte /
 * PriceChart.svelte — not touched or tested here).
 *
 * Part A — `.ch-title` (CardHeader.svelte) gets a `border-left: 3px
 *   solid currentColor` + left padding, matching MarketPulse's own
 *   `.mp-bucket-label` reference pattern, and its default color
 *   changes from amber (`--c-action`, which collided with AlgoTabs'
 *   own default untagged-tab color) to white (`--algo-slate`).
 *
 *   IMPORTANT finding from implementation (not assumed from the plan):
 *   the component-level fallback alone is dead code for every algo-app
 *   CardHeader — `(algo)/+layout.svelte`'s OWN `:global(body)` block
 *   declares `--ch-title-color: var(--c-action)` explicitly, and a
 *   body-level custom property always wins over CardHeader.svelte's
 *   internal `var(--ch-title-color, <fallback>)` fallback regardless
 *   of what that fallback is (same ancestor-wins rule as the existing
 *   `--card-divider` note in that same file). Both places were changed
 *   in the same commit; this spec verifies the REAL rendered color on
 *   a live algo page, not just the component source.
 *
 * Part B — enumerated all 21 files with a live `<CardHeader>` mount
 *   (fresh grep, not the stale round-1/2 list) and found NO panel
 *   rendering with neither a `title` prop nor a `left`/`prefix` snippet
 *   providing visible text. Three borderline cases were investigated
 *   and are NOT fixed, each for a stated reason:
 *     1. dashboard/+page.svelte:1902 (Capital/Equity tabbed card) — no
 *        `title`, but its `middle` snippet always renders AlgoTabs
 *        labeled P&L/Capital/Equity, which the card's own 2026-10
 *        comment documents as a DELIBERATE choice ("AlgoTabs still IS
 *        the card identifier"). Also the companion agent's file for
 *        Parts C/D — left untouched to avoid a merge collision.
 *     2. admin/derivatives/+page.svelte:5704 (Legs card) — no `title`,
 *        but its `middle` snippet always renders AlgoTabs labeled
 *        Legs/Exp close, and its own comment documents this as
 *        deliberate ("Tabs replace the static title"). The only
 *        existing wording to reuse (`label="Legs"`, the CardControls
 *        id) would duplicate the "Legs" tab text right next to it —
 *        the same title-vs-tab collision class Part D is fixing
 *        elsewhere. Flagged here for an explicit operator decision
 *        rather than guessed.
 *     3. order/OrderTicket.svelte:2573 (standalone-mode CardHeader) —
 *        only renders `{#if standalone}`, and the component's ONLY
 *        live mount (SymbolPanel.svelte:2515) passes
 *        `standalone={false}`. Not reachable from any route today, so
 *        not "live" per the plan's own enumeration instruction, and
 *        per this repo's test-for-every-change rule, an unreachable
 *        code path can't be given a real browser test anyway.
 *   This test suite proves (1) and (2) are genuinely, visibly labeled
 *   today via their tab strips, backing the "zero edits" decision with
 *   evidence rather than just a source-reading claim.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/cardheader_sweep_round3.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Parses a computed `rgb(...)`/`rgba(...)` color string into
 *  {r,g,b,a} (a defaults to 1 for 3-channel `rgb(...)`). */
function parseRGBA(str) {
  const m = /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/.exec(str || '');
  if (!m) return null;
  return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] !== undefined ? Number(m[4]) : 1 };
}

test.describe('CardHeader sweep round 3 — Part A: .ch-title border-left + gap + neutral default', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('plain-title CardHeader (OrderBook, "Order Book") — border-left matches text color (both white, --algo-slate), real left padding before text', async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/orders/held/**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ held: [] }),
    }));
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    // OrderBook.svelte defaults `title = 'Order Book'` and never overrides
    // --ch-title-color — the exact "plain title, amber-free after this
    // change" panel the plan's own Test 1 calls for.
    const title = page.locator('.ob-root .ch-title', { hasText: 'Order Book' }).first();
    await expect(title).toBeVisible({ timeout: 20_000 });

    const info = await title.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        color: s.color,
        borderLeftColor: s.borderLeftColor,
        borderLeftWidth: s.borderLeftWidth,
        borderLeftStyle: s.borderLeftStyle,
        paddingLeft: s.paddingLeft,
      };
    });

    expect(info.borderLeftWidth, 'border-left must actually render (non-zero width)').toBe('3px');
    expect(info.borderLeftStyle).toBe('solid');

    const textRgba = parseRGBA(info.color);
    const borderRgba = parseRGBA(info.borderLeftColor);
    expect(textRgba, `title color should be rgb(), got "${info.color}"`).not.toBeNull();
    expect(borderRgba, `border-left-color should be rgb(), got "${info.borderLeftColor}"`).not.toBeNull();

    // border-left: 3px solid currentColor — the border must track the
    // SAME resolved color as the text, not a second independent color.
    expect(borderRgba.r).toBeCloseTo(textRgba.r, 0);
    expect(borderRgba.g).toBeCloseTo(textRgba.g, 0);
    expect(borderRgba.b).toBeCloseTo(textRgba.b, 0);

    // New neutral default — --algo-slate (#ffffff) — not the old amber
    // (#fbbf24 / rgb(251,191,36)), which collided with AlgoTabs' own
    // default untagged-tab color.
    expect(textRgba.r).toBeCloseTo(255, 0);
    expect(textRgba.g).toBeCloseTo(255, 0);
    expect(textRgba.b).toBeCloseTo(255, 0);

    // Measurable left gap before the title text starts — was 0 before
    // this fix (title sat flush against `.ch-left`'s own edge).
    const paddingPx = parseFloat(info.paddingLeft);
    expect(paddingPx, `padding-left should be > 0, got "${info.paddingLeft}"`).toBeGreaterThan(0);
  });
});

test.describe('CardHeader sweep round 3 — Part A regression guard: explicit --ch-title-color override still wins', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('/faq (public light theme, .pub-viewport sets --ch-title-color: #1a2744) renders UNCHANGED navy, not the new white default', async ({ page }) => {
    // Public page — no auth needed. This is the ONLY real explicit
    // `--ch-title-color` override found anywhere in the app (confirmed
    // via grep across src/ before writing this test — there is no
    // per-panel override in Derivatives or elsewhere; both the plan's
    // own hint and this test's premise were verified empirically).
    await page.goto('/faq', { waitUntil: 'domcontentloaded' });

    const title = page.locator('.flow-card .ch-title', { hasText: 'NAV & Profit Distribution Flow' }).first();
    await expect(title).toBeVisible({ timeout: 20_000 });

    const info = await title.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.color, borderLeftColor: s.borderLeftColor, borderLeftWidth: s.borderLeftWidth };
    });

    const textRgba = parseRGBA(info.color);
    expect(textRgba, `title color should be rgb(), got "${info.color}"`).not.toBeNull();

    // #1a2744 = rgb(26, 39, 68) — the explicit .pub-viewport override,
    // UNCHANGED by Part A's default-color swap.
    expect(textRgba.r).toBeCloseTo(26, 0);
    expect(textRgba.g).toBeCloseTo(39, 0);
    expect(textRgba.b).toBeCloseTo(68, 0);
    // Must NOT have silently fallen through to the new white default.
    expect(textRgba.r).not.toBeCloseTo(255, 0);

    // The new border-left is additive everywhere (including /faq, a
    // real visual change there — flagged in the handback report, not
    // a regression) but must track the SAME override color via
    // currentColor, not some other color.
    expect(info.borderLeftWidth).toBe('3px');
    const borderRgba = parseRGBA(info.borderLeftColor);
    expect(borderRgba.r).toBeCloseTo(26, 0);
    expect(borderRgba.g).toBeCloseTo(39, 0);
    expect(borderRgba.b).toBeCloseTo(68, 0);
  });
});

test.describe('CardHeader sweep round 3 — Part B: tab-strip-as-title panels remain genuinely labeled (zero-edit decision, evidence)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('admin/derivatives Legs card — no title prop, but AlgoTabs "Legs"/"Exp close" in middle is always visible', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const legsTab = page.getByRole('tab', { name: /^Legs/ }).first();
    await expect(legsTab, 'Legs card must show its own identifying tab label, confirming it is not a bare unlabeled header').toBeVisible({ timeout: 20_000 });
  });

  test('dashboard Capital/Equity card — no title prop, but AlgoTabs "P&L"/"Capital"/"Equity" in middle is always visible', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const capEqTab = page.getByRole('tab', { name: 'Capital', exact: true }).first();
    await expect(capEqTab, 'Capital/Equity card must show its own identifying tab labels, confirming it is not a bare unlabeled header').toBeVisible({ timeout: 20_000 });
  });
});
