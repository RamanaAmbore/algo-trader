/**
 * agent_cli_editor_sprint_h.spec.js — Sprint H (2026-10)
 *
 * Playwright coverage for the autocomplete/dropdown-completeness gaps
 * fixed in this sprint (see .claude/PLAN.md's "Sprint H" section and
 * frontend/src/lib/command/grammars/agents.js's own "Sprint H" header
 * comment). Exhaustive per-item coverage lives in Vitest
 * (src/lib/__tests__/agentsSuggest.test.js) — this spec confirms the
 * dropdown actually renders and is selectable in the REAL
 * AgentCliEditor UI for a representative sample:
 *   - statement-head keywords (item 1)
 *   - order( parameter suggestions (item 4)
 *   - label suggestions (item 8)
 *   - "#" meta-tag suggestions (item 5)
 *   - the empty/whitespace-only textarea no longer shows a spurious
 *     "expected a name" error (operator-reported follow-up, same file).
 *
 * Mounted on /console (the shared CLI surface — see commandLineTabAgentDispatch
 * / console_relocation_cli_scope.spec.js for precedent), same pattern as
 * the existing agent_cli_editor_suggestion_hover_description.spec.js.
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — matches every other AgentCliEditor-adjacent spec).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

test.describe('AgentCliEditor — Sprint H autocomplete completeness', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/console');
    await page.waitForLoadState('networkidle');
  });

  test('empty textarea shows no compile error (follow-up fix, same file)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: TIMEOUT });
    // Freshly mounted, nothing typed yet.
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);

    // Whitespace-only — type then clear back to spaces, same "nothing
    // meaningful to parse" state the fix targets.
    await cliTextarea.click();
    await cliTextarea.pressSequentially('   ', { delay: 15 });
    // Give the 150ms debounce + effect a moment to settle.
    await page.waitForTimeout(400);
    await expect(page.locator('.agent-cli-errors')).toHaveCount(0);
  });

  test('statement-head keywords (WHEN/ALERT/DO/GIVEN) are suggested at the very start (item 1)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await cliTextarea.click();
    await cliTextarea.pressSequentially('ALE', { delay: 15 });

    const alertPill = page.locator('.agent-cli-suggest-pill', { hasText: 'ALERT' }).first();
    await expect(alertPill).toBeVisible({ timeout: TIMEOUT });
    await alertPill.click();

    await expect(cliTextarea).toHaveValue('ALERT');
  });

  test('order( gets real parameter suggestions — previously zero (item 4)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await cliTextarea.click();
    await cliTextarea.pressSequentially('order(', { delay: 15 });

    const accountPill = page.locator('.agent-cli-suggest-pill', { hasText: 'account' }).first();
    await expect(accountPill).toBeVisible({ timeout: TIMEOUT });

    // lots= (order's own sugar, not on place_order's bare catalog schema)
    // must also be offered — the whole point of the fix is that this
    // sugar wasn't visible before this sprint. The real place_order
    // catalog entry has enough params that "lots" can fall outside the
    // suggestion row's own top-12 display cap with nothing typed yet —
    // narrow with a partial so it's unambiguously the top (only) match.
    await cliTextarea.pressSequentially('lo', { delay: 15 });
    const lotsPill = page.locator('.agent-cli-suggest-pill', { hasText: 'lots' }).first();
    await expect(lotsPill).toBeVisible({ timeout: TIMEOUT });
  });

  test('bracket( and token( get real parameter suggestions — previously zero (item 4)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await cliTextarea.click();
    await cliTextarea.pressSequentially('bracket(', { delay: 15 });
    const tpPctPill = page.locator('.agent-cli-suggest-pill', { hasText: 'tp_pct' }).first();
    await expect(tpPctPill).toBeVisible({ timeout: TIMEOUT });

    await cliTextarea.fill('');
    await cliTextarea.click();
    await cliTextarea.pressSequentially('token(', { delay: 15 });
    const kindPill = page.locator('.agent-cli-suggest-pill', { hasText: 'kind' }).first();
    await expect(kindPill).toBeVisible({ timeout: TIMEOUT });
  });

  test('a bound label is suggested as a whole WHEN condition (item 8)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await cliTextarea.click();
    // The real catalog has dozens of metrics — with nothing typed yet
    // after "WHEN ", the label "x" ranks well past the suggestion row's
    // own top-12 display cap. Type the partial "x" itself so it's an
    // unambiguous exact-prefix match, same as the real operator flow
    // (typing toward what they want, not scanning an unfiltered list).
    await cliTextarea.pressSequentially('GIVEN x = pnl@positions.total <= -5 WHEN x', { delay: 10 });

    const xPill = page.locator('.agent-cli-suggest-pill', { hasText: /^x$/ }).first();
    await expect(xPill).toBeVisible({ timeout: TIMEOUT });
  });

  test('"#" suggests the meta-tag keys (item 5)', async ({ page }) => {
    const cliTextarea = page.locator('.agent-cli-textarea');
    await cliTextarea.click();
    await cliTextarea.pressSequentially('WHEN always ALERT nop DO nop #', { delay: 10 });

    const namePill = page.locator('.agent-cli-suggest-pill', { hasText: 'name=' }).first();
    await expect(namePill).toBeVisible({ timeout: TIMEOUT });
    await namePill.click();

    // The "#" itself is left in place — only the key fragment after it is
    // replaced by the picked suggestion.
    await expect(cliTextarea).toHaveValue('WHEN always ALERT nop DO nop #name=');
  });
});
