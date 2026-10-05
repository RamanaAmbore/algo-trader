/**
 * Lab chat panel — request box above the Lab tabs, calling POST /api/research/chat.
 *
 * The panel is gated by the `use_research_chat` capability (designated only).
 * The endpoint is mocked with page.route, so these tests exercise the UI only.
 *
 * Login selection:
 * - Default login (PLAYWRIGHT_USER, admin in local/dev setups): must NOT see
 *   the panel. Skips with a reason if the login cannot reach the Lab page.
 * - Designated login (PLAYWRIGHT_DESIGNATED_USER + PLAYWRIGHT_DESIGNATED_PASS):
 *   must see the panel. Every functional test below runs as this login and
 *   skips with a reason when those env vars are not set.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_DESIGNATED_USER=<user> PLAYWRIGHT_DESIGNATED_PASS=<pass> \
 *     npx playwright test e2e/lab_chat_panel.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

const PLACEHOLDER_PREFIX = 'Ask about positions, orders, or research.';
const CHAT_TEXTAREA = `textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`;

const DESIGNATED_USER = process.env.PLAYWRIGHT_DESIGNATED_USER || '';
const DESIGNATED_PASS = process.env.PLAYWRIGHT_DESIGNATED_PASS || '';
const HAS_DESIGNATED = Boolean(DESIGNATED_USER && DESIGNATED_PASS);
const NO_DESIGNATED_REASON =
  'No designated login available. Set PLAYWRIGHT_DESIGNATED_USER and ' +
  'PLAYWRIGHT_DESIGNATED_PASS to run the chat panel tests.';

/**
 * Open /admin/mcp and wait for either the chat box or the access-denied panel.
 * Returns true when the Lab page content is reachable for this login.
 */
async function openLab(page) {
  await page.goto(`${BASE}/admin/mcp`, { waitUntil: 'networkidle' });
  const chat = page.locator(CHAT_TEXTAREA);
  const denied = page.locator('text=Access denied').first();
  await chat.or(denied).first().waitFor({ timeout: 10000 }).catch(() => {});
  return !(await denied.isVisible().catch(() => false));
}

test.describe('Lab chat panel — visibility by capability', () => {
  test('panel is absent for a non-designated login', async ({ page }) => {
    await loginAsAdmin(page);
    const reachable = await openLab(page);
    if (!reachable) {
      // Access denied: the panel cannot be present. Still assert absence.
      await expect(page.locator(CHAT_TEXTAREA)).toHaveCount(0);
      return;
    }
    await expect(page.locator(CHAT_TEXTAREA)).toHaveCount(0);
    await expect(page.locator('section[aria-label="MCP chat"]')).toHaveCount(0);
  });

  test('panel is present for a designated login', async ({ page }) => {
    test.skip(!HAS_DESIGNATED, NO_DESIGNATED_REASON);
    await loginAsAdmin(page, { user: DESIGNATED_USER, pass: DESIGNATED_PASS });
    const reachable = await openLab(page);
    test.skip(!reachable, 'Designated login cannot reach /admin/mcp (view_research missing).');
    await expect(page.locator(CHAT_TEXTAREA)).toBeVisible();
  });
});

test.describe('Lab chat panel — behaviour (designated login)', () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!HAS_DESIGNATED, NO_DESIGNATED_REASON);
    await loginAsAdmin(page, { user: DESIGNATED_USER, pass: DESIGNATED_PASS });
    const reachable = await openLab(page);
    test.skip(!reachable, 'Designated login cannot reach /admin/mcp (view_research missing).');
  });

  test('textarea and Send are visible', async ({ page }) => {
    await expect(page.locator(CHAT_TEXTAREA)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
  });

  test('Send posts the message and renders the 200 reply as text', async ({ page }) => {
    let postedBody = null;
    await page.route('**/api/research/chat', async (route) => {
      postedBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          reply: 'Line one\n<b>not bold</b>',
          duration_ms: 420,
        }),
      });
    });

    await page.locator(CHAT_TEXTAREA).fill('  what is my P&L?  ');
    await page.getByRole('button', { name: 'Send', exact: true }).click();

    const reply = page.locator('.lab-chat-reply');
    await expect(reply).toBeVisible();
    await expect(reply).toContainText('Line one');
    // Reply is plain text: the HTML tag must appear literally, not render as markup.
    await expect(reply).toContainText('<b>not bold</b>');
    await expect(reply.locator('b')).toHaveCount(0);

    expect(postedBody).toEqual({ message: 'what is my P&L?' });
  });

  test('a mocked 503 shows its detail text', async ({ page }) => {
    const detail = 'Claude is not configured on this server.';
    await page.route('**/api/research/chat', (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail }),
      })
    );

    await page.locator(CHAT_TEXTAREA).fill('hello');
    await page.getByRole('button', { name: 'Send', exact: true }).click();

    await expect(page.locator('.lab-chat-info')).toContainText(detail);
    await expect(page.locator('.lab-chat-reply')).toHaveCount(0);
  });

  test('Send is disabled and Thinking shows while pending', async ({ page }) => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/research/chat', async (route) => {
      await gate;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ reply: 'done', duration_ms: 1 }),
      });
    });

    const send = page.getByRole('button', { name: 'Send', exact: true });
    await page.locator(CHAT_TEXTAREA).fill('slow question');
    await send.click();

    await expect(page.locator('.lab-chat-thinking')).toHaveText('Thinking…');
    await expect(send).toBeDisabled();

    release();
    await expect(page.locator('.lab-chat-reply')).toContainText('done');
    await expect(send).toBeEnabled();
    await expect(page.locator('.lab-chat-thinking')).toHaveCount(0);
  });
});
