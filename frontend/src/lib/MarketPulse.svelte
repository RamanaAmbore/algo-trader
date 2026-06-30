<script>
  // Unified market-pulse component shared by /pulse (default preset)
  // and /dashboard (Phase 2 preset).
  //
  // A single ag-Grid lists every symbol the operator is tracking,
  // with per-row source badges (W=watchlist, H=holding, P=position,
  // U=option-underlying) so the same symbol never appears twice.
  // The merge engine, batch-quote pipeline, symbol-cell renderer,
  // row-tint CSS, and SymbolPanel wiring all live here — pages
  // compose by toggling props:
  //
  //   enableWatchlists    — show tab strip / add row / remove ×
  //   enableSourceToggles — show "P · Positions" / "H · Holdings" pills
  //   allowOrders         — row click opens SymbolPanel
  //
  // Phase 2 additions (not wired yet): accountFilter, showSummaryRows,
  // showFundsCard.

  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist,
    deleteWatchlist, renameWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchPositions, fetchHoldings, fetchAccounts, batchQuote,
    fetchWatchlistQuotes,
  } from '$lib/api';
  import { visibleInterval, marketAwareInterval, connStatus, authStore, selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { instrumentsCacheVersion } from '$lib/data/instruments';

  // Module-scope cache for hyphenated symbol display. The cellRenderer
  // re-runs for every row × redraw; parsing each symbol once per session
  // keeps the render hot path O(1). Invalidated when the instruments
  // cache populates (see effect below) — otherwise the first paint
  // pins the cold-cache form (no expiry day) and never picks up the
  // per-symbol day once the dump loads.
  const _pulseSymFmtCache = new Map();
  function _pulseFmtSym(/** @type {string} */ s) {
    if (!s) return '';
    if (_pulseSymFmtCache.size > 600) _pulseSymFmtCache.clear();
    let v = _pulseSymFmtCache.get(s);
    if (v === undefined) {
      v = formatSymbol(s);
      _pulseSymFmtCache.set(s, v);
    }
    return v;
  }
  import { fetchSettings } from '$lib/api';
  import { streamOpen, startQuoteStream, stopQuoteStream } from '$lib/data/quoteStream';
  import { createFreshnessShimmer } from '$lib/data/tickFlash.svelte.js';
  import { getSnapshot, symbolStore, symbolTickCount } from '$lib/data/symbolStore.svelte.js';
  import { bookChanged } from '$lib/data/bookChanged';
  import {
    positionsStore, holdingsStore, fundsStore,
    moversStore, activeListsStore, sparklinesStore,
    publishWatchQuotes, publishPulseQuotes, publishPositionsRows, publishHoldingsRows,
  } from '$lib/data/marketDataStores.svelte.js';
  import { resolveUnderlying, INDEX_LTP_KEY, MCX_COMMODITIES, CDS_CURRENCIES } from '$lib/data/resolveUnderlying';
  import CardControls from '$lib/CardControls.svelte';
  import { createPerformanceSocket } from '$lib/ws';
  import { lastRefreshAt } from '$lib/stores';
  import { priceFmt, pctFmt, aggCompact, qtyFmt, directional } from '$lib/format';
  import { acctColor, leadAccount } from '$lib/account';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import Select      from '$lib/Select.svelte';
  import { openActivityModal } from '$lib/stores';
  import ChartModal from '$lib/ChartModal.svelte';

  let {
    title              = 'Pulse',
    enableWatchlists   = true,
    enableMovers       = true, // gate the Movers source pill + 30s mover poll
    enablePinned       = true, // gate the Pinned source pill (indices + commodities + USDINR)
    enableSourceToggles = true,
    allowOrders        = true,
    // Phase 2 presets — dashboard mode turns these on.
    accountPicker      = false, // <select> next to the toolbar
    showSummary        = false, // small per-account summary grid above the main grid
    showFunds          = false, // small per-account funds grid below the main grid
    showSymbolsGrid    = true,  // unified symbols grid at the bottom; /dashboard passes false
    // /pulse passes `flat=true` to drop the outer .algo-status-card
    // chrome — the unified grid (with its own fixed-header scroll
    // behaviour) becomes the page's primary surface instead of being
    // boxed inside a non-scrolling navy card.
    flat               = false,
  } = $props();

  // AG Grid valueFormatter wrappers — the canonical idiom every other
  // algo grid uses. Single source of truth for how numbers render:
  // no `+` prefix on positives (colour carries direction), no `₹`
  // prefix, en-IN grouping, '—' for null.
  const numFmt     = ({ value }) => value == null ? '—' : priceFmt(value);
  const aggFmtGrid = ({ value }) => value == null ? '—' : aggCompact(value);
  const pctFmtGrid = ({ value }) => value == null ? '—' : `${pctFmt(value)}%`;
  const numericHdr = 'ag-right-aligned-header';

  ModuleRegistry.registerModules([AllCommunityModule]);

  let lists       = $state(/** @type {any[]} */ ([]));
  // Multi-select model — operator can include any combination of
  // their saved watchlists in the unified view. activeIds is now
  // $derived from selectedShow further down — toggling a wl: token
  // in the Show MultiSelect flips this. Fetched list contents land
  // in `activeLists` so buildUnified can iterate every selected list;
  // `watchQuotes` stays a flat itemId→quote map (item ids are
  // globally unique) populated by unioning the per-list /quotes
  // responses.
  // activeLists / watchQuotes / positions / holdings / funds / movers /
  // sparklines are now three-tier stores (slice AB). $derived reads are
  // reactive and pre-populated from localStorage on module init, so the
  // grids paint with cached data before any network fetch completes.
  const activeLists = $derived(activeListsStore.value ?? []);
  // BH4: watchQuotesStore is deleted. Per-item LTPs land in symbolStore
  // (via SSE + the inline loadQuotes() fetcher below + every other
  // market-data publisher). buildUnified reads getSnapshot(sym) directly;
  // no derived store mirror is needed.
  // "Target" list for add / rename / delete operations. Defaults to
  // the first selected list; updated when the operator clicks a tab
  // (set focus AND toggle inclusion in one click).
  let focusedListId = $state(/** @type {number | null} */ (null));
  // Bridge positionsStore / holdingsStore through $effect → $state so that
  // when loadPulse() calls positionsStore.set(), the downstream unifiedRows
  // $derived.by (which runs buildUnified — a heavy computation) is scheduled
  // by Svelte's microtask queue rather than firing synchronously inside the
  // same long task as the store write. Without this bridge the synchronous
  // $derived cascade blocks the main thread for >100 ms, freezing the
  // RefreshButton spinner mid-animation (RAIL long-task violation).
  let positions = $state(/** @type {any[]} */ (positionsStore.value ?? []));
  let holdings  = $state(/** @type {any[]} */ (holdingsStore.value  ?? []));
  $effect(() => {
    const p = positionsStore.value;
    const h = holdingsStore.value;
    untrack(() => {
      positions = p ?? [];
      holdings  = h ?? [];
    });
  });
  // Pulse quote bag — bundles BOTH option-underlying quotes (keyed
  // by logical underlying name, each value carries a `_resolved`
  // info block from resolveUnderlying) AND contract quotes (keyed
  // by `${exchange}:${tradingsymbol}`). Single $state object so a
  // single assignment in loadPulse triggers exactly one buildUnified
  // recomputation per tick.
  let pulseQuotes = $state(/** @type {{underlyings: Record<string, any>, contracts: Record<string, any>}} */ ({ underlyings: {}, contracts: {} }));
  let pulseLastUpdate  = $state(/** @type {number | null} */ (null));
  let agoTick          = $state(0);
  let refreshedAt = $state('');
  let error       = $state('');
  // Per-feed broker error (positions / holdings / funds) — surfaced
  // in a banner above the chrome row so the operator knows when the
  // strip + grids are stale because the broker layer is down.
  let brokerErr   = $state('');

  // True when the current user is admin or designated — those are the
  // only roles that can mutate the shared global Pinned watchlist
  // (alias edits, item add / remove, rename, item-reorder). The
  // backend gates the same way; the frontend hides affordances so
  // non-admins don't get a 403 surprise.
  const _isDesignated = $derived.by(() => {
    const r = String($authStore.user?.role || '').toLowerCase();
    return r === 'admin' || r === 'designated';
  });

  // Add-symbol form state.
  let symInput   = $state('');
  // Optional display name (operator's nickname for the contract). Sent
  // as `alias` to /api/watchlist/{id}/items when present. Empty means
  // the grid shows the raw tradingsymbol.
  let aliasInput = $state('');
  // Instrument-TYPE picker shown next to the symbol input. EQ → cash
  // equity, FU → future, CE / PE → option call / put. Maps to an
  // exchange in addRow() (EQ → NSE, others → NFO) when the operator
  // direct-adds without picking from the typeahead. Typeahead picks
  // override with the instrument's actual exchange.
  let typeInput  = $state(/** @type {'EQ'|'FU'|'CE'|'PE'} */ ('EQ'));
  let typeahead  = $state(/** @type {any[]} */ ([]));
  let typeaheadOpen = $state(false);
  // Target watchlist for the next add. Either an existing list id or
  // the literal 'NEW' (reveals the inline new-list name input). Seeded
  // from the user's default watchlist when the Add popup opens.
  let targetListId = $state(/** @type {number | 'NEW' | null} */ (null));
  // Two-click delete confirmation for the watchlist delete button in
  // the Add popup. First click arms it (4s window); second click
  // actually deletes. Cleared on timeout or success.
  let _pendingDeleteId = $state(/** @type {number | null} */ (null));
  // Search popup — opened by the magnifier button at the top of the
  // header row (and by the `/` keyboard shortcut). Houses the symbol
  // input + exchange picker + typeahead + Add button. Hiding the
  // always-on form clears ~30 px of vertical chrome from the page
  // header so the grid claims more screen real estate.
  let searchOpen = $state(false);

  // Option picker — shown inline when the operator picks an underlying
  // from the typeahead (i.e. a symbol that has CE/PE chains).
  /** @type {{ name: string, exchange: string } | null} */
  let optionPickerUnderlying = $state(null);
  let optionPickerExpiry     = $state('');
  let optionPickerSide       = $state(/** @type {'CE'|'PE'} */ ('CE'));
  let optionPickerStrike     = $state(/** @type {number|null} */ (null));
  let optionPickerExpiries   = $state(/** @type {string[]} */ ([]));
  let optionPickerStrikes    = $state(/** @type {number[]} */ ([]));

  // Reload strikes when expiry changes (side change intentionally does
  // NOT reset strike — operator may want the same strike in CE↔PE).
  $effect(() => {
    void optionPickerExpiry; void optionPickerSide;
    if (!optionPickerUnderlying || !optionPickerExpiry) {
      optionPickerStrikes = []; optionPickerStrike = null; return;
    }
    import('$lib/data/instruments').then(({ listStrikes }) => {
      const strikes = listStrikes(
        optionPickerUnderlying.name.toUpperCase(),
        optionPickerSide,
        optionPickerExpiry,
      );
      optionPickerStrikes = strikes;
      // If the current strike is still in the new list, keep it.
      if (optionPickerStrike != null && strikes.includes(optionPickerStrike)) return;
      // Otherwise default to ATM (nearest to current spot).
      const spot = pulseQuotes.underlyings[optionPickerUnderlying.name.toUpperCase()]
                     ?.last_price
                ?? pulseQuotes.underlyings[optionPickerUnderlying.name]
                     ?.last_price
                ?? null;
      if (spot != null && strikes.length) {
        let best = strikes[0], bestDiff = Math.abs(strikes[0] - spot);
        for (const k of strikes) {
          const d = Math.abs(k - spot);
          if (d < bestDiff) { best = k; bestDiff = d; }
        }
        optionPickerStrike = best;
      } else {
        optionPickerStrike = strikes[Math.floor(strikes.length / 2)] ?? null;
      }
    }).catch(() => { optionPickerStrikes = []; optionPickerStrike = null; });
  });

  async function openOptionPicker(instName, instExchange) {
    try {
      const { listExpiries } = await import('$lib/data/instruments');
      const expiries = listExpiries(instName.toUpperCase(), 'CE');
      if (!expiries.length) return false; // no option chain — direct-add
      optionPickerUnderlying = { name: instName.toUpperCase(), exchange: instExchange };
      optionPickerExpiry     = expiries[0];
      optionPickerSide       = 'CE';
      optionPickerExpiries   = expiries;
      // Strike is set reactively by the $effect above.
      return true;
    } catch { return false; }
  }

  function closeOptionPicker() {
    optionPickerUnderlying = null;
    optionPickerExpiry     = '';
    optionPickerSide       = 'CE';
    optionPickerStrike     = null;
    optionPickerExpiries   = [];
    optionPickerStrikes    = [];
  }

  /**
   * Add-to-watchlist with silent dedup against live positions + holdings.
   * If the symbol is already in either book, we skip the underlying
   * `addWatchlistItem` call entirely — the unified grid would have shown
   * it twice (once as the position/holding row, once as the watchlist
   * row), which is exactly the kind of duplication /pulse is meant to
   * eliminate. Returns true if the row was actually appended.
   */
  async function addToWatchlistDeduped(targetId, sym, exch, alias = null) {
    const needle = String(sym || '').toUpperCase();
    if (!needle) return false;
    const already = (rows) => rows.some(
      r => String(r.symbol || r.tradingsymbol || '').toUpperCase() === needle
    );
    if (already(positions) || already(holdings)) {
      return false;  // silent skip — already present in positions/holdings
    }
    const aliasArg = (alias || '').trim() || null;
    await addWatchlistItem(targetId, sym, exch, aliasArg);
    // Pre-warm the chart cache so when the operator clicks the chart
    // icon on this new row the modal opens against memory. Operator:
    // "you can prefetch all the pulse symbols. whenever, a symbol is
    // added to pulse this should happen". Fire-and-forget — failure
    // falls back to the normal load-on-open path.
    try {
      const { prefetchChartBars } = await import('$lib/ChartWorkspace.svelte');
      prefetchChartBars(sym, exch);
    } catch (_) { /* silent */ }
    return true;
  }

  async function addOptionFromPicker() {
    if (!optionPickerUnderlying || !optionPickerExpiry || optionPickerStrike == null) return;
    // Use the same target-resolution path as addRow + pickFromTypeahead.
    // The previous direct focusedListId fallback silently landed picks
    // in the default list whenever the operator had chosen "+ New
    // watchlist" (or any non-default list) from the Add popup's
    // Watchlist dropdown — and the "NEW" branch was never created at
    // all, so options chosen via the chain picker just vanished.
    const targetId = await _resolveTargetListId();
    if (targetId == null) return;
    try {
      const { findOption } = await import('$lib/data/instruments');
      const inst = findOption(
        optionPickerUnderlying.name,
        optionPickerSide,
        optionPickerStrike,
        optionPickerExpiry,
      );
      if (!inst) { error = 'Symbol not in cache — retry.'; return; }
      await addToWatchlistDeduped(targetId, inst.s, inst.e || 'NFO');
      symInput = ''; typeahead = []; typeaheadOpen = false;
      closeOptionPicker();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  async function addSpotFromPicker() {
    if (!optionPickerUnderlying) return;
    const targetId = await _resolveTargetListId();
    if (targetId == null) return;
    // Resolve the actual NSE/BSE tradingsymbol for the index/stock.
    const KEY = {
      NIFTY: 'NIFTY 50', BANKNIFTY: 'NIFTY BANK', FINNIFTY: 'NIFTY FIN SERVICE',
      MIDCPNIFTY: 'NIFTY MID SELECT', SENSEX: 'SENSEX', BANKEX: 'BANKEX',
    };
    const sym  = KEY[optionPickerUnderlying.name] ?? optionPickerUnderlying.name;
    const exch = (sym === 'SENSEX' || sym === 'BANKEX') ? 'BSE' : 'NSE';
    try {
      await addToWatchlistDeduped(targetId, sym, exch);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      closeOptionPicker();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // Detect whether the underlying in the option picker also has an
  // NSE equity listing (RELIANCE, TCS, INDIGO …). When true, the
  // "Spot" quick-add button gets relabelled "EQ" + bumped to amber
  // so the operator notices the equity affordance instead of
  // assuming the picker is options-only. Indices (NIFTY 50 / SENSEX)
  // keep the "Spot" label because they're traded as derivatives only.
  let _underlyingHasEquity = $state(false);
  $effect(() => {
    if (!optionPickerUnderlying?.name) { _underlyingHasEquity = false; return; }
    const name = optionPickerUnderlying.name.toUpperCase();
    (async () => {
      try {
        const { findEquity } = await import('$lib/data/instruments');
        _underlyingHasEquity = !!findEquity(name);
      } catch { _underlyingHasEquity = false; }
    })();
  });

  // New-list form state.
  let newListName = $state('');

  // Source toggles — driven by the MultiSelect below. The dropdown
  // only surfaces options the caller actually enabled, so consumers
  // like /dashboard (positions + holdings only — no watchlist, no
  // movers, no pinned) don't see disabled-but-listed picks. Each
  // toggle is filtered against its enable* prop; only positions and
  // holdings are unconditionally available since every embedder cares
  // about at least one of them.
  const _ALL_SOURCE_OPTIONS = [
    { value: 'pinned',    label: 'Pinned'    },  // indices + commodities + USDINR
    { value: 'watchlist', label: 'Watchlist' },
    { value: 'positions', label: 'Positions' },
    { value: 'holdings',  label: 'Holdings'  },
    // Movers was a single toggle in the legacy unified-grid era; the
    // 6-grid layout renders winners + losers as two distinct buckets,
    // so the Show filter mirrors that split — operator can show one
    // direction or both. Both toggles roll up to the same upstream
    // `enableMovers` prop because the data source (loadMovers) is one.
    { value: 'winners',   label: 'Winners'   },
    { value: 'losers',    label: 'Losers'    },
  ];
  const _availableSourceValues = $derived(new Set([
    ...(enablePinned     ? ['pinned']    : []),
    ...(enableWatchlists ? ['watchlist'] : []),
    'positions',
    'holdings',
    ...(enableMovers     ? ['winners', 'losers'] : []),
  ]));
  // SOURCE_OPTIONS derived removed — the unified Show MultiSelect now
  // builds its options inline via _showOptions, so the legacy
  // standalone source picker constant has no remaining consumers.
  // selectedSources is now a true $derived of selectedShow (declared
  // below). Earlier we ran a $effect that wrote selectedSources from
  // selectedShow — but the indirect write didn't always propagate
  // through Svelte 5's reactivity to downstream consumers like the
  // unifiedRows derivation. Toggling Positions in the Show dropdown
  // updated the trigger label but left buildUnified's includePos flag
  // stale, so row visibility didn't change. $derived makes this a
  // single inline-computed value, so every consumer that reads
  // selectedSources subscribes directly to selectedShow.

  // ── Unified "Show" multiselect (single source of truth) ──────────────
  // Combines the source toggles AND every watchlist into one flat
  // MultiSelect. Token format:
  //   "src:<source>"  — pinned / positions / holdings / movers
  //   "wl:<id>"       — a specific user-created watchlist
  // The standalone `watchlist` source is implicit: any `wl:` token
  // selected ⇒ watchlist rows shown. Removing this duplication
  // collapses what used to be a tabs row + a separate Sources picker
  // into one control.
  //
  // Seed with EVERY default source on at mount — otherwise the
  // propagation $effect below derives selectedSources = [] which flips
  // every showXxx boolean off and the grid renders nothing while we
  // wait for loadLists() to maybe-fire-a-seed. The prune $effect strips
  // out any source the embedder doesn't actually enable (so disabling
  // movers, pinned, etc. via props still works). Watchlist tokens get
  // appended later by loadLists() once /watchlists comes back.
  let selectedShow = $state(/** @type {string[]} */ ([
    'src:pinned', 'src:positions', 'src:holdings', 'src:winners', 'src:losers',
  ]));

  // Options list rebuilds whenever the available sources or the user's
  // watchlist set changes. Sources come first (Pinned, Positions, …),
  // then watchlists in their `lists` order. Default ★ marker stays as
  // a visual hint on the label so the operator still sees which list
  // is the default add-target.
  const _showOptions = $derived.by(() => {
    const opts = [];
    for (const s of _ALL_SOURCE_OPTIONS) {
      // Drop the standalone 'watchlist' source — selecting any list
      // by name (wl:N) implicitly turns watchlist rows on.
      if (s.value === 'watchlist') continue;
      if (_availableSourceValues.has(s.value)) {
        opts.push({ value: `src:${s.value}`, label: s.label });
      }
    }
    for (const l of lists) {
      opts.push({
        value: `wl:${l.id}`,
        label: l.is_default ? `${l.name} ★` : l.name,
      });
    }
    return opts;
  });

  const selectedSources = $derived.by(() => {
    const arr = selectedShow
      .filter(v => v.startsWith('src:'))
      .map(v => v.slice(4));
    // 'watchlist' source is implicit — any wl: token selected ⇒ show
    // watchlist rows. The rest of the engine reads selectedSources for
    // that flag, so we synthesize it here.
    if (selectedShow.some(v => v.startsWith('wl:')) && !arr.includes('watchlist')) {
      arr.push('watchlist');
    }
    return arr;
  });

  const activeIds = $derived(new Set(
    selectedShow
      .filter(v => v.startsWith('wl:'))
      .map(v => Number(v.slice(3)))
      .filter(n => Number.isFinite(n))
  ));

  // Prune selectedShow when an embedder disables a source group or
  // when a watchlist is deleted out from under us — same defensive
  // pattern as the original selectedSources prune.
  $effect(() => {
    const allowedSrc = _availableSourceValues;
    const allowedWl  = new Set(lists.map(l => `wl:${l.id}`));
    const filtered = selectedShow.filter(v =>
      v.startsWith('src:') ? allowedSrc.has(v.slice(4))
      : v.startsWith('wl:') ? allowedWl.has(v)
      : false
    );
    if (filtered.length !== selectedShow.length) selectedShow = filtered;
  });

  // Refetch watchlist contents whenever activeIds changes from any
  // source — the MultiSelect toggle was a silent gap before this:
  // the OLD pickList helper called loadActive() inline, but the new
  // Show-multiselect path leaves activeIds derived, so we need an
  // explicit effect to mirror that behaviour. Signature-compare
  // avoids redundant re-fetches when the same id set is re-assigned
  // (e.g., makeList add → loadLists rebuild).
  let _lastActiveIdsSig = '';
  $effect(() => {
    const sig = [...activeIds].sort((a, b) => a - b).join(',');
    if (sig === _lastActiveIdsSig) return;
    _lastActiveIdsSig = sig;
    if (sig === '') { activeListsStore.set([]); return; }
    loadActive();
  });

  // Keep individual booleans so buildUnified + other callsites are unchanged.
  let showWatchlist = $state(true);
  let showPinned    = $state(true);
  let showPositions = $state(true);
  let showHoldings  = $state(true);

  // Movers — top-% movers from /watchlist/movers. Polled every 30 s.
  // Failure is non-fatal: the rest of the page keeps working. Internal
  // state still uses a single `movers` array (loadMovers tags each
  // row with `_moverDirection: winners|losers`); the Show filter
  // exposes the two directions as separate toggles so the operator
  // can hide one side without losing the other.
  const movers    = $derived(moversStore.value ?? []);
  let _moversWarnLast = 0;          // rate-limit movers-fetch warn to once per 60 s
  let showMovers = $state(true);  // legacy umbrella — true iff EITHER direction is on
  let showWinners = $state(true);
  let showLosers  = $state(true);

  $effect(() => {
    showPinned    = selectedSources.includes('pinned');
    showWatchlist = selectedSources.includes('watchlist');
    showPositions = selectedSources.includes('positions');
    showHoldings  = selectedSources.includes('holdings');
    showWinners   = selectedSources.includes('winners');
    showLosers    = selectedSources.includes('losers');
    showMovers    = showWinners || showLosers;
  });
  // Account-picker state. Now a MultiSelect — operator can scope
  // positions / holdings INPUTS to buildUnified to any subset of
  // broker accounts. EMPTY array = all accounts (no filter); the
  // sentinel here is `length === 0` rather than the old 'all' string.
  // Watchlist + option-underlying rows are not account-scoped —
  // they always show.
  //
  // Per-card account filters. Positions and Holdings each own a
  // separate picker — operator can scope Positions to ZG#### (intraday
  // book) while Holdings tracks ZJ#### (long-term holds) without one
  // filter clobbering the other. EMPTY array = "all accounts" for
  // that card. Both arrays persist to sessionStorage so the filter
  // survives a tab refresh.
  let positionsAccounts = $state(/** @type {string[]} */ ([]));
  let holdingsAccounts  = $state(/** @type {string[]} */ ([]));
  // Derived membership predicates — `true` when EITHER no filter is
  // applied for that card (length === 0) OR the given account is in
  // the chosen set. Hot-path helpers used by per-account derivations.
  //
  // IMPORTANT: read the array at derivation time (via length+map) so
  // Svelte 5's reactivity subscribes the derivation. An earlier shape
  // that returned a fresh arrow without touching the state inside
  // the derivation body never re-derived (capture trap); the same
  // pattern below for both cards.
  const _includesPosAcct = $derived.by(() => {
    const allow = positionsAccounts.length === 0
      ? null
      : new Set(positionsAccounts.map(String));
    return (/** @type {any} */ acct) =>
      allow === null || allow.has(String(acct || ''));
  });
  const _includesHoldAcct = $derived.by(() => {
    const allow = holdingsAccounts.length === 0
      ? null
      : new Set(holdingsAccounts.map(String));
    return (/** @type {any} */ acct) =>
      allow === null || allow.has(String(acct || ''));
  });
  // Funds strip is rendered above the two cards — it's not owned by
  // either one. Scope = UNION of both pickers (so an account selected
  // in either card surfaces in Funds). Empty + empty = show all.
  const _includesFundsAcct = $derived.by(() => {
    const hasPos = positionsAccounts.length > 0;
    const hasHold = holdingsAccounts.length > 0;
    if (!hasPos && !hasHold) return (/** @type {any} */ _a) => true;
    const allow = new Set([
      ...positionsAccounts.map(String),
      ...holdingsAccounts.map(String),
    ]);
    return (/** @type {any} */ acct) => allow.has(String(acct || ''));
  });
  let availableAccounts = $state(/** @type {string[]} */ ([]));
  // Prune stale account selections that aren't in availableAccounts.
  // Without this, a persisted selection from a PRIOR session can
  // survive a role / mask-mode change — e.g., an admin session
  // persisted [ZG0790, ZJ6294]; later session is demo where accounts
  // surface as [ZG####, ZJ####]. The selection retains unmasked codes
  // while availableAccounts has the masked ones, so the trigger label
  // renders BOTH sets (real codes from the selection, masked from
  // options) → looks like 2× the accounts. Pruning keeps each card's
  // selection a strict subset of availableAccounts.
  $effect(() => {
    if (availableAccounts.length === 0) return;
    const allow = new Set(availableAccounts);
    const prunedP = positionsAccounts.filter((a) => allow.has(a));
    if (prunedP.length !== positionsAccounts.length) positionsAccounts = prunedP;
    const prunedH = holdingsAccounts.filter((a) => allow.has(a));
    if (prunedH.length !== holdingsAccounts.length) holdingsAccounts = prunedH;
  });
  // Broker-registry-loaded accounts — surfaced via /api/admin/brokers
  // Broker account list sourced from the connStatus store which is
  // polled every 15 s by startConnStatusPoller (runs from the layout).
  // Eliminates the separate fetchBrokerAccounts() call on mount.
  // Empty fallback when the endpoint is admin-gated for the current session.
  let _connStatusSnap = $state($connStatus);
  $effect(() => { _connStatusSnap = $connStatus; });

  // book_changed bus — refetch on every terminal postback so cancels
  // and rejections (which don't emit `position_filled`) also trigger
  // a single-iteration update. The store is debounced 200ms upstream
  // so a basket-order burst coalesces into one loadPulse.
  let _pulseBookCounter = 0;
  $effect(() => {
    const n = $bookChanged;
    if (n <= _pulseBookCounter) return;
    _pulseBookCounter = n;
    loadPulse();
  });
  const _knownBrokerAccounts = $derived(_connStatusSnap.accounts ?? []);
  // Latches when the Account seed has firmed up after the broker
  // fetch resolved — prevents re-seeding from clobbering operator
  // toggles on later loadPulse polls.
  let _seededFromBrokers = false;
  // Per-session record of every account we've ever observed in
  // `availableAccounts`. Loaded from sessionStorage on mount so a tab
  // refresh retains the "have we seen this account before?" state.
  // Critical for late-arriving brokers (e.g. Dhan rebuilt via
  // /admin/brokers AFTER the first Kite-only Pulse load): a previously
  // unseen account is auto-unioned into BOTH positionsAccounts and
  // holdingsAccounts the first time it surfaces, even when the latch
  // (`_seededFromBrokers`) has already fired. Without this, Dhan
  // accounts stay invisible until the operator manually opens each
  // multi-select and toggles them on.
  /** @type {Set<string>} */
  let _seenAccounts = new Set();

  // Persist per-card account selections to sessionStorage on change so
  // the filters survive a tab refresh; cleared per session.
  $effect(() => {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem('mp.positionsAccounts', JSON.stringify(positionsAccounts));
    } catch (_) { /* quota / blocked — silent. */ }
  });
  $effect(() => {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem('mp.holdingsAccounts', JSON.stringify(holdingsAccounts));
    } catch (_) { /* quota / blocked — silent. */ }
  });
  // Persist the unified Show filter alongside. Without this, the
  // operator's deselected sources / watchlists reset on every refresh
  // (defaults re-seed everything ON). Same sessionStorage scope as
  // the per-card account pickers so they feel symmetric.
  $effect(() => {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem('mp.selectedShow', JSON.stringify(selectedShow));
    } catch (_) {}
  });
  // Per-source summary rows from the backend (positions / holdings
  // endpoints return precomputed per-account totals in .summary).
  // Combined into one row-set for the summary grid above the main one.
  let positionsSummary = $state(/** @type {any[]} */ ([]));
  let holdingsSummary  = $state(/** @type {any[]} */ ([]));
  // Funds (per-account margins) — loaded only when showFunds is true.
  const funds     = $derived(fundsStore.value ?? []);

  const sparklines = $derived(sparklinesStore.value ?? /** @type {Record<string, number[]>} */ ({}));
  // _firstSparkDone is now tracked inside sparklinesStore's fetcher
  // (the _firstSparkFetched module-level flag in marketDataStores).
  // BH3: snapshot of current LTPs sourced from symbolStore, refreshed
  // at 50ms (~20Hz) when symbolTickCount fires. Cell renderers + the
  // sparkline tail read _liveLtpSnap[sym] for O(1) lookups instead of
  // calling getSnapshot inside each paint. The throttle is preserved
  // because SSE ticks can land 100/sec under load — too fast for
  // ag-Grid refreshCells without coalescing. Replaces the previous
  // liveLtp.subscribe-driven mirror; the `liveLtp` writable store is
  // deleted with this slice.
  let _liveLtpSnap = $state(/** @type {Record<string, number>} */ ({}));
  let _liveLtpFlushTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
  // Design B: freshness shimmer instance — neutral "data just landed" signal
  // on the LTP cell. Pilot on /pulse only (this component). Fires on every
  // SSE tick update, not just on delta, so it fires even on repeat values.
  const _ltpShimmer = createFreshnessShimmer({ durationMs: 700 });
  /** Build a `{sym: ltp}` map snapshot from symbolStore for fast cell reads. */
  function _buildLtpSnap() {
    /** @type {Record<string, number>} */
    const out = {};
    for (const [sym, snap] of symbolStore.entries()) {
      const v = snap?.ltp;
      if (v != null && Number.isFinite(v)) out[sym] = v;
    }
    return out;
  }
  $effect(() => {
    // Seed once on mount so a hydrated symbolStore (loaded from
    // localStorage at module init) paints into _liveLtpSnap before
    // any SSE tick fires — without this, cell renderers read empty
    // map on the first frame.
    _liveLtpSnap = _buildLtpSnap();
    const unsub = symbolTickCount.subscribe(() => {
      if (_liveLtpFlushTimer) return;
      _liveLtpFlushTimer = setTimeout(() => {
        _liveLtpSnap = _buildLtpSnap();
        _liveLtpFlushTimer = null;
      }, 50);
    });
    return () => {
      unsub();
      if (_liveLtpFlushTimer) { clearTimeout(_liveLtpFlushTimer); _liveLtpFlushTimer = null; }
    };
  });
  let stopWS;

  // ── Manual group order + symbol detachment (per-browser overrides) ──
  // Operator clicks ↑ / ↓ on a row → that row's underlying group moves
  // up or down within its bucket. Clicks the ⋯ menu → "Detach from
  // group" pulls the symbol out of its underlying group entirely so
  // it sorts by itself at the end of the bucket.
  //
  // Persistence: localStorage, per-browser. Backend sync would need a
  // schema migration we don't want yet. Reset clears both overrides.
  const LS_GROUP_ORDER = 'pulse:groupOrder';   // { NIFTY: 0, BANKNIFTY: 1, ... }
  const LS_DETACHED    = 'pulse:detached';     // ["NIFTY25APR21000PE", ...]
  let groupOrder       = $state(/** @type {Record<string, number>} */ ({}));
  let detachedSymbols  = $state(/** @type {string[]} */ ([]));

  function loadOverrides() {
    try {
      const g = JSON.parse(localStorage.getItem(LS_GROUP_ORDER) || '{}');
      if (g && typeof g === 'object') groupOrder = g;
    } catch (_) { /* corrupt JSON — start fresh */ }
    try {
      const d = JSON.parse(localStorage.getItem(LS_DETACHED) || '[]');
      if (Array.isArray(d)) detachedSymbols = d;
    } catch (_) { /* corrupt JSON — start fresh */ }
  }

  function saveGroupOrder() {
    try { localStorage.setItem(LS_GROUP_ORDER, JSON.stringify(groupOrder)); } catch (_) {}
  }
  function saveDetached() {
    try { localStorage.setItem(LS_DETACHED, JSON.stringify(detachedSymbols)); } catch (_) {}
  }

  /** Move the row's underlying group up (-1) or down (+1) within its bucket. */
  function moveGroup(row, dir) {
    const g = String(row?.underlying || row?.tradingsymbol || '').toUpperCase();
    if (!g) return;
    // Build the visible group list at the row's bucket, sorted current.
    const myBucket = unifiedRows
      .filter(r => bucketOf(r) === bucketOf(row))
      .map(r => String(r.underlying || `~~${r.tradingsymbol || ''}`).toUpperCase());
    // Preserve order of first appearance — that's the current sort.
    const seen = new Set();
    const ordered = [];
    for (const k of myBucket) {
      if (!seen.has(k)) { seen.add(k); ordered.push(k); }
    }
    const idx = ordered.indexOf(g);
    if (idx < 0) return;
    const newIdx = idx + dir;
    if (newIdx < 0 || newIdx >= ordered.length) return;
    // Swap into the new position and persist ALL groups in the bucket
    // with the new ranks so the comparator can read them back.
    [ordered[idx], ordered[newIdx]] = [ordered[newIdx], ordered[idx]];
    const next = { ...groupOrder };
    ordered.forEach((k, i) => { next[k] = i; });
    groupOrder = next;
    saveGroupOrder();
  }

  /** True iff the symbol is currently in the detached set. */
  function isDetached(sym) {
    return detachedSymbols.includes(String(sym || '').toUpperCase());
  }

  function detachSymbol(row) {
    const sym = String(row?.tradingsymbol || '').toUpperCase();
    if (!sym || isDetached(sym)) return;
    detachedSymbols = [...detachedSymbols, sym];
    saveDetached();
  }

  function reattachSymbol(row) {
    const sym = String(row?.tradingsymbol || '').toUpperCase();
    if (!sym) return;
    detachedSymbols = detachedSymbols.filter(s => s !== sym);
    saveDetached();
  }

  function resetOverrides() {
    groupOrder = {};
    detachedSymbols = [];
    saveGroupOrder();
    saveDetached();
  }

  const hasOverrides = $derived(
    Object.keys(groupOrder).length > 0 || detachedSymbols.length > 0
  );

  // Bucket-of helper — mirrors the bucket logic inside buildUnified
  // so moveGroup can constrain its swap to the same bucket.
  // Order: watchlist → positions → holdings → everything else.
  function bucketOf(row) {
    // Prefer the row's declared major group when buildUnified set it
    // (every row in mainRows carries `_majorGroup`). This keeps the
    // postSortRows regrouping consistent with the major-divider
    // assignment — without it, anchor rows (src.u-only) fell into
    // bucket 4 here, while their `_majorGroup` was 'positions', so the
    // CRUDEOIL / GOLDM / INDIGO anchors landed AFTER the entire
    // holdings block instead of right above their option contracts.
    //
    // 5 distinct buckets so postSortRows sorts INSIDE each one
    // independently (sort by P&L within Positions does not pull a
    // pinned NIFTY row up into the Positions block). Earlier this
    // merged Pinned + Watchlist into bucket 1 — sorting by P&L
    // mixed those two majors.
    const mg = row?._majorGroup;
    if (mg === 'pinned')     return 1;
    if (mg === 'watchlist')  return 2;
    if (mg === 'positions')  return 3;
    if (mg === 'holdings')   return 4;
    if (mg === 'movers')     return 5;
    // Legacy fallback for callers that don't run through mainRows
    // (the watchlist add/remove helpers up at line ~384 hit this).
    if (row?.src?.w) return 2;
    if (row?.src?.p) return 3;
    if (row?.src?.h) return 4;
    return 5;
  }
  // Single unified ticker — replaces stopPoll / stopPulsePoll /
  // stopMoversPoll / stopSparkPoll. Tick cadence comes from the
  // pulse.tick_interval_ms setting (default 5000 ms). Heavier ops
  // piggy-back every Nth tick.
  let stopPulseTick;
  let stopTickSettingPoll;
  let _tickMs = 5000;
  let _tickCount = 0;
  // Multipliers (relative to the base tick):
  //   quotes    — every tick when SSE is down; every 6 ticks when SSE is live
  //               (30s — OHLC / volume / watchlist fields still need polling,
  //               but LTP is served by the stream so the 5s poll is redundant)
  //   pulse+funds — every 2 ticks (positions + holdings + funds)
  //   movers    — every 6 ticks
  //   sparklines — every 12 ticks (daily-closes don't change intraday;
  //                rare refresh is fine, cosmetic only)
  const _TICK_PULSE = 2;
  const _TICK_MOVERS = 6;
  const _TICK_SPARK = 12;
  // When the SSE stream is healthy, loadQuotes only runs every N ticks
  // (5s × 6 = 30s). When the stream is down it runs every tick (5s)
  // so LTP freshness is maintained through the polling fallback.
  const _TICK_QUOTES_SSE = 6;
  // Bridge $store.streamOpen into a local $state variable so $derived
  // and regular JS reads work without the store subscription wrapping.
  let _liveStreamUp = $state(false);
  $effect(() => {
    const unsub = streamOpen.subscribe(v => { _liveStreamUp = v; });
    return unsub;
  });
  let _refreshing = $state(false);
  async function _runTick() {
    _tickCount++;
    // When the SSE stream is live, LTP ticks arrive in real time so we
    // only poll the /watchlist/{id}/quotes endpoints at a reduced cadence
    // (every 6 ticks ≈ 30 s) to refresh OHLC, volume, and other non-LTP
    // fields. When the stream is down every tick triggers the full poll
    // so LTP freshness degrades gracefully to the 5 s polling baseline.
    const quotesDue = !_liveStreamUp || (_tickCount % _TICK_QUOTES_SSE === 0);
    if (quotesDue) await loadQuotes();
    if (_tickCount % _TICK_PULSE === 0) {
      await loadPulse();
      if (showFunds) await loadFunds();
    }
    let moversChanged = false;
    if (enableMovers && _tickCount % _TICK_MOVERS === 0) {
      await loadMovers();
      moversChanged = true;
    }
    // Sparklines fire on the regular 12-tick cadence (60 s) OR
    // immediately after a movers refresh so new winners/losers
    // get their 5d column populated within the same tick instead
    // of waiting up to another 30 s. The backend's _spark_past_cache
    // + _spark_today_cache absorb the extra calls — symbols already
    // cached return instantly from the past+today layers, only
    // genuinely-new mover symbols pay a historical_data fetch (paced
    // at 3 req/sec by the endpoint).
    if (moversChanged || _tickCount % _TICK_SPARK === 0) loadSparklines();
  }
  /** Operator-initiated "refresh everything now" — bound to the
   *  RefreshButton in the toolbar. Drains the spinner state after the
   *  heaviest op (loadPulse) settles so the button reads as busy until
   *  the grid has new rows, not just until the quote call returns.
   *
   *  Sleep audit Jun 2026: switched Promise.all → Promise.allSettled
   *  so a single hung sub-fetch (network blip, broker timeout, etc.)
   *  can't strand the spinner. Operator complaint: "even refresh gets
   *  stuck in the middle" — the spinner was tied to _refreshing, which
   *  stayed true forever if any one of the 5 awaits never resolved.
   *  allSettled guarantees `finally` runs once every fetcher has
   *  either fulfilled OR rejected, so the spinner always drains. */
  async function refreshAllNow() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      await Promise.allSettled([
        loadQuotes(),
        loadPulse(),
        showFunds ? loadFunds() : Promise.resolve(),
        enableMovers ? loadMovers() : Promise.resolve(),
        loadSparklines(),
      ]);
    } finally {
      _refreshing = false;
    }
  }
  // Exposed so a parent page can wire the page-header RefreshButton
  // (next to the wall-clock timestamp) into the same refresh flow the
  // per-card RefreshButtons trigger. `bind:this={pulseRef}` from the
  // page lets the page call `pulseRef.refresh()`.
  export async function refresh() { await refreshAllNow(); }
  // Wall-clock timestamp (ms) of the last loadPulse() completion.
  // The 5 s loadQuotes poll consults this to skip ticks that land
  // within a 700 ms window of a loadPulse — the two pollers used to
  // collide on every 10 s boundary, hammering the backend with
  // /positions + /holdings + /quote/batch + N × /watchlist/{id}/quotes
  // simultaneously. Within-window skip preserves up-to-5 s freshness
  // on the watchlist while removing the thundering herd.
  let _lastPulseAt = 0;
  // 6-grid layout — one ag-Grid instance per bucket. Desktop renders
  // them in a 2 × 3 CSS-grid (Pinned + Watchlist | Positions +
  // Holdings | Winners + Losers); mobile stacks vertically. Each
  // grid scrolls independently, owns its own column set, and (for
  // Positions / Holdings) pins a TOTAL row at the bottom edge.
  let gridPinnedEl     = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridWatchEl      = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridPositionsEl  = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridHoldingsEl   = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridWinEl        = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridLoseEl       = $state(/** @type {HTMLDivElement | null} */ (null));
  let positionsSummaryEl = $state(/** @type {HTMLDivElement | null} */ (null));
  let holdingsSummaryEl  = $state(/** @type {HTMLDivElement | null} */ (null));
  let fundsEl            = $state(/** @type {HTMLDivElement | null} */ (null));
  let gridPinned, gridWatch, gridPositions, gridHoldings, gridWin, gridLose;
  let positionsSummaryGrid;
  let holdingsSummaryGrid;
  let fundsGrid;
  // Sentinel flags flip true once each createGrid runs so the $effect
  // that pushes filtered row data into the grid is gated on both
  // (a) the grid being mounted and (b) the derived row set having
  // populated.
  let gridPinnedReady     = $state(false);
  let gridWatchReady      = $state(false);
  let gridPositionsReady  = $state(false);
  let gridHoldingsReady   = $state(false);
  let gridWinReady        = $state(false);
  let gridLoseReady       = $state(false);

  // Instruments cache populated / rebuilt → expiry-day lookup is now
  // available. Drop the stale symbol-format cache (every entry was
  // computed pre-cache so each one is missing the day suffix) and
  // force every grid to re-paint its tradingsymbol column so the
  // operator sees the day appear without having to wait for the next
  // poll. Cheap: refreshCells is column-scoped and ag-Grid batches.
  $effect(() => {
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    _pulseSymFmtCache.clear();
    const cols = ['tradingsymbol'];
    for (const [g, ready] of /** @type {Array<[any, boolean]>} */ ([
      [gridPinned,    gridPinnedReady],
      [gridWatch,     gridWatchReady],
      [gridPositions, gridPositionsReady],
      [gridHoldings,  gridHoldingsReady],
      [gridWin,       gridWinReady],
      [gridLose,      gridLoseReady],
    ])) {
      if (ready && g) try { g.refreshCells({ columns: cols, force: true }); } catch (_) { /* grid not ready */ }
    }
  });

  // Per-bucket collapse state — driven by <CollapseButton> in each
  // header. Each bucket persists its toggle independently via
  // CollapseButton's own localStorage layer (keyed cardId per
  // operator), so the operator can collapse Pinned permanently
  // while keeping Positions expanded. `_effCol*` (declared below
  // once the row derivations exist) folds in the auto-collapse
  // rule: a card with zero rows renders as collapsed regardless
  // of the operator toggle.
  // Pinned + Watchlist merged into ONE tabbed card; _colPinWatch is
  // its single collapse state, topTab toggles which feed is visible.
  let _colPinWatch  = $state(false);
  // Top-tab is either the special 'pinned' string (the merged
  // Default + Markets feed) OR a user-watchlist id. Operator request:
  // "there should not be any watchlist tab. it should be the tab I
  // have created" — each user list becomes its own peer of Pinned.
  let topTab        = $state(/** @type {'pinned'|number} */ ('pinned'));
  // Snap back to Pinned when the operator deletes the user list out
  // from under the active tab.
  $effect(() => {
    if (typeof topTab === 'number' && !_userLists?.some(l => l.id === topTab)) {
      topTab = 'pinned';
    }
  });
  let _colWinners   = $state(false);
  let _colLosers    = $state(false);
  let _colPositions = $state(false);
  let _colHoldings  = $state(false);

  // Per-card symbol filters — bound by <GridSearchButton> in each
  // bucket header. Empty string = no filter. Each filter pipes into
  // its grid via setGridOption('quickFilterText') below.
  let _filterPinWatch  = $state('');
  let _filterWinners   = $state('');
  let _filterLosers    = $state('');
  let _filterPositions = $state('');
  let _filterHoldings  = $state('');
  $effect(() => {
    const v = _filterPinWatch;
    try { gridPinned?.setGridOption('quickFilterText', v); } catch (_) {}
    try { gridWatch?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  $effect(() => {
    const v = _filterWinners;
    try { gridWin?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  $effect(() => {
    const v = _filterLosers;
    try { gridLose?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  $effect(() => {
    const v = _filterPositions;
    try { gridPositions?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  $effect(() => {
    const v = _filterHoldings;
    try { gridHoldings?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  // Per-card fullscreen toggles — pair with the existing collapse
  // toggles so every bucket gets the canonical card-control trio
  // (Fullscreen / Default / Collapse). Each card owns its own
  // `_fsXxx` state so the operator can promote any single card to
  // viewport while the others stay inline.
  let _fsPinWatch   = $state(false);
  let _fsWinners    = $state(false);
  let _fsLosers     = $state(false);
  let _fsPositions  = $state(false);
  let _fsHoldings   = $state(false);

  let positionsSummaryReady  = $state(false);
  let holdingsSummaryReady   = $state(false);
  let fundsReady             = $state(false);

  // Instrument-cache lookup functions. Loaded once at mount + cached
  // as module-scope state so buildUnified() can parse tradingsymbols
  // synchronously and loadPulse can resolve MCX commodities to their
  // near-month future without re-importing the module on every tick.
  let getInstrument     = $state(/** @type {((s: string) => any) | null} */ (null));
  let findNearestFuture = $state(/** @type {((u: string) => any) | null} */ (null));
  let listFutures       = $state(/** @type {((u: string) => any[]) | null} */ (null));
  // F&O underlying lot lookup — returns the contract lot size when the
  // tradingsymbol has CE/PE listed (covered-call viable), 0 otherwise.
  // Loaded alongside getInstrument so the Holdings row-class can fire
  // synchronously inside getRowClass. Memoized per-symbol because the
  // underlying lookup is stable within a session (lot sizes don't
  // change tick-to-tick) and getRowClass + symRenderer call it on
  // every visible row every refresh.
  let _fnoLotForState   = $state(/** @type {((s: string) => number) | null} */ (null));
  /** @type {Map<string, number>} */
  const _fnoLotCache = new Map();
  $effect(() => {
    // Clear the per-session memo when the upstream lookup is replaced
    // (e.g. instruments cache reload).
    void _fnoLotForState;
    _fnoLotCache.clear();
  });
  function _fnoLotFor(/** @type {string} */ s) {
    if (_fnoLotCache.has(s)) return /** @type {number} */ (_fnoLotCache.get(s));
    const v = _fnoLotForState ? _fnoLotForState(s) : 0;
    _fnoLotCache.set(s, v);
    return v;
  }

  /** Lots-in-F&O-units for a unified Pulse row.
   *  - Holdings on an F&O underlying (EQ row with options listed)
   *      → qty_hold / underlying_lot
   *  - Position on a derivative contract (CE / PE / FUT)
   *      → qty_pos / contract_lot
   *  - Everything else → 0
   *  Combined holdings + position rows on the same symbol sum.
   *  Returns null for TOTAL rows (the aggregate is meaningless). */
  function _lotsForUnifiedRow(/** @type {any} */ row) {
    if (!row || row._isTotal) return null;
    const sym = String(row.tradingsymbol || '').toUpperCase();
    if (!sym) return 0;
    let total = 0;
    // Derivative-contract position: use the contract's own ls.
    const inst = getInstrument ? getInstrument(sym) : null;
    const itype = inst?.t;
    if (itype === 'CE' || itype === 'PE' || itype === 'FUT') {
      const lot = Number(inst?.ls) || 0;
      if (lot > 0) {
        const qPos = Math.abs(Number(row.qty_pos) || 0);
        if (qPos > 0) total += qPos / lot;
      }
    } else {
      // Equity / index: use the underlying-options lot (if any).
      const lot = _fnoLotFor(sym);
      if (lot > 0) {
        const qHold = Math.abs(Number(row.qty_hold) || 0);
        if (qHold > 0) total += qHold / lot;
      }
    }
    return Math.round(total * 10) / 10;
  }

  /** Number formatter for the Lots column. Hides 0 as a bare '0'
   *  string (operator scan-cost matters more than completeness);
   *  whole-number lots render without decimal, fractional with one. */
  function _lotsFmt(/** @type {number|null|undefined} */ value) {
    if (value == null) return '';
    if (value === 0) return '0';
    return value % 1 === 0 ? String(value) : value.toFixed(1);
  }

  // Transient-error suppression. Quote-refresh polls fire every 5 s
  // and can blip on broker hiccups; one failed call shouldn't paint
  // the page red. Show the banner only after 3 consecutive failures.
  let quoteFailStreak = 0;

  // Order-ticket integration — row click opens the SymbolPanel
  // pre-filled with the row's symbol / exchange / lot size. Stays
  // null when no ticket is open. Real broker accounts (unmasked
  // account_id) fetched once on mount so the operator can pick.
  let ticketProps     = $state(/** @type {any} */ (null));
  let realAccounts    = $state(/** @type {string[]} */ ([]));

  // loadSparklines — builds the pairs list from unifiedRows and delegates
  // to sparklinesStore (slice AB). Chunking, prune, and TTL.day write-back
  // all live inside the store's fetcher. The first-call prune guard is
  // tracked via _firstSparkFetched in marketDataStores.svelte.js.
  async function loadSparklines() {
    const pairs = [];
    const seen = new Set();
    for (const row of unifiedRows) {
      if (!row.tradingsymbol) continue;
      const sym  = String(row.tradingsymbol).toUpperCase();
      const exch = String(row.exchange || 'NSE').toUpperCase();
      const key  = `${exch}:${sym}`;
      if (!seen.has(key)) {
        seen.add(key);
        pairs.push({ tradingsymbol: sym, exchange: exch });
      }
    }
    if (!pairs.length) return;
    try {
      await sparklinesStore.load(pairs);
    } catch (_) { /* non-fatal — sparklines are cosmetic */ }
  }

  onMount(async () => {
    // Auth is enforced by the (algo) layout — no goto('/signin')
    // here so this component can also be embedded in flows that
    // intentionally allow anonymous demo viewers.
    loadOverrides();
    // Account selection defaults to ALL ACCOUNTS on every page load.
    // Operator: "all accounts should be default for positions and
    // holdings in pulse." Previously the picker restored its
    // sessionStorage cache here, so a prior narrowing (e.g. ZJ6294
    // only) stuck across tab reopens. Now within-tab toggles still
    // persist via the auto-save $effect below — but a fresh tab
    // starts wide. The "seen accounts" ledger also resets so
    // late-arriving brokers (Dhan, Groww) are treated as new and
    // surface in the wide-default view immediately. */
    if (accountPicker) {
      positionsAccounts = [];
      holdingsAccounts  = [];
      _seenAccounts     = new Set();
    }
    // Restore the Show filter (sources + watchlists). The eager seed
    // at $state declaration acts as fallback when no persisted value
    // exists. wl: tokens whose ids no longer exist get pruned by the
    // selectedShow $effect once `lists` loads.
    try {
      const cachedShow = sessionStorage.getItem('mp.selectedShow');
      if (cachedShow) {
        const parsed = JSON.parse(cachedShow);
        if (Array.isArray(parsed) && parsed.length > 0) selectedShow = parsed;
      }
    } catch (_) { /* fall through to default seed */ }
    // Cache restoration is now handled automatically by the three-tier
    // stores (slice AB). Each store pre-populates from localStorage
    // on module init (see dataStore.svelte.js _initFromCache), so
    // positions / holdings / funds / movers / activeLists / watchQuotes /
    // sparklines are already non-empty before onMount fires.
    await tick();
    mountGrid();

    // Parallel cold-mount fan-out. Previously instruments → accounts →
    // lists → loadActive → loadPulse → loadFunds → loadMovers ran
    // strictly serially, blocking first paint for 3–5 s on a cold
    // cache. None of these depend on each other except (loadLists +
    // loadActive — loadActive needs activeIds populated from lists),
    // and the heavy ones (instruments / pulse / movers) can all fire
    // concurrently. loadSparklines is cosmetic — fire and forget.
    const instrumentsP = (async () => {
      try {
        const mod = await import('$lib/data/instruments');
        await mod.loadInstruments();
        getInstrument     = mod.getInstrument;
        findNearestFuture = mod.findNearestFuture;
        listFutures       = mod.listFutures;
        _fnoLotForState   = mod.getOptionUnderlyingLot;
      } catch (_) { /* cache cold — group/sort falls back to alphabetical */ }
    })();
    const accountsP = (async () => {
      try {
        const r = await fetchAccounts();
        realAccounts = (r?.accounts || [])
          .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
          .filter(Boolean);
      } catch (_) { realAccounts = []; }
    })();
    const listsP = enableWatchlists
      ? loadLists().then(() => (activeIds.size > 0 ? loadActive() : null))
      : Promise.resolve();
    const pulseP  = loadPulse();
    const fundsP  = showFunds ? loadFunds() : Promise.resolve();
    const moversP = enableMovers ? loadMovers() : Promise.resolve();

    // Block onMount only on the data the first paint actually needs.
    // Sparklines run fire-and-forget (cosmetic; missing them shows the
    // grid without the inline trend column for ≤1 s).
    // Broker accounts are sourced from connStatus (polled every 15 s by
    // the layout's startConnStatusPoller) — no separate fetch needed here.
    //
    // Sleep audit Jun 2026: Promise.all → Promise.allSettled so a
    // failed cold-load (e.g. broker hiccup on first connect) doesn't
    // throw out of onMount and prevent the interval below from being
    // scheduled. The page would otherwise sit frozen with cached data
    // until a manual refresh.
    await Promise.allSettled([instrumentsP, accountsP, listsP, pulseP, fundsP, moversP]);
    loadSparklines();
    // The first loadSparklines call races with `unifiedRows` deriving —
    // `loadQuotes` finishes loading the watchQuotes map AFTER the
    // Promise.all above, so the very first call can fire with an empty
    // or partial pairs list and silently return (no sparklines on any
    // grid until the 60 s _TICK_SPARK tick). Re-fire 2 s later to cover
    // the race; the second call dedups by exchange:sym so it's cheap
    // even when the first call already populated everything.
    setTimeout(() => { loadSparklines(); }, 2000);

    // Unified pulse tick — one marketAwareInterval drives every refresh.
    // pulse.tick_interval_ms (default 5000) is the base cadence; heavier
    // operations piggy-back every Nth tick so the overall load stays
    // bounded. Earlier this page ran 4 independent intervals
    // (quotes 5s / pulse 10s / movers 30s / sparklines 60s) which made
    // the cadence non-obvious and impossible to tune from one knob.
    //
    // Market-aware gating: outside the combined NSE/MCX session (overnight,
    // weekend, holiday) every loadQuotes/Pulse/Movers call returns the same
    // values the page already has, so polling at 5s burns broker quota for
    // no UI change. marketAwareInterval keeps the underlying timer alive
    // (so the session-open boundary re-engages naturally) but no-ops the
    // tick body when no segment is open.
    //
    // Visibility throttle (Option B hybrid): positions / holdings / funds
    // are critical data — we keep them alive at 30 s on hidden rather
    // than pausing entirely, so the operator returns to current numbers
    // without waiting for a full 5 s warm-up cycle. The WS `position_filled`
    // channel fires loadPulse() immediately on a fill regardless of this
    // cadence (that path is not gated by the interval).
    const _HIDDEN_TICK_MS = 30_000;
    async function _readTickSetting() {
      try {
        const rows = await fetchSettings();
        const all = Array.isArray(rows) ? rows : (rows?.settings || []);
        const row = all.find?.(s => s?.key === 'pulse.tick_interval_ms');
        const v = Number(row?.value ?? row?.default_value);
        if (Number.isFinite(v) && v >= 500 && v <= 60000) return v;
      } catch (_) { /* anon/demo — keep current */ }
      return _tickMs;
    }
    _tickMs = await _readTickSetting();
    stopPulseTick = marketAwareInterval(_runTick, _tickMs, _HIDDEN_TICK_MS);

    // Re-read the tick setting every 60s. When the operator changes
    // pulse.tick_interval_ms in /admin/settings, the new value lands on
    // the next 60s read without a page reload — previously the cadence
    // froze at whatever was set on mount.
    // Non-critical poller — pause entirely on hidden (no need to reload
    // a UI setting while the operator is not watching).
    stopTickSettingPoll = visibleInterval(async () => {
      const next = await _readTickSetting();
      if (next !== _tickMs) {
        _tickMs = next;
        stopPulseTick?.();
        stopPulseTick = marketAwareInterval(_runTick, _tickMs, _HIDDEN_TICK_MS);
      }
    }, 60_000);

    // Real-time order-fill push — Kite postback fires a WS event
    // `position_filled` the moment an order fills. Subscribe so
    // Pulse refreshes positions + holdings IMMEDIATELY
    // instead of waiting up to 10 s for the next loadPulse tick.
    // Other (non-fill) events on the same socket also trigger a
    // refresh — cheap to over-fetch, expensive to lag a fill.
    // (The wider `book_changed` bus also fires loadPulse — kept
    // here for the qty-delta optimistic patch path which only
    // emits on FILL, not on CANCELLED/REJECTED.)
    stopWS = createPerformanceSocket((msg) => {
      if (msg?.event === 'position_filled') {
        // Order just filled — refresh both books right now so the
        // grid shows the new qty within a tick. The 10 s pulse
        // poll keeps running as a backstop.
        loadPulse();
      }
    });

    // Keyboard shortcuts — scoped to this wrapper only.
    document.addEventListener('keydown', handleKeydown);
    document.addEventListener('click', onDocClick);

    // SSE quote stream — live LTP pushed from the server's KiteTicker
    // WebSocket. startQuoteStream() is idempotent so multiple Pulse
    // instances on the same page share one connection.
    startQuoteStream();
  });

  async function loadFunds() {
    await fundsStore.load();
  }

  // Movers fetch now delegated to moversStore (slice AB). The full
  // batchQuote + projection logic lives in marketDataStores.svelte.js
  // so any other consumer can import the same singleton.
  async function loadMovers() {
    try {
      await moversStore.load();
    } catch (e) {
      const _now = Date.now();
      if (_now - _moversWarnLast > 60_000) {
        _moversWarnLast = _now;
        console.warn('[MarketPulse] movers fetch failed:', e?.message || e);
      }
    }
  }

  // Pinned-top group — any watchlist row whose underlying is in
  // this set bypasses column sort via ag-Grid's pinnedTopRowData.
  // Display order within the pinned block is operator-meaningful:
  // broad indices first, narrowing down, then BSE, volatility,
  // currency, and finally commodities (precious → energy → base).
  // Operator can detach individual rows via the ⋯ context menu.
  const PIN_ORDER = {
    // ── Indices ── operator-preferred sequence (Nifty 50 first as
    // broad benchmark, Sensex as the BSE counterpart, then Bank Nifty
    // for financials, Nifty IT for tech, broad mid + small caps,
    // VIX last as the volatility gauge). Less-frequented indices
    // (FinNifty, Nifty Next 50, Bankex) come after the canonical 7.
    NIFTY:        1,   // Nifty 50
    SENSEX:       2,   // BSE benchmark
    BANKNIFTY:    3,   // Bank Nifty
    NIFTYIT:      4,   // Nifty IT
    MIDCPNIFTY:   5,   // Mid Cap
    SMALLCAP:     6,   // Small Cap
    INDIAVIX:     7,   // Volatility
    // Less-frequented after the operator's preferred 7
    FINNIFTY:     8,
    NIFTYNXT50:   9,
    BANKEX:      10,
    // Slot 11-12 reserved for future indices
    // ── Currencies (NSE CDS) ──
    USDINR:      13,
    // ── Commodities (MCX): precious → energy → base ──
    GOLD:        14,
    GOLDM:       15,
    SILVER:      16,
    SILVERM:     17,
    SILVERMIC:   18,
    CRUDEOIL:    19,
    CRUDEOILM:   20,
    NATURALGAS:  21,
    COPPER:      22,
  };
  const PINNED_INDEX_UNDERLYINGS = new Set(Object.keys(PIN_ORDER));
  function isPinnedIndexRow(r) {
    // Pinned membership is now DB-driven via the watchlist's is_pinned
    // flag (propagated to rows as _fromPinnedList by buildUnified). The
    // legacy PIN_ORDER hardcode survives only as a sort-rank hint for
    // visual sub-grouping inside the pinned strip (Indices → Forex →
    // Commodities). Operator-detached symbols still drop out via the
    // detach override regardless of source.
    if (isDetached(r.tradingsymbol)) return false;
    if (r?._fromPinnedList) return true;
    return false;
  }
  function pinRank(r) {
    return PIN_ORDER[String(r.underlying || '').toUpperCase()] ?? 999;
  }
  // Pinned-area sub-category for visual grouping (indices / forex /
  // commodity). Ranks 1-9 = indices (incl. VIX); rank 10 = forex
  // (USDINR); 11-19 = commodity. Each sub-group gets a thin amber
  // top-border on its first row to visually separate from the prior
  // group. Operator sees three distinct mini-sections at the top:
  //   INDICES   — NIFTY, BANKNIFTY, …, INDIA VIX
  //   FOREX     — USDINR (and any future currency pairs)
  //   COMMODITY — GOLD, SILVER, CRUDE, …
  function pinCategory(rank) {
    if (rank <= 12) return 'idx';
    if (rank === 13) return 'fx';
    if (rank <= 29) return 'commodity';
    return 'other';
  }
  // ag-Grid renders pinnedTopRowData in array order (no column-sort
  // applied), so this sorted slice IS the effective display order.
  // Each row is tagged with _pinCategory + _pinFirst so getRowClass
  // can paint a divider line at sub-group boundaries.
  const pinnedTopRows = $derived.by(() => {
    if (!showPinned) return [];
    const sorted = unifiedRows.filter(r => r._majorGroup === 'pinned' && !isDetached(r.tradingsymbol))
      .slice().sort((a, b) => {
        const ra = pinRank(a), rb = pinRank(b);
        if (ra !== rb) return ra - rb;
        return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
      });
    let lastCat = null;
    return sorted.map(r => {
      const cat = pinCategory(pinRank(r));
      const tagged = { ...r, _pinCategory: cat, _pinFirst: cat !== lastCat };
      lastCat = cat;
      return tagged;
    });
  });
  // mainRows hosts the Watchlist + Positions + Holdings + Movers
  // majors below the Pinned strip. Each row carries its major as
  // `_majorGroup`; the sort comparator lays them out in MAJOR_ORDER
  // sequence (watchlist 1 → positions 2 → holdings 3 → movers 4).
  // First row of each major gets `_majorFirst=true` so the row
  // styling can paint a divider above it.
  const mainRows = $derived.by(() => {
    const rows = unifiedRows.filter(r => {
      // Pinned major lives in pinnedTopRows; never appears in main.
      if (r._majorGroup === 'pinned') return false;
      // Watchlist major gated by the operator's source toggle.
      if (r._majorGroup === 'watchlist' && !showWatchlist) return false;
      // Positions / Holdings / Movers are pre-filtered at buildUnified
      // entry via includePos / includeHold / includeMovers, so no
      // additional gate needed here. (Symbols already absent.)
      return true;
    });
    // Sort: major first → underlying-group within major → anchor before
    // options within the same underlying → alphabetical tradingsymbol as
    // the final tiebreaker.
    //
    // Why group by `underlying` and not by tradingsymbol alphabetic?
    // Anchors (src.u-only rows) carry a tradingsymbol that may differ
    // from their underlying name — MCX same-month-future stubs land as
    // `CRUDEOIL26JUNFUT`, alphabetically AFTER `CRUDEOIL26JUN10000CE`,
    // which orphans the anchor at the BOTTOM of the positions block.
    // Sorting by `underlying` first pins every row carrying
    // underlying='CRUDEOIL' to one contiguous block; within that block,
    // anchor rows (tier 0/1) precede option rows (tier 2).
    const _ugKey = (r) => String(r.underlying || r.tradingsymbol || '').toUpperCase();
    const _tier  = (r) => (r.kind === 'spot' ? 0 : r.kind === 'fut' ? 1 : r.kind === 'mcx' ? 1 : r.kind === 'opt' ? 2 : 3);
    // Sub-group order WITHIN the Movers major — underlying first
    // (most-actively-traded, F&O-eligible names), then midcap, then
    // smallcap. Unknown / null sub-groups go last (defensive — every
    // row from loadMovers carries one of the three tags).
    const MOVER_GROUP_ORDER = { underlying: 0, midcap: 1, smallcap: 2 };
    // Direction order: Winners first, Losers second. Within each
    // direction the underlying → midcap → smallcap sub-grouping
    // applies. Result inside Movers:
    //   Winners → Underlyings, Midcap, Smallcap
    //   Losers  → Underlyings, Midcap, Smallcap
    const MOVER_DIRECTION_ORDER = { winners: 0, losers: 1 };
    const _mgOrder = (r) =>
      r._majorGroup === 'movers' ? (MOVER_GROUP_ORDER[r._moverGroup] ?? 9) : -1;
    const _mdOrder = (r) =>
      r._majorGroup === 'movers' ? (MOVER_DIRECTION_ORDER[r._moverDirection] ?? 9) : -1;
    rows.sort((a, b) => {
      const moA = a._majorOrder ?? 99, moB = b._majorOrder ?? 99;
      if (moA !== moB) return moA - moB;
      // Direction first inside Movers (winners → losers).
      const mdA = _mdOrder(a), mdB = _mdOrder(b);
      if (mdA !== mdB) return mdA - mdB;
      // Sub-group ordering applies ONLY to the Movers major — other
      // majors keep their existing underlying-then-tier sort. _mgOrder
      // returns -1 for non-mover rows so this branch is a no-op there.
      const mgA = _mgOrder(a), mgB = _mgOrder(b);
      if (mgA !== mgB) return mgA - mgB;
      // Inside a Movers sub-group, biggest mover first (by % change abs).
      if (a._majorGroup === 'movers' && b._majorGroup === 'movers') {
        const pA = Math.abs(Number(a._mover_change_pct) || Number(a.change_pct) || 0);
        const pB = Math.abs(Number(b._mover_change_pct) || Number(b.change_pct) || 0);
        if (pA !== pB) return pB - pA;
      }
      const ua = _ugKey(a), ub = _ugKey(b);
      if (ua !== ub) return ua.localeCompare(ub);
      const ta = _tier(a),  tb = _tier(b);
      if (ta !== tb) return ta - tb;
      return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
    });
    // First-row flags: major divider as before; direction divider for
    // the first row of each Winners/Losers block; sub-group divider for
    // each (underlying / midcap / smallcap) section inside the
    // direction block.
    let lastMajor = null;
    let lastMoverDirection = null;
    let lastMoverGroup = null;
    for (const r of rows) {
      r._majorFirst = (r._majorGroup !== lastMajor);
      lastMajor = r._majorGroup;
      if (r._majorGroup === 'movers') {
        r._moverDirectionFirst = (r._moverDirection !== lastMoverDirection);
        lastMoverDirection = r._moverDirection;
        // Reset sub-group tracker when direction flips so the first
        // sub-group of Losers gets its own divider regardless of what
        // the last Winners sub-group was.
        if (r._moverDirectionFirst) lastMoverGroup = null;
        r._moverGroupFirst = (r._moverGroup !== lastMoverGroup);
        lastMoverGroup = r._moverGroup;
      } else {
        r._moverDirectionFirst = false;
        r._moverGroupFirst = false;
      }
    }
    return rows;
  });

  // 6-grid layout — derive each bucket's rows from mainRows /
  // pinnedTopRows so each ag-Grid instance receives exactly the
  // subset it renders. Pinned + Watchlist + Movers Winners + Movers
  // Losers feed the left-style monitoring grids; Positions +
  // Holdings feed the right-style book grids (with TOTAL rows
  // pinned at the bucket's bottom edge).
  const pinnedRows     = $derived(pinnedTopRows);
  // Single-pass bucketing over mainRows. The previous shape had four
  // separate `$derived(mainRows.filter(...))` chains (_allWatchRows,
  // positionsRows, holdingsRows + watchCounts iterated _allWatchRows
  // again), so every mainRows change triggered four full passes.
  // Bucketing in one pass cuts that to a single O(n) walk per
  // mainRows update — meaningful on the initial-paint hot path where
  // mainRows is 100-300 rows wide and 4 filter passes adds up to a
  // visible delay on lower-end devices.
  const _rowBuckets = $derived.by(() => {
    const watch = [];
    const positions = [];
    const holdings = [];
    /** @type {Record<number, number>} */
    const counts = {};
    for (const r of mainRows) {
      const mg = r._majorGroup;
      if (mg === 'watchlist') {
        watch.push(r);
        const id = Number(r.watchlist_list_id);
        if (Number.isFinite(id)) counts[id] = (counts[id] || 0) + 1;
      } else if (mg === 'positions') {
        positions.push(r);
      } else if (mg === 'holdings') {
        holdings.push(r);
      }
    }
    return { watch, positions, holdings, counts };
  });
  const _allWatchRows  = $derived(_rowBuckets.watch);
  // User watchlists — capped at 5 visible tabs (anything beyond can be
  // reached via the Show dropdown). Each list becomes its own top-tab.
  const _userLists = $derived((lists || []).filter(l => !l.is_pinned).slice(0, 5));
  // watchRows now reads topTab directly — when topTab is a list id,
  // filter to that list's rows; when it's 'pinned' the watch grid is
  // hidden so the filter doesn't matter (shows all user-list rows).
  const watchRows = $derived(
    typeof topTab === 'number'
      ? _allWatchRows.filter(r => r.watchlist_list_id === topTab)
      : _allWatchRows
  );
  // Per-tab row counts so the tab pill can show its denominator.
  const watchCounts = $derived(_rowBuckets.counts);
  const positionsRows  = $derived(_rowBuckets.positions);
  const holdingsRows   = $derived(_rowBuckets.holdings);
  // Set of watchlist list-ids that are flagged `is_global`. Hoisted out
  // of `symRenderer` so the per-row `lists.find(l => l.id === ... &&
  // l.is_global)` linear scan becomes an O(1) `Set.has()`. With ~50
  // visible rows × multiple SSE-triggered `refreshCells` per second,
  // this turns ~50 walks/tick into one walk per `lists` change.
  const _globalListIds = $derived(
    new Set((lists || []).filter(l => l?.is_global).map(l => l.id))
  );
  // Set of underlying symbols where the operator currently has any
  // OPEN option/future position (qty != 0). Feeds the holdings lot
  // chip: amber chip → underlying already has live derivative
  // exposure (covered call / hedge / spread in play); green chip →
  // clean F&O underlying with no derivative position yet. The check
  // walks the raw `positions` array (not positionsRows, which is the
  // unified-grid view) so it sees every option/future contract the
  // operator holds, parsed via the instruments cache for the
  // underlying name.
  const _underlyingsWithActivePositions = $derived.by(() => {
    const set = new Set();
    if (!getInstrument) return set;
    for (const p of positions) {
      const qty = Number(p?.quantity ?? p?.qty_pos ?? 0);
      if (!qty) continue;
      const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
      if (!sym) continue;
      const inst = getInstrument(sym);
      const u = inst?.u ? String(inst.u).toUpperCase() : '';
      if (u) set.add(u);
    }
    return set;
  });
  // Sub-group tabs on the Winners + Losers grids — each grid scopes
  // to one of five universes:
  //   underlying → major indices (NIFTY 50, BANKNIFTY, etc.)
  //   large_cap  → F&O-eligible stocks
  //   midcap     → Nifty Midcap 100
  //   smallcap   → Nifty Smallcap 100
  //   holdings   → operator's own holdings rows (only relevant when
  //                  an account is picked; otherwise empty)
  // Every tab caps at top 10 by |change_pct| so the grid stays
  // scan-tight; long-tail names trail off the visible window.
  // Holdings tab retired per operator request — Winners/Losers focus
  // on the market scan; operator's own book lives on the right column.
  const MOVER_TABS = /** @type {const} */ (['underlying', 'large_cap', 'midcap', 'smallcap']);
  /** @typedef {'underlying'|'large_cap'|'midcap'|'smallcap'} MoverTab */
  const _MOVER_TOP_N = 10;
  const MOVER_TAB_LABEL = /** @type {Record<MoverTab,string>} */ ({
    underlying: 'Underlying',
    large_cap:  'L.Cap',
    midcap:     'M.Cap',
    smallcap:   'S.Cap',
  });
  let winTab  = $state(/** @type {MoverTab} */ ('underlying'));
  let loseTab = $state(/** @type {MoverTab} */ ('underlying'));

  // Top-N filter for a direction × tab. All tabs draw from the
  // movers fetch (Holdings tab retired — operator's book lives
  // in the right column's Holdings card).
  /** @param {'winners'|'losers'} direction
   *  @param {MoverTab} tab */
  function _topRowsFor(direction, tab) {
    let pool;
    if (tab === 'large_cap') {
      // Large Cap = F&O stocks only. Reads the _isLargeCap flag
      // (set in loadMovers) rather than the _moverGroup bucket so
      // we don't compete with the broader Underlying tab.
      pool = mainRows.filter(r =>
        r._majorGroup === 'movers'
        && r._moverDirection === direction
        && r._isLargeCap === true);
    } else {
      // Underlying / Midcap / Smallcap → straight bucket match.
      pool = mainRows.filter(r =>
        r._majorGroup === 'movers'
        && r._moverDirection === direction
        && r._moverGroup === tab);
    }
    return pool
      .slice()
      .sort((a, b) => Math.abs(Number(b.change_pct) || 0)
                    - Math.abs(Number(a.change_pct) || 0))
      .slice(0, _MOVER_TOP_N);
  }
  const winRows  = $derived(_topRowsFor('winners', winTab));
  const loseRows = $derived(_topRowsFor('losers',  loseTab));

  // Per-tab denominator badges. Counts mirror the full pool (NOT
  // the top-N slice) so the operator sees how many candidates
  // exist before the cap kicks in.
  /** @param {'winners'|'losers'} direction */
  function _tabCounts(direction) {
    const out = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of mainRows) {
      if (r._majorGroup !== 'movers') continue;
      if (r._moverDirection !== direction) continue;
      const g = /** @type {'underlying'|'midcap'|'smallcap'} */ (r._moverGroup);
      if (g === 'underlying' || g === 'midcap' || g === 'smallcap') out[g]++;
      // Large Cap is a SUBSET of underlying — counted in addition.
      if (r._isLargeCap) out.large_cap++;
    }
    return out;
  }
  const winnerCounts = $derived(_tabCounts('winners'));
  const loserCounts  = $derived(_tabCounts('losers'));

  // TOTAL row for one major (positions / holdings). Each carries
  // summed day_pnl + pnl + cost_basis so the Day P&L % and P&L %
  // columns auto-derive via their valueGetters. Wrapped in an array
  // for direct use as pinnedBottomRowData (ag-Grid accepts an empty
  // array when there's nothing to sum).
  function _totalsRowFor(rows, major, label) {
    if (!rows.length) return null;
    let day_pnl = 0, pnl = 0, cost = 0, prevMktVal = 0;
    let invSum = 0, curSum = 0;
    let anyInv = false, anyCur = false;
    let qty_pos = 0, qty_hold = 0;
    let anyDayPnl = false, anyPnl = false;
    for (const r of rows) {
      // Prefer the BROKER raw values (`_broker_pnl`, `_broker_day_pnl`
      // mirrored on each row inside buildUnified) so the TOTAL row
      // matches PositionStrip exactly — the strip reads `r.pnl` and
      // `r.day_change_val` straight off the API. Per-row P&L cells
      // still display the live-recomputed `row.pnl` / `row.day_pnl`
      // so they tick with quotes; only the TOTAL falls back to the
      // broker snapshot for cross-surface sync. Small per-row vs
      // TOTAL delta during active trading is an accepted tradeoff.
      const rowPnl     = (r._broker_pnl     != null) ? r._broker_pnl     : r.pnl;
      const rowDayPnl  = (r._broker_day_pnl != null) ? r._broker_day_pnl : r.day_pnl;
      if (rowDayPnl != null) { day_pnl += Number(rowDayPnl) || 0; anyDayPnl = true; }
      if (rowPnl    != null) { pnl     += Number(rowPnl)    || 0; anyPnl    = true; }
      cost       += Math.abs(Number(r._cost_basis)        || 0);
      prevMktVal += Math.abs(Number(r._prev_market_value) || 0);
      if (r.inv_val != null) { invSum += Number(r.inv_val) || 0; anyInv = true; }
      if (r.cur_val != null) { curSum += Number(r.cur_val) || 0; anyCur = true; }
      qty_pos += Number(r.qty_pos)  || 0;
      qty_hold += Number(r.qty_hold) || 0;
    }
    return {
      key: `__total_${major}`,
      _isTotal: true,
      _majorGroup: major,
      tradingsymbol: label,
      day_pnl:  anyDayPnl ? day_pnl : null,
      pnl:      anyPnl    ? pnl     : null,
      _cost_basis: cost,
      _prev_market_value: prevMktVal,
      inv_val:  anyInv ? invSum : null,
      cur_val:  anyCur ? curSum : null,
      qty_pos, qty_hold,
    };
  }
  const positionsTotalRows = $derived.by(() => {
    const t = _totalsRowFor(positionsRows, 'positions', 'TOTAL Positions');
    return t ? [t] : [];
  });
  const holdingsTotalRows = $derived.by(() => {
    const t = _totalsRowFor(holdingsRows, 'holdings', 'TOTAL Holdings');
    return t ? [t] : [];
  });

  // Mobile breakpoint tracker — matches the Tailwind `lg:` 1024 px
  // cutoff the rest of the app uses. Cards on desktop fill the
  // available viewport height via flex; cards on mobile collapse
  // when their bucket is empty so the operator's scroll length
  // doesn't bloat with header-only stubs before market open.
  let _isMobile = $state(false);
  if (typeof window !== 'undefined') {
    onMount(() => {
      const mq = window.matchMedia('(max-width: 1023px)');
      _isMobile = mq.matches;
      const onChange = (/** @type {MediaQueryListEvent} */ e) => { _isMobile = e.matches; };
      mq.addEventListener('change', onChange);
      return () => mq.removeEventListener('change', onChange);
    });
  }

  // Effective collapse state per bucket — operator-driven on desktop;
  // auto-collapse on empty on mobile so empty cards don't add to the
  // mobile scroll length. Desktop keeps every card expanded with the
  // inline empty-state message rendered inside the grid body, then
  // flex-grow distributes viewport height across the column so the
  // dashboard stays visually aligned regardless of data availability.
  //
  // Winner/loser counts kept for the per-tab badge in the card header.
  const _winnersTotal = $derived(
    winnerCounts.underlying + winnerCounts.large_cap
    + winnerCounts.midcap + winnerCounts.smallcap);
  const _losersTotal = $derived(
    loserCounts.underlying + loserCounts.large_cap
    + loserCounts.midcap + loserCounts.smallcap);
  // Effective collapse = operator's explicit choice only. The earlier
  // `(_isMobile && rows.length === 0)` auto-collapse meant: while data
  // is still loading OR when an operator's book legitimately has zero
  // rows in a bucket, the card body would render at height: 0 with
  // only the header showing — and on the post-sticky-footer layout
  // this looked like a broken card (no body, no footer-floating to
  // hint at the empty zone below). Let the ag-Grid render its own
  // "No rows to display" message so the operator sees a real empty
  // state instead of a phantom collapsed shell.
  const _effColPinWatch  = $derived(_colPinWatch);
  const _effColWinners   = $derived(_colWinners);
  const _effColLosers    = $derived(_colLosers);
  const _effColPositions = $derived(_colPositions);
  const _effColHoldings  = $derived(_colHoldings);

  // Desktop proportional sizing — each bucket's flex-grow is proportional
  // to its visible row count so taller buckets claim more column height.
  // Minimum of 1 keeps empty buckets from collapsing to zero; collapsed
  // buckets intentionally keep their weight (the header stays visible).
  // PinWatch shows either pinnedRows or watchRows depending on topTab.
  const _bRowsPinWatch  = $derived(Math.max(1,
    topTab === 'pinned' ? pinnedRows.length : watchRows.length));
  const _bRowsWinners   = $derived(Math.max(1, winRows.length));
  const _bRowsLosers    = $derived(Math.max(1, loseRows.length));
  // +1 for the pinned TOTAL row at the bottom of each book grid.
  const _bRowsPositions = $derived(Math.max(1, positionsRows.length + 1));
  const _bRowsHoldings  = $derived(Math.max(1, holdingsRows.length  + 1));

  // One effect per grid — Svelte 5 reactivity tracks the closed-over
  // derivation so any source change automatically pushes fresh row
  // data without us having to re-bundle effects.
  $effect(() => { if (gridPinnedReady && gridPinned)
    gridPinned.setGridOption('rowData', pinnedRows); });
  $effect(() => { if (gridWatchReady && gridWatch)
    gridWatch.setGridOption('rowData', watchRows); });
  $effect(() => { if (gridPositionsReady && gridPositions) {
    gridPositions.setGridOption('rowData', positionsRows);
    gridPositions.setGridOption('pinnedBottomRowData', positionsTotalRows);
  } });
  $effect(() => { if (gridHoldingsReady && gridHoldings) {
    gridHoldings.setGridOption('rowData', holdingsRows);
    gridHoldings.setGridOption('pinnedBottomRowData', holdingsTotalRows);
  } });
  $effect(() => { if (gridWinReady && gridWin)
    gridWin.setGridOption('rowData', winRows); });
  $effect(() => { if (gridLoseReady && gridLose)
    gridLose.setGridOption('rowData', loseRows); });

  // ag-Grid doesn't observe $state reads inside cell renderers, so
  // sparkline updates after the row data has stabilised won't trigger
  // a re-render on their own. Explicitly refresh the Curve column
  // whenever the sparklines map changes. Live LTP tail updates are
  // handled by the separate RAF-debounced effect below (it also
  // refreshes the sparkline column via _liveLtpSnap → sparkRenderer).
  $effect(() => {
    sparklines;
    // Refresh the Curve column only on grids that are currently visible.
    if (gridPinnedReady    && gridPinned    && topTab === 'pinned')    gridPinned.refreshCells({ columns: ['sparkline'], force: true });
    if (gridWatchReady     && gridWatch     && typeof topTab === 'number') gridWatch.refreshCells({ columns: ['sparkline'], force: true });
    if (gridPositionsReady && gridPositions && showPositions)          gridPositions.refreshCells({ columns: ['sparkline'], force: true });
    if (gridHoldingsReady  && gridHoldings  && showHoldings)           gridHoldings.refreshCells({ columns: ['sparkline'], force: true });
    if (gridWinReady       && gridWin       && showWinners)            gridWin.refreshCells({ columns: ['sparkline'], force: true });
    if (gridLoseReady      && gridLose      && showLosers)             gridLose.refreshCells({ columns: ['sparkline'], force: true });
  });

  // Refresh both the LTP column and sparkline tail whenever the SSE
  // stream delivers new tick values. Throttled to 4 Hz (250 ms minimum
  // gap) and diff-gated: if all LTP values are identical to the last
  // paint we skip the refreshCells call entirely.
  //
  // Prior approach: rAF-debounce (~60 Hz cap) fired on every $state
  // change regardless of whether any price actually changed. At ~93
  // SSE ticks/sec that was 60 × 6 = 360 refreshCells calls/sec even
  // during a flat market. The 250 ms floor caps it at 4 × 6 = 24
  // calls/sec; the diff-gate cuts it to 0 when the stream is idle.
  let _lastPaintedSnap = /** @type {Record<string, number>} */ ({});
  let _ltpPaintTimer   = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
  const _LTP_PAINT_MS  = 250; // 4 Hz
  // B1 — symbols whose LTP just changed; cleared after animation window.
  // Two sets so the LTP cell can flash GREEN on tick-up vs RED on tick-down.
  // Slice AS audit defect: the prior single-set + amber `ltp-flash` lost
  // direction information on the product's highest-frequency cell. Pro
  // terminals (Bloomberg, IBKR) always split.
  let _ltpFlashUp   = $state(/** @type {Set<string>} */ (new Set()));
  let _ltpFlashDown = $state(/** @type {Set<string>} */ (new Set()));

  $effect(() => {
    const snap = _liveLtpSnap; // reactive subscribe — re-runs on every update

    // Diff: skip paint when no value changed since the last paint.
    let changed = false;
    const snapKeys = Object.keys(snap);
    for (const k of snapKeys) {
      if (snap[k] !== _lastPaintedSnap[k]) { changed = true; break; }
    }
    if (!changed) {
      // Also catch keys that were removed from the snap.
      for (const k of Object.keys(_lastPaintedSnap)) {
        if (!(k in snap)) { changed = true; break; }
      }
    }
    if (!changed) return;

    // Throttle: schedule one paint at most every _LTP_PAINT_MS.
    if (_ltpPaintTimer) return;
    // Schedule via requestIdleCallback so the heavy ag-Grid refreshCells
    // batch runs only when the main thread is idle. Click handlers and
    // input dispatch jump the queue ahead of this work. Timeout 500ms
    // forces the paint even on a saturated thread so cells don't go
    // stale during heavy tick bursts. Safari fallback: setTimeout with
    // a minimal delay so the work still yields one tick.
    const _scheduleIdle = (cb) => {
      if (typeof window !== 'undefined'
          && typeof window.requestIdleCallback === 'function') {
        window.requestIdleCallback(cb, { timeout: 500 });
      } else {
        setTimeout(cb, 1);
      }
    };
    _ltpPaintTimer = setTimeout(() => {
      _ltpPaintTimer = null;
      // B1 — collect symbols whose value changed so the LTP cell can flash.
      // Split by direction (up=green, down=red) — slice AS audit fix.
      const prev = _lastPaintedSnap;
      const cur  = _liveLtpSnap;
      const flashedUp   = new Set(/** @type {string[]} */ ([]));
      const flashedDown = new Set(/** @type {string[]} */ ([]));
      // Design B: freshness shimmer — collect ALL symbols that arrived in this
      // tick batch (not just those whose value changed). notifyAll fires only
      // when the tab is visible (guard is inside createFreshnessShimmer).
      const freshnessKeys = /** @type {string[]} */ ([]);
      for (const k of Object.keys(cur)) {
        const p = prev[k];
        if (p === undefined) continue;
        freshnessKeys.push(k);
        const c = cur[k];
        if (c > p) flashedUp.add(k);
        else if (c < p) flashedDown.add(k);
      }
      if (freshnessKeys.length > 0) _ltpShimmer.notifyAll(freshnessKeys);
      if (flashedUp.size > 0 || flashedDown.size > 0) {
        _ltpFlashUp   = flashedUp;
        _ltpFlashDown = flashedDown;
        setTimeout(() => {
          _ltpFlashUp   = new Set();
          _ltpFlashDown = new Set();
        }, 650);
      }
      // Capture the current snapshot at paint time (may have advanced
      // further than when the timer was scheduled).
      _lastPaintedSnap = { ..._liveLtpSnap };
      // Defer the actual ag-Grid refresh batch until the main thread
      // is idle so clicks always jump the queue. The flash class
      // assignments above already happened synchronously — only the
      // expensive grid work runs on idle.
      _scheduleIdle(() => {
        if (gridPinnedReady    && gridPinned    && topTab === 'pinned')    gridPinned.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
        if (gridWatchReady     && gridWatch     && typeof topTab === 'number') gridWatch.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
        if (gridPositionsReady && gridPositions && showPositions)          gridPositions.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
        if (gridHoldingsReady  && gridHoldings  && showHoldings)           gridHoldings.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
        if (gridWinReady       && gridWin       && showWinners)            gridWin.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
        if (gridLoseReady      && gridLose      && showLosers)             gridLose.refreshCells({ columns: ['ltp', 'sparkline'], force: true });
      });
    }, _LTP_PAINT_MS);
  });

  // Per-source summary derivations for the two separate summary grids.
  // Account picker scopes the body rows; TOTAL pinned at the bottom.
  function isTotalRow(r) { return r && r.account === 'TOTAL'; }

  // Positions Summary — Day P&L + Day % + P&L per account.
  //
  // Pass-through: backend `/api/positions` already returns one row per
  // account + a TOTAL row, with `day_change_percentage` correctly
  // computed as Σ day_change_val / Σ |close × qty| × 100. The frontend
  // used to re-derive `day_change_percentage` from a phantom `inv_val`
  // field that doesn't exist on `PositionsSummaryRow` — silent null.
  // TOTAL is dropped when an account filter is active because it
  // reflects every account, not the filtered subset.
  const positionsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('positions')) return [];
    const filterActive = positionsAccounts && positionsAccounts.length > 0;
    return positionsSummary
      .filter(r => {
        if (!r?.account) return false;
        if (r.account === 'TOTAL') return !filterActive;
        return _includesPosAcct(r.account);
      })
      .map(r => ({
        account: r.account,
        day_pnl: Number(r.day_change_val) || 0,
        pnl:     Number(r.pnl)            || 0,
        day_change_percentage: r.day_change_percentage ?? null,
        // Carry the per-account |close × qty| sum through so the
        // filtered-subset synthesised TOTAL can derive a proper
        // Day % (Σday_pnl / Σday_prev_val × 100).
        day_prev_val: Number(r.day_prev_val) || 0,
      }));
  });

  // Holdings Summary — Day P&L + Day % + P&L + P&L % + Cur Val + Inv Val per account.
  //
  // Pass-through for the same reason as positions above: backend's
  // `day_change_percentage` uses the (cur_val − day_change_val) denominator
  // (yesterday's value), matching `PerformancePage.makeHoldingsTotals`.
  // The frontend used to divide by `inv_val` instead, which gave a
  // different number for the same column on the same screen.
  const holdingsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('holdings')) return [];
    const filterActive = holdingsAccounts && holdingsAccounts.length > 0;
    return holdingsSummary
      .filter(r => {
        if (!r?.account) return false;
        // Backend TOTAL is the all-accounts sum — keep when no filter
        // is active so the operator sees the firm-wide number. When a
        // filter is active, drop it and compute a filtered TOTAL below
        // (operator request: "the holdings grid should continue to
        // show total row when accounts are filtered").
        if (r.account === 'TOTAL') return !filterActive;
        return _includesHoldAcct(r.account);
      })
      .map(r => ({
        account: r.account,
        day_pnl: Number(r.day_change_val) || 0,
        pnl:     Number(r.pnl)            || 0,
        cur_val: Number(r.cur_val)        || 0,
        inv_val: Number(r.inv_val)        || 0,
        day_change_percentage: r.day_change_percentage ?? null,
        pnl_percentage:        r.pnl_percentage        ?? null,
      }));
  });

  // Sum body rows into a synthesised TOTAL — used when an account
  // filter is active and the backend's all-accounts TOTAL row is
  // therefore irrelevant.
  //   - Holdings carry cur_val + inv_val; Day % = day_pnl/(cur_val−day_pnl),
  //     P&L % = pnl/inv_val. Matches PerformancePage + backend.
  //   - Positions carry day_prev_val (|close × qty| sum); Day % =
  //     day_pnl / day_prev_val × 100. Matches backend positions.py
  //     summary derivation cell-for-cell.
  // Missing-field branches return null so the grid renders an em-dash
  // rather than NaN.
  function _synthesiseTotalRow(/** @type {any[]} */ rows) {
    const t = {
      account: 'TOTAL',
      day_pnl: 0, pnl: 0, cur_val: 0, inv_val: 0, day_prev_val: 0,
      day_change_percentage: null, pnl_percentage: null,
    };
    for (const r of rows) {
      t.day_pnl      += Number(r.day_pnl)      || 0;
      t.pnl          += Number(r.pnl)          || 0;
      t.cur_val      += Number(r.cur_val)      || 0;
      t.inv_val      += Number(r.inv_val)      || 0;
      t.day_prev_val += Number(r.day_prev_val) || 0;
    }
    // Holdings path — cur_val / inv_val available.
    if (t.cur_val > 0 || t.inv_val > 0) {
      const yest = t.cur_val - t.day_pnl;
      t.day_change_percentage = (yest > 0) ? (t.day_pnl / yest) * 100 : null;
      t.pnl_percentage        = (t.inv_val > 0) ? (t.pnl / t.inv_val) * 100 : null;
    }
    // Positions path — day_prev_val carries the absolute-notional sum
    // backend already computed per account. Σday_pnl / Σday_prev_val is
    // exactly what backend's TOTAL row uses.
    else if (t.day_prev_val > 0) {
      t.day_change_percentage = (t.day_pnl / t.day_prev_val) * 100;
    }
    return t;
  }

  const positionsSummaryBody  = $derived(
    positionsSummaryData.filter(r => !isTotalRow(r) && _includesPosAcct(r.account))
  );
  const positionsSummaryTotal = $derived.by(() => {
    const filterActive = positionsAccounts && positionsAccounts.length > 0;
    // Backend TOTAL when no filter; synthesised TOTAL across the
    // filtered body when filter is active. Operator always sees a
    // TOTAL row pinned at the bottom.
    return filterActive
      ? [_synthesiseTotalRow(positionsSummaryBody)]
      : positionsSummaryData.filter(isTotalRow);
  });

  const holdingsSummaryBody  = $derived(
    holdingsSummaryData.filter(r => !isTotalRow(r) && _includesHoldAcct(r.account))
  );
  const holdingsSummaryTotal = $derived.by(() => {
    const filterActive = holdingsAccounts && holdingsAccounts.length > 0;
    return filterActive
      ? [_synthesiseTotalRow(holdingsSummaryBody)]
      : holdingsSummaryData.filter(isTotalRow);
  });

  $effect(() => {
    if (!positionsSummaryReady || !positionsSummaryGrid) return;
    positionsSummaryGrid.setGridOption('rowData', positionsSummaryBody);
    positionsSummaryGrid.setGridOption('pinnedBottomRowData', positionsSummaryTotal);
  });

  $effect(() => {
    if (!holdingsSummaryReady || !holdingsSummaryGrid) return;
    holdingsSummaryGrid.setGridOption('rowData', holdingsSummaryBody);
    holdingsSummaryGrid.setGridOption('pinnedBottomRowData', holdingsSummaryTotal);
  });

  // Per-account cash debited on currently-held long options — same
  // derivation the strip uses, but bucketed by account. For each long
  // CE/PE position row:
  //   num_lots = quantity / lot_size
  //   cash    = average_price × lot_size × num_lots
  // (mathematically equivalent to avg × qty, but explicit so the
  // formula matches the operator's mental model and stays correct
  // for any adapter that surfaces qty in lots).
  const longOptCashByAccount = $derived.by(() => {
    /** @type {Record<string, number>} */
    const m = {};
    let total = 0;
    for (const p of positions) {
      const sym = String(p?.tradingsymbol || '').toUpperCase();
      if (!(sym.endsWith('CE') || sym.endsWith('PE'))) continue;
      const rawQty = Number(p?.quantity) || 0;
      if (rawQty <= 0) continue;
      const qty = Math.abs(rawQty);
      const avg = Number(p?.average_price) || 0;
      const inst    = getInstrument?.(sym);
      const lotSize = Number(inst?.ls) || 0;
      const v = lotSize > 0
        ? avg * lotSize * (qty / lotSize)
        : avg * qty;  // fallback when instruments cache cold
      const a = String(p?.account || '');
      if (a) m[a] = (m[a] || 0) + v;
      total += v;
    }
    m.TOTAL = total;
    return m;
  });

  const scopedFunds = $derived.by(() => {
    // Funds isn't owned by either Positions or Holdings — scope by
    // the UNION of both pickers so an account selected in either
    // card shows up here. Empty + empty = all.
    const list = funds.filter(r => isTotalRow(r) || _includesFundsAcct(r.account));
    // Inject _long_opt_cash so the grid's `cash_total` valueGetter
    // can pick it up without poking back into the positions array.
    return list.map(r => ({
      ...r,
      _long_opt_cash: longOptCashByAccount[String(r.account || '')] || 0,
    }));
  });

  $effect(() => {
    if (!fundsReady || !fundsGrid) return;
    const body  = scopedFunds.filter(r => !isTotalRow(r));
    const total = scopedFunds.filter(isTotalRow);
    fundsGrid.setGridOption('rowData', body);
    fundsGrid.setGridOption('pinnedBottomRowData', total);
  });

  onDestroy(() => {
    stopPulseTick?.(); stopTickSettingPoll?.(); stopWS?.();
    stopQuoteStream();
    // Clear any in-flight throttle timers so their setTimeout
    // callbacks don't fire into a destroyed component (audit-flagged
    // — the flush timer is cleaned up by the $effect's own teardown,
    // but the paint + prefetch timers weren't).
    if (_ltpPaintTimer) { clearTimeout(_ltpPaintTimer); _ltpPaintTimer = null; }
    _ltpShimmer.dispose();
    for (const t of _prefetchTimers) { try { clearTimeout(t); } catch { /* no-op */ } }
    _prefetchTimers.length = 0;
    document.removeEventListener('keydown', handleKeydown);
    document.removeEventListener('click', onDocClick);
    gridPinned?.destroy?.();
    gridWatch?.destroy?.();
    gridPositions?.destroy?.();
    gridHoldings?.destroy?.();
    gridWin?.destroy?.();
    gridLose?.destroy?.();
    positionsSummaryGrid?.destroy?.();
    holdingsSummaryGrid?.destroy?.();
    fundsGrid?.destroy?.();
  });

  async function loadLists() {
    try {
      lists = await fetchWatchlists();
      if (!lists.length) return;
      // selectedShow is seeded eagerly with source tokens at declaration
      // (so the grid renders even when there are no watchlists). Append
      // every loaded watchlist's wl: token IF no wl: tokens are present
      // yet — first-time activation only, so the operator's later
      // deselections aren't clobbered by a subsequent loadLists() poll.
      const hasAnyWl = selectedShow.some(v => v.startsWith('wl:'));
      if (!hasAnyWl) {
        const wlTokens = lists.map(l => `wl:${l.id}`);
        selectedShow = [...selectedShow, ...wlTokens];
      } else {
        // Lists may have changed underneath us — drop any wl: token
        // whose id no longer exists. The pruning $effect at the top
        // does the same thing, but doing it here too means activeIds
        // is correct on the very next tick (no flicker).
        const validIds = new Set(lists.map(l => `wl:${l.id}`));
        const trimmed = selectedShow.filter(v =>
          v.startsWith('wl:') ? validIds.has(v) : true
        );
        if (trimmed.length !== selectedShow.length) selectedShow = trimmed;
      }
      // focusedListId: where new symbols added via the search popup land.
      // No tabs anymore, so the operator can't switch this from the UI —
      // default to the user's default watchlist (or the first selected).
      if (focusedListId == null || !activeIds.has(focusedListId)) {
        const def = lists.find(l => l.is_default && activeIds.has(l.id))
                 ?? lists.find(l => activeIds.has(l.id))
                 ?? lists.find(l => l.is_default)
                 ?? lists[0];
        focusedListId = def?.id ?? null;
      }
    } catch (e) { error = e.message; }
  }

  // loadActive — fetches watchlist items for the current activeIds set
  // and then triggers a per-list quote fetch that publishes directly
  // into symbolStore (BH4 — no intermediary watchQuotesStore). When
  // no lists are selected, empties activeListsStore immediately.
  async function loadActive() {
    error = '';
    const ids = [...activeIds];
    if (ids.length === 0) {
      activeListsStore.set([]);
      return;
    }
    try {
      await activeListsStore.load(ids);
      await loadQuotes();
    } catch (e) { error = e.message; }
  }

  // loadQuotes — direct per-list /watchlist/{id}/quotes fetch that
  // publishes the response into symbolStore. The 700ms thunder-herd
  // gate skips when loadPulse just ran (positions/holdings already
  // refreshed the same syms). Slice BH4 removed the watchQuotesStore
  // wrapper — its only consumer was buildUnified's wq[it.id] lookup,
  // which is gone now that symbolStore is the single market-data sink.
  async function loadQuotes() {
    const ids = [...activeIds];
    if (ids.length === 0) return;
    // De-thundering-herd: skip this tick if loadPulse just ran.
    if (Date.now() - _lastPulseAt < 700) return;
    try {
      const results = await Promise.all(
        ids.map(id => fetchWatchlistQuotes(id).catch(() => null))
      );
      /** @type {Record<number, any>} */
      const map = {};
      for (const r of results) {
        if (!r) continue;
        for (const q of (r.items || [])) map[q.item_id] = q;
      }
      publishWatchQuotes(map);
      refreshedAt = new Date().toISOString();
      if (quoteFailStreak > 0) {
        quoteFailStreak = 0;
        if (/Quote refresh/.test(error)) error = '';
      }
    } catch (e) {
      quoteFailStreak++;
      if (quoteFailStreak >= 3) {
        error = `Quote refresh: ${e.message}`;
      }
    }
  }

  /** Chunked batch-quote — 200-key chunks, parallel, 5 s per-chunk
   *  timeout so a slow Kite round-trip can't stall the next 10 s tick. */
  async function batchQuoteChunked(keys) {
    if (!keys || !keys.length) return [];
    const CHUNK = 200;
    const TIMEOUT_MS = 5000;
    const chunks = [];
    for (let i = 0; i < keys.length; i += CHUNK) chunks.push(keys.slice(i, i + CHUNK));
    const withTimeout = (p) => Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error('quote timeout')), TIMEOUT_MS)),
    ]);
    const results = await Promise.all(chunks.map(c =>
      withTimeout(batchQuote(c)).catch(() => ({ items: [] }))
    ));
    return results.flatMap(r => (r?.items || []));
  }

  // Resolve underlying for an OPTION position. For MCX commodities the
  // option's underlying is the same-month future (CRUDEOIL26JUN10500CE
  // settles to CRUDEOIL26JUNFUT, not the front-month MAY future). For
  // indices/stocks the spot is shared across all expiries so we just
  // delegate to resolveUnderlying.
  function resolveUnderlyingForOption(name, optionExpiryISO,
                                       /** @type {((u: string) => any) | null} */ findNearestFut,
                                       /** @type {((u: string) => any[]) | null} */ listFuts) {
    const n = String(name || '').toUpperCase();
    if (!n) return null;
    if (MCX_COMMODITIES.has(n) && optionExpiryISO && listFuts) {
      const ym = String(optionExpiryISO).slice(0, 7);  // YYYY-MM
      const futs = listFuts(n) || [];
      const same = futs.find(f => String(f.x || '').slice(0, 7) === ym);
      if (same?.s && same?.e) {
        return {
          tradingsymbol: same.s,
          exchange: same.e,
          quoteKey: `${same.e}:${same.s}`,
          // underlying_group keeps the month suffix so multiple option
          // months for the same commodity each map to their own future
          // (CRUDEOIL_2026-06 → CRUDEOIL26JUNFUT,
          //  CRUDEOIL_2026-07 → CRUDEOIL26JULFUT). It's a per-anchor
          // dedup key for the underlyingInfos Map only.
          underlying_group: `${n}_${ym}`,
          // displayUnderlying is the BARE commodity name — the value
          // that goes onto row.underlying so the anchor groups with
          // its option positions (which carry underlying='CRUDEOIL'
          // from the instrument cache). Without this, the anchor
          // ended up in its own 'CRUDEOIL_2026-06' group, floating
          // off above/below the CRUDEOIL option block.
          displayUnderlying: n,
          kind: 'fut',
        };
      }
    }
    // Non-MCX or no same-month match → fall back to nearest-month
    // (still useful — indices/stocks have a single spot, the U badge
    // groups every option month under it).
    return resolveUnderlying(n, findNearestFut);
  }

  async function loadPulse() {
    try {
      // Fetch positions + holdings via the shared three-tier stores (slice AB).
      // Per-feed error capture still needs the raw fetch result for the
      // brokerErr banner, so we call the API directly here and push to
      // the stores via .set() rather than routing through store.load() —
      // the store's fetcher loses the per-feed error/summary context.
      let pErr = '', hErr = '';
      const [p, h] = await Promise.all([
        fetchPositions().catch((e) => { pErr = e?.message || 'positions unavailable'; return { rows: [], summary: [] }; }),
        fetchHoldings().catch((e)  => { hErr = e?.message || 'holdings unavailable';  return { rows: [], summary: [] }; }),
      ]);
      brokerErr = [pErr, hErr].filter(Boolean).join(' · ');
      const p_rows = (p?.rows || []).slice();
      const h_rows = (h?.rows || []).slice();
      // Push to stores (writes Tier 1 + Tier 2). Only write on success
      // so partial broker errors don't blank whichever side worked.
      if (!pErr && p_rows.length) {
        positionsStore.set(p_rows);
        // .set() bypasses the parse hook; publish to symbolStore
        // explicitly so the per-row LTPs land in the central store
        // (audit found this was the primary 10s-poll gap — pinned/
        // watchlist LTPs went stale because the parse-hook publish
        // never fired on the .set() path).
        publishPositionsRows(p_rows);
      }
      if (!hErr && h_rows.length) {
        holdingsStore.set(h_rows);
        publishHoldingsRows(h_rows);
      }
      // Capture the backend's precomputed per-account summary rows
      // (used by the dashboard's summary grid). Excludes the TOTAL
      // synthetic row — we render that separately as pinnedBottomRowData.
      if (showSummary) {
        positionsSummary = (p?.summary || []).slice();
        holdingsSummary  = (h?.summary || []).slice();
      }
      // Surface every account id we see across positions + holdings
      // for the account-picker dropdown. Also UNION with the broker
      // registry (loaded accounts that may have 0 positions / 0
      // holdings — operator still wants to see them in the filter so
      // they can confirm the row was added correctly). Deduplicated,
      // sorted. _knownBrokerAccounts is populated by the parallel
      // fetchBrokerAccounts() call in onMount + the visible-interval
      // poll below.
      if (accountPicker) {
        const accts = new Set();
        for (const r of p_rows) if (r.account) accts.add(String(r.account));
        for (const r of h_rows) if (r.account) accts.add(String(r.account));
        for (const a of _knownBrokerAccounts) accts.add(String(a));
        const sorted = [...accts].sort();
        availableAccounts = sorted;
        // Two-stage seeding:
        //   (a) FIRST-LOAD seed (latched by `_seededFromBrokers`) — when
        //       no persisted state exists in sessionStorage, populate
        //       each picker with EVERY known account so positions /
        //       holdings render immediately on a fresh session.
        //   (b) LATE-ARRIVAL union (runs every poll) — accounts that
        //       were NOT in `_seenAccounts` before but appear in `sorted`
        //       now are unioned into BOTH pickers UNCONDITIONALLY. Fixes
        //       the Dhan-not-visible bug: when an admin rebuilds
        //       Connections to add Dhan after the operator's Pulse tab
        //       is already running, the Dhan account code arrives in
        //       `_knownBrokerAccounts` and `positions` rows but the
        //       persisted positionsAccounts set doesn't include it — the
        //       latch (a) would never re-seed. Stage (b) closes that gap
        //       while still preserving operator manual exclusions on
        //       previously-known accounts.
        if (sorted.length > 0) {
          // Stage (b): late-arrival accounts ALWAYS union in.
          const newAccts = sorted.filter(a => !_seenAccounts.has(a));
          if (newAccts.length > 0) {
            // Skip stage (b) only on the very first sighting (when
            // `_seenAccounts` is empty AND no persisted state exists) —
            // stage (a) below handles that case with the same union.
            const hasPersistedP = (() => {
              try {
                const cP = sessionStorage.getItem('mp.positionsAccounts');
                if (cP) {
                  const parsed = JSON.parse(cP);
                  return Array.isArray(parsed) && parsed.length > 0;
                }
              } catch (_) {}
              return false;
            })();
            const hasPersistedH = (() => {
              try {
                const cH = sessionStorage.getItem('mp.holdingsAccounts');
                if (cH) {
                  const parsed = JSON.parse(cH);
                  return Array.isArray(parsed) && parsed.length > 0;
                }
              } catch (_) {}
              return false;
            })();
            const seenAny = _seenAccounts.size > 0;
            // Run the union when EITHER (i) we've seen accounts before
            // (so this is a genuine late-arrival), OR (ii) there's
            // persisted state (so the operator's session is mid-stream
            // and a new account is joining).
            if (seenAny || hasPersistedP || hasPersistedH) {
              if (hasPersistedP || positionsAccounts.length > 0) {
                const cur = new Set(positionsAccounts);
                for (const a of newAccts) cur.add(a);
                positionsAccounts = [...cur].sort();
              }
              if (hasPersistedH || holdingsAccounts.length > 0) {
                const cur = new Set(holdingsAccounts);
                for (const a of newAccts) cur.add(a);
                holdingsAccounts = [...cur].sort();
              }
            }
            // Mark every account in `sorted` (incl. the new arrivals)
            // as seen, and persist the ledger so it survives a tab
            // refresh. Done BEFORE stage (a) so stage (a)'s ADD-all
            // path doesn't trip on the same accounts as "new" later.
            for (const a of sorted) _seenAccounts.add(a);
            try {
              sessionStorage.setItem(
                'mp.seenAccounts', JSON.stringify([..._seenAccounts]));
            } catch (_) {}
          }
          // Stage (a): first-load latch. Operator: "all accounts should
          // be default for positions and holdings in pulse." The picker
          // intentionally STARTS EMPTY (= "All accounts" filter), so
          // first-load just marks the ledger and latches without
          // pre-filling. Stage (b) above handles late-arriving brokers
          // when the operator has explicitly narrowed (non-empty
          // selection); empty-selection sessions stay wide.
          if (!_seededFromBrokers) {
            for (const a of sorted) _seenAccounts.add(a);
            try {
              sessionStorage.setItem(
                'mp.seenAccounts', JSON.stringify([..._seenAccounts]));
            } catch (_) {}
            if (_knownBrokerAccounts.length > 0) _seededFromBrokers = true;
          }
        }
      }
      const underlyingInfos = /** @type {Map<string, any>} */ (new Map());
      const contractKeys = new Set();
      const lookup = getInstrument;
      const nearestFut = findNearestFuture;
      const listFuts = listFutures;
      // Keyed by underlying_group so MCX options with different
      // contract months (CRUDEOIL26MAY vs CRUDEOIL26JUN) each map to
      // their own same-month future. Indices/stocks share one key.
      //
      // Derivative-grouping rule: anchors are injected ONLY for
      // options (CE/PE). Futures stand alone — each future is its own
      // row, no underlying-anchor pulled in alongside. This stops a
      // standalone NIFTY25APRFUT position from also surfacing a NIFTY
      // anchor row (the future IS the tradable instrument; grouping
      // it under spot adds no signal).
      //
      // The anchor row also carries the major group of its trigger
      // (positions vs holdings) so buildUnified can land the anchor
      // in the same major as its options. First wins — if NIFTY
      // options exist in BOTH positions and holdings, the anchor
      // gets the positions tag (priority: positions > holdings).
      // Parse a Kite derivative tradingsymbol into underlying / expiry /
      // kind without consulting the instruments cache. Handles both
      // monthly-options (NIFTY25APR22000CE), weekly-options
      // (NIFTY2540422000CE), monthly-futures (NIFTY25APRFUT), and the
      // commodity variants (CRUDEOIL26JUNFUT, GOLDM26MAY152000PE).
      // Returns null for equity tradingsymbols. Used as a fallback path
      // for the anchor-creation loop when the instruments cache hasn't
      // loaded this contract yet (race on cold start) — instead of
      // bailing silently, we synthesize the minimum we need.
      const _MONTH = { JAN:'01',FEB:'02',MAR:'03',APR:'04',MAY:'05',JUN:'06',
                       JUL:'07',AUG:'08',SEP:'09',OCT:'10',NOV:'11',DEC:'12' };
      const _parseDerivSym = (sym) => {
        const s = String(sym || '').toUpperCase();
        // Monthly opt:  PREFIX + YYMMM + STRIKE + CE|PE
        let m = s.match(/^([A-Z]+)(\d{2})([A-Z]{3})\d+(CE|PE)$/);
        if (m) return { underlying: m[1], expiry: `20${m[2]}-${_MONTH[m[3]] || '01'}-01`, kind: m[4] };
        // Weekly opt: PREFIX + YY + MM + DD + STRIKE + CE|PE
        m = s.match(/^([A-Z]+)(\d{2})(\d{1,2})(\d{1,2})\d+(CE|PE)$/);
        if (m) return { underlying: m[1], expiry: `20${m[2]}-${String(m[3]).padStart(2,'0')}-${String(m[4]).padStart(2,'0')}`, kind: m[5] };
        // Monthly fut:  PREFIX + YYMMM + FUT
        m = s.match(/^([A-Z]+)(\d{2})([A-Z]{3})FUT$/);
        if (m) return { underlying: m[1], expiry: `20${m[2]}-${_MONTH[m[3]] || '01'}-01`, kind: 'FUT' };
        return null;
      };

      const addUnderlying = (sym, triggerMajor, optInst) => {
        // Bypass the instruments cache — parse the tradingsymbol
        // directly. Without this, contracts missing from the cache
        // (cold-start race, new strikes, weekly options Kite
        // publishes late) silently lost their parent anchor row.
        const parsed = _parseDerivSym(sym);
        if (!parsed) return;
        const { underlying: u, expiry, kind } = parsed;
        const isOpt = (kind === 'CE' || kind === 'PE');
        const isFut = (kind === 'FUT');
        if (!isOpt && !isFut) return;
        const info = isOpt
          ? resolveUnderlyingForOption(u, expiry, nearestFut, listFuts)
          : resolveUnderlying(u, nearestFut);
        if (info && !underlyingInfos.has(info.underlying_group)) {
          underlyingInfos.set(info.underlying_group, { ...info, _major: triggerMajor });
        }
      };
      for (const r of p_rows) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NFO';
        if (sym) {
          addUnderlying(sym, 'positions');
          contractKeys.add(`${exch}:${sym}`);
        }
      }
      for (const r of h_rows) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NSE';
        if (sym) {
          addUnderlying(sym, 'holdings');
          contractKeys.add(`${exch}:${sym}`);
        }
      }
      // Watchlist option-anchor pass — when the operator has an
      // option (CE/PE) in any active watchlist, synthesise an
      // anchor row for that option's underlying so the option sits
      // under a parent anchor instead of orphaning in the grid.
      // First-trigger-wins via the underlyingInfos map; positions
      // anchors (registered above) already in the map are NOT
      // overwritten — a watchlist option for an underlying that
      // ALSO appears in positions will still surface the anchor in
      // the Positions major (the original semantics). The
      // watchlist anchor only lands when no higher-priority source
      // claimed the underlying first.
      for (const list of (activeLists || [])) {
        const major = list?.is_pinned ? 'pinned' : 'watchlist';
        for (const it of (list?.items || [])) {
          const sym = String(it.tradingsymbol || '').toUpperCase();
          if (sym) addUnderlying(sym, major);
        }
      }

      const allKeys = new Set(contractKeys);
      for (const info of underlyingInfos.values()) allKeys.add(info.quoteKey);

      if (allKeys.size) {
        const items = await batchQuoteChunked([...allKeys]);
        // BH5: publish the broker-quote snapshot for every symbol in
        // view into symbolStore. This is the last non-symbolStore data
        // sink — after this, every market-data poll on this page feeds
        // the same per-symbol cache.
        publishPulseQuotes(items);
        const cMap = {};
        for (const q of items) {
          cMap[`${q.exchange}:${q.tradingsymbol}`] = q;
        }
        const uMap = {};
        for (const [name, info] of underlyingInfos.entries()) {
          const q = cMap[info.quoteKey];
          // Always create the anchor — even when the broker quote
          // endpoint didn't return a row for info.quoteKey. Without
          // this, INDIGO (whose NSE quote can silently fail) and
          // GOLDM (whose MCX future may not be in the instruments
          // cache) lost their anchor rows entirely, leaving the
          // option positions orphaned in the grid. Quote-less
          // anchors render with an em-dash LTP — still better than
          // a missing parent row.
          uMap[name] = q ? { ...q, _resolved: info } : { _resolved: info };
        }
        pulseQuotes = { underlyings: uMap, contracts: cMap };
      } else {
        pulseQuotes = { underlyings: {}, contracts: {} };
      }
      pulseLastUpdate = Date.now();
      _lastPulseAt = pulseLastUpdate;
      // Surface the auto-poll completion to every mounted RefreshButton's
      // tooltip — see dashboard.loadHero for the rationale.
      lastRefreshAt.set(pulseLastUpdate);
    } catch (_) { /* nothing fatal */ }
  }

  // Per-card account-scoped inputs to buildUnified. Empty per-card
  // selection (default) passes the raw array straight through.
  // Otherwise filter the corresponding source to that card's chosen
  // set — watchlist items and option underlyings are not account-
  // scoped so they remain visible regardless.
  // Slice 7g — strategy filter narrows the positions + holdings
  // input to symbols with open lots in the picked strategy.
  // `selectedStrategyId == null` → no filter (every row contributes).
  // Filter happens AFTER account scoping so the operator's account
  // pick + strategy pick compose (AND). Holdings filter is the same
  // — even though equity holdings aren't strictly per-strategy, the
  // operator's intent when picking a strategy is "show me ONLY this
  // strategy's universe" — including any equity legs that strategy
  // holds.
  const _matchStrategySym = (/** @type {string} */ sym) => {
    if ($selectedStrategyId == null) return true;
    if ($strategyOpenSymbols.size === 0) return false;
    return $strategyOpenSymbols.has(String(sym || '').toUpperCase());
  };

  const scopedPositions = $derived(
    (positionsAccounts.length === 0
      ? positions
      : positions.filter(r => _includesPosAcct(r.account))
    ).filter(r => _matchStrategySym(r.tradingsymbol || r.symbol || ''))
  );
  const scopedHoldings = $derived(
    (holdingsAccounts.length === 0
      ? holdings
      : holdings.filter(r => _includesHoldAcct(r.account))
    ).filter(r => _matchStrategySym(r.tradingsymbol || r.symbol || ''))
  );

  // buildUnified reads groupOrder + detachedSymbols from module scope.
  // The previous comma-operator dep-tracking trick
  //   $derived(((groupOrder, detachedSymbols), buildUnified(...)))
  // was fragile — Svelte 5's compiler doesn't always pick up state
  // reads inside a function call evaluated as a comma-expression
  // sibling. Switched to $derived.by with explicit touches so the
  // compiler unambiguously sees both reads inside the derivation body.
  // Module-scoped (per-instance) set of symbols whose chart bars
  // we've already kicked off a prefetch for. Persists across
  // unifiedRows recomputes so we don't re-fire on every poll —
  // only NEW symbols get a prefetch. The 60s backend cache + the
  // ChartWorkspace module cache absorb the result.
  /** @type {Set<string>} */
  const _prefetchedChartSyms = new Set();
  /** @type {number[]} */
  const _prefetchTimers = [];
  function _stagedPrefetch(sym, exch) {
    if (!sym || _prefetchedChartSyms.has(sym)) return;
    _prefetchedChartSyms.add(sym);
    // Stagger: 80ms per symbol so 30 fresh symbols spread across
    // ~2.4s instead of hammering the backend simultaneously.
    const t = setTimeout(() => {
      import('$lib/ChartWorkspace.svelte')
        .then(m => m.prefetchChartBars(sym, exch || ''))
        .catch(() => {});
    }, _prefetchTimers.length * 80);
    _prefetchTimers.push(t);
  }

  const unifiedRows = $derived.by(() => {
    // eslint-disable-next-line no-unused-expressions
    groupOrder; detachedSymbols;
    // Positions + Holdings are the only account-scoped sources. Empty
    // picker = "All accounts" (default) — show every row. Non-empty
    // picker = scope to the picked accounts (filter lives in
    // scopedPositions / scopedHoldings via _includesPosAcct /
    // _includesHoldAcct). Operator: "for default all accounts, position
    // and holding all rows are not fetched. they grids are empty. fix
    // it." The previous gate `accountPicker && positionsAccounts.length
    // === 0 → HIDE` was a relic of the now-removed stage (a) seeder
    // that pre-filled the picker; with the new "All accounts" default
    // it dropped every row when no narrowing was active.
    return buildUnified(
      activeLists, scopedPositions, scopedHoldings, pulseQuotes, getInstrument,
      showPositions, showHoldings,
      movers, showMovers,
      showWatchlist,
    );
  });

  // Non-blocking chart-bar prefetch for every symbol visible in
  // /pulse. Operator: "you can prefetch all the pulse symbols.
  // whenever, a symbol is added to pulse this should happen. the
  // prefetch should non-blocking." This $effect tracks unifiedRows;
  // any NEW symbol since the last run gets queued through
  // _stagedPrefetch (setTimeout + dynamic import). The render loop
  // never blocks on a prefetch.
  $effect(() => {
    if (!unifiedRows?.length) return;
    for (const row of unifiedRows) {
      const sym = String(row?.symbol || row?.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      _stagedPrefetch(sym, row?.exchange || '');
    }
  });

  function parseSymbol(/** @type {string} */ sym, /** @type {any} */ instCache) {
    const inst = instCache ? instCache(sym) : null;
    // Cache lookup is preferred but commodity options + brand-new
    // contracts may not be indexed yet — fall back to a regex parser
    // so the row still gets the right underlying/strike/opt_type for
    // grouping + sorting.
    if (inst) {
      const t = String(inst.t || '').toUpperCase();
      const k = inst.k != null ? Number(inst.k) : null;
      let optType = null;
      if (t === 'CE' || t === 'PE') optType = t;
      else if (/CE$/i.test(sym)) optType = 'CE';
      else if (/PE$/i.test(sym)) optType = 'PE';
      const kind = optType ? 'opt' : (t === 'FUT' ? 'fut' : (t === 'EQ' ? 'eq' : null));
      return { underlying: inst.u || null, kind, strike: k, opt_type: optType, expiry: inst.x || null };
    }
    return parseSymbolFallback(sym);
  }

  /**
   * Regex-only parser for F&O tradingsymbols when the instruments
   * cache misses (commodity options, new contracts, dev fixtures).
   * Pulls underlying / strike / opt_type so group + sort logic still
   * lands the row in its correct bucket.
   *
   * Matches:
   *   CRUDEOIL26JUN10000CE → CRUDEOIL / 10000 / CE / opt
   *   NIFTY25APR21500PE    → NIFTY / 21500 / PE / opt
   *   NIFTY25APRFUT        → NIFTY / null  / null / fut
   *   GOLDM26JUN78000CE    → GOLDM / 78000 / CE / opt
   */
  function parseSymbolFallback(/** @type {string} */ sym) {
    const empty = { underlying: null, kind: null, strike: null, opt_type: null, expiry: null };
    if (!sym) return empty;
    const m = /^([A-Z][A-Z&]+?)(\d{2}[A-Z]{3})(\d+)?(CE|PE|FUT)$/.exec(sym);
    if (!m) return empty;
    const [, underlying, expiry, strikeStr, suffix] = m;
    const strike = strikeStr ? Number(strikeStr) : null;
    const opt_type = (suffix === 'CE' || suffix === 'PE') ? suffix : null;
    const kind = opt_type ? 'opt' : (suffix === 'FUT' ? 'fut' : null);
    return { underlying, kind, strike, opt_type, expiry };
  }

  // Major-group ordering. Pinned at top, then user Watchlist, then
  // Positions (the operator's open derivative book), then Holdings
  // (CNC equity), then Movers (session signal). Used as the row sort
  // primary key so the unified grid renders these as five visually
  // contiguous blocks.
  const MAJOR_ORDER = { pinned: 0, watchlist: 1, positions: 2, holdings: 3, movers: 4 };

  function buildUnified(actLists, pos, hold, pq, getInst, includePos, includeHold, moverRows, includeMovers, includeWatch = true) {
    const uq = pq?.underlyings || {};
    const cq = pq?.contracts   || {};
    const byKey = /** @type {Record<string, any>} */ ({});
    // Per-major keying: a symbol that qualifies for multiple majors
    // renders as multiple rows (one per major). Within a major,
    // symbols dedupe (a position in 3 accounts merges to one row).
    //   __pin   pinned watchlist (Default + Markets + any operator-pinned)
    //   __wl    user-created watchlist (is_pinned=false)
    //   __pos   positions
    //   __hold  holdings
    //   __mov   movers (only created when symbol has no other major)
    const MAJOR_SUFFIX = { pinned: '__pin', watchlist: '__wl', positions: '__pos', holdings: '__hold', movers: '__mov' };
    const get = (sym, major) => {
      const key = `${sym}${MAJOR_SUFFIX[major]}`;
      if (byKey[key]) return byKey[key];
      return byKey[key] = {
        key,
        _majorGroup: major,
        _majorOrder: MAJOR_ORDER[major],
        src: { w:false, h:false, p:false, u:false, m:false },
        qty_pos: 0, qty_hold: 0,
        pnl: null, day_pnl: null,
        _avg_num: 0,         // Σ(positions avg × qty)
        _avg_hold_num: 0,    // Σ(holdings  avg × qty)
        accounts: /** @type {Set<string>} */ (new Set()),
      };
    };

    function fill(row, sym) {
      const p = parseSymbol(sym, getInst);
      if (row.underlying == null) row.underlying = p.underlying;
      if (row.kind       == null) row.kind       = p.kind;
      if (row.strike     == null) row.strike     = p.strike;
      if (row.opt_type   == null) row.opt_type   = p.opt_type;
      if (row.expiry     == null) row.expiry     = p.expiry;
    }

    // 1. Watchlists. Each list carries an is_pinned flag (true for
    //    auto-seeded Default + Markets, false for operator-created
    //    lists). Pinned lists feed the Pinned major; non-pinned lists
    //    feed the Watchlist major. A symbol that appears in BOTH a
    //    pinned list AND a user list creates TWO rows — once per
    //    major. Operator-visible overlap by design.
    for (const list of (actLists || [])) {
      const major = list?.is_pinned ? 'pinned' : 'watchlist';
      for (const it of (list?.items || [])) {
        // BH4: alias resolution dropped. Post-_expand_root_items_to_futures
        // the backend already ships actual broker tradingsymbols (GOLD →
        // GOLD25APRFUT, NIFTY → NIFTY 50). Market data is keyed by
        // tradingsymbol everywhere downstream — no second-level alias
        // lookup needed. The legacy wq[it.id].quote_symbol path is gone
        // along with watchQuotesStore.
        const sym = String(it.tradingsymbol || '').toUpperCase();
        if (!sym) continue;
        const row = get(sym, major);
        row.exchange      = row.exchange || it.exchange;
        row.tradingsymbol = sym;
        // Operator-supplied display name on the WatchlistItem (e.g.
        // "Crude oil" labelling CRUDEOIL26JUNFUT). The symbol cell
        // shows this when present; tradingsymbol moves to the tooltip.
        if (it.alias) row.display_name = String(it.alias);
        if (row.watchlist_item_id == null) {
          row.watchlist_item_id = it.id;
          row.watchlist_list_id = list.id;
        }
        row.src.w = true;
        // Back-compat tag for the legacy isPinnedIndexRow check.
        if (major === 'pinned') row._fromPinnedList = true;
        // All market fields source from symbolStore — populated by
        // every fetcher (positions/holdings/movers/inline-watchQuotes-
        // fetch) + SSE ticks. TTL.week localStorage carries closing
        // values across off-hours; live ticks overwrite on session
        // open. Pinned + watchlist + positions + holdings + movers
        // grids now share the same per-symbol freshness — no more
        // "pinned shows zeros while positions show values" asymmetry.
        const snap = untrack(() => getSnapshot(sym));
        row.ltp        = snap?.ltp            ?? row.ltp        ?? null;
        row.bid        = snap?.bid            ?? row.bid        ?? null;
        row.ask        = snap?.ask            ?? row.ask        ?? null;
        row.close      = snap?.close          ?? row.close      ?? null;
        row.open       = snap?.open           ?? row.open       ?? null;
        row.change     = snap?.day_change     ?? row.change     ?? null;
        row.change_pct = snap?.day_change_pct ?? row.change_pct ?? null;
        row.volume     = snap?.volume         ?? row.volume     ?? null;
        row.oi         = snap?.oi             ?? row.oi         ?? null;
        fill(row, sym);
      }
    }

    // 2. Positions — major 'positions'. Multi-account positions for
    //    the same symbol merge into a single row (qty / avg / pnl
    //    accumulated across accounts).
    for (const r of (includePos === false ? [] : pos)) {
      const exch = r.exchange || 'NFO';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym, 'positions');
      row.exchange      = row.exchange || exch;
      row.tradingsymbol = sym;
      row.src.p = true;
      const q   = Number(r.quantity) || 0;
      const avg = Number(r.average_price) || 0;
      row.qty_pos  += q;
      row._avg_num += avg * q;
      if (r.account) row.accounts.add(String(r.account));
      // BH6: positions branch now reads market fields from symbolStore
      // so SSE ticks land here at the same cadence pinned/mover already
      // see them. cq (pulseQuotes) and r.last_price stay as ordered
      // fallbacks for the cold-mount path before any tick has fired.
      const snap  = untrack(() => getSnapshot(sym));
      const liveQ = cq?.[`${exch}:${sym}`];
      const snapLtp = snap?.ltp;
      if (snapLtp != null) {
        row.ltp        = snapLtp;
        row.bid        = snap.bid    ?? liveQ?.bid    ?? row.bid    ?? null;
        row.ask        = snap.ask    ?? liveQ?.ask    ?? row.ask    ?? null;
        row.open       = snap.open   ?? liveQ?.open   ?? row.open   ?? null;
        row.close      = snap.close  ?? liveQ?.close  ?? row.close  ?? null;
        row.change     = snap.day_change     ?? liveQ?.change     ?? row.change     ?? null;
        row.change_pct = snap.day_change_pct ?? liveQ?.change_pct ?? row.change_pct ?? null;
        row.volume     = snap.volume ?? liveQ?.volume ?? row.volume ?? null;
        row.oi         = snap.oi     ?? liveQ?.oi     ?? row.oi     ?? null;
      } else if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.open       = liveQ.open  ?? row.open  ?? null;
        row.close      = liveQ.close ?? row.close ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
        row.volume     = liveQ.volume ?? row.volume ?? null;
        row.oi         = liveQ.oi     ?? row.oi     ?? null;
      } else if (row.ltp == null) {
        row.ltp = r.last_price ?? null;
      }
      // Day P&L recompute with realised-today carried through:
      //
      //   broker_dcv      = realised_today + (poll_ltp − close) × qty
      //   realised_today  = broker_dcv − (poll_ltp − close) × qty
      //   live row.day_pnl = realised_today + (live_ltp − close) × qty
      //
      // Without the realised_today term, a partially-closed leg
      // (overnight 10, sold 4 today, current_qty 6) would show only
      // the unrealised portion (~6 × move) on the day, missing the
      // realised cash flow from the 4 sold today. End-to-end: the row
      // diverged from broker's Day P&L by the realised amount.
      //
      // Treat ltp=0 as "no live quote" — a positive LTP is the only
      // value that should drive the live recompute. Otherwise a broken
      // quote (pre-open, no trades yet, circuit, broker glitch) would
      // post day_pnl = (0 − close) × qty → a phantom −100% day move.
      const livePos = Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null;
      const closePx = Number(r.close_price) || 0;
      const pollLtp = Number(r.last_price) || 0;
      const brokerDcv = Number(r.day_change_val) || 0;
      // Solve for realised_today from the broker snapshot. Falls
      // through to 0 when LTP or close is missing (no way to solve).
      const realisedToday = (pollLtp > 0 && closePx > 0 && q !== 0)
        ? brokerDcv - (pollLtp - closePx) * q
        : 0;
      if (livePos != null && closePx > 0 && q !== 0) {
        row.day_pnl = (row.day_pnl ?? 0) + realisedToday + (livePos - closePx) * q;
      } else {
        row.day_pnl = (row.day_pnl ?? 0) + brokerDcv;
      }
      // Total P&L = (live_ltp − avg_price) × qty + realised. realised
      // only changes when a trade fills, so the live recompute stays
      // accurate between loadPulse cycles.
      if (livePos != null && avg > 0 && q !== 0) {
        row.pnl = (row.pnl ?? 0) + (livePos - avg) * q + (Number(r.realised) || 0);
      } else {
        row.pnl = (row.pnl ?? 0) + (Number(r.pnl) || 0);
      }
      // Mirror the BROKER raw values per-row so the TOTAL footer can
      // sum the same numbers PositionStrip uses (the strip reads
      // `r.pnl` and `r.day_change_val` straight off /api/holdings +
      // /api/positions). Per-row `row.pnl` / `row.day_pnl` keep the
      // live-recompute path above so the cells still tick.
      row._broker_pnl     = (row._broker_pnl     ?? 0) + (Number(r.pnl)             || 0);
      row._broker_day_pnl = (row._broker_day_pnl ?? 0) + (Number(r.day_change_val) || 0);
      fill(row, sym);
    }

    // 3. Holdings — major 'holdings'.
    for (const r of (includeHold === false ? [] : hold)) {
      const exch = r.exchange || 'NSE';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym, 'holdings');
      row.exchange      = row.exchange || exch;
      row.tradingsymbol = sym;
      row.src.h = true;
      const heldQty = Number(r.opening_quantity) || Number(r.quantity) || 0;
      row.qty_hold += heldQty;
      // Carry holdings avg cost so the unified-grid Avg column can show
      // a weighted avg across positions + holdings (some symbols carry
      // both — e.g. an investor holding 100 INFY who also opens an
      // intraday position).
      row._avg_hold_num += (Number(r.average_price) || 0) * heldQty;
      if (r.account) row.accounts.add(String(r.account));
      // BH6: holdings branch reads from symbolStore first; cq + row
      // fallback chain stays for cold-mount.
      const snap  = untrack(() => getSnapshot(sym));
      const liveQ = cq?.[`${exch}:${sym}`];
      const snapLtp = snap?.ltp;
      if (snapLtp != null) {
        row.ltp        = snapLtp;
        row.bid        = snap.bid    ?? liveQ?.bid    ?? row.bid    ?? null;
        row.ask        = snap.ask    ?? liveQ?.ask    ?? row.ask    ?? null;
        row.open       = snap.open   ?? liveQ?.open   ?? row.open   ?? null;
        row.close      = snap.close  ?? liveQ?.close  ?? row.close  ?? Number(r.close_price) ?? null;
        row.change     = snap.day_change     ?? liveQ?.change     ?? row.change     ?? null;
        row.change_pct = snap.day_change_pct ?? liveQ?.change_pct ?? row.change_pct ?? null;
        if (snap.volume != null) row.volume = snap.volume;
        if (snap.oi     != null) row.oi     = snap.oi;
      } else if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.open       = liveQ.open  ?? row.open  ?? null;
        row.close      = liveQ.close ?? row.close ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else {
        if (row.ltp == null) row.ltp = r.last_price ?? null;
        if (r.day_change != null && row.change == null)
          row.change = Number(r.day_change);
        if (r.day_change_percentage != null && row.change_pct == null)
          row.change_pct = Number(r.day_change_percentage);
        // Holdings rows carry close_price (yesterday); use it as the
        // fallback close when no live quote landed. open price isn't
        // in the holdings payload — leaves null.
        if (row.close == null && r.close_price != null)
          row.close = Number(r.close_price);
      }
      // Holdings: same recompute as positions — Day P&L from
      // (live_ltp − close_price) × held_qty when LTP is live. avg cost
      // for holdings lives on `r.average_price`; pnl recompute uses
      // (live_ltp − avg) × qty (no realised component on holdings).
      if (liveQ?.volume != null) row.volume = liveQ.volume;
      if (liveQ?.oi     != null) row.oi     = liveQ.oi;
      // Prefer the snapshot LTP for the day-pnl recompute (cell renderers
      // also read it via _liveLtpSnap; keep the math consistent).
      const liveHold = (snapLtp != null && Number(snapLtp) > 0) ? Number(snapLtp)
                     : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
      const holdClose = Number(r.close_price) || 0;
      const holdAvg   = Number(r.average_price) || 0;
      if (liveHold != null && holdClose > 0 && heldQty !== 0) {
        row.day_pnl = (row.day_pnl ?? 0) + (liveHold - holdClose) * heldQty;
      } else {
        row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
      }
      if (liveHold != null && holdAvg > 0 && heldQty !== 0) {
        row.pnl = (row.pnl ?? 0) + (liveHold - holdAvg) * heldQty;
      } else {
        row.pnl = (row.pnl ?? 0) + (Number(r.pnl) || 0);
      }
      // Mirror the BROKER raw values per-row so the TOTAL footer can
      // sum the same numbers PositionStrip uses. Per-row live recompute
      // path above stays untouched.
      row._broker_pnl     = (row._broker_pnl     ?? 0) + (Number(r.pnl)             || 0);
      row._broker_day_pnl = (row._broker_day_pnl ?? 0) + (Number(r.day_change_val) || 0);
      fill(row, sym);
    }

    // 4. Option-underlying anchors. Each anchor carries the major of
    //    its trigger context (positions / holdings) so the anchor
    //    lands in the same major as the options it's grouping.
    //
    //    Source-toggle gate: the anchor is a derived sibling-summary
    //    for option positions/holdings. If those are filtered off,
    //    the anchor row dangles (INDIGO / GOLDM / CRUDEOIL futures
    //    appearing with no child options to anchor). Skip anchor
    //    creation when its trigger major's toggle is off. Watchlist
    //    + pinned anchors are gated downstream by mainRows /
    //    pinnedTopRows via the _majorGroup filter; the includePos /
    //    includeHold flags only reach buildUnified.
    //
    //    Account-scope gate: the underlying anchor SEEDS come from the
    //    full unfiltered positions+holdings universe (so anchors
    //    survive cache rolls), but the anchor row should only appear
    //    in a major when the scoped per-card input array (pos / hold,
    //    already account-filtered) carries an OPTION (CE/PE) on that
    //    underlying. Operator: "when i select dh3747 in positions, why
    //    is it showing bel and underlyings when there are no options in
    //    the account. if an option is present in the account, the
    //    underlying can be present. fix it." Same rule applies symmetrically
    //    to holdings — though holdings rarely carry options, the gate is
    //    written for both to stay symmetric.
    /** @type {Set<string>} */
    const posOptUnderlyings = new Set();
    for (const r of (includePos === false ? [] : pos)) {
      const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!/(CE|PE)$/i.test(sym)) continue;
      const p = parseSymbol(sym, getInst);
      if (p?.underlying) posOptUnderlyings.add(String(p.underlying).toUpperCase());
    }
    /** @type {Set<string>} */
    const holdOptUnderlyings = new Set();
    for (const r of (includeHold === false ? [] : hold)) {
      const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!/(CE|PE)$/i.test(sym)) continue;
      const p = parseSymbol(sym, getInst);
      if (p?.underlying) holdOptUnderlyings.add(String(p.underlying).toUpperCase());
    }
    for (const [logicalName, q] of Object.entries(uq)) {
      const info = q._resolved;
      if (!info) continue;
      const anchorMajor = info._major || 'positions';
      if (anchorMajor === 'positions' && includePos  === false) continue;
      if (anchorMajor === 'holdings'  && includeHold === false) continue;
      const _uKey = String(info.displayUnderlying || info.underlying_group || '').toUpperCase();
      if (anchorMajor === 'positions' && _uKey && !posOptUnderlyings.has(_uKey)) continue;
      if (anchorMajor === 'holdings'  && _uKey && !holdOptUnderlyings.has(_uKey)) continue;
      const row = get(info.tradingsymbol, anchorMajor);
      row.exchange      = row.exchange || info.exchange;
      row.tradingsymbol = info.tradingsymbol;
      row.src.u = true;
      // Use displayUnderlying for the row's grouping value when the
      // resolver supplied it (MCX same-month-future case where
      // underlying_group is suffixed with the year-month for dedup).
      // Falls back to underlying_group for non-MCX paths where the
      // two are identical. Without this, MCX option-underlying
      // anchors end up in their own 'CRUDEOIL_2026-06'-style group
      // and don't sit alongside their CRUDEOIL options.
      row.underlying    = info.displayUnderlying || info.underlying_group;
      row.kind          = info.kind;
      row.ltp        = q.ltp        ?? row.ltp        ?? null;
      row.bid        = q.bid        ?? row.bid        ?? null;
      row.ask        = q.ask        ?? row.ask        ?? null;
      // Prev close + change/change_pct on the underlying anchor. The
      // earlier shape only set ltp/bid/ask so the Prev Close + Day %
      // columns rendered as "—" on every BHEL / BEL / NIFTY anchor
      // row even though the broker quote carries the values. Operator:
      // "why bhel, bel, prev close day %, day P&L in positions grid
      // are not updated as underlyings in pulse"
      if (row.close      == null) row.close      = q.close      ?? null;
      if (row.change     == null) row.change     = q.change     ?? null;
      if (row.change_pct == null) row.change_pct = q.change_pct ?? null;
    }

    // 5. Movers — single-place rule. A symbol qualifies for the
    //    Movers major ONLY if it has no existing row in any other
    //    major. Symbols that already appear in Pinned / Watchlist /
    //    Positions / Holdings get the mover badge stamped onto every
    //    such row, but do NOT create an additional Movers row.
    //
    //    This fixes the previous "option underlyings showing in
    //    movers" complaint — NIFTY moving 6% intraday is captured by
    //    badging the existing positions / pinned NIFTY rows; it
    //    doesn't surface as a fresh Movers entry.
    // Build existingSymbols from ONLY currently-visible majors so the
    // mover badging logic doesn't suppress rows whose source toggle is
    // off. Without this gate, a stock in a toggled-off watchlist would
    // be badged into a watchlist row → the filter at the end deletes
    // the watchlist row → the symbol disappears completely. Pinned is
    // always visible so it remains a badge-eligible source.
    const existingSymbols = new Set();
    for (const row of Object.values(byKey)) {
      if (!row.tradingsymbol) continue;
      const mg = row._majorGroup;
      if (mg === 'positions' && !includePos)  continue;
      if (mg === 'holdings'  && !includeHold) continue;
      if (mg === 'watchlist' && !includeWatch) continue;
      existingSymbols.add(row.tradingsymbol);
    }
    for (const m of (moverRows || [])) {
      const sym = String(m.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      // BH5: market fields (ltp / change_pct / close) sourced from
      // symbolStore where available — keeps the mover panel live with
      // SSE ticks rather than frozen at the last moversStore poll
      // (every 30s). Sticky / direction / group tags still come from
      // the moversStore row (they're index-membership metadata, not
      // market data).
      const snap = untrack(() => getSnapshot(sym));
      const liveLtp        = snap?.ltp            ?? m.last_price ?? null;
      const liveChangePct  = snap?.day_change_pct ?? m.change_pct ?? null;
      const liveClose      = snap?.close          ?? m.previous_close ?? null;
      if (existingSymbols.has(sym)) {
        // Badge every existing row for this symbol (across majors).
        for (const row of Object.values(byKey)) {
          if (row.tradingsymbol === sym) {
            row.src.m = true;
            row._mover_sticky    = m.sticky ?? row._mover_sticky    ?? false;
            row._mover_change_pct = liveChangePct ?? row._mover_change_pct ?? null;
          }
        }
        continue;
      }
      // Pure mover — no other major qualification. Create a Movers row.
      const row = get(sym, 'movers');
      row.src.m = true;
      row.exchange      = row.exchange || m.exchange || 'NSE';
      row.tradingsymbol = sym;
      if (row.ltp == null && liveLtp != null)             row.ltp        = liveLtp;
      if (row.change_pct == null && liveChangePct != null) row.change_pct = liveChangePct;
      // Wire previous_close → row.close so the Prev Close column
      // renders on mover rows (including futures showing up as
      // pure movers). Without this, futures and stocks in the
      // movers buckets showed a blank Prev Close cell — only
      // watchlist + positions rows had close hooked up.
      if (row.close == null && liveClose != null)
        row.close = liveClose;
      if (liveClose != null && row.change == null && row.ltp != null)
        row.change = row.ltp - liveClose;
      row._mover_sticky    = m.sticky ?? false;
      row._mover_change_pct = liveChangePct ?? null;
      // Sub-group tag carried over from loadMovers() — drives the
      // identifiable underlying / midcap / smallcap sections in the grid.
      if (m._moverGroup)     row._moverGroup     = m._moverGroup;
      if (m._moverDirection) row._moverDirection = m._moverDirection;
      if (m._isLargeCap != null) row._isLargeCap = !!m._isLargeCap;
    }
    // When showMovers is off, strip the Movers-major rows entirely.
    if (!includeMovers) {
      for (const [k, row] of Object.entries(byKey)) {
        if (row._majorGroup === 'movers') delete byKey[k];
      }
    }

    // 6. Watched indices: re-tag tradingsymbol → underlying so the
    //    sort groups them with their derivatives.
    const INDEX_TO_UNDERLYING = {
      'NIFTY 50':              'NIFTY',
      'NIFTY BANK':            'BANKNIFTY',
      'NIFTY FIN SERVICE':     'FINNIFTY',
      'NIFTY IT':              'NIFTYIT',
      // Mid-cap variants — Kite returns 'NIFTY MIDCAP 100' literally
      // in the watchlist seed; the older 'NIFTY MID SELECT' label is
      // a different ticker but we treat both as the mid-cap pin slot.
      'NIFTY MID SELECT':      'MIDCPNIFTY',
      'NIFTY MIDCAP 100':      'MIDCPNIFTY',
      'NIFTY MIDCAP 50':       'MIDCPNIFTY',
      'NIFTY MIDCAP 150':      'MIDCPNIFTY',
      'NIFTY NXT 50':          'NIFTYNXT50',
      // NSE small-cap benchmarks — tag so the pin bucket catches them.
      // 'NIFTY SMLCAP 100' is Kite's abbreviated name (matches the
      // watchlist seed); 'NIFTY SMALLCAP *' covers the older full
      // spellings still present in some payloads.
      'NIFTY SMLCAP 100':      'SMALLCAP',
      'NIFTY SMLCAP 250':      'SMALLCAP',
      'NIFTY SMALLCAP 100':    'SMALLCAP',
      'NIFTY SMALLCAP 50':     'SMALLCAP',
      'SENSEX':                'SENSEX',
      'BANKEX':                'BANKEX',
      // Volatility — tag INDIA VIX so it joins the pin bucket and
      // groups under the same underlying as VIX-derived rows.
      'INDIA VIX':             'INDIAVIX',
    };
    for (const row of Object.values(byKey)) {
      const tag = INDEX_TO_UNDERLYING[String(row.tradingsymbol || '').toUpperCase()];
      if (tag) {
        row.underlying = tag;
        row.kind       = 'spot';
      }
    }

    // Finalise: weighted-avg avg_pos + directional day_pct + combined Avg.
    for (const row of Object.values(byKey)) {
      if (row.qty_pos !== 0) {
        row.avg_pos = row._avg_num / row.qty_pos;
      }
      if (row.qty_hold !== 0) {
        row.avg_hold = row._avg_hold_num / row.qty_hold;
      }
      // Combined Avg shown in the unified grid's Avg column:
      // weighted across whichever side(s) have qty. Cost basis is
      // sign-agnostic on the qty side so a short position's positive
      // entry price stays positive in the weighted average.
      const posCost  = Math.abs(row._avg_num);
      const holdCost = Math.abs(row._avg_hold_num);
      const denom    = Math.abs(row.qty_pos) + Math.abs(row.qty_hold);
      row.avg_combined = denom > 0 ? (posCost + holdCost) / denom : null;
      row._cost_basis  = posCost + holdCost;   // for P&L% column (lifetime return)
      // Holdings-specific Investment + Current Value columns. The user
      // is investing CASH against avg cost on holdings; positions are
      // intraday and don't carry a meaningful "invested" number — so
      // these stay null on pure-position rows and render as '—'.
      const heldAbs = Math.abs(row.qty_hold);
      const ltpNum  = Number(row.ltp);
      row.inv_val = heldAbs > 0 ? holdCost : null;
      row.cur_val = (heldAbs > 0 && Number.isFinite(ltpNum) && ltpNum > 0)
        ? ltpNum * heldAbs
        : null;
      delete row._avg_num;
      delete row._avg_hold_num;
      const netQty = (Number(row.qty_pos) || 0) + (Number(row.qty_hold) || 0);
      row.day_pct = directional(row.change_pct, netQty);
      // Day P&L % column needs yesterday's market value as denominator,
      // NOT cost basis. For a long-held stock with strong capital
      // appreciation, dividing day_pnl by cost over-states the day %:
      // an INFY held 10 yrs at ₹100 cost / ₹2000 today gets day_pnl ~₹20
      // (1% day) but day_pnl/cost = 20%. Using close × |qty| gives the
      // honest one-day return.
      const closeVal = Number(row.close) || 0;
      row._prev_market_value = closeVal > 0
        ? closeVal * (Math.abs(row.qty_pos) + Math.abs(row.qty_hold))
        : 0;
      // Pre-compute account colour once per row so cellStyle reads the
      // cached value directly instead of re-running the djb2 hash on
      // every ag-Grid cell paint (can fire 100s of times per second).
      const _lead = leadAccount(row);
      row._acctColor = _lead ? acctColor(_lead) : null;
    }

    // Sort: index groups first, then positions/holdings, watchlist,
    // bare underlyings. Within a group: spot → fut → opt (strike ASC,
    // CE before PE).
    const out = Object.values(byKey);
    // Detached symbols → each becomes its own single-row group keyed
    // off the tradingsymbol with a `__DETACHED__` prefix so it sorts
    // distinctly from the auto-group. The prefix puts detached rows
    // at the bottom of the bucket via localeCompare ordering.
    const detachedSet = new Set(detachedSymbols.map(s => s.toUpperCase()));
    const groupKey = (r) => {
      const sym = String(r.tradingsymbol || '').toUpperCase();
      if (detachedSet.has(sym)) return `__DETACHED__${sym}`;
      return r.underlying || `~~${r.tradingsymbol || ''}`;
    };
    const tierRank = (r) => {
      if (r.kind === 'spot')                 return 0;
      if (r.kind === 'fut')                  return 1;
      if (r.kind === 'opt')                  return 2;
      return 3;
    };
    const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);
    // Bucket priority — operator picked: watchlist first, then
    // positions, then holdings, then everything else (option
    // underlyings, movers, detached symbols). No automatic index pin
    // — indices appear in whichever bucket their source dictates
    // (they're watched, so they end up in the watchlist bucket).
    const groupBucket = /** @type {Record<string, number>} */ ({});
    for (const r of out) {
      const g = String(groupKey(r));
      const bucket = r.src?.w ? 1
                   : r.src?.p ? 2
                   : r.src?.h ? 3
                   : 4;
      if (groupBucket[g] == null || bucket < groupBucket[g]) {
        groupBucket[g] = bucket;
      }
    }
    // Manual group order — operator's ↑/↓ overrides take precedence
    // within a bucket. Groups without a manual rank sort alphabetically
    // AFTER manually-ranked groups.
    const order = groupOrder || {};
    const rankOf = (g) => {
      const u = String(g || '').toUpperCase();
      return order[u] != null ? order[u] : null;
    };
    out.sort((a, b) => {
      const ga = String(groupKey(a)), gb = String(groupKey(b));
      const ba = groupBucket[ga] ?? 2, bb = groupBucket[gb] ?? 2;
      if (ba !== bb) return ba - bb;
      if (ga !== gb) {
        const ra = rankOf(ga), rb = rankOf(gb);
        if (ra != null && rb != null && ra !== rb) return ra - rb;
        if (ra != null && rb == null) return -1;
        if (ra == null && rb != null) return  1;
        return ga.localeCompare(gb);
      }
      const ta = tierRank(a), tb = tierRank(b);
      if (ta !== tb) return ta - tb;
      if (a.kind === 'opt' && b.kind === 'opt') {
        const sa = a.strike ?? 0, sb = b.strike ?? 0;
        if (sa !== sb) return sa - sb;
        return optTypeRank(a) - optTypeRank(b);
      }
      return (a.tradingsymbol || '').localeCompare(b.tradingsymbol || '');
    });
    return out;
  }

  // ── Add / remove ────────────────────────────────────────────────

  async function searchSymbols(q) {
    if (!q || q.length < 3) { typeahead = []; return; }
    try {
      const { searchByPrefix } = await import('$lib/data/instruments');
      typeahead = await searchByPrefix(q.toUpperCase(), 12);
    } catch { typeahead = []; }
  }

  // Map the EQ/FU/CE/PE picker to the broker exchange used by
  // addToWatchlistDeduped. Cash equities live on NSE (BSE quotes
  // are reachable by typing the symbol explicitly via typeahead, which
  // overrides this); every derivative variant lands on NFO. MCX /
  // CDS instruments come in via the typeahead path which carries
  // the real exchange in inst.e.
  function _exchangeForType(t) {
    return t === 'EQ' ? 'NSE' : 'NFO';
  }

  // Resolve the target watchlist id for the next add. When the
  // operator picked 'NEW' from the watchlist dropdown, lazily create
  // the new list (using newListName), splice it into selectedShow
  // so its rows show immediately, and return its fresh id. Falls back
  // to the previously-focused list when the dropdown picker hasn't
  // been touched (e.g., direct-add via Enter on typeahead before any
  // dropdown interaction).
  async function _resolveTargetListId() {
    if (targetListId === 'NEW') {
      const name = newListName.trim();
      if (!name) return null;
      try {
        const w = await createWatchlist(name);
        newListName = '';
        targetListId = w.id;
        // CRITICAL ORDER — loadLists() FIRST, then push the wl: token
        // to selectedShow. The prune $effect (lines ~447) filters
        // selectedShow against `lists`, removing any wl: token whose
        // id isn't in the current lists set. If we push the new token
        // before loadLists() resolves, the $effect sees the old lists,
        // doesn't recognise the new id, and strips the token — the
        // operator's just-created watchlist disappears from the grid.
        // Operator: "when I create a new watchlist and select the
        // stock the panel disappears in pulse." Race fix: refresh
        // lists first so allowedWl already includes the new id.
        await loadLists();
        const newToken = `wl:${w.id}`;
        if (!selectedShow.includes(newToken)) {
          selectedShow = [...selectedShow, newToken];
        }
        focusedListId = w.id;
        return w.id;
      } catch (e) { error = e.message; return null; }
    }
    return targetListId ?? focusedListId ?? [...activeIds][0] ?? null;
  }

  async function addRow() {
    if (!symInput.trim()) return;
    const targetId = await _resolveTargetListId();
    if (targetId == null) return;
    try {
      await addToWatchlistDeduped(
        targetId,
        symInput.trim().toUpperCase(),
        _exchangeForType(typeInput),
        aliasInput.trim() || null,
      );
      symInput = ''; aliasInput = '';
      typeahead = []; typeaheadOpen = false;
      searchOpen = false;
      await loadActive();
    } catch (e) { error = e.message; }
  }

  async function pickFromTypeahead(inst) {
    typeaheadOpen = false;
    // Sync the EQ/FU/CE/PE picker to whatever the operator just chose
    // — purely a UI hint; the actual exchange used below comes from
    // inst.e (the broker's authoritative value).
    const sym = String(inst.s || '').toUpperCase();
    if (sym.endsWith('CE'))       typeInput = 'CE';
    else if (sym.endsWith('PE'))  typeInput = 'PE';
    else if (sym.endsWith('FUT')) typeInput = 'FU';
    else                          typeInput = 'EQ';
    // If the picked symbol is an underlying (has CE/PE chains), open the
    // inline option picker instead of adding directly. Close the
    // search modal first so the option picker isn't visually stacked
    // behind it.
    searchOpen = false;
    const opened = await openOptionPicker(inst.s, inst.e);
    if (opened) return;
    // Direct-add path: equities, futures, CDS, and anything without a chain.
    const targetId = await _resolveTargetListId();
    if (targetId == null) return;
    try {
      await addToWatchlistDeduped(targetId, inst.s, inst.e, aliasInput.trim() || null);
      symInput = ''; aliasInput = '';
      typeahead = []; typeaheadOpen = false;
      await loadActive();
    } catch (e) { error = e.message; }
  }

  function openSearch() {
    searchOpen = true;
    typeaheadOpen = false;
    // Seed the watchlist dropdown to the default list (or the
    // currently-focused list) so a fresh popup always has a sensible
    // target pre-selected. Operator can flip to "+ New watchlist" or
    // any other list from the dropdown.
    const def = lists.find(l => l.is_default);
    targetListId = focusedListId ?? def?.id ?? lists[0]?.id ?? null;
    // Defer focus until the popup mounts. requestAnimationFrame is
    // enough — the input is rendered in the same Svelte tick.
    requestAnimationFrame(() => { symInputEl?.focus(); symInputEl?.select(); });
  }
  function closeSearch() {
    searchOpen = false;
    typeaheadOpen = false;
    // Reset transient form state so the popup opens clean next time.
    newListName = '';
    aliasInput  = '';
    cancelRename();
  }

  async function makeList() {
    if (!newListName.trim()) return;
    try {
      const w = await createWatchlist(newListName.trim());
      newListName = '';
      // Race fix (mirrors `_resolveTargetListId`) — loadLists() FIRST
      // so the prune $effect's allowedWl set already contains the new
      // id by the time we push the token onto selectedShow. Otherwise
      // the effect strips the just-added wl: token and the new
      // watchlist's grid panel never appears.
      await loadLists();
      const newToken = `wl:${w.id}`;
      if (!selectedShow.includes(newToken)) {
        selectedShow = [...selectedShow, newToken];
      }
      focusedListId = w.id;
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // Rename UX state — set to the list id when the operator clicks
  // ✎ Rename, then the row reveals an inline name input. Save commits
  // to /api/watchlist/{id} via renameWatchlist; cancel reverts state.
  let _renameId    = $state(/** @type {number|null} */ (null));
  let _renameName  = $state('');
  let _renameError = $state('');
  async function commitRename() {
    const id = _renameId;
    const name = _renameName.trim();
    if (id == null || !name) return;
    _renameError = '';
    try {
      await renameWatchlist(id, { name });
      _renameId = null;
      _renameName = '';
      await loadLists();
    } catch (e) {
      _renameError = e?.message || 'Rename failed';
    }
  }
  function cancelRename() {
    _renameId = null;
    _renameName = '';
    _renameError = '';
  }

  async function dropList(/** @type {number} */ id) {
    try {
      await deleteWatchlist(id);
      // Remove the token from the unified Show multiselect — activeIds
      // gets re-derived via the propagation $effect. The pruning effect
      // would also strip it once `lists` reloads, but doing it here too
      // avoids a brief frame where the deleted list still shows.
      const droppedToken = `wl:${id}`;
      selectedShow = selectedShow.filter(v => v !== droppedToken);
      if (focusedListId === id) {
        focusedListId = [...activeIds].find(x => x !== id) ?? null;
      }
      await loadLists();
      if (activeIds.size === 0 && lists.length) {
        // Re-seed onto the first remaining list so the grid never
        // sits empty when an operator just nuked their only active list.
        selectedShow = [...selectedShow, `wl:${lists[0].id}`];
        focusedListId = lists[0].id;
      }
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // ── Grid setup ────────────────────────────────────────────────────

  function symRenderer(params) {
    const row = params.data || {};
    // Two alias sources can light up the cell:
    //   - row.display_name — operator-supplied nickname on the
    //     WatchlistItem (e.g. "Crude oil" for CRUDEOIL26JUNFUT). Wins
    //     over the quote-key alias when present.
    //   - row.alias — quote-key vs tradingsymbol shim ("NIFTY 50"
    //     spot keyed off "NIFTY"). Shown as a "→ raw symbol" hint.
    const opAlias = row.display_name ? String(row.display_name) : '';
    // When the row's `main` is the raw tradingsymbol (no operator alias,
    // no quote-key alias), apply the Dhan-style hyphenated transform
    // so the cell reads as NIFTY-26JUN-22000-CE not NIFTY26JUN22000CE.
    // Aliases (display_name / quote-key) are operator-visible labels —
    // those bypass the formatter.
    const main    = opAlias
                    || row.alias
                    || _pulseFmtSym(row.tradingsymbol || '');
    // Always surface the raw tradingsymbol after the main when an alias
    // is in play so the operator can still see what the row really is.
    const aliasTail = (opAlias || row.alias)
      ? `<span class="sym-alias" title="Tradingsymbol"> → ${_pulseFmtSym(row.tradingsymbol || '')}</span>`
      : '';
    // CE / PE tint on the symbol text — Sensibull / Streak convention.
    // Green = Call (right to BUY the underlying), red = Put (right to
    // SELL). Operator scanning their book tells calls from puts at a
    // glance without reading the strike.
    const optClass = row.opt_type === 'CE' ? 'sym-ce'
                    : row.opt_type === 'PE' ? 'sym-pe'
                    : '';
    // Lot-viable chip — sits IMMEDIATELY after the symbol text (before
    // any P/H/W/U/M badge group) so the operator's eye lands on the
    // actionable "how many covered calls can I write" number first.
    // Operator: "keep lot chip immediately after the symbol." Shown
    // only when the holding qty ≥ 1 whole lot of an F&O underlying.
    //
    // Colour rule (operator: "color code the lot chip based on if
    // underlying active positions exist"):
    //   no derivative position on this underlying → GREEN  (clean,
    //     covered-call viable; write something new)
    //   existing derivative position on this underlying → AMBER
    //     (the operator already has exposure here — covered call /
    //      hedge / spread in play; double-writing would compound risk)
    let lotChip = '';
    if (row.src?.h) {
      try {
        const symStr = String(row.tradingsymbol || '').toUpperCase();
        const lot = symStr ? _fnoLotFor(symStr) : 0;
        if (lot > 0) {
          const qHold = Math.abs(Number(row.qty_hold) || 0);
          // Round to one decimal place. Operator: "if lot size is
          // fraction, show that with one decimal point precision …
          // if decimal point is 0, don't show decimal point and
          // fraction part." So 70/100 = 0.7L (DIXON-style sub-lot),
          // 100/100 = 1L (whole lot), 150/100 = 1.5L. Threshold of
          // 0.1 hides noise from negligible holdings (e.g. 1 share
          // of a 100-lot stock = 0.0L would clutter the row).
          const lotsRounded = Math.round((qHold / lot) * 10) / 10;
          if (lotsRounded >= 0.1) {
            const lotsStr = lotsRounded % 1 === 0
              ? lotsRounded.toFixed(0)
              : lotsRounded.toFixed(1);
            const _hasPos = _underlyingsWithActivePositions.has(symStr);
            const _cls = _hasPos ? 'badge-fno-lot badge-fno-lot-pos'
                                 : 'badge-fno-lot';
            const _plural = lotsRounded === 1 ? '' : 's';
            const _title = _hasPos
              ? `Covered-call viable — ${lotsStr} lot${_plural} (lot size ${lot}). Underlying already has an open derivative position; review exposure before writing more.`
              : `Covered-call viable — ${lotsStr} lot${_plural} (lot size ${lot})`;
            lotChip = `<span class="sym-badge ${_cls}" title="${_title}">${lotsStr}L</span>`;
          }
        }
      } catch (_) { /* defensive */ }
    }
    const badges = [];
    if (row.src?.p) {
      const q = Number(row.qty_pos) || 0;
      badges.push(`<span class="sym-badge badge-p" title="Position">P ${qtyFmt(q)}</span>`);
    }
    if (row.src?.h) {
      const q = Number(row.qty_hold) || 0;
      badges.push(`<span class="sym-badge badge-h" title="Holding">H ${qtyFmt(q)}</span>`);
    }
    if (row.src?.w) {
      badges.push(`<span class="sym-badge badge-w" title="Watchlist">W</span>`);
    }
    if (row.src?.u) {
      badges.push(`<span class="sym-badge badge-u" title="Option underlying">U</span>`);
    }
    if (row.src?.m) {
      const pct = row._mover_change_pct;
      const dir = pct != null && pct >= 0 ? 'pos' : 'neg';
      const arrow = pct != null && pct >= 0 ? '↑' : '↓';
      const sticky = row._mover_sticky ? ' (sticky)' : '';
      const label = pct != null ? `${pct >= 0 ? '+' : ''}${Number(pct).toFixed(2)}%` : '';
      badges.push(`<span class="sym-badge badge-m badge-m-${dir}" title="Top mover ${label}${sticky}">M${arrow}</span>`);
    }
    const badgeHtml = badges.length ? `<span class="sym-badges">${badges.join('')}</span>` : '';
    // Per-row remove (×) — operator can drop a watchlist row inline.
    // Hidden on the shared global Pinned row for non-admin / non-
    // designated users since the backend would 403 the delete; UI
    // shouldn't tease an affordance that won't work.
    const _isGlobalRow = _globalListIds.has(row.watchlist_list_id);
    const _canRemoveHere = !_isGlobalRow || _isDesignated;
    const removeBtn = (row.src?.w && row.watchlist_item_id != null && _canRemoveHere)
      ? `<span class="sym-remove" data-item="${row.watchlist_item_id}" data-list="${row.watchlist_list_id ?? ''}" title="Remove from watchlist">×</span>`
      : '';
    // Per-row actions menu — ⋯ pops a Chart / Watchlist / Trade
    // popover via handleRowClick → _actionsMenu state. Hidden on
    // bare-underlying placeholder rows where there's no resolvable
    // symbol to act on.
    const sym = String(row.tradingsymbol || '').trim();
    const exch = String(row.exchange || '').trim();
    const actionsBtn = sym
      ? `<span class="sym-actions" data-sym="${sym}" data-exch="${exch}" data-watchitem="${row.watchlist_item_id ?? ''}" title="Symbol actions">⋯</span>`
      : '';
    // Per-row ↑/↓ buttons — move the row's whole underlying group up
    // or down within its bucket. Visible on row hover (CSS handles
    // the hover-only display); the actual move happens in
    // handleRowClick via data-attr dispatch.
    const moveBtns = sym
      ? `<span class="sym-move" data-dir="-1" title="Move group up">▲</span>` +
        `<span class="sym-move" data-dir="1"  title="Move group down">▼</span>`
      : '';
    // B2 — screen-reader direction label for pos-long / pos-short rows.
    let dirLabel = '';
    if (row.src?.p) {
      const q = Number(row.qty_pos) || 0;
      if (q > 0) dirLabel = '<span class="sr-only">Long position</span>';
      else if (q < 0) dirLabel = '<span class="sr-only">Short position</span>';
    }
    return `<span class="sym-main ${optClass}">${main}</span>${lotChip}${aliasTail}${badgeHtml}${removeBtn}${moveBtns}${actionsBtn}${dirLabel}`;
  }

  /**
   * Inline SVG sparkline for the last N daily closes.
   * ~60×18px, no axes/labels, stroke coloured by direction.
   * Missing data → em-dash placeholder.
   */
  function sparkRenderer(params) {
    const sym    = String((params.data || {}).tradingsymbol || '').toUpperCase();
    let base     = sparklines[sym];
    if (!base || base.length === 0) {
      return '<span style="display:flex;align-items:center;justify-content:center;height:100%;color:#7e97b8;font-size:0.6rem">—</span>';
    }
    // Defensive: when the backend ships only the current LTP (movers
    // entering a fresh universe with no cached history + broker rate-
    // limited), duplicate the single point so the polyline can draw
    // a flat baseline. Backend (quote.py:batch_sparkline) also pads
    // this case server-side; this branch handles older deploys + any
    // race where the response slipped through with length=1.
    if (base.length === 1) {
      base = [base[0], base[0]];
    }
    // Override the last (today's) point with the live SSE LTP when
    // available — sparkline tail tracks the real-time price.
    const liveTail = _liveLtpSnap[sym] ?? null;
    const closes = liveTail != null
      ? [...base.slice(0, -1), liveTail]
      : base;
    // SVG centered inside a flex wrapper that fills the cell, so left
    // and right whitespace are equal regardless of column width. An
    // inline-block + symmetric padding earlier sat against the cell's
    // left edge (cells default to text-align:left in ag-Grid), making
    // the curve look pushed against the left border.
    const W = 32, H = 14, PAD = 2;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const xStep = (W - PAD * 2) / (closes.length - 1);
    const yOf   = (v) => PAD + (1 - (v - min) / range) * (H - PAD * 2);
    const pts   = closes.map((v, i) => `${(PAD + i * xStep).toFixed(1)},${yOf(v).toFixed(1)}`).join(' ');
    const up    = closes[closes.length - 1] >= closes[0];
    const color = up ? 'rgba(91,142,149,0.85)' : 'rgba(196,122,61,0.85)';
    return `<span style="display:flex;align-items:center;justify-content:center;height:100%"><svg width="${W}" height="${H}" style="display:block;overflow:visible"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg></span>`;
  }

  function dirCls(v) {
    if (v == null) return 'cell-flat';
    if (v > 0) return 'cell-pos';
    if (v < 0) return 'cell-neg';
    return 'cell-flat';
  }

  function getRowClass(params) {
    const r = params.data || {};
    const s = r.src || {};
    const classes = [];
    // TOTAL rows on the right grid — bold, slightly-elevated tint
    // so the operator's eye lands on the consolidated number.
    if (r._isTotal) {
      classes.push('mp-total-row');
      classes.push(`mp-total-${r._majorGroup || 'misc'}`);
    }
    // Pin-category divider — first row of each pinned sub-group
    // (indices → forex → commodity) gets a top-border so the three
    // mini-sections at the very top of the grid feel distinct.
    if (r._pinFirst && r._pinCategory) {
      classes.push(`pin-divider pin-cat-${r._pinCategory}`);
    }
    // Major-group divider — first row of each major in mainRows
    // (watchlist → positions → holdings → movers) gets an amber
    // top-border + section label. Operator sees four visually
    // distinct blocks below the Pinned strip.
    if (r._majorFirst && r._majorGroup && r._majorGroup !== 'pinned') {
      classes.push(`major-divider major-${r._majorGroup}`);
    }
    // Within Movers, first row of each direction (Winners / Losers)
    // carries a stronger top-divider + colour label (green for winners,
    // red for losers). Suppressed on the major-first row because that
    // already paints the Movers divider; the operator sees the major
    // label then the direction label as the body begins.
    if (r._majorGroup === 'movers' && r._moverDirectionFirst && !r._majorFirst) {
      // Direction-specific sub-class (`mover-dir-winners` / `-losers`)
      // was retired with the per-colour divider rules; only the
      // parent class is consumed by CSS now.
      classes.push('mover-direction-divider');
    }
    if (r._majorGroup === 'movers' && r._moverDirection) {
      classes.push(`mover-dir-row-${r._moverDirection}`);
    }
    // Within each direction, first row of each sub-group (underlying /
    // midcap / smallcap) gets a thinner divider so operators can scan
    // the three universes individually. Skip when the row is ALSO the
    // major-first OR direction-first (those already paint dividers —
    // stacking three would triple-line).
    if (r._majorGroup === 'movers' && r._moverGroupFirst
        && !r._majorFirst && !r._moverDirectionFirst) {
      // Group-specific sub-class (`mover-grp-underlying` / `-midcap`
      // / `-smallcap`) was retired with the per-colour divider rules;
      // only the parent class is consumed by CSS now. The per-group
      // left-edge accent uses `mover-${r._moverGroup}` (a different
      // class, pushed below).
      classes.push('mover-group-divider');
    }
    if (r._majorGroup === 'movers' && r._moverGroup) {
      classes.push(`mover-${r._moverGroup}`);
    }
    if (s.p) {
      const q = Number(r.qty_pos) || 0;
      if (q < 0) classes.push('pos-short');
      else if (q > 0) classes.push('pos-long');
      else classes.push('row-pos');
    }
    else if (s.h) {
      // Differentiate Holdings rows by P&L sign so the symbol-cell
      // tint border encodes "this holding is up / down / flat" at a
      // glance, matching the pos-long / pos-short visual idiom on
      // Positions. Earlier every Holding got the same green tint
      // regardless of whether the operator was up or down on it —
      // visually uniform but information-blind.
      const pnl = Number(r.pnl);
      if (Number.isFinite(pnl) && pnl > 0) classes.push('row-hold-up');
      else if (Number.isFinite(pnl) && pnl < 0) classes.push('row-hold-down');
      else classes.push('row-hold-flat');
      // F&O-eligibility tagging — one class, one visual treatment:
      //   row-hold-fno → stock has options listed (green left stripe)
      // Lot-count viability is communicated via the NL badge appended
      // to the symbol cell by symRenderer, not by a second row class.
      try {
        if (getInstrument) {
          const sym = String(r.tradingsymbol || '').toUpperCase();
          if (_fnoLotFor(sym) > 0) classes.push('row-hold-fno');
        }
      } catch (_) { /* defensive — cache miss shouldn't break row class */ }
    }
    // Day-P&L indicator deprecated — the symbol-cell right border now
    // encodes POSITION direction (pos-long/pos-short) or HOLDING
    // direction (row-hold-up/-down) instead of today's P&L sign.
    // Operator preferred the position-direction encoding so the cell
    // border tells "am I long or short?" at a glance, without grey
    // for flat rows. See app.css :: ".ag-row.pos-long .ag-col-sym".
    if (s.w && !s.p && !s.h) classes.push('row-watch');
    else if (s.u) classes.push('row-und');
    return classes.join(' ');
  }

  function mountGrid() {
    const RA = 'ag-right-aligned-cell';
    const dirCellClass = (p) => `${RA} ${dirCls(p.value)}`;
    // P&L-tinted cell class for the Positions / Holdings right-grid
    // P&L + Day P&L columns. Adds a green/red background tint on
    // top of the direction-colored text — matches the Candidates
    // panel in /admin/derivatives (`.cand-pnl.cell-pos` etc.) so the
    // P&L columns read with the same visual identity across both
    // surfaces. Plain `dirCellClass` (text-only) stays on the
    // watch/mover grids where the P&L column isn't surfaced.
    const pnlCellClass = (p) => `${RA} ${dirCls(p.value)} mp-pnl-cell`;

    // Main symbols grid — only built when the parent opted into the
    // per-symbol view (/pulse). /dashboard passes showSymbolsGrid=false
    // because it shows only summary + funds; the summary/funds grids
    // below still need to mount in that case.
    if (showSymbolsGrid && gridPinnedEl) {

    // ─── Shared column shapes ────────────────────────────────────
    // Left and right grids serve different audiences:
    //   left  → market-data scan (Pinned / Watchlist / Movers)
    //   right → operator's book (Positions / Holdings)
    // Position-specific columns (Avg / Qty / Day P&L / P&L / P&L %)
    // render blank on left-grid rows because those rows carry no qty
    // — so they were noise. Right grid carries the full position set
    // plus an Account column at the trailing edge so the operator can
    // tell at a glance which book a row belongs to.
    const _symColLeft = {
      field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
      cellRenderer: symRenderer, sortable: true,
      cellClass: 'ag-col-sym ag-col-fill',
    };
    // Right-grid symbol cell carries an account-tinted background +
    // vertical borders so the operator can colour-spot each row's
    // owning account at a glance (matches the per-account hash
    // palette already used on PerformancePage account stripes).
    const _symColRight = {
      field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
      cellRenderer: symRenderer, sortable: true,
      cellClass: 'ag-col-sym ag-col-fill mp-sym-acct',
      cellStyle: (p) => {
        if (p.data?._isTotal) return {};
        const color = p.data?._acctColor ?? null;
        if (!color) return {};
        return {
          '--mp-sym-acct-color': color,
        };
      },
    };
    // Per-symbol columns suppress their values on TOTAL rows — the
    // aggregate doesn't have a meaningful LTP / Prev / Avg / Open /
    // Vol / OI / sparkline, so the cell renders blank instead of a
    // bogus number. Day P&L %, P&L %, P&L and Day P&L stay populated
    // (those are legitimately aggregable across the bucket).
    const _sparkCol = {
      field: 'tradingsymbol', headerName: '5d', width: 44, minWidth: 44,
      maxWidth: 48, colId: 'sparkline',
      cellRenderer: (p) => p.data?._isTotal ? '' : sparkRenderer(p),
      sortable: false, resizable: false,
      cellClass: 'spark-cell',
      headerClass: 'ag-header-cell-spark',
    };
    const _ltpCol = {
      // valueGetter reads _liveLtpSnap first (real-time SSE tick) and
      // falls back to the polled row.ltp from buildUnified. ag-Grid
      // re-evaluates the getter when refreshCells is called on 'ltp'.
      colId: 'ltp', headerName: 'LTP', width: 77, minWidth: 77, maxWidth: 96,
      type: 'numericColumn', headerClass: numericHdr,
      // Heat encoding: bg vs purchase price (avg_pos / avg_hold),
      // left-border stripe vs prev_close. Operator can scan both
      // axes simultaneously — "am I up overall?" (bg) and "is it
      // going my way today?" (stripe). TOTAL row suppresses both.
      cellClass: (p) => {
        if (!p.data || p.data._isTotal) return RA;
        const sym = String(p.data.tradingsymbol || '').toUpperCase();
        const ltp  = _liveLtpSnap[sym] ?? p.data.ltp ?? null;
        const prev = p.data.close ?? null;
        const avg  = (p.data.qty_pos && p.data.avg_pos) ? p.data.avg_pos
                   : (p.data.qty_hold && p.data.avg_hold) ? p.data.avg_hold
                   : null;
        const cls = [RA];
        // B1 — flash green/red on tick-up/tick-down (slice AS audit fix).
        if      (_ltpFlashUp.has(sym))   cls.push('ltp-flash-up');
        else if (_ltpFlashDown.has(sym)) cls.push('ltp-flash-down');
        // Design B — freshness shimmer: neutral underline sweep on every data
        // update from the bus, regardless of direction.
        const shimmerCls = _ltpShimmer.classOf(sym);
        if (shimmerCls) cls.push(shimmerCls);
        if (typeof ltp === 'number' && typeof avg === 'number' && avg > 0) {
          cls.push(ltp > avg ? 'ltp-vs-avg-up' : ltp < avg ? 'ltp-vs-avg-down' : 'ltp-vs-avg-flat');
        }
        if (typeof ltp === 'number' && typeof prev === 'number' && prev > 0) {
          cls.push(ltp > prev ? 'ltp-vs-prev-up' : ltp < prev ? 'ltp-vs-prev-down' : 'ltp-vs-prev-flat');
        }
        return cls.join(' ');
      },
      valueGetter: (p) => {
        if (!p.data) return null;
        const sym = String(p.data.tradingsymbol || '').toUpperCase();
        return _liveLtpSnap[sym] ?? p.data.ltp ?? null;
      },
      valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
    };
    const _prevCol = {
      field: 'close', headerName: 'Close', width: 68, minWidth: 68, maxWidth: 84,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: `${RA} cell-muted`,
      valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
    };
    const _openCol = {
      field: 'open', headerName: 'Open', width: 68, minWidth: 68, maxWidth: 90,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: `${RA} cell-muted`,
      valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
    };
    const _volCol = {
      field: 'volume', headerName: 'Vol', width: 58, minWidth: 58, maxWidth: 80,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: `${RA} cell-muted`,
      valueFormatter: (p) => {
        if (p.data?._isTotal) return '';
        return (p.value == null || p.value === 0) ? '—' : aggCompact(p.value);
      },
    };
    const _oiCol = {
      field: 'oi', headerName: 'OI', width: 58, minWidth: 58, maxWidth: 80,
      type: 'numericColumn', headerClass: numericHdr,
      cellClass: `${RA} cell-muted`,
      valueFormatter: (p) => {
        if (p.data?._isTotal) return '';
        return (p.value == null || p.value === 0) ? '—' : aggCompact(p.value);
      },
    };

    // ─── Left grid: Pinned / Watchlist / Movers ──────────────────
    // Day % shows raw symbol change_pct (no qty) — back-fills the
    // information the legacy Day % column carried for non-position
    // rows. Renders blank on rows without a quote.
    const leftColDefs = /** @type {any[]} */ ([
      _symColLeft,
      _sparkCol,
      _ltpCol,
      { field: 'change_pct', headerName: 'Day %', colId: 'left_change_pct',
        width: 64, type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: pctFmtGrid,
        headerTooltip: 'Raw symbol day-change % (no qty).' },
      _prevCol,
      _openCol,
      _volCol,
      _oiCol,
    ]);  // ← left grid (Pinned / Watchlist / Movers) — no Account col

    // ─── Right grid: Positions / Holdings ────────────────────────
    // Column order — action-first: numbers leading, Account trailing.
    // Operator: "i am more interested in ltp, avg, day p&l, p&l, etc
    // for taking action."
    //
    //   Symbol → Sparkline → LTP → Avg → Day P&L → Day % → Close →
    //   P&L → P&L % → Qty → Invested → Value → Open → Vol →
    //   OI → Account (trailing)
    //
    // The trailing Account column keeps the lead-account-or-"+N"
    // valueGetter + per-account tint so colour-spotting still works,
    // but it sits at the end of the row where it doesn't compete with
    // the action-relevant numbers.
    const _acctColTrailing = {
      field: '_acct_display', headerName: 'Account', colId: 'account',
      width: 86, minWidth: 70, maxWidth: 110,
      cellClass: 'mp-acct-cell',
      cellStyle: (p) => {
        if (p.data?._isTotal) return {};
        const color = p.data?._acctColor ?? null;
        if (!color) return {};
        return { color };
      },
      valueGetter: (p) => {
        if (p.data?._isTotal) return '';
        const accts = p.data?.accounts;
        if (!accts) return '';
        const list = accts instanceof Set ? Array.from(accts) : Array.isArray(accts) ? accts : [];
        if (list.length === 0) return '';
        if (list.length === 1) return list[0];
        return `${list[0]} +${list.length - 1}`;
      },
    };
    const rightColDefs = /** @type {any[]} */ ([
      _symColRight,
      _sparkCol,
      _ltpCol,
      // Avg — weighted entry price across positions + holdings on the row.
      { field: 'avg_combined', headerName: 'Avg', colId: 'avg_combined',
        width: 68, minWidth: 60, maxWidth: 90,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: (p) => p.data?._isTotal ? '' : numFmt({ value: p.value }),
        headerTooltip: 'Weighted average entry across positions + holdings.' },
      { field: 'day_pnl', headerName: 'Day P&L', width: 78, minWidth: 60, maxWidth: 96,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: pnlCellClass,
        valueFormatter: aggFmtGrid },
      // Day P&L % — one-day return on yesterday's market value (close × qty).
      // NOT cost basis: dividing by cost over-states day % for a long-held
      // stock — INFY held 10 yrs at ₹100 cost / ₹2000 today posts a real
      // 1 % day move as 20 % when normalised against cost. close × qty is
      // the honest denominator: per-symbol this collapses to change_pct;
      // TOTAL gets a market-value-weighted day return.
      { field: 'day_pnl_pct', headerName: 'Day %', colId: 'day_pnl_pct',
        width: 64, type: 'numericColumn', headerClass: numericHdr,
        cellClass: pnlCellClass,
        valueGetter: (p) => {
          const dpnl = Number(p.data?.day_pnl);
          const prev = Number(p.data?._prev_market_value);
          // Underlying anchor rows (BHEL / BEL / NIFTY) carry no qty
          // and no day_pnl, but the underlying's change_pct IS on the
          // row. Fall back to it so the Day % column reads the
          // underlying's intraday move directly. Same fallback also
          // covers just-listed contracts and watchlist-only rows where
          // close × qty isn't computable.
          if (!Number.isFinite(dpnl) || prev <= 0) {
            const cp = Number(p.data?.change_pct);
            return Number.isFinite(cp) ? cp : null;
          }
          return (dpnl / prev) * 100;
        },
        valueFormatter: pctFmtGrid,
        headerTooltip: 'Day P&L as % of yesterday’s market value (close × qty).' },
      _prevCol,
      { field: 'pnl', headerName: 'P&L', width: 78, minWidth: 60, maxWidth: 96,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: pnlCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'pnl_pct', headerName: 'P&L %', colId: 'pnl_pct',
        width: 64, type: 'numericColumn', headerClass: numericHdr,
        cellClass: pnlCellClass,
        valueGetter: (p) => {
          const pnl  = Number(p.data?.pnl);
          const cost = Number(p.data?._cost_basis);
          if (!Number.isFinite(pnl) || !(cost > 0)) return null;
          return (pnl / cost) * 100;
        },
        valueFormatter: pctFmtGrid,
        headerTooltip: 'P&L as % of cost basis.' },
      { field: 'qty_net', headerName: 'Qty', width: 56, colId: 'qty_net',
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueGetter: (p) => {
          // Blank on TOTAL — summing qty across different symbols
          // (100 NIFTY + 50 RELIANCE etc.) gives a meaningless
          // number. Stays populated on per-symbol rows where qty is
          // a real shares/contracts count.
          if (p.data?._isTotal) return null;
          const q = (Number(p.data?.qty_pos) || 0) + (Number(p.data?.qty_hold) || 0);
          return q === 0 ? null : q;
        },
        valueFormatter: ({ value }) => value == null ? '' : qtyFmt(value) },
      // Lots — qty expressed in F&O lot units. Holdings on F&O
      // underlyings use the underlying's lot; option/futures POSITIONS
      // use the contract's own lot. Everything else (cash equity
      // position, non-F&O holding) reads 0. Operator: "you can keep
      // qty as lot size as a separate column. if it is not an
      // underlying show it as 0… similarly do it for option positions
      // for other positions show it as 0. keep it consistent across
      // all algo pages for holdings and positions."
      // One-decimal precision when fractional, integer otherwise.
      { field: 'lots', headerName: 'Lots', width: 52, colId: 'lots',
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueGetter: (p) => _lotsForUnifiedRow(p.data),
        valueFormatter: ({ value }) => _lotsFmt(value),
        headerTooltip: 'Qty in F&O lot units. Holdings on F&O underlyings use the underlying lot; option / futures positions use the contract lot. Cash equity + non-F&O rows read 0.' },
      // Investment + Current value — holdings only. The user wants both
      // values per row plus a TOTAL footer; aggregator sums them when
      // present (positions rows carry null inv/cur and skip the sum).
      { field: 'inv_val', headerName: 'Invested', colId: 'inv_val',
        width: 78, type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: aggFmtGrid,
        headerTooltip: 'Avg cost × held qty — your invested rupees on this holding.' },
      { field: 'cur_val', headerName: 'Value', colId: 'cur_val',
        width: 78, type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueFormatter: aggFmtGrid,
        headerTooltip: 'Live LTP × held qty — current market value of this holding.' },
      _openCol,
      _volCol,
      _oiCol,
      _acctColTrailing,
    ]);

    // Group-preserving postSortRows. After ag-Grid sorts each row
    // independently by the selected column, we re-arrange so an
    // underlying (kind='spot' / 'fut') always carries its options
    // (kind='opt') with it as one contiguous block. Operator: "sort
    // moves the GROUP, not individual elements in the group".
    //
    // Algorithm:
    //   1. Walk the post-sort row order.
    //   2. For each row, look up its group key (underlying name).
    //   3. The FIRST time we see a group, emit the anchor row +
    //      every other row in that group (preserving the post-sort
    //      relative order of the group's members).
    //   4. Subsequent rows of an already-emitted group are skipped
    //      since they were attached above.
    //
    // Rows without an underlying (cash equity, watchlist items
    // without F&O coverage) keep their post-sort position individually.
    function postSortGroups({ nodes }) {
      if (!nodes || nodes.length === 0) return;
      const byKey = new Map();   // groupKey → array of nodes
      const orderedGroupKeys = [];   // first-appearance order of groups
      const standaloneOrder = [];   // rows with no group key
      const standaloneIdx = new Map();   // node → its index in standaloneOrder
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const d = n.data || {};
        // Group key: underlying name. Rows without an underlying
        // (cash equity, pinned indices, watchlist symbols that
        // aren't derivatives) get a unique key so they don't
        // collapse into one another's group.
        const u = String(d.underlying || '').toUpperCase();
        if (!u) {
          standaloneIdx.set(n, standaloneOrder.length);
          standaloneOrder.push(n);
          continue;
        }
        if (!byKey.has(u)) {
          byKey.set(u, []);
          orderedGroupKeys.push(u);
        }
        byKey.get(u).push(n);
      }
      // Build the final order. Interleave standalone rows and group
      // blocks based on the first occurrence of each in the original
      // sorted list — preserves the relative sort order between
      // (groups + non-grouped rows).
      const firstIdxOf = new Map();   // groupKey → idx in original `nodes` of its first member
      for (const k of orderedGroupKeys) {
        firstIdxOf.set(k, nodes.indexOf(byKey.get(k)[0]));
      }
      // Combine groups + standalone rows, sorted by their first
      // occurrence in the ag-Grid sort result.
      const seq = [];
      for (const k of orderedGroupKeys) seq.push({ first: firstIdxOf.get(k), kind: 'g', key: k });
      for (const n of standaloneOrder) seq.push({ first: nodes.indexOf(n), kind: 's', node: n });
      seq.sort((a, b) => a.first - b.first);

      // Reassemble in place — ag-Grid mutates the nodes array.
      const out = [];
      for (const entry of seq) {
        if (entry.kind === 'g') {
          for (const n of byKey.get(entry.key)) out.push(n);
        } else {
          out.push(entry.node);
        }
      }
      // Mutate nodes in place (ag-Grid postSortRows contract).
      nodes.length = 0;
      for (const n of out) nodes.push(n);
    }

    // Factory: every per-bucket grid shares the same shape (height
    // tracks, getRowClass, sort + resize defaults, click handlers).
    // Only columnDefs / emptyMsg / pinnedBottom vary per bucket.
    function makeBucketGrid(el, columnDefs, emptyMsg, pinnedBottom = []) {
      return createGrid(el, {
        theme: 'legacy',
        columnDefs,
        rowData: [],
        pinnedBottomRowData: pinnedBottom,
        getRowId: ({ data }) => String(data?.key || data?.tradingsymbol || ''),
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        overlayNoRowsTemplate:
          `<span style="font-size:0.65rem;color:#7e97b8">${emptyMsg}</span>`,
        domLayout: 'normal',
        getRowClass,
        // Keep underlyings + their options together as a block during
        // column sort. The TOTAL row + pinned-bottom rows ride
        // outside this since they're rendered via pinnedBottomRowData
        // (not in the sortable body).
        postSortRows: postSortGroups,
        rowHeight: 28,
        headerHeight: 28,
        onRowClicked: handleRowClick,
        onCellContextMenu: (ev) => {
          if (ev.data) openContextMenu(ev.event, ev.data);
        },
      });
    }

    // Left-column buckets (monitoring views — leftColDefs).
    if (gridPinnedEl) {
      gridPinned = makeBucketGrid(gridPinnedEl, leftColDefs,
        'Pinned watchlist is empty — add a symbol via the + button.');
      gridPinnedReady = true;
    }
    if (gridWatchEl) {
      gridWatch = makeBucketGrid(gridWatchEl, leftColDefs,
        'No custom watchlists — create one to track symbols here.');
      gridWatchReady = true;
    }
    if (gridWinEl) {
      gridWin = makeBucketGrid(gridWinEl, leftColDefs,
        'No top winners right now.');
      gridWinReady = true;
    }
    if (gridLoseEl) {
      gridLose = makeBucketGrid(gridLoseEl, leftColDefs,
        'No top losers right now.');
      gridLoseReady = true;
    }
    // Right-column buckets (operator's book — rightColDefs + TOTAL
    // pinned at the bottom edge, sort-stable by ag-Grid native
    // pinnedBottomRowData semantics).
    if (gridPositionsEl) {
      gridPositions = makeBucketGrid(gridPositionsEl, rightColDefs,
        'No positions in the active book.');
      gridPositionsReady = true;
    }
    if (gridHoldingsEl) {
      gridHoldings = makeBucketGrid(gridHoldingsEl, rightColDefs,
        'No holdings in the active book.');
      gridHoldingsReady = true;
    }
    }  // end main symbols grid block

    // Positions Summary grid — Account | Day P&L | Day % | P&L
    // Compact widths — aggCompact maxes at ~8 chars ("-999.99L") so
    // 78 px fits every rupee value plus the standard cell padding.
    if (showSummary && positionsSummaryEl) {
      const posSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 76,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
      ];
      positionsSummaryGrid = createGrid(positionsSummaryEl, {
        theme: 'legacy',
        columnDefs: posSummaryCols,
        rowData: [],
        getRowId: ({ data }) => String(data?.account || ''),
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      positionsSummaryReady = true;
    }

    // Holdings Summary grid — Account | Day P&L | Day % | P&L | P&L % | Cur Val | Inv Val
    // Compact widths matching the Positions Summary above.
    if (showSummary && holdingsSummaryEl) {
      const holdSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 76,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: aggFmtGrid },
        { field: 'pnl_percentage',        headerName: 'P&L %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: pnlCellClass, valueFormatter: pctFmtGrid },
        { field: 'cur_val',               headerName: 'Value', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'inv_val',               headerName: 'Invested', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
      ];
      holdingsSummaryGrid = createGrid(holdingsSummaryEl, {
        theme: 'legacy',
        columnDefs: holdSummaryCols,
        rowData: [],
        getRowId: ({ data }) => String(data?.account || ''),
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      holdingsSummaryReady = true;
    }

    // Funds grid — per-account margins. Compact widths matching the
    // summary grids above. Header labels shortened (Avail Margin →
    // Avail Mar, Used Margin → Used Mar, Collateral → Coll) so the
    // headers don't truncate at 78 px column width.
    if (showFunds && fundsEl) {
      const fundsCols = [
        { field: 'account',        headerName: 'Account',   width: 76,
          cellClass: 'ag-col-fill' },
        { field: 'cash_total',     headerName: 'Cash',      width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid,
          headerTooltip: 'Live cash + cash debited on currently-held long options (= cash you would have if every long option were closed at its entry premium)',
          // live_cash + sum(avg_price × |qty|) for long CE/PE rows in
          // this account. `_long_opt_cash` is pre-computed by the
          // scopedFunds derivation. Falls back to row.cash when
          // live_cash is missing (older API cached during deploy).
          valueGetter: (/** @type {any} */ p) => {
            const d = p?.data || {};
            const lc  = Number(d.live_cash ?? 0);
            const loc = Number(d._long_opt_cash ?? 0);
            return (lc !== 0 ? lc : Number(d.cash ?? 0)) + loc;
          } },
        { field: 'live_cash',      headerName: 'Live Cash', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid,
          headerTooltip: 'Current cash — decreases when option premium is debited' },
        { field: '_long_opt_cash', headerName: 'Opt Cash',  width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Cash debited on currently-held long options (sum of avg_price × |qty| across each long CE/PE)' },
        { field: 'avail_margin',   headerName: 'Avail Margin', width: 92,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Available margin — net trading buffer' },
        { field: 'used_margin',    headerName: 'Used Margin', width: 90,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Margin currently locked against open positions' },
        { field: 'collateral',     headerName: 'Collateral', flex: 1, minWidth: 80,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Collateral value from pledged holdings' },
      ];
      fundsGrid = createGrid(fundsEl, {
        theme: 'legacy',
        columnDefs: fundsCols,
        rowData: [],
        getRowId: ({ data }) => String(data?.account || ''),
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      fundsReady = true;
    }
  }

  async function handleRowClick(ev) {
    if (!ev.data) return;
    const target = /** @type {HTMLElement | null} */ (ev.event?.target ?? null);
    const rmBtn = target?.closest?.('.sym-remove');
    if (rmBtn) {
      const itemId = Number(rmBtn.getAttribute('data-item'));
      const listId = Number(rmBtn.getAttribute('data-list'));
      if (itemId && listId) removeItem(listId, itemId);
      return;
    }
    // ▲/▼ group-move buttons — bump the row's whole underlying group
    // up or down. The bucket stays the same (pinned indices stay in
    // pinned, watchlist in watchlist) so swaps are constrained.
    const moveBtn = target?.closest?.('.sym-move');
    if (moveBtn) {
      ev.event?.stopPropagation?.();
      const dir = Number(moveBtn.getAttribute('data-dir')) || 0;
      if (dir !== 0) moveGroup(ev.data, dir);
      return;
    }
    // ⋯ actions button — re-uses the existing right-click context
    // menu (which already has Open in Options / Open ticket / Copy
    // symbol / Set price alert items). Positioning is anchored to
    // the button's bottom-right so it pops next to the symbol.
    const actBtn = target?.closest?.('.sym-actions');
    if (actBtn) {
      ev.event?.stopPropagation?.();
      const rect = /** @type {DOMRect} */ (actBtn.getBoundingClientRect());
      openContextMenu(
        { clientX: rect.right, clientY: rect.bottom + 4, preventDefault: () => {} },
        ev.data,
      );
      return;
    }
    if (!allowOrders) return;
    const r = ev.data;
    const inst = getInstrument?.(r.tradingsymbol);
    const lot = Number(inst?.ls || 1);
    let side = 'BUY';
    if (r.src?.p && r.qty_pos < 0) side = 'BUY';
    else if (r.src?.p && r.qty_pos > 0) side = 'SELL';
    // Use the row's own account field directly — each position/holding
    // row carries one unmasked account id for admin sessions, so this
    // is always correct. The Set+length=1 heuristic was unreliable when
    // a single account appeared after async realAccounts resolution.
    const isRealAcct = (a) => !!(a && !String(a).includes('#'));
    const preAccount = isRealAcct(r.account) ? String(r.account) : '';
    // Ensure accounts are resolved before opening the ticket — avoids
    // a blank picker on first click when onMount hasn't finished yet.
    if (!realAccounts.length) {
      try {
        const r2 = await fetchAccounts();
        realAccounts = (r2?.accounts || [])
          .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
          .filter(Boolean);
      } catch (_) { /* leave empty — ticket's self-fetch backstop will fill */ }
    }
    // Pass currentQty (signed position qty) so SymbolPanel's footer
    // button shows "CLOSE BUY" / "CLOSE SELL" for position rows and
    // the ADD/CLOSE verb in _addCloseVerb() activates correctly.
    // For watchlist/mover/anchor rows (no position), currentQty=0 so
    // the ticket opens as a plain BUY/SELL without close semantics.
    const posQty = r.src?.p ? (Number(r.qty_pos) || 0) : 0;
    openTicket({
      symbol:     r.tradingsymbol,
      exchange:   r.exchange,
      side,
      qty:        Math.abs(posQty) || lot,
      lotSize:    lot,
      accounts:   realAccounts,
      account:    preAccount,
      currentQty: posQty,
      action:     posQty !== 0 ? 'close' : 'open',
    });
  }

  function openTicket(p) { ticketProps = { defaultTab: 'ticket', ...p }; }
  function closeTicket() { ticketProps = null; }

  async function removeItem(/** @type {number} */ listId, /** @type {number} */ itemId) {
    try {
      await removeWatchlistItem(listId, itemId);
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // makeListAndClose was retired when the standalone "New watchlist"
  // section folded into the Watchlist target dropdown — list creation
  // now happens lazily inside _resolveTargetListId() the moment the
  // operator clicks Add with target = 'NEW'.

  // ── Context menu ─────────────────────────────────────────────────
  /** @type {{ x: number, y: number, row: any } | null} */
  let ctxMenu = $state(null);
  let ctxMenuEl = $state(/** @type {HTMLElement | null} */ (null));

  function openContextMenu(ev, row) {
    ev.preventDefault();
    ctxMenu = { x: ev.clientX, y: ev.clientY, row };
  }
  function closeContextMenu() { ctxMenu = null; }

  function ctxOpenOptions(row) {
    closeContextMenu();
    const sym = encodeURIComponent(row.tradingsymbol || '');
    window.location.href = `/admin/derivatives?symbol=${sym}`;
  }
  function ctxOpenTicket(row) {
    closeContextMenu();
    // Synthesise a fake ag-Grid event shape handleRowClick expects.
    handleRowClick({ data: row, event: null });
  }
  async function ctxCopySymbol(row) {
    closeContextMenu();
    try { await navigator.clipboard.writeText(row.tradingsymbol || ''); } catch (_) {}
  }
/** 📈 Chart action — opens ChartModal directly for the row's symbol.
   *  SymbolPanel's TABS no longer includes 'chart' (it was retired when
   *  ChartModal became the dedicated chart surface), so opening via
   *  openTicket({defaultTab:'chart'}) produced a blank modal body.
   *  We now open ChartModal independently using _chartModalSym/_chartModalExch. */
  function ctxOpenChart(row) {
    closeContextMenu();
    const sym = String(row.tradingsymbol || '').trim();
    if (!sym) return;
    _openChartModal(sym, String(row.exchange || '').trim());
  }

  async function ctxRemoveWatch(row) {
    closeContextMenu();
    if (row.watchlist_item_id && row.watchlist_list_id) {
      await removeItem(row.watchlist_list_id, row.watchlist_item_id);
    }
  }
  /** Add the symbol to the focused / first-active watchlist. Mirror of
   *  the existing keyboard "+" handler. */
  async function ctxAddWatch(row) {
    closeContextMenu();
    const sym  = String(row.tradingsymbol || '').trim();
    const exch = String(row.exchange || 'NFO').trim();
    if (!sym) return;
    const targetId = focusedListId ?? [...activeIds][0];
    if (!targetId) return;
    try {
      await addToWatchlistDeduped(targetId, sym, exch);
      // Reload active watchlists so the row materialises immediately
      // without waiting for the next refresh cycle.
      await loadActive();
    } catch (e) {
      // 409 = already in list — silently ignore, the operator's
      // intent (have this symbol in the list) is already satisfied.
      const msg = String(/** @type {any} */ (e)?.message || '');
      if (!/already/i.test(msg)) {
        console.warn('Add to watchlist failed:', e);
      }
    }
  }

  function ctxOpenOrders(row) {
    closeContextMenu();
    const sym = encodeURIComponent(row.tradingsymbol || '');
    window.location.href = `/orders?symbol=${sym}`;
  }
  function ctxOpenLog(/** @type {any} */ _row) {
    closeContextMenu();
    // Open the layout-mounted ActivityLogModal singleton via store
    // instead of mounting a second instance here that races the
    // layout's. Duplication-audit P1 fix.
    openActivityModal('order');
  }

  // Chart modal state — opened by the "Chart →" context-menu item.
  // Does NOT go through SymbolPanel (the chart tab was retired from
  // TABS; opening with defaultTab:'chart' produced a blank modal body).
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  // Modal open flag — separate from the symbol so {#if} reactivity
  // depends on a boolean, not the same string the modal also reads as
  // its `symbol` prop. Closing the modal sets _chartModalOpen=false
  // synchronously, then clears the symbol/exchange after a microtask
  // so the unmount path doesn't see a transient empty-string symbol.
  let _chartModalOpen = $state(false);
  function _openChartModal(/** @type {string} */ sym, /** @type {string} */ exch) {
    _chartModalSym  = String(sym  || '');
    _chartModalExch = String(exch || '');
    _chartModalOpen = true;
  }
  function _closeChartModal() {
    _chartModalOpen = false;
    queueMicrotask(() => { _chartModalSym = ''; _chartModalExch = ''; });
  }

  // Dismiss context menu on outside click.
  function onDocClick(ev) {
    if (ctxMenu && ctxMenuEl && !ctxMenuEl.contains(/** @type {Node} */ (ev.target))) {
      closeContextMenu();
    }
  }

  // ── Keyboard shortcuts ────────────────────────────────────────────
  let pulseWrapper = $state(/** @type {HTMLElement | null} */ (null));
  /** @type {HTMLInputElement | null} */ let symInputEl = $state(null);

  function handleKeydown(ev) {
    // Never intercept shortcuts when typing in an input/textarea.
    const tag = /** @type {HTMLElement} */ (ev.target).tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    if (ev.key === 'Escape') {
      if (ctxMenu) { closeContextMenu(); return; }
      if (optionPickerUnderlying) { closeOptionPicker(); return; }
      // Only consume Esc for an actually-rendered typeahead (open AND
      // non-empty). With typeaheadOpen-on-focus + empty list, the old
      // guard swallowed Esc when there was nothing visible to close.
      if (typeaheadOpen && typeahead.length) { typeaheadOpen = false; return; }
      if (searchOpen) { closeSearch(); return; }
      ticketProps = null;
      return;
    }
    if (ev.key === '/') {
      ev.preventDefault();
      openSearch();
      return;
    }
    if (ev.key === 'j' || ev.key === 'k') {
      // j/k navigation retired in the 6-grid refactor — focus would
      // need a "current grid" tracker to walk rows across instances.
      // Operator can still use ag-Grid's native arrow-key nav inside
      // whichever grid has focus.
      if (false) {
      ev.preventDefault();
      const api = /** @type {any} */ (null);
      // Attempt to move selection.
      try {
        const focused = api.getFocusedCell();
        if (!focused) {
          api.setFocusedCell(0, 'tradingsymbol');
        } else {
          const next = focused.rowIndex + (ev.key === 'j' ? 1 : -1);
          if (next >= 0) api.setFocusedCell(next, focused.column);
        }
      } catch (_) {}
      }  // end if(false) — j/k handler suppressed for 6-grid layout
    }
  }

  // Column-state persistence retired in the 6-grid refactor — the
  // single-grid getColumnState/applyColumnState pattern doesn't map
  // cleanly when there are 6 separate ag-Grid instances each with a
  // different columnDef shape (left-style vs right-style). Operators
  // can still sort + resize columns in-session; the values just
  // don't carry across reloads. If we need cross-session persistence
  // back, the new shape would be a per-grid map: {bucket: state}.

  // ── Per-source subtotals (item 1) ─────────────────────────────────
  const subtotals = $derived.by(() => {
    const rows = unifiedRows;
    let hDayPnl = 0, hPnl = 0, hCurVal = 0, hCount = 0;
    let pDayPnl = 0, pPnl = 0, pCount = 0;
    let mCount = 0, mTopSym = '', mTopPct = /** @type {number|null} */ (null);

    for (const r of rows) {
      if (r.src?.h) {
        hDayPnl += Number(r.day_pnl) || 0;
        hPnl    += Number(r.pnl)     || 0;
        // Cur Val: qty_hold × ltp
        const cv = (Number(r.qty_hold) || 0) * (Number(r.ltp) || 0);
        hCurVal += cv;
        hCount++;
      }
      if (r.src?.p) {
        pDayPnl += Number(r.day_pnl) || 0;
        pPnl    += Number(r.pnl)     || 0;
        pCount++;
      }
      if (r.src?.m) {
        mCount++;
        const pct = r._mover_change_pct;
        if (pct != null && (mTopPct == null || Math.abs(pct) > Math.abs(mTopPct))) {
          mTopPct = pct;
          mTopSym = r.tradingsymbol || '';
        }
      }
    }
    return { hDayPnl, hPnl, hCurVal, hCount, pDayPnl, pPnl, pCount, mCount, mTopSym, mTopPct };
  });

  const hasSubtotals = $derived(
    subtotals.hCount > 0 || subtotals.pCount > 0 || subtotals.mCount > 0
  );
</script>

<div bind:this={pulseWrapper}
     class={flat ? 'mp-flat-wrap' : 'algo-status-card p-1.5'}
     data-status="inactive">

  {#if error}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}
  {#if brokerErr}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">
      Broker feed unavailable — <span class="font-mono">{brokerErr}</span>. Strip + Day P&L stale until recovery.
    </div>
  {/if}

  <!-- Chrome row retired — Show dropdown removed, + moved into the
       Pinned/Watchlist card header, refresh moved into each card's
       header (after the CollapseButton). -->


  <!-- Per-source subtotals strip retired per operator request — the
       same numbers (Holdings / Positions / Movers totals + top
       mover) are now visible inside each bucket card's grid + tab
       counts, so the strip duplicated information that's already
       on-screen. -->


  <!-- Option picker is rendered as a modal at the BOTTOM of this
       component (search-overlay style) so it sits above all other
       page chrome. The inline-row version was disorienting because
       it appeared below the chrome row AFTER the Add popup closed,
       leaving the operator with no clear visual continuity. -->

  <!-- Operator: 'summary, fund balances, positions/holdings is the
       sequence.' Summary leads (the at-a-glance per-account P&L
       answer), Funds follows as shared cash/margin context, and the
       symbol grids below (when showSymbolsGrid is true) carry the
       per-row drill-down. -->
  {#if showSummary}
    <!-- Positions Summary — per-account Day P&L + P&L. -->
    <div class="mp-section-label">Positions Summary</div>
    <div bind:this={positionsSummaryEl} class="ag-theme-quartz ag-theme-algo summary-grid mb-2"></div>
    <!-- Holdings Summary — per-account Day P&L + P&L + Cur Val. -->
    <div class="mp-section-label">Holdings Summary</div>
    <div bind:this={holdingsSummaryEl} class="ag-theme-quartz ag-theme-algo summary-grid mb-2"></div>
  {/if}

  {#if showFunds}
    <!-- Funds strip — per-account Cash / Avail Margin / Used Margin /
         Collateral. Shared cash/margin context between Positions
         and Holdings summaries above. -->
    <div class="mp-section-label">Funds</div>
    <div bind:this={fundsEl} class="ag-theme-quartz ag-theme-algo funds-grid mb-2"></div>
  {/if}

  {#if showSymbolsGrid}
    <!-- Slice 7g — strategy filter chip lives above the grids,
         next to the Symbols section label. Hides itself when no
         strategies exist. Active pick narrows Positions + Holdings
         grids on the right to symbols with open lots in that
         strategy. Other grids (Pinned / Watchlist / Movers) are
         intentionally unaffected — they're book-wide market scan
         surfaces, not per-strategy. -->
    {#if showSummary || showFunds}
      <div class="mp-section-label mp-section-with-picker">
        <span>Symbols</span>
        <StrategyPicker label="Strategy" />
      </div>
    {:else}
      <div class="mp-section-with-picker mp-picker-standalone">
        <StrategyPicker label="Strategy" />
      </div>
    {/if}
    <!--
      Desktop ≥lg: two side-by-side grids inside a flex row.
      Mobile <lg: same divs stack vertically (column).
      Each grid scrolls independently — viewport whitespace on
      wide displays goes to the right grid rather than padding
      the unified grid's empty right margin.
        Left  → Pinned (pinned-top strip) · Watchlist · Movers
        Right → Positions · Holdings (each with a TOTAL pinned at top)
      Mobile single-column reading order: left content first,
      then right (so Positions / Holdings appear after the
      watchlist content on phones — same scan order as the
      original unified grid).
    -->
    <!--
      Two-column layout — desktop puts monitoring surfaces on the
      LEFT (Pinned · Watchlist · Winners · Losers, stacked) and the
      operator's book on the RIGHT (Account picker · Positions ·
      Holdings, stacked). Mobile collapses to a single column with
      the natural reading order (left col items first, then right).
      Each .mp-bucket-wrap carries its own header label + the
      ag-Grid container.
    -->
    <div class="mp-layout">
      <div class="mp-col mp-col-left">
        <!--
          Pinned + Watchlist merged into ONE tabbed card. Top tab
          strip picks the visible feed (Pinned ★ or Watchlist N).
          The + button anchors immediately after the top tabs
          (operator: "+ before expand/contract"). Two ag-Grid
          containers always live in the DOM so bind:this lands at
          mount time; CSS hides the inactive one via display:none.
          ag-Grid's ResizeObserver re-measures the active grid
          when the operator flips tabs.
        -->
        <section class="mp-bucket-wrap mp-bucket-pinwatch"
                 class:is-collapsed={_effColPinWatch}
                 class:fs-card-on={_fsPinWatch}
                 style="--bucket-rows:{_bRowsPinWatch}">
          <div class="mp-bucket-head">
            <!-- Top-tab strip. Pinned (Default + Markets merged feed)
                 lives on the left; each operator-created watchlist is
                 its own peer tab to the right, labelled with the list
                 name directly. No generic "Watchlist" wrapper or
                 sub-tabs — operator request: "there should not be any
                 watchlist tab. it should be the tab I have created".
                 AlgoTabs supplies the canonical underline-on-active
                 decoration shared with every other tab strip on the
                 site. Per-tab color: amber for Pinned, sky for each
                 user watchlist so the operator can still distinguish
                 the two feed families at a glance. Watchlist ids are
                 string-encoded as `wl:<id>` since AlgoTabs uses string
                 ids; onChange decodes back to the number/'pinned'
                 union the rest of MarketPulse expects. -->
            <AlgoTabs
              tabs={[
                { id: 'pinned', label: 'Pinned', color: /** @type {const} */ ('amber') },
                ..._userLists.map(l => ({ id: `wl:${l.id}`, label: l.name, color: /** @type {const} */ ('sky') }))
              ]}
              value={topTab === 'pinned' ? 'pinned' : `wl:${topTab}`}
              onChange={(id) => { topTab = id === 'pinned' ? 'pinned' : Number(id.slice(3)); }}
              compact={true}
            />
            <!-- Watchlist manage button — opens the Add-to-Pulse modal
                 which handles every list-level operation: add a symbol,
                 create a new watchlist, rename existing, delete it,
                 add/remove items. Earlier "+" glyph only conveyed
                 "add" but the modal does much more — switched to a
                 pencil-edits-list glyph (horizontal lines + pencil)
                 which reads as "manage list". Same shortcut (/). -->
            <button onclick={openSearch}
                    title="Manage watchlists — add / rename / delete (/)"
                    aria-label="Manage watchlists"
                    class="mp-add-btn">
              <svg width="14" height="14" viewBox="0 0 16 16"
                   fill="none" stroke="currentColor" stroke-width="1.5"
                   stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">
                <!-- list lines on the left -->
                <path d="M2.5 5h5M2.5 8h5M2.5 11h3.5" />
                <!-- pencil overlaid on the right -->
                <path d="M11 3l2 2L8 10l-2.3 0.6L6.4 8.3L11 3z" />
                <path d="M9.7 4.3l2 2" />
              </svg>
            </button>
            <span class="mp-bucket-head-spacer"></span>
            <CardControls
              bind:isCollapsed={_colPinWatch}
              bind:isFullscreen={_fsPinWatch}
              bind:filter={_filterPinWatch}
              cardId="pulse-pinwatch"
              label="Pinned/Watchlist"
              onRefresh={refreshAllNow}
              refreshLoading={_refreshing}
            />
          </div>
          <!-- Sub-tab strip retired — each user watchlist now lives
               as its own top-tab next to Pinned. -->

          <!-- Both ag-Grid containers stay in the DOM at all times so
               bind:this lands before mountGrid() runs. CSS hides the
               inactive one — switching tabs is a paint-only flip,
               no remount. -->
          <div bind:this={gridPinnedEl}
               class="ag-theme-quartz ag-theme-algo bucket-grid"
               class:mp-grid-hidden={topTab !== 'pinned'}></div>
          <div bind:this={gridWatchEl}
               class="ag-theme-quartz ag-theme-algo bucket-grid"
               class:mp-grid-hidden={typeof topTab !== 'number'}></div>
        </section>
        {#if showWinners}
          <section class="mp-bucket-wrap mp-bucket-winners"
                   class:is-collapsed={_effColWinners}
                   class:is-empty={_winnersTotal === 0}
                   class:fs-card-on={_fsWinners}
                   style="--bucket-rows:{_bRowsWinners}">
            <div class="mp-bucket-head">
              <span class="mp-bucket-label mp-bucket-label-winners">Winners</span>
              <div class="mp-head-tabs">
                <AlgoTabs
                  tabs={MOVER_TABS.map(t => ({ id: t, label: MOVER_TAB_LABEL[t] }))}
                  value={winTab}
                  onChange={(id) => { winTab = /** @type {MoverTab} */ (id); }}
                  compact={true}
                />
              </div>
              <span class="mp-bucket-head-spacer"></span>
              <CardControls
                bind:isCollapsed={_colWinners}
                bind:isFullscreen={_fsWinners}
                bind:filter={_filterWinners}
                cardId="pulse-winners"
                label="Winners"
                onRefresh={refreshAllNow}
                refreshLoading={_refreshing}
              />
            </div>
            <div bind:this={gridWinEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
          </section>
        {/if}
        {#if showLosers}
          <section class="mp-bucket-wrap mp-bucket-losers"
                   class:is-collapsed={_effColLosers}
                   class:is-empty={_losersTotal === 0}
                   class:fs-card-on={_fsLosers}
                   style="--bucket-rows:{_bRowsLosers}">
            <div class="mp-bucket-head">
              <span class="mp-bucket-label mp-bucket-label-losers">Losers</span>
              <div class="mp-head-tabs">
                <AlgoTabs
                  tabs={MOVER_TABS.map(t => ({ id: t, label: MOVER_TAB_LABEL[t] }))}
                  value={loseTab}
                  onChange={(id) => { loseTab = /** @type {MoverTab} */ (id); }}
                  compact={true}
                />
              </div>
              <span class="mp-bucket-head-spacer"></span>
              <CardControls
                bind:isCollapsed={_colLosers}
                bind:isFullscreen={_fsLosers}
                bind:filter={_filterLosers}
                cardId="pulse-losers"
                label="Losers"
                onRefresh={refreshAllNow}
                refreshLoading={_refreshing}
              />
            </div>
            <div bind:this={gridLoseEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
          </section>
        {/if}
      </div>

      <div class="mp-col mp-col-right">
        <section class="mp-bucket-wrap mp-bucket-positions"
                 class:is-collapsed={_effColPositions}
                 class:fs-card-on={_fsPositions}
                 style="--bucket-rows:{_bRowsPositions}">
          <div class="mp-bucket-head">
            <span class="mp-bucket-label mp-bucket-label-positions">Positions</span>
            {#if accountPicker && availableAccounts.length > 0}
              <!-- Per-card Account picker. Positions and Holdings each
                   carry their own filter so an operator can scope
                   Positions to ZG#### (intraday) while Holdings
                   tracks ZJ#### (long-term) independently. Left-anchored
                   so it reads as part of the card's identity strip
                   (label + filter), not part of the controls cluster. -->
              <div class="mp-head-acct">
                <AccountMultiSelect bind:value={positionsAccounts}
                  options={availableAccounts.map(a => ({ value: a, label: a }))}
                  ariaLabel="Filter Positions by broker account" />
              </div>
            {/if}
            <span class="mp-bucket-head-spacer"></span>
            <CardControls
              bind:isCollapsed={_colPositions}
              bind:isFullscreen={_fsPositions}
              bind:filter={_filterPositions}
              cardId="pulse-positions"
              label="Positions"
              onRefresh={refreshAllNow}
              refreshLoading={_refreshing}
            />
          </div>
          <div bind:this={gridPositionsEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
        </section>
        <section class="mp-bucket-wrap mp-bucket-holdings"
                 class:is-collapsed={_effColHoldings}
                 class:fs-card-on={_fsHoldings}
                 style="--bucket-rows:{_bRowsHoldings}">
          <div class="mp-bucket-head">
            <span class="mp-bucket-label mp-bucket-label-holdings">Holdings</span>
            {#if accountPicker && availableAccounts.length > 0}
              <div class="mp-head-acct">
                <AccountMultiSelect bind:value={holdingsAccounts}
                  options={availableAccounts.map(a => ({ value: a, label: a }))}
                  ariaLabel="Filter Holdings by broker account" />
              </div>
            {/if}
            <span class="mp-bucket-head-spacer"></span>
            <CardControls
              bind:isCollapsed={_colHoldings}
              bind:isFullscreen={_fsHoldings}
              bind:filter={_filterHoldings}
              cardId="pulse-holdings"
              label="Holdings"
              onRefresh={refreshAllNow}
              refreshLoading={_refreshing}
            />
          </div>
          <div bind:this={gridHoldingsEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
        </section>
      </div>
    </div>
  {/if}
</div>

{#if allowOrders && ticketProps}
  <SymbolPanel
    {...ticketProps}
    onSubmit={() => closeTicket()}
    onClose={closeTicket}
    onAddToWatchlist={async (sym, exch) => {
      // Adds the symbol to whichever watchlist the operator currently
      // has focused (or the first active one). Surfaces in the panel
      // header as a `+W` button. Useful when the operator opens
      // SymbolPanel for a contract they don't yet own — e.g. an
      // option strike from a chain pick on /admin/options that
      // they want to track here too.
      const targetId = focusedListId ?? [...activeIds][0];
      if (!targetId) throw new Error('No active watchlist');
      await addToWatchlistDeduped(targetId, sym, exch || 'NFO');
      await loadActive();
    }} />
{/if}

<!-- Context menu (item 2) -->
{#if ctxMenu}
  <div
    bind:this={ctxMenuEl}
    class="ctx-menu"
    style="left:{ctxMenu.x}px;top:{ctxMenu.y}px"
    role="menu">
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenChart(ctxMenu.row)}>Chart →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenOptions(ctxMenu.row)}>Open in Options →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenTicket(ctxMenu.row)}>Place order →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenOrders(ctxMenu.row)}>Orders →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenLog(ctxMenu.row)}>Log →</button>
    <div class="ctx-sep"></div>
    {#if !ctxMenu.row?.src?.w}
      <!-- ★ Add to watchlist — visible when the symbol is NOT already
           in the operator's watchlist. The other branch below shows
           the Remove counterpart. -->
      <button class="ctx-item" role="menuitem" onclick={() => ctxAddWatch(ctxMenu.row)}>★ Add to watchlist</button>
    {/if}
    <button class="ctx-item" role="menuitem" onclick={() => ctxCopySymbol(ctxMenu.row)}>Copy symbol</button>
    <div class="ctx-sep"></div>
    {#if isDetached(ctxMenu.row?.tradingsymbol)}
      <button class="ctx-item" role="menuitem" onclick={() => { reattachSymbol(ctxMenu.row); closeContextMenu(); }}>↩ Re-attach to group</button>
    {:else if ctxMenu.row?.underlying}
      <button class="ctx-item" role="menuitem" onclick={() => { detachSymbol(ctxMenu.row); closeContextMenu(); }}>↗ Detach from group</button>
    {/if}
    {#if hasOverrides}
      <button class="ctx-item" role="menuitem" onclick={() => { resetOverrides(); closeContextMenu(); }}>↻ Reset all overrides</button>
    {/if}
    {#if ctxMenu.row?.src?.w && ctxMenu.row?.watchlist_item_id != null}
      <div class="ctx-sep"></div>
      <button class="ctx-item ctx-item-danger" onclick={() => ctxRemoveWatch(ctxMenu.row)}>Remove from watchlist</button>
    {/if}
  </div>
{/if}

<!-- ActivityLogModal singleton lives in the (algo) layout via the
     activityModal store — ctxOpenLog above opens it via the store. -->


{#if _chartModalOpen}
  <ChartModal
    symbol={_chartModalSym}
    exchange={_chartModalExch}
    onClose={_closeChartModal}
  />
{/if}

<!-- Unified Add popup — opened by the `+` button in the chrome row (or
     the `/` keyboard shortcut). Single modal: pick a target watchlist
     (default / existing / new), then type a symbol + pick its type
     (EQ / FU / CE / PE), then Add. Click-outside / Esc to dismiss. -->
{#if searchOpen}
  <div class="search-overlay" role="dialog" aria-modal="true"
       aria-label="Add to Pulse" tabindex="-1"
       onclick={closeSearch}
       onkeydown={(e) => { if (e.key === 'Escape') closeSearch(); }}>
    <div class="search-modal" role="presentation" onclick={(e) => e.stopPropagation()}>
      <div class="search-header">
        <span class="search-title">Manage watchlists</span>
        <button type="button" class="search-close" title="Close" aria-label="Close" onclick={closeSearch}>×</button>
      </div>
      <div class="search-body">
        <!-- Watchlist target — Default ★ pre-selected; "+ New watchlist"
             reveals an inline name input which is created on Add. The
             trailing × button deletes the currently-selected list
             (disabled for the Default list and the "+ New" sentinel). -->
        <div class="mp-add-section-label">Watchlist</div>
        <div class="search-row">
          <div class="flex-1">
            <Select ariaLabel="Watchlist" bind:value={targetListId}
              options={[
                ...lists.map(l => ({
                  value: l.id,
                  label: l.is_default ? `${l.name} ★` : l.name,
                })),
                { value: 'NEW', label: '+ New watchlist' },
              ]} />
          </div>
          {#if typeof targetListId === 'number'}
            {@const _tgtList = lists.find(l => l.id === targetListId)}
            <!-- Show the Rename / Delete affordances for operator-
                 created lists only. The shared global Pinned is the
                 canonical always-present list — its name is fixed and
                 it can't be deleted (would leave every user without a
                 pinned list). Designated users can still add / remove
                 ITEMS on it via the symbol picker + per-row × glyph;
                 only the list-level rename / delete is locked out. -->
            {#if _tgtList && !_tgtList.is_global}
              <!-- ✎ Rename — reveals the inline name input row below
                   so the operator can edit the watchlist's name without
                   leaving the popup. -->
              <button type="button"
                onclick={(e) => {
                  e.preventDefault();
                  const id = /** @type {number} */ (targetListId);
                  if (_renameId === id) { cancelRename(); return; }
                  _renameId = id;
                  _renameName = _tgtList.name;
                  _renameError = '';
                  _pendingDeleteId = null;
                }}
                class="text-[0.7rem] py-1 px-3 rounded font-bold border"
                style="background: rgba(56,189,248,0.2); color: #7dd3fc; border-color: rgba(56,189,248,0.55);"
                title={_renameId === targetListId ? 'Cancel rename' : `Rename "${_tgtList.name}" watchlist`}>
                {_renameId === targetListId ? '× Cancel' : '✎ Rename'}
              </button>
              <button type="button"
                onclick={async (e) => {
                  e.preventDefault();
                  // Single-click delete (operator picked the list +
                  // clicked Delete inside the Manage popup — that's
                  // confirmation enough). The earlier two-click
                  // pattern confused operators ("when I delete test
                  // watchlist it is not getting deleted" — they
                  // missed the 4-second confirm window).
                  const id = /** @type {number} */ (targetListId);
                  try {
                    await dropList(id);
                    closeSearch();
                  } catch (err) {
                    // Surface the failure inline so the operator sees
                    // why nothing happened (auth lapse, 403 on Pinned,
                    // network drop, etc.) instead of a silent no-op.
                    _renameError = (err && err.message) || 'Delete failed.';
                  }
                }}
                class="text-[0.7rem] py-1 px-3 rounded font-bold border"
                style="background: rgba(248,113,113,0.2); color: #f87171; border-color: rgba(248,113,113,0.55);"
                title={`Delete "${_tgtList.name}" watchlist`}>
                🗑 Delete
              </button>
            {/if}
          {/if}
        </div>
        {#if _renameId !== null && _renameId === targetListId}
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={_renameName}
              onkeydown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
                else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="New name" autocomplete="off" />
            <button type="button" onclick={commitRename}
              disabled={!_renameName.trim()}
              class="btn-primary text-[0.7rem] py-1 px-3 disabled:opacity-50">Save</button>
          </div>
          {#if _renameError}
            <div class="search-hint" style="color:#f87171">{_renameError}</div>
          {:else}
            <div class="search-hint">Enter to save · Esc to cancel · names are case-insensitive and must be unique.</div>
          {/if}
        {/if}
        {#if targetListId === 'NEW'}
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={newListName}
              onkeydown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault();
                  if (typeaheadOpen && typeahead.length) { typeaheadOpen = false; }
                  else { closeSearch(); }
                }
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="New watchlist name" autocomplete="off" />
          </div>
          <div class="search-hint">
            Names are case-insensitive and must be unique. The list is created when you press Add.
          </div>
        {:else if typeof targetListId === 'number'}
          {@const _tgtCheck = lists.find(l => l.id === targetListId)}
          {#if _tgtCheck && !_tgtCheck.is_default}
            <div class="search-hint">
              Pick a different list to switch target. Click 🗑 Delete to remove "{_tgtCheck.name}".
            </div>
          {/if}
        {/if}

        <div class="mp-add-divider"></div>

        <!-- Symbol + Type. The two-letter type picker after the symbol
             input lets the operator disambiguate equity vs derivative
             without picking a raw exchange code (EQ/FU/CE/PE → NSE/NFO
             internally). Typeahead picks override the type from the
             matched instrument's tradingsymbol suffix. -->
        <div class="mp-add-section-label">Add symbol</div>
        <div class="search-row">
          <input bind:this={symInputEl} bind:value={symInput}
            oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
            onfocus={() => typeaheadOpen = true}
            onkeydown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                if (typeaheadOpen && typeahead.length && symInput.trim()) pickFromTypeahead(typeahead[0]);
                else addRow();
              } else if (e.key === 'Escape') {
                // First Esc closes the typeahead suggestions if they're
                // ACTUALLY rendered (typeaheadOpen AND non-empty); a
                // second Esc closes the popup. When the dropdown is
                // invisible — onfocus sets typeaheadOpen=true even when
                // typeahead is [] — first Esc would otherwise no-op,
                // forcing operators to press Esc twice to dismiss.
                e.preventDefault();
                if (typeaheadOpen && typeahead.length) { typeaheadOpen = false; }
                else { closeSearch(); }
              }
            }}
            class="field-input text-[0.7rem] py-1 px-2 flex-1"
            placeholder="Symbol (≥ 3 chars) — stocks, futures, options" autocomplete="off" />
          <div class="w-16">
            <Select ariaLabel="Type" bind:value={typeInput}
              options={[
                { value: 'EQ', label: 'EQ' },
                { value: 'FU', label: 'FU' },
                { value: 'CE', label: 'CE' },
                { value: 'PE', label: 'PE' },
              ]} />
          </div>
          <button onclick={addRow}
            disabled={!symInput.trim() || (targetListId === 'NEW' && !newListName.trim())}
            class="btn-primary text-[0.7rem] py-1 px-3 disabled:opacity-50"
            title="Add to target watchlist">Add</button>
        </div>
        <!-- Optional display name (alias). Lets the operator label a
             contract by its underlying nickname — e.g. type "Crude oil"
             for CRUDEOIL26JUNFUT. Empty leaves the grid showing the
             raw tradingsymbol; non-empty replaces the symbol cell with
             the alias (and the tradingsymbol moves to the tooltip). -->
        <div class="search-row" style="margin-top: 0.4rem;">
          <input bind:value={aliasInput}
            onkeydown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addRow(); }
              else if (e.key === 'Escape') { e.preventDefault(); closeSearch(); }
            }}
            class="field-input text-[0.7rem] py-1 px-2 flex-1"
            placeholder="Display name (optional) — e.g. Crude oil"
            autocomplete="off" />
        </div>
        {#if typeahead.length}
          <div class="search-typeahead">
            {#each typeahead as inst}
              <button onclick={() => pickFromTypeahead(inst)}
                class="search-typeahead-item">
                <span class="font-mono text-[#fbbf24]">{inst.s}</span>
                <span class="text-[0.6rem] text-[#7e97b8] ml-2">{inst.e}</span>
              </button>
            {/each}
          </div>
        {/if}
        <div class="search-hint">
          Type ≥ 3 characters · Enter picks the first match · F&amp;O underlyings open the option chain picker
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- Option-chain picker modal — opens when the operator picks an F&O
     underlying from the Add popup's typeahead. Previously this rendered
     as an inline row below the page chrome which felt jarring; modal
     form keeps the Add flow visually contiguous. Click-overlay / Esc
     to dismiss; targets the watchlist chosen in the Add popup via
     _resolveTargetListId. -->
{#if optionPickerUnderlying}
  <div class="search-overlay" role="dialog" aria-modal="true"
       aria-label="Pick option strike" tabindex="-1"
       onclick={closeOptionPicker}
       onkeydown={(e) => { if (e.key === 'Escape') closeOptionPicker(); }}>
    <div class="search-modal" role="presentation" onclick={(e) => e.stopPropagation()}>
      <div class="search-header">
        <span class="search-title">{optionPickerUnderlying.name} — pick contract</span>
        <button type="button" class="search-close" title="Close" aria-label="Close" onclick={closeOptionPicker}>×</button>
      </div>
      <div class="search-body">
        <!-- Watchlist target — same dropdown as the Add popup. Lets the
             operator change target without backing out to the prior
             modal. "+ New watchlist" reveals an inline name input. -->
        <div class="mp-add-section-label">Watchlist</div>
        <div class="search-row">
          <div class="flex-1">
            <Select ariaLabel="Watchlist target" bind:value={targetListId}
              options={[
                ...lists.map(l => ({
                  value: l.id,
                  label: l.is_default ? `${l.name} ★` : l.name,
                })),
                { value: 'NEW', label: '+ New watchlist' },
              ]} />
          </div>
        </div>
        {#if targetListId === 'NEW'}
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={newListName}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="New watchlist name" autocomplete="off" />
          </div>
        {/if}

        <div class="mp-add-divider"></div>

        <div class="mp-add-section-label">Option chain</div>
        <div class="search-row" style="flex-wrap: wrap;">
          <!-- Expiry dropdown -->
          <div class="w-28">
            <Select ariaLabel="Expiry" bind:value={optionPickerExpiry}
              options={optionPickerExpiries.map(exp => ({ value: exp, label: exp }))} />
          </div>
          <!-- CE / PE toggle -->
          <span class="flex rounded overflow-hidden border border-[#fbbf24]/25">
            <button type="button"
              onclick={() => optionPickerSide = 'CE'}
              class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                     {optionPickerSide === 'CE'
                       ? 'bg-[#fbbf24] text-[#0a1628]'
                       : 'text-[#7e97b8] hover:bg-[#fbbf24]/10'}">CE</button>
            <button type="button"
              onclick={() => optionPickerSide = 'PE'}
              class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                     {optionPickerSide === 'PE'
                       ? 'bg-[#fbbf24] text-[#0a1628]'
                       : 'text-[#7e97b8] hover:bg-[#fbbf24]/10'}">PE</button>
          </span>
          <!-- Strike dropdown -->
          <div class="w-28">
            <Select ariaLabel="Strike" bind:value={optionPickerStrike}
              disabled={!optionPickerStrikes.length}
              options={optionPickerStrikes.map(k => ({ value: k, label: String(k) }))} />
          </div>
          <button type="button"
            onclick={addOptionFromPicker}
            disabled={optionPickerStrike == null || !optionPickerExpiry}
            class="btn-primary text-[0.7rem] py-1 px-3 disabled:opacity-50"
            title="Add this contract to the target watchlist">Add</button>
        </div>

        <div class="mp-add-divider"></div>

        <div class="mp-add-section-label">{_underlyingHasEquity ? 'Equity (spot)' : 'Spot index'}</div>
        <div class="search-row">
          <button type="button"
            onclick={addSpotFromPicker}
            title={_underlyingHasEquity ? 'Add the underlying NSE equity' : 'Add the spot index'}
            class="btn-primary text-[0.7rem] py-1 px-3">
            {_underlyingHasEquity ? `Add ${optionPickerUnderlying.name} (NSE)` : 'Add Spot'}
          </button>
        </div>
        <div class="search-hint">
          The contract is added to the watchlist chosen in the previous step.
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Mobile touch-target — WCAG 2.5.8 minimum 24px; aim for 36px on
     phones. !important is required to beat ag-Grid's inline row-height
     style (set via rowHeight: 26 in the grid options). Applies
     uniformly to BOTH grid columns (left + right; all use
     .ag-theme-algo). Desktop (>720px) honors the rowHeight: 26
     setting normally. Slice AS audit clarification — the override
     is intentional, not a bug. */
  @media (max-width: 720px) {
    :global(.ag-theme-algo .ag-row) { min-height: 36px !important; }
  }

  /* Symbol cell — main + alias. */
  :global(.sym-main)  { color: #e2e8f0; font-weight: 600; }
  /* CE = green (right to BUY = bullish), PE = red (right to SELL =
     bearish). Sensibull / Streak convention. Operator scanning
     positions tells calls from puts at a glance. */
  :global(.sym-main.sym-ce) { color: #4ade80; }
  :global(.sym-main.sym-pe) { color: #f87171; }
  :global(.sym-alias) { color: var(--algo-muted); font-size: 0.55rem; }

  /* Source badges (P / H / W / U) — sit right of the symbol. */
  :global(.sym-badges) {
    display: inline-flex;
    gap: 2px;
    margin-left: 2px;
    vertical-align: middle;
  }
  :global(.sym-badge) {
    display: inline-block;
    padding: 0 3px;
    font-size: 0.5rem;
    font-weight: 700;
    line-height: 12px;
    border-radius: 2px;
    font-variant-numeric: tabular-nums;
  }
  :global(.badge-p) { color: #7dd3fc; background: var(--algo-sky-bg);   }
  :global(.badge-h) { color: #4ade80; background: var(--algo-green-bg); }
  :global(.badge-w) { color: #fbbf24; background: var(--algo-amber-bg); }
  :global(.badge-u) { color: #c084fc; background: rgba(192,132,252,0.14); }
  :global(.badge-m-pos) { color: #4ade80; background: var(--algo-green-bg); }
  :global(.badge-m-neg) { color: #f87171; background: var(--algo-red-bg);   }
  /* Covered-call lot-count badge — green pill with the number of whole
     lots the operator holds. Same pill family as the H/P/W/U badges so
     the row's badge strip reads as one consistent set. Bolder
     background than the others to draw the operator's eye to the
     actionable column (the rest are informational tags). */
  :global(.badge-fno-lot) {
    color: #052e16;
    background: #4ade80;
    font-weight: 800;
    /* Standalone — sits between sym-main and the sym-badges group, so
       it needs its own breathing room (the group's `margin-left: 2px`
       doesn't apply since the chip lives outside the group). */
    margin-left: 4px;
    vertical-align: middle;
  }
  /* Amber variant — the underlying already has an open derivative
     position so the operator should think twice before writing more.
     Uses the same amber as `.badge-w` watchlist chip so the colour
     family stays consistent. Dark text for contrast against the
     bright fill. */
  :global(.badge-fno-lot-pos) {
    color: #422006;
    background: #fbbf24;
  }

  /* Inline remove-from-watchlist button. */
  :global(.sym-remove) {
    display: inline-block;
    margin-left: 6px;
    padding: 0 4px;
    color: rgba(248,113,113,0.45);
    font-size: 0.7rem;
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-remove:hover) {
    color: #f87171;
    background: rgba(248,113,113,0.12);
  }
  /* Compositor-thread :active so mobile / touch clicks feel
     responsive even when the main thread is busy. ag-Grid cell
     renderers don't inherit from the global button rules in app.css,
     so the press-feedback is scoped here. touch-action keeps the
     ~300ms double-tap-zoom delay off. */
  :global(.sym-remove:active) {
    transform: scale(0.92);
    background: rgba(248,113,113,0.18);
  }
  :global(.sym-remove) { touch-action: manipulation; }

  /* ⋯ symbol-actions button — sibling of .sym-remove. Routes click
     through openContextMenu() so the existing right-click menu also
     opens on left-click of this affordance. */
  :global(.sym-actions) {
    display: inline-block;
    margin-left: 4px;
    padding: 0 4px;
    color: rgba(126,151,184,0.55);
    font-size: 0.75rem;
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-actions:hover) {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
  }
  :global(.sym-actions:active) {
    transform: scale(0.92);
    background: rgba(251,191,36,0.18);
  }
  :global(.sym-actions) { touch-action: manipulation; }

  /* ▲ / ▼ group-move buttons. Hidden by default, revealed on row
     hover so the icons don't compete with the symbol/badge content
     in the resting state. */
  :global(.sym-move) {
    display: inline-block;
    margin-left: 2px;
    padding: 0 3px;
    color: rgba(126,151,184,0.45);
    font-size: 0.55rem;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    opacity: 0;
    transition: opacity 0.12s ease, color 0.12s ease, background 0.12s ease;
  }
  :global(.ag-row:hover .sym-move) { opacity: 1; }
  :global(.sym-move:hover) {
    color: #fbbf24;
    background: rgba(251,191,36,0.12);
  }

  /* Mobile / touch screens — hover doesn't trigger reliably, so the
     ▲/▼ buttons stay visible at low opacity by default. The ⋯ menu
     trigger is already always-visible. */
  @media (hover: none), (max-width: 768px) {
    :global(.sym-move) { opacity: 0.55; }
    :global(.sym-move:active) {
      color: #fbbf24;
      background: rgba(251,191,36,0.18);
    }
  }

  /* Day Δ / P&L cells. */
  :global(.cell-pos)  { color: #4ade80 !important; }
  :global(.cell-neg)  { color: #f87171 !important; }
  :global(.cell-flat) { color: #94a3b8 !important; }
  /* P&L cell background tint — same colour family + same alphas as the
     /admin/derivatives Candidates panel (`.cand-pnl.cell-pos` etc.) so
     the two surfaces' P&L columns read with the same visual identity.
     Applied via the `mp-pnl-cell` marker class on Pulse's right-grid
     P&L / Day P&L / P&L % / Day % columns + the summary grids. */
  :global(.mp-pnl-cell.cell-pos)  { background-color: rgba(74,222,128,0.10) !important; }
  :global(.mp-pnl-cell.cell-neg)  { background-color: rgba(248,113,113,0.10) !important; }
  :global(.mp-pnl-cell.cell-flat) { background-color: rgba(148,163,184,0.08) !important; }
  :global(.cell-muted){ color: rgba(200,216,240,0.55) !important; }

  /* Pinned sub-group dividers — first row of each pinned category
     (idx / fx / commodity) carries `.pin-divider` so the three
     mini-sections at the top of the grid feel distinct. Thin amber
     top-border, no other visual change to row contents. The FIRST
     idx row (very first row in the grid) skips the divider since
     there's nothing above it to separate from. */
  :global(.ag-theme-algo .ag-row.pin-divider) {
    border-top: 1px solid rgba(251,191,36,0.30);
  }
  :global(.ag-theme-algo .ag-row.pin-divider.pin-cat-idx:first-of-type) {
    border-top: none;
  }

  /* Horizontal section dividers across EVERY bucket grid — unified
     thin amber 30 % alpha so the five grids (Pinned/Watchlist,
     Winners, Losers, Positions, Holdings) carry the same horizontal
     border thickness + colour as the pin-divider on the pinned grid.
     Operator gets one visual idiom for "section break" instead of
     a different colour per grid type. Background tints on the row
     still differentiate (positions sky / holdings green / movers
     violet etc), so the section identity is encoded in the FILL,
     not the divider. */
  :global(.ag-theme-algo .ag-row.major-divider),
  :global(.ag-theme-algo .ag-row.mover-direction-divider),
  :global(.ag-theme-algo .ag-row.mover-group-divider) {
    border-top: 1px solid var(--algo-amber-border-soft);
    box-shadow: none;
  }
  /* Per-row left-edge accent on the movers sub-groups — kept faint
     so it reads as a section-membership cue without competing with
     the direction tint on pos-long / pos-short rows. Colour scales
     stay (violet/sky/teal) since this is a per-ROW indicator, not
     a per-section divider. */
  :global(.ag-theme-algo .ag-row.mover-underlying .ag-cell:first-child) {
    box-shadow: inset 2px 0 0 rgba(192, 132, 252, 0.35);
  }
  :global(.ag-theme-algo .ag-row.mover-midcap .ag-cell:first-child) {
    box-shadow: inset 2px 0 0 rgba(56, 189, 248, 0.35);
  }
  :global(.ag-theme-algo .ag-row.mover-smallcap .ag-cell:first-child) {
    box-shadow: inset 2px 0 0 rgba(94, 234, 212, 0.35);
  }

  /* Grid containers — each ag-Grid sits inside its own .bucket-grid
     wrapper. domLayout:'normal' (set on createGrid) pairs with a
     fixed height so the header stays pinned at the top of each
     box. With 6 grids on the page, individual heights are tighter
     than the legacy unified grid was — ~260 px lets the operator
     see ~7-8 rows per grid without scrolling, then scroll inside
     for the rest. Width: 100% of the parent grid cell (2-col on
     desktop, full row on mobile). */
  .bucket-grid {
    width: 100%;
    height: 260px;
    min-height: 260px;
    flex: 1 1 auto;
  }
  /* Collapsed card hides the grid body but keeps the header — the
     grid div stays in the DOM (so bind:this lands at mount time and
     ag-Grid can instantiate) but renders as zero-height. When the
     section un-collapses, the grid div springs back to its full
     260 px and ag-Grid resizes on the next setGridOption call. */
  .mp-bucket-wrap.is-collapsed .bucket-grid {
    height: 0 !important;
    min-height: 0 !important;
    overflow: hidden;
  }
  /* Empty-state shrink — when the Winners / Losers bucket has zero
     rows, drop the grid to just enough to fit the column header
     (28 px) + ag-Grid's "No top winners/losers right now." overlay
     message (line-height ~14 px + ~14 px vertical padding) without
     truncating. 4.2 rem ≈ 67 px leaves a comfortable 5-6 px breathing
     room below the message. Operator gets the saved space for the
     next non-empty card; fullscreen rule below still lifts the card
     to the viewport surface when maximised. */
  .mp-bucket-wrap.is-empty:not(.is-collapsed):not(.fs-card-on) .bucket-grid {
    height: 4.2rem !important;
    min-height: 4.2rem !important;
  }
  /* Fullscreen card promotes the bucket-grid to fill the viewport
     minus the card's inset + header height. ag-Grid's ResizeObserver
     re-measures on the next paint so rows fill the new height. */
  .mp-bucket-wrap.fs-card-on .bucket-grid {
    height: calc(100vh - 8rem) !important;
    min-height: 320px !important;
    flex: 1 1 auto;
  }
  @media (max-width: 600px) {
    .mp-bucket-wrap.fs-card-on .bucket-grid {
      height: calc(100vh - 6rem) !important;
    }
  }

  /* Desktop split — two grids side-by-side in a flex row, each
     filling 50% with its own scrollbar. Mobile drops to a column so
     the grids stack one above the other (left on top of right). The
     1024 px breakpoint matches the Tailwind `lg:` cutoff used on
     the algo navbar / hamburger toggle. */
  /* Two-column layout — left col carries monitoring surfaces
     (Pinned · Watchlist · Winners · Losers); right col carries
     the operator's book (Account picker · Positions · Holdings).
     Mobile = single column; desktop ≥lg = side by side. Each
     column is a flex stack so the cards inside flow naturally
     and independently — the left col having 4 cards vs the right
     having 2 doesn't try to align rows across columns. */
  .mp-layout {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    width: 100%;
    /* Grow to fill .mp-flat-wrap so the two-column buckets claim the
       full residual viewport height on desktop. min-height: 0 prevents
       the flex child from sizing to its content (would cause overflow). */
    flex: 1 1 0;
    min-height: 0;
  }
  .mp-col {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    min-width: 0;
  }
  @media (min-width: 1024px) {
    .mp-layout {
      flex-direction: row;
      /* align-items: stretch (default) → right col matches the
         height of the left col so the operator's book grids
         occupy the full available height. */
      align-items: stretch;
    }
    /* Each column fills the mp-layout row height and stacks its
       buckets proportionally along the column axis. min-height: 0
       is required so the column doesn't force its own intrinsic
       content height onto the layout. */
    .mp-col {
      flex: 1 1 0;
      min-height: 0;
    }
    /* Left col carries the monitoring grids (leftColDefs: 8
       columns), right col carries the book grids (rightColDefs:
       14 columns). Width ratio mirrors the column-count ratio so
       each row reads at roughly the same px-per-column. */
    .mp-col-left  { flex: 4 1 0; }
    .mp-col-right { flex: 6 1 0; }
    /* Proportional bucket sizing — each bucket's flex-grow is driven
       by --bucket-rows (set inline from the derived row count). More
       rows = more column space; empty buckets get a floor of 240px so
       5 data rows + ag-Grid header + bucket-header all fit without
       grid-internal scroll. Breakdown: bucket-head ~32px + ag-Grid
       header ~32px + 5 rows × 28px = ~204px; 240px adds comfortable
       breathing room. Collapsed buckets still hold their flex weight
       (header visible); the bucket-grid itself is zeroed by the
       is-collapsed rule below. */
    .mp-col > .mp-bucket-wrap {
      flex: var(--bucket-rows, 1) 0 0;
      min-height: 240px;
    }
    /* Right grid only: positions + holdings share height equally
       regardless of row count. Left grid keeps proportional sizing. */
    .mp-col-right > .mp-bucket-wrap {
      flex: 1 1 0;
      min-height: 240px;
    }
    .mp-col > .mp-bucket-wrap > .bucket-grid {
      height: auto;
      flex: 1 1 0;
      min-height: 0;
    }
  }
  .mp-bucket-wrap {
    /* width: 100% keeps the card filling its parent flex column
       even when the body (grid) is collapsed to height: 0. Without
       this, the card can shrink to its (header-only) intrinsic width
       when collapsed — operator sees the card width jump.
       box-sizing: border-box so padding doesn't push the card over
       the column width. */
    width: 100%;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  /* Bucket label — small mono caps above each grid, tinted to match
     the per-major palette already used elsewhere on the page so the
     six grids feel like the same family seen from the top. */
  .mp-bucket-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.15rem 0.35rem;
    margin-bottom: 0.2rem;
    border-left: 3px solid currentColor;
    color: var(--algo-slate);
  }
  .mp-bucket-label-positions { color: rgba(125, 211, 252, 0.85); }
  .mp-bucket-label-holdings  { color: rgba(134, 239, 172, 0.85); }
  .mp-bucket-label-winners   { color: rgba(74, 222, 128, 0.85); }
  .mp-bucket-label-losers    { color: rgba(248, 113, 113, 0.85); }

  /* Bucket header — label + universe tabs on the same row above
     each Winners / Losers grid. The label keeps its left-border
     accent (inherited from .mp-bucket-label) so the section still
     reads as part of the 6-bucket family. */
  .mp-bucket-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.1rem;
    /* flex-wrap: nowrap so the CollapseButton stays on the same
       row as the label across every card — uniform vertical
       offset (operator: "watchlist expand should align with the
       others"). Tabs moved into a dedicated .mp-bucket-subhead
       row below so they no longer compete with the button for
       header-row space. */
    flex-wrap: nowrap;
  }
  .mp-bucket-head .mp-bucket-label { margin-bottom: 0; }
  /* Spacer pushes the CollapseButton to the FAR RIGHT of the
     header regardless of card content. Label sits left, spacer
     absorbs the gap, button locks to the right edge. Card width
     is set by the parent column flex so collapse doesn't move
     the button. */
  .mp-bucket-head-spacer {
    flex: 1 1 0;
  }
  /* Hidden grid container (inactive tab) — display:none keeps it
     in the DOM so bind:this lands at mount, but ag-Grid won't
     render anything until the operator flips back. ag-Grid's
     ResizeObserver re-measures + repaints when display restores. */
  .bucket-grid.mp-grid-hidden {
    display: none;
  }
  /* mp-head-tabs is a thin wrapper around AlgoTabs inside the
     bucket-head row — gives the strip a flex-shrink: 0 anchor so
     the label + controls cluster around it predictably. */
  .mp-head-tabs {
    display: inline-flex;
    flex-shrink: 0;
  }

  /* Symbol cell on the RIGHT grid — account-tinted left + right
     borders with a faint tint background. The colour is injected
     via the `--mp-sym-acct-color` custom property by the column's
     cellStyle (which reads the row's leadAccount and resolves via
     the shared hash palette in $lib/account.js). Without an account
     (rare; broken row data), the borders disappear so totals + edge
     cases stay clean. */
  :global(.ag-theme-algo .mp-sym-acct) {
    /* Per the symbol-cell simplification: drop the per-account inset
       bars and rely on the trailing Account column for account
       identity. The bg tint stays as a subtle marker. */
    background-color: color-mix(in srgb, var(--mp-sym-acct-color, transparent) 14%, transparent);
  }
  /* Symbol cell on the Winners / Losers grids — vertical tint on
     BOTH left + right edges (matching the bucket-label colour) so
     the cell reads as a clean coloured frame instead of a one-sided
     accent with ag-Grid's default gray divider showing through on
     the opposite edge. Same inset-box-shadow idiom as `.mp-sym-acct`
     so the line lands at the exact same pixel position regardless
     of which grid the cell is in. */
  /* Global divider-strip on every Pulse bucket grid. Per operator
     feedback the five grids (Pinned/Watchlist, Winners, Losers,
     Positions, Holdings) need to look IDENTICAL except for the
     symbol-cell tint (encoding direction / account) and the row
     background tint. ag-Grid's 1 px gray cell-divider would otherwise
     show on some grids and not others (Winners/Losers had them
     stripped earlier so the green/red tint border read clean, the
     others kept them — visually inconsistent). Strip both border-
     right + border-left on every cell so the gray hairlines never
     show; the meaningful colored inset borders on symbol cells stay
     because they're painted via box-shadow, not border. */
  :global(.mp-bucket-wrap .ag-theme-algo .ag-cell),
  :global(.mp-bucket-wrap .ag-theme-algo .ag-header-cell) {
    border-right: 0 !important;
    border-left: 0 !important;
  }
  /* Column-header bottom border — same amber 30 % as every other
     horizontal divider on the page (pin-divider, major-divider,
     mover-direction-divider). Earlier ag-Grid's default header-row
     border was too faint to read against the navy bucket-grid
     background; making it match the section-divider colour means
     the header row sits visually consistent with the section
     stripes below it. */
  :global(.mp-bucket-wrap .ag-theme-algo .ag-header-row) {
    border-bottom: 1px solid var(--algo-amber-border-soft) !important;
  }
  /* Winners / Losers ROW background tint — completes the "every
     section has a row-tint" rhythm. Same 0.06 alpha as Holdings
     row-hold-up / row-hold-down so the bucket reads as part of the
     family. Tinted on every row (not just first/last), matching
     pos-long / pos-short / row-watch. */
  :global(.mp-bucket-winners .ag-theme-algo .ag-row) {
    background-color: var(--algo-green-bg-soft) !important;
  }
  :global(.mp-bucket-losers .ag-theme-algo .ag-row) {
    background-color: var(--algo-red-bg-soft) !important;
  }
  /* Per the symbol-cell simplification pass: Winners / Losers no
     longer paint inset bars on the symbol cell. The per-bucket row
     background tint (added above) and the bucket-label header carry
     the direction identity. */
  /* Account column on the RIGHT grid — small-caps, account-colour
     foreground, monospace to lock the +N badge alignment. */
  :global(.ag-theme-algo .mp-acct-cell) {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  /* TOTAL row styling — bold + slight tint so the operator's eye
     locks onto the consolidated number at the END of each bucket.
     postSortRows pins them last in their bucket regardless of
     column sort, so the value reads as "sum of the block above"
     rather than a header strip. */
  /* TOTAL row scheme — bold + amber bg + thin amber top + bottom
     borders to close the bucket. Top border thinned from 2 px → 1 px
     per operator feedback; combined with the 0.12 amber bg + bold,
     the row reads as a footer without visually shouting. Aggregate
     is direction-agnostic so no green/red variants here (the
     per-cell P&L text inside still uses cell-pos / cell-neg for
     value-level direction). */
  :global(.ag-theme-algo .mp-total-row) {
    font-weight: 700;
    /* Stronger amber stratum so TOTAL stands out over data-row
       directional tints + (incoming) LTP heat cells. Operator:
       "total row should have a different background color scheme." */
    background: rgba(251, 191, 36, 0.22) !important;
    border-top: 2px solid rgba(251, 191, 36, 0.70) !important;
    border-bottom: 1px solid var(--algo-amber-border-soft) !important;
  }
  /* TOTAL row symbol cell — amber tint instead of the per-row
     direction tint so the row reads as an aggregate, not as a
     direction-coded data row. */
  :global(.ag-theme-algo .mp-total-row .ag-col-sym) {
    background-color: rgba(251, 191, 36, 0.20) !important;
  }
  /* TOTAL row LTP cell — suppress the incoming LTP heat colouring
     so the amber TOTAL stratum stays uniform. */
  :global(.ag-theme-algo .mp-total-row .ltp-vs-prev-up),
  :global(.ag-theme-algo .mp-total-row .ltp-vs-prev-down),
  :global(.ag-theme-algo .mp-total-row .ltp-vs-avg-up),
  :global(.ag-theme-algo .mp-total-row .ltp-vs-avg-down) {
    background-color: transparent !important;
  }
  /* Hide the day-P&L mini-bar on TOTAL rows — the aggregate doesn't
     get a per-day sign indicator (the operator reads the TOTAL P&L
     value directly). */
  :global(.ag-theme-algo .mp-total-row .ag-col-sym::after) {
    display: none !important;
  }
  /* Flat-mode wrapper — used by /pulse to drop the .algo-status-card
     navy chrome. The page becomes a flex column whose unified grid
     grows to fill the remaining viewport height, so the operator
     gets the maximum number of rows on-screen with the column
     header pinned at the top. */
  .mp-flat-wrap {
    /* Operator: "before there is a gap between page header and
       pinned tab. this is specific to pulse". Top padding zeroed
       so the pinned/watchlist tab strip sits flush below the
       page-header. Side + bottom padding kept. */
    padding: 0 0.4rem 0.4rem;
    display: flex;
    flex-direction: column;
    /* Fill the algo-content flex column so the grids claim all
       available viewport height. `flex: 1 1 0` + `min-height: 0`
       pairs with algo-content's own `flex: 1; display: flex;
       flex-direction: column` to make the pulse layout a proper
       height-constrained flex child (height: 0 is the canonical
       trick to make a flex child start at zero and grow to fill
       instead of sizing to its intrinsic content height). */
    flex: 1 1 0;
    min-height: 0;
  }
  /* Mobile — reset the viewport-fill behaviour; buckets use fixed
     heights and the page scrolls naturally through them. */
  @media (max-width: 600px) {
    .mp-flat-wrap {
      /* Opt out of flex-fill on mobile — the column stacks at
         natural content height so the page body scrolls. */
      flex: none;
      min-height: 0;
      overflow: visible;
      /* padding-top zeroed (was 0.3rem all-sides) so the Pinned tab
         strip sits flush below the page-header on mobile, matching
         the desktop rule above. Operator: "the gap between rows
         with pulse and pinned text needs to be reduced a little
         bit in pulse page". */
      padding: 0 0.3rem 0.3rem;
    }
    .mp-layout {
      /* On mobile the layout is a normal column stack — no height
         constraint so all buckets render at fixed heights and the
         page scrolls. Reset the flex-fill set for desktop. */
      flex: none;
    }
    .bucket-grid {
      height: 220px;
      min-height: 220px;
    }
  }
  .summary-grid,
  .funds-grid {
    width: 100%;
    min-height: 40px;
  }

  /* .mp-section-label is defined globally in app.css. */

  /* Slice 7g — strategy picker mounts next to the Symbols section
     label. Flex row so the picker sits on the right of the label
     without pushing the grids below it. */
  .mp-section-with-picker {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  /* Standalone wrapper used ONLY on /pulse (showSummary + showFunds
     both false → no "Symbols" header to host the picker inline).
     `display: contents` removes the wrapper from layout entirely so:
       - when StrategyPicker renders nothing (no strategies loaded /
         operator hasn't set any up — the common case), there is
         ZERO vertical footprint between the page-header and the
         first .mp-bucket-wrap (Pinned/Watchlist card)
       - when the picker DOES render, `.sp-wrap` flows directly into
         .mp-flat-wrap's flex column at its natural ~1.7rem height
         without the wrapper contributing extra margin
     Operator: "pulse the gap after header is still not reduced" —
     prior CSS edits (page-header, mp-toptab, mp-bucket-head) were
     nibbling pixels while this empty wrapper kept reserving ~30 px
     of gap on every /pulse render. */
  .mp-picker-standalone {
    display: contents;
  }

  /* Per-source subtotals strip retired — the block was commented
     out in the template; CSS family (.subtotals-strip, .st-group,
     .st-src, .st-chip, .st-val, .st-pos, .st-neg, .st-sym, .st-sep)
     dropped per audit. */

  /* Context menu (item 2) */
  :global(.ctx-menu) {
    position: fixed;
    z-index: 9999;
    min-width: 10rem;
    background: rgba(10,22,40,0.97);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 5px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.55);
    padding: 0.25rem 0;
    font-size: 0.65rem;
  }
  :global(.ctx-item) {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.3rem 0.75rem;
    background: transparent;
    border: none;
    color: rgba(200,216,240,0.85);
    cursor: pointer;
    font-size: 0.65rem;
    white-space: nowrap;
    transition: background 0.1s, color 0.1s;
  }
  :global(.ctx-item:hover) {
    background: rgba(251,191,36,0.1);
    color: #fbbf24;
  }
  :global(.ctx-item-danger) { color: rgba(248,113,113,0.8); }
  :global(.ctx-item-danger:hover) { background: rgba(248,113,113,0.1); color: #f87171; }
  :global(.ctx-sep) {
    height: 1px;
    background: rgba(200,216,240,0.1);
    margin: 0.2rem 0;
  }

  /* ── Search popup ─────────────────────────────────────────────────
     Lightweight overlay + modal for the symbol-add input. Mirrors
     the OrderTicket modal palette so the algo dark UI feels
     consistent across popups. Click-outside closes via the overlay's
     onclick handler; the modal itself stops propagation. */

  /* Chrome row — every control left-aligned. Wraps to a new line on
     narrow viewports rather than scrolling horizontally because CSS
     overflow-x:auto + overflow-y:visible CLIPS the Y axis too (CSS
     spec quirk). Clipping the Y axis killed the Show / Account
     MultiSelect dropdowns — they're position:absolute and got cut off
     above the chrome row's lower edge, so they were rendered but
     invisible to the operator (panel had measurable bounding box but
     was painted under the next row). flex-wrap:wrap dodges the
     overflow trap entirely while still allowing horizontal layout
     when the viewport is wide enough. */

  /* Per-card Account picker inside each bucket-head. LEFT-aligned
     (canonical rule) — sits immediately after the card label, before
     the spacer + control trio. Narrow width so it doesn't crowd the
     row on tighter viewports. */
  .mp-head-acct {
    flex: 0 0 auto;
    width: 7rem;
    min-width: 0;
  }
  /* Universe tabs (Underlying / Large Cap / Midcap / Smallcap)
     inline in the Winners / Losers bucket-head. Sits BETWEEN the
     label and the spacer so it reads as "Winners → which universe
     → controls". `flex-wrap: wrap` lets the tabs wrap onto a
     second row when the card width is tight (narrow viewports),
     keeping the card width unchanged. `min-width: 0` allows the
     flex container to shrink rather than forcing the card wider. */
  .mp-head-tabs {
    flex: 0 1 auto;
    min-width: 0;
    flex-wrap: wrap;
    row-gap: 0.18rem;
  }

  /* Manage-watchlists button — single chip at the end of the chrome
     row. Pencil-edits-list glyph reads as "manage list" rather than
     just "add". Same amber palette as the tab borders / Save buttons
     so it sits in the page palette. */
  .mp-add-btn {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0 0.45rem;
    height: 1.5rem;
    font-size: 0.95rem;
    line-height: 1;
    font-weight: 700;
    color: #fbbf24;
    background: transparent;
    border: 1px solid rgba(251, 191, 36, 0.4);
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .mp-add-btn:hover {
    background: rgba(251, 191, 36, 0.10);
    border-color: rgba(251, 191, 36, 0.7);
  }

  /* Section label inside the unified Add popup — separates the
     "Add symbol" and "New watchlist" sections so the two actions
     read as distinct without needing tabs. Subtle uppercase amber
     header (same palette as the page-section headers). */
  :global(.mp-add-section-label) {
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #fbbf24;
    margin-bottom: 0.35rem;
  }
  /* Divider between the two Add-popup sections — faint horizontal
     rule that mirrors the algo theme's hairline accents. */
  :global(.mp-add-divider) {
    height: 1px;
    background: rgba(200, 216, 240, 0.10);
    margin: 0.85rem 0 0.6rem;
  }

  :global(.search-overlay) {
    position: fixed;
    inset: 0;
    background: rgba(8, 15, 30, 0.7);
    z-index: 60;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 12vh;
  }
  :global(.search-modal) {
    width: min(28rem, 92vw);
    background: linear-gradient(180deg, #0c1830 0%, #0a1628 100%);
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 6px;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
    overflow: hidden;
  }
  :global(.search-header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.55rem 0.8rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.18);
    background: rgba(251, 191, 36, 0.04);
  }
  :global(.search-title) {
    font-size: 0.7rem;
    font-weight: 700;
    color: #fbbf24;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  :global(.search-close) {
    font-size: 1.05rem;
    line-height: 1;
    color: var(--algo-muted);
    background: transparent;
    border: none;
    padding: 0 0.25rem;
    cursor: pointer;
  }
  :global(.search-close:hover) { color: #fbbf24; }
  :global(.search-body) {
    padding: 0.7rem 0.8rem 0.85rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  :global(.search-row) {
    display: flex;
    align-items: stretch;
    gap: 0.35rem;
  }
  :global(.search-typeahead) {
    max-height: 16rem;
    overflow-y: auto;
    background: #0c1830;
    border: 1px solid rgba(251, 191, 36, 0.25);
    border-radius: 4px;
  }
  :global(.search-typeahead-item) {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.4rem 0.7rem;
    font-size: 0.7rem;
    background: transparent;
    border: none;
    cursor: pointer;
  }
  :global(.search-typeahead-item:hover) { background: rgba(251, 191, 36, 0.1); }
  :global(.search-hint) {
    font-size: 0.6rem;
    color: var(--algo-muted);
    line-height: 1.4;
  }

  /* B1 — LTP flash: directional (green up / red down). Slice AS audit
     fix — the prior single amber animation lost direction information
     on the product's highest-frequency cell. */
  :global(.ltp-flash-up) {
    animation: ltp-flash-up 600ms ease-out;
  }
  :global(.ltp-flash-down) {
    animation: ltp-flash-down 600ms ease-out;
  }
  @keyframes ltp-flash-up {
    0%   { background-color: rgba(74, 222, 128, 0.35); }
    100% { background-color: transparent; }
  }
  @keyframes ltp-flash-down {
    0%   { background-color: rgba(248, 113, 113, 0.35); }
    100% { background-color: transparent; }
  }
  /* Respect prefers-reduced-motion (audit fix 15). */
  @media (prefers-reduced-motion: reduce) {
    :global(.ltp-flash-up), :global(.ltp-flash-down) { animation: none; }
  }

  /* B2 — visually-hidden a11y helper */
  :global(.sr-only) {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border-width: 0;
  }
</style>
