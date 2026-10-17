/**
 * order_ticket_chase_bracket_draft_tooltips.spec.js
 *
 * 2026-10-16 feature — operator: "when you can click on chase label,
 * templates, draft it should show a tooltip with help info. for chase,
 * it can show waiting time for each category."
 *
 * CHASE, BRACKET, and DRAFT labels on the Ticket tab's CHASE/BRACKET/
 * DRAFT row each get a click-to-pin InfoHint (the house tooltip
 * component — works on touch, not just mouse hover, unlike a plain
 * `title=` attribute).
 *
 * 2026-10-17 — general rule (operator): "if label is plain text
 * anywhere, it should work as a tooltip. otherwise, we need to add
 * circle with i as tooltip." Applied here:
 *   - CHASE is a pure status indicator (no toggle to conflict with) —
 *     its separate "(i)" chip was removed; the CHASE label text itself
 *     is now the click-to-pin trigger (hideButton + anchor + bind:open
 *     on the SAME InfoHint, same pattern OptionsPayoff.svelte's stat
 *     tooltips already use).
 *   - BRACKET and DRAFT are real toggles — clicking the label already
 *     flips real order behavior (attaches/detaches a bracket, routes
 *     to a draft instead of the broker), so they KEEP their separate
 *     "(i)" chip — a help-seeking click must not ALSO silently change
 *     the order.
 * Chase's tooltip specifically states the wait time per aggressiveness
 * category (operator's exact ask), sourced from the real backend
 * values in orders_helpers.py:_live_chase_config (low=30s/med=20s/
 * high=10s) — not invented numbers.
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

test.describe('Chase/Bracket/Draft click-to-help tooltips', () => {
  test('source: Bracket and Draft InfoHints sit as separate chips, never wired into the toggle onclick itself', () => {
    const templateBar = readFileSync(`${ROOT}/TemplateBar.svelte`, 'utf-8');
    expect(templateBar).toContain("import InfoHint from '$lib/InfoHint.svelte'");
    expect(templateBar).toMatch(/<InfoHint popup panel title="Bracket"/);

    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicket).toMatch(/<InfoHint popup panel title="Draft"/);
  });

  test('source: Chase has NO separate "(i)" chip — the label itself is the hideButton/anchor trigger (2026-10-17)', () => {
    const symbolPanel = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    expect(symbolPanel).toContain("import InfoHint from '$lib/InfoHint.svelte'");
    // The InfoHint still exists (pinned popover), but with hideButton +
    // anchor — no visible icon of its own.
    expect(symbolPanel).toMatch(/<InfoHint popup hideButton id="chase-info-hint" anchor=\{_chaseInfoAnchor\} bind:open=\{_chaseInfoOpen\}/);
    // The CHASE label span itself carries the click/keydown trigger and
    // is the bind:this anchor target.
    const labelIdx = symbolPanel.indexOf('class="oes-common-chase-label on"');
    expect(labelIdx, 'CHASE label span not found').toBeGreaterThan(-1);
    const labelBlock = symbolPanel.slice(labelIdx, labelIdx + 300);
    expect(labelBlock).toMatch(/bind:this=\{_chaseInfoAnchor\}/);
    expect(labelBlock).toMatch(/onclick=\{.*_chaseInfoOpen = !_chaseInfoOpen.*\}/);
  });

  test("source: Chase's tooltip states the real per-tier wait times (30s/20s/10s), not invented numbers", () => {
    // Values must match backend/api/routes/orders_helpers.py:_live_chase_config
    // (low=30s/0.05 step/30 attempts, med=20s/0.10/20, high=10s/0.25/10) —
    // this test only checks the seconds figures since those are what the
    // operator actually asked for ("waiting time for each category").
    const symbolPanel = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    const idx = symbolPanel.indexOf('title="Chase"');
    expect(idx, 'Chase InfoHint not found').toBeGreaterThan(-1);
    const block = symbolPanel.slice(idx, idx + 500);
    expect(block).toMatch(/Low.*30s/);
    expect(block).toMatch(/Med.*20s/);
    expect(block).toMatch(/High.*10s/);
  });

  test('live: clicking the Bracket InfoHint shows help text WITHOUT toggling the bracket', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    const wasActive = await toggle.evaluate((el) => el.classList.contains('active'));

    const bracketHint = toggle.locator('xpath=following-sibling::*[1]//button');
    await expect(bracketHint).toBeVisible({ timeout: 5_000 });
    await bracketHint.click();

    const popup = page.locator('.info-popout, [class*="popout"]').first();
    await expect(popup).toBeVisible({ timeout: 5_000 });
    await expect(popup).toContainText(/exit-rule preset/i);

    // The toggle's own active/inactive state must be unchanged by the
    // help click — this is the whole point of using a separate chip.
    const stillSameState = await toggle.evaluate((el) => el.classList.contains('active'));
    expect(stillSameState, 'clicking the Bracket InfoHint must not flip the toggle').toBe(wasActive);
  });

  test('live: clicking the Draft InfoHint shows help text WITHOUT toggling draft mode', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const draftToggle = page.locator('.ot-draft-toggle').first();
    const draftCheckbox = draftToggle.locator('input[type="checkbox"]');
    if (await draftToggle.count() === 0) {
      test.skip(true, 'Draft toggle not visible for this symbol/action');
      return;
    }
    const wasChecked = await draftCheckbox.isChecked();

    const draftHint = draftToggle.locator('xpath=following-sibling::*[1]//button');
    await expect(draftHint).toBeVisible({ timeout: 5_000 });
    await draftHint.click();

    const popup = page.locator('.info-popout, [class*="popout"]').first();
    await expect(popup).toBeVisible({ timeout: 5_000 });
    await expect(popup).toContainText(/payoff chart/i);

    const stillChecked = await draftCheckbox.isChecked();
    expect(stillChecked, 'clicking the Draft InfoHint must not flip the checkbox').toBe(wasChecked);
  });

  test('live: clicking the CHASE label itself (no separate chip) shows the wait-time tooltip', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const chaseLabel = page.locator('.oes-common-chase-label').first();
    if (await chaseLabel.count() === 0) {
      test.skip(true, 'Chase label not visible for this symbol/order type');
      return;
    }
    await expect(chaseLabel).toBeVisible({ timeout: 10_000 });
    await chaseLabel.click();

    const popup = page.locator('.info-popout, [class*="popout"]').first();
    await expect(popup).toBeVisible({ timeout: 5_000 });
    await expect(popup).toContainText(/30s/);
    await expect(popup).toContainText(/20s/);
    await expect(popup).toContainText(/10s/);
  });
});
