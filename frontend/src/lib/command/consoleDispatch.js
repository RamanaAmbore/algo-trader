// Console dispatch layer — sits ahead of the agents.js call-style grammar
// on /automation's Console tab. Decides, from the raw typed text alone,
// whether a statement is agent-family syntax (WHEN/ALERT/DO/GIVEN, bare
// order(...)/place_order(...)/token(...)/cancel_all_orders(...)) or
// anything else.
//
// The legacy positional orders.js grammar (buy/sell/modify/cancel,
// `agent list`/`agent status` interpret, raw `/api/admin/exec` shell
// passthrough) and its hidden CommandLineTab host were removed entirely
// (2026-10-17, operator-confirmed deletion — see the 2026-10-17 entry in
// docs/specs/AUTOMATION_SPEC.md's change log). `classifyConsoleStatement`
// now only distinguishes real agent-family syntax from everything else;
// there is no second grammar or fallback execution path left to route to.
//
// `bracket(...)`/`bracket="slug"` resolution (the templates-catalog
// lookup this module used to do before building a ticket/basket request)
// was removed 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal) —
// `bracket(...)` is now a hard compile error in agents.js, and
// `bracket=` on an `order(...)` leg is rejected at compile time too, so
// no compiled result can ever carry a `template_slug` for this module to
// resolve. `bracket` is still matched by `_AGENT_CALL_RE` below so a
// typed `bracket(...)` classifies as agent-family and reaches the real
// compiler's specific "bracket() was removed" error, rather than falling
// through to this module's generic "unsupported syntax" hint.

import {
  placeTicketOrder, placeBasket, cancelAllOrders,
  createAgent, updateAgent,
  lookupHelpToken,
} from '$lib/api';

// ─────────────────────────────────────────────────────────────────────────
// (a) Classification
// ─────────────────────────────────────────────────────────────────────────

// Sprint [grammar-unify] — `GIVEN` is removed from the real GRAMMAR (no
// longer a clause keyword at all — see agents.js's own `_RESERVED`
// comment), but it's kept HERE deliberately: a leading `GIVEN` is still
// unambiguously an ATTEMPT at agent-family syntax, so routing it to
// 'agents' lets the real compiler give its specific, helpful "GIVEN is
// no longer part of the grammar" rejection (surfaced via AgentCliEditor's
// own compile-error block) instead of this classifier's generic
// "unsupported syntax" hint, which would be a confusing downgrade for
// exactly the operator who most needs the specific message. `WHEN`/`DO`
// are the only two real clause-head keywords going forward. A bare
// `alert` prefix still matches here too (this is a prefix-only
// `\b`-boundary match, so `alert(telegram)` — the new ordinary call —
// still matches `/^alert\b/` the same way it always has, coincidentally;
// this regex was never actually keyed on `alert` being a reserved CLAUSE
// keyword specifically).
const _AGENT_HEAD_RE = /^(when|alert|do|given)\b/i;
// A bare `order(...)`/`place_order(...)`/`bracket(...)`/`token(...)`/
// `debug(...)`/`help(...)` call — matched on just the "name(" prefix (not
// a balanced-paren scan) so a STILL-BEING-TYPED statement like
// `order(acct=` classifies correctly before the operator has finished
// typing the closing paren. Sprint [grammar-unify] widened this to
// `debug`/`help` too — either is now a complete, valid, STANDALONE
// preamble-only statement on its own (not just DO-clause vocabulary), so
// omitting them here would misclassify as 'unsupported' and hide
// AgentCliEditor's own compile-error block (ConsoleTab's
// `showCompileErrors` gates on `_classify === 'agents'`) — even though
// the statement is genuinely agent-family and just mid-typed. A bare
// label assignment (`x = 1`) has no distinguishing head token at all and
// is NOT covered by either regex — accepted, narrower gap: `onUnsupported`
// only fires the "unsupported syntax" hint, it never blocks a submit that
// reaches `compileAgentCliStatement` through some OTHER path.
const _AGENT_CALL_RE = /^(order|place_order|bracket|token|cancel_all_orders|debug|help)\s*\(/i;

/**
 * Classify a raw CLI statement by its head tokens alone — cheap regex
 * matching, NOT a full lex/parse (lexAgentStatement returns `tokens: []`
 * on unbalanced parens, which would misclassify a statement that's only
 * half-typed).
 * @param {string} text
 * @returns {'agents'|'unsupported'|'empty'}
 */
export function classifyConsoleStatement(text) {
  const t = String(text == null ? '' : text).trim();
  if (!t) return 'empty';
  if (_AGENT_HEAD_RE.test(t)) return 'agents';
  if (_AGENT_CALL_RE.test(t)) return 'agents';
  // Anything else (the retired positional buy/sell/modify/cancel
  // grammar, `agent list`/`agent status` interpret text, raw shell
  // commands, or just unrecognized text) has no execution path left to
  // route to — see this module's header. The caller shows a short,
  // informational "unsupported syntax" hint instead of silently doing
  // nothing or throwing.
  return 'unsupported';
}

/**
 * The one function that decides what a given raw statement typed into
 * Console's single visible textbox should do. Thin wrapper over
 * `classifyConsoleStatement` (reused, not re-implemented): 'agents' routes
 * to the caller's own generic-grammar flow (`compileAgentCliStatement` —
 * already fully wired in ConsoleTab.svelte, untouched by this function);
 * 'unsupported' routes to the caller's own hint/toast handler (no
 * execution path exists for non-agent-family text — see this module's
 * header). 'empty' text calls neither callback.
 *
 * Deliberately does NOT itself call compileAgentCliStatement / submit /
 * fetch — the `onAgentFamily` callback owns its own compile + submission
 * mechanics; this is pure routing so it stays trivial to unit-test in
 * isolation (see consoleDispatch.test.js).
 *
 * @param {string} text
 * @param {{onAgentFamily?: () => any, onUnsupported?: () => any}} handlers
 * @returns {any} whatever the invoked callback returns (undefined for 'empty')
 */
export function dispatchConsoleStatement(text, { onAgentFamily, onUnsupported } = {}) {
  const kind = classifyConsoleStatement(text);
  if (kind === 'agents') return onAgentFamily?.();
  if (kind === 'empty') return undefined;
  return onUnsupported?.();
}

// ─────────────────────────────────────────────────────────────────────────
// (b) `bracket="slug"` → `template_id` resolution — REMOVED 2026-11
//     (Track 3, T3.4, Bracket/Template/GTT removal). `_collectLegs`,
//     `needsTemplateResolution`, `_resolveLegTemplateRef` and
//     `resolveBracketTemplateRefs` are gone — agents.js's compiler now
//     rejects `bracket=` on an order leg outright (see `compileOrderStmt`
//     in agents.js), so no compiled leg can ever carry a `template_slug`
//     field for this module to resolve.
// ─────────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────────
// (c) Submission — same API calls the existing Ticket/basket UI already
//     makes (placeTicketOrder / placeBasket / createAgent). No parallel
//     execution path.
// ─────────────────────────────────────────────────────────────────────────

/**
 * Pass a compiled `kind:'order'` result through unchanged. Kept as its
 * own function (not inlined at call sites) for two reasons: it is split
 * out from `submitResolvedConsoleOrder` so a caller (the /console page)
 * can run this step BEFORE showing a live-order confirm dialog, and its
 * name/shape stay stable for `OrderCliTab.svelte`/`ConsoleTab.svelte`,
 * which both call it today. It used to resolve a `bracket="slug"`
 * reference to a real `template_id` here (removed 2026-11, Track 3 T3.4,
 * Bracket/Template/GTT removal — see note (b) above); nothing is left to
 * resolve now, so this can never fail.
 * @param {{ticket:any, basket:any}} compiled
 * @returns {Promise<{ok:true, ticket:any, basket:any}>}
 */
export async function resolveConsoleOrder(compiled) {
  return { ok: true, ticket: compiled.ticket || null, basket: compiled.basket || null };
}

/**
 * Submit an ALREADY-RESOLVED order (no `template_slug` fields remain —
 * see `resolveConsoleOrder`) via the exact same API calls the existing
 * Ticket/basket UI already makes. `mode` overrides whatever the compiler
 * baked into `resolved.ticket.mode` (AgentCliEditor only ever threads
 * 'paper'|'live' through its own `mode` prop, which would silently
 * collapse shadow/sim/replay to paper — the caller reads the real
 * `executionMode` store and passes it through explicitly instead).
 * @param {{ticket:any, basket:any}} resolved
 * @param {{mode?: string, source?: string}} opts
 * @returns {Promise<{ok:true, kind:'basket'|'ticket', response:any} | {ok:false, error:string, timedOut?:boolean}>}
 */
export async function submitResolvedConsoleOrder(resolved, opts = {}) {
  const { mode, source = 'command' } = opts;
  try {
    if (resolved.basket) {
      // 2026-11-05 fix (CRITICAL money-safety audit finding) — `mode`
      // used to be read above (this function's own docstring explains
      // why — AgentCliEditor's compiled `mode` collapses shadow/sim/
      // replay to paper, so the caller passes the real executionMode
      // value through explicitly) but was never threaded into the
      // basket call at all, silently dropping the exact same signal
      // the ticket branch below correctly uses. See `placeBasket`'s
      // own comment (api.js) and `basket_order_handler`'s (orders_
      // basket.py) for how a non-"live" value is honoured server-side.
      const response = await placeBasket(resolved.basket.groups, mode);
      return { ok: true, kind: 'basket', response };
    }
    if (resolved.ticket) {
      const payload = { ...resolved.ticket, mode, source };
      const response = await placeTicketOrder(payload);
      return { ok: true, kind: 'ticket', response };
    }
    return { ok: false, error: 'nothing to submit' };
  } catch (e) {
    // 2026-11-05 fix (HIGH-severity audit finding, same class as
    // placeBasket's own `throwOnTimeout` fix) — a client-side submit
    // timeout is not a known rejection; `timedOut` lets a caller show
    // "still processing" instead of implying the order never went
    // through (which, for a basket especially, it very often did).
    return {
      ok: false, error: e?.message || 'order failed',
      timedOut: e?.name === 'TimeoutError',
    };
  }
}

/**
 * Convenience wrapper combining `resolveConsoleOrder` +
 * `submitResolvedConsoleOrder`. `resolveConsoleOrder` is now a trivial,
 * always-ok pass-through (the `bracket=` resolution step it used to run
 * first was removed 2026-11, Track 3 T3.4, Bracket/Template/GTT
 * removal), so this is a thin two-step call, not a resolve-then-guard.
 * @param {{ticket:any, basket:any}} compiled
 * @param {{mode?: string, source?: string}} opts
 * @returns {Promise<{ok:true, kind:'basket'|'ticket', response:any} | {ok:false, error:string}>}
 */
export async function submitConsoleOrder(compiled, opts = {}) {
  const resolved = await resolveConsoleOrder(compiled);
  return submitResolvedConsoleOrder(resolved, opts);
}

/**
 * Submit a compiled `cancel_all_orders(...)` statement (Sprint
 * [cancel-all-cli], 2026-10) via `POST /api/orders/cancel-all` — live-only,
 * no `bracket=`/template resolution step (the `cancel_all_orders`
 * params_schema has no such field), so this is a thin wrapper, not a
 * `resolveConsoleOrder`-style two-step pipeline. Mirrors
 * `submitResolvedConsoleOrder`'s own try/catch-to-structured-error
 * convention so callers never need a bare `try` of their own.
 * @param {{cancelAll:{mode:string, params:any}}} compiled
 * @returns {Promise<{ok:true, response:any} | {ok:false, error:string}>}
 */
export async function submitConsoleCancelAllOrders(compiled) {
  const params = compiled?.cancelAll?.params;
  if (!params) return { ok: false, error: 'nothing to submit' };
  try {
    const response = await cancelAllOrders(params);
    return { ok: true, response };
  } catch (e) {
    return { ok: false, error: e?.message || 'cancel-all failed' };
  }
}

/**
 * Submit a compiled bare `help(<name>)` statement (item 4,
 * grammar-unification plan) via `POST /api/agents/help` — read-only, no
 * mode gate, no bracket/template resolution step, same thin-wrapper
 * shape as `submitConsoleCancelAllOrders` immediately above. An operator
 * typing `help pnl` wants to SEE the catalog description/signature/
 * docstring immediately — ConsoleTab renders `response.matches` inline
 * rather than only logging it (matching the backend route's own doc).
 * @param {{help:{params:{name:string}}}} compiled
 * @returns {Promise<{ok:true, response:any} | {ok:false, error:string}>}
 */
export async function submitConsoleHelp(compiled) {
  const params = compiled?.help?.params;
  if (!params || !params.name) return { ok: false, error: 'nothing to look up' };
  try {
    const response = await lookupHelpToken(params.name);
    return { ok: true, response };
  } catch (e) {
    return { ok: false, error: e?.message || 'help lookup failed' };
  }
}

/** Slugify an agent `#name=` value into a URL/DB-safe slug — console has
 *  no separate "slug" input field (unlike /automation's full create
 *  form), so the slug is derived from the operator-typed name. Collisions
 *  surface as the backend's own 409 (never silently resaved as an
 *  update — see `submitConsoleAgent`'s own doc). */
export function slugifyAgentName(name) {
  const s = String(name || '')
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
  return s || 'agent';
}

/**
 * Submit a compiled `kind:'agent'` result as a NEW agent. Console is a
 * create-only surface — it has no concept of "the agent currently being
 * edited" the way /automation's Structured/CLI tabs do, so a `#name=`
 * that collides with an existing slug surfaces the backend's real 409
 * as a clear error; it never silently falls back to updateAgent (that
 * could overwrite an unrelated agent whose slugified name happens to
 * match).
 * @param {any} compiledAgent — `compiled.agent` from compileAgentCliStatement
 * @param {string} rawText — the raw CLI text, stored as cli_source for audit
 */
export async function submitConsoleAgent(compiledAgent, rawText) {
  if (!compiledAgent || !compiledAgent.name) {
    return { ok: false, error: 'agent requires a #name= tag' };
  }
  const slug = slugifyAgentName(compiledAgent.name);
  // Wave 2 (grammar-unification) — `compileAgentStmt` (agents.js) stamps
  // `kind: 'event'` directly on the compiled agent object when the
  // statement's condition is a `log(...)` leaf (`isEventKind`) — the
  // same field the retiring `compileEventStmt` used to stamp for the old
  // standalone `event_stmt`. Every OTHER (ordinary) compile never sets a
  // `kind` field at all, so anything else (including `undefined`) means
  // the existing `'threshold'` behavior — never a second hardcoded literal.
  const kind = compiledAgent.kind === 'event' ? 'event' : 'threshold';
  const payload = {
    slug,
    kind,
    name: compiledAgent.name,
    description: compiledAgent.description || '',
    conditions: compiledAgent.conditions,
    events: compiledAgent.events,
    actions: compiledAgent.actions,
    // Sprint [GIVEN-expr] — `given` is fully driven by the CLI text, same
    // as conditions/events/actions/tags (NOT a "backend-managed, leave
    // unchanged when the grammar can't express it" field like
    // schedule/cooldown/tier below in `updateConsoleAgent`) — so it is
    // ALWAYS sent as a real, explicit value, same "|| []" convention
    // `tags` already uses, never `undefined`. This matters more on
    // UPDATE (see that function's own comment — an `undefined` here
    // would mean "leave the OLD given[] in place forever" on the
    // backend's own "absent = unchanged" convention), but kept identical
    // here on CREATE too, for consistency / to never depend on which
    // path a statement happens to go through.
    given: compiledAgent.given || [],
    // Sprint [grammar-unify] — `preamble` (the unconditional-call array,
    // item 2) is driven ENTIRELY by the CLI text, exactly like `given`
    // above. The backend's `AgentCreateRequest` (in
    // `backend/api/routes/agents.py`, not `schemas.py`) carries a
    // `preamble` field as of commit `4e6df1ec` (item 0's engine work,
    // which also executes it — diagnostics every cycle, side-effecting
    // calls once per activation).
    preamble: compiledAgent.preamble || [],
    // Design item 0.C — unlike UPDATE (see `updateConsoleAgent`'s own
    // `hadExplicitAlert` doc), CREATE has no existing row to be sticky
    // against, so a brand-new agent genuinely authored with
    // `DO ... alert(...) ...` trusts the fresh compile unconditionally.
    has_explicit_alert: !!compiledAgent.has_explicit_alert,
    tags: compiledAgent.tags || [],
    group: compiledAgent.group || null,
    cli_source: rawText || '',
  };
  try {
    await createAgent(payload);
    return { ok: true, slug };
  } catch (e) {
    return { ok: false, error: e?.message || 'agent create failed' };
  }
}

// `submitConsoleBracket` removed 2026-11 (Track 3, T3.4, Bracket/
// Template/GTT removal) — `compiled.kind` can never be `'bracket'`
// anymore (agents.js rejects `bracket(...)` with a hard compile error in
// every position), so this function was unreachable.

/**
 * Submit a compiled `kind:'token'` result (`compiled.fragment` —
 * `{kind, name, conditions?, description?}` — see `compileTokenStmt` in
 * agents.js) as a new Template — `Agent(kind='template')` — via the
 * unified `createAgent` (`POST /api/agents/`). Sprint D2 (Agent/Bracket/
 * Template unification phase) retired the separate
 * `/api/admin/fragments` CRUD this used to call; a Template is now just
 * an ordinary Agent row.
 *
 * `compiledFragment.kind` is ALWAYS `'condition'` in practice —
 * `compileTokenStmt` (agents.js) is a hard COMPILE-time error for
 * `kind === 'notify'` (returns `fragment: null`), so a non-null fragment
 * with `kind !== 'condition'` can never reach here through the real
 * compiler (channel-naming removal, 2026-11, commit `0216d534`). The
 * explicit guard below is defensive only: it surfaces a clear error
 * instead of silently writing an empty, useless Template row if some
 * other caller ever constructs a `kind:'notify'`-shaped fragment by
 * hand.
 *
 * `fragment.conditions` (the JSON condition tree, from `compileCondition`
 * — the same production WHEN uses) lands in `conditions`; `events`
 * always stays `[]` — a Template's own notify-kind channel-list body can
 * no longer be authored via this grammar. No backend change needed —
 * `createAgent` already accepts a non-empty `conditions` tree for
 * `kind='template'` rows (confirmed: real condition-kind Templates
 * already exist in production, authored via direct JSON before this
 * grammar supported them).
 *
 * Unlike `submitConsoleAgent`, the slug is NOT derived via
 * `slugifyAgentName` — `fragment.name` IS the slug (the exact string
 * every `{"$ref": ...}` elsewhere resolves by), so it must be sent
 * unchanged; the backend 400s if it isn't already a valid slug shape
 * (lowercase alnum + hyphens), same as the retired route did.
 * @param {any} compiledFragment — `compiled.fragment` from compileAgentCliStatement
 * @param {string} rawText — the raw CLI text, stored as cli_source for audit
 */
export async function submitConsoleToken(compiledFragment, rawText = '') {
  if (!compiledFragment || !compiledFragment.name) {
    return { ok: false, error: 'token requires a #name= tag' };
  }
  if (compiledFragment.kind !== 'condition') {
    return { ok: false, error: 'token: only kind=condition can be created — notify-kind channel-list Tokens were removed 2026-11 (channel-naming removal)' };
  }
  const slug = compiledFragment.name;
  const payload = {
    slug,
    kind: 'template',
    name: slug,
    description: compiledFragment.description || '',
    conditions: compiledFragment.conditions || {},
    events: [],
    actions: [],
    cli_source: rawText || '',
  };
  try {
    await createAgent(payload);
    return { ok: true, slug };
  } catch (e) {
    return { ok: false, error: e?.message || 'token create failed' };
  }
}

// ─────────────────────────────────────────────────────────────────────────
// (d) Edit-mode updates (Sprint C, 2026-10, Phase 2 "Automation redesign")
//     — the Agents-tab row list's Edit action decompiles a row into this
//     Console tab and must route Save through an UPDATE on the SAME row,
//     never `submitConsole*`'s own create-only path (which 409s on a slug
//     collision rather than risking an overwrite of an unrelated row —
//     see `submitConsoleAgent`'s own doc). Each function below targets
//     one already-identified row (slug for an Agent, numeric id for a
//     Bracket/Token) and calls the EXISTING update endpoint the
//     Structured/Brackets/Tokens UIs already used — no new write path.
// ─────────────────────────────────────────────────────────────────────────

/**
 * Update an EXISTING agent/event agent (any kind the unified `/api/agents/`
 * endpoint accepts) from a compiled `kind:'agent'|'event'` CLI result.
 * Sends ONLY the fields the CLI grammar can express (name, description,
 * conditions, events, actions, given, tags, group, cli_source) — every
 * other `AgentUpdateRequest` field (schedule, cooldown, tier, topic,
 * trade_mode, lifespan_*, debounce_minutes, blackout_windows) is
 * deliberately left `undefined` so `update_agent`'s own "None/absent =
 * leave unchanged" convention preserves whatever those fields were
 * already set to — a CLI-driven Edit+Save must never silently reset a
 * setting the grammar has no syntax to express at all.
 *
 * Sprint [GIVEN-expr] — `given` is FULLY expressible by the grammar (the
 * CLI text is the one and only source of truth for it, exactly like
 * conditions/events/actions), so it must NEVER be left `undefined` here —
 * doing so would hit the exact same "None/absent = leave unchanged"
 * convention this doc comment just described for the OTHER, deliberately-
 * preserved fields, except wrongly: an operator who edits a statement to
 * remove every GIVEN line and saves would have the OLD `given[]` persist
 * on the backend forever, silently. `compiledAgent.given || []` always
 * sends a real, explicit value (empty array when the statement has none),
 * matching the SAME convention `tags`/`conditions`/`events`/`actions`
 * already use.
 * @param {string} slug — the agent row being edited (unchanged by this call)
 * @param {any} compiledAgent — `compiled.agent` from compileAgentCliStatement
 * @param {string} rawText — the raw (edited) CLI text, stored as cli_source
 * @param {boolean} hadExplicitAlert — the ORIGINALLY LOADED row's own
 *  `has_explicit_alert` value (threaded through by the caller — see
 *  `/automation/+page.svelte`'s `editRow()`, the "load then diff on
 *  save" flow `given`/`preamble` above also rely on). Deliberately NOT
 *  derived from `compiledAgent.has_explicit_alert` alone: unlike
 *  `given`/`preamble`, this field is NOT fully expressible by the CLI
 *  text on its own — `decompileAgent` renders ANY non-empty `events`
 *  list as `DO alert(...)` text unconditionally (it has no way to know
 *  whether the row it decompiled was explicit-alert or seed/form-default
 *  — both look identical once rendered as text), so a round-trip
 *  recompile of that text always sets `has_explicit_alert: true`
 *  regardless of which one it actually was. Design item 0.C — sticky
 *  false-unless-already-true: an agent NOT already in explicit-alert
 *  mode (every seed/form-originated `loss-*` builtin, via
 *  `_LOSS_AGENT_DEFAULTS`) ALWAYS saves `has_explicit_alert: false`, even
 *  on an unrelated edit that happens to leave a populated `events` list
 *  in the text — otherwise the FIRST edit of any kind to one of those
 *  agents would silently and permanently flip it into explicit-alert
 *  mode, losing the rich kind/threshold/per-account alert table forever
 *  with no visible text change. An agent ALREADY in explicit-alert mode
 *  trusts the fresh compile (can go true→false if the operator removes
 *  the alert() call, or stay true). This intentionally means an operator
 *  cannot yet convert a seed-style agent into explicit-alert mode
 *  through ordinary Console editing — a dedicated "convert" action is a
 *  future, separate feature.
 */
export async function updateConsoleAgent(slug, compiledAgent, rawText, hadExplicitAlert = false) {
  if (!slug) return { ok: false, error: 'no agent selected for update' };
  if (!compiledAgent) return { ok: false, error: 'nothing compiled to save' };
  const payload = {
    name: compiledAgent.name,
    description: compiledAgent.description || '',
    conditions: compiledAgent.conditions,
    events: compiledAgent.events,
    actions: compiledAgent.actions,
    given: compiledAgent.given || [],
    // Sprint [grammar-unify] — `preamble` (the unconditional-call array,
    // item 2) is driven ENTIRELY by the CLI text, exactly like `given`
    // above (the same reasoning applies verbatim: an `undefined` here on
    // the UPDATE path would mean "leave the OLD preamble[] in place
    // forever" on the backend's own "absent = unchanged" convention). The
    // backend's `AgentUpdateRequest` (in `backend/api/routes/agents.py`,
    // not `schemas.py`) carries a `preamble` field as of commit
    // `4e6df1ec` (item 0's engine work, which also executes it).
    preamble: compiledAgent.preamble || [],
    // Design item 0.C — see this function's own `hadExplicitAlert` param
    // doc above for the full "why" (sticky false-unless-already-true).
    has_explicit_alert: hadExplicitAlert === true ? !!compiledAgent.has_explicit_alert : false,
    tags: compiledAgent.tags || [],
    group: compiledAgent.group || null,
    cli_source: rawText || '',
  };
  try {
    await updateAgent(slug, payload);
    return { ok: true, slug };
  } catch (e) {
    return { ok: false, error: e?.message || 'agent update failed' };
  }
}

// `updateConsoleBracket` removed 2026-11 (Track 3, T3.4, Bracket/
// Template/GTT removal) — `PATCH /api/admin/templates/{id}` and
// `compileBracketStmt` are both gone; this function was unreachable.

/**
 * Update an EXISTING Template (`Agent(kind='template')` row) via the
 * unified `updateAgent` (`PUT /api/agents/{slug}`). Sprint D2 retired the
 * separate `/api/admin/fragments` PATCH this used to call — the
 * Templates card's own read path already sourced from these same Agent
 * rows (Sprint C's adapter), so there's no separate id to resolve
 * anymore; the row's own `slug` IS the thing to PUT against.
 *
 * Deliberately does NOT send `name` — the retired FragmentPatch endpoint
 * had no field for it either (a Token's name/slug were fixed at
 * creation), and under the unified model `Agent.slug` is the real `$ref`
 * key while `Agent.name` is free-text display text; sending a possibly-
 * edited `#name=` here would risk the two drifting apart with no way for
 * an operator to tell which one `$ref` still resolves by. If the
 * operator wants to rename a Template, they delete + recreate it.
 * @param {string} slug — the Agent row being edited (unchanged by this call)
 * @param {any} compiledFragment — `compiled.fragment` (`{kind, name, conditions?, description?}`)
 * @param {string} rawText — the raw (edited) CLI text, stored as cli_source
 *
 * `compiledFragment.kind` is ALWAYS `'condition'` in practice, same
 * reasoning as `submitConsoleToken` above — `compileTokenStmt` hard-
 * errors on `kind === 'notify'`, and `decompileToken` (agents.js) also
 * refuses to decompile an already-saved notify-kind row into editable
 * text in the first place, so this function can never be reached with a
 * non-null `kind:'notify'` fragment through the real Edit flow
 * (channel-naming removal, 2026-11, commit `0216d534`). The explicit
 * guard below is defensive only.
 */
export async function updateConsoleToken(slug, compiledFragment, rawText = '') {
  if (!slug) return { ok: false, error: 'no token selected for update' };
  if (!compiledFragment) return { ok: false, error: 'nothing compiled to save' };
  if (compiledFragment.kind !== 'condition') {
    return { ok: false, error: 'token: only kind=condition can be edited — notify-kind channel-list Tokens were removed 2026-11 (channel-naming removal)' };
  }
  const payload = {
    description: compiledFragment.description || '',
    conditions: compiledFragment.conditions || {},
    events: [],
    cli_source: rawText || '',
  };
  try {
    await updateAgent(slug, payload);
    return { ok: true, slug };
  } catch (e) {
    return { ok: false, error: e?.message || 'token update failed' };
  }
}
