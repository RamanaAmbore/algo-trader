<script>
  /**
   * NavCard — partner NAV slice + optional firm-aggregate panel.
   *
   * Rendered at the top of /performance, between the page header and the
   * existing tab strip. Hidden entirely for anonymous / demo sessions.
   *
   * Role rules:
   *   partner              → single "YOUR SHARE" panel
   *   designated / admin   → two panels: YOUR SHARE (left) + FIRM NAV (right)
   *                          YOUR SHARE panel hides when share_pct === 0
   *   anonymous / 401      → card hidden (silent swallow)
   */
  import { onMount, onDestroy } from 'svelte';
  import { fetchMyNav, fetchFirmNavPublic } from '$lib/api';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { authStore, visibleInterval } from '$lib/stores';
  import { priceFmt, pctFmt } from '$lib/format';

  // Reactive auth state — bridged through $state so reactivity is stable
  // across CSR hydration (avoids the stale-derived $store.x pattern).
  let _auth = $state({ token: null, user: null });
  const _unsub = authStore.subscribe(v => { _auth = v; });
  onDestroy(() => _unsub());

  // Is the visitor authenticated?
  const isLoggedIn  = $derived(!!_auth.token && !!_auth.user);
  const role        = $derived(_auth.user?.role ?? '');
  // Firm-aggregate NAV view — designated (firm owner) + admin (operational
  // support) only. Partner sees per-LP slice via /api/nav/me.
  const canSeeFirm  = $derived(role === 'designated' || role === 'admin');

  /** @type {'loading'|'ready'|'hidden'} */
  let status      = $state('loading');
  let nav         = $state(/** @type {any} */ (null));

  // Computed display values — all derived from the `nav` state atom.
  const sharePct    = $derived(nav ? Number(nav.share_pct ?? 0) : 0);
  const shareNav    = $derived(nav ? Number(nav.share_nav ?? 0) : 0);
  const shareDayPnl = $derived(nav ? Number(nav.share_day_pnl ?? 0) : 0);
  const shareCumPnl = $derived(nav ? Number(nav.share_cum_pnl ?? 0) : 0);
  const contribution= $derived(nav ? Number(nav.contribution ?? 0) : 0);
  const firmNav     = $derived(nav ? Number(nav.firm_nav ?? 0) : 0);
  const firmDayPnl  = $derived(nav ? Number(nav.firm_day_pnl ?? 0) : 0);
  const firmCumPnl  = $derived(nav ? Number(nav.firm_cum_pnl ?? 0) : 0);
  const partnerCount= $derived(nav ? (nav.partner_count ?? 0) : 0);
  const asOf        = $derived(nav?.as_of ?? '');

  // Day P&L % relative to NAV (avoids /0 when nav is fresh/zero)
  const shareDayPct = $derived(shareNav > 0 ? (shareDayPnl / shareNav * 100) : 0);
  const firmDayPct  = $derived(firmNav  > 0 ? (firmDayPnl  / firmNav  * 100) : 0);

  // Show YOUR SHARE panel only when the user has a stake
  const showMyShare = $derived(isLoggedIn && sharePct > 0);
  // Show FIRM NAV panel — designated/admin via /me/nav OR ANYONE
  // (including anonymous visitors) via the public /firm-nav route.
  // The public endpoint returns firm-aggregate numbers only; no
  // share/role data leaks.
  const showFirm    = $derived(firmNav > 0);
  // Card is visible when at least one panel should render
  const cardVisible = $derived(showMyShare || showFirm);

  async function load() {
    // Authenticated path — pulls share slice + firm aggregate (when
    // the operator's role permits) in one request.
    if (isLoggedIn) {
      try {
        const data = await fetchMyNav();
        nav    = data;
        status = 'ready';
        return;
      } catch (_) {
        // 401 / network — fall through to the public path so the
        // operator at least sees the firm-aggregate panel.
      }
    }
    // Anonymous / fallback — public firm-aggregate only.
    try {
      const data = await fetchFirmNavPublic();
      nav    = data;
      status = 'ready';
    } catch (_) {
      // Only collapse the card to hidden on the FIRST-load failure
      // (nav still null). If we already had a successful payload,
      // keep showing it — operator gets last-good values until the
      // next poll lands fresh data instead of the card vanishing
      // mid-session on a transient blip.
      if (!nav) status = 'hidden';
    }
  }

  let stopInterval = () => {};

  // Tick-flash — brief directional pulse (green up / red down) on the
  // big-value rows when the 60s poll lands a different number. Matches
  // PositionStrip + /admin/derivatives convention so the operator's
  // muscle memory for "this just refreshed" works everywhere. First
  // sample establishes baseline (no flash on mount).
  const flash = createTickFlash({ threshold: 0, durationMs: 350 });
  $effect(() => {
    flash.update('shareNav',    shareNav);
    flash.update('shareDayPnl', shareDayPnl);
    flash.update('firmNav',     firmNav);
    flash.update('firmDayPnl',  firmDayPnl);
  });

  onMount(() => {
    load();
    stopInterval = visibleInterval(load, 60_000);
  });

  onDestroy(() => {
    stopInterval();
    flash.dispose();
  });

  // Re-fetch when auth state changes (e.g. user logs in mid-session).
  // The $effect would otherwise fire on first mount alongside onMount,
  // doubling the initial /api/nav/me call. Track the prior value inside
  // the effect itself (initialized to undefined) so the first run is a
  // no-op "remember the current value"; subsequent runs only call load()
  // when isLoggedIn actually flips.
  let _prevLoggedIn = $state(/** @type {boolean | undefined} */ (undefined));
  $effect(() => {
    const now = isLoggedIn;
    if (_prevLoggedIn !== undefined && now !== _prevLoggedIn) {
      load();
    }
    _prevLoggedIn = now;
  });

  function signLabel(val) {
    if (val > 0) return '+';
    if (val < 0) return '';   // minus already in number
    return '';
  }

  function pnlClass(val) {
    if (val > 0) return 'nav-gain';
    if (val < 0) return 'nav-loss';
    return 'nav-zero';
  }
</script>

{#if status === 'loading' && isLoggedIn}
  <!-- Skeleton — shown only for ~1 s on first paint when the user is
       authenticated. Anonymous sessions skip straight to hidden. -->
  <div class="nav-card nav-skeleton" aria-hidden="true">
    <div class="nav-skel-bar nav-skel-title"></div>
    <div class="nav-skel-bar nav-skel-val"></div>
    <div class="nav-skel-bar nav-skel-sub"></div>
  </div>
{:else if status === 'ready' && cardVisible}
  <div class="nav-card" class:nav-two-panels={showMyShare && showFirm}>

    {#if showMyShare}
      <div class="nav-panel">
        <div class="nav-panel-label">YOUR SHARE</div>
        <div class="nav-big tabular-nums {flash.classOf('shareNav')}">
          <span class="nav-currency">₹</span>{priceFmt(shareNav)}
        </div>
        <div class="nav-sub {pnlClass(shareDayPnl)} tabular-nums {flash.classOf('shareDayPnl')}">
          {signLabel(shareDayPnl)}₹{priceFmt(Math.abs(shareDayPnl))} today
          ({signLabel(shareDayPct)}{pctFmt(Math.abs(shareDayPct))}%)
        </div>
        <div class="nav-meta tabular-nums">
          contribution: ₹{priceFmt(contribution)}
          &nbsp;·&nbsp;
          share: {pctFmt(sharePct)}%
        </div>
        {#if shareCumPnl !== 0}
          <div class="nav-cum {pnlClass(shareCumPnl)} tabular-nums">
            Cumulative: {signLabel(shareCumPnl)}₹{priceFmt(Math.abs(shareCumPnl))}
          </div>
        {/if}
      </div>
    {/if}

    {#if showFirm}
      <div class="nav-panel nav-panel-firm" class:nav-panel-divider={showMyShare}>
        <div class="nav-panel-label">FIRM NAV</div>
        <div class="nav-big tabular-nums {flash.classOf('firmNav')}">
          <span class="nav-currency">₹</span>{priceFmt(firmNav)}
        </div>
        <div class="nav-sub {pnlClass(firmDayPnl)} tabular-nums {flash.classOf('firmDayPnl')}">
          {signLabel(firmDayPnl)}₹{priceFmt(Math.abs(firmDayPnl))} today
          ({signLabel(firmDayPct)}{pctFmt(Math.abs(firmDayPct))}%)
        </div>
        {#if partnerCount > 0}
          <div class="nav-meta">{partnerCount} partner{partnerCount === 1 ? '' : 's'}</div>
        {/if}
        {#if firmCumPnl !== 0}
          <div class="nav-cum {pnlClass(firmCumPnl)} tabular-nums">
            Cumulative: {signLabel(firmCumPnl)}₹{priceFmt(Math.abs(firmCumPnl))}
          </div>
        {/if}
      </div>
    {/if}

    {#if asOf}
      <div class="nav-as-of">as of {asOf}</div>
    {/if}

  </div>
{/if}

<style>
  /* ── Card shell ─────────────────────────────────────────────────── */
  /* Operator: "flip card decoration between first and second cards
     in performance page." NavCard (primary content — the partner's
     NAV slice) swapped to the WARMER prominent treatment that was
     on .perf-strategy. Strategy thesis below it gets the softer
     palette in return so the visual hierarchy now reads NAV first,
     context-blurb second.

     Colors are sourced from CSS custom properties set by the parent
     route wrapper (.card-theme-cream on /performance, .card-theme-dark
     on /dashboard). Cream values are the defaults when no wrapper is
     present so legacy callers see no change. */
  .nav-card {
    background: var(--card-bg, #f0ead8);
    border: 1px solid var(--card-border, #d4c89f);
    border-radius: 6px;
    padding: 0.9rem 1.1rem 0.75rem;
    margin-bottom: 1rem;
    position: relative;
  }

  /* Two-panel mode: CSS grid 1fr 1fr at ≥768px, stacked below */
  .nav-two-panels {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 1.5rem;
    /* as-of line spans the full width */
    grid-template-rows: auto auto;
  }
  /* The as-of line should span both columns when in two-panel mode */
  .nav-two-panels .nav-as-of {
    grid-column: 1 / -1;
  }

  @media (max-width: 767px) {
    .nav-two-panels {
      grid-template-columns: 1fr;
      gap: 1rem 0;
    }
  }

  /* ── Panel ──────────────────────────────────────────────────────── */
  .nav-panel {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
  }

  /* Vertical divider between panels — replaced by padding gap on mobile */
  @media (min-width: 768px) {
    .nav-panel-divider {
      border-left: 1px solid var(--card-divider, #e0d9cc);
      padding-left: 1.25rem;
    }
  }

  /* Firm NAV — content is centre-aligned inside its panel. In two-
     panel mode this balances the visual weight (YOUR SHARE flush
     left, FIRM NAV centred in its column); when FIRM NAV renders
     alone (designated/admin without a personal share) it sits
     centred in the card via the rule below. */
  .nav-panel-firm {
    align-items: center;
    text-align: center;
  }

  /* Single-panel (partner / firm-only): cap width + centre */
  :not(.nav-two-panels) > .nav-panel {
    max-width: 28rem;
    margin: 0 auto;
  }

  /* ── Labels ─────────────────────────────────────────────────────── */
  .nav-panel-label {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--card-label-text, #c8a84b);
    text-transform: uppercase;
    margin-bottom: 0.1rem;
  }

  /* ── Big NAV number ─────────────────────────────────────────────── */
  .nav-big {
    font-size: 1.55rem;
    font-weight: 700;
    color: var(--card-cell-text, #0c1830);
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .nav-currency {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--card-currency-text, #4a5872);
    margin-right: 0.05em;
  }

  /* ── Sub-line (day P&L) ─────────────────────────────────────────── */
  .nav-sub {
    font-size: 0.75rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  /* ── Meta row (contribution · share%) ──────────────────────────── */
  .nav-meta {
    font-size: 0.63rem;
    color: var(--card-muted-text, #7a6b52);
    margin-top: 0.08rem;
    font-variant-numeric: tabular-nums;
  }

  /* ── Cumulative P&L ─────────────────────────────────────────────── */
  .nav-cum {
    font-size: 0.63rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    margin-top: 0.05rem;
  }

  /* ── As-of timestamp ────────────────────────────────────────────── */
  /* Centred to match the FIRM NAV panel content (label / big number /
     sub-line / meta / cumulative all centred). The earlier right-
     alignment broke the visual rhythm — every other element in the
     card sits on the centre axis, the timestamp was alone hanging
     off the right edge. */
  .nav-as-of {
    font-size: 0.58rem;
    color: var(--card-as-of-text, #a89878);
    margin-top: 0.55rem;
    text-align: center;
  }

  /* ── P&L colour tokens — sourced from card theme vars ───────────── */
  .nav-gain { color: var(--card-gain-text, #1a6b3a); }
  .nav-loss { color: var(--card-loss-text, #9b1c1c); }
  .nav-zero { color: var(--card-zero-text, #7a6b52); }

  /* ── Tick-flash — directional pulse on poll update ───────────────
     Brief green / red background tint that fades over ~550ms when
     the 60s poll lands a different number. Matches the visual
     vocabulary on PositionStrip + /admin/derivatives. The start color
     is read from --card-tf-up-start / --card-tf-down-start so the
     cream variant gets deep saturated tones (matching .nav-gain /
     .nav-loss) while the dark variant uses the algo green-400 / red-400
     palette. */
  @keyframes nav-tf-up {
    0%   { background-color: var(--card-tf-up-start, rgba(26, 107, 58, 0.20)); }
    100% { background-color: transparent; }
  }
  @keyframes nav-tf-down {
    0%   { background-color: var(--card-tf-down-start, rgba(155, 28, 28, 0.20)); }
    100% { background-color: transparent; }
  }
  .tf-up   {
    animation: nav-tf-up   350ms ease-out;
    border-radius: 0.35rem;
  }
  .tf-down {
    animation: nav-tf-down 350ms ease-out;
    border-radius: 0.35rem;
  }

  /* ── Skeleton ───────────────────────────────────────────────────── */
  .nav-skeleton {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .nav-skel-bar {
    background: linear-gradient(
      90deg,
      var(--card-skel-from, #ede8df) 25%,
      var(--card-skel-to,   #f5f0e8) 50%,
      var(--card-skel-from, #ede8df) 75%
    );
    background-size: 200% 100%;
    animation: nav-skel-shimmer 1.4s ease-in-out infinite;
    border-radius: 3px;
    height: 0.75rem;
  }
  .nav-skel-title { width: 5rem; height: 0.55rem; }
  .nav-skel-val   { width: 8rem; height: 1.4rem; }
  .nav-skel-sub   { width: 12rem; }

  @keyframes nav-skel-shimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .tf-up, .tf-down     { animation: none; }
    .nav-skel-bar        { animation: none; }
  }

  /* tabular-nums helper class applied via inline class on elements */
  .tabular-nums { font-variant-numeric: tabular-nums; }
</style>
