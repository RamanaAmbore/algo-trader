/**
 * order_ticket_bracket_scope_ssot_and_clear_policy.spec.js
 *
 * 2026-11-01 investigation (handed back without a code change to the
 * auto-select policy itself — see the handback for the full writeup).
 *
 * A task framed `_autoSelectTemplate()`'s comments-only body in
 * `OrderTicket.svelte` as an accidental bug and asked for it to be
 * filled in with `templateId = _defaultTemplate?.id ?? null;`.
 * Chronology (git log -S) showed this was wrong: commit `2f60d39a`
 * (2026-10-05, "fix(orders): no implicit take-profit, no default
 * ticket template...") deliberately gutted this function's real body
 * (and made the identical change to SymbolPanel.svelte's own
 * `_sharedTemplateId = null;` side-aware auto-swap effect, same
 * commit) — restoring it would silently reverse a confirmed, recent,
 * explicit operator policy. That reactivation was NOT applied.
 *
 * Two real, narrow defects WERE fixed, independent of that policy:
 *
 * 1. `_defaultTemplate`'s own scope computation used the bare `symbol`
 *    prop instead of `_resolvedSymbol || symbol`, disagreeing with the
 *    side+symbol re-validation effect's identical-in-intent scope
 *    expression. `_defaultTemplate` still feeds the Bracket toggle's
 *    DISPLAYED label (via `_ticketState.defaultTemplate`, mirrored to
 *    SymbolPanel) even though nothing auto-arms it any more — an SSOT
 *    bug independent of the auto-select question.
 * 2. The #16 flash's comment and rendered text ("Bracket auto-updated
 *    for new side") became FALSE the moment 2f60d39a removed the
 *    re-selection it was announcing — it fires on every side-flip
 *    mismatch, implying a new default was armed, when the bracket was
 *    actually just cleared to None. Wording fixed to "Bracket cleared
 *    for new side".
 *
 * Test 1 is a source-level regression guard (the divergence between
 * `_resolvedSymbol` and the bare `symbol` prop is reachable only via
 * the bare-underlying picker, which per
 * `order_ticket_preview_resolved_symbol_watchers.spec.js`'s own
 * documented finding is NOT reachable through the real `/orders` UI
 * today — same precedent, same reason for a source-level guard here).
 * Test 2 is a source-level guard pinning `_autoSelectTemplate()` as a
 * deliberate no-op, so a future edit can't silently re-arm a default
 * without a conscious, reviewed change to this exact test.
 * Test 3 is a live interaction test on a reachable concrete symbol
 * (RELIANCE — no expiry, so immune to the 26OCT-style staleness that
 * hits hardcoded futures symbols elsewhere in this test suite) proving
 * the actual, current behaviour: a side flip clears an armed bracket
 * to None (never silently re-arms a wrong-scope template, never
 * silently re-arms the OTHER scope's template either), and the flash
 * text accurately says "cleared".
 *
 * 2026-11-01 follow-up (option C of the same investigation): closes a
 * SEPARATE, narrower SSOT gap the write-up above left open.
 * `_defaultTemplate` (OrderTicket) feeds the Bracket toggle's DISPLAYED
 * label via the `onTicketStateChange` -> `_ticketState.defaultTemplate`
 * mirror, but clicking the toggle's "Default" button on the Ticket tab
 * arms `SymbolPanel`'s own `_sideAwareDefault`, computed via the
 * SHELL's `_currentScope()` — which, before this follow-up, could only
 * see the shell's own `_localSymbol` on Ticket (no legs), never
 * OrderTicket's internally-resolved contract (`_resolvedSymbol`). For a
 * bare-underlying option order (symbol = root, OrderTicket resolves a
 * specific CE/PE contract that `_localSymbol` never gets rewritten to —
 * see `_pickSymbol`'s Ticket-tab branch) the DISPLAYED template and the
 * template a Default CLICK arms could disagree.
 *
 * Fix: the `onTicketStateChange` mirror now also carries
 * `resolvedSymbol: _resolvedSymbol`; `_currentScope()`'s non-preferLeg
 * (Ticket-tab-no-legs) branch now prefers `_ticketState.resolvedSymbol`
 * ahead of `_localSymbol`. The preferLeg (Chain tab / basket legs)
 * branch is untouched — that's the unrelated, already-correct
 * 2026-09-30 fix referenced in the comment above.
 *
 * Not live-reachable, same precedent as Test 1 above and as
 * `order_ticket_preview_resolved_symbol_watchers.spec.js`'s own
 * documented finding: `_resolvedSymbol` only diverges from the bare
 * `symbol` prop when `_isBareUnderlying` is true, which requires the
 * `symType` prop to be `'OPT'`/`'FUT'` — a static `'ALL'` in
 * `SymbolPanel.svelte` with no UI control that ever changes it. So the
 * tests below are source-level: they extract the REAL, current
 * `_currentScope()` function body out of `SymbolPanel.svelte` at test
 * run time and execute it (via `new Function(...)`, with the real
 * `appliesToFor` from `templateScope.js` injected) against synthetic
 * inputs that reproduce the gap — proving actual behaviour, not just a
 * regex pattern match, while being honest that it's a source-level
 * proof rather than a live-UI one.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

const BUY_DEFAULT_TEMPLATE = {
  id: 1, kind: 'bracket', name: 'Buy Default Bracket', slug: null,
  description: '', is_system: false, status: 'active', group: null, tags: [],
  legacy_template_id: 9301,
  bracket_config: { applies_to: 'buy_any', is_default: true, tp_pct: 5, sl_pct: 3 },
};
const SELL_DEFAULT_TEMPLATE = {
  id: 2, kind: 'bracket', name: 'Sell Default Bracket', slug: null,
  description: '', is_system: false, status: 'active', group: null, tags: [],
  legacy_template_id: 9302,
  bracket_config: { applies_to: 'sell_any', is_default: true, tp_pct: 5, sl_pct: 3 },
};

async function mockBracketTemplates(page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([BUY_DEFAULT_TEMPLATE, SELL_DEFAULT_TEMPLATE]),
    });
  });
}

test.describe('Order Ticket — _defaultTemplate / re-validation scope SSOT (source)', () => {
  test('source: _defaultTemplate and the re-validation effect resolve scope identically', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');

    const defIdx = src.indexOf('const _defaultTemplate = $derived.by(');
    expect(defIdx, '_defaultTemplate derived not found').toBeGreaterThan(-1);
    const defBlock = src.slice(defIdx, src.indexOf('});', defIdx));
    expect(defBlock).toMatch(/_appliesToFor\(_side,\s*_resolvedSymbol\s*\|\|\s*symbol\)/);
    // Regression: must NOT use the bare `symbol` prop alone.
    expect(defBlock).not.toMatch(/_appliesToFor\(_side,\s*symbol\)/);

    const effIdx = src.indexOf('const newScope = _appliesToFor(');
    expect(effIdx, 're-validation effect scope line not found').toBeGreaterThan(-1);
    const effLine = src.slice(effIdx, src.indexOf(';', effIdx) + 1);
    expect(effLine).toMatch(/_appliesToFor\(_side,\s*_resolvedSymbol\s*\|\|\s*symbol\)/);
  });

  test('source: _autoSelectTemplate is a deliberate no-op (2f60d39a policy) — not silently re-armed', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const idx = src.indexOf('function _autoSelectTemplate()');
    expect(idx, '_autoSelectTemplate not found').toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('\n  }', idx) + 4);
    // The three early-return guards must still be present.
    expect(block).toMatch(/if \(action === 'close'\) return;/);
    expect(block).toMatch(/if \(templateId !== null\) return;/);
    expect(block).toMatch(/if \(_templates\.length === 0\) return;/);
    // No assignment to templateId anywhere in the function body — a
    // future change that re-arms a default here must also update this
    // test, which is the point.
    expect(block).not.toMatch(/templateId\s*=/);
  });

  test('source: the #16 flash markup renders the corrected clear-only text', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const idx = src.indexOf('{#if _templateChanged}');
    expect(idx, '_templateChanged markup block not found').toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('{/if}', idx));
    // Scoped to the rendered markup block only — the surrounding
    // comments intentionally quote the OLD, now-corrected wording for
    // history, so a whole-file check would false-positive on them.
    expect(block).toContain('Bracket cleared for new side');
    expect(block).not.toContain('Bracket auto-updated for new side');
  });
});

test.describe('Order Ticket — side flip clears (never silently re-arms) the bracket', () => {
  test('BUY default armed -> flip to SELL -> bracket clears to None, flash says "cleared"', async ({ page }) => {
    test.setTimeout(60_000);
    await page.route('**/api/admin/execution/mode', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'] }),
        });
      } else {
        await route.continue();
      }
    });

    await loginAsAdmin(page);
    await mockBracketTemplates(page);

    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const symInput = page.locator('.ssi-input').first();
    await expect(symInput).toBeVisible({ timeout: 15_000 });
    await symInput.fill('RELIANCE');
    const sugg = page.locator('.ssi-drop .ssi-row').first();
    await expect(sugg).toBeVisible({ timeout: 45_000 });
    await sugg.click({ force: true });
    await page.waitForTimeout(500);

    // Force a known starting side.
    const buyPill = page.locator('button.ot-side-buy').first();
    await expect(buyPill).toBeVisible({ timeout: 15_000 });
    await buyPill.click();
    await page.waitForTimeout(200);

    // Arm the Bracket toggle explicitly (operator action — this is how
    // a bracket gets armed under the current no-implicit-default
    // policy; nothing arms it automatically).
    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    if (!(await toggle.evaluate((el) => el.classList.contains('active')))) {
      await toggle.click();
    }
    await expect(toggle).toHaveClass(/active/, { timeout: 5_000 });
    await expect(toggle).toHaveAttribute('title', /Buy Default Bracket/, { timeout: 5_000 });

    // Flip side — this is the mismatch trigger (buy_any no longer
    // matches sell_any).
    const sellPill = page.locator('button.ot-side-sell').first();
    await expect(sellPill).toBeVisible({ timeout: 15_000 });
    await sellPill.click();

    // The bracket must clear to None — NEVER silently re-arm to the
    // Sell Default (that would be the reverted policy) and never stay
    // stuck on the stale Buy Default either.
    await expect(toggle).not.toHaveClass(/active/, { timeout: 5_000 });
    await expect(toggle).toHaveAttribute('title', /No Bracket/, { timeout: 5_000 });

    // The flash must say "cleared", never "auto-updated".
    const flash = page.locator('.ot-tmpl-changed-flash').first();
    // The flash is a 1.2s transient — tolerate it having already
    // cleared on a slow CI run, but if it's visible, its text must be
    // the corrected wording.
    if (await flash.count() > 0) {
      await expect(flash).toHaveText(/Bracket cleared for new side/);
    }
  });
});

/**
 * Extracts the body of a `function <name>() { ... }` declaration from
 * raw source text via brace counting (handles nested braces/ternaries
 * inside the body; the function has no nested function literals so a
 * naive counter is safe here).
 * @param {string} src
 * @param {string} marker literal text immediately preceding the body's
 *   opening brace, e.g. `'function _currentScope() {'`.
 */
function extractFunctionBody(src, marker) {
  const markerIdx = src.indexOf(marker);
  expect(markerIdx, `marker not found: ${marker}`).toBeGreaterThan(-1);
  const braceStart = src.indexOf('{', markerIdx);
  let depth = 0;
  let i = braceStart;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) break;
    }
  }
  expect(i, `matching closing brace not found for: ${marker}`).toBeLessThan(src.length);
  return src.slice(braceStart + 1, i);
}

test.describe('SymbolPanel — _currentScope() SSOT with OrderTicket\'s _resolvedSymbol mirror (source)', () => {
  test('source: onTicketStateChange mirror carries resolvedSymbol', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const idx = src.indexOf('onTicketStateChange?.({');
    expect(idx, 'onTicketStateChange mirror call not found').toBeGreaterThan(-1);
    const block = src.slice(idx, src.indexOf('});', idx));
    expect(block).toMatch(/resolvedSymbol:\s*_resolvedSymbol/);
  });

  test('source: _currentScope() non-preferLeg branch prefers _ticketState.resolvedSymbol; preferLeg branch is untouched', () => {
    const src = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    const body = extractFunctionBody(src, 'function _currentScope() {');
    // The Chain/basket-leg branch (preferLeg === true) must be
    // byte-identical to the pre-existing 2026-09-30 fix — it must never
    // consult `_ticketState`.
    expect(body).toContain("? (legSym || (_localSymbol || '').trim())");
    // The Ticket-tab-no-legs branch (preferLeg === false) must check
    // `_ticketState.resolvedSymbol` BEFORE `_localSymbol`.
    const falseBranchIdx = body.indexOf(': (');
    expect(falseBranchIdx, 'preferLeg-false branch not found').toBeGreaterThan(-1);
    const falseBranch = body.slice(falseBranchIdx, body.indexOf(';', falseBranchIdx));
    const resolvedIdx = falseBranch.indexOf('_ticketState');
    const localIdx = falseBranch.indexOf('_localSymbol');
    expect(resolvedIdx, '_ticketState not read in preferLeg-false branch').toBeGreaterThan(-1);
    expect(localIdx, '_localSymbol not read in preferLeg-false branch').toBeGreaterThan(-1);
    expect(resolvedIdx).toBeLessThan(localIdx);
  });

  test('behavioural: _currentScope() resolves the SAME scope as OrderTicket\'s _defaultTemplate/re-validation effect for a bare-underlying option order', async () => {
    const { appliesToFor } = await import('../src/lib/data/templateScope.js');
    const src = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    const body = extractFunctionBody(src, 'function _currentScope() {');

    // Build the REAL, current `_currentScope()` logic as a callable —
    // extracted from the live source at test-run time, not a hand-typed
    // reimplementation that could silently drift from the real code.
    const currentScope = new Function(
      '_focusedLeg', 'basketLegs', '_activeTab', '_localSymbol', '_ticketState',
      '_modalSide', '_appliesToFor',
      body
    );

    // Scenario: bare-underlying option order on the Ticket tab, no
    // basket legs staged. The shell's own `_localSymbol` still holds
    // the bare root (nothing on the Ticket tab ever rewrites it to the
    // resolved contract — see `_pickSymbol`'s Ticket-tab branch), while
    // OrderTicket has resolved a specific CE contract internally via
    // its own strike/expiry picker and mirrored it up as
    // `_ticketState.resolvedSymbol`.
    const side = 'BUY';
    const localSymbol = 'RELIANCE';
    const resolvedSymbol = 'RELIANCE26NOV1500CE';

    const shellScope = currentScope(
      /* _focusedLeg */ null,
      /* basketLegs */ [],
      /* _activeTab */ 'ticket',
      /* _localSymbol */ localSymbol,
      /* _ticketState */ { resolvedSymbol },
      /* _modalSide */ side,
      /* _appliesToFor */ appliesToFor,
    );

    // What OrderTicket's own `_defaultTemplate` / re-validation effect
    // compute, byte-identical expression:
    // `_appliesToFor(_side, _resolvedSymbol || symbol)`.
    const ticketScope = appliesToFor(side, resolvedSymbol || localSymbol);

    expect(shellScope).toBe('buy_option');
    expect(ticketScope).toBe('buy_option');
    expect(shellScope).toBe(ticketScope);

    // Regression guard: confirm this test actually discriminates the
    // fix. The OLD precedence (local-symbol-first, no ticketState read)
    // would have resolved the shell to the WRONG, disagreeing scope —
    // proving the label-vs-click-action gap this follow-up closes was
    // real, not hypothetical.
    const preFixShellScope = appliesToFor(side, localSymbol);
    expect(preFixShellScope).toBe('buy_any');
    expect(preFixShellScope).not.toBe(ticketScope);
  });

  test('behavioural: the Chain/basket-leg branch ignores _ticketState.resolvedSymbol (unchanged 2026-09-30 behaviour)', async () => {
    const { appliesToFor } = await import('../src/lib/data/templateScope.js');
    const src = readFileSync(`${ROOT}/SymbolPanel.svelte`, 'utf-8');
    const body = extractFunctionBody(src, 'function _currentScope() {');
    const currentScope = new Function(
      '_focusedLeg', 'basketLegs', '_activeTab', '_localSymbol', '_ticketState',
      '_modalSide', '_appliesToFor',
      body
    );

    // Chain tab, a focused leg carrying a SELL option contract, and a
    // deliberately-mismatched `_ticketState.resolvedSymbol` that must
    // be ignored — proving the preferLeg branch stayed untouched.
    const scope = currentScope(
      /* _focusedLeg */ { sym: 'NIFTY26JUNC22000CE', side: 'SELL' },
      /* basketLegs */ [],
      /* _activeTab */ 'chain',
      /* _localSymbol */ 'NIFTY',
      /* _ticketState */ { resolvedSymbol: 'RELIANCE' },
      /* _modalSide */ 'BUY',
      /* _appliesToFor */ appliesToFor,
    );
    expect(scope).toBe('sell_option');
  });
});
