/**
 * order_book_draft_rows.spec.js
 *
 * 2026-10 draft UX round 2 — draft AlgoOrder rows (mode='draft', status='OPEN')
 * on the Order Book (/orders):
 *   1. A draft never counts toward the "Open" chip (or the "Chase" chip), but
 *      stays visible in the merged list.
 *   2. The draft card renders the DRAFT mode-pill instead of a status badge and
 *      carries data-status="inactive".
 *   3. A draft-only book: Open chip reads 0 yet the draft row is still listed
 *      (default chip still resolves to Open via the raw count).
 *   4. Chase regression: a draft (attempts=0, never placed) never appears in the
 *      Chase chip. The backend /chases/active endpoint excludes mode='draft' too
 *      (asserted at source level below).
 *
 * Mocks GET /api/orders/ (broker book), GET /api/orders/algo/recent and the
 * GTT list; nothing is written to the dev database.
 *
 * LogPanel: its Orders tab is only reachable through ActivityLogModal, which is
 * not a stable entry point from this route, so LogPanel's draft exclusion is
 * asserted at source level (last describe block) rather than in the browser.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(__dir, '..');

const NOW = new Date();
// Naive-UTC created_at, matching the backend's datetime.isoformat() shape.
const CREATED = NOW.toISOString().replace('Z', '');

const REAL_OPEN = {
  id: 1, account: 'T1', symbol: 'RBQ-REALOPEN', exchange: 'NFO', transaction_type: 'BUY',
  quantity: 50, initial_price: 100, current_limit: 100, fill_price: null,
  attempts: 0, status: 'OPEN', engine: 'manual', mode: 'live', detail: null, created_at: CREATED,
};
const DRAFT_OPEN = {
  id: 2, account: 'T1', symbol: 'RBQ-DRAFTROW', exchange: 'NFO', transaction_type: 'BUY',
  quantity: 75, initial_price: 25000, current_limit: 25000, fill_price: null,
  attempts: 0, status: 'OPEN', engine: 'manual', mode: 'draft', detail: null, created_at: CREATED,
};

async function mockOrders(page, algoRows) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: [], count: 0 }) });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(algoRows) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
      return;
    }
    await route.continue();
  });
}

const chip = (page, label) => page.locator('.ob-status-bar .ob-sc',
  { has: page.locator('.ob-sc-l', { hasText: new RegExp(`^${label}$`) }) });
const card = (page, symbol) => page.locator('.oc-book-grid .order-card', { hasText: symbol });

async function gotoOrders(page) {
  await page.goto('/orders', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.waitForSelector('.ob-status-bar', { timeout: 20_000 });
}

test.describe('OrderBook — draft rows never count toward status chips', () => {
  test.setTimeout(90_000);

  test('1+2: draft is excluded from the Open count, stays listed, and shows the DRAFT pill', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrders(page, [REAL_OPEN, DRAFT_OPEN]);
    await gotoOrders(page);

    // Only the real resting order counts toward Open.
    await expect(chip(page, 'Open').locator('.ob-sc-n')).toHaveText('1', { timeout: 15_000 });
    // And never toward Chase (attempts=0 for a draft too).
    await expect(chip(page, 'Chase').locator('.ob-sc-n')).toHaveText('0');

    // The draft row is still present in the merged list.
    const draftCard = card(page, 'RBQ-DRAFTROW');
    await expect(draftCard).toHaveCount(1);
    await expect(card(page, 'RBQ-REALOPEN')).toHaveCount(1);

    // DRAFT pill replaces the status badge; wrapper is inactive, not "running".
    await expect(draftCard.locator('.mode-pill-draft')).toHaveText('DRAFT');
    await expect(draftCard).toHaveAttribute('data-status', 'inactive');
    await expect(draftCard.locator('[data-status="running"]')).toHaveCount(0);
    // The real row keeps its normal status treatment and no DRAFT pill.
    await expect(card(page, 'RBQ-REALOPEN').locator('.mode-pill-draft')).toHaveCount(0);
  });

  test('3: draft-only book — Open chip reads 0 but the draft row is still listed', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrders(page, [DRAFT_OPEN]);
    await gotoOrders(page);

    await expect(chip(page, 'Open').locator('.ob-sc-n')).toHaveText('0', { timeout: 15_000 });
    await expect(chip(page, 'Chase').locator('.ob-sc-n')).toHaveText('0');
    // Default chip still resolves to Open (raw count), so the draft is visible.
    await expect(card(page, 'RBQ-DRAFTROW')).toHaveCount(1);
    await expect(card(page, 'RBQ-DRAFTROW').locator('.mode-pill-draft')).toHaveText('DRAFT');
  });

  test('4: chase regression — a draft never appears under the Chase chip', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrders(page, [DRAFT_OPEN]);
    await gotoOrders(page);

    await expect(chip(page, 'Chase').locator('.ob-sc-n')).toHaveText('0', { timeout: 15_000 });
    // Clicking Chase must not surface the draft.
    await chip(page, 'Chase').click();
    await expect(card(page, 'RBQ-DRAFTROW')).toHaveCount(0);
  });
});

test.describe('Draft-row exclusion — source-level guards', () => {
  test('backend /chases/active excludes mode=draft from the active-chase query', () => {
    const src = readFileSync(join(FRONTEND, '../backend/api/routes/orders.py'), 'utf8');
    expect(src).toMatch(/AlgoOrder\.mode\s*!=\s*"draft"/);
  });

  test('LogPanel excludes drafts from its in-flight predicate and renders the DRAFT pill', () => {
    const src = readFileSync(join(FRONTEND, 'src/lib/LogPanel.svelte'), 'utf8');
    expect(src).toMatch(/import \{ isDraftOrderRow \}/);
    expect(src).toMatch(/if \(isDraftOrderRow\(o\)\) return false;/);
    expect(src).toMatch(/if \(mode === 'draft'\)\s+return DRAFT_PILL;/);
  });

  test('OrderBook counts drafts out of every status chip predicate', () => {
    const src = readFileSync(join(FRONTEND, 'src/lib/OrderBook.svelte'), 'utf8');
    for (const k of ['held', 'open', 'complete', 'rejected_cancelled']) {
      expect(src, `${k} chip count must exclude drafts`)
        .toMatch(new RegExp(`${k}:\\s+_countScopedOrderRows\\.filter\\(o => !isDraftOrderRow\\(o\\)`));
    }
  });
});
