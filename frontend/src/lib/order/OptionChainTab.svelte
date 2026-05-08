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
  import { isMarketOpen } from '$lib/marketHours';
  import {
    fetchOptionsSpot, fetchChainQuotes,
    placeTicketOrder, fetchLiveStatus,
    fetchAccounts,
  } from '$lib/api';
  import Select      from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listStrikes, findOption,
    listFutures, getInstrument,
  } from '$lib/data/instruments';
  import { priceFmt } from '$lib/format';

  /** @type {{
   *   symbol?:         string,
   *   account?:        string,
   *   accounts?:       string[],
   *   onBasketPlace?:  (result: {ok: number, fail: number}) => void,
   *   basketLegs?:     any[],
   *   onAddLeg?:       (leg: any) => void,
   *   onRemoveLeg?:    (leg: any) => void,
   *   onSubmitBasket?: () => void,
   *   onClearBasket?:  () => void,
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
    onSubmitBasket = /** @type {(() => void)|undefined} */ (undefined),
    onClearBasket  = /** @type {(() => void)|undefined} */ (undefined),
  } = $props();

  // Whether basket state is lifted to the shell or owned locally.
  const _externalBasket = $derived(basketLegs !== undefined && !!onAddLeg);

  // ── Instruments cache ─────────────────────────────────────────────
  let instrumentsReady = $state(false);

  const _COMMON_INDICES_AND_COMMODITIES = [
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
    'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL',
    'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
    'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
    'ALUMINIUM', 'ALUMINI', 'NICKEL',
    'MENTHAOIL', 'COTTON',
  ];

  // Derive the seed underlying from the symbol prop (strip trailing
  // digits + CE/PE/FUT suffix). E.g. NIFTY25APR22000CE → NIFTY.
  const seedUnderlying = $derived.by(() => {
    if (!symbol) return '';
    return String(symbol).toUpperCase().replace(/\d.*$/, '') || '';
  });

  // ── Chain picker state ────────────────────────────────────────────
  let chainUnderlying  = $state('');
  let chainExpiry      = $state('');
  /** @type {Array<'opt'|'fut'>} */
  let chainKinds       = $state(/** @type {Array<'opt'|'fut'>} */ (['opt']));

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
  let _account = $state(_isRealAcct(account) ? account : '');
  $effect(() => {
    if (_account) return;
    if (_isRealAcct(account)) { _account = account; return; }
    if (_allAccounts.length === 1) _account = _allAccounts[0];
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
    for (const u of suggestUnderlyings('', 1000)) push(u);
    return out;
  });

  const chainExpiries = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
    return listExpiries(chainUnderlying.toUpperCase(), 'CE');
  });
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

  // Auto-default underlying + expiry once instruments are ready.
  $effect(() => {
    const list = underlyingChoices;
    untrack(() => {
      if (!list.length) { if (chainUnderlying) chainUnderlying = ''; return; }
      if (!chainUnderlying || !list.includes(chainUnderlying)) chainUnderlying = list[0];
    });
  });
  $effect(() => {
    void chainUnderlying;
    if (chainExpiries.length && !chainExpiries.includes(chainExpiry)) {
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
      if (chainQuotesPoll) { clearInterval(chainQuotesPoll); chainQuotesPoll = null; }
      if (!chainUnderlying || !chainExpiry) { chainQuotesMap = null; chainQuotesKey = ''; return; }
      const key = `${chainUnderlying.toUpperCase()}|${chainExpiry}`;
      if (key !== chainQuotesKey) { chainQuotesMap = null; chainQuotesKey = key; }
      _refreshChainQuotes();
      chainQuotesPoll = setInterval(_refreshChainQuotes, 5000);
    });
  });
  onDestroy(() => { if (chainQuotesPoll) clearInterval(chainQuotesPoll); });

  function _fmtLtp(/** @type {number|null|undefined} */ v) {
    if (v == null || !Number.isFinite(v)) return '—';
    return v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  }

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

  function _quickKeyOpt(/** @type {number} */ strike, /** @type {string} */ optType) { return `o:${strike}:${optType}`; }
  function _quickKeyFut(/** @type {string} */ sym) { return `f:${sym}`; }

  function _flashToast(/** @type {string} */ key, /** @type {string} */ msg) {
    quickToast = { key, msg };
    setTimeout(() => { if (quickToast?.key === key) quickToast = null; }, 900);
  }

  function _mergeIntoBasket(/** @type {{sym:string,side:'BUY'|'SELL',lots:number}} */ incoming) {
    const idx = chainBasket.findIndex(b => b.sym === incoming.sym && b.side === incoming.side);
    if (idx < 0) return false;
    // External basket: can't mutate in place — call the shell remove+add cycle.
    if (_externalBasket && onRemoveLeg && onAddLeg) {
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
    if (!_account) { basketError = 'Pick a routable account before adding legs.'; return; }
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
    if (!_account) { basketError = 'Pick a routable account before adding legs.'; return; }
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
    if (_externalBasket) {
      // External: remove + re-add with updated agg.
      const leg = chainBasket.find(b => b.key === key);
      if (leg && onRemoveLeg && onAddLeg) { onRemoveLeg(leg); onAddLeg({ ...leg, chaseAgg: agg }); }
    } else {
      _localBasket = _localBasket.map(b => b.key === key ? { ...b, chaseAgg: agg } : b);
    }
  }
  function basketStepLots(/** @type {string} */ key, /** @type {number} */ delta) {
    if (_externalBasket) {
      const leg = chainBasket.find(b => b.key === key);
      if (leg && onRemoveLeg && onAddLeg) { onRemoveLeg(leg); onAddLeg({ ...leg, lots: Math.max(1, Math.floor((leg.lots || 1) + delta)) }); }
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

  async function placeBasket() {
    // When the basket is lifted to the shell, delegate entirely.
    if (_externalBasket && onSubmitBasket) {
      onSubmitBasket();
      return;
    }
    if (basketPlacing || !chainBasket.length) return;
    const acct = _account;
    if (!acct) { basketError = 'No routable account. Pick an account above.'; return; }

    let basketMode = 'paper';
    try {
      const live = await fetchLiveStatus();
      if (live && live.paper_trading_mode === false && live.branch === 'main') basketMode = 'live';
    } catch { /* safe default paper */ }

    basketPlacing = true; basketError = ''; basketProgress = 0;
    /** @type {string[]} */ const failures = [];
    for (const leg of chainBasket) {
      try {
        const hasLimit = Number(leg.limit) > 0;
        await placeTicketOrder({
          mode: basketMode, side: leg.side, tradingsymbol: leg.sym,
          quantity: leg.lots * leg.lotSize, exchange: leg.exchange,
          product: leg.product || 'NRML',
          order_type: hasLimit ? 'LIMIT' : 'MARKET',
          price: hasLimit ? Number(leg.limit) : 0,
          variety: 'regular', account: leg.account || acct,
          chase: hasLimit, chase_aggressiveness: hasLimit ? (leg.chaseAgg || 'low') : 'low',
        });
      } catch (e) {
        failures.push(`${leg.side} ${leg.sym}: ${String(/** @type {any} */ (e)?.message || e || 'failed')}`);
      }
      basketProgress += 1;
    }
    const total = chainBasket.length;
    basketPlacing = false;
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
  });
</script>

<div class="oct-root">
  <!-- Account selector (required for basket submit) -->
  <div class="oct-account-row">
    <label class="oct-label" for="oct-acct">Account</label>
    {#if _allAccounts.length === 0}
      <span class="oct-acct-hint">No routable account — sign in or pick one from the main picker.</span>
    {:else if _allAccounts.length === 1}
      <span class="oct-acct-single">{_allAccounts[0]}</span>
    {:else}
      <select id="oct-acct" class="oct-acct-select" bind:value={_account}>
        <option value="" disabled>Pick account…</option>
        {#each _allAccounts as a}
          <option value={a}>{a}</option>
        {/each}
      </select>
    {/if}
  </div>

  <!-- Underlying / Expiry / Kind controls -->
  <div class="oct-controls">
    <div class="oct-field">
      <label class="oct-label" for="oct-und">Underlying</label>
      <Select id="oct-und"
        bind:value={chainUnderlying}
        searchable={true}
        searchPlaceholder="Type 3+ chars to filter…"
        options={underlyingChoices.map(u => ({ value: u, label: u }))} />
    </div>
    <div class="oct-field">
      <label class="oct-label" for="oct-exp">Expiry</label>
      <Select id="oct-exp"
        bind:value={chainExpiry}
        options={chainExpiries.map(e => ({ value: e, label: e }))}
        placeholder={chainExpiries.length ? 'Pick expiry' : '—'} />
    </div>
    <div class="oct-field">
      <label class="oct-label" for="oct-kind">Kind</label>
      <MultiSelect id="oct-kind"
        bind:value={chainKinds}
        options={[{ value: 'opt', label: 'Options' }, { value: 'fut', label: 'Futures' }]}
        placeholder="Both" />
    </div>
  </div>

  <!-- Spot + ATM pill -->
  {#if chainSpot != null}
    <div class="oct-spot-row">
      <span class="chain-spot-pill">
        SPOT {priceFmt(chainSpot)}
        {#if chainAtmStrike != null}· ATM {priceFmt(chainAtmStrike)}{/if}
      </span>
    </div>
  {/if}

  <!-- Futures rows -->
  {#if chainKinds.includes('fut') && chainFutures.length}
    <div class="chain-futures">
      {#each chainFutures as f (f.s)}
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
            {#if isAtm}
              <tr class="chain-row chain-row-{dir} chain-row-atm" use:chainAtmRow>
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
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
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.ask)}</span>
                    </span>
                  </span>
                </td>
                <td class="chain-td-strike chain-td-strike-atm">{k.toFixed(0)}</td>
                <td class="chain-td-pe">
                  <span class="chain-cell-row chain-cell-row-pe">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.ask)}</span>
                    </span>
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
                  </span>
                </td>
              </tr>
            {:else}
              <tr class="chain-row chain-row-{dir}">
                <td class="chain-td-ce">
                  <span class="chain-cell-row chain-cell-row-ce">
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
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.ce?.ask)}</span>
                    </span>
                  </span>
                </td>
                <td class="chain-td-strike">{k.toFixed(0)}</td>
                <td class="chain-td-pe">
                  <span class="chain-cell-row chain-cell-row-pe">
                    <span class="chain-cell-quote">
                      <span class="chain-cell-bid">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.bid)}</span><span
                            class="chain-cell-sep">-</span><span
                            class="chain-cell-ask">{_fmtLtp(chainQuotesMap?.[String(k)]?.pe?.ask)}</span>
                    </span>
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

  <!-- Basket bar -->
  {#if chainBasket.length}
    <div class="chain-basket">
      <div class="chain-basket-legs">
        {#each chainBasket as leg (leg.key)}
          <span class="chain-basket-leg chain-basket-leg-{leg.side === 'BUY' ? 'buy' : 'sell'} chain-basket-leg-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : 'fut'}"
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
            <span class="chain-basket-sym">{leg.sym}</span>
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
        <button type="button" class="chain-basket-clear" disabled={basketPlacing} onclick={clearBasket}>Clear</button>
        <button type="button" class="chain-basket-place" disabled={basketPlacing} onclick={placeBasket}>
          {#if basketPlacing}Placing… ({basketProgress}/{chainBasket.length}){:else}Place {chainBasket.length} leg{chainBasket.length === 1 ? '' : 's'}{/if}
        </button>
      </div>
      {#if basketError}
        <div class="chain-basket-err">{basketError}</div>
      {/if}
    </div>
  {:else if basketJustDone}
    <div class="chain-basket-toast">✓ basket placed</div>
  {/if}
</div>

<style>
  .oct-root { display: flex; flex-direction: column; gap: 0.5rem; }

  .oct-label {
    display: block;
    font-size: 0.62rem;
    color: #fbbf24;
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
  .oct-acct-hint { font-size: 0.62rem; color: #7e97b8; font-style: italic; }
  .oct-acct-single {
    font-family: monospace;
    font-size: 0.72rem;
    font-weight: 700;
    color: #c8d8f0;
    padding: 0.2rem 0.4rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
  }
  .oct-acct-select {
    font-family: monospace;
    font-size: 0.72rem;
    color: #c8d8f0;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.25rem 0.4rem;
    cursor: pointer;
  }
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
  .oct-spot-row { margin-bottom: 0.25rem; }
  .oct-empty { font-size: 0.6rem; color: #a3b9d0; font-style: italic; margin-top: 0.5rem; }

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
    font-size: 0.65rem;
    font-weight: 700;
    color: #7dd3fc;
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
  }
  .chain-fut-meta {
    font-size: 0.58rem;
    color: #a3b9d0;
    font-weight: 500;
  }
  .chain-grid-wrap {
    overflow-y: auto;
    max-height: 22rem;
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 3px;
  }
  .chain-grid {
    width: 100%;
    border-collapse: collapse;
    font-family: monospace;
    font-size: 0.65rem;
  }
  .chain-col-ce     { width: 42%; }
  .chain-col-strike { width: 16%; }
  .chain-col-pe     { width: 42%; }
  .chain-th-ce      { text-align: left;   color: #4ade80; padding: 0.2rem 0.5rem; font-weight: 700; font-size: 0.62rem; border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-th-pe      { text-align: right;  color: #f87171; padding: 0.2rem 0.5rem; font-weight: 700; font-size: 0.62rem; border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-th-strike  { text-align: center; color: #c8d8f0; padding: 0.2rem 0.3rem; font-weight: 700; font-size: 0.62rem; border-bottom: 1px solid rgba(255,255,255,0.08); background: rgba(13,21,38,0.6); }
  .chain-row > td {
    padding: 0.18rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .chain-row:last-child > td { border-bottom: 0; }
  .chain-td-ce      { text-align: left; }
  .chain-td-pe      { text-align: right; }
  .chain-cell-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
    width: 100%;
  }
  .chain-td-strike  { text-align: center; color: #c8d8f0; font-weight: 700; }
  .chain-td-strike-atm { color: #fbbf24; font-weight: 800; letter-spacing: 0.04em; }
  .chain-cell-quote {
    display: inline-flex;
    align-items: baseline;
    min-width: 3.4rem;
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 600;
    white-space: nowrap;
    text-align: center;
  }
  .chain-cell-bid { color: #4ade80; }
  .chain-cell-ask { color: #f87171; }
  .chain-cell-sep { color: #7e97b8; opacity: 0.7; margin: 0 0.18rem; }
  .chain-side-action { display: inline-flex; align-items: center; }
  .chain-row-itm-call > td { background: rgba(56,189,248,0.05); }
  .chain-row-itm-put  > td { background: rgba(251,146,60,0.05); }
  .chain-row-atm > td {
    background: rgba(251,191,36,0.18);
    border-top:    1px solid rgba(251,191,36,0.55);
    border-bottom: 1px solid rgba(251,191,36,0.55);
  }
  .chain-spot-pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    font-family: monospace; font-size: 0.65rem; font-weight: 700; letter-spacing: 0.05em;
    padding: 1px 6px; border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.55);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
  }
  .chain-btn {
    font-family: monospace; font-size: 0.65rem; font-weight: 700;
    padding: 1px 6px; border-radius: 2px;
    border: 1px solid currentColor; background: transparent;
    cursor: pointer; letter-spacing: 0.04em; transition: background 0.12s;
  }
  .chain-btn-pair { display: inline-flex; gap: 3px; }
  .chain-btn-buy  { color: #4ade80; }
  .chain-btn-sell { color: #f87171; }
  .chain-btn-buy:hover  { background: rgba(74,222,128,0.10); }
  .chain-btn-sell:hover { background: rgba(248,113,113,0.10); }
  .chain-quick-toast {
    display: inline-block; padding: 2px 8px; border-radius: 2px;
    background: rgba(74,222,128,0.18); color: #4ade80;
    font-family: monospace; font-size: 0.6rem; font-weight: 700;
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
    font-family: monospace; font-size: 0.6rem; line-height: 1.5;
    cursor: pointer; user-select: none; transition: background 0.12s, transform 0.05s;
  }
  .chain-basket-leg:hover:not(.is-disabled) { background: rgba(248,113,113,0.10); transform: translateY(-1px); }
  .chain-basket-leg.is-disabled { cursor: progress; opacity: 0.55; }
  .chain-basket-leg-buy  { color: #4ade80; background: rgba(74,222,128,0.06); }
  .chain-basket-leg-sell { color: #f87171; background: rgba(248,113,113,0.06); }
  .chain-basket-leg-type-ce  { border-left-color: #4ade80; }
  .chain-basket-leg-type-pe  { border-left-color: #f87171; }
  .chain-basket-leg-type-fut { border-left-color: #7dd3fc; }
  .chain-basket-side { font-weight: 800; letter-spacing: 0.04em; }
  .chain-basket-sym { color: #c8d8f0; font-weight: 600; }
  .chain-basket-qty { color: #a3b9d0; font-size: 0.58rem; opacity: 0.85; font-variant-numeric: tabular-nums; }
  .chain-basket-step {
    width: 1.05rem; height: 1.05rem; padding: 0; border-radius: 2px;
    border: 1px solid currentColor; background: transparent; color: currentColor;
    cursor: pointer; font-family: monospace; font-size: 0.7rem; font-weight: 700;
    line-height: 1; display: inline-flex; align-items: center; justify-content: center;
  }
  .chain-basket-step:hover:not(:disabled) { background: rgba(255,255,255,0.05); }
  .chain-basket-step:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-lots { min-width: 1.1rem; text-align: center; color: #fbbf24; font-family: monospace; font-weight: 700; font-size: 0.62rem; font-variant-numeric: tabular-nums; }
  .chain-basket-limit-static { color: #fbbf24; font-family: monospace; font-size: 0.62rem; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: 0.02em; }
  .chain-basket-chase { display: inline-flex; align-items: center; gap: 0.15rem; margin-left: 0.15rem; }
  .chain-basket-chase-pill {
    width: 1rem; height: 1rem; padding: 0;
    border: 1px solid rgba(126,151,184,0.35); border-radius: 2px;
    background: transparent; color: #a3b9d0;
    font-family: monospace; font-size: 0.55rem; font-weight: 700; line-height: 1;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  }
  .chain-basket-chase-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  .chain-basket-chase-pill-low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; border-color: rgba(125,211,252,0.55); }
  .chain-basket-chase-pill-med.on  { background: rgba(251,191,36,0.20);  color: #fbbf24; border-color: rgba(251,191,36,0.55); }
  .chain-basket-chase-pill-high.on { background: rgba(74,222,128,0.20);  color: #4ade80; border-color: rgba(74,222,128,0.55); }
  .chain-basket-actions { display: inline-flex; align-items: center; gap: 0.4rem; margin-left: auto; flex-wrap: wrap; }
  .chain-basket-clear,
  .chain-basket-place {
    height: 1.5rem; padding: 0 0.7rem; border-radius: 2px;
    border: 1px solid currentColor; background: transparent;
    cursor: pointer; font-family: monospace; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.04em;
  }
  .chain-basket-clear { color: #a3b9d0; }
  .chain-basket-clear:hover { background: rgba(163,185,208,0.08); }
  .chain-basket-place { color: #fbbf24; background: rgba(251,191,36,0.10); }
  .chain-basket-place:hover { background: rgba(251,191,36,0.20); }
  .chain-basket-place:disabled,
  .chain-basket-clear:disabled { opacity: 0.55; cursor: progress; }
  .chain-basket-err { flex: 1 1 100%; color: #f87171; font-family: monospace; font-size: 0.6rem; margin-top: 0.2rem; }
  .chain-basket-toast {
    margin-top: 0.5rem; padding: 0.3rem 0.5rem; border-radius: 2px;
    background: rgba(74,222,128,0.14); color: #4ade80;
    font-family: monospace; font-size: 0.65rem; font-weight: 700; text-align: center;
    animation: chain-quick-fade 2.2s ease-out forwards;
  }
  @keyframes chain-quick-fade {
    0%   { opacity: 1; }
    70%  { opacity: 1; }
    100% { opacity: 0; }
  }
</style>
