<script module>
  // MCX commodity-name pattern hoisted to module-level so the regex
  // object is built once at module load, not rebuilt on every
  // reactive tick of _modalNotice. Tail anchor stops NSE ETFs like
  // GOLDBEES / SILVERBEES from being misclassified as MCX.
  const _MCX_NAMES_RE = /^(CRUDEOIL|GOLD|SILVER|COPPER|NATURALGAS|ZINC|LEAD|ALUMINIUM|NICKEL|MENTHA|COTTON|CARDAMOM)(FUT$|(MINI|MICRO|M)?\d{2}[A-Z]{3}|(MINI|MICRO|M)$)/;
</script>

<script>
  // SymbolPanel — unified symbol-keyed modal. Replaces three older
  // surfaces: OrderEntryShell (tabbed order entry), SymbolChartModal
  // (standalone chart popup), and SymbolActions (the ⋯ row-menu).
  //
  // Three tabs:
  //   ticket   — single-leg OrderTicket. Default for trade intent.
  //   chain    — option-chain basket builder (OptionChainTab).
  //              Disabled for cash-equity symbols.
  //   command  — terminal-style command input (CommandLineTab).
  //              Power-user surface; lands last in the tab strip.
  //
  // Chart moved to ChartModal (launched via the chart-icon button
  // in the panel header) and the /charts page so the history
  // chart doesn't inflate the modal for operators who only trade.
  //
  // Industry shape — matches IB TWS "Symbol Detail", ThinkOrSwim
  // "Active Trader", and TradingView "Order Form" patterns: one
  // symbol-keyed panel, tabs for the different actions on that
  // symbol, no hidden context menus.

  import { onMount, onDestroy, untrack, getContext } from 'svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { get as _storeGet } from 'svelte/store';
  import { portal } from '$lib/portal';
  import { ORDER_TABS } from '$lib/order/tabs.js';
  import { SYM_TYPE_OPTS } from '$lib/data/symbolTypes';
  import { aggregateCapWarnings } from '$lib/data/brokerCapWarnings';
  import { placeBasket, fetchBasketMargin, fetchLiveStatus, previewTicketTemplate } from '$lib/api';
  import ChartModal from '$lib/ChartModal.svelte';
  import { executionMode } from '$lib/stores';
  import { priceFmt, aggFmt as aggFmtMargin } from '$lib/format';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';
  import ChaseCard       from '$lib/order/ChaseCard.svelte';
  import ChaseAggPicker  from '$lib/order/ChaseAggPicker.svelte';
  import TemplateBar     from '$lib/TemplateBar.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import Select            from '$lib/Select.svelte';
  // Order-template catalog — shared with OrderTicket. SymbolPanel's
  // basket bar exposes the "On fill" picker; the chosen template is
  // attached per leg in submitBasket. Single source of truth so
  // CRUD on /automation/templates propagates here without a refresh.
  import { loadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';
  import { appliesToFor as _appliesToFor } from '$lib/data/templateScope.js';
  // resolveUnderlying / findNearestFuture / resolveAnchorToTradeable
  // dynamically imported inside effects only — no static imports needed.
  import { loadAccounts, getDefaultAccount, recentSymbolStore, setRecentSymbol, setRecentAccount } from '$lib/data/accounts';
  import { isMarketOpen, isNseOpen, isMcxOpen } from '$lib/marketHours';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import CardHeader from '$lib/CardHeader.svelte';

  // Pinned anchors: no hardcoded list — SymbolSearchInput's own
  // _autoLoadPins() fires when no `pins` prop is supplied and loads
  // from loadWatchlistSymbols(). Drop _DEFAULT_PINS / _PIN_LABELS /
  // _LABEL_TO_ANCHOR entirely; the picked symbol is already the
  // real tradingsymbol, so no reverse-lookup is needed.

  // Symbol-type filter — shared 4-option vocabulary so every
  // surface (modals, /orders, /charts) reads the same.
  const _SYM_TYPE_OPTS = SYM_TYPE_OPTS;
  let _symType = $state(/** @type {'ALL'|'EQ'|'FUT'|'OPT'} */ ('ALL'));

  /** @type {{
   *   defaultTab?:     'ticket' | 'chain',
   *   symbol?:         string,
   *   exchange?:       string,
   *   instrument?:     { kind?: string, exchange?: string },
   *   side?:           'BUY' | 'SELL',
   *   action?:         'open' | 'close' | 'modify' | 'repeat' | 'cancel',
   *   qty?:            number,
   *   product?:        'CNC' | 'MIS' | 'NRML',
   *   orderType?:      'MARKET' | 'LIMIT' | 'SL' | 'SL-M',
   *   variety?:        'regular' | 'co' | 'bo' | 'amo' | 'iceberg',
   *   price?:          number,
   *   trigger?:        number,
   *   lotSize?:        number,
   *   accounts?:       string[],
   *   account?:        string,
   *   orderId?:        string,
   *   currentQty?:     number,
   *   onSubmit:        (payload: any) => void | Promise<void>,
   *   onClose:         () => void,
   *   onAddToBasket?:  ((payload: any) => void) | null,
   *   onAddToWatchlist?: ((sym: string, exch?: string) => void | Promise<void>) | null,
   *   inline?:         boolean,
   *   headerless?:     boolean,
   *   onSymbolChange?: ((sym: string) => void) | null,
   *   tabsExternal?:   boolean,
   *   activeTab?:      'ticket' | 'chain',
   *   hideBottomPanel?: boolean,
   *   actionsHidden?:  boolean,
   *   triggerSubmit?:  number,
   *   triggerBasket?:  number,
   *   triggerClearBasket?: number,
   *   chase?:          boolean,
   *   chaseAgg?:       'low'|'med'|'high',
   *   basketCount?:    number,
   *   showChartButton?: boolean,
   *   showCommonActions?: boolean,
   *   pickerSuffix?:   import('svelte').Snippet,
   * }} */
  let {
    defaultTab     = /** @type {'chain'|'ticket'} */ ('ticket'),
    symbol         = '',
    exchange       = '',
    instrument     = /** @type {{kind?:string,exchange?:string}} */ ({}),
    side           = /** @type {'BUY'|'SELL'|null} */ (null),
    action         = /** @type {'open'|'close'|'modify'|'repeat'|'cancel'} */ ('open'),
    qty            = 0,
    product        = /** @type {'CNC'|'MIS'|'NRML'|undefined} */ (undefined),
    orderType      = /** @type {'MARKET'|'LIMIT'|'SL'|'SL-M'} */ ('LIMIT'),
    variety        = /** @type {'regular'|'co'|'bo'|'amo'|'iceberg'} */ ('regular'),
    price          = /** @type {number|undefined} */ (undefined),
    trigger        = /** @type {number|undefined} */ (undefined),
    lotSize        = 0,
    accounts       = /** @type {string[]} */ ([]),
    account        = '',
    orderId        = '',
    currentQty     = 0,
    onSubmit,
    onClose,
    onAddToBasket  = /** @type {((payload:any)=>void)|null} */ (null),
    // Optional — when provided, renders a `+W` (add to watchlist)
    // affordance in the panel header. Callers wire it conditionally:
    //   • options chain pick → wire it so operator can track a
    //     not-yet-owned strike
    //   • MarketPulse "add symbol" picker → same
    //   • Performance row click → omit (positions/holdings are
    //     already auto-merged into MarketPulse's watchlist)
    // Receives (symbol, exchange); returning a promise lets the
    // panel show a brief success/error flash without blocking.
    onAddToWatchlist = /** @type {((sym:string,exch?:string)=>void|Promise<void>)|null} */ (null),
    // When true, render flat inline (no overlay, no fixed positioning,
    // no close button). Used by /console which hosts the shell as the
    // page's primary content rather than as a popup over another page.
    inline         = false,
    // When true, omit the shell's own header strip (symbol picker +
    // exchange chip + close + watchlist add). The host page renders
    // its own symbol picker — typically inside a bucket-card header
    // — and the operator picks symbol there, so duplicating it inside
    // the shell would be redundant. Used by /orders. The shell still
    // tracks `_localSymbol` and propagates to every tab; the host
    // just needs to two-way bind it via the `symbol` prop and an
    // `onSymbolChange` callback if it wants to mirror picks back.
    headerless     = false,
    // Fired whenever the operator picks a symbol inside the shell
    // (currently only via the header picker, but reserved for future
    // tab-level picks). Hosts that pass `headerless` typically wire
    // this to their own state so a chain-tab pick still lands in the
    // header chip.
    onSymbolChange = /** @type {((sym: string) => void) | null} */ (null),
    // When true the shell omits its own tab strip. The host page is
    // expected to render the strip itself (typically in a parent
    // bucket-card header) and two-way bind `activeTab` so picks land
    // in the right body. Used by /orders to consolidate the Order
    // Entry header into a single row.
    tabsExternal   = false,
    // Bindable active tab. Defaults to undefined; resolved internally
    // to the requested defaultTab if no host binding is wired. When
    // tabsExternal is true the host MUST bind this prop or the body
    // won't update on tab clicks.
    activeTab      = $bindable(/** @type {'ticket'|'chain'|undefined} */ (undefined)),
    // When true the shell omits its own bottom panel (Order Log /
    // Order History). The host renders these in a separate card.
    // Used by /orders to split the entry shell from the activity
    // panel for independent collapse / fullscreen control.
    hideBottomPanel = false,
    // When true, the OrderTicket's internal action buttons are
    // suppressed. Hosts that render a page-level common action
    // strip pass this true + increment the counter props below.
    actionsHidden = false,
    // Counter props — bumping increments fires the corresponding
    // action on the currently-active tab. Forwarded to OrderTicket.
    triggerSubmit = 0,
    triggerBasket = 0,
    // When true the modal renders its own common action footer
    // (+Basket / BUY/SELL) at the bottom, visible across every tab.
    // /orders sets this false because it renders the same footer at
    // the page level. Defaults true so the modal mount always shows
    // the footer.
    showCommonActions = true,
    // When false, suppresses the chart-icon button in the header.
    // Callers that open SymbolPanel FROM a chart-aware page (e.g.
    // /charts opens it via the order-icon for the symbol already on
    // screen) pass false so we don't render an affordance that would
    // open a duplicate ChartModal for the same symbol.
    showChartButton = true,
    // Bindable chase + chase-aggressiveness — surfaced so the /orders
    // page can render the mode/chase cluster inside its OWN bucket
    // header (where SymbolPanel's `.oes-header` is suppressed via
    // headerless). The modal context binds these implicitly via the
    // internal cluster in `.oes-header`.
    chase          = $bindable(true),
    chaseAgg       = $bindable(/** @type {'low'|'med'|'high'} */ ('low')),
    // Read-only outbound count — /orders reads this to decide whether
    // to render the basket Clear button in the bucket header.
    basketCount    = $bindable(0),
    // Counter trigger — parent bumps this to clear the basket without
    // needing a direct callback handle. Same pattern as triggerSubmit
    // / triggerBasket.
    triggerClearBasket = 0,
    // Optional snippet rendered inside .oes-picker, immediately after
    // the symbol search input. /orders passes the CHASE cluster here so
    // it appears inline in the same flex row as the symbol input, without
    // needing a separate header-level block.
    /** @type {import('svelte').Snippet | undefined} */
    pickerSuffix = undefined,
  } = $props();

  // Local mutable copy of the symbol prop — operator can edit it from
  // the top search input so every tab (Ticket / Chain /
  // Command) re-renders against the new symbol. Synced from the prop
  // via $effect so an external pick (chain row + CE click, dashboard
  // row click, MarketPulse symbol pick) still updates the shell.
  // intentional: seeds from symbol prop once; $effect below re-syncs on external prop changes
  // svelte-ignore state_referenced_locally
  let _localSymbol = $state(String($state.snapshot(symbol) || '').toUpperCase());
  // Sync FROM prop only. Reading _localSymbol via untrack() so the
  // operator's own picks (which set _localSymbol from inside the modal)
  // don't re-trigger this effect — without untrack the comparison
  // sees the new local value, decides it differs from the prop, and
  // slams the local back to the prop, undoing the operator's typing.
  $effect(() => {
    const next = String(symbol || '').toUpperCase();
    if (!next || next === untrack(() => _localSymbol)) return;
    // Operator: "chain always should show root". When the parent pushes
    // a contract (row click, basket leg) and Chain is the active tab,
    // remember the contract as the context and display its root in the
    // picker instead. Ticket tab keeps the contract verbatim.
    const isContract = /\d/.test(next);
    if (untrack(() => _activeTab) === 'chain' && isContract) {
      _contextSymbol = next;
      _localSymbol   = _parseRoot(next);
    } else {
      _localSymbol = next;
    }
  });

  // Symbol search dropdown state.
  let _symbolQuery = $state('');
  let _symbolSuggestions = $state(/** @type {any[]} */ ([]));
  let _symbolOpen = $state(false);
  let _symbolDebounce;
  // HIGH 3: track the exchange of the last symbol the operator picked
  // from the search dropdown. Used by _isEquityExch to gate the Chain
  // tab when the panel opens without an `instrument` or `exchange` prop
  // (e.g. PageHeaderActions passes neither).
  let _pickedExchange = $state('');
  function _onSymbolInput(/** @type {string} */ v) {
    _symbolQuery = v;
    _symbolOpen = true;
    if (_symbolDebounce) clearTimeout(_symbolDebounce);
    _symbolDebounce = setTimeout(async () => {
      try {
        const { searchByPrefix } = await import('$lib/data/instruments');
        _symbolSuggestions = await searchByPrefix(v, 12);
      } catch (_) { _symbolSuggestions = []; }
    }, 150);
  }
  function _pickSymbol(/** @type {any} */ inst) {
    const picked = String(inst?.s ?? inst?.sym ?? inst?.tradingsymbol ?? _symbolQuery).toUpperCase();
    // Operator: "chain always should show root". Picking a contract while
    // the Chain tab is active stores the contract as the remembered
    // context (so a later flip back to Ticket can restore it) but the
    // picker itself displays the root. Picking on the Ticket tab keeps
    // the full contract because Ticket is a place-the-order surface.
    if (/\d/.test(picked)) _contextSymbol = picked;
    if (_activeTab === 'chain') {
      _localSymbol = _parseRoot(picked);
    } else {
      _localSymbol = picked;
    }
    _pickedExchange = String(inst?.e || inst?.exchange || '').toUpperCase();
    _symbolQuery = '';
    _symbolOpen = false;
    _symbolSuggestions = [];
    onSymbolChange?.(_localSymbol);
  }

  // Determine whether Chain tab applies.
  // Equity = no FUT/CE/PE suffix AND (kind=equity OR exchange is cash-equity).
  const _sym = $derived(_localSymbol);
  // Lookbehind: a digit must precede CE/PE/FUT so bare equity names
  // that happen to end in those letters (hypothetical edge-cases) aren't
  // misclassified. All Kite contract tradingsymbols have the form
  // <ROOT><YYMMM><STRIKE>CE — the digit before the suffix is reliable.
  const _isDerivative = $derived(/\d(?:CE|PE|FUT)$/.test(_sym));
  const _isEquityExch = $derived(
    (!_isDerivative) &&
    (
      (instrument?.kind === 'equity') ||
      // HIGH 3: include _pickedExchange so the Chain tab is correctly
      // disabled when the operator picks a cash-equity (NSE/BSE) via
      // the header search input and no instrument/exchange prop is set
      // (the PageHeaderActions case).
      (['NSE', 'BSE'].includes(
        String(instrument?.exchange || exchange || _pickedExchange || '').toUpperCase()
      ) && !_isDerivative)
    )
  );
  // Track whether the currently-picked symbol has tradeable F&O (options
  // OR futures). Equity tickers like RELIANCE / INFY / TCS are cash-equity
  // on NSE but ALSO have NFO option chains; GOLD / CRUDEOIL have MCX
  // futures. _hasFNOForSymbol re-runs whenever _localSymbol changes;
  // instruments may load lazily, so it's $state updated by an async $effect.
  let _hasFNOForSymbol = $state(false);
  // Flip true after the first $effect run so chainDisabled returns false
  // (assume enabled) during the async hydration window — avoids the tab
  // strip briefly greying out for valid equities while instruments load.
  let _chainGateLoaded = $state(false);
  $effect(() => {
    const s = _localSymbol;
    if (!s) { _hasFNOForSymbol = false; _chainGateLoaded = true; return; }
    // hasFNO wants the underlying root, not a full contract tradingsymbol.
    // Strip trailing digit/expiry/CE-PE-FUT so RELIANCE→RELIANCE,
    // NIFTY26JUN22000CE→NIFTY, CRUDEOIL26JUNFUT→CRUDEOIL.
    // Also normalise Kite index quote-key forms (e.g. "NIFTY 50"→"NIFTY")
    // so operators who pick via the Pulse grid's spot quote-key still land
    // on the correct underlying root.
    const upper = String(s).toUpperCase().trim();
    (async () => {
      try {
        const mod = await import('$lib/data/instruments');
        const ruMod = await import('$lib/data/resolveUnderlying');
        await mod.loadInstruments?.();
        const mapped = ruMod.KITE_INDEX_QUOTE_KEY_TO_ROOT[upper] || upper;
        const root = mapped.replace(/\d.*$/, '') || mapped;
        _hasFNOForSymbol = !!mod.hasFNO?.(root);
      } catch (_) {
        _hasFNOForSymbol = false;
      } finally {
        _chainGateLoaded = true;
      }
    })();
  });
  // Chain stays available when the symbol IS a derivative contract
  // (the chain navigates around the underlying) or when the
  // underlying has F&O coverage (RELIANCE → NFO CE/PE; CRUDEOIL →
  // MCX FUT; NIFTY → NFO weeklies). During the async hydration window
  // (_chainGateLoaded=false) return false (assume enabled) so the tab
  // strip doesn't briefly grey out for valid equities while instruments load.
  const chainDisabled = $derived(
    _chainGateLoaded && !_isDerivative && !_hasFNOForSymbol
  );

  // Resolve initial tab — fall through chain → ticket when equity.
  function _resolveInitialTab() {
    const req = defaultTab || 'chain';
    if (req === 'chain' && chainDisabled) return 'ticket';   // fall through for cash equity
    return req;
  }
  // If the host wired `activeTab` we mirror it; otherwise own the
  // state internally. Either way, downstream code reads `_activeTab`
  // and tab-click handlers write to it — the two-way bind takes care
  // of pushing the change back up to the host when applicable.
  let _activeTabInternal = $state(/** @type {'ticket'|'chain'} */ (_resolveInitialTab()));
  // Seed the host's binding from the resolved default on first render.
  $effect(() => { if (activeTab === undefined) activeTab = _activeTabInternal; });
  const _activeTab = $derived(activeTab || _activeTabInternal);

  // Context-symbol remembered for tab swaps. Operator: "when you press
  // chain, the symbol change to root. when you press ticket, it should
  // show the actual symbol from the context".
  //
  // Behaviour:
  //   - Chain tab active → _localSymbol shows the ROOT (parsed prefix,
  //     e.g. GOLDM from GOLDM26JUN148000CE). Chain is a browse-by-family
  //     surface so the picker reflects "which family am I scanning".
  //   - Ticket tab active → _localSymbol shows the CONTRACT from
  //     context (the full tradingsymbol the operator opened the modal
  //     with, e.g. GOLDM26JUN148000CE). Ticket is a place-the-order
  //     surface so the picker reflects "which exact contract am I
  //     trading".
  //
  // _contextSymbol stores the contract; we recompute the root from it
  // via parseRoot() so we never have to round-trip through the parent.
  // svelte-ignore state_referenced_locally
  let _contextSymbol = $state(String($state.snapshot(symbol) || '').toUpperCase());
  function _parseRoot(/** @type {string} */ s) {
    if (!s) return '';
    const up = String(s).toUpperCase();
    // Match the same shapes parse_tradingsymbol() handles backend-side:
    // monthly options + futures + weekly options. The root is the
    // alpha prefix before the date/strike token.
    const m = up.match(/^([A-Z&]+)\d/);
    return m ? m[1] : up;
  }

  function _setActiveTab(/** @type {'ticket'|'chain'} */ id) {
    _activeTabInternal = id;
    activeTab = id;
    // Tab swap drives the picker's displayed value per the rule above.
    // We don't push this back to the host via onSymbolChange — it's a
    // pure within-modal display swap. The Chain tab's internal root +
    // expiry state still derives from this _localSymbol.
    if (id === 'chain') {
      _localSymbol = _parseRoot(_contextSymbol || _localSymbol);
    } else if (id === 'ticket') {
      // Restore the context contract if we still have one; otherwise
      // leave _localSymbol alone (operator may have picked a different
      // contract via Chain → +CE).
      if (_contextSymbol) _localSymbol = _contextSymbol;
    }
  }
  // Keep _contextSymbol in sync when the parent passes a new contract
  // (row click, chain pick, etc.). Only treat it as a "context" change
  // when it's actually a contract (matches the parser's prefix-digit
  // shape) — a bare root like "GOLDM" from a Chain pick shouldn't
  // overwrite the previously-remembered contract.
  $effect(() => {
    const s = String(symbol || '').toUpperCase();
    if (s && /\d/.test(s)) _contextSymbol = s;
  });

  // Tab-activation refresh bumps. Operator request: "when chain tab is
  // pressed, the chain details need to be refreshed. when order ticket
  // is clicked, market depth and other details need to be refreshed".
  // Each child reads its bump as a $bindable / $derived prop; an
  // increment triggers an immediate re-fetch inside the child without
  // unmounting it (which would lose form state). Initial mount counts
  // as one activation so the first invoke also refreshes (covers
  // "chart also sometimes not refreshed the first time the modal gets
  // invoked").
  let _ticketBump = $state(1);
  let _chainBump  = $state(1);
  $effect(() => {
    const t = _activeTab;
    untrack(() => {
      if (t === 'ticket') _ticketBump++;
      else if (t === 'chain') _chainBump++;
    });
  });

  // Chart modal — opens ChartModal for the current symbol when the
  // operator clicks the chart-icon button in the header. Hidden in
  // headerless mode (the host page manages its own chart button).
  let _chartModalOpen = $state(false);


  // ── Watchlist add — flash feedback ───────────────────────────────
  // Mirrors the toast pattern from the retired SymbolActions
  // component. State lives here (not in the parent) so callers don't
  // have to wire a separate toast queue per page.
  /** @type {{msg: string, ok: boolean} | null} */
  let _wlToast = $state(null);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _wlToastTimer = null;
  let _wlInFlight = $state(false);

  function _wlFlash(/** @type {string} */ msg, /** @type {boolean} */ ok = true) {
    _wlToast = { msg, ok };
    if (_wlToastTimer) clearTimeout(_wlToastTimer);
    _wlToastTimer = setTimeout(() => { _wlToast = null; }, 1600);
  }

  async function _addToWatchlist() {
    if (!onAddToWatchlist || _wlInFlight || !_localSymbol) return;
    _wlInFlight = true;
    try {
      await onAddToWatchlist(_localSymbol, exchange);
      _wlFlash('✓ added to watchlist', true);
    } catch (e) {
      _wlFlash(`Watchlist: ${/** @type {any} */ (e)?.message || 'failed'}`, false);
    } finally {
      _wlInFlight = false;
    }
  }


  // ── Basket state (shared across all tabs) ───────────────────────────
  // When basketMode is active (Chain tab is selected), submissions from
  // Command and Ticket tabs accumulate here instead of firing immediately.
  const basketMode = $derived(_activeTab === 'chain');
  /** @type {any[]} */
  let basketLegs   = $state(/** @type {any[]} */ ([]));
  // Mirror basketLegs.length out to the bindable basketCount prop so
  // the /orders bucket header can react (show Clear button on > 0).
  $effect(() => { const n = basketLegs.length; if (n !== basketCount) basketCount = n; });
  /** @type {string} */ let basketResultMsg = $state('');
  let basketSubmitting = $state(false);
  // Sticky result line — persists in the common-actions row for a
  // few seconds after the basket-bar collapses, so the operator
  // gets a clear confirmation that submit landed. Cleared via
  // `_stickyResultTimer`.
  /** @type {string} */ let _stickyResultMsg = $state('');
  /** @type {'ok' | 'warn' | 'err' | ''} */ let _stickyResultLevel = $state('');
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let _stickyResultTimer;

  function addToBasket(/** @type {any} */ leg) {
    basketLegs = [...basketLegs, leg];
  }
  function removeBasketLeg(/** @type {number} */ i) {
    basketLegs = basketLegs.filter((_, k) => k !== i);
  }
  function clearBasket() {
    basketLegs = []; basketResultMsg = ''; _basketMarginRows = [];
    // Reset per-account caps cache so stale data from a previous basket
    // session does not carry over when the operator builds a new basket
    // (possibly with different accounts). Cache will re-populate lazily
    // from the effect below as new legs are added.
    _basketCapsCache = {};
    // Audit fix (H-6) — also clear the persistent partial-fail
    // sticky banner so it doesn't outlive the basket it described.
    if (_stickyResultTimer) { clearTimeout(_stickyResultTimer); _stickyResultTimer = null; }
    _stickyResultMsg = ''; _stickyResultLevel = '';
  }
  // Parent-driven clear — bumping triggerClearBasket from /orders fires
  // the same path the in-modal Clear button does. Skip the initial 0
  // value so mount doesn't auto-clear.
  // intentional: captures initial trigger value to detect subsequent bumps
  // svelte-ignore state_referenced_locally
  let _lastClearTrigger = $state($state.snapshot(triggerClearBasket));
  $effect(() => {
    if (triggerClearBasket !== _lastClearTrigger) {
      _lastClearTrigger = triggerClearBasket;
      if (triggerClearBasket > 0) clearBasket();
    }
  });

  // ── Per-account basket margin strip ──────────────────────────────────
  // When the basket spans >1 account, show Required / Available / After
  // per account above the leg pills. When there's only 1 account, the
  // existing single-account margin pill in the common footer stays as-is.
  /** @type {Array<{account:string,required:number,available:number,shortfall:number,error:string|null}>} */
  let _basketMarginRows = $state([]);
  let _basketMarginTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);

  // Derive the distinct set of accounts in the current basket.
  const _basketAccounts = $derived.by(() => {
    const seen = new Set();
    for (const leg of basketLegs) {
      const a = leg.account || _sharedAccount || account || '';
      if (a) seen.add(a);
    }
    return [...seen];
  });
  // Per-leg (account, exchange) tuples — needed by the cap-warning
  // aggregator (H-5) so the MCX check fires on the leg whose
  // exchange is MCX, not on every leg in the basket. Same de-dup
  // pattern as _basketAccounts; key is "ACCT|EXCHANGE".
  const _basketAccountExchanges = $derived.by(() => {
    const seen = new Map();
    for (const leg of basketLegs) {
      const a = leg.account || _sharedAccount || account || '';
      const e = leg.exchange || '';
      if (!a) continue;
      const k = `${a}|${e}`;
      if (!seen.has(k)) seen.set(k, { account: a, exchange: e });
    }
    return [...seen.values()];
  });
  // Per-account BrokerCapabilities cache. Populated lazily by the
  // $effect below; reads from the same /api/admin/brokers/{acct}/
  // capabilities endpoint OrderTicket already hits. Cached so an
  // operator who adds + removes + re-adds a leg only fetches caps
  // once per account per modal lifetime.
  /** @type {Record<string, any>} */
  let _basketCapsCache = $state({});
  $effect(() => {
    const accts = _basketAccounts;
    if (!Array.isArray(accts) || accts.length === 0) return;
    const _needed = accts.filter(a => !_basketCapsCache[a]);
    if (_needed.length === 0) return;
    // Fire each fetch independently so a slow / failing one doesn't
    // block the others. Cache result on success; on failure we leave
    // the cache empty so the next basket change re-tries (rare).
    untrack(() => {
      for (const a of _needed) {
        (async () => {
          try {
            const { fetchBrokerCapabilities } = await import('$lib/api');
            const c = await fetchBrokerCapabilities(a);
            if (c) _basketCapsCache = { ..._basketCapsCache, [a]: c };
          } catch (_e) {
            // Silent — demo / unauthed sessions can't see admin
            // endpoints. The cap chip falls back to OrderTicket's
            // single-account warning.
          }
        })();
      }
    });
  });
  // Aggregated cap warning across all (account, exchange) pairs in
  // the basket. Imported helper de-dupes warning strings + tags each
  // with the originating account so the operator can map back. When
  // basket is empty, returns ''; the chip render falls back to the
  // Ticket-tab single-account warning piped up from OrderTicket.
  const _basketCapWarning = $derived.by(() => {
    if (basketLegs.length === 0) return '';
    if (!_selectedTemplate || _shellUsingNone) return '';
    /** @type {Array<{account:string, caps:any, exchange:string}>} */
    const tuples = [];
    for (const { account: a, exchange: e } of _basketAccountExchanges) {
      const c = _basketCapsCache[a];
      if (!c) continue;   // caps not loaded yet — chip stays empty
      tuples.push({ account: a, caps: c, exchange: e });
    }
    return aggregateCapWarnings(_selectedTemplate, tuples);
  });

  // Debounced effect — fires 500 ms after any basket change when basket
  // spans ≥2 accounts; clears the strip when basket is empty or single-acct.
  $effect(() => {
    // Track reactive inputs.
    const legs = basketLegs;
    const accts = _basketAccounts;
    void legs; void accts;

    if (_basketMarginTimer) { clearTimeout(_basketMarginTimer); _basketMarginTimer = null; }

    if (!legs.length || accts.length < 2) {
      _basketMarginRows = [];
      return;
    }
    _basketMarginTimer = setTimeout(async () => {
      try {
        // Build groups matching the basket/margin request shape.
        /** @type {Map<string,any[]>} */
        const byAcct = new Map();
        for (const leg of legs) {
          const a = legAccountOf(leg);
          if (!a) continue;
          if (!byAcct.has(a)) byAcct.set(a, []);
          // v2 API (2026-07-08): send LOTS for F&O, raw shares for equity.
          const _bmIsFO = Number(leg.lotSize || 1) > 1;
          const _bmQty = _bmIsFO
            ? Math.max(1, Number(leg.lots) || 1)
            : Math.max(1, (Number(leg.lots) || 1) * (Number(leg.lotSize) || 1));
          byAcct.get(a)?.push({
            tradingsymbol: leg.sym,
            exchange: leg.exchange || 'NFO',
            transaction_type: leg.side,
            quantity: _bmQty,
            product: leg.product || 'NRML',
            order_type: 'LIMIT',
            price: Number(leg.limit) || 0,
          });
        }
        const groups = [...byAcct.entries()].map(([account, _legs]) => ({ account, legs: _legs }));
        const resp = await fetchBasketMargin(groups);
        _basketMarginRows = (resp?.groups || []).map(/** @type {any} */ (g) => ({
          account: g.account,
          required: Number(g.required || 0),
          available: Number(g.available || 0),
          shortfall: Number(g.shortfall || 0),
          error: g.error || null,
        }));
      } catch { _basketMarginRows = []; }
    }, 500);

    return () => {
      if (_basketMarginTimer) { clearTimeout(_basketMarginTimer); _basketMarginTimer = null; }
    };
  });

  // Last-basket-leg on-fill preview — computed shell-side so the chip
  // can swap to it when the operator is on the Chain tab. Mirrors
  // OrderTicket's preview effect: same API, same 200ms debounce, same
  // sequence guard for out-of-order responses. Effective overrides
  // follow the same per-leg-vs-shell rule submitBasket uses (leg's
  // explicit template_id → leg overrides only; shell template →
  // shell overrides via `??`).
  $effect(() => {
    // Track every input that affects the last leg's plan.
    const legs = basketLegs;
    void legs;
    void _sharedTemplateId;
    void _sharedTpOverride;
    void _sharedSlOverride;
    void _sharedWingPremPctOverride;
    void _sharedWingStrikeOffsetOverride;

    if (_lastLegTimer) { clearTimeout(_lastLegTimer); _lastLegTimer = null; }
    // Use the operator-focused leg (click any basket pill to focus it);
    // falls back to the last leg added when nothing is explicitly
    // focused.
    const leg = _focusedLeg;
    if (!leg || !leg.sym || !leg.lots || !(Number(leg.limit) > 0)) {
      _lastLegPlan = null;
      _lastLegError = '';
      return;
    }
    const legAcct = legAccountOf(leg);
    if (!legAcct) {
      _lastLegPlan = null;
      _lastLegError = '';
      return;
    }
    _lastLegSeq++;
    const seq = _lastLegSeq;
    _lastLegTimer = setTimeout(async () => {
      _lastLegLoading = true; _lastLegError = '';
      const _hasLegTpl = leg.template_id != null && leg.template_id !== _sharedTemplateId;
      // Per-leg legs use the leg's own overrides only — shell overrides
      // belong to a DIFFERENT template and would silently contaminate
      // (matches the submitBasket per-leg isolation fix).
      const tpO = leg.tp_pct_override
        ?? (_hasLegTpl ? null
            : (_sharedTpOverride !== '' ? Number(_sharedTpOverride) : null));
      const slO = leg.sl_pct_override
        ?? (_hasLegTpl ? null
            : (_sharedSlOverride !== '' ? Number(_sharedSlOverride) : null));
      const wpO = leg.wing_premium_pct_override
        ?? (_hasLegTpl ? null
            : (_sharedWingPremPctOverride !== '' ? Number(_sharedWingPremPctOverride) : null));
      const wsO = leg.wing_strike_offset_override
        ?? (_hasLegTpl ? null
            : (_sharedWingStrikeOffsetOverride !== '' ? Number(_sharedWingStrikeOffsetOverride) : null));
      const dispatchedTplId = leg.template_id ?? _sharedTemplateId;
      try {
        const res = await previewTicketTemplate({
          mode:             _sharedMode === 'draft' ? 'paper' : _sharedMode,
          side:             leg.side,
          tradingsymbol:    leg.sym,
          quantity:         (leg.lots || 1) * (leg.lotSize || 1),
          exchange:         leg.exchange || 'NFO',
          product:          leg.product || 'NRML',
          account:          legAcct,
          reference_price:  Number(leg.limit) || 0,
          template_id:                  dispatchedTplId,
          tp_pct_override:              tpO,
          sl_pct_override:              slO,
          wing_premium_pct_override:    wpO,
          wing_strike_offset_override:  wsO,
        });
        if (seq !== _lastLegSeq) return;
        _lastLegPlan = res?.plan || null;
      } catch (e) {
        if (seq !== _lastLegSeq) return;
        _lastLegError = /** @type {any} */ (e)?.message || 'preview failed';
        _lastLegPlan = null;
      } finally {
        if (seq === _lastLegSeq) _lastLegLoading = false;
      }
    }, 200);

    return () => {
      if (_lastLegTimer) { clearTimeout(_lastLegTimer); _lastLegTimer = null; }
    };
  });

  // ── Shared account state ─────────────────────────────────────────
  // Single source of truth for the routable account across all three
  // tabs (command / ticket / chain). Earlier each tab maintained its
  // own _account so picking it in one tab didn't sync to the others
  // — operators could submit a Ticket-tab order on one account while
  // the Chain-tab basket was staged for another. Lifted here as $state
  // initialised from the `account` prop; each tab receives it as
  // `account` and calls `onAccountChange` when its picker changes.
  // intentional: seeds shared account from prop once; caller can push updates via effects
  // svelte-ignore state_referenced_locally
  let _sharedAccount = $state($state.snapshot(account) || '');

  // Shared mode / chase / chaseAgg state — operator: "should mode,
  // chase, margin, common for chase and order ticket". Lifted out of
  // OrderTicket so the same controls show up regardless of which tab
  // (Chain / Ticket) is active. OrderTicket binds these three props
  // (modeChaseHidden=true on its end) so its in-form mode/chase row
  // is suppressed and the shared toolbar drives the values its submit
  // pipeline reads. Margin pill is already in the common row below.
  // Mode is read from the navbar's executionMode store. defaultMode +
  // availableModes props were removed (Wave C); the variable below is
  // load-bearing (passed as `mode={_sharedMode}` to OrderTicket).
  let _sharedMode = $derived(/** @type {'draft'|'paper'|'live'|'shadow'} */ (
    /** @type {any} */ ($executionMode) || 'paper'
  ));
  // Internal state mirrors the bindable props so the existing tab
  // codepaths (template attach, basket submit, etc.) can keep using
  // _sharedChase / _sharedChaseAgg by name. The bindable props are the
  // operator-facing handles; effects below keep both in sync.
  // Operator: "on cold start show chase with L as active." Default
  // 'low' so L renders highlighted on first paint.
  // The dual two-way `$effect` sync between the bindable prop and an
  // internal `_sharedChaseAgg` state was causing reactivity drift
  // when the operator clicked M or H — the effect ping-pong settled
  // on the prior value before the markup re-rendered. Single source
  // of truth: the `chaseAgg` $bindable prop. Reads + writes go
  // through `_sharedChaseAgg` which is a $derived alias so the rest
  // of the file's references stay valid. Writes go through the
  // setter which assigns directly to the prop.
  const _sharedChaseAgg = $derived(chaseAgg);
  function _setSharedChaseAgg(/** @type {'low'|'med'|'high'} */ v) { chaseAgg = v; }
  // Shared template — applied to every leg in the basket on submit.
  // Operator picks once from the bar; submitBasket threads the id
  // into each BasketLeg's `template_id` so the backend runs the
  // `apply_template_to_order` pipeline per leg on fill. Defaults
  // to the 'none' (no-attach) row once the catalog loads so the
  // first basket doesn't surprise the operator with an unexpected
  // GTT. Operator: "template should be applicable to option chain too".
  // Audit fix (L-3) — demo session gate. Anonymous prod visitors can
  // see the algo surface but every LIVE/PAPER submit is blocked at
  // the API layer. Pre-fix the Template Default/None pill toggle,
  // override inputs, cap warning chip, and on-fill preview chip all
  // rendered + computed for demo visitors — wasted compute + visual
  // clutter that suggested capabilities they don't actually have.
  // Replaced with a single muted "Sign in to use templates" note.
  const _algoStatus = getContext('algoStatus');
  const _isDemo = $derived(_algoStatus?.isDemo ?? false);
  let _sharedTemplateId = $state(/** @type {number|null} */ (null));
  let _templates = $state(/** @type {any[]} */ ([]));
  const _selectedTemplate = $derived(
    _templates.find(t => t.id === _sharedTemplateId) || null
  );
  // The seeded "none" template — used as the per-leg "(no template —
  // entry only)" sentinel. When the operator picks this option, leg.
  // template_id gets the none-template's REAL id (not null) so the
  // `??` fallback in submitBasket doesn't silently revert to shell.
  const _noneTpl = $derived(_templates.find(t => t.slug === 'none') || null);
  // Templates excluding the "none" row — used by the per-leg editor's
  // dropdown so "none" isn't listed twice (once as the explicit
  // sentinel, once as a regular template).
  const _nonNoneTemplates = $derived(_templates.filter(t => t.slug !== 'none'));
  // True when the shell row is on the explicit "None" pill — the
  // operator opted out of any GTT attach for this submit. Derived so
  // the pill toggle and the Default-template resolution stay in
  // lockstep.
  const _shellUsingNone = $derived(
    !!(_noneTpl && _sharedTemplateId === _noneTpl.id)
  );
  // Side-scope helper — mirrors OrderTicket's `_appliesToFor` so the
  // shell can decide whether the active template matches the operator's
  // current direction. Returns 'sell_option' for SELL CE/PE legs (the
  // only scope that wants a protective wing), 'sell_any' / 'buy_any'
  // for the directional defaults, and 'both' as a no-op catch.
  // Shell-level template parameter overrides. Editing these in the
  // "On fill" row updates them; OrderTicket binds them so its own
  // submit carries the values. Operator: "on fill selected, if there
  // are any parameters i should be update the parameters."
  // Empty string = "no override; use the template's value".
  let _sharedTpOverride               = $state(/** @type {number|''} */ (''));
  let _sharedSlOverride               = $state(/** @type {number|''} */ (''));
  let _sharedWingStrikeOffsetOverride = $state(/** @type {number|''} */ (''));
  let _sharedWingPremPctOverride      = $state(/** @type {number|''} */ (''));
  // Whether the selected template's scope is a SELL option (the only
  // case where the wing fields are relevant). Mirrors OrderTicket's
  // `_appliesToFor` check at SymbolPanel level so the shell-row UI
  // can hide the wing inputs when they wouldn't apply.
  const _sharedTplShowsWing = $derived.by(() => {
    const s = (_selectedTemplate?.applies_to || '').toLowerCase();
    return s === 'sell_option';
  });
  // True when the active template will auto-attach a protective wing
  // on fill — used to surface a "+ wing" indicator inside every
  // eligible (SELL option) basket pill so the operator knows which
  // legs will get a paired protective BUY at fill time. Combines the
  // template's wing params (saved) with the operator's shell-level
  // overrides (per-submit).
  const _sharedWingPlanned = $derived.by(() => {
    if (!_selectedTemplate || _selectedTemplate.slug === 'none') return false;
    if ((_selectedTemplate.applies_to || '').toLowerCase() !== 'sell_option') return false;
    const effPrem = _sharedWingPremPctOverride !== ''
      ? Number(_sharedWingPremPctOverride)
      : (_selectedTemplate.wing_premium_pct ?? null);
    const effOff  = _sharedWingStrikeOffsetOverride !== ''
      ? Number(_sharedWingStrikeOffsetOverride)
      : (_selectedTemplate.wing_strike_offset ?? null);
    return (effPrem != null && effPrem > 0) || (effOff != null && effOff !== 0);
  });
  // Side-aware default template — resolves the saved is_default row
  // whose applies_to matches the operator's current scope (SELL CE/PE
  // → sell_option; SELL eq/fut → sell_any; BUY any → buy_any). The
  // Default pill in the on-fill row clicks this. Falls back through
  // (a) side-matching default → (b) 'both' default → (c) null when
  // nothing fits. Reactive so a side flip or symbol change refreshes
  // both the pill state and the rendered template params.
  const _sideAwareDefault = $derived.by(() => {
    if (_templates.length === 0) return null;
    // Symbol used for the CE/PE regex check. Prefer _localSymbol (the
    // header / Ticket form), but fall back to the focused (or last)
    // basket leg's symbol when the operator built the basket from the
    // Chain tab without typing a header symbol. Falling through to
    // _modalSide for the side itself, which is shell-global.
    const symForScope = (_localSymbol || '').trim()
      || (_focusedLeg?.sym || '')
      || (basketLegs.length > 0 ? basketLegs[basketLegs.length - 1].sym : '');
    const sideForScope = _focusedLeg?.side || _modalSide;
    const scope = _appliesToFor(sideForScope, symForScope);
    const sideMatch = _templates.find(t =>
      t.is_default && (t.applies_to || '').toLowerCase() === scope
    );
    if (sideMatch) return sideMatch;
    const bothMatch = _templates.find(t =>
      t.is_default && (t.applies_to || '').toLowerCase() === 'both'
    );
    return bothMatch || null;
  });
  // Phase 5 — which basket-leg keys currently have their per-leg
  // override editor open. Operator clicks the "⚙ tmpl" chip on a
  // pill to toggle inclusion. Per-leg overrides are stored directly
  // on the leg object (leg.template_id / leg.tp_pct_override / etc.)
  // and consumed at submit time by submitBasket — the backend already
  // accepts BasketLeg overrides as of the Phase 2 commit.
  let _legEditorsOpen = $state(/** @type {Set<string>} */ (new Set()));
  function _toggleLegEditor(/** @type {string} */ key) {
    const next = new Set(_legEditorsOpen);
    if (next.has(key)) next.delete(key); else next.add(key);
    _legEditorsOpen = next;
  }
  /** Effective template id for a leg — per-leg override wins, else the
   *  shell's pick. Used to label the per-leg chip. */
  function _legEffectiveTplId(/** @type {any} */ leg) {
    return leg?.template_id ?? _sharedTemplateId;
  }
  /** Effective template row for a leg — looked up in the catalog so
   *  the chip can show the template name + the editor's inputs can
   *  use the right placeholders. */
  function _legEffectiveTpl(/** @type {any} */ leg) {
    const id = _legEffectiveTplId(leg);
    if (id == null) return null;
    return _templates.find(t => t.id === id) || null;
  }
  /** True when the leg has ANY per-leg override set (template +
   *  numeric fields). Used by the chip styling so the operator can
   *  see at a glance which legs deviate from the shell defaults. */
  function _legHasOverride(/** @type {any} */ leg) {
    return (leg?.template_id != null)
        || (leg?.tp_pct_override != null)
        || (leg?.sl_pct_override != null)
        || (leg?.wing_premium_pct_override != null)
        || (leg?.wing_strike_offset_override != null);
  }

  // Compute the protective wing's tradingsymbol when the template
  // uses a fixed `wing_strike_offset` — pure JS port of the backend
  // `_wing_symbol` helper. Returns null for premium-scan wings (the
  // exact strike comes from the chain scan at fill time, so we show
  // a generic "+ wing on fill" instead).
  // Audit fix — memoize the regex parse keyed by `parentSym` so the
  // basket-pill render loop doesn't re-exec the regex per leg per
  // render. Cache is keyed by the parsed-symbol shape (root + exp +
  // strike + opt) only; the wing offset and selected template are
  // applied after lookup so an override change doesn't have to bust
  // the cache. The cache is process-lifetime; tradingsymbols don't
  // change identity, so unbounded growth is bounded by the number of
  // unique contracts the operator touches in a session.
  /** @type {Map<string, {root: string, expTok: string, strike: number, opt: string} | null>} */
  const _SYMBOL_PARSE_CACHE = new Map();
  /** @param {string} parentSym */
  function _parseOptionSymbol(parentSym) {
    const key = String(parentSym || '').toUpperCase();
    if (_SYMBOL_PARSE_CACHE.has(key)) return _SYMBOL_PARSE_CACHE.get(key);
    const m = key.match(
      /^([A-Z]+?)(\d{2}[A-Z]{3}|\d{4,5})(\d+(?:\.\d+)?)(CE|PE)$/
    );
    if (!m) { _SYMBOL_PARSE_CACHE.set(key, null); return null; }
    const [, root, expTok, strikeStr, opt] = m;
    const strike = parseInt(strikeStr, 10);
    if (!Number.isFinite(strike)) { _SYMBOL_PARSE_CACHE.set(key, null); return null; }
    const parsed = { root, expTok, strike, opt };
    _SYMBOL_PARSE_CACHE.set(key, parsed);
    return parsed;
  }
  function _wingSymbolFor(/** @type {string} */ parentSym) {
    if (!_sharedWingPlanned || !_selectedTemplate) return null;
    const effOff = _sharedWingStrikeOffsetOverride !== ''
      ? Number(_sharedWingStrikeOffsetOverride)
      : (_selectedTemplate.wing_strike_offset ?? null);
    if (!effOff || isNaN(effOff)) return null;
    const parsed = _parseOptionSymbol(parentSym);
    if (!parsed) return null;
    const { root, expTok, strike, opt } = parsed;
    const wingStrike = opt === 'CE' ? strike + effOff : strike - effOff;
    if (wingStrike <= 0) return null;
    return `${root}${expTok}${wingStrike}${opt}`;
  }
  // Reset overrides when the template changes — operator's overrides
  // were tied to a SPECIFIC template's defaults; carrying them across
  // to a different template would silently surface unintended values.
  let _lastSeenTemplateId = null;
  $effect(() => {
    if (_sharedTemplateId !== _lastSeenTemplateId) {
      untrack(() => {
        _lastSeenTemplateId = _sharedTemplateId;
        _sharedTpOverride               = '';
        _sharedSlOverride               = '';
        _sharedWingStrikeOffsetOverride = '';
        _sharedWingPremPctOverride      = '';
      });
    }
  });
  // Side-aware template auto-swap. When the operator flips BUY → SELL
  // (or symbol changes from option to non-option), the currently-
  // selected template's `applies_to` may no longer match the new
  // direction (e.g. a long-call template on a SELL submit places a
  // wrong-direction TP/SL). Swap to a different is_default template
  // matching the new scope. Skipped when the operator has explicitly
  // picked the "none" row (slug='none'), and when no matching default
  // exists (we don't change a manual pick to something arbitrary).
  // Operator: "side-aware template auto-select removed (BUY → SELL
  // doesn't re-select default)" — restored via this effect.
  let _lastSideScope = '';
  $effect(() => {
    // action='modify' and 'cancel' don't need template auto-swap —
    // the template is irrelevant for those actions. All other actions
    // ('open', 'close', 'repeat') need it so e.g. a SELL close on a
    // long option gets a sell_option template, not the stale BUY default.
    if (action === 'modify' || action === 'cancel') return;
    if (_templates.length === 0) return;
    // Mirror the symbol-fallback rule in `_sideAwareDefault` so the
    // auto-swap on side-flip also picks the right scope when the
    // operator is on Chain with staged basket legs and no header
    // symbol typed.
    const symForScope = (_localSymbol || '').trim()
      || (_focusedLeg?.sym || '')
      || (basketLegs.length > 0 ? basketLegs[basketLegs.length - 1].sym : '');
    const sideForScope = _focusedLeg?.side || _modalSide;
    const scope = _appliesToFor(sideForScope, symForScope);
    if (scope === _lastSideScope) return;
    untrack(() => {
      _lastSideScope = scope;
      // Don't override an explicit operator pick — "none" stays "none";
      // a non-default template the operator picked stays as-is.
      const current = _templates.find(t => t.id === _sharedTemplateId);
      if (!current) return;
      if (current.slug === 'none') return;
      if (!current.is_default) return;
      // If the current default still fits the new scope, leave it.
      const cscope = (current.applies_to || '').toLowerCase();
      if (cscope === scope || cscope === 'both') return;
      // Find a different is_default that matches the new scope.
      const next = _templates.find(t =>
        t.is_default && ((t.applies_to || '').toLowerCase() === scope
                         || (t.applies_to || '').toLowerCase() === 'both')
      );
      if (next && next.id !== _sharedTemplateId) {
        _sharedTemplateId = next.id;
      } else if (!next) {
        // No default template matches the new scope (e.g. operator has
        // only a buy_option default and flipped to SELL). Clear the
        // stale BUY template so the preview fires with templateId=null
        // rather than carrying the wrong-direction template. The
        // OrderTicket's re-validation effect will call _autoSelectTemplate
        // which will also find no match and leave templateId null, so
        // both paths agree. Fixes: audit shows parent_side:'BUY' when
        // operator intends SELL because the BUY template was never cleared.
        _sharedTemplateId = null;
      }
    });
  });
  // Account list — falls through three layers:
  //   1. `accounts` prop (host page injects them, e.g. /orders)
  //   2. cached fetch from /api/accounts/ on mount when prop is empty
  //   3. last-good cache (loadAccounts memoises in-process)
  // The header's Account Select reads from this so the operator can
  // pick from the same set even when the modal is opened via the
  // page-header trio (which passes accounts={[]}).
  // intentional: seeds from accounts prop once; $effect below re-syncs when prop updates
  // svelte-ignore state_referenced_locally
  let _modalAccounts = $state(/** @type {string[]} */ (Array.isArray(accounts) ? $state.snapshot(accounts) : []));
  $effect(() => {
    if (Array.isArray(accounts) && accounts.length) {
      untrack(() => { _modalAccounts = accounts; });
    }
  });
  // Subscribe to the template store + kick a defensive load. The
  // store warms once at module evaluation in templates.js, but if
  // this is the FIRST mount in the page's lifetime the rows may
  // not have landed yet — kick a load() (idempotent, cached on
  // repeat). The store subscription below picks up CRUD mutations
  // on /automation/templates while the modal is open.
  loadOrderTemplates().catch(() => { /* picker stays empty */ });
  // Pure subscription — keeps the basket-bar Select rows current when
  // /automation/templates mutates. Default selection is owned by the
  // bound children: OrderTicket (always mounted) applies its
  // side-aware default on mount, OptionChainTab falls back to 'none'
  // if templateId is still null when chain activates.
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows && rows.length) {
      _templates = rows.filter(t => t.is_active);
    }
  });

  onMount(async () => {
    try {
      const list = await loadAccounts();
      if (!_modalAccounts.length) {
        _modalAccounts = (list || []).map(a => String(a?.account_id || a?.account || a || '')).filter(Boolean);
      }
      // Account pre-select fall-through:
      //   1. context-supplied account (host page passed it via prop)
      //   2. orders.default_account setting (from /admin/settings)
      //   3. sole loaded account when the list has exactly one entry
      // Empty string when none of the above resolves.
      if (!_sharedAccount) {
        const defaultAcct = getDefaultAccount();
        if (defaultAcct && _modalAccounts.includes(defaultAcct)) {
          _sharedAccount = defaultAcct;
        } else if (_modalAccounts.length === 1) {
          _sharedAccount = _modalAccounts[0];
        }
      }
      // Symbol pre-select: same ladder.
      //   1. host-supplied `symbol` prop (already seeds _localSymbol on init)
      //   2. recently-used symbol from the operator's last pick on any
      //      surface. Operator: "The symbol should be updated from the
      //      latest symbol used or clear from the context for modals".
      //      orders.default_symbol setting retired — no underlying-to-
      //      future resolver layer; the recent value is whatever
      //      tradeable the operator picked last.
      if (!_localSymbol) {
        let _recent = '';
        try { _recent = String(_storeGet(recentSymbolStore) || '').toUpperCase(); } catch { /* empty */ }
        if (_recent) {
          _localSymbol = _recent;
          onSymbolChange?.(_localSymbol);
        }
      }
    } catch { /* keep last-good */ }
  });
  // Re-sync from the prop when the caller updates it externally (e.g.
  // when /admin/options opens the modal with a new default after the
  // operator picks a different position).
  // CLEAN 3: only overwrite when the prop carries a real value — if the
  // caller passes account='' we should not reset the operator's in-modal
  // pick back to empty. `untrack` prevents the _sharedAccount read from
  // registering as a dependency and causing a re-sync cycle.
  $effect(() => { if (account) untrack(() => { _sharedAccount = account; }); });
  function _onAccountChange(/** @type {string} */ a) {
    _sharedAccount = a;
    // Persist the operator's pick so /orders defaults to it next time.
    if (a) setRecentAccount(a);
  }

  // Persist whatever symbol / account the modal opened with (caller
  // prop, recent-store fallback, or live pick) so the next /orders
  // or /charts open carries the same context. Operator: "I have
  // opened a orders modal where it showed the symbol. when I go to
  // orders page, that symbol and account is not getting defaulted".
  $effect(() => {
    const sym  = _localSymbol;
    const acct = _sharedAccount;
    if (!sym && !acct) return;
    if (sym)  setRecentSymbol(String(sym));
    if (acct) setRecentAccount(String(acct));
  });

  // Modal-level common action footer — visible across all tabs when
  // showCommonActions=true (modal mount). Counter-prop pattern: every
  // click bumps an internal counter; OrderTicket's $effect on
  // triggerSubmit/triggerBasket reacts to the cumulative value. Side
  // toggle flips the Submit button label between BUY and SELL.
  // Operator: "when order modal is clicked without using symbol, don't
  // show buy or sell as active. user needs to choose. this will not
  // apply when order modal is triggered by clicking symbol or using
  // symbol menu." When the modal opens cold (no symbol context),
  // _modalSide stays null and neither BUY nor SELL is pre-active;
  // margin preflight short-circuits until the operator picks a side.
  // When opened via a symbol pick (PageHeaderActions with a contextSymbol,
  // row-symbol click, etc.), the caller passes `side="BUY"` so the
  // existing flow is unchanged.
  // intentional: seeds modal side from prop once; operator changes it directly thereafter
  // svelte-ignore state_referenced_locally
  let _modalSide          = $state(/** @type {'BUY'|'SELL'|null} */ ($state.snapshot(side)));
  // When the panel mounts with a deterministic side context (action=close
  // or a non-zero currentQty), seed _modalSide on first render so the
  // operator can click Submit immediately without first clicking the
  // side-toggle to pick BUY/SELL. The Jun 26 2026 `_modalFireSubmit`
  // guard (commit 5ac190e2) blocks null-side submits with a toast — fine
  // when the operator genuinely hasn't picked, but on a close/add row
  // the side IS deterministic and shouldn't require a manual pick first.
  $effect(() => {
    if (_modalSide !== null) return;
    const cq = Number($state.snapshot(currentQty)) || 0;
    if (action === 'close') {
      // Long → SELL closes; short → BUY closes.
      _modalSide = cq < 0 ? 'BUY' : 'SELL';
    } else if (cq !== 0) {
      // Open from a row context with an existing position — default to
      // the ADD direction (long → BUY adds; short → SELL adds). Matches
      // the visual cue the operator gets from the row's qty sign.
      _modalSide = cq < 0 ? 'SELL' : 'BUY';
    }
    // cq == 0 + action != 'close' → genuine fresh-open path, leave null
    // so the side toggle reads as "Pick side" until the operator chooses.
  });
  // Submit button label adapts to:
  //   currentQty=0  → "Buy" / "Sell" (plain new order)
  //   currentQty>0  → "Add to position" / "Close position"  (long)
  //   currentQty<0  → "Close position" / "Add to position"  (short)
  // When the basket has legs the label switches to "Submit basket (N)"
  // so the same button drives the basket-submit path. Operator
  // request: "buy sell button can be a single button as buy/sell
  // selected within order tab. it should change based on add or close
  // when clicking order on existing symbol."
  // Operator: "add/close is active only when order modal is opened from
  // symbol row, from which it gets symbol, qty etc. in that case order
  // ticket should have submit - add/close - buy/sell. in other cases
  // submit - buy/sell."
  //
  // Detect "opened from symbol row" via currentQty != 0 — the symbol-row
  // entry path (LogPanel modify/repeat, position grid click) seeds the
  // modal with the position's current qty. Cold opens / +Basket flows
  // / new tickets all have currentQty == 0.
  //
  // Format:
  //   basket has legs           → "Submit (N)"
  //   modal cold + side null    → "Submit"
  //   modal cold + side set     → "Submit · BUY"  /  "Submit · SELL"
  //   from symbol row + side    → "Submit · ADD · BUY"  /  "Submit · CLOSE · SELL"
  //                               (verb derived from side vs position direction)
  const _submitLabel = $derived.by(() => {
    if (basketLegs.length > 0) return `Submit (${basketLegs.length})`;
    // Operator: "when chase is active the button should say submit."
    // Chase fires re-quotes off the form's side anyway; the label just
    // needs to say what action the operator is committing to (a chase),
    // not parrot the side that's already visible on the BUY/SELL toggle.
    if (_chaseEnabled) return 'Submit';
    if (!_modalSide) return 'Submit';
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    if (cq === 0) return `Submit · ${_modalSide}`;
    // ADD = same direction as the existing position
    // CLOSE = opposite direction
    const verb = (cq > 0 ? (_modalSide === 'BUY' ? 'ADD' : 'CLOSE')
                         : (_modalSide === 'BUY' ? 'CLOSE' : 'ADD'));
    return `Submit · ${verb} · ${_modalSide}`;
  });
  // Style class for the submit button — green when the submit will
  // place a BUY OR add to long OR close short; red when it will place
  // a SELL OR close long OR add to short. Cyan when basket-submit
  // (mixed sides possible).
  const _submitFlavor = $derived.by(() => {
    if (basketLegs.length > 0) return 'basket';
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    if (cq === 0) return _modalSide === 'BUY' ? 'buy' : 'sell';
    // Long position: BUY is add (green), SELL is close (red).
    if (cq > 0)   return _modalSide === 'BUY' ? 'buy' : 'sell';
    // Short position: BUY closes (green-ish since it brings flat),
    // SELL adds to short (red).
    return _modalSide === 'BUY' ? 'buy' : 'sell';
  });
  let _modalTriggerSubmit = $state(0);
  let _modalTriggerBasket = $state(0);
  // Latest validation error from the Ticket form — updated whenever
  // OrderTicket's validationErr changes via the onValidationChange
  // callback. Used to surface a toast when the common-action Submit
  // fires but the form is blocking placement silently (e.g. depth
  // ladder hasn't loaded the limit price yet after a 2s poll delay).
  let _ticketValidationErr = $state('');
  function _modalFireBasket() { if (_activeTab === 'ticket') _modalTriggerBasket++; }
  function _modalFireSubmit() {
    if (_activeTab !== 'ticket') {
      // Tab drift — operator clicked the common-actions Submit but the
      // active tab isn't 'ticket', so OrderTicket.submit() wouldn't
      // fire. Surface the silent-swallow rather than no-op.
      toast.info('Switch to the Ticket tab to place an order');
      return;
    }
    if (!_modalSide) {
      // Side was never picked — without this, the trigger fires
      // submit() in OrderTicket with side=null, which builds a
      // null-side payload that the backend rejects + the error
      // toast may not paint if the modal auto-closes. Operator sees
      // nothing happen and assumes the button is broken. Block here
      // with an explicit affordance.
      toast.warning('Pick BUY or SELL before submitting');
      return;
    }
    // Surface OrderTicket's validation error BEFORE incrementing the
    // trigger counter. Without this, submit() fires → validationErr
    // trips the silent `if (validationErr) return;` guard → no order
    // POST, no visible feedback (the .ot-err div renders inside the
    // ticket form body but is not visually prominent enough when the
    // common footer Submit is clicked). The operator clicks repeatedly,
    // generating 20+ preview/preflight calls with no basket/ticket POST
    // — exactly the audit pattern seen for CRUDEOIL 6500PE SELL.
    if (_ticketValidationErr) {
      toast.warning(_ticketValidationErr);
      return;
    }
    _modalTriggerSubmit++;
  }
  function _modalFlipSide()   { _modalSide = _modalSide === 'BUY' ? 'SELL' : 'BUY'; }
  // Stable handler reference for the single side footer button.
  // Operator: "based on short or long existing position it should
  // derive buy or sell." First click in a symbol-row context (where
  // currentQty != 0) defaults to ADD direction — long → BUY, short →
  // SELL. Subsequent clicks toggle. Cold context (currentQty == 0)
  // defaults to BUY on first click, then toggles.
  function _cycleSide() {
    // Operator: "ticket add sell button not working" (Jun 26 2026).
    // The Jun 2026 "one-shot" iteration locked the side after the
    // first click and pointed operators at OrderTicket's internal
    // BUY/SELL toggle — but that toggle is suppressed when
    // `actionsHidden={showCommonActions}` (true on every common-actions
    // surface), so operators had no way to switch side at all.
    //
    // Revert to a proper toggle:
    //   - cold (no _modalSide yet): derive ADD direction from current
    //     position (short → SELL, otherwise BUY)
    //   - subsequent clicks: flip BUY ⇄ SELL
    // The visual (on-buy / on-sell colour classes + verb / side
    // line in the label) reflects the live state so the operator
    // always sees the CURRENT side, not a "this will switch you to"
    // call to action.
    if (!_modalSide) {
      const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
      _modalSide = cq < 0 ? 'SELL' : 'BUY';
      return;
    }
    _modalSide = _modalSide === 'BUY' ? 'SELL' : 'BUY';
  }
  // Derive ADD / CLOSE verb from a side + current position direction.
  // Used by the two-line side button label so the operator sees the
  // intent (ADD/CLOSE) above the broker-side (BUY/SELL).
  function _addCloseVerb(/** @type {'BUY'|'SELL'} */ side) {
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    if (cq === 0) return '';
    if (cq > 0) return side === 'BUY' ? 'ADD' : 'CLOSE';
    return side === 'BUY' ? 'CLOSE' : 'ADD';
  }

  // Margin preview lifted out of OrderTicket so the operator sees the
  // same MARGIN / Avail / After / Short row regardless of which tab is
  // active (matches the operator ask: "margin line should be common
  // for all the tabs in modal"). OrderTicket fires onMarginUpdate
  // every time its computed preview changes; we cache it here and
  // render in the common action footer.
  let _modalMargin        = $state(/** @type {any} */ (null));
  let _modalMarginLoading = $state(false);
  // On-fill preview chip piped up from OrderTicket via
  // onPreviewPlanUpdate. Rendered inside the Template container so
  // it shows on BOTH tabs (operator: "on fill chip text can be shown
  // in chain also"). Updates whenever the Ticket form's _previewPlan
  // re-derives — TP%/SL%/Wing override edits flow visibly into the chip.
  let _modalPreviewPlan    = $state(/** @type {any} */ (null));
  let _modalPreviewLoading = $state(false);
  let _modalPreviewError   = $state('');
  let _modalCapWarning     = $state('');
  // Last-basket-leg preview — separate from the Ticket-form preview
  // above. When the operator is on the Chain tab AND basket has legs,
  // the displayed chip swaps to this so they see "on fill → TP/SL/Wing
  // for the leg I just added" instead of the (potentially stale)
  // Ticket form's preview. Computed shell-side with its own debounce +
  // sequence guard so it doesn't interfere with the OrderTicket's
  // identical mechanism. Operator: "swap to last basket leg on chain tab."
  let _lastLegPlan    = $state(/** @type {any} */ (null));
  let _lastLegLoading = $state(false);
  let _lastLegError   = $state('');
  // The basket-leg key the preview chip is locked to. Click any basket
  // pill on the Chain tab to focus that leg's preview; null = follow
  // the LAST leg added (the default). Cleared when the focused leg is
  // removed so the chip falls back to last-leg behaviour without
  // sticking on a stale key. Operator: "click a basket pill to swap
  // preview to that leg."
  let _focusedLegKey = $state(/** @type {string|null} */ (null));
  // Resolved focused leg — explicit pick when _focusedLegKey matches a
  // current leg, otherwise the last leg. Used by both the preview
  // effect and the pill's `is-focused` visual cue.
  const _focusedLeg = $derived.by(() => {
    if (basketLegs.length === 0) return null;
    if (_focusedLegKey) {
      const m = basketLegs.find(l => l.key === _focusedLegKey);
      if (m) return m;
    }
    return basketLegs[basketLegs.length - 1];
  });
  // Position of the focused leg in the basket (1-based) for the badge.
  const _focusedLegIndex = $derived.by(() => {
    if (!_focusedLeg) return 0;
    const i = basketLegs.findIndex(l => l.key === _focusedLeg.key);
    return i >= 0 ? i + 1 : basketLegs.length;
  });
  // Drop a stale `_focusedLegKey` once its leg leaves the basket so the
  // preview cleanly falls back to last-leg.
  $effect(() => {
    if (_focusedLegKey && !basketLegs.find(l => l.key === _focusedLegKey)) {
      untrack(() => { _focusedLegKey = null; });
    }
  });
  // Click-to-cycle handler — advances the focused-leg pointer through
  // the basket in order, wrapping to leg 1 after the last. Used by the
  // preview chip's onclick so the operator can flip through legs
  // without leaving the chip surface. Released back to "track last
  // leg" only via × on the focused pill (per the existing cleanup).
  function _cycleFocusedLeg() {
    if (basketLegs.length < 2) return;
    const curIdx = _focusedLeg
      ? basketLegs.findIndex(l => l.key === _focusedLeg.key)
      : -1;
    const nextIdx = (curIdx + 1) % basketLegs.length;
    _focusedLegKey = basketLegs[nextIdx].key;
  }
  // Rich tooltip for the preview chip — shows the focused leg's
  // identity + effective template so hovering anywhere on the chip
  // surfaces the context the ₹ triggers below come from.
  const _previewChipTooltip = $derived.by(() => {
    if (!_previewFromLeg || !_focusedLeg) return '';
    const leg = _focusedLeg;
    const tplName = _legEffectiveTpl(leg)?.name
      || _legEffectiveTpl(leg)?.slug
      || 'no template';
    const acct = legAccountOf(leg) || '—';
    const qty = ((leg.lots || 1) * (leg.lotSize || 1));
    const cycleHint = basketLegs.length > 1 ? ' · click to cycle to next leg' : '';
    return `${leg.side} ${qty} ${leg.sym} · acct ${acct} · template: ${tplName}${cycleHint}`;
  });
  // Which preview drives the displayed chip — last-leg on Chain when
  // basket has legs, Ticket form everywhere else. Single derivation
  // so the chip swap is atomic + the label rendering doesn't have to
  // branch internally.
  const _activePreviewPlan    = $derived(
    _activeTab === 'chain' && basketLegs.length > 0
      ? _lastLegPlan : _modalPreviewPlan
  );
  const _activePreviewLoading = $derived(
    _activeTab === 'chain' && basketLegs.length > 0
      ? _lastLegLoading : _modalPreviewLoading
  );
  const _activePreviewError   = $derived(
    _activeTab === 'chain' && basketLegs.length > 0
      ? _lastLegError : _modalPreviewError
  );
  // True when the chip is showing the basket-leg preview instead of
  // the Ticket form's. Used for a small "leg N of M" badge so the
  // operator knows which context the chip reflects.
  const _previewFromLeg = $derived(
    _activeTab === 'chain' && basketLegs.length > 0
  );
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _lastLegTimer = null;
  let _lastLegSeq = 0;
  // Chip-meta from OrderTicket: { isCashMode, cash, kind, side }.
  // Drives the footer chip label swap between "Cost · Cash" (cash-mode
  // orders: equity buy/sell + long option premium) and "Req · Avail"
  // (margin-mode: short option, futures). Null until first emit.
  let _chipMeta = $state(/** @type {any} */ (null));
  function _onMarginUpdate(/** @type {any} */ preview, /** @type {boolean} */ loading, /** @type {any} */ meta) {
    _modalMargin = preview;
    _modalMarginLoading = loading;
    if (meta) _chipMeta = meta;
  }
  // Compact derived view of the margin block so the footer template
  // stays readable. Returns null when there's nothing to show.
  const _marginInfo = $derived.by(() => {
    if (!_modalMargin && !_modalMarginLoading) return null;
    if (_modalMargin?.error) return { error: _modalMargin.error };
    if (!_modalMargin) return { loading: true, isCashMode: !!_chipMeta?.isCashMode };
    const d = _modalMargin.diagnostics ?? {};
    const required  = Number(d.basket_margin_used) || 0;
    // Pick the "available" figure based on whether the leg consumes
    // cash (equity buy/sell, long option premium) or margin (short
    // option, futures). Cash comes from /api/funds (passed via meta);
    // margin comes from broker.margins().net. Same shape downstream so
    // the chip + colour bands work the same way.
    const isCashMode = !!_chipMeta?.isCashMode;
    const available = isCashMode
      ? (typeof _chipMeta?.cash === 'number' ? _chipMeta.cash : null)
      : d.available_margin;
    const shortfall = isCashMode
      ? (typeof available === 'number' && available < required ? (required - available) : 0)
      : (Number(d.margin_shortfall) || 0);
    let afterCls = '';
    let after = null;
    if (typeof available === 'number') {
      after = available - required;
      const pct = available > 0 ? (after / available) * 100 : 0;
      afterCls = after < 0 || pct < 10 ? 'err' : pct < 40 ? 'warn' : '';
    }
    const pairedCount = Number(_chipMeta?.pairedCount) || 0;
    return { required, available, after, afterCls, shortfall, isCashMode, pairedCount };
  });
  // Modal notice — single source of truth for error / warning / info
  // banners surfaced in the action footer's LEFT slot. Priority:
  //   1. preflight error    (err  — broker / preview blocked)
  //   2. market closed      (warn — outside the symbol's session hours)
  //   3. broker not loaded  (warn — account selected but broker offline)
  //   4. info notices       (info — reserved for future use)
  // Returns null when the row should fall through to cash / margin info.
  const _modalNotice = $derived.by(() => {
    // Live market clock — re-derive every minute via _modalMarginLoading
    // alongside the normal _modalMargin dependency so the chip
    // refreshes naturally when other state changes. For a pure clock
    // tick, the funds/margin re-poll inherits the same cadence.
    void _modalMarginLoading;
    if (_modalMargin?.error) {
      return { level: 'err',  text: '⚠ ' + String(_modalMargin.error).slice(0, 60), detail: '' };
    }
    if (Array.isArray(_modalMargin?.blocked) && _modalMargin.blocked.length) {
      const b = _modalMargin.blocked[0];
      return { level: 'err',  text: '⚠ ' + String(b?.reason || 'preview blocked').slice(0, 60), detail: String(b?.fix || '') };
    }
    // Market-hours check — operator: "when exchange is resolved
    // showing the message is appropriate". So the warning only
    // fires when we KNOW which segment the order is going to:
    //
    //   1. explicit exchange (_pickedExchange OR exchange prop)
    //   2. tradingsymbol-pattern fallback for common MCX
    //      commodities (CRUDEOIL / GOLD / SILVER / ...) — counts
    //      as resolved because the symbol itself disambiguates.
    //
    // When we can't resolve the exchange at all, suppress the
    // notice rather than guessing — the broker will reject the
    // order at submit time with a precise reason.
    try {
      const _sym = String(_localSymbol || symbol || '').toUpperCase();
      // MCX-by-name pattern: commodity ROOT followed by either an
      // MCX-specific tail (MINI, M, MICRO, or an expiry token like
      // `25APRFUT` / `25APR` / `26JUNFUT`). The trailing anchor stops
      // the regex from misclassifying NSE ETFs like GOLDBEES,
      // GOLDIAM, SILVERBEES which begin with the same root.
      // Tail patterns:
      //   FUT$                         — month-future
      //   (MINI|MICRO|M)?\d{2}[A-Z]{3}  — expiry token (`25APRFUT`, etc.)
      //   (MINI|MICRO|M)$              — bare mini/micro
      const _explicitExch = (_pickedExchange || exchange || '').toUpperCase();
      const _isMcxByName = _MCX_NAMES_RE.test(_sym);
      const isMcx = _explicitExch === 'MCX' || _explicitExch === 'NCO' || _isMcxByName;
      const resolved = !!_explicitExch || _isMcxByName;
      if (!resolved) return null;  // exchange not known — no warning
      const open = isMcx ? isMcxOpen() : isNseOpen();
      if (!open) {
        return {
          level: 'warn',
          text:  isMcx ? 'MCX closed — order will queue' : 'Market closed — order will queue',
          detail: 'Outside live session hours; PAPER orders still execute. LIVE orders are deferred.',
        };
      }
    } catch (_) { /* swallow */ }
    return null;
  });

  // Margin pill color flavor — single source for the .oes-margin-pill-*
  // classname. Maps the live margin state into one of: err (shortfall
  // or insufficient), warn (covers required but headroom is thin), ok
  // (comfortable headroom). Loading + error states fall back to neutral.
  const _marginPillCls = $derived.by(() => {
    if (!_marginInfo || _marginInfo.loading) return 'neutral';
    if (_marginInfo.error) return 'err';
    if (_marginInfo.shortfall > 0) return 'err';
    if (_marginInfo.available == null) return 'neutral';
    const cls = _marginInfo.afterCls;
    if (cls === 'err')  return 'err';
    if (cls === 'warn') return 'warn';
    return 'ok';
  });

  // Chase only re-quotes a LIMIT (or SL with a limit). MARKET / SL-M
  // fill at the book's price — no limit to re-quote. Chain tab
  // always uses LIMIT so chase stays available there. Derivatives
  // (FUT / CE / PE) are *always* chase-eligible — operator: "chase
  // is default for derivatives as market order cannot be placed" —
  // because Kite rejects MARKET on F&O anyway, so the order type
  // dropdown is effectively LIMIT / SL for derivatives. Keeping the
  // toggle enabled means it stays ON (default true) regardless of
  // whether the operator landed on the form with the orderType
  // chip mid-update. Surfaced via a derived so the template can
  // grey out (rather than hide) the chase affordance when the
  // active order type can't use it.
  // Live ticket order type — bound from OrderTicket so chase can
  // disable + deselect the MOMENT the operator picks MARKET, without
  // waiting for a side+margin round-trip. (Previously _chaseEnabled
  // relied on `_chipMeta.orderType` which only landed after the
  // margin preflight ran.)
  // intentional: seeds from orderType prop once; $effect below re-syncs on prop/ticketProps changes
  // svelte-ignore state_referenced_locally
  let _ticketOrderType = $state(/** @type {'MARKET'|'LIMIT'|'SL'|'SL-M'} */ ($state.snapshot(orderType)));
  // Sync from caller props on modify/repeat — the shell repopulates
  // _ticketProps when LogPanel dispatches lp:modify-order or similar.
  $effect(() => {
    const ot = _ticketProps?.orderType ?? orderType;
    if (ot && ot !== untrack(() => _ticketOrderType)) _ticketOrderType = ot;
  });
  // Chase is always active for Chain (all Chain orders are LIMIT).
  // On Ticket: show only for LIMIT / SL (both carry a limit price the
  // chase engine can re-quote). MARKET / SL-M fill at the book —
  // no limit to re-quote, so hide entirely.
  const _chaseEnabled = $derived(
    _activeTab === 'chain'
      || _ticketOrderType === 'LIMIT'
      || _ticketOrderType === 'SL'
  );
  // _sharedChase mirrors _chaseEnabled — chase is implicitly ON
  // whenever the L/M/H cluster is visible (default 'low' on cold
  // start means an aggressiveness is always picked).
  let _sharedChase = $state(chase);
  $effect(() => {
    const v = _chaseEnabled;
    if (_sharedChase !== v) _sharedChase = v;
    if (chase !== v) chase = v;
  });
  // +Basket is the Ticket-tab add-to-basket affordance. Chain has
  // per-row +CE / +PE buttons; on Chain the +Basket button stays
  // visible but grayed so the operator sees the affordance is part
  // of the panel — operator: "what happened to +basket button which
  // is supposed to be present in order modal and order page".
  const _basketEnabled = $derived(_activeTab === 'ticket');
  // Operator: "ticket should have basket which can be on or off."
  // Sticky pill state — when ON the Ticket's Submit routes to
  // _modalFireBasket (add to basket) instead of _modalFireSubmit
  // (immediate place). Chain ignores this — its Submit always submits
  // the basket. Defaults OFF so Ticket's primary action stays "place
  // the order now"; operator opts into basket-build mode explicitly.
  let _toBasket = $state(false);

  // Footer-side button label helper — operator: "there should be one
  // additional button in ticket buy/sell. if add/close is active
  // instead buy/sell in ticket, buy/sell should be prefixed by
  // add/close." Cold (currentQty=0): "BUY" / "SELL". Symbol-row
  // (currentQty != 0): "ADD BUY" or "CLOSE BUY" / "ADD SELL" or
  // "CLOSE SELL" depending on whether the side adds to or closes the
  // existing direction.
  function _sideBtnLabel(/** @type {'BUY'|'SELL'} */ side) {
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    if (cq === 0) return side;
    // long: BUY adds, SELL closes
    // short: BUY closes, SELL adds
    const verb = (cq > 0 ? (side === 'BUY' ? 'ADD' : 'CLOSE')
                         : (side === 'BUY' ? 'CLOSE' : 'ADD'));
    return `${verb} ${side}`;
  }

  // Single-pass leg update — used by chain merge (sym+side dedupe) and
  // +/- steppers. Maps in place so rapid clicks accumulate cleanly
  // instead of relying on the remove+re-add pattern which can drop
  // updates if the prop hasn't propagated back to the child between
  // calls.
  function updateLegByKey(/** @type {string} */ key,
                         /** @type {(leg:any) => any} */ updater) {
    basketLegs = basketLegs.map(b => b.key === key ? updater(b) : b);
  }

  /**
   * Resolve the effective account for a basket leg.
   * Priority: per-leg account field → shell-level shared account → prop account.
   * Extracted so submitBasket and the margin debounce use the exact same logic.
   */
  const legAccountOf = (/** @type {any} */ leg) =>
    leg.account || _sharedAccount || account || '';

  /** Submit every leg in the shell basket via POST /api/orders/basket. */
  // Resolves effective basket execution mode: if the current mode is
  // paper/live, confirms against the server's branch + paper_trading_mode
  // flag so a dev-branch basket doesn't silently go live.
  async function _resolveBasketMode(/** @type {string} */ mode) {
    if (mode !== 'paper' && mode !== 'live') return mode;
    try {
      const live = await fetchLiveStatus();
      if (live && (live.branch !== 'main' || live.paper_trading_mode === true)) return 'paper';
    } catch { /* keep caller's pick */ }
    return mode;
  }

  // Applies shared template override fields to a built leg object.
  // Per-leg template picks opt out of shell overrides — see inline
  // comment in submitBasket for the audit fix rationale.
  function _applySharedOverrides(/** @type {any} */ l) {
    const hasLegTpl = (l.template_id !== _sharedTemplateId);
    return {
      ...l,
      tp_pct_override:             l.tp_pct_override ?? (hasLegTpl
        ? null : (_sharedTpOverride !== '' ? Number(_sharedTpOverride) : null)),
      sl_pct_override:             l.sl_pct_override ?? (hasLegTpl
        ? null : (_sharedSlOverride !== '' ? Number(_sharedSlOverride) : null)),
      wing_premium_pct_override:   l.wing_premium_pct_override ?? (hasLegTpl
        ? null : (_sharedWingPremPctOverride !== '' ? Number(_sharedWingPremPctOverride) : null)),
      wing_strike_offset_override: l.wing_strike_offset_override ?? (hasLegTpl
        ? null : (_sharedWingStrikeOffsetOverride !== '' ? Number(_sharedWingStrikeOffsetOverride) : null)),
    };
  }

  async function submitBasket() {
    if (basketSubmitting || !basketLegs.length) return;
    basketSubmitting = true; basketResultMsg = '';

    // Validate that every leg has a limit price before going to the backend.
    const missingQuote = basketLegs.find(leg => !(Number(leg.limit) > 0));
    if (missingQuote) {
      const msg = `${missingQuote.side} ${missingQuote.sym}: no quote yet — wait for bid/ask to load`;
      basketResultMsg = msg;
      toast.warning(msg);
      basketSubmitting = false;
      return;
    }

    // Resolve effective mode from the global store (Phase B: no per-ticket override).
    const basketMode2 = await _resolveBasketMode($executionMode || 'paper');

    // Build groups: one group per distinct account.
    /** @type {Map<string, {legIndex: number, leg: any}[]>} */
    const byAcct = new Map();
    basketLegs.forEach((leg, idx) => {
      const acct = legAccountOf(leg);
      if (!byAcct.has(acct)) byAcct.set(acct, []);
      byAcct.get(acct)?.push({ legIndex: idx, leg });
    });

    const groups = [...byAcct.entries()].map(([acct, entries]) => ({
      account: acct,
      mode: basketMode2,
      legs: entries.map(({ leg }) => ({
        tradingsymbol:    leg.sym,
        exchange:         leg.exchange || 'NFO',
        transaction_type: leg.side,
        // v2 API (2026-07-08): send LOTS for F&O (lotSize > 1),
        // raw shares for equity. Backend multiplies lots × lot_size
        // to derive contracts internally.
        quantity:         Number(leg.lotSize || 1) > 1
                            ? Math.max(1, Number(leg.lots) || 1)
                            : Math.max(1, (Number(leg.lots) || 1) * (Number(leg.lotSize) || 1)),
        product:          leg.product || 'NRML',
        order_type:       'LIMIT',
        variety:          'regular',
        price:            Number(leg.limit),
        chase:            _sharedChase,
        chase_aggressiveness: _sharedChase ? (leg.chaseAgg || _sharedChaseAgg || 'low') : 'low',
        target_pct:       leg.target_pct   ?? null,
        target_abs:       leg.target_abs   ?? null,
        // Same template attaches to every leg. Backend basket route
        // reads BasketLeg.template_id; per-leg `apply_template_to_order`
        // runs on fill to attach TP/SL/Wing GTTs. Per-leg overrides
        // (Phase 5) supersede the shell defaults.
        // Audit fix — when leg.template_id is set explicitly (operator
        // picked a per-leg template), DON'T fall through to the shell
        // overrides. The shell overrides were typed against the shell
        // template's defaults; carrying them to a per-leg template T2
        // silently contaminates T2's params with T1's overrides. Per-
        // leg legs MUST opt-in to each override field independently.
        template_id:      leg.template_id ?? _sharedTemplateId,
      })).map(_applySharedOverrides),
    }));

    const total = basketLegs.length;
    /** @type {Set<number>} */
    const failedIdx = new Set();
    let ok = 0;
    /** @type {string[]} */
    const fails = [];

    try {
      const resp = await placeBasket(groups);
      // Map per-account results back to original leg indices.
      /** @type {Map<number,any>} */
      const resultByOrigIdx = new Map();
      let groupIdx = 0;
      for (const [acct, entries] of byAcct) {
        const grpResp = (resp?.groups || [])[groupIdx];
        groupIdx++;
        entries.forEach(({ legIndex, leg }, i) => {
          const r = grpResp?.results?.[i];
          resultByOrigIdx.set(legIndex, { leg, acct, r });
        });
      }
      for (const [origIdx, { leg, acct, r }] of resultByOrigIdx) {
        if (!r || r.status === 'REJECTED' || r.error) {
          fails.push(`${leg.side} ${leg.sym}: ${r?.error || 'rejected'}`);
          failedIdx.add(origIdx);
        } else {
          ok++;
          onSubmit?.({
            mode:            basketMode2,
            side:            leg.side,
            symbol:          leg.sym,
            quantity:        (leg.lots || 1) * (leg.lotSize || 1),
            price:           Number(leg.limit),
            account:         acct,
            broker_response: { order_id: r.order_id, status: r.status },
            _basketLeg:      true,
            ...leg,
          });
        }
      }
    } catch (e) {
      // Full request failure — mark all legs failed.
      basketLegs.forEach((_, i) => failedIdx.add(i));
      fails.push(/** @type {any} */ (e)?.message || 'Basket submit failed');
    }

    basketSubmitting = false;
    const numAccts = byAcct.size;
    if (!fails.length) {
      const msg = `${ok}/${total} placed across ${numAccts} account${numAccts > 1 ? 's' : ''} · basket cleared`;
      basketResultMsg = msg;
      basketLegs = [];
      _basketMarginRows = [];
      // Reset the symbol picker so the next session starts clean.
      _localSymbol = '';
      _stickyResultMsg = msg;
      _stickyResultLevel = 'ok';
      if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
      _stickyResultTimer = setTimeout(() => { _stickyResultMsg = ''; _stickyResultLevel = ''; }, 3000);
      // Operator: modal stays open after a successful chain submit so
      // they can place more legs without re-opening. Explicit close
      // via × or Escape only.
    } else if (ok > 0) {
      // Audit fix — basket partial-failure UX. Pre-fix the operator
      // saw the rejected leg in the result message but the legs that
      // SUCCEEDED were silently committed to the broker with no
      // visible record. Their template attaches would still fire on
      // fill but the operator had no way to know which legs were
      // live + needed monitoring. Now: surface a persistent sticky
      // banner (8s vs the 3s success banner) with the placed-vs-
      // rejected counts, and clear the basket only of legs that
      // DID succeed — the failed ones stay in the basket bar so
      // the operator can fix the rejection cause and resubmit.
      basketResultMsg = `${ok}/${total} placed — ${fails.length} rejected: ${fails[0]}. Placed legs are live; failed legs kept in basket for retry.`;
      basketLegs = basketLegs.filter((_, i) => failedIdx.has(i));
      _stickyResultMsg = `${ok} placed · ${fails.length} rejected — check /orders for live legs`;
      _stickyResultLevel = 'warn';
      if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
      // Audit fix (H-6) — partial-fail banner stays persistent until
      // the operator clears the basket or closes the modal. Pre-fix
      // it expired in 8s; the modal stays open with failed legs in
      // the basket, and after the timer ran out the operator had no
      // visible reminder that `ok` legs were already live at the
      // broker → resubmit risk on the retained legs. Persistent
      // banner is dismissed by basket-clear or modal-close.
      _stickyResultTimer = null;
    } else {
      basketResultMsg = `Failed: ${fails[0]}`;
      _stickyResultMsg = `All ${total} legs rejected — no orders placed`;
      _stickyResultLevel = 'err';
      if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
      _stickyResultTimer = setTimeout(() => { _stickyResultMsg = ''; _stickyResultLevel = ''; }, 8000);
    }
  }

  function handleParsedOrder(/** @type {any} */ _props) {
    _setActiveTab('ticket');
  }

  // Focus-trap anchor — bound to .oes-modal so Tab cycles stay inside.
  let _modalEl      = $state(/** @type {HTMLElement|null} */ (null));

  function _oesFocusables() {
    return /** @type {HTMLElement[]} */ (
      Array.from(_modalEl?.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      ) ?? [])
    );
  }

  onMount(() => {
    // Defer initial focus until portal re-parenting settles (overlay
    // mode only — inline renders in-flow so no re-parenting occurs).
    // Skip the symbol input as the auto-focus target: its onfocus
    // opens the search dropdown, which the operator hasn't asked for
    // yet on a fresh modal open. Picking the first NON-input focusable
    // (typically the × close button) lets the dropdown stay closed
    // until the operator explicitly clicks the symbol input.
    if (!inline) {
      setTimeout(() => {
        const els = _oesFocusables();
        const target = els.find(el => el.tagName !== 'INPUT') ?? els[0];
        target?.focus();
      }, 0);
    }

    const onKey = (/** @type {KeyboardEvent} */ e) => {
      if (e.key === 'Escape') {
        // Fullscreen exits first; second Esc closes the panel.
        onClose();
        return;
      }
      if (e.key === 'Tab' && !inline) {
        const els = _oesFocusables();
        if (!els.length) return;
        const first = els[0], last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    };
    window.addEventListener('keydown', onKey);
    // HIGH 1: prevent background page scroll while the modal is open.
    // Only applies in overlay mode (not inline) — inline renders as a
    // flat page element so scroll should remain enabled.
    const wasInline = inline;
    if (!wasInline) {
      document.body.style.overflow = 'hidden';
    }
    return () => {
      window.removeEventListener('keydown', onKey);
      if (_wlToastTimer) { clearTimeout(_wlToastTimer); _wlToastTimer = null; }
      if (!wasInline) {
        document.body.style.overflow = '';
      }
    };
  });

  // Tab order — Ticket first (most common trade action), Chain
  // (specialised F&O), Command (power-user). Chart moved to
  // ChartModal (header icon button) and /charts page.
  // Chain first — basket builder is the most-used surface per operator,
  // it lands the operator straight on the strike/expiry chooser; Ticket
  // is the single-leg fast path; Command is the power-user terminal.
  // TABS is ORDER_TABS augmented with visual metadata (dot / active palette).
  // ORDER_TABS is the shared id/label source of truth from $lib/order/tabs.js.
  const TABS = ORDER_TABS.map(t => ({
    ...t,
    ...(t.id === 'chain'   ? { dot: 'var(--c-long)', activeTxt: 'var(--c-long)', activeBorder: 'var(--c-long)', activeBg: 'rgba(74,222,128,0.14)'  } :
        t.id === 'ticket'  ? { dot: 'var(--c-action)', activeTxt: 'var(--c-action)', activeBorder: 'var(--c-action)', activeBg: 'var(--c-action-14)'  } :
                             { dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', activeBg: 'rgba(125,211,252,0.14)' }),
  }));

  // Effective OrderTicket props — shell-level props forwarded to the ticket.
  const _ticketProps = $derived({
    symbol, exchange, side, action, qty, product, orderType, variety,
    price, trigger, lotSize, accounts, account, orderId,
    currentQty, onAddToBasket,
  });
</script>

<!-- Modal overlay (omitted in inline mode — renders as flat page content). -->
<!-- use:portal={!inline} — portals to document.body when NOT inline so
     the fixed overlay clears any parent stacking-context clipping.
     portal() is a no-op when the argument is false (inline mode).
     In modal mode the wrapper uses .canonical-modal-overlay /
     .canonical-modal-panel (defined in app.css) so SymbolPanel,
     ChartModal, and ActivityLogModal all land at the same viewport
     position + size. Inline mode keeps the legacy .oes-overlay /
     .oes-modal classes (which strip all chrome via .oes-inline). -->
<div class={inline ? 'oes-overlay oes-inline' : 'canonical-modal-overlay oes-overlay'}
     role={inline ? undefined : 'dialog'}
     aria-modal={inline ? undefined : 'true'}
     aria-label={inline ? undefined : (symbol || 'Symbol panel')}
     onclick={inline ? undefined : (_chartModalOpen ? undefined : onClose)}
     use:portal={!inline}>
  <div class={inline ? 'oes-modal oes-modal-inline' : 'canonical-modal-panel oes-modal'}
       role="presentation"
       bind:this={_modalEl}
       onclick={inline ? undefined : (e) => e.stopPropagation()}>

    <!-- Header (close button hidden in inline mode). The plain title
         placeholder was replaced with a live Symbol picker — operator
         types a prefix, picks an instrument from the dropdown, and
         every tab (Ticket, Chain, Command) re-renders against
         the new symbol. Symbol is the only shell-level shared state;
         per-tab values (qty, side, etc.) stay with their tabs.

         When `headerless` is true, the host page renders its own
         symbol picker (typically inside a parent card header) so we
         skip the strip here entirely. -->
    {#if !headerless}
    <!-- Minimal header — Orders title + close. Symbol + Account
         pickers moved INTO the tabs that need them (Ticket has its
         own; Chain derives from the picked underlying via the symbol
         passed from context). Uses CardHeader with showControls=false;
         amber gradient bg + border supplied by the algo layout's
         CardHeader theme tokens. -->
    <div style="--ch-padding: 0.35rem 0.65rem">
      <CardHeader showControls={false}>
        {#snippet left()}
          <span class="oes-modal-name">
            <!-- Order-slip / receipt glyph — matches the page-header Order
                 button (PageHeaderActions). Rectangle with order lines
                 inside reads as "order entry form" without the directional
                 confusion the prior dual-arrow caused (operator: read as a
                 refresh icon). -->
            <svg class="oes-modal-name-icon" width="13" height="13" viewBox="0 0 16 16"
                 fill="none" stroke="currentColor" stroke-width="1.5"
                 stroke-linecap="round" aria-hidden="true">
              <rect x="3.2" y="2" width="9.6" height="12" rx="1.2" />
              <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" stroke-width="1.4" />
            </svg>
            Order entry
          </span>
          {#if _wlToast}
            <span class="oes-wl-toast" class:ok={_wlToast?.ok} class:err={!_wlToast?.ok}>
              {_wlToast?.msg}
            </span>
          {/if}
        {/snippet}
        {#snippet right()}
          <!-- Operator: "mode and chase should be left aligned. chase value
               should be selectable." Cluster sits immediately AFTER the
               title chip (left-aligned). The close X stays anchored right. -->
          <span class="oes-right-group canonical-card-btn-group">
            {#if !inline && _localSymbol}
              <button type="button" class="oes-chart-btn" title="Chart — {_localSymbol}"
                      onclick={() => _chartModalOpen = true}>
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.9"
                        stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
            {/if}
            <span class="oes-header-cluster">
              <!-- Operator: "remove live chip from order modal and page.
                   navbar live is enough." The mode chip used to sit here. -->
              {#if basketLegs.length > 0}
                <button type="button" class="oes-common-clear oes-common-clear-inline"
                  title="Clear all basket legs"
                  disabled={basketSubmitting}
                  onclick={clearBasket}>Clear</button>
              {/if}
            </span>
            {#if !inline}
              <button type="button" class="oes-close" title="Close" aria-label="Close"
                      onclick={(e) => { e.stopPropagation(); onClose(); }}>×</button>
            {/if}
          </span>
        {/snippet}
      </CardHeader>
    </div>
    {/if}

    <!-- Picker row — Account · Symbol type · Symbol — placed BEFORE
         the tab strip so the operator sets the order's identity once
         and both tabs (Chain / Order ticket) read from the same
         values. Mirrors ChartWorkspace's toolbar pattern (type filter
         + combo). Renders in EVERY mount where the tabs are visible
         (modal, inline /orders, even when headerless because /orders
         uses headerless to drop the title chip but still wants the
         picker). -->
    <!-- Audit cleanup: dropped a `{#if true}` wrapper that did nothing
         but trigger svelte-check warnings. The picker always renders. -->
      <div class="oes-picker">
        <!-- Operator: "order entry in modal and page don't have account
             drop to default from context or select while placing order."
             Render the account Select whenever the loaded list has at
             least 1 entry (previously hidden for single-account
             operators). The Select still lets a multi-account operator
             switch broker; for single-account it shows the resolved
             code as a read-but-clickable affordance so the context is
             always visible.
             Fall back to a static label when the prop carries an
             account but the list is still loading. -->
        {#if _modalAccounts.length >= 1}
          <div class="oes-account-pick">
            <Select
              options={_modalAccounts.map(a => ({ value: a, label: a }))}
              value={_sharedAccount || _modalAccounts[0] || ''}
              onValueChange={(v) => _onAccountChange(String(v))}
              placeholder="Account"
              ariaLabel="Trading account" />
          </div>
        {:else if _sharedAccount}
          <span class="oes-account-single" title="Trading account (list loading)">{_sharedAccount}</span>
        {/if}
        <div class="oes-type-wrap">
          <Select
            options={_SYM_TYPE_OPTS}
            bind:value={_symType}
            ariaLabel="Symbol type filter" />
        </div>
        <div class="oes-sym-pick">
          <SymbolSearchInput
            value={_localSymbol}
            type={_symType}
            placeholder="Symbol — pick or type 3+"
            onPick={(sym, meta) => {
              // No hardcoded pin list — every picked symbol is already
              // a real tradingsymbol (from watchlist auto-load or search).
              _localSymbol = sym;
              if (meta?.exchange) _pickedExchange = meta.exchange;
              onSymbolChange?.(sym);
            }}
            ariaLabel="Symbol — pinned or search" />
        </div>
        {#if exchange || _pickedExchange}
          <span class="oes-exch">{exchange || _pickedExchange}</span>
        {/if}
        {#if !inline && _chaseEnabled}
          <span class="oes-common-chase-label on" title="Chase is active">CHASE</span>
          <ChaseAggPicker value={_sharedChaseAgg} onChange={_setSharedChaseAgg} variant="panel" />
        {/if}
        {#if pickerSuffix}
          {@render pickerSuffix()}
        {/if}
      </div>

      <!-- Cluster lifted INTO `.oes-header` above (modal) or into the
           /orders bucket header (page) per operator request. Standalone
           strip removed. -->

    <!-- Tab strip — suppressed when the host page renders its own
         (tabsExternal). Operator clicks still flow back via the
         two-way bound `activeTab` either way. -->
    {#if !tabsExternal}
    <div class="oes-tabs" style="border-bottom: 1px solid rgba(255,255,255,0.08);">
      <AlgoTabs
        tabs={TABS.map(t => ({
          id: t.id,
          label: t.label,
          badge: t.id === 'chain' && basketLegs.length > 0 ? basketLegs.length : undefined,
          /* Operator (2026-07-01): "active tab text color must be
             consistent". Ticket / chain / panel previously took
             amber / green / sky variants to distinguish flow; now
             every tab is amber. Chain still carries a badge for the
             leg count, which conveys the semantic distinction
             without breaking the uniform active state. */
          color: /** @type {const} */ ('amber'),
          disabled: t.id === 'chain' ? chainDisabled : false,
          disabledTitle: t.id === 'chain' && chainDisabled
            ? 'No F&O for this root — chain unavailable'
            : undefined,
        }))}
        value={_activeTab}
        onChange={(id) => {
          if (id === 'chain' && chainDisabled) return;
          _setActiveTab(/** @type {any} */ (id));
        }}
      />
    </div>
    {/if}

    <!-- Shell-level template picker — visible from EVERY tab + the modal,
         regardless of whether the basket has legs staged. Operator:
         "why template is present only for order ticket, but not for
         chain" — moved ABOVE the tab body so it sits at the same
         altitude as the tabs themselves on both Ticket + Chain views.
         Pre-fix, the picker lived BELOW the body so a tall Chain tab
         (strike grid + futures row + basket) pushed it past the
         modal's scrolled-into-view area and the operator only saw it
         on the shorter Ticket tab. Bound to _sharedTemplateId so the
         value persists across Ticket ↔ Chain tab flips. -->
    <!-- On-fill container is now rendered BELOW the tab body
         (immediately above the basket bar). Operator: "on fill
         container should be after chain picker or order ticket
         depth." Sequence reads top-to-bottom as: header → tabs →
         body (depth / strike grid) → On-fill → basket bar →
         actions. The picker stays visible from BOTH tabs because
         it lives at the shell level. -->

    <!-- Tab content. Command Line tab retired — was the third option
         alongside Chain and Ticket; the in-tab account+symbol inputs
         duplicated the modal header's. /console keeps its own
         command-line surface for shell-style usage. -->
    <div class="oes-body">
      <!-- OrderTicket is ALWAYS MOUNTED so its margin-preflight $effect
           keeps computing in the background. When the Chain tab is
           active, we hide the ticket body via inline `display: none`
           rather than {#if}-unmounting it — otherwise the margin
           strip in the common action footer goes stale on tab switch
           (operator: "margin details are not common for both tabs").
           Inline `style:display` beats the `hidden` HTML attribute we
           used previously, which left the ticket content visible
           beside the chain content on some build configurations
           (operator: "it is still showing order ticket details in
           chain followed by chain by increating the height. Chain
           content should replace order ticket content without
           changing the height"). The descendant `display: block
           !important` rules on `.ot-overlay` / `.ot-modal` (needed
           for inline rendering inside the shell) were overriding the
           UA's `[hidden] { display: none }` cascade on some browsers,
           so the ticket kept painting. Inline style on the wrapper
           wins regardless. -->
      <div class="oes-ticket-body"
           style:display={_activeTab === 'ticket' ? null : 'none'}>
          <OrderTicket
            suspended={_activeTab !== 'ticket'}
            symbol={_ticketProps.symbol || _localSymbol}
            exchange={_ticketProps.exchange ?? _pickedExchange ?? exchange}
            side={showCommonActions ? _modalSide : (_ticketProps.side ?? side)}
            action={_ticketProps.action ?? action}
            qty={_ticketProps.qty ?? qty}
            product={_ticketProps.product ?? product}
            bind:orderType={_ticketOrderType}
            variety={_ticketProps.variety ?? variety}
            price={_ticketProps.price ?? price}
            trigger={_ticketProps.trigger ?? trigger}
            lotSize={_ticketProps.lotSize ?? lotSize}
            accounts={_ticketProps.accounts ?? accounts}
            account={_sharedAccount || _ticketProps.account || account}
            onAccountChange={_onAccountChange}
            onSideChange={(s) => { _modalSide = s; }}
            orderId={_ticketProps.orderId ?? orderId}
            currentQty={_ticketProps.currentQty ?? currentQty}
            onAddToBasket={addToBasket}
            basketMode={basketMode}
            accountHidden={true}
            symbolHidden={true}
            symType={_symType}
            actionsHidden={actionsHidden || showCommonActions}
            fundsHidden={true}
            refreshKey={_ticketBump}
            triggerSubmit={triggerSubmit + _modalTriggerSubmit}
            triggerBasket={triggerBasket + _modalTriggerBasket}
            hostManagesEsc={true}
            mode={_sharedMode}
            bind:chase={_sharedChase}
            bind:chaseAgg={chaseAgg}
            bind:templateId={_sharedTemplateId}
            bind:tpOverride={_sharedTpOverride}
            bind:slOverride={_sharedSlOverride}
            bind:wingStrikeOffsetOverride={_sharedWingStrikeOffsetOverride}
            bind:wingPremPctOverride={_sharedWingPremPctOverride}
            standalone={false}
            defaultChase={_sharedChase}
            defaultChaseAgg={_sharedChaseAgg}
            modeChaseHidden={true}
            onMarginUpdate={showCommonActions ? _onMarginUpdate : null}
            onPreviewPlanUpdate={(plan, loading, err, capW) => {
              _modalPreviewPlan    = plan;
              _modalPreviewLoading = loading;
              _modalPreviewError   = err;
              _modalCapWarning     = capW;
            }}
            onValidationChange={(err) => { _ticketValidationErr = err; }}
            {onSubmit}
            {onClose} />
      </div>

      {#if _activeTab === 'chain'}
        <!-- OptionChainTab's own basket state is migrated to the shell.
             The tab receives the shared basket as props and calls back
             into the shell to mutate it. Its own placeBasket is unused
             when routed through onSubmitBasket. -->
        <OptionChainTab
          symbol={_localSymbol}
          account={_sharedAccount || account}
          onAccountChange={_onAccountChange}
          bind:templateId={_sharedTemplateId}
          {accounts}
          refreshKey={_chainBump}
          basketLegs={basketLegs}
          onAddLeg={addToBasket}
          onRemoveLeg={(/** @type {any} */ leg) => {
            const i = basketLegs.findIndex(b => b.key === leg.key);
            if (i >= 0) removeBasketLeg(i);
          }}
          onUpdateLeg={updateLegByKey}
          onSubmitBasket={submitBasket}
          onClearBasket={clearBasket}
          onPlaceLeg={handleParsedOrder}
          onBasketPlace={({ ok, fail }) => {
            if (ok > 0 && fail === 0) {
              // Notify the host (e.g. /admin/derivatives) about the
              // successful place but keep the modal mounted so the
              // operator can place more legs. Mirrors the OrderTicket
              // success path and operator's explicit ask: "i want it
              // to be open like after placing order from order ticket
              // until i close the modal."
              onSubmit?.({ mode: 'paper', _basketLegs: ok });
            }
          }} />

      {/if}

    </div>

    <!-- Shell-level Template picker — sits BELOW the tab body. Two-
         pill toggle (Default / None) mirrors the Side toggle in the
         Ticket tab so the operator's mental model is the same.
         Operator: "so just have default or none like add or close.
         based on the symbol type order type, for default show the
         specific template values."
         Default → side-aware is_default template (resolved at render
                   time via `_sideAwareDefault`; auto-refreshes on
                   side flip + symbol change)
         None    → explicit opt-out, no GTT attach
         No dropdown: the operator never picks the template by name,
         the platform picks the right one for the leg + side. Override
         inputs surface inline only when Default is active and the
         template has the relevant fields. -->
    <!-- Symbol gate — the side-aware Default resolution depends on the
         CE/PE regex against _localSymbol. Without a symbol picked the
         scope would silently fall back to buy_any/sell_any and surface
         the wrong template; better to hide the row entirely until the
         operator has committed to what they're trading. Operator:
         "when no order type selected, template should not be displayed.
         when symbol and buy or sell selected, then template container
         should be displayed". Side is always set (default BUY), so
         symbol presence is the meaningful gate.
         Operator (turn N+1): "in chain, the pill is hidden when
         template is default in chain. in order ticket it shows."
         On Chain tab the operator can drop legs via +CE / +PE without
         typing a header symbol — _localSymbol stays empty in that
         flow, hiding the Template row even though basket legs are
         staged. Treat basket-leg presence as equivalent "symbol
         context" so the Template row stays visible. The shell's
         side-aware default resolver uses _modalSide for direction
         and the focused-leg's symbol (last-leg by default) for the
         CE/PE regex via _appliesToFor — falling back through
         _localSymbol when no legs are staged. -->
    {#if _isDemo && action === 'open'
         && ((_localSymbol || '').trim() || basketLegs.length > 0)}
      <!-- Audit fix (L-3) — demo session sees a single muted note
           where the Template Default/None toggle would render for
           authenticated sessions. Anonymous LIVE/PAPER submits are
           blocked at the API layer; surfacing the full picker would
           promise capabilities the visitor doesn't have. -->
      <div class="oes-basket-tpl-row oes-basket-tpl-row-shell oes-basket-tpl-row-demo">
        <span class="oes-basket-tpl-label">Template</span>
        <span class="oes-basket-tpl-demo-note">Exit rules (TP / SL / Wing) not available in demo.</span>
      </div>
    {:else if _templates.length > 0 && action === 'open'
         && ((_localSymbol || '').trim() || basketLegs.length > 0)}
      <div class="oes-basket-tpl-row oes-basket-tpl-row-shell"
           title={!_shellUsingNone && _selectedTemplate
             ? `${_selectedTemplate.name || _selectedTemplate.slug}${_selectedTemplate.description ? ' — ' + _selectedTemplate.description : ''}`
             : 'Default attaches the saved template that matches the current side + symbol type. None opts out of any GTT attach.'}>
        <TemplateBar
          selectedTemplate={_selectedTemplate}
          sideAwareDefault={_sideAwareDefault}
          showsWing={_sharedTplShowsWing}
          shellUsingNone={_shellUsingNone}
          bind:tpOverride={_sharedTpOverride}
          bind:slOverride={_sharedSlOverride}
          bind:wingStrikeOffsetOverride={_sharedWingStrikeOffsetOverride}
          bind:wingPremPctOverride={_sharedWingPremPctOverride}
          onSelectDefault={() => {
            if (_sideAwareDefault) _sharedTemplateId = _sideAwareDefault.id;
          }}
          onSelectNone={() => {
            if (_noneTpl) _sharedTemplateId = _noneTpl.id;
          }}
        />
        <!-- On-fill preview chip + cap warning. Piped up from OrderTicket
             via onPreviewPlanUpdate (mirrors onMarginUpdate). Visible on
             BOTH tabs because it lives in the shell-level Template
             container. The chip's ₹ values re-compute reactively whenever
             the operator edits TP% / SL% / Wing inputs above.
             - cap warning surfaces broker-capability gaps (Groww OCO,
               Dhan trail, MCX) BEFORE submit so the operator catches
               them at compose time.
             - preview chips render the EXACT triggers Kite GTT / Dhan
               Forever / Groww OCO will set on fill — "TP ₹250 / SL ₹180
               + Wing BUY 22000PE @ ~₹85" — the most useful piece of
               context the operator has at submit time. -->
        {#if !_shellUsingNone}
          <!-- Audit fix (H-5) — when basket has legs spanning ≥1
               account, surface the aggregated cross-account cap
               warning instead of the Ticket-tab's single-account
               warning. Multi-broker baskets (Kite + Dhan + Groww)
               now show the union of broker-specific gaps with each
               warning tagged by the originating account so the
               operator can map back to the affected leg. Empty
               basket falls back to OrderTicket's chip (single-
               account context). -->
          {@const _activeCapWarning = basketLegs.length > 0
            ? _basketCapWarning
            : _modalCapWarning}
          {#if _activeCapWarning && _sharedTemplateId !== null && !_shellUsingNone}
            <div class="oes-tpl-cap-warn" title={_activeCapWarning}>
              ⚠ {_activeCapWarning}
            </div>
          {/if}
          {#if _activePreviewError}
            <div class="oes-tpl-preview-err">⚠ preview: {_activePreviewError}</div>
          {:else if _activePreviewLoading}
            <div class="oes-tpl-preview-loading">resolving plan…</div>
          {:else if _activePreviewPlan && (_activePreviewPlan.gtts?.length > 0 || _activePreviewPlan.wing)}
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <!-- role and tabindex are conditionally 'button'/0 when multi-leg
                 (genuinely interactive) or undefined otherwise. Svelte can't
                 track the conditional so we suppress the false-positive. -->
            <div class="oes-tpl-preview"
                 class:oes-tpl-preview-clickable={_previewFromLeg && basketLegs.length > 1}
                 title={_previewChipTooltip || undefined}
                 role={_previewFromLeg && basketLegs.length > 1 ? 'button' : undefined}
                 tabindex={_previewFromLeg && basketLegs.length > 1 ? 0 : undefined}
                 onclick={(e) => {
                   if (!(_previewFromLeg && basketLegs.length > 1)) return;
                   // Don't intercept clicks on existing chips inside —
                   // they have their own affordances (none today, but
                   // future-proof). Cycle on bare chip-area clicks.
                   const tgt = /** @type {HTMLElement} */ (e.target);
                   if (tgt && tgt !== e.currentTarget && tgt.closest('button, a, input')) return;
                   _cycleFocusedLeg();
                 }}
                 onkeydown={(e) => {
                   if (!(_previewFromLeg && basketLegs.length > 1)) return;
                   if (e.key === 'Enter' || e.key === ' ') {
                     e.preventDefault();
                     _cycleFocusedLeg();
                   }
                 }}>
              <span class="oes-tpl-preview-label">on fill →</span>
              {#if _previewFromLeg}
                <span class="oes-tpl-preview-leg-badge"
                      title={_focusedLegKey
                        ? `Preview locked to leg ${_focusedLegIndex} of ${basketLegs.length}. Click another basket pill to swap, or × the pill to release.`
                        : `Preview tracks the last leg added (${_focusedLegIndex} of ${basketLegs.length}). Click any basket pill to lock to that leg instead.`}>
                  leg {_focusedLegIndex}/{basketLegs.length}{_focusedLegKey ? ' ●' : ''}
                </span>
              {/if}
              {#each _activePreviewPlan.gtts || [] as g}
                <span class="oes-tpl-preview-chip"
                      class:tp={g.label === 'TP'}
                      class:sl={g.label === 'SL'}
                      class:both={g.label === 'TP+SL'}>
                  {g.label} {g.trigger_values?.map(v => '₹' + Number(v).toLocaleString('en-IN')).join(' / ')}
                </span>
              {/each}
              {#if _activePreviewPlan.wing}
                <span class="oes-tpl-preview-chip oes-tpl-preview-wing"
                      title={`Protective BUY leg auto-attached on fill. Reduces SPAN margin and caps tail risk. ${_activePreviewPlan.wing.order_type || 'MARKET'} order, qty matches parent.`}>
                  + Wing BUY {_activePreviewPlan.wing.quantity}× <LegLabel sym={_activePreviewPlan.wing.tradingsymbol} compact={true} />
                  {#if _activePreviewPlan.wing.estimated_price != null && _activePreviewPlan.wing.estimated_price > 0}
                    <span class="oes-tpl-preview-chip-px">@ ~₹{priceFmt(Number(_activePreviewPlan.wing.estimated_price))}</span>
                  {/if}
                </span>
              {/if}
              {#each _activePreviewPlan.notes || [] as n}
                <span class="oes-tpl-preview-note">· {n}</span>
              {/each}
            </div>
          {/if}
        {/if}
      </div>
    {/if}

    <!-- Shell-level basket bar — visible from any tab when legs exist.
         Per-leg pills (B/S · sym · lots stepper · × remove) sit on the
         left; Clear / Submit on the right. Same shape as the chain
         tab's in-tab basket so the operator sees what's pending from
         any tab without flipping back to Chain. -->
    {#if basketLegs.length > 0}
      <div class="oes-basket-bar">
        <!-- Per-account margin strip — shown when basket spans >1 account.
             Single-account baskets keep using the existing common-footer
             margin pill so behaviour is unchanged for the typical case. -->
        {#if _basketMarginRows.length > 1}
          <div class="oes-basket-margin-strip">
            {#each _basketMarginRows as row (row.account)}
              {@const _after = row.available - row.required}
              {@const _cls = row.error ? 'bms-row-err' : row.shortfall > 0 ? 'bms-row-short' : _after < row.required * 0.1 ? 'bms-row-warn' : 'bms-row-ok'}
              <span class="bms-row {_cls}" title={row.error || `${row.account}: Required ₹${aggFmtMargin(row.required)} · Available ₹${aggFmtMargin(row.available)} · After ₹${aggFmtMargin(_after)}`}>
                <span class="bms-acct">{row.account}</span>
                {#if row.error}
                  <span class="bms-err">{row.error.slice(0, 30)}</span>
                {:else}
                  <span class="bms-kv"><span class="bms-k">Req</span><span class="bms-v">₹{aggFmtMargin(row.required)}</span></span>
                  <span class="bms-kv"><span class="bms-k">Avail</span><span class="bms-v">₹{aggFmtMargin(row.available)}</span></span>
                  <span class="bms-kv {row.shortfall > 0 ? 'bms-kv-short' : ''}"><span class="bms-k">After</span><span class="bms-v">₹{aggFmtMargin(_after)}</span></span>
                {/if}
              </span>
            {/each}
          </div>
        {/if}
        <div class="oes-basket-pills" role="list">
          {#each basketLegs as leg, i (leg.key)}
            {@const _legAcct = leg.account || _sharedAccount || ''}
            {@const _isFocused = !!(_focusedLeg && _focusedLeg.key === leg.key)}
            <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
            <!-- Basket pill is a compound widget: the <span role="listitem">
                 contains interactive button children and also responds to
                 click/keydown to focus the preview. Keyboard handler is
                 already attached; the listitem role is intentional for AT. -->
            <span class="oes-basket-pill oes-basket-pill-{leg.side === 'BUY' ? 'buy' : 'sell'} oes-basket-pill-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                  class:is-disabled={basketSubmitting}
                  class:is-focused={_isFocused}
                  role="listitem"
                  title={_isFocused
                    ? 'On-fill preview is focused on this leg. Click another pill to swap.'
                    : 'Click to focus the on-fill preview on this leg. Click × to remove.'}
                  onclick={(e) => {
                    // Don't focus when the click bubbled from a child
                    // interactive element (×, stepper, account select,
                    // template editor, limit input). The target check
                    // covers all of them — buttons + inputs + selects.
                    const tgt = /** @type {HTMLElement} */ (e.target);
                    if (tgt?.closest('button, input, select, label')) return;
                    _focusedLegKey = leg.key;
                  }}
                  onkeydown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      const tgt = /** @type {HTMLElement} */ (e.target);
                      if (tgt?.closest('button, input, select, label')) return;
                      e.preventDefault();
                      _focusedLegKey = leg.key;
                    }
                  }}>
              <span class="oes-basket-pill-side">{leg.side === 'BUY' ? 'B' : 'S'}</span>
              <span class="oes-basket-pill-sym"><LegLabel sym={leg.sym} compact={true} /></span>
              <button type="button" class="oes-basket-pill-step"
                      title="Decrease lots"
                      disabled={basketSubmitting || (leg.lots || 1) <= 1}
                      onclick={() => updateLegByKey(leg.key, b => ({ ...b, lots: Math.max(1, (b.lots || 1) - 1) }))}>−</button>
              <span class="oes-basket-pill-lots">{leg.lots || 1}</span>
              <button type="button" class="oes-basket-pill-step"
                      title="Increase lots"
                      disabled={basketSubmitting}
                      onclick={() => updateLegByKey(leg.key, b => ({ ...b, lots: (b.lots || 1) + 1 }))}>+</button>
              {#if leg.lotSize > 1}
                <span class="oes-basket-pill-qty">× {leg.lotSize} = {(leg.lots || 1) * leg.lotSize}</span>
              {/if}
              <!-- Per-leg limit price — editable so operator can submit
                   outside market hours when bid/ask hasn't pre-filled. -->
              <span class="oes-basket-pill-limit-wrap"
                    title={!(Number(leg.limit) > 0) ? 'Set a limit price to submit' : `Limit ₹${leg.limit}`}>
                <span class="oes-basket-pill-limit-prefix">₹</span>
                <input type="number"
                       class="oes-basket-pill-limit"
                       class:oes-basket-pill-limit-warn={!(Number(leg.limit) > 0)}
                       disabled={basketSubmitting}
                       min="0"
                       step="0.05"
                       value={leg.limit ?? 0}
                       oninput={(e) => {
                         const v = parseFloat(/** @type {HTMLInputElement} */ (e.currentTarget).value) || 0;
                         updateLegByKey(leg.key, b => ({ ...b, limit: v }));
                       }} />
              </span>
              <!-- Per-leg account picker — operator: "I should be able to
                   place order from different accounts by selecting account
                   for each order adding them to basket and place order".
                   Switched from native `<select>` to the platform's
                   custom `<Select>` component to follow the CLAUDE.md
                   convention (no native popups anywhere in the algo
                   surfaces). Same updateLegByKey reassignment pattern
                   so the each-block iteration variable picks up the
                   new value through basketLegs replacement. -->
              {#if _modalAccounts.length > 1}
                <span class="oes-basket-pill-acct-wrap"
                      title="Route this leg through this broker account">
                  <Select
                    value={_legAcct}
                    onValueChange={(v) => {
                      const _v = String(v || '');
                      updateLegByKey(leg.key, b => ({ ...b, account: _v }));
                    }}
                    options={_modalAccounts.map(a => ({ value: a, label: a }))}
                    ariaLabel="Leg account" />
                </span>
              {:else if _legAcct}
                <span class="oes-basket-pill-acct-static" title="Routing account (single broker loaded)">{_legAcct}</span>
              {/if}
              <!-- Inline wing tag dropped — the wing now renders as a
                   separate basket pill AFTER the parent pill closes
                   (see `{#if _legHasWing(leg)}` below) so the operator
                   sees the auto-attached BUY as a distinct order chip
                   per operator request. -->

              {#if action === 'open' && _templates.length > 0}
                {@const _eff = _legEffectiveTpl(leg)}
                {@const _has = _legHasOverride(leg)}
                <button type="button"
                        class="oes-basket-pill-tpl-chip"
                        class:has-override={_has}
                        disabled={basketSubmitting}
                        title={_has
                          ? `Per-leg override active. Click to edit / clear.`
                          : `Inheriting shell defaults. Click to set a per-leg template or override TP / SL / Wing for THIS leg only.`}
                        onclick={() => _toggleLegEditor(leg.key)}>
                  ⚙ {(_eff?.name || _eff?.slug || 'tmpl')}
                </button>
              {/if}
              <button type="button" class="oes-basket-pill-remove"
                      title="Remove leg from basket"
                      disabled={basketSubmitting}
                      onclick={() => removeBasketLeg(i)}>×</button>
            </span>
            <!-- Wing as a SEPARATE pill — operator: "when the wing
                 order to be placed, the wing order also should be
                 shown as an additional order as a chip". Sits
                 immediately after the parent leg so the operator
                 reads the pair "SELL CE + WING BUY PE" as two
                 distinct orders. Read-only — qty + symbol come from
                 the parent leg + template wing-scan. -->
            {#if _sharedWingPlanned && leg.side === 'SELL' && /(CE|PE)$/.test(leg.sym)}
              {@const _wingSym = _wingSymbolFor(leg.sym)}
              <span class="oes-basket-pill oes-basket-pill-buy oes-basket-pill-wing-leg"
                    role="listitem"
                    title={_wingSym
                      ? `Auto-attached wing — BUY ${_wingSym} on parent fill (qty matches the SELL).`
                      : `Auto-attached wing — protective BUY picked from the chain by premium scan at fill time.`}>
                <span class="oes-basket-pill-wing-tag">WING</span>
                <span class="oes-basket-pill-side">B</span>
                <span class="oes-basket-pill-sym"><LegLabel sym={_wingSym || `${leg.sym.replace(/(CE|PE)$/i, m => m.toUpperCase() === 'CE' ? 'PE' : 'CE')}`} compact={true} /></span>
                <span class="oes-basket-pill-lots">{leg.lots || 1}</span>
                {#if leg.lotSize > 1}
                  <span class="oes-basket-pill-qty">× {leg.lotSize} = {(leg.lots || 1) * leg.lotSize}</span>
                {/if}
                <span class="oes-basket-pill-wing-note">auto on fill</span>
              </span>
            {/if}
            {#if _legEditorsOpen.has(leg.key) && action === 'open' && _templates.length > 0}
              <!-- Per-leg override editor — surfaces below the pill in
                   the same flex container so it wraps to a new line.
                   Template Select + 4 numeric inputs bound to leg
                   fields. Empty = inherit shell defaults. -->
              {@const _eff2 = _legEffectiveTpl(leg)}
              {@const _showWing2 = (_eff2?.applies_to || '').toLowerCase() === 'sell_option'}
              <div class="oes-leg-editor" role="group" aria-label={`Override template for ${leg.sym}`}>
                <span class="oes-leg-editor-label">tmpl for {leg.sym}</span>
                <label class="oes-leg-editor-field" title="Override the template for THIS leg only. (shell default) inherits the On-fill picker; (no template) explicitly skips any GTT attach for this leg even when the shell has one set.">
                  <span>on fill</span>
                  <!-- Audit fix — per-leg null-template sentinel.
                       Pre-fix the dropdown only had "(shell default)"
                       which set leg.template_id=null and silently fell
                       through to the shell pick via the `??` operator
                       in submitBasket. The operator could not say
                       "this leg explicitly has NO template, ignore
                       shell". Adding a separate "(no template)"
                       option that maps to the seeded none-template's
                       id covers that case — the id is non-null so
                       the `??` no longer overrides it. The none row
                       is excluded from the each-loop below so it's
                       not duplicated. -->
                  <Select
                    disabled={basketSubmitting}
                    value={leg.template_id ?? ''}
                    options={[
                      { value: '', label: '(shell default)' },
                      ...(_noneTpl ? [{ value: _noneTpl.id, label: '(no template — entry only)' }] : []),
                      ..._nonNoneTemplates.map(t => ({ value: t.id, label: t.name || t.slug || `#${t.id}` })),
                    ]}
                    onValueChange={(v) => {
                      const id = v === '' ? null : Number(v);
                      updateLegByKey(leg.key, b => ({ ...b, template_id: id }));
                    }} />
                </label>
                <label class="oes-leg-editor-field" title="TP% for this leg. Empty = inherit shell / template default.">
                  <span>TP%</span>
                  <input type="number" step="0.5" disabled={basketSubmitting}
                         placeholder={_eff2?.tp_pct != null ? String(_eff2.tp_pct) : '—'}
                         value={leg.tp_pct_override ?? ''}
                         oninput={(e) => {
                           const raw = /** @type {HTMLInputElement} */ (e.currentTarget).value;
                           const v = raw === '' ? null : Number(raw);
                           updateLegByKey(leg.key, b => ({ ...b, tp_pct_override: v }));
                         }} />
                </label>
                <label class="oes-leg-editor-field" title="SL% for this leg. Empty = inherit shell / template default.">
                  <span>SL%</span>
                  <input type="number" step="0.5" disabled={basketSubmitting}
                         placeholder={_eff2?.sl_pct != null ? String(_eff2.sl_pct) : '—'}
                         value={leg.sl_pct_override ?? ''}
                         oninput={(e) => {
                           const raw = /** @type {HTMLInputElement} */ (e.currentTarget).value;
                           const v = raw === '' ? null : Number(raw);
                           updateLegByKey(leg.key, b => ({ ...b, sl_pct_override: v }));
                         }} />
                </label>
                {#if _showWing2}
                  <label class="oes-leg-editor-field" title="Wing strike offset for this leg.">
                    <span>Wing strike+</span>
                    <input type="number" step="50" disabled={basketSubmitting}
                           placeholder={_eff2?.wing_strike_offset != null ? String(_eff2.wing_strike_offset) : '—'}
                           value={leg.wing_strike_offset_override ?? ''}
                           oninput={(e) => {
                             const raw = /** @type {HTMLInputElement} */ (e.currentTarget).value;
                             const v = raw === '' ? null : Number(raw);
                             updateLegByKey(leg.key, b => ({ ...b, wing_strike_offset_override: v }));
                           }} />
                  </label>
                  <label class="oes-leg-editor-field" title="Wing premium % for this leg.">
                    <span>Wing prem%</span>
                    <input type="number" step="0.5" disabled={basketSubmitting}
                           placeholder={_eff2?.wing_premium_pct != null ? String(_eff2.wing_premium_pct) : '—'}
                           value={leg.wing_premium_pct_override ?? ''}
                           oninput={(e) => {
                             const raw = /** @type {HTMLInputElement} */ (e.currentTarget).value;
                             const v = raw === '' ? null : Number(raw);
                             updateLegByKey(leg.key, b => ({ ...b, wing_premium_pct_override: v }));
                           }} />
                  </label>
                {/if}
                <button type="button" class="oes-leg-editor-clear"
                        disabled={basketSubmitting}
                        title="Clear every per-leg override on this leg — fall back to shell defaults."
                        onclick={() => updateLegByKey(leg.key, b => ({
                          ...b,
                          template_id: null,
                          tp_pct_override: null,
                          sl_pct_override: null,
                          wing_premium_pct_override: null,
                          wing_strike_offset_override: null,
                        }))}>clear</button>
                <button type="button" class="oes-leg-editor-close"
                        title="Close editor (overrides persist)."
                        onclick={() => _toggleLegEditor(leg.key)}>×</button>
              </div>
            {/if}
          {/each}
        </div>
        <!-- Operator: "clear submit basket area duplicates common
             buttons". Dropped .oes-basket-actions (Clear + Submit)
             since the common action footer already carries both.
             Result message stays so post-submit feedback is still
             visible alongside the leg pills. -->
        {#if basketResultMsg}
          <div class="oes-basket-meta">
            <span class="oes-basket-result">{basketResultMsg}</span>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Common action footer — same shape as the /orders page-level
         footer (+Basket · Side · BUY/SELL submit). Visible across
         every tab (Ticket / Chain / Command). Inactive on tabs that
         don't accept a generic submit (Chain has its own per-strike
         buttons; CommandLine submits via Enter), but +Basket still
         works when there's pending content. -->
    {#if showCommonActions && !actionsHidden}
      <div class="oes-common-actions">
        <!-- Mode pill + chase controls + Clear lifted to the picker row
             (heading row) above. The dedicated mode/chase row that used
             to sit here was removed per operator request — its space
             merged with the picker row so the modal/page footprint is
             tighter. -->

        <!-- Single action row, three-priority left slot:
               1. Notice (market closed / broker disconnected / preview
                  error) — wins over everything.
               2. Cash info — when the order consumes cash (equity buy/
                  sell, long option premium).
               3. Margin info — for SPAN-collateralised orders (short
                  options, futures).
             Then the button cluster sits on the right. -->
        <div class="oes-common-row">
          {#if _stickyResultMsg}
            <span class="oes-sticky-result oes-sticky-result-{_stickyResultLevel || 'ok'}"
                  title={_stickyResultMsg}>
              {_stickyResultMsg}
            </span>
          {:else if _modalNotice}
            <span class="oes-notice oes-notice-{_modalNotice.level}"
                  title={_modalNotice.detail || _modalNotice.text}>
              {_modalNotice.text}
            </span>
          {:else if (_activeTab === 'ticket' && !_modalSide) || (_activeTab === 'chain' && basketLegs.length === 0)}
            <!-- Operator: "the message Place an order — pick BUY or SELL
                 above should also be displayed after cold start in chain
                 to keep it in sync with ticket. the message should be
                 displayed like a chip both the tabs. and should get
                 updates later for margin, cash related info."
                 Both tabs render a chip-styled cold prompt during their
                 own idle state — Ticket = no side picked, Chain = no
                 basket legs staged. The same slot later gets the
                 margin/cash chip the moment the operator picks a side
                 (Ticket) or stages a leg (Chain). -->
            <span class="oes-cold-prompt"
                  title={_activeTab === 'chain'
                    ? 'Click +CE / +PE on a strike row to stage a basket leg'
                    : 'Pick a side (BUY / SELL) in the form above to enable Submit'}>
              {_activeTab === 'chain'
                ? 'Place an order — pick a strike from the chain'
                : 'Place an order — pick BUY or SELL above'}
            </span>
          {:else if _marginInfo && !(_activeTab === 'chain' && _basketMarginRows.length > 0)}
            <!-- Margin pill is shared between Chain and Ticket so
                 the operator reads the same Required / Avail values
                 regardless of which tab is active. Operator:
                 "margin message should be common between chain and
                 order ticket". The chain-basket aggregate pill on
                 the basket-pill row above stays as a per-leg
                 breakdown; this is the single live preview.
                 Audit fix — on Chain tab with staged legs, the chip's
                 value reflects the Ticket form's stale last-entered
                 state (qty / side / price). Hide it then so the
                 basket-aggregate rows rendered above the basket bar
                 are the operator's single source of truth. -->

            {@const _isCash = !!_marginInfo.isCashMode}
            {@const _reqKey = _isCash ? 'Cost' : 'Req'}
            {@const _avlKey = _isCash ? 'Cash' : 'Avail'}
            {@const _kind = _isCash ? 'Cash debit' : 'Margin required'}
            <!-- Operator: "for the margin area allocate to two rows
                 of space". Required + Available stack on two lines
                 (kept inline-grid for tight alignment). Buttons
                 below match the new pill height. -->
            <span class="oes-margin-pill oes-margin-pill-{_marginPillCls} oes-margin-pill-stack"
                  title={_marginInfo.error
                    ? `Preview: ${_marginInfo.error}`
                    : _marginInfo.loading
                      ? `Computing ${_kind.toLowerCase()}…`
                      : `${_kind}: ₹${aggFmtMargin(_marginInfo.required)}${_marginInfo.pairedCount > 0 ? ' (parent + auto-wing, basket-net margin)' : ''} vs ${_avlKey} ₹${aggFmtMargin(_marginInfo.available ?? 0)}`}>
              {#if _marginInfo.error}
                ⚠ {_marginInfo.error}
              {:else if _marginInfo.loading}
                Computing {_isCash ? 'cost' : 'margin'}…
              {:else}
                <span class="oes-margin-pill-row">
                  <span class="oes-margin-pill-key">{_reqKey}</span>
                  <span class="oes-margin-pill-val">₹{aggFmtMargin(_marginInfo.required)}</span>
                  {#if _marginInfo.pairedCount > 0}
                    <span class="oes-margin-pill-paired" title="Margin reflects parent + auto-attached wing (basket-net)">+wing</span>
                  {/if}
                </span>
                {#if _marginInfo.available != null}
                  <span class="oes-margin-pill-row">
                    <span class="oes-margin-pill-key">{_avlKey}</span>
                    <span class="oes-margin-pill-val">₹{aggFmtMargin(_marginInfo.available)}</span>
                  </span>
                {/if}
              {/if}
            </span>
          {/if}
          <!-- Ticket-only side picker. Operator: "buy and sell should
               be one single button. when add/close active it should
               prefix add/close. based on short or long existing
               position it should derive buy or sell. id button has
               more show in two rows in the button without changing
               button height." Two-line layout when in symbol-row
               context: line 1 = ADD/CLOSE verb, line 2 = derived
               broker side. Cold context: single-line BUY/SELL or
               "Pick side". -->
          {#if _activeTab === 'ticket' && action !== 'modify'}
            {@const _cq = Number(_ticketProps?.currentQty ?? currentQty) || 0}
            <button type="button"
                    class="oes-footer-side-btn-single"
                    class:on-buy={_modalSide === 'BUY'}
                    class:on-sell={_modalSide === 'SELL'}
                    class:on-none={!_modalSide}
                    class:is-stacked={!!_modalSide && _cq !== 0}
                    title={!_modalSide
                      ? (_cq === 0
                          ? 'Pick a side — click to set BUY (click again to flip to SELL)'
                          : 'Pick — click to ADD to the existing position')
                      : (_cq === 0
                          ? `Side is ${_modalSide} — click to switch to ${_modalSide === 'BUY' ? 'SELL' : 'BUY'}`
                          : `${_addCloseVerb(_modalSide)} via ${_modalSide} — click to switch to ${_modalSide === 'BUY' ? 'SELL' : 'BUY'}`)}
                    onclick={_cycleSide}>
              {#if !_modalSide}
                <span>Pick side</span>
              {:else if _cq !== 0}
                <span class="oes-side-line oes-side-line1">{_addCloseVerb(_modalSide)}</span>
                <span class="oes-side-line oes-side-line2">{_modalSide}</span>
              {:else}
                <span>{_modalSide}</span>
              {/if}
            </button>
          {/if}
          <button type="button" class="oes-common-submit"
            class:oes-common-submit-buy={_submitFlavor === 'buy'}
            class:oes-common-submit-sell={_submitFlavor === 'sell'}
            class:oes-common-submit-basket={basketLegs.length > 0 || _submitFlavor === 'basket' || (_activeTab === 'ticket' && _toBasket)}
            class:oes-common-submit-narrow={basketLegs.length > 0}
            title={basketLegs.length > 0
              ? `Submit all ${basketLegs.length} basket leg${basketLegs.length > 1 ? 's' : ''}`
              : (_activeTab === 'chain'
                  ? 'Add legs via +CE / +PE on the chain rows first'
                  : _toBasket
                    ? 'Add this ticket as a basket leg'
                    : 'Place the order')}
            disabled={basketSubmitting
                      || (basketLegs.length === 0 && _activeTab === 'chain')}
            onclick={() => {
              if (basketLegs.length > 0) {
                // Global basket submit — fires from any tab whenever there
                // are staged legs. Ticket's _toBasket mode stays intact:
                // when the operator toggles basket on Ticket and clicks
                // Submit with no legs yet, _modalFireBasket() adds the
                // current ticket as the first leg without submitting.
                submitBasket();
              } else if (_activeTab === 'ticket' && _toBasket) {
                _modalFireBasket();
              } else if (_activeTab === 'ticket') {
                _modalFireSubmit();
              }
            }}>{basketSubmitting
                ? 'Placing…'
                : (basketLegs.length > 0
                    ? `Submit (${basketLegs.length})`
                    : (_activeTab === 'ticket' && _toBasket)
                        /* Operator: "change submit to basket to just
                           submit in ticket with bracket count of orders
                           like chain." Show count AFTER click — clicking
                           adds this ticket as a new leg, so basket goes
                           from N to N+1. */
                        ? `Submit (${basketLegs.length + 1})`
                        : _submitLabel)}</button>
          <!-- Basket icon at the trailing edge. Operator:
               · "in chain tab, basket icon enabled by default" →
                 Chain renders the icon in the `.on` state but as a
                 read-only span (Chain is always basket-mode).
               · "active basket icon should have a different background
                 color" → `.on` state now uses a stronger sky accent +
                 amber border to read distinctly from the resting state.
               · "if you have a better icon for basket use it" →
                 swapped the previous slanted basket for Lucide's
                 shopping-cart silhouette (wheels + handle), which is
                 the canonical e-commerce icon. -->
          {#if _activeTab === 'chain'}
            <span class="oes-common-basket-toggle oes-common-basket-toggle-icon on is-static"
                  title="Chain orders are always basket orders — the basket builds from each +CE / +PE / +Fut click."
                  aria-label="Basket mode (always on for Chain)">
              <svg width="16" height="16" viewBox="0 0 24 24"
                   fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">
                <circle cx="8" cy="21" r="1" />
                <circle cx="19" cy="21" r="1" />
                <path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12" />
              </svg>
            </span>
          {:else}
            <label class="oes-common-basket-toggle oes-common-basket-toggle-icon"
                   class:on={_toBasket}
                   title={_toBasket
                     ? 'Basket mode ON — Submit will add the current ticket as a basket leg'
                     : 'Basket mode OFF — Submit will place the order immediately'}>
              <input type="checkbox" bind:checked={_toBasket} class="sr-only" />
              <svg width="16" height="16" viewBox="0 0 24 24"
                   fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">
                <circle cx="8" cy="21" r="1" />
                <circle cx="19" cy="21" r="1" />
                <path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12" />
              </svg>
            </label>
          {/if}
        </div>
      </div>
    {/if}

    <!-- Chases card — mirrors the /orders page Chases section so the
         modal stays in sync. Self-hides on idle so it costs nothing
         when no chases are in flight. Suppressed when the host renders
         its own Chase card alongside (e.g. /orders page passes
         hideBottomPanel). -->
    {#if !hideBottomPanel}
      <div class="oes-chase-slot">
        <ChaseCard pollMs={3000} />
      </div>
    {/if}

    <!-- Activity panel — canonical 6-tab LogPanel (Orders · Agents ·
         Terminal · Ticks · System · News). Placed BELOW the common
         action footer per operator request ("move the order placement
         above the activity"). Suppressed when the host renders the
         activity in a separate surface via `hideBottomPanel`. -->
    {#if !hideBottomPanel}
      <!-- LogPanel mount mirrors ActivityLogModal exactly so both
           surfaces render their tabs identically. The same heightClass
           ("flex-1 min-h-0") is what makes Orders cards show on the
           Activity modal — the previous custom oes-bottom-scroll class
           pinned the inner scroll cap to a fixed max-height that hid
           the card list and prevented other tabs from rendering. -->
      <div class="oes-bottom-panel">
        <ActivityLogSurface
          context="card"
          heightClass="flex-1 min-h-0"
          label="Log"
          defaultTab="order"
          hideInlineAccountFilter={false}
        />
      </div>
    {/if}

  </div>
</div>

<!-- Chart modal — opened by the chart-icon button in the header.
     Rendered at top-level so it sits above the SymbolPanel overlay
     (z-index 200 > 100). Only mounted when the operator clicks the
     chart button and a symbol is set. -->
{#if _chartModalOpen && _localSymbol}
  <ChartModal
    symbol={_localSymbol}
    exchange={exchange}
    mode="live"
    onClose={() => _chartModalOpen = false} />
{/if}

<style>
  /* In MODAL mode the outer overlay + panel sizing is supplied by
     canonical-modal-overlay / canonical-modal-panel (app.css) so this
     modal lands at the same viewport position + size as ChartModal /
     ActivityLogModal. The local .oes-overlay / .oes-modal rules are
     scoped to chrome-only adjustments (text color, font family) and
     the inline-mode override below. */
  .oes-overlay {
    /* Modal-mode-only: scrollable body inside the fixed-height
       canonical-modal-panel. */
    color: var(--algo-slate);
    font-family: var(--font-numeric);
  }
  /* Inline mode strips the modal chrome — used by /console which hosts
     the shell as the page's primary content. */
  .oes-overlay.oes-inline {
    position: static;
    inset: auto;
    background: transparent;
    display: block;
    z-index: auto;
    padding: 0;
  }
  .oes-modal {
    /* Modal-mode allows the body to scroll inside the fixed panel
       height — operator's order ticket + chain + bottom log panel
       together can exceed 760 px on small viewports. */
    overflow-y: auto;
  }
  .oes-modal.oes-modal-inline {
    /* Outer chrome stripped (the host bucket-card provides amber
       accent + gradient).
       Operator: "the orders page is not scrolling. it is showing
       footer behind order activity". An explicit min-height here
       was forcing the bucket-card to a tall viewport-relative
       size and breaking the page's natural document scroll —
       Order Activity ran off the viewport without ever pushing
       the footer down. Switched to a content-driven height:
       the panel grows with the chain grid / depth ladder.
       display:flex column is REQUIRED — .oes-body uses flex:1 to
       absorb the remaining space; without a flex column parent
       the body collapsed to zero height and chain rows + depth
       ladder rendered invisibly. min-height on .oes-body now
       gives the body a reasonable floor so the chain tab still
       has room for ~15 strikes when first opened. */
    width: 100%;
    display: flex;
    flex-direction: column;
    border-radius: 0;
    box-shadow: none;
    border: none;
    background: transparent;
  }
  .oes-modal.oes-modal-inline > .oes-body { min-height: 18rem; }
  /* Operator: "The line below modal headers is too prominent.
     Reduce its prominence. Instead, change background color of
     the header for modals." Stronger amber-tinted gradient bg
     acts as the visual separator from the body; bottom border is
     a hairline (1px low-alpha). */
  /* Plain title text — operator: "remove pill kind of decoration
     for modal header text". Bold uppercase amber glyphs on the
     navy gradient strip; the gradient itself is the prominence.
     Typography tokens locked to canonical .algo-card-title so
     "Order entry" reads at the same intensity as "Greeks" / "Snapshot"
     — operator (2026-07-01): "Order entry vs Greeks — many examples
     like that. GREEKS is good. Make them consistent and uniform." */
  .oes-modal-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-sm);
    color: var(--c-action);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .oes-modal-name-icon { color: currentColor; flex-shrink: 0; }
  /* Shared Symbol picker — replaces the static `.oes-title` placeholder
     ("Symbol") with a live search input. Operator can pick a new
     symbol; every tab (Ticket / Chain / Command) re-renders against
     the chosen instrument. Same amber typography as the old title so
     the position reads identically. */
  .oes-sym-pick {
    position: relative;
    display: inline-flex;
    align-items: center;
    /* Take the remaining row space so Symbol gets the largest slot. */
    flex: 1 1 0;
    min-width: 0;
  }
  .oes-sym-pick :global(.ssi-wrap) { width: 100%; }
  .oes-sym-pick :global(.ssi-input) { width: 100%; min-width: 0; }
  /* Account dropdown — placeholder "Account" reads as its label when
     nothing is picked. Narrow Select pinned next to the symbol combo. */
  /* Picker row — Account · Symbol type · Symbol — between header and
     tabs. Mirrors the chart workspace's .cw-picker bar so the two
     modals look the same. flex-wrap: nowrap so all 3 controls stay
     on a single row at common viewport widths. */
  .oes-picker {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.4rem 0.4rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.10);
    flex-wrap: nowrap;
    flex-shrink: 0;
    min-width: 0;
  }
  /* Cluster inside `.oes-header` — sits immediately AFTER the title
     chip (left-aligned per operator). The close X carries
     margin-left:auto to stay anchored at the right edge. */
  .oes-right-group {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    margin-left: auto;
    flex-shrink: 0;
  }
  .oes-header-cluster {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    flex-shrink: 0;
  }
  /* Type filter — narrowed so it fits in the same row as Account +
     Symbol. The "EQ · FUT · OPT" label gets ellipsised when not the
     active selection; the active value renders fully (Equity /
     Futures / Options all fit). */
  .oes-type-wrap {
    width: 5.5rem;
    flex-shrink: 0;
  }
  .oes-type-wrap :global(.rbq-select-trigger) { width: 100%; }
  /* Account picker — narrower so the picker row fits Account + Type +
     Symbol on a single line at common viewport widths. 10 chars of
     account code fit at 5.5rem in monospace. */
  .oes-account-pick {
    flex-shrink: 0;
    width: 5.5rem;
  }
  .oes-account-pick :global(.rbq-select-trigger) { width: 100%; }
  .oes-account-single {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--algo-slate);
    padding: 0.18rem 0.45rem;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 3px;
    flex-shrink: 0;
  }
  .oes-sym-input {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    padding: 0.18rem 0.45rem;
    color: var(--c-action);
    font-size: var(--fs-xl);
    font-weight: 800;
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    width: 11rem;
    text-transform: uppercase;
  }
  .oes-sym-input:focus,
  .oes-sym-input:focus-visible {
    /* Override the global :focus-visible amber ring (app.css) so the
       input shows only its own amber border — was stacking as a
       2 px outer outline + 1 px inner border. */
    outline: none !important;
    border-color: var(--algo-amber-border);
    background: rgba(251, 191, 36, 0.06);
  }
  .oes-sym-input::placeholder {
    color: rgba(251, 191, 36, 0.40);
    font-weight: 600;
  }
  .oes-sym-drop {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 60;
    margin-top: 2px;
    min-width: 14rem;
    max-height: 14rem;
    overflow-y: auto;
    background: #1b2540;
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.55);
    display: flex;
    flex-direction: column;
  }
  .oes-sym-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.2rem 0.4rem;
    background: transparent;
    border: 0;
    color: var(--algo-slate);
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
    cursor: pointer;
    text-align: left;
    width: 100%;
  }
  .oes-sym-row:hover {
    background: rgba(251, 191, 36, 0.12);
    color: var(--c-action);
  }
  .oes-sym-row-sym {
    font-weight: 700;
    letter-spacing: 0.03em;
  }
  .oes-sym-row-meta {
    color: var(--algo-muted);
    font-size: var(--fs-xs);
    letter-spacing: 0.06em;
  }
  /* Exchange tag — small, muted, matches the LogPanel chip palette. */
  .oes-exch {
    color: var(--algo-muted);
    background: rgba(126, 151, 184, 0.15);
    border: 1px solid rgba(126, 151, 184, 0.32);
    padding: 0.06rem 0.32rem;
    border-radius: 2px;
    font-size: var(--fs-xs);
    letter-spacing: 0.06em;
    font-family: var(--font-numeric);
  }
  /* Push the close button to the right edge regardless of how many
     header chips render. */
  /* .oes-close margin handled by its main rule below — auto-pushed to
     the right edge of the header by chart-button's flex parent. */

  /* +W (add to watchlist) — outlined champagne button, sits between
     the price chips and the close button. Visible only when the
     caller wires `onAddToWatchlist`. */
  .oes-wl-add {
    margin-left: auto;
    background: transparent;
    border: 1px solid rgba(251,191,36,0.45);
    color: var(--c-action);
    padding: 0.18rem 0.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.06em;
    line-height: 1;
  }
  .oes-wl-add:hover:not(:disabled) {
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.7);
  }
  .oes-wl-add:disabled { opacity: 0.55; cursor: wait; }
  /* When +W is rendered, the close button no longer needs to push
     itself; +W has margin-left:auto and close sits next to it. */
  /* .oes-wl-add ~ .oes-close adjacency rule retired with the +W button. */
  /* Brief success/error flash next to the +W button after a click. */
  .oes-wl-toast {
    padding: 0.18rem 0.45rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .oes-wl-toast.ok  { color: var(--c-long); background: rgba(74,222,128,0.16); }
  .oes-wl-toast.err { color: var(--c-short); background: rgba(248,113,113,0.16); }

  /* Chart icon button — opens ChartModal for the current symbol.
     Same 1.4rem × 1.4rem dimensions + cyan-400 palette as the
     card-control trio (CollapseButton, FullscreenButton). */
  .oes-chart-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(34, 211, 238, 0.40);
    border-radius: 4px;
    color: var(--c-info);
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    padding: 0;
  }
  .oes-chart-btn:hover:not(:disabled) {
    background: var(--c-info-14);
    color: #67e8f9;
    border-color: rgba(103, 232, 249, 0.65);
  }
  .oes-chart-btn:disabled {
    opacity: 0.38;
    cursor: not-allowed;
  }
  /* Close button — matches ChartModal's .cm-close palette (red border
     + red text) so the operator sees the same close affordance across
     every modal. Top-right of the header, 1.8rem square, font-size
     1.1rem so the × glyph reads from a quick glance. */
  .oes-close {
    /* Standard close button — square 1.4rem matches ChartModal's
       refresh + close buttons; glyph 0.95rem is proportional to the
       0.72rem header title text. */
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    color: var(--c-short);
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    cursor: pointer;
    font-family: monospace;
    font-size: var(--fs-xl);
    line-height: 1;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s;
  }
  .oes-close:hover { background: rgba(248, 113, 113, 0.15); }

  /* Tab strip wrapper — padding + flex-shrink only; AlgoTabs renders the
     button row via the global .algo-tab rules in app.css. */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 0.4rem;
    flex-shrink: 0;
  }

  /* Basket bar — sticky bottom strip inside the modal when legs exist. */
  /* Per-account basket margin strip */
  .oes-basket-margin-strip {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.6rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid rgba(74,222,128,0.18);
    margin-bottom: 0.2rem;
  }
  .bms-row {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    border: 1px solid;
  }
  .bms-row-ok    { border-color: rgba(74,222,128,0.35); background: rgba(74,222,128,0.08); color: var(--c-long); }
  .bms-row-warn  { border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.08); color: var(--c-action); }
  .bms-row-short { border-color: rgba(248,113,113,0.45); background: var(--c-short-10); color: var(--c-short); }
  .bms-row-err   { border-color: rgba(248,113,113,0.35); background: rgba(248,113,113,0.08); color: var(--c-short); }
  .bms-acct { font-weight: 700; margin-right: 0.15rem; }
  .bms-kv   { display: inline-flex; gap: 0.15rem; }
  .bms-k    { opacity: 0.65; }
  .bms-v    { font-variant-numeric: tabular-nums; }
  .bms-kv-short .bms-v { color: var(--c-short); }
  .bms-err  { opacity: 0.85; }

  /* On-fill template picker row — lifted out of .oes-basket-bar in
     commit 39184013 and now lives at SymbolPanel shell level (above
     the sticky basket-bar) so it's visible on every tab + the modal.
     Operator picks once; submitBasket threads template_id into every
     leg. Active-template chip sits inline so the operator sees the
     identity of the rule that will attach to each leg's fill. */
  .oes-basket-tpl-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    flex-wrap: wrap;
  }
  /* Shell-level template row — sits BELOW the tab body, above the
     basket bar. Repalette: violet (0.06 bg / 0.18 borders) was dull
     and blended into the surrounding navy. Switched to a deeper
     navy-into-amber gradient with stronger amber accent borders so
     the row reads as the algo primary action band. Amber matches the
     platform's "this is the algo primary accent" everywhere else
     (template chip, agent rules, GTT pills). */
  /* Operator: "keep template container background a little different.
     it is dominating everything". Toned down — amber gradient dropped
     in favour of a faint slate tint that sits a half-step above the
     surrounding navy. Borders use a low-alpha slate so the row reads
     as "distinct band" without competing with the picker row or the
     chase strip. */
  .oes-basket-tpl-row-shell {
    padding: 0.45rem 0.7rem;
    background: rgba(15, 25, 45, 0.55);
    border-top: 1px solid rgba(125, 211, 252, 0.16);
    border-bottom: 1px solid rgba(125, 211, 252, 0.16);
    box-shadow: none;
    box-sizing: border-box;
  }
  /* Demo-mode variant — muted slate accent instead of amber so the
     row reads as "not active" without competing for attention. */
  .oes-basket-tpl-row-demo {
    background: rgba(13, 22, 38, 0.45);
    border-color: rgba(148, 163, 184, 0.30);
    box-shadow: none;
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .oes-basket-tpl-demo-note {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: rgba(180, 200, 230, 0.65);
    font-style: italic;
  }
  /* .oes-basket-tpl-pick, .oes-basket-tpl-params, .oes-basket-tpl-param,
     .oes-tpl-toggle, .oes-tpl-btn, .oes-basket-tpl-name
     — moved to TemplateBar.svelte (Phase 3 extraction). */
  .oes-basket-tpl-label {
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 800;
    color: var(--algo-amber, var(--c-action));
    font-size: var(--fs-sm);
  }
  /* Cap warning + on-fill preview chips — lifted from OrderTicket so
     they're visible on both Ticket and Chain tabs. Same palette family
     as the OrderTicket version so the visual identity is preserved:
     cyan tinted background, TP green, SL red, Both amber, Wing violet. */
  .oes-tpl-cap-warn {
    flex-basis: 100%;
    margin-top: 0.2rem;
    padding: 0.18rem 0.42rem;
    border-radius: 3px;
    font-size: var(--fs-sm);
    line-height: 1.25;
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.40);
  }
  .oes-tpl-preview {
    flex-basis: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    align-items: center;
    margin-top: 0.25rem;
    padding: 0.3rem 0.45rem;
    background: rgba(34, 211, 238, 0.06);
    border: 1px solid var(--c-info-22);
    border-radius: 3px;
    font-size: var(--fs-sm);
    line-height: 1.35;
    transition: background 0.12s, border-color 0.12s;
  }
  /* Clickable variant — Chain tab with 2+ legs. Cursor + hover ring
     surface the "click to cycle" affordance without a textual hint.
     Keyboard focus ring matches so Tab + Enter cycles too. */
  .oes-tpl-preview-clickable {
    cursor: pointer;
  }
  .oes-tpl-preview-clickable:hover {
    background: var(--c-info-14);
    border-color: rgba(34, 211, 238, 0.45);
  }
  .oes-tpl-preview-clickable:focus-visible {
    outline: 2px solid rgba(165, 180, 252, 0.65);
    outline-offset: 1px;
  }
  .oes-tpl-preview-label {
    color: rgba(180, 200, 230, 0.85);
    font-weight: 600;
    margin-right: 0.15rem;
    font-family: var(--font-numeric);
  }
  .oes-tpl-preview-chip {
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-weight: 600;
    color: rgba(220, 230, 245, 0.92);
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(180, 200, 230, 0.20);
  }
  .oes-tpl-preview-chip.tp {
    color: var(--c-long);
    background: var(--c-long-10);
    border-color: rgba(74, 222, 128, 0.40);
  }
  .oes-tpl-preview-chip.sl {
    color: var(--c-short);
    background: var(--c-short-10);
    border-color: rgba(248, 113, 113, 0.40);
  }
  .oes-tpl-preview-chip.both {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.10);
    border-color: rgba(251, 191, 36, 0.40);
  }
  .oes-tpl-preview-chip.oes-tpl-preview-wing {
    color: #c084fc;
    background: rgba(192, 132, 252, 0.10);
    border-color: rgba(192, 132, 252, 0.40);
  }
  .oes-tpl-preview-chip-px {
    margin-left: 0.3rem;
    color: rgba(192, 132, 252, 0.78);
    font-weight: 500;
  }
  /* Leg badge — small pill that labels the chip as reflecting a
     specific basket leg's preview (Chain tab when basket has legs).
     Slate-blue family so it reads as a context tag, not a direction
     indicator. */
  .oes-tpl-preview-leg-badge {
    padding: 0.05rem 0.38rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #a5b4fc;
    background: rgba(165, 180, 252, 0.10);
    border: 1px solid rgba(165, 180, 252, 0.35);
    margin-right: 0.1rem;
  }
  .oes-tpl-preview-note {
    color: rgba(180, 200, 230, 0.6);
    font-style: italic;
  }
  .oes-tpl-preview-loading {
    flex-basis: 100%;
    font-size: var(--fs-sm);
    color: rgba(180, 200, 230, 0.55);
    font-family: var(--font-numeric);
    padding-left: 0.45rem;
    margin-top: 0.25rem;
  }
  .oes-tpl-preview-err {
    flex-basis: 100%;
    font-size: var(--fs-sm);
    color: #fca5a5;
    padding: 0.25rem 0.4rem;
    margin-top: 0.25rem;
    background: rgba(248, 113, 113, 0.08);
    border: 1px solid rgba(248, 113, 113, 0.30);
    border-radius: 3px;
  }
  .oes-basket-tpl-note {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.5rem;
    background: rgba(125, 211, 252, 0.10);
    border: 1px solid rgba(125, 211, 252, 0.28);
    border-radius: 3px;
    font-family: monospace;
    font-size: var(--fs-xs);
    color: #c8d8f0;
  }
  .oes-basket-tpl-note-arrow { color: #7dd3fc; font-weight: 700; }
  .oes-basket-tpl-note-name  { color: #7dd3fc; font-weight: 700; }
  .oes-basket-tpl-note-desc  { color: rgba(200, 216, 240, 0.6); }

  .oes-basket-bar {
    position: sticky;
    bottom: 0;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem 0.7rem;
    padding: 0.55rem 1rem;
    border-top: 2px solid var(--c-long);
    background: rgba(74,222,128,0.18);
    box-shadow: inset 0 4px 12px rgba(0,0,0,0.25);
    flex-shrink: 0;
    z-index: 2;
  }
  .oes-basket-result {
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--algo-slate);
    margin-right: 0.4rem;
  }
  .oes-basket-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  /* .oes-basket-actions retired with the Submit/Clear lift to the
     common footer (656be671). Audit defect #12. */

  /* Per-leg pills. Mirror the chain-tab basket palette so an
     operator scanning the bottom strip from any tab gets the same
     B / S colour, sym, lots stepper, and × remove affordance. */
  .oes-basket-pills {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.35rem;
    align-items: center;
    flex: 1 1 auto;
    min-width: 0;
  }
  .oes-basket-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.18rem 0.4rem;
    border-radius: 2px;
    border: 1px solid;
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 1;
    white-space: nowrap;
    /* Cap each pill at the basket-bar's inner width so a fully-loaded
       pill (sym + stepper + lots + qty + limit input + acct + wing
       chip + tmpl chip + remove) can't horizontally overflow the
       container on narrow viewports. min-width:0 lets internal flex
       children shrink. */
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }
  /* Mobile (< 720 px) — flip the pill from a single nowrap row to a
     wrapping flex container so the limit input + acct select + wing
     + tmpl chips can drop to a second line instead of pushing past
     the modal's right edge. Operator: "the order chips overflowing
     the container horizontally on mobile." Each chip keeps its own
     fixed width; the pill grows vertically rather than horizontally. */
  @media (max-width: 720px) {
    .oes-basket-pill {
      flex-wrap: wrap;
      white-space: normal;
      row-gap: 0.2rem;
    }
    /* Tighten the inner inputs so two of them can sit side-by-side on
       narrow viewports without forcing a wrap on every chip. */
    .oes-basket-pill-limit { width: 3.2rem !important; }
    .oes-basket-pill-acct-wrap,
    .oes-basket-pill-acct-static { max-width: 4.5rem; }
    .oes-basket-pill-wing,
    .oes-basket-pill-tpl-chip { max-width: 6rem; overflow: hidden; text-overflow: ellipsis; }
  }
  .oes-basket-pill-buy {
    color:        #67e8f9;
    border-color: rgba(103,232,249,0.55);
    background:   rgba(103,232,249,0.10);
  }
  .oes-basket-pill-sell {
    color:        var(--c-action);
    border-color: rgba(251,191,36,0.55);
    background:   rgba(251,191,36,0.10);
  }
  .oes-basket-pill-side {
    font-weight: 900;
    padding-right: 0.18rem;
    border-right: 1px solid currentColor;
    opacity: 0.9;
  }
  .oes-basket-pill-sym {
    font-weight: 800;
    color: #f1f7ff;
  }
  .oes-basket-pill-step {
    border: none;
    background: transparent;
    color: currentColor;
    font-weight: 800;
    font-family: monospace;
    cursor: pointer;
    padding: 0 0.2rem;
    line-height: 1;
  }
  .oes-basket-pill-step:hover:not(:disabled) { color: #fff; }
  .oes-basket-pill-step:disabled { opacity: 0.35; cursor: not-allowed; }
  .oes-basket-pill-lots {
    min-width: 1rem;
    text-align: center;
    font-weight: 800;
    color: #f1f7ff;
  }
  .oes-basket-pill-qty {
    font-size: var(--fs-2xs);
    opacity: 0.7;
    font-weight: 600;
  }
  /* Per-leg account picker — wrapper around the custom <Select>
     component so the popup matches the platform's account-picker
     style everywhere. Sized narrow so two-leg pills don't blow up
     the basket bar's horizontal budget. */
  .oes-basket-pill-acct-wrap {
    margin-left: 0.35rem;
    max-width: 5.5rem;
    font-size: var(--fs-xs);
  }
  .oes-basket-pill-acct-wrap :global(.algo-select-btn) {
    height: 1.2rem;
    padding: 0 0.25rem;
    background: rgba(255,255,255,0.06);
    color: var(--algo-slate);
    border: 1px solid rgba(125,211,252,0.32);
    border-radius: 2px;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
  }
  .oes-basket-pill-acct-wrap :global(.algo-select-btn:hover) {
    border-color: rgba(125,211,252,0.65);
  }
  .oes-basket-pill-acct-static {
    margin-left: 0.35rem;
    color: #7dd3fc;
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    opacity: 0.85;
  }
  .oes-basket-pill-remove {
    border: none;
    background: transparent;
    color: rgba(255,255,255,0.55);
    font-weight: 900;
    font-family: monospace;
    cursor: pointer;
    padding: 0 0.2rem;
    margin-left: 0.1rem;
    line-height: 1;
  }
  .oes-basket-pill-remove:hover:not(:disabled) { color: var(--c-short); }
  .oes-basket-pill-remove:disabled { opacity: 0.35; cursor: not-allowed; }
  /* Wing-attach indicator chip — sits inline inside a SELL option
     pill, before the × remove button. Purple palette matches the
     wing chip in OrderTicket's template preview row + the OrderCard
     "wing/wings:" chip so the paired-leg identity reads consistently
     across surfaces. Compact + tabular-nums so the symbol string
     doesn't shift the pill's height. */
  .oes-basket-pill-wing {
    display: inline-flex;
    align-items: center;
    margin-left: 0.25rem;
    padding: 0.05rem 0.35rem;
    background: rgba(192, 132, 252, 0.14);
    border: 1px solid rgba(192, 132, 252, 0.45);
    border-radius: 3px;
    color: #c084fc;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: 0.02em;
    font-variant-numeric: tabular-nums;
  }
  /* Wing AS a basket pill — operator: "the wing order also should be
     shown as an additional order as a chip". Same shape as the parent
     buy pill so it reads as an order; violet WING tag at the front so
     the operator can tell at a glance "this is an auto-attached
     wing, not a leg I added myself". Dashed border + slightly muted
     bg distinguishes it from operator-added legs. */
  .oes-basket-pill-wing-leg {
    cursor: default !important;
    border-style: dashed !important;
    opacity: 0.92;
  }
  .oes-basket-pill-wing-tag {
    display: inline-flex;
    align-items: center;
    padding: 0 0.32rem;
    margin-right: 0.3rem;
    height: 1.05rem;
    background: rgba(192, 132, 252, 0.18);
    border: 1px solid rgba(192, 132, 252, 0.55);
    border-radius: 2px;
    color: #c084fc;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .oes-basket-pill-wing-note {
    margin-left: 0.35rem;
    color: rgba(192, 132, 252, 0.75);
    font-size: var(--fs-2xs);
    font-style: italic;
    font-weight: 500;
    letter-spacing: 0.02em;
  }
  /* Per-leg template chip — click to open the override editor.
     Subtle slate-grey when inheriting shell; cyan-tinted when the
     leg has any per-leg override active so the operator can spot
     deviating legs at a glance. */
  .oes-basket-pill-tpl-chip {
    display: inline-flex;
    align-items: center;
    margin-left: 0.25rem;
    padding: 0.05rem 0.35rem;
    background: rgba(126, 151, 184, 0.16);
    border: 1px solid rgba(126, 151, 184, 0.40);
    border-radius: 3px;
    color: #c8d8f0;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: 0.02em;
    cursor: pointer;
    max-width: 7rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition: background 0.1s, border-color 0.1s;
  }
  .oes-basket-pill-tpl-chip:hover:not(:disabled) {
    background: rgba(126, 151, 184, 0.26);
    border-color: rgba(126, 151, 184, 0.70);
  }
  .oes-basket-pill-tpl-chip.has-override {
    background: rgba(34, 211, 238, 0.16);
    border-color: rgba(34, 211, 238, 0.55);
    color: #67e8f9;
  }
  .oes-basket-pill-tpl-chip.has-override:hover:not(:disabled) {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.80);
  }
  .oes-basket-pill-tpl-chip:disabled { opacity: 0.45; cursor: not-allowed; }

  /* Per-leg override editor — sits below the pill via flex-wrap.
     Same cyan family as the chip's active state so the visual link
     reads cleanly. Compact density matches the rest of the basket
     bar; numeric inputs share the override-input shape used by the
     shell-level row. */
  .oes-leg-editor {
    flex: 0 0 100%;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem;
    margin: 0.15rem 0 0.3rem 0.4rem;
    padding: 0.3rem 0.5rem;
    background: rgba(34, 211, 238, 0.06);
    border: 1px solid rgba(34, 211, 238, 0.30);
    border-radius: 4px;
  }
  .oes-leg-editor-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: rgba(200, 216, 240, 0.7);
  }
  .oes-leg-editor-field {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-family: monospace;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
  }
  .oes-leg-editor-field > span {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 700;
  }
  /* Per-leg override inputs — same high-contrast treatment as the
     shell-level "On fill" param inputs (commit ba4d1d59). Pre-fix the
     input bg (rgba(34,211,238,0.08)) almost matched the surrounding
     editor container bg (rgba(34,211,238,0.06)) so the field chrome
     bled into the row. Dark slate fill + stronger cyan border, with
     hover lifting the border and focus inverting to a saturated cyan
     for an unmistakable active cue. Per-leg overrides ride the cyan
     palette (matches the `.has-override` per-leg chip + the OrderCard
     `has-override` styling). */
  .oes-leg-editor-field > input {
    height: 1.3rem;
    padding: 0 0.3rem;
    background: rgba(8, 14, 28, 0.78);
    border: 1px solid rgba(34, 211, 238, 0.65);
    border-radius: 3px;
    color: #f8fafc;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 600;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    box-shadow: inset 0 0 0 1px rgba(34, 211, 238, 0.08);
    transition: border-color 0.12s, background 0.12s;
    width: 3.2rem;
    text-align: right;
  }
  .oes-leg-editor-field > input:hover {
    border-color: rgba(34, 211, 238, 0.90);
  }
  .oes-leg-editor-field > input:focus {
    outline: none;
    border-color: var(--algo-cyan, var(--c-info));
    background: rgba(12, 24, 40, 0.92);
    box-shadow: inset 0 0 0 1px rgba(34, 211, 238, 0.45);
  }
  .oes-leg-editor-field > input::placeholder {
    color: rgba(34, 211, 238, 0.70);
    font-style: italic;
  }
  .oes-leg-editor-clear,
  .oes-leg-editor-close {
    padding: 0.1rem 0.4rem;
    background: transparent;
    border: 1px solid rgba(126, 151, 184, 0.35);
    border-radius: 3px;
    color: rgba(200, 216, 240, 0.75);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    cursor: pointer;
  }
  .oes-leg-editor-clear:hover:not(:disabled),
  .oes-leg-editor-close:hover {
    background: rgba(126, 151, 184, 0.15);
    color: #c8d8f0;
  }
  .oes-leg-editor-close { margin-left: auto; }
  .oes-basket-pill.is-disabled { opacity: 0.55; }
  /* Focused pill — the basket leg the on-fill preview is locked to.
     Slate-blue outer ring (matches the .oes-tpl-preview-leg-badge
     palette) so the operator can see at a glance "this pill is
     driving the chip". The whole pill is clickable to set focus;
     hovering shows the affordance via cursor: pointer + a faint
     ring. The badge in the preview chip displays the focused leg's
     index, so eye travels from chip → badge → pill in one motion. */
  .oes-basket-pill { cursor: pointer; transition: box-shadow 0.12s; }
  .oes-basket-pill:hover:not(.is-focused) {
    box-shadow: inset 0 0 0 1px rgba(165, 180, 252, 0.32);
  }
  .oes-basket-pill.is-focused {
    box-shadow: 0 0 0 2px rgba(165, 180, 252, 0.65),
                inset 0 0 0 1px rgba(165, 180, 252, 0.45);
  }
  .oes-basket-pill-limit-wrap {
    display: inline-flex;
    align-items: center;
    gap: 0.05rem;
    margin-left: 0.3rem;
  }
  .oes-basket-pill-limit-prefix {
    font-size: var(--fs-md);
    color: rgba(200,216,240,0.6);
    line-height: 1;
  }
  .oes-basket-pill-limit {
    width: 5em;
    background: rgba(15,25,45,0.7);
    border: 1px solid rgba(125,211,252,0.3);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: 'Roboto Mono', monospace;
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-lg);
    padding: 0.1rem 0.25rem;
    text-align: right;
    -moz-appearance: textfield;
    appearance: textfield;
  }
  .oes-basket-pill-limit::-webkit-inner-spin-button,
  .oes-basket-pill-limit::-webkit-outer-spin-button { -webkit-appearance: none; appearance: none; }
  .oes-basket-pill-limit:focus {
    outline: none;
    border-color: rgba(125,211,252,0.7);
  }
  .oes-basket-pill-limit-warn {
    border-color: rgba(248,113,113,0.7);
    color: var(--c-short);
  }
  /* .oes-basket-clear / .oes-basket-submit retired with the
     duplicate-buttons drop (656be671); the common footer's
     .oes-common-clear / .oes-common-submit own those affordances
     now. Audit defect #12. */

  /* Body — the tab content area. Flex column so child panels (chain
     grid in particular) can `flex: 1` to fill the full available
     height instead of clamping to their content.
     Operator: "chain specific data area or order ticket specific
     data area height should be same so that there is no waste of
     space". Each tab panel below uses flex:1 + min-height:0 so they
     consume the full body height regardless of their content. */
  .oes-body {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    /* Shared height anchor for the Ticket-tab depth ladder + the
       Chain-tab strike grid. Both panels read this custom property
       so the modal stays the same vertical size when the operator
       flips tabs — eye stays anchored to one horizontal baseline
       (Sensibull / Streak convention). Mobile drops the lock so the
       depth can collapse to its natural ~5-row height. */
    --chain-depth-h: 22rem;
  }
  @media (max-width: 720px) {
    .oes-body { --chain-depth-h: auto; }
  }
  /* Equal-height tab panels — ticket and chain BOTH absorb the
     full .oes-body height. Whichever tab is hidden via
     style:display:none reclaims its space; the visible one fills
     the body. Tab switch never changes the body height. */
  .oes-ticket-body,
  .oes-body :global(.oct-root) {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  /* Ticket body — OrderTicket renders its OWN overlay + modal shell,
     which conflicts when nested inside oes-modal. We override those
     fixed-position styles so it renders inline. The outer shell
     already provides the backdrop + border + padding. */
  .oes-ticket-body :global(.ot-overlay) {
    position: static !important;
    background: none !important;
    padding: 0 !important;
    display: block !important;
    z-index: auto !important;
  }
  .oes-ticket-body :global(.ot-modal) {
    width: 100% !important;
    max-height: none !important;
    /* Critical: OrderTicket's own .ot-modal has overflow-y: auto so it
       can scroll independently when used as a standalone modal. Inside
       the shell, that creates a double-scroll (outer .oes-body also
       scrolls), which pushes the Submit/Place footer off the visible
       viewport, behind the sticky bottom panel. Force visible so the
       ticket grows to its natural height inside .oes-body's single
       scroll region — Submit is then reachable by scrolling the body. */
    overflow-y: visible !important;
    border: none !important;
    border-radius: 0 !important;
    background: none !important;
    box-shadow: none !important;
    /* Match .oes-common-actions L/R inset so the order ticket form
       (Type · Product · Lots / Limit row · Submit pre-check) aligns
       vertically with the +Basket / BUY/SELL button row beneath it.
       Operator: "order ticket contents left good amount of space
       left and right because of which it is not aligning with the
       container with +basket and buy buttons". The common-actions
       row has 0.4rem L/R; matching here means the ticket row first/
       last child sits flush with the +Basket pill above. */
    padding: 0.35rem 0.4rem !important;
  }
  /* Ticket rows: kill any flex `gap` end-of-row drift by clamping
     margin to 0. The row container is gap-driven; trailing children
     don't carry margin in flex but defensive zeroing keeps the
     left/right edges of the row exactly at .ot-modal's 0.4rem inset. */
  .oes-ticket-body :global(.ot-row) {
    margin-left: 0 !important;
    margin-right: 0 !important;
  }
  /* Hide the OrderTicket's own × close button — the shell has one. */
  .oes-ticket-body :global(.ot-close) {
    display: none !important;
  }
  /* Hide OrderTicket header (symbol line) — the shell title carries it. */
  .oes-ticket-body :global(.ot-header) {
    display: none !important;
  }

  /* ── Orders tab ──────────────────────────────────────────────────── */
  .oes-orders-wrap {
    max-height: 30rem;
    overflow-y: auto;
    padding: 0.5rem 0.75rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .oes-orders-section {
    padding: 0.55rem 0;
  }
  .oes-orders-section + .oes-orders-section {
    border-top: 1px solid rgba(255,255,255,0.07);
  }
  .oes-orders-head {
    color: var(--algo-muted);
    font-size: var(--fs-xs);
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.35rem;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .oes-orders-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.1rem;
    height: 1.1rem;
    padding: 0 0.25rem;
    border-radius: 999px;
    background: rgba(192,132,252,0.2);
    border: 1px solid rgba(192,132,252,0.5);
    color: #c084fc;
    font-size: var(--fs-xs);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .oes-orders-empty {
    color: var(--algo-muted);
    font-style: italic;
    padding: 0.3rem 0;
  }

  /* Event rows inside the Orders tab */
  .oes-events-list { display: flex; flex-direction: column; gap: 0.42rem; }
  /* Stacked row: time on its own subtle line, kind + message below.
     Time block widened beyond grid cells caused the kind / message
     to disappear off-screen on narrow modals — the dual IST|EDT
     format is ~58 chars wide. Stacking is the only layout that
     handles every viewport width without truncation. */
  .oes-event-row {
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
    color: var(--algo-slate);
  }
  .oes-event-time {
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-2xs);
    letter-spacing: 0.02em;
  }
  .oes-event-line {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    flex-wrap: wrap;
  }
  .oes-event-kind {
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: var(--fs-xs);
    flex-shrink: 0;
  }
  .oes-event-kind-placed          { color: #38bdf8; }
  .oes-event-kind-chase_modify    { color: var(--c-action); }
  .oes-event-kind-fill            { color: var(--c-long); }
  .oes-event-kind-unfill          { color: var(--c-short); }
  .oes-event-kind-reject          { color: var(--c-short); }
  .oes-event-kind-preflight_ok    { color: #94a3b8; }
  .oes-event-kind-preflight_block { color: var(--c-short); }
  .oes-event-kind-cancel          { color: #94a3b8; }
  .oes-event-kind-postback        { color: #c084fc; }
  /* Agent-sourced event kinds — violet/pink palette so "rule fired"
     is instantly distinguishable from "manual order" events.         */
  .oes-event-kind-agent_fire          { color: #e879f9; }
  .oes-event-kind-agent_match         { color: #d946ef; }
  .oes-event-kind-agent_action_success { color: #a855f7; }
  .oes-event-kind-agent_action_error  { color: #f472b6; }
  .oes-event-kind-agent_skipped       { color: #94a3b8; }
  .oes-event-kind-agent_paused        { color: var(--algo-muted); }
  .oes-event-msg { color: var(--algo-slate); }

  /* Order cards */
  .oes-order-card {
    background: rgba(15,23,42,0.6);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 3px;
    padding: 0.35rem 0.55rem;
    margin-bottom: 0.3rem;
  }
  .oes-order-card-done {
    opacity: 0.75;
  }
  .oes-card-meta {
    margin-top: 0.2rem;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
  }
  .oes-card-sym  { color: var(--algo-slate); font-weight: 800; }
  .oes-card-qty  { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .oes-card-px   { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .oes-card-chase {
    font-size: var(--fs-xs);
    color: var(--c-action);
    border: 1px solid rgba(251,191,36,0.4);
    padding: 0 0.3rem;
    border-radius: 2px;
  }

  /* Status pills */
  .oes-status {
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-status-open,
  .oes-status-pending,
  .oes-status-trigger-pending,
  .oes-status-validation-pending  { background: rgba(251,191,36,0.15); color: var(--c-action); border: 1px solid rgba(251,191,36,0.4); }
  .oes-status-complete,
  .oes-status-filled              { background: rgba(74,222,128,0.12);  color: var(--c-long); border: 1px solid rgba(74,222,128,0.4); }
  .oes-status-unfilled,
  .oes-status-rejected            { background: rgba(248,113,113,0.12);  color: var(--c-short); border: 1px solid rgba(248,113,113,0.4); }
  .oes-status-cancelled           { background: rgba(148,163,184,0.1); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }
  /* LOCAL chip — marks algo_order rows that never reached Kite (preflight blocks). */
  .oes-local-chip {
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
    background: rgba(251,191,36,0.12);
    color: var(--c-action);
    border: 1px solid rgba(251,191,36,0.35);
  }

  /* Side pills */
  .oes-side {
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-side-buy  { background: rgba(74,222,128,0.12); color: var(--c-long); border: 1px solid rgba(74,222,128,0.35); }
  .oes-side-sell { background: rgba(248,113,113,0.12); color: var(--c-short); border: 1px solid rgba(248,113,113,0.35); }

  /* ── Common action footer (modal-mode only) ─────────────────────── */
  /* Two-row stack: buttons on top, margin row below. */
  .oes-common-actions {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 0.3rem;
    padding: 0.5rem 0.4rem;
    background: rgba(15, 23, 42, 0.55);
    border-top: 1px solid rgba(251, 191, 36, 0.18);
    flex-shrink: 0;
  }
  .oes-common-row {
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }
  .oes-common-spacer { flex: 1 1 0; }
  /* Operator naming convention: the left-slot chip is the "margin
     chip" (regardless of whether it's showing margin / cash / notice /
     sticky / cold prompt). All four variants share these layout rules:
       · flex-grow to fill available width (button widths stay fixed)
       · height pinned to 1.7rem so the chip and every action button
         in the row read as a single horizontal stripe
     Audit fix history — selector previously used `> *:first-child`
     which accidentally matched the basket icon on an empty info slot
     and let it expand to ~300px, intercepting clicks on Submit +
     BUY/SELL. Named-class selector below targets ONLY the info-slot
     elements so the action cluster keeps its natural sizing. */
  .oes-common-row > .oes-sticky-result,
  .oes-common-row > .oes-notice,
  .oes-common-row > .oes-cold-prompt,
  .oes-common-row > .oes-margin-pill {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 1.7rem;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
  }
  /* Cold-start prompt — same chip styling as the margin pill so both
     tabs read consistently during cold start. Operator: "the message
     should be displayed like a chip both the tabs." Faint slate fill
     + subtle border so it reads as a hint, not a real notice. The
     same slot later carries the margin/cash chip the moment the
     operator picks a side (Ticket) or stages a basket leg (Chain). */
  .oes-cold-prompt {
    display: inline-flex;
    align-items: center;
    height: 1.7rem;
    padding: 0 0.7rem;
    border-radius: 3px;
    border: 1px solid rgba(180, 200, 230, 0.22);
    background: rgba(15, 25, 45, 0.45);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: rgba(180, 200, 230, 0.65);
    font-style: italic;
    letter-spacing: 0.02em;
    box-sizing: border-box;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* Basket icon — operator: "make all the buttons including basket
     icon button same height as chip." 1.7rem square so it matches
     Submit, Side, and the margin chip exactly. box-sizing ensures
     border doesn't shift effective height. */
  .oes-common-basket-toggle-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.7rem;
    height: 1.7rem;
    padding: 0;
    cursor: pointer;
    user-select: none;
    border: 1px solid rgba(125, 211, 252, 0.40);
    border-radius: 3px;
    background: transparent;
    color: rgba(200, 216, 240, 0.55);
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .oes-common-basket-toggle-icon:hover { color: #7dd3fc; background: rgba(125, 211, 252, 0.10); }
  /* Operator: "active basket icon should have a different background
     color." Active state now uses a stronger amber accent so it pops
     against the resting/hover sky tones — distinct from the muted
     sky-blue inactive treatment. */
  .oes-common-basket-toggle-icon.on {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.20);
    border-color: rgba(251, 191, 36, 0.65);
  }
  /* Static (always-on) variant — used on Chain where basket is the
     only mode. Cursor stays default so the icon reads as a status
     badge, not an actionable toggle. */
  .oes-common-basket-toggle-icon.is-static {
    cursor: default;
  }
  .oes-common-basket-toggle-icon.is-static:hover {
    background: rgba(251, 191, 36, 0.20);
    border-color: rgba(251, 191, 36, 0.65);
  }
  .sr-only {
    position: absolute;
    width: 1px; height: 1px;
    margin: -1px; padding: 0;
    overflow: hidden;
    clip: rect(0,0,0,0);
    border: 0;
  }
  /* Single side toggle — operator: "buy and sell should be one single
     button. id button has more show in two rows in the button without
     changing button height." Cycles cold → BUY → SELL → BUY on click.
     In a symbol-row context the verb (ADD/CLOSE) stacks above the
     derived broker side (BUY/SELL) inside the same 1.7rem button. */
  .oes-footer-side-btn-single {
    height: 1.7rem;
    min-width: 5.5rem;
    padding: 0 0.7rem;
    border-radius: 3px;
    border: 1px solid;
    cursor: pointer;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.04em;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    white-space: nowrap;
    background: transparent;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    line-height: 1;
  }
  /* Two-line stacking when in symbol-row context. Same total height
     (1.7rem) — labels just shrink to ~0.5rem so two fit. */
  .oes-footer-side-btn-single.is-stacked {
    flex-direction: column;
    line-height: 1.05;
    padding: 0 0.5rem;
  }
  .oes-footer-side-btn-single.is-stacked .oes-side-line {
    display: block;
    text-align: center;
  }
  .oes-footer-side-btn-single.is-stacked .oes-side-line1 {
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.08em;
    opacity: 0.85;
  }
  .oes-footer-side-btn-single.is-stacked .oes-side-line2 {
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.04em;
  }
  .oes-footer-side-btn-single.on-none {
    color: rgba(200, 216, 240, 0.55);
    border-color: rgba(200, 216, 240, 0.32);
    border-style: dashed;
  }
  .oes-footer-side-btn-single.on-none:hover {
    color: #cbd5e1;
    background: rgba(255,255,255,0.04);
  }
  .oes-footer-side-btn-single.on-buy {
    color: var(--c-long);
    background: rgba(74, 222, 128, 0.18);
    border-color: rgba(74, 222, 128, 0.70);
  }
  .oes-footer-side-btn-single.on-sell {
    color: var(--c-short);
    background: rgba(248, 113, 113, 0.18);
    border-color: rgba(248, 113, 113, 0.70);
  }

  /* Shared mode + chase toolkit — sits ABOVE the margin/action row
     so both Chain and Ticket tabs read from the same controls.
     Compact: monospace, 0.62rem, tight gaps. Pills + chase glyphs
     mirror the OrderTicket styling so flipping between the two
     modal modes (modal vs standalone OrderTicket) the operator sees
     the same affordance shape. */
  /* .oes-common-mode-chip removed — the row that used it was deleted.
     .oes-common-chase-toggle removed — replaced by {#if _chaseEnabled}. */
  .oes-common-chase-label {
    color: rgba(200,216,240,0.55);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
  }
  .oes-common-chase-label.on { color: var(--c-action); }

  /* Margin strip — sits BELOW the action buttons. MARGIN · Avail ·
     After · (Short) cells in a horizontal row. After is colour-coded
     by remaining-margin band (mirrors the OrderTicket's
     ot-margin-row-{err,warn,sub} convention). */
  /* Inline notice — error / warning / info banner that sits in the
     action row's left slot. Replaces the cash/margin chip when active
     so the operator sees the blocker BEFORE they click submit. Tinted
     by level (err = red, warn = amber, info = sky) — same palette as
     the .oes-margin-pill variants for visual consistency. */
  .oes-notice {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.6rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    border: 1px solid transparent;
    white-space: nowrap;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .oes-notice-err  { background: rgba(248,113,113,0.18); border-color: rgba(248,113,113,0.55); color: var(--c-short); }
  .oes-notice-warn { background: rgba(251,191,36,0.16); border-color: rgba(251,191,36,0.50); color: var(--c-action); }
  .oes-notice-info { background: rgba(56,189,248,0.16); border-color: rgba(56,189,248,0.50); color: #38bdf8; }

  /* Sticky result line — same shape as notice; replaces the
     transient basketResultMsg that vanished alongside the basket
     bar after a successful chase placement. Lives in the
     common-actions LEFT slot for ~3 s after submit lands. */
  .oes-sticky-result {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.6rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    border: 1px solid transparent;
    white-space: nowrap;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .oes-sticky-result-ok {
    background: rgba(74,222,128,0.18);
    border-color: rgba(74,222,128,0.55);
    color: var(--c-long);
  }
  /* Audit fix — basket partial-failure warn level. Amber palette so
     the operator sees "some succeeded, some failed" as distinct from
     full-success (green) and full-failure (red). */
  .oes-sticky-result-warn {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: var(--c-action);
  }
  .oes-sticky-result-err {
    background: rgba(248,113,113,0.18);
    border-color: rgba(248,113,113,0.55);
    color: var(--c-short);
  }

  /* Funds summary line above the action row — Avail margin · Cash ·
     Used (or per-account label when the operator picks a specific
     broker). Matches the OrderTicket's .ot-funds palette so the visual
     family stays consistent: muted slate keys, light value text, amber
     "TOTAL" prefix. Negative avail-margin flips the row red. */
  .oes-funds-line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.4rem 0.55rem;
    padding: 0.25rem 0.35rem 0.35rem;
    font-family: monospace;
    font-size: var(--fs-sm);
    color: var(--algo-slate);
  }
  .oes-funds-line-low { color: var(--c-short); }
  .oes-funds-k {
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: var(--algo-muted);
  }
  .oes-funds-v {
    color: #e2e8f0;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .oes-funds-sep { opacity: 0.5; }

  /* Margin pill — single chip showing "Req ₹X · Avail ₹Y" tinted by
     availability. ok = green, warn = amber, err = red, neutral = slate
     (loading / unknown). Anchored to the LEFT of the action button
     cluster so the operator scans amount → buttons → submit. */
  .oes-margin-pill {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35rem;
    padding: 0.3rem 0.6rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 600;
    border: 1px solid transparent;
    background: rgba(126, 151, 184, 0.10);
    color: var(--algo-slate);
    white-space: nowrap;
  }
  /* Stacked variant — two rows (Req / Avail) one above the other.
     Sized to match the action buttons' new 2-row height. */
  .oes-margin-pill-stack {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.1rem;
    padding: 0.35rem 0.6rem;
    line-height: 1.15;
  }
  .oes-margin-pill-row {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    width: 100%;
    justify-content: space-between;
  }
  .oes-margin-pill-key {
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.05em;
    opacity: 0.75;
  }
  .oes-margin-pill-val {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }
  /* +wing tag — sits inline after the margin number when the
     Required figure reflects parent + auto-attached wing. Purple
     palette matches the wing chip in OrderTicket's template preview. */
  .oes-margin-pill-paired {
    margin-left: 0.3rem;
    padding: 0 0.3rem;
    border-radius: 2px;
    background: rgba(192, 132, 252, 0.18);
    color: #c084fc;
    font-size: var(--fs-xs);
    font-weight: 600;
    font-family: monospace;
    letter-spacing: 0.04em;
  }
  .oes-margin-pill-sep {
    opacity: 0.5;
  }
  .oes-margin-pill-ok {
    background: rgba(74, 222, 128, 0.14);
    border-color: rgba(74, 222, 128, 0.45);
    color: #86efac;
  }
  .oes-margin-pill-warn {
    background: rgba(251, 191, 36, 0.16);
    border-color: rgba(251, 191, 36, 0.50);
    color: var(--c-action);
  }
  .oes-margin-pill-err {
    background: rgba(248, 113, 113, 0.18);
    border-color: var(--algo-red-border);
    color: var(--c-short);
  }
  .oes-margin-pill-neutral {
    background: rgba(126, 151, 184, 0.12);
    border-color: rgba(126, 151, 184, 0.35);
    color: var(--algo-slate);
  }
  .oes-common-basket,
  .oes-common-side,
  .oes-common-submit {
    /* Operator: "all buttons should be of same height." Submit + side
       single button + basket icon all 1.7rem tall. */
    height: 1.7rem;
    padding: 0 0.85rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    background: transparent;
    border: 1px solid rgba(125, 211, 252, 0.45);
    color: #7dd3fc;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  /* Compact Submit label when basket is active — short "Submit (N)"
     text + slightly smaller font + tighter padding so the cluster
     fits even on narrow viewports. */
  .oes-common-submit-narrow {
    font-size: var(--fs-sm);
    padding-left: 0.55rem;
    padding-right: 0.55rem;
    letter-spacing: 0.02em;
  }
  /* Clear-basket pill nested in the chase row — smaller, quieter
     red so it doesn't compete with the chase pills next to it. */
  .oes-common-clear-inline {
    padding: 0.2rem 0.5rem;
    font-size: var(--fs-xs);
    border-radius: 3px;
    border: 1px solid rgba(248, 113, 113, 0.40);
    background: transparent;
    color: rgba(248, 113, 113, 0.85);
    cursor: pointer;
    font-family: monospace;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .oes-common-clear-inline:hover { background: var(--c-short-10); }
  .oes-common-basket:hover:not(.is-disabled) { background: var(--algo-sky-bg); }
  /* Grayed-out +Basket on Chain tab — affordance stays visible so
     the operator knows the basket flow exists; clicking it would
     do nothing on Chain (per-row +CE / +PE is the path there). */
  .oes-common-basket.is-disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .oes-common-side-buy  { border-color: var(--algo-green-border); color: var(--c-long); }
  .oes-common-side-sell { border-color: var(--algo-red-border); color: var(--c-short); }
  .oes-common-side-buy:hover  { background: rgba(74, 222, 128, 0.12); }
  .oes-common-side-sell:hover { background: rgba(248, 113, 113, 0.12); }
  .oes-common-submit-buy {
    background: rgba(74, 222, 128, 0.18);
    border-color: rgba(74, 222, 128, 0.65);
    color: var(--c-long);
  }
  .oes-common-submit-sell {
    background: rgba(248, 113, 113, 0.18);
    border-color: rgba(248, 113, 113, 0.65);
    color: var(--c-short);
  }
  .oes-common-submit-buy:hover  { background: rgba(74, 222, 128, 0.28); }
  .oes-common-submit-sell:hover { background: rgba(248, 113, 113, 0.28); }
  /* Basket submit — cyan to signal "multiple legs at once", distinct
     from the directional buy/sell palette. */
  .oes-common-submit-basket {
    background: rgba(34, 211, 238, 0.18);
    border-color: rgba(34, 211, 238, 0.65);
    color: var(--c-info);
  }
  .oes-common-submit-basket:hover { background: rgba(34, 211, 238, 0.28); }
  /* Clear-basket — neutral outline. */
  .oes-common-clear {
    padding: 0.35rem 0.75rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    background: transparent;
    border: 1px solid rgba(126, 151, 184, 0.45);
    color: var(--algo-muted);
    transition: background 0.12s, color 0.12s;
  }
  .oes-common-clear:hover:not(:disabled) {
    background: rgba(126, 151, 184, 0.12);
    color: var(--algo-slate);
  }
  .oes-common-clear:disabled { opacity: 0.45; cursor: progress; }

  /* ── Bottom panel (Log / Orders) ──────────────────────────────────── */
  /* Sits AFTER the common action footer ("move the order placement
     above the activity"). Mirrors ActivityLogModal's .alm-body —
     flex column, fixed slot inside the modal's overall height, the
     LogPanel inside expands via heightClass="flex-1 min-h-0". */
  /* Chase slot — sits between the order ticket section and the
     activity panel, mirroring the /orders page layout (Entry → Chases →
     Activity). ChaseCard self-hides when idle, so the slot collapses
     visually with zero in-flight orders. */
  .oes-chase-slot {
    padding: 0.3rem 0.6rem 0;
    flex: 0 0 auto;
  }

  /* Bottom activity panel — operator request: shrink so the order
     ticket (top of the modal) gets the screen real estate needed to
     surface the market-depth ladder. 22rem/18rem was eating ~46 % of
     a 760px modal; 13rem/11rem leaves the ticket form + depth visible
     while the LogPanel inside still scrolls comfortably. The active
     tab inside (Orders / Agents / Terminal / Ticks / System / News)
     manages its own scroll, so reducing the slot height never breaks
     content — it just keeps the strip more compact. */
  .oes-bottom-panel {
    flex: 0 0 13rem;
    min-height: 11rem;
    display: flex;
    flex-direction: column;
    padding: 0.3rem 0.6rem 0.5rem;
    overflow: hidden;
    border-top: 1px solid rgba(168, 85, 247, 0.22);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  /* Bottom body fills the bottom-panel's fixed slot; LogPanel inside
     manages its own internal scroll for the active tab. */
  .oes-bottom-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    /* Padding tightened (was 0.4 / 0.6 / 0.6) so the panel's already
       reduced height isn't eaten by inner whitespace. */
    padding: 0.2rem 0.5rem 0.35rem;
    overflow: hidden;
  }
  :global(.oes-bottom-scroll) {
    flex: 1 1 0;
    min-height: 0;
    padding: 0.1rem 0.25rem;
  }
</style>
