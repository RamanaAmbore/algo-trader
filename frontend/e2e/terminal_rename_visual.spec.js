/**
 * Terminal rename visual verification.
 *
 * Verifies that the "Rambo Terminal" public-surface rename and the
 * algo-side "Console" label are correctly deployed on dev.ramboq.com.
 * Two viewport projects: chromium-desktop (1400×900) and mobile-portrait
 * (360×800).
 *
 * Checks:
 *  1. Public home `/` Y-fork right card — tag / h2 / CTA say "Rambo Terminal"
 *     and left card is unchanged (Investment Partnership / Partner with us).
 *  2. Public navbar desktop pill reads "Terminal ↗"
 *  3. Public navbar mobile hamburger tray contains "Terminal ↗"
 *  4. Algo navbar (signed in) — "Console" button navigates to /automation's
 *     Console tab (2026-10, Phase 2 Sprint B — /console retired as its own
 *     route; nav entry repointed, old bookmarks still redirect there)
 *  5. Algo navbar (signed in) mobile hamburger — "Console" entry visible
 *  6. Old /console bookmark redirects to /automation and lands on the
 *     Console tab
 *  7. /showcase page — h1 = "Rambo Terminal — Tour" + title contains same string
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     PLAYWRIGHT_USER=ambore PLAYWRIGHT_PASS=<pass> \
 *     npx playwright test e2e/terminal_rename_visual.spec.js \
 *     --project=chromium-desktop --workers=1
 *
 * When PLAYWRIGHT_PASS is absent the fixture falls back to the default
 * rambo/admin1234 credentials. ambore requires an explicit PLAYWRIGHT_PASS.
 *
 * DOM notes (from reading the Svelte source):
 *  - (public)/+layout.svelte: desktop nav uses `<button class="pub-nav-algo-btn">Terminal ↗</button>`
 *    inside `hidden md:flex` container. Mobile tray (`{#if menuOpen}`) renders
 *    `<button class="pub-mobile-item pub-mobile-algo">Terminal ↗</button>`.
 *  - (algo)/+layout.svelte: nav links are ALL `<button class="algo-nav-btn">` (not <a> tags).
 *    Mobile tray renders `<button class="algo-mobile-item">` for each link.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT  = 25_000;
const SHOT_DIR = '/tmp';

// When PLAYWRIGHT_USER=ambore but PLAYWRIGHT_PASS is absent, fall back to the
// default rambo/admin1234 credentials so algo-side checks still run.
// Both rambo (admin) and ambore (designated) have access to /console and /showcase.
const AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const AUTH_PASS = process.env.PLAYWRIGHT_PASS
  || (AUTH_USER !== 'rambo' ? null : 'admin1234');

/**
 * Log into the algo site. If AUTH_PASS is null (ambore requested but no
 * PLAYWRIGHT_PASS provided) skip the test rather than timing out 3× through
 * the auth retry loop.
 */
async function loginForAlgo(page) {
  if (!AUTH_PASS) {
    test.skip(true, `PLAYWRIGHT_PASS not set for user "${AUTH_USER}" — supply it to run algo checks`);
    return;
  }
  // When AUTH_USER is not 'ambore' (i.e. we fell back to rambo) pass undefined
  // so loginAsAdmin uses its own env-var defaults (rambo / admin1234).
  await loginAsAdmin(page, {
    user: AUTH_USER === 'ambore' ? AUTH_USER : undefined,
    pass: AUTH_PASS,
  });
}

// ── helpers ──────────────────────────────────────────────────────────────────

/** Navigate to the public home on the PLAYWRIGHT_BASE_URL target. */
async function gotoHome(page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  // Wait for the Y-fork section to hydrate.
  await page.locator('.fork-card, .fork-tag, h2').first().waitFor({ state: 'visible', timeout: TIMEOUT });
}

// ── public checks (anonymous) ─────────────────────────────────────────────────

test.describe('Public home / — Y-fork cards', () => {

  test('right card: tag = "Rambo Terminal"', async ({ page }) => {
    await gotoHome(page);
    // The right fork card carries .fork-tag-alt per the source.
    const tag = page.locator('.fork-tag-alt').first();
    await expect(tag).toBeVisible({ timeout: TIMEOUT });
    const text = (await tag.textContent() || '').trim();
    expect(text, `fork-tag-alt text was: "${text}"`).toBe('Rambo Terminal');
  });

  test('right card: h2 = "Explore Rambo Terminal"', async ({ page }) => {
    await gotoHome(page);
    const h2 = page.getByRole('heading', { name: 'Explore Rambo Terminal' }).first();
    await expect(h2).toBeVisible({ timeout: TIMEOUT });
  });

  test('right card: CTA = "Open Rambo Terminal →"', async ({ page }) => {
    await gotoHome(page);
    const cta = page.getByText('Open Rambo Terminal →', { exact: true }).first();
    await expect(cta).toBeVisible({ timeout: TIMEOUT });
  });

  test('left card: tag = "Investment Partnership" (unchanged)', async ({ page }) => {
    await gotoHome(page);
    // Left card uses .fork-tag WITHOUT .fork-tag-alt.
    // Must use CSS :not() — Playwright's locator().not() is not a method.
    const tag = page.locator('.fork-tag:not(.fork-tag-alt)').first();
    await expect(tag).toBeVisible({ timeout: TIMEOUT });
    const text = (await tag.textContent() || '').trim();
    expect(text, `left fork-tag text was: "${text}"`).toBe('Investment Partnership');
  });

  test('left card: h2 = "Partner with us" (unchanged)', async ({ page }) => {
    await gotoHome(page);
    const h2 = page.getByRole('heading', { name: 'Partner with us' }).first();
    await expect(h2).toBeVisible({ timeout: TIMEOUT });
  });

  test('left card: CTA = "See partnership →" (unchanged)', async ({ page }) => {
    await gotoHome(page);
    const cta = page.getByText('See partnership →', { exact: true }).first();
    await expect(cta).toBeVisible({ timeout: TIMEOUT });
  });
});

// ── public navbar ─────────────────────────────────────────────────────────────

test.describe('Public navbar — Terminal ↗ pill', () => {

  test('desktop: navbar pill reads "Terminal ↗" (not Platform/Platform Demo)', async ({ page }) => {
    const width = page.viewportSize()?.width ?? 1400;
    if (width <= 800) test.skip(true, 'desktop-only check — mobile tray tested separately');

    await gotoHome(page);
    // Desktop nav renders: <button class="pub-nav-algo-btn">Terminal ↗</button>
    // inside a `hidden md:flex` container — visible at desktop widths.
    const pill = page.locator('.pub-nav-algo-btn').first();
    await expect(pill).toBeVisible({ timeout: TIMEOUT });
    const text = (await pill.textContent() || '').trim();
    expect(text, `navbar pill text was: "${text}"`).toMatch(/Terminal\s*↗/);
    expect(text, 'should NOT contain Platform').not.toMatch(/Platform/i);

    // Screenshot for the report (laptop public home).
    await page.screenshot({ path: `${SHOT_DIR}/terminal-rename-laptop-home.png`, fullPage: true });
  });

  test('mobile: hamburger tray contains "Terminal ↗" (not Platform/Platform Demo)', async ({ page }) => {
    const width = page.viewportSize()?.width ?? 1400;
    if (width > 800) test.skip(true, 'mobile-only check — desktop pill tested separately');

    await gotoHome(page);
    // Mobile bar is inside `md:hidden` section. The hamburger is .pub-hamburger.
    const hamburger = page.locator('.pub-hamburger').first();
    await expect(hamburger).toBeVisible({ timeout: TIMEOUT });
    await hamburger.click();

    // After click, `{#if menuOpen}` renders `.pub-mobile-dropdown` which contains
    // `<button class="pub-mobile-item pub-mobile-algo">Terminal ↗</button>`.
    const trayEntry = page.locator('.pub-mobile-algo').first();
    await expect(trayEntry).toBeVisible({ timeout: TIMEOUT });
    const text = (await trayEntry.textContent() || '').trim();
    expect(text, `mobile tray entry was: "${text}"`).toMatch(/Terminal\s*↗/);
    expect(text, 'should NOT contain Platform').not.toMatch(/Platform/i);

    // Screenshot with tray open.
    await page.screenshot({ path: `${SHOT_DIR}/terminal-rename-mobile-home-hamburger.png`, fullPage: false });
  });
});

// ── algo navbar (authenticated) ───────────────────────────────────────────────

test.describe('Algo navbar — Console entry removed (2026-10-13)', () => {

  test.beforeEach(async ({ page }) => {
    await loginForAlgo(page);
  });

  // The separate top-level "Console" nav entry (which used to point at
  // /automation?tab=console) was removed per operator request — Console
  // is reachable as a tab inside the Automation nav entry, so a second
  // link to the same page was redundant. These tests replace the old
  // "Console nav button exists" checks with the inverse: it's gone, on
  // both desktop and mobile, and "Terminal" (the pre-rename label) was
  // never reintroduced either.

  test('desktop: navbar has no "Console" button (removed) and no "Terminal" (old name)', async ({ page }) => {
    const width = page.viewportSize()?.width ?? 1400;
    if (width <= 800) test.skip(true, 'desktop-only check');

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForURL(/dashboard/, { timeout: TIMEOUT });

    await page.locator('.algo-nav-btn').first().waitFor({ state: 'visible', timeout: TIMEOUT });

    const navBtns = page.locator('.algo-nav-btn');
    const labels = await navBtns.allTextContents();
    const consoleIdx = labels.findIndex(t => t.trim() === 'Console');
    expect(consoleIdx, `"Console" nav button should be removed; got: ${JSON.stringify(labels)}`).toBe(-1);

    const terminalIdx = labels.findIndex(t => t.trim() === 'Terminal');
    expect(terminalIdx, `Nav button "Terminal" still present — should not exist`).toBe(-1);

    // Automation's own nav entry still exists — Console remains
    // reachable as a tab inside it.
    const automationIdx = labels.findIndex(t => t.trim() === 'Automation');
    expect(automationIdx, `Expected an "Automation" nav button; got: ${JSON.stringify(labels)}`).toBeGreaterThanOrEqual(0);
  });

  test('mobile: hamburger tray has no "Console" entry (removed) and no "Terminal" (old name)', async ({ page }) => {
    const width = page.viewportSize()?.width ?? 1400;
    if (width > 800) test.skip(true, 'mobile-only check');

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForURL(/dashboard/, { timeout: TIMEOUT });

    const hamburger = page.locator('.algo-hamburger').first();
    await expect(hamburger).toBeVisible({ timeout: 10_000 });
    await hamburger.click();

    await page.locator('.algo-mobile-dropdown').first().waitFor({ state: 'visible', timeout: 8000 });

    const trayBtns = page.locator('.algo-mobile-item');
    const labels = await trayBtns.allTextContents();
    const consoleIdx = labels.findIndex(t => t.trim() === 'Console');
    expect(consoleIdx, `"Console" tray entry should be removed; got: ${JSON.stringify(labels)}`).toBe(-1);

    const terminalIdx = labels.findIndex(t => t.trim() === 'Terminal');
    expect(terminalIdx, `Tray entry "Terminal" still present — should not exist`).toBe(-1);
  });
});

// ── /console → /automation Console tab (retired 2026-10, Phase 2 Sprint B) ─────

test.describe('/console redirect lands on the Automation Console tab', () => {

  test.beforeEach(async ({ page }) => {
    await loginForAlgo(page);
  });

  // /console is no longer its own page (no own <svelte:head> title / h1) —
  // it's a tab on /automation now, so the bookmark/redirect check asserts
  // the AUTOMATION page's own title/h1, plus that the Console tab content
  // (the CLI textarea) actually rendered — not a blank Agents-tab fallback.
  test('old /console bookmark redirects to /automation and lands on the Console tab', async ({ page }) => {
    await page.goto('/console', { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveURL(/\/automation\?tab=console/, { timeout: TIMEOUT });
    await expect(page).toHaveTitle('Automation | RamboQuant Analytics', { timeout: TIMEOUT });
    const h1 = page.locator('h1.page-title-chip, h1').first();
    await expect(h1).toBeVisible({ timeout: TIMEOUT });
    expect((await h1.textContent() || '').trim()).toContain('Automation');
    await expect(page.locator('.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
  });
});

// ── /showcase page ────────────────────────────────────────────────────────────

test.describe('/showcase page', () => {

  test.beforeEach(async ({ page }) => {
    await loginForAlgo(page);
  });

  test('h1 = "Rambo Terminal — Tour"', async ({ page }) => {
    await page.goto('/showcase', { waitUntil: 'domcontentloaded' });
    const h1 = page.locator('h1.show-title, h1').first();
    await expect(h1).toBeVisible({ timeout: TIMEOUT });
    const text = (await h1.textContent() || '').trim();
    expect(text, `h1 was: "${text}"`).toBe('Rambo Terminal — Tour');
  });

  test('browser tab title contains "Rambo Terminal — Tour"', async ({ page }) => {
    await page.goto('/showcase', { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveTitle(/Rambo Terminal — Tour/i, { timeout: TIMEOUT });
  });
});
