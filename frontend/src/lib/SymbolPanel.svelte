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

  import { onMount, onDestroy, untrack } from 'svelte';
  import { get as _storeGet } from 'svelte/store';
  import { portal } from '$lib/portal';
  import { ORDER_TABS } from '$lib/order/tabs.js';
  import { SYM_TYPE_OPTS } from '$lib/data/symbolTypes';
  import { placeTicketOrder, placeBasket, fetchBasketMargin, fetchLiveStatus, fetchOrders, fetchAlgoOrdersRecent } from '$lib/api';
  import ChartModal from '$lib/ChartModal.svelte';
  import { logTime, executionMode } from '$lib/stores';
  import { priceFmt, aggFmt as aggFmtMargin } from '$lib/format';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';
  import ChaseCard       from '$lib/order/ChaseCard.svelte';
  import LogPanel        from '$lib/LogPanel.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import Select            from '$lib/Select.svelte';
  // Order-template catalog — shared with OrderTicket. SymbolPanel's
  // basket bar exposes the "On fill" picker; the chosen template is
  // attached per leg in submitBasket. Single source of truth so
  // CRUD on /automation/templates propagates here without a refresh.
  import { loadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';
  // resolveUnderlying / findNearestFuture / resolveAnchorToTradeable
  // dynamically imported inside effects only — no static imports needed.
  import { loadAccounts, getDefaultAccount, recentSymbolStore, setRecentSymbol, setRecentAccount } from '$lib/data/accounts';
  import { isMarketOpen, isNseOpen, isMcxOpen } from '$lib/marketHours';
  import AlgoTabs from '$lib/AlgoTabs.svelte';

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
   *   showChartButton?: boolean,
   *   showCommonActions?: boolean,
   * }} */
  let {
    defaultTab     = /** @type {'chain'|'ticket'} */ ('chain'),
    symbol         = '',
    exchange       = '',
    instrument     = /** @type {{kind?:string,exchange?:string}} */ ({}),
    side           = /** @type {'BUY'|'SELL'} */ ('BUY'),
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
  } = $props();

  // Local mutable copy of the symbol prop — operator can edit it from
  // the top search input so every tab (Ticket / Chain /
  // Command) re-renders against the new symbol. Synced from the prop
  // via $effect so an external pick (chain row + CE click, dashboard
  // row click, MarketPulse symbol pick) still updates the shell.
  let _localSymbol = $state(String(symbol || '').toUpperCase());
  // Sync FROM prop only. Reading _localSymbol via untrack() so the
  // operator's own picks (which set _localSymbol from inside the modal)
  // don't re-trigger this effect — without untrack the comparison
  // sees the new local value, decides it differs from the prop, and
  // slams the local back to the prop, undoing the operator's typing.
  $effect(() => {
    const next = String(symbol || '').toUpperCase();
    if (next && next !== untrack(() => _localSymbol)) _localSymbol = next;
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
    _localSymbol = String(inst?.s ?? inst?.sym ?? inst?.tradingsymbol ?? _symbolQuery).toUpperCase();
    // Capture exchange from the instrument row (field `e` in the search
    // result shape) so _isEquityExch can correctly gate the Chain tab
    // even when no exchange prop was supplied by the caller.
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
  function _setActiveTab(/** @type {'ticket'|'chain'} */ id) {
    _activeTabInternal = id;
    activeTab = id;
  }

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


  // ── Bottom-panel tab state ───────────────────────────────────────────
  // Default to Order Book — same as ActivityLogModal so the operator
  // sees the same first surface whether the activity appears as a card
  // tab inside SymbolPanel or as a modal from the page-header Log icon.
  let _bottomTab = $state(/** @type {'orders'|'log'} */ ('orders'));

  // ── Basket state (shared across all tabs) ───────────────────────────
  // When basketMode is active (Chain tab is selected), submissions from
  // Command and Ticket tabs accumulate here instead of firing immediately.
  const basketMode = $derived(_activeTab === 'chain');
  /** @type {any[]} */
  let basketLegs   = $state([]);
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
  function clearBasket() { basketLegs = []; basketResultMsg = ''; _basketMarginRows = []; }

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
          byAcct.get(a)?.push({
            tradingsymbol: leg.sym,
            exchange: leg.exchange || 'NFO',
            transaction_type: leg.side,
            quantity: (leg.lots || 1) * (leg.lotSize || 1),
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

  // ── Shared account state ─────────────────────────────────────────
  // Single source of truth for the routable account across all three
  // tabs (command / ticket / chain). Earlier each tab maintained its
  // own _account so picking it in one tab didn't sync to the others
  // — operators could submit a Ticket-tab order on one account while
  // the Chain-tab basket was staged for another. Lifted here as $state
  // initialised from the `account` prop; each tab receives it as
  // `account` and calls `onAccountChange` when its picker changes.
  let _sharedAccount = $state(account || '');

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
  let _sharedChase    = $state(true);
  let _sharedChaseAgg = $state(/** @type {'low'|'med'|'high'} */ ('low'));
  // Shared template — applied to every leg in the basket on submit.
  // Operator picks once from the bar; submitBasket threads the id
  // into each BasketLeg's `template_id` so the backend runs the
  // `apply_template_to_order` pipeline per leg on fill. Defaults
  // to the 'none' (no-attach) row once the catalog loads so the
  // first basket doesn't surprise the operator with an unexpected
  // GTT. Operator: "template should be applicable to option chain too".
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
  /** @param {string} sd @param {string} sym */
  function _appliesToFor(sd, sym) {
    if (sd === 'SELL' && /\d+(CE|PE)$/i.test(sym || '')) return 'sell_option';
    if (sd === 'SELL') return 'sell_any';
    if (sd === 'BUY')  return 'buy_any';
    return 'both';
  }
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
    const scope = _appliesToFor(_modalSide, _localSymbol);
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
    if (action !== 'open') return;
    if (_templates.length === 0) return;
    const scope = _appliesToFor(_modalSide, _localSymbol);
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
  let _modalAccounts = $state(/** @type {string[]} */ (Array.isArray(accounts) ? accounts : []));
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
  let _modalSide          = $state(/** @type {'BUY'|'SELL'} */ (side));
  // Submit button label adapts to:
  //   currentQty=0  → "Buy" / "Sell" (plain new order)
  //   currentQty>0  → "Add to position" / "Close position"  (long)
  //   currentQty<0  → "Close position" / "Add to position"  (short)
  // When the basket has legs the label switches to "Submit basket (N)"
  // so the same button drives the basket-submit path. Operator
  // request: "buy sell button can be a single button as buy/sell
  // selected within order tab. it should change based on add or close
  // when clicking order on existing symbol."
  const _submitLabel = $derived.by(() => {
    if (basketLegs.length > 0) return `Submit basket (${basketLegs.length})`;
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    const verb = (() => {
      if (cq === 0) return _modalSide === 'BUY' ? 'Buy' : 'Sell';
      if (cq > 0)   return _modalSide === 'BUY' ? 'Add to position' : 'Close position';
      /* cq < 0 */  return _modalSide === 'BUY' ? 'Close position' : 'Add to position';
    })();
    return verb;
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
  function _modalFireBasket() { if (_activeTab === 'ticket') _modalTriggerBasket++; }
  function _modalFireSubmit() { if (_activeTab === 'ticket') _modalTriggerSubmit++; }
  function _modalFlipSide()   { _modalSide = _modalSide === 'BUY' ? 'SELL' : 'BUY'; }

  // Margin preview lifted out of OrderTicket so the operator sees the
  // same MARGIN / Avail / After / Short row regardless of which tab is
  // active (matches the operator ask: "margin line should be common
  // for all the tabs in modal"). OrderTicket fires onMarginUpdate
  // every time its computed preview changes; we cache it here and
  // render in the common action footer.
  let _modalMargin        = $state(/** @type {any} */ (null));
  let _modalMarginLoading = $state(false);
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
  const _chaseEnabled = $derived(
    _activeTab === 'chain'
      || _isDerivative
      || !_chipMeta?.orderType
      || _chipMeta?.orderType === 'LIMIT'
      || _chipMeta?.orderType === 'SL'
  );
  // Auto-uncheck chase when the active order type can't use it
  // (MARKET / SL-M). Operator: "for market and sl m, the chase
  // should be deselected while graying it out". The toggle is
  // also visually `disabled` via _chaseEnabled — flipping the
  // backing state to false ensures the order is actually placed
  // without chase if the operator submits straight from the form.
  $effect(() => {
    if (!_chaseEnabled && _sharedChase) _sharedChase = false;
  });
  // +Basket is the Ticket-tab add-to-basket affordance. Chain has
  // per-row +CE / +PE buttons; on Chain the +Basket button stays
  // visible but grayed so the operator sees the affordance is part
  // of the panel — operator: "what happened to +basket button which
  // is supposed to be present in order modal and order page".
  const _basketEnabled = $derived(_activeTab === 'ticket');

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
  async function submitBasket() {
    if (basketSubmitting || !basketLegs.length) return;
    basketSubmitting = true; basketResultMsg = '';

    // Validate that every leg has a limit price before going to the backend.
    const missingQuote = basketLegs.find(leg => !(Number(leg.limit) > 0));
    if (missingQuote) {
      basketResultMsg = `${missingQuote.side} ${missingQuote.sym}: no quote yet — re-open chain so bid/ask loads.`;
      basketSubmitting = false;
      return;
    }

    // Resolve effective mode from the global store (Phase B: no per-ticket override).
    let basketMode2 = $executionMode || 'paper';
    if (basketMode2 === 'paper' || basketMode2 === 'live') {
      try {
        const live = await fetchLiveStatus();
        if (live && (live.branch !== 'main' || live.paper_trading_mode === true)) {
          basketMode2 = 'paper';
        }
      } catch { /* keep operator's pick */ }
    }

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
        quantity:         (leg.lots || 1) * (leg.lotSize || 1),
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
      })).map(/** @param {any} l */ (l) => {
        const _hasLegTpl = (l.template_id !== _sharedTemplateId);
        return {
          ...l,
          tp_pct_override:             l.tp_pct_override ?? (_hasLegTpl
            ? null
            : (_sharedTpOverride !== '' ? Number(_sharedTpOverride) : null)),
          sl_pct_override:             l.sl_pct_override ?? (_hasLegTpl
            ? null
            : (_sharedSlOverride !== '' ? Number(_sharedSlOverride) : null)),
          wing_premium_pct_override:   l.wing_premium_pct_override ?? (_hasLegTpl
            ? null
            : (_sharedWingPremPctOverride !== '' ? Number(_sharedWingPremPctOverride) : null)),
          wing_strike_offset_override: l.wing_strike_offset_override ?? (_hasLegTpl
            ? null
            : (_sharedWingStrikeOffsetOverride !== '' ? Number(_sharedWingStrikeOffsetOverride) : null)),
        };
      }),
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
      _stickyResultTimer = setTimeout(() => { _stickyResultMsg = ''; _stickyResultLevel = ''; }, 8000);
    } else {
      basketResultMsg = `Failed: ${fails[0]}`;
      _stickyResultMsg = `All ${total} legs rejected — no orders placed`;
      _stickyResultLevel = 'err';
      if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
      _stickyResultTimer = setTimeout(() => { _stickyResultMsg = ''; _stickyResultLevel = ''; }, 8000);
    }
  }

  // ── Command tab pre-fill ─────────────────────────────────────────────
  // Ticket pre-fill state — when the Command tab parses an order command
  // it routes back here to switch to the Ticket tab pre-filled (or adds
  // to basket when basketMode is active).
  /** @type {any} */
  let _cmdOrderProps = $state(null);

  function handleParsedOrder(/** @type {any} */ props) {
    _cmdOrderProps = props;
    _setActiveTab('ticket');
  }

  /** Called by CommandLineTab when basketMode is on. */
  function handleCmdAddToBasket(/** @type {any} */ leg) {
    addToBasket(leg);
  }

  // ── Orders tab state ─────────────────────────────────────────────────
  let _orders       = $state(/** @type {any[]} */ ([]));
  let _algoRejected = $state(/** @type {any[]} */ ([]));
  /** @type {ReturnType<typeof setInterval> | undefined} */
  let _ordersPoll;
  // Focus-trap anchor — bound to .oes-modal so Tab cycles stay inside.
  let _modalEl      = $state(/** @type {HTMLElement|null} */ (null));

  // Kite statuses: OPEN / TRIGGER PENDING / VALIDATION PENDING are
  // pending; COMPLETE / REJECTED / CANCELLED / UNFILLED are terminal.
  const PENDING_STATUSES = new Set([
    'OPEN', 'TRIGGER PENDING', 'VALIDATION PENDING', 'PENDING',
  ]);
  const _ordersPending   = $derived(_orders.filter(o => PENDING_STATUSES.has(o.status)));

  // Completed section: Kite terminal orders + LOCAL REJECTED algo_orders.
  // Local rows carry a `_local: true` flag so the template can render
  // a "LOCAL" chip distinguishing "we blocked it" from "broker rejected".
  const _ordersCompleted = $derived.by(() => {
    const kite = /** @type {any[]} */ (_orders).filter(o => !PENDING_STATUSES.has(o.status));
    const local = /** @type {any[]} */ (_algoRejected).map(o => ({ .../** @type {any} */ (o), _local: true }));
    return [...kite, ...local]
      .sort((a, b) => {
        const ta = a.exchange_update_timestamp ?? a.order_timestamp ?? a.filled_at ?? a.created_at ?? '';
        const tb = b.exchange_update_timestamp ?? b.order_timestamp ?? b.filled_at ?? b.created_at ?? '';
        return (tb || '').localeCompare(ta || '');
      })
      .slice(0, 30);
  });

  async function _loadOrdersData() {
    try {
      const [ordRes, algoRejRes] = await Promise.all([
        // Real Kite broker orders — same source the /orders page uses.
        fetchOrders(),
        // Local REJECTED algo_orders that never reached Kite (preflight blocks).
        fetchAlgoOrdersRecent(20, 'live'),
      ]);
      _orders       = (Array.isArray(ordRes) ? ordRes : (ordRes?.rows ?? ordRes ?? []));
      const allAlgo = (Array.isArray(algoRejRes) ? algoRejRes : (algoRejRes?.orders ?? algoRejRes ?? []));
      _algoRejected = allAlgo.filter((/** @type {any} */ o) => (o.status ?? '').toUpperCase() === 'REJECTED');
    } catch (_) { /* silent */ }
  }

  /** Format an ISO UTC timestamp to HH:MM:SS for order-card meta lines. */
  function _fmtEventTime(/** @type {unknown} */ ts) {
    if (!ts || typeof ts !== 'string') return '—';
    const out = logTime(ts.endsWith('Z') ? ts : ts + 'Z');
    return out || '—';
  }

  // Close on Escape + conditional order-data poll.
  // CRIT 2: when hideBottomPanel=true (PageHeaderActions case) the order
  // history section is never rendered — skip both the initial load and the
  // 3-second interval to avoid firing two API calls per tick for the full
  // modal lifetime.
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
    if (!hideBottomPanel) {
      _loadOrdersData();
      _ordersPoll = setInterval(_loadOrdersData, 3000);
    }
    // HIGH 1: prevent background page scroll while the modal is open.
    // Only applies in overlay mode (not inline) — inline renders as a
    // flat page element so scroll should remain enabled.
    const wasInline = inline;
    if (!wasInline) {
      document.body.style.overflow = 'hidden';
    }
    return () => {
      window.removeEventListener('keydown', onKey);
      if (_ordersPoll) { clearInterval(_ordersPoll); _ordersPoll = undefined; }
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
    ...(t.id === 'chain'   ? { dot: '#4ade80', activeTxt: '#4ade80', activeBorder: '#4ade80', activeBg: 'rgba(74,222,128,0.14)'  } :
        t.id === 'ticket'  ? { dot: '#fbbf24', activeTxt: '#fbbf24', activeBorder: '#fbbf24', activeBg: 'rgba(251,191,36,0.14)'  } :
                             { dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', activeBg: 'rgba(125,211,252,0.14)' }),
  }));

  // Effective OrderTicket props — merge _cmdOrderProps (from Command tab
  // parse) on top of the shell's own props, so a typed command wins.
  const _ticketProps = $derived.by(() => {
    if (_cmdOrderProps) return _cmdOrderProps;
    return {
      symbol, exchange, side, action, qty, product, orderType, variety,
      price, trigger, lotSize, accounts, account, orderId,
      currentQty, onAddToBasket,
    };
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
       role="document"
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
         passed from context). Matches the Charts / Activity modal
         header shape (icon + name + close X — nothing else). -->
    <div class="oes-header">
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
        Orders
      </span>
      {#if _wlToast}
        <span class="oes-wl-toast" class:ok={_wlToast.ok} class:err={!_wlToast.ok}>
          {_wlToast.msg}
        </span>
      {/if}
      {#if !inline}
        <button type="button" class="oes-close" title="Close" aria-label="Close"
                onclick={(e) => { e.stopPropagation(); onClose(); }}>×</button>
      {/if}
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
        {#if _modalAccounts.length > 1}
          <div class="oes-account-pick">
            <Select
              options={_modalAccounts.map(a => ({ value: a, label: a }))}
              value={_sharedAccount}
              onValueChange={(v) => _onAccountChange(String(v))}
              placeholder="Account"
              ariaLabel="Trading account" />
          </div>
        {:else if _modalAccounts.length === 1 && _sharedAccount}
          <span class="oes-account-single" title="Single broker account">{_sharedAccount}</span>
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
      </div>

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
          color: t.id === 'chain' ? 'green' : t.id === 'ticket' ? 'amber' : 'sky',
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
            side={_ticketProps.side ?? (showCommonActions ? _modalSide : side)}
            action={_ticketProps.action ?? action}
            qty={_ticketProps.qty ?? qty}
            product={_ticketProps.product ?? product}
            orderType={_ticketProps.orderType ?? orderType}
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
            bind:chaseAgg={_sharedChaseAgg}
            bind:templateId={_sharedTemplateId}
            bind:tpOverride={_sharedTpOverride}
            bind:slOverride={_sharedSlOverride}
            bind:wingStrikeOffsetOverride={_sharedWingStrikeOffsetOverride}
            bind:wingPremPctOverride={_sharedWingPremPctOverride}
            modeChaseHidden={true}
            onMarginUpdate={showCommonActions ? _onMarginUpdate : null}
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
    {#if _templates.length > 0 && action === 'open'}
      <div class="oes-basket-tpl-row oes-basket-tpl-row-shell"
           title={!_shellUsingNone && _selectedTemplate
             ? `${_selectedTemplate.name || _selectedTemplate.slug}${_selectedTemplate.description ? ' — ' + _selectedTemplate.description : ''}`
             : 'Default attaches the saved template that matches the current side + symbol type. None opts out of any GTT attach.'}>
        <span class="oes-basket-tpl-pick">
          <span class="oes-basket-tpl-label">Template</span>
          <span class="oes-tpl-toggle" role="group" aria-label="Template attach">
            <button type="button"
                    class={'oes-tpl-btn' + (!_shellUsingNone ? ' on' : '')}
                    disabled={!_sideAwareDefault}
                    title={_sideAwareDefault
                      ? `Attach ${_sideAwareDefault.name} on fill`
                      : 'No side-default template configured for this scope'}
                    onclick={() => {
                      if (_sideAwareDefault) _sharedTemplateId = _sideAwareDefault.id;
                    }}>
              Default
            </button>
            <button type="button"
                    class={'oes-tpl-btn' + (_shellUsingNone ? ' on' : '')}
                    title="No template — entry only, no GTT / no wing"
                    onclick={() => {
                      if (_noneTpl) _sharedTemplateId = _noneTpl.id;
                    }}>
              None
            </button>
          </span>
          {#if !_shellUsingNone && _selectedTemplate}
            <!-- Active template name + description so the operator sees
                 WHICH default Default resolved to (relevant when there
                 are multiple side-defaults seeded). -->
            <span class="oes-basket-tpl-name" title={_selectedTemplate.description || ''}>
              {_selectedTemplate.name || _selectedTemplate.slug}
            </span>
          {/if}
        </span>
        {#if !_shellUsingNone && _selectedTemplate}
          <div class="oes-basket-tpl-params">
            <label class="oes-basket-tpl-param" title="Take-profit % above (BUY) or below (SELL) the fill price.">
              <span>TP%</span>
              <input type="number" step="0.5"
                placeholder={_selectedTemplate.tp_pct != null ? String(_selectedTemplate.tp_pct) : '—'}
                bind:value={_sharedTpOverride} />
            </label>
            <label class="oes-basket-tpl-param" title="Stop-loss % opposite the TP side.">
              <span>SL%</span>
              <input type="number" step="0.5"
                placeholder={_selectedTemplate.sl_pct != null ? String(_selectedTemplate.sl_pct) : '—'}
                bind:value={_sharedSlOverride} />
            </label>
            {#if _sharedTplShowsWing}
              <label class="oes-basket-tpl-param" title="Protective wing BUY at this many strikes away from the parent.">
                <span>Wing strike+</span>
                <input type="number" step="50"
                  placeholder={_selectedTemplate.wing_strike_offset != null ? String(_selectedTemplate.wing_strike_offset) : '—'}
                  bind:value={_sharedWingStrikeOffsetOverride} />
              </label>
              <label class="oes-basket-tpl-param" title="Wing premium target as a % of the parent's premium.">
                <span>Wing prem%</span>
                <input type="number" step="0.5"
                  placeholder={_selectedTemplate.wing_premium_pct != null ? String(_selectedTemplate.wing_premium_pct) : '—'}
                  bind:value={_sharedWingPremPctOverride} />
              </label>
            {/if}
          </div>
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
            <span class="oes-basket-pill oes-basket-pill-{leg.side === 'BUY' ? 'buy' : 'sell'} oes-basket-pill-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                  class:is-disabled={basketSubmitting}
                  role="listitem"
                  title="Click × to remove from basket">
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
              {#if _sharedWingPlanned && leg.side === 'SELL' && /(CE|PE)$/.test(leg.sym)}
                {@const _wingSym = _wingSymbolFor(leg.sym)}
                <span class="oes-basket-pill-wing"
                      title={_wingSym
                        ? `Wing BUY ${_wingSym} will attach on fill (qty matches leg).`
                        : 'Protective wing BUY will be picked from the option chain at fill time (premium-scan).'}>
                  + wing{_wingSym ? ` ${_wingSym}` : ''}
                </span>
              {/if}
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
                  <select disabled={basketSubmitting}
                          value={leg.template_id ?? ''}
                          onchange={(e) => {
                            const raw = /** @type {HTMLSelectElement} */ (e.currentTarget).value;
                            const v = raw === '' ? null : Number(raw);
                            updateLegByKey(leg.key, b => ({ ...b, template_id: v }));
                          }}>
                    <option value="">(shell default)</option>
                    {#if _noneTpl}
                      <option value={_noneTpl.id}>(no template — entry only)</option>
                    {/if}
                    {#each _nonNoneTemplates as t (t.id)}
                      <option value={t.id}>{t.name || t.slug || `#${t.id}`}</option>
                    {/each}
                  </select>
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
        <!-- Shared mode + chase toolkit — operator: "mode, chase,
             margin, common for chase and order ticket". Sits ABOVE
             the margin / submit row so both Chain and Ticket tabs
             read from the same controls. Mode pills now iterate
             `availableModes` so /orders (paper/live/shadow/sim/
             replay) and the modal (paper/live) share the same
             markup. State bound from `_sharedMode / _sharedChase /
             _sharedChaseAgg`; OrderTicket suppresses its own row
             via `modeChaseHidden=true` and reads the lifted values
             through its bindable props. -->
        <div class="oes-common-mode-row">
          <span class="oes-common-mode-label">Mode</span>
          <!-- Phase B: mode is now read-only — set from the navbar mode dropdown.
               Chip reuses the LogPanel mode-pill-* palette (sky=paper, red=live,
               orange=shadow, amber=sim, green=replay). "change" link navigates
               to /admin/execution so operator can flip modes without hunting. -->
          <span class="oes-common-mode-chip mode-pill-{$executionMode ?? 'paper'}"
                title="Current execution mode (set from the navbar dropdown)">
            {($executionMode ?? 'paper').toUpperCase()}
          </span>
          <a href="/admin/execution?mode={$executionMode ?? 'paper'}"
             class="oes-common-mode-change"
             title="Change mode (opens the execution settings page)">
            change
          </a>
          <!-- Chase is only meaningful for LIMIT and SL orders (those
               carry a limit price the engine can re-quote each tick).
               MARKET and SL-M fill at the book's price — no limit to
               re-quote. Operator: "make chase grayed out when market
               is selected for order type". Render the toggle + agg
               pills unconditionally so the affordance is always
               visible; flip them disabled + tinted when the active
               order type can't use chase. Chain tab always uses
               LIMIT so chase is always enabled there. -->
          <label class="oes-common-chase-toggle"
                 class:is-disabled={!_chaseEnabled}
                 title={!_chaseEnabled
                   ? 'Chase unavailable — MARKET / SL-M orders fill at the book; no limit to re-quote'
                   : _sharedChase
                     ? 'Chase ON — re-quote the limit each tick until filled'
                     : 'Chase OFF — order rests at the initial limit; fills only if the market crosses'}>
            <input type="checkbox" bind:checked={_sharedChase} disabled={!_chaseEnabled} />
            <span class="oes-common-chase-label" class:on={_sharedChase && _chaseEnabled}>CHASE</span>
          </label>
          {#if _sharedChase && _chaseEnabled}
            <div class="oes-common-chase-agg" role="group" aria-label="Chase aggressiveness">
              <button type="button" class="oes-common-chase-agg-pill"
                      class:on={_sharedChaseAgg === 'low'}
                      title="Low — patient. Pegs to your own side; fills only if the market lifts it."
                      onclick={() => _sharedChaseAgg = 'low'}>L</button>
              <button type="button" class="oes-common-chase-agg-pill"
                      class:on={_sharedChaseAgg === 'med'}
                      title="Medium — peg to midpoint of bid+ask."
                      onclick={() => _sharedChaseAgg = 'med'}>M</button>
              <button type="button" class="oes-common-chase-agg-pill"
                      class:on={_sharedChaseAgg === 'high'}
                      title="High — urgent. Crosses the spread to take liquidity on the next tick."
                      onclick={() => _sharedChaseAgg = 'high'}>H</button>
            </div>
          {/if}
          <!-- Clear basket lifted to the mode/chase row per operator
               request — frees space in the submit row so the Submit
               button isn't cramped. -->
          {#if basketLegs.length > 0}
            <span class="oes-common-spacer"></span>
            <button type="button" class="oes-common-clear oes-common-clear-inline"
              title="Clear all basket legs"
              disabled={basketSubmitting}
              onclick={clearBasket}>Clear</button>
          {/if}
        </div>
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
          <span class="oes-common-spacer"></span>
          <!-- +Basket — operator: "what happened to +basket button
               which is supposed to be present in order modal and
               order page". Always rendered so the affordance is
               consistent across tabs. On Chain the per-row +CE / +PE
               buttons are the primary path; the common +Basket
               grays out so the operator sees the control without
               clicking-into-silence. -->
          <button type="button" class="oes-common-basket"
            class:is-disabled={!_basketEnabled}
            disabled={!_basketEnabled}
            title={_basketEnabled
              ? 'Add the current order to the basket'
              : 'Switch to Ticket tab — Chain uses per-row +CE / +PE buttons'}
            onclick={_modalFireBasket}>+ Basket</button>
          <button type="button" class="oes-common-submit"
            class:oes-common-submit-buy={_submitFlavor === 'buy'}
            class:oes-common-submit-sell={_submitFlavor === 'sell'}
            class:oes-common-submit-basket={_submitFlavor === 'basket'}
            class:oes-common-submit-narrow={basketLegs.length > 0}
            title={basketLegs.length > 0
              ? `Submit all ${basketLegs.length} basket legs`
              : (_activeTab === 'chain'
                  ? 'Add legs via +CE / +PE on the chain rows first'
                  : 'Place the order via the active tab')}
            disabled={basketSubmitting
                      || (basketLegs.length === 0 && _activeTab === 'chain')}
            onclick={() => {
              if (basketLegs.length > 0) submitBasket();
              else _modalFireSubmit();
            }}>{basketSubmitting
                ? 'Placing…'
                : (basketLegs.length > 0
                    ? `Submit (${basketLegs.length})`
                    : _submitLabel)}</button>
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
        <LogPanel
          heightClass="flex-1 min-h-0"
          defaultTab="order"
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
    font-family: ui-monospace, monospace;
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
  .oes-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.5rem;
    background: linear-gradient(180deg,
                  rgba(251, 191, 36, 0.18) 0%,
                  rgba(251, 191, 36, 0.06) 100%);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
  }
  /* Plain title text — operator: "remove pill kind of decoration
     for modal header text". Bold uppercase amber glyphs on the
     navy gradient strip; the gradient itself is the prominence. */
  .oes-modal-name {
    font-family: ui-monospace, monospace;
    font-size: 0.72rem;
    color: #fbbf24;
    font-weight: 800;
    letter-spacing: 0.10em;
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
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
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
    color: #fbbf24;
    font-size: 0.85rem;
    font-weight: 800;
    font-family: ui-monospace, monospace;
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
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    text-align: left;
    width: 100%;
  }
  .oes-sym-row:hover {
    background: rgba(251, 191, 36, 0.12);
    color: #fbbf24;
  }
  .oes-sym-row-sym {
    font-weight: 700;
    letter-spacing: 0.03em;
  }
  .oes-sym-row-meta {
    color: var(--algo-muted);
    font-size: 0.55rem;
    letter-spacing: 0.06em;
  }
  /* Exchange tag — small, muted, matches the LogPanel chip palette. */
  .oes-exch {
    color: var(--algo-muted);
    background: rgba(126, 151, 184, 0.15);
    border: 1px solid rgba(126, 151, 184, 0.32);
    padding: 0.06rem 0.32rem;
    border-radius: 2px;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
    font-family: ui-monospace, monospace;
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
    color: #fbbf24;
    padding: 0.18rem 0.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .oes-wl-toast.ok  { color: #4ade80; background: rgba(74,222,128,0.16); }
  .oes-wl-toast.err { color: #f87171; background: rgba(248,113,113,0.16); }

  /* Chart icon button — opens ChartModal for the current symbol.
     Same 1.4rem × 1.4rem dimensions + cyan-400 palette as the
     card-control trio (CollapseButton, FullscreenButton). */
  .oes-chart-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.40);
    border-radius: 3px;
    color: #22d3ee;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    padding: 0;
  }
  .oes-chart-btn:hover:not(:disabled) {
    background: rgba(103, 232, 249, 0.18);
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
    margin-left: auto;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    color: #f87171;
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    cursor: pointer;
    font-family: monospace;
    font-size: 0.95rem;
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
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    border: 1px solid;
  }
  .bms-row-ok    { border-color: rgba(74,222,128,0.35); background: rgba(74,222,128,0.08); color: #4ade80; }
  .bms-row-warn  { border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.08); color: #fbbf24; }
  .bms-row-short { border-color: rgba(248,113,113,0.45); background: rgba(248,113,113,0.10); color: #f87171; }
  .bms-row-err   { border-color: rgba(248,113,113,0.35); background: rgba(248,113,113,0.08); color: #f87171; }
  .bms-acct { font-weight: 700; margin-right: 0.15rem; }
  .bms-kv   { display: inline-flex; gap: 0.15rem; }
  .bms-k    { opacity: 0.65; }
  .bms-v    { font-variant-numeric: tabular-nums; }
  .bms-kv-short .bms-v { color: #f87171; }
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
  .oes-basket-tpl-row-shell {
    padding: 0.45rem 0.7rem;
    background:
      linear-gradient(90deg,
        rgba(251, 191, 36, 0.10) 0%,
        rgba(251, 191, 36, 0.04) 100%),
      rgba(13, 22, 38, 0.55);
    border-top: 1px solid rgba(251, 191, 36, 0.42);
    border-bottom: 1px solid rgba(251, 191, 36, 0.42);
    box-shadow: inset 0 1px 0 rgba(251, 191, 36, 0.10);
    box-sizing: border-box;
  }
  /* Parameter override row — sits inline with the Select. Each
     param is a tight label+input pair. The input is bare-monospace
     for density; placeholder shows the template's value so the
     operator sees what the value would be without overrides. */
  .oes-basket-tpl-params {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
    margin-left: 0.4rem;
  }
  .oes-basket-tpl-param {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-family: monospace;
    font-size: 0.58rem;
    color: var(--algo-muted);
  }
  .oes-basket-tpl-param > span {
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    color: rgba(251, 191, 36, 0.85);
  }
  /* On-fill param inputs — amber accent on dark navy. The new
     container gradient already carries an amber wash, so the input
     borders use a solid amber that pops against the gradient and
     reads as algo-primary. Focus state inverts to bright amber with
     an inset glow so the active field jumps out. */
  .oes-basket-tpl-param > input {
    width: 3.6rem;
    height: 1.4rem;
    padding: 0 0.35rem;
    background: rgba(12, 18, 32, 0.82);
    border: 1px solid rgba(251, 191, 36, 0.70);
    border-radius: 3px;
    color: #f8fafc;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 600;
    text-align: right;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    box-shadow: inset 0 0 0 1px rgba(251, 191, 36, 0.10);
    transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
  }
  .oes-basket-tpl-param > input:hover {
    border-color: rgba(251, 191, 36, 0.95);
  }
  .oes-basket-tpl-param > input:focus {
    outline: none;
    border-color: var(--algo-amber, #fbbf24);
    background: rgba(28, 22, 8, 0.92);
    box-shadow: inset 0 0 0 1px rgba(251, 191, 36, 0.55),
                0 0 0 2px rgba(251, 191, 36, 0.20);
  }
  .oes-basket-tpl-param > input::placeholder {
    color: rgba(251, 191, 36, 0.75);
    font-style: italic;
  }
  .oes-basket-tpl-pick {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: monospace;
    font-size: 0.62rem;
    color: var(--algo-muted);
  }
  .oes-basket-tpl-label {
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 800;
    color: var(--algo-amber, #fbbf24);
    font-size: 0.6rem;
  }
  /* Default / None two-pill toggle — mirrors the Side toggle in
     OrderTicket so the operator's mental model is the same: Default
     attaches the platform-resolved template, None opts out. Sits
     compact next to the "Template" label. */
  .oes-tpl-toggle {
    display: inline-flex;
    height: 1.4rem;
    min-height: 1.4rem;
    border-radius: 3px;
    overflow: hidden;
    background: rgba(8, 14, 28, 0.55);
    border: 1px solid rgba(251, 191, 36, 0.55);
    box-sizing: border-box;
  }
  .oes-tpl-btn {
    flex: 0 0 auto;
    padding: 0 0.75rem;
    background: transparent;
    border: 0;
    color: rgba(200, 216, 240, 0.65);
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .oes-tpl-btn + .oes-tpl-btn { border-left: 1px solid rgba(251, 191, 36, 0.30); }
  .oes-tpl-btn:hover:not(.on):not([disabled]) {
    background: rgba(251, 191, 36, 0.08);
    color: #f1f7ff;
  }
  .oes-tpl-btn.on {
    background: rgba(251, 191, 36, 0.22);
    color: var(--algo-amber, #fbbf24);
  }
  .oes-tpl-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  /* Active-template name chip — sits inline next to the Default pill
     so the operator sees WHICH default Default resolved to (relevant
     once 4 side-defaults are seeded). */
  .oes-basket-tpl-name {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 600;
    color: #f8fafc;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.32);
    padding: 0.12rem 0.42rem;
    border-radius: 3px;
    letter-spacing: 0.02em;
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
    font-size: 0.58rem;
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
    border-top: 2px solid #4ade80;
    background: rgba(74,222,128,0.18);
    box-shadow: inset 0 4px 12px rgba(0,0,0,0.25);
    flex-shrink: 0;
    z-index: 2;
  }
  .oes-basket-result {
    font-family: monospace;
    font-size: 0.62rem;
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
    font-size: 0.58rem;
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
    color:        #fbbf24;
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
    font-size: 0.52rem;
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
    font-size: 0.58rem;
  }
  .oes-basket-pill-acct-wrap :global(.algo-select-btn) {
    height: 1.2rem;
    padding: 0 0.25rem;
    background: rgba(255,255,255,0.06);
    color: var(--algo-slate);
    border: 1px solid rgba(125,211,252,0.32);
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
  }
  .oes-basket-pill-acct-wrap :global(.algo-select-btn:hover) {
    border-color: rgba(125,211,252,0.65);
  }
  .oes-basket-pill-acct-static {
    margin-left: 0.35rem;
    color: #7dd3fc;
    font-size: 0.58rem;
    font-family: ui-monospace, monospace;
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
  .oes-basket-pill-remove:hover:not(:disabled) { color: #f87171; }
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    font-variant-numeric: tabular-nums;
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
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
    font-size: 0.55rem;
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
  .oes-leg-editor-field > input,
  .oes-leg-editor-field > select {
    height: 1.3rem;
    padding: 0 0.3rem;
    background: rgba(8, 14, 28, 0.78);
    border: 1px solid rgba(34, 211, 238, 0.65);
    border-radius: 3px;
    color: #f8fafc;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 600;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
    box-shadow: inset 0 0 0 1px rgba(34, 211, 238, 0.08);
    transition: border-color 0.12s, background 0.12s;
  }
  .oes-leg-editor-field > input { width: 3.2rem; text-align: right; }
  .oes-leg-editor-field > select { min-width: 5.6rem; }
  .oes-leg-editor-field > input:hover,
  .oes-leg-editor-field > select:hover {
    border-color: rgba(34, 211, 238, 0.90);
  }
  .oes-leg-editor-field > input:focus,
  .oes-leg-editor-field > select:focus {
    outline: none;
    border-color: var(--algo-cyan, #22d3ee);
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
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    cursor: pointer;
  }
  .oes-leg-editor-clear:hover:not(:disabled),
  .oes-leg-editor-close:hover {
    background: rgba(126, 151, 184, 0.15);
    color: #c8d8f0;
  }
  .oes-leg-editor-close { margin-left: auto; }
  .oes-basket-pill.is-disabled { opacity: 0.55; }
  .oes-basket-pill-limit-wrap {
    display: inline-flex;
    align-items: center;
    gap: 0.05rem;
    margin-left: 0.3rem;
  }
  .oes-basket-pill-limit-prefix {
    font-size: 0.65rem;
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
    font-size: 0.7rem;
    padding: 0.1rem 0.25rem;
    text-align: right;
    -moz-appearance: textfield;
  }
  .oes-basket-pill-limit::-webkit-inner-spin-button,
  .oes-basket-pill-limit::-webkit-outer-spin-button { -webkit-appearance: none; }
  .oes-basket-pill-limit:focus {
    outline: none;
    border-color: rgba(125,211,252,0.7);
  }
  .oes-basket-pill-limit-warn {
    border-color: rgba(248,113,113,0.7);
    color: #f87171;
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
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
  }
  .oes-orders-section {
    padding: 0.55rem 0;
  }
  .oes-orders-section + .oes-orders-section {
    border-top: 1px solid rgba(255,255,255,0.07);
  }
  .oes-orders-head {
    color: var(--algo-muted);
    font-size: 0.55rem;
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
    font-size: 0.55rem;
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
    font-size: 0.5rem;
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
    font-size: 0.55rem;
    flex-shrink: 0;
  }
  .oes-event-kind-placed          { color: #38bdf8; }
  .oes-event-kind-chase_modify    { color: #fbbf24; }
  .oes-event-kind-fill            { color: #4ade80; }
  .oes-event-kind-unfill          { color: #f87171; }
  .oes-event-kind-reject          { color: #f87171; }
  .oes-event-kind-preflight_ok    { color: #94a3b8; }
  .oes-event-kind-preflight_block { color: #f87171; }
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
  .oes-card-head {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.3rem 0.5rem;
    font-size: 0.62rem;
    font-weight: 700;
  }
  .oes-card-meta {
    margin-top: 0.2rem;
    font-size: 0.58rem;
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
  }
  .oes-card-sym  { color: var(--algo-slate); font-weight: 800; }
  .oes-card-qty  { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .oes-card-px   { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .oes-card-chase {
    font-size: 0.55rem;
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.4);
    padding: 0 0.3rem;
    border-radius: 2px;
  }

  /* Status pills */
  .oes-status {
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-status-open,
  .oes-status-pending,
  .oes-status-trigger-pending,
  .oes-status-validation-pending  { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.4); }
  .oes-status-complete,
  .oes-status-filled              { background: rgba(74,222,128,0.12);  color: #4ade80; border: 1px solid rgba(74,222,128,0.4); }
  .oes-status-unfilled,
  .oes-status-rejected            { background: rgba(248,113,113,0.12);  color: #f87171; border: 1px solid rgba(248,113,113,0.4); }
  .oes-status-cancelled           { background: rgba(148,163,184,0.1); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }
  /* LOCAL chip — marks algo_order rows that never reached Kite (preflight blocks). */
  .oes-local-chip {
    font-size: 0.52rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
    background: rgba(251,191,36,0.12);
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.35);
  }

  /* Side pills */
  .oes-side {
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-side-buy  { background: rgba(74,222,128,0.12); color: #4ade80; border: 1px solid rgba(74,222,128,0.35); }
  .oes-side-sell { background: rgba(248,113,113,0.12); color: #f87171; border: 1px solid rgba(248,113,113,0.35); }

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

  /* Shared mode + chase toolkit — sits ABOVE the margin/action row
     so both Chain and Ticket tabs read from the same controls.
     Compact: monospace, 0.62rem, tight gaps. Pills + chase glyphs
     mirror the OrderTicket styling so flipping between the two
     modal modes (modal vs standalone OrderTicket) the operator sees
     the same affordance shape. */
  .oes-common-mode-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: var(--algo-slate);
  }
  .oes-common-mode-label {
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    font-size: 0.55rem;
  }
  .oes-common-mode-pills {
    display: inline-flex;
    border: 1px solid rgba(125,211,252,0.32);
    border-radius: 3px;
    overflow: hidden;
  }
  /* Phase B: static mode chip (read-only — set from navbar). */
  .oes-common-mode-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.18rem 0.5rem;
    border: 1px solid currentColor;
    border-radius: 9999px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .oes-common-mode-change {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    color: var(--algo-muted);
    text-decoration: none;
    padding: 0 0.25rem;
  }
  .oes-common-mode-change:hover { color: var(--algo-slate); text-decoration: underline; }

  .oes-common-chase-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    cursor: pointer;
    user-select: none;
  }
  .oes-common-chase-toggle input { margin: 0; }
  /* Grayed-out state for MARKET / SL-M order types — chase is not
     applicable (no limit price to re-quote). Operator: "make chase
     grayed out when market is selected for order type". */
  .oes-common-chase-toggle.is-disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .oes-common-chase-toggle.is-disabled input { cursor: not-allowed; }
  .oes-common-chase-label {
    color: rgba(200,216,240,0.55);
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
  }
  .oes-common-chase-label.on { color: #fbbf24; }

  .oes-common-chase-agg {
    display: inline-flex;
    border: 1px solid rgba(251,191,36,0.32);
    border-radius: 3px;
    overflow: hidden;
  }
  .oes-common-chase-agg-pill {
    padding: 0.14rem 0.4rem;
    background: transparent;
    color: rgba(200,216,240,0.65);
    border: 0;
    border-right: 1px solid rgba(251,191,36,0.20);
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    cursor: pointer;
  }
  .oes-common-chase-agg-pill:last-child { border-right: 0; }
  .oes-common-chase-agg-pill:hover { color: #fbbf24; background: rgba(251,191,36,0.08); }
  .oes-common-chase-agg-pill.on {
    color: #0c1830;
    background: #fbbf24;
  }

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
    font-size: 0.62rem;
    font-weight: 700;
    border: 1px solid transparent;
    white-space: nowrap;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .oes-notice-err  { background: rgba(248,113,113,0.18); border-color: rgba(248,113,113,0.55); color: #f87171; }
  .oes-notice-warn { background: rgba(251,191,36,0.16); border-color: rgba(251,191,36,0.50); color: #fbbf24; }
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
    font-size: 0.62rem;
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
    color: #4ade80;
  }
  /* Audit fix — basket partial-failure warn level. Amber palette so
     the operator sees "some succeeded, some failed" as distinct from
     full-success (green) and full-failure (red). */
  .oes-sticky-result-warn {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  .oes-sticky-result-err {
    background: rgba(248,113,113,0.18);
    border-color: rgba(248,113,113,0.55);
    color: #f87171;
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
    font-size: 0.6rem;
    color: var(--algo-slate);
  }
  .oes-funds-line-low { color: #f87171; }
  .oes-funds-k {
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: var(--algo-muted);
  }
  /* TOTAL prefix gets the amber accent that ties to the modal title. */
  .oes-funds-line .oes-funds-k:first-child:is(:nth-child(1)) {
    color: #fbbf24;
  }
  .oes-funds-v {
    color: #e2e8f0;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .oes-funds-line-low .oes-funds-v { color: #f87171; }
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
    font-size: 0.62rem;
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
    font-size: 0.55rem;
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
    color: #fbbf24;
  }
  .oes-margin-pill-err {
    background: rgba(248, 113, 113, 0.18);
    border-color: var(--algo-red-border);
    color: #f87171;
  }
  .oes-margin-pill-neutral {
    background: rgba(126, 151, 184, 0.12);
    border-color: rgba(126, 151, 184, 0.35);
    color: var(--algo-slate);
  }
  .oes-common-basket,
  .oes-common-side,
  .oes-common-submit {
    /* Bumped vertical padding so button height matches the new
       two-row .oes-margin-pill-stack. */
    padding: 0.55rem 0.75rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: 0.66rem;
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
    font-size: 0.6rem;
    padding-left: 0.55rem;
    padding-right: 0.55rem;
    letter-spacing: 0.02em;
  }
  /* Clear-basket pill nested in the chase row — smaller, quieter
     red so it doesn't compete with the chase pills next to it. */
  .oes-common-clear-inline {
    padding: 0.2rem 0.5rem;
    font-size: 0.55rem;
    border-radius: 3px;
    border: 1px solid rgba(248, 113, 113, 0.40);
    background: transparent;
    color: rgba(248, 113, 113, 0.85);
    cursor: pointer;
    font-family: monospace;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .oes-common-clear-inline:hover { background: rgba(248, 113, 113, 0.10); }
  .oes-common-basket:hover:not(.is-disabled) { background: var(--algo-sky-bg); }
  /* Grayed-out +Basket on Chain tab — affordance stays visible so
     the operator knows the basket flow exists; clicking it would
     do nothing on Chain (per-row +CE / +PE is the path there). */
  .oes-common-basket.is-disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .oes-common-side-buy  { border-color: var(--algo-green-border); color: #4ade80; }
  .oes-common-side-sell { border-color: var(--algo-red-border); color: #f87171; }
  .oes-common-side-buy:hover  { background: rgba(74, 222, 128, 0.12); }
  .oes-common-side-sell:hover { background: rgba(248, 113, 113, 0.12); }
  .oes-common-submit-buy {
    background: rgba(74, 222, 128, 0.18);
    border-color: rgba(74, 222, 128, 0.65);
    color: #4ade80;
  }
  .oes-common-submit-sell {
    background: rgba(248, 113, 113, 0.18);
    border-color: rgba(248, 113, 113, 0.65);
    color: #f87171;
  }
  .oes-common-submit-buy:hover  { background: rgba(74, 222, 128, 0.28); }
  .oes-common-submit-sell:hover { background: rgba(248, 113, 113, 0.28); }
  /* Basket submit — cyan to signal "multiple legs at once", distinct
     from the directional buy/sell palette. */
  .oes-common-submit-basket {
    background: rgba(34, 211, 238, 0.18);
    border-color: rgba(34, 211, 238, 0.65);
    color: #22d3ee;
  }
  .oes-common-submit-basket:hover { background: rgba(34, 211, 238, 0.28); }
  /* Clear-basket — neutral outline. */
  .oes-common-clear {
    padding: 0.35rem 0.75rem;
    border-radius: 4px;
    font-family: monospace;
    font-size: 0.66rem;
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
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
  }
  .oes-bottom-tabs {
    display: flex;
    gap: 0;
    padding: 0;
    /* Active-tab underline already encodes which tab is on. The
       container's bottom divider was clutter — dropped. */
  }
  .oes-bottom-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.35rem 0.65rem;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #94a3b8;
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.12s, border-color 0.12s;
  }
  .oes-bottom-tab.active {
    color: #c084fc;
    border-bottom-color: #c084fc;
    font-weight: 800;
  }
  .oes-bottom-tab:hover:not(.active) { color: var(--algo-slate); }
  .oes-bottom-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.0rem;
    height: 1.0rem;
    padding: 0 0.2rem;
    border-radius: 999px;
    background: rgba(192,132,252,0.22);
    border: 1px solid rgba(192,132,252,0.55);
    color: #c084fc;
    font-size: 0.52rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    line-height: 1;
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
