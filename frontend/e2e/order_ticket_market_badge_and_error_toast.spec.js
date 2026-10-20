/**
 * order_ticket_market_badge_and_error_toast.spec.js
 *
 * 2026-10-20 — UX audit fix (parallel to a backend fix for an MCX evening-
 * session market-hours regression). Two frontend gaps covered here:
 *
 * 1. OrderTicket.svelte already computed a correct, holiday-aware `_isOpen`
 *    value (isNseOpen()/isMcxOpen()/isMarketOpen() from $lib/marketHours)
 *    on every depth-quote tick, but nothing rendered it — dead code. Fixed
 *    by surfacing a small per-exchange open/closed badge, reusing the exact
 *    palette convention RefreshButton.svelte already uses for its own
 *    `.rf-mkt-*` classes (open = algo-green, closed = muted slate), plus a
 *    30s visibleInterval re-check (mirrors RefreshButton's `_mktTimer`) so
 *    the badge flips at a session boundary even when depth polling is idle.
 *
 *    Critical placement note: the badge markup is deliberately OUTSIDE the
 *    `{#if !symbolHidden}` gate — SymbolPanel.svelte (the ONLY real caller
 *    of <OrderTicket>, per `grep -rln "<OrderTicket" src`) always passes
 *    `symbolHidden={true}` since it renders its own symbol picker. A badge
 *    gated on `!symbolHidden` would never render on the real embedded
 *    surface operators actually use to place orders.
 *
 * 2. The submit-error toast (`toast.error(...)`) truncated the error
 *    string to 80 chars via a bare `.slice(0, 80)` on the SHORT, already
 *    ~32-char-capped `submitErr` (api.js's `_trimDetail`, by design, for
 *    the persistent inline banner — CLAUDE.md: "~25-35 chars so layout
 *    doesn't shift"). A real 409 market-hours rejection (holiday / evening-
 *    session explanation) runs 130+ chars and got cut off mid-sentence
 *    well before even hitting the 80-char slice. Fixed by preferring the
 *    already-existing `submitErrFull` (api.js's untruncated
 *    `err.fullMessage` — the R6 fix's own "callers that have room... can
 *    show the operator what actually happened" mechanism) and raising the
 *    toast's own cap to 200 so a realistic message isn't re-clipped there
 *    either. `Toast.svelte`'s `.rbq-toast-msg` has no `white-space: nowrap`
 *    / `text-overflow: ellipsis` — wraps via `word-break: break-word`, so
 *    a longer string is not visually re-truncated by the toast UI itself.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

test.describe('OrderTicket market-state badge', () => {
  test('source: badge reads the existing _isOpen (no new market-hours math), reuses isNseOpen/isMcxOpen', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    // Still imports the same two functions RefreshButton.svelte uses —
    // no bespoke re-implementation of market-hours logic. isMarketOpen
    // was dropped from the import (2026-10-20, 2nd pass) along with the
    // permissive any-segment-open fallback for an unrecognized exchange.
    expect(orderTicket).toContain("import { isNseOpen, isMcxOpen } from '$lib/marketHours'");
    expect(orderTicket).not.toMatch(/import\s*\{[^}]*isMarketOpen[^}]*\}\s*from\s*'\$lib\/marketHours'/);
    expect(orderTicket).toMatch(/return isMcxOpen\(\);/);
    expect(orderTicket).toMatch(/return isNseOpen\(\);/);
    // Badge markup exists and is driven by _isOpen.
    expect(orderTicket).toContain('data-testid="ot-market-badge"');
    expect(orderTicket).toMatch(/class:ot-mkt-open=\{_isOpen\}/);
    expect(orderTicket).toMatch(/class:ot-mkt-closed=\{!_isOpen\}/);
  });

  test('source: badge per-exchange mapping mirrors the backend _EXCHANGE_TO_SEGMENT table exactly (2026-10-20 fix)', () => {
    // Backend table (agent_engine.py): NSE/BSE/NFO/BFO/CDS/BCD -> equity
    // (isNseOpen), MCX/NCO -> commodity (isMcxOpen). Before this fix,
    // CDS was wrongly routed through isMcxOpen() and NCO fell through
    // to the generic isMarketOpen() instead of isMcxOpen() -- the point
    // of the badge is matching that segment table, so a mismatch here
    // is the exact "two different numbers for the same fact" class of
    // defect the badge exists to prevent.
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const fnMatch = orderTicket.match(/function _mktOpenForExchange\([^)]*\)\s*\{[\s\S]*?\n  \}/);
    expect(fnMatch, '_mktOpenForExchange function not found').not.toBeNull();
    const fn = fnMatch[0];

    // MCX/NCO branch must route through isMcxOpen().
    const mcxBranch = fn.match(/if\s*\(([^)]*)\)\s*return isMcxOpen\(\);/);
    expect(mcxBranch, 'MCX/NCO branch (isMcxOpen) not found').not.toBeNull();
    expect(mcxBranch[1]).toMatch(/exch === 'MCX'/);
    expect(mcxBranch[1]).toMatch(/exch === 'NCO'/);
    expect(mcxBranch[1]).not.toMatch(/exch === 'CDS'/);

    // Equity branch must route through isNseOpen() and include CDS/BCD.
    const equityBranch = fn.match(/if\s*\(([^)]*)\)\s*return isNseOpen\(\);/);
    expect(equityBranch, 'equity branch (isNseOpen) not found').not.toBeNull();
    for (const exch of ['NFO', 'NSE', 'BSE', 'BFO', 'CDS', 'BCD']) {
      expect(equityBranch[1], `equity branch should include ${exch}`).toMatch(
        new RegExp(`exch === '${exch}'`)
      );
    }
    expect(equityBranch[1]).not.toMatch(/exch === 'NCO'/);

    // Unrecognized-exchange fallback must be fail-closed (false), not
    // the old permissive isMarketOpen() any-segment-open check — matches
    // the backend's _segment_for_exchange -> None -> "never wrongly
    // allow" convention (2026-10-20, 2nd pass).
    expect(fn).toMatch(/return false;\s*\}$/);
    expect(fn).not.toMatch(/isMarketOpen/);
  });

  test('source: badge markup sits OUTSIDE the symbolHidden gate (SymbolPanel is the only real caller and always sets symbolHidden=true)', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const gateIdx = orderTicket.indexOf('{#if !symbolHidden}');
    expect(gateIdx, 'symbolHidden gate not found').toBeGreaterThan(-1);
    const gateEndIdx = orderTicket.indexOf('{/if}', gateIdx);
    const gateBlock = orderTicket.slice(gateIdx, gateEndIdx);
    // The badge must NOT be inside the symbolHidden-gated block.
    expect(gateBlock).not.toContain('ot-market-badge');
    // It must appear shortly after the gate closes.
    const badgeIdx = orderTicket.indexOf('ot-market-badge', gateEndIdx);
    expect(badgeIdx, 'badge not found after the symbolHidden gate').toBeGreaterThan(-1);

    // Confirm the real-world embedding actually sets symbolHidden=true,
    // i.e. this placement decision is load-bearing, not theoretical.
    const symbolPanel = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    expect(symbolPanel).toMatch(/symbolHidden=\{true\}/);
  });

  test('source: badge re-checks on a 30s visibleInterval (not just on depth-quote ticks), mirroring RefreshButton.svelte', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicket).toMatch(/import \{[^}]*visibleInterval[^}]*\} from '\$lib\/stores'/);
    expect(orderTicket).toMatch(/visibleInterval\(_recomputeMktOpen,\s*30_000\)/);
    // Timer must be torn down on destroy — same convention as every
    // other interval-owning effect in this file.
    const onDestroyIdx = orderTicket.indexOf('onDestroy(() => {');
    const onDestroyBlock = orderTicket.slice(onDestroyIdx, onDestroyIdx + 400);
    expect(onDestroyBlock).toMatch(/_mktTimer\?\.\(\)/);
  });

  test('source: badge CSS uses app.css tokens, no hardcoded hex/rgba literals', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const openIdx = orderTicket.indexOf('.ot-mkt-open {');
    const closedEndIdx = orderTicket.indexOf('.ot-label {', openIdx);
    expect(openIdx).toBeGreaterThan(-1);
    const block = orderTicket.slice(openIdx, closedEndIdx);
    expect(block).not.toMatch(/#[0-9a-fA-F]{3,6}/);
    expect(block).not.toMatch(/rgba\(/);
    expect(block).toContain('var(--algo-green-bg-mid)');
    expect(block).toContain('var(--algo-green-border)');
    expect(block).toContain('var(--algo-green-text)');
    expect(block).toContain('var(--algo-muted-bg)');
    expect(block).toContain('var(--algo-muted-border)');
    expect(block).toContain('var(--algo-dim)');
  });

  test('live: market badge renders on the real embedded surface (/orders ticket, via SymbolPanel)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const badge = page.locator('[data-testid="ot-market-badge"]').first();
    await expect(badge).toBeVisible({ timeout: 10_000 });
    const text = (await badge.textContent() || '').trim();
    expect(text).toMatch(/\b(open|closed)\b/i);
    const cls = await badge.evaluate((el) => el.className);
    expect(cls).toMatch(/ot-mkt-(open|closed)/);
  });

  test('live: badge reflects a fixed /api/market/status mock per-exchange, not just "renders some text" (2026-10-20 extension)', async ({ page }) => {
    // Shape matches backend/api/routes/market.py:MarketStatusResponse
    // exactly (nse_open, mcx_open, any_open, is_holiday, checked_at).
    await page.route('**/api/market/status', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nse_open: false,
        mcx_open: true,
        any_open: true,
        is_holiday: false,
        checked_at: new Date().toISOString(),
      }),
    }));

    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });

    // MCX symbol — mcx_open:true in the mock → badge must read "MCX open".
    await symInput.fill('CRUDEOIL26NOVFUT');
    const mcxSugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(mcxSugg).toBeVisible({ timeout: 10_000 });
    await mcxSugg.click({ force: true });
    // Settle delay — the pick triggers a margin/depth-quote fetch whose
    // transient overlay can occlude the search box for a moment; a
    // `force:true` click issued into that window lands on the wrong
    // element (empirically confirmed: the input's text still updates
    // via fill(), but the instrument pick silently doesn't register).
    await page.waitForTimeout(600);

    const badge = page.locator('[data-testid="ot-market-badge"]').first();
    await expect(badge).toBeVisible({ timeout: 10_000 });
    await expect(badge).toHaveText(/^MCX open$/, { timeout: 10_000 });
    await expect(badge).toHaveClass(/ot-mkt-open/);
    await expect(badge).not.toHaveClass(/ot-mkt-closed/);

    // NFO symbol — nse_open:false in the mock → badge must read "NFO closed".
    await symInput.fill('NIFTY26OCTFUT');
    const nfoSugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(nfoSugg).toBeVisible({ timeout: 10_000 });
    await nfoSugg.click({ force: true });
    await page.waitForTimeout(600);

    await expect(badge).toHaveText(/^NFO closed$/, { timeout: 10_000 });
    await expect(badge).toHaveClass(/ot-mkt-closed/);
    await expect(badge).not.toHaveClass(/ot-mkt-open/);
  });
});

test.describe('OrderTicket submit-error toast truncation', () => {
  test('source: toast prefers submitErrFull and caps at 200, not the old bare submitErr.slice(0, 80)', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicket).not.toMatch(/toast\.error\(`Order failed[^`]*\$\{submitErr\}`\.slice\(0,\s*80\)/);
    expect(orderTicket).toMatch(/toast\.error\(`Order failed[^`]*\$\{submitErrFull \|\| submitErr\}`\.slice\(0,\s*200\)/);
  });

  test('live: a real 409 market-hours rejection renders its FULL detail in the toast, not truncated (2026-10-20 rewrite)', async ({ page }) => {
    // Replaces the old made-up-string "logic" test, which only exercised
    // String.slice() in Node and never touched the app. This intercepts
    // the real /api/orders/ticket submit call and reads the actual
    // rendered .rbq-toast-msg element — the strongest guard for this
    // class of bug (same pattern as
    // order_ticket_template_toggle_regression.spec.js's request-
    // interception test).
    //
    // Detail text mirrors the EXACT wording
    // backend/api/routes/orders_place.py:_ticket_closed_market_detail()
    // produces for an MCX evening-session rejection (134 chars, > the
    // task's 130-char floor and well past the OLD 80-char toast cap).
    const detail =
      "Exchange MCX is closed. Orders for CRUDEOIL26NOVFUT can only be " +
      "placed during MCX's market hours, or its evening session on a holiday.";
    expect(detail.length).toBeGreaterThan(130);

    await page.route('**/api/admin/execution/mode', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'] }),
        });
      } else {
        await route.continue();
      }
    });
    await page.route('**/api/orders/ticket', async (route) => {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail }),
      });
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('CRUDEOIL26NOVFUT');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 10_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(500);

    // MARKET avoids the "limit price required" validation gate.
    const typeTrigger = page.locator('[aria-label="Order type"]').first();
    await typeTrigger.click();
    const marketOpt = page.locator('.rbq-select-option-label').filter({ hasText: /^MARKET$/ }).first();
    await marketOpt.click();
    await page.waitForTimeout(300);

    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit, .ot-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 8_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();

    const toastMsg = page.locator('.rbq-toast-msg').first();
    await expect(toastMsg).toBeVisible({ timeout: 5_000 });
    const text = (await toastMsg.textContent()) || '';
    expect(text).toContain(detail);
    // Regression guard: the OLD 80-char slice on the bare (already-
    // trimmed) submitErr would have cut this well short of the full
    // sentence — confirm the rendered text is NOT the truncated form.
    expect(text.length).toBeGreaterThan(80);
  });

  test('source: api.js exposes an untruncated err.fullMessage so the toast layer has something non-trimmed to read', () => {
    const api = readFileSync(`${ROOT}/api.js`, 'utf-8');
    // _trimDetail (~32-35 chars) stays reserved for the short, persistent
    // inline banner convention (CLAUDE.md) — this assertion only checks
    // that a parallel, untruncated path still exists for callers with
    // more room (the R6 fix's own stated purpose).
    expect(api).toMatch(/err\)\.fullMessage\s*=/);
    expect(api).toContain('_fullDetail(detail)');
  });

  test('source: Toast.svelte does not visually re-clip the message (no nowrap/ellipsis on .rbq-toast-msg)', () => {
    const toastSvelte = readFileSync(`${ROOT}/Toast.svelte`, 'utf-8');
    const idx = toastSvelte.indexOf('.rbq-toast-msg {');
    expect(idx).toBeGreaterThan(-1);
    const block = toastSvelte.slice(idx, toastSvelte.indexOf('}', idx));
    expect(block).not.toContain('white-space: nowrap');
    expect(block).not.toContain('text-overflow: ellipsis');
  });
});
