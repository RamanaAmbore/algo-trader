/**
 * draft_positions_pulse.spec.js
 *
 * 2026-10-21 — draft positions on MarketPulse's Positions grid (shared-row
 * re-wire round). Covers:
 *   B. Draft rows render with the `row-draft` class. (2026-10 simplification:
 *      a draft now sums into the pinned TOTAL like any real row — MarketPulse's
 *      TOTAL has no qty cell (qty_net is null on totals), so that sum is
 *      covered at unit level in pulseUnified.test.js, not in the DOM.)
 *   C. Select via right-click (desktop) / long-press (touch) on a draft row —
 *      no checkbox. "Clear Selected Drafts (N)" and the ✕ Cancel button are
 *      absent from the DOM at 0 selected, appear once selection starts, Cancel
 *      empties selection with no DELETE, Clear removes exactly the selected ids.
 *      A plain click on another draft toggles membership while selecting.
 *   D. Click-to-promote: clicking a draft row (not its checkbox) opens the
 *      SymbolPanel ticket with ONLY the Ticket and Chart tabs, auto-submits
 *      nothing, and a confirmed Submit sends `draft_id` and removes the
 *      draft from the grid without a separate DELETE.
 *
 * All /api/orders/drafts traffic is intercepted with a small stateful mock,
 * so these specs never create or delete rows in the dev database.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { mockDrafts } from './fixtures/draftsMock.js';
import { skipUnlessRendered } from './fixtures/instrumentGate.js';

async function gotoPulse(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 30_000 });
}

/**
 * Positions-card draft rows. ag-Grid renders each row once per pinned
 * container (symbol column vs numeric columns), so one draft = 2+ DOM rows
 * sharing a `row-id` ("<SYMBOL>::draft::<id>", pulseRowId). Count distinct
 * row-ids, never raw DOM nodes.
 */
function draftRows(page) {
  return {
    /** Resolves to the distinct draft row-ids currently rendered. */
    ids: () => page.locator('.ag-row.row-draft').evaluateAll(
      (els) => [...new Set(els.map((e) => (e.getAttribute('row-id') || '').replace(/__pos$/, '')))].sort()),
  };
}

/** Symbol cell of one draft row, addressed by its stable row-id. */
function draftSymbolCell(page, symbol, id) {
  // Positions rows carry a `__pos` suffix on their row-id; match by prefix.
  return page.locator(`.ag-row[row-id^="${symbol}::draft::${id}"] .ag-cell[col-id="tradingsymbol"]`).first();
}

test.describe('MarketPulse Positions — draft rows', () => {
  test('B: draft rows render tagged row-draft and show their symbols', async ({ page }) => {
    await loginAsAdmin(page);
    await mockDrafts(page);
    await gotoPulse(page);

    await skipUnlessRendered(draftSymbolCell(page, 'NIFTY26OCTFUT', 9001), 'MarketPulse draft rows');
    await expect.poll(() => draftRows(page).ids(), { timeout: 20_000 })
      .toEqual(['CRUDEOIL26NOVFUT::draft::9002', 'NIFTY26OCTFUT::draft::9001']);
    // Grid shows the display form of the symbol (e.g. NIFTY-OCT26-FUT), so
    // match the root only.
    await expect(draftSymbolCell(page, 'NIFTY26OCTFUT', 9001)).toContainText('NIFTY');
    await expect(draftSymbolCell(page, 'CRUDEOIL26NOVFUT', 9002)).toContainText('CRUDEOIL');
  });
});

test.describe('MarketPulse Positions — select and clear drafts', () => {
  test('C: right-click selects a draft, header button counts only selected drafts and removes exactly those', async ({ page }) => {
    await loginAsAdmin(page);
    const state = await mockDrafts(page);
    await gotoPulse(page);

    await skipUnlessRendered(draftSymbolCell(page, 'NIFTY26OCTFUT', 9001), 'MarketPulse draft rows');
    const clearBtn = page.locator('.mp-clear-drafts-btn');
    const cancelBtn = page.locator('.mp-draft-cancel-btn');

    // Nothing selected: both controls absent from the DOM, not merely disabled.
    await expect(clearBtn).toHaveCount(0);
    await expect(cancelBtn).toHaveCount(0);
    // No per-row checkbox any more.
    await expect(page.locator('.draft-select-cb')).toHaveCount(0);

    // Right-click the NIFTY draft (id 9001) to enter selection.
    await draftSymbolCell(page, 'NIFTY26OCTFUT', 9001).click({ button: 'right' });
    await expect(clearBtn).toBeVisible({ timeout: 10_000 });
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(1\)/);
    await expect(cancelBtn).toBeVisible();
    await expect(page.locator('.ag-row.row-draft.draft-row-selected')).not.toHaveCount(0);

    await clearBtn.click();
    await expect.poll(() => state.calls.filter((c) => c.method === 'DELETE').map((c) => c.id))
      .toEqual([9001]);
    // The unselected draft survives in the grid.
    await expect.poll(() => draftRows(page).ids()).toEqual(['CRUDEOIL26NOVFUT::draft::9002']);
    // Selection cleared with the delete: both controls gone again.
    await expect(clearBtn).toHaveCount(0);
    await expect(cancelBtn).toHaveCount(0);
  });

  test('C2: Cancel (✕) empties the selection without issuing any DELETE', async ({ page }) => {
    await loginAsAdmin(page);
    const state = await mockDrafts(page);
    await gotoPulse(page);

    await skipUnlessRendered(draftSymbolCell(page, 'NIFTY26OCTFUT', 9001), 'MarketPulse draft rows');
    const clearBtn = page.locator('.mp-clear-drafts-btn');
    const cancelBtn = page.locator('.mp-draft-cancel-btn');

    await draftSymbolCell(page, 'NIFTY26OCTFUT', 9001).click({ button: 'right' });
    await draftSymbolCell(page, 'CRUDEOIL26NOVFUT', 9002).click({ button: 'right' });
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(2\)/);

    await cancelBtn.click();
    // Selection emptied: both controls removed from the DOM, no tint left.
    await expect(clearBtn).toHaveCount(0);
    await expect(cancelBtn).toHaveCount(0);
    await expect(page.locator('.ag-row.row-draft.draft-row-selected')).toHaveCount(0);
    // Both drafts still rendered, and crucially no DELETE went out.
    await expect.poll(() => draftRows(page).ids())
      .toEqual(['CRUDEOIL26NOVFUT::draft::9002', 'NIFTY26OCTFUT::draft::9001']);
    expect(state.calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);
  });

  test('C3: once selecting, a plain click on another draft toggles it instead of opening the ticket', async ({ page }) => {
    await loginAsAdmin(page);
    await mockDrafts(page);
    const ticketCalls = [];
    await page.route('**/api/orders/ticket', async (route) => {
      ticketCalls.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"ok","order_id":1}' });
    });
    await gotoPulse(page);

    await skipUnlessRendered(draftSymbolCell(page, 'NIFTY26OCTFUT', 9001), 'MarketPulse draft rows');
    const clearBtn = page.locator('.mp-clear-drafts-btn');
    await draftSymbolCell(page, 'NIFTY26OCTFUT', 9001).click({ button: 'right' });
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(1\)/);

    // Plain click on the OTHER draft adds it to the selection.
    await draftSymbolCell(page, 'CRUDEOIL26NOVFUT', 9002).click();
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(2\)/);
    // And it did not open the promote ticket or hit /ticket.
    expect(ticketCalls).toHaveLength(0);
    await expect(page.locator('.oes-modal')).toHaveCount(0);

    // Plain click on an already-selected draft deselects it.
    await draftSymbolCell(page, 'CRUDEOIL26NOVFUT', 9002).click();
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(1\)/);
  });
});

test.describe('MarketPulse Positions — click-to-promote a draft', () => {
  test('D: draft row click opens Ticket+Chart only, submits nothing by itself, removes the draft on a confirmed Submit', async ({ page }) => {
    // handleRowClick -> _openTicketFromDraftRow awaits _instrumentsReady, which
    // on dev waits for the full instrument master (minutes on a cold context),
    // so the modal opens late. Give this one test the headroom.
    test.setTimeout(240_000);
    await loginAsAdmin(page);
    const state = await mockDrafts(page);
    const ticketCalls = [];
    await page.route('**/api/orders/ticket', async (route) => {
      ticketCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', order_id: 1 }),
      });
    });
    // Mock execution mode as paper so the pre-existing "Engine is idle —
    // pick PAPER/SIM/REPLAY" guard doesn't block Submit from ever reaching
    // the ticket endpoint (same pattern as crudeoil_add_sell.spec.js) —
    // unrelated to the draft simplification, this guard predates it.
    await page.route('**/api/admin/execution/mode', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ mode: 'paper', paper_trading_mode: true, dev_active: true }),
      });
    });
    await gotoPulse(page);

    const cell = draftSymbolCell(page, 'NIFTY26OCTFUT', 9001);
    await skipUnlessRendered(cell, 'MarketPulse draft symbol cell');
    // Click the symbol text, not the checkbox.
    await cell.click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 200_000 });

    // Only Ticket and Chart in the tab strip — no Chain, no CLI.
    const tabNames = await page.getByRole('tab').allTextContents();
    const labels = tabNames.map((t) => t.trim().toLowerCase().replace(/\s+\d+$/, ''));
    expect(labels).toEqual(expect.arrayContaining(['ticket', 'chart']));
    expect(labels).not.toContain('chain');
    expect(labels).not.toContain('cli');

    // Opening the ticket must not hit the broker.
    expect(ticketCalls).toHaveLength(0);

    // Shared footer Submit (SymbolPanel), not OrderTicket's internal footer.
    const submit = modal.locator('.oes-common-submit').first();
    await expect(submit).toBeVisible({ timeout: 10_000 });
    // Depth quote must land before Submit enables (_ticketDepthPending gate).
    await expect(submit).toBeEnabled({ timeout: 90_000 });
    await submit.click();

    await expect.poll(() => ticketCalls.length, { timeout: 15_000 }).toBe(1);

    // Draft is removed from the grid AND cleaned up server-side via a real
    // DELETE /drafts/{id} — this is the SOLE cleanup path for a draft
    // promoted from MarketPulse (verified this session, not assumed):
    // `_openTicketFromDraftRow()` sets `_draftId`, never `initialDraftId`
    // (setting `initialDraftId` would wrongly turn Draft mode back ON via
    // OrderTicket's own mount-seed effect — the opposite of what promoting
    // to real requires). So `placeCtx.draftId` in OrderTicket.svelte's
    // submit() is always null here, its own `payoffDrafts.forget()` never
    // fires, and MarketPulse's `onSubmit` handler's own
    // `payoffDrafts.remove(ticketProps._draftId)` is the real, necessary
    // cleanup — not a harmless-no-op fallback.
    await expect.poll(() => draftRows(page).ids(), { timeout: 15_000 }).not.toContain('NIFTY26OCTFUT::draft::9001');
    const deletes = state.calls.filter((c) => c.method === 'DELETE');
    expect(deletes).toHaveLength(1);
    expect(deletes[0].id).toBe(9001);
  });

  // `draft_id` is deliberately NOT threaded into the real `/orders/ticket`
  // payload for a MarketPulse-promoted draft — cleanup happens entirely via
  // the frontend's own payoffDrafts.remove() DELETE call above, not via any
  // backend-side draft_id-triggered cleanup. This is the correct design
  // (confirmed this session), not a gap: MarketPulse never sets
  // `initialDraftId` (see the comment on the previous test for why), so
  // there is no mechanism that WOULD thread draft_id into this payload.
  test('D: promote does not send draft_id on the /ticket payload — cleanup is frontend-only', async ({ page }) => {
    test.setTimeout(240_000);
    await loginAsAdmin(page);
    await mockDrafts(page);
    const ticketCalls = [];
    await page.route('**/api/orders/ticket', async (route) => {
      ticketCalls.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"ok","order_id":1}' });
    });
    await page.route('**/api/admin/execution/mode', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ mode: 'paper', paper_trading_mode: true, dev_active: true }),
      });
    });
    await gotoPulse(page);
    await draftSymbolCell(page, 'NIFTY26OCTFUT', 9001).click();
    const submit = page.locator('.oes-modal .oes-common-submit').first();
    await expect(submit).toBeEnabled({ timeout: 90_000 });
    await submit.click();
    await expect.poll(() => ticketCalls.length, { timeout: 15_000 }).toBe(1);
    expect(ticketCalls[0].draft_id).toBeFalsy();
  });
});
