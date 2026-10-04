/**
 * Lab chat panel — request box above the Lab tabs, calling POST /api/lab/chat.
 *
 * The endpoint is mocked with page.route, so these tests exercise the UI only.
 *
 * Asserts:
 * 1. Textarea and Send are visible for a login with view_lab
 * 2. Send posts the trimmed message as {message}
 * 3. A mocked 200 reply renders in the reply area as plain text
 * 4. A mocked 503 shows its detail text in the inline info banner
 * 5. Send is disabled and "Thinking…" shows while a request is pending
 *
 * Run:
 *   cd frontend && npx playwright test e2e/lab_chat_panel.spec.js --project=chromium-desktop
 *   PLAYWRIGHT_USER=trader npx playwright test e2e/lab_chat_panel.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

const PLACEHOLDER_PREFIX = 'Ask about positions, orders, or research.';

test.describe('Lab chat panel', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin/lab`, { waitUntil: 'networkidle' });

    // view_lab gates the whole page. Wait for either the chat box or the
    // access-denied panel, then skip with a reason if the login lacks the cap.
    const textarea = page.locator(`textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`);
    const denied = page.locator('text=Access denied').first();
    await textarea.or(denied).first().waitFor({ timeout: 10000 });
    if (await denied.isVisible().catch(() => false)) {
      test.skip(
        'Lab page requires view_lab capability (designated, trader, risk, or demo role). ' +
        'Run with PLAYWRIGHT_USER=<trader-or-designated-user> to test the chat panel.'
      );
    }
  });

  test('textarea and Send are visible', async ({ page }) => {
    const textarea = page.locator(`textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`);
    await expect(textarea).toBeVisible();
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
  });

  test('Send posts the message and renders the 200 reply as text', async ({ page }) => {
    let postedBody = null;
    await page.route('**/api/lab/chat', async (route) => {
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

    await page.locator(`textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`).fill('  what is my P&L?  ');
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
    await page.route('**/api/lab/chat', (route) =>
      route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail }),
      })
    );

    await page.locator(`textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`).fill('hello');
    await page.getByRole('button', { name: 'Send', exact: true }).click();

    await expect(page.locator('.lab-chat-info')).toContainText(detail);
    await expect(page.locator('.lab-chat-reply')).toHaveCount(0);
  });

  test('Send is disabled and Thinking shows while pending', async ({ page }) => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    await page.route('**/api/lab/chat', async (route) => {
      await gate;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ reply: 'done', duration_ms: 1 }),
      });
    });

    const send = page.getByRole('button', { name: 'Send', exact: true });
    await page.locator(`textarea[placeholder^="${PLACEHOLDER_PREFIX}"]`).fill('slow question');
    await send.click();

    await expect(page.locator('.lab-chat-thinking')).toHaveText('Thinking…');
    await expect(send).toBeDisabled();

    release();
    await expect(page.locator('.lab-chat-reply')).toContainText('done');
    await expect(send).toBeEnabled();
    await expect(page.locator('.lab-chat-thinking')).toHaveCount(0);
  });
});
