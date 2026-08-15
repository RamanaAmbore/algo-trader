<script>
  let { open = $bindable(false) } = $props();

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
</script>

{#if open}
  <div class="opm-overlay" role="dialog" aria-modal="true">
    <div class="opm-card">
      <div class="opm-title">Pair Orders</div>
      {#if error}<div class="opm-error">{error}</div>{/if}
      {#if success}<div class="opm-success">{success}</div>{/if}
      <label class="opm-label">Parent order
        <select class="opm-select" bind:value={parentId}>
          <option value="">— select parent —</option>
          {#each orders as o (o.id)}
            <option value={String(o.id)}>{label(o)}</option>
          {/each}
        </select>
      </label>
      <label class="opm-label">Child order (unlinked only)
        <select class="opm-select" bind:value={childId}>
          <option value="">— select child —</option>
          {#each unlinked as o (o.id)}
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
    min-width: 320px; max-width: 420px; width: 100%;
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
</style>
