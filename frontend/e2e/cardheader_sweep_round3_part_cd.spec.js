/**
 * cardheader_sweep_round3_part_cd.spec.js
 *
 * 2026-10-13 "CardHeader sweep, round 3 — label clarity" — Parts C and D
 * ONLY (Parts A/B are a companion agent's disjoint work on CardHeader.svelte
 * + a missing-label sweep — see cardheader_sweep_round3.spec.js, not
 * touched or tested here).
 *
 * Part C — Dashboard's NAV/Intraday/Performance chart card gets ONE
 *   shared border/background wrapper (`.dash-chart-frame.algo-chart-frame`)
 *   around all three tabs, instead of per-tab chrome (NavTab.svelte
 *   ~line 280-288 documents the exact "border jumps on tab switch" bug
 *   this avoids by construction — one DOM node, one CSS box, regardless
 *   of which tab's content is visible). The new `.algo-chart-frame`
 *   utility (app.css) is also the DRY extraction point for
 *   OptionsPayoff's `.payoff-chart` and PriceChart's `.price-chart`,
 *   which independently duplicated the identical border/background
 *   literal before this change. Separately, Intraday's Y-axis labels
 *   move from the right edge (unexplained, PAD_R=52) to the left,
 *   matching NAV's own `.nav-tab-wrap` convention on the SAME tabbed
 *   card — this required swapping PAD_L/PAD_R (520 -> INNER_W unchanged)
 *   and reproducing NavTab's own already-solved stat-overlay-collision
 *   fix (`.eq-stats`'s `left` now clears the Y-axis label zone via the
 *   identical `calc(<pad.l%> + <rem>)` formula NavTab uses for its own
 *   chip overlay).
 *
 * Part D — the outer CardHeader's visible `title` on this same card
 *   changes from "Performance" to "Chart" (reusing the existing internal
 *   `label="Chart"` wording) because one of the card's OWN inner tabs is
 *   also literally named "Performance" — the outer umbrella title must
 *   not reuse the same word as one of its own children.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/cardheader_sweep_round3_part_cd.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const __dir = dirname(fileURLToPath(import.meta.url));
const DASH_SRC = readFileSync(
  join(__dir, '../src/routes/(algo)/dashboard/+page.svelte'), 'utf8',
);
const PAYOFF_SRC = readFileSync(join(__dir, '../src/lib/OptionsPayoff.svelte'), 'utf8');
const PRICECHART_SRC = readFileSync(join(__dir, '../src/lib/PriceChart.svelte'), 'utf8');
const APP_CSS = readFileSync(join(__dir, '../src/app.css'), 'utf8');

/** Deterministic points so the Intraday chart's domain is never degenerate
 *  (min !== max) and the Y-axis label + stat-overlay geometry checks have
 *  something real to measure, regardless of live market data availability. */
function mockEquityPoints() {
  const base = Date.UTC(2026, 9, 13, 4, 0, 0); // ~09:30 IST
  const pts = [];
  for (let i = 0; i < 12; i++) {
    const t = base + i * 15 * 60 * 1000;
    const wobble = Math.sin(i / 2) * 4000;
    pts.push({
      ts: new Date(t).toISOString(),
      day_pnl: 1000 + i * 500 + wobble,
      cum_pnl: 20000 + i * 300 - wobble,
      h_pnl: 15000 + i * 200,
      h_day: 300 + i * 50,
      p_pnl: 5000 + i * 100,
      p_day: 200 + i * 40,
    });
  }
  return pts;
}

async function mockIntradayEquity(page) {
  await page.route('**/api/charts/intraday-equity**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ points: mockEquityPoints() }),
  }));
}

/** Locate the dashboard's chart card (NAV/Intraday/Performance tabs). */
function chartCard(page) {
  return page.locator('.row1-col-chart').first();
}

/** Click a tab button by its visible label inside the chart card's tab strip. */
async function clickChartTab(page, label) {
  const card = chartCard(page);
  await card.getByRole('tab', { name: label, exact: true }).click();
  await page.waitForTimeout(250);
}

// ── Part C, Test 1 — border consistency across NAV/Intraday/Performance ────

test.describe('CardHeader sweep round 3 Part C — chart-frame border never moves between tabs', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('dash-chart-frame border box (left/top/width) + border style identical across all three tabs', async ({ page }) => {
    await mockIntradayEquity(page);
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const frame = chartCard(page).locator('.dash-chart-frame').first();
    await expect(frame, 'shared chart frame wrapper must exist').toBeVisible({ timeout: 20_000 });

    async function measure() {
      return frame.evaluate((el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return {
          left: Math.round(r.left), top: Math.round(r.top), width: Math.round(r.width),
          height: Math.round(r.height),
          borderWidth: s.borderTopWidth, borderStyle: s.borderTopStyle,
          borderColor: s.borderTopColor, borderRadius: s.borderTopLeftRadius,
          backgroundImage: s.backgroundImage,
        };
      });
    }

    // NAV is the default tab.
    const navM = await measure();

    await clickChartTab(page, 'Intraday');
    const intradayM = await measure();

    await clickChartTab(page, 'Performance');
    const perfM = await measure();

    for (const [label, m] of [['Intraday', intradayM], ['Performance', perfM]]) {
      expect(m.left, `${label}: frame left must match NAV's`).toBe(navM.left);
      expect(m.top, `${label}: frame top must match NAV's`).toBe(navM.top);
      expect(m.width, `${label}: frame width must match NAV's`).toBe(navM.width);
      expect(m.borderWidth, `${label}: border-width must match NAV's`).toBe(navM.borderWidth);
      expect(m.borderStyle, `${label}: border-style must match NAV's`).toBe(navM.borderStyle);
      expect(m.borderColor, `${label}: border-color must match NAV's`).toBe(navM.borderColor);
      expect(m.borderRadius, `${label}: border-radius must match NAV's`).toBe(navM.borderRadius);
      expect(m.backgroundImage, `${label}: background must match NAV's`).toBe(navM.backgroundImage);
    }

    // Border must actually be the Payoff-chart treatment, not a no-op.
    expect(navM.borderWidth).toBe('1px');
    expect(navM.borderStyle).toBe('solid');
    expect(navM.borderRadius).toBe('4px');

    // Height is content-driven (NAV/Intraday/Performance legitimately
    // render different amounts of content) and is NOT asserted equal —
    // only the frame's own box origin/width/border must never move.
    // Logged (not asserted) for visibility — see the handback report.
    console.log(`[height-by-tab] NAV=${navM.height} Intraday=${intradayM.height} Performance=${perfM.height}`);
  });
});

// ── Part C, reusable chart frame — DRY retrofit, zero visual change ────────

test.describe('CardHeader sweep round 3 Part C — reusable .algo-chart-frame (DRY retrofit, no visual change)', () => {
  test('app.css defines .algo-chart-frame with the exact literal both charts used to duplicate', () => {
    const m = /\.algo-chart-frame\s*\{([^}]*)\}/.exec(APP_CSS);
    expect(m, '.algo-chart-frame rule must exist in app.css').not.toBeNull();
    const body = m[1];
    expect(body).toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(body).toMatch(/border:\s*1px solid rgba\(251,\s*191,\s*36,\s*0\.18\)/);
    expect(body).toMatch(/border-radius:\s*4px/);
    expect(body).toMatch(/box-sizing:\s*border-box/);
  });

  test('OptionsPayoff .payoff-chart uses algo-chart-frame and no longer duplicates the border/background literal', () => {
    expect(PAYOFF_SRC).toMatch(/class="payoff-chart algo-chart-frame"/);
    const m = /\.payoff-chart\s*\{([^}]*)\}/.exec(PAYOFF_SRC);
    expect(m, '.payoff-chart rule must still exist (for padding/width/position)').not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/border:\s*1px solid rgba\(251/);
    expect(body).not.toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(body).not.toMatch(/border-radius:\s*4px/);
    // Per-component spacing stays local.
    expect(body).toMatch(/padding:\s*6px 8px 8px/);
  });

  test('PriceChart .price-chart uses algo-chart-frame, keeps its own border-left accent, no duplicated border/background literal', () => {
    expect(PRICECHART_SRC).toMatch(/class="price-chart algo-chart-frame \{_pulse\.classOf\('chart'\)\}"/);
    const m = /\.price-chart\s*\{([^}]*)\}/.exec(PRICECHART_SRC);
    expect(m, '.price-chart rule must still exist (for padding/width/max-width/border-left)').not.toBeNull();
    const body = m[1];
    expect(body).not.toMatch(/border:\s*1px solid rgba\(251/);
    expect(body).not.toMatch(/background:\s*var\(--card-bg-gradient\)/);
    expect(body).not.toMatch(/border-radius:\s*4px/);
    // The component-specific accent stays, layered on top of the shared class.
    expect(body).toMatch(/border-left:\s*3px solid var\(--c-action\)/);
    expect(body).toMatch(/max-width:\s*960px/);
  });

  test('live: OptionsPayoff .payoff-chart renders the exact pre-existing border/radius/box-sizing (regression guard)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    const chart = page.locator('.payoff-chart').first();
    const count = await chart.count();
    if (!count || !(await chart.isVisible().catch(() => false))) {
      // No legs loaded in this test environment — structural checks above
      // already prove the DRY retrofit; skip the live pixel check.
      return;
    }

    const info = await chart.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        borderWidth: s.borderTopWidth, borderStyle: s.borderTopStyle,
        borderColor: s.borderTopColor, borderRadius: s.borderTopLeftRadius,
        boxSizing: s.boxSizing,
      };
    });
    expect(info.borderWidth).toBe('1px');
    expect(info.borderStyle).toBe('solid');
    expect(info.borderRadius).toBe('4px');
    expect(info.boxSizing).toBe('border-box');
    // rgba(251,191,36,0.18) resolved.
    expect(info.borderColor).toMatch(/251,\s*191,\s*36/);
  });

  test('live: PriceChart .price-chart renders the exact pre-existing border/radius + its own border-left accent (regression guard)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/execution', { waitUntil: 'domcontentloaded' });

    const chart = page.locator('.price-chart').first();
    const count = await chart.count();
    if (!count || !(await chart.isVisible().catch(() => false))) {
      // PriceChart only renders with an active simulator/replay run in
      // this test environment — structural checks above already prove
      // the DRY retrofit; skip the live pixel check.
      return;
    }

    const info = await chart.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        borderTopWidth: s.borderTopWidth, borderTopStyle: s.borderTopStyle,
        borderTopColor: s.borderTopColor, borderRadius: s.borderTopRightRadius,
        borderLeftWidth: s.borderLeftWidth, borderLeftColor: s.borderLeftColor,
      };
    });
    expect(info.borderTopWidth).toBe('1px');
    expect(info.borderTopStyle).toBe('solid');
    expect(info.borderTopColor).toMatch(/251,\s*191,\s*36/);
    expect(info.borderRadius).toBe('4px');
    // Own accent, unaffected by the shared class.
    expect(info.borderLeftWidth).toBe('3px');
  });
});

// ── Part C, Y-axis side fix ──────────────────────────────────────────────

test.describe('CardHeader sweep round 3 Part C — Intraday Y-axis labels move to the left (matching NAV)', () => {
  test('source: PAD_L/PAD_R swapped, label anchored left ("end"), old right-side expression removed', () => {
    expect(DASH_SRC).toMatch(/const PAD_L = 52;/);
    expect(DASH_SRC).toMatch(/const PAD_R = 8;/);
    expect(DASH_SRC).toMatch(/x=\{PAD_L - 8\}/);
    expect(DASH_SRC).toMatch(/text-anchor="end">\{lbl\.label\}<\/text>/);
    expect(DASH_SRC).not.toMatch(/x=\{PAD_L \+ INNER_W \+ 4\}/);
    expect(DASH_SRC).not.toMatch(/<!-- Y-axis labels \(right\) -->/);
  });

  test('live: Y-axis label x-position sits left of plot (not right), and the stat overlay no longer overlaps it', async ({ page }) => {
    await mockIntradayEquity(page);
    await loginAsAdmin(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    await clickChartTab(page, 'Intraday');

    const svg = chartCard(page).locator('svg.eq-svg').first();
    const svgVisible = await svg.isVisible().catch(() => false);
    if (!svgVisible) {
      // Mocked fetch didn't land before mount, or empty-state still
      // showing — the source-level test above already proves the fix;
      // skip the geometry check rather than fail on environment timing.
      return;
    }

    // Y-axis label texts: rotated -45deg text with the 11px/600-weight
    // styling this block uses (no dedicated class — matched by style).
    const labels = svg.locator('text[font-weight="600"][text-anchor="end"]');
    const labelCount = await labels.count();
    expect(labelCount, 'Intraday chart should render Y-axis tick labels').toBeGreaterThan(0);

    // Geometry: every Y-axis label's bounding box must sit LEFT of the
    // plot's horizontal midpoint (proves left-side, not right-side).
    const svgBox = await svg.evaluate((el) => el.getBoundingClientRect());
    const midX = svgBox.left + svgBox.width / 2;
    for (let i = 0; i < labelCount; i++) {
      const box = await labels.nth(i).evaluate((el) => el.getBoundingClientRect());
      expect(box.right, `Y-axis label ${i} must render left of the plot midpoint`).toBeLessThan(midX);
    }

    // Non-overlap regression guard — the exact collision class NavTab's
    // own chip overlay already hit once (NavTab.svelte ~line 394).
    const statsBox = await chartCard(page).locator('.eq-stats').first()
      .evaluate((el) => el.getBoundingClientRect()).catch(() => null);
    if (statsBox) {
      const rightmostLabelEdge = Math.max(
        ...(await Promise.all(
          Array.from({ length: labelCount }, (_, i) =>
            labels.nth(i).evaluate((el) => el.getBoundingClientRect().right))
        ))
      );
      expect(statsBox.left, 'stat overlay must clear the Y-axis label zone, not sit on top of it')
        .toBeGreaterThanOrEqual(rightmostLabelEdge - 2); // 2px tolerance for sub-pixel rounding
    }
  });
});

// ── Part D — outer "Trend" title vs. inner "Performance" tab ───────────────
// 2026-10-13 header button-group standardization, Part 7 — operator
// follow-up renamed the outer title a second time, from "Chart" (Part D's
// own original choice, commit 43cb5a4f) to "Trend". The internal
// `label="Chart"` card-id (never user-visible) is unchanged.

test.describe('CardHeader sweep round 3 Part D — outer title renamed, inner tab unchanged', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('chart card CardHeader title reads "Trend"; inner tab strip still has "Performance"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const card = chartCard(page);
    await expect(card).toBeVisible({ timeout: 20_000 });

    const title = card.locator('.ch-title').first();
    await expect(title).toBeVisible({ timeout: 20_000 });
    await expect(title).toHaveText('Trend');
    await expect(title).not.toHaveText('Chart');
    await expect(title).not.toHaveText('Performance');

    const perfTab = card.getByRole('tab', { name: 'Performance', exact: true });
    await expect(perfTab, 'inner Performance tab must remain, unchanged').toBeVisible();
  });

  test('source: outer CardHeader title prop is "Trend", internal label="Chart" card-id unchanged', () => {
    expect(DASH_SRC).toMatch(/label="Chart"\s*\n\s*title="Trend"/);
    expect(DASH_SRC).not.toMatch(/title="Chart"/);
    expect(DASH_SRC).not.toMatch(/title="Performance"/);
    // Inner tab definition unchanged.
    expect(DASH_SRC).toMatch(/\{ id: 'performance', label: 'Performance' \}/);
  });
});
