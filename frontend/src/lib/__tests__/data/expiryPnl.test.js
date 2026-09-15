import { describe, it, expect } from 'vitest';
import { expiryPnl } from '../../data/expiryPnl.js';

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
});
