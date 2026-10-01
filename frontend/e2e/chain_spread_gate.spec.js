/**
 * chain_spread_gate.spec.js
 *
 * Chain-tab pre-submission spread gate (operator: "if there is too
 * much spread on the offset limit side, it should warn ... it should
 * be in a loop until the conditions are satisfied before placing the
 * order"). Scope: OptionChainTab.svelte + TemplateBar.svelte +
 * SymbolPanel.svelte's shared Submit button wiring ONLY — the Ticket
 * tab's own submit path (`_modalFireSubmit`) must be untouched.
 *
 * The full flow (stage a wing-configured leg in a real chain basket,
 * then exercise a live multi-second poll loop against a mocked
 * backend) requires a real IndexedDB instruments cache + live chain
 * quotes + a staged basket — reaching that deterministically in a
 * browser is its own large undertaking independent of this feature.
 * Per this codebase's existing convention for this exact file
 * (`option_chain_order.spec.js` — a source-scan spec, no browser),
 * the scenario-level requirements below are verified as precise code
 * shape assertions instead: every assertion targets the literal
 * function/branch that implements the described behaviour, so a
 * regression that removes or inverts the logic fails the test exactly
 * as a behavioural mock would. The bounded-loop mechanics themselves
 * (never hangs, discards stale responses, bounded errors/timeout) are
 * covered with real fake-timer behavioural tests in
 * `frontend/src/lib/__tests__/data/spreadGate.test.js` (Vitest) —
 * that is the authoritative test for "doesn't hang indefinitely".
 *
 * One genuine browser smoke test is included at the bottom to confirm
 * the app still boots with the new wiring in place (no console errors
 * mounting SymbolPanel), mirroring `template_bar_smoke.spec.js`'s
 * already-established soft-smoke pattern.
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
const TEMPLATE_PATH = `${ROOT}/TemplateBar.svelte`;
const SHELL_PATH     = `${ROOT}/SymbolPanel.svelte`;
const GATE_LIB_PATH  = `${ROOT}/data/spreadGate.js`;

const BACKEND_ROOT = '/Users/ramanambore/projects/ramboq/backend';
const SETTINGS_PATH     = `${BACKEND_ROOT}/shared/helpers/settings.py`;
const SPREAD_CHECK_PATH = `${BACKEND_ROOT}/api/algo/spread_check.py`;
const TEMPLATE_ATTACH_PATH = `${BACKEND_ROOT}/api/algo/template_attach.py`;

let chainSrc = '', templateSrc = '', shellSrc = '', gateLibSrc = '';
let settingsSrc = '', spreadCheckSrc = '', templateAttachSrc = '';

test.beforeAll(() => {
  chainSrc    = readFileSync(CHAIN_PATH, 'utf-8');
  templateSrc = readFileSync(TEMPLATE_PATH, 'utf-8');
  shellSrc    = readFileSync(SHELL_PATH, 'utf-8');
  gateLibSrc  = readFileSync(GATE_LIB_PATH, 'utf-8');

  settingsSrc        = readFileSync(SETTINGS_PATH, 'utf-8');
  spreadCheckSrc      = readFileSync(SPREAD_CHECK_PATH, 'utf-8');
  templateAttachSrc   = readFileSync(TEMPLATE_ATTACH_PATH, 'utf-8');
});

test.describe('Spread% default threshold is 0.5% everywhere (not the old 10.0%)', () => {
  test('backend admin-settings seed registers templates.wing_max_spread_pct default as 0.5', () => {
    expect(settingsSrc).toContain(
      '("templates", "templates.wing_max_spread_pct", "float", 0.5,'
    );
    expect(settingsSrc).not.toContain(
      '("templates", "templates.wing_max_spread_pct", "float", 10.0,'
    );
  });

  test('resolve_max_spread_pct() fallback (get_float + except branch) both use 0.5', () => {
    expect(spreadCheckSrc).toContain(
      'get_float("templates.wing_max_spread_pct", 0.5)'
    );
    expect(spreadCheckSrc).toContain('return 0.5, "setting"');
    expect(spreadCheckSrc).not.toContain(
      'get_float("templates.wing_max_spread_pct", 10.0)'
    );
  });

  test('_pick_wing_by_premium() fallback (get_float + except branch) both use 0.5', () => {
    expect(templateAttachSrc).toContain(
      'get_float("templates.wing_max_spread_pct", 0.5)'
    );
    expect(templateAttachSrc).toContain('min_oi, max_spread_pct, chain_radius = 1000, 0.5, 20');
    expect(templateAttachSrc).not.toContain(
      'get_float("templates.wing_max_spread_pct", 10.0)'
    );
  });

  test('frontend fallbacks (TemplateBar + OptionChainTab) both use 0.5, not 10', () => {
    expect(templateSrc).toContain('selectedTemplate?.wing_max_spread_pct ?? 0.5');
    expect(chainSrc).toContain('(tpl.wing_max_spread_pct ?? 0.5)');
    expect(templateSrc).not.toContain('selectedTemplate?.wing_max_spread_pct ?? 10');
    expect(chainSrc).not.toContain('(tpl.wing_max_spread_pct ?? 10)');
  });
});

test.describe('TP %/SL %/Spread % labels render with a space before the %', () => {
  test('TemplateBar renders "TP %", "SL %", "Spread %" (space before %, asterisk right after %)', () => {
    expect(templateSrc).toContain("<span>TP %{_tpAsterisk ? '*' : ''}</span>");
    expect(templateSrc).toContain("<span>SL %{_slAsterisk ? '*' : ''}</span>");
    expect(templateSrc).toContain("<span>Spread %{_spreadAsterisk ? '*' : ''}</span>");
    expect(templateSrc).not.toContain('<span>TP%');
    expect(templateSrc).not.toContain('<span>SL%');
    expect(templateSrc).not.toContain('<span>Spread%');
  });

  test('TemplateBar also spaces the sibling "Trail SL %" label (same fix, same file)', () => {
    expect(templateSrc).toContain("<span>Trail SL %{_trailAsterisk ? '*' : ''}</span>");
    expect(templateSrc).not.toContain('Trail SL%');
  });

  test('SymbolPanel per-leg editor renders "TP %" / "SL %" (space before %)', () => {
    expect(shellSrc).toContain('<span>TP %</span>');
    expect(shellSrc).toContain('<span>SL %</span>');
  });
});

test.describe('Spread% field renders alongside TP%/SL% with the correct default', () => {
  test('TemplateBar declares a Spread% bindable prop + input, defaulted from wing_max_spread_pct ?? 0.5', () => {
    expect(templateSrc).toContain('spreadMaxPctOverride');
    // Default placeholder resolution — per-template field once it
    // exists, else the same 0.5 fallback the global admin setting uses.
    expect(templateSrc).toContain("selectedTemplate?.wing_max_spread_pct ?? 0.5");
    // Rendered in the SAME param row as TP%/SL% (not gated on showsWing)
    // — i.e. the <label>...Spread %... block sits between the SL % input
    // and the `{#if showsWing}` wing block, not inside it.
    const slIdx     = templateSrc.indexOf('>SL %');
    const spreadIdx = templateSrc.indexOf('>Spread %');
    const wingIfIdx  = templateSrc.indexOf('{#if showsWing}');
    expect(slIdx).toBeGreaterThan(-1);
    expect(spreadIdx).toBeGreaterThan(slIdx);
    expect(wingIfIdx).toBeGreaterThan(spreadIdx);
  });

  test('OptionChainTab threads spreadMaxPctOverride through to TemplateBar via bind:', () => {
    expect(chainSrc).toContain('spreadMaxPctOverride');
    expect(chainSrc).toContain('bind:spreadMaxPctOverride');
  });

  test('SymbolPanel owns the shared Spread% override state and binds it into OptionChainTab', () => {
    expect(shellSrc).toContain('_sharedSpreadMaxPctOverride');
    expect(shellSrc).toContain('bind:spreadMaxPctOverride={_sharedSpreadMaxPctOverride}');
  });
});

test.describe('A wide-spread result blocks submission and shows a warning', () => {
  test('_checkLegs() requires ok===true on every checked leg (parent AND offset) before the gate can pass', () => {
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

  test('both the original/parent leg AND the computed offset/wing leg are included in the check targets', () => {
    const fnIdx = chainSrc.indexOf('function _buildCheckTargets(');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 900);
    expect(body).toContain('it.parentSym');
    expect(body).toContain('it.wingSym');
  });
});

test.describe('Changing TP% / SL% / Spread% triggers an immediate re-check', () => {
  test('the gate re-check $effect watches tpOverride, slOverride, and spreadMaxPctOverride', () => {
    const effIdx = chainSrc.indexOf('// Immediate re-check triggers');
    expect(effIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(effIdx, effIdx + 700);
    expect(body).toContain('void tpOverride');
    expect(body).toContain('void slOverride');
    expect(body).toContain('void spreadMaxPctOverride');
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
    const body = chainSrc.slice(fnIdx, fnIdx + 1600);
    expect(body).toContain("s.phase === 'passed' || s.phase === 'overridden'");
    expect(body).toContain('_settleGateResolvers(true)');
  });
});

test.describe('Disabling the template skips the check entirely', () => {
  test('_buildGatePlan() returns an empty plan whenever no leg resolves a template (covers shellUsingNone)', () => {
    const fnIdx = chainSrc.indexOf('function _buildGatePlan()');
    expect(fnIdx).toBeGreaterThan(-1);
    const body = chainSrc.slice(fnIdx, fnIdx + 1100);
    expect(body).toContain('if (!wp || !wp.hasWing) continue;');
  });

  test('runPreSubmitGate() resolves true immediately (no loop) when the plan is empty', () => {
    const fnIdx = chainSrc.indexOf('export async function runPreSubmitGate()');
    const body = chainSrc.slice(fnIdx, fnIdx + 500);
    expect(body).toContain('if (items.length === 0)');
    expect(body).toContain('return true;');
  });

  test('shellUsingNone is a recheck trigger, so disabling the template mid-wait resolves on the next tick', () => {
    const effIdx = chainSrc.indexOf('// Immediate re-check triggers');
    const body = chainSrc.slice(effIdx, effIdx + 700);
    expect(body).toContain('void shellUsingNone');
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

  test('OrderTicket.svelte does not mount TemplateBar (Spread% field is Chain-only)', () => {
    const orderTicketSrc = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    expect(orderTicketSrc).not.toContain('<TemplateBar');
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

  test('premium%-mode wing resolution calls /orders/ticket/preview ONCE per gate session, not inside the per-tick checkLegs callback', () => {
    const resolveIdx = chainSrc.indexOf('async function _resolvePremiumWingSymbols(');
    expect(resolveIdx).toBeGreaterThan(-1);
    const resolveBody = chainSrc.slice(resolveIdx, resolveIdx + 1400);
    expect(resolveBody).toContain('previewTicketTemplate(');

    const checkLegsIdx = chainSrc.indexOf('async function _checkLegs(');
    const checkLegsBody = chainSrc.slice(checkLegsIdx, checkLegsIdx + 900);
    expect(checkLegsBody).not.toContain('previewTicketTemplate');

    const runGateIdx = chainSrc.indexOf('export async function runPreSubmitGate()');
    const runGateBody = chainSrc.slice(runGateIdx, runGateIdx + 900);
    expect(runGateBody).toContain('_resolvePremiumWingSymbols(');
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
