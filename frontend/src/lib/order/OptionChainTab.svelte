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
    fetchOptionsSpot, fetchChainQuotes,
    placeTicketOrder,
    fetchAccounts,
  } from '$lib/api';
  import { executionMode } from '$lib/stores';
  import Select from '$lib/Select.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listStrikes, findOption,
    listFutures, getInstrument,
  } from '$lib/data/instruments';
  import { POPULAR_UNDERLYINGS } from '$lib/data/popularUnderlyings';
  import { KITE_INDEX_QUOTE_KEY_TO_ROOT as _KITE_IDX_TO_ROOT } from '$lib/data/resolveUnderlying.js';
  import { priceFmt } from '$lib/format';
  // Order-template catalog — same source the OrderTicket uses.
  // Operator: "template should be applicable to option chain too".
  // Templates apply per-leg to each basket entry: when the leg fills,
  // the template runs (TP / SL / Wing) just like a single-leg ticket.
  import {
    loadOrderTemplates, orderTemplatesStore,
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
  } = $props();

  // "Place" mode toggle — default OFF (Basket mode). Off: +/− stage
  // legs into the shared basket. On: +/− open the Ticket tab pre-
  // filled with that leg for direct submit, same as Command's flow.
  let _placeMode = $state(false);

  // Whether basket state is lifted to the shell or owned locally.
  const _externalBasket = $derived(basketLegs !== undefined && !!onAddLeg);

  // ── Instruments cache ─────────────────────────────────────────────
  let instrumentsReady = $state(false);

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
    if (_account) return;
    if (_isRealAcct(account)) { _account = account; return; }
    if (_allAccounts.length === 1) _account = _allAccounts[0];
  });
  // Sync from a shell-pushed prop change (operator switched account
  // in another tab; shell re-renders us with the new value).
  $effect(() => {
    if (account && _isRealAcct(account) && account !== _account) {
      _account = account;
    }
  });
  // ── Template state ───────────────────────────────────────────
  // Templates apply per leg of the basket: when the leg fills, the
  // backend's apply_template_to_order pipeline runs to attach TP /
  // SL / Wing GTTs. Same shape as the OrderTicket template flow —
  // single template selection per basket; threaded into every leg
  // on placeBasket(). Operator picks once, the whole basket
  // inherits.
  let _templates = $state(/** @type {any[]} */ ([]));
  const _selectedTemplate = $derived(
    _templates.find(t => t.id === templateId) || null
  );
  // Subscribe to the shared template store — keeps the picker
  // current when the operator edits a template on
  // /automation/templates while the chain tab is mounted. First-paint
  // 'none' fallback is now handled by SymbolPanel so the default is
  // shared across the Ticket / Chain / Basket-bar surfaces.
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows && rows.length) {
      _templates = rows.filter(t => t.is_active);
      if (templateId === null) {
        // Operator: "instead of None, going forward use the default
        // valid template for buy or sell". Standalone OptionChainTab
        // has no _side prop in scope, so we pick the first is_default
        // (any applies_to) — the shell's side-aware effect overrides
        // this whenever it is mounted, so this fallback only matters
        // for the rare standalone mount.
        const def = _templates.find(t => t.is_default);
        if (def) { templateId = def.id; return; }
        const none = _templates.find(t => t.slug === 'none');
        if (none) templateId = none.id;
      }
    }
  });

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
    if (!instrumentsReady) return [];
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
  const chainStrikes = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying || !chainExpiry) return [];
    return listStrikes(chainUnderlying.toUpperCase(), 'CE', chainExpiry);
  });
  const chainFutures = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
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
    if (chainExpiries.length && !chainExpiries.includes(chainExpiry)) {
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

  // ── Chain quotes (bid/ask per strike) ─────────────────────────────
  /** @type {Record<string,{ce:{bid:number|null,ask:number|null},pe:{bid:number|null,ask:number|null}}>|null} */
  let chainQuotesMap = $state(null);
  let chainQuotesKey = '';
  let chainQuotesPoll = /** @type {any} */ (null);
  function _refreshChainQuotes() {
    if (!chainUnderlying || !chainExpiry || !isMarketOpen()) return;
    const u = chainUnderlying.toUpperCase(); const e = chainExpiry;
    fetchChainQuotes(u, e).then((r) => {
      if (chainQuotesKey !== `${u}|${e}`) return;
      /** @type {Record<string,{ce:{bid:number|null,ask:number|null},pe:{bid:number|null,ask:number|null}}>} */
      const map = {};
      for (const row of (r?.rows || [])) {
        map[String(row.k)] = {
          ce: { bid: row.ce_bid == null ? null : Number(row.ce_bid), ask: row.ce_ask == null ? null : Number(row.ce_ask) },
          pe: { bid: row.pe_bid == null ? null : Number(row.pe_bid), ask: row.pe_ask == null ? null : Number(row.pe_ask) },
        };
      }
      chainQuotesMap = map;
    }).catch(() => {});
  }
  $effect(() => {
    void chainUnderlying; void chainExpiry;
    untrack(() => {
      if (chainQuotesPoll) { chainQuotesPoll(); chainQuotesPoll = null; }
      if (!chainUnderlying || !chainExpiry) { chainQuotesMap = null; chainQuotesKey = ''; return; }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry}`;
      if (key !== chainQuotesKey) { chainQuotesMap = null; chainQuotesKey = key; }
      _refreshChainQuotes();
      chainQuotesPoll = visibleInterval(_refreshChainQuotes, 5000);
    });
  });
  onDestroy(() => { if (chainQuotesPoll) { chainQuotesPoll(); chainQuotesPoll = null; } });

  // Host-triggered refresh — invalidate the spot + quotes cache keys
  // and re-fire the fetchers so a tab activation always lands on fresh
  // chain data (operator request: "when chain tab is pressed, the
  // chain details need to be refreshed").
  $effect(() => {
    if (refreshKey <= 0) return;
    untrack(() => {
      // Clear keys so the next spot effect treats the underlying as
      // newly-set and re-fetches.
      chainSpotKey = '';
      chainQuotesKey = '';
      if (chainUnderlying) {
        // Spot re-fetch — bypass the key-equality short-circuit.
        const u = chainUnderlying; const e = chainExpiry || null;
        fetchOptionsSpot(u, e).then((r) => {
          chainSpotFetched = r ? { spot: Number(r.spot) || 0, source: String(r.spot_source || '') } : null;
          chainSpotKey = `${u.toUpperCase()}|${e || ''}`;
        }).catch(() => { chainSpotFetched = null; });
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
    // Audit fix — eliminate the _account='' race. The $effect at line
    // 166 auto-picks the single account but only after the first paint;
    // a fast click on +CE in that window used to silently no-op (basket
    // never appears, operator confused). Re-derive synchronously here:
    // single account → pick it; multiple → ask the operator to choose.
    if (!_account) {
      if (_allAccounts.length === 1) { _account = _allAccounts[0]; }
      else if (_allAccounts.length === 0) { basketError = 'No broker accounts loaded — wait or sign in.'; return; }
      else { basketError = 'Pick a routable account before adding legs.'; return; }
    }
    // Pin the active row visual marker — survives the 900 ms toast.
    _markActive(strike, optType);
    // Place-mode short-circuit: route directly to the Ticket tab
    // pre-filled with this leg, bypassing the basket entirely.
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
    });
    basketError = ''; _flashToast(_quickKeyOpt(strike, optType), '✓ added');
  }

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

  function setBasketChaseAgg(/** @type {string} */ key, /** @type {'low'|'med'|'high'} */ agg) {
    if (_externalBasket && onUpdateLeg) {
      onUpdateLeg(key, (leg) => ({ ...leg, chaseAgg: agg }));
    } else if (_externalBasket && onRemoveLeg && onAddLeg) {
      const leg = chainBasket.find(b => b.key === key);
      if (leg) { onRemoveLeg(leg); onAddLeg({ ...leg, chaseAgg: agg }); }
    } else {
      _localBasket = _localBasket.map(b => b.key === key ? { ...b, chaseAgg: agg } : b);
    }
  }
  function basketStepLots(/** @type {string} */ key, /** @type {number} */ delta) {
    if (_externalBasket && onUpdateLeg) {
      onUpdateLeg(key, (leg) => ({
        ...leg,
        lots: Math.max(1, Math.floor((leg.lots || 1) + delta)),
      }));
    } else if (_externalBasket && onRemoveLeg && onAddLeg) {
      const leg = chainBasket.find(b => b.key === key);
      if (leg) { onRemoveLeg(leg); onAddLeg({ ...leg, lots: Math.max(1, Math.floor((leg.lots || 1) + delta)) }); }
    } else {
      _localBasket = _localBasket.map(b => b.key !== key ? b : { ...b, lots: Math.max(1, Math.floor((b.lots || 1) + delta)) });
    }
  }
  function removeFromBasket(/** @type {string} */ key) {
    if (_externalBasket && onRemoveLeg) {
      const leg = chainBasket.find(b => b.key === key);
      if (leg) onRemoveLeg(leg);
    } else {
      _localBasket = _localBasket.filter(b => b.key !== key);
    }
  }
  function clearBasket() {
    if (_externalBasket && onClearBasket) { onClearBasket(); }
    else { _localBasket = []; }
    basketError = '';
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
    } else if (failures.length) {
      basketError = `${failures.length}/${total} failed: ${failures[0]}`;
      _localBasket = [];
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

  onMount(async () => {
    await loadInstruments();
    instrumentsReady = true;
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
  });
</script>

<div class="oct-root">
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
          {_dte === 0 ? 'expires today' : `${_dte}d to expiry`}
        </span>
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
            {@const activeCe = activeOptionRow?.strike === k && activeOptionRow?.optType === 'CE'}
            {@const activePe = activeOptionRow?.strike === k && activeOptionRow?.optType === 'PE'}
            {@const activeRow = activeCe || activePe}
            {#if isAtm}
              <tr class="chain-row chain-row-{dir} chain-row-atm" class:chain-row-active={activeRow} class:chain-row-active-ce={activeCe} class:chain-row-active-pe={activePe} use:chainAtmRow>
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.ask)}</span>
                    </span>
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                title="BUY {k} CE"
                                onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                title="SELL {k} CE"
                                onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                      </span>
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
                                title="BUY {k} PE"
                                onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                title="SELL {k} PE"
                                onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                      </span>
                      {#if quickToast?.key === peKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.ask)}</span>
                    </span>
                  </span>
                </td>
              </tr>
            {:else}
              <tr class="chain-row chain-row-{dir}" class:chain-row-active={activeRow} class:chain-row-active-ce={activeCe} class:chain-row-active-pe={activePe}>
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.ask)}</span>
                    </span>
                    <span class="chain-side-action">
                      <span class="chain-btn-pair">
                        <button type="button" class="chain-btn chain-btn-buy"
                                title="BUY {k} CE"
                                onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                title="SELL {k} CE"
                                onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                      </span>
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
                                title="BUY {k} PE"
                                onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                        <button type="button" class="chain-btn chain-btn-sell"
                                title="SELL {k} PE"
                                onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                      </span>
                      {#if quickToast?.key === peKey}
                        <span class="chain-quick-toast">{quickToast.msg}</span>
                      {/if}
                    </span>
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.ask)}</span>
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

  <!-- Basket bar — render the in-tab pills ONLY when the basket isn't
       lifted to the shell. With _externalBasket=true the OrderEntryShell
       already renders a richer pill row in its sticky bottom strip
       that's visible from every tab; showing the same pills here would
       just be a duplicate of that. -->
  {#if chainBasket.length && !_externalBasket}
    <div class="chain-basket">
      <div class="chain-basket-legs">
        {#each chainBasket as leg (leg.key)}
          <span class="chain-basket-leg chain-basket-leg-{leg.side === 'BUY' ? 'buy' : 'sell'} chain-basket-leg-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                class:is-disabled={basketPlacing}
                role="button" tabindex="0"
                title="Click to remove from basket"
                onclick={() => { if (!basketPlacing) removeFromBasket(leg.key); }}
                onkeydown={(e) => {
                  if (basketPlacing) return;
                  if (e.key === 'Enter' || e.key === ' ' || e.key === 'Delete' || e.key === 'Backspace') {
                    e.preventDefault(); removeFromBasket(leg.key);
                  }
                }}>
            <span class="chain-basket-side">{leg.side === 'BUY' ? 'B' : 'S'}</span>
            <span class="chain-basket-sym"><LegLabel sym={leg.sym} compact={true} /></span>
            <button type="button" class="chain-basket-step" title="Decrease lots"
                    disabled={basketPlacing || leg.lots <= 1}
                    onclick={(e) => { e.stopPropagation(); basketStepLots(leg.key, -1); }}>−</button>
            <span class="chain-basket-lots">{leg.lots}</span>
            <button type="button" class="chain-basket-step" title="Increase lots"
                    disabled={basketPlacing}
                    onclick={(e) => { e.stopPropagation(); basketStepLots(leg.key, +1); }}>+</button>
            <span class="chain-basket-qty">× {leg.lotSize} = {leg.lots * leg.lotSize}</span>
            <span class="chain-basket-limit-static"
                  title={leg.limit > 0 ? `Limit ₹${priceFmt(leg.limit)} from chain bid/ask.` : 'No quote — routes as MARKET.'}>
              {#if leg.limit > 0}algo @{priceFmt(leg.limit)}{:else}@MKT{/if}
            </span>
            <span class="chain-basket-chase">
              <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-low"
                      class:on={(leg.chaseAgg || 'low') === 'low'} disabled={basketPlacing}
                      title="Low — patient" onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'low'); }}>L</button>
              <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-med"
                      class:on={leg.chaseAgg === 'med'} disabled={basketPlacing}
                      title="Medium — midpoint" onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'med'); }}>M</button>
              <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-high"
                      class:on={leg.chaseAgg === 'high'} disabled={basketPlacing}
                      title="High — urgent" onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'high'); }}>H</button>
            </span>
          </span>
        {/each}
      </div>
      <div class="chain-basket-actions">
        <!-- Template picker was previously rendered here but the
             reachable shell (SymbolPanel) lifts the basket to
             `_externalBasket=true` so this block never paints in
             practice. The picker now lives in the shell's
             `.oes-basket-bar` (SymbolPanel.svelte) so it appears for
             every chain-modal mount. -->
        <button type="button" class="chain-basket-clear" disabled={basketPlacing} onclick={clearBasket}>Clear</button>
        <button type="button" class="chain-basket-place" disabled={basketPlacing} onclick={placeBasket}>
          {#if basketPlacing}Placing… ({basketProgress}/{chainBasket.length}){:else}Place {chainBasket.length} leg{chainBasket.length === 1 ? '' : 's'}{/if}
        </button>
      </div>
    </div>
  {:else if basketJustDone}
    <div class="chain-basket-toast">✓ basket placed</div>
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
     operator picks the contract month BEFORE scanning strikes. */
  .oct-toolbar {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.1rem 0.35rem;
    margin-bottom: 0.25rem;
    border-bottom: 1px dashed rgba(251, 191, 36, 0.10);
    flex-wrap: wrap;
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
  .oct-expiry-pick { min-width: 11rem; max-width: 16rem; flex: 0 1 auto; }
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
  }
  .oct-expiry-dte-warn {
    color: var(--c-action);
    background: var(--algo-amber-bg);
    border-color: rgba(251, 191, 36, 0.42);
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
    overflow-y: auto;
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
  .chain-grid {
    width: 100%;
    border-collapse: collapse;
    font-family: monospace;
    font-size: var(--fs-md);
  }
  .chain-col-ce     { width: 42%; }
  .chain-col-strike { width: 16%; }
  .chain-col-pe     { width: 42%; }
  .chain-th-ce      { text-align: left;   color: var(--c-long); padding: 0.2rem 0.5rem; font-weight: 700; font-size: var(--fs-sm); border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-th-pe      { text-align: right;  color: var(--c-short); padding: 0.2rem 0.5rem; font-weight: 700; font-size: var(--fs-sm); border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-th-strike  { text-align: center; color: var(--algo-slate); padding: 0.2rem 0.3rem; font-weight: 700; font-size: var(--fs-sm); border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-row > td {
    /* Operator: "reduce the height of chain grid for strike prices
       by half". Vertical padding zeroed (was 0.1rem), button
       padding + font compressed below — each strike row drops
       from ~18px to ~9px so the whole grid roughly halves in
       height. */
    padding: 0 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.04);
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
  .chain-td-strike  { text-align: center; color: var(--algo-slate); font-weight: 700; font-variant-numeric: tabular-nums; }
  .chain-td-strike-atm { color: var(--c-action); font-weight: 800; letter-spacing: 0.04em; }
  .chain-cell-quote {
    display: inline-flex;
    align-items: baseline;
    min-width: 3.4rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 600;
    white-space: nowrap;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
  .chain-cell-bid { color: var(--algo-green, var(--c-long)); }
  .chain-cell-ask { color: var(--algo-red, var(--c-short)); }
  .chain-cell-sep { color: var(--algo-muted); opacity: 0.7; margin: 0 0.18rem; }
  .chain-side-action { display: inline-flex; align-items: center; }
  /* Audit fix — align ITM row tints to CE/PE palette. Pre-fix ITM
     calls were sky-blue (rgba 56,189,248) and ITM puts were orange
     (rgba 251,146,60), inverting the CE=green / PE=red convention
     used by the chain headers one row above. Now the ITM tint reads
     "the call/put on this row is in the money" in the same color
     family as the header. */
  .chain-row-itm-call > td { background: var(--c-long-06); }
  .chain-row-itm-put  > td { background: var(--c-short-06); }
  .chain-row-atm > td {
    background: rgba(251,191,36,0.18);
    border-top:    1px solid rgba(251,191,36,0.55);
    border-bottom: 1px solid rgba(251,191,36,0.55);
  }
  /* Sticky "active row" — the strike the operator last poked. Distinct
     violet accent so it never fights the amber ATM stripe (which
     persists when the active row happens to be ATM) or the cyan/orange
     ITM tint. Applied to either side of the row via -ce / -pe variants
     so the operator sees WHICH leg was last touched, not just which
     strike. Box-shadow inset rather than background so it stacks
     visually with the ATM background without erasing it. */
  .chain-row-active > td {
    box-shadow: inset 0 0 0 1px rgba(167,139,250,0.55);
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
  /* Per-side emphasis — left edge for CE, right edge for PE — to
     show the operator which option leg they last touched on the row. */
  .chain-row-active-ce > .chain-td-ce {
    border-left: 2px solid #a78bfa;
  }
  .chain-row-active-pe > .chain-td-pe {
    border-right: 2px solid #a78bfa;
  }
  .chain-spot-pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    font-family: monospace; font-size: var(--fs-md); font-weight: 700; letter-spacing: 0.05em;
    padding: 1px 6px; border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.55);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
  }
  .chain-btn {
    font-family: monospace; font-size: var(--fs-xs); font-weight: 700;
    padding: 0 5px; border-radius: 2px;
    border: 1px solid currentColor; background: transparent;
    cursor: pointer; letter-spacing: 0.04em; transition: background 0.12s;
    line-height: 1.3;
  }
  .chain-btn-pair { display: inline-flex; gap: 3px; }
  .chain-btn-buy  { color: var(--c-long); }
  .chain-btn-sell { color: var(--c-short); }
  .chain-btn-buy:hover  { background: var(--c-long-10); }
  .chain-btn-sell:hover { background: var(--c-short-10); }
  .chain-quick-toast {
    display: inline-block; padding: 2px 8px; border-radius: 2px;
    background: rgba(74,222,128,0.18); color: var(--c-long);
    font-family: monospace; font-size: var(--fs-sm); font-weight: 700;
    letter-spacing: 0.04em; margin-left: 0.3rem;
    animation: chain-quick-fade 0.9s ease-out forwards;
  }
  /* Basket styles (exact mirror of admin/options) */
  .chain-basket {
    margin-top: 0.6rem; padding: 0.45rem 0.55rem;
    border: 1px solid rgba(251,191,36,0.32); border-radius: 3px;
    background: rgba(251,191,36,0.06);
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem 0.6rem;
  }
  .chain-basket-legs { display: flex; flex-wrap: wrap; gap: 0.35rem; flex: 1 1 60%; min-width: 0; }
  .chain-basket-leg {
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 1px 6px 1px 4px; border-radius: 3px;
    border: 1px solid currentColor; border-left-width: 4px;
    font-family: monospace; font-size: var(--fs-sm); line-height: 1.5;
    cursor: pointer; user-select: none; transition: background 0.12s, transform 0.05s;
  }
  .chain-basket-leg:hover:not(.is-disabled) { background: var(--c-short-10); transform: translateY(-1px); }
  .chain-basket-leg.is-disabled { cursor: progress; opacity: 0.55; }
  .chain-basket-leg-buy  { color: var(--c-long); background: var(--c-long-06); }
  .chain-basket-leg-sell { color: var(--c-short); background: var(--c-short-06); }
  .chain-basket-leg-type-ce  { border-left-color: var(--c-long); }
  .chain-basket-leg-type-pe  { border-left-color: var(--c-short); }
  .chain-basket-leg-type-fut { border-left-color: #7dd3fc; }
  .chain-basket-leg-type-eq  { border-left-color: var(--c-action); }
  .chain-basket-side { font-weight: 800; letter-spacing: 0.04em; }
  .chain-basket-sym { color: var(--algo-slate); font-weight: 600; }
  .chain-basket-qty { color: var(--text-muted); font-size: var(--fs-xs); opacity: 0.85; font-variant-numeric: tabular-nums; }
  .chain-basket-step {
    width: 1.05rem; height: 1.05rem; padding: 0; border-radius: 2px;
    border: 1px solid currentColor; background: transparent; color: currentColor;
    cursor: pointer; font-family: monospace; font-size: var(--fs-lg); font-weight: 700;
    line-height: 1; display: inline-flex; align-items: center; justify-content: center;
  }
  .chain-basket-step:hover:not(:disabled) { background: rgba(126,151,184,0.10); }
  .chain-basket-step:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-lots { min-width: 1.1rem; text-align: center; color: var(--c-action); font-family: monospace; font-weight: 700; font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }
  .chain-basket-limit-static { color: var(--c-action); font-family: monospace; font-size: var(--fs-sm); font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: 0.02em; }
  .chain-basket-chase { display: inline-flex; align-items: center; gap: 0.15rem; margin-left: 0.15rem; }
  .chain-basket-chase-pill {
    width: 1rem; height: 1rem; padding: 0;
    border: 1px solid rgba(126,151,184,0.35); border-radius: 2px;
    background: transparent; color: var(--text-muted);
    font-family: monospace; font-size: var(--fs-xs); font-weight: 700; line-height: 1;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  }
  .chain-basket-chase-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  /* Audit fix — HIGH is RED (danger / urgent), not green. Pre-fix
     OrderTicket painted HIGH red and OptionChainTab painted it
     green; same semantic control conveyed opposite meanings on tab
     switch. Industry convention (NinjaTrader / Zerodha) is amber → red
     ascending urgency. Aligned to LOW=sky, MED=amber, HIGH=red on
     both surfaces. */
  .chain-basket-chase-pill-low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; border-color: rgba(125,211,252,0.55); }
  .chain-basket-chase-pill-med.on  { background: rgba(251,191,36,0.20);  color: var(--c-action); border-color: rgba(251,191,36,0.55); }
  .chain-basket-chase-pill-high.on { background: rgba(248,113,113,0.20); color: var(--c-short); border-color: rgba(248,113,113,0.55); }
  .chain-basket-actions { display: inline-flex; align-items: center; gap: 0.4rem; margin-left: auto; flex-wrap: wrap; }
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
    color: #c8d8f0;
    width: 100%;
  }
  .chain-tpl-note-arrow {
    color: #7dd3fc;
    font-weight: 700;
  }
  .chain-tpl-note-label {
    color: rgba(200, 216, 240, 0.7);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
  }
  .chain-tpl-note-name {
    color: #7dd3fc;
    font-weight: 700;
  }
  .chain-tpl-note-desc {
    color: rgba(200, 216, 240, 0.55);
  }
  .chain-basket-clear,
  .chain-basket-place {
    height: 1.5rem; padding: 0 0.7rem; border-radius: 2px;
    border: 1px solid currentColor; background: transparent;
    cursor: pointer; font-family: monospace; font-size: var(--fs-sm); font-weight: 700; letter-spacing: 0.04em;
  }
  .chain-basket-clear { color: var(--text-muted); }
  .chain-basket-clear:hover { background: rgba(163,185,208,0.08); }
  .chain-basket-place { color: var(--c-action); background: rgba(251,191,36,0.10); }
  .chain-basket-place:hover { background: rgba(251,191,36,0.20); }
  .chain-basket-place:disabled,
  .chain-basket-clear:disabled { opacity: 0.55; cursor: progress; }
  .chain-basket-err { flex: 1 1 100%; color: var(--c-short); font-family: monospace; font-size: var(--fs-sm); margin-top: 0.2rem; }
  .chain-basket-toast {
    margin-top: 0.5rem; padding: 0.3rem 0.5rem; border-radius: 2px;
    background: rgba(74,222,128,0.14); color: var(--c-long);
    font-family: monospace; font-size: var(--fs-md); font-weight: 700; text-align: center;
    animation: chain-quick-fade 2.2s ease-out forwards;
  }
  @keyframes chain-quick-fade {
    0%   { opacity: 1; }
    70%  { opacity: 1; }
    100% { opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .chain-quick-toast { animation: none; }
    .chain-basket-toast { animation: none; }
  }
</style>
