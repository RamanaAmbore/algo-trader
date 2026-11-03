/**
 * derivatives_card_spacing_consistency.spec.js
 *
 * Guards the "wasted whitespace is inconsistent across grids" fix on
 * /admin/derivatives (2026-11-03). Payoff/Legs/Snapshot are the reference
 * cards — their CardHeader-to-content wrappers (`.card-body`/`.cand-scroll`/
 * `.byund-scroll`) all set `margin-top: 0` so the header's own bottom
 * border and the content's top edge read as a single seam with ZERO
 * visual gap. The two Greeks/Risk cards (`.opt-block`, converted from a
 * bare unwrapped div to the shared `algo-status-card cmd-surface p-3` +
 * <CardHeader> pattern earlier in the same session) carried a leftover
 * `gap: 0.5rem` flex gap between CardHeader and their `.opt-kv` content —
 * an 8px pocket of dead space the other three cards don't have. Fixed by
 * zeroing `.opt-block`'s gap.
 *
 * Also guards a real card-padding fix, same day: every derivatives card
 * (`.algo-status-card`) inherited a hardcoded `padding: 0.75rem` from
 * that class's own global base rule in `(algo)/+layout.svelte` — a
 * Tailwind `p-3` utility class on the same element never actually
 * controlled it (confirmed by direct measurement, not source-reading:
 * swapping it for `p-1.5` measured no change at all). Fixed with an
 * explicit, higher-specificity override scoped to this page's own
 * cards (`.opt-payoff-full.algo-status-card` etc.), not a global change
 * to `.algo-status-card` itself.
 *
 * Five quality dimensions:
 *   1. SSOT      — one reference gap value (0px) shared by all 5 cards;
 *                  no card gets its own bespoke header-to-content spacing.
 *   2. Perf      — page load + all 5 measurements complete well under the
 *                  spec's own timeout budget.
 *   3. Stale     — regresses to a bare numeric assertion (not a snapshot
 *                  of whatever the current pixel value happens to be) so
 *                  reverting the `.opt-block` gap fix fails this test —
 *                  verified live before shipping (see commit notes).
 *   4. Reuse     — measures the real CardHeader DOM node + its very next
 *                  element sibling for every card, the same structural
 *                  relationship on all 5, not a per-card bespoke selector
 *                  pair.
 *   5. UX        — checked at both desktop (1440px) and mobile (375px)
 *                  viewports, matching this app's mobile-first convention.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_card_spacing_consistency.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Module-level cached token — one login per spec run.
let _token = null;
async function injectSession(page) {
  if (!_token) {
    for (const delay of [0, 6000, 15000]) {
      if (delay) await new Promise((res) => setTimeout(res, delay));
      for (const u of ['ambore', 'rambo']) {
        const r = await page.request.post(`${BASE}/api/auth/login`, {
          data: { username: u, password: PASS },
        });
        if (r.ok()) { _token = (await r.json()).access_token; break; }
      }
      if (_token) break;
    }
    if (!_token) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _token);
}

// Reference cards — always mounted, no strategy needed. Greeks/Risk only
// mount once a strategy/underlying has loaded (behind `{#if strategy}`).
const REFERENCE_CARDS = [
  { name: 'Payoff', sel: '.opt-payoff' },
  { name: 'Legs', sel: '.opt-legs-card' },
  { name: 'Snapshot', sel: '.opt-byund-card' },
];
const GREEKS_RISK_CARDS = [
  { name: 'Greeks', sel: '.opt-block:nth-of-type(1)' },
  { name: 'Risk', sel: '.opt-block:nth-of-type(2)' },
];

/**
 * For each card selector, finds its `.card-header` and measures the
 * vertical gap (px) between the header's bottom edge and the very next
 * element sibling's top edge — the real on-screen CardHeader-to-content
 * seam, not a value read off a single CSS rule.
 */
async function measureHeaderToContentGaps(page, cards) {
  return page.evaluate((cardList) => {
    const out = [];
    for (const c of cardList) {
      const cardEl = document.querySelector(c.sel);
      if (!cardEl) { out.push({ name: c.name, found: false, gap: null }); continue; }
      const header = cardEl.querySelector('.card-header');
      const contentEl = header ? header.nextElementSibling : null;
      if (!header || !contentEl) { out.push({ name: c.name, found: false, gap: null }); continue; }
      const headerRect = header.getBoundingClientRect();
      const contentRect = contentEl.getBoundingClientRect();
      out.push({
        name: c.name,
        found: true,
        gap: +(contentRect.top - headerRect.bottom).toFixed(2),
      });
    }
    return out;
  }, cards);
}

async function gotoDerivativesWithStrategy(page) {
  await injectSession(page);
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'load', timeout: 30_000 });
  // Greeks/Risk only mount once the auto-default underlying's strategy
  // has loaded — give it real time on a possibly-cold dev backend.
  await page.waitForSelector('.opt-block', { timeout: 20_000 });
}

for (const viewport of [
  { label: 'desktop 1440×900', width: 1440, height: 900 },
  { label: 'mobile 375×800', width: 375, height: 800 },
]) {
  test.describe(`derivatives card spacing consistency — ${viewport.label}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test('Payoff/Legs/Snapshot reference cards all measure a zero header-to-content gap', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);
      const gaps = await measureHeaderToContentGaps(page, REFERENCE_CARDS);

      for (const g of gaps) {
        expect(g.found, `${g.name} card or its CardHeader/content sibling was not found`).toBe(true);
      }
      for (const g of gaps) {
        // Reference pattern — confirmed by prior CardHeader/.algo-grid-chrome
        // family comments: header bottom border + content top edge are one
        // seam, zero gap. Tight tolerance (not an exact-0 assertion) to
        // tolerate sub-pixel rendering differences across engines.
        expect(g.gap, `${g.name} header-to-content gap`).toBeGreaterThanOrEqual(-0.5);
        expect(g.gap, `${g.name} header-to-content gap`).toBeLessThanOrEqual(0.5);
      }
    });

    test('Greeks and Risk cards match the Payoff/Legs/Snapshot zero-gap reference', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);

      const refGaps = await measureHeaderToContentGaps(page, REFERENCE_CARDS);
      const gkGaps = await measureHeaderToContentGaps(page, GREEKS_RISK_CARDS);

      for (const g of refGaps) expect(g.found, `reference ${g.name} not found`).toBe(true);
      for (const g of gkGaps) expect(g.found, `${g.name} not found — strategy may not have loaded`).toBe(true);

      const refGap = refGaps[0].gap; // all three references share one value, asserted above
      for (const g of gkGaps) {
        // The regression this guards: `.opt-block`'s leftover flex `gap`
        // (0.5rem / 8px) left a dead pocket of space under the Greeks/Risk
        // headers that the reference cards don't have. Assert within the
        // SAME tight tolerance as the reference assertion above, anchored
        // to the measured reference value rather than a hardcoded literal
        // so a future change to the reference pattern doesn't silently
        // desync this test from it.
        expect(
          Math.abs(g.gap - refGap),
          `${g.name} gap (${g.gap}px) must match the Payoff/Legs/Snapshot reference (${refGap}px)`
        ).toBeLessThanOrEqual(0.5);
        // Belt-and-suspenders absolute check — catches the literal pre-fix
        // regression (8px / 0.5rem gap) even if the reference measurement
        // itself were ever broken by an unrelated change.
        expect(g.gap, `${g.name} header-to-content gap`).toBeLessThanOrEqual(0.5);
      }
    });

    test('every derivatives card uses Pulse\'s own card padding (p-1.5, 0.375rem) — not the old p-3 (0.75rem, 2x too generous)', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);
      const ALL_CARDS = [...REFERENCE_CARDS, ...GREEKS_RISK_CARDS];
      const pads = await page.evaluate((cards) => {
        const out = [];
        for (const c of cards) {
          const el = document.querySelector(c.sel);
          if (!el) { out.push({ name: c.name, found: false }); continue; }
          const cs = getComputedStyle(el);
          out.push({
            name: c.name,
            found: true,
            // Rounded — real rendering can carry sub-pixel rounding, and
            // this assertion only cares about the Tailwind step (6px vs
            // 12px), not fractional noise.
            top: Math.round(parseFloat(cs.paddingTop)),
            left: Math.round(parseFloat(cs.paddingLeft)),
          });
        }
        return out;
      }, ALL_CARDS);

      for (const p of pads) expect(p.found, `${p.name} card not found`).toBe(true);
      for (const p of pads) {
        // 0.375rem @ 16px root = 6px (Tailwind p-1.5) — not 12px (p-3).
        expect(p.top, `${p.name} padding-top`).toBe(6);
        expect(p.left, `${p.name} padding-left`).toBe(6);
      }
    });

    test('Greeks/Risk CardHeader height — and its border-left decor — matches Snapshot\'s, not a shorter box', async ({ page }) => {
      // Guards the 2026-11-03 fix (operator: "for greeks and risk labels,
      // the left border should be present like snapshot label"). The
      // `.algo-label-decor` border-left was ALWAYS present and the same
      // color on both — confirmed by direct measurement before this fix
      // shipped — the real defect was `.card-header` collapsing to the
      // bare title's own ~15.4px line height (Greeks/Risk pass
      // `showControls={false}`, so no CardControls button row inflates
      // the row the way Snapshot's does), making the border-left bar
      // ~64% as tall and visually read as thin/absent. Fixed with an
      // explicit `min-height` on `.opt-block .card-header`, scoped to
      // this page only — not a change to CardHeader's own default.
      await gotoDerivativesWithStrategy(page);
      const heights = await page.evaluate((cards) => {
        const out = [];
        for (const c of cards) {
          const card = document.querySelector(c.sel);
          const header = card?.querySelector('.card-header');
          if (!header) { out.push({ name: c.name, found: false }); continue; }
          out.push({ name: c.name, found: true, h: +header.getBoundingClientRect().height.toFixed(1) });
        }
        return out;
      }, [...REFERENCE_CARDS.filter((c) => c.name === 'Snapshot'), ...GREEKS_RISK_CARDS]);

      for (const h of heights) expect(h.found, `${h.name} card/header not found`).toBe(true);
      const refH = heights.find((h) => h.name === 'Snapshot').h;
      for (const h of heights) {
        expect(Math.abs(h.h - refH), `${h.name} .card-header height (${h.h}px) must match Snapshot's (${refH}px)`)
          .toBeLessThanOrEqual(0.5);
      }
    });
  });
}
