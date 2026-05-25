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

  import { onMount, onDestroy, tick } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist,
    deleteWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchWatchlistQuotes,
    fetchPositions, fetchHoldings, fetchAccounts, fetchFunds, batchQuote,
    fetchMovers, fetchSparklines, fetchBrokerAccounts,
  } from '$lib/api';
  import { visibleInterval, clientTimestamp } from '$lib/stores';
  import { createPerformanceSocket } from '$lib/ws';
  import { priceFmt, pctFmt, aggCompact, qtyFmt, directional } from '$lib/format';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import Select      from '$lib/Select.svelte';

  let {
    title              = 'Market Pulse',
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
  let activeLists = $state(/** @type {any[]} */ ([]));
  let watchQuotes = $state(/** @type {Record<number, any>} */ ({}));
  // "Target" list for add / rename / delete operations. Defaults to
  // the first selected list; updated when the operator clicks a tab
  // (set focus AND toggle inclusion in one click).
  let focusedListId = $state(/** @type {number | null} */ (null));
  let positions   = $state(/** @type {any[]} */ ([]));
  let holdings    = $state(/** @type {any[]} */ ([]));
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

  // Add-symbol form state.
  let symInput   = $state('');
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
  async function addToWatchlistDeduped(targetId, sym, exch) {
    const needle = String(sym || '').toUpperCase();
    if (!needle) return false;
    const already = (rows) => rows.some(
      r => String(r.symbol || r.tradingsymbol || '').toUpperCase() === needle
    );
    if (already(positions) || already(holdings)) {
      return false;  // silent skip — already present in positions/holdings
    }
    await addWatchlistItem(targetId, sym, exch);
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
    { value: 'movers',    label: 'Movers'    },
  ];
  const _availableSourceValues = $derived(new Set([
    ...(enablePinned     ? ['pinned']    : []),
    ...(enableWatchlists ? ['watchlist'] : []),
    'positions',
    'holdings',
    ...(enableMovers     ? ['movers']    : []),
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
    'src:pinned', 'src:positions', 'src:holdings', 'src:movers',
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
    if (sig === '') { activeLists = []; return; }
    loadActive();
  });

  // Keep individual booleans so buildUnified + other callsites are unchanged.
  let showWatchlist = $state(true);
  let showPinned    = $state(true);
  let showPositions = $state(true);
  let showHoldings  = $state(true);

  // Movers — top-% movers from /watchlist/movers. Polled every 30 s.
  // Failure is non-fatal: the rest of the page keeps working.
  let movers     = $state(/** @type {any[]} */ ([]));
  let showMovers = $state(true);

  $effect(() => {
    showPinned    = selectedSources.includes('pinned');
    showWatchlist = selectedSources.includes('watchlist');
    showPositions = selectedSources.includes('positions');
    showHoldings  = selectedSources.includes('holdings');
    showMovers    = selectedSources.includes('movers');
  });
  let stopMoversPoll;

  // Account-picker state. Now a MultiSelect — operator can scope
  // positions / holdings INPUTS to buildUnified to any subset of
  // broker accounts. EMPTY array = all accounts (no filter); the
  // sentinel here is `length === 0` rather than the old 'all' string.
  // Watchlist + option-underlying rows are not account-scoped —
  // they always show.
  //
  // Both the old single-value `selectedAccount` and the new array
  // `selectedAccounts` co-exist here only as a one-call mental model
  // — every downstream check reads `selectedAccounts`. The picker
  // persists to sessionStorage so the filter survives a refresh.
  let selectedAccounts = $state(/** @type {string[]} */ ([]));
  // Derived membership predicate — `true` when EITHER no filter is
  // applied (length === 0) OR the given account is in the chosen set.
  // Hot-path helper used by every per-account derivation below.
  //
  // IMPORTANT: we read selectedAccounts at derivation time (via the
  // length+map below) so Svelte 5's reactivity subscribes this
  // derivation to selectedAccounts. The earlier version
  //   $derived((acct) => selectedAccounts.length === 0 || ...)
  // returned a fresh arrow function each call but the derivation body
  // itself NEVER touched selectedAccounts — Svelte 5 had no
  // subscription, so the function captured the initial empty array and
  // never re-derived. Result: Account picker label updated but
  // scopedPositions / scopedHoldings stayed unfiltered.
  const _includesAccount = $derived.by(() => {
    const allow = selectedAccounts.length === 0
      ? null
      : new Set(selectedAccounts.map(String));
    return (/** @type {any} */ acct) =>
      allow === null || allow.has(String(acct || ''));
  });
  let availableAccounts = $state(/** @type {string[]} */ ([]));
  // Broker-registry-loaded accounts — surfaced via /api/admin/brokers
  // on mount. Unioned into availableAccounts so the Account picker
  // lists EVERY broker account the operator added via /admin/brokers,
  // even ones with 0 positions / 0 holdings (so the operator can
  // confirm the row exists and is loaded). Empty fallback when the
  // endpoint is admin-gated for the current session.
  let _knownBrokerAccounts = $state(/** @type {string[]} */ ([]));
  // Latches when the Account seed has firmed up after the broker
  // fetch resolved — prevents re-seeding from clobbering operator
  // toggles on later loadPulse polls.
  let _seededFromBrokers = false;

  // Persist account-multiselect to sessionStorage on change so the
  // filter survives a tab refresh; cleared per session.
  $effect(() => {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem('mp.selectedAccounts', JSON.stringify(selectedAccounts));
    } catch (_) { /* quota / blocked — silent. */ }
  });
  // Persist the unified Show filter alongside. Without this, the
  // operator's deselected sources / watchlists reset on every refresh
  // (defaults re-seed everything ON). Same sessionStorage scope as
  // selectedAccounts so the two pickers feel symmetric.
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
  let funds = $state(/** @type {any[]} */ ([]));

  let sparklines = $state(/** @type {Record<string, number[]>} */ ({}));
  let stopSparkPoll;
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
    const mg = row?._majorGroup;
    if (mg === 'pinned' || mg === 'watchlist') return 1;
    if (mg === 'positions') return 2;
    if (mg === 'holdings')  return 3;
    if (mg === 'movers')    return 4;
    // Legacy fallback for callers that don't run through mainRows
    // (the watchlist add/remove helpers up at line ~384 hit this).
    if (row?.src?.w) return 1;
    if (row?.src?.p) return 2;
    if (row?.src?.h) return 3;
    return 4;
  }
  let stopPoll, stopPulsePoll;
  // Wall-clock timestamp (ms) of the last loadPulse() completion.
  // The 5 s loadQuotes poll consults this to skip ticks that land
  // within a 700 ms window of a loadPulse — the two pollers used to
  // collide on every 10 s boundary, hammering the backend with
  // /positions + /holdings + /quote/batch + N × /watchlist/{id}/quotes
  // simultaneously. Within-window skip preserves up-to-5 s freshness
  // on the watchlist while removing the thundering herd.
  let _lastPulseAt = 0;
  let gridEl;
  // $state on the bind:this refs so Svelte 5's reactive-update
  // analyzer doesn't warn (gridEl was grandfathered in pre-Phase 2;
  // the new summary / funds refs need the explicit annotation).
  let positionsSummaryEl = $state(/** @type {HTMLDivElement | null} */ (null));
  let holdingsSummaryEl  = $state(/** @type {HTMLDivElement | null} */ (null));
  let fundsEl            = $state(/** @type {HTMLDivElement | null} */ (null));
  let grid;
  let positionsSummaryGrid;
  let holdingsSummaryGrid;
  let fundsGrid;
  // Sentinel that flips to true once createGrid runs — used by the
  // $effect below to push unifiedRows into ag-Grid the moment both
  // (a) the grid is mounted and (b) the derived row set has populated.
  let gridReady              = $state(false);
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
      const res = await fetchSparklines(pairs, 5);
      if (res?.data && typeof res.data === 'object') {
        sparklines = { ...sparklines, ...res.data };
      }
    } catch (_) { /* non-fatal — sparklines are cosmetic */ }
  }

  onMount(async () => {
    // Auth is enforced by the (algo) layout — no goto('/signin')
    // here so this component can also be embedded in flows that
    // intentionally allow anonymous demo viewers.
    loadOverrides();
    // Restore previous account-multiselect from sessionStorage so
    // the filter survives a tab refresh. Wrapped in try/catch
    // because cached account codes may no longer exist on the
    // current server (operator switched broker accounts).
    if (accountPicker) {
      try {
        const cached = sessionStorage.getItem('mp.selectedAccounts');
        if (cached) selectedAccounts = JSON.parse(cached) || [];
      } catch (_) { selectedAccounts = []; }
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
    // Broker-accounts fan-out — admin-only endpoint. Fire-and-forget;
    // if it 403s for non-admin users the picker simply falls back to
    // rows-based account discovery. When it succeeds, every loaded
    // broker account lands in _knownBrokerAccounts and the next
    // loadPulse() unions them into availableAccounts.
    const brokersP = accountPicker
      ? fetchBrokerAccounts().then((arr) => {
          if (Array.isArray(arr)) {
            _knownBrokerAccounts = arr
              .filter((a) => a?.account)
              .map((a) => String(a.account));
          }
        }).catch(() => { _knownBrokerAccounts = []; })
      : Promise.resolve();

    // Block onMount only on the data the first paint actually needs.
    // Sparklines run fire-and-forget (cosmetic; missing them shows the
    // grid without the inline trend column for ≤1 s).
    await Promise.all([instrumentsP, accountsP, listsP, pulseP, fundsP, moversP, brokersP]);
    loadSparklines();

    stopPoll      = visibleInterval(async () => { await loadQuotes(); }, 5000);
    stopPulsePoll = visibleInterval(async () => {
      await loadPulse();
      if (showFunds) await loadFunds();
    }, 10000);
    if (enableMovers) stopMoversPoll = visibleInterval(loadMovers, 30000);
    stopSparkPoll   = visibleInterval(loadSparklines, 60000);

    // Real-time order-fill push — Kite postback fires a WS event
    // `position_filled` the moment an order fills. Subscribe so
    // Market Pulse refreshes positions + holdings IMMEDIATELY
    // instead of waiting up to 10 s for the next loadPulse tick.
    // Other (non-fill) events on the same socket also trigger a
    // refresh — cheap to over-fetch, expensive to lag a fill.
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
  });

  async function loadFunds() {
    try {
      const r = await fetchFunds();
      funds = (r?.rows || []).slice();
    } catch (_) { /* nothing fatal */ }
  }

  async function loadMovers() {
    try {
      const r = await fetchMovers();
      movers = (r?.movers || []).slice();
    } catch (e) {
      console.warn('[MarketPulse] movers fetch failed:', e?.message || e);
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
    rows.sort((a, b) => {
      const moA = a._majorOrder ?? 99, moB = b._majorOrder ?? 99;
      if (moA !== moB) return moA - moB;
      const ua = _ugKey(a), ub = _ugKey(b);
      if (ua !== ub) return ua.localeCompare(ub);
      const ta = _tier(a),  tb = _tier(b);
      if (ta !== tb) return ta - tb;
      return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
    });
    let lastMajor = null;
    for (const r of rows) {
      r._majorFirst = (r._majorGroup !== lastMajor);
      lastMajor = r._majorGroup;
    }
    return rows;
  });

  $effect(() => {
    if (!gridReady || !grid) return;
    grid.setGridOption('rowData', mainRows);
    grid.setGridOption('pinnedTopRowData', pinnedTopRows);
    // Curve column is ALWAYS visible — cells render `—` when sparkline
    // data is missing (sparkRenderer handles that). The earlier
    // auto-hide-when-empty logic interacted badly with column-state
    // persistence: any sort/resize after the column went hidden saved
    // the hidden state to localStorage, leaving the column permanently
    // gone across reloads even after data came back.
  });

  // Per-source summary derivations for the two separate summary grids.
  // Account picker scopes the body rows; TOTAL pinned at the bottom.
  function isTotalRow(r) { return r && r.account === 'TOTAL'; }

  // Positions Summary — Day P&L + Day % + P&L per account.
  const positionsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('positions')) return [];
    const byAcct = /** @type {Record<string, any>} */ ({});
    for (const r of positionsSummary) {
      const a = r.account;
      if (!a) continue;
      // Honour the account multiselect — without this, summary rows
      // showed every account regardless of the picker. Main grid rows
      // already respect _includesAccount via scopedPositions; summary
      // was the missing surface.
      if (!_includesAccount(a)) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, _inv_val: 0 };
      byAcct[a].day_pnl  += Number(r.day_change_val) || 0;
      byAcct[a].pnl      += Number(r.pnl)            || 0;
      byAcct[a]._inv_val += Number(r.inv_val)         || 0;
    }
    // Derive weighted-average percentages after accumulating the sums.
    for (const row of Object.values(byAcct)) {
      const iv = row._inv_val;
      row.day_change_percentage = iv ? (row.day_pnl / iv) * 100 : null;
      delete row._inv_val;
    }
    return Object.values(byAcct);
  });

  // Holdings Summary — Day P&L + Day % + P&L + P&L % + Cur Val + Inv Val per account.
  const holdingsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('holdings')) return [];
    const byAcct = /** @type {Record<string, any>} */ ({});
    for (const r of holdingsSummary) {
      const a = r.account;
      if (!a) continue;
      // Same account-multiselect filter as positionsSummaryData above —
      // backend summary rows arrive per-account; we need to scope by
      // the operator's picker before aggregating.
      if (!_includesAccount(a)) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, cur_val: 0, inv_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
      byAcct[a].pnl     += Number(r.pnl)            || 0;
      byAcct[a].cur_val += Number(r.cur_val)         || 0;
      byAcct[a].inv_val += Number(r.inv_val)         || 0;
    }
    // Derive weighted-average percentages after accumulating the sums.
    for (const row of Object.values(byAcct)) {
      const iv = row.inv_val;
      row.day_change_percentage = iv ? (row.day_pnl / iv) * 100 : null;
      row.pnl_percentage        = iv ? (row.pnl     / iv) * 100 : null;
    }
    return Object.values(byAcct);
  });

  const positionsSummaryBody  = $derived(
    positionsSummaryData.filter(r => !isTotalRow(r) && _includesAccount(r.account))
  );
  const positionsSummaryTotal = $derived(positionsSummaryData.filter(isTotalRow));

  const holdingsSummaryBody  = $derived(
    holdingsSummaryData.filter(r => !isTotalRow(r) && _includesAccount(r.account))
  );
  const holdingsSummaryTotal = $derived(holdingsSummaryData.filter(isTotalRow));

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
    // Filter funds by the active account-multiselect (empty = all).
    const list = funds.filter(r => isTotalRow(r) || _includesAccount(r.account));
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
    stopPoll?.(); stopPulsePoll?.(); stopMoversPoll?.(); stopSparkPoll?.(); stopWS?.();
    document.removeEventListener('keydown', handleKeydown);
    document.removeEventListener('click', onDocClick);
    grid?.destroy?.();
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

  async function loadActive() {
    error = '';
    const ids = [...activeIds];
    if (ids.length === 0) {
      activeLists = [];
      watchQuotes = {};
      return;
    }
    try {
      const results = await Promise.all(
        ids.map(id => fetchWatchlist(id).catch(() => null))
      );
      activeLists = results.filter(Boolean);
      await loadQuotes();
    } catch (e) { error = e.message; }
  }

  async function loadQuotes() {
    const ids = [...activeIds];
    if (ids.length === 0) {
      watchQuotes = {};
      return;
    }
    // De-thundering-herd: skip this tick if loadPulse just ran. The
    // backend is already handling /positions + /holdings + a /quote/
    // batch from that pulse; piling N watchlist /quotes calls on top
    // of the same network burst stalled the response on slower links.
    // Watchlist data is still refreshed at most 5 s late vs the
    // intent — operator can't perceive a sub-second skip.
    if (Date.now() - _lastPulseAt < 700) return;
    try {
      const results = await Promise.all(
        ids.map(id => fetchWatchlistQuotes(id).catch(() => null))
      );
      const map = /** @type {Record<number, any>} */ ({});
      let latestRefreshed = '';
      for (const r of results) {
        if (!r) continue;
        for (const q of (r.items || [])) map[q.item_id] = q;
        if (r.refreshed_at && r.refreshed_at > latestRefreshed) {
          latestRefreshed = r.refreshed_at;
        }
      }
      watchQuotes = map;
      refreshedAt = latestRefreshed;
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

  // Mirrors the backend `derivatives.underlying_ltp_key` /
  // `is_mcx_underlying` + `findNearestFuture` chain.
  const INDEX_LTP_KEY = {
    NIFTY:      { tradingsymbol: 'NIFTY 50',         exchange: 'NSE' },
    BANKNIFTY:  { tradingsymbol: 'NIFTY BANK',       exchange: 'NSE' },
    FINNIFTY:   { tradingsymbol: 'NIFTY FIN SERVICE', exchange: 'NSE' },
    MIDCPNIFTY: { tradingsymbol: 'NIFTY MID SELECT', exchange: 'NSE' },
    SENSEX:     { tradingsymbol: 'SENSEX',           exchange: 'BSE' },
    BANKEX:     { tradingsymbol: 'BANKEX',           exchange: 'BSE' },
  };
  const MCX_COMMODITIES = new Set([
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
    'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL', 'GOLDGUINEA',
    'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
    'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
    'ALUMINIUM', 'ALUMINI', 'NICKEL',
    'MENTHAOIL', 'COTTON', 'CASTORSEED', 'KAPAS', 'CARDAMOM',
  ]);
  const CDS_CURRENCIES = new Set(['USDINR']);

  function resolveUnderlying(name, findNearestFut) {
    const n = String(name || '').toUpperCase();
    if (!n) return null;
    const idx = INDEX_LTP_KEY[n];
    if (idx) {
      return {
        tradingsymbol: idx.tradingsymbol,
        exchange: idx.exchange,
        quoteKey: `${idx.exchange}:${idx.tradingsymbol}`,
        underlying_group: n,
        kind: 'spot',
      };
    }
    if (MCX_COMMODITIES.has(n)) {
      const fut = findNearestFut?.(n);
      if (fut?.s && fut?.e) {
        return {
          tradingsymbol: fut.s,
          exchange: fut.e,
          quoteKey: `${fut.e}:${fut.s}`,
          underlying_group: n,
          kind: 'fut',
        };
      }
      // MCX commodity with no resolvable nearest future — fall through
      // to a stub anchor (commodity name as tradingsymbol, no live
      // quote). Better than dropping the anchor entirely: GOLDM /
      // GOLDPETAL / similar mini contracts may have option positions
      // even when MCX hasn't published a matching nearest-future
      // tradingsymbol the instruments cache can find.
      return {
        tradingsymbol: n,
        exchange: 'MCX',
        quoteKey: `MCX:${n}`,   // synthetic; quote will silently miss
        underlying_group: n,
        kind: 'mcx',
      };
    }
    if (CDS_CURRENCIES.has(n)) {
      const fut = findNearestFut?.(n);
      if (fut?.s && fut?.e) {
        return {
          tradingsymbol: fut.s,
          exchange: fut.e,
          quoteKey: `${fut.e}:${fut.s}`,
          underlying_group: n,
          kind: 'fut',
        };
      }
      return null;
    }
    return {
      tradingsymbol: n,
      exchange: 'NSE',
      quoteKey: `NSE:${n}`,
      underlying_group: n,
      kind: 'spot',
    };
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
      const [p, h] = await Promise.all([
        fetchPositions().catch(() => ({ rows: [] })),
        fetchHoldings().catch(() => ({ rows: [] })),
      ]);
      positions = (p?.rows || []).slice();
      holdings  = (h?.rows || []).slice();
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
        for (const r of positions) if (r.account) accts.add(String(r.account));
        for (const r of holdings)  if (r.account) accts.add(String(r.account));
        for (const a of _knownBrokerAccounts) accts.add(String(a));
        const sorted = [...accts].sort();
        availableAccounts = sorted;
        // First-load seed of the Account picker. selectedAccounts
        // = union of (whatever we've seeded so far) + (newly-
        // discovered accounts) — this auto-extends to include broker
        // accounts that fetchBrokerAccounts() returned AFTER the
        // first loadPulse fired (e.g., empty-holdings Dhan / Groww
        // accounts that wouldn't surface from positions+holdings
        // rows). Operator toggles never get clobbered because we
        // only ADD, never REMOVE, and we stop adding once
        // _seededAccountsAt has run (persistence layer takes over).
        // Skipped entirely if persisted state was already restored.
        if (sorted.length > 0 && !_seededFromBrokers) {
          let restored = false;
          try {
            const cached = sessionStorage.getItem('mp.selectedAccounts');
            if (cached) {
              const parsed = JSON.parse(cached);
              if (Array.isArray(parsed) && parsed.length > 0) restored = true;
            }
          } catch (_) {}
          if (!restored) {
            const cur = new Set(selectedAccounts);
            let changed = false;
            for (const a of sorted) {
              if (!cur.has(a)) { cur.add(a); changed = true; }
            }
            if (changed) selectedAccounts = [...cur].sort();
            // Mark as seeded once the broker fetch has confirmed
            // (at least one wl_ token in there from the broker
            // registry, OR the operator has had time to interact).
            if (_knownBrokerAccounts.length > 0) _seededFromBrokers = true;
          } else {
            _seededFromBrokers = true;
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
      for (const r of positions) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NFO';
        if (sym) {
          addUnderlying(sym, 'positions');
          contractKeys.add(`${exch}:${sym}`);
        }
      }
      for (const r of holdings) {
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
    } catch (_) { /* nothing fatal */ }
  }

  // Account-scoped inputs to buildUnified. Empty selectedAccounts
  // (default) passes the raw arrays straight through. Otherwise
  // filter positions / holdings to the chosen set — watchlist items
  // and option underlyings are not account-scoped so they remain
  // visible.
  const scopedPositions = $derived(
    selectedAccounts.length === 0
      ? positions
      : positions.filter(r => _includesAccount(r.account))
  );
  const scopedHoldings = $derived(
    selectedAccounts.length === 0
      ? holdings
      : holdings.filter(r => _includesAccount(r.account))
  );

  // buildUnified reads groupOrder + detachedSymbols from module scope.
  // The previous comma-operator dep-tracking trick
  //   $derived(((groupOrder, detachedSymbols), buildUnified(...)))
  // was fragile — Svelte 5's compiler doesn't always pick up state
  // reads inside a function call evaluated as a comma-expression
  // sibling. Switched to $derived.by with explicit touches so the
  // compiler unambiguously sees both reads inside the derivation body.
  const unifiedRows = $derived.by(() => {
    // eslint-disable-next-line no-unused-expressions
    groupOrder; detachedSymbols;
    return buildUnified(
      activeLists, watchQuotes, scopedPositions, scopedHoldings, pulseQuotes, getInstrument,
      showPositions, showHoldings, movers, showMovers,
    );
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

  function buildUnified(actLists, wq, pos, hold, pq, getInst, includePos, includeHold, moverRows, includeMovers) {
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
        _avg_num: 0,
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
        const q = wq[it.id];
        const sym = String(q?.quote_symbol || it.tradingsymbol).toUpperCase();
        const row = get(sym, major);
        row.exchange      = row.exchange || it.exchange;
        row.tradingsymbol = sym;
        row.alias         = (q?.quote_symbol && q.quote_symbol !== it.tradingsymbol) ? it.tradingsymbol : null;
        if (row.watchlist_item_id == null) {
          row.watchlist_item_id = it.id;
          row.watchlist_list_id = list.id;
        }
        row.src.w = true;
        // Back-compat tag for the legacy isPinnedIndexRow check.
        if (major === 'pinned') row._fromPinnedList = true;
        row.ltp    = q?.ltp    ?? row.ltp    ?? null;
        row.bid    = q?.bid    ?? row.bid    ?? null;
        row.ask    = q?.ask    ?? row.ask    ?? null;
        row.change = q?.change ?? row.change ?? null;
        row.change_pct = q?.change_pct ?? row.change_pct ?? null;
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
      const liveQ = cq?.[`${exch}:${sym}`];
      if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else if (row.ltp == null) {
        row.ltp = r.last_price ?? null;
      }
      row.pnl     = (row.pnl     ?? 0) + (Number(r.pnl)            || 0);
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
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
      if (r.account) row.accounts.add(String(r.account));
      const liveQ = cq?.[`${exch}:${sym}`];
      if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else {
        if (row.ltp == null) row.ltp = r.last_price ?? null;
        if (r.day_change != null && row.change == null)
          row.change = Number(r.day_change);
        if (r.day_change_percentage != null && row.change_pct == null)
          row.change_pct = Number(r.day_change_percentage);
      }
      row.pnl     = (row.pnl     ?? 0) + (Number(r.pnl)            || 0);
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
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
    for (const [logicalName, q] of Object.entries(uq)) {
      const info = q._resolved;
      if (!info) continue;
      const anchorMajor = info._major || 'positions';
      if (anchorMajor === 'positions' && includePos  === false) continue;
      if (anchorMajor === 'holdings'  && includeHold === false) continue;
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
      if (row.change == null)     row.change     = q.change ?? null;
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
    const existingSymbols = new Set();
    for (const row of Object.values(byKey)) {
      if (row.tradingsymbol) existingSymbols.add(row.tradingsymbol);
    }
    for (const m of (moverRows || [])) {
      const sym = String(m.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      if (existingSymbols.has(sym)) {
        // Badge every existing row for this symbol (across majors).
        for (const row of Object.values(byKey)) {
          if (row.tradingsymbol === sym) {
            row.src.m = true;
            row._mover_sticky    = m.sticky ?? row._mover_sticky    ?? false;
            row._mover_change_pct = m.change_pct ?? row._mover_change_pct ?? null;
          }
        }
        continue;
      }
      // Pure mover — no other major qualification. Create a Movers row.
      const row = get(sym, 'movers');
      row.src.m = true;
      row.exchange      = row.exchange || m.exchange || 'NSE';
      row.tradingsymbol = sym;
      if (row.ltp == null && m.last_price != null)    row.ltp        = m.last_price;
      if (row.change_pct == null && m.change_pct != null) row.change_pct = m.change_pct;
      if (m.previous_close != null && row.change == null && row.ltp != null)
        row.change = row.ltp - m.previous_close;
      row._mover_sticky    = m.sticky ?? false;
      row._mover_change_pct = m.change_pct ?? null;
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

    // Finalise: weighted-avg avg_pos + directional day_pct.
    for (const row of Object.values(byKey)) {
      if (row.qty_pos !== 0) {
        row.avg_pos = row._avg_num / row.qty_pos;
      }
      delete row._avg_num;
      const netQty = (Number(row.qty_pos) || 0) + (Number(row.qty_hold) || 0);
      row.day_pct = directional(row.change_pct, netQty);
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
        const newToken = `wl:${w.id}`;
        if (!selectedShow.includes(newToken)) {
          selectedShow = [...selectedShow, newToken];
        }
        focusedListId = w.id;
        await loadLists();
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
      );
      symInput = ''; typeahead = []; typeaheadOpen = false;
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
      await addToWatchlistDeduped(targetId, inst.s, inst.e);
      symInput = ''; typeahead = []; typeaheadOpen = false;
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
    // Reset the inline new-list name so a stale name doesn't linger
    // when the popup reopens; targetListId is re-seeded by openSearch.
    newListName = '';
  }

  async function makeList() {
    if (!newListName.trim()) return;
    try {
      const w = await createWatchlist(newListName.trim());
      newListName = '';
      // Add the new list's token to the unified Show multiselect — this
      // is what causes its rows to appear in the grid (selectedShow is
      // now the source of truth; activeIds is derived from it).
      const newToken = `wl:${w.id}`;
      if (!selectedShow.includes(newToken)) {
        selectedShow = [...selectedShow, newToken];
      }
      focusedListId = w.id;
      await loadLists();
      await loadActive();
    } catch (e) { error = e.message; }
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
    const alias = row.alias ? `<span class="sym-alias"> → ${row.tradingsymbol}</span>` : '';
    const main  = row.alias || row.tradingsymbol || '';
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
    const removeBtn = (row.src?.w && row.watchlist_item_id != null)
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
    return `<span class="sym-main">${main}</span>${alias}${badgeHtml}${removeBtn}${moveBtns}${actionsBtn}`;
  }

  /**
   * Inline SVG sparkline for the last N daily closes.
   * ~60×18px, no axes/labels, stroke coloured by direction.
   * Missing data → em-dash placeholder.
   */
  function sparkRenderer(params) {
    const sym    = String((params.data || {}).tradingsymbol || '').toUpperCase();
    const closes = sparklines[sym];
    if (!closes || closes.length < 2) {
      return '<span style="color:#7e97b8;font-size:0.6rem;padding:0 4px">—</span>';
    }
    // Compact width (32px SVG inside a 36px column) — frees up
    // horizontal space so the LTP column stays visible on mobile.
    const W = 32, H = 14, PAD = 2;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const xStep = (W - PAD * 2) / (closes.length - 1);
    const yOf   = (v) => PAD + (1 - (v - min) / range) * (H - PAD * 2);
    const pts   = closes.map((v, i) => `${(PAD + i * xStep).toFixed(1)},${yOf(v).toFixed(1)}`).join(' ');
    const up    = closes[closes.length - 1] >= closes[0];
    const color = up ? 'rgba(91,142,149,0.85)' : 'rgba(196,122,61,0.85)';
    return `<svg width="${W}" height="${H}" style="display:block;overflow:visible"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
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
    if (s.p) {
      const q = Number(r.qty_pos) || 0;
      if (q < 0) classes.push('pos-short');
      else if (q > 0) classes.push('pos-long');
      else classes.push('row-pos');
    }
    else if (s.h) classes.push('row-hold');
    else if (s.w) classes.push('row-watch');
    else if (s.u) classes.push('row-und');
    return classes.join(' ');
  }

  function mountGrid() {
    const RA = 'ag-right-aligned-cell';
    const dirCellClass = (p) => `${RA} ${dirCls(p.value)}`;

    // Main symbols grid — only built when the parent opted into the
    // per-symbol view (/pulse). /dashboard passes showSymbolsGrid=false
    // because it shows only summary + funds; the summary/funds grids
    // below still need to mount in that case.
    if (showSymbolsGrid && gridEl) {

    const colDefs = /** @type {any[]} */ ([
      { field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
        cellRenderer: symRenderer, sortable: true,
        cellClass: 'ag-col-sym ag-col-fill' },
      // Curve column shrunk from 64 px → 36 px to free LTP/Δ% room on
      // mobile viewports. SVG dropped from 58×16 to 32×14 to match.
      { field: 'tradingsymbol', headerName: 'Curve', width: 36, colId: 'sparkline',
        cellRenderer: sparkRenderer, sortable: false, resizable: true,
        cellClass: 'spark-cell',
        headerClass: 'ag-header-cell-spark' },
      // Net qty held — positions + holdings summed (signed). When both
      // are zero the cell renders blank. Sortable so the operator can
      // sort by absolute exposure.
      { field: 'qty_net', headerName: 'Qty', width: 56, colId: 'qty_net',
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueGetter: (p) => {
          const q = (Number(p.data?.qty_pos) || 0) + (Number(p.data?.qty_hold) || 0);
          return q === 0 ? null : q;
        },
        valueFormatter: ({ value }) => value == null ? '' : qtyFmt(value) },
      { field: 'ltp', headerName: 'LTP', width: 80,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueFormatter: numFmt },
      { field: 'day_pnl', headerName: 'Day P&L', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'day_pct', headerName: 'Day %', width: 56,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: pctFmtGrid },
      { field: 'bid', headerName: 'Bid', width: 52,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'ask', headerName: 'Ask', width: 52,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'pnl', headerName: 'P&L', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'expiry', headerName: 'Expiry', width: 60,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: 'ag-right-aligned-cell cell-muted',
        valueFormatter: ({ value }) => value || '' },
    ]);

    grid = createGrid(gridEl, {
      theme: 'legacy',
      columnDefs: colDefs,
      rowData: unifiedRows,
      // Stable row identity — without this, ag-Grid treats every
      // rowData update (every 10 s poll) as a fresh dataset and
      // resets the scroll position to the top. The operator scrolls
      // down to see Holdings, the next poll fires, and the grid
      // yanks back to row 0. Defining getRowId via the key field
      // (set in buildUnified per (symbol, major)) lets ag-Grid match
      // pre/post rows and preserve scroll position + row state.
      getRowId: ({ data }) => String(data?.key || data?.tradingsymbol || ''),
      defaultColDef: {
        resizable: true, sortable: true, suppressMovable: true,
        suppressHeaderMenuButton: true,
      },
      sortingOrder: ['asc', 'desc', null],
      overlayNoRowsTemplate: '<span style="font-size:0.65rem;color:#7e97b8">No rows — add symbols to your watchlist or load positions/holdings</span>',
      // Fixed-height layout (not autoHeight) so ag-Grid's built-in
      // sticky header behaviour kicks in — data rows scroll inside
      // the grid viewport while the column headers stay pinned at
      // the top of the grid box. autoHeight was making the grid
      // grow with every new row, so when the operator scrolled the
      // page the header slid out of view with the rows. Fixed
      // height = sticky header + scrolling data area.
      // 28 px header + 28 px row × 18 rows + 6 px slack = ~520 px.
      // Long lists scroll inside the grid; short lists just leave
      // empty white space at the bottom which the operator can
      // tolerate (we'd otherwise have to swap autoHeight in
      // dynamically — overkill for the current row counts).
      domLayout: 'normal',
      getRowClass,
      rowHeight: 28,
      headerHeight: 28,
      // Sort never breaks bucket/group integrity. ag-Grid sorts normally
      // first (by whatever column the operator clicked), then this hook
      // re-stitches the sorted rows so:
      //   1. Bucket order (positions → holdings → other) is preserved
      //      regardless of sort direction.
      //   2. Within each bucket, rows are grouped by their underlying;
      //      the group's first-appearance position in the column-sorted
      //      list determines group order (so sorting by P&L surfaces
      //      the best-performing group first, not interleaved).
      //   3. Within each group, the column-sort order survives — so
      //      strikes still sort by LTP / Day P&L / whatever within
      //      their NIFTY (or BANKNIFTY, etc.) cluster.
      postSortRows: (params) => {
        const nodes = params.nodes;
        if (!nodes || nodes.length === 0) return;
        const groupOf = (n) =>
          String(n.data?.underlying || n.data?.tradingsymbol || '').toUpperCase();
        // Group by bucket → group key, recording first-appearance index
        // so we can order groups by their best column-sorted row.
        const bucketMap = /** @type {Record<number, Record<string, any[]>>} */ ({});
        const groupFirstSeen = /** @type {Record<string, number>} */ ({});
        for (let i = 0; i < nodes.length; i++) {
          const n = nodes[i];
          const b = bucketOf(n.data || {});
          const g = groupOf(n);
          (bucketMap[b] ??= {})[g] ??= [];
          bucketMap[b][g].push(n);
          if (!(g in groupFirstSeen)) groupFirstSeen[g] = i;
        }
        const out = [];
        const bucketKeys = Object.keys(bucketMap).map(Number).sort((a, b) => a - b);
        for (const b of bucketKeys) {
          const groups = bucketMap[b];
          const groupKeys = Object.keys(groups).sort(
            (a, b) => groupFirstSeen[a] - groupFirstSeen[b]
          );
          for (const gk of groupKeys) out.push(...groups[gk]);
        }
        params.nodes.length = 0;
        params.nodes.push(...out);
      },
      onRowClicked: handleRowClick,
      onSortChanged: saveColumnState,
      onColumnResized: saveColumnState,
      onGridReady: () => { restoreColumnState(); },
      onCellContextMenu: (ev) => {
        if (ev.data) openContextMenu(ev.event, ev.data);
      },
    });
    gridReady = true;
    }  // end main symbols grid block

    // Positions Summary grid — Account | Day P&L | Day % | P&L
    // Compact widths — aggCompact maxes at ~8 chars ("-999.99L") so
    // 78 px fits every rupee value plus the standard cell padding.
    if (showSummary && positionsSummaryEl) {
      const posSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 54,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
      ];
      positionsSummaryGrid = createGrid(positionsSummaryEl, {
        theme: 'legacy',
        columnDefs: posSummaryCols,
        rowData: [],
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
        { field: 'account',               headerName: 'Account', width: 54,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'pnl_percentage',        headerName: 'P&L %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'cur_val',               headerName: 'Cur Val', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'inv_val',               headerName: 'Inv Val', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
      ];
      holdingsSummaryGrid = createGrid(holdingsSummaryEl, {
        theme: 'legacy',
        columnDefs: holdSummaryCols,
        rowData: [],
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
        { field: 'account',        headerName: 'Account',   width: 54,
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
        { field: 'avail_margin',   headerName: 'Avail Mar', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Available margin — net trading buffer' },
        { field: 'used_margin',    headerName: 'Used Mar',  width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Margin currently locked against open positions' },
        { field: 'collateral',     headerName: 'Coll',      flex: 1, minWidth: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Collateral value from pledged holdings' },
      ];
      fundsGrid = createGrid(fundsEl, {
        theme: 'legacy',
        columnDefs: fundsCols,
        rowData: [],
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
    openTicket({
      symbol:   r.tradingsymbol,
      exchange: r.exchange,
      side,
      qty:      Math.abs(Number(r.qty_pos) || lot),
      lotSize:  lot,
      accounts: realAccounts,
      account:  preAccount,
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
    window.location.href = `/admin/options?symbol=${sym}`;
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
/** 📈 Chart action — opens SymbolPanel on the Chart tab for the
   *  row's symbol. Historical bars fetched lazily inside the panel
   *  when the Chart tab activates. Earlier this opened a separate
   *  SymbolChartModal; folded into the unified panel so chart +
   *  ticket + chain all live in one symbol-keyed surface. */
  function ctxOpenChart(row) {
    closeContextMenu();
    const sym = String(row.tradingsymbol || '').trim();
    if (!sym) return;
    openTicket({
      symbol:    sym,
      exchange:  String(row.exchange || '').trim(),
      defaultTab: 'chart',
    });
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

  // Dismiss context menu on outside click.
  function onDocClick(ev) {
    if (ctxMenu && ctxMenuEl && !ctxMenuEl.contains(/** @type {Node} */ (ev.target))) {
      closeContextMenu();
    }
  }

  // ── Keyboard shortcuts ────────────────────────────────────────────
  let pulseWrapper = $state(/** @type {HTMLElement | null} */ (null));
  /** @type {HTMLInputElement | null} */ let symInputEl = null;

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
      if (!grid) return;
      ev.preventDefault();
      const api = grid;
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
    }
  }

  // ── Column-state persistence (sort + width) ───────────────────────
  const COL_STATE_KEY = 'pulse.gridColumnState.v1';

  function saveColumnState() {
    if (!grid) return;
    try {
      const state = grid.getColumnState();
      localStorage.setItem(COL_STATE_KEY, JSON.stringify(state));
    } catch (_) {}
  }

  function restoreColumnState() {
    if (!grid) return;
    try {
      const raw = localStorage.getItem(COL_STATE_KEY);
      if (!raw) return;
      const state = JSON.parse(raw);
      // Force the sparkline (Curve) column visible — earlier builds
      // had an auto-hide effect that, paired with onSortChanged /
      // onColumnResized persistence, could pin `hide: true` into
      // localStorage. We removed the auto-hide; this evicts any
      // legacy persisted-hide so operators on stale state get the
      // Curve column back without clearing storage.
      const cleaned = Array.isArray(state)
        ? state.map(c => c?.colId === 'sparkline' ? { ...c, hide: false } : c)
        : state;
      grid.applyColumnState({ state: cleaned, applyOrder: true });
    } catch (_) {}
  }

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

  {#if enableWatchlists || enableSourceToggles || accountPicker}
    <!-- Single chrome row — every control left-aligned. The previous
         tabs strip + Sources picker have been consolidated into ONE
         "Show" MultiSelect that lists source toggles AND every
         watchlist as peer options (flat list per operator preference).
         Watchlists added via the `+` popup automatically appear in the
         dropdown via the reactive _showOptions derivation. -->
    <div class="mp-chrome-row mb-1">
      <div class="w-44 shrink-0">
        <MultiSelect bind:value={selectedShow} options={_showOptions} placeholder="Show…" />
      </div>
      {#if accountPicker && availableAccounts.length > 0}
        {@const _acctOff = !selectedSources.includes('positions')
                        && !selectedSources.includes('holdings')}
        <div class="w-28 shrink-0">
          <AccountMultiSelect bind:value={selectedAccounts}
            options={availableAccounts.map(a => ({ value: a, label: a }))}
            disabled={_acctOff}
            disabledReason="Account filter applies only when Positions or Holdings is selected" />
        </div>
      {/if}
      <button onclick={openSearch} title="Add symbol or watchlist  (/)"
        aria-label="Add symbol or watchlist"
        class="mp-add-btn">
        +
      </button>
    </div>
  {/if}

  <!-- Per-source subtotals strip (item 1) — shown when ≥1 source has rows. -->
  {#if hasSubtotals}
    <div class="subtotals-strip">
      {#if subtotals.hCount > 0}
        <span class="st-group">
          <span class="st-src">H</span>
          <span class="st-chip">Day <span class={subtotals.hDayPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.hDayPnl)}</span></span>
          <span class="st-chip">P&amp;L <span class={subtotals.hPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.hPnl)}</span></span>
          <span class="st-chip">Cur <span class="st-val">{aggCompact(subtotals.hCurVal)}</span></span>
        </span>
        {#if subtotals.pCount > 0 || subtotals.mCount > 0}<span class="st-sep">|</span>{/if}
      {/if}
      {#if subtotals.pCount > 0}
        <span class="st-group">
          <span class="st-src">P</span>
          <span class="st-chip">Day <span class={subtotals.pDayPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.pDayPnl)}</span></span>
          <span class="st-chip">P&amp;L <span class={subtotals.pPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.pPnl)}</span></span>
        </span>
        {#if subtotals.mCount > 0}<span class="st-sep">|</span>{/if}
      {/if}
      {#if subtotals.mCount > 0}
        <span class="st-group">
          <span class="st-src">M</span>
          <span class="st-chip">{subtotals.mCount} movers{subtotals.mTopSym ? '' : ''}</span>
          {#if subtotals.mTopSym && subtotals.mTopPct != null}
            <span class="st-chip"><span class="st-sym">{subtotals.mTopSym}</span> <span class={subtotals.mTopPct >= 0 ? 'st-pos' : 'st-neg'}>{subtotals.mTopPct >= 0 ? '+' : ''}{subtotals.mTopPct.toFixed(2)}%</span></span>
          {/if}
        </span>
      {/if}
    </div>
  {/if}

  <!-- Option picker is rendered as a modal at the BOTTOM of this
       component (search-overlay style) so it sits above all other
       page chrome. The inline-row version was disorienting because
       it appeared below the chrome row AFTER the Add popup closed,
       leaving the operator with no clear visual continuity. -->

  {#if showFunds}
    <!-- Funds strip — per-account Cash / Avail Margin / Used Margin /
         Collateral. Same first-impression order as the previous
         PerformancePage-driven /dashboard ("what's my cash" answers
         before "what's my P&L"). -->
    <div class="mp-section-label">Funds</div>
    <div bind:this={fundsEl} class="ag-theme-algo funds-grid mb-2"></div>
  {/if}

  {#if showSummary}
    <!-- Positions Summary — per-account Day P&L + P&L. -->
    <div class="mp-section-label">Positions Summary</div>
    <div bind:this={positionsSummaryEl} class="ag-theme-algo summary-grid mb-2"></div>
    <!-- Holdings Summary — per-account Day P&L + P&L + Cur Val. -->
    <div class="mp-section-label">Holdings Summary</div>
    <div bind:this={holdingsSummaryEl} class="ag-theme-algo summary-grid mb-2"></div>
  {/if}

  {#if showSymbolsGrid}
    {#if showSummary || showFunds}
      <div class="mp-section-label">Symbols</div>
    {/if}
    <!-- Unified grid — the per-symbol detail view. -->
    <div bind:this={gridEl} class="ag-theme-algo unified-grid"></div>
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
    <button class="ctx-item" onclick={() => ctxOpenChart(ctxMenu.row)}>📈 Chart →</button>
    <button class="ctx-item" onclick={() => ctxOpenOptions(ctxMenu.row)}>🧮 Open in Options →</button>
    <button class="ctx-item" onclick={() => ctxOpenTicket(ctxMenu.row)}>📝 Open ticket →</button>
    {#if !ctxMenu.row?.src?.w}
      <!-- ★ Add to watchlist — visible when the symbol is NOT already
           in the operator's watchlist. The other branch below shows
           the Remove counterpart. -->
      <button class="ctx-item" onclick={() => ctxAddWatch(ctxMenu.row)}>★ Add to watchlist</button>
    {/if}
    <button class="ctx-item" onclick={() => ctxCopySymbol(ctxMenu.row)}>Copy symbol</button>
    <div class="ctx-sep"></div>
    {#if isDetached(ctxMenu.row?.tradingsymbol)}
      <button class="ctx-item" onclick={() => { reattachSymbol(ctxMenu.row); closeContextMenu(); }}>↩ Re-attach to group</button>
    {:else if ctxMenu.row?.underlying}
      <button class="ctx-item" onclick={() => { detachSymbol(ctxMenu.row); closeContextMenu(); }}>↗ Detach from group</button>
    {/if}
    {#if hasOverrides}
      <button class="ctx-item" onclick={() => { resetOverrides(); closeContextMenu(); }}>↻ Reset all overrides</button>
    {/if}
    {#if ctxMenu.row?.src?.w && ctxMenu.row?.watchlist_item_id != null}
      <div class="ctx-sep"></div>
      <button class="ctx-item ctx-item-danger" onclick={() => ctxRemoveWatch(ctxMenu.row)}>Remove from watchlist</button>
    {/if}
  </div>
{/if}

<!-- Unified Add popup — opened by the `+` button in the chrome row (or
     the `/` keyboard shortcut). Single modal: pick a target watchlist
     (default / existing / new), then type a symbol + pick its type
     (EQ / FU / CE / PE), then Add. Click-outside / Esc to dismiss. -->
{#if searchOpen}
  <div class="search-overlay" role="dialog" aria-modal="true"
       aria-label="Add to Market Pulse" onclick={closeSearch}>
    <div class="search-modal" role="document" onclick={(e) => e.stopPropagation()}>
      <div class="search-header">
        <span class="search-title">Add to Market Pulse</span>
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
            {#if _tgtList && !_tgtList.is_default}
              <button type="button"
                onclick={async (e) => {
                  e.preventDefault();
                  // The enclosing {#if typeof targetListId === 'number'}
                  // guard narrows to number at the JS level, but JSDoc
                  // type-check sees the original number|'NEW' union, so
                  // we cast explicitly here.
                  const id = /** @type {number} */ (targetListId);
                  if (_pendingDeleteId === id) {
                    _pendingDeleteId = null;
                    await dropList(id);
                  } else {
                    _pendingDeleteId = id;
                    setTimeout(() => { _pendingDeleteId = null; }, 4000);
                  }
                }}
                class="text-[0.7rem] py-1 px-3 rounded font-bold border"
                style="background: rgba(248,113,113,0.2); color: #fda4af; border-color: rgba(248,113,113,0.55);"
                title={_pendingDeleteId === targetListId ? 'Click again to confirm delete' : `Delete "${_tgtList.name}" watchlist`}>
                {_pendingDeleteId === targetListId ? '× Confirm?' : '🗑 Delete'}
              </button>
            {/if}
          {/if}
        </div>
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
       aria-label="Pick option strike" onclick={closeOptionPicker}>
    <div class="search-modal" role="document" onclick={(e) => e.stopPropagation()}>
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
  /* Symbol cell — main + alias. */
  :global(.sym-main)  { color: #e2e8f0; font-weight: 600; }
  :global(.sym-alias) { color: #7e97b8; font-size: 0.55rem; }

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
  :global(.badge-p) { color: #38bdf8; background: rgba(56,189,248,0.18); }
  :global(.badge-h) { color: #4ade80; background: rgba(74,222,128,0.18); }
  :global(.badge-w) { color: #fbbf24; background: rgba(251,191,36,0.18); }
  :global(.badge-u) { color: #c4b5fd; background: rgba(196,181,253,0.18); }
  :global(.badge-m-pos) { color: #4ade80; background: rgba(74,222,128,0.18); }
  :global(.badge-m-neg) { color: #f87171; background: rgba(248,113,113,0.18); }

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

  /* Major-group dividers — first row of each major in mainRows
     (Watchlist → Positions → Holdings → Movers) gets a thicker
     amber top-border + a thin amber gradient strip above. The very
     first major (watchlist when active, else positions) doesn't
     skip the divider — pinnedTopRowData sits above mainRows so the
     transition from Pinned → next-major is visually meaningful.
     Per-major sub-tint controls the colour temperature of the
     divider (sky for positions/holdings activity-related, amber
     for watchlist/movers signal-related). */
  :global(.ag-theme-algo .ag-row.major-divider) {
    border-top: 2px solid rgba(251, 191, 36, 0.55);
    box-shadow: inset 0 1px 0 rgba(251, 191, 36, 0.18);
  }
  :global(.ag-theme-algo .ag-row.major-watchlist) {
    border-top-color: rgba(251, 191, 36, 0.55);
  }
  :global(.ag-theme-algo .ag-row.major-positions) {
    border-top-color: rgba(125, 211, 252, 0.55);
    box-shadow: inset 0 1px 0 rgba(125, 211, 252, 0.18);
  }
  :global(.ag-theme-algo .ag-row.major-holdings) {
    border-top-color: rgba(134, 239, 172, 0.55);
    box-shadow: inset 0 1px 0 rgba(134, 239, 172, 0.18);
  }
  :global(.ag-theme-algo .ag-row.major-movers) {
    border-top-color: rgba(196, 181, 253, 0.55);
    box-shadow: inset 0 1px 0 rgba(196, 181, 253, 0.18);
  }

  /* Grid containers */
  .unified-grid {
    width: 100%;
    /* Fixed-height grid pairs with `domLayout: 'normal'` (set when the
       grid is created) so ag-Grid pins the column header at the top
       of this box and only the data area scrolls when row count
       exceeds visible space. Height tuned for ~18 rows at 28 px each
       + 28 px header + 6 px slack. Operators with longer watchlists
       scroll inside the grid; the surrounding page no longer carries
       the header away when it scrolls. */
    height: 520px;
    min-height: 520px;
  }
  /* Flat-mode wrapper — used by /pulse to drop the .algo-status-card
     navy chrome. The page becomes a flex column whose unified grid
     grows to fill the remaining viewport height, so the operator
     gets the maximum number of rows on-screen with the column
     header pinned at the top. */
  .mp-flat-wrap {
    padding: 0.4rem;
    display: flex;
    flex-direction: column;
    /* 100vh minus navbar (4 rem) + page chrome (~3 rem). Fits inside
       the algo layout without overflowing the viewport. */
    min-height: calc(100vh - 7rem);
  }
  .mp-flat-wrap .unified-grid {
    /* Explicit calc-based height so ag-Grid's domLayout:normal viewport
       measurement is reliable on first mount. The previous
       `flex: 1 1 auto; height: auto;` setup rendered correctly in dev
       preview but in production the grid's container measured at 0 px
       during ag-Grid's synchronous mount, leaving subsequent
       setGridOption('rowData') calls invisible. A definite pixel
       height computed from viewport units sidesteps the issue while
       still filling the page (the calc subtracts navbar + page chrome
       + toolbar + sticky banners that sit between the navbar and
       the grid). */
    height: calc(100vh - 11rem);
    min-height: 320px;
    flex: none;
  }
  /* Mobile — the grid was rendering taller than the visible viewport,
     so the page itself scrolled (navbar disappeared as the operator
     swiped to see Holdings). Shrink the grid + lock the wrap to the
     dynamic viewport so the page chrome stays anchored and only the
     grid's internal scroll moves rows under a fixed header.
     `dvh` (dynamic viewport height) follows the URL-bar show/hide on
     mobile browsers, unlike plain `vh`. Buffer is 13rem: navbar
     (3rem) + accent strip + toolbar (multi-line on narrow viewports)
     + impersonation/sim/paper banners (sticky, occasionally up). */
  @media (max-width: 600px) {
    .mp-flat-wrap {
      min-height: 0;
      overflow: hidden;
      padding: 0.3rem;
    }
    .mp-flat-wrap .unified-grid {
      height: calc(100dvh - 13rem);
      min-height: 240px;
    }
  }
  .summary-grid,
  .funds-grid {
    width: 100%;
    min-height: 40px;
  }

  /* Section labels above each grid in dashboard mode. Small muted
     amber so they read as section headings without competing with
     the data rows or the toolbar. */
  .mp-section-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251,191,36,0.7);
    margin-bottom: 0.25rem;
  }

  /* Per-source subtotals strip (item 1) */
  .subtotals-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem;
    margin-bottom: 0.35rem;
    padding: 0.25rem 0.5rem;
    background: rgba(251,191,36,0.04);
    border: 1px solid rgba(251,191,36,0.12);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }
  .st-group {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .st-src {
    font-weight: 800;
    color: rgba(251,191,36,0.85);
    font-size: 0.6rem;
    letter-spacing: 0.06em;
  }
  .st-chip {
    color: rgba(200,216,240,0.65);
  }
  .st-val  { color: rgba(200,216,240,0.9); font-weight: 600; }
  .st-pos  { color: #4ade80; font-weight: 600; }
  .st-neg  { color: #f87171; font-weight: 600; }
  .st-sym  { color: rgba(251,191,36,0.75); }
  .st-sep  { color: rgba(200,216,240,0.2); padding: 0 0.15rem; }

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
  .mp-chrome-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.25rem;
    overflow: visible;
  }
  /* Tab buttons inside the row never shrink (would clip the label). */
  .mp-chrome-row > button { flex: 0 0 auto; white-space: nowrap; }

  /* Unified `+` add button — single chip at the end of the chrome row.
     Bigger glyph than the surrounding watchlist tabs so it reads as
     an action affordance rather than another tab. Same amber palette
     as the tab borders / Save buttons so it sits in the page palette. */
  .mp-add-btn {
    flex: 0 0 auto;
    padding: 0 0.55rem;
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
    color: #7e97b8;
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
    color: #7e97b8;
    line-height: 1.4;
  }
</style>
