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
  import { isDraftOrderRow } from '$lib/data/draftOrderRow.js';
  import { parseHold } from '$lib/data/holdRecord.js';
  import { formatDualTz } from '$lib/stores';
  import { longPress } from '$lib/actions/longPress.js';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { acctColor } from '$lib/account';

  let {
    /** @type {any} */                                   order,
    /** @type {((o:any) => void) | undefined} */         onCardClick   = undefined,
    /** @type {((o:any, e:Event) => void) | undefined} */onSymbolClick = undefined,
    /** @type {((o:any, e:MouseEvent) => void) | undefined} */
                                                          onSymbolContext = undefined,
    /** @type {import('svelte').Snippet | undefined} */  actions       = undefined,
  } = $props();

  // Hold state: the backend stores it as JSON on hold_json; null when the
  // order is not held. `parseHold` lives in `$lib/data/holdRecord.js`,
  // shared across every hold-displaying surface so they can never
  // silently disagree on what "held" means.
  const holdInfo = $derived(parseHold(order.hold_json));

  // Status → data-status attribute. .algo-status-card reads this for
  // the left-edge stripe colour; .algo-status-pill child inherits
  // --st-fg / --st-bg / --st-border via the parent's data-status.
  // A draft (mode='draft') is excluded BEFORE this function runs (see
  // the template's `data-status` binding below) — its real `status` is
  // always 'OPEN', which this function would otherwise map to 'running'
  // (amber, "actively working at the broker"), the exact look a draft
  // must not carry since it never reached one.
  /** @param {string} s */
  function _statusDataAttr(s) {
    const c = (s || '').toUpperCase();
    if (c === 'COMPLETE' || c === 'FILLED')            return 'complete';
    if (c === 'REJECTED')                              return 'rejected';
    if (c === 'CANCELLED')                             return 'cancelled';
    // Audit fix (2026-10) — HELD (operator-review hold, order_hold_gate.py)
    // previously fell through to the `inactive` catch-all (grey,
    // indistinguishable from a long-dead row) despite being an order that
    // actively needs operator action (release or cancel). Distinct
    // violet treatment, matching OrderBook.svelte's new "Held/Attn" chip.
    if (c === 'HELD')                                   return 'held';
    // Audit fix (H-1, H-2) — CANCEL_FAILED is a distinct state from
    // CANCELLED: the operator clicked Kill but the broker.cancel call
    // failed; the order is still live at the broker. Pre-fix it
    // dropped through to "inactive" (grey, indistinguishable from
    // long-dead rows). Now treated as `error` so the row's left-edge
    // stripe reads as a danger signal — operator must verify broker
    // state + reattempt the kill or reconcile.
    if (c === 'CANCEL_FAILED')                         return 'error';
    if (c === 'OPEN' || c === 'TRIGGER_PENDING'
      || c === 'TRIGGER PENDING'
      || c === 'AMO_REQ_RECEIVED'
      || c === 'PUT_ORDER_REQ_RECEIVED')               return 'running';
    return 'inactive';
  }

  /** @param {string} t */
  function _txnStyle(t) {
    return t === 'BUY' ? 'color: var(--btn-buy)' : 'color: var(--btn-sell)';
  }

  // Per-account identity — delegates to the shared $lib/account.js
  // palette (A2, 2026-09 audit). Previously this was a local, insertion-
  // order Map over 4 Tailwind TEXT classes — non-deterministic across
  // sessions (order of first-seen account changed the colour) AND
  // recolored the identity signal onto text, the same anti-pattern A2
  // corrects everywhere else. Fixed to the STRIPE (identity channel) via
  // the `.oc-acct` class below, not text colour.

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
  // current_limit is the chase's live re-quoted price (updated on every
  // cancel-and-replace); price/initial_price are the order's ORIGINAL
  // submission price and go stale the moment chase re-prices. Same
  // priority OrderCard's dedicated sibling ChaseCard.svelte already uses
  // (`row.current_limit ?? row.initial_price`) — OrderBook.svelte and
  // LogPanel.svelte already merge `current_limit` onto this row before
  // it reaches OrderCard, this was just never read here.
  const _limit     = $derived(order?.current_limit ?? order?.price ?? order?.initial_price ?? null);
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
    // 'ramboq-agent' branch removed (2026-10-18) — no backend path ever
    // produces that broker tag (confirmed via repo-wide grep). The real
    // agent-origin signal is `order.agent_origin` (2026-10-31, rendered
    // as the "agent:" chip below) — `order.source` is an unrelated
    // cancel-source label (mcp/operator/sim_stop), rendered separately
    // as the "origin:" chip.
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
  data-status={isDraftOrderRow(order) ? 'inactive' : _statusDataAttr(order.status)}>
  <div class="flex items-center justify-start gap-2 mb-0.5">
    <!-- font-size via inline style (not Tailwind's text-xs) — matches the
         app's own --fs-* scale rather than Tailwind's stock 0.75rem,
         consistent with --fs-lg (0.72rem) used elsewhere for "main row
         text" (2026-10-18 size-consistency fix). -->
    <span class="font-semibold min-w-0 truncate" style="font-size: var(--fs-lg);">
      <span style={_txnStyle(order.transaction_type)}>{order.transaction_type}</span>
      <span class="oc-acct" style={order.account ? `--acct-color: ${acctColor(order.account) || 'transparent'};` : ''}>{order.account}</span>
      <!-- svelte-ignore a11y_interactive_supports_focus -->
      <span class="text-[var(--algo-slate)] oc-sym-btn"
        role="button"
        tabindex="0"
        title="Open {_sym}"
        onclick={(e) => { e.stopPropagation(); onSymbolClick?.(order, e); }}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); onSymbolClick?.(order, e); } }}
        oncontextmenu={(e) => { e.preventDefault(); onSymbolContext?.(order, e); }}
        use:longPress={(ev) => { onSymbolContext?.(order, ev); }}>{_sym}</span>
    </span>
    <!-- Status pill — color driven by parent .algo-status-card[data-status]
         via --st-fg / --st-bg / --st-border CSS vars. CANCEL_FAILED gets
         a ⚠ prefix and tooltip so the operator can distinguish it from a
         clean CANCELLED row. ml-auto pushes it to the right edge now that
         the container is justify-start (was justify-between).
         2026-10 round 2: a draft (mode='draft') never reached a broker —
         its real `status` is always 'OPEN', which would otherwise render
         exactly like a genuine resting order. Relabel, don't hide: a
         DRAFT pill replaces the status badge here (reuses the
         `.mode-pill-draft` token — originally defined in LogPanel.svelte
         for this exact mode, now lifted to app.css, see that file's
         `_modePill`) while the row itself stays fully visible in the
         merged list, just uncounted in every OrderBook/LogPanel status
         chip (see those files' own fixes). -->
    {#if isDraftOrderRow(order)}
      <span class="mode-pill mode-pill-draft ml-auto flex-shrink-0"
        title="Draft position — never placed, no broker round-trip">DRAFT</span>
    {:else}
      <span class="algo-status-pill ml-auto flex-shrink-0"
        title={order.status === 'CANCEL_FAILED'
          ? 'Kill attempt failed — order may still be live at broker. Reconcile or retry kill.'
          : ''}>{order.status === 'CANCEL_FAILED' ? '⚠ KILL FAILED' : order.status}</span>
    {/if}
    {#if holdInfo}
      <!-- Item 3 (2026-11-02, PLAN.md) — "Pending" replaces "HELD" for
           the template-exit-hold mechanism (order_hold_gate.py). CSS
           class `held-badge`/`held` kept as-is — internal selector,
           not operator-visible text. Distinct from the unrelated
           covered-call feature's own "Held" usage elsewhere in the
           app, which this does NOT touch. -->
      <span class="algo-status-pill held-badge ml-1"
        title={`Pending: ${holdInfo.reason || holdInfo.category}`}>Pending</span>
    {/if}
  </div>
  <!-- Chip row — same .log-chip / .log-chip-key family the LogPanel
       order rows used to render via _orderRowHtml, so the chips read
       identically when you flip between the dedicated /orders page
       and the Activity-modal Orders tab. -->
  <div class="flex flex-wrap items-center gap-y-1">
    {#if order.exchange}<span class="log-chip"><span class="log-chip-key">ex:</span>{order.exchange}</span>{/if}
    <!-- Audit fix (L-2) — pre-fix `0/50` read like an error state for
         freshly placed OPEN orders (operator parses the leading zero
         as "broken"). When nothing has filled AND status is non-
         terminal, show `qty:50` (clean) instead of `qty:0/50`.
         Terminal states + partial fills keep the filled/total form
         so the operator sees progress through partials clearly. -->
    <span class="log-chip">
      <span class="log-chip-key">qty:</span>{_qtyFilled > 0 || ['COMPLETE','FILLED','REJECTED','CANCELLED','UNFILLED','CANCEL_FAILED'].includes((order.status || '').toUpperCase())
        ? `${qtyFmt(_qtyFilled)}/${qtyFmt(order.quantity)}`
        : qtyFmt(order.quantity)}
    </span>
    {#if order.qty_unverified}
      <span class="log-chip qty-unverified" title="Lot size not loaded: this quantity is shown unconverted">qty ?</span>
    {/if}
    {#if order.order_type}<span class="log-chip"><span class="log-chip-key">type:</span>{order.order_type}</span>{/if}
    <span class="log-chip"><span class="log-chip-key">price:</span>{_filled != null ? priceFmt(_filled) : _limit != null ? priceFmt(_limit) : '—'}</span>
    {#if _slip != null}
      <!-- Slippage direction: ↑ fill > limit, ↓ fill < limit.
           Favorability is side-dependent (SELL: ↑ is good, BUY: ↓ is good)
           so we show a neutral directional arrow in slate rather than
           green/red which would be misleading for one side. -->
      <span class="log-chip log-chip-slip"><span class="log-chip-key">slip:</span>{_slip > 0 ? '↑' : _slip < 0 ? '↓' : ''}{priceFmt(Math.abs(_slip))}</span>
    {/if}
    <!-- Partial-fill indicator: renders when AlgoOrderInfo.filled_quantity
         (B5 backend field) is present, positive, and less than total qty.
         Fires on CANCEL_FAILED rows where the broker partially filled before
         the kill: operator sees "partial: 30 of 50" as a distinct danger
         signal rather than having to decode the qty:X/Y chip. -->
    {#if order.filled_quantity != null && order.filled_quantity > 0 && order.filled_quantity < order.quantity}
      <span class="log-chip log-chip-partial-fill"
            title="Order partially filled — {qtyFmt(order.filled_quantity)} of {qtyFmt(order.quantity)} contracts filled. Reconcile the remaining {qtyFmt(order.quantity - order.filled_quantity)} open with the broker.">
        <span class="log-chip-key">partial:</span>{qtyFmt(order.filled_quantity)} of {qtyFmt(order.quantity)}
      </span>
    {/if}
    {#if order.attempts != null && order.attempts > 0}<span class="log-chip"><span class="log-chip-key">chase:</span>#{order.attempts}</span>{/if}
    {#if order.trigger_price}<span class="log-chip"><span class="log-chip-key">trigger:</span>{priceFmt(order.trigger_price)}</span>{/if}
    {#if order.validity}<span class="log-chip"><span class="log-chip-key">validity:</span>{order.validity}</span>{/if}
    {#if order.product}<span class="log-chip"><span class="log-chip-key">product:</span>{order.product}</span>{/if}
    {#if order.variety}<span class="log-chip"><span class="log-chip-key">variety:</span>{order.variety}</span>{/if}
    {#if order.mode}<span class="log-chip"><span class="log-chip-key">mode:</span>{order.mode}</span>{/if}
    {#if order.engine}<span class="log-chip"><span class="log-chip-key">engine:</span>{order.engine}</span>{/if}
    <!-- 2026-10-31 — "Agent: manual" / "Agent: <name>" on every order,
         same SSOT resolution (`resolve_origin()`) as every alert's own
         origin line (see "Every alert leads with its origin" in
         CLAUDE.md). AlgoOrderInfo.agent_origin defaults to "manual" so
         this always renders — distinct from the "origin:" chip below,
         which is `order.source` (an unrelated cancel-source label). -->
    {#if order.agent_origin}<span class="log-chip"><span class="log-chip-key">agent:</span>{order.agent_origin}</span>{/if}
    <!-- Sprint 2b (SPRINT2_LAYER_INTEGRATION.md §3) — AlgoOrder.source,
         labeled "Origin" in the UI (not "source") to avoid colliding
         with UnifiedLogRow.source, an unrelated field meaning "which
         table did this merged log row come from" in the sibling
         UnifiedLog.svelte surface. null on legacy rows placed before
         this field existed and on raw broker OrderRow shapes (no
         `source` field on that schema at all) — renders nothing, not
         an empty/placeholder chip. -->
    {#if order.source}<span class="log-chip"><span class="log-chip-key">origin:</span>{order.source}</span>{/if}
    {#if _ts}<span class="log-chip"><span class="log-chip-key">time:</span>{formatDualTz(new Date(_ts))}</span>{/if}
    {#if order.tag}<span class="log-chip {_tagClass(order.tag)}"><span class="log-chip-key">tag:</span>{order.tag}</span>{/if}
    {#if order.target_pct != null}<span class="log-chip log-chip-tp"><span class="log-chip-key">tp:</span>+{(Number(order.target_pct) * 100).toFixed(1)}%</span>{/if}
    {#if order.parent_order_id != null}<span class="log-chip log-chip-parent" title="This row is an auto-attached leg of the listed parent order (typically a take-profit child)."><span class="log-chip-key">parent:</span>#{String(order.parent_order_id).slice(-6)}</span>{/if}
    {#if order.child_order_ids && order.child_order_ids.length}
      <span class="log-chip log-chip-child"
            title={`Auto-attached children of this order (typically a take-profit child). Click any id in the activity log to inspect.`}>
        <span class="log-chip-key">{order.child_order_ids.length === 1 ? 'child:' : 'children:'}</span>
        {order.child_order_ids.map(id => '#' + String(id).slice(-6)).join(' ')}
      </span>
    {/if}
    {#if order.basket_tag}<span class="log-chip log-chip-basket"><span class="log-chip-key">basket:</span>{order.basket_tag}</span>{/if}
    {#if order.status_message}<span class="log-chip"><span class="log-chip-key">note:</span>{order.status_message}</span>{/if}
    {#if order.detail}<span class="log-chip"><span class="log-chip-key">note:</span>{order.detail}</span>{/if}
  </div>
  {#if actions}
    {@render actions(order)}
  {/if}
</div>

<style>
  /* algo-status-pill base look + --st-fg / --st-bg / --st-border per-status
     variants are the SHARED SSOT in app.css (.algo-status-pill +
     .algo-status-card[data-status="…"]) — DO NOT redeclare them here.
     A local :global() copy previously repainted every .algo-status-card
     app-wide (e.g. the /automation agent cards), with different literal
     values than app.css, causing visible mismatches (P0 audit fix,
     2026-10). The only genuinely OrderCard-local status is "held",
     added to app.css's shared block instead of kept here. */
  .held-badge { color: var(--c-action); border-color: var(--c-action); }
  /* Account identity — STRIPE (not text colour, A2 2026-09 audit fix).
     Inline card header has far less horizontal room than a grid cell
     (LogPanel mounts at ≤360px), so this is a thinner 2px stripe + tight
     0.2rem padding rather than the 3px + 14% bg tint used on ag-Grid
     account columns — same identity signal, sized for the available
     space. --acct-color set inline per-order via $lib/account.js's
     acctColor(); falls back to transparent for orders with no account
     (e.g. some AlgoOrderInfo shapes). */
  :global(.oc-acct) {
    border-left: 2px solid var(--acct-color, transparent);
    padding-left: 0.2rem;
    color: var(--algo-slate);
    font-weight: 600;
  }
  /* complete / rejected / cancelled / running / error / inactive / held
     status colors all come from app.css's shared
     .algo-status-card[data-status="…"] block now — see comment above
     the .held-badge rule. */

  /* Slippage chip — neutral slate arrow glyph (↑/↓). Side-relative
     coloring was dropped: ↑ is good for SELL, bad for BUY, so
     green/red was misleading for one side. Arrow alone reads correctly. */
  :global(.log-chip-slip) { color: var(--algo-dim); }

  /* Tag colour-coding — manual ticket = sky-blue. 'tag-agent' rule
     removed (2026-10-18) — _tagClass() no longer emits it; see that
     function's own comment. */
  :global(.tag-manual)       { color: var(--algo-cyan-text); background: var(--c-info-14); }
  /* TP / parent / basket linkage chips */
  :global(.log-chip-tp)      { color: var(--c-long); background: var(--algo-green-bg); }
  :global(.log-chip-parent)  { color: var(--algo-sky); background: rgba(125, 211, 252, 0.10); }
  :global(.log-chip-child)   { color: var(--algo-violet); background: rgba(192, 132, 252, 0.12); }
  :global(.log-chip-basket)  { color: var(--c-action); background: rgba(251, 191, 36, 0.10); }

  /* Partial-fill chip — cyan palette (info/attention, not error).
     Shown when filled_quantity > 0 and < quantity on a non-terminal row.
     Matches the CANCEL_FAILED orange pill to signal "this row needs
     operator attention" without competing visually with the status pill. */
  :global(.log-chip-partial-fill) {
    color: var(--algo-sky);
    background: rgba(125, 211, 252, 0.12);
    border: 1px solid rgba(125, 211, 252, 0.40);
  }
.qty-unverified { color: var(--c-action); }
</style>