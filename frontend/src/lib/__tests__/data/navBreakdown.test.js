import { describe, it, expect } from 'vitest';

// Pure-logic tests for NavBreakdown account union algorithm.
//
// NavBreakdown._allAccounts builds a Set from three data sources
// (funds, positions, holdings) plus connStatus.accounts. This file
// tests the union logic in isolation — no Svelte component mount
// required because the logic is a straightforward Set iteration.

/**
 * Mirrors the _allAccounts union logic from NavBreakdown.svelte.
 * Used as the reference implementation under test.
 *
 * @param {Array<{account?: string}>} funds
 * @param {Array<{account?: string}>} positions
 * @param {Array<{account?: string}>} holdings
 * @param {string[]} connAccounts
 * @returns {string[]}
 */
function buildAccountSet(funds, positions, holdings, connAccounts) {
  const set = new Set();
  for (const r of funds)     if (r.account && r.account !== 'TOTAL') set.add(String(r.account));
  for (const r of positions) if (r.account) set.add(String(r.account));
  for (const r of holdings)  if (r.account) set.add(String(r.account));
  for (const a of connAccounts) if (a) set.add(String(a));
  return [...set];
}

describe('NavBreakdown account union', () => {
  it('includes connStatus accounts even with no holdings/positions data', () => {
    const funds     = [{ account: 'ZG0790', balance: 100 }];
    const positions = [];
    const holdings  = [{ account: 'ZG0790', pnl: 0 }];
    const connAccounts = ['ZG0790', 'DH3747', 'DH6847'];

    const result = buildAccountSet(funds, positions, holdings, connAccounts);

    expect(result).toContain('ZG0790');
    expect(result).toContain('DH3747');
    expect(result).toContain('DH6847');
    expect(result).toHaveLength(3);
  });

  it('excludes TOTAL pseudo-account from funds', () => {
    const funds     = [{ account: 'ZG0790' }, { account: 'TOTAL' }];
    const positions = [];
    const holdings  = [];
    const connAccounts = [];

    const result = buildAccountSet(funds, positions, holdings, connAccounts);

    expect(result).not.toContain('TOTAL');
    expect(result).toContain('ZG0790');
  });

  it('deduplicates accounts that appear in multiple sources', () => {
    const funds     = [{ account: 'ZG0790' }];
    const positions = [{ account: 'ZG0790' }];
    const holdings  = [{ account: 'ZG0790' }];
    const connAccounts = ['ZG0790'];

    const result = buildAccountSet(funds, positions, holdings, connAccounts);

    expect(result.filter(a => a === 'ZG0790')).toHaveLength(1);
  });

  it('returns empty array when all sources are empty', () => {
    const result = buildAccountSet([], [], [], []);
    expect(result).toHaveLength(0);
  });

  it('handles connStatus with null/empty string entries gracefully', () => {
    const connAccounts = ['ZG0790', '', null];

    // Cast to string[] as the runtime will — guard rejects falsy
    const result = buildAccountSet([], [], [], connAccounts.filter(Boolean));

    expect(result).toContain('ZG0790');
    expect(result).not.toContain('');
    expect(result).not.toContain('null');
  });

  it('picks up positions-only account absent from funds and holdings', () => {
    const funds     = [];
    const positions = [{ account: 'ZG0790' }];
    const holdings  = [];
    const connAccounts = [];

    const result = buildAccountSet(funds, positions, holdings, connAccounts);

    expect(result).toContain('ZG0790');
  });

  it('connStatus accounts only — no data rows at all — still appear', () => {
    const connAccounts = ['DH3747', 'DH6847'];

    const result = buildAccountSet([], [], [], connAccounts);

    expect(result).toContain('DH3747');
    expect(result).toContain('DH6847');
    expect(result).toHaveLength(2);
  });
});
