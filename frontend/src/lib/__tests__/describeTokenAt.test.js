/**
 * describeTokenAt.test.js — Vitest coverage for
 * frontend/src/lib/command/grammars/agents.js:describeTokenAt(), the
 * "click an already-typed token to see its description" resolver — see
 * .claude/PLAN.md ("Click-to-describe already-entered CLI tokens") for
 * the full design and the council review that shaped it.
 *
 * Distinct from `suggestAgentCliAt` (live-autocomplete, keyed off the
 * typing cursor + dropdown candidate list) — this resolver answers "what
 * does the token already sitting at this position mean", using a small,
 * self-contained fixture catalog (same `buildCatalog()` pattern as
 * agentsGrammar.test.js/agentsSuggest.test.js).
 *
 * A fixture-only token `sig` is deliberately registered in THREE catalog
 * buckets at once (metric / channel / action) — this is the mechanism
 * used to PROVE clauseKind inference actually drives bucket precedence
 * (`_lookupAnyCatalogEntry`'s own documented per-clause section order),
 * rather than merely asserting a lookup happened to succeed.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const { buildCatalog, compileAgentCliStatement, describeTokenAt } =
  await import('$lib/command/grammars/agents.js');

const FIXTURE_ROWS = [
  // Registered in three buckets under the SAME token name — see file
  // header. Each description is bucket-distinguishable so a resolved
  // result can be asserted against the EXPECTED winning bucket.
  { grammar_kind: 'condition', token_kind: 'metric', token: 'sig', description: 'metric-sig', params_schema: {} },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'sig', description: 'channel-sig', params_schema: {} },
  { grammar_kind: 'action', token_kind: 'action_type', token: 'sig', description: 'action-sig', params_schema: {} },

  { grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', description: 'P&L metric', params_schema: {} },
  { grammar_kind: 'condition', token_kind: 'scope', token: 'positions.total', description: 'All positions', params_schema: {} },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', description: 'Telegram channel', params_schema: {} },
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'place_order', description: 'Place an order',
    params_schema: {
      symbol: { type: 'string', required: true, description: 'Trading symbol' },
      qty: { type: 'number', required: true, description: 'Order quantity' },
    },
  },
  {
    grammar_kind: 'action', token_kind: 'action_type', token: 'emit_log', description: 'Write a log line',
    params_schema: {
      level: { type: 'enum', enum: ['info', 'warning', 'error'], required: false, default: 'info', description: 'Log severity' },
      message: { type: 'string', required: true, description: 'Log message text' },
    },
  },
];

const CATALOG = buildCatalog(FIXTURE_ROWS);

describe('describeTokenAt() — clauseKind inference drives bucket precedence', () => {
  it('WHEN clause resolves the metric bucket first', () => {
    const text = 'WHEN sig <= -1';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'metric-sig', token: 'sig', kind: 'token' });
  });

  it('GIVEN clause ALSO resolves the metric bucket (same section order as WHEN)', () => {
    const text = 'GIVEN sig';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'metric-sig', token: 'sig', kind: 'token' });
  });

  it('ALERT clause resolves the channel bucket first', () => {
    const text = 'ALERT sig';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'channel-sig', token: 'sig', kind: 'token' });
  });

  it('DO clause resolves the action bucket first', () => {
    const text = 'DO sig';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'action-sig', token: 'sig', kind: 'token' });
  });
});

describe('describeTokenAt() — param-key resolution', () => {
  it('resolves a KEY-token inside a CLOSED call via the args-scan path', () => {
    const text = 'DO place_order(symbol="RELIANCE", qty=10)';
    const r = describeTokenAt(text, text.indexOf('qty'), CATALOG);
    expect(r).toEqual({ name: 'qty', description: 'Order quantity', token: 'place_order', kind: 'param' });
  });

  it('resolves the OTHER key (symbol) in the same closed call', () => {
    const text = 'DO place_order(symbol="RELIANCE", qty=10)';
    const r = describeTokenAt(text, text.indexOf('symbol') + 1, CATALOG);
    expect(r).toEqual({ name: 'symbol', description: 'Trading symbol', token: 'place_order', kind: 'param' });
  });

  it('resolves a KEY-token inside a STILL-OPEN call (missing closing paren)', () => {
    const text = 'DO place_order(symbol="RELIANCE", qty=10';
    const r = describeTokenAt(text, text.indexOf('qty'), CATALOG);
    expect(r).toEqual({ name: 'qty', description: 'Order quantity', token: 'place_order', kind: 'param' });
  });

  it('a click landing on a comma/space gap between args resolves to null', () => {
    const text = 'DO place_order(symbol="RELIANCE", qty=10)';
    const r = describeTokenAt(text, text.indexOf(', qty') , CATALOG); // the comma itself
    expect(r).toBeNull();
  });
});

describe('describeTokenAt() — GIVEN-bound label resolution', () => {
  it('a bound label referenced later describes as the binding, not a catalog miss', () => {
    const text = 'GIVEN mylbl = pnl@positions.total WHEN mylbl <= -1';
    const pos = text.lastIndexOf('mylbl') + 1;
    const r = describeTokenAt(text, pos, CATALOG);
    expect(r).toEqual({
      name: 'mylbl',
      description: 'Bound via GIVEN — mylbl = pnl@positions.total',
      token: 'mylbl',
      kind: 'binding',
    });
  });

  it('bound-label resolution does not need a catalog at all', () => {
    const text = 'GIVEN mylbl = 5 WHEN mylbl <= -1';
    const pos = text.lastIndexOf('mylbl') + 1;
    const r = describeTokenAt(text, pos, null);
    expect(r && r.kind).toBe('binding');
  });

  it('clicking the DEFINING occurrence of the label (not a later use) is not described as a binding', () => {
    // At the exact point the label is first typed, `_collectBindingsUpToCursor`
    // (sliced up to `pos`, i.e. BEFORE the "=" is even reached) has not yet
    // registered it — this is semantically correct: the definition site
    // isn't "a reference to itself".
    const text = 'GIVEN mylbl = pnl@positions.total WHEN mylbl <= -1';
    const pos = text.indexOf('mylbl') + 1; // the FIRST (defining) occurrence
    const r = describeTokenAt(text, pos, CATALOG);
    expect(r === null || r.kind !== 'binding').toBe(true);
  });
});

describe('describeTokenAt() — null/empty catalog never throws', () => {
  it('null catalog on a plain metric lookup resolves to null, not a throw', () => {
    const text = 'WHEN pnl <= -1';
    expect(() => describeTokenAt(text, text.indexOf('pnl'), null)).not.toThrow();
    expect(describeTokenAt(text, text.indexOf('pnl'), null)).toBeNull();
  });

  it('an empty-rows catalog behaves the same as null for a plain lookup', () => {
    const emptyCatalog = buildCatalog([]);
    const text = 'WHEN pnl <= -1';
    expect(describeTokenAt(text, text.indexOf('pnl'), emptyCatalog)).toBeNull();
  });
});

describe('describeTokenAt() — two-tier lex fallback', () => {
  it('tier 2: a LATER, unrelated unclosed call does not blank out an earlier token', () => {
    const text = 'DO place_order(symbol="RELIANCE", qty=10) ; unrelated_unclosed(';
    const r = describeTokenAt(text, text.indexOf('qty'), CATALOG);
    expect(r).toEqual({ name: 'qty', description: 'Order quantity', token: 'place_order', kind: 'param' });
  });

  it('tier 3: an unterminated string spanning the click position resolves to null, never a wrong match', () => {
    // The quote opened right after "symbol=" never closes before end of
    // text — a naive raw-text scan could otherwise latch onto "symbol"'s
    // own "=" as the "nearest top-level one" and wrongly describe THAT
    // key instead of refusing to guess.
    const text = 'DO place_order(symbol="RELIANCE, qty=10)';
    const r = describeTokenAt(text, text.indexOf('qty'), CATALOG);
    expect(r).toBeNull();
  });
});

describe('describeTokenAt() — whitespace/punctuation clicks resolve to null', () => {
  it('a click on a plain whitespace gap returns null', () => {
    const text = 'WHEN pnl <= -1 ALERT telegram';
    const r = describeTokenAt(text, text.indexOf(' ALERT'), CATALOG); // the space itself
    expect(r).toBeNull();
  });

  it('a click on a comparator/punctuation token returns null', () => {
    const text = 'WHEN pnl <= -1';
    const r = describeTokenAt(text, text.indexOf('<='), CATALOG);
    expect(r).toBeNull();
  });
});

describe('describeTokenAt() — read-only proof (never mutates text/selection/compile output)', () => {
  it('compileAgentCliStatement output is byte-identical before and after a describeTokenAt call', () => {
    const text = 'WHEN pnl@positions.total <= -1 ALERT telegram DO emit_log(message="hello")';
    const before = compileAgentCliStatement(text, CATALOG);

    // Simulate a burst of clicks across every kind of token in the
    // statement — metric, scope, channel, action, and a param key.
    const positions = [
      text.indexOf('pnl'), text.indexOf('positions.total'), text.indexOf('telegram'),
      text.indexOf('emit_log'), text.indexOf('message'),
    ];
    const textBefore = text;
    for (const pos of positions) {
      describeTokenAt(text, pos, CATALOG);
    }

    const after = compileAgentCliStatement(text, CATALOG);
    expect(text).toBe(textBefore); // the source string itself is a primitive — can't be mutated, but assert anyway for the record
    expect(after).toEqual(before);
    expect(before.ok).toBe(true); // sanity: this really did compile, not fail identically both times
  });

  it('a textarea-shaped before/after selection snapshot is unaffected by describeTokenAt', () => {
    const value = 'WHEN pnl <= -1 ALERT telegram DO emit_log(message="hi")';
    const fakeTextarea = { value, selectionStart: value.indexOf('telegram'), selectionEnd: value.indexOf('telegram') + 'telegram'.length };
    const snapshotBefore = { ...fakeTextarea };

    describeTokenAt(fakeTextarea.value, fakeTextarea.selectionStart, CATALOG);

    expect(fakeTextarea).toEqual(snapshotBefore);
  });
});
