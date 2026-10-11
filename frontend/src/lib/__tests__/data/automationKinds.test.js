import { describe, it, expect } from 'vitest';
import {
  KIND_CARDS, classifyAgentCardId, classifyBracketCardId,
  groupRowsByGroup, kindCounts, filterAgentRowsByCard, UNGROUPED_TAB_LABEL,
  bracketAgentToTemplateOut, bracketRowsFromAgents,
} from '$lib/data/automationKinds.js';

describe('KIND_CARDS', () => {
  it('has exactly the four Sprint C cards, in display order', () => {
    expect(KIND_CARDS.map((c) => c.id)).toEqual(['agents', 'alerts', 'brackets', 'templates']);
    expect(KIND_CARDS.map((c) => c.label)).toEqual(['Agents', 'Alerts', 'Brackets', 'Templates']);
  });
});

describe('classifyAgentCardId', () => {
  it('kind=event -> alerts', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'event' })).toBe('alerts');
  });
  it('kind=template -> templates', () => {
    expect(classifyAgentCardId({ slug: 'x', kind: 'template' })).toBe('templates');
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
  it('Sprint E: kind=bracket -> null (excluded, NOT reclassified to "brackets" '
    + '— see the function doc for why reclassifying would double-count)', () => {
    expect(classifyAgentCardId({ slug: 'default-bull', kind: 'bracket' })).toBeNull();
  });
});

describe('bracketAgentToTemplateOut (Sprint E)', () => {
  it('reconstructs an OrderTemplateOut-shaped row, id = legacy_template_id — '
    + 'NEVER the Agent row\'s own id', () => {
    const agentRow = {
      id: 999,                    // the Agent row's OWN pk — must be ignored
      legacy_template_id: 42,     // the REAL, canonical OrderTemplate.id
      slug: 'default-bull',
      name: 'Default Bull',
      description: 'desc',
      kind: 'bracket',
      is_system: true,
      status: 'active',
      group: 'defaults',
      tags: ['x'],
      bracket_config: {
        tp_pct: 30, sl_pct: 20, wing_premium_pct: null,
        wing_strike_offset: null, wing_max_spread_pct: null,
        tp_order_type: 'LIMIT', tp_scales_json: null, sl_trail_pct: null,
        applies_to: 'buy_any', is_default: true,
      },
    };
    const out = bracketAgentToTemplateOut(agentRow);
    expect(out.id).toBe(42);
    expect(out.id).not.toBe(agentRow.id);
    expect(out.slug).toBe('default-bull');
    expect(out.tp_pct).toBe(30);
    expect(out.sl_pct).toBe(20);
    expect(out.applies_to).toBe('buy_any');
    expect(out.is_default).toBe(true);
    expect(out.is_system).toBe(true);
    expect(out.is_active).toBe(true);
    expect(out.group).toBe('defaults');
    expect(out.tags).toEqual(['x']);
  });

  it('is_active derives from status, not a stored bracket_config flag', () => {
    const out = bracketAgentToTemplateOut({
      id: 1, legacy_template_id: 2, slug: 's', name: 'n', kind: 'bracket',
      is_system: false, status: 'inactive', bracket_config: {},
    });
    expect(out.is_active).toBe(false);
  });

  it('returns null for a non-bracket row', () => {
    expect(bracketAgentToTemplateOut({ kind: 'cycle' })).toBeNull();
    expect(bracketAgentToTemplateOut(null)).toBeNull();
  });
});

describe('bracketRowsFromAgents (Sprint E)', () => {
  it('filters to kind=bracket only and reshapes each via bracketAgentToTemplateOut', () => {
    const agentRows = [
      { id: 1, slug: 'a1', kind: 'cycle' },
      { id: 2, legacy_template_id: 10, slug: 'default-bull', name: 'Default Bull',
        kind: 'bracket', is_system: true, status: 'active', bracket_config: { tp_pct: 30 } },
      { id: 3, slug: 'notify-trio', kind: 'template' },
    ];
    const out = bracketRowsFromAgents(agentRows);
    expect(out.length).toBe(1);
    expect(out[0].id).toBe(10);
    expect(out[0].slug).toBe('default-bull');
  });

  it('handles empty/missing input', () => {
    expect(bracketRowsFromAgents([])).toEqual([]);
    expect(bracketRowsFromAgents(undefined)).toEqual([]);
  });
});

describe('classifyBracketCardId', () => {
  it('every OrderTemplate row is a Bracket, regardless of its own fields', () => {
    expect(classifyBracketCardId({ id: 1, name: 'x' })).toBe('brackets');
    expect(classifyBracketCardId({})).toBe('brackets');
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
    const bracketRows = [{ id: 1, name: 'b1', group: 'hedge-book' }];

    const agentsOnly = filterAgentRowsByCard(agentRows, 'agents');
    const alertsOnly = filterAgentRowsByCard(agentRows, 'alerts');
    expect(agentsOnly).toEqual([{ kind: 'cycle', slug: 'a1', group: 'hedge-book' }]);
    expect(alertsOnly).toEqual([{ kind: 'event', slug: 'a2', group: 'hedge-book' }]);

    const agentsGroupTabs = groupRowsByGroup(agentsOnly);
    const alertsGroupTabs = groupRowsByGroup(alertsOnly);
    const bracketsGroupTabs = groupRowsByGroup(bracketRows);

    expect(agentsGroupTabs).toEqual([{ id: 'hedge-book', name: 'hedge-book', rows: [{ kind: 'cycle', slug: 'a1', group: 'hedge-book' }] }]);
    expect(alertsGroupTabs).toEqual([{ id: 'hedge-book', name: 'hedge-book', rows: [{ kind: 'event', slug: 'a2', group: 'hedge-book' }] }]);
    expect(bracketsGroupTabs).toEqual([{ id: 'hedge-book', name: 'hedge-book', rows: bracketRows }]);
    // Each kind's own "hedge-book" tab carries only that kind's rows.
    expect(agentsGroupTabs[0].rows.length).toBe(1);
    expect(alertsGroupTabs[0].rows.length).toBe(1);
    expect(bracketsGroupTabs[0].rows.length).toBe(1);
  });
});

describe('kindCounts', () => {
  it('counts each Agent sub-kind and the Brackets list, excluding the manual row', () => {
    const agentRows = [
      { slug: 'manual', kind: 'cycle' },
      { slug: 'loss-funds-negative', kind: 'cycle' },
      { slug: 'template-attach-high', kind: 'event' },
      { slug: 'notify-critical-trio', kind: 'template' },
      { slug: 'loss-margin-low', kind: 'threshold' },
    ];
    const templateRows = [{ id: 1, name: 'default-bull' }, { id: 2, name: 'default-bear' }];
    expect(kindCounts(agentRows, templateRows)).toEqual({
      agents: 2, alerts: 1, brackets: 2, templates: 1,
    });
  });

  it('handles empty/missing inputs without throwing', () => {
    expect(kindCounts([], [])).toEqual({ agents: 0, alerts: 0, brackets: 0, templates: 0 });
    expect(kindCounts(undefined, undefined)).toEqual({ agents: 0, alerts: 0, brackets: 0, templates: 0 });
  });

  it('Sprint E: kind=bracket rows riding along in the full agentRows list are '
    + 'NOT double-counted against the separately-supplied templateRows count', () => {
    // This is the exact shape GET /api/agents/ now returns post-Sprint-E —
    // bracket rows are real Agent rows, so they appear in the unfiltered
    // list too. Automation/+page.svelte passes the bracket-reshaped list
    // (via loadOrderTemplates()) as the SECOND argument separately.
    const agentRows = [
      { slug: 'loss-funds-negative', kind: 'cycle' },
      { slug: 'default-bull', kind: 'bracket' },
      { slug: 'default-bear', kind: 'bracket' },
    ];
    const templateRows = [{ id: 10, name: 'default-bull' }, { id: 11, name: 'default-bear' }];
    expect(kindCounts(agentRows, templateRows)).toEqual({
      agents: 1, alerts: 0, brackets: 2, templates: 0,
    });
  });
});

describe('filterAgentRowsByCard', () => {
  it('"brackets" always returns [] for Agent rows — Agent rows are never Brackets', () => {
    expect(filterAgentRowsByCard([{ slug: 'x', kind: 'cycle' }], 'brackets')).toEqual([]);
  });
});
