// Console dispatch layer (Sprint 9) — sits ahead of BOTH CLI grammars on
// /console. Decides, from the raw typed text alone, whether a statement
// belongs to the positional orders.js grammar (buy/sell/modify/cancel) or
// the call-style agents.js grammar (WHEN/ALERT/DO, bare order(...)/
// place_order(...)/bracket(...)) — then, for the orders path, resolves any
// `bracket="slug"` reference to a real numeric `template_id` before a
// ticket/basket request is ever built.
//
// Deliberately NOT a merge of the two grammars (confirmed out of scope in
// an earlier investigation — see .claude/PLAN.md history) — this module
// only classifies + (for the order-compile path) resolves + submits via
// the EXACT SAME API calls the existing Ticket/basket UI already uses.
// No parallel execution path is introduced.
//
// Sprint I Phase 1 (2026-10-11) — `dispatchConsoleStatement` (below) is
// the thin top-level router ConsoleTab.svelte now calls to decide between
// its own generic-grammar flow and CommandLineTab's headless
// `runExternal` fallback, collapsing the two previously-separate VISIBLE
// input boxes into one. The legacy orders.js parser itself is unchanged
// and not retired — see that function's own doc for the Phase 1/Phase 2
// split.
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
} from '$lib/api';
import { loadOrderTemplates, reloadOrderTemplates } from '$lib/data/templates';

// ─────────────────────────────────────────────────────────────────────────
// (a) Classification
// ─────────────────────────────────────────────────────────────────────────

const _ORDER_VERBS = new Set(['buy', 'sell', 'modify', 'cancel']);
// WHEN / ALERT / DO / GIVEN are agent_stmt's clause keywords — matching
// any one of them as the FIRST word is unambiguous because none of them
// can ever be a real order verb or a bare order/place_order/bracket call
// name (agents.js's own _RESERVED set). `given` added Sprint I Phase 1
// (2026-10-11) — Sprint G's GIVEN preamble (agents.js) was missing here,
// so `GIVEN x = ...; WHEN x ...` misclassified as 'legacy' and would
// have been POSTed to /api/admin/exec as a shell command.
const _AGENT_HEAD_RE = /^(when|alert|do|given)\b/i;
// A bare `order(...)`/`place_order(...)`/`bracket(...)`/`token(...)` call —
// matched on just the "name(" prefix (not a balanced-paren scan) so a
// STILL-BEING-TYPED statement like `order(acct=` classifies correctly
// before the operator has finished typing the closing paren. `token(...)`
// added Sprint 14 — without it, a `token(kind=notify) ...` statement typed
// into the legacy CommandLineTab fell through to 'legacy' and got POSTed
// to /api/admin/exec as a shell command. `cancel_all_orders(...)` added
// Sprint [cancel-all-cli] (2026-10) for the identical reason — without
// it, compile errors would be hidden (ConsoleTab's `showCompileErrors`
// gates on `_classify === 'agents'`), a spurious "Run shell command"
// fallback button would render alongside the real Cancel button, and
// Ctrl/Cmd+Enter would POST the raw text to /api/admin/exec instead of
// submitting the cancel-all request.
const _AGENT_CALL_RE = /^(order|place_order|bracket|token|cancel_all_orders)\s*\(/i;

/**
 * Classify a raw CLI statement by its head tokens alone — cheap regex
 * matching, NOT a full lex/parse (lexAgentStatement returns `tokens: []`
 * on unbalanced parens, which would misclassify a statement that's only
 * half-typed).
 * @param {string} text
 * @returns {'orders'|'agents'|'legacy'|'empty'}
 */
export function classifyConsoleStatement(text) {
  const t = String(text == null ? '' : text).trim();
  if (!t) return 'empty';
  if (_AGENT_HEAD_RE.test(t)) return 'agents';
  if (_AGENT_CALL_RE.test(t)) return 'agents';
  const head = (t.split(/\s+/)[0] || '').toLowerCase();
  if (_ORDER_VERBS.has(head)) return 'orders';
  // Anything else (shell commands, `agent list`/`agent status`, etc.)
  // keeps flowing through CommandLineTab's existing raw-command handling
  // (interpretAgent / POST /api/admin/exec) — zero behaviour change.
  return 'legacy';
}

/**
 * Sprint I (2026-10-11, Phase 1 "unify console's two CLI boxes into one
 * window via a dispatch layer") — the one function that decides which of
 * the two live CLI parsers handles a given raw statement typed into
 * Console's SINGLE visible textbox. Thin wrapper over
 * `classifyConsoleStatement` (reused, not re-implemented): 'agents' routes
 * to the caller's own generic-grammar flow (`compileAgentCliStatement` —
 * already fully wired in ConsoleTab.svelte, untouched by this function);
 * anything else ('orders': buy/sell/modify/cancel, or 'legacy': `agent
 * ...` interpret / shell passthrough) routes to the caller's legacy
 * fallback (CommandLineTab's `runExternal`, invoked headless). 'empty'
 * text calls neither callback.
 *
 * Deliberately does NOT itself call compileAgentCliStatement / submit /
 * fetch — both callbacks own their own compile + submission mechanics
 * exactly as before this sprint; this is pure routing so it stays trivial
 * to unit-test in isolation (see consoleDispatch.test.js).
 *
 * Phase 1 explicitly keeps BOTH parsers alive side by side — this is not
 * a merge of the two grammars (out of scope, confirmed in an earlier
 * investigation). Phase 2 (gated on the Sprint F parity checklist —
 * cancel/modify verbs, recall, /interpret AI-draft, /api/admin/exec
 * passthrough — actually passing) will retire this function along with
 * the legacy fallback branch entirely.
 *
 * @param {string} text
 * @param {{onAgentFamily?: () => any, onLegacyFallback?: () => any}} handlers
 * @returns {any} whatever the invoked callback returns (undefined for 'empty')
 */
export function dispatchConsoleStatement(text, { onAgentFamily, onLegacyFallback } = {}) {
  const kind = classifyConsoleStatement(text);
  if (kind === 'agents') return onAgentFamily?.();
  if (kind === 'empty') return undefined;
  return onLegacyFallback?.();
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
 * collapse shadow/sim/replay to paper — same override CommandLineTab's
 * own `_submitPlaceOrder` already applies via the `executionMode` store).
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
 */
export async function updateConsoleAgent(slug, compiledAgent, rawText) {
  if (!slug) return { ok: false, error: 'no agent selected for update' };
  if (!compiledAgent) return { ok: false, error: 'nothing compiled to save' };
  const payload = {
    name: compiledAgent.name,
    description: compiledAgent.description || '',
    conditions: compiledAgent.conditions,
    events: compiledAgent.events,
    actions: compiledAgent.actions,
    given: compiledAgent.given || [],
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
