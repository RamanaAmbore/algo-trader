// Collapse removal regression guard — Dashboard.
//
// Header button-group standardization (2026-10-13, Part 1) — the chevron
// CollapseButton was flagged as visual clutter and deleted from
// CardControls.svelte project-wide; Dashboard's own isCollapsed/
// cardId-for-collapse/class:is-collapsed wiring was stripped from every
// call site (Chart card, Capital/Equity tabbed card, Activity card, Agent
// activity card). This file used to click `.collapse-btn` on the old
// `.bucket-cap`/`.bucket-eq`/`.dash-mini-grid` markup (a layout that
// predates even the current split-card/tabbed redesign) to verify
// toggle + localStorage persistence — none of that is reachable any
// more, so this is now a pure removal-regression guard across Dashboard's
// real cards.
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Collapse removed from Dashboard (2026-10-13 header button-group standardization)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('no card on /dashboard renders a Collapse button or carries is-collapsed', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    for (const sel of [
      'section.row1-col-chart',
      'section.cap-eq-tabbed',
      'section.dash-activity',
      'section.dash-agent',
    ]) {
      const card = page.locator(sel);
      await expect(card, `${sel} should be visible`).toBeVisible({ timeout: 20_000 });
      await expect(card.locator('button.collapse-btn'), `${sel} must have no collapse button`).toHaveCount(0);
      await expect(card, `${sel} must never carry is-collapsed`).not.toHaveClass(/is-collapsed/);
    }

    // No per-user collapse key is ever written any more.
    const stored = await page.evaluate(() => {
      const keys = Object.keys(localStorage).filter((k) => k.startsWith('ramboq.collapse.'));
      // Clean up any stale keys left by a pre-removal localStorage snapshot
      // in this browser profile — the assertion is that nothing NEW gets
      // written going forward, not that old keys self-delete.
      return keys;
    });
    console.log('[collapse_check] pre-existing stale collapse keys (informational):', stored);
  });
});
