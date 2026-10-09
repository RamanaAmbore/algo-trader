<script>
  import { computePairPreview } from '$lib/order/pairModalUtils.js';
  import { portal } from '$lib/portal';
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';
  import Select from '$lib/Select.svelte';

  let {
    open = $bindable(false),
    symbolHint = '',
    pairedCandidates = [],
  } = $props();

  // Escape-stack coordinator (layerStack.js) — this modal previously had
  // NO Escape handling at all (audit finding: Escape exited a parent
  // full-screen card but left this modal open and visible underneath).
  // Teardown form (push on open, pop via the $effect's own cleanup
  // callback) — not the if/open-else/pop form used by components that
  // never unmount (e.g. Select.svelte) — because this modal is wrapped
  // in `{#if _pairModalOpen}` by its host (MarketPulse.svelte), so
  // `open` flips to false and the component unmounts in the same
  // reactive flush; relying on the effect's own re-run to catch
  // `open === false` would race the unmount and could leak the layer.
  $effect(() => {
    if (!open) return;
    const id = pushLayer(() => { open = false; });
    return () => popLayer(id);
  });

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

  // Select component option lists — label() is defined further below but
  // hoisted at call time, same as the native <select> `{#each}` blocks
  // these replace. Child list additionally excludes whichever order is
  // currently picked as the parent (same filter the old `{#if}` inside
  // the each block applied).
  const parentOptions = $derived(filteredParentOrders.map(o => ({ value: String(o.id), label: label(o) })));
  const childOptions = $derived(
    filteredChildOrders
      .filter(o => String(o.id) !== parentId)
      .map(o => ({ value: String(o.id), label: label(o) }))
  );

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
  <div class="opm-overlay" role="dialog" aria-modal="true" use:portal>
    <div class="opm-card">
      <div class="opm-title-row">
        <span class="opm-title">Pair Orders</span>
        <button type="button" class="opm-close" onclick={() => open = false} aria-label="Close">×</button>
      </div>
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

      <div class="opm-field">
        <label class="opm-label" for="opm-parent-sel">Parent order</label>
        <Select id="opm-parent-sel"
                bind:value={parentId}
                ariaLabel="Parent order"
                placeholder="Select parent order"
                options={parentOptions} />
      </div>
      <div class="opm-field">
        <label class="opm-label" for="opm-child-sel">Child order (unlinked only)</label>
        <Select id="opm-child-sel"
                bind:value={childId}
                ariaLabel="Child order"
                placeholder="Select child order"
                options={childOptions} />
      </div>
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
    /* --z-modal-nested: modals opened FROM INSIDE a full-screen card
       (e.g. the Positions card's "Pair" button) must sit above both
       the full-screen tier (9998/9999) and the order-modal tier
       (--z-command: 10500) — previously a bare 9000 literal rendered
       this modal BEHIND an open full-screen card. */
    position: fixed; inset: 0; z-index: var(--z-modal-nested);
    /* Modal-dim audit (2026-10-02): was a stray rgba(0,0,0,0.55) — this
       is a genuine centered dialog (fields + Pair/Cancel actions), the
       same visual weight as a ModalShell consumer, so it takes
       ModalShell's own canonical dim (.ms-dim: rgba(8,12,20,0.72) +
       blur(2px)) rather than the lighter .canonical-modal-overlay
       value used for drawers/popovers. */
    background: rgba(8,12,20,0.72);
    backdrop-filter: blur(2px);
    display: flex; align-items: center; justify-content: center;
  }
  .opm-card {
    background: #0f1b2e; border: 1px solid color-mix(in srgb, var(--text-med) 20%, transparent);
    border-radius: 8px; padding: 1.25rem 1.5rem;
    min-width: 320px; max-width: 480px; width: 100%;
    display: flex; flex-direction: column; gap: 0.75rem;
  }
  .opm-title-row { display: flex; align-items: center; justify-content: space-between; }
  .opm-title { font-size: var(--fs-xl); font-weight: 600; color: var(--text-med); }
  .opm-close {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.4rem; height: 1.4rem;
    background: var(--close-btn-danger-bg); border: 1px solid rgba(248,113,113,0.35);
    border-radius: 3px; color: var(--c-short); font-size: var(--fs-xl);
    line-height: 1; cursor: pointer; flex-shrink: 0; transition: background 0.1s;
  }
  .opm-close:hover { background: var(--close-btn-danger-bg-hover); }
  /* Wraps a label + its Select (canonical dropdown — Select.svelte)
     as one flex item, same shrink-to-content shape OrderKnobsRow's
     `.ot-knob` uses, so the two fields keep their tight 0.25rem
     label-to-control gap while the card's own 0.75rem gap still
     separates the two field groups from each other. */
  .opm-field { display: flex; flex-direction: column; gap: 0.25rem; }
  .opm-label { font-size: var(--fs-lg); color: var(--text-lo); }
  .opm-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.25rem; }
  .opm-cancel {
    font-size: var(--fs-lg); padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid color-mix(in srgb, var(--text-med) 30%, transparent); background: transparent;
    color: var(--text-lo); cursor: pointer;
  }
  .opm-submit {
    font-size: var(--fs-lg); padding: 0.25rem 0.75rem; border-radius: 4px;
    border: 1px solid rgba(99,179,101,0.5); background: rgba(99,179,101,0.15);
    color: #6bb365; cursor: pointer;
  }
  .opm-submit:disabled { opacity: 0.4; cursor: default; }
  .opm-error { font-size: var(--fs-lg); color: #fb7185; }
  .opm-success { font-size: var(--fs-lg); color: #6bb365; }

  /* Position preview panel */
  .opm-preview {
    background: color-mix(in srgb, var(--text-med) 5%, transparent);
    border: 1px solid color-mix(in srgb, var(--text-med) 15%, transparent);
    border-radius: 5px;
    padding: 0.55rem 0.7rem;
    display: flex; flex-direction: column; gap: 0.3rem;
    font-size: var(--fs-lg);
  }
  .opm-preview-title {
    font-size: var(--fs-md); font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--text-lo);
    margin-bottom: 0.15rem;
  }
  .opm-preview-leg {
    display: flex; align-items: center; gap: 0.5rem;
    color: var(--text-med);
  }
  .opm-preview-label {
    font-size: var(--fs-md); font-weight: 600; color: var(--text-muted);
    min-width: 2.8rem; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .opm-preview-acct {
    font-family: monospace; font-size: var(--fs-md);
    color: var(--text-lo); min-width: 2.6rem;
  }
  .opm-preview-sym {
    font-family: monospace; font-size: var(--fs-md);
    color: var(--text-med); flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .opm-preview-qty {
    font-family: monospace; font-size: var(--fs-md);
    color: var(--text-med); white-space: nowrap;
  }
  .opm-preview-row {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.15rem 0.35rem; border-radius: 3px;
    background: rgba(34,211,238,0.10);
    color: var(--c-info);
  }
  .opm-preview-qty-val {
    font-family: monospace; font-size: var(--fs-md); font-weight: 600;
    margin-left: auto;
  }
  .opm-orphan {
    background: rgba(251,191,36,0.12);
    color: var(--c-action);
  }
</style>
