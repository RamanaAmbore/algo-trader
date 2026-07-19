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
  import { retryTemplateAttach } from '$lib/api';

  // Re-attach button state — per-card spinner + inline note. The note
  // disappears the next time the parent OrderCard re-renders with a
  // fresh order prop (typical poll cadence ≤5s) so it stays ephemeral.
  let _retrying = $state(false);
  let _retryNote = $state('');

  let {
    /** @type {any} */                                   order,
    /** @type {((o:any) => void) | undefined} */         onCardClick   = undefined,
    /** @type {((o:any, e:Event) => void) | undefined} */onSymbolClick = undefined,
    /** @type {((o:any, e:MouseEvent) => void) | undefined} */
                                                          onSymbolContext = undefined,
    /** @type {import('svelte').Snippet | undefined} */  actions       = undefined,
  } = $props();

  // Status → data-status attribute. .algo-status-card reads this for
  // the left-edge stripe colour; .algo-status-pill child inherits
  // --st-fg / --st-bg / --st-border via the parent's data-status.
  /** @param {string} s */
  function _statusDataAttr(s) {
    const c = (s || '').toUpperCase();
    if (c === 'COMPLETE' || c === 'FILLED')            return 'complete';
    if (c === 'REJECTED')                              return 'rejected';
    if (c === 'CANCELLED')                             return 'cancelled';
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

  // Per-account hue — same algorithm /orders uses so the same account
  // gets the same tint on both surfaces. Module-level cache keeps the
  // assignment stable across mounts.
  // Audit fix — switched from unbounded `string[]` + O(n) indexOf to a
  // bounded Map<acct, idx> with O(1) lookup. Pre-fix the array grew
  // by one entry per unique account seen across all OrderCard
  // instances and `_acctList.indexOf(a)` ran O(n) on every render.
  // In LogPanel with 50 orders polling at 3 s that was 150 indexOf
  // searches per cycle. With a Map it's a single hash lookup; the
  // Map size is capped by the number of unique accounts (handful in
  // practice), so unbounded growth isn't a real concern but using
  // Map makes the bound explicit + the lookup constant-time.
  const _ACCT_COLORS = ['text-sky-300', 'text-amber-300', 'text-fuchsia-300', 'text-teal-300'];
  /** @type {Map<string, number>} */
  const _ACCT_IDX = new Map();
  /** @param {string} a */
  function _acctColor(a) {
    let idx = _ACCT_IDX.get(a);
    if (idx === undefined) { idx = _ACCT_IDX.size; _ACCT_IDX.set(a, idx); }
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
    <!-- Status pill — color driven by parent .algo-status-card[data-status]
         via --st-fg / --st-bg / --st-border CSS vars. CANCEL_FAILED gets
         a ⚠ prefix and tooltip so the operator can distinguish it from a
         clean CANCELLED row. -->
    <span class="algo-status-pill"
      title={order.status === 'CANCEL_FAILED'
        ? 'Kill attempt failed — order may still be live at broker. Reconcile or retry kill.'
        : ''}>{order.status === 'CANCEL_FAILED' ? '⚠ KILL FAILED' : order.status}</span>
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
    {#if _ts}<span class="log-chip"><span class="log-chip-key">time:</span>{formatDualTz(new Date(_ts))}</span>{/if}
    {#if order.tag}<span class="log-chip {_tagClass(order.tag)}"><span class="log-chip-key">tag:</span>{order.tag}</span>{/if}
    {#if order.target_pct != null}<span class="log-chip log-chip-tp"><span class="log-chip-key">tp:</span>+{(Number(order.target_pct) * 100).toFixed(1)}%</span>{/if}
    {#if order.template_id != null}
      <!-- Audit fix (H-3) — distinguish full attach vs partial. Pre-fix
           `attached_gtts_json != null` = ✓ (full). A row where TP
           attached but the wing failed (e.g. low-OI chain scan)
           still has attached_gtts_json populated; the chip showed ✓
           even though the wing didn't land. Now inspect the JSON for
           a wing entry and render `✓+w` (full, with wing), `✓` (full,
           no wing was expected), or `✓⚠` (partial — at least one
           GTT spec missing its broker id). Operator can hover for the
           full JSON breakdown. -->
      {@const _atJson = order.attached_gtts_json}
      {@const _at = (() => {
        if (!_atJson) return null;
        try { const a = JSON.parse(_atJson); return Array.isArray(a) ? a : null; } catch { return null; }
      })()}
      {@const _hasWing = !!(_at && _at.some(e => e?.kind === 'wing' && e.id))}
      {@const _gttCount = _at ? _at.filter(e => e?.kind === 'gtt').length : 0}
      {@const _missingId = !!(_at && _at.some(e => e?.kind === 'gtt' && !e.id))}
      {@const _chipBadge = !_atJson
        ? (order.status === 'FILLED' ? ' ⟳' : '…')
        : (_missingId ? ' ✓⚠' : (_hasWing ? ' ✓+w' : (_gttCount > 0 ? ' ✓' : ' ✓∅')))}
      {@const _atSummary = (() => {
        // Build a human-readable summary of the attached GTT specs instead
        // of dumping raw JSON into the title attribute. Each entry has
        // `kind` ('gtt'|'wing'), `label` ('TP'/'SL'/'TP+SL'), and `id`.
        if (!_at || !_at.length) return '';
        const lines = [];
        for (const e of _at) {
          if (e?.kind === 'gtt') {
            const idStr = e.id ? `#${String(e.id).slice(-8)}` : 'no id';
            const trail = e.sl_trail_pct != null ? ` trail ${e.sl_trail_pct}%` : '';
            lines.push(`${e.label || 'GTT'}${trail} · ${idStr}`);
          } else if (e?.kind === 'wing') {
            const idStr = e.id ? `#${String(e.id).slice(-8)}` : 'no id';
            lines.push(`Wing BUY · ${idStr}`);
          }
        }
        return lines.join(' | ');
      })()}
      <span class="log-chip log-chip-template"
            class:log-chip-template-partial={_missingId}
            title={_atJson
              ? `Template attached on fill — ${_gttCount} GTT spec(s)${_hasWing ? ', wing attached' : ''}${_missingId ? '. ⚠ At least one spec is missing its broker id — partial attach.' : '.'} ${_atSummary}`
              : (order.status === 'FILLED'
                  ? 'Template was selected but attach did not run — click Re-attach to retry.'
                  : 'Template selected — will attach on fill')}>
        <span class="log-chip-key">tmpl:</span>#{order.template_id}{_chipBadge}
      </span>
    {/if}
    {#if order.template_id != null && (order.status || '').toUpperCase() === 'FILLED' && !order.attached_gtts_json}
      <button type="button"
              class="log-chip log-chip-retry-attach"
              disabled={_retrying}
              title="Re-run template attach against this filled parent — useful when the original attach silently dropped the wing (low OI etc.)."
              onclick={async () => {
                _retrying = true;
                try {
                  const r = await retryTemplateAttach(order.id);
                  if (r?.ok) {
                    _retryNote = 'Re-attach OK' + (r.wing_order_id ? ` · wing #${String(r.wing_order_id).slice(-6)}` : '');
                  } else {
                    _retryNote = `Re-attach skipped: ${r?.reason || 'unknown'}`;
                  }
                } catch (e) {
                  _retryNote = `Re-attach failed: ${e?.message || e}`;
                } finally {
                  _retrying = false;
                }
              }}>
        {_retrying ? '…' : '⟳ Re-attach'}
      </button>
    {/if}
    {#if _retryNote}<span class="log-chip log-chip-retry-note">{_retryNote}</span>{/if}
    {#if order.parent_order_id != null}<span class="log-chip log-chip-parent" title="This row is an auto-attached leg of the listed parent order (typically the protective wing of a SELL option)."><span class="log-chip-key">parent:</span>#{String(order.parent_order_id).slice(-6)}</span>{/if}
    {#if order.child_order_ids && order.child_order_ids.length}
      <span class="log-chip log-chip-child"
            title={`Auto-attached children of this order (typically the protective wing). Click any id in the activity log to inspect.`}>
        <span class="log-chip-key">{order.child_order_ids.length === 1 ? 'wing:' : 'wings:'}</span>
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
  /* algo-status-pill — reads --st-fg / --st-bg / --st-border set by
     the parent .algo-status-card[data-status="…"] so every new status
     variant only needs a CSS var block here, not a conditional class.
     Falls back to amber (running/default) when no data-status matches. */
  :global(.algo-status-pill) {
    font-size: 0.55rem;
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
    border: 1px solid var(--st-border, rgba(251,191,36,0.40));
    background: var(--st-bg, rgba(251,191,36,0.12));
    color: var(--st-fg, #fbbf24);
  }
  /* Per-status CSS var blocks — parent .algo-status-card carries
     data-status; child .algo-status-pill inherits via cascade. */
  :global(.algo-status-card[data-status="complete"])  {
    --st-fg:     #4ade80;
    --st-bg:     rgba(74,222,128,0.12);
    --st-border: rgba(74,222,128,0.38);
  }
  :global(.algo-status-card[data-status="rejected"])  {
    --st-fg:     #f87171;
    --st-bg:     rgba(248,113,113,0.12);
    --st-border: rgba(248,113,113,0.38);
  }
  :global(.algo-status-card[data-status="cancelled"]) {
    --st-fg:     #94a3b8;
    --st-bg:     rgba(100,116,139,0.15);
    --st-border: rgba(100,116,139,0.38);
  }
  :global(.algo-status-card[data-status="running"])   {
    --st-fg:     #fbbf24;
    --st-bg:     rgba(251,191,36,0.12);
    --st-border: rgba(251,191,36,0.40);
  }
  :global(.algo-status-card[data-status="error"])     {
    --st-fg:     #fb923c;
    --st-bg:     rgba(220,38,38,0.18);
    --st-border: rgba(220,38,38,0.55);
  }
  :global(.algo-status-card[data-status="inactive"])  {
    --st-fg:     #94a3b8;
    --st-bg:     rgba(100,116,139,0.12);
    --st-border: rgba(100,116,139,0.28);
  }

  /* Slippage chip — neutral slate arrow glyph (↑/↓). Side-relative
     coloring was dropped: ↑ is good for SELL, bad for BUY, so
     green/red was misleading for one side. Arrow alone reads correctly. */
  :global(.log-chip-slip) { color: #94a3b8; }

  /* Tag colour-coding — manual ticket = sky-blue, agent-fired = amber. */
  :global(.tag-manual)       { color: #67e8f9; background: var(--c-info-14); }
  :global(.tag-agent)        { color: var(--c-action); background: rgba(251, 191, 36, 0.10); }
  /* TP / parent / basket linkage chips */
  :global(.log-chip-tp)      { color: var(--c-long); background: var(--algo-green-bg); }
  :global(.log-chip-template) { color: #c084fc; background: rgba(192, 132, 252, 0.12); }
  :global(.log-chip-parent)  { color: #7dd3fc; background: rgba(125, 211, 252, 0.10); }
  :global(.log-chip-child)   { color: #c084fc; background: rgba(192, 132, 252, 0.12); }
  :global(.log-chip-basket)  { color: var(--c-action); background: rgba(251, 191, 36, 0.10); }

  /* Re-attach button — same chip shape as the rest of the row so it
     visually slots in next to the template chip. Cyan-400 (cyan)
     palette since it's an active action (vs purple's "informational"
     template chip). Disabled while in-flight. */
  :global(.log-chip-retry-attach) {
    color: var(--c-info);
    background: var(--c-info-14);
    border: 1px solid rgba(34, 211, 238, 0.45);
    cursor: pointer;
  }
  :global(.log-chip-retry-attach:hover) {
    background: rgba(34, 211, 238, 0.18);
    border-color: rgba(34, 211, 238, 0.75);
  }
  :global(.log-chip-retry-attach:disabled) {
    opacity: 0.5;
    cursor: wait;
  }
  :global(.log-chip-retry-note) {
    color: var(--text-muted);
    background: rgba(126, 151, 184, 0.10);
    font-style: italic;
  }
  /* Partial-fill chip — cyan palette (info/attention, not error).
     Shown when filled_quantity > 0 and < quantity on a non-terminal row.
     Matches the CANCEL_FAILED orange pill to signal "this row needs
     operator attention" without competing visually with the status pill. */
  :global(.log-chip-partial-fill) {
    color: #7dd3fc;
    background: rgba(125, 211, 252, 0.12);
    border: 1px solid rgba(125, 211, 252, 0.40);
  }
</style>