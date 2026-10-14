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

const { buildCatalog, suggestAgentCliAt, parseStatement, lookupAgentCliEntry, isInsideOpenCall } = await import('$lib/command/grammars/agents.js');

const FIXTURE_ROWS = [
  // metrics
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'day_pct', value_type: 'number', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'avail_margin', value_type: 'number', params_schema: null },
  {
    grammar_kind: 'condition', token_kind: 'metric', token: 'mean_pnl', value_type: 'number',
    description: 'Average positions P&L over a lookback window (fixture).',
    params_schema: { minutes: { type: 'number', description: 'Lookback window in minutes (fixture).' } },
  },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'stdev_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  { grammar_kind: 'condition', token_kind: 'metric', token: 'max_drawdown_pnl', value_type: 'number', params_schema: { minutes: { type: 'number' } } },
  // scopes
  {
    grammar_kind: 'condition', token_kind: 'scope', token: 'positions.total', params_schema: null,
    description: 'The single TOTAL row of the positions summary (fixture).',
  },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.any_acct', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.total', params_schema: null },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'funds.any_acct', params_schema: null },
  // channels
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: null,
    description: 'Telegram group defined by secrets.telegram_chat_id (fixture).',
  },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: null },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'log', params_schema: null },
  // actions — place_order mirrors the real agent_grammar.yaml schema
  // (account first, enum fields for exchange/side/order_type/chase_level).
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'place_order',
    params_schema: {
      account: { type: 'string', required: true, token_ref_ok: true, description: 'Masked account id to route the order to (fixture).' },
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

  it('gap right after a comparator ("<= ") is NOT a fresh name position, but now surfaces a value hint (Sprint H item 3)', () => {
    const r = suggest('WHEN pnl@positions.total <= ');
    expect(r.suggestions).toEqual([]);
    expect(r.kind).toBe('value-hint');
    expect(r.hint).toBeTruthy();
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

// ─────────────────────────────────────────────────────────────────────────
// Sprint 11 — catalog `description` surfaced via a `descriptions` array
// parallel to `suggestions` (same order/length), for every suggestion
// context `suggestAgentCliAt` handles.
// ─────────────────────────────────────────────────────────────────────────
describe('suggestAgentCliAt() — descriptions (Sprint 11)', () => {
  it('a token-name suggestion carries its catalog description', () => {
    const r = suggest('WHEN mean_');
    expect(r.descriptions).toBeDefined();
    expect(r.descriptions.length).toBe(r.suggestions.length);
    const idx = r.suggestions.indexOf('mean_pnl');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('Average positions P&L over a lookback window (fixture).');
  });

  it('a channel token-name suggestion (ALERT clause) carries its catalog description', () => {
    const r = suggest('WHEN always ALERT tel');
    const idx = r.suggestions.indexOf('telegram');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('Telegram group defined by secrets.telegram_chat_id (fixture).');
  });

  it('a scope-ref suggestion carries that scope\'s own catalog description', () => {
    const r = suggest('WHEN mean_pnl(minutes=5)@positions.');
    const idx = r.suggestions.indexOf('positions.total');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('The single TOTAL row of the positions summary (fixture).');
  });

  it('a param-name suggestion inside an open call carries that param\'s own description', () => {
    const r = suggest('WHEN always ALERT nop DO place_order(');
    expect(r.kind).toBe('param');
    const idx = r.suggestions.indexOf('account');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('Masked account id to route the order to (fixture).');
  });

  it('a param-name suggestion with no backend-provided description degrades to an empty string, not a crash', () => {
    const r = suggest('WHEN always ALERT nop DO place_order(');
    const idx = r.suggestions.indexOf('symbol'); // fixture's `symbol` param carries no `description` key
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(r.descriptions[idx]).toBe('');
  });

  it('an enum-value suggestion has no per-value description anywhere in the schema — degrades to empty strings, never throws', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", side=';
    expect(() => suggest(text)).not.toThrow();
    const r = suggest(text);
    expect(r.kind).toBe('enum');
    expect(r.suggestions).toEqual(expect.arrayContaining(['BUY', 'SELL']));
    expect(r.descriptions).toBeDefined();
    expect(r.descriptions.length).toBe(r.suggestions.length);
    expect(r.descriptions.every((d) => d === '')).toBe(true);
  });

  it('an empty-result suggestion still carries a (zero-length) descriptions array', () => {
    const r = suggest('WHEN pnl@positions.total <= ');
    expect(r.suggestions).toEqual([]);
    expect(r.descriptions).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Sprint H (2026-10) — CLI autocomplete/dropdown completeness. Each
// `describe` below maps to one numbered item in the sprint brief
// (`.claude/PLAN.md`'s "Sprint H" section). A few cases also assert a
// round-trip sanity check: splicing `suggestions[0]` in at `replaceRange`
// must not make the statement LESS parseable than a hand-written
// equivalent — catching a suggestion that's offered but doesn't actually
// fit grammatically once picked.
// ─────────────────────────────────────────────────────────────────────────

/** Fixture extended with a `holdings.total` scope — the base FIXTURE_ROWS
 *  has no `holdings.*` family at all, so the `hold`/`hld` alias fix (item
 *  11) needs its own catalog to prove anything (there'd be nothing to
 *  alias onto otherwise). */
const CATALOG_WITH_HOLDINGS = buildCatalog([
  ...FIXTURE_ROWS,
  { grammar_kind: 'condition', token_kind: 'scope', token: 'holdings.total', params_schema: null, description: 'Holdings TOTAL row (fixture).' },
]);

describe('suggestAgentCliAt() — Sprint H item 1: statement-head keywords', () => {
  it('very start of a statement suggests GIVEN/WHEN/ALERT/DO alongside bare action names', () => {
    const r = suggest('');
    expect(r.suggestions).toEqual(expect.arrayContaining(['GIVEN', 'WHEN', 'ALERT', 'DO']));
  });

  it('a partially-typed clause-head keyword at the very start is completable', () => {
    const r = suggest('ALE');
    expect(r.suggestions).toContain('ALERT');
    expect(r.replaceRange).toEqual([0, 3]);
  });

  it('after a complete WHEN condition (no ALERT/DO yet) suggests ALERT/DO and "&"/"|", not WHEN/GIVEN again', () => {
    const r = suggest('WHEN pnl@positions.total <= -5 ');
    expect(r.kind).toBe('keyword');
    expect(r.suggestions).toEqual(expect.arrayContaining(['ALERT', 'DO', '&', '|']));
    expect(r.suggestions).not.toContain('WHEN');
    expect(r.suggestions).not.toContain('GIVEN');
  });

  it('after a complete ALERT clause (no DO yet) suggests DO, not ALERT again, and no "&"/"|" (condition already sealed)', () => {
    const r = suggest('WHEN pnl@positions.total <= -5 ALERT telegram ');
    expect(r.kind).toBe('keyword');
    expect(r.suggestions).toContain('DO');
    expect(r.suggestions).not.toContain('ALERT');
    expect(r.suggestions).not.toContain('&');
  });

  it('round-trip: picking "ALERT" after a complete condition, then completing the rest, parses cleanly', () => {
    const text = 'WHEN pnl@positions.total <= -5 ';
    const r = suggest(text);
    const picked = text + 'ALERT';
    const completed = `${picked} telegram DO nop`;
    const { errors } = parseStatement(completed);
    expect(errors).toEqual([]);
    expect(r.suggestions).toContain('ALERT');
  });

  it('bug fix (2026-10): a fully-typed clause-head keyword still shows itself as the matched suggestion, case-insensitively — it used to collapse to empty the instant the last letter was typed', () => {
    const base = 'WHEN pnl@positions.total <= -5 ALERT telegram ';
    // One letter in: both the bare keyword and the unrelated "debug" call
    // fuzzy-match — this part always worked.
    expect(suggest(base + 'D').suggestions).toEqual(expect.arrayContaining(['DO']));
    // Fully typed, any case — this collapsed to [] before the fix.
    expect(suggest(base + 'Do').suggestions).toEqual(['DO']);
    expect(suggest(base + 'do').suggestions).toEqual(['DO']);
    expect(suggest(base + 'DO').suggestions).toEqual(['DO']);
  });
});

describe('suggestAgentCliAt() — Sprint H item 2/3: comparators (incl. in/not in) + post-comparator hint', () => {
  it('right after a complete metric_ref with no comparator yet, suggests the full comparator set including in/not in', () => {
    const r = suggest('WHEN pnl@positions.total ');
    expect(r.kind).toBe('comparator');
    expect(r.suggestions).toEqual(expect.arrayContaining(['<', '<=', '>', '>=', '==', '!=', 'in', 'not in']));
  });

  it('right after a complete bare col_ref chain (Sprint 15 dot-access, no "@") also suggests comparators', () => {
    const r = suggest('WHEN positions.total.pnl ');
    expect(r.kind).toBe('comparator');
    expect(r.suggestions).toContain('in');
    expect(r.suggestions).toContain('not in');
  });

  it('mid-typing a word comparator ("i") after a complete ref offers only the word-shaped comparators (in / not in)', () => {
    const r = suggest('WHEN pnl@positions.total i');
    expect(r.kind).toBe('comparator');
    expect(r.suggestions).toEqual(expect.arrayContaining(['in']));
    expect(r.suggestions.every((s) => s === 'in' || s === 'not in')).toBe(true);
  });

  it('round-trip: picking "in" then supplying a list literal parses cleanly', () => {
    const text = 'WHEN pnl@positions.total ';
    const picked = `${text}in [1, 2]`;
    const { errors } = parseStatement(picked);
    expect(errors).toEqual([]);
  });

  it('right after a comparator token, no catalog completion exists, but a value-position hint is surfaced instead of silence', () => {
    const r = suggest('WHEN pnl@positions.total <= ');
    expect(r.suggestions).toEqual([]);
    expect(r.kind).toBe('value-hint');
    expect(typeof r.hint).toBe('string');
    expect(r.hint.length).toBeGreaterThan(0);
  });
});

describe('suggestAgentCliAt() — Sprint H item 4: order(/bracket(/token( param suggestions', () => {
  it('"order(" (the alias, not "place_order") now gets real param suggestions, including lots=/bracket= sugar', () => {
    const r = suggest('WHEN always ALERT nop DO order(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(expect.arrayContaining(['account', 'symbol', 'side', 'qty', 'lots', 'bracket', 'tp_pct', 'sl_pct']));
  });

  it('"place_order(" gets the SAME augmented param list as "order(" (not just the bare catalog schema)', () => {
    const r = suggest('WHEN always ALERT nop DO place_order(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(expect.arrayContaining(['lots', 'bracket', 'tp_pct', 'sl_pct', 'wing_premium_pct']));
  });

  it('a bare standalone "order(" (no WHEN/DO at all) also gets param suggestions', () => {
    const r = suggest('order(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(expect.arrayContaining(['account', 'symbol', 'lots']));
  });

  it('"bracket(" gets its TP/SL/Wing/applies_to param list — previously ZERO suggestions', () => {
    const r = suggest('bracket(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(expect.arrayContaining(['tp_pct', 'sl_pct', 'wing_premium_pct', 'wing_strike_offset', 'tp_order_type', 'applies_to']));
  });

  it('"token(" gets its "kind" param — previously ZERO suggestions', () => {
    const r = suggest('token(');
    expect(r.kind).toBe('param');
    expect(r.suggestions).toEqual(['kind']);
  });

  it('"token(kind=" only suggests the one real compiling value ("notify"), not "condition" (a guaranteed compile error)', () => {
    const r = suggest('token(kind=');
    expect(r.kind).toBe('enum');
    expect(r.suggestions).toEqual(['notify']);
  });

  it('round-trip: a bracket(...) built entirely from suggested params compiles/parses', () => {
    const text = 'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="x"';
    const { errors } = parseStatement(text);
    expect(errors).toEqual([]);
  });
});

describe('suggestAgentCliAt() — Sprint H item 5: "#" meta-tag suggestions', () => {
  it('a bare trailing "#" (nothing typed yet) suggests all four meta keys', () => {
    const r = suggest('WHEN always ALERT nop DO nop #');
    expect(r.kind).toBe('meta');
    expect(r.suggestions).toEqual(expect.arrayContaining(['name=', 'desc=', 'tag=', 'group=']));
  });

  it('a partially-typed meta key ("#na") filters to the matching key only', () => {
    const text = 'WHEN always ALERT nop DO nop #na';
    const r = suggest(text);
    expect(r.kind).toBe('meta');
    expect(r.suggestions).toEqual(['name=']);
    expect(r.replaceRange).toEqual([text.indexOf('#') + 1, text.length]);
  });

  it('an already-used meta key is excluded from the next "#" suggestion', () => {
    const r = suggest('WHEN always ALERT nop DO nop #name="x" #');
    expect(r.suggestions).not.toContain('name=');
    expect(r.suggestions).toEqual(expect.arrayContaining(['desc=', 'tag=', 'group=']));
  });

  it('round-trip: picking "name=" and supplying a value parses cleanly', () => {
    const text = 'WHEN always ALERT nop DO nop #';
    const picked = `${text}name="hello"`;
    const { errors } = parseStatement(picked);
    expect(errors).toEqual([]);
  });
});

describe('suggestAgentCliAt() — Sprint H item 6: WHEN suggests dot-access scope roots', () => {
  it('right after "WHEN " suggests scope-family roots (positions/funds) alongside metric names', () => {
    const r = suggest('WHEN ');
    expect(r.suggestions).toEqual(expect.arrayContaining(['positions', 'funds', 'mean_pnl']));
  });

  it('typing "positions." (bare dot-access, no "@") still completes to the real scope tokens', () => {
    const r = suggest('WHEN positions.');
    expect(r.suggestions).toEqual(expect.arrayContaining(['positions.total', 'positions.any_acct']));
  });
});

describe('suggestAgentCliAt() — Sprint H item 7: GIVEN suggestable, and its own item-head is debug-only', () => {
  it('GIVEN appears in the statement-head keyword set', () => {
    const r = suggest('');
    expect(r.suggestions).toContain('GIVEN');
  });

  it('right after "GIVEN " (fresh item head) suggests ONLY debug — not metrics (GIVEN items are assignments/debug only)', () => {
    const r = suggest('GIVEN ');
    expect(r.suggestions).toEqual(['debug']);
  });
});

describe('suggestAgentCliAt() — Sprint [comma-separator]: live-typing matches the grammar\'s own comma acceptance', () => {
  it('right after a "," ending a GIVEN assignment, suggests ONLY debug (GIVEN item-head restriction) — identical to ";"/newline, not a bare metric list', () => {
    // Regression guard for a real bug this comma-separator sprint
    // introduced and then fixed: `_bareNameCandidates`'s GIVEN item-head
    // detection originally checked only for a literal SEMI token, so
    // right after the NEWLY-accepted comma it fell through to the
    // broader metric/scope-root candidate set — offering suggestions
    // that would compile to an INVALID GIVEN item if picked (a bare
    // metric is not a legal top-level GIVEN item). Fixed by widening
    // that same detection to treat COMMA identically to SEMI.
    const viaComma = suggest('GIVEN a1 = 1, ');
    const viaSemi = suggest('GIVEN a1 = 1; ');
    expect(viaComma.suggestions).toEqual(['debug']);
    expect(viaComma.suggestions).toEqual(viaSemi.suggestions);
  });

  it('right after a "," ending an ALERT assignment, suggests fresh channel/action candidates (not just more call params)', () => {
    const r = suggest('WHEN always ALERT a = 1, ');
    expect(r.kind).toBe('token');
    expect(r.suggestions).toEqual(expect.arrayContaining(['telegram']));
  });
});

describe('suggestAgentCliAt() — Sprint H item 8: bound labels suggested wherever a value/condition is valid', () => {
  it('a CONDITION-kind label is suggested as a whole bare WHEN leaf', () => {
    const r = suggest('GIVEN x = pnl@positions.total <= -5 WHEN ');
    expect(r.suggestions).toContain('x');
  });

  it('a PRIMITIVE(value)-kind label is NOT suggested as a whole bare WHEN leaf (would be a parse error if picked)', () => {
    const r = suggest('GIVEN x = 5 WHEN ');
    expect(r.suggestions).not.toContain('x');
  });

  it('right after a label-bind\'s own "=" (given_rhs position), suggests metrics + scope roots', () => {
    const r = suggest('GIVEN x = ');
    expect(r.suggestions).toEqual(expect.arrayContaining(['mean_pnl', 'positions', 'funds']));
  });

  it('a PRIMITIVE-kind label is suggested as an ordinary call param value, alongside real enum values', () => {
    const text = 'GIVEN lvl = HIGH WHEN always ALERT nop DO order(account="Z", symbol="N", side=SELL, chase_level=';
    const r = suggest(text);
    expect(r.suggestions).toEqual(expect.arrayContaining(['LOW', 'MED', 'HIGH', 'lvl']));
  });

  it('round-trip: a condition-kind label used as a whole WHEN condition compiles with no errors', () => {
    const text = 'GIVEN x = pnl@positions.total <= -5 WHEN x ALERT telegram';
    const { errors } = parseStatement(text);
    expect(errors).toEqual([]);
  });
});

describe('suggestAgentCliAt() — Sprint H item 9: debug(...) suggested in GIVEN/ALERT/DO, and its own arg position', () => {
  it('"debug" is offered as a candidate right after ALERT', () => {
    const r = suggest('WHEN always ALERT ');
    expect(r.suggestions).toContain('debug');
  });

  it('"debug" is offered as a candidate right after DO', () => {
    const r = suggest('WHEN always ALERT nop DO ');
    expect(r.suggestions).toContain('debug');
  });

  it('inside debug(...)\'s own parens, suggests bound labels', () => {
    const r = suggest('GIVEN x = 5 WHEN always ALERT nop DO debug(');
    expect(r.suggestions).toContain('x');
  });

  it('round-trip: debug(x) on a bound label compiles with no errors', () => {
    const text = 'GIVEN x = 5 WHEN always ALERT nop DO debug(x)';
    const { errors } = parseStatement(text);
    expect(errors).toEqual([]);
  });
});

describe('suggestAgentCliAt() — Sprint H item 10: kind:/underlying: (and account:/symbol:/exchange:) qualifiers', () => {
  it('typing "positions." offers the scope qualifier keys alongside deeper scope names', () => {
    const r = suggest('WHEN positions.');
    expect(r.suggestions).toEqual(expect.arrayContaining([
      'positions.account:', 'positions.symbol:', 'positions.exchange:', 'positions.kind:', 'positions.underlying:',
    ]));
  });

  it('round-trip: a leaf built with the underlying: qualifier parses cleanly', () => {
    const text = 'WHEN positions.underlying:"NIFTY".pnl <= -5';
    const { errors } = parseStatement(text);
    expect(errors).toEqual([]);
  });
});

describe('suggestAgentCliAt() — Sprint H item 11: "hold" alias + funds.cash/funds.margin', () => {
  it('"hold." resolves through the alias to the real "holdings" family, same as "holdings." would', () => {
    const r = suggestAgentCliAt('WHEN hold.', 'WHEN hold.'.length, CATALOG_WITH_HOLDINGS);
    expect(r.suggestions).toEqual(expect.arrayContaining(['holdings.total']));
  });

  it('"funds." offers funds.cash/funds.margin column-alias shortcuts alongside the real scope tokens', () => {
    const r = suggest('WHEN funds.');
    expect(r.suggestions).toEqual(expect.arrayContaining(['funds.cash', 'funds.margin', 'funds.total']));
  });

  it('round-trip: funds.cash as a leaf compiles/parses cleanly', () => {
    const text = 'WHEN funds.cash <= 0';
    const { errors } = parseStatement(text);
    expect(errors).toEqual([]);
  });
});

// Sprint [CLI dropdown] (2026-10) — `lookupAgentCliEntry`/`isInsideOpenCall`
// are thin public pass-throughs to this file's own internal
// `_lookupAnyCatalogEntry`/`_findOpenCallAt`, exported specifically so
// `AgentCliEditor.svelte`'s auto-advance logic can reuse the EXACT same
// resolution rules `suggestAgentCliAt` itself uses internally, rather than
// re-deriving either from scratch. Covered here because they're new
// exported surface area; the actual auto-advance DECISION built on top of
// them (e.g. "open a paren only when a param is required") is UI-layer
// logic in AgentCliEditor.svelte, covered by the Playwright spec for this
// feature, not here.
describe('lookupAgentCliEntry()', () => {
  it('resolves a metric name to its real catalog entry (paramKeys/paramsSchema intact)', () => {
    const entry = lookupAgentCliEntry('mean_pnl', CATALOG);
    expect(entry).not.toBeNull();
    expect(entry.token).toBe('mean_pnl');
    expect(entry.paramKeys).toEqual(['minutes']);
  });

  it('resolves a zero-param channel to an entry with an empty paramKeys list', () => {
    const entry = lookupAgentCliEntry('telegram', CATALOG);
    expect(entry).not.toBeNull();
    expect(entry.paramKeys).toEqual([]);
  });

  it('resolves "order" through the same place_order alias/augmentation `suggestAgentCliAt` uses', () => {
    const entry = lookupAgentCliEntry('order', CATALOG);
    expect(entry).not.toBeNull();
    expect(entry.paramKeys).toEqual(expect.arrayContaining(['account', 'symbol', 'side', 'qty', 'lots']));
    // account/symbol/side/qty are all `required: true` in the fixture —
    // this is the exact flag AgentCliEditor's auto-advance reads to decide
    // whether to auto-open `(`.
    expect(entry.paramsSchema.account.required).toBe(true);
  });

  it('returns null for a name not present in any catalog section', () => {
    expect(lookupAgentCliEntry('not_a_real_token', CATALOG)).toBeNull();
  });

  it('returns null when catalog is not yet loaded', () => {
    expect(lookupAgentCliEntry('telegram', null)).toBeNull();
  });
});

describe('isInsideOpenCall()', () => {
  it('true when the cursor sits inside a call\'s still-open argument list', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790", ';
    expect(isInsideOpenCall(text, text.length)).toBe(true);
  });

  it('false once the call has been closed', () => {
    const text = 'WHEN always ALERT nop DO place_order(account="ZG0790") ';
    expect(isInsideOpenCall(text, text.length)).toBe(false);
  });

  it('false at a bare top-level position (no open call at all)', () => {
    const text = 'WHEN mean_pnl';
    expect(isInsideOpenCall(text, text.length)).toBe(false);
  });

  it('false for a bare grouping "(" (boolean sub-expression, not a call)', () => {
    const text = 'WHEN (mean_pnl';
    expect(isInsideOpenCall(text, text.length)).toBe(false);
  });
});
