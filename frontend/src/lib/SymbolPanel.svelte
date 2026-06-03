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
  import { portal } from '$lib/portal';
  import { ORDER_TABS } from '$lib/order/tabs.js';
  import { placeTicketOrder, fetchLiveStatus, fetchOrders, fetchAlgoOrdersRecent } from '$lib/api';
  import ChartModal from '$lib/ChartModal.svelte';
  import { logTime } from '$lib/stores';
  import { priceFmt, aggFmt as aggFmtMargin } from '$lib/format';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';
  import LogPanel        from '$lib/LogPanel.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import Select            from '$lib/Select.svelte';
  import { resolveUnderlying } from '$lib/data/resolveUnderlying';
  import { findNearestFuture, loadInstruments } from '$lib/data/instruments';
  import { resolveUnderlying as _resolveUnderlyingFn } from '$lib/data/resolveUnderlying';
  import { loadAccounts, getDefaultAccount, getDefaultSymbol } from '$lib/data/accounts';

  // Pinned anchors shown at the top of the symbol combo's dropdown.
  // Same set as ChartWorkspace so the operator sees identical pinned
  // options regardless of which icon they clicked. Resolved to the
  // current tradeable contract (NIFTY 50 → NIFTY26JUNFUT,
  // CRUDEOIL → CRUDEOILM26JUNFUT) before display.
  const _DEFAULT_PINS = [
    'NIFTY 50', 'BANKNIFTY', 'FINNIFTY', 'SENSEX',
    'GOLD', 'SILVER', 'CRUDEOIL',
  ];
  function _pinLabel(/** @type {string} */ anchor) {
    const r = resolveUnderlying(String(anchor || '').toUpperCase(), findNearestFuture);
    return r?.tradingsymbol && r.tradingsymbol !== anchor ? r.tradingsymbol : anchor;
  }
  const _PIN_LABELS = _DEFAULT_PINS.map(_pinLabel);
  // Reverse lookup so a pin click reaches the anchor (drives the
  // exchange hint via resolveUnderlying when the operator picks one).
  const _LABEL_TO_ANCHOR = $derived(
    Object.fromEntries(_DEFAULT_PINS.map((a, i) => [_PIN_LABELS[i], a]))
  );

  // Symbol-type filter — mirrors ChartWorkspace's _SYM_TYPE_OPTS so
  // the chart and order modals use the same vocabulary. "EQ · FUT ·
  // OPT" spells out what the unfiltered ALL value contains.
  const _SYM_TYPE_OPTS = [
    { value: 'ALL', label: 'EQ · FUT · OPT' },
    { value: 'EQ',  label: 'Equity'  },
    { value: 'FUT', label: 'Futures' },
    { value: 'OPT', label: 'Options' },
  ];
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
   *   defaultMode?:    'draft' | 'paper' | 'live',
   *   availableModes?: Array<'draft' | 'paper' | 'live'>,
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
    defaultMode    = /** @type {'draft'|'paper'|'live'} */ ('live'),
    availableModes = /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'live']),
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
  const _isDerivative = $derived(/(?:CE|PE|FUT)$/.test(_sym));
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
  // Track whether the currently-picked symbol has tradeable options.
  // Equity tickers like RELIANCE / INFY / TCS are cash-equity on NSE
  // but ALSO have NFO option chains (CE / PE strikes per expiry); the
  // operator wants the Chain tab open for those so they can browse
  // strikes without retyping the underlying. _hasOptionsForSymbol
  // re-runs whenever _localSymbol changes; instruments may load lazily,
  // so it's wrapped in $derived so the gate flips the moment the
  // instruments cache hydrates.
  let _hasOptionsForSymbol = $state(false);
  $effect(() => {
    const s = _localSymbol;
    if (!s) { _hasOptionsForSymbol = false; return; }
    (async () => {
      try {
        const mod = await import('$lib/data/instruments');
        await mod.loadInstruments?.();
        _hasOptionsForSymbol = !!mod.hasOptions?.(s);
      } catch (_) {
        _hasOptionsForSymbol = false;
      }
    })();
  });
  // Chain stays available when the equity has an option chain (e.g.
  // RELIANCE → NFO CE/PE strikes). Only pure cash-equities with no
  // F&O coverage land on the disabled state.
  const chainDisabled = $derived(_isEquityExch && !_isDerivative && !_hasOptionsForSymbol);

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

  function addToBasket(/** @type {any} */ leg) {
    basketLegs = [...basketLegs, leg];
  }
  function removeBasketLeg(/** @type {number} */ i) {
    basketLegs = basketLegs.filter((_, k) => k !== i);
  }
  function clearBasket() { basketLegs = []; basketResultMsg = ''; }

  // ── Shared account state ─────────────────────────────────────────
  // Single source of truth for the routable account across all three
  // tabs (command / ticket / chain). Earlier each tab maintained its
  // own _account so picking it in one tab didn't sync to the others
  // — operators could submit a Ticket-tab order on one account while
  // the Chain-tab basket was staged for another. Lifted here as $state
  // initialised from the `account` prop; each tab receives it as
  // `account` and calls `onAccountChange` when its picker changes.
  let _sharedAccount = $state(account || '');
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
      //   2. orders.default_symbol setting — resolved to a tradeable
      //      contract via resolveUnderlying() (CRUDEOIL → CRUDEOIL26JUNFUT,
      //      NIFTY stays NIFTY, RELIANCE stays RELIANCE). The instruments
      //      cache hydration happens inside resolveUnderlying; we await
      //      it here so the pre-fill is the resolved tradeable, not the
      //      raw underlying name.
      if (!_localSymbol) {
        const defaultSym = getDefaultSymbol();
        if (defaultSym) {
          // Show the raw underlying immediately so the operator sees
          // something while resolution runs in the background.
          _localSymbol = defaultSym.toUpperCase();
          onSymbolChange?.(_localSymbol);
          try {
            // Hydrate the instruments cache before resolving — MCX
            // commodities (CRUDEOIL / GOLD / SILVER) need it to find
            // the nearest future. NSE index spots (NIFTY / BANKNIFTY)
            // resolve from a static map and don't depend on it.
            await loadInstruments().catch(() => null);
            const resolved = _resolveUnderlyingFn(defaultSym.toUpperCase(), findNearestFuture);
            const resolvedSym = resolved?.tradingsymbol || '';
            if (resolvedSym && resolvedSym.toUpperCase() !== _localSymbol) {
              _localSymbol = resolvedSym.toUpperCase();
              onSymbolChange?.(_localSymbol);
            }
          } catch { /* keep raw */ }
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
  }

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
    return { required, available, after, afterCls, shortfall, isCashMode };
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

  // Single-pass leg update — used by chain merge (sym+side dedupe) and
  // +/- steppers. Maps in place so rapid clicks accumulate cleanly
  // instead of relying on the remove+re-add pattern which can drop
  // updates if the prop hasn't propagated back to the child between
  // calls.
  function updateLegByKey(/** @type {string} */ key,
                         /** @type {(leg:any) => any} */ updater) {
    basketLegs = basketLegs.map(b => b.key === key ? updater(b) : b);
  }

  /** Submit every leg in the shell basket via placeTicketOrder. */
  async function submitBasket() {
    if (basketSubmitting || !basketLegs.length) return;
    basketSubmitting = true; basketResultMsg = '';
    let ok = 0; /** @type {string[]} */ const fails = [];
    let basketMode2 = 'paper';
    try {
      const live = await fetchLiveStatus();
      if (live && live.paper_trading_mode === false && live.branch === 'main') basketMode2 = 'live';
    } catch { /* safe default */ }

    // Track failed-leg indices explicitly. The earlier code pruned
    // legs by `i >= ok` which assumed all failures were at the END of
    // the array — but the loop continues on error, so `ok` is a
    // counter of successes, not a positional index. A failing leg in
    // the MIDDLE would survive while a passing leg at the end got
    // dropped. Bug now fixed with an explicit failedIdx set.
    /** @type {Set<number>} */
    const failedIdx = new Set();
    for (let i = 0; i < basketLegs.length; i++) {
      const leg = basketLegs[i];
      try {
        const hasLimit = Number(leg.limit) > 0;
        if (!hasLimit) {
          // Every order is LIMIT + chase[low] by default — silently
          // downgrading to MARKET when the quote hadn't arrived yet
          // bypassed the operator's intent and made the chase engine
          // a no-op (MARKET fills immediately). Force the operator to
          // either wait for the quote or override per-leg manually.
          fails.push(`${leg.side} ${leg.sym}: no quote yet — re-open the chain so the bid/ask price loads, then submit again.`);
          failedIdx.add(i);
          continue;
        }
        const brokerResp = await placeTicketOrder({
          mode:             basketMode2,
          side:             leg.side,
          tradingsymbol:    leg.sym,
          exchange:         leg.exchange || 'NFO',
          quantity:         (leg.lots || 1) * (leg.lotSize || 1),
          product:          leg.product || 'NRML',
          order_type:       'LIMIT',
          variety:          'regular',
          price:            Number(leg.limit),
          account:          leg.account || account || '',
          chase:            true,
          chase_aggressiveness: leg.chaseAgg || 'low',
        });
        ok++;
        // Surface the full ticket-shape payload to the parent (mode +
        // broker_response are what /admin/options needs to push a
        // completion toast and link the fill broadcast back to the
        // right order). Earlier we only spread `leg` which had no
        // mode/broker_response, so the parent's toast logic silently
        // fell through and the operator saw nothing land.
        onSubmit?.({
          mode:           basketMode2,
          side:           leg.side,
          symbol:         leg.sym,
          quantity:       (leg.lots || 1) * (leg.lotSize || 1),
          price:          Number(leg.limit),
          account:        leg.account || account || '',
          broker_response: brokerResp,
          _basketLeg:     true,
          ...leg,
        });
      } catch (e) {
        fails.push(`${leg.side} ${leg.sym}: ${/** @type {any} */ (e)?.message || 'failed'}`);
        failedIdx.add(i);
      }
    }
    basketSubmitting = false;
    const total = basketLegs.length;
    if (!fails.length) {
      basketResultMsg = `${ok}/${total} placed`;
      basketLegs = [];
      setTimeout(onClose, 1500);
    } else if (ok > 0) {
      basketResultMsg = `${ok}/${total} placed — ${fails.length} rejected: ${fails[0]}`;
      // Keep ONLY the legs that failed so the operator can retry just
      // those (after fixing the quote / limit / etc.).
      basketLegs = basketLegs.filter((_, i) => failedIdx.has(i));
    } else {
      basketResultMsg = `Failed: ${fails[0]}`;
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
      defaultMode, availableModes, currentQty, onAddToBasket,
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
        <svg class="oes-modal-name-icon" width="12" height="12" viewBox="0 0 16 16"
             fill="none" stroke="currentColor" stroke-width="2.2"
             stroke-linecap="round" aria-hidden="true">
          <path d="M8 3v10M3 8h10" />
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
         + combo) so the two modals look + behave identically. -->
    {#if !headerless && !inline}
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
            pins={_PIN_LABELS}
            resolvePin={(label) => label}
            type={_symType}
            placeholder="Symbol — pick or type 3+"
            onPick={(sym, meta) => {
              if (meta?.pinLabel) {
                const anchor = _LABEL_TO_ANCHOR[meta.pinLabel] || meta.pinLabel;
                const r = resolveUnderlying(String(anchor).toUpperCase(), findNearestFuture);
                if (r?.tradingsymbol) {
                  _localSymbol = r.tradingsymbol;
                  _pickedExchange = r.exchange || '';
                  onSymbolChange?.(r.tradingsymbol);
                }
              } else {
                _localSymbol = sym;
                if (meta?.exchange) _pickedExchange = meta.exchange;
                onSymbolChange?.(sym);
              }
            }}
            ariaLabel="Symbol — pinned or search" />
        </div>
        {#if exchange || _pickedExchange}
          <span class="oes-exch">{exchange || _pickedExchange}</span>
        {/if}
      </div>
    {/if}

    <!-- Tab strip — suppressed when the host page renders its own
         (tabsExternal). Operator clicks still flow back via the
         two-way bound `activeTab` either way. -->
    {#if !tabsExternal}
    <div class="oes-tabs" role="tablist">
      {#each TABS as tab}
        {@const disabled   = tab.id === 'chain' && chainDisabled}
        {@const isActive   = _activeTab === tab.id}
        {@const badgeCount = tab.id === 'chain' ? basketLegs.length : 0}
        <!-- Tab styling unified with LogPanel + every other algo-side
             tab strip: amber underline + amber text on active, slate
             text + transparent underline otherwise. Drops the colored
             background tint + per-tab colored dot prefix; the
             underline is the single active-state indicator across the
             platform. -->
        <button
          type="button"
          role="tab"
          class="oes-tab"
          class:oes-tab-active={isActive}
          class:oes-tab-disabled={disabled}
          disabled={disabled}
          title={disabled ? 'Option chain only applies to F&O instruments' : undefined}
          aria-selected={isActive}
          aria-disabled={disabled}
          onclick={() => { if (!disabled) _setActiveTab(/** @type {any} */ (tab.id)); }}
        >
          {tab.label}
          {#if tab.id === 'chain' && badgeCount > 0}
            <span class="oes-tab-badge">{badgeCount}</span>
          {/if}
        </button>
      {/each}
    </div>
    {/if}

    <!-- Tab content. Command Line tab retired — was the third option
         alongside Chain and Ticket; the in-tab account+symbol inputs
         duplicated the modal header's. /console keeps its own
         command-line surface for shell-style usage. -->
    <div class="oes-body">
      <!-- OrderTicket is ALWAYS MOUNTED so its margin-preflight $effect
           keeps computing in the background. When the Chain tab is
           active, we hide the ticket body via CSS rather than
           {#if}-unmounting it — otherwise the margin strip in the
           common action footer goes stale on tab switch (operator:
           "margin details are not common for both tabs"). -->
      <div class="oes-ticket-body" hidden={_activeTab !== 'ticket'}>
          <OrderTicket
            symbol={_ticketProps.symbol || _localSymbol}
            exchange={_ticketProps.exchange ?? _pickedExchange ?? exchange}
            side={_ticketProps.side ?? (showCommonActions && !inline ? _modalSide : side)}
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
            defaultMode={_ticketProps.defaultMode ?? defaultMode}
            availableModes={_ticketProps.availableModes ?? availableModes}
            currentQty={_ticketProps.currentQty ?? currentQty}
            onAddToBasket={addToBasket}
            basketMode={basketMode}
            accountHidden={true}
            symbolHidden={!headerless && !inline}
            symType={_symType}
            actionsHidden={actionsHidden || showCommonActions}
            fundsHidden={showCommonActions && !inline}
            refreshKey={_ticketBump}
            triggerSubmit={triggerSubmit + _modalTriggerSubmit}
            triggerBasket={triggerBasket + _modalTriggerBasket}
            hostManagesEsc={true}
            onMarginUpdate={showCommonActions && !inline ? _onMarginUpdate : null}
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
              onSubmit?.({ mode: 'paper', _basketLegs: ok });
              setTimeout(onClose, 400);
            }
          }} />

      {/if}

    </div>

    <!-- Shell-level basket bar — visible from any tab when legs exist.
         Per-leg pills (B/S · sym · lots stepper · × remove) sit on the
         left; Clear / Submit on the right. Same shape as the chain
         tab's in-tab basket so the operator sees what's pending from
         any tab without flipping back to Chain. -->
    {#if basketLegs.length > 0}
      <div class="oes-basket-bar">
        <div class="oes-basket-pills" role="list">
          {#each basketLegs as leg, i (leg.key)}
            <span class="oes-basket-pill oes-basket-pill-{leg.side === 'BUY' ? 'buy' : 'sell'} oes-basket-pill-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                  class:is-disabled={basketSubmitting}
                  role="listitem"
                  title="Click × to remove from basket">
              <span class="oes-basket-pill-side">{leg.side === 'BUY' ? 'B' : 'S'}</span>
              <span class="oes-basket-pill-sym">{leg.sym}</span>
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
              <button type="button" class="oes-basket-pill-remove"
                      title="Remove leg from basket"
                      disabled={basketSubmitting}
                      onclick={() => removeBasketLeg(i)}>×</button>
            </span>
          {/each}
        </div>
        <div class="oes-basket-meta">
          {#if basketResultMsg}
            <span class="oes-basket-result">{basketResultMsg}</span>
          {/if}
          <div class="oes-basket-actions">
            <button type="button" class="oes-basket-clear" disabled={basketSubmitting} onclick={clearBasket}>Clear</button>
            <button type="button" class="oes-basket-submit" disabled={basketSubmitting} onclick={submitBasket}>
              {basketSubmitting ? 'Placing…' : `Submit basket (${basketLegs.length})`}
            </button>
          </div>
        </div>
      </div>
    {/if}

    <!-- Common action footer — same shape as the /orders page-level
         footer (+Basket · Side · BUY/SELL submit). Visible across
         every tab (Ticket / Chain / Command). Inactive on tabs that
         don't accept a generic submit (Chain has its own per-strike
         buttons; CommandLine submits via Enter), but +Basket still
         works when there's pending content. -->
    {#if showCommonActions && !inline && !actionsHidden}
      <div class="oes-common-actions">
        <!-- Funds summary line — sits ABOVE the action row so the
             operator's overall wallet position (Avail margin · Cash ·
             Used) reads at a glance on every tab. Visible regardless
             of which tab is active. Per-order Cost vs Cash / Req vs
             Avail chip lives on the action row below. -->
        {#if _chipMeta && (_chipMeta.availMargin != null || _chipMeta.cash != null)}
          <div class="oes-funds-line"
               class:oes-funds-line-low={(_chipMeta.availMargin ?? 0) < 0}
               title={(_chipMeta.fundsAccount === 'TOTAL')
                 ? 'Sum across every loaded broker account'
                 : `Funds for ${_chipMeta.fundsAccount}`}>
            {#if _chipMeta.fundsAccount === 'TOTAL'}
              <span class="oes-funds-k">TOTAL</span>
            {/if}
            {#if _chipMeta.availMargin != null}
              <span class="oes-funds-k">Avail margin</span>
              <span class="oes-funds-v">₹{aggFmtMargin(_chipMeta.availMargin)}</span>
            {/if}
            {#if _chipMeta.cash != null}
              <span class="oes-funds-sep">·</span>
              <span class="oes-funds-k">Cash</span>
              <span class="oes-funds-v">₹{aggFmtMargin(_chipMeta.cash)}</span>
            {/if}
            {#if (_chipMeta.usedMargin ?? 0) > 0}
              <span class="oes-funds-sep">·</span>
              <span class="oes-funds-k">Used</span>
              <span class="oes-funds-v">₹{aggFmtMargin(_chipMeta.usedMargin)}</span>
            {/if}
          </div>
        {/if}
        <!-- Single action row: margin chip (left) + adaptive submit
             buttons (right). Operator request: "required and available
             margin can be displayed before order buttons. only two
             fields required margin and available margin as a single
             chip. color code the chip based on margin availability". -->
        <div class="oes-common-row">
          {#if _marginInfo}
            {@const _isCash = !!_marginInfo.isCashMode}
            {@const _reqKey = _isCash ? 'Cost' : 'Req'}
            {@const _avlKey = _isCash ? 'Cash' : 'Avail'}
            {@const _kind = _isCash ? 'Cash debit' : 'Margin required'}
            <span class="oes-margin-pill oes-margin-pill-{_marginPillCls}"
                  title={_marginInfo.error
                    ? `Preview: ${_marginInfo.error}`
                    : _marginInfo.loading
                      ? `Computing ${_kind.toLowerCase()}…`
                      : `${_kind}: ₹${aggFmtMargin(_marginInfo.required)} vs ${_avlKey} ₹${aggFmtMargin(_marginInfo.available ?? 0)}`}>
              {#if _marginInfo.error}
                ⚠ {_marginInfo.error}
              {:else if _marginInfo.loading}
                Computing {_isCash ? 'cost' : 'margin'}…
              {:else}
                <span class="oes-margin-pill-key">{_reqKey}</span>
                <span class="oes-margin-pill-val">₹{aggFmtMargin(_marginInfo.required)}</span>
                {#if _marginInfo.available != null}
                  <span class="oes-margin-pill-sep">·</span>
                  <span class="oes-margin-pill-key">{_avlKey}</span>
                  <span class="oes-margin-pill-val">₹{aggFmtMargin(_marginInfo.available)}</span>
                {/if}
              {/if}
            </span>
          {/if}
          <span class="oes-common-spacer"></span>
          {#if basketLegs.length > 0}
            <button type="button" class="oes-common-clear"
              title="Clear all basket legs"
              disabled={basketSubmitting}
              onclick={clearBasket}>Clear basket</button>
          {/if}
          <button type="button" class="oes-common-basket"
            title="Add the current order to the basket"
            onclick={_modalFireBasket}>+ Basket</button>
          <button type="button" class="oes-common-submit"
            class:oes-common-submit-buy={_submitFlavor === 'buy'}
            class:oes-common-submit-sell={_submitFlavor === 'sell'}
            class:oes-common-submit-basket={_submitFlavor === 'basket'}
            title={basketLegs.length > 0
              ? `Submit all ${basketLegs.length} basket legs`
              : 'Place the order via the active tab'}
            disabled={basketSubmitting}
            onclick={() => {
              if (basketLegs.length > 0) submitBasket();
              else _modalFireSubmit();
            }}>{basketSubmitting ? 'Placing…' : _submitLabel}</button>
        </div>
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
    color: #c8d8f0;
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
    width: 100%;
    max-height: none;
    border-radius: 0;
    box-shadow: none;
    /* Drop the amber outline + gradient background — the host card
       provides its own chrome, so a second nested border would
       double-frame the content. Per operator request "remove the
       current yellow border". */
    border: none;
    background: transparent;
  }
  /* Header */
  .oes-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.7rem 1rem 0.5rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    flex-shrink: 0;
  }
  /* Modal-name chip — matches .cm-title / .alm-title typography on the
     sibling modals so all three read the same at the top-left. */
  .oes-modal-name {
    font-family: monospace;
    font-size: 0.65rem;
    color: #7e97b8;
    font-weight: 600;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Amber matches the page-header Order button so the modal title
     reads as the same surface the operator clicked to open it. */
  .oes-modal-name-icon { color: #fbbf24; flex-shrink: 0; }
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
    padding: 0.4rem 0.6rem;
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
    color: #c8d8f0;
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
    border-color: rgba(251, 191, 36, 0.55);
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
    color: #c8d8f0;
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
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
  }
  /* Exchange tag — small, muted, matches the LogPanel chip palette. */
  .oes-exch {
    color: #7e97b8;
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
    /* Push to the right edge of the header. The wl-toast (if shown)
       gets squeezed against the close button — same as before. */
    margin-left: auto;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    color: #f87171;
    width: 1.8rem;
    height: 1.8rem;
    border-radius: 3px;
    /* Defensive: button always receives clicks + sits above panel content */
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    cursor: pointer;
    font-family: monospace;
    font-size: 1.1rem;
    line-height: 1;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s;
  }
  .oes-close:hover { background: rgba(248, 113, 113, 0.15); }

  /* Tab strip — bottom-border underline active tab; each tab has its own
     accent colour via inline style (applied from the TABS metadata).     */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    flex-shrink: 0;
  }
  /* Tab style — underline-only active state, matches LogPanel +
     /admin/tokens + every other algo-side tab strip. */
  .oes-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.45rem 0.75rem;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #b4c8e6;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s;
    white-space: nowrap;
  }
  .oes-tab:hover:not(.oes-tab-disabled):not(.oes-tab-active) {
    color: #fbbf24;
  }
  .oes-tab-active {
    border-bottom-color: #d97706;
    color: #fbbf24;
    font-weight: 800;
  }
  .oes-tab-disabled {
    cursor: not-allowed !important;
    opacity: 0.5;
  }
  /* Count badge on Chain tab */
  .oes-tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.1rem;
    height: 1.1rem;
    padding: 0 0.25rem;
    border-radius: 999px;
    background: rgba(74,222,128,0.25);
    border: 1px solid rgba(74,222,128,0.6);
    color: #4ade80;
    font-size: 0.55rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  /* Basket bar — sticky bottom strip inside the modal when legs exist. */
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
    color: #c8d8f0;
    margin-right: 0.4rem;
  }
  .oes-basket-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  .oes-basket-actions {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

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
  .oes-basket-pill.is-disabled { opacity: 0.55; }
  .oes-basket-clear,
  .oes-basket-submit {
    height: 1.9rem;
    padding: 0 0.85rem;
    border-radius: 2px;
    font-family: monospace;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    border: 1px solid currentColor;
    background: transparent;
    white-space: nowrap;
  }
  .oes-basket-clear  { color: #a3b9d0; }
  .oes-basket-clear:hover:not(:disabled) { background: rgba(163,185,208,0.08); }
  .oes-basket-submit {
    color: #fff;
    background: #4ade80;
    border-color: #4ade80;
    font-weight: 800;
  }
  .oes-basket-submit:hover:not(:disabled) { background: #4ade80; border-color: #4ade80; }
  .oes-basket-clear:disabled,
  .oes-basket-submit:disabled { opacity: 0.45; cursor: progress; }

  /* Body — the tab content area. Flex column so child panels (chain
     grid in particular) can `flex: 1` to fill the full available
     height instead of clamping to their content. */
  .oes-body {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
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
    padding: 0.6rem 0.9rem !important;
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
    color: #7e97b8;
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
    color: #7e97b8;
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
    color: #c8d8f0;
  }
  .oes-event-time {
    color: #7e97b8;
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
  .oes-event-kind-agent_paused        { color: #7e97b8; }
  .oes-event-msg { color: #c8d8f0; }

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
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
  }
  .oes-card-sym  { color: #c8d8f0; font-weight: 800; }
  .oes-card-qty  { color: #c8d8f0; font-variant-numeric: tabular-nums; }
  .oes-card-px   { color: #c8d8f0; font-variant-numeric: tabular-nums; }
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
    padding: 0.5rem 0.85rem;
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

  /* Margin strip — sits BELOW the action buttons. MARGIN · Avail ·
     After · (Short) cells in a horizontal row. After is colour-coded
     by remaining-margin band (mirrors the OrderTicket's
     ot-margin-row-{err,warn,sub} convention). */
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
    color: #c8d8f0;
  }
  .oes-funds-line-low { color: #f87171; }
  .oes-funds-k {
    text-transform: uppercase;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: #7e97b8;
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
    color: #c8d8f0;
    white-space: nowrap;
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
    border-color: rgba(248, 113, 113, 0.55);
    color: #f87171;
  }
  .oes-margin-pill-neutral {
    background: rgba(126, 151, 184, 0.12);
    border-color: rgba(126, 151, 184, 0.35);
    color: #c8d8f0;
  }
  .oes-common-basket,
  .oes-common-side,
  .oes-common-submit {
    padding: 0.35rem 0.75rem;
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
  .oes-common-basket:hover { background: rgba(125, 211, 252, 0.12); }
  .oes-common-side-buy  { border-color: rgba(74, 222, 128, 0.55); color: #4ade80; }
  .oes-common-side-sell { border-color: rgba(248, 113, 113, 0.55); color: #f87171; }
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
    color: #7e97b8;
    transition: background 0.12s, color 0.12s;
  }
  .oes-common-clear:hover:not(:disabled) {
    background: rgba(126, 151, 184, 0.12);
    color: #c8d8f0;
  }
  .oes-common-clear:disabled { opacity: 0.45; cursor: progress; }

  /* ── Bottom panel (Log / Orders) ──────────────────────────────────── */
  /* Sits AFTER the common action footer ("move the order placement
     above the activity"). Mirrors ActivityLogModal's .alm-body —
     flex column, fixed slot inside the modal's overall height, the
     LogPanel inside expands via heightClass="flex-1 min-h-0". */
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
  .oes-bottom-tab:hover:not(.active) { color: #c8d8f0; }
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
