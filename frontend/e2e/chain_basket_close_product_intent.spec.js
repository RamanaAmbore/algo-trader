/**
 * chain_basket_close_product_intent.spec.js
 *
 * 2026-11 real-money audit fix — Chain-tab basket legs always hardcoded
 * `product: 'NRML'` with zero position context, and never set
 * `leg.intent = 'close'` even when the leg was genuinely closing an
 * existing held position. Kite only nets a close within the SAME
 * product, so a close claimed against the WRONG product silently opened
 * a stray opposite-direction NRML position next to the still-open one
 * instead of closing it. Mirrors the Ticket-tab fix (commit `959a9d747`,
 * see CLAUDE.md's "real-money close-product mismatch" entry) for the
 * Chain-tab basket path — same `_verify_close_intent(product=...)`
 * backend defense, this time fed by a Chain-staged leg instead of a
 * "Close" click on a position row.
 *
 * Resolution lives in two new pieces:
 *   - `resolveLegHeldContext()` (spreadGate.js) — a pure, per-product
 *     resolver, deliberately separate from `isCloseIntentLeg` (which
 *     sums quantity ACROSS products for a different, lower-stakes
 *     purpose — see that function's own docstring). Unit-tested in
 *     spreadGate.test.js (13 cases: no match, stale zero-qty row,
 *     single-product close, exact flatten, flip-past-flat, same-
 *     direction add, cross-product ambiguity, same-product summing,
 *     account scoping, product-less default, symbol/qty row shape,
 *     zero-contracts leg).
 *   - `_resolveLegSubmitFields()` (SymbolPanel.svelte) — resolves fresh
 *     at submit/preview time (never at staging time, since a leg's own
 *     `lots` can change after staging and positions can change between
 *     staging and submit), and ONLY for legs OptionChainTab tagged
 *     `productAuto: true` — a Chain-tab default, never an operator
 *     choice (OrderTicket's own `_basketPayload()` never sets this tag,
 *     so an operator-picked product from the Ticket tab's own basket-add
 *     path is never overridden).
 *
 * Backend: the server-side refusal of a close claimed against the WRONG
 * product is already covered by
 * backend/tests/test_orders_basket_close_intent.py::
 * TestBasketCloseIntentVerification::test_close_claim_wrong_product_rejected_for_nfo
 * (added alongside the Ticket-tab fix) — `BasketLeg.intent`/`.product`
 * and the `_verify_close_intent(product=...)` filter are shared by both
 * the Ticket and Chain submit paths, so that test is NOT duplicated here.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — the live describe block drives the real rendered Chain
 *             tab and the real `submitBasket()` payload builder, via a
 *             genuine `/api/orders/basket` network capture; the
 *             source-scan block is a secondary, deterministic pin of
 *             the wiring (same convention as the Ticket-tab fix's own
 *             "source: all three close-ticket openers..." test and
 *             chain_hedge_draft_mutual_exclusivity.spec.js)
 *  2. Perf  — one mocked /api/positions/ round-trip, no extra polling;
 *             `/api/orders/basket` is route-mocked so no real broker
 *             call is ever made by this spec
 *  3. Stale — pins the `productAuto` tag count (exactly 3 staging
 *             sites) and the exact `_resolveLegSubmitFields` call
 *             sites, so a future refactor that silently drops either
 *             is caught
 *  4. Reuse — `resolveLegHeldContext` is the SAME function both the
 *             submit-groups builder and the margin-preview builder call
 *  5. UX    — a fresh/unmatched leg stays byte-identical to the
 *             pre-fix default (NRML, no intent) — no new UI, no
 *             operator-visible change for the common case
 *
 * Run (local dev server, API proxied to dev.ramboq.com — see
 * vite.config.js's /api proxy target):
 *   cd frontend && npx playwright test \
 *     e2e/chain_basket_close_product_intent.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH    = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');
const SPREAD_GATE_PATH  = path.resolve(DIR, '../src/lib/data/spreadGate.js');

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural guarantees (no browser/market dependency).
// ──────────────────────────────────────────────────────────────────
test.describe('Chain basket close-product/intent — source-scan', () => {
  let chainSrc = '', panelSrc = '', gateSrc = '';

  test.beforeAll(() => {
    chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    gateSrc  = readFileSync(SPREAD_GATE_PATH, 'utf8');
  });

  test('resolveLegHeldContext exists and is a SEPARATE implementation from isCloseIntentLeg', () => {
    const idx = gateSrc.indexOf('export function resolveLegHeldContext(');
    expect(idx).toBeGreaterThan(-1);
    const body = gateSrc.slice(idx, gateSrc.indexOf('\n}', idx));
    // Must not be a thin wrapper that reuses the cross-product-summing
    // helper — that would misclassify a mixed-product book.
    expect(body).not.toMatch(/isCloseIntentLeg\(/);
    expect(body).toMatch(/byProduct\.size > 1/);
  });

  test('every Chain-tab leg-staging site tags the leg productAuto: true (exactly 3 sites)', () => {
    const occurrences = chainSrc.split('productAuto: true,').length - 1;
    // addOptionToBasket's fresh push, addFuturesToBasket's fresh push,
    // and the auto-hedge leg push.
    expect(occurrences).toBe(3);
  });

  test('submitBasket() resolves product/intent via _resolveLegSubmitFields, not a bare leg.product default', () => {
    const idx = panelSrc.indexOf('legs: entries.map(({ leg }) => {');
    expect(idx).toBeGreaterThan(-1);
    const block = panelSrc.slice(idx, idx + 1800);
    expect(block).toMatch(/const _pi = _resolveLegSubmitFields\(leg\);/);
    expect(block).toMatch(/product:\s*_pi\.product,/);
    expect(block).toMatch(/intent:\s*_pi\.intent,/);
  });

  test('the multi-account margin-preview builder resolves the SAME product (preview matches enforcement)', () => {
    const idx = panelSrc.indexOf('_basketMarginTimer = setTimeout');
    expect(idx).toBeGreaterThan(-1);
    const block = panelSrc.slice(idx, idx + 1600);
    expect(block).toMatch(/product:\s*_resolveLegSubmitFields\(leg\)\.product,/);
  });

  test('_resolveLegSubmitFields only resolves productAuto-tagged legs — an operator-chosen product is never overridden', () => {
    const idx = panelSrc.indexOf('function _resolveLegSubmitFields(');
    expect(idx).toBeGreaterThan(-1);
    const block = panelSrc.slice(idx, idx + 600);
    expect(block).toMatch(/if \(!leg\.productAuto\) return fallback;/);
  });
});

// ──────────────────────────────────────────────────────────────────
// Live — real browser, drives the actual Chain tab + real submit path.
// ──────────────────────────────────────────────────────────────────

/**
 * Open /admin/derivatives, pick the first account, and open the Chain
 * tab via a genuine F&O candidate row — the same canonical path
 * basket_pills.spec.js already uses (do not reintroduce the removed
 * page-level "Toggle chain picker" button). This sidesteps the bare-
 * underlying filter on the recent-symbol store entirely (NIFTY/
 * BANKNIFTY/etc. are deliberately NOT persistable as a recent symbol —
 * see accounts.js's `_BARE_UNDERLYINGS` — so a `/orders?symbol=NIFTY`
 * seed would be silently stripped).
 */
async function pickFirstAccount(page) {
  const acctTrigger = page.locator('button#opt-acct').first();
  await expect(acctTrigger).toBeVisible({ timeout: 15_000 });
  await expect(acctTrigger).not.toContainText(/No accounts loaded/i, { timeout: 15_000 });
  await acctTrigger.click();
  const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await expect(firstAcct).toBeVisible({ timeout: 5000 });
  await firstAcct.click();
  await acctTrigger.click({ position: { x: 8, y: 8 } });
}

async function openChainFromRow(page) {
  const modal = page.locator('.oes-modal:visible').first();
  const rows = page.locator('.cand-row:not(.cand-row-total):not(.cand-closed)');
  await expect(rows.first()).toBeVisible({ timeout: 15_000 });
  const n = Math.min(await rows.count(), 10);
  for (let i = 0; i < n; i++) {
    await rows.nth(i).click();
    await expect(modal).toBeVisible({ timeout: 10_000 });
    const chainTab = modal.getByRole('tab', { name: 'Chain', exact: true });
    if ((await chainTab.count()) && (await chainTab.isEnabled())) {
      await chainTab.click();
      return;
    }
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden({ timeout: 5000 });
  }
  throw new Error('openChainFromRow: no candidate row exposes an enabled Chain tab');
}

async function goDerivativesChain(page) {
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
  await pickFirstAccount(page);
  await openChainFromRow(page);
  await page.waitForTimeout(1500); // let chain quotes settle (basket_pills.spec.js's own margin)
}

/** Ensure the Chain tab's strike grid is rendered. */
async function ensureChainGridVisible(page) {
  const chainGrid = page.locator('.chain-td-ce').first();
  await expect(chainGrid).toBeVisible({ timeout: 15_000 });
}

/**
 * Click the ATM row's CE BUY ("+") button — NOT the first DOM-order
 * strike row. A held position's own strike can render as a pinned row
 * far from the money (e.g. deep ITM), and the pre-submit spread gate
 * (`createSpreadGate`, spreadGate.js) can poll indefinitely on a wide,
 * illiquid deep-ITM spread. The ATM row is the tightest, most liquid
 * strike on the ladder and resolves the spread gate immediately.
 */
async function addAtmCeBuyLeg(page) {
  const ceAddBtn = page.locator('.chain-row-atm .chain-td-ce .chain-btn-buy').first();
  await expect(ceAddBtn).toBeVisible({ timeout: 15_000 });
  await ceAddBtn.click();
  await expect(page.locator('.oes-basket-pill').first()).toBeVisible({ timeout: 5_000 });
}

/** Click the common-footer Submit button that fires submitBasket(). */
async function clickSubmit(page) {
  const submitBtn = page.locator('.oes-common-submit').first();
  await expect(submitBtn).toBeVisible({ timeout: 5_000 });
  await submitBtn.click();
}

test.describe('Live — Chain basket close-product/intent', () => {
  test.setTimeout(90_000);

  test('a fresh (unmatched) leg submits with the old NRML/no-intent default; the SAME matched MIS position resolves product=MIS, intent=close', async ({ page }) => {
    await loginAsAdmin(page);

    // The /admin/derivatives candidate list (the row openChainFromRow
    // clicks) is itself built from the account's REAL positions — the
    // positions mock below must NOT be installed until AFTER that row
    // has been clicked and the Chain tab is open, or there is nothing
    // to click at all.
    await goDerivativesChain(page);
    await ensureChainGridVisible(page);

    // Widen the pre-submit spread gate's own threshold so a genuinely
    // wide live GOLD spread (unrelated to this fix) never blocks
    // submission mid-test. This spec tests product/intent resolution,
    // not spread-gate behaviour.
    const spreadInput = page.locator('.oct-spread-field > input').first();
    if (await spreadInput.count()) {
      await spreadInput.fill('50');
      await spreadInput.blur();
    }

    const hookReady = await page.evaluate(
      () => typeof (/** @type {any} */ (window).__stores?.positionsStore) === 'object',
    );
    if (!hookReady) {
      test.skip(true, 'window.__stores.positionsStore test hook not present in this build');
      return;
    }

    // Always zero positions until the test explicitly flips this —
    // guarantees the first submit below is a genuine "no held position"
    // baseline, regardless of what the real signed-in account actually
    // holds on dev.ramboq.com.
    /** @type {any[]} */
    let mockedPositionRows = [];
    await page.route('**/api/positions/**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ rows: mockedPositionRows, refreshed_at: new Date().toISOString(), as_of: null, stale_accounts: [] }),
      });
    });

    /** @type {any[]} */
    const capturedBaskets = [];
    await page.route('**/api/orders/basket', async (route) => {
      const body = route.request().postDataJSON();
      capturedBaskets.push(body);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          groups: (body?.groups || []).map((g) => ({
            account: g.account,
            basket_id: 'test-basket',
            results: (g.legs || []).map((_, i) => ({
              leg_index: i, order_id: `TEST-${i}`, status: 'OPEN', error: null,
            })),
          })),
        }),
      });
    });

    // Force the mock (empty positions) into portfolioStore before
    // staging the first leg — the real fetch that ran during
    // goDerivativesChain() above may have already populated it with
    // the account's genuine positions.
    await page.evaluate(() => (/** @type {any} */ (window).__stores.positionsStore.load({ force: true })));
    await page.waitForTimeout(500);

    // ── Pass 1: fresh leg, no held position anywhere ──────────────
    await addAtmCeBuyLeg(page);
    await clickSubmit(page);
    await expect.poll(() => capturedBaskets.length, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);

    const firstBody = capturedBaskets[0];
    const firstLeg = firstBody?.groups?.[0]?.legs?.[0];
    expect(firstLeg, 'expected a captured basket leg').toBeTruthy();
    console.log(`[chain-close-intent] pass 1 leg: ${JSON.stringify(firstLeg)}`);
    expect(firstLeg.product).toBe('NRML');
    expect(firstLeg.intent == null).toBe(true);

    // `account` lives at the GROUP level, not on the BasketLeg itself
    // (BasketLeg carries no account field — see schemas.py). `quantity`
    // on the OUTGOING leg is LOTS for an MCX symbol (lotSize > 1, the
    // v2 API convention), not contracts — resolveLegHeldContext expects
    // CONTRACTS (`leg.lots * leg.lotSize`, read from the staged leg
    // object, not this payload). Rather than reverse-deriving the exact
    // lot size here, hold a deliberately oversized opposite-sign
    // quantity — resolveLegHeldContext's close rule is "reduces or
    // flattens" (`contracts <= |held|`), so any realistic staged
    // contracts count still resolves as a close against it.
    const { tradingsymbol, exchange, transaction_type } = firstLeg;
    const account = firstBody?.groups?.[0]?.account;
    expect(account, 'expected a captured basket group account').toBeTruthy();

    // ── Pass 2: mock a held position that this EXACT leg (same strike,
    //     re-staged from the same row) would genuinely close ──────
    const oppositeSign = transaction_type === 'BUY' ? -1 : 1;
    mockedPositionRows = [{
      account, tradingsymbol, exchange,
      product: 'MIS',
      quantity: oppositeSign * 1_000_000,
      average_price: 100, pnl: 0, last_price: 100,
    }];
    await page.evaluate(() => (/** @type {any} */ (window).__stores.positionsStore.load({ force: true })));
    // Let the reactive chain (positionsStore -> portfolioStore) settle.
    await page.waitForTimeout(500);

    await addAtmCeBuyLeg(page);
    await clickSubmit(page);
    await expect.poll(() => capturedBaskets.length, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);

    const secondBody = capturedBaskets[1];
    const secondLeg = secondBody?.groups?.[0]?.legs?.[0];
    expect(secondLeg, 'expected a second captured basket leg').toBeTruthy();
    console.log(`[chain-close-intent] pass 2 leg: ${JSON.stringify(secondLeg)}`);
    expect(secondLeg.product).toBe('MIS');
    expect(secondLeg.intent).toBe('close');
  });
});
