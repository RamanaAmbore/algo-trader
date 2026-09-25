<script>
  // Options Analytics dashboard (/admin/options).
  //
  // Multi-leg options analytics workspace; auto-detects live vs sim from
  // `/api/simulator/status`. Payoff diagram, Greeks, risk metrics, POP,
  // EV. Strategy analytics auto-refreshes whenever the leg set changes.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore, marketAwareInterval, visibleInterval, selectedStrategyId, strategyOpenSymbols, includeHoldings, brokerHealthStore, lastRefreshAt, ltpFlashPct, loadLtpFlashPct } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import { isMarketOpen } from '$lib/marketHours';
  import { createPerformanceSocket } from '$lib/ws';
  import { bookChanged } from '$lib/data/bookChanged';
  import {
    fetchSimStatus, fetchStrategyAnalytics,
    fetchAccounts, fetchOptionsSpot, fetchChainQuotes,
    placeTicketOrder, fetchLiveStatus,
    fetchWatchlists, fetchWatchlist, addWatchlistItem,
  } from '$lib/api';
  import { positionsStore, holdingsStore, pulsePositionsStore } from '$lib/data/marketDataStores.svelte.js';
  import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';
  import { positionsDerivedStore } from '$lib/data/positionsDerivedStore.svelte.js';
  import { loadWatchlistSymbols } from '$lib/data/watchlistSymbols.js';
  import { getProvisionalPositions } from '$lib/data/provisionalPositions.svelte.js';
  import { getDraftPositions } from '$lib/data/draftPositions.svelte.js';
  import { getSnapshot, liveSnap, symbolTickCount, tickBus } from '$lib/data/symbolStore.svelte.js';
  import OptionsPayoff from '$lib/OptionsPayoff.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import Select        from '$lib/Select.svelte';
  import MultiSelect   from '$lib/MultiSelect.svelte';
  import InfoHint      from '$lib/InfoHint.svelte';
  import CardControls from '$lib/CardControls.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listStrikes, findOption,
    listFutures, getInstrument, getOptionUnderlyingLot,
    findNearestFuture,
  } from '$lib/data/instruments';
  import { resolveUnderlying, resolveUnderlyingTradingsymbol } from '$lib/data/resolveUnderlying';
  import { expiryPnl, expiryPnlWithRealised, resolveExpiryAnchor, legExtrinsicDisplay } from '$lib/data/expiryPnl';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { decomposeSymbol, formatSymbol } from '$lib/data/decomposeSymbol';
  import { rootOfLabel } from '$lib/data/rootOf.js';
  import { acctColor } from '$lib/account';
  import { POPULAR_UNDERLYINGS } from '$lib/data/popularUnderlyings';
  import { priceFmt, pctFmt, aggCompact, fmtPctFraction, ltpDayClass } from '$lib/format';
  import { todayIST } from '$lib/dateFormat.js';
  import { lotsForRow, fmtLots } from '$lib/data/lotsForRow';
  import {
    loadHedgeProxies, proxiesForTarget, targetsForProxy, getProxyRow,
  } from '$lib/data/hedgeProxies';
  import { baseDayPnlForPosition, FO_EXCHANGES } from '$lib/data/nav';
  import { exportRowsToCsv } from '$lib/utils/csvExport.js';
  import { RISK_FREE_R as _RISK_FREE_R, normCdf as _normCdf, probAbove as _probAbove, expectedValueOnCurve as _expectedValueOnCurve, multilegPopOnCurve as _multilegPopOnCurve } from '$lib/data/riskMath.js';
  import ChartModal from '$lib/ChartModal.svelte';
  import OrderPairModal from '$lib/order/OrderPairModal.svelte';
  import { computePairEnabled } from '$lib/order/pairModalUtils.js';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { longPress } from '$lib/actions/longPress.js';
  import { accountDisplayOrder, sortAccountsBy, getAccountOrderMap } from '$lib/data/accountSort.js';
  import {
    buildAcctMatcher, buildStrategyMatcher,
    annotateOptionCandidates, computeExpiryBands,
    rollupByUnderlying, perRootReduce, interpAt,
  } from '$lib/data/derivativesMath.js';
  import {
    isFOSymbol, buildExpiryMatcher, buildCandidatePositions,
    buildPositionRowFromBroker, buildHoldingRowFromBroker, bumpExcluded,
    splitClosedReopened,
    buildCleanLegs, computeLegsKey, didUnderlyingChange,
    synthCacheKey, synthEquityOnlyStrategy,
  } from '$lib/derivatives/pageLoad.js';
  import CandidateLegRow from './CandidateLegRow.svelte';
  import { openOrderQtyBySymbol } from '$lib/data/openOrdersStore.svelte.js';
  import { payoffDrafts } from '$lib/data/payoffDrafts.svelte.js';
  import { debugLog } from '$lib/debug/debugLog.js';
  import { underlyingSpotStore, loadUnderlyingSpots as _loadUnderlyingSpotsFn, patchUnderlyingSpot, pruneUnderlyingSpotRoots } from '$lib/data/underlyingSpotStore.svelte.js';

  // Row-level chart modal for Candidates panel rows.
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  function _openChart(/** @type {string} */ symbol, /** @type {string} */ exchange = '') {
    _chartModalSym  = String(symbol  || '').toUpperCase();
    _chartModalExch = String(exchange || '');
  }

  // Pair-orders modal state — opened by the "Pair" button in the Drafts leg-headrow.
  let _legPairModalOpen = $state(false);

  // Context menu state — right-click / long-press on any candidate symbol.
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | 'log' | null} */ let _ctxAction = $state(null);
  /** @type {string} */ let _ctxSym  = $state('');
  /** @type {string} */ let _ctxExch = $state('');

  // Source card semantics (v4): no more single-vs-multi distinction.
  // Everything is multi-leg. One leg analyses fine through the strategy
  // endpoint; the operator just sees the same payoff + Greeks + risk
  // panel regardless of how many legs are checked.
  //
  // Data source is auto-detected: when a sim is running, the page works
  // off sim positions; otherwise it works off live broker positions.
  // Drafts (operator-typed hypothetical positions) layer on top in
  // either case.
  /** @type {any} */ let strategy   = $state(null);
  let strategyErr   = $state('');
  // Canonical account display order map — subscribed so `accountChoices`
  // re-derives whenever fetchBrokerOrder() resolves after cold load.
  let _derivOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubDerivsOrder = accountDisplayOrder.subscribe(m => { _derivOrderMap = m; });
  let _brokerWorstState = $state(/** @type {'green'|'amber'|'red'} */ ('amber'));
  const _unsubBrokerHealth = brokerHealthStore.subscribe(v => { _brokerWorstState = v?.worstState || 'amber'; });
  let loading       = $state(false);
  // Payoff-chart-specific "a routine strategy refetch is in flight" flag
  // (B1 fix). Distinct from `loading` (which drives OptionsPayoff's
  // "Resolving spot…" full-chart placeholder for a genuine cold start)
  // and from `_refreshing` below (the RefreshButton "any of the three
  // page-level loads is in flight" flag) — this one drives ONLY the
  // small spinner in the payoff chart's LTP/CHG% stat rows, so a
  // routine 5s refetch never re-triggers the full-chart cyan pulse.
  // Generation-guarded (B4) the same way `loading` now is — see
  // loadStrategy()'s `finally` block.
  let _stratRefreshing = $state(false);
  // `loading` is toggled by loadStrategy() and short-circuits on
  // its leg-cache shortcut, so RefreshButton wired to `loading`
  // never animates when the operator clicks Refresh on an unchanged
  // basket. `_refreshing` is the canonical "any of the three loads
  // is in flight" state — the three RefreshButton instances on this
  // page bind to this instead.
  let _refreshing   = $state(false);
  async function _refreshAll() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      await Promise.allSettled([
        loadPositions({ fresh: true }),
        loadSimStatus(),
        loadStrategy({ force: true }),
      ]);
    } finally {
      _refreshing = false;
    }
  }
  let teardown;
  let wsTeardown;
  let quotesTeardown;
  let simTeardown;

  // Sim status — when true, the candidates panel shows sim positions
  // instead of live. Polled every few seconds.
  let simActive = $state(false);

  // "+ Add" panel toggle — when on, the option-chain picker opens to
  // let the operator browse strikes for the underlying and drop legs
  // into Drafts.
  let showAddPanel = $state(false);

  // Drafts — hypothetical positions the operator types in. They sit
  // beside live + sim positions in the Candidates panel and feed into
  // either single-leg or strategy analytics. `id` is a stable client-
  // side key so the panel rows don't lose their state when one is
  // removed mid-edit.
  let _draftSeq = 0;
  /** @type {Array<{id:number, symbol:string, qty:number|'', avg_cost:number|'', ltp:number|''}>} */
  let drafts = $state([]);

  // Merged draft list: local text-input drafts + OrderTicket "Add to Payoff" entries.
  // buildCandidatePositions accepts the same shape for both sources.
  // payoffDrafts entries carry string ids; we normalise them to a numeric-compatible
  // shape (the function only uses `.id` as an opaque key for draftId).
  const _allDrafts = $derived.by(() => {
    const pd = [...payoffDrafts.value.values()];
    return [
      ...drafts,
      ...pd.map(e => ({
        id:       e.id,
        symbol:   e.symbol,
        qty:      e.qty,
        avg_cost: e.avg_cost ?? '',
        ltp:      '',
      })),
    ];
  });

  // showDraftInPayoff is declared further down (near _loadDraftInPayoff)
  // with localStorage persistence. No duplicate declaration here.

  // Non-blocking completion toast stack. Pushed by onTicketSubmit when
  // a paper/live order lands; rendered as fixed-position cards at the
  // top-right corner so the chain + drafts table behind them stay
  // fully interactive (operator can place the next order without
  // dismissing). Auto-dismiss after 5 s; click × to clear early.
  /** @type {Array<{id:number, mode:string, side:string, qty:number, symbol:string, price:string, orderId:string, status:string, ts:number}>} */
  let _orderToasts = $state([]);
  let _orderToastSeq = 0;
  /** Tracks all live auto-dismiss timer handles so onDestroy can clear them. */
  const _orderToastTimers = new Set(/** @type {ReturnType<typeof setTimeout>[]} */ ([]));
  onDestroy(() => {
    for (const t of _orderToastTimers) clearTimeout(t);
    _orderToastTimers.clear();
  });
  function _pushOrderToast(/** @type {any} */ payload) {
    const resp  = payload?.broker_response || {};
    const px    = payload.price != null
      ? `@₹${Number(payload.price).toFixed(2)}`
      : '@MKT';
    const toast = {
      id:      ++_orderToastSeq,
      mode:    String(payload.mode || '').toUpperCase(),
      side:    String(payload.side || ''),
      qty:     Number(payload.quantity || 0),
      symbol:  String(payload.symbol || ''),
      price:   px,
      orderId: String(resp.order_id || '?'),
      status:  String(resp.status || 'PLACED').toUpperCase(),
      ts:      Date.now(),
    };
    _orderToasts = [..._orderToasts, toast];
    // Auto-dismiss after 5 s. Identified by toast id rather than
    // index so concurrent dismissals don't fight each other.
    const tid = setTimeout(() => {
      _orderToastTimers.delete(tid);
      _orderToasts = _orderToasts.filter(t => t.id !== toast.id);
    }, 5000);
    _orderToastTimers.add(tid);
  }
  function _dismissOrderToast(/** @type {number} */ id) {
    _orderToasts = _orderToasts.filter(t => t.id !== id);
  }
  /** Mark a toast as FILLED in-place when the WS broadcasts a fill
   *  for an order_id we previously placed. Refreshes the auto-
   *  dismiss timer so the operator sees the FILLED status for 5 s
   *  starting NOW (not stale from the original placement). */
  function _markToastFilled(/** @type {string} */ orderId, /** @type {number} */ fillPrice) {
    const idx = _orderToasts.findIndex(t => String(t.orderId) === String(orderId));
    if (idx < 0) return false;
    const next = [..._orderToasts];
    next[idx] = {
      ...next[idx],
      status: 'FILLED',
      price: fillPrice ? `@₹${Number(fillPrice).toFixed(2)}` : next[idx].price,
      ts: Date.now(),
    };
    _orderToasts = next;
    const toastId = next[idx].id;
    const tid = setTimeout(() => {
      _orderToastTimers.delete(tid);
      _orderToasts = _orderToasts.filter(t => t.id !== toastId);
    }, 5000);
    _orderToastTimers.add(tid);
    return true;
  }
  /** Push a fresh FILLED toast when the WS reports a fill we don't
   *  have a prior placement toast for (e.g. an algo-engine fill the
   *  operator didn't manually place via the OrderTicket modal). */
  function _pushFillToast(/** @type {any} */ msg) {
    const toast = {
      id:      ++_orderToastSeq,
      mode:    'FILL',
      side:    msg.qty > 0 ? 'BUY' : msg.qty < 0 ? 'SELL' : '',
      qty:     Math.abs(Number(msg.qty || 0)),
      symbol:  String(msg.tradingsymbol || ''),
      price:   msg.fill_price ? `@₹${Number(msg.fill_price).toFixed(2)}` : '',
      orderId: String(msg.order_id || ''),
      status:  'FILLED',
      ts:      Date.now(),
    };
    _orderToasts = [..._orderToasts, toast];
    const tid = setTimeout(() => {
      _orderToastTimers.delete(tid);
      _orderToasts = _orderToasts.filter(t => t.id !== toast.id);
    }, 5000);
    _orderToastTimers.add(tid);
  }
  function addDraft() {
    drafts = [...drafts, { id: ++_draftSeq, symbol: '', qty: '', avg_cost: '', ltp: '' }];
  }
  function removeDraft(/** @type {number|string} */ id) {
    // Local text-input drafts use numeric ids.
    drafts = drafts.filter(d => d.id !== id);
    // payoffDrafts entries use string ids prefixed with 'pd_'.
    if (typeof id === 'string' && id.startsWith('pd_')) {
      payoffDrafts.remove(id);
    }
  }

  // Strategy mode v2 — pick (Account, Underlying) and the matching
  // open positions appear as toggleable candidates below the chart.
  // Each candidate is an option or future on the chosen underlying
  // that exists in one of the selected accounts. Operator checks the
  // ones to include; legs[] is derived from enabled candidates plus
  // any manually-added or chain-picked rows.

  /** @type {string[]} Selected account codes; empty = all accounts */
  let selectedAccounts = $state([]);
  /** @type {Set<string>} Per-session ledger of every account we've ever
   * observed in `accountChoices`. Persisted to sessionStorage so the
   * "is this account new?" check survives tab refresh. Used by the
   * auto-union effect below to fold late-arriving broker accounts (Dhan
   * loaded via /admin/brokers AFTER the operator's selectedAccounts
   * was already restored from cache) into the active selection. Without
   * this, Dhan rows stay filtered out of the Candidates panel until
   * the operator manually opens the picker and toggles them on. */
  let _seenAccts = new Set();
  /** @type {string} Underlying name (e.g. NIFTY); '' = pick required */
  let selectedUnderlying = $state('');
  /** @type {string[]} Expiry filter (YYYY-MM-DD[]); empty = all expiries.
   *  Multi-select — operator can view candidates from multiple expiries
   *  simultaneously. The strategy endpoint now accepts cross-expiry baskets. */
  let selectedExpiries = $state([]);

  // ── URL state sync (slice AV) ─────────────────────────────────────
  // Persist (underlying, expiry) to ?u=...&e=YYYY-MM-DD,YYYY-MM-DD so the
  // operator can bookmark or share a scoped view, and jumping away then
  // back via the browser's Back button restores the same scope. Mirrors
  // the pattern in /charts (commit eaa9a91d) and addresses UX audit
  // item #7. sessionStorage is kept for the rich snapshot (the URL only
  // carries the two filter axes, not the heavy data cache).
  //
  // One-shot read on mount: seed from URL params if present. Any subsequent
  // operator change to selectedUnderlying / selectedExpiries fires the
  // sync $effect below which goto({replaceState: true}) the new URL —
  // doesn't push a history entry on every picker click.
  onMount(() => {
    try {
      const sp = new URLSearchParams(window.location.search);
      const u = (sp.get('u') || '').toUpperCase().trim();
      if (u) selectedUnderlying = u;
      const e = (sp.get('e') || '').trim();
      if (e) selectedExpiries = e.split(',').map(x => x.trim()).filter(Boolean);
    } catch {}
  });

  // URL sync — debounced 150 ms so a flurry of picks doesn't queue N
  // goto() invocations. goto() with replaceState is still measurably
  // expensive (SvelteKit walks the route tree, fires nav lifecycle hooks
  // even when the route is unchanged). Operator: "takes much time to
  // update symbol in dropdown" — the synchronous goto() inside the pick
  // path was a multi-frame hit. Deferring releases the click-to-paint
  // budget so the dropdown closes and downstream candidatePositions /
  // loadStrategy work scheduled by the same pick proceeds without
  // racing the navigation.
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _urlSyncTimer = null;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _orderUpdateTimer = null;
  $effect(() => {
    // Track both pieces of state.
    const u = selectedUnderlying;
    const es = selectedExpiries.slice();
    untrack(() => {
      if (_urlSyncTimer) clearTimeout(_urlSyncTimer);
      _urlSyncTimer = setTimeout(() => {
        _urlSyncTimer = null;
        try {
          const url = new URL(window.location.href);
          if (u) url.searchParams.set('u', u);
          else   url.searchParams.delete('u');
          if (es.length) url.searchParams.set('e', es.join(','));
          else           url.searchParams.delete('e');
          const next = url.pathname + (url.search ? url.search : '');
          if (next !== page.url.pathname + page.url.search) {
            goto(next, { replaceState: true, noScroll: true, keepFocus: true });
          }
        } catch {}
      }, 150);
    });
  });
  /** @type {Record<string, boolean>} `${account}|${symbol}` → enabled flag.
   * Composite key so the same option symbol in two broker accounts gets
   * an independent checkbox + isn't double-counted in P&L. */
  let enabledSymbols = $state({});
  /** Master toggle — when ON, enabled equity-holding legs contribute to
   *  the payoff curve + Greeks + risk metrics. When OFF, eq legs are
   *  excluded even if their row checkbox is ticked. Default OFF so the
   *  page lands on the pure-derivative payoff; operator opts in when
   *  they want the covered-call / collar / hedged-stock view layered
   *  on. Persisted to localStorage so subsequent visits remember the
   *  operator's last choice — the OFF default applies only on the
   *  very first visit. */
  // Sync to shared `includeHoldings` store so NavStrip's P slots 1 + 2
  // reflect the same toggle. Store persists to localStorage
  // (opt.includeHoldings key) + fires storage events for cross-tab sync.
  // Operator 2026-07-01: "p & l should include underlying position if
  // hold button is on... it is similar to exp p & L."
  let _includeHoldings = $state($includeHoldings);
  $effect(() => { _includeHoldings = $includeHoldings; });
  // Stable callback reference for OptionsPayoff.onToggleDraft.
  function _flipDraft() {
    showDraftInPayoff = !showDraftInPayoff;
  }

  // Stable callback reference for OptionsPayoff.onToggleHoldings.
  // Hoisting it out of the JSX prevents the fresh-closure problem —
  // every parent re-render would otherwise pass a new function ref,
  // triggering OptionsPayoff to invalidate downstream $derived caches
  // even when nothing about the chart actually changed.
  function _flipHoldings() {
    includeHoldings.set(!_includeHoldings);
  }
  /** Whether provisional (~) and draft-store (D) legs are included in the
   *  payoff curve. Both non-confirmed sources are gated by one toggle so
   *  the operator can quickly exclude unconfirmed legs from the analytics
   *  without having to uncheck each row individually. Default ON so fills
   *  that haven't settled yet still appear in the curve immediately.
   *  Persisted to localStorage so the operator's choice survives reload. */
  const _DRAFT_PAYOFF_KEY = 'ramboq:options-draft-in-payoff';
  function _loadDraftInPayoff() {
    if (typeof localStorage === 'undefined') return true;
    try {
      const v = localStorage.getItem(_DRAFT_PAYOFF_KEY);
      return v === null ? true : v !== 'false';
    } catch { return true; }
  }
  let showDraftInPayoff = $state(_loadDraftInPayoff());
  $effect(() => {
    if (typeof localStorage !== 'undefined') {
      try { localStorage.setItem(_DRAFT_PAYOFF_KEY, String(showDraftInPayoff)); }
      catch { /* quota / private mode — silent */ }
    }
  });
  // Composite key for the enabledSymbols map. Plain symbol collided
  // across accounts: a NIFTY24DEC25000PE held in both ZG#### and
  // ZJ#### would share one checkbox, and the candidatesActualPnl
  // counter summed both rows even though only one was "checked".
  function enKey(c) { return `${c.account || ''}|${c.symbol || ''}`; }

  /** Long-term memory of eq-leg checkbox choices, keyed by enKey(c).
   *  Survives across page reloads (localStorage, no TTL), so an
   *  operator who's once explicitly checked BHEL eq doesn't have to
   *  re-check it on every future visit. Only eq rows write here —
   *  derivative legs follow the default-ON rule and don't need memory.
   *  Toggling OFF removes the key entirely (no "remember off" — the
   *  default IS off, so explicit off behaves like "forget"). */
  const _EQ_MEMORY_KEY = 'ramboq:options-eq-checked';
  /** @type {Record<string, boolean>} */
  let _eqMemory = $state(_loadEqMemory());
  function _loadEqMemory() {
    if (typeof localStorage === 'undefined') return {};
    try {
      const v = JSON.parse(localStorage.getItem(_EQ_MEMORY_KEY) || '{}');
      return (v && typeof v === 'object') ? v : {};
    } catch { return {}; }
  }
  function _saveEqMemory() {
    if (typeof localStorage === 'undefined') return;
    try { localStorage.setItem(_EQ_MEMORY_KEY, JSON.stringify(_eqMemory)); }
    catch { /* quota / private mode — silent */ }
  }
  /** Record an eq-leg's explicit on/off choice. ON writes the key;
   *  OFF removes it (default IS off, so we never need to persist a
   *  "remember off" — absence == default == off). */
  function _persistEqMemory(/** @type {{account?:string, symbol?:string}} */ c, /** @type {boolean} */ checked) {
    const k = enKey(c);
    if (!k || k === '|') return;
    const next = { ..._eqMemory };
    if (checked) next[k] = true;
    else delete next[k];
    _eqMemory = next;
    _saveEqMemory();
  }

  /** Single source of truth for "is this candidate row contributing to
   *  the payoff right now?". Derivative legs default ON; equity-
   *  holding legs default OFF — but eq legs also consult the long-
   *  term `_eqMemory` so a previously-checked row stays checked
   *  across page reloads / days / weeks. Session enabledSymbols
   *  state takes precedence so an explicit toggle inside the page
   *  reflects immediately.
   *
   * @param {{kind?:string, account?:string, symbol?:string}} c
   * @returns {boolean}
   */
  function _isLegEnabled(c) {
    const v = enabledSymbols[enKey(c)];
    if (c?.kind === 'eq') {
      if (v === true) return true;
      if (v === false) return false;
      return _eqMemory[enKey(c)] === true;
    }
    return v !== false;
  }

  // Legs sent to the strategy endpoint — built from candidate positions
  // (live or sim, depending on simActive) plus drafts that match the
  // selected underlying, intersected with the operator's checked rows
  // in the Candidates panel.
  /** @type {Array<{symbol:string, qty:any, avg_cost:any, ltp:any, source:string, kind?:string}>} */
  let legs = $state([]);

  // Timestamp (ms) set each time loadPositions successfully completes.
  // Used to guard the strategy-wipe in loadStrategy: if positions data
  // is stale (loaded >30s ago, e.g. on hibernation exit before the book
  // poller has refreshed), we do NOT wipe the strategy — doing so would
  // show a blank chart until the next poll tick.
  let _positionsRefreshedAt = 0;

  // Legs panel collapsed/expanded — operator may want to fold it
  // away once they've vetted the basket so the chart + cards have
  // more vertical room.
  // legsOpen retired — _colLegs (driven by the global CollapseButton)
  // is now the sole gate on the legs card body.

  // Per-card collapse + fullscreen toggles — match the dashboard
  // pattern. CollapseButton hydrates each from localStorage per-user
  // on mount (keyed by username + cardId).
  let _colPayoff = $state(false);
  let _colLegs   = $state(false);
  let _colByund  = $state(false);
  let _fsPayoff  = $state(false);
  let _fsLegs    = $state(false);
  let _fsByund   = $state(false);
  let _filterByund = $state('');

  // Tab inside the Legs card — 'legs' shows the full candidate
  // grid; 'expiry' shows positions identified for close before
  // expiry day (equity rules: every ITM contract; commodity rules:
  // only unhedged ITM legs where CE qty + PE qty per
  // (underlying, expiry) doesn't net to zero).
  /** @typedef {'legs' | 'expiry'} LegsTab */
  let legsTab = $state(/** @type {LegsTab} */ ('legs'));

  // Expiry-close analysis. Derived from candidatePositions + the
  // strategy spot price. Splits by exchange: NFO (equity) drops
  // all ITM contracts into the close list; MCX (commodity) groups
  // by (underlying, expiry) and only surfaces ITM contracts whose
  // net CE qty + net PE qty is non-zero (the broker settles
  // perfectly-hedged pairs against each other, no operator action
  // needed). Closed positions (qty 0) and drafts are skipped.
  // MCX underlyings — used to classify a position as commodity vs
  // equity from the parsed symbol's underlying name. The Kite
  // exchange field can be missing on synthesized/draft rows, and
  // some pipelines normalise GOLDM-style names without preserving
  // exchange, so detecting segment from the symbol is more robust
  // than reading c.exchange. Add new MCX names here as needed.
  const _MCX_UNDERLYINGS = new Set([
    'GOLD', 'GOLDM', 'GOLDGUINEA', 'GOLDPETAL',
    'SILVER', 'SILVERM', 'SILVERMIC',
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATURALGASM',
    'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
    'ALUMINIUM', 'ALUMINI', 'NICKEL',
    'MENTHAOIL', 'COTTON', 'CASTORSEED', 'KAPAS',
  ]);

  // Hoisted — used in the {#each} template; defining inside {#if} would
  // allocate a new object on every render cycle.
  const BAND_LABELS = { close: 'ITM ON EXPIRY', netted: 'NETTED', otm: 'OUT OF THE MONEY' };

  // Band sort order — shared by both equity + commodity sort comparators.
  const expiryCloseAnalysis = $derived.by(() => {
    // Track candidatePositions + selectedExpiries reactively (this is
    // the heavy O(N²) work — we want it to re-run when positions
    // actually change, but NOT on every 5 s `strategy` repoll just
    // because spot drifted a paisa or analytics returned fresh refs).
    // Spot + leg analytics are read via `untrack` so the derive
    // captures them as of the moment of re-evaluation without
    // re-firing on every poll. Trade-off: bands won't immediately
    // re-classify on a sub-strike spot drift; the next position poll
    // (30 s) re-runs the analysis cleanly. Operator never sees stale
    // since the only way bands flip is positions adding/closing.
    const cps = candidatePositions;
    const expFilter = selectedExpiries;
    // Per-underlying spot resolver — reads SSE snapshot → batchQuote → 0.
    // Wrapped in untrack() so this derived re-fires only when candidatePositions
    // changes, not on every SSE tick. Full-book expiry analysis needs a spot
    // for every underlying in the book, not just selectedUnderlying.
    const uq = untrack(() => _underlyingQuotes);
    const legA = untrack(() => legAnalyticsBySymbol);
    void expFilter;
    const empty = /** @type {{equity:any[], commodity:any[]}} */ ({ equity: [], commodity: [] });
    if (!cps.length) return empty;

    const spotResolver = (/** @type {string} */ underlying) => {
      const key = String(underlying || '').toUpperCase();
      const v = Number(untrack(() => getSnapshot(key)?.ltp));
      if (Number.isFinite(v) && v > 0) return v;
      const bq = Number(uq[key]?.ltp);
      if (Number.isFinite(bq) && bq > 0) return bq;
      return 0;
    };

    // annotateOptionCandidates + computeExpiryBands are pure helpers in
    // derivativesMath.js. The spotResolver function is called per-row so
    // each underlying resolves its own spot independently.
    const annotated = annotateOptionCandidates({
      candidates: cps,
      spot: spotResolver,
      expFilter,
      mcxUnderlyings: _MCX_UNDERLYINGS,
      legAnalytics: legA,
      getInstrument,
    });
    return computeExpiryBands({ annotated });
  });
  // expiryCloseTotal counts only the 'close' band rows — those
  // are the ones that need operator action before expiry.
  const expiryCloseTotal = $derived(
    expiryCloseAnalysis.equity.filter(r => r._band === 'close').length +
    expiryCloseAnalysis.commodity.filter(r => r._band === 'close').length
  );

  // Rows surfaced inside the Legs card's grid. When legsTab='legs'
  // the operator sees the full candidate set in its natural order.
  // When 'expiry', we pull rows from expiryCloseAnalysis directly so
  // the theta-DESC ordering survives the trip to the rendered grid.
  // All three bands (close / netted / otm) are surfaced; _expiryStatus
  // encodes both segment and band so the CSS tint applies correctly.
  // Header symbol filter — bound by <GridSearchButton> in the Legs
  // card header. Empty = no filter; otherwise case-insensitive
  // substring match against the row's symbol field.
  let _filterLegs = $state('');
  const displayedCandidates = $derived.by(() => {
    let rows;
    if (legsTab !== 'expiry') {
      rows = candidatePositions;
    } else {
      rows = [];
      for (const r of expiryCloseAnalysis.equity)
        rows.push({ ...r, _expiryStatus: `equity-${r._band}` });
      for (const r of expiryCloseAnalysis.commodity)
        rows.push({ ...r, _expiryStatus: `commodity-${r._band}` });
    }
    // Holdings toggle — when OFF, hide every equity-holding row from
    // the panel + drop them from the TOTAL row sums. Operator:
    // "when it is off, legs should not show holdings rows. the totals
    // should reflect that." The chart-side gate is already in
    // `_equityLegs`; this gate keeps the visible Candidates panel +
    // its TOTAL row in lockstep with the payoff.
    if (!_includeHoldings) {
      rows = rows.filter(c => c.kind !== 'eq');
    }
    if (_filterLegs) {
      const q = _filterLegs.toUpperCase();
      rows = rows.filter(c => String(c.symbol || '').toUpperCase().includes(q));
    }
    // Pair-group sort (legs tab only — expiry tab preserves its band ordering).
    // P1 cluster → P2 → … → orphan / ungrouped last. Stable within each group.
    if (legsTab !== 'expiry') {
      rows = rows.slice().sort((a, b) => {
        const ka = a.pair_group_key ?? 'ZZZZ_orphan';
        const kb = b.pair_group_key ?? 'ZZZZ_orphan';
        return ka < kb ? -1 : ka > kb ? 1 : 0;
      });
    }
    return rows;
  });

  /** Same rows as displayedCandidates, but WITHOUT the search-text filter
   *  (_filterLegs) — the base for the Legs TOTAL / Exp P&L / offset
   *  computations below, so typing in the Legs search box narrows the
   *  grid's VISIBLE rows without shifting the computed totals (§5: the
   *  curve is built from the unfiltered leg set, so totals derived from a
   *  search-filtered set could silently diverge from the curve). Still
   *  applies the legsTab (legs vs expiry) and _includeHoldings gates,
   *  since those are intentional scope changes the operator expects
   *  totals to reflect (operator: "when [holdings] is off, legs should
   *  not show holdings rows. the totals should reflect that"). */
  const _legsTotalsBase = $derived.by(() => {
    let rows;
    if (legsTab !== 'expiry') {
      rows = candidatePositions;
    } else {
      rows = [];
      for (const r of expiryCloseAnalysis.equity)
        rows.push({ ...r, _expiryStatus: `equity-${r._band}` });
      for (const r of expiryCloseAnalysis.commodity)
        rows.push({ ...r, _expiryStatus: `commodity-${r._band}` });
    }
    if (!_includeHoldings) {
      rows = rows.filter(c => c.kind !== 'eq');
    }
    return rows;
  });

  /** Whole-book snapshot grouped by parsed underlying root. Operator:
   *  "The underlying snapshot should show all totals for all
   *  underlying with no relation with root/symbol selector for
   *  derivatives. There should be one row for each root/underlying.
   *  It should be wired to account dropdown in the page. not
   *  underlying."
   *
   *  Reads raw `positions` (opt + fut) directly so the picker bar's
   *  Underlying / Expiry filters DO NOT scope this rollup. The only
   *  filter applied is the Account multi-select. Each row is one
   *  underlying root with F&O-only totals. */
  const _byUnderlyingTotals = $derived.by(() => {
    const wantedSource = simActive ? 'sim' : 'live';
    // Delegates to rollupByUnderlying in derivativesMath.js.
    // buildAcctMatcher + buildStrategyMatcher eliminate the duplicated
    // closure boilerplate that appears in _byUnderlyingExp and _perRootReduce.
    const matchAccount  = buildAcctMatcher(selectedAccounts);
    const matchStrategy = buildStrategyMatcher($selectedStrategyId, $strategyOpenSymbols);
    return rollupByUnderlying({
      positions, holdings, wantedSource,
      matchAccount, matchStrategy,
      filterQ: _filterByund,
      decomposeSymbol, targetsForProxy, getOptionUnderlyingLot,
      baseDayPnlForPosition,
      holdingsDayPnlByKey: holdingsDayPnlStore.byKey,
    });
  });

  /** Multi-source per-root spot resolver — same chain the payoff overlay
   *  uses. Operator 2026-07-01: "SUZLON, IDFIRSTB, CRUDEOIL are showing
   *  exp p&l as 0. overlay for symbol shows correct value. use ssot of
   *  overlay and keep others in sync." Previously _byUnderlyingExp used
   *  _underlyingQuotes[root]?.ltp alone; when batchQuote missed the root
   *  (equity delisted from batch response, MCX nearest-future resolution
   *  failure, or transient response error), the row silently dropped
   *  every leg and totalled 0. Chain of fallbacks so most roots resolve:
   *    1. _underlyingQuotes[root]?.ltp — batchQuote-cached snapshot
   *    2. symbolStore for resolveUnderlying(root)?.tradingsymbol — SSE tick
   *    3. symbolStore for bare root — bare-name subscription
   */
  function _rootSpot(/** @type {string} */ root) {
    const v0 = _underlyingQuotes[root]?.ltp;
    if (typeof v0 === 'number' && v0 > 0) return v0;
    const resolved = resolveUnderlying(root, findNearestFuture);
    if (resolved?.tradingsymbol) {
      const v1 = getSnapshot(String(resolved.tradingsymbol).toUpperCase())?.ltp;
      if (typeof v1 === 'number' && v1 > 0) return v1;
    }
    const v2 = getSnapshot(String(root).toUpperCase())?.ltp;
    if (typeof v2 === 'number' && v2 > 0) return v2;
    // Fallback 4 (closed-hours-critical): scan positions + holdings for a
    // row whose tradingsymbol IS the underlying root (equity holding /
    // spot future) OR whose resolveUnderlying match hits this root. Use
    // its `last_price` as the spot. When markets are closed, batchQuote
    // is paused by marketAwareInterval and symbolStore may not have SSE
    // ticks for equity roots — but the position/holding row still has
    // the LAST session's close price which is authoritative for expiry-
    // day P&L intrinsic calculation.
    const targetKey = String(resolved?.tradingsymbol || root).toUpperCase();
    const targetRoot = String(root).toUpperCase();
    for (const src of [positions, holdings]) {
      for (const _row of (src ?? [])) {
        const row = /** @type {any} */ (_row);
        const rSym = String(row?.symbol || row?.tradingsymbol || '').toUpperCase();
        if (!rSym) continue;
        if (rSym === targetKey || rSym === targetRoot) {
          const lp = Number(row?.last_price || 0);
          if (lp > 0) return lp;
        }
      }
    }
    return null;
  }

  /**
   * Accumulate one F&O position into the exp-P&L map.
   * Returns false when the row should be skipped (wrong source/account/kind).
   * Mutates `out` in place via `ensure`.
   *
   * @param {any}    p            - position row
   * @param {string} wantedSource - 'sim' | 'live'
   * @param {Function} matchAccount
   * @param {Function} matchStrategy
   * @param {Function} ensure     - (root) => out[root]
   * @returns {boolean}  true if row was accumulated
   */
  // _accumulatePosExpPnl, _accumulateHoldingExpPnl, _byUnderlyingExp removed.
  // Exp P&L per root for the Snapshot grid now comes from
  // _filteredExpPnlByRoot (built via _perRootReduce below), NOT from
  // positionsDerivedStore.byRootPositions/byRootHoldings directly —
  // those read the unfiltered book with no matchAccount/matchStrategy
  // gate. _filteredExpPnlByRoot DOES apply the same filter as the rest
  // of the Snapshot row (item-4/§5 fix), uniformly for EVERY root
  // including the selected one (item-3 fix, round 4 — see
  // _rowExpPnlFor's docstring; it no longer special-cases the selected
  // underlying against the differently-filtered Legs TOTAL).

  /** Shared per-root accumulator — mirrors overlay's per-leg iteration
   *  (candidatesDayPnl / candidatesActualPnl / _legsExpPnlTotal) but
   *  groups by root so every Snapshot row consumes the SAME overlay
   *  compute rather than a divergent per-row sum.
   *
   *  Semantics locked in one place:
   *    - _isLegEnabled gate per candidate (operator's per-leg checkbox)
   *    - _includeHoldings gate for eq legs
   *    - Proxy-target routing via targetsForProxy(h.symbol) → root credits
   *    - Account + source filter (live vs sim)
   *    - Strategy filter (matchStrategy) — MUST match _byUnderlyingTotals /
   *      _byUnderlyingExp to keep the Snapshot TOTAL row consistent with
   *      per-row data.
   *
   *  Operator 2026-07-01: "day p & l should use the same calculation
   *  in overlay in payoff. ssot" + "p & l should use the same
   *  calculation of tday. exp p & l should use the same calculation as
   *  exp of overlay. reusable similar code should be used for both."
   *
   *  @param {(c: any, spot: number|null) => number|null|undefined} accessor
   *    Per-leg value function — same shape overlay uses.
   *  @param {(sym: string) => boolean} [matchStrategy]
   *    Optional strategy gate — callers build it from $selectedStrategyId /
   *    $strategyOpenSymbols so the TOTAL always sums the SAME rows that are
   *    visible above it.  Defaults to () => true (no filter).
   *  @returns {Record<string, number>} root → summed value
   */
  /** Page-local _perRootReduce wrapper — delegates to the pure helper in
   *  derivativesMath.js. Captures page-level reactive state (positions,
   *  simActive, selectedAccounts) so callers pass only accessor + matcher. */
  function _perRootReduce(accessor, /** @type {(sym: string) => boolean} */ matchStrategy = (_s) => true) {
    const wantedSource = simActive ? 'sim' : 'live';
    const matchAccount = buildAcctMatcher(selectedAccounts);
    return perRootReduce({
      positions, wantedSource,
      matchAccount, matchStrategy,
      decomposeSymbol,
      // Live-first (§4/§5): _undLive (liveSnap of the resolved front-month
      // tradingsymbol, same Tier-1 source liveSpot uses) takes priority,
      // matching the priority reversal already applied in portfolioStore's
      // exp_pnl block and _legExpPnlDisplay, then falls back to _rootSpot's
      // own multi-source chain. This tick-driven `spot` is the Exp P&L
      // DISPLAY basis (§4) — it is NOT used for Extrinsic (item-2 fix):
      // legExtrinsicDisplay reads `c.underlying_ltp` (the row's own
      // poll-time field, now carried by buildPositionRowFromBroker)
      // directly instead, so the exp-P&L and MTM terms of that
      // subtraction share one poll-time snapshot rather than mixing a
      // live tick with a poll-only price.
      getSpot: (root, p) => {
        const live = _undLive[root]?.ltp;
        if (live > 0) return live;
        return untrack(() => _rootSpot(root)) || 0;
      },
      accessor,
    });
  }

  /** Builds the same strategy-gate closure used by _byUnderlyingTotals /
   *  _byUnderlyingExp.  Call this inside a $derived.by() so reads of
   *  $selectedStrategyId + $strategyOpenSymbols are tracked.
   *  Delegates to buildStrategyMatcher in derivativesMath.js. */
  function _makeStrategyMatcher() {
    return buildStrategyMatcher($selectedStrategyId, $strategyOpenSymbols);
  }


  // Snapshot filter consistency (§5): rows are filtered by account/strategy
  // (_byUnderlyingTotals via matchAccount/matchStrategy), but Day/P&L/Exp
  // values used to come from positionsDerivedStore.getByRoot /
  // byRootPositions — selection-independent by design, summed across ALL
  // accounts/strategies regardless of the active filter. Day P&L and
  // lifetime P&L are already correctly filtered on each _byUnderlyingTotals
  // row (g.day_without / g.pnl_without via rollupByUnderlying's own
  // matchAccount/matchStrategy) — those are used directly at each render
  // site instead of the unfiltered store. Exp P&L has no such filtered
  // per-root source yet, so it's computed here via the same _perRootReduce
  // infrastructure the overlay already uses (same iteration, same
  // _legExpPnlDisplay accessor, same matchStrategy gate) so filtered rows
  // and the filtered TOTAL are internally consistent by construction.
  const _filteredExpPnlByRoot = $derived.by(() => {
    const matchStrategy = _makeStrategyMatcher();
    return _perRootReduce((c, spot) => _legExpPnlDisplay(c, spot), matchStrategy);
  });

  // Extrinsic, filtered by the same account/strategy gate as the other four
  // Snapshot columns (§4/item-4 fix). Computed PER ROW via the shared
  // legExtrinsicDisplay (expiryPnl.js) — NOT via positionsDerivedStore.get()
  // (item-2 fix, round 4): that store's byKey/byRoot maps are already
  // summed across every account for a given symbol, but this reduction
  // walks PER-ACCOUNT position rows — indexing the pre-summed store from
  // inside a per-row walk added the same cross-account total once per
  // account row it touched (2 accounts on the same symbol → 2× the real
  // number, filtering to one account still showed the full cross-account
  // sum instead of that account's own share). legExtrinsicDisplay takes
  // this row's OWN qty/avg_cost/ltp + its OWN poll-time underlying_ltp, so
  // perRootReduce's normal per-row accumulation (already correct for
  // everything else) sums real per-account contributions instead.
  const _filteredExtrinsicByRoot = $derived.by(() => {
    const matchStrategy = _makeStrategyMatcher();
    return _perRootReduce((c) => legExtrinsicDisplay(c, Number(c?.underlying_ltp) || 0), matchStrategy);
  });

  /** Per-row Snapshot Exp P&L — uniform across EVERY root, including the
   *  currently selected one (item-3 fix, round 4). Always reads
   *  `_filteredExpPnlByRoot` — the same account/strategy-filtered
   *  reduction `_byUnderlyingTotals`' Day P&L / P&L columns are built from
   *  — so the Snapshot row's four value columns are computed on ONE
   *  consistent filter basis regardless of selection state.
   *
   *  Previously the selected root took a DIFFERENT path
   *  (`_legsExpPnlTotal`, sourced from `_legsTotalsBase` — expiry-picker
   *  filtered but NOT strategy-filtered) while every other root used
   *  `_filteredExpPnlByRoot` (strategy-filtered but NOT expiry-picker
   *  filtered). Confirmed repro: a strategy scoped to NIFTY 24500CE only,
   *  while the book also holds NIFTY 24000PE — with NIFTY selected the row
   *  showed CE+PE (unfiltered by strategy); deselecting NIFTY made the
   *  SAME row jump to CE-only (strategy-filtered) with no change to the
   *  book. Day P&L / P&L never had this problem (`g.day_without` /
   *  `g.pnl_without` are already uniformly strategy-filtered for every
   *  row) — Exp P&L was the one column that special-cased the selected
   *  root. `_legsExpPnlTotal` remains valid as its OWN thing: the
   *  Legs-tab TOTAL row, which is intentionally expiry-picker-scoped and
   *  what-if aware (leg checkboxes / drafts) — see its own docstring. It
   *  is no longer read from here. */
  function _rowExpPnlFor(/** @type {string} */ underlying) {
    return _filteredExpPnlByRoot[underlying] ?? 0;
  }

  // Snapshot TOTAL sums — sum ONLY the filtered per-root values so TOTAL
  // always equals the sum of the visible (filtered) rows above it.
  const _snapshotTotalPnl = $derived.by(() =>
    _byUnderlyingTotals.reduce((s, g) => s + (g.pnl_without ?? 0), 0)
  );
  const _snapshotTotalDay = $derived.by(() =>
    _byUnderlyingTotals.reduce((s, g) => s + (g.day_without ?? 0), 0)
  );
  const _snapshotTotalExp = $derived.by(() =>
    _byUnderlyingTotals.reduce((s, g) => s + _rowExpPnlFor(g.underlying), 0)
  );
  const _snapshotTotalExtrinsic = $derived.by(() =>
    _byUnderlyingTotals.reduce((s, g) => s + (_filteredExtrinsicByRoot[g.underlying] ?? 0), 0)
  );


  /** Per-underlying live quote map — { ROOT: { ltp, day_pct, prev_close } }.
   *  Populated by loadUnderlyingQuotes() via the shared underlyingSpotStore
   *  (one batchQuote per Snapshot poll). Drives the Spot / Day % / Prev Close
   *  columns. Missing roots render as "—". */
  /** @type {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>} */
  let _underlyingQuotes = $derived(underlyingSpotStore.value);
  /** Monotonic counter incremented each time _underlyingQuotes is replaced.
   *  Used as a reactive dependency in liveSpot and _clientPayoffStub when
   *  the market is closed — guarantees those derived values re-run after
   *  every batchQuote refresh off-market (where _throttledTick is frozen). */
  let _quoteGeneration = $state(0);
  /** Reactive LTP of the currently selected underlying from underlyingSpotStore.
   *  Updates when patchUnderlyingSpot patches _quotes on each anchor/Path-3 tick,
   *  so liveSpot re-derives per-tick instead of waiting for the 30s _quoteGeneration bump. */
  const _activeQuoteLtp = $derived(_underlyingQuotes[selectedUnderlying]?.ltp ?? 0);

  /** Map every Snapshot underlying → { root, quoteKey } via
   *  resolveUnderlying. Indices land on the spot tradingsymbol
   *  (NSE:NIFTY 50), MCX commodities land on the nearest future,
   *  everything else lands on NSE:<root>. */
  const _underlyingQuoteKeys = $derived.by(() => {
    void instrumentsReady; // re-derive after instruments load so findNearestFuture resolves MCX contracts
    /** @type {Array<{ root: string, quoteKey: string }>} */
    const out = [];
    const seen = new Set();
    for (const g of _byUnderlyingTotals) {
      const r = resolveUnderlying(g.underlying, findNearestFuture);
      if (r?.quoteKey) { out.push({ root: g.underlying, quoteKey: r.quoteKey }); seen.add(g.underlying); }
    }
    // Include selectedUnderlying when not already covered by portfolio positions so
    // freshly-selected MCX virtual roots (e.g. GOLDM with no open positions) get
    // their front-month futures subscribed to KiteTicker via loadUnderlyingQuotes.
    if (selectedUnderlying && !seen.has(selectedUnderlying)) {
      const r = resolveUnderlying(selectedUnderlying, findNearestFuture);
      if (r?.quoteKey) out.push({ root: selectedUnderlying, quoteKey: r.quoteKey });
    }
    // Anchor contract subscription (§5): strategy.spot_anchor_contract can be
    // a DIFFERENT contract than the front-month resolution above (e.g. a
    // far-month MCX option chain whose pricing anchor isn't the front-month
    // future). Without a dedicated subscription entry here, liveSpot/
    // payoffSpot's Tier-1 anchor read (liveSnap(anchor)?.ltp) never gets a
    // batch-quote refresh or ticker subscription and can silently serve a
    // days-old localStorage-cached price. Reuses the underlying's own
    // resolved exchange (MCX/CDS anchors always share the underlying's
    // exchange) since the strategy response carries no exchange field.
    const _stratUnd = String(strategy?.underlying || '').toUpperCase();
    const _anchor    = String(strategy?.spot_anchor_contract || '').toUpperCase();
    if (_anchor && !out.some(p => p.quoteKey.endsWith(`:${_anchor}`))) {
      const r = resolveUnderlying(_stratUnd || selectedUnderlying, findNearestFuture);
      const exch = r?.exchange || 'MCX';
      out.push({ root: `${_anchor}__ANCHOR`, quoteKey: `${exch}:${_anchor}` });
    }
    return out;
  });

  // Tick-flash instance — one helper covers every directional cell in
  // the Snapshot grid. Threshold 0 (any change triggers) because the
  // 30 s polling cadence already filters out sub-second jitter, and
  // 350 ms decay so the operator sees the pulse without it competing
  // with the next poll cycle. Keyed as `<root>:<field>` so each cell
  // has its own timer (Spot moves but Day % may not on the same tick).
  // pctThreshold seeded from the admin setting at page-mount time.
  // loadLtpFlashPct() is called in onMount (fire-and-forget) so the
  // store is primed for the next mount; get() reads the current value
  // which is either the default (0.1) or a previously-loaded value.
  const flash = createTickFlash({ threshold: 0, pctThreshold: get(ltpFlashPct), durationMs: 300 });
  // Keep pctThreshold live — operator may change ui.ltp_flash_pct in-session.
  // get() seeds the initial value; subscribe() ensures subsequent updates land.
  const _unsubFlashPct = ltpFlashPct.subscribe(v => flash.setPctThreshold(v));

  // Drive the flash off the polled data. Two effects — one for the
  // F&O rollup (Day / P&L / Day Net / P&L Net) and one for the live
  // quote map (Spot / Day %). Prev Close skipped: doesn't change
  // intraday, so a flash would be a false signal of "fresh data".
  //
  // NOTE — intentional deviation from the LTP-cascade pattern used on
  // PerformancePage and MarketPulse: those pages have one LTP source
  // per position row, so the cascade rule "LTP direction drives all
  // derived cells" is unambiguous. Here each by-underlying rollup
  // aggregates N legs across multiple instruments — there is no single
  // LTP event that dominates the row. Per-field poll-diff flash is
  // therefore the semantically correct choice; cascade would require an
  // arbitrary tie-break and would mislead the operator. Spot / Day %
  // cells DO use the `${root}:ltp` key, so the underlying quote flash
  // is still independent and accurate.
  $effect(() => {
    const groups = _byUnderlyingTotals;
    untrack(() => {
      for (const g of groups) {
        flash.update(`${g.underlying}:day_w`,  g.day_without ?? 0);
        flash.update(`${g.underlying}:pnl_w`,  g.pnl_without);
      }
    });
  });
  $effect(() => {
    const quotes = _underlyingQuotes;
    untrack(() => {
      for (const [root, q] of Object.entries(quotes)) {
        flash.update(`${root}:ltp`, q?.ltp);
      }
    });
  });

  // Payoff header chips — EV + Greeks (Δ Γ Θ 𝒱 ρ).
  // Shell guard: synth equity-only strategy has iv_proxy=0 and
  // days_to_expiry=0; those are placeholder zeros, not real ticks,
  // so we skip the flash update when the strategy is a shell.
  $effect(() => {
    if (!strategy || (!strategy.iv_proxy && !strategy.days_to_expiry)) return;
    const g  = _mergedGreeks ?? strategy.aggregate_greeks;
    const ev = _mergedEv;
    untrack(() => {
      flash.update('payoff:ev',    ev);
      flash.update('payoff:delta', g?.delta);
      flash.update('payoff:gamma', g?.gamma);
      flash.update('payoff:theta', g?.theta);
      flash.update('payoff:vega',  g?.vega);
      flash.update('payoff:rho',   g?.rho);
    });
  });

  // Aggregate kv-block — POP, EV, R:R (same shell guard).
  $effect(() => {
    if (!strategy || (!strategy.iv_proxy && !strategy.days_to_expiry)) return;
    const pop    = _mergedPop;
    const ev     = _mergedEv;
    const evPct  = _mergedEvPct;
    const risk   = _mergedRisk ?? strategy.risk;
    untrack(() => {
      flash.update('kv:pop',    pop);
      flash.update('kv:ev',     ev);
      flash.update('kv:ev_pct', evPct);
      flash.update('kv:max_profit', risk?.max_profit);
      flash.update('kv:max_loss',   risk?.max_loss);
    });
  });

  // Legs per-row: Day P&L, P&L, Exp P&L (keyed by account|symbol).
  $effect(() => {
    const spot       = untrack(() => liveSpot);
    const candidates = candidatePositions;
    untrack(() => {
      for (const c of candidates) {
        const k = `${c.account ?? ''}|${c.symbol ?? ''}`;
        flash.update(`leg:${k}:day`, positionsDerivedStore.get(c.symbol).day_pnl);
        flash.update(`leg:${k}:pnl`, c.pnl != null ? Number(c.pnl) : null);
        flash.update(`leg:${k}:exp`, _legExpPnlDisplay(c, spot ?? null));
        flash.update(`leg:${k}:ltp`, c.ltp != null ? Number(c.ltp) : null);
        flash.update(`leg:${k}:chg`, positionsDerivedStore.get(c.symbol).chg_pct);
      }
    });
  });

  // Snapshot TOTAL row.
  $effect(() => {
    const pnl = _snapshotTotalPnl;
    const exp = _snapshotTotalExp;
    const day = _snapshotTotalDay;
    untrack(() => {
      flash.update('total:day', day);
      flash.update('total:pnl', pnl);
      flash.update('total:exp', exp);
    });
  });

  async function loadUnderlyingQuotes() {
    const pairs = untrack(() => _underlyingQuoteKeys);
    if (pairs.length === 0) return;
    debugLog('payoff:bq', 'request', { keys: pairs.map(p => p.quoteKey) });
    try {
      // Delegate to the shared store — batchQuote + publishPulseQuotes +
      // store update happen in one place so NavStrip and this page share
      // the same underlying spot prices without a second network call.
      await _loadUnderlyingSpotsFn(pairs);
      _quoteGeneration++;
      debugLog('payoff:bq', 'result', Object.fromEntries(
        Object.entries(underlyingSpotStore.value).map(([k, v]) => [k, v?.ltp])
      ));
    } catch (_) { /* leave previous values up — chip stays */ }
  }

  /**
   * Accumulate one F&O position into the TOTAL row accumulator.
   * Mutates `t` in place.
   * Returns false when the row should be skipped (wrong source / account / kind).
   *
   * @param {any}    p             - position row
   * @param {string} wantedSource  - 'sim' | 'live'
   * @param {Function} matchAccount
   * @param {object} t             - running totals object
   * @returns {boolean}
   */
  function _accumulateFnOTotal(p, wantedSource, matchAccount, t) {
    if (p.source !== wantedSource) return false;
    if (!matchAccount(p.account)) return false;
    const sym = String(p.symbol || p.tradingsymbol || '').toUpperCase();
    if (!/FUT$|(CE|PE)$/i.test(sym)) return false;
    const qty = Number(p.quantity ?? p.qty) || 0;
    const pnl = Number(p.pnl) || 0;
    // SSOT: baseDayPnlForPosition uses daily_book settlement as base; keeps TOTAL in sync with NavStrip P1.
    const day = baseDayPnlForPosition(p);
    t.qty_fno      += qty;
    t.legs_without++;
    t.pnl_without  += pnl;
    t.day_without  += day;
    return true;
  }

  /** TOTAL row — sums F&O positions only so the rollup matches the
   *  NavStrip P1 slot exactly. */
  const _byUnderlyingTotal = $derived.by(() => {
    const wantedSource = simActive ? 'sim' : 'live';
    const matchAccount = buildAcctMatcher(selectedAccounts);
    const t = { qty_fno: 0, legs_without: 0, pnl_without: 0, day_without: 0 };
    for (const _p of positions) {
      _accumulateFnOTotal(/** @type {any} */ (_p), wantedSource, matchAccount, t);
    }
    return t;
  });

  /** Per-candidate Day P&L — computed per-row using baseDayPnlForPosition
   *  (poll-only — §1, no live-tick delta).
   *
   *  positionsDayPnlStore.byKey[sym] aggregates across ALL accounts
   *  for that symbol. candidatePositions has per-account rows so using
   *  byKey would double-count when two accounts hold the same symbol
   *  (e.g. CRUDEOIL26AUGPE5400 in two accounts → byKey returns 2×).
   *  Fix: apply baseDayPnlForPosition directly using each candidate's own
   *  fields, which is exactly what the store does per-row before aggregating.
   */
  const _candDayPnl = (c) => baseDayPnlForPosition(c);

  /** Lookup map: symbol → backend leg analytics (greeks, iv, …) from
   *  the latest strategy response. Lets the Candidates panel show
   *  per-row IV / Δ / Θ / 𝒱 without a second endpoint. */
  const legAnalyticsBySymbol = $derived.by(() => {
    /** @type {Record<string, any>} */
    const out = {};
    if (!strategy?.legs) return out;
    for (const l of strategy.legs) out[l.symbol] = l;
    return out;
  });

  // Distinct underlyings + accounts derived from the loaded positions.
  // Falls back to the major indices when the operator hasn't loaded a
  // book yet so the dropdowns never appear empty.
  const accountChoices = $derived.by(() => {
    const accts = new Set();
    for (const p of positions) {
      if (p.account) accts.add(p.account);
    }
    // Union broker registry so chain picker / leg drops aren't blocked
    // pre-market when positions[] is empty (or holiday / weekend).
    for (const a of realAccounts) {
      if (a) accts.add(a);
    }
    // Use canonical display order (tracked via _derivOrderMap $state so the
    // derived re-fires when fetchBrokerOrder() resolves after cold load).
    return sortAccountsBy(Array.from(accts), _derivOrderMap);
  });
  /** Two roots-with-positions sets so the picker can sort + color them
   *  separately. `_rootsWithOptions` carries underlyings where the
   *  operator holds at least one CE or PE — these are the most
   *  actively-analysed roots (the page is built for option payoff
   *  analysis), so they sort to the top of the picker AND get a
   *  cyan-highlighted label. `_rootsWithFuturesOnly` carries
   *  underlyings where the only open derivative is a future — still
   *  shown without dimming but ranked second. Operator: "in the symbol
   *  dropdown, if options position exists for underlying, color code
   *  the root differently, show them first in default order." */
  // Underlying dropdown narrows to roots with a derivative position
  // in the filtered accounts — picking ZG0790 hides CRUDEOIL when
  // it's only on DH6847. Legs panel applies the same account filter
  // so the analysis stays consistent with the picker scope.
  const _accountAllow = $derived.by(() => {
    if (selectedAccounts.length === 0) return null;
    return new Set(selectedAccounts.map(String));
  });
  const _rootsWithOptions = $derived.by(() => {
    const set = new Set();
    const allow = _accountAllow;
    for (const p of positions) {
      if (allow && !allow.has(String(p.account || ''))) continue;
      if (!/(CE|PE)$/i.test(p.symbol)) continue;
      const u = p.symbol.replace(/\d.*$/, '');
      if (u) set.add(u);
    }
    return set;
  });
  const _rootsWithFuturesOnly = $derived.by(() => {
    const set = new Set();
    const allow = _accountAllow;
    for (const p of positions) {
      if (allow && !allow.has(String(p.account || ''))) continue;
      if (!/FUT$/i.test(p.symbol)) continue;
      const u = p.symbol.replace(/\d.*$/, '');
      if (u && !_rootsWithOptions.has(u)) set.add(u);
    }
    return set;
  });
  const underlyingChoicesFromBook = $derived.by(() => {
    // Order = options-first (sorted), then futures-only (sorted) — both
    // carry derivative positions; the split exists for visual coding +
    // ranking only. Each MCX variant (CRUDEOIL vs CRUDEOILM, GOLD vs
    // GOLDM vs GOLDPETAL vs GOLDTEN vs GOLDGUINEA, etc.) keeps its own
    // entry — they're separately tradable contracts with different
    // lot sizes / tick sizes and need their own payoff chart.
    const opts = [..._rootsWithOptions].sort();
    const futs = [..._rootsWithFuturesOnly].sort();
    return [...opts, ...futs];
  });

  /** F&O-eligible holdings the operator has NO derivative position
   *  in. No longer surfaced as its own picker tier (holdings now feed
   *  the picker's Tier 3 directly). Retained because the auto-check
   *  eq-leg $effect below reads it to decide "did the operator just
   *  pick a hedge stock they hold?" — in which case the matching eq
   *  row is pre-checked so the covered-call / collar payoff shape
   *  reflects the underlying position without an extra click.
   *
   *  Excludes anything already in `underlyingChoicesFromBook` so the
   *  auto-check doesn't fire when the same root also has a real
   *  derivative position (regular flow: eq leg stays default-OFF).
   *  Gated on `instrumentsReady` because getOptionUnderlyingLot
   *  requires the instruments cache. */
  const _hedgeOpportunities = $derived.by(() => {
    if (!instrumentsReady || !_positionsLoaded) return [];
    const have = new Set(underlyingChoicesFromBook);
    const set = new Set();
    for (const h of holdings) {
      const sym = String(h?.symbol || '').toUpperCase();
      if (!sym) continue;
      // (a) Direct hedge — operator holds the literal underlying, and
      //     that underlying has F&O contracts listed at Kite. Same as
      //     the original hedge-opp surface: covered-call analysis on
      //     a stock you hold.
      if (!have.has(sym) && getOptionUnderlyingLot(sym) > 0) set.add(sym);
      // (b) Proxy hedge — operator holds an ETF (GOLDBEES, NIFTYBEES
      //     etc.) whose tracked target has its own F&O book at Kite.
      //     Stage 2 mapping lives in the `hedge_proxies` DB table
      //     edited via /admin/settings. Picking the target from the
      //     dropdown auto-checks the proxy eq leg; the conversion
      //     factor (dynamic/static/beta) + correlation come from the
      //     row. Read proxyTableReady so this derived re-runs when
      //     the async load completes (otherwise the page would
      //     hydrate against an empty cache and never refresh).
      void proxyTableReady;
      for (const t of targetsForProxy(sym)) {
        if (have.has(t)) continue;          // already covered by positions tier
        set.add(t);
      }
    }
    return Array.from(set).sort();
  });

  // Auto-union late-arriving broker accounts into selectedAccounts.
  // Fires whenever accountChoices changes (positions reload or
  // realAccounts refresh). Without this, a Dhan account loaded AFTER
  // the operator's selectedAccounts was restored from sessionStorage
  // would silently filter every Dhan row out of the Candidates panel
  // and the strategy basket — even though Dhan rows are in `positions`
  // and the account dropdown shows the Dhan code in its options. Same
  // bug pattern + same fix shape as MarketPulse's stage (b) seeder.
  // No-op when selectedAccounts is empty (= show all): the operator
  // hasn't curated a subset, so there's nothing to extend.
  $effect(() => {
    const choices = accountChoices;
    if (!choices.length) return;
    const newAccts = choices.filter(a => !_seenAccts.has(a));
    if (newAccts.length === 0) return;
    if (selectedAccounts.length > 0) {
      const cur = new Set(selectedAccounts);
      for (const a of newAccts) cur.add(a);
      selectedAccounts = sortAccountsBy([...cur], _derivOrderMap);
    }
    // Mark every account in choices as seen — "has been offered to
    // the operator in the picker, they had a chance to react." If
    // they're picked into selectedAccounts later, that's the
    // operator's call. Auto-union only fires when an account appears
    // AFTER first being seen — i.e., a genuine late-arrival.
    for (const a of choices) _seenAccts.add(a);
    try {
      if (typeof sessionStorage !== 'undefined') {
        sessionStorage.setItem(
          'opt.seenAccounts', JSON.stringify([..._seenAccts]));
      }
    } catch (_) {}
  });

  /** Display labels for the Underlying picker. MCX commodity roots
   *  (CRUDEOIL / GOLDM / NATURALGAS / …) don't have a tradeable spot —
   *  the operator trades the front-month future. Show that contract's
   *  full symbol (e.g. "CRUDEOIL25JUNFUT") in the dropdown label so the
   *  picker reads as a concrete instrument instead of a placeholder
   *  category name. Indices (NIFTY / BANKNIFTY / FINNIFTY) keep the
   *  bare ticker — that IS the index spot.
   *
   *  Four-tier ordering (dedup — first occurrence wins):
   *    1. Virtual roots with OPTIONS in active positions (cyan 'options')
   *    2. Virtual roots with FUTURES in active positions ('futures')
   *    3. Roots in holdings — extracted from the equity symbol ('holdings')
   *    4. General underlyings — POPULAR_UNDERLYINGS whitelist ('popular')
   *
   *  Tiers 1-3 read from `positions` / `holdings` ($state arrays
   *  initialised to []). Tier 4 is a static hardcoded whitelist. No
   *  gate on `instrumentsReady` — the list is always available
   *  immediately on cold start so the picker + payoff card mount
   *  without waiting for the instruments cache. */
  const underlyingOptionsForPicker = $derived.by(() => {
    const seen = new Set();
    const out = [];
    // Build a position-count map for roots so that roots with more legs
    // surface first within each tier (secondary sort: alphabetical).
    const _rootQtySum = new Map();
    const _allow = _accountAllow;
    for (const p of positions) {
      if (_allow && !_allow.has(String(p.account || ''))) continue;
      const r = p.symbol.replace(/\d.*$/, '');
      if (r) _rootQtySum.set(r, (_rootQtySum.get(r) || 0) + Math.abs(Number(p.qty || 0)));
    }
    // Tier 1 — Options positions on this root. Cyan-highlighted label
    // + 'options' hint chip. Sorted by position count desc, then alpha.
    for (const u of [..._rootsWithOptions].sort((a, b) =>
      (_rootQtySum.get(b) || 0) - (_rootQtySum.get(a) || 0) || a.localeCompare(b))) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'options', qtySum: _rootQtySum.get(u) || 0 });
    }
    // Tier 2 — Futures positions on this root (no options). Default
    // colour, 'futures' hint chip. Sorted by position count desc, then alpha.
    for (const u of [..._rootsWithFuturesOnly].sort((a, b) =>
      (_rootQtySum.get(b) || 0) - (_rootQtySum.get(a) || 0) || a.localeCompare(b))) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'futures', qtySum: _rootQtySum.get(u) || 0 });
    }
    // Tier 3 — Roots present in cash-equity holdings. Extract the bare
    // symbol from each holding row (already uppercased in
    // buildHoldingRowFromBroker). Same account filter the positions
    // tiers apply so picking an account narrows this tier too.
    const _holdingsRoots = new Set();
    const allow = _accountAllow;
    for (const h of holdings) {
      if (allow && !allow.has(String(h.account || ''))) continue;
      const sym = String(h?.symbol || '').toUpperCase();
      if (sym) _holdingsRoots.add(sym);
    }
    for (const u of [..._holdingsRoots].sort()) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'holdings', qtySum: 0 });
    }
    // Tier 4 — Pinned watchlist F&O underlying roots. Comes before regular
    // watchlists so operator-curated pinned symbols surface above ordinary lists.
    // Populated by loadDefaultWatchlist() → _extractFOUnderlyingRoots(pinnedSyms).
    for (const u of _pinnedWatchlistRoots) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'pinned', qtySum: 0 });
    }
    // Tier 5 — Non-pinned watchlist F&O underlying roots. Populated from
    // regularSyms; appears after pinned watchlist and before popular fallback.
    for (const u of _regularWatchlistRoots) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'watchlist', qtySum: 0 });
    }
    // Tier 6 — Popular/liquid F&O underlyings (NIFTY, BANKNIFTY,
    // RELIANCE, …). Always emitted — the operator sees the popular
    // list appended below their own book so switching to any liquid
    // symbol is one click, even when they already hold positions.
    // Not gated on instrumentsReady: the list is a static hardcoded
    // whitelist so the picker + payoff card seed immediately on cold
    // start (no positions, no holdings, no instruments cache yet).
    for (const u of POPULAR_UNDERLYINGS) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u, hint: 'popular', qtySum: 0 });
    }
    return out;
  });

  // Auto-select the best underlying when the page lands without a
  // cached selection. Reads the first active-position entry from the
  // picker list. Cold-start with no book AND no holdings lands on
  // POPULAR_UNDERLYINGS[0] = 'NIFTY'; when the operator has real
  // positions or holdings, those take precedence. Untracks the
  // selectedUnderlying read so operator-driven changes don't re-trigger
  // the fallback logic.
  //
  // One-time promote (Fix 1): fires once when positions first load and
  // the current provisional selection (watchlist/pinned) has no active
  // qty. _autoSelectDone guards against re-firing on every 5s refresh
  // so the operator can manually pick an inactive underlying (e.g.
  // COPPER for analysis) without being bounced back.
  $effect(() => {
    // Track EVERY upstream source explicitly. The picker's `$derived.by`
    // recomputes when any of these change, but reading only the derived
    // wasn't reliably propagating in production builds — the effect
    // fired once on mount (opts empty) and never re-fired when the
    // stores hydrated. Belt + suspenders: touch each source AND the
    // picker itself so any of them re-firing wakes the effect.
    void positions; void holdings; void _rootsWithOptions; void _rootsWithFuturesOnly;
    void _positionsLoaded; void _pinnedWatchlistRoots; void _regularWatchlistRoots;
    const opts = underlyingOptionsForPicker;
    const cur  = untrack(() => selectedUnderlying);
    // First option with active positions; fallback to opts[0]
    const firstActive = opts.find(o => (o.qtySum || 0) > 0) ?? opts[0];

    // Standard case: nothing selected yet — pick first active (skip popular if positions exist).
    if (!cur) {
      const first = (firstActive ?? opts[0])?.value;
      if (first) untrack(() => { selectedUnderlying = first; });
      return;
    }

    const curInOpts = opts.find(o => o.value === cur);
    if (!curInOpts && opts[0]?.value) {
      // Stale cache: previously-selected underlying is no longer in options — reset to first.
      // Covered by Playwright spec derivatives-payoff-default.
      untrack(() => { selectedUnderlying = opts[0].value; });
      return;
    }

    const curIsPopular = curInOpts?.hint === 'popular';
    const curHasActiveQty = (curInOpts?.qtySum || 0) > 0;
    const bestHasActiveQty = (firstActive?.qtySum || 0) > 0;

    // Promote: popular provisional → any position tier (existing logic, unchanged).
    if (curIsPopular && firstActive?.hint !== 'popular') {
      untrack(() => { selectedUnderlying = firstActive.value; });
      return;
    }

    // One-time promote: fires when positions first load and current is a non-active
    // provisional (watchlist/pinned selected before positions were available).
    // Guard: _autoSelectDone prevents re-firing on every subsequent 5s position refresh
    // so the operator can manually pick an inactive underlying (e.g. COPPER for analysis)
    // without being immediately bounced back to the active one.
    if (!untrack(() => _autoSelectDone) && _positionsLoaded && !curIsPopular && !curHasActiveQty && bestHasActiveQty) {
      untrack(() => {
        _autoSelectDone = true;
        selectedUnderlying = firstActive.value;
      });
    }
  });

  // Auto-check the eq leg when the operator picks a hedge-opportunity
  // underlying (a held stock with no existing derivative position).
  // The intent of picking from that group is clearly "model a covered-
  // call / collar against this stock I hold" — making the eq leg opt-
  // in would force a click that's already implicit in the pick. Eq
  // legs of derivative-position roots stay default-OFF (regular flow).
  $effect(() => {
    void selectedUnderlying;
    untrack(() => {
      const target = selectedUnderlying;
      if (!target) return;
      if (!_hedgeOpportunities.includes(target)) return;
      // Pre-check every matching eq row's key, BUT only when the key
      // is untouched (=== undefined). Respect explicit false — if the
      // operator unchecked the row, switched underlying, and came
      // back, the auto-check shouldn't override their choice.
      // candidatePositions may not have materialised on first pick
      // (holdings still loading), so we seed against every account
      // that holds this root — operator's holdings list is the
      // source of truth.
      const next = { ...enabledSymbols };
      let touched = false;
      // Direct hedge — operator holds the literal underlying.
      for (const h of holdings) {
        if (String(h?.symbol || '').toUpperCase() !== target.toUpperCase()) continue;
        const key = `${h.account || ''}|${target.toUpperCase()}`;
        if (next[key] === undefined) {
          next[key] = true;
          touched = true;
          _persistEqMemory({ account: h.account, symbol: target.toUpperCase() }, true);
        }
      }
      // Proxy hedge — operator holds an ETF (GOLDBEES → GOLD etc.).
      // Same opt-in semantics — the pick is the consent, the key is
      // by the proxy's actual symbol so the operator can selectively
      // un-check it without losing the memory for OTHER targets the
      // same proxy hedges.
      const _allowed = new Set(proxiesForTarget(target));
      if (_allowed.size) {
        for (const h of holdings) {
          const psym = String(h?.symbol || '').toUpperCase();
          if (!_allowed.has(psym)) continue;
          const key = `${h.account || ''}|${psym}`;
          if (next[key] === undefined) {
            next[key] = true;
            touched = true;
            _persistEqMemory({ account: h.account, symbol: psym }, true);
          }
        }
      }
      if (touched) enabledSymbols = next;
    });
  });

  // Clear strategy IMMEDIATELY on underlying change. Operator:
  // "for a brief moment, the payoff showed wrong info for spot
  // price and making payoff chart invalid". The loadStrategy
  // underlying-mismatch check inside the function already clears
  // strategy when it detects a switch — BUT the loadStrategy
  // $effect runs AFTER legs is recomputed, which happens AFTER
  // candidatePositions re-derives off selectedUnderlying. During
  // those few frames the OLD strategy (with old spot, old anchor,
  // old payoff curve) is still on screen, briefly mismatched
  // against the new underlying label. Clearing here flips the
  // chart to its loading/empty state on the SAME frame as the
  // underlying change — no transient flash of wrong-spot.
  let _lastUnderlyingForChart = $state('');
  $effect(() => {
    const u = selectedUnderlying;
    untrack(() => {
      // Do NOT null out `strategy` on underlying change — the next
      // successful loadStrategy() overwrites atomically. Blanking here
      // caused the payoff card to unmount + remount on every pick,
      // which the operator flagged as "disappearing payoff chart."
      // The brief visual mismatch (old spot marker on new-labeled card)
      // is far less disruptive than the full-card unmount.
      _lastUnderlyingForChart = u;
    });
  });

  // Immediately trigger strategy analytics whenever the operator picks
  // a different underlying. Without this, loadStrategy() only fires on
  // the 5 s marketAwareInterval tick — so switching symbols causes up
  // to 5 s of the wrong underlying's payoff being shown.
  // `untrack` prevents candidatePositions / legs reads inside
  // loadStrategy from adding reactive deps to this effect (they belong
  // to the deriveds, not this trigger).
  $effect(() => {
    void selectedUnderlying;
    untrack(() => { try { loadStrategy({ clear: true }); } catch (_) {} });
  });

  /** Distinct expiries (YYYY-MM-DD) the operator has positions /
   *  drafts on for the chosen underlying. Operator: "expiry should
   *  show only the expiry day values dropdown present in the
   *  positions." Reverted from the full-universe instruments-cache
   *  scan back to a book-only derivation so the picker reads as
   *  "what's actually in scope right now" rather than every listed
   *  contract Kite knows about. Drafts contribute too. */
  const expiryChoicesForUnderlying = $derived.by(() => {
    if (!instrumentsReady || !selectedUnderlying) return [];
    const target = selectedUnderlying.toUpperCase();
    // Strip _NEXT suffix before building the prefix regex — a URL-restored
    // underlying like "CRUDEOIL_NEXT" must still match instrument symbols
    // that start with the bare root "CRUDEOIL".
    const base = target.replace(/_NEXT$/i, '');
    const prefixRe = new RegExp(`^${base}\\d`, 'i');
    const set = new Set();
    const consider = /** @param {string} sym */ (sym) => {
      const upper = String(sym || '').toUpperCase();
      if (!upper || !prefixRe.test(upper)) return;
      const inst = getInstrument(upper);
      if (inst?.x) set.add(inst.x);
    };
    for (const p of positions)  consider(p.symbol);
    for (const d of _allDrafts) consider(d.symbol);
    return Array.from(set).sort();
  });

  /** Drop any selected expiries that have disappeared from the list
   *  (e.g. after switching underlying). Empty = all — no auto-pick. */
  $effect(() => {
    void selectedUnderlying;
    untrack(() => {
      const list = expiryChoicesForUnderlying;
      if (!list.length) {
        if (selectedExpiries.length) selectedExpiries = [];
        return;
      }
      const still = selectedExpiries.filter(e => list.includes(e));
      if (still.length !== selectedExpiries.length) selectedExpiries = still;
    });
  });

  // Candidate positions matching the filter. Live + sim positions on
  // the chosen underlying held in one of the chosen accounts, plus all
  // drafts whose symbol matches the underlying prefix. Source is a
  // per-row property (badge in the panel), not a mode-level filter.
  // Three sources appear in order: real → provisional (~) → draft store (D).
  /** @type {{symbol:string,account:string,qty:number,opening_qty?:number,avg_cost:number|null,ltp:number|null,prev_close?:number|null,pnl?:number,realised?:number,day_change_val?:number,underlying_ltp?:number,source:string,kind:string,exchange?:string,draftId?:number,_expiryStatus?:string,proxy_for?:string,proxy_kind?:string,_provisional?:boolean,_draft_store?:boolean}[]} */
  const candidatePositions = $derived.by(() => {
    if (!selectedUnderlying) return [];
    void instrumentsReady;  // re-derive when instruments cache warms (cold start drops MCX open positions)
    void proxyTableReady;   // re-derive when the proxy table loads
    // Reading the store Maps here registers reactive dependencies —
    // getProvisionalPositions() and getDraftPositions() return $state Maps
    // that are replaced on every applyFill / addDraftPosition call,
    // so this derived re-runs automatically on each mutation.
    return buildCandidatePositions({
      positions,
      holdings,
      drafts: _allDrafts,
      target:               selectedUnderlying.toUpperCase(),
      selectedExpiries,
      selectedAccounts,
      simActive,
      proxiesForTarget,
      getInstrument,
      provisionalPositions: getProvisionalPositions(),
      draftStorePositions:  getDraftPositions(),
    });
  });

  // Derived pair-button state — recomputes when enabledSymbols or candidatePositions change.
  const _pairState = $derived.by(() =>
    computePairEnabled(candidatePositions, _isLegEnabled)
  );
  const _pairEnabled        = $derived(_pairState.pairEnabled);
  const _pairedCandidates   = $derived(_pairState.pairedCandidates);
  const _firstCheckedSymbol = $derived(_pairState.firstCheckedSymbol);

  /** Count of opt/fut/eq rows that would have matched the underlying +
   *  expiry filter but are hidden by the account filter. Surfaces a
   *  hint chip so the operator knows the payoff is a partial view —
   *  the silent-hide bug the line 1066 comment block used to describe. */
  const hiddenByAccount = $derived.by(() => {
    if (!selectedAccounts.length || !selectedUnderlying) return { rows: 0, accts: 0 };
    const target       = selectedUnderlying.toUpperCase();
    const prefixRe     = new RegExp(`^${target}\\d`, 'i');
    const wantedSource = simActive ? 'sim' : 'live';
    const inFilter     = buildAcctMatcher(selectedAccounts);
    const inExpiry     = buildExpiryMatcher(selectedExpiries, getInstrument);
    const accts = new Set();
    let rows = 0;
    for (const p of positions) {
      if (p.source !== wantedSource) continue;
      const sym = p.symbol;
      if (!prefixRe.test(sym)) continue;
      if (!isFOSymbol(sym)) continue;
      if (!inExpiry(sym)) continue;
      if (inFilter(p.account)) continue;
      rows++; accts.add(String(p.account || ''));
    }
    const proxies = new Set(proxiesForTarget(target));
    for (const h of holdings) {
      const sym = String(h.symbol || '').toUpperCase();
      if (sym !== target && !proxies.has(sym)) continue;
      if (inFilter(h.account)) continue;
      rows++; accts.add(String(h.account || ''));
    }
    return { rows, accts: accts.size };
  });

  // Sum of broker P&L across CHECKED candidates only. The chart's
  // payoff curve is built from `legs` (also filtered by enabled
  // checkboxes), so the alignment target must use the same subset.
  // Earlier this summed every candidate — unchecking a leg dropped
  // it from the curve but kept it in the offset, so the chart's
  // TDAY didn't change visually. Now they stay in lock-step.
  // candidatePositions already enforces the expiry filter, so every
  // aggregate over it stays scoped to the selected expiry by
  // construction. P&L (realised + unrealised) from other expiries
  // is excluded at the candidate level — no per-aggregate re-gate
  // needed.
  const candidatesActualPnl = $derived.by(() => {
    let s = 0;
    for (const c of candidatePositions) {
      if (!_isLegEnabled(c)) continue;
      // Holdings toggle gate — when OFF, eq legs are excluded from
      // the chart everywhere else (`_equityLegs` returns [],
      // `displayedCandidates` filters them out). The chart offset
      // anchor MUST match that scope or the curve gets vertically
      // shifted by the broker P&L of holdings the chart isn't
      // actually showing.
      if (!_includeHoldings && c.kind === 'eq') continue;
      s += Number(c.pnl || 0);
    }
    return s;
  });

  // 250ms-throttled SSE-tick mirror. liveSpot + candidatesDayPnl read
  // it as their only tracked dep (the getSnapshot calls inside them
  // are wrapped in untrack), so they re-derive at 4Hz max regardless
  // of burst rate — same pattern PositionStrip uses to keep the main
  // thread free for click handlers. Without this, getSnapshot reads
  // inside $derived.by registered per-symbol SvelteMap deps and
  // re-derived at full SSE rate (~100 ticks/sec), saturating the
  // scheduler.
  let _throttledTick = $state(0);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _tickThrottleTimer = null;
  /** @type {(() => void) | null} */
  let _tickThrottleUnsub = null;
  /** @type {(() => void) | null} */
  let _derivTickUnsub = null;
  onMount(() => {
    // Prime the ltpFlashPct store from admin settings — fire-and-forget.
    // Any value loaded here applies on the next page mount (flash instance
    // is created at script-init time using get(ltpFlashPct) above).
    loadLtpFlashPct();

    _tickThrottleUnsub = symbolTickCount.subscribe(() => {
      if (_tickThrottleTimer) return;
      _tickThrottleTimer = setTimeout(() => {
        _throttledTick++;
        _tickThrottleTimer = null;
      }, 250);
    });

    // Tick-bus subscription — re-arms flash.update for Spot LTP and per-leg
    // LTP cells on every SSE tick, eliminating the 30 s poll-diff lag.
    // The underlying-quotes $effect already calls flash.update(`${root}:ltp`, q?.ltp)
    // on every batch-quote poll; this runs the same call immediately on each SSE tick
    // so the Spot cell flashes within 250 ms of the real market move.
    _derivTickUnsub = tickBus.subscribe(({ sym }) => {
      const root = sym.toUpperCase();

      // 1. Spot (by-underlying) LTP cell — re-arm if this sym is a tracked underlying.
      if (root in _underlyingQuotes) {
        const snap = getSnapshot(root);
        if (snap?.ltp != null) {
          flash.update(`${root}:ltp`, Number(snap.ltp));
          // Write through the shared store so NavStrip (getUnderlyingSpot)
          // sees per-tick patches, not just 30s poll refreshes.
          patchUnderlyingSpot(root, snap.ltp);
        }
      }

      // Anchor contract tick — no longer flashes the Snapshot card's LTP
      // cell (removed, secondary-defect fix): that cell displays the
      // FRONT-MONTH price (Option B), so feeding it anchor-contract flash
      // events under the same `${root}:ltp` key corrupted
      // createTickFlash's per-key baseline whenever anchor ≠ front-month
      // (alternating anchor/front-month values registered as large
      // spurious deltas, flipping the flash direction every tick). The
      // Payoff overlay's own marker flash (`OptionsPayoff.svelte`'s
      // internal `_spotFlash`) already reacts independently to its own
      // `spot` prop (`payoffSpot`, anchor-aware) — no bridging needed
      // here. debugLog kept for diagnostics.
      const _anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
      const _stratUnd = String(strategy?.underlying || '').toUpperCase();
      if (_anchor && root === _anchor && _stratUnd && _stratUnd in _underlyingQuotes) {
        const _as = getSnapshot(root);
        if (_as?.ltp != null) {
          debugLog('payoff:anchor', 'tick', { root, stratUnd: _stratUnd, ltp: Number(_as.ltp) });
        }
      }

      // Path 3: resolved quoteKey tradingsymbol — covers MCX when spot_anchor_contract is null.
      // Fires whenever Path 1 (direct root key) missed this sym. NOTE: no longer
      // excludes root === _anchor — in the common case the strategy's anchor
      // contract IS the front-month contract (same tradingsymbol), and with
      // Path 2's store-patch removed above, this is now the ONLY path that
      // patches the store for that tick. Excluding it here would silently
      // drop the store patch back to poll-rate (~30s) for every root whose
      // anchor happens to equal its front-month contract — the common case.
      // When anchor is a genuine far-month contract (≠ front-month), this
      // loop simply finds no matching _underlyingQuoteKeys entry for it, so
      // no double-patch / race is reintroduced.
      if (!(root in _underlyingQuotes)) {
        for (const { root: und, quoteKey } of _underlyingQuoteKeys) {
          const _ts = quoteKey.includes(':') ? quoteKey.split(':')[1].toUpperCase() : '';
          if (_ts === root && und in _underlyingQuotes) {
            const _ps = getSnapshot(root);
            if (_ps?.ltp != null) {
              flash.update(`${und}:ltp`, Number(_ps.ltp));
              patchUnderlyingSpot(und, _ps.ltp);
            }
            break;
          }
        }
      }

      // 2. CandidateLegRow LTP cells — re-arm for each leg whose symbol matches.
      for (const c of candidatePositions) {
        if ((c.symbol ?? '').toUpperCase() === root) {
          const k = `${c.account ?? ''}|${c.symbol ?? ''}`;
          const snap = getSnapshot(root);
          if (snap?.ltp != null) {
            flash.update(`leg:${k}:ltp`, Number(snap.ltp));
            flash.update(`leg:${k}:chg`, positionsDerivedStore.get(c.symbol).chg_pct);
          }
        }
      }
    });
  });
  onDestroy(() => {
    if (_tickThrottleTimer) clearTimeout(_tickThrottleTimer);
    _tickThrottleUnsub?.();
    _derivTickUnsub?.();
  });

  // Sum of day_change_val across enabled candidates. Surfaced as the
  // DAY row in the OptionsPayoff overlay so the operator can reconcile
  // it with the PositionStrip's P∆ chip (= sum of day_change_val
  // across the ENTIRE position book). Subset relationship: if every
  // position in the strip belongs to the chart's underlying AND every
  // candidate is enabled, DAY equals the strip's P∆ exactly. Useful
  // for "is my chart showing what the strip says?" sanity checks.
  // Live underlying spot for the payoff chart. server-side `strategy.spot`
  // is a per-poll snapshot; overriding with the SSE-tick LTP from
  // symbolStore makes the SPOT row + TDAY / EXP / spot vertical line +
  // curveAtSpot interpolation all track the underlying live. Prefer
  // strategy.spot_anchor_contract (the actual quote symbol — for MCX
  // commodity options the "spot" anchor is the front-month future), fall
  // back to strategy.underlying, fall back to the server value.

  // liveSpot — spot price for the payoff diagram and Greeks.
  //
  // Only SSE-tick sources are trusted (ltp_ts is live). Tiers 3-5 were
  // removed: underlying_ltp (backend-polled, stale), strategy.spot
  // (backend 5s poll, stale), and getSnapshot(root) (bare commodity root
  // like "GOLDM" never matches a tick key in symbolStore). If no SSE tick
  // is available yet, show blank rather than masking with a stale value.
  //
  // Tiers (SSOT: same sources as snapshot rows so overlay and table stay in sync):
  //   1  — _undLive[selectedUnderlying]?.ltp  (liveSnap of resolved tradingsymbol, per-root map)
  //   2  — _activeQuoteLtp                  (underlyingSpotStore: batchQuote + tickBus patches)
  //   3a — strategy anchor contract SSE tick (cold-start fallback when store not yet populated)
  //   3b — strategy underlying SSE tick
  const liveSpot = $derived.by(() => {
    // Tier 1: per-root map — exact same derivation as snapshot rows (_undLive[g.underlying])
    const _selLtp = _undLive[selectedUnderlying]?.ltp;
    if (_selLtp > 0) {
      untrack(() => debugLog('payoff:spot', 'resolved', { tier: '1-undLive', value: _selLtp }));
      return _selLtp;
    }

    // Tier 2: underlyingSpotStore (batchQuote + tickBus Path 3 patches at WebSocket speed)
    if (_activeQuoteLtp > 0) {
      untrack(() => debugLog('payoff:spot', 'resolved', { tier: '2-activeQuoteLtp', value: _activeQuoteLtp }));
      return _activeQuoteLtp;
    }

    // Tier 3: strategy anchor / underlying SSE ticks — cold-start only, before store is warm
    const stratUnd = String(strategy?.underlying || '').toUpperCase();
    if (stratUnd && stratUnd === selectedUnderlying) {
      const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
      if (anchor) {
        const v = liveSnap(anchor)?.ltp;
        if (v > 0) {
          untrack(() => debugLog('payoff:spot', 'resolved', { tier: '3a-anchor', anchor, value: v }));
          return v;
        }
      }
      const v = liveSnap(stratUnd)?.ltp;
      if (v > 0) {
        untrack(() => debugLog('payoff:spot', 'resolved', { tier: '3b-stratUnd', sym: stratUnd, value: v }));
        return v;
      }
    }

    untrack(() => debugLog('payoff:spot', 'unresolved', { selectedUnderlying }));
    return undefined;
  });

  // payoffSpot — spot price specifically for the Payoff overlay's marker.
  // Operator decision: the payoff curve prices against the strategy's
  // actual ANCHOR contract, not front-month — chartTheoreticalAtSpot,
  // _mergedEv, _mergedPop and chainSpot all already read `strategy.spot`
  // (the backend's anchor-contract resolution, reliable since the
  // cold-start override fix above), so the marker must track the same
  // contract or it won't sit where the curve says it should. A contango
  // MCX far-month strategy would otherwise show the marker at the
  // front-month price while the curve is priced on the anchor.
  const payoffSpot = $derived.by(() => {
    const stratUnd = String(strategy?.underlying || '').toUpperCase();
    if (stratUnd && stratUnd === selectedUnderlying) {
      // Tier 1: live tick on the exact anchor contract (MCX/CDS — the
      // strategy's own matching-expiry future).
      const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
      if (anchor) {
        const v = liveSnap(anchor)?.ltp;
        if (v > 0) return v;
      } else {
        // Tier 1b (C2 fix): NSE underlyings have no anchor future — the
        // backend prices strategy.spot off the cash/index ticker
        // (underlying_ltp_key: NIFTY→"NIFTY 50", stock→"NSE:<sym>"), the
        // exact same ticker `resolveUnderlyingTradingsymbol` resolves to
        // for an index/stock root (verified: both map to the SAME cash
        // ticker, not a future — the b1b946a8 basis-mismatch class does
        // NOT apply here). Reuse liveSpot's own Tier-1 lookup (`_undLive`,
        // same live-tick source, same resolved symbol) instead of
        // falling straight to the 5s-refetch-stepped strategy.spot below
        // — without this the overlay LTP/CHG% "stepped" once per 5s
        // refetch instead of ticking live like the rest of the page.
        const _selLtp = _undLive[selectedUnderlying]?.ltp;
        if (_selLtp > 0) return _selLtp;
      }
      // Tier 2: backend's own resolved value for this strategy — already
      // anchor-priced (backend/api/routes/options.py:_resolve_spot).
      // Reached only when Tier 1/1b have no live tick yet (cold store).
      const s = Number(strategy?.spot || 0);
      if (s > 0) return s;
    }
    // Tier 3: no strategy loaded yet for this underlying (cold start) —
    // fall back to front-month so the marker isn't blank.
    return liveSpot;
  });

  // Per-underlying live {ltp, close} map for the by-underlying Snapshot
  // table AND the Payoff overlay's spot resolver (liveSpot Tier 1) — one
  // shared source so Snapshot's LTP / Chg% / P.Close trio and the Payoff
  // overlay's spot marker never diverge (secondary-defect fix: Snapshot's
  // own row used to read LTP from one source and Chg%/P.Close from
  // another).
  //
  // Iterates _underlyingQuoteKeys (NOT _byUnderlyingTotals) so a
  // freshly-selected root with zero open F&O positions/holdings (e.g.
  // GOLDM) still populates — _byUnderlyingTotals only contains roots with
  // book activity, but _underlyingQuoteKeys always includes
  // selectedUnderlying too (see its own comment).
  //
  // Each entry resolves via liveSnap (SSE tick, same SSOT as before) on
  // the front-month tradingsymbol — resolveUnderlyingTradingsymbol() is
  // the same shared resolution boundary underlyingSpotStore's
  // getUnderlyingSpot() (NavStrip) now uses, so all three surfaces
  // (Snapshot / Payoff / NavStrip) always track the same contract
  // (operator-confirmed: always front-month, never the strategy's
  // pricing anchor when they differ).
  const _undLive = $derived.by(() => {
    const m = /** @type {Record<string, {ltp: number, close: number|null}>} */ ({});
    for (const { root } of _underlyingQuoteKeys) {
      const ts = resolveUnderlyingTradingsymbol(root, findNearestFuture);
      const snap = liveSnap(ts);
      const v = snap?.ltp;
      if (v > 0) {
        const c = snap?.close;
        m[root] = { ltp: v, close: c > 0 ? c : null };
      }
    }
    return m;
  });

  // True for the WHOLE fetch duration between selectedUnderlying changing
  // and the new strategy analytics response landing — NOT "one render
  // frame" as an earlier version of this comment claimed (verified: the
  // window lasts as long as fetchStrategyAnalytics() takes, easily
  // multiple render frames on a slow connection; B3's audit traced the
  // ACTUAL one-frame-per-tick symptom to `_clientPayoffStub` rebuilding
  // on every 250ms tick while this stays true, not to this flag itself
  // being short-lived). liveSpot/payoffSpot recompute immediately (read
  // live ticks) while strategy still holds the old symbol's data — this
  // derived detects that stale window. Every strategy-derived overlay
  // prop routes through `payoffDisplay` (see below), which uses this
  // flag plus a last-good-per-root cache so the overlay switches
  // atomically instead of leaking the old root's data prop-by-prop.
  const _strategyStale = $derived(
    !!strategy &&
    String(strategy.underlying || '').toUpperCase() !== selectedUnderlying.toUpperCase()
  );

  // The anchor future's OWN expiry (for the "rolls in N days" chip) — NOT
  // strategy.expiry, which is the OPTIONS' expiry and can differ from the
  // anchor contract's own roll date (e.g. a monthly option chain priced
  // off a quarterly MCX future). Falls back to strategy.expiry only when
  // the instrument cache can't resolve the anchor (cold cache) so the
  // chip never goes blank.
  const _spotAnchorExpiryISO = $derived.by(() => {
    const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
    if (!anchor) return strategy?.expiry ?? '';
    const inst = getInstrument(anchor);
    return inst?.x || strategy?.expiry || '';
  });

  // prevClose for the payoff chart — throttle-gated to prevent OptionsPayoff
  // from re-rendering on every _underlyingQuotes wholesale replacement
  // (which happens on every loadUnderlyingQuotes() call, including on tab
  // return via exitHibernation). Mirrors the same pattern as liveSpot.
  //
  // Should-fix: `selectedUnderlying` is now read OUTSIDE untrack (a real
  // reactive dependency) — previously it was read only inside the untrack
  // callback, so after a root switch this derived kept resolving the OLD
  // root's close for a few seconds until _throttledTick's next tick forced
  // a re-run.
  //
  // Tier order changed from strategy-first to front-month-first
  // (operator-confirmed Option B: always front-month, everywhere) — now
  // that loadStrategy() no longer sends a `spot` override (cold-start
  // spot-anchor fix above), `strategy.spot_prev_close` reliably carries
  // the BACKEND's resolved anchor contract's close, which on a contango
  // MCX root can differ from the front-month close the Snapshot row and
  // liveSpot use. Front-month tiers win first so the SPOT chip's Chg%
  // stays consistent with liveSpot/Snapshot; strategy.spot_prev_close is
  // now only a last-resort fallback, gated to the current underlying so
  // a still-loading strategy for a different root can't leak its close in.
  const _prevClose = $derived.by(() => {
    void _throttledTick;
    const sel = selectedUnderlying; // tracked — real reactive dependency
    // Tier 1: front-month live close (same source as Snapshot's P.Close column).
    const _liveClose = _undLive[sel]?.close;
    if (_liveClose != null && _liveClose > 0) return _liveClose;
    // Tier 2: front-month batchQuote-cached close.
    const _qClose = untrack(() => _underlyingQuotes[sel]?.prev_close);
    if (_qClose != null && _qClose > 0) return _qClose;
    // Tier 3: backend-resolved anchor close — only when strategy is current
    // for this underlying (avoids leaking a stale root's close during a
    // root-switch window).
    const _stratUnd = String(strategy?.underlying || '').toUpperCase();
    if (_stratUnd === String(sel).toUpperCase() && (strategy?.spot_prev_close ?? 0) > 0) {
      return strategy.spot_prev_close;
    }
    return null;
  });

  // payoffPrevClose — matches payoffSpot's contract basis (the strategy's
  // anchor contract) so the Payoff overlay's own SPOT chip Chg% isn't
  // computed by mixing an anchor-contract LTP against a front-month close.
  const payoffPrevClose = $derived.by(() => {
    const sel = selectedUnderlying;
    const stratUnd = String(strategy?.underlying || '').toUpperCase();
    if (stratUnd === String(sel).toUpperCase()) {
      const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
      if (anchor) {
        const c = liveSnap(anchor)?.close;
        if (c != null && c > 0) return c;
      }
      if ((strategy?.spot_prev_close ?? 0) > 0) return strategy.spot_prev_close;
    }
    return _prevClose;
  });

  // Live-adjusted: incorporate SSE-tick price moves on top of the broker
  // snapshot day_change_val. Without this, the DAY row in the payoff
  // overlay stayed pinned at the last poll's value while ticks were
  // flowing — operator: "I see P∆ constant while P is changing." Mirrors
  // the per-row delta pattern PositionStrip uses (BH2).
  let _lastCandidatesDayPnl = /** @type {number|null} */ (null);
  const candidatesDayPnl = $derived.by(() => {
    void _throttledTick;
    let s = 0;
    let hasLegs = false;
    for (const c of candidatePositions) {
      if (!_isLegEnabled(c)) continue;
      if (!_includeHoldings && c.kind === 'eq') continue;
      hasLegs = true;
      s += _candDayPnl(c);
    }
    if (hasLegs) {
      _lastCandidatesDayPnl = s;
      return s;
    }
    // candidatePositions briefly empty during 5s poll refresh — return stale value
    return _lastCandidatesDayPnl;
  });

  // Net strategy cost — total premium paid (positive = net debit) or
  // received (negative = net credit) across all ENABLED option legs.
  // Futures and equity legs are excluded: their "cost" is the purchase
  // price which tracks spot 1:1 and doesn't have the same semantic as
  // an option premium. Rendered as a horizontal cost annotation line
  // in OptionsPayoff so the operator can see where the breakeven lives
  // relative to the entry cost without switching to the Greeks overlay.
  const _netStrategyCost = $derived.by(() => {
    let total = 0;
    for (const c of candidatePositions) {
      if (!_isLegEnabled(c)) continue;
      if (c.kind !== 'opt') continue;
      const cost = Number(c.avg_cost ?? 0);
      const qty  = Number(c.qty ?? 0);
      if (!qty) continue;
      total += cost * qty;
    }
    return total === 0 ? null : total;
  });

  // Expiry P&L per leg — what the row would settle to if every contract
  // expired RIGHT NOW at the current underlying spot. Different from
  // c.pnl (mark-to-market via LTP): option time-value is stripped and
  // only intrinsic value remains; futures + equity track spot 1:1 so
  // their MTM and expiry P&L converge to the same value. Operator: "in
  // snapshot and legs, show the profit/loss for each on expiration day
  // and updated total for the column".
  /** True when the candidate's expiry date is today (IST) or earlier.
   *  Reads expiry from instruments cache (`inst.x`) → falls back to the
   *  symbol parser's last-Thursday inference for rows without an
   *  instruments entry. Returns false for futures / equity / undated
   *  rows — those have no expiry-promotion concept.
   *  @param {any} c */
  function _isLegExpired(c) {
    if (c.kind !== 'opt') return false;
    const sym = String(c.symbol || '').toUpperCase();
    const inst = getInstrument(sym);
    let expISO = inst?.x || null;
    if (!expISO) return false;
    try {
      // Compare expiry date (yyyy-mm-dd) to today's IST date.
      const today = todayIST();
      return expISO <= today;
    } catch (_) {
      return false;
    }
  }

  /**
   * EXP P&L for one leg — canonical per-candidate formula used by both
   * the legs grid and _legsExpPnlTotal so sum(rows) == TOTAL by construction.
   * - Open F&O: intrinsic at expiry + partial-close realised
   * - Closed F&O (qty=0): locked-in realised || pnl (Kite settled-option fallback)
   * - Equity / proxy hedge: beta-adjusted linear P&L via _eqExpPnlByKey
   * - No spot or unparseable option: null (shows '—')
   * @param {any} c
   * @param {number|null} spot
   * @returns {number|null}
   */
  function _legExpPnlDisplay(c, spot) {
    if (c.kind === 'eq') return _eqExpPnlByKey[enKey(c)] ?? null;
    // Futures value at THEIR OWN contract's live price, not the shared
    // front-month root `spot` — these only coincide when the held future
    // IS the front-month contract (§4 futures-own-price fix). Options
    // keep using the front-month root spot passed in via `spot` (the Exp
    // P&L SSOT for options — Snapshot/Legs TOTAL convergence).
    // resolveExpiryAnchor (expiryPnl.js) is the SAME shared decision tree
    // portfolioStore.svelte.js's _posTier2 uses for this call — a single
    // pure function both sites delegate to, rather than two hand-mirrored
    // copies of the same if/else chain.
    let effSpot = spot;
    if (c.kind === 'fut') {
      const sym = String(c?.symbol || c?.tradingsymbol || '').toUpperCase();
      const ownLive = Number(liveSnap(sym)?.ltp || 0);
      effSpot = resolveExpiryAnchor({ isOpt: false, rootSpot: spot, ownLiveLtp: ownLive, ownPolledLtp: Number(c?.ltp) || 0 });
    }
    // Unified with portfolioStore's Pulse/NavStrip path via the shared
    // expiryPnlWithRealised helper (expiryPnl.js). c already carries both
    // realised and pnl (buildPositionRowFromBroker) — the pnl-fallback is
    // applied INSIDE expiryPnlWithRealised, and only on its qty===0 branch,
    // so it must not be pre-merged into realised here (would double-count
    // against unrealised on still-open legs).
    return expiryPnlWithRealised(c, effSpot, legAnalyticsBySymbol);
  }

  /** Day P&L TOTAL for the currently selected underlying across all enabled
   *  F&O legs — script-level SSOT shared by the Legs TOTAL row AND the
   *  Snapshot row for the selected underlying. Excludes equity (kind === 'eq').
   *  Reads _legsTotalsBase (unaffected by the Legs search box — see its
   *  own comment) rather than displayedCandidates. */
  const _legsDayPnlTotal = $derived.by(() =>
    _legsTotalsBase
      .filter(c => _isLegEnabled(c) && c.kind !== 'eq')
      .reduce((s, c) => s + _candDayPnl(c), 0)
  );

  /** Exp P&L total for the CURRENTLY SELECTED underlying across all
   *  enabled, displayed candidate legs — the Legs-tab TOTAL row's own
   *  SSOT. Expiry-picker filtered (via `_legsTotalsBase`), what-if aware
   *  (`_isLegEnabled`/`showDraftInPayoff` gates), scoped to
   *  `selectedUnderlying` only.
   *
   *  NOT read by the Snapshot grid (item-3 fix, round 4) — every Snapshot
   *  row, including the one for `selectedUnderlying`, reads
   *  `_filteredExpPnlByRoot` via `_rowExpPnlFor` instead (account/strategy
   *  filtered, NOT expiry-picker filtered, no leg-toggle awareness — see
   *  `_rowExpPnlFor`'s docstring for why mixing the two filter bases
   *  produced an inconsistent Snapshot row that changed value purely from
   *  (de)selecting the root). This TOTAL and the Snapshot row for the
   *  same underlying can legitimately differ whenever the expiry picker,
   *  an unchecked leg, or a draft leg is in play.
   *
   *  Uses `liveSpot` rather than reading
   *  `_underlyingQuotes[selectedUnderlying]?.ltp` directly. The direct
   *  read would register a dependency on the whole `_underlyingQuotes`
   *  object — which is replaced wholesale every 30 s — causing this
   *  derived AND every downstream ($equityLegs, payoff chart, legs-grid
   *  TOTAL row) to re-run at the snapshot poll cadence on top of the 4 Hz
   *  tick rate. `liveSpot` itself is tick-driven (its Tier 1/2/3 sources
   *  each track `snapTick` internally via `liveSnap`/`getSnapshot`, not
   *  `_throttledTick` — that gate belongs to a different derived further
   *  down in this file), so using it as the sole spot source keeps this
   *  cascade bounded without a separate throttle here. */
  /** Shared filter+sum used by both _legsExpPnlTotal (liveSpot / front-month
   *  — the Legs-grid TOTAL row's SSOT) and _chartExpPnlAtSpot (payoffSpot
   *  / anchor-contract basis — feeds ONLY the payoff chart's own on-chart
   *  Exp P&L readout next to the expiry marker, C1 fix). Extracted so the
   *  two spot bases can never drift apart in filter logic — only in which
   *  `spot` value is passed. See each derived's own docstring for why the
   *  two consumers intentionally use different bases. */
  function _sumEnabledLegsExpPnl(/** @type {number|null} */ spot) {
    // Single pass: _legExpPnlDisplay is the canonical per-candidate formula
    // so sum(per-leg rows in the grid) == this TOTAL by construction.
    // Handles open F&O, closed F&O, equity/proxy legs, and null (no-spot) legs uniformly.
    // Reads _legsTotalsBase (unaffected by the Legs search box) so typing
    // in the search box narrows the grid without shifting this total or
    // diverging it from the payoff curve, which is built from the
    // unfiltered leg set (§5).
    return _legsTotalsBase
      .filter(c => {
        if (!_isLegEnabled(c)) return false;
        if (!showDraftInPayoff &&
            (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
        return true;
      })
      .reduce((/** @type {number} */ s, c) => {
        const v = _legExpPnlDisplay(c, spot);
        return v == null ? s : s + v;
      }, 0);
  }
  const _legsExpPnlTotal = $derived.by(() => _sumEnabledLegsExpPnl(liveSpot ?? null));
  /** Chart-only Exp P&L at spot (C1 fix) — evaluated at `payoffSpot`
   *  (anchor-contract basis) instead of `liveSpot` (front-month), so the
   *  number the chart displays next to the expiry marker/dart always
   *  agrees with what the dart visually points at (both already use
   *  payoffSpot — see the LTP row, spot line, CHG%, and marker position).
   *  Feeds ONLY the OptionsPayoff `legsExpPnlAtSpot` prop — the separate
   *  Legs-grid TOTAL row keeps reading `_legsExpPnlTotal` (liveSpot),
   *  a deliberately different, correctly-scoped consumer per the
   *  operator's decision (see plan C1). */
  const _chartExpPnlAtSpot = $derived.by(() => _sumEnabledLegsExpPnl(payoffSpot ?? null));

  /** Legs-tab TOTAL row Extrinsic — same enabled/draft gate as
   *  `_legsExpPnlTotal` above, so sum(visible leg rows' Extrinsic) equals
   *  this TOTAL by construction (previously read
   *  `positionsDerivedStore.total.extrinsic`, the WHOLE-BOOK aggregate —
   *  unaffected by search box, expiry filter, OR the enabled/draft
   *  checkboxes — so it silently diverged from the per-leg cells the
   *  moment a leg was unchecked or a filter narrowed the grid). Per-leg
   *  values come from the same `legExtrinsicDisplay` (expiryPnl.js) the
   *  Snapshot row and Legs cells use — poll-time `c.underlying_ltp`, not
   *  the tick-driven `spot` used for Exp P&L (item-2 fix). */
  const _legsExtrinsicTotal = $derived.by(() =>
    _legsTotalsBase
      .filter(c => {
        if (!_isLegEnabled(c)) return false;
        if (!showDraftInPayoff &&
            (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
        return true;
      })
      .reduce((/** @type {number} */ s, c) => {
        const v = legExtrinsicDisplay(c, Number(c?.underlying_ltp) || 0);
        return v == null ? s : s + v;
      }, 0)
  );

  /** Realised P&L offset for the expiry curve — locked-in gains from
   *  partially/fully closed F&O legs. Unlike `chartPnlOffset` (which
   *  carries full BS-vs-broker MTM drift to align the today curve),
   *  this carries only the closed-leg realised component. BS drift has
   *  no meaning at expiry — intrinsic value is path-independent — so
   *  the expiry curve should shift only by locked-in gains, not by
   *  any mark-to-market noise from open legs.
   *  For closed legs (qty=0): use c.realised || c.pnl — Kite returns
   *  realised=0 for options settled at expiry and puts the P&L in c.pnl.
   *  For open legs: c.realised only (c.pnl includes unrealised MTM). */
  // Reads _legsTotalsBase (unaffected by the Legs search box — see its
  // own comment) so the curve's offset stays stable while the operator
  // types in the search box (§5).
  const _expiryPnlOffset = $derived.by(() =>
    _legsTotalsBase
      .filter(c => {
        if (!_isLegEnabled(c) || c.kind === 'eq') return false;
        if (!showDraftInPayoff &&
            (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
        return true;
      })
      .reduce((s, c) => s + (Number(c.qty || 0) === 0
        ? Number(c.realised || c.pnl || 0)
        : Number(c.realised || 0)), 0)
  );

  // Master "select all" plumbing for the Legs panel header checkbox.
  // allCandidatesOn = true when every candidate is enabled in the
  // map; false when some are off. The DOM element ref drives the
  // tri-state visual: checked / unchecked / indeterminate (some on,
  // some off) — set via $effect so the indeterminate flag reflects
  // the live state without us having to manually clear it on toggle.
  let allCandidatesEl = $state(/** @type {HTMLInputElement|null} */ (null));
  const allCandidatesOn = $derived.by(() => {
    if (!candidatePositions.length) return false;
    return candidatePositions.every(c => _isLegEnabled(c));
  });
  const someCandidatesOn = $derived.by(() => {
    if (!candidatePositions.length) return false;
    return candidatePositions.some(c => _isLegEnabled(c));
  });
  /** True when every enabled non-eq leg has qty=0 (all positions closed today).
   *  Distinct from "no legs selected" (operator unchecked rows) — legs ARE
   *  enabled but there is nothing open to price. Gates the empty-state message
   *  so the operator sees a meaningful hint rather than "Tick at least one row"
   *  when the rows are already ticked. Derived off `legs` (already filtered
   *  to enabled candidates) rather than candidatePositions to avoid reading
   *  enabledSymbols again (already expressed in legs). */
  const _allEnabledLegsZeroQty = $derived.by(() => {
    const nonEq = legs.filter(l => l.kind !== 'eq');
    if (nonEq.length === 0) return false;
    return nonEq.every(l => Number(l.qty) === 0);
  });
  /** True when the only enabled candidates are equity holdings (kind==='eq')
   *  and the "Include Holdings" toggle is OFF. In this case cleanLegs is
   *  empty (backend only accepts opt/fut) and strategy stays null — not
   *  because rows are unchecked but because the master toggle hides them.
   *  Gates a dedicated empty-state branch so the operator sees an actionable
   *  hint ("flip the toggle or add F&O legs") instead of the generic
   *  "No legs selected" message that implies they need to tick checkboxes. */
  const _legsAreEqOnly = $derived.by(() => {
    if (_includeHoldings) return false;
    if (!candidatePositions.length) return false;
    // Every candidate must be eq — no opt/fut in the book for this underlying.
    return candidatePositions.every(c => c.kind === 'eq');
  });
  $effect(() => {
    if (!allCandidatesEl) return;
    // Indeterminate iff some-but-not-all are on. Browser doesn't
    // accept this as an attribute; only JS property writes work.
    allCandidatesEl.indeterminate = someCandidatesOn && !allCandidatesOn;
  });
  function toggleAllCandidates() {
    // Flip toward the opposite of the current "all-on" state. Builds
    // a fresh map so Svelte 5 picks up the change reactively. Also
    // propagates eq-leg picks to long-term memory so the master
    // toggle teaches the same "remember my choice" pattern as the
    // per-row checkbox.
    /** @type {Record<string, boolean>} */
    const next = {};
    const target = !allCandidatesOn;
    for (const c of candidatePositions) {
      next[enKey(c)] = target;
      if (c.kind === 'eq') _persistEqMemory(c, target);
    }
    enabledSymbols = next;
  }

  // C5 fix — the spot `candidatesActualPnl` was actually priced at.
  // `candidatesActualPnl` sums each enabled candidate's broker `pnl`,
  // which was computed by the backend against that candidate's own
  // poll-time `underlying_ltp` (stamped by positions.py's option-Greeks
  // enrichment pass on the SAME expiry-matching anchor contract the
  // payoff curve itself is priced on — see option_underlying_quote_key,
  // "the matching-month future, which serves as the spot proxy for
  // MCX"). Reading `c.underlying_ltp` here (frozen inside the `positions`
  // $state until the NEXT book-poll write) — rather than the live-ticking
  // `payoffSpot`/`strategy.spot` — keeps `chartTheoreticalAtSpot` and
  // `candidatesActualPnl` anchored to ONE shared clock (the book poll),
  // instead of two independent 5s timers that drift in and out of phase
  // and double-count a spot move for 0-5s before snapping back (the
  // sawtooth the plan describes). Falls back to strategy.spot for a
  // futures/equity-only basket (no option legs ⇒ no underlying_ltp
  // stamped on any row).
  const _pollSpot = $derived.by(() => {
    for (const c of candidatePositions) {
      if (!_isLegEnabled(c)) continue;
      if (!_includeHoldings && c.kind === 'eq') continue;
      const v = Number(c.underlying_ltp) || 0;
      if (v > 0) return v;
    }
    return Number(strategy?.spot || 0);
  });

  // Backend's BS-theoretical TDAY at the poll-clock spot (C5) — the
  // value the chart would render WITHOUT any offset. Linearly
  // interpolated (C4) rather than snapped to the nearest grid point, so
  // the offset computed below doesn't inherit a grid-step rounding error.
  const chartTheoreticalAtSpot = $derived.by(() => {
    // Read from the MERGED payoff (option strategy + any layered
    // equity-holding contribution), not strategy.payoff. Without this,
    // the chart's TDAY at current spot would double-count the eq
    // holding's P&L: candidatesActualPnl already includes the eq's
    // broker pnl, and merged_today_value(spot) also includes
    // (spot − cost) × qty for the eq leg — at spot=ltp those two
    // quantities are equal and stacked.
    const arr = _mergedPayoff;
    if (!arr || arr.length === 0) return 0;
    const targetSpot = _pollSpot || Number(strategy?.spot || 0);
    return interpAt(arr, targetSpot, 'today_value') ?? 0;
  });

  // Vertical offset applied to the chart curves so that TDAY at the
  // current spot exactly equals the dashboard's per-underlying ₹.
  // Direct alignment — no per-leg theoretical / LTP accounting; the
  // formula is the simplest expression of "make chart-at-current
  // match the candidates' broker pnl":
  //
  //     offset = candidatesActualPnl − chartTheoreticalAtSpot
  //
  // After the shift, chart_today_value(current_spot)
  //   = chart_theoretical_at_spot + offset
  //   = chart_theoretical_at_spot
  //     + (candidatesActualPnl − chart_theoretical_at_spot)
  //   = candidatesActualPnl  ← matches dashboard exactly
  //
  // Off-current spots get the same offset, so the curve SHAPE (BS
  // sensitivity to spot) is preserved. At realizedPnl=0 AND no
  // theoretical-vs-LTP drift, candidatesActualPnl ≈ chart_theoretical,
  // offset ≈ 0, no visible shift.
  const chartPnlOffset = $derived(
    candidatesActualPnl - chartTheoreticalAtSpot
  );

  // Initialize the enable-flag map when candidates change. Default:
  // every candidate enabled (operator sees their book in the payoff
  // immediately; un-checks to drop a leg).
  // ── untrack the read of `enabledSymbols` so this effect re-runs only
  //    when the candidate set itself changes; otherwise the assignment
  //    on line below would re-trigger this effect → infinite loop hang.
  $effect(() => {
    const cands = candidatePositions;
    untrack(() => {
      /** @type {Record<string, boolean>} */
      const next = {};
      let changed = false;
      const prevKeys = Object.keys(enabledSymbols);
      if (prevKeys.length !== cands.length) changed = true;
      for (const c of cands) {
        const k = enKey(c);
        if (!(k in enabledSymbols)) changed = true;
        next[k] = (k in enabledSymbols) ? enabledSymbols[k] : true;
      }
      // Skip the write entirely when nothing meaningful changed —
      // assigning a new ref would still trigger downstream effects.
      if (changed) enabledSymbols = next;
    });
  });

  // Rebuild legs from the current candidates × enabled-flag combination.
  // Drafts already live in candidatePositions (with source='draft'),
  // so this single derivation covers live + sim + draft uniformly.
  //
  // Expiry-filter integrity: candidatePositions deliberately lets
  // closed rows (qty=0) bypass the expiry filter so they stay visible
  // in the Legs panel (parity with the dashboard grid). But they MUST
  // NOT bleed into the payoff payload — sending other-expiry legs to
  // the strategy endpoint pollutes the curve with contracts the
  // operator explicitly filtered out. Re-apply the expiry gate here so
  // legs stays scoped to the picked expiries even when the panel
  // shows extra context rows.
  //
  // showDraftInPayoff gates provisional (~), draft-store (D), and
  // payoffDraft (text-input + OrderTicket "Add to Payoff") legs:
  // when OFF they still appear as rows in the Legs panel but are
  // excluded from the payoff curve / analytics fetch.
  $effect(() => {
    void candidatePositions; void enabledSymbols; void showDraftInPayoff;
    untrack(() => {
      legs = candidatePositions
        .filter(c => {
          if (!_isLegEnabled(c)) return false;
          if (!showDraftInPayoff &&
              (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
          return true;
        })
        .map(c => ({
          symbol:   c.symbol,
          qty:      c.qty,
          avg_cost: c.avg_cost ?? '',
          ltp:      c.ltp ?? '',
          source:   c.source,
          kind:     c.kind,
        }));
    });
  });

  // Bug 1 fix: loadStrategy trigger that fires AFTER legs have been updated
  // following a symbol switch. The existing trigger effect (line ~1695) reads
  // selectedUnderlying and calls loadStrategy immediately — but it runs BEFORE
  // the legs-update $effect above (Svelte 5 runs effects in declaration order).
  // That means the first call runs with the stale previous symbol's legs,
  // hits the memo (_stratLastKey), and returns early. By the time the
  // legs-update $effect fires and writes the new symbol's legs, there is no
  // other reactive path to re-run loadStrategy until the next 5s interval tick.
  // This effect tracks `legs` (written by the update $effect above) and fires
  // loadStrategy() when the strategy is stale for the current underlying.
  $effect(() => {
    void legs;
    const sel = selectedUnderlying;
    const stratUnd = String(strategy?.underlying || '').toUpperCase();
    if (!legs.length) return;
    if (stratUnd && stratUnd === sel.toUpperCase()) return; // strategy already matches — skip
    untrack(() => { try { loadStrategy(); } catch (_) {} });
  });

  // Enabled equity-holding legs of the underlying — held out of the
  // strategy-analytics POST (backend only accepts opt/fut) and layered
  // onto the rendered payoff in the chart instead. Two contribution
  // shapes:
  //   qty > 0       → linear  (S − avg_cost) × qty   (still held)
  //   qty = 0, opening_qty > 0 → flat realized pnl   (sold today —
  //                              profit locked in, applied as a
  //                              constant offset regardless of spot)
  const _equityLegs = $derived(
    !_includeHoldings
      ? []
      : candidatePositions.filter(c => {
          if (c.kind !== 'eq') return false;
          if (!_isLegEnabled(c)) return false;
          const qty = Number(c.qty || 0);
          const opq = Number(c.opening_qty || 0);
          return qty !== 0 || opq !== 0;
        })
  );
  /** Backend payoff with the equity-holding contribution layered on
   *  per-spot. Operator: "include underlying from holdings in legs.
   *  show the cost price on it profit to offset option return in
   *  payoff graph."
   *
   *  When an eq leg is ENABLED it always contributes linearly:
   *    payoff_add(spot) = (spot − avg_cost) × effective_qty
   *  where effective_qty = current qty if still held, else
   *  opening_qty if the row was fully sold today. Both today AND
   *  expiry curves shift by the same amount (stock value doesn't
   *  decay).
   *
   *  Treating sold-today rows the same as held rows when enabled is
   *  a deliberate "what-if-still-held" simulation — the operator's
   *  opt-in is the signal that they want the underlying's full
   *  exposure reflected in the combined view. At the current spot
   *  the linear contribution ≈ the realised P&L (cost basis + sale
   *  near current LTP); at distant spots it shows the slope of
   *  having maintained the position. This is what makes the
   *  combined Δ chip change when the eq leg is checked.
   *
   *  Pass-through when no equity legs are enabled (preserves
   *  strategy.payoff identity for OptionsPayoff $derived caches). */
  /** Shared linear-leg computation — every consumer
   *  (`_mergedPayoff` / `_mergedGreeks` / `_mergedRisk`) needs to
   *  know each eq leg's effective (qty, cost) after proxy-β scaling.
   *  Pulled into one derived so the proxy lookup + math run ONCE per
   *  state change, not three times. ETF case (β=null) → β=1.0 implicit;
   *  Stage 3 stock-vs-index uses the regression slope. Skips legs
   *  whose proxy price or target spot is unusable. */
  const _equityLinearLegs = $derived.by(() => {
    const eqs = _equityLegs;
    /** @type {Array<{qty:number,cost:number,key:string}>} */
    const out = [];
    if (!eqs.length) return out;
    const targetSpot = Number(strategy?.spot) || 0;
    for (const eq of eqs) {
      const cost = Number(eq.avg_cost);
      if (!Number.isFinite(cost)) continue;
      const rawQty = Number(eq.qty) || Number(eq.opening_qty) || 0;
      if (rawQty === 0) continue;
      let effQty = rawQty;
      let effCost = cost;
      if (eq.proxy_for) {
        const proxyLtp = Number(eq.ltp);
        if (proxyLtp <= 0 || targetSpot <= 0) continue;
        const row = getProxyRow(eq.symbol, eq.proxy_for);
        const beta = row?.beta != null ? Number(row.beta) : 1.0;
        const marketValue = rawQty * proxyLtp;
        const investmentValue = rawQty * cost;
        effQty = (beta * marketValue) / targetSpot;
        if (effQty === 0) continue;
        effCost = investmentValue / effQty;
      }
      out.push({ qty: effQty, cost: effCost, key: enKey(eq) });
    }
    return out;
  });

  /** Payoff chart's "leg composition" identity — the signal OptionsPayoff
   *  uses to distinguish "same strategy, new data landed on the routine
   *  5s refetch" from "the operator actually changed the basket" (B1 fix).
   *  NOT `computeLegsKey(cleanLegs)` — that memo key embeds `ltp` for
   *  sim/draft/provisional legs (buildCleanLegs inlines it so the backend
   *  can price legs it can't fetch from a broker), so it would change on
   *  every sim/draft price tick and pulse the chart continuously in sim
   *  mode, the exact bug this fix is meant to remove.
   *
   *  Built from content (symbol:qty), not object identity, so a routine
   *  refetch — which always reassigns `strategy` to a brand-new object
   *  with the SAME leg composition — produces an IDENTICAL string and
   *  therefore does not change this derived's value (Svelte 5 skips
   *  downstream reactions when a primitive $derived's value is
   *  unchanged). Only a genuine add/remove/qty-change leg, an
   *  underlying switch, or a holdings/draft toggle flip changes it. */
  const _legSignature = $derived.by(() => {
    const optLegs = (strategy?.legs || [])
      .map(/** @param {{symbol:string, qty:number}} l */ l => `${l.symbol}:${l.qty}`)
      .sort()
      .join(',');
    const eqLegs = _equityLinearLegs
      .map(l => `${l.key}:${l.qty.toFixed(2)}`)
      .sort()
      .join(',');
    return `${selectedUnderlying}|${optLegs}|${eqLegs}|H${_includeHoldings ? 1 : 0}|D${showDraftInPayoff ? 1 : 0}`;
  });

  const _eqExpPnlByKey = $derived.by(() => {
    const spot = liveSpot;
    if (spot == null) return /** @type {Record<string,number>} */ ({});
    /** @type {Record<string,number>} */
    const m = {};
    for (const l of _equityLinearLegs) m[l.key] = (spot - l.cost) * l.qty;
    return m;
  });
  const _mergedPayoff = $derived.by(() => {
    const base = strategy?.payoff;
    if (!Array.isArray(base) || base.length === 0) {
      const result = base || [];
      untrack(() => debugLog('payoff:merge', 'computed', {
        length: result?.length ?? 0,
        spotMin: result?.[0]?.spot,
        spotMax: result?.at?.(-1)?.spot,
      }));
      return result;
    }
    const linearLegs = _equityLinearLegs;
    if (linearLegs.length === 0) {
      untrack(() => debugLog('payoff:merge', 'computed', {
        length: base.length,
        spotMin: base[0]?.spot,
        spotMax: base.at(-1)?.spot,
      }));
      return base;
    }
    const result = base.map(/** @param {{spot:number,today_value:number,expiry_value:number}} pt */ pt => {
      let add = 0;
      for (const l of linearLegs) add += (pt.spot - l.cost) * l.qty;
      return {
        ...pt,
        today_value:  pt.today_value  + add,
        expiry_value: pt.expiry_value + add,
      };
    });
    untrack(() => debugLog('payoff:merge', 'computed', {
      length: result?.length ?? 0,
      spotMin: result?.[0]?.spot,
      spotMax: result?.at?.(-1)?.spot,
    }));
    return result;
  });

  /** Client-side intrinsic payoff stub — rendered immediately when legs
   *  are available but strategy (backend BS response) hasn't arrived yet.
   *  Uses the same `expiryPnl()` SSOT as the Legs grid's Exp P&L column:
   *    CE: (max(0, spot − K) − cost) × qty
   *    PE: (max(0, K − spot) − cost) × qty
   *    FUT/EQ: (spot − cost) × qty
   *  `today_value` is set to null so OptionsPayoff suppresses the amber today
   *  curve until the backend BS response arrives. `expiry_value` is set to the
   *  intrinsic sum — the sky-blue dashed expiry curve renders immediately.
   *
   *  Empty-legs case (cold start, no positions yet): returns a flat
   *  41-point grid at y=0 across the ±20 % spot range. This lets the
   *  payoff card render its axes + spot marker immediately when the
   *  operator picks a symbol, before any legs land — a valid and
   *  useful display (shows what the chart will look like, primes the
   *  operator to add legs).
   *
   *  Spot resolution: mirrors liveSpot's 4-tier chain but reads strategy
   *  as null (strategy hasn't loaded yet), so only tiers 3-4 matter:
   *    1. _underlyingQuotes batchQuote snapshot (30 s cadence)
   *    2. getSnapshot SSE tick for selectedUnderlying (sub-second)
   *  Both reads are wrapped in untrack() so this derived stays at the
   *  250 ms _throttledTick gate and doesn't bypass it.
   *
   *  Returns [] when spot ≤ 0 (no price data at all). Empty legs or
   *  legs with no computable payoff with a valid spot return a flat
   *  y=0 grid rather than []. Never returns null. */
  const _clientPayoffStub = $derived.by(() => {
    void _throttledTick;
    // Only the non-eq enabled legs (eq contribution needs _includeHoldings).
    const activeLegs = legs.filter(l => {
      if (l.kind === 'eq') return _includeHoldings;
      return true;
    });

    // Spot resolution — strategy is null at this point; read the same
    // sources that liveSpot's tiers 3+4 use.
    // Gap A: capture selectedUnderlying BEFORE any untrack() so a symbol
    //        switch always triggers re-derive — if the bqLtp early-return
    //        fires, selectedUnderlying would otherwise be inside untrack
    //        and never tracked.
    // Gap B: always track _quoteGeneration regardless of market state so
    //        batchQuote updates (MCX pre-open, post-open) re-trigger this
    //        derived even when _throttledTick is sparse.
    const spot = (() => {
      void _quoteGeneration;            // Gap B: always track
      const _sel = selectedUnderlying;  // Gap A: capture before untrack
      const bqLtp = untrack(() => _underlyingQuotes[_sel]?.ltp);
      if (bqLtp != null && Number.isFinite(bqLtp) && bqLtp > 0) return bqLtp;
      if (_sel) {
        const _resolvedSym = untrack(() => resolveUnderlying(String(_sel).toUpperCase(), findNearestFuture)?.tradingsymbol);
        const _lookupSym = _resolvedSym || String(_sel).toUpperCase();
        const v = untrack(() => Number(getSnapshot(_lookupSym)?.ltp));
        if (Number.isFinite(v) && v > 0) return v;
      }
      return 0;
    })();
    untrack(() => debugLog('payoff:stub', 'spot', { spot, legs: activeLegs?.length ?? 0, selectedUnderlying }));
    if (spot <= 0) return [];

    // 41-point grid from 80 % to 120 % of spot.
    const lo = spot * 0.80;
    const hi = spot * 1.20;
    const n  = 41;
    const step = (hi - lo) / (n - 1);
    /** @type {Array<{spot: number, today_value: number|null, expiry_value: number}>} */
    const out = [];

    // Empty legs → flat line at y=0. Valid + useful display: axes + spot
    // marker + zero-P&L line. Once legs land the derived recomputes into
    // the intrinsic curve without a card unmount.
    if (activeLegs.length === 0) {
      for (let i = 0; i < n; i++) {
        out.push({ spot: lo + i * step, today_value: 0, expiry_value: 0 });
      }
      return out;
    }

    for (let i = 0; i < n; i++) {
      const s = lo + i * step;
      let sum = 0;
      let anyValid = false;
      for (const l of activeLegs) {
        const v = expiryPnl({ ...l, kind: l.kind ?? 'fut' }, s);
        if (v != null) { sum += v; anyValid = true; }
      }
      if (!anyValid) { out.push({ spot: s, today_value: 0, expiry_value: 0 }); continue; }
      out.push({ spot: s, today_value: null, expiry_value: sum });
    }
    return out;
  });

  /** Risk overlay values — recomputed from the merged curve when eq
   *  legs are present so MAX P / MAX L / breakevens reflect the
   *  combined position (stock + option) instead of the backend's
   *  option-only numbers. When no eq legs are enabled this is a thin
   *  pass-through of `strategy.risk` so behaviour for pure-option
   *  baskets is unchanged. */
  const _mergedRisk = $derived.by(() => {
    const baseRisk = strategy?.risk;
    if (!baseRisk) return null;
    if (_equityLegs.length === 0) return baseRisk;
    const curve = _mergedPayoff;
    if (!curve.length) return baseRisk;
    // Walk the expiry curve over the displayed range.
    let maxP = -Infinity, maxL = Infinity;
    for (const pt of curve) {
      const v = pt.expiry_value;
      if (v > maxP) maxP = v;
      if (v < maxL) maxL = v;
    }
    // Breakevens: linear-interpolate every zero-crossing along the
    // expiry curve. Stable for monotonic and piecewise-linear segments.
    const breakevens = [];
    for (let i = 1; i < curve.length; i++) {
      const a = curve[i - 1], b = curve[i];
      const av = a.expiry_value, bv = b.expiry_value;
      if ((av <= 0 && bv >= 0) || (av >= 0 && bv <= 0)) {
        if (av === bv) continue;
        const t = -av / (bv - av);
        breakevens.push(a.spot + t * (b.spot - a.spot));
      }
    }
    return { ...baseRisk, max_profit: maxP, max_loss: maxL, breakevens };
  });

  // Sprint D fix (audit cross-symbol #1): re-derive EV + POP over the
  // MERGED curve when proxy/equity legs are layered. Pre-fix the chart
  // displayed `strategy.risk.pop` and `strategy.risk.ev` directly from
  // the backend, which only saw the option legs — so a GOLDBEES proxy
  // hedged short straddle showed option-only POP/EV unchanged after
  // the equity leg shifted the payoff curve.
  //
  // Math mirrors backend/api/algo/derivatives.py — JS port:
  //   _normCdf  — Abramowitz & Stegun 7.1.26 approximation
  //   _probAbove(S, K, T, σ) — P(S_T ≥ K) under risk-neutral lognormal
  //   _expectedValueOnCurve — trapezoid ∫ expiry_value × pdf
  //   _multilegPopOnCurve — sum over contiguous profit segments
  /** EV recomputed over the merged curve. Falls back to the backend's
   *  strategy.risk.ev when no eq legs are present (zero overhead) or
   *  when prereqs are missing. */
  const _mergedEv = $derived.by(() => {
    const baseEv = strategy?.risk?.ev ?? null;
    if (_equityLegs.length === 0) return baseEv;
    const S     = Number(strategy?.spot || 0);
    const dte   = Number(strategy?.days_to_expiry || 0);
    const sigma = Number(strategy?.iv_proxy || 0);
    if (S <= 0 || dte <= 0 || sigma <= 0) return baseEv;
    const T = dte / 365.0;
    const ev = _expectedValueOnCurve(_mergedPayoff, S, T, sigma);
    return Number.isFinite(ev) ? ev : baseEv;
  });
  /** POP recomputed over the merged curve. Same fallback shape as EV. */
  const _mergedPop = $derived.by(() => {
    const basePop = strategy?.risk?.pop ?? null;
    if (_equityLegs.length === 0) return basePop;
    const S     = Number(strategy?.spot || 0);
    const dte   = Number(strategy?.days_to_expiry || 0);
    const sigma = Number(strategy?.iv_proxy || 0);
    if (S <= 0 || dte <= 0 || sigma <= 0) return basePop;
    const T = dte / 365.0;
    const pop = _multilegPopOnCurve(_mergedPayoff, S, T, sigma);
    return pop !== null && Number.isFinite(pop) ? pop : basePop;
  });
  /** EV-as-pct-of-cost when eq legs are layered. Back-derives the
   *  underlying |cost| basis from the backend's
   *  `base.ev_pct = base.ev / |cost| × 100` identity, then rescales
   *  with the merged EV. Returns null when prereqs are absent or the
   *  base values can't be inverted (ev=0). */
  const _mergedEvPct = $derived.by(() => {
    const baseEvPct = strategy?.risk?.ev_pct ?? null;
    if (_equityLegs.length === 0 || baseEvPct == null) return baseEvPct;
    const baseEv = Number(strategy?.risk?.ev ?? 0);
    const mergedEv = _mergedEv;
    if (!baseEv || mergedEv == null) return baseEvPct;
    return mergedEv * baseEvPct / baseEv;
  });

  /** Aggregate Greeks with the equity-holding contribution layered on.
   *  Long stock has delta = +1 per share, gamma/theta/vega/rho = 0
   *  (linear payoff, no convexity, no decay, no IV/rate sensitivity).
   *  Short stock contributes -1 per share. Sold-today rows contribute
   *  ZERO across all Greeks — the realised P&L is locked in, there's
   *  no go-forward position to risk-manage.
   *
   *  Pass-through when no eq legs are enabled (preserves
   *  strategy.aggregate_greeks identity). */
  const _mergedGreeks = $derived.by(() => {
    const base = strategy?.aggregate_greeks;
    if (!base) return null;
    const linearLegs = _equityLinearLegs;
    if (linearLegs.length === 0) return base;
    let extraDelta = 0;
    for (const l of linearLegs) extraDelta += l.qty;
    if (extraDelta === 0) return base;
    return { ...base, delta: Number(base.delta || 0) + extraDelta };
  });

  // Auto-trigger strategy analytics whenever the leg set changes — no
  // explicit Analyze button needed. Also re-runs when the Holdings
  // toggle flips so the equity-only synth path (or its absence)
  // takes effect immediately.
  $effect(() => {
    void legs;
    void _includeHoldings;
    untrack(() => loadStrategy());
  });

  // ── Option-chain picker (Strategy mode) ───────────────────────────
  // Lets the operator browse strikes for a given underlying + expiry
  // and add legs by clicking CE / PE buttons next to each strike. Pulls
  // the contract universe from the instruments cache (already loaded
  // for /console autocomplete) — no extra API round-trips.
  let instrumentsReady = $state(false);
  // True once the hedge-proxy table has resolved on mount. The proxy
  // derivations (_hedgeOpportunities, candidatePositions eq merge) read
  // this so they re-trigger after the async fetch lands; without it,
  // the page would render with an empty proxy cache and never refresh.
  let proxyTableReady  = $state(false);
  let chainUnderlying  = $state('');
  let chainExpiry      = $state('');
  // Kind multi-select — operator picks any combination of Options
  // and Futures. Defaults to Options only; the Futures section is
  // opt-in so the strike grid stays the focal point on mount.
  /** @type {Array<'opt'|'fut'>} */
  let chainKinds       = $state(/** @type {Array<'opt'|'fut'>} */ (['opt']));

  // chainSide stays as the (i) launcher's default leg side (long).
  // Per-row +/− buttons override on a per-pick basis (each button
  // explicitly passes its own side to addChainDraft), so the outer
  // toggle no longer carries operator-meaningful state.
  let chainSide        = $state(/** @type {'long'|'short'} */ ('long'));
  // chainLots is now only the FALLBACK qty multiplier the (i) modal
  // path uses when opening OrderTicket directly. The fast +/− path
  // owns its own per-click lots state via quickPicker below.
  let chainLots        = $state(1);

  // ── Direct-to-basket quick buttons ────────────────────────────────
  // Click + or − on a strike (or a futures pill) → leg lands in
  // `chainBasket` immediately at 1 lot (default). Lot adjustments
  // happen on the basket pill itself (inline − / + stepper). The
  // OrderTicket modal (i button) is still the slow path for limit
  // / mode / qty edits before placing. Replaces the previous inline
  // ✓ / ✕ / +B picker — operator wanted a single, predictable path
  // through the basket bar.
  // Brief toast tracking the cell that just got "+ added" so the
  // operator can see their click registered. Auto-clears in ~1 s.
  /** @type {{ key: string, msg: string }|null} */
  let quickToast = $state(null);

  function _quickKeyOpt(strike, optType) { return `o:${strike}:${optType}`; }
  function _quickKeyFut(sym)             { return `f:${sym}`; }

  function _flashToast(/** @type {string} */ key, /** @type {string} */ msg) {
    quickToast = { key, msg };
    setTimeout(() => {
      if (quickToast?.key === key) quickToast = null;
    }, 900);
  }

  /** Merge an incoming leg into the existing basket if a leg with
   *  the same symbol AND same side already exists — bump its lots
   *  rather than appending a duplicate pill. Returns true when a
   *  merge happened (caller flashes the dedupe toast on the
   *  existing cell), false otherwise. */
  function _mergeIntoBasket(/** @type {{sym:string, side:'BUY'|'SELL', lots:number}} */ incoming) {
    const idx = chainBasket.findIndex(b =>
      b.sym === incoming.sym && b.side === incoming.side);
    if (idx < 0) return false;
    chainBasket = chainBasket.map((b, i) =>
      i === idx ? { ...b, lots: (b.lots || 0) + (incoming.lots || 1) } : b);
    return true;
  }

  /** Add an option leg to the basket. Resolves symbol + lot size from
   *  the instruments cache and seeds the limit price from chain
   *  quotes (BUY → ask, SELL → bid) so the basket goes out as a
   *  LIMIT order. Adding the same (strike, type, side) again bumps
   *  the existing pill's lots count instead of duplicating. */
  function addOptionToBasket(/** @type {number} */ strike,
                              /** @type {'CE'|'PE'} */ optType,
                              /** @type {'long'|'short'} */ side) {
    if (!chainUnderlying || !chainExpiry) return;
    const inst = findOption(
      chainUnderlying.toUpperCase(), optType, strike, chainExpiry,
    );
    if (!inst) { basketError = 'Symbol not in instruments cache.'; return; }
    const sideTag = /** @type {'BUY'|'SELL'} */ (side === 'long' ? 'BUY' : 'SELL');
    if (_mergeIntoBasket({ sym: String(inst.s), side: sideTag, lots: 1 })) {
      basketError = '';
      _flashToast(_quickKeyOpt(strike, optType), '+1 lot');
      return;
    }
    const q = chainQuotesMap?.[String(strike)]?.[optType.toLowerCase()];
    const limit = sideTag === 'BUY'
      ? (q?.ask ?? q?.bid ?? 0)
      : (q?.bid ?? q?.ask ?? 0);
    chainBasket = [...chainBasket, {
      key:      `${sideTag}|${_quickKeyOpt(strike, optType)}|${Date.now()}`,
      side:     sideTag,
      sym:      String(inst.s),
      exchange: inst.e || 'NFO',
      lots:     1,
      lotSize:  Number(inst.ls || 1),
      product:  'NRML',
      limit:    Number(limit) || 0,
      chaseAgg: /** @type {'low'} */ ('low'),
    }];
    basketError = '';
    _flashToast(_quickKeyOpt(strike, optType), '✓ added');
  }

  /** Add a futures leg to the basket. Same dedupe rule — same sym
   *  + same side bumps lots on the existing leg. Limit defaults to
   *  0 (the Place handler routes 0-priced legs as MARKET). */
  function addFuturesToBasket(/** @type {string} */ sym,
                               /** @type {number} */ lotSize,
                               /** @type {'long'|'short'} */ side) {
    const inst = getInstrument(String(sym || '').toUpperCase());
    const sideTag = /** @type {'BUY'|'SELL'} */ (side === 'long' ? 'BUY' : 'SELL');
    if (_mergeIntoBasket({ sym: String(sym), side: sideTag, lots: 1 })) {
      basketError = '';
      _flashToast(_quickKeyFut(sym), '+1 lot');
      return;
    }
    chainBasket = [...chainBasket, {
      key:      `${sideTag}|${_quickKeyFut(sym)}|${Date.now()}`,
      side:     sideTag,
      sym:      String(sym),
      exchange: inst?.e || 'NFO',
      lots:     1,
      lotSize:  Number(lotSize || inst?.ls || 1),
      product:  'NRML',
      limit:    0,
      chaseAgg: /** @type {'low'} */ ('low'),
    }];
    basketError = '';
    _flashToast(_quickKeyFut(sym), '✓ added');
  }

  /** Set the chase aggressiveness on a single basket leg. */
  function setBasketChaseAgg(/** @type {string} */ key,
                              /** @type {'low'|'med'|'high'} */ agg) {
    chainBasket = chainBasket.map(b =>
      b.key === key ? { ...b, chaseAgg: agg } : b
    );
  }

  /** Step the lots count on a single basket leg. Floored at 1; no
   *  upper cap (margin pre-flight catches over-sizing on submit). */
  function basketStepLots(/** @type {string} */ key,
                           /** @type {number} */ delta) {
    chainBasket = chainBasket.map(b => {
      if (b.key !== key) return b;
      return { ...b, lots: Math.max(1, Math.floor((b.lots || 1) + delta)) };
    });
  }

  // ── Chain basket — staged legs awaiting one-shot submit ───────────
  // Each leg carries its OWN chase aggressiveness, surfaced on the
  // pill as C[L|M|H]. Defaults to 'low' for quick-adds; OrderTicket
  // submissions through "+ Basket" pass the operator's picked agg.
  /** @type {Array<{
   *   key: string,
   *   side: 'BUY'|'SELL',
   *   sym: string,
   *   exchange: string,
   *   lots: number,
   *   lotSize: number,
   *   product: string,
   *   limit: number,
   *   chaseAgg: 'low'|'med'|'high',
   * }>} */
  let chainBasket    = $state([]);
  let basketPlacing  = $state(false);
  let basketError    = $state('');
  let basketProgress = $state(0);
  let basketJustDone = $state(false);
  function removeFromBasket(/** @type {string} */ key) {
    chainBasket = chainBasket.filter(b => b.key !== key);
  }
  function clearBasket() { chainBasket = []; basketError = ''; }
  /** Submit every basket leg sequentially as a paper MARKET order via
   *  the same `placeTicketOrder` path used by the OrderTicket modal.
   *  Sequential (not Promise.all) so the broker isn't hammered with
   *  parallel writes that could trip rate-limits — matters more in
   *  prod where these resolve to live broker calls. */
  async function placeBasket() {
    if (basketPlacing || !chainBasket.length) return;
    const acct = _ticketAccountDefault();
    if (!acct) { basketError = 'No routable account selected.'; return; }

    // Mode follows the master execution toggle, mirroring the OrderTicket
    // path so the basket isn't a footgun (was hardcoded to 'paper' which
    // silently routed every leg through the paper engine even when the
    // operator had flipped to LIVE on /admin/execution).
    let basketMode = 'paper';
    try {
      const live = await fetchLiveStatus();
      if (live && live.paper_trading_mode === false && live.branch === 'main') {
        basketMode = 'live';
      }
    } catch {
      basketError = 'Could not determine execution mode. Try again.';
      basketPlacing = false;
      return;
    }

    basketPlacing  = true;
    basketError    = '';
    basketProgress = 0;
    /** @type {string[]} */ const failures = [];
    for (const leg of chainBasket) {
      try {
        const hasLimit = Number(leg.limit) > 0;
        await placeTicketOrder({
          mode:          basketMode,
          side:          leg.side,
          tradingsymbol: leg.sym,
          quantity:      leg.lots * leg.lotSize,
          exchange:      leg.exchange,
          product:       leg.product || 'NRML',
          order_type:    hasLimit ? 'LIMIT' : 'MARKET',
          price:         hasLimit ? Number(leg.limit) : 0,
          variety:       'regular',
          account:       acct,
          // Chase is ON for every limit-bearing leg; per-leg
          // aggressiveness comes from the pill's C[L|M|H] picker
          // (defaults to 'low' on quick-add). MARKET legs ignore
          // these on the backend.
          chase:                hasLimit,
          chase_aggressiveness: hasLimit ? (leg.chaseAgg || 'low') : 'low',
        });
      } catch (e) {
        const msg = String(/** @type {any} */ (e)?.message || e || 'failed');
        failures.push(`${leg.side} ${leg.sym}: ${msg}`);
      }
      basketProgress += 1;
    }
    basketPlacing = false;
    if (failures.length === chainBasket.length) {
      basketError = failures[0] || 'All legs failed';
    } else if (failures.length) {
      basketError = `${failures.length}/${chainBasket.length} failed: ${failures[0]}`;
      chainBasket = [];
    } else {
      chainBasket    = [];
      basketJustDone = true;
      setTimeout(() => { basketJustDone = false; }, 2200);
    }
  }

  /** Underlyings the chain picker offers, in priority order:
   *  1. The page's currently-selected underlying (the operator's
   *     anchor — first thing they see).
   *  2. Underlyings already on the operator's loaded book (positions
   *     and holdings).
   *  3. Common indices + MCX commodities — quick-access bucket so
   *     the operator can pivot to a new instrument without typing.
   *  4. Everything else from the instruments cache, alphabetical.
   *  Re-derives whenever the page's selectedUnderlying, positions,
   *  or instrumentsReady changes — the chain picker always reflects
   *  the freshest book without a manual refresh. */
  const _COMMON_INDICES_AND_COMMODITIES = POPULAR_UNDERLYINGS;
  // Static slice of the chain-picker universe — common indices /
  // commodities + every underlying from the instruments cache. Depends
  // only on `instrumentsReady`, so the ~5 k-entry universe scan runs
  // ONCE after the cache warms, not on every 30 s positions poll
  // (which was the prior cost — re-scanning the full Kite dump just to
  // surface a dropdown the operator isn't even looking at).
  const _staticUnderlyingChoices = $derived.by(() => {
    if (!instrumentsReady) return /** @type {string[]} */ ([]);
    const seen = new Set();
    /** @type {string[]} */
    const out = [];
    for (const u of _COMMON_INDICES_AND_COMMODITIES) {
      const k = String(u || '').toUpperCase();
      if (k && !seen.has(k)) { seen.add(k); out.push(k); }
    }
    for (const u of suggestUnderlyings('', 100000)) {
      const k = String(u || '').toUpperCase();
      if (k && !seen.has(k)) { seen.add(k); out.push(k); }
    }
    return out;
  });
  // Dynamic prefix — currently-selected underlying + everything held.
  // Cheap to rebuild on each positions poll.
  const underlyingChoices = $derived.by(() => {
    if (!instrumentsReady) return [];
    const seen = new Set();
    /** @type {string[]} */
    const out = [];
    const push = (/** @type {string|null|undefined} */ u) => {
      if (!u) return;
      const k = String(u).toUpperCase();
      if (seen.has(k)) return;
      seen.add(k);
      out.push(k);
    };
    push(selectedUnderlying);
    for (const p of positions) {
      push(String(p.symbol || '').replace(/\d.*$/, ''));
    }
    for (const u of _staticUnderlyingChoices) push(u);
    return out;
  });

  // Expiry list rebuilds when the operator picks a different underlying.
  const chainExpiries = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
    // Use 'CE' as the type — every option underlying has both CE + PE
    // expiries on the same dates, so checking one is enough.
    return listExpiries(chainUnderlying.toUpperCase(), 'CE');
  });
  // Strike grid for the picked (underlying, expiry).
  const chainStrikes = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying || !chainExpiry) return [];
    return listStrikes(chainUnderlying.toUpperCase(), 'CE', chainExpiry);
  });

  /** Spot fetched directly via /api/options/spot for the current
   *  (chainUnderlying, chainExpiry) pair. Lets the chain picker
   *  anchor the ATM highlight + spot pill on whatever underlying
   *  the operator switches to, even when it doesn't match the
   *  page's primary underlying. Refreshes on every chain pivot
   *  (effect below). Stays null until the first fetch lands; the
   *  derived `chainSpot` falls back to strategy.spot when this is
   *  unavailable. */
  /** @type {{spot:number, source:string, prevClose:number|null, contract:string|null} | null} */
  let chainSpotFetched = $state(null);
  let chainSpotKey = ''; // last-fetched cache key — skip duplicate calls
  $effect(() => {
    void chainUnderlying; void chainExpiry; void showAddPanel;
    untrack(() => {
      if (!showAddPanel || !chainUnderlying) {
        chainSpotFetched = null;
        chainSpotKey = '';
        return;
      }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry || ''}`;
      if (key === chainSpotKey) return;
      chainSpotKey = key;
      const u = chainUnderlying;
      const e = chainExpiry || null;
      fetchOptionsSpot(u, e).then((r) => {
        // Race guard — operator may have flipped the picker between
        // the request and this resolution.
        if (chainSpotKey !== key) return;
        chainSpotFetched = r ? {
          spot:      Number(r.spot) || 0,
          source:    String(r.spot_source || ''),
          prevClose: r.spot_prev_close != null ? Number(r.spot_prev_close) : null,
          contract:  r.spot_anchor_contract ? String(r.spot_anchor_contract) : null,
        } : null;
      }).catch(() => {
        if (chainSpotKey !== key) return;
        // 502 / 4xx — broker unreachable. Suppress the highlight
        // rather than anchoring on a synthetic value.
        chainSpotFetched = null;
      });
    });
  });

  /** Per-strike CE + PE bid/ask map, populated by
   *  /api/options/chain-quotes whenever (chainUnderlying, chainExpiry)
   *  changes while the panel is open. Keyed by strike → {ce, pe} →
   *  {bid, ask}. Stays null until the first fetch resolves; UI shows
   *  '—' for absent sides so layout doesn't jump as data lands. */
  /** @type {Record<string,{
   *    ce:{bid:number|null,ask:number|null},
   *    pe:{bid:number|null,ask:number|null}
   *  }> | null} */
  let chainQuotesMap = $state(null);
  let chainQuotesKey = '';
  let chainQuotesPoll = /** @type {any} */ (null);
  function _refreshChainQuotes({ force = false } = {}) {
    if (!showAddPanel || !chainUnderlying || !chainExpiry) return;
    // Bid/ask quotes are static when both NSE + MCX are closed —
    // the marketAwareInterval below only fires the periodic poll during
    // market hours. But the FIRST call on panel-open must fire even
    // during closed hours so operators see the last-known bid/ask
    // snapshot; the backend serves the ohlc close from cache. The
    // `force` flag from that first invocation bypasses the market-open
    // gate; the periodic interval calls with no arg → gates as normal.
    if (!force && !isMarketOpen()) return;
    const u = chainUnderlying.toUpperCase();
    const e = chainExpiry;
    fetchChainQuotes(u, e).then((r) => {
      if (chainQuotesKey !== `${u}|${e}`) return;
      /** @type {Record<string,{ce:{bid:number|null,ask:number|null},pe:{bid:number|null,ask:number|null}}>} */
      const map = {};
      for (const row of (r?.rows || [])) {
        map[String(row.k)] = {
          ce: {
            bid: row.ce_bid == null ? null : Number(row.ce_bid),
            ask: row.ce_ask == null ? null : Number(row.ce_ask),
          },
          pe: {
            bid: row.pe_bid == null ? null : Number(row.pe_bid),
            ask: row.pe_ask == null ? null : Number(row.pe_ask),
          },
        };
      }
      chainQuotesMap = map;
    }).catch(() => { /* swallow — UI shows '—' */ });
  }
  $effect(() => {
    void chainUnderlying; void chainExpiry; void showAddPanel;
    untrack(() => {
      if (chainQuotesPoll) {
        chainQuotesPoll();
        chainQuotesPoll = null;
      }
      if (!showAddPanel || !chainUnderlying || !chainExpiry) {
        chainQuotesMap = null;
        chainQuotesKey = '';
        return;
      }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry}`;
      if (key !== chainQuotesKey) {
        chainQuotesMap = null;       // clear stale rows on pivot
        chainQuotesKey = key;
      }
      _refreshChainQuotes({ force: true });
      // marketAwareInterval — same self-pausing as visibleInterval but
      // also short-circuits outside trading hours, so the chain poll
      // stops calling isMarketOpen() + new Date() per tick when the
      // operator leaves the page open overnight.
      chainQuotesPoll = marketAwareInterval(_refreshChainQuotes, 5000);
    });
  });
  onDestroy(() => {
    if (chainQuotesPoll) chainQuotesPoll();
  });
  const _fmtLtp = priceFmt;

  /** Spot for the chain underlying. Prefers the strategy response's
   *  spot when chainUnderlying matches the page's primary underlying
   *  (free, no extra round-trip); otherwise uses whatever the
   *  /options/spot fetch returned. Null when neither is available
   *  — the picker collapses the ATM highlight + spot pill rather
   *  than anchoring on a synthetic value. */
  const chainSpot = $derived.by(() => {
    if (!chainUnderlying) return null;
    const cU = chainUnderlying.toUpperCase();
    if (strategy) {
      const sUnd = String(strategy.underlying || '').toUpperCase();
      if (sUnd && sUnd === cU) return Number(strategy.spot) || null;
    }
    if (chainSpotFetched && chainSpotFetched.spot > 0) {
      return chainSpotFetched.spot;
    }
    return null;
  });

  /** Strike closest to the underlying spot — the ATM row. Used to
   *  highlight the row in the chain table and auto-scroll it into
   *  view when the chain opens. Null when chainSpot is unknown
   *  (different underlying or no strategy yet). */
  const chainAtmStrike = $derived.by(() => {
    if (chainSpot == null || !chainStrikes.length) return null;
    let best = chainStrikes[0];
    let bestDiff = Math.abs(best - chainSpot);
    for (const k of chainStrikes) {
      const d = Math.abs(k - chainSpot);
      if (d < bestDiff) { best = k; bestDiff = d; }
    }
    return best;
  });

  /** ATM-row DOM ref — captured via the `chainAtmRow` Svelte action
   *  attached to the ATM <tr>. The action fires when the element
   *  mounts (or remounts due to a key change), updates the ref, and
   *  the effect below scrolls it into view. Using an action sidesteps
   *  the `bind:this` constraint (which only accepts a plain
   *  identifier, not a conditional expression). */
  /** @type {HTMLTableRowElement | null} */
  let chainAtmRowEl = $state(null);
  /** Svelte action — captures the <tr> element on mount, releases it
   *  on destroy. Used in place of `bind:this` since the ATM row's
   *  identity flips between strike rows as the underlying spot moves. */
  /** @type {(node: HTMLTableRowElement) => { destroy(): void }} */
  const chainAtmRow = (node) => {
    chainAtmRowEl = node;
    return {
      destroy() { if (chainAtmRowEl === node) chainAtmRowEl = null; },
    };
  };
  $effect(() => {
    void chainAtmRowEl;
    void chainAtmStrike;
    void showAddPanel;
    if (chainAtmRowEl && showAddPanel) {
      // Defer one tick so the row has been laid out before we scroll.
      queueMicrotask(() => {
        // Scroll INSIDE .chain-grid-wrap only — earlier we used
        // rowEl.scrollIntoView({block:'center'}) which propagates up
        // every scrollable ancestor and yanks the whole page up,
        // hiding the Account / Underlying / Expiry picker bar at the
        // top of the panel. Computing scrollTop on the wrapper keeps
        // the page in place and only moves the chain grid.
        const row  = chainAtmRowEl;
        if (!row) return;
        const wrap = row.closest('.chain-grid-wrap');
        if (wrap) {
          const target = row.offsetTop - (wrap.clientHeight - row.offsetHeight) / 2;
          wrap.scrollTop = Math.max(0, target);
        } else {
          row.scrollIntoView({ block: 'nearest', behavior: 'auto' });
        }
      });
    }
  });

  // Futures contracts on the same underlying — surfaced as a quick-add
  // row above the strike grid. Filter to the selected expiry first; if
  // none match (futures and option expiries can drift by a day in
  // weekly cycles), show whichever future is closest. Futures aren't
  // strikable, so they appear once per chain, not per row.
  const chainFutures = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
    const all = listFutures(chainUnderlying.toUpperCase()) || [];
    if (!chainExpiry) return all.slice(0, 3);
    // Exact expiry match first — futures + options sometimes share
    // the same Thursday (the monthly-options + monthly-future date).
    const exact = all.filter(f => f.x === chainExpiry);
    if (exact.length) return exact;
    // Fallback: month-of match. Indian weekly options expire on
    // Thursdays; their natural pairing is the monthly future for
    // the same calendar month, which expires on the LAST Thursday
    // of that month. Picking by year-month captures this without
    // hard-coding the last-Thursday rule.
    const ym = String(chainExpiry).slice(0, 7);   // 'YYYY-MM'
    return all.filter(f => String(f.x || '').slice(0, 7) === ym);
  });

  // Default the chain underlying to the top of the priority list
  // (selectedUnderlying when set, otherwise the operator's first
  // book underlying) once the instruments cache has loaded. Empty
  // out the picked value if it disappears from the list.
  $effect(() => {
    const list = underlyingChoices;
    untrack(() => {
      if (!list.length) {
        if (chainUnderlying) chainUnderlying = '';
        return;
      }
      if (!chainUnderlying || !list.includes(chainUnderlying)) {
        chainUnderlying = list[0];
      }
    });
  });

  // Auto-pick first expiry when chain underlying changes.
  $effect(() => {
    void chainUnderlying;
    if (chainExpiries.length && !chainExpiries.includes(chainExpiry)) {
      chainExpiry = chainExpiries[0];
    }
  });

  // Order-ticket state — chain clicks open the reusable
  // <SymbolPanel> modal; on DRAFT submit we append to drafts.
  /** @type {any} */
  let ticketProps = $state(null);
  function openTicket(/** @type {any} */ p) { ticketProps = { defaultTab: 'ticket', ...p }; }

  function closeTicket() { ticketProps = null; }

  // Chain "+" handlers — open the OrderTicket pre-filled. The ticket
  // routes back here via onSubmit when the operator confirms; in
  // DRAFT mode we just push onto the drafts array (the existing
  // strategy auto-recompute picks it up).
  // Account hand-off: pre-select when the operator filtered to one
  // account; otherwise leave blank and let the ticket force a pick.
  // The ticket itself owns the dropdown UI, so pages just pass the
  // candidate list + an optional default.
  /** Account name "looks real" — i.e. it's not the masked `ZG####`
   *  form that non-admin sessions see on positions/holdings. The
   *  OrderTicket needs the real account_id to route correctly, so
   *  we filter masked entries out of any default-pick logic. */
  function _isRealAccount(/** @type {string|null|undefined} */ a) {
    return !!(a && !String(a).includes('#'));
  }
  function _ticketAccountDefault() {
    // Filter is a leg filter, not an order-routing hint. Only seed when
    // the choice is unambiguous: exactly one real account is on the
    // page, or the operator's filter picks exactly one. Otherwise
    // leave blank so the OrderTicket modal's own picker forces a
    // deliberate choice.
    if (selectedAccounts.length === 1 && _isRealAccount(selectedAccounts[0])) {
      return selectedAccounts[0];
    }
    if (realAccounts.length === 1) return realAccounts[0];
    return '';
  }
  // Per-row buttons pass the side explicitly. Default to chainSide so
  // any caller that hasn't been migrated yet still works. Qty sizing:
  // contract lot size × chainLots, clamped so a 0/blank lots input
  // doesn't post a zero-qty order.
  function addChainDraft(/** @type {number} */ strike,
                         /** @type {'CE'|'PE'} */ optType,
                         /** @type {'long'|'short'} */ side = chainSide) {
    if (!chainUnderlying || !chainExpiry) return;
    const inst = findOption(chainUnderlying.toUpperCase(), optType, strike, chainExpiry);
    if (!inst) return;
    const lot  = Number(inst.ls || 1);
    openTicket({
      defaultTab: 'chain',
      symbol:   inst.s,
      // Exchange comes from the instruments cache (Kite's authoritative
      // `e` field per contract). CRUDEOIL options live on MCX, NIFTY
      // options on NFO, BSE options on BFO — hardcoding NFO would route
      // every depth lookup to the wrong exchange and the ladder would
      // come back empty.
      exchange: inst.e || 'NFO',
      side:     side === 'long' ? 'BUY' : 'SELL',
      // Add path: always 1 lot. Operator steps it up in the ticket.
      qty:      lot,
      lotSize:  lot,
      accounts: ticketAccounts,
      account:  _ticketAccountDefault(),
    });
  }
  function addFutureDraft(/** @type {string} */ sym,
                          /** @type {number} */ lotSize,
                          /** @type {'long'|'short'} */ side = chainSide) {
    if (!sym) return;
    const lot  = Number(lotSize || 1);
    // Look up the instrument so we route to the right exchange
    // (commodity futures live on MCX, not NFO).
    const inst = getInstrument(sym.toUpperCase());
    openTicket({
      symbol:   sym,
      exchange: inst?.e || 'NFO',
      side:     side === 'long' ? 'BUY' : 'SELL',
      qty:      lot,
      lotSize:  lot,
      accounts: ticketAccounts,
      account:  _ticketAccountDefault(),
    });
  }

  /** Pre-fill account when the row already names one. Falls back
   *  to the page-level default. Masked (`ZG####`) values get
   *  filtered so the ticket never seeds with an unroutable
   *  account. */
  function _rowTicketAccount(/** @type {{account?: string}} */ c) {
    const fromRow = String(c.account || '').trim();
    if (_isRealAccount(fromRow)) return fromRow;
    return _ticketAccountDefault();
  }

  /** Click-on-row handler — opens the OrderTicket pre-filled to
   *  close the row's position. Skipped for drafts (no real
   *  exposure to close) and zero-qty rows (already closed —
   *  sorted to end of list, included for visibility). The
   *  checkbox inside the row stops propagation so its toggle
   *  doesn't double-fire as a close. */
  async function closePosition(/** @type {any} */ c) {
    if (!c?.symbol || c.source === 'draft') return;
    // Effective qty for the close ticket. When the row came from the
    // Close tab's netting pass, `_residualQty` carries the un-netted
    // remainder — that's what actually needs closing, not the gross
    // position. Falls back to c.qty for ordinary Legs-tab rows.
    const effectiveSignedQty = c._residualQty != null
      ? Number(c._residualQty)
      : Number(c.qty || 0);
    const qty = Math.abs(effectiveSignedQty);
    if (!qty) return;       // already closed (or fully netted)
    // D2 fix (2026-09): loadInstruments() is memoized (see
    // frontend/src/lib/data/instruments.js) so this is a no-op once the
    // cache is warm, but rescues a close click that lands before the
    // cache has loaded — without it, `inst` resolves to null, lot size
    // falls back to a bogus 1, and OrderTicket sends the raw contract
    // count as if it were lots (a many-times oversize CLOSE order).
    await loadInstruments().catch(() => {});
    const inst = getInstrument(String(c.symbol).toUpperCase());
    const lot  = Number(inst?.ls) || 0;
    openTicket({
      symbol:   c.symbol,
      exchange: inst?.e || 'NFO',
      side:     effectiveSignedQty < 0 ? 'BUY' : 'SELL',   // opposite of held
      action:   'close',
      qty,
      lotSize:  lot,
      // Pass signed qty so the OrderTicket can label the side toggle
      // as ADD / CLOSE (instead of BUY / SELL) — operator thinks in
      // "I want to close this position" terms when clicking on a
      // current position row.
      currentQty: effectiveSignedQty,
      accounts: ticketAccounts,
      account:  _rowTicketAccount(c),
    });
  }

  /** Click handler for draft rows — opens OrderTicket pre-filled in
   *  PAPER mode (LIVE pill available too) so the operator can
   *  convert a draft into a real order. The draft's id is stashed
   *  on ticketProps; onTicketSubmit removes the draft from drafts[]
   *  on a successful PAPER / LIVE submit, so the operator doesn't
   *  end up with both a draft AND a real position for the same leg. */
  function executeDraft(/** @type {any} */ c) {
    if (c.source !== 'draft' || !c.symbol) return;
    const sym  = String(c.symbol).toUpperCase();
    const inst = getInstrument(sym);
    const lot  = Number(inst?.ls || 1);
    // Add path: default to 1 lot regardless of the draft's recorded qty.
    // Operator can step it up in the ticket; close path keeps existing qty.
    const qty  = lot;
    const side = Number(c.qty || 0) < 0 ? 'SELL' : 'BUY';
    const acct = _ticketAccountDefault();
    openTicket({
      symbol:    sym,
      exchange:  inst?.e || 'NFO',
      side,
      action:    'open',
      qty,
      lotSize:   lot,
      price:     c.avg_cost != null && c.avg_cost !== ''
                   ? Number(c.avg_cost) : undefined,
      accounts:  ticketAccounts,
      account:   acct,
      // defaultMode + availableModes removed (Wave C); navbar's
      // executionMode store decides mode for the modal.
      _draftId:  c.draftId,
    });
  }

  // Ticket → drafts: signed qty (BUY = +qty, SELL = −qty) so the
  // existing payoff math keeps working. Auto-aligns the page
  // underlying so the new draft surfaces in candidates immediately.
  // PAPER / LIVE submits when ticketProps carries _draftId clear the
  // matching draft so the operator doesn't end up with both.
  function onTicketSubmit(/** @type {any} */ payload) {
    if (payload.mode === 'paper' || payload.mode === 'live') {
      const did = ticketProps?._draftId;
      if (did != null) {
        drafts = drafts.filter(d => d.id !== did);
      }
      // Non-blocking completion notification — surfaces order_id +
      // status without making the operator close the OrderTicket modal
      // or lose their place in the chain. Auto-dismisses in 5 s;
      // operator can click to dismiss earlier.
      _pushOrderToast(payload);
      return;
    }
    if (payload.mode !== 'draft') return;
    const signedQty = payload.side === 'BUY'
      ? Math.abs(Number(payload.quantity || 0))
      : -Math.abs(Number(payload.quantity || 0));
    drafts = [...drafts, {
      id:       ++_draftSeq,
      symbol:   String(payload.symbol),
      qty:      signedQty,
      avg_cost: payload.price != null ? Number(payload.price) : '',
      ltp:      '',
    }];
    if (!selectedUnderlying && chainUnderlying) {
      selectedUnderlying = chainUnderlying.toUpperCase();
    }
  }

  // Position lists for the picker. Carries avg_cost + ltp so that
  // the strategy leg-builder can ship them inline (sim legs need this
  // because the backend can't fetch their ltp from the broker).
  /** @type {Array<{symbol:string, account:string, qty:number, source:string, avg_cost:number|null, ltp:number|null, prev_close:number|null, pnl:number, day_change_val:number, overnight_quantity:number, realised:number, day_buy_quantity:number, day_sell_quantity:number, day_buy_value:number, day_sell_value:number, prev_settlement_pnl?:number|null}>} */
  let positions = $state([]);
  // Last-known pulse positions — used as fallback in loadPositions() when
  // positionsStore.value has no rows. Plain let (not $state) since it is
  // only read inside the async loadPositions() function, not in $derived.
  let _lastDervPulsePos = /** @type {any[]} */ ([]);

  /** Per-row "EV" column value (§5 EV fix) — real merged EV for the
   *  currently-selected underlying when a strategy is loaded (matches the
   *  Payoff overlay's EV chip), Exp P&L as a proxy for every other root
   *  (no per-underlying EV without a loaded strategy for that root).
   *  Shared by the row template AND _snapshotTotalEvFull below so TOTAL
   *  equals the sum of the visible rows BY CONSTRUCTION — matches what
   *  the column tooltip claims. (Previously _snapshotTotalEvFull computed
   *  `base − _legsExpPnlTotal + mergedEv`, where `base` no longer
   *  contained exactly `_legsExpPnlTotal` for the selected underlying
   *  after the §5 filtered-per-root Exp P&L fix, leaving a residual
   *  error term — and it added mergedEv even when the selected
   *  underlying wasn't among the displayed rows.) */
  function _rowEvFor(/** @type {string} */ underlying) {
    // _strategyStale gate (same pattern as the OptionsPayoff overlay's
    // `payoffDisplay`): for the WHOLE fetch duration between
    // selectedUnderlying changing and loadStrategy({clear:true}) landing
    // (not "one render frame" — see _strategyStale's own comment),
    // `strategy` (and therefore _mergedEv) still holds the PREVIOUS
    // root's data. Without this gate that stale EV briefly displays on
    // the NEW root's row (and sums into TOTAL) before the real fetch
    // completes.
    if (underlying === selectedUnderlying && !_strategyStale && _mergedEv != null) return _mergedEv;
    // Exp P&L proxy — delegates to _rowExpPnlFor rather than indexing
    // _filteredExpPnlByRoot directly, so this fallback always matches
    // whatever the Exp P&L column itself shows for the same row.
    // _rowExpPnlFor no longer special-cases the selected underlying
    // (item-3 fix, round 4) so this is now the SAME filtered per-root
    // reduction for every row, selected or not.
    return _rowExpPnlFor(underlying);
  }

  const _snapshotTotalEvFull = $derived.by(() =>
    _byUnderlyingTotals.reduce((s, g) => s + _rowEvFor(g.underlying), 0)
  );

  /** Raw broker holdings keyed by symbol. When the operator picks an
   *  underlying that they ALSO hold the cash equity for, the holding
   *  appears as a long-equity leg in candidatePositions so the payoff
   *  curve reflects covered calls / hedges correctly. */
  /** @type {Array<{symbol:string, account:string, qty:number, avg_cost:number|null, ltp:number|null, prev_close:number|null, pnl:number, day_change_val:number}>} */
  let holdings = $state([]);
  // Last-known holdings from holdingsStore — used in loadPositions() right after
  // await holdingsStore.load() as a guard against a transient null value.
  let _lastDervHold = /** @type {any[]} */ ([]);
  // Track last-known values from pulse/holdings stores for use in the
  // async loadPositions() function (plain $effects, not $derived).
  $effect(() => {
    const v = pulsePositionsStore.value;
    if (v != null) _lastDervPulsePos = v;
  });
  $effect(() => {
    const v = holdingsStore.value;
    if (v != null) _lastDervHold = v;
  });
  /** Per-account totals of the rows the page FILTERS OUT of `positions`
   *  (equity intraday) and `holdings` (derivative-looking). The
   *  navbar PositionStrip sums every row from /api/positions +
   *  /api/holdings without any F&O / equity filter; the page only
   *  carries F&O rows for the per-underlying display. To keep the
   *  Snapshot TOTAL row in sync with the strip when no account
   *  filter is picked, the snapshot derivation adds these excluded
   *  totals back. Keyed by account so an account-filter still
   *  partitions correctly.
   *  Operator: "still snapshot totals not in sync with nav strip numbers." */

  /** Real (unmasked) broker account IDs from /api/accounts/. Loaded
   *  separately from positions because /positions masks the account
   *  field for non-admin signed-in users (server-side mask_column),
   *  which would leave the OrderTicket with un-tradeable "ZG####"
   *  options. The /accounts endpoint is jwt-guarded but doesn't
   *  mask, so any signed-in user gets the real account_ids and the
   *  ticket can route the order to the right Kite handle.
   *
   *  Stays empty for anonymous demo sessions — the OrderTicket
   *  routes such orders through the demo paper-engine path which
   *  doesn't need a real account anyway. */
  /** @type {string[]} */
  let realAccounts = $state([]);

  /** ID of the user's default watchlist — populated on mount so chain
   *  rows can drop a "+W" button that adds the strike to the right
   *  list with zero extra clicks. Stays null on demo / unauthenticated
   *  sessions and the button hides. */
  let defaultWatchlistId = $state(/** @type {number | null} */ (null));
  /** F&O-eligible underlying roots extracted from pinned (is_pinned / is_global)
   *  watchlists. Drives Tier 4 in the underlying picker — pinned lists come before
   *  regular lists so the operator's curated symbols appear first. Stays [] until
   *  instruments are ready (getOptionUnderlyingLot needs the instruments cache). */
  let _pinnedWatchlistRoots = $state(/** @type {string[]} */ ([]));
  /** F&O-eligible underlying roots from non-pinned operator watchlists.
   *  Drives Tier 5 in the underlying picker — appears after pinned watchlist
   *  roots and before the static POPULAR_UNDERLYINGS fallback. */
  let _regularWatchlistRoots = $state(/** @type {string[]} */ ([]));
  /** Per-strike|optType toast confirming the watchlist add. Keyed
   *  by `${strike}|${CE|PE}` so each chain row tracks its own. */
  let watchToast = $state(/** @type {{ key: string, msg: string } | null} */ (null));

  async function loadRealAccounts() {
    try {
      const r = await fetchAccounts();
      const list = (r?.accounts || [])
        .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
        .filter(Boolean);
      realAccounts = sortAccountsBy(list, getAccountOrderMap());
    } catch (_) {
      // 401 / 403 (anonymous demo on prod) — keep realAccounts
      // empty; the ticket falls back to the masked accountChoices.
      realAccounts = [];
    }
  }

  /** Extract F&O-eligible underlying roots from a list of tradingsymbols.
   *  Bare equity/index symbols (NIFTY, RELIANCE) are checked directly via
   *  getOptionUnderlyingLot. Derivative symbols (CRUDEOIL26JUNFUT) are
   *  decomposed and their root is checked. Requires instruments cache to be
   *  warm — call only after loadInstruments() has resolved.
   * @param {string[]} syms */
  function _extractFOUnderlyingRoots(syms) {
    const roots = new Set();
    for (const sym of syms) {
      const s = sym.toUpperCase().replace(/\s+/g, '');
      if (!s) continue;
      if (getOptionUnderlyingLot(s) > 0) { roots.add(s); continue; }
      const d = decomposeSymbol(s);
      if (d?.root && getOptionUnderlyingLot(d.root) > 0) roots.add(d.root);
    }
    return Array.from(roots).sort();
  }

  async function loadDefaultWatchlist() {
    try {
      const result = await loadWatchlistSymbols();
      const lists = result.lists ?? [];
      const def = lists.find(/** @param {any} l */ (l) => l?.is_default) ?? lists[0];
      if (def) defaultWatchlistId = Number(def.id);
      _pinnedWatchlistRoots  = _extractFOUnderlyingRoots(result.pinnedSyms ?? []);
      _regularWatchlistRoots = _extractFOUnderlyingRoots(result.regularSyms ?? []);
    } catch (_) {
      // Demo / unauthenticated — leave null; the "+W" button hides.
      defaultWatchlistId = null;
    }
  }

  /** Add a chain row to the user's default watchlist. Resolves the
   *  contract via the instrument cache so the tradingsymbol matches
   *  exactly what the broker knows. */
  /** Generic "add this symbol to the default watchlist" — used by
   *  the SymbolActions component on the Candidates rows. Doesn't
   *  need a strike/optType because it acts on a resolved
   *  tradingsymbol directly. */
  async function addSymbolToWatchlist(
    /** @type {string} */ sym,
    /** @type {string|undefined} */ exchange,
  ) {
    if (defaultWatchlistId == null) throw new Error('No default watchlist');
    await addWatchlistItem(defaultWatchlistId, String(sym), exchange || 'NFO');
  }

  async function addOptionToWatchlist(
    /** @type {number} */ strike,
    /** @type {'CE'|'PE'} */ optType,
  ) {
    if (defaultWatchlistId == null) return;
    if (!chainUnderlying || !chainExpiry) return;
    const inst = findOption(
      chainUnderlying.toUpperCase(), optType, strike, chainExpiry,
    );
    if (!inst) { basketError = 'Symbol not in instruments cache.'; return; }
    const key = `${strike}|${optType}`;
    try {
      await addWatchlistItem(defaultWatchlistId, String(inst.s), inst.e || 'NFO');
      watchToast = { key, msg: '+ Watch' };
    } catch (e) {
      // 409 = already in list — show a friendlier confirmation rather
      // than a red error, since the operator's intent was met.
      watchToast = { key, msg: /already/i.test(e?.message || '') ? 'in list' : 'err' };
    }
    setTimeout(() => { if (watchToast?.key === key) watchToast = null; }, 1200);
  }

  /** Account list to feed the OrderTicket — prefers the unmasked
   *  /api/accounts/ result; falls back to whatever's on the loaded
   *  positions (which may be masked for non-admin sessions). */
  const ticketAccounts = $derived(
    realAccounts.length ? realAccounts : accountChoices.map(String)
  );

  // Surface a banner when /api/positions fails so the operator sees
  // WHY the page is empty (instead of an opaque "no candidates" hint).
  // Consecutive-failure gate: mirror the _stratFails pattern — suppress
  // the banner on a single transient hiccup (page open during a brief
  // backend hiccup, race with book-poller's in-flight, etc.) and only
  // escalate after 2+ consecutive failures.
  let positionsLoadErr = $state('');
  let _posLoadFails = 0;
  // When broker health is confirmed green, the poll error is a snapshot lag
  // (not a genuine outage) — reset the failure counter and clear the error.
  // Only fires on green transitions; no else-branch so the poll loop that
  // also writes positionsLoadErr isn't fought by this effect.
  $effect(() => {
    if (_brokerWorstState === 'green') {
      _posLoadFails = 0;
      positionsLoadErr = '';
    }
  });
  // Latches to true after the first successful positions+holdings load.
  // Gates `_hedgeOpportunities` so it only appears in the underlying
  // picker AFTER we know which underlyings have actual derivative
  // positions — preventing the dropdown from briefly listing all
  // held stocks (hedge-opp tier) before positions arrive, then
  // replacing them with the correct option-root tier, which the
  // operator perceived as "sometimes showing all underlyings."
  let _positionsLoaded = $state(false);
  // Guard for the one-time promote logic in the auto-select $effect.
  // Set to true after the first promote fires (positions → active underlying)
  // so subsequent 5s position refreshes don't bounce the operator back to
  // the active underlying if they manually picked an inactive one (e.g. COPPER
  // for analysis). Reset is intentionally omitted — page reload is the only
  // way to re-trigger the one-time promote.
  let _autoSelectDone = $state(false);

  // Propagate book-poller updates (positionsStore.value refreshes every 5s)
  // into the local `positions` $state without requiring a full loadPositions()
  // call. The effect reads positionsStore.value as the reactive dependency and
  // uses untrack() for all writes so it doesn't create a circular dependency.
  // Skips until _positionsLoaded is true (first full load sets the baseline).
  // Sim rows (source === 'sim') are preserved from the current positions array
  // since positionsStore only holds live broker rows.
  $effect(() => {
    const rawPos = positionsStore.value;
    if (!rawPos || !_positionsLoaded) return;
    untrack(() => {
      // Shared-root fallback (0-vs-last-known-good fix, frontend half):
      // the conn-service can mask a genuine fetch failure as an HTTP 200
      // with `accounts: []`, which arrives here as `rawPos = []` —
      // indistinguishable, at this layer, from "operator genuinely
      // closed every F&O position." `loadPositions()` elsewhere in this
      // file falls back to `_lastDervPulsePos` for exactly this reason,
      // but that store is fed by a DIFFERENT poll (pulsePositionsStore)
      // that can be degraded by the SAME masked-failure window at the
      // same moment — an unconditional fallback to it wouldn't actually
      // guarantee real data. Safer: when this cycle's read is empty AND
      // we're currently showing live (non-sim) rows, treat it as a
      // suspected degraded read and skip the write entirely — freeze
      // `positions` at its last-known-good value rather than overwriting
      // with a false empty. Deliberately does NOT stamp
      // `_positionsRefreshedAt` on this path either — a suspected-
      // degraded read must not count as "confirmed fresh" for the
      // _positionsFresh gate that governs the equity-only-synth
      // strategy-wipe branch (see the §5 comment above): wiping the
      // payoff strategy on a masked failure would reproduce the exact
      // flat-line-at-0 bug this fix removes. A genuinely empty book
      // (operator closed everything) is NOT permanently stuck — the
      // very next confirmed-non-empty OR the periodic `loadPositions()`
      // full-load path (5s cadence) will resolve it; this is a narrow,
      // known trade-off documented here rather than solved locally —
      // the proper disambiguation (backend degraded/stale_accounts tag)
      // is being threaded through by the parallel NavStrip-data-layer fix.
      const _hasLiveRows = positions.some(r => r.source !== 'sim');
      if (rawPos.length === 0 && _hasLiveRows) return;
      const merged = [];
      /** @type {Record<string, {pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} */
      const excluded = {};
      for (const p of rawPos) {
        const sym = p?.tradingsymbol || p?.symbol;
        if (!sym) continue;
        if (!isFOSymbol(sym)) {
          bumpExcluded(excluded, p?.account, {
            pos_pnl: Number(p?.pnl || 0),
            pos_day: baseDayPnlForPosition(p),
          });
          continue;
        }
        const baseRow = buildPositionRowFromBroker(p, 'live');
        for (const row of splitClosedReopened(baseRow)) merged.push(row);
      }
      const simRows = positions.filter(r => r.source === 'sim');
      positions = [...merged, ...simRows];
      // §5: this propagation path also refreshes positions data (every 5s
      // via positionsStore.value's book-poller cadence) — it must stamp
      // _positionsRefreshedAt too, not just loadPositions(). Without this,
      // switching to a position-less root can permanently stick the
      // Payoff card in "loading": loadStrategy()'s equity-only-synth
      // branch gates the strategy-wipe on _positionsFresh (< 30s since
      // _positionsRefreshedAt), and if this propagation effect is the
      // ONLY thing keeping positions current (loadPositions() not
      // re-invoked), _positionsRefreshedAt goes stale, _positionsFresh
      // stays false forever, strategy never nulls out for the new root,
      // and _strategyStale (strategy.underlying mismatch) never clears.
      _positionsRefreshedAt = Date.now();
    });
  });

  // splitClosedReopened — imported from $lib/derivatives/pageLoad.js
  // (moved for cc reduction; see pageLoad.js for the full implementation + docs)

  async function loadPositions({ fresh = false } = {}) {
    /** @type {Array<any>} */
    const merged = [];
    // Equity intraday positions (excluded from F&O view) are accumulated
    // here so the Snapshot TOTAL can reconcile with the navbar PositionStrip.
    /** @type {Record<string, {pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} */
    const _excluded = {};

    // Live broker positions — route through positionsStore so NavStrip and
    // this page share one SSOT fetch. Independent fetchPositions() calls
    // caused symbolStore to be written twice per poll (once by positionsStore
    // parse, once by publishPulseQuotes here), which oscillated liveLtp for
    // MCX futures that appear as both a position and an underlying anchor.
    await positionsStore.load({ fresh });
    // Stale-while-error: always process positionsStore.value even when
    // an error is set — the store retains the last-good broker snapshot,
    // so the dropdown and legs populate from cached data while a retry
    // is pending rather than going blank. Mirror the _stratFails pattern:
    // show the banner only after 2+ consecutive failures so a single
    // transient hiccup (deploy window, book-poller race) doesn't flash
    // a misleading red banner when the connection chip is green.
    if (positionsStore.error) {
      _posLoadFails++;
      positionsLoadErr = _posLoadFails >= 2 ? positionsStore.error : '';
    } else {
      _posLoadFails = 0;
      positionsLoadErr = '';
    }
    const _posSource = positionsStore.value?.length
      ? positionsStore.value
      : _lastDervPulsePos;
    for (const p of _posSource) {
      const sym = p?.tradingsymbol || p?.symbol;
      if (!sym) continue;
      if (!isFOSymbol(sym)) {
        // Equity intraday — excluded from F&O panel; capture for TOTAL reconcile
        bumpExcluded(_excluded, p?.account, {
          pos_pnl: Number(p?.pnl || 0),
          pos_day: baseDayPnlForPosition(p),
        });
        continue;
      }
      const baseRow = buildPositionRowFromBroker(p, 'live');
      for (const row of splitClosedReopened(baseRow)) merged.push(row);
    }

    // Sim positions — inline ltp so strategy endpoint can compute
    // analytics without an extra broker round-trip.
    try {
      const s = await fetchSimStatus();
      for (const p of (s?.positions || [])) {
        const sym = p?.symbol;
        if (!sym || !isFOSymbol(sym)) continue;
        const baseRow = buildPositionRowFromBroker(p, 'sim');
        for (const row of splitClosedReopened(baseRow)) merged.push(row);
      }
    } catch (_) { /* ignore */ }

    positions = merged;
    // Seed underlying spot prices immediately after positions land —
    // avoids blank payoff chart on cold load (before the first interval fires).
    loadUnderlyingQuotes();

    // Cash-equity holdings — skipped in sim (sim doesn't model equity book).
    // Only EQ rows are kept; derivative holdings are picked up by positions.
    if (!simActive) {
      await holdingsStore.load();
      const rows = [];
      // Use holdingsStore.value directly (set by load() above) with _lastDervHold
      // as a last-known-good fallback in case the store value is transiently null.
      for (const h of (holdingsStore.value ?? _lastDervHold)) {
        const sym = h?.tradingsymbol || h?.symbol;
        if (!sym) continue;
        if (isFOSymbol(sym)) {
          bumpExcluded(_excluded, h?.account, {
            hold_pnl: Number(h?.pnl || 0),
            hold_day: holdingsDayPnlStore.get(
              String(h?.tradingsymbol || h?.symbol || '').toUpperCase(), 0
            ).day_pnl,
          });
          continue;
        }
        const row = buildHoldingRowFromBroker(h);
        if (row) rows.push(row);
      }
      holdings = rows;
    } else {
      holdings = [];
    }

    _positionsLoaded   = true;
    _positionsRefreshedAt = Date.now();
    if (!positionsStore.error) lastRefreshAt.set(Date.now());

    // Do NOT include enabledSymbols in the positions-poll snapshot —
    // the strategy-success path persists selections; polls must not
    // overwrite an unchecked state with an all-checked snapshot.
    _saveCache({ includeSelections: false });
  }

  // Consecutive-failure counter for loadStrategy. Suppresses the
  // error banner on a single transient hiccup (page reopen during
  // a backend redeploy, slow first response on a cold connection,
  // etc.) — only escalates after 2+ failures in a row so the user
  // sees the chart appear cleanly when the next poll succeeds.
  let _stratFails = 0;
  // Generation counter — incremented at the start of every fetch so
  // a stale (slower) response that resolves after a newer request
  // has already started can detect itself as stale and discard its
  // result without overwriting the fresher data.
  let _stratGen = 0;

  // Memoize equity-only synth — every 5s poll calls loadStrategy. When
  // strategy is synthesized from eq legs, recomputing a fresh 41-point
  // payoff array (+ new strategy ref) on each tick triggers
  // `_mergedPayoff` / `_mergedRisk` / `_mergedGreeks` re-derive + the
  // entire OptionsPayoff SVG re-render. The synth output only depends
  // on (symbol, qty, avg_cost, ltp) per leg + selectedUnderlying; cache
  // the result by that signature so polls that bring no relevant
  // change return the same reference and downstream derives stay
  // memoized.
  let _synthCache = /** @type {{key: string, value: any} | null} */ (null);
  // Signature of the legs that produced the current `strategy` value
  // via the broker fetch path. Used to short-circuit duplicate
  // round-trips when nothing has changed AND a refresh isn't due yet
  // (see _stratLastFetchAt below) — the memo dedupes identical concurrent
  // requests, it must NOT suppress every periodic refresh.
  let _stratLastKey = '';
  // Timestamp (Date.now()) of the last successful fetch. buildCleanLegs
  // sends `ltp: null` for live (non-sim/draft) legs, so legsKey never
  // changes purely from price movement — without this, the legsKey memo
  // above silently blocked every 5s marketAwareInterval(loadStrategy)
  // tick once legs stopped changing, leaving `strategy.spot` (feeding
  // chartTheoreticalAtSpot/EV/POP/chainSpot) stale from whenever legs
  // last changed while candidatesActualPnl kept moving live — an implicit
  // double-count of price movement. A fetch is forced once REFRESH_MS has
  // elapsed since the last one, independent of legsKey.
  let _stratLastFetchAt = 0;
  const _STRAT_REFRESH_MS = 5000; // matches book-poll cadence
  // synthCacheKey, synthEquityOnlyStrategy — imported from $lib/derivatives/pageLoad.js
  // (renamed: synthCacheKey(underlying, eqs), synthEquityOnlyStrategy(eqs, underlying))

  /** @param {{ force?: boolean, clear?: boolean }} [opts] */
  async function loadStrategy(opts = {}) {
    // `clear: true` (passed by the selectedUnderlying-change $effect) force-
    // resets the legs-signature memo key. Placed FIRST, before the
    // equity-only-synth early return below, so it also covers the gap where
    // didUnderlyingChange() misses an underlying switch: the synth path
    // (no non-eq legs) returns early without ever touching _stratLastKey,
    // and didUnderlyingChange short-circuits to `false` when the CURRENT
    // strategy is a synth shell (its `legs` array is always []). Without
    // this reset, switching from an equity-only synth strategy to a real
    // option/futures strategy on the same poll cycle can hit the legsKey
    // memo and skip the fetch, leaving the stale synth payoff on screen
    // under the new underlying's label.
    if (opts?.clear) _stratLastKey = '';

    // Build clean legs (exclude eq kind, inline ltp for sim/draft, look up expiry).
    const cleanLegs = buildCleanLegs(legs, getInstrument);

    if (!cleanLegs.length) {
      // Equity-only path — synthesize a zero-baseline strategy shell so
      // _mergedPayoff can layer the linear long-stock contribution on top.
      const enabledEqs = _includeHoldings
        ? candidatePositions.filter(c => c.kind === 'eq' && _isLegEnabled(c))
        : [];
      if (enabledEqs.length > 0) {
        // Memoize by (underlying, per-leg signature) to skip re-render when
        // the 5s poll brings no relevant change.
        const key = synthCacheKey(selectedUnderlying, enabledEqs);
        if (!_synthCache || _synthCache.key !== key) {
          _synthCache = { key, value: synthEquityOnlyStrategy(enabledEqs, selectedUnderlying) };
        }
        if (strategy !== _synthCache.value) {
          strategy = _synthCache.value;
          debugLog('payoff:strategy', 'loaded', {
            underlying: strategy?.underlying,
            anchor: strategy?.spot_anchor_contract,
            legs: strategy?.legs?.length ?? 0,
            spot: strategy?.spot,
          });
        }
      } else {
        // Clear when no non-eq leg has a non-zero qty — closed-position-only
        // sets (all qty=0) must not keep the prior symbol's payoff chart
        // visible under the new symbol's label.
        // Guard: only wipe if positions data is fresh (<30s old). On
        // hibernation exit the marketAwareInterval edge fires loadStrategy
        // before the book poller has refreshed — stale qty=0 legs must not
        // blank the chart until the next poll tick delivers real data.
        const _hasEnabledLegs = legs.some(l => l.kind !== 'eq' && Number(l.qty) !== 0);
        const _positionsFresh = _positionsRefreshedAt > 0 && (Date.now() - _positionsRefreshedAt < 30_000);
        if (!_hasEnabledLegs && strategy !== null && _positionsLoaded && instrumentsReady && _positionsFresh) strategy = null;
        _synthCache = null;
      }
      strategyErr = ''; _stratFails = 0;
      return;
    }

    // Detect underlying switch — reset memo key so the fetch fires even
    // if legs key happens to be identical across the switch.
    if (didUnderlyingChange(cleanLegs, strategy, decomposeSymbol)) {
      _stratLastKey = '';
    }

    // Legs-signature memo: skip round-trip when inputs are unchanged, a
    // chart is already rendered, AND a periodic refresh isn't due yet.
    // The `strategy &&` guard is critical: without it a single failure
    // sets the key and recovery never fires. `_dueForRefresh` is what
    // keeps this a "dedupe identical concurrent requests" memo rather
    // than a blanket suppression of the 5s periodic refetch.
    const legsKey = computeLegsKey(cleanLegs);
    // ~10% margin below the nominal cadence (item-5 fix, round 4): the
    // caller is a setInterval/visibleInterval tick that fires at
    // approximately _STRAT_REFRESH_MS, not exactly — a tick landing a few
    // ms early (timer jitter, event-loop backpressure) would otherwise
    // miss `_dueForRefresh` by a hair and silently push the refresh to
    // the NEXT tick, halving the effective cadence to ~10s.
    const _dueForRefresh = (Date.now() - _stratLastFetchAt) >= (_STRAT_REFRESH_MS - 500);
    if (!opts?.force && strategy && legsKey === _stratLastKey && !_dueForRefresh) {
      strategyErr = ''; _stratFails = 0;
      return;
    }

    const _thisGen = ++_stratGen;
    if (!strategy) loading = true;
    // B1 fix: a routine refetch (strategy already loaded) sets ONLY
    // `_stratRefreshing` — the small stat-row spinner — never `loading`,
    // which would re-trigger OptionsPayoff's full-chart placeholder.
    _stratRefreshing = true;
    // Stamp BEFORE the await (item-6 fix), not after. Stamping post-await
    // only counted (5000ms − request latency) as "elapsed" by the time the
    // next 5s interval tick checked _dueForRefresh, so the effective
    // cadence became ~10s instead of 5s. It also left the stamp stale
    // WHILE a request was in flight, letting other independent triggers
    // (legs-change effect, bookChanged, a fill event) each decide a
    // refresh was "due" and fire their own overlapping request. Stamping
    // here — once the memo/dedupe checks above have already decided this
    // call IS going to fetch — closes both gaps.
    _stratLastFetchAt = Date.now();
    try {
      // No `spot` override (cold-start spot-anchor fix): sending a nonzero
      // override made the backend's _resolve_spot short-circuit to
      // `(override, "override", None, None)` — prev_close AND anchor_contract
      // came back null on every refetch after the first. That's a one-way
      // ratchet: the frontend permanently lost the signal it needs to
      // identify the anchor contract on the NEXT refetch, and the payoff
      // curve/Greeks/EV got priced against the override (front-month) instead
      // of the contract matching the legs' modal expiry for the rest of the
      // session. Backend's own resolution (_resolve_spot, unchanged) is
      // correct as-is — this is purely "stop calling it with the corrupting
      // argument." The Payoff overlay's spot marker is unaffected — it's
      // driven independently by `liveSpot` via props, not by this value.
      const resp    = await fetchStrategyAnalytics(cleanLegs, {});
      if (_thisGen !== _stratGen) return;
      strategy      = resp;
      _stratLastKey = legsKey;
      // _stratLastFetchAt is stamped BEFORE the await above (item-6 fix) —
      // not re-stamped here so an overlapping/superseded response (caught
      // by the _thisGen check) can't push the timestamp forward twice.
      strategyErr   = '';
      _stratFails   = 0;
      _saveCache();
      debugLog('payoff:strategy', 'loaded', {
        underlying: strategy?.underlying,
        anchor: strategy?.spot_anchor_contract,
        legs: strategy?.legs?.length ?? 0,
        spot: strategy?.spot,
      });
    } catch (e) {
      if (_thisGen !== _stratGen) return;
      _stratFails  += 1;
      if (!strategy && _stratFails >= 2) {
        strategyErr = /** @type {any} */ (e).message || String(e);
      }
    } finally {
      // B4 fix: a superseded (stale-generation) fetch's `finally` must NOT
      // clear `loading`/`_stratRefreshing` when a NEWER fetch is still in
      // flight — only the CURRENT generation's own finally may clear them.
      // Previously `finally` ran unconditionally (even after the early
      // `return` on a stale-gen catch/success above), so a slow superseded
      // response could clear `loading` while the real in-flight request
      // was still running, prematurely dropping the placeholder/spinner.
      if (_thisGen === _stratGen) {
        loading = false;
        _stratRefreshing = false;
      }
    }
  }

  async function loadSimStatus() {
    try {
      const s = await fetchSimStatus();
      simActive = !!s?.active;
    } catch (_) { /* keep last-known simActive; transient 502 shouldn't hide the badge */ }
  }

  // ── Stale-while-revalidate cache ──────────────────────────────────
  // sessionStorage-backed snapshot of the page's data + operator
  // selections so a tab reopen / SPA back-nav comes up with the
  // previous view (chart, dropdowns, leg toggles, drafts) instead of
  // a blank page. The first fresh fetch overwrites the snapshot;
  // entries > 5 minutes old are discarded so the operator never sees
  // a wildly stale chart.
  const _CACHE_KEY = 'ramboq:options-state';
  const _CACHE_MAX_AGE_MS = 5 * 60 * 1000;
  /** Persist the page snapshot to sessionStorage for stale-while-revalidate.
   *  @param {object} [opts]
   *  @param {boolean} [opts.includeSelections=true]  When false, omits
   *    `enabledSymbols` from the payload. The positions-poll path sets this
   *    false so a mid-session poll (where the operator may have unchecked rows)
   *    never overwrites a working checked state with an all-false map. The
   *    strategy-success path always includes selections so the full state is
   *    persisted after a confirmed working fetch. */
  function _saveCache(opts = { includeSelections: true }) {
    if (typeof sessionStorage === 'undefined') return;
    try {
      /** @type {Record<string, any>} */
      const payload = {
        ts: Date.now(),
        positions, strategy, drafts,
        selectedAccounts, selectedUnderlying, selectedExpiries,
        _includeHoldings,
      };
      if (opts.includeSelections !== false) {
        payload.enabledSymbols = enabledSymbols;
      }
      sessionStorage.setItem(_CACHE_KEY, JSON.stringify(payload));
    } catch (_) { /* quota / private mode — silent */ }
  }
  function _loadCache() {
    if (typeof sessionStorage === 'undefined') return false;
    try {
      const raw = sessionStorage.getItem(_CACHE_KEY);
      if (!raw) return false;
      const d = JSON.parse(raw);
      if (!d || (Date.now() - (d.ts || 0)) > _CACHE_MAX_AGE_MS) return false;
      // Restore data first, then selections — derived state (candidates,
      // legs) recomputes off the restored positions + drafts.
      if (Array.isArray(d.positions)) positions = d.positions;
      if (d.strategy) {
        strategy  = d.strategy;
        debugLog('payoff:strategy', 'loaded', {
          underlying: strategy?.underlying,
          anchor: strategy?.spot_anchor_contract,
          legs: strategy?.legs?.length ?? 0,
          spot: strategy?.spot,
        });
      }
      if (Array.isArray(d.drafts))     drafts    = d.drafts;
      if (Array.isArray(d.selectedAccounts)) selectedAccounts = d.selectedAccounts;
      // URL param (set by onMount #1 which runs before this onMount #2)
      // takes precedence over the sessionStorage snapshot. Only restore the
      // cached underlying when the URL did NOT already seed one.
      if (typeof d.selectedUnderlying === 'string' && !selectedUnderlying) selectedUnderlying = d.selectedUnderlying;
      if (Array.isArray(d.selectedExpiries))          selectedExpiries  = d.selectedExpiries;
      if (d.enabledSymbols && typeof d.enabledSymbols === 'object') {
        enabledSymbols = d.enabledSymbols;
      }
      if (typeof d._includeHoldings === 'boolean') {
        _includeHoldings = d._includeHoldings;
      }
      return true;
    } catch (_) { return false; }
  }

  // Auth transition watcher — when the operator signs in (demo →
  // admin) or out, the cached `positions` array carries account
  // identifiers from the previous session: masked (Z#####) for demo,
  // unmasked for signed-in admin. Without invalidation, the 30-s
  // poll eventually replaces them but the operator briefly sees the
  // wrong shape. On every user-id transition, drop the sessionStorage
  // cache + refire the loaders so the page reflects the active
  // identity immediately.
  let _lastAuthUserId = /** @type {string|null} */ (null);
  $effect(() => {
    const uid = String($authStore?.user?.id ?? $authStore?.user?.email ?? '');
    if (uid === _lastAuthUserId) return;
    const wasInitial = _lastAuthUserId === null;
    _lastAuthUserId = uid;
    if (wasInitial) return;            // first run — onMount handles the load
    untrack(() => {
      try {
        if (typeof sessionStorage !== 'undefined') {
          sessionStorage.removeItem(_CACHE_KEY);
        }
      } catch (_) { /* private mode / quota — ignore */ }
      // Wipe in-memory rows whose account values came from the
      // previous session. The fetches below repopulate from the
      // backend within one tick.
      positions = [];
      loadPositions({ fresh: true });
      loadRealAccounts();
    });
  });

  // book_changed bus — postback handler emits this on every terminal
  // order status. Refetch positions + strategy in lockstep so the
  // Snapshot grid + Legs + Payoff all settle on the same iteration
  // (operator's prior pain: "snapshot grid updated two iterations").
  // Bus is debounced 200ms upstream so a basket-order burst coalesces
  // into one refresh.
  //
  // Bridge the legacy `bookChanged` writable store through an explicit
  // subscribe → $state to avoid Svelte 5 runes interop stale-cache.
  // Reading `$bookChanged` directly inside `$effect` can miss updates
  // in runes-mode components (same root cause as the auth-badge
  // dual-render defect fixed via the bridge pattern). The explicit
  // subscribe fires synchronously on every increment and on initial
  // subscribe, so the effect below never races the mount lifecycle.
  let _bookChangedVal = $state(0);
  const _unsubBook = bookChanged.subscribe(n => { _bookChangedVal = n; });
  let _lastBookCounter = 0;
  $effect(() => {
    const n = _bookChangedVal;
    if (n <= _lastBookCounter) return;
    _lastBookCounter = n;
    // fresh=true bypasses the backend 30s TTL cache so the new
    // position appears in the underlying dropdown immediately rather
    // than waiting for the next cache expiry window.
    untrack(() => {
      loadPositions({ fresh: true });
      try { loadStrategy(); } catch (_) { /* strategy panel not always mounted */ }
    });
  });

  onMount(async () => {
    // Auth/redirect handled by the algo layout; demo visitors view
    // this page read-only.
    // Stale-while-revalidate: paint the previous session's view first
    // (positions populate the dropdowns, strategy renders the chart)
    // so the page never shows up empty on a tab reopen. Background
    // fetches below replace the snapshot once the broker responds.
    _loadCache();
    // Restore the "seen accounts" ledger so the auto-union effect knows
    // which broker accounts were already known last time the operator
    // was on this page. A fresh-arrival (e.g. Dhan loaded after the
    // operator's prior session) gets unioned into selectedAccounts on
    // first sighting; previously-known-and-untoggled accounts stay
    // unchanged. See `selectedAccounts` declaration block above for the
    // full rationale.
    try {
      if (typeof sessionStorage !== 'undefined') {
        const raw = sessionStorage.getItem('opt.seenAccounts');
        if (raw) {
          const parsed = JSON.parse(raw);
          if (Array.isArray(parsed)) _seenAccts = new Set(parsed.map(String));
        }
      }
    } catch (_) { _seenAccts = new Set(); }
    // PRIMARY — position book + instruments cache. The underlying-picker
    // dropdown and option-chain rendering both depend on these; nothing
    // useful paints until they land. loadPositions fires immediately
    // (no await) so it races in parallel with the instruments await below.
    // portfolioStore is the reactive SSOT and manages its own SWR TTL refresh.
    loadPositions();
    // Cold-start: seed NIFTY only when positions are not yet cached.
    // When positionsStore.value already has entries (e.g. page revisit with
    // a warm store), skip the NIFTY seed so the auto-select $effect can pick
    // the operator's actual position-derived underlying instead. After
    // loadPositions() resolves, the $effect at line 1429 re-fires (it watches
    // `positions` + `holdings` + `_rootsWithOptions`) and will overwrite the
    // NIFTY provisional seed with the correct tier-1/tier-2 entry. When the
    // store is cold, the $effect fires with an empty book and NIFTY is the
    // correct fallback — same end-state, no flash.
    if (!selectedUnderlying && !(positionsStore.value?.length) && !(pulsePositionsStore.value?.length)
        && !_pinnedWatchlistRoots.length && !_regularWatchlistRoots.length) {
      selectedUnderlying = POPULAR_UNDERLYINGS[0]; // 'NIFTY' provisional
    }
    // Load the instruments cache so the option-chain picker has data.
    // Already cached in IndexedDB after the first /console autocomplete
    // load — most operators will see this resolve from cache instantly.
    // `underlyingChoices` is a $derived that recomputes off
    // instrumentsReady + selectedUnderlying + positions, so flipping
    // the flag is enough to populate the chain picker's dropdown.
    try {
      await loadInstruments();
      instrumentsReady = true;
      loadUnderlyingQuotes(); // seed spot prices for _clientPayoffStub on cold start
    } catch (_) { /* instruments unreachable — chain picker hides */ }
    // SECONDARY — broker accounts, default watchlist, hedge-proxy table.
    // None of these gate the primary chain / payoff render:
    //   • loadRealAccounts is only needed when OrderTicket opens
    //   • loadDefaultWatchlist drives the +W button on chain rows
    //   • loadHedgeProxies overlays proxy legs on the payoff (decorative)
    // Defer one event-loop tick so the primary view paints before these
    // network requests fire. Pattern mirrors ChartWorkspace._loadGreeks
    // (Tier 1 reference).
    setTimeout(() => {
      // Real broker accounts — needed by the OrderTicket so the
      // operator can pick which Kite handle the order routes through.
      // Fetched separately from positions because /positions masks
      // the account field for non-admin signed-in users; /accounts
      // returns unmasked account_ids to any authenticated user.
      loadRealAccounts();
      // Resolve the user's default watchlist id once, so the chain rows
      // can drop a "+W" button. Demo / unauthenticated sessions just
      // leave defaultWatchlistId null and the button hides.
      loadDefaultWatchlist();
      // Load the proxy-hedge table once at mount. Failure leaves the
      // module cache empty — page degrades gracefully (no proxy legs)
      // rather than crashing. /admin/settings panel forces a reload
      // after mutations.
      loadHedgeProxies().then(() => { proxyTableReady = true; }).catch(() => {});
    }, 0);
    // Two separate cadences:
    //   - hot (5 s): analytics / strategy aggregate — Greeks + IV move
    //     intra-tick so the operator wants this fresh.
    //   - cold (30 s): the picker's position list — the broker book
    //     changes on the order of minutes; polling it every 5 s wasted
    //     a /api/positions/ + /api/simulator/status round-trip per tick
    //     for no operator-visible benefit.
    // Historical refreshes only on symbol change (daily candles don't
    // change intra-day).
    loadSimStatus();
    // Market-hours gated — outside NSE + MCX windows every poll
    // pauses. Sim status is included: when both segments are closed
    // AND no sim is running, there's nothing to refresh. Starting a
    // sim re-enters /admin/simulator (which polls on its own); the
    // status here picks up on the next mount or manual refresh.
    // Option B hybrid visibility: strategy/sim-status are analysis
    // pollers — pause on hidden. Positions + underlying-quotes are
    // critical data — throttle to 30 s on hidden so the operator
    // returns to current P&L without waiting for a cold-start cycle.
    teardown    = marketAwareInterval(loadStrategy, 5000);
    // Fix 7: timed positions poll removed — book poller (5 s) keeps
    // positionsStore fresh. Event-driven loadPositions({ fresh: true })
    // calls (fills, order updates, manual refresh) still fire as before.
    // Underlying quotes at 5s — same cadence as loadStrategy — so liveSpot
    // stays within 5s even when the underlying is not in the SSE ticker.
    quotesTeardown = visibleInterval(loadUnderlyingQuotes, 5000, 'throttle:30000');
    simTeardown    = marketAwareInterval(loadSimStatus, 30000, 30_000);

    // Real-time fill notifications — Kite postback fires a
    // `position_filled` ws event the moment an order completes.
    // Subscribe so the placement toast can transition to FILLED in-
    // place (or a fresh FILLED toast lands when the fill is from an
    // algo-engine path the operator didn't manually place here).
    // Refreshes loadPositions too so the Candidates panel reflects
    // the new fill within one ws round-trip, not on the 30 s poll.
    // Debounce handle for order_update bursts (basket fills, rapid postbacks).
    wsTeardown = createPerformanceSocket((msg) => {
      if (msg?.event === 'order_update') {
        const terminal = ['COMPLETE', 'REJECTED', 'CANCELLED'].includes(String(msg.status || '').toUpperCase());
        if (terminal) {
          // CANCELLED/REJECTED won't trigger position_filled, so we must
          // refresh positions directly so the candidates panel reconciles.
          const s = String(msg.status || '').toUpperCase();
          if (s === 'CANCELLED' || s === 'REJECTED') loadPositions({ fresh: true });
          return;
        }
        // Non-terminal: debounce to coalesce rapid postback bursts (basket fills).
        if (_orderUpdateTimer) clearTimeout(_orderUpdateTimer);
        _orderUpdateTimer = setTimeout(() => {
          _orderUpdateTimer = null;
          loadPositions({ fresh: true });
        }, 200);
        return;
      }
      if (msg?.event !== 'position_filled') return;
      const orderId = String(msg.order_id || '');
      const matched = orderId ? _markToastFilled(orderId, Number(msg.fill_price || 0)) : false;
      if (!matched) _pushFillToast(msg);
      // fresh=true: position_filled means the broker book just changed;
      // bypass the backend 30s TTL cache to surface the new position
      // in the underlying dropdown without waiting for cache expiry.
      loadPositions({ fresh: true });
      // Refresh the payoff chart immediately after the fill so the new
      // leg appears in the strategy without waiting for the 5s interval.
      try { loadStrategy(); } catch (_) {}
    });
  });
  onDestroy(() => {
    teardown?.(); wsTeardown?.(); quotesTeardown?.(); simTeardown?.();
    flash.dispose(); _unsubFlashPct(); _unsubBook?.(); _unsubDerivsOrder?.(); _unsubBrokerHealth?.();
    if (_orderUpdateTimer) { clearTimeout(_orderUpdateTimer); _orderUpdateTimer = null; }
    if (_urlSyncTimer) { clearTimeout(_urlSyncTimer); _urlSyncTimer = null; }
  });

  // Refresh underlying quotes whenever the Snapshot universe changes
  // (a new underlying lands in the book, an old one drops out, the
  // operator's account filter shrinks/grows the set, or a front-month
  // roll changes the quoteKey for an MCX commodity root).
  let _lastQuoteSig = '';
  /** @type {Set<string>} */
  let _prevQuoteRoots = new Set();
  $effect(() => {
    const keys = _underlyingQuoteKeys;
    const sig = keys.map(p => `${p.root}:${p.quoteKey}`).sort().join('|');
    if (sig === _lastQuoteSig) return;
    _lastQuoteSig = sig;
    // Prune roots this page stopped tracking (§5) — a previously-selected,
    // now-deselected root's stale entry can otherwise linger indefinitely
    // and be silently served by _activeQuoteLtp / _clientPayoffStub. Only
    // roots THIS page dropped are removed — pruneUnderlyingSpotRoots never
    // touches roots another page (e.g. PositionStrip) still tracks under a
    // different subscription cycle; any accidental cross-page removal is
    // self-healing on that page's own next poll.
    const nextRoots = new Set(keys.map(p => p.root));
    const dropped = [..._prevQuoteRoots].filter(r => !nextRoots.has(r));
    if (dropped.length) untrack(() => pruneUnderlyingSpotRoots(dropped));
    _prevQuoteRoots = nextRoots;
    if (sig) loadUnderlyingQuotes();
  });

  // ── Helpers ──────────────────────────────────────────────────────
  // Number formatters delegate to format.js — no ₹ prefix and no leading
  // `+` on positives anywhere (color carries direction; the rupee symbol
  // ate column width). `signed` retained as a flag for legacy callers
  // that wanted the +/- chip; both paths now omit the +.
  /** Money formatter for plain values — null reads as '—' (data
   *  unavailable). Use `fmtUnbounded` for max-profit / max-loss
   *  callsites where null carries the "unlimited payoff" semantic
   *  for long calls / short puts. Earlier this function returned
   *  '∞' on null and was reused for EV — uncomputed EV showed as
   *  '∞ EV' which was a false read of the data.
   */
  function fmtMoney(/** @type {number|null|undefined} */ v, /** @type {boolean} */ _signed = true) {
    if (v == null) return '—';
    return aggCompact(v);
  }
  /** Max-profit / max-loss formatter — null reads as '∞' because the
   *  backend nulls those fields on unbounded payoff legs. */
  function fmtUnbounded(/** @type {number|null|undefined} */ v, /** @type {boolean} */ _signed = true) {
    if (v == null) return '∞';
    return aggCompact(v);
  }

  /** Risk-of-ruin ratio (|max_loss| / |net_cost|). Hoisted out of an
   *  `{#if true}` wrapper in the template — that was needed to
   *  satisfy Svelte's "immediate-child-of-block" rule for `{@const}`.
   *  Cleaner as a script-level $derived. Null when the divisor is
   *  near zero or max_loss is unbounded; rendered as '—' in the UI. */
  const _ror = $derived.by(() => {
    if (!strategy) return null;
    const ml = _mergedRisk?.max_loss ?? strategy.risk?.max_loss;
    const nc = strategy.net_cost;
    if (ml == null || !Number.isFinite(ml)) return null;
    if (nc == null || Math.abs(nc) <= 1) return null;
    return Math.abs(ml) / Math.abs(nc);
  });
  /** R:R ratio recomputed from the merged risk (max_profit / |max_loss|).
   *  Falls back to the backend's strategy.risk.rr_ratio when no eq
   *  legs are layered. Returns null when either side is unbounded. */
  const _rrRatio = $derived.by(() => {
    if (!strategy) return null;
    const merged = _mergedRisk;
    if (!merged || merged === strategy.risk) return strategy.risk?.rr_ratio ?? null;
    const mp = merged.max_profit;
    const ml = merged.max_loss;
    if (mp == null || ml == null || !Number.isFinite(mp) || !Number.isFinite(ml)) return null;
    if (Math.abs(ml) < 1) return null;
    return mp / Math.abs(ml);
  });
  // Strategy R:R, intrinsic ratios, etc are returned by the API as
  // fractions (0.05 = 5%). Use the canonical *Fraction variant.
  const fmtPct = fmtPctFraction;
  function fmtNum(/** @type {number|null|undefined} */ v, /** @type {number} */ dp = 4) {
    if (v == null) return '—';
    return v.toFixed(dp);
  }

  // ── Payoff overlay snapshot (B3 + C3 combined fix) ──────────────────
  // Every strategy-derived value the OptionsPayoff overlay renders is
  // bundled into ONE object here, so the whole overlay switches
  // atomically — previously only `payoff`/`intermediateCurves` were
  // gated on `_strategyStale`; breakevens/spanSigmas/spanPct/dte/
  // ivProxy/legCount/legSymbols/spotAnchor/realizedPnl/expiryPnlOffset/
  // legsExpPnlAtSpot/netCost/multiExpiry all kept reading the OLD
  // `strategy` during the stale window right after an underlying
  // switch, while `spot`/`prevClose` (payoffSpot/payoffPrevClose) had
  // ALREADY switched to the new root — briefly showing the new
  // symbol's LTP next to the old symbol's DTE/σ/legs.
  //
  // Combined with a last-good-PER-ROOT cache (B3): a routine 5s
  // refetch (strategy reassigned to a new object, same underlying)
  // never touches this — `_strategyStale` is false the whole time, so
  // `payoffDisplay` below just returns the live, freshly-computed
  // snapshot every cycle. Only a genuine underlying switch enters the
  // stale window; while stale, the cache is keyed by `selectedUnderlying`
  // (the NEW root, not the old one) — so the LAST snapshot successfully
  // computed for THIS SAME (new) root, if one was ever cached earlier
  // this session, is shown instead of blanking to the client stub. A root visited
  // for the very first time this session (no cache entry yet) is the
  // ONLY case that falls through to `_clientPayoffStub`/the "Resolving
  // spot…" placeholder — a genuine cold start, not a routine refetch.
  const _payoffSnapshot = $derived.by(() => ({
    payoff:             _mergedPayoff,
    breakevens:         _mergedRisk?.breakevens ?? strategy?.risk?.breakevens,
    intermediateCurves: strategy?.intermediate_curves || [],
    spanSigmas:         strategy?.span_sigmas,
    spanPct:            strategy?.span_pct,
    dte:                strategy?.days_to_expiry,
    ivProxy:            strategy?.iv_proxy,
    legCount:           (strategy?.legs?.length ?? 0) + _equityLegs.length,
    multiExpiry:        strategy?.multi_expiry ?? false,
    realizedPnl:        chartPnlOffset,
    expiryPnlOffset:    _expiryPnlOffset,
    legsExpPnlAtSpot:   _chartExpPnlAtSpot,
    legSymbols:         (strategy?.legs ?? []).map(/** @param {{symbol:string}} l */ l => l.symbol),
    spotAnchor:         strategy?.spot_anchor_contract
      ? { contract: strategy.spot_anchor_contract,
          source: strategy.spot_source || 'futures',
          expiryISO: _spotAnchorExpiryISO }
      : null,
    netCost:            _netStrategyCost,
  }));

  /** @type {Record<string, any>} */
  let _payoffSnapshotByRoot = $state({});
  // Writes the cache ONLY while `strategy` is genuinely current for
  // `selectedUnderlying` (not stale) — a write during the stale window
  // would cache the OLD root's data under the NEW root's key.
  $effect(() => {
    const snap = _payoffSnapshot;
    const root = selectedUnderlying;
    const isCurrent = !!strategy && !_strategyStale;
    untrack(() => {
      if (isCurrent && root) {
        _payoffSnapshotByRoot = { ..._payoffSnapshotByRoot, [root]: snap };
      }
    });
  });

  /** Atomic overlay data source. Live + current → the fresh snapshot.
   *  Stale (mid underlying-switch) but this root has rendered before
   *  this session → its last-good snapshot, frozen until the new fetch
   *  lands. Never rendered before for this root → null, the ONLY case
   *  that falls through to the client-side stub / cold-start placeholder. */
  const payoffDisplay = $derived.by(() => {
    if (strategy && !_strategyStale) return _payoffSnapshot;
    return _payoffSnapshotByRoot[selectedUnderlying] ?? null;
  });

</script>

<svelte:head><title>Derivatives | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Derivatives</h1>
    {#if simActive}
      <span class="opt-mode-pill opt-mode-sim" title="A simulator run is active. Candidates and analytics are sourced from the sim book.">SIMULATOR</span>
    {/if}
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={_refreshAll}
                   loading={_refreshing} label="derivatives" />
    <PageHeaderActions symbol={selectedUnderlying} />
  </span>
</div>

{#if positionsLoadErr && _brokerWorstState === 'green'}
  <div class="pos-stale-bar" title={positionsLoadErr}>
    <span class="pos-stale-dot" aria-hidden="true"></span>
    <span class="pos-stale-text">Positions delayed</span>
  </div>
{/if}

<!-- Picker bar — two dropdowns + a "+" toggle for the option-chain
     picker. Strategy auto-recomputes whenever the leg set changes;
     no Analyze button needed. -->
<!-- Picker bar — no card wrapper. Account + Underlying + Expiry sit
     directly on the page so they read as inline page-level
     controls rather than as content inside a panel. Both Account
     and Expiry are filters on the Candidates panel; empty = all. -->
<div class="opt-picker mb-3">
  <div class="opt-field opt-field-grow">
    <label class="field-label" for="opt-acct" title="Filter the Candidates / Legs panel and the Snapshot rollup to positions held in these accounts. Empty = all accounts.">Account</label>
    <!-- Plain MultiSelect (not AccountMultiSelect, which forces
         singleSelect=true). Operator: "account dropdown should support
         multi account select like expiry." -->
    <MultiSelect id="opt-acct"
      bind:value={selectedAccounts}
      options={accountChoices.map(a => ({ value: a, label: a }))}
      placeholder={accountChoices.length ? 'All accounts' : 'No accounts loaded'} />
  </div>
  <div class="opt-field opt-field-grow">
    <label class="field-label" for="opt-und">Underlying</label>
    <div class="opt-und-row">
      <Select id="opt-und"
        bind:value={selectedUnderlying}
        options={underlyingOptionsForPicker}
        placeholder={'Pick underlying…'} />
    </div>
    {#if _positionsLoaded && !underlyingChoicesFromBook.length && !holdings.length}
      <div class="opt-und-hint">
        No F&O positions — showing popular.
      </div>
    {/if}
  </div>
  <div class="opt-field">
    <label class="field-label" for="opt-exp">Expiry</label>
    <MultiSelect id="opt-exp"
      bind:value={selectedExpiries}
      options={expiryChoicesForUnderlying.map(x => ({ value: x, label: x }))}
      placeholder={expiryChoicesForUnderlying.length ? 'All expiries' : '—'} />
  </div>
</div>

{#if positionsLoadErr && _brokerWorstState !== 'green'}
  <!-- Full banner only on a genuine outage (broker health is not green).
       When broker is green, snapshot lag shows the inline chip instead.
       Short banner per ops convention (≤35 chars). Full error detail
       logged to console by api.js. -->
  <div class="mb-3 p-2 rounded bg-[var(--c-short-10)] text-[var(--c-short)] text-[0.65rem] border border-[rgba(248,113,113,0.40)]"
       title={positionsLoadErr}>
    Positions feed unavailable — candidates may be stale.
  </div>
{/if}
{#if strategyErr}
  <div class="pos-stale-bar" title={strategyErr}>
    <span class="pos-stale-dot pos-stale-dot-red" aria-hidden="true"></span>
    <span class="pos-stale-text">Strategy analytics unavailable — retry shortly.</span>
  </div>
{/if}

{#if !strategy && !strategyErr && !loading && !_allDrafts.length}
  <div class="text-[0.65rem] text-[var(--c-muted)] italic mb-3">
    {#if !selectedUnderlying}
      Pick an underlying to surface {simActive ? 'sim' : 'live'} candidates, or click
      <b>+ Add</b> to drop a draft strike into the payoff.
    {:else if candidatePositions.length === 0}
      No {simActive ? 'sim' : 'live'} positions on <b>{selectedUnderlying}</b> and no drafts yet.
      Click <b>+ Add</b> to drop a draft strike on this underlying.
    {:else if _allEnabledLegsZeroQty}
      All {simActive ? 'sim' : 'live'} positions on <b>{selectedUnderlying}</b> are closed — no open payoff to render.
      Click <b>+ Add</b> to model a hypothetical strike.
    {:else if _legsAreEqOnly}
      Only equity holdings for <b>{selectedUnderlying}</b> — no F&amp;O legs in book.
      Enable <b>Include Holdings</b> above to plot the linear stock payoff, or click
      <b>+ Add</b> to drop a derivative strike.
    {:else}
      No legs selected. Tick at least one row in the Candidates panel
      below to render the payoff.
    {/if}
  </div>
{/if}

<!-- Drafts editor — operator-typed hypothetical positions. Each row has
     editable symbol/qty/avg_cost/ltp + a delete. Drafts whose symbol
     matches the selected underlying also appear (read-only) in the
     Candidates panel below so they can be picked / checked alongside
     live + sim positions. -->
{#if drafts.length}
  <div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
    <div class="opt-section-h" style="padding-bottom: 0.5rem;">
      Drafts <span class="opt-section-meta">({drafts.length}) — hypothetical positions; appear in Candidates when their symbol matches the underlying</span>
    </div>
    <div class="leg-grid">
      <div class="leg-headrow">
        <span>Symbol</span>
        <span>Qty</span>
        <span>Avg cost</span>
        <span>LTP</span>
        <span>Source</span>
        <button class="leg-pair-btn" onclick={() => _legPairModalOpen = true}
          disabled={!_pairEnabled}
          title={_pairEnabled ? 'Pair selected positions' : 'Select 2 positions from the same account to pair'}>⟷ Pair</button>
      </div>
      {#each drafts as _d, i (drafts[i].id)}
        <div class="leg-row">
          <input type="text" class="field-input"
            placeholder="NIFTY25APR22000CE"
            bind:value={drafts[i].symbol} />
          <input type="number" class="field-input"
            placeholder="±qty"
            bind:value={drafts[i].qty} />
          <input type="number" class="field-input"
            placeholder="₹"
            step="0.05"
            bind:value={drafts[i].avg_cost} />
          <input type="number" class="field-input"
            placeholder="₹ (auto from broker)"
            step="0.05"
            bind:value={drafts[i].ltp} />
          <span class="leg-source leg-source-draft">draft</span>
          <button type="button" class="leg-del"
                  title="Remove this draft"
                  onclick={() => removeDraft(drafts[i].id)}>×</button>
        </div>
      {/each}
    </div>
  </div>
{/if}

<!-- ───── Option-chain picker — opens via "+ Add" button ───────────────
     Browse strikes for the chosen underlying and click +CE / +PE / a
     futures pill to drop a draft into the Drafts panel. Drafts that
     match the page's selected underlying then auto-show in Candidates,
     re-running the strategy analytics with the new leg included. -->
<!-- In-page Option-chain panel removed — the canonical chain UI is
     the SymbolPanel modal opened by the Chain button. Earlier
     this file rendered a second chain panel inline so the operator
     saw two chains simultaneously (one with +W watchlist buttons,
     one without). Single source of truth now lives in OptionChainTab. -->

<div class="opt-payoff-legs-row">
<!-- Payoff card visibility is driven off `selectedUnderlying`, not
     strategy / stub existence. The card mounts as soon as the picker
     has a symbol pinned (POPULAR_UNDERLYINGS[0]='NIFTY' on cold start)
     so operators never see a blank workspace while positions +
     instruments + strategy resolve. When strategy is null and the
     client stub can't produce a curve (cold-start spot=0, no ticks
     yet), we pass an empty array and OptionsPayoff renders its
     "Pick legs to see payoff." placeholder inside the card frame. -->
{#if selectedUnderlying}
  <div class="opt-payoff opt-payoff-full algo-status-card cmd-surface p-3"
    class:fs-card-on={_fsPayoff}
    class:is-collapsed={_colPayoff}>
    <!-- Single-row header — title + EV + Greeks chips, plus the global
         collapse + fullscreen toggles. Net Dr/Cr / MAX P / MAX L chips
         removed 2026-07-01 per operator ("on payoff line show ev and
         greeks. remove other chips"); the same numbers are still available
         in the Aggregate / Risk kv-block below the chart. SPOT / TDAY /
         EXP / DTE / σ / LEGS live in the on-chart stat overlay. -->
    <CardHeader
      title="Payoff"
      loading={loading}
      bind:isCollapsed={_colPayoff}
      bind:isFullscreen={_fsPayoff}
      cardId="optPayoff"
      label="Payoff"
      onRefresh={_refreshAll}
      bind:refreshLoading={_refreshing}
      showSearch={false}
      detectOverflow={false}
    >
      {#snippet middle()}
        <div class="opt-section-chips">
          <!-- Expected value chip — probability-weighted average payoff
               under the lognormal distribution. Positive = positive
               expectancy; negative = lose money on average. Companion
               to POP for assessing trade quality (POP alone is
               misleading on asymmetric clip sizes). -->
          <span class="opt-section-tag tf-cell {(_mergedEv ?? 0) >= 0 ? 'tag-long' : 'tag-short'}"
                title="Expected value — probability-weighted average payoff at expiry. ev_pct = EV / |entry cost|.">
            EV {fmtUnbounded(_mergedEv, false)}{_mergedEvPct != null ? ` (${pctFmt(_mergedEvPct)})` : ''}
          </span>
          <!-- Greeks chips — full Δ Γ Θ 𝒱 ρ surfaced inline in the payoff
               header so the operator sees position-level direction /
               convexity / decay / volatility / rate exposure without
               scrolling to the Greeks card below. Same `.opt-section-tag`
               chip chrome; greek-tagged variant uses a sky-cyan tint to
               read as distinct from the amber Net / Max chips. Sign-tinted
               variants on theta / vega / rho since those flip sign with
               credit vs debit structures. -->
          <!-- Null-safe access — Svelte 5 can evaluate template expressions
               inside `{#if strategy}` blocks in the same reactive flush as
               the guard's own update. When `strategy` transitions to null
               (during loadStrategy underlying-change), the inner `.delta`
               etc. access threw TypeError, poisoning the scheduler and
               freezing all downstream $state writes (instrumentsReady,
               etc.) — causing the page-wide hang. `?.` throughout so the
               expression returns undefined instead of throwing; pctFmt
               renders '—' for undefined. -->
          <span class="opt-section-tag tf-cell tag-greek"
            title="Delta — net directional exposure (₹ per ₹1 spot move). Includes +qty for enabled equity-holding legs.">
            Δ {pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.delta)}
          </span>
          <span class="opt-section-tag tf-cell tag-greek"
            title="Gamma — convexity, rate of change of Δ as spot moves">
            Γ {pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.gamma)}
          </span>
          <span class="opt-section-tag tf-cell tag-greek {((_mergedGreeks ?? strategy?.aggregate_greeks)?.theta ?? 0) < 0 ? 'tag-greek-neg' : ''}"
            title="Theta — daily decay (₹/day, positive when net short premium)">
            Θ {pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.theta)}
          </span>
          <span class="opt-section-tag tf-cell tag-greek {((_mergedGreeks ?? strategy?.aggregate_greeks)?.vega ?? 0) < 0 ? 'tag-greek-neg' : ''}"
            title="Vega — P&L per 1% IV move (positive = long volatility)">
            𝒱 {pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.vega)}
          </span>
          <span class="opt-section-tag tf-cell tag-greek {((_mergedGreeks ?? strategy?.aggregate_greeks)?.rho ?? 0) < 0 ? 'tag-greek-neg' : ''}"
            title="Rho — P&L per 1% interest-rate move (typically small for short-DTE)">
            ρ {pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.rho)}
          </span>
        </div>
      {/snippet}
    </CardHeader>
    <!-- Body wrapped in [hidden] (not {#if}) so the SVG chart stays
         mounted across collapse cycles — same pattern as dashboard
         cards (avoids re-mounting + state loss). -->
    <!--
      When the backend says spot_source='fallback' (e.g. CRUDEOIL
      future not found in any broker's instruments cache → strike-
      as-degenerate-spot), suppress the value entirely so the chart
      doesn't briefly show a wrong number (150000 for GOLDM, 9000
      for CRUDEOIL) before correcting on the next poll. Force
      loading=true instead — operator sees an explicit loading
      state, not a misleading number.
    -->
    <div class="card-body" hidden={_colPayoff}>
      <OptionsPayoff
        payoff={payoffDisplay ? payoffDisplay.payoff : (_clientPayoffStub ?? [])}
        spot={payoffSpot}
        prevClose={payoffPrevClose}
        breakevens={payoffDisplay?.breakevens}
        intermediateCurves={payoffDisplay?.intermediateCurves ?? []}
        spanSigmas={payoffDisplay?.spanSigmas}
        spanPct={payoffDisplay?.spanPct}
        dte={payoffDisplay?.dte}
        ivProxy={payoffDisplay?.ivProxy}
        legCount={payoffDisplay ? payoffDisplay.legCount : _equityLegs.length}
        multiExpiry={payoffDisplay?.multiExpiry ?? false}
        realizedPnl={payoffDisplay?.realizedPnl ?? 0}
        expiryPnlOffset={payoffDisplay?.expiryPnlOffset ?? 0}
        dayPnl={candidatesDayPnl}
        legsExpPnlAtSpot={payoffDisplay?.legsExpPnlAtSpot}
        legSymbols={payoffDisplay?.legSymbols ?? []}
        spotAnchor={payoffDisplay?.spotAnchor ?? null}
        includeHoldings={_includeHoldings}
        onToggleHoldings={_flipHoldings}
        showDraftInPayoff={showDraftInPayoff}
        onToggleDraft={_flipDraft}
        netCost={payoffDisplay?.netCost}
        loading={!payoffDisplay && (loading || _strategyStale)}
        refreshing={_stratRefreshing}
        legSignature={_legSignature}
        height={320} />
    </div>
  </div>
{/if}

<!-- Candidates — sits between the payoff chart above and the
     Aggregate / Greeks / Risk cards below. Reading order: see the
     chart → see which legs feed it → see the maths beneath. Each
     row carries position info (qty / cost / LTP / P&L) plus per-leg
     analytics (IV / Δ / Θ / 𝒱) joined from the latest strategy
     response by symbol. Horizontal + vertical overflow scrolling. -->
<!-- Card always mounted — empty-state placeholder lives inside.
     Operator: "The cards should always exist. The grids should have
     refreshed data in the cards without destroying the cards and
     grids." -->
  <div class="algo-status-card cmd-surface p-3 opt-legs-card"
    data-status="inactive"
    class:fs-card-on={_fsLegs}
    class:is-collapsed={_colLegs}>
    <!-- Legs header — non-clickable title block on the left; the
         global CollapseButton on the right is the sole collapse
         control. (Earlier the row carried a redundant left-side
         chevron button — duplicate toggle with the same effect as
         CollapseButton, removed for visual + behavioural parity
         with every other card on the page.) -->
    <div class="legs-header-row">
      <div class="legs-header legs-header-static">
        {#if selectedUnderlying}
          <span class="legs-underlying-chip">{selectedUnderlying}</span>
          <span class="legs-header-sep" aria-hidden="true"></span>
        {/if}
        <!-- Tabs replace the static title — operator flips between
             the full leg list and the close-list summary. Expiry
             tab carries its count in a rose-colored badge variant
             so an unfilled close-before-expiry surface flags as
             warning vs the neutral leg count on Legs. -->
        <AlgoTabs
          tabs={[
            { id: 'legs', label: 'Legs',
              badge: candidatePositions.length > 0 ? candidatePositions.length : null },
            { id: 'expiry', label: 'Exp close',
              /* Operator (2026-07-01): "active tab text color must be
                 consistent". Warning cue for unfilled expiry-close
                 total moves to the badge count only — the tab itself
                 always renders in canonical amber. */
              color: /** @type {const} */ ('amber'),
              badge: expiryCloseTotal > 0 ? expiryCloseTotal : null },
          ]}
          value={legsTab}
          onChange={(id) => { legsTab = /** @type {'legs'|'expiry'} */ (id); }}
          compact={true}
        />
      </div>
      <!-- Same tight no-wrap trio cluster as the Payoff header — see
           the comment over `.payoff-card-controls`. Reuses the same
           class so the CSS rule covers both cards. -->
      <span class="payoff-card-controls">
        <CardControls
          bind:isCollapsed={_colLegs}
          bind:isFullscreen={_fsLegs}
          bind:filter={_filterLegs}
          cardId="optLegs"
          label="Legs"
          onRefresh={_refreshAll}
          bind:refreshLoading={_refreshing}
          onDownload={() => exportRowsToCsv(
            displayedCandidates,
            [
              { header: 'Symbol',    key: 'symbol' },
              { header: 'Exchange',  key: 'exchange' },
              { header: 'Account',   key: 'account' },
              { header: 'Qty',       key: 'qty' },
              { header: 'Avg',       key: 'avg_cost',        format: (v) => v == null ? '' : String(v) },
              { header: 'LTP',       key: 'ltp',             format: (v) => v == null ? '' : String(v) },
              { header: 'Day P&L',   key: 'day_change_val',  format: (v) => v == null ? '' : String(v) },
              { header: 'P&L',       key: 'pnl',             format: (v) => v == null ? '' : String(v) },
            ],
            'legs.csv'
          )}
        />
      </span>
    </div>
    {#if !_colLegs}
      {#if hiddenByAccount.rows > 0}
        <div class="cand-hidden-hint" title="Picker scopes legs to the chosen accounts. Clear the Account filter to include these rows in the payoff.">
          Hiding {hiddenByAccount.rows} position{hiddenByAccount.rows === 1 ? '' : 's'} on {hiddenByAccount.accts} other account{hiddenByAccount.accts === 1 ? '' : 's'}
        </div>
      {/if}
      <!-- Draft/Provisional payoff toggle — shown when at least one
           ~ or D row is present in the displayed candidates, OR when
           payoffDrafts has any entries (added via OrderTicket). Amber
           tint matches the "non-confirmed" palette of those chips. -->
      {#if candidatePositions.some(c => c._provisional || c._draft_store || c.source === 'draft')}
        <div class="cand-draft-payoff-bar">
          <label class="cand-draft-payoff-toggle" title="Include provisional (~), draft-store (D), and payoff draft legs in the payoff curve and analytics. When off, they appear in the legs list but are excluded from the chart.">
            <input type="checkbox" bind:checked={showDraftInPayoff} />
            <span class="cand-draft-payoff-label">Incl. draft/prov in payoff</span>
          </label>
        </div>
      {/if}
      <div class="cand-scroll algo-grid-chrome fs-content-fill" style="--fs-chrome-h: 4.5rem;">
        <div class="cand-grid">
          <!-- Header row checkbox = master toggle. Checked when
               EVERY candidate is on; unchecked when none; the
               middle "indeterminate" state is rendered via the JS
               input.indeterminate property when some-but-not-all
               are on. Click flips every row to the opposite of the
               current "all-on" state (operator: easier than
               clicking 12 checkboxes when starting from a fresh
               page). Stops click propagation so the surrounding
               grid-row toggle handlers don't double-fire. -->
          <div class="cand-headrow">
            <input type="checkbox"
                   class="cand-check cand-check-master"
                   aria-label="Toggle all positions"
                   title={allCandidatesOn
                     ? 'Uncheck all positions'
                     : 'Check all positions'}
                   checked={allCandidatesOn}
                   bind:this={allCandidatesEl}
                   onclick={(e) => { e.stopPropagation(); toggleAllCandidates(); }} />
            <!-- State column header — matches second grid-template-columns track (28px). -->
            <span title="Position state: GTT (green) / Paired (cyan) / Orphan (amber)">St</span>
            <!-- Symbol header — hyphenated form below carries the
                 expiry month inline (e.g. NIFTY-26JUN-22000-CE) so a
                 separate Expiry column would be redundant. -->
            <span>Symbol</span>
            <span class="num">LTP</span>
            <span class="num cand-chg-sep-hdr">Chg %</span>
            <span class="num"
                  title="Qty in F&O lot units. Option / futures positions use the contract's own lot; other rows show 0.">Lots</span>
            <span class="num">Qty</span>
            <span class="num"
                  title="Quantity carried overnight from yesterday's session close.">O/N Qty</span>
            <span class="num">Avg</span>
            <span class="num">P.Close</span>
            <span class="num"
                  title="Today's change in P&L (broker-agnostic split formula). Sum across all rows = strip's P∆ chip.">
              Day P&amp;L
            </span>
            <span class="num"
                  title="Cumulative P&L on the position (lifetime, broker-reported). Sum across all rows = strip's P chip.">
              P&amp;L
            </span>
            <span class="num"
                  title="P&L if every contract expired RIGHT NOW at the current underlying spot — intrinsic value minus cost basis. Futures + equity track spot 1:1, so this matches their P&L. Options strip out time value and show only intrinsic settlement.">
              Exp P&amp;L
            </span>
            <span class="num"
                  title="Extrinsic value in P&L terms — Exp P&amp;L minus (ltp−avg)×qty. Positive = time value still in the option premium.">
              Extrinsic
            </span>
            <span>Acct</span>
            <span class="num">IV</span>
            <span class="num">Δ</span>
            <span class="num">Γ</span>
            <span class="num">Θ</span>
            <span class="num">𝒱</span>
            <span class="num"
                  title="Expected value — probability-weighted average payoff. Per-leg EV requires backend support and shows '—' today; the TOTAL footer carries the strategy-level EV.">
              EV
            </span>
          </div>
          {#each displayedCandidates as c, _ci (c.source + '|' + c.account + '|' + c.symbol + '|' + (c._splitTag ?? _ci) + '|' + (c._pairId ?? '') + '|' + (c._band ?? '') + '|' + (c.draftId != null ? c.draftId : _ci))}
            <CandidateLegRow
              {c}
              ci={_ci}
              stripe={_ci % 2 === 0 ? 'row-tint-odd' : 'row-tint-even'}
              prevBand={displayedCandidates[_ci - 1]?._band ?? null}
              bandCount={displayedCandidates.filter(r => r._band === c._band && r._segment === c._segment).length}
              {legsTab}
              legAnalytics={legAnalyticsBySymbol[c.symbol]}
              pendingQty={$openOrderQtyBySymbol[c.symbol] ?? 0}
              enabled={_isLegEnabled(c)}
              dayPnl={_candDayPnl(c)}
              expPnl={_legExpPnlDisplay(c, liveSpot ?? null)}
              extrinsic={legExtrinsicDisplay(c, Number(c?.underlying_ltp) || 0)}
              legExpired={_isLegExpired(c)}
              {strategy}
              {flash}
              onToggleEnabled={(candidate, checked) => {
                const next = { ...enabledSymbols };
                next[enKey(candidate)] = checked;
                enabledSymbols = next;
                if (candidate.kind === 'eq') _persistEqMemory(candidate, checked);
              }}
              onExecuteDraft={executeDraft}
              onClosePosition={closePosition}
              onRemoveDraft={removeDraft}
              onOpenChartTicket={(candidate) => openTicket({
                symbol:    candidate.symbol,
                exchange:  'NFO',
                defaultTab:'chart',
                accounts:  ticketAccounts,
                account:   _rowTicketAccount(candidate),
              })}
              onContextMenu={(candidate, ev) => {
                _ctxMenu = { symbol: candidate.symbol, exchange: candidate.exchange || 'NFO', x: ev.clientX, y: ev.clientY };
              }}
            />
          {/each}
          {#if displayedCandidates.length === 0}
            <!-- Empty-state row — keeps the scroll wrapper + header
                 mounted across symbol switches so the card doesn't
                 visually rebuild when the operator picks a new
                 underlying. Spans the whole grid via cand-empty. -->
            <div class="cand-empty" class:cand-loading={loading || !selectedUnderlying}>
              {#if !selectedUnderlying}
                <EmptyState
                  title={instrumentsReady ? 'No underlying selected' : 'Loading underlyings…'}
                  hint={instrumentsReady ? 'Pick an underlying to surface candidates.' : 'Instruments cache is warming up.'}
                  icon="search"
                />
              {:else if loading}
                <EmptyState message="Loading candidates…" />
              {:else}
                <EmptyState
                  title="No candidates"
                  hint="No candidates for {selectedUnderlying} on the current filter."
                  icon="inbox"
                />
              {/if}
            </div>
          {/if}
          {#if displayedCandidates.length > 0}
            <!-- TOTAL row — always the last row of the grid. Sums
                 pnl + day_change_val across the CHECKED candidates only
                 so the operator sees totals that track their selection.
                 Operator: "the legs totals should match the selection."
                 Earlier the totals walked every displayed row, which
                 conflicted with the eq-leg default-OFF + per-row
                 checkbox semantics: unchecking BHEL eq still saw it
                 contributing to the TOTAL while the payoff curve had
                 already dropped it. _isLegEnabled is the same gate
                 _mergedPayoff / _mergedGreeks already use, so the
                 TOTAL row now reconciles cell-by-cell with the chart. -->
            <!-- P&L TOTAL reads _legsTotalsBase (unaffected by the Legs
                 search box — item-7 fix), same pattern as _legsDayPnlTotal /
                 _legsExpPnlTotal above. Previously this read the
                 search-filtered `displayedCandidates`, so typing in the
                 search box moved P&L TOTAL while Day/Exp TOTAL stayed
                 fixed, and P&L TOTAL no longer equalled the sum of the
                 rows actually visible in the grid either. -->
            {@const _totalPnl = _legsTotalsBase.filter(c => _isLegEnabled(c)).reduce((s, c) => s + Number(c.pnl ?? 0), 0)}
            {@const _tg = _mergedGreeks ?? strategy?.aggregate_greeks ?? { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 }}
            <div class="cand-row cand-row-total">
              <span></span>
              <span></span>
              <span class="cand-total-label">TOTAL</span>
              <span class="num"></span>
              <span class="num"></span><!-- chg % -->
              <span class="num"></span>
              <span class="num"></span><!-- qty -->
              <span class="num"></span><!-- O/N qty -->
              <span class="num"></span>
              <span class="num"></span><!-- P.Close — was missing, caused 1-column offset -->
              <span class="num tf-cell cand-pnl {_legsDayPnlTotal > 0 ? 'cell-pos' : _legsDayPnlTotal < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ Day P&L across enabled F&O legs (excludes equity)">
                {aggCompact(_legsDayPnlTotal)}
              </span>
              <span class="num tf-cell cand-pnl {_totalPnl > 0 ? 'cell-pos' : _totalPnl < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ P&L across every enabled leg (unaffected by the search box) = strip's P chip for these accounts">
                {aggCompact(_totalPnl)}
              </span>
              <!-- This is the Legs-tab TOTAL row — expiry-picker-filtered,
                   what-if aware (leg checkboxes / drafts), scoped to
                   selectedUnderlying only. The Snapshot grid's row for this
                   same underlying does NOT read this value (item-3 fix,
                   round 4) — every Snapshot row, selected or not, uses the
                   account/strategy-filtered _filteredExpPnlByRoot instead,
                   so Snapshot stays internally consistent with its own
                   Day P&L/P&L columns regardless of which root happens to
                   be selected. This TOTAL and the Snapshot row for the same
                   root can legitimately differ whenever the expiry picker,
                   an unchecked leg, or a draft leg puts them out of scope
                   with each other — see _rowExpPnlFor's docstring. -->
              <span class="num tf-cell cand-pnl {_legsExpPnlTotal > 0 ? 'cell-pos' : _legsExpPnlTotal < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ Exp P&L across every selected leg — strategy expiry-day P&L at current spot.">
                {aggCompact(_legsExpPnlTotal)}
              </span>
              <span class="num tf-cell cand-pnl {_legsExtrinsicTotal > 0 ? 'cell-pos' : _legsExtrinsicTotal < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ Extrinsic value across every selected leg — time value remaining, same scope as Exp P&L above.">
                {aggCompact(_legsExtrinsicTotal)}
              </span>
              <span class="num"></span>
              <span class="num"></span>
              <span class="num" title="Σ Δ across every selected leg (position-scaled).">{pctFmt(_tg.delta)}</span>
              <span class="num" title="Σ Γ across every selected leg (position-scaled).">{pctFmt(_tg.gamma)}</span>
              <span class="num {_tg.theta < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ Θ across every selected leg (position-scaled). Negative = decay eating value each day.">
                {aggCompact(_tg.theta)}
              </span>
              <span class="num" title="Σ 𝒱 across every selected leg (position-scaled).">{aggCompact(_tg.vega)}</span>
              <span class="num {(_mergedEv ?? 0) > 0 ? 'cell-pos' : (_mergedEv ?? 0) < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Strategy-level EV across every selected leg.">
                {_mergedEv != null ? aggCompact(_mergedEv) : ''}
              </span>
            </div>
          {/if}
        </div>
      </div>
    {:else if !_colLegs}
      <div class="text-[0.6rem] text-[var(--c-muted)] italic">
        {#if legsTab === 'expiry'}
          No ITM options in the current candidate set.
        {:else}
          No options or futures on <b>{selectedUnderlying}</b> in
          {selectedAccounts.length ? 'the chosen accounts' : 'any account'}.
          Try a different underlying / account, or click <b>+</b> to drop a
          draft strike into the payoff.
        {/if}
      </div>
    {/if}
  </div>
</div>

<!-- Per-underlying snapshot — totals rows grouped by parsed root, with
     P&L + Day P&L shown side-by-side for "with hold" (eq legs
     included) and "without hold" (F&O only). Lets the operator see
     at a glance whether an equity holding is carrying or dragging
     the F&O book on each underlying. Always mounted (no outer gate)
     so the chrome stays stable across symbol switches. -->
<div class="algo-status-card cmd-surface p-3 opt-byund-card mb-3"
  data-status="inactive"
  class:fs-card-on={_fsByund}
  class:is-collapsed={_colByund}>
  <CardHeader
    title="Snapshot"
    bind:isCollapsed={_colByund}
    bind:isFullscreen={_fsByund}
    bind:filter={_filterByund}
    cardId="optByund"
    label="Snapshot"
    onRefresh={_refreshAll}
    bind:refreshLoading={_refreshing}
    detectOverflow={false}
    onDownload={() => {
      const rows = _byUnderlyingTotals.map(g => {
        // Same LTP/Chg%/Close source as the on-screen row (_undLive first,
        // _underlyingQuotes fallback) — CSV export must match the display,
        // not read a different (batch-cache-only) source (§6).
        const _q    = _underlyingQuotes[g.underlying];
        const _live = _undLive[g.underlying];
        const _ltp   = _live ? _live.ltp   : (_q ? Number(_q.ltp) : null);
        const _close = _live ? _live.close : (_q ? Number(_q.prev_close) : null);
        const _pct   = _live
          ? (_close != null && _close > 0 ? ((_ltp - _close) / _close) * 100 : null)
          : (_q?.day_pct ?? null);
        // Day P&L / lifetime P&L already filtered by matchAccount/matchStrategy
        // on the _byUnderlyingTotals row; Exp P&L via _rowExpPnlFor — the
        // SAME function the on-screen row calls, so the export can never
        // diverge from the display (§6: CSV must match on-screen, same
        // pattern already applied to LTP/Chg%/Close).
        const dayVal  = g.day_without ?? 0;
        const pnlVal  = g.pnl_without ?? 0;
        const expVal  = _rowExpPnlFor(g.underlying);
        return {
          underlying:  g.underlying,
          spot:        _ltp != null ? _ltp : '',
          day_pct:     _pct != null ? _pct : '',
          prev_close:  _close != null ? _close : '',
          day_pnl:     dayVal,
          pnl:         pnlVal,
          exp_pnl:     expVal,
          legs:        g.legs_without,
          qty_fno:     g.qty_fno || '',
        };
      });
      exportRowsToCsv(
        rows,
        [
          { header: 'Underlying',   key: 'underlying' },
          { header: 'Spot',         key: 'spot',        format: (v) => v == null ? '' : String(v) },
          { header: 'Chg %',        key: 'day_pct',     format: (v) => v === '' ? '' : Number(v).toFixed(2) },
          { header: 'Close',        key: 'prev_close',  format: (v) => v == null ? '' : String(v) },
          { header: 'Day P&L',      key: 'day_pnl',     format: (v) => String(v) },
          { header: 'P&L',          key: 'pnl',         format: (v) => String(v) },
          { header: 'Exp P&L',      key: 'exp_pnl',     format: (v) => String(v) },
          { header: 'Legs',         key: 'legs',        format: (v) => String(v) },
          { header: 'F&O Qty',      key: 'qty_fno',     format: (v) => v == null ? '' : String(v) },
        ],
        'snapshot.csv'
      );
    }}
  >
    {#snippet middle()}
      <StrategyPicker label="Strategy" />
    {/snippet}
  </CardHeader>
  {#if !_colByund}
    <div class="byund-scroll algo-grid-chrome">
      <div class="byund-grid">
        <div class="byund-headrow">
          <span>Underlying</span>
          <span class="num" title="Live underlying LTP. Indices use the spot price; MCX commodities use the nearest-future LTP (no tradeable spot).">LTP</span>
          <span class="num" title="Underlying day-change %, signed (+/-). Computed from broker `change_percent`, else (LTP - prev_close) / prev_close.">Chg %</span>
          <span class="num" title="Underlying previous-session close (broker `ohlc.close`).">P.Close</span>
          <span class="num" title="Today's Day P&L for the underlying — matches the payoff overlay value for this symbol.">Day P&amp;L</span>
          <span class="num" title="Total P&L from F&O legs only. Sums to the NavStrip P slot 2 value.">P&amp;L</span>
          <span class="num" title="F&O-only expiry P&L for this group. Sums to the NavStrip P slot 3 value.">Exp P&amp;L</span>
          <span class="num" title="Extrinsic value in P&L terms — Exp P&L minus (ltp−avg)×qty. Positive when premium captured exceeds current mark-to-market.">Extrinsic</span>
          <span class="num">Legs</span>
          <span class="num" title="Sum of contract-qty across option + future legs.">F&amp;O qty</span>
          <span class="num"
                title="Expected value — probability-weighted average payoff at expiry. Shown for the currently-selected underlying when a strategy is loaded (uses the same merged EV as the Payoff overlay); other rows show Exp P&L as a proxy since no per-underlying EV exists without a loaded strategy for that root. TOTAL sums exactly these per-row values.">
            EV
          </span>
        </div>
        {#if _byUnderlyingTotals.length === 0}
          <div class="byund-empty">
            {#if positions.length === 0 && holdings.length === 0}
              Loading book…
            {:else if selectedAccounts.length > 0}
              No positions or holdings in the selected account(s).
            {:else}
              No positions or holdings in the book.
            {/if}
          </div>
        {/if}
        {#each _byUnderlyingTotals as g (g.underlying)}
          {@const _q = _underlyingQuotes[g.underlying]}
          {@const _live  = _undLive[g.underlying]}
          <!-- _live is treated as one atomic unit (ltp+close together) —
               falls back to the _underlyingQuotes trio (ltp+prev_close+
               day_pct) wholesale only when _live has nothing yet for
               this root. Never mixes a _live ltp with a _q-sourced
               close/day_pct — that reintroduces the exact
               internally-inconsistent-row bug this fix closes. -->
          {@const _ltp   = _live ? _live.ltp   : (_q ? Number(_q.ltp) : null)}
          {@const _close = _live ? _live.close : (_q ? Number(_q.prev_close) : null)}
          {@const _pct   = _live
              ? (_close != null && _close > 0 ? ((_ltp - _close) / _close) * 100 : null)
              : (_q?.day_pct ?? null)}
          <!-- Filter consistency (§5, tightened item-3 round 4): Day P&L /
               lifetime P&L come from THIS row's own g.day_without /
               g.pnl_without — already filtered by the same
               matchAccount/matchStrategy gate that determined whether this
               row is even shown (rollupByUnderlying). Exp P&L uses
               _rowExpPnlFor, which ALWAYS reads _filteredExpPnlByRoot —
               the same account/strategy-filtered _perRootReduce
               infrastructure, for every row, selected underlying included
               (no more special-casing the selected root against the
               differently-filtered Legs-tab TOTAL — see _rowExpPnlFor's
               docstring). Extrinsic uses the same _perRootReduce
               filtered-reduction pattern via _filteredExtrinsicByRoot
               (item-4 fix), so it also respects the active
               account/strategy filter and TOTAL sums exactly these
               per-row values. -->
          {@const _dayVal  = g.day_without ?? 0}
          {@const _pnlVal  = g.pnl_without ?? 0}
          {@const _expVal  = _rowExpPnlFor(g.underlying)}
          {@const _extVal  = _filteredExtrinsicByRoot[g.underlying] ?? 0}
          <!-- Per-underlying EV: real merged EV (_mergedEv) when the current
               strategy is scoped to this exact root — matches the Payoff
               overlay's EV chip and _snapshotTotalEvFull's per-row term
               exactly. Other roots have no loaded-strategy EV to show, so
               they fall back to Exp P&L as a proxy (same fallback
               _snapshotTotalEvFull assumes for non-selected roots) rather
               than a real (but uncomputed) EV. -->
          {@const _rowEv = _rowEvFor(g.underlying)}
          <div class="byund-row {(g.qty_fno ?? 0) > 0 ? 'byund-dir-long' : (g.qty_fno ?? 0) < 0 ? 'byund-dir-short' : ''}">
            <span class="byund-und" style="background: {acctColor(g.underlying) ? acctColor(g.underlying) + '1a' : 'transparent'}">{g.underlying}</span>
            <span class="num {ltpDayClass(_pct)} {flash.classOf(`${g.underlying}:ltp`)}">{_ltp != null && _ltp > 0 ? priceFmt(_ltp) : '—'}</span>
            <span class="num byund-chg-sep {ltpDayClass(_pct)} {flash.classOf(`${g.underlying}:ltp`)}">{_pct != null ? `${_pct.toFixed(2)}%` : '—'}</span>
            <span class="num">{_close != null && _close > 0 ? priceFmt(_close) : '—'}</span>
            <span class="num {_dayVal > 0 ? 'cell-pos' : _dayVal < 0 ? 'cell-neg' : 'cell-flat'}">{aggCompact(_dayVal)}</span>
            <span class="num {_pnlVal > 0 ? 'cell-pos' : _pnlVal < 0 ? 'cell-neg' : 'cell-flat'}">{aggCompact(_pnlVal)}</span>
            <span class="num {_expVal > 0 ? 'cell-pos' : _expVal < 0 ? 'cell-neg' : 'cell-flat'}">{_expVal === 0 ? '—' : aggCompact(_expVal)}</span>
            <span class="num {_extVal > 0 ? 'cell-pos' : _extVal < 0 ? 'cell-neg' : 'cell-flat'}">{_extVal === 0 ? '—' : aggCompact(_extVal)}</span>
            <span class="num cell-muted">{Math.round(g.legs_without)}</span>
            <span class="num cell-muted">{g.qty_fno || '—'}</span>
            <span class="num {_rowEv > 0 ? 'cell-pos' : _rowEv < 0 ? 'cell-neg' : 'cell-muted'}">
              {_rowEv !== 0 ? aggCompact(_rowEv) : '—'}
            </span>
          </div>
        {/each}
        {#if _byUnderlyingTotals.length > 0}
          <div class="byund-row byund-row-total">
            <span class="byund-und" style="color: inherit" title="F&O positions total">TOTAL</span>
            <span class="num"></span>
            <span class="num"></span>
            <span class="num"></span>
            <span class="num tf-cell {_snapshotTotalDay > 0 ? 'cell-pos' : _snapshotTotalDay < 0 ? 'cell-neg' : 'cell-flat'}">{aggCompact(_snapshotTotalDay)}</span>
            <span class="num tf-cell {_snapshotTotalPnl > 0 ? 'cell-pos' : _snapshotTotalPnl < 0 ? 'cell-neg' : 'cell-flat'}">{aggCompact(_snapshotTotalPnl)}</span>
            <span class="num tf-cell {_snapshotTotalExp > 0 ? 'cell-pos' : _snapshotTotalExp < 0 ? 'cell-neg' : 'cell-flat'}">{aggCompact(_snapshotTotalExp)}</span>
            <span class="num tf-cell {_snapshotTotalExtrinsic > 0 ? 'cell-pos' : _snapshotTotalExtrinsic < 0 ? 'cell-neg' : 'cell-flat'}">{_snapshotTotalExtrinsic === 0 ? '' : aggCompact(_snapshotTotalExtrinsic)}</span>
            <span class="num">{Math.round(_byUnderlyingTotal.legs_without)}</span>
            <span class="num">{_byUnderlyingTotal.qty_fno || ''}</span>
            <span class="num {_snapshotTotalEvFull > 0 ? 'cell-pos' : _snapshotTotalEvFull < 0 ? 'cell-neg' : 'cell-flat'}">
              {_snapshotTotalEvFull !== 0 ? aggCompact(_snapshotTotalEvFull) : ''}
            </span>
          </div>
        {/if}
      </div>
    </div>
  {/if}
</div>

<!-- Aggregate / Greeks / Risk cards — three cards in a horizontal
     flex row under the candidates panel. Each card has its own
     internal kv-pair flow. -->
{#if strategy}
  <aside class="opt-side opt-side-row">

        <div class="opt-block">
          <div class="opt-block-h">
            Greeks (position)
            <InfoHint popup text={'Sum of every leg\'s signed-qty Greeks, including +qty per enabled equity-holding leg (long stock = +1 Δ/share). Θ / 𝒱 / Γ / ρ stay option-only since vanilla stock has zero convexity, decay, IV and rate sensitivity.'} />
          </div>
          <div class="opt-kv opt-kv-greeks">
            <div class="kv-pair">
              <span class="kv-k kv-k-greek">Δ <InfoHint popup text="Delta — net directional exposure. +50 ≈ ₹50 gained per ₹1 spot rise. Includes +qty for enabled equity-holding legs." /></span>
              <span class="kv-v tf-cell">{pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.delta)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k kv-k-greek">Γ <InfoHint popup text="Gamma — rate-of-change of delta as spot moves. High Γ = position is becoming more/less directional quickly." /></span>
              <span class="kv-v tf-cell">{pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.gamma)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k kv-k-greek">Θ <InfoHint popup text="Theta — daily decay in rupees. Positive when net short premium. A Θ of −5 = position loses ₹5/day from time decay alone." /></span>
              <span class="kv-v tf-cell {(_mergedGreeks ?? strategy?.aggregate_greeks)?.theta < 0 ? 'kv-neg' : 'kv-pos'}">{pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.theta)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k kv-k-greek">𝒱 <InfoHint popup text="Vega — P&L change per 1% IV move. Positive = long volatility (benefits from IV expansion)." /></span>
              <span class="kv-v tf-cell {(_mergedGreeks ?? strategy?.aggregate_greeks)?.vega < 0 ? 'kv-neg' : 'kv-pos'}">{pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.vega)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k kv-k-greek">ρ <InfoHint popup text="Rho — sensitivity to a 1% rate change. Mostly cosmetic for short-dated index options." /></span>
              <span class="kv-v tf-cell">{pctFmt((_mergedGreeks ?? strategy?.aggregate_greeks)?.rho)}</span>
            </div>
          </div>
        </div>

        <div class="opt-block">
          <div class="opt-block-h">
            Risk &amp; expected value
            <InfoHint popup text={'Aggregate risk + expected value across all legs. Probability-weighted outcomes integrated against the lognormal pdf of the underlying using a qty-weighted IV proxy. POP × magnitudes captures the asymmetry that POP alone misses.'} />
          </div>
          <div class="opt-kv opt-kv-risk">
            <div class="kv-pair">
              <span class="kv-k">R:R <InfoHint popup text={'<b>Risk-to-reward</b> = max_profit / |max_loss|. "1 : 0.5" = risk ₹100 to make ₹50. "1 : 3" = risk ₹100 to make ₹300. <b>—</b> when one side is unbounded.'} /></span>
              <span class="kv-v">{_rrRatio == null ? '—' : `1 : ${pctFmt(_rrRatio)}`}</span>
            </div>
            <!-- Risk-of-ruin: |max_loss| / |net_cost|. How many times the
                 strategy's premium budget is consumed by a single
                 max-loss event. 1× = one max loss wipes the trade's cost
                 basis exactly; >1× = one max loss costs more than the
                 premium paid (sized too aggressively). CBOE Education +
                 every options primer surfaces this alongside R:R. —
                 when net cost is ~0 (free debit/credit) or max_loss is
                 unbounded. Wrapped in {#if} so the {@const} satisfies
                 Svelte's "immediate-child" rule. -->
            <div class="kv-pair">
              <span class="kv-k">Risk-of-ruin <InfoHint popup text={'<b>Risk-of-ruin</b> = |max_loss| / |net_cost|. How many times the strategy\'s premium budget is consumed by a single max-loss event. <b>1.0×</b> means a single retest of max loss wipes the trade\'s cost basis exactly. <b>&gt;1×</b> means one max loss costs more than the premium paid — strategy is sized too aggressively. <b>—</b> when net cost is ~0 (free) or max loss is unbounded.'} /></span>
              <span class="kv-v">{_ror == null ? '—' : `${_ror.toFixed(2)}×`}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">Breakevens <InfoHint popup text={'<b>Breakevens</b> — spot prices at expiry where the strategy\'s P&L crosses zero. Iron condors and butterflies have 2; verticals have 1; fully ITM/OTM 0.'} /></span>
              <span class="kv-v">
                {#if ((_mergedRisk?.breakevens ?? strategy?.risk?.breakevens) ?? []).length}
                  {((_mergedRisk?.breakevens ?? strategy?.risk?.breakevens) ?? []).map(/** @param {number} b */ (b) => priceFmt(b)).join(' / ')}
                {:else}—{/if}
              </span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">POP <InfoHint popup text={'<b>Probability of profit</b> at expiry — sum of lognormal mass over every contiguous profitable region of the payoff curve. For range strategies (iron condors), this measures "P(spot ends inside the wings)".'} /></span>
              <span class="kv-v tf-cell {(_mergedPop ?? strategy?.risk?.pop) > 0.6 ? 'kv-pos' : (_mergedPop ?? strategy?.risk?.pop) < 0.4 ? 'kv-neg' : ''}">{fmtPct(_mergedPop ?? strategy?.risk?.pop)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">EV <InfoHint popup text={'<b>Expected value</b> — POP × win-magnitude − (1−POP) × loss-magnitude, integrated against the lognormal pdf of the underlying. Positive EV = edge in expectation; negative EV = no edge, even if POP is high.'} /></span>
              <span class="kv-v tf-cell {(_mergedEv ?? strategy?.risk?.ev) > 0 ? 'kv-pos' : (_mergedEv ?? strategy?.risk?.ev) < 0 ? 'kv-neg' : ''}">{fmtMoney(_mergedEv ?? strategy?.risk?.ev)}</span>
            </div>
            {#if strategy?.risk?.ev_pct != null}
              <div class="kv-pair">
                <span class="kv-k">EV / cost <InfoHint popup text={'<b>EV / cost</b> — EV as a percentage of |net cost|. Return-on-capital expectation. +5 % = "on average, my outlay returns 5 % of itself per cycle".'} /></span>
                <span class="kv-v tf-cell {(_mergedEvPct ?? strategy?.risk?.ev_pct) > 0 ? 'kv-pos' : (_mergedEvPct ?? strategy?.risk?.ev_pct) < 0 ? 'kv-neg' : ''}">
                  {pctFmt(_mergedEvPct ?? strategy?.risk?.ev_pct)}%
                </span>
              </div>
            {/if}
          </div>
        </div>
  </aside>
{/if}


  {#if !strategy && !strategyErr && !legs.length}
    <div class="text-[0.65rem] text-[var(--c-muted)] italic mb-3">
      No legs yet. Pick an underlying above to surface candidates, or click
      <b>+</b> to drop a draft strike into the payoff.
    </div>
  {/if}

<!-- Reusable order ticket — opens via the option-chain CE/PE/futures
     buttons. Phase 1: DRAFT mode wired (appends to local drafts on
     submit). Phase 2 / 3: PAPER + LIVE submit paths land in the
     ticket itself; this page won't need to change. -->
{#if ticketProps}
  <!-- SymbolPanel replaces the raw OrderTicket here. All ticketProps
       spread through to the Ticket tab unchanged. defaultTab is set on
       openTicket() so chain (i) buttons open on the Ticket tab; future
       callsites that want the Chain tab default can set defaultTab:'chain'. -->
  <SymbolPanel
    {...ticketProps}
    onSubmit={onTicketSubmit}
    onClose={closeTicket}
    onAddToWatchlist={defaultWatchlistId != null ? addSymbolToWatchlist : null}
    onAddToBasket={(payload) => {
      // Same shape as a quick-add chain leg so the basket pill renders
      // identically. Symbol's CE/PE/FUT suffix decides the type accent.
      const sym = String(payload.sym || '').toUpperCase();
      const sideTag = payload.side === 'BUY' ? 'BUY' : 'SELL';
      const lots = Number(payload.lots) || 1;
      if (_mergeIntoBasket({ sym, side: sideTag, lots })) {
        basketError = '';
        return;
      }
      const cellKey = /(CE|PE)$/.test(sym)
        ? _quickKeyOpt(0, /CE$/.test(sym) ? 'CE' : 'PE') + ':' + sym
        : _quickKeyFut(sym);
      chainBasket = [...chainBasket, {
        key:      `${sideTag}|${cellKey}|${Date.now()}`,
        side:     sideTag,
        sym,
        exchange: payload.exchange || 'NFO',
        lots,
        lotSize:  Number(payload.lotSize) || 1,
        product:  payload.product || 'NRML',
        limit:    Number(payload.limit) || 0,
        chaseAgg: /** @type {'low'|'med'|'high'} */
                   (payload.chaseAgg || 'low'),
      }];
      basketError = '';
    }} />
{/if}

<!-- Order-completion toast stack — fixed top-right, non-blocking.
     Pointer-events: auto only on the toasts themselves so the
     surrounding screen (chain, drafts table, OrderTicket modal that
     might still be open) stays fully interactive. Each toast shows
     mode + side + qty + symbol + price + order_id + status; auto-
     dismisses after 5 s with the option to dismiss earlier via ×. -->
{#if _orderToasts.length > 0}
  <div class="order-toast-stack" aria-live="polite" aria-atomic="false">
    {#each _orderToasts as t (t.id)}
      <div class="order-toast order-toast-{t.status.toLowerCase()}"
           role="status">
        <div class="order-toast-head">
          <span class="order-toast-mode order-toast-mode-{t.mode.toLowerCase()}">{t.mode}</span>
          <span class="order-toast-status">{t.status}</span>
          <button type="button" class="order-toast-close"
                  aria-label="Dismiss"
                  onclick={() => _dismissOrderToast(t.id)}>×</button>
        </div>
        <div class="order-toast-body">
          <span class="order-toast-side order-toast-side-{t.side.toLowerCase()}">{t.side}</span>
          <span class="order-toast-qty">{t.qty}</span>
          <span class="order-toast-sym">{formatSymbol(t.symbol)}</span>
          <span class="order-toast-px">{t.price}</span>
        </div>
        <div class="order-toast-foot">
          <span class="order-toast-oid">#{t.orderId}</span>
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if _chartModalSym}
  <ChartModal
    symbol={_chartModalSym}
    exchange={_chartModalExch}
    onClose={() => { _chartModalSym = ''; _chartModalExch = ''; }} />
{/if}

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <!-- Context-action order panel (opened from snapshot row right-click).
       Routes through onTicketSubmit so it fires the same placement toast
       as the main SymbolPanel. -->
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={onTicketSubmit}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
{/if}

{#if _legPairModalOpen}
  <OrderPairModal bind:open={_legPairModalOpen} symbolHint={_firstCheckedSymbol} pairedCandidates={_pairedCandidates} />
{/if}

<style>
  /* Page-header title + (i) + optional SIM badge as a single inline
     group on the left, timestamp pushed right by .page-header's
     justify-content: space-between. Earlier the InfoHint and ts both
     sat as direct flex children of .page-header, which spread the (i)
     midway down the row instead of beside the title — the operator
     wanted the (i) snug to the title with the timestamp on the right. */
  /* Picker bar — Account / Underlying / Expiry / + always on a
     single row, even on narrow viewports. flex-wrap: nowrap forces
     the row; min-width: 0 on each field lets the Selects shrink to
     fit (their content scrolls / truncates inside the trigger).
     Per canonical picker rule: every field is `flex: 0 0 auto` so
     the cluster sits flush-left at natural widths and empty space
     trails on the right. */
  /* Underlying picker row — Select laid out flush-left at natural width. */
  .opt-und-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: nowrap;
    min-width: 0;
  }
  .opt-und-row :global(.rbq-select-wrap) { flex: 1 1 auto; min-width: 0; }
  /* Underlying-picker tier colour coding. Operator: "only colo coding
     for root. no chips in dropdown in derivatives page." — drop the
     hint chips entirely; the label colour alone communicates the tier
     so the dropdown rows read as clean monosymbol entries.

     Four tiers, top → bottom:
       Tier 1  data-hint='options'    cyan-400    (has CE/PE position)
       Tier 2  data-hint='futures'    default     (has FUT-only position)
       Tier 3  data-hint='holdings'   amber-400   (equity holding, F&O overlay)
       Tier 4  data-hint='popular'    muted       (popular fallback)

     The `hint` field still rides on the option object purely as the
     CSS marker that wires up `data-hint` on the label — the
     rbq-select-option-hint <span> itself is hidden inside this picker
     so no chip ever renders. */
  .opt-und-row :global(.rbq-select-option-hint) {
    display: none;
  }
  .opt-und-row :global(.rbq-select-option-label[data-hint='options']) {
    color: var(--c-info);         /* cyan-400 — actionable, matches card controls */
    font-weight: 700;
  }
  /* Futures tier uses the default label colour — no override. */
  .opt-und-row :global(.rbq-select-option-label[data-hint='holdings']) {
    color: var(--c-action);         /* amber-400 — equity holding overlay */
  }
  /* Popular/liquid tier — default colour, muted opacity so they read
     as "generic fallback, not your book". */
  .opt-und-row :global(.rbq-select-option-label[data-hint='popular']) {
    opacity: 0.72;
  }
  /* Hint shown below the underlying picker when book is empty. */
  .opt-und-hint {
    font-size: var(--fs-xs, 0.65rem);
    color: var(--c-muted);
    font-style: italic;
    margin-top: 0.2rem;
    line-height: 1.3;
  }

  .opt-picker {
    display: flex;
    flex-wrap: nowrap;
    /* Operator: "leave gap between account, underlying, expiry and
       chip". Re-spaced from the prior 1 px hairline strip back to
       a comfortable inter-field gap so each picker reads as its own
       control rather than blending into the next. */
    gap: 0.6rem;
    align-items: flex-end;
  }
  /* AccountMultiSelect wraps MultiSelect in an extra `.ams` div with
     its own min/max-width clamp. Underlying (bare Select) and Expiry
     (bare MultiSelect) don't have that wrapper, so their columns hug
     the trigger natively — there's no gap between them. Force the
     Account column to behave the same way by collapsing the `.ams`
     wrapper to `display: contents`: the wrapper produces no box of
     its own, and the inner MultiSelect lays out as a direct child of
     `.opt-field`, exactly mirroring how Underlying / Expiry sit
     inside their columns. Trades off the disabled-state title
     tooltip the `.ams` wrapper hosts — not needed here since the
     picker is always enabled on this page. */
  .opt-picker :global(.ams) {
    display: contents;
  }

  /* SIMULATOR badge — sim-active context surfaces as a pink pill
     matching the navbar's SIM pill colour family. Audit caught this
     as critical: the classes were referenced but defined nowhere,
     so the badge rendered as plain text and the sim-active context
     was invisible. */
  .opt-mode-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.5rem;
    border-radius: 999px;
    font-size: var(--fs-xs);
    font-weight: 800;
    font-family: var(--font-numeric);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    line-height: 1.4;
    flex-shrink: 0;
  }
  .opt-mode-sim {
    color: #f9a8d4;
    background: rgba(236, 72, 153, 0.18);
    border: 1px solid rgba(236, 72, 153, 0.55);
  }
  /* Slim stale-status bars — shown below the page header for positions
     lag (amber) and strategy errors (red). Replaces the old inline chip. */
  .pos-stale-bar {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.5rem;
  }
  .pos-stale-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: rgba(251, 191, 36, 0.9);
    flex-shrink: 0;
    animation: pos-dot-blink 2s ease-in-out infinite;
  }
  .pos-stale-dot-red {
    background: rgba(248, 113, 113, 0.9);
  }
  .pos-stale-text {
    font-size: var(--fs-sm, 0.6rem);
    color: var(--c-muted);
    letter-spacing: 0.03em;
  }
  @keyframes pos-dot-blink {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.3; transform: scale(0.7); }
  }
  @media (prefers-reduced-motion: reduce) {
    .pos-stale-dot { animation: none; }
  }
  .opt-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;          /* allow shrink past content size */
  }
  /* Desktop: ALL fields LEFT-aligned at natural widths. Per the
     canonical picker-bar rule — controls cluster on the left, no
     flex-grow on any single field, empty space pushed to the right.
     Mobile retains all-grow so content-width pickers don't wrap
     awkwardly in a narrow column. */
  /* Account column: no outer min-width reservation. AccountMultiSelect
     owns its own width clamp (5.6–8.8rem desktop) so the column hugs
     the AMS trigger — no empty padding to the right of "All accounts"
     that would read as a gap between Account and Underlying. */
  .opt-field-grow { flex: 0 0 auto; }
  @media (max-width: 899px) {
    .opt-field { flex: 1 1 0; }
  }
  @media (min-width: 900px) {
    /* All fields content-sized (`flex: 0 0 auto`, no min-width) so the
       row reads as one tight left-flush cluster: Account → Underlying
       → Expiry → Chain. Operator: "left align all the fields accounts,
       underlying, expiry and chain" — every field hugs its trigger,
       1px gap between, empty space trails on the right. */
    .opt-picker .opt-field:nth-of-type(2),
    .opt-picker .opt-field:nth-of-type(3) {
      flex: 0 0 auto;
    }
  }

  /* Refresh button moved onto the chart's top-right corner — see
     OptionsPayoff.svelte for its styles. */

  .opt-grid {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
    gap: 0.6rem;
    margin-bottom: 0.6rem;
  }
  @media (max-width: 980px) {
    .opt-grid { grid-template-columns: 1fr; }
  }

  .opt-section-h {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    /* Locked to canonical .algo-card-title typography tokens so this
       header renders identically to Snapshot / Order entry / GREEKS.
       Operator: "make them consistent and uniform." */
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--c-action);
    padding: 0 0.25rem 0;
    /* Match .opt-block-h (Greeks heading) — the amber underline
       anchors the label and makes it read as bright as the Greeks
       card title on the same page. Operator: "GREEKS is brighter." */
    border-bottom: 1px solid rgba(251,191,36,0.18);
    margin-bottom: 0.4rem;
    flex-wrap: wrap;
  }
  /* Chips scroll container — holds EV + Greek chips in the Payoff
     header. Takes all remaining space between the title and the
     button cluster; scrolls horizontally so chips never wrap into
     the controls area on narrow viewports. Scrollbar hidden on all
     browsers; touch-momentum scrolling preserved on iOS. */
  .opt-section-chips {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1 1 0;
    min-width: 0;
    overflow-x: auto;
    overflow-y: visible;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .opt-section-chips::-webkit-scrollbar { display: none; }
  .opt-section-tag {
    font-size: var(--fs-md);
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
    font-weight: 700;
    white-space: nowrap;
  }
  /* Compact mobile rendering — keeps the NET DEBIT / MAX PROFIT /
     MAX LOSS chips on one line by trimming font + padding + the
     section gap so the row fits inside a ~360px viewport. */
  @media (max-width: 600px) {
    .opt-section-h { gap: 0.25rem; flex-wrap: nowrap; overflow-x: auto; }
    .opt-section-tag { font-size: var(--fs-sm); padding: 1px 3px; letter-spacing: 0.02em; }
  }
  /* Tight no-wrap cluster for the card-control trio (Collapse +
     DefaultSize + Fullscreen + optional Refresh in fullscreen mode).
     Without this, the outer `.opt-section-row gap: 0.5rem` applied
     between each child and the trio ate ~1rem of horizontal space —
     enough to spill the Fullscreen button to a second row on mobile.
     Inside the cluster the buttons sit at 0.15rem; the outer 0.5rem
     gap separates the cluster from the previous header chip. */
  .payoff-card-controls {
    display: inline-flex;
    align-items: center;
    gap: 0.15rem;
    white-space: nowrap;
    flex-shrink: 0;
    margin-left: auto;
  }
  .tag-deriv  { color: var(--algo-sky); background: rgba(125,211,252,0.10); }
  .tag-long   { color: var(--c-long); background: var(--algo-green-bg); }
  .tag-short  { color: var(--c-short); background: var(--algo-red-bg); }
  /* Greek chips in the payoff header — distinct cyan tint so they
     read as a different category from the amber net-cost / max-PnL
     chips. Theta + Vega flip to a red variant when negative (short
     premium / long volatility carry the inverse sign convention). */
  .tag-greek      { color: #c084fc; background: rgba(192,132,252,0.10); }
  .tag-greek-neg  { color: #fda4af; background: rgba(253,164,175,0.10); }
  .opt-section-meta {
    color: var(--text-muted);
    font-weight: 400;
    font-size: var(--fs-md);
    margin-left: auto;
  }

  /* Default: legacy stacked aside (column of cards). Used when the
     side panel sat next to the chart. */
  .opt-side {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  /* Row variant: the three cards (Aggregate / Greeks / Risk) sit
     side by side under the candidates panel. Each card grows
     proportionally; on narrower viewports the row wraps to a column. */
  .opt-side-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.6rem;
    margin-bottom: 0.6rem;
  }
  .opt-block {
    background: var(--card-bg-gradient);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    padding: 0.5rem 0.65rem;
  }
  /* Canonical .algo-card-title tokens — this is the "GREEKS" heading
     operator explicitly cited as the reference. Locked size + family
     + spacing so future edits can't drift. */
  .opt-block-h {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--c-action);
    border-bottom: 1px solid rgba(251,191,36,0.18);
    padding-bottom: 0.25rem;
    margin-bottom: 0.4rem;
  }
  /* kv-pairs flow TWO per row, with label and value SIDE BY SIDE
     within each pair: "Underlying  CRUDE OIL", "Spot  ₹9000". Two
     pairs per row keeps the cards compact; labels and values
     align flush-left / flush-right so the eye scans cleanly across
     pairs. */
  .opt-kv {
    display: grid;
    grid-template-columns: 1fr 1fr;
    column-gap: 0.7rem;
    row-gap: 0.3rem;
    font-family: monospace;
  }
  .kv-pair {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    min-width: 0;
  }
  .kv-k {
    color: var(--text-muted);
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-size: var(--fs-sm);
    flex: 0 0 auto;
    flex-wrap: nowrap;
    cursor: help;
  }
  /* Non-Greek labels: dashed underline signals "tap for explanation" */
  .kv-k:not(.kv-k-greek) {
    border-bottom: 1px dashed rgba(148,163,184,0.3);
    display: inline-flex;
    align-items: center;
  }
  .kv-v {
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    font-weight: 600;
    margin-left: auto;          /* push value to the right of pair */
    text-align: right;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Greek symbols — clean, slightly heavier than other labels but
     not oversize. The visual identity of each Greek pair without
     overpowering the value next to it. */
  .kv-k-greek {
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--algo-slate);
  }
  /* Greeks card — all five pairs in a single row. The narrow
     `max-content auto` per slot lets each pair size to its own
     content; column-gap stays tight so the row feels uniform. */
  .opt-kv-greeks {
    display: grid;
    grid-template-columns: repeat(5, max-content auto);
    column-gap: 0.45rem;
    row-gap: 0;
  }
  .opt-kv-greeks .kv-pair {
    /* Each pair contributes label + value into two adjacent grid
       cells via `display: contents` — the pair wrapper itself
       doesn't take a grid slot. */
    display: contents;
  }
  .opt-kv-greeks .kv-v {
    font-size: var(--fs-md);
    margin-left: 0.15rem;
    margin-right: 0.5rem;
    text-align: left;
  }
  /* Risk block: single row on desktop */
  @media (min-width: 1180px) {
    .opt-kv-risk {
      grid-template-columns: repeat(6, max-content auto);
      column-gap: 0;
      row-gap: 0;
    }
    .opt-kv-risk .kv-pair {
      display: contents;
    }
    .opt-kv-risk .kv-v {
      margin-left: 0.2rem;
      margin-right: 0.8rem;
      text-align: left;
    }
  }
  .kv-pos { color: var(--c-long); }
  .kv-neg { color: var(--c-short); }
  .kv-sub { color: var(--text-muted); font-size: var(--fs-md); margin-left: 0.2rem; }

  .opt-payoff {
    display: flex;
    flex-direction: column;
  }

  /* Payoff + Legs side-by-side wrapper.
     Stacked column on narrow viewports; on >= 1180 px laptops the
     chart shrinks to ~1.4fr (≈ 58 %) and the legs panel slots into
     the remaining 1fr (≈ 42 %). The cand-scroll inside legs handles
     its own horizontal overflow if the column gets tight. Without the
     `:has(.opt-legs-card)` guard the lone-payoff state would also
     compress to 58 % and leave dead space on the right. */
  .opt-payoff-legs-row {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    margin-bottom: 0.75rem;
  }
  @media (min-width: 1180px) {
    .opt-payoff-legs-row:has(.opt-legs-card) {
      display: grid;
      /* Equal-width columns — each card claims half the row width.
         Earlier this was 1.4fr / 1fr (payoff ≈ 58 %), favouring the
         chart; operator preference is 50 / 50 so the candidate list
         gets the same horizontal real estate as the chart. */
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      /* Stretch the two cards to the tallest sibling's height so the
         row reads as one visual unit. The legs card's .cand-scroll
         absorbs the extra vertical space via flex (rule below). */
      align-items: stretch;
    }
    /* Legs card becomes a flex column so .cand-scroll can flex:1
       and fill the height delta between header + scroll viewport.
       min-height:0 prevents the card from escaping the grid's
       align-items:stretch constraint via its natural content height
       (without it, a tall candidate list pushes the legs card past
       the payoff card's fixed height and the two stop being equal). */
    .opt-payoff-legs-row:has(.opt-legs-card) .opt-legs-card {
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    /* flex:1 1 0 + min-height:0 lets the scroll wrapper shrink to fit
       the legs card's grid-stretched height instead of growing to its
       content height (which broke height parity with the payoff card).
       Vertical overflow scrolls internally; horizontal overflow is
       unchanged. max-height:none removed — no longer needed because
       the grid+flex chain caps the wrapper at the payoff card height. */
    .opt-payoff-legs-row:has(.opt-legs-card) .opt-legs-card .cand-scroll {
      flex: 1 1 0;
      min-height: 0;
      overflow-y: auto;
      max-height: none;
    }
  }
  /* Leg builder — compact monospace grid mirroring the simulator's
     custom-positions panel so the two read as siblings. */
  .leg-grid {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    margin-top: 0.4rem;
  }
  .leg-headrow,
  .leg-row {
    display: grid;
    grid-template-columns: minmax(0, 2.2fr) minmax(0, 0.9fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 0.8fr) auto;
    gap: 0.35rem;
    align-items: center;
  }
  .leg-headrow {
    font-family: monospace;
    font-size: var(--fs-md);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
  }
  .leg-pair-btn {
    font-size: 0.75rem;
    padding: 0.18rem 0.55rem;
    border-radius: 3px;
    border: 1px solid rgba(190,210,240,0.55);
    background: rgba(160,185,220,0.08);
    color: rgba(190,210,240,0.95);
    cursor: pointer;
  }
  .leg-pair-btn:hover { border-color: rgba(160,185,220,0.5); color: #e2eeff; }
  .leg-pair-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  :global(.leg-row .field-input) {
    font-size: var(--fs-sm);
    padding: 0.25rem 0.4rem;
    font-family: monospace;
  }
  .leg-source {
    font-family: monospace;
    font-size: var(--fs-md);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    text-align: center;
  }
  .leg-source-live   { color: var(--c-long); }
  .leg-source-sim    { color: var(--c-action); }
  .leg-source-manual { color: var(--algo-sky); }
  .leg-source-draft  { color: #f0abfc; }
  .leg-del {
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    border: 1px solid rgba(248,113,113,0.4);
    background: rgba(248,113,113,0.08);
    color: var(--c-short);
    font-size: var(--fs-xl);
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .leg-del:hover {
    background: rgba(248,113,113,0.18);
    border-color: rgba(248,113,113,0.65);
  }

  /* Candidate position toggle list — sits immediately under the
     payoff chart. The wrapping `.cand-scroll` handles overflow:
       - horizontal: when the row is wider than the card (narrow
         viewport, long symbols), the table scrolls within the card
         instead of breaking layout
       - vertical: capped at ~16 rows; longer lists scroll inside
   */
  /* Hint chip — appears above the Legs grid when the Account filter
     is hiding rows. Grey palette so it reads as informational, not
     alarming. */
  .cand-hidden-hint {
    margin-top: 0.4rem;
    padding: 0.25rem 0.55rem;
    font-size: var(--fs-sm);
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--text-muted);
    background: rgba(126,151,184,0.10);
    border: 1px solid rgba(126,151,184,0.30);
    border-radius: 3px;
    cursor: help;
  }

  /* Draft/provisional payoff toggle bar — amber-tinted strip below the
     hidden-account hint (when present). Shown only when at least one
     ~ or D row is visible. Matches the amber "non-confirmed" palette. */
  .cand-draft-payoff-bar {
    margin-top: 0.4rem;
    padding: 0.25rem 0.55rem;
    font-size: var(--fs-sm);
    background: var(--algo-amber-bg);
    border: 1px solid var(--algo-amber-border);
    border-radius: 3px;
    display: flex;
    align-items: center;
  }
  .cand-draft-payoff-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    cursor: pointer;
    user-select: none;
  }
  .cand-draft-payoff-toggle input[type="checkbox"] {
    accent-color: var(--c-action);
    width: 0.9rem;
    height: 0.9rem;
    cursor: pointer;
  }
  .cand-draft-payoff-label {
    color: var(--c-action);
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  /* Candidate row status chips — open/closed order state indicators. */
  .cand-status-chip {
    display: inline-flex;
    align-items: center;
    padding: 0 0.3rem;
    border-radius: 3px;
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    vertical-align: middle;
    line-height: 1.5;
  }
  .cand-open-chip {
    background: rgba(74, 222, 128, 0.15);
    border: 1px solid rgba(74, 222, 128, 0.4);
    color: var(--c-long, #4ade80);
  }
  .cand-closed-chip {
    background: rgba(126, 151, 184, 0.12);
    border: 1px solid rgba(126, 151, 184, 0.3);
    color: var(--c-muted, #7e97b8);
  }

  /* LTP heat encoding for .cand-grid rows moved to CandidateLegRow.svelte. */

  /* Empty-state row inside .cand-grid — spans the whole grid width
     so it reads as a single placeholder line, not a half-width cell
     in the leftmost column. Keeps the scroll wrapper + header row
     mounted across underlying switches (the grid only rebuilds its
     row contents, not its chrome). */
  .cand-empty {
    grid-column: 1 / -1;
    padding: 0.85rem 0.7rem;
    font-family: monospace;
    font-size: var(--fs-md);
    color: var(--c-muted);
    font-style: italic;
    text-align: center;
    align-self: start;
  }
  .cand-empty.cand-loading {
    align-self: stretch;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 8rem;
  }

  /* ── Per-underlying snapshot card ────────────────────────────────
     Compact 8-column table; first column is the underlying root,
     followed by leg-count + qty + four side-by-side P&L columns
     (Hold / No-Hold for both lifetime + today). Right-aligned numeric
     cells with the standard cell-pos / cell-neg / cell-flat tinting. */
  /* Chrome delegated to .algo-grid-chrome class on the element. */
  .byund-scroll {
    overflow-x: auto;
    margin-top: 0.4rem;
  }
  .byund-grid {
    display: grid;
    grid-template-columns:
      minmax(3.5rem, 0.55fr) /* underlying */
      minmax(4rem,   0.65fr) /* Spot */
      minmax(3.5rem, 0.5fr)  /* Day % */
      minmax(4rem,   0.65fr) /* Prev Close */
      minmax(3.8rem, 0.6fr)  /* Day P&L */
      minmax(3.8rem, 0.6fr)  /* P&L */
      minmax(4rem,   0.6fr)  /* Exp P&L */
      minmax(4rem,   0.6fr)  /* Extrinsic */
      minmax(3rem,   0.55fr) /* Legs */
      minmax(4rem,   0.6fr)  /* F&O qty */
      minmax(4rem,   0.6fr); /* EV */
    min-width: 1020px;
    font-family: var(--font-numeric);
    font-size: 0.72rem;              /* match Pulse Positions ~0.625rem */
  }
  .byund-headrow,
  .byund-row {
    display: contents;
  }
  /* Header row — deep-dark bg + muted-slate text + amber bottom border,
     matching .hist-table (History page) canonical header treatment. */
  .byund-headrow > span {
    padding: 0.3rem 0.45rem;
    background: rgba(15,23,42,0.65);  /* matches History / ag-theme-algo */
    border-bottom: 1px solid var(--algo-amber-border-soft);
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted);
  }
  /* Data cells — no left/right borders (Pulse pattern strips them so
     the row reads as one continuous band). Faint slate bottom border
     separates rows. tabular-nums so digit widths don't jitter on
     poll updates. */
  .byund-row > span {
    padding: 0.32rem 0.45rem;
    border-bottom: 1px solid rgba(126,151,184,0.10);
    color: var(--algo-slate);
    transition: background-color 0.1s;
  }
  .byund-row > span.num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .byund-headrow > span.num {
    text-align: right;
  }
  /* Alternating row background — matches ag-theme-algo .ag-row-odd */
  .byund-row:nth-of-type(odd) > span {
    background-color: var(--ag-odd-row-background-color, rgba(13,22,42,0.30));
  }
  /* Row hover — cyan tint, matching History hover rgba(34,211,238,0.05). */
  .byund-row:hover > span {
    background-color: rgba(34,211,238,0.05) !important;
  }
  .byund-und {
    font-weight: 700;
    color: var(--c-action);
    letter-spacing: 0.02em;
    font-variant-numeric: tabular-nums;
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  .byund-row > .cell-pos { color: var(--c-long); }
  .byund-row > .cell-neg { color: var(--c-short); }
  .byund-row > .cell-flat { color: var(--c-muted); }
  .byund-row > .cell-muted { color: rgba(200,216,240,0.65); }

  /* Tick-flash animation — transient background pulse when a tracked
     numeric cell changes. Subtle alpha so the flash reads as ambient
     liveness, not an alert; 350ms decay keeps it out of the way of
     the next poll cycle. cell-pos / cell-neg COLOR rules above still
     apply — flash paints background only, text color stays signed.
     In fullscreen mode the alpha doubles so the operator sees the
     pulse from across the room. */
  @keyframes tf-pulse-up {
    0%   { background-color: var(--algo-green-bg-strong); }
    100% { background-color: transparent; }
  }
  @keyframes tf-pulse-down {
    0%   { background-color: var(--algo-red-bg-strong); }
    100% { background-color: transparent; }
  }
  .byund-row > .tf-up   { animation: tf-pulse-up   350ms ease-out; }
  .byund-row > .tf-down { animation: tf-pulse-down 350ms ease-out; }
  :global(.fs-card-on) .byund-row > .tf-up {
    animation: tf-pulse-up-fs 500ms ease-out;
  }
  :global(.fs-card-on) .byund-row > .tf-down {
    animation: tf-pulse-down-fs 500ms ease-out;
  }
  @keyframes tf-pulse-up-fs {
    0%   { background-color: rgba(74, 222, 128, 0.42); }
    100% { background-color: transparent; }
  }
  @keyframes tf-pulse-down-fs {
    0%   { background-color: rgba(248, 113, 113, 0.42); }
    100% { background-color: transparent; }
  }

  /* tf-cell — shared marker for ALL directional flash targets outside
     the Snapshot grid (payoff chips, kv-block values, legs cells,
     TOTAL rows). Same 350ms decay + 0.12 alpha as byund-row. The
     `.byund-row >` selector above keeps the Snapshot grid on its
     tighter parent-scoped rule; this rule covers the rest. */
  .tf-cell.tf-up   { animation: tf-pulse-up   350ms ease-out; }
  .tf-cell.tf-down { animation: tf-pulse-down 350ms ease-out; }

  @media (prefers-reduced-motion: reduce) {
    .byund-row > .tf-up,
    .byund-row > .tf-down,
    :global(.fs-card-on) .byund-row > .tf-up,
    :global(.fs-card-on) .byund-row > .tf-down,
    .tf-cell.tf-up,
    .tf-cell.tf-down { animation: none; }
  }
  /* byund TOTAL > span: amber background/border/color/font-weight from shared
     .cand-row.cand-row-total, .byund-row-total > span rule above.
     Padding inherited from .byund-row > span. Direction tints below. */
  .byund-row-total > .cell-pos  { color: var(--algo-green) !important; }
  .byund-row-total > .cell-neg  { color: var(--algo-red) !important; }
  .byund-row-total > .cell-flat { color: var(--algo-slate) !important; }
  .byund-empty {
    grid-column: 1 / -1;
    padding: 0.85rem 0.7rem;
    font-family: monospace;
    font-size: var(--fs-md);
    color: var(--c-muted);
    font-style: italic;
    text-align: center;
  }

  :global(.fs-card-on) .cand-scroll {
    max-height: calc(100vh - 28rem) !important;
  }

  /* Chrome delegated to .algo-grid-chrome class on the element. */
  .cand-scroll {
    overflow-x: auto;
    overflow-y: auto;
    max-height: 22rem;
    margin-top: 0.4rem;
    /* Scrollbar styling — operator: "add scroll bars so that newly
       added columns get the space required in legs". Bumped from
       6 px to 10 px so the horizontal track reads as a clear
       affordance ("there's more grid off-screen") instead of a
       hairline that's easy to miss. */
    scrollbar-width: auto;
    scrollbar-color: var(--algo-amber-border) rgba(15, 25, 45, 0.6);
  }
  .cand-scroll::-webkit-scrollbar { height: 10px; width: 10px; }
  .cand-scroll::-webkit-scrollbar-track {
    background: rgba(15, 25, 45, 0.6);
    border-radius: 4px;
  }
  .cand-scroll::-webkit-scrollbar-thumb {
    background: var(--algo-amber-border);
    border-radius: 4px;
  }
  .cand-scroll::-webkit-scrollbar-thumb:hover {
    background: rgba(251,191,36,0.85);
  }
  /* Legs header row — flex container that hosts the legs-header
     button (chevron + title + tag + count) on the left and the
     global Collapse + Fullscreen toggles clustered on the right.
     Same idiom as dashboard .card-header-row. */
  .legs-header-row {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    width: 100%;
  }
  .legs-header-row > .legs-header { flex: 1 1 auto; width: auto; }

  /* Legs panel header — collapsable. Reset button defaults so it
     still picks up the .opt-section-h typography but with a click
     affordance + a rotating chevron on the left. */
  .legs-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    background: none;
    border: 0;
    padding: 0 0.25rem 0;
    cursor: pointer;
    color: var(--c-action);
    font-family: inherit;
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: left;
    flex-wrap: nowrap;
    overflow-x: auto;
    min-width: 0;
    scrollbar-width: none;
  }
  .legs-header:hover { color: #fde047; }

  /* Legs / Expiry tabs migrated to canonical AlgoTabs (compact
     amber; expiry switches to rose color when expiryCloseTotal>0
     so the unfilled-close alert reads as warning instead of
     neutral). All `.legs-tab*` CSS retired — AlgoTabs owns the
     decoration via `.algo-tab` in app.css. */

  /* Underlying chip — sits before the legs/close tabs. Visually
     distinct from the tabs so the operator's eye reads it as
     "what symbol am I looking at" first, then "which view".
     Solid amber pill with a darker background; the separator
     below pushes the tabs visually apart. */
  .legs-underlying-chip {
    color: var(--c-action, #fbbf24);
    font-size: var(--fs-sm, 0.6rem);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .legs-header-sep {
    display: inline-block;
    width: 1px;
    height: 1.1rem;
    background: rgba(200, 216, 240, 0.25);
    margin: 0 0.25rem;
    flex-shrink: 0;
  }

  /* Expiry tab band tints, expiry-band-header, expiry-id-chip, and
     all cand-row variant styles moved to CandidateLegRow.svelte. */

  /* Parent grid — defines column tracks once. Children (`.cand-headrow`
     and each `.cand-row`) consume the same tracks via `subgrid` so
     headers + data cells line up precisely.
     Column order (cluster rule, May 2026): checkbox · Symbol ·
     Expiry · Account · Qty · LTP · Prev · Avg · P&L · IV · Δ · Γ ·
     Θ · 𝒱. The LTP → Prev → Avg → P&L block stays contiguous (the
     codebase-wide adjacency rule); Greeks trail at the end so they
     don't break the price-action cluster.

     Track sizing — split into two policies:
       • Symbol + Account: `minmax(max-content, Nfr)`. Min stays at
         the widest cell so the F&O ticker + account code never
         truncate. These are the identifying columns; the operator
         needs to read them in full.
       • Numeric (qty / pnl / cost / ltp / iv / delta / theta / vega):
         `minmax(34px, 1fr)`. The 34 px floor is ~half the natural
         max-content for a typical numeric cell (≈ 60-70 px), per the
         operator's "halve the min width of every numeric column"
         request — lets the grid fit narrower containers without
         spawning a horizontal scrollbar. Numerics that don't fit at
         34 px ellipsis-truncate (cell-level rule below); the row's
         `title` attribute carries the full value for hover lookup.
     `min-width: max-content` removed: the grid's intrinsic minimum
     is now driven by Symbol + Account alone, not the sum of every
     column's max-content. */
  /* Alternating row backgrounds for legs grid — stripe class is passed
     from the {#each} index so band-header elements (also grid children)
     do not consume nth-child positions and corrupt the pattern. */
  :global(.cand-row.row-tint-odd):not(:global(.cand-row-total)) {
    background-color: var(--row-tint-odd-bg);
  }
  :global(.cand-row.row-tint-even):not(:global(.cand-row-total)) {
    background-color: transparent;
  }

  .cand-grid {
    display: grid;
    /* Operator: "lots of white space as columns are getting more
       space than required". Switched every track from `minmax(floor,
       1fr)` to `minmax(floor, max-content)` so columns only consume
       what their widest cell actually needs — no stretching to fill
       the card's remaining width. `.cand-scroll` carries the
       horizontal scrollbar when total column widths overflow the
       card; on wide viewports the grid leaves trailing whitespace
       inside the scroll container (acceptable; preserves dense
       column packing). */
    /* Expiry column REMOVED — the hyphenated symbol (e.g.
       NIFTY-26JUN-22000-CE) already encodes the expiry month inline
       via formatSymbol(). Operator no longer needs a separate column;
       saves ~58 px of horizontal real estate per row. */
    grid-template-columns:
      auto                                 /* checkbox */
      28px                                 /* pos state (GTT / paired / orphan) */
      minmax(max-content, max-content)     /* symbol (hyphenated, carries expiry) */
      minmax(62px, max-content)            /* ltp */
      minmax(48px, max-content)            /* chg % */
      minmax(44px, max-content)            /* lots */
      minmax(48px, max-content)            /* qty */
      minmax(52px, max-content)            /* O/N qty - overnight carry */
      minmax(62px, max-content)            /* avg (cost basis) */
      minmax(72px, max-content)            /* prev close */
      minmax(46px, max-content)            /* day pnl - today */
      minmax(54px, max-content)            /* pnl - cumulative */
      minmax(54px, max-content)            /* exp pnl @ current spot */
      minmax(54px, max-content)            /* extrinsic (time value in P&L terms) */
      minmax(max-content, max-content)     /* account */
      minmax(52px, max-content)            /* iv */
      minmax(56px, max-content)            /* delta */
      minmax(56px, max-content)            /* gamma */
      minmax(62px, max-content)            /* theta */
      minmax(56px, max-content)            /* vega */
      minmax(62px, max-content);           /* ev */
    column-gap: 0.35rem;
    width: max-content;
  }
  /* Amber TOTAL stratum — single rule drives both Snapshot (byund-row-total > span)
     and Legs/ExpClose (cand-row-total container). The Legs TOTAL row uses a subgrid
     container that spans grid-column:1/-1, covering column-gap areas, so the amber
     lives on the container (not > span) to avoid dark column-gap gaps. */
  .cand-row.cand-row-total,
  .byund-row-total > span {
    background:
      linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22)),
      #1d2a44 !important;
    border-top: 2px solid rgba(251,191,36,0.70);
    border-bottom: 1px solid rgba(251,191,36,0.55);
    color: var(--c-action);
    font-weight: 700;
  }
  /* Legs TOTAL row — layout + positioning only (amber comes from shared rule above). */
  .cand-row.cand-row-total {
    display: grid;
    grid-template-columns: subgrid;
    grid-column: 1 / -1;
    position: sticky;
    bottom: 0;
    z-index: 2;
    cursor: default;
  }
  /* Legs TOTAL row cell typography — font + padding + overflow on each span.
     Background/border live on the container (shared rule above). */
  .cand-row.cand-row-total > span {
    padding: 0.32rem 0.45rem;
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .cand-row.cand-row-total > span.num { text-align: right; }
  /* Direction tints — lighter green/red readable against amber. */
  .cand-row.cand-row-total > .cell-pos  { color: var(--algo-green) !important; }
  .cand-row.cand-row-total > .cell-neg  { color: var(--algo-red) !important; }
  .cand-row.cand-row-total > .cell-flat { color: var(--algo-slate) !important; }
  .cand-total-label {
    color: var(--c-action);
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  /* cand-split-tag, cand-eq-tag, cand-proxy-tag, cand-row.cand-eq, and
     all cand-row variant styles moved to CandidateLegRow.svelte. */
  /* Cell-level truncation for the header row's numeric cells. The same
     rule for .cand-row > .num lives in CandidateLegRow.svelte. */
  .cand-headrow > .num {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Single parent grid via subgrid. The headrow is scoped here;
     .cand-row display:grid+subgrid lives in CandidateLegRow.svelte. */
  .cand-headrow {
    display: grid;
    grid-template-columns: subgrid;
    grid-column: 1 / -1;
    /* Subgrid inherits column-gap from .cand-grid (0.6rem). Don't
       set `gap` here — that overrides the parent and decouples the
       rows' spacing from the header's. */
    padding: 0.1rem 0.2rem;
    align-items: center;
    font-size: var(--fs-sm);
    font-family: monospace;
    font-variant-numeric: tabular-nums;
  }
  .cand-headrow {
    font-size: 0.65rem;
    font-weight: 800;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid var(--algo-amber-border-soft);  /* amber — matches History */
    /* Sticky header — operator scrolls data rows under it instead of
       the whole grid sliding up. Pinned to top of .cand-scroll (the
       overflow-y container); the card-bottom navy of the parent card
       gradient is reused as a solid fill so data rows don't bleed
       through, and z-index lifts the header above .cand-row hovers. */
    position: sticky;
    top: 0;
    z-index: 2;
    background: rgba(15,23,42,0.65);  /* matches History / ag-theme-algo */
  }
  /* Numeric column cells — right-aligned (industry-standard for
     trade panels) so digits in different rows line up cleanly under
     each column header. The .cand-row > .num portion moved to
     CandidateLegRow.svelte. */
  .cand-headrow > .num {
    text-align: right;
    justify-self: end;
  }
  /* All .cand-row variant styles (cand-row, cand-row:hover, cand-row-long/short,
     cand-row.cand-draft/closed/eq, cand-sym, cand-draft-x, cand-pnl,
     cand-sym-acct, cand-disabled, cand-row input[type="checkbox"]) moved
     to CandidateLegRow.svelte. */
  /* Candidate row's ⋯ actions container — currently unused but kept for future. */
  .cand-actions {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
  }
  /* .cand-kind[-fut|-opt] + .cand-row-btn + .cand-row-active +
     .cand-row-disabled + .cand-bullet retired — replaced by the
     checkbox-driven multi-select Candidates panel. */

  .leg-type-CE {
    color: var(--c-long);
    background: var(--algo-green-bg);
    border: 1px solid rgba(74,222,128,0.4);
    border-radius: 2px;
    padding: 0 4px;
    font-weight: 700;
    font-size: var(--fs-md);
  }
  .leg-type-PE {
    color: var(--c-short);
    background: var(--algo-red-bg);
    border: 1px solid rgba(248,113,113,0.4);
    border-radius: 2px;
    padding: 0 4px;
    font-weight: 700;
    font-size: var(--fs-md);
  }

  /* "Clear" button styled subtly red so the destructive action stands
     out from "+ Add row" / "Analyze" without being scary. */
  :global(.opt-clear) {
    border-color: rgba(248,113,113,0.45) !important;
    color: var(--c-short) !important;
  }
  :global(.opt-clear:hover) {
    background: var(--algo-red-bg) !important;
  }

  /* Stale-LTP / fallback-source chips — surfaced when broker live price
     wasn't available and the engine fell back (close/depth/avg_cost/
     default IV). Lets the operator know which numbers to treat with
     extra care, without burying the result. */
  .src-chip {
    margin-left: 0.5rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
  }
  .src-stale {
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
  }
  .src-tag {
    margin-left: 0.3rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .src-warn { color: var(--c-action); font-weight: 700; }
  /* When .src-warn is paired with .src-chip (background context), give
     it the same amber-tinted background as .src-stale so the chip looks
     like a chip and not just amber text floating on the panel. */
  .src-chip.src-warn { background: var(--algo-amber-bg); }

  /* Per-leg LTP source pill — fresh = sky-blue, stale = amber. Sits in
     its own column on the breakdown table. */
  .leg-src {
    display: inline-block;
    font-family: monospace;
    font-size: var(--fs-sm);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
    font-weight: 700;
  }
  .leg-src-fresh { color: var(--algo-sky); background: rgba(125,211,252,0.10); }
  .leg-src-stale { color: var(--c-action); background: rgba(251,191,36,0.10); }

  /* Option-chain picker — three-column controls (Underlying / Expiry /
     Side) above a CE-strike-PE table. Each row shows one strike with
     Add-leg buttons on either side. Capped height so the page doesn't
     scroll into oblivion when an underlying has 100+ strikes. */
  .chain-controls {
    display: grid;
    /* Underlying gets the most room (long names like CRUDEOIL),
       Expiry mid, Kind compact (2-option multi-select). All three
       on a single row on every viewport — operator wanted them
       squeezed onto one line on mobile rather than wrapping. */
    grid-template-columns:
      minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 0.8fr);
    gap: 0.4rem 0.5rem;
    margin-bottom: 0.5rem;
    align-items: end;
  }
  @media (max-width: 600px) {
    /* Mobile: still all three on one line — squeeze gap + drop the
       Underlying weighting so equal thirds maximise width per cell. */
    .chain-controls {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr);
      gap: 0.25rem 0.3rem;
    }
    .chain-controls .field-label {
      font-size: var(--fs-xs);
    }
    .chain-controls :global(.rbq-select-trigger),
    .chain-controls :global(.rbq-multi-trigger) {
      font-size: var(--fs-sm);
      padding-left: 0.4rem;
      padding-right: 0.4rem;
    }
  }
  .chain-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  /* Futures section above the strike grid. One row per future,
     same `+ − i` button cluster as the strike grid. Sky-blue
     accent on the panel border so the section reads as the
     futures-specific block without competing with the green/red
     option buttons inside. */
  .chain-futures {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-bottom: 0.4rem;
    padding: 0.35rem 0.5rem;
    background: rgba(125,211,252,0.04);
    border: 1px solid rgba(125,211,252,0.20);
    border-radius: 3px;
  }
  .chain-fut-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 1px 0;
    font-family: monospace;
    font-size: var(--fs-md);
  }
  .chain-fut-sym {
    color: var(--algo-slate);
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .chain-fut-meta {
    margin-left: 0.4rem;
    font-weight: 400;
    font-size: var(--fs-xs);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .chain-grid-wrap {
    max-height: 18rem;
    overflow-y: auto;
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 3px;
    background: rgba(0,0,0,0.10);
  }
  .chain-grid {
    width: 100%;
    border-collapse: collapse;
    font-family: monospace;
    font-size: var(--fs-md);
    /* Fixed layout + explicit colgroup widths so the CE and PE
       columns are the SAME width on every row. Without this, the
       inner edges (where bid-ask quotes sit) drift across rows
       depending on per-row content width. */
    table-layout: fixed;
  }
  .chain-th-ce     { text-align: left; color: var(--c-long); }
  .chain-th-pe     { text-align: right; color: var(--c-short); }
  .chain-th-strike { text-align: center; color: var(--algo-slate); }
  /* CE / PE cells host an inner flex wrapper (chain-cell-row) so
     the cells themselves stay regular table-cells (which keeps the
     colgroup widths reliable). The wrapper distributes action +
     bid-ask quote across the cell width: action on the OUTER edge
     of the table, bid-ask on the INNER edge next to the strike. */
  .chain-td-ce      { text-align: left; }
  .chain-td-pe      { text-align: right; }
  .chain-cell-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
    width: 100%;
  }
  .chain-td-strike  { text-align: center; color: var(--algo-slate); font-weight: 700; }
  /* ATM strike — bold amber numeral substitutes for the dropped
     "ATM" pill; the row's amber background + borders carry the rest
     of the highlight, so this stays compact (no extra width). */
  .chain-td-strike-atm {
    color: var(--c-action);
    font-weight: 800;
    letter-spacing: 0.04em;
  }
  /* Per-side bid / ask cell — top-of-book inline next to the strike
     column. Bid (green) - ask (red) reads "what I can hit if I'm
     selling - what I'd pay to buy" at a glance. Single-line layout
     keeps the row height tight; a muted hyphen separates the two
     numbers without crowding them. */
  .chain-cell-quote {
    display: inline-flex;
    flex-direction: row;
    align-items: baseline;
    min-width: 3.4rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 600;
    white-space: nowrap;
    text-align: center;
  }
  .chain-cell-bid { color: var(--c-long); }   /* same green as CE header */
  .chain-cell-ask { color: var(--c-short); }   /* same red as PE header */
  .chain-cell-sep {
    color: var(--c-muted);
    opacity: 0.7;
    margin: 0 0.18rem;
  }
  .chain-side-action {
    display: inline-flex;
    align-items: center;
  }

  /* Chain row tinting — strikes BELOW spot are ITM-call (the call is
     in-the-money); strikes ABOVE spot are ITM-put. Subtle background
     bands so the operator sees the moneyness boundary at a glance.
     ATM row (closest to spot) gets a warm amber highlight + amber
     borders top/bottom — paired with the bolder amber strike numeral
     it identifies the row without taking extra horizontal space. */
  .chain-row-atm {
    background: rgba(251,191,36,0.18);
    border-top:    1px solid var(--algo-amber-border);
    border-bottom: 1px solid var(--algo-amber-border);
  }

  /* Chain header SPOT pill — sits next to the "Option chain" title
     when chainSpot resolves. Same amber palette as the ATM tag so
     the visual link "this number drives that highlighted row" reads
     instantly. */
  .chain-spot-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid var(--algo-amber-border);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
  }
  .chain-btn {
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid currentColor;
    background: transparent;
    cursor: pointer;
    letter-spacing: 0.04em;
    transition: background 0.12s;
  }
  /* Side-by-side BUY (+) / SELL (−) pair per CE/PE cell. */
  .chain-btn-pair {
    display: inline-flex;
    gap: 3px;
  }
  /* BUY (+) — green, SELL (−) — red. Same palette as the outer
     +/− toggle so the operator's eye reads the side without
     parsing the glyph. */
  .chain-btn-buy  { color: var(--c-long); }
  .chain-btn-sell { color: var(--c-short); }
  .chain-btn-buy:hover  { background: var(--algo-green-bg); }
  .chain-btn-sell:hover { background: var(--algo-red-bg); }
  /* Info button — sky-blue, neutral. Opens the full OrderTicket
     pre-filled (advanced path, when the operator wants to edit
     qty / limit price / chase / mode before placing). */
  .chain-btn-info {
    color: var(--algo-sky);
    font-style: italic;
    padding: 1px 5px;
  }
  .chain-btn-info:hover { background: rgba(125,211,252,0.10); }
  /* Watchlist button — amber, sits next to the "i" info button.
     One click adds the contract to the user's default watchlist. */
  .chain-btn-watch {
    color: var(--c-action);
    padding: 1px 5px;
    font-weight: 700;
  }
  .chain-btn-watch:hover { background: rgba(251,191,36,0.10); }
  /* Brief "✓ added" toast that flashes alongside a strike row's
     button cluster (or a futures pill) the moment the operator's
     click landed in the basket. Auto-fades. */
  .chain-quick-toast {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 2px;
    background: rgba(74,222,128,0.18);
    color: var(--c-long);
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.04em;
    margin-left: 0.3rem;
    animation: chain-quick-fade 0.9s ease-out forwards;
  }
  /* Basket bar — pinned below the chain table. Compact; one row of
     leg pills + a Clear / Place pair on the right. Pills wrap on a
     second line if the operator stages more than fits on one row. */
  .chain-basket {
    margin-top: 0.6rem;
    padding: 0.45rem 0.55rem;
    border: 1px solid rgba(251,191,36,0.32);
    border-radius: 3px;
    background: rgba(251,191,36,0.06);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.6rem;
  }
  .chain-basket-legs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    flex: 1 1 60%;
    min-width: 0;
  }
  /* Each chip is the leg's full action surface — click anywhere on
     the chip (except the inline lot stepper) to remove the leg from
     the basket. The chip is colour-coded by SIDE on the OUTLINE
     (BUY=green / SELL=red, matching the strike-row +/- buttons and
     the OrderTicket's Add/Close pills) and by OPTION TYPE via a
     subtle inner left-border accent (CE green / PE red / FUT sky)
     so the operator reads "what side am I taking?" + "is this a
     call / put / future?" at a glance without a separate text tag. */
  .chain-basket-leg {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 1px 6px 1px 4px;
    border-radius: 3px;
    border: 1px solid currentColor;
    border-left-width: 4px;
    font-family: monospace;
    font-size: var(--fs-sm);
    line-height: 1.5;
    cursor: pointer;
    user-select: none;
    transition: background 0.12s, transform 0.05s;
  }
  .chain-basket-leg:focus-visible {
    outline: 2px solid var(--c-action);
    outline-offset: 1px;
  }
  .chain-basket-leg:hover:not(.is-disabled) {
    background: var(--algo-red-bg);
    transform: translateY(-1px);
  }
  .chain-basket-leg:hover:not(.is-disabled) .chain-basket-sym::after {
    content: ' ✕';
    color: var(--c-short);
    margin-left: 0.15rem;
    font-weight: 700;
  }
  .chain-basket-leg.is-disabled {
    cursor: progress;
    opacity: 0.55;
  }
  /* Outline + side text colour by SIDE (chain-btn-buy / -sell green /
     red, same as the strike-row buttons). */
  .chain-basket-leg-buy  { color: var(--c-long); background: var(--algo-green-bg-soft); }
  .chain-basket-leg-sell { color: var(--c-short); background: var(--algo-red-bg-soft); }
  /* Left-border accent by TYPE (CE green / PE red / FUT sky-blue,
     matching the strike header palette + OrderTicket option-type
     pills). */
  .chain-basket-leg-type-ce  { border-left-color: var(--c-long); }
  .chain-basket-leg-type-pe  { border-left-color: var(--c-short); }
  .chain-basket-leg-type-fut { border-left-color: var(--algo-sky); }
  .chain-basket-side {
    font-weight: 800;
    letter-spacing: 0.04em;
  }
  .chain-basket-sym {
    color: var(--algo-slate);
    font-weight: 600;
  }
  .chain-basket-qty {
    color: var(--text-muted);
    font-size: var(--fs-xs);
    opacity: 0.85;
    font-variant-numeric: tabular-nums;
  }
  /* In-pill lot stepper. Same family as `.chain-btn` but slightly
     smaller (basket-pill is itself compact). Coloured with the
     pill's own side-tinted text colour so it reads as part of the
     pill, not a foreign control. */
  .chain-basket-step {
    width: 1.05rem;
    height: 1.05rem;
    padding: 0;
    border-radius: 2px;
    border: 1px solid currentColor;
    background: transparent;
    color: currentColor;
    cursor: pointer;
    font-family: monospace;
    font-size: var(--fs-lg);
    font-weight: 700;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .chain-basket-step:hover:not(:disabled) {
    background: rgba(126,151,184,0.10);
  }
  .chain-basket-step:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-lots {
    min-width: 1.1rem;
    text-align: center;
    color: var(--c-action);
    font-family: monospace;
    font-weight: 700;
    font-size: var(--fs-sm);
    font-variant-numeric: tabular-nums;
  }
  /* Algo-selected limit price — static, not editable. Auto-seeded
     from chain bid/ask at add-time; shows "@MKT" when no quote was
     available so the operator knows that leg routes as MARKET. */
  .chain-basket-limit-static {
    color: var(--c-action);
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.02em;
  }
  /* Per-leg chase aggressiveness pill cluster — `C[L|M|H]` lives
     INSIDE each basket leg pill so every leg carries its own
     aggressiveness. Quick-adds default to L; the operator can flip
     individual legs to M/H without touching siblings. Mirrors the
     OrderTicket / chase L/M/H palette (sky / amber / green). */
  .chain-basket-chase {
    display: inline-flex;
    align-items: center;
    gap: 0.15rem;
    margin-left: 0.15rem;
  }
  .chain-basket-chase-label {
    color: var(--text-muted);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .chain-basket-chase-pill {
    width: 1rem;
    height: 1rem;
    padding: 0;
    border: 1px solid rgba(126,151,184,0.35);
    border-radius: 2px;
    background: transparent;
    color: var(--text-muted);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .chain-basket-chase-pill:hover:not(:disabled):not(.on) {
    background: rgba(126,151,184,0.10);
  }
  .chain-basket-chase-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-chase-pill-low.on  { background: rgba(125,211,252,0.20); color: var(--algo-sky); border-color: var(--algo-sky-border); }
  .chain-basket-chase-pill-med.on  { background: rgba(251,191,36,0.20); color: var(--c-action); border-color: var(--algo-amber-border); }
  .chain-basket-chase-pill-high.on { background: rgba(74,222,128,0.20); color: var(--c-long); border-color: var(--algo-green-border); }
  .chain-basket-actions {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
    flex-wrap: wrap;
  }
  .chain-basket-clear,
  .chain-basket-place {
    height: 1.5rem;
    padding: 0 0.7rem;
    border-radius: 2px;
    border: 1px solid currentColor;
    background: transparent;
    cursor: pointer;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .chain-basket-clear { color: var(--text-muted); }
  .chain-basket-clear:hover { background: rgba(163,185,208,0.08); }
  .chain-basket-place { color: var(--c-action); background: rgba(251,191,36,0.10); }
  .chain-basket-place:hover    { background: rgba(251,191,36,0.20); }
  .chain-basket-place:disabled,
  .chain-basket-clear:disabled { opacity: 0.55; cursor: progress; }
  .chain-basket-err {
    flex: 1 1 100%;
    color: var(--c-short);
    font-family: monospace;
    font-size: var(--fs-sm);
    margin-top: 0.2rem;
  }
  .chain-basket-toast {
    margin-top: 0.5rem;
    padding: 0.3rem 0.5rem;
    border-radius: 2px;
    background: rgba(74,222,128,0.14);
    color: var(--c-long);
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    text-align: center;
    animation: chain-quick-fade 2.2s ease-out forwards;
  }

  @keyframes chain-quick-fade {
    0%   { opacity: 1; }
    70%  { opacity: 1; }
    100% { opacity: 0; }
  }
  /* Lots input — match Select trigger height + border so the
     control bar reads as one consistent row. */
  .chain-lots-input {
    width: 100%;
    min-height: 1.55rem;
    padding: 0 6px;
    border-radius: 3px;
    border: 1px solid rgba(126,151,184,0.35);
    background: rgba(13,21,38,0.6);
    color: var(--algo-slate);
    font-family: monospace;
    font-size: var(--fs-md);
    box-sizing: border-box;
  }
  .chain-lots-input:focus {
    outline: none;
    border-color: var(--algo-amber-border);
    background: rgba(13,21,38,0.8);
  }
  /* "chain" source pill on legs added via the chain picker — sky-blue
     to distinguish from manual / live / sim. */
  .leg-source-chain { color: #c084fc; }

  /* ── Order-completion toast stack ─────────────────────────────────
     Fixed top-right, pointer-events: none on the wrapper so the
     chain + drafts behind the toasts stay clickable. Individual
     toasts are pointer-events: auto so × can be clicked.
     Slide-in from the right + auto-dismiss after 5 s. */
  .order-toast-stack {
    position: fixed;
    top: 4.5rem;     /* clears the navbar + any mode banners */
    right: 1rem;
    z-index: 50;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    pointer-events: none;
    max-width: 22rem;
  }
  @media (max-width: 720px) {
    .order-toast-stack {
      top: 4rem;
      right: 0.5rem;
      left: 0.5rem;
      max-width: none;
    }
  }
  .order-toast {
    pointer-events: auto;
    background: rgba(13,21,38,0.96);
    border: 1px solid rgba(167,139,250,0.55);
    border-left: 3px solid #a78bfa;
    border-radius: 0.35rem;
    padding: 0.45rem 0.65rem 0.4rem;
    box-shadow: 0 6px 18px rgba(0,0,0,0.45);
    font-family: var(--font-numeric);
    color: #e5edf7;
    animation: order-toast-in 180ms ease-out;
  }
  @keyframes order-toast-in {
    from { transform: translateX(120%); opacity: 0; }
    to   { transform: translateX(0);    opacity: 1; }
  }
  /* Per-status accent — overrides the default violet for filled /
     unfilled / rejected outcomes so the operator catches errors at
     a glance without reading the text. */
  .order-toast-filled   { border-left-color: var(--c-long); border-color: var(--algo-green-border); }
  .order-toast-unfilled { border-left-color: var(--c-short); border-color: var(--algo-red-border); }
  .order-toast-rejected { border-left-color: var(--c-short); border-color: var(--algo-red-border); }

  .order-toast-head {
    display: flex; align-items: center; gap: 0.45rem;
    font-size: var(--fs-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-slate);
    opacity: 0.85;
    margin-bottom: 0.25rem;
  }
  .order-toast-mode {
    padding: 0.05rem 0.4rem;
    border-radius: 0.2rem;
    font-weight: 700;
    background: rgba(167,139,250,0.14);
    color: #a78bfa;
    border: 1px solid rgba(167,139,250,0.55);
  }
  .order-toast-mode-paper { background: rgba(56,189,248,0.18);  color: var(--algo-sky); border-color: rgba(56,189,248,0.4); }
  .order-toast-mode-live  { background: rgba(248,113,113,0.18); color: #fca5a5; border-color: rgba(248,113,113,0.5); }
  /* FILL toast — emerald, fired when a position_filled ws event lands.
     Either replaces an in-flight placement toast (updates its status to
     FILLED in place) or pushes fresh when the fill came from an algo
     path the operator didn't manually place. */
  .order-toast-mode-fill  { background: rgba(74,222,128,0.18);  color: var(--c-long); border-color: rgba(74,222,128,0.5); }
  .order-toast-status { font-weight: 600; }
  .order-toast-close {
    margin-left: auto;
    width: 1.2rem; height: 1.2rem;
    background: transparent;
    border: 0;
    color: rgba(200,216,240,0.7);
    cursor: pointer;
    font-size: var(--fs-xl);
    line-height: 1;
    border-radius: 0.2rem;
    padding: 0;
  }
  .order-toast-close:hover { color: #fff; background: rgba(255,255,255,0.08); }

  .order-toast-body {
    display: flex; align-items: baseline; gap: 0.4rem;
    font-size: var(--fs-lg);
    font-weight: 700;
  }
  .order-toast-side-buy  { color: var(--c-long); }
  .order-toast-side-sell { color: var(--c-short); }
  .order-toast-qty { color: var(--c-action); }
  .order-toast-sym { color: #e5edf7; }
  .order-toast-px  { color: var(--algo-slate); opacity: 0.9; }

  .order-toast-foot {
    margin-top: 0.2rem;
    font-size: var(--fs-xs);
    color: rgba(200,216,240,0.55);
  }
  .order-toast-oid { font-family: var(--font-numeric); }
  @media (prefers-reduced-motion: reduce) {
    .chain-quick-toast { animation: none; }
    .chain-basket-toast { animation: none; }
    .order-toast { animation: none; }
  }

  /* ── Payoff fullscreen chrome — align to ChartModal canonical ────────────
     When the payoff card is in fullscreen (.fs-card-on already supplies the
     amber ring box-shadow via app.css:1785-1786) we swap the outer border to
     the same amber 1px as .canonical-modal-panel (app.css:445), and give the
     header section the same cyan gradient bg + cyan title as .cm-header in
     ChartModal.svelte.  Non-fullscreen payoff card keeps its default
     .algo-status-card white-alpha border + amber opt-section-h underline. */

  /* 1. Outer border — matches .canonical-modal-panel border exactly. */
  :global(.opt-payoff.fs-card-on) {
    border: 1px solid rgba(251, 191, 36, 0.40) !important;
  }

  /* 2. Header accent — scoped to payoff fullscreen only.
        Gradient bg + cyan title match .cm-header; hairline border-bottom
        matches app.css comment intent (low-alpha separator). */
  :global(.opt-payoff.fs-card-on) .opt-section-h {
    background: linear-gradient(180deg,
                  rgba(34, 211, 238, 0.18) 0%,
                  rgba(34, 211, 238, 0.06) 100%);
    color: #67e8f9;
    border-bottom-color: rgba(34, 211, 238, 0.40);
  }
  .byund-chg-sep {
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  .cand-chg-sep-hdr {
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  /* Strip cell-level green/red bg from totals — amber container is the signal. */
  .cand-row.cand-row-total .cand-pnl.cell-pos,
  .cand-row.cand-row-total .cand-pnl.cell-neg { background-color: transparent !important; }
</style>
