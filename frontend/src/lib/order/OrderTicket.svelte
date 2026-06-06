<script>
  // Reusable order-placement ticket — single component for every
  // order op the platform needs (open / close / modify / repeat /
  // cancel) across every instrument (EQ / FUT / OPT / commodities).
  //
  // Phase 1 scope (this file): visual shell + DRAFT mode wired.
  // PAPER / LIVE submit paths come in phase 2 (backend endpoints)
  // and phase 3 (live broker wiring per the existing
  // execution.live.<action> setting flag).
  //
  // Calling pages own:
  //   - the symbol + side + qty defaults (passed as props)
  //   - the onSubmit handler that decides what DRAFT does (typically
  //     append to a local drafts[] array)
  //   - opening / closing the ticket via <{#if showTicket}> wrap
  //
  // The ticket itself owns:
  //   - field state (qty / type / price / trigger / variety …)
  //   - validation (price required when LIMIT, trigger when SL …)
  //   - mode toggle (DRAFT today; PAPER / LIVE in phase 2 / 3)
  //   - viewport-bounded modal positioning
  //
  // Component is intentionally "dumb" — it doesn't know about the
  // page's drafts array, the strategy state, the broker. Every
  // outcome routes through onSubmit(payload).

  import { onMount, untrack, getContext } from 'svelte';
  import { get } from 'svelte/store';
  import OrderDepth from './OrderDepth.svelte';
  import Select from '$lib/Select.svelte';
  import { placeTicketOrder, previewOrderMargin, fetchAccounts, fetchFunds, modifyOrder, fetchSettings } from '$lib/api';
  import { getDefaultAccount } from '$lib/data/accounts';
  import { aggFmt } from '$lib/format';
  import { executionMode } from '$lib/stores';
  import {
    getInstrument, listExpiries, listStrikes,
    findOption, findNearestFuture, listFutures,
  } from '$lib/data/instruments';

  // Demo-mode detection — used to suppress margin preflight (403) and
  // account self-fetch (401) for anonymous prod visitors.
  const _algoStatus = getContext('algoStatus');
  const _isDemo = $derived(_algoStatus?.isDemo ?? false);

  /** @type {{
   *   symbol:    string,
   *   exchange?: string,
   *   side?:     'BUY' | 'SELL',
   *   action?:   'open' | 'close' | 'modify' | 'repeat' | 'cancel',
   *   qty?:      number,
   *   product?:  'CNC' | 'MIS' | 'NRML',
   *   orderType?:'MARKET' | 'LIMIT' | 'SL' | 'SL-M',
   *   variety?:  'regular' | 'co' | 'bo' | 'amo' | 'iceberg',
   *   price?:    number,
   *   trigger?:  number,
   *   lotSize?:  number,
   *   accounts?: string[],
   *   account?:  string,
   *   orderId?:  string,
   *   defaultMode?:    'draft' | 'paper' | 'live' | 'shadow',
   *   availableModes?: Array<'draft' | 'paper' | 'live' | 'shadow'>,
   *   currentQty?: number,
   *   onSubmit:  (payload: any) => void | Promise<void>,
   *   onClose:   () => void,
   *   onAddToBasket?: ((payload: any) => void) | null,
   *   basketMode?: boolean,
   *   accountHidden?: boolean,
   *   actionsHidden?: boolean,
   *   triggerSubmit?: number,
   *   triggerBasket?: number,
   *   onAccountChange?: (account: string) => void,
   *   onSideChange?: ((side: 'BUY'|'SELL') => void) | null,
   *   hostManagesEsc?: boolean,
   *   onMarginUpdate?: ((preview:any, loading:boolean, meta?: {isCashMode:boolean, cash:number|null, availMargin:number|null, usedMargin:number|null, fundsAccount:string, kind:string, side:string}) => void) | null,
   *   fundsHidden?: boolean,
   *   symbolHidden?: boolean,
   *   symType?: 'ALL' | 'EQ' | 'FUT' | 'OPT',
   *   refreshKey?: number,
   *   mode?:       'draft' | 'paper' | 'live' | 'shadow' | undefined,
   *   chase?:      boolean | undefined,
   *   chaseAgg?:   'low' | 'med' | 'high' | undefined,
   *   modeChaseHidden?: boolean,
   * }} */
  let {
    symbol,
    exchange  = '',
    side      = /** @type {'BUY' | 'SELL'} */ ('BUY'),
    action    = /** @type {'open' | 'close' | 'modify' | 'repeat' | 'cancel'} */ ('open'),
    qty       = 0,
    product   = /** @type {'CNC' | 'MIS' | 'NRML' | undefined} */ (undefined),
    orderType = /** @type {'MARKET' | 'LIMIT' | 'SL' | 'SL-M'} */ ('LIMIT'),
    variety   = /** @type {'regular' | 'co' | 'bo' | 'amo' | 'iceberg'} */ ('regular'),
    price     = /** @type {number | undefined} */ (undefined),
    trigger   = /** @type {number | undefined} */ (undefined),
    lotSize   = 0,
    accounts  = /** @type {string[]} */ ([]),
    account   = '',
    // Existing-order id — required for action='modify'/'cancel' so
    // the submit path knows which order to mutate. Ignored for
    // action='open'.
    orderId   = '',
    // Initial mode pill the ticket opens on. Surfaces with no drafts
    // concept (PerformancePage row click) typically pass 'paper';
    // surfaces with a drafts panel (admin/options) keep 'draft'.
    defaultMode    = /** @type {'draft' | 'paper' | 'live' | 'shadow'} */ ('live'),
    // Which mode pills the operator can see. PAPER is no longer a
    // user-facing choice — on dev all orders are paper-only via the
    // branch gate; on prod the per-action execution.live.* flags
    // decide paper vs live for each action. Operators pick between
    // DRAFT (page-local what-if) and LIVE (submit to backend).
    availableModes = /** @type {Array<'draft'|'paper'|'live'|'shadow'>} */ (['draft', 'live']),
    // Signed qty of the operator's existing position when the ticket
    // is opened from a position-row click. Drives the side toggle's
    // ADD/CLOSE labels — operator thinks in "I want to add to this
    // position" or "I want to close this position", not "I want to
    // BUY or SELL". The bottom submit button still shows the resolved
    // BUY/SELL so the actual broker action is unambiguous.
    //   currentQty > 0  → existing LONG  ⇒ BUY pill = ADD,  SELL = CLOSE
    //   currentQty < 0  → existing SHORT ⇒ SELL pill = ADD, BUY  = CLOSE
    //   currentQty == 0 → no existing position ⇒ plain BUY / SELL labels
    currentQty = 0,
    onSubmit,
    onClose,
    // Pushed back to OrderEntryShell when the operator picks a
    // different account from this tab's selector. Shell uses it to
    // sync the other tabs (command / chain) to the same account so
    // they all submit through the same Kite handle.
    onAccountChange = /** @type {((account: string) => void) | null} */ (null),
    // Fired whenever the operator flips the internal BUY/SELL toggle.
    // SymbolPanel uses this to keep its _modalSide (which drives the
    // common-footer submit button's adaptive label) in sync.
    onSideChange = /** @type {((side: 'BUY'|'SELL') => void) | null} */ (null),
    // Optional "+ Basket" callback — when set, the ticket renders
    // an "Add to basket" action alongside the primary Submit. The
    // caller pushes the leg into its own basket panel (same pill
    // shape as a quick-add). Set to null/undefined to hide.
    onAddToBasket = /** @type {((payload: any) => void) | null} */ (null),
    // When true (shell's Chain tab is active), the primary Submit
    // button reads "Add to basket" and routes through onAddToBasket
    // instead of firing to the backend. The ticket stays open after
    // add so the operator can adjust the leg and add another.
    basketMode = false,
    // When true, the ticket's internal Account picker is suppressed.
    // Used by host pages that expose a shared account at the page /
    // shell level (e.g. /orders Order Entry card header) so the
    // operator only sees one Account chooser. `_account` still binds
    // to the prop — only the picker chrome is hidden.
    accountHidden = false,
    // When true, the ticket's internal action buttons (Exit /
    // +Basket / Submit) are suppressed. Used when the host renders
    // a common action footer at the page level; the host increments
    // `triggerSubmit` / `triggerBasket` props to fire the actions.
    actionsHidden = false,
    // Counter props — each increment fires the corresponding
    // internal handler. Pattern: host calls `count++`, the $effect
    // here observes the delta and dispatches.
    triggerSubmit = 0,
    triggerBasket = 0,
    // When true, OrderTicket skips its own Escape keydown listener.
    // SymbolPanel already attaches a window-level Esc → onClose handler;
    // without this gate both fire when the ticket is embedded inside the
    // panel, producing a latent double-close race.
    hostManagesEsc = false,
    // Fires whenever the margin preview state updates. Host (SymbolPanel
    // modal) uses this to render the same MARGIN/Avail/After/Short row
    // inside its common action footer so the operator sees the verdict
    // regardless of which tab is active.
    onMarginUpdate = /** @type {((preview:any, loading:boolean, meta?:{isCashMode:boolean,cash:number|null,availMargin:number|null,usedMargin:number|null,fundsAccount:string,kind:string,side:string}) => void) | null} */ (null),
    // When true, the in-ticket per-account funds line is suppressed.
    // The order modal sets this so the funds row only renders once in
    // the common action footer (visible on every tab).
    fundsHidden = false,
    // Increments any time the host wants the ticket to refresh its
    // live data — depth ladder, margin preflight, funds. Used by the
    // order modal on tab-activation so switching FROM Chain back TO
    // Ticket re-fetches the depth (operator request: "when order
    // ticket is clicked, market depth and other details need to be
    // refreshed").
    refreshKey = 0,
    // When true, the Symbol chip in the quick-row top strip is
    // suppressed. Used by the order modal where the picker row above
    // the tabs already shows the symbol — no need to repeat it inside
    // the Ticket form. Same semantic as accountHidden but for the
    // symbol affordance.
    symbolHidden = false,
    // Lifted mode / chase / chase-aggressiveness controls. When the
    // host (SymbolPanel) renders these in its shared toolbar, it
    // binds the three props here AND passes `modeChaseHidden=true`
    // to suppress the in-ticket .ot-mode-row. Either prop being
    // undefined keeps the standalone behaviour — internal state
    // takes over and the row renders. Operator: "mode, chase,
    // margin should be common for chase and order ticket".
    mode      = $bindable(/** @type {'draft'|'paper'|'live'|'shadow'|undefined} */ (undefined)),
    chase     = $bindable(/** @type {boolean|undefined} */ (undefined)),
    chaseAgg  = $bindable(/** @type {'low'|'med'|'high'|undefined} */ (undefined)),
    modeChaseHidden = false,
    // Instrument-type intent from the picker row (Equity / Future /
    // Option / ALL). When the operator has chosen FUT or OPT but the
    // symbol prop is just a bare underlying (NIFTY rather than
    // NIFTY26JUNFUT), we surface inline pickers that build the full
    // tradingsymbol. ALL = no constraint (legacy behaviour).
    symType = /** @type {'ALL'|'EQ'|'FUT'|'OPT'} */ ('ALL'),
  } = $props();

  // Derived label map for the side toggle. Keeps the actual _side
  // state as 'BUY' / 'SELL' (the broker payload never changes); only
  // the display label flips between BUY/SELL and ADD/CLOSE.
  const sideLabels = $derived.by(() => {
    if (!currentQty || currentQty === 0) {
      return { BUY: 'BUY', SELL: 'SELL' };
    }
    if (currentQty > 0) {
      // Long position: buying more = ADD, selling = CLOSE.
      return { BUY: 'ADD', SELL: 'CLOSE' };
    }
    // Short position: selling more = ADD, buying back = CLOSE.
    return { BUY: 'CLOSE', SELL: 'ADD' };
  });

  // Derived instrument kind. Reads suffix from either the resolved
  // tradingsymbol (preferred — set when bare-mode pickers built one)
  // or the raw symbol prop. When the operator picked the picker-row
  // Type filter (symType=FUT/OPT) but no resolved symbol exists yet,
  // the kind falls back to OPT (CE as default) / FUT so the lots
  // input + product defaults render correctly during the picker
  // interaction.
  const kind = $derived.by(() => {
    const resolved = (typeof _resolvedSymbol === 'string' ? _resolvedSymbol : '').toUpperCase();
    const raw = (symbol || '').toUpperCase();
    const s = resolved || raw;
    if (/CE$/.test(s)) return 'CE';
    if (/PE$/.test(s)) return 'PE';
    if (/FUT$/.test(s)) return 'FUT';
    // Check the instruments cache: if the symbol is a known equity
    // row (RELIANCE / INFY / TCS etc.), force EQ regardless of the
    // picker row's Type filter intent. Operator who scoped Type=OPT
    // and then picked an equity by accident gets EQ behaviour, not
    // option-mode with a missing strike. (Operator: "lots does not
    // get updated as qty as it was a stock".)
    if (s) {
      const inst = getInstrument(s);
      if (inst?.t === 'EQ') return 'EQ';
    }
    if (symType === 'OPT') return _pickedOptType || 'CE';
    if (symType === 'FUT') return 'FUT';
    return 'EQ';
  });
  const isOption = $derived(kind === 'CE' || kind === 'PE');
  const isFuture = $derived(kind === 'FUT');
  const isEquity = $derived(kind === 'EQ');

  // Bare-underlying mode — symbol is just the underlying name
  // ("NIFTY") and symType says the operator wants FUT or OPT. We
  // surface inline pickers (expiry, plus strike + CE/PE for OPT) to
  // build the full tradingsymbol. Driven off the picker row's
  // Type filter; defaults to off when symbol already carries a
  // FUT/CE/PE suffix or when symType is ALL/EQ.
  const _underlying = $derived((symbol || '').toUpperCase());
  const _hasContractSuffix = $derived(/(FUT|CE|PE)$/.test(_underlying));
  const _wantsFut = $derived(symType === 'FUT' && !_hasContractSuffix && !!_underlying);
  const _wantsOpt = $derived(symType === 'OPT' && !_hasContractSuffix && !!_underlying);
  const _isBareUnderlying = $derived(_wantsFut || _wantsOpt);

  // Inline-picker state. Seeded lazily on first bare-mode entry via
  // an effect below so the operator sees a sensible default
  // (nearest expiry / ATM strike) rather than empty controls.
  let _pickedExpiry = $state('');
  let _pickedStrike = $state(/** @type {number|''} */ (''));
  let _pickedOptType = $state(/** @type {'CE'|'PE'} */ ('CE'));

  // Expiry choices for the bare-underlying picker. For OPT pulls
  // distinct expiries from the CE side (symmetric with PE); for FUT
  // pulls distinct expiries from the futures rows.
  const _expiryChoices = $derived.by(() => {
    if (!_underlying) return [];
    if (_wantsFut) {
      return listFutures(_underlying).map(r => r.x).filter(Boolean);
    }
    if (_wantsOpt) {
      return listExpiries(_underlying, 'CE');
    }
    return [];
  });
  const _strikeChoices = $derived.by(() => {
    if (!_wantsOpt || !_pickedExpiry) return [];
    return listStrikes(_underlying, _pickedOptType, _pickedExpiry);
  });

  // Clear stale picks when the underlying changes — without this,
  // switching from NIFTY to BANKNIFTY leaves _pickedExpiry/_pickedStrike
  // holding values from the old underlying and the resolved symbol
  // either fails to build or builds an invalid contract.
  let _prevUnderlying = '';
  $effect(() => {
    if (_underlying !== _prevUnderlying) {
      _prevUnderlying = _underlying;
      untrack(() => {
        _pickedExpiry = '';
        _pickedStrike = '';
      });
    }
  });

  // Auto-seed defaults when the operator enters bare-underlying mode
  // OR when the expiry/strike lists materialise after the instruments
  // cache warms. Idempotent — only fills empty values, never overrides
  // an operator pick.
  $effect(() => {
    if (!_isBareUnderlying) return;
    if (!_pickedExpiry && _expiryChoices.length) {
      _pickedExpiry = _expiryChoices[0];
    }
    if (_wantsOpt && !_pickedStrike && _strikeChoices.length) {
      // ATM-ish proxy: middle strike. Real ATM needs spot LTP; the
      // Chain tab does that properly. Mid-strike is fine for the
      // ticket's defaults.
      _pickedStrike = _strikeChoices[Math.floor(_strikeChoices.length / 2)];
    }
  });

  // Resolved tradingsymbol — derived from the bare-mode picks. When
  // not in bare mode, falls through to the symbol prop unchanged.
  // Returns null when the picks haven't resolved yet so the form
  // disables submit while the operator finishes choosing.
  const _resolvedSymbol = $derived.by(() => {
    if (!_isBareUnderlying) return symbol;
    if (!_pickedExpiry) return null;
    if (_wantsFut) {
      const fut = findNearestFuture(_underlying);
      // findNearestFuture returns the nearest; we want the one
      // matching _pickedExpiry. Walk the list.
      const list = listFutures(_underlying);
      const match = list.find(r => r.x === _pickedExpiry) || fut;
      return match?.s || null;
    }
    if (_wantsOpt) {
      if (!_pickedStrike) return null;
      const opt = findOption(_underlying, _pickedOptType, Number(_pickedStrike), _pickedExpiry);
      return opt?.s || null;
    }
    return symbol;
  });

  // Resolve the exchange via the instruments cache. The order modal's
  // OrderDepth poll needs `?exchange=…` to hit the right Kite quote
  // endpoint — MCX commodities need exchange=MCX, NSE F&O needs NFO,
  // etc. The instruments cache IS authoritative: PageHeaderActions
  // passes a generic 'NSE' default that's wrong for MCX commodities,
  // so we look up the resolved symbol's actual exchange first and
  // only fall back to the caller's hint when the cache lookup misses.
  const _resolvedExchange = $derived.by(() => {
    const sym = String(_resolvedSymbol || symbol || '').toUpperCase();
    if (sym) {
      const inst = getInstrument(sym);
      if (inst?.e) return inst.e;
    }
    return exchange || '';
  });

  // Default product based on instrument when caller didn't specify.
  const productVal = $derived(product ?? (isEquity ? 'CNC' : 'NRML'));
  const productOptions = $derived(isEquity
    ? ['CNC', 'MIS']
    : ['NRML', 'MIS']);

  // Local form state — start from prop defaults, then operator edits.
  let _side    = $state(side);
  // Re-sync the internal side state when the parent updates the
  // `side` prop. Without this, the modal's "BUY / SELL" footer
  // buttons can change _modalSide → propagate via the side prop, but
  // OrderTicket would still submit whichever side it last held
  // locally. action='modify' freezes the side per the existing rule.
  $effect(() => {
    if (action === 'modify') return;
    if (side && side !== untrack(() => _side)) _side = side;
  });

  // Resolved lot size — starts from the prop; may be updated on mount
  // via the instruments cache when the caller didn't supply one.
  let _lotSize = $state(lotSize);

  // Qty path:
  //   - _lotSize > 0  → operator edits in LOTS via [−] [N] [+], the
  //     resolved qty `_lots * _lotSize` flows into _qty. Mirrors the
  //     chain picker so both surfaces read consistently.
  //   - _lotSize == 0 → cash equity / no lot concept; fall back to
  //     raw number input bound directly to _qty.
  // Initial _lots comes from the caller-supplied qty (rounded to the
  // nearest whole lot, floored at 1). When qty is also 0 we start at
  // 1 lot so the ticket opens ready-to-submit.
  let _lots = $state(
    _lotSize > 0
      ? Math.max(1, Math.round((Number(qty) || _lotSize) / _lotSize))
      : 1
  );
  let _qty     = $state(qty || _lotSize || (isEquity ? 1 : 0));
  // Track whether the operator has touched _lots manually. If they
  // haven't, late-resolving _lotSize (instrument cache warming after
  // ticket mount) should recompute _lots from the original `qty` prop
  // so the ticket displays "2 Lots" instead of staying stuck at the
  // fallback "1 Lot". Set true by the +/- steppers + direct input.
  let _lotsTouched = $state(false);
  // When _lotSize transitions from 0 → positive (instrument cache
  // resolved after mount), recompute _lots from the caller's original
  // qty prop. Skipped once the operator has manually adjusted.
  let _prevLotSize = _lotSize;
  $effect(() => {
    if (_prevLotSize === 0 && _lotSize > 0 && !_lotsTouched) {
      _lots = Math.max(1, Math.round((Number(qty) || _lotSize) / _lotSize));
    }
    _prevLotSize = _lotSize;
  });
  // Keep _qty in sync with _lots × _lotSize so submit + validation see
  // the resolved raw quantity. Skipped when _lotSize=0 (operator types
  // qty directly).
  $effect(() => {
    if (_lotSize > 0) _qty = _lots * _lotSize;
  });

  // Re-derive _lotSize when the bare-mode picker builds a new
  // tradingsymbol. Without this, the operator picks NIFTY → expiry +
  // strike + CE → resolved = NIFTY26JUN22000CE — but _lotSize stays
  // at whatever was passed in via the prop (often 0 because the
  // caller didn't know the lot yet). Pulls from the instruments cache.
  $effect(() => {
    const r = _resolvedSymbol;
    if (!r || typeof r !== 'string') return;
    const up = r.toUpperCase();
    // Pattern-only equity check — if the resolved symbol has no
    // derivative suffix (CE/PE/FUT) AND no expiry-style 2-digit-year
    // segment, treat it as a non-derivative (stock / ETF / index) and
    // force lot=0 so the template renders bare Qty. This catches the
    // common case operator picks Symbol Type=OPT then types RELIANCE
    // by mistake — kind detection via getInstrument() may not yet
    // have cache data, but the suffix check is always reliable.
    const looksLikeDerivative =
      /CE$|PE$|FUT$/.test(up) || /\d{2}[A-Z]{3}/.test(up);
    if (isEquity || !looksLikeDerivative) {
      if (_lotSize !== 0) untrack(() => { _lotSize = 0; });
      return;
    }
    const inst = getInstrument(up);
    const ls = Number(inst?.ls) || 0;
    if (ls > 0 && ls !== _lotSize) {
      untrack(() => { _lotSize = ls; });
    }
  });

  // When the operator flips side, reset to 1 lot (ADD direction) or
  // restore the held qty (CLOSE direction). "ADD" = same direction as
  // the existing position; "CLOSE" = opposite.
  let _prevSide = _side;
  $effect(() => {
    if (!currentQty || currentQty === 0 || _lotSize <= 0) return;
    const cur = _side;
    if (cur === _prevSide) return;
    _prevSide = cur;
    const isAdd = (currentQty > 0 && cur === 'BUY') ||
                  (currentQty < 0 && cur === 'SELL');
    if (isAdd) {
      _lots = 1;
    } else {
      _lots = Math.max(1, Math.round(Math.abs(currentQty) / _lotSize));
    }
  });

  function stepLots(/** @type {number} */ delta) {
    _lots = Math.max(1, Math.floor((Number(_lots) || 1) + delta));
    _lotsTouched = true;  // operator owns _lots from here
  }

  /**
   * Reset the ticket form to safe defaults. Replaces the legacy Exit
   * button — operator: "there is not exit button required. probably
   * clear button required". Wipes price/trigger overrides, resets lots
   * to 1, restores side/order-type to the props the host passed, and
   * forgets any submit success / error state. The modal close (×) on
   * the SymbolPanel header handles dismissal; Clear keeps the operator
   * in the ticket with a clean slate.
   */
  function clearForm() {
    _lots = _lotSize > 0 ? 1 : 1;
    _lotsTouched = false;
    _qty = _lotSize > 0 ? _lots * _lotSize : (isEquity ? 1 : 0);
    _price = '';
    _trigger = '';
    _priceTouched = false;
    _side = side;
    _type = orderType;
    _variety = variety;
    _validity = 'DAY';
    _product = productVal;
    _setChase(true);
    _setChaseAgg('low');
    submitErr = '';
    submitOk = '';
    // _shownErr is a $derived from `_submitTried && validationErr`; flip
    // _submitTried back to false so the inline validation error chip
    // clears alongside the form fields.
    _submitTried = false;
  }
  let _type    = $state(orderType);
  let _variety = $state(variety);
  // Validity (Time-in-Force): DAY by default. IOC (Immediate-Or-Cancel)
  // for fast-trading scenarios where partial fills are acceptable but
  // unfilled remainder should drop instead of resting. Kite supports
  // DAY + IOC; TTL/GTT live on different code paths so they're not
  // exposed here. Industry analogue: every order book (IB TWS, ToS,
  // Kite Web) surfaces DAY/IOC as inline pills.
  let _validity = $state('DAY');
  let _price   = $state(price ?? '');
  let _trigger = $state(trigger ?? '');

  // ── Target take-profit ───────────────────────────────────────────────
  // Two modes: % (default) or ₹. Default pct seeded from the
  // `algo.default_target_pct` setting fetched on mount. Operator
  // can toggle via the pill next to the field.
  // `_targetMode` drives which field is shown and which payload field
  // is populated; the other is set to null.
  let _targetMode = $state(/** @type {'pct' | 'abs'} */ ('pct'));
  let _targetPct = $state(/** @type {number} */ (30));
  let _targetAbs = $state(/** @type {number|''} */ (''));

  // Derived payload values — null when empty so the backend can
  // distinguish "not set" from zero.
  const _targetPctVal = $derived(
    _targetMode === 'pct' && Number(_targetPct) > 0 ? Number(_targetPct) / 100 : null
  );
  const _targetAbsVal = $derived(
    _targetMode === 'abs' && Number(_targetAbs) > 0 ? Number(_targetAbs) : null
  );
  let _product = $state(productVal);
  // Initial mode:
  //   1. defaultMode prop wins when the caller explicitly picked one
  //      AND it's in availableModes.
  //   2. Otherwise fall back to the global executionMode store
  //      (operator's last-used mode — synced from /admin/live toggle
  //      in a follow-up). The store normalises shadow/sim/replay to
  //      'paper' for surfaces that only expose draft/paper/live pills.
  //   3. Last resort: first available mode.
  function _resolveInitialMode() {
    const normalise = (/** @type {string} */ m) => {
      if (m === 'sim' || m === 'replay') return 'paper';
      return /** @type {'draft'|'paper'|'live'|'shadow'} */ (m);
    };
    if (availableModes.includes(/** @type {any} */ (defaultMode))) return defaultMode;
    const fromStore = normalise(get(executionMode) || 'paper');
    if (availableModes.includes(/** @type {any} */ (fromStore))) return fromStore;
    return availableModes[0] || 'draft';
  }
  // Mode/chase/chaseAgg state. Host (SymbolPanel) may supply the
  // matching bindable props to lift the controls into a shared
  // toolbar; otherwise we own them internally. `_setMode/_setChase
  // /_setChaseAgg` funnel writes to both the bound prop AND the
  // internal backing so each side stays in sync without an explicit
  // round-trip.
  let _modeInternal     = $state(/** @type {'draft'|'paper'|'live'} */ (untrack(_resolveInitialMode)));
  let _chaseInternal    = $state(true);
  let _chaseAggInternal = $state(/** @type {'low'|'med'|'high'} */ ('low'));
  const _mode     = $derived(mode      !== undefined ? mode      : _modeInternal);
  const _chase    = $derived(chase     !== undefined ? chase     : _chaseInternal);
  const _chaseAgg = $derived(chaseAgg  !== undefined ? chaseAgg  : _chaseAggInternal);
  function _setMode(/** @type {'draft'|'paper'|'live'} */ v) {
    _modeInternal = v;
    if (mode !== undefined) mode = v;
  }
  function _setChase(/** @type {boolean} */ v) {
    _chaseInternal = v;
    if (chase !== undefined) chase = v;
  }
  function _setChaseAgg(/** @type {'low'|'med'|'high'} */ v) {
    _chaseAggInternal = v;
    if (chaseAgg !== undefined) chaseAgg = v;
  }

  // Auto-fill plumbing — the OrderDepth child polls the quote
  // every 1.2 s and bubbles each fresh response here via
  // onDepthQuote. We pre-fill the limit price with the marketable
  // side (BUY → ask, SELL → bid) so the operator doesn't have to
  // type a price every time. Once the operator types into the
  // field, `_priceTouched` flips true and we stop overwriting
  // their input. Flipping side resets to the new marketable side
  // unless they've typed.
  // Caller can pre-supply `price` to suppress auto-fill (e.g. a
  // close-position flow that wants the operator's last limit).
  // Untrack the read so we capture the initial value once — this
  // is intentional, the operator's edits flip the flag from there.
  let _priceTouched = $state(untrack(() => typeof price === 'number' && price > 0));
  /** @type {{ bid: number|null, ask: number|null, ltp: number|null } | null} */
  let _lastQuote = $state(null);

  // Tick size for NSE F&O / equity is ₹0.05; commodities are
  // typically ₹0.05 or coarser (CRUDEOIL ₹1.00, GOLDM ₹1.00). Kite
  // rejects orders whose price isn't an exact tick multiple — the
  // bid/ask from depth ARE tick-aligned, but JS floating-point can
  // turn 590.80 into 590.7999999999999 which Kite then refuses.
  // Snap to the nearest 0.05 + round to 2 decimals to scrub away
  // both float artifacts and any operator-typed extra decimals.
  function _roundToTick(/** @type {number|string} */ px,
                        /** @type {number} */ tick = 0.05) {
    const n = Number(px);
    if (!Number.isFinite(n) || n <= 0) return n;
    return Math.round((n / tick) + Number.EPSILON) * tick;
  }
  function _formatTick(/** @type {number} */ n) {
    // Always render with 2 decimals for paise-aligned ticks. Doesn't
    // round (caller did that already); just stringifies cleanly.
    return Number.isFinite(n) ? Number(n.toFixed(2)) : n;
  }
  function _autoFillFromQuote() {
    if (_priceTouched) return;
    if (_type !== 'LIMIT' && _type !== 'SL') return;
    if (!_lastQuote) return;
    const px = _side === 'BUY' ? _lastQuote.ask : _lastQuote.bid;
    // Fall back to LTP when the corresponding side has no depth
    // (off-hours, illiquid contracts) so the operator isn't left
    // with a blank field.
    const fallback = (px && px > 0) ? px : _lastQuote.ltp;
    if (fallback && fallback > 0) _price = _formatTick(_roundToTick(fallback));
  }
  function onDepthQuote(/** @type {any} */ q) {
    _lastQuote = q ? {
      bid: q.bid ?? null,
      ask: q.ask ?? null,
      ltp: q.ltp ?? null,
    } : null;
    _autoFillFromQuote();
  }
  // Re-fill when the operator flips BUY ⇄ SELL or changes order
  // type (LIMIT ⇄ MARKET ⇄ SL — only LIMIT/SL show the price
  // field, but the helper guards that).
  $effect(() => {
    void _side; void _type;
    _autoFillFromQuote();
  });

  // Self-fetched real account list — backstop for when the caller
  // didn't (or couldn't) supply one. /api/accounts/ is jwt-guarded
  // but doesn't mask, so any signed-in user gets real account_ids
  // even if the page's positions came back masked.
  /** @type {string[]} */
  let _selfAccounts = $state([]);

  // Per-account funds — used to render the "Avail margin" pill next to
  // the account picker so the operator can see whether the chosen
  // account has enough room to place this order. Populated lazily when
  // the modal mounts (PAPER / LIVE submits need it; DRAFT doesn't but
  // the cost is one cached fetch). Each row carries:
  //   { account, cash, avail_margin, used_margin, collateral }
  /** @type {Array<{account:string, cash:number, avail_margin:number,
   *                used_margin:number, collateral:number}>} */
  let _funds = $state([]);
  // Pick the funds row to surface in the pill. Order:
  //   1. exact match on the currently-picked account (most useful)
  //   2. summed totals across every loaded fund row (fallback when
  //      the operator hasn't picked an account yet, or when /accounts
  //      hasn't resolved so the picker is empty)
  //   3. null — funds payload empty (401 / 403 / fetch failure)
  // The earlier "exact match only" rule made the pill silently
  // disappear whenever the account picker collapsed (single-acct
  // case OR demo session before sign-in), even though we already
  // had per-account funds in memory. Falling back to the summed
  // view keeps the operator informed in those edge cases.
  const _accountFunds = $derived.by(() => {
    if (!_funds.length) return null;
    if (_account) {
      const match = _funds.find(r => r.account === _account);
      if (match) return match;
    }
    let cash = 0, am = 0, um = 0, col = 0;
    for (const f of _funds) {
      cash += Number(f?.cash || 0);
      am   += Number(f?.avail_margin || 0);
      um   += Number(f?.used_margin  || 0);
      col  += Number(f?.collateral   || 0);
    }
    return {
      account:      'TOTAL',
      cash, avail_margin: am, used_margin: um, collateral: col,
    };
  });

  // Account list shown by the picker — caller's `accounts` prop
  // wins when populated; otherwise we use whatever we self-fetched.
  // A masked-only list (e.g. ZG####) is treated as empty so we
  // don't pre-pick an unroutable value.
  function _isRealAcct(/** @type {string|null|undefined} */ a) {
    return !!(a && !String(a).includes('#'));
  }
  const _accounts = $derived.by(() => {
    const fromProp = (accounts || []).filter(_isRealAcct);
    if (fromProp.length) return fromProp;
    return _selfAccounts.filter(_isRealAcct);
  });

  // Account — explicit operator choice for which Kite handle the
  // order routes through. Required for PAPER and LIVE; ignored in
  // DRAFT. Initialized empty and reactively seeded from the prop /
  // picker via the effect below — this lets a late-arriving caller
  // account list (the common race: /api/accounts/ resolves AFTER
  // the operator clicks +) auto-select once it lands. A masked
  // ZG#### is unroutable so we never seed it as a default.
  let _account = $state('');

  // Reactive seed:
  //   1. Caller-supplied `account` prop wins when it's a real value.
  //   2. Otherwise, single real account in the picker → pre-pick it.
  //   3. If the seeded value disappears from the picker (caller
  //      flips symbol / picker reloads), reset.
  $effect(() => {
    const propPick = _isRealAcct(account) ? String(account) : '';
    // Account-resolution ladder (top wins):
    //   1. Caller-supplied `account` prop — context from the host page
    //      (e.g. clicked Edit on a specific row's order).
    //   2. orders.default_account setting (operator's primary account,
    //      ZG0790 by default) when it's in the loaded picker list.
    //   3. Sole loaded account when the list has exactly one entry.
    // Empty when none resolves so the operator can pick manually.
    if (!_account && propPick) {
      _account = propPick;
      return;
    }
    if (!_account && _accounts.length) {
      const defaultAcct = getDefaultAccount();
      if (defaultAcct && _accounts.includes(defaultAcct)) {
        _account = defaultAcct;
        return;
      }
      if (_accounts.length === 1) {
        _account = _accounts[0];
        return;
      }
    }
    if (_account && _accounts.length && !_accounts.includes(_account)) {
      // Re-check against propPick first so a late-arriving list that
      // confirms the prop doesn't clear it.
      const defaultAcct = getDefaultAccount();
      _account = propPick
        || (defaultAcct && _accounts.includes(defaultAcct) ? defaultAcct : '')
        || (_accounts.length === 1 ? _accounts[0] : '');
    }
  });

  // Push picker changes back to the shell so the other tabs (command /
  // chain) re-sync. Guard against the no-op echo (when shell pushes
  // a new prop value, our $effect above sets `_account` to match,
  // which would otherwise fire onAccountChange in a loop). Untrack the
  // callback read.
  let _lastNotifiedAcct = '';
  $effect(() => {
    if (_account && _account !== _lastNotifiedAcct && _account !== account) {
      _lastNotifiedAcct = _account;
      onAccountChange?.(_account);
    } else if (_account === account) {
      _lastNotifiedAcct = _account;
    }
  });

  // Field visibility derived from order type + variety.
  const showLimit   = $derived(_type === 'LIMIT' || _type === 'SL');
  const showTrigger = $derived(_type === 'SL' || _type === 'SL-M');

  // Validation — applied client-side; backend validates again before
  // hitting the broker. Lot-size check protects against rejections
  // for non-multiple quantities (NIFTY lot 50, BANKNIFTY 15, etc.).
  const validationErr = $derived.by(() => {
    if (!Number(_qty) || Number(_qty) <= 0) {
      if (_lotSize > 0) return `Qty required (1 lot = ${_lotSize} for ${symbol})`;
      return 'Qty required';
    }
    if (_lotSize > 0 && Number(_qty) % _lotSize !== 0) {
      return `Qty must be a multiple of lot ${_lotSize}`;
    }
    if (showLimit   && !Number(_price))   return 'Limit price required';
    if (showTrigger && !Number(_trigger)) return 'Trigger price required';
    if (Number(_price) < 0)   return 'Price must be ≥ 0';
    if (Number(_trigger) < 0) return 'Trigger must be ≥ 0';
    if ((_mode === 'paper' || _mode === 'live') && !_account) {
      return 'Pick an account';
    }
    return '';
  });

  /** Build a basket-leg payload from the modal's current state.
   *  Caller (admin/options) folds this into chainBasket so the leg
   *  renders as a regular basket pill alongside quick-adds. */
  function _basketPayload() {
    return {
      side:       _side,
      sym:        symbol,
      exchange:   exchange || 'NFO',
      account:    _account,
      lots:       Math.max(1, Number(_lots) || 1),
      lotSize:    Number(_lotSize) || 1,
      product:    _product,
      limit:      showLimit ? Number(_roundToTick(_price)) || 0 : 0,
      chaseAgg:   showLimit && _chase ? _chaseAgg : 'low',
      target_pct: _targetPctVal,
      target_abs: _targetAbsVal,
    };
  }

  // Counter-prop dispatch — host pages that render their own action
  // footer (via `actionsHidden`) bump `triggerSubmit++` and
  // `triggerBasket++` to fire the internal handlers without needing
  // a function-ref binding. The last-seen counter is tracked so the
  // initial render doesn't auto-fire on mount.
  let _lastSubmitTrigger = $state(/** @type {number} */ (-1));
  let _lastBasketTrigger = $state(/** @type {number} */ (-1));
  $effect(() => {
    if (triggerSubmit !== _lastSubmitTrigger && _lastSubmitTrigger >= 0) {
      submit();
    }
    _lastSubmitTrigger = triggerSubmit;
  });
  $effect(() => {
    if (triggerBasket !== _lastBasketTrigger && _lastBasketTrigger >= 0) {
      addToBasket();
    }
    _lastBasketTrigger = triggerBasket;
  });

  function addToBasket() {
    if (!onAddToBasket) return;
    _submitTried = true;
    if (validationErr) return;
    onAddToBasket(_basketPayload());
    // Operator: "when i press +basket in order ticket, the modal is
    // disappearing which is not correct. it should behave like
    // chain". Add-to-basket is a STAGING action — the operator
    // expects to keep adding legs (matching the Chain tab's
    // +CE/+PE buttons which never close). Removed onClose(); the
    // shell's basket bar shows the added leg + Submit/Clear still
    // sits in the common footer.
  }

  let submitting = $state(false);
  // Suppress the validation error banner until the operator actually
  // tries to submit / add to basket. Prevents "Limit price required"
  // (or any other "this field is empty" message) flashing on a fresh
  // ticket form before the operator has had a chance to fill anything
  // in. Submit / Add-to-basket flip this true; Clear flips it back.
  let _submitTried = $state(false);
  const _shownErr = $derived(_submitTried ? validationErr : '');
  /** @type {string} */ let submitErr = $state('');

  // ── Margin / cash preview ────────────────────────────────────────
  // Calls /api/orders/preflight on field change (debounced 350 ms) so
  // the operator sees the SPAN + premium / cash cost of the order BEFORE
  // they click Submit. Mirrors IB TWS's "Order Confirmation / what-if"
  // preview and NinjaTrader's buying-power panel — standard for any
  // trading desk where margin gates the order. Backend reuses the same
  // preflight that the live-order submit path runs, so the numbers the
  // operator sees here are the same numbers the broker will charge.
  let _marginPreview = $state(/** @type {any} */ (null));
  let _marginLoading = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _marginTimer = null;

  // Chip-meta carries the leg-classification + the FULL funds row the
  // host (SymbolPanel) needs to render both:
  //   1. the per-order Cost/Cash vs Req/Avail chip (was already there)
  //   2. the modal-wide funds summary line ("Avail margin · Cash ·
  //      Used") above the action footer — operator request: the
  //      TOTAL/per-account funds line was inside OrderTicket only, so
  //      it was hidden on the Chain tab. Surfacing it via the host
  //      keeps it visible across every modal tab.
  const _chipMeta = $derived.by(() => {
    const isCashMode =
      isEquity ||
      ((kind === 'CE' || kind === 'PE') && _side === 'BUY');
    return {
      isCashMode,
      cash:         _accountFunds ? Number(_accountFunds.cash || 0)         : null,
      availMargin:  _accountFunds ? Number(_accountFunds.avail_margin || 0) : null,
      usedMargin:   _accountFunds ? Number(_accountFunds.used_margin || 0)  : null,
      fundsAccount: _accountFunds ? String(_accountFunds.account || '')     : '',
      kind,
      side: _side,
      // Surfacing _type lets the shell hide chase pills for MARKET
      // and SL-M (operator: "chase needs to be active for limit or
      // stop limit. stop limit market does not need chase").
      orderType: _type,
    };
  });
  // Re-emit chip meta on every state change so the host chip
  // re-renders even when the margin preview itself hasn't changed
  // (e.g. operator flips BUY→SELL on an option without retyping qty).
  // Dedupe via JSON-string ref-equality so identical chipMeta /
  // preview / loading triples don't churn the host's reactive
  // graph on every keystroke; audit defect #14.
  let _lastEmittedKey = '';
  $effect(() => {
    void _chipMeta;
    const key = JSON.stringify({
      m: _marginPreview, l: _marginLoading, c: _chipMeta,
    });
    if (key === _lastEmittedKey) return;
    _lastEmittedKey = key;
    onMarginUpdate?.(_marginPreview, _marginLoading, _chipMeta);
  });

  $effect(() => {
    // Track everything that materially affects the basket_margin number.
    // (Svelte 5 picks up reads inside this function automatically.)
    // Host-driven refreshKey is included so tab activation triggers an
    // immediate re-fetch of the margin preview.
    const _watchers = [
      _side, _qty, _account, _product, _type, _variety,
      _price, _trigger, symbol, exchange, _resolvedSymbol, refreshKey,
    ];
    void _watchers;

    if (_marginTimer) {
      clearTimeout(_marginTimer);
      _marginTimer = null;
    }
    // CRIT 3: demo sessions get a 403 from /api/orders/preflight — skip
    // the preview entirely so the modal doesn't spam error entries.
    // Skip when the ticket is incomplete — no point hitting Kite with
    // a half-typed form. Also skip drafts (no broker; cost is the limit
    // × qty multiplication which the operator can already see).
    if (_isDemo || !_account || !symbol || Number(_qty) <= 0 || _mode === 'draft') {
      _marginPreview = null;
      _marginLoading = false;
      onMarginUpdate?.(null, false, _chipMeta);
      return;
    }
    _marginLoading = true;
    onMarginUpdate?.(_marginPreview, true, _chipMeta);
    _marginTimer = setTimeout(async () => {
      try {
        const payload = {
          account: _account,
          tradingsymbol: _resolvedSymbol || symbol,
          exchange: exchange || 'NFO',
          quantity: Number(_qty),
          side: _side,
          order_type: _type,
          product: _product,
          variety: _variety,
          validity: _validity,
          price: showLimit ? Number(_price) || 0 : 0,
          trigger_price: showTrigger ? Number(_trigger) || 0 : 0,
        };
        _marginPreview = await previewOrderMargin(payload);
      } catch (e) {
        _marginPreview = { error: (e?.message || 'preview failed').slice(0, 60) };
      } finally {
        _marginLoading = false;
        onMarginUpdate?.(_marginPreview, false, _chipMeta);
      }
    }, 350);

    return () => {
      if (_marginTimer) {
        clearTimeout(_marginTimer);
        _marginTimer = null;
      }
    };
  });

  // Inline success state — shown briefly inside the modal after a
  // successful PAPER / LIVE submit so the operator sees confirmation
  // before the modal closes. Without it the modal disappears silently
  // and the operator has no idea whether the order actually landed.
  /** @type {string} */ let submitOk = $state('');

  async function submit() {
    _submitTried = true;
    if (validationErr) return;
    // ── action='modify' branch ─────────────────────────────────
    // Modifying an existing working order — bypass the
    // place/ticket pipeline entirely. Calls PUT /api/orders/{id}
    // with whatever fields the operator changed (price, qty,
    // order_type, trigger). Mode pills + chase + L/M/H don't
    // apply (those are place-time concerns). Account, symbol,
    // and side are locked in the UI.
    if (action === 'modify') {
      if (!orderId) {
        submitErr = 'Modify path requires an order id.';
        return;
      }
      submitting = true; submitErr = ''; submitOk = '';
      try {
        const payload = {
          account:       _account,
          quantity:      Number(_qty) || undefined,
          price:         showLimit   ? _roundToTick(_price)   : null,
          trigger_price: showTrigger ? _roundToTick(_trigger) : null,
          order_type:    _type,
          variety:       _variety,
          validity:      _validity,
        };
        await modifyOrder(orderId, payload);
        submitOk = `Order #${orderId} modified`;
        // Surface the diff to the caller so the page can refresh
        // its order list / log the change. Modal stays open with the
        // Exit button until the operator dismisses it (was 1.2 s auto-
        // close; operator couldn't read the confirmation in time).
        await onSubmit({ action: 'modify', orderId, ...payload });
      } catch (e) {
        submitErr = /** @type {any} */ (e)?.message || String(e);
      } finally {
        submitting = false;
      }
      return;
    }

    // LIVE submits straight through — no confirm dialog. The pre-submit
    // margin/cost row above the Submit button already tells the operator
    // exactly what they're committing to. Backend still enforces both
    // gates (prod-branch + execution.paper_trading_mode=False) before
    // any broker call, so accidental clicks on dev or in paper mode
    // can't reach Kite.
    const payload = {
      mode:           _mode,
      action,
      symbol,
      exchange,
      side:           _side,
      quantity:       Number(_qty),
      product:        _product,
      order_type:     _type,
      variety:        _variety,
      validity:       _validity,
      price:          showLimit   ? _roundToTick(_price)   : null,
      trigger_price:  showTrigger ? _roundToTick(_trigger) : null,
      account:        _account,
      // Chase only carries on price-bearing order types; MARKET /
      // SL-M ignore it on the backend, but we still ship the flag
      // so the AlgoOrder row records the intent for replay.
      chase:               showLimit ? _chase : false,
      chase_aggressiveness: showLimit && _chase ? _chaseAgg : 'low',
      target_pct:          _targetPctVal,
      target_abs:          _targetAbsVal,
    };
    submitting = true; submitErr = ''; submitOk = '';
    /** @type {any} */
    let brokerResp = null;
    try {
      // PAPER + LIVE both route through the backend. DRAFT hands off
      // to the caller's onSubmit only (no API call).
      if (_mode === 'paper' || _mode === 'live') {
        brokerResp = await placeTicketOrder({
          mode:             _mode,
          side:             _side,
          tradingsymbol:    _resolvedSymbol || symbol,
          exchange:         exchange || 'NFO',
          quantity:         Number(_qty),
          product:          _product,
          order_type:       _type,
          variety:          _variety,
          validity:         _validity,
          price:            showLimit   ? _roundToTick(_price)   : null,
          trigger_price:    showTrigger ? _roundToTick(_trigger) : null,
          account:          _account,
          chase:                showLimit ? _chase : false,
          chase_aggressiveness: showLimit && _chase ? _chaseAgg : 'low',
        });
        // Show inline confirmation so the operator sees the order
        // landed; modal stays open until the operator clicks Exit.
        // Backend returns {order_id, mode, status, detail} — surface
        // a verbose summary line with side / qty / symbol / price.
        const oid   = brokerResp?.order_id || '?';
        const px    = showLimit && _price ? `@₹${_roundToTick(_price)}` : '@MKT';
        submitOk = (
          `${(_mode || '').toUpperCase()} ${_side} ${_qty} ${symbol} ${px} · ` +
          `#${oid}`
        );
      }
      // Record the symbol + account as the operator's most recent
      // pick so the next /orders or /charts page open lands on
      // them. (Operator: "if any symbol is used in charts or
      // orders ... the symbol should be defaulted to that".)
      try {
        const { setRecentSymbol, setRecentAccount } = await import('$lib/data/accounts');
        if (symbol)   setRecentSymbol(String(symbol));
        if (_account) setRecentAccount(String(_account));
      } catch { /* silent */ }

      // Notify the caller — DRAFT mode appends to drafts[]; PAPER /
      // LIVE let the caller refresh its local view if it wants to.
      // Thread the broker response so the caller can surface order_id /
      // status (e.g. as a non-blocking completion toast) without
      // re-parsing the inline submitOk string.
      await onSubmit({ ...payload, broker_response: brokerResp });
      // DRAFT closes immediately; PAPER / LIVE stay open with an Exit
      // button so the operator can read the placed-order line. The
      // previous auto-close (200 / 600 ms) was racing the operator's
      // gaze — the inline "✓ placed" flash was visible for a heartbeat
      // then gone, leaving them unsure what landed.
      if (_mode === 'draft') {
        onClose();
      }
    } catch (e) {
      submitErr = /** @type {any} */ (e)?.message || String(e);
    } finally {
      submitting = false;
    }
  }

  // Cleanup handle for the conditional Esc listener (CRIT 1).
  /** @type {(() => void) | null} */
  let _escCleanup = null;

  // Esc to close + backstop /api/accounts/ self-fetch. Runs when
  // the caller didn't supply real accounts (the chain picker pre-
  // /accounts/ load, the per-row buttons before the page poll
  // landed, generic order surfaces that don't know about Kite at
  // all). /accounts is jwt-guarded but doesn't mask, so we get the
  // real account_ids for any signed-in operator. 401 / 403 leaves
  // _selfAccounts empty and the picker collapses gracefully.
  onMount(() => {
    // CRIT 1: skip Esc listener when the host (SymbolPanel) already
    // handles it — prevents a double-close race when the ticket is
    // embedded inside a panel that has its own window keydown handler.
    if (!hostManagesEsc) {
      const onKey = (/** @type {KeyboardEvent} */ e) => {
        if (e.key === 'Escape') onClose();
      };
      window.addEventListener('keydown', onKey);
      _escCleanup = () => window.removeEventListener('keydown', onKey);
    }

    // Lot-size auto-fill from instruments cache when the caller
    // didn't supply lotSize (= 0). Equity keeps qty=1 raw; F&O
    // must trade in whole lots. Uses the synchronous getInstrument()
    // which returns null before loadInstruments() resolves — in that
    // case the fallback is 1 and the operator can adjust.
    if (_lotSize === 0 && !isEquity && symbol) {
      const inst = getInstrument(symbol.toUpperCase());
      if (inst?.ls && Number(inst.ls) > 0) {
        const ls = Number(inst.ls);
        _lotSize = ls;
        // Seed _lots = 1 and let the $effect derive _qty.
        _lots = Math.max(1, qty > 0 ? Math.round(qty / ls) : 1);
      }
    }
    // HIGH 4: skip account self-fetch for demo sessions (would 401-spam).
    const propRealCount = (accounts || []).filter(_isRealAcct).length;
    if (!propRealCount && !_isDemo) {
      fetchAccounts()
        .then(/** @param {any} r */ (r) => {
          const list = (r?.accounts || [])
            .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
            .filter(Boolean);
          _selfAccounts = list;
        })
        .catch(() => { /* silent — picker just stays empty */ });
    }
    // Funds — drives the "Avail margin" pill next to the account
    // picker. Cached for 30 s on the backend so re-opening the modal
    // is instant. 401 / 403 (anonymous demo) leaves _funds empty and
    // the pill collapses gracefully.
    _refetchFunds();

    // Seed default target pct from the `algo.default_target_pct` setting.
    // Silent fallback — if the setting is absent the field stays empty.
    if (!_isDemo) {
      fetchSettings()
        .then(/** @param {any} r */ (r) => {
          // fetchSettings returns a raw array (list of SettingInfo objects),
          // not a {settings: [...]} wrapper.
          const row = (Array.isArray(r) ? r : []).find(
            /** @param {any} s */ (s) => s.key === 'algo.default_target_pct'
          );
          const v = row ? Number(row.value) : NaN;
          // DB stores as fraction (0.30); UI displays as percent (30).
          // Fallback to 30 if setting is missing or zero.
          const pct = v > 0 ? +(v * 100).toFixed(2) : 30;
          // Always apply the DB value — it's authoritative.
          // The initial state of 30 is just a synchronous default.
          _targetPct = /** @type {number} */ (pct);
        })
        .catch(() => {
          // Silent fetch failure — initial default of 30 already set; no-op.
        });
    }

    return () => _escCleanup?.();
  });

  function _refetchFunds() {
    fetchFunds()
      .then(/** @param {any} r */ (r) => {
        _funds = (r?.rows || []).filter(/** @param {any} f */ (f) =>
          f && f.account && f.account !== 'TOTAL'
        );
      })
      .catch(() => { /* silent — pill stays hidden */ });
  }

  // Host-triggered refresh — when refreshKey bumps, re-fetch funds so
  // the avail-margin chip and the modal-wide funds line both reflect
  // the latest balances after a fill/cancel elsewhere.
  $effect(() => {
    if (refreshKey > 0) _refetchFunds();
  });
</script>

<div class="ot-overlay" role="dialog" aria-modal="true" aria-label="Place order"
     onclick={onClose}>
  <div class="ot-modal" role="document" onclick={(e) => e.stopPropagation()}>
    <div class="ot-header">
      <div class="ot-symbol">
        <span class="ot-symbol-text">{symbol}</span>
        <span class="ot-symbol-meta">
          {exchange ? exchange + ' · ' : ''}
          {kind}{_lotSize ? ' · lot ' + _lotSize : ''}
          {action !== 'open' ? ' · ' + action.toUpperCase() : ''}
        </span>
      </div>
      <button type="button" class="ot-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
    </div>

    <!-- Combined top row: Account · Symbol · Qty (and Side toggle).
         Operator reads the entry strip left-to-right: WHO is placing,
         WHAT contract, HOW MUCH — then the side pill flips BUY/SELL.
         When _lotSize > 0, Qty becomes the Lots stepper. -->
    <div class="ot-row ot-row-quick">
      <!-- Account picker — hidden when the host page (e.g. /orders'
           Order Entry card header) already exposes a shared account
           picker, so the operator doesn't see two of them. _account
           still binds to the prop, just the visible chooser is
           suppressed. -->
      {#if _accounts.length && !accountHidden}
        <div class="ot-quick-block">
          <span class="ot-label">Account</span>
          {#if _accounts.length === 1}
            <input id="ot-account" type="text" class="ot-input ot-account-readonly"
                   value={_account} readonly />
          {:else}
            <Select id="ot-account"
                    bind:value={_account}
                    placeholder="Pick an account…"
                    ariaLabel="Account"
                    options={_accounts.map(a => ({ value: a, label: a }))} />
          {/if}
        </div>
      {/if}
      {#if !symbolHidden}
        <div class="ot-quick-block">
          <span class="ot-label">Symbol</span>
          <span class="ot-sym-chip" title={`${exchange || '?'} · ${kind}${_lotSize ? ' · lot ' + _lotSize : ''}`}>
            {symbol || '—'}
          </span>
        </div>
      {/if}
      <!-- Bare-underlying inline pickers (Possibility B). When the
           picker row's Type filter says FUT or OPT and the symbol is
           just the underlying name, we surface Expiry (FUT + OPT) +
           Strike + CE/PE (OPT only) so the operator can build the
           full tradingsymbol without leaving the Ticket. -->
      {#if _isBareUnderlying}
        <div class="ot-quick-block">
          <span class="ot-label">Expiry</span>
          {#if _expiryChoices.length}
            <Select
              bind:value={_pickedExpiry}
              ariaLabel="Expiry"
              options={_expiryChoices.map(e => ({ value: e, label: e.slice(5) }))} />
          {:else}
            <span class="ot-sym-chip">—</span>
          {/if}
        </div>
        {#if _wantsOpt}
          <div class="ot-quick-block">
            <span class="ot-label">Strike</span>
            {#if _strikeChoices.length}
              <Select
                value={String(_pickedStrike)}
                onValueChange={(v) => { _pickedStrike = Number(v); }}
                ariaLabel="Strike"
                options={_strikeChoices.map(k => ({ value: String(k), label: String(k) }))} />
            {:else}
              <span class="ot-sym-chip">—</span>
            {/if}
          </div>
          <div class="ot-quick-block">
            <span class="ot-label">CE/PE</span>
            <div class="ot-side-toggle ot-side-toggle-compact">
              <button type="button" class="ot-side-btn"
                class:on={_pickedOptType === 'CE'}
                onclick={() => { _pickedOptType = 'CE'; _pickedStrike = ''; }}>CE</button>
              <button type="button" class="ot-side-btn"
                class:on={_pickedOptType === 'PE'}
                onclick={() => { _pickedOptType = 'PE'; _pickedStrike = ''; }}>PE</button>
            </div>
          </div>
        {/if}
        <!-- Resolved contract chip — confirms what tradingsymbol the
             picks would submit. Reads "Resolving…" until the
             instruments cache hits the right combo. -->
        <div class="ot-quick-block">
          <span class="ot-label">Resolved</span>
          <span class="ot-sym-chip" class:ot-sym-chip-warn={!_resolvedSymbol}>
            {_resolvedSymbol || 'Resolving…'}
          </span>
        </div>
      {/if}
      <!-- Lots/Qty moved into its own row below alongside Limit Price
           (operator: "lots and limit price should be in the same row.
           let lots/qty take 65% of the space and 35% for limit
           price"). The quick-row above now ends after Resolved. -->
    </div>

    <!-- Side toggle moved into the knobs row below per operator
         request ("type, product, variety and validity should be on
         the same row as buy/sell side"). -->

    <!-- Per-account funds pill — sits ABOVE the Type/Product row so
         the operator always sees Avail margin + Cash before picking
         a side. Lifted out of the account-list gate so it surfaces
         even when the picker is empty (single-account scenarios, or
         a delayed /accounts fetch); falls back to the summed totals
         across every loaded fund row when no specific account is
         selected. Negative margin (margin debt) flips the pill red. -->
    {#if _accountFunds && !fundsHidden}
      <div class="ot-funds" class:ot-funds-low={_accountFunds.avail_margin < 0}
           title={_accountFunds.account === 'TOTAL'
             ? 'Sum across every loaded broker account'
             : `Funds for ${_accountFunds.account}`}>
        {#if _accountFunds.account === 'TOTAL'}
          <span class="ot-funds-k">Total</span>
        {/if}
        <span class="ot-funds-k">Avail margin</span>
        <span class="ot-funds-v">₹{aggFmt(_accountFunds.avail_margin || 0)}</span>
        <span class="ot-funds-sep">·</span>
        <span class="ot-funds-k">Cash</span>
        <span class="ot-funds-v">₹{aggFmt(_accountFunds.cash || 0)}</span>
        {#if _accountFunds.used_margin > 0}
          <span class="ot-funds-sep">·</span>
          <span class="ot-funds-k">Used</span>
          <span class="ot-funds-v">₹{aggFmt(_accountFunds.used_margin || 0)}</span>
        {/if}
      </div>
    {/if}

    <!-- Type + Product pills — kept on a single row even on narrow
         viewports. Earlier each label sat ABOVE its pill row, and
         the row + the pills both wrapped — the modal felt cluttered.
         Now: inline `Type:` / `Product:` labels next to compact
         pills, ot-pills nowrap, ot-row nowrap. Pills shrink slightly
         (font 0.6 → 0.55rem, padding tightened) to leave headroom. -->
    <!-- Type · Product · Variety · Validity — all four order-shape
         knobs in a single row of compact Selects. Industry analogue:
         Kite Web's order form puts these inline (not stacked); the
         earlier pill-rows approach was visually noisy and wasted
         vertical space (8 pills + 5 pills + 2 pills across 2 rows).
         Selects keep the density tight on mobile and align cleanly
         with the rest of the form. -->
    <div class="ot-row ot-row-knobs">
      <!-- Side toggle sits as the FIRST knob alongside Type / Product
           / Variety / Validity. Operators pick "what I'm doing"
           (side) and "how I'm doing it" (knobs) in one horizontal
           sweep. Locked when action='modify' (Kite doesn't support
           flipping side on a working order). -->
      <!-- Side as a Select (operator: "buy and sell be drop down
           like type in order ticket"). Frees up horizontal space
           previously taken by the two-pill toggle. -->
      <div class="ot-knob ot-knob-side">
        <label class="ot-label" for="ot-side-sel">Side</label>
        <Select id="ot-side-sel"
                bind:value={_side}
                disabled={action === 'modify'}
                ariaLabel="Side"
                onValueChange={(v) => onSideChange?.(/** @type {'BUY'|'SELL'} */ (v))}
                options={[
                  { value: 'BUY',  label: sideLabels.BUY  },
                  { value: 'SELL', label: sideLabels.SELL },
                ]} />
      </div>
      <div class="ot-knob">
        <label class="ot-label" for="ot-type-sel">Type</label>
        <Select id="ot-type-sel"
                bind:value={_type}
                ariaLabel="Order type"
                options={[
                  { value: 'MARKET', label: 'MARKET' },
                  { value: 'LIMIT',  label: 'LIMIT'  },
                  { value: 'SL',     label: 'SL'     },
                  { value: 'SL-M',   label: 'SL-M'   },
                ]} />
      </div>
      <div class="ot-knob">
        <label class="ot-label" for="ot-product-sel">Product</label>
        <Select id="ot-product-sel"
                bind:value={_product}
                ariaLabel="Product"
                options={productOptions.map(p => ({ value: p, label: p }))} />
      </div>
      <div class="ot-knob">
        <label class="ot-label" for="ot-variety-sel">Variety</label>
        <Select id="ot-variety-sel"
                bind:value={_variety}
                ariaLabel="Variety"
                options={[
                  { value: 'regular', label: 'REG' },
                  { value: 'amo',     label: 'AMO' },
                  { value: 'co',      label: 'CO'  },
                ]} />
      </div>
      <div class="ot-knob">
        <label class="ot-label" for="ot-validity-sel">Validity</label>
        <Select id="ot-validity-sel"
                bind:value={_validity}
                ariaLabel="Validity"
                options={[
                  { value: 'DAY', label: 'DAY' },
                  { value: 'IOC', label: 'IOC' },
                ]} />
      </div>
    </div>

    <!-- Lots/Qty + Limit price (or Trigger when no limit) — single
         row, 65% / 35% split per operator request. Trigger gets its
         own row below when both showLimit AND showTrigger (SL). -->
    <div class="ot-row ot-lots-price-row">
      <div class="ot-label-block ot-lots-cell">
        {#if _lotSize > 0 && !isEquity}
          <label class="ot-label" for="ot-lots">Lots</label>
          <div class="ot-lots-row">
            <button type="button" class="ot-lots-step"
                    onclick={() => stepLots(-1)}
                    disabled={_lots <= 1}
                    aria-label="Decrease lots">−</button>
            <input id="ot-lots" type="number"
                   class="ot-input ot-num ot-lots-input"
                   step="1" min="1"
                   bind:value={_lots}
                   oninput={() => { _lotsTouched = true; }}
                   onblur={() => { _lots = Math.max(1, Number(_lots) || 1); }}
                   aria-label="Lots" />
            <button type="button" class="ot-lots-step"
                    onclick={() => stepLots(1)}
                    aria-label="Increase lots">+</button>
            <span class="ot-meta">× {_lotSize} = {_qty}</span>
          </div>
        {:else}
          <label class="ot-label" for="ot-qty">Qty</label>
          <div class="ot-lots-row">
            <button type="button" class="ot-lots-step"
                    onclick={() => { _qty = Math.max(1, (Number(_qty) || 1) - 1); }}
                    disabled={!_qty || _qty <= 1}
                    aria-label="Decrease qty">−</button>
            <input id="ot-qty" type="number"
                   class="ot-input ot-num ot-lots-input"
                   step="1" min="1"
                   bind:value={_qty}
                   onblur={() => { _qty = Math.max(1, Number(_qty) || 1); }}
                   aria-label="Qty" />
            <button type="button" class="ot-lots-step"
                    onclick={() => { _qty = (Number(_qty) || 0) + 1; }}
                    aria-label="Increase qty">+</button>
          </div>
        {/if}
      </div>
      {#if showLimit}
        <div class="ot-label-block ot-price-cell">
          <label class="ot-label" for="ot-price">
            Limit price
            {#if _priceTouched && _lastQuote}
              <button type="button" class="ot-price-reset"
                      title="Re-arm auto-fill — restore {_side === 'BUY' ? 'top ask' : 'top bid'}"
                      aria-label="Reset price to depth"
                      onclick={() => { _priceTouched = false; _autoFillFromQuote(); }}>↺</button>
            {/if}
          </label>
          <input id="ot-price" type="number" class="ot-input ot-num"
                 step="0.05"
                 bind:value={_price}
                 oninput={() => { _priceTouched = true; }} />
        </div>
      {:else if showTrigger}
        <div class="ot-label-block ot-price-cell">
          <label class="ot-label" for="ot-trigger">Trigger</label>
          <input id="ot-trigger" type="number" class="ot-input ot-num"
                 step="0.05"
                 bind:value={_trigger} />
        </div>
      {/if}
    </div>

    <!-- Trigger row only when BOTH limit AND trigger are required (SL). -->
    {#if showLimit && showTrigger}
      <div class="ot-row">
        <div class="ot-label-block">
          <label class="ot-label" for="ot-trigger">Trigger</label>
          <input id="ot-trigger" type="number" class="ot-input ot-num"
                 step="0.05"
                 bind:value={_trigger} />
        </div>
      </div>
    {/if}

    <!-- Target take-profit — optional field. When set, the backend
         creates a linked TP order on fill. Toggle between % and ₹
         modes via the mode pill. Industry analogue: Kite GTT,
         ToS bracket-order TP leg. Shown only for new orders. -->
    {#if action === 'open'}
      <div class="ot-row ot-target-row">
        <div class="ot-label-block" style="flex: 1 1 0; min-width: 0">
          <label class="ot-label" for="ot-target">
            Target
            <span class="ot-label-sub">(optional)</span>
          </label>
          <div class="ot-target-input-row">
            <button type="button" class="ot-target-mode-pill"
                    class:on={_targetMode === 'pct'}
                    title="Percentage above entry price"
                    onclick={() => { _targetMode = 'pct'; }}>%</button>
            <button type="button" class="ot-target-mode-pill"
                    class:on={_targetMode === 'abs'}
                    title="Absolute ₹ above entry price"
                    onclick={() => { _targetMode = 'abs'; }}>₹</button>
            {#if _targetMode === 'pct'}
              <input id="ot-target" type="number" class="ot-input ot-num ot-target-input"
                     step="0.1" min="0" placeholder="e.g. 30"
                     bind:value={_targetPct} />
              {#if Number(_targetPct) > 0}
                <span class="ot-target-hint">+{Number(_targetPct).toFixed(1)}%</span>
              {/if}
            {:else}
              <input id="ot-target" type="number" class="ot-input ot-num ot-target-input"
                     step="1" min="0" placeholder="₹ amount"
                     bind:value={_targetAbs} />
              {#if Number(_targetAbs) > 0}
                <span class="ot-target-hint">+₹{Number(_targetAbs).toLocaleString('en-IN')}</span>
              {/if}
            {/if}
          </div>
        </div>
      </div>
    {/if}

    <!-- Depth — also bubbles its quote tick up via `onQuote` so the
         ticket can keep the limit price aligned with the marketable
         side (BUY → top ask, SELL → top bid). Operator edits to the
         price field freeze the auto-fill until they hit the ↺ button
         next to the field label.
         Passes the RESOLVED tradeable symbol (e.g. CRUDEOILM26JUNFUT)
         + RESOLVED exchange (e.g. MCX) so the /api/quote poll lands on
         the right broker endpoint. Without this, the depth ladder
         would stay frozen for any bare-underlying pre-fill (CRUDEOIL,
         NIFTY, etc.) since the raw root isn't a quotable contract. -->
    <OrderDepth
      symbol={_resolvedSymbol || symbol}
      exchange={_resolvedExchange || exchange || 'NFO'}
      {refreshKey}
      onQuote={onDepthQuote} />

    <!-- Mode selector + chase — only relevant when *placing* a new
         order. action='modify' bypasses the place-pipeline entirely
         (PUT /api/orders/{id} hits the broker directly), so neither
         mode nor chase apply there; the whole row is hidden.
         Suppressed when `modeChaseHidden` is true — the host
         (SymbolPanel) renders the same controls in its shared
         toolbar so Chain + Ticket both read from one toolkit. -->
    {#if action !== 'modify' && !modeChaseHidden}
    <div class="ot-mode-row">
      <!-- Mode pills only render when there's an actual choice. With
           only one mode available (e.g. ['live']) there's nothing to
           pick — the operator just clicks Submit. The row stays
           rendered for the chase / aggressiveness controls below. -->
      {#if availableModes.length > 1}
        <span class="ot-label">Mode</span>
        <div class="ot-mode-pills">
          {#if availableModes.includes('draft')}
            <button type="button" class="ot-mode-pill ot-mode-draft" class:on={_mode === 'draft'}
                    onclick={() => _setMode('draft')}>DRAFT</button>
          {/if}
          <!-- PAPER pill not in default availableModes — dev is paper-
               only via the branch gate; on prod the per-action
               execution.live.* flags decide. Calling sites that need
               PAPER as an explicit choice can still pass it via
               availableModes. -->
          {#if availableModes.includes('paper')}
            <button type="button" class="ot-mode-pill ot-mode-paper" class:on={_mode === 'paper'}
                    title="Routes through the prod paper engine — real bid/ask, no broker hit"
                    onclick={() => _setMode('paper')}>PAPER</button>
          {/if}
          {#if availableModes.includes('live')}
            <button type="button" class="ot-mode-pill ot-mode-live" class:on={_mode === 'live'}
                    title="Submit to backend. On dev always routes to paper. On prod, routed to LIVE only when the per-action execution.live.* flag is on."
                    onclick={() => _setMode('live')}>LIVE</button>
          {/if}
        </div>
      {/if}

      <!-- Chase toggle — only meaningful for limit-bearing orders.
           When ON, the engine re-quotes the limit each tick until
           the order fills. The aggressiveness pills below set HOW
           it re-quotes:
             L (patient) — sit on your own side, wait for the market
             M (balanced) — peg to the midpoint
             H (urgent) — cross the spread to take liquidity
           Mirrors IBKR's Adaptive Algo Patient / Normal / Urgent. -->
      {#if showLimit}
        <label class="ot-chase-toggle"
               title={_chase
                 ? 'Chase ON — re-quote the limit each tick until filled'
                 : 'Chase OFF — order rests at the initial limit; fills only if the market crosses'}>
          <input type="checkbox" checked={_chase}
                 onchange={(e) => _setChase(/** @type {HTMLInputElement} */ (e.currentTarget).checked)} />
          <span class="ot-chase-label" class:on={_chase}>CHASE</span>
        </label>
        {#if _chase}
          <div class="ot-chase-agg" role="group" aria-label="Chase aggressiveness">
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-low"
                    class:on={_chaseAgg === 'low'}
                    title="Low — patient. SELL pegs to ASK, BUY pegs to BID. Order rests on your own side; fills only if the market lifts it."
                    onclick={() => _setChaseAgg('low')}>L</button>
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-med"
                    class:on={_chaseAgg === 'med'}
                    title="Medium — peg to midpoint of bid+ask. Fills when the inside moves halfway in your favour."
                    onclick={() => _setChaseAgg('med')}>M</button>
            <button type="button"
                    class="ot-chase-agg-pill ot-chase-agg-high"
                    class:on={_chaseAgg === 'high'}
                    title="High — urgent. SELL pegs to BID, BUY pegs to ASK. Crosses the spread to take liquidity on the next tick."
                    onclick={() => _setChaseAgg('high')}>H</button>
          </div>
        {/if}
      {/if}
    </div>
    {/if}

    {#if _shownErr}
      <div class="ot-err">{_shownErr}</div>
    {/if}
    {#if submitErr}
      <!-- Surface backend rejections (preflight 422, 503, broker errors)
           inline. Silent failure was causing operators to believe orders
           had been placed when they hadn't. -->
      <div class="ot-err">{submitErr}</div>
    {/if}

    <div class="ot-footer">
      <!-- Buttons FIRST, margin preview BELOW per operator request.
           Earlier the footer was horizontal: margin on the left,
           buttons on the right. Operator wanted the action row at the
           top + the trade economics chip immediately under it — same
           layout IB TWS uses (action footer, then cost-impact strip
           beneath). Suppressed when `actionsHidden` is true so the
           host can render its own page-level common action strip. -->
      {#if !actionsHidden}
      <div class="ot-footer-actions">
        {#if submitOk}
          <!-- Post-submit success: Clear wipes the form so the operator
               can immediately enter the next order (operator: "there is
               not exit button required. probably clear button required").
               × on the SymbolPanel header handles modal dismissal. -->
          <button type="button" class="ot-exit"
                  title="Reset the ticket to a clean form"
                  onclick={clearForm}>Clear</button>
        {:else}
          <button type="button" class="ot-exit"
                  title="Reset the ticket to a clean form"
                  onclick={clearForm}>Clear</button>
          {#if onAddToBasket && action === 'open'}
            <button type="button" class="ot-basket"
                    disabled={!!validationErr || submitting}
                    title="Add this leg to the basket — place every leg together later"
                    onclick={addToBasket}>+ Basket</button>
          {/if}
          {#if basketMode && action !== 'modify'}
            <button type="button" class="ot-submit ot-submit-basket-mode"
                    disabled={!!validationErr}
                    onclick={addToBasket}>
              + Add to basket
            </button>
          {:else}
            <button type="button" class="ot-submit"
                    class:ot-submit-buy={_side === 'BUY'}
                    class:ot-submit-sell={_side === 'SELL'}
                    disabled={!!validationErr || submitting}
                    onclick={submit}>
              {#if submitting}…{:else if action === 'modify'}Modify{orderId ? ' · #' + orderId : ''}{:else if _mode === 'draft'}Save draft{:else if sideLabels[_side] === 'CLOSE'}Close · {_side.toLowerCase()}{:else if sideLabels[_side] === 'ADD'}Add · {_side.toLowerCase()}{:else}Place {_side.toLowerCase()}{/if}
            </button>
          {/if}
        {/if}
      </div>
      {/if}
      <!-- Operator: "margin details should be common for chain and
           order ticket". OrderTicket's internal margin block
           dropped — the shell's .oes-margin-pill (in
           SymbolPanel's common action footer) now owns the
           cost/cash vs margin/avail readout for both tabs.
           Keeps the success message slot since that's a
           per-ticket lifecycle signal (not shared). -->
      {#if submitOk}
        <div class="ot-footer-info">
          <div class="ot-ok">✓ {submitOk}</div>
        </div>
      {/if}

    </div>
  </div>
</div>

<style>
  .ot-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    /* z-index 300: above the OrderTimelineDrawer (z=200), the
       PositionStrip (z=49), the navbar (z=50), and any per-page
       sticky LogPanel that might be in the operator's way when the
       ticket opens. Operators reported the Submit row getting
       clipped by bottom panels — this guarantees the ticket sits
       on top. */
    z-index: 300;
    padding: 1rem;
  }
  .ot-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    width: min(28rem, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
  }
  /* Mobile: match the canonical-modal-panel sizing (96vw × 90vh)
     so every modal — ticket, order shell, chart, activity — opens
     at the same dimensions and position. Operator request: "on
     mobile, all the modals should be of same dimensions and at the
     same location. They should be fully visible on mobile." */
  @media (max-width: 760px) {
    .ot-modal {
      width: 96vw;
      max-height: 90vh;
    }
  }

  .ot-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.55rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    margin-bottom: 0.6rem;
  }
  .ot-symbol-text {
    font-size: 0.78rem;
    font-weight: 700;
    color: #c8d8f0;
    display: block;
  }
  .ot-symbol-meta {
    font-size: 0.6rem;
    color: #a3b9d0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ot-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: #c8d8f0;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 0.78rem;
    line-height: 1;
  }
  .ot-close:hover { border-color: #f87171; color: #f87171; }

  .ot-row {
    display: flex;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
    flex-wrap: wrap;
    /* All row children stack label-above-control. align-items:flex-start
       keeps the label baselines on the same y-axis even when one child
       wraps to a taller stack (e.g. price + auto/reset chips). */
    align-items: flex-start;
  }
  .ot-label-block { flex: 1 1 0; min-width: 0; }

  /* Quick-row top strip: Account · Symbol · Qty side-by-side. Each
     block stacks its label above the control like the rest of the
     ticket, but they share the row so the operator reads WHO · WHAT
     · HOW MUCH in one glance. Wraps cleanly on narrow viewports. */
  .ot-row-quick {
    gap: 0.5rem;
    align-items: flex-end;
  }
  .ot-quick-block {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    flex: 1 1 0;
    min-width: 0;
  }
  /* Qty block grows a bit more than Account / Symbol — the lots
     stepper carries multiple controls. Label sits INLINE to the LEFT
     of the value (operator request) instead of stacked above. */
  .ot-quick-qty {
    flex: 1.4 1 0;
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
  }
  .ot-quick-qty .ot-label {
    /* Inline-mode label: stop taking 100% width of the column, drop
       the bottom margin, align baseline with the value. */
    width: auto;
    min-width: 2.2rem;
    margin: 0;
    flex-shrink: 0;
  }
  .ot-quick-qty .ot-lots-row {
    flex: 1 1 auto;
  }
  .ot-quick-qty .ot-input.ot-num {
    flex: 1 1 auto;
    min-width: 0;
  }
  /* Symbol display chip — read-only label showing the symbol from
     SymbolPanel's shared picker. Operator picks the symbol once at
     the shell level; this chip mirrors it inside the ticket form
     so the operator can confirm what they're trading without
     leaving the entry fields. */
  .ot-sym-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.22rem 0.5rem;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 3px;
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* Warn state for the resolved-contract chip — fires when the
     bare-mode pickers haven't yet built a valid tradingsymbol. */
  .ot-sym-chip-warn {
    background: rgba(248, 113, 113, 0.08);
    border-color: rgba(248, 113, 113, 0.35);
    color: #f87171;
  }
  .ot-label {
    /* Section-header treatment so labels read as form structure
       cues, distinct from values / pills / numeric inputs. Amber
       weight 700 (was muted-slate 400-ish) + slightly larger
       letter-spacing — matches the way headings on the algo theme
       cards lead with amber. Operator: "make labels look different
       from others in the order window with depth". */
    display: block;
    font-size: 0.62rem;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.18rem;
    opacity: 0.85;
  }

  /* Re-arm-auto button — visible only after the operator has
     touched the price field. Click → reset _priceTouched and
     re-fill from the latest quote. */
  .ot-price-reset {
    margin-left: 0.4rem;
    padding: 0 0.35rem;
    height: 0.95rem;
    line-height: 1;
    border-radius: 2px;
    border: 1px solid rgba(125,211,252,0.55);
    background: rgba(125,211,252,0.10);
    color: #7dd3fc;
    font-size: 0.6rem;
    font-weight: 700;
    cursor: pointer;
    vertical-align: 1px;
  }
  .ot-price-reset:hover {
    background: rgba(125,211,252,0.22);
    border-color: rgba(125,211,252,0.85);
  }

  /* Side block — "Side" label + BUY/SELL pill pair. The label
     mirrors the "Lots" / "Qty" label structure in the sibling
     ot-qty-block so the pills + the lots-row line up on the same
     baseline. Without the label the pills would float up to the
     top of the row while Lots data sat below its own label. */
  .ot-side-block { display: flex; flex-direction: column; }
  .ot-side-block .ot-label { margin-bottom: 0.18rem; }

  /* Side toggle (BUY / SELL / ADD / CLOSE). Fixed height + flex
     centring so it lines up exactly with the lots row + steppers
     in the sibling .ot-qty-block. The earlier padding-based sizing
     left the side pill ~0.13rem taller than the 1.5rem steppers,
     so the BUY/SELL label and the [−] N [+] glyphs sat at slightly
     different y-centres. */
  .ot-side-toggle {
    display: flex;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    overflow: hidden;
    height: 1.7rem;
  }
  .ot-side-btn {
    padding: 0 0.75rem;
    background: transparent;
    border: 0;
    color: #a3b9d0;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    flex: 1 1 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  .ot-side-buy.on  { background: rgba(74,222,128,0.18);  color: #4ade80; }
  .ot-side-sell.on { background: rgba(248,113,113,0.18); color: #f87171; }
  /* Locked side toggle (action='modify') — Kite doesn't support
     flipping side on a working order; the button visibly reads as
     disabled and the click is a no-op. */
  .ot-side-toggle.ot-locked .ot-side-btn:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  /* Stacks label above the [−] N [+] row so the block lines up with
     the sibling .ot-side-block (also label-on-top). Previously the
     label sat INLINE next to the row via flex-end alignment, which
     made the Side toggle's pills (label-on-top) and the Lots row
     (label-inline) start at different y-coordinates. */
  .ot-qty-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0;
    flex: 1 1 0;
    min-width: 0;
  }
  .ot-qty-block .ot-label { margin: 0 0 0.18rem; }
  .ot-qty-block .ot-meta { font-size: 0.65rem; color: #a3b9d0; }

  /* [−] [1 ▼] [+] (× 50 = 50) — lots-driven Qty UI. Sits inline on
     a single row; nowrap so the +/− and the dropdown can never
     break onto two lines on narrow viewports. Height pinned to
     1.7rem to match the .ot-side-toggle so the [−] N [+] glyphs
     and the BUY/SELL pill share the same y-baseline + y-centre. */
  /* Lots + Limit row — 65 % / 35 % split with NO gap (operator:
     "lots and limit price should not have gap between them. if
     required expand both the elements to fill the gap"). Cells
     expand to exactly 65 / 35 of the row width and sit flush.
     When Lots is the only child (MARKET order — no limit, no
     trigger) it takes the full row instead of leaving 35 %
     whitespace; audit defect #19. */
  .ot-lots-price-row { gap: 0; align-items: flex-start; }
  .ot-lots-cell  { flex: 0 0 65%; min-width: 0; }
  .ot-lots-cell:only-child { flex: 1 1 100%; }
  .ot-price-cell { flex: 0 0 35%; min-width: 0; }
  .ot-price-cell .ot-input { width: 100%; }

  .ot-lots-row {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    flex-wrap: nowrap;
    height: 1.7rem;
  }
  /* Editable [−][N][+] input — sits between the two stepper buttons.
     Narrow but readable; same height as the steppers so the trio
     reads as one control. */
  .ot-lots-input {
    width: 3.2rem;
    height: 1.7rem;
    text-align: center;
    padding: 0 0.25rem;
    -moz-appearance: textfield;
  }
  .ot-lots-input::-webkit-outer-spin-button,
  .ot-lots-input::-webkit-inner-spin-button {
    -webkit-appearance: none;
    margin: 0;
  }
  .ot-lots-step {
    width: 1.7rem;
    height: 1.7rem;
    padding: 0;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
    font-family: monospace;
    font-size: 0.9rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* Rapid taps on iOS Safari otherwise get swallowed by the
       double-tap-to-zoom gesture — manipulation lets every tap
       through cleanly. user-select: none prevents accidental text
       selection between fast taps. */
    touch-action: manipulation;
    -webkit-user-select: none;
    user-select: none;
  }
  .ot-lots-step:hover:not(:disabled) {
    background: rgba(251,191,36,0.22);
    border-color: rgba(251,191,36,0.75);
  }
  .ot-lots-step:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  /* Lots count display — replaces the dropdown that spilled out of
     the row on narrow viewports. Compact pill, amber + bold,
     matching the chain picker's `.chain-quick-lots-val` style. */
  .ot-lots-val {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.8rem;
    padding: 0 0.35rem;
    height: 1.4rem;
    flex: 0 0 auto;
    color: #fbbf24;
    font-family: monospace;
    font-weight: 700;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
    text-align: center;
  }
  .ot-qty-block .ot-lots-row .ot-meta {
    /* Meta tag sits inline next to the [+] button without padding
       below — was inheriting `.ot-meta { padding-bottom: 0.5rem }`
       from the cash-equity Qty path which mis-aligned it on the
       lots row. */
    padding-bottom: 0;
    white-space: nowrap;
  }

  .ot-input {
    width: 100%;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.3rem 0.45rem;
    color: #c8d8f0;
    font-size: 0.7rem;
    font-family: monospace;
  }
  .ot-input:focus { outline: none; border-color: #fbbf24; }
  .ot-num { text-align: right; }

  /* Pill toggles (Type, Product, Variety) */
  .ot-pills { display: flex; gap: 0.15rem; flex-wrap: wrap; }
  .ot-pills-nowrap { flex-wrap: nowrap; }
  .ot-pill {
    padding: 0.2rem 0.4rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 600;
    cursor: pointer;
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .ot-pill.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  /* Disabled-pill — visible but unreachable. Used for Variety BO + ICE
     where backend wiring isn't done yet; operator sees them on the
     roadmap without being able to click. Tooltip explains why. */
  .ot-pill-disabled,
  .ot-pill[disabled] {
    opacity: 0.35;
    cursor: not-allowed;
    pointer-events: auto; /* keep title tooltip reachable */
  }
  .ot-pill-disabled:hover,
  .ot-pill[disabled]:hover {
    background: rgba(255,255,255,0.04);
    color: #a3b9d0;
    border-color: rgba(255,255,255,0.12);
  }
  /* Inline label + pill row: labels sit next to pills (instead of
     stacking above), so Type and Product fit on the same line
     within the modal's 28 rem width. nowrap on the ot-row level
     keeps the two blocks side by side; the modal scrolls
     horizontally only as a last-resort safety net. */
  .ot-label-inline {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex: 0 0 auto;
    min-width: 0;
  }
  .ot-label-inline .ot-label {
    margin: 0;
  }
  .ot-row-tight {
    flex-wrap: nowrap;
    gap: 0.5rem;
    overflow-x: auto;
  }

  /* Knobs row — Type · Product · Variety · Validity rendered as
     four compact Selects in one row. Each Select min-width: 4.5rem
     so the dropdown triggers don't shrink past their label glyph
     count. Wraps cleanly on narrow viewports (Select carries its
     own internal width logic). */
  .ot-row-knobs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: flex-end;
    margin-bottom: 0.45rem;
  }
  .ot-knob {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    flex: 1 1 5rem;
    min-width: 5rem;
  }
  .ot-knob-side { flex: 1.4 1 7rem; min-width: 7rem; }
  .ot-side-toggle-compact {
    display: inline-flex;
    width: 100%;
    height: 1.7rem;
    border-radius: 3px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.12);
  }
  .ot-side-toggle-compact .ot-side-btn {
    flex: 1 1 0;
    background: transparent;
    border: 0;
    color: #94a3b8;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .ot-side-toggle-compact .ot-side-btn.ot-side-buy.on  { background: rgba(74, 222, 128, 0.22); color: #4ade80; }
  .ot-side-toggle-compact .ot-side-btn.ot-side-sell.on { background: rgba(248, 113, 113, 0.22); color: #f87171; }
  .ot-side-toggle-compact .ot-side-btn[disabled] { opacity: 0.4; cursor: not-allowed; }

  /* Mode row */
  .ot-mode-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.7rem 0;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .ot-mode-pills { display: flex; gap: 0.25rem; }
  .ot-mode-pill {
    padding: 0.2rem 0.55rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: #a3b9d0;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
  }
  .ot-mode-pill:disabled { opacity: 0.4; cursor: not-allowed; }
  .ot-mode-draft.on { background: rgba(192,132,252,0.18); border-color: rgba(192,132,252,0.55); color: #c084fc; }
  .ot-mode-paper.on { background: rgba(125,211,252,0.18); border-color: rgba(125,211,252,0.55); color: #7dd3fc; }
  .ot-mode-live.on  { background: rgba(74,222,128,0.18);  border-color: rgba(74,222,128,0.55);  color: #4ade80; }

  /* Chase toggle — pushed to the row's far right (margin-left: auto)
     so it sits opposite the mode pills. Native checkbox + label
     pill; the label tints amber when ON to match the rest of the
     ticket's "active state" treatment. */
  .ot-chase-toggle {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
    user-select: none;
  }
  .ot-chase-toggle input[type="checkbox"] {
    accent-color: #fbbf24;
    width: 0.85rem;
    height: 0.85rem;
    cursor: pointer;
  }
  .ot-chase-label {
    font-family: monospace;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.12);
    color: #a3b9d0;
    background: rgba(255,255,255,0.04);
  }
  .ot-chase-label.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }

  /* Aggressiveness segment — three square pills (L · M · H) sitting
     immediately right of the CHASE checkbox. Color graduates from
     sky-blue (low/patient) → amber (med) → red (high/urgent) so
     the operator's eye lands on the urgency level without reading
     the glyph. Only the active pill carries the filled bg. */
  .ot-chase-agg {
    display: inline-flex;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    overflow: hidden;
    margin-left: 0.3rem;
  }
  .ot-chase-agg-pill {
    width: 1.4rem;
    height: 1.1rem;
    padding: 0;
    border: 0;
    border-right: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04);
    color: #a3b9d0;
    font-family: monospace;
    font-size: 0.6rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .ot-chase-agg-pill:last-child { border-right: 0; }
  .ot-chase-agg-pill:hover { color: #c8d8f0; background: rgba(255,255,255,0.08); }
  .ot-chase-agg-low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; }
  .ot-chase-agg-med.on  { background: rgba(251,191,36,0.20);  color: #fbbf24; }
  .ot-chase-agg-high.on { background: rgba(248,113,113,0.20); color: #f87171; }

  .ot-err {
    background: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.4);
    color: #f87171;
    padding: 0.35rem 0.55rem;
    border-radius: 3px;
    font-size: 0.62rem;
    margin: 0.4rem 0;
  }
  /* Margin/cash preview rules retired — the lift-to-shell commit
     (fcd75587) removed the inline .ot-margin-row markup; the shell's
     .oes-margin-pill now owns the cost/margin readout. CSS block
     dropped as dead code (audit defect #12). */
  /* Preflight blockers (segment inactive, freeze qty, etc.) */
  .ot-margin-blocked {
    color: #f87171;
    font-size: 0.58rem;
    margin-top: 0.2rem;
    line-height: 1.35;
  }
  .ot-margin-err {
    color: rgba(248,113,113,0.85);
    font-size: 0.6rem;
  }
  /* Placed-order summary line — lives inside .ot-footer-info, to the
     left of the Exit button. Compact (no vertical margin, smaller pad
     than the prior block-level version) so the footer doesn't shove
     the form fields off-screen. */
  .ot-ok {
    background: rgba(74,222,128,0.10);
    border: 1px solid rgba(74,222,128,0.45);
    color: #4ade80;
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    font-size: 0.65rem;
    font-weight: 700;
    line-height: 1.3;
    word-break: break-word;
  }
  /* Readonly single-account display — matches the custom Select
     trigger's metrics so single-account vs multi-account UIs sit at
     the same height. Cursor: default keeps it visually distinct from
     editable inputs. */
  .ot-account-readonly {
    color: #c8d8f0;
    background: rgba(255,255,255,0.04);
    cursor: default;
    min-height: 1.55rem;
    padding: 0.25rem 0.5rem 0.25rem 0.4rem;
    font-size: 0.62rem;
  }

  /* Funds pill — appears under the Account input. Compact 12px-ish
     row of `Avail margin ₹X · Cash ₹Y · Used ₹Z`. Sky-blue tint to
     read as info, flips red when margin goes negative (margin debt
     — the operator's about to get a Kite rejection). */
  .ot-funds {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.3rem 0.4rem;
    margin-top: 0.4rem;
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    background: rgba(125,211,252,0.08);
    border: 1px solid rgba(125,211,252,0.25);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.7rem;
    line-height: 1.2;
  }
  .ot-funds-k {
    color: #a3b9d0;
    font-size: 0.65rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .ot-funds-v {
    color: #c8d8f0;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .ot-funds-sep {
    color: #a3b9d0;
    opacity: 0.5;
  }
  .ot-funds-low {
    background: rgba(248,113,113,0.10);
    border-color: rgba(248,113,113,0.35);
  }
  .ot-funds-low .ot-funds-v { color: #f87171; }

  /* Footer is a column: action buttons on top, margin / cost preview
     beneath. IB TWS, ToS, and Sensibull all stack the impact line
     under the action row — the operator's eye reads top-to-bottom
     "what will I do" → "what will it cost". */
  .ot-footer {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding-top: 0.6rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .ot-footer-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    justify-content: flex-end;
  }
  .ot-footer-info {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap;
    gap: 0.45rem 0.85rem;
    align-items: center;
    /* Subtle background tint so the impact strip reads as its own
       region distinct from the buttons + form above. */
    padding: 0.35rem 0.5rem;
    background: rgba(15, 23, 42, 0.40);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 3px;
  }
  .ot-footer-info:empty { display: none; }
  .ot-exit,
  .ot-basket,
  .ot-submit {
    padding: 0.45rem 1rem;
    border-radius: 3px;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    border: 1px solid transparent;
    flex-shrink: 0;
  }
  /* Exit — replaces Cancel + Submit after a successful order, and also
     stands in for Cancel before submit. Outlined champagne — reads as
     "close when ready", not as another primary action. */
  .ot-exit {
    background: rgba(212,146,12,0.12);
    border-color: rgba(212,146,12,0.55);
    color: #d4920c;
  }
  .ot-exit:hover { background: rgba(212,146,12,0.22); border-color: rgba(212,146,12,0.85); }
  /* Stage-into-basket — outlined amber, distinct from the filled
     green/red Submit. Reads as a secondary action: the operator
     can stack legs before placing the whole basket together. */
  .ot-basket {
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  .ot-basket:hover:not(:disabled) {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.85);
  }
  .ot-basket:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-submit {
    background: #fbbf24;
    color: #0c1830;
  }
  .ot-submit-buy  { background: #4ade80; }
  .ot-submit-sell { background: #f87171; }
  /* Basket-mode primary action — amber outlined, distinct from the
     green/red fill so the operator reads "stage, don't fire yet". */
  .ot-submit-basket-mode {
    background: rgba(74,222,128,0.12);
    color: #4ade80;
    border: 1px solid rgba(74,222,128,0.55);
  }
  .ot-submit-basket-mode:hover:not(:disabled) {
    background: rgba(74,222,128,0.22);
    border-color: rgba(74,222,128,0.85);
  }
  .ot-submit:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-submit-basket-mode:disabled { opacity: 0.45; cursor: not-allowed; }

  /* Target take-profit row */
  .ot-target-row { flex-wrap: nowrap; }
  .ot-label-sub { opacity: 0.5; font-weight: 400; font-size: 0.55rem; margin-left: 0.2rem; }
  .ot-target-input-row {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    flex-wrap: nowrap;
  }
  .ot-target-mode-pill {
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    border: 1px solid rgba(125,211,252,0.35);
    background: transparent;
    color: #7dd3fc;
    font-size: 0.6rem;
    cursor: pointer;
    font-family: inherit;
    transition: background 0.15s;
  }
  .ot-target-mode-pill.on {
    background: rgba(125,211,252,0.18);
    border-color: rgba(125,211,252,0.65);
    color: #bae6fd;
  }
  .ot-target-mode-pill:hover:not(.on) { background: rgba(125,211,252,0.08); }
  .ot-target-input { width: 6rem; flex-shrink: 0; }
  .ot-target-hint {
    font-size: 0.6rem;
    color: #4ade80;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
</style>
