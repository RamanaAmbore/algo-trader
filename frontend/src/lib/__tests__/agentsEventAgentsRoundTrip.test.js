/**
 * agentsEventAgentsRoundTrip.test.js — Wave 2 (grammar-unification)
 * byte-identical correctness gate.
 *
 * For every one of the 23 real `SEEDED_AGENTS` (backend/api/algo/
 * event_agents.py — snapshotted into fixtures/seededEventAgents.json,
 * guarded against drift by backend/tests/
 * test_seeded_event_agents_frontend_fixture.py), this file:
 *   1. Decompiles the stored `{conditions, events, actions}` shape to the
 *      NEW `WHEN log(...) DO alert(...), render(...)` CLI text via the
 *      generic `decompileAgent` (the dedicated `decompileEventAgent` is
 *      retired — Wave 2 dissolved it).
 *   2. Recompiles that text.
 *   3. Asserts the recompiled `{conditions, events, actions}` is
 *      STRUCTURALLY IDENTICAL (`toEqual`, which ignores object key order —
 *      the same thing `json.dumps(..., sort_keys=True)` canonicalizes for
 *      in `compute_seed_spec_hash()`) to the original stored shape.
 *
 * This is the load-bearing gate: `compute_seed_spec_hash()` hashes exactly
 * `(conditions, events, actions)`, so any drift here would misclassify an
 * already-seeded row as operator-modified on the next SEED_VERSION bump.
 */
import { describe, it, expect, vi } from 'vitest';
import seededAgents from './fixtures/seededEventAgents.json';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null), listExchangesForSymbol: vi.fn(() => []) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const {
  buildCatalog, compileAgentCliStatement, decompileAgent,
} = await import('$lib/command/grammars/agents.js');

const FIXTURE_ROWS = [
  { grammar_kind: 'notify', token_kind: 'channel', token: 'telegram', params_schema: { gate: { type: 'boolean', required: false } } },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'ntfy', params_schema: { priority: { type: 'enum', enum: ['low', 'default', 'high', 'urgent'], required: false }, gate: { type: 'boolean', required: false } } },
  { grammar_kind: 'notify', token_kind: 'channel', token: 'email', params_schema: { gate: { type: 'boolean', required: false } } },
  { grammar_kind: 'action', token_kind: 'render_action', token: 'render', params_schema: { render: { type: 'freetext', required: true }, gate: { type: 'boolean', required: false }, channel_key: { type: 'freetext', required: false } } },
  {
    grammar_kind: 'condition', token_kind: 'log_filter', token: 'log', value_type: 'void',
    params_schema: {
      tag: { type: 'freetext', required: true },
      min_level: { type: 'enum', enum: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default: 'INFO' },
      where: { type: 'object', required: false },
    },
  },
];
const CATALOG = buildCatalog(FIXTURE_ROWS);

function compile(text, opts = {}) {
  return compileAgentCliStatement(text, CATALOG, opts);
}

describe('Wave 2 — byte-identical compile gate: all 10 real SEEDED_AGENTS shapes', () => {
  // 2026-10-25 alert-channel collapse (commit 7d2b51fc): 23 -> 10 seeded
  // kind='event' agents. See CLAUDE.md's "Alert-channel collapse" entry.
  expect(seededAgents.length).toBe(10);

  for (const agent of seededAgents) {
    it(`${agent.slug}: decompile -> recompile reproduces the EXACT stored conditions/events/actions`, () => {
      const dec = decompileAgent(
        { conditions: agent.conditions, events: agent.events, actions: agent.actions, name: agent.name },
        CATALOG,
      );
      expect(dec.errors).toEqual([]);
      expect(dec.text).toBeTruthy();

      const recompiled = compile(dec.text);
      expect(recompiled.ok).toBe(true);
      expect(recompiled.kind).toBe('event');
      expect(recompiled.agent.kind).toBe('event');
      // The load-bearing comparison — structurally identical to the real
      // stored shape `compute_seed_spec_hash()` hashes.
      expect(recompiled.agent.conditions).toEqual(agent.conditions);
      expect(recompiled.agent.events).toEqual(agent.events);
      expect(recompiled.agent.actions).toEqual(agent.actions);
    });
  }

  it('covers both condition shapes present in the real catalog: bare {"log":...} and {"any":[{"log":...}, ...]}', () => {
    const hasPlainLog = seededAgents.some((a) => 'log' in a.conditions);
    const hasAnyOfLogs = seededAgents.some((a) => 'any' in a.conditions);
    expect(hasPlainLog).toBe(true);
    expect(hasAnyOfLogs).toBe(true);
  });

  it('covers the action-level gate:true shape (error-alert) and a non-empty events shape (unrouted-alert-catch-all)', () => {
    const errorAlert = seededAgents.find((a) => a.slug === 'error-alert');
    const unrouted = seededAgents.find((a) => a.slug === 'unrouted-alert-catch-all');
    expect(/** @type {any} */ (errorAlert.actions[0]).gate).toBe(true);
    expect(unrouted.events.length).toBeGreaterThan(0);
  });
});
