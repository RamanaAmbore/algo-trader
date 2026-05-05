<script>
  // Options Analytics dashboard (/admin/options).
  //
  // Distinct from the regular tick-chart pages (/admin/simulator,
  // /admin/paper) — those are tick-by-tick price monitors. THIS page is
  // an options-research workspace: payoff diagram, Greeks, theoretical-
  // vs-market discrepancy, risk metrics, POP, historical price chart.
  //
  // Four input modes:
  //   live          — pick a symbol from broker positions
  //   sim           — pick from active simulator positions
  //   hypothetical  — type any option symbol to dry-analyse pre-trade
  //   strategy      — combine multiple legs (vertical, iron condor, …)
  //
  // The analytics endpoint returns everything in one round-trip; this
  // page polls every 5 s while the symbol is set so Greeks + IV + LTP
  // stay current. Historical chart fetches once on symbol change.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp, marketAwareInterval } from '$lib/stores';
  import { isMarketOpen } from '$lib/marketHours';
  import {
    fetchPositions, fetchSimStatus, fetchStrategyAnalytics,
    fetchAccounts, fetchOptionsSpot, fetchChainQuotes,
    placeTicketOrder,
  } from '$lib/api';
  import OptionsPayoff from '$lib/OptionsPayoff.svelte';
  import OrderTicket   from '$lib/order/OrderTicket.svelte';
  import Select        from '$lib/Select.svelte';
  import MultiSelect   from '$lib/MultiSelect.svelte';
  import InfoHint      from '$lib/InfoHint.svelte';
  import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listStrikes, findOption,
    listFutures, getInstrument,
  } from '$lib/data/instruments';

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
  /** @type {string} Underlying name (e.g. NIFTY); '' = pick required */
  let selectedUnderlying = $state('');
  /** @type {string} Expiry filter (YYYY-MM-DD); '' = all expiries.
   *  When the underlying has options across multiple expiries, the
   *  strategy endpoint rejects mixed-expiry baskets — so the picker
   *  forces the operator to nominate one expiry. Auto-picked to the
   *  nearest available expiry when the underlying changes. */
  let selectedExpiry = $state('');
  /** @type {Record<string, boolean>} symbol → enabled flag */
  let enabledSymbols = $state({});

  // Legs sent to the strategy endpoint — built from candidate positions
  // (live or sim, depending on simActive) plus drafts that match the
  // selected underlying, intersected with the operator's checked rows
  // in the Candidates panel.
  /** @type {Array<{symbol:string, qty:any, avg_cost:any, ltp:any, source:string}>} */
  let legs = $state([]);

  // Legs panel collapsed/expanded — operator may want to fold it
  // away once they've vetted the basket so the chart + cards have
  // more vertical room.
  let legsOpen = $state(true);

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
    return Array.from(accts).sort();
  });
  const underlyingChoicesFromBook = $derived.by(() => {
    const set = new Set();
    for (const p of positions) {
      if (!/(CE|PE|FUT)$/i.test(p.symbol)) continue;
      // Strip everything from the first digit on — that's where the
      // YY-month-strike block starts. Works for monthly + weekly.
      const u = p.symbol.replace(/\d.*$/, '');
      if (u) set.add(u);
    }
    return Array.from(set).sort();
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

  /** Auto-pick the first expiry when the underlying changes (and
   *  drop the picked expiry if it disappears from the list). */
  $effect(() => {
    void selectedUnderlying;
    untrack(() => {
      const list = expiryChoicesForUnderlying;
      if (!list.length) {
        if (selectedExpiry) selectedExpiry = '';
        return;
      }
      if (!list.includes(selectedExpiry)) selectedExpiry = list[0];
    });
  });

  // Candidate positions matching the filter. Live + sim positions on
  // the chosen underlying held in one of the chosen accounts, plus all
  // drafts whose symbol matches the underlying prefix. Source is a
  // per-row property (badge in the panel), not a mode-level filter.
  /** @type {{symbol:string,account:string,qty:number,avg_cost:number|null,ltp:number|null,pnl?:number,source:string,kind:string,draftId?:number}[]} */
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
    // Expiry filter — when an expiry is selected, only legs whose
    // contract expires on that date appear (strategy endpoint
    // rejects mixed-expiry baskets).
    const wantedSource = simActive ? 'sim' : 'live';
    const matchExpiry = /** @param {string} sym */ (sym) => {
      if (!selectedExpiry) return true;
      const inst = getInstrument(String(sym || '').toUpperCase());
      return inst?.x === selectedExpiry;
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
      if (enabledSymbols[c.symbol] === false) continue;
      s += Number(c.pnl || 0);
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
    return candidatePositions.every(c => enabledSymbols[c.symbol] !== false);
  });
  const someCandidatesOn = $derived.by(() => {
    if (!candidatePositions.length) return false;
    return candidatePositions.some(c => enabledSymbols[c.symbol] !== false);
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
    for (const c of candidatePositions) next[c.symbol] = target;
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
        if (!(c.symbol in enabledSymbols)) changed = true;
        next[c.symbol] = (c.symbol in enabledSymbols)
          ? enabledSymbols[c.symbol]
          : true;
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
        .filter(c => enabledSymbols[c.symbol] !== false)
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

    basketPlacing  = true;
    basketError    = '';
    basketProgress = 0;
    /** @type {string[]} */ const failures = [];
    for (const leg of chainBasket) {
      try {
        const hasLimit = Number(leg.limit) > 0;
        await placeTicketOrder({
          mode:          'paper',
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
  const _COMMON_INDICES_AND_COMMODITIES = [
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
    'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL',
    'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
    'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
    'ALUMINIUM', 'ALUMINI', 'NICKEL',
    'MENTHAOIL', 'COTTON',
  ];
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
    // 4. Everything else from the instruments cache.
    for (const u of suggestUnderlyings('', 1000)) push(u);
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
  /** @type {{spot:number, source:string, prevClose:number|null} | null} */
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
        clearInterval(chainQuotesPoll);
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
      chainQuotesPoll = setInterval(_refreshChainQuotes, 5000);
    });
  });
  onDestroy(() => {
    if (chainQuotesPoll) clearInterval(chainQuotesPoll);
  });
  function _fmtLtp(/** @type {number|null|undefined} */ v) {
    if (v == null || !Number.isFinite(v)) return '—';
    return v >= 100
      ? v.toFixed(0)
      : v >= 10
        ? v.toFixed(1)
        : v.toFixed(2);
  }

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
  // <OrderTicket> modal; on DRAFT submit we append to drafts. Phase
  // 2 / 3 add PAPER / LIVE submit paths through the ticket without
  // touching this file.
  /** @type {any} */
  let ticketProps = $state(null);
  function openTicket(/** @type {any} */ p) { ticketProps = p; }
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
    // real account. Then the masked-but-only choice — falls back to
    // empty so the picker forces an explicit pick. Never returns a
    // masked value that the order endpoint can't route on.
    if (selectedAccounts.length === 1 && _isRealAccount(selectedAccounts[0])) {
      return selectedAccounts[0];
    }
    if (realAccounts.length === 1) return realAccounts[0];
    if (accountChoices.length === 1 && _isRealAccount(accountChoices[0])) {
      return String(accountChoices[0]);
    }
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
    const lots = Math.max(1, Number(chainLots) || 1);
    openTicket({
      symbol:   inst.s,
      // Exchange comes from the instruments cache (Kite's authoritative
      // `e` field per contract). CRUDEOIL options live on MCX, NIFTY
      // options on NFO, BSE options on BFO — hardcoding NFO would route
      // every depth lookup to the wrong exchange and the ladder would
      // come back empty.
      exchange: inst.e || 'NFO',
      side:     side === 'long' ? 'BUY' : 'SELL',
      qty:      lot * lots,
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
    const lots = Math.max(1, Number(chainLots) || 1);
    // Look up the instrument so we route to the right exchange
    // (commodity futures live on MCX, not NFO).
    const inst = getInstrument(sym.toUpperCase());
    openTicket({
      symbol:   sym,
      exchange: inst?.e || 'NFO',
      side:     side === 'long' ? 'BUY' : 'SELL',
      qty:      lot * lots,
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
    const qty = Math.abs(Number(c.qty || 0));
    if (!qty) return;       // already closed
    const inst = getInstrument(String(c.symbol).toUpperCase());
    const lot  = Number(inst?.ls || 1);
    openTicket({
      symbol:   c.symbol,
      exchange: inst?.e || 'NFO',
      side:     c.qty < 0 ? 'BUY' : 'SELL',   // opposite of held
      action:   'close',
      qty,
      lotSize:  lot,
      // Pass signed qty so the OrderTicket can label the side toggle
      // as ADD / CLOSE (instead of BUY / SELL) — operator thinks in
      // "I want to close this position" terms when clicking on a
      // current position row.
      currentQty: Number(c.qty || 0),
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
    const qty  = Math.abs(Number(c.qty || 0)) || lot;
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
      defaultMode:    'live',
      availableModes: ['live'],
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
   *  Forcing a single-account filter makes the routing unambiguous;
   *  the OrderTicket inherits that account via _ticketAccountDefault. */
  const chainEnabled = $derived(
    selectedAccounts.length === 1 && _isRealAccount(selectedAccounts[0])
  );

  // Auto-close the chain panel when the operator de-selects / multi-
  // selects accounts after opening it — otherwise a stale picker
  // would sit open with the buttons greyed.
  $effect(() => {
    if (!chainEnabled && showAddPanel) showAddPanel = false;
  });

  async function loadPositions() {
    /** @type {Array<{symbol:string, account:string, qty:number, source:string, avg_cost:number|null, ltp:number|null, pnl:number}>} */
    const merged = [];

    // Live broker positions
    try {
      const r = await fetchPositions();
      for (const p of (r?.rows || [])) {
        const sym = p?.tradingsymbol || p?.symbol;
        if (!sym) continue;
        // Only options + futures (skip cash equities — this page is
        // options-only).
        if (!/(CE|PE|FUT)$/i.test(String(sym))) continue;
        merged.push({
          symbol:   String(sym).toUpperCase(),
          account:  String(p?.account || ''),
          qty:      Number(p?.quantity || 0),
          source:   'live',
          avg_cost: p?.average_price != null ? Number(p.average_price) : null,
          ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
          // Broker P&L (realised + unrealised). For qty=0 closed-out
          // intraday rows this is the realized P&L; the chart adds it
          // up separately so the legs panel + dashboard reconcile.
          pnl:      p?.pnl != null ? Number(p.pnl) : 0,
        });
      }
    } catch (_) { /* ignore — show sim only */ }

    // Sim positions — capture last_price + average_price from the
    // driver state at click time so the strategy endpoint can compute
    // analytics without round-tripping back to the broker.
    try {
      const s = await fetchSimStatus();
      for (const p of (s?.positions || [])) {
        const sym = p?.symbol;
        if (!sym) continue;
        if (!/(CE|PE|FUT)$/i.test(String(sym))) continue;
        merged.push({
          symbol:   String(sym).toUpperCase(),
          account:  String(p?.account || ''),
          qty:      Number(p?.quantity || 0),
          source:   'sim',
          avg_cost: p?.average_price != null ? Number(p.average_price) : null,
          ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
          pnl:      p?.pnl != null ? Number(p.pnl) : 0,
        });
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
    } catch (_) { simActive = false; }
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
        selectedAccounts, selectedUnderlying, selectedExpiry,
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
      if (typeof d.selectedExpiry === 'string')      selectedExpiry    = d.selectedExpiry;
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
    loadPositions();
    // Real broker accounts — needed by the OrderTicket so the
    // operator can pick which Kite handle the order routes through.
    // Fetched separately from positions because /positions masks
    // the account field for non-admin signed-in users; /accounts
    // returns unmasked account_ids to any authenticated user.
    loadRealAccounts();
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
    simTeardown = marketAwareInterval(loadSimStatus,  5000);
  });
  onDestroy(() => { teardown?.(); posTeardown?.(); simTeardown?.(); });

  // ── Helpers ──────────────────────────────────────────────────────
  function fmtMoney(/** @type {number|null|undefined} */ v, /** @type {boolean} */ signed = true) {
    if (v == null) return '∞';
    const sign = signed ? (v < 0 ? '-' : v > 0 ? '+' : '') : '';
    return `${sign}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
  }
  function fmtPct(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return `${(v * 100).toFixed(2)}%`;
  }
  function fmtNum(/** @type {number|null|undefined} */ v, /** @type {number} */ dp = 4) {
    if (v == null) return '—';
    return v.toFixed(dp);
  }

</script>

<svelte:head><title>Options Analytics | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Options Analytics</h1>
  <InfoHint text={'Pick an underlying to load every option / future on it from your ' + (simActive ? '<b>simulator</b> book' : '<b>live</b> book') + '. The payoff diagram below charts the aggregated position; uncheck a row in the Candidates panel to drop it from the payoff. Click <b>+ Add</b> to open the option chain and pick draft strikes (modelled as hypothetical positions). Stats below the chart explain themselves — click any <span class="font-mono">(i)</span> chip for a definition.'} />
  <span class="algo-ts">{clientTimestamp()}</span>
  {#if simActive}
    <span class="opt-mode-pill opt-mode-sim" title="A simulator run is active. Candidates and analytics are sourced from the sim book.">SIMULATOR</span>
  {/if}
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
    <MultiSelect id="opt-acct"
      bind:value={selectedAccounts}
      options={accountChoices.map(a => ({ value: a, label: a }))}
      placeholder={accountChoices.length ? 'All accounts' : 'No accounts loaded'} />
  </div>
  <div class="opt-field opt-field-grow">
    <label class="field-label" for="opt-und">Underlying</label>
    <Select id="opt-und"
      bind:value={selectedUnderlying}
      options={underlyingChoicesFromBook.map(u => ({ value: u, label: u }))}
      placeholder={underlyingChoicesFromBook.length ? 'Pick underlying…' : 'No options in book'} />
  </div>
  <div class="opt-field">
    <label class="field-label" for="opt-exp">Expiry</label>
    <Select id="opt-exp"
      bind:value={selectedExpiry}
      options={expiryChoicesForUnderlying.map(x => ({ value: x, label: x }))}
      placeholder={expiryChoicesForUnderlying.length ? 'Pick expiry' : '—'} />
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
            disabled={!chainEnabled}
            title={chainEnabled
              ? (showAddPanel ? 'Hide the chain picker' : 'Open the chain picker')
              : 'Pick exactly one account to enable the chain picker'}
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
                if (selectedUnderlying) chainUnderlying = selectedUnderlying;
                if (selectedExpiry)     chainExpiry     = selectedExpiry;
              }
              showAddPanel = !showAddPanel;
            }}>OChain</button>
  </div>
</div>

{#if strategyErr}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{strategyErr}</div>
{/if}

{#if !strategy && !strategyErr && !loading && !selectedUnderlying && !drafts.length}
  <div class="text-[0.65rem] text-[#7e97b8] italic mb-3">
    Pick an underlying to surface {simActive ? 'sim' : 'live'} candidates, or click
    <b>+ Add</b> to drop a draft strike into the payoff.
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
{#if showAddPanel}
  {#if instrumentsReady && underlyingChoices.length}
    <div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
      <div class="opt-section-h" style="padding-bottom: 0.5rem;">
        Option chain
        {#if chainSpot != null}
          <span class="chain-spot-pill" title="Underlying spot — ATM strike highlighted in the grid below">
            SPOT ₹{chainSpot.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
            {#if chainAtmStrike != null}
              · ATM {chainAtmStrike.toFixed(0)}
            {/if}
          </span>
        {/if}
        <!-- The trailing "click CE / PE next to a strike to add a leg
             · quantity defaults to 1 lot" hint was dropped per
             operator request: with it gone, Option chain + SPOT/ATM
             pill fit on a single line at narrow widths. -->
      </div>
      <div class="chain-controls">
        <div class="chain-field">
          <label class="field-label" for="chain-und">Underlying</label>
          <Select id="chain-und"
            bind:value={chainUnderlying}
            searchable={true}
            searchPlaceholder="Type 3+ chars to filter…"
            options={underlyingChoices.map(u => ({ value: u, label: u }))} />
        </div>
        <div class="chain-field">
          <label class="field-label" for="chain-exp">Expiry</label>
          <Select id="chain-exp"
            bind:value={chainExpiry}
            options={chainExpiries.map(e => ({ value: e, label: e }))}
            placeholder={chainExpiries.length ? 'Pick expiry' : '—'} />
        </div>
        <div class="chain-field">
          <label class="field-label" for="chain-kind">Kind</label>
          <MultiSelect id="chain-kind"
            bind:value={chainKinds}
            options={[
              { value: 'opt', label: 'Options'  },
              { value: 'fut', label: 'Futures'  },
            ]}
            placeholder="Both" />
        </div>
      </div>
      {#if chainKinds.includes('fut') && chainFutures.length}
        <!-- Futures rows — same `+ − i` button cluster as the strike
             rows, just no strike / CE / PE columns. Symbol + lot
             size sit on the left; buttons on the right. -->
        <div class="chain-futures">
          {#each chainFutures as f (f.s)}
            {@const futKey = _quickKeyFut(f.s)}
            <div class="chain-fut-row">
              <span class="chain-fut-sym">
                {f.s}<span class="chain-fut-meta">lot {f.ls}</span>
              </span>
              <span class="chain-side-action">
                <span class="chain-btn-pair">
                  <button type="button" class="chain-btn chain-btn-buy"
                          title="BUY {f.s} — adds 1 lot ({f.ls}) to basket"
                          onclick={() => addFuturesToBasket(f.s, f.ls, 'long')}>+</button>
                  <button type="button" class="chain-btn chain-btn-sell"
                          title="SELL {f.s} — adds 1 lot to basket"
                          onclick={() => addFuturesToBasket(f.s, f.ls, 'short')}>−</button>
                  <button type="button" class="chain-btn chain-btn-info"
                          title="Open full ticket for {f.s} (edit qty / price / mode)"
                          onclick={() => addFutureDraft(f.s, f.ls, 'long')}>i</button>
                </span>
                {#if quickToast?.key === futKey}
                  <span class="chain-quick-toast">{quickToast?.msg}</span>
                {/if}
              </span>
            </div>
          {/each}
        </div>
      {/if}
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
                {@const dir   = chainSpot != null
                                ? (k < chainSpot ? 'itm-call'
                                  : k > chainSpot ? 'itm-put' : 'atm')
                                : ''}
                <!-- Each strike row exposes BOTH BUY (+) and SELL (−)
                     for both CE and PE — 4 buttons total — so the
                     operator can land mixed long/short legs across
                     strikes in one open-then-pick session without
                     toggling the outer +/− between picks. -->
                <!-- Buttons drop the CE/PE suffix — the column header
                     (CE | Strike | PE) already attributes side, so the
                     glyph alone is enough. (i) opens the full
                     OrderTicket pre-filled for that side+type as the
                     advanced / "edit before placing" path. -->
                <!-- + / − fast path: opens the inline lots-picker
                     in-cell. Press ✓ → submits a paper MARKET order
                     directly. (i) is the slow path: opens the full
                     OrderTicket modal pre-filled. -->
                {@const ceKey = _quickKeyOpt(k, 'CE')}
                {@const peKey = _quickKeyOpt(k, 'PE')}
                {#if isAtm}
                  <tr use:chainAtmRow class="chain-row chain-row-{dir} chain-row-atm">
                    <td class="chain-td-ce">
                      <span class="chain-cell-row chain-cell-row-ce">
                        <span class="chain-side-action">
                          <span class="chain-btn-pair">
                            <button type="button" class="chain-btn chain-btn-buy"
                                    title="BUY {k} CE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                            <button type="button" class="chain-btn chain-btn-sell"
                                    title="SELL {k} CE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                            <button type="button" class="chain-btn chain-btn-info"
                                    title="Open full ticket for {k} CE (edit qty / price / mode)"
                                    onclick={() => addChainDraft(k, 'CE', 'long')}>i</button>
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
                    <td class="chain-td-strike chain-td-strike-atm">
                      {k.toFixed(0)}
                    </td>
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
                                    title="BUY {k} PE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                            <button type="button" class="chain-btn chain-btn-sell"
                                    title="SELL {k} PE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                            <button type="button" class="chain-btn chain-btn-info"
                                    title="Open full ticket for {k} PE (edit qty / price / mode)"
                                    onclick={() => addChainDraft(k, 'PE', 'long')}>i</button>
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
                                    title="BUY {k} CE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'CE', 'long')}>+</button>
                            <button type="button" class="chain-btn chain-btn-sell"
                                    title="SELL {k} CE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'CE', 'short')}>−</button>
                            <button type="button" class="chain-btn chain-btn-info"
                                    title="Open full ticket for {k} CE (edit qty / price / mode)"
                                    onclick={() => addChainDraft(k, 'CE', 'long')}>i</button>
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
                                    title="BUY {k} PE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'PE', 'long')}>+</button>
                            <button type="button" class="chain-btn chain-btn-sell"
                                    title="SELL {k} PE — adds 1 lot to basket"
                                    onclick={() => addOptionToBasket(k, 'PE', 'short')}>−</button>
                            <button type="button" class="chain-btn chain-btn-info"
                                    title="Open full ticket for {k} PE (edit qty / price / mode)"
                                    onclick={() => addChainDraft(k, 'PE', 'long')}>i</button>
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
      {:else}
        <div class="text-[0.6rem] text-[#a3b9d0] italic mt-2">
          No strikes for {chainUnderlying} expiring {chainExpiry || '(pick expiry)'}.
          Try a different underlying or expiry.
        </div>
      {/if}

      <!-- Basket bar — shows up the moment the operator stages their
           first leg via ✓ in the inline picker. One-leg or many,
           pressing Place fires every leg sequentially as a paper
           MARKET order through the same /api/orders/ticket path the
           OrderTicket modal uses. -->
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
                        e.preventDefault();
                        removeFromBasket(leg.key);
                      }
                    }}>
                <span class="chain-basket-side">{leg.side === 'BUY' ? 'B' : 'S'}</span>
                <span class="chain-basket-sym">{leg.sym}</span>
                <button type="button" class="chain-basket-step"
                        title="Decrease lots"
                        disabled={basketPlacing || leg.lots <= 1}
                        onclick={(e) => { e.stopPropagation(); basketStepLots(leg.key, -1); }}>−</button>
                <span class="chain-basket-lots">{leg.lots}</span>
                <button type="button" class="chain-basket-step"
                        title="Increase lots"
                        disabled={basketPlacing}
                        onclick={(e) => { e.stopPropagation(); basketStepLots(leg.key, +1); }}>+</button>
                <span class="chain-basket-qty">× {leg.lotSize} = {leg.lots * leg.lotSize}</span>
                <span class="chain-basket-limit-static"
                      title={leg.limit > 0
                        ? `Algo-selected limit (₹${leg.limit.toLocaleString('en-IN', { maximumFractionDigits: 2 })} from the chain bid/ask at add-time). Chase re-quotes per the L/M/H pill.`
                        : 'No quote available — leg routes as MARKET.'}>
                  {#if leg.limit > 0}
                    algo @{leg.limit.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                  {:else}
                    @MKT
                  {/if}
                </span>
                <span class="chain-basket-chase" title="Chase aggressiveness. L = patient (peg to your side), M = midpoint, H = urgent (cross the spread).">
                  <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-low"
                          class:on={(leg.chaseAgg || 'low') === 'low'}
                          disabled={basketPlacing}
                          title="Low — patient. SELL pegs to ASK, BUY pegs to BID."
                          onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'low'); }}>L</button>
                  <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-med"
                          class:on={leg.chaseAgg === 'med'}
                          disabled={basketPlacing}
                          title="Medium — peg to midpoint of bid+ask."
                          onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'med'); }}>M</button>
                  <button type="button" class="chain-basket-chase-pill chain-basket-chase-pill-high"
                          class:on={leg.chaseAgg === 'high'}
                          disabled={basketPlacing}
                          title="High — urgent. SELL pegs to BID, BUY pegs to ASK."
                          onclick={(e) => { e.stopPropagation(); setBasketChaseAgg(leg.key, 'high'); }}>H</button>
                </span>
              </span>
            {/each}
          </div>
          <div class="chain-basket-actions">
            <button type="button" class="chain-basket-clear"
                    disabled={basketPlacing}
                    onclick={clearBasket}>Clear</button>
            <button type="button" class="chain-basket-place"
                    disabled={basketPlacing}
                    onclick={placeBasket}>
              {#if basketPlacing}
                Placing… ({basketProgress}/{chainBasket.length})
              {:else}
                Place {chainBasket.length} leg{chainBasket.length === 1 ? '' : 's'}
              {/if}
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
  {/if}
{/if}

{#if strategy}
  <div class="opt-payoff opt-payoff-full mb-3">
    <!-- Single-row header — title + Net debit/credit + Max profit /
         Max loss chips. DTE / σ / LEGS / SPOT / TDAY / EXP live in
         the on-chart stat overlay; MAX P / MAX L stay outside the
         chart so the at-a-glance "what can this strategy make/lose"
         pair reads at the page-header altitude. -->
    <div class="opt-section-h opt-section-h-grid">
      <div class="opt-section-row">
        <span class="opt-section-title">Payoff</span>
        <span class="opt-section-tag tag-{strategy.net_cost > 0 ? 'long' : strategy.net_cost < 0 ? 'short' : 'long'}">
          {strategy.net_cost > 0 ? 'NET DR' : strategy.net_cost < 0 ? 'NET CR' : 'FREE'}
          {fmtMoney(Math.abs(strategy.net_cost), false)}
        </span>
        <span class="opt-section-tag tag-long" title="Max profit">
          MAX P {fmtMoney(strategy.risk.max_profit, false)}
        </span>
        <span class="opt-section-tag tag-short" title="Max loss">
          MAX L {fmtMoney(strategy.risk.max_loss, false)}
        </span>
        <!-- The dedicated REAL chip in the header was retired —
             realised P&L is now folded into chartPnlOffset and
             applied as a vertical curve shift inside OptionsPayoff,
             so the chart's TDAY at any spot already matches the
             dashboard total. The breakdown (open theoretical-vs-LTP
             gap + closed realised) is surfaced inline as the RLZ
             row in the chart's stat overlay. -->
      </div>
    </div>
    <OptionsPayoff
      payoff={strategy.payoff}
      spot={strategy.spot}
      prevClose={strategy.spot_prev_close}
      breakevens={strategy.risk.breakevens}
      intermediateCurves={strategy.intermediate_curves || []}
      spanSigmas={strategy.span_sigmas}
      spanPct={strategy.span_pct}
      dte={strategy.days_to_expiry}
      ivProxy={strategy.iv_proxy}
      legCount={strategy.legs.length}
      realizedPnl={chartPnlOffset}
      loading={loading}
      onRefresh={() => { loadPositions(); loadSimStatus(); loadStrategy(); }}
      height={320} />
  </div>
{/if}

<!-- Candidates — sits between the payoff chart above and the
     Aggregate / Greeks / Risk cards below. Reading order: see the
     chart → see which legs feed it → see the maths beneath. Each
     row carries position info (qty / cost / LTP / P&L) plus per-leg
     analytics (IV / Δ / Θ / 𝒱) joined from the latest strategy
     response by symbol. Horizontal + vertical overflow scrolling. -->
{#if selectedUnderlying || drafts.length}
  <div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
    <button type="button"
            class="legs-header"
            aria-expanded={legsOpen}
            title={legsOpen ? 'Collapse leg list' : 'Expand leg list'}
            onclick={() => legsOpen = !legsOpen}>
      <span class="legs-chevron">{legsOpen ? '▾' : '▸'}</span>
      <span>Legs</span>
      {#if selectedUnderlying}
        <span class="opt-section-tag tag-deriv">{selectedUnderlying}</span>
      {/if}
      <span class="opt-section-meta">{candidatePositions.length}</span>
    </button>
    {#if legsOpen && candidatePositions.length}
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
            <span>Symbol</span>
            {#if !hideAcct}<span>Acct</span>{/if}
            <span class="num">Qty</span>
            <span class="num">P&amp;L</span>
            <span class="num">Cost</span>
            <span class="num">LTP</span>
            <span class="num">IV</span>
            <span class="num">Δ</span>
            <span class="num">Θ</span>
            <span class="num">𝒱</span>
          </div>
          {#each candidatePositions as c (c.source + '|' + c.account + '|' + c.symbol)}
            {@const lg = legAnalyticsBySymbol[c.symbol]}
            {@const ltp = lg && lg.ltp != null ? lg.ltp : c.ltp}
            {@const cost = c.avg_cost != null ? c.avg_cost : (lg ? lg.avg_cost : null)}
            {@const pnl = (ltp != null && cost != null) ? (ltp - cost) * c.qty : null}
            {@const dir = c.qty < 0 ? 'short' : c.qty > 0 ? 'long' : 'flat'}
            {@const isClosed = Number(c.qty || 0) === 0}
            {@const isClosable = !isClosed && c.source !== 'draft'}
            <!-- Row click → close-position ticket. Skipped on
                 drafts (no real exposure) and zero-qty rows
                 (already closed — sorted to end of list, kept
                 visible for context). The checkbox stops
                 propagation so toggling a leg doesn't
                 inadvertently fire the close handler. -->
            {@const isDraft = c.source === 'draft'}
            {@const isActionable = isDraft || isClosable}
            <div class="cand-row cand-row-{dir}"
                 class:cand-disabled={enabledSymbols[c.symbol] === false}
                 class:cand-closed={isClosed}
                 class:cand-draft={isDraft}
                 role="button"
                 tabindex={isActionable ? 0 : -1}
                 aria-disabled={!isActionable}
                 title={isDraft
                   ? `Execute draft — open the order ticket pre-filled (PAPER / LIVE)`
                   : isClosable
                     ? `Close ${Math.abs(c.qty)} ${c.symbol}`
                     : isClosed ? 'Closed position' : ''}
                 onclick={() => {
                   if (isDraft) executeDraft(c);
                   else if (isClosable) closePosition(c);
                 }}
                 onkeydown={(e) => {
                   if (!isActionable) return;
                   if (e.key === 'Enter' || e.key === ' ') {
                     e.preventDefault();
                     if (isDraft) executeDraft(c);
                     else closePosition(c);
                   }
                 }}>
              <input type="checkbox"
                     checked={enabledSymbols[c.symbol] !== false}
                     onclick={(e) => e.stopPropagation()}
                     onchange={(e) => {
                       const next = { ...enabledSymbols };
                       next[c.symbol] = /** @type {HTMLInputElement} */ (e.currentTarget).checked;
                       enabledSymbols = next;
                     }} />
              <span class="font-mono cand-sym">
                {c.symbol}
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
              </span>
              {#if !hideAcct}<span class="font-mono">{c.account}</span>{/if}
              <span class="num {c.qty < 0 ? 'kv-neg' : 'kv-pos'}">{c.qty > 0 ? '+' : ''}{c.qty}</span>
              <span class="num cand-pnl {pnl == null ? '' : pnl >= 0 ? 'cand-pnl-pos' : 'cand-pnl-neg'}">
                {pnl == null ? '—' : (pnl >= 0 ? '+' : '−') + '₹' + Math.abs(pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              </span>
              <span class="num">{cost != null ? '₹' + cost.toFixed(2) : '—'}</span>
              <span class="num">{ltp != null ? '₹' + ltp.toFixed(2) : '—'}</span>
              <span class="num">{lg ? (lg.iv * 100).toFixed(2) + '%' : '—'}</span>
              <span class="num">{lg ? lg.greeks.delta.toFixed(2) : '—'}</span>
              <span class="num {lg && lg.greeks.theta < 0 ? 'kv-neg' : ''}">{lg ? lg.greeks.theta.toFixed(0) : '—'}</span>
              <span class="num">{lg ? lg.greeks.vega.toFixed(0) : '—'}</span>
            </div>
          {/each}
        </div>
      </div>
    {:else if legsOpen}
      <div class="text-[0.6rem] text-[#7e97b8] italic">
        No options or futures on <b>{selectedUnderlying}</b> in
        {selectedAccounts.length ? 'the chosen accounts' : 'any account'}.
        Try a different underlying / account, or click <b>+</b> to drop a
        draft strike into the payoff.
      </div>
    {/if}
  </div>
{/if}

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
              <span class="kv-v">{fmtNum(strategy.aggregate_greeks.delta, 2)}</span>
            </div>
            <div class="kv-pair" title="Gamma — rate-of-change of delta as spot moves.">
              <span class="kv-k kv-k-greek">Γ</span>
              <span class="kv-v">{fmtNum(strategy.aggregate_greeks.gamma, 2)}</span>
            </div>
            <div class="kv-pair" title="Theta — daily decay in rupees. Positive when net short premium.">
              <span class="kv-k kv-k-greek">Θ</span>
              <span class="kv-v {strategy.aggregate_greeks.theta < 0 ? 'kv-neg' : 'kv-pos'}">{fmtNum(strategy.aggregate_greeks.theta, 2)}</span>
            </div>
            <div class="kv-pair" title="Vega — P&L change per 1 % IV move. Positive = long volatility.">
              <span class="kv-k kv-k-greek">𝒱</span>
              <span class="kv-v {strategy.aggregate_greeks.vega < 0 ? 'kv-neg' : 'kv-pos'}">{fmtNum(strategy.aggregate_greeks.vega, 2)}</span>
            </div>
            <div class="kv-pair" title="Rho — sensitivity to a 1 % rate change. Mostly cosmetic for short-dated index options.">
              <span class="kv-k kv-k-greek">ρ</span>
              <span class="kv-v">{fmtNum(strategy.aggregate_greeks.rho, 2)}</span>
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
              <span class="kv-v">{strategy.risk.rr_ratio == null ? '—' : `1 : ${strategy.risk.rr_ratio.toFixed(2)}`}</span>
            </div>
            <div class="kv-pair">
              <span class="kv-k">Breakevens <InfoHint popup text={'<b>Breakevens</b> — spot prices at expiry where the strategy\'s P&L crosses zero. Iron condors and butterflies have 2; verticals have 1; fully ITM/OTM 0.'} /></span>
              <span class="kv-v">
                {#if strategy.risk.breakevens.length}
                  {strategy.risk.breakevens.map(/** @param {number} b */ (b) => `₹${b.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`).join(' / ')}
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
                  {strategy.risk.ev_pct > 0 ? '+' : ''}{strategy.risk.ev_pct.toFixed(2)}%
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
  <!-- Spread every prop the calling site set onto the ticket. The
       earlier hand-listed pass-through dropped accounts / account /
       action / defaultMode / availableModes / orderId on the floor —
       so chain + futures clicks opened the ticket without an account
       picker (PAPER / LIVE submit blocked) and closePosition's
       action='close' never reached the modal. Spread keeps any
       future ticketProps field flowing through automatically. -->
  <OrderTicket
    {...ticketProps}
    onSubmit={onTicketSubmit}
    onClose={closeTicket}
    onAddToBasket={(payload) => {
      // Same shape as a quick-add chain leg so the basket pill
      // renders identically. Symbol's CE/PE/FUT suffix decides
      // the type accent on the pill. Same-sym + same-side
      // adds bump lots on the existing leg (dedupe).
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

<style>
  /* Picker bar — Account / Underlying / Expiry / + always on a
     single row, even on narrow viewports. flex-wrap: nowrap forces
     the row; min-width: 0 on each field lets the Selects shrink to
     fit (their content scrolls / truncates inside the trigger).
     Equal `flex: 1 1 0` on the three Selects so they share the
     available width proportionally. */
  .opt-picker {
    display: flex;
    flex-wrap: nowrap;
    gap: 0.4rem 0.4rem;
    align-items: flex-end;
  }
  .opt-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1 1 0;
    min-width: 0;          /* allow shrink past content size */
  }
  /* All three Select fields share the same flex weight so each gets
     a third of the leftover space after the + button. */
  .opt-field-grow { flex: 1 1 0; min-width: 0; }

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
    color: #c8d8f0;
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
    color: #c8d8f0;
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
  .opt-historical {
    margin-top: 0.3rem;
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
    /* Compact scrollbar styling — works on WebKit; falls back to
       browser default elsewhere. */
    scrollbar-width: thin;
    scrollbar-color: rgba(251,191,36,0.4) transparent;
  }
  .cand-scroll::-webkit-scrollbar { height: 6px; width: 6px; }
  .cand-scroll::-webkit-scrollbar-thumb {
    background: rgba(251,191,36,0.35);
    border-radius: 3px;
  }
  .cand-scroll::-webkit-scrollbar-thumb:hover {
    background: rgba(251,191,36,0.55);
  }
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
  .legs-chevron {
    /* Bumped to match the Select / MultiSelect dropdown carets
       (0.95rem amber + weight 700). Earlier 0.6rem read as a stray
       glyph; now consistent with the other open/close affordances
       across the algo theme. */
    font-size: 0.95rem;
    line-height: 1;
    font-weight: 700;
    color: #fbbf24;
    width: 1rem;
    text-align: center;
    transition: transform 0.12s ease;
  }

  /* Parent grid — defines column tracks once. Children (`.cand-headrow`
     and each `.cand-row`) consume the same tracks via `subgrid` so
     headers + data cells line up precisely.
     Column order (post-Apr-2026 reshuffle): checkbox · Symbol ·
     Account · Qty · P&L · Cost · LTP · IV · Δ · Θ · 𝒱 · Source.
     P&L sits between Qty and Cost so the operator's eye scans
     "what I have → what I'm making/losing → what I paid". */
  /* Content-sized tracks across the board so the row reads dense
     instead of stretched. Numeric columns (qty / P&L / cost / LTP /
     greeks) use `max-content` so each cell sizes to its widest
     value; the row keeps a tight 0.6 rem gap. Earlier `fr` units
     stretched columns to fill the wrapper width, leaving lots of
     empty space between cells when the panel sat in a wide card. */
  .cand-grid {
    display: grid;
    grid-template-columns:
      auto         /* checkbox */
      max-content  /* symbol */
      max-content  /* account */
      max-content  /* qty */
      max-content  /* pnl */
      max-content  /* cost */
      max-content  /* ltp */
      max-content  /* iv */
      max-content  /* delta */
      max-content  /* theta */
      max-content; /* vega */
    column-gap: 0.6rem;
    row-gap: 0.2rem;
    /* No min-width — the grid is naturally as wide as its content.
       The wrapping .cand-scroll still handles overflow when the
       viewport is narrower than the row. */
    width: max-content;
  }
  /* When the operator filters to a single account, the Account
     column is implicit (every row carries the same value) — drop
     the column entirely. */
  .cand-grid-noacct {
    grid-template-columns:
      auto         /* checkbox */
      max-content  /* symbol */
      max-content  /* qty */
      max-content  /* pnl */
      max-content  /* cost */
      max-content  /* ltp */
      max-content  /* iv */
      max-content  /* delta */
      max-content  /* theta */
      max-content; /* vega */
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

  /* P&L cell — same green/red scheme as /dashboard's pnl-gain /
     pnl-loss classes. Subtle background tint for a glanceable
     "win or lose?" cue at row-scan speed; bold weight so the
     numbers pop alongside the otherwise-muted row content. */
  .cand-pnl {
    border-radius: 2px;
    padding: 0 0.25rem;
    font-weight: 700;
  }
  .cand-pnl-pos {
    color: #4ade80;
    background-color: rgba(74,222,128,0.10);
  }
  .cand-pnl-neg {
    color: #f87171;
    background-color: rgba(248,113,113,0.10);
  }

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
  .cand-kind {
    text-align: center;
    font-weight: 700;
    font-size: 0.65rem;
    letter-spacing: 0.05em;
  }
  .cand-kind-fut { color: #c084fc; }
  .cand-kind-opt { color: #7dd3fc; }

  /* Click-to-pick row variant — used in live / sim mode where the
     candidate is a single-select. Reset button defaults so the row
     reads as plain text not a button, then layer the pick-state styling
     on top (active row gets an amber border + bg). */
  .cand-row-btn {
    background: transparent;
    border: 1px solid transparent;
    color: inherit;
    text-align: left;
    width: 100%;
  }
  .cand-row-btn:hover { background: rgba(251,191,36,0.08); }
  .cand-row-active {
    background: rgba(251,191,36,0.15) !important;
    border-color: rgba(251,191,36,0.55);
  }
  .cand-row-disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .cand-row-disabled:hover { background: transparent; }
  .cand-bullet {
    color: #fbbf24;
    font-size: 0.75rem;
    text-align: center;
    width: 0.9rem;
    line-height: 1;
  }

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
    color: #c8d8f0;
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
  .chain-th-strike { text-align: center; color: #c8d8f0; }
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
  .chain-td-strike  { text-align: center; color: #c8d8f0; font-weight: 700; }
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
    color: #7e97b8;
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
    color: #c8d8f0;
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
    color: #c8d8f0;
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
</style>
