<script>
  // Options Analytics dashboard (/admin/options).
  //
  // Multi-leg options analytics workspace; auto-detects live vs sim from
  // `/api/simulator/status`. Payoff diagram, Greeks, risk metrics, POP,
  // EV. Strategy analytics auto-refreshes whenever the leg set changes.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, nowStamp, marketAwareInterval, visibleInterval } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import { isMarketOpen } from '$lib/marketHours';
  import { createPerformanceSocket } from '$lib/ws';
  import {
    fetchPositions, fetchSimStatus, fetchStrategyAnalytics,
    fetchAccounts, fetchOptionsSpot, fetchChainQuotes,
    placeTicketOrder, fetchLiveStatus,
    fetchWatchlists, addWatchlistItem,
  } from '$lib/api';
  import OptionsPayoff from '$lib/OptionsPayoff.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import Select        from '$lib/Select.svelte';
  import MultiSelect   from '$lib/MultiSelect.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import InfoHint      from '$lib/InfoHint.svelte';
  import CollapseButton  from '$lib/CollapseButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listStrikes, findOption,
    listFutures, getInstrument,
  } from '$lib/data/instruments';
  import { decomposeSymbol, formatSymbol } from '$lib/data/decomposeSymbol';
  import { acctColor } from '$lib/account';
  import { POPULAR_UNDERLYINGS } from '$lib/data/popularUnderlyings';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';
  import ChartModal from '$lib/ChartModal.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import { longPress } from '$lib/actions/longPress.js';

  // Row-level chart modal for Candidates panel rows.
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  function _openChart(/** @type {string} */ symbol, /** @type {string} */ exchange = '') {
    _chartModalSym  = String(symbol  || '').toUpperCase();
    _chartModalExch = String(exchange || '');
  }

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
  let loading       = $state(false);
  let teardown;
  let posTeardown;
  let simTeardown;
  let wsTeardown;

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

  // Non-blocking completion toast stack. Pushed by onTicketSubmit when
  // a paper/live order lands; rendered as fixed-position cards at the
  // top-right corner so the chain + drafts table behind them stay
  // fully interactive (operator can place the next order without
  // dismissing). Auto-dismiss after 5 s; click × to clear early.
  /** @type {Array<{id:number, mode:string, side:string, qty:number, symbol:string, price:string, orderId:string, status:string, ts:number}>} */
  let _orderToasts = $state([]);
  let _orderToastSeq = 0;
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
    setTimeout(() => {
      _orderToasts = _orderToasts.filter(t => t.id !== toast.id);
    }, 5000);
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
    const tid = next[idx].id;
    setTimeout(() => {
      _orderToasts = _orderToasts.filter(t => t.id !== tid);
    }, 5000);
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
    setTimeout(() => {
      _orderToasts = _orderToasts.filter(t => t.id !== toast.id);
    }, 5000);
  }
  function addDraft() {
    drafts = [...drafts, { id: ++_draftSeq, symbol: '', qty: '', avg_cost: '', ltp: '' }];
  }
  function removeDraft(/** @type {number} */ id) {
    drafts = drafts.filter(d => d.id !== id);
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
  /** @type {Record<string, boolean>} `${account}|${symbol}` → enabled flag.
   * Composite key so the same option symbol in two broker accounts gets
   * an independent checkbox + isn't double-counted in P&L. */
  let enabledSymbols = $state({});
  // Composite key for the enabledSymbols map. Plain symbol collided
  // across accounts: a NIFTY24DEC25000PE held in both ZG#### and
  // ZJ#### would share one checkbox, and the candidatesActualPnl
  // counter summed both rows even though only one was "checked".
  function enKey(c) { return `${c.account || ''}|${c.symbol || ''}`; }

  // Legs sent to the strategy endpoint — built from candidate positions
  // (live or sim, depending on simActive) plus drafts that match the
  // selected underlying, intersected with the operator's checked rows
  // in the Candidates panel.
  /** @type {Array<{symbol:string, qty:any, avg_cost:any, ltp:any, source:string}>} */
  let legs = $state([]);

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
  let _fsPayoff  = $state(false);
  let _fsLegs    = $state(false);

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
  const BAND_ORDER = { close: 0, netted: 1, otm: 2 };

  const expiryCloseAnalysis = $derived.by(() => {
    // Each array now carries rows with _band ∈ {'close','netted','otm'}
    // plus _pairId (shared by both members of a netted pair),
    // _closeId (unique to each to-close row), and _reason.
    /** @type {{equity:any[], commodity:any[]}} */
    const result = { equity: [], commodity: [] };
    const spot = Number(strategy?.spot || 0);
    if (!spot || !candidatePositions.length) return result;

    // First pass — annotate every option candidate with parsed
    // metadata, ITM verdict, and theta from the strategy analytics.
    // Segment is derived from the underlying name (MCX list above),
    // not the row's exchange field — GOLDM positions sometimes
    // arrive with empty / NSE exchange and were getting tagged as
    // equity before this fix. Skip futures, zero-qty, and drafts.
    const annotated = [];
    for (const c of candidatePositions) {
      const qty = Number(c.qty || 0);
      if (qty === 0) continue;
      if (c.source === 'draft') continue;
      const inst = getInstrument(String(c.symbol || '').toUpperCase());
      if (!inst) continue;
      const optType = inst.t;
      if (optType !== 'CE' && optType !== 'PE') continue;
      const strike = Number(inst.k || 0);
      if (!strike) continue;
      const underlying = String(inst.u || '').toUpperCase();
      const expiry = String(inst.x || '');
      const segment = _MCX_UNDERLYINGS.has(underlying) ? 'commodity' : 'equity';
      const isITM = optType === 'CE' ? spot > strike : spot < strike;
      const lg = legAnalyticsBySymbol[c.symbol];
      const theta = Number(lg?.greeks?.theta ?? 0) || 0;
      const otmDist = isITM ? 0
        : (optType === 'CE' ? strike - spot : spot - strike);
      annotated.push({
        ...c,
        _strike: strike,
        _underlying: underlying,
        _expiry: expiry,
        _optType: optType,
        _segment: segment,
        _isITM: isITM,
        _spot: spot,
        _qty: qty,
        _theta: theta,
        _otmDist: otmDist,
      });
    }

    // Equity segment — ITM → close, OTM → otm band.
    // No hedge exception on NFO; each contract settles independently.
    let _eqCloseCounter = 0;
    for (const r of annotated) {
      if (r._segment !== 'equity') continue;
      if (r._isITM) {
        _eqCloseCounter++;
        result.equity.push({
          ...r,
          _band: 'close',
          _closeId: `C${_eqCloseCounter}`,
          _reason: 'ITM equity — physical settlement risk',
        });
      } else {
        result.equity.push({
          ...r,
          _band: 'otm',
          _reason: `OTM by ₹${Math.round(r._otmDist).toLocaleString('en-IN')}`,
        });
      }
    }

    // Commodity segment — greedy theta-priority netting, scoped per
    // (account, underlying, expiry). Same four pair rules preserved.
    //
    // Output enrichment vs previous version:
    //   • Fully netted pairs → _band='netted', shared _pairId
    //   • Partial cancels → consumed slice goes to 'netted' with
    //     _pairId + _splitNote; remaining qty stays in the map and
    //     may form another pair or land as residual
    //   • Residual (non-zero after all greedy attempts) → _band='close'
    //   • Non-ITM commodity positions → _band='otm'
    //
    // Theta-priority direction is UNCHANGED: high |theta| paired
    // first → low |theta| as residual = to-close. This matches the
    // "close the positions that are costing you the most time-value"
    // logic in the original algorithm.
    function _canPair(A, B, remaining) {
      const aq = remaining.get(A) || 0;
      const bq = remaining.get(B) || 0;
      if (aq === 0 || bq === 0) return false;
      const aSign = Math.sign(aq);
      const bSign = Math.sign(bq);
      // Rule 1 + 2: same opt type, opposite sign
      if (A._optType === B._optType && aSign !== bSign) return true;
      // Rule 3 + 4: different opt type, same sign
      if (A._optType !== B._optType && aSign === bSign) return true;
      return false;
    }

    // Emit OTM commodity positions first.
    for (const r of annotated) {
      if (r._segment !== 'commodity' || r._isITM) continue;
      result.commodity.push({
        ...r,
        _band: 'otm',
        _reason: `OTM by ₹${Math.round(r._otmDist).toLocaleString('en-IN')}`,
      });
    }

    // Group ITM commodity positions by (account, underlying, expiry).
    /** @type {Record<string, any[]>} */
    const groups = {};
    for (const r of annotated) {
      if (r._segment !== 'commodity' || !r._isITM) continue;
      const key = `${r.account || ''}|${r._underlying}|${r._expiry}`;
      (groups[key] ??= []).push(r);
    }

    for (const key of Object.keys(groups)) {
      const grp = groups[key];
      const sortedAbs = grp.slice().sort(
        (a, b) => Math.abs(b._theta || 0) - Math.abs(a._theta || 0));

      // remaining tracks signed qty consumed through netting.
      const remaining = new Map();
      for (const r of sortedAbs) remaining.set(r, r._qty);

      // nettedRows accumulates {row, consumedQty, pairId, splitNote}
      // so we can emit them all after the greedy pass.
      /** @type {Array<{row:any, consumedQty:number, pairId:string, splitNote:string}>} */
      const nettedRows = [];
      let pairCounter = 0;

      for (const A of sortedAbs) {
        let aq = remaining.get(A) || 0;
        while (aq !== 0) {
          // Pick the highest-|theta| valid partner remaining.
          let bestB = null;
          let bestT = -1;
          for (const B of sortedAbs) {
            if (B === A) continue;
            if (!_canPair(A, B, remaining)) continue;
            const t = Math.abs(B._theta || 0);
            if (t > bestT) { bestB = B; bestT = t; }
          }
          if (!bestB) break;
          pairCounter++;
          const pairId = `N${pairCounter}`;
          const bq = remaining.get(bestB) || 0;
          const netAmt = Math.min(Math.abs(aq), Math.abs(bq));
          const newAq = aq - netAmt * Math.sign(aq);
          const newBq = bq - netAmt * Math.sign(bq);
          remaining.set(A, newAq);
          remaining.set(bestB, newBq);
          // Record consumed slices for both members of the pair.
          const aSplit = newAq !== 0;
          const bSplit = newBq !== 0;
          nettedRows.push({
            row: A,
            consumedQty: netAmt * Math.sign(aq),
            pairId,
            splitNote: aSplit ? `split ${aq > 0 ? '+' : ''}${aq}→${netAmt * Math.sign(aq)}` : '',
          });
          nettedRows.push({
            row: bestB,
            consumedQty: netAmt * Math.sign(bq),
            pairId,
            splitNote: bSplit ? `split ${bq > 0 ? '+' : ''}${bq}→${netAmt * Math.sign(bq)}` : '',
          });
          aq = remaining.get(A) || 0;
        }
      }

      // Emit netted rows.
      for (const { row, consumedQty, pairId, splitNote } of nettedRows) {
        result.commodity.push({
          ...row,
          _band: 'netted',
          _pairId: pairId,
          _residualQty: consumedQty,
          _reason: splitNote
            ? `Netted (${splitNote})`
            : `Netted — broker settles at expiry`,
        });
      }

      // Residuals → close band.
      let closeCounter = 0;
      for (const r of sortedAbs) {
        const q = remaining.get(r) || 0;
        if (q === 0) continue;
        closeCounter++;
        result.commodity.push({
          ...r,
          _band: 'close',
          _closeId: `C${closeCounter}`,
          _residualQty: q,
          _reason: `Unhedged ITM commodity (residual qty ${q > 0 ? '+' : ''}${q})`,
        });
      }
    }

    // Final display sort per band — account ASC, then symbol ASC.
    const acctSymSort = (a, b) => {
      const ac = String(a.account || '').localeCompare(String(b.account || ''));
      if (ac !== 0) return ac;
      return String(a.symbol || '').localeCompare(String(b.symbol || ''));
    };
    // Sort within each band independently so band order is preserved
    // in rendering (we'll render close → netted → otm).
    result.equity.sort((a, b) => {
      const bo = (BAND_ORDER[a._band] ?? 9) - (BAND_ORDER[b._band] ?? 9);
      if (bo !== 0) return bo;
      // Within NETTED band, sort by pair id so paired rows sit adjacent.
      if (a._band === 'netted' && b._band === 'netted') {
        const ap = a._pairId || '';
        const bp = b._pairId || '';
        if (ap !== bp) return ap < bp ? -1 : 1;
      }
      return acctSymSort(a, b);
    });
    result.commodity.sort((a, b) => {
      const bo = (BAND_ORDER[a._band] ?? 9) - (BAND_ORDER[b._band] ?? 9);
      if (bo !== 0) return bo;
      // Within NETTED band, sort by pair id so paired rows sit
      // adjacent — operator: "in netted, show the opposite
      // positions together color coded". N1-A, N1-B, N2-A, N2-B …
      if (a._band === 'netted' && b._band === 'netted') {
        const ap = a._pairId || '';
        const bp = b._pairId || '';
        if (ap !== bp) return ap < bp ? -1 : 1;
      }
      return acctSymSort(a, b);
    });

    // Tag each NETTED row with a `_pairTint` color-cycle index so
    // the renderer can apply one of N alternating background tints
    // per pair — operator: "you can alternate color for each
    // netted opposite positions". Numbered 0..4 cycling so two
    // adjacent pairs always read as distinct.
    const _assignPairTint = (arr) => {
      const map = new Map();
      let cycle = 0;
      for (const r of arr) {
        if (r._band !== 'netted') continue;
        const pid = r._pairId || '';
        if (!pid) continue;
        if (!map.has(pid)) {
          map.set(pid, cycle % 5);
          cycle++;
        }
        r._pairTint = map.get(pid);
      }
    };
    _assignPairTint(result.commodity);
    _assignPairTint(result.equity);

    return result;
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
  const displayedCandidates = $derived.by(() => {
    if (legsTab !== 'expiry') return candidatePositions;
    const out = [];
    for (const r of expiryCloseAnalysis.equity)
      out.push({ ...r, _expiryStatus: `equity-${r._band}` });
    for (const r of expiryCloseAnalysis.commodity)
      out.push({ ...r, _expiryStatus: `commodity-${r._band}` });
    return out;
  });

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
    return Array.from(accts).sort();
  });
  const underlyingChoicesFromBook = $derived.by(() => {
    const set = new Set();
    for (const p of positions) {
      if (!/(CE|PE|FUT)$/i.test(p.symbol)) continue;
      // Strip everything from the first digit on — that's where the
      // YY-month-strike block starts. Works for monthly + weekly.
      // Each MCX variant (CRUDEOIL vs CRUDEOILM, GOLD vs GOLDM vs
      // GOLDPETAL vs GOLDTEN vs GOLDGUINEA, etc.) keeps its own
      // entry in the dropdown — they're separately tradable contracts
      // with different lot sizes / tick sizes and need their own
      // payoff chart. Operator: "for goldm positions, it should show
      // goldm in dropdown. not gold. otherwise it may conflict in
      // future with gold options".
      const u = p.symbol.replace(/\d.*$/, '');
      if (!u) continue;
      set.add(u);
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
      selectedAccounts = [...cur].sort();
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
   *  bare ticker — that IS the index spot. Same migration the operator
   *  ran on Pinned watchlist (MCX/CDS root → actual future) for
   *  consistency across the app.
   *
   *  The VALUE stays as the bare root so every downstream filter
   *  (expiryChoicesForUnderlying, candidatePositions prefix match,
   *  strategy analytics) continues to match every option / future on
   *  that root — picking "CRUDEOIL25JUNFUT" still surfaces CRUDEOIL25JUN
   *  + CRUDEOIL25JUL + CRUDEOIL25AUG option chains under the Expiry
   *  picker. */
  const underlyingOptionsForPicker = $derived.by(() => {
    // Dedupe by value. Label is the root (GOLDM, NIFTY, CRUDEOIL
    // etc.) — same identity the ChartWorkspace title shows.
    // The previous separate "front-month contract" chip beside the
    // picker was removed per operator request; the contract month
    // in scope is now read off the expiry picker + legs grid.
    const seen = new Set();
    const out = [];
    for (const u of underlyingChoicesFromBook) {
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ value: u, label: u });
    }
    return out;
  });

  // Auto-select the first underlying from the loaded book when the
  // page lands without a cached selection. Saves the operator a click;
  // the page is essentially useless without an underlying picked, so
  // defaulting feels right. Untrack the read of selectedUnderlying so
  // operator-driven changes don't re-trigger this.
  $effect(() => {
    void underlyingChoicesFromBook;
    untrack(() => {
      const list = underlyingChoicesFromBook;
      if (!selectedUnderlying && list.length) {
        selectedUnderlying = list[0];
      }
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
      if (u && _lastUnderlyingForChart && u !== _lastUnderlyingForChart) {
        strategy = null;
      }
      _lastUnderlyingForChart = u;
    });
  });

  /** Distinct expiries (YYYY-MM-DD) available on the chosen
   *  underlying — derived by looking up each loaded position's
   *  symbol in the instruments cache. Drafts contribute too. */
  const expiryChoicesForUnderlying = $derived.by(() => {
    if (!instrumentsReady || !selectedUnderlying) return [];
    const target = selectedUnderlying.toUpperCase();
    // Construct the symbol-prefix regex once per re-derivation; the
    // closure below would otherwise rebuild it for every position/draft.
    const prefixRe = new RegExp(`^${target}\\d`, 'i');
    const set = new Set();
    const consider = /** @param {string} sym */ (sym) => {
      const upper = String(sym || '').toUpperCase();
      if (!upper || !prefixRe.test(upper)) return;
      const inst = getInstrument(upper);
      if (inst?.x) set.add(inst.x);
    };
    for (const p of positions) consider(p.symbol);
    for (const d of drafts)    consider(d.symbol);
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
  /** @type {{symbol:string,account:string,qty:number,avg_cost:number|null,ltp:number|null,prev_close?:number|null,pnl?:number,realised?:number,day_change_val?:number,source:string,kind:string,exchange?:string,draftId?:number,_expiryStatus?:string}[]} */
  const candidatePositions = $derived.by(() => {
    if (!selectedUnderlying) return [];
    const target = selectedUnderlying.toUpperCase();
    /** @type {string[]} */
    const acctFilter = selectedAccounts.length ? selectedAccounts : [];
    // Hoisted regexes — constructed once per re-derivation rather than
    // once per position/draft. The literal /FUT$/ and /(CE|PE)$/ are
    // already cached at parse time; the dynamic prefix regex is the
    // only one that needs hoisting.
    const prefixRe = new RegExp(`^${target}\\d`, 'i');
    /** @type {any[]} */
    const out = [];
    // Source filter — when a sim is active, work off the sim book only;
    // otherwise the live book. Drafts are always visible regardless.
    const wantedSource = simActive ? 'sim' : 'live';
    const matchExpiry = /** @param {string} sym */ (sym) => {
      if (!selectedExpiries.length) return true;
      const inst = getInstrument(String(sym || '').toUpperCase());
      return selectedExpiries.includes(inst?.x);
    };
    for (const p of positions) {
      if (p.source !== wantedSource) continue;
      if (acctFilter.length && !acctFilter.includes(p.account)) continue;
      const sym = p.symbol;
      if (!prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      // Closed positions (qty=0) bypass the expiry filter. Their
      // contract often expired in the past, OR the instruments cache
      // has already evicted them — so matchExpiry would always reject
      // them and the row would silently disappear from the legs panel
      // even though it still shows in the dashboard positions grid.
      // Operator wanted parity with the grid: show every closed
      // position regardless of selected expiry.
      const isClosed = Number(p.qty || 0) === 0;
      if (!isClosed && !matchExpiry(sym)) continue;
      out.push({
        ...p,
        kind: isFut ? 'fut' : 'opt',
      });
    }
    // Drafts — matched by symbol prefix; no account filter (drafts
    // aren't tied to a broker account).
    for (const d of drafts) {
      const sym = String(d.symbol || '').toUpperCase();
      if (!sym || !prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      if (!matchExpiry(sym)) continue;
      const qty = d.qty === '' || d.qty == null ? 0 : Number(d.qty);
      const cost = d.avg_cost === '' || d.avg_cost == null ? null : Number(d.avg_cost);
      const ltp  = d.ltp      === '' || d.ltp      == null ? null : Number(d.ltp);
      out.push({
        symbol: sym,
        account: '',
        qty,
        avg_cost: cost,
        ltp,
        source: 'draft',
        kind: isFut ? 'fut' : 'opt',
        draftId: d.id,
      });
    }
    // Closed positions (qty=0 — Kite returns these so realised P/L
    // stays visible) sort to the END of the legs list. Live exposure
    // first, history last; stable order otherwise.
    out.sort((a, b) => {
      const ac = (Number(a?.qty || 0) === 0) ? 1 : 0;
      const bc = (Number(b?.qty || 0) === 0) ? 1 : 0;
      return ac - bc;
    });
    return out;
  });

  // Sum of broker P&L across CHECKED candidates only. The chart's
  // payoff curve is built from `legs` (also filtered by enabled
  // checkboxes), so the alignment target must use the same subset.
  // Earlier this summed every candidate — unchecking a leg dropped
  // it from the curve but kept it in the offset, so the chart's
  // TDAY didn't change visually. Now they stay in lock-step.
  const candidatesActualPnl = $derived.by(() => {
    let s = 0;
    for (const c of candidatePositions) {
      if (enabledSymbols[enKey(c)] === false) continue;
      s += Number(c.pnl || 0);
    }
    return s;
  });

  // Sum of day_change_val across enabled candidates. Surfaced as the
  // DAY row in the OptionsPayoff overlay so the operator can reconcile
  // it with the PositionStrip's P∆ chip (= sum of day_change_val
  // across the ENTIRE position book). Subset relationship: if every
  // position in the strip belongs to the chart's underlying AND every
  // candidate is enabled, DAY equals the strip's P∆ exactly. Useful
  // for "is my chart showing what the strip says?" sanity checks.
  const candidatesDayPnl = $derived.by(() => {
    let s = 0;
    for (const c of candidatePositions) {
      if (enabledSymbols[enKey(c)] === false) continue;
      s += Number(c.day_change_val || 0);
    }
    return s;
  });

  // Master "select all" plumbing for the Legs panel header checkbox.
  // allCandidatesOn = true when every candidate is enabled in the
  // map; false when some are off. The DOM element ref drives the
  // tri-state visual: checked / unchecked / indeterminate (some on,
  // some off) — set via $effect so the indeterminate flag reflects
  // the live state without us having to manually clear it on toggle.
  let allCandidatesEl = $state(/** @type {HTMLInputElement|null} */ (null));
  const allCandidatesOn = $derived.by(() => {
    if (!candidatePositions.length) return false;
    return candidatePositions.every(c => enabledSymbols[enKey(c)] !== false);
  });
  const someCandidatesOn = $derived.by(() => {
    if (!candidatePositions.length) return false;
    return candidatePositions.some(c => enabledSymbols[enKey(c)] !== false);
  });
  $effect(() => {
    if (!allCandidatesEl) return;
    // Indeterminate iff some-but-not-all are on. Browser doesn't
    // accept this as an attribute; only JS property writes work.
    allCandidatesEl.indeterminate = someCandidatesOn && !allCandidatesOn;
  });
  function toggleAllCandidates() {
    // Flip toward the opposite of the current "all-on" state. Builds
    // a fresh map so Svelte 5 picks up the change reactively.
    /** @type {Record<string, boolean>} */
    const next = {};
    const target = !allCandidatesOn;
    for (const c of candidatePositions) next[enKey(c)] = target;
    enabledSymbols = next;
  }

  // Backend's BS-theoretical TDAY at the current spot — the value
  // the chart would render WITHOUT any offset. We pick the payoff
  // point nearest strategy.spot from the unshifted curve.
  const chartTheoreticalAtSpot = $derived.by(() => {
    const arr = strategy?.payoff;
    if (!arr || arr.length === 0) return 0;
    const targetSpot = Number(strategy?.spot || 0);
    let best = arr[0];
    let bestDiff = Math.abs(best.spot - targetSpot);
    for (const p of arr) {
      const d = Math.abs(p.spot - targetSpot);
      if (d < bestDiff) { best = p; bestDiff = d; }
    }
    return Number(best?.today_value || 0);
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
  $effect(() => {
    void candidatePositions; void enabledSymbols;
    untrack(() => {
      legs = candidatePositions
        .filter(c => enabledSymbols[enKey(c)] !== false)
        .map(c => ({
          symbol:   c.symbol,
          qty:      c.qty,
          avg_cost: c.avg_cost ?? '',
          ltp:      c.ltp ?? '',
          source:   c.source,
        }));
    });
  });

  // Auto-trigger strategy analytics whenever the leg set changes — no
  // explicit Analyze button needed.
  $effect(() => {
    void legs;
    untrack(() => loadStrategy());
  });

  // ── Option-chain picker (Strategy mode) ───────────────────────────
  // Lets the operator browse strikes for a given underlying + expiry
  // and add legs by clicking CE / PE buttons next to each strike. Pulls
  // the contract universe from the instruments cache (already loaded
  // for /console autocomplete) — no extra API round-trips.
  let instrumentsReady = $state(false);
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
    // 1. Currently-selected underlying — top of the list.
    push(selectedUnderlying);
    // 2. Underlyings the operator already holds.
    for (const p of positions) {
      push(String(p.symbol || '').replace(/\d.*$/, ''));
    }
    // 3. Common indices + MCX commodities.
    for (const u of _COMMON_INDICES_AND_COMMODITIES) push(u);
    // 4. Everything else from the instruments cache — the full
    // universe (Kite dump has ~5k unique underlyings, well under
    // any sane bound) so a typed substring can match anything,
    // not just the alphabetical-first-1000 slice.
    for (const u of suggestUnderlyings('', 100000)) push(u);
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
  function _refreshChainQuotes() {
    if (!showAddPanel || !chainUnderlying || !chainExpiry) return;
    // Bid/ask quotes are static when both NSE + MCX are closed —
    // skip the poll outside market hours so the chain panel doesn't
    // hammer /api/options/chain-quotes overnight. The first call on
    // panel-open still runs (good for end-of-day book inspection).
    if (!isMarketOpen()) return;
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
      _refreshChainQuotes();
      chainQuotesPoll = visibleInterval(_refreshChainQuotes, 5000);
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
    // Prefer the operator's filter (single account picked in the
    // multiselect) when it's a real value. Then a single available
    // real account. Then the masked-but-only choice. Finally fall
    // back to the FIRST real account so the operator can drop legs
    // without needing to open the account picker first — previously
    // the empty fallback caused the chain Buy/Sell buttons to fail
    // silently (addOptionToBasket has an early `if (!_account)
    // return`, no toast). Operator can still switch accounts via
    // the picker inside the modal.
    if (selectedAccounts.length === 1 && _isRealAccount(selectedAccounts[0])) {
      return selectedAccounts[0];
    }
    if (realAccounts.length === 1) return realAccounts[0];
    if (accountChoices.length === 1 && _isRealAccount(accountChoices[0])) {
      return String(accountChoices[0]);
    }
    if (realAccounts.length > 0) return realAccounts[0];
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
  function closePosition(/** @type {any} */ c) {
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
    const inst = getInstrument(String(c.symbol).toUpperCase());
    const lot  = Number(inst?.ls || 1);
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
      defaultMode:    'draft',
      availableModes: ['draft', 'live'],
      // Stashed for onTicketSubmit; OrderTicket ignores extra
      // fields, so this rides through unchanged.
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
  /** @type {Array<{symbol:string, account:string, qty:number, source:string, avg_cost:number|null, ltp:number|null}>} */
  let positions = $state([]);

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
  /** Per-strike|optType toast confirming the watchlist add. Keyed
   *  by `${strike}|${CE|PE}` so each chain row tracks its own. */
  let watchToast = $state(/** @type {{ key: string, msg: string } | null} */ (null));

  async function loadRealAccounts() {
    try {
      const r = await fetchAccounts();
      const list = (r?.accounts || [])
        .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
        .filter(Boolean);
      realAccounts = list;
    } catch (_) {
      // 401 / 403 (anonymous demo on prod) — keep realAccounts
      // empty; the ticket falls back to the masked accountChoices.
      realAccounts = [];
    }
  }

  async function loadDefaultWatchlist() {
    try {
      const lists = await fetchWatchlists();
      const def = (lists || []).find(/** @param {any} l */ (l) => l?.is_default)
                ?? (lists || [])[0];
      if (def) defaultWatchlistId = Number(def.id);
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

  /** Chain picker is gated on having exactly ONE real (un-masked)
   *  account selected at the page level. Reasoning:
   *   - Every leg added through the chain goes straight to OrderTicket
   *     for placement, and an order needs exactly one routing account.
   *   - With zero accounts picked, the picker would default to "all
   *     accounts" which is meaningless for placement.
   *   - With multiple accounts picked, the picker has no way to know
   *     which account a basket leg should land on — operator could
   *     pick the wrong one in the ticket and split a strategy across
   *     accounts by accident.
   *  Account selection now lives inside the SymbolPanel's order
   *  panel — the Chain button on this page bar is always enabled,
   *  and the operator picks the routable account from the modal. */

  // Surface a banner when /api/positions fails so the operator sees
  // WHY the page is empty (instead of an opaque "no candidates" hint).
  let positionsLoadErr = $state('');

  /**
   * Split a broker-consolidated position into separate display rows
   * when it had intraday close/reopen activity. Operator: "if positions
   * are closed and the same positions are opened same or different qty,
   * you have to show them as different rows".
   *
   * Trigger (Variant 1, with partial-reduction): `overnight ≠ 0` AND
   * (`day_buy > 0` OR `day_sell > 0`).
   *
   * The split produces:
   *   • Closed row    — qty = 0, P&L = realised on the portion of the
   *                     overnight position that was closed today.
   *                     Realised = (today's exit price − yesterday's
   *                     close) × closed_qty.
   *   • Open row      — qty = current_qty (signed), P&L = unrealised
   *                     on what's currently open, day_change_val = the
   *                     total leg P∆ minus the closed-row realised.
   *
   * Sum of the two rows' Day P&L Δ equals the original total day
   * change so the TOTAL row at the bottom still reconciles to the
   * strip's P∆ chip. The closed row keeps the original symbol /
   * account so the operator can still scan + filter; a small "closed"
   * tag on the row label distinguishes it visually.
   */
  function splitClosedReopened(/** @type {any} */ p) {
    const oq  = Number(p.overnight_quantity || 0);
    const dbq = Number(p.day_buy_quantity   || 0);
    const dsq = Number(p.day_sell_quantity  || 0);
    const dbv = Number(p.day_buy_value      || 0);
    const dsv = Number(p.day_sell_value     || 0);
    const close = Number(p.prev_close ?? 0);
    // No split when overnight was zero (pure intraday open or fresh
    // round-trip — already a single semantic event) or there were no
    // intraday trades (pure overnight hold).
    if (oq === 0 || (dbq === 0 && dsq === 0)) return [p];

    // Side-of-close — long overnight closes via sells; short via buys.
    // closed_qty is the unsigned magnitude that was closed today, capped
    // at |overnight_quantity|.
    const closed_qty = oq > 0 ? Math.min(oq, dsq) : Math.min(-oq, dbq);
    if (closed_qty <= 0) return [p];   // intraday only added more, no close

    // Today's exit price for the closed portion. For closing a LONG
    // overnight: it was sold today at avg = dsv / dsq. For covering a
    // SHORT: it was bought back at avg = dbv / dbq.
    const exit_price = oq > 0
      ? (dsq > 0 ? dsv / dsq : 0)
      : (dbq > 0 ? dbv / dbq : 0);

    // Realised on the closed portion vs yesterday's close (the day-mark
    // anchor — same convention the P∆ formula uses). For a long sold:
    // gain = (exit − close) × closed. For a short covered: gain = (close
    // − exit) × closed.
    const closed_realised = oq > 0
      ? (exit_price - close) * closed_qty
      : (close - exit_price) * closed_qty;

    // Open row's day_change_val = total leg P∆ minus the realised the
    // closed row absorbs, so the two rows sum back to the original.
    const open_dcv = Number(p.day_change_val || 0) - closed_realised;

    const closedRow = {
      ...p,
      // qty=0 means "closed" everywhere downstream (cand-row tinting,
      // P&L cell shows realised, no chart-icon for trade-action).
      qty: 0,
      pnl: closed_realised,
      day_change_val: closed_realised,
      // Mark the row visually as the closed half via a synthesized
      // key suffix; cand-row `c.source + '|' + c.account + '|' +
      // c.symbol` key gets a unique tail so Svelte tracks the two
      // halves as different rows.
      _splitTag: 'closed',
    };
    // Broker net quantity reflects what's CURRENTLY held. When
    // overnight=4 and operator sold all 4 today, broker reports
    // quantity=0 — the closed row alone fully describes the day and
    // there is no actual "open" portion. Emitting an OPEN-tagged row
    // with qty=0 misled the operator into thinking there was still
    // something on the book to act on. Suppress it.
    const brokerQty = Math.abs(Number(p.qty || 0));
    if (brokerQty === 0) return [closedRow];

    const openRow = {
      ...p,
      // Re-attribute total leg P&L: closed row gets the realised
      // portion; the open row keeps the remaining unrealised
      // (broker's pnl − realised already attributed).
      pnl: Number(p.pnl || 0) - closed_realised,
      // Zero out realised on the open half — the closed half already
      // carries it as its pnl. Without this, the per-row P&L formula
      // `(ltp − cost) × qty + realised` would double-count the
      // realised on the open row.
      realised: 0,
      day_change_val: open_dcv,
      _splitTag: 'open',
    };
    return [closedRow, openRow];
  }

  async function loadPositions() {
    /** @type {Array<any>} */
    const merged = [];

    // Live broker positions
    try {
      const r = await fetchPositions();
      positionsLoadErr = '';
      for (const p of (r?.rows || [])) {
        const sym = p?.tradingsymbol || p?.symbol;
        if (!sym) continue;
        // Only options + futures (skip cash equities — this page is
        // options-only).
        if (!/(CE|PE|FUT)$/i.test(String(sym))) continue;
        const baseRow = {
          symbol:   String(sym).toUpperCase(),
          account:  String(p?.account || ''),
          qty:      Number(p?.quantity || 0),
          source:   'live',
          avg_cost: p?.average_price != null ? Number(p.average_price) : null,
          ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
          prev_close: p?.close_price != null ? Number(p.close_price) : null,
          pnl:      p?.pnl != null ? Number(p.pnl) : 0,
          realised: p?.realised != null ? Number(p.realised) : 0,
          day_change_val: p?.day_change_val != null ? Number(p.day_change_val) : 0,
          overnight_quantity: Number(p?.overnight_quantity || 0),
          day_buy_quantity:   Number(p?.day_buy_quantity || 0),
          day_sell_quantity:  Number(p?.day_sell_quantity || 0),
          day_buy_value:      Number(p?.day_buy_value || 0),
          day_sell_value:     Number(p?.day_sell_value || 0),
        };
        // Split closed-and-reopened legs into separate display rows
        // (operator: "if positions are closed and the same positions
        // are opened same or different qty, you have to show them as
        // different rows"). Variant 1: trigger when overnight ≠ 0 AND
        // either day_buy > 0 or day_sell > 0 (also covers partial
        // reduction). Pure overnight (no day trades) or pure intraday
        // round-trip (overnight = 0) stay as one row.
        for (const row of splitClosedReopened(baseRow)) merged.push(row);
      }
    } catch (e) {
      // Don't blank the previous candidates on a transient failure —
      // banner explains the staleness, the prior list keeps rendering.
      positionsLoadErr = e?.message || 'Broker positions unavailable.';
    }

    // Sim positions — capture last_price + average_price from the
    // driver state at click time so the strategy endpoint can compute
    // analytics without round-tripping back to the broker.
    try {
      const s = await fetchSimStatus();
      for (const p of (s?.positions || [])) {
        const sym = p?.symbol;
        if (!sym) continue;
        if (!/(CE|PE|FUT)$/i.test(String(sym))) continue;
        const baseRow = {
          symbol:   String(sym).toUpperCase(),
          account:  String(p?.account || ''),
          qty:      Number(p?.quantity || 0),
          source:   'sim',
          avg_cost: p?.average_price != null ? Number(p.average_price) : null,
          ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
          prev_close: p?.close_price != null ? Number(p.close_price) : null,
          pnl:      p?.pnl != null ? Number(p.pnl) : 0,
          realised: p?.realised != null ? Number(p.realised) : 0,
          day_change_val: p?.day_change_val != null ? Number(p.day_change_val) : 0,
          overnight_quantity: Number(p?.overnight_quantity || 0),
          day_buy_quantity:   Number(p?.day_buy_quantity || 0),
          day_sell_quantity:  Number(p?.day_sell_quantity || 0),
          day_buy_value:      Number(p?.day_buy_value || 0),
          day_sell_value:     Number(p?.day_sell_value || 0),
        };
        for (const row of splitClosedReopened(baseRow)) merged.push(row);
      }
    } catch (_) { /* ignore */ }

    positions = merged;
    _saveCache();
  }

  // Consecutive-failure counter for loadStrategy. Suppresses the
  // error banner on a single transient hiccup (page reopen during
  // a backend redeploy, slow first response on a cold connection,
  // etc.) — only escalates after 2+ failures in a row so the user
  // sees the chart appear cleanly when the next poll succeeds.
  let _stratFails = 0;
  async function loadStrategy() {
    const cleanLegs = legs
      .map(l => {
        const sym = String(l.symbol || '').trim().toUpperCase();
        // Look up the contract's actual expiry from the instruments
        // cache. Kite stores per-contract expiries on the `x` field —
        // authoritative for every exchange. Critical for MCX
        // commodities (GOLDM/CRUDEOIL/etc.) where the backend's
        // symbol parser would otherwise infer the NSE-F&O last-
        // Thursday rule and land 1-3 days off the real expiry.
        const inst    = sym ? getInstrument(sym) : null;
        const expiry  = inst?.x || null;
        return {
          symbol:   sym,
          qty:      l.qty === '' || l.qty == null ? 0 : Number(l.qty),
          avg_cost: l.avg_cost === '' || l.avg_cost == null ? null : Number(l.avg_cost),
          // Only inline ltp for sources whose price isn't on the wire
          // (sim driver state, operator drafts). For live broker
          // positions, drop ltp so the backend re-fetches a fresh
          // quote every poll — otherwise the stale `last_price` from
          // the 30s position poll overrides every subsequent broker
          // fetch and the chart's spot/Greeks/EV freeze even though
          // analytics is polling at 5s.
          ltp: (l.source === 'sim' || l.source === 'draft')
            ? (l.ltp === '' || l.ltp == null ? null : Number(l.ltp))
            : null,
          expiry,
        };
      })
      .filter(l => l.symbol && l.qty);
    if (!cleanLegs.length) {
      strategy = null; strategyErr = ''; _stratFails = 0;
      return;
    }
    // Detect underlying change between the previous strategy fetch
    // and this one. When the operator switches from GOLDM to
    // CRUDEOIL (or any other inter-underlying flip), the previous
    // response carries the OLD underlying's spot_anchor_contract,
    // spot value, payoff curve x-range and the OptionsPayoff
    // component renders all of that until the new fetch resolves
    // — operator sees GOLDM anchor chip + GOLDM spot marker on a
    // CRUDEOIL-titled card for the duration of the request.
    // Clear strategy immediately so the chart goes blank
    // (loading state) instead of lying about the underlying.
    const newU = decomposeSymbol(cleanLegs[0].symbol).root;
    const oldU = strategy?.legs?.length ? decomposeSymbol(strategy.legs[0].symbol).root : '';
    if (newU && oldU && newU !== oldU) {
      strategy = null;
    }
    loading = true;
    try {
      strategy = await fetchStrategyAnalytics(cleanLegs);
      strategyErr = '';
      _stratFails = 0;
      _saveCache();
    } catch (e) {
      _stratFails += 1;
      // Banner shows only when (a) we have no prior chart to fall
      // back on AND (b) we've failed at least twice in a row. A
      // first-load transient — common on tab reopen during a deploy
      // or after a wifi reconnect — stays silent and the next poll
      // brings the chart in cleanly. The api-layer logger still
      // records the raw error in the browser console for debugging.
      if (!strategy && _stratFails >= 2) {
        strategyErr = /** @type {any} */ (e).message || String(e);
      }
    } finally {
      loading = false;
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
  function _saveCache() {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem(_CACHE_KEY, JSON.stringify({
        ts: Date.now(),
        positions, strategy, drafts,
        selectedAccounts, selectedUnderlying, selectedExpiries,
        enabledSymbols,
      }));
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
      if (d.strategy)                  strategy  = d.strategy;
      if (Array.isArray(d.drafts))     drafts    = d.drafts;
      if (Array.isArray(d.selectedAccounts)) selectedAccounts = d.selectedAccounts;
      if (typeof d.selectedUnderlying === 'string') selectedUnderlying = d.selectedUnderlying;
      if (Array.isArray(d.selectedExpiries))          selectedExpiries  = d.selectedExpiries;
      if (d.enabledSymbols && typeof d.enabledSymbols === 'object') {
        enabledSymbols = d.enabledSymbols;
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
      loadPositions();
      loadRealAccounts();
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
    loadPositions();
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
    // Load the instruments cache so the option-chain picker has data.
    // Already cached in IndexedDB after the first /console autocomplete
    // load — most operators will see this resolve from cache instantly.
    // `underlyingChoices` is a $derived that recomputes off
    // instrumentsReady + selectedUnderlying + positions, so flipping
    // the flag is enough to populate the chain picker's dropdown.
    try {
      await loadInstruments();
      instrumentsReady = true;
    } catch (_) { /* instruments unreachable — chain picker hides */ }
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
    teardown    = marketAwareInterval(loadStrategy,  5000);
    posTeardown = marketAwareInterval(loadPositions, 30000);
    // Sim status polled at 30 s here (down from 5 s) — the layout-level
    // _adaptiveInterval already polls it every 4 s when a sim is
    // actually active and every 30 s when idle, so 5 s here was double-
    // polling for no benefit. 30 s catches sim-start transitions
    // within one cycle without burning extra requests.
    simTeardown = marketAwareInterval(loadSimStatus, 30000);

    // Real-time fill notifications — Kite postback fires a
    // `position_filled` ws event the moment an order completes.
    // Subscribe so the placement toast can transition to FILLED in-
    // place (or a fresh FILLED toast lands when the fill is from an
    // algo-engine path the operator didn't manually place here).
    // Refreshes loadPositions too so the Candidates panel reflects
    // the new fill within one ws round-trip, not on the 30 s poll.
    wsTeardown = createPerformanceSocket((msg) => {
      if (msg?.event !== 'position_filled') return;
      const orderId = String(msg.order_id || '');
      const matched = orderId ? _markToastFilled(orderId, Number(msg.fill_price || 0)) : false;
      if (!matched) _pushFillToast(msg);
      loadPositions();
    });
  });
  onDestroy(() => { teardown?.(); posTeardown?.(); simTeardown?.(); wsTeardown?.(); });

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
    const ml = strategy.risk?.max_loss;
    const nc = strategy.net_cost;
    if (ml == null || !Number.isFinite(ml)) return null;
    if (nc == null || Math.abs(nc) <= 1) return null;
    return Math.abs(ml) / Math.abs(nc);
  });
  function fmtPct(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return `${pctFmt(v * 100)}%`;
  }
  function fmtNum(/** @type {number|null|undefined} */ v, /** @type {number} */ dp = 4) {
    if (v == null) return '—';
    return v.toFixed(dp);
  }

</script>

<svelte:head><title>Derivatives | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Derivatives</h1>
    {#if simActive}
      <span class="opt-mode-pill opt-mode-sim" title="A simulator run is active. Candidates and analytics are sourced from the sim book.">SIMULATOR</span>
    {/if}
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={() => { loadPositions(); loadSimStatus(); loadStrategy(); }}
                   loading={loading} label="derivatives" />
    <PageHeaderActions symbol={selectedUnderlying} />
  </span>
</div>

<!-- Picker bar — two dropdowns + a "+" toggle for the option-chain
     picker. Strategy auto-recomputes whenever the leg set changes;
     no Analyze button needed. -->
<!-- Picker bar — no card wrapper. Account + Underlying + + sit
     directly on the page so they read as inline page-level
     controls rather than as content inside a panel. -->
<div class="opt-picker mb-3">
  <div class="opt-field opt-field-grow">
    <label class="field-label" for="opt-acct">Account</label>
    <!-- /admin/options is fundamentally about positions-based option
         analysis — picker is always per-account, so AccountMultiSelect
         here is just a styled passthrough (no disabled state ever). -->
    <AccountMultiSelect id="opt-acct"
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
        placeholder={underlyingChoicesFromBook.length ? 'Pick underlying…' : 'No options in book'} />
    </div>
  </div>
  <div class="opt-field">
    <label class="field-label" for="opt-exp">Expiry</label>
    <MultiSelect id="opt-exp"
      bind:value={selectedExpiries}
      options={expiryChoicesForUnderlying.map(x => ({ value: x, label: x }))}
      placeholder={expiryChoicesForUnderlying.length ? 'All expiries' : '—'} />
  </div>
  <!-- OChain launcher — single toggle that opens / closes the chain
       picker. Per-row +/− inside the picker decides each leg's side
       (BUY / SELL); the outer button is now purely "show / hide
       picker." Disabled until the operator picks exactly one real
       account on the page — every leg landed via the chain routes
       through OrderTicket which needs an unambiguous routing
       account. -->
  <div class="opt-trade" role="group" aria-label="Open chain picker">
    <button type="button"
            class="opt-add-btn opt-add-btn-ochain"
            class:opt-add-btn-on={showAddPanel}
            title={showAddPanel
              ? 'Hide the chain picker'
              : 'Open the chain picker — pick or change account inside the order panel.'}
            aria-label="Toggle chain picker"
            aria-pressed={showAddPanel}
            onclick={() => {
              // Seed the chain picker from the operator's already-
              // chosen underlying / expiry on the picker bar above.
              // Earlier the chain picker kept its own independent
              // chainUnderlying state, defaulting to NIFTY — an
              // operator analysing BANKNIFTY / GOLDM had to pick the
              // underlying again inside the panel.
              if (!showAddPanel) {
                if (selectedUnderlying)          chainUnderlying = selectedUnderlying;
                if (selectedExpiries.length > 0) chainExpiry     = selectedExpiries[0];
              }
              showAddPanel = !showAddPanel;
              // Also open the 3-tab shell on the Chain tab so the
              // operator can build and submit a basket directly from
              // the shell. The in-page panel stays for visual scan;
              // the shell is the action surface.
              if (showAddPanel) {
                // Pass `selectedUnderlying` as the symbol so the
                // OrderTicket chain tab seeds its own underlying
                // picker from the page's active underlying instead
                // of opening blank.
                openTicket({
                  defaultTab: 'chain',
                  symbol:     selectedUnderlying || '',
                  exchange:   'NFO',
                  side:       'BUY',
                  action:     'open',
                  qty:        0,
                  lotSize:    0,
                  accounts:   ticketAccounts,
                  account:    _ticketAccountDefault(),
                });
              }
            }}>Chain</button>
  </div>
</div>

{#if positionsLoadErr}
  <!-- Short banner per ops convention (≤35 chars). Full error detail
       logged to console by api.js. -->
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40"
       title={positionsLoadErr}>
    Positions feed unavailable — candidates may be stale.
  </div>
{/if}
{#if strategyErr}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{strategyErr}</div>
{/if}

{#if !strategy && !strategyErr && !loading && !drafts.length}
  <div class="text-[0.65rem] text-[#7e97b8] italic mb-3">
    {#if !selectedUnderlying}
      Pick an underlying to surface {simActive ? 'sim' : 'live'} candidates, or click
      <b>+ Add</b> to drop a draft strike into the payoff.
    {:else if candidatePositions.length === 0}
      No {simActive ? 'sim' : 'live'} positions on <b>{selectedUnderlying}</b> and no drafts yet.
      Click <b>+ Add</b> to drop a draft strike on this underlying.
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
        <span></span>
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
{#if strategy}
  <div class="opt-payoff opt-payoff-full algo-status-card cmd-surface p-3"
    class:fs-card-on={_fsPayoff}
    class:is-collapsed={_colPayoff}>
    <!-- Single-row header — title + Net debit/credit + Max profit /
         Max loss + Greeks chips, plus the global collapse +
         fullscreen toggles. SPOT / TDAY / EXP / DTE / σ / LEGS live
         in the on-chart stat overlay. -->
    <div class="opt-section-h opt-section-h-grid">
      <div class="opt-section-row">
        <span class="opt-section-title">Payoff</span>
        <span class="opt-section-tag tag-{strategy.net_cost > 0 ? 'long' : strategy.net_cost < 0 ? 'short' : 'long'}">
          {strategy.net_cost > 0 ? 'Net Dr' : strategy.net_cost < 0 ? 'Net Cr' : 'Free'}
          {fmtMoney(Math.abs(strategy.net_cost), false)}
        </span>
        <span class="opt-section-tag tag-long" title="Max profit">
          MAX P {fmtUnbounded(strategy.risk.max_profit, false)}
        </span>
        <span class="opt-section-tag tag-short" title="Max loss">
          MAX L {fmtUnbounded(strategy.risk.max_loss, false)}
        </span>
        <!-- Greeks chips — full Δ Γ Θ 𝒱 ρ surfaced inline in the payoff
             header so the operator sees position-level direction /
             convexity / decay / volatility / rate exposure without
             scrolling to the Greeks card below. Same `.opt-section-tag`
             chip chrome; greek-tagged variant uses a sky-cyan tint to
             read as distinct from the amber Net / Max chips. Sign-tinted
             variants on theta / vega / rho since those flip sign with
             credit vs debit structures. -->
        <span class="opt-section-tag tag-greek"
          title="Delta — net directional exposure (₹ per ₹1 spot move)">
          Δ {pctFmt(strategy.aggregate_greeks.delta)}
        </span>
        <span class="opt-section-tag tag-greek"
          title="Gamma — convexity, rate of change of Δ as spot moves">
          Γ {pctFmt(strategy.aggregate_greeks.gamma)}
        </span>
        <span class="opt-section-tag tag-greek {strategy.aggregate_greeks.theta < 0 ? 'tag-greek-neg' : ''}"
          title="Theta — daily decay (₹/day, positive when net short premium)">
          Θ {pctFmt(strategy.aggregate_greeks.theta)}
        </span>
        <span class="opt-section-tag tag-greek {strategy.aggregate_greeks.vega < 0 ? 'tag-greek-neg' : ''}"
          title="Vega — P&L per 1% IV move (positive = long volatility)">
          𝒱 {pctFmt(strategy.aggregate_greeks.vega)}
        </span>
        <span class="opt-section-tag tag-greek {strategy.aggregate_greeks.rho < 0 ? 'tag-greek-neg' : ''}"
          title="Rho — P&L per 1% interest-rate move (typically small for short-DTE)">
          ρ {pctFmt(strategy.aggregate_greeks.rho)}
        </span>
        {#if _fsPayoff}
          <RefreshButton onClick={() => { loadPositions(); loadSimStatus(); loadStrategy(); }}
                         loading={loading} label="payoff" />
        {/if}
        <CollapseButton bind:isCollapsed={_colPayoff} cardId="optPayoff" label="Payoff" />
        <DefaultSizeButton bind:isFullscreen={_fsPayoff} bind:isCollapsed={_colPayoff} label="Payoff" />
        <FullscreenButton bind:isFullscreen={_fsPayoff} label="Payoff" />
      </div>
    </div>
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
        payoff={strategy.payoff}
        spot={(strategy.spot_source === 'fallback') ? null : strategy.spot}
        prevClose={strategy.spot_prev_close}
        breakevens={strategy.risk.breakevens}
        intermediateCurves={strategy.intermediate_curves || []}
        spanSigmas={strategy.span_sigmas}
        spanPct={strategy.span_pct}
        dte={strategy.days_to_expiry}
        ivProxy={strategy.iv_proxy}
        legCount={strategy.legs.length}
        multiExpiry={strategy.multi_expiry ?? false}
        realizedPnl={chartPnlOffset}
        dayPnl={candidatesDayPnl}
        legSymbols={strategy.legs.map(/** @param {{symbol:string}} l */ l => l.symbol)}
        spotAnchor={strategy.spot_anchor_contract
          ? { contract: strategy.spot_anchor_contract,
              source: strategy.spot_source || 'futures',
              expiryISO: strategy.expiry ?? '' }
          : null}
        loading={loading || strategy.spot_source === 'fallback'}
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
{#if selectedUnderlying || drafts.length}
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
             the full leg list and the close-list summary. -->
        <div class="legs-tabs" role="tablist" aria-label="Legs view">
          <button type="button" role="tab"
                  class="legs-tab"
                  class:legs-tab-on={legsTab === 'legs'}
                  aria-selected={legsTab === 'legs'}
                  onclick={() => legsTab = 'legs'}>
            Legs
            {#if candidatePositions.length > 0}
              <span class="legs-tab-count">{candidatePositions.length}</span>
            {/if}
          </button>
          <button type="button" role="tab"
                  class="legs-tab"
                  class:legs-tab-on={legsTab === 'expiry'}
                  aria-selected={legsTab === 'expiry'}
                  title="Positions identified for close before expiry day"
                  onclick={() => legsTab = 'expiry'}>
            Exp close
            {#if expiryCloseTotal > 0}
              <span class="legs-tab-count legs-tab-count-alert">{expiryCloseTotal}</span>
            {/if}
          </button>
        </div>
      </div>
      {#if _fsLegs}
        <RefreshButton onClick={() => { loadPositions(); loadSimStatus(); loadStrategy(); }}
                       loading={loading} label="legs" />
      {/if}
      <CollapseButton bind:isCollapsed={_colLegs} cardId="optLegs" label="Legs" />
      <DefaultSizeButton bind:isFullscreen={_fsLegs} bind:isCollapsed={_colLegs} label="Legs" />
      <FullscreenButton bind:isFullscreen={_fsLegs} label="Legs" />
    </div>
    {#if !_colLegs && displayedCandidates.length}
      {@const hideAcct = selectedAccounts.length === 1}
      <div class="cand-scroll">
        <div class="cand-grid" class:cand-grid-noacct={hideAcct}>
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
            <!-- Symbol header — hyphenated form below carries the
                 expiry month inline (e.g. NIFTY-26JUN-22000-CE) so a
                 separate Expiry column would be redundant. -->
            <span>Symbol</span>
            {#if !hideAcct}<span>Acct</span>{/if}
            <span class="num">Qty</span>
            <span class="num">LTP</span>
            <span class="num">Prev</span>
            <span class="num">Avg</span>
            <span class="num"
                  title="Cumulative P&L on the position (lifetime, broker-reported). Sum across all rows = strip's P chip.">
              P&amp;L
            </span>
            <span class="num"
                  title="Today's change in P&L (broker-agnostic split formula). Sum across all rows = strip's P∆ chip.">
              Day P&amp;L
            </span>
            <span class="num">IV</span>
            <span class="num">Δ</span>
            <span class="num">Γ</span>
            <span class="num">Θ</span>
            <span class="num">𝒱</span>
          </div>
          {#each displayedCandidates as c, _ci (c.source + '|' + c.account + '|' + c.symbol + '|' + (c._splitTag ?? _ci) + '|' + (c._pairId ?? '') + '|' + (c._band ?? '') + '|' + (c.draftId != null ? c.draftId : _ci))}
            <!-- Band section headers — inject a full-width header row
                 when this row is the first of its band in the expiry view.
                 Three bands: close (amber) → netted (slate) → otm (muted).
                 We check whether this row is the first of its band by
                 comparing with the previous row's band. -->
            {#if legsTab === 'expiry' && c._band && (
              _ci === 0 ||
              displayedCandidates[_ci - 1]?._band !== c._band
            )}
              {@const _bandCount = displayedCandidates.filter(r => r._band === c._band && r._segment === c._segment).length}
              <div class="expiry-band-header expiry-band-header-{c._band}" aria-label="{BAND_LABELS[c._band] ?? c._band} — {c._segment}">
                <!-- Pill — section identity in a single visual chunk:
                     dot glyph + label text + count badge. Operator:
                     "highlight heading to close, netted, out of the
                     money like a pill/chip for better visibility". -->
                <span class="expiry-band-pill">
                  <span class="expiry-band-dot" aria-hidden="true">
                    {#if c._band === 'close'}●{:else if c._band === 'netted'}⊗{:else}○{/if}
                  </span>
                  <span class="expiry-band-label">{BAND_LABELS[c._band] ?? c._band}</span>
                  <span class="expiry-band-count">{_bandCount}</span>
                </span>
                {#if c._band === 'close'}<span class="expiry-band-hint">action required before expiry</span>
                {:else if c._band === 'netted'}<span class="expiry-band-hint">broker nets at settlement — no action needed</span>
                {:else if c._band === 'otm'}<span class="expiry-band-hint">expires worthless — monitor only</span>
                {/if}
              </div>
            {/if}
            {@const lg = legAnalyticsBySymbol[c.symbol]}
            {@const ltp = lg && lg.ltp != null ? lg.ltp : c.ltp}
            {@const cost = c.avg_cost != null ? c.avg_cost : (lg ? lg.avg_cost : null)}
            {@const isClosed = Number(c.qty || 0) === 0}
            <!-- displayQty = residual qty (after netting) when the row
                 came from the Close tab's expiry analysis; otherwise
                 the original position qty. Drives the qty cell, the
                 row direction tint, P&L recompute, and the close-
                 ticket prefill so every surface speaks to the
                 effective exposure rather than the gross position. -->
            {@const displayQty = c._residualQty != null
              ? Number(c._residualQty)
              : Number(c.qty || 0)}
            <!-- Open-row P&L = (live_ltp − cost) × current_qty + realised.
                 The realised term carries the cash from intraday
                 closeouts so the row reconciles with Kite's broker
                 pnl (which includes realised). Without it, a leg
                 that's been partially closed today would show
                 unrealised-only and diverge from the strip's P
                 chip by the realised amount. Closed rows (qty=0)
                 use broker's c.pnl directly — that IS realised. -->
            {@const pnl = isClosed
              ? (c.pnl != null ? Number(c.pnl) : null)
              : ((ltp != null && cost != null)
                  ? (ltp - cost) * displayQty + Number(c.realised || 0)
                  : null)}
            {@const dir = displayQty < 0 ? 'short' : displayQty > 0 ? 'long' : 'flat'}
            {@const isClosable = !isClosed && c.source !== 'draft'}
            <!-- Row click → close-position ticket. Skipped on
                 drafts (no real exposure) and zero-qty rows
                 (already closed — sorted to end of list, kept
                 visible for context). The checkbox stops
                 propagation so toggling a leg doesn't
                 inadvertently fire the close handler. -->
            {@const isDraft = c.source === 'draft'}
            {@const isActionable = isDraft || isClosable}
            {@const _instParsed = getInstrument(c.symbol)}
            {@const _expiryStr = _instParsed?.x || ''}
            {@const _decomp = decomposeSymbol(c.symbol)}
            {@const _optClass = _decomp.optType === 'CE' ? 'sym-ce'
                              : _decomp.optType === 'PE' ? 'sym-pe'
                              : ''}
            {@const _acctColor = c.account ? acctColor(c.account) : null}
            <div class="cand-row cand-row-{dir}"
                 style={_acctColor ? `--cand-acct-color: ${_acctColor};` : ''}
                 class:cand-disabled={enabledSymbols[enKey(c)] === false}
                 class:cand-closed={isClosed}
                 class:cand-draft={isDraft}
                 class:expiry-band-close={legsTab === 'expiry' && c._band === 'close'}
                 class:expiry-band-netted={legsTab === 'expiry' && c._band === 'netted'}
                 class:expiry-band-otm={legsTab === 'expiry' && c._band === 'otm'}
                 data-pair-tint={legsTab === 'expiry' && c._band === 'netted' ? (c._pairTint ?? 0) : null}
                 class:cand-row-equity-close={c._expiryStatus === 'equity-close'}
                 class:cand-row-commodity-close={c._expiryStatus === 'commodity-close'}
                 role="button"
                 tabindex="0"
                 title={isDraft
                   ? `Execute draft — open SymbolPanel on Ticket tab pre-filled`
                   : isClosable
                     ? `Close ${Math.abs(displayQty)} ${c.symbol} — SymbolPanel on Ticket tab`
                     : `${c.symbol} — open SymbolPanel on Chart tab`}
                 onclick={() => {
                   // Actionable rows open the Ticket tab pre-filled
                   // for close/execute; non-actionable rows (closed
                   // positions, etc.) open the Chart tab — SymbolPanel
                   // is the single entry point for any per-symbol
                   // workflow, no separate ⋯ menu.
                   if (isDraft) executeDraft(c);
                   else if (isClosable) closePosition(c);
                   else openTicket({
                     symbol:    c.symbol,
                     exchange:  'NFO',
                     defaultTab:'chart',
                     accounts:  ticketAccounts,
                     account:   _rowTicketAccount(c),
                   });
                 }}
                 onkeydown={(e) => {
                   if (e.key === 'Enter' || e.key === ' ') {
                     e.preventDefault();
                     if (isDraft) executeDraft(c);
                     else if (isClosable) closePosition(c);
                     else openTicket({
                       symbol:    c.symbol,
                       exchange:  'NFO',
                       defaultTab:'chart',
                       accounts:  ticketAccounts,
                       account:   _rowTicketAccount(c),
                     });
                   }
                 }}>
              <input type="checkbox"
                     checked={enabledSymbols[enKey(c)] !== false}
                     onclick={(e) => e.stopPropagation()}
                     onchange={(e) => {
                       const next = { ...enabledSymbols };
                       next[enKey(c)] = /** @type {HTMLInputElement} */ (e.currentTarget).checked;
                       enabledSymbols = next;
                     }} />
              <!-- svelte-ignore a11y_interactive_supports_focus -->
              <span class="font-mono cand-sym cand-sym-acct"
                oncontextmenu={(ev) => { ev.preventDefault(); _ctxMenu = { symbol: c.symbol, exchange: c.exchange || 'NFO', x: ev.clientX, y: ev.clientY }; }}
                use:longPress={(ev) => {
                  _ctxMenu = { symbol: c.symbol, exchange: c.exchange || 'NFO', x: ev.clientX, y: ev.clientY };
                }}>
                <span class="sym-main {_optClass}">{formatSymbol(c.symbol)}</span>
                {#if c._splitTag === 'closed'}
                  <!-- Split-row tag: this row represents the portion of
                       the overnight position that was CLOSED today.
                       The sibling row (without the tag) represents
                       what's still OPEN after the round-trip. -->
                  <span class="cand-split-tag cand-split-closed"
                        title="Closed portion of an intraday round-trip on this leg">CLOSED</span>
                {:else if c._splitTag === 'open'}
                  <span class="cand-split-tag cand-split-open"
                        title="Currently open portion after today's close-and-reopen">OPEN</span>
                {/if}
                {#if isDraft}
                  <!-- Draft remove button — page-local removal only,
                       NO order placed. Clicking the row body still
                       opens the OrderTicket pre-filled to PLACE the
                       draft as a real order; this × is the
                       "discard" affordance. Stops propagation so
                       the row's executeDraft handler doesn't fire. -->
                  <button type="button" class="cand-draft-x"
                          title="Remove this draft (no order placed)"
                          aria-label="Remove draft"
                          onclick={(e) => {
                            e.stopPropagation();
                            if (c.draftId != null) removeDraft(c.draftId);
                          }}>×</button>
                {/if}
                {#if legsTab === 'expiry' && c._band}
                  {#if c._band === 'close' && c._closeId}
                    <span class="expiry-id-chip expiry-id-close" title={c._reason}>{c._closeId}</span>
                  {:else if c._band === 'netted' && c._pairId}
                    <span class="expiry-id-chip expiry-id-netted" title={c._reason ?? ''}>{c._pairId}</span>
                  {/if}
                {/if}
              </span>
              <!-- Expiry cell removed — the hyphenated symbol shows it. -->
              {#if !hideAcct}<span class="font-mono">{c.account}</span>{/if}
              <span class="num {displayQty < 0 ? 'kv-neg' : 'kv-pos'}">{displayQty}</span>
              <span class="num">{ltp != null ? priceFmt(ltp) : '—'}</span>
              <span class="num">{c.prev_close != null ? priceFmt(c.prev_close) : '—'}</span>
              <span class="num">{cost != null ? priceFmt(cost) : '—'}</span>
              <span class="num cand-pnl {pnl == null ? '' : pnl > 0 ? 'cell-pos' : pnl < 0 ? 'cell-neg' : 'cell-flat'}">
                {pnl == null ? '—' : aggCompact(pnl)}
              </span>
              <span class="num cand-pnl {c.day_change_val == null ? 'cell-flat' : Number(c.day_change_val) > 0 ? 'cell-pos' : Number(c.day_change_val) < 0 ? 'cell-neg' : 'cell-flat'}">
                {c.day_change_val == null ? '—' : aggCompact(Number(c.day_change_val))}
              </span>
              <span class="num">{lg ? pctFmt(lg.iv * 100) + '%' : '—'}</span>
              <span class="num">{lg ? pctFmt(lg.greeks.delta) : '—'}</span>
              <span class="num">{lg ? pctFmt(lg.greeks.gamma) : '—'}</span>
              <span class="num {lg && lg.greeks.theta < 0 ? 'kv-neg' : ''}">{lg ? aggCompact(lg.greeks.theta) : '—'}</span>
              <span class="num">{lg ? aggCompact(lg.greeks.vega) : '—'}</span>
            </div>
          {/each}
          {#if displayedCandidates.length > 0}
            <!-- TOTAL row — always the last row of the grid. Sums
                 pnl + day_change_val across every displayed candidate so
                 the two columns roll up to the strip's P and P∆ chips
                 for the same set of accounts the grid is showing. -->
            {@const _totalPnl = displayedCandidates.reduce((s, c) => s + Number(c.pnl ?? 0), 0)}
            {@const _totalDcv = displayedCandidates.reduce((s, c) => s + Number(c.day_change_val ?? 0), 0)}
            <div class="cand-row cand-row-total">
              <span></span>
              <span class="cand-total-label">TOTAL</span>
              {#if !hideAcct}<span>—</span>{/if}
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num cand-pnl {_totalPnl > 0 ? 'cell-pos' : _totalPnl < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ P&L across every visible row = strip's P chip for these accounts">
                {aggCompact(_totalPnl)}
              </span>
              <span class="num cand-pnl {_totalDcv > 0 ? 'cell-pos' : _totalDcv < 0 ? 'cell-neg' : 'cell-flat'}"
                    title="Σ Day P&L Δ across every visible row = strip's P∆ chip for these accounts">
                {aggCompact(_totalDcv)}
              </span>
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num">—</span>
              <span class="num">—</span>
            </div>
          {/if}
        </div>
      </div>
    {:else if !_colLegs}
      <div class="text-[0.6rem] text-[#7e97b8] italic">
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
            <InfoHint popup text={'Sum of every leg\'s signed-qty Greeks. Δ = net directional exposure; Θ = daily decay (positive when net short premium); 𝒱 = sensitivity to a 1 % IV move.'} />
          </div>
          <div class="opt-kv opt-kv-greeks">
            <div class="kv-pair" title="Delta — net directional exposure. +50 ≈ ₹50 gained per ₹1 spot rise.">
              <span class="kv-k kv-k-greek">Δ</span>
              <span class="kv-v">{pctFmt(strategy.aggregate_greeks.delta)}</span>
            </div>
            <div class="kv-pair" title="Gamma — rate-of-change of delta as spot moves.">
              <span class="kv-k kv-k-greek">Γ</span>
              <span class="kv-v">{pctFmt(strategy.aggregate_greeks.gamma)}</span>
            </div>
            <div class="kv-pair" title="Theta — daily decay in rupees. Positive when net short premium.">
              <span class="kv-k kv-k-greek">Θ</span>
              <span class="kv-v {strategy.aggregate_greeks.theta < 0 ? 'kv-neg' : 'kv-pos'}">{pctFmt(strategy.aggregate_greeks.theta)}</span>
            </div>
            <div class="kv-pair" title="Vega — P&L change per 1 % IV move. Positive = long volatility.">
              <span class="kv-k kv-k-greek">𝒱</span>
              <span class="kv-v {strategy.aggregate_greeks.vega < 0 ? 'kv-neg' : 'kv-pos'}">{pctFmt(strategy.aggregate_greeks.vega)}</span>
            </div>
            <div class="kv-pair" title="Rho — sensitivity to a 1 % rate change. Mostly cosmetic for short-dated index options.">
              <span class="kv-k kv-k-greek">ρ</span>
              <span class="kv-v">{pctFmt(strategy.aggregate_greeks.rho)}</span>
            </div>
          </div>
        </div>

        <div class="opt-block">
          <div class="opt-block-h">
            Risk &amp; expected value
            <InfoHint popup text={'Aggregate risk + expected value across all legs. Probability-weighted outcomes integrated against the lognormal pdf of the underlying using a qty-weighted IV proxy. POP × magnitudes captures the asymmetry that POP alone misses.'} />
          </div>
          <div class="opt-kv">
            <div class="kv-pair">
              <span class="kv-k">R:R <InfoHint popup text={'<b>Risk-to-reward</b> = max_profit / |max_loss|. "1 : 0.5" = risk ₹100 to make ₹50. "1 : 3" = risk ₹100 to make ₹300. <b>—</b> when one side is unbounded.'} /></span>
              <span class="kv-v">{strategy.risk.rr_ratio == null ? '—' : `1 : ${pctFmt(strategy.risk.rr_ratio)}`}</span>
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
                {#if strategy.risk.breakevens.length}
                  {strategy.risk.breakevens.map(/** @param {number} b */ (b) => priceFmt(b)).join(' / ')}
                {:else}—{/if}
              </span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">POP <InfoHint popup text={'<b>Probability of profit</b> at expiry — sum of lognormal mass over every contiguous profitable region of the payoff curve. For range strategies (iron condors), this measures "P(spot ends inside the wings)".'} /></span>
              <span class="kv-v {strategy.risk.pop > 0.6 ? 'kv-pos' : strategy.risk.pop < 0.4 ? 'kv-neg' : ''}">{fmtPct(strategy.risk.pop)}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">EV <InfoHint popup text={'<b>Expected value</b> — POP × win-magnitude − (1−POP) × loss-magnitude, integrated against the lognormal pdf of the underlying. Positive EV = edge in expectation; negative EV = no edge, even if POP is high.'} /></span>
              <span class="kv-v {strategy.risk.ev > 0 ? 'kv-pos' : strategy.risk.ev < 0 ? 'kv-neg' : ''}">{fmtMoney(strategy.risk.ev)}</span>
            </div>
            {#if strategy.risk.ev_pct != null}
              <div class="kv-pair">
                <span class="kv-k">EV / cost <InfoHint popup text={'<b>EV / cost</b> — EV as a percentage of |net cost|. Return-on-capital expectation. +5 % = "on average, my outlay returns 5 % of itself per cycle".'} /></span>
                <span class="kv-v {strategy.risk.ev_pct > 0 ? 'kv-pos' : strategy.risk.ev_pct < 0 ? 'kv-neg' : ''}">
                  {pctFmt(strategy.risk.ev_pct)}%
                </span>
              </div>
            {/if}
          </div>
          <div class="text-[0.5rem] text-[#7e97b8] mt-1 italic">
            * numerical max/min within
            {#if strategy.span_sigmas > 0}
              ±{strategy.span_sigmas.toFixed(1)}σ
              ({(strategy.span_pct * 100).toFixed(1)}%)
              spot range at expiry
            {:else}
              ±{(strategy.span_pct * 100).toFixed(1)}% spot range
            {/if}
          </div>
        </div>
  </aside>
{/if}


  {#if !strategy && !strategyErr && !legs.length}
    <div class="text-[0.65rem] text-[#7e97b8] italic mb-3">
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
    symbol={_ctxMenu.symbol}
    exchange={_ctxMenu.exchange}
    x={_ctxMenu.x}
    y={_ctxMenu.y}
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
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
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
    font-size: 0.55rem;
    font-weight: 800;
    font-family: ui-monospace, monospace;
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

  /* Chain-launcher slot — single OChain pill aligned with the
     Select-trigger row. (Earlier this hosted a paired BUY (+) /
     SELL (−) toggle; per-row +/− inside the picker now decides
     side, so the outer is just an open / close affordance.) */
  .opt-trade {
    display: inline-flex;
    flex: 0 0 auto;
    align-self: flex-end;
  }
  /* OChain pill — height matches the Select trigger
     (min-height: 1.55rem) so the row reads as one consistent
     control bar. Amber palette to identify it as an action button
     in the same family as the chart-corner Refresh. Wider than
     the old square pills since "OChain" needs the room. */
  .opt-add-btn {
    height: 1.55rem;
    min-height: 1.55rem;
    padding: 0 0.65rem;
    flex: 0 0 auto;
    align-self: flex-end;
    border-radius: 3px;          /* match Select's 3px radius */
    border: 1px solid rgba(251,191,36,0.5);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .opt-add-btn:hover {
    background: rgba(251,191,36,0.22);
    border-color: rgba(251,191,36,0.75);
  }
  /* Disabled state — chain picker requires exactly one account
     selected on the page. Greyed border + faded glyph + not-allowed
     cursor; hover styling suppressed so it doesn't flash on
     mouse-over. */
  .opt-add-btn:disabled,
  .opt-add-btn[disabled] {
    opacity: 0.45;
    cursor: not-allowed;
    background: rgba(126,151,184,0.08);
    border-color: rgba(126,151,184,0.30);
    color: #a3b9d0;
  }
  .opt-add-btn:disabled:hover,
  .opt-add-btn[disabled]:hover {
    background: rgba(126,151,184,0.08);
    border-color: rgba(126,151,184,0.30);
  }
  /* Active (panel-open) — invert the palette so the OChain pill
     visually links to the picker panel below it. */
  .opt-add-btn-on {
    background: #fbbf24;
    color: #0c1830;
    border-color: #fbbf24;
  }

  /* Refresh button moved onto the chart's top-right corner — see
     OptionsPayoff.svelte for its styles. The picker bar now ends
     with the "+" toggle. */

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
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #fbbf24;
    padding: 0 0.25rem 0.4rem;
    flex-wrap: wrap;
  }
  /* Two-row variant — title + chips on row 1, meta line on row 2.
     Each row independently flex-wraps so chips squeeze together
     before pushing the meta line down. */
  .opt-section-h-grid {
    display: grid;
    grid-template-rows: auto auto;
    row-gap: 0.3rem;
  }
  .opt-section-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .opt-section-title {
    color: #fbbf24;
    font-weight: 700;
    font-size: 0.7rem;          /* slightly larger than meta so the
                                   header reads as the section anchor */
    letter-spacing: 0.06em;
  }
  .opt-section-tag {
    font-size: 0.65rem;
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
    .opt-section-tag { font-size: 0.6rem; padding: 1px 3px; letter-spacing: 0.02em; }
  }
  .tag-deriv  { color: #7dd3fc; background: rgba(125,211,252,0.10); }
  .tag-long   { color: #4ade80; background: rgba(74,222,128,0.10); }
  .tag-short  { color: #f87171; background: rgba(248,113,113,0.10); }
  /* Greek chips in the payoff header — distinct cyan tint so they
     read as a different category from the amber net-cost / max-PnL
     chips. Theta + Vega flip to a red variant when negative (short
     premium / long volatility carry the inverse sign convention). */
  .tag-greek      { color: #c4b5fd; background: rgba(196,181,253,0.10); }
  .tag-greek-neg  { color: #fda4af; background: rgba(253,164,175,0.10); }
  .opt-section-meta {
    color: #a3b9d0;
    font-weight: 400;
    font-size: 0.65rem;
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
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid #fbbf24;
    border-radius: 4px;
    padding: 0.5rem 0.65rem;
  }
  .opt-block-h {
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #fbbf24;
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
    color: #a3b9d0;
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.6rem;
    flex: 0 0 auto;
    flex-wrap: nowrap;
  }
  .kv-v {
    color: var(--algo-slate);
    font-size: 0.7rem;
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
    font-size: 0.7rem;
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
    font-size: 0.65rem;
    margin-left: 0.15rem;
    margin-right: 0.5rem;
    text-align: left;
  }
  .kv-pos { color: #4ade80; }
  .kv-neg { color: #f87171; }
  .kv-sub { color: #a3b9d0; font-size: 0.65rem; margin-left: 0.2rem; }

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
       and fill the height delta between header + scroll viewport. */
    .opt-payoff-legs-row:has(.opt-legs-card) .opt-legs-card {
      display: flex;
      flex-direction: column;
    }
    .opt-payoff-legs-row:has(.opt-legs-card) .opt-legs-card .cand-scroll {
      flex: 1;
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
    font-size: 0.65rem;
    color: #a3b9d0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
  }
  :global(.leg-row .field-input) {
    font-size: 0.62rem;
    padding: 0.25rem 0.4rem;
    font-family: monospace;
  }
  .leg-source {
    font-family: monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #a3b9d0;
    text-align: center;
  }
  .leg-source-live   { color: #4ade80; }
  .leg-source-sim    { color: #fbbf24; }
  .leg-source-manual { color: #7dd3fc; }
  .leg-source-draft  { color: #f0abfc; }
  .leg-del {
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    border: 1px solid rgba(248,113,113,0.4);
    background: rgba(248,113,113,0.08);
    color: #f87171;
    font-size: 0.85rem;
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
    scrollbar-color: rgba(251,191,36,0.55) rgba(15, 25, 45, 0.6);
  }
  .cand-scroll::-webkit-scrollbar { height: 10px; width: 10px; }
  .cand-scroll::-webkit-scrollbar-track {
    background: rgba(15, 25, 45, 0.6);
    border-radius: 4px;
  }
  .cand-scroll::-webkit-scrollbar-thumb {
    background: rgba(251,191,36,0.55);
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
    padding: 0 0.25rem 0.5rem;
    cursor: pointer;
    color: #fbbf24;
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: left;
    flex-wrap: wrap;
  }
  .legs-header:hover { color: #fde047; }

  /* Legs / Expiry Action tab strip — same underline pattern shared
     across every sub-tab strip on the algo site (mp-toptab,
     mp-wl-tab, lab-tab, cap-eq-tab, exec-tab). Bloomberg / Sensibull
     / IBKR TWS convention. */
  .legs-tabs {
    display: flex;
    align-items: center;
    gap: 0;
  }
  .legs-tab {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.22rem 0.55rem 0.2rem;
    cursor: pointer;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    transition: color 0.12s, border-color 0.12s;
  }
  .legs-tab:hover { color: var(--algo-slate); }
  .legs-tab-on {
    color: #fbbf24;
    border-bottom-color: #fbbf24;
  }
  .legs-tab-count {
    font-size: 0.55rem;
    font-weight: 800;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: rgba(126, 151, 184, 0.18);
    color: rgba(200, 216, 240, 0.85);
  }
  .legs-tab-on .legs-tab-count {
    background: var(--algo-amber-bg-strong);
    color: #fbbf24;
  }
  /* Alert badge when expiry-close has 1+ rows — red so the
     operator's eye lands on it when contracts need closing. */
  .legs-tab-count-alert {
    background: var(--algo-red-bg-strong);
    color: #f87171;
  }

  /* Underlying chip — sits before the legs/close tabs. Visually
     distinct from the tabs so the operator's eye reads it as
     "what symbol am I looking at" first, then "which view".
     Solid amber pill with a darker background; the separator
     below pushes the tabs visually apart. */
  .legs-underlying-chip {
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    background: var(--algo-amber-bg-strong);
    border: 1px solid var(--algo-amber-border);
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: none;
    line-height: 1;
  }
  .legs-header-sep {
    display: inline-block;
    width: 1px;
    height: 1.1rem;
    background: rgba(200, 216, 240, 0.25);
    margin: 0 0.25rem;
    flex-shrink: 0;
  }

  /* Expiry tab — three-band row tints. Specificity must outrank
     cand-row-long / cand-row-short (hence !important on bg).
     Band semantics:
       close  → amber accent — operator action required
       netted → slate/cool — broker settles, no action needed
       otm    → faded/muted — expires worthless, monitor only
     Legacy cand-row-equity-close / cand-row-commodity-close are kept
     so the existing _expiryStatus-based class assignments still work;
     the new band classes are the canonical path going forward. */
  .cand-row.expiry-band-close {
    background-color: var(--algo-amber-bg-soft) !important;
    box-shadow: inset 3px 0 0 rgba(251, 191, 36, 0.65);
  }
  .cand-row.expiry-band-netted {
    background-color: rgba(125, 145, 184, 0.08) !important;
    box-shadow: inset 3px 0 0 rgba(125, 145, 184, 0.35);
  }
  /* Per-pair tint — each pair of opposite positions inside the
     NETTED band gets one of 5 alternating background tints + a
     matching left-edge accent so the operator can visually map
     "this row cancels that one". Cycle-of-5 means two adjacent
     pairs are always visually distinct. Operator: "in netted, show
     the opposite positions together color coded · you can
     alternate color for each netted opposite positions". */
  .cand-row.expiry-band-netted[data-pair-tint="0"] {
    background-color: rgba(125, 211, 252, 0.10) !important;  /* sky */
    box-shadow: inset 3px 0 0 rgba(125, 211, 252, 0.65);
  }
  .cand-row.expiry-band-netted[data-pair-tint="1"] {
    background-color: rgba(168, 85, 247, 0.10) !important;   /* violet */
    box-shadow: inset 3px 0 0 rgba(168, 85, 247, 0.65);
  }
  .cand-row.expiry-band-netted[data-pair-tint="2"] {
    background-color: rgba(45, 212, 191, 0.10) !important;   /* teal */
    box-shadow: inset 3px 0 0 rgba(45, 212, 191, 0.65);
  }
  .cand-row.expiry-band-netted[data-pair-tint="3"] {
    background-color: rgba(244, 114, 182, 0.10) !important;  /* pink */
    box-shadow: inset 3px 0 0 rgba(244, 114, 182, 0.65);
  }
  .cand-row.expiry-band-netted[data-pair-tint="4"] {
    background-color: rgba(132, 204, 22, 0.10) !important;   /* lime */
    box-shadow: inset 3px 0 0 rgba(132, 204, 22, 0.65);
  }
  .cand-row.expiry-band-otm {
    background-color: transparent !important;
    box-shadow: none;
    opacity: 0.55;
  }
  /* Legacy band aliases — keep while _expiryStatus still references them. */
  .cand-row.cand-row-equity-close {
    background-color: var(--algo-red-bg) !important;
    box-shadow: inset 3px 0 0 rgba(248, 113, 113, 0.65);
  }
  .cand-row.cand-row-commodity-close {
    background-color: rgba(251, 191, 36, 0.10) !important;
    box-shadow: inset 3px 0 0 rgba(251, 191, 36, 0.65);
  }

  /* Band section header — full-width row containing the section
     identity pill + a muted hint to the right. The pill itself
     does the heavy visual work; the row chrome stays minimal. */
  .expiry-band-header {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.55rem 0.45rem 0.4rem;
    margin-top: 0.6rem;
    border-bottom: 1px solid rgba(200, 216, 240, 0.08);
  }
  .expiry-band-header:first-of-type,
  .expiry-band-header-close:first-child {
    margin-top: 0;
  }
  /* Section pill — colored background + border + leading dot glyph
     + label + count badge, all as a single inline-flex chunk so
     the section identity reads at a glance. Each band gets its
     own palette via the modifier rules below. */
  .expiry-band-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.32rem 0.7rem 0.32rem 0.55rem;
    border-radius: 9999px;
    font-family: ui-monospace, monospace;
    line-height: 1;
    border: 1px solid transparent;
  }
  .expiry-band-dot {
    font-size: 0.7rem;
    line-height: 1;
    flex-shrink: 0;
  }
  .expiry-band-label {
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  .expiry-band-count {
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    padding: 0.12rem 0.45rem;
    border-radius: 9999px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .expiry-band-hint {
    font-size: 0.58rem;
    opacity: 0.55;
    font-style: italic;
    color: var(--algo-muted);
  }
  /* TO CLOSE — amber pill, glowing. Highest-attention band:
     these positions need broker action before expiry. */
  .expiry-band-header-close .expiry-band-pill {
    background: var(--algo-amber-bg-strong);
    border-color: var(--algo-amber-border);
    color: var(--algo-amber);
    box-shadow: 0 0 6px rgba(251, 191, 36, 0.30);
  }
  .expiry-band-header-close .expiry-band-count {
    background: rgba(251, 191, 36, 0.30);
    color: #fed7aa;
    border: 1px solid var(--algo-amber-border);
  }
  /* NETTED — slate pill, balanced. Mid-attention band: positions
     cancel each other at settlement, operator should see the pair
     structure but no action needed. */
  .expiry-band-header-netted .expiry-band-pill {
    background: rgba(125, 145, 184, 0.18);
    border-color: rgba(125, 145, 184, 0.42);
    color: #c8d8f0;
  }
  .expiry-band-header-netted .expiry-band-count {
    background: rgba(125, 145, 184, 0.30);
    color: #c8d8f0;
    border: 1px solid rgba(125, 145, 184, 0.45);
  }
  /* OUT OF THE MONEY — muted pill, lowest visual weight.
     Traceability only; these expire worthless. */
  .expiry-band-header-otm .expiry-band-pill {
    background: rgba(126, 151, 184, 0.10);
    border-color: rgba(126, 151, 184, 0.28);
    color: var(--algo-muted);
  }
  .expiry-band-header-otm .expiry-band-count {
    background: rgba(126, 151, 184, 0.22);
    color: var(--algo-muted);
    border: 1px solid rgba(126, 151, 184, 0.35);
  }

  /* Tag chip inside the symbol cell — #N1 / #C1. */
  .expiry-id-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    margin-left: 0.25rem;
    line-height: 1;
    vertical-align: middle;
  }
  .expiry-id-close {
    background: var(--algo-amber-bg-strong);
    color: #fbbf24;
    border: 1px solid var(--algo-amber-border-soft);
  }
  .expiry-id-netted {
    background: rgba(125, 145, 184, 0.15);
    color: #94a3b8;
    border: 1px solid rgba(125, 145, 184, 0.3);
  }
  /* .legs-chevron retired — no callsites. */

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
      minmax(max-content, max-content)     /* symbol (hyphenated, carries expiry) */
      minmax(max-content, max-content)     /* account */
      minmax(48px, max-content)            /* qty */
      minmax(62px, max-content)            /* ltp */
      minmax(62px, max-content)            /* prev close */
      minmax(62px, max-content)            /* avg (cost basis) */
      minmax(72px, max-content)            /* day pnl - cumulative */
      minmax(72px, max-content)            /* day pnl delta - today */
      minmax(52px, max-content)            /* iv */
      minmax(56px, max-content)            /* delta */
      minmax(56px, max-content)            /* gamma */
      minmax(62px, max-content)            /* theta */
      minmax(56px, max-content);           /* vega */
    column-gap: 0.6rem;
    row-gap: 0.2rem;
    width: max-content;
  }
  /* When the operator filters to a single account, the Account
     column is implicit (every row carries the same value) — drop
     the column entirely. */
  .cand-grid-noacct {
    /* Expiry column also removed in the no-account-column variant. */
    grid-template-columns:
      auto                                 /* checkbox */
      minmax(max-content, max-content)     /* symbol (carries expiry) */
      minmax(48px, max-content)            /* qty */
      minmax(62px, max-content)            /* ltp */
      minmax(62px, max-content)            /* prev close */
      minmax(62px, max-content)            /* avg (cost basis) */
      minmax(72px, max-content)            /* day pnl */
      minmax(72px, max-content)            /* day pnl delta */
      minmax(52px, max-content)            /* iv */
      minmax(56px, max-content)            /* delta */
      minmax(56px, max-content)            /* gamma */
      minmax(62px, max-content)            /* theta */
      minmax(56px, max-content);           /* vega */
  }
  /* TOTAL row — always last, visually distinct (top border + bolder
     text) so the operator sees the roll-up at a glance. The two pnl
     columns sum to the strip's P + P∆ chips for the same accounts.
     position:sticky pins the row to the bottom of the .cand-scroll
     container so the operator always sees the roll-up even when the
     candidate list is long enough to scroll. Background is slightly
     more opaque than the in-line variant so it reads as a solid pill
     against the scrolling rows underneath. */
  .cand-row.cand-row-total {
    border-top: 2px solid var(--algo-amber-border);
    background: rgba(20, 28, 48, 0.96);
    box-shadow: 0 -4px 8px rgba(0, 0, 0, 0.35);
    font-weight: 700;
    margin-top: 0.2rem;
    padding-top: 0.35rem;
    position: sticky;
    bottom: 0;
    z-index: 2;
  }
  .cand-row.cand-row-total::after {
    /* Amber tint overlay — same colour family as the in-line variant,
       layered on top of the dark base so the tint shows through but
       the rows underneath don't bleed in. */
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(251, 191, 36, 0.08);
    pointer-events: none;
  }
  .cand-total-label {
    color: #fbbf24;
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  /* Split-row tags — small chip beside the symbol, indicates whether
     this row is the closed half or the open half of a close-and-
     reopen sequence today. */
  .cand-split-tag {
    display: inline-block;
    margin-left: 0.35rem;
    padding: 0 0.3rem;
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    vertical-align: middle;
  }
  .cand-split-closed {
    color: #f87171;
    background: var(--algo-red-bg);
    border: 1px solid rgba(248, 113, 113, 0.45);
  }
  .cand-split-open {
    color: #4ade80;
    background: var(--algo-green-bg);
    border: 1px solid rgba(74, 222, 128, 0.45);
  }
  /* Cell-level truncation so numeric tracks can shrink below their
     natural max-content without breaking row layout. Scoped to
     `.num` only — applying it row-wide also clipped .cand-actions
     and the SymbolActions popover menu inside it (the menu is
     absolutely positioned but `overflow: hidden` on its ancestor
     still clips it visually). Symbol and Account don't need
     truncation because their column min stays at max-content. */
  .cand-headrow > .num,
  .cand-row > .num {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Single parent grid via subgrid. Each row inherits the parent's
     column tracks — so headers and data cells line up exactly,
     regardless of which row has the longest content per column.
     Earlier each row was its own `display: grid` with `max-content`
     which sized columns per-row → header columns drifted out of
     alignment with data columns. */
  .cand-headrow,
  .cand-row {
    display: grid;
    grid-template-columns: subgrid;
    grid-column: 1 / -1;
    /* Subgrid inherits column-gap from .cand-grid (0.6rem). Don't
       set `gap` here — that overrides the parent and decouples the
       rows' spacing from the header's. */
    padding: 0.1rem 0.2rem;
    align-items: center;
    font-size: 0.62rem;
    font-family: monospace;
    font-variant-numeric: tabular-nums;
  }
  .cand-headrow {
    font-size: 0.65rem;
    color: #a3b9d0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
    /* Sticky header — operator scrolls data rows under it instead of
       the whole grid sliding up. Pinned to top of .cand-scroll (the
       overflow-y container); the card-bottom navy of the parent card
       gradient is reused as a solid fill so data rows don't bleed
       through, and z-index lifts the header above .cand-row hovers. */
    position: sticky;
    top: 0;
    z-index: 2;
    background: #1d2a44;
  }
  /* Numeric column cells — right-aligned (industry-standard for
     trade panels) so digits in different rows line up cleanly under
     each column header. tabular-nums on the row already keeps glyph
     widths even, so a "+12,500" lands directly above a "−300" with
     digits stacked in the same columns. */
  .cand-headrow > .num,
  .cand-row > .num {
    text-align: right;
    justify-self: end;
  }
  .cand-row {
    padding: 0.2rem 0.3rem;
    border-radius: 3px;
    cursor: pointer;
    transition: background 0.1s;
  }
  .cand-row:hover { background: rgba(251,191,36,0.05); }
  /* Closed positions (qty=0) — sorted to end of list, kept
     visible for context. Dim them so live rows pop, and disable
     the click-to-close affordance (no exposure to close). */
  .cand-row.cand-closed {
    opacity: 0.45;
    cursor: default;
  }
  .cand-row.cand-closed:hover { background: transparent; }

  /* Long / short row tint — mirrors the /dashboard ag-theme-algo
     palette: sky-cyan for long positions, warm-orange for short.
     Faint left + right inset bars on the row scope the direction
     cue to the row body without flooding the whole table. */
  .cand-row-long {
    background-color: rgba(56,189,248,0.08);
    box-shadow: inset 3px 0 0 rgba(56,189,248,0.75),
                inset -3px 0 0 rgba(56,189,248,0.75);
  }
  .cand-row-short {
    background-color: rgba(251,146,60,0.08);
    box-shadow: inset 3px 0 0 rgba(251,146,60,0.75),
                inset -3px 0 0 rgba(251,146,60,0.75);
  }
  .cand-row-long:hover  { background-color: rgba(56,189,248,0.16); }
  .cand-row-short:hover { background-color: rgba(251,146,60,0.16); }

  /* Draft rows — distinct from live / sim positions: dashed
     magenta inset bar on the LEFT only (not both edges like
     long/short), faint magenta-tinted background, and a slim
     row-level dashed left border so even a flat-zero draft
     reads as "this isn't a real position". Magenta matches the
     `leg-source-draft` text colour `#f0abfc` used on the leg
     panel + the draft input rows above. */
  .cand-row.cand-draft {
    background-color: rgba(240,171,252,0.06);
    box-shadow: inset 4px 0 0 rgba(240,171,252,0.85);
    /* Override the long/short tint so the draft cue wins. */
  }
  .cand-row.cand-draft.cand-row-long,
  .cand-row.cand-draft.cand-row-short {
    background-color: rgba(240,171,252,0.06);
    box-shadow: inset 4px 0 0 rgba(240,171,252,0.85);
  }
  .cand-row.cand-draft:hover {
    background-color: rgba(240,171,252,0.14);
  }

  /* Draft × — sits inline with the symbol, lets operator discard
     a draft without going through the OrderTicket modal. Magenta
     to match the draft row identity. */
  .cand-sym {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .cand-draft-x {
    flex: 0 0 auto;
    width: 1.1rem;
    height: 1.1rem;
    padding: 0;
    border-radius: 2px;
    border: 1px solid rgba(240,171,252,0.45);
    background: rgba(240,171,252,0.10);
    color: #f0abfc;
    font-family: monospace;
    font-size: 0.8rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .cand-draft-x:hover {
    background: rgba(240,171,252,0.22);
    border-color: rgba(240,171,252,0.75);
    color: #fff;
  }
  /* Candidate row's ⋯ actions container — keeps the popover button
     visually grouped at the right edge of the row. */
  .cand-actions {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
  }

  /* P&L cell — same green/red scheme as /dashboard's pnl-gain /
     pnl-loss classes. Subtle background tint for a glanceable
     "win or lose?" cue at row-scan speed; bold weight so the
     numbers pop alongside the otherwise-muted row content. */
  .cand-pnl {
    border-radius: 2px;
    padding: 0 0.25rem;
    font-weight: 700;
  }
  /* Background tint for P&L cells in the Candidates grid (colour comes
     from the global cell-pos / cell-neg / cell-flat rules in MarketPulse). */
  :global(.cand-pnl.cell-pos)  { background-color: rgba(74,222,128,0.10); }
  :global(.cand-pnl.cell-neg)  { background-color: rgba(248,113,113,0.10); }
  :global(.cand-pnl.cell-flat) { background-color: rgba(148,163,184,0.08); }

  /* Symbol-cell treatment ported from the Pulse Positions grid so the
     two surfaces look identical at a glance — flat hyphenated symbol
     via formatSymbol (no structured LegLabel chips), CE/PE text
     tinting, account-tint background. ONE vertical right border per
     symbol cell encoding TODAY's P&L direction (day-pnl mini-bar) —
     this border applies across both tabs (legs / exp close).
     Account identity stays in the trailing Account column
     so we don't need a second right border for it.
     `--cand-acct-color` is set per-row via inline style from the
     account's hash colour (acctColor from $lib/account). */
  .cand-sym-acct {
    position: relative;
    background-color: color-mix(in srgb, var(--cand-acct-color, transparent) 14%, transparent);
  }
  /* CE / PE text tint on the symbol main (Sensibull / Streak convention,
     same colours used everywhere else for sym-main). */
  :global(.cand-sym .sym-main)        { color: #e2e8f0; font-weight: 600; }
  :global(.cand-sym .sym-main.sym-ce) { color: #4ade80; }
  :global(.cand-sym .sym-main.sym-pe) { color: #f87171; }
  /* SINGLE vertical right border on the symbol cell, encoding
     POSITION DIRECTION (long vs short). 2 px wide, flush against the
     right edge so it reads as a clean cell-edge border. Green when
     qty > 0 (long), red when qty < 0 (short), NO border when qty = 0
     (flat) — same idiom Pulse Positions uses. Operator: "I want the
     gray border to go away" — flat rows now render with no right
     border at all. Applies in every tab the cand-row renders in
     (legs / exp close). */
  .cand-row.cand-row-long  .cand-sym-acct::after,
  .cand-row.cand-row-short .cand-sym-acct::after {
    content: '';
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 2px;
    pointer-events: none;
  }
  .cand-row.cand-row-long  .cand-sym-acct::after { background: rgba(74, 222, 128, 0.85); }
  .cand-row.cand-row-short .cand-sym-acct::after { background: rgba(248, 113, 113, 0.85); }

  .cand-row input[type="checkbox"] {
    accent-color: #fbbf24;
    width: 0.9rem;
    height: 0.9rem;
    cursor: pointer;
  }
  .cand-disabled {
    opacity: 0.45;
  }
  .cand-disabled:hover { background: rgba(248,113,113,0.05); }
  /* .cand-kind[-fut|-opt] + .cand-row-btn + .cand-row-active +
     .cand-row-disabled + .cand-bullet retired — replaced by the
     checkbox-driven multi-select Candidates panel. */

  .leg-type-CE {
    color: #4ade80;
    background: rgba(74,222,128,0.10);
    border: 1px solid rgba(74,222,128,0.4);
    border-radius: 2px;
    padding: 0 4px;
    font-weight: 700;
    font-size: 0.65rem;
  }
  .leg-type-PE {
    color: #f87171;
    background: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.4);
    border-radius: 2px;
    padding: 0 4px;
    font-weight: 700;
    font-size: 0.65rem;
  }

  /* "Clear" button styled subtly red so the destructive action stands
     out from "+ Add row" / "Analyze" without being scary. */
  :global(.opt-clear) {
    border-color: rgba(248,113,113,0.45) !important;
    color: #f87171 !important;
  }
  :global(.opt-clear:hover) {
    background: rgba(248,113,113,0.10) !important;
  }

  /* Stale-LTP / fallback-source chips — surfaced when broker live price
     wasn't available and the engine fell back (close/depth/avg_cost/
     default IV). Lets the operator know which numbers to treat with
     extra care, without burying the result. */
  .src-chip {
    margin-left: 0.5rem;
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
  }
  .src-stale {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
  }
  .src-tag {
    margin-left: 0.3rem;
    font-family: monospace;
    font-size: 0.6rem;
    color: #a3b9d0;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .src-warn { color: #fbbf24; font-weight: 700; }
  /* When .src-warn is paired with .src-chip (background context), give
     it the same amber-tinted background as .src-stale so the chip looks
     like a chip and not just amber text floating on the panel. */
  .src-chip.src-warn { background: rgba(251,191,36,0.14); }

  /* Per-leg LTP source pill — fresh = sky-blue, stale = amber. Sits in
     its own column on the breakdown table. */
  .leg-src {
    display: inline-block;
    font-family: monospace;
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
    font-weight: 700;
  }
  .leg-src-fresh { color: #7dd3fc; background: rgba(125,211,252,0.10); }
  .leg-src-stale { color: #fbbf24; background: rgba(251,191,36,0.10); }

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
      font-size: 0.55rem;
    }
    .chain-controls :global(.rbq-select-trigger),
    .chain-controls :global(.rbq-multi-trigger) {
      font-size: 0.62rem;
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
    font-size: 0.65rem;
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
    font-size: 0.55rem;
    color: #a3b9d0;
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
    font-size: 0.65rem;
    /* Fixed layout + explicit colgroup widths so the CE and PE
       columns are the SAME width on every row. Without this, the
       inner edges (where bid-ask quotes sit) drift across rows
       depending on per-row content width. */
    table-layout: fixed;
  }
  .chain-grid col.chain-col-strike { width: 4.4rem; }
  .chain-grid col.chain-col-ce,
  .chain-grid col.chain-col-pe { width: calc((100% - 4.4rem) / 2); }
  .chain-grid th {
    position: sticky;
    top: 0;
    z-index: 2;
    /* Stack a solid panel base under the amber tint so rows don't
       bleed through when the body scrolls under the sticky header.
       The panel gradient base is #1d2a44; matching it here keeps the
       header visually contiguous with the surrounding card. */
    background:
      linear-gradient(rgba(251,191,36,0.10), rgba(251,191,36,0.10)),
      #1d2a44;
    color: #a3b9d0;
    font-weight: 700;
    text-transform: uppercase;
    font-size: 0.65rem;
    letter-spacing: 0.04em;
    padding: 0.25rem 0.4rem;
    border-bottom: 1px solid rgba(251,191,36,0.45);
    box-shadow: 0 2px 0 rgba(0,0,0,0.25);
  }
  .chain-th-ce     { text-align: left; color: #4ade80; }
  .chain-th-pe     { text-align: right; color: #f87171; }
  .chain-th-strike { text-align: center; color: var(--algo-slate); }
  .chain-grid td {
    padding: 0.18rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .chain-grid tr:last-child td { border-bottom: 0; }
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
    color: #fbbf24;
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
    font-size: 0.6rem;
    font-weight: 600;
    white-space: nowrap;
    text-align: center;
  }
  .chain-cell-bid { color: #4ade80; }   /* same green as CE header */
  .chain-cell-ask { color: #f87171; }   /* same red as PE header */
  .chain-cell-sep {
    color: var(--algo-muted);
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
  .chain-row-itm-call > td { background: rgba(56,189,248,0.05); }
  .chain-row-itm-put  > td { background: rgba(251,146,60,0.05); }
  .chain-row-atm > td {
    background: rgba(251,191,36,0.18);
    border-top:    1px solid rgba(251,191,36,0.55);
    border-bottom: 1px solid rgba(251,191,36,0.55);
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
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.55);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
  }
  .chain-btn {
    font-family: monospace;
    font-size: 0.65rem;
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
  .chain-btn-buy  { color: #4ade80; }
  .chain-btn-sell { color: #f87171; }
  .chain-btn-buy:hover  { background: rgba(74,222,128,0.10); }
  .chain-btn-sell:hover { background: rgba(248,113,113,0.10); }
  /* Info button — sky-blue, neutral. Opens the full OrderTicket
     pre-filled (advanced path, when the operator wants to edit
     qty / limit price / chase / mode before placing). */
  .chain-btn-info {
    color: #7dd3fc;
    font-style: italic;
    padding: 1px 5px;
  }
  .chain-btn-info:hover { background: rgba(125,211,252,0.10); }
  /* Watchlist button — amber, sits next to the "i" info button.
     One click adds the contract to the user's default watchlist. */
  .chain-btn-watch {
    color: #fbbf24;
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
    color: #4ade80;
    font-family: monospace;
    font-size: 0.6rem;
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
    font-size: 0.6rem;
    line-height: 1.5;
    cursor: pointer;
    user-select: none;
    transition: background 0.12s, transform 0.05s;
  }
  .chain-basket-leg:focus-visible {
    outline: 2px solid #fbbf24;
    outline-offset: 1px;
  }
  .chain-basket-leg:hover:not(.is-disabled) {
    background: rgba(248,113,113,0.10);
    transform: translateY(-1px);
  }
  .chain-basket-leg:hover:not(.is-disabled) .chain-basket-sym::after {
    content: ' ✕';
    color: #f87171;
    margin-left: 0.15rem;
    font-weight: 700;
  }
  .chain-basket-leg.is-disabled {
    cursor: progress;
    opacity: 0.55;
  }
  /* Outline + side text colour by SIDE (chain-btn-buy / -sell green /
     red, same as the strike-row buttons). */
  .chain-basket-leg-buy  { color: #4ade80; background: rgba(74,222,128,0.06); }
  .chain-basket-leg-sell { color: #f87171; background: rgba(248,113,113,0.06); }
  /* Left-border accent by TYPE (CE green / PE red / FUT sky-blue,
     matching the strike header palette + OrderTicket option-type
     pills). */
  .chain-basket-leg-type-ce  { border-left-color: #4ade80; }
  .chain-basket-leg-type-pe  { border-left-color: #f87171; }
  .chain-basket-leg-type-fut { border-left-color: #7dd3fc; }
  .chain-basket-side {
    font-weight: 800;
    letter-spacing: 0.04em;
  }
  .chain-basket-sym {
    color: var(--algo-slate);
    font-weight: 600;
  }
  .chain-basket-qty {
    color: #a3b9d0;
    font-size: 0.58rem;
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
    font-size: 0.7rem;
    font-weight: 700;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .chain-basket-step:hover:not(:disabled) {
    background: rgba(255,255,255,0.05);
  }
  .chain-basket-step:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-lots {
    min-width: 1.1rem;
    text-align: center;
    color: #fbbf24;
    font-family: monospace;
    font-weight: 700;
    font-size: 0.62rem;
    font-variant-numeric: tabular-nums;
  }
  /* Algo-selected limit price — static, not editable. Auto-seeded
     from chain bid/ask at add-time; shows "@MKT" when no quote was
     available so the operator knows that leg routes as MARKET. */
  .chain-basket-limit-static {
    color: #fbbf24;
    font-family: monospace;
    font-size: 0.62rem;
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
    color: #a3b9d0;
    font-family: monospace;
    font-size: 0.55rem;
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
    color: #a3b9d0;
    font-family: monospace;
    font-size: 0.55rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .chain-basket-chase-pill:hover:not(:disabled):not(.on) {
    background: rgba(255,255,255,0.05);
  }
  .chain-basket-chase-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-chase-pill-low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; border-color: rgba(125,211,252,0.55); }
  .chain-basket-chase-pill-med.on  { background: rgba(251,191,36,0.20); color: #fbbf24; border-color: rgba(251,191,36,0.55); }
  .chain-basket-chase-pill-high.on { background: rgba(74,222,128,0.20); color: #4ade80; border-color: rgba(74,222,128,0.55); }
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
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .chain-basket-clear { color: #a3b9d0; }
  .chain-basket-clear:hover { background: rgba(163,185,208,0.08); }
  .chain-basket-place { color: #fbbf24; background: rgba(251,191,36,0.10); }
  .chain-basket-place:hover    { background: rgba(251,191,36,0.20); }
  .chain-basket-place:disabled,
  .chain-basket-clear:disabled { opacity: 0.55; cursor: progress; }
  .chain-basket-err {
    flex: 1 1 100%;
    color: #f87171;
    font-family: monospace;
    font-size: 0.6rem;
    margin-top: 0.2rem;
  }
  .chain-basket-toast {
    margin-top: 0.5rem;
    padding: 0.3rem 0.5rem;
    border-radius: 2px;
    background: rgba(74,222,128,0.14);
    color: #4ade80;
    font-family: monospace;
    font-size: 0.65rem;
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
    font-size: 0.65rem;
    box-sizing: border-box;
  }
  .chain-lots-input:focus {
    outline: none;
    border-color: rgba(251,191,36,0.55);
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
  .order-toast {
    pointer-events: auto;
    background: rgba(13,21,38,0.96);
    border: 1px solid rgba(167,139,250,0.55);
    border-left: 3px solid #a78bfa;
    border-radius: 0.35rem;
    padding: 0.45rem 0.65rem 0.4rem;
    box-shadow: 0 6px 18px rgba(0,0,0,0.45);
    font-family: ui-monospace, monospace;
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
  .order-toast-filled   { border-left-color: #4ade80; border-color: rgba(74,222,128,0.55); }
  .order-toast-unfilled { border-left-color: #f87171; border-color: rgba(248,113,113,0.55); }
  .order-toast-rejected { border-left-color: #f87171; border-color: rgba(248,113,113,0.55); }

  .order-toast-head {
    display: flex; align-items: center; gap: 0.45rem;
    font-size: 0.55rem;
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
    background: rgba(167,139,250,0.18);
    color: #c4b5fd;
    border: 1px solid rgba(167,139,250,0.4);
  }
  .order-toast-mode-paper { background: rgba(56,189,248,0.18);  color: #7dd3fc; border-color: rgba(56,189,248,0.4); }
  .order-toast-mode-live  { background: rgba(248,113,113,0.18); color: #fca5a5; border-color: rgba(248,113,113,0.5); }
  /* FILL toast — emerald, fired when a position_filled ws event lands.
     Either replaces an in-flight placement toast (updates its status to
     FILLED in place) or pushes fresh when the fill came from an algo
     path the operator didn't manually place. */
  .order-toast-mode-fill  { background: rgba(74,222,128,0.18);  color: #4ade80; border-color: rgba(74,222,128,0.5); }
  .order-toast-status { font-weight: 600; }
  .order-toast-close {
    margin-left: auto;
    width: 1.2rem; height: 1.2rem;
    background: transparent;
    border: 0;
    color: rgba(200,216,240,0.7);
    cursor: pointer;
    font-size: 0.85rem;
    line-height: 1;
    border-radius: 0.2rem;
    padding: 0;
  }
  .order-toast-close:hover { color: #fff; background: rgba(255,255,255,0.08); }

  .order-toast-body {
    display: flex; align-items: baseline; gap: 0.4rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
  .order-toast-side-buy  { color: #4ade80; }
  .order-toast-side-sell { color: #f87171; }
  .order-toast-qty { color: #fbbf24; }
  .order-toast-sym { color: #e5edf7; }
  .order-toast-px  { color: var(--algo-slate); opacity: 0.9; }

  .order-toast-foot {
    margin-top: 0.2rem;
    font-size: 0.55rem;
    color: rgba(200,216,240,0.55);
  }
  .order-toast-oid { font-family: ui-monospace, monospace; }
</style>
