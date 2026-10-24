// Console dispatch layer — sits ahead of the agents.js call-style grammar
// on /automation's Console tab. Decides, from the raw typed text alone,
// whether a statement is agent-family syntax (WHEN/ALERT/DO/GIVEN, bare
// order(...)/place_order(...)/bracket(...)/token(...)/cancel_all_orders(...))
// or anything else — then, for the order-compile path, resolves any
// `bracket="slug"` reference to a real numeric `template_id` before a
// ticket/basket request is ever built.
//
// The legacy positional orders.js grammar (buy/sell/modify/cancel,
// `agent list`/`agent status` interpret, raw `/api/admin/exec` shell
// passthrough) and its hidden CommandLineTab host were removed entirely
// (2026-10-17, operator-confirmed deletion — see the 2026-10-17 entry in
// docs/specs/AUTOMATION_SPEC.md's change log). `classifyConsoleStatement`
// now only distinguishes real agent-family syntax from everything else;
// there is no second grammar or fallback execution path left to route to.
//
// ── `bracket="slug"` resolution — KNOWN GAP, do not silently "fix" ─────
// The one existing server-side resolver for this exact reference shape
// (`backend/api/algo/template_attach.py:load_template_for_slug_or_id`,
// used by the Lab/MCP `place_order` tool) matches ONLY on
// `Agent.slug` (Sprint E, Agent/Bracket/Template unification phase —
// previously `OrderTemplate.slug` directly; see that function's own
// doc) — exact string equality, never on `name`. This module mirrors
// that canonical behaviour exactly, on purpose, by resolving against
// the SAME reshaped row list `loadOrderTemplates()` already produces
// (bracket-Agent rows reshaped via `bracketAgentToTemplateOut`, `id` =
// the real `OrderTemplate.id` via `Agent.legacy_template_id`) rather
// than fetching `/api/admin/templates/` directly: operator-authored
// custom templates leave `slug` NULL on the `OrderTemplate` side and
// get a DERIVED slug (`bracket-{id}`) on the mirrored Agent side (see
// `backend.api.algo.bracket_registry.bracket_slug_for`) — never the
// template's `name` — so a `bracket="my-custom-template"` reference
// where "my-custom-template" is a custom template's NAME (not its
// slug/derived-slug) will NOT resolve today — it surfaces a clear
// "Unknown bracket" error rather than guessing. Widening this to also
// match on `name` would diverge from the canonical resolver and risks
// silently picking the wrong row when name and slug disagree. This is
// a pre-existing, shared limitation (the Lab/MCP path has the identical
// gap) — flagged here, not patched here.

import {
  placeTicketOrder, placeBasket, cancelAllOrders,
  createAgent, updateAgent,
  createOrderTemplate, patchOrderTemplate,
  lookupHelpToken,
} from '$lib/api';
import { loadOrderTemplates, reloadOrderTemplates } from '$lib/data/templates';

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
// (b) `bracket="slug"` → `template_id` resolution
// ─────────────────────────────────────────────────────────────────────────

/** Collect every leg object out of a compiled order-kind result,
 *  regardless of single-ticket vs multi-leg-basket shape. */
function _collectLegs(compiled) {
  if (compiled && compiled.ticket) return [compiled.ticket];
  if (compiled && compiled.basket) {
    return (compiled.basket.groups || []).flatMap((g) => g.legs || []);
  }
  return [];
}

/** True iff ANY leg in the compiled result carries a `template_slug`
 *  reference — used to skip the templates fetch entirely on the (common)
 *  no-bracket-used path. */
export function needsTemplateResolution(compiled) {
  return _collectLegs(compiled).some((leg) => leg && leg.template_slug != null);
}

/** Resolve one leg's `template_slug` (if any) to a real `template_id`,
 *  stripping `template_slug` from the output object either way. Never
 *  mutates the input. Returns `{ok:true, leg}` or `{ok:false, error}`.
 * @returns {{ok:true, leg:any} | {ok:false, error:string}} */
function _resolveLegTemplateRef(leg, templatesBySlug) {
  if (!leg || leg.template_slug == null) return { ok: true, leg };
  const slug = leg.template_slug;
  if (leg.template_id != null) {
    return {
      ok: false,
      error: `cannot specify both bracket="${slug}" and a numeric template_id on the same leg`,
    };
  }
  const row = templatesBySlug.get(slug);
  if (!row) {
    return {
      ok: false,
      error: `Unknown bracket "${slug}" — no saved template with that slug`,
    };
  }
  const { template_slug, ...rest } = leg; // eslint-disable-line no-unused-vars
  return { ok: true, leg: { ...rest, template_id: row.id } };
}

/**
 * Resolve every `template_slug` reference in a compiled order result
 * against an already-fetched templates list. Pure — does no I/O. Matches
 * ONLY on `row.slug` (see this file's header note) — never on `name`.
 * @param {{ticket: any, basket: any}} compiled
 * @param {any[]} templateRows
 * @returns {{ok:true, ticket:any, basket:any} | {ok:false, error:string}}
 */
export function resolveBracketTemplateRefs(compiled, templateRows) {
  const templatesBySlug = new Map();
  for (const row of templateRows || []) {
    if (row && row.slug != null) templatesBySlug.set(row.slug, row);
  }

  if (compiled && compiled.ticket) {
    const r = _resolveLegTemplateRef(compiled.ticket, templatesBySlug);
    if (!r.ok) return /** @type {{ok:false, error:string}} */ (r);
    return { ok: true, ticket: r.leg, basket: null };
  }

  if (compiled && compiled.basket) {
    const groups = [];
    for (const group of compiled.basket.groups || []) {
      const legs = [];
      for (const leg of group.legs || []) {
        const r = _resolveLegTemplateRef(leg, templatesBySlug);
        if (!r.ok) return /** @type {{ok:false, error:string}} */ (r);
        legs.push(r.leg);
      }
      groups.push({ ...group, legs });
    }
    return { ok: true, ticket: null, basket: { groups } };
  }

  return { ok: true, ticket: null, basket: null };
}

// ─────────────────────────────────────────────────────────────────────────
// (c) Submission — same API calls the existing Ticket/basket UI already
//     makes (placeTicketOrder / placeBasket / createAgent /
//     createOrderTemplate). No parallel execution path.
// ─────────────────────────────────────────────────────────────────────────

/**
 * Resolve any `bracket=` reference in a compiled `kind:'order'` result
 * (fetching the templates catalog only when at least one leg actually
 * carries a `template_slug`). Does NOT submit — split out from
 * `submitResolvedConsoleOrder` so a caller (the /console page) can run
 * resolution BEFORE showing a live-order confirm dialog, so a no-match
 * or fetch-failure error surfaces without ever popping the dialog.
 * @param {{ticket:any, basket:any}} compiled
 * @returns {Promise<{ok:true, ticket:any, basket:any} | {ok:false, error:string}>}
 */
export async function resolveConsoleOrder(compiled) {
  if (!needsTemplateResolution(compiled)) {
    return { ok: true, ticket: compiled.ticket || null, basket: compiled.basket || null };
  }
  let templateRows;
  try {
    templateRows = await loadOrderTemplates();
  } catch (e) {
    return { ok: false, error: e?.message ? `Could not load templates: ${e.message}` : 'Could not load templates for bracket lookup' };
  }
  return resolveBracketTemplateRefs(compiled, templateRows);
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
 * @returns {Promise<{ok:true, kind:'basket'|'ticket', response:any} | {ok:false, error:string}>}
 */
export async function submitResolvedConsoleOrder(resolved, opts = {}) {
  const { mode, source = 'command' } = opts;
  try {
    if (resolved.basket) {
      const response = await placeBasket(resolved.basket.groups);
      return { ok: true, kind: 'basket', response };
    }
    if (resolved.ticket) {
      const payload = { ...resolved.ticket, mode, source };
      const response = await placeTicketOrder(payload);
      return { ok: true, kind: 'ticket', response };
    }
    return { ok: false, error: 'nothing to submit' };
  } catch (e) {
    return { ok: false, error: e?.message || 'order failed' };
  }
}

/**
 * Convenience wrapper combining `resolveConsoleOrder` +
 * `submitResolvedConsoleOrder` — resolves any `bracket=` reference FIRST;
 * a fetch failure or an unresolved slug returns a structured error and
 * NEVER calls placeTicketOrder/placeBasket.
 * @param {{ticket:any, basket:any}} compiled
 * @param {{mode?: string, source?: string}} opts
 * @returns {Promise<{ok:true, kind:'basket'|'ticket', response:any} | {ok:false, error:string}>}
 */
export async function submitConsoleOrder(compiled, opts = {}) {
  const resolved = await resolveConsoleOrder(compiled);
  if (!resolved.ok) return /** @type {{ok:false, error:string}} */ (resolved);
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
  // Sprint 12 — `compileEventStmt` (agents.js) stamps `kind: 'event'`
  // directly on the compiled agent object for a notification (event_stmt)
  // agent; the ordinary `compileAgentStmt` path never sets a `kind` field
  // at all, so anything else (including `undefined`) means the existing
  // `'threshold'` behavior — never a second hardcoded literal.
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

/**
 * Submit a compiled `kind:'bracket'` result (`compiled.template`,
 * OrderTemplateCreate-shaped) as a new saved order template. Reloads the
 * shared `loadOrderTemplates()` cache on success so OrderTicket / Chain /
 * this same console's own bracket= resolution immediately see it.
 * @param {any} compiledTemplate — `compiled.template`
 */
export async function submitConsoleBracket(compiledTemplate) {
  if (!compiledTemplate) return { ok: false, error: 'bracket requires a #name= tag' };
  try {
    const response = await createOrderTemplate(compiledTemplate);
    try { await reloadOrderTemplates(); } catch { /* non-fatal — next loader call just refetches */ }
    return { ok: true, response };
  } catch (e) {
    return { ok: false, error: e?.message || 'bracket create failed' };
  }
}

/**
 * Submit a compiled `kind:'token'` result (`compiled.fragment` —
 * `{kind, name, body?, conditions?, description?}` — see
 * `compileTokenStmt` in agents.js) as a new Template —
 * `Agent(kind='template')` — via the unified `createAgent` (`POST
 * /api/agents/`). Sprint D2 (Agent/Bracket/Template unification phase)
 * retired the separate `/api/admin/fragments` CRUD this used to call; a
 * Template is now just an ordinary Agent row.
 *
 * `compiledFragment.kind` is `'notify'` or `'condition'` (2026-10-12 —
 * condition-kind Token creation is now supported). For `'notify'`,
 * `fragment.body` (a channel list) lands in `events`, `conditions`
 * stays `{}`. For `'condition'`, `fragment.conditions` (the JSON
 * condition tree, from `compileCondition` — the same production WHEN
 * uses) lands in `conditions`, `events` stays `[]`. No backend change
 * needed — `createAgent` already accepts a non-empty `conditions` tree
 * for `kind='template'` rows (confirmed: real condition-kind Templates
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
  const slug = compiledFragment.name;
  const isCondition = compiledFragment.kind === 'condition';
  const payload = {
    slug,
    kind: 'template',
    name: slug,
    description: compiledFragment.description || '',
    conditions: isCondition ? (compiledFragment.conditions || {}) : {},
    events: isCondition ? [] : (compiledFragment.body || []),
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

/**
 * Update an EXISTING saved order template (Bracket) via the existing
 * `PATCH /api/admin/templates/{id}`. `compiledTemplate` (`compiled.template`
 * from `compileBracketStmt`) never carries `slug`/`is_default`/`is_active`/
 * `is_system` at all (see `_bracketUnexpressibleFields` in agents.js — the
 * decompiler already refuses to produce CLI text for a row where any of
 * those four would be lost), so a PATCH built straight from it can never
 * regress them — every field PATCH doesn't receive stays at its current
 * value, same "absent = unchanged" convention `updateConsoleAgent` relies
 * on above.
 * @param {number} id — the OrderTemplate row being edited
 * @param {any} compiledTemplate — `compiled.template`
 */
export async function updateConsoleBracket(id, compiledTemplate) {
  if (id == null) return { ok: false, error: 'no bracket selected for update' };
  if (!compiledTemplate) return { ok: false, error: 'nothing compiled to save' };
  try {
    const response = await patchOrderTemplate(id, compiledTemplate);
    try { await reloadOrderTemplates(); } catch { /* non-fatal — next loader call just refetches */ }
    return { ok: true, response };
  } catch (e) {
    return { ok: false, error: e?.message || 'bracket update failed' };
  }
}

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
 * @param {any} compiledFragment — `compiled.fragment` (`{kind, name, body?, conditions?, description?}`)
 * @param {string} rawText — the raw (edited) CLI text, stored as cli_source
 */
export async function updateConsoleToken(slug, compiledFragment, rawText = '') {
  if (!slug) return { ok: false, error: 'no token selected for update' };
  if (!compiledFragment) return { ok: false, error: 'nothing compiled to save' };
  // 2026-10-12 — condition-kind Token editing, same branch submitConsoleToken uses.
  const isCondition = compiledFragment.kind === 'condition';
  const payload = {
    description: compiledFragment.description || '',
    conditions: isCondition ? (compiledFragment.conditions || {}) : {},
    events: isCondition ? [] : (compiledFragment.body || []),
    cli_source: rawText || '',
  };
  try {
    await updateAgent(slug, payload);
    return { ok: true, slug };
  } catch (e) {
    return { ok: false, error: e?.message || 'token update failed' };
  }
}
