<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { clientTimestamp, nowStamp, visibleInterval } from '$lib/stores';
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

  // ag-Grid module registration — idempotent across re-mounts.
  ModuleRegistry.registerModules([AllCommunityModule]);

  // ag-Grid valueFormatter wrappers — single source of truth for
  // how numbers render across the dashboard's grids: en-IN grouping,
  // no `+` prefix on positives (direction is colour-coded), '—'
  // for null. Mirrors MarketPulse / PerformancePage conventions so
  // a glance across pages reads the same.
  const _agNumFmt   = ({ value }) => value == null ? '—' : priceFmt(value);
  const _agAggFmt   = ({ value }) => value == null ? '—' : aggCompact(value);
  const _agPctFmt   = ({ value }) => value == null ? '—' : `${pctFmt(value)}%`;
  const _agUtilFmt  = ({ value }) => value == null ? '—' : `${Math.round(value * 100)}%`;
  const _numericHdr = 'ag-right-aligned-header';

  // Direction-coloured numeric cell — reuse the algo theme's
  // existing pnl-gain / pnl-loss / pnl-zero classes (defined in
  // app.css with `!important` to win against the theme's row
  // colour). Same idiom as MarketPulse + PerformancePage so the
  // visual treatment matches across every algo page.
  const _agDirCell = (p) =>
    `ag-right-aligned-cell ${p.value > 0 ? 'pnl-gain' : p.value < 0 ? 'pnl-loss' : 'pnl-zero'}`;

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

  // Operator-facing log declutter: default to agent_fire ONLY so the
  // expanded log is a thin chronological list of "what fired".
  // Operator can flip the chip to ALSO include action successes /
  // errors when they want the deeper "what did the fire DO" trace.
  // (Collapse state itself is owned by the CollapseButton via
  // _colAgent / _colPnl + localStorage.)
  let _agentLogShowActions = $state(false);
  const _agentLogKinds = $derived(
    _agentLogShowActions
      ? ['agent_fire', 'agent_action_success', 'agent_action_error']
      : ['agent_fire'],
  );


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

  // Per-card account filters — each AccountMultiSelect on the
  // dashboard binds to its OWN state, so adjusting one card's
  // scope doesn't cascade into the others. Empty array = all
  // accounts (no filter). Persisted to sessionStorage under
  // separate keys so the operator's per-card intent survives a
  // tab refresh.
  let _eqAccounts  = $state(/** @type {string[]} */ ([]));   // Equity card
  let _winAccounts = $state(/** @type {string[]} */ ([]));   // Top Winners
  let _losAccounts = $state(/** @type {string[]} */ ([]));   // Top Losers

  // Broker-registry-loaded accounts — populated by _fetchConn() (the
  // same /admin/brokers fan-out the connection-health chip uses).
  // Unioned into _availableAccounts so the per-card Account
  // MultiSelects list every loaded account including ones with no
  // positions / holdings yet (e.g. freshly-added Dhan / Groww rows).
  let _knownBrokerAccounts = $state(/** @type {string[]} */ ([]));

  // Derived list of distinct accounts seen in current positions +
  // holdings + broker registry. Sorted ascending. Empty fallback when
  // fetchBrokerAccounts 403s (non-admin session) — picker still works
  // off the rows-derived set.
  const _availableAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    for (const a of _knownBrokerAccounts) set.add(String(a));
    return [...set].sort();
  });

  // Apply a per-card account filter to a row list. Empty filter
  // (default) = pass-through. Generic helper so each card can pass
  // its own state and share the filtering logic.
  function _accountFilter(rows, accountsFilter) {
    if (!accountsFilter.length) return rows;
    const allow = new Set(accountsFilter);
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
  // Capital + Equity merged into one tabbed card so they share half
  // the page width on desktop (with the intraday equity curve filling
  // the right half). Default: Capital — that's the trader's first
  // "can I take risk?" glance.
  let _capEqTab = $state(/** @type {'capital'|'equity'} */ ('capital'));

  // Row 1 right slot — tabbed card: Intraday (SVG curve) vs Performance
  // (PnlAnalysis component). Default Intraday — that's the live "what
  // is the book doing right now" view; Performance is the deeper
  // historical drill-down sitting one click away.
  let _chartTab = $state(/** @type {'intraday'|'performance'} */ ('intraday'));
  // Bindable mirror of PnlAnalysis.hasData — flips to false once
  // /pnl-benchmarks confirms zero dates. Default true so the
  // auto-collapse effect below doesn't fire during the initial
  // loading window before the fetch resolves.
  let _pnlHasData = $state(true);

  // Auto-collapse the Intraday/Performance card when BOTH tabs are
  // confirmed empty (no intraday equity points AND PnlAnalysis returned
  // zero dates). One-shot per emptiness event: once latched, the
  // operator can manually expand without us re-collapsing them. The
  // latch resets when data appears, so a later empty-state recurrence
  // (broker reconnect → data cleared) re-triggers cleanly.
  let _autoCollapseLatched = $state(false);
  $effect(() => {
    const intradayEmpty = _equityPoints.length === 0;
    const pnlEmpty      = !_pnlHasData;
    if (!intradayEmpty || _pnlHasData) {
      _autoCollapseLatched = false;
      return;
    }
    if (pnlEmpty && intradayEmpty && !_autoCollapseLatched) {
      _colEquityCurve = true;
      _autoCollapseLatched = true;
    }
  });

  // Per-card fullscreen toggles. Each card binds its own slot —
  // multiple cards can theoretically open at once but only one is
  // visually on top (last-clicked wins via DOM order).
  let _fsEquityCurve = $state(false);
  let _fsCapital     = $state(false);
  let _fsEquity      = $state(false);
  let _fsWinners     = $state(false);
  let _fsLosers      = $state(false);
  let _fsNews        = $state(false);
  // _fsPnl / _colPnl retired with the standalone P&L Analysis card —
  // PnlAnalysis now lives inside the Intraday/Performance tabbed card
  // (shares _fsEquityCurve + _colEquityCurve).
  let _fsAgent       = $state(false);

  // Per-card collapse toggles. CollapseButton hydrates each from
  // localStorage on mount (keyed by user + cardId), so these
  // initial `false` values are placeholders only — the component
  // overwrites after the first onMount tick.
  let _colEquityCurve = $state(false);
  let _colCapital     = $state(false);
  let _colEquity      = $state(false);
  let _colWinners     = $state(false);
  let _colLosers      = $state(false);
  let _colNews        = $state(false);
  // Agent activity is a heavyweight card — start collapsed by default
  // so the dashboard's first paint stays light. CollapseButton overrides
  // from localStorage on mount if the user previously expanded it.
  let _colAgent       = $state(true);

  // ── ag-Grid bindings for the dashboard cards ────────────────────
  // Each card mounts its own grid imperatively via bind:this + a
  // mount-time $effect. Once mounted, rowData updates flow through
  // a separate reactive $effect that hits setGridOption('rowData').
  // Pattern mirrors MarketPulse for consistency.
  // Grid element bindings — MUST be $state so the `$effect` that
  // mounts the grid re-triggers when `bind:this` assigns. In Svelte 5
  // a plain `let` is NOT reactive; bind:this would set it but the
  // effect would only ever run once with the initial undefined value
  // and the grid would never mount.
  let _fundsEl     = $state(/** @type {HTMLDivElement|null} */ (null));
  let _marginEl    = $state(/** @type {HTMLDivElement|null} */ (null));
  let _winEl       = $state(/** @type {HTMLDivElement|null} */ (null));
  let _losEl       = $state(/** @type {HTMLDivElement|null} */ (null));
  let _eqPosEl     = $state(/** @type {HTMLDivElement|null} */ (null));
  let _eqHoldEl    = $state(/** @type {HTMLDivElement|null} */ (null));
  let _fundsGrid, _marginGrid, _winGrid, _losGrid, _eqPosGrid, _eqHoldGrid;
  let _fundsReady  = $state(false);
  let _marginReady = $state(false);
  let _winReady    = $state(false);
  let _losReady    = $state(false);
  let _eqPosReady  = $state(false);
  let _eqHoldReady = $state(false);

  // Click-to-open SymbolPanel from W/L grid rows.
  function _openSymbol(sym) {
    const s = String(sym || '').trim();
    if (!s) return;
    _ticketProps = {
      symbol: s,
      defaultTab: 'chart',
      onClose:  () => { _ticketProps = null; },
      onSubmit: () => { _ticketProps = null; },
    };
  }

  // Per-account summary derivations — same shape MarketPulse
  // builds internally, but computed here from the already-loaded
  // _positions / _holdings. No extra fetches; the HTML tables
  // below render directly from these derivations.
  //
  // Day-change for positions comes from row.day_change_val (today's
  // P&L; previously used row.pnl which is life-to-date and was
  // incorrectly labelled "Day P&L"). For holdings it's also
  // row.day_change_val per the HoldingRow schema; legacy fallbacks
  // for `day_change` / `day_change_pct_amount` are dead field names
  // that never exist on the row.
  /** @typedef {{account: string, day_pnl: number, pnl: number, inv_val: number, cur_val: number}} SumRow */

  // Pre-seed byAcct with every broker-registry account so the summary
  // grid lists ALL loaded accounts including ones with 0 positions /
  // 0 holdings (e.g. freshly-added Dhan / Groww rows). Without this,
  // empty accounts silently vanish from the summary and the operator
  // can't tell at a glance that the row is loaded.
  function _seedSummaryByAcct(filter) {
    /** @type {Record<string, SumRow>} */
    const byAcct = {};
    const allowSet = filter && filter.length > 0 ? new Set(filter) : null;
    for (const a of _knownBrokerAccounts) {
      if (!a) continue;
      if (allowSet && !allowSet.has(a)) continue;
      byAcct[String(a)] = { account: String(a), day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    }
    return byAcct;
  }

  const _positionsSummary = $derived.by(() => {
    const byAcct = _seedSummaryByAcct(_eqAccounts);
    // Equity card uses its own _eqAccounts state — independent of
    // the W/L cards' filters.
    for (const r of _accountFilter(_positions, _eqAccounts)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
      byAcct[a].pnl     += Number(r.pnl) || 0;
    }
    return Object.values(byAcct).sort((a, b) => a.account.localeCompare(b.account));
  });

  const _holdingsSummary = $derived.by(() => {
    const byAcct = _seedSummaryByAcct(_eqAccounts);
    for (const r of _accountFilter(_holdings, _eqAccounts)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
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
  // /quote/batch response shape: {refreshed_at, items: [BatchQuoteRow]}.
  // We index by "${exchange}:${tradingsymbol}" so universe queries can
  // pluck back out by key. Polled every 60 s during active viewing.
  /** @type {Record<string, {ltp:number, close:number, change_pct:number, ohlc?:{close?:number}, previous_close?:number}>} */
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
      const ltp = Number(q.ltp) || 0;
      // Backend already computes change_pct; fall back to manual
      // calc only when zero / missing (defensive against partial fills).
      let pct = Number(q.change_pct);
      if (!isFinite(pct) || pct === 0) {
        const close = Number(q.ohlc?.close ?? q.close ?? q.previous_close) || 0;
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

  // Aggregate positions by parsed underlying. Kept for any future
  // "your underlying exposure" surface (e.g. an extra W/L bucket)
  // — Top Winners/Losers Underlying now reads from _marketRows().
  // Takes an account filter as input so the caller picks the scope.
  function _positionsByUnderlying(accounts) {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number}>} */
    const byU = new Map();
    for (const p of _accountFilter(_positions, accounts)) {
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
  }

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
  //
  // day_pct is a per-symbol global, not additive — it's the same
  // (last_price - close_price) / close_price * 100 for every account
  // holding the symbol, so we take the first non-zero value and let
  // every other account's row confirm it. Without this, _toWlRow used
  // to derive pct = pnl_today / inv_val (= today's rupee move / cost
  // basis), which conflates "today's % move" with "average per-rupee-
  // invested gain" — wildly wrong when one account holds a tiny
  // position and another holds a huge one. Operator saw IFCI's 4.98%
  // day move reported as 13.58% via that broken formula.
  //
  // LTP is a per-symbol global value too — first non-zero ltp wins.
  function _aggregateBySymbol(rows) {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}>} */
    const bySym = new Map();
    for (const r of rows) {
      const sym = r.symbol;
      if (!sym) continue;
      const cur = bySym.get(sym) ?? { symbol: sym, pnl: 0, inv_val: 0, ltp: 0, day_pct: null };
      cur.pnl     += Number(r.pnl) || 0;
      cur.inv_val += Number(r.inv_val) || 0;
      if (!cur.ltp && r.ltp) cur.ltp = Number(r.ltp) || 0;
      if (cur.day_pct == null && r.day_pct != null) {
        cur.day_pct = Number(r.day_pct);
      }
      bySym.set(sym, cur);
    }
    return Array.from(bySym.values());
  }

  // Holdings, with optional class filter (midcap / smallcap / null=all)
  // + per-card account filter. Aggregated by symbol so a stock held
  // in N accounts appears as one row with summed day P&L + cost basis.
  function _holdingsFor(cls, accounts) {
    /** @type {{symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}[]} */
    const raw = [];
    for (const h of _accountFilter(_holdings, accounts)) {
      const sym = String(h.tradingsymbol || h.symbol || '');
      const pnl = Number(h.day_change_val ?? 0);
      if (!sym) continue;
      if (cls) {
        const c = classifyByIndex(sym);
        if (c !== cls) continue;
      }
      raw.push({
        symbol: sym,
        pnl,
        inv_val: Number(h.inv_val ?? 0),
        ltp:     Number(h.last_price ?? h.ltp ?? 0),
        // Carry the broker's per-row day_change_percentage so the
        // aggregate keeps the correct (last-close)/close * 100 value.
        // Without this, Winners/Losers fell back to a wrong derived
        // formula (pnl/inv_val) — see _aggregateBySymbol.
        day_pct: h.day_change_percentage != null
                 ? Number(h.day_change_percentage)
                 : null,
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
  // across accounts (same reason as holdings). Takes a per-card
  // account filter so Winners + Losers cards can scope independently.
  function _positionsRowsFor(accounts) {
    /** @type {{symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}[]} */
    const raw = [];
    for (const p of _accountFilter(_positions, accounts)) {
      const sym = String(p.tradingsymbol || p.symbol || '');
      const pnl = Number(p.pnl) || 0;
      if (!sym) continue;
      raw.push({
        symbol: sym,
        pnl,
        inv_val: 0,
        ltp: Number(p.last_price ?? p.ltp ?? 0),
        // Same fix as _holdingsFor — carry the broker's day-change %
        // so _toWlRow can use it directly instead of falling back to
        // a wrong derived formula.
        day_pct: p.day_change_percentage != null
                 ? Number(p.day_change_percentage)
                 : null,
      });
    }
    return _aggregateBySymbol(raw)
      .filter(r => r.pnl !== 0)
      .map(r => ({ ...r, kind: 'user' }));
  }

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
  //     positions/holdings + ANY account filter.
  //   - HOLDINGS / POSITIONS — user-scoped, honour the CARD'S OWN
  //     _winAccounts / _losAccounts state (independent per card).
  const _winnerBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,                 'win'),
    _bucket('MIDCAP',            _midcapRows,                       'win'),
    _bucket('SMALLCAP',          _smlcapRows,                       'win'),
    _bucket('HOLDINGS',          _holdingsFor(null, _winAccounts),  'win'),
    _bucket('POSITIONS',         _positionsRowsFor(_winAccounts),   'win'),
  ]);

  const _loserBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,                 'lose'),
    _bucket('MIDCAP',            _midcapRows,                       'lose'),
    _bucket('SMALLCAP',          _smlcapRows,                       'lose'),
    _bucket('HOLDINGS',          _holdingsFor(null, _losAccounts),  'lose'),
    _bucket('POSITIONS',         _positionsRowsFor(_losAccounts),   'lose'),
  ]);

  // Show the categorised section only when SOMETHING is movable. Hides
  // cleanly outside trading hours when every bucket is empty.
  const _hasWinners = $derived(_winnerBuckets.some(b => b.rows.length > 0));
  const _hasLosers  = $derived(_loserBuckets.some(b => b.rows.length > 0));

  // Normalise a bucket row to the shape the W/L ag-Grid expects:
  // {symbol, ltp, pct, pnl_abs?}. Market rows already carry pnl=pct;
  // user rows carry pnl=rupees, so we derive pct from (pnl / inv_val).
  // Adds `pnl_abs` for user rows so a future column can show ₹ alongside
  // the %.
  function _toWlRow(r) {
    if (r.kind === 'market') {
      return { symbol: r.symbol, ltp: r.ltp || null, pct: r.pnl, pnl_abs: null };
    }
    // user row: prefer the broker-supplied day_change_percentage (the
    // canonical (last - close) / close × 100). The legacy fallback
    // (pnl / inv_val × 100) was wrong on multiple axes:
    //   - it mixed up "today's % move" with "today's rupee move as a
    //     fraction of cost basis" → IFCI's 4.98 % real day move
    //     reported as 13.58 %
    //   - it broke entirely for positions where inv_val=0 (always)
    const pct = r.day_pct != null
      ? r.day_pct
      : (r.inv_val > 0 ? (r.pnl / r.inv_val) * 100 : null);
    return { symbol: r.symbol, ltp: r.ltp || null, pct, pnl_abs: r.pnl };
  }

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
    // Use `day_pnl` (today's P&L) — NOT `cum_pnl` (lifetime P&L). The
    // lifetime number is ₹4-5M and dwarfs the ₹40k of intraday
    // movement; forcing zero into a 4.5M-scale chart visually flattens
    // the entire intraday curve into a horizontal line.
    //
    // day_pnl is centred near zero with small absolute swings — the
    // chart actually animates per-tick. Hover tooltip still shows
    // both day and cum so the operator can read the bigger context.
    const vals = _equityPoints.map(p => p.day_pnl);
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
    return _equityPoints.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.day_pnl).toFixed(1)}`).join(' ');
  });

  const _eqAreaPath = $derived.by(() => {
    if (!_equityPoints.length || !_equityDomain) return '';
    const pts = _equityPoints;
    const zero = _eqY(0);
    const first = `${_eqX(pts[0].ts).toFixed(1)},${zero}`;
    const last  = `${_eqX(pts[pts.length - 1].ts).toFixed(1)},${zero}`;
    const line  = pts.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.day_pnl).toFixed(1)}`).join(' L ');
    return `M ${first} L ${line} L ${last} Z`;
  });

  const _eqZeroY = $derived(_equityDomain ? _eqY(0) : null);

  const _eqPositive = $derived(
    _equityPoints.length ? _equityPoints[_equityPoints.length - 1].day_pnl >= 0 : true
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
    _hoverY   = parseFloat(_eqY(_equityPoints[best].day_pnl).toFixed(1));
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
      // Accept both wrapped {points: [...]} (current backend) and bare
      // array (defensive against a future shape change).
      const pts = Array.isArray(res) ? res : (res?.points ?? []);
      _equityPoints = pts;
    } catch (_) { /* leave stale */ }
  }

  // Standalone freshness stamp for the equity curve was retired when
  // the Capital/Equity card lost its refresh chip — there's no UI
  // surface for it anymore. The chart-refresh cadence is still 15 s
  // via visibleInterval below.
  let _equityPollStop;

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
    } catch (_) { /* leave _margins / _funds at last-good; banner stays stale-silent */ }
  }

  async function _fetchConn() {
    try {
      const accounts = await fetchBrokerAccounts();
      if (!Array.isArray(accounts)) return;
      _conn = {
        total:  accounts.length,
        loaded: accounts.filter(a => a.loaded).length,
      };
      // Capture the broker-registry account codes so the per-card
      // Account MultiSelects can list accounts that have no positions
      // / holdings yet (e.g., a freshly-added Dhan or Groww row before
      // any trades land). Same union logic /pulse uses.
      _knownBrokerAccounts = accounts
        .filter((a) => a?.account)
        .map((a) => String(a.account));
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

      // Sum today's P&L + total capital base across BOTH books.
      //   - positions: day P&L = day_change_val (NOT pnl — that's
      //     life-to-date and inflates the hero number when used as
      //     "today's" change). Capital deployed ≈ Σ |avg_price × qty|
      //     (notional cost basis — premium paid for longs, premium
      //     received for shorts, both as positive value).
      //   - holdings: day P&L = day_change_val. Capital = inv_val.
      // Earlier `_startingNav = holdings.inv_val` only, which ignored
      // positions capital — `_todayPct = dayPnl / inv_val` inflated
      // the displayed % whenever F&O positions were open.
      let dayPnl = 0;
      let invVal  = 0;
      for (const p of _positions) {
        dayPnl += Number(p.day_change_val) || 0;
        invVal += Math.abs(Number(p.average_price) * Number(p.quantity)) || 0;
      }
      for (const h of _holdings) {
        dayPnl += Number(h.day_change_val) || 0;
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

      // Parallel: margins + conn health + NIFTY quote.
      // _fetchEquity intentionally NOT in this batch — it has its
      // own independent 15 s poll wired in onMount so a hero-batch
      // failure can't stall the equity-curve refresh cycle.
      await Promise.all([
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
    // (P&L + Agent collapse state now owned by CollapseButton via
    // its own per-user localStorage key — the old dash.pnlOpen
    // restore is retired.)
    // Restore per-card account filters from sessionStorage. Each
    // card persists under its own key so the operator's per-card
    // intent survives a tab refresh. Wrapped in try/catch since
    // the stored account codes may no longer exist on this server
    // (operator switched broker accounts) — silently fall back to
    // all-accounts on parse error.
    //
    // Important: defer restoration until AFTER the broker registry
    // load completes. We compare the stored filter against the
    // currently-loaded broker accounts and reset to "all" when a
    // new broker account has appeared since the operator last set
    // the filter. Otherwise a stale ['ZG0790','ZJ6294'] filter
    // from before Dhan / Groww accounts were added silently keeps
    // them hidden — operator sees an empty Dhan row instead of
    // the obvious "your new account is here, click to include".
    function _restore(key, /** @type {(v:string[])=>void} */ setter) {
      try {
        const cached = sessionStorage.getItem(key);
        if (!cached) return;
        const stored = JSON.parse(cached);
        if (!Array.isArray(stored) || stored.length === 0) return;
        // _knownBrokerAccounts populates via _fetchConn — may not be
        // ready yet on first restore. Fall back to "use stored as
        // is" when registry is empty; the watcher below re-runs
        // once the registry lands.
        const known = _knownBrokerAccounts;
        if (known.length === 0) {
          setter(stored);
          return;
        }
        // Drop unknown account codes (broker removed).
        const valid = stored.filter(a => known.includes(a));
        // If the operator picked a subset, but a NEW broker account
        // is now loaded that isn't covered, reset → "all" so the
        // new account is visible by default. Operator can re-narrow.
        const missingNew = known.some(a => !stored.includes(a));
        if (missingNew) {
          setter([]);            // = show all
          sessionStorage.removeItem(key);
        } else {
          setter(valid);
        }
      } catch (_) { setter([]); }
    }
    _restore('dash.eqAccounts',  v => _eqAccounts  = v);
    _restore('dash.winAccounts', v => _winAccounts = v);
    _restore('dash.losAccounts', v => _losAccounts = v);
    // Re-run restore once the broker registry lands (_knownBrokerAccounts
    // starts empty and populates async via _fetchConn). $effect tracks
    // the dep and fires once the array transitions from empty → loaded.
    $effect(() => {
      if (_knownBrokerAccounts.length === 0) return;
      _restore('dash.eqAccounts',  v => _eqAccounts  = v);
      _restore('dash.winAccounts', v => _winAccounts = v);
      _restore('dash.losAccounts', v => _losAccounts = v);
    });
    loadHero();
    // Match the equity-curve cadence (15 s). The earlier 30 s rate
    // left the Capital card visibly stale next to the chart that
    // ticked twice between hero refreshes. Backend cycle is 60 s
    // anyway so 15 s polling is comfortably within the freshness
    // window without hammering the broker.
    _heroTeardown = visibleInterval(loadHero, 15000);
    // Equity-curve polling — independent of loadHero so an upstream
    // sub-fetch failure (positions / holdings / events) can't stall
    // the chart's refresh cycle. Same 15 s cadence — backend buffer
    // appends a new point every ~1 min and the chart should reflect
    // it within one frame of arrival.
    _fetchEquity();
    _equityPollStop = visibleInterval(_fetchEquity, 15000);
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
      // Backend trims keys[] at 300; F&O + Midcap + Smallcap totals
      // ~300, so we batch in two halves to avoid silent truncation.
      const allKeys = [...new Set([
        ...FO_QUOTE_KEYS, ...MIDCAP_QUOTE_KEYS, ...SMLCAP_QUOTE_KEYS,
      ])];
      const HALF = Math.ceil(allKeys.length / 2);
      const batches = [allKeys.slice(0, HALF), allKeys.slice(HALF)];
      const responses = await Promise.all(batches.map(b => batchQuote(b)));
      // BatchQuoteResponse is `{refreshed_at, items: [BatchQuoteRow]}`
      // — each row has its OWN {exchange, tradingsymbol} pair which
      // we re-key as "exchange:tradingsymbol" to match the universe
      // key arrays. Reassign the whole map so disappearing symbols
      // clear out (avoids stale rows in W/L buckets).
      /** @type {Record<string, any>} */
      const map = {};
      for (const r of responses) {
        for (const it of (r?.items ?? [])) {
          if (!it?.exchange || !it?.tradingsymbol) continue;
          map[`${it.exchange}:${it.tradingsymbol}`] = it;
        }
      }
      _marketQuotes = map;
    } catch (_) { /* transient — leave previous map up */ }
  }

  // Persist per-card filter changes — sessionStorage so the intent
  // survives a tab refresh but resets per session (operators don't
  // usually carry the same filter across days). One effect per
  // card so a change in one doesn't trigger writes for the others.
  $effect(() => {
    try { sessionStorage.setItem('dash.eqAccounts',  JSON.stringify(_eqAccounts));  } catch (_) {}
  });
  $effect(() => {
    try { sessionStorage.setItem('dash.winAccounts', JSON.stringify(_winAccounts)); } catch (_) {}
  });
  $effect(() => {
    try { sessionStorage.setItem('dash.losAccounts', JSON.stringify(_losAccounts)); } catch (_) {}
  });
  // ── ag-Grid mounts ────────────────────────────────────────────────
  // Each $effect runs once when the bound element appears in the DOM
  // (bind:this flips from null → HTMLDivElement). Subsequent
  // rowData updates flow through separate effects further down.
  //
  // Default colDef: resizable, sortable, non-movable, no header menu.
  // domLayout: 'autoHeight' so the card grows with its rows up to the
  // card's flexbox cap — keeps the grid compact when only 2 rows are
  // present and avoids reserving 250 px of empty space.
  /** @type {any} */
  const _baseGridOpts = {
    theme: 'legacy',
    defaultColDef: {
      resizable: true, sortable: true, suppressMovable: true,
      suppressHeaderMenuButton: true,
    },
    sortingOrder: ['asc', 'desc', null],
    headerHeight: 26,
    rowHeight: 26,
  };

  $effect(() => {
    if (!_fundsEl || _fundsGrid) return;
    _fundsGrid = createGrid(_fundsEl, {
      ..._baseGridOpts,
      // Tag the pinned-bottom TOTAL row with the algo theme's
      // `totals-row` class so it inherits the amber accent +
      // background tint defined in app.css. Mirrors the PerformancePage
      // funds-grid behaviour.
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account codes are short (ZG0790 / ZJ6294 / TOTAL — 6 chars).
        // 46 px is the tightest fit that still shows the 6-char code
        // + 4 px cell padding without truncation. Shrunk 20 % from
        // the earlier 58 px so numeric columns get more flex room.
        { field: 'account', headerName: 'Account', minWidth: 46, pinned: 'left',
          cellClass: 'ag-col-fill' },
        { field: 'cash', headerName: 'Cash', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'collateral', headerName: 'Collat', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'avail_margin', headerName: 'Avail', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'used_margin', headerName: 'Used', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size:0.65rem;color:#7e97b8">No fund data</span>',
    });
    _fundsReady = true;
  });

  $effect(() => {
    if (!_marginEl || _marginGrid) return;
    _marginGrid = createGrid(_marginEl, {
      ..._baseGridOpts,
      // Pinned-bottom TOTAL row inherits the algo theme's totals-row
      // amber-accent styling — mirrors the Funds grid pattern.
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account codes are short (ZG0790 / ZJ6294 / TOTAL — 6 chars).
        // 46 px is the tightest fit that still shows the 6-char code
        // + 4 px cell padding without truncation. Shrunk 20 % from
        // the earlier 58 px so numeric columns get more flex room.
        { field: 'account', headerName: 'Account', minWidth: 46, pinned: 'left',
          cellClass: 'ag-col-fill' },
        { field: 'used', headerName: 'Used', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'avail', headerName: 'Avail', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'util_pct', headerName: 'Util', minWidth: 56, flex: 0.7,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: (p) => {
            // Util % colour ramp using the algo theme's pnl-* classes
            // — green when well under-utilised, red when close to a
            // margin call. Same colour family operators see on every
            // other algo grid for "is this number good or bad".
            const v = Number(p.value) || 0;
            const cls = v >= 0.85 ? 'pnl-loss'
                      : v >= 0.70 ? 'util-warn'
                      : v >= 0.50 ? 'util-mild'
                      : 'pnl-gain';
            return `ag-right-aligned-cell ${cls}`;
          },
          valueFormatter: _agUtilFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size:0.65rem;color:#7e97b8">No accounts connected</span>',
    });
    _marginReady = true;
  });

  // W/L grid factory — shared shape, separate instances per side.
  // Direction determines the colour of the % cell (up=green/down=red).
  function _makeWlGrid(el, kind /* 'win' | 'lose' */) {
    return createGrid(el, {
      ..._baseGridOpts,
      columnDefs: [
        // Symbol column iteratively shrunk: 110 → 72 (−35 %) → 65
        // (further −10 %). 65 px still fits the longest visible
        // F&O underlying like 'BANKNIFTY' (9 chars) at the current
        // font-size; flex:2 lets the column expand when there's room.
        { field: 'symbol', headerName: 'Symbol', minWidth: 65, flex: 2,
          pinned: 'left', cellClass: 'ag-col-fill ag-col-sym',
          sortable: true },
        { field: 'ltp', headerName: 'LTP', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agNumFmt },
        { field: 'pct', headerName: 'Δ %', minWidth: 64, flex: 0.9,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: () => `ag-right-aligned-cell ${kind === 'win' ? 'pnl-gain' : 'pnl-loss'}`,
          valueFormatter: ({ value }) =>
            value == null ? '—'
            : (value > 0 ? '+' : '') + pctFmt(value) + '%',
          sort: kind === 'win' ? 'desc' : 'asc' },
      ],
      rowData: [],
      // Normal layout (not autoHeight) — the wrapper carries an
      // explicit height; ag-Grid scrolls internally. autoHeight +
      // overflow:auto on the container had ag-Grid mis-measuring
      // and rendering invisible headers on the W/L cards.
      domLayout: 'normal',
      onRowClicked: (ev) => _openSymbol(ev.data?.symbol),
      overlayNoRowsTemplate:
        `<span style="font-size:0.65rem;color:#7e97b8">No ${kind === 'win' ? 'winners' : 'losers'} in this bucket</span>`,
    });
  }

  $effect(() => {
    if (!_winEl || _winGrid) return;
    _winGrid = _makeWlGrid(_winEl, 'win');
    _winReady = true;
  });
  $effect(() => {
    if (!_losEl || _losGrid) return;
    _losGrid = _makeWlGrid(_losEl, 'lose');
    _losReady = true;
  });

  // Equity card — Positions Summary + Holdings Summary grids.
  // Per-account aggregates with TOTAL pinned at bottom. Same algo
  // theme classes (pnl-gain / pnl-loss / pnl-zero / totals-row /
  // ag-col-fill) as the other dashboard grids.
  $effect(() => {
    if (!_eqPosEl || _eqPosGrid) return;
    _eqPosGrid = createGrid(_eqPosEl, {
      ..._baseGridOpts,
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account codes are short (ZG0790 / ZJ6294 / TOTAL — 6 chars).
        // 46 px is the tightest fit that still shows the 6-char code
        // + 4 px cell padding without truncation. Shrunk 20 % from
        // the earlier 58 px so numeric columns get more flex room.
        { field: 'account', headerName: 'Account', minWidth: 46, pinned: 'left',
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl', headerName: 'Day P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _agDirCell, valueFormatter: _agNumFmt },
        { field: 'pnl', headerName: 'Open P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _agDirCell, valueFormatter: _agNumFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size:0.65rem;color:#7e97b8">No open positions</span>',
    });
    _eqPosReady = true;
  });

  $effect(() => {
    if (!_eqHoldEl || _eqHoldGrid) return;
    _eqHoldGrid = createGrid(_eqHoldEl, {
      ..._baseGridOpts,
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account codes are short (ZG0790 / ZJ6294 / TOTAL — 6 chars).
        // 46 px is the tightest fit that still shows the 6-char code
        // + 4 px cell padding without truncation. Shrunk 20 % from
        // the earlier 58 px so numeric columns get more flex room.
        { field: 'account', headerName: 'Account', minWidth: 46, pinned: 'left',
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl', headerName: 'Day P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _agDirCell, valueFormatter: _agNumFmt },
        { field: 'pnl', headerName: 'Open P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _agDirCell, valueFormatter: _agNumFmt },
        { field: 'cur_val', headerName: 'Cur Val', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell', valueFormatter: _agAggFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size:0.65rem;color:#7e97b8">No holdings</span>',
    });
    _eqHoldReady = true;
  });

  // Row-data updates flow through here. Each $effect tracks just the
  // derivation it cares about so unrelated state changes don't churn
  // every grid.

  // Funds grid — body + TOTAL pinned at bottom.
  const _fundsBody = $derived(_funds.map(r => ({
    account:      r.account,
    cash:         Number(r.cash) || 0,
    collateral:   Number(r.collateral) || 0,
    avail_margin: Number(r.avail_margin ?? r.available_margin) || 0,
    used_margin:  Number(r.used_margin) || 0,
  })));
  const _fundsTotal = $derived([{
    account:      'TOTAL',
    cash:         _fundsBody.reduce((s, r) => s + r.cash, 0),
    collateral:   _fundsBody.reduce((s, r) => s + r.collateral, 0),
    avail_margin: _fundsBody.reduce((s, r) => s + r.avail_margin, 0),
    used_margin:  _fundsBody.reduce((s, r) => s + r.used_margin, 0),
  }]);
  $effect(() => {
    if (!_fundsReady || !_fundsGrid) return;
    _fundsGrid.setGridOption('rowData', _fundsBody);
    _fundsGrid.setGridOption('pinnedBottomRowData', _fundsTotal);
  });

  // Margin grid — same shape as the SVG donuts we retired, but as
  // a tabular view alongside Funds.
  const _marginRows = $derived(_margins.map(r => ({
    account:  r.account,
    used:     r.used,
    avail:    r.avail,
    util_pct: r.util_pct,
  })));
  // TOTAL row — sum used + avail across accounts, derive util %
  // from the totals (not an average of per-account ratios — that'd
  // double-weight small accounts). Pinned at the grid bottom.
  const _marginTotal = $derived.by(() => {
    const tu = _marginRows.reduce((s, r) => s + (Number(r.used) || 0), 0);
    const ta = _marginRows.reduce((s, r) => s + (Number(r.avail) || 0), 0);
    return [{
      account:  'TOTAL',
      used:     tu,
      avail:    ta,
      util_pct: (tu + ta) > 0 ? tu / (tu + ta) : 0,
    }];
  });
  $effect(() => {
    if (!_marginReady || !_marginGrid) return;
    _marginGrid.setGridOption('rowData', _marginRows);
    _marginGrid.setGridOption('pinnedBottomRowData',
      _marginRows.length > 0 ? _marginTotal : []);
  });

  // W/L grids — active tab's bucket → ag-Grid rows.
  const _winRowsAg = $derived.by(() => {
    const b = _winnerBuckets.find(b => b.label === _winTabLabel(_winTab));
    return (b?.rows ?? []).map(_toWlRow);
  });
  const _losRowsAg = $derived.by(() => {
    const b = _loserBuckets.find(b => b.label === _winTabLabel(_losTab));
    return (b?.rows ?? []).map(_toWlRow);
  });
  $effect(() => {
    if (!_winReady || !_winGrid) return;
    _winGrid.setGridOption('rowData', _winRowsAg);
  });
  $effect(() => {
    if (!_losReady || !_losGrid) return;
    _losGrid.setGridOption('rowData', _losRowsAg);
  });

  // Equity card — Positions Summary + Holdings Summary feeds.
  // Body rows from _positionsSummary / _holdingsSummary (already
  // account-filtered via _filterByAccount); TOTAL row from
  // _positionsTotal / _holdingsTotal is pinned at bottom.
  $effect(() => {
    if (!_eqPosReady || !_eqPosGrid) return;
    _eqPosGrid.setGridOption('rowData', _positionsSummary);
    _eqPosGrid.setGridOption('pinnedBottomRowData', [_positionsTotal]);
  });
  $effect(() => {
    if (!_eqHoldReady || !_eqHoldGrid) return;
    _eqHoldGrid.setGridOption('rowData', _holdingsSummary);
    _eqHoldGrid.setGridOption('pinnedBottomRowData', [_holdingsTotal]);
  });

  // ── Account-multiselect scope predicate ───────────────────────────
  // The shared _selectedAccounts filter applies only to the user-scoped
  // buckets (Holdings / Positions). Market-wide tabs ignore it. We
  // disable the MultiSelect on each W/L card when the active tab
  // doesn't honour the filter so the operator doesn't think the
  // picker is silently being applied.
  const _USER_TABS = new Set(['holdings', 'positions']);
  const _winAcctDisabled = $derived(!_USER_TABS.has(_winTab));
  const _losAcctDisabled = $derived(!_USER_TABS.has(_losTab));

  onDestroy(() => {
    _heroTeardown?.(); _stopMarketPoll?.(); _equityPollStop?.();
    _fundsGrid?.destroy();  _marginGrid?.destroy();
    _winGrid?.destroy();    _losGrid?.destroy();
    _eqPosGrid?.destroy();  _eqHoldGrid?.destroy();
  });

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
  <span class="algo-ts ml-auto">{$nowStamp}</span>
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

<!-- Row 1 (split): Capital+Equity tabbed card LEFT half, Intraday
     Equity Curve RIGHT half. Earlier the chart filled a full-width
     hero row and Capital+Equity sat below at 1:1. The split layout
     keeps the operator's three first-glance signals (cash, equity
     P&L, curve trajectory) on one screen — same as Bloomberg PRTU
     where the portfolio sidebar lives alongside the chart. Stacks
     on mobile. -->
<div class="dash-row1-split">

  <!-- LEFT: Capital | Equity tabbed card -->
  <section class="bucket-card cap-eq-tabbed"
    class:fs-card-on={_fsCapital || _fsEquity}
    class:is-collapsed={_colCapital && _colEquity}>
    <div class="bucket-header">
      <!-- Tab bar replaces the section label. Active tab carries
           the same amber underline + bg the W/L tabs use. -->
      <div class="cap-eq-tabs" role="tablist">
        <button type="button" role="tab"
          class="cap-eq-tab" class:cap-eq-tab-on={_capEqTab === 'capital'}
          aria-selected={_capEqTab === 'capital'}
          onclick={() => _capEqTab = 'capital'}>Capital</button>
        <button type="button" role="tab"
          class="cap-eq-tab" class:cap-eq-tab-on={_capEqTab === 'equity'}
          aria-selected={_capEqTab === 'equity'}
          onclick={() => _capEqTab = 'equity'}>Equity</button>
      </div>
      {#if _capEqTab === 'equity'}
        <AccountMultiSelect
          bind:value={_eqAccounts}
          options={_availableAccounts.map(a => ({ value: a, label: a }))} />
      {/if}
      <!-- Bind targets the active-tab's own state pair. Svelte 5
           doesn't permit ternary expressions inside `bind:`, so we
           split into two component instances guarded by {#if}. Each
           CollapseButton hydrates from its own localStorage key, so
           the operator's collapse intent persists per-tab. -->
      {#if _capEqTab === 'capital'}
        <CollapseButton bind:isCollapsed={_colCapital} cardId="capital" label="Capital" />
        <FullscreenButton bind:isFullscreen={_fsCapital} label="Capital" />
      {:else}
        <CollapseButton bind:isCollapsed={_colEquity} cardId="equity" label="Equity" />
        <FullscreenButton bind:isFullscreen={_fsEquity} label="Equity" />
      {/if}
    </div>

    <!-- BOTH panels stay mounted (hidden, not {#if}) so ag-Grid
         instances don't orphan when the operator flips tabs. -->
    <!-- Capital panel -->
    <div class="card-body" hidden={_capEqTab !== 'capital' || _colCapital}>
      {#if _marginRows.length > 0}
        <div class="bucket-subheader">Margin Utilisation</div>
      {/if}
      <div
        bind:this={_marginEl}
        class="ag-theme-algo dash-mini-grid"
        class:is-empty={_marginRows.length === 0}></div>

      {#if _fundsBody.length > 0}
        <div class="bucket-subheader bucket-subheader-spaced">Funds</div>
      {/if}
      <div
        bind:this={_fundsEl}
        class="ag-theme-algo dash-mini-grid"
        class:is-empty={_fundsBody.length === 0}></div>

      {#if _marginRows.length === 0 && _fundsBody.length === 0}
        <EmptyState message="No accounts connected" />
      {/if}
    </div>

    <!-- Equity panel -->
    <div class="card-body" hidden={_capEqTab !== 'equity' || _colEquity}>
      {#if _positionsSummary.length > 0}
        <div class="bucket-subheader">
          Positions
          <span class="eq-count">{_positionsCount}</span>
        </div>
      {/if}
      <div
        bind:this={_eqPosEl}
        class="ag-theme-algo dash-mini-grid"
        class:is-empty={_positionsSummary.length === 0}></div>

      {#if _holdingsSummary.length > 0}
        <div class="bucket-subheader bucket-subheader-spaced">
          Holdings
          <span class="eq-count">{_holdingsCount}</span>
        </div>
      {/if}
      <div
        bind:this={_eqHoldEl}
        class="ag-theme-algo dash-mini-grid"
        class:is-empty={_holdingsSummary.length === 0}></div>

      {#if _positionsSummary.length === 0 && _holdingsSummary.length === 0}
        <EmptyState message="No equity exposure" />
      {/if}
    </div>
  </section>

  <!-- RIGHT: Intraday / Performance tabbed card. Intraday surfaces
       today's cumulative P&L curve; Performance hosts the historical
       drill-down (PnlAnalysis component) one click away. Both panels
       stay mounted (hidden, not {#if}) so internal state — including
       PnlAnalysis filters + benchmark series — persists across tab
       flips. -->
  <section class="row1-col row1-col-chart"
    class:fs-card-on={_fsEquityCurve}
    class:is-collapsed={_colEquityCurve}>
    <div class="card-header-row">
      <div class="cap-eq-tabs" role="tablist">
        <button type="button" role="tab"
          class="cap-eq-tab" class:cap-eq-tab-on={_chartTab === 'intraday'}
          aria-selected={_chartTab === 'intraday'}
          onclick={() => _chartTab = 'intraday'}>Intraday</button>
        <button type="button" role="tab"
          class="cap-eq-tab" class:cap-eq-tab-on={_chartTab === 'performance'}
          aria-selected={_chartTab === 'performance'}
          onclick={() => _chartTab = 'performance'}>Performance</button>
      </div>
      <CollapseButton bind:isCollapsed={_colEquityCurve} cardId="equityCurve" label="Chart" />
      <FullscreenButton bind:isFullscreen={_fsEquityCurve} label="Chart" />
    </div>

    <!-- Intraday panel — SVG curve of today's cum P&L. -->
    <div class="card-body" hidden={_chartTab !== 'intraday' || _colEquityCurve}>
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
            stroke="rgba(200,216,240,0.18)" stroke-width="1"
            stroke-dasharray={frac === 0.0 || frac === 1.0 ? '' : '2 3'} />
        {/each}

        <!-- Grid lines (vertical) — at x-axis label positions, behind data -->
        {#each _eqXLabels as lbl}
          <line
            x1={parseFloat(lbl.x)} y1={PAD_T}
            x2={parseFloat(lbl.x)} y2={PAD_T + INNER_H}
            stroke="rgba(200,216,240,0.10)" stroke-width="1" stroke-dasharray="2 3" />
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
            font-size="11" font-weight="600" fill="#c8d8f0" font-family="ui-monospace,monospace"
            text-anchor="start">{lbl.label}</text>
        {/each}

        <!-- X-axis text labels intentionally removed — dashboard chart
             reads as a clean trajectory; hover tooltip still surfaces
             the exact timestamp on demand. Grid lines stay for visual
             rhythm. -->

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
    </div>

    <!-- Performance panel — historical P&L drill-down. Renders
         PnlAnalysis which owns its own filters, summary, and chart.
         hasData flows back so the parent can auto-collapse this card
         when both Intraday + Performance are empty. -->
    <div class="card-body" hidden={_chartTab !== 'performance' || _colEquityCurve}>
      <PnlAnalysis bind:hasData={_pnlHasData} />
    </div>
  </section>
</div>

<!-- Capital + Equity moved into the split row above as tabs. -->


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
      <section class="wl-tile wl-tile-win"
        class:fs-card-on={_fsWinners}
        class:is-collapsed={_colWinners}>
        <div class="card-header-row">
          <span class="mp-section-label wl-tile-label">TOP WINNERS</span>
          <AccountMultiSelect
            bind:value={_winAccounts}
            options={_availableAccounts.map(a => ({ value: a, label: a }))}
            disabled={_winAcctDisabled}
            disabledReason="Account filter applies only to Holdings + Positions tabs" />
          <CollapseButton bind:isCollapsed={_colWinners} cardId="winners" label="Top Winners" />
          <FullscreenButton bind:isFullscreen={_fsWinners} label="Top Winners" />
        </div>
        <div class="card-body" hidden={_colWinners}>
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
                {#if bucket.count > 0}<span class="wl-tab-count">{bucket.count}</span>{/if}
              </button>
            {/each}
          </div>
          <div
            bind:this={_winEl}
            class="ag-theme-algo dash-wl-grid"
            class:is-empty={_winRowsAg.length === 0}></div>
        </div>
      </section>
    {/if}

    {#if _hasLosers}
      <section class="wl-tile wl-tile-loss"
        class:fs-card-on={_fsLosers}
        class:is-collapsed={_colLosers}>
        <div class="card-header-row">
          <span class="mp-section-label wl-tile-label">TOP LOSERS</span>
          <AccountMultiSelect
            bind:value={_losAccounts}
            options={_availableAccounts.map(a => ({ value: a, label: a }))}
            disabled={_losAcctDisabled}
            disabledReason="Account filter applies only to Holdings + Positions tabs" />
          <CollapseButton bind:isCollapsed={_colLosers} cardId="losers" label="Top Losers" />
          <FullscreenButton bind:isFullscreen={_fsLosers} label="Top Losers" />
        </div>
        <div class="card-body" hidden={_colLosers}>
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
                {#if bucket.count > 0}<span class="wl-tab-count">{bucket.count}</span>{/if}
              </button>
            {/each}
          </div>
          <div
            bind:this={_losEl}
            class="ag-theme-algo dash-wl-grid"
            class:is-empty={_losRowsAg.length === 0}></div>
        </div>
      </section>
    {/if}
  </div>
{/if}

<!-- Row 3: Market news strip — single column. -->
<div class="dash-row3"
  class:fs-card-on={_fsNews}
  class:is-collapsed={_colNews}>
  <div class="row3-header">
    <span class="mp-section-label">MARKET NEWS</span>
    <CollapseButton bind:isCollapsed={_colNews} cardId="news" label="Market News" />
    <FullscreenButton bind:isFullscreen={_fsNews} label="Market News" />
  </div>
  <div class="card-body" hidden={_colNews}>
    <!-- Two-column magazine flow on wide viewports (≥900 px) so the
         news card uses the full dashboard width without leaving a
         blank right half. Limit bumped to 10 to actually fill both
         columns; NewsList collapses to 1 column below 900 px. -->
    <NewsList limit={10} columns={2} showRefreshTime={true} />
  </div>
</div>

<!-- P&L Analysis section retired — PnlAnalysis now lives inside the
     row-1 Intraday/Performance tabbed card. Dropping the standalone
     full-width section keeps the page compact and removes the duplicate
     mount. -->

<!-- SymbolPanel — opened by winners/losers tile clicks -->
{#if _ticketProps}
  <SymbolPanel
    {..._ticketProps}
    onClose={() => { _ticketProps = null; }}
    onSubmit={() => { _ticketProps = null; }} />
{/if}

<!-- Agent activity — same CollapseButton pattern as every other card.
     Default collapsed; CollapseButton restores from localStorage if
     the operator's last state was expanded. -->
<section class="dash-agent"
  class:fs-card-on={_fsAgent}
  class:is-collapsed={_colAgent}>
  <div class="card-header-row dash-agent-summary">
    <span class="mp-section-label">Agent activity</span>
    <span class="dash-agent-chip">
      <span class="dash-agent-count">{_firesToday}</span>
      <span class="dash-agent-label">fires today</span>
    </span>
    <CollapseButton bind:isCollapsed={_colAgent} cardId="agent"
      initialCollapsed={true} label="Agent activity" />
    <FullscreenButton bind:isFullscreen={_fsAgent} label="Agent activity" />
  </div>
  <div class="card-body" hidden={_colAgent}>
    <div class="dash-agent-filter">
      <button
        type="button"
        class="dash-agent-filter-btn"
        class:dash-agent-filter-btn-on={_agentLogShowActions}
        onclick={() => _agentLogShowActions = !_agentLogShowActions}>
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
  </div>
</section>

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

  /* Refresh chip per-card header. Same palette as .hero-refresh but
     sized to sit alongside the section label without pushing the
     Collapse / Fullscreen buttons off the right edge. Margin-left
     auto pins it after the label; the buttons that follow get a
     small left gap so they don't sit flush against the timestamp. */
  .bucket-refresh-chip {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.5rem;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .bucket-refresh-chip + :global(button) { margin-left: 0.4rem; }

  /* ── Row 1 (split): Capital/Equity tabbed card + Equity curve ───── */
  /* Earlier the equity curve filled a full-width hero row and Capital
     / Equity sat below at 1:1. The split layout keeps Capital + Equity
     + curve all in one glance — Bloomberg PRTU's portfolio sidebar
     beside the chart. Stacks below 1024 px. */
  .dash-row1-split {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.6rem;
    margin-bottom: 0.75rem;
  }
  @media (min-width: 1024px) {
    .dash-row1-split {
      grid-template-columns: 1fr 1fr;
    }
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

  /* Tabbed Capital/Equity card — uses the bucket-card chrome but
     replaces the section label with a tab strip. Tabs share the
     same amber-underline palette as the W/L tabs for consistency. */
  .cap-eq-tabbed { display: flex; flex-direction: column; }
  .cap-eq-tabs {
    display: inline-flex;
    gap: 0.15rem;
    margin-right: 0.5rem;
  }
  .cap-eq-tab {
    background: transparent;
    border: none;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem 0.18rem;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    transition: color 120ms, border-color 120ms;
  }
  .cap-eq-tab:hover { color: #c8d8f0; }
  .cap-eq-tab-on {
    color: #fbbf24;
    border-bottom-color: #fbbf24;
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
    /* Compact empty state — earlier reserved 220 px which left a
       big blank band on cold-start or pre-market. 3 rem fits the
       single-line message and lets the rest of the dashboard pull
       up. The curve grows back to 220 px once data lands. */
    height: 3rem;
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

  /* Push the picker + fullscreen-toggle cluster to the right of
     the bucket header — AccountMultiSelect carries its own width
     clamps, so the only local rule we need is the margin-left:auto
     on whichever element follows the heading. */
  .bucket-header > :global(.ams) { margin-left: auto; }

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
    /* Center-align the count + label vertically — baseline-align made
       the chip's visual center sit lower than the "AGENT ACTIVITY"
       label in the same flex row (the large count's baseline pulled
       the chip's content down). center-align matches the rest of the
       row chrome (.mp-section-label, CollapseButton, FullscreenButton). */
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.5rem;
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
    display: inline-block;
    padding: 0 0.32rem;
    border-radius: 0.5rem;
    background: rgba(126, 151, 184, 0.18);
    color: inherit;
    font-size: 0.55rem;
    font-weight: 700;
    line-height: 1.15rem;
    letter-spacing: 0.02em;
  }
  .wl-tab-on .wl-tab-count {
    background: rgba(251, 191, 36, 0.18);
  }
  .wl-bucket-empty {
    padding: 0.5rem 0.3rem;
    color: rgba(126, 151, 184, 0.55);
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    letter-spacing: 0.04em;
    text-align: center;
  }
  /* AccountMultiSelect on W/L card headers pushes itself + the
     fullscreen toggle to the right of the section label. The
     component carries its own width clamps. */
  .card-header-row > :global(.ams) { margin-left: auto; }

  /* Compact ag-Grid wrappers inside the Capital + W/L cards. Width
     is 100% (grid columns flex to fill); height is driven by the
     grid's autoHeight option so the card grows naturally with row
     count. min-height keeps the header visible even with 0 rows. */
  .dash-mini-grid {
    width: 100%;
    min-height: 60px;
    transition: min-height 0.18s ease;
  }
  /* When a mini-grid has no rows, collapse it out of the layout —
     the surrounding bucket-subheader is already conditionally hidden
     above, and the dash-card-empty fallback below carries the empty
     message. Without this rule ag-Grid's autoHeight still reserves
     a header strip (~28 px) for an empty grid. */
  .dash-mini-grid.is-empty {
    min-height: 0;
    height: 0;
    overflow: hidden;
    border: none;
    box-shadow: none;
  }
  .dash-mini-grid + .bucket-subheader { margin-top: 0.55rem; }

  /* (Dead .dash-card-empty rule retired — every dashboard empty
     state now uses the global <EmptyState> component which carries
     its own styling.) */

  /* W/L grid — explicit height so ag-Grid (domLayout: 'normal')
     measures + scrolls internally. Earlier max-height + overflow:auto
     on the wrapper with autoHeight on the grid caused ag-Grid to
     mis-measure and render an invisible header. Fullscreen mode
     fills the modal via flex:1 + min-height. */
  .dash-wl-grid {
    width: 100%;
    height: 18rem;
    cursor: pointer;
    transition: height 0.18s ease;
  }
  /* When the active bucket has no rows, collapse the grid wrapper
     to just enough height to show the empty-state overlay. ag-Grid
     keeps the header + overlay sized for the wrapper; this turns a
     wasteful 18 rem blank into a compact ~3.5 rem strip without
     destroying the grid (operator can flip tabs and the rows show
     instantly). */
  .dash-wl-grid.is-empty {
    height: 3.5rem;
    cursor: default;
  }
  .fs-card-on .dash-wl-grid {
    flex: 1;
    height: auto;
    min-height: calc(100vh - 14rem);
  }
  .fs-card-on .dash-wl-grid.is-empty {
    min-height: 3.5rem;
  }

  /* Util-% gradient bands — pnl-gain (green, low util) and pnl-loss
     (red, dangerous util) already live in the algo theme; util-warn
     + util-mild fill the amber middle of the colour ramp.
     `!important` so they win against the theme's row-level `color`. */
  :global(.ag-theme-algo .util-warn) {
    color: #fbbf24 !important;
    background-color: rgba(251,191,36,0.08) !important;
  }
  :global(.ag-theme-algo .util-mild) {
    color: #e5c87a !important;
    background-color: rgba(251,191,36,0.04) !important;
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
    /* align-items: center (not baseline) to match the canonical
       card-header convention — keeps the title + collapse + fullscreen
       cluster vertically aligned with the icon midline like every
       other algo card. */
    display: flex;
    align-items: center;
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
