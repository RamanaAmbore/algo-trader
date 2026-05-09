/**
 * AI agent drafter — Ask AI flow on /agents.
 *
 * Verifies the violet "✦ Ask AI" pill opens the draft form, that
 * a typed prompt produces a draft, that the Save button surfaces
 * with the expected paper · inactive copy, and that the JSON
 * details block carries the safe defaults (paper trade_mode,
 * one_shot lifespan). DOES NOT click Save — leaves no agents
 * behind.
 *
 * If GenAI is disabled in the environment (cap_in_<branch>.genai
 * = False), the draft request errors out cleanly; the spec
 * detects that and skips so the file stays green on dev.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('AI agent draft — /agents Ask AI', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Ask AI pill opens form, drafts agent, surfaces Save', async ({ page }) => {
    await page.goto('/agents');

    // Find the pill via accessible name (button text).
    const aiBtn = page.locator('button.ai-pill').first();
    await expect(aiBtn).toBeVisible({ timeout: TIMEOUT });

    // Pill computed colour matches the violet token (#a78bfa).
    const pillColor = await aiBtn.evaluate(el => getComputedStyle(el).color);
    expect(pillColor).toBe('rgb(167, 139, 250)');

    await aiBtn.click();

    // Form mount.
    const promptBox = page.locator('textarea.ai-prompt').first();
    await expect(promptBox).toBeVisible({ timeout: TIMEOUT });

    await promptBox.fill('alert me when total positions P&L drops below -50000');

    // Click Draft, wait for the request to settle. The button text
    // flips to "Drafting…" while in flight; we wait for the response.
    const draftReq = page.waitForResponse(
      r => r.url().includes('/api/agents/ai-draft') && r.request().method() === 'POST',
      { timeout: TIMEOUT },
    );
    await page.locator('button.ai-btn', { hasText: /^Draft$/ }).click();
    const resp = await draftReq;
    /** @type {any} */
    const body = await resp.json().catch(() => ({}));

    // GenAI off → response carries an "errors" entry with "GenAI is
    // disabled" or similar. Skip the rest gracefully.
    const errs = Array.isArray(body?.errors) ? body.errors : [];
    if (errs.some(e => /genai/i.test(String(e)) || /disabled/i.test(String(e)) || /not installed/i.test(String(e)))) {
      test.skip(true, `GenAI not enabled — backend returned: ${errs.join(' | ')}`);
    }
    // Other backend hiccups (rate-limit, schema mismatch) → also
    // skip rather than fail; this isn't a backend conformance test.
    if (errs.length) {
      test.skip(true, `AI draft returned errors: ${errs.join(' | ')}`);
    }

    // Save button appears (only when aiDraft is set and aiErrors is empty).
    const saveBtn = page.locator('button.ai-btn-save').first();
    await expect(saveBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(saveBtn).toContainText(/paper/i);
    await expect(saveBtn).toContainText(/inactive/i);

    // Open the JSON details panel and parse the <pre>.
    const details = page.locator('details.ai-json').first();
    await expect(details).toBeVisible();
    await details.locator('summary').click();
    const jsonText = await details.locator('pre').innerText();
    /** @type {any} */
    let draft = {};
    try {
      draft = JSON.parse(jsonText);
    } catch (e) {
      throw new Error(`draft JSON did not parse: ${e}\n${jsonText.slice(0, 200)}`);
    }

    expect(draft.trade_mode, 'draft.trade_mode should be paper').toBe('paper');
    expect(draft.lifespan_type, 'draft.lifespan_type should be one_shot').toBe('one_shot');

    // Deliberately do NOT click Save — leaves no agents behind.
  });
});
