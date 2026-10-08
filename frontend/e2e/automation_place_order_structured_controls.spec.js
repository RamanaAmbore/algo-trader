/**
 * Structured product / chase aggressiveness / Bracket controls for
 * place_order agent actions on the /automation page.
 *
 * Covers:
 *  1. Structured controls appear when the Actions JSON parses to exactly
 *     one place_order action, and stay hidden for other action types.
 *  2. Changing the Product dropdown, Chase aggressiveness picker, or
 *     Bracket dropdown re-serializes the raw-JSON textarea to match.
 *  3. Manually editing the raw textarea with a different place_order
 *     shape updates the structured controls to reflect it.
 *  4. Selecting "None" for Bracket clears template_slug from params.
 *
 * Note: the dropdown's operator-visible label is "Bracket" (display
 * only — the underlying JSON param key stays `template_slug` unchanged).
 *
 * See CLAUDE.md "F&O order qty convention" / chase-aggressiveness thread
 * (backend/api/algo/actions_live.py:_action_place_order) for the backend
 * half of this feature — params.chase_aggressiveness now threads through
 * to `_live_chase_config()`.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const PLACE_ORDER_JSON = JSON.stringify(
  [
    {
      type: 'place_order',
      params: {
        account: 'ZG####',
        symbol: '<tradingsymbol>',
        exchange: 'NFO',
        side: 'BUY',
        qty: 50,
        order_type: 'LIMIT',
        product: 'NRML',
        chase_aggressiveness: 'low',
      },
    },
  ],
  null,
  2
);

const CLOSE_POSITION_JSON = JSON.stringify(
  [
    {
      type: 'close_position',
      params: { account: 'ZG####', symbol: '<tradingsymbol>', exchange: 'NFO', product: 'NRML' },
    },
  ],
  null,
  2
);

/** Open the first agent's inline editor and return the Actions textarea locator.
 *  Agent rows are collapsed by default (role="button" row toggle) — the
 *  Edit button only renders once a row is expanded. */
async function openFirstAgentEditor(/** @type {import('@playwright/test').Page} */ page) {
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle');
  const firstRow = page.locator('[role="button"][aria-expanded]').first();
  await firstRow.waitFor({ state: 'visible', timeout: 15_000 });
  await firstRow.click();
  const editBtn = page.getByRole('button', { name: 'Edit' }).first();
  await editBtn.waitFor({ state: 'visible', timeout: 15_000 });
  await editBtn.click();
  const textarea = page.getByTestId('actions-json-textarea');
  await textarea.waitFor({ state: 'visible', timeout: 10_000 });
  return textarea;
}

test.describe('Automation — structured place_order controls', () => {
  test('appear for a single place_order action, hidden for other action types', async ({ page }) => {
    await loginAsAdmin(page);
    const textarea = await openFirstAgentEditor(page);

    // Non-place_order action → structured controls stay hidden.
    await textarea.fill(CLOSE_POSITION_JSON);
    await expect(page.getByTestId('place-order-struct')).toHaveCount(0);

    // Single place_order action → structured controls appear.
    await textarea.fill(PLACE_ORDER_JSON);
    await expect(page.getByTestId('place-order-struct')).toBeVisible();
  });

  test('control edits re-serialize into the raw-JSON textarea', async ({ page }) => {
    await loginAsAdmin(page);
    const textarea = await openFirstAgentEditor(page);
    await textarea.fill(PLACE_ORDER_JSON);

    const struct = page.getByTestId('place-order-struct');
    await expect(struct).toBeVisible();

    // Product dropdown: NRML → MIS.
    await struct.getByRole('button', { name: 'Product' }).click();
    await struct.getByRole('option', { name: 'MIS', exact: true }).click();
    await expect(textarea).toHaveValue(/"product":\s*"MIS"/);

    // Chase aggressiveness picker: low → high.
    const chaseGroup = struct.locator('[role="group"][aria-label="Chase aggressiveness"]');
    await chaseGroup.getByRole('button', { name: 'H' }).click();
    await expect(textarea).toHaveValue(/"chase_aggressiveness":\s*"high"/);

    // Bracket dropdown: pick "None" explicitly → clears template_slug.
    // The currently-selected option's accessible name carries a CSS
    // ::before checkmark ("✓ None") per Chromium's accname computation,
    // so match by substring (no `exact`), not literal "None".
    await struct.getByRole('button', { name: 'Bracket' }).click();
    await struct.getByRole('option', { name: 'None' }).click();
    await expect(textarea).not.toHaveValue(/template_slug/);
  });

  test('selecting a real template sets template_slug, then None clears it', async ({ page }) => {
    await loginAsAdmin(page);
    const textarea = await openFirstAgentEditor(page);
    await textarea.fill(PLACE_ORDER_JSON);

    const struct = page.getByTestId('place-order-struct');
    await expect(struct).toBeVisible();

    await struct.getByRole('button', { name: 'Bracket' }).click();
    const panel = struct.locator('.rbq-select-panel');
    await panel.waitFor({ state: 'visible' });
    const options = panel.getByRole('option');
    const count = await options.count();
    // At minimum the "None" sentinel option is always present; the system
    // seeds default-bull / default-short-vol templates so count should be
    // >1 in a normal dev environment, but don't hard-fail if the catalog
    // is empty for some reason — only assert the dynamic-selection path
    // when a real (non-None) template exists.
    if (count > 1) {
      const realOption = options.nth(1);
      const label = (await realOption.textContent())?.trim();
      await realOption.click();
      await expect(textarea).toHaveValue(/"template_slug"/);
      // The visible label is the template's name (not slug), but a value
      // was written — confirm it's a non-empty string, not the sentinel.
      await expect(textarea).not.toHaveValue(/"template_slug":\s*"none"/);
      expect(label).toBeTruthy();

      // Now clear it back via "None".
      await struct.getByRole('button', { name: 'Bracket' }).click();
      await struct.getByRole('option', { name: 'None' }).click();
      await expect(textarea).not.toHaveValue(/template_slug/);
    }
  });

  test('manual textarea edit updates the structured controls', async ({ page }) => {
    await loginAsAdmin(page);
    const textarea = await openFirstAgentEditor(page);
    await textarea.fill(PLACE_ORDER_JSON);

    const struct = page.getByTestId('place-order-struct');
    await expect(struct).toBeVisible();

    // Manually type a different place_order shape — MIS product, high chase.
    const manual = JSON.stringify(
      [
        {
          type: 'place_order',
          params: {
            account: 'ZG0790', symbol: 'NIFTY25OCTFUT', exchange: 'NFO',
            side: 'SELL', qty: 25, order_type: 'LIMIT',
            product: 'MIS', chase_aggressiveness: 'high',
          },
        },
      ],
      null,
      2
    );
    await textarea.fill(manual);

    // Product dropdown now reflects MIS.
    await expect(struct.getByRole('button', { name: 'Product' })).toContainText('MIS');
    // Chase H pill is the active one.
    const chaseGroup = struct.locator('[role="group"][aria-label="Chase aggressiveness"]');
    await expect(chaseGroup.getByRole('button', { name: 'H' })).toHaveClass(/on/);
    await expect(chaseGroup.getByRole('button', { name: 'L' })).not.toHaveClass(/on/);
  });
});
