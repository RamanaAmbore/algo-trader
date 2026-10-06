import { describe, it, expect } from 'vitest';
import { parseAttached, gttStatusLabel, matchGtts } from '$lib/data/gttMatch.js';

const json = [
  { kind: 'gtt', label: 'tp', id: '101' },
  { kind: 'gtt', label: 'sl', id: '102' },
  { kind: 'wing', label: 'wing', id: '900' },
];

describe('parseAttached', () => {
  it('parses a JSON string and an array', () => {
    expect(parseAttached(JSON.stringify(json))).toHaveLength(3);
    expect(parseAttached(json)).toHaveLength(3);
  });
  it('returns an empty list for null or bad JSON', () => {
    expect(parseAttached(null)).toEqual([]);
    expect(parseAttached('{not json')).toEqual([]);
  });
});

describe('gttStatusLabel', () => {
  it('maps broker statuses to display labels', () => {
    expect(gttStatusLabel('ACTIVE')).toBe('active');
    expect(gttStatusLabel('triggered')).toBe('triggered');
    expect(gttStatusLabel('disabled')).toBe('cancelled');
    expect(gttStatusLabel('')).toBe('unknown');
  });
});

describe('matchGtts', () => {
  it('pairs each GTT leg with its broker row by id', () => {
    const orders = [{ order_id: 1, attached_gtts_json: json }];
    const broker = [
      { gtt_id: '101', status: 'active', trigger_values: [90] },
      { gtt_id: '102', status: 'triggered', trigger_values: [120] },
    ];
    const { legsByOrder, unmatched } = matchGtts(orders, broker);
    const legs = legsByOrder.get('1');
    expect(legs.map(l => [l.label, l.status, l.missing])).toEqual([
      ['tp', 'active', false],
      ['sl', 'triggered', false],
    ]);
    expect(unmatched).toEqual([]);
  });

  it('flags a stored leg that is not at the broker as missing', () => {
    const orders = [{ order_id: 2, attached_gtts_json: [{ kind: 'gtt', label: 'sl', id: '777' }] }];
    const { legsByOrder } = matchGtts(orders, []);
    expect(legsByOrder.get('2')[0]).toMatchObject({ status: 'missing', missing: true });
  });

  it('lists broker GTTs that no order refers to', () => {
    const orders = [{ order_id: 1, attached_gtts_json: json }];
    const broker = [
      { gtt_id: '101', status: 'active' },
      { gtt_id: '555', status: 'active', tradingsymbol: 'CRUDEOIL' },
    ];
    const { unmatched } = matchGtts(orders, broker);
    expect(unmatched.map(r => r.gtt_id)).toEqual(['555']);
  });

  it('ignores wing entries for leg matching', () => {
    const orders = [{ order_id: 3, attached_gtts_json: json }];
    const { legsByOrder } = matchGtts(orders, [{ gtt_id: '900', status: 'active' }]);
    expect(legsByOrder.get('3').map(l => l.id)).toEqual(['101', '102']);
  });
});
