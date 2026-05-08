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
  import { authStore } from '$lib/stores';
  import { loadInstruments, getInstrument } from '$lib/data/instruments';
  import { loadAccounts } from '$lib/data/accounts';
  import { interpretAgent } from '$lib/api';
  import CommandBar from '$lib/CommandBar.svelte';
  import {
    orderGrammar,
    setQuoteLoadedCallback,
    previewSymbol,
    enrichOrderPairs,
    buildOrderPayload,
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
  const initialValue = (() => {
    const parts = [];
    const verb = String(prefillSide || '').toLowerCase();
    if (verb === 'buy' || verb === 'sell') parts.push(verb);
    if (prefillAccount) parts.push(String(prefillAccount));
    if (prefillSymbol)  parts.push(String(prefillSymbol).toUpperCase());
    if (prefillQty > 0) parts.push(String(prefillQty));
    if (prefillOrderType && prefillOrderType.toUpperCase() !== 'LIMIT' &&
        prefillOrderType.toUpperCase() !== 'MARKET') {
      parts.push(prefillOrderType.toUpperCase());
    }
    if (prefillPrice > 0) parts.push(String(prefillPrice));
    return parts.length ? parts.join(' ') + ' ' : '';
  })();

  /** @type {Array<{cmd: string, result: string, time: string}>} */
  let cmdHistory = $state([]);
  let running    = $state(false);
  let cmdVerb    = $state('');
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
    setQuoteLoadedCallback(() => cmdBar?.refresh());
  });

  function authHeaders() {
    const token = $authStore.token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  function addResult(/** @type {string} */ cmd, /** @type {string} */ result) {
    const time = new Date().toLocaleTimeString('en-IN', { hour12: false });
    cmdHistory = [{ cmd, result, time }, ...cmdHistory].slice(0, 200);
  }

  function orderEnrichPairs(pairs, ctx) {
    cmdVerb = (ctx?._verb || '').toUpperCase();
    return enrichOrderPairs(pairs, ctx);
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

        const props = {
          symbol:         sym,
          exchange:       payload.exchange || inst?.e || 'NFO',
          side:           payload.transaction_type,
          action:         'open',
          qty:            Number(payload.quantity) || 0,
          lotSize:        lot,
          orderType:      payload.order_type || 'LIMIT',
          price:          payload.price > 0 ? payload.price : undefined,
          trigger:        payload.trigger_price > 0 ? payload.trigger_price : undefined,
          product:        payload.product,
          accounts:       [],
          account:        String(payload.account || ''),
          defaultMode:    'live',
          availableModes: ['live'],
          _origCommand:   raw,
        };
        addResult(
          raw,
          `Opening ticket: ${payload.transaction_type} ${payload.quantity} ${sym} on ${payload.exchange}`,
        );
        cmdBar?.clear(); cmdVerb = '';
        running = false;
        if (onParsedOrder) onParsedOrder(props);
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
      <button onclick={() => { _intent = 'place'; cmdBar?.submit(); }} disabled={running}
        class="sim-btn sim-btn-order
          {cmdVerb === 'SELL' ? 'sim-btn-danger' : 'sim-btn-primary'}
          disabled:opacity-40">
        {cmdVerb === 'BUY' ? 'BUY' : cmdVerb === 'SELL' ? 'SELL' : 'Run'}
      </button>
      {#if onAddToBasket}
        <button onclick={() => { _intent = 'basket'; cmdBar?.submit(); }}
          disabled={running || !cmdVerb}
          title="Add to basket — place every leg together later"
          class="sim-btn sim-btn-order sim-btn-basket disabled:opacity-40">+ Basket</button>
      {/if}
      <button onclick={() => { cmdBar?.clear(); cmdVerb = ''; }}
        class="sim-btn sim-btn-order sim-btn-secondary">Clear</button>
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
