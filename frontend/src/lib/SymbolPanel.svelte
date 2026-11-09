<script>
  // SymbolPanel — unified symbol-keyed modal. Replaces three older
  // surfaces: OrderEntryShell (tabbed order entry), SymbolChartModal
  // (standalone chart popup), and SymbolActions (the ⋯ row-menu).
  //
  // Tabs (see $lib/order/tabs.js ORDER_TABS for the current set):
  //   ticket   — single-leg OrderTicket. Default for trade intent.
  //   chain    — option-chain basket builder (OptionChainTab).
  //              Disabled for cash-equity symbols.
  //   cli      — agent-family CLI (OrderCliTab/AgentCliEditor), scoped
  //              to order(...)/place_order(...) statements only.
  //              Power-user surface; lands last in the tab strip. The
  //              older positional-grammar `command` tab (CommandLineTab)
  //              was retired 2026-06 and deleted entirely 2026-10-17.
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
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';
  import { get as _storeGet } from 'svelte/store';
  import { portal } from '$lib/portal';
  import { ORDER_TABS } from '$lib/order/tabs.js';
  import { formatSubmitLabel } from '$lib/order/orderTicketSubmit.js';
  import { SYM_TYPE_OPTS } from '$lib/data/symbolTypes';
  import { placeBasket, fetchBasketMargin, fetchLiveStatus } from '$lib/api';
  import ChartModal from '$lib/ChartModal.svelte';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import { executionMode, visibleInterval } from '$lib/stores';
  import { priceFmt, aggFmt as aggFmtMargin, qtyFmt } from '$lib/format';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';
  import OrderCliTab      from '$lib/order/OrderCliTab.svelte';
  import ChaseCard       from '$lib/order/ChaseCard.svelte';
  import ChaseAggPicker  from '$lib/order/ChaseAggPicker.svelte';
  import OrderBook from '$lib/OrderBook.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import Select            from '$lib/Select.svelte';
  // resolveUnderlying / findNearestFuture / resolveAnchorToTradeable
  // dynamically imported inside effects only — no static imports needed.
  import { loadAccounts, getDefaultAccount, recentSymbolStore, setRecentSymbol, setRecentAccount } from '$lib/data/accounts';
  import { segmentOpenForExchange } from '$lib/marketHours';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import { liveSnap } from '$lib/data/symbolStore.svelte.js';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { resolveUnderlying, resolveUnderlyingTradingsymbol } from '$lib/data/resolveUnderlying.js';
  import { findNearestFuture, instrumentsCacheVersion, getInstrument } from '$lib/data/instruments.js';
  import { rootOf } from '$lib/data/rootOf.js';
  import { payoffDrafts } from '$lib/data/payoffDrafts.svelte.js';

  // Bracket/Templ preference — DEAD (T3.3, bracket-removal). The
  // per-scope on/off/specific-template localStorage preference this
  // key used to carry no longer has any reader; `_TEMPL_PREF_KEY_STALE`
  // is removed once, on mount, below (see the onMount block) so an old
  // browser session doesn't carry the dead key forever.
  const _TEMPL_PREF_KEY_STALE = 'ramboq_templ_pref_v1';

  // Pinned anchors: no hardcoded list — SymbolSearchInput's own
  // _autoLoadPins() fires when no `pins` prop is supplied and loads
  // from loadWatchlistSymbols(). Drop _DEFAULT_PINS / _PIN_LABELS /
  // _LABEL_TO_ANCHOR entirely; the picked symbol is already the
  // real tradingsymbol, so no reverse-lookup is needed.

  // Symbol-type filter — shared 4-option vocabulary so every
  // surface (modals, /orders, /charts) reads the same.
  const _SYM_TYPE_OPTS = SYM_TYPE_OPTS;
  let _symType     = $state(/** @type {'ALL'|'EQ'|'FUT'|'OPT'} */ ('ALL'));
  let _exchFilter  = $state(/** @type {string} */ ('ALL'));

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
   *   avgCost?:        number | null,
   *   unrealizedPnl?:  number | null,
   *   onSubmit:        (payload: any) => void | Promise<void>,
   *   onClose:         () => void,
   *   onAddToBasket?:  ((payload: any) => void) | null,
   *   onAddToWatchlist?: ((sym: string, exch?: string) => void | Promise<void>) | null,
   *   inline?:         boolean,
   *   headerless?:     boolean,
   *   onSymbolChange?: ((sym: string) => void) | null,
   *   tabsExternal?:   boolean,
   *   activeTab?:      'ticket' | 'chain' | 'chart' | 'cli',
   *   allowedTabs?:    Array<'ticket'|'chain'|'chart'|'cli'> | null,
   *   hideBottomPanel?: boolean,
   *   actionsHidden?:  boolean,
   *   triggerSubmit?:  number,
   *   triggerBasket?:  number,
   *   triggerClearBasket?: number,
   *   triggerRefresh?: number,
   *   chase?:          boolean,
   *   chaseAgg?:       'low'|'med'|'high',
   *   basketCount?:    number,
   *   showChartButton?: boolean,
   *   showCommonActions?: boolean,
   *   showRefresh?: boolean,
   *   pickerSuffix?:   import('svelte').Snippet,
   *   initialDraftId?: number | null,
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
    avgCost        = /** @type {number|null} */ (null),
    unrealizedPnl  = /** @type {number|null} */ (null),
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
    activeTab      = $bindable(/** @type {'ticket'|'chain'|'chart'|'cli'|undefined} */ (undefined)),
    // Restricts which tabs the strip shows (and which tabs the
    // default/activeTab resolution can land on). null/omitted (every
    // caller as of this prop's addition) means "unchanged" — all 4
    // tabs show, exactly as before. Purely additive — when set, TABS
    // is filtered to the ids present here before the tab-strip render,
    // and `_resolveInitialTab()` falls back to `allowedTabs[0]` if the
    // otherwise-resolved default isn't in the list.
    allowedTabs    = /** @type {Array<'ticket'|'chain'|'chart'|'cli'>|null} */ (null),
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
    // When true, renders a refresh button in the modal header that
    // calls _refreshAll() (same action as CardHeader's onRefresh).
    // The order modal in PageHeaderActions passes showRefresh={true}
    // so operators can reload the ticket without closing.
    showRefresh = false,
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
    triggerRefresh = 0,
    // Optional snippet rendered inside .oes-picker, immediately after
    // the symbol search input. /orders passes the CHASE cluster here so
    // it appears inline in the same flex row as the symbol input, without
    // needing a separate header-level block.
    /** @type {import('svelte').Snippet | undefined} */
    pickerSuffix = undefined,
    // When set, OrderTicket opens pre-filled from this payoffDrafts id
    // (the real backend AlgoOrder id of a mode='draft' row) with
    // _sharedDraftMode=true. Submit → "Update Draft" (PATCHes the row in
    // place). Closing without submitting leaves the draft untouched
    // (2026-10 fix). Passed through from _openDraftTicket on the
    // /orders page. The derivatives page's own draft-row click handler
    // (executeDraft) is a separate, local-only mechanism that does not
    // use this prop — see payoffDrafts.svelte.js's module docstring.
    initialDraftId = /** @type {number|null} */ (null),
  } = $props();

  // Local mutable copy of the symbol prop — operator can edit it from
  // the top search input so every tab (Ticket / Chain /
  // Command) re-renders against the new symbol. Synced from the prop
  // via $effect so an external pick (chain row + CE click, dashboard
  // row click, MarketPulse symbol pick) still updates the shell.
  // intentional: seeds from symbol prop once; $effect below re-syncs on external prop changes
  // svelte-ignore state_referenced_locally
  let _localSymbol = $state(String($state.snapshot(symbol) || '').toUpperCase());
  // Header days-to-expiry chip REMOVED (2026-09-30, operator: "remove
  // 15d from order ticket after symbol. I am reversing my decision.") —
  // was added earlier this same day to make the DTE chip common across
  // every tab. Chain tab's own Expiry-row chip (.oct-expiry-dte in
  // OptionChainTab.svelte, unaffected by this reversal) remains the
  // only days-to-expiry indicator.
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
    const resolved = (req === 'chain' && chainDisabled) ? 'ticket' : req;   // fall through for cash equity
    // allowedTabs restriction (additive — null/omitted means unchanged).
    // If the otherwise-resolved default isn't in the allowed set, fall
    // back to the first allowed tab instead of landing on a tab the
    // host never asked to show.
    if (Array.isArray(allowedTabs) && allowedTabs.length && !allowedTabs.includes(resolved)) {
      return allowedTabs[0];
    }
    return resolved;
  }
  // chart tab guard on external activeTab binding — chart is never the
  // default starting tab so if the host somehow passes 'chart' via the
  // bindable, resolve to 'ticket' instead.
  // (No separate function needed — _activeTab is derived from activeTab
  // and _activeTabInternal; chart is a valid in-session state once the
  // operator clicks the tab, just not a valid initial default.)
  // If the host wired `activeTab` we mirror it; otherwise own the
  // state internally. Either way, downstream code reads `_activeTab`
  // and tab-click handlers write to it — the two-way bind takes care
  // of pushing the change back up to the host when applicable.
  let _activeTabInternal = $state(/** @type {'ticket'|'chain'|'chart'|'cli'} */ (_resolveInitialTab()));
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

  function _setActiveTab(/** @type {'ticket'|'chain'|'chart'|'cli'} */ id) {
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
    } else if (id === 'chart') {
      // Restore the context contract so chart shows the option/future
      // the operator opened the panel with, not the root symbol that
      // the Chain tab strips down to.
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

  // Live LTP for the current symbol — pulled from the shared symbolStore
  // so the tab row can show a real-time price without a separate fetch.
  // Chain tab shows root LTP (e.g. CRUDEOIL for any CRUDEOIL future);
  // Ticket + Chart tabs show the contract's own LTP so the header price
  // reflects the exact instrument the operator is viewing / trading.
  //
  // 2026-10-17 fix (operator: "when chain is pressed the ltp field is
  // getting erased") — the bare root (`_parseRoot(_localSymbol)`, e.g.
  // "NIFTY") is NOT itself a live-ticked quote key for an index
  // underlying (Kite ticks it under "NIFTY 50") or for an unheld/cold
  // MCX commodity (no virtual-root alias published yet) — `liveSnap`
  // on the bare root silently resolved to null, erasing the field.
  // `resolveUnderlyingTradingsymbol` (same SSOT Snapshot/Payoff/NavStrip
  // use — see CLAUDE.md "Underlying spot resolution") maps the root to
  // its real tradeable/quote-key tradingsymbol first (index spot key,
  // MCX/CDS nearest future, or the root unchanged for everything else);
  // falling back to the bare root keeps today's working case (a
  // commodity WITH a published virtual-root alias) unaffected.
  // `$instrumentsCacheVersion` dependency mirrors OrderTicket's `_dte` —
  // `findNearestFuture` reads a non-reactive module-level cache, so
  // without it a cold-cache resolution (bare root, wrong) never
  // re-resolves once instruments finish loading.
  const _ltpSym = $derived.by(() => {
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    if (_activeTab !== 'chain') return _localSymbol;
    const root = _parseRoot(_localSymbol) || _localSymbol;
    if (!root) return _localSymbol;
    return resolveUnderlyingTradingsymbol(root, findNearestFuture) || root;
  });
  const _ltp = $derived(
    liveSnap(_ltpSym)?.ltp
    ?? (_activeTab === 'chain' ? liveSnap(_parseRoot(_localSymbol) || _localSymbol)?.ltp : null)
    ?? null
  );
  // Flash parity with Pulse's LTP (operator: "even ltp which is a common
  // element flashes while refresh" — this header chip, shared by both the
  // Chain and Ticket tabs, had none). Standard `createTickFlash` usage
  // per its own module docstring: an `$effect` that only READS `_ltpSym`/
  // `_ltp` (both plain `$derived`) and calls `update()`, which writes
  // only its own internal `classes` state — no dependency cycle, unlike
  // the live-wing-badge incident this session (an effect that read AND
  // wrote the same tracked value).
  const _ltpFlash = createTickFlash({ durationMs: 350 });
  $effect(() => { _ltpFlash.update(_ltpSym, _ltp); });

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
  let _headerRefreshing = $state(false);
  $effect(() => {
    const t = _activeTab;
    untrack(() => {
      if (t === 'ticket') _ticketBump++;
      else if (t === 'chain') _chainBump++;
    });
  });

  function _refreshAll() {
    _ticketBump++;
    _chainBump++;
    _headerRefreshing = true;
    setTimeout(() => { _headerRefreshing = false; }, 700);
  }

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
  // No Vitest test: onDestroy cleanup in .svelte components requires a component
  // mount harness; no corresponding Vitest test file exists for this component.
  onDestroy(() => {
    _ltpFlash.dispose();
    // Prevent dangling debounce from firing a symbol search after unmount.
    if (_symbolDebounce) clearTimeout(_symbolDebounce);
    // Prevent sticky result clear-timer from accessing destroyed state.
    if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
  });

  function addToBasket(/** @type {any} */ leg) {
    basketLegs = [...basketLegs, leg];
  }
  function removeBasketLeg(/** @type {number} */ i) {
    basketLegs = basketLegs.filter((_, k) => k !== i);
  }
  function clearBasket() {
    basketLegs = []; basketResultMsg = ''; _basketMarginRows = [];
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

  // Parent-driven refresh — bumping triggerRefresh fires _refreshAll().
  // Same skip-initial-0 pattern as triggerClearBasket.
  // svelte-ignore state_referenced_locally
  let _lastRefreshTrigger = $state($state.snapshot(triggerRefresh));
  $effect(() => {
    if (triggerRefresh !== _lastRefreshTrigger) {
      _lastRefreshTrigger = triggerRefresh;
      if (triggerRefresh > 0) _refreshAll();
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
  // Debounced effect — fires 500 ms after any basket change when basket
  // spans ≥2 accounts; clears the strip when basket is empty or single-acct.
  $effect(() => {
    // Track reactive inputs.
    const legs = basketLegs;
    const accts = _basketAccounts;
    void legs; void accts; void _sharedDraftMode;

    if (_basketMarginTimer) { clearTimeout(_basketMarginTimer); _basketMarginTimer = null; }

    // 2026-10-21 fix (operator: "draft positions don't need margin
    // check") — this multi-account basket margin preview had NO draft
    // gate at all; a Draft-mode basket (which never reaches the broker)
    // was still firing a real /api/orders/basket-margin round-trip
    // every 500ms-debounced change. Same early-return shape as the
    // empty-basket/single-account case just below.
    if (_sharedDraftMode) {
      _basketMarginRows = [];
      return;
    }

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

  // Chase tooltip trigger (2026-10-17, operator: "remove tool tip with
  // circle around i, make the labels themselves as tooltips. do it for
  // chase") — CHASE's label is plain, non-interactive text (the actual
  // toggle is a separate control elsewhere), so per the general rule it
  // becomes the tooltip trigger itself instead of needing a separate
  // "(i)" InfoHint chip. Same hideButton+anchor+bind:open pattern
  // OptionsPayoff.svelte's own click-to-pin stat tooltips already use.
  /** @type {HTMLElement | null} */
  let _chaseInfoAnchor = $state(null);
  let _chaseInfoOpen = $state(false);
  function _chaseInfoKeydown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _chaseInfoOpen = !_chaseInfoOpen; }
  }
  // Audit fix (L-3) — demo session gate. Anonymous prod visitors can
  // see the algo surface but every LIVE/PAPER submit is blocked at
  // the API layer. Still used to suppress the CLI tab's own demo gate.
  const _algoStatus = getContext('algoStatus');
  const _isDemo = $derived(_algoStatus?.isDemo ?? false);
  // Chain-only pre-submit spread-gate threshold — see OptionChainTab's
  // runPreSubmitGate(). Rendered in that tab's own Expiry toolbar row.
  let _sharedSpreadMaxPctOverride     = $state(/** @type {number|''} */ (''));
  // bind:this target for OptionChainTab — lets the shared Submit button
  // (below) await its exported runPreSubmitGate() before calling
  // submitBasket(). Chain-tab-only; never set when _activeTab !== 'chain'.
  let _chainTabRef = $state(/** @type {any} */ (null));
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
  //   modal cold + side set     → "Submit · BUY 75"  /  "Submit · SELL 75"
  //   from symbol row + side    → "Submit · ADD · BUY 75"  /  "Submit · CLOSE · SELL 75"
  //                               (verb derived from side vs position direction)
  //
  // R7 fix (2026-09): the label now ALWAYS reflects the real pending
  // action (side / verb / qty), including while chase is on. This
  // supersedes a prior operator instruction ("when chase is active the
  // button should say submit") — that carve-out is exactly the scenario
  // the operator's later report ("close buy close sell buttons don't
  // work") happened in, since chase is the default for LIMIT/SL
  // tickets: a bare "Submit" with no hint of side/qty/close-vs-add read
  // as broken. Qty/side come from `_ticketState` (live, reported by
  // OrderTicket via `onTicketStateChange`) so the label tracks lot
  // steppers and side flips made INSIDE the ticket, not just the
  // initial props this modal was opened with.
  const _submitLabel = $derived.by(() => {
    // 2026-10-23 fix (operator: "it is not add to payoff. better to use
    // the same submit button.") — Draft mode no longer gets a distinct
    // "Add to Payoff" label; it falls through to the exact same label
    // logic every other mode uses. The DRAFT toggle itself already
    // signals the mode; the button's `title` tooltip still says "no
    // broker order" on hover. A dedicated label existed from the
    // 2026-10-21 shared-row re-wire (matching OrderTicket's own
    // now-unreachable-at-the-real-mount footer label) but made this the
    // one button whose text changes shape/width based on mode, which is
    // also what caused it to drop to its own row on a narrow viewport
    // instead of sitting beside the single-leg basket pill above it.
    //
    // 2026-10-23 follow-up (operator: "use submit draft like close buy
    // format for button when draft is selected") — a single staged
    // Chain-tab leg has one unambiguous side, same as the Ticket tab's
    // single order, so it gets the same CLOSE/BUY-style formatted label
    // instead of a bare "Submit" — even in draft mode. A multi-leg
    // basket has no one coherent side to format and keeps the existing
    // plain "Submit" (unchanged, this is the pre-existing non-draft rule
    // too — operator 2026-09-30: "chain button ... shows submit").
    if (_sharedDraftMode && basketLegs.length === 1) {
      const leg = basketLegs[0];
      const legCq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
      return formatSubmitLabel({ side: leg.side, currentQty: legCq, qty: leg.lots || 0, basketCount: 0 });
    }
    // Chain tab always shows plain "Submit" — operator (2026-09-30):
    // "chain button to start with shows submit suffixed by additional
    // text. which should be removed." Before this fix, an EMPTY basket
    // on the Chain tab fell through to the Ticket-style side/verb label
    // below (keyed only on basketLegs.length, not the active tab) — the
    // gate now checks the tab directly so Chain is unconditionally
    // "Submit" whether or not legs are staged yet.
    if (basketLegs.length > 0 || _activeTab === 'chain') return 'Submit';
    const liveSide = _ticketState.side || _modalSide;
    const liveQty  = _ticketState.qty || Number(_ticketProps?.qty ?? qty) || 0;
    const cq = Number(_ticketProps?.currentQty ?? currentQty) || 0;
    return formatSubmitLabel({ side: liveSide, currentQty: cq, qty: liveQty, basketCount: 0 });
  });
  // D4 fix (2026-09): the common-action Submit button's `onclick` only
  // routes to OrderTicket's own `submit()` (via `_modalFireSubmit` →
  // `triggerSubmit++`) in the plain-ticket-submit branch — basket
  // submits go through `submitBasket()` instead, which doesn't touch
  // OrderTicket's `submitting` state at all. This
  // mirrors that same branch condition so the button disables (and
  // shows a pending label) for the FULL duration of a single-ticket
  // submit, not just the unrelated `basketSubmitting` flag — previously
  // a re-click during a slow submit (D3's territory) was a completely
  // natural operator reaction that queued a genuine duplicate order.
  const _ticketOwnSubmitBusy = $derived.by(() =>
    _activeTab === 'ticket' && basketLegs.length === 0
    && (_ticketState.submitting || _ticketState.pending)
  );
  // Mirrors OrderTicket's own `_depthPending` — every template/ticket is
  // LIMIT or GTT (no MARKET), so submission needs the active strike's
  // bid/ask at least once. Gates the shared common-action Submit button
  // too, not just OrderTicket's own internal footer button (2026-10 fix
  // for the "limit price required" click-before-depth-loads incident).
  // 2026-10-21 — excludes Draft mode: OrderTicket's own footer button
  // already excludes Draft from this same disable (it never calls the
  // broker, so there's no depth-quote requirement to wait on); the
  // shell's mirrored button previously didn't, which is the gap this
  // fix closes.
  const _ticketDepthPending = $derived.by(() =>
    !_sharedDraftMode && _activeTab === 'ticket' && basketLegs.length === 0 && !!_ticketState.depthPending
  );
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
  // Latest validation error from the Ticket form — updated whenever
  // OrderTicket's validationErr changes via the onValidationChange
  // callback. Used to surface a toast when the common-action Submit
  // fires but the form is blocking placement silently (e.g. depth
  // ladder hasn't loaded the limit price yet after a 2s poll delay).
  let _ticketValidationErr = $state('');
  // Live side / qty / submitting / pending mirror from OrderTicket
  // (D4 + R7, 2026-09) via the `onTicketStateChange` callback. Without
  // this, the common-action Submit button's label + disabled state can
  // only see the ticket's INITIAL props (`_modalSide`, `_ticketProps.qty`)
  // — stale the moment the operator bumps a lot stepper or the ticket's
  // own submit is in flight (the latter previously left the button
  // clickable for the ENTIRE duration of a slow submit, since it was
  // only gated on the unrelated `basketSubmitting` flag).
  // 2026-10-21 shared-row re-wire — widened with mktOpen/mktExch (the
  // market badge's belief, mirrored up so the shell doesn't recompute
  // or relocate OrderTicket's own `_isOpen`/`_mktOpenForExchange`).
  // The Bracket-display fields (selectedTemplate/defaultTemplate/
  // showsWing/isUsingNone) this payload used to carry are gone —
  // T3.3, bracket-removal.
  let _ticketState = $state(
    /** @type {{side: 'BUY'|'SELL'|null, qty: number, submitting: boolean, pending: boolean, depthPending: boolean, mktOpen: boolean, mktExch: string, showDraftToggle: boolean, resolvedSymbol: string|null}} */
    ({ side: null, qty: 0, submitting: false, pending: false, depthPending: false,
       mktOpen: false, mktExch: '', showDraftToggle: false, resolvedSymbol: '' })
  );
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
    if (_ticketDepthPending) {
      toast.warning('Waiting for market depth — try again in a moment');
      return;
    }
    if (_ticketValidationErr) {
      toast.warning(_ticketValidationErr);
      return;
    }
    _modalTriggerSubmit++;
  }
  function _modalFlipSide()   { _modalSide = _modalSide === 'BUY' ? 'SELL' : 'BUY'; }

  // Margin preview lifted out of OrderTicket so the operator sees the
  // same MARGIN / Avail / After / Short row regardless of which tab is
  // active (matches the operator ask: "margin line should be common
  // for all the tabs in modal"). OrderTicket fires onMarginUpdate
  // every time its computed preview changes; we cache it here and
  // render in the common action footer.
  let _modalMargin        = $state(/** @type {any} */ (null));
  let _modalMarginLoading = $state(false);
  // The basket-leg key the operator has clicked to "focus". Click any
  // basket pill on the Chain tab to focus it; null = follow the LAST
  // leg added (the default). Cleared when the focused leg is removed.
  // Operator: "click a basket pill to swap preview to that leg" —
  // drives `_chainMktExch`'s per-leg market-open badge resolution.
  let _focusedLegKey = $state(/** @type {string|null} */ (null));
  // Resolved focused leg — explicit pick when _focusedLegKey matches a
  // current leg, otherwise the last leg. Used by both `_chainMktExch`
  // and the pill's `is-focused` visual cue.
  const _focusedLeg = $derived.by(() => {
    if (basketLegs.length === 0) return null;
    if (_focusedLegKey) {
      const m = basketLegs.find(l => l.key === _focusedLegKey);
      if (m) return m;
    }
    return basketLegs[basketLegs.length - 1];
  });
  // Drop a stale `_focusedLegKey` once its leg leaves the basket so
  // `_chainMktExch`'s own per-leg exchange resolution cleanly falls
  // back to last-leg.
  $effect(() => {
    if (_focusedLegKey && !basketLegs.find(l => l.key === _focusedLegKey)) {
      untrack(() => { _focusedLegKey = null; });
    }
  });
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
    // `pairedCount`/"+wing" netted-margin annotation removed —
    // OrderTicket's margin preview is deliberately NAKED now (no
    // `paired_legs` sent), matching what the real submit-time preflight
    // enforces. Showing a netted figure here was a display bug: the
    // wing is only placed on parent fill via template attach, so at
    // submit time the broker only ever sees the naked parent order.
    return { required, available, after, afterCls, shortfall, isCashMode };
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
    if (Array.isArray(_modalMargin?.blocked) && _modalMargin.blocked.length > 0) {
      const b = _modalMargin.blocked[0] ?? null;
      return { level: 'err',  text: '⚠ ' + String(b?.reason || 'preview blocked').slice(0, 60), detail: String(b?.fix || '') };
    }
    // Market-hours check — operator: "when exchange is resolved
    // showing the message is appropriate". So the warning only fires
    // when we KNOW which segment the order is going to.
    //
    // Fixed 2026-10-22 (live incident: MCX order showed "Market closed
    // — order will queue" while MCX was genuinely open) — this used to
    // re-derive its OWN exchange resolution from `_pickedExchange ||
    // exchange` (the raw, unresolved prop/picker hint) plus a narrow
    // tradingsymbol-pattern regex as a fallback for bare MCX roots.
    // Both are weaker than the resolution OrderTicket already does:
    // PageHeaderActions (and several other callers) pass a generic
    // 'NSE' exchange HINT for a blank/bare-underlying ticket — see
    // OrderTicket.svelte's `_resolvedExchange` comment, which exists
    // specifically to correct that wrong hint by looking the resolved
    // symbol up in the instruments cache first. A bare commodity root
    // ("GOLD", "CRUDEOIL" with no FUT/CE/PE suffix — the normal shape
    // on the Chain tab, and for any ticket opened before the operator
    // picks a specific contract) also never matched the old regex,
    // which required a suffix tail. The badge above (`_mktBadgeOpen`/
    // `_mktBadgeExchShell`) already resolves correctly for both tabs —
    // Ticket mirrors OrderTicket's own `_resolvedExchange`-based belief,
    // Chain resolves per-leg via the focused leg/instrument cache — so
    // this now reads the SAME single source instead of guessing again
    // with a weaker signal. `_mktBadgeExchShell` falls back to the
    // literal 'Market' placeholder when nothing has resolved yet (no
    // symbol at all); treat that placeholder as unresolved and suppress
    // the notice, matching the old "don't guess" behavior.
    // A draft never reaches the broker at all — it never "queues" and
    // is never "deferred", so this notice (which only makes sense for a
    // real order) would be actively misleading while Draft is on
    // (operator: a draft order "does not need market to be open").
    if (_sharedDraftMode) return null;
    try {
      const exch = _mktBadgeExchShell;
      if (!exch || exch === 'Market') return null;  // exchange not known — no warning
      const isMcx = exch === 'MCX' || exch === 'NCO';
      if (!_mktBadgeOpen) {
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

  /**
   * Audit fix — a basket leg's `limit > 0` was the ONLY submit gate,
   * which a stale or operator-typed positive number satisfies just as
   * easily as a real one. Mirrors OrderTicket's `_depthPending`
   * invariant (a quote must have arrived at least once) applied
   * per-leg instead of to one ticket. Option legs (strike + optType,
   * stamped by OptionChainTab's +CE/+PE path) are gated on
   * `quoteArrived` — latched true once OptionChainTab's chain-quotes
   * poll resolves ANY bid/ask for that strike's side. Deliberately
   * does NOT require confirmed real market depth (`depthAvail`) —
   * illiquid strikes that only ever report last-traded-price must
   * still be submittable, same as OrderTicket's `_lastQuote` check,
   * which doesn't require real depth either. Futures legs carry no
   * strike/optType and have no live depth-quote map in the Chain tab
   * (operator fills `limit` manually there), so they keep the
   * original limit-only gate.
   */
  const _legNeedsDepth = (/** @type {any} */ leg) =>
    !(Number(leg.limit) > 0) || (leg.strike != null && leg.optType != null && !leg.quoteArrived);

  // Gates the shared common-action Submit button for the basket path —
  // mirrors `_ticketDepthPending` (which only covers the single-ticket
  // path, `basketLegs.length === 0`). 2026-10-21 — while Draft mode is
  // on, NO leg in the basket ever reaches `placeBasket()` (see
  // submitBasket()'s own Draft-mode branch, which either drafts a leg
  // via live-tick pricing or refuses-and-reports it — never a real
  // broker call), so no leg needs to wait on a live quote/depth at all
  // in that mode. When Draft is off, unchanged: every leg must have a
  // resolved `leg.limit` + (for options) an arrived chain quote.
  const _basketDepthPending = $derived.by(() =>
    !_sharedDraftMode && basketLegs.length > 0 && basketLegs.some(_legNeedsDepth)
  );

  /** Submit every leg in the shell basket via POST /api/orders/basket. */
  // Resolves effective basket execution mode.
  //
  // 2026-11-05 fix (CRITICAL money-safety audit finding) — sim/replay/
  // shadow used to pass straight through unchanged (the `mode !==
  // 'paper' && mode !== 'live'` early return), matching neither this
  // function's own name nor OrderTicket's `_resolveInitialMode`/mode-
  // sync effect, which normalise those three to 'paper' first. Worse,
  // the resulting value was sent as `mode: basketMode2` in the request
  // body, but `BasketOrderRequest` had no such field at all — msgspec
  // silently dropped it, and the backend computed its own `eff_mode`
  // purely from server-side settings with zero visibility into what
  // the navbar actually showed. Net effect: a Chain basket submitted
  // while the navbar showed SIM or REPLAY placed real LIVE broker
  // orders on prod. Normalising here AND threading the result to the
  // backend (`basket_order_handler`'s own `data.mode` handling) closes
  // both the client-side and server-side halves of the gap.
  async function _resolveBasketMode(/** @type {string} */ mode) {
    if (mode === 'sim' || mode === 'replay' || mode === 'shadow') return 'paper';
    if (mode !== 'paper' && mode !== 'live') return 'paper';
    try {
      const live = await fetchLiveStatus();
      if (live && (live.branch !== 'main' || live.paper_trading_mode === true)) return 'paper';
    } catch { /* keep caller's pick */ }
    return mode;
  }

  async function submitBasket() {
    if (basketSubmitting || !basketLegs.length) return;
    basketSubmitting = true; basketResultMsg = '';

    // 2026-10-21 — Draft mode: route each draftable leg to the
    // server-backed payoffDrafts store instead of the broker, priced
    // from the latest live tick (same `liveSnap()` read OrderTicket's
    // own single-leg draft path uses), rather than `leg.limit` which
    // may be stale. `onSubmit` is deliberately never called for a
    // drafted leg — same as OrderTicket's own draft path.
    //
    // CRITICAL FIX (2026-10-21, correctness bug found via advisor
    // review) — a leg is "draftable" only when `lotSize > 1` (F&O,
    // pre-existing rule that predates this fix and is NOT being
    // expanded here — equity legs, only reachable via the Ticket tab's
    // own +Basket, still never draft). The bug: a leg whose lotSize is
    // missing/undefined/≤1 for ANY reason — equity, OR an F&O leg
    // whose lot-size resolution hasn't completed yet (cold instruments
    // cache; confirmed on dev the cache can take minutes to warm) —
    // used to fall through to the REAL `placeBasket` call below even
    // though `_sharedDraftMode` was on and the Submit button read "Add
    // to Payoff." Fix: while `_sharedDraftMode` is on, this function
    // NEVER reaches `placeBasket` at all — every non-draftable leg is
    // refused-and-reported (same per-leg `fails`-array/toast/sticky-
    // banner shape the real-submission path below already uses for a
    // broker rejection) and left IN the basket (not stripped) so the
    // operator can retry once e.g. the lot-size cache warms, or
    // explicitly turn Draft off to place an equity leg for real.
    if (_sharedDraftMode) {
      const draftLegs   = basketLegs.filter(_isDraftableLeg);
      const skippedLegs = basketLegs.filter(l => !_isDraftableLeg(l));

      let draftOk = 0;
      /** @type {string[]} */
      const draftFails = [];
      for (const leg of draftLegs) {
        try {
          const liveLtp = liveSnap(String(leg.sym || '').toUpperCase())?.ltp;
          const px = (liveLtp != null && liveLtp > 0)
            ? Number(liveLtp)
            : (Number(leg.limit) > 0 ? Number(leg.limit) : null);
          const signedQty = ((Number(leg.lots) || 1) * (Number(leg.lotSize) || 1))
            * (leg.side === 'SELL' ? -1 : 1);
          const id = await payoffDrafts.add({
            symbol:     leg.sym,
            exchange:   leg.exchange || 'NFO',
            qty:        signedQty,
            avg_cost:   px,
            underlying: rootOf(leg.sym, leg.exchange || 'NFO'),
            account:    legAccountOf(leg) || null,
          });
          if (id != null) draftOk++;
          else draftFails.push(`${leg.side} ${leg.sym}: draft save failed`);
        } catch (e) {
          draftFails.push(`${leg.side} ${leg.sym}: ${/** @type {any} */ (e)?.message || 'draft save failed'}`);
        }
      }
      /** @type {string[]} */
      const skipFails = skippedLegs.map(leg =>
        `${leg.side} ${leg.sym}: not drafted — equity or lot size not resolved yet`
      );
      const allFails = [...draftFails, ...skipFails];

      // Strip only the legs that were actually ATTEMPTED as drafts
      // (success or failure of the draft save itself — a failed draft
      // save is surfaced via toast and the operator re-adds the leg
      // manually, same pre-existing behaviour). Skipped (non-
      // draftable) legs stay in the basket untouched.
      basketLegs = skippedLegs;
      basketSubmitting = false;

      if (!allFails.length) {
        toast.success(`${draftOk} leg${draftOk > 1 ? 's' : ''} added to payoff`, { timeoutMs: 3000 });
        basketResultMsg = `${draftOk} added to payoff`;
      } else if (draftOk > 0) {
        basketResultMsg = `${draftOk} added to payoff — ${allFails.length} not placed: ${allFails[0]}`;
        toast.warning(`Draft: ${draftOk} added, ${allFails.length} not placed — see basket`, { timeoutMs: 5000 });
      } else {
        basketResultMsg = `Nothing drafted — ${allFails[0]}`;
        toast.warning(`Draft: 0 added — ${allFails.length} leg${allFails.length > 1 ? 's' : ''} not placed`, { timeoutMs: 5000 });
      }
      // NEVER fall through to the real broker-submission path below
      // while Draft mode is on — that is the entire point of this fix.
      return;
    }

    // Validate that every leg has a limit price AND (for option legs) a
    // live chain quote has actually arrived for its own strike — see
    // `_legNeedsDepth` for the full rationale.
    const missingQuote = basketLegs.find(_legNeedsDepth);
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

    // `mode` is sent at the TOP LEVEL of the request body (see
    // `placeBasket(groups, basketMode2)` below) — `BasketGroup` itself
    // has no `mode` field, so a per-group `mode` key here was always
    // silently dropped by the backend (2026-11-05 fix; see that call's
    // own comment for the full story).
    const groups = [...byAcct.entries()].map(([acct, entries]) => ({
      account: acct,
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
        // Sprint 2b (SPRINT2_LAYER_INTEGRATION.md §3/§4.4) — per-leg
        // origin tag for the /orders/basket path. Backend's BasketLeg
        // schema needs a matching `source` field to persist this onto
        // the AlgoOrder row (tracked separately); harmless no-op if
        // the field isn't yet recognized server-side.
        source:           'basket',
        // `template_id` deliberately omitted (T3.3, bracket-removal) —
        // backend's `BasketLeg.template_id` still accepts it but
        // silently drops it before persist; nothing should send it
        // any more. See CLAUDE.md's "Order Ticket / Bracket-template
        // re-wire" history.
      })),
    }));

    const total = basketLegs.length;
    /** @type {Set<number>} */
    const failedIdx = new Set();
    let ok = 0;
    /** @type {string[]} */
    const fails = [];

    try {
      const resp = await placeBasket(groups, basketMode2);
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
      // 2026-11-05 live fix (operator: "chase status is taking time to
      // display. it can show chase immediately after showing the
      // order") — same as OrderTicket.svelte's own submit() call site;
      // basket chase is a shared per-request toggle (`_sharedChase`),
      // not per-leg, so one kick after a successful live submit covers
      // every chase-eligible leg in this basket.
      if (basketMode2 === 'live' && _sharedChase && ok > 0) {
        const { kickChasePoll } = await import('$lib/stores');
        kickChasePoll();
      }
    } catch (e) {
      // 2026-11-05 fix (HIGH-severity audit finding) — a client-side
      // submit timeout is NOT a known rejection (same D3 bug class
      // OrderTicket's own submit() already guards against). Before
      // this, `placeBasket` resolved `null` on timeout, every leg's
      // `r` read undefined, and the generic catch below marked EVERY
      // leg failed and kept them all in the basket for "retry" — even
      // though the server had very often already placed them (each
      // live leg pays for preflight + a broker margin call +
      // _start_live_chase's own confirm window, which alone can run up
      // to 15s). A resubmit on that false "all rejected" banner could
      // duplicate every already-placed live order. Render "still
      // processing" instead, leave `basketLegs` COMPLETELY untouched
      // (no failedIdx entries at all — nothing is known to have
      // failed), and return before the ok/fails-driven banner logic
      // below, which assumes a real, known outcome.
      if (/** @type {any} */ (e)?.name === 'TimeoutError') {
        basketSubmitting = false;
        basketResultMsg = 'Still processing — check Orders before resubmitting';
        _stickyResultMsg = 'Basket submit still processing — check /orders before resubmitting';
        _stickyResultLevel = 'warn';
        if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
        _stickyResultTimer = null;
        toast.warning('Basket still processing — check Orders before resubmitting', { timeoutMs: 8000 });
        return;
      }
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
      // visible record, and the operator had no way to know which
      // legs were live + needed monitoring. Now: surface a persistent sticky
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
      // Trading-critical toast (2026-09-30) — the sticky banner above
      // already carries this, but it's scoped to this modal; a toast
      // means the operator notices even after the modal closes.
      toast.warning(`Basket: ${ok}/${total} placed, ${fails.length} rejected`, { timeoutMs: 5000 });
    } else {
      basketResultMsg = `Failed: ${fails[0]}`;
      _stickyResultMsg = `All ${total} legs rejected — no orders placed`;
      _stickyResultLevel = 'err';
      if (_stickyResultTimer) clearTimeout(_stickyResultTimer);
      _stickyResultTimer = setTimeout(() => { _stickyResultMsg = ''; _stickyResultLevel = ''; }, 8000);
      toast.error(`Basket failed — ${fails[0]}`.slice(0, 80), { timeoutMs: 5000 });
    }
  }

  function handleParsedOrder(/** @type {any} */ _props) {
    _setActiveTab('ticket');
  }

  // Focus-trap anchor — bound to .oes-modal so Tab cycles stay inside.
  let _modalEl      = $state(/** @type {HTMLElement|null} */ (null));

  // Click-outside-to-close backdrop region (2026-10-01 regression fix).
  // The overlay itself stays `pointer-events: none` (shared
  // canonical-modal-overlay default) so fixed chrome painted BELOW it in
  // z-index — the navbar (`.algo-navbar`, hamburger included) and the
  // `.page-header` action strip — stays clickable while this modal is
  // open, exactly like every other canonical-modal-overlay consumer
  // (ChartModal, ActivityLogModal). Only this narrow band — the visible
  // backdrop strip between the navbar's bottom edge and this modal's own
  // sheet panel (`.canonical-modal-panel` is a full-bleed sheet with zero
  // margin, so that band IS the entire clickable backdrop at every
  // viewport width) — gets its own `pointer-events: auto` plate carrying
  // the click-outside-to-close behaviour via normal event bubbling up to
  // the overlay's `onclick={onClose}` (bubbling isn't blocked by an
  // ancestor's `pointer-events: none` — only direct hit-testing on that
  // ancestor is). Measured from live layout rather than hardcoding the
  // navbar height, since the mobile navbar can wrap to a second row.
  let _navGapTop    = $state(0);
  let _navGapHeight = $state(0);

  function _measureNavGap() {
    if (inline || typeof document === 'undefined') return;
    const navEl = document.querySelector('.algo-navbar');
    const navBottom = navEl ? navEl.getBoundingClientRect().bottom : 0;
    const panelTop = _modalEl ? _modalEl.getBoundingClientRect().top : navBottom;
    _navGapTop = navBottom;
    _navGapHeight = Math.max(0, panelTop - navBottom);
  }

  function _oesFocusables() {
    return /** @type {HTMLElement[]} */ (
      Array.from(_modalEl?.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      ) ?? [])
    );
  }

  onMount(() => {
    // One-time cleanup of the stale Bracket preference key (T3.3,
    // bracket-removal) — an old browser session may still carry
    // `ramboq_templ_pref_v1` from before Bracket was removed; nothing
    // reads it any more, so drop it so it doesn't persist forever.
    try { localStorage.removeItem(_TEMPL_PREF_KEY_STALE); } catch { /* private mode etc */ }

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
        // Re-measure after the portal + focus settle — _modalEl's real
        // rendered position is only reliable post-portal.
        _measureNavGap();
      }, 0);
      _measureNavGap();
      window.addEventListener('resize', _measureNavGap);
    }

    const onKey = (/** @type {KeyboardEvent} */ e) => {
      // Escape is handled by the layerStack coordinator (pushLayer
      // below) instead of here — this used to call onClose()
      // unconditionally for ANY Escape, which is exactly the bug that
      // let a nested dropdown's Escape close the whole modal instead
      // of just the dropdown (2026-09-30 stacking-defect audit, Wave A).
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
    // HIGH 1: prevent background page scroll while the modal is open.
    // Only applies in overlay mode (not inline) — inline renders as a
    // flat page element so scroll should remain enabled.
    const wasInline = inline;
    // Capture whatever value was already there BEFORE this modal locks
    // it, and restore THAT value (not an unconditional '') on close —
    // same pattern as DefaultSizeButton.svelte's fullscreen scroll-lock.
    // Without this, closing SymbolPanel wiped a lock a still-open
    // full-screen card had already applied (2026-09-30 stacking-defect
    // audit, Wave A): operator opens a fullscreen card (locks scroll),
    // then the order modal from inside it, then closes just the order
    // modal — the page could scroll again even though the fullscreen
    // card was still open underneath.
    const _prevOverflow = document.body.style.overflow;
    if (!wasInline) {
      document.body.style.overflow = 'hidden';
    }

    // Escape-stack coordinator (layerStack.js) — SymbolPanel is its own
    // dismissible layer. Replaces the former unconditional window Escape
    // listener (which closed the WHOLE modal on any Escape, including
    // one meant for a nested dropdown inside it — e.g. Select.svelte /
    // SymbolSearchInput.svelte's own pickers, now migrated to push
    // their own nested layer so their Escape is consumed first).
    const _layerId = inline ? null : pushLayer(onClose);

    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (!wasInline) {
        window.removeEventListener('resize', _measureNavGap);
      }
      if (_wlToastTimer) { clearTimeout(_wlToastTimer); _wlToastTimer = null; }
      if (!wasInline) {
        document.body.style.overflow = _prevOverflow;
      }
      popLayer(_layerId);
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
  // `allowedTabs` (optional prop, additive) filters this down to a subset
  // of tabs — null/omitted (every caller as of this prop's addition)
  // leaves the full 4-tab set unchanged. $derived rather than a plain
  // const so a host that mutates `allowedTabs` after mount (not done by
  // any caller today) would still reflect correctly.
  const TABS = $derived.by(() => {
    const base = ORDER_TABS.map(t => ({
      ...t,
      ...(t.id === 'chain'   ? { dot: 'var(--c-long)', activeTxt: 'var(--c-long)', activeBorder: 'var(--c-long)', activeBg: 'rgba(74,222,128,0.14)'  } :
          t.id === 'ticket'  ? { dot: 'var(--c-action)', activeTxt: 'var(--c-action)', activeBorder: 'var(--c-action)', activeBg: 'var(--c-action-14)'  } :
                               { dot: 'var(--algo-sky)', activeTxt: 'var(--algo-sky)', activeBorder: 'var(--algo-sky)', activeBg: 'rgba(125,211,252,0.14)' }),
    }));
    if (Array.isArray(allowedTabs) && allowedTabs.length) {
      return base.filter(t => allowedTabs.includes(t.id));
    }
    return base;
  });

  // Effective OrderTicket props — shell-level props forwarded to the ticket.
  // When the picker row's quick-qty / quick-price fields have a value, they
  const _ticketProps = $derived({
    symbol, exchange, side, action, qty, product, orderType, variety,
    price, trigger, lotSize, accounts, account, orderId,
    currentQty, avgCost, unrealizedPnl, onAddToBasket, initialDraftId,
  });

  // ── Shared CHASE/DRAFT row (2026-10-21 consolidation; BRACKET
  // removed T3.3 — bracket-removal) ───────
  // Promotes DRAFT from OrderTicket-local state to shell-level so one
  // row (below .oes-picker) can render CHASE + DRAFT identically on
  // the Ticket tab (Chain renders its own CHASE/DRAFT copies inside
  // OptionChainTab's Expiry toolbar). `draftMode` itself binds
  // straight into <OrderTicket> (see its mount below); Chain's own
  // submitBasket() reads this flag directly.
  let _sharedDraftMode = $state(false);

  // CRITICAL FIX (2026-10-21, correctness bug found via advisor review)
  // — fail-open #1: unlike the OLD OrderTicket-local `_draftMode` (which
  // naturally reset per mount/symbol), `_sharedDraftMode` is PERSISTENT
  // shell state that survives a symbol switch WITHIN the same Ticket
  // tab session. Scenario: operator turns Draft on viewing an option,
  // then switches the symbol to an equity on the SAME tab — the Draft
  // toggle disappears (`_draftToggleVisible`/`_ticketState.showDraftToggle`
  // flips false, since OrderTicket's own `showDraftToggle = !isEquity
  // && ...`) but `_sharedDraftMode` itself stayed true. The Submit
  // button still read "Add to Payoff" (`_submitLabel` checks
  // `_sharedDraftMode` unconditionally) while OrderTicket's own
  // `submit()` check (`if (draftMode && !isEquity)`) was now FALSE —
  // `isEquity` flipped true — so it fell through to the REAL
  // paper/live placement branch instead. Mirrors the existing "close-
  // order-clears-template" effect pattern already in OrderTicket.svelte
  // (auto-clear the moment the condition that made a control
  // applicable stops holding) — scoped to the Ticket tab only; Chain's
  // Draft toggle never disappears (`_draftToggleVisible` is
  // unconditionally true there) and Chain's own per-leg draftability
  // is enforced directly inside submitBasket(), not via this kind of
  // clear.
  $effect(() => {
    if (_activeTab === 'ticket' && _sharedDraftMode && !_ticketState.showDraftToggle) {
      _sharedDraftMode = false;
    }
  });

  // CRITICAL FIX (2026-10-21, correctness bug, verification question #2)
  // — fail-open #2: SymbolPanel can stay MOUNTED across successive
  // ticket opens — both MarketPulse.svelte's `openTicket()` and the
  // derivatives page's equivalent reassign `ticketProps = {...}` (a
  // plain prop-value swap) rather than closing (`ticketProps = null`)
  // then reopening, so the host's `{#if allowOrders && ticketProps}`
  // guard never goes false in between and this component instance
  // (and its local `_sharedDraftMode` $state) is never destroyed and
  // recreated. If the operator had Draft on for a PRIOR order and then
  // clicks a draft row to promote a DIFFERENT draft to real
  // (`_openTicketFromDraftRow()` / the derivatives page's
  // `executeDraft()`, both of which set a fresh `initialDraftId`),
  // `_sharedDraftMode` would otherwise carry over from the prior
  // order's session. Reset it the moment a NEW, DIFFERENT draft begins
  // being promoted — never on an unchanged `initialDraftId` (an
  // ordinary re-render) or a null one (an unrelated, non-promotion
  // ticket open — the equity auto-clear effect above handles "is this
  // new symbol even draftable" separately). Implemented inside
  // SymbolPanel itself (not in either host) so both call sites get the
  // fix for free with no changes to either host file.
  // intentional: seeds from initialDraftId prop once; $effect below re-syncs on subsequent prop changes
  // svelte-ignore state_referenced_locally
  let _lastDraftPromoteId = $state($state.snapshot(initialDraftId));
  $effect(() => {
    if (initialDraftId !== _lastDraftPromoteId) {
      _lastDraftPromoteId = initialDraftId;
      if (initialDraftId != null) _sharedDraftMode = false;
    }
  });

  // DRAFT toggle handler — Bracket used to force itself to None the
  // moment Draft turned on (mutual exclusion). That relationship no
  // longer exists (T3.3, bracket-removal); this is now a plain setter.
  function _onSharedDraftToggleChange(/** @type {boolean} */ checked) {
    _sharedDraftMode = checked;
  }

  // Market badge — Chain tab. Ticket's own badge value is mirrored up
  // from OrderTicket via the onTicketStateChange payload (_ticketState.
  // mktOpen/_ticketState.mktExch below) rather than recomputed here —
  // OrderTicket's own `_isOpen`/`_mktOpenForExchange` stay untouched
  // (an existing spec asserts against their exact source text). Chain
  // always resolves a real per-leg exchange (no bare-underlying
  // ambiguity the way Ticket can have), so it gets its own independent
  // resolution: focused/last basket leg's own exchange first, then the
  // resolved LTP symbol's instrument-cache exchange, then NFO.
  const _chainMktExch = $derived.by(() => {
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    if (_focusedLeg?.exchange) return String(_focusedLeg.exchange).toUpperCase();
    const inst = getInstrument(_ltpSym);
    if (inst?.e) return String(inst.e).toUpperCase();
    // Bare-root rescue (2026-10-29 live incident: "MCX closed" shown
    // while MCX was open, no leg staged). `_ltpSym` can end up as a
    // bare commodity/currency root ("GOLD") when `resolveUnderlying-
    // Tradingsymbol`'s own `findNearestFuture` lookup misses — that
    // wrapper returns only the tradingsymbol STRING, discarding the
    // `.exchange` field `resolveUnderlying()` already correctly
    // computed (it defaults MCX commodities/CDS currencies to their
    // real exchange even on its own stub-fallback branch — see
    // resolveUnderlying.js). `getInstrument()` can never hit on a bare
    // root (never a real instrument row), so the lookup above always
    // misses in that case, and this used to fall straight through to
    // the hardcoded 'NFO' default. Re-resolve the FULL object directly
    // instead of re-deriving the same lookup a second, lossy way.
    const root = _parseRoot(_localSymbol) || _localSymbol;
    // 2026-11-04 live incident — a successful basket submit resets
    // `basketLegs=[]` AND `_localSymbol=''` (see submitBasket()'s
    // post-submit cleanup), so `root` is genuinely empty here, not just
    // "unresolved". The old code fell through `resolveUnderlying(null,
    // ...)` straight to the hardcoded 'NFO' default regardless — so
    // EVERY basket submit (MCX included) immediately showed "Market
    // closed" the instant NSE's own hours closed (15:30), even seconds
    // after a genuinely successful MCX order placed while MCX itself
    // was still open (MCX trades until 23:30). An empty root means
    // nothing is picked at all, which is exactly the case `_modalNotice`
    // already knows how to suppress correctly (`if (!exch || exch ===
    // 'Market') return null`) — so return '' here instead of guessing,
    // rather than inventing a false NFO/NSE answer.
    if (!root) return '';
    const resolved = resolveUnderlying(root, findNearestFuture);
    return String(resolved?.exchange || 'NFO').toUpperCase();
  });
  let _chainMktOpen = $state(false);
  function _recomputeChainMktOpen() {
    _chainMktOpen = segmentOpenForExchange(_chainMktExch);
  }
  // Re-check on every exchange resolution change (new leg, symbol
  // swap) and on a 30s visibleInterval tick (mirrors OrderTicket's own
  // `_mktTimer` convention exactly — session-boundary flips even when
  // nothing else triggers a re-render).
  $effect(() => {
    void _chainMktExch;
    _recomputeChainMktOpen();
  });
  /** @type {(() => void) | null} */
  let _chainMktTimer = null;
  onMount(() => {
    _chainMktTimer = visibleInterval(() => {
      _recomputeChainMktOpen();
    }, 30_000);
  });
  onDestroy(() => { _chainMktTimer?.(); });

  // Resolved exchange/open fields for the active tab's SYMBOL — Ticket
  // mirrors OrderTicket's own belief (via `_ticketState.mktOpen`/
  // `mktExch`, which is `_resolvedExchange`-based: it looks the
  // resolved symbol up in the instruments cache FIRST, falling back to
  // the caller's `exchange` prop hint only on a cache miss — see
  // OrderTicket.svelte's `_resolvedExchange` comment for why that
  // ordering matters, PageHeaderActions passes a generic 'NSE' hint
  // that's wrong for MCX commodities). Chain uses the independent
  // per-leg resolution above (`_chainMktExch`/`_chainMktOpen`). Chart
  // is excluded from rendering the shared row entirely (see the
  // `.oes-chase-row` guard above) but OrderTicket keeps computing in
  // the background regardless of active tab, so these two values stay
  // valid to read from the Chart tab too.
  //
  // 2026-10-22: these two are NOT rendered as a visible badge here —
  // the fixed, symbol-agnostic three-pill market-status badge (M / MCX
  // / Eq, per operator: "market open, mcx open and non-mcx open") moved
  // to the shared page header (`AlgoTimestamp.svelte`), since it's
  // page-wide ambient information, not something specific to having an
  // order ticket open. These two values remain the single source of
  // truth `_modalNotice` below reads for its SYMBOL-SPECIFIC "market
  // closed — order will queue" warning, instead of `_modalNotice`
  // re-deriving its own (and, until a prior fix, buggier) exchange
  // resolution — keep them wired to `_modalNotice`; do not let them go
  // unused.
  const _mktBadgeOpen = $derived(_activeTab === 'chain' ? _chainMktOpen : !!_ticketState.mktOpen);
  const _mktBadgeExchShell = $derived(_activeTab === 'chain' ? _chainMktExch : (_ticketState.mktExch || 'Market'));

  // Draft toggle visibility — Chain always eligible (never equity,
  // never 'modify'); Ticket mirrors OrderTicket's own `showDraftToggle`
  // (depends on `isEquity`/`action`, both internal to OrderTicket).
  const _draftToggleVisible = $derived(
    _activeTab === 'chain' ? true : !!_ticketState.showDraftToggle
  );

  // A leg that will route to payoffDrafts instead of the broker (see
  // submitBasket() below) never needs a live chain quote/depth — used
  // both to gate `_basketDepthPending` above and, inside submitBasket()
  // itself, to strip draftable legs out of `basketLegs` before the
  // existing real-submission logic (missingQuote check, byAcct
  // building, etc.) runs. Equity legs (only ever staged via the Ticket
  // tab's own +Basket) are never draftable, mirroring OrderTicket's own
  // `draftMode && !isEquity` gate.
  const _isDraftableLeg = (/** @type {any} */ leg) =>
    _sharedDraftMode && Number(leg.lotSize || 1) > 1;
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
  {#if !inline}
    <!-- Click-catcher plate — see _measureNavGap() comment above. Carries
         no handler of its own; a click on it bubbles normally to this
         overlay's own onclick={onClose} above (pointer-events:none on
         the overlay only blocks IT from being a hit-test target, it
         doesn't block bubbling from an auto descendant). -->
    <div class="oes-click-catcher" aria-hidden="true"
         style="top:{_navGapTop}px; height:{_navGapHeight}px"></div>
  {/if}
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
    <!-- --ch-padding override removed in the 2026-10 CardHeader/grid
         consistency pass (Part C), then partially RE-ADDED here
         (2026-11-03, operator: "orders and chart modal header button
         placement is not in sync... X button right alignment"). That
         pass made this header inherit the shared body-level
         `--ch-padding: 0` default so it would match every ordinary
         CardHeader-based card. Measured (Playwright getBoundingClientRect,
         desktop 1400px): with the zero-padding default, `.oes-close` sat
         1px right / 0px top from `.oes-modal`'s corner — flush — while
         ChartModal's `.cm-close` and NavBreakdown's `.ps-bd-close` (both
         built on `.canonical-modal-header`, app.css) sit ~14.6px right /
         ~5.6-6.6px top, the shared convention three other popups already
         use. This override applies the SAME canonical padding
         (0.35rem 0.85rem) to just this one CardHeader instance, scoped to
         non-inline (real modal) use only — the global
         `--ch-padding: 0` default in (algo)/+layout.svelte is untouched
         and still governs every ordinary in-page card header. Re-measured
         after this change: `.oes-close` lands at the same ~14.6px/~5.6px
         inset as the other two. Known, accepted tradeoff: the header's
         own height grows from ~23.4px to ~34.6px (same growth ChartModal's
         sibling fix already accepted) and `.oes-modal-name`'s
         `algo-label-decor` left-border bar (sized via `.ch-left`'s
         `align-self: stretch`, which stretches to the CONTENT box, not
         the padding box) now stops short of the header's visual top/
         bottom edges by about half the new vertical padding — the same
         decor-bar inset ChartModal and NavBreakdown never show because
         neither of their titles carries that decor class. Not fixed here;
         flagged for a future pass if the inset becomes visually
         objectionable. `.oes-modal-name`'s amber color/letter-spacing is
         unchanged (confirmed intentional, preserved below). -->
    <div class:oes-modal-hdr-canonical={!inline}>
      <CardHeader showControls={false} onRefresh={_refreshAll}>
        {#snippet left()}
          <span class="oes-modal-name algo-label-decor">
            <!-- Order-slip / receipt glyph — matches the page-header Order
                 button (PageHeaderActions) and orders/+page.svelte's own
                 .oc-entry-icon. Restored 2026-10-13 (operator: "keep the
                 icon back to orders modal and page") after a same-day
                 earlier removal — the page's own icon was never removed,
                 so the modal was the odd one out, not vice versa. -->
            <svg class="oes-modal-name-icon" width="13" height="13" viewBox="0 0 16 16"
                 fill="none" stroke="currentColor" stroke-width="1.5"
                 stroke-linecap="round" aria-hidden="true">
              <rect x="3.2" y="2" width="9.6" height="12" rx="1.2" />
              <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" stroke-width="1.4" />
            </svg>
            Orders
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
            {#if showRefresh}
              <RefreshButton onClick={_refreshAll} loading={_headerRefreshing} label="order ticket" />
            {/if}
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

    <!-- Tab strip — suppressed when the host page renders its own
         (tabsExternal). Operator clicks still flow back via the
         two-way bound `activeTab` either way. -->
    {#if !tabsExternal}
    <div class="oes-tabs" style="border-bottom: 1px solid rgba(255,255,255,0.08);">
      <!-- Wrapper div (2026-09-30, alignment fix) — Svelte scoped styles
           can't reach AlgoTabs' own component root from here, so
           `flex-shrink: 0` is applied to this wrapper instead; keeps the
           TICKET/CHAIN/CHART cluster intact (never compressed) when
           `.oes-tabs` wraps onto a second line on narrow viewports. -->
      <div class="oes-tabs-inner">
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
          compact={true}
        />
      </div>
      <!-- Vertical divider (2026-09-30, operator: "add vertical after
           chart tab to differentiate the label values showing after") —
           separates the TICKET/CHAIN/CHART tab strip from the LTP/CHASE
           label-value pairs now that both clusters sit left-aligned and
           close together. -->
      <span class="oes-tabs-divider" aria-hidden="true"></span>
      {#if _activeTab === 'cli'}
        <!-- CLI tab (2026-10, operator: "when cli is selected in order
             modal or page, ltp and chase should not be displayed. in its
             place, trader guide button for link should be displayed")
             — LTP/CHASE are meaningless for a CLI statement (no single
             symbol/chase-target is "active" the way Ticket/Chain have
             one); the Guide link takes their place in the same row
             instead. `.oes-cli-guide-link` reuses `.oes-tab-ltp`'s own
             row-height-parity recipe (--ctl-h floor) so it doesn't grow
             the tab row — operator: "trader guide should not increase
             the height of the header height. it should be fit within
             header vertically centered." -->
        <a href="/automation/guide" title="CLI grammar guide" class="btn-secondary oes-cli-guide-link">Trader Guide</a>
      {:else}
        {#if _ltp != null && _ltp > 0}
          <span class="oes-tab-ltp {_ltpFlash.classOf(_ltpSym)}"><span class="oes-tab-ltp-label">LTP</span>{priceFmt(_ltp)}</span>
        {/if}
      {/if}
    </div>
    {/if}

    <!-- Picker row — Account · Exchange filter · Symbol — placed AFTER
         the tab strip (2026-10, operator request: "move account,
         exchange, symbol elements below the tabs"). Still lives at the
         shell level so both tabs (Chain / Order ticket) read from the
         same values — only its vertical position relative to the tabs
         changed, not its scope. Mirrors ChartWorkspace's toolbar pattern
         (type filter + combo). Renders in EVERY mount where the tabs
         are visible (modal, inline /orders, even when headerless
         because /orders uses headerless to drop the title chip but
         still wants the picker) — EXCEPT the CLI tab (2026-10, operator:
         "in orders cli remove account, exchange, symbol, console
         details. just keep trader guide button for link and cli for
         order entry") — CLI statements carry account/symbol as their
         own text parameters (`order(account=..., symbol=..., ...)`),
         so this picker is redundant there and was reading as leftover
         chrome rather than a relevant control. -->
    {#if _activeTab !== 'cli'}
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
        <div class="oes-exch-filter">
          <Select
            options={[
              { value: 'ALL', label: 'All'  },
              { value: 'NSE', label: 'NSE'  },
              { value: 'BSE', label: 'BSE'  },
              { value: 'NFO', label: 'NFO'  },
              { value: 'MCX', label: 'MCX'  },
              { value: 'NCO', label: 'NCO'  },
            ]}
            value={_exchFilter}
            onValueChange={(v) => { _exchFilter = String(v); }}
            placeholder="All"
            ariaLabel="Exchange filter" />
        </div>
        <div class="oes-sym-pick">
          <SymbolSearchInput
            value={_localSymbol}
            type={_symType}
            exchangeFilter={_exchFilter}
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
        {#if pickerSuffix}
          {@render pickerSuffix()}
        {/if}
      </div>
    {/if}

    <!-- Shared CHASE / DRAFT row (2026-10-21 consolidation; BRACKET
         removed T3.3 — bracket-removal). One row, directly below the
         account/exchange/symbol picker, below OrderTicket's own
         (display:none-while-Chain-active) body. Order left-to-right:
         CHASE, DRAFT. Excluded on Chart (2026-10-22, operator: "chase,
         mcx open, draft and brackets are not applicable in chart tab")
         in addition to the pre-existing CLI exclusion — neither control
         places or manages a real order from the Chart tab, which is
         read-only (OrderTicket keeps running underneath with
         display:none for its own margin-preflight effect).
         CHASE is excluded on Chain too (T3.3) — it now renders inside
         OptionChainTab's own Expiry toolbar row instead, so this shared
         row only ever shows CHASE for the Ticket tab. -->
    {#if _activeTab !== 'cli' && _activeTab !== 'chart'}
      <div class="oes-chase-row">
        <!-- CHASE — moved verbatim from .oes-tabs (unchanged markup).
             2026-10-23 fix: hidden entirely (not just greyed) while
             Draft is on — operator report: "when submitting draft
             order, chase is getting invoked and chase status is
             shown." Investigated: no backend invocation ever occurs
             (both OrderTicket's single-leg draft path and
             SymbolPanel's own `submitBasket()` return before any
             chase/broker call; confirmed live — zero mode='draft' rows
             exist on prod or dev, and every recent real chase has
             mode='live'). The confusion was this cluster staying
             visually present (greyed, non-interactive) during Draft
             mode instead of disappearing — a draft has zero chase
             semantics, so there is nothing for it to show here. -->
        {#if _chaseEnabled && !_sharedDraftMode && _activeTab !== 'chain'}
          <span class="oes-chase-cluster">
            <span class="oes-common-chase-label on" bind:this={_chaseInfoAnchor}
                  role="button" tabindex={0} aria-expanded={_chaseInfoOpen}
                  title="Chase is active — click for details"
                  onclick={() => { _chaseInfoOpen = !_chaseInfoOpen; }}
                  onkeydown={_chaseInfoKeydown}>CHASE</span>
            <InfoHint popup hideButton id="chase-info-hint" anchor={_chaseInfoAnchor} bind:open={_chaseInfoOpen}
              panel title="Chase"
              text="Chase is automatic for every limit order (on for LIMIT/SL order types) — re-quotes the price each interval until filled or max attempts is reached. Wait time between re-quotes depends on the aggressiveness picker beside it: <b>Low</b> — 30s, patient, eases into the spread (30 attempts max). <b>Med</b> — 20s, balanced (20 attempts max). <b>High</b> — 10s, crosses the spread fast (10 attempts max)." />
            <span class="oes-chase-agg-gap">
              <ChaseAggPicker value={_sharedChaseAgg} onChange={_setSharedChaseAgg} variant="panel" />
            </span>
          </span>
        {/if}

        <!-- Market-status badge group — REMOVED 2026-10-22. Moved from
             here (a single symbol-specific pill, then redesigned into
             three fixed symbol-agnostic pills) into the shared page
             header (`AlgoTimestamp.svelte`'s "M"/"MCX"/"Eq" pills,
             `data-testid="algo-ts-market-badge*"`) since market status
             is page-wide information, not something specific to having
             an order ticket open. Once it rendered in the header
             (visible on every page regardless of whether a ticket is
             open), keeping a second copy here became redundant. -->

        <!-- BRACKET (TemplateBar) removed entirely — T3.3, bracket-
             removal. See CLAUDE.md's "Order Ticket / Bracket-template
             re-wire" history for the feature this used to render. -->

        <!-- DRAFT — markup + class names moved verbatim from
             OrderTicket.svelte so existing Playwright selectors keyed
             on `.ot-draft-toggle`/`.ot-draft-label` keep matching. Now
             bound to the shared `_sharedDraftMode` instead of a local
             OrderTicket-only $state.
             Chain-tab exclusion (2026-11-03, operator: "move draft to
             days before expiry row") — Chain now renders its OWN copy
             inside `<OptionChainTab>`'s Expiry toolbar (see the
             `draftMode`/`onDraftToggleChange` props passed to it
             below), so this shared-row copy is suppressed specifically
             while Chain is active to avoid a second, duplicate DRAFT
             control on screen at once. The Ticket tab is unaffected —
             it has no Expiry/dte row to move the toggle onto, so it
             keeps rendering this copy exactly as before. -->
        {#if _draftToggleVisible && _activeTab !== 'chain'}
          <label class="ot-draft-toggle"
                 title={_sharedDraftMode
                   ? 'Draft ON — submit adds this leg to the payoff chart without placing an order'
                   : 'Draft OFF — submit routes to broker normally'}>
            <input type="checkbox" checked={_sharedDraftMode}
                   onchange={(e) => _onSharedDraftToggleChange(/** @type {HTMLInputElement} */ (e.currentTarget).checked)} />
            <span class="ot-draft-label" class:on={_sharedDraftMode}>DRAFT</span>
          </label>
          <InfoHint popup panel title="Draft"
            text="When ON, Submit adds this leg to the session payoff chart instead of placing a real broker order — nothing is sent to the exchange. Useful for sketching a strategy before committing." />
        {/if}
      </div>
    {/if}

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
            avgCost={_ticketProps.avgCost ?? avgCost}
            unrealizedPnl={_ticketProps.unrealizedPnl ?? unrealizedPnl}
            onAddToBasket={addToBasket}
            basketMode={basketMode}
            accountHidden={true}
            symbolHidden={true}
            symType={_symType}
            actionsHidden={actionsHidden || showCommonActions}
            fundsHidden={true}
            refreshKey={_ticketBump}
            triggerSubmit={triggerSubmit + _modalTriggerSubmit}
            triggerBasket={triggerBasket}
            hostManagesEsc={true}
            mode={_sharedMode}
            bind:chase={_sharedChase}
            bind:chaseAgg={chaseAgg}
            standalone={false}
            defaultChase={_sharedChase}
            defaultChaseAgg={_sharedChaseAgg}
            modeChaseHidden={true}
            bind:draftMode={_sharedDraftMode}
            onMarginUpdate={showCommonActions ? _onMarginUpdate : null}
            onValidationChange={(err) => { _ticketValidationErr = err; }}
            onTicketStateChange={(s) => { _ticketState = s; }}
            initialDraftId={_ticketProps.initialDraftId ?? null}
            {onSubmit}
            {onClose} />
      </div>

      {#if _activeTab === 'chart'}
        <!-- Chart tab — embedded ChartWorkspace for the current symbol.
             No ticket form, no submit buttons, no basket controls — only
             the chart canvas. The bottom panel (ActivityLogSurface) and
             the .oes-bottom-panel remain visible outside the tab body,
             exactly as they do for Ticket and Chain tabs. -->
        <div class="oes-chart-tab-body">
          {#if _localSymbol}
            <ChartWorkspace
              symbol={_localSymbol}
              exchange={_pickedExchange || exchange || ''}
              mode="live"
              compact={true}
              showHeader={false} />
          {:else}
            <p class="oes-chart-placeholder">Select a symbol to view chart</p>
          {/if}
        </div>
      {/if}

      {#if _activeTab === 'chain'}
        <!-- OptionChainTab's own basket state is migrated to the shell.
             The tab receives the shared basket as props and calls back
             into the shell to mutate it. Its own placeBasket is unused
             when routed through onSubmitBasket. -->
        <OptionChainTab
          bind:this={_chainTabRef}
          symbol={_localSymbol}
          account={_sharedAccount || account}
          onAccountChange={_onAccountChange}
          bind:spreadMaxPctOverride={_sharedSpreadMaxPctOverride}
          draftMode={_sharedDraftMode}
          onDraftToggleChange={_onSharedDraftToggleChange}
          chaseAgg={_sharedChaseAgg}
          onChaseAggChange={_setSharedChaseAgg}
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

      {#if _activeTab === 'cli'}
        <!-- CLI tab (Sprint B, 2026-10, Phase 2) — order-scoped mount of
             the generic agent-CLI grammar (agents.js/AgentCliEditor via
             OrderCliTab), restricted to order(...)/place_order(...)
             statements only. Shared by /orders' inline mount (this same
             SymbolPanel instance, headerless) and the order-ticket
             modal — see tabs.js's ORDER_TABS comment for the full
             restoration history. -->
        <OrderCliTab
          mode={_sharedMode === 'live' ? 'live' : 'paper'}
          isDemo={_isDemo}
          onSubmit={(payload) => { onSubmit?.(payload); }}
        />
      {/if}

    </div>

    <!-- BRACKET (TemplateBar) on-fill preview chip + cap-warning strip
         removed entirely — T3.3, bracket-removal. See CLAUDE.md's
         "Order Ticket / Bracket-template re-wire" history for the
         feature this used to render. -->

    <!-- Shell-level basket bar — visible from any tab when legs exist,
         except the Chart tab (shows only the ChartWorkspace) and the
         CLI tab (Sprint B, 2026-10 — its own order path is the typed
         statement, not the shared basket). Per-leg pills (B/S · sym ·
         lots stepper · × remove) sit on the left; Clear / Submit on
         the right. Same shape as the chain tab's in-tab basket so the
         operator sees what's pending from any tab without flipping
         back to Chain. -->
    {#if basketLegs.length > 0 && _activeTab !== 'chart' && _activeTab !== 'cli'}
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
                 click/keydown to set this leg as "focused" (drives
                 `_chainMktExch`'s per-leg market-open badge). Keyboard
                 handler is already attached; the listitem role is
                 intentional for AT. -->
            <span class="oes-basket-pill oes-basket-pill-{leg.side === 'BUY' ? 'buy' : 'sell'} oes-basket-pill-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                  class:is-disabled={basketSubmitting}
                  class:is-focused={_isFocused}
                  role="listitem"
                  title={_isFocused
                    ? 'Focused — this leg\'s exchange drives the market-open badge. Click another pill to swap.'
                    : 'Click to focus this leg. Click × to remove.'}
                  onclick={(e) => {
                    // Don't focus when the click bubbled from a child
                    // interactive element (×, stepper, account select,
                    // limit input). The target check covers all of
                    // them — buttons + inputs + selects.
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
                <span class="oes-basket-pill-qty">× {leg.lotSize} = {qtyFmt((leg.lots || 1) * leg.lotSize)}</span>
              {/if}
              <!-- Per-leg limit price — editable so operator can submit
                   outside market hours when bid/ask hasn't pre-filled. -->
              <span class="oes-basket-pill-limit-wrap"
                    title={_legNeedsDepth(leg)
                      ? (!(Number(leg.limit) > 0)
                          ? 'Set a limit price to submit'
                          : 'Waiting for market depth (bid/ask) for this strike')
                      : `Limit ₹${leg.limit}`}>
                <span class="oes-basket-pill-limit-prefix">₹</span>
                <input type="number"
                       class="oes-basket-pill-limit"
                       class:oes-basket-pill-limit-warn={_legNeedsDepth(leg)}
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
              <!-- BRACKET per-leg chip, WING pill, and per-leg override
                   editor removed entirely — T3.3, bracket-removal. See
                   CLAUDE.md's "Order Ticket / Bracket-template re-wire"
                   history for the feature this used to render. -->
              <button type="button" class="oes-basket-pill-remove"
                      title="Remove leg from basket"
                      disabled={basketSubmitting}
                      onclick={() => removeBasketLeg(i)}>×</button>
            </span>
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
         works when there's pending content. Excluded on the CLI tab
         (Sprint B, 2026-10) — OrderCliTab renders its own submit
         button via AgentCliEditor's own "Place order" affordance; this
         shared footer's BUY/SELL submit would otherwise fire against
         whatever's in the (hidden) OrderTicket instead. -->
    {#if showCommonActions && !actionsHidden && _activeTab !== 'chart' && _activeTab !== 'cli'}
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
                      : `${_kind}: ₹${aggFmtMargin(_marginInfo.required)} vs ${_avlKey} ₹${aggFmtMargin(_marginInfo.available ?? 0)}`}>
              {#if _marginInfo.error}
                ⚠ {_marginInfo.error}
              {:else if _marginInfo.loading}
                Computing {_isCash ? 'cost' : 'margin'}…
              {:else}
                <span class="oes-margin-pill-row">
                  <span class="oes-margin-pill-key">{_reqKey}</span>
                  <span class="oes-margin-pill-val">{aggFmtMargin(_marginInfo.required)}</span>
                </span>
                {#if _marginInfo.available != null}
                  <span class="oes-margin-pill-sep">·</span>
                  <span class="oes-margin-pill-row">
                    <span class="oes-margin-pill-key">{_avlKey}</span>
                    <span class="oes-margin-pill-val">{aggFmtMargin(_marginInfo.available)}</span>
                  </span>
                {/if}
              {/if}
            </span>
          {/if}
          <!-- Ticket-only side preview label REMOVED (2026-09-29, operator:
               "remove the text before order submit in order ticket") —
               it duplicated what the Submit button's own label already
               says (e.g. "Submit · CLOSE/BUY 90"). Side is still set via
               the ticket body's own SideToggle (BUY/SELL pills, always
               rendered regardless of actionsHidden — see OrderTicket.svelte's
               ot-row-knobs); nothing functional was lost, only the
               redundant preview text. -->
          <button type="button" class="oes-common-submit"
            class:oes-common-submit-buy={_submitFlavor === 'buy'}
            class:oes-common-submit-sell={_submitFlavor === 'sell'}
            class:oes-common-submit-basket={basketLegs.length > 0 || _submitFlavor === 'basket'}
            class:oes-common-submit-narrow={basketLegs.length > 0}
            class:oes-common-submit-depth-pending={_ticketDepthPending || _basketDepthPending}
            title={basketLegs.length > 0
              ? (_basketDepthPending ? 'Waiting for market depth (bid/ask) for a leg' : (_sharedDraftMode ? 'Add legs to payoff chart (no broker order)' : 'Submit'))
              : (_activeTab === 'chain'
                  ? 'Add legs via +CE / +PE on the chain rows first'
                  : _ticketOwnSubmitBusy
                    ? (_ticketState.pending ? 'Still processing — check Orders' : 'Placing…')
                    : _ticketDepthPending
                      ? 'Waiting for market depth (bid/ask) for this strike'
                      : 'Place the order')}
            disabled={basketSubmitting
                      || (basketLegs.length === 0 && _activeTab === 'chain')
                      || _ticketOwnSubmitBusy
                      || _ticketDepthPending
                      || _basketDepthPending}
            onclick={async () => {
              if (basketLegs.length > 0) {
                // Global basket submit — fires from any tab whenever there
                // are staged legs (Chain's own +CE / +PE mechanism).
                // Chain-only pre-submit spread gate (operator: "it should
                // be in a loop until the conditions are satisfied before
                // placing the order") — runPreSubmitGate() is a no-op
                // (resolves true immediately) when no leg has a wing/
                // offset configured. No impact on the Ticket tab's own
                // submit path below.
                if (_activeTab === 'chain' && _chainTabRef?.runPreSubmitGate) {
                  const proceed = await _chainTabRef.runPreSubmitGate();
                  if (!proceed) return;
                }
                submitBasket();
              } else if (_activeTab === 'ticket') {
                _modalFireSubmit();
              }
            }}>{basketSubmitting
                ? 'Placing…'
                : (basketLegs.length > 0
                    ? _submitLabel
                    : _ticketOwnSubmitBusy
                      ? (_ticketState.pending ? 'Processing…' : 'Placing…')
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
         Console · Ticks · System · News). Placed BELOW the common
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
        <OrderBook
          statusFilter="open"
          onSymbolClick={() => {}}
        />
      </div>
    {/if}

  </div>
</div>

<!-- Chart modal — opened by the chart-icon button in the header.
     Rendered at top-level so it sits above the SymbolPanel overlay.
     ChartModal overrides the shared canonical-modal-overlay z-index
     (var(--z-command) = 10500, SymbolPanel's own tier) to 10600 — see
     ChartModal.svelte's own z-index comment. Only mounted when the
     operator clicks the chart button and a symbol is set. -->
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
    /* NOTE (2026-10-01, fixes a regression from the 2026-09-30
       stacking-defect audit Wave A): this rule used to override
       canonical-modal-overlay's shared `pointer-events: none` to `auto`
       here, making THIS entire full-viewport overlay a click target so
       its own `onclick={onClose}` would fire. That reintroduced a bug a
       separate same-day fix (mobile hamburger drawer) had just closed:
       the overlay (z-index var(--z-command)=10500) then sat ABOVE the
       fixed `.algo-navbar` (z-index var(--z-nav)=50) and intercepted
       every click meant for it — including the hamburger button — for
       as long as this modal was open. Fix: leave `pointer-events: none`
       here (the shared canonical-modal-overlay default, same as
       ChartModal/ActivityLogModal) so the navbar stays clickable, and
       do click-outside-to-close via the narrow `.oes-click-catcher`
       plate below instead — see its own comment and the
       `_measureNavGap()` comment in <script>. */
  }
  /* Click-outside-to-close plate — spans only the visible backdrop band
     between the navbar's bottom edge and this modal's own sheet panel
     (computed at runtime by _measureNavGap(); the panel is a full-bleed
     sheet with zero margin, so that band is the ENTIRE backdrop at every
     viewport width — there is no "outside the panel" region below the
     navbar). `pointer-events: auto` makes only this plate hit-testable;
     a click on it bubbles normally to the overlay's own
     onclick={onClose} (ancestor `pointer-events: none` doesn't block
     bubbling from an `auto` descendant, only direct hits on the
     ancestor itself). No background — purely a click target, the
     overlay's own background still supplies the visible dim. */
  .oes-click-catcher {
    position: fixed;
    left: 0;
    right: 0;
    pointer-events: auto;
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
  /* Operator: "order ticket window is wider than viewport mobile
     sometimes" — the modal's own width is already correctly capped
     (96vw via .ot-modal / 100% here), but nothing clipped the X axis,
     so an occasional wide child (a long basket pill, a symbol-search
     row, a knobs row that doesn't get to wrap in time) could still
     visually bleed past the modal edge and force page-level horizontal
     scroll. Each inner row already manages its own overflow-x
     (auto-scroll or flex-wrap) independently — this is a containment
     backstop on the outer shell only, not a replacement for those.
     Alignment fix (2026-09-30) — `.oes-tabs` now sets `flex-wrap: wrap`
     precisely so its children drop to a second line instead of relying
     on THIS backstop to hide an off-screen overflow. Breakpoint note:
     this file uses 720px here; OptionChainTab.svelte uses 760px for a
     related mobile rule — out of scope to reconcile in this pass, flagged
     for a future cleanup. */
  @media (max-width: 720px) {
    .oes-modal { overflow-x: hidden; }
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
    gap: 0.35rem;
    /* 2026-10-13 — border-left + left padding, matching every other
       header label's decoration (.ch-title, .mp-bucket-label). Kept
       the deliberate amber color — "writing surface / operator
       action" surface, not the generic neutral default. Icon was
       briefly removed earlier the same day, then restored — operator:
       "keep the icon back to orders modal and page". (orders/
       +page.svelte's own matching .oc-entry-label/.oc-entry-icon were
       removed entirely in a later pass, along with that page's whole
       Order Entry header — no longer a live sibling to compare
       against.)
       Reusability sweep Part A (same day): border-left/padding-left,
       plus (2026-10, later pass) display/align-items/align-self for
       the full-header-height stretch, all moved to the shared
       `.algo-label-decor` class (app.css); this rule still owns
       `color` and everything else above.

       2026-11-03, operator: "in orders modal, the left vertical bar
       to order label should not be present" — overrides OUT just the
       bar/inset half of `.algo-label-decor` (border-left, padding-
       left, border-radius) right below, scoped to this one label.
       The shared class is otherwise untouched (still used by
       `.ch-title`/`.mp-bucket-label` elsewhere) and this rule keeps
       its `display: inline-flex; align-items: center; align-self:
       stretch` — the icon+text layout and the full-header-height
       stretch still need those, only the visible bar is gone. */
  }
  .oes-modal-name.algo-label-decor {
    border-left: none;
    padding-left: 0;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
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
    /* Takes remaining row space (largest slot) but capped — was
       unbounded flex-grow, stretching across the full header width on
       wide viewports (2026-09-30, operator: "don't expand the width of
       the symbol to the full width. limit the max width"). */
    flex: 1 1 0;
    min-width: 0;
    max-width: 20rem;
  }
  .oes-sym-pick :global(.ssi-wrap) { width: 100%; }
  .oes-sym-pick :global(.ssi-input) { width: 100%; min-width: 0; }
  /* Root-cause fix (mobile overflow) — SymbolSearchInput's own
     `.ssi-drop` results panel anchors `left: 0` relative to its
     `.ssi-wrap`, with a fixed `min-width: 14rem` for readability.
     In THIS picker row the Account (5.5rem) + Exchange (5rem)
     fixed-width selects precede the symbol input, so `.ssi-wrap`
     sits well right of the row's own left edge — on mobile (320–
     390px confirmed) a left-anchored 14rem-wide dropdown spills
     past the viewport's right edge, its tail end clipped by
     `.canonical-modal-panel`'s `overflow: hidden`, reading as the
     modal itself "overflowing" on the right. `.ssi-wrap`'s right
     edge, by contrast, always sits close to the row's own right
     edge (the symbol input is the last flex-grow slot before the
     optional `pickerSuffix`), so right-anchoring keeps the full
     dropdown on-screen at every width without capping its readable
     content — it grows leftward into the picker row instead of
     rightward off it. Scoped to `.oes-sym-pick` only: other
     SymbolSearchInput consumers (ChartWorkspace's `.cw-picker`,
     which places the input near the row's LEFT edge) keep the
     default left-anchor, where it's the correct choice. */
  .oes-sym-pick :global(.ssi-drop) { left: auto; right: 0; }
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
  /* Scoped CardHeader padding override for the real modal header only
     (2026-11-03) — see the markup comment above the `<CardHeader>` call
     for the full measurement/rationale. Sets the SAME padding value
     `.canonical-modal-header` (app.css) uses, on just this one
     CardHeader instance, so `.oes-close` lands at the same inset as
     ChartModal's `.cm-close` / NavBreakdown's `.ps-bd-close` instead of
     the global `--ch-padding: 0` body-level default (which stays correct
     for every ordinary in-page CardHeader card and is not touched). */
  .oes-modal-hdr-canonical {
    --ch-padding: 0.35rem 0.85rem;
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
  /* Account picker — narrower so the picker row fits Account +
     Symbol on a single line at common viewport widths. 10 chars of
     account code fit at 5.5rem in monospace. */
  .oes-account-pick {
    flex-shrink: 0;
    width: 5.5rem;
  }
  .oes-account-pick :global(.rbq-select-trigger) { width: 100%; }
  /* Exchange filter dropdown — sits before the symbol input, replaces
     the old static chip. Narrow (5rem) so it doesn't crowd the row. */
  .oes-exch-filter {
    flex-shrink: 0;
    width: 5rem;
  }
  .oes-exch-filter :global(.rbq-select-trigger) { width: 100%; }
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
  /* .oes-exch was the old static exchange chip — removed in favour of
     the exchange filter dropdown (.oes-exch-filter). */
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
     card-control buttons (FullscreenButton, DefaultSizeButton). */
  .oes-chart-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    background: rgba(34, 211, 238, 0.08);
    border: 1px solid rgba(34, 211, 238, 0.30);
    border-radius: 3px;
    color: var(--c-info);
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    padding: 0;
  }
  .oes-chart-btn:hover:not(:disabled) {
    background: var(--c-info-14);
    color: var(--algo-cyan-text);
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
    background: var(--close-btn-danger-bg);
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
  .oes-close:hover { background: var(--close-btn-danger-bg-hover); }

  /* Tab strip wrapper — padding + flex-shrink only; AlgoTabs renders the
     button row via the global .algo-tab rules in app.css. */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 0.4rem;
    flex-shrink: 0;
    align-items: center;
    /* Alignment fix (2026-09-30) — no wrap + .oes-modal's
       `overflow-x: hidden` backstop meant the LTP/CHASE/L-M-H picker
       cluster could silently clip off-screen on mobile instead of
       dropping to a second line. */
    flex-wrap: wrap;
    /* 2026-10-21 (shared-row re-wire) — CHASE moved out of this row
       into the new shared CHASE/BRACKET/DRAFT row below .oes-picker.
       Before this fix, CHASE's own --ctl-h floor (see
       .oes-common-chase-label below) was what kept this row from
       visibly shrinking whenever LTP had nothing to show (_ltp null —
       e.g. no symbol picked yet). Explicit floor here reproduces the
       EXACT height-parity regression fix documented in the
       .oes-cli-guide-link comment above, now that CHASE can no longer
       hold the line for it. */
    min-height: var(--ctl-h, 1.7rem);
  }
  /* See the `<div class="oes-tabs-inner">` wrapper note at the markup
     site — flex-shrink:0 has to land on a real element here since
     scoped styles can't reach AlgoTabs' own component root. */
  .oes-tabs-inner {
    flex-shrink: 0;
  }
  /* Divider between the TICKET/CHAIN/CHART tab strip and the LTP/CHASE
     label-value pairs (2026-09-30, operator: "add vertical after chart
     tab to differentiate the label values showing after"). */
  .oes-tabs-divider {
    width: 1px;
    align-self: stretch;
    margin: 0.3rem 0.5rem;
    /* Themed amber tint (2026-09-30) — was plain white/gray, out of
       step with this surface's amber accent family (e.g. TemplateBar's
       border tones). */
    background: rgba(251, 191, 36, 0.18);
    flex-shrink: 0;
  }
  .oes-tab-ltp {
    /* Left-aligned (2026-09-30, operator: "even ltp and chase label
       value pairs should be aligned to left") — supersedes the
       2026-09-29 margin-left:auto that right-anchored this + CHASE.
       See .oes-common-chase-label below for the matching change. */
    padding: 0 0.4rem;
    font-size: var(--fs-lg);
    font-variant-numeric: tabular-nums;
    /* A3 (2026-09 audit) — stale rgba(200,216,240,α); alpha preserved. */
    color: color-mix(in srgb, var(--algo-slate) 85%, transparent);
    font-weight: 600;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    /* Row-height parity fix — the TICKET/CHAIN/CHART tab buttons carry
       an enforced min-height (app.css .algo-tab, var(--toolbar-h)) but
       this sibling had none, rendering visibly thinner in the same
       .oes-tabs row. Floor (not a rigid height) matching this modal's
       shared --ctl-h control-height token. */
    min-height: var(--ctl-h, 1.7rem);
    box-sizing: border-box;
  }
  .oes-tab-ltp-label {
    font-size: var(--fs-sm);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--c-action);
    font-weight: 700;
  }
  /* CLI-tab Trader Guide link — replaces LTP/CHASE in the same .oes-tabs
     row (2026-10). Height matched to `.oes-tab-ltp`/`.oes-common-chase-
     label`'s own --ctl-h floor (27.2px), NOT the tab buttons' own
     22px compact height — live getBoundingClientRect measurement
     (2026-10-15 regression fix) confirmed the TRUE .oes-tabs row height
     on Ticket/Chart (28.2px) is governed by CHASE/LTP's --ctl-h floor,
     not by .algo-tab's shorter compact height. An earlier attempt at
     this fix matched the tab buttons instead (22px), which shrank the
     whole row by ~5px only on the CLI tab — operator: "when cli is
     selected the header height is getting reduced." Matching --ctl-h
     here keeps the row height IDENTICAL across all four tabs, which is
     what "trader guide should not increase the height of the header
     height" actually meant — not matching the tab buttons specifically. */
  .oes-cli-guide-link {
    display: inline-flex;
    align-items: center;
    min-height: var(--ctl-h, 1.7rem);
    box-sizing: border-box;
    padding: 0 0.6rem;
    font-size: var(--fs-sm);
    white-space: nowrap;
    text-decoration: none;
    color: var(--algo-slate);
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
    /* Alignment fix (2026-09-30) — this rule used to right-anchor the
       basket result message with an auto left margin, against this
       app's convention that elements flow left-aligned except the
       header / card-button-group. That rule is removed so the message
       flows inline after the pills instead. */
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
    color:        var(--c-long);
    border-color: rgba(74,222,128,0.35);
    /* -22 (stronger tier), not the faint -14 rest-state alpha most
       other -14 consumers use — operator: "the order info card
       background the text is not very user friendly". -14 read as
       low-contrast against the dark modal background; -22 is this
       app's existing "needs more visual weight" tier (already used
       for hover/emphasis elsewhere, e.g. --close-btn-danger-bg-hover),
       reused here rather than inventing a new alpha value. */
    background:   var(--c-long-22);
  }
  .oes-basket-pill-sell {
    color:        var(--c-short);
    border-color: rgba(248,113,113,0.55);
    background:   var(--c-short-22);
  }
  .oes-basket-pill-side {
    font-weight: 900;
    padding-right: 0.18rem;
    border-right: 1px solid currentColor;
    opacity: 0.9;
  }
  .oes-basket-pill-sym {
    font-weight: 800;
    color: var(--algo-blue-tint, #f1f7ff);
  }
  .oes-basket-pill-sym :global(.leg-root)   { color: var(--algo-blue-tint, #f1f7ff); font-weight: 700; }
  .oes-basket-pill-sym :global(.leg-month)  { color: rgba(148,163,184,0.70); font-weight: 400; }
  .oes-basket-pill-sym :global(.leg-strike) { color: var(--algo-blue-tint, #f1f7ff); }
  .oes-basket-pill-sym :global(.leg-sep)    { opacity: 0.35; }
  /* Same amber-chip treatment as QtyInput.svelte's `.ot-lots-step`
     (the ticket form's own lot stepper) — reused, not reinvented, so
     the +/− read as one consistent "true button" affordance across
     both surfaces. Operator (2026-09-29): these were borderless/
     transparent at rest and didn't read as buttons in the basket
     pill row. Sized down from `.ot-lots-step`'s 2rem to fit the
     compact pill context. */
  .oes-basket-pill-step {
    width: 1.15rem;
    height: 1.15rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 3px;
    border: 1px solid rgba(251, 191, 36, 0.45);
    background: rgba(251, 191, 36, 0.10);
    color: var(--c-action);
    font-weight: 800;
    font-family: monospace;
    line-height: 1;
    padding: 0;
    cursor: pointer;
    flex: 0 0 auto;
    box-sizing: border-box;
    touch-action: manipulation;
    -webkit-user-select: none;
    user-select: none;
  }
  .oes-basket-pill-step:hover:not(:disabled) {
    background: var(--c-action-22, rgba(251, 191, 36, 0.22));
    border-color: rgba(251, 191, 36, 0.75);
  }
  .oes-basket-pill-step:disabled { opacity: 0.35; cursor: not-allowed; }
  .oes-basket-pill-lots {
    min-width: 1rem;
    text-align: center;
    font-weight: 800;
    color: var(--algo-blue-tint, #f1f7ff);
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
    color: var(--algo-sky);
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
  /* BRACKET per-leg chip / editor CSS removed entirely — T3.3,
     bracket-removal. */

  .oes-basket-pill.is-disabled { opacity: 0.55; }
  /* Focused pill — the basket leg `_chainMktExch` resolves the
     market-open badge against. Slate-blue outer ring so the operator
     can see at a glance "this pill is driving the badge". The whole
     pill is clickable to set focus; hovering shows the affordance via
     cursor: pointer + a faint ring. */
  .oes-basket-pill { cursor: pointer; transition: box-shadow 0.12s; }
  .oes-basket-pill:hover:not(.is-focused) {
    box-shadow: inset 0 0 0 1px rgba(165, 180, 252, 0.32);
  }
  .oes-basket-pill.is-focused {
    box-shadow: 0 0 0 1.5px rgba(165, 180, 252, 0.45);
  }
  .oes-basket-pill-limit-wrap {
    display: inline-flex;
    align-items: center;
    gap: 0.05rem;
    margin-left: 0.3rem;
  }
  .oes-basket-pill-limit-prefix {
    font-size: var(--fs-md);
    /* A3 (2026-09 audit) — stale rgba(200,216,240,α); alpha preserved. */
    color: color-mix(in srgb, var(--algo-slate) 60%, transparent);
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
  /* Mobile drops .oct-root's forced full-stretch too (2026-09-30 root-
     cause fix — see the matching --chain-depth-h:auto exception just
     above). Forcing `flex: 1 1 0` unconditionally meant .oct-root
     always stretched to fill the ENTIRE .oes-body height on a short
     mobile viewport regardless of its actual content, and whatever
     leftover slack existed between the grid's real content height and
     that forced full height had to show up as blank space SOMEWHERE
     inside .oct-root — which is why flipping .chain-grid-wrap's own
     flex value (OptionChainTab.svelte) across earlier sessions only
     ever relocated the same gap rather than fixing it. `.oes-ticket-
     body` is deliberately left OUT of this override — the Ticket
     tab's own depth ladder already handles its mobile collapse
     differently and isn't part of this fix. */
  @media (max-width: 720px) {
    .oes-body :global(.oct-root) {
      flex: 0 1 auto;
    }
  }
  /* REGRESSION FIX (2026-09-30, mobile "Chain takes full viewport")  —
     OptionChainTab.svelte's own mobile max-height cap on
     .chain-grid-wrap was removed the same day (see that file's
     .chain-grid-wrap comment) on the theory that the MODAL path's own
     .oes-body (fixed-height `.canonical-modal-panel`, flex:1 1 auto +
     min-height:0 + overflow-y:auto one level up) already bounds the
     grid, so the per-grid cap was redundant there — true, verified live
     (popup chain-grid-wrap stayed ~140-290px with or without the cap,
     submit/chase/bottom-panel all stayed visible either way).
     That reasoning does NOT hold for INLINE mode (`.oes-modal-inline`,
     used by the /orders page's Order Entry card): that mode is
     DELIBERATELY content-driven with no fixed-height ancestor (see
     `.oes-modal.oes-modal-inline`'s own comment — an explicit min-height
     there previously broke the page's natural scroll, so it was
     switched to grow with content on purpose). With no outer cap AND no
     per-grid cap, a real NIFTY chain's ~100+ rows rendered at full
     content height (measured live: .chain-grid-wrap 2780px,
     .bucket-card-entry 3022px at 375x667/812), shoving the Chases /
     Order Activity cards further down the PAGE than the operator could
     reasonably scroll to find them. Restoring a cap scoped to inline
     mode only (never touching the modal path) fixes this without
     reverting the modal-side improvement. 16rem matches the original
     (pre-audit) cap value deliberately, not a new tuning — measured
     live at 375x667 (the shortest supported phone height): a larger
     cap (tried 20rem) left .bucket-card-activity's top edge landing
     exactly AT the viewport fold (1px of it visible); 16rem leaves a
     real, if modest, sliver of it on-screen without any scroll at
     all, while still giving ~6-7 strike rows (vs the old 5) before the
     operator needs to scroll inside the grid itself. */
  @media (max-width: 720px) {
    .oes-modal-inline .oes-body :global(.chain-grid-wrap) {
      max-height: 16rem;
    }
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
    color: var(--algo-violet);
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
  .oes-event-kind-preflight_ok    { color: var(--algo-dim); }
  .oes-event-kind-preflight_block { color: var(--c-short); }
  .oes-event-kind-cancel          { color: var(--algo-dim); }
  .oes-event-kind-postback        { color: var(--algo-violet); }
  /* Agent-sourced event kinds — violet/pink palette so "rule fired"
     is instantly distinguishable from "manual order" events.         */
  .oes-event-kind-agent_fire          { color: #e879f9; }
  .oes-event-kind-agent_match         { color: #d946ef; }
  .oes-event-kind-agent_action_success { color: #a855f7; }
  .oes-event-kind-agent_action_error  { color: #f472b6; }
  .oes-event-kind-agent_skipped       { color: var(--algo-dim); }
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
  .oes-status-cancelled           { background: rgba(148,163,184,0.1); color: var(--algo-dim); border: 1px solid rgba(148,163,184,0.3); }
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
    /* Shared control-height / control-value-font tokens (order-ticket
       visual-consistency pass) — mirrors the same declaration on
       OrderTicket.svelte's .ot-modal. Scoped to THIS row, not the
       whole .oes-modal — the row is the only place these tokens are
       meant to apply (side selector / submit / basket icon / margin
       chip below). Declaring it any higher (e.g. on .oes-modal itself)
       would also resize every OTHER Select in the shell that has
       nothing to do with this fix: the header account/exchange
       pickers, the Chain-tab controls, and the per-leg account Select
       inside basket pills — all out of this pass's scope. */
    --ctl-h: 1.7rem;
    --ctl-fs: var(--fs-md);
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
    min-height: var(--ctl-h, 1.7rem);
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
    height: var(--ctl-h, 1.7rem);
    padding: 0 0.7rem;
    border-radius: 3px;
    border: 1px solid color-mix(in srgb, var(--text-med) 22%, transparent);
    background: rgba(15, 25, 45, 0.45);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--text-lo);
    font-style: italic;
    letter-spacing: 0.02em;
    box-sizing: border-box;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sr-only {
    position: absolute;
    width: 1px; height: 1px;
    margin: -1px; padding: 0;
    overflow: hidden;
    clip: rect(0,0,0,0);
    border: 0;
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
    /* A3 (2026-09 audit) — this scoped rule (Svelte-hashed, higher
       specificity than app.css's global :global(.oes-common-chase-label))
       had its OWN separate stale rgba(200,216,240,0.55) — same alpha as
       the .cell-muted token, so reused directly. */
    color: var(--algo-slate-muted);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    /* Left-aligned (2026-09-30, operator: "even ltp and chase label
       value pairs should be aligned to left") — REVERSES the
       2026-09-29 "keep chase right aligned" decision documented in
       git history. margin-left:auto removed; CHASE now sits inline
       right after LTP (or right after the tab strip when LTP isn't
       rendered — e.g. Chain tab staging a multi-leg basket with no
       single-symbol LTP), same as every other sibling in .oes-tabs. */
    margin-left: 0.5rem;
    /* Row-height parity fix (same rationale as .oes-tab-ltp above) —
       this label had no explicit height at all, just font-size +
       letter-spacing, so it rendered thinner than the tab buttons in
       the same .oes-tabs row. */
    display: inline-flex;
    align-items: center;
    min-height: var(--ctl-h, 1.7rem);
    box-sizing: border-box;
    /* 2026-10-17 — the label itself is now the tooltip trigger (the
       removed "(i)" InfoHint chip's click target moved onto this text). */
    cursor: pointer;
  }
  .oes-common-chase-label.on { color: var(--c-action); }
  /* .oes-tabs has gap:0 (each child owns its own spacing) — the CHASE
     label had none, so the L/M/H picker sat flush against it. Operator
     (2026-09-29): "there should be space between chase and l/m/h". */
  .oes-chase-agg-gap { margin-left: 0.4rem; display: inline-flex; }

  /* Shared CHASE/DRAFT row (2026-10-21 consolidation; BRACKET removed
     T3.3 — bracket-removal). */
  .oes-chase-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding: 0.2rem 0.4rem;
  }
  /* Market badge group — REMOVED 2026-10-22 (.oes-mkt-badge/.oes-mkt-pill/
     .oes-mkt-open/.oes-mkt-closed). Moved to the shared page header
     (AlgoTimestamp.svelte's .ats-mkt-* classes) — see the markup-removal
     comment above where this group used to render. */
  /* DRAFT toggle — class names + styling duplicated verbatim from
     OrderTicket.svelte (now removed there) so existing Playwright
     selectors keyed on `.ot-draft-toggle`/`.ot-draft-label` keep
     matching without edits. Magenta/purple palette matches the
     draft-row identity in CandidateLegRow.svelte. */
  .ot-draft-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
    user-select: none;
  }
  .ot-draft-toggle input[type="checkbox"] {
    accent-color: var(--algo-violet);
    width: 0.85rem;
    height: 0.85rem;
    cursor: pointer;
  }
  .ot-draft-label {
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
  .ot-draft-label.on {
    background: rgba(192,132,252,0.18);
    border-color: rgba(192,132,252,0.55);
    color: var(--algo-violet);
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
    border-radius: 3px;
    font-family: var(--font-numeric);
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
    border-radius: 3px;
    font-family: var(--font-numeric);
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
    /* A3 (2026-09 audit) — was flat hex #e2e8f0; now var(--algo-slate). */
    color: var(--algo-slate);
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
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 600;
    border: 1px solid transparent;
    background: rgba(126, 151, 184, 0.10);
    color: var(--algo-slate);
    white-space: nowrap;
    /* Alignment fix (2026-09-30) — at 320px width the Req/Avail text had
       no truncation, so it could paint over the Side/Submit buttons in
       the same row. `min-width: 0` is required alongside `overflow:
       hidden` because flex items default to `min-width: auto`, which
       would otherwise keep the pill at its content width regardless of
       the ellipsis rule (`.oes-common-row > .oes-margin-pill` already
       sets `min-width: 0` for that specific context; set it here too so
       the base class is safe in any flex context). */
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* Single-row variant — Req and Avail sit side by side, separated by
     a middot (operator: "the margin area can be shown in a single
     row ... instead of current two rows" — was previously stacked
     two-high, see the archived rationale this replaces). Pinned to
     the shared --ctl-h (item 9) so it never resizes the footer row
     relative to its siblings. */
  .oes-margin-pill-stack {
    display: inline-flex;
    flex-direction: row;
    align-items: baseline;
    justify-content: center;
    gap: 0.4rem;
    padding: 0.15rem 0.6rem;
    line-height: 1.05;
    height: var(--ctl-h, 1.7rem);
    min-height: var(--ctl-h, 1.7rem);
    box-sizing: border-box;
    min-width: 0;
    overflow: hidden;
  }
  .oes-margin-pill-row {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    flex-shrink: 1;
    /* Alignment fix (2026-09-30) — paired with the parent
       `.oes-margin-pill`'s overflow:hidden so an overlong Req/Avail
       value inside this row is clipped, not painted past the pill's
       own edge. */
    min-width: 0;
    overflow: hidden;
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
    /* Token consolidation (item 11) — was a hardcoded #86efac literal;
       the established "long/ok" token elsewhere in the app is
       var(--c-long) (#4ade80). */
    color: var(--c-long);
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
       single button + basket icon all pinned to the shared --ctl-h. */
    height: var(--ctl-h, 1.7rem);
    padding: 0 0.85rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    background: transparent;
    border: 1px solid rgba(125, 211, 252, 0.45);
    color: var(--algo-sky);
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
  /* Basket submit — sky to signal "multiple legs at once", distinct
     from the directional buy/sell palette. --c-info itself is left
     untouched — it's the app-wide "interactive affordance" token,
     out of this pass's scope. */
  .oes-common-submit-basket {
    background: rgba(125, 211, 252, 0.18);
    border-color: rgba(125, 211, 252, 0.65);
    color: var(--algo-sky);
  }
  .oes-common-submit-basket:hover { background: rgba(125, 211, 252, 0.28); }
  /* Depth-pending — LIMIT/SL ticket path OR any basket leg still
     waiting on a live chain quote (mirrors OrderTicket's own
     .ot-submit-depth-pending; GTT exit legs never reach this button).
     Overrides buy/sell/basket colour with the same neutral slate used
     elsewhere for "placeholder, not real yet" state. */
  .oes-common-submit.oes-common-submit-depth-pending {
    background: rgba(148, 163, 184, 0.14);
    border-color: rgba(148, 163, 184, 0.45);
    color: var(--c-muted);
  }
  /* Clear-basket — neutral outline. */
  .oes-common-clear {
    padding: 0.35rem 0.75rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
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
     tab inside (Orders / Agents / Console / Ticks / System / News)
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
  .oes-chart-tab-body {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .oes-chart-placeholder {
    margin: auto;
    color: var(--algo-slate-dim);
    font-size: var(--fs-xl);
    letter-spacing: 0.02em;
  }
</style>
