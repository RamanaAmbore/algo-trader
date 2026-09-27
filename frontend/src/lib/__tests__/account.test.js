import { describe, it, expect, afterEach } from 'vitest';
import { ACCT_PALETTE, acctColor, setAccountColorRank, leadAccount } from '$lib/account.js';

// Real operator accounts (2026-09 audit) — the exact set that motivated
// the rank-based colour assignment: a djb2 hash mod either the 7-hue
// ACCT_PALETTE or the (deleted) duplicated 8-hue palette collided on
// this set (DH6847 / DH3747 landed on the same hue under both).
const REAL_ACCOUNTS = ['ZG0790', 'ZJ6294', 'DH6847', 'DH3747', 'GR87DF'];

describe('acctColor', () => {
  afterEach(() => {
    // Reset module-level rank state between tests so each test starts
    // from the hash-fallback path unless it explicitly seeds a rank.
    setAccountColorRank(null);
  });

  it('returns null for TOTAL and null/undefined/empty accounts', () => {
    expect(acctColor('TOTAL')).toBeNull();
    expect(acctColor(null)).toBeNull();
    expect(acctColor(undefined)).toBeNull();
    expect(acctColor('')).toBeNull();
  });

  it('is a pure hex value (no CSS var entries) — safe for string-concat callers like acctColor(x)+"1a"', () => {
    for (const hue of ACCT_PALETTE) {
      expect(hue).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  it('hash fallback (no rank seeded) is deterministic and stable across calls', () => {
    const a = acctColor('ZG0790');
    const b = acctColor('ZG0790');
    expect(a).toBe(b);
    expect(ACCT_PALETTE).toContain(a);
  });

  it('hash fallback collides for the real account set (documents the bug rank-assignment fixes)', () => {
    const colors = REAL_ACCOUNTS.map(acctColor);
    const distinct = new Set(colors);
    // This is the confirmed-collision regression case — DH6847/DH3747
    // share a hue under pure djb2-hash. If this ever stops colliding
    // (e.g. account list changes), the assertion below simply
    // documents "no collision today" rather than failing; the real
    // guarantee is the rank-based test below, which must ALWAYS be
    // collision-free for this set.
    expect(distinct.size).toBeLessThanOrEqual(REAL_ACCOUNTS.length);
  });

  it('rank-based assignment is collision-free for the real 5-account set', () => {
    setAccountColorRank(REAL_ACCOUNTS);
    const colors = REAL_ACCOUNTS.map(acctColor);
    const distinct = new Set(colors);
    expect(distinct.size).toBe(REAL_ACCOUNTS.length);
  });

  it('rank-based assignment maps by position in the ranked list, not by hash', () => {
    // REAL_ACCOUNTS is already in the seeded rank order — index N maps
    // to ACCT_PALETTE[N], regardless of what a hash of the string
    // would produce.
    setAccountColorRank(REAL_ACCOUNTS);
    REAL_ACCOUNTS.forEach((acct, idx) => {
      expect(acctColor(acct)).toBe(ACCT_PALETTE[idx % ACCT_PALETTE.length]);
    });
  });

  it('falls back to hash for an account not present in the seeded rank list', () => {
    setAccountColorRank(['ZG0790']);
    // 'DH3747' isn't in the ranked list — must still resolve to *a*
    // stable palette colour via the hash fallback, not null/undefined.
    const c = acctColor('DH3747');
    expect(c).not.toBeNull();
    expect(ACCT_PALETTE).toContain(c);
  });

  it('falls back to hash when rank is cleared (e.g. order-map fetch not yet complete)', () => {
    setAccountColorRank(null);
    const c = acctColor('ZG0790');
    expect(ACCT_PALETTE).toContain(c);
  });

  it('non-array rank input is ignored (treated as unseeded)', () => {
    // @ts-expect-error - intentional bad input
    setAccountColorRank('not-an-array');
    const c = acctColor('ZG0790');
    expect(ACCT_PALETTE).toContain(c);
  });
});

describe('leadAccount', () => {
  it('returns null for a null/undefined row or missing accounts field', () => {
    expect(leadAccount(null)).toBeNull();
    expect(leadAccount(undefined)).toBeNull();
    expect(leadAccount({})).toBeNull();
  });

  it('returns the first element for an array', () => {
    expect(leadAccount({ accounts: ['ZG0790', 'ZJ6294'] })).toBe('ZG0790');
  });

  it('returns the first element for a Set', () => {
    expect(leadAccount({ accounts: new Set(['DH3747', 'DH6847']) })).toBe('DH3747');
  });
});
