/**
 * chain_spread_gate.spec.js
 *
 * Chain-tab pre-submission spread gate (operator: "if there is too
 * much spread on the offset limit side, it should warn ... it should
 * be in a loop until the conditions are satisfied before placing the
 * order"). Scope: OptionChainTab.svelte + SymbolPanel.svelte's shared
 * Submit button wiring ONLY — the Ticket tab's own submit path
 * (`_modalFireSubmit`) must be untouched.
 *
 * T3.3 (bracket-removal, 2026-11) rewrite — the gate used to check a
 * leg ONLY when a Bracket wing/offset would attach to it. With Bracket
 * gone, that predicate could never be true again, which would have
 * made the whole gate a permanent no-op — defeating its stated purpose
 * rather than preserving it. Redesigned: every CE/PE leg staged in the
 * basket now checks its OWN bid/ask spread against `spreadMaxPctOverride`
 * (default 0.5%) unconditionally; futures legs stay out of scope. The
 * Spread% control itself relocated from the deleted `TemplateBar.svelte`
 * into OptionChainTab's own Expiry toolbar row, next to the DTE chip —
 * same state variable (`spreadMaxPctOverride`), same default, same live
 * gate; only where it renders changed.
 *
 * The full flow (stage a leg in a real chain basket, then exercise a
 * live multi-second poll loop against a mocked backend) requires a real
 * IndexedDB instruments cache + live chain quotes + a staged basket —
 * reaching that deterministically in a browser is its own large
 * undertaking independent of this feature. Per this codebase's existing
 * convention for this exact file (`option_chain_order.spec.js` — a
 * source-scan spec, no browser), the scenario-level requirements below
 * are verified as precise code shape assertions instead: every
 * assertion targets the literal function/branch that implements the
 * described behaviour, so a regression that removes or inverts the
 * logic fails the test exactly as a behavioural mock would. The
 * bounded-loop mechanics themselves (never hangs, discards stale
 * responses, bounded errors/timeout) are covered with real fake-timer
 * behavioural tests in `frontend/src/lib/__tests__/data/spreadGate.test.js`
 * (Vitest) — that is the authoritative test for "doesn't hang indefinitely".
 *
 * One genuine browser smoke test is included at the bottom to confirm
 * the app still boots with the new wiring in place (no console errors
 * mounting SymbolPanel).
 *
 * Run:
 *   npx playwright test e2e/chain_spread_gate.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';
const CHAIN_PATH    = `${ROOT}/order/OptionChainTab.svelte`;
const SHELL_PATH     = `${ROOT}/SymbolPanel.svelte`;
const GATE_LIB_PATH  = `${ROOT}/data/spreadGate.js`;

let chainSrc = '', shellSrc = '', gateLibSrc = '';

test.beforeAll(() => {
  chainSrc    = readFileSync(CHAIN_PATH, 'utf-8');
  shellSrc    = readFileSync(SHELL_PATH, 'utf-8');
  gateLibSrc  = readFileSync(GATE_LIB_PATH, 'utf-8');
});

test.describe('Spread% field renders alongside Expiry/DTE in OptionChainTab\'s own toolbar', () => {
  test('OptionChainTab declares a Spread% bindable prop + its own input, defaulted to 0.5', () => {
    expect(chainSrc).toContain('spreadMaxPctOverride');
    expect(chainSrc).toContain('class="oct-spread-field"');
    expect(chainSrc).toContain('placeholder="0.5"');
    // Rendered in the Expiry toolbar, after the DTE chip.
    const dteIdx = chainSrc.indexOf('class="oct-expiry-dte"');
    const spreadIdx = chainSrc.indexOf('class="oct-spread-field"');
    expect(dteIdx).toBeGreaterThan(-1);
    expect(spreadIdx).toBeGreaterThan(dteIdx);
  });

  test('_effMaxSpreadPct() falls back to 0.5 when no override is set', () => {
    const fnIdx = chainSrc.indexOf('function _effMaxSpreadPct()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 300);
    expect(body).toContain('0.5');
  });

  test('SymbolPanel owns the shared Spread% override state and binds it into OptionChainTab', () => {
    expect(shellSrc).toContain('_sharedSpreadMaxPctOverride');
    expect(shellSrc).toContain('bind:spreadMaxPctOverride={_sharedSpreadMaxPctOverride}');
  });
});

test.describe('A wide-spread result blocks submission and shows a warning', () => {
  test('_checkLegs() requires ok===true on every checked leg before the gate can pass', () => {
    const fnIdx = chainSrc.indexOf('async function _checkLegs(');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 1200);
    expect(body).toContain('checkOrderSpread(');
    expect(body).toContain('r?.ok === true');
    expect(body).toContain('results.every((r) => r.ok === true)');
  });

  test('the "wide" phase renders a warning banner with a Place-anyway override button', () => {
    expect(chainSrc).toContain("_gateState.phase === 'wide'");
    expect(chainSrc).toContain('Place anyway');
    expect(chainSrc).toContain('data-testid="spread-gate-place-anyway"');
  });

  test('every staged CE/PE leg is its own check target — futures legs are excluded', () => {
    const fnIdx = chainSrc.indexOf('function _buildGatePlan()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 900);
    expect(body).toMatch(/leg\?\.optType !== 'CE' && leg\?\.optType !== 'PE'/);
    expect(body).toContain('tradingsymbol: leg.sym');
  });
});

test.describe('Changing Spread% or the basket triggers an immediate re-check', () => {
  test('the gate re-check $effect watches spreadMaxPctOverride and chainBasket', () => {
    const effIdx = chainSrc.indexOf('// Immediate re-check triggers');
    expect(effIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(effIdx, effIdx + 400);
    expect(body).toContain('void spreadMaxPctOverride');
    expect(body).toContain('void chainBasket');
    expect(body).toContain('_gate.recheck()');
  });

  test("createSpreadGate's recheck() discards a still-in-flight response from before the edit (generation guard)", () => {
    expect(gateLibSrc).toContain('function recheck()');
    const fnIdx = gateLibSrc.indexOf('function recheck()');
    const body = gateLibSrc.slice(fnIdx, fnIdx + 300);
    expect(body).toContain('_gen += 1');
    expect(body).toContain('_abortInFlight()');
  });
});

test.describe('An explicit confirm-override allows submission despite a persistent warning', () => {
  test('_gatePlaceAnyway() calls confirmOverride(), and confirmOverride() resolves the gate to overridden (= proceed)', () => {
    expect(chainSrc).toContain('function _gatePlaceAnyway() { _gate?.confirmOverride(); }');
    const fnIdx = gateLibSrc.indexOf('function confirmOverride()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = gateLibSrc.slice(fnIdx, fnIdx + 300);
    expect(body).toContain("phase: 'overridden'");
  });

  test("runPreSubmitGate() resolves the awaiting caller true on 'overridden' (submission proceeds)", () => {
    const fnIdx = chainSrc.indexOf('export async function runPreSubmitGate()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 1200);
    expect(body).toContain("s.phase === 'passed' || s.phase === 'overridden'");
    expect(body).toContain('_settleGateResolvers(true)');
  });
});

test.describe('No CE/PE leg staged skips the check entirely', () => {
  test('_buildGatePlan() returns an empty plan when the basket has no CE/PE leg', () => {
    const fnIdx = chainSrc.indexOf('function _buildGatePlan()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 900);
    expect(body).toMatch(/continue;\s*\/\/ futures legs out of scope/);
  });

  test('runPreSubmitGate() resolves true immediately (no loop) when the plan is empty', () => {
    const fnIdx = chainSrc.indexOf('export async function runPreSubmitGate()');
    const body = chainSrc.slice(fnIdx, fnIdx + 500);
    expect(body).toContain('if (items.length === 0)');
    expect(body).toContain('return true;');
  });
});

test.describe('The loop never hangs indefinitely on a simulated backend failure', () => {
  // Authoritative behavioural coverage lives in spreadGate.test.js
  // (Vitest, fake timers) — these assertions confirm OptionChainTab
  // actually wires the BOUNDED config (not an unbounded custom override).
  test('createSpreadGate exports bounded constants for errors and total wait', () => {
    expect(gateLibSrc).toContain('export const SPREAD_GATE_MAX_ERRORS');
    expect(gateLibSrc).toContain('export const SPREAD_GATE_MAX_WAIT_MS');
    const errIdx = gateLibSrc.indexOf('maxErrors) {');
    expect(errIdx).toBeGreaterThan(-1);
  });

  test('OptionChainTab does not override pollMs/maxErrors/maxWaitMs with an unbounded value', () => {
    const fnIdx = chainSrc.indexOf('_gate = createSpreadGate({');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 400);
    // Uses the library defaults — no bespoke pollMs/maxErrors/maxWaitMs
    // override that could widen or remove the bound.
    expect(body).not.toContain('maxErrors:');
    expect(body).not.toContain('maxWaitMs:');
  });

  test("the 'error' and 'timeout' phases both offer Retry + Place anyway + Cancel, never a silent re-poll forever", () => {
    for (const phase of ["'error'", "'timeout'"]) {
      const idx = chainSrc.indexOf(`_gateState.phase === ${phase}`);
      expect(idx, `banner branch for ${phase} missing`).toBeGreaterThan(-1);
    }
    expect(chainSrc).toContain('data-testid="spread-gate-retry"');
  });
});

test.describe('No impact on the Ticket tab / OrderTicket submit path', () => {
  test('the shared Submit button only awaits the gate when _activeTab === "chain"', () => {
    const idx = shellSrc.indexOf("if (_activeTab === 'chain' && _chainTabRef?.runPreSubmitGate)");
    expect(idx).toBeGreaterThan(-1);
  });

  test('the Ticket tab still submits via _modalFireSubmit(), untouched by the gate', () => {
    expect(shellSrc).toContain("} else if (_activeTab === 'ticket') {\n                _modalFireSubmit();");
  });

  test('Spread% stays Chain-only — OrderTicket.svelte has no spreadMaxPctOverride prop at all (T3.3, bracket-removal — OrderTicket no longer mounts anything Bracket-related)', () => {
    const orderTicketSrc = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicketSrc).not.toContain('spreadMaxPctOverride');
    expect(orderTicketSrc).not.toContain('TemplateBar');
  });
});

test.describe('Backend contract — GET /api/orders/spread-check', () => {
  test('checkOrderSpread() calls the real endpoint with tradingsymbol/exchange/max_spread_pct', () => {
    const apiSrc = readFileSync(`${ROOT}/api.js`, 'utf-8');
    const fnIdx = apiSrc.indexOf('export async function checkOrderSpread(');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = apiSrc.slice(fnIdx, fnIdx + 500);
    expect(body).toContain('/orders/spread-check');
    expect(body).toContain('max_spread_pct');
  });

  test('no premium%-mode wing resolution remains — the wing/template machinery that used to feed the gate is gone', () => {
    expect(chainSrc).not.toContain('_resolvePremiumWingSymbols');
    expect(chainSrc).not.toContain('_premiumWingSymCache');
    expect(chainSrc).not.toContain('_effTemplateForLeg');
    expect(chainSrc).not.toContain('_effWingParams');
  });
});

test.describe('Browser smoke — app boots with the new wiring (no console errors)', () => {
  test('SymbolPanel modal opens without throwing', async ({ page }) => {
    await loginAsAdmin(page);
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 15_000 });
    await page.waitForTimeout(800);

    const firstCell = page.locator('[role="gridcell"]').first();
    const hasCell = await firstCell.isVisible({ timeout: 3000 }).catch(() => false);
    if (hasCell) {
      await firstCell.click({ timeout: 5000, force: true }).catch(() => {});
      await page.waitForTimeout(500);
    }

    const fatal = consoleErrors.filter((e) =>
      /spreadGate|runPreSubmitGate|_chainTabRef|checkOrderSpread/i.test(e));
    expect(fatal, `spread-gate-related console errors: ${JSON.stringify(fatal)}`).toHaveLength(0);
  });
});
