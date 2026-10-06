<!--
  HeldOrdersCard.svelte — automated orders held for review, with a release
  action per order. Held expiry closes and held template exits are listed.
-->
<script>
  import { onMount } from 'svelte';
  import { fetchHeldOrders, releaseHeldOrder } from '$lib/api';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import ConfirmModal from '$lib/ConfirmModal.svelte';

  let confirmRef = $state(null);
  let releasingAll = $state(false);

  let rows = $state(/** @type {any[]} */ ([]));
  let busy = $state(/** @type {Record<string, boolean>} */ ({}));

  function kindOf(row) {
    try {
      const h = JSON.parse(row.hold || '{}');
      return h.category === 'template_exit' ? 'Template exit' : 'Expiry close';
    } catch {
      return 'Held';
    }
  }

  async function load() {
    try {
      const resp = await fetchHeldOrders();
      rows = Array.isArray(resp?.held) ? resp.held : [];
    } catch (_) { /* keep last good */ }
  }

  async function release(row) {
    busy = { ...busy, [row.id]: true };
    try {
      await releaseHeldOrder(row.id);
      toast?.success?.(`Released ${row.side} ${row.qty} ${row.symbol}`);
      await load();
    } catch (e) {
      toast?.error?.(e?.message || 'Release refused');
    } finally {
      busy = { ...busy, [row.id]: false };
    }
  }

  async function releaseAll() {
    if (!rows.length || releasingAll) return;
    const ok = await confirmRef?.ask({
      title: 'Release all held orders?',
      message: `${rows.length} held order${rows.length === 1 ? '' : 's'} will be sent to the broker now.`,
      danger: true,
    });
    if (!ok) return;
    releasingAll = true;
    let released = 0;
    let refused = 0;
    for (const row of [...rows]) {
      try {
        await releaseHeldOrder(row.id);
        released += 1;
      } catch (_) {
        refused += 1;
      }
    }
    releasingAll = false;
    toast?.success?.(`Released ${released}${refused ? `, refused ${refused}` : ''}`);
    await load();
  }

  onMount(() => { load(); });
</script>

<ConfirmModal bind:this={confirmRef} />
{#if rows.length}
  <section class="held-card" aria-label="Held orders">
    <header class="held-head">
      <span class="held-title">Held orders</span>
      <span class="held-count">{rows.length}</span>
      <button type="button" class="held-release-all"
              disabled={releasingAll}
              onclick={releaseAll}>Release all</button>
    </header>
    {#each rows as row (row.id)}
      <div class="held-row">
        <span class="held-kind">{kindOf(row)}</span>
        <span class="held-desc">{row.side} {row.qty} {row.symbol} · {row.account}</span>
        <button type="button" class="held-release"
                disabled={busy[row.id]}
                onclick={() => release(row)}>Release</button>
      </div>
    {/each}
  </section>
{/if}

<style>
  .held-card { border: 1px solid rgba(251,191,36,0.4); border-radius: 4px; padding: 6px 8px; margin-bottom: 8px; }
  .held-head { display: flex; justify-content: space-between; font-weight: 700; color: var(--c-action); }
  .held-row { display: flex; gap: 8px; align-items: center; padding: 4px 0; font-size: var(--fs-sm); }
  .held-kind { color: var(--algo-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .held-desc { flex: 1; color: var(--algo-slate); }
  .held-release { padding: 2px 8px; border: 1px solid var(--c-action); color: var(--c-action); background: transparent; border-radius: 3px; cursor: pointer; }
  .held-release:disabled { opacity: 0.5; cursor: default; }
  .held-release-all { padding: 2px 8px; border: 1px solid var(--c-action); color: var(--c-action); background: transparent; border-radius: 3px; cursor: pointer; font-size: var(--fs-sm); }
  .held-release-all:disabled { opacity: 0.5; cursor: default; }
</style>
