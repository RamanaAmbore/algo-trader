<script>
  /**
   * UnifiedLog — canonical merged log feed.
   *
   * Renders order-lifecycle events (placed, chase_modify, fill, …) and
   * agent-fire events (agent_fire, agent_action_success, …) with the same
   * row format, chip palette, and timestamp style everywhere.
   *
   * Props:
   *   filter       — { kinds?, accounts?, since? } passed to fetchUnifiedLog
   *   pollMs       — polling interval in ms; 0 disables (default 3000)
   *   maxRows      — max rows to fetch+render (default 50)
   *   emptyMessage — text shown when list is empty
   *   heightClass  — Tailwind height class for the scroll container
   */

  import { onMount, onDestroy } from 'svelte';
  import { fetchUnifiedLog } from '$lib/api';
  import { logTime } from '$lib/stores';

  /** @type {{
   *   filter?:       { kinds?: string[], accounts?: string[], since?: string },
   *   pollMs?:       number,
   *   maxRows?:      number,
   *   emptyMessage?: string,
   *   heightClass?:  string,
   * }} */
  let {
    filter       = /** @type {{ kinds?: string[], accounts?: string[], since?: string }} */ ({}),
    pollMs       = 3000,
    maxRows      = 50,
    emptyMessage = 'No recent events.',
    heightClass  = 'max-h-48',
  } = $props();

  let rows    = $state(/** @type {any[]} */ ([]));
  let loading = $state(true);
  let error   = $state('');

  /** @type {ReturnType<typeof setInterval> | undefined} */
  let _interval;

  async function _fetch() {
    try {
      const data = await fetchUnifiedLog(filter, maxRows);
      rows  = Array.isArray(data) ? data : [];
      error = '';
    } catch (e) {
      error = /** @type {any} */ (e)?.message || 'Failed to load.';
    } finally {
      loading = false;
    }
  }

  /** Format an ISO UTC timestamp to the canonical dual-zone string used
   *  by logTime(), but only take the first HH:MM:SS run so the column
   *  stays compact. Returns '—' for any unparseable value. */
  function _fmtTs(/** @type {unknown} */ ts) {
    if (!ts || typeof ts !== 'string') return '—';
    const full = logTime(ts.endsWith('Z') ? ts : ts + 'Z');
    if (!full) return '—';
    // logTime returns e.g. "Wed 07 May 09:30:00 IST | Wed 07 May 21:00:00 EDT"
    // — take the first HH:MM:SS run for the compact log column.
    const m = full.match(/\d{2}:\d{2}:\d{2}/);
    return m ? m[0] : full;
  }

  function _startPoll() {
    if (pollMs > 0 && typeof setInterval !== 'undefined') {
      _interval = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return;
        _fetch();
      }, pollMs);
    }
  }

  onMount(() => {
    _fetch();
    _startPoll();
    const _onVisibility = () => {
      if (!document.hidden) _fetch();
    };
    document.addEventListener('visibilitychange', _onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', _onVisibility);
    };
  });

  onDestroy(() => {
    if (_interval) clearInterval(_interval);
  });
</script>

<div class="ul-wrap {heightClass}">
  {#if loading && rows.length === 0}
    <div class="ul-empty">Loading…</div>
  {:else if error}
    <div class="ul-empty ul-error">{error}</div>
  {:else if rows.length === 0}
    <div class="ul-empty">{emptyMessage}</div>
  {:else}
    <div class="ul-list">
      {#each rows as row (row.source + row.id)}
        <div class="ul-row">
          <span class="ul-time">{_fmtTs(row.ts)}</span>
          <span class="ul-line">
            <span class="ul-kind ul-kind-{row.kind}">{row.kind}</span>
            <span class="ul-msg">
              {#if row.order_id}#{row.order_id} · {/if}{#if row.agent_slug}[{row.agent_slug}] · {/if}{row.message ?? ''}
            </span>
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .ul-wrap {
    overflow-y: auto;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    padding: 0.35rem 0;
  }

  .ul-empty {
    color: #7e97b8;
    font-style: italic;
    padding: 0.3rem 0.5rem;
  }
  .ul-error { color: #f87171; font-style: normal; }

  .ul-list {
    display: flex;
    flex-direction: column;
    gap: 0.42rem;
    padding: 0 0.5rem;
  }

  /* Stacked layout: time on its own line (compact), kind + message below.
     Matches OrderEntryShell.svelte's .oes-event-row so copy-paste across
     surfaces is zero-friction. */
  .ul-row {
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
    color: #c8d8f0;
  }
  .ul-time {
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    font-size: 0.5rem;
    letter-spacing: 0.02em;
  }
  .ul-line {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    flex-wrap: wrap;
  }
  .ul-kind {
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.55rem;
    flex-shrink: 0;
  }
  .ul-msg { color: #c8d8f0; }

  /* ── Order-event kind chips ─────────────────────────────────── */
  .ul-kind-placed          { color: #38bdf8; }
  .ul-kind-chase_modify    { color: #fbbf24; }
  .ul-kind-fill            { color: #4ade80; }
  .ul-kind-unfill          { color: #f87171; }
  .ul-kind-reject          { color: #f87171; }
  .ul-kind-preflight_ok    { color: #94a3b8; }
  .ul-kind-preflight_block { color: #f87171; }
  .ul-kind-cancel          { color: #94a3b8; }
  .ul-kind-postback        { color: #c084fc; }
  .ul-kind-error           { color: #f87171; }

  /* ── Agent-event kind chips — violet/fuchsia/pink so rule fires
       are instantly distinguishable from order events. ──────────── */
  .ul-kind-agent_fire             { color: #e879f9; }
  .ul-kind-agent_match            { color: #d946ef; }
  .ul-kind-agent_action_success   { color: #a855f7; }
  .ul-kind-agent_action_error     { color: #f472b6; }
  .ul-kind-agent_skipped          { color: #94a3b8; }
  .ul-kind-agent_paused           { color: #7e97b8; }

  /* Fallback for any unknown kind */
  .ul-kind:not([class*="ul-kind-"]) { color: #7dd3fc; }
</style>
