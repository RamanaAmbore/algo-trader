<script>
  /**
   * ChaseCard — live view of every OPEN algo_orders row across paper /
   * live / shadow modes, plus a per-row Kill action. Reusable surface
   * for the /orders page and any future panel that wants to track
   * in-flight chases.
   *
   * Operator: "add a card in orders for chase and kill functionality".
   * Reads /api/orders/chases/active every `pollMs` (default 3 s) and
   * invokes /api/orders/chases/{id}/kill on click.
   *
   * Props
   *   pollMs?       — poll interval (default 3000)
   *   compact?      — hide age / mode badge for narrow surfaces
   *   onKilled?     — callback fired after a successful kill
   */
  import { onMount, onDestroy } from 'svelte';
  import { fetchActiveChases, killChase } from '$lib/api';
  import { priceFmt } from '$lib/format';

  let {
    pollMs   = 3000,
    compact  = false,
    onKilled = null,
  } = $props();

  let _chases  = $state(/** @type {any[]} */ ([]));
  let _loading = $state(false);
  let _err     = $state('');
  let _killing = $state(/** @type {Set<number>} */ (new Set()));
  /** @type {ReturnType<typeof setInterval>|null} */
  let _timer = null;

  async function _load() {
    try {
      _loading = true;
      _err = '';
      _chases = (await fetchActiveChases()) || [];
    } catch (e) {
      _err = e?.message || 'load failed';
    } finally {
      _loading = false;
    }
  }

  async function _kill(/** @type {any} */ row) {
    if (_killing.has(row.id)) return;
    _killing = new Set([..._killing, row.id]);
    try {
      const r = await killChase(row.id);
      if (r?.ok) {
        // Remove from local view immediately; the next poll will
        // confirm via the absence of the row.
        _chases = _chases.filter(c => c.id !== row.id);
        onKilled?.(row, r);
      } else if (r?.err) {
        _err = `kill #${row.id}: ${r.err}`;
      }
    } catch (e) {
      _err = `kill #${row.id}: ${e?.message || 'failed'}`;
    } finally {
      const next = new Set(_killing);
      next.delete(row.id);
      _killing = next;
    }
  }

  function _age(/** @type {string} */ iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return '—';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60)    return `${s}s`;
    if (s < 3600)  return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
  }

  function _modeCls(/** @type {string} */ m) {
    const k = String(m || '').toLowerCase();
    if (k === 'live')   return 'cc-mode cc-mode-live';
    if (k === 'paper')  return 'cc-mode cc-mode-paper';
    if (k === 'shadow') return 'cc-mode cc-mode-shadow';
    return 'cc-mode';
  }

  onMount(() => {
    _load();
    _timer = setInterval(_load, Math.max(1000, pollMs));
  });
  onDestroy(() => {
    if (_timer) { clearInterval(_timer); _timer = null; }
  });
</script>

<div class="cc-root" class:cc-compact={compact}>
  <div class="cc-header">
    <span class="cc-label">Chases in flight</span>
    {#if _chases.length}
      <span class="cc-count">{_chases.length}</span>
    {/if}
    <span class="cc-spacer"></span>
    {#if _err}
      <span class="cc-err" title={_err}>{_err.slice(0, 60)}</span>
    {/if}
  </div>

  {#if !_chases.length}
    <div class="cc-empty">
      {_loading ? 'Loading…' : 'No active chases.'}
    </div>
  {:else}
    <div class="cc-grid" role="grid">
      <div class="cc-row cc-row-h" role="row">
        <span class="cc-col cc-col-acct">Account</span>
        <span class="cc-col cc-col-side">Side</span>
        <span class="cc-col cc-col-qty">Qty</span>
        <span class="cc-col cc-col-sym">Symbol</span>
        <span class="cc-col cc-col-limit">Limit</span>
        <span class="cc-col cc-col-att">Attempts</span>
        {#if !compact}
          <span class="cc-col cc-col-age">Age</span>
          <span class="cc-col cc-col-mode">Mode</span>
        {/if}
        <span class="cc-col cc-col-actions"></span>
      </div>
      {#each _chases as row (row.id)}
        <div class="cc-row" role="row">
          <span class="cc-col cc-col-acct" title="Account">{row.account}</span>
          <span class="cc-col cc-col-side cc-side-{(row.transaction_type || '').toLowerCase()}">
            {row.transaction_type}
          </span>
          <span class="cc-col cc-col-qty">{row.quantity}</span>
          <span class="cc-col cc-col-sym" title={row.symbol}>{row.symbol}</span>
          <span class="cc-col cc-col-limit">
            {row.initial_price != null ? '₹' + priceFmt(row.initial_price) : '—'}
          </span>
          <span class="cc-col cc-col-att">{row.attempts || 0}</span>
          {#if !compact}
            <span class="cc-col cc-col-age">{_age(row.created_at)}</span>
            <span class="cc-col {_modeCls(row.mode)}">{(row.mode || '?').toUpperCase()}</span>
          {/if}
          <span class="cc-col cc-col-actions">
            <button type="button" class="cc-kill"
              disabled={_killing.has(row.id)}
              title="Cancel this chase"
              onclick={() => _kill(row)}>
              {_killing.has(row.id) ? '…' : 'Kill'}
            </button>
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .cc-root {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .cc-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .cc-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(248, 113, 133, 0.8);
  }
  .cc-count {
    display: inline-flex;
    align-items: center;
    padding: 0 0.4rem;
    border-radius: 8px;
    background: rgba(248, 113, 133, 0.18);
    border: 1px solid rgba(248, 113, 133, 0.45);
    color: #fb7185;
    font-size: 0.55rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .cc-spacer { flex: 1 1 0; }
  .cc-err {
    color: #f87171;
    font-size: 0.55rem;
    font-family: monospace;
  }
  .cc-empty {
    color: #7e97b8;
    font-size: 0.65rem;
    padding: 0.4rem 0.2rem;
    font-style: italic;
  }
  .cc-grid {
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .cc-row {
    display: grid;
    grid-template-columns:
      minmax(4rem, 1fr)  /* acct  */
      3rem               /* side  */
      3rem               /* qty   */
      minmax(8rem, 2fr)  /* sym   */
      4.5rem             /* limit */
      3.5rem             /* att   */
      2.5rem             /* age   */
      3.5rem             /* mode  */
      3rem;              /* kill  */
    column-gap: 0.45rem;
    align-items: center;
    padding: 0.32rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    font-size: 0.62rem;
    font-family: ui-monospace, monospace;
    color: #c8d8f0;
  }
  .cc-compact .cc-row {
    grid-template-columns:
      minmax(4rem, 1fr) 3rem 3rem minmax(8rem, 2fr) 4.5rem 3.5rem 3rem;
  }
  .cc-row-h {
    color: #7e97b8;
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    padding-bottom: 0.18rem;
    border-bottom-color: rgba(255, 255, 255, 0.12);
  }
  .cc-row:last-child { border-bottom: none; }
  .cc-col { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cc-col-sym { font-weight: 700; }
  .cc-col-qty, .cc-col-limit, .cc-col-att, .cc-col-age {
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .cc-side-buy  { color: #4ade80; font-weight: 700; }
  .cc-side-sell { color: #f87171; font-weight: 700; }
  .cc-mode {
    font-size: 0.5rem;
    font-weight: 800;
    padding: 0 0.32rem;
    border-radius: 2px;
    text-align: center;
    border: 1px solid currentColor;
  }
  .cc-mode-live   { color: #4ade80; }
  .cc-mode-paper  { color: #7dd3fc; }
  .cc-mode-shadow { color: #fbbf24; }
  .cc-col-actions { text-align: right; }
  .cc-kill {
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(248, 113, 133, 0.45);
    background: transparent;
    color: rgba(248, 113, 133, 0.9);
    font-family: monospace;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    cursor: pointer;
    text-transform: uppercase;
  }
  .cc-kill:hover:not(:disabled) {
    background: rgba(248, 113, 133, 0.12);
    color: #fb7185;
  }
  .cc-kill:disabled { opacity: 0.45; cursor: progress; }
</style>
