/**
 * draft_persistence.spec.js
 *
 * Verifies the 2026-10 server-persisted payoff-draft feature
 * (payoffDrafts.svelte.js backed by /api/orders/drafts, commit 61a9dd39's
 * backend half + this session's frontend half).
 *
 * All /api/orders/drafts* and /api/orders/ticket calls are mocked via
 * page.route — this repo's `/api` dev-proxy target is dev.ramboq.com
 * (see frontend/vite.config.js), which does not yet have the new backend
 * endpoints (they're only committed to `workshop`, not deployed). Every
 * other call (auth, accounts, instruments, orders/, execution/mode) hits
 * the real dev.ramboq.com backend, same as every other spec in this
 * directory that uses loginAsAdmin's cached-token fast path.
 *
 * Covered:
 *   1. A draft fetched via GET /api/orders/drafts renders as a "D" row
 *      in ChaseCard, including its assigned account — and survives a
 *      page reload (the actual bug this feature fixes: drafts used to
 *      be pure in-memory $state, wiped on refresh).
 *
 * NOT covered here (see test.fixme + comments below for why):
 *   - Bug A (delete-before-confirmation) and Bug B (close-while-editing
 *     discards the draft) both require UI affordances (`.ot-close`, the
 *     DRAFT checkbox) that live inside `{#if standalone}` /
 *     `{#if showLimit && !modeChaseHidden}` blocks in OrderTicket.svelte.
 *     SymbolPanel — the ONLY <OrderTicket> mount site anywhere in this
 *     app — hardcodes `standalone={false}` and `modeChaseHidden={true}`
 *     unconditionally, so neither block ever renders through the
 *     shipped UI tree. This is a pre-existing, separately-scoped defect
 *     (not introduced by this fix — see git blame on both props).
 *     Both fixes are covered instead by source-grep regression guards:
 *     see orderTicketDraftLifecycle.test.js (Vitest).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

/** Builds one NFO draft row shaped like the /api/orders/drafts payload. */
function makeDraft(id) {
  return {
    id, account: 'ZG0790', symbol: 'NIFTY26OCTFUT', exchange: 'NFO',
    transaction_type: 'BUY', quantity: 50, initial_price: 24500, current_limit: null,
    fill_price: null, attempts: 0, status: 'OPEN', engine: 'manual', mode: 'draft',
    detail: '[DRAFT] BUY 50 NIFTY26OCTFUT', created_at: new Date().toISOString(),
    target_pct: null, target_abs: null, parent_order_id: null, basket_tag: null,
    template_id: null, attached_gtts_json: null, filled_quantity: null,
    child_order_ids: [], interval_seconds: null, last_attempt_at: null,
    next_attempt_at: null, broker_order_id: null, source: null, agent_id: null,
  };
}

/** Registers mocked handlers for every /api/orders/drafts* request,
 *  backed by the given mutable array (so a test can inspect what was
 *  requested, and GET reflects whatever the array currently holds). */
async function mockDraftsApi(page, draftRows) {
  await page.route('**/api/orders/drafts', async (route) => {
    const req = route.request();
    if (req.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(draftRows) });
      return;
    }
    await route.continue();
  });
  // Deterministic engine mode — avoids the dev-default IDLE boot state
  // racing the test (see stores.js:_bootMode — localhost boots 'idle'
  // until the first /api/admin/execution/mode poll resolves).
  await page.route('**/api/admin/execution/mode', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['paper', 'live'] }),
    });
  });
}

test.describe('Payoff draft persistence (server-backed, 2026-10)', () => {
  test('draft fetched via GET survives a page reload and shows its account in ChaseCard', async ({ page }) => {
    const draftRows = [
      {
        id: 9001, account: 'ZG0790', symbol: 'NIFTY26OCTFUT', exchange: 'NFO',
        transaction_type: 'BUY', quantity: 50, initial_price: 24500, current_limit: null,
        fill_price: null, attempts: 0, status: 'OPEN', engine: 'manual', mode: 'draft',
        detail: '[DRAFT] BUY 50 NIFTY26OCTFUT', created_at: new Date().toISOString(),
        target_pct: null, target_abs: null, parent_order_id: null, basket_tag: null,
        template_id: null, attached_gtts_json: null, filled_quantity: null,
        child_order_ids: [], interval_seconds: null, last_attempt_at: null,
        next_attempt_at: null, broker_order_id: null, source: null, agent_id: null,
      },
    ];

    let getCount = 0;
    await page.route('**/api/orders/drafts', async (route) => {
      if (route.request().method() === 'GET') {
        getCount++;
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(draftRows) });
        return;
      }
      await route.continue();
    });
    await page.route('**/api/admin/execution/mode', async (route) => {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['paper', 'live'] }),
      });
    });

    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const draftRow = page.locator('.cc-row-draft').first();
    await expect(draftRow).toBeVisible({ timeout: TIMEOUT });
    await expect(draftRow).toContainText('NIFTY');
    // Account threading (brief requirement #3) — the draft's assigned
    // account renders next to the "D" chip instead of the generic
    // "no account assigned" fallback.
    await expect(draftRow.locator('.cc-draft-acct-text')).toHaveText('ZG0790');
    expect(getCount).toBeGreaterThanOrEqual(1);

    await page.reload({ waitUntil: 'domcontentloaded' });

    const draftRowAfterReload = page.locator('.cc-row-draft').first();
    await expect(draftRowAfterReload).toBeVisible({ timeout: TIMEOUT });
    await expect(draftRowAfterReload).toContainText('NIFTY');
    await expect(draftRowAfterReload.locator('.cc-draft-acct-text')).toHaveText('ZG0790');
    // A second GET must have fired on reload — this is the actual bug
    // being fixed: payoffDrafts used to be pure in-memory $state with no
    // fetch at all, so a reload always produced an empty Map.
    expect(getCount).toBeGreaterThanOrEqual(2);
  });

  test('a genuinely empty draft list renders no "D" rows (confirmed-empty, not a load failure)', async ({ page }) => {
    await mockDraftsApi(page, []);
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    // Give the async load() a moment to resolve before asserting absence.
    await page.waitForTimeout(1500);
    await expect(page.locator('.cc-row-draft')).toHaveCount(0);
  });

  // ── DRAFT reachability + Bug A / Bug B ─────────────────────────────────
  //
  // DRAFT now has its own `hideDraftToggle` gate (default false), so the
  // checkbox renders at SymbolPanel's mount (which only sets
  // modeChaseHidden=true). Bug A is driven end-to-end below.
  //
  // Bug B is still unreachable: `.ot-close` only renders when
  // standalone=true, and SymbolPanel hardcodes standalone=false.
  // Covered by orderTicketDraftLifecycle.test.js (Vitest source-grep).
  test('DRAFT checkbox renders at SymbolPanel mount; CHASE and mode row stay hidden', async ({ page }) => {
    await mockDraftsApi(page, [makeDraft(9010)]);
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    await page.locator('.cc-row-draft').first().click();

    const ticket = page.getByRole('dialog', { name: 'NIFTY26OCTFUT' });
    const draftToggle = ticket.locator('.ot-draft-toggle').first();
    await expect(draftToggle).toBeVisible({ timeout: TIMEOUT });
    await expect(draftToggle).toContainText('DRAFT');
    // DRAFT is clickable here even though chase is forced ON for LIMIT
    // tickets: draft and chase coexist (a draft never reaches the broker).
    await expect(draftToggle.locator('input[type="checkbox"]')).toBeEnabled();

    // Regression guard: modeChaseHidden (SymbolPanel) must still hide the
    // in-ticket CHASE toggle and the mode-hint row.
    await expect(ticket.locator('.ot-chase-toggle')).toHaveCount(0);
    await expect(ticket.locator('.ot-mode-row')).toHaveCount(0);
  });

  test('checking DRAFT with CHASE forced on leaves CHASE on (allow both)', async ({ page }) => {
    await mockDraftsApi(page, [makeDraft(9015)]);
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    await page.locator('.cc-row-draft').first().click();

    const ticket = page.getByRole('dialog', { name: 'NIFTY26OCTFUT' });
    const draftToggle = ticket.locator('.ot-draft-toggle').first();
    const draftBox = draftToggle.locator('input[type="checkbox"]');
    // SymbolPanel's shell-level CHASE label (forced on for LIMIT).
    const chaseLabel = ticket.locator('.oes-common-chase-label');
    await expect(draftToggle).toBeVisible({ timeout: TIMEOUT });
    await expect(chaseLabel).toHaveClass(/\bon\b/);

    // Uncheck then re-check DRAFT — the re-check is the case that used to
    // force chase off via the mutual-exclusion handler.
    await draftToggle.click();
    await expect(draftBox).not.toBeChecked();
    await draftToggle.click();
    await expect(draftBox).toBeChecked();
    await expect(chaseLabel).toHaveClass(/\bon\b/);
  });

  test('open an existing draft, submit for real (mocked reject) → draft NOT removed (Bug A)', async ({ page }) => {
    const drafts = [makeDraft(9020)];
    await mockDraftsApi(page, drafts);
    await page.route('**/api/orders/ticket', async (route) => {
      await route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'rejected' }) });
    });
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    await page.locator('.cc-row-draft').first().click();
    // Flip DRAFT off while initialDraftId stays set → submit goes to broker.
    await page.locator('.ot-draft-toggle').click();
    await page.getByRole('dialog', { name: 'NIFTY26OCTFUT' }).locator('.oes-common-submit').first().click();

    // Rejected placement must leave the draft row in place.
    await expect(page.locator('.cc-row-draft')).toHaveCount(1, { timeout: TIMEOUT });
  });

  test('open an existing draft, submit for real (mocked success) → draft IS removed', async ({ page }) => {
    const drafts = [makeDraft(9030)];
    await mockDraftsApi(page, drafts);
    // Mirror the server: DELETE drops the row from the mutable list so the
    // next GET reflects it.
    await page.route('**/api/orders/drafts/*', async (route) => {
      if (route.request().method() === 'DELETE') {
        const id = Number(route.request().url().split('/').pop());
        const idx = drafts.findIndex((d) => d.id === id);
        if (idx >= 0) drafts.splice(idx, 1);
        await route.fulfill({ status: 204, body: '' });
        return;
      }
      await route.continue();
    });
    await page.route('**/api/orders/ticket', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', order_id: 1 }) });
    });
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    await page.locator('.cc-row-draft').first().click();
    await page.locator('.ot-draft-toggle').click();
    await page.getByRole('dialog', { name: 'NIFTY26OCTFUT' }).locator('.oes-common-submit').first().click();

    await expect(page.locator('.cc-row-draft')).toHaveCount(0, { timeout: TIMEOUT });
  });
});
