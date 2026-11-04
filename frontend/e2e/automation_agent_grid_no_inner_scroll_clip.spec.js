/**
 * automation_agent_grid_no_inner_scroll_clip.spec.js — 2026-11-03, operator:
 * "when I click on a card the edit and other buttons are sometimes hidden
 * behind other cards" + "is there anyway to make agents page with list of
 * agents to look better".
 *
 * Root cause: `.agent-group-grid` (Automation page's agent/alert/bracket
 * card list) had `max-height: 32rem; overflow-y: auto` — a SEPARATE,
 * nested scroll region from the page's own scroll, with no visible
 * scrollbar affordance. Live-measured with the real 23-agent seed data:
 * the grid's `scrollHeight` already exceeded its capped `clientHeight`
 * even before any card was expanded. Expanding a card near that boundary
 * pushed its own Edit/Delete footer past the clipped edge — invisible,
 * not literally behind another card, but reading exactly that way. No
 * other list surface in the app uses this nested-scroll-cap pattern.
 * Fixed by removing the cap — the grid now grows with its content and
 * the page scrolls it, like every other list page.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { agentKindTrigger, pickAgentKind } from './fixtures/agentKind.js';

const TIMEOUT = 20_000;

test.use({ projects: ['chromium-desktop'], viewport: { width: 1400, height: 900 } });

test('agent-group-grid has no inner max-height/overflow clip — expanded card content is never cut off', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await expect(agentKindTrigger(page)).toBeVisible({ timeout: TIMEOUT });
  await pickAgentKind(page, 'agents');
  await page.waitForSelector('[role="listitem"]', { timeout: TIMEOUT });

  const grid = page.locator('.agent-group-grid').first();
  await expect(grid).toBeVisible({ timeout: TIMEOUT });

  const style = await grid.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { overflowY: cs.overflowY, maxHeight: cs.maxHeight };
  });
  expect(style.overflowY).toBe('visible');
  expect(style.maxHeight).toBe('none');

  // The grid's own clientHeight must always equal its scrollHeight — no
  // content is ever clipped inside it.
  const geom = await grid.evaluate((el) => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }));
  expect(geom.clientHeight).toBe(geom.scrollHeight);

  // Expand the first row and confirm its Edit button is genuinely visible
  // (not just present in the DOM, which the old clipped box would still
  // report as "attached" despite being scrolled out of view).
  const rows = page.locator('[role="listitem"]');
  await rows.nth(0).locator('[role="button"]').first().click();
  const editBtn = rows.nth(0).getByRole('button', { name: 'Edit' });
  await expect(editBtn).toBeVisible({ timeout: TIMEOUT });

  const geomAfter = await grid.evaluate((el) => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }));
  expect(geomAfter.clientHeight).toBe(geomAfter.scrollHeight);
  // The grid genuinely grew to accommodate the expanded card.
  expect(geomAfter.scrollHeight).toBeGreaterThan(geom.scrollHeight);
});
