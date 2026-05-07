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

  // Ticket pre-fill state — when the Command tab parses an order command
  // it routes back here to switch to the Ticket tab pre-filled.
  /** @type {any} */
  let _cmdOrderProps = $state(null);

  function handleParsedOrder(/** @type {any} */ props) {
    _cmdOrderProps = props;
    _activeTab = 'ticket';
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
    { id: 'command', label: 'Command line' },
    { id: 'ticket',  label: 'Order ticket' },
    { id: 'chain',   label: 'Chain'        },
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

<!-- Modal overlay — click outside closes. -->
<div class="oes-overlay" role="dialog" aria-modal="true" aria-label="Order entry"
     onclick={onClose}>
  <div class="oes-modal" role="document" onclick={(e) => e.stopPropagation()}>

    <!-- Header -->
    <div class="oes-header">
      <span class="oes-title">Order entry{symbol ? ' · ' + symbol : ''}</span>
      <button type="button" class="oes-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
    </div>

    <!-- Tab strip -->
    <div class="oes-tabs" role="tablist">
      {#each TABS as tab}
        {@const disabled = tab.id === 'chain' && chainDisabled}
        <button
          type="button"
          role="tab"
          class="oes-tab"
          class:oes-tab-active={_activeTab === tab.id}
          class:oes-tab-disabled={disabled}
          disabled={disabled}
          title={disabled ? 'Option chain only applies to F&O instruments' : undefined}
          aria-selected={_activeTab === tab.id}
          aria-disabled={disabled}
          onclick={() => { if (!disabled) _activeTab = /** @type {any} */ (tab.id); }}
        >{tab.label}</button>
      {/each}
    </div>

    <!-- Tab content -->
    <div class="oes-body">
      {#if _activeTab === 'command'}
        <CommandLineTab
          onParsedOrder={handleParsedOrder} />

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
            onAddToBasket={_ticketProps.onAddToBasket ?? onAddToBasket}
            {onSubmit}
            {onClose} />
        </div>

      {:else if _activeTab === 'chain'}
        <OptionChainTab
          {symbol}
          {account}
          {accounts}
          onBasketPlace={({ ok, fail }) => {
            if (ok > 0 && fail === 0) {
              // All legs submitted — let the caller refresh and close.
              onSubmit?.({ mode: 'paper', _basketLegs: ok });
              setTimeout(onClose, 400);
            }
          }} />
      {/if}
    </div>
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

  /* Tab strip — bottom-border underline active tab, same as /market tabs. */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    flex-shrink: 0;
  }
  .oes-tab {
    padding: 0.45rem 0.75rem;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    color: #7e97b8;
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s;
    white-space: nowrap;
  }
  .oes-tab:hover:not(.oes-tab-disabled):not(.oes-tab-active) {
    color: #c8d8f0;
  }
  .oes-tab-active {
    color: #e2e8f0;
    border-bottom-color: #d4920c; /* champagne gold — matches /market active tab */
  }
  .oes-tab-disabled {
    color: #3d5068;
    cursor: not-allowed;
    opacity: 0.6;
  }

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
