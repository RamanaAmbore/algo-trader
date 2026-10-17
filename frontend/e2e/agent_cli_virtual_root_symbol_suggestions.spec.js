/**
 * agent_cli_virtual_root_symbol_suggestions.spec.js
 *
 * Regression guard for a 2026-10-17 operator-reported gap: "does agent cli
 * recognize virtual roots? ... does it show in dropdown. this should be
 * consistent across." Every OTHER symbol-entry surface in the app
 * (SymbolSearchInput.svelte, used by Order Ticket/Chain/watchlist add) calls
 * `searchByPrefix()`, which injects virtual roots (GOLD/GOLD_NEXT,
 * CRUDEOIL/CRUDEOIL_NEXT, etc. — MCX/CDS auto-roll conventions) ahead of
 * real tradingsymbol matches. The Agent CLI's `symbol=` value dropdown
 * (AgentCliEditor.svelte, backed by `suggestTradingsymbols()` in
 * instruments.js) called a DIFFERENT, simpler function that never injected
 * virtual roots — typing a bare root like "CRUDE" in a `symbol=` argument
 * never surfaced "CRUDEOIL"/"CRUDEOIL_NEXT", even though every other symbol
 * picker in the app would. Fixed by extending `suggestTradingsymbols()` to
 * inject the same virtual-root set, sorted, ahead of real matches.
 *
 * Run:
 *   npx playwright test e2e/agent_cli_virtual_root_symbol_suggestions.spec.js \
 *   --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

/** Navigate to the Console tab's AgentCliEditor and wait for it to be ready. */
async function goConsole(page) {
  await loginAsAdmin(page);
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const textarea = page.locator('textarea.agent-cli-textarea');
  await textarea.waitFor({ timeout: TIMEOUT });
  const preview = page.locator('pre.agent-cli-preview-json');
  const errors = page.locator('.agent-cli-errors');
  await Promise.race([
    preview.waitFor({ timeout: 5_000 }).catch(() => {}),
    errors.waitFor({ timeout: 5_000 }).catch(() => {}),
    page.waitForTimeout(5_000),
  ]);
  await textarea.focus();
  return textarea;
}

function suggestionRows(page) {
  return page.locator('[data-testid="agent-cli-dropdown-row"]');
}

test.describe('Agent CLI symbol= dropdown — virtual-root parity with SymbolSearchInput', () => {
  test('typing a bare MCX root prefix (3+ chars) surfaces the virtual root symbol, not just real contracts', async ({ page }) => {
    const textarea = await goConsole(page);
    // DO-action call argument context — `symbol=` inside close_position(...)
    // is the exact shape `realKey === 'symbol'` gates on (agents.js).
    await textarea.fill('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO close_position(account=ZG0790, symbol=CRU');
    await page.waitForTimeout(400);

    const rows = suggestionRows(page);
    await expect(rows.first()).toBeVisible({ timeout: TIMEOUT });
    const texts = await rows.allTextContents();
    const joined = texts.join(' | ');
    expect(joined, `dropdown rows: ${joined}`).toMatch(/CRUDEOIL(?!M)\b/);
  });

  test('the virtual root sorts ahead of real contract matches for the same prefix', async ({ page }) => {
    const textarea = await goConsole(page);
    await textarea.fill('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO close_position(account=ZG0790, symbol=GOLD');
    await page.waitForTimeout(400);

    const rows = suggestionRows(page);
    await expect(rows.first()).toBeVisible({ timeout: TIMEOUT });
    const texts = await rows.allTextContents();
    const goldIdx = texts.findIndex((t) => t.trim() === 'GOLD');
    const realContractIdx = texts.findIndex((t) => /^GOLD\d/.test(t.trim()));
    expect(goldIdx, `GOLD (bare virtual root) not found in: ${JSON.stringify(texts)}`).toBeGreaterThanOrEqual(0);
    if (realContractIdx >= 0) {
      expect(goldIdx, 'virtual root GOLD must sort ahead of a real GOLD contract').toBeLessThan(realContractIdx);
    }
  });

  test('below the 3-character gate, no symbol suggestions appear at all (existing behavior unchanged)', async ({ page }) => {
    const textarea = await goConsole(page);
    await textarea.fill('WHEN pnl_pct@positions.total <= -5 ALERT telegram DO close_position(account=ZG0790, symbol=CR');
    await page.waitForTimeout(400);
    // Below 3 chars, suggestSymbols is never called (agents.js gate) — no
    // dropdown rows for the symbol value at all.
    const rows = suggestionRows(page);
    await expect(rows).toHaveCount(0);
  });
});
