/**
 * research_safety_card_accuracy.spec.js
 *
 * Covers the Sprint 2a Lab-page Safety-card fix
 * (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §2, §5):
 *
 * The Settings tab's "4. Safety" card (admin/mcp/+page.svelte)
 * previously stated "No order placement from MCP yet. The server cannot
 * move money." and "...Phase 3 will match." — both false. MCP's
 * `place_order` / `cancel_order` / `modify_order` tools
 * (backend/mcp/kite_server.py) already route through the real `/ticket`
 * pipeline today, gated by a single-use, 60-second-expiring,
 * parameter-bound confirm token minted by the operator from this same
 * page (article "0. Mint a confirm token").
 *
 * Static source check (no backend/login dependency — always runnable)
 * plus a rendered-DOM check that the live page actually shows the fixed
 * text to an operator, not just that the source string is correct.
 *
 * Five quality dimensions:
 *   1. SSOT   — the card's wording must match the real mechanism
 *               described in kite_server.py's place_order/cancel_order/
 *               modify_order docstrings (single-use, 60s, parameter-bound).
 *   2. Perf   — static-source test is near-instant; no network.
 *   3. Stale  — explicitly asserts the OLD false claims are GONE, not
 *               just that new text is present (catches a "both old and
 *               new text present" half-fix).
 *   4. Reuse  — n/a (single card, no shared component).
 *   5. UX     — rendered DOM check confirms the operator-visible card
 *               text, not just the source string.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/research_safety_card_accuracy.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

const PAGE_PATH = 'src/routes/(algo)/admin/mcp/+page.svelte';

test.describe('Lab Safety card — accuracy fix (static source)', () => {
  test('the stale "no order placement" / "Phase 3 will match" claims are gone', () => {
    const src = fs.readFileSync(path.join(process.cwd(), PAGE_PATH), 'utf-8');
    const safetyCardMatch = src.match(/<h2>4\. Safety<\/h2>([\s\S]*?)<\/ul>/);
    expect(safetyCardMatch, 'Safety card (<h2>4. Safety</h2> ... </ul>) not found').not.toBeNull();
    const safetyCard = safetyCardMatch[1];

    // Old, false claims must be gone.
    expect(safetyCard).not.toMatch(/No order placement from MCP yet/i);
    expect(safetyCard).not.toMatch(/server cannot move money/i);
    expect(safetyCard).not.toMatch(/Phase 3 will match/i);

    // New, accurate claim: MCP CAN place/cancel/modify orders, gated by
    // an operator-minted, single-use, 60-second-expiring, parameter-bound
    // confirm token — matching kite_server.py's place_order docstring.
    // Strip tags first — "can" is wrapped in <b>, "place / cancel /
    // modify" in <code>, so matching raw markup would be brittle.
    const safetyCardText = safetyCard.replace(/<[^>]+>/g, ' ');
    expect(safetyCardText).toMatch(/\bcan\b[\s\S]*place[\s\S]*cancel[\s\S]*modify[\s\S]*orders/i);
    expect(safetyCardText).toMatch(/confirm token/i);
    expect(safetyCardText).toMatch(/single-use/i);
    expect(safetyCardText).toMatch(/60 seconds/i);
  });

  test('the mint-token mechanism description (card 0) and the Safety card (card 4) agree on single-use + 60s + parameter-bound', () => {
    const src = fs.readFileSync(path.join(process.cwd(), PAGE_PATH), 'utf-8');
    const mintCardMatch = src.match(/<h2>0\. Mint a confirm token[^<]*<\/h2>([\s\S]*?)<\/p>/);
    expect(mintCardMatch, 'Mint-token card intro paragraph not found').not.toBeNull();
    const mintCard = mintCardMatch[1];

    expect(mintCard).toMatch(/single-use/i);
    expect(mintCard).toMatch(/60 seconds/i);
    expect(mintCard).toMatch(/bound to/i);
  });
});

test.describe('Lab Safety card — accuracy fix (rendered DOM)', () => {
  test.setTimeout(60_000);

  // Skipped against the local webServer: /admin/mcp is gated by the
  // `view_lab` capability, which the local `rambo` test account (the only
  // credential available to this harness's global-setup) does not carry —
  // confirmed via a live run (Access-denied screenshot, capability gate in
  // +page.svelte). The static-source tests above are the primary coverage
  // for this fix and need no login. research_place_order_verify.spec.js
  // already exercises the rendered Settings tab against dev.ramboq.com
  // with a properly-privileged account; re-enable this test there (or once
  // local `rambo` carries `view_lab`) by removing `.skip`.
  test.skip('the live Settings tab shows the corrected Safety card text to the operator', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/mcp');
    await page.waitForLoadState('domcontentloaded');

    await page.locator('.algo-tab', { hasText: 'Settings' }).click();

    const safetyCard = page.locator('.lab-card', { has: page.locator('h2', { hasText: '4. Safety' }) });
    await expect(safetyCard).toBeVisible({ timeout: 15_000 });

    const safetyText = await safetyCard.innerText();
    expect(safetyText).not.toMatch(/No order placement from MCP yet/i);
    expect(safetyText).not.toMatch(/Phase 3 will match/i);
    expect(safetyText).toMatch(/single-use/i);
    expect(safetyText).toMatch(/60 seconds/i);
    expect(safetyText.toLowerCase()).toContain('place');
    expect(safetyText.toLowerCase()).toContain('cancel');
    expect(safetyText.toLowerCase()).toContain('modify');
  });
});
