// automationKinds.js — Sprint C (2026-10, Phase 2 "Automation redesign").
//
// Pure, backend-agnostic adapter sitting between the EXISTING APIs
// (agents-list for Agents/Alerts) and the new Automation "Agents" tab UI:
// kind-count-card -> per-kind #group= tabs -> row list. This is the ONE
// seam Sprint D2 (dropping the separate /api/admin/fragments route + its
// agent_fragments table — Templates now source from the unified
// agents-list API like Agents/Alerts do) swapped underneath with zero
// change to this module.
//
// The Brackets kind-card (a third top-level card, sourced from
// `GET /api/admin/templates/` reshaped via `bracketAgentToTemplateOut`/
// `bracketRowsFromAgents`, classified via `classifyBracketCardId`) was
// removed 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal, operator:
// "in automation, the brackets dropdown should not exist, as bracket are
// no longer available") — those three functions and the `brackets` entry
// in `KIND_CARDS` are gone. `classifyAgentCardId`'s own `kind === 'bracket'`
// exclusion (below) is KEPT — a historical Agent row with that kind may
// still exist in the database, and it must still be excluded from every
// card, never shown anywhere.
//
// "Groups are strictly per-kind" (CLAUDE.md, Sprint C) is enforced
// STRUCTURALLY here, not by convention: groupRowsByGroup() has zero
// kind-awareness and operates on whatever row array it's handed — the
// caller is always responsible for pre-filtering to one kind's own rows
// first (via classifyAgentCardId). There is no code path inside this
// module that could accidentally merge two kinds' rows into one group tab.

/** The two top-level kind-cards, in display order. Templates were
 *  folded into Agents (2026-10-12, operator: "there are no agent
 *  templates. they are either brackets, alerts or agents... move them
 *  under agents") — a kind='template' row is a condition/notify
 *  snippet referenced via $ref, not a genuinely distinct top-level
 *  category. They now classify as 'agents' (see `classifyAgentCardId`
 *  below) and are distinguished within that row list by their own
 *  `group` tag + inline `.log-chip` detail, same as any other row —
 *  no forced/synthetic group value is applied here. */
export const KIND_CARDS = [
  { id: 'agents',    label: 'Action agent' },
  { id: 'alerts',    label: 'Alert agent' },
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
  // 'bracket' — EXCLUDED here (null), same as the 'manual' exclusion
  // above. The backend Bracket engine and the Brackets kind-card are
  // both gone (2026-11, Track 3 T3.4, Bracket/Template/GTT removal); a
  // historical kind='bracket' row must never render anywhere.
  if (agent.kind === 'bracket') return null;
  // 'cycle' (current canonical spelling), 'threshold' (legacy synonym —
  // see backend's _age_normalize_kind, already normalized server-side by
  // the time this row reaches the frontend), or an absent/unrecognized
  // kind all land on the ordinary Agents card.
  return 'agents';
}

function _normGroup(g) {
  const s = (g == null ? '' : String(g)).trim();
  return s || null;
}

/** Group one kind's own, already-filtered rows by their `group` field
 *  (the same API field on `AgentInfo` — `group_name` at the DB/ORM
 *  layer). Returns `[{id, name, rows}]` — every named group
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

/** Row counts for the top-level kind-cards, built the same way
 *  `OrderBook.svelte`'s own `_statusCounts` status-chip-count row is
 *  built: one small object, one key per card id. `agentRows` is the raw
 *  `GET /api/agents/` list (every kind mixed together, `manual` included —
 *  this function does the `manual` exclusion itself via
 *  `classifyAgentCardId`). The `templateRows` param (Brackets' own count)
 *  was removed 2026-11, Track 3 T3.4, Bracket/Template/GTT removal. */
export function kindCounts(agentRows) {
  const counts = { agents: 0, alerts: 0 };
  for (const a of agentRows || []) {
    const id = classifyAgentCardId(a);
    if (id) counts[id] += 1;
  }
  return counts;
}

/** Convenience: every Agent row belonging to one kind-card, `manual`
 *  already excluded. `cardId` is one of 'agents'|'alerts'.
 *  `cardId: 'agents'` includes both ordinary cycle agents AND
 *  `kind='template'` rows (folded in 2026-10-12 — see KIND_CARDS). */
export function filterAgentRowsByCard(agentRows, cardId) {
  return (agentRows || []).filter((a) => classifyAgentCardId(a) === cardId);
}
