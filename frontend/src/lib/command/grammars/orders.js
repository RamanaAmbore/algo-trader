// Order-entry grammar: buy / sell / cancel / modify.
//
// This file loads the declarative YAML grammar from backend/config/grammars/orders.yaml
// and wires JavaScript-side suggesters and resolvers.
//
// Exports:
//   orderGrammar              — grammar object for CommandBar
//   buildOrderPayload(parsed) — converts parsed command to order REST payload
//   resolveInstrument(args)   — resolves instType+symbol+strike+expiry → Kite instrument
//   chaseConfig(level)        — LOW/MED/HIGH → { band_ticks, retry_seconds }
//   setQuoteLoadedCallback(fn)— register callback for async quote completion

import yamlText from './orders.yaml?raw';
import yaml from 'js-yaml';

import {
  getInstrument, listOptions, listFutures, nearestExpiry, listStrikes,
  findOption, findNearestFuture, findEquity, listUnderlyingsByType,
  listExpiries,
} from '$lib/data/instruments';
import { suggestAccounts } from '$lib/data/accounts';

const GRAMMAR_DOC = /** @type {any} */ (yaml.load(yamlText));
const INST_TYPE_MAP = { CALL: 'CE', PUT: 'PE', FUT: 'FUT', EQ: 'EQ' };
const INST_TYPE_REVERSE = { CE: 'CALL', PE: 'PUT', FUT: 'FUT', EQ: 'EQ' };

// ---------------------------------------------------------------------------
// Quote cache (for price suggestions)
// ---------------------------------------------------------------------------

const _ltpCache = new Map();
const _pendingLtp = new Set();
let _onQuoteLoaded = null;
export function setQuoteLoadedCallback(fn) { _onQuoteLoaded = fn; }

/** Get cached equity LTP for a symbol. Kicks off fetch if not cached. */
export function getLtp(symbol) {
  try {
    const eq = findEquity(String(symbol).toUpperCase());
    if (!eq) return null;
    const key = `${eq.e}:${eq.s}`;
    const entry = _ltpCache.get(key);
    if (!entry) { _fetchLtp(eq.e, eq.s); return null; }
    return entry.ltp;
  } catch { return null; }
}

async function _fetchLtp(exchange, tradingsymbol) {
  const key = `${exchange}:${tradingsymbol}`;
  const cached = _ltpCache.get(key);
  if (cached && Date.now() - cached.at < 30000) return cached;
  if (_pendingLtp.has(key)) return null;
  _pendingLtp.add(key);
  try {
    // TODO(FIX-26): migrate to fetchQuote from api.js once it can be
    // imported without risking SSR issues. This file is used outside the
    // Svelte component lifecycle; dynamic import of stores is required, and
    // a static api.js import would pull authStore at module level.
    const { authStore } = await import('$lib/stores');
    const token = authStore.getToken();
    const res = await fetch(`/api/quote/?exchange=${encodeURIComponent(exchange)}&tradingsymbol=${encodeURIComponent(tradingsymbol)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) return null;
    const data = await res.json();
    const entry = {
      ltp: data.ltp, bid: data.bid, ask: data.ask,
      depth_buy: data.depth_buy || [], depth_sell: data.depth_sell || [],
      volume: data.volume || 0, at: Date.now(),
    };
    _ltpCache.set(key, entry);
    if (_onQuoteLoaded) _onQuoteLoaded();
    return entry;
  } catch { return null; }
  finally { _pendingLtp.delete(key); }
}

// ---------------------------------------------------------------------------
// Suggesters
// ---------------------------------------------------------------------------

function symbolSuggest(prefix, ctx) {
  const instType = (ctx && ctx.instType ? String(ctx.instType) : 'EQ').toUpperCase();
  const mapped = INST_TYPE_MAP[instType] || 'EQ';
  return listUnderlyingsByType(mapped, prefix, 20);
}

function strikeSuggest(prefix, ctx) {
  const instType = (ctx && ctx.instType ? String(ctx.instType) : '').toUpperCase();
  const mapped = INST_TYPE_MAP[instType];
  if (mapped !== 'CE' && mapped !== 'PE') return [];
  const underlying = ctx && ctx.symbol;
  if (!underlying) return [];
  const expiry = nearestExpiry(underlying, mapped);
  if (!expiry) return [];
  const allStrikes = listStrikes(underlying, mapped, expiry);
  if (allStrikes.length === 0) return [];

  // Find ATM from equity LTP (or cached option LTP)
  const eq = findEquity(underlying);
  let atmPrice = 0;
  if (eq) {
    const eqKey = `${eq.e}:${eq.s}`;
    const eqEntry = _ltpCache.get(eqKey);
    if (eqEntry) {
      atmPrice = eqEntry.ltp;
    } else {
      _fetchLtp(eq.e, eq.s); // kick off background fetch
    }
  }

  // Find nearest strike to ATM, or fall back to middle of list
  let atmIdx = Math.floor(allStrikes.length / 2);
  if (atmPrice > 0) {
    let bestDist = Infinity;
    for (let i = 0; i < allStrikes.length; i++) {
      const dist = Math.abs(allStrikes[i] - atmPrice);
      if (dist < bestDist) { bestDist = dist; atmIdx = i; }
    }
  }
  // Narrow to ±15 strikes around ATM
  const lo = Math.max(0, atmIdx - 15);
  const hi = Math.min(allStrikes.length, atmIdx + 16);
  const strikes = allStrikes.slice(lo, hi);
  atmIdx = atmIdx - lo; // adjust for sliced array

  // Build labels with quote info
  const labels = strikes.map(k => {
    const opt = findOption(underlying, mapped, k, expiry);
    if (!opt) return String(k);
    const cacheKey = `${opt.e}:${opt.s}`;
    const entry = _ltpCache.get(cacheKey);
    if (!entry) {
      _fetchLtp(opt.e, opt.s); // background fetch
      return String(k);
    }
    const hasBoth = entry.bid && entry.ask && entry.bid > 0 && entry.ask > 0;
    if (!hasBoth) return `${k} (no quotes)`;
    const spread = entry.ask - entry.bid;
    const tick = opt.ts || 0.05;
    const spreadTicks = Math.round(spread / tick);
    const star = spreadTicks > 20 ? ' ***' : spreadTicks > 10 ? ' **' : spreadTicks > 5 ? ' *' : '';
    return `${k} (${entry.ltp.toFixed(2)})${star}`;
  });

  if (!prefix) {
    // Focus on ATM strike
    Object.defineProperty(labels, '_focusIndex', { value: atmIdx, enumerable: false });
    return labels;
  }
  return labels.filter(s => s.startsWith(prefix));
}

function expirySuggest(prefix, ctx) {
  if (!ctx || !ctx.symbol) return [];
  const underlying = String(ctx.symbol).toUpperCase();
  const instType = (ctx.instType || 'EQ').toUpperCase();
  const mapped = INST_TYPE_MAP[instType];
  if (mapped === 'EQ') return [];
  if (mapped === 'FUT') {
    return listFutures(underlying).map(r => r.x).filter(Boolean).slice(0, 20);
  }
  return listExpiries(underlying, mapped).slice(0, 20);
}

function _liquidityTag(totalDepth, qty) {
  if (!totalDepth || totalDepth <= 0) return '';
  if (qty <= totalDepth * 0.1) return ' ✓';
  if (qty <= totalDepth * 0.5) return ' ~';
  return ' ✗';
}

function qtySuggest(prefix, ctx) {
  try {
    let ls = 1;
    let isFO = false;
    let inst = null;
    try {
      inst = resolveInstrument({
        instType: ctx.instType || 'EQ',
        symbol: ctx.symbol,
        strike: ctx.strike,
        expiry: ctx.expiry,
      });
      ls = inst.ls || 1;
      isFO = inst.t === 'CE' || inst.t === 'PE' || inst.t === 'FUT';
    } catch {
      ls = ctx._lotSize || 1;
      isFO = ls > 1 || ['CALL','PUT','FUT'].includes((ctx.instType || '').toUpperCase());
    }

    // Get total depth for liquidity indicator
    const cacheKey = inst ? `${inst.e}:${inst.s}` : '';
    const entry = cacheKey ? _ltpCache.get(cacheKey) : null;
    if (inst && !entry) _fetchLtp(inst.e, inst.s);
    const totalBid = (entry?.depth_buy || []).reduce((s, d) => s + d.quantity, 0);
    const totalAsk = (entry?.depth_sell || []).reduce((s, d) => s + d.quantity, 0);
    const totalDepth = totalBid + totalAsk;

    if (isFO && ls > 1) {
      const maxLots = ctx.maxLots || 0;
      let lotOptions = prefix ? [Number(prefix) || 1] : [1, 2, 3, 5, 10];
      if (maxLots > 0) lotOptions = lotOptions.filter(n => n <= maxLots);
      if (maxLots > 0 && !lotOptions.includes(maxLots)) lotOptions.push(maxLots);
      return lotOptions.map(n => {
        const total = n * ls;
        const liq = _liquidityTag(totalDepth, total);
        return `${n} (×${ls}=${total})${liq}`;
      });
    }
    // Equity: plain share counts with liquidity
    const maxQty = ctx.maxLots || 0;
    let counts = prefix ? [Number(prefix) || 1] : [1, 5, 10, 25, 50, 100, 500];
    if (maxQty > 0) counts = counts.filter(n => n <= maxQty);
    if (maxQty > 0 && !counts.includes(maxQty)) counts.push(maxQty);
    return counts.map(n => {
      const liq = _liquidityTag(totalDepth, n);
      return liq ? `${n} (${liq.trim()})` : String(n);
    });
  } catch {
    if (prefix) return [];
    return ['1', '5', '10', '25', '50', '100', '500'];
  }
}

function _depthMap(depthArr) {
  const m = new Map();
  for (const d of depthArr) m.set(+d.price.toFixed(2), d);
  return m;
}

function priceSuggest(prefix, ctx) {
  try {
    const inst = resolveInstrument({
      instType: ctx.instType || 'EQ',
      symbol: ctx.symbol,
      strike: ctx.strike,
      expiry: ctx.expiry,
    });
    const entry = _ltpCache.get(`${inst.e}:${inst.s}`);
    if (!entry) {
      _fetchLtp(inst.e, inst.s);
      return [];
    }
    const ltp = entry.ltp;
    const tick = inst.ts || 0.05;
    const orderType = (ctx.orderType || 'LIMIT').toUpperCase();
    const bidMap = _depthMap(entry.depth_buy || []);
    const askMap = _depthMap(entry.depth_sell || []);

    if (orderType === 'SL' || orderType === 'SL-M') {
      // Show % trigger prices with depth info
      const isBuy = ctx._verb === 'buy';
      const pcts = isBuy ? [5, 10, 20] : [-5, -10, -20];
      return pcts.map(pct => {
        const price = +(ltp * (1 + pct / 100)).toFixed(2);
        const rounded = +(Math.round(price / tick) * tick).toFixed(2);
        return `${rounded} (${pct > 0 ? '+' : ''}${pct}% of ${ltp.toFixed(2)})`;
      });
    }

    // LIMIT: show ATM ± 10 ticks with depth annotations
    const atm = Math.round(ltp / tick) * tick;
    const steps = [];
    for (let i = -10; i <= 10; i++) {
      const p = +(atm + i * tick).toFixed(2);
      if (p <= 0) continue;
      const bid = bidMap.get(p);
      const ask = askMap.get(p);
      let label = String(p);
      if (bid) label += ` (bid ${bid.quantity})`;
      else if (ask) label += ` (ask ${ask.quantity})`;
      if (+p.toFixed(2) === +ltp.toFixed(2)) label += ' ◀ LTP';
      steps.push(label);
    }
    const atmStr = String(+atm.toFixed(2));
    const focus = steps.findIndex(s => s.startsWith(atmStr));
    if (focus >= 0) Object.defineProperty(steps, '_focusIndex', { value: focus, enumerable: false });
    if (!prefix) return steps;
    return steps.filter(s => s.startsWith(prefix));
  } catch { return []; }
}

function orderIdSuggest(prefix, ctx) {
  const orders = ctx.openOrders || [];
  const labels = orders.map(o => {
    const price = o.price ? `@${o.price}` : '';
    return `${o.order_id} (${o.transaction_type} ${o.quantity} ${o.tradingsymbol} ${price} ${o.account})`;
  });
  if (!prefix) return labels.slice(0, 20);
  return labels.filter(s => s.startsWith(prefix)).slice(0, 20);
}

const SUGGESTERS = {
  symbol:       symbolSuggest,
  strike:       strikeSuggest,
  account:      (p, _) => suggestAccounts(p, 20),
  qty:          qtySuggest,
  price:        priceSuggest,
  order_id:     orderIdSuggest,
  expiry:       expirySuggest,
  chase_level:  null,
  order_type:   null,
};

// ---------------------------------------------------------------------------
// Grammar wiring (YAML → engine format)
// ---------------------------------------------------------------------------

function _wireRequired(required) {
  if (typeof required !== 'string') return !!required;
  const m = required.match(/^if:([a-zA-Z_]+)(==|!=)(.+)$/);
  if (!m) return false;
  const [, name, op, valuesRaw] = m;
  const wanted = valuesRaw.split('|').map(v => v.toUpperCase());
  return (ctx) => {
    const actual = (ctx[name] || '').toString().toUpperCase();
    const inList = wanted.includes(actual);
    return op === '==' ? inList : !inList;
  };
}

const _PARSE_MAP = {
  int: Number,
  float: Number,
  upper: (v) => String(v).toUpperCase(),
  str: String,
};

function _wireTokens(tokenSpecs) {
  return (tokenSpecs || []).map(spec => ({
    role: spec.role,
    values: spec.values || undefined,
    suggest: spec.kind ? SUGGESTERS[spec.kind] || undefined : undefined,
    required: _wireRequired(spec.required),
    parse: _PARSE_MAP[spec.parse] || undefined,
    resolve: undefined,
    hint: spec.hint || undefined,
  }));
}

function _wireKwargs(kwargSpecs) {
  if (!kwargSpecs) return undefined;
  const out = {};
  for (const [key, spec] of Object.entries(kwargSpecs)) {
    out[key] = {
      values: spec.values || undefined,
      suggest: spec.kind ? SUGGESTERS[spec.kind] || undefined : undefined,
      parse: _PARSE_MAP[spec.parse] || undefined,
      hint: spec.hint || undefined,
    };
  }
  return out;
}

const _wiredVerbs = {};
for (const [name, def] of Object.entries(GRAMMAR_DOC.verbs)) {
  _wiredVerbs[name] = {
    tokens: _wireTokens(def.tokens),
    kwargs: _wireKwargs(def.kwargs),
  };
}

export const orderGrammar = {
  verbs: _wiredVerbs,
  // Single-key shortcuts: typing this key auto-expands to kwarg=
  kwargShortcuts: { p: 'product', P: 'product' },
};

// ---------------------------------------------------------------------------
// Symbol resolution
// ---------------------------------------------------------------------------

export function resolveInstrument(args = /** @type {any} */ ({})) {
  const { instType, symbol, strike, expiry } = args;
  if (!symbol) throw new Error('symbol required');
  const underlying = String(symbol).toUpperCase();
  const type = String(instType || 'EQ').toUpperCase();
  const mapped = INST_TYPE_MAP[type];
  if (!mapped) throw new Error(`unknown instType: ${instType}`);

  if (mapped === 'EQ') {
    const eq = findEquity(underlying);
    if (!eq) throw new Error(`no equity instrument: ${underlying}`);
    return eq;
  }
  if (mapped === 'FUT') {
    if (expiry) {
      const alt = listFutures(underlying).find(r => r.x === expiry);
      if (alt) return alt;
    }
    const inst = findNearestFuture(underlying);
    if (!inst) throw new Error(`no futures for ${underlying}`);
    return inst;
  }
  if (strike == null) throw new Error(`strike required for ${type}`);
  const exp = expiry || nearestExpiry(underlying, mapped);
  if (!exp) throw new Error(`no ${type} contracts for ${underlying}`);
  const inst = findOption(underlying, mapped, Number(strike), exp);
  if (!inst) throw new Error(`no ${underlying} ${strike} ${type} on ${exp}`);
  return inst;
}

export function resolveSymbol(raw, ctx = {}) {
  const direct = getInstrument(raw);
  if (direct) return direct;
  return resolveInstrument({
    instType: ctx.instType,
    symbol: raw,
    strike: ctx.strike,
    expiry: ctx.expiry,
  });
}

// ---------------------------------------------------------------------------
// Chase config
// ---------------------------------------------------------------------------

const CHASE_LEVELS = (GRAMMAR_DOC.chase_levels || {});
export function chaseConfig(level) {
  const key = String(level).toUpperCase();
  const cfg = CHASE_LEVELS[key];
  if (cfg) return { ...cfg, level: key };
  return { band_ticks: 4, retry_seconds: 30, level: 'MED' };
}

// ---------------------------------------------------------------------------
// Build order payload
// ---------------------------------------------------------------------------

export function buildOrderPayload(parsed) {
  const { verb, args, kwargs } = parsed;
  if (verb !== 'buy' && verb !== 'sell') return null;
  const inst = resolveInstrument({
    instType: args.instType || 'EQ',
    symbol: args.symbol,
    strike: args.strike,
    expiry: args.expiry,
  });
  const product = kwargs.product
    || (inst.t === 'EQ' ? 'CNC' : (inst.t === 'FUT' || inst.t === 'CE' || inst.t === 'PE') ? 'NRML' : 'MIS');
  // F&O: multiply lots by lot_size to get total quantity for Kite
  const isFO = inst.t === 'CE' || inst.t === 'PE' || inst.t === 'FUT';
  const ls = inst.ls || 1;
  const quantity = (isFO && ls > 1) ? args.qty * ls : args.qty;

  // SL/SL-M: user-entered price is the trigger price
  const ot = args.orderType.toUpperCase();
  let price = args.price || 0;
  let trigger_price = 0;
  if (ot === 'SL') {
    trigger_price = price;  // trigger at this level, then limit at same price
  } else if (ot === 'SL-M') {
    trigger_price = price;  // trigger at this level, then market
    price = 0;
  }

  return {
    account: args.account,
    tradingsymbol: inst.s,
    exchange: inst.e,
    quantity,
    transaction_type: verb.toUpperCase(),
    order_type: ot,
    price,
    trigger_price,
    product,
    variety: 'regular',
    validity: 'DAY',
    chase: args.chase ? chaseConfig(args.chase) : null,
  };
}

/**
 * After a command is fully typed, return a preview string showing the resolved
 * Kite tradingsymbol (e.g. "→ NIFTY25APR0322500CE"). Used by the CommandBar
 * to display a confirmation line.
 */
export function previewSymbol(parsed) {
  try {
    const { args } = parsed;
    const inst = resolveInstrument({
      instType: args.instType || 'EQ',
      symbol: args.symbol,
      strike: args.strike,
      expiry: args.expiry,
    });
    return `→ ${inst.s} (${inst.e})`;
  } catch { return ''; }
}

/**
 * Reverse-map a Kite tradingsymbol back to grammar tokens.
 * e.g. 'INFY' → { instType: 'EQ', symbol: 'INFY' }
 *      'NIFTY25APR22500CE' → { instType: 'CALL', symbol: 'NIFTY', strike: '22500', expiry: '2025-04-...' }
 */
export function parseKiteSymbol(tradingsymbol) {
  const inst = getInstrument(tradingsymbol);
  if (!inst) {
    // Fallback: assume equity
    return { instType: 'EQ', symbol: tradingsymbol };
  }
  const instType = INST_TYPE_REVERSE[inst.t] || 'EQ';
  const result = { instType, symbol: inst.u || tradingsymbol };
  if (inst.k) result.strike = String(inst.k);
  if (inst.x) result.expiry = inst.x;
  return result;
}

/**
 * Shared enrichPairs function for order CommandBars.
 * Adds symbol:LTP and expanded qty format for F&O.
 * Used by the Orders page CommandBar.
 */
// `executeBuySell` was retired during the Phase 2/4 unification.
// The Orders page now parses with `buildOrderPayload(parsed)` then
// opens the shared <OrderTicket> pre-filled (with action='open').
// Submit happens through the ticket → POST /api/orders/ticket. The
// old direct-broker path (POST /api/orders/place) is no longer
// reached from any frontend code path.

export function enrichOrderPairs(pairs, ctx) {
  return pairs.map(p => {
    if (p.role === 'symbol' && p.status === 'filled' && p.value) {
      const ltp = getLtp(p.value);
      if (ltp) return { ...p, value: `${p.value}:${ltp}` };
    }
    if (p.role === 'qty' && p.status === 'filled' && p.value) {
      try {
        let ls = ctx?._lotSize || 0;
        let isFO = false;
        if (!ls) {
          const inst = resolveInstrument({ instType: ctx.instType || 'EQ', symbol: ctx.symbol, strike: ctx.strike, expiry: ctx.expiry });
          ls = inst.ls || 1;
          isFO = inst.t === 'CE' || inst.t === 'PE' || inst.t === 'FUT';
        } else {
          isFO = true;
        }
        if (isFO && ls > 1) {
          const n = Number(p.value) || 0;
          return { ...p, value: `${n} (×${ls}=${n * ls})` };
        }
      } catch {}
    }
    return p;
  });
}
