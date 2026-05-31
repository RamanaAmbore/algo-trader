<script>
  // CommandLineTab — grammar-driven command bar extracted from
  // /console/+page.svelte. Shared between the standalone Terminal page
  // and the OrderEntryShell's "Command line" tab.
  //
  // When embedded in the shell, `onParsedOrder` fires whenever the
  // operator types an order command (BUY/SELL …) so the shell can
  // switch to the Ticket tab pre-filled rather than opening a nested
  // modal.

  import { onMount } from 'svelte';
  import { authStore, executionMode } from '$lib/stores';
  import { get as getStore } from 'svelte/store';
  import { loadInstruments, getInstrument } from '$lib/data/instruments';
  import { loadAccounts } from '$lib/data/accounts';
  import { interpretAgent, placeTicketOrder, previewOrderMargin } from '$lib/api';
  import CommandBar from '$lib/CommandBar.svelte';
  import { aggFmt } from '$lib/format';
  import {
    orderGrammar,
    setQuoteLoadedCallback,
    previewSymbol,
    enrichOrderPairs,
    buildOrderPayload,
    getLtpForContext,
  } from '$lib/command/grammars/orders';

  /** @type {{
   *   onParsedOrder?: (props: any) => void,
   *   onAddToBasket?: (leg: any) => void,
   *   standalone?: boolean,
   *   prefillSide?: string, prefillAccount?: string, prefillSymbol?: string,
   *   prefillQty?: number,  prefillPrice?: number,  prefillOrderType?: string,
   * }} */
  let {
    onParsedOrder = /** @type {((props: any) => void) | undefined} */ (undefined),
    onAddToBasket = /** @type {((leg: any) => void) | undefined} */ (undefined),
    standalone    = false,
    // When the parent (OrderEntryShell) already knows context tokens,
    // we pre-fill the command bar with them so the operator only fills
    // in the missing slots. Empty strings / 0 / undefined are treated
    // as "no value" and skipped.
    prefillSide       = '',
    prefillAccount    = '',
    prefillSymbol     = '',
    prefillQty        = 0,
    prefillPrice      = 0,
    prefillOrderType  = '',
  } = $props();

  // Compose the initial command line from known context. Format follows
  // orderGrammar: `<verb> <account> <symbol> <qty> [<order_type> <price>]`.
  // Trailing space ensures the next token slot opens immediately.
  // Only pre-fills when the caller has supplied a *symbol* — without
  // a symbol the verb alone collides with whatever the operator types
  // (e.g. shell default `side='BUY'` → 'buy ' → operator types
  // 'buy NIFTY…' → bar reads 'buy buy NIFTY…' and the parser barfs).
  // Symbol-driven opens (chain pill / position-row click) carry context
  // worth pre-filling; symbol-less opens (Terminal / generic shell)
  // start the bar empty.
  const initialValue = (() => {
    if (!prefillSymbol) return '';
    const parts = [];
    const verb = String(prefillSide || '').toLowerCase();
    if (verb === 'buy' || verb === 'sell') parts.push(verb);
    if (prefillAccount) parts.push(String(prefillAccount));
    parts.push(String(prefillSymbol).toUpperCase());
    if (prefillQty > 0) parts.push(String(prefillQty));
    if (prefillOrderType && prefillOrderType.toUpperCase() !== 'LIMIT' &&
        prefillOrderType.toUpperCase() !== 'MARKET') {
      parts.push(prefillOrderType.toUpperCase());
    }
    if (prefillPrice > 0) parts.push(String(prefillPrice));
    return parts.join(' ') + ' ';
  })();

  /** @type {Array<{cmd: string, result: string, time: string}>} */
  let cmdHistory = $state([]);
  let running    = $state(false);
  let cmdVerb    = $state('');
  // Captures the latest parsed context — used to look up the LTP chip
  // shown above the CommandBar. Refreshed by `orderEnrichPairs` on
  // every keystroke that re-tokenises the input.
  let _cmdCtx    = $state(/** @type {any} */ ({}));
  let _ltp       = $state(/** @type {number|null} */ (null));
  // Margin / cash impact preview — same shape PreviewOrderMargin
  // surfaces inside OrderTicket. Auto-fetches when the parse has
  // enough fields to build a valid payload (symbol + side + qty,
  // and price when LIMIT). Cleared otherwise so the chip hides.
  let _marginPreview = $state(/** @type {any} */ (null));
  let _marginLoading = $state(false);
  let _marginTimer;
  let _lastMarginKey = '';
  // Intent set by the action button right before submit. 'place' fires
  // onParsedOrder (switches to Ticket tab); 'basket' fires onAddToBasket.
  /** @type {'place' | 'basket'} */
  let _intent    = 'place';

  // No open-orders context needed here — the tab is for fresh orders only.
  const cmdContext = { openOrders: [] };

  /** @type {any} */
  let cmdBar;

  onMount(() => {
    loadInstruments().catch(() => {});
    loadAccounts().catch(() => {});
    setQuoteLoadedCallback(() => {
      // Re-render the CommandBar so the price popup picks up newly
      // arrived quotes; ALSO refresh the LTP chip outside the popup
      // by re-resolving the cache.
      cmdBar?.refresh();
      _ltp = getLtpForContext(_cmdCtx);
    });
  });

  function authHeaders() {
    const token = $authStore.token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  function addResult(/** @type {string} */ cmd, /** @type {string} */ result) {
    // IST seconds for command history (trading-critical — operator may
    // need to correlate a manual command with a fill / agent event).
    // Tight inline display, so IST-only with explicit suffix; the wider
    // log surfaces (OrderTimelineDrawer, LogPanel) carry the full
    // dual-TZ form via `logTime` / `dualTsHtml`.
    const time = new Date().toLocaleTimeString('en-GB', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false, timeZone: 'Asia/Kolkata',
    }) + ' IST';
    cmdHistory = [{ cmd, result, time }, ...cmdHistory].slice(0, 200);
  }

  function orderEnrichPairs(pairs, ctx) {
    cmdVerb = (ctx?._verb || '').toUpperCase();
    _cmdCtx = ctx || {};
    // Look up LTP for the current parse context — re-fired by the
    // quote-loaded callback when the cache fills, so the chip flips
    // from "—" to the value without an extra keystroke.
    _ltp = getLtpForContext(_cmdCtx);
    _scheduleMarginPreview();
    return enrichOrderPairs(pairs, ctx);
  }

  // Schedules a debounced previewOrderMargin call when the parse has
  // enough info to build a payload. Same shape OrderTicket uses, so
  // the operator sees the same MARGIN / Avail / After breakdown
  // without flipping to the Ticket tab.
  function _scheduleMarginPreview() {
    if (_marginTimer) clearTimeout(_marginTimer);
    _marginTimer = setTimeout(async () => {
      try {
        const ctx = _cmdCtx;
        if (!ctx?.symbol || !ctx?._verb || !ctx?.qty) {
          _marginPreview = null;
          return;
        }
        // Resolve to a tradingsymbol via the same helper buildOrderPayload uses.
        const payload = buildOrderPayload({ verb: ctx._verb, args: ctx, kwargs: ctx });
        if (!payload) { _marginPreview = null; return; }
        const orderType = payload.order_type || 'LIMIT';
        if (orderType === 'LIMIT' && !(payload.price > 0)) {
          // No usable limit yet → skip until the operator types a price.
          _marginPreview = null;
          return;
        }
        const key = JSON.stringify({
          a: payload.account, s: payload.tradingsymbol, q: payload.quantity,
          t: orderType, side: payload.transaction_type,
          p: payload.price || 0, tp: payload.trigger_price || 0,
        });
        if (key === _lastMarginKey && _marginPreview) return;
        _lastMarginKey = key;
        _marginLoading = true;
        _marginPreview = await previewOrderMargin({
          account:       payload.account,
          tradingsymbol: payload.tradingsymbol,
          exchange:      payload.exchange,
          quantity:      payload.quantity,
          side:          payload.transaction_type,
          order_type:    orderType,
          product:       payload.product,
          variety:       payload.variety || 'regular',
          price:         payload.price || 0,
          trigger_price: payload.trigger_price || 0,
        });
      } catch (e) {
        _marginPreview = { error: (/** @type {any} */ (e)?.message || 'preview failed').slice(0, 60) };
      } finally {
        _marginLoading = false;
      }
    }, 400);
  }

  // onsubmitRaw fires for every submit (even when the grammar can't parse it),
  // carrying `_line` = the raw textarea text. Used for agent + shell commands
  // that don't match the order grammar verbs.
  async function runRaw(parsed) {
    const raw = (parsed._line || '').trim();
    if (!raw) return;
    const verb = (raw.split(/\s+/)[0] || '').toLowerCase();

    // Order verbs are handled by onsubmit (runParsed) — skip here.
    if (verb === 'buy' || verb === 'sell' || verb === 'cancel' || verb === 'modify') return;

    running = true;
    try {
      // Agent command
      if (verb === 'agent') {
        try {
          const d = await interpretAgent(raw);
          addResult(raw, d.output || d.detail || 'No output');
        } catch (e) { addResult(raw, `ERROR: ${/** @type {any} */ (e).message}`); }
        cmdBar?.clear(); cmdVerb = '';
        return;
      }

      // Shell command
      try {
        const res = await fetch('/api/admin/exec', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ command: raw }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) { addResult(raw, d.detail || 'Error'); }
        else {
          let out = (d.stdout || '') + (d.stderr ? '\n[stderr]\n' + d.stderr : '');
          if (!out.trim()) out = `[exit ${d.returncode}]`;
          addResult(raw, out);
        }
      } catch (e) { addResult(raw, /** @type {any} */ (e).message); }
      cmdBar?.clear(); cmdVerb = '';
    } finally {
      running = false;
    }
  }

  // onsubmit fires only when the grammar validates cleanly (buy/sell/cancel/modify).
  async function runParsed(parsed) {
    running = true;
    const raw = (parsed._line || `${parsed.verb} …`).trim();
    try {
      const verb = parsed.verb || '';

      // Order command — build props and route
      if (verb === 'buy' || verb === 'sell') {
        const payload = buildOrderPayload(parsed);
        if (!payload) throw new Error(`couldn't build order payload`);
        const sym  = String(payload.tradingsymbol || '').toUpperCase();
        const inst = getInstrument(sym);
        const lot  = Number(inst?.ls || 1);

        // Basket intent: add a leg directly without switching tabs.
        if (_intent === 'basket' && onAddToBasket) {
          _intent = 'place';   // reset for next submit
          const hasPrice = payload.price > 0;
          const leg = {
            key:      `cmd|${payload.transaction_type}|${sym}|${Date.now()}`,
            side:     /** @type {'BUY'|'SELL'} */ (payload.transaction_type),
            sym,
            exchange: payload.exchange || inst?.e || 'NFO',
            account:  String(payload.account || prefillAccount || ''),
            lots:     Math.max(1, lot > 0 ? Math.round((Number(payload.quantity) || lot) / lot) : 1),
            lotSize:  lot,
            product:  payload.product || 'NRML',
            limit:    hasPrice ? Number(payload.price) : 0,
            chaseAgg: /** @type {'low'|'med'|'high'} */ ('low'),
          };
          addResult(raw, `Added to basket: ${leg.side} ${leg.lots * leg.lotSize} ${sym}`);
          cmdBar?.clear(); cmdVerb = '';
          running = false;
          onAddToBasket(leg);
          return;
        }

        // Place intent (Enter / Submit button) — fires the order
        // directly via placeTicketOrder instead of opening the Ticket
        // tab for review. Operator already typed everything explicitly;
        // forcing a second confirmation slows the keyboard-first flow.
        // The margin chip above the bar surfaces the impact BEFORE
        // submit so the operator isn't flying blind.
        try {
          // Read the executionMode store so command-line submits
          // route through the operator's active mode (paper / live /
          // shadow / sim / replay) instead of a hardcoded 'live'.
          // Defaults to 'paper' if the store hasn't been hydrated.
          // Backend still enforces is_prod_branch() + paper_trading_
          // mode gates even when the frontend asks for 'live'.
          const _mode = /** @type {'paper'|'live'|'shadow'|'sim'|'replay'} */ (getStore(executionMode) || 'paper');
          const resp = await placeTicketOrder({
            mode:             _mode,
            side:             payload.transaction_type,
            tradingsymbol:    sym,
            exchange:         payload.exchange || inst?.e || 'NFO',
            quantity:         Number(payload.quantity) || 0,
            product:          payload.product,
            order_type:       payload.order_type || 'LIMIT',
            variety:          payload.variety || 'regular',
            validity:         'DAY',
            price:            payload.price > 0 ? payload.price : null,
            trigger_price:    payload.trigger_price > 0 ? payload.trigger_price : null,
            account:          String(payload.account || ''),
          });
          const oid = resp?.order_id || resp?.id || '';
          addResult(
            raw,
            `✓ ${payload.transaction_type} ${payload.quantity} ${sym} placed${oid ? ' · #' + oid : ''}`,
          );
        } catch (e) {
          addResult(raw, `✗ ${/** @type {any} */ (e)?.message || 'place failed'}`);
        }
        cmdBar?.clear(); cmdVerb = '';
        _marginPreview = null;
        running = false;
        return;
      }
    } catch (e) {
      addResult(raw, /** @type {any} */ (e).message);
    } finally {
      running = false;
    }
  }

  // Expose cmdHistory so standalone Terminal page can pass it to LogPanel.
  export { cmdHistory };
</script>

<div class="clt-root" class:clt-standalone={standalone}>
  {#if _cmdCtx?.symbol}
    <!-- Pre-submit info strip — LTP + margin / cash impact for the
         currently parsed command. Operator can read the trade
         economics before pressing Enter or clicking Submit.
         Previously LTP lived inline as " ◀ LTP" in the price popup;
         lifted out here per operator request. The margin / cash
         impact mirrors what OrderTicket shows, computed via the
         same previewOrderMargin call. -->
    <div class="clt-info-strip">
      <span class="clt-chip clt-chip-ltp">
        <span class="clt-chip-label">LTP</span>
        <span class="clt-chip-val">{_ltp != null ? '₹' + _ltp.toFixed(2) : '—'}</span>
        <span class="clt-chip-sym">{_cmdCtx.symbol}{_cmdCtx.strike ? ' ' + _cmdCtx.strike : ''}{_cmdCtx.instType ? ' ' + _cmdCtx.instType : ''}</span>
      </span>
      {#if _marginLoading}
        <span class="clt-chip clt-chip-margin clt-chip-loading">
          <span class="clt-chip-label">MARGIN</span>
          <span class="clt-chip-val">…</span>
        </span>
      {:else if _marginPreview?.error}
        <span class="clt-chip clt-chip-margin clt-chip-err">
          <span class="clt-chip-label">MARGIN</span>
          <span class="clt-chip-val">⚠ {_marginPreview.error}</span>
        </span>
      {:else if _marginPreview && _marginPreview.required_margin != null}
        {@const _required  = Number(_marginPreview.required_margin)  || 0}
        {@const _available = Number(_marginPreview.available_margin) || 0}
        {@const _after     = _available - _required}
        {@const _afterPct  = _available > 0 ? (_after / _available) * 100 : 0}
        {@const _afterCls  = _after < 0     ? 'clt-chip-err'
                            : _afterPct < 10 ? 'clt-chip-err'
                            : _afterPct < 40 ? 'clt-chip-warn'
                            : 'clt-chip-ok'}
        <span class="clt-chip clt-chip-margin">
          <span class="clt-chip-label">MARGIN</span>
          <span class="clt-chip-val">₹{aggFmt(_required)}</span>
        </span>
        <span class="clt-chip clt-chip-margin">
          <span class="clt-chip-label">AVAIL</span>
          <span class="clt-chip-val">₹{aggFmt(_available)}</span>
        </span>
        <span class="clt-chip clt-chip-margin {_afterCls}">
          <span class="clt-chip-label">AFTER</span>
          <span class="clt-chip-val">{_after < 0 ? '−' : ''}₹{aggFmt(Math.abs(_after))}</span>
        </span>
      {/if}
    </div>
  {/if}
  <div class="relative mb-2">
    <CommandBar
      bind:this={cmdBar}
      grammar={orderGrammar}
      context={cmdContext}
      rows={2}
      placeholder="buy | sell | agent | shell command"
      {initialValue}
      onsubmit={runParsed}
      onsubmitRaw={runRaw}
      previewFn={previewSymbol}
      enrichPairs={orderEnrichPairs}
      disabled={running}
    />
    <div class="absolute bottom-1 right-2 flex gap-1 z-10">
      {#if onAddToBasket}
        <button onclick={() => { _intent = 'basket'; cmdBar?.submit(); }}
          disabled={running || !cmdVerb}
          title="Add to basket — accumulate this order, fire all together via Submit below"
          class="sim-btn sim-btn-order sim-btn-basket disabled:opacity-40">+ Basket</button>
      {/if}
      <button onclick={() => { _intent = 'place'; cmdBar?.submit(); }} disabled={running || !cmdVerb}
        title="Submit — place this single order immediately (same as pressing Enter)"
        class="sim-btn sim-btn-order
          {cmdVerb === 'SELL' ? 'sim-btn-danger' : 'sim-btn-primary'}
          disabled:opacity-40">
        Submit
      </button>
      <button onclick={() => { cmdBar?.clear(); cmdVerb = ''; _marginPreview = null; }}
        title="Clear the command bar" aria-label="Clear command bar"
        class="clt-clear-btn">×</button>
    </div>
  </div>
  {#if standalone}
    <div class="text-[0.5rem] text-muted mb-1">
      <code>buy|sell ACCT SYMBOL QTY [LIMIT PRICE]</code> · <code>agent list|status|activate|config</code> · shell
    </div>
  {/if}

  <!-- Scrollable output history -->
  {#if cmdHistory.length}
    <div class="clt-history">
      {#each cmdHistory as row (row.time + row.cmd)}
        <div class="clt-row">
          <span class="clt-time">{row.time}</span>
          <span class="clt-cmd">&gt; {row.cmd}</span>
          <pre class="clt-result">{row.result}</pre>
        </div>
      {/each}
    </div>
  {:else}
    <div class="clt-empty">No commands yet.</div>
  {/if}
</div>

<style>
  .clt-root { display: flex; flex-direction: column; }
  .clt-standalone { /* no extra styles needed — caller owns layout */ }

  /* Drop CommandBar's amber 2px left-edge accent inside the
     command-line tab body. The parent /orders Order Entry card
     carries the accent at the card level — duplicating it inside
     the tab body created nested visual hierarchy that the operator
     flagged. Global override targets the .cmd-chips-area and
     .cmd-container scoped CSS classes. */
  :global(.clt-root .cmd-chips-area),
  :global(.clt-root .cmd-container) {
    border-left: 1px solid #334155 !important;
  }
  :global(.clt-root .cmd-container:focus-within) {
    border-left: 1px solid rgba(251, 191, 36, 0.40) !important;
  }

  /* Pre-submit info strip — LTP + margin / cash chips above the
     CommandBar. Operator reads "what does my book look like before
     and after this trade?" at a glance. Same chip vocabulary the
     OrderTicket form uses so the visual identity carries across
     entry methods. */
  .clt-info-strip {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 0.35rem;
  }
  .clt-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    font-size: 0.62rem;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
  }
  .clt-chip-label {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #7e97b8;
  }
  .clt-chip-val {
    color: #f1f7ff;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .clt-chip-sym {
    color: #7e97b8;
    font-weight: 600;
  }
  .clt-chip-ltp {
    background: rgba(125, 211, 252, 0.08);
    border-color: rgba(125, 211, 252, 0.30);
  }
  .clt-chip-ltp .clt-chip-label { color: #7dd3fc; }
  .clt-chip-margin {
    background: rgba(251, 191, 36, 0.06);
    border-color: rgba(251, 191, 36, 0.25);
  }
  .clt-chip-margin .clt-chip-label { color: #fbbf24; }
  .clt-chip-ok      { border-color: rgba(74, 222, 128, 0.45); }
  .clt-chip-ok .clt-chip-val      { color: #4ade80; }
  .clt-chip-warn    { border-color: rgba(251, 191, 36, 0.55); }
  .clt-chip-warn .clt-chip-val    { color: #fbbf24; }
  .clt-chip-err     { border-color: rgba(248, 113, 113, 0.55); background: rgba(248, 113, 113, 0.06); }
  .clt-chip-err .clt-chip-val     { color: #f87171; }
  .clt-chip-loading .clt-chip-val { color: #94a3b8; }

  /* Compact clear button — replaces the wider "Clear" text button.
     Same cyan-400 palette family as the card-control trio so the
     three button shapes (×, +Basket, Submit) read as one cluster. */
  .clt-clear-btn {
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 3px;
    color: #94a3b8;
    font-size: 0.85rem;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .clt-clear-btn:hover {
    background: rgba(126, 151, 184, 0.18);
    color: #c8d8f0;
    border-color: rgba(126, 151, 184, 0.55);
  }

  /* + Basket button — green outline matching the basket palette. */
  :global(.sim-btn-basket) {
    background: rgba(74,222,128,0.10);
    color: #4ade80;
    border-color: rgba(74,222,128,0.55);
    font-weight: 700;
  }
  :global(.sim-btn-basket:hover:not(:disabled)) {
    background: rgba(74,222,128,0.20);
    border-color: #4ade80;
  }

  .clt-history {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-height: 18rem;
    overflow-y: auto;
    margin-top: 0.4rem;
    padding-right: 0.2rem;
  }
  .clt-row {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 3px;
    padding: 0.3rem 0.5rem;
  }
  .clt-time {
    font-family: monospace;
    font-size: 0.55rem;
    color: #7e97b8;
    margin-right: 0.4rem;
  }
  .clt-cmd {
    font-family: monospace;
    font-size: 0.65rem;
    font-weight: 600;
    color: #fbbf24;
  }
  .clt-result {
    font-family: monospace;
    font-size: 0.62rem;
    color: #c8d8f0;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0.2rem 0 0;
    padding: 0;
    border: none;
    background: transparent;
  }
  .clt-empty {
    font-size: 0.62rem;
    color: #7e97b8;
    font-style: italic;
    margin-top: 0.5rem;
  }
</style>
