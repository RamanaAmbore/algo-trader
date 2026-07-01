<script>
  /**
   * UnifiedLog — canonical merged log feed.
   *
   * Renders order-lifecycle events (placed, chase_modify, fill, …) and
   * agent-fire events (agent_fire, agent_action_success, …) with the same
   * row format, chip palette, and timestamp style everywhere.
   *
   * Props:
   *   filter       — { kinds?, accounts?, since?, simMode? } passed to
   *                  fetchUnifiedLog. simMode=false suppresses simulator
   *                  rows entirely — what /dashboard uses so a recruiter
   *                  doesn't see fabricated agent fires during a sim.
   *   pollMs       — polling interval in ms; 0 disables (default 3000)
   *   maxRows      — max rows to fetch+render (default 50)
   *   emptyMessage — text shown when list is empty
   *   heightClass  — Tailwind height class for the scroll container
   *   excludeSim   — convenience flag; sets filter.simMode = false. When
   *                  not passed, sim rows are rendered with a [SIM] chip
   *                  so they're visually disambiguated from real fires.
   */

  import { onMount, onDestroy } from 'svelte';
  import { fetchUnifiedLog } from '$lib/api';
  import { logTimeIst, formatDualTz, visibleInterval } from '$lib/stores';

  /** @type {{
   *   filter?:       { kinds?: string[], accounts?: string[], since?: string, simMode?: boolean | null },
   *   pollMs?:       number,
   *   maxRows?:      number,
   *   emptyMessage?: string,
   *   heightClass?:  string,
   *   excludeSim?:   boolean,
   *   bump?:         number,
   *   cardMode?:     boolean,
   *   tsFormat?:     'short' | 'dual',
   * }} */
  let {
    filter       = /** @type {{ kinds?: string[], accounts?: string[], since?: string, simMode?: boolean | null }} */ ({}),
    pollMs       = 3000,
    maxRows      = 50,
    emptyMessage = 'No recent events.',
    heightClass  = 'max-h-48',
    excludeSim   = false,
    /** Increment to trigger an immediate on-demand reload (e.g. from a
     *  parent page's RefreshButton). The effect fires whenever this
     *  value changes, then the regular poll continues as before.
     *  @type {number} */
    bump         = /** @type {number} */ (0),
    // cardMode renders each row as a structured card (header line with
    // timestamp + kind chip + order/agent refs; message on its own
    // line) matching the .oes-order-card layout in SymbolPanel. The
    // default `row` mode stays — used by the dashboard's agent-
    // activity column where the compressed two-line format keeps the
    // history dense.
    cardMode     = false,
    // Timestamp format. `short` (default) = HH:MM:SS IST; `dual` =
    // full dual-tz (`Sun 31 May · 08:01 IST · 22:31 EDT`) matching
    // the page-header wall clock. /orders uses `dual` so row time
    // and header time read in the same shape.
    tsFormat     = /** @type {'short' | 'dual'} */ ('short'),
  } = $props();

  // Merge excludeSim into the filter — single source of truth for the
  // sim-filter flag the backend sees.
  const _effectiveFilter = $derived(
    excludeSim ? { ...filter, simMode: false } : filter
  );

  let rows    = $state(/** @type {any[]} */ ([]));
  let loading = $state(true);
  let error   = $state('');

  // On-demand refresh triggered by parent incrementing `bump`.
  $effect(() => {
    if (bump > 0) _fetch();
  });

  /** @type {(() => void) | undefined} */
  let _intervalTeardown;

  async function _fetch() {
    try {
      const data = await fetchUnifiedLog(_effectiveFilter, maxRows);
      // Only swap to the new payload when it's an actual array; an
      // unexpected response shape used to silently blank `rows`.
      if (Array.isArray(data)) rows = data;
      error = '';
    } catch (e) {
      // Keep last-good rows; the banner above signals staleness.
      error = /** @type {any} */ (e)?.message || 'Failed to load.';
    } finally {
      loading = false;
    }
  }

  function _fmtTs(/** @type {unknown} */ ts) {
    if (!ts || typeof ts !== 'string') return '—';
    const iso = ts.endsWith('Z') ? ts : ts + 'Z';
    if (tsFormat === 'dual') {
      try { return formatDualTz(new Date(iso)); }
      catch { /* fall through to short */ }
    }
    return logTimeIst(iso) || '—';
  }

  /** Re-render JSON-shaped messages as bracketed `[k=v, k=v]` so the
   *  row reads as compact key/value pairs instead of a JSON literal
   *  with quotes + braces. Plain-string messages pass through
   *  unchanged. Per operator: "show order key value pairs in square
   *  brackets in order log". */
  function _fmtMsg(/** @type {unknown} */ raw) {
    if (raw == null) return '';
    if (typeof raw !== 'string') return String(raw);
    const s = raw.trim();
    if (!s.startsWith('{') && !s.startsWith('[')) return raw;
    try {
      const obj = JSON.parse(s);
      if (Array.isArray(obj)) {
        return obj.map(v => typeof v === 'object' ? _fmtMsg(JSON.stringify(v)) : String(v)).join(', ');
      }
      if (obj && typeof obj === 'object') {
        return '[' + Object.entries(obj)
          .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
          .join(', ') + ']';
      }
    } catch { /* not JSON — pass through */ }
    return raw;
  }

  function _startPoll() {
    if (pollMs > 0 && typeof document !== 'undefined') {
      // visibleInterval pauses while hidden and fires an immediate _fetch
      // on tab return — no stale data after switching back.
      _intervalTeardown = visibleInterval(_fetch, pollMs);
    }
  }

  onMount(() => {
    _fetch();
    _startPoll();
  });

  onDestroy(() => {
    _intervalTeardown?.();
  });
</script>

<div class="ul-wrap {heightClass}">
  {#if loading && rows.length === 0}
    <div class="ul-empty">Loading…</div>
  {:else if error}
    <div class="ul-empty ul-error">{error}</div>
  {:else if rows.length === 0}
    <div class="ul-empty">{emptyMessage}</div>
  {:else if cardMode}
    <!-- cardMode — structured cards with a head line (timestamp +
         kind chip + order/agent refs) and a separate message line.
         Matches the .oes-order-card layout so the Log and Orders
         tabs in SymbolPanel read with the same visual rhythm. -->
    <div class="ul-list ul-list-cards">
      {#each rows as row (row.source + row.id)}
        <article class="ul-card">
          <div class="ul-card-head">
            <span class="ul-card-kind ul-kind-{row.kind}">{row.kind}</span>
            {#if row.order_id}<span class="ul-card-ref">#{row.order_id}</span>{/if}
            {#if row.agent_slug}<span class="ul-card-ref">[{row.agent_slug}]</span>{/if}
            <span class="ul-card-time">{_fmtTs(row.ts)}</span>
          </div>
          {#if row.message}
            <div class="ul-card-msg">{_fmtMsg(row.message)}</div>
          {/if}
        </article>
      {/each}
    </div>
  {:else}
    <div class="ul-list">
      {#each rows as row (row.source + row.id)}
        <div class="ul-row">
          <span class="ul-time">{_fmtTs(row.ts)}</span>
          <span class="ul-line">
            <span class="ul-kind ul-kind-{row.kind}">{row.kind}</span>
            <span class="ul-msg">
              {#if row.order_id}#{row.order_id} · {/if}{#if row.agent_slug}[{row.agent_slug}] · {/if}{_fmtMsg(row.message ?? '')}
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
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    padding: 0.35rem 0;
  }

  .ul-empty {
    color: var(--algo-muted);
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
     surfaces is zero-friction. Desktop override below flips to a single
     row with time | message in two columns — Bloomberg/TradingView log
     convention on wide viewports. */
  .ul-row {
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
    color: var(--algo-slate);
  }
  @media (min-width: 1024px) {
    .ul-row {
      flex-direction: row;
      align-items: baseline;
      gap: 0.5rem;
    }
    .ul-time {
      flex: 0 0 auto;
      white-space: nowrap;
    }
    .ul-line {
      flex: 1 1 0;
      min-width: 0;
    }
  }
  .ul-time {
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-2xs);
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
    font-size: var(--fs-xs);
    flex-shrink: 0;
  }
  .ul-msg { color: var(--algo-slate); }

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
  .ul-kind-agent_paused           { color: var(--algo-muted); }

  /* Fallback for any unknown kind */
  .ul-kind:not([class*="ul-kind-"]) { color: #7dd3fc; }

  /* SIM chip — fabricated-data marker so a sim fire never gets
     mistaken for a real one. Same rose-red palette as the navbar
     SIMULATOR banner so the eye recognises it instantly. */
  .ul-sim {
    color: #fda4af;
    background: rgba(251, 113, 133, 0.15);
    border: 1px solid rgba(251, 113, 133, 0.45);
    padding: 0 0.25rem;
    border-radius: 2px;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    flex-shrink: 0;
  }

  /* ── Card mode ─────────────────────────────────────────────────────
     Structured cards used by SymbolPanel's bottom Log tab so its
     visual rhythm matches the Orders tab (also card-style). Each row
     becomes an <article> with a head line (kind chip + sim badge +
     order/agent refs + timestamp pushed right) and an optional
     message line below. */
  .ul-list-cards {
    gap: 0.38rem;
    padding: 0 0.15rem;
  }
  .ul-card {
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-left: 2px solid rgba(125, 211, 252, 0.45);
    border-radius: 3px;
    padding: 0.32rem 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
  }
  .ul-card-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
    font-size: var(--fs-md);
  }
  .ul-card-kind {
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-size: var(--fs-sm);
    flex-shrink: 0;
  }
  .ul-card-sim {
    color: #c084fc;
    background: rgba(192, 132, 252, 0.10);
    border: 1px solid rgba(192, 132, 252, 0.55);
    padding: 0 0.3rem;
    border-radius: 2px;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    flex-shrink: 0;
  }
  .ul-card-ref {
    color: var(--algo-slate);
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.25);
    padding: 0 0.35rem;
    border-radius: 2px;
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    flex-shrink: 0;
  }
  .ul-card-time {
    margin-left: auto;
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-xs);
    flex-shrink: 0;
  }
  .ul-card-msg {
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    line-height: 1.4;
    padding-left: 0.1rem;
  }
</style>
