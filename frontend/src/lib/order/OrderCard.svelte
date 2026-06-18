<script>
  /**
   * OrderCard — single-source order-row card used by /orders (Order
   * Activity book grid) AND every LogPanel Orders-tab surface
   * (Activity modal, Order modal bottom panel, /console, /automation).
   *
   * Earlier the two surfaces rendered orders differently: /orders
   * drew bordered cards (algo-status-card / order-card); LogPanel
   * streamed text-spans inside a <pre>. The result was that the
   * Activity-modal "Orders" tab read as a terminal log, while the
   * dedicated /orders page looked like a broker blotter — same data,
   * different look. Operator asked for visual sync.
   *
   * Props:
   *   order             the AlgoOrder row (or broker order shape)
   *   onCardClick?      fires when the card body is clicked / Enter /
   *                     Space — typically opens OrderDetail panel
   *   onSymbolClick?    fires when the symbol cell is clicked — typically
   *                     opens the Order modal pre-filled with that symbol
   *   onSymbolContext?  fires on right-click / long-press of the symbol
   *                     — opens SymbolContextMenu at the click coords
   *   slot "actions"    optional trailing action strip (Cancel / Modify /
   *                     Repeat on /orders; suppressed in LogPanel context)
   */

  import { priceFmt, qtyFmt } from '$lib/format';
  import { formatDualTz } from '$lib/stores';
  import { longPress } from '$lib/actions/longPress.js';
  import { formatSymbol } from '$lib/data/decomposeSymbol';

  let {
    /** @type {any} */                                   order,
    /** @type {((o:any) => void) | undefined} */         onCardClick   = undefined,
    /** @type {((o:any, e:Event) => void) | undefined} */onSymbolClick = undefined,
    /** @type {((o:any, e:MouseEvent) => void) | undefined} */
                                                          onSymbolContext = undefined,
    /** @type {import('svelte').Snippet | undefined} */  actions       = undefined,
  } = $props();

  // Status → data-status attribute. .algo-status-card reads this for
  // the left-edge stripe colour. Matches /orders'
  // statusDataAttr().
  /** @param {string} s */
  function _statusDataAttr(s) {
    const c = (s || '').toUpperCase();
    if (c === 'COMPLETE')                              return 'active';
    if (c === 'REJECTED' || c === 'CANCELLED')         return 'error';
    if (c === 'OPEN' || c === 'TRIGGER PENDING')       return 'running';
    return 'inactive';
  }

  /** @param {string} t */
  function _txnStyle(t) {
    return t === 'BUY' ? 'color: var(--btn-buy)' : 'color: var(--btn-sell)';
  }

  // Per-account hue — same algorithm /orders uses so the same account
  // gets the same tint on both surfaces. Module-level cache keeps the
  // assignment stable across mounts.
  const _ACCT_COLORS = ['text-sky-300', 'text-amber-300', 'text-fuchsia-300', 'text-teal-300'];
  /** @type {string[]} */
  const _acctList = [];
  /** @param {string} a */
  function _acctColor(a) {
    let idx = _acctList.indexOf(a);
    if (idx < 0) { _acctList.push(a); idx = _acctList.length - 1; }
    return _ACCT_COLORS[idx % _ACCT_COLORS.length];
  }

  // Field-fallback helpers — broker `OrderRow` uses `tradingsymbol` /
  // `price` / `average_price` / `order_timestamp`; the platform's
  // `AlgoOrderInfo` (paper / sim / shadow / live-tracked) uses `symbol`
  // / `initial_price` / `fill_price` / `created_at`. The same OrderCard
  // renders both shapes so the /orders Order Activity book and every
  // LogPanel Orders-tab surface look identical.
  const _symRaw    = $derived(order?.tradingsymbol || order?.symbol || '');
  // Display-only — `_sym` is what shows in the UI (Dhan-style hyphenated
  // for F&O, unchanged for cash equity). Storage / API calls keep using
  // _symRaw which is the Kite tradingsymbol.
  const _sym       = $derived(formatSymbol(_symRaw));
  const _limit     = $derived(order?.price ?? order?.initial_price ?? null);
  const _filled    = $derived(order?.average_price ?? order?.fill_price ?? null);
  const _ts        = $derived(order?.order_timestamp || order?.created_at || null);
  const _qtyFilled = $derived(order?.filled_quantity ?? (order?.fill_price != null ? order?.quantity : 0));

  /** @param {any} o */
  function _slippage(o) {
    // Slippage = fill − limit, only when both are present + numeric.
    // Works for broker (status=COMPLETE → avg vs price) AND for algo
    // (status=FILLED → fill_price vs initial_price).
    const status = (o.status || '').toUpperCase();
    const isTerminalFilled = status === 'COMPLETE' || status === 'FILLED';
    if (!isTerminalFilled) return null;
    const lim = (o.price ?? o.initial_price);
    const fil = (o.average_price ?? o.fill_price);
    if (lim == null || fil == null) return null;
    const p = Number(lim);
    if (!(p > 0)) return null;
    const d = Number(fil) - p;
    return Number.isFinite(d) ? d : null;
  }

  /** @param {string} tag */
  function _tagClass(tag) {
    if (!tag) return '';
    if (tag === 'ramboq-ticket') return 'tag-manual';
    if (tag.startsWith('ramboq-agent')) return 'tag-agent';
    return '';
  }

  const _slip = $derived(_slippage(order));
</script>

<!-- Outer is a div role=button (not <button>) so inline action buttons
     can nest. Click / Enter / Space toggle the OrderDetail panel on
     surfaces that wire onCardClick; LogPanel mounts pass none and the
     card stays inert. -->
<div role="button" tabindex="0"
  onclick={() => onCardClick?.(order)}
  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onCardClick?.(order); } }}
  class="algo-status-card text-left p-2.5 transition order-card"
  data-status={_statusDataAttr(order.status)}>
  <div class="flex items-center justify-between mb-0.5 gap-1">
    <span class="font-semibold text-xs">
      <span style={_txnStyle(order.transaction_type)}>{order.transaction_type}</span>
      <span class={_acctColor(order.account)}>{order.account}</span>
      <!-- svelte-ignore a11y_interactive_supports_focus -->
      <span class="text-[#c8d8f0] oc-sym-btn"
        role="button"
        tabindex="0"
        title="Open {_sym}"
        onclick={(e) => { e.stopPropagation(); onSymbolClick?.(order, e); }}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onSymbolClick?.(order, e); } }}
        oncontextmenu={(e) => { e.preventDefault(); onSymbolContext?.(order, e); }}
        use:longPress={(ev) => { onSymbolContext?.(order, ev); }}>{_sym}</span>
    </span>
    <span class="text-[0.55rem] px-1.5 py-0.5 rounded font-medium uppercase border
      {order.status === 'COMPLETE' || order.status === 'FILLED' ? 'bg-green-500/15 text-green-400 border-green-500/40'
      : order.status === 'REJECTED' ? 'bg-red-500/15 text-red-400 border-red-500/40'
      : order.status === 'UNFILLED' ? 'bg-orange-500/15 text-orange-400 border-orange-500/40'
      : order.status === 'CANCELLED' ? 'bg-slate-500/20 text-slate-300 border-slate-500/40'
      : 'bg-amber-500/15 text-amber-400 border-amber-500/40'}">{order.status}</span>
  </div>
  <!-- Chip row — same .log-chip / .log-chip-key family the LogPanel
       order rows used to render via _orderRowHtml, so the chips read
       identically when you flip between the dedicated /orders page
       and the Activity-modal Orders tab. -->
  <div class="flex flex-wrap items-center gap-y-1">
    {#if order.exchange}<span class="log-chip"><span class="log-chip-key">ex:</span>{order.exchange}</span>{/if}
    <span class="log-chip"><span class="log-chip-key">qty:</span>{qtyFmt(_qtyFilled)}/{qtyFmt(order.quantity)}</span>
    {#if order.order_type}<span class="log-chip"><span class="log-chip-key">type:</span>{order.order_type}</span>{/if}
    <span class="log-chip"><span class="log-chip-key">price:</span>{_filled != null ? priceFmt(_filled) : _limit != null ? priceFmt(_limit) : '—'}</span>
    {#if _slip != null}<span class="log-chip log-chip-slip" class:slip-up={_slip > 0} class:slip-down={_slip < 0}><span class="log-chip-key">slip:</span>{_slip > 0 ? '+' : ''}{priceFmt(_slip)}</span>{/if}
    {#if order.attempts != null && order.attempts > 0}<span class="log-chip"><span class="log-chip-key">chase:</span>#{order.attempts}</span>{/if}
    {#if order.trigger_price}<span class="log-chip"><span class="log-chip-key">trigger:</span>{priceFmt(order.trigger_price)}</span>{/if}
    {#if order.validity}<span class="log-chip"><span class="log-chip-key">validity:</span>{order.validity}</span>{/if}
    {#if order.product}<span class="log-chip"><span class="log-chip-key">product:</span>{order.product}</span>{/if}
    {#if order.variety}<span class="log-chip"><span class="log-chip-key">variety:</span>{order.variety}</span>{/if}
    {#if order.mode}<span class="log-chip"><span class="log-chip-key">mode:</span>{order.mode}</span>{/if}
    {#if order.engine}<span class="log-chip"><span class="log-chip-key">engine:</span>{order.engine}</span>{/if}
    {#if _ts}<span class="log-chip"><span class="log-chip-key">time:</span>{formatDualTz(new Date(_ts))}</span>{/if}
    {#if order.tag}<span class="log-chip {_tagClass(order.tag)}"><span class="log-chip-key">tag:</span>{order.tag}</span>{/if}
    {#if order.target_pct != null}<span class="log-chip log-chip-tp"><span class="log-chip-key">tp:</span>+{(Number(order.target_pct) * 100).toFixed(1)}%</span>{/if}
    {#if order.template_id != null}<span class="log-chip log-chip-template" title={order.attached_gtts_json ? 'Template attached on fill: ' + order.attached_gtts_json : 'Template selected — will attach on fill'}><span class="log-chip-key">tmpl:</span>#{order.template_id}{order.attached_gtts_json ? ' ✓' : '…'}</span>{/if}
    {#if order.parent_order_id != null}<span class="log-chip log-chip-parent"><span class="log-chip-key">parent:</span>#{String(order.parent_order_id).slice(-6)}</span>{/if}
    {#if order.basket_tag}<span class="log-chip log-chip-basket"><span class="log-chip-key">basket:</span>{order.basket_tag}</span>{/if}
    {#if order.status_message}<span class="log-chip"><span class="log-chip-key">note:</span>{order.status_message}</span>{/if}
    {#if order.detail}<span class="log-chip"><span class="log-chip-key">note:</span>{order.detail}</span>{/if}
  </div>
  {#if actions}
    {@render actions(order)}
  {/if}
</div>

<style>
  /* Slippage chip colouring — positive slip (paid more) = red,
     negative (received more) = green. */
  :global(.log-chip-slip.slip-up)   { color: #fca5a5; }
  :global(.log-chip-slip.slip-down) { color: #86efac; }

  /* Tag colour-coding — manual ticket = sky-blue, agent-fired = amber. */
  :global(.tag-manual)       { color: #67e8f9; background: rgba(34, 211, 238, 0.10); }
  :global(.tag-agent)        { color: #fbbf24; background: rgba(251, 191, 36, 0.10); }
  /* TP / parent / basket linkage chips */
  :global(.log-chip-tp)      { color: #4ade80; background: var(--algo-green-bg); }
  :global(.log-chip-template) { color: #c084fc; background: rgba(192, 132, 252, 0.12); }
  :global(.log-chip-parent)  { color: #7dd3fc; background: rgba(125, 211, 252, 0.10); }
  :global(.log-chip-basket)  { color: #fbbf24; background: rgba(251, 191, 36, 0.10); }
</style>