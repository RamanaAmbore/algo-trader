<!--
  HeldOrdersCard.svelte — automated orders held for review, with release
  and cancel actions per order. Lists held expiry closes, held agent
  orders (chase paused after repeated rejection), and held agent-close
  requests (the exit-tp-short-option-90 agent's require_release hold,
  2026-11). The 'template_exit' category (Bracket exits) was removed
  2026-11 (Track 3, T3.4, Bracket/Template/GTT removal) — see kindOf()'s
  own comment.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { fetchHeldOrders, releaseHeldOrder, cancelHeldOrder } from '$lib/api';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { visibleInterval } from '$lib/stores';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import CardHeader from '$lib/CardHeader.svelte';

  let confirmRef = $state(null);
  let releasingAll = $state(false);

  let rows = $state(/** @type {any[]} */ ([]));
  let busy = $state(/** @type {Record<string, boolean>} */ ({}));

  // 2026-11-24 fix (audit finding #6, frontend half) — this card used to
  // load once on mount and never again. An operator had to manually
  // reload the page to see a newly-staged hold, or to notice one had
  // auto-released. Polls on the same 30s cadence BrokerHealthBadge.svelte
  // already uses, via the shared visibleInterval helper (NavCard.svelte's
  // own load()/stopInterval() convention).
  /** @type {(() => void) | null} */
  let stopInterval = null;

  // Per-second clock for the auto-release countdown — same pattern as
  // ChaseCard.svelte's `_nowSec` (ticks every 1s between the slower data
  // poll, so the "releases in Xm Ys" text counts down live).
  let _nowMs = $state(Date.now());
  /** @type {ReturnType<typeof setInterval> | null} */
  let _clockTimer = null;

  /** `auto_release_at` is an ISO datetime string (set by hold_record() —
   *  see order_hold.py), not epoch seconds like ChaseCard's
   *  `next_attempt_at`, so this parses rather than subtracting directly.
   *  Returns null when there's nothing to show (absent, unparseable, or
   *  already past — a released row should just not show a countdown,
   *  not a confusing negative one). */
  function _releaseCountdown(/** @type {string | null | undefined} */ autoReleaseAt, /** @type {number} */ nowMs) {
    if (!autoReleaseAt) return null;
    const t = Date.parse(autoReleaseAt);
    if (Number.isNaN(t)) return null;
    const totalSec = Math.floor((t - nowMs) / 1000);
    if (totalSec <= 0) return null;
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${m}m ${String(s).padStart(2, '0')}s`;
  }

  function kindOf(row) {
    try {
      const h = JSON.parse(row.hold || '{}');
      // 'template_exit' branch removed 2026-11 (Track 3, T3.4,
      // Bracket/Template/GTT removal) — `HoldCategory.TEMPLATE_EXIT` no
      // longer exists server-side (T3.1), so this category can never
      // occur in a real hold_json payload going forward.
      if (h.category === 'agent_order') return 'Repeated rejection';
      // `HoldCategory.AGENT_CLOSE` (2026-11, the exit-tp-short-option-90
      // agent's require_release close) used to silently fall through to
      // the 'Expiry close' default below — a real, live category, not a
      // removed one. Give it its own label instead of misreporting it.
      if (h.category === 'agent_close') return 'Profit-target close';
      if (h.category === 'expiry_close') return 'Expiry close';
      // 2026-11-24 fix (audit finding #19): any OTHER category (missing,
      // malformed, or a genuinely unrecognized value — e.g. a future
      // category this card hasn't been taught yet) used to silently fall
      // through to 'Expiry close', which actively mislabels it as a real,
      // known kind. Falls through to the neutral 'Pending' below instead
      // — the same genuinely-unknown label the parse-failure catch branch
      // already uses, so a category this card doesn't recognize is never
      // misreported as one it does.
    } catch { /* fall through to the neutral default below */ }
    // Item 3 (2026-11-02, PLAN.md) — "Held" → "Pending": a malformed/
    // unparseable hold record, or any unrecognized category, is still a
    // release-pending row, not a genuine failure — same "Pending"
    // vocabulary as every other template-exit-hold surface. Distinct from
    // the unrelated covered-call feature's own "Held" usage, which this
    // does not touch.
    return 'Pending';
  }

  // 2026-10 fix: the hold categories previously all rendered with the
  // exact same --algo-muted color — an operator scanning this card
  // couldn't tell at a glance which kind of hold they were looking at.
  // One CSS class per kind, ranked by how urgent leaving it held is.
  // ('Bracket exit' branch removed 2026-11, see kindOf()'s own comment.)
  function kindClassOf(row) {
    const kind = kindOf(row);
    if (kind === 'Repeated rejection') return 'held-kind-rejection';
    if (kind === 'Profit-target close') return 'held-kind-agent-close';
    if (kind === 'Expiry close') return 'held-kind-expiry';
    // 2026-11-24 fix (audit finding #19): a genuinely unrecognized
    // category gets the neutral muted styling, not the amber "Expiry
    // close" treatment — matching kindOf()'s own fallback above.
    return 'held-kind-pending';
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

  onMount(() => {
    load();
    stopInterval = visibleInterval(load, 30_000);
    _clockTimer = setInterval(() => { _nowMs = Date.now(); }, 1000);
  });
  onDestroy(() => {
    if (stopInterval) stopInterval();
    if (_clockTimer) clearInterval(_clockTimer);
  });
</script>

<ConfirmModal bind:this={confirmRef} />
{#if rows.length}
  <section class="held-card" aria-label="Held orders">
    <CardHeader title="Held orders" showControls={false} detectOverflow={false}>
      {#snippet left()}
        <InfoHint
          text="Automated orders paused for your review — the broker never got this order. Release sends it; Cancel discards it."
        />
        <span class="held-count">{rows.length}</span>
      {/snippet}
      {#snippet right()}
        <button type="button" class="held-release-all"
                disabled={releasingAll}
                onclick={releaseAll}>Release all</button>
      {/snippet}
    </CardHeader>
    {#each rows as row (row.id)}
      <div class="held-row">
        <span class="held-kind {kindClassOf(row)}">{kindOf(row)}</span>
        <span class="held-desc">
          <span class="held-side held-side-{(row.side || '').toLowerCase()}">{row.side}</span>
          {row.qty} {row.symbol} · {row.account}
        </span>
        {#if _releaseCountdown(row.auto_release_at, _nowMs)}
          <span class="held-countdown" title="Auto-releases unless cancelled or released first">
            auto-releases in {_releaseCountdown(row.auto_release_at, _nowMs)}
          </span>
        {/if}
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
  /* .held-head/.held-title retired — CardHeader now owns the title bar
     (2026-10 CardHeader adoption sweep); push-right-of-Release-all comes
     from CardHeader's own middle spacer instead of a local flex:1 rule. */
  .held-count {
    color: var(--algo-muted);
    font-weight: 400;
    font-size: var(--fs-sm);
  }
  .held-row { display: flex; gap: 8px; align-items: center; padding: 4px 0; font-size: var(--fs-sm); }
  .held-kind { text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600; }
  /* Per-category tint, ranked by how risky it is to leave the row held —
     Expiry close keeps the card's own amber; Repeated rejection
     (agent_order) is orange, matching OrderBook's SHADOW/rejection-
     adjacent hue. No --algo-orange-bg token exists, so the background is
     derived from --algo-orange via color-mix, the same idiom this
     codebase already uses wherever a role has no dedicated -bg token.
     ('Bracket exit'/.held-kind-bracket removed 2026-11, see kindOf().) */
  .held-kind-expiry    { color: var(--algo-amber); background: var(--algo-amber-bg-strong); padding: 1px 5px; border-radius: 3px; }
  .held-kind-rejection { color: var(--algo-orange); background: color-mix(in srgb, var(--algo-orange) 22%, transparent); padding: 1px 5px; border-radius: 3px; }
  .held-kind-agent-close { color: var(--algo-green); background: var(--algo-green-bg-strong); padding: 1px 5px; border-radius: 3px; }
  /* 2026-11-24 fix (audit finding #19) — neutral styling for a row this
     card genuinely doesn't recognize, instead of misreporting it as
     Expiry close's own amber. The pre-2026-10 undifferentiated color
     every category used to share, before the per-category tint fix
     above. */
  .held-kind-pending   { color: var(--algo-muted); background: color-mix(in srgb, var(--algo-muted) 18%, transparent); padding: 1px 5px; border-radius: 3px; }
  .held-desc { flex: 1; color: var(--algo-slate); }
  /* 2026-11-24 fix (audit finding #6, frontend half) — muted, not an
     alert color: an auto-release is the designed, expected outcome of
     leaving a hold alone, not a problem the operator needs to notice. */
  .held-countdown { color: var(--algo-muted); font-size: var(--fs-2xs); white-space: nowrap; }
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
