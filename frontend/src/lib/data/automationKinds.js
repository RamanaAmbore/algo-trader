// automationKinds.js — Sprint C (2026-10, Phase 2 "Automation redesign").
//
// Pure, backend-agnostic adapter sitting between the EXISTING APIs
// (agents-list for Agents/Alerts/Templates, /api/admin/templates for
// Brackets) and the new Automation "Agents" tab UI: kind-count-card ->
// per-kind #group= tabs -> row list. This is the ONE seam Sprint D2
// (dropping the separate /api/admin/fragments route + its agent_fragments
// table — Templates now source from the unified agents-list API like
// Agents/Alerts do) swapped underneath with zero change to this module;
// a future Sprint E (Brackets becoming kind='bracket' Agent rows) will
// do the same for /api/admin/templates — the card/tab/row UI itself
// never changes as long as this module's output shape stays constant
// across that swap.
//
// "Groups are strictly per-kind" (CLAUDE.md, Sprint C) is enforced
// STRUCTURALLY here, not by convention: groupRowsByGroup() has zero
// kind-awareness and operates on whatever row array it's handed — the
// caller is always responsible for pre-filtering to one kind's own rows
// first (via classifyAgentCardId/classifyBracketCardId). There is no
// code path inside this module that could accidentally merge two kinds'
// rows into one group tab.

/** The three top-level kind-cards, in display order. Templates were
 *  folded into Agents (2026-10-12, operator: "there are no agent
 *  templates. they are either brackets, alerts or agents... move them
 *  under agents") — a kind='template' row is a condition/notify
 *  snippet referenced via $ref, not a genuinely distinct top-level
 *  category. They now classify as 'agents' (see `classifyAgentCardId`
 *  below) and are distinguished within that row list by their own
 *  `group` tag + inline `.log-chip` detail, same as any other row —
 *  no forced/synthetic group value is applied here. */
export const KIND_CARDS = [
  { id: 'agents',    label: 'Action agents' },
  { id: 'alerts',    label: 'Alert agents' },
  { id: 'brackets',  label: 'Brackets' },
];

const UNGROUPED = '__ungrouped__';
export const UNGROUPED_TAB_LABEL = 'Ungrouped';

/** Classify one Agent row (as returned by `GET /api/agents/`, any
 *  `AgentInfo`-shaped object) into its Automation kind-card id, or `null`
 *  for a row that should never appear in any card. The `manual` system row
 *  (a placeholder target for manually-placed orders — `conditions: null`,
 *  see `MANUAL_AGENT` in `agent_engine.py`) is the one such exclusion,
 *  matching the pre-Sprint-C flat grid's own `agents.filter(a => a.slug
 *  !== 'manual')` guard. */
export function classifyAgentCardId(agent) {
  if (!agent || agent.slug === 'manual') return null;
  if (agent.kind === 'event') return 'alerts';
  // 'template' rows (2026-10-12) now land on the ordinary Agents card —
  // see the KIND_CARDS comment above. Falls through to the final
  // `return 'agents'` below, same as 'cycle'/'threshold'/unrecognized.
  // 'bracket' (Sprint E, Agent/Bracket/Template unification phase) —
  // EXCLUDED here (null, same as the 'manual' exclusion above), NOT
  // reclassified to 'brackets'. Bracket counting/listing goes through
  // the separate `templateRows` parameter (now bracket-Agent rows,
  // reshaped by `bracketAgentToTemplateOut` before reaching this
  // module — see templates.js:loadOrderTemplates()), which is counted
  // independently in `kindCounts` below. If this function returned
  // 'brackets' instead of null, every bracket row riding along in the
  // full `/api/agents/` list (`agentRows`, the first argument to
  // `kindCounts`) would be counted a SECOND time on top of
  // `templateRows.length` — a real double-count bug, not a style
  // choice. `filterAgentRowsByCard(agentRows, 'brackets')` keeping its
  // existing "always returns []" contract (see that function's own
  // doc) depends on this returning null, not 'brackets'.
  if (agent.kind === 'bracket') return null;
  // 'cycle' (current canonical spelling), 'threshold' (legacy synonym —
  // see backend's _age_normalize_kind, already normalized server-side by
  // the time this row reaches the frontend), or an absent/unrecognized
  // kind all land on the ordinary Agents card.
  return 'agents';
}

/** Reconstruct an `OrderTemplateOut`-shaped row from one `kind='bracket'`
 *  Agent row (as returned by `GET /api/agents/`, any `AgentInfo`-shaped
 *  object) — Sprint E (Agent/Bracket/Template unification phase). Every
 *  existing Bracket consumer (OrderTicket's template dropdown, Chain,
 *  TemplateBar, SymbolPanel, the Automation page's Brackets kind-card,
 *  the console's `bracket="slug"` resolution) expects this exact shape,
 *  unchanged from when it was read straight off `GET /api/admin/
 *  templates/` — see loadOrderTemplates().
 *
 *  `id` is `agent.legacy_template_id` — the REAL, canonical
 *  `OrderTemplate.id` — NEVER `agent.id` (this Agent row's own,
 *  unrelated primary key). OrderTicket/console send this `id` straight
 *  through as `template_id` on a real order; sending `agent.id` instead
 *  would risk a numeric `Agent.id`/`OrderTemplate.id` collision silently
 *  arming the WRONG bracket on a live fill — the exact defect class the
 *  backend council-risk review (Sprint E) blocked dispatch on. Returns
 *  `null` for a non-bracket row (defensive — callers should already be
 *  filtering to kind='bracket' before mapping). */
export function bracketAgentToTemplateOut(agent) {
  if (!agent || agent.kind !== 'bracket') return null;
  const cfg = agent.bracket_config || {};
  return {
    id: agent.legacy_template_id,
    // `decompileBracket`'s refusal rule treats a non-null `slug` as
    // "system-seeded, can't round-trip" (Sprint 18 -- written when
    // OrderTemplate.slug was only ever set for system rows). Agent.slug
    // is unique/required on EVERY row now, so a custom bracket's mirror
    // gets bracket_registry.bracket_slug_for()'s synthetic `bracket-{id}`
    // placeholder, not a real slug -- passing it through verbatim here
    // made every custom bracket falsely trip that refusal. Only forward
    // a real slug for system rows; custom rows report null, restoring
    // the original "null = safe to decompile" signal the check depends on.
    slug: agent.is_system ? agent.slug : null,
    name: agent.name,
    description: agent.description || '',
    applies_to: cfg.applies_to ?? 'both',
    tp_pct: cfg.tp_pct ?? null,
    sl_pct: cfg.sl_pct ?? null,
    wing_premium_pct: cfg.wing_premium_pct ?? null,
    wing_strike_offset: cfg.wing_strike_offset ?? null,
    wing_max_spread_pct: cfg.wing_max_spread_pct ?? null,
    tp_order_type: cfg.tp_order_type || 'LIMIT',
    tp_scales_json: cfg.tp_scales_json ?? null,
    sl_trail_pct: cfg.sl_trail_pct ?? null,
    is_default: !!cfg.is_default,
    is_system: !!agent.is_system,
    is_active: agent.status === 'active',
    group: agent.group ?? null,
    tags: Array.isArray(agent.tags) ? agent.tags : [],
  };
}

/** Every `kind='bracket'` row out of a raw `GET /api/agents/` list,
 *  reshaped via `bracketAgentToTemplateOut` — the read-side counterpart
 *  to `classifyAgentCardId`'s exclusion of the same rows. */
export function bracketRowsFromAgents(agentRows) {
  return (agentRows || [])
    .filter((a) => a && a.kind === 'bracket')
    .map(bracketAgentToTemplateOut)
    .filter(Boolean);
}

/** Every `OrderTemplate` row (`GET /api/admin/templates/`) is a Bracket —
 *  table identity alone decides the kind (Sprint 7's storage note: no
 *  stored type-tag needed, unlike Agent rows which share one table across
 *  three kind-cards). Kept as a named function (not an inline constant)
 *  so call sites read symmetrically with `classifyAgentCardId` and a
 *  future Sprint E swap (Brackets -> kind='bracket' Agent rows) has one
 *  obvious place to change. */
export function classifyBracketCardId(_template) {
  return 'brackets';
}

function _normGroup(g) {
  const s = (g == null ? '' : String(g)).trim();
  return s || null;
}

/** Group one kind's own, already-filtered rows by their `group` field
 *  (same API field on both `AgentInfo` and `OrderTemplate` — `group_name`
 *  at the DB/ORM layer). Returns `[{id, name, rows}]` — every named group
 *  alphabetically, the ungrouped bucket always LAST (so a page landing on
 *  a kind with no custom groups yet still shows one tab, not zero). Never
 *  returns an empty array for a non-empty `rows` input. */
export function groupRowsByGroup(rows) {
  const byName = new Map();
  for (const row of rows || []) {
    const g = _normGroup(row?.group);
    const key = g || UNGROUPED;
    if (!byName.has(key)) byName.set(key, []);
    byName.get(key).push(row);
  }
  const names = [...byName.keys()]
    .filter((k) => k !== UNGROUPED)
    .sort((a, b) => a.localeCompare(b));
  const out = names.map((name) => ({ id: name, name, rows: byName.get(name) }));
  if (byName.has(UNGROUPED)) {
    out.push({ id: UNGROUPED, name: UNGROUPED_TAB_LABEL, rows: byName.get(UNGROUPED) });
  }
  return out;
}

/** Row counts for the four top-level kind-cards, built the same way
 *  `OrderBook.svelte`'s own `_statusCounts` status-chip-count row is
 *  built: one small object, one key per card id. `agentRows` is the raw
 *  `GET /api/agents/` list (every kind mixed together, `manual` included —
 *  this function does the `manual` exclusion itself via
 *  `classifyAgentCardId`); `templateRows` is the raw `GET
 *  /api/admin/templates/` list (every row is a Bracket). */
export function kindCounts(agentRows, templateRows) {
  const counts = { agents: 0, alerts: 0, brackets: (templateRows || []).length };
  for (const a of agentRows || []) {
    const id = classifyAgentCardId(a);
    if (id) counts[id] += 1;
  }
  return counts;
}

/** Convenience: every Agent row belonging to one kind-card, `manual`
 *  already excluded. `cardId` is one of 'agents'|'alerts' — passing
 *  'brackets' always returns `[]` (Agent rows are never Brackets; use
 *  `filterTemplateRows`/the raw template list for that card instead).
 *  `cardId: 'agents'` includes both ordinary cycle agents AND
 *  `kind='template'` rows (folded in 2026-10-12 — see KIND_CARDS). */
export function filterAgentRowsByCard(agentRows, cardId) {
  return (agentRows || []).filter((a) => classifyAgentCardId(a) === cardId);
}
