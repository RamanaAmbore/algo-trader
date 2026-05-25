// Verify the /pulse symbol-add UI is collapsed into a popup triggered
// by a 🔍 button in the header. No inline symbol input visible at rest.
import { test, expect } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('pulse search button opens a popup; inline form is gone', async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // (a) Search popup is NOT visible by default.
  const overlay = page.locator('.search-overlay');
  await expect(overlay).toHaveCount(0);

  // (b) No inline Symbol input present at rest (the old Row 2 form).
  const inlineSym = page.locator('input[placeholder="Symbol"]');
  await expect(inlineSym).toHaveCount(0);

  // (c) The header search trigger button is visible + clickable.
  const trigger = page.getByRole('button', { name: /search.*add symbol/i });
  await expect(trigger).toBeVisible();

  await page.screenshot({ path: 'test-results/pulse-search-collapsed.png', clip: { x: 0, y: 60, width: 1440, height: 220 } });

  // (d) Click → popup appears with the new (≥ 3 chars) Symbol input.
  await trigger.click();
  await page.waitForTimeout(250);
  await expect(overlay).toBeVisible();
  const popupInput = page.locator('.search-modal input.field-input');
  await expect(popupInput).toBeVisible();
  await expect(popupInput).toBeFocused();

  // (e) Type a query — typeahead renders inside the popup.
  await popupInput.fill('NIF');
  await page.waitForTimeout(700);
  const typeahead = page.locator('.search-typeahead');
  await expect(typeahead).toBeVisible();
  await page.screenshot({ path: 'test-results/pulse-search-open.png' });

  // (f) Esc closes the popup.
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  // (typeahead closes first on first Esc — press once more for popup itself)
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  await expect(overlay).toHaveCount(0);

  // (g) `/` shortcut reopens the popup.
  await page.keyboard.press('/');
  await page.waitForTimeout(250);
  await expect(overlay).toBeVisible();
});
