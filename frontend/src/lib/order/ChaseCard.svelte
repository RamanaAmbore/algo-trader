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
   *   headless?     — when true, suppress the component's own template
   *                   render entirely (no `.cc-root` in the DOM) while
   *                   polling + the `activeCount`/`loadError` bindables
   *                   keep updating in the background. Added 2026-10-23
   *                   so (algo)/+layout.svelte's header chase-chip drawer
   *                   can mount ONE persistent ChaseCard instance (to
   *                   keep the chip's live count correct even while the
   *                   drawer itself is closed) without leaving a hidden,
   *                   still-focusable `.cc-kill` button set in the DOM —
   *                   and without colliding with a second `.cc-root` on
   *                   pages that already embed their own ChaseCard
   *                   (/orders, the order-ticket Chase tab, the Activity
   *                   modal). Default false — every pre-existing call
   *                   site is unaffected.
   *   loadError?    — bindable, mirrors the internal `_err` string (or
   *                   null). Additive alongside `activeCount`/
   *                   `chaseOrderIds` — added so a host that keeps this
   *                   component mounted while hidden (see `headless`
   *                   above) can still surface a failed fetch; the
   *                   component's own `_err` banner only renders inside
   *                   `.cc-root`, which stays hidden whenever `_chases`
   *                   is empty (including on a failed fetch, since `_err`
   *                   does not populate `_chases`).
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { fetchActiveChases, killChase, reconcileAlgoOrders } from '$lib/api';
  import { priceFmt } from '$lib/format';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import CardHeader from '$lib/CardHeader.svelte';
  import { visibleInterval } from '$lib/stores';

  let {
    pollMs   = 3000,
    compact  = false,
    onKilled = null,
    // Bindable outward count — lets the parent gate its container's
    // visibility on whether any chase is active without needing to
    // mount a second poller.
    activeCount = $bindable(0),
    // Bindable outward set of broker_order_ids currently active in
    // the chase engine. The orders page uses this to exclude those
    // orders from the pendingOrders list (they already appear as chase rows).
    chaseOrderIds = $bindable(/** @type {Set<string>} */ (new Set())),
    // Draft payoff orders — shown below real pending rows with a "D"
    // chip in the status column. Inline × removes without a modal;
    // row click opens the order ticket pre-filled in draft mode.
    // Passed from the orders page which reads payoffDrafts.value.
    draftOrders = /** @type {any[]} */ ([]),
    // Called when the operator clicks × on a draft row.
    onDraftRemove = /** @type {((id: number) => void) | null} */ (null),
    // Called when the operator clicks a draft row body (opens ticket).
    onDraftClick  = /** @type {((entry: any) => void) | null} */ (null),
    // OPEN / TRIGGER_PENDING broker orders that are not active chases.
    // Shown as clickable rows between chase rows and draft rows.
    // Clicking opens the order ticket pre-filled for modify.
    pendingOrders = /** @type {any[]} */ ([]),
    // Called when the operator clicks a pending order row body.
    onPendingModify = /** @type {((o: any) => void) | undefined} */ (undefined),
    // See the component doc comment above for both of these.
    headless  = false,
    loadError = $bindable(/** @type {string | null} */ (null)),
  } = $props();

  let _chases  = $state(/** @type {any[]} */ ([]));
  // Guards the mirror effect below until this instance's own FIRST
  // fetch has resolved (success or failure — set in _load()'s finally).
  //
  // 2026-10-23 fix — runaway refetch loop on /orders. Root cause: a
  // freshly-mounted instance starts with `_chases = []` (n = 0) while
  // `activeCount` is a bindable that can already be non-zero — either
  // inherited from a sibling instance sharing the same bound variable
  // (/orders mounts two ChaseCards, one hidden via `{#if}/{:else}`,
  // both `bind:activeCount` to the SAME page variable), or simply
  // because the parent's `{#if}` show/hide condition is itself derived
  // from that same bound variable. Before this fix, the un-guarded
  // effect wrote `activeCount = 0` on every fresh mount BEFORE its own
  // `_load()` had a chance to resolve — on /orders that zero flipped
  // the page's `{#if _showChases}` condition back to hidden, which
  // destroyed the just-mounted instance and remounted the other
  // branch, whose own `onMount` → `_load()` fired a brand new network
  // request, resolved non-empty, wrote `activeCount = 1`, flipped the
  // condition again, and repeated — unboundedly, with one real fetch
  // per cycle. Confirmed empirically: ~350 requests/second.
  // `_loadedOnce` closes this by refusing to touch any bindable until
  // this instance has a real answer, so a fresh empty `_chases` can
  // never stomp an inherited non-zero value.
  let _loadedOnce = $state(false);
  // Mirror the chase count, broker_order_id set, and load-error string
  // out to bindable props so the parent's gate / dedup / error-surfacing
  // logic can react — this effect runs regardless of `headless`, since
  // it's script-level, not gated by the template's own `{#if}` below.
  //
  // `untrack()` around the CURRENT values of `activeCount`/`loadError`
  // is the second half of the 2026-10-23 fix: without it, this effect
  // re-runs whenever ANY holder of the shared bindable writes to it
  // (not just when this instance's own `_chases`/`_err` change) —
  // two simultaneous instances bound to the same variable (a future
  // consumer, or a page that mounts more than one ChaseCard at once)
  // would otherwise re-read and re-write each other's value in a tight
  // ping-pong. Reading the bound props through `untrack` makes this
  // effect depend ONLY on this instance's own local state, which is
  // the invariant a bindable mirror must hold to be safe to share.
  $effect(() => {
    const n = _chases.length;
    if (!_loadedOnce) return;
    const curActiveCount = untrack(() => activeCount);
    if (n !== curActiveCount) activeCount = n;
    chaseOrderIds = new Set(_chases.map(c => String(c.broker_order_id || '')).filter(Boolean));
    const errOrNull = _err || null;
    const curLoadError = untrack(() => loadError);
    if (curLoadError !== errOrNull) loadError = errOrNull;
  });
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
  // Set of all order IDs that exist in the active-chase list. Used in the
  // template to detect orphan children — children whose parent_order_id
  // doesn't resolve to any currently-active chase row.
  const _parentIdsPresent = $derived(new Set(_chases.map(r => r.id)));
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
  // In-flight guard: prevents concurrent fetches when a poll fires
  // before the previous one has resolved (e.g. slow network + 3s interval).
  let _fetching = false;

  async function _load() {
    try {
      _loading = true;
      _err = '';
      _chases = (await fetchActiveChases()) || [];
    } catch (e) {
      _err = e?.message || 'load failed';
    } finally {
      _loading = false;
      // Set on both success AND failure — a failed first fetch must
      // still be allowed to propagate `loadError` to the parent (see
      // `_loadedOnce`'s doc comment above), not get stuck silently
      // deferring forever.
      _loadedOnce = true;
    }
    // Switch the poll cadence based on the result. Empty result =
    // idle; slow the poll way down (default 30s) and hide the card
    // entirely via the template gate. Non-empty = active; fast poll
    // (3s default) so kill state stays current.
    const wasIdle = _idle;
    _idle = _chases.length === 0;
    if (wasIdle !== _idle) _rescheduleTimer();
  }

  // _poll — in-flight-guarded wrapper for the recurring interval.
  // Direct callers (_reconcile, onMount) bypass the guard intentionally:
  // those are single-shot, not recurring. Only the recurring timer path
  // needs the guard to prevent pile-up.
  async function _poll() {
    if (_fetching) return;
    _fetching = true;
    try {
      await _load();
    } finally {
      _fetching = false;
    }
  }

  function _rescheduleTimer() {
    // Audit fix — `visibleInterval` pauses on `document.hidden`, so
    // when the operator switches away from the tab the polling stops
    // (was `setInterval` with no visibility gate; chase polled every
    // 3 s even on a hidden tab, generating background network noise).
    if (_timer) { _timer(); _timer = null; }
    const ms = _idle ? Math.max(15000, pollMs * 10) : Math.max(1000, pollMs);
    _timer = visibleInterval(_poll, ms);
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
          // 2026-10-23 Kill-lifecycle fix — `ok` alone used to be
          // trusted as success even when the SAME response carried a
          // non-null `err` (the backend's own pre-fix bug: it always
          // returned ok:true, even on a genuine broker-cancel failure).
          // A real success never carries an error message — treat
          // `ok && err` as a failure too, as defense in depth even
          // after the backend fix (operator instruction).
          const ok = !!r?.ok && !r?.err;
          return { id, ok, err: r?.err || null, pending: !!r?.pending };
        } catch (e) {
          return { id, ok: false, err: e?.message || 'failed', pending: false };
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
        // A toast — not just the in-card `_err` banner — because (a)
        // ChaseCard's own 3s poll (`_load()`) unconditionally clears
        // `_err` at the start of every fetch, so the banner can vanish
        // within 3s of appearing, and (b) the ENTIRE card collapses to
        // nothing (`{#if !headless && (_chases.length || ...)}`) if a
        // kill success elsewhere in the same cascade emptied `_chases`,
        // taking the banner down with it. `pending` (chase still
        // actively cancelling its own replacement order) is a distinct,
        // milder case — warning, not error, since nothing has actually
        // failed yet.
        const msg = `Kill #${f.id} failed: ${f.err || 'failed'}`;
        if (f.pending) toast.warning(msg);
        else toast.error(msg);
      }
    } finally {
      const next = new Set(_killing);
      for (const id of toKill) next.delete(id);
      _killing = next;
    }
  }

  // Countdown to the chase's next re-quote attempt (2026-10-15, operator:
  // "instead of showing up elapsed time, do count down for how much time
  // chase is going to wait before taking action" — replaces the former
  // `_age()` elapsed-since-created display, which is removed; this is now
  // the row's sole timing column). `nowSec` is the component's existing
  // 1-second `_nowSec` clock, so this ticks every second between polls.
  // '—' for a row with no pending next attempt (no `next_attempt_at`, or
  // not currently `OPEN`) — e.g. a chase that's between poll cycles
  // with nothing scheduled, or a terminal row.
  //
  // 2026-10-23 fix — this was gated on `status === 'active'`, a value
  // GET /api/orders/chases/active NEVER returns: the real query
  // (`backend/api/routes/orders.py:list_active_chases`) filters
  // `AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"])`, so every row this
  // component ever receives is literally 'OPEN' or 'CANCEL_FAILED'. The
  // countdown and pulse dot (below) were structurally unreachable in
  // production — permanently '—'. CANCEL_FAILED deliberately stays
  // excluded here: that status means the chase's own cancel-and-replace
  // loop has already stopped (the order may still be resting live at
  // the broker, but nothing is scheduling a next re-quote attempt for
  // it), so showing a live countdown for it would be misleading.
  function _nextIn(/** @type {number|null|undefined} */ nextAttemptAt, /** @type {string} */ status, /** @type {number} */ nowSec) {
    if (nextAttemptAt == null || status !== 'OPEN') return null;
    const s = Math.max(0, Math.ceil(nextAttemptAt - nowSec));
    return s;
  }

  function _modeCls(/** @type {string} */ m) {
    const k = String(m || '').toLowerCase();
    if (k === 'live')   return 'cc-mode cc-mode-live';
    if (k === 'paper')  return 'cc-mode cc-mode-paper';
    if (k === 'shadow') return 'cc-mode cc-mode-shadow';
    if (k === 'sim')    return 'cc-mode cc-mode-sim';
    if (k === 'replay') return 'cc-mode cc-mode-replay';
    // Reuses the existing .cc-mode-draft rule (muted/dashed) already used
    // by the hardcoded draft-rows section below — same visual meaning,
    // same class.
    if (k === 'draft')  return 'cc-mode cc-mode-draft';
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
     Root is gated on real chases OR pending broker orders OR draft orders.
     Polling auto-slows in _rescheduleTimer when idle so background
     traffic drops to one fetch every ~30s. -->
{#if !headless && (_chases.length || pendingOrders.length || draftOrders.length)}
<div class="cc-root" class:cc-compact={compact}>
  <!-- 2026-10 CardHeader adoption sweep — this is now the ONE header
       ChaseCard renders, consolidating what used to be a second,
       redundant header some host pages (e.g. /orders) wrapped around
       this component themselves. ActivityLogModal and SymbolPanel mount
       ChaseCard bare with no wrapper at all, so moving the header inside
       the component (rather than leaving it to each host) is also what
       gives those two surfaces the shared CardHeader shade for the
       first time. -->
  <CardHeader title="Chases in flight" showControls={false}>
    {#snippet left()}
      <span class="cc-count">{_chases.length + pendingOrders.length + draftOrders.length}</span>
    {/snippet}
    {#snippet right()}
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
    {/snippet}
  </CardHeader>

  <div class="cc-grid" role="grid">
    <div class="cc-row cc-row-h" role="row">
      <span class="cc-col cc-col-acct">Account</span>
      <span class="cc-col cc-col-side">Side</span>
      <span class="cc-col cc-col-qty">Qty</span>
      <span class="cc-col cc-col-sym">Symbol</span>
      <span class="cc-col cc-col-limit">Limit</span>
      <span class="cc-col cc-col-att">Attempts</span>
      <span class="cc-col cc-col-age" title="Time until the next re-quote attempt">Next</span>
      {#if !compact}
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
        <span class="cc-col cc-col-sym" title={row.symbol}>
          {#if row.status === 'OPEN'}
            <span class="cc-pulse cc-pulse-{(row.transaction_type || '').toLowerCase()}" aria-hidden="true"></span>
          {/if}
          {formatSymbol(row.symbol)}
          {#if row.parent_order_id != null && !_parentIdsPresent.has(row.parent_order_id)}
            <span class="cc-chip cc-chip-o" title="Orphan — parent order not in active chases">O</span>
          {/if}
        </span>
        <!-- Audit fix (M-6) — show current_limit (live re-quoted) when
             the chase has moved off initial_price; otherwise fall back
             to initial_price for the first iteration. A small "·N"
             chip after the price indicates the price has moved through
             N modifies so the operator knows it's not the entry price.
             Title surfaces both values for context.

             2026-10-23 fix — N was raw `row.attempts`, which counts
             PLACEMENTS starting at 1 (chase.py's `for attempt in
             range(1, max_attempts + 1)`: attempt 1 is the initial
             placement, attempt 2 is the first cancel-and-replace
             re-quote). This chip only renders once current_limit has
             actually diverged from initial_price, which can't happen
             before attempt 2 — so after exactly ONE re-quote it read
             "·2" instead of "·1". Subtract 1 so the number reflects
             re-quotes/modifies, matching the chip's own label. -->
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
            <span class="cc-limit-moved">·{Math.max(0, (row.attempts || 0) - 1)}</span>
          {/if}
        </span>
        <span class="cc-col cc-col-att">{row.attempts || 0}</span>
        <span class="cc-col cc-col-age">
          {#if row.next_attempt_at != null && row.status === 'OPEN'}
            {#if _nowSec < row.next_attempt_at}
              <span class="cc-countdown" title="Seconds until next re-quote">{_nextIn(row.next_attempt_at, row.status, _nowSec)}s</span>
            {:else}
              <span class="cc-countdown cc-requoting" title="Re-quoting now">now</span>
            {/if}
          {:else}
            —
          {/if}
        </span>
        {#if !compact}
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

    <!-- Pending broker order rows — OPEN / TRIGGER_PENDING orders that
         are not managed by the chase engine. Shown between chase rows
         and draft rows. Clicking the row opens the order ticket pre-filled
         for modify/cancel. No inline action — cancel goes through the modal. -->
    {#if pendingOrders.length}
      {#if _orderedChases.length}
        <div class="cc-pending-divider" role="separator" aria-label="Pending orders"></div>
      {/if}
      {#each pendingOrders as o (o.order_id ?? o.id)}
        <!-- svelte-ignore a11y_interactive_supports_focus -->
        <div class="cc-row cc-pending-row"
             role="button"
             tabindex="0"
             title="Click to modify or cancel this order"
             onclick={() => onPendingModify?.(o)}
             onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPendingModify?.(o); } }}>
          <!-- Status chip: OPEN or PENDING — both amber (2026-10 P1 audit
               fix; OPEN was previously green, disagreeing with SymbolPanel's
               .oes-status-open and OrderBook's amber "Open" bucket, which
               already group OPEN + TRIGGER_PENDING as the same resting-order
               semantic state). The two chips now share one color but keep
               distinct text labels/classes so the state is still legible. -->
          <span class="cc-col cc-col-acct cc-pending-acct" title={o.account || ''}>
            {#if o.status === 'OPEN'}
              <span class="cc-pending-chip cc-pending-chip-pending" aria-label="Open order">OPEN</span>
            {:else}
              <span class="cc-pending-chip cc-pending-chip-pending" aria-label="Trigger pending">PENDING</span>
            {/if}
          </span>
          <span class="cc-col cc-col-side cc-side-{(o.transaction_type || '').toLowerCase()}">
            {o.transaction_type || '—'}
          </span>
          <span class="cc-col cc-col-qty">{o.quantity ?? '—'}</span>
          <span class="cc-col cc-col-sym" title={o.tradingsymbol || o.symbol}>
            {formatSymbol(o.tradingsymbol || o.symbol || '')}
          </span>
          <span class="cc-col cc-col-limit">
            {#if (o.price ?? 0) > 0}
              ₹{priceFmt(o.price)}
            {:else if (o.trigger_price ?? 0) > 0}
              ₹{priceFmt(o.trigger_price)}
            {:else}
              MKT
            {/if}
          </span>
          <!-- Attempts, Next, Mode cols — empty for pending (no chase data) -->
          <span class="cc-col cc-col-att">—</span>
          <span class="cc-col cc-col-age">—</span>
          {#if !compact}
            <span class="cc-col cc-mode cc-mode-pending">
              {(o.product || '').toUpperCase() || '—'}
            </span>
          {/if}
          <!-- No inline action — modify/cancel goes through the modal -->
          <span class="cc-col cc-col-actions"></span>
        </div>
      {/each}
    {/if}

    <!-- Draft payoff order rows — shown below real chases with an amber
         "D" chip instead of a broker status pill. Clicking the row body
         opens the order ticket pre-filled in draft mode. The inline ×
         removes the draft immediately without a confirm modal. -->
    {#if draftOrders.length}
      {#if _orderedChases.length || pendingOrders.length}
        <div class="cc-draft-divider" role="separator" aria-label="Draft orders"></div>
      {/if}
      {#each draftOrders as draft (draft.id)}
        <!-- svelte-ignore a11y_interactive_supports_focus -->
        <div class="cc-row cc-row-draft"
             role="button"
             tabindex="0"
             title="Click to edit this draft in the order ticket"
             onclick={() => onDraftClick?.(draft)}
             onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onDraftClick?.(draft); } }}>
          <!-- Account col: shows the draft's account when one was
               assigned in OrderTicket; "no account assigned" otherwise
               (backend already masks the account for non-admins, same
               as the real chase rows above — no client-side masking). -->
          <span class="cc-col cc-col-acct cc-draft-acct"
                title={draft.account ? `Draft — ${draft.account}` : 'Draft — no account assigned'}>
            <span class="cc-draft-chip" aria-label="Draft order">D</span>
            {#if draft.account}<span class="cc-draft-acct-text">{draft.account}</span>{/if}
          </span>
          <span class="cc-col cc-col-side cc-side-{(draft.transaction_type || '').toLowerCase()}">
            {draft.transaction_type || '—'}
          </span>
          <!-- Draft qty is signed; show absolute value -->
          <span class="cc-col cc-col-qty">{Math.abs(draft.qty || 0)}</span>
          <span class="cc-col cc-col-sym" title={draft.symbol}>
            {formatSymbol(draft.symbol || '')}
          </span>
          <!-- Limit = avg_cost from the ticket price field -->
          <span class="cc-col cc-col-limit">
            {draft.avg_cost != null ? '₹' + priceFmt(draft.avg_cost) : '—'}
          </span>
          <!-- Attempts col: empty for drafts -->
          <span class="cc-col cc-col-att">—</span>
          <!-- Next col: empty for drafts -->
          <span class="cc-col cc-col-age">—</span>
          {#if !compact}
            <span class="cc-col cc-mode cc-mode-draft">DRAFT</span>
          {/if}
          <!-- × remove button — inline delete, no modal -->
          <span class="cc-col cc-col-actions">
            <button type="button"
                    class="cc-draft-remove"
                    title="Remove this draft order"
                    aria-label="Remove draft {draft.symbol}"
                    onclick={(e) => { e.stopPropagation(); onDraftRemove?.(draft.id); }}>
              ×
            </button>
          </span>
        </div>
      {/each}
    {/if}
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
  /* .cc-header/.cc-label/.cc-spacer retired (2026-10 CardHeader adoption
     sweep) — title now renders via <CardHeader>'s own title prop; the
     push-right-of-Reconcile effect comes from CardHeader's middle
     spacer instead of a local flex:1 spacer element. */
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
    color: var(--algo-sky);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    text-transform: uppercase;
  }
  .cc-reconcile:hover:not(:disabled) {
    background: var(--algo-sky-bg);
    color: var(--algo-sky-text);
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
       past their content with text-overflow:ellipsis.
       2026-10-23 fix (compact=false — the header-drawer/orders/Chain
       mount) — an audit found side ("BUY"/"SELL") and qty collapsing
       to a single ellipsized character on a ~375px viewport, and att
       (attempt count) clipping, because those tracks were left at the
       `minmax(0, …)` pattern the 2026-10-16 fix already identified as
       the mobile footgun for the age column below. A first pass gave
       floors to only those 3 tracks; a Playwright measurement pass
       (not just reasoning about the CSS) showed that just pushed the
       squeeze onto limit/mode instead — a `fr` track with no floor
       absorbs whatever space its locked-minimum siblings take, so
       widening a subset always starves the rest. Measured pre-fix
       baseline on a 360px viewport confirmed limit and mode were
       ALREADY clipping too (29px/76 and 21px/29 content-vs-rendered),
       just not named in the original finding. Every track below now
       carries a real content-derived floor, so the fix is "stop
       squeezing any column past legibility" rather than "move the
       squeeze elsewhere" — re-measured post-fix: side/qty/att/mode all
       render at or above their natural content width, limit improves
       from 29px to 72px (vs. 76px needed). On a container narrower
       than the sum of these floors, `.cc-grid`'s pre-existing
       `overflow-x: auto` (see its own comment above) takes over and
       the row scrolls horizontally instead of rendering any column
       unreadably small — that escape hatch already existed for
       exactly this case and was simply never reached before, because
       no column's floor was previously high enough to trigger it.
       Wide viewports are unaffected: the fr portion of each minmax
       still grows past its floor with room to spare, so nothing
       changes above ~460px. */
    grid-template-columns:
      minmax(2.6rem, 1.4fr) /* acct  */
      minmax(2.1rem, 0.6fr) /* side  */
      minmax(2.1rem, 0.6fr) /* qty   */
      minmax(4rem, 2.4fr)   /* sym   */
      minmax(4.5rem, 1fr)   /* limit */
      minmax(1.6rem, 0.8fr) /* att   */
      /* 2026-10-16 fix — locked min so the countdown ("Next") stays
         legible on narrow (mobile) viewports instead of shrinking to
         an unreadable sliver under flex pressure from the other 8
         columns. Same "locked min so it always fits" rationale as the
         kill button's own minmax below. Operator: "the count time is
         not visible on mobile for chase." */
      minmax(2.1rem, 0.6fr) /* age   */
      minmax(2.6rem, 0.8fr) /* mode  */
      minmax(2.6rem, auto); /* kill — locked min so the button always fits */
    column-gap: 0.4rem;
    align-items: center;
    padding: 0.32rem 0;
    border-bottom: 1px solid var(--sep-color);
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    color: var(--algo-slate);
    min-width: 0;
  }
  .cc-compact .cc-row {
    grid-template-columns:
      minmax(0, 1.4fr) minmax(0, 0.6fr) minmax(0, 0.6fr)
      minmax(0, 2.4fr) minmax(0, 1fr) minmax(0, 0.8fr)
      minmax(2.1rem, 0.6fr)   /* age — always shown now, locked min (2026-10-16) */
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
  .cc-chip-o {
    background: rgba(251,113,133,0.18);
    color: #fb7185;
    border: 1px solid rgba(251,113,133,0.4);
    font-size: var(--fs-md);
    padding: 0 0.3rem;
    border-radius: 3px;
    font-weight: 600;
    margin-left: 0.2rem;
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
    color: var(--text-muted);
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
  /* LIVE — red, matching the navbar MODE_COLOR (the canonical mode→color
     map, see +layout.svelte) and LogPanel's .mode-pill-live. LIVE is the
     only mode that moves real money; a green LIVE pill here used to read
     as "safe", the same footgun the navbar comment already calls out. */
  .cc-mode-live   { color: var(--c-short); }
  .cc-mode-paper  { color: var(--algo-sky); }
  .cc-mode-shadow { color: var(--algo-orange); }
  /* SIM / REPLAY — green, matching the navbar (both sandbox/safe modes). */
  .cc-mode-sim    { color: var(--c-long); }
  .cc-mode-replay { color: var(--c-long); }
  .cc-col-actions { text-align: right; }
  /* Destructive-button recipe unified to the shared --btn-sell-* family
     (2026-10 P1 audit fix) — matches HeldOrdersCard's .held-cancel exactly
     (border + resting color from the token, hover swaps only the
     background). Was a hand-rolled rgba(248,113,113,…) recipe. */
  .cc-kill {
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    border: 1px solid var(--btn-sell-border);
    background: transparent;
    color: var(--btn-sell);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    cursor: pointer;
    text-transform: uppercase;
  }
  .cc-kill:hover:not(:disabled) { background: var(--btn-sell-bg-hi); }
  .cc-kill:disabled { opacity: var(--btn-disabled-opacity); cursor: progress; }
  .cc-countdown {
    /* 2026-10-20 fix — was --fs-2xs (8px, the smallest token in the
       whole app, smaller even than this column's own "Next" header).
       The 2026-10-16 width fix only stopped truncation; it never
       touched legibility. Operator: "I want to see the value
       clearly." Bumped two steps to --fs-sm and given weight so the
       live-ticking number actually reads at a glance, including on
       mobile. */
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--text-med);
    font-variant-numeric: tabular-nums;
  }
  .cc-countdown.cc-requoting {
    color: var(--c-action);
  }
  .cc-pulse {
    display: inline-block;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    margin-right: 0.25rem;
    vertical-align: middle;
    flex-shrink: 0;
    animation: cc-pulse-anim 1.4s ease-in-out infinite;
  }
  @keyframes cc-pulse-anim {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.2; }
  }
  .cc-pulse-buy  { background: var(--c-long); }
  .cc-pulse-sell { background: var(--c-short); }

  /* Draft order rows — amber palette, visually distinct from live chases.
     Row is clickable (opens order ticket pre-filled); cursor:pointer. */
  .cc-row-draft {
    cursor: pointer;
    background: rgba(251, 191, 36, 0.04);
  }
  .cc-row-draft:hover {
    background: rgba(251, 191, 36, 0.10);
  }
  /* "D" chip — amber, same pill shape as algo-status-pill */
  .cc-draft-chip {
    display: inline-block;
    font-size: var(--fs-xs);
    padding: 0.18rem 0.42rem;
    border-radius: 3px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border: 1px solid rgba(251, 191, 36, 0.50);
    background: rgba(251, 191, 36, 0.14);
    color: var(--c-action);
    white-space: nowrap;
  }
  .cc-draft-acct {
    /* Flex so the "D" chip aligns with account text in real rows */
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }
  /* Account text next to the "D" chip when a draft has one assigned —
     same muted tone as the real-row account column, kept compact. */
  .cc-draft-acct-text {
    font-size: var(--fs-md);
    color: var(--c-muted, #94a3b8);
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* DRAFT mode label in the mode column — muted/dashed, matching
     LogPanel's .mode-pill-draft and OrderTimelineDrawer's .otd-mode-draft.
     Unified off the old amber treatment so DRAFT (never placed, no
     broker round-trip) reads as a quieter state than SHADOW's orange,
     consistent with the other two mode-pill surfaces. Scoped to this
     one mode-column pill only — the surrounding draft-row chrome
     (.cc-draft-chip / .cc-draft-remove / .cc-row-draft / .cc-draft-divider)
     is a separate amber visual language for the whole draft row, not a
     mode pill, and is intentionally left unchanged. */
  .cc-mode-draft {
    color: var(--algo-muted);
    border-color: rgba(126, 151, 184, 0.45);
    border-style: dashed;
  }
  /* Visual separator between real chases and draft rows */
  .cc-draft-divider {
    height: 1px;
    background: rgba(251, 191, 36, 0.20);
    margin: 0.25rem 0;
  }
  /* × remove button for drafts — amber palette, smaller than Kill */
  .cc-draft-remove {
    padding: 0.18rem 0.45rem;
    border-radius: 3px;
    border: 1px solid rgba(251, 191, 36, 0.45);
    background: transparent;
    color: rgba(251, 191, 36, 0.85);
    font-size: var(--fs-sm);
    font-weight: 700;
    cursor: pointer;
    line-height: 1;
  }
  .cc-draft-remove:hover {
    background: rgba(251, 191, 36, 0.12);
    color: var(--c-action);
    border-color: rgba(251, 191, 36, 0.75);
  }

  /* Pending broker order rows — OPEN / TRIGGER_PENDING, not in chase engine.
     Sky-tinted background; pointer cursor signals click-to-modify. */
  .cc-pending-row {
    cursor: pointer;
    background: rgba(125, 211, 252, 0.04);
  }
  .cc-pending-row:hover {
    background: rgba(125, 211, 252, 0.10);
  }
  /* Status chip: OPEN and PENDING both amber (2026-10 P1 audit fix —
     see the template comment above for the "why", was green for OPEN). */
  .cc-pending-chip {
    display: inline-block;
    font-size: var(--fs-xs);
    padding: 0.18rem 0.42rem;
    border-radius: 3px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .cc-pending-chip-pending {
    border: 1px solid rgba(251, 191, 36, 0.50);
    background: rgba(251, 191, 36, 0.12);
    color: var(--c-action);
  }
  .cc-pending-acct {
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }
  /* PENDING mode label — sky palette (info) */
  .cc-mode-pending {
    color: var(--algo-sky);
    border-color: rgba(125, 211, 252, 0.45);
  }
  /* Visual separator between chase rows and pending rows */
  .cc-pending-divider {
    height: 1px;
    background: rgba(125, 211, 252, 0.18);
    margin: 0.25rem 0;
  }
</style>
