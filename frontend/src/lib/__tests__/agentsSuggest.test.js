/**
 * agentsSuggest.test.js — Vitest coverage for `suggestAgentCliAt()`
 * (Sprint 4, agent CLI grammar live autocomplete), added to
 * frontend/src/lib/command/grammars/agents.js.
 *
 * Fixture catalog mirrors agentsGrammar.test.js's FIXTURE_ROWS (same
 * metric/channel/action names) so the compiler's own test suite and this
 * one stay consistent about what a realistic catalog looks like.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const { buildCatalog, suggestAgentCliAt } = await import('$lib/command/grammars/agents.js');

const FIXTURE_ROWS = [
  // metrics
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'day_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'avail_margin', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'mean_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'stdev_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'max_drawdown_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  // scopes
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.any_acct', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.any_acct', params_schema: null },
  // channels
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  // actions — place_order mirrors the real agent_grammar.yaml schema
  // (account first, enum fields for exchange/side/order_type/chase_level).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'place_order',
    params_schema: {
      account: { type: 'string', required: true, token_ref_ok: true },
      symbol: { type: 'string', required: true },
      exchange: { type: 'enum', enum: ['NSE', 'BSE', 'NFO', 'CDS', 'MCX'], required: false, default: 'NFO' },
      side: { type: 'enum', enum: ['BUY', 'SELL'], required: true },
      qty: { type: 'number', required: true, token_ref_ok: true },
      order_type: { type: 'enum', enum: ['MARKET', 'LIMIT', 'SL', 'SL-M'], required: false, default: 'MARKET' },
      chase_level: { type: 'enum', enum: ['LOW', 'MED', 'HIGH'], required: false },
      tag: { type: 'string', required: false },
    },
  },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'emit_log', params_schema: { message: { type: 'string', required: true, token_ref_ok: true } } },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'close_position', params_schema: {} },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'cancel_all_orders', params_schema: {} },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

function suggest(text, cursor = text.length) {
  return suggestAgentCliAt(text, cursor, CATALOG);
}

describe('suggestAgentCliAt() — bare-name position', () => {
  it('WHEN clause: suggests metric names only (not channels/actions)', () => {
    const r = suggest('WHEN mean_');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('mean_pnl');
    expect(r.suggestions).not.toContain('telegram');
    expect(r.suggestions).not.toContain('place_order');
  });

  it('ALERT clause: suggests channel names only (not metrics/actions)', () => {
    const r = suggest('WHEN always ALERT tel');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toEqual(['telegram']);
  });

  it('DO clause: suggests action names only (not metrics/channels)', () => {
    const r = suggest('WHEN always ALERT nop DO place_');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('place_order');
    expect(r.suggestions).not.toContain('mean_pnl');
    expect(r.suggestions).not.toContain('telegram');
  });

  it('bare order statement (no WHEN at all): suggests action names', () => {
    const r = suggest('close_');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('close_position');
  });

  it('replaceRange covers exactly the partial word typed so far', () => {
    const text = 'WHEN mean_';
    const r = suggest(text);
    expect(r.replaceRange).toEqual([5, text.length]);
  });

  it('fresh gap right after "WHEN " (no partial yet) still suggests metrics', () => {
    const r = suggest('WHEN ');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('mean_pnl');
  });

  it('fresh gap right after "ALERT " (no partial yet) still suggests channels', () => {
    const r = suggest('WHEN always ALERT ');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('telegram');
  });

  it('fresh gap right after "DO " (no partial yet) still suggests actions', () => {
    const r = suggest('WHEN always ALERT nop DO ');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toContain('place_order');
  });

  it('gap right after a comparator ("<= ") is NOT a fresh name position', () => {
    const r = suggest('WHEN pnl@positions.total <= ');
    expect(r.suggestions).toEqual([]);
    expect(r.kind).toBeNull();
  });
});

describe('suggestAgentCliAt() — call arg-list position', () => {
  it('right after "(" suggests the call\'s own param names', () => {
    const r = suggest('WHEN always ALERT nop DO place_order(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(expect.arrayContaining(['account', 'symbol', 'exchange', 'side', 'qty']));
  });

  it('right after "," suggests remaining param names, excluding ones already used', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", ';
    const r = suggest(text);
    expect(r.kind).toBe('param');
    expect(r.suggestions).not.toContain('account');
    expect(r.suggestions).toContain('symbol');
    expect(r.suggestions).toContain('side');
  });

  it('mid-typing a param key filters by fuzzy match', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", sym';
    const r = suggest(text);
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(['symbol']);
  });

  it('inside an already-closed call (cursor moved back in), still suggests unused params', () => {
    const full = 'WHEN always ALERT nop DO place_order(account="ZG0790", )';
    const cursor = full.indexOf(', ') + 2; // right after the comma+space, before ")"
    const r = suggestAgentCliAt(full, cursor, CATALOG);
    expect(r.kind).toBe('param');
    expect(r.suggestions).not.toContain('account');
    expect(r.suggestions).toContain('symbol');
  });

  it('works for a metric call\'s own params (mean_pnl)', () => {
    const r = suggest('WHEN mean_pnl(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(['minutes']);
  });
});

describe('suggestAgentCliAt() — enum value position', () => {
  it('right after "<enumParam>=" suggests that param\'s enum values', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", side=';
    const r = suggest(text);
    expect(r.kind).toBe('enum');
    expect(r.suggestions).toEqual(expect.arrayContaining(['BUY', 'SELL']));
  });

  it('mid-typing an enum value filters by fuzzy match', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", side=SE';
    const r = suggest(text);
    expect(r.kind).toBe('enum');
    expect(r.suggestions).toEqual(['SELL']);
  });

  it('a non-enum param (e.g. account=) after "=" suggests nothing', () => {
    const text = 'WHEN always ALERT nop DO place_order(account=';
    const r = suggest(text);
    expect(r.kind).toBeNull();
    expect(r.suggestions).toEqual([]);
  });
});

describe('suggestAgentCliAt() — scope-ref position (right after "@" or ".")', () => {
  it('immediately after "@" suggests scope names only (not metrics/channels/actions)', () => {
    const r = suggest('WHEN mean_pnl(minutes=5)@');
    expect(r.kind).toBe('scope');
    expect(r.suggestions).toEqual(expect.arrayContaining([
      'positions.total', 'positions.any_acct', 'funds.total', 'funds.any_acct',
    ]));
    expect(r.suggestions).not.toContain('mean_pnl');
    expect(r.suggestions).not.toContain('telegram');
    expect(r.suggestions).not.toContain('place_order');
  });

  it('replaceRange right after "@" is a zero-width insertion point', () => {
    const text = 'WHEN mean_pnl(minutes=5)@';
    const r = suggest(text);
    expect(r.replaceRange).toEqual([text.length, text.length]);
  });

  it('immediately after "." in a dotted scope suggests only matching next segment(s)', () => {
    const r = suggest('WHEN mean_pnl(minutes=5)@positions.');
    expect(r.kind).toBe('scope');
    expect(r.suggestions).toEqual(expect.arrayContaining(['positions.total', 'positions.any_acct']));
    expect(r.suggestions).not.toContain('funds.total');
    expect(r.suggestions).not.toContain('funds.any_acct');
  });

  it('replaceRange after "." spans the WHOLE scope_ref typed so far (from right after "@")', () => {
    const text = 'WHEN mean_pnl(minutes=5)@positions.';
    const r = suggest(text);
    const scopeRefStart = text.indexOf('@') + 1;
    expect(r.replaceRange).toEqual([scopeRefStart, text.length]);
  });

  it('mid-typing a second segment keeps the whole scope_ref in replaceRange and filters by fuzzy match', () => {
    const text = 'WHEN mean_pnl(minutes=5)@positions.an';
    const r = suggest(text);
    expect(r.kind).toBe('scope');
    expect(r.suggestions).toEqual(['positions.any_acct']);
    const scopeRefStart = text.indexOf('@') + 1;
    expect(r.replaceRange).toEqual([scopeRefStart, text.length]);
  });

  it('fuzzy ranking: exact-prefix scope match ranks above a non-contiguous subsequence match', () => {
    const extraCatalog = buildCatalog([
      ...FIXTURE_ROWS,
      { grammar_kind: 'condition', token_kind: 'scope', token: 'xpositions.total', params_schema: null },
    ]);
    const text = 'WHEN mean_pnl(minutes=5)@positions.t';
    const r = suggestAgentCliAt(text, text.length, extraCatalog);
    expect(r.kind).toBe('scope');
    expect(r.suggestions[0]).toBe('positions.total');
    expect(r.suggestions).toContain('xpositions.total');
  });

  it('a "." that is NOT part of a real scope_ref (e.g. a bare decimal literal) still degrades to empty, no throw', () => {
    const r = suggest('WHEN pnl@positions.total <= 3.');
    expect(r.suggestions).toEqual([]);
    expect(r.kind).toBeNull();
  });
});

describe('suggestAgentCliAt() — positions that resolve to nothing', () => {
  it('garbage / unparseable input never throws and degrades to empty', () => {
    expect(() => suggest('WHEN "unterminated string')).not.toThrow();
    const r = suggest('WHEN "unterminated string');
    expect(r.suggestions).toEqual([]);
  });

  it('cursor outside any recognizable position (unknown bare call name) returns empty', () => {
    const r = suggest('WHEN always ALERT nop DO totally_unknown_fn(');
    expect(r.suggestions).toEqual([]);
    expect(r.kind).toBeNull();
  });

  it('non-string text / nullish cursor degrades to empty, never throws', () => {
    expect(() => suggestAgentCliAt(null, 0, CATALOG)).not.toThrow();
    expect(suggestAgentCliAt(null, 0, CATALOG).suggestions).toEqual([]);
  });
});

describe('suggestAgentCliAt() — fuzzy ranking order', () => {
  it('exact-prefix match ranks above pure-subsequence match', () => {
    // 'mean_pnl' startsWith('me'); 'max_drawdown_pnl' only matches 'me' as
    // a non-contiguous subsequence (m...e via max_drawdown) — exact-prefix
    // must rank first, same assertion style as commandEngine.test.js.
    const r = suggest('WHEN me');
    expect(r.suggestions[0]).toBe('mean_pnl');
  });

  it('non-matching partial drops all candidates', () => {
    const r = suggest('WHEN zzz_no_such_metric');
    expect(r.suggestions).toEqual([]);
  });
});
