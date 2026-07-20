<script>
  import { goto, onNavigate, afterNavigate, preloadCode } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore } from '$lib/stores';
  import ImpersonationBanner from '$lib/ImpersonationBanner.svelte';
  import NavigationIndicator from '$lib/NavigationIndicator.svelte';

  const { children } = $props();

  function isActive(/** @type {string} */ href) {
    return page.url.pathname.startsWith(href);
  }

  function signOut() {
    authStore.logout();
    goto('/about');
  }

  const baseLinks = [
    { href: '/about',       label: 'About'       },
    { href: '/market',      label: 'Market'      },
    { href: '/performance', label: 'Performance' },
    { href: '/faq',         label: 'FAQ'         },
    { href: '/contact',     label: 'Contact'     },
  ];

  let menuOpen = $state(false);
  const closeMenu = () => { menuOpen = false; };

  const bullSrc = "/bull.webp";

  // ── Navigation loading indicator ───────────────────────────────────
  /** @type {NavigationIndicator | null} */
  let _navIndicator = $state(null);

  onNavigate(() => {
    _navIndicator?.start();
  });

  afterNavigate(() => {
    _navIndicator?.complete();
  });

  /**
   * Preload the JS bundle for `href` on hover — gives near-instant
   * transitions on fast connections by fetching the route chunk before
   * the operator commits to a click.
   * @param {string} href
   */
  function _preloadHover(href) {
    preloadCode(href).catch(() => {});
  }
</script>

<svelte:head>
  <!-- Per-page canonical: every public route gets its own URL so
       Google doesn't collapse all pages back to /. Individual pages
       override title, description, og:*, and twitter:* via their
       own <svelte:head> blocks. -->
  <link rel="canonical" href="https://ramboq.com{page.url.pathname}" />
</svelte:head>

<!-- Navigation loading indicator — champagne-gold top-bar progress strip
     for the public (cream) layout. Same UX contract as the algo layout's
     cyan variant. bind:this gives onNavigate / afterNavigate a direct
     reference. -->
<NavigationIndicator bind:this={_navIndicator} variant="pub" />

<div class="pub-viewport card-theme-cream">
  <div class="pub-accent-top"></div>

  <div class="pub-card">
    <!-- Desktop navbar -->
    <header class="pub-navbar">
      <div class="pub-nav-inner hidden md:flex items-center gap-1 h-14">
        <a href="/" class="pub-brand shrink-0 mr-5">
          <img src={bullSrc} alt="" width="42" height="38" loading="eager" style="height:2.6rem;width:auto;display:block;flex-shrink:0;pointer-events:none;filter:drop-shadow(0 0 3px rgba(200,168,75,0.75)) drop-shadow(0 0 6px rgba(200,168,75,0.45));" />
          <div class="pub-brand-text">
            <span class="pub-brand-name">RAMBO QUANT</span>
            <span class="pub-brand-sub">ANALYTICS LLP</span>
            <div class="pub-brand-sep"></div>
            <span class="pub-brand-tagline">INVEST · GROW · COMPOUND</span>
          </div>
        </a>

        <nav class="flex items-center gap-3 flex-1 justify-center" aria-label="Main navigation">
          {#each baseLinks as link}
            <a
              href={link.href}
              onmouseenter={() => _preloadHover(link.href)}
              class="pub-nav-btn {isActive(link.href) ? 'pub-nav-btn-active' : ''}"
            >{link.label}</a>
          {/each}
        </nav>

        <!-- Rambo Terminal cross-link visible to everyone. Lands on
             /pulse (Pulse) — the most useful entry surface
             regardless of role: live positions / holdings / pinned
             market data. The previous target /dashboard is more
             admin-flavoured (P&L analysis + agent fires); operators
             reach it via the algo navbar from /pulse if they want it.
              - admin → /pulse with full access
              - anonymous on prod → demo mode (real broker data, masked
                accounts, paper-only writes)
              - anonymous on dev → /signin via algo layout's auth guard. -->
        <a href="/pulse" class="pub-nav-algo-btn">
          Rambo Terminal ↗
        </a>

        {#if $authStore.user}
          <span class="pub-user-pill">
            {$authStore.user.username}
            {#if $authStore.user.role === 'designated'}
              <span class="pub-user-role pub-user-role-designated">designated</span>
            {:else if $authStore.user.role === 'admin'}
              <span class="pub-user-role">admin</span>
            {/if}
          </span>
          <button onclick={signOut} class="pub-nav-btn">Sign Out</button>
        {:else}
          <button onclick={() => goto('/signin')} class="pub-nav-signin {isActive('/signin') ? 'pub-nav-btn-active' : ''}">Sign In</button>
        {/if}
      </div>

      <!-- Mobile bar -->
      <div class="pub-nav-inner md:hidden flex items-center justify-between h-16 py-2">
        <a href="/" class="pub-brand pub-brand-mobile">
          <img src={bullSrc} alt="" width="42" height="38" loading="eager" style="height:2.2rem;width:auto;display:block;flex-shrink:0;pointer-events:none;filter:drop-shadow(0 0 3px rgba(200,168,75,0.75)) drop-shadow(0 0 6px rgba(200,168,75,0.45));" />
          <div class="pub-brand-text">
            <span class="pub-brand-name">RAMBO QUANT</span>
            <span class="pub-brand-sub">ANALYTICS LLP</span>
            <div class="pub-brand-sep"></div>
            <span class="pub-brand-tagline">INVEST · GROW · COMPOUND</span>
          </div>
        </a>
        <div class="flex items-center gap-2">
          {#if $authStore.user}
            <span class="pub-user-pill text-[0.6rem]">
              {$authStore.user.username}
              {#if $authStore.user.role === 'designated'}
                <span class="pub-user-role pub-user-role-designated">designated</span>
              {:else if $authStore.user.role === 'admin'}
                <span class="pub-user-role">admin</span>
              {/if}
            </span>
          {/if}
          <button
            onclick={() => menuOpen = !menuOpen}
            class="pub-hamburger"
            aria-label="Toggle menu"
            aria-expanded={menuOpen}
          >
            {#if menuOpen}
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"/>
              </svg>
            {:else}
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 6h16M4 12h16M4 18h16"/>
              </svg>
            {/if}
          </button>
        </div>
      </div>

      <!-- Mobile dropdown -->
      {#if menuOpen}
        <nav class="pub-mobile-dropdown" aria-label="Mobile navigation">
          {#each baseLinks as link}
            <a
              href={link.href}
              onclick={closeMenu}
              onmouseenter={() => _preloadHover(link.href)}
              class="pub-mobile-item {isActive(link.href) ? 'pub-mobile-active' : ''}"
            >{link.label}</a>
          {/each}
          <a
            href="/pulse"
            onclick={closeMenu}
            class="pub-mobile-item pub-mobile-algo"
          >Rambo Terminal ↗</a>
          {#if $authStore.user}
            <button onclick={() => { signOut(); closeMenu(); }} class="pub-mobile-item">Sign Out</button>
          {:else}
            <button onclick={() => { goto('/signin'); closeMenu(); }} class="pub-mobile-item">Sign In</button>
          {/if}
        </nav>
      {/if}
    </header>

    <ImpersonationBanner />

    <main class="pub-content">
      {@render children()}
    </main>

    <footer class="pub-footer">
      <p class="hidden md:block text-center leading-none pub-footer-text">
        © RamboQuant Analytics LLP
        <span class="pub-sep">|</span>
        ACU-5195
        <span class="pub-sep">|</span>
        Disclaimer: Investment in markets is subject to risk. Past performance is not indicative of future results.
        <span class="pub-sep">|</span>
        Built by <a class="pub-footer-link" href="https://ramanaambore.me" target="_blank" rel="noopener">Ramana R. Ambore</a>
      </p>
      <p class="md:hidden text-center leading-none pub-footer-text">
        © RamboQuant Analytics LLP
        <span class="pub-sep">|</span>
        ACU-5195
        <span class="pub-sep">|</span>
        Built by <a class="pub-footer-link" href="https://ramanaambore.me" target="_blank" rel="noopener">Ramana R. Ambore</a>
      </p>
    </footer>
  </div>

  <div class="pub-accent-bottom"></div>
</div>

<style>
  /*
   * ── Investor palette: Deep Navy + Champagne Gold ───────────────────────────
   *   Navy primary:    #0c1830   navbar, footer, grid headers
   *   Champagne gold:  #c8a84b   accents, borders, active states
   *   Gold bright:     #e8c86a   text on dark, brand name
   *   Page bg:         #f0ece3   warm cream — premium feel
   *   Card bg:         #faf7f0   warm white
   *   Body text:       #1a1e35   near-black navy
   */

  /* ── Viewport / card shell ─────────────────────────────────────────────── */
  .pub-viewport {
    min-height: 100vh;
    min-height: 100dvh;   /* dvh follows actual visible area on mobile rotation */
    /* Body cream — matches the public site palette so the side
       gutters don't read as a cool-grey frame around a champagne card.
       The diagonal stripe overlay (rgba below) is now a subtle warm
       hatch instead of a cool-on-cool wash. */
    background-color: #f0ece3;
    background-image: repeating-linear-gradient(
      135deg,
      transparent,
      transparent 40px,
      rgba(154,126,56,0.05) 40px,
      rgba(154,126,56,0.05) 41px
    );
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  .pub-accent-top, .pub-accent-bottom {
    /* Strip spans the full viewport width and is `position: fixed` so
       the entire top/bottom 4 px region (gradient + side panels) sits
       as one immovable banner — never scrolls with content.
       The visible champagne gradient is still bounded to the 1280 px
       card footprint (matching .pub-footer's gold hairline contract).
       Outside 1280 px the strip is filled with the body's cream so
       the side panels match the .pub-viewport background — but, being
       part of the fixed strip, they're locked relative to the viewport
       instead of scrolling with the diagonal-hatch body bg behind. */
    position: fixed;
    height: 4px;
    z-index: 200;
    width: 100%;
    left: 0;
    right: 0;
    background-color: #f0ece3;
    background-image: linear-gradient(
      90deg,
      #0c1830 0%, #c8a84b 30%, #f0d878 50%, #c8a84b 70%, #0c1830 100%
    );
    background-size: min(1280px, 100%) 100%;
    background-position: center;
    background-repeat: no-repeat;
  }
  .pub-accent-top    { top: 0; }
  .pub-accent-bottom { bottom: 0; }
  @media (max-width: 767px) {
    .pub-accent-top { height: 5px; }
  }

  .pub-card {
    width: 100%;
    max-width: 1280px;
    min-height: 100vh;
    min-height: 100dvh;   /* dvh fixes mobile-rotation whitespace at bottom */
    display: flex;
    flex-direction: column;
    background-color: #fffdf8;
    border-left:  none;
    border-right: none;
    box-shadow: -4px 0 14px rgba(0,0,0,0.22), 4px 0 14px rgba(0,0,0,0.22);
    margin-top: 4px;
    margin-bottom: 4px;
    position: relative;
  }


  /* ── Navbar ─────────────────────────────────────────────────────────────── */
  .pub-navbar {
    position: sticky;
    top: 4px;
    z-index: 50;
    background-color: #0c1830;
    background-image:
      linear-gradient(rgba(8,14,30,0.78), rgba(8,14,30,0.78)),
      url('/nav_image.webp');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    border-bottom: 2px solid #c8a84b;
    overflow: visible;
  }

  .pub-nav-inner {
    max-width: 1280px;
    margin: 0 auto;
    padding: 0 1rem;
  }

  /* ── Brand text logo ────────────────────────────────────────────────────── */
  .pub-brand {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 0;
    text-decoration: none;
    line-height: 1;
    margin-right: 1rem;
  }
  .pub-brand-text {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0;
    padding: 0 0 0 0.35rem;
    justify-content: center;
    margin-top: 0.1rem;
  }
  .pub-brand-name {
    font-size: 0.78rem;
    font-weight: 900;
    color: #f0d070;
    letter-spacing: 0.12em;
    font-family: 'Trebuchet MS', 'Arial Narrow', Arial, sans-serif;
    line-height: 1.1;
    -webkit-text-stroke: 0.8px rgba(200,140,20,0.9);
    margin-bottom: 0.1rem;
  }
  .pub-brand-sub {
    font-size: 0.58rem;
    font-weight: 700;
    color: #f0d070;
    letter-spacing: 0.1em;
    font-family: 'Trebuchet MS', Arial, sans-serif;
    text-transform: uppercase;
    line-height: 1.1;
    margin-bottom: 0;
    padding-bottom: 0;
    -webkit-text-stroke: 0.7px rgba(200,140,20,0.9);
  }
  .pub-brand-sep {
    align-self: stretch;
    border-top: 1px solid rgba(200,168,75,0.55);
    margin: 0.12rem 0 0.08rem;
  }
  .pub-brand-tagline {
    font-size: 0.4rem;
    font-weight: 500;
    color: rgba(255,255,255,0.82);
    letter-spacing: 0.02em;
    display: block;
    padding-top: 0;
    margin-top: 0.12rem;
  }
  .pub-brand-mobile .pub-brand-name    { font-size: 0.66rem; }
  .pub-brand-mobile .pub-brand-sub     { font-size: 0.5rem; }
  .pub-brand-mobile .pub-brand-tagline { font-size: 0.4rem; }

  /* Nav buttons — laptop / desktop. About / Market / Performance / FAQ /
     Contact get the prominent treatment; the right-side context-switch
     pills (Platform Demo, Sign In) stay tighter. Padding kept minimal
     so the hover/active background pill hugs the text. Click target
     stays comfortable because the navbar row itself is h-14 — every
     button is vertically centred in a 3.5rem strip regardless of its
     own intrinsic height. */
  :global(.pub-nav-btn) {
    padding: 0.18rem 0.45rem 0.04rem;
    /* Slightly tighter than the previous 0.95 rem so the navbar
       reads a touch lighter alongside the brand wordmark. */
    font-size: 0.88rem;
    font-weight: 500;
    border-radius: 0.25rem;
    background: transparent;
    color: rgba(215, 228, 255, 0.82);
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    letter-spacing: 0.02em;
    transition: background-color 0.08s, color 0.08s, border-bottom-color 0.08s;
    white-space: nowrap;
    outline: none;
    -webkit-tap-highlight-color: transparent;
    text-shadow: 0 1px 3px rgba(0,0,0,0.55);
    text-decoration: none;
    display: inline-flex;
    align-items: center;
  }
  :global(.pub-nav-btn:focus-visible) {
    outline: 2px solid #c8a84b;
    outline-offset: 2px;
  }
  :global(.pub-nav-btn:hover) {
    background: rgba(255,255,255,0.09);
    color: #fff;
    border-bottom-color: #c8a84b;
  }
  :global(.pub-nav-btn-active) {
    background: rgba(200,168,75,0.25);
    color: #f0d070;
    font-weight: 600;
    border-bottom-color: #c8a84b;
  }

  /* Algo Site cross-link — gold-pill emphasis. Operators landing on
     the public site rarely jump to the algo dashboard, but when they
     do, the destination is the heavy-machinery surface; the pill
     visually flags it as a "specialised" route. Mirrors the dark-side
     amber-pill "Investor site" link so both context-switch buttons
     read with equal visual weight. */
  .pub-nav-algo-btn {
    padding: 0.18rem 0.55rem;
    font-size: 0.88rem;
    font-weight: 500;
    border-radius: 0.25rem;
    background: rgba(200,168,75,0.10);
    color: #b27908;
    border: 1px solid rgba(200,168,75,0.32);
    cursor: pointer;
    letter-spacing: 0.02em;
    transition: background-color 0.08s, border-color 0.08s, color 0.08s;
    outline: none !important;
    white-space: nowrap;
    margin-right: 0.25rem;
  }
  .pub-nav-algo-btn:hover {
    background: rgba(200,168,75,0.20);
    border-color: rgba(200,168,75,0.5);
    color: #b27908;
  }

  /* Sign-in button */
  .pub-nav-signin {
    padding: 0.18rem 0.6rem;
    font-size: 0.88rem;
    font-weight: 700;
    border-radius: 0.25rem;
    background: rgba(200,168,75,0.22);
    color: #f0d070;
    border: 1px solid rgba(200,168,75,0.55);
    cursor: pointer;
    transition: background-color 0.08s;
    outline: none !important;
    white-space: nowrap;
    text-shadow: 0 1px 2px rgba(0,0,0,0.4);
    letter-spacing: 0.03em;
  }
  .pub-nav-signin:hover { background: rgba(200,168,75,0.4); color: #fff; }

  /* User pill */
  .pub-user-pill {
    font-size: 0.8rem;
    font-weight: 500;
    color: rgba(210, 225, 255, 0.72);
    padding: 0.22rem 0.65rem;
    border-radius: 999px;
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.12);
    margin-right: 0.2rem;
    white-space: nowrap;
  }
  .pub-user-role {
    font-size: 0.58rem;
    color: #f0d070;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-left: 0.3rem;
  }
  /* Designated tier — violet, matches the DESIGNATED badge in /admin. */
  .pub-user-role.pub-user-role-designated { color: #c4b5fd; }

  /* Hamburger */
  .pub-hamburger {
    padding: 0.35rem;
    border-radius: 0.25rem;
    background: transparent;
    color: rgba(215,228,255,0.88);
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background-color 0.08s;
    outline: none !important;
    min-width: 2.75rem;
    min-height: 2.75rem;
  }
  .pub-hamburger:hover { background: rgba(255,255,255,0.10); }

  /* Mobile dropdown */
  .pub-mobile-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    z-index: 49;
    background: linear-gradient(160deg, #1a2e4a 0%, #0f1f38 50%, #0c1830 100%);
    border-top: 2px solid rgba(200,168,75,0.55);
    border-bottom: 1px solid rgba(200,168,75,0.2);
    box-shadow: 0 12px 32px rgba(0,0,0,0.55), inset 0 1px 0 rgba(200,168,75,0.08);
  }
  .pub-mobile-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.8rem 1.4rem;
    /* Matches the desktop nav font-size reduction for visual
       parity between hamburger items and the inline nav links. */
    font-size: 0.85rem;
    font-weight: 500;
    color: rgba(220,232,255,0.8);
    background: transparent;
    border: none;
    border-bottom: 1px solid rgba(126,151,184,0.10);
    cursor: pointer;
    letter-spacing: 0.01em;
    transition: background-color 0.06s, color 0.06s, border-left-color 0.06s;
    border-left: 3px solid transparent;
    outline: none !important;
    text-decoration: none;
  }
  .pub-mobile-item:last-child { border-bottom: none; }
  .pub-mobile-item:hover { background: rgba(200,168,75,0.09); color: #f0d070; border-left-color: rgba(240,208,112,0.5); }
  .pub-mobile-active { color: #f0d070; background: rgba(200,168,75,0.14); border-left-color: #f0d070; font-weight: 600; }
  /* Algo-site cross-link inside the mobile menu — gold-pill emphasis
     matching the desktop button, separated from the regular mobile
     items by a thin top border so it reads as a context-switch row,
     not another tab. Symmetric with the dark-side mobile menu's
     amber-pill investor-site link. */
  .pub-mobile-algo {
    color: #b27908;
    font-weight: 500;
    letter-spacing: 0.02em;
    background: rgba(200,168,75,0.10);
    border-top: 1px solid rgba(200,168,75,0.30);
    margin-top: 0.3rem;
    padding-top: 0.55rem;
  }

  /* ── Content + footer ────────────────────────────────────────────────────── */
  .pub-content {
    flex: 1;
    padding: 1rem 1rem 1.5rem;
  }

  .pub-footer {
    position: sticky;
    bottom: 4px;
    z-index: 40;
    background-color: #0c1830;
    background-image:
      linear-gradient(rgba(8,14,30,0.78), rgba(8,14,30,0.78)),
      url('/nav_image.webp');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    /* Symmetric champagne hairlines top + bottom — mirrors .pub-navbar
       which has a 2px gold border-bottom at the navbar/card seam.
       Without the matching border-bottom, the footer's bottom edge
       fades straight into the accent-bottom strip, leaving the footer
       visually unanchored on its lower side. */
    border-top:    1px solid rgba(200,168,75,0.45);
    border-bottom: 1px solid rgba(200,168,75,0.45);
    min-height: 2rem;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.4rem 0.75rem;
  }
  .pub-footer p { width: 100%; }
  .pub-footer-text { color: rgba(210,225,255,0.75); font-size: 0.7rem; line-height: 1.3; }
  .pub-sep { color: #c8a84b; font-weight: bold; margin: 0 0.35rem; }
  .pub-footer-link {
    color: #c8a84b;
    text-decoration: none;
    border-bottom: 1px dotted rgba(200,168,75,0.45);
  }
  .pub-footer-link:hover { color: #e9c870; border-bottom-color: #e9c870; }

  /* ── CardHeader theming — public light/cream scheme ─────────────────
     Matches .flow-card-header + .flow-card-title from /faq:
     cream bg (#f5f2eb), warm tan border (#ddd8ce), generous padding.
     Title uses navy body text (#1a2744), inherit font stack (not
     monospace), no uppercase, tight 0.01em tracking.
     Scoped to .pub-viewport (the public layout root) so these values
     override the algo :global(body) vars that ship as the base set. */
  .pub-viewport {
    --ch-bg: #f5f2eb;
    --ch-border-bottom: 1px solid #ddd8ce;
    --ch-padding: 0.65rem 1rem;
    --ch-gap: 0.4rem;
    --ch-title-font-family: inherit;
    --ch-title-size: 0.78rem;
    --ch-title-weight: 700;
    --ch-title-color: #1a2744;
    --ch-title-letter-spacing: 0.01em;
    --ch-title-transform: none;
    --ch-ts-size: 0.65rem;
    --ch-ts-color: rgba(26, 39, 68, 0.55);
  }
</style>
