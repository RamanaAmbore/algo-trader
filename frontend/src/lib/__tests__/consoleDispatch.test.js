/**
 * consoleDispatch.test.js — Vitest coverage for
 * frontend/src/lib/command/consoleDispatch.js (Sprint 9, /console
 * dispatch layer).
 *
 * Covers:
 *   - classifyConsoleStatement: routes a `buy ...`-style statement to the
 *     orders.js positional grammar, and a WHEN/ALERT/DO/bare
 *     order(.../place_order(.../bracket(... statement to the agents.js
 *     call-style grammar — including a still-being-typed partial call.
 *   - resolveBracketTemplateRefs: a `bracket="slug"` reference resolves
 *     to the correct numeric `template_id` for both a single ticket and
 *     a multi-leg basket; an unknown slug (or a leg carrying both
 *     template_id and template_slug) is a structured error.
 *   - submitConsoleOrder: a no-match slug / a templates-fetch failure
 *     surfaces a structured error and NEVER calls placeTicketOrder or
 *     placeBasket.
 *   - submitConsoleAgent / submitConsoleBracket: thin wrappers over
 *     createAgent / createOrderTemplate, reusing the exact same API
 *     calls the existing UI already makes.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const fetchOrderTemplates = vi.fn();
const placeTicketOrder    = vi.fn();
const placeBasket         = vi.fn();
const createAgent         = vi.fn();
const createOrderTemplate = vi.fn();
const createAgentFragment = vi.fn();
const reloadOrderTemplates = vi.fn();

vi.mock('$lib/api', () => ({
  fetchOrderTemplates: (...a) => fetchOrderTemplates(...a),
  placeTicketOrder:    (...a) => placeTicketOrder(...a),
  placeBasket:         (...a) => placeBasket(...a),
  createAgent:         (...a) => createAgent(...a),
  createOrderTemplate: (...a) => createOrderTemplate(...a),
  createAgentFragment: (...a) => createAgentFragment(...a),
}));
vi.mock('$lib/data/templates', () => ({
  reloadOrderTemplates: (...a) => reloadOrderTemplates(...a),
}));

const {
  classifyConsoleStatement,
  needsTemplateResolution,
  resolveBracketTemplateRefs,
  resolveConsoleOrder,
  submitResolvedConsoleOrder,
  submitConsoleOrder,
  submitConsoleAgent,
  submitConsoleBracket,
  submitConsoleToken,
  slugifyAgentName,
} = await import('$lib/command/consoleDispatch.js');

beforeEach(() => {
  fetchOrderTemplates.mockReset();
  placeTicketOrder.mockReset();
  placeBasket.mockReset();
  createAgent.mockReset();
  createOrderTemplate.mockReset();
  createAgentFragment.mockReset();
  reloadOrderTemplates.mockReset();
});

// ── classifyConsoleStatement ─────────────────────────────────────────────

describe('classifyConsoleStatement', () => {
  it('routes buy/sell/modify/cancel (any case) to the orders grammar', () => {
    expect(classifyConsoleStatement('buy ZG0790 NIFTY 5')).toBe('orders');
    expect(classifyConsoleStatement('Sell ZG0790 NIFTY 5')).toBe('orders');
    expect(classifyConsoleStatement('MODIFY ABC123')).toBe('orders');
    expect(classifyConsoleStatement('cancel ABC123')).toBe('orders');
  });

  it('routes WHEN/ALERT/DO (any case) to the agent-family grammar', () => {
    expect(classifyConsoleStatement('WHEN pnl@positions.total < -1000 ALERT telegram')).toBe('agents');
    expect(classifyConsoleStatement('alert telegram')).toBe('agents');
    expect(classifyConsoleStatement('Do place_order(account="ZG0790")')).toBe('agents');
  });

  it('routes a bare order(/place_order(/bracket(/token( call to the agent-family grammar', () => {
    expect(classifyConsoleStatement('order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=5)')).toBe('agents');
    expect(classifyConsoleStatement('place_order(account="ZG0790")')).toBe('agents');
    expect(classifyConsoleStatement('bracket(tp_pct=20) #name=foo')).toBe('agents');
    // Sprint 14 — without `token` in `_AGENT_CALL_RE`, this statement
    // misclassified as 'legacy' and got POSTed to /api/admin/exec as a
    // shell command instead of reaching the agents.js grammar.
    expect(classifyConsoleStatement('token(kind=notify) telegram #name="x"')).toBe('agents');
  });

  it('classifies a still-being-typed partial call as agent-family', () => {
    expect(classifyConsoleStatement('order(acct=')).toBe('agents');
    expect(classifyConsoleStatement('bracket(')).toBe('agents');
    expect(classifyConsoleStatement('token(kind=')).toBe('agents');
  });

  it('leaves unrelated text (agent interpret / shell) classified as legacy', () => {
    expect(classifyConsoleStatement('agent list')).toBe('legacy');
    expect(classifyConsoleStatement('ls -la')).toBe('legacy');
  });

  it('classifies empty/whitespace-only text as empty', () => {
    expect(classifyConsoleStatement('')).toBe('empty');
    expect(classifyConsoleStatement('   ')).toBe('empty');
    expect(classifyConsoleStatement(null)).toBe('empty');
  });
});

// ── resolveBracketTemplateRefs / needsTemplateResolution ────────────────

const TEMPLATE_ROWS = [
  { id: 101, slug: 'default-bull', name: 'Default Bull' },
  { id: 102, slug: 'default-bear', name: 'Default Bear' },
  // operator-authored custom template — slug is NULL per
  // backend/api/models.py's own documented convention; NOT resolvable by
  // name here (see consoleDispatch.js's header note on this gap).
  { id: 103, slug: null, name: 'My Custom Template' },
];

describe('needsTemplateResolution', () => {
  it('is false when no leg carries a template_slug', () => {
    expect(needsTemplateResolution({ ticket: { account: 'ZG0790' }, basket: null })).toBe(false);
  });
  it('is true when a ticket leg carries a template_slug', () => {
    expect(needsTemplateResolution({ ticket: { template_slug: 'default-bull' }, basket: null })).toBe(true);
  });
  it('is true when any basket leg carries a template_slug', () => {
    const basket = { groups: [{ account: 'A', legs: [{ side: 'BUY' }, { side: 'SELL', template_slug: 'default-bear' }] }] };
    expect(needsTemplateResolution({ ticket: null, basket })).toBe(true);
  });
});

describe('resolveBracketTemplateRefs', () => {
  it('resolves a single-ticket bracket= reference to template_id and strips template_slug', () => {
    const r = /** @type {any} */ (resolveBracketTemplateRefs(
      { ticket: { account: 'ZG0790', template_slug: 'default-bull' }, basket: null },
      TEMPLATE_ROWS,
    ));
    expect(r.ok).toBe(true);
    expect(r.ticket).toEqual({ account: 'ZG0790', template_id: 101 });
    expect(r.ticket.template_slug).toBeUndefined();
  });

  it('resolves bracket= references across every leg of a multi-account basket', () => {
    const basket = {
      groups: [
        { account: 'A', legs: [{ side: 'BUY', template_slug: 'default-bull' }] },
        { account: 'B', legs: [{ side: 'SELL', template_slug: 'default-bear' }, { side: 'BUY' }] },
      ],
    };
    const r = /** @type {any} */ (resolveBracketTemplateRefs({ ticket: null, basket }, TEMPLATE_ROWS));
    expect(r.ok).toBe(true);
    expect(r.basket.groups[0].legs[0]).toEqual({ side: 'BUY', template_id: 101 });
    expect(r.basket.groups[1].legs[0]).toEqual({ side: 'SELL', template_id: 102 });
    expect(r.basket.groups[1].legs[1]).toEqual({ side: 'BUY' }); // untouched — no slug ref
  });

  it('errors on an unknown bracket slug, never returning a resolved payload', () => {
    const r = /** @type {any} */ (resolveBracketTemplateRefs(
      { ticket: { template_slug: 'nonexistent-slug' }, basket: null },
      TEMPLATE_ROWS,
    ));
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/Unknown bracket/);
  });

  it('cannot resolve a custom template by its name — only slug is matched (documented gap)', () => {
    const r = /** @type {any} */ (resolveBracketTemplateRefs(
      { ticket: { template_slug: 'My Custom Template' }, basket: null },
      TEMPLATE_ROWS,
    ));
    expect(r.ok).toBe(false);
  });

  it('errors when a leg carries both an explicit template_id and a bracket= slug', () => {
    const r = /** @type {any} */ (resolveBracketTemplateRefs(
      { ticket: { template_id: 999, template_slug: 'default-bull' }, basket: null },
      TEMPLATE_ROWS,
    ));
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/cannot specify both/);
  });
});

// ── submitConsoleOrder — the no-match / fetch-failure invariants ────────

describe('submitConsoleOrder — bracket resolution gates submission', () => {
  it('resolves bracket= then places a single ticket with template_id substituted', async () => {
    fetchOrderTemplates.mockResolvedValue(TEMPLATE_ROWS);
    placeTicketOrder.mockResolvedValue({ order_id: 'OID1' });
    const compiled = { ticket: { account: 'ZG0790', template_slug: 'default-bull' }, basket: null };
    const r = await submitConsoleOrder(compiled, { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(placeTicketOrder).toHaveBeenCalledTimes(1);
    const sentPayload = placeTicketOrder.mock.calls[0][0];
    expect(sentPayload.template_id).toBe(101);
    expect(sentPayload.template_slug).toBeUndefined();
    expect(sentPayload.mode).toBe('paper');
    expect(sentPayload.source).toBe('command');
  });

  it('resolves bracket= across a basket and calls placeBasket with resolved groups', async () => {
    fetchOrderTemplates.mockResolvedValue(TEMPLATE_ROWS);
    placeBasket.mockResolvedValue({ groups: [] });
    const basket = { groups: [{ account: 'A', legs: [{ side: 'BUY', template_slug: 'default-bull' }] }] };
    const r = await submitConsoleOrder({ ticket: null, basket }, { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(placeBasket).toHaveBeenCalledTimes(1);
    expect(placeBasket.mock.calls[0][0][0].legs[0].template_id).toBe(101);
  });

  it('skips the templates fetch entirely when no leg references a bracket', async () => {
    placeTicketOrder.mockResolvedValue({ order_id: 'OID2' });
    const r = await submitConsoleOrder({ ticket: { account: 'ZG0790' }, basket: null }, { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(fetchOrderTemplates).not.toHaveBeenCalled();
    expect(placeTicketOrder).toHaveBeenCalledTimes(1);
  });

  it('a no-match bracket slug never calls placeTicketOrder or placeBasket', async () => {
    fetchOrderTemplates.mockResolvedValue(TEMPLATE_ROWS);
    const compiled = { ticket: { account: 'ZG0790', template_slug: 'does-not-exist' }, basket: null };
    const r = await submitConsoleOrder(compiled, { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(/** @type {any} */ (r).error).toMatch(/Unknown bracket/);
    expect(placeTicketOrder).not.toHaveBeenCalled();
    expect(placeBasket).not.toHaveBeenCalled();
  });

  it('a templates-fetch failure surfaces a distinct error and never calls the ticket endpoint', async () => {
    fetchOrderTemplates.mockRejectedValue(new Error('network down'));
    const compiled = { ticket: { account: 'ZG0790', template_slug: 'default-bull' }, basket: null };
    const r = await submitConsoleOrder(compiled, { mode: 'paper' });
    expect(r.ok).toBe(false);
    expect(/** @type {any} */ (r).error).toMatch(/Could not load templates/);
    expect(placeTicketOrder).not.toHaveBeenCalled();
    expect(placeBasket).not.toHaveBeenCalled();
  });
});

describe('resolveConsoleOrder + submitResolvedConsoleOrder split (page pre-confirm flow)', () => {
  it('resolving separately from submitting still reaches the broker call once', async () => {
    fetchOrderTemplates.mockResolvedValue(TEMPLATE_ROWS);
    placeTicketOrder.mockResolvedValue({ order_id: 'OID3' });
    const compiled = { ticket: { account: 'ZG0790', template_slug: 'default-bull' }, basket: null };
    const resolved = await resolveConsoleOrder(compiled);
    expect(resolved.ok).toBe(true);
    const r = await submitResolvedConsoleOrder(/** @type {any} */ (resolved), { mode: 'live' });
    expect(r.ok).toBe(true);
    expect(placeTicketOrder).toHaveBeenCalledTimes(1);
    expect(fetchOrderTemplates).toHaveBeenCalledTimes(1); // not refetched twice
  });

  it('a resolution failure never reaches submitResolvedConsoleOrder (caller must check .ok)', async () => {
    fetchOrderTemplates.mockResolvedValue(TEMPLATE_ROWS);
    const compiled = { ticket: { account: 'ZG0790', template_slug: 'nope' }, basket: null };
    const resolved = await resolveConsoleOrder(compiled);
    expect(resolved.ok).toBe(false);
    expect(placeTicketOrder).not.toHaveBeenCalled();
  });
});

// ── submitConsoleAgent ───────────────────────────────────────────────────

describe('submitConsoleAgent', () => {
  it('slugifies #name= and creates the agent via the existing createAgent call', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const agent = {
      name: 'My Loss Guard!', description: 'desc', conditions: { type: 'always' },
      events: [], actions: [{ type: 'emit_log' }], tags: ['x'], group: 'g1',
    };
    const r = await submitConsoleAgent(agent, 'WHEN always DO emit_log() #name="My Loss Guard!"');
    expect(r.ok).toBe(true);
    expect(r.slug).toBe('my-loss-guard');
    expect(createAgent).toHaveBeenCalledTimes(1);
    const payload = createAgent.mock.calls[0][0];
    expect(payload.slug).toBe('my-loss-guard');
    expect(payload.name).toBe('My Loss Guard!');
    expect(payload.conditions).toEqual({ type: 'always' });
    expect(payload.cli_source).toContain('WHEN always');
  });

  it('requires #name= — never calls createAgent without one', async () => {
    const r = await submitConsoleAgent({ conditions: {}, events: [], actions: [] }, 'WHEN always DO emit_log()');
    expect(r.ok).toBe(false);
    expect(createAgent).not.toHaveBeenCalled();
  });

  it('surfaces a 409-style create failure as a structured error (never falls back to update)', async () => {
    createAgent.mockRejectedValue(new Error("agent 'my-loss-guard' already exists"));
    const r = await submitConsoleAgent({ name: 'My Loss Guard', conditions: {}, events: [], actions: [] }, '');
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/already exists/);
  });

  // Sprint 12 — `compileEventStmt` (agents.js) stamps `kind: 'event'`
  // directly on the compiled agent object for a notification (event_stmt)
  // agent; submitConsoleAgent's payload `kind` must follow that, not the
  // old hardcoded 'threshold' literal.
  it("a kind:'event' compiled agent submits with kind:'event' on the payload", async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const eventAgent = {
      kind: 'event',
      name: 'Error alert',
      conditions: { log: { tag: 'error', min_level: 'ERROR' } },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'render', render: 'error' }],
    };
    const r = await submitConsoleAgent(eventAgent, 'ALERT log_tag=error, min_level=ERROR, render=error, telegram #name="Error alert"');
    expect(r.ok).toBe(true);
    expect(createAgent).toHaveBeenCalledTimes(1);
    const payload = createAgent.mock.calls[0][0];
    expect(payload.kind).toBe('event');
    expect(payload.conditions).toEqual({ log: { tag: 'error', min_level: 'ERROR' } });
    expect(payload.actions).toEqual([{ type: 'render', render: 'error' }]);
  });

  // Regression guard — an ordinary (non-event) compiled agent has no
  // `kind` field at all on the object `compileAgentStmt` produces (see
  // agents.js), so this must still submit with the existing 'threshold'
  // literal, byte-for-byte unchanged from before this sprint.
  it("an ordinary non-event compiled agent still submits with kind:'threshold' (regression guard)", async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const ordinaryAgent = {
      name: 'My Loss Guard', conditions: { type: 'always' }, events: [], actions: [{ type: 'emit_log' }],
    };
    const r = await submitConsoleAgent(ordinaryAgent, 'WHEN always DO emit_log() #name="My Loss Guard"');
    expect(r.ok).toBe(true);
    const payload = createAgent.mock.calls[0][0];
    expect(payload.kind).toBe('threshold');
  });
});

describe('slugifyAgentName', () => {
  it('lowercases, hyphenates, and strips non-alphanumerics', () => {
    expect(slugifyAgentName('My Loss Guard!')).toBe('my-loss-guard');
    expect(slugifyAgentName('  weird___name--')).toBe('weird-name');
    expect(slugifyAgentName('')).toBe('agent');
  });
});

// ── submitConsoleBracket ─────────────────────────────────────────────────

describe('submitConsoleBracket', () => {
  it('creates the template via the existing createOrderTemplate call and reloads the cache', async () => {
    createOrderTemplate.mockResolvedValue({ id: 55, slug: null, name: 'My Bracket' });
    const r = await submitConsoleBracket({ name: 'My Bracket', tp_pct: 20, sl_pct: 10 });
    expect(r.ok).toBe(true);
    expect(createOrderTemplate).toHaveBeenCalledWith({ name: 'My Bracket', tp_pct: 20, sl_pct: 10 });
    expect(reloadOrderTemplates).toHaveBeenCalledTimes(1);
  });

  it('surfaces a create failure without reloading the templates cache', async () => {
    createOrderTemplate.mockRejectedValue(new Error("template 'My Bracket' already exists"));
    const r = await submitConsoleBracket({ name: 'My Bracket' });
    expect(r.ok).toBe(false);
    expect(reloadOrderTemplates).not.toHaveBeenCalled();
  });

  it('requires a compiled template — never calls createOrderTemplate with nothing', async () => {
    const r = await submitConsoleBracket(null);
    expect(r.ok).toBe(false);
    expect(createOrderTemplate).not.toHaveBeenCalled();
  });
});

// ── submitConsoleToken (Sprint 14) ──────────────────────────────────────

describe('submitConsoleToken', () => {
  it('creates the fragment via the existing createAgentFragment call', async () => {
    createAgentFragment.mockResolvedValue({ id: 7, kind: 'notify', name: 'my-token' });
    const fragment = { kind: 'notify', name: 'my-token', body: [{ channel: 'telegram', enabled: true }] };
    const r = await submitConsoleToken(fragment);
    expect(r.ok).toBe(true);
    expect(createAgentFragment).toHaveBeenCalledWith(fragment);
  });

  it('surfaces a create failure (e.g. 409 name collision) as a structured error', async () => {
    createAgentFragment.mockRejectedValue(new Error("Fragment notify/my-token already exists"));
    const r = await submitConsoleToken({ kind: 'notify', name: 'my-token', body: [] });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/already exists/);
  });

  it('requires a compiled fragment — never calls createAgentFragment with nothing', async () => {
    const r = await submitConsoleToken(null);
    expect(r.ok).toBe(false);
    expect(createAgentFragment).not.toHaveBeenCalled();
  });
});
