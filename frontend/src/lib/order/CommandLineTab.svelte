<script>
  // CommandLineTab — command input + output area extracted from
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
  import { interpretAgent } from '$lib/api';

  /** @type {{
   *   onParsedOrder?: (props: any) => void,
   *   standalone?: boolean,
   * }} */
  let {
    // When set, order commands route through this callback instead of
    // opening a nested OrderTicket. The shell switches to the Ticket
    // tab with the pre-filled props. Standalone Terminal page leaves
    // this undefined and handles the ticket itself.
    onParsedOrder = /** @type {((props: any) => void) | undefined} */ (undefined),
    // True when rendered as the full /console page body — shows extra
    // layout padding and the help hint line.
    standalone = false,
  } = $props();

  let command    = $state('');
  /** @type {Array<{cmd: string, result: string, time: string}>} */
  let cmdHistory = $state([]);
  let running    = $state(false);

  onMount(() => { loadInstruments().catch(() => {}); });

  function authHeaders() {
    const token = $authStore.token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  function parseOrder(/** @type {string} */ cmd) {
    const parts = cmd.trim().split(/\s+/);
    if (parts.length < 4) return null;
    const txn = parts[0].toUpperCase();
    if (txn !== 'BUY' && txn !== 'SELL') return null;
    return {
      transaction_type: txn, account: parts[1], tradingsymbol: parts[2],
      quantity: parseInt(parts[3]) || 0, order_type: (parts[4] || 'LIMIT').toUpperCase(),
      price: parseFloat(parts[5]) || 0, exchange: 'NFO', product: 'NRML',
      variety: 'regular', validity: 'DAY',
    };
  }

  function addResult(/** @type {string} */ cmd, /** @type {string} */ result) {
    const time = new Date().toLocaleTimeString('en-IN', { hour12: false });
    cmdHistory = [{ cmd, result, time }, ...cmdHistory].slice(0, 200);
  }

  async function runCommand() {
    if (!command.trim()) return;
    const cmd = command.trim();
    running = true;

    // Agent command
    if (cmd.toLowerCase().startsWith('agent ')) {
      try {
        const d = await interpretAgent(cmd);
        addResult(cmd, d.output || d.detail || 'No output');
      } catch (e) { addResult(cmd, `ERROR: ${/** @type {any} */ (e).message}`); }
      finally { running = false; command = ''; }
      return;
    }

    // Order command — parse and route
    const order = parseOrder(cmd);
    if (order) {
      const sym  = String(order.tradingsymbol || '').toUpperCase();
      const inst = getInstrument(sym);
      const exch = inst?.e || order.exchange || 'NFO';
      const lot  = Number(inst?.ls || 1);
      const props = {
        symbol:         sym,
        exchange:       exch,
        side:           order.transaction_type,
        action:         'open',
        qty:            Number(order.quantity) || 0,
        lotSize:        lot,
        orderType:      order.order_type || 'LIMIT',
        price:          order.price > 0 ? order.price : undefined,
        product:        order.product,
        accounts:       [],
        account:        String(order.account || ''),
        defaultMode:    'live',
        availableModes: ['live'],
        _origCommand:   cmd,
      };
      addResult(cmd, `Opening ticket: ${order.transaction_type} ${order.quantity} ${sym} on ${exch}`);
      running = false; command = '';
      if (onParsedOrder) {
        onParsedOrder(props);
      }
      return;
    }

    // Shell command
    try {
      const res = await fetch('/api/admin/exec', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ command: cmd }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { addResult(cmd, d.detail || 'Error'); }
      else {
        let out = (d.stdout || '') + (d.stderr ? '\n[stderr]\n' + d.stderr : '');
        if (!out.trim()) out = `[exit ${d.returncode}]`;
        addResult(cmd, out);
      }
    } catch (e) { addResult(cmd, /** @type {any} */ (e).message); }
    finally { running = false; command = ''; }
  }

  // Expose cmdHistory so standalone Terminal page can pass it to LogPanel.
  export { cmdHistory };
</script>

<div class="clt-root" class:clt-standalone={standalone}>
  <div class="relative mb-2">
    <textarea
      bind:value={command}
      class="field-input cmd-input font-mono text-xs w-full"
      style="height:8rem; padding-bottom:1.5rem"
      placeholder="Shell command, order (buy/sell), or agent command"
      onkeydown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); runCommand(); } }}
    ></textarea>
    <div class="absolute bottom-3 right-2 flex gap-1 z-10">
      <button onclick={runCommand} disabled={running}
        class="sim-btn sim-btn-order sim-btn-primary disabled:opacity-40">{running ? '...' : 'Run'}</button>
      <button onclick={() => { command = ''; }}
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
