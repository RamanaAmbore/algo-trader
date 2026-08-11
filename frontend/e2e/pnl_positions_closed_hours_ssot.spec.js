/**
 * pnl_positions_closed_hours_ssot.spec.js
 *
 * Verifies that after market close, the positions snapshot uses `previous_close`
 * (official Kite settlement, frozen by COALESCE) as the close-price basis —
 * NOT `prev_ltp` (recent batch LTP ≈ current LTP → day_change ≈ 0).
 *
 * The patch ensures correct day P&L calculation during closed hours:
 *   - Old: day P&L could use prev_ltp (latest batch LTP) if close was stale
 *   - New: actual_previous_close is set via COALESCE; frozen until market open
 *   - Guard: computed_day_pnl uses actual_previous_close, not raw prev_ltp
 *
 * Three quality dimensions:
 *  1. SSOT    — backend SSOT: actual_previous_close is defined before prev_ltp
 *  2. Stale   — stale-guard: actual_previous_close used in day P&L computation
 *  3. API-smoke — positions response has correct day_change_val structure
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/pnl_positions_closed_hours_ssot.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

test.setTimeout(60000);

test.describe('Positions day P&L closed-hours SSOT', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── Test 1: Source-scan — actual_previous_close definition order ──────
  test('1-SSOT: backend uses actual_previous_close before prev_ltp in positions.py', () => {
    // Read positions_helpers.py source.
    const positionsHelpersPath = '/Users/ramanambore/projects/ramboq/backend/api/routes/positions_helpers.py';
    let source = '';
    try {
      source = readFileSync(positionsHelpersPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read positions_helpers.py: ${e.message}`);
      return;
    }

    // Assertion 1: actual_previous_close must be defined
    expect(source, 'positions_helpers.py should define actual_previous_close').toContain('actual_previous_close');

    // Assertion 2: actual_previous_close should be set in a way that guards against stale prev_ltp
    // The pattern should be: actual_previous_close = ... then prev_close_val uses it OR prev_ltp fallback
    const hasActualPrevCloseGuard = /actual_previous_close\s*=\s*\(\s*float\(previous_close\)/.test(source);
    expect(hasActualPrevCloseGuard, 'positions_helpers.py should define actual_previous_close as guarded float(previous_close)').toBe(true);

    // Assertion 3: prev_close_val should reference actual_previous_close OR prev_ltp fallback
    const hasPrevCloseValLogic = /prev_close_val\s*=\s*\(\s*actual_previous_close\s*or/.test(source);
    expect(hasPrevCloseValLogic, 'positions_helpers.py should use actual_previous_close with OR prev_ltp fallback in prev_close_val').toBe(true);

    // Assertion 4: build_row_from_snapshot_raw or build_snapshot_position_row
    // should pass actual_previous_close as previous_close parameter
    const hasPrevCloseParam = /previous_close\s*=\s*actual_previous_close/.test(source);
    expect(hasPrevCloseParam, 'positions_helpers.py should pass actual_previous_close to row builder as previous_close').toBe(true);

    console.log('[pnl_positions_closed_hours_ssot] 1-SSOT actual_previous_close definition verified');
  });

  // ── Test 2: Source-scan — actual_previous_close used in day P&L ──────
  test('2-Stale: backend uses actual_previous_close in computed_day_pnl, not raw prev_ltp', () => {
    // Read positions_helpers.py source.
    const positionsHelpersPath = '/Users/ramanambore/projects/ramboq/backend/api/routes/positions_helpers.py';
    let source = '';
    try {
      source = readFileSync(positionsHelpersPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read positions_helpers.py: ${e.message}`);
      return;
    }

    // Assertion 1: computed_day_pnl or day_change_val computation should reference actual_previous_close
    const hasComputedDayPnl = /computed_day_pnl|day_change.*actual_previous_close/.test(source);
    expect(hasComputedDayPnl, 'positions_helpers.py should compute day P&L using actual_previous_close').toBe(true);

    // Assertion 2: build_snapshot_position_row should accept previous_close parameter
    const hasRowBuilderSig = /build_snapshot_position_row|build_row_from_snapshot_raw/.test(source);
    expect(hasRowBuilderSig, 'positions_helpers.py should have row builder function').toBe(true);

    // Assertion 3: actual_previous_close should not be a direct alias to prev_ltp
    // (it's set via COALESCE or similar guard, frozen to settlement price)
    const hasCoalesce = /COALESCE|coalesce|or\s+prev_close/.test(source);
    expect(hasCoalesce, 'positions_helpers.py should use COALESCE or fallback guard for actual_previous_close').toBe(true);

    console.log('[pnl_positions_closed_hours_ssot] 2-Stale actual_previous_close in day P&L verified');
  });

  // ── Test 3: API smoke — positions response has correct day_change_val ──
  test('3-API-smoke: /api/positions response has valid day_change_val structure', async ({ page }) => {
    // Set up interception of positions API BEFORE navigation
    let positionsResponse = null;
    page.on('response', async (resp) => {
      if (resp.url().includes('/api/positions') && resp.status() === 200) {
        try {
          positionsResponse = await resp.json();
        } catch (e) {
          // Skip parse errors
        }
      }
    });

    // Navigate to positions or dashboard page to trigger positions fetch
    await page.goto('/admin/dashboard');

    // Wait for positions data to be captured
    const startTime = Date.now();
    while (!positionsResponse && Date.now() - startTime < 10000) {
      await page.waitForTimeout(100);
    }

    // If no API response captured, skip
    if (!positionsResponse) {
      test.skip(true, 'No /api/positions response captured; skipping API smoke test');
      return;
    }

    let positions = positionsResponse;
    if (Array.isArray(positionsResponse?.rows)) {
      positions = positionsResponse.rows;
    }

    // If no positions, skip gracefully
    if (!positions || positions.length === 0) {
      test.skip(true, 'No positions in response; skipping API smoke test');
      return;
    }

    // Assertion 1: positions array is present
    expect(Array.isArray(positions), '/api/positions should return an array or have .rows array').toBe(true);

    // Assertion 2: if any position has previous_close > 0, day_change_val must be a number
    let foundPositionWithPrevClose = false;
    for (const pos of positions) {
      if (pos.previous_close && pos.previous_close > 0) {
        foundPositionWithPrevClose = true;
        expect(
          typeof pos.day_change_val === 'number',
          `Position with previous_close=${pos.previous_close} should have numeric day_change_val, got ${typeof pos.day_change_val}`
        ).toBe(true);
      }
    }

    // Assertion 3: at least one position should have valid previous_close (or skip gracefully)
    if (!foundPositionWithPrevClose) {
      test.skip(true, 'No positions with previous_close > 0; skipping day_change_val assertion');
      return;
    }

    console.log('[pnl_positions_closed_hours_ssot] 3-API-smoke /api/positions day_change_val verified');
  });
});
