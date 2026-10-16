<script>
  import { portal } from '$lib/portal';
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';
  import Select from '$lib/Select.svelte';

  let {
    open = $bindable(false),
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

  // Select component option lists — label() is defined further below but
  // hoisted at call time, same as the native <select> `{#each}` blocks
  // these replace. Child list excludes whichever order is currently
  // picked as the parent (same filter the old `{#if}` inside the each
  // block applied). Previously these filtered by `symbolHint`/the
  // checkbox-select-2-legs `pairedCandidates` prop — both props were
  // retired (2026-10, derivatives page's own "⟷ Pair" checkbox flow was
  // replaced by PositionPinPicker; this modal's one remaining real
  // caller, MarketPulse.svelte's "⟷ Pair" button, never passed either
  // prop) so the filtering collapsed to a no-op and was removed.
  const parentOptions = $derived(orders.map(o => ({ value: String(o.id), label: label(o) })));
  const childOptions = $derived(
    unlinked
      .filter(o => String(o.id) !== parentId)
      .map(o => ({ value: String(o.id), label: label(o) }))
  );

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
  <div class="opm-overlay" role="dialog" aria-modal="true" use:portal>
    <div class="opm-card">
      <div class="opm-title-row">
        <span class="opm-title">Pair Orders</span>
        <button type="button" class="opm-close" onclick={() => open = false} aria-label="Close">×</button>
      </div>
      {#if error}<div class="opm-error">{error}</div>{/if}
      {#if success}<div class="opm-success">{success}</div>{/if}

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
      <div class="opm-warn">
        Pairing these orders will suppress automatic take-profit / template
        attachment on the child order.
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

  /* Side-effect disclosure (2026-10) — pairing two orders here suppresses
     the child order's own automatic take-profit/template attachment, a
     real behavior change the operator should see before submitting. */
  .opm-warn {
    font-size: var(--fs-md); color: var(--c-action);
    background: color-mix(in srgb, var(--c-action) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--c-action) 30%, transparent);
    border-radius: 5px;
    padding: 0.4rem 0.6rem;
  }
</style>
