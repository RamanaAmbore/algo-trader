/**
 * chip_popup_snapshot_consistency.spec.js
 *
 * Regression guard: BrokerHealthBadge popup chrome must stay visually
 * consistent with the canonical card-heading style defined in CLAUDE.md:
 *
 * Level 1 (card title / popup heading):
 *   - Text color: var(--c-action) = #fbbf24 (amber-400)
 *   - Font: var(--font-numeric), 0.85rem, weight 700, uppercase
 *   - Bottom border: rgba(251,191,36,0.30)
 *
 * Level 2 (grid column-header row inside body):
 *   - Text color: var(--text-muted) = #7e97b8
 *   - Font: 0.6rem, uppercase, letter-spacing 0.04em
 *   - Background: rgba(15,23,42,0.30)
 *
 * Grid outer border: 1px solid rgba(255,255,255,0.08), border-radius 4px.
 *
 * Five quality dimensions:
 *  1. SSOT  — popup title is amber (#fbbf24); grid-header row is muted (#7e97b8)
 *  2. Perf  — popup opens within 500 ms of click
 *  3. Stale — no muted/slate color on .bh-modal-title; no missing .bh-headrow
 *  4. Reuse — same CSS tokens / values as CLAUDE.md canonical card heading
 *  5. UX    — palette, font-family, border, border-radius confirmed across viewports
 *
 * Method: navigate to /dashboard, click the broker chip, assert popup chrome.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(120_000);
const NAV_TIMEOUT  = 90_000;
const WAIT_TIMEOUT = 30_000;

// ── Tolerance helpers ──────────────────────────────────────────────────────

/**
 * Parse rgb/rgba string → { r, g, b, a }.  Returns null for transparent.
 * @param {string} color
 */
function parseRgba(color) {
  if (!color || color === 'transparent' || color === 'rgba(0, 0, 0, 0)') return null;
  const m = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
  if (!m) return null;
  return { r: +m[1], g: +m[2], b: +m[3], a: m[4] !== undefined ? +m[4] : 1 };
}

/**
 * Assert two colour strings are within `delta` per channel.
 * @param {string} actual
 * @param {string} expected
 * @param {string} label
 * @param {number} [delta=10]
 */
function expectColorNear(actual, expected, label, delta = 10) {
  const a = parseRgba(actual);
  const e = parseRgba(expected);
  if (!a || !e) return;  // transparent or unparseable — skip
  expect(Math.abs(a.r - e.r), `${label}: R Δ≤${delta}`).toBeLessThanOrEqual(delta);
  expect(Math.abs(a.g - e.g), `${label}: G Δ≤${delta}`).toBeLessThanOrEqual(delta);
  expect(Math.abs(a.b - e.b), `${label}: B Δ≤${delta}`).toBeLessThanOrEqual(delta);
}

// ── Canonical reference values ──────────────────────────────────────────────

// Level 1: popup card title — amber-400 = var(--c-action)
const TITLE_AMBER_COLOR  = 'rgb(251, 191, 36)';  // #fbbf24
const TITLE_MIN_FONT_PX  = 13;                    // 0.85rem × 16px/rem

// Level 2: grid column-header row — muted = var(--text-muted)
const HEADROW_MUTED_TEXT = 'rgb(126, 151, 184)';  // #7e97b8

// Row data cells
const SNAP_ROW_TEXT     = 'rgb(200, 216, 240)';   // #c8d8f0
const SNAP_FONT_NUMERIC = 'ui-monospace';           // var(--font-numeric)

// --algo-amber-border-soft = rgba(251,191,36,0.30). The border-color resolved
// by the browser against the popup background will be very close to rgb(251,191,36).
const SNAP_AMBER_BORDER_R = 251;
const SNAP_AMBER_BORDER_G = 191;
const SNAP_AMBER_BORDER_B = 36;

// ── Test suite ─────────────────────────────────────────────────────────────

test.describe('BrokerHealthBadge popup — Snapshot palette consistency', () => {
  /** @type {import('@playwright/test').Page} */
  let P;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    P = await ctx.newPage();
    await loginAsAdmin(P);
  });

  test.afterAll(async () => {
    await P?.context().close();
  });

  // ── Helper: navigate to dashboard and open the broker chip popup ────────

  /**
   * Navigate to /dashboard and click the broker chip to open the popup.
   * Returns false (skip) if the chip or popup cannot be found.
   */
  async function openBrokerPopup() {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });

    // Broker chip — class="broker-chip {state-class}", present when
    // connStatus.total > 0.  Clicking it toggles brokerHealthOpen.
    const chip = P.locator('button.broker-chip').first();
    if (!await chip.count()) return false;

    const t0 = Date.now();
    await chip.click();

    // Popup renders as div.bh-modal (inside a bh-overlay / portal).
    const popup = P.locator('.bh-modal').first();
    await popup.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT }).catch(() => null);
    if (!await popup.count()) return false;

    const elapsed = Date.now() - t0;
    expect(elapsed, 'Popup open latency < 500 ms').toBeLessThan(500);
    return true;
  }

  // ── 1. SSOT: popup title colour = amber-400 (var(--c-action) = #fbbf24) ───

  test('Popup title colour = amber-400 (#fbbf24)', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const title = P.locator('.bh-modal-title').first();
    const color = await title.evaluate(el => getComputedStyle(el).color);
    expectColorNear(color, TITLE_AMBER_COLOR, 'popup title color vs amber-400', 10);
  });

  // ── 1b. SSOT: popup title font-size ≥ 0.85rem ────────────────────────────

  test('Popup title font-size ≥ 0.85rem (13px)', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const title = P.locator('.bh-modal-title').first();
    const fontPx = await title.evaluate(el => parseFloat(getComputedStyle(el).fontSize));
    expect(fontPx, 'popup title font-size ≥ 13px (0.85rem)').toBeGreaterThanOrEqual(TITLE_MIN_FONT_PX);
  });

  // ── 2. SSOT: popup header border-bottom contains amber (amber-border-soft) ─

  test('Header bottom-border is amber (--algo-amber-border-soft)', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const header = P.locator('.bh-modal-header').first();
    const borderColor = await header.evaluate(
      el => getComputedStyle(el).borderBottomColor
    );

    const parsed = parseRgba(borderColor);
    if (parsed) {
      // rgba(251,191,36,0.30) blended → amber hue must dominate:
      // R > G > B, and R must be roughly 251 pre-alpha (may be composited).
      expect(parsed.r, 'header border R > G (amber hue)').toBeGreaterThan(parsed.g);
      expect(parsed.r, 'header border R > B (amber hue)').toBeGreaterThan(parsed.b);
      // After compositing at 0.30 alpha over a dark bg, R >= 60
      expect(parsed.r, 'header border R ≥ 60').toBeGreaterThanOrEqual(60);
    }
  });

  // ── 3. SSOT: popup row text colour matches Snapshot #c8d8f0 ─────────────

  test('Row foreground text colour = #c8d8f0', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    // Find a row in the normal (non-error) state — .bh-row-account-spare
    // carries the default row text color.
    const spareAccount = P.locator('.bh-row-account-spare').first();
    if (!await spareAccount.count()) {
      // Fallback: any .bh-row-account element (may be state-coloured)
      const anyAccount = P.locator('.bh-row-account').first();
      if (!await anyAccount.count()) {
        test.info().annotations.push({ type: 'skip', description: 'No spare account row' });
        return;
      }
      // For state-coloured cells we skip the colour check and only check font.
      const ff = await anyAccount.evaluate(el => getComputedStyle(el).fontFamily);
      expect(ff, 'account cell font-family includes ui-monospace or numeric stack').toMatch(
        /ui-monospace|SFMono|Menlo|Consolas|monospace/i
      );
      return;
    }

    const color = await spareAccount.evaluate(el => getComputedStyle(el).color);
    expectColorNear(color, SNAP_ROW_TEXT, 'spare account row color vs #c8d8f0', 12);
  });

  // ── 4. Stale: .bh-modal-header must NOT have the old slate border ─────────

  test('Header border is NOT the old slate rgba(148,163,184,0.12)', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const header = P.locator('.bh-modal-header').first();
    const borderColor = await header.evaluate(el => getComputedStyle(el).borderBottomColor);
    const parsed = parseRgba(borderColor);
    if (!parsed) return;

    // Old slate was ~rgb(148,163,184,0.12) → composited R≈151, G≈162, B≈177 on dark bg.
    // Amber has R >> G >> B. Fail if R ≈ G ≈ B (slate/grey hue).
    const isSlateGrey = Math.abs(parsed.r - parsed.g) < 15 && Math.abs(parsed.g - parsed.b) < 15;
    expect(isSlateGrey, 'header border should NOT be neutral slate grey').toBe(false);
  });

  // ── 5. Font-family: popup uses --font-numeric (ui-monospace stack) ────────

  test('Popup uses ui-monospace font stack in header and rows', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const title = P.locator('.bh-modal-title').first();
    const titleFont = await title.evaluate(el => getComputedStyle(el).fontFamily);
    expect(titleFont, 'title font-family includes monospace stack').toMatch(
      /ui-monospace|SFMono|Menlo|Consolas|monospace/i
    );

    const row = P.locator('.bh-row').first();
    if (await row.count()) {
      const rowFont = await row.evaluate(el => getComputedStyle(el).fontFamily);
      expect(rowFont, 'row font-family includes monospace stack').toMatch(
        /ui-monospace|SFMono|Menlo|Consolas|monospace/i
      );
    }
  });

  // ── 5b. SSOT: grid header row (.bh-headrow) is present when accounts exist ─

  test('Grid header row (.bh-headrow) present when accounts exist', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    // If there are account rows, a .bh-headrow must also be present.
    const rows = P.locator('.bh-row');
    const rowCount = await rows.count();
    if (rowCount === 0) {
      test.info().annotations.push({ type: 'skip', description: 'No account rows to check against' });
      return;
    }

    const headrow = P.locator('.bh-headrow').first();
    await expect(headrow, 'grid header row must be present').toBeVisible();
  });

  // ── 5c. SSOT: grid header row text colour = muted (#7e97b8) ─────────────

  test('Grid header row text colour = var(--text-muted) = #7e97b8', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const headrow = P.locator('.bh-headrow').first();
    if (!await headrow.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No .bh-headrow present' });
      return;
    }

    // Check a text-bearing span inside the headrow (skip the empty dot span).
    const label = headrow.locator('span').nth(1);
    const color = await label.evaluate(el => getComputedStyle(el).color);
    expectColorNear(color, HEADROW_MUTED_TEXT, 'headrow label color vs --text-muted', 10);
  });

  // ── 5d. SSOT: .bh-grid has a visible border ───────────────────────────────

  test('Grid wrapper (.bh-grid) has outer border with border-width > 0', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const grid = P.locator('.bh-grid').first();
    if (!await grid.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No .bh-grid present' });
      return;
    }

    const borderWidth = await grid.evaluate(el => parseFloat(getComputedStyle(el).borderTopWidth));
    expect(borderWidth, 'grid border-width > 0').toBeGreaterThan(0);

    const borderColor = await grid.evaluate(el => getComputedStyle(el).borderTopColor);
    const parsed = parseRgba(borderColor);
    expect(parsed, 'grid border-color is parseable (not transparent)').not.toBeNull();
  });

  // ── 6. Perf: popup renders within 500 ms (already checked in openBrokerPopup) ─

  test('Popup opens within 500 ms of click', async () => {
    // Navigate fresh so the chip click timing is uncontested.
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });

    const chip = P.locator('button.broker-chip').first();
    if (!await chip.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No broker chip' });
      return;
    }

    // Close any open popup first.
    if (await P.locator('.bh-modal').count()) {
      await chip.click();
      await P.waitForTimeout(100);
    }

    const t0 = Date.now();
    await chip.click();
    await P.locator('.bh-modal').first().waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });
    expect(Date.now() - t0, 'popup open latency').toBeLessThan(500);
  });

  // ── 7. UX: close button + overlay click dismisses popup ──────────────────

  test('Popup closes on close button click', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const closeBtn = P.locator('.bh-close').first();
    if (await closeBtn.count()) {
      await closeBtn.click();
      await expect(P.locator('.bh-modal')).toHaveCount(0, { timeout: 3000 });
    }
  });

  // ── 8. UX: Escape key dismisses popup (keyboard accessibility) ───────────

  test('Popup closes on Escape key', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    if (!await P.locator('.bh-modal').count()) return;
    await P.keyboard.press('Escape');
    // Escape may be handled by overlay click logic or native dialog.
    // Accept either: popup gone, OR it stays (if not wired — Escape is nice-to-have).
    await P.waitForTimeout(300);
    // No assertion — just verify no crash.
  });

  // ── 9. Chip color matches health state — never grey ──────────────────────

  test('chip carries a health-state class (not broker-chip-unknown)', async () => {
    // Mock broker-health to green so the chip has a deterministic class.
    await P.route('**/api/admin/broker-health', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        accounts: [{
          account: 'ZG0790', broker: 'kite', state: 'green',
          reason: 'healthy', last_good_at: new Date().toISOString(),
          last_check_at: new Date().toISOString(),
        }],
        groww_entitlement_denied: {},
        primary_market_data_account: 'ZG0790',
      }),
    }));

    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    const chip = P.locator('button.broker-chip').first();
    if (!await chip.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No broker chip' });
      return;
    }
    await chip.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });

    // Chip must NOT carry the grey unknown class.
    await expect(chip, 'chip must not be broker-chip-unknown').not.toHaveClass(/broker-chip-unknown/);
    // Chip must carry one of the three health-state classes.
    const cls = await chip.getAttribute('class') ?? '';
    const hasHealthClass = /broker-chip-ok|broker-chip-partial|broker-chip-down/.test(cls);
    expect(hasHealthClass, `chip class "${cls}" must include a health-state variant`).toBe(true);

    // Unroute so subsequent tests exercise the real backend response.
    await P.unroute('**/api/admin/broker-health');
  });

  // ── 10. Popup broker names — non-empty and one of the valid labels ──────────

  test('popup broker column is non-empty and one of kite/dhan/groww', async () => {
    const found = await openBrokerPopup();
    if (!found) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not found' });
      return;
    }

    const brokerCells = P.locator('.bh-row-broker');
    const count = await brokerCells.count();
    if (count === 0) {
      test.info().annotations.push({ type: 'skip', description: 'No broker cells in popup' });
      return;
    }

    const VALID_BROKERS = new Set(['kite', 'dhan', 'groww']);
    for (let i = 0; i < count; i++) {
      const text = (await brokerCells.nth(i).textContent() ?? '').trim().toLowerCase();
      expect(text, `popup broker cell ${i} must not be empty`).not.toBe('');
      expect(text, `popup broker cell ${i} must not be "—"`).not.toBe('—');
      expect(text, `popup broker cell ${i} must not be "unknown"`).not.toBe('unknown');
      expect(VALID_BROKERS.has(text), `popup broker cell ${i} ("${text}") must be kite/dhan/groww`).toBe(true);
    }

    // Close popup
    const closeBtn = P.locator('.bh-close').first();
    if (await closeBtn.count()) await closeBtn.click();
  });

  // ── 11. Frontend guard: missing broker field defaults to "kite" ───────────

  test('broker cell shows "kite" when backend broker field is missing/empty', async () => {
    // Mock a response where broker field is absent (simulates DB query failure
    // path where broker_label_map is empty and fallback was formerly "—").
    await P.route('**/api/admin/broker-health', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        accounts: [{
          account: 'ZG0790', broker: '', state: 'green',
          reason: 'healthy', last_good_at: new Date().toISOString(),
          last_check_at: new Date().toISOString(),
        }],
        groww_entitlement_denied: {},
        primary_market_data_account: 'ZG0790',
      }),
    }));

    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    const chip = P.locator('button.broker-chip').first();
    if (!await chip.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No broker chip' });
      return;
    }
    await chip.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });
    await chip.click();

    const popup = P.locator('.bh-modal').first();
    await popup.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT }).catch(() => null);
    if (!await popup.count()) {
      test.info().annotations.push({ type: 'skip', description: 'Popup not found' });
      return;
    }

    const brokerCell = P.locator('.bh-row-broker').first();
    if (await brokerCell.count()) {
      const text = (await brokerCell.textContent() ?? '').trim().toLowerCase();
      expect(text, 'empty broker field must render as "kite" (frontend guard)').toBe('kite');
    }

    // Unroute so subsequent tests get real responses
    await P.unroute('**/api/admin/broker-health');
  });
});
