import { describe, it, expect } from 'vitest';
import { expiryPnl, expiryPnlWithRealised, AVG_PRICE_IS_COST_BASIS } from '../../data/expiryPnl.js';

describe('expiryPnl', () => {
  const spot = 100;

  // ============================================================================
  // Call option tests
  // ============================================================================

  describe('call option (CE)', () => {
    it('uses qty when provided (long call)', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      // intrinsic at spot=100: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('uses quantity as fallback when qty is missing (long call)', () => {
      const c = { symbol: 'NIFTY100CE', quantity: 1, avg_cost: 5, kind: 'opt' };
      // intrinsic at spot=100: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('uses avg_cost when provided (long call)', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('uses average_price as fallback when avg_cost is missing (long call)', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, average_price: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('falls back to both quantity and average_price (API field names)', () => {
      const c = { symbol: 'NIFTY100CE', quantity: 2, average_price: 3, kind: 'opt' };
      // intrinsic at spot=100: max(0, 100-100) = 0
      // pnl: (0 - 3) * 2 = -6
      expect(expiryPnl(c, spot)).toBe(-6);
    });

    it('handles short call (negative qty)', () => {
      const c = { symbol: 'NIFTY100CE', qty: -1, avg_cost: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * (-1) = 5 (profit on short)
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('handles short call with quantity fallback', () => {
      const c = { symbol: 'NIFTY100CE', quantity: -1, average_price: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * (-1) = 5
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('handles ITM call with positive profit', () => {
      const c = { symbol: 'NIFTY90CE', quantity: 2, average_price: 3, kind: 'opt' };
      // intrinsic at spot=100: max(0, 100-90) = 10
      // pnl: (10 - 3) * 2 = 14
      expect(expiryPnl(c, spot)).toBe(14);
    });

    it('uses legAnalyticsBySymbol to override strike parsing', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      const legAnalytics = { 'NIFTY100CE': { strike: 95, opt_type: 'CE' } };
      // intrinsic at spot=100, strike=95: max(0, 100-95) = 5
      // pnl: (5 - 5) * 1 = 0
      expect(expiryPnl(c, spot, legAnalytics)).toBe(0);
    });
  });

  // ============================================================================
  // Put option tests
  // ============================================================================

  describe('put option (PE)', () => {
    it('uses qty when provided (long put)', () => {
      const c = { symbol: 'NIFTY100PE', qty: 1, avg_cost: 5, kind: 'opt' };
      // intrinsic at spot=100: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('uses quantity as fallback when qty is missing (long put)', () => {
      const c = { symbol: 'NIFTY100PE', quantity: 1, avg_cost: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('uses average_price as fallback when avg_cost is missing (long put)', () => {
      const c = { symbol: 'NIFTY100PE', qty: 1, average_price: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * 1 = -5
      expect(expiryPnl(c, spot)).toBe(-5);
    });

    it('falls back to both quantity and average_price (API field names)', () => {
      const c = { symbol: 'NIFTY100PE', quantity: 2, average_price: 3, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 3) * 2 = -6
      expect(expiryPnl(c, spot)).toBe(-6);
    });

    it('handles ITM put with positive profit', () => {
      const c = { symbol: 'NIFTY110PE', quantity: 2, average_price: 3, kind: 'opt' };
      // intrinsic at spot=100: max(0, 110-100) = 10
      // pnl: (10 - 3) * 2 = 14
      expect(expiryPnl(c, spot)).toBe(14);
    });

    it('handles short put (negative qty)', () => {
      const c = { symbol: 'NIFTY100PE', quantity: -1, average_price: 5, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 5) * (-1) = 5 (profit on short)
      expect(expiryPnl(c, spot)).toBe(5);
    });
  });

  // ============================================================================
  // Futures tests
  // ============================================================================

  describe('futures (fut)', () => {
    it('uses qty when provided', () => {
      const c = { symbol: 'NIFTY', qty: 1, avg_cost: 95, kind: 'fut' };
      // pnl: (spot - avg_cost) * qty = (100 - 95) * 1 = 5
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('uses quantity as fallback when qty is missing', () => {
      const c = { symbol: 'NIFTY', quantity: 1, avg_cost: 95, kind: 'fut' };
      // pnl: (100 - 95) * 1 = 5
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('uses average_price as fallback when avg_cost is missing', () => {
      const c = { symbol: 'NIFTY', qty: 1, average_price: 95, kind: 'fut' };
      // pnl: (100 - 95) * 1 = 5
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('falls back to both quantity and average_price (API field names)', () => {
      const c = { symbol: 'NIFTY', quantity: 2, average_price: 95, kind: 'fut' };
      // pnl: (100 - 95) * 2 = 10
      expect(expiryPnl(c, spot)).toBe(10);
    });

    it('handles short future (negative qty)', () => {
      const c = { symbol: 'NIFTY', quantity: -1, average_price: 105, kind: 'fut' };
      // pnl: (100 - 105) * (-1) = 5 (profit on short)
      expect(expiryPnl(c, spot)).toBe(5);
    });
  });

  // ============================================================================
  // Equity tests
  // ============================================================================

  describe('equity (eq)', () => {
    it('uses qty when provided', () => {
      const c = { symbol: 'TCS', qty: 10, avg_cost: 95, kind: 'eq' };
      // pnl: (100 - 95) * 10 = 50
      expect(expiryPnl(c, spot)).toBe(50);
    });

    it('uses quantity as fallback when qty is missing', () => {
      const c = { symbol: 'TCS', quantity: 10, avg_cost: 95, kind: 'eq' };
      // pnl: (100 - 95) * 10 = 50
      expect(expiryPnl(c, spot)).toBe(50);
    });

    it('falls back to both quantity and average_price (API field names)', () => {
      const c = { symbol: 'TCS', quantity: 10, average_price: 95, kind: 'eq' };
      // pnl: (100 - 95) * 10 = 50
      expect(expiryPnl(c, spot)).toBe(50);
    });
  });

  // ============================================================================
  // Edge cases
  // ============================================================================

  describe('edge cases', () => {
    it('returns null when spot is missing', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, null)).toBe(null);
      expect(expiryPnl(c, undefined)).toBe(null);
    });

    it('returns null when spot is non-positive', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, 0)).toBe(null);
      expect(expiryPnl(c, -10)).toBe(null);
    });

    it('returns null when qty is 0', () => {
      const c = { symbol: 'NIFTY100CE', qty: 0, avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, spot)).toBe(null);
    });

    it('returns null when both qty and quantity are missing (fallback to 0)', () => {
      const c = { symbol: 'NIFTY100CE', avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, spot)).toBe(null);
    });

    it('treats avg_cost=0 as valid (fresh intraday fills)', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 0, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 0) * 1 = 0 (still returns 0, not null)
      expect(expiryPnl(c, spot)).toBe(0);
    });

    it('treats average_price=0 as valid (fallback)', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, average_price: 0, kind: 'opt' };
      // intrinsic: max(0, 100-100) = 0
      // pnl: (0 - 0) * 1 = 0
      expect(expiryPnl(c, spot)).toBe(0);
    });

    it('returns null when option strike cannot be parsed and legAnalytics missing', () => {
      const c = { symbol: 'BADOPTION', qty: 1, avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, spot)).toBe(null);
    });

    it('returns null when option opt_type cannot be parsed and legAnalytics missing', () => {
      const c = { symbol: 'NIFTY100XX', qty: 1, avg_cost: 5, kind: 'opt' };
      expect(expiryPnl(c, spot)).toBe(null);
    });

    it('prefers legAnalytics strike over regex parse', () => {
      const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt' };
      const legAnalytics = { 'NIFTY100CE': { strike: 98, opt_type: 'CE' } };
      // intrinsic at spot=100, strike=98: max(0, 100-98) = 2
      // pnl: (2 - 5) * 1 = -3
      expect(expiryPnl(c, spot, legAnalytics)).toBe(-3);
    });
  });

  // ============================================================================
  // Field precedence tests
  // ============================================================================

  describe('field precedence (qty over quantity, avg_cost over average_price)', () => {
    it('prefers qty over quantity', () => {
      const c = { symbol: 'NIFTY', qty: 5, quantity: 999, avg_cost: 95, kind: 'fut' };
      // pnl should use qty=5: (100 - 95) * 5 = 25
      expect(expiryPnl(c, spot)).toBe(25);
    });

    it('prefers avg_cost over average_price', () => {
      const c = { symbol: 'NIFTY', qty: 1, avg_cost: 95, average_price: 999, kind: 'fut' };
      // pnl should use avg_cost=95: (100 - 95) * 1 = 5
      expect(expiryPnl(c, spot)).toBe(5);
    });

    it('uses quantity when qty is undefined and average_price when avg_cost is undefined', () => {
      const c = { symbol: 'NIFTY', quantity: 3, average_price: 97, kind: 'fut' };
      // pnl: (100 - 97) * 3 = 9
      expect(expiryPnl(c, spot)).toBe(9);
    });
  });

  // ============================================================================
  // Numeric coercion tests
  // ============================================================================

  describe('numeric coercion', () => {
    it('coerces string qty to number', () => {
      const c = { symbol: 'NIFTY', qty: '2', avg_cost: '95', kind: 'fut' };
      // pnl: (100 - 95) * 2 = 10
      expect(expiryPnl(c, spot)).toBe(10);
    });

    it('coerces string quantity to number (fallback)', () => {
      const c = { symbol: 'NIFTY', quantity: '2', average_price: '95', kind: 'fut' };
      // pnl: (100 - 95) * 2 = 10
      expect(expiryPnl(c, spot)).toBe(10);
    });

    it('coerces string spot to number', () => {
      const c = { symbol: 'NIFTY', qty: 1, avg_cost: 95, kind: 'fut' };
      // pnl: (100 - 95) * 1 = 5
      expect(expiryPnl(c, Number('100'))).toBe(5);
    });
  });

  // ============================================================================
  // Weekly-symbol strike parsing (decomposeSymbol-based — regression guard
  // against the broken inline regex that greedily captured the whole
  // numeric run as strike for symbols like NIFTY2592324000CE)
  // ============================================================================

  describe('weekly-symbol strike parsing (via decomposeSymbol)', () => {
    it('parses NIFTY weekly single-digit-month symbol (Sep, day 23, strike 24000)', () => {
      const c = { symbol: 'NIFTY2592324000CE', qty: 1, avg_cost: 100, kind: 'opt' };
      // strike=24000, spot=24500 → intrinsic = max(0, 24500-24000) = 500
      // pnl: (500 - 100) * 1 = 400
      expect(expiryPnl(c, 24500)).toBe(400);
    });

    it('parses October weekly symbol (monCode "O")', () => {
      const c = { symbol: 'NIFTY25O0724500PE', qty: 2, avg_cost: 50, kind: 'opt' };
      // strike=24500, spot=24000 → intrinsic (PE) = max(0, 24500-24000) = 500
      // pnl: (500 - 50) * 2 = 900
      expect(expiryPnl(c, 24000)).toBe(900);
    });

    it('parses monthly NIFTY symbol (unaffected by the weekly-parse fix)', () => {
      const c = { symbol: 'NIFTY25SEP24000CE', qty: 1, avg_cost: 100, kind: 'opt' };
      expect(expiryPnl(c, 24500)).toBe(400);
    });

    it('parses MCX monthly commodity option (CRUDEOIL)', () => {
      const c = { symbol: 'CRUDEOIL25OCT5800CE', qty: 1, avg_cost: 50, kind: 'opt' };
      // strike=5800, spot=5900 → intrinsic = 100; pnl = (100-50)*1 = 50
      expect(expiryPnl(c, 5900)).toBe(50);
    });

    it('parses BFO weekly symbol (SENSEX)', () => {
      const c = { symbol: 'SENSEX2591281000CE', qty: 1, avg_cost: 200, kind: 'opt' };
      // strike=81000, spot=81500 → intrinsic = 500; pnl = (500-200)*1 = 300
      expect(expiryPnl(c, 81500)).toBe(300);
    });

    // Regression guard (2026-09): the bare-suffix regex fallback must only
    // fire for symbols that carry no Kite year/month encoding at all.
    // Monthly-shaped symbols on a digit/punctuation-bearing root (M&M,
    // BAJAJ-AUTO) parse unambiguously via the monthly-tail capture, since
    // the 3-letter month always separates year from strike. Weekly-shaped
    // symbols on a digit-bearing root are genuinely ambiguous (no letter
    // separator) — those must return null rather than have the day-code
    // and strike silently merged into one garbage number by the old
    // unconditional fallback.
    describe('bare-suffix fallback gating', () => {
      it('still parses plain strike-only synthetic symbols (no year/month encoding) via the bare fallback', () => {
        // "23100" is a realistic 5-digit NIFTY strike with NO year+month
        // prefix — must NOT be mistaken for Kite-shaped and must still
        // resolve via the bare-suffix fallback (regression: an earlier,
        // looser gate falsely flagged this as Kite-shaped and returned
        // null instead of parsing strike=23100).
        const c = { symbol: 'NIFTY23100CE', qty: 25, avg_cost: 50, kind: 'opt' };
        // intrinsic = max(0, 23200-23100) = 100; pnl = (100-50)*25 = 1250
        expect(expiryPnl(c, 23200)).toBe(1250);
      });

      it('parses a monthly symbol on a punctuation-bearing root (M&M) via the monthly-tail capture, even though decomposeSymbol rejects it', () => {
        // decomposeSymbol's root pattern is pure-letters-only ([A-Z]+?) and
        // cannot match a "&" — the whole regex fails to match. But the
        // 3-letter month code ("APR") unambiguously separates the year from
        // the strike regardless of what precedes it, so this is safe to
        // parse directly from the monthly-tail capture group — M&M is a
        // real, heavily-traded F&O stock and must not go blank.
        const c = { symbol: 'M&M25APR2800CE', qty: 1, avg_cost: 5, kind: 'opt' };
        // strike=2800, spot=2850 → intrinsic = 50; pnl = (50-5)*1 = 45
        expect(expiryPnl(c, 2850)).toBe(45);
      });

      it('parses a monthly symbol on a hyphenated root (BAJAJ-AUTO) via the monthly-tail capture, even though decomposeSymbol rejects it', () => {
        const c = { symbol: 'BAJAJ-AUTO25APR8000CE', qty: 1, avg_cost: 5, kind: 'opt' };
        // strike=8000, spot=8100 → intrinsic = 100; pnl = (100-5)*1 = 95
        expect(expiryPnl(c, 8100)).toBe(95);
      });

      it('returns null for a weekly-shaped symbol on a digit-bearing root (NIFTYNXT50) — decomposeSymbol mis-splits it, not just rejects it', () => {
        // NIFTYNXT50 is a real root containing digits. decomposeSymbol's
        // weekly regex has no way to know where the root ends, so it does
        // NOT fail outright — it "succeeds" with a garbage split: root=
        // "NIFTYNXT", yy="50", monthCode="2", dd="56" (day 56 is
        // impossible), strike="24400" — instead of the true root=
        // "NIFTYNXT50", yy="25", monthCode="6", dd="24", strike="400".
        // The invalid day must be caught and the symbol treated as
        // unparseable (weekly has no letter separator, so — unlike the
        // monthly case above — there is no safe way to recover the real
        // strike from the tail alone).
        const c = { symbol: 'NIFTYNXT5025624400CE', qty: 1, avg_cost: 5, kind: 'opt' };
        expect(expiryPnl(c, 100)).toBe(null);
      });
    });
  });
});

// ============================================================================
// expiryPnlWithRealised — unified partial-close / realised-inclusive helper
// ============================================================================

describe('expiryPnlWithRealised', () => {
  it('AVG_PRICE_IS_COST_BASIS defaults to true (flagged-decision default)', () => {
    expect(AVG_PRICE_IS_COST_BASIS).toBe(true);
  });

  it('open F&O position: adds realised to the intrinsic-value expiry P&L', () => {
    const c = { symbol: 'NIFTY100CE', qty: 1, avg_cost: 5, kind: 'opt', realised: 20 };
    // ev = (0 - 5) * 1 = -5; + realised(20) = 15
    expect(expiryPnlWithRealised(c, 100)).toBe(15);
  });

  it('fully closed today (qty=0): returns realised directly, no spot needed', () => {
    const c = { symbol: 'NIFTY100CE', qty: 0, kind: 'opt', realised: 750 };
    expect(expiryPnlWithRealised(c, null)).toBe(750);
  });

  it('fully closed today with realised=0: returns 0, not null', () => {
    const c = { symbol: 'NIFTY100CE', qty: 0, kind: 'opt', realised: 0 };
    expect(expiryPnlWithRealised(c, null)).toBe(0);
  });

  it('fully closed today with no realised field at all: returns null (unusable row)', () => {
    const c = { symbol: 'NIFTY100CE', qty: 0, kind: 'opt' };
    expect(expiryPnlWithRealised(c, null)).toBe(null);
  });

  it('partial close (Groww-style overnight_quantity=0, remaining qty + realised carried): unified with ev', () => {
    // Groww hardcodes overnight_quantity=0 — this exercises the same shared
    // function the Legs page's splitClosedReopened-derived rows now use.
    const c = { symbol: 'NIFTY', qty: 5, avg_cost: 95, kind: 'fut', realised: 100, overnight_quantity: 0 };
    // ev = (100-95)*5 = 25; + realised(100) = 125
    expect(expiryPnlWithRealised(c, 100)).toBe(125);
  });

  it('MCX futures: falls back to expiryPnl (S − cost) × qty, unaffected by option strike parsing', () => {
    const c = { symbol: 'CRUDEOIL25OCTFUT', qty: 2, avg_cost: 5800, kind: 'fut', realised: 0 };
    expect(expiryPnlWithRealised(c, 5900)).toBe(200);
  });

  it('no spot / unparseable option and qty != 0: returns null', () => {
    const c = { symbol: 'BADOPTION', qty: 1, avg_cost: 5, kind: 'opt', realised: 10 };
    expect(expiryPnlWithRealised(c, 100)).toBe(null);
  });
});
