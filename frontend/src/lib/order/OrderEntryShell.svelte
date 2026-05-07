<script>
  // OrderEntryShell — tabbed order-entry modal.
  //
  // Three tabs:
  //   command  — terminal-style command input (CommandLineTab)
  //   ticket   — single-leg ticket (OrderTicket body)
  //   chain    — option-chain basket builder (OptionChainTab)
  //
  // Default tab is set by `defaultTab` prop. When the instrument is
  // equity (kind='equity', or exchange NSE/BSE with no FUT/CE/PE suffix)
  // the Chain tab is disabled and falls through to 'ticket'.
  //
  // All current OrderTicket props pass through transparently to the
  // Ticket tab so existing callsites only need to swap the import
  // from OrderTicket → OrderEntryShell and add a `defaultTab` prop.

  import { onMount } from 'svelte';
  import { placeTicketOrder, fetchLiveStatus } from '$lib/api';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import CommandLineTab   from '$lib/order/CommandLineTab.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';

  /** @type {{
   *   defaultTab?:     'command' | 'ticket' | 'chain',
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
   *   inline?:         boolean,
   * }} */
  let {
    defaultTab     = /** @type {'command'|'ticket'|'chain'} */ ('ticket'),
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
    // When true, render flat inline (no overlay, no fixed positioning,
    // no close button). Used by /console which hosts the shell as the
    // page's primary content rather than as a popup over another page.
    inline         = false,
  } = $props();

  // Determine whether Chain tab applies.
  // Equity = no FUT/CE/PE suffix AND (kind=equity OR exchange is cash-equity).
  const _sym = $derived(String(symbol || '').toUpperCase());
  const _isDerivative = $derived(/(?:CE|PE|FUT)$/.test(_sym));
  const _isEquityExch = $derived(
    (!_isDerivative) &&
    (
      (instrument?.kind === 'equity') ||
      (['NSE', 'BSE'].includes(String(instrument?.exchange || exchange || '').toUpperCase()) && !_isDerivative)
    )
  );
  const chainDisabled = $derived(_isEquityExch && !_isDerivative);

  // Resolve initial tab — fall through chain → ticket when equity.
  function _resolveInitialTab() {
    const req = defaultTab || 'ticket';
    if (req === 'chain' && chainDisabled) return 'ticket';
    return req;
  }
  let _activeTab = $state(/** @type {'command'|'ticket'|'chain'} */ (_resolveInitialTab()));

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

    for (const leg of basketLegs) {
      try {
        const hasLimit = Number(leg.limit) > 0;
        await placeTicketOrder({
          mode:             basketMode2,
          side:             leg.side,
          tradingsymbol:    leg.sym,
          exchange:         leg.exchange || 'NFO',
          quantity:         (leg.lots || 1) * (leg.lotSize || 1),
          product:          leg.product || 'NRML',
          order_type:       hasLimit ? 'LIMIT' : 'MARKET',
          variety:          'regular',
          price:            hasLimit ? Number(leg.limit) : 0,
          account:          leg.account || '',
          chase:            hasLimit,
          chase_aggressiveness: hasLimit ? (leg.chaseAgg || 'low') : 'low',
        });
        ok++;
        onSubmit?.({ ...leg, _basketLeg: true });
      } catch (e) {
        fails.push(`${leg.side} ${leg.sym}: ${/** @type {any} */ (e)?.message || 'failed'}`);
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
      // Keep only failed legs in the basket.
      basketLegs = basketLegs.filter((_, i) => i >= ok);
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
    _activeTab = 'ticket';
  }

  /** Called by CommandLineTab when basketMode is on. */
  function handleCmdAddToBasket(/** @type {any} */ leg) {
    addToBasket(leg);
  }

  // Close on Escape (only when the confirm overlay inside OrderTicket
  // is NOT open — OrderTicket handles its own Escape for that).
  onMount(() => {
    const onKey = (/** @type {KeyboardEvent} */ e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const TABS = /** @type {const} */ ([
    { id: 'command', label: 'Command line', dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', mutedTxt: '#475569' },
    { id: 'ticket',  label: 'Order ticket', dot: '#fbbf24', activeTxt: '#fbbf24', activeBorder: '#fbbf24', mutedTxt: '#7e6a3b' },
    { id: 'chain',   label: 'Chain',        dot: '#4ade80', activeTxt: '#4ade80', activeBorder: '#4ade80', mutedTxt: '#3a604a' },
  ]);

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
<div class="oes-overlay"
     class:oes-inline={inline}
     role={inline ? undefined : 'dialog'}
     aria-modal={inline ? undefined : 'true'}
     aria-label={inline ? undefined : 'Order entry'}
     onclick={inline ? undefined : onClose}>
  <div class="oes-modal" class:oes-modal-inline={inline}
       role="document"
       onclick={inline ? undefined : (e) => e.stopPropagation()}>

    <!-- Header (close button hidden in inline mode) -->
    <div class="oes-header">
      <span class="oes-title">Order entry{symbol ? ' · ' + symbol : ''}</span>
      {#if !inline}
        <button type="button" class="oes-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
      {/if}
    </div>

    <!-- Tab strip -->
    <div class="oes-tabs" role="tablist">
      {#each TABS as tab}
        {@const disabled   = tab.id === 'chain' && chainDisabled}
        {@const isActive   = _activeTab === tab.id}
        {@const badgeCount = tab.id === 'chain' ? basketLegs.length : 0}
        <button
          type="button"
          role="tab"
          class="oes-tab"
          class:oes-tab-disabled={disabled}
          disabled={disabled}
          title={disabled ? 'Option chain only applies to F&O instruments' : undefined}
          aria-selected={isActive}
          aria-disabled={disabled}
          style="
            color: {isActive ? tab.activeTxt : tab.mutedTxt};
            border-bottom-color: {isActive ? tab.activeBorder : 'transparent'};
            opacity: {disabled ? '0.5' : '1'};
            cursor: {disabled ? 'not-allowed' : 'pointer'};
          "
          onclick={() => { if (!disabled) _activeTab = /** @type {any} */ (tab.id); }}
        >
          <span class="oes-tab-dot" style="background:{tab.dot};"></span>
          {tab.label}
          {#if tab.id === 'chain' && badgeCount > 0}
            <span class="oes-tab-badge">{badgeCount}</span>
          {/if}
        </button>
      {/each}
    </div>

    <!-- Tab content -->
    <div class="oes-body">
      {#if _activeTab === 'command'}
        <CommandLineTab
          onParsedOrder={basketMode ? undefined : handleParsedOrder}
          onAddToBasket={basketMode ? handleCmdAddToBasket : undefined} />

      {:else if _activeTab === 'ticket'}
        <!-- OrderTicket renders its own overlay/modal chrome; inside
             the shell we only want the body. We render it without the
             outer overlay by mounting it directly — the shell provides
             the modal chrome above, so we suppress OrderTicket's own
             overlay by setting a container class that strips position:fixed. -->
        <div class="oes-ticket-body">
          <OrderTicket
            symbol={_ticketProps.symbol || symbol}
            exchange={_ticketProps.exchange ?? exchange}
            side={_ticketProps.side ?? side}
            action={_ticketProps.action ?? action}
            qty={_ticketProps.qty ?? qty}
            product={_ticketProps.product ?? product}
            orderType={_ticketProps.orderType ?? orderType}
            variety={_ticketProps.variety ?? variety}
            price={_ticketProps.price ?? price}
            trigger={_ticketProps.trigger ?? trigger}
            lotSize={_ticketProps.lotSize ?? lotSize}
            accounts={_ticketProps.accounts ?? accounts}
            account={_ticketProps.account ?? account}
            orderId={_ticketProps.orderId ?? orderId}
            defaultMode={_ticketProps.defaultMode ?? defaultMode}
            availableModes={_ticketProps.availableModes ?? availableModes}
            currentQty={_ticketProps.currentQty ?? currentQty}
            onAddToBasket={basketMode ? addToBasket : (_ticketProps.onAddToBasket ?? onAddToBasket)}
            basketMode={basketMode}
            {onSubmit}
            {onClose} />
        </div>

      {:else if _activeTab === 'chain'}
        <!-- OptionChainTab's own basket state is migrated to the shell.
             The tab receives the shared basket as props and calls back
             into the shell to mutate it. Its own placeBasket is unused
             when routed through onSubmitBasket. -->
        <OptionChainTab
          {symbol}
          {account}
          {accounts}
          basketLegs={basketLegs}
          onAddLeg={addToBasket}
          onRemoveLeg={(/** @type {any} */ leg) => {
            const i = basketLegs.findIndex(b => b.key === leg.key);
            if (i >= 0) removeBasketLeg(i);
          }}
          onSubmitBasket={submitBasket}
          onClearBasket={clearBasket}
          onBasketPlace={({ ok, fail }) => {
            if (ok > 0 && fail === 0) {
              onSubmit?.({ mode: 'paper', _basketLegs: ok });
              setTimeout(onClose, 400);
            }
          }} />
      {/if}
    </div>

    <!-- Shell-level basket bar — visible from any tab when legs exist. -->
    {#if basketLegs.length > 0}
      <div class="oes-basket-bar">
        <span class="oes-basket-count">{basketLegs.length} leg{basketLegs.length === 1 ? '' : 's'} in basket</span>
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
    {/if}
  </div>
</div>

<style>
  .oes-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 1rem;
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
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 8px;
    width: min(34rem, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
    display: flex;
    flex-direction: column;
  }
  .oes-modal.oes-modal-inline {
    width: 100%;
    max-height: none;
    border-radius: 6px;
    box-shadow: none;
  }

  /* Header */
  .oes-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.7rem 1rem 0.5rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    flex-shrink: 0;
  }
  .oes-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #e2e8f0;
    letter-spacing: 0.04em;
  }
  .oes-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: #c8d8f0;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .oes-close:hover { border-color: #f87171; color: #f87171; }

  /* Tab strip — bottom-border underline active tab; each tab has its own
     accent colour via inline style (applied from the TABS metadata).     */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    flex-shrink: 0;
  }
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
    transition: color 0.12s, border-color 0.12s, opacity 0.12s;
    white-space: nowrap;
  }
  .oes-tab:hover:not(.oes-tab-disabled) {
    opacity: 0.8 !important;
  }
  .oes-tab-disabled {
    cursor: not-allowed !important;
  }
  /* Colour dot — small circle before the tab label */
  .oes-tab-dot {
    display: inline-block;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    flex-shrink: 0;
    opacity: 0.75;
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
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem 0.7rem;
    padding: 0.5rem 1rem;
    border-top: 1px solid rgba(74,222,128,0.25);
    background: rgba(74,222,128,0.05);
    flex-shrink: 0;
  }
  .oes-basket-count {
    font-family: monospace;
    font-size: 0.65rem;
    font-weight: 700;
    color: #4ade80;
    letter-spacing: 0.04em;
  }
  .oes-basket-result {
    font-family: monospace;
    font-size: 0.62rem;
    color: #c8d8f0;
    flex: 1 1 100%;
    margin-top: 0.15rem;
  }
  .oes-basket-actions {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  .oes-basket-clear,
  .oes-basket-submit {
    height: 1.5rem;
    padding: 0 0.75rem;
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
  .oes-basket-submit { color: #4ade80; background: rgba(74,222,128,0.10); }
  .oes-basket-submit:hover:not(:disabled) { background: rgba(74,222,128,0.20); }
  .oes-basket-clear:disabled,
  .oes-basket-submit:disabled { opacity: 0.45; cursor: progress; }

  /* Body — the tab content area. */
  .oes-body {
    flex: 1 1 auto;
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
</style>
