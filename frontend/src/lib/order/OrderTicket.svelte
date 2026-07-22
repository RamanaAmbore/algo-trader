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

  import { onMount, onDestroy, untrack, getContext } from 'svelte';
  import { get } from 'svelte/store';
  import OrderDepth from './OrderDepth.svelte';
  import ChaseAggPicker from './ChaseAggPicker.svelte';
  import QtyInput from './QtyInput.svelte';
  import Select from '$lib/Select.svelte';
  import OrderKnobsRow from '$lib/order/OrderKnobsRow.svelte';
  import SideToggle from '$lib/order/SideToggle.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { placeTicketOrder, previewOrderMargin, fetchAccounts, modifyOrder, previewTicketTemplate, fetchStrategies } from '$lib/api';
  import {
    buildModifyPayload,
    buildOnSubmitPayload,
    buildPlacePayload,
    formatPlacementOk,
    classifyIntent,
  } from './orderTicketSubmit.js';
  import { fundsStore } from '$lib/data/marketDataStores.svelte.js';
  import { loadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';
  import ModalShell from '$lib/ModalShell.svelte';
  import { appliesToFor as _appliesToFor } from '$lib/data/templateScope.js';
  import { capWarningFor } from '$lib/data/brokerCapWarnings';
  import { getDefaultAccount } from '$lib/data/accounts';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { aggFmt } from '$lib/format';
  import { executionMode } from '$lib/stores';
  import {
    getInstrument, listExpiries, listStrikes,
    findOption, findNearestFuture, listFutures,
    listExchangesForSymbol,
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
   *   onPreviewPlanUpdate?: ((plan:any, loading:boolean, error:string, capWarning:string) => void) | null,
   *   onValidationChange?: ((err: string) => void) | null,
   *   fundsHidden?: boolean,
   *   symbolHidden?: boolean,
   *   symType?: 'ALL' | 'EQ' | 'FUT' | 'OPT',
   *   refreshKey?: number,
   *   mode?:       'draft' | 'paper' | 'live' | 'shadow' | undefined,
   *   chase?:      boolean | undefined,
   *   chaseAgg?:   'low' | 'med' | 'high' | undefined,
   *   modeChaseHidden?: boolean,
   *   suspended?: boolean,
   *   templateId?: number | null,
   *   tpOverride?: number | '',
   *   slOverride?: number | '',
   *   wingStrikeOffsetOverride?: number | '',
   *   wingPremPctOverride?: number | '',
   *   standalone?: boolean,
   *   defaultChase?: boolean,
   *   defaultChaseAgg?: 'low' | 'med' | 'high',
   * }} */
  let {
    symbol,
    exchange  = '',
    side      = /** @type {'BUY' | 'SELL' | null} */ (null),
    action    = /** @type {'open' | 'close' | 'modify' | 'repeat' | 'cancel'} */ ('open'),
    qty       = 0,
    product   = /** @type {'CNC' | 'MIS' | 'NRML' | undefined} */ (undefined),
    orderType = $bindable(/** @type {'MARKET' | 'LIMIT' | 'SL' | 'SL-M'} */ ('LIMIT')),
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
    // Pipes the on-fill preview chip (_previewPlan / _previewLoading /
    // _previewError + cap warning) up to the host so SymbolPanel can
    // render it inside the Template container — visible on BOTH tabs
    // instead of only on Ticket. Mirrors the onMarginUpdate plumbing.
    onPreviewPlanUpdate = /** @type {((plan:any, loading:boolean, error:string, capWarning:string) => void) | null} */ (null),
    // Fires whenever the current validation error changes. Host
    // (SymbolPanel) uses this to surface the error as a toast when
    // its common-action Submit button is clicked and the ticket form
    // has a validation block that would otherwise silently prevent
    // placement (e.g. limit price not yet filled by the depth ladder).
    onValidationChange = /** @type {((err: string) => void) | null} */ (null),
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
    // When true the ticket is mounted but not visible (e.g. Chain tab
    // is active in SymbolPanel). Preflight effects early-return to
    // avoid firing /api/orders/preflight while the tab is hidden.
    suspended = false,
    // Shared exit-template id across SymbolPanel surfaces (Ticket /
    // Chain / Basket bar). Operator picks once; selection persists
    // across tabs and side flips. When unbound (parent passes a plain
    // null), this falls back to standalone behaviour.
    templateId = $bindable(/** @type {number|null} */ (null)),
    // Per-template parameter overrides. Shell (SymbolPanel) binds
    // them at the panel level so editing the TP%/SL%/Wing inputs in
    // the shell row updates the value the Ticket form would submit.
    // Empty string = "no override; use the template's value".
    tpOverride                = $bindable(/** @type {number|''} */ ('')),
    slOverride                = $bindable(/** @type {number|''} */ ('')),
    wingStrikeOffsetOverride  = $bindable(/** @type {number|''} */ ('')),
    wingPremPctOverride       = $bindable(/** @type {number|''} */ ('')),
    // When false, OrderTicket is embedded inside another dialog (e.g.
    // SymbolPanel) and must not re-declare role="dialog" (ARIA prohibits
    // nested dialog roles). The overlay backdrop is also suppressed.
    standalone                = true,
    // Default values for chase / chaseAgg when clearForm() runs.
    // SymbolPanel passes its shared state here so a form reset keeps the
    // operator's chase preference instead of snapping back to true/'low'.
    defaultChase              = true,
    defaultChaseAgg           = /** @type {'low'|'med'|'high'} */ ('low'),
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
      return { BUY: 'ADD · BUY', SELL: 'CLOSE · SELL' };
    }
    // Short position: selling more = ADD, buying back = CLOSE.
    return { BUY: 'CLOSE · BUY', SELL: 'ADD · SELL' };
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

  // Exchange choices — every Indian instrument lives on one of these
  // six segments. The static fallback per kind keeps the picker
  // useful before the instruments cache loads; once the cache is
  // warm, intersect with the symbol's ACTUAL listings so single-
  // exchange symbols lock to that one (RELIANCE futures live only
  // on NFO; CRUDEOIL only on MCX) and dual-listed equities offer
  // both (RELIANCE / IFCI on NSE + BSE).
  const _kindExchangeFallback = $derived.by(() => {
    if (isEquity) return ['NSE', 'BSE'];
    if (kind === 'CE' || kind === 'PE') return ['NFO', 'BFO'];
    if (kind === 'FUT') return ['NFO', 'BFO', 'MCX', 'CDS'];
    return ['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS'];
  });
  const exchangeOptions = $derived.by(() => {
    const fallback = _kindExchangeFallback;
    const sym = String(_resolvedSymbol || symbol || '').toUpperCase();
    if (!sym) return fallback;
    // Equity carve-out — for cash-market stocks the kind-fallback
    // (['NSE','BSE']) IS authoritative: virtually every NSE-listed
    // stock has a BSE listing and vice versa, and a partial instruments
    // cache (one of the two Kite exchange-calls failed at warm time)
    // would otherwise wrongly lock the operator out of the missing
    // exchange. Trust the kind list; let Kite reject at order-time if
    // the operator picks a truly missing listing.
    if (isEquity) return fallback;
    // Derivatives — strict intersection. RELIANCE26JUNFUT only lives
    // on NFO; CRUDEOIL26JUNFUT only on MCX. The instruments cache IS
    // ground truth for contract listings; locking prevents the
    // operator from picking a non-existent broker-route.
    const actual = listExchangesForSymbol(sym);
    if (actual.length === 0) return fallback;
    // Order by the static fallback so NFO precedes BFO / etc. (the
    // operator's mental scan order — primary national exchange first).
    const filtered = fallback.filter(e => actual.includes(e));
    return filtered.length > 0 ? filtered : actual;
  });

  // Local form state — start from prop defaults, then operator edits.
  // intentional: seeds from side prop once; $effect below re-syncs on prop changes
  // svelte-ignore state_referenced_locally
  let _side    = $state($state.snapshot(side));
  // Re-sync the internal side state when the parent EXPLICITLY changes
  // the `side` prop. Tracks the previous prop value so this fires only
  // on genuine prop-value transitions (e.g. SymbolPanel's _modalSide
  // store committing, or a Command-tab parse pre-filling the ticket),
  // NOT on every render the parent triggers for unrelated reasons.
  //
  // Bug class fixed (Jun 2026, operator: "when buy order is placed,
  // the buttons are flipping to sell in red color without placing the
  // order"): without the prop-transition guard, ANY re-render where
  // `side` was a non-null fixed value (typically the original side
  // prop passed to SymbolPanel, which never tracks _modalSide updates)
  // would force _side back to the stale prop value, fighting the
  // operator's BUY/SELL click. The new pattern lets local clicks win
  // and only resyncs when the parent actually moves the value.
  //
  // action='modify' freezes the side per the existing rule.
  // svelte-ignore state_referenced_locally
  let _prevSideProp = $state(/** @type {string|null} */ ($state.snapshot(side)));
  $effect(() => {
    if (action === 'modify') return;
    if (side !== _prevSideProp) {
      _prevSideProp = side;
      if (side) _side = side;
    }
  });

  // Resolved lot size — starts from the prop; may be updated on mount
  // via the instruments cache when the caller didn't supply one.
  // intentional: seeds from lotSize prop once; instruments cache may update it later
  // svelte-ignore state_referenced_locally
  let _lotSize = $state($state.snapshot(lotSize));

  // Qty path:
  //   - _lotSize > 0  → operator edits in LOTS via [−] [N] [+], the
  //     resolved qty `_lots * _lotSize` flows into _qty. Mirrors the
  //     chain picker so both surfaces read consistently.
  //   - _lotSize == 0 → cash equity / no lot concept; fall back to
  //     raw number input bound directly to _qty.
  // Initial _lots comes from the caller-supplied qty (rounded to the
  // nearest whole lot, floored at 1). When qty is also 0 we start at
  // 1 lot so the ticket opens ready-to-submit.
  // intentional: _lots and _qty seed from props/local state at init; effects below keep in sync
  // svelte-ignore state_referenced_locally
  let _lots = $state(
    _lotSize > 0
      ? Math.max(1, Math.round((Number($state.snapshot(qty)) || _lotSize) / _lotSize))
      : 1
  );
  // svelte-ignore state_referenced_locally
  let _qty     = $state($state.snapshot(qty) || _lotSize || ($state.snapshot(isEquity) ? 1 : 0));
  // Track whether the operator has touched _lots manually. If they
  // haven't, late-resolving _lotSize (instrument cache warming after
  // ticket mount) should recompute _lots from the original `qty` prop
  // so the ticket displays "2 Lots" instead of staying stuck at the
  // fallback "1 Lot". Set true by the +/- steppers + direct input.
  let _lotsTouched = $state(false);
  // When _lotSize transitions from 0 → positive (instrument cache
  // resolved after mount), recompute _lots from the caller's original
  // qty prop. Skipped once the operator has manually adjusted.
  // intentional: snapshot of _lotSize at mount to detect the 0→positive transition
  // svelte-ignore state_referenced_locally
  let _prevLotSize = $state.snapshot(_lotSize);
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

  // Reset lots to 1 when the operator picks a NEW symbol (e.g. +CE in
  // the chain tab). Without this, _lotsTouched=true from a prior
  // stepper bump kept _lots stuck at the old value — operator clicked
  // +CE expecting a fresh 1-lot ticket, got 2 lots from the previous
  // symbol's stepper bump (the GOLDM 20-contracts-instead-of-10 incident,
  // 2026-06-18). Skipped when currentQty is set (close-position flow
  // wants the held qty seeded; that's a different prop path).
  let _prevResolvedSymbol = '';
  $effect(() => {
    const r = (_resolvedSymbol || '').toString().toUpperCase();
    if (!r) return;
    if (!_prevResolvedSymbol) { _prevResolvedSymbol = r; return; }
    if (r === _prevResolvedSymbol) return;
    _prevResolvedSymbol = r;
    if (currentQty) return;
    untrack(() => { _lots = 1; _lotsTouched = false; });
  });

  // When the operator flips side, reset to 1 lot (ADD direction) or
  // restore the held qty (CLOSE direction). "ADD" = same direction as
  // the existing position; "CLOSE" = opposite.
  // intentional: snapshot of _side at mount to detect side-flip transitions
  // svelte-ignore state_referenced_locally
  let _prevSide = $state.snapshot(_side);
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
    _lots = 1;
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
    _setChase(defaultChase);
    _setChaseAgg(defaultChaseAgg);
    submitErr = '';
    submitOk = '';
    // _shownErr is a $derived from `_submitTried && validationErr`; flip
    // _submitTried back to false so the inline validation error chip
    // clears alongside the form fields.
    _submitTried = false;
  }
  let _type    = $state(orderType);
  // Bidirectional sync between local `_type` and the bindable
  // `orderType` prop. Two paired $effects: one tracks `_type` and
  // pushes outward; the other tracks `orderType` (the prop) and
  // pushes inward. Both early-return on value equality so a fired
  // write from one side immediately settles the other. The audit
  // flagged this shape as "architecturally fragile" — true; a
  // clean collapse to one effect would need source-of-truth
  // tracking that exceeds the value of the change. The pattern is
  // protected by the equality guards on both sides.
  $effect(() => { if (_type !== orderType) orderType = _type; });
  $effect(() => { if (orderType !== untrack(() => _type)) _type = orderType; });
  // intentional: seeds from variety prop once; clearForm() resets it to the prop snapshot
  // svelte-ignore state_referenced_locally
  let _variety = $state($state.snapshot(variety));
  // Validity (Time-in-Force): DAY by default. IOC (Immediate-Or-Cancel)
  // for fast-trading scenarios where partial fills are acceptable but
  // unfilled remainder should drop instead of resting. Kite supports
  // DAY + IOC; TTL/GTT live on different code paths so they're not
  // exposed here. Industry analogue: every order book (IB TWS, ToS,
  // Kite Web) surfaces DAY/IOC as inline pills.
  let _validity = $state('DAY');
  // intentional: price and trigger seed from props; operator edits thereafter
  // svelte-ignore state_referenced_locally
  let _price   = $state($state.snapshot(price) ?? '');
  // svelte-ignore state_referenced_locally
  let _trigger = $state($state.snapshot(trigger) ?? '');

  // Legacy `_targetMode / _targetPct / _targetAbs` state + the matching
  // `_targetPctVal / _targetAbsVal` derived fields removed in audit
  // pass 6. The Template picker (added Phase 4c) supersedes them —
  // operator picks a template for TP/SL/Wing instead of typing a
  // single TP %. The backend's _ticket_overrides_dict still accepts
  // legacy `target_pct` from external callers (Lab MCP scripts) via
  // a shim, so this removal is UI-surface-only.

  // ── v2 template picker ──────────────────────────────────────────────
  // Template state. `_templates` is the list fetched from
  // /api/admin/templates on mount. `templateId` is the selected row's
  // id; null means "no template" (entry-only, no follow-on attach).
  // Override fields default to '' (blank) — the picker shows the
  // template's value as a placeholder so the operator sees what will
  // run unless they tweak it. Submitting with a blank override sends
  // null (= use template default).
  let _templates = $state(/** @type {any[]} */ ([]));
  // Override fields aliased onto the bindable props above so every
  // existing read/write through these locals routes through the
  // shell's shared state (when SymbolPanel binds them). Stand-alone
  // OrderTicket mounts (no parent binding) get fresh per-mount state.

  // Pre-submit preview state.
  let _previewPlan = $state(/** @type {any} */ (null));
  let _previewLoading = $state(false);
  let _previewError = $state('');
  // Sequence number for the preview fetch — incremented before each
  // dispatch, checked after `await` to drop stale responses. See the
  // template-preview $effect for the full rationale.
  let _previewSeq = 0;

  const _selectedTemplate = $derived(
    _templates.find(t => t.id === templateId) || null
  );

  // Two-state Template toggle (Default / None) — replaces the legacy
  // multi-row Select. Operator: "Keep it simple to start using it."
  // Default → auto-pick the is_default template for the current side.
  // None    → explicit opt-out (no GTT / no wing on submit).
  const _defaultTemplate = $derived.by(() => {
    if (_templates.length === 0) return null;
    const scope = _appliesToFor(_side, symbol);
    return _templates.find(t =>
      t.is_default && (t.applies_to === scope || t.applies_to === 'both')
    ) || null;
  });
  const _noneTemplate = $derived(
    _templates.find(t => t.slug === 'none') || null
  );

  // Sprint C — broker capability matrix for the selected account.
  // Drives the inline warning chip: when caps.gtt_oco is false the
  // operator gets an "OCO emulated — ~15s race window" note before
  // submitting a TP+SL template. Fetched lazily on first account
  // change, cached in-memory per account so account-toggle is cheap.
  /** @type {Record<string, any>} */
  let _capsCache = $state({});
  /** @type {any} */
  let _brokerCaps = $state(null);
  $effect(() => {
    const acct = _account;
    if (!acct) { _brokerCaps = null; return; }
    if (_capsCache[acct]) { _brokerCaps = _capsCache[acct]; return; }
    (async () => {
      try {
        const { fetchBrokerCapabilities } = await import('$lib/api');
        const caps = await fetchBrokerCapabilities(acct);
        _capsCache = { ..._capsCache, [acct]: caps };
        if (_account === acct) _brokerCaps = caps;
      } catch (_e) {
        // Silent — demo / unauthed sessions can't see admin endpoints.
        // OrderTicket continues to work; just no inline warning chip.
      }
    })();
  });
  // Warning text — returned non-empty only when the SELECTED template
  // produces a bracket the broker can't handle natively. Drives a
  // small amber chip rendered next to the template summary. Logic
  // extracted to `$lib/data/brokerCapWarnings` so SymbolPanel can
  // reuse the same vocabulary when aggregating across basket accounts
  // (H-5). One source of truth for "OCO emulated" / "can't trail" /
  // MCX / postback-lag warnings.
  const _templateCapWarning = $derived.by(() => {
    const ex = String(_exchange || _resolvedExchange || exchange || '');
    return capWarningFor(_selectedTemplate, _brokerCaps, ex);
  });
  const _isUsingNone = $derived(
    _selectedTemplate?.slug === 'none' || templateId === null
  );

  function _summariseTemplate(t) {
    if (!t) return '';
    const parts = [];
    if (t.tp_pct != null) {
      const mkt = t.tp_order_type === 'MARKET' ? ' MKT' : '';
      parts.push(`TP +${t.tp_pct}%${mkt}`);
    }
    // Scale-out ladder summary — Sprint D fix. Pre-fix a template
    // with only tp_scales_json (no tp_pct) showed "(entry only)"
    // and the operator had no warning that multiple TP legs would
    // fire on submit.
    if (t.tp_scales_json) {
      try {
        const scales = JSON.parse(t.tp_scales_json);
        if (Array.isArray(scales) && scales.length > 0) {
          parts.push(`TP ×${scales.length} scales`);
        }
      } catch (_e) { /* malformed JSON — silently skip */ }
    }
    if (t.sl_pct != null) {
      const trail = t.sl_trail_pct != null ? ` trail ${t.sl_trail_pct}%` : '';
      parts.push(`SL -${t.sl_pct}%${trail}`);
    } else if (t.sl_trail_pct != null) {
      parts.push(`SL trail ${t.sl_trail_pct}%`);
    }
    if (t.wing_strike_offset != null) parts.push(`Wing +${t.wing_strike_offset} pts`);
    if (t.wing_premium_pct != null) parts.push(`Wing ${t.wing_premium_pct}% prem`);
    return parts.length ? parts.join(' · ') : '(entry only)';
  }

  // Default template auto-selection by side + symbol kind. Runs once on
  // mount AFTER templates are loaded so the dropdown opens with the
  // right pick already highlighted.
  function _autoSelectTemplate() {
    if (templateId !== null) return;        // operator already picked
    if (_templates.length === 0) return;
    // Operator (turn N): "Some templates are valid only for sell or
    // buy. Instead of None, going forward use the default valid
    // template for buy or sell, instead of None."
    // First-paint pick is now the side-aware is_default template that
    // matches the current scope (`_appliesToFor` resolves SELL CE/PE
    // → sell_option, BUY any → buy_any, etc.). Falls back to a 'both'
    // scope default, then to the 'none' row, then to null — so the
    // form is never stuck and a legacy install without seeded
    // defaults still works.
    const scope = _appliesToFor(_side, symbol);
    const sideMatch = _templates.find(t =>
      t.is_default && t.applies_to === scope
    );
    if (sideMatch) { templateId = sideMatch.id; return; }
    const bothMatch = _templates.find(t =>
      t.is_default && t.applies_to === 'both'
    );
    if (bothMatch) { templateId = bothMatch.id; return; }
    const none = _templates.find(t => t.slug === 'none');
    if (none) { templateId = none.id; return; }
  }

  // Side+symbol → template re-validation. The _autoSelectTemplate
  // above only runs ONCE on first paint; if the operator flips side
  // (BUY → SELL pill) or changes the symbol kind (equity → option)
  // after the initial pick, a stale templateId carries over and may
  // no longer match the new applies_to scope. Concrete incident:
  // 2026-06-22 — modal opened BUY (default scope: buy_any → picked
  // `default-bull`), operator clicked SELL pill on a PE option, submit
  // shipped template_id=1 (buy_any) for a SELL option. Backend then
  // attached a TP+SL OCO with BUY-side price math; one leg fired and
  // closed 20 contracts of the operator's short at ₹1447.5.
  //
  // The backend now has a hard `applies_to` guard (refuses to attach
  // a buy template to a SELL leg, etc.) so even a stale templateId
  // is non-destructive — but resetting the picker on side/scope flip
  // is the cleaner UX, and avoids the "I picked Default and got nothing"
  // confusion after the guard kicks in.
  $effect(() => {
    // Track _side + _resolvedSymbol (covers symbol prop + bare-
    // underlying + option-chain picks). _selectedTemplate is reactive
    // off templateId + _templates, so we don't need to depend on it
    // here — re-validate purely against the current leg shape.
    const newScope = _appliesToFor(_side, _resolvedSymbol || symbol);
    untrack(() => {
      const current = _templates.find(t => t.id === templateId);
      if (!current) return;             // no templateId or templates not loaded
      const ap = (current.applies_to || 'both').toLowerCase();
      // 'both' and 'none' (the explicit opt-out) match any scope.
      if (ap === 'both' || current.slug === 'none') return;
      if (ap === newScope) return;      // still valid for the new scope
      // Mismatch: reset to null so _autoSelectTemplate picks the
      // correct side-aware default on the next derived re-eval.
      templateId = null;
      _autoSelectTemplate();
    });
  });
  // intentional: seeds from productVal once; $effect below re-syncs on symbol kind change
  // svelte-ignore state_referenced_locally
  let _product = $state($state.snapshot(productVal));
  // Local exchange state — seeded from the resolved exchange. Operator
  // can override via the Exchange Select (essential for dual-listed
  // equities like IFCI where the instruments cache only indexes one
  // of NSE / BSE per symbol; Dhan rejects BSE+NRML for equity).
  // intentional: seeds from resolved exchange at mount; $effect below re-syncs on symbol change
  // svelte-ignore state_referenced_locally
  let _exchange = $state($state.snapshot(_resolvedExchange) || $state.snapshot(exchange) || 'NSE');
  // Tracks whether the operator picked the exchange manually for THIS
  // symbol. Reset on symbol change so a stale BSE pick doesn't carry
  // over to a new RELIANCE → INFY swap. Without it the auto-snap to
  // `exchangeOptions[0]` would constantly fight the operator's choice.
  let _exchangeTouched = $state(false);
  // Re-sync the picker when the symbol changes (so the operator
  // doesn't carry NSE over to an MCX commodity by accident). Default
  // policy: first listing in `exchangeOptions` (NSE before BSE / NFO
  // before BFO). Operator override on the SAME symbol wins.
  // Single combined re-sync — exchange + product MUST update atomically
  // when the symbol kind flips. Pre-audit, these lived in two separate
  // $effects: when the operator switched from an option (NFO+NRML) to
  // an equity (NSE+CNC), the exchange effect could fire first and set
  // `_exchange='NSE'` while `_product` was still `'NRML'`. Dhan rejects
  // NSE+NRML for equity, so a Submit clicked in that one-frame window
  // (rare but possible via SymbolPanel's _ticketBump-driven re-prefill)
  // landed in a broker error. Merging the writes into one effect body
  // closes the race — Svelte 5 flushes both reactivity writes within a
  // single tick.
  let _lastExchangeSym = '';
  $effect(() => {
    void _resolvedExchange; void exchangeOptions; void productOptions;
    const sym = String(_resolvedSymbol || symbol || '').toUpperCase();
    untrack(() => {
      // Symbol changed → reset touched + auto-snap to the first
      // available exchange so the operator never lands on a stale
      // BSE pick after switching to a single-listed instrument.
      if (sym !== _lastExchangeSym) {
        _lastExchangeSym = sym;
        _exchangeTouched = false;
      }
      const valid = exchangeOptions.includes(_exchange);
      if (!_exchangeTouched && exchangeOptions.length > 0) {
        const want = exchangeOptions[0];
        if (_exchange !== want) _exchange = want;
      } else if (!valid && _resolvedExchange) {
        _exchange = _resolvedExchange;
      }
      if (!productOptions.includes(_product)) {
        _product = productVal;
      }
    });
  });
  // Wave C: _mode is READ from $executionMode store unconditionally.
  // The store is the single source of truth for execution mode (set
  // by the navbar dropdown). Earlier this resolver filtered against
  // availableModes — defaulting to ['draft', 'live'] — which silently
  // forced the ticket into draft mode when the navbar said PAPER
  // (because 'paper' wasn't in ['draft', 'live']), making Submit
  // save a local draft instead of placing the order. CRITICAL BUG:
  // operator clicks Place, nothing visible happens.
  //
  // The new derivation normalises sim/replay/shadow → 'paper' (those
  // modes route through the paper engine on the backend; the backend
  // then decides whether they actually hit a broker based on
  // SimDriver.active / execution.shadow_mode / etc). 'paper' and
  // 'live' pass through unchanged. We never resolve to 'draft' from
  // the store — drafts are a deliberate operator choice that requires
  // an explicit `mode='draft'` prop from the caller.
  function _resolveInitialMode() {
    // Wave-C / dead-prop sweep: defaultMode + availableModes props
    // removed; the navbar's executionMode store is the sole source.
    const fromStore = get(executionMode) || 'paper';
    if (fromStore === 'sim' || fromStore === 'replay' || fromStore === 'shadow') return 'paper';
    if (fromStore === 'paper' || fromStore === 'live') return fromStore;
    return 'paper';
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
  // Live-track the navbar mode store. When operator changes the
  // navbar pill while the modal is open, the ticket's mode reflects
  // the new choice immediately — submit will route through whatever
  // mode is visibly shown in the read-only hint above the Submit
  // button. Earlier the initial mode was latched at mount and never
  // updated, so a stale 'draft' (from the old availableModes filter
  // bug) lingered for the entire modal lifetime.
  $effect(() => {
    const m = $executionMode || 'paper';
    const normalised = (m === 'sim' || m === 'replay' || m === 'shadow') ? 'paper'
                     : (m === 'paper' || m === 'live') ? m
                     : 'paper';
    untrack(() => {
      if (mode === undefined && _modeInternal !== normalised && _modeInternal !== 'draft') {
        _modeInternal = normalised;
      }
    });
  });
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
  // onDepthQuote. Operator request: pre-fill the limit price with
  // LTP (last traded price) so it reads as "buy/sell at the
  // current market price by default". Falls through to the
  // marketable side (BUY → ask, SELL → bid) when LTP is missing
  // (off-hours, just-listed contracts with no trades yet). Once
  // the operator types into the field, `_priceTouched` flips
  // true and we stop overwriting their input.
  // Caller can pre-supply `price` to suppress auto-fill (e.g. a
  // close-position flow that wants the operator's last limit).
  // Untrack the read so we capture the initial value once — this
  // is intentional, the operator's edits flip the flag from there.
  let _priceTouched = $state(untrack(() => typeof price === 'number' && price > 0));
  /** @type {{ bid: number|null, ask: number|null, ltp: number|null } | null} */
  let _lastQuote = $state(null);

  // Per-instrument tick size + decimals. Kite ships tick_size on
  // every instrument row (CRUDEOIL ₹1.00, GOLDM ₹1.00, NSE equity
  // & F&O ₹0.05, USDINR ₹0.0025, …). Reading from the cache means
  // the operator can't type a ₹100.07 stock order that Kite would
  // reject with "price not as per tick size" — the input snaps to
  // the nearest valid multiple as soon as it loses focus.
  //
  // _tickSize falls back to 0.05 when the cache hasn't resolved a
  // tick for the symbol (instruments not loaded yet, or hypothetical
  // symbol typed into a draft ticket). 0.05 covers NSE equity / F&O
  // which is the majority case.
  const _tickSize = $derived.by(() => {
    const sym = String(_resolvedSymbol || symbol || '').toUpperCase();
    if (!sym) return 0.05;
    const inst = getInstrument(sym);
    const ts = Number(inst?.ts);
    return Number.isFinite(ts) && ts > 0 ? ts : 0.05;
  });
  // Decimals derived from the tick — 0.05 → 2dp, 1.00 → 0dp, 0.0025
  // → 4dp. Capped at 4 so a freak rounding ts like 0.00001 doesn't
  // blow up the input. The label chip + formatter both read this.
  const _tickDecimals = $derived.by(() => {
    const s = String(_tickSize);
    const dot = s.indexOf('.');
    return dot < 0 ? 0 : Math.min(4, s.length - dot - 1);
  });
  // Kite rejects orders whose price isn't an exact tick multiple —
  // the bid/ask from depth ARE tick-aligned, but JS floating-point
  // can turn 590.80 into 590.7999999999999 which Kite then refuses.
  // Default tick = `_tickSize` (the symbol's actual tick); callers
  // can override only for hypothetical paths where the symbol isn't
  // known yet.
  function _roundToTick(/** @type {number|string} */ px,
                        /** @type {number} */ tick = _tickSize) {
    const n = Number(px);
    if (!Number.isFinite(n) || n <= 0) return n;
    const t = tick > 0 ? tick : 0.05;
    return Math.round((n / t) + Number.EPSILON) * t;
  }
  function _formatTick(/** @type {number} */ n) {
    // Render with the tick's natural decimals. CRUDEOIL ₹1.00 reads
    // as "8200" not "8200.00"; NSE ₹0.05 reads as "590.80" not "590.8".
    if (!Number.isFinite(n)) return n;
    return Number(n.toFixed(_tickDecimals));
  }
  // Snap operator-typed price / trigger to the nearest tick on blur.
  // Without this the operator could type 100.07 into an NSE stock
  // (₹0.05 tick) and the order would round-trip to Kite → reject.
  // Skips empty / zero values so blur with nothing typed doesn't
  // populate the field with "0".
  function _snapPriceField(/** @type {'price'|'trigger'} */ which) {
    const raw = which === 'price' ? _price : _trigger;
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) return;
    const snapped = _formatTick(_roundToTick(n));
    const next = String(snapped);
    if (which === 'price') _price = next;
    else                   _trigger = next;
  }
  function _autoFillFromQuote() {
    if (_priceTouched) return;
    if (_type !== 'LIMIT' && _type !== 'SL') return;
    if (!_lastQuote) return;
    // LTP first — the operator-facing "current market price".
    // Marketable side (BUY → ask, SELL → bid) is the fallback for
    // off-hours / illiquid contracts where ltp is null. Without
    // this preference order, LIMIT orders pre-filled at the
    // worst-marketable-side price even when LTP was a tighter
    // anchor, which forced the operator to retype every time.
    const marketable = _side === 'BUY' ? _lastQuote.ask : _lastQuote.bid;
    const pick = (_lastQuote.ltp && _lastQuote.ltp > 0)
      ? _lastQuote.ltp
      : ((marketable && marketable > 0) ? marketable : null);
    if (pick && pick > 0) _price = _formatTick(_roundToTick(pick));
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
  // account has enough room to place this order. Sourced from the
  // module-level fundsStore singleton (three-tier cache, TOTAL row
  // pre-stripped). On mount _refetchFunds() calls fundsStore.load()
  // which either serves from cache instantly or fires a broker fetch.
  // Each row carries: { account, cash, avail_margin, used_margin, collateral }
  /** @type {Array<{account:string, cash:number, avail_margin:number,
   *                used_margin:number, collateral:number}>} */
  const _funds = $derived(fundsStore.value ?? []);
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
  // Current canonical order map (module-level singleton, kept in sync by
  // accountDisplayOrder.subscribe below).
  let _otOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubOtOrder = accountDisplayOrder.subscribe(m => { _otOrderMap = m; });

  const _accounts = $derived.by(() => {
    const fromProp = (accounts || []).filter(_isRealAcct);
    const raw = fromProp.length ? fromProp : _selfAccounts.filter(_isRealAcct);
    return sortAccountsBy(raw, _otOrderMap);
  });

  // Account — explicit operator choice for which Kite handle the
  // order routes through. Required for PAPER and LIVE; ignored in
  // DRAFT. Initialized empty and reactively seeded from the prop /
  // picker via the effect below — this lets a late-arriving caller
  // account list (the common race: /api/accounts/ resolves AFTER
  // the operator clicks +) auto-select once it lands. A masked
  // ZG#### is unroutable so we never seed it as a default.
  let _account = $state('');

  // Slice 7b — strategy picker. Loaded once on mount, refreshed
  // on `bump` (parent's manual refresh). Sourced from active
  // strategies only. Demo / observer / read-only roles still see
  // the picker but it carries an "(unattributed)" default since
  // they can't actually place an order.
  /** @type {{id: number, slug: string, name: string}[]} */
  let _strategies = $state([]);
  let _strategyId = $state(/** @type {number|null} */ (null));
  let _strategiesLoaded = $state(false);
  async function _loadStrategies() {
    try {
      const r = await fetchStrategies({ activeOnly: true });
      _strategies = Array.isArray(r?.rows)
        ? r.rows.map(s => ({ id: s.id, slug: s.slug, name: s.name }))
        : [];
    } catch (_) {
      // Demo / unauth: server still 200s for view_strategies; this
      // catch is for total network failure. Keep _strategies empty.
      _strategies = [];
    } finally {
      _strategiesLoaded = true;
    }
  }
  $effect(() => {
    if (!_strategiesLoaded) {
      _loadStrategies();
    }
  });

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
  // hitting the broker. v2 API (2026-07-08): operator inputs LOTS for
  // F&O, so the G1 multiple-of-lot check is obsolete — qty = lots ×
  // lotSize is a valid multiple by construction. Only the 5-lot fat-
  // finger cap remains.

  /**
   * Validate qty / lots fields. Returns an error string or null.
   * @param {number} qty
   * @param {number} lots
   * @param {number} lotSize
   * @param {string} sym
   * @returns {string | null}
   */
  function _validateQtyLots(qty, lots, lotSize, sym) {
    if (!qty || qty <= 0) {
      if (lotSize > 0) return `Qty required (1 lot = ${lotSize} for ${formatSymbol(sym)})`;
      return 'Qty required';
    }
    // Fat-finger 5-lot cap — operator 2026-07-01: "the code by
    // mistake ordered 100 lots instead of 1 lot. qty vs lot issue
    // exists in order placement. add additional guard." Applies only
    // when lot_size > 1 (F&O). Backend enforces the same limit as a
    // defense-in-depth layer; front here shows a clear message before
    // Submit is attempted.
    // Close orders are exempt — the operator must be able to exit a
    // position larger than 5 lots without the safety cap blocking them.
    if (lotSize > 1 && lots > 5 && classifyIntent(currentQty, _side) !== 'close') {
      return `Refusing ${lots} lots — the 5-lot safety cap prevents fat-finger errors. Reduce lots to ≤5.`;
    }
    return null;
  }

  /**
   * Validate price / trigger fields including tick-alignment.
   * Returns an error string or null.
   * @param {boolean} needsLimit
   * @param {boolean} needsTrigger
   * @param {number} price
   * @param {number} trigger
   * @param {number} tickSize
   * @param {number} tickDecimals
   * @returns {string | null}
   */
  function _validatePriceTrigger(needsLimit, needsTrigger, price, trigger, tickSize, tickDecimals) {
    if (needsLimit   && !price)   return 'Limit price required';
    if (needsTrigger && !trigger) return 'Trigger price required';
    if (price   < 0) return 'Price must be ≥ 0';
    if (trigger < 0) return 'Trigger must be ≥ 0';
    // Tick-alignment check — blur snap should catch operator-typed
    // input, but a draft loaded from an older session may carry a
    // stale non-aligned price. Tolerance is one-tenth of a tick so
    // floating-point noise (590.7999999999999) doesn't trip the gate.
    if (tickSize > 0) {
      const tol = tickSize / 10;
      const aligned = (/** @type {number} */ n) =>
        Math.abs(n - Math.round(n / tickSize) * tickSize) < tol;
      if (needsLimit   && !aligned(price))   return `Price must be a multiple of ₹${tickSize.toFixed(tickDecimals)}`;
      if (needsTrigger && !aligned(trigger)) return `Trigger must be a multiple of ₹${tickSize.toFixed(tickDecimals)}`;
    }
    return null;
  }

  /**
   * Validate order context (account, mode). Returns an error string or null.
   * @param {string} mode
   * @param {string} acct
   * @returns {string | null}
   */
  function _validateOrderContext(mode, acct) {
    if ((mode === 'paper' || mode === 'live') && !acct) return 'Pick an account';
    return null;
  }

  const validationErr = $derived.by(() =>
    _validateQtyLots(Number(_qty), Number(_lots), _lotSize, symbol) ??
    _validatePriceTrigger(showLimit, showTrigger, Number(_price), Number(_trigger), _tickSize, _tickDecimals) ??
    _validateOrderContext(_mode, _account) ??
    ''
  );

  // Pipe validation state to the host (SymbolPanel) so its common-action
  // footer can surface the error as a toast when Submit is clicked while
  // a validation block is active (e.g. limit price not yet filled by the
  // depth ladder poll). Without this the host has no way to know why the
  // silent early-return in submit() fired.
  $effect(() => {
    onValidationChange?.(validationErr);
  });

  // True when no symbol has been resolved yet — disables all form
  // controls below the symbol picker so the operator can't accidentally
  // submit a blank order. Cleared as soon as the picker resolves a
  // tradeable symbol (_resolvedSymbol) or the caller passed one in (symbol).
  const _noSymbol = $derived(!_resolvedSymbol && !symbol);

  /** Build a basket-leg payload from the modal's current state.
   *  Caller (admin/options) folds this into chainBasket so the leg
   *  renders as a regular basket pill alongside quick-adds. */
  function _basketPayload() {
    return {
      side:       _side,
      sym:        symbol,
      exchange:   _exchange || _resolvedExchange || exchange || 'NFO',
      account:    _account,
      lots:       Math.max(1, Number(_lots) || 1),
      lotSize:    Number(_lotSize) || 1,
      product:    _product,
      limit:      showLimit ? Number(_roundToTick(_price)) || 0 : 0,
      chaseAgg:   showLimit && _chase ? _chaseAgg : 'low',
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
      if (submitting) return;
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
  // Demo-mode submit modal — opens when an anonymous prod visitor
  // clicks Submit. Replaces the silent-disable UX with a friendly
  // explanation of demo mode + sign-in CTA + Hire Me CTA.
  let _demoSubmitOpen = $state(false);
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
    // pairedCount > 0 when the preflight margin number reflects parent
    // + wing (or any other auto-attached leg). Surfaced to the shell
    // chip so the Req value reads "Req ₹X · +wing" instead of being
    // mistaken for the naked-short margin.
    const pairedCount = _previewPlan?.wing ? 1 : 0;
    return {
      isCashMode,
      cash:         _accountFunds ? Number(_accountFunds.cash || 0)         : null,
      availMargin:  _accountFunds ? Number(_accountFunds.avail_margin || 0) : null,
      usedMargin:   _accountFunds ? Number(_accountFunds.used_margin || 0)  : null,
      fundsAccount: _accountFunds ? String(_accountFunds.account || '')     : '',
      kind,
      side: _side,
      pairedCount,
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

  // Fire onPreviewPlanUpdate whenever the per-leg preview state changes.
  // Same dedup pattern as onMarginUpdate but cheaper since the dict is
  // already a stable reference. The shell renders the chip inside the
  // Template container so it's visible on BOTH Ticket and Chain tabs.
  let _lastPreviewKey = '';
  $effect(() => {
    if (!onPreviewPlanUpdate) return;
    const key = JSON.stringify({
      p: _previewPlan, l: _previewLoading, e: _previewError, c: _templateCapWarning,
    });
    if (key === _lastPreviewKey) return;
    _lastPreviewKey = key;
    onPreviewPlanUpdate(_previewPlan, _previewLoading, _previewError, _templateCapWarning);
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
    if (suspended) {
      return;
    }
    // Operator: "user needs to choose [a side]. because of that margin
    // chip will not be displayed in the beginning." Gate the preflight
    // on _side being non-null so the chip stays empty until the
    // operator clicks BUY or SELL.
    if (_isDemo || !_account || !symbol || !_side || Number(_qty) <= 0 || _mode === 'draft') {
      _marginPreview = null;
      _marginLoading = false;
      // `_chipMeta` is read via untrack so it doesn't add itself to the
      // effect's dep set — otherwise template-preview cycles (which
      // update _previewPlan.wing → _chipMeta.pairedCount) re-fire the
      // preflight unnecessarily. The chip emitter `$effect` above
      // handles every chipMeta change on its own.
      onMarginUpdate?.(null, false, untrack(() => _chipMeta));
      return;
    }
    _marginLoading = true;
    onMarginUpdate?.(_marginPreview, true, untrack(() => _chipMeta));
    _marginTimer = setTimeout(async () => {
      try {
        // Paired legs — when the template preview has resolved a wing,
        // factor it into the basket_margin call so the operator sees
        // the BRACKETED-strategy margin (parent SELL net of protective
        // BUY) instead of the scarier naked-short margin.
        const _wing = _previewPlan?.wing;
        const paired_legs = (_wing && _wing.tradingsymbol)
          ? [{
              tradingsymbol:    _wing.tradingsymbol,
              exchange:         _wing.exchange || _exchange,
              transaction_type: _wing.transaction_type || 'BUY',
              quantity:         Number(_wing.quantity) || Number(_qty),
              product:          _wing.product || _product,
              order_type:       _wing.order_type || 'MARKET',
              price:            Number(_wing.estimated_price) || 0,
            }]
          : [];
        const payload = {
          account: _account,
          tradingsymbol: _resolvedSymbol || symbol,
          exchange: _exchange || _resolvedExchange || exchange || 'NFO',
          quantity: Number(_qty),
          side: _side,
          order_type: _type,
          product: _product,
          variety: _variety,
          validity: _validity,
          price: showLimit ? Number(_price) || 0 : 0,
          trigger_price: showTrigger ? Number(_trigger) || 0 : 0,
          intent: classifyIntent(currentQty, _side),
          paired_legs,
        };
        _marginPreview = await previewOrderMargin(payload);
      } catch (e) {
        _marginPreview = { error: (e?.message || 'preview failed').slice(0, 60) };
      } finally {
        _marginLoading = false;
        // setTimeout callback runs outside the tracking frame so this
        // _chipMeta read doesn't track regardless — keeping it bare for
        // readability. The chip emitter $effect picks up changes.
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
    // Demo session — short-circuit before any validation or broker
    // call. Open the friendly "Demo mode" modal instead of silently
    // disabling Submit; recruiter sees the affordance + understands
    // why it doesn't fire. Real placement requires a signed-in
    // trader role per the RBAC matrix.
    if (_isDemo) {
      _demoSubmitOpen = true;
      return;
    }
    // Engine-idle guard — when the navbar mode is IDLE (dev only), the
    // backend's set_mode hasn't activated dev_active=True yet, so submit
    // would land but no broker calls would fire (background tasks all
    // short-circuit). Refuse with a clear hint instead of letting the
    // operator click into a silent black hole.
    if (get(executionMode) === 'idle') {
      submitErr = 'Engine is idle — pick PAPER / SIM / REPLAY from the navbar before placing orders.';
      return;
    }
    if (validationErr) return;

    // ── action='modify' branch ─────────────────────────────────
    // Modifying an existing working order — bypass the place/ticket
    // pipeline entirely. Mode pills + chase + L/M/H don't apply.
    if (action === 'modify') {
      if (!orderId) {
        submitErr = 'Modify path requires an order id.';
        return;
      }
      submitting = true; submitErr = ''; submitOk = '';
      try {
        const modPayload = buildModifyPayload({
          account: _account,
          qty: _qty,
          showLimit,
          showTrigger,
          roundToTick: _roundToTick,
          price: _price,
          trigger: _trigger,
          type: _type,
          variety: _variety,
          validity: _validity,
        });
        await modifyOrder(orderId, modPayload);
        submitOk = `Order #${orderId} modified`;
        // Surface the diff to the caller so the page can refresh
        // its order list / log the change. Modal stays open with the
        // Exit button until the operator dismisses it.
        await onSubmit({ action: 'modify', orderId, ...modPayload });
      } catch (e) {
        submitErr = /** @type {any} */ (e)?.message || String(e);
      } finally {
        submitting = false;
      }
      return;
    }

    // ── paper / live / draft path ───────────────────────────────
    // LIVE submits straight through — no confirm dialog. The pre-submit
    // margin/cost row above the Submit button already tells the operator
    // exactly what they're committing to. Backend still enforces both
    // gates (prod-branch + execution.paper_trading_mode=False) before
    // any broker call, so accidental clicks on dev or in paper mode
    // can't reach Kite.
    const submitCtx = {
      mode: _mode,
      action,
      symbol,
      exchange,
      side: _side,
      qty: _qty,
      product: _product,
      type: _type,
      variety: _variety,
      validity: _validity,
      showLimit,
      showTrigger,
      roundToTick: _roundToTick,
      price: _price,
      trigger: _trigger,
      account: _account,
      // Chase only carries on price-bearing order types; MARKET /
      // SL-M ignore it on the backend, but we still ship the flag
      // so the AlgoOrder row records the intent for replay.
      chase: _chase,
      chaseAgg: _chaseAgg,
    };
    const payload = buildOnSubmitPayload(submitCtx);
    submitting = true; submitErr = ''; submitOk = '';
    /** @type {any} */
    let brokerResp = null;
    try {
      // PAPER + LIVE both route through the backend. DRAFT hands off
      // to the caller's onSubmit only (no API call).
      if (_mode === 'paper' || _mode === 'live') {
        const placeCtx = {
          ...submitCtx,
          resolvedSymbol: _resolvedSymbol,
          resolvedExchange: _resolvedExchange || '',
          lots: _lots,
          lotSize: _lotSize,
          currentQty,
          templateId,
          tpOverride,
          slOverride,
          wingPremPctOverride,
          wingStrikeOffsetOverride,
          strategyId: _strategyId,
        };
        brokerResp = await placeTicketOrder(buildPlacePayload(placeCtx));
        // Show inline confirmation so the operator sees the order
        // landed; modal stays open until the operator clicks Exit.
        // Backend returns {order_id, mode, status, detail}.
        submitOk = formatPlacementOk({
          mode:         _mode,
          side:         _side,
          qty:          _qty,
          symbolLabel:  formatSymbol(symbol),
          showLimit,
          price:        _price,
          roundedPrice: _roundToTick(_price),
          orderId:      brokerResp?.order_id || '?',
        });
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
      // button so the operator can read the placed-order line and
      // place more without re-opening. Explicit close via × / Escape.
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
  let _escCleanup = $state(/** @type {(() => void) | null} */ (null));

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
        if (e.key === 'Escape') {
          if (submitting) return;
          onClose();
        }
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
    // Legacy `algo.default_target_pct` setting fetch removed in audit
    // pass 6 alongside the _targetPct state field. The Template picker
    // is the canonical TP/SL surface now; setting is preserved server-
    // side for back-compat but the modal no longer reads it.

    // Load the OrderTemplate catalog from the module-level cache so
    // repeated modal opens don't re-hit the DB. The cache kicks in
    // its first fetch at module-evaluation time, so by the time the
    // first modal opens the rows are usually already warm.
    loadOrderTemplates()
      .then(/** @param {any[]} rows */ (rows) => {
        _templates = rows.filter(t => t.is_active);
        _autoSelectTemplate();
      })
      .catch(() => { /* silent — picker stays empty */ });

    return () => _escCleanup?.();
  });

  // Final-teardown sweep: cancel any in-flight debounce timers so
  // their async setTimeout callbacks don't fire $state writes into a
  // destroyed component. The $effects that own these timers also
  // clear them on re-run, but they don't run on unmount.
  onDestroy(() => {
    _unsubOtOrder();
    if (_marginTimer)  { clearTimeout(_marginTimer);  _marginTimer  = null; }
    if (_previewTimer) { clearTimeout(_previewTimer); _previewTimer = null; }
  });

  // Re-sync when the catalog mutates elsewhere (e.g. operator edits a
  // template on /automation/templates while the modal is open). Pure
  // subscription — no network cost.
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows && rows.length) {
      _templates = rows.filter(t => t.is_active);
    }
  });

  // Side-flip auto-update $effect removed when templateId became a
  // shared SymbolPanel-level state (commit 108ce3a5) — operator's
  // pick persists across tabs and side flips. The `_autoSelectTemplate`
  // function itself is still called once on mount (line ~1454 onMount
  // callback) to pick the side-aware default when templateId arrives
  // null. To restore a side-aware default after a side flip, click
  // the Default pill in the template row.

  // Pre-submit preview — debounced fetch so an operator typing in the
  // override fields doesn't fire a request per keystroke.
  //
  // CRITICAL: `_previewTimer` MUST be a plain `let`, NOT `$state`. The
  // $effect below reads it (`if (_previewTimer) clearTimeout(...)`) AND
  // writes it (`_previewTimer = setTimeout(...)`); declaring it as
  // $state creates a read+write loop inside the same effect that
  // re-queues itself every 200 ms, saturating the scheduler and
  // hanging the entire modal. Timer handles are internal cleanup
  // state, not reactive UI state.
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _previewTimer = null;
  $effect(() => {
    // Track the inputs that affect the preview.
    const inputs = [
      templateId, tpOverride, slOverride,
      wingPremPctOverride, wingStrikeOffsetOverride,
      _side, symbol, _qty, _price,
    ];
    // Skip preview when basics are incomplete. Critically — guard on
    // empty `_account` too: an operator who opens the modal before
    // an account is picked would otherwise generate one 400 every
    // time they tweak the override fields (the backend rejects an
    // empty account upfront). The catalog + auto-select still run
    // on mount; only the preview API call is debounced behind a
    // valid account.
    if (!symbol || Number(_qty) <= 0 || !_account
        || (templateId === null && !tpOverride && !slOverride)) {
      _previewPlan = null;
      _previewError = '';
      return;
    }
    if (_previewTimer) clearTimeout(_previewTimer);
    // Sequence guard — every fetch captures its own `seq` at start.
    // When the response lands, we only assign if `seq` still matches
    // the latest dispatched value. Without this, two preview calls
    // fired back-to-back (operator flipping templates faster than
    // the 200ms debounce + backend latency) could land
    // out-of-order: the older call's response overwrites the newer
    // call's response → "on fill" preview shows the template the
    // operator already moved away from. Symptom: preview chips
    // never sync with the template selector.
    _previewSeq++;
    const seq = _previewSeq;
    _previewTimer = setTimeout(async () => {
      _previewLoading = true; _previewError = '';
      // Snapshot the dispatching state so the comparison after
      // await checks the EXACT template_id we sent — not whatever
      // templateId says by the time the response lands.
      const dispatchedTemplateId = templateId;
      try {
        const refPx = Number(_price) > 0 ? Number(_price) : 0;
        const res = await previewTicketTemplate({
          mode:             _mode === 'draft' ? 'paper' : _mode,
          side:             _side,
          tradingsymbol:    symbol,
          quantity:         Number(_qty),
          exchange:         _exchange || _resolvedExchange || exchange || 'NFO',
          product:          _product || 'NRML',
          account:          _account || '',
          reference_price:  refPx,
          template_id:                  dispatchedTemplateId,
          tp_pct_override:              tpOverride !== '' ? Number(tpOverride) : null,
          sl_pct_override:              slOverride !== '' ? Number(slOverride) : null,
          wing_premium_pct_override:    wingPremPctOverride !== '' ? Number(wingPremPctOverride) : null,
          wing_strike_offset_override:  wingStrikeOffsetOverride !== '' ? Number(wingStrikeOffsetOverride) : null,
        });
        // Drop stale responses — a newer fetch has been dispatched.
        if (seq !== _previewSeq) return;
        _previewPlan = res?.plan || null;
      } catch (e) {
        if (seq !== _previewSeq) return;
        _previewError = e?.message || 'preview failed';
        _previewPlan = null;
      } finally {
        if (seq === _previewSeq) _previewLoading = false;
      }
    }, 200);
  });

  function _refetchFunds() {
    // Delegates to the module-level store singleton. force=true bypasses
    // the inflight dedup so a manual refresh always gets a fresh broker
    // snapshot rather than riding an existing in-flight request.
    fundsStore.load(undefined, { force: true });
  }

  // Host-triggered refresh — when refreshKey bumps, re-fetch funds so
  // the avail-margin chip and the modal-wide funds line both reflect
  // the latest balances after a fill/cancel elsewhere.
  $effect(() => {
    if (refreshKey > 0) _refetchFunds();
  });
</script>

<!-- When standalone=false (embedded in SymbolPanel), the overlay backdrop
     is hidden via .ot-overlay-embedded and role="dialog" is omitted to
     avoid nested dialog ARIA roles. The inner ot-modal remains the visual
     container; SymbolPanel's own dialog role already covers it. -->
<div class="ot-overlay" class:ot-overlay-embedded={!standalone}
     role="presentation"
     onclick={standalone ? onClose : undefined}>
  <div class="ot-modal" role={standalone ? 'dialog' : 'document'}
       aria-modal={standalone ? 'true' : undefined}
       aria-label={standalone ? 'Place order' : undefined}
       onclick={(e) => e.stopPropagation()}>
    <div class="ot-header">
      <div class="ot-symbol">
        <span class="ot-symbol-text"><LegLabel sym={symbol} exchange={exchange || ''} /></span>
        <span class="ot-symbol-meta">
          {exchange ? exchange + ' · ' : ''}
          {kind}{_lotSize ? ' · lot ' + _lotSize : ''}
          {action !== 'open' ? ' · ' + action.toUpperCase() : ''}
        </span>
      </div>
      <button type="button" class="ot-close" title="Close" aria-label="Close" onclick={onClose} disabled={submitting}>×</button>
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
      <!-- Side as a two-pill toggle. Labels flip to ADD / CLOSE when
           there's an existing position (sideLabels logic), BUY / SELL
           otherwise. Operator: "make dropdown as a toggle between add
           close or buy sell. use existing space for dropdown for this
           without taking more space." Sized to match the surrounding
           Selects (1.55 rem height, same knob flex slot) so the row
           stays a single-pass horizontal sweep. -->
      <SideToggle
        bind:side={_side}
        {currentQty}
        disabled={_noSymbol}
        locked={action === 'modify'}
        onChange={(s) => onSideChange?.(s)}
      />
      <OrderKnobsRow
        bind:type={_type}
        bind:product={_product}
        bind:variety={_variety}
        bind:validity={_validity}
        exchange={_exchange}
        onExchangeChange={(v) => { _exchange = String(v); _exchangeTouched = true; }}
        disabled={_noSymbol}
        {productOptions}
        {exchangeOptions}
      />
      <!-- Strategy attribution (slice 7b). Optional in v1 — None /
           "—" means "no strategy" and the AlgoOrder.strategy_id is
           saved as NULL. Picker reads active strategies from
           /api/strategies; if the operator hasn't created any yet
           the picker stays at "—" and the order saves un-attributed.
           Tightens to required once the operator workflow settles
           and a default strategy is mandated per role. -->
      {#if _strategies.length > 0}
        <div class="ot-knob ot-knob-strategy">
          <label class="ot-label" for="ot-strategy-sel">Strategy</label>
          <Select id="ot-strategy-sel"
                  value={_strategyId == null ? '' : String(_strategyId)}
                  ariaLabel="Strategy"
                  onValueChange={(v) => { _strategyId = v ? Number(v) : null; }}
                  options={[
                    { value: '', label: '—' },
                    ..._strategies.map(s => ({ value: String(s.id), label: s.slug })),
                  ]} />
        </div>
      {/if}
    </div>

    <!-- Lots/Qty + Limit price (or Trigger when no limit) — single
         row, 65% / 35% split per operator request. Trigger gets its
         own row below when both showLimit AND showTrigger (SL). -->
    <div class="ot-row ot-lots-price-row">
      <div class="ot-label-block ot-lots-cell">
        <QtyInput
          bind:lots={_lots}
          bind:qty={_qty}
          lotSize={_lotSize}
          {isEquity}
          disabled={_noSymbol}
          onTouch={() => { _lotsTouched = true; }}
        />
      </div>
      {#if showLimit}
        <div class="ot-label-block ot-price-cell">
          <label class="ot-label" for="ot-price">
            Limit price
            <span class="ot-tick-chip" title="Kite rejects prices not aligned to this tick. The field snaps on blur.">tick ₹{_tickSize.toFixed(_tickDecimals)}</span>
            {#if _priceTouched && _lastQuote}
              <button type="button" class="ot-price-reset"
                      title="Re-arm auto-fill — restore {_side === 'BUY' ? 'top ask' : 'top bid'}"
                      aria-label="Reset price to depth"
                      onclick={() => { _priceTouched = false; _autoFillFromQuote(); }}>↺</button>
            {/if}
          </label>
          <input id="ot-price" type="number" class="ot-input ot-num"
                 step={_tickSize}
                 bind:value={_price}
                 disabled={_noSymbol}
                 oninput={() => { _priceTouched = true; }}
                 onblur={() => _snapPriceField('price')} />
        </div>
      {:else if showTrigger}
        <div class="ot-label-block ot-price-cell">
          <label class="ot-label" for="ot-trigger">
            Trigger
            <span class="ot-tick-chip" title="Kite rejects prices not aligned to this tick. The field snaps on blur.">tick ₹{_tickSize.toFixed(_tickDecimals)}</span>
          </label>
          <input id="ot-trigger" type="number" class="ot-input ot-num"
                 step={_tickSize}
                 bind:value={_trigger}
                 disabled={_noSymbol}
                 onblur={() => _snapPriceField('trigger')} />
        </div>
      {/if}
    </div>

    <!-- Trigger row only when BOTH limit AND trigger are required (SL). -->
    {#if showLimit && showTrigger}
      <div class="ot-row">
        <div class="ot-label-block">
          <label class="ot-label" for="ot-trigger">
            Trigger
            <span class="ot-tick-chip" title="Kite rejects prices not aligned to this tick. The field snaps on blur.">tick ₹{_tickSize.toFixed(_tickDecimals)}</span>
          </label>
          <input id="ot-trigger" type="number" class="ot-input ot-num"
                 step={_tickSize}
                 bind:value={_trigger}
                 disabled={_noSymbol}
                 onblur={() => _snapPriceField('trigger')} />
        </div>
      </div>
    {/if}

    <!-- Template attachment — exit-rule preset chosen at submit time.
         Replaces the legacy "Target take-profit" row from v1. The
         dropdown lists every active OrderTemplate; the inline override
         fields let the operator tune one order without saving. On
         submit, /api/orders/ticket runs the unified template-attach
         pipeline (sim or live based on SimDriver.active). The preview
         line below shows exactly what TP/SL/Wing will be placed.
         Industry analogue: NinjaTrader ATM Strategy attachment. -->
    <!-- Template Default/None toggle + override inputs were removed
         from OrderTicket because the shell-level "On fill" picker
         (SymbolPanel) renders the same picker + the same four
         override fields ABOVE the tab body. Pre-fix the Ticket tab
         carried TWO template controls (shell + internal) while the
         Chain tab carried one — same logical concept, asymmetric UI
         between tabs. Operator: "On order template info shown, while
         it is not shown on chain." Cap warning + on-fill preview
         chip survive here because they're per-leg context (broker
         capability vs the resolved leg, fill-time TP/SL/Wing trigger
         prices computed against the entered limit) that the Chain
         tab's multi-leg basket can't surface directly. -->
    <!-- On-fill preview chip + cap warning lifted to the shell-level
         Template container in SymbolPanel — visible on BOTH Ticket and
         Chain tabs now. Operator: "on fill chip text can be shown in
         chain also, as it clearly tells what is going to happen. when
         changing these values chip should get updated. it can be part
         of template container for both chain and order ticket." The
         `onPreviewPlanUpdate` callback above fires whenever
         _previewPlan / _previewLoading / _previewError /
         _templateCapWarning change, so the shell stays in lockstep
         with the Ticket form's reactive state. -->


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
      exchange={_exchange || _resolvedExchange || exchange || 'NFO'}
      {refreshKey}
      paused={suspended}
      onQuote={onDepthQuote} />

    <!-- Chase row — only relevant when *placing* a new order.
         action='modify' bypasses the place-pipeline entirely
         (PUT /api/orders/{id} hits the broker directly).
         Suppressed when `modeChaseHidden` is true — the host
         (SymbolPanel) renders the same controls in its shared
         toolbar so Chain + Ticket both read from one toolkit.

         MODE PILLS REMOVED (Wave C): execution mode is set
         EXCLUSIVELY via the navbar dropdown. The ticket reads
         `_mode` from `$executionMode` store via _resolveInitialMode
         (normalised sim/replay → paper for surfaces that only
         expose draft/paper/live pills). Per-ticket pill picking
         contradicted the "one mode chooser" UX rule and let the
         operator submit in a mode different from the navbar
         pill — bug surface. Submit derives mode from the store
         at click time. -->
    {#if action !== 'modify' && !modeChaseHidden}
    <div class="ot-mode-row">
      <!-- Read-only mode hint so the operator can see at-a-glance
           what mode the ticket will submit in. Source of truth is
           the navbar pill; click it to change mode. -->
      <span class="ot-mode-hint" title="Execution mode is set via the navbar dropdown. Submit will route through this mode.">
        Mode <span class="ot-mode-hint-val ot-mode-hint-{_mode}">{_mode.toUpperCase()}</span>
        <span class="ot-mode-hint-src">(navbar)</span>
      </span>

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
          <ChaseAggPicker value={_chaseAgg} onChange={_setChaseAgg} />
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
                    disabled={!!validationErr || submitting || _noSymbol}
                    title="Add this leg to the basket — place every leg together later"
                    onclick={addToBasket}>+ Basket</button>
          {/if}
          {#if basketMode && action !== 'modify'}
            <button type="button" class="ot-submit ot-submit-basket-mode"
                    disabled={!!validationErr || _noSymbol}
                    onclick={addToBasket}>
              + Add to basket
            </button>
          {:else}
            <!-- Direct-submit button. Operator: "I tried closing position
                 using the order, it didn't place the order" — the
                 unified-basket spec retired this for action='open' (the
                 basket path replaces it), but action='close' /
                 action='modify' / action='repeat' flows still need a
                 standalone Submit because basket isn't the right vehicle
                 for those. Label varies by action so the operator knows
                 what's about to fire. -->
            <button type="button" class="ot-submit"
                    class:ot-submit-buy={_side === 'BUY'}
                    class:ot-submit-sell={_side === 'SELL'}
                    class:ot-submit-demo={_isDemo}
                    disabled={_isDemo ? false : (!!validationErr || submitting || _noSymbol)}
                    title={_isDemo ? 'Demo mode — click to learn how to enable real orders' : ''}
                    onclick={submit}>
              {#if _isDemo}Submit (Demo){:else if submitting}…
              {:else if action === 'modify'}Modify{orderId ? ' · #' + orderId : ''}
              {:else if action === 'close'}Close · {_side.toLowerCase()}
              {:else if action === 'repeat'}Place again
              {:else if sideLabels[_side] === 'ADD'}Add · {_side.toLowerCase()}
              {:else}Place {_side.toLowerCase()}{/if}
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

<ModalShell
  open={_demoSubmitOpen}
  onClose={() => (_demoSubmitOpen = false)}
  ariaLabel="Demo mode"
  zIndex={300}
>
  <div class="ot-demo-modal"
       onkeydown={(e) => e.stopPropagation()}>
    <button type="button" class="ot-demo-close" aria-label="Close"
            onclick={() => _demoSubmitOpen = false}>×</button>
    <h3 id="ot-demo-title" class="ot-demo-title">Demo mode</h3>
    <p class="ot-demo-body">
      You're viewing the live RamboQuant terminal as an anonymous
      visitor. Order placement is intentionally disabled so you can
      explore every surface — the UI, derivatives analytics,
      simulator, agents — without touching real broker accounts.
    </p>
    <p class="ot-demo-body">
      The Submit affordance stays visible so the trading workflow is
      complete to read. To place real orders you'd need a signed-in
      <code>trader</code> or <code>admin</code> role with assigned
      broker accounts.
    </p>
    <div class="ot-demo-cta">
      <button type="button" class="ot-demo-btn ot-demo-btn-primary"
              onclick={() => _demoSubmitOpen = false}>Got it</button>
    </div>
  </div>
</ModalShell>

<style>
  .ot-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    /* z-index var(--z-modal): above the OrderTimelineDrawer (var(--z-drawer)=200),
       the PositionStrip (z=49), the navbar (var(--z-nav)=50), and any per-page
       sticky LogPanel that might be in the operator's way when the
       ticket opens. Operators reported the Submit row getting
       clipped by bottom panels — this guarantees the ticket sits
       on top. */
    z-index: var(--z-modal);
    /* Top-anchored at the canonical sheet position — same Y as
       ChartModal / ActivityLogModal so all modals open at the same
       vertical position below the page header. */
    padding-top: var(--modal-sheet-top, calc(3rem + 1.8rem));
    padding-left: 0;
    padding-right: 0;
    padding-bottom: 0;
    box-sizing: border-box;
  }
  /* When embedded inside another dialog (standalone=false, e.g.
     SymbolPanel), suppress the backdrop overlay entirely — the host
     modal already provides it. The inner ot-modal remains the visual
     container but the overlay div becomes transparent + non-blocking. */
  .ot-overlay-embedded {
    position: static;
    background: none;
    z-index: auto;
    padding: 0;
    display: contents;
  }
  .ot-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    width: min(28rem, calc(100vw - 2rem));
    max-height: calc(100dvh - var(--modal-sheet-top, calc(3rem + 1.8rem)) - 1rem);
    overflow-y: auto;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
  }
  /* Mobile: match the canonical-modal-panel sizing (96vw) so every modal
     opens at the same width; height is bounded by the top-anchored
     max-height above, no separate override needed. */
  @media (max-width: 760px) {
    .ot-modal {
      width: 96vw;
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
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--algo-slate);
    display: block;
  }
  .ot-symbol-meta {
    font-size: var(--fs-sm);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ot-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: var(--algo-slate);
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: var(--fs-lg);
    line-height: 1;
  }
  .ot-close:hover { border-color: var(--c-short); color: var(--c-short); }

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
    color: var(--c-action);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
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
    color: var(--c-short);
  }
  .ot-label {
    /* Section-header treatment so labels read as form structure
       cues, distinct from values / pills / numeric inputs. Amber
       weight 700 (was muted-slate 400-ish) + slightly larger
       letter-spacing — matches the way headings on the algo theme
       cards lead with amber. Operator: "make labels look different
       from others in the order window with depth". */
    display: block;
    font-size: var(--fs-sm);
    color: var(--c-action);
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
    font-size: var(--fs-sm);
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
    color: var(--text-muted);
    font-size: var(--fs-lg);
    font-weight: 700;
    cursor: pointer;
    flex: 1 1 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  /* .ot-side-buy.on / .ot-side-sell.on moved to SideToggle.svelte */

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
  .ot-meta { font-size: var(--fs-md); color: var(--text-muted); }

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

  .ot-input {
    width: 100%;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.3rem 0.45rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    font-family: monospace;
  }
  .ot-input:focus { outline: none; border-color: var(--c-action); }
  .ot-num { text-align: right; }

  /* Pill toggles (Type, Product, Variety) */
  .ot-pills { display: flex; gap: 0.15rem; flex-wrap: wrap; }
  .ot-pills-nowrap { flex-wrap: nowrap; }
  .ot-pill {
    padding: 0.2rem 0.4rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    color: var(--text-muted);
    font-size: var(--fs-xs);
    font-weight: 600;
    cursor: pointer;
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .ot-pill.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: var(--c-action);
  }
  /* Disabled-pill — visible but unreachable. Used for Variety BO + ICE
     where backend wiring isn't done yet; operator sees them on the
     roadmap without being able to click. Tooltip explains why. */
  .ot-pill-disabled {
    opacity: 0.35;
    cursor: not-allowed;
    pointer-events: auto; /* keep title tooltip reachable */
  }
  .ot-pill-disabled:hover {
    background: rgba(255,255,255,0.04);
    color: var(--text-muted);
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
  /* Tick-size chip on the Limit / Trigger labels — informs the
     operator of the symbol's minimum price increment. Reading the
     chip at a glance is cheaper than learning by Kite rejection
     after Submit. */
  .ot-tick-chip {
    margin-left: 0.35rem;
    padding: 0.05rem 0.32rem;
    border-radius: 3px;
    background: var(--c-info-14);
    border: 1px solid rgba(34, 211, 238, 0.32);
    color: #67e8f9;
    font-family: var(--font-numeric);
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: none;
    user-select: none;
  }
  /* .ot-exchange-locked moved to OrderKnobsRow.svelte — exclusive to that component */
  .ot-side-toggle-compact {
    display: inline-flex;
    width: 100%;
    height: 1.55rem;          /* match Select chip height */
    min-height: 1.55rem;
    border-radius: 3px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.18);
    box-sizing: border-box;
  }
  .ot-side-toggle-compact .ot-side-btn {
    flex: 1 1 0;
    padding: 0;
    background: transparent;
    border: 0;
    color: #94a3b8;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.04em;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .ot-side-toggle-compact .ot-side-btn:hover:not(.on):not([disabled]) {
    background: rgba(255, 255, 255, 0.06);
    color: #cbd5e1;
  }
  /* .ot-side-toggle-compact .ot-side-btn.ot-side-buy.on / .ot-side-sell.on moved to SideToggle.svelte */

  /* Mode row */
  .ot-mode-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.7rem 0;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  /* Wave C — read-only mode hint replaces the legacy mode pills.
     Operator clicks the navbar dropdown to change mode; this row
     just shows what the ticket will submit in. Color tag mirrors
     the navbar MODE_COLOR palette so visual identity is stable
     across surfaces. */
  .ot-mode-hint {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--fs-sm);
    color: rgba(180,200,230,0.7);
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
  }
  .ot-mode-hint-val {
    padding: 0.10rem 0.45rem;
    border-radius: 3px;
    font-weight: 700;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(180,200,230,0.20);
  }
  .ot-mode-hint-paper  { color: #7dd3fc; border-color: rgba(125,211,252,0.50); background: rgba(125,211,252,0.10); }
  .ot-mode-hint-live   { color: var(--c-short); border-color: rgba(248,113,113,0.55); background: var(--c-short-10); }
  .ot-mode-hint-draft  { color: #c084fc; border-color: rgba(192,132,252,0.50); background: rgba(192,132,252,0.10); }
  .ot-mode-hint-shadow { color: #fb923c; border-color: rgba(251,146,60,0.50);  background: rgba(251,146,60,0.10); }
  .ot-mode-hint-src    { color: rgba(180,200,230,0.45); font-size: var(--fs-xs); }

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
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.12);
    color: var(--text-muted);
    background: rgba(255,255,255,0.04);
  }
  .ot-chase-label.on {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: var(--c-action);
  }

  .ot-err {
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.4);
    color: var(--c-short);
    padding: 0.35rem 0.55rem;
    border-radius: 3px;
    font-size: var(--fs-sm);
    margin: 0.4rem 0;
  }
  /* Margin/cash preview rules retired — the lift-to-shell commit
     (fcd75587) removed the inline .ot-margin-row markup; the shell's
     .oes-margin-pill now owns the cost/margin readout. CSS block
     dropped as dead code (audit defect #12). */
  /* Preflight blockers (segment inactive, freeze qty, etc.) */
  .ot-margin-blocked {
    color: var(--c-short);
    font-size: var(--fs-xs);
    margin-top: 0.2rem;
    line-height: 1.35;
  }
  .ot-margin-err {
    color: rgba(248,113,113,0.85);
    font-size: var(--fs-sm);
  }
  /* Placed-order summary line — lives inside .ot-footer-info, to the
     left of the Exit button. Compact (no vertical margin, smaller pad
     than the prior block-level version) so the footer doesn't shove
     the form fields off-screen. */
  .ot-ok {
    background: var(--c-long-10);
    border: 1px solid rgba(74,222,128,0.45);
    color: var(--c-long);
    padding: 0.3rem 0.5rem;
    border-radius: 3px;
    font-size: var(--fs-md);
    font-weight: 700;
    line-height: 1.3;
    word-break: break-word;
  }
  /* Readonly single-account display — matches the custom Select
     trigger's metrics so single-account vs multi-account UIs sit at
     the same height. Cursor: default keeps it visually distinct from
     editable inputs. */
  .ot-account-readonly {
    color: var(--algo-slate);
    background: rgba(255,255,255,0.04);
    cursor: default;
    min-height: 1.55rem;
    padding: 0.25rem 0.5rem 0.25rem 0.4rem;
    font-size: var(--fs-sm);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    line-height: 1.2;
  }
  .ot-funds-k {
    color: var(--text-muted);
    font-size: var(--fs-md);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .ot-funds-v {
    color: var(--algo-slate);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .ot-funds-sep {
    color: var(--text-muted);
    opacity: 0.5;
  }
  .ot-funds-low {
    background: var(--c-short-10);
    border-color: rgba(248,113,113,0.35);
  }
  .ot-funds-low .ot-funds-v { color: var(--c-short); }

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
    font-size: var(--fs-lg);
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
    color: var(--c-action);
  }
  .ot-basket:hover:not(:disabled) {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.85);
  }
  .ot-basket:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-submit {
    background: var(--c-action);
    color: #0c1830;
  }
  .ot-submit-buy  { background: var(--c-long); }
  .ot-submit-sell { background: var(--c-short); }
  /* Demo variant — amber to match the Hire Me / Tour CTAs. Reads as
     "this is informational, not a money-mover". Tooltip + click → modal
     fires regardless of which side the operator picked, so we override
     the green/red side variants for demo. */
  .ot-submit.ot-submit-demo {
    background: rgba(251, 191, 36, 0.20);
    border: 1px solid rgba(251, 191, 36, 0.65);
    color: var(--c-action);
  }
  .ot-submit.ot-submit-demo:hover {
    background: rgba(251, 191, 36, 0.35);
    border-color: rgba(251, 191, 36, 0.90);
    color: #fcd34d;
  }
  /* Basket-mode primary action — amber outlined, distinct from the
     green/red fill so the operator reads "stage, don't fire yet". */
  .ot-submit-basket-mode {
    background: rgba(74,222,128,0.12);
    color: var(--c-long);
    border: 1px solid rgba(74,222,128,0.55);
  }
  .ot-submit-basket-mode:hover:not(:disabled) {
    background: var(--c-long-22);
    border-color: rgba(74,222,128,0.85);
  }
  .ot-submit:disabled { opacity: 0.45; cursor: not-allowed; }
  .ot-close:disabled { opacity: 0.35; cursor: not-allowed; }
  .ot-submit-basket-mode:disabled { opacity: 0.45; cursor: not-allowed; }

  /* `.ot-label-sub` kept — used by the Template card "(exit rules)"
     hint and other secondary-text spans across the ticket. */
  .ot-label-sub { opacity: 0.5; font-weight: 400; font-size: var(--fs-xs); margin-left: 0.2rem; }
  /* `.ot-target-row` / `.ot-target-input-row` / `.ot-target-mode-pill`
     / `.ot-target-input` / `.ot-target-hint` CSS rules removed in
     audit pass 6 — the Target row markup was replaced by the
     Template card in Phase 4c so these selectors had no markup left
     to style. */

  /* ── Template attachment card (v2.1) ─────────────────────────────── */
  .ot-template-row { flex-wrap: nowrap; }
  .ot-template-block {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    width: 100%;
  }
  /* Eight dead CSS rule-blocks removed — `.ot-template-overrides`,
     `.ot-tpl-field`, `.ot-tpl-input`, `.ot-tpl-toggle`, `.ot-tpl-btn`,
     `.ot-tpl-active`, `.ot-tpl-active-name`, `.ot-tpl-active-summary`
     all targeted markup that was deleted when the OrderTicket-internal
     Default/None pill toggle + override-inputs row was lifted to the
     shell-level "On fill" picker in SymbolPanel (commit 17a8b73b).
     Cap-warn + preview chip CSS below stays because those elements
     survive in OrderTicket. */
  /* Sprint C — broker-capability warning chip below the template
     summary. Amber so it reads as "heads-up", not "blocker". Only
     visible when the selected template asks for a feature the
     selected account's broker can't provide natively. */
  .ot-tpl-cap-warn {
    margin-top: 0.2rem;
    padding: 0.18rem 0.42rem;
    border-radius: 3px;
    font-size: var(--fs-sm);
    line-height: 1.25;
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.40);
  }

  /* Preview line — explains what will fire after the entry fills */
  .ot-tpl-preview {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    align-items: center;
    margin-top: 0.15rem;
    padding: 0.3rem 0.45rem;
    background: rgba(34,211,238,0.06);
    border: 1px solid var(--c-info-22);
    border-radius: 3px;
    font-size: var(--fs-sm);
    line-height: 1.35;
  }
  .ot-tpl-preview-label {
    color: rgba(180,200,230,0.85);
    font-weight: 600;
    margin-right: 0.15rem;
    font-family: var(--font-numeric);
  }
  .ot-tpl-preview-chip {
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-weight: 600;
    color: rgba(220,230,245,0.92);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(180,200,230,0.20);
  }
  .ot-tpl-preview-chip.tp {
    color: var(--c-long);
    background: var(--c-long-10);
    border-color: rgba(74,222,128,0.40);
  }
  .ot-tpl-preview-chip.sl {
    color: var(--c-short);
    background: var(--c-short-10);
    border-color: rgba(248,113,113,0.40);
  }
  .ot-tpl-preview-chip.both {
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.40);
  }
  .ot-tpl-preview-chip.wing {
    color: #c084fc;
    background: rgba(192,132,252,0.10);
    border-color: rgba(192,132,252,0.40);
  }
  /* Subtle in-chip price suffix — operator sees "+ Wing BUY 1×
     SYM @ ~₹2.40" as one visual unit; the price half is dimmed
     so the symbol stays the dominant element. */
  .ot-tpl-preview-chip-px {
    margin-left: 0.3rem;
    color: rgba(192,132,252,0.78);
    font-weight: 500;
  }
  .ot-tpl-preview-note {
    color: rgba(180,200,230,0.6);
    font-style: italic;
  }
  .ot-tpl-preview-loading {
    font-size: var(--fs-sm);
    color: rgba(180,200,230,0.55);
    font-family: var(--font-numeric);
    padding-left: 0.45rem;
  }
  .ot-tpl-preview-err {
    font-size: var(--fs-sm);
    color: #fca5a5;
    padding: 0.25rem 0.4rem;
    background: rgba(248,113,113,0.08);
    border: 1px solid rgba(248,113,113,0.30);
    border-radius: 3px;
  }

  /* Demo-mode "you can't submit" modal — fires when an anonymous demo
     visitor clicks the (amber) Submit button. Modal sits ABOVE the
     OrderTicket (z=110 vs ticket z~50). Same visual language as
     TourModal so modal types feel like one family. */
  .ot-demo-modal {
    position: relative;
    max-width: 26rem;
    width: 100%;
    background: linear-gradient(180deg, #0f172a 0%, #131c33 100%);
    border: 1px solid rgba(251, 191, 36, 0.50);
    border-radius: 6px;
    padding: 1.2rem 1.3rem 1rem;
    color: #c8d8f0;
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  .ot-demo-close {
    position: absolute; top: 0.55rem; right: 0.7rem;
    width: 1.5rem; height: 1.5rem;
    border: none; background: transparent;
    color: #94a3b8; font-size: 1.2rem; cursor: pointer; line-height: 1;
    border-radius: 3px;
  }
  .ot-demo-close:hover { background: rgba(255,255,255,0.10); color: var(--c-action); }
  .ot-demo-title {
    margin: 0 0 0.55rem;
    font-size: var(--fs-xl);
    font-weight: 800;
    color: var(--c-action);
  }
  .ot-demo-body {
    margin: 0 0 0.7rem;
    font-size: var(--fs-lg);
    line-height: 1.5;
    color: #c8d8f0;
  }
  .ot-demo-body code {
    background: var(--c-info-14);
    border: 1px solid rgba(34, 211, 238, 0.30);
    border-radius: 3px;
    padding: 0 0.25rem;
    color: #67e8f9;
    font-size: var(--fs-md);
  }
  .ot-demo-cta {
    display: flex; gap: 0.5rem; justify-content: flex-end;
    margin-top: 0.85rem;
    padding-top: 0.7rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
  }
  .ot-demo-btn {
    padding: 0.38rem 0.85rem;
    border-radius: 4px;
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.02em;
    cursor: pointer;
    text-decoration: none;
    font-family: inherit;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .ot-demo-btn-secondary {
    background: var(--c-info-14);
    border: 1px solid rgba(34, 211, 238, 0.45);
    color: #67e8f9;
    display: inline-flex; align-items: center;
  }
  .ot-demo-btn-secondary:hover {
    background: var(--c-info-22);
    border-color: rgba(34, 211, 238, 0.75);
    color: #a5f3fc;
  }
  .ot-demo-btn-primary {
    background: rgba(251, 191, 36, 0.20);
    border: 1px solid rgba(251, 191, 36, 0.65);
    color: var(--c-action);
  }
  .ot-demo-btn-primary:hover {
    background: rgba(251, 191, 36, 0.35);
    border-color: rgba(251, 191, 36, 0.90);
    color: #fcd34d;
  }
</style>
