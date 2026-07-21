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
  import { isMarketOpen } from '$lib/marketHours';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import ModalShell from '$lib/ModalShell.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { instrumentsCacheVersion } from '$lib/data/instruments';
  import { rootOfLabel, resolveVirtual } from '$lib/data/rootOf.js';
  import { displaySymbol } from '$lib/data/displaySymbol.js';

  // Module-scope cache for hyphenated symbol display. The cellRenderer
  // re-runs for every row × redraw; parsing each symbol once per session
  // keeps the render hot path O(1). Invalidated when the instruments
  // cache populates (see effect below) — otherwise the first paint
  // pins the cold-cache form (no expiry day) and never picks up the
  // per-symbol day once the dump loads.
  const _pulseSymFmtCache = new Map();
  /**
   * Format a symbol for the Pulse cell renderer.
   * For MCX/CDS futures the virtual root label is shown (e.g. "CRUDEOIL",
   * "CRUDEOIL.NEXT") instead of the raw contract name.
   * All other symbols go through the Dhan-style hyphenated formatter.
   *
   * @param {string} s         tradingsymbol
   * @param {string} [exch]    exchange (MCX / CDS / …)
   */
  function _pulseFmtSym(s, exch = '') {
    if (!s) return '';
    const cacheKey = `${s}|${exch}`;
    if (_pulseSymFmtCache.size > 600) _pulseSymFmtCache.clear();
    let v = _pulseSymFmtCache.get(cacheKey);
    if (v === undefined) {
      const eUp = (exch || '').toUpperCase();
      if (eUp === 'MCX' || eUp === 'CDS') {
        // Returns "CRUDEOIL", "CRUDEOIL.NEXT", or raw contract for far-month
        const rl = rootOfLabel(s, eUp);
        // Only use virtual label when it differs from the raw contract.
        // If rootOfLabel returns the raw contract (far-month), fall through
        // to the hyphenated format so the expiry is still visible.
        v = rl !== s ? rl : formatSymbol(s);
      } else {
        v = formatSymbol(s);
      }
      _pulseSymFmtCache.set(cacheKey, v);
    }
    return v;
  }
  import { fetchSettings } from '$lib/api';
  import { streamOpen, startQuoteStream, stopQuoteStream } from '$lib/data/quoteStream';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { getSnapshot, symbolStore, symbolTickCount, tickBus } from '$lib/data/symbolStore.svelte.js';
  import { bookChanged } from '$lib/data/bookChanged';
  import {
    fundsStore,
    pulsePositionsStore, pulseHoldingsStore,
    moversStore, moversSnapshotAt, activeListsStore, sparklinesStore,
    publishWatchQuotes, publishPulseQuotes,
  } from '$lib/data/marketDataStores.svelte.js';
  // resolveUnderlying helpers used by loadPulse are imported via pulseLoad.js.
  import CardControls from '$lib/CardControls.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import { createPerformanceSocket } from '$lib/ws';
  import { lastRefreshAt, formatDualTz, logTimeIst } from '$lib/stores';
  import { priceFmt, pctFmt, aggCompact, aggFmtGrid, pctFmtGrid, qtyFmt, directional, fmtPctScaled } from '$lib/format';
  import { acctColor, leadAccount } from '$lib/account';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import Select      from '$lib/Select.svelte';
  import { openActivityModal } from '$lib/stores';
  import ChartModal from '$lib/ChartModal.svelte';
  import AddToPulseModal from '$lib/AddToPulseModal.svelte';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { baseDayPnlForPosition } from '$lib/data/nav';
  import { lotsForRow, fmtLots } from '$lib/data/lotsForRow';
  import {
    dirCls,
    mkPnlCellClass, mkResolveCellLtp,
    mkSymColLeft, mkSymColRight, mkSparkCol,
    mkLtpCol, mkPrevCol, mkOpenCol, mkVolCol, mkOiCol, mkAcctColTrailing,
    mkLeftColDefs, mkRightColDefs,
    mkPosSummaryCols, mkHoldSummaryCols, mkFundsCols,
  } from '$lib/data/pulseColumns';
  import {
    mergeWatchlistRows, mergePositionRows, mergeHoldingRows,
    mergeUnderlyingAnchors, mergeMoverRows, tagWatchedIndices,
    finalizeRows, sortUnifiedRows,
  } from '$lib/data/pulseUnified';
  import {
    collectUnderlyings, assembleQuoteKeys, buildQuoteMaps, planAccountSeeding,
  } from '$lib/data/pulseLoad';
  import {
    PULSE_DEFAULT_COL_DEF, PULSE_SORTING_ORDER,
    pulseRowId, summaryRowId, postSortGroups,
  } from '$lib/data/pulseGridSetup';

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
    /** Bindable — parent page can bind to receive the movers snapshot timestamp. */
    moversAsOf = $bindable(/** @type {string|null} */ (null)),
  } = $props();

  // AG Grid valueFormatter wrappers — imported from $lib/format (shared SSOT).
  // aggFmtGrid / pctFmtGrid are the canonical formatters used across every
  // algo grid. numFmt uses priceFmt for per-share tick precision.
  const numFmt     = ({ value }) => value == null ? '—' : priceFmt(value);
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
  // Bridge pulsePositionsStore / pulseHoldingsStore through $effect → $state
  // so that when loadPulse() resolves, the downstream unifiedRows $derived.by
  // (which runs buildUnified — a heavy computation) is scheduled by Svelte's
  // microtask queue rather than firing synchronously inside the same long task
  // as the store write. Without this bridge the synchronous $derived cascade
  // blocks the main thread for >100 ms, freezing the RefreshButton spinner
  // mid-animation (RAIL long-task violation).
  //
  // Hydration-race fix (Jun 2026): when the store's `.value` transiently
  // reverts to `null` (e.g. an invalidate() during a refresh-cycle mode
  // switch, or a cross-page Performance load that hadn't pushed yet),
  // the prior `positions = p ?? []` line WIPED the local copy → emptied
  // the unifiedRows derivation → grid flashed empty for one frame
  // before the next store update repopulated. Now we explicitly skip
  // the mirror when the store goes null and keep the prior local
  // snapshot — stale-while-revalidate at the bridge.
  let positions = $state(/** @type {any[]} */ (pulsePositionsStore.value ?? []));
  let holdings  = $state(/** @type {any[]} */ (pulseHoldingsStore.value  ?? []));
  $effect(() => {
    const p = pulsePositionsStore.value;
    const h = pulseHoldingsStore.value;
    untrack(() => {
      if (p != null) positions = p;
      if (h != null) holdings  = h;
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
  // True when the current user is admin or designated — those are the
  // only roles that can mutate the shared global Pinned watchlist
  // (alias edits, item add / remove, rename, item-reorder). The
  // backend gates the same way; the frontend hides affordances so
  // non-admins don't get a 403 surprise.
  const _isDesignated = $derived.by(() => {
    const r = String($authStore.user?.role || '').toLowerCase();
    return r === 'admin' || r === 'designated';
  });

  // True when the visitor is anonymous (no authenticated user). Demo
  // users can view all data but cannot mutate watchlists — the backend
  // already rejects writes with 403; the UI must not surface controls
  // that will fail so anonymous visitors don't get a confusing error.
  const isDemo = $derived(!$authStore.user);

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
    { value: 'winners',   label: 'Gainers'   },
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
  // Mover-symbol-set change trigger for sparklines.
  // When the set of winner/loser tradingsymbols rotates (new movers appear
  // every 30 s), the next scheduled _TICK_SPARK is up to 60 s away —
  // new rows show "—" for sparkline until then. This $effect computes a
  // sorted signature of EXCH:SYM keys from the current movers list and
  // calls loadSparklines() immediately when the signature changes.
  //
  // Guard: skip the very first run (identical to the mount-path call at
  // line 1342) so we don't double-fetch on initial load. untrack() wraps
  // loadSparklines so it doesn't re-subscribe to whatever stores
  // loadSparklines reads internally.
  let _moverSparkSig = /** @type {string} */ ('');
  // Previous mover rotation's sparkline pairs — kept for 1 extra cycle so
  // symbols that just left the movers list aren't immediately pruned.
  // If they re-enter within one rotation (30s), their cached sparkline
  // shows instantly without a backend round-trip.
  let _prevMoverSparkPairs = /** @type {{tradingsymbol: string, exchange: string}[]} */ ([]);
  $effect(() => {
    const sig = movers
      .filter(m => m?.tradingsymbol)
      .map(m => `${String(m?.exchange || 'NSE').toUpperCase()}:${String(m.tradingsymbol).toUpperCase()}`)
      .sort()
      .join(',');
    if (sig === _moverSparkSig) return;
    const prev = _moverSparkSig;
    _moverSparkSig = sig;
    // Skip the first assignment (component boot — mount path already fires
    // loadSparklines() after loadPulse completes).
    if (!prev) return;
    untrack(() => loadSparklines());
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
  // Canonical account display order — sorted union of broker accounts.
  let _mpOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubMpOrder = accountDisplayOrder.subscribe(m => { _mpOrderMap = m; });
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
  // Bridge the legacy writable store through $state so Svelte 5's
  // runes scheduler sees a proper reactive dependency (avoids stale-
  // cache risk from reading a writable store directly inside $effect).
  let _bookChangedVal = $state(0);
  const _unsubBook = bookChanged.subscribe(/** @param {number} n */ n => { _bookChangedVal = n; });
  let _pulseBookCounter = 0;
  $effect(() => {
    const n = _bookChangedVal;
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
  // Tick-flash for P&L columns on the right grid (Positions / Holdings).
  // LTP already has its own directional flash (_ltpFlashUp/_ltpFlashDown);
  // this instance covers Day P&L and P&L whose values change on each
  // poll cycle (broker fetch every 5 s) rather than on every SSE tick.
  // Key format: tradingsymbol + ':' + field. Threshold 0.001 (epsilon)
  // prevents false flashes when the effect re-runs with identical float
  // values — Math.abs(v - last) < 0 is always false with threshold=0.
  // Alpha is 0.13 (app.css .tf-up/.tf-down) — subtle, not alarming.
  const _mpFlash = createTickFlash({ threshold: 0.001, durationMs: 300 });
  /** Build a `{sym: ltp}` map snapshot from symbolStore for fast cell reads.
   *
   * LTP flicker fix (Jun 2026): include only strictly-positive values.
   * A stored 0 (legacy entry from before the symbolStore zero-guard
   * tightened up) would otherwise propagate into _liveLtpSnap and the
   * valueGetter `?? p.data.ltp` chain would render 0 for that cell —
   * `0 ?? p.data.ltp` is 0 because nullish-coalescing only falls back
   * on null/undefined. Excluding non-positive values here lets the
   * valueGetter fall through to the polled `p.data.ltp` (which may be
   * positive even if symbolStore is cold), then to null (renders "—").
   */
  function _buildLtpSnap() {
    /** @type {Record<string, number>} */
    const out = {};
    for (const [sym, snap] of symbolStore.entries()) {
      const v = snap?.ltp;
      if (v != null && Number.isFinite(v) && v > 0) out[sym] = v;
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
  // Closed-hours sparkline safety net: fires every 5 min regardless of market
  // state. During open hours it defers to the runTick path (_TICK_SPARK every
  // 60 s) which is already running — no duplicate call is made. During closed
  // hours the marketAwareInterval suspends runTick entirely, so without this
  // poller sparklines would never refresh until next market open.
  let _stopClosedSparkPoll;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _deferredSparkTimer = null;
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
      // Snapshot BEFORE loadMovers() so _prevMoverSparkPairs holds the
      // outgoing rotation — the symbols that may be pruned by the next
      // loadSparklines() call. Reading movers here (pre-update) is safe
      // because moversStore.value hasn't changed yet.
      _prevMoverSparkPairs = untrack(() => movers ?? [])
        .filter(m => m?.tradingsymbol)
        .map(m => ({
          tradingsymbol: String(m.tradingsymbol).toUpperCase(),
          exchange: String(m.exchange || 'NSE').toUpperCase(),
        }));
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
  /**
   * @param {boolean} [skipLtp]  When true (RefreshButton's both-closed
   *   path), positions + holdings fetches route with `?skip_ltp=1` so the
   *   broker LTP fetch is bypassed and rows serve from the daily_book
   *   snapshot. Funds/margins still fetch fresh (broker-authoritative
   *   values that don't depend on market being open). Batched quotes +
   *   sparklines also skip because there's no live LTP to consume.
   */
  async function refreshAllNow(skipLtp = false) {
    if (_refreshing) return;
    _refreshing = true;
    try {
      // loadPulse({ force: true }) re-fetches positions + holdings even if
      // in-flight requests are already running (operator expects fresh data
      // on explicit Refresh). skipLtp=true routes positions/holdings to
      // the snapshot path AND skips the quote / sparkline pollers that
      // would otherwise fan out broker LTP fetches for the watchlist +
      // movers.
      //
      // Parallel handshake: kick off loadMovers() eagerly and stash the
      // promise in `_pendingMoversP`. loadPulse awaits it inline just
      // before the mover-add loop (see :mover-await), so positions +
      // holdings + underlying resolution + contract/watchlist batchQuote
      // key assembly all overlap with movers RTT. Funds + quotes fire
      // fully parallel — neither depends on movers.
      _pendingMoversP = (enableMovers && !skipLtp)
        ? loadMovers().catch(() => null)
        : null;
      try {
        await Promise.allSettled([
          skipLtp ? Promise.resolve() : loadQuotes(),
          showFunds ? loadFunds() : Promise.resolve(),
          _pendingMoversP || Promise.resolve(),
          loadPulse({ force: true, skipLtp }),
          skipLtp ? Promise.resolve() : loadSparklines(),
        ]);
      } finally {
        // Clear handshake so idle _runTick ticks after this refresh
        // don't await a resolved promise (harmless but noisy).
        _pendingMoversP = null;
      }
    } finally {
      _refreshing = false;
    }
  }
  // Exposed so a parent page can wire the page-header RefreshButton
  // (next to the wall-clock timestamp) into the same refresh flow the
  // per-card RefreshButtons trigger. `bind:this={pulseRef}` from the
  // page lets the page call `pulseRef.refresh()`.
  export async function refresh(skipLtp = false) { await refreshAllNow(skipLtp); }
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

  // _lotsForUnifiedRow and _lotsFmt removed — replaced by lotsForRow / fmtLots
  // imported from $lib/data/lotsForRow (shared with PerformancePage / Dashboard).

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
    // Carry the previous mover rotation's pairs forward so their sparklines
    // survive one prune cycle (oscillating symbols show from cache).
    // _prevMoverSparkPairs is snapshotted in _runTick() before loadMovers(),
    // so it always holds the outgoing rotation regardless of who calls us.
    for (const p of _prevMoverSparkPairs) {
      const key = `${p.exchange}:${p.tradingsymbol}`;
      if (!seen.has(key)) {
        seen.add(key);
        pairs.push(p);
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
        if (Array.isArray(parsed) && parsed.length > 0) {
          // One-time migration: old builds stored 'src:movers' as a single
          // unified movers toggle. The 6-grid layout split this into two
          // separate 'src:winners' + 'src:losers' tokens. If the stored
          // value contains the old token, replace it with both new tokens
          // so the Winners/Losers grids don't silently disappear after
          // an upgrade (the prune $effect would strip 'src:movers' since
          // it's no longer in _availableSourceValues).
          const migrated = parsed.flatMap(v => v === 'src:movers' ? ['src:winners', 'src:losers'] : [v]);
          selectedShow = migrated;
        }
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
    const fundsP  = showFunds ? loadFunds() : Promise.resolve();
    // Load movers BEFORE loadPulse so mover symbols are in the store when
    // loadPulse's batchQuote pass assembles allKeys. During closed hours
    // marketAwareInterval suspends _runTick so a mount race means blank
    // Winners/Losers vol/oi cells until manual refresh.
    //
    // Mount stays sequential — activeLists (from listsP) must be populated
    // before loadPulse's watchlist-add loop runs, or pinned/watchlist symbols
    // miss the same batchQuote pass. refreshAllNow uses a `_pendingMoversP`
    // handshake instead (see loadMovers block) since by refresh time both
    // instruments + activeLists are already hot.
    await Promise.allSettled([instrumentsP, accountsP, listsP, fundsP,
      enableMovers ? loadMovers() : Promise.resolve()]);
    // Now that movers (and lists/positions) are in their stores, run
    // loadPulse so the batchQuote includes mover + watchlist symbols.
    // force=true: ensure positions + holdings + batchQuote all fire on
    // first paint even if the book poller already has a value cached.
    await loadPulse({ force: true });
    // Sparkline bootstrap — ensure positions + holdings are in the store
    // before we snapshot unifiedRows for the pairs list.
    //
    // After the loadPulse-store-migration (Sprint F+, commit 50ed5e83),
    // loadPulse(force=false) no longer fetches positions on the initial
    // mount call. On a cold start (empty localStorage) pulsePositionsStore.value
    // can still be null when the Promise.allSettled above resolves because the
    // pulse stores are populated only by MarketPulse's own load() calls.
    // Without this wait, loadSparklines() reads unifiedRows with empty positions
    // → pairs list is watchlist-only → position sparkline cells show "—" until
    // the 60 s _TICK_SPARK cadence fires.
    //
    // pulsePositionsStore.load() is safe to call concurrently: createDataStore
    // deduplicates by args so if a prior load() is still in-flight we join its
    // Promise (zero extra HTTP round-trips). If already resolved we short-circuit
    // to the cached value synchronously. After the allSettled,
    // pulsePositionsStore.value + pulseHoldingsStore.value are guaranteed
    // populated (or errored — the store keeps the last-good value).
    // await tick() flushes the $effect bridge that mirrors store values into
    // the `positions` / `holdings` $state variables that unifiedRows reads.
    await Promise.allSettled([pulsePositionsStore.load(), pulseHoldingsStore.load()]);
    await tick();
    loadSparklines();
    // 2 s retry covers any remaining watchlist-quote race: loadQuotes
    // finishes its watchlist poll AFTER the Promise.allSettled above,
    // so watchlist-only symbols that weren't yet in activeLists get their
    // sparklines without waiting for the 60 s _TICK_SPARK tick.
    // The second call is cheap — createDataStore deduplicates pairs that
    // were already fetched by the first call.
    _deferredSparkTimer = setTimeout(() => { _deferredSparkTimer = null; loadSparklines(); }, 2000);

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
      // R2: skip settings poll for anonymous / demo users — they have
      // no auth token and would generate repeated 401 responses.
      if (!$authStore.user) return _tickMs;
      try {
        const rows = await fetchSettings();
        const all = Array.isArray(rows) ? rows : (rows?.settings || []);
        const row = all.find?.(s => s?.key === 'pulse.tick_interval_ms');
        const v = Number(row?.value ?? row?.default_value);
        if (Number.isFinite(v) && v >= 500 && v <= 60000) return v;
      } catch (_) { /* keep current */ }
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

    // Closed-hours sparkline safety net — 60 s cadence, visible-only.
    // When the market is open, runTick already fires loadSparklines every
    // 60 s via _TICK_SPARK; bail early to avoid a duplicate fetch.
    // When the market is closed, marketAwareInterval suspends runTick so
    // this is the only path that keeps sparklines refreshed from DB.
    // The backend batch_sparkline endpoint runs in db_only mode during
    // closed hours (no broker calls), so this is cheap: Tier 1+2 only.
    // 60 s (was 5 min) so a cold ohlcv_store on a fresh deploy retries
    // within a minute rather than leaving premarket sparklines blank for
    // up to 5 min.
    _stopClosedSparkPoll = visibleInterval(() => {
      if (isMarketOpen()) return; // open-hours: runTick handles it
      loadSparklines();
    }, 60 * 1000);

    // Real-time order-fill push — Kite postback fires a WS event
    // `position_filled` the moment an order fills. Subscribe so
    // Pulse refreshes positions + holdings IMMEDIATELY
    // instead of waiting up to 10 s for the next loadPulse tick.
    // Other (non-fill) events on the same socket also trigger a
    // refresh — cheap to over-fetch, expensive to lag a fill.
    // (The wider `book_changed` bus also fires loadPulse — kept
    // here for the qty-delta optimistic patch path which only
    // emits on FILL, not on CANCELLED/REJECTED.)
    // R1: anonymous / demo users have no auth token — skip the WS
    // entirely to avoid a flood of 401/403 upgrade attempts.
    if (!isDemo) {
      stopWS = createPerformanceSocket((msg) => {
        if (msg?.event === 'position_filled') {
          // Order just filled — refresh both books right now so the
          // grid shows the new qty within a tick. The 10 s pulse
          // poll keeps running as a backstop.
          loadPulse();
        }
      });
    }

    // Keyboard shortcuts — scoped to this wrapper only.
    document.addEventListener('keydown', handleKeydown);
    document.addEventListener('click', onDocClick);

    // SSE quote stream — live LTP pushed from the server's KiteTicker
    // WebSocket. startQuoteStream() is idempotent so multiple Pulse
    // instances on the same page share one connection.
    startQuoteStream();

    // Tick-bus subscription — drives _ltpFlashUp/Down from real SSE ticks
    // (sub-250ms per sym) instead of the poll-diffed $effect. Direction
    // comes from tickBus (computed in symbolStore._mergeSymbolWrite where
    // prev and next LTP are both available). Per-sym clearance timers
    // prevent one sym's 300ms window from wiping another sym's flash.
    _tickBusUnsub = tickBus.subscribe(({ sym, dir }) => {
      if (dir === 'up') {
        _ltpFlashUp = new Set([..._ltpFlashUp, sym]);
        _ltpFlashDown = new Set([..._ltpFlashDown].filter(s => s !== sym));
      } else if (dir === 'down') {
        _ltpFlashDown = new Set([..._ltpFlashDown, sym]);
        _ltpFlashUp = new Set([..._ltpFlashUp].filter(s => s !== sym));
      }
      // Clear existing timer for this sym (re-arm on each tick).
      const existing = _ltpFlashTimers.get(sym);
      if (existing) clearTimeout(existing);
      _ltpFlashTimers.set(sym, setTimeout(() => {
        _ltpFlashTimers.delete(sym);
        _ltpFlashUp   = new Set([..._ltpFlashUp].filter(s => s !== sym));
        _ltpFlashDown = new Set([..._ltpFlashDown].filter(s => s !== sym));
      }, 300));
    });
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

  // Handshake for parallel refresh: refreshAllNow / mount kick off
  // loadMovers() and stash its promise here BEFORE calling loadPulse.
  // loadPulse then runs positions + holdings + underlying resolution +
  // batchQuote key-assembly in parallel with movers, and only awaits
  // this promise immediately before the mover-add loop (:mover-await).
  // This eliminates the serialized `movers → loadPulse` chain that
  // held funds / positions / holdings hostage to the movers RTT.
  let _pendingMoversP = /** @type {Promise<any> | null} */ (null);

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
  // ── mainRows sort helpers (module-level pure fns, no reactive deps) ───────
  // Extracted from the $derived.by body to reduce cyclomatic complexity
  // (sort comparator was CC 13; now mainRows derived is CC 3).

  // Sub-group order WITHIN the Movers major — underlying first
  // (most-actively-traded, F&O-eligible names), then large_cap → midcap →
  // smallcap. Unknown / null sub-groups go last.
  const _MOVER_GROUP_ORDER     = { underlying: 0, large_cap: 1, midcap: 2, smallcap: 3 };
  // Direction order: Winners first, Losers second.
  const _MOVER_DIRECTION_ORDER = { winners: 0, losers: 1 };

  function _mrUgKey(r) { return String(r.underlying || r.tradingsymbol || '').toUpperCase(); }
  function _mrTier(r) {
    if (r.kind === 'spot')                       return 0;
    if (r.kind === 'fut' || r.kind === 'mcx')    return 1;
    if (r.kind === 'opt')                        return 2;
    return 3;
  }
  function _mrMgOrder(r) {
    return r._majorGroup === 'movers' ? (_MOVER_GROUP_ORDER[r._moverGroup] ?? 9) : -1;
  }
  function _mrMdOrder(r) {
    return r._majorGroup === 'movers' ? (_MOVER_DIRECTION_ORDER[r._moverDirection] ?? 9) : -1;
  }
  // Sort: major → direction (Movers) → sub-group (Movers) → biggest
  // mover first → underlying-group → anchor-before-options → symbol.
  // Why underlying-group over alphabetic? Anchors (spot/fut stubs) carry
  // tradingsymbol that differs from underlying — sorting by underlying
  // keeps them contiguous with their options block.
  function _compareMainRows(a, b) {
    const moA = a._majorOrder ?? 99, moB = b._majorOrder ?? 99;
    if (moA !== moB) return moA - moB;
    const mdA = _mrMdOrder(a), mdB = _mrMdOrder(b);
    if (mdA !== mdB) return mdA - mdB;
    const mgA = _mrMgOrder(a), mgB = _mrMgOrder(b);
    if (mgA !== mgB) return mgA - mgB;
    if (a._majorGroup === 'movers' && b._majorGroup === 'movers') {
      const pA = Math.abs(Number(a._mover_change_pct) || Number(a.change_pct) || 0);
      const pB = Math.abs(Number(b._mover_change_pct) || Number(b.change_pct) || 0);
      if (pA !== pB) return pB - pA;
    }
    const ua = _mrUgKey(a), ub = _mrUgKey(b);
    if (ua !== ub) return ua.localeCompare(ub);
    const ta = _mrTier(a), tb = _mrTier(b);
    if (ta !== tb) return ta - tb;
    return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
  }
  // Annotate first-row divider flags (major / direction / sub-group) in-place.
  function _annotateMainRowFlags(rows) {
    let lastMajor = null, lastMoverDirection = null, lastMoverGroup = null;
    for (const r of rows) {
      r._majorFirst = (r._majorGroup !== lastMajor);
      lastMajor = r._majorGroup;
      if (r._majorGroup === 'movers') {
        r._moverDirectionFirst = (r._moverDirection !== lastMoverDirection);
        lastMoverDirection = r._moverDirection;
        // Reset sub-group tracker when direction flips so the first
        // sub-group of Losers gets its own divider.
        if (r._moverDirectionFirst) lastMoverGroup = null;
        r._moverGroupFirst = (r._moverGroup !== lastMoverGroup);
        lastMoverGroup = r._moverGroup;
      } else {
        r._moverDirectionFirst = false;
        r._moverGroupFirst = false;
      }
    }
  }

  const mainRows = $derived.by(() => {
    const rows = unifiedRows.filter(r => {
      if (r._majorGroup === 'pinned')                       return false;
      if (r._majorGroup === 'watchlist' && !showWatchlist)  return false;
      return true;
    });
    rows.sort(_compareMainRows);
    _annotateMainRowFlags(rows);
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
  // Per-tab denominator badges. Counts mirror the full pool (NOT
  // the top-N slice) so the operator sees how many candidates
  // exist before the cap kicks in. Declared FIRST because the
  // effective-tab deriveds below reference them.
  /** @param {'winners'|'losers'} direction */
  function _tabCounts(direction) {
    const out = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of mainRows) {
      if (r._majorGroup !== 'movers') continue;
      if (r._moverDirection !== direction) continue;
      // A symbol may belong to multiple tabs (_moverGroups is an array).
      // Increment every tab it belongs to so counts reflect true coverage.
      const gs = /** @type {string[]} */ (r._moverGroups ?? [r._moverGroup].filter(Boolean));
      for (const g of gs) {
        if (g === 'underlying' || g === 'large_cap' || g === 'midcap' || g === 'smallcap') {
          out[g]++;
        }
      }
    }
    return out;
  }
  const winnerCounts = $derived(_tabCounts('winners'));
  const loserCounts  = $derived(_tabCounts('losers'));

  // null = auto-select (system picks the tab with the most rows).
  // Once the operator clicks a tab the selection is "locked" to that tab.
  let winTab  = $state(/** @type {MoverTab|null} */ (null));
  let loseTab = $state(/** @type {MoverTab|null} */ (null));

  /** Auto-pick the tab with the highest candidate count for a direction.
   *  Falls back to 'underlying' when all counts are zero. */
  function _bestTab(counts) {
    /** @type {MoverTab} */
    let best = 'underlying';
    let max  = -1;
    for (const t of MOVER_TABS) {
      const c = counts[t] ?? 0;
      if (c > max) { max = c; best = /** @type {MoverTab} */ (t); }
    }
    return best;
  }
  const _effWinTab  = $derived(winTab  ?? _bestTab(winnerCounts));
  const _effLoseTab = $derived(loseTab ?? _bestTab(loserCounts));

  // Top-N filter for a direction × tab. All tabs draw from the
  // movers fetch (Holdings tab retired — operator's book lives
  // in the right column's Holdings card).
  /** @param {'winners'|'losers'} direction
   *  @param {MoverTab} tab */
  function _topRowsFor(direction, tab) {
    // A symbol may belong to multiple tabs. Use _moverGroups (array)
    // when available; fall back to legacy _moverGroup for old cached rows.
    const pool = mainRows.filter(r =>
      r._majorGroup === 'movers'
      && r._moverDirection === direction
      && (r._moverGroups
          ? r._moverGroups.includes(tab)
          : r._moverGroup === tab));
    return pool
      .slice()
      .sort((a, b) => {
        const pA = Math.abs(Number(a._mover_change_pct) || Number(a.change_pct) || 0);
        const pB = Math.abs(Number(b._mover_change_pct) || Number(b.change_pct) || 0);
        if (pA !== pB) return pB - pA;
        // Stable tie-break: tradingsymbol prevents row shuffling when
        // two symbols have equal |change_pct| (common during closed hours
        // when symbolStore publishers may produce subtly different
        // floating-point values across polls).
        return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
      })
      .slice(0, _MOVER_TOP_N);
  }
  const winRows  = $derived(_topRowsFor('winners', _effWinTab));
  const loseRows = $derived(_topRowsFor('losers',  _effLoseTab));

  // ── _totalsRowFor helpers ─────────────────────────────────────────
  // Seed a blank accumulator for _accumTotalsRow.
  function _blankTotalsAcc() {
    return {
      day_pnl: 0, pnl: 0, cost: 0, prevMktVal: 0,
      invSum: 0, curSum: 0, qty_pos: 0, qty_hold: 0,
      anyDayPnl: false, anyPnl: false, anyInv: false, anyCur: false,
    };
  }

  // Accumulate one row into acc (mutates in place).
  // Prefers broker raw values so the TOTAL matches PositionStrip exactly.
  // anyDayPnl/anyPnl/anyInv/anyCur flags decide null vs 0 in the return.
  function _accumTotalsRow(/** @type {ReturnType<typeof _blankTotalsAcc>} */ acc, /** @type {any} */ r) {
    const rowPnl    = (r._broker_pnl     != null) ? r._broker_pnl     : r.pnl;
    const rowDayPnl = (r._broker_day_pnl != null) ? r._broker_day_pnl : r.day_pnl;
    if (rowDayPnl != null) { acc.day_pnl += Number(rowDayPnl) || 0; acc.anyDayPnl = true; }
    if (rowPnl    != null) { acc.pnl     += Number(rowPnl)    || 0; acc.anyPnl    = true; }
    acc.cost       += Math.abs(Number(r._cost_basis)        || 0);
    acc.prevMktVal += Math.abs(Number(r._prev_market_value) || 0);
    if (r.inv_val != null) { acc.invSum += Number(r.inv_val) || 0; acc.anyInv = true; }
    if (r.cur_val != null) { acc.curSum += Number(r.cur_val) || 0; acc.anyCur = true; }
    acc.qty_pos  += Number(r.qty_pos)  || 0;
    acc.qty_hold += Number(r.qty_hold) || 0;
  }

  // TOTAL row for one major (positions / holdings). Each carries
  // summed day_pnl + pnl + cost_basis so the Day P&L % and P&L %
  // columns auto-derive via their valueGetters. Wrapped in an array
  // for direct use as pinnedBottomRowData.
  function _totalsRowFor(rows, major, label) {
    if (!rows.length) return null;
    const acc = _blankTotalsAcc();
    for (const r of rows) _accumTotalsRow(acc, r);
    return {
      key: `__total_${major}`,
      _isTotal: true,
      _majorGroup: major,
      tradingsymbol: label,
      day_pnl:  acc.anyDayPnl ? acc.day_pnl : null,
      pnl:      acc.anyPnl    ? acc.pnl     : null,
      _cost_basis: acc.cost,
      _prev_market_value: acc.prevMktVal,
      inv_val:  acc.anyInv ? acc.invSum : null,
      cur_val:  acc.anyCur ? acc.curSum : null,
      qty_pos: acc.qty_pos, qty_hold: acc.qty_hold,
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

  // Compact "DD Mon HH:MM IST" label for the movers closed-hours snapshot.
  // Non-null only when the backend returned a persisted off-hours snapshot;
  // null during live market hours (no "as of" caveat needed).
  const _moversAsOf = $derived.by(() => {
    const ts = moversSnapshotAt.value;
    if (!ts) return null;
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return null;
      const datePart = d.toLocaleDateString('en-GB', {
        day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata',
      });
      const timePart = logTimeIst(ts).slice(0, 5); // HH:MM only
      return `${datePart} ${timePart} IST`;
    } catch (_) { return null; }
  });
  $effect(() => { moversAsOf = _moversAsOf; });

  // One effect per grid — Svelte 5 reactivity tracks the closed-over
  // derivation so any source change automatically pushes fresh row
  // data without us having to re-bundle effects.
  $effect(() => { if (gridPinnedReady && gridPinned)
    gridPinned.setGridOption('rowData', pinnedRows); });
  $effect(() => { if (gridWatchReady && gridWatch)
    gridWatch.setGridOption('rowData', watchRows); });
  $effect(() => { if (gridPositionsReady && gridPositions) {
    // Read reactive deps BEFORE untrack so the effect re-fires on changes.
    const pRows      = positionsRows;
    const pTotalRows = positionsTotalRows;
    untrack(() => {
      // Tick-flash: call _mpFlash.update() for each row's P&L fields before
      // writing rowData. Wrapped in untrack() to prevent the $state write
      // inside flash.update() from registering as a dep and looping.
      for (const r of pRows) {
        if (r._isTotal) continue;
        const sym = r.tradingsymbol;
        if (!sym) continue;
        if (r.day_pnl != null) _mpFlash.update(`${sym}:day_pnl`, Number(r.day_pnl));
        if (r.pnl    != null) _mpFlash.update(`${sym}:pnl`,     Number(r.pnl));
      }
      gridPositions.setGridOption('rowData', pRows);
      gridPositions.setGridOption('pinnedBottomRowData', pTotalRows);
      // Force a refreshCells pass so cellClass callbacks re-evaluate the
      // flash state set above. Deferred 0ms so ag-Grid's own row-data
      // transaction finishes first; second refresh at +400ms clears flash.
      try { gridPositions.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      setTimeout(() => {
        try { gridPositions.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      }, 400);
    });
  } });
  $effect(() => { if (gridHoldingsReady && gridHoldings) {
    // Same pattern for holdings P&L flash.
    const hRows      = holdingsRows;
    const hTotalRows = holdingsTotalRows;
    untrack(() => {
      for (const r of hRows) {
        if (r._isTotal) continue;
        const sym = r.tradingsymbol;
        if (!sym) continue;
        if (r.day_pnl != null) _mpFlash.update(`${sym}:day_pnl`, Number(r.day_pnl));
        if (r.pnl    != null) _mpFlash.update(`${sym}:pnl`,     Number(r.pnl));
      }
      gridHoldings.setGridOption('rowData', hRows);
      gridHoldings.setGridOption('pinnedBottomRowData', hTotalRows);
      try { gridHoldings.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      setTimeout(() => {
        try { gridHoldings.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      }, 400);
    });
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
  //
  // TICK-BUS MIGRATION (2026-07): _ltpFlashUp/Down are now fed by the
  // tickBus subscriber (onMount below) rather than by the $effect diff.
  // The $effect retains the shimmer + refreshCells cascade path.
  let _ltpFlashUp   = $state(/** @type {Set<string>} */ (new Set()));
  let _ltpFlashDown = $state(/** @type {Set<string>} */ (new Set()));
  // Per-sym clearance timers for tickBus-fed flash (avoids wiping the
  // whole set when one symbol clears — prevents flicker when multiple
  // symbols have staggered 300ms windows).
  const _ltpFlashTimers = /** @type {Map<string, ReturnType<typeof setTimeout>>} */ (new Map());
  /** @type {(() => void) | null} */
  let _tickBusUnsub = null;

  const _scheduleIdle = (cb) => {
    if (typeof window !== 'undefined'
        && typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(cb, { timeout: 500 });
    } else {
      setTimeout(cb, 1);
    }
  };

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
    _ltpPaintTimer = setTimeout(() => {
      _ltpPaintTimer = null;
      // Determine which symbols had an LTP change in this paint batch
      // so the cascade refresh only repaints cells that actually changed.
      const prev = _lastPaintedSnap;
      const cur  = _liveLtpSnap;
      let hasCascade = false;
      for (const k of Object.keys(cur)) {
        const p = prev[k];
        if (p === undefined) continue;
        if (cur[k] !== p) hasCascade = true;
      }
      // Capture the current snapshot at paint time (may have advanced
      // further than when the timer was scheduled).
      _lastPaintedSnap = { ..._liveLtpSnap };
      // Defer the actual ag-Grid refresh batch until the main thread
      // is idle so clicks always jump the queue. The flash class
      // assignments (now driven by tickBus subscriber) already happened
      // synchronously — only the expensive grid work runs on idle.
      // Part B cascade: when any symbol's LTP changed, also repaint
      // derived columns whose cellClass callbacks check _ltpFlashUp/Down.
      // Cascade: Day P&L + P&L (absolute). Day % and P&L % no longer flash,
      // so they are excluded from the force-repaint list.
      _scheduleIdle(() => {
        const _ltpCols = ['ltp', 'sparkline'];
        const _cascadeCols = hasCascade
          ? ['ltp', 'sparkline', 'day_pnl', 'pnl']
          : _ltpCols;
        if (gridPinnedReady    && gridPinned    && topTab === 'pinned')    gridPinned.refreshCells({ columns: _ltpCols, force: true });
        if (gridWatchReady     && gridWatch     && typeof topTab === 'number') gridWatch.refreshCells({ columns: _ltpCols, force: true });
        if (gridPositionsReady && gridPositions && showPositions)          gridPositions.refreshCells({ columns: _cascadeCols, force: true });
        if (gridHoldingsReady  && gridHoldings  && showHoldings)           gridHoldings.refreshCells({ columns: _cascadeCols, force: true });
        if (gridWinReady       && gridWin       && showWinners)            gridWin.refreshCells({ columns: _ltpCols, force: true });
        if (gridLoseReady      && gridLose      && showLosers)             gridLose.refreshCells({ columns: _ltpCols, force: true });
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
  //
  // WHY `r.day_change_val` is read directly here (not via baseDayPnlForPosition):
  // `r` is a PositionsSummaryRow — an aggregate with no `overnight_quantity`
  // field. baseDayPnlForPosition() requires per-position fields to apply its
  // new-position override (oq=0, dcv=0, pnl≠0) and MUST NOT be called on an
  // aggregate row. The aggregate is safe because:
  //   • Live path: apply_day_change_backstop() runs BEFORE build_summary_from_rows()
  //     in backend/api/routes/positions.py:393, so Σ day_change_val is already
  //     corrected for every per-position edge case.
  //   • Snapshot path (_positions_snapshot in positions.py:40): summary rows are
  //     built from build_snapshot_position_row() which sets overnight_quantity=qty
  //     (never 0) and resolves day_pnl via resolve_snapshot_day_pnl() before
  //     aggregation — the override condition cannot fire on snapshot rows.
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
  //
  // WHY `r.day_change_val` is read directly here (not via baseDayPnlForPosition):
  // `r` is a HoldingsSummaryRow — a per-account aggregate from
  // _build_holdings_summary() in backend/api/routes/holdings.py. Holdings do
  // not carry `overnight_quantity` (baseDayPnlForPosition is a positions-only
  // helper). Holdings Day P&L is broker-computed (pnl − (close − cost) × qty)
  // and arrives already correct; no backstop override is needed or applicable.
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
    _unsubMpOrder();
    _unsubBook();
    stopPulseTick?.(); stopTickSettingPoll?.(); _stopClosedSparkPoll?.(); stopWS?.();
    if (_deferredSparkTimer) { clearTimeout(_deferredSparkTimer); _deferredSparkTimer = null; }
    stopQuoteStream();
    // Clear any in-flight throttle timers so their setTimeout
    // callbacks don't fire into a destroyed component (audit-flagged
    // — the flush timer is cleaned up by the $effect's own teardown,
    // but the paint + prefetch timers weren't).
    if (_ltpPaintTimer) { clearTimeout(_ltpPaintTimer); _ltpPaintTimer = null; }
    _tickBusUnsub?.();
    for (const t of _ltpFlashTimers.values()) clearTimeout(t);
    _ltpFlashTimers.clear();
    _mpFlash.dispose();
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

  // Part 1 — Jun 2026: loadPulse accepts a force flag.
  // force=true  → operator Refresh or mount: re-fetch positions + holdings.
  // force=false → background tick: layout poller already fetched; read values.
  // Rate impact: ~24 req/min (layout poller only) vs ~36/min pre-migration.
  //
  // Heavy logic extracted to pulseLoad.js helpers:
  //   collectUnderlyings, assembleQuoteKeys, buildQuoteMaps, planAccountSeeding.
  async function loadPulse(/** @type {{ force?: boolean, skipLtp?: boolean }} */ { force = false, skipLtp = false } = {}) {
    try {
      if (force) {
        // showSummary (dashboard mode) needs raw .summary from the API response;
        // the shared store discards it. Fetch in parallel with the store loads.
        // skipLtp (Jul 2026) — RefreshButton's both-markets-closed path.
        const summaryP = showSummary
          ? Promise.allSettled([
              fetchPositions({ skipLtp }).catch(() => null),
              fetchHoldings({ skipLtp }).catch(() => null),
            ])
          : Promise.resolve(null);

        await Promise.allSettled([
          pulsePositionsStore.load({ skipLtp }, { force: true }),
          pulseHoldingsStore.load({ skipLtp }, { force: true }),
        ]);

        if (showSummary) {
          const summaryResults = await summaryP;
          const pRaw = summaryResults?.[0]?.status === 'fulfilled' ? summaryResults[0].value : null;
          const hRaw = summaryResults?.[1]?.status === 'fulfilled' ? summaryResults[1].value : null;
          positionsSummary = (pRaw?.summary || []).slice();
          holdingsSummary  = (hRaw?.summary || []).slice();
        }
      }

      const p_rows = pulsePositionsStore.value ?? [];
      const h_rows = pulseHoldingsStore.value  ?? [];

      // ── Account-picker seeding ──────────────────────────────────────
      // Surface every account id seen across positions + holdings for the
      // account-picker dropdown. Also UNION with the broker registry
      // (loaded accounts that may have 0 positions / 0 holdings).
      if (accountPicker) {
        const accts = new Set();
        for (const r of p_rows) if (r.account) accts.add(String(r.account));
        for (const r of h_rows) if (r.account) accts.add(String(r.account));
        for (const a of _knownBrokerAccounts) accts.add(String(a));
        const sorted = sortAccountsBy([...accts], _mpOrderMap);
        availableAccounts = sorted;

        // readPersisted shim: checks sessionStorage for a non-empty array.
        const readPersisted = (/** @type {string} */ key) => {
          try {
            const raw = sessionStorage.getItem(key);
            if (raw) {
              const p = JSON.parse(raw);
              return Array.isArray(p) && p.length > 0;
            }
          } catch (_) {}
          return false;
        };

        const plan = planAccountSeeding({
          sorted,
          seenAccounts: _seenAccounts,
          positionsAccounts,
          holdingsAccounts,
          seededFromBrokers: _seededFromBrokers,
          hasKnownBrokers: _knownBrokerAccounts.length > 0,
          orderMap: _mpOrderMap,
          readPersisted,
        });
        positionsAccounts = plan.positionsAccounts;
        holdingsAccounts  = plan.holdingsAccounts;
        _seenAccounts     = plan.seenAccounts;
        _seededFromBrokers = plan.seededFromBrokers;
        if (plan.persistSeen) {
          try {
            sessionStorage.setItem('mp.seenAccounts', JSON.stringify(plan.persistSeen));
          } catch (_) {}
        }
      }

      // ── Underlying resolution + contract keys ───────────────────────
      // collectUnderlyings walks positions, holdings, and activeLists to
      // build the underlyingInfos map and contractKeys set.
      const { underlyingInfos, contractKeys } = collectUnderlyings({
        pRows: p_rows,
        hRows: h_rows,
        activeLists,
        findNearestFut: findNearestFuture,
        listFuts: listFutures,
      });

      // ── Assemble batchQuote universe ────────────────────────────────
      // :mover-await — synchronise with the parallel loadMovers() the
      // caller (refreshAllNow / mount) kicked off before invoking us.
      // Position+holding fetches, underlying resolution, and the
      // contract/watchlist batchQuote-key assembly above ran in parallel
      // with movers; we only need `movers` populated for the mover-add
      // loop below, so awaiting here (not at the top of loadPulse)
      // maximises overlap. When there's no handshake (idle _runTick
      // ticks) `_pendingMoversP` is null → the await is a no-op.
      if (_pendingMoversP) {
        try { await _pendingMoversP; } catch (_) { /* movers store logs */ }
      }

      const allKeys = assembleQuoteKeys({
        contractKeys,
        underlyingInfos,
        activeLists,
        movers,
      });

      // ── Batch quote + pulseQuotes assembly ──────────────────────────
      if (allKeys.size) {
        const items = await batchQuoteChunked([...allKeys]);
        // Stamp arrival time before any processing — reflects when data
        // arrived from the broker, not when quote maps finish building.
        // Works for both force=true (operator refresh/mount) and
        // force=false (background auto-poll tick).
        pulseLastUpdate = Date.now();
        _lastPulseAt = pulseLastUpdate;
        lastRefreshAt.set(pulseLastUpdate);
        // BH5: publish the broker-quote snapshot for every symbol in
        // view into symbolStore. This is the last non-symbolStore data
        // sink — after this, every market-data poll on this page feeds
        // the same per-symbol cache.
        publishPulseQuotes(items);
        pulseQuotes = buildQuoteMaps(items, underlyingInfos);
      } else {
        pulseQuotes = { underlyings: {}, contracts: {} };
        // No quotes requested (empty watchlist/positions) — still stamp
        // so the RefreshButton tooltip reflects the completed poll.
        pulseLastUpdate = Date.now();
        _lastPulseAt = pulseLastUpdate;
        lastRefreshAt.set(pulseLastUpdate);
      }
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
  // Count of prefetch requests still in flight (pending timers + pending
  // fetches). Used for stagger so the delay is bounded to actually-pending
  // requests rather than the ever-growing _prefetchTimers array length.
  // After 125+ symbols the old `_prefetchTimers.length * 80` delay would
  // exceed 10s; this counter resets toward zero as each request completes.
  let _prefetchPending = $state(0);
  function _stagedPrefetch(sym, exch) {
    if (!sym || _prefetchedChartSyms.has(sym)) return;
    _prefetchedChartSyms.add(sym);
    // Stagger: 80ms per pending symbol so concurrent requests spread out
    // without accumulating indefinitely as completed slots free up.
    const t = setTimeout(() => {
      import('$lib/ChartWorkspace.svelte')
        .then(m => m.prefetchChartBars(sym, exch || ''))
        .catch(() => {})
        .finally(() => { _prefetchPending--; });
    }, _prefetchPending * 80);
    _prefetchPending++;
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

  // parseSymbol / parseSymbolFallback have moved to pulseUnified.js.
  // No direct callers remain in this file.
  // MAJOR_ORDER is baked into makeRowFactory in pulseUnified.js.

  // buildUnified — thin orchestrator. All logic is in pulseUnified.js helpers.
  // Called by the $derived.by(unifiedRows) derivation.
  function buildUnified(actLists, pos, hold, pq, getInst, includePos, includeHold, moverRows, includeMovers, includeWatch = true) {
    const uq = pq?.underlyings || {};
    const cq = pq?.contracts   || {};
    /** @type {Record<string, any>} */
    const byKey = {};

    // snapOf wraps the Svelte untrack() call so helpers in pulseUnified.js
    // stay pure (no reactive context required).
    const snapOf = (sym) => untrack(() => getSnapshot(sym));

    // Context bags passed to each helper.
    const wlCtx  = { snapOf, getInst };
    const posCtx = { snapOf, getInst, isMarketOpen, baseDayPnlForPosition };
    const holdCtx = { snapOf, getInst, isMarketOpen };
    const anchCtx = { getInst };
    const movCtx  = { snapOf };
    const finCtx  = { directional, leadAccount, acctColor };

    mergeWatchlistRows(byKey, actLists, wlCtx);
    mergePositionRows(byKey, pos, includePos, cq, posCtx);
    mergeHoldingRows(byKey, hold, includeHold, cq, holdCtx);
    mergeUnderlyingAnchors(byKey, uq, pos, hold, includePos, includeHold, anchCtx);
    mergeMoverRows(byKey, moverRows, includeMovers, includePos, includeHold, includeWatch, movCtx);
    tagWatchedIndices(byKey);
    finalizeRows(byKey, finCtx);
    return sortUnifiedRows(Object.values(byKey), groupOrder, detachedSymbols);
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
    else if (sym.endsWith('FUT') || inst.virtual) typeInput = 'FU';
    else                          typeInput = 'EQ';
    // Virtual roots (GOLD, GOLD_NEXT) must be added directly — they are
    // intentionally the auto-roll watchlist entry, not a signal to open
    // the option chain picker. An unguarded openOptionPicker(inst.s, inst.e)
    // on "GOLD" would route into the CE/PE chain flow and never save the
    // virtual root row the operator actually chose.
    searchOpen = false;
    if (!inst.virtual) {
      // If the picked symbol is an underlying (has CE/PE chains), open the
      // inline option picker instead of adding directly. Close the
      // search modal first so the option picker isn't visually stacked
      // behind it.
      const opened = await openOptionPicker(inst.s, inst.e);
      if (opened) return;
    }
    // Direct-add path: equities, futures, CDS, virtual roots, and anything
    // without an option chain.
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
    // Focus is now managed by AddToPulseModal's $effect (tick + focus).
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

  // ── symRenderer helpers ──────────────────────────────────────────
  // Closes over module-level symbols: _fnoLotFor, _underlyingsWithActivePositions,
  // lotsForRow, fmtLots, qtyFmt, fmtPctScaled (all defined in scope).

  /**
   * Build the lot-chip and P/H/W/U/M badge cluster HTML for the symbol cell.
   * Returns { lotChip, badgeHtml } separately so symRenderer can interleave
   * aliasTail between them (matching original: sym + lotChip + aliasTail + badgeHtml).
   */
  function _symCellBadges(row) {
    let lotChip = '';
    if (row.src?.h) {
      try {
        const symStr = String(row.tradingsymbol || '').toUpperCase();
        const lot = symStr ? _fnoLotFor(symStr) : 0;
        if (lot > 0) {
          const lotsRounded = lotsForRow(row) ?? 0;
          if (lotsRounded >= 0.1) {
            const lotsStr = fmtLots(lotsRounded);
            const _hasPos = _underlyingsWithActivePositions.has(symStr);
            const _cls = _hasPos ? 'badge-fno-lot badge-fno-lot-pos' : 'badge-fno-lot';
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
      const _pLots = lotsForRow(row);
      const _pLabel = (_pLots != null && _pLots > 0) ? `${fmtLots(_pLots)}L` : qtyFmt(q);
      badges.push(`<span class="sym-badge badge-p" title="Position (${qtyFmt(q)} contracts)">P ${_pLabel}</span>`);
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
      const label = pct != null ? fmtPctScaled(pct, 2, true) : '';
      badges.push(`<span class="sym-badge badge-m badge-m-${dir}" title="Top mover ${label}${sticky}">M${arrow}</span>`);
    }
    const badgeHtml = badges.length ? `<span class="sym-badges">${badges.join('')}</span>` : '';
    return { lotChip, badgeHtml };
  }

  /**
   * Return { optClass, symTitle } for the symbol span.
   * optClass: CE/PE tint class (or empty string).
   * symTitle: virtual-root title attribute string for MCX/CDS rows (or empty string).
   */
  function _symCellClasses(row, main) {
    const optClass = row.opt_type === 'CE' ? 'sym-ce'
                   : row.opt_type === 'PE' ? 'sym-pe'
                   : '';
    const _exchU = String(row.exchange || '').toUpperCase();
    const _isMcxCds = _exchU === 'MCX' || _exchU === 'CDS';
    const _rawSym = String(row.tradingsymbol || '');
    const _resolvedContract = _isMcxCds ? resolveVirtual(_rawSym, _exchU) : _rawSym;
    const symTitle = (_isMcxCds && _resolvedContract && _resolvedContract !== main)
      ? ` title="${_resolvedContract}"`
      : '';
    return { optClass, symTitle };
  }

  // ── getRowClass helpers ──────────────────────────────────────────

  /** Returns CSS classes for Movers direction/group dividers within the movers major-group. */
  function _moverRowClasses(r) {
    const out = [];
    if (r._moverDirectionFirst && !r._majorFirst) out.push('mover-direction-divider');
    if (r._moverDirection)                        out.push(`mover-dir-row-${r._moverDirection}`);
    if (r._moverGroupFirst && !r._majorFirst && !r._moverDirectionFirst) out.push('mover-group-divider');
    if (r._moverGroup)                            out.push(`mover-${r._moverGroup}`);
    return out;
  }

  /**
   * Returns CSS classes driven by row source flags (p/h/w/u).
   * Preserves the original two-statement structure so that rows with s.p+s.u or
   * s.h+s.u receive BOTH the position/holding class AND row-und (s.u can co-fire).
   */
  function _sourceRowClasses(r, s) {
    const out = [];
    if (s.p) {
      const q = Number(r.qty_pos) || 0;
      if (q < 0) out.push('pos-short');
      else if (q > 0) out.push('pos-long');
      else out.push('row-pos');
    } else if (s.h) {
      const pnl = Number(r.pnl);
      if (Number.isFinite(pnl) && pnl > 0) out.push('row-hold-up');
      else if (Number.isFinite(pnl) && pnl < 0) out.push('row-hold-down');
      else out.push('row-hold-flat');
      try {
        if (getInstrument) {
          const sym = String(r.tradingsymbol || '').toUpperCase();
          if (_fnoLotFor(sym) > 0) out.push('row-hold-fno');
        }
      } catch (_) { /* defensive — cache miss shouldn't break row class */ }
    }
    if (s.w && !s.p && !s.h) out.push('row-watch');
    else if (s.u) out.push('row-und');
    return out;
  }

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
    const main = opAlias
               || row.alias
               || _pulseFmtSym(row.tradingsymbol || '', row.exchange || '');
    // Always surface the raw tradingsymbol after the main when an alias
    // is in play so the operator can still see what the row really is.
    const aliasTail = (opAlias || row.alias)
      ? `<span class="sym-alias" title="Tradingsymbol"> → ${_pulseFmtSym(row.tradingsymbol || '', row.exchange || '')}</span>`
      : '';
    // CE/PE tint + virtual-root title attribute (MCX/CDS only).
    const { optClass, symTitle } = _symCellClasses(row, main);
    // Lot-viable chip + P/H/W/U/M badge cluster.
    // lotChip sits immediately after the symbol text (before aliasTail + badgeHtml)
    // so the operator's eye lands on the actionable covered-call count first.
    const { lotChip, badgeHtml } = _symCellBadges(row);
    // Per-row remove (×) — operator can drop a watchlist row inline.
    // Hidden on the shared global Pinned row for non-admin / non-
    // designated users since the backend would 403 the delete; UI
    // shouldn't tease an affordance that won't work.
    const _isGlobalRow = _globalListIds.has(row.watchlist_list_id);
    const _canRemoveHere = (!_isGlobalRow || _isDesignated) && !isDemo;
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
    const moveBtns = (sym && !isDemo)
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
    return `<span class="sym-main ${optClass}"${symTitle}>${main}</span>${lotChip}${aliasTail}${badgeHtml}${removeBtn}${moveBtns}${actionsBtn}${dirLabel}`;
  }

  /**
   * Inline SVG sparkline for the last N daily closes.
   * ~32×14px, no axes/labels, stroke coloured by direction.
   *
   * Render contract (hardened Jun 2026):
   *   1. "—" only when sparklines[sym] is absent or empty (no historical data).
   *   2. Historical bars always render when ≥ 1 bar is present.
   *   3. If liveTail > 0: replace the last bar with the live SSE LTP.
   *   4. If liveTail = 0 / null: render historical bars unchanged (no override).
   *
   * The live-LTP tail is an ENHANCEMENT only — never a gate on rendering.
   * The body renders regardless of whether SSE LTP is available.
   */
  function sparkRenderer(params) {
    const sym    = String((params.data || {}).tradingsymbol || '').toUpperCase();
    let base     = sparklines[sym];
    if (!base || base.length === 0) {
      return '<span style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--c-muted);font-size: var(--fs-sm)">—</span>';
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
    // Live-LTP tail override — ENHANCEMENT only, never gates the render.
    // Replace the last historical close with the live SSE LTP when strictly
    // positive. When liveTail is 0 or absent, closes = base and the
    // historical body renders unchanged. The backend's batch_sparkline already
    // appends a live LTP; this refreshes it with the SSE tick (sub-second vs
    // polling interval). _buildLtpSnap excludes non-positive values; the
    // `liveTail > 0` guard here is belt-and-suspenders against any transient
    // 0 collapsing the polyline to baseline.
    const liveTail = _liveLtpSnap[sym];
    const closes = (typeof liveTail === 'number' && liveTail > 0)
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
    // Flat-line centering (Jun 2026 hydration races fix): when every
    // close is identical (backend's [ltp, ltp] pad shipped on a
    // historical-data rate-limit, OR a genuinely flat-day symbol),
    // the prior `range = max - min || 1` left the line glued to the
    // bottom of the cell because `(1 - 0/1) * (H - 2*PAD)` resolved
    // to the lowest Y. Render the flat line at vertical CENTER so it
    // reads as a deliberate "no movement" indicator rather than a
    // rendering bug. Operator: "sparkline shows either dash or flat
    // line" — this addresses the flat-at-bottom edge of that complaint.
    const flat  = (max === min);
    const range = flat ? 1 : (max - min);
    const xStep = (W - PAD * 2) / (closes.length - 1);
    const yMid  = H / 2;
    const yOf   = (v) => flat ? yMid : (PAD + (1 - (v - min) / range) * (H - PAD * 2));
    const pts   = closes.map((v, i) => `${(PAD + i * xStep).toFixed(1)},${yOf(v).toFixed(1)}`).join(' ');
    const up    = closes[closes.length - 1] >= closes[0];
    const lineColor = flat
      ? 'rgba(126,151,184,0.55)'  // muted slate for the no-movement case
      : (up ? 'rgba(91,142,149,0.85)' : 'rgba(196,122,61,0.85)');
    const areaBase = flat
      ? 'rgba(126,151,184,1)'
      : (up ? 'rgba(91,142,149,1)' : 'rgba(196,122,61,1)');
    const gradId = `sg${sym.replace(/[^A-Za-z0-9]/g, '')}`;
    const firstX = PAD.toFixed(1);
    const lastX  = (PAD + (closes.length - 1) * xStep).toFixed(1);
    const areaPts = `${firstX},${H} ${pts} ${lastX},${H}`;
    const svgInner = [
      `<defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">`,
      `<stop offset="0%" stop-color="${areaBase}" stop-opacity="0.22"/>`,
      `<stop offset="100%" stop-color="${areaBase}" stop-opacity="0"/>`,
      `</linearGradient></defs>`,
      `<polygon points="${areaPts}" fill="url(#${gradId})" stroke="none"/>`,
      `<polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>`,
    ].join('');
    return `<span style="display:flex;align-items:center;justify-content:center;height:100%"><svg width="${W}" height="${H}" style="display:block;overflow:visible">${svgInner}</svg></span>`;
  }

  // dirCls is imported from pulseColumns.js (line 77 import) — the local
  // duplicate definition was removed as part of the pulseColumns refactor.

  function getRowClass(params) {
    const r = params.data || {};
    const s = r.src || {};
    const classes = [];
    if (r._isTotal) {
      classes.push('mp-total-row');
      classes.push(`mp-total-${r._majorGroup || 'misc'}`);
    }
    if (r._pinFirst && r._pinCategory) classes.push(`pin-divider pin-cat-${r._pinCategory}`);
    if (r._majorFirst && r._majorGroup && r._majorGroup !== 'pinned')
      classes.push(`major-divider major-${r._majorGroup}`);
    if (r._majorGroup === 'movers') classes.push(..._moverRowClasses(r));
    classes.push(..._sourceRowClasses(r, s));
    if (r.account_stale === true) classes.push('row-account-stale');
    return classes.join(' ');
  }

  function mountGrid() {
    const RA = 'ag-right-aligned-cell';
    const dirCellClass = (p) => `${RA} ${dirCls(p.value)}`;
    // P&L-tinted cell class — built via factory so the accessor closures
    // capture the live $state bindings (_ltpFlashUp/Down reassigned each tick).
    const pnlCellClass = mkPnlCellClass({
      RA,
      getMpFlash:       () => _mpFlash,
      getLtpFlashUp:    () => _ltpFlashUp,
      getLtpFlashDown:  () => _ltpFlashDown,
    });

    // Main symbols grid — only built when the parent opted into the
    // per-symbol view (/pulse). /dashboard passes showSymbolsGrid=false
    // because it shows only summary + funds; the summary/funds grids
    // below still need to mount in that case.
    if (showSymbolsGrid && gridPinnedEl) {

    // ─── Shared column shapes (built via pulseColumns.js factories) ─────
    // Accessor functions (`() => _ltpFlashUp` etc.) are mandatory for any
    // $state binding that is REASSIGNED (not just mutated) — the tick handler
    // does `_ltpFlashUp = new Set(...)` so a captured value would be stale.
    const _symColLeft       = mkSymColLeft({ symRenderer });
    const _symColRight      = mkSymColRight({ symRenderer });
    const _sparkCol         = mkSparkCol({ sparkRenderer });
    const _ltpCol           = mkLtpCol({
      getLiveLtpSnap:  () => _liveLtpSnap,
      getLtpFlashUp:   () => _ltpFlashUp,
      getLtpFlashDown: () => _ltpFlashDown,
      numFmt, RA, numericHdr,
    });
    const _prevCol          = mkPrevCol({ RA, numericHdr, numFmt });
    const _openCol          = mkOpenCol({ RA, numericHdr, numFmt });
    const _volCol           = mkVolCol({ RA, numericHdr, aggCompact });
    const _oiCol            = mkOiCol({ RA, numericHdr, aggCompact });

    // ─── Left grid: Pinned / Watchlist / Movers ──────────────────
    const leftColDefs = mkLeftColDefs({
      symColLeft: _symColLeft, sparkCol: _sparkCol, ltpCol: _ltpCol,
      prevCol: _prevCol, openCol: _openCol, volCol: _volCol, oiCol: _oiCol,
      numericHdr, dirCellClass, pctFmtGrid,
    });

    // ─── Right grid: Positions / Holdings ────────────────────────
    const _acctColTrailing  = mkAcctColTrailing({ RA });
    const rightColDefs = mkRightColDefs({
      symColRight: _symColRight, sparkCol: _sparkCol, ltpCol: _ltpCol,
      prevCol: _prevCol, openCol: _openCol, volCol: _volCol, oiCol: _oiCol,
      acctColTrailing: _acctColTrailing,
      RA, numericHdr,
      pnlCellClass, dirCellClass, pctFmtGrid, aggFmtGrid, numFmt, qtyFmt,
      lotsForRow, fmtLots,
    });

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
    // postSortGroups imported from $lib/data/pulseGridSetup — pure fn, no closure.

    // Factory: every per-bucket grid shares the same shape (height
    // tracks, getRowClass, sort + resize defaults, click handlers).
    // Only columnDefs / emptyMsg / pinnedBottom vary per bucket.
    function makeBucketGrid(el, columnDefs, emptyMsg, pinnedBottom = []) {
      return createGrid(el, {
        theme: 'legacy',
        columnDefs,
        rowData: [],
        pinnedBottomRowData: pinnedBottom,
        getRowId: pulseRowId,
        defaultColDef: PULSE_DEFAULT_COL_DEF,
        sortingOrder: PULSE_SORTING_ORDER,
        overlayNoRowsTemplate:
          `<span style="font-size: var(--fs-md);color:var(--c-muted)">${emptyMsg}</span>`,
        domLayout: 'normal',
        getRowClass,
        // Keep underlyings + their options together as a block during
        // column sort. The TOTAL row + pinned-bottom rows ride
        // outside this since they're rendered via pinnedBottomRowData
        // (not in the sortable body).
        postSortRows: postSortGroups,
        rowHeight: 28,
        onRowClicked: handleRowClick,
        onCellContextMenu: (ev) => {
          if (ev.data) openContextMenu(ev.event, ev.data);
        },
      });
    }

    // Left-column buckets (monitoring views — leftColDefs).
    if (gridPinnedEl) {
      const _pinnedEmptyMsg = isDemo
        ? 'Pinned watchlist — read-only in demo.'
        : 'Pinned watchlist is empty — add a symbol via the + button.';
      gridPinned = makeBucketGrid(gridPinnedEl, leftColDefs, _pinnedEmptyMsg);
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
    if (showSummary && positionsSummaryEl) {
      const posSummaryCols = mkPosSummaryCols({ numericHdr, pnlCellClass, aggFmtGrid, pctFmtGrid });
      positionsSummaryGrid = createGrid(positionsSummaryEl, {
        theme: 'legacy',
        columnDefs: posSummaryCols,
        rowData: [],
        getRowId: summaryRowId,
        defaultColDef: PULSE_DEFAULT_COL_DEF,
        sortingOrder: PULSE_SORTING_ORDER,
        domLayout: 'autoHeight',
        rowHeight: 28,
      });
      positionsSummaryReady = true;
    }

    // Holdings Summary grid — Account | Day P&L | Day % | P&L | P&L % | Cur Val | Inv Val
    if (showSummary && holdingsSummaryEl) {
      const holdSummaryCols = mkHoldSummaryCols({ RA, numericHdr, pnlCellClass, aggFmtGrid, pctFmtGrid });
      holdingsSummaryGrid = createGrid(holdingsSummaryEl, {
        theme: 'legacy',
        columnDefs: holdSummaryCols,
        rowData: [],
        getRowId: summaryRowId,
        defaultColDef: PULSE_DEFAULT_COL_DEF,
        sortingOrder: PULSE_SORTING_ORDER,
        domLayout: 'autoHeight',
        rowHeight: 28,
      });
      holdingsSummaryReady = true;
    }

    // Funds grid — per-account margins.
    if (showFunds && fundsEl) {
      const fundsCols = mkFundsCols({ RA, numericHdr, dirCellClass, aggFmtGrid });
      fundsGrid = createGrid(fundsEl, {
        theme: 'legacy',
        columnDefs: fundsCols,
        rowData: [],
        getRowId: summaryRowId,
        defaultColDef: PULSE_DEFAULT_COL_DEF,
        sortingOrder: PULSE_SORTING_ORDER,
        domLayout: 'autoHeight',
        rowHeight: 28,
      });
      fundsReady = true;
    }
  }

  // ── handleRowClick helpers ────────────────────────────────────────
  // Returns true when a cell-button click was consumed so the outer
  // dispatcher can short-circuit without opening the order ticket.
  // Note: remove-button does NOT call stopPropagation (intentional);
  // move + actions buttons do — preserve that asymmetry.
  function _tryHandleRowButton(ev, /** @type {HTMLElement | null} */ target) {
    const rmBtn = target?.closest?.('.sym-remove');
    if (rmBtn) {
      const itemId = Number(rmBtn.getAttribute('data-item'));
      const listId = Number(rmBtn.getAttribute('data-list'));
      if (itemId && listId) removeItem(listId, itemId);
      return true;
    }
    // ▲/▼ group-move buttons — bump the row's whole underlying group
    // up or down. Bucket membership is preserved.
    const moveBtn = target?.closest?.('.sym-move');
    if (moveBtn) {
      ev.event?.stopPropagation?.();
      const dir = Number(moveBtn.getAttribute('data-dir')) || 0;
      if (dir !== 0) moveGroup(ev.data, dir);
      return true;
    }
    // ⋯ actions button — opens the right-click context menu anchored
    // to the button's bottom-right corner.
    const actBtn = target?.closest?.('.sym-actions');
    if (actBtn) {
      ev.event?.stopPropagation?.();
      const rect = /** @type {DOMRect} */ (actBtn.getBoundingClientRect());
      openContextMenu(
        { clientX: rect.right, clientY: rect.bottom + 4, preventDefault: () => {} },
        ev.data,
      );
      return true;
    }
    return false;
  }

  // Derive BUY/SELL side from the position's signed qty.
  function _computeTicketSide(/** @type {any} */ r) {
    if (r.src?.p && r.qty_pos > 0) return 'SELL';
    return 'BUY';
  }

  // Lazy-resolve realAccounts if onMount hasn't finished yet.
  async function _ensureRealAccountsLoaded() {
    if (realAccounts.length) return;
    try {
      const r2 = await fetchAccounts();
      realAccounts = (r2?.accounts || [])
        .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
        .filter(Boolean);
    } catch (_) { /* leave empty — ticket's self-fetch backstop will fill */ }
  }

  // Build and open the order ticket from a unified row object.
  // Use the row's own account field directly — each position/holding
  // row carries one unmasked account id for admin sessions.
  async function _openTicketFromRow(/** @type {any} */ r) {
    const inst = getInstrument?.(r.tradingsymbol);
    const lot = Number(inst?.ls || 1);
    const side = _computeTicketSide(r);
    const isRealAcct = (/** @type {any} */ a) => !!(a && !String(a).includes('#'));
    const preAccount = isRealAcct(r.account) ? String(r.account) : '';
    await _ensureRealAccountsLoaded();
    // Pass currentQty (signed position qty) so SymbolPanel's footer
    // button shows "CLOSE BUY" / "CLOSE SELL" for position rows.
    // Watchlist/mover/anchor rows (no position) get currentQty=0.
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

  async function handleRowClick(ev) {
    if (!ev.data) return;
    const target = /** @type {HTMLElement | null} */ (ev.event?.target ?? null);
    if (_tryHandleRowButton(ev, target)) return;
    if (!allowOrders) return;
    await _openTicketFromRow(ev.data);
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
    const underlying = encodeURIComponent(
      row.underlying || row.tradingsymbol || ''
    );
    window.location.href = `/admin/derivatives?u=${underlying}`;
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
    openActivityModal();
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
      if (!isDemo) openSearch();
      return;
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
          <CardHeader
            bind:isCollapsed={_colPinWatch}
            bind:isFullscreen={_fsPinWatch}
            bind:filter={_filterPinWatch}
            cardId="pulse-pinwatch"
            label="Pinned/Watchlist"
            onRefresh={refreshAllNow}
            refreshLoading={_refreshing}
            onDownload={() => (topTab === 'pinned' ? gridPinned : gridWatch)?.exportDataAsCsv({ fileName: 'watchlist.csv' })}
          >
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
            {#snippet left()}
              <AlgoTabs
                tabs={[
                  { id: 'pinned', label: 'Pinned', color: /** @type {const} */ ('amber') },
                  /* Operator (2026-07-01): "active tab text color must be
                     consistent". User-created watchlists previously took
                     the sky variant to distinguish them from Pinned; now
                     every tab shares the canonical amber palette so the
                     active state reads uniform across the platform. */
                  ..._userLists.map(l => ({ id: `wl:${l.id}`, label: l.name, color: /** @type {const} */ ('amber') }))
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
              {#if !isDemo}
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
              {/if}
            {/snippet}
          </CardHeader>
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
            <CardHeader
              bind:isCollapsed={_colWinners}
              bind:isFullscreen={_fsWinners}
              bind:filter={_filterWinners}
              cardId="pulse-winners"
              label="Gainers"
              onRefresh={refreshAllNow}
              refreshLoading={_refreshing}
              onDownload={() => gridWin?.exportDataAsCsv({ fileName: 'winners.csv' })}
            >
              {#snippet left()}
                <span class="mp-bucket-label mp-bucket-label-winners">Gainers</span>
              {/snippet}
              {#snippet middle()}
                <div class="mp-head-tabs">
                  <AlgoTabs
                    tabs={MOVER_TABS.map(t => ({ id: t, label: MOVER_TAB_LABEL[t], badge: winnerCounts[t] || undefined }))}
                    value={_effWinTab}
                    onChange={(id) => { winTab = /** @type {MoverTab} */ (id); }}
                    compact={true}
                  />
                </div>
              {/snippet}
            </CardHeader>
            <div bind:this={gridWinEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
          </section>
        {/if}
        {#if showLosers}
          <section class="mp-bucket-wrap mp-bucket-losers"
                   class:is-collapsed={_effColLosers}
                   class:is-empty={_losersTotal === 0}
                   class:fs-card-on={_fsLosers}
                   style="--bucket-rows:{_bRowsLosers}">
            <CardHeader
              bind:isCollapsed={_colLosers}
              bind:isFullscreen={_fsLosers}
              bind:filter={_filterLosers}
              cardId="pulse-losers"
              label="Losers"
              onRefresh={refreshAllNow}
              refreshLoading={_refreshing}
              onDownload={() => gridLose?.exportDataAsCsv({ fileName: 'losers.csv' })}
            >
              {#snippet left()}
                <span class="mp-bucket-label mp-bucket-label-losers">Losers</span>
              {/snippet}
              {#snippet middle()}
                <div class="mp-head-tabs">
                  <AlgoTabs
                    tabs={MOVER_TABS.map(t => ({ id: t, label: MOVER_TAB_LABEL[t], badge: loserCounts[t] || undefined }))}
                    value={_effLoseTab}
                    onChange={(id) => { loseTab = /** @type {MoverTab} */ (id); }}
                    compact={true}
                  />
                </div>
              {/snippet}
            </CardHeader>
            <div bind:this={gridLoseEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
          </section>
        {/if}
      </div>

      <div class="mp-col mp-col-right">
        <section class="mp-bucket-wrap mp-bucket-positions"
                 class:is-collapsed={_effColPositions}
                 class:fs-card-on={_fsPositions}
                 style="--bucket-rows:{_bRowsPositions}">
          <CardHeader
            bind:isCollapsed={_colPositions}
            bind:isFullscreen={_fsPositions}
            bind:filter={_filterPositions}
            cardId="pulse-positions"
            label="Positions"
            onRefresh={refreshAllNow}
            refreshLoading={_refreshing}
            onDownload={() => gridPositions?.exportDataAsCsv({ fileName: 'positions.csv' })}
          >
            {#snippet left()}
              <span class="mp-bucket-label mp-bucket-label-positions">Positions</span>
              {#if accountPicker && availableAccounts.length > 0}
                <span class="mp-head-sep" aria-hidden="true"></span>
                <div class="mp-head-acct">
                  <AccountMultiSelect bind:value={positionsAccounts}
                    options={availableAccounts.map(a => ({ value: a, label: a }))}
                    ariaLabel="Filter Positions by broker account" />
                </div>
              {/if}
            {/snippet}
          </CardHeader>
          <div bind:this={gridPositionsEl} class="ag-theme-quartz ag-theme-algo bucket-grid"></div>
        </section>
        <section class="mp-bucket-wrap mp-bucket-holdings"
                 class:is-collapsed={_effColHoldings}
                 class:fs-card-on={_fsHoldings}
                 style="--bucket-rows:{_bRowsHoldings}">
          <CardHeader
            bind:isCollapsed={_colHoldings}
            bind:isFullscreen={_fsHoldings}
            bind:filter={_filterHoldings}
            cardId="pulse-holdings"
            label="Holdings"
            onRefresh={refreshAllNow}
            refreshLoading={_refreshing}
            onDownload={() => gridHoldings?.exportDataAsCsv({ fileName: 'holdings.csv' })}
          >
            {#snippet left()}
              <span class="mp-bucket-label mp-bucket-label-holdings">Holdings</span>
              {#if accountPicker && availableAccounts.length > 0}
                <span class="mp-head-sep" aria-hidden="true"></span>
                <div class="mp-head-acct">
                  <AccountMultiSelect bind:value={holdingsAccounts}
                    options={availableAccounts.map(a => ({ value: a, label: a }))}
                    ariaLabel="Filter Holdings by broker account" />
                </div>
              {/if}
            {/snippet}
          </CardHeader>
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
    style="left:{ctxMenu?.x}px;top:{ctxMenu?.y}px"
    role="menu">
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenChart(ctxMenu?.row)}>Chart →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenOptions(ctxMenu?.row)}>Open in Options →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenTicket(ctxMenu?.row)}>Place order →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenOrders(ctxMenu?.row)}>Orders →</button>
    <button class="ctx-item" role="menuitem" onclick={() => ctxOpenLog(ctxMenu?.row)}>Log →</button>
    <div class="ctx-sep"></div>
    {#if !ctxMenu?.row?.src?.w && !isDemo}
      <!-- ★ Add to watchlist — visible when the symbol is NOT already
           in the operator's watchlist. The other branch below shows
           the Remove counterpart. -->
      <button class="ctx-item" role="menuitem" onclick={() => ctxAddWatch(ctxMenu?.row)}>★ Add to watchlist</button>
    {/if}
    <button class="ctx-item" role="menuitem" onclick={() => ctxCopySymbol(ctxMenu?.row)}>Copy symbol</button>
    <div class="ctx-sep"></div>
    {#if isDetached(ctxMenu?.row?.tradingsymbol)}
      <button class="ctx-item" role="menuitem" onclick={() => { reattachSymbol(ctxMenu?.row); closeContextMenu(); }}>↩ Re-attach to group</button>
    {:else if ctxMenu?.row?.underlying}
      <button class="ctx-item" role="menuitem" onclick={() => { detachSymbol(ctxMenu?.row); closeContextMenu(); }}>↗ Detach from group</button>
    {/if}
    {#if hasOverrides}
      <button class="ctx-item" role="menuitem" onclick={() => { resetOverrides(); closeContextMenu(); }}>↻ Reset all overrides</button>
    {/if}
    {#if ctxMenu?.row?.src?.w && ctxMenu?.row?.watchlist_item_id != null && !isDemo}
      <div class="ctx-sep"></div>
      <button class="ctx-item ctx-item-danger" onclick={() => ctxRemoveWatch(ctxMenu?.row)}>Remove from watchlist</button>
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
     the `/` keyboard shortcut). Extracted to AddToPulseModal.svelte;
     all async backend-calling functions remain here in MarketPulse. -->
<AddToPulseModal
  bind:open={searchOpen}
  {lists} {focusedListId} {isDemo}
  bind:targetListId bind:newListName
  bind:symInput bind:typeInput bind:aliasInput
  bind:typeahead bind:typeaheadOpen
  bind:renameId={_renameId} bind:renameName={_renameName} bind:renameError={_renameError}
  onAdd={addRow}
  onDropList={dropList}
  onCommitRename={commitRename}
  onCancelRename={cancelRename}
  onSearchSymbols={searchSymbols}
  onPickTypeahead={pickFromTypeahead}
  onClose={closeSearch}
/>

<!-- Option-chain picker modal — opens when the operator picks an F&O
     underlying from the Add popup's typeahead. Previously this rendered
     as an inline row below the page chrome which felt jarring; modal
     form keeps the Add flow visually contiguous. Click-overlay / Esc
     to dismiss; targets the watchlist chosen in the Add popup via
     _resolveTargetListId. -->
<ModalShell open={!!optionPickerUnderlying} onClose={closeOptionPicker} ariaLabel="Pick option strike">
    <div class="search-modal" role="presentation" onclick={(e) => e.stopPropagation()}>
      <div class="search-header">
        <span class="search-title">{optionPickerUnderlying?.name} — pick contract</span>
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
          <span class="flex rounded overflow-hidden border border-[var(--c-action)]/25">
            <button type="button"
              onclick={() => optionPickerSide = 'CE'}
              class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                     {optionPickerSide === 'CE'
                       ? 'bg-[var(--c-action)] text-[#0a1628]'
                       : 'text-[var(--c-muted)] hover:bg-[var(--c-action)]/10'}">CE</button>
            <button type="button"
              onclick={() => optionPickerSide = 'PE'}
              class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                     {optionPickerSide === 'PE'
                       ? 'bg-[var(--c-action)] text-[#0a1628]'
                       : 'text-[var(--c-muted)] hover:bg-[var(--c-action)]/10'}">PE</button>
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
</ModalShell>

<style>
  /* Mobile touch-target — WCAG 2.5.8 minimum 24px; aim for 36px on
     phones. !important is required to beat ag-Grid's inline row-height
     style (set via rowHeight: 28 in the grid options). Applies
     uniformly to BOTH grid columns (left + right; all use
     .ag-theme-algo). Desktop (>720px) honors the rowHeight: 28
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
  :global(.sym-main.sym-ce) { color: var(--c-long); }
  :global(.sym-main.sym-pe) { color: var(--c-short); }
  :global(.sym-alias) { color: var(--c-muted); font-size: var(--fs-xs); }

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
    font-size: var(--fs-xs);
    font-weight: 700;
    line-height: 12px;
    border-radius: 2px;
    font-variant-numeric: tabular-nums;
  }
  :global(.badge-p) { color: var(--algo-sky); background: var(--algo-sky-bg);   }
  :global(.badge-h) { color: var(--c-long); background: var(--algo-green-bg); }
  :global(.badge-w) { color: var(--c-action); background: var(--algo-amber-bg); }
  :global(.badge-u) { color: #c084fc; background: rgba(192,132,252,0.14); }
  :global(.badge-m-pos) { color: var(--c-long); background: var(--algo-green-bg); }
  :global(.badge-m-neg) { color: var(--c-short); background: var(--algo-red-bg);   }
  /* Covered-call lot-count badge — green pill with the number of whole
     lots the operator holds. Same pill family as the H/P/W/U badges so
     the row's badge strip reads as one consistent set. Bolder
     background than the others to draw the operator's eye to the
     actionable column (the rest are informational tags). */
  :global(.badge-fno-lot) {
    color: #052e16;
    background: var(--c-long);
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
    background: var(--c-action);
  }

  /* Inline remove-from-watchlist button. */
  :global(.sym-remove) {
    display: inline-block;
    margin-left: 6px;
    padding: 0 4px;
    color: rgba(248,113,113,0.45);
    font-size: var(--fs-lg);
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-remove:hover) {
    color: var(--c-short);
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
    font-size: var(--fs-lg);
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-actions:hover) {
    color: var(--c-action);
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
    font-size: var(--fs-xs);
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    opacity: 0;
    transition: opacity 0.12s ease, color 0.12s ease, background 0.12s ease;
  }
  :global(.ag-row:hover .sym-move) { opacity: 1; }
  :global(.sym-move:hover) {
    color: var(--c-action);
    background: rgba(251,191,36,0.12);
  }

  /* Mobile / touch screens — hover doesn't trigger reliably, so the
     ▲/▼ buttons stay visible at low opacity by default. The ⋯ menu
     trigger is already always-visible. */
  @media (hover: none), (max-width: 768px) {
    :global(.sym-move) { opacity: 0.55; }
    :global(.sym-move:active) {
      color: var(--c-action);
      background: rgba(251,191,36,0.18);
    }
  }

  /* Day Δ / P&L cells. */
  :global(.cell-pos)  { color: var(--c-long) !important; }
  :global(.cell-neg)  { color: var(--c-short) !important; }
  :global(.cell-flat) { color: #94a3b8 !important; }
  /* P&L cell background tint — same colour family + same alphas as the
     /admin/derivatives Candidates panel (`.cand-pnl.cell-pos` etc.) so
     the two surfaces' P&L columns read with the same visual identity.
     Applied via the `mp-pnl-cell` marker class on Pulse's right-grid
     P&L / Day P&L / P&L % / Day % columns + the summary grids. */
  :global(.mp-pnl-cell.cell-pos)  { background-color: var(--algo-green-bg) !important; }
  :global(.mp-pnl-cell.cell-neg)  { background-color: var(--algo-red-bg) !important; }
  :global(.mp-pnl-cell.cell-flat) { background-color: rgba(148,163,184,0.08) !important; }
  :global(.cell-muted){ color: rgba(200,216,240,0.55) !important; }

  /* Pinned sub-group dividers — first row of each pinned category
     (idx / fx / commodity) carries `.pin-divider` so the three
     mini-sections at the top of the grid feel distinct. Thin amber
     top-border, no other visual change to row contents. The FIRST
     idx row (very first row in the grid) skips the divider since
     there's nothing above it to separate from. */
  :global(.ag-theme-algo .ag-row.pin-divider) {
    border-top: 1px solid var(--algo-amber-border-soft);
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
  /* Operator 2026-07-01: "remove the border around ag grids in pulse. it
     is only outer border." The ag-Grid default draws a 1px --ag-border-color
     ring around .ag-root-wrapper. Card chrome already provides visual
     containment, so the outer ring reads as duplicate chrome. Kill it on
     Pulse's three grid classes (bucket / summary / funds) only — other
     surfaces (derivatives, dashboard) keep the ring. Inner row / cell
     borders are unaffected (they come from --ag-row-border-* variables). */
  :global(.bucket-grid .ag-root-wrapper),
  :global(.summary-grid .ag-root-wrapper),
  :global(.funds-grid .ag-root-wrapper) {
    border: none !important;
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
       the column width.

       Outer chrome matches .algo-grid-chrome (Legs, Snapshot, History,
       strategies) — 1.5px slate border + 4px radius + navy inset
       shadow + gradient bg. Gives each bucket a sharp card boundary
       instead of relying only on the inner ag-Grid theme border,
       which reads softer against the dark surface. Operator:
       "have sharp border to pulse ag grids like legs ag grid." */
    width: 100%;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    min-width: 0;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    background: var(--card-bg-elevated);
    overflow: hidden;
  }
  /* Bucket label — small mono caps above each grid, tinted to match
     the per-major palette already used elsewhere on the page so the
     six grids feel like the same family seen from the top. Typography
     tokens locked to canonical .algo-card-title; only COLOR varies
     per bucket (semantic — positions/holdings/winners/losers/watch). */
  .mp-bucket-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.04em;
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

  /* Label lives inside CardHeader's .ch-left flex row —
     neutralise the base margin-bottom so it centres correctly. */
  .mp-bucket-label { margin-bottom: 0; }
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
  /* Sparkline cell exception — restore right border as visual separator
     between the 5d chart column and LTP. */
  :global(.mp-bucket-wrap .ag-theme-algo .ag-cell.spark-cell) {
    border-right: 1px solid var(--algo-amber-border-soft) !important;
  }
  /* Header underline retired — `.ag-theme-algo .ag-header` (app.css)
     already applies `border-bottom: 1px solid var(--algo-amber-border-soft)`
     from the History-parity treatment. Adding a second border on
     .ag-header-row stacked at the same pixel, darkening the line via
     alpha compositing so Pulse's underline read heavier than every
     other grid on the platform. Operator: "in pulse the grid header
     underline is not consistent with other grids." */
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
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
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
     per operator feedback; combined with the 0.22 amber bg + bold,
     the row reads as a footer without visually shouting. Aggregate
     is direction-agnostic so no green/red variants here (the
     per-cell P&L text inside still uses cell-pos / cell-neg for
     value-level direction). */
  :global(.ag-theme-algo .mp-total-row) {
    font-weight: 700;
    /* Canonical TOTAL typography — uppercase amber monospace so the row
       reads as an aggregate footer, not a data row. Matches the TOTAL
       treatment on PerformancePage and the audit canonical spec. */
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #fbbf24;
    /* Stronger amber stratum so TOTAL stands out over data-row
       directional tints + (incoming) LTP heat cells. Operator:
       "total row should have a different background color scheme."
       Layered over opaque #1d2a44 base — prevents scrolled rows
       from bleeding through the pinned-bottom TOTAL row. */
    background:
      linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22)),
      #1d2a44 !important;
    border-top: 2px solid rgba(251, 191, 36, 0.70) !important;
    border-bottom: 1px solid rgba(251, 191, 36, 0.55) !important;
  }
  /* TOTAL row cell dividers — amber hairline so the aggregate row
     reads as one visual unit with clear column boundaries. */
  :global(.ag-theme-algo .mp-total-row .ag-cell) {
    border-right: 1px solid rgba(251, 191, 36, 0.30) !important;
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
    padding: 0 0 0.4rem;
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
         bit in pulse page".
         Side padding zeroed so cards fill the full viewport width
         edge-to-edge on mobile — operator: "Pulse cards should take
         full width mobile." The bucket-label has its own 0.35rem
         horizontal padding so text never touches the screen edge. */
      padding: 0 0 0.3rem;
    }
    .mp-layout {
      /* On mobile the layout is a normal column stack — no height
         constraint so all buckets render at fixed heights and the
         page scrolls. Reset the flex-fill set for desktop.
         Reduce gap from 0.6rem to 0.3rem so the between-column
         spacing matches the tighter cadence other algo pages use
         on mobile (operator: "I see gaps in pulse which is not
         there in other pages on mobile"). */
      flex: none;
      gap: 0.3rem;
    }
    .mp-col {
      /* Mirror the reduced gap inside each column so all inter-bucket
         spacing is uniform at 0.3rem on mobile. */
      gap: 0.3rem;
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
    z-index: var(--z-tooltip);
    min-width: 10rem;
    background: rgba(10,22,40,0.97);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 5px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.55);
    padding: 0.25rem 0;
    font-size: var(--fs-md);
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
    font-size: var(--fs-md);
    white-space: nowrap;
    transition: background 0.1s, color 0.1s;
  }
  :global(.ctx-item:hover) {
    background: rgba(251,191,36,0.1);
    color: var(--c-action);
  }
  :global(.ctx-item-danger) { color: rgba(248,113,113,0.8); }
  :global(.ctx-item-danger:hover) { background: rgba(248,113,113,0.1); color: var(--c-short); }
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
  .mp-head-sep {
    width: 1px;
    align-self: stretch;
    background: var(--sep-color);
    flex-shrink: 0;
    margin: var(--sep-margin);
  }
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
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .mp-head-tabs::-webkit-scrollbar { display: none; }

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
    font-size: var(--fs-xl);
    line-height: 1;
    font-weight: 700;
    color: var(--c-action);
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
     read as distinct without needing tabs. Canonical .algo-card-title
     tokens (operator 2026-07-01: "GREEKS is good, make uniform"). */
  :global(.mp-add-section-label) {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--c-action);
    margin-bottom: 0.35rem;
  }
  /* Divider between the two Add-popup sections — faint horizontal
     rule that mirrors the algo theme's hairline accents. */
  :global(.mp-add-divider) {
    height: 1px;
    background: rgba(200, 216, 240, 0.10);
    margin: 0.85rem 0 0.6rem;
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
  /* Modal title inside the Add / option-picker popup — canonical
     .algo-card-title tokens so it reads at the same intensity as the
     card headings behind it (operator: "GREEKS is good"). Was fs-lg
     with 0.05em spacing — slightly heavier than the platform default. */
  :global(.search-title) {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--c-action);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  :global(.search-close) {
    font-size: 1.05rem;
    line-height: 1;
    color: var(--c-muted);
    background: transparent;
    border: none;
    padding: 0 0.25rem;
    cursor: pointer;
  }
  :global(.search-close:hover) { color: var(--c-action); }
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
    font-size: var(--fs-lg);
    background: transparent;
    border: none;
    cursor: pointer;
  }
  :global(.search-typeahead-item:hover) { background: rgba(251, 191, 36, 0.1); }
  :global(.search-hint) {
    font-size: var(--fs-sm);
    color: var(--c-muted);
    line-height: 1.4;
  }

  /* ltp-flash-up / ltp-flash-down keyframes defined in app.css */

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
