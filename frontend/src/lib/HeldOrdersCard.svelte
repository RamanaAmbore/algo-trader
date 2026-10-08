<!--
  HeldOrdersCard.svelte — automated orders held for review, with release
  and cancel actions per order. Lists held expiry closes, held template
  exits, and held agent orders (chase paused after repeated rejection).
-->
<script>
  import { onMount } from 'svelte';
  import { fetchHeldOrders, releaseHeldOrder, cancelHeldOrder } from '$lib/api';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import InfoHint from '$lib/InfoHint.svelte';

  let confirmRef = $state(null);
  let releasingAll = $state(false);

  let rows = $state(/** @type {any[]} */ ([]));
  let busy = $state(/** @type {Record<string, boolean>} */ ({}));

  function kindOf(row) {
    try {
      const h = JSON.parse(row.hold || '{}');
      if (h.category === 'template_exit') return 'Bracket exit';
      if (h.category === 'agent_order') return 'Repeated rejection';
      return 'Expiry close';
    } catch {
      return 'Held';
    }
  }

  // 2026-10 fix: the three hold categories previously all rendered with
  // the exact same --algo-muted color — an operator scanning this card
  // couldn't tell at a glance which kind of hold they were looking at,
  // even though "Bracket exit" (an already-filled position sitting with
  // zero protection) is materially riskier to leave sitting than
  // "Expiry close" or "Repeated rejection". One CSS class per kind,
  // ranked by how urgent leaving it held is.
  function kindClassOf(row) {
    const kind = kindOf(row);
    if (kind === 'Bracket exit') return 'held-kind-bracket';
    if (kind === 'Repeated rejection') return 'held-kind-rejection';
    return 'held-kind-expiry';
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

  async function cancel(row) {
    const ok = await confirmRef?.ask({
      title: 'Cancel held order?',
      message: `${row.side} ${row.qty} ${row.symbol} will be cancelled, not sent to the broker.`,
      danger: true,
      confirmLabel: 'Cancel order',
      cancelLabel: 'Keep held',
    });
    if (!ok) return;
    busy = { ...busy, [row.id]: true };
    try {
      await cancelHeldOrder(row.id);
      toast?.success?.(`Cancelled ${row.side} ${row.qty} ${row.symbol}`);
      await load();
    } catch (e) {
      toast?.error?.(e?.message || 'Cancel refused');
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
      // Explicit labels (audit fix, 2026-10) — `danger: true` with no
      // override defaults to ConfirmModal's generic 'Delete'/'Cancel',
      // which reads backwards here: this is a non-destructive release-to-
      // broker action, not a delete. Matches the per-row cancel()'s own
      // unambiguous-labeling convention above.
      confirmLabel: 'Release all',
      cancelLabel: 'Keep held',
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
      <InfoHint
        text="Automated orders paused for your review — the broker never got this order, or (Bracket exit) a position is sitting with no TP/SL. Release sends it; Cancel discards it."
      />
      <span class="held-count">{rows.length}</span>
      <button type="button" class="held-release-all"
              disabled={releasingAll}
              onclick={releaseAll}>Release all</button>
    </header>
    {#each rows as row (row.id)}
      <div class="held-row">
        <span class="held-kind {kindClassOf(row)}">{kindOf(row)}</span>
        <span class="held-desc">
          <span class="held-side held-side-{(row.side || '').toLowerCase()}">{row.side}</span>
          {row.qty} {row.symbol} · {row.account}
        </span>
        <button type="button" class="held-release"
                disabled={busy[row.id]}
                onclick={() => release(row)}>Release</button>
        <button type="button" class="held-cancel"
                disabled={busy[row.id]}
                onclick={() => cancel(row)}>Cancel</button>
      </div>
    {/each}
  </section>
{/if}

<style>
  /* 2026-10 cleanup: hand-written rgba literals + hardcoded radii/opacity
     replaced with the app.css token set every other algo card/button
     already uses (--algo-amber-border-soft, --btn-radius,
     --btn-disabled-opacity, the --btn-amber- and --btn-sell- families). */
  .held-card {
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: var(--btn-radius);
    padding: 6px 8px;
    margin-bottom: 8px;
  }
  .held-head { display: flex; align-items: center; gap: 6px; font-weight: 700; color: var(--c-action); }
  .held-title { flex-shrink: 0; }
  .held-count {
    flex: 1;
    text-align: left;
    color: var(--algo-muted);
    font-weight: 400;
    font-size: var(--fs-sm);
  }
  .held-row { display: flex; gap: 8px; align-items: center; padding: 4px 0; font-size: var(--fs-sm); }
  .held-kind { text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600; }
  /* Per-category tint, ranked by how risky it is to leave the row held —
     Bracket exit (an already-filled position with zero TP/SL) is the
     most urgent, so it gets the same red used for SELL/short/reject
     everywhere else; Expiry close keeps the card's own amber; Repeated
     rejection (agent_order) is orange, matching OrderBook's SHADOW/
     rejection-adjacent hue. No --algo-orange-bg token exists, so the
     background is derived from --algo-orange via color-mix, the same
     idiom this codebase already uses wherever a role has no dedicated
     -bg token. */
  .held-kind-bracket   { color: var(--algo-red); background: var(--algo-red-bg-strong); padding: 1px 5px; border-radius: 3px; }
  .held-kind-expiry    { color: var(--algo-amber); background: var(--algo-amber-bg-strong); padding: 1px 5px; border-radius: 3px; }
  .held-kind-rejection { color: var(--algo-orange); background: color-mix(in srgb, var(--algo-orange) 22%, transparent); padding: 1px 5px; border-radius: 3px; }
  .held-desc { flex: 1; color: var(--algo-slate); }
  .held-side { font-weight: 700; }
  .held-side-buy  { color: var(--btn-buy); }
  .held-side-sell { color: var(--btn-sell); }
  .held-release {
    padding: 2px 8px;
    border: 1px solid var(--btn-amber-border);
    color: var(--btn-amber);
    background: transparent;
    border-radius: var(--btn-radius);
    cursor: pointer;
  }
  .held-release:hover:not(:disabled) { background: var(--btn-amber-bg-hi); }
  .held-release:disabled { opacity: var(--btn-disabled-opacity); cursor: default; }
  .held-cancel {
    padding: 2px 8px;
    border: 1px solid var(--btn-sell-border);
    color: var(--btn-sell);
    background: transparent;
    border-radius: var(--btn-radius);
    cursor: pointer;
  }
  .held-cancel:hover:not(:disabled) { background: var(--btn-sell-bg-hi); }
  .held-cancel:disabled { opacity: var(--btn-disabled-opacity); cursor: default; }
  .held-release-all {
    padding: 2px 8px;
    border: 1px solid var(--btn-amber-border);
    color: var(--btn-amber);
    background: transparent;
    border-radius: var(--btn-radius);
    cursor: pointer;
    font-size: var(--fs-sm);
    flex-shrink: 0;
  }
  .held-release-all:hover:not(:disabled) { background: var(--btn-amber-bg-hi); }
  .held-release-all:disabled { opacity: var(--btn-disabled-opacity); cursor: default; }
</style>
