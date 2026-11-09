import { describe, it, expect } from 'vitest';
import {
  KIND_CARDS, classifyAgentCardId,
  groupRowsByGroup, kindCounts, filterAgentRowsByCard, UNGROUPED_TAB_LABEL,
} from '$lib/data/automationKinds.js';

// `classifyBracketCardId`, `bracketAgentToTemplateOut`, `bracketRowsFromAgents`
// and the Brackets kind-card removed 2026-11 (Track 3, T3.4, Bracket/
// Template/GTT removal, operator: "in automation, the brackets dropdown
// should not exist, as bracket are no longer available") — their test
// coverage below is removed too.

describe('KIND_CARDS', () => {
  it('has exactly the two cards (Templates folded into Agents, 2026-10-12; Brackets removed, 2026-11), in display order', () => {
    expect(KIND_CARDS.map((c) => c.id)).toEqual(['agents', 'alerts']);
    // 2026-10-29: labels renamed to distinguish order-placing agents from
    // notify-only alert agents in the count pills (operator: "call agents
    // as action agents, alerts as alert agents in count pills").
    // 2026-10-31: singularized ("Action agent" not "Action agents") to
    // match the Console statement-kind dropdown's own labels, now that
    // this same Select pattern is reused for both.
    expect(KIND_CARDS.map((c) => c.label)).toEqual(['Action agent', 'Alert agent']);
  });
});

describe('classifyAgentCardId', () => {
  it('kind=event -> alerts', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'event' })).toBe('alerts');
  });
  it('kind=template -> agents (folded in, 2026-10-12 — Templates are no longer a separate card)', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'template' })).toBe('agents');
  });
  it('kind=cycle -> agents', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'cycle' })).toBe('agents');
  });
  it('legacy kind=threshold -> agents (pre-normalize synonym)', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'threshold' })).toBe('agents');
  });
  it('absent/unknown kind -> agents (never crashes, never silently excluded)', () => {
    expect(classifyAgentCardId({ slug: 'x' })).toBe('agents');
    expect(classifyAgentCardId({ slug: 'x', kind: 'something-new' })).toBe('agents');
  });
  it('slug=manual -> null (the MANUAL_AGENT placeholder, excluded entirely)', () => {
    expect(classifyAgentCardId({ slug: 'manual', kind: 'cycle' })).toBeNull();
  });
  it('null/undefined row -> null', () => {
    expect(classifyAgentCardId(null)).toBeNull();
    expect(classifyAgentCardId(undefined)).toBeNull();
  });
  it('kind=bracket -> null (excluded — a historical row from before the Bracket engine was removed must never render anywhere)', () => {
    expect(classifyAgentCardId({ slug: 'default-bull', kind: 'bracket' })).toBeNull();
  });
});

describe('groupRowsByGroup', () => {
  it('buckets rows by group, alphabetical, ungrouped last', () => {
    const rows = [
      { name: 'a', group: 'zebra' },
      { name: 'b', group: null },
      { name: 'c', group: 'alpha' },
      { name: 'd', group: '' },
      { name: 'e', group: '  alpha  ' }, // trimmed, same bucket as 'c'
    ];
    const groups = groupRowsByGroup(rows);
    expect(groups.map((g) => g.name)).toEqual(['alpha', 'zebra', UNGROUPED_TAB_LABEL]);
    expect(groups[0].rows.map((r) => r.name)).toEqual(['c', 'e']);
    expect(groups[1].rows.map((r) => r.name)).toEqual(['a']);
    expect(groups[2].rows.map((r) => r.name)).toEqual(['b', 'd']);
  });

  it('returns a single ungrouped bucket when no row has a group', () => {
    const groups = groupRowsByGroup([{ name: 'a' }, { name: 'b' }]);
    expect(groups.length).toBe(1);
    expect(groups[0].name).toBe(UNGROUPED_TAB_LABEL);
    expect(groups[0].rows.length).toBe(2);
  });

  it('returns [] for an empty/missing row list', () => {
    expect(groupRowsByGroup([])).toEqual([]);
    expect(groupRowsByGroup(undefined)).toEqual([]);
  });

  it('never mixes kinds — caller must pre-filter, and doing so keeps a shared group name separate per kind', () => {
    // Two different kinds' rows share the group name "hedge-book" — this
    // module has no kind field to read at all, so the only way kinds
    // never mix is that the CALLER always filters to one kind first.
    // This test proves that discipline actually produces disjoint tabs.
    const agentRows = [{ kind: 'cycle', slug: 'a1', group: 'hedge-book' }, { kind: 'event', slug: 'a2', group: 'hedge-book' }];

    const agentsOnly = filterAgentRowsByCard(agentRows, 'agents');
    const alertsOnly = filterAgentRowsByCard(agentRows, 'alerts');
    expect(agentsOnly).toEqual([{ kind: 'cycle', slug: 'a1', group: 'hedge-book' }]);
    expect(alertsOnly).toEqual([{ kind: 'event', slug: 'a2', group: 'hedge-book' }]);

    const agentsGroupTabs = groupRowsByGroup(agentsOnly);
    const alertsGroupTabs = groupRowsByGroup(alertsOnly);

    expect(agentsGroupTabs).toEqual([{ id: 'hedge-book', name: 'hedge-book', rows: [{ kind: 'cycle', slug: 'a1', group: 'hedge-book' }] }]);
    expect(alertsGroupTabs).toEqual([{ id: 'hedge-book', name: 'hedge-book', rows: [{ kind: 'event', slug: 'a2', group: 'hedge-book' }] }]);
    // Each kind's own "hedge-book" tab carries only that kind's rows.
    expect(agentsGroupTabs[0].rows.length).toBe(1);
    expect(alertsGroupTabs[0].rows.length).toBe(1);
  });
});

describe('kindCounts', () => {
  it('counts each Agent sub-kind, excluding the manual row (the Brackets count/templateRows param was removed 2026-11)', () => {
    const agentRows = [
      { slug: 'manual', kind: 'cycle' },
      { slug: 'loss-funds-negative', kind: 'cycle' },
      { slug: 'template-attach-high', kind: 'event' },
      { slug: 'notify-critical-trio', kind: 'template' },
      { slug: 'loss-margin-low', kind: 'threshold' },
    ];
    // 'notify-critical-trio' (kind=template) now counts under 'agents'
    // too (2026-10-12 — Templates folded into Agents): 2 cycle/threshold
    // rows + 1 template row = 3.
    expect(kindCounts(agentRows)).toEqual({ agents: 3, alerts: 1 });
  });

  it('handles empty/missing input without throwing', () => {
    expect(kindCounts([])).toEqual({ agents: 0, alerts: 0 });
    expect(kindCounts(undefined)).toEqual({ agents: 0, alerts: 0 });
  });

  it('a historical kind=bracket row riding along in the agentRows list is excluded entirely, never counted anywhere', () => {
    const agentRows = [
      { slug: 'loss-funds-negative', kind: 'cycle' },
      { slug: 'default-bull', kind: 'bracket' },
      { slug: 'default-bear', kind: 'bracket' },
    ];
    expect(kindCounts(agentRows)).toEqual({ agents: 1, alerts: 0 });
  });
});

describe('filterAgentRowsByCard', () => {
  it('"brackets" always returns [] for Agent rows — the Brackets card no longer exists', () => {
    expect(filterAgentRowsByCard([{ slug: 'x', kind: 'cycle' }], 'brackets')).toEqual([]);
  });

  it('"agents" includes both cycle rows AND kind=template rows (2026-10-12 — Templates folded into Agents)', () => {
    const rows = [
      { slug: 'a', kind: 'cycle' },
      { slug: 'b', kind: 'event' },
      { slug: 'c', kind: 'template' },
    ];
    expect(filterAgentRowsByCard(rows, 'agents')).toEqual([
      { slug: 'a', kind: 'cycle' },
      { slug: 'c', kind: 'template' },
    ]);
  });
});
