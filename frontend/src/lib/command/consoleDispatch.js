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
// ── `bracket="slug"` resolution — KNOWN GAP, do not silently "fix" ─────
// The one existing server-side resolver for this exact reference shape
// (`backend/api/algo/template_attach.py:load_template_for_slug_or_id`,
// used by the Lab/MCP `place_order` tool) matches ONLY on
// `OrderTemplate.slug` — exact string equality, never on `name`. This
// module mirrors that canonical behaviour exactly, on purpose: operator-
// authored custom templates leave `slug` NULL and are addressed by `name`
// everywhere else in the app (`backend/api/models.py` lines ~1531-1533),
// so a `bracket="my-custom-template"` reference where "my-custom-template"
// is a custom template's NAME (not its slug) will NOT resolve today — it
// surfaces a clear "Unknown bracket" error rather than guessing. Widening
// this to also match on `name` would diverge from the canonical resolver
// and risks silently picking the wrong row when name and slug disagree.
// This is a pre-existing, shared limitation (the Lab/MCP path has the
// identical gap) — flagged here, not patched here.

import { fetchOrderTemplates, placeTicketOrder, placeBasket, createAgent, createOrderTemplate, createAgentFragment } from '$lib/api';
import { reloadOrderTemplates } from '$lib/data/templates';

// ─────────────────────────────────────────────────────────────────────────
// (a) Classification
// ─────────────────────────────────────────────────────────────────────────

const _ORDER_VERBS = new Set(['buy', 'sell', 'modify', 'cancel']);
// WHEN / ALERT / DO are agent_stmt's three (each-optional) clause
// keywords — matching any one of them as the FIRST word is unambiguous
// because none of them can ever be a real order verb or a bare
// order/place_order/bracket call name (agents.js's own _RESERVED set).
const _AGENT_HEAD_RE = /^(when|alert|do)\b/i;
// A bare `order(...)`/`place_order(...)`/`bracket(...)`/`token(...)` call —
// matched on just the "name(" prefix (not a balanced-paren scan) so a
// STILL-BEING-TYPED statement like `order(acct=` classifies correctly
// before the operator has finished typing the closing paren. `token(...)`
// added Sprint 14 — without it, a `token(kind=notify) ...` statement typed
// into the legacy CommandLineTab fell through to 'legacy' and got POSTed
// to /api/admin/exec as a shell command.
const _AGENT_CALL_RE = /^(order|place_order|bracket|token)\s*\(/i;

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
    templateRows = await fetchOrderTemplates();
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
 * `{kind, name, body, description?}`, already `FragmentCreate`-shaped —
 * see `compileTokenStmt` in agents.js) as a new Token via the existing
 * `createAgentFragment` (`POST /api/admin/fragments`). Mirrors
 * `submitConsoleBracket`'s pattern exactly; unlike `submitConsoleAgent`
 * there's no slug to derive — `fragment.name` is sent as-is (the backend
 * itself lowercases + validates it's alnum/hyphen-only).
 * @param {any} compiledFragment — `compiled.fragment` from compileAgentCliStatement
 */
export async function submitConsoleToken(compiledFragment) {
  if (!compiledFragment) return { ok: false, error: 'token requires a #name= tag' };
  try {
    const response = await createAgentFragment(compiledFragment);
    return { ok: true, response };
  } catch (e) {
    return { ok: false, error: e?.message || 'token create failed' };
  }
}
