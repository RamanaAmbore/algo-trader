<script>
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, onDestroy } from 'svelte';
  import { authStore, visibleInterval } from '$lib/stores';
  import {
    fetchSimStatus, fetchPaperStatus,
    fetchReplayStatus, fetchShadowStatus, fetchLiveStatus,
  } from '$lib/api';
  import PositionStrip from '$lib/PositionStrip.svelte';

  const { children } = $props();

  const bullSrc = "/bull.png";

  // ── Activity polling state ─────────────────────────────────────────
  // Declared early so the demo derivation + nav filter below can
  // read `paperStatus.branch`. Pollers themselves run from onMount.
  let simStatus    = $state(/** @type {any} */ ({ active: false }));
  let paperStatus  = $state(/** @type {any} */ ({ enabled: false, open_order_count: 0 }));
  let replayStatus = $state(/** @type {any} */ ({ active: false }));
  let shadowStatus = $state(/** @type {any} */ ({ shadow_active: false }));
  let liveStatus   = $state(/** @type {any} */ ({ live_count: 0 }));

  // Demo mode == anonymous visitor on the prod (main) branch. The
  // `paperStatus.branch` flag arrives via the poll; before the
  // first response lands, `isDemo` is conservatively false so the
  // page doesn't flicker.
  const isDemo = $derived(
    !$authStore.user && paperStatus?.branch === 'main'
  );

  function isActive(/** @type {string} */ href) {
    // Longest-match semantics so sub-pages (e.g. /admin/tokens) don't light up
    // their parent (/admin) in the hamburger at the same time.
    const path = page.url.pathname;
    let bestHref = '';
    for (const link of algoLinks) {
      const h = link.href;
      if ((path === h || path.startsWith(h + '/')) && h.length > bestHref.length) {
        bestHref = h;
      }
    }
    return href === bestHref;
  }

  function signOut() {
    authStore.logout();
    goto('/about');
  }

  // Grouped by operator activity + branch-aware mode entries.
  //
  //   Monitor  — Dashboard, Agents, Orders. Touched daily.
  //   Analyze  — Options.
  //   Modes    — branch-filtered: dev gets Simulator + Replay;
  //              prod gets Paper + Shadow + Live + Replay.
  //   Build    — Terminal, Tokens.
  //   Config   — Settings, Brokers, Users.
  //
  // The `branches` field controls visibility:
  //   'dev'  = non-main branch only
  //   'main' = prod only
  //   absent = always shown
  const _algoLinksAll = [
    // ── Monitor ──
    { href: '/dashboard',        label: 'Dashboard', group: 'monitor' },
    { href: '/agents',           label: 'Agents',    group: 'monitor' },
    { href: '/orders',           label: 'Orders',    group: 'monitor' },
    { href: '/admin/alerts',     label: 'Alerts',    adminOnly: true, group: 'monitor' },
    // ── Analyze ──
    { href: '/admin/options',    label: 'Options',   group: 'analyze' },
    { href: '/admin/pnl',        label: 'P&L',       adminOnly: true, group: 'analyze' },
    // ── Modes (branch-filtered) ──
    { href: '/admin/simulator',  label: 'Simulator', branches: ['dev'],  group: 'modes' },
    { href: '/admin/replay',     label: 'Replay',                        group: 'modes' },
    { href: '/admin/paper',      label: 'Paper',     branches: ['main'], group: 'modes' },
    { href: '/admin/shadow',     label: 'Shadow',    branches: ['main'], group: 'modes' },
    { href: '/admin/live',       label: 'Live',      branches: ['main'], group: 'modes' },
    // ── Build / extend ──
    { href: '/console',          label: 'Terminal',  group: 'build' },
    { href: '/admin/tokens',     label: 'Tokens',    group: 'build' },
    // ── Config ──
    { href: '/admin/health',     label: 'Health',    adminOnly: true, group: 'config' },
    { href: '/admin/settings',   label: 'Settings',  adminOnly: true, group: 'config' },
    { href: '/admin/brokers',    label: 'Brokers',   adminOnly: true, group: 'config' },
    { href: '/admin',            label: 'Users',     adminOnly: true, group: 'config' },
  ];
  // Branch-aware + demo-aware filter.
  const algoLinks = $derived(
    _algoLinksAll.filter(l => {
      if (l.adminOnly && isDemo) return false;
      if (l.branches) {
        const branch = paperStatus?.branch || 'dev';
        const key = branch === 'main' ? 'main' : 'dev';
        return l.branches.includes(key);
      }
      return true;
    })
  );

  /** Footer label that tracks the current page. Earlier the footer
   *  hard-coded "Admin Console" on every algo page; now it reflects
   *  whichever nav entry the operator is on (longest-prefix match
   *  against `_algoLinksAll`). Falls back to "Algo Console" for
   *  unrecognised paths so the footer never reads as empty. */
  const pageLabel = $derived.by(() => {
    const path = page.url.pathname;
    let best = { href: '', label: 'Algo Console' };
    for (const link of _algoLinksAll) {
      const h = link.href;
      if ((path === h || path.startsWith(h + '/')) && h.length > best.href.length) {
        best = { href: h, label: link.label };
      }
    }
    return best.label;
  });

  let menuOpen = $state(false);
  const closeMenu = () => { menuOpen = false; };

  // ── Polling pipeline ───────────────────────────────────────────────
  // The state vars are declared at the top of the script so the
  // `isDemo` derivation + nav filter can read them; the actual
  // pollers + lifecycle live here.
  let simTeardown, paperTeardown, replayTeardown, shadowTeardown, liveTeardown;
  async function pollSim() {
    try { simStatus = await fetchSimStatus(); }
    catch (_) { /* cap flag off or auth gone — treat as idle */ }
  }
  async function pollPaper() {
    try { paperStatus = await fetchPaperStatus(); }
    catch (_) { /* cap flag off, dev branch, or auth gone — treat as idle */ }
  }
  async function pollReplay() {
    try { replayStatus = await fetchReplayStatus(); }
    catch (_) { /* treat as idle */ }
  }
  async function pollShadow() {
    try { shadowStatus = await fetchShadowStatus(); }
    catch (_) { /* treat as idle */ }
  }
  async function pollLive() {
    try { liveStatus = await fetchLiveStatus(); }
    catch (_) { /* treat as idle */ }
  }
  onMount(() => {
    pollSim();    simTeardown    = visibleInterval(pollSim,    4000);
    pollPaper();  paperTeardown  = visibleInterval(pollPaper,  4000);
    pollReplay(); replayTeardown = visibleInterval(pollReplay, 5000);
    pollShadow(); shadowTeardown = visibleInterval(pollShadow, 5000);
    pollLive();   liveTeardown   = visibleInterval(pollLive,   5000);
  });
  onDestroy(() => {
    simTeardown?.(); paperTeardown?.(); replayTeardown?.();
    shadowTeardown?.(); liveTeardown?.();
  });

  // ── Demo / signin redirect ─────────────────────────────────────────
  //   - Logged-in admin       → free pass.
  //   - Anonymous on main     → demo mode (no redirect, isDemo = true).
  //   - Anonymous on non-main → /signin (devs are expected to log in).
  //   - Logged in non-admin   → /signin.
  // While the paper-status poll is still pending the branch is
  // unknown — we stay put rather than flicker the user away.
  $effect(() => {
    const branchKnown = paperStatus?.branch !== undefined;
    if (!$authStore.user && branchKnown && paperStatus.branch !== 'main') {
      goto('/signin');
      return;
    }
    if ($authStore.user && $authStore.user.role !== 'admin') {
      goto('/signin');
    }
  });
</script>

<!-- Algo-side favicon — a circled "algo" mark so the browser tab visually
     separates the trading-console tabs from the public marketing site. -->
<svelte:head>
  <link rel="icon" type="image/svg+xml" href="/algo-favicon.svg" />
</svelte:head>

<div class="algo-viewport">
  <div class="algo-card">
    <!-- Top bar -->
    <header class="algo-navbar">
      <div class="algo-nav-inner hidden lg:flex items-center gap-1 h-12">
        <!-- Vertical ALGO label, flush at the left edge. Bare text —
             no chip, no background, no border. -->
        <span class="algo-vert" aria-hidden="true">ALGO</span>
        <!-- Site label -->
        <button onclick={() => goto('/about')} class="algo-brand">
          <img src={bullSrc} alt="" class="algo-brand-bull" />
          <span class="algo-brand-name">Rambo Quant</span>
        </button>

        <nav class="flex items-center gap-0.5 flex-1">
          {#each algoLinks as link, i}
            {#if i > 0 && link.group !== algoLinks[i - 1].group}
              <span class="algo-nav-sep" aria-hidden="true"></span>
            {/if}
            <button
              onclick={() => goto(link.href)}
              class="algo-nav-btn {isActive(link.href) ? 'algo-nav-btn-active' : ''}"
            >{link.label}</button>
          {/each}
        </nav>

        <!-- Mode badges — env-aware:
             prod (main)  → DEMO (anonymous) | PAPER (paper engine has open orders) | nothing
             dev (non-main) → SIM (sim active) — PAPER never shows on dev (paperStatus.enabled is main-only)
             Word labels (DEMO / PAPER / SIM) instead of single letters
             so the affordance is unambiguous on every page. -->
        {#if isDemo}
          <span class="algo-mode-badge algo-mode-demo"
                title="Demo mode — anonymous visitor, paper-only, no broker connection">DEMO</span>
        {/if}
        {#if paperStatus?.branch !== 'main' && simStatus?.active}
          <span class="algo-mode-badge algo-mode-sim"
                title="Simulator is running — fabricated market data">SIM</span>
        {/if}
        {#if replayStatus?.active}
          <span class="algo-mode-badge algo-mode-replay"
                title="Replay running — tick {replayStatus.tick_index}/{replayStatus.total_ticks}">REPLAY</span>
        {/if}
        {#if paperStatus?.enabled && paperStatus.open_order_count > 0}
          <span class="algo-mode-badge algo-mode-paper"
                title="Paper engine has {paperStatus.open_order_count} open chase order{paperStatus.open_order_count === 1 ? '' : 's'}">PAPER</span>
        {/if}
        {#if shadowStatus?.shadow_active}
          <span class="algo-mode-badge algo-mode-shadow"
                title="Shadow mode — orders logged, not executed">SHADOW</span>
        {/if}
        {#if liveStatus?.live_count > 0}
          <span class="algo-mode-badge algo-mode-live"
                title="{liveStatus.live_count}/{liveStatus.total_flags} actions are LIVE">LIVE</span>
        {/if}
        {#if $authStore.user}
          <span class="algo-user-pill">
            {$authStore.user.display_name?.toLowerCase() ?? ''}
            <span class="algo-user-role">admin</span>
          </span>
          <button onclick={signOut} class="algo-nav-btn">Sign Out</button>
        {:else if isDemo}
          <button onclick={() => goto('/signin')} class="algo-nav-btn">Sign In</button>
        {/if}
        <button onclick={() => goto('/about')} class="algo-pub-link">↙ Investor site</button>
      </div>

      <!-- Mobile -->
      <div class="algo-nav-inner lg:hidden flex items-center justify-between h-12">
        <div class="flex items-center">
          <span class="algo-vert algo-vert-sm" aria-hidden="true">ALGO</span>
          <button onclick={() => goto('/about')} class="algo-brand">
            <img src={bullSrc} alt="" class="algo-brand-bull algo-brand-bull-sm" />
            <span class="algo-brand-name">Rambo Quant</span>
          </button>
        </div>
        {#if isDemo}
          <span class="algo-mode-badge algo-mode-demo" title="Demo mode — paper-only">DEMO</span>
        {/if}
        {#if paperStatus?.branch !== 'main' && simStatus?.active}
          <span class="algo-mode-badge algo-mode-sim" title="Simulator running">SIM</span>
        {/if}
        {#if replayStatus?.active}
          <span class="algo-mode-badge algo-mode-replay" title="Replay running">REPLAY</span>
        {/if}
        {#if paperStatus?.enabled && paperStatus.open_order_count > 0}
          <span class="algo-mode-badge algo-mode-paper" title="Paper engine has open chase orders">PAPER</span>
        {/if}
        {#if shadowStatus?.shadow_active}
          <span class="algo-mode-badge algo-mode-shadow" title="Shadow mode active">SHADOW</span>
        {/if}
        {#if liveStatus?.live_count > 0}
          <span class="algo-mode-badge algo-mode-live" title="Live actions enabled">LIVE</span>
        {/if}
        {#if $authStore.user}
          <span class="algo-user-pill">
            {$authStore.user.display_name?.toLowerCase() ?? ''}
            <span class="algo-user-role">admin</span>
          </span>
        {/if}
        <button
          onclick={() => menuOpen = !menuOpen}
          class="algo-hamburger"
          aria-label="Toggle menu"
          aria-expanded={menuOpen}
        >
          {#if menuOpen}
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
            </svg>
          {:else}
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/>
            </svg>
          {/if}
        </button>
      </div>

      <!-- Mobile dropdown -->
      {#if menuOpen}
        <nav class="algo-mobile-dropdown">
          {#each algoLinks as link}
            <button
              onclick={() => { goto(link.href); closeMenu(); }}
              class="algo-mobile-item {isActive(link.href) ? 'algo-mobile-active' : ''}"
            >{link.label}</button>
          {/each}
          <button onclick={() => { goto('/about'); closeMenu(); }} class="algo-mobile-item algo-mobile-site">↙ Investor site</button>
          <!-- Sign Out only when authenticated. Anonymous demo
               sessions get a Sign In affordance instead, mirroring
               the desktop nav. -->
          {#if $authStore.user}
            <button onclick={() => { signOut(); closeMenu(); }} class="algo-mobile-item">Sign Out</button>
          {:else}
            <button onclick={() => { goto('/signin'); closeMenu(); }} class="algo-mobile-item">Sign In</button>
          {/if}
        </nav>
      {/if}
    </header>

    {#if simStatus?.active}
      <div class="sim-banner" role="status" aria-live="polite">
        <span class="sim-banner-dot"></span>
        <span class="sim-banner-label">SIMULATOR</span>
        <span class="sim-banner-sep">·</span>
        <span><b class="sim-banner-scenario">{simStatus.scenario || '—'}</b></span>
        {#if simStatus.seed_mode}
          <span class="sim-banner-sep">·</span>
          <span>seed {simStatus.seed_mode}</span>
        {/if}
        <span class="sim-banner-sep">·</span>
        <span>tick {simStatus.tick_index}/{simStatus.total_ticks}</span>
        {#if simStatus.only_agent_ids?.length}
          <span class="sim-banner-sep">·</span>
          <span>agents=[{simStatus.only_agent_ids.join(',')}]</span>
        {/if}
      </div>
    {/if}
    {#if paperStatus?.enabled && paperStatus.open_order_count > 0}
      <!-- Paper-trade banner — fires when the prod paper engine has any
           in-flight chase. Faded sky-blue tint to differentiate from
           the red SIMULATOR banner; both can stack when both are live. -->
      <div class="paper-banner" role="status" aria-live="polite">
        <span class="paper-banner-dot"></span>
        <span class="paper-banner-label">PAPER</span>
        <span class="paper-banner-sep">·</span>
        <span>{paperStatus.open_order_count} open chase order{paperStatus.open_order_count === 1 ? '' : 's'}</span>
        <span class="paper-banner-sep">·</span>
        <span class="paper-banner-meta">fake fills against live quotes</span>
      </div>
    {/if}

    {#if replayStatus?.active}
      <div class="replay-banner" role="status" aria-live="polite">
        <span class="replay-banner-dot"></span>
        <span class="replay-banner-label">REPLAY</span>
        <span class="replay-banner-sep">·</span>
        <span>tick {replayStatus.tick_index}/{replayStatus.total_ticks}</span>
        {#if replayStatus.date_from && replayStatus.date_to}
          <span class="replay-banner-sep">·</span>
          <span>{replayStatus.date_from} → {replayStatus.date_to}</span>
        {/if}
        {#if replayStatus.interval}
          <span class="replay-banner-sep">·</span>
          <span>{replayStatus.interval}</span>
        {/if}
      </div>
    {/if}

    <!-- Glanceable position / holdings strip — pinned just under
         the navbar (and below SIM / PAPER banners when those are
         active) so it reads as part of the chrome. Self-fetches
         from /api/positions + /api/holdings on mount; auto-hides
         when the operator has no positions. -->
    <PositionStrip />

    <main class="algo-content">
      {@render children()}
    </main>

    <footer class="algo-footer">
      <span class="algo-footer-text">RamboQuant Analytics</span>
      <span class="algo-footer-sep">·</span>
      <span class="algo-footer-text">ACU-5195</span>
      <span class="algo-footer-sep">·</span>
      <span class="algo-footer-text">{pageLabel}</span>
    </footer>
  </div>
</div>

<style>
  /* ── Algo viewport ─────────────────────────────────────────────────────── */
  .algo-viewport {
    min-height: 100vh;
    background-color: #080f1c;
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  /* No max-width: let the card consume the whole viewport so the
     grids / log panel / simulator don't leave dead gutters on wide
     monitors. Borders dropped because there's nothing to separate
     from anymore. */
  .algo-card {
    width: 100%;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    background-color: #0d1829;
  }

  /* ── Navbar ─────────────────────────────────────────────────────────────── */
  .algo-navbar {
    position: sticky;
    top: 0;
    z-index: 50;
    background: #0a1020;
    border-bottom: 1px solid #d97706;
    overflow: visible;
  }

  .algo-nav-inner {
    width: 100%;
    margin: 0 auto;
    padding: 0 0.5rem;
  }

  /* Brand mark — vertical-centered by both the parent flex's
     items-center AND the button's own align-items: center. Symmetric
     padding + line-height matched to the bull's height keeps the text
     baseline aligned to the image centre. */
  .algo-brand {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
    margin-right: 0.75rem;
    outline: none !important;
    height: 100%;
  }
  .algo-brand-name {
    font-size: 0.72rem;
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: 0.08em;
    font-family: ui-monospace, monospace;
    /* Match the bull's box height so the text's line-box and the
       image's box have the same height — items-center then lands on
       the same visual centre for both. */
    line-height: 1.3rem;
    display: flex;
    align-items: center;
  }
  /* Bull logo with an amber glow so it reads as "lit" against the
     dark navbar without needing a surrounding badge. */
  .algo-brand-bull {
    height: 1.3rem;
    width: auto;
    display: block;
    filter: drop-shadow(0 0 3px rgba(251,191,36,0.75))
            drop-shadow(0 0 6px rgba(251,191,36,0.45));
  }
  .algo-brand-bull-sm { height: 1.05rem; }

  /* Vertical ALGO text on the far left of the navbar. Bare — no chip,
     no background, no border. Negative left margin eats the 0.5rem
     container padding so the text sits flush against the viewport
     edge. writing-mode + rotate(180deg) mirrors the LogPanel "log"
     stamp so the two site-wide vertical labels read the same way. */
  .algo-vert {
    writing-mode: vertical-lr;
    transform: rotate(180deg);
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.55rem;
    font-weight: 500;
    letter-spacing: 0.18em;
    /* Faint amber — reads as a quiet section stamp against the navbar,
       not a label competing with the brand. */
    color: rgba(251,191,36,0.4);
    text-transform: uppercase;
    line-height: 1;
    /* Container padding is 0.5rem (8px); -3px leaves a 5px gap between
       the text and the viewport's left edge. */
    margin-left: -3px;
    margin-right: 0.35rem;
    padding: 0;
    background: none;
    border: 0;
    align-self: stretch;
    display: inline-flex;
    align-items: center;
  }
  .algo-vert-sm {
    font-size: 0.48rem;
    letter-spacing: 0.14em;
    margin-right: 0.3rem;
  }


  /* Group separator — thin vertical bar between nav groups.
     Dim sky-blue at low opacity so it reads as a structural divider
     without drawing the eye away from the active item. */
  .algo-nav-sep {
    display: inline-block;
    width: 1px;
    height: 1rem;
    background: rgba(125,211,252,0.18);
    margin: 0 0.3rem;
    flex-shrink: 0;
    align-self: center;
  }

  /* Nav buttons */
  :global(.algo-nav-btn) {
    padding: 0.22rem 0.6rem 0.22rem calc(0.6rem - 2px);
    font-size: 0.68rem;
    font-weight: 500;
    border-radius: 0.2rem;
    background: transparent;
    color: rgba(180, 200, 230, 0.75);
    border: none;
    border-left: 2px solid transparent;
    cursor: pointer;
    letter-spacing: 0.03em;
    font-family: ui-monospace, monospace;
    transition: background-color 0.06s, color 0.06s, border-left-color 0.06s;
    white-space: nowrap;
    outline: none !important;
    -webkit-tap-highlight-color: transparent;
  }
  :global(.algo-nav-btn:hover) {
    background: rgba(251,191,36,0.1);
    color: #fbbf24;
    border-left-color: #fbbf24;
  }
  :global(.algo-nav-btn-active) {
    background: rgba(251,191,36,0.15);
    color: #fbbf24;
    font-weight: 700;
    border-left-color: #fbbf24;
  }

  /* Back-to-investor-site link — amber-pill emphasis on dark, mirroring
     the gold-pill "Algo Site" button on the public side. Both context-
     switch buttons carry equal visual weight; the operator never has
     to hunt for the way back. */
  /* Mode badges — env-aware short word labels (DEMO / PAPER / SIM).
     Sit in the navbar so they stay visible regardless of scroll;
     the existing full-width banners under the nav still surface
     scenario / chase detail. The badges are the at-a-glance "are
     we in fake-money land right now?" indicator on every algo
     page. Pill-shaped (rounded ends) instead of circles since
     the labels are 3-5 characters. */
  /* Outlined-pill style — subtle tinted bg with bright text + matching
     dot accent. Dot pulses (not the whole pill) so a recruiter glance
     catches the indicator without the loud full-badge throb the
     earlier solid-fill version had. Same shape + sizing across all
     three modes; only the colour changes. */
  .algo-mode-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    height: 1.4rem;
    padding: 0 0.6rem;
    border-radius: 9999px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    line-height: 1;
    flex: 0 0 auto;
    margin-right: 0.35rem;
    border: 1px solid currentColor;
  }
  .algo-mode-badge::before {
    content: '';
    width: 0.4rem;
    height: 0.4rem;
    border-radius: 9999px;
    background: currentColor;
    box-shadow: 0 0 6px currentColor;
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .algo-mode-demo   { color: #c084fc; background: rgba(192,132,252,0.10); }
  .algo-mode-sim    { color: #fb7185; background: rgba(251,113,133,0.10); }
  .algo-mode-replay { color: #4ade80; background: rgba(74,222,128,0.10); }
  .algo-mode-paper  { color: #38bdf8; background: rgba(56,189,248,0.10); }
  .algo-mode-shadow { color: #fb923c; background: rgba(251,146,60,0.10); }
  .algo-mode-live   { color: #ef4444; background: rgba(239,68,68,0.10); }
  @keyframes algo-mode-dot {
    0%, 100% { opacity: 1;   transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(0.8); }
  }

  .algo-pub-link {
    padding: 0.2rem 0.65rem;
    font-size: 0.65rem;
    font-weight: 500;
    border-radius: 0.25rem;
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.32);
    cursor: pointer;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.02em;
    transition: color 0.08s, border-color 0.08s, background-color 0.08s;
    outline: none !important;
    margin-left: 0.5rem;
    white-space: nowrap;
  }
  .algo-pub-link:hover {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.5);
    color: #fbbf24;
  }

  /* User pill */
  .algo-user-pill {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.65rem;
    font-weight: 500;
    color: #c8d8f0;
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(255,255,255,0.1);
    margin-right: 0.25rem;
    white-space: nowrap;
    font-family: ui-monospace, monospace;
  }
  .algo-user-role {
    font-size: 0.5rem;
    color: #fbbf24;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* Hamburger */
  .algo-hamburger {
    padding: 0.3rem;
    border-radius: 0.2rem;
    background: transparent;
    color: rgba(180,200,230,0.8);
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    transition: background-color 0.06s;
    outline: none !important;
  }
  .algo-hamburger:hover { background: rgba(251,191,36,0.12); }

  /* Mobile dropdown */
  .algo-mobile-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    z-index: 49;
    background: #0a1020;
    border-top: 1px solid rgba(251,191,36,0.2);
    border-bottom: 1px solid rgba(251,191,36,0.2);
    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
  }
  .algo-mobile-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.65rem 1.25rem;
    font-size: 0.85rem;
    font-weight: 500;
    color: rgba(180,200,230,0.8);
    background: transparent;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    cursor: pointer;
    font-family: ui-monospace, monospace;
    transition: background-color 0.05s;
    outline: none !important;
  }
  .algo-mobile-item:last-child { border-bottom: none; }
  .algo-mobile-item:hover { background: rgba(251,191,36,0.1); color: #fbbf24; }
  .algo-mobile-active { color: #fbbf24; background: rgba(251,191,36,0.1); }
  /* Investor-site cross-link inside the mobile menu — amber-pill
     emphasis matching the desktop button. Separated from the regular
     mobile items by a thin top border so it reads as a context-switch
     row, not another tab. Symmetric with the public-side mobile
     menu's gold-pill algo-site link. */
  .algo-mobile-site {
    color: #fbbf24;
    font-size: 0.75rem;
    font-weight: 500;
    background: rgba(251,191,36,0.10);
    border-top: 1px solid rgba(251,191,36,0.32);
    margin-top: 0.3rem;
    padding-top: 0.55rem;
    letter-spacing: 0.02em;
  }

  /* ── Simulator banner — pinned under the nav when sim is active ──────────
     Opaque background (solid over a tinted overlay) so page content
     scrolling underneath never bleeds through. Compact padding so it
     steals minimum vertical space. */
  .sim-banner {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.2rem 0.85rem;
    background-color: #0a1020;   /* solid under the tint — prevents bleed */
    background-image: linear-gradient(90deg,
                      rgba(251,113,133,0.22) 0%,
                      rgba(251,191,36,0.22) 100%);
    border-top: 1px solid rgba(251,113,133,0.45);
    border-bottom: 1px solid rgba(251,113,133,0.45);
    color: #fecaca;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    position: sticky;
    top: 3rem;                  /* sits just under the algo navbar */
    z-index: 49;
    animation: sim-banner-pulse 2.2s ease-in-out infinite;
  }
  .sim-banner-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: #fb7185;
    box-shadow: 0 0 5px rgba(251,113,133,0.85);
    flex-shrink: 0;
  }
  .sim-banner-label {
    color: #fbbf24;
    letter-spacing: 0.1em;
    font-size: 0.6rem;
    font-weight: 800;
  }
  .sim-banner-sep { color: rgba(251,191,36,0.5); }
  .sim-banner-scenario { color: #fde68a; }
  @keyframes sim-banner-pulse {
    0%, 100% { box-shadow: inset 0 0 0 0 rgba(251,113,133,0);   }
    50%      { box-shadow: inset 0 0 10px 0 rgba(251,113,133,0.30); }
  }

  /* Paper-trade banner — sky-blue palette so it visually distinct from
     the red SIMULATOR banner. Stacks below it when both are active. */
  .paper-banner {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.2rem 0.85rem;
    background-color: #0a1020;
    background-image: linear-gradient(90deg,
                      rgba(56,189,248,0.20) 0%,
                      rgba(125,211,252,0.20) 100%);
    border-top: 1px solid rgba(56,189,248,0.45);
    border-bottom: 1px solid rgba(56,189,248,0.45);
    color: #bae6fd;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    position: sticky;
    top: 3rem;
    z-index: 48;
  }
  .paper-banner-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: #38bdf8;
    box-shadow: 0 0 5px rgba(56,189,248,0.85);
    flex-shrink: 0;
  }
  .paper-banner-label {
    color: #7dd3fc;
    letter-spacing: 0.1em;
    font-size: 0.6rem;
    font-weight: 800;
  }
  .paper-banner-sep  { color: rgba(125,211,252,0.5); }
  .paper-banner-meta { color: #bae6fd; opacity: 0.8; }

  /* ── Replay banner ───────────────────────────────────────────────────── */
  .replay-banner {
    position: sticky;
    top: 3rem;
    z-index: 39;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.75rem;
    font-size: 0.7rem;
    font-weight: 600;
    color: #86efac;
    background: rgba(22,101,52,0.25);
    border-bottom: 1px solid rgba(74,222,128,0.2);
  }
  .replay-banner-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #4ade80;
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .replay-banner-label {
    color: #4ade80;
    letter-spacing: 0.1em;
    font-size: 0.6rem;
    font-weight: 800;
  }
  .replay-banner-sep { color: rgba(74,222,128,0.5); }

  /* ── Content ─────────────────────────────────────────────────────────────── */
  .algo-content {
    flex: 1;
    /* Tighter side padding (0.5rem instead of 1rem) so the grids +
       log panel have more horizontal room on narrow widths.
       Top padding tightened to 0.5rem so the page content sits
       close to the PositionStrip immediately above (was 1rem,
       which left visible empty space below the strip's underline). */
    padding: 0.5rem 0.5rem 1.5rem;
    color: #c8d8f0;
  }

  /* ── Footer ─────────────────────────────────────────────────────────────── */
  .algo-footer {
    background: #0a1020;
    border-top: 1px solid rgba(251,191,36,0.2);
    height: 1.6rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    padding: 0 0.5rem;
  }
  .algo-footer-text { font-size: 0.6rem; color: rgba(160,185,220,0.7); font-family: ui-monospace, monospace; }
  .algo-footer-sep  { font-size: 0.6rem; color: rgba(251,191,36,0.6); margin: 0 0.4rem; }

  /* Visible scrollbars on algo pages. Default browser scrollbars on
     dark themes are so low-contrast they're easy to miss when content
     overflows; colouring the thumb in the site's amber makes "there is
     more below" obvious at a glance. Firefox uses `scrollbar-color`;
     WebKit/Blink use the pseudo-elements. Idle state is faint, hover is
     bright so moving the mouse near the bar lights it up. */
  :global(.algo-viewport),
  :global(.algo-viewport *),
  :global(.algo-content *) {
    scrollbar-color: rgba(251,191,36,0.45) rgba(148,163,184,0.08);
    scrollbar-width: thin;
  }
  :global(.algo-viewport ::-webkit-scrollbar),
  :global(.algo-content ::-webkit-scrollbar) {
    width: 10px;
    height: 10px;
  }
  :global(.algo-viewport ::-webkit-scrollbar-track),
  :global(.algo-content ::-webkit-scrollbar-track) {
    background: rgba(148,163,184,0.06);
  }
  :global(.algo-viewport ::-webkit-scrollbar-thumb),
  :global(.algo-content ::-webkit-scrollbar-thumb) {
    background: rgba(251,191,36,0.45);
    border-radius: 5px;
    border: 2px solid transparent;
    background-clip: padding-box;
  }
  :global(.algo-viewport ::-webkit-scrollbar-thumb:hover),
  :global(.algo-content ::-webkit-scrollbar-thumb:hover) {
    background: #fbbf24;
    background-clip: padding-box;
  }

  /* Page-top header row — H1 on the left, timestamp right-aligned on
     the same line to conserve vertical space. Wraps to its own line on
     narrow widths. Used by every admin page via `.page-header`. */
  :global(.page-header) {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.4rem;
  }
  /* Every algo page gets the full-width amber underline that dashboard
     already had — keeps the headline visually separated from the
     content cards below without crowding the title chip itself.
     Tighter spacing below the underline so the page content sits
     close to the title (was 0.35rem padding + 1rem margin = ~22px
     of empty zone). */
  :global(.algo-content .page-header) {
    border-bottom: 1px solid rgba(251,191,36,0.25);
    padding-bottom: 0.2rem;
    margin-bottom: 0.45rem;
  }

  /* Page-level timestamp (cyan, matches log timestamps). Works inline
     inside .page-header OR stand-alone when a page renders it on its
     own row (left over on public pages). */
  :global(.algo-ts) {
    font-size: 0.62rem;
    color: #fde047;
    font-family: ui-monospace, monospace;
    white-space: nowrap;
  }

  /* Algo dark-theme overrides for classes shared with public pages */
  :global(.algo-content .text-muted) { color: #7e97b8; }
  :global(.algo-content .field-label) { color: #7e97b8; }
  :global(.algo-content .field-input) {
    background: #152033;
    border-color: rgba(255,255,255,0.12);
    color: #e2e8f0;
    color-scheme: dark;
    accent-color: #fbbf24;
  }
  :global(.algo-content .field-input:focus) { border-color: #fbbf24 !important; }

  /* .field-input sets border-color shorthand which overwrites the amber
     left rule shipped with .cmd-input — restore it inside algo pages so
     the Terminal textarea and any other cmd-input surface keep the
     Bloomberg-style amber accent that the Orders command bar uses. */
  :global(.algo-content .cmd-input) {
    border-left: 3px solid #fbbf24 !important;
  }
  :global(.algo-content .cmd-input:focus) {
    border-left-color: #fbbf24 !important;
  }

  /* .cmd-surface: mirrors the Orders command-bar amber left accent onto
     any entry/form card so the user knows at a glance "this is where
     input goes". Applied to the Simulator's control card; can be added
     to future form surfaces as-is. */
  :global(.algo-content .cmd-surface) {
    border-left: 3px solid #fbbf24 !important;
  }

  /* .sim-btn-order: compact modifier for places where the sim-btn
     palette is reused outside the Simulator's grow-to-fill row. Drops
     the flex grow/basis so the button sizes to its content (e.g. the
     Submit/BUY/SELL/Clear cluster on the Orders page). */
  :global(.sim-btn.sim-btn-order) {
    flex: 0 0 auto;
    max-width: none;
    padding: 0.3rem 0.9rem;
  }

  /* <select> element + dropdown popup — matches the OrderPopup modal's
     palette (linear-gradient(#273552 → #1d2a44) + amber accent border).
     Native browsers render the option list as OS chrome, but setting
     background-color + color on <option> is honoured by Chromium + FF,
     so the dropdown reads as a continuation of the OrderPopup instead
     of a foreign OS widget. */
  :global(.algo-content select.field-input) {
    background-image: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    background-color: #1d2a44;
    border-color: rgba(251,191,36,0.25);
  }
  :global(.algo-content select.field-input:hover) {
    border-color: rgba(251,191,36,0.5);
  }
  :global(.algo-content select.field-input option),
  :global(.algo-content select option) {
    background-color: #1d2a44;
    color: #e2e8f0;
    padding: 0.35rem 0.5rem;
  }
  :global(.algo-content select.field-input option:checked),
  :global(.algo-content select option:checked) {
    background-color: #273552;
    color: #fbbf24;
    font-weight: 700;
  }
  :global(.algo-content select.field-input option:hover),
  :global(.algo-content select option:hover) {
    background-color: rgba(251,191,36,0.15);
    color: #fbbf24;
  }
  :global(.algo-content .section-heading) { color: #fbbf24; }
  :global(.algo-content .page-title-chip) {
    color: #fbbf24;
    border-bottom: none;
    padding-bottom: 0;
  }
  :global(.algo-content .btn-secondary) {
    color: #c8d8f0;
    border-color: rgba(255,255,255,0.2);
    background: transparent;
  }
  :global(.algo-content .btn-secondary:hover:not(:disabled)) {
    background: rgba(251,191,36,0.1);
    border-color: rgba(251,191,36,0.5);
    color: #fbbf24;
  }
  :global(.algo-content .btn-tertiary) { color: #c8d8f0; }
  :global(.algo-content .btn-tertiary:hover) { background: rgba(251,191,36,0.1); color: #fbbf24; }
  :global(.algo-content .btn-tertiary.active) { color: #fbbf24; background: rgba(251,191,36,0.15); }

  /* ── Status-driven surface card — used across algo pages ─────────────────── */
  :global(.algo-status-card) {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    padding: 0.75rem;
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.08);
    color: #c8d8f0;
    transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  }
  :global(.algo-status-card[data-status="active"]) {
    border-color: rgba(74,222,128,0.6);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(74,222,128,0.18);
  }
  :global(.algo-status-card[data-status="inactive"]) {
    border-color: rgba(180,200,230,0.18);
    opacity: 0.82;
  }
  :global(.algo-status-card[data-status="triggered"]) {
    border-color: rgba(248,113,113,0.75);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(248,113,113,0.22);
  }
  :global(.algo-status-card[data-status="running"]) {
    border-color: rgba(251,191,36,0.65);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(251,191,36,0.18);
  }
  :global(.algo-status-card[data-status="cooldown"]) {
    border-color: rgba(251,191,36,0.4);
  }
  :global(.algo-status-card[data-status="error"]) {
    border-color: rgba(220,38,38,0.85);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(220,38,38,0.28);
  }
</style>
