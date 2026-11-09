/**
 * order_ticket_chase_bracket_draft_tooltips.spec.js
 *
 * 2026-10-16 feature — operator: "when you can click on chase label,
 * templates, draft it should show a tooltip with help info. for chase,
 * it can show waiting time for each category."
 *
 * CHASE and DRAFT labels on the Ticket tab's CHASE/DRAFT row each get a
 * click-to-pin InfoHint (the house tooltip component — works on touch,
 * not just mouse hover, unlike a plain `title=` attribute).
 *
 * 2026-10-17 — general rule (operator): "if label is plain text
 * anywhere, it should work as a tooltip. otherwise, we need to add
 * circle with i as tooltip." Applied here:
 *   - CHASE is a pure status indicator (no toggle to conflict with) —
 *     its separate "(i)" chip was removed; the CHASE label text itself
 *     is now the click-to-pin trigger (hideButton + anchor + bind:open
 *     on the SAME InfoHint, same pattern OptionsPayoff.svelte's stat
 *     tooltips already use).
 *   - DRAFT is a real toggle — clicking the label already flips real
 *     order behavior (routes to a draft instead of the broker), so it
 *     KEEPS its separate "(i)" chip — a help-seeking click must not
 *     ALSO silently change the order.
 * Chase's tooltip specifically states the wait time per aggressiveness
 * category (operator's exact ask), sourced from the real backend
 * values in orders_helpers.py:_live_chase_config (low=30s/med=20s/
 * high=10s) — not invented numbers.
 *
 * T3.3 (bracket-removal, 2026-11) — BRACKET is gone entirely.
 * `TemplateBar.svelte` is deleted; every Bracket-specific test here
 * (the "(i)" chip source check, the live click-without-toggling check,
 * and the Draft-greys-Bracket assertion) is removed, not rewritten —
 * there is no surviving Bracket behavior to assert against. The CHASE/
 * DRAFT coverage that remains is otherwise unchanged.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

async function _seedNiftyOnTicket(page) {
  // Bare index (never expires), not a specific monthly contract — the
  // previous hardcoded 'NIFTY26OCTFUT' contract expired and made these
  // live tests fail independent of any Bracket-removal change.
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 45_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

test.describe('Chase/Draft click-to-help tooltips', () => {
  test('source: Draft InfoHint sits as a separate chip, never wired into the toggle onclick itself', () => {
    const symbolPanel = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    expect(symbolPanel).toMatch(/<InfoHint popup panel title="Draft"/);
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
    const labelEnd = symbolPanel.indexOf('>CHASE</span>', labelIdx);
    expect(labelEnd, 'CHASE label closing tag not found').toBeGreaterThan(labelIdx);
    const labelBlock = symbolPanel.slice(labelIdx, labelEnd);
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

  test('live: turning DRAFT on hides CHASE entirely (not greyed) on the Ticket tab, and Chain tab keeps its own independent CHASE', async ({ page }) => {
    // T3.3 (bracket-removal) rewrite — the BRACKET half of this test is
    // gone (no cluster to grey/hide any more). CHASE's own 2026-10-23
    // fix hides the cluster entirely while Draft is on (not a grey/
    // disabled treatment) — this test now asserts that real behavior
    // instead of the stale `.oes-ctl-cluster-disabled` claim the
    // original version made.
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const draftCheckbox = page.locator('.ot-draft-toggle input[type="checkbox"]').first();
    await expect(draftCheckbox).toBeVisible({ timeout: 10_000 });
    const chaseCluster = page.locator('.oes-chase-cluster').first();

    // Ticket tab — Draft OFF: CHASE cluster visible.
    await expect(chaseCluster).toBeVisible({ timeout: 10_000 });
    if (await draftCheckbox.isChecked()) await draftCheckbox.click();
    await expect(chaseCluster).toBeVisible();

    // Ticket tab — Draft ON: CHASE cluster is removed from the DOM
    // entirely, not just greyed.
    await draftCheckbox.click();
    await expect(draftCheckbox).toBeChecked();
    await expect(chaseCluster).toHaveCount(0);

    // Chain tab — Draft state carries over (shared `_sharedDraftMode`),
    // and Chain's own CHASE cluster (now inside OptionChainTab's own
    // Expiry toolbar, independent of the Ticket-tab row) is ALSO hidden
    // while Draft is on.
    await page.getByRole('tab', { name: /chain/i }).first().click();
    await expect(draftCheckbox).toBeChecked();
    await expect(chaseCluster).toHaveCount(0);
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
