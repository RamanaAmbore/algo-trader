// 2026-10 — operator ask: regroup /automation agent categories by
// consequence ("can this agent act on its own") instead of slug-text
// guessing. Motivating bug: expiry-day-positions-alert is a pure
// notify-only review alert (empty `actions: []`), but the old
// categoryFor() matched `slug.includes('expiry')` and bucketed it into
// "Automation" alongside the two agents that actually auto-close
// positions — misleading.
//
// Source-level guard, same pattern as
// automation_agent_row_card_styling.spec.js / held_orders_card_styling.spec.js.
// Fixtures below are copied verbatim from the real backend seed shapes
// (backend/api/algo/agent_engine.py) so the structural checks actually
// prove the motivating bug is fixed, not just that the code has a
// certain shape.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const pageSrc = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname,
  'utf8'
);

function extractFn(name) {
  // Grabs `function <name>(...) { ... }` through its matching closing
  // brace (depth-counted, handles nested braces in the body). Finds the
  // function's own opening brace AFTER the parameter list's closing paren
  // (depth-counted too) — a bare `indexOf('{', start)` would wrongly match
  // a `{...}` inside an inline JSDoc param comment like `/** @type {any} */`.
  const start = pageSrc.indexOf(`function ${name}(`);
  if (start === -1) return '';
  const parenStart = pageSrc.indexOf('(', start);
  let pdepth = 0;
  let parenEnd = -1;
  for (let i = parenStart; i < pageSrc.length; i++) {
    if (pageSrc[i] === '(') pdepth++;
    else if (pageSrc[i] === ')') {
      pdepth--;
      if (pdepth === 0) { parenEnd = i; break; }
    }
  }
  if (parenEnd === -1) return '';
  const openBrace = pageSrc.indexOf('{', parenEnd);
  let depth = 0;
  for (let i = openBrace; i < pageSrc.length; i++) {
    if (pageSrc[i] === '{') depth++;
    else if (pageSrc[i] === '}') {
      depth--;
      if (depth === 0) return pageSrc.slice(start, i + 1);
    }
  }
  return '';
}

const categoryForSrc = extractFn('categoryFor');
const groupedAgentsSrc = extractFn('groupedAgents');
const sentinelSrc = extractFn('_isScheduleOnlySentinel');

test.describe('automation /+page.svelte — category grouping by consequence', () => {
  test('AUTOMATED_ACTION_TYPES is defined with exactly the 7 broker-verb action types', () => {
    const match = pageSrc.match(/const AUTOMATED_ACTION_TYPES\s*=\s*new Set\(\[([\s\S]*?)\]\)/);
    expect(match).not.toBeNull();
    const body = match[1];
    for (const verb of [
      'place_order', 'modify_order', 'cancel_order', 'cancel_all_orders',
      'close_position', 'chase_close_positions', 'expiry_auto_close',
    ]) {
      expect(body).toMatch(new RegExp(`['"]${verb}['"]`));
    }
    // Pure-notification action types must never be in this set.
    for (const notifyOnly of ['monitor_order', 'deactivate_agent', 'set_flag', 'emit_log']) {
      expect(body).not.toMatch(new RegExp(`['"]${notifyOnly}['"]`));
    }
  });

  test('CATEGORY_ORDER matches the new 4-bucket scheme exactly', () => {
    const match = pageSrc.match(/const CATEGORY_ORDER\s*=\s*(\[[^\]]*\])/);
    expect(match).not.toBeNull();
    // eslint-disable-next-line no-eval
    const order = eval(match[1]);
    expect(order).toEqual(['Automated Actions', 'Risk Alerts', 'Scheduled Info', 'Custom']);
  });

  test('categoryFor excludes the manual pseudo-agent (slug === "manual")', () => {
    expect(categoryForSrc).toMatch(/agent\.slug\s*===\s*['"]manual['"]/);
  });

  test('groupedAgents() guards against a falsy category (manual never lands in any group)', () => {
    expect(groupedAgentsSrc).toMatch(/if\s*\(!cat\)\s*continue;/);
  });

  test('categoryFor checks actions for an automated-action-type match BEFORE the scheduled/system/custom checks', () => {
    const idxActions = categoryForSrc.search(/AUTOMATED_ACTION_TYPES\.has/);
    const idxFireAt = categoryForSrc.search(/fire_at_time/);
    const idxIsSystem = categoryForSrc.search(/agent\.is_system/);
    expect(idxActions).toBeGreaterThan(-1);
    expect(idxFireAt).toBeGreaterThan(-1);
    expect(idxIsSystem).toBeGreaterThan(-1);
    expect(idxActions).toBeLessThan(idxFireAt);
    expect(idxActions).toBeLessThan(idxIsSystem);
  });

  test('schedule-only sentinel check mirrors backend convention (abs(value) >= 1e8)', () => {
    expect(sentinelSrc).toMatch(/Math\.abs\(v\)\s*>=\s*1e8/);
  });

  // ── Structural fixtures, executed against the REAL extracted source (via
  // `new Function`) rather than a hand-copied reimplementation — a future
  // regression in the real categoryFor/groupedAgents would fail this test,
  // not silently pass against a stale copy. Fixture shapes are copied
  // verbatim from backend/api/algo/agent_engine.py's real seed dicts.
  test('AUTOMATED_ACTION_TYPES has exactly 7 entries', () => {
    const setSrc = pageSrc.match(/const AUTOMATED_ACTION_TYPES\s*=\s*new Set\(\[[\s\S]*?\]\);/)[0];
    const size = new Function(`${setSrc}\nreturn AUTOMATED_ACTION_TYPES.size;`)();
    expect(size).toBe(7);
  });

  test('fixture-level: real seed shapes classify correctly under the ACTUAL source', () => {
    const setSrc = pageSrc.match(/const AUTOMATED_ACTION_TYPES\s*=\s*new Set\(\[[\s\S]*?\]\);/)[0];
    const orderSrc = pageSrc.match(/const CATEGORY_ORDER\s*=\s*\[[^\]]*\];/)[0];
    const { categoryFor, groupedAgentsFactory } = new Function(`
      ${setSrc}
      ${orderSrc}
      ${sentinelSrc}
      ${categoryForSrc}
      function groupedAgentsFactory(agents) {
        ${groupedAgentsSrc.replace(/^function groupedAgents\(\)\s*\{/, '').replace(/\}$/, '')}
      }
      return { categoryFor, groupedAgentsFactory };
    `)();

    // loss-pos-total-auto-close — real action: {"type": "chase_close_positions", ...}
    expect(categoryFor({
      slug: 'loss-pos-total-auto-close', is_system: true, fire_at_time: null,
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -5000 },
      actions: [{ type: 'chase_close_positions', params: {} }],
    })).toBe('Automated Actions');

    // expiry-day-equity-itm-auto-close — real action: {"type": "expiry_auto_close", ...}
    expect(categoryFor({
      slug: 'expiry-day-equity-itm-auto-close', is_system: true, fire_at_time: '15:15',
      conditions: { all: [{ metric: 'is_itm', scope: 'positions.expiring_today.nfo', op: '==', value: 1.0 }] },
      actions: [{ type: 'expiry_auto_close', params: { exchange: 'NFO' } }],
    })).toBe('Automated Actions');

    // expiry-day-positions-alert — THE motivating bug fixture. Real seed has
    // no `actions` key at all (model column default -> []), a real (non-
    // sentinel) condition, and no fire_at_time. Must land in Risk Alerts,
    // not Automated Actions, despite "expiry" in the slug.
    expect(categoryFor({
      slug: 'expiry-day-positions-alert', is_system: true, fire_at_time: null,
      conditions: { metric: 'days_until_expiry', scope: 'positions.expiring_today', op: '<=', value: 1.5 },
      actions: [],
    })).toBe('Risk Alerts');

    // market-open-nse — real seed condition is a hand-authored sentinel
    // ({"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin",
    // "value": -999999999}), NOT null and NOT {} — must still classify as
    // Scheduled Info via the 1e8-magnitude sentinel check.
    expect(categoryFor({
      slug: 'market-open-nse', is_system: true, fire_at_time: '09:15',
      conditions: { op: '>=', scope: 'funds.any_acct', metric: 'avail_margin', value: -999999999 },
      actions: [],
    })).toBe('Scheduled Info');

    // market-preclose-mcx — same sentinel pattern.
    expect(categoryFor({
      slug: 'market-preclose-mcx', is_system: true, fire_at_time: '23:00',
      conditions: { op: '>=', scope: 'funds.any_acct', metric: 'avail_margin', value: -999999999 },
      actions: [],
    })).toBe('Scheduled Info');

    // manual — excluded entirely (null sentinel), never appears in any group.
    expect(categoryFor({
      slug: 'manual', is_system: false, fire_at_time: null,
      conditions: null, actions: [],
    })).toBeNull();

    // A user-created agent with no actions, a real condition, not is_system.
    expect(categoryFor({
      slug: 'my-custom-agent', is_system: false, fire_at_time: null,
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1000 },
      actions: [],
    })).toBe('Custom');

    // groupedAgentsFactory (the real groupedAgents() body, executed against
    // a fixture roster) — proves exclusion + ordering by behavior, not just
    // by regexing for `if (!cat) continue`.
    const roster = [
      { name: 'Manual operator order', slug: 'manual', is_system: false, fire_at_time: null, conditions: null, actions: [] },
      { name: 'Auto-close on loss', slug: 'loss-pos-total-auto-close', is_system: true, fire_at_time: null,
        conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -5000 },
        actions: [{ type: 'chase_close_positions', params: {} }] },
      { name: 'Positions expiring today', slug: 'expiry-day-positions-alert', is_system: true, fire_at_time: null,
        conditions: { metric: 'days_until_expiry', scope: 'positions.expiring_today', op: '<=', value: 1.5 },
        actions: [] },
      { name: 'NSE market open', slug: 'market-open-nse', is_system: true, fire_at_time: '09:15',
        conditions: { op: '>=', scope: 'funds.any_acct', metric: 'avail_margin', value: -999999999 },
        actions: [] },
    ];
    const groups = groupedAgentsFactory(roster);
    const allGroupedSlugs = groups.flatMap(g => g.agents.map(a => a.slug));
    expect(allGroupedSlugs).not.toContain('manual');
    expect(groups.map(g => g.name)).toEqual(['Automated Actions', 'Risk Alerts', 'Scheduled Info']);
  });
});
