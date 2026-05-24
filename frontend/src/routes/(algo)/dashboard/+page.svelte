<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import { clientTimestamp, visibleInterval } from '$lib/stores';
  import NewsList from '$lib/NewsList.svelte';
  import {
    fetchPositions, fetchHoldings, fetchRecentAgentEvents,
    fetchFunds, fetchBrokerAccounts, fetchIntradayEquity,
    batchQuote,
  } from '$lib/api';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';
  import {
    classifyByIndex,
    FO_QUOTE_KEYS, MIDCAP_QUOTE_KEYS, SMLCAP_QUOTE_KEYS,
    symbolFromQuoteKey,
  } from '$lib/data/indexConstituents';

  // IST-midnight-as-UTC for "today" date-window filters. Indian markets
  // (and operators) live in Asia/Kolkata; using the browser's local
  // midnight via setHours(0,0,0,0) gave wrong counts whenever the
  // browser TZ differed from IST (or even across IST midnight rollover
  // when the operator was outside India).
  function istMidnightTodayAsDate() {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    const y = parts.find(p => p.type === 'year').value;
    const m = parts.find(p => p.type === 'month').value;
    const d = parts.find(p => p.type === 'day').value;
    return new Date(`${y}-${m}-${d}T00:00:00+05:30`);
  }

  // ── Demo banner — sourced from the layout's shared context ─────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);
  let bannerDismissed = $state(false);

  // ── Hero row state ─────────────────────────────────────────────────
  let _todayPnl     = $state(/** @type {number|null} */ (null));
  let _startingNav  = $state(/** @type {number|null} */ (null));
  let _niftyDayPct  = $state(/** @type {number|null} */ (null));
  let _firesToday   = $state(0);
  let _paperOpen    = $state(0);
  let _conn         = $state({ loaded: 0, total: 0 });
  let _heroLoadedAt = $state(/** @type {string|null} */ (null));
  let _heroTeardown;

  // Agent log collapsed by default.
  let _agentLogOpen = $state(false);
  // Operator-facing log declutter: default to agent_fire ONLY so the
  // expanded log is a thin chronological list of "what fired".
  // Operator can flip the chip to ALSO include action successes /
  // errors when they want the deeper "what did the fire DO" trace.
  let _agentLogShowActions = $state(false);
  const _agentLogKinds = $derived(
    _agentLogShowActions
      ? ['agent_fire', 'agent_action_success', 'agent_action_error']
      : ['agent_fire'],
  );

  // PnlAnalysis collapse — persisted to localStorage.
  let _pnlOpen = $state(false);


  // ── Raw positions + holdings (reused for winners/losers and
  //     the new Equity-card tabs) ─────────────────────────────────
  /** @type {any[]} */
  let _positions = $state([]);
  /** @type {any[]} */
  let _holdings  = $state([]);
  // Full funds rows (for the Capital-card Funds table). Kept
  // separate from _margins (which is the cleaned-down gauge
  // input) so the table can show cash + collateral + net etc.
  /** @type {any[]} */
  let _funds     = $state([]);

  // Equity card stacks Positions Summary on top and Holdings
  // Summary below — no tabs. The tab variant left ~half the card
  // empty whenever one side rendered; stacked uses the card's
  // full vertical real estate without operator interaction.

  // Equity-card account filter — MultiSelect in the header lets the
  // operator scope both Positions Summary AND Holdings Summary to
  // any subset of broker accounts. Default = all accounts.
  // selectedAccounts is the SOURCE OF TRUTH; empty list means "all"
  // (no filter applied), so removing every checkbox doesn't blank
  // the tables. Persisted to sessionStorage so the filter survives
  // tab refresh but resets across sessions.
  let _selectedAccounts = $state(/** @type {string[]} */ ([]));

  // Derived list of distinct accounts seen in current positions +
  // holdings — feeds the MultiSelect options list. Sorted ascending.
  const _availableAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    return [...set].sort();
  });

  // Apply the account filter to a row list. Empty filter = pass-through.
  function _filterByAccount(rows) {
    if (!_selectedAccounts.length) return rows;
    const allow = new Set(_selectedAccounts);
    return rows.filter(r => allow.has(String(r.account || '')));
  }

  // Winners / Losers cards each tab through the 5 buckets
  // (underlying / midcap / smallcap / holdings / positions) instead
  // of stacking them. Default tab: 'underlying' — the broadest view
  // that aggregates F&O positions to their underlying name, which
  // is usually the operator's first question on the dashboard.
  // Default tab is HOLDINGS — that bucket consistently has the
  // deepest stream (operators with a stock-heavy book usually have
  // 20-100+ holdings vs 2-5 F&O underlyings). The earlier default
  // 'underlying' often surfaced only 2-3 entries, hiding the
  // existence of the top-10 cap until the operator clicked away.
  let _winTab = $state(/** @type {'underlying'|'midcap'|'smallcap'|'holdings'|'positions'} */ ('holdings'));
  let _losTab = $state(/** @type {'underlying'|'midcap'|'smallcap'|'holdings'|'positions'} */ ('holdings'));

  // Per-card fullscreen toggles. Each card binds its own slot —
  // multiple cards can theoretically open at once but only one is
  // visually on top (last-clicked wins via DOM order).
  let _fsEquityCurve = $state(false);
  let _fsCapital     = $state(false);
  let _fsEquity      = $state(false);
  let _fsWinners     = $state(false);
  let _fsLosers      = $state(false);
  let _fsNews        = $state(false);
  let _fsPnl         = $state(false);
  let _fsAgent       = $state(false);

  // Per-account summary derivations — same shape MarketPulse
  // builds internally, but computed here from the already-loaded
  // _positions / _holdings. No extra fetches; the HTML tables
  // below render directly from these derivations.
  //
  // Day-change for positions comes from row.pnl (intraday is the
  // whole position life). For holdings it's row.day_change (the
  // delta from yesterday's close). Both sum to the same TOTAL the
  // existing MarketPulse summary grids show.
  /** @typedef {{account: string, day_pnl: number, pnl: number, inv_val: number, cur_val: number}} SumRow */

  const _positionsSummary = $derived.by(() => {
    /** @type {Record<string, SumRow>} */
    const byAcct = {};
    for (const r of _filterByAccount(_positions)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.pnl) || 0;
      byAcct[a].pnl     += Number(r.pnl) || 0;
    }
    return Object.values(byAcct).sort((a, b) => a.account.localeCompare(b.account));
  });

  const _holdingsSummary = $derived.by(() => {
    /** @type {Record<string, SumRow>} */
    const byAcct = {};
    for (const r of _filterByAccount(_holdings)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change ?? r.day_change_pct_amount ?? 0);
      byAcct[a].pnl     += Number(r.pnl) || 0;
      byAcct[a].inv_val += Number(r.inv_val) || 0;
      byAcct[a].cur_val += Number(r.cur_val) || 0;
    }
    return Object.values(byAcct).sort((a, b) => a.account.localeCompare(b.account));
  });

  // Per-account TOTAL rows (sum across accounts) — pinned at the
  // bottom of each summary table so the operator's eye lands on
  // the firm-wide number without scrolling.
  const _positionsTotal = $derived.by(() => {
    const t = { account: 'TOTAL', day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    for (const r of _positionsSummary) {
      t.day_pnl += r.day_pnl; t.pnl += r.pnl;
    }
    return t;
  });

  const _holdingsTotal = $derived.by(() => {
    const t = { account: 'TOTAL', day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    for (const r of _holdingsSummary) {
      t.day_pnl += r.day_pnl; t.pnl += r.pnl;
      t.inv_val += r.inv_val; t.cur_val += r.cur_val;
    }
    return t;
  });

  // Counts shown in the tab strip — symbol count, not row count.
  // A book of 4 NIFTY contracts across 2 accounts shows as "4".
  const _positionsCount = $derived(_positions.length);
  const _holdingsCount  = $derived(_holdings.length);

  // Helpers for table cells.
  const _pnlColor = (v) =>
    v > 0 ? 'cap-up' : v < 0 ? 'cap-down' : 'cap-neutral';

  // ── SymbolPanel for winners/losers tile click ──────────────────────
  let _ticketProps = $state(/** @type {any} */ (null));

  // ── Row 1: Intraday equity curve ───────────────────────────────────
  /** @type {{ ts: string, day_pnl: number, cum_pnl: number }[]} */
  let _equityPoints = $state([]);

  // ── Row 1: Margin utilisation gauges ──────────────────────────────
  /**
   * @type {{ account: string, used: number, avail: number, util_pct: number }[]}
   */
  let _margins = $state([]);

  // ── Derived hero values ────────────────────────────────────────────
  const _todayPct = $derived(
    (_todayPnl != null && _startingNav != null && _startingNav !== 0)
      ? (_todayPnl / _startingNav) * 100
      : null
  );

  const _vsNifty = $derived(
    (_todayPct != null && _niftyDayPct != null)
      ? _todayPct - _niftyDayPct
      : null
  );

  const _pnlClass = $derived(
    _todayPnl == null ? 'hero-pnl-neutral'
    : _todayPnl > 0   ? 'hero-pnl-up'
    : _todayPnl < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _todayPctClass = $derived(
    _todayPct == null ? 'hero-pnl-neutral'
    : _todayPct > 0   ? 'hero-pnl-up'
    : _todayPct < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _vsNiftyClass = $derived(
    _vsNifty == null ? 'hero-pnl-neutral'
    : _vsNifty > 0   ? 'hero-pnl-up'
    : _vsNifty < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  // ── Open orders (from layout's algoStatus poll) ───────────────────
  const _openOrders = $derived(
    /** @type {any[]} */ (algoStatus.paperStatus?.open_order_details ?? [])
  );

  // ── Categorised top-3 movers per bucket ─────────────────────────────
  // (The earlier merged _combinedBook / _winners / _losers derivations
  // were retired when the dashboard moved to the 5-bucket tabbed
  // Winners/Losers cards — every bucket is now built directly from
  // the _positionsByUnderlying / _holdingsFor / _positionsRows
  // helpers, all of which aggregate by symbol.)
  // Five buckets, each filtered to its source/classification, sorted
  // by P&L. Rendered as side-by-side compact lists inside the Top
  // Winners / Top Losers cards so the operator sees the movement
  // picture across all instrument classes in one glance.
  //
  // Buckets:
  //   1. Option Underlying — positions aggregated by parsed underlying
  //      (NIFTY, BANKNIFTY, …). One row per underlying, sum of P&L.
  //   2. Midcap   — holdings classified as NIFTY MIDCAP 100.
  //   3. Smallcap — holdings classified as NIFTY SMLCAP 100.
  //   4. Holdings — top single-stock from holdings (all caps).
  //   5. Positions — top single-contract from positions (all symbols).

  // Parse the equity ticker prefix from a tradingsymbol — same logic
  // as instruments.js _derivedUnderlying (longest pure-letter prefix
  // before any digit). Works for NIFTY25APR22000CE, NIFTY2640722700CE,
  // CRUDEOIL25APRFUT, RELIANCE26APR1360CE, etc.
  function _parseUnderlyingPrefix(sym) {
    if (!sym) return '';
    const m = String(sym).match(/^([A-Z]+)/);
    return m ? m[1] : String(sym);
  }

  // Market-wide quotes for Top Winners/Losers — Underlying / Midcap /
  // Smallcap buckets show top movers across the FULL exchange universe
  // (NIFTY MIDCAP 100 / NIFTY SMLCAP 100 / curated F&O list),
  // independent of the operator's positions or holdings. Only the
  // Holdings + Positions tabs remain user-scoped.
  //
  // Single batchQuote payload covers all three universes (~300 symbols),
  // polled every 60 s. Backend caches under /quote/batch.
  /** @type {Record<string, any>} */
  let _marketQuotes = $state({});
  let _stopMarketPoll;

  // Build a market-wide row list from quotes for the given universe.
  // Each row carries `kind: 'market'`, with `pnl` holding the day
  // percentage (used both as the sort key and the display value).
  // `ltp` rides along so the row can show the live spot alongside
  // the move. User-scoped rows (Holdings / Positions buckets) use
  // `kind: 'user'` with pnl in rupees and inv_val for the % cell.
  function _marketRows(keys) {
    const out = [];
    for (const k of keys) {
      const q = _marketQuotes[k];
      if (!q) continue;
      const ltp = Number(q.last_price ?? q.ltp ?? 0);
      // Prefer Kite's own change_percent / change_pct when present;
      // fall back to (ltp - close) / close × 100.
      let pct = null;
      if (q.change_percent != null) pct = Number(q.change_percent);
      else if (q.change_pct    != null) pct = Number(q.change_pct);
      else {
        const close = Number(q.ohlc?.close ?? q.close ?? 0);
        if (close > 0 && ltp > 0) pct = ((ltp - close) / close) * 100;
      }
      if (pct == null || !isFinite(pct) || pct === 0) continue;
      out.push({
        symbol: symbolFromQuoteKey(k),
        pnl:    pct,           // sort key + display %
        ltp,                   // shown as @ ₹X
        kind:   'market',
      });
    }
    return out;
  }

  // Aggregate positions by parsed underlying. {underlying → sum(pnl)}
  // Filtered by the shared account multiselect (_filterByAccount —
  // bound to the same _selectedAccounts state the Equity card uses).
  // Used by the (now retired) user-scoped underlying bucket — keeping
  // the derivation alive for any future "your underlying exposure"
  // surface even though Top Winners/Losers Underlying now reads from
  // _marketRows() instead.
  const _positionsByUnderlying = $derived.by(() => {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number}>} */
    const byU = new Map();
    for (const p of _filterByAccount(_positions)) {
      const sym = String(p.tradingsymbol || p.symbol || '');
      const pnl = Number(p.pnl) || 0;
      if (!sym || pnl === 0) continue;
      const u = _parseUnderlyingPrefix(sym);
      if (!u) continue;
      const cur = byU.get(u) ?? { symbol: u, pnl: 0, inv_val: 0 };
      cur.pnl += pnl;
      byU.set(u, cur);
    }
    return Array.from(byU.values());
  });

  // Market-wide rows for the three universe-based buckets.
  // Re-derives whenever _marketQuotes flips. Each is one bag of
  // {symbol, pnl=day_pct, inv_val=ltp} ready for _eligible/_top.
  const _foUnderlyingRows = $derived(_marketRows(FO_QUOTE_KEYS));
  const _midcapRows       = $derived(_marketRows(MIDCAP_QUOTE_KEYS));
  const _smlcapRows       = $derived(_marketRows(SMLCAP_QUOTE_KEYS));

  // Aggregate by symbol — dedupes the same instrument when held in
  // multiple accounts. Without this, GMDCLTD in both ZG0790 + ZJ6294
  // shows up twice in the Winners/Losers list. The winners/losers
  // surface is about "which symbol moved" not "which (symbol, account)
  // pair moved" — so collapse by symbol and sum across accounts.
  function _aggregateBySymbol(rows) {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number}>} */
    const bySym = new Map();
    for (const r of rows) {
      const sym = r.symbol;
      if (!sym) continue;
      const cur = bySym.get(sym) ?? { symbol: sym, pnl: 0, inv_val: 0 };
      cur.pnl     += Number(r.pnl) || 0;
      cur.inv_val += Number(r.inv_val) || 0;
      bySym.set(sym, cur);
    }
    return Array.from(bySym.values());
  }

  // Holdings, with optional class filter (midcap / smallcap / null=all).
  // Aggregated by symbol so a stock held in N accounts appears as one
  // row with summed day P&L + cost basis. Filtered by the shared
  // _selectedAccounts state.
  function _holdingsFor(cls) {
    /** @type {{symbol: string, pnl: number, inv_val: number}[]} */
    const raw = [];
    for (const h of _filterByAccount(_holdings)) {
      const sym = String(h.tradingsymbol || h.symbol || '');
      const pnl = Number(h.day_change ?? h.day_change_pct_amount ?? 0);
      if (!sym) continue;
      if (cls) {
        const c = classifyByIndex(sym);
        if (c !== cls) continue;
      }
      raw.push({
        symbol: sym,
        pnl,
        inv_val: Number(h.inv_val ?? 0),
      });
    }
    // Aggregate first, then drop zero-pnl symbols (the dedupe could
    // resolve a +X / -X pair into 0 — still want to hide those).
    // Tag every survivor as user-scoped so the template renders the
    // ₹ form (vs market rows which render as %).
    return _aggregateBySymbol(raw)
      .filter(r => r.pnl !== 0)
      .map(r => ({ ...r, kind: 'user' }));
  }

  // Positions as individual contracts, aggregated by tradingsymbol
  // across accounts (same reason as holdings). Filtered by the
  // shared _selectedAccounts state.
  const _positionsRows = $derived.by(() => {
    /** @type {{symbol: string, pnl: number, inv_val: number}[]} */
    const raw = [];
    for (const p of _filterByAccount(_positions)) {
      const sym = String(p.tradingsymbol || p.symbol || '');
      const pnl = Number(p.pnl) || 0;
      if (!sym) continue;
      raw.push({ symbol: sym, pnl, inv_val: 0 });
    }
    return _aggregateBySymbol(raw)
      .filter(r => r.pnl !== 0)
      .map(r => ({ ...r, kind: 'user' }));
  });

  // Eligible-rows picker — sorted by P&L, NOT sliced. Splits the
  // bucket source into the winner / loser subset.
  function _eligible(rows, kind) {
    if (!Array.isArray(rows) || rows.length === 0) return [];
    if (kind === 'win') {
      return rows.filter(r => r.pnl > 0).sort((a, b) => b.pnl - a.pnl);
    }
    return rows.filter(r => r.pnl < 0).sort((a, b) => a.pnl - b.pnl);
  }

  // Cap rows rendered per tab. The scrollable .wl-rows container
  // handles overflow at default size; fullscreen lifts the cap.
  // count chip on each tab shows the FULL eligible count (not the
  // sliced cap), so the operator knows "Holdings · 50" means 50
  // winners exist with top 10 visible inside.
  const _TOP_N = 10;

  // Five-bucket bundle, one per category. `count` is the total
  // eligible count; `rows` is sliced to _TOP_N for rendering.
  function _bucket(label, source, kind) {
    const all = _eligible(source, kind);
    return { label, count: all.length, rows: all.slice(0, _TOP_N) };
  }

  // Bucket sources:
  //   - OPTION UNDERLYING / MIDCAP / SMALLCAP — MARKET-wide (universe
  //     constants in $lib/data/indexConstituents), independent of
  //     positions/holdings + the account multiselect.
  //   - HOLDINGS / POSITIONS — user-scoped, honour the account filter.
  const _winnerBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,       'win'),
    _bucket('MIDCAP',            _midcapRows,             'win'),
    _bucket('SMALLCAP',          _smlcapRows,             'win'),
    _bucket('HOLDINGS',          _holdingsFor(null),      'win'),
    _bucket('POSITIONS',         _positionsRows,          'win'),
  ]);

  const _loserBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,       'lose'),
    _bucket('MIDCAP',            _midcapRows,             'lose'),
    _bucket('SMALLCAP',          _smlcapRows,             'lose'),
    _bucket('HOLDINGS',          _holdingsFor(null),      'lose'),
    _bucket('POSITIONS',         _positionsRows,          'lose'),
  ]);

  // Show the categorised section only when SOMETHING is movable. Hides
  // cleanly outside trading hours when every bucket is empty.
  const _hasWinners = $derived(_winnerBuckets.some(b => b.rows.length > 0));
  const _hasLosers  = $derived(_loserBuckets.some(b => b.rows.length > 0));

  // Tab key ↔ bucket label helpers — keeps the template terse and the
  // state machine canonical.
  const _BUCKET_KEY = {
    'OPTION UNDERLYING': 'underlying',
    'MIDCAP':            'midcap',
    'SMALLCAP':          'smallcap',
    'HOLDINGS':          'holdings',
    'POSITIONS':         'positions',
  };
  const _BUCKET_LABEL = Object.fromEntries(
    Object.entries(_BUCKET_KEY).map(([l, k]) => [k, l])
  );
  function _bucketKey(label)   { return _BUCKET_KEY[label] ?? 'underlying'; }
  function _winTabLabel(key)   { return _BUCKET_LABEL[key] ?? 'OPTION UNDERLYING'; }
  // Short tab labels — "OPTION UNDERLYING" gets truncated for the
  // narrow tab strip; the others stay as their full token.
  const _TAB_SHORT = {
    'OPTION UNDERLYING': 'Underlying',
    'MIDCAP':            'Midcap',
    'SMALLCAP':          'Smallcap',
    'HOLDINGS':          'Holdings',
    'POSITIONS':         'Positions',
  };
  function _tabShort(label) { return _TAB_SHORT[label] ?? label; }

  const _connIcon = $derived(
    _conn.total === 0     ? '—'
    : _conn.loaded === 0  ? '✗'
    : _conn.loaded < _conn.total ? '⚠'
    : '✓'
  );

  const _connClass = $derived(
    _conn.total === 0     ? 'hero-chip-conn-neutral'
    : _conn.loaded === 0  ? 'hero-chip-conn-red'
    : _conn.loaded < _conn.total ? 'hero-chip-conn-amber'
    : 'hero-chip-conn-green'
  );

  // ── Equity chart SVG constants ─────────────────────────────────────
  const CHART_W = 600;
  const CHART_H = 220;
  const PAD_L = 8;
  const PAD_R = 52;
  const PAD_T = 12;
  const PAD_B = 28;
  const INNER_W = CHART_W - PAD_L - PAD_R;
  const INNER_H = CHART_H - PAD_T - PAD_B;

  // ── Equity chart derived state ─────────────────────────────────────
  const _equityDomain = $derived.by(() => {
    if (!_equityPoints.length) return null;
    const vals = _equityPoints.map(p => p.cum_pnl);
    let yMin = Math.min(...vals);
    let yMax = Math.max(...vals);
    // ensure zero is always visible; add 10% padding
    yMin = Math.min(yMin, 0);
    yMax = Math.max(yMax, 0);
    const pad = Math.max((yMax - yMin) * 0.10, 500);
    yMin -= pad; yMax += pad;
    const ts = _equityPoints.map(p => new Date(p.ts).getTime());
    return { yMin, yMax, tMin: Math.min(...ts), tMax: Math.max(...ts) };
  });

  function _eqX(ts) {
    const d = _equityDomain;
    if (!d || d.tMax === d.tMin) return PAD_L;
    return PAD_L + ((new Date(ts).getTime() - d.tMin) / (d.tMax - d.tMin)) * INNER_W;
  }

  function _eqY(val) {
    const d = _equityDomain;
    if (!d || d.yMax === d.yMin) return PAD_T + INNER_H / 2;
    return PAD_T + (1 - (val - d.yMin) / (d.yMax - d.yMin)) * INNER_H;
  }

  const _eqPolyline = $derived.by(() => {
    if (!_equityPoints.length || !_equityDomain) return '';
    return _equityPoints.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.cum_pnl).toFixed(1)}`).join(' ');
  });

  const _eqAreaPath = $derived.by(() => {
    if (!_equityPoints.length || !_equityDomain) return '';
    const pts = _equityPoints;
    const zero = _eqY(0);
    const first = `${_eqX(pts[0].ts).toFixed(1)},${zero}`;
    const last  = `${_eqX(pts[pts.length - 1].ts).toFixed(1)},${zero}`;
    const line  = pts.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.cum_pnl).toFixed(1)}`).join(' L ');
    return `M ${first} L ${line} L ${last} Z`;
  });

  const _eqZeroY = $derived(_equityDomain ? _eqY(0) : null);

  const _eqPositive = $derived(
    _equityPoints.length ? _equityPoints[_equityPoints.length - 1].cum_pnl >= 0 : true
  );

  const _eqLineColor  = $derived(_eqPositive ? '#4ade80' : '#f87171');
  const _eqFillColor  = $derived(_eqPositive ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)');

  // Y-axis labels for equity chart (5 ticks)
  const _eqYLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d) return [];
    return Array.from({ length: 5 }, (_, i) => {
      const frac = i / 4;
      const val  = d.yMin + frac * (d.yMax - d.yMin);
      const y    = _eqY(val);
      return { y: y.toFixed(1), label: aggCompact(val) };
    });
  });

  // X-axis time labels (up to 5)
  const _eqXLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d || _equityPoints.length < 2) return [];
    const count = Math.min(5, _equityPoints.length);
    const step = Math.floor((_equityPoints.length - 1) / (count - 1 || 1));
    return Array.from({ length: count }, (_, i) => {
      const pt = _equityPoints[Math.min(i * step, _equityPoints.length - 1)];
      const x  = _eqX(pt.ts).toFixed(1);
      // times from backend are UTC; display in IST
      const d2  = new Date(pt.ts);
      const ist = new Date(d2.getTime() + 5.5 * 3600 * 1000);
      const ih  = String(ist.getUTCHours()).padStart(2, '0');
      const im  = String(ist.getUTCMinutes()).padStart(2, '0');
      return { x, label: `${ih}:${im}` };
    });
  });

  // Hover crosshair state
  let _hoverIdx = $state(/** @type {number|null} */ (null));
  let _hoverX   = $state(0);
  let _hoverY   = $state(0);

  const _hoverPt = $derived(
    _hoverIdx != null && _equityPoints[_hoverIdx]
      ? _equityPoints[_hoverIdx]
      : null
  );

  function _eqMouseMove(/** @type {MouseEvent} */ e) {
    if (!_equityPoints.length || !_equityDomain) return;
    const svg = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const svgX = ((e.clientX - rect.left) / rect.width) * CHART_W;
    const frac = Math.max(0, Math.min(1, (svgX - PAD_L) / INNER_W));
    const ts   = _equityDomain.tMin + frac * (_equityDomain.tMax - _equityDomain.tMin);
    // find nearest point
    let best = 0, bestDt = Infinity;
    for (let i = 0; i < _equityPoints.length; i++) {
      const dt = Math.abs(new Date(_equityPoints[i].ts).getTime() - ts);
      if (dt < bestDt) { bestDt = dt; best = i; }
    }
    _hoverIdx = best;
    _hoverX   = parseFloat(_eqX(_equityPoints[best].ts).toFixed(1));
    _hoverY   = parseFloat(_eqY(_equityPoints[best].cum_pnl).toFixed(1));
  }

  function _eqMouseLeave() { _hoverIdx = null; }

  // ── Margin gauge helpers ───────────────────────────────────────────
  const GAUGE_R = 32;
  const GAUGE_SW = 6;
  const GAUGE_CIRC = 2 * Math.PI * GAUGE_R;

  function _gaugeColor(pct) {
    if (pct < 0.50) return '#4ade80';
    if (pct < 0.70) return '#fbbf24';
    if (pct < 0.85) return '#f59410';
    return '#f87171';
  }

  function _gaugeDash(pct) {
    const used = Math.max(0, Math.min(1, pct)) * GAUGE_CIRC;
    return `${used.toFixed(2)} ${GAUGE_CIRC.toFixed(2)}`;
  }

  // ── Fetch functions ────────────────────────────────────────────────
  async function _fetchEquity() {
    try {
      const res = await fetchIntradayEquity(200);
      _equityPoints = (res?.points ?? []);
    } catch (_) { /* leave stale */ }
  }

  async function _fetchMargins() {
    try {
      const res = await fetchFunds();
      // FundsResponse is `{rows, refreshed_at}`; older callers also
      // hand us a bare array. Accept either.
      const rows = Array.isArray(res) ? res : (res?.rows ?? []);
      if (!Array.isArray(rows) || !rows.length) {
        _margins = []; _funds = []; return;
      }
      // Keep full funds rows for the Capital-card table.
      _funds = rows.filter(r => r.account && r.account !== 'TOTAL');
      // Build gauge rows. Backend field is `avail_margin` (Polars
      // rename of the broker's `net` column); older callers used
      // `available_margin`. Try both.
      _margins = _funds.map(r => {
        const used  = Number(r.used_margin) || 0;
        const avail = Number(r.avail_margin ?? r.available_margin) || 0;
        const total = used + avail;
        return {
          account: String(r.account),
          used,
          avail,
          util_pct: total > 0 ? used / total : 0,
        };
      });
    } catch (_) { _margins = []; _funds = []; }
  }

  async function _fetchConn() {
    try {
      const accounts = await fetchBrokerAccounts();
      if (!Array.isArray(accounts)) return;
      _conn = {
        total:  accounts.length,
        loaded: accounts.filter(a => a.loaded).length,
      };
    } catch (_) { /* leave stale */ }
  }

  async function loadHero() {
    try {
      const [positions, holdings, events] = await Promise.all([
        fetchPositions().catch(() => null),
        fetchHoldings().catch(() => null),
        fetchRecentAgentEvents(100).catch(() => []),
      ]);

      // PositionsResponse / HoldingsResponse are `{rows, summary,
      // refreshed_at}`. Older fixtures handed us bare arrays — accept
      // either. The previous Array.isArray(...) guard silently emptied
      // the state when the actual {rows} object came back, which is
      // why Positions and Holdings showed 0 in the Equity card even
      // when the broker had open trades.
      _positions = Array.isArray(positions) ? positions : (positions?.rows ?? []);
      _holdings  = Array.isArray(holdings)  ? holdings  : (holdings?.rows  ?? []);

      // Sum day's P&L from positions (day-pnl) + holdings (day_change).
      let dayPnl = 0;
      let invVal  = 0;
      for (const p of _positions) dayPnl += Number(p.pnl) || 0;
      for (const h of _holdings) {
        const dc = Number(h.day_change ?? h.day_change_pct_amount ?? 0);
        dayPnl += dc;
        invVal += Number(h.inv_val ?? 0);
      }
      _todayPnl    = dayPnl;
      _startingNav = invVal > 0 ? invVal : null;

      // Agent fires today (IST midnight boundary).
      const todayStart = istMidnightTodayAsDate();
      _firesToday = (events || []).filter((e) => {
        const k = e.kind ?? e.event_type ?? '';
        if (k !== 'agent_fire') return false;
        const t = new Date(e.timestamp ?? e.created_at ?? 0);
        return t >= todayStart;
      }).length;

      _paperOpen = Number(algoStatus.paperStatus?.open_order_count) || 0;
      _heroLoadedAt = clientTimestamp();

      // Parallel: equity curve + margins + conn health + NIFTY quote
      await Promise.all([
        _fetchEquity(),
        _fetchMargins(),
        _fetchConn(),
        _fetchNifty(),
      ]);
    } catch (_) { /* leave previous values up */ }
  }

  async function _fetchNifty() {
    try {
      const res = await batchQuote(['NSE:NIFTY 50']);
      const q = res?.quotes?.['NSE:NIFTY 50'] ?? res?.['NSE:NIFTY 50'] ?? null;
      if (!q) return;
      // Prefer change_percent / change_pct; fall back to (ltp-close)/close*100
      if (q.change_percent != null)     { _niftyDayPct = Number(q.change_percent); return; }
      if (q.change_pct    != null)     { _niftyDayPct = Number(q.change_pct);     return; }
      const ltp   = Number(q.last_price  ?? q.ltp  ?? 0);
      const close = Number(q.ohlc?.close ?? q.close ?? 0);
      if (close > 0 && ltp > 0) _niftyDayPct = ((ltp - close) / close) * 100;
    } catch (_) { /* leave null — chip stays "—" */ }
  }

  onMount(() => {
    bannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
    _pnlOpen = localStorage.getItem('dash.pnlOpen') === '1';
    // Restore the Equity-card account filter from sessionStorage.
    // Stored as a JSON-encoded string array. Wrapped in try/catch
    // because the cached value's account names may no longer exist
    // on this server (operator switched broker accounts) — silently
    // ignore + fall back to all-accounts.
    try {
      const cached = sessionStorage.getItem('dash.equityAccounts');
      if (cached) _selectedAccounts = JSON.parse(cached);
    } catch (_) { _selectedAccounts = []; }
    loadHero();
    _heroTeardown = visibleInterval(loadHero, 30000);
    // Market-wide quotes for Underlying / Midcap / Smallcap winners
    // and losers. One batched request covers all three universes;
    // 60 s poll keeps the buckets fresh during market hours without
    // hammering Kite's quote endpoint.
    loadMarketMovers();
    _stopMarketPoll = visibleInterval(loadMarketMovers, 60000);
  });

  async function loadMarketMovers() {
    try {
      // De-dup keys across universes (e.g. NIFTY MIDCAP 100 appears
      // in both FO_QUOTE_KEYS and the midcap index — irrelevant on
      // the server but cleaner on the wire).
      const keys = [...new Set([
        ...FO_QUOTE_KEYS, ...MIDCAP_QUOTE_KEYS, ...SMLCAP_QUOTE_KEYS,
      ])];
      const res = await batchQuote(keys);
      const map = res?.quotes ?? res ?? {};
      // Reassign rather than merge so disappearing symbols clear
      // out (avoids stale rows lingering in winners/losers buckets).
      _marketQuotes = map;
    } catch (_) { /* transient — leave previous map up */ }
  }

  // Persist filter changes — keep in sessionStorage so it survives a
  // page refresh but resets per session (operator's filter intent
  // doesn't usually carry across days).
  $effect(() => {
    try {
      sessionStorage.setItem('dash.equityAccounts', JSON.stringify(_selectedAccounts));
    } catch (_) { /* sessionStorage quota / blocked — silent. */ }
  });
  onDestroy(() => { _heroTeardown?.(); _stopMarketPoll?.(); });

  function dismissBanner() {
    bannerDismissed = true;
    localStorage.setItem('ramboq.demo_banner_dismissed', '1');
  }
</script>

<svelte:head>
  <title>Dashboard | RamboQuant Analytics</title>
</svelte:head>

{#if isDemo && !bannerDismissed}
  <div class="demo-banner" role="status">
    <span class="demo-banner-text">
      <strong>Rambo Terminal — live production</strong> · real broker data · accounts masked · paper-only writes.
      <a href="/showcase" class="demo-banner-link">Take the tour</a>
      <span class="demo-banner-sep">·</span>
      <a href="/signin" class="demo-banner-link">Sign in</a>
    </span>
    <button onclick={dismissBanner} class="demo-banner-close" aria-label="Dismiss">×</button>
  </div>
{/if}

<!-- Page header -->
<div class="page-header">
  <h1 class="algo-page-title">Dashboard</h1>
  <InfoHint popup text="Admin dashboard: P&amp;L analysis first, then funds + position/holdings summary grids, then recent agent activity." />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<!-- Hero row — 6 chips answering "what changed since I last looked?" -->
<div class="hero-row" role="status">
  <!-- 1. P&L TODAY -->
  <div class="hero-chip {_pnlClass}">
    <span class="hero-label">P&amp;L TODAY</span>
    <span class="hero-value">
      {#if _todayPnl == null}—{:else}{_todayPnl >= 0 ? '+' : ''}₹{priceFmt(_todayPnl)}{/if}
    </span>
  </div>

  <!-- 2. TODAY % — portfolio day return -->
  <div class="hero-chip {_todayPctClass}">
    <span class="hero-label">TODAY %</span>
    <span class="hero-value">
      {#if _todayPct == null}—{:else}{_todayPct >= 0 ? '+' : ''}{pctFmt(_todayPct)}%{/if}
    </span>
    {#if _startingNav != null}
      <span class="hero-meta">of ₹{aggCompact(_startingNav)}</span>
    {/if}
  </div>

  <!-- 3. vs NIFTY — outperformance spread -->
  <div class="hero-chip {_vsNiftyClass}">
    <span class="hero-label">vs NIFTY</span>
    <span class="hero-value">
      {#if _vsNifty == null}—{:else}{_vsNifty >= 0 ? '+' : ''}{pctFmt(_vsNifty)}%{/if}
    </span>
    {#if _niftyDayPct != null}
      <span class="hero-meta">NIFTY {_niftyDayPct >= 0 ? '+' : ''}{pctFmt(_niftyDayPct)}%</span>
    {/if}
  </div>

  <!-- 4. AGENT FIRES -->
  <div class="hero-chip hero-chip-fires">
    <span class="hero-label">AGENT FIRES</span>
    <span class="hero-value">{_firesToday}</span>
    <span class="hero-meta">today</span>
  </div>

  <!-- 5. PAPER OPEN -->
  <div class="hero-chip hero-chip-paper">
    <span class="hero-label">PAPER OPEN</span>
    <span class="hero-value">{_paperOpen}</span>
    <span class="hero-meta">orders</span>
  </div>

  <!-- 6. CONN — broker connection health -->
  <div class="hero-chip hero-chip-conn {_connClass}">
    <span class="hero-label">CONN</span>
    <span class="hero-value conn-icon">{_connIcon}</span>
    {#if _conn.total > 0}
      <span class="hero-meta">{_conn.loaded}/{_conn.total}</span>
    {/if}
  </div>

  {#if _heroLoadedAt}
    <span class="hero-refresh">refreshed {_heroLoadedAt}</span>
  {/if}
</div>

<!-- Open orders strip — hidden when nothing is chasing -->
{#if _openOrders.length > 0}
  <div class="dash-open-orders">
    <div class="oo-header">
      <span class="mp-section-label">OPEN ORDERS</span>
      <span class="oo-count">
        <span class="oo-dot" aria-hidden="true"></span>
        {_openOrders.length} chasing
      </span>
    </div>
    <div class="oo-pills">
      {#each _openOrders as ord}
        {@const isBuy = (ord.side ?? '').toUpperCase() === 'BUY'}
        <a
          href="/orders{ord.order_id ? `?order_id=${encodeURIComponent(ord.order_id)}` : ''}"
          class="oo-pill {isBuy ? 'oo-pill-buy' : 'oo-pill-sell'}"
        >
          <span class="oo-side">{isBuy ? 'BUY' : 'SELL'}</span>
          <span class="oo-qty">{ord.qty ?? ord.quantity ?? ''}</span>
          <span class="oo-sym">{ord.symbol ?? ord.tradingsymbol ?? ''}</span>
          <span class="oo-price">@ ₹{priceFmt(ord.limit_price ?? ord.price ?? 0)}</span>
          {#if (ord.attempts ?? 0) > 0}
            <span class="oo-attempts">({ord.attempts})</span>
          {/if}
        </a>
      {/each}
    </div>
  </div>
{/if}

<!-- Row 1: Intraday equity curve (full width hero) -->
<div class="dash-row1">
  <!-- Intraday equity curve — full-width hero. Margin gauges
       moved into the Capital card below, so the chart gets the
       full page width. Reads like Bloomberg's portfolio chart
       on a desktop PRTU page. -->
  <section class="row1-col row1-col-chart" class:fs-card-on={_fsEquityCurve}>
    <div class="card-header-row">
      <div class="mp-section-label">Intraday Equity Curve</div>
      <FullscreenButton bind:isFullscreen={_fsEquityCurve} label="Intraday Equity Curve" />
    </div>
    {#if !_equityPoints.length}
      <div class="eq-empty">
        No data yet — markets open at 09:15 IST
      </div>
    {:else}
      <svg
        class="eq-svg"
        viewBox="0 0 {CHART_W} {CHART_H}"
        preserveAspectRatio="none"
        role="img"
        aria-label="Intraday cumulative P&L curve"
        onmousemove={_eqMouseMove}
        onmouseleave={_eqMouseLeave}
      >
        <!-- Grid lines (horizontal) -->
        {#each [0.0, 0.25, 0.5, 0.75, 1.0] as frac}
          {@const gy = PAD_T + frac * INNER_H}
          <line
            x1={PAD_L} y1={gy} x2={PAD_L + INNER_W} y2={gy}
            stroke="rgba(200,216,240,0.10)" stroke-width="1" />
        {/each}

        <!-- Zero baseline (dotted) -->
        {#if _eqZeroY != null}
          <line
            x1={PAD_L} y1={_eqZeroY} x2={PAD_L + INNER_W} y2={_eqZeroY}
            stroke="rgba(200,216,240,0.45)" stroke-width="1"
            stroke-dasharray="4 3" />
        {/if}

        <!-- Filled area -->
        {#if _eqAreaPath}
          <path d={_eqAreaPath} fill={_eqFillColor} />
        {/if}

        <!-- Line -->
        {#if _eqPolyline}
          <polyline
            points={_eqPolyline}
            fill="none"
            stroke={_eqLineColor}
            stroke-width="1.5"
            stroke-linejoin="round"
            stroke-linecap="round" />
        {/if}

        <!-- Y-axis labels (right) -->
        {#each _eqYLabels as lbl}
          <text
            x={PAD_L + INNER_W + 4} y={parseFloat(lbl.y) + 3.5}
            font-size="9" fill="#7e97b8" font-family="ui-monospace,monospace"
            text-anchor="start">{lbl.label}</text>
        {/each}

        <!-- X-axis labels -->
        {#each _eqXLabels as lbl}
          <text
            x={parseFloat(lbl.x)} y={CHART_H - 6}
            font-size="9" fill="#7e97b8" font-family="ui-monospace,monospace"
            text-anchor="middle">{lbl.label}</text>
        {/each}

        <!-- Hover crosshair -->
        {#if _hoverPt != null}
          <line
            x1={_hoverX} y1={PAD_T} x2={_hoverX} y2={PAD_T + INNER_H}
            stroke="rgba(200,216,240,0.55)" stroke-width="1"
            stroke-dasharray="3 2" />
          <circle cx={_hoverX} cy={_hoverY} r="3"
            fill={_eqLineColor} stroke="#0a1428" stroke-width="1.5" />
          <!-- Tooltip box -->
          {@const _tipX = _hoverX > INNER_W * 0.65 ? _hoverX - 108 : _hoverX + 8}
          {@const _tipY = Math.max(PAD_T, Math.min(_hoverY - 28, PAD_T + INNER_H - 58))}
          <rect x={_tipX} y={_tipY} width="100" height="54"
            rx="3" fill="rgba(10,20,40,0.92)"
            stroke="rgba(126,151,184,0.35)" stroke-width="1" />
          {#if _hoverPt}
            {@const _ist = new Date(new Date(_hoverPt.ts).getTime() + 5.5*3600*1000)}
            {@const _th = String(_ist.getUTCHours()).padStart(2,'0')}
            {@const _tm = String(_ist.getUTCMinutes()).padStart(2,'0')}
            <text x={_tipX + 6} y={_tipY + 13}
              font-size="8.5" fill="#7dd3fc" font-family="ui-monospace,monospace">{_th}:{_tm} IST</text>
            <text x={_tipX + 6} y={_tipY + 26}
              font-size="8" fill="#7e97b8" font-family="ui-monospace,monospace">Day P&amp;L</text>
            <text x={_tipX + 6} y={_tipY + 37}
              font-size="9" font-weight="700" fill={_hoverPt.day_pnl >= 0 ? '#4ade80' : '#f87171'}
              font-family="ui-monospace,monospace"
              style="font-variant-numeric:tabular-nums">
              {_hoverPt.day_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.day_pnl)}
            </text>
            <text x={_tipX + 6} y={_tipY + 49}
              font-size="9" font-weight="700" fill={_hoverPt.cum_pnl >= 0 ? '#4ade80' : '#f87171'}
              font-family="ui-monospace,monospace"
              style="font-variant-numeric:tabular-nums">
              cum {_hoverPt.cum_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.cum_pnl)}
            </text>
          {/if}
        {/if}
      </svg>
    {/if}
  </section>
</div>

<!-- Row 1.5: Capital + Equity buckets — the page's main two-column row.
     Capital (margin gauges + Funds table) sits left, Equity (tabbed
     Positions / Holdings summary) sits right. Both natural-height,
     equal-width on desktop; stacks on mobile.

     Industry reference: IB Mosaic and Bloomberg PRTU tab the
     positions/orders/holdings panes inside a single rectangle —
     same idiom. Count chips on each tab keep the hidden one in
     peripheral awareness. -->
<div class="dash-buckets">
  <!-- Capital card — funds + margin utilisation. The two natural
       siblings: "what cash do I have" + "how much of my margin is
       in use" answer the same question (can I take on more risk?). -->
  <section class="bucket-card bucket-cap" class:fs-card-on={_fsCapital}>
    <div class="bucket-header">
      <span class="mp-section-label">Capital</span>
      <FullscreenButton bind:isFullscreen={_fsCapital} label="Capital" />
    </div>

    <!-- Margin gauges — circular utilisation per account. -->
    <div class="bucket-subheader">Margin Utilisation</div>
    {#if !_margins.length}
      <div class="gauge-empty">No accounts connected</div>
    {:else}
      <div class="gauge-grid">
        {#each _margins as acct}
          {@const color = _gaugeColor(acct.util_pct)}
          {@const dash  = _gaugeDash(acct.util_pct)}
          <div class="gauge-tile">
            <svg class="gauge-svg" viewBox="0 0 80 80" width="80" height="80"
              role="img" aria-label="{acct.account} margin utilisation {(acct.util_pct*100).toFixed(0)}%">
              <circle cx="40" cy="40" r={GAUGE_R} fill="none"
                stroke="rgba(126,151,184,0.18)" stroke-width={GAUGE_SW} />
              <circle cx="40" cy="40" r={GAUGE_R} fill="none"
                stroke={color} stroke-width={GAUGE_SW}
                stroke-dasharray={dash} stroke-linecap="round"
                transform="rotate(-90 40 40)" />
              <text x="40" y="44" text-anchor="middle"
                font-size="13" font-weight="800"
                font-family="ui-monospace,monospace"
                style="font-variant-numeric:tabular-nums"
                fill={color}>{(acct.util_pct * 100).toFixed(0)}%</text>
            </svg>
            <span class="gauge-label">{acct.account}</span>
            <span class="gauge-detail">
              ₹{aggCompact(acct.used)} / ₹{aggCompact(acct.used + acct.avail)}
            </span>
          </div>
        {/each}
      </div>
    {/if}

    <!-- Funds table — per-account cash + collateral + avail/used
         margin. Compact HTML table; ag-Grid is overkill for 2-3
         rows + a TOTAL. Right-aligned monospace numbers; muted
         TOTAL row at the bottom. -->
    <div class="bucket-subheader bucket-subheader-spaced">Funds</div>
    {#if !_funds.length}
      <div class="cap-empty">No fund data — broker not connected</div>
    {:else}
      {@const total = {
        cash:        _funds.reduce((s, r) => s + (Number(r.cash) || 0), 0),
        collateral:  _funds.reduce((s, r) => s + (Number(r.collateral) || 0), 0),
        avail_margin:_funds.reduce((s, r) => s + (Number(r.avail_margin ?? r.available_margin) || 0), 0),
        used_margin: _funds.reduce((s, r) => s + (Number(r.used_margin) || 0), 0),
      }}
      <div class="cap-table-wrap">
      <table class="cap-table">
        <thead>
          <tr>
            <th class="cap-th-l">Account</th>
            <th>Cash</th>
            <th>Collat</th>
            <th>Avail</th>
            <th>Used</th>
          </tr>
        </thead>
        <tbody>
          {#each _funds as r}
            <tr>
              <td class="cap-acct">{r.account}</td>
              <td class="cap-num">₹{aggCompact(Number(r.cash) || 0)}</td>
              <td class="cap-num">₹{aggCompact(Number(r.collateral) || 0)}</td>
              <td class="cap-num">₹{aggCompact(Number(r.avail_margin ?? r.available_margin) || 0)}</td>
              <td class="cap-num">₹{aggCompact(Number(r.used_margin) || 0)}</td>
            </tr>
          {/each}
          <tr class="cap-total">
            <td class="cap-acct">TOTAL</td>
            <td class="cap-num">₹{aggCompact(total.cash)}</td>
            <td class="cap-num">₹{aggCompact(total.collateral)}</td>
            <td class="cap-num">₹{aggCompact(total.avail_margin)}</td>
            <td class="cap-num">₹{aggCompact(total.used_margin)}</td>
          </tr>
        </tbody>
      </table>
      </div>
    {/if}
  </section>

  <!-- Equity card — Positions Summary + Holdings Summary stacked.
       Earlier this was tabbed [Positions] / [Holdings], but tab-view
       left the right half of the card empty when one side rendered.
       Stacked uses the card's full vertical space: positions on top
       (intraday-relevant, glanced first), holdings below. Both are
       compact HTML tables sharing the same .cap-table treatment as
       Capital. Counts are inline next to each sub-heading. -->
  <section class="bucket-card bucket-eq" class:fs-card-on={_fsEquity}>
    <div class="bucket-header">
      <span class="mp-section-label">Equity</span>
      <!-- Account filter — MultiSelect with one option per broker
           account seen in the current positions + holdings. Empty
           selection = all accounts (no filter). Applies to BOTH
           Positions Summary and Holdings Summary tables below. -->
      <div class="eq-acct-picker" title="Filter Positions + Holdings by account">
        <MultiSelect
          bind:value={_selectedAccounts}
          options={_availableAccounts.map(a => ({ value: a, label: a }))}
          placeholder="All accounts"
          ariaLabel="Filter by broker account" />
      </div>
      <FullscreenButton bind:isFullscreen={_fsEquity} label="Equity" />
    </div>

    <!-- Positions Summary — intraday, glanced first. -->
    <div class="bucket-subheader">
      Positions
      <span class="eq-count">{_positionsCount}</span>
    </div>
    {#if !_positionsSummary.length}
      <div class="cap-empty">No open positions</div>
    {:else}
      <div class="cap-table-wrap">
      <table class="cap-table">
        <thead>
          <tr>
            <th class="cap-th-l">Account</th>
            <th>Day P&amp;L</th>
            <th>Open P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {#each _positionsSummary as r}
            <tr>
              <td class="cap-acct">{r.account}</td>
              <td class="cap-num {_pnlColor(r.day_pnl)}">{r.day_pnl >= 0 ? '+' : ''}₹{priceFmt(r.day_pnl)}</td>
              <td class="cap-num {_pnlColor(r.pnl)}">{r.pnl >= 0 ? '+' : ''}₹{priceFmt(r.pnl)}</td>
            </tr>
          {/each}
          <tr class="cap-total">
            <td class="cap-acct">TOTAL</td>
            <td class="cap-num {_pnlColor(_positionsTotal.day_pnl)}">{_positionsTotal.day_pnl >= 0 ? '+' : ''}₹{priceFmt(_positionsTotal.day_pnl)}</td>
            <td class="cap-num {_pnlColor(_positionsTotal.pnl)}">{_positionsTotal.pnl >= 0 ? '+' : ''}₹{priceFmt(_positionsTotal.pnl)}</td>
          </tr>
        </tbody>
      </table>
      </div>
    {/if}

    <!-- Holdings Summary — EOD picture, sits below positions. -->
    <div class="bucket-subheader bucket-subheader-spaced">
      Holdings
      <span class="eq-count">{_holdingsCount}</span>
    </div>
    {#if !_holdingsSummary.length}
      <div class="cap-empty">No holdings</div>
    {:else}
      <div class="cap-table-wrap">
      <table class="cap-table">
        <thead>
          <tr>
            <th class="cap-th-l">Account</th>
            <th>Day P&amp;L</th>
            <th>Open P&amp;L</th>
            <th>Cur Val</th>
          </tr>
        </thead>
        <tbody>
          {#each _holdingsSummary as r}
            <tr>
              <td class="cap-acct">{r.account}</td>
              <td class="cap-num {_pnlColor(r.day_pnl)}">{r.day_pnl >= 0 ? '+' : ''}₹{priceFmt(r.day_pnl)}</td>
              <td class="cap-num {_pnlColor(r.pnl)}">{r.pnl >= 0 ? '+' : ''}₹{priceFmt(r.pnl)}</td>
              <td class="cap-num">₹{aggCompact(r.cur_val)}</td>
            </tr>
          {/each}
          <tr class="cap-total">
            <td class="cap-acct">TOTAL</td>
            <td class="cap-num {_pnlColor(_holdingsTotal.day_pnl)}">{_holdingsTotal.day_pnl >= 0 ? '+' : ''}₹{priceFmt(_holdingsTotal.day_pnl)}</td>
            <td class="cap-num {_pnlColor(_holdingsTotal.pnl)}">{_holdingsTotal.pnl >= 0 ? '+' : ''}₹{priceFmt(_holdingsTotal.pnl)}</td>
            <td class="cap-num">₹{aggCompact(_holdingsTotal.cur_val)}</td>
          </tr>
        </tbody>
      </table>
      </div>
    {/if}
  </section>
</div>

<!-- Row 2: Top Winners (left) + Top Losers (right). Each card carries
     five tabbed buckets — Underlying · Midcap · Smallcap · Holdings ·
     Positions. Active tab carries the amber underline; count chip on
     each tab so the operator sees every bucket's denominator without
     flipping. Default tab: Underlying (the broadest aggregation, the
     operator's first question on the dashboard).

     Tabbed (not stacked) so each card stays compact — earlier the
     stacked version filled an extra full screen-height with 5×3 rows
     per side. -->
{#if _hasWinners || _hasLosers}
  <div class="dash-row2">
    {#if _hasWinners}
      {@const winRows = (_winnerBuckets.find(b => b.label === _winTabLabel(_winTab)))?.rows ?? []}
      <section class="wl-tile wl-tile-win" class:fs-card-on={_fsWinners}>
        <div class="card-header-row">
          <span class="mp-section-label wl-tile-label">TOP WINNERS</span>
          <!-- Same _selectedAccounts state as the Equity card —
               filter applied here propagates to every bucket
               source (underlying / midcap / smallcap / holdings /
               positions). Operator doesn't need to scroll back up
               to the Equity card to change scope. -->
          <div class="wl-acct-picker" title="Filter by broker account (shared across cards)">
            <MultiSelect
              bind:value={_selectedAccounts}
              options={_availableAccounts.map(a => ({ value: a, label: a }))}
              placeholder="All accounts"
              ariaLabel="Filter Top Winners by broker account" />
          </div>
          <FullscreenButton bind:isFullscreen={_fsWinners} label="Top Winners" />
        </div>
        <div class="wl-tabs" role="tablist">
          {#each _winnerBuckets as bucket}
            {@const key = _bucketKey(bucket.label)}
            <button
              type="button"
              role="tab"
              class="wl-tab"
              class:wl-tab-on={_winTab === key}
              aria-selected={_winTab === key}
              onclick={() => _winTab = key}>
              {_tabShort(bucket.label)}
              <span class="wl-tab-count" title={bucket.count > _TOP_N ? `${bucket.count} total — top ${_TOP_N} shown` : `${bucket.count} total`}>{bucket.count}</span>
            </button>
          {/each}
        </div>
        {#if winRows.length === 0}
          <div class="wl-bucket-empty">No winners in this bucket</div>
        {:else}
          <div class="wl-rows">
            {#each winRows as row}
              <button
                class="wl-row"
                onclick={() => {
                  const sym = row.symbol.trim();
                  if (!sym) return;
                  _ticketProps = {
                    symbol:     sym,
                    defaultTab: 'chart',
                    onClose:    () => { _ticketProps = null; },
                    onSubmit:   () => { _ticketProps = null; },
                  };
                }}
              >
                <span class="wl-sym">{row.symbol}</span>
                {#if row.kind === 'market'}
                  <!-- Market-wide winners — pnl IS the day %; ltp shows
                       alongside so the operator sees both move + price. -->
                  <span class="wl-pnl wl-pnl-up">+{pctFmt(row.pnl)}%</span>
                  {#if row.ltp > 0}
                    <span class="wl-pct">@ ₹{priceFmt(row.ltp)}</span>
                  {/if}
                {:else}
                  <!-- User-scoped (Holdings / Positions) — pnl is ₹ money. -->
                  <span class="wl-pnl wl-pnl-up">+₹{priceFmt(row.pnl)}</span>
                  {#if row.inv_val > 0}
                    <span class="wl-pct">({pctFmt((row.pnl / row.inv_val) * 100)}%)</span>
                  {/if}
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      </section>
    {/if}

    {#if _hasLosers}
      {@const losRows = (_loserBuckets.find(b => b.label === _winTabLabel(_losTab)))?.rows ?? []}
      <section class="wl-tile wl-tile-loss" class:fs-card-on={_fsLosers}>
        <div class="card-header-row">
          <span class="mp-section-label wl-tile-label">TOP LOSERS</span>
          <div class="wl-acct-picker" title="Filter by broker account (shared across cards)">
            <MultiSelect
              bind:value={_selectedAccounts}
              options={_availableAccounts.map(a => ({ value: a, label: a }))}
              placeholder="All accounts"
              ariaLabel="Filter Top Losers by broker account" />
          </div>
          <FullscreenButton bind:isFullscreen={_fsLosers} label="Top Losers" />
        </div>
        <div class="wl-tabs" role="tablist">
          {#each _loserBuckets as bucket}
            {@const key = _bucketKey(bucket.label)}
            <button
              type="button"
              role="tab"
              class="wl-tab"
              class:wl-tab-on={_losTab === key}
              aria-selected={_losTab === key}
              onclick={() => _losTab = key}>
              {_tabShort(bucket.label)}
              <span class="wl-tab-count" title={bucket.count > _TOP_N ? `${bucket.count} total — top ${_TOP_N} shown` : `${bucket.count} total`}>{bucket.count}</span>
            </button>
          {/each}
        </div>
        {#if losRows.length === 0}
          <div class="wl-bucket-empty">No losers in this bucket</div>
        {:else}
          <div class="wl-rows">
            {#each losRows as row}
              <button
                class="wl-row"
                onclick={() => {
                  const sym = row.symbol.trim();
                  if (!sym) return;
                  _ticketProps = {
                    symbol:     sym,
                    defaultTab: 'chart',
                    onClose:    () => { _ticketProps = null; },
                    onSubmit:   () => { _ticketProps = null; },
                  };
                }}
              >
                <span class="wl-sym">{row.symbol}</span>
                {#if row.kind === 'market'}
                  <!-- Market-wide losers — pnl is the day % (already negative). -->
                  <span class="wl-pnl wl-pnl-down">{pctFmt(row.pnl)}%</span>
                  {#if row.ltp > 0}
                    <span class="wl-pct">@ ₹{priceFmt(row.ltp)}</span>
                  {/if}
                {:else}
                  <!-- User-scoped — pnl is ₹ money. -->
                  <span class="wl-pnl wl-pnl-down">-₹{priceFmt(Math.abs(row.pnl))}</span>
                  {#if row.inv_val > 0}
                    <span class="wl-pct">({pctFmt((row.pnl / row.inv_val) * 100)}%)</span>
                  {/if}
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  </div>
{/if}

<!-- Row 3: Market news strip — single column. -->
<div class="dash-row3" class:fs-card-on={_fsNews}>
  <div class="row3-header">
    <span class="mp-section-label">MARKET NEWS</span>
    <FullscreenButton bind:isFullscreen={_fsNews} label="Market News" />
  </div>
  <NewsList limit={5} showRefreshTime={true} />
</div>

<!-- P&L Analysis — full-width collapsible. Capital + Equity buckets
     above already cover the per-account summary view (which is what
     the old MarketPulse instance gave us), so the dashboard now
     drops MarketPulse entirely. P&L Analysis stays as the deeper
     "drill into a date / segment / agent" surface below the fold. -->
<details
  class="dash-pnl-details dash-pnl-full"
  class:fs-card-on={_fsPnl}
  bind:open={_pnlOpen}
  ontoggle={() => localStorage.setItem('dash.pnlOpen', _pnlOpen ? '1' : '0')}
>
  <summary class="dash-pnl-summary">
    <span class="mp-section-label">P&amp;L ANALYSIS</span>
    <span class="dash-pnl-toggle">{_pnlOpen ? '▾ collapse' : '▸ expand'}</span>
    <FullscreenButton bind:isFullscreen={_fsPnl} label="P&L Analysis" />
  </summary>
  <div class="dash-pnl-body">
    <PnlAnalysis />
  </div>
</details>

<!-- SymbolPanel — opened by winners/losers tile clicks -->
{#if _ticketProps}
  <SymbolPanel
    {..._ticketProps}
    onClose={() => { _ticketProps = null; }}
    onSubmit={() => { _ticketProps = null; }} />
{/if}

<!-- Agent activity — collapsed by default. Expands to a clean
     fires-only log; chip flips to also show action successes/errors
     for the deeper "what did the fire actually do" trace. -->
<details class="dash-agent" class:fs-card-on={_fsAgent} bind:open={_agentLogOpen}>
  <summary class="dash-agent-summary">
    <span class="mp-section-label">Agent activity</span>
    <span class="dash-agent-chip">
      <span class="dash-agent-count">{_firesToday}</span>
      <span class="dash-agent-label">fires today</span>
    </span>
    <span class="dash-agent-toggle">{_agentLogOpen ? '▾ hide log' : '▸ show log'}</span>
    <FullscreenButton bind:isFullscreen={_fsAgent} label="Agent activity" />
  </summary>
  <!-- Inline filter chip — flips fires-only vs fires+actions. Click
       handler stops the click from bubbling up to the <summary>
       (which would toggle the details element instead). -->
  <div class="dash-agent-filter">
    <button
      type="button"
      class="dash-agent-filter-btn"
      class:dash-agent-filter-btn-on={_agentLogShowActions}
      onclick={(e) => { e.preventDefault(); e.stopPropagation();
                        _agentLogShowActions = !_agentLogShowActions; }}>
      {_agentLogShowActions ? '✓' : ''} include action events
    </button>
    <span class="dash-agent-filter-hint">
      {_agentLogShowActions
        ? 'showing fires + action successes/errors'
        : 'showing fires only — toggle to include actions'}
    </span>
  </div>
  <UnifiedLog
    filter={{ kinds: _agentLogKinds }}
    excludeSim={true}
    maxRows={30}
    emptyMessage="No agent fires yet today." />
</details>

<style>
  .algo-page-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-family: ui-monospace, monospace;
  }
  :global(.page-header:has(.algo-page-title)) {
    border-bottom: none;
    padding-bottom: 0;
    margin-bottom: 0.3rem;
  }

  /* Section labels — used as the heading inside every dashboard card
     (Intraday Equity Curve, Margin Utilisation, Top Winners, Top
     Losers, Market News, P&L Analysis, Agent activity, OPEN ORDERS).
     Treatment: amber accent bar on the left + amber small-caps text.
     Reads as a classic trader-platform "section tag" — distinct from
     the body but tasteful, not shouting. */
  .mp-section-label {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #fbbf24;
    margin-bottom: 0.45rem;
    padding: 0.05rem 0;
  }
  .mp-section-label::before {
    content: '';
    display: inline-block;
    width: 3px;
    height: 0.85rem;
    background: linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%);
    border-radius: 1px;
    flex-shrink: 0;
    box-shadow: 0 0 6px rgba(251, 191, 36, 0.45);
  }

  /* ── Hero row ────────────────────────────────────────────────────── */
  /* All card-shaped sections on this page (hero row, row1 cols, wl
     tiles, news strip, collapsible summaries) inherit the canonical
     algo-status-card chrome — gradient bg + 1.5px border + box-shadow.
     Match the visual depth of /agents, /admin/options, /admin/execution
     so the dashboard doesn't read as one-generation-back. */
  /* Hide the CONN chip on narrow viewports — it tends to wrap onto a
     row of its own (single chip, looks orphaned) and "no accounts
     connected" has no actionable meaning for a demo / recruiter
     visitor scanning the dashboard on a phone. Operators on desktop
     keep it as a glanceable broker-health indicator. */
  @media (max-width: 600px) {
    .hero-chip.hero-chip-conn { display: none; }
  }
  .hero-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.6rem;
    margin: 0 0 0.6rem 0;
    padding: 0.5rem 0.7rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }
  .hero-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35rem;
    padding: 0.18rem 0.55rem;
    border-left: 2px solid;
    background: rgba(255,255,255,0.02);
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    line-height: 1;
  }
  .hero-label {
    color: #7e97b8;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .hero-value {
    font-size: 0.82rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    color: #f1f7ff;
  }
  .hero-meta {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
  }
  .hero-pnl-up      { border-left-color: #4ade80; }
  .hero-pnl-up      .hero-value { color: #4ade80; }
  .hero-pnl-down    { border-left-color: #f87171; }
  .hero-pnl-down    .hero-value { color: #f87171; }
  .hero-pnl-neutral { border-left-color: #7e97b8; }
  .hero-chip-fires  { border-left-color: #fbbf24; }
  .hero-chip-paper  { border-left-color: #7dd3fc; }

  /* CONN chip — border driven by conn state class */
  .hero-chip-conn { border-left-color: #7e97b8; }
  .hero-chip-conn-green { border-left-color: #4ade80; }
  .hero-chip-conn-green .hero-value,
  .hero-chip-conn-green .conn-icon { color: #4ade80; }
  .hero-chip-conn-amber { border-left-color: #fbbf24; }
  .hero-chip-conn-amber .hero-value,
  .hero-chip-conn-amber .conn-icon { color: #fbbf24; }
  .hero-chip-conn-red   { border-left-color: #f87171; }
  .hero-chip-conn-red   .hero-value,
  .hero-chip-conn-red   .conn-icon { color: #f87171; }
  .hero-chip-conn-neutral { border-left-color: #7e97b8; }

  .hero-refresh {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
  }

  /* ── Row 1: equity curve (full-width hero) ───────────────────────── */
  /* Earlier the equity curve shared this row with the margin gauges
     at a 1.6:1 ratio (≥1024px). Gauges moved into the Capital bucket
     so the chart now claims the full width — same idiom as Bloomberg
     PRTU's portfolio chart at the top of a desktop view. */
  .dash-row1 {
    display: block;
    margin-bottom: 0.75rem;
  }
  .row1-col {
    min-width: 0;
    padding: 0.65rem 0.75rem 0.6rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }

  /* Equity curve */
  .eq-svg {
    display: block;
    width: 100%;
    height: 220px;
    cursor: crosshair;
    overflow: visible;
  }
  .eq-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 220px;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    letter-spacing: 0.04em;
  }

  /* Margin gauges */
  .gauge-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100px;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    letter-spacing: 0.04em;
  }
  .gauge-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 1.2rem 1.5rem;
    padding: 0.4rem 0 0.2rem;
    justify-content: center;
  }
  .gauge-tile {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
  }
  .gauge-svg {
    display: block;
    flex-shrink: 0;
  }
  .gauge-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    color: #7e97b8;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .gauge-detail {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.03em;
  }

  /* ── Capital + Equity buckets ───────────────────────────────────────
     The page's main two-column row. Capital (margin gauges + Funds
     table) and Equity (tabbed Positions / Holdings) are natural
     siblings and read at equal weight. Stacks on mobile; equal
     column widths on desktop so neither dominates. */
  .dash-buckets {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }
  @media (min-width: 1024px) {
    .dash-buckets {
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      /* align-items defaults to stretch — both cards match the
         tallest sibling's height. Earlier `start` left the shorter
         card with a visual height-mismatch on desktop. */
    }
  }
  .bucket-card {
    min-width: 0;
    padding: 0.65rem 0.75rem 0.7rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    /* Flex column so the card stretches to the row's height while
       its content stacks naturally from the top. Without this,
       grid-stretch makes the .bucket-card box grow but its inner
       <table>+<div> children stay at their natural height, leaving
       the visible content top-aligned but the card border bottom-
       extended — which is what we want. */
    display: flex;
    flex-direction: column;
  }
  .bucket-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }
  .bucket-subheader {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 0.35rem;
  }
  .bucket-subheader-spaced { margin-top: 0.7rem; }

  /* Account picker sits between the Equity heading and the
     fullscreen toggle in the bucket header. margin-left:auto so
     the heading stays left, the picker + FS button cluster right.
     Width clamped — operator usually has 2-3 accounts, so a wide
     dropdown is wasted space. */
  .eq-acct-picker {
    margin-left: auto;
    min-width: 8.5rem;
    max-width: 14rem;
    flex-shrink: 1;
  }
  @media (max-width: 600px) {
    .eq-acct-picker {
      min-width: 6rem;
      max-width: 9rem;
    }
  }

  /* Inline count chip used inside Equity sub-headings (Positions
     and Holdings). Small muted pill — at-a-glance "how many", not
     a primary action. Reads as a subordinate of the sub-heading
     label, not a separate element. */
  .eq-count {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.32rem;
    margin-left: 0.35rem;
    border-radius: 8px;
    background: rgba(126, 151, 184, 0.18);
    color: #c8d8f0;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0;
    font-variant-numeric: tabular-nums;
    text-transform: none;
  }

  /* Compact HTML tables — Funds / Positions Summary / Holdings
     Summary all share this treatment. Hairline column rules,
     right-aligned monospace numbers, muted TOTAL row at the
     bottom. ag-Grid is overkill for 2-4 rows. */
  .cap-table-wrap {
    overflow-x: auto;
  }
  .cap-table {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
  }
  .cap-table th, .cap-table td {
    padding: 0.28rem 0.5rem;
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .cap-table th {
    color: #7e97b8;
    font-weight: 700;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-bottom: 1px solid rgba(126, 151, 184, 0.20);
  }
  .cap-table .cap-th-l { text-align: left; }
  .cap-table td.cap-acct {
    text-align: left;
    color: #e2ecff;
    font-weight: 700;
  }
  .cap-table td.cap-num { color: #c8d8f0; }
  .cap-table tr.cap-total {
    border-top: 1px solid rgba(126, 151, 184, 0.30);
  }
  .cap-table tr.cap-total td.cap-acct {
    color: #fbbf24;
    font-weight: 800;
    font-size: 0.6rem;
    letter-spacing: 0.06em;
  }
  .cap-table tr.cap-total td.cap-num { font-weight: 800; }
  .cap-table tbody tr:hover {
    background: rgba(255, 255, 255, 0.025);
  }
  .cap-up      { color: #4ade80; }
  .cap-down    { color: #f87171; }
  .cap-neutral { color: #c8d8f0; }
  .cap-empty {
    padding: 1rem 0.5rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    text-align: center;
  }

  /* Full-width P&L details (no longer in a 2-col grid). */
  .dash-pnl-full {
    display: block;
    margin-bottom: 0.6rem;
  }

  /* Agent log */
  .dash-agent {
    margin-top: 0.6rem;
  }
  .dash-agent-summary {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .dash-agent-summary::-webkit-details-marker { display: none; }
  .dash-agent-summary:hover {
    border-color: rgba(251, 191, 36, 0.50);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-agent[open] > .dash-agent-summary {
    border-color: rgba(251, 191, 36, 0.65);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-agent-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.1rem 0.5rem;
    border-left: 2px solid #fbbf24;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    line-height: 1;
  }
  .dash-agent-count {
    color: #fbbf24;
    font-size: 0.85rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .dash-agent-label {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .dash-agent-toggle {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
  }
  /* Inline filter strip inside the expanded agent-activity log.
     The chip on the left is a toggleable pill; the hint on the
     right just describes what the operator is currently looking at. */
  .dash-agent-filter {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.35rem 0.5rem;
    margin-top: 0.35rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.15);
    font-family: ui-monospace, monospace;
  }
  .dash-agent-filter-btn {
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.25);
    color: #c8d8f0;
    font-family: inherit;
    font-size: 0.6rem;
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    cursor: pointer;
    transition: background-color 0.15s, color 0.15s;
  }
  .dash-agent-filter-btn:hover {
    background: rgba(251, 191, 36, 0.10);
    color: #fbbf24;
  }
  .dash-agent-filter-btn-on {
    background: rgba(251, 191, 36, 0.18);
    border-color: rgba(251, 191, 36, 0.55);
    color: #fbbf24;
  }
  .dash-agent-filter-hint {
    color: rgba(126, 151, 184, 0.65);
    font-size: 0.56rem;
    letter-spacing: 0.02em;
  }
  .pnl-section-label {
    margin-top: 0.75rem;
    margin-bottom: 0.3rem;
  }

  /* ── Open orders strip ───────────────────────────────────────────── */
  .dash-open-orders {
    margin-bottom: 0.6rem;
    padding: 0.4rem 0.55rem;
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .oo-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.35rem;
  }
  .oo-count {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    color: #7dd3fc;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .oo-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #7dd3fc;
    animation: oo-pulse 2s ease-in-out infinite;
  }
  @keyframes oo-pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }
  .oo-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }
  .oo-pill {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    text-decoration: none;
    border: 1px solid;
    font-variant-numeric: tabular-nums;
    transition: filter 0.12s;
    white-space: nowrap;
  }
  .oo-pill:hover { filter: brightness(1.15); }
  .oo-pill-buy {
    background: rgba(74, 222, 128, 0.10);
    border-color: rgba(74, 222, 128, 0.30);
    color: #a7f3c0;
  }
  .oo-pill-sell {
    background: rgba(248, 113, 113, 0.10);
    border-color: rgba(248, 113, 113, 0.30);
    color: #fca5a5;
  }
  .oo-side   { font-weight: 800; font-size: 0.58rem; letter-spacing: 0.06em; }
  .oo-qty    { font-weight: 700; }
  .oo-sym    { font-weight: 700; }
  .oo-price  { color: #c8d8f0; }
  .oo-attempts { color: #7e97b8; font-size: 0.58rem; }

  /* ── Row 2: Winners / Losers ─────────────────────────────────────── */
  .dash-row2 {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  @media (min-width: 1024px) {
    .dash-row2 {
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
    }
  }
  .wl-tile {
    padding: 0.65rem 0.75rem 0.6rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-top-width: 3px;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    min-width: 0;
  }
  /* Coloured top accent on each winner/loser tile — same idiom as
     /showcase cards. Identity is in the border-top stripe, not in
     the body tint, so the tiles still belong to the algo card family. */
  .wl-tile-win  { border-top-color: rgba(74, 222, 128, 0.85); }
  .wl-tile-loss { border-top-color: rgba(248, 113, 113, 0.85); }
  .wl-tile-label {
    margin-bottom: 0;
  }
  /* Card-header row used by every card carrying a FullscreenButton —
     section label on the left, expand toggle pushed to the right. */
  .card-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    margin-bottom: 0.4rem;
  }
  /* Winners / Losers — 5 tabbed buckets per card. Tab strip sits
     below the card heading; active tab gets the amber underline +
     brighter text. Count chip stays muted (slate-blue when inactive,
     amber when active). Tab labels are short ("Underlying" / "Midcap"
     / …) so the row fits one line on desktop and wraps on mobile. */
  .wl-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.05rem 0.15rem;
    margin-bottom: 0.4rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    padding-bottom: 0.05rem;
  }
  .wl-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    background: none;
    border: none;
    padding: 0.22rem 0.4rem 0.24rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.12s, border-color 0.12s;
  }
  .wl-tab:hover { color: #c8d8f0; }
  .wl-tab-on {
    color: #fbbf24;
    border-bottom-color: #fbbf24;
  }
  .wl-tab-count {
    display: inline-flex;
    align-items: center;
    padding: 0.02rem 0.3rem;
    border-radius: 7px;
    background: rgba(126, 151, 184, 0.18);
    color: #c8d8f0;
    font-size: 0.5rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .wl-tab-on .wl-tab-count {
    background: rgba(251, 191, 36, 0.20);
    color: #fbbf24;
  }
  .wl-bucket-empty {
    padding: 0.5rem 0.3rem;
    color: rgba(126, 151, 184, 0.55);
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    letter-spacing: 0.04em;
    text-align: center;
  }
  /* Account picker on Winners / Losers cards. Sits between the
     section label and the fullscreen toggle in the card header.
     Width clamped — operator usually has 2-3 accounts; a wide
     dropdown wastes space. */
  .wl-acct-picker {
    margin-left: auto;
    min-width: 7.5rem;
    max-width: 12rem;
    flex-shrink: 1;
  }
  @media (max-width: 600px) {
    .wl-acct-picker {
      min-width: 5.5rem;
      max-width: 8rem;
    }
  }

  /* Scrollable rows container — default-size cards cap the visible
     height so 10 rows don't bloat the card; fullscreen mode lifts
     the cap to fill the modal. Custom scrollbar matches the algo
     palette (muted slate, amber on hover). */
  .wl-rows {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: 16rem;
    overflow-y: auto;
    /* Firefox + Chrome custom scrollbar */
    scrollbar-width: thin;
    scrollbar-color: rgba(126, 151, 184, 0.35) transparent;
  }
  .wl-rows::-webkit-scrollbar { width: 6px; }
  .wl-rows::-webkit-scrollbar-track { background: transparent; }
  .wl-rows::-webkit-scrollbar-thumb {
    background: rgba(126, 151, 184, 0.35);
    border-radius: 3px;
  }
  .wl-rows::-webkit-scrollbar-thumb:hover {
    background: rgba(251, 191, 36, 0.55);
  }
  /* Fullscreen mode lifts the height cap so the operator can scan
     the full top-10 (or top-50 in the future) without scrolling. */
  .fs-card-on .wl-rows {
    max-height: calc(100vh - 12rem);
  }
  .wl-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    width: 100%;
    padding: 0.22rem 0.3rem;
    border-radius: 3px;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    font-family: ui-monospace, monospace;
    transition: background 0.1s;
  }
  .wl-row:hover { background: rgba(255, 255, 255, 0.04); }
  .wl-sym {
    font-size: 0.72rem;
    font-weight: 700;
    color: #e2ecff;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wl-pnl {
    font-size: 0.75rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .wl-pnl-up   { color: #4ade80; }
  .wl-pnl-down { color: #f87171; }
  .wl-pct {
    font-size: 0.6rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  /* ── Row 3: Market news strip ───────────────────────────────────── */
  .dash-row3 {
    margin-bottom: 0.6rem;
    padding: 0.55rem 0.75rem 0.6rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }
  .row3-header {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    margin-bottom: 0.3rem;
  }

  /* ── P&L Analysis collapsible ────────────────────────────────────── */
  /* Summary bar carries the same card chrome as every other section
     even when collapsed, so the surface reads as a closed accordion
     panel — not a hairline. Hover lifts the border to amber as before. */
  .dash-pnl-summary {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .dash-pnl-summary::-webkit-details-marker { display: none; }
  .dash-pnl-summary:hover {
    border-color: rgba(251, 191, 36, 0.50);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  /* Open state — amber accent stays so the operator knows which
     section is currently exposing its inner content. */
  .dash-pnl-details[open] > .dash-pnl-summary {
    border-color: rgba(251, 191, 36, 0.65);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-pnl-toggle {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
  }
  .dash-pnl-body {
    margin-top: 0.4rem;
  }

  /* Demo banner */
  .demo-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.45rem 0.75rem;
    margin-bottom: 0.75rem;
    border-radius: 4px;
    background: rgba(168,85,247,0.15);
    border: 1px solid rgba(168,85,247,0.35);
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
  }
  .demo-banner-text { color: #d8b4fe; flex: 1; }
  .demo-banner-text strong { color: #e9d5ff; font-weight: 700; }
  .demo-banner-link {
    color: #c084fc;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-weight: 600;
  }
  .demo-banner-link:hover { color: #e9d5ff; }
  .demo-banner-sep { color: rgba(168,85,247,0.45); margin: 0 0.35rem; }
  .demo-banner-close {
    flex-shrink: 0;
    background: none;
    border: none;
    color: rgba(168,85,247,0.6);
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    padding: 0 0.15rem;
    transition: color 0.1s;
  }
  .demo-banner-close:hover { color: #c084fc; }
</style>
