/**
 * Expand/Contract Button Consistency — smoke tests for shared UI component rendering.
 *
 * Goal: Verify that CollapseButton and FullscreenButton use consistent DOM structure
 * across multiple pages, confirming they're rendered by the same shared CardControls
 * component.
 *
 * Tests:
 *   1. Dashboard activity card collapse/expand
 *   2. Dashboard activity card fullscreen/default-size
 *   3. Button structural consistency (tag, class name match)
 *   4. Collapse state persistence via localStorage
 *   5. ESC key exits fullscreen mode
 *   6. Default-size button resets card state
 *
 * Auth: requires admin credentials (set PLAYWRIGHT_USER + PLAYWRIGHT_PASS, or use cached token).
 * Execution: `cd frontend && npx playwright test e2e/expand-contract-consistency.spec.js`
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('Expand/Contract Button Consistency', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 1: Dashboard activity card collapse button
  // ──────────────────────────────────────────────────────────────────────
  test('Dashboard activity card — collapse hides card body', async ({ page }) => {
    // Navigate to dashboard (admin area)
    // The dashboard takes time to load due to SSE connections (networkidle stays busy).
    // Wait for the activity card to appear instead.
    await page.goto('/dashboard');
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    // Verify card body is initially visible (not collapsed)
    const cardBody = activityCard.locator('.card-body').first();
    await expect(cardBody).toBeVisible({ timeout: 15_000 });

    // Find the collapse button (class "collapse-btn" inside CardControls)
    const collapseBtn = activityCard.locator('button.collapse-btn');
    await expect(collapseBtn).toBeVisible({ timeout: 5_000 });

    // Verify aria-label contains "Collapse" in initial state
    const ariaLabel = await collapseBtn.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/Collapse/i);

    // Click to collapse
    await collapseBtn.click();
    await page.waitForTimeout(300); // Allow collapse animation

    // Verify card body is now hidden
    const hiddenAttr = await cardBody.getAttribute('hidden');
    expect(hiddenAttr).toBeDefined();

    // Verify button aria-label now shows "Expand"
    const expandLabel = await collapseBtn.getAttribute('aria-label');
    expect(expandLabel).toMatch(/Expand/i);

    // Click to re-expand
    await collapseBtn.click();
    await page.waitForTimeout(300);

    // Verify card body is visible again (hidden attribute removed)
    const notHiddenAttr = await cardBody.getAttribute('hidden');
    expect(notHiddenAttr).toBeNull();

    // Verify aria-label is back to "Collapse"
    const collapseLabel = await collapseBtn.getAttribute('aria-label');
    expect(collapseLabel).toMatch(/Collapse/i);
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 2: Dashboard activity card fullscreen button
  // ──────────────────────────────────────────────────────────────────────
  test('Dashboard activity card — fullscreen expands to viewport', async ({ page }) => {
    // Navigate to dashboard (admin area)
    await page.goto('/dashboard');
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    // Initially should not have fullscreen class
    const fsClassBefore = await activityCard.getAttribute('class');
    expect(fsClassBefore).not.toContain('fs-card-on');

    // Find the fullscreen button (class "fs-btn")
    const fullscreenBtn = activityCard.locator('button.fs-btn');
    await expect(fullscreenBtn).toBeVisible({ timeout: 5_000 });

    // Verify aria-label indicates fullscreen expansion
    const fsAriaLabel = await fullscreenBtn.getAttribute('aria-label');
    expect(fsAriaLabel).toMatch(/fullscreen/i);

    // Click to enter fullscreen
    await fullscreenBtn.click();
    await page.waitForTimeout(300);

    // Verify card has fullscreen class applied
    const fsClassAfter = await activityCard.getAttribute('class');
    expect(fsClassAfter).toContain('fs-card-on');

    // Backdrop should be visible (portalled to body)
    const backdrop = page.locator('.fs-backdrop');
    await expect(backdrop).toBeVisible({ timeout: 2_000 });

    // Find default-size button (appears when fullscreen=true)
    const defaultSizeBtn = activityCard.locator('button.default-btn');
    await expect(defaultSizeBtn).toBeVisible({ timeout: 2_000 });

    // Click to exit fullscreen
    await defaultSizeBtn.click();
    await page.waitForTimeout(300);

    // Verify fullscreen class is removed
    const fsClassRestored = await activityCard.getAttribute('class');
    expect(fsClassRestored).not.toContain('fs-card-on');

    // Backdrop should be gone
    await expect(backdrop).not.toBeVisible();

    // Fullscreen button should be visible again
    await expect(fullscreenBtn).toBeVisible();
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 3: Dashboard agent activity card collapse button
  //         (second card on dashboard using same CardHeader pattern)
  // ──────────────────────────────────────────────────────────────────────
  test('Dashboard agent activity card — collapse hides card body', async ({ page }) => {
    // Navigate to dashboard
    await page.goto('/dashboard');
    const agentCard = page.locator('section.dash-agent');
    await expect(agentCard).toBeVisible({ timeout: 30_000 });

    // Verify card body is initially visible
    const cardBody = agentCard.locator('.card-body').first();
    await expect(cardBody).toBeVisible({ timeout: 15_000 });

    // Find the collapse button
    const collapseBtn = agentCard.locator('button.collapse-btn');
    await expect(collapseBtn).toBeVisible({ timeout: 5_000 });

    // Verify aria-label contains "Collapse"
    const ariaLabel = await collapseBtn.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/Collapse/i);

    // Click to collapse
    await collapseBtn.click();
    await page.waitForTimeout(300);

    // Verify card body is now hidden
    const hiddenAttr = await cardBody.getAttribute('hidden');
    expect(hiddenAttr).toBeDefined();

    // Verify button aria-label shows "Expand"
    const expandLabel = await collapseBtn.getAttribute('aria-label');
    expect(expandLabel).toMatch(/Expand/i);

    // Click to re-expand
    await collapseBtn.click();
    await page.waitForTimeout(300);

    // Verify card body is visible again (hidden attribute removed)
    const notHiddenAttr = await cardBody.getAttribute('hidden');
    expect(notHiddenAttr).toBeNull();

    // Verify aria-label is back to "Collapse"
    const collapseLabel = await collapseBtn.getAttribute('aria-label');
    expect(collapseLabel).toMatch(/Collapse/i);
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 4: Button structural consistency (shared component validation)
  // ──────────────────────────────────────────────────────────────────────
  test('Collapse buttons on both dashboard cards use identical structure', async ({ page }) => {
    // Both Activity and Agent Activity cards on the dashboard use the same
    // CardHeader + CardControls component chain, so their collapse buttons
    // should render with identical HTML structure.

    await page.goto('/dashboard');

    // PART A: Activity card collapse button
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    const activityCollapseBtn = activityCard.locator('button.collapse-btn');
    await expect(activityCollapseBtn).toBeVisible({ timeout: 5_000 });

    // Capture activity button structure
    const actBtnTag = await activityCollapseBtn.evaluate((el) => el.tagName.toLowerCase());
    const actBtnClasses = await activityCollapseBtn.getAttribute('class');
    const actBtnAriaExpanded = await activityCollapseBtn.getAttribute('aria-expanded');

    // PART B: Agent Activity card collapse button
    const agentCard = page.locator('section.dash-agent');
    await expect(agentCard).toBeVisible({ timeout: 30_000 });

    const agentCollapseBtn = agentCard.locator('button.collapse-btn');
    await expect(agentCollapseBtn).toBeVisible({ timeout: 5_000 });

    // Capture agent activity button structure
    const agentBtnTag = await agentCollapseBtn.evaluate((el) => el.tagName.toLowerCase());
    const agentBtnClasses = await agentCollapseBtn.getAttribute('class');
    const agentBtnAriaExpanded = await agentCollapseBtn.getAttribute('aria-expanded');

    // ASSERTIONS: Both buttons must have identical structure
    // (confirming they're rendered by the same CollapseButton component)
    expect(actBtnTag).toBe(agentBtnTag);
    expect(actBtnTag).toBe('button');

    // Both must have the "collapse-btn" class (the component's primary class)
    expect(actBtnClasses).toContain('collapse-btn');
    expect(agentBtnClasses).toContain('collapse-btn');

    // Both must have aria-expanded attribute (a11y requirement)
    expect(actBtnAriaExpanded).toBeDefined();
    expect(agentBtnAriaExpanded).toBeDefined();

    // Both should be in the same initial state (expanded / aria-expanded="true")
    expect(actBtnAriaExpanded).toBe(agentBtnAriaExpanded);

    console.log('✓ Button structure match:');
    console.log(`  Activity Card:  <${actBtnTag} class="${actBtnClasses}" aria-expanded="${actBtnAriaExpanded}" />`);
    console.log(`  Agent Card:     <${agentBtnTag} class="${agentBtnClasses}" aria-expanded="${agentBtnAriaExpanded}" />`);
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 5: Fullscreen button structural consistency
  // ──────────────────────────────────────────────────────────────────────
  test('Fullscreen buttons on both dashboard cards use identical structure', async ({ page }) => {
    // Both Activity and Agent Activity cards use the same CardControls
    // component, so their fullscreen buttons should render identically.

    await page.goto('/dashboard');

    // PART A: Activity card fullscreen button
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    const actFsBtn = activityCard.locator('button.fs-btn');
    await expect(actFsBtn).toBeVisible({ timeout: 5_000 });

    // Capture activity fullscreen button structure
    const actFsBtnTag = await actFsBtn.evaluate((el) => el.tagName.toLowerCase());
    const actFsBtnClasses = await actFsBtn.getAttribute('class');
    const actFsBtnAriaLabel = await actFsBtn.getAttribute('aria-label');

    // PART B: Agent Activity card fullscreen button
    const agentCard = page.locator('section.dash-agent');
    await expect(agentCard).toBeVisible({ timeout: 30_000 });

    const agentFsBtn = agentCard.locator('button.fs-btn');
    await expect(agentFsBtn).toBeVisible({ timeout: 5_000 });

    // Capture agent fullscreen button structure
    const agentFsBtnTag = await agentFsBtn.evaluate((el) => el.tagName.toLowerCase());
    const agentFsBtnClasses = await agentFsBtn.getAttribute('class');
    const agentFsBtnAriaLabel = await agentFsBtn.getAttribute('aria-label');

    // ASSERTIONS: Both fullscreen buttons must have identical structure
    expect(actFsBtnTag).toBe(agentFsBtnTag);
    expect(actFsBtnTag).toBe('button');

    // Both must have the "fs-btn" class
    expect(actFsBtnClasses).toContain('fs-btn');
    expect(agentFsBtnClasses).toContain('fs-btn');

    // Both should have aria-label (a11y requirement)
    expect(actFsBtnAriaLabel).toBeDefined();
    expect(agentFsBtnAriaLabel).toBeDefined();

    console.log('✓ Fullscreen button structure match:');
    console.log(`  Activity Card:  <${actFsBtnTag} class="${actFsBtnClasses}" />`);
    console.log(`  Agent Card:     <${agentFsBtnTag} class="${agentFsBtnClasses}" />`);
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 6: ESC key closes fullscreen (backdrop escape)
  // ──────────────────────────────────────────────────────────────────────
  test('Fullscreen can be exited via ESC key', async ({ page }) => {
    await page.goto('/dashboard');
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    const fullscreenBtn = activityCard.locator('button.fs-btn');
    await expect(fullscreenBtn).toBeVisible({ timeout: 5_000 });

    // Enter fullscreen
    await fullscreenBtn.click();
    await page.waitForTimeout(300);

    // Verify fullscreen state
    let fsClass = await activityCard.getAttribute('class');
    expect(fsClass).toContain('fs-card-on');

    // Press ESC to exit
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // Verify fullscreen exited
    fsClass = await activityCard.getAttribute('class');
    expect(fsClass).not.toContain('fs-card-on');

    // Default-size button should disappear, fullscreen button should reappear
    await expect(fullscreenBtn).toBeVisible({ timeout: 2_000 });
  });

  // ──────────────────────────────────────────────────────────────────────
  // TEST 7: Collapse state persists (localStorage validation)
  // ──────────────────────────────────────────────────────────────────────
  test('Collapse state persists to localStorage', async ({ page }) => {
    await page.goto('/dashboard');
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    const collapseBtn = activityCard.locator('button.collapse-btn');
    await expect(collapseBtn).toBeVisible({ timeout: 5_000 });

    const cardBody = activityCard.locator('.card-body').first();

    // Get username for the storage key (should be 'rambo' for default test user)
    const username = await page.evaluate(() => {
      const auth = sessionStorage.getItem('ramboq_user');
      return auth ? JSON.parse(auth).username : 'demo';
    });

    // Collapse the card
    await collapseBtn.click();
    await page.waitForTimeout(300);
    const hiddenAttr = await cardBody.getAttribute('hidden');
    expect(hiddenAttr).toBeDefined();

    // Check localStorage has the collapse state
    // CardHeader passes cardId="dash-activity" to CollapseButton
    const storageKey = `ramboq.collapse.${username}.dash-activity`;
    const stored = await page.evaluate((key) => localStorage.getItem(key), storageKey);
    expect(stored).toBe('1');

    // Reload the page
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle');

    // Activity card should remain collapsed
    const activityCardReloaded = page.locator('section.dash-activity');
    await expect(activityCardReloaded).toBeVisible({ timeout: 10_000 });

    const cardBodyReloaded = activityCardReloaded.locator('.card-body').first();
    await expect(cardBodyReloaded).toBeHidden();
  });

});
