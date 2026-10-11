/**
 * agent_cli_editor_suggestion_hover_description.spec.js — Sprint 11
 *
 * Sprint 11 added the hover/focus → description detail line under the
 * suggestion-pill row in AgentCliEditor.svelte (`_hoverLabel` /
 * `_hoverDesc`, `.agent-cli-suggest-desc`) but shipped with no
 * Playwright spec despite changing a `.svelte` file — closing that gap.
 *
 * Verified against the REAL current component (src/lib/command/
 * AgentCliEditor.svelte) rather than trusting the Sprint 11 design doc,
 * since Sprints 12-17 touched the same file repeatedly:
 *   - A suggestion pill (`.agent-cli-suggest-pill`) sets `_hoverLabel`
 *     on `mouseenter`/`focus`, clears it on `mouseleave`/`blur`.
 *   - `_hoverDesc` looks the label back up in the CURRENT
 *     `suggestResult.suggestions`/`descriptions` arrays (never a stale
 *     index) and is '' when the matched token has no backend-provided
 *     description.
 *   - `.agent-cli-suggest-desc` only renders `{#if _hoverDesc}` — no
 *     empty box for an undescribed token.
 *
 * Uses a real catalog token ('pnl', a condition metric with a non-empty
 * `description` in backend/config/grammars/agent_grammar.yaml) and
 * fetches that description live from `GET /api/admin/grammar/tokens`
 * rather than hardcoding the expected string, per this app's existing
 * convention of never re-deriving facts the backend already owns.
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — matches every other AgentCliEditor-adjacent spec).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

test.describe('AgentCliEditor — suggestion hover/focus shows description (Sprint 11)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('hovering the "pnl" suggestion pill shows its real catalog description; mouseleave clears it', async ({ page }) => {
    // Fetch the real description straight from the backend — never
    // hardcode a copy of config that could drift.
    const tokensResp = await page.request.get('/api/admin/grammar/tokens?grammar=condition');
    expect(tokensResp.ok()).toBe(true);
    const tokens = await tokensResp.json();
    const pnlToken = tokens.find((t) => t.token === 'pnl' && t.token_kind === 'metric');
    expect(pnlToken).toBeTruthy();
    expect(pnlToken.description).toBeTruthy();

    await page.goto('/console');
    await page.waitForLoadState('networkidle');

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: TIMEOUT });
    await cliTextarea.click();
    // pressSequentially fires real keydown/keyup per character — the
    // textarea's own onkeyup={_updateCursor} needs this (a plain .fill()
    // never moves cursorPos, which would leave the suggester evaluating
    // at position 0 instead of after "pn").
    await cliTextarea.pressSequentially('WHEN pn', { delay: 15 });

    const pnlPill = page.locator('.agent-cli-suggest-pill', { hasText: 'pnl' }).first();
    await expect(pnlPill).toBeVisible({ timeout: TIMEOUT });

    // No description shown before hovering anything.
    await expect(page.locator('.agent-cli-suggest-desc')).toHaveCount(0);

    await pnlPill.hover();
    const descEl = page.locator('.agent-cli-suggest-desc');
    await expect(descEl).toBeVisible({ timeout: TIMEOUT });
    await expect(descEl).toHaveText(pnlToken.description);

    // mouseleave (moving to the textarea, away from the pill) clears it.
    await cliTextarea.hover();
    await expect(page.locator('.agent-cli-suggest-desc')).toHaveCount(0);
  });

  test('keyboard focus on the suggestion pill shows the description; blur clears it', async ({ page }) => {
    const tokensResp = await page.request.get('/api/admin/grammar/tokens?grammar=condition');
    const tokens = await tokensResp.json();
    const pnlToken = tokens.find((t) => t.token === 'pnl' && t.token_kind === 'metric');

    await page.goto('/console');
    await page.waitForLoadState('networkidle');

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: TIMEOUT });
    await cliTextarea.click();
    await cliTextarea.pressSequentially('WHEN pn', { delay: 15 });

    const pnlPill = page.locator('.agent-cli-suggest-pill', { hasText: 'pnl' }).first();
    await expect(pnlPill).toBeVisible({ timeout: TIMEOUT });

    await pnlPill.focus();
    const descEl = page.locator('.agent-cli-suggest-desc');
    await expect(descEl).toBeVisible({ timeout: TIMEOUT });
    await expect(descEl).toHaveText(pnlToken.description);

    // Blur by moving focus back to the textarea.
    await cliTextarea.focus();
    await expect(page.locator('.agent-cli-suggest-desc')).toHaveCount(0);
  });
});
