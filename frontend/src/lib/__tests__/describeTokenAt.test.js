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
    // item 7 (backend) — DSL-visible token renamed `emit_log` -> `log`.
    grammar_kind: 'action', token_kind: 'action_type', token: 'log', description: 'Write a log line',
    params_schema: {
      level: { type: 'enum', enum: ['info', 'warning', 'error'], required: false, default: 'info', description: 'Log severity' },
      message: { type: 'string', required: true, description: 'Log message text' },
    },
  },
  // 2026-11-03 — the real catalog carries THREE distinct `log`-named rows
  // across three different buckets: the action above (`emit_log` rename),
  // a notify channel ("write to the app log instead of a real
  // notification"), and this one — the condition-kind `log_filter` row
  // (`catalog.logConditions`, a bucket `_lookupAnyCatalogEntry` used to
  // never check at all). Also a real `render_action` row
  // (`catalog.renderAction`, a lone object, never inside `actions`),
  // which the same lookup gap affected identically. Operator report
  // (2026-11-03): editing an existing alert agent, clicking the `log(`
  // condition call showed the WRONG (channel) description, and clicking
  // a param key inside it (or the `render(` action) showed nothing.
  {
    grammar_kind: 'notify', token_kind: 'channel', token: 'log',
    description: 'Write to the app log only.', params_schema: {},
  },
  {
    grammar_kind: 'condition', token_kind: 'log_filter', token: 'log', value_type: 'void',
    description: 'Match log records by tag, minimum level, and exact extra-field values.',
    params_schema: {
      tag: { type: 'freetext', required: true, description: 'Log tag to match.' },
      // 2026-11-04 — real schema (agent_grammar.yaml) now carries a
      // description here too (operator: "why in agents I don't help
      // info on min_level" — it genuinely had none before this fix).
      min_level: { type: 'enum', enum: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default: 'INFO', description: 'Minimum log level to match, inclusive.' },
    },
  },
  {
    grammar_kind: 'action', token_kind: 'render_action', token: 'render',
    description: "kind='event' agent render-action optional params (e.g. gate).",
    params_schema: {
      render: { type: 'freetext', required: true, description: 'The renderer key.' },
      gate: { type: 'boolean', required: false, description: 'Gate flag.' },
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

  it('a bare preamble position (no WHEN/DO yet) ALSO resolves the action bucket (same section order as DO)', () => {
    const text = 'sig';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'action-sig', token: 'sig', kind: 'token' });
  });

  it('ALERT clause (now a plain call, alert(...)) resolves the channel bucket first', () => {
    const text = 'alert(sig)';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'channel-sig', token: 'sig', kind: 'token' });
  });

  it('DO clause resolves the action bucket first', () => {
    const text = 'WHEN always DO sig';
    const r = describeTokenAt(text, text.indexOf('sig'), CATALOG);
    expect(r).toEqual({ name: 'sig', description: 'action-sig', token: 'sig', kind: 'token' });
  });
});

describe('describeTokenAt() — 2026-11-03: condition log(...) vs the same-named channel/action `log`, and the render_action single-entry bucket', () => {
  it('a WHEN-clause log(...) call resolves catalog.logConditions, never the same-named notify channel', () => {
    const text = 'WHEN log(tag=error, min_level=ERROR)';
    const r = describeTokenAt(text, text.indexOf('log('), CATALOG);
    expect(r).toEqual({
      name: 'log',
      description: 'Match log records by tag, minimum level, and exact extra-field values.',
      token: 'log', kind: 'token',
    });
  });

  it('a param key inside the WHEN-clause log(...) call resolves against the CONDITION schema (tag), not the channel\'s empty one', () => {
    const text = 'WHEN log(tag=error, min_level=ERROR)';
    const r = describeTokenAt(text, text.indexOf('tag='), CATALOG);
    expect(r).toEqual({ name: 'tag', description: 'Log tag to match.', token: 'log', kind: 'param' });
  });

  it('the OTHER param key (min_level) inside the same call also resolves, with a real description', () => {
    const text = 'WHEN log(tag=error, min_level=ERROR)';
    const r = describeTokenAt(text, text.indexOf('min_level='), CATALOG);
    expect(r).toEqual({ name: 'min_level', description: 'Minimum log level to match, inclusive.', token: 'log', kind: 'param' });
  });

  it('an ALERT-clause bare log is unaffected — still resolves the notify-channel bucket, not the condition', () => {
    const text = 'alert(log)';
    const r = describeTokenAt(text, text.indexOf('log'), CATALOG);
    expect(r).toEqual({ name: 'log', description: 'Write to the app log only.', token: 'log', kind: 'token' });
  });

  it('a DO-clause bare log is unaffected — still resolves the action bucket, not the condition', () => {
    const text = 'WHEN always DO log(message="hi")';
    const r = describeTokenAt(text, text.indexOf('log('), CATALOG);
    expect(r).toEqual({ name: 'log', description: 'Write a log line', token: 'log', kind: 'token' });
  });

  it('a DO-clause render(...) action name resolves catalog.renderAction (a lone object, not inside `actions`)', () => {
    const text = 'WHEN always DO render(render=error)';
    const r = describeTokenAt(text, text.indexOf('render('), CATALOG);
    expect(r).toEqual({
      name: 'render',
      description: "kind='event' agent render-action optional params (e.g. gate).",
      token: 'render', kind: 'token',
    });
  });

  it('the render= param key inside that call resolves against renderAction\'s own schema', () => {
    const text = 'WHEN always DO render(render=error)';
    const r = describeTokenAt(text, text.indexOf('render=error'), CATALOG);
    expect(r).toEqual({ name: 'render', description: 'The renderer key.', token: 'render', kind: 'param' });
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

describe('describeTokenAt() — preamble-bound label resolution', () => {
  it('a bound label referenced later describes as the binding, not a catalog miss', () => {
    const text = 'mylbl = pnl@positions.total WHEN mylbl <= -1';
    const pos = text.lastIndexOf('mylbl') + 1;
    const r = describeTokenAt(text, pos, CATALOG);
    expect(r).toEqual({
      name: 'mylbl',
      description: 'Bound label — mylbl = pnl@positions.total',
      token: 'mylbl',
      kind: 'binding',
    });
  });

  it('bound-label resolution does not need a catalog at all', () => {
    const text = 'mylbl = 5 WHEN mylbl <= -1';
    const pos = text.lastIndexOf('mylbl') + 1;
    const r = describeTokenAt(text, pos, null);
    expect(r && r.kind).toBe('binding');
  });

  it('clicking the DEFINING occurrence of the label (not a later use) is not described as a binding', () => {
    // At the exact point the label is first typed, `_collectBindingsUpToCursor`
    // (sliced up to `pos`, i.e. BEFORE the "=" is even reached) has not yet
    // registered it — this is semantically correct: the definition site
    // isn't "a reference to itself".
    const text = 'mylbl = pnl@positions.total WHEN mylbl <= -1';
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
    const text = 'WHEN pnl <= -1 DO alert(telegram)';
    const r = describeTokenAt(text, text.indexOf(' DO'), CATALOG); // the space itself
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
    const text = 'WHEN pnl@positions.total <= -1 DO alert(telegram), log(message="hello")';
    const before = compileAgentCliStatement(text, CATALOG);

    // Simulate a burst of clicks across every kind of token in the
    // statement — metric, scope, channel, action, and a param key.
    const positions = [
      text.indexOf('pnl'), text.indexOf('positions.total'), text.indexOf('telegram'),
      text.indexOf('log('), text.indexOf('message'),
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
    const value = 'WHEN pnl <= -1 DO alert(telegram), log(message="hi")';
    const fakeTextarea = { value, selectionStart: value.indexOf('telegram'), selectionEnd: value.indexOf('telegram') + 'telegram'.length };
    const snapshotBefore = { ...fakeTextarea };

    describeTokenAt(fakeTextarea.value, fakeTextarea.selectionStart, CATALOG);

    expect(fakeTextarea).toEqual(snapshotBefore);
  });
});
