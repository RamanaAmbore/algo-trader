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
  test('source: badge reads the existing _isOpen (no new market-hours math), reuses isNseOpen/isMcxOpen/isMarketOpen', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    // Still imports the same three functions RefreshButton.svelte uses —
    // no bespoke re-implementation of market-hours logic.
    expect(orderTicket).toContain("import { isNseOpen, isMcxOpen, isMarketOpen } from '$lib/marketHours'");
    expect(orderTicket).toMatch(/_isOpen\s*=\s*isMcxOpen\(\)/);
    expect(orderTicket).toMatch(/_isOpen\s*=\s*isNseOpen\(\)/);
    expect(orderTicket).toMatch(/_isOpen\s*=\s*isMarketOpen\(\)/);
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
    // of the badge is matching the real order-placement gate, so a
    // mismatch here is the exact "two different numbers for the same
    // fact" class of defect the badge exists to prevent.
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const fnMatch = orderTicket.match(/function _recomputeMktOpen\(\)[\s\S]*?\n  \}/);
    expect(fnMatch, '_recomputeMktOpen function not found').not.toBeNull();
    const fn = fnMatch[0];

    // MCX/NCO branch must route through isMcxOpen().
    const mcxBranch = fn.match(/if\s*\(([^)]*)\)\s*\{\s*\n\s*_isOpen = isMcxOpen\(\);/);
    expect(mcxBranch, 'MCX/NCO branch (isMcxOpen) not found').not.toBeNull();
    expect(mcxBranch[1]).toMatch(/exch === 'MCX'/);
    expect(mcxBranch[1]).toMatch(/exch === 'NCO'/);
    expect(mcxBranch[1]).not.toMatch(/exch === 'CDS'/);

    // Equity branch must route through isNseOpen() and include CDS/BCD.
    const equityBranch = fn.match(/else if\s*\(([^)]*)\)\s*\{\s*\n\s*_isOpen = isNseOpen\(\);/);
    expect(equityBranch, 'equity branch (isNseOpen) not found').not.toBeNull();
    for (const exch of ['NFO', 'NSE', 'BSE', 'BFO', 'CDS', 'BCD']) {
      expect(equityBranch[1], `equity branch should include ${exch}`).toMatch(
        new RegExp(`exch === '${exch}'`)
      );
    }
    expect(equityBranch[1]).not.toMatch(/exch === 'NCO'/);
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
});

test.describe('OrderTicket submit-error toast truncation', () => {
  test('source: toast prefers submitErrFull and caps at 200, not the old bare submitErr.slice(0, 80)', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicket).not.toMatch(/toast\.error\(`Order failed[^`]*\$\{submitErr\}`\.slice\(0,\s*80\)/);
    expect(orderTicket).toMatch(/toast\.error\(`Order failed[^`]*\$\{submitErrFull \|\| submitErr\}`\.slice\(0,\s*200\)/);
  });

  test('logic: a realistic ~135-char market-hours rejection message survives the toast slice uncut', () => {
    // Mirrors the exact construction in OrderTicket.svelte's catch block:
    //   toast.error(`Order failed — ${submitErrFull || submitErr}`.slice(0, 200), ...)
    const marketHoursDetail =
      'MCX is closed for the evening session today due to a scheduled ' +
      'exchange holiday. Orders can be placed once the next trading ' +
      'session opens at 09:00 IST tomorrow.';
    expect(marketHoursDetail.length).toBeGreaterThan(130);

    const msg = `Order failed — ${marketHoursDetail}`.slice(0, 200);
    expect(msg).toBe(`Order failed — ${marketHoursDetail}`);
    expect(msg.endsWith('tomorrow.')).toBe(true);

    // Regression guard: the OLD 80-char cap would have cut this message
    // mid-word, well before the sentence (let alone the explanation) ends.
    const oldTruncated = `Order failed — ${marketHoursDetail}`.slice(0, 80);
    expect(oldTruncated.endsWith('tomorrow.')).toBe(false);
    expect(oldTruncated.length).toBeLessThan(msg.length);
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
