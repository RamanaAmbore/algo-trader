<script>
  // OptionChainTab — option-chain basket builder extracted from
  // /admin/options/+page.svelte. Self-contained: loads instruments,
  // fetches spot + quotes, builds the basket, and calls
  // onBasketPlace(legs[]) when the operator places.
  //
  // The basket leg shape is the same dict that placeBasket() uses in
  // admin/options, so the parent shell can loop over legs and submit
  // via placeTicketOrder() without a new backend route.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { visibleInterval } from '$lib/stores';
  import { isMarketOpen } from '$lib/marketHours';
  import {
    fetchOptionsSpot, fetchChainQuotesPrices,
    placeTicketOrder,
    fetchAccounts,
    checkOrderSpread,
    previewTicketTemplate,
  } from '$lib/api';
  import { createSpreadGate, resolveWingTradingsymbol } from '$lib/data/spreadGate.js';
  import { executionMode } from '$lib/stores';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import Select from '$lib/Select.svelte';
  import TemplateBar from '$lib/TemplateBar.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listFutures, getInstrument,
    listStrikes, findOption,
    instrumentsCacheVersion,
  } from '$lib/data/instruments';
  import { POPULAR_UNDERLYINGS } from '$lib/data/popularUnderlyings';
  import { parseChainQuoteRow } from '$lib/data/chainQuotes';
  import { KITE_INDEX_QUOTE_KEY_TO_ROOT as _KITE_IDX_TO_ROOT } from '$lib/data/resolveUnderlying.js';
  import { priceFmt } from '$lib/format';
  // Order-template catalog — same source the OrderTicket uses.
  // Operator: "template should be applicable to option chain too".
  // Templates apply per-leg to each basket entry: when the leg fills,
  // the template runs (TP / SL / Wing) just like a single-leg ticket.
  import {
    loadOrderTemplates,
  } from '$lib/data/templates';

  /** @type {{
   *   symbol?:         string,
   *   account?:        string,
   *   accounts?:       string[],
   *   onBasketPlace?:  (result: {ok: number, fail: number}) => void,
   *   basketLegs?:     any[],
   *   onAddLeg?:       (leg: any) => void,
   *   onRemoveLeg?:    (leg: any) => void,
   *   onUpdateLeg?:    (key: string, updater: (leg: any) => any) => void,
   *   onSubmitBasket?: () => void,
   *   onClearBasket?:  () => void,
   *   onPlaceLeg?:     (props: any) => void,
   *   onAccountChange?: (account: string) => void,
   *   refreshKey?:     number,
   *   templateId?:     number | null,
   *   templateName?:   string,
   *   templateIsNone?: boolean,
   *   showTemplateBar?:  boolean,
   *   showDemoTplNote?:  boolean,
   *   selectedTemplate?: object | null,
   *   sideAwareDefault?: object | null,
   *   nonNoneTemplates?: any[],
   *   showsWing?:        boolean,
   *   shellUsingNone?:   boolean,
   *   tpOverride?:               number | '',
   *   slOverride?:               number | '',
   *   wingStrikeOffsetOverride?: number | '',
   *   wingPremPctOverride?:      number | '',
   *   spreadMaxPctOverride?:     number | '',
   *   onSelectDefault?:  () => void,
   *   onSelectNone?:     () => void,
   *   onSelectTemplate?: (id: number) => void,
   * }} */
  let {
    // Seed the underlying from a known symbol (e.g. NIFTY25APR22000CE → NIFTY).
    symbol    = '',
    account   = '',
    accounts  = /** @type {string[]} */ ([]),
    // Fired after the basket has been submitted. ok = filled count, fail = failed.
    onBasketPlace = /** @type {((r:{ok:number,fail:number})=>void)|undefined} */ (undefined),
    // When the shell passes these, the chain tab's basket is lifted to the
    // shell level — the tab reads from and writes to the shell's basketLegs.
    basketLegs    = /** @type {any[]|undefined} */ (undefined),
    onAddLeg      = /** @type {((leg: any) => void)|undefined} */ (undefined),
    onRemoveLeg   = /** @type {((leg: any) => void)|undefined} */ (undefined),
    onUpdateLeg   = /** @type {((key: string, updater: (leg: any) => any) => void)|undefined} */ (undefined),
    onSubmitBasket = /** @type {(() => void)|undefined} */ (undefined),
    onClearBasket  = /** @type {(() => void)|undefined} */ (undefined),
    // When Place mode is enabled, +/− route through this instead of
    // staging into the basket — the shell flips to the Ticket tab
    // pre-filled with the leg, mirroring CommandLineTab's
    // BUY/SELL → Ticket flow.
    onPlaceLeg     = /** @type {((p: any) => void)|undefined} */ (undefined),
    // Pushed back to OrderEntryShell when the operator changes the
    // routable account from this tab — shell syncs the other tabs
    // (command / ticket) to the same value.
    onAccountChange = /** @type {((a: string) => void)|undefined} */ (undefined),
    // Host-driven refresh — increments to force a chain re-fetch
    // (futures + strikes + ATM). Used by SymbolPanel on tab activation
    // so switching back to Chain always shows fresh data.
    refreshKey = 0,
    // Shared exit-template id across SymbolPanel surfaces. Bound by the
    // shell so a pick on the Ticket tab persists when the operator
    // flips to Chain (and vice versa). Standalone callers leave it
    // unbound — the chain falls back to 'none' on first paint.
    templateId = $bindable(/** @type {number|null} */ (null)),
    // Resolved template display info — owned by SymbolPanel's own
    // _selectedTemplate/_shellUsingNone (the single shared Template
    // Default/None picker below the tab body). Passed down, not
    // re-derived here, so the strike grid's per-leg badge can show
    // WHICH template the system auto-picked without duplicating the
    // template-catalog lookup the shell already owns.
    templateName   = /** @type {string} */ (''),
    templateIsNone = false,
    // Shell-computed visibility gates — replicate the exact original
    // SymbolPanel if/else-if show/hide logic for the Templ row
    // (`_templates.length > 0 && action === 'open' && (symbol-or-legs)`
    // for the live toggle, `_isDemo && action === 'open' && (symbol-
    // or-legs)` for the demo note) so this tab doesn't need its own
    // duplicate copy of `action`/`_templates.length` state. Computed
    // once in SymbolPanel where that state already lives; passed down
    // as plain booleans, mirroring the existing templateId/templateName
    // pass-through convention above.
    showTemplateBar = false,
    showDemoTplNote = false,
    // TemplateBar pass-through props — the toggle + expand panel now
    // render inline in this tab's expiry toolbar row (see markup
    // below) instead of SymbolPanel mounting <TemplateBar> itself.
    // Same prop shape TemplateBar.svelte declares; forwarded straight
    // through, selection callbacks bubble back up to the shell (which
    // owns `_sharedTemplateId` / the four override $state vars).
    selectedTemplate = /** @type {any} */ (null),
    sideAwareDefault = /** @type {any} */ (null),
    nonNoneTemplates = /** @type {any[]} */ ([]),
    showsWing        = false,
    shellUsingNone   = false,
    tpOverride               = $bindable(/** @type {number|''} */ ('')),
    slOverride               = $bindable(/** @type {number|''} */ ('')),
    wingStrikeOffsetOverride = $bindable(/** @type {number|''} */ ('')),
    wingPremPctOverride      = $bindable(/** @type {number|''} */ ('')),
    // Pre-submit spread-gate threshold (Chain-only). Shown in TemplateBar
    // alongside TP%/SL%; consumed by runPreSubmitGate() below.
    spreadMaxPctOverride     = $bindable(/** @type {number|''} */ ('')),
    onSelectDefault  = /** @type {(() => void)|undefined} */ (undefined),
    onSelectNone     = /** @type {(() => void)|undefined} */ (undefined),
    onSelectTemplate = /** @type {((id: number) => void)|undefined} */ (undefined),
  } = $props();

  // "Place" mode toggle — default OFF (Basket mode). Off: +/− stage
  // legs into the shared basket. On: +/− open the Ticket tab pre-
  // filled with that leg for direct submit, same as Command's flow.
  let _placeMode = $state(false);

  // Whether basket state is lifted to the shell or owned locally.
  const _externalBasket = $derived(basketLegs !== undefined && !!onAddLeg);

  // Curated priority list (indices + top NSE F&O stocks + MCX) imported
  // from `$lib/data/popularUnderlyings` — single source shared with the
  // in-page chain picker on /admin/options. Without RELIANCE et al. in
  // this list, typing "rel" matched nothing because `suggestUnderlyings`
  // fallback only returns the first 1000 alphabetical names (RELIANCE
  // lands past index 1000 in the Kite instruments dump).
  const _COMMON_INDICES_AND_COMMODITIES = POPULAR_UNDERLYINGS;

  // _KITE_IDX_TO_ROOT imported from $lib/data/resolveUnderlying.js
  // (KITE_INDEX_QUOTE_KEY_TO_ROOT) — single source of truth.

  // Derive the seed underlying from the symbol prop. Handles:
  //   - Kite index quote-key forms (e.g. "NIFTY 50" → "NIFTY")
  //   - Full contract tradingsymbols (e.g. NIFTY25APR22000CE → NIFTY)
  //   - Plain roots (e.g. NIFTY → NIFTY)
  const seedUnderlying = $derived.by(() => {
    if (!symbol) return '';
    const upper = String(symbol).toUpperCase().trim();
    // Normalise index quote-key forms first, then strip digit-suffix.
    const mapped = _KITE_IDX_TO_ROOT[upper] || upper;
    return mapped.replace(/\d.*$/, '') || mapped;
  });

  // Derive the seed expiry from the symbol prop when it's a specific
  // contract (CE/PE/FUT). The instruments cache row carries the
  // authoritative ISO expiry as `.x` — way more reliable than parsing
  // 25APR out of the tradingsymbol. Returns null for bare underlyings
  // ('NIFTY') or when the cache is still loading. When set, the
  // default-pick effect below prefers this expiry over chainExpiries[0]
  // (the near-month default) — operator clicking "close this position"
  // lands on the position's own contract month, not nearest-future.
  let _instrumentsTimedOut = $state(false);
  // Set to true when loadInstruments() throws (IDB error / IndexedDB unavailable).
  // Triggers an error banner + Retry button in the template so the operator
  // can recover without a full page reload.
  let _instrumentsError = $state(false);
  const instrumentsReady = $derived($instrumentsCacheVersion > 0 || _instrumentsTimedOut);

  const seedExpiry = $derived.by(() => {
    if (!instrumentsReady || !symbol) return null;
    const inst = getInstrument(String(symbol).toUpperCase());
    return inst?.x || null;
  });

  // ── Chain picker state ────────────────────────────────────────────
  let chainUnderlying  = $state('');
  let chainExpiry      = $state('');
  /** @type {Array<'opt'|'fut'>} */
  let chainKinds       = $state(/** @type {Array<'opt'|'fut'>} */ (['opt']));

  // ── sessionStorage keys for root + per-root expiry memory ─────────
  const _SS_ROOT    = 'chain.lastRoot';
  const _ssExpKey   = (/** @type {string} */ root) => `chain.lastExpiry.${root}`;

  // Persist root whenever it changes (operator pick or cascade result).
  // Guard: don't write empty string — that would clobber a valid previous pick.
  $effect(() => {
    const r = chainUnderlying;
    if (r) {
      try { sessionStorage.setItem(_SS_ROOT, r); } catch { /* SSR / private mode */ }
    }
  });

  // Persist per-root expiry whenever it changes.
  $effect(() => {
    const root = chainUnderlying;
    const exp  = chainExpiry;
    if (root && exp) {
      try { sessionStorage.setItem(_ssExpKey(root), exp); } catch { /* SSR / private mode */ }
    }
  });

  // Account for order routing — required for basket submit.
  // Prefer the `account` prop; fall back to the first real account in `accounts`.
  function _isRealAcct(/** @type {string|null|undefined} */ a) {
    return !!(a && !String(a).includes('#'));
  }
  /** @type {string[]} */
  let _selfAccounts = $state([]);
  const _allAccounts = $derived.by(() => {
    const fromProp = (accounts || []).filter(_isRealAcct);
    if (fromProp.length) return fromProp;
    return _selfAccounts.filter(_isRealAcct);
  });
  // intentional: seeds from account prop once; $effects below re-sync on prop changes
  // svelte-ignore state_referenced_locally
  let _account = $state(_isRealAcct(account) ? $state.snapshot(account) : '');
  $effect(() => {
    if (untrack(() => _account)) return;
    if (_isRealAcct(account)) { _account = account; return; }
    if (_allAccounts.length === 1) _account = _allAccounts[0];
  });
  // Sync from a shell-pushed prop change (operator switched account
  // in another tab; shell re-renders us with the new value).
  $effect(() => {
    if (account && _isRealAcct(account) && account !== untrack(() => _account)) {
      _account = account;
    }
  });
  // Template state — SELECTION lives entirely in the shell now.
  // `templateId` is always bound by SymbolPanel (`bind:templateId`,
  // the tab's sole call site), so the picker default and the
  // side-aware auto-pick both live there; this tab only threads
  // `templateId` through to `placeBasket()` per leg.

  // Push picker changes back to the shell so the other tabs sync.
  // Guard against the echo of the inbound prop sync above.
  let _lastNotifiedAcct = '';
  $effect(() => {
    if (_account && _account !== _lastNotifiedAcct && _account !== account) {
      _lastNotifiedAcct = _account;
      onAccountChange?.(_account);
    } else if (_account === account) {
      _lastNotifiedAcct = _account;
    }
  });

  // Underlying choices — common indices first, then everything else.
  const underlyingChoices = $derived.by(() => {
    const seen = new Set();
    /** @type {string[]} */ const out = [];
    const push = (/** @type {string|null|undefined} */ u) => {
      if (!u) return;
      const k = String(u).toUpperCase();
      if (seen.has(k)) return;
      seen.add(k); out.push(k);
    };
    push(seedUnderlying);
    for (const u of _COMMON_INDICES_AND_COMMODITIES) push(u);
    // Pull the entire underlying universe (Kite dump produces ~5k
    // unique underlyings — well under any sane bound). The Select
    // component's own filter handles the long tail; we just need
    // every name to be in `options` so a typed substring can match.
    for (const u of suggestUnderlyings('', 100000)) push(u);
    return out;
  });

  const chainExpiries = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
    return listExpiries(chainUnderlying.toUpperCase(), 'CE');
  });
  // Human-readable expiry label for the picker. Input is YYYY-MM-DD;
  // output is e.g. "26 Jun 2026" / "26 Jun 2026 (Thu)" so the
  // operator can scan the date at a glance instead of parsing ISO.
  function _humanExpiry(/** @type {string} */ iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso + 'T00:00:00Z');
      if (Number.isNaN(d.getTime())) return iso;
      const day = d.getUTCDate();
      const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getUTCMonth()];
      const yr  = d.getUTCFullYear();
      const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getUTCDay()];
      return `${day} ${mon} ${yr} (${dow})`;
    } catch { return iso; }
  }
  // Days-to-expiry, rounded down. Drives the amber "rolls in N days"
  // chip in the toolbar so the operator sees the imminent roll.
  function _daysToExpiry(/** @type {string} */ iso) {
    if (!iso) return null;
    try {
      const d = new Date(iso + 'T15:30:00+05:30');
      const diffMs = d.getTime() - Date.now();
      return Math.max(0, Math.floor(diffMs / 86_400_000));
    } catch { return null; }
  }
  const _chainExpiryOptions = $derived(
    chainExpiries.map(e => ({ value: e, label: _humanExpiry(e) }))
  );
  // ── Chain quotes (bid/ask per strike) — declared here so chainStrikes
  //    can reference it before the polling $effect below.
  /** @type {Record<string,{ce:{bid:number|null,ask:number|null,depthAvail:boolean},pe:{bid:number|null,ask:number|null,depthAvail:boolean}}>|null} */
  let chainQuotesMap = $state(null);
  let _pricesFetching = $state(false);
  let _pricesAbort = /** @type {AbortController|null} */ (null);
  const chainStrikes = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying || !chainExpiry) return [];
    return listStrikes(chainUnderlying.toUpperCase(), 'CE', chainExpiry);
  });
  const chainFutures = $derived.by(() => {
    if (!chainUnderlying) return [];
    const all = listFutures(chainUnderlying.toUpperCase()) || [];
    if (!chainExpiry) return all.slice(0, 3);
    const exact = all.filter(f => f.x === chainExpiry);
    if (exact.length) return exact;
    const ym = String(chainExpiry).slice(0, 7);
    return all.filter(f => String(f.x || '').slice(0, 7) === ym);
  });

  // Sync chainUnderlying from the symbol prop. When the operator
  // picks a new symbol via the modal's picker row, seedUnderlying
  // updates — without this effect, chainUnderlying stays on whatever
  // it last defaulted to and the chain shows the wrong instrument.
  // untrack on chainUnderlying so this isn't a self-firing loop.
  $effect(() => {
    const seed = seedUnderlying;
    if (!seed) return;
    if (seed !== untrack(() => chainUnderlying)) chainUnderlying = seed;
  });

  // Auto-default underlying once instruments are ready (when the
  // operator hasn't supplied one via the symbol prop).
  // Root cascade (first hit wins):
  //   1. seedUnderlying (from symbol prop) — explicit host context takes
  //      priority so clicking BHEL always opens BHEL, not whatever was
  //      last visited (fixes BHEL→CRUDEOIL stale-sessionStorage bug).
  //   2. sessionStorage 'chain.lastRoot'  — operator's last visit, only
  //      used when no specific symbol context was passed by the host.
  //   3. underlyingChoices[0]             — first available (popular list)
  //   4. 'NIFTY'                          — hard fallback
  //
  // Note: when instruments aren't ready yet (list is empty) we do NOT
  // clear chainUnderlying — Effect A (sync-from-seedUnderlying above)
  // may have already set it from the prop, and clearing it here would
  // force a sessionStorage read on the next list-ready tick even when
  // a fresh seed was supplied.
  $effect(() => {
    const list = underlyingChoices;
    untrack(() => {
      if (!list.length) return; // instruments not ready yet — preserve whatever Effect A set
      if (chainUnderlying && list.includes(chainUnderlying)) return; // already valid
      // seedUnderlying takes priority when the host passed an explicit symbol.
      if (seedUnderlying && list.includes(seedUnderlying)) { chainUnderlying = seedUnderlying; return; }
      // sessionStorage fallback — only used when opening the chain without
      // a specific symbol context (e.g. bare "Chain" button in navbar).
      let ssRoot = '';
      try { ssRoot = sessionStorage.getItem(_SS_ROOT) || ''; } catch { /* SSR */ }
      if (ssRoot && list.includes(ssRoot)) { chainUnderlying = ssRoot; return; }
      // First popular underlying.
      chainUnderlying = list[0] || 'NIFTY';
    });
  });

  $effect(() => {
    void chainUnderlying;
    if (chainExpiries.length && !chainExpiries.includes(untrack(() => chainExpiry))) {
      // Prefer the seed contract's own expiry if it's in the list —
      // operator clicked a specific position (e.g. NIFTY26MAY22000CE)
      // and the chain should open on THAT month, not nearest-future.
      if (seedExpiry && chainExpiries.includes(seedExpiry)) {
        chainExpiry = seedExpiry;
        return;
      }
      // sessionStorage per-root expiry memory — restore last-used expiry
      // for this root when the operator reopens the same root.
      let ssExp = '';
      try { ssExp = sessionStorage.getItem(_ssExpKey(chainUnderlying)) || ''; } catch { /* SSR */ }
      if (ssExp && chainExpiries.includes(ssExp)) {
        chainExpiry = ssExp;
        return;
      }
      // Default: nearest available expiry.
      chainExpiry = chainExpiries[0];
    }
  });

  // ── Spot fetch ────────────────────────────────────────────────────
  /** @type {{spot:number, source:string}|null} */
  let chainSpotFetched = $state(null);
  let chainSpotKey = '';
  $effect(() => {
    void chainUnderlying; void chainExpiry;
    untrack(() => {
      if (!chainUnderlying) { chainSpotFetched = null; chainSpotKey = ''; return; }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry || ''}`;
      if (key === chainSpotKey) return;
      chainSpotKey = key;
      const u = chainUnderlying; const e = chainExpiry || null;
      fetchOptionsSpot(u, e).then((r) => {
        if (chainSpotKey !== key) return;
        chainSpotFetched = r ? { spot: Number(r.spot) || 0, source: String(r.spot_source || '') } : null;
      }).catch(() => { if (chainSpotKey !== key) return; chainSpotFetched = null; });
    });
  });
  const chainSpot = $derived(chainSpotFetched?.spot ?? null);
  const chainAtmStrike = $derived.by(() => {
    if (chainSpot == null || !chainStrikes.length) return null;
    let best = chainStrikes[0]; let bestDiff = Math.abs(best - chainSpot);
    for (const k of chainStrikes) { const d = Math.abs(k - chainSpot); if (d < bestDiff) { best = k; bestDiff = d; } }
    return best;
  });

  // ATM row scroll
  /** @type {HTMLTableRowElement | null} */
  let chainAtmRowEl = $state(null);
  /** @type {(node: HTMLTableRowElement) => { destroy(): void }} */
  const chainAtmRow = (node) => {
    chainAtmRowEl = node;
    return { destroy() { if (chainAtmRowEl === node) chainAtmRowEl = null; } };
  };
  $effect(() => {
    void chainAtmRowEl; void chainAtmStrike;
    if (chainAtmRowEl) {
      queueMicrotask(() => {
        const row = chainAtmRowEl; if (!row) return;
        const wrap = row.closest('.chain-grid-wrap');
        if (wrap) { const target = row.offsetTop - (wrap.clientHeight - row.offsetHeight) / 2; wrap.scrollTop = Math.max(0, target); }
        else row.scrollIntoView({ block: 'nearest', behavior: 'auto' });
      });
    }
  });

  // ── Chain quotes polling — bid/ask overlay ────────────────────────
  // Strike list comes from the local instruments cache (instant via
  // listStrikes). This poll fetches bid/ask from the backend every 30 s
  // and overlays them onto the rendered grid. Grid shows immediately;
  // bid/ask cells show '—' until the first poll resolves.

  let chainQuotesKey = '';
  let chainQuotesPoll = /** @type {any} */ (null);
  function _refreshChainQuotes() {
    if (!chainUnderlying || !chainExpiry) return;
    if (_pricesFetching) return;
    const u = chainUnderlying.toUpperCase(); const e = chainExpiry;
    const key = `${u}|${e}`;
    _pricesAbort?.abort();
    const ac = new AbortController();
    _pricesAbort = ac;
    _pricesFetching = true;
    const _tout = setTimeout(() => ac.abort(), 10_000);
    fetchChainQuotesPrices(u, e, { signal: ac.signal }).then((r) => {
      if (chainQuotesKey !== key) return;
      /** @type {Record<string, object>} */
      const map = {};
      for (const row of (r?.rows || [])) {
        const [k, q] = parseChainQuoteRow(row, r?.exchange);
        map[k] = q;
      }
      chainQuotesMap = map;
    }).catch(() => {}).finally(() => { clearTimeout(_tout); _pricesFetching = false; });
  }
  $effect(() => {
    void chainUnderlying; void chainExpiry;
    untrack(() => {
      if (chainQuotesPoll) { chainQuotesPoll(); chainQuotesPoll = null; }
      _pricesAbort?.abort(); _pricesAbort = null; _pricesFetching = false;
      if (!chainUnderlying || !chainExpiry) { chainQuotesMap = null; chainQuotesKey = ''; return; }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry}`;
      if (key !== chainQuotesKey) { chainQuotesMap = null; chainQuotesKey = key; }
      _refreshChainQuotes();
      chainQuotesPoll = visibleInterval(_refreshChainQuotes, 30000);
    });
  });
  onDestroy(() => { if (chainQuotesPoll) { chainQuotesPoll(); chainQuotesPoll = null; } _pricesAbort?.abort(); _pricesAbort = null; });

  // Periodic ATM spot refresh — re-fetch spot every 30s during market
  // hours so the ATM row marker tracks NIFTY/CRUDEOIL moves intraday.
  // Without this, spot is fetched once when underlying/expiry changes
  // and the ATM row stays pinned even if the underlying moves 200pts.
  // Race-condition guard (chainSpotKey !== key) still works: we
  // capture the current key before the async fetch and discard stale
  // results if underlying/expiry changed mid-flight.
  async function _refreshChainSpot() {
    if (!isMarketOpen() || !chainUnderlying || !chainExpiry) return;
    const key = `${chainUnderlying.toUpperCase()}|${chainExpiry || ''}`;
    const u = chainUnderlying; const e = chainExpiry || null;
    try {
      const r = await fetchOptionsSpot(u, e);
      if (chainSpotKey !== key) return; // underlying/expiry changed mid-flight
      chainSpotFetched = r ? { spot: Number(r.spot) || 0, source: String(r.spot_source || '') } : null;
    } catch { /* silent — ATM stays at last-good value */ }
  }

  let _chainSpotRefreshPoll = /** @type {any} */ (null);
  $effect(() => {
    void chainUnderlying; void chainExpiry;
    untrack(() => {
      if (_chainSpotRefreshPoll) { _chainSpotRefreshPoll(); _chainSpotRefreshPoll = null; }
      if (!chainUnderlying || !chainExpiry) return;
      _chainSpotRefreshPoll = visibleInterval(_refreshChainSpot, 30_000);
    });
  });
  onDestroy(() => { if (_chainSpotRefreshPoll) { _chainSpotRefreshPoll(); _chainSpotRefreshPoll = null; } });

  // Host-triggered refresh — invalidate the spot + quotes cache keys
  // and re-fire the fetchers so a tab activation always lands on fresh
  // chain data (operator request: "when chain tab is pressed, the
  // chain details need to be refreshed").
  // Race-condition guard: capture refreshKey in the reactive scope BEFORE
  // entering untrack(), then check it in the .then() handler so a double-tap
  // (two parallel spot fetches) only writes state for the last-issued request.
  $effect(() => {
    if (refreshKey <= 0) return;
    const capturedKey = refreshKey; // reactive read — establishes the guard value
    untrack(() => {
      // Clear keys so the next spot effect treats the underlying as
      // newly-set and re-fetches.
      chainSpotKey = '';
      // Do NOT reset chainQuotesKey here — that would cause every subsequent
      // price response to be discarded (the .then() guard checks key equality).
      if (chainUnderlying) {
        // Spot re-fetch — bypass the key-equality short-circuit.
        const u = chainUnderlying; const e = chainExpiry || null;
        fetchOptionsSpot(u, e).then((r) => {
          if (refreshKey !== capturedKey) return; // stale — a newer refresh fired
          chainSpotFetched = r ? { spot: Number(r.spot) || 0, source: String(r.spot_source || '') } : null;
          chainSpotKey = `${u.toUpperCase()}|${e || ''}`;
        }).catch(() => {
          if (refreshKey !== capturedKey) return; // stale — discard error too
          chainSpotFetched = null;
        });
      }
      _refreshChainQuotes();
    });
  });

  const _fmtLtp = priceFmt;

  // ── Basket ────────────────────────────────────────────────────────
  // When _externalBasket, reads/writes go via shell callbacks.
  // Local state is the fallback for the standalone chain-only case.
  /** @type {Array<{key:string,side:'BUY'|'SELL',sym:string,exchange:string,lots:number,lotSize:number,product:string,limit:number,chaseAgg:'low'|'med'|'high'}>} */
  let _localBasket   = $state([]);
  // Effective basket — shell's if lifted, otherwise local.
  const chainBasket  = $derived(_externalBasket ? (basketLegs ?? []) : _localBasket);
  // (strike, optType) -> staged leg, for the per-row lot-detail badge.
  // Keyed off the fields addOptionToBasket already stamps onto each new
  // leg (strike/optType) — avoids re-resolving/parsing the tradingsymbol
  // per row just to find out "is this strike's CE/PE already staged".
  const _basketLegByKey = $derived.by(() => {
    /** @type {Map<string, any>} */
    const map = new Map();
    for (const b of chainBasket) {
      if (b.strike != null && b.optType) map.set(`${b.strike}:${b.optType}`, b);
    }
    return map;
  });

  let basketPlacing  = $state(false);
  let basketError    = $state('');
  let basketProgress = $state(0);
  let basketJustDone = $state(false);
  /** @type {{key:string, msg:string}|null} */
  let quickToast = $state(null);
  // Sticky "active row" marker. Last strike + opt_type the operator
  // poked. Survives the 900 ms quickToast so the row the operator
  // just hit stays visually pinned — useful when they're going to
  // place several orders in a row off the same strike. Stays until
  // they click a different row OR explicitly clear (e.g. close the
  // OrderTicket modal). Coexists with chain-row-atm coloring; the
  // active class adds an outline + background tint without
  // overriding the ATM cyan/orange direction stripe.
  /** @type {{strike:number, optType:'CE'|'PE'}|null} */
  let activeOptionRow = $state(null);

  function _quickKeyOpt(/** @type {number} */ strike, /** @type {string} */ optType) { return `o:${strike}:${optType}`; }
  function _quickKeyFut(/** @type {string} */ sym) { return `f:${sym}`; }

  function _flashToast(/** @type {string} */ key, /** @type {string} */ msg) {
    quickToast = { key, msg };
    setTimeout(() => { if (quickToast?.key === key) quickToast = null; }, 900);
  }

  function _markActive(/** @type {number} */ strike, /** @type {'CE'|'PE'} */ optType) {
    activeOptionRow = { strike, optType };
  }

  function _mergeIntoBasket(/** @type {{sym:string,side:'BUY'|'SELL',lots:number}} */ incoming) {
    const idx = chainBasket.findIndex(b => b.sym === incoming.sym && b.side === incoming.side);
    if (idx < 0) return false;
    if (_externalBasket && onUpdateLeg) {
      // Single-pass map update on the shell's $state. Avoids the
      // remove+re-add round-trip which could drop rapid clicks when
      // the basketLegs prop hadn't propagated back to the child.
      const existing = chainBasket[idx];
      onUpdateLeg(existing.key, (leg) => ({
        ...leg,
        lots: (leg.lots || 0) + (incoming.lots || 1),
      }));
    } else if (_externalBasket && onRemoveLeg && onAddLeg) {
      // Legacy path for callers that haven't wired onUpdateLeg yet.
      const existing = chainBasket[idx];
      onRemoveLeg(existing);
      onAddLeg({ ...existing, lots: (existing.lots || 0) + (incoming.lots || 1) });
    } else {
      _localBasket = _localBasket.map((b, i) => i === idx ? { ...b, lots: (b.lots || 0) + (incoming.lots || 1) } : b);
    }
    return true;
  }

  function _netAgainstBasket(/** @type {string} */ sym, /** @type {'BUY'|'SELL'} */ sideTag) {
    const oppSide = sideTag === 'BUY' ? 'SELL' : 'BUY';
    const idx = chainBasket.findIndex(b => b.sym === sym && b.side === oppSide);
    if (idx < 0) return false;
    const leg = chainBasket[idx];
    const newLots = (leg.lots || 1) - 1;
    if (newLots <= 0) {
      if (_externalBasket && onRemoveLeg) { onRemoveLeg(leg); }
      else if (_externalBasket && !onRemoveLeg) {
        // External basket mode without a remove handler — cannot safely
        // mutate _localBasket (wrong array). Caller must provide onRemoveLeg.
        console.warn('OptionChainTab: _externalBasket requires onRemoveLeg — leg not removed');
        return false;
      } else { _localBasket = _localBasket.filter((_, i) => i !== idx); }
    } else {
      if (_externalBasket && onUpdateLeg) {
        onUpdateLeg(leg.key, (l) => ({ ...l, lots: newLots }));
      } else if (_externalBasket && onRemoveLeg && onAddLeg) {
        onRemoveLeg(leg); onAddLeg({ ...leg, lots: newLots });
      } else {
        _localBasket = _localBasket.map((b, i) => i === idx ? { ...b, lots: newLots } : b);
      }
    }
    return true;
  }

  function _pushToBasket(/** @type {any} */ newLeg) {
    if (_externalBasket && onAddLeg) {
      onAddLeg(newLeg);
    } else {
      _localBasket = [..._localBasket, newLeg];
    }
  }

  function addOptionToBasket(/** @type {number} */ strike, /** @type {'CE'|'PE'} */ optType, /** @type {'long'|'short'} */ side) {
    if (!chainUnderlying || !chainExpiry) return;
    const inst = findOption(chainUnderlying.toUpperCase(), optType, strike, chainExpiry);
    if (!inst) { basketError = 'Symbol not in instruments cache.'; return; }
    const sideTag = /** @type {'BUY'|'SELL'} */ (side === 'long' ? 'BUY' : 'SELL');
    if (!_account) {
      if (_allAccounts.length === 1) { _account = _allAccounts[0]; }
      else if (_allAccounts.length === 0) { basketError = 'No broker accounts loaded — wait or sign in.'; return; }
      else { basketError = 'Pick a routable account before adding legs.'; return; }
    }
    _markActive(strike, optType);
    if (_placeMode && onPlaceLeg) {
      const q = chainQuotesMap?.[String(strike)]?.[optType.toLowerCase()];
      const limit = sideTag === 'BUY' ? (q?.ask ?? q?.bid ?? 0) : (q?.bid ?? q?.ask ?? 0);
      onPlaceLeg({
        symbol: String(inst.s), exchange: inst.e || 'NFO', side: sideTag,
        qty: Number(inst.ls || 1), lotSize: Number(inst.ls || 1),
        price: Number(limit) || 0,
        orderType: limit > 0 ? 'LIMIT' : 'MARKET',
        product: 'NRML', variety: 'regular', account: _account,
      });
      _flashToast(_quickKeyOpt(strike, optType), '→ ticket');
      return;
    }
    if (_netAgainstBasket(String(inst.s), sideTag)) {
      basketError = ''; _flashToast(_quickKeyOpt(strike, optType), 'netted'); return;
    }
    if (_mergeIntoBasket({ sym: String(inst.s), side: sideTag, lots: 1 })) {
      basketError = ''; _flashToast(_quickKeyOpt(strike, optType), '+1 lot'); return;
    }
    const q = chainQuotesMap?.[String(strike)]?.[optType.toLowerCase()];
    const limit = sideTag === 'BUY' ? (q?.ask ?? q?.bid ?? 0) : (q?.bid ?? q?.ask ?? 0);
    _pushToBasket({
      key:      `${sideTag}|${_quickKeyOpt(strike, optType)}|${Date.now()}`,
      side:     sideTag, sym: String(inst.s), exchange: inst.e || 'NFO',
      account:  _account,
      lots: 1, lotSize: Number(inst.ls || 1), product: 'NRML',
      limit: Number(limit) || 0, chaseAgg: 'low',
      // Row-lookup fields only (not read by placeTicketOrder, which
      // builds its own explicit payload) — let the strike grid show a
      // per-leg lot badge without re-resolving/parsing the tradingsymbol.
      strike, optType,
      // Audit fix — `limit > 0` alone is not proof a live chain quote has
      // ever actually arrived for THIS leg's strike (a stale `0` fallback
      // reads the same as a real price once coerced). `quoteArrived`
      // latches true the moment `chainQuotesMap` resolves ANY bid/ask for
      // this strike+side (here, or via the sync effect below for a leg
      // added before the first poll lands) and never resets — same "has
      // a quote arrived at least once" invariant as OrderTicket's
      // `_lastQuote` truthy check, applied per-leg. Read by SymbolPanel's
      // `_legNeedsDepth()` basket-submit gate.
      quoteArrived: !!q,
    });
    basketError = ''; _flashToast(_quickKeyOpt(strike, optType), '✓ added');
  }

  // ── Keep `quoteArrived` in sync with the live chain-quotes poll ────
  // `addOptionToBasket` stamps `quoteArrived` from whatever
  // `chainQuotesMap` already holds at push time. If a leg is pushed in
  // the brief window before the FIRST chain-quotes poll resolves (map
  // still null right after the Chain tab mounts), `quoteArrived` starts
  // false and would otherwise never update without a remove+re-add.
  // This re-checks every option leg currently in the basket whenever
  // the quotes map refreshes (every 30s, or the host-triggered
  // refresh) and latches `quoteArrived` true the first time a quote
  // resolves for that leg's own strike+side. One-way latch only — this
  // is "has a quote arrived at least once", not a staleness/
  // re-validation mechanism, mirroring OrderTicket's `_lastQuote`
  // invariant (which also never re-checks once set).
  $effect(() => {
    const map = chainQuotesMap;
    const legs = chainBasket;
    if (!map) return;
    const pending = legs.filter(l => !l.quoteArrived && l.strike != null && l.optType
      && map[String(l.strike)]?.[String(l.optType).toLowerCase()]);
    if (!pending.length) return;
    untrack(() => {
      for (const leg of pending) {
        if (_externalBasket && onUpdateLeg) {
          onUpdateLeg(leg.key, (l) => (l.quoteArrived ? l : { ...l, quoteArrived: true }));
        } else {
          _localBasket = _localBasket.map(b => b.key === leg.key ? { ...b, quoteArrived: true } : b);
        }
      }
    });
  });

  function addFuturesToBasket(/** @type {string} */ sym, /** @type {number} */ lotSize, /** @type {'long'|'short'} */ side) {
    const inst = getInstrument(String(sym || '').toUpperCase());
    const sideTag = /** @type {'BUY'|'SELL'} */ (side === 'long' ? 'BUY' : 'SELL');
    // Same _account race fallback as addOptionToBasket above.
    if (!_account) {
      if (_allAccounts.length === 1) { _account = _allAccounts[0]; }
      else if (_allAccounts.length === 0) { basketError = 'No broker accounts loaded — wait or sign in.'; return; }
      else { basketError = 'Pick a routable account before adding legs.'; return; }
    }
    if (_placeMode && onPlaceLeg) {
      // Hand-off to Ticket tab as LIMIT (default for the platform) —
      // operator enters the limit price in Ticket and submits. The
      // Ticket's _chase + _chaseAgg defaults are already on/low.
      onPlaceLeg({
        symbol: String(sym), exchange: inst?.e || 'NFO', side: sideTag,
        qty: Number(lotSize || inst?.ls || 1),
        lotSize: Number(lotSize || inst?.ls || 1),
        price: 0, orderType: 'LIMIT',
        product: 'NRML', variety: 'regular', account: _account,
      });
      _flashToast(_quickKeyFut(sym), '→ ticket');
      return;
    }
    if (_netAgainstBasket(String(sym), sideTag)) {
      basketError = ''; _flashToast(_quickKeyFut(sym), 'netted'); return;
    }
    if (_mergeIntoBasket({ sym: String(sym), side: sideTag, lots: 1 })) {
      basketError = ''; _flashToast(_quickKeyFut(sym), '+1 lot'); return;
    }
    _pushToBasket({
      key:      `${sideTag}|${_quickKeyFut(sym)}|${Date.now()}`,
      side:     sideTag, sym: String(sym), exchange: inst?.e || 'NFO',
      account:  _account,
      lots: 1, lotSize: Number(lotSize || inst?.ls || 1), product: 'NRML',
      limit: 0, chaseAgg: 'low',
    });
    basketError = ''; _flashToast(_quickKeyFut(sym), '✓ added');
  }

  /** @returns {'live'|'paper'} */
  function _resolveBasketMode() {
    const m = String($executionMode || 'paper').toLowerCase();
    return m === 'live' ? 'live' : 'paper';
  }

  /**
   * Place a single basket leg. Returns null on success, an error string on failure.
   * Does NOT mutate basketProgress — the caller increments after every leg.
   * @param {any} leg @param {'live'|'paper'} mode @param {string} acct
   * @returns {Promise<string|null>}
   */
  async function _placeOneLeg(leg, mode, acct) {
    if (!(Number(leg.limit) > 0)) {
      // Default to LIMIT + chase[low]; refuse a placement that
      // would silently downgrade to MARKET because the quote
      // hadn't arrived yet. Operator can wait for chain quotes
      // to load (auto-poll every 5 s) and resubmit.
      return `${leg.side} ${leg.sym}: no quote yet — re-open the chain so the bid/ask price loads, then submit again.`;
    }
    // v2 API (2026-07-08): send LOTS for F&O (lotSize > 1),
    // raw shares for equity. Backend multiplies lots × lot_size
    // to get contracts internally.
    const _isFO = Number(leg.lotSize) > 1;
    const _requestQty = _isFO
      ? Math.max(1, Number(leg.lots) || 1)
      : Math.max(1, (Number(leg.lots) || 1) * (Number(leg.lotSize) || 1));
    try {
      await placeTicketOrder({
        mode, side: leg.side, tradingsymbol: leg.sym,
        quantity: _requestQty, exchange: leg.exchange,
        lot_size_hint: Number(leg.lotSize) > 0 ? Number(leg.lotSize) : null,
        product: leg.product || 'NRML',
        order_type: 'LIMIT',
        price: Number(leg.limit),
        variety: 'regular', account: leg.account || acct,
        chase: true, chase_aggressiveness: leg.chaseAgg || 'low',
        // Same template attaches to every leg in the basket. The
        // backend ticket route reads `template_id` and runs
        // apply_template_to_order on fill — TP / SL / Wing GTTs
        // for each leg get queued individually.
        template_id: templateId,
        // Sprint 2b (SPRINT2_LAYER_INTEGRATION.md §3/§4.4) — Chain tab's
        // own per-leg ticket submit, distinct from SymbolPanel's
        // /orders/basket path (tagged 'basket').
        source: 'chain',
      });
      return null;
    } catch (e) {
      return `${leg.side} ${leg.sym}: ${String(/** @type {any} */ (e)?.message || e || 'failed')}`;
    }
  }

  /**
   * Resolve basket outcome: set basketError, clear/reset _localBasket,
   * fire basketJustDone flash, and call onBasketPlace callback.
   * @param {string[]} failures @param {number} total
   */
  function _finalizeBasket(failures, total) {
    if (failures.length === total) {
      basketError = failures[0] || 'All legs failed';
      // Trading-critical toast (2026-09-30) — chain-tab basket has its
      // own inline `basketError` banner but no shared toast; add one so
      // the operator notices even if they've scrolled away from it.
      toast.error(`Chain basket failed — ${basketError}`.slice(0, 80), { timeoutMs: 5000 });
    } else if (failures.length) {
      basketError = `${failures.length}/${total} failed: ${failures[0]}`;
      _localBasket = [];
      toast.warning(`Chain basket: ${total - failures.length}/${total} placed, ${failures.length} failed`, { timeoutMs: 5000 });
    } else {
      _localBasket = []; basketJustDone = true;
      setTimeout(() => { basketJustDone = false; }, 2200);
    }
    onBasketPlace?.({ ok: total - failures.length, fail: failures.length });
  }

  async function placeBasket() {
    // When the basket is lifted to the shell, delegate entirely.
    if (_externalBasket && onSubmitBasket) { onSubmitBasket(); return; }
    if (basketPlacing || !chainBasket.length) return;
    const acct = _account;
    if (!acct) { basketError = 'No routable account. Pick an account above.'; return; }

    // Audit fix — read mode from the $executionMode store instead of
    // an async fetchLiveStatus() round-trip. Same source the navbar
    // dropdown writes to + SymbolPanel and OrderTicket read from, so
    // a navbar mode change between the operator clicking Place and
    // the response arriving can't desync the basket.
    const basketMode = _resolveBasketMode();

    basketPlacing = true; basketError = ''; basketProgress = 0;
    /** @type {string[]} */ const failures = [];
    for (const leg of chainBasket) {
      const err = await _placeOneLeg(leg, basketMode, acct);
      if (err) failures.push(err);
      basketProgress += 1;
    }
    const total = chainBasket.length;
    basketPlacing = false;
    _finalizeBasket(failures, total);
  }

  // ── Pre-submit spread gate (Chain-only) ───────────────────────────
  // Operator: "if there is too much spread on the offset limit side,
  // it should warn... it should be in a loop until the conditions are
  // satisfied before placing the order." Runs ONLY when the submission
  // would attach a wing/offset leg (either direction — see
  // `_wing_direction` in backend/api/algo/template_attach.py, not the
  // SELL-only `showsWing` prop, which predates that BUY-parent offset
  // leg and must not be reused as this gate's predicate). Exposed as
  // `runPreSubmitGate()` (component export, called via bind:this) so
  // the shell's shared Submit button can await it before calling
  // submitBasket() — see SymbolPanel.svelte's wiring.
  /** @type {import('$lib/data/spreadGate.js').SpreadGateState} */
  let _gateState = $state({ phase: 'idle', legs: [], attempts: 0, lastError: '' });
  /** @type {ReturnType<typeof createSpreadGate> | null} */
  let _gate = null;
  /** Pending resolvers for `runPreSubmitGate()` — a Set (not a single
   *  ref) so a double-click on Submit while the gate is already open
   *  can't leave an earlier caller's await dangling forever. */
  const _gateResolvers = /** @type {Set<(ok: boolean) => void>} */ (new Set());
  /** Premium%-mode wing tradingsymbol cache, keyed by basket leg `key`.
   *  Populated ONCE per gate session (in `runPreSubmitGate()`, before
   *  `_gate.start()`) via `POST /api/orders/ticket/preview` — NEVER
   *  re-queried on every poll tick. That endpoint runs a template load
   *  + the wing chain-scan (with its own ntfy-alert-on-failure side
   *  effect on scan failure — see `_pick_wing_by_premium` in
   *  template_attach.py), so polling it every 4s would spam alerts;
   *  `GET /api/orders/spread-check` (the actual per-tick poll target)
   *  is deliberately template-agnostic and has no such side effect. */
  let _premiumWingSymCache = /** @type {Map<string, string>} */ (new Map());

  function _isGateTerminal(/** @type {string} */ phase) {
    return phase === 'idle' || phase === 'passed' || phase === 'overridden' || phase === 'cancelled';
  }

  /** Effective template row for a basket leg — per-leg `template_id`
   *  wins, else the shell's shared pick. Mirrors SymbolPanel's
   *  `_legEffectiveTpl` (that component owns the catalog lookup for
   *  its own per-leg editor; this is the Chain tab's own copy over the
   *  `nonNoneTemplates`/`selectedTemplate` props already passed down). */
  function _effTemplateForLeg(/** @type {any} */ leg) {
    const tid = leg?.template_id ?? templateId;
    if (tid == null) return null;
    if (selectedTemplate && selectedTemplate.id === tid) return selectedTemplate;
    return nonNoneTemplates.find((t) => t.id === tid) || null;
  }

  /** Effective wing/spread params for a leg — leg override > shell
   *  override (only when the leg has no template override of its own,
   *  same precedence SymbolPanel's `_applySharedOverrides` uses) >
   *  template default. Side-inclusive (CE or PE, BUY or SELL parent) —
   *  deliberately NOT gated on `showsWing`/`applies_to === 'sell_option'`. */
  function _effWingParams(/** @type {any} */ leg) {
    const tpl = _effTemplateForLeg(leg);
    if (!tpl) return null;
    const hasLegTpl = leg?.template_id != null && leg.template_id !== templateId;
    const offset = leg?.wing_strike_offset_override ?? (hasLegTpl ? null
      : (wingStrikeOffsetOverride !== '' && wingStrikeOffsetOverride != null ? Number(wingStrikeOffsetOverride) : null))
      ?? (tpl.wing_strike_offset ?? null);
    const premPct = leg?.wing_premium_pct_override ?? (hasLegTpl ? null
      : (wingPremPctOverride !== '' && wingPremPctOverride != null ? Number(wingPremPctOverride) : null))
      ?? (tpl.wing_premium_pct ?? null);
    const maxSpreadPct = (spreadMaxPctOverride !== '' && spreadMaxPctOverride != null)
      ? Number(spreadMaxPctOverride)
      : (tpl.wing_max_spread_pct ?? 0.5);
    // _template_has_wing parity: offset != null (including 0, a valid
    // ATM wing) OR a truthy premium%. Not a truthiness check on offset.
    const hasWing = offset != null || !!(premPct && Number(premPct) > 0);
    return { offset, premPct, maxSpreadPct, hasWing };
  }

  /**
   * Build the list of {leg, parentSym, parentExch, wingSym, wingUnresolved,
   * maxSpreadPct} entries that need a spread check right now. Empty
   * list = gate doesn't apply (no template, or no leg has a wing
   * configured) — recomputed on every check (including every recheck),
   * so disabling the template or removing the last wing-configured leg
   * mid-wait naturally resolves to "nothing left to check" on the next
   * tick, per the operator's "disabling template skips the check
   * entirely" requirement.
   */
  function _buildGatePlan() {
    /** @type {Array<{leg: any, parentSym: string, parentExch: string, wingSym: string|null, wingUnresolved: boolean, premiumMode: boolean, maxSpreadPct: number}>} */
    const items = [];
    for (const leg of chainBasket) {
      if (leg?.optType !== 'CE' && leg?.optType !== 'PE') continue; // wing only applies to option legs
      const wp = _effWingParams(leg);
      if (!wp || !wp.hasWing) continue;
      let wingSym = /** @type {string|null} */ (null);
      let wingUnresolved = false;
      let premiumMode = false;
      if (wp.offset != null) {
        wingSym = resolveWingTradingsymbol(leg.sym, wp.offset);
        wingUnresolved = !wingSym;
      } else {
        // Premium%-scan mode — the real wing strike depends on a
        // server-side chain scan at fill time (parent fill price not
        // known pre-submit). `runPreSubmitGate()` resolves this ONCE
        // per gate session via /orders/ticket/preview and caches it
        // here by leg key; until resolved, check the parent leg only.
        premiumMode = true;
        const cached = _premiumWingSymCache.get(leg.key);
        if (cached) wingSym = cached; else wingUnresolved = true;
      }
      items.push({ leg, parentSym: leg.sym, parentExch: leg.exchange || 'NFO', wingSym, wingUnresolved, premiumMode, maxSpreadPct: wp.maxSpreadPct });
    }
    return items;
  }

  /** Resolve premium%-mode wing tradingsymbols ONCE per gate session —
   *  see `_premiumWingSymCache`'s own comment for why this must not run
   *  on every poll tick. No-op for items already cached or not in
   *  premium mode. Failures are swallowed — that leg's wing simply
   *  stays unresolved (parent-only check, flagged in the banner) rather
   *  than blocking the whole gate on a preview-call failure. */
  async function _resolvePremiumWingSymbols(/** @type {ReturnType<typeof _buildGatePlan>} */ items) {
    const toResolve = items.filter((it) => it.premiumMode && it.wingUnresolved && !_premiumWingSymCache.has(it.leg.key));
    if (toResolve.length === 0) return;
    const mode = _resolveBasketMode();
    await Promise.all(toResolve.map(async (it) => {
      try {
        const wp = _effWingParams(it.leg);
        const tpl = _effTemplateForLeg(it.leg);
        const resp = await previewTicketTemplate({
          mode,
          side: it.leg.side,
          tradingsymbol: it.leg.sym,
          quantity: Math.max(1, Number(it.leg.lots) || 1),
          exchange: it.parentExch,
          product: it.leg.product || 'NRML',
          account: it.leg.account || _account,
          reference_price: Number(it.leg.limit) || 0,
          template_id: tpl?.id ?? null,
          wing_premium_pct_override: wp?.premPct ?? null,
        });
        const sym = resp?.plan?.wing?.tradingsymbol;
        if (sym) _premiumWingSymCache.set(it.leg.key, String(sym));
      } catch { /* leave unresolved — parent-only check for this leg */ }
    }));
  }

  /** Dedupe parent + wing symbols across every leg into one flat list
   *  of {tradingsymbol, exchange, maxSpreadPct, label} check targets. */
  function _buildCheckTargets(/** @type {ReturnType<typeof _buildGatePlan>} */ items) {
    /** @type {Map<string, {tradingsymbol: string, exchange: string, maxSpreadPct: number, label: string}>} */
    const map = new Map();
    for (const it of items) {
      const pk = `${it.parentExch}:${it.parentSym}`;
      if (!map.has(pk)) map.set(pk, { tradingsymbol: it.parentSym, exchange: it.parentExch, maxSpreadPct: it.maxSpreadPct, label: it.parentSym });
      if (it.wingSym) {
        const wk = `${it.parentExch}:${it.wingSym}`;
        if (!map.has(wk)) map.set(wk, { tradingsymbol: it.wingSym, exchange: it.parentExch, maxSpreadPct: it.maxSpreadPct, label: `${it.wingSym} offset` });
      }
    }
    return [...map.values()];
  }

  /** `checkLegs` callback handed to createSpreadGate — rebuilds the plan
   *  fresh on every call (not a stale snapshot from `start()`), so a
   *  TP%/SL%/Spread%/wing edit — or disabling the template entirely —
   *  is reflected on the very next tick/recheck. */
  async function _checkLegs(/** @type {{signal: AbortSignal}} */ { signal }) {
    const items = _buildGatePlan();
    if (items.length === 0) {
      return { ok: true, legs: [] }; // nothing wing-configured anymore — gate clears itself
    }
    const targets = _buildCheckTargets(items);
    const results = await Promise.all(targets.map(async (t) => {
      const r = await checkOrderSpread({
        tradingsymbol: t.tradingsymbol, exchange: t.exchange,
        maxSpreadPct: t.maxSpreadPct, signal,
      });
      return {
        label: t.label, tradingsymbol: t.tradingsymbol,
        ok: r?.ok === true, spread_pct: r?.spread_pct ?? null,
        bid: r?.bid ?? null, ask: r?.ask ?? null, maxSpreadPct: t.maxSpreadPct,
        reason: r?.reason ?? null,
      };
    }));
    const ok = results.every((r) => r.ok === true);
    return { ok, legs: results };
  }

  function _settleGateResolvers(/** @type {boolean} */ ok) {
    for (const r of _gateResolvers) r(ok);
    _gateResolvers.clear();
  }

  /**
   * Component export — called via `bind:this` from the shell (see
   * SymbolPanel.svelte's shared Submit button). Returns a Promise that
   * resolves `true` once it's safe to call submitBasket() (no wing
   * configured, spread already fine, operator explicitly overrode, or
   * the template got disabled mid-wait) and `false` if the operator
   * cancels. NEVER auto-resolves true past the bounded error/timeout
   * state — those require an explicit Retry / Place anyway / Cancel.
   * @returns {Promise<boolean>}
   */
  export async function runPreSubmitGate() {
    if (_gate && !_isGateTerminal(_gateState.phase)) {
      // Already running (e.g. a double-click on Submit) — attach to
      // the same in-flight loop instead of starting a second one.
      return new Promise((resolve) => { _gateResolvers.add(resolve); });
    }
    let items = _buildGatePlan();
    if (items.length === 0) {
      _gateState = { phase: 'idle', legs: [], attempts: 0, lastError: '' };
      return true;
    }
    // New gate session — fresh premium-wing-symbol cache, resolved
    // once here (not on every poll tick; see the cache's own comment).
    _premiumWingSymCache = new Map();
    const premiumItems = items.filter((it) => it.premiumMode);
    if (premiumItems.length > 0) {
      await _resolvePremiumWingSymbols(premiumItems);
      items = _buildGatePlan(); // re-resolve now that the cache is populated
      if (items.length === 0) {
        _gateState = { phase: 'idle', legs: [], attempts: 0, lastError: '' };
        return true;
      }
    }
    return new Promise((resolve) => {
      _gateResolvers.add(resolve);
      _gate = createSpreadGate({
        checkLegs: _checkLegs,
        onUpdate: (s) => {
          _gateState = s;
          if (s.phase === 'passed' || s.phase === 'overridden') _settleGateResolvers(true);
          else if (s.phase === 'cancelled') _settleGateResolvers(false);
          // 'checking' / 'wide' / 'error' / 'timeout' stay pending —
          // operator (or a recheck) must move the phase along.
        },
      });
      _gate.start();
    });
  }

  function _gatePlaceAnyway() { _gate?.confirmOverride(); }
  function _gateCancel()      { _gate?.cancel(); }
  function _gateRetry()       { _gate?.retry(); }

  // Immediate re-check triggers (operator req't c): TP%/SL%/Spread%/wing
  // param edits, a template switch, or a basket-leg add/remove — only
  // while the gate is actually open (not idle/resolved). Disabling the
  // template (shellUsingNone flips true) also lands here and resolves
  // to "nothing to check" on the very next tick via `_buildGatePlan()`.
  $effect(() => {
    void tpOverride; void slOverride; void spreadMaxPctOverride;
    void wingStrikeOffsetOverride; void wingPremPctOverride;
    void templateId; void shellUsingNone; void chainBasket;
    untrack(() => {
      if (_gate && !_isGateTerminal(_gateState.phase)) _gate.recheck();
    });
  });

  onDestroy(() => {
    // No dangling timers if the operator navigates away / closes the
    // modal mid-loop — resolves any still-pending caller as false
    // (same outcome as an explicit Cancel).
    _gate?.cancel();
  });

  function _loadInstrumentsSafe() {
    _instrumentsError = false;
    loadInstruments().catch(() => { _instrumentsError = true; });
  }

  onMount(() => {
    // Fire-and-forget: IDB may block (other tab holds connection); don't await.
    // instrumentsReady derives from instrumentsCacheVersion (bumped on load)
    // or _instrumentsTimedOut (8s fallback so spinner never freezes).
    // Errors (IDB unavailable, timeout) set _instrumentsError for user recovery.
    _loadInstrumentsSafe();
    const _readyTimer = setTimeout(() => { _instrumentsTimedOut = true; }, 8000);
    // Self-fetch accounts when the prop didn't supply any.
    if (!accounts.length && !_isRealAcct(account)) {
      fetchAccounts()
        .then(/** @param {any} r */ (r) => {
          const list = (r?.accounts || []).map(/** @param {any} a */ (a) => String(a?.account_id || '')).filter(Boolean);
          _selfAccounts = list;
        }).catch(() => {});
    }
    // Templates catalog — shared store warms via OrderTicket too, but
    // an isolated chain mount (modal opened straight to Chain tab)
    // wouldn't have triggered it yet. Idempotent — repeat opens
    // serve from the in-memory cache.
    loadOrderTemplates().catch(() => { /* silent — picker stays empty */ });
    return () => clearTimeout(_readyTimer);
  });
</script>

<div class="oct-root">
  {#if _instrumentsError}
    <div class="oct-instruments-error">
      <span class="oct-instruments-error-msg">Failed to load instruments — IndexedDB may be unavailable.</span>
      <button type="button" class="oct-instruments-retry" onclick={() => _loadInstrumentsSafe()}>Retry</button>
    </div>
  {:else if !instrumentsReady && chainUnderlying}
    <div class="oct-empty">Loading instruments…</div>
  {/if}
  <!-- Account / Underlying / Expiry / Kind / Mode pickers retired per
       operator request — Account lives in the modal header's Account
       dropdown; Underlying is derived from the symbol the operator
       picks at the header level; Expiry defaults to nearest; Kind
       defaults to options + futures; Mode defaults to Basket. Strikes
       grid and futures rows below pick up the defaults reactively. -->
  {#if _allAccounts.length === 0 && !_account}
    <div class="oct-acct-warn">No routable account — pick one from the modal header's Account dropdown.</div>
  {/if}

  <!-- Expiry picker — operator picks which expiry the strike grid
       and futures row are anchored against. Defaults to the nearest
       non-expired contract (seedExpiry → chainExpiries[0] fallback).
       When the picked expiry is within 3 days the chip flips amber
       so the operator sees the imminent roll. -->
  {#if chainUnderlying && chainExpiries.length}
    {@const _dte = _daysToExpiry(chainExpiry)}
    <div class="oct-toolbar">
      <span class="oct-toolbar-label">Expiry</span>
      <div class="oct-expiry-pick">
        <Select
          bind:value={chainExpiry}
          options={_chainExpiryOptions}
          ariaLabel="Chain expiry"
          placeholder="Pick expiry…" />
      </div>
      {#if _dte != null && chainExpiry}
        <span class="oct-expiry-dte"
              class:oct-expiry-dte-warn={_dte <= 3}
              title="Days until this contract's expiry">
          {_dte === 0 ? 'expires today' : `${_dte}d`}
        </span>
      {/if}
      <!-- Template toggle — relocated from SymbolPanel's shell-level
           row (2026-09-30) so it sits with the rest of the chain's own
           per-basket controls. showDemoTplNote / showTemplateBar are
           computed in the shell (SymbolPanel) mirroring the exact
           original if/else-if gates: demo wins when both would be
           true, matching the old branch's precedence.

           RE-GATED (2026-09-30, same day) after finding and fixing the
           actual root cause: loadOrderTemplates() (templates.js) was
           permanently caching a transient fetch failure as an empty
           template list for the rest of the browser tab's session,
           which made `_templates.length > 0` (part of showTemplateBar's
           definition) false indefinitely in a real operator session
           that happened to hit the race once — unrelated to this gate's
           own logic, which a temporary debug bypass correctly ruled
           out as NOT the cause before the real fix was found. -->
      {#if showDemoTplNote}
        <span class="oct-tpl-demo-note">Exit rules (TP / SL / Wing) not available in demo.</span>
      {:else if showTemplateBar}
        <TemplateBar
          {selectedTemplate}
          {sideAwareDefault}
          {nonNoneTemplates}
          {showsWing}
          {shellUsingNone}
          bind:tpOverride
          bind:slOverride
          bind:wingStrikeOffsetOverride
          bind:wingPremPctOverride
          bind:spreadMaxPctOverride
          {onSelectDefault}
          {onSelectNone}
          {onSelectTemplate} />
      {/if}
    </div>
  {/if}

  <!-- Spot + ATM "index pill" retired per operator request — the
       index/underlying value already feeds into the strike grid
       below (strikes are sorted relative to ATM, ATM row gets the
       ATM-highlight class) so a standalone SPOT/ATM chip duplicated
       what the table already encoded. -->


  <!-- Futures rows -->
  {#if chainKinds.includes('fut') && chainFutures.length}
    <div class="chain-futures">
      {#each chainFutures as f (f.s + ':' + (f.e ?? '') + ':' + (f.x ?? ''))}
        {@const futKey = _quickKeyFut(f.s)}
        <div class="chain-fut-row">
          <span class="chain-fut-sym">{f.s}<span class="chain-fut-meta">lot {f.ls}</span></span>
          <span class="chain-side-action">
            <span class="chain-btn-pair">
              <button type="button" class="chain-btn chain-btn-buy"
                      title="BUY {f.s} — adds 1 lot to basket"
                      onclick={() => addFuturesToBasket(f.s, f.ls, 'long')}>+</button>
              <button type="button" class="chain-btn chain-btn-sell"
                      title="SELL {f.s} — adds 1 lot to basket"
                      onclick={() => addFuturesToBasket(f.s, f.ls, 'short')}>−</button>
            </span>
            {#if quickToast?.key === futKey}
              <span class="chain-quick-toast">{quickToast?.msg}</span>
            {/if}
          </span>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Strike grid -->
  {#if chainKinds.includes('opt') && chainStrikes.length}
    <div class="chain-grid-wrap">
      {#if _pricesFetching && !chainQuotesMap}
        <!-- Operator (2026-09-30): "the bottom border shows up below the
             header and disappears" — root cause: this message used to be
             a SIBLING block rendered BEFORE .chain-grid-wrap in normal
             flow, so for the ~200-300ms window before live quotes arrive
             it pushed the entire grid (header + border included) down by
             its own line-height; once quotes landed and the message
             unmounted, the grid jumped back up to fill that gap — read as
             the header's border "appearing, then disappearing" as it
             visibly relocated. Fixed by moving it INSIDE .chain-grid-wrap
             as an absolutely-positioned overlay (.chain-grid-wrap now has
             position: relative below) so it floats on top of the
             already-rendered grid instead of occupying flow space that
             later collapses. -->
        <div class="oct-empty chain-fetching-overlay">Fetching live prices…</div>
      {/if}
      <table class="chain-grid">
        <colgroup>
          <col class="chain-col-ce" />
          <col class="chain-col-strike" />
          <col class="chain-col-pe" />
        </colgroup>
        <thead>
          <tr>
            <th class="chain-th-ce">CE</th>
            <th class="chain-th-strike">Strike</th>
            <th class="chain-th-pe">PE</th>
          </tr>
        </thead>
        <tbody>
          {#each chainStrikes as k (k)}
            {@const isAtm = chainAtmStrike != null && k === chainAtmStrike}
            {@const dir   = chainSpot != null ? (k < chainSpot ? 'itm-call' : k > chainSpot ? 'itm-put' : 'atm') : ''}
            {@const ceKey = _quickKeyOpt(k, 'CE')}
            {@const peKey = _quickKeyOpt(k, 'PE')}
            {@const activeRow = activeOptionRow?.strike === k}
            {@const ceQ = chainQuotesMap?.[String(k)]?.ce}
            {@const peQ = chainQuotesMap?.[String(k)]?.pe}
            {@const ceSpreadWide = ceQ?.bid > 0 && ceQ?.ask > 0 && (ceQ.ask - ceQ.bid) / ((ceQ.ask + ceQ.bid) / 2) > 0.10}
            {@const peSpreadWide = peQ?.bid > 0 && peQ?.ask > 0 && (peQ.ask - peQ.bid) / ((peQ.ask + peQ.bid) / 2) > 0.10}
            {@const ceLeg = _basketLegByKey.get(`${k}:CE`)}
            {@const peLeg = _basketLegByKey.get(`${k}:PE`)}
            {@const tmplAttached = !templateIsNone && !!templateName}
            {@const tmplShort = tmplAttached ? String(templateName).slice(0, 6) : ''}
            {#if isAtm}
              <tr class="chain-row chain-row-{dir} chain-row-atm" class:chain-row-active={activeRow} use:chainAtmRow>
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(ceQ?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(ceQ?.ask)}</span>{#if ceSpreadWide}<span class="chain-cell-spread-warn" title="Wide spread — {_fmtLtp(ceQ.ask - ceQ.bid)} ({((ceQ.ask - ceQ.bid)/((ceQ.ask+ceQ.bid)/2)*100).toFixed(0)}% of mid)">⚠</span>{/if}
                    </span>
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                disabled={!(ceQ?.bid > 0 || ceQ?.ask > 0)}
                                title={ceQ?.bid > 0 || ceQ?.ask > 0 ? `BUY ${k} CE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                disabled={!(ceQ?.bid > 0 || ceQ?.ask > 0)}
                                title={ceQ?.bid > 0 || ceQ?.ask > 0 ? `SELL ${k} CE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                      </span>
                      {#if ceLeg}
                        <span class="chain-leg-badge" class:chain-leg-badge-tmpl={tmplAttached}
                              title={`${ceLeg.side} ${ceLeg.lots} lot${ceLeg.lots === 1 ? '' : 's'} × ${ceLeg.lotSize} = ${ceLeg.lots * ceLeg.lotSize} qty${tmplAttached ? ' · bracket: ' + templateName : ' · no bracket'}`}>{ceLeg.lots}L{tmplAttached ? ' · ' + tmplShort : ''}</span>
                      {/if}
                      {#if quickToast?.key === ceKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                  </span>
                </td>
                <td class="chain-td-strike chain-td-strike-atm">{k.toFixed(0)}</td>
                <td class="chain-td-pe">
                  <span class="chain-cell-row chain-cell-row-pe">
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                disabled={!(peQ?.bid > 0 || peQ?.ask > 0)}
                                title={peQ?.bid > 0 || peQ?.ask > 0 ? `BUY ${k} PE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                disabled={!(peQ?.bid > 0 || peQ?.ask > 0)}
                                title={peQ?.bid > 0 || peQ?.ask > 0 ? `SELL ${k} PE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                      </span>
                      {#if peLeg}
                        <span class="chain-leg-badge" class:chain-leg-badge-tmpl={tmplAttached}
                              title={`${peLeg.side} ${peLeg.lots} lot${peLeg.lots === 1 ? '' : 's'} × ${peLeg.lotSize} = ${peLeg.lots * peLeg.lotSize} qty${tmplAttached ? ' · bracket: ' + templateName : ' · no bracket'}`}>{peLeg.lots}L{tmplAttached ? ' · ' + tmplShort : ''}</span>
                      {/if}
                      {#if quickToast?.key === peKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(peQ?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(peQ?.ask)}</span>{#if peSpreadWide}<span class="chain-cell-spread-warn" title="Wide spread — {_fmtLtp(peQ.ask - peQ.bid)} ({((peQ.ask - peQ.bid)/((peQ.ask+peQ.bid)/2)*100).toFixed(0)}% of mid)">⚠</span>{/if}
                    </span>
                  </span>
                </td>
              </tr>
            {:else}
              <tr class="chain-row chain-row-{dir}" class:chain-row-active={activeRow}>
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(ceQ?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(ceQ?.ask)}</span>{#if ceSpreadWide}<span class="chain-cell-spread-warn" title="Wide spread — {_fmtLtp(ceQ.ask - ceQ.bid)} ({((ceQ.ask - ceQ.bid)/((ceQ.ask+ceQ.bid)/2)*100).toFixed(0)}% of mid)">⚠</span>{/if}
                    </span>
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                disabled={!(ceQ?.bid > 0 || ceQ?.ask > 0)}
                                title={ceQ?.bid > 0 || ceQ?.ask > 0 ? `BUY ${k} CE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                disabled={!(ceQ?.bid > 0 || ceQ?.ask > 0)}
                                title={ceQ?.bid > 0 || ceQ?.ask > 0 ? `SELL ${k} CE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                      </span>
                      {#if ceLeg}
                        <span class="chain-leg-badge" class:chain-leg-badge-tmpl={tmplAttached}
                              title={`${ceLeg.side} ${ceLeg.lots} lot${ceLeg.lots === 1 ? '' : 's'} × ${ceLeg.lotSize} = ${ceLeg.lots * ceLeg.lotSize} qty${tmplAttached ? ' · bracket: ' + templateName : ' · no bracket'}`}>{ceLeg.lots}L{tmplAttached ? ' · ' + tmplShort : ''}</span>
                      {/if}
                      {#if quickToast?.key === ceKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                  </span>
                </td>
                <td class="chain-td-strike">{k.toFixed(0)}</td>
                <td class="chain-td-pe">
                  <span class="chain-cell-row chain-cell-row-pe">
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                disabled={!(peQ?.bid > 0 || peQ?.ask > 0)}
                                title={peQ?.bid > 0 || peQ?.ask > 0 ? `BUY ${k} PE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                disabled={!(peQ?.bid > 0 || peQ?.ask > 0)}
                                title={peQ?.bid > 0 || peQ?.ask > 0 ? `SELL ${k} PE` : "No quote — price unknown"}
                                onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                      </span>
                      {#if peLeg}
                        <span class="chain-leg-badge" class:chain-leg-badge-tmpl={tmplAttached}
                              title={`${peLeg.side} ${peLeg.lots} lot${peLeg.lots === 1 ? '' : 's'} × ${peLeg.lotSize} = ${peLeg.lots * peLeg.lotSize} qty${tmplAttached ? ' · bracket: ' + templateName : ' · no bracket'}`}>{peLeg.lots}L{tmplAttached ? ' · ' + tmplShort : ''}</span>
                      {/if}
                      {#if quickToast?.key === peKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(peQ?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(peQ?.ask)}</span>{#if peSpreadWide}<span class="chain-cell-spread-warn" title="Wide spread — {_fmtLtp(peQ.ask - peQ.bid)} ({((peQ.ask - peQ.bid)/((peQ.ask+peQ.bid)/2)*100).toFixed(0)}% of mid)">⚠</span>{/if}
                    </span>
                  </span>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {:else if chainUnderlying && chainExpiry}
    <div class="oct-empty">No strikes for {chainUnderlying} expiring {chainExpiry}. Try a different underlying or expiry.</div>
  {/if}

  <!-- Audit fix — basketError used to live INSIDE the
       `chainBasket.length && !_externalBasket` block, so in external-
       basket mode (the SymbolPanel shell mounts the tab) the operator's
       +CE / +PE / +Fut clicks silently no-op'd when `_account=''` (a
       race against the async `loadAccounts` populating the account
       prop). The basket bar never appeared and there was no feedback.
       Hoisted out so the error always renders when set; clears on the
       next successful add. -->
  {#if basketError}
    <div class="chain-basket-err" role="alert">{basketError}</div>
  {/if}

  <!-- Pre-submit spread gate banner — only ever visible once the
       operator has clicked Submit AND at least one staged leg has a
       wing/offset configured (see runPreSubmitGate() above). -->
  {#if _gateState.phase !== 'idle'}
    <div class="chain-spread-gate"
         class:chain-spread-gate-wide={_gateState.phase === 'wide'}
         class:chain-spread-gate-error={_gateState.phase === 'error' || _gateState.phase === 'timeout'}
         data-testid="spread-gate-banner"
         data-phase={_gateState.phase}
         role="alert">
      {#if _gateState.phase === 'checking'}
        <span class="chain-spread-gate-msg">Checking spread…</span>
        <button type="button" class="chain-spread-gate-btn" onclick={_gateCancel}>Cancel</button>
      {:else if _gateState.phase === 'wide'}
        <span class="chain-spread-gate-msg">
          ⚠ Wide spread —{#each _gateState.legs.filter((l) => l.ok === false) as l (l.tradingsymbol)} {l.label} {l.spread_pct != null ? l.spread_pct.toFixed(1) : '?'}%{/each}
        </span>
        <button type="button" class="chain-spread-gate-btn chain-spread-gate-btn-primary"
                data-testid="spread-gate-place-anyway" onclick={_gatePlaceAnyway}>Place anyway</button>
        <button type="button" class="chain-spread-gate-btn" onclick={_gateCancel}>Cancel</button>
      {:else if _gateState.phase === 'error'}
        <span class="chain-spread-gate-msg">⚠ Spread check failed</span>
        <button type="button" class="chain-spread-gate-btn" data-testid="spread-gate-retry" onclick={_gateRetry}>Retry</button>
        <button type="button" class="chain-spread-gate-btn chain-spread-gate-btn-primary"
                data-testid="spread-gate-place-anyway" onclick={_gatePlaceAnyway}>Place anyway</button>
        <button type="button" class="chain-spread-gate-btn" onclick={_gateCancel}>Cancel</button>
      {:else if _gateState.phase === 'timeout'}
        <span class="chain-spread-gate-msg">⚠ Spread check timed out</span>
        <button type="button" class="chain-spread-gate-btn" data-testid="spread-gate-retry" onclick={_gateRetry}>Retry</button>
        <button type="button" class="chain-spread-gate-btn chain-spread-gate-btn-primary"
                data-testid="spread-gate-place-anyway" onclick={_gatePlaceAnyway}>Place anyway</button>
        <button type="button" class="chain-spread-gate-btn" onclick={_gateCancel}>Cancel</button>
      {/if}
    </div>
  {/if}
</div>

<style>
  /* Root is a flex column so the strike grid can grow to fill the
     modal body's remaining height. `min-height: 0` is the canonical
     flex-grow gate — without it the table would refuse to shrink
     below its content height and overflow the modal. */
  .oct-root {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    flex: 1 1 auto;
    min-height: 0;
    width: 100%;
  }

  .oct-label {
    display: block;
    font-size: var(--fs-sm);
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.18rem;
    opacity: 0.85;
  }
  .oct-account-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }
  .oct-acct-hint { font-size: var(--fs-sm); color: var(--algo-muted); font-style: italic; }
  /* Subtle inline warn line shown when no broker account is loaded
     and none was supplied via the modal header. The legacy
     .oct-account-row wrapper is gone; this stand-alone div replaces
     the empty-state hint that used to render inside it. */
  .oct-acct-warn {
    font-size: var(--fs-sm);
    color: var(--c-action);
    background: var(--algo-amber-bg-soft);
    border: 1px solid rgba(251, 191, 36, 0.28);
    border-radius: 3px;
    padding: 0.28rem 0.5rem;
    margin: 0 0 0.4rem;
  }
  /* Expiry toolbar — sits above the futures row + strike grid so
     operator picks the contract month BEFORE scanning strikes.
     --ctl-h scoped here (2026-09-30 height audit) — the row's boxed
     controls were split across two heights: the Select trigger +
     TemplateBar's toggle button (both read var(--ctl-h, 1.55rem), so
     with nothing declared here they fell through to that 1.55rem
     fallback) vs the DTE chip + TP%/SL%(/Wing) override inputs
     (explicit 1.4rem, rendered whenever the toggle is ON — which is
     the default mount state). Live count in the default-ON state:
     3-4 controls at 1.4rem vs 2 at 1.55rem — 1.4rem is the majority,
     so pin --ctl-h to it here rather than inventing a third value.
     TemplateBar's button uses a rigid `height`, so it shrinks to match
     exactly. Select.svelte's trigger only sets `min-height` (a floor,
     by design — shared app-wide, its own fallback is deliberately
     untouched per an earlier fix's test comment) — its DEFAULT vertical
     padding (0.25rem top+bottom) plus --fs-sm text needs ~24.4px on
     its own, which is MORE than the 1.4rem/22.4px floor, so the floor
     never actually governs and the trigger stays oversized. The
     `:global(.rbq-select-trigger)` padding override just below closes
     that gap (0.15rem — the same value the DTE chip already uses)
     without touching Select.svelte itself; only Selects rendered
     inside THIS toolbar (the expiry picker, and TemplateBar's own
     "Specific tmpl" Select inside its expand panel) are affected. */
  .oct-toolbar {
    --ctl-h: 1.4rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.1rem 0.35rem;
    margin-bottom: 0.25rem;
    /* Dashed border-bottom removed (2026-09-30, operator: "the dotted
       line is not needed") — it sat directly above the CE/Strike/PE
       header row and read as a stray second separator. */
    flex-wrap: wrap;
  }
  .oct-toolbar :global(.rbq-select-trigger) {
    padding-block: 0.15rem;
  }
  .oct-toolbar-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251, 191, 36, 0.7);
    flex-shrink: 0;
  }
  /* Narrowed 20% (2026-09-30, operator request): 11rem/16rem → 8.8rem/12.8rem. */
  .oct-expiry-pick { min-width: 7.7rem; max-width: 11.2rem; flex: 0 1 auto; }
  /* Days-to-expiry chip — slate-blue resting, amber when ≤ 3 days
     to expiry so the operator sees the imminent roll. */
  .oct-expiry-dte {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    color: var(--algo-muted);
    background: rgba(125, 145, 184, 0.08);
    border: 1px solid rgba(125, 145, 184, 0.22);
    border-radius: 3px;
    padding: 0.15rem 0.45rem;
    flex-shrink: 0;
    /* Height parity with TemplateBar's TP%/SL%/Wing override inputs
       (.oes-basket-tpl-param > input, height: 1.4rem) — this chip sits
       in the same Expiry toolbar row and was visibly shorter with no
       explicit height. 1.4rem is a local, hardcoded value here — kept
       that way even after the 2026-09-30 audit made .oct-toolbar's
       --ctl-h equal 1.4rem too (see that rule's own comment): this
       chip and the TP%/SL% inputs were the reference the row's OTHER
       controls (Select trigger, Template toggle) were pulled DOWN to
       match, not consumers of --ctl-h themselves. */
    min-height: 1.4rem;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
  }
  .oct-expiry-dte-warn {
    color: var(--c-action);
    background: var(--algo-amber-bg);
    border-color: rgba(251, 191, 36, 0.42);
  }
  /* Demo-mode note — replaces the Template toggle when exit rules
     (TP/SL/Wing) aren't available to an anonymous session. Muted
     slate italic so it reads as "not active" without competing with
     the amber toggle it stands in for. */
  .oct-tpl-demo-note {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: rgba(180, 200, 230, 0.65);
    font-style: italic;
    /* Audit fix (2026-09-30) — flex-shrink: 0 removed. Verified via an
       isolated render (this rule's exact flex siblings + values) that
       it was a no-op at every realistic viewport (320/375/412/600px):
       the wrap-to-its-own-line decision is governed by this item's
       max-content width vs the remaining space on the CURRENT line,
       which is independent of flex-shrink — so the wrap behavior
       operators already approved ("happens to look fine") is
       unaffected at those widths. Below the realistic phone-viewport
       floor (tested 180/250px) flex-shrink:0 actively made it WORSE:
       the sentence's box stayed pinned to its full single-line width
       and bled past the toolbar/viewport edge (the exact horizontal-
       overflow class of bug .oes-modal's own mobile overflow-x:hidden
       backstop exists to guard against) instead of wrapping in place.
       Removing it lets the text wrap within its own box at any width
       that's actually too narrow to hold it on one line. */
  }
  .oct-acct-single {
    font-family: monospace;
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--algo-slate);
    padding: 0.2rem 0.4rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
  }
  /* Account picker uses the custom Select component (matches the
     OrderTicket modal's popup palette). Wrap caps the trigger width
     so it doesn't stretch the whole row. */
  .oct-acct-select-wrap { min-width: 9rem; }
  .oct-controls {
    display: flex;
    flex-wrap: nowrap;
    gap: 0.4rem;
    align-items: flex-end;
  }
  .oct-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1 1 0;
    min-width: 0;
  }
  /* Place-mode toggle — outline pill row, active button highlighted. */
  .oct-field-mode { flex: 0 0 auto; }
  .oct-mode-toggle {
    display: inline-flex;
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 3px;
    overflow: hidden;
  }
  .oct-mode-btn {
    padding: 0.3rem 0.55rem;
    border: none;
    background: transparent;
    color: var(--text-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
  }
  .oct-mode-btn + .oct-mode-btn { border-left: 1px solid rgba(255,255,255,0.12); }
  .oct-mode-btn:hover { background: rgba(126,151,184,0.10); color: #f1f7ff; }
  /* Audit fix — neutral cyan active state. Pre-fix the "place mode"
     workflow toggle painted green when on, which read as "direction =
     long" on every other surface (SymbolPanel uses green = BUY, red =
     SELL). On a SELL-heavy session this button visually competed with
     the SELL pill's red. Workflow toggles use cyan throughout the
     platform (card-control trio, Refresh, etc.). */
  .oct-mode-btn.on {
    background: rgba(34, 211, 238, 0.18);
    color: var(--algo-cyan, var(--c-info));
  }

  .oct-spot-row { margin-bottom: 0.25rem; }
  .oct-empty { font-size: var(--fs-sm); color: var(--text-muted); font-style: italic; margin-top: 0.5rem; }
  .oct-instruments-error {
    display: flex; align-items: center; gap: 0.6rem;
    margin-top: 0.5rem; padding: 0.35rem 0.6rem;
    background: rgba(248,113,113,0.10); border: 1px solid rgba(248,113,113,0.35);
    border-radius: 4px;
  }
  .oct-instruments-error-msg { font-size: var(--fs-sm); color: var(--c-short, #f87171); }
  .oct-instruments-retry {
    font-size: var(--fs-sm); font-weight: 600; padding: 0.15rem 0.5rem;
    border: 1px solid rgba(251,191,36,0.55); border-radius: 3px;
    background: rgba(251,191,36,0.10); color: var(--c-action, #fbbf24);
    cursor: pointer; white-space: nowrap;
  }
  .oct-instruments-retry:hover { background: rgba(251,191,36,0.20); }
  .oct-inst-err { color: var(--c-short); font-style: normal; display: flex; align-items: center; gap: 0.5rem; }
  .oct-retry-btn {
    padding: 0.15rem 0.5rem; border-radius: 2px;
    border: 1px solid var(--c-short); background: transparent; color: var(--c-short);
    font-family: var(--font-numeric); font-size: var(--fs-xs); font-weight: 700;
    cursor: pointer; letter-spacing: 0.04em;
  }
  .oct-retry-btn:hover { background: rgba(248,113,113,0.12); }

  /* ── chain grid (mirrors admin/options styles) ────────────────── */
  .chain-futures {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 0.35rem;
  }
  .chain-fut-row {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.5rem;
    background: rgba(125,211,252,0.06);
    border: 1px solid rgba(125,211,252,0.25);
    border-radius: 3px;
  }
  .chain-fut-sym {
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    color: #7dd3fc;
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
  }
  .chain-fut-meta {
    font-size: var(--fs-xs);
    color: var(--text-muted);
    font-weight: 500;
  }
  /* Strike-grid height tightened so the basket + chart panels below
     stay visible without forcing a second scroll on the modal body.
     Earlier 22rem felt cramped on tablet viewports and pushed the
     LogPanel bottom panel out of view. */
  /* Strike grid: grow to fill the modal body's available height
     instead of clamping to 14rem. The parent .oct-root has flex:1, and
     here we take whatever vertical room is left after the futures bar
     + ATM gauge above. Operator gets a full-height chain without
     forcing a second internal scroll. */
  .chain-grid-wrap {
    /* position: relative (2026-09-30) so .chain-fetching-overlay (below)
       can absolutely position itself against THIS box instead of the
       page — see the markup comment above for why this overlay exists. */
    position: relative;
    overflow-y: auto;
    /* Operator (2026-09-29): "give overall background to chain area
       consistent with overall color scheme on mobile and desktop" —
       previously only the header cells carried a background
       (rgba(13,21,38,0.6), a translucent overlay of the same hue as
       --algo-bg-elev2 but not the token itself), while the body rows
       had none — a patchy, two-toned look rather than one consistent
       surface. One solid background at the standard card-wrapper
       elevation, covering the whole grid (header + body), same on
       every viewport.
       Corrected (2026-09-30) — --algo-bg-elev2 was a flat solid navy
       that read as a visibly DIFFERENT (darker) surface from every
       other card-like surface in the app (.algo-card, .bucket-card,
       chart wrappers), which all use --card-bg-gradient. Switched to
       the app's actual canonical card surface token instead.
       Corrected AGAIN same day — operator: now reads "almost the same
       as other areas", too blended into generic cards, lost the cue
       that this is a strike-grid surface specifically. --chain-depth-bg
       (app.css) is the settled middle ground: --card-bg-gradient with a
       thin amber wash on top — same family as every other card, but
       recognizably its own tier. See app.css for the full history.
       REVERSED YET AGAIN (2026-09-30, operator: "keep the chain
       background colors in sync with price chart background with
       the exception of in the money call and in the put area") — the
       price chart and the order ticket's depth ladder (.ot-depth) had
       both already moved to bare --card-bg-gradient earlier the same
       day; this grid-wrap was the one surface still left on
       --chain-depth-bg. Switched to match. The sticky header
       (.chain-th-ce/-pe/-strike, below) intentionally KEEPS its own,
       distinct background token — operator confirmed the header/body
       contrast should survive this change (the token itself later
       became --card-bg-elevated, see the header rule's own comment
       for that follow-up). ITM call/put washes
       (.chain-row-itm-call/-put, further below) are untouched; they
       layer on top of whatever base background this rule sets, so
       they remain visually distinct regardless of this token. */
    background: var(--card-bg-gradient);
    /* Operator: "order ticket window is wider than viewport mobile
       sometimes" — root-caused to THIS table: table-layout defaults to
       `auto`, which sizes columns off cell content's natural minimum
       width, ignoring .chain-col-ce/-strike/-pe's % hints entirely once
       CE/PE's bid+ask+stepper content doesn't fit a narrow column. The
       table then grows past its wrapper (measured 406px inside a
       ~350px mobile modal) with nothing clipping the X axis, bleeding
       the whole modal out of the viewport. `table-layout: fixed` below
       makes the % column widths authoritative; this overflow-x is a
       clipping backstop for the rare cell whose content still doesn't
       fit the fixed column. */
    overflow-x: hidden;
    flex: 1 1 0;
    /* Operator: "show more pe and ce rows in chain". Was 9rem
       (~5–6 strike rows at default row height); bumped to 22rem
       so the operator sees ~15+ rows around ATM at a glance
       without having to scroll. Reads --chain-depth-h from the
       enclosing order modal so the Ticket-tab depth ladder matches
       this height and the modal doesn't jump on tab flip. Falls
       back to 22rem for standalone callers. The flex:1 1 0 above
       still lets the grid grow to fill the full modal body when
       there's more room. */
    min-height: var(--chain-depth-h, 22rem);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 3px;
  }
  /* Floats over the already-rendered grid instead of occupying flow
     space — see the markup comment at this element's {#if} for the
     layout-shift bug this fixes. z-index above the sticky header
     (z-index: 2) so it's actually visible on top while shown. */
  .chain-fetching-overlay {
    position: absolute;
    /* Cleared below the sticky header (measured ~1.33rem tall) so this
       overlay sits over the first data rows, not on top of the CE /
       Strike / PE labels — an earlier version at top: 0.35rem visually
       covered the header text for the ~200-300ms this is shown. */
    top: 1.5rem;
    left: 0.5rem;
    z-index: 1;
    margin: 0;
    pointer-events: none;
  }
  /* Operator (2026-09-29): "mobile chain looks better than desktop
     chain which looks very cluttered" was FIRST fixed by capping the
     grid to a mobile-like max-width — later REVERSED by the operator
     ("let the chain occupy fully available spaces"). The cap is gone.
     Deliberately did NOT change .chain-cell-row-ce/-pe's flex-end/
     flex-start alignment (the +/- buttons sitting immediately next to
     the Strike column, quotes on the outer edge) — that's a considered,
     explicitly-commented layout choice, not the source of the
     "cluttered" complaint. The consistent solid background (below),
     larger desktop font-size, and visible button backgrounds applied
     elsewhere in this file are what should keep a full-width grid from
     reading as sparse/clashing rather than re-litigating that layout. */
  /* NOTE: border-bottom/font-size desktop overrides moved to a later
     @media (min-width: 640px) block below (after the base .chain-grid /
     .chain-row > td / .chain-th-* / .chain-cell-* rules they override)
     so they actually win the cascade — same selector + specificity,
     source order decides, and this early block sat BEFORE those base
     rules. */
  .chain-grid {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-family: monospace;
    font-size: var(--fs-md);
  }
  .chain-col-ce     { width: 44%; }
  .chain-col-strike { width: 12%; }
  .chain-col-pe     { width: 44%; }
  /* Operator (2026-09-29): "ce, strike, pe header row should not be
     scrollable" — sticky within .chain-grid-wrap's own scroll
     container (overflow-y: auto). Needs a fully OPAQUE background
     (not the old 0.6-alpha overlay) so scrolled body rows don't show
     through underneath it.
     Corrected (2026-09-30) — was --algo-bg-elev2 (flat solid navy,
     mismatched the app's actual card-surface token). Then briefly
     --card-bg-gradient, same token as .chain-grid-wrap, matching the
     app's every other card-like surface (.algo-card, .bucket-card,
     chart wrappers) — reverted again same day, too blended into
     generic cards. Then --chain-depth-bg (same token as
     .chain-grid-wrap, intentionally pixel-matched to the body).
     REVERSED again same day (operator: "chain header background
     should not be same as chain [body]... slight variation for
     contrast") — the header used its own --chain-header-bg (app.css):
     same --card-bg-gradient family, a slightly stronger amber wash
     (0.07 vs the body's 0.04).
     REVERSED YET AGAIN (2026-09-30, operator: "the row containing ce,
     strike, pe is on black and gray side which i don't like... below
     it there is no white border") — live computed-style check showed
     the 0.07 amber wash over the dark navy gradient blends to
     ~rgb(45,52,66), too low-saturation to read as anything but plain
     "black and gray". Switched to --card-bg-elevated (app.css) — an
     actually-lighter navy tier, same family, no wash — which reads
     as a genuinely brighter/distinct surface rather than the same
     darkness with a faint tint. --chain-header-bg token removed from
     app.css as dead code (no longer referenced). */
  /* Operator (2026-09-30): CE/PE header text sat on the OPPOSITE
     side from the +/- buttons — .chain-cell-row-ce/-pe (below,
     deliberately unchanged) push the quote+buttons block toward the
     Strike column (flex-end for CE, flex-start for PE), but the
     header text alignment was the mirror image of that. Flipped so
     header labels sit over their own row's actual content. Border
     bumped 0.05 -> 0.18, then -> 0.35 (2026-09-30, operator: "below
     it there is no white border" — 0.18-alpha white on a dark navy
     background wasn't reading as a visible line) so the header row
     clearly separates from the strike rows below it.
     CHANGED border-bottom -> box-shadow: inset (2026-09-30, operator:
     "again the border shows and disappears" / "when it shows the
     background colors for in the money calls and puts, it
     disappears") — root cause: `position: sticky` `<th>` elements
     inside a `border-collapse: collapse` table (.chain-grid, below)
     are a well-documented Chrome/WebKit bug class where the browser
     can drop the sticky element's own border on repaint, because
     collapsed borders are painted on the table's shared border layer
     rather than each cell's own box, which doesn't composite reliably
     with a stickied element's own layer. A large simultaneous repaint
     — exactly what happens when chainSpot resolves and every ITM/OTM
     .chain-td-ce/-pe cell's background wash switches on at once (see
     .chain-row-itm-call/-put below) — is precisely the kind of trigger
     that surfaces it. `box-shadow: inset` is not part of table border-
     collapse semantics at all, so it is unaffected by this bug and
     renders reliably through any repaint. This is the standard fix
     recommended for this exact "sticky thead border disappears" bug
     class.
     Dialed back 0.35 white -> 0.18 amber (2026-09-30, operator:
     "reduce the thickness of the border... if the border thinner
     with amber shade it may look better") — a 1px line can't get
     visually thinner than 1px, so "thinner" here is read as lower
     opacity / warmer color rather than sub-pixel width. 0.18 amber
     matched the existing --algo-amber divider convention already
     used elsewhere in this file (.chain-row-atm's own border-bottom,
     below) instead of introducing a new one-off value.
     Bumped again 0.18 -> 0.28 (2026-09-30, operator: "the border
     color should be a little strong") — 0.28 also already exists
     elsewhere in this file as a border alpha (.oct-acct-warn, above)
     rather than a new one-off value.
     Bumped again 0.28 -> 0.40 (2026-09-30, operator: "the bottom
     border should be stronger on amber side") — kept in sync with
     the matching bump on the order ticket's .ot-depth-header-bg
     (OrderDepth.svelte).
     Top edge added (2026-09-30, operator: "add top border also to
     headers in chain and order ticket") — a second inset box-shadow
     layer (inset 0 1px 0, positive y-offset = top edge, vs. the
     existing -1px = bottom edge), same amber/alpha, sandwiching the
     header row on both sides instead of just underlining it.
     text-align flipped (2026-09-30, operator: "ce label should be
     left aligned and pe should be right aligned") — was right/left;
     changed to CE left-aligned, PE right-aligned. Deliberately scoped
     to just this header text — the data rows' own +/- button/quote
     layout (.chain-cell-row-ce/-pe, their own flex-end/flex-start,
     further below) is untouched.
     REVERSED again same day (operator: "reverse ce and re label
     alignment") — back to the ORIGINAL right/left. */
  /* Background/bottom-border decoration reused verbatim from the Legs
     grid's own sticky header (2026-09-30, operator: "you can use legs
     grid header decoration like background, borders, etc to chain and
     order ticket header. text color can remain the same") — see
     .cand-headrow in derivatives/+page.svelte for the source. Still a
     box-shadow (not a real border) — this header is a sticky <th> in
     a border-collapse <table>, the exact repaint-reliability trap the
     box-shadow workaround already on this rule was built to avoid, so
     switching to a literal border-bottom here would reintroduce it.
     Top inset layer (added earlier the same session) is dropped — the
     Legs grid header never had one. Text color (var(--c-long)/
     var(--c-short)) intentionally untouched. */
  .chain-th-ce      { text-align: right;  color: var(--c-long); padding: 0.2rem 0.5rem; font-weight: 700; font-size: var(--fs-sm); box-shadow: inset 0 -1px 0 var(--algo-amber-border-soft); background: linear-gradient(rgba(15,23,42,0.65), rgba(15,23,42,0.65)), #1d2a44; position: sticky; top: 0; z-index: 2; }
  .chain-th-pe      { text-align: left;   color: var(--c-short); padding: 0.2rem 0.5rem; font-weight: 700; font-size: var(--fs-sm); box-shadow: inset 0 -1px 0 var(--algo-amber-border-soft); background: linear-gradient(rgba(15,23,42,0.65), rgba(15,23,42,0.65)), #1d2a44; position: sticky; top: 0; z-index: 2; }
  /* Operator: "reduce the space before and after strike in chain" —
     strike is a short 4-5 digit number, doesn't need the same
     horizontal padding as CE/PE (which carry a quote + a stepper
     button). Tightened from 0.3rem to 0.1rem; column width narrowed
     from 16% to 12%, giving CE/PE the reclaimed width.
     Left/right borders folded into the same box-shadow: inset as the
     bottom edge (2026-09-30, same border-collapse/sticky fix as
     .chain-th-ce/-pe above) instead of separate border-left/-right
     declarations, for the same repaint-reliability reason.
     Padding widened 0.1rem -> 0.4rem (operator: "the gap between ce,
     strike, pe label should be increased") — the earlier "move ce pe
     away from strike" fix only widened the DATA rows' Strike cell
     (.chain-row>td.chain-td-strike), not this header cell, leaving
     the header row's CE|Strike|PE gap visibly tighter than the data
     rows below it.
     Reduced again 0.4rem -> 0.22rem (2026-09-30, operator: "the gap
     between ce, strike, pe values should be reduced") — 0.4rem read
     as too wide once seen rendered; 0.22rem keeps a real, visible gap
     (vs. the original cramped 0.1rem) without being as wide as the
     brief 0.4rem experiment. Kept in sync with the DATA row's own
     Strike cell padding (below) for header/body consistency. */
  .chain-th-strike  { text-align: center; color: var(--algo-slate); padding: 0.2rem 0.22rem; font-weight: 700; font-size: var(--fs-sm); box-shadow: inset 0 -1px 0 var(--algo-amber-border-soft), inset 1px 0 0 rgba(255,255,255,0.03), inset -1px 0 0 rgba(255,255,255,0.03); background: linear-gradient(rgba(15,23,42,0.65), rgba(15,23,42,0.65)), #1d2a44; position: sticky; top: 0; z-index: 2; }
  .chain-row > td {
    /* Operator: "reduce the height of chain grid for strike prices
       by half". Vertical padding zeroed (was 0.1rem), button
       padding + font compressed below — each strike row drops
       from ~18px to ~9px so the whole grid roughly halves in
       height. */
    padding: 0 0.4rem;
    /* Very-subtle pass (2026-09-29) — operator: "row and column
       borders should be very very subtle", halved again from 0.04. */
    border-bottom: 1px solid rgba(255,255,255,0.025);
    line-height: 1.1;
  }
  .chain-row:last-child > td { border-bottom: 0; }
  .chain-td-ce      { text-align: left; }
  .chain-td-pe      { text-align: right; }
  .chain-cell-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
  }
  /* CE: push block to right so +/- sit immediately left of the strike column.
     PE: push block to left so +/- sit immediately right of the strike column. */
  .chain-cell-row-ce { justify-content: flex-end; }
  .chain-cell-row-pe { justify-content: flex-start; }
  /* Audit fix — tabular-nums on strike + bid/ask cells. Strikes are
     fixed integers but column-align with LTP/IV/OI in adjacent
     columns; without tabular-nums the digit widths can drift between
     fonts. Bid + ask are the most frequently changing numbers in
     the chain — without tabular-nums, digits shift horizontally
     on every quote update, creating visual jitter that obscures
     the spread. */
  .chain-row > td.chain-td-strike {
    text-align: center;
    color: var(--algo-slate);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    /* Widened 0.1rem -> 0.4rem (operator: "move ce pe away from
       strike") — CE/PE content is flex-end/flex-start aligned toward
       this column (see .chain-cell-row-ce/-pe above), so the visible
       gap between CE/PE and Strike is this cell's own left/right
       padding; the earlier 0.1rem was tightened for a different
       reason ("reduce the space before and after strike") and read
       as too close once CE/PE content butted right up against it.
       text-align: center keeps "Strike"/the numbers centered in the
       column regardless of this padding value, so widening it
       doesn't misalign the header label against the data rows below.
       Reduced again 0.4rem -> 0.22rem (2026-09-30, operator: "the gap
       between ce, strike, pe values should be reduced") — matches
       the header's own Strike cell padding (.chain-th-strike, above)
       for header/body consistency. */
    padding-left: 0.22rem;
    padding-right: 0.22rem;
    /* Subtle column divider between CE | Strike | PE — same
       whisper-quiet weight as the row dividers above, completing
       the "grid" reading operator asked for without adding visual
       clutter back in. Halved again 0.06->0.03 (2026-09-29, "very
       very subtle" pass). */
    border-left: 1px solid rgba(255,255,255,0.03);
    border-right: 1px solid rgba(255,255,255,0.03);
  }
  .chain-td-strike-atm { color: var(--c-action); font-weight: 800; letter-spacing: 0.04em; }
  .chain-cell-quote {
    display: inline-flex;
    align-items: baseline;
    /* Audit fix (2026-09-30) — was `min-width: 3.4rem`. At 320-375px a
       CE/PE column's real content (quote text + gap + +/- stepper
       pair) can exceed the column's fixed % width. .chain-grid-wrap's
       overflow-x:hidden is a deliberate clipping backstop (see that
       rule's own comment) — but a fixed 3.4rem floor meant this box
       could never give way, so the overflow clipped whichever end sat
       outside the wrap, sometimes eating into the steppers themselves
       rather than just the quote. `min-width: 0` is a PERMISSION, not
       a forced width — flexbox never shrinks an item below its content
       size unless the container is actually narrower than its
       children's combined hypothetical size, so this is a no-op on
       every viewport wide enough to fit both boxes at full size.
       Paired with overflow/ellipsis so a genuine squeeze costs quote-
       text precision (a trailing digit) rather than a hard character
       clip. .chain-side-action (below) is marked flex-shrink: 0 so the
       +/- buttons are never the side that gives. */
    min-width: 0;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 600;
    white-space: nowrap;
    text-align: center;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chain-cell-bid { color: var(--algo-green, var(--c-long)); }
  .chain-cell-ask { color: var(--algo-red, var(--c-short)); }
  .chain-cell-sep { color: var(--algo-muted); opacity: 0.7; margin: 0 0.18rem; }
  /* flex-shrink: 0 — the +/- buttons + leg badge must never be the
     side that gives under a narrow squeeze; .chain-cell-quote (above)
     is the one permitted to shrink. See that rule's own comment. */
  .chain-side-action { display: inline-flex; align-items: center; flex-shrink: 0; }
  /* Audit fix — align ITM row tints to CE/PE palette. Pre-fix ITM
     calls were sky-blue (rgba 56,189,248) and ITM puts were orange
     (rgba 251,146,60), inverting the CE=green / PE=red convention
     used by the chain headers one row above. Now the ITM tint reads
     "the call/put on this row is in the money" in the same color
     family as the header. */
  /* Very-subtle pass (2026-09-29) — dropped below the -06 token tier
     (lowest pre-existing c-long/c-short alpha) to a bespoke ~3%
     wash; operator wants the ITM tint barely-there, not a design-
     system-standard "soft" fill.
     Per-side ITM/OTM split (2026-09-30, operator: "ITM and OTM calls
     can have different background... ITM and OTM puts can have
     different background... they can mirror calls in opposite
     direction... very subtle"). `dir` ('itm-call' when strike < spot,
     'itm-put' when strike > spot) already tells us unambiguously which
     SIDE is ITM at this strike — the other side is the OTM one. Each
     side keeps its own established color family (CE=green, matching
     .chain-th-ce's --c-long; PE=red, matching .chain-th-pe's
     --c-short) at every strike, but the ITM side of that family washes
     in stronger (0.05) than the OTM side (0.015) — so as you scan down
     the strike column, the CE cell's green fades while the PE cell's
     red strengthens (and vice versa above spot): a genuine mirror,
     not just "ITM has a tint, OTM has none". */
  .chain-row-itm-call .chain-td-ce { background: rgba(74,222,128,0.05); }
  .chain-row-itm-call .chain-td-pe { background: rgba(248,113,113,0.015); }
  .chain-row-itm-put  .chain-td-pe { background: rgba(248,113,113,0.05); }
  .chain-row-itm-put  .chain-td-ce { background: rgba(74,222,128,0.015); }
  /* Softened 2026-09-29 — operator: the top+bottom amber border read
     as an "overpowering underline" at 0.55 alpha. Same mechanism,
     gentler weight; still reads clearly as "this is the ATM row"
     without fighting the row's own content for attention. Softened
     AGAIN same day ("very very subtle" pass) 0.10->0.06 / 0.32->0.18. */
  .chain-row-atm > td {
    background: rgba(251,191,36,0.06);
    border-bottom: 1px solid rgba(251,191,36,0.18);
  }
  /* Sticky "active row" — the strike the operator last poked. Distinct
     violet accent so it never fights the amber ATM stripe (which
     persists when the active row happens to be ATM) or the cyan/orange
     ITM tint. Applied to either side of the row via -ce / -pe variants
     so the operator sees WHICH leg was last touched, not just which
     strike. Box-shadow inset rather than background so it stacks
     visually with the ATM background without erasing it. */
  .chain-row-active > td {
    box-shadow: inset 0 -1px 0 rgba(167,139,250,0.55);
    background-image: linear-gradient(
      to bottom,
      rgba(167,139,250,0.10),
      rgba(167,139,250,0.10)
    );
  }
  .chain-row-active.chain-row-atm > td {
    /* When the active row is also the ATM row, keep the amber stripe
       and overlay a softer violet tint so both signals remain visible. */
    background-image: linear-gradient(
      to bottom,
      rgba(167,139,250,0.14),
      rgba(167,139,250,0.14)
    );
  }
  .chain-spot-pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    font-family: monospace; font-size: var(--fs-md); font-weight: 700; letter-spacing: 0.05em;
    padding: 1px 6px; border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.55);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
  }
  /* Quiet-at-rest, full-strength-on-interaction — the strike grid
     should read as a clean quiet grid, not a wall of bordered
     buttons. Border drops to a soft 22%-alpha tint at rest (was
     solid currentColor); hover/focus restores the full solid
     border + a background fill so the buy/sell affordance is still
     unmistakable the moment the operator's attention is on it. */
  .chain-btn {
    font-family: monospace; font-size: var(--fs-xs); font-weight: 700;
    padding: 0 5px; border-radius: 2px;
    border: 1px solid transparent; background: transparent;
    cursor: pointer; letter-spacing: 0.04em;
    transition: background 0.12s, border-color 0.12s;
    line-height: 1.3;
  }
  .chain-btn-pair { display: inline-flex; gap: 10px; }
  /* Operator (2026-09-29): "+ and - are not looking like button" — the
     hover-reveal design (transparent at rest, filled on hover) read as
     plain text at rest. Visible fill at rest now, stronger on hover.
     Operator (2026-09-30): "+/- still don't look like buttons" — bumped
     rest-state one tier further: background -10 -> -14, and the border
     goes from a 22%-alpha tint to a SOLID full-color border so the
     button reads as clickable even before hover. Hover now bumps
     background to -22 (was -14) so it still reads visibly stronger
     than rest even though the border itself is now already solid at
     rest; :active (below) differentiates further via its inset ring +
     press-down scale. Rest < hover < active progression preserved via
     background tier + the active-only box-shadow/scale, not border
     alone (border is now solid at every state). */
  .chain-btn-buy  { color: var(--c-long);  background: var(--c-long-14);  border-color: var(--c-long); }
  .chain-btn-sell { color: var(--c-short); background: var(--c-short-14); border-color: var(--c-short); }
  .chain-btn-buy:hover,  .chain-btn-buy:focus-visible  { background: var(--c-long-22);  border-color: var(--c-long); }
  .chain-btn-sell:hover, .chain-btn-sell:focus-visible { background: var(--c-short-22); border-color: var(--c-short); }
  /* Operator (2026-09-29): "make + and - look like buttons. when
     pressed add border or some kind of highlight to show it is
     pressed" — hover already fills the button; :active goes a step
     further (stronger background + a solid ring + a slight press-down
     scale) so a tap/click reads as tactile feedback, not just another
     hover state. */
  .chain-btn-buy:active  {
    background: var(--c-long-22);
    border-color: var(--c-long);
    box-shadow: inset 0 0 0 1px var(--c-long);
    transform: scale(0.93);
  }
  .chain-btn-sell:active {
    background: var(--c-short-22);
    border-color: var(--c-short);
    box-shadow: inset 0 0 0 1px var(--c-short);
    transform: scale(0.93);
  }
  .chain-btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .chain-btn:disabled:hover { background: transparent; border-color: var(--c-long); }
  .chain-btn-sell:disabled:hover { border-color: var(--c-short); }
  .chain-cell-spread-warn { font-size: 0.55rem; color: var(--algo-amber, #fbbf24); margin-left: 0.12rem; cursor: default; vertical-align: super; }
  .chain-quick-toast {
    display: inline-block; padding: 2px 8px; border-radius: 2px;
    background: rgba(74,222,128,0.18); color: var(--c-long);
    font-family: monospace; font-size: var(--fs-sm); font-weight: 700;
    letter-spacing: 0.04em; margin-left: 0.3rem;
    animation: chain-quick-fade 0.9s ease-out forwards;
  }
  /* Persistent per-leg lot badge — unlike .chain-quick-toast (900ms
     flash), this stays as long as the strike's CE/PE leg is staged in
     the basket, so the operator can see at a glance which legs of a
     row are already added without re-clicking. Neutral slate at rest;
     gets an amber ring + dot when a non-None template is currently
     armed (`tmplAttached`), so "this leg will get TP/SL/Wing attached
     on fill" is visible right at the leg, not only in the shared
     Template Default/None row below the tab body. */
  .chain-leg-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    padding: 1px 5px;
    border-radius: 2px;
    margin-left: 0.3rem;
    font-family: monospace;
    font-size: var(--fs-xs, 0.65rem);
    font-weight: 700;
    letter-spacing: 0.03em;
    background: rgba(148, 163, 184, 0.14);
    border: 1px solid rgba(148, 163, 184, 0.35);
    color: var(--algo-slate);
    /* 2026-09-30 — badge now optionally carries a " · <template>"
       suffix (6-char truncated). nowrap + a max-width ellipsis
       backstop keeps it from wrapping/pushing the +/- buttons out of
       the 44%-width CE/PE column on a 375px viewport. */
    white-space: nowrap;
    max-width: 6.2rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chain-leg-badge-tmpl {
    background: rgba(251, 191, 36, 0.14);
    border-color: rgba(251, 191, 36, 0.45);
    color: var(--c-action);
  }
  .chain-leg-badge-tmpl::before {
    content: '';
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--c-action);
    flex-shrink: 0;
  }
  /* Template selector — small inline label + dropdown. Sized to sit
     next to Clear / Place without dominating the action row. */
  .chain-tpl-pick {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
  }
  .chain-tpl-pick-label {
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
  }
  /* Active-template chip — sits below the action row when a template
     other than 'none' is picked, so the operator can see the on-fill
     identity at a glance. Same family as OrderTicket's on-fill
     preview chip but lighter; no per-leg breakdown since the same
     template applies to every leg. */
  .chain-tpl-note {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-top: 0.35rem;
    padding: 0.2rem 0.5rem;
    background: rgba(125, 211, 252, 0.08);
    border: 1px solid rgba(125, 211, 252, 0.24);
    border-radius: 3px;
    font-family: monospace;
    font-size: var(--fs-xs);
    color: var(--algo-slate);
    width: 100%;
  }
  .chain-tpl-note-arrow {
    color: #7dd3fc;
    font-weight: 700;
  }
  .chain-tpl-note-label {
    /* A3 (2026-09 audit) — stale rgba(200,216,240,α); alpha preserved. */
    color: color-mix(in srgb, var(--algo-slate) 70%, transparent);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
  }
  .chain-tpl-note-name {
    color: #7dd3fc;
    font-weight: 700;
  }
  .chain-tpl-note-desc {
    /* A3 (2026-09 audit) — was rgba(200,216,240,0.55); same alpha as the
       .cell-muted token, so reused directly. */
    color: var(--algo-slate-muted);
  }
  .chain-basket-err { flex: 1 1 100%; color: var(--c-short); font-family: monospace; font-size: var(--fs-sm); margin-top: 0.2rem; }
  /* Pre-submit spread gate banner — amber (checking/wide, action color)
     escalating to red (error/timeout, same --c-short the basket error
     above uses) once the loop has exhausted its bounded retries. */
  .chain-spread-gate {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.3rem;
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    font-family: monospace;
    font-size: var(--fs-sm);
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.35);
    color: var(--algo-amber, var(--c-action));
  }
  .chain-spread-gate-error {
    background: rgba(248, 113, 113, 0.10);
    border-color: rgba(248, 113, 113, 0.40);
    color: var(--c-short);
  }
  .chain-spread-gate-msg { flex: 1 1 auto; font-variant-numeric: tabular-nums; }
  .chain-spread-gate-btn {
    flex-shrink: 0;
    padding: 0.15rem 0.5rem;
    background: rgba(148, 163, 184, 0.10);
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    cursor: pointer;
  }
  .chain-spread-gate-btn-primary {
    background: rgba(251, 191, 36, 0.20);
    border-color: rgba(251, 191, 36, 0.55);
    color: var(--algo-amber, var(--c-action));
  }
  @keyframes chain-quick-fade {
    0%   { opacity: 1; }
    70%  { opacity: 1; }
    100% { opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .chain-quick-toast { animation: none; }
  }

  /* Operator (2026-09-29): "on desktop, the row borders in chain making
     is cluttered" + "increase the text size in chain for desktop. On
     mobile, the chain text looks bigger". Placed HERE (after every base
     chain-grid, chain-row, chain-th and chain-cell rule above) so these
     same-specificity overrides actually win the cascade, unlike the
     earlier (now-removed) copy that sat before those base rules and lost.
     Row dividers: mobile's own report was the opposite ("mobile chain
     looks better"), so this drops dividers desktop-only, relying on the
     ATM highlight + ITM tint for row grouping instead of lines. Text
     size: matches the mobile-only sizes in the block just below instead
     of leaving desktop smaller at the --fs-md base. */
  @media (min-width: 640px) {
    .chain-row > td {
      border-bottom: none;
      /* Operator (2026-09-29): "on desktop the chain rows are very
         close. add gap between them. mobile chain looks better" —
         the base rule above zeroes vertical padding entirely (a
         density pass for the row-height-halving fix), and removing
         the border-bottom just above dropped the only remaining row
         separator, so desktop rows read as touching. Vertical padding
         restores breathing room without reintroducing the border
         lines mobile already reported looking worse with. */
      padding-top: 0.3rem;
      padding-bottom: 0.3rem;
    }
    /* Operator (2026-09-30): "reset chain font size to normal" — the
       0.78rem override here (and its matched th/quote/no-depth/
       spread-warn overrides, tuned as a set alongside it) read as
       oversized against the rest of the order-entry surface. Removed
       entirely so every element falls back to its own base rule
       (--fs-sm for th/quote, smaller hardcoded values for no-depth/
       spread-warn) — already a sane, consistent "normal" scale without
       inventing new overrides. */
  }
  /* Operator: "on mobile the chain strike rows too tense, leave space
     between the rows and make the text a little larger" — the earlier
     "reduce chain row height by half" pass (see .chain-row > td above)
     zeroed vertical padding for desktop density; on a touch screen that
     reads as cramped and hard to tell rows apart. Scoped to mobile only
     — desktop density is unchanged. */
  @media (max-width: 760px) {
    .chain-row > td {
      padding: 0.32rem 0.4rem;
      line-height: 1.4;
    }
    /* Operator (2026-09-30): font-size overrides removed here too —
       see the matching removal note in the desktop block above. */
  }
  /* Operator: "I don't see template elements in chain on mobile. Looks
     like they are hidden" / "reduce the height of chain area on mobile
     to show them". Root cause: .chain-grid-wrap's `flex: 1 1 0` (above)
     has nothing capping its growth, so on a short mobile viewport it
     expands to absorb the ENTIRE flex-parent chain up through
     .oes-body — a sibling scroll container of its own
     (SymbolPanel.svelte's .oes-body has `overflow-y: auto`). Two
     nested `overflow-y: auto` regions (.oes-body and .chain-grid-wrap)
     each absorb their own overflow internally, so shell-level content
     rendered AFTER .oes-body (the cap-warning/on-fill-preview strip,
     basket bar, common action footer) never gets pushed into view by
     outer-modal scroll — starved of any box height to begin with.
     Capping this wrapper's height on mobile guarantees .oes-body has
     leftover room to lay out that content below it.
     Operator (2026-09-30): "give overall background... mobile chain
     looks better" — `flex: 1 1 0` (the base rule above) greedily
     fills all available flex space up to this max-height cap
     regardless of actual row count, dominating the mobile layout even
     when only a handful of strikes are in view. `flex: 0 1 auto` sizes
     the wrapper to its own content instead, still capped at 16rem as
     a ceiling. --chain-depth-h resolves to `auto` on mobile
     (SymbolPanel.svelte's own `@media max-width:720px` override), so
     no competing min-height forces growth back past the content
     height here.
     REVERSED (2026-09-30) — operator: "empty space below chain
     strikes, chain is not fully using available space". `flex: 0 1
     auto` was overly conservative: it was chosen to stop the grid
     starving the Templ row, which at the time sat BELOW this wrapper
     as a flex sibling. The Templ toggle has since moved INTO the
     expiry toolbar row ABOVE the grid (2026-09-30 toggle redesign),
     so that sibling no longer exists to starve. With no grow at all,
     the wrapper settled at a small intrinsic content size (~179px on
     a real device) even when its flex parent had ~288px of genuinely
     free space and the cap was 256px — a visible dead gap below the
     grid. `flex: 1 1 auto` lets it grow to actually use that leftover
     space (still capped at 16rem, still shrinkable), without
     reintroducing the original starvation problem since there's
     nothing left below it to starve.
     ROOT-CAUSE FIX (2026-09-30, same day) — flipping this value alone
     never actually fixed the "empty space below chain" complaint; it
     only relocated the same gap between "inside the grid, below the
     last row" (flex:0) and "inside .oct-root, below the grid" (flex:1)
     because the REAL cause was the parent: SymbolPanel.svelte's
     `.oes-body :global(.oct-root)` was unconditionally forcing
     `flex: 1 1 0` (full-stretch) on mobile too, with no override next
     to its own `--chain-depth-h: auto` mobile exception. Now that
     `.oct-root` drops its forced full-stretch on mobile (see that
     rule's own comment), `flex: 1 1 auto` here lets the grid genuinely
     grow into leftover space without an outer container adding extra
     dead space beyond what the grid needs. Future passes: check the
     PARENT (.oct-root) before re-flipping this value again.
     max-height: 16rem REMOVED (2026-09-30 audit) — this cap was a
     leftover from the very first pass above ("Capping this wrapper's
     height on mobile guarantees .oes-body has leftover room to lay out
     that content below it" — the Templ row, at the time a flex sibling
     BELOW this wrapper). The Templ toggle moved INTO the expiry toolbar
     row ABOVE the grid later the same day (see the comment two
     paragraphs up), so that sibling no longer exists to starve, and the
     cap had nothing left to protect — it just silently wasted ~200px of
     real strike-grid space on a typical phone (measured via headless
     browser: 412×919 → wrap height capped at 256px with clientHeight
     254px vs scrollHeight ~2779px for a real 100+-row NIFTY chain,
     i.e. the operator saw only ~5 strike rows and had to scroll inside
     a tiny sub-box). `.oct-root`'s own mobile override (`flex: 0 1
     auto`, SymbolPanel.svelte) still sizes the grid's parent to content,
     and `.oes-body`'s fixed-height flex-shrink math (min-height: 0,
     between a fixed header and a fixed footer row) is what actually
     protects the basket-bar/submit footer below — NOT this cap — so
     removing it does not resurrect the original starvation bug. No
     flex-value tuning here or on `.oct-root` can grow a box past its
     own max-height; that's why three same-day attempts to fix this via
     flex values alone never worked. `flex: 1 1 auto` is kept — still
     correct (confirmed by the same measurement) once the ceiling is
     gone. Breakpoint changed 760px -> 720px to match `--chain-depth-h`
     / `.oct-root`'s own mobile breakpoint in SymbolPanel.svelte exactly
     — between 721-760px the mismatch previously meant `min-height: 22rem`
     (desktop `--chain-depth-h`, not yet dropped to `auto`) combined with
     no max-height would force the grid to stretch its parent. */
  @media (max-width: 720px) {
    .chain-grid-wrap {
      flex: 1 1 auto;
    }
  }
</style>
