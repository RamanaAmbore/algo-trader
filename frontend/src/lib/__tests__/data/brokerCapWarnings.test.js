/**
 * brokerCapWarnings.test.js — Vitest tests for capWarningFor /
 * aggregateCapWarnings, the single source of truth OrderTicket.svelte
 * (single-account Ticket tab) and SymbolPanel.svelte (multi-account
 * basket aggregation) both read for broker-capability-gap warning text.
 *
 * This file's primary addition (2026-10-23, audit item T17) is the Kite
 * MARKET-leg GTT rejection check: backend/brokers/adapters/kite.py's
 * place_gtt raises BrokerCapabilityError when ANY leg of a GTT carries
 * order_type=MARKET, checked across the WHOLE `orders` list for one call
 * — a TP+SL bracket is one two-leg OCO call, so a MARKET TP silently
 * drops the SL too. Pre-fix, neither the saved-template case nor the
 * live-override case (TemplateBar's inline LIMIT/MKT toggle) produced
 * any warning.
 *
 * Five quality dimensions:
 *  1. SSOT  — one warning-text function feeds both OrderTicket and
 *             SymbolPanel; this suite exercises both call shapes
 *             (capWarningFor directly, aggregateCapWarnings's fan-out).
 *  2. Perf  — pure function, no I/O; no perf assertion needed.
 *  3. Stale — guards the exact `broker_id` casing/alias match
 *             ('zerodha_kite' vs legacy 'kite') and the override-vs-
 *             template precedence the backend's own `_ta_pick_float_override`
 *             / tp_order_type resolution uses, so the two can't drift.
 *  4. Reuse — confirms the pre-existing OCO/trail/scale-out/MCX/poll-lag
 *             checks are unaffected by the new 4th `ov` parameter
 *             (default-safe — omitting it entirely must not throw or
 *             change existing behaviour).
 *  5. UX    — asserts the exact operator-facing message text and that
 *             the SL-lost wording only appears when an SL is actually
 *             configured (TP-only MARKET gets a different, accurate
 *             message).
 */

import { describe, it, expect } from 'vitest';
import { capWarningFor, aggregateCapWarnings } from '$lib/data/brokerCapWarnings.js';

const KITE_CAPS = {
  broker_id: 'zerodha_kite',
  display_name: 'Zerodha Kite',
  gtt_single: true,
  gtt_oco: true,
  gtt_modify: true,
  gtt_cap_per_account: 100,
  gtt_validity_days: 365,
  gtt_supports_mcx: true,
  bracket_order: false,
  cover_order: true,
  atomic_basket: false,
  order_tag: true,
  margin_preview: true,
  postback_gtt: 'reliable',
  rate_limit_orders_sec: 10,
};

const DHAN_CAPS = {
  broker_id: 'dhan',
  display_name: 'Dhan',
  gtt_single: true,
  gtt_oco: true,
  gtt_modify: true,
  gtt_cap_per_account: 50,
  gtt_validity_days: 365,
  gtt_supports_mcx: false,
  bracket_order: true,
  cover_order: true,
  atomic_basket: true,
  order_tag: true,
  margin_preview: true,
  postback_gtt: 'poll_only',
  rate_limit_orders_sec: 20,
};

const GROWW_CAPS = {
  broker_id: 'groww',
  display_name: 'Groww',
  gtt_single: true,
  gtt_oco: false,
  gtt_modify: true,
  gtt_cap_per_account: 25,
  gtt_validity_days: 90,
  gtt_supports_mcx: false,
  bracket_order: false,
  cover_order: false,
  atomic_basket: false,
  order_tag: false,
  margin_preview: false,
  postback_gtt: 'poll_only',
  rate_limit_orders_sec: 5,
};

describe('capWarningFor — pre-existing checks unaffected by the new ov param', () => {
  it('returns empty string with no template or no caps', () => {
    expect(capWarningFor(null, KITE_CAPS, 'NFO')).toBe('');
    expect(capWarningFor({ tp_pct: 5, sl_pct: 3 }, null, 'NFO')).toBe('');
  });

  it('Groww OCO emulated warning fires for TP+SL on Groww, omitting ov entirely', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'LIMIT' };
    const w = capWarningFor(t, GROWW_CAPS, 'NFO');
    expect(w).toContain('Groww OCO emulated');
  });

  it('Dhan MCX template-wont-attach warning fires on MCX', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'LIMIT' };
    const w = capWarningFor(t, DHAN_CAPS, 'MCX');
    expect(w).toContain("Dhan GTT can't run on MCX");
  });

  it('Kite + LIMIT TP+SL template has no warnings at all', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'LIMIT' };
    expect(capWarningFor(t, KITE_CAPS, 'NFO')).toBe('');
  });
});

describe('capWarningFor — Kite MARKET-leg GTT rejection (T17)', () => {
  it('saved template with tp_order_type=MARKET + sl_pct → SL-lost warning', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO');
    expect(w).toContain('Zerodha Kite GTT rejects MARKET legs');
    expect(w).toContain('SL will also fail to attach');
  });

  it('saved template with tp_order_type=MARKET and NO sl_pct → TP-only wording, no SL claim', () => {
    const t = { tp_pct: 5, sl_pct: null, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO');
    expect(w).toContain("TP won't attach");
    expect(w).not.toContain('SL will also fail to attach');
  });

  it('live override flips LIMIT-saved template to MARKET — still warns (the actual T17 gap)', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'LIMIT' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO', { tpOrderType: 'MARKET' });
    expect(w).toContain('Zerodha Kite GTT rejects MARKET legs');
  });

  it('live override flips MARKET-saved template back to LIMIT — warning clears', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO', { tpOrderType: 'LIMIT' });
    expect(w).not.toContain('rejects MARKET legs');
  });

  it('empty-string override is "not overridden" (matches backend _ta_pick_float_override convention) — falls back to template', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO', { tpOrderType: '' });
    expect(w).toContain('rejects MARKET legs');
  });

  it('legacy broker_id alias "kite" is also recognised', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, { ...KITE_CAPS, broker_id: 'kite' }, 'NFO');
    expect(w).toContain('rejects MARKET legs');
  });

  it('Dhan/Groww with MARKET TP never fire the Kite-specific warning', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'MARKET' };
    expect(capWarningFor(t, DHAN_CAPS, 'NFO')).not.toContain('rejects MARKET legs');
    expect(capWarningFor(t, GROWW_CAPS, 'NFO')).not.toContain('rejects MARKET legs');
  });

  it('no TP target at all (no tp_pct, no tp_scales_json) never warns even if tp_order_type=MARKET', () => {
    const t = { tp_pct: null, sl_pct: 3, tp_order_type: 'MARKET' };
    expect(capWarningFor(t, KITE_CAPS, 'NFO')).not.toContain('rejects MARKET legs');
  });

  it('scale-out ladder (tp_scales_json) counts as a TP target for the MARKET check', () => {
    const t = { tp_pct: null, sl_pct: 3, tp_order_type: 'MARKET', tp_scales_json: '[{"at_pct":30,"close_pct":50}]' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO');
    expect(w).toContain('rejects MARKET legs');
  });

  it('override tpPct supplies the TP target when the template itself has none', () => {
    const t = { tp_pct: null, sl_pct: 3, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO', { tpPct: 4 });
    expect(w).toContain('rejects MARKET legs');
  });

  it('override slPct can introduce the "SL also fails" wording over a template with no sl_pct', () => {
    const t = { tp_pct: 5, sl_pct: null, tp_order_type: 'MARKET' };
    const w = capWarningFor(t, KITE_CAPS, 'NFO', { slPct: 2 });
    expect(w).toContain('SL will also fail to attach');
  });
});

describe('aggregateCapWarnings — ov threads through every tuple in the basket', () => {
  it('Kite leg in a mixed-broker basket surfaces the MARKET-leg warning, tagged by account', () => {
    const t = { tp_pct: 5, sl_pct: 3, tp_order_type: 'LIMIT' };
    const tuples = [
      { account: 'ZG0790', caps: KITE_CAPS, exchange: 'NFO' },
      { account: 'DH1234', caps: DHAN_CAPS, exchange: 'NFO' },
    ];
    const w = aggregateCapWarnings(t, tuples, { tpOrderType: 'MARKET' });
    expect(w).toContain('rejects MARKET legs');
    expect(w).toContain('ZG0790');
    // Dhan is unaffected by the Kite-specific check.
    expect(w).not.toMatch(/DH1234.*rejects MARKET legs/);
  });

  it('empty accountCaps / no ov → empty string, no throw', () => {
    expect(aggregateCapWarnings({ tp_pct: 5, sl_pct: 3 }, [])).toBe('');
  });
});
