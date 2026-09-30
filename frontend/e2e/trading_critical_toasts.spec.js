/**
 * trading_critical_toasts.spec.js
 *
 * Trading-critical errors now ALSO surface a short-lived in-app toast
 * (operator audit, 2026-09-30) — on top of, not instead of, the existing
 * inline error banners / logs / backend alerts, which are unchanged.
 * Scope is deliberately narrow per the operator's own framing ("trading-
 * critical only", not a blanket app-wide error-popup policy):
 *
 *  a) Order placement failure — a ticket submit that fails (network
 *     error, 4xx/5xx, or a preflight block) now also fires
 *     `toast.error(...)` from OrderTicket.svelte's submit() catch
 *     branch, in addition to the existing `.ot-err` inline banner.
 *     Covers every SymbolPanel/OrderTicket mount (this spec drives the
 *     dashboard's `.pha-order` modal entry point — same code path as
 *     /orders, /admin/derivatives, /console, /charts).
 *
 *  b) Chase-failure alerts — SKIPPED. No backend event for
 *     `_ch_exhaust_max_attempts` / `_ch_cancel_and_capture` reaches the
 *     frontend today (chase.py never calls the shared `_ws_broadcast`
 *     helper; only orders_postback.py does, for fills). Wiring a toast
 *     off a signal that doesn't exist would be a silent no-op, so this
 *     is intentionally not implemented here — see the task report.
 *
 *  c) Template-attach stall — wired into the POLL LOOP (OrderBook.svelte
 *     / LogPanel.svelte `_loadOrders`, evaluated on the merged rows
 *     BEFORE status-chip filtering — NOT tied to OrderCard's own mount
 *     lifecycle, which only ever renders rows the currently-selected
 *     status chip matches). Once a LIVE-mode order's "template
 *     selected, order FILLED, nothing attached" state has PERSISTED
 *     for `ATTACH_STALL_THRESHOLD_MS` (15s — rides out the real async
 *     attach-latency race, timed from first observation, shared across
 *     any number of concurrent pollers via sessionStorage) a toast
 *     fires — deduped per order_id for the session, and never at all
 *     for non-live modes (paper/sim/replay/shadow never attempt a real
 *     attach, so that state is their permanent normal resting state).
 *     Debounce/gating logic itself is covered exhaustively by the
 *     Vitest unit suite (src/lib/__tests__/data/templateAttachToast.test.js);
 *     this spec verifies the toast actually renders end-to-end off a
 *     live poll cycle.
 *
 * Five quality dimensions:
 *  1. SSOT   — drives the real submit() catch-path / poll-loop paths,
 *              not a source-grep proxy for either surface.
 *  2. Perf   — no added polling; toast auto-dismiss timing unchanged
 *              from toastStore.svelte.js's own defaults.
 *  3. Stale  — (c) explicitly asserts NO toast before the threshold and
 *              exactly ONE after, across multiple poll ticks of the same
 *              failed-attach order — guards both the false-positive and
 *              the toast-per-poll spam regressions.
 *  4. Reuse  — both wirings call the existing `toast` API from
 *              toastStore.svelte.js; no parallel toast system invented.
 *  5. UX     — (a) asserts the existing inline `.ot-err` banner is
 *              STILL present (the toast is additive, not a replacement).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('(a) Order placement failure — toast + inline banner both fire', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('a rejected ticket POST shows an error toast AND keeps the inline banner', async ({ page }) => {
    // Boot mode on localhost defaults to 'idle' until the layout's first
    // /admin/execution/mode poll "upgrades" it — mock that poll so the
    // ticket's pre-submit idle-gate doesn't short-circuit before this
    // spec's actual target (the submit()-catch → toast wiring).
    await page.route('**/api/admin/execution/mode', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ mode: 'paper', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
      })
    );
    // Instruments + quote are mocked so the symbol picker and OrderDepth
    // don't depend on a live broker/instruments cache — this spec only
    // cares about the submit()-catch → toast wiring, not real market data.
    await page.route('**/api/instruments/', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          cycle_date: new Date().toISOString().slice(0, 10),
          count: 1,
          items: [{ s: 'RELIANCE', e: 'NSE', t: 'EQ', u: 'RELIANCE', x: null, k: null, ls: 1, ts: 0.05 }],
        }),
      })
    );
    await page.route('**/api/quote/**', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          last_price: 2950.5,
          ohlc: { open: 2900, high: 2960, low: 2890, close: 2900 },
          depth: {
            buy:  [{ price: 2950, quantity: 10, orders: 1 }],
            sell: [{ price: 2951, quantity: 10, orders: 1 }],
          },
        }),
      })
    );
    // Preflight (margin preview, fired on field change) must resolve —
    // otherwise it hangs against the (absent in this sandbox) real
    // backend and the submit button's debounced preview never settles.
    await page.route('**/api/orders/preflight', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true, blocked: [],
          diagnostics: { basket_margin_used: 1000, available_margin: 500_000, margin_shortfall: 0 },
        }),
      })
    );
    await page.route('**/api/orders/ticket', (route) =>
      route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Margin insufficient for this order' }),
      })
    );

    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1_500);

    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      test.skip(true, 'no .pha-order entry point in this environment');
      return;
    }
    await orderBtn.click({ force: true });

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 8_000 });

    const symInput = page.locator('.ssi-input').first();
    if (await symInput.count() === 0) {
      test.skip(true, 'no symbol input in this ticket layout');
      return;
    }
    await symInput.fill('RELIANCE');
    await page.waitForTimeout(800);
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(500);

    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
      await page.waitForTimeout(200);
    }

    // Default order type is LIMIT — fill a price so the pre-submit
    // client-side validation (a separate, pre-existing toast in
    // SymbolPanel.svelte for "Limit price required" etc.) doesn't
    // short-circuit before this spec's actual target: the submit()
    // catch-branch toast for a REAL backend rejection.
    const priceInput = page.locator('#ot-price').first();
    if (await priceInput.count() > 0) {
      await priceInput.fill('2950');
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();

    // Existing inline error banner — unchanged behaviour, must still
    // appear (the toast is additive).
    const inlineErr = page.locator('.ot-err').first();
    await expect(inlineErr).toBeVisible({ timeout: 10_000 });
    await expect(inlineErr).toContainText(/margin/i);

    // NEW: a short-lived error toast fires alongside it.
    const errorToast = page.locator('.rbq-toast[role="alert"]').first();
    await expect(errorToast).toBeVisible({ timeout: 5_000 });
    await expect(errorToast).toContainText(/order failed/i);
  });
});

test.describe('(c) Template-attach stall — one toast per order_id, not one per poll', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  // AlgoOrderInfo shape (backend/api/routes/orders_helpers.py) — the
  // ONLY row shape that ever carries `template_id`/`attached_gtts_json`
  // (the plain broker `/orders/` feed never does — see
  // templateAttachToast.js's header comment). `mode: 'live'` is
  // required: `_fire_template_attach_on_fill` is a no-op for every
  // other mode, so a non-live fixture would never satisfy
  // `isAttachFailedState`.
  function stalledAlgoOrder(id) {
    return {
      id,
      symbol: 'NIFTY26JUN25000CE',
      exchange: 'NFO',
      transaction_type: 'BUY',
      quantity: 50,
      initial_price: 210.25,
      status: 'FILLED',
      mode: 'live',
      created_at: new Date().toISOString(),
      template_id: 3,
      attached_gtts_json: null,
      account: 'ZG0790',
    };
  }

  test('toast fires once the stall has persisted, not on every poll, never before the threshold', async ({ page }) => {
    // Detection is wired into the POLL LOOP (OrderBook.svelte /
    // LogPanel.svelte `_loadOrders`), evaluated on the merged rows
    // BEFORE status-chip filtering — so no chip needs to be selected
    // for the toast to fire, sidestepping OrderBook's pre-existing
    // "Filled" chip gap (its predicate checks broker-vocabulary
    // 'COMPLETE', which AlgoOrder rows never use — see this fix's task
    // report). Driving /orders here for a concrete UI surface; the
    // same wiring also covers the dashboard card, Activity modal, and
    // /activity page via LogPanel.svelte.
    let pollCount = 0;
    await page.route('**/api/orders/', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [] }) })
    );
    await page.route('**/api/orders/algo/recent**', (route) => {
      pollCount++;
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([stalledAlgoOrder(555)]) });
    });
    await page.route('**/api/orders/events/recent**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
    );

    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    // Wait for the first poll to land, then assert NO toast yet — the
    // failed-attach state must persist for ATTACH_STALL_THRESHOLD_MS
    // (15s) before it's treated as a genuine stall rather than the
    // normal async attach-latency window.
    await expect.poll(() => pollCount, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);
    const attachToast = page.locator('.rbq-toast').filter({ hasText: /template did not attach/i });
    await expect(attachToast).toHaveCount(0);

    // Wait past the threshold — the toast should now have fired exactly once.
    await expect(attachToast).toHaveCount(1, { timeout: 20_000 });
    await expect(attachToast.first()).toContainText('#555');

    // A further poll cycle must not add a second toast for the same order.
    await page.waitForTimeout(4_000);
    await expect(attachToast).toHaveCount(1);
  });
});
