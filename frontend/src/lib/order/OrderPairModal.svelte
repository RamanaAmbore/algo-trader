<script>
  import { computePairPreview } from '$lib/order/pairModalUtils.js';

  let {
    open = $bindable(false),
    symbolHint = '',
    pairedCandidates = [],
  } = $props();

  let orders = $state([]);
  let parentId = $state('');
  let childId  = $state('');
  let submitting = $state(false);
  let error = $state('');
  let success = $state('');

  $effect(() => {
    if (open) {
      fetch('/api/orders/recent?n=200&mode=all')
        .then(r => r.json())
        .then(d => { orders = d.orders ?? d ?? []; })
        .catch(() => { error = 'Failed to load orders'; });
    }
  });

  const unlinked = $derived(orders.filter(o => o.parent_order_id == null));

  /** Filter orders by symbolHint and account from pairedCandidates[0] */
  const filteredParentOrders = $derived.by(() => {
    const hint = symbolHint.trim().toLowerCase();
    const acct = pairedCandidates[0]?.account ?? '';
    return orders.filter(o => {
      const sym = (o.symbol ?? o.tradingsymbol ?? '').toLowerCase();
      if (hint && !sym.includes(hint)) return false;
      if (acct && o.account && o.account !== acct) return false;
      return true;
    });
  });

  /** Filter unlinked orders by account from pairedCandidates[0] */
  const filteredChildOrders = $derived.by(() => {
    const acct = pairedCandidates[0]?.account ?? '';
    return unlinked.filter(o => {
      if (acct && o.account && o.account !== acct) return false;
      return true;
    });
  });

  /** Preview quantities derived from pairedCandidates */
  const preview = $derived(computePairPreview(pairedCandidates));

  async function submit() {
    if (!parentId || !childId || parentId === childId) return;
    submitting = true; error = '';
    try {
      const r = await fetch('/api/orders/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_id: Number(parentId), child_id: Number(childId) }),
      });
      if (!r.ok) { const d = await r.json(); error = d.detail ?? 'Error'; return; }
      success = 'Paired successfully';
      setTimeout(() => { open = false; success = ''; }, 1200);
    } finally { submitting = false; }
  }

  function label(o) {
    return `#${o.id} ${o.symbol ?? o.tradingsymbol ?? ''} [${o.status}]`;
  }

  /** Direction character for a candidate quantity */
  function dirChar(/** @type {any} */ c) {
    const q = c?.quantity ?? c?.qty_pos ?? 0;
    return q >= 0 ? '+' : '−';
  }

  /** Account short label — last 4 chars */
  function acctShort(/** @type {string|undefined} */ acct) {
    return acct ? String(acct).slice(-4) : '—';
  }
</script>

{#if open}
  <div class="opm-overlay" role="dialog" aria-modal="true">
    <div class="opm-card">
      <div class="opm-title">Pair Orders</div>
      {#if error}<div class="opm-error">{error}</div>{/if}
      {#if success}<div class="opm-success">{success}</div>{/if}

      {#if pairedCandidates.length >= 2}
        <div class="opm-preview">
          <div class="opm-preview-title">Position Preview</div>
          <div class="opm-preview-leg">
            <span class="opm-preview-label">Leg A</span>
            <span class="opm-preview-acct">{acctShort(pairedCandidates[0]?.account)}</span>
            <span class="opm-preview-sym">{pairedCandidates[0]?.symbol ?? pairedCandidates[0]?.tradingsymbol ?? '—'}</span>
            <span class="opm-preview-qty">{dirChar(pairedCandidates[0])}{preview.qty_a} lots</span>
          </div>
          <div class="opm-preview-leg">
            <span class="opm-preview-label">Leg B</span>
            <span class="opm-preview-acct">{acctShort(pairedCandidates[1]?.account)}</span>
            <span class="opm-preview-sym">{pairedCandidates[1]?.symbol ?? pairedCandidates[1]?.tradingsymbol ?? '—'}</span>
            <span class="opm-preview-qty">{dirChar(pairedCandidates[1])}{preview.qty_b} lots</span>
          </div>
          <div class="opm-preview-row opm-matched">
            <span class="opm-preview-label">Matched</span>
            <span class="opm-preview-qty-val">{preview.proposedPairedQty} lots</span>
          </div>
          <div class="opm-preview-row" class:opm-orphan={preview.orphanQty > 0}>
            <span class="opm-preview-label">Orphan</span>
            <span class="opm-preview-qty-val">{preview.orphanQty} lots</span>
          </div>
        </div>
      {/if}

      <label class="opm-label">Parent order
        <select class="opm-select" bind:value={parentId}>
          <option value="">Select parent order</option>
          {#each filteredParentOrders as o (o.id)}
            <option value={String(o.id)}>{label(o)}</option>
          {/each}
        </select>
      </label>
      <label class="opm-label">Child order (unlinked only)
        <select class="opm-select" bind:value={childId}>
          <option value="">Select child order</option>
          {#each filteredChildOrders as o (o.id)}
            {#if String(o.id) !== parentId}
              <option value={String(o.id)}>{label(o)}</option>
            {/if}
          {/each}
        </select>
      </label>
      <div class="opm-actions">
        <button class="opm-cancel" onclick={() => open = false}>Cancel</button>
        <button class="opm-submit" onclick={submit}
          disabled={!parentId || !childId || parentId === childId || submitting}>
          {submitting ? 'Pairing…' : 'Pair'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .opm-overlay {
    position: fixed; inset: 0; z-index: 9000;
    background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center;
  }
  .opm-card {
    background: #0f1b2e; border: 1px solid rgba(160,185,220,0.2);
    border-radius: 8px; padding: 1.25rem 1.5rem;
    min-width: 320px; max-width: 480px; width: 100%;
    display: flex; flex-direction: column; gap: 0.75rem;
  }
  .opm-title { font-size: 0.9rem; font-weight: 600; color: rgba(210,225,255,0.9); }
  .opm-label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.72rem; color: rgba(160,185,220,0.7); }
  .opm-select {
    background: rgba(160,185,220,0.07); border: 1px solid rgba(160,185,220,0.2);
    color: rgba(210,225,255,0.85); border-radius: 4px; padding: 0.3rem 0.4rem; font-size: 0.75rem;
  }
  .opm-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.25rem; }
  .opm-cancel {
    font-size: 0.75rem; padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid rgba(160,185,220,0.3); background: transparent;
    color: rgba(160,185,220,0.7); cursor: pointer;
  }
  .opm-submit {
    font-size: 0.75rem; padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid rgba(99,179,101,0.5); background: rgba(99,179,101,0.15);
    color: #6bb365; cursor: pointer;
  }
  .opm-submit:disabled { opacity: 0.4; cursor: default; }
  .opm-error { font-size: 0.72rem; color: #fb7185; }
  .opm-success { font-size: 0.72rem; color: #6bb365; }

  /* Position preview panel */
  .opm-preview {
    background: rgba(160,185,220,0.05);
    border: 1px solid rgba(160,185,220,0.15);
    border-radius: 5px;
    padding: 0.55rem 0.7rem;
    display: flex; flex-direction: column; gap: 0.3rem;
    font-size: 0.71rem;
  }
  .opm-preview-title {
    font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: rgba(160,185,220,0.6);
    margin-bottom: 0.15rem;
  }
  .opm-preview-leg {
    display: flex; align-items: center; gap: 0.5rem;
    color: rgba(210,225,255,0.8);
  }
  .opm-preview-label {
    font-size: 0.65rem; font-weight: 600; color: rgba(160,185,220,0.55);
    min-width: 2.8rem; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .opm-preview-acct {
    font-family: monospace; font-size: 0.68rem;
    color: rgba(160,185,220,0.6); min-width: 2.6rem;
  }
  .opm-preview-sym {
    font-family: monospace; font-size: 0.68rem;
    color: rgba(210,225,255,0.85); flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .opm-preview-qty {
    font-family: monospace; font-size: 0.68rem;
    color: rgba(210,225,255,0.8); white-space: nowrap;
  }
  .opm-preview-row {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.15rem 0.35rem; border-radius: 3px;
    background: rgba(34,211,238,0.10);
    color: #22d3ee;
  }
  .opm-preview-qty-val {
    font-family: monospace; font-size: 0.68rem; font-weight: 600;
    margin-left: auto;
  }
  .opm-orphan {
    background: rgba(251,191,36,0.12);
    color: #fbbf24;
  }
</style>
