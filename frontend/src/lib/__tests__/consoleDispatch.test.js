/**
 * consoleDispatch.test.js — Vitest coverage for
 * frontend/src/lib/command/consoleDispatch.js (Sprint 9, /console
 * dispatch layer).
 *
 * Covers:
 *   - classifyConsoleStatement: recognizes a WHEN/ALERT/DO/GIVEN/bare
 *     order(.../place_order(.../bracket(.../token(.../cancel_all_orders(...
 *     statement — including a still-being-typed partial call — as
 *     agent-family ('agents'); everything else (the retired positional
 *     buy/sell/modify/cancel grammar, `agent list` interpret text, raw
 *     shell commands, or just unrecognized text) classifies 'unsupported'
 *     (2026-10-17 — the legacy orders.js grammar, CommandBar.svelte and
 *     CommandLineTab.svelte were deleted entirely, operator-confirmed;
 *     see the 2026-10-17 entry in docs/specs/AUTOMATION_SPEC.md's change
 *     log). `bracket(...)` is still matched here (classifies 'agents'),
 *     even though it's now a hard compile error one layer down — see
 *     agents.js.
 *   - submitConsoleOrder: a thin pass-through now (no bracket/template
 *     resolution step — removed 2026-11, Track 3 T3.4, Bracket/
 *     Template/GTT removal).
 *   - submitConsoleAgent: a thin wrapper over createAgent, reusing the
 *     exact same API call the existing UI already makes.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const placeTicketOrder    = vi.fn();
const placeBasket         = vi.fn();
const createAgent         = vi.fn();
const updateAgent         = vi.fn();
const lookupHelpToken = vi.fn();

vi.mock('$lib/api', () => ({
  placeTicketOrder:    (...a) => placeTicketOrder(...a),
  placeBasket:         (...a) => placeBasket(...a),
  createAgent:         (...a) => createAgent(...a),
  updateAgent:         (...a) => updateAgent(...a),
  lookupHelpToken:     (...a) => lookupHelpToken(...a),
}));

const {
  classifyConsoleStatement,
  dispatchConsoleStatement,
  resolveConsoleOrder,
  submitResolvedConsoleOrder,
  submitConsoleOrder,
  submitConsoleAgent,
  submitConsoleToken,
  updateConsoleAgent,
  updateConsoleToken,
  slugifyAgentName,
  submitConsoleHelp,
} = await import('$lib/command/consoleDispatch.js');

// `loadOrderTemplates` is no longer imported at all, since
// `resolveConsoleOrder` no longer needs it — see the "no bracket
// resolution step" describe block below.

beforeEach(() => {
  placeTicketOrder.mockReset();
  placeBasket.mockReset();
  createAgent.mockReset();
  updateAgent.mockReset();
  lookupHelpToken.mockReset();
});

// ── classifyConsoleStatement ─────────────────────────────────────────────

describe('classifyConsoleStatement', () => {
  it('classifies the retired positional buy/sell/modify/cancel grammar as unsupported (any case)', () => {
    expect(classifyConsoleStatement('buy ZG0790 NIFTY 5')).toBe('unsupported');
    expect(classifyConsoleStatement('Sell ZG0790 NIFTY 5')).toBe('unsupported');
    expect(classifyConsoleStatement('MODIFY ABC123')).toBe('unsupported');
    expect(classifyConsoleStatement('cancel ABC123')).toBe('unsupported');
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

  it('classifies unrelated text (agent interpret / shell) as unsupported', () => {
    expect(classifyConsoleStatement('agent list')).toBe('unsupported');
    expect(classifyConsoleStatement('ls -la')).toBe('unsupported');
  });

  it('classifies empty/whitespace-only text as empty', () => {
    expect(classifyConsoleStatement('')).toBe('empty');
    expect(classifyConsoleStatement('   ')).toBe('empty');
    expect(classifyConsoleStatement(null)).toBe('empty');
  });

  // Sprint I Phase 1 (2026-10-11, "unify console's two CLI boxes") —
  // GIVEN (Sprint G's agent_stmt preamble keyword) was missing from
  // `_AGENT_HEAD_RE`, so a `GIVEN x = ...; WHEN x ...` statement
  // misclassified as 'legacy' and would have been POSTed to
  // /api/admin/exec as a shell command.
  it('routes a leading GIVEN (any case) to the agent-family grammar', () => {
    expect(classifyConsoleStatement('GIVEN x = pnl@positions.total; WHEN x < -1000 ALERT telegram')).toBe('agents');
    expect(classifyConsoleStatement('given y = 5 DO nop')).toBe('agents');
  });

  it('classifies a half-typed order( call as agent-family mid-type', () => {
    expect(classifyConsoleStatement('order(acct=')).toBe('agents');
  });

  // Sprint [grammar-unify] (item 4) — `debug(...)`/`help(...)` are now
  // complete, valid, STANDALONE preamble-only statements on their own
  // (not just DO-clause vocabulary) — without them in `_AGENT_CALL_RE`,
  // a bare `help(pnl)` typed into Console would misclassify as
  // 'unsupported' and never reach the real grammar at all.
  it('routes a bare debug(/help( call to the agent-family grammar', () => {
    expect(classifyConsoleStatement('help(pnl)')).toBe('agents');
    expect(classifyConsoleStatement('debug(x)')).toBe('agents');
    expect(classifyConsoleStatement('help(')).toBe('agents'); // still-being-typed
  });

  // Wave 2 (grammar-unification, 2026-10-24) retired the standalone
  // `event_stmt` (`ALERT log_tag=...`, `_EVENT_META_KEYS`) entirely — a
  // bare, non-called leading `ALERT` is now always a specific
  // "removed clause keyword" compile error. This classifier is purely
  // textual (`_AGENT_HEAD_RE` matches ANY bare leading `alert`), so the
  // text still classifies 'agents' regardless — it just won't compile
  // downstream. Asserted here with the OLD (now-invalid) spelling
  // specifically to prove the classifier doesn't care either way.
  it('routes a bare ALERT (even the now-retired event-meta spelling) to the agent-family grammar, classification-only', () => {
    expect(classifyConsoleStatement('ALERT log_tag=error, render=error, telegram')).toBe('agents');
  });

  it('classifies an unrelated shell-style command as unsupported', () => {
    expect(classifyConsoleStatement('docker ps')).toBe('unsupported');
  });
});

describe('dispatchConsoleStatement', () => {
  it('routes agent-family text to onAgentFamily, never onUnsupported', () => {
    const onAgentFamily = vi.fn(() => 'agent-result');
    const onUnsupported = vi.fn();
    const r = dispatchConsoleStatement('order(account="ZG0790")', { onAgentFamily, onUnsupported });
    expect(onAgentFamily).toHaveBeenCalledTimes(1);
    expect(onUnsupported).not.toHaveBeenCalled();
    expect(r).toBe('agent-result');
  });

  it('routes the retired positional grammar (buy/sell/modify/cancel) to onUnsupported', () => {
    const onAgentFamily = vi.fn();
    const onUnsupported = vi.fn(() => 'unsupported-result');
    const r = dispatchConsoleStatement('buy ZG0790 NIFTY 5', { onAgentFamily, onUnsupported });
    expect(onUnsupported).toHaveBeenCalledTimes(1);
    expect(onAgentFamily).not.toHaveBeenCalled();
    expect(r).toBe('unsupported-result');
  });

  it('routes unrelated/shell text to onUnsupported', () => {
    const onUnsupported = vi.fn();
    dispatchConsoleStatement('docker ps', { onUnsupported });
    expect(onUnsupported).toHaveBeenCalledTimes(1);
  });

  it('calls neither callback for empty text', () => {
    const onAgentFamily = vi.fn();
    const onUnsupported = vi.fn();
    const r = dispatchConsoleStatement('   ', { onAgentFamily, onUnsupported });
    expect(onAgentFamily).not.toHaveBeenCalled();
    expect(onUnsupported).not.toHaveBeenCalled();
    expect(r).toBeUndefined();
  });

  it('tolerates missing handlers (no-op, never throws)', () => {
    expect(() => dispatchConsoleStatement('order(account="x")', {})).not.toThrow();
    expect(() => dispatchConsoleStatement('buy x', {})).not.toThrow();
  });
});

// `resolveBracketTemplateRefs` / `needsTemplateResolution` and their test
// coverage removed 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal)
// — `bracket=` on an order leg is now rejected at COMPILE time (see
// agents.js's `compileOrderStmt`), so a compiled leg can never carry a
// `template_slug` field for this layer to resolve.

// ── submitConsoleOrder / resolveConsoleOrder — now a trivial pass-through ──

describe('submitConsoleOrder — no bracket resolution step (removed 2026-11, Track 3 T3.4)', () => {
  it('never fetches the templates catalog — there is nothing left to resolve', async () => {
    placeTicketOrder.mockResolvedValue({ order_id: 'OID2' });
    const r = await submitConsoleOrder({ ticket: { account: 'ZG0790' }, basket: null }, { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(placeTicketOrder).toHaveBeenCalledTimes(1);
  });

  it('a single ticket and a basket both still submit through resolveConsoleOrder + submitResolvedConsoleOrder unchanged', async () => {
    placeBasket.mockResolvedValue({ groups: [] });
    const basket = { groups: [{ account: 'A', legs: [{ side: 'BUY' }] }] };
    const r = await submitConsoleOrder({ ticket: null, basket }, { mode: 'paper' });
    expect(r.ok).toBe(true);
    expect(placeBasket).toHaveBeenCalledTimes(1);
  });
});

describe('resolveConsoleOrder + submitResolvedConsoleOrder split (page pre-confirm flow)', () => {
  it('resolving separately from submitting still reaches the broker call once, with no templates fetch', async () => {
    placeTicketOrder.mockResolvedValue({ order_id: 'OID3' });
    const compiled = { ticket: { account: 'ZG0790' }, basket: null };
    const resolved = await resolveConsoleOrder(compiled);
    expect(resolved.ok).toBe(true);
    expect(resolved.ticket).toEqual(compiled.ticket);
    const r = await submitResolvedConsoleOrder(/** @type {any} */ (resolved), { mode: 'live' });
    expect(r.ok).toBe(true);
    expect(placeTicketOrder).toHaveBeenCalledTimes(1);
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

  // Wave 2 (grammar-unification) — `compileAgentStmt` (agents.js) stamps
  // `kind: 'event'` directly on the compiled agent object when the
  // condition is a `log(...)` leaf (the same field the retiring
  // `compileEventStmt` used to stamp for the old standalone `event_stmt`);
  // submitConsoleAgent's payload `kind` must follow that, not the old
  // hardcoded 'threshold' literal. `rawText` here is only ever stored
  // verbatim as `cli_source` audit text, never recompiled by this
  // function — its exact spelling doesn't affect this test.
  it("a kind:'event' compiled agent submits with kind:'event' on the payload", async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const eventAgent = {
      kind: 'event',
      name: 'Error alert',
      conditions: { log: { tag: 'error', min_level: 'ERROR' } },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [{ type: 'render', render: 'error' }],
    };
    const r = await submitConsoleAgent(eventAgent, 'WHEN log(tag=error, min_level=ERROR) DO alert(telegram), render(render=error) #name="Error alert"');
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

  // Sprint [GIVEN-expr]
  it('sends given: [] (not undefined) when the compiled agent has no GIVEN bindings', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    await submitConsoleAgent({ name: 'x', conditions: {}, events: [], actions: [] }, 'WHEN always DO nop #name="x"');
    const payload = createAgent.mock.calls[0][0];
    expect(payload.given).toEqual([]);
  });

  it('forwards a non-empty compiled "given" array through unchanged, verbatim, in declaration order', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const given = [{ name: 'p', expr: 'pnl@positions.total' }, { name: 'msg_txt', expr: '"Loss: " + p' }];
    await submitConsoleAgent({ name: 'x', conditions: {}, events: [], actions: [], given }, '');
    const payload = createAgent.mock.calls[0][0];
    expect(payload.given).toEqual(given);
  });

  // Sprint [grammar-unify] — `preamble` must get the SAME "always a real
  // array, never undefined" treatment `given` already gets above, for
  // the identical reason (an absent key hits the backend's "None/absent
  // = unchanged" convention on UPDATE — see `updateConsoleAgent`'s tests
  // below for the sharper version of this).
  it('sends preamble: [] (not undefined) when the compiled agent has no preamble calls', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    await submitConsoleAgent({ name: 'x', conditions: {}, events: [], actions: [] }, 'WHEN always DO nop #name="x"');
    const payload = createAgent.mock.calls[0][0];
    expect(payload.preamble).toEqual([]);
  });

  it('forwards a non-empty compiled "preamble" array through unchanged, verbatim, in declaration order', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    const preamble = [
      { type: 'order', params: { account: 'ZG0790', symbol: 'NIFTY25JULFUT', side: 'BUY', qty: 75 } },
      { type: 'alert', events: [{ channel: 'telegram', enabled: true }] },
    ];
    await submitConsoleAgent({ name: 'x', conditions: {}, events: [], actions: [], preamble }, '');
    const payload = createAgent.mock.calls[0][0];
    expect(payload.preamble).toEqual(preamble);
  });

  // Design item 0.C — CREATE has no existing row to be sticky against
  // (unlike UPDATE, see `updateConsoleAgent`'s `has_explicit_alert`
  // describe block), so a brand-new agent trusts the fresh compile
  // unconditionally.
  it('sends has_explicit_alert: true for a genuinely new DO ... alert(...) ... statement', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    await submitConsoleAgent(
      { name: 'x', conditions: {}, events: [{ channel: 'telegram', enabled: true }], actions: [], has_explicit_alert: true },
      'WHEN always DO alert(telegram) #name="x"'
    );
    expect(createAgent.mock.calls[0][0].has_explicit_alert).toBe(true);
  });

  it('sends has_explicit_alert: false when the compiled agent has no DO-level alert() call', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    await submitConsoleAgent({ name: 'x', conditions: {}, events: [], actions: [] }, 'WHEN always DO nop #name="x"');
    expect(createAgent.mock.calls[0][0].has_explicit_alert).toBe(false);
  });
});

describe('slugifyAgentName', () => {
  it('lowercases, hyphenates, and strips non-alphanumerics', () => {
    expect(slugifyAgentName('My Loss Guard!')).toBe('my-loss-guard');
    expect(slugifyAgentName('  weird___name--')).toBe('weird-name');
    expect(slugifyAgentName('')).toBe('agent');
  });
});

// `submitConsoleBracket` and its test coverage removed 2026-11 (Track 3,
// T3.4, Bracket/Template/GTT removal) — `compiled.kind` can never be
// `'bracket'` anymore.

// ── submitConsoleToken (Sprint 14) ──────────────────────────────────────

describe('submitConsoleToken', () => {
  it('creates an Agent(kind="template") row via the unified createAgent call (Sprint D2)', async () => {
    createAgent.mockResolvedValue({ detail: "Agent 'my-token' created" });
    const fragment = { kind: 'notify', name: 'my-token', body: [{ channel: 'telegram', enabled: true }] };
    const r = await submitConsoleToken(fragment, 'token(kind=notify) telegram #name="my-token"');
    expect(r.ok).toBe(true);
    expect(r.slug).toBe('my-token');
    expect(createAgent).toHaveBeenCalledWith({
      slug: 'my-token',
      kind: 'template',
      name: 'my-token',
      description: '',
      conditions: {},
      events: fragment.body,
      actions: [],
      cli_source: 'token(kind=notify) telegram #name="my-token"',
    });
  });

  it('sends the fragment name as the slug UNCHANGED — never slugified', async () => {
    createAgent.mockResolvedValue({ detail: 'ok' });
    await submitConsoleToken({ kind: 'notify', name: 'already-valid-slug', body: [] });
    expect(createAgent.mock.calls[0][0].slug).toBe('already-valid-slug');
  });

  it('surfaces a create failure (e.g. 409 slug collision, or a 400 bad-slug-shape) as a structured error', async () => {
    createAgent.mockRejectedValue(new Error("Agent 'my-token' already exists"));
    const r = await submitConsoleToken({ kind: 'notify', name: 'my-token', body: [] });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/already exists/);
  });

  it('requires a compiled fragment with a name — never calls createAgent with nothing', async () => {
    const r1 = await submitConsoleToken(null);
    expect(r1.ok).toBe(false);
    const r2 = await submitConsoleToken({ kind: 'notify', body: [] });   // no name
    expect(r2.ok).toBe(false);
    expect(createAgent).not.toHaveBeenCalled();
  });
});

// ── updateConsoleAgent / updateConsoleBracket / updateConsoleToken
//    (Sprint C, 2026-10, Phase 2 "Automation redesign") — the Edit→
//    decompile→Console→resave path routes through UPDATE, never CREATE.

describe('updateConsoleAgent', () => {
  it('calls updateAgent(slug, ...) — never createAgent — with only the CLI-expressible fields', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    const compiledAgent = {
      name: 'NIFTY loss guard', description: 'Closes on -50k drawdown',
      conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
      events: [{ channel: 'telegram', enabled: true }],
      actions: [], tags: ['risk'], group: 'hedge-book',
    };
    const r = await updateConsoleAgent('nifty-loss-guard', compiledAgent, 'WHEN ...');
    expect(r.ok).toBe(true);
    expect(r.slug).toBe('nifty-loss-guard');
    expect(createAgent).not.toHaveBeenCalled();
    expect(updateAgent).toHaveBeenCalledWith('nifty-loss-guard', {
      name: 'NIFTY loss guard',
      description: 'Closes on -50k drawdown',
      conditions: compiledAgent.conditions,
      events: compiledAgent.events,
      actions: [],
      // Sprint [GIVEN-expr] — ALWAYS sent as a real array (never
      // `undefined`), same convention as tags/group above — an absent
      // `given` key would hit the backend's "None/absent = unchanged"
      // convention and leave a STALE given[] in place forever once an
      // operator edits a statement to remove every GIVEN line.
      given: [],
      // Sprint [grammar-unify] — ALWAYS sent as a real array (never
      // `undefined`), same convention/reasoning as `given` immediately
      // above.
      preamble: [],
      // Design item 0.C — no `hadExplicitAlert` 4th arg passed here, so
      // it defaults to `false` (sticky-false-unless-already-true — see
      // the dedicated `has_explicit_alert` describe block below for the
      // full danger-scenario coverage).
      has_explicit_alert: false,
      tags: ['risk'],
      group: 'hedge-book',
      cli_source: 'WHEN ...',
    });
  });

  it('sends given: [] (not undefined/omitted) when the compiled agent has no GIVEN bindings at all — required so removing every GIVEN line on edit actually clears stale server-side bindings', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [] }, '');
    const sentPayload = updateAgent.mock.calls[0][1];
    expect(sentPayload.given).toEqual([]);
  });

  it('forwards a non-empty compiled "given" array through unchanged, verbatim, in declaration order', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    const given = [{ name: 'p', expr: 'pnl@positions.total' }, { name: 'msg_txt', expr: '"Loss: " + p' }];
    await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [], given }, '');
    const sentPayload = updateAgent.mock.calls[0][1];
    expect(sentPayload.given).toEqual(given);
  });

  // Sprint [grammar-unify] — the sharper version of the `given` reasoning
  // above: an operator who edits a statement to remove every preamble
  // call and saves must have the OLD preamble[] actually cleared, not
  // left stale on the backend's own "absent = unchanged" convention.
  it('sends preamble: [] (not undefined/omitted) when the compiled agent has no preamble calls at all — required so removing every preamble line on edit actually clears a stale saved preamble', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [] }, '');
    const sentPayload = updateAgent.mock.calls[0][1];
    expect(sentPayload.preamble).toEqual([]);
  });

  it('forwards a non-empty compiled "preamble" array through unchanged, verbatim, in declaration order', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    const preamble = [{ type: 'log', params: { level: 'debug', message: 'x' } }, { type: 'help', params: { name: 'pnl' } }];
    await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [], preamble }, '');
    const sentPayload = updateAgent.mock.calls[0][1];
    expect(sentPayload.preamble).toEqual(preamble);
  });

  it('never sends schedule/tier/trade_mode/lifespan fields — None/absent on the backend means unchanged', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [] }, '');
    const sentPayload = updateAgent.mock.calls[0][1];
    for (const forbidden of ['schedule', 'tier', 'topic', 'trade_mode', 'cooldown_minutes',
      'lifespan_type', 'lifespan_max_fires', 'lifespan_expires_at', 'debounce_minutes',
      'blackout_windows', 'slug', 'kind']) {
      expect(Object.prototype.hasOwnProperty.call(sentPayload, forbidden)).toBe(false);
    }
  });

  it('requires a target slug — never calls updateAgent with no row selected', async () => {
    const r = await updateConsoleAgent(null, { name: 'x' }, '');
    expect(r.ok).toBe(false);
    expect(updateAgent).not.toHaveBeenCalled();
  });

  it('requires a compiled agent — never calls updateAgent with nothing', async () => {
    const r = await updateConsoleAgent('x', null, '');
    expect(r.ok).toBe(false);
    expect(updateAgent).not.toHaveBeenCalled();
  });

  it('surfaces an update failure (e.g. 400 kind-change rejection) as a structured error', async () => {
    updateAgent.mockRejectedValue(new Error('kind cannot be changed after creation'));
    const r = await updateConsoleAgent('x', { name: 'x', conditions: {}, events: [], actions: [] }, '');
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/kind cannot be changed/);
  });

  // Design item 0.C — `has_explicit_alert` sticky-false-unless-already-true.
  // `decompileAgent` renders ANY non-empty `events` list as `DO alert(...)`
  // text unconditionally, with no way to tell a real explicit alert() call
  // apart from a seed/form-default `events` list once rendered as text —
  // so this field CANNOT be derived from the freshly recompiled text
  // alone the way `given`/`preamble` are. See `updateConsoleAgent`'s own
  // `hadExplicitAlert` param doc for the full danger-scenario reasoning.
  describe('has_explicit_alert — sticky false-unless-already-true', () => {
    it('THE DANGER SCENARIO: a loss-* seed agent (has_explicit_alert=false, populated events[]) edited and resaved must NOT silently flip to true, even though the decompile->recompile round-trip of its text alone produces a populated events array', async () => {
      updateAgent.mockResolvedValue({ detail: 'ok' });
      // Shaped exactly like a real `loss-*` builtin: `has_explicit_alert`
      // false/absent on the loaded row, but a real, non-empty `events`
      // list from `_LOSS_AGENT_DEFAULTS` — decompiling THIS agent and
      // recompiling the decompiled text (unrelated to this function, but
      // the scenario this test pins) would render `DO alert(telegram)`
      // and recompile with `has_explicit_alert: true`, even though the
      // row was never in explicit-alert mode.
      const compiledAgentAfterUnrelatedEdit = {
        name: 'loss-funds-negative', description: '',
        conditions: { metric: 'cash', scope: 'funds.any_acct', op: '<', value: -1 }, // threshold tweak: 0 -> -1
        events: [{ channel: 'telegram', enabled: true }, { channel: 'ntfy', enabled: true, priority: 'urgent' }],
        actions: [],
        has_explicit_alert: true, // what the fresh compile of the decompiled text produces
      };
      await updateConsoleAgent('loss-funds-negative', compiledAgentAfterUnrelatedEdit, 'WHEN cash@funds.any_acct < -1 DO alert(telegram, ntfy(priority="urgent"))', false);
      const payload = updateAgent.mock.calls[0][1];
      expect(payload.has_explicit_alert).toBe(false);
      // Sanity: the rest of the payload DID pick up the unrelated edit —
      // this isn't a "nothing changed" false positive.
      expect(payload.conditions.value).toBe(-1);
    });

    it('an agent already in explicit-alert mode stays true when the fresh compile still has a real alert', async () => {
      updateAgent.mockResolvedValue({ detail: 'ok' });
      const compiledAgent = {
        name: 'my-explicit-alert-agent', conditions: {}, events: [{ channel: 'telegram', enabled: true }],
        actions: [], has_explicit_alert: true,
      };
      await updateConsoleAgent('my-explicit-alert-agent', compiledAgent, '', true);
      expect(updateAgent.mock.calls[0][1].has_explicit_alert).toBe(true);
    });

    it('an agent already in explicit-alert mode flips to false when the operator removes the alert() call', async () => {
      updateAgent.mockResolvedValue({ detail: 'ok' });
      const compiledAgent = {
        name: 'my-explicit-alert-agent', conditions: {}, events: [],
        actions: [{ type: 'close_position', params: {} }], // no alert() left in DO
        // has_explicit_alert absent — compileAgentStmt never splices it
        // in when doResult.events is empty.
      };
      await updateConsoleAgent('my-explicit-alert-agent', compiledAgent, '', true);
      expect(updateAgent.mock.calls[0][1].has_explicit_alert).toBe(false);
    });

    it('defaults to false when the caller passes no hadExplicitAlert argument at all (backward-compatible default)', async () => {
      updateAgent.mockResolvedValue({ detail: 'ok' });
      const compiledAgent = { name: 'x', conditions: {}, events: [{ channel: 'telegram', enabled: true }], actions: [], has_explicit_alert: true };
      await updateConsoleAgent('x', compiledAgent, '');
      expect(updateAgent.mock.calls[0][1].has_explicit_alert).toBe(false);
    });
  });
});

// `updateConsoleBracket` and its test coverage removed 2026-11 (Track 3,
// T3.4, Bracket/Template/GTT removal) — `PATCH /api/admin/templates/{id}`
// is gone server-side.

describe('updateConsoleToken', () => {
  it('calls updateAgent(slug, {description, conditions, events, cli_source}) — never createAgent (Sprint D2)', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    const compiledFragment = { kind: 'notify', name: 'my-token', body: [{ channel: 'telegram', enabled: true }], description: 'hi' };
    const r = await updateConsoleToken('my-token', compiledFragment, 'token(kind=notify) telegram #name="my-token" #desc="hi"');
    expect(r.ok).toBe(true);
    expect(r.slug).toBe('my-token');
    expect(createAgent).not.toHaveBeenCalled();
    expect(updateAgent).toHaveBeenCalledWith('my-token', {
      description: 'hi',
      conditions: {},
      events: compiledFragment.body,
      cli_source: 'token(kind=notify) telegram #name="my-token" #desc="hi"',
    });
  });

  it('never sends name — Agent.name must never drift from the $ref-key slug', async () => {
    updateAgent.mockResolvedValue({ detail: 'ok' });
    await updateConsoleToken('my-token', { kind: 'notify', name: 'my-token', body: [] });
    const sentPayload = updateAgent.mock.calls[0][1];
    expect(Object.prototype.hasOwnProperty.call(sentPayload, 'name')).toBe(false);
  });

  it('requires a target slug — never calls updateAgent with no row selected', async () => {
    const r = await updateConsoleToken(null, { kind: 'notify', name: 'x', body: [] });
    expect(r.ok).toBe(false);
    expect(updateAgent).not.toHaveBeenCalled();
  });

  it('requires a compiled fragment — never calls updateAgent with nothing', async () => {
    const r = await updateConsoleToken('my-token', null);
    expect(r.ok).toBe(false);
    expect(updateAgent).not.toHaveBeenCalled();
  });
});

// ── submitConsoleHelp (item 4, grammar-unification plan) ────────────────

describe('submitConsoleHelp', () => {
  it('calls lookupHelpToken with the compiled help params.name and returns the response', async () => {
    lookupHelpToken.mockResolvedValue({
      action: 'help', status: 'logged', name: 'pnl',
      matches: [{ grammar_kind: 'condition', token_kind: 'metric', token: 'pnl', description: 'P&L', signature: '(ctx)', docstring: 'doc' }],
    });
    const r = await submitConsoleHelp({ help: { params: { name: 'pnl' } } });
    expect(r.ok).toBe(true);
    expect(lookupHelpToken).toHaveBeenCalledWith('pnl');
    expect(/** @type {any} */ (r).response.matches).toHaveLength(1);
  });

  it('requires a compiled help result with a name — never calls lookupHelpToken with nothing', async () => {
    const r1 = await submitConsoleHelp(null);
    expect(r1.ok).toBe(false);
    const r2 = await submitConsoleHelp(/** @type {any} */ ({ help: { params: {} } }));
    expect(r2.ok).toBe(false);
    expect(lookupHelpToken).not.toHaveBeenCalled();
  });

  it('surfaces a lookup failure as a structured error, never throws', async () => {
    lookupHelpToken.mockRejectedValue(new Error('network error'));
    const r = await submitConsoleHelp({ help: { params: { name: 'pnl' } } });
    expect(r.ok).toBe(false);
    expect(/** @type {any} */ (r).error).toMatch(/network error/);
  });
});
