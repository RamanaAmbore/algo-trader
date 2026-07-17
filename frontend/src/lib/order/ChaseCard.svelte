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
  import { fetchActiveChases, killChase, reconcileAlgoOrders } from '$lib/api';
  import { priceFmt } from '$lib/format';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { visibleInterval } from '$lib/stores';

  let {
    pollMs   = 3000,
    compact  = false,
    onKilled = null,
    // Bindable outward count — lets the parent gate its container's
    // visibility on whether any chase is active without needing to
    // mount a second poller.
    activeCount = $bindable(0),
  } = $props();

  let _chases  = $state(/** @type {any[]} */ ([]));
  // Mirror the chase count out to the bindable prop so the parent's
  // `{#if openOrders > 0 || chaseActive > 0}` gate can react.
  $effect(() => { const n = _chases.length; if (n !== activeCount) activeCount = n; });
  // Group children directly under their parents so the operator sees
  // parent + protective wing as a visual cluster. Rows are partitioned
  // into "parents and standalones" first, then each child is spliced
  // in immediately after its parent (or appended at the end if its
  // parent isn't in the active-chase set).
  const _orderedChases = $derived.by(() => {
    const childrenByParent = new Map();
    const standalones = [];
    for (const r of _chases) {
      if (r.parent_order_id != null) {
        if (!childrenByParent.has(r.parent_order_id)) childrenByParent.set(r.parent_order_id, []);
        childrenByParent.get(r.parent_order_id).push(r);
      } else {
        standalones.push(r);
      }
    }
    const seen = new Set();
    const out = [];
    for (const p of standalones) {
      out.push(p);
      seen.add(p.id);
      const kids = childrenByParent.get(p.id);
      if (kids) {
        for (const k of kids) {
          out.push(k);
          seen.add(k.id);
        }
      }
    }
    // Children whose parent isn't in the chase set (already filled /
    // cancelled) — render at the end so they don't disappear.
    for (const r of _chases) {
      if (!seen.has(r.id)) out.push(r);
    }
    return out;
  });
  let _loading = $state(false);
  let _err     = $state('');
  let _killing = $state(/** @type {Set<number>} */ (new Set()));
  let _reconciling = $state(false);
  let _reconcileMsg = $state('');
  // Operator: "in chase card, if there are no active chases, no need
  // to show 'no active chases' and refresh it continuously". Idle
  // detection slows the poll to a much longer interval so the card
  // only re-fetches when something might have changed; combined
  // with the parent's `{#if _chases.length}` hiding the section
  // entirely when empty, the network noise goes to near-zero on
  // idle pages. Exposed so the parent can react.
  let _idle = $state(false);
  /** @type {(() => void) | null} */
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
    // Switch the poll cadence based on the result. Empty result =
    // idle; slow the poll way down (default 30s) and hide the card
    // entirely via the template gate. Non-empty = active; fast poll
    // (3s default) so kill state stays current.
    const wasIdle = _idle;
    _idle = _chases.length === 0;
    if (wasIdle !== _idle) _rescheduleTimer();
  }

  function _rescheduleTimer() {
    // Audit fix — `visibleInterval` pauses on `document.hidden`, so
    // when the operator switches away from the tab the polling stops
    // (was `setInterval` with no visibility gate; chase polled every
    // 3 s even on a hidden tab, generating background network noise).
    if (_timer) { _timer(); _timer = null; }
    const ms = _idle ? Math.max(15000, pollMs * 10) : Math.max(1000, pollMs);
    _timer = visibleInterval(_load, ms);
  }

  async function _reconcile() {
    if (_reconciling) return;
    _reconciling = true;
    _reconcileMsg = '';
    try {
      const r = await reconcileAlgoOrders();
      _reconcileMsg = `Reconciled — scanned ${r?.scanned ?? 0}, updated ${r?.updated ?? 0}, missing ${r?.missing ?? 0}`;
      await _load();
      setTimeout(() => { _reconcileMsg = ''; }, 4000);
    } catch (e) {
      _err = `reconcile: ${e?.message || 'failed'}`;
    } finally {
      _reconciling = false;
    }
  }

  async function _kill(/** @type {any} */ row) {
    if (_killing.has(row.id)) return;
    // Cancel cascade: when the operator kills a parent that has
    // auto-attached children (typically the wing of a SELL option),
    // kill the children first. Avoids the orphan-wing case where the
    // protective leg keeps chasing after the parent is gone.
    // Audit fix: re-read child_order_ids from the LIVE _chases snapshot
    // at click time, not the stale row object the operator clicked on.
    // Between poll cycles the parent may have gained or lost children;
    // the row prop in the template doesn't update until the next render.
    const liveRow = _chases.find(c => c.id === row.id) || row;
    const childIds = Array.isArray(liveRow.child_order_ids) ? liveRow.child_order_ids : [];
    const toKill = [...childIds, row.id];
    _killing = new Set([..._killing, ...toKill]);
    // Audit fix — parallel kill via Promise.all instead of sequential
    // await loop. Each killChase call marks the broker_order_id with
    // mark_killed BEFORE the broker.cancel_order returns (Sprint A),
    // so the cascade order is preserved by mark_killed itself — every
    // chase loop in the cascade sees is_killed=true on its next
    // iteration regardless of the order kill_chase endpoints complete.
    // N child orders + parent now resolve in ~1 round-trip instead of N+1.
    try {
      const results = await Promise.all(toKill.map(async (id) => {
        try {
          const r = await killChase(id);
          return { id, ok: !!r?.ok, err: r?.err || null };
        } catch (e) {
          return { id, ok: false, err: e?.message || 'failed' };
        }
      }));
      const successIds = new Set(results.filter(r => r.ok).map(r => r.id));
      if (successIds.size) {
        _chases = _chases.filter(c => !successIds.has(c.id));
        if (successIds.has(row.id)) onKilled?.(row, { ok: true });
      }
      const failures = results.filter(r => !r.ok);
      if (failures.length) {
        // Surface only the first failure to keep the error banner short.
        const f = failures[0];
        _err = `kill #${f.id}: ${f.err || 'failed'}`;
      }
    } finally {
      const next = new Set(_killing);
      for (const id of toKill) next.delete(id);
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

  // Per-second clock for next_attempt_at countdown. Updates every 1s
  // so the "next re-quote in Xs" display ticks down live between 3s polls.
  let _nowSec = $state(Math.floor(Date.now() / 1000));
  /** @type {ReturnType<typeof setInterval>|null} */
  let _clockTimer = null;

  onMount(() => {
    _load();
    _rescheduleTimer();
    _clockTimer = setInterval(() => { _nowSec = Math.floor(Date.now() / 1000); }, 1000);
  });
  onDestroy(() => {
    if (_timer) { _timer(); _timer = null; }
    if (_clockTimer) { clearInterval(_clockTimer); _clockTimer = null; }
  });
</script>

<!-- Operator: "in chase card, if there are no active chases, no
     need to show 'no active chases' and refresh it continuously".
     Entire root is gated on _chases.length so the card simply
     vanishes when idle (the parent's bucket-card chrome
     disappears with it via :empty / display:none). Polling auto-
     slows in _rescheduleTimer when idle so background traffic
     drops to one fetch every ~30s. -->
{#if _chases.length}
<div class="cc-root" class:cc-compact={compact}>
  <div class="cc-header">
    <span class="cc-label">Chases in flight</span>
    <span class="cc-count">{_chases.length}</span>
    <span class="cc-spacer"></span>
    {#if _reconcileMsg}
      <span class="cc-reconcile-msg" title={_reconcileMsg}>{_reconcileMsg}</span>
    {:else if _err}
      <span class="cc-err" title={_err}>{_err.slice(0, 60)}</span>
    {/if}
    <button type="button" class="cc-reconcile"
      title="Re-sync live OPEN rows against the broker (live mode only)"
      disabled={_reconciling}
      onclick={_reconcile}>
      {_reconciling ? 'Reconciling…' : 'Reconcile'}
    </button>
  </div>

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
    {#each _orderedChases as row, _ci (row.id)}
      <div class="cc-row" class:cc-row-child={row.parent_order_id != null} role="row">
        <span class="cc-col cc-col-acct" title="Account">
          {#if row.parent_order_id != null}
            <span class="cc-child-tee" aria-hidden="true">↳</span>
          {/if}
          {row.account}
        </span>
        <span class="cc-col cc-col-side cc-side-{(row.transaction_type || '').toLowerCase()}">
          {row.transaction_type}
        </span>
        <span class="cc-col cc-col-qty">{row.quantity}</span>
        <span class="cc-col cc-col-sym" title={row.symbol}>{formatSymbol(row.symbol)}</span>
        <!-- Audit fix (M-6) — show current_limit (live re-quoted) when
             the chase has moved off initial_price; otherwise fall back
             to initial_price for the first iteration. A small "·N"
             chip after the price indicates the price has moved through
             N modifies so the operator knows it's not the entry price.
             Title surfaces both values for context. -->
        <span class="cc-col cc-col-limit"
              title={row.current_limit != null && row.initial_price != null
                     && Number(row.current_limit) !== Number(row.initial_price)
                ? `Live limit: ₹${priceFmt(row.current_limit)} (initial: ₹${priceFmt(row.initial_price)})`
                : (row.initial_price != null
                    ? `Initial limit: ₹${priceFmt(row.initial_price)}`
                    : 'No limit set')}>
          {(row.current_limit ?? row.initial_price) != null
            ? '₹' + priceFmt(row.current_limit ?? row.initial_price)
            : '—'}
          {#if row.current_limit != null && row.initial_price != null
               && Number(row.current_limit) !== Number(row.initial_price)}
            <span class="cc-limit-moved">·{row.attempts || 0}</span>
          {/if}
        </span>
        <span class="cc-col cc-col-att">{row.attempts || 0}{#if row.next_attempt_at != null && row.status === 'active'}
            {#if _nowSec < row.next_attempt_at}
              <span class="cc-countdown" title="Seconds until next re-quote"> · {Math.max(0, Math.ceil(row.next_attempt_at - _nowSec))}s</span>
            {:else}
              <span class="cc-countdown cc-requoting" title="Re-quoting now"> · re-quoting…</span>
            {/if}
          {/if}</span>
        {#if !compact}
          <span class="cc-col cc-col-age">{_age(row.last_attempt_at || row.created_at)}</span>
          <span class="cc-col {_modeCls(row.mode)}">{(row.mode || '?').toUpperCase()}</span>
        {/if}
        <span class="cc-col cc-col-actions">
          <button type="button" class="cc-kill"
            disabled={_killing.has(row.id)}
            title={row.child_order_ids && row.child_order_ids.length
              ? `Cancel this chase + ${row.child_order_ids.length} auto-attached child order${row.child_order_ids.length === 1 ? '' : 's'}.`
              : 'Cancel this chase'}
            onclick={() => _kill(row)}>
            {_killing.has(row.id) ? '…' : 'Kill'}
          </button>
        </span>
      </div>
    {/each}
  </div>
</div>
{/if}

<style>
  .cc-root {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    /* Operator: "chase in flight card has data crossing the
       boundaries. make it look like other cards". width:100% +
       min-width:0 lets the grid below shrink instead of pushing
       past the bucket-card's right edge. */
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }
  .cc-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .cc-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(248, 113, 113, 0.8);
  }
  .cc-count {
    display: inline-flex;
    align-items: center;
    padding: 0 0.4rem;
    border-radius: 8px;
    background: rgba(248, 113, 113, 0.18);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--c-short);
    font-size: var(--fs-xs);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .cc-spacer { flex: 1 1 0; }
  .cc-err {
    color: var(--c-short);
    font-size: var(--fs-xs);
    font-family: monospace;
  }
  .cc-reconcile-msg {
    color: var(--c-long);
    font-size: var(--fs-xs);
    font-family: monospace;
  }
  .cc-reconcile {
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(125, 211, 252, 0.45);
    background: transparent;
    color: #7dd3fc;
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    text-transform: uppercase;
  }
  .cc-reconcile:hover:not(:disabled) {
    background: var(--algo-sky-bg);
    color: #bae6fd;
  }
  .cc-reconcile:disabled { opacity: 0.45; cursor: progress; }
  .cc-grid {
    display: flex;
    flex-direction: column;
    gap: 0;
    width: 100%;
    /* Scroll-x on overflow: protects against narrow viewports where
       the column total exceeds available width. The grid itself
       still uses fr units so wide viewports never trigger the
       scrollbar. */
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .cc-row {
    display: grid;
    /* Mix of fr columns and tight fixed widths so the row fits the
       card on common viewports. minmax(0, ...) lets cells shrink
       past their content with text-overflow:ellipsis. */
    grid-template-columns:
      minmax(0, 1.4fr)   /* acct  */
      minmax(0, 0.6fr)   /* side  */
      minmax(0, 0.6fr)   /* qty   */
      minmax(0, 2.4fr)   /* sym   */
      minmax(0, 1fr)     /* limit */
      minmax(0, 0.8fr)   /* att   */
      minmax(0, 0.6fr)   /* age   */
      minmax(0, 0.8fr)   /* mode  */
      minmax(2.6rem, auto); /* kill — locked min so the button always fits */
    column-gap: 0.4rem;
    align-items: center;
    padding: 0.32rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    color: var(--algo-slate);
    min-width: 0;
  }
  .cc-compact .cc-row {
    grid-template-columns:
      minmax(0, 1.4fr) minmax(0, 0.6fr) minmax(0, 0.6fr)
      minmax(0, 2.4fr) minmax(0, 1fr) minmax(0, 0.8fr)
      minmax(2.6rem, auto);
  }
  /* Child rows — auto-attached legs of the parent above (typically
     the protective wing of a SELL option). Subtle violet left-rule
     so the visual cluster of parent + child reads as one unit. */
  .cc-row-child {
    background: rgba(192, 132, 252, 0.05);
    box-shadow: inset 3px 0 0 0 rgba(192, 132, 252, 0.65);
  }
  .cc-child-tee {
    color: rgba(192, 132, 252, 0.85);
    font-weight: 700;
    margin-right: 0.25rem;
  }
  .cc-row-h {
    color: var(--algo-muted);
    font-size: var(--fs-xs);
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
  /* Audit fix (M-6) — small dimmed chip next to the limit price when
     the chase has re-quoted the order. Operator sees at a glance
     "this is leg-iteration N's price, not the entry". */
  .cc-limit-moved {
    margin-left: 0.18rem;
    color: rgba(180, 200, 230, 0.5);
    font-size: var(--fs-xs);
    font-weight: 600;
  }
  .cc-side-buy  { color: var(--c-long); font-weight: 700; }
  .cc-side-sell { color: var(--c-short); font-weight: 700; }
  .cc-mode {
    font-size: var(--fs-2xs);
    font-weight: 800;
    padding: 0 0.32rem;
    border-radius: 2px;
    text-align: center;
    border: 1px solid currentColor;
  }
  .cc-mode-live   { color: var(--c-long); }
  .cc-mode-paper  { color: #7dd3fc; }
  .cc-mode-shadow { color: var(--c-action); }
  .cc-col-actions { text-align: right; }
  .cc-kill {
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(248, 113, 113, 0.45);
    background: transparent;
    color: rgba(248, 113, 113, 0.9);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    cursor: pointer;
    text-transform: uppercase;
  }
  .cc-kill:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.12);
    color: var(--c-short);
  }
  .cc-kill:disabled { opacity: 0.45; cursor: progress; }
  .cc-countdown {
    font-size: var(--fs-2xs);
    color: rgba(180, 200, 230, 0.6);
    font-variant-numeric: tabular-nums;
  }
  .cc-countdown.cc-requoting {
    color: var(--c-action);
  }
</style>
