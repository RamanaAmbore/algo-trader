// automationKinds.js — Sprint C (2026-10, Phase 2 "Automation redesign").
//
// Pure, backend-agnostic adapter sitting between the three EXISTING APIs
// (agents-list, /api/admin/templates, /api/admin/fragments) and the new
// Automation "Agents" tab UI: kind-count-card -> per-kind #group= tabs ->
// row list. This is the ONE seam Sprint D2 (dropping agent_fragments) and
// a future Sprint E (Brackets becoming kind='bracket' Agent rows) later
// swap underneath — the card/tab/row UI itself never changes as long as
// this module's output shape stays constant across that swap.
//
// "Groups are strictly per-kind" (CLAUDE.md, Sprint C) is enforced
// STRUCTURALLY here, not by convention: groupRowsByGroup() has zero
// kind-awareness and operates on whatever row array it's handed — the
// caller is always responsible for pre-filtering to one kind's own rows
// first (via classifyAgentCardId/classifyBracketCardId). There is no
// code path inside this module that could accidentally merge two kinds'
// rows into one group tab.

/** The four top-level kind-cards, in display order. */
export const KIND_CARDS = [
  { id: 'agents',    label: 'Agents' },
  { id: 'alerts',    label: 'Alerts' },
  { id: 'brackets',  label: 'Brackets' },
  { id: 'templates', label: 'Templates' },
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
  if (agent.kind === 'template') return 'templates';
  // 'cycle' (current canonical spelling), 'threshold' (legacy synonym —
  // see backend's _age_normalize_kind, already normalized server-side by
  // the time this row reaches the frontend), or an absent/unrecognized
  // kind all land on the ordinary Agents card.
  return 'agents';
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
  const counts = { agents: 0, alerts: 0, brackets: (templateRows || []).length, templates: 0 };
  for (const a of agentRows || []) {
    const id = classifyAgentCardId(a);
    if (id) counts[id] += 1;
  }
  return counts;
}

/** Convenience: every Agent row belonging to one kind-card, `manual`
 *  already excluded. `cardId` is one of 'agents'|'alerts'|'templates' —
 *  passing 'brackets' always returns `[]` (Agent rows are never Brackets;
 *  use `filterTemplateRows`/the raw template list for that card instead). */
export function filterAgentRowsByCard(agentRows, cardId) {
  return (agentRows || []).filter((a) => classifyAgentCardId(a) === cardId);
}
