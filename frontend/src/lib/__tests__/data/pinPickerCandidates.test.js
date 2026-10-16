import { describe, it, expect } from 'vitest';
import { pinPickerCandidatesFromRows } from '../../data/pinPickerCandidates.js';

describe('pinPickerCandidatesFromRows', () => {
  const rows = [
    { account: 'ZG1234', tradingsymbol: 'NIFTY25JUN22000CE', quantity: 50 },
    { account: 'ZG1234', tradingsymbol: 'NIFTY25JUN22000PE', quantity: -50 },
    { account: 'ZG1234', tradingsymbol: 'NIFTY25JUN21000CE', quantity: 0 },
    { account: 'DH9876', tradingsymbol: 'BANKNIFTY25JUN50000CE', quantity: 25 },
  ];

  it('returns other open positions in the same account, excluding the source symbol', () => {
    const r = pinPickerCandidatesFromRows(rows, 'ZG1234', 'NIFTY25JUN22000CE');
    expect(r).toEqual([
      { symbol: 'NIFTY25JUN22000PE', account: 'ZG1234', qty: -50 },
    ]);
  });

  it('excludes qty=0 rows (flat/closed positions)', () => {
    const r = pinPickerCandidatesFromRows(rows, 'ZG1234', 'NIFTY25JUN22000PE');
    expect(r.find(c => c.symbol === 'NIFTY25JUN21000CE')).toBeUndefined();
  });

  it('excludes rows from a different account', () => {
    const r = pinPickerCandidatesFromRows(rows, 'ZG1234', 'NIFTY25JUN22000CE');
    expect(r.find(c => c.symbol === 'BANKNIFTY25JUN50000CE')).toBeUndefined();
  });

  it('account comparison is case-insensitive', () => {
    const r = pinPickerCandidatesFromRows(rows, 'zg1234', 'NIFTY25JUN22000CE');
    expect(r).toHaveLength(1);
    expect(r[0].symbol).toBe('NIFTY25JUN22000PE');
  });

  it('falls back to `symbol` field when `tradingsymbol` is absent', () => {
    const r = pinPickerCandidatesFromRows(
      [{ account: 'ZG1234', symbol: 'INFY', quantity: 10 }],
      'ZG1234',
      'TCS',
    );
    expect(r).toEqual([{ symbol: 'INFY', account: 'ZG1234', qty: 10 }]);
  });

  it('falls back to `qty` field when `quantity` is absent', () => {
    const r = pinPickerCandidatesFromRows(
      [{ account: 'ZG1234', symbol: 'INFY', qty: 10 }],
      'ZG1234',
      'TCS',
    );
    expect(r).toEqual([{ symbol: 'INFY', account: 'ZG1234', qty: 10 }]);
  });

  it('dedupes a tradingsymbol reported twice under one account (NRML + MIS)', () => {
    const dupRows = [
      { account: 'ZG1234', tradingsymbol: 'INFY', quantity: 10, product: 'NRML' },
      { account: 'ZG1234', tradingsymbol: 'INFY', quantity: 5, product: 'MIS' },
    ];
    const r = pinPickerCandidatesFromRows(dupRows, 'ZG1234', 'TCS');
    expect(r).toHaveLength(1);
    expect(r[0].symbol).toBe('INFY');
  });

  it('returns [] when account is empty', () => {
    expect(pinPickerCandidatesFromRows(rows, '', 'NIFTY25JUN22000CE')).toEqual([]);
  });

  it('returns [] when rows is null/undefined/not an array', () => {
    expect(pinPickerCandidatesFromRows(null, 'ZG1234', 'X')).toEqual([]);
    expect(pinPickerCandidatesFromRows(undefined, 'ZG1234', 'X')).toEqual([]);
    expect(pinPickerCandidatesFromRows(/** @type {any} */ ('nope'), 'ZG1234', 'X')).toEqual([]);
  });

  it('excludes the source symbol case-insensitively', () => {
    const r = pinPickerCandidatesFromRows(rows, 'ZG1234', 'nifty25jun22000ce');
    expect(r.find(c => c.symbol === 'NIFTY25JUN22000CE')).toBeUndefined();
  });
});
