/**
 * derivatives_payoff_no_flash_on_refetch.spec.js
 *
 * Regression suite for the Payoff chart's B1 (full-chart flash) and C2
 * (NSE overlay stepping) fixes.
 *
 * Background (see CLAUDE.md's derivatives Payoff-chart plan, B1/C2):
 *   B1 — OptionsPayoff.svelte's `$effect` used to fire `_pulse.notify('payoff')`
 *        (a cyan flash on the whole `.payoff-svg-stack`) whenever the `payoff`
 *        prop's ARRAY IDENTITY changed. Since the derivatives page refetches
 *        strategy analytics every ~5s even when the leg basket is unchanged
 *        (each refetch reassigns `strategy` to a brand-new object with
 *        identical content), the whole chart flashed on every routine
 *        background refresh, not just on a genuine leg/strategy change.
 *   Fix: the pulse now fires only when `legSignature` (a content-keyed,
 *        not identity-keyed, string built from underlying + leg
 *        symbol:qty pairs + holdings/draft toggles) actually changes.
 *
 *   C2 — For NSE underlyings, the Payoff overlay's `payoffSpot` had no
 *        anchor-contract live tick (NSE has no matching future) and fell
 *        straight to `strategy.spot`, which only updates once per 5s
 *        strategy refetch — the overlay's LTP/CHG% visibly "stepped" once
 *        per 5s instead of ticking live like the rest of the page.
 *   Fix: `payoffSpot` now reuses `liveSpot`'s own Tier-1 live-tick lookup
 *        (`_undLive[selectedUnderlying]?.ltp`) for NSE underlyings before
 *        falling back to the 5s-stepped `strategy.spot`.
 *
 * Test 1 — no-flash-on-routine-refetch:
 *   Observes `.payoff-svg-stack`'s className via MutationObserver across a
 *   window covering 2+ routine 5s refetch cycles WITHOUT any operator leg
 *   change. Asserts the full-chart pulse class (`cp-pulse-a`/`cp-pulse-b`)
 *   fires at most once (the initial data-landing pulse), not once per
 *   refetch cycle.
 *
 * Test 2 — NSE overlay ticks smoothly (best-effort, market-hours gated):
 *   Samples the LTP stat row's text at sub-second intervals over an
 *   ~11s window on an NSE (non-commodity) underlying. If the market is
 *   quiet enough that the LTP never changes during the window, the test
 *   skips (nothing to assert — this is inherently market-data-dependent,
 *   same convention as this suite's other soft-skip specs). When at least
 *   two distinct values ARE observed, asserts they are not all spaced
 *   ~5s apart (i.e., at least one change happens faster than the 5s
 *   strategy-refetch cadence — proof the value tracks live ticks, not
 *   just the periodic refetch).
 *
 * Five quality dimensions:
 *  1. SSOT     — legSignature/payoffSpot are the same values +page.svelte
 *                passes as props; this spec observes the actual DOM, not
 *                a re-implementation of the fix's logic.
 *  2. Perf     — MutationObserver-based, no polling busy-loop for Test 1.
 *  3. Stale    — directly reproduces both original symptoms (flash on
 *                refetch, stepped LTP) and asserts they no longer occur.
 *  4. Reusable — OptionsPayoff.svelte is shared by SimulatorPanel too;
 *                this spec exercises the derivatives page's wiring
 *                (`legSignature`/`refreshing` props), the only consumer
 *                that opts into B1/B2's pinning + signature-gated pulse.
 *  5. UX       — verifies the operator's original ask (chart shouldn't
 *                flash on background refresh; overlay should tick live).
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_payoff_no_flash_on_refetch.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const DERIV_URL = '/admin/derivatives';

/** MCX/CDS commodity/currency roots — used to pick an NSE (non-anchor-future)
 *  underlying for Test 2. Mirrors resolveUnderlying.js's own sets closely
 *  enough for E2E symbol-picking purposes (doesn't need to be exhaustive). */
const NON_NSE_ROOTS = new Set([
  'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
  'GOLD', 'GOLDM', 'GOLDPETAL', 'GOLDGUINEA',
  'SILVER', 'SILVERM', 'SILVERMIC',
  'COPPER', 'ZINC', 'LEAD', 'ALUMINIUM', 'NICKEL',
  'MENTHAOIL', 'COTTON', 'CPO',
  'USDINR', 'EURINR', 'GBPINR', 'JPYINR',
]);

/** Navigate to derivatives page and wait for it to settle. Returns the
 *  underlying dropdown trigger button (#opt-und). */
async function gotoDerivatives(page) {
  await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });
  const trigger = page.locator('#opt-und');
  await trigger.waitFor({ state: 'visible', timeout: 25_000 });
  return trigger;
}

function getPanelForTrigger(triggerLocator) {
  return triggerLocator.locator('xpath=..').locator('.rbq-select-panel');
}

async function getUnderlyingOptions(page, trigger) {
  await trigger.click();
  const panel = getPanelForTrigger(trigger);
  await panel.waitFor({ state: 'visible', timeout: 10_000 });
  const optTexts = await panel.locator('.rbq-select-option-label').allTextContents();
  await page.keyboard.press('Escape');
  return optTexts.map((t) => t.trim()).filter(Boolean);
}

/** Select an underlying by its exact label text via the dropdown. */
async function selectUnderlyingByLabel(page, trigger, label) {
  await trigger.click();
  const panel = getPanelForTrigger(trigger);
  await panel.waitFor({ state: 'visible', timeout: 10_000 });
  await panel.locator('.rbq-select-option-label', { hasText: label }).first().click();
  await page.waitForTimeout(300);
}

test.describe('Payoff chart — no full-chart flash on routine refetch (B1)', () => {
  test('cp-pulse class fires at most once across 2+ routine refetch cycles with an unchanged basket', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    const trigger = await gotoDerivatives(page);

    const stack = page.locator('.payoff-svg-stack').first();
    await stack.waitFor({ state: 'visible', timeout: 15_000 });

    // Let the very first data-landing pulse (if any) settle before we
    // start counting — Test scope is "routine refetch", not initial load.
    await page.waitForTimeout(2000);

    // MutationObserver on the class attribute — catches the cp-pulse-a /
    // cp-pulse-b alternation the component applies via classOf(). Runs for
    // ~12s, comfortably covering 2 full 5s strategy-refetch cycles.
    const pulseCount = await page.evaluate(async () => {
      const el = document.querySelector('.payoff-svg-stack');
      if (!el) return -1;
      let count = 0;
      const seen = new Set();
      const observer = new MutationObserver((mutations) => {
        for (const m of mutations) {
          if (m.attributeName !== 'class') continue;
          const cls = /** @type {HTMLElement} */ (m.target).className;
          if (/\bcp-pulse-[ab]\b/.test(cls)) {
            // Count distinct pulse-start events, not every class-list write
            // (the component toggles a/b, so two writes — add, then later
            // remove — could otherwise double-count a single pulse).
            const key = cls;
            if (!seen.has(key)) { seen.add(key); count++; }
          }
        }
      });
      observer.observe(el, { attributes: true, attributeFilter: ['class'] });
      await new Promise((r) => setTimeout(r, 12_000));
      observer.disconnect();
      return count;
    });

    console.log(`[B1] pulse events observed over 12s with no leg change: ${pulseCount}`);
    if (pulseCount === -1) {
      test.skip(true, '.payoff-svg-stack not found — no strategy rendered for this account');
      return;
    }
    // Routine refetch cadence is ~5s, so 12s covers ~2 cycles. Allow at
    // most 1 pulse in this window (defensive slack for a genuine
    // leg-signature change firing from an unrelated background write,
    // e.g. a fill event) — the pre-fix bug fired on EVERY cycle (~2+).
    expect(
      pulseCount,
      'Expected the full-chart pulse to fire at most once (not once per 5s refetch) when the leg basket is unchanged'
    ).toBeLessThanOrEqual(1);

    // Sanity: the underlying picker itself is still usable after the
    // observation window (page never crashed/froze mid-poll).
    await expect(trigger).toBeVisible();
  });
});

test.describe('Payoff chart — NSE overlay ticks live, not stepped (C2)', () => {
  test('LTP stat-row value updates faster than the 5s refetch cadence for an NSE underlying', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    const trigger = await gotoDerivatives(page);

    const optionLabels = await getUnderlyingOptions(page, trigger);
    const nseLabel = optionLabels.find((l) => {
      const root = l.split(/[\s(]/)[0].toUpperCase();
      return !NON_NSE_ROOTS.has(root);
    });
    if (!nseLabel) {
      test.skip(true, 'No NSE (non-commodity) underlying available to test');
      return;
    }
    await selectUnderlyingByLabel(page, trigger, nseLabel);

    const ltpValue = page.locator('.payoff-stats .ps-row', { hasText: 'LTP' }).locator('.ps-v').first();
    await ltpValue.waitFor({ state: 'visible', timeout: 15_000 });

    // Sample every 400ms for ~11s (covers 2+ refetch cycles at the 5s
    // cadence) and record (timestamp, text) whenever the value changes.
    /** @type {Array<{t:number, v:string}>} */
    const changes = [];
    let last = await ltpValue.textContent();
    const t0 = Date.now();
    while (Date.now() - t0 < 11_000) {
      await page.waitForTimeout(400);
      const cur = await ltpValue.textContent();
      if (cur !== last) {
        changes.push({ t: Date.now() - t0, v: cur ?? '' });
        last = cur;
      }
    }

    console.log(`[C2] LTP value changes observed: ${JSON.stringify(changes)}`);
    if (changes.length < 2) {
      // Market too quiet during this run (or outside session hours) to
      // observe any tick movement — inherently market-data-dependent,
      // same soft-skip convention as this suite's other live-data specs.
      test.skip(true, `Fewer than 2 LTP changes observed in 11s (market quiet or closed) — cannot assert tick cadence`);
      return;
    }
    const gaps = changes.slice(1).map((c, i) => c.t - changes[i].t);
    const anyFasterThanRefetch = gaps.some((g) => g < 4_500);
    expect(
      anyFasterThanRefetch,
      `Expected at least one LTP change faster than the 5s refetch cadence (live tick), got gaps: ${JSON.stringify(gaps)}`
    ).toBe(true);
  });
});
