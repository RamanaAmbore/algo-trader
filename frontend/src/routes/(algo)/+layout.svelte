<script>
  import { goto, onNavigate, afterNavigate, preloadCode } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, onDestroy, setContext } from 'svelte';
  import { get } from 'svelte/store';
  import { authStore, visibleInterval, executionMode, connStatus, startConnStatusPoller, startMarketStatusPoller, brokerHealthStore, startBrokerHealthPoller, activityModal, openActivityModal, closeActivityModal, setHibernationIdleMinutes, openOrderTicketModal, chartModal, openChartModal } from '$lib/stores';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import {
    fetchSimStatus, fetchPaperStatus,
    fetchReplayStatus,
    fetchExecutionMode, setExecutionMode,
    fetchOrderEvents,
    fetchPersistenceMode, setPersistenceMode,
    fetchSettings,
  } from '$lib/api';
  import { userRole, hasCap, userCaps } from '$lib/rbac';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import OrderTimelineDrawer from '$lib/order/OrderTimelineDrawer.svelte';
  import { portal } from '$lib/portal';
  import PositionStrip from '$lib/PositionStrip.svelte';
  import ImpersonationBanner from '$lib/ImpersonationBanner.svelte';
  import AgentToast from '$lib/AgentToast.svelte';
  import ToastContainer from '$lib/ToastContainer.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import ShortcutCheatsheet from '$lib/ShortcutCheatsheet.svelte';
  import { bootstrapRBAC } from '$lib/rbac';
  import { startBookChangedBus } from '$lib/data/bookChanged';
  import { startBookPollers, setBookPollerInterval } from '$lib/data/marketDataStores.svelte.js';
  import { loadAccountOrder } from '$lib/data/accountSort.js';
  import { startMarketGatedQuoteStream, stopMarketGatedQuoteStream } from '$lib/data/quoteStream';
  import { tickBus } from '$lib/data/symbolStore.svelte.js';
  import NavigationIndicator from '$lib/NavigationIndicator.svelte';
  import BrokerHealthBadge from '$lib/BrokerHealthBadge.svelte';

  const { children } = $props();


  // ── Navigation loading indicator (user-action precedence) ─────────
  // Show a top-bar progress strip immediately when a navigation starts
  // so the operator sees instant feedback before the destination page
  // finishes loading. This is especially important for heavy routes
  // (/charts, /admin/derivatives) where the first-paint can take 200ms+.
  /** @type {NavigationIndicator | null} */
  let _navIndicator = $state(null);

  // ── Route transition (slice AY, item 23) ──────────────────────────
  // Use the View Transitions API for a ~120ms cross-fade between algo
  // pages instead of the hard content swap. Gracefully degrades on
  // browsers without startViewTransition (Safari < 18, Firefox < 130).
  // Guarded to no-preference only via CSS — the JS hook still fires
  // but the browser respects the CSS animation-duration:0 override when
  // reduce is set, so we don't need a JS-side matchMedia check.
  //
  // Also fires the NavigationIndicator start() so the cyan top-bar
  // appears immediately on click — before any page JS runs — giving
  // the operator instant feedback that the click registered.
  onNavigate((navigation) => {
    // Start the top-bar indicator immediately (user-action feedback).
    _navIndicator?.start();

    if (!document.startViewTransition) return;
    return new Promise((resolve) => {
      document.startViewTransition(async () => {
        resolve();
        await navigation.complete;
      });
    });
  });

  // Complete the indicator once the destination page has settled.
  afterNavigate(() => {
    _navIndicator?.complete();
  });

  /**
   * Preload the JS bundle for `href` on hover.
   * Buttons (vs <a> tags) don't get SvelteKit's built-in
   * data-sveltekit-preload-code attribute support, so we call
   * preloadCode() explicitly. This fetches the route's code chunk
   * before the click lands, making transitions feel instant on fast
   * connections. The call is idempotent — subsequent hovers are no-ops
   * once the chunk is cached.
   * @param {string} href
   */
  function _preloadHover(href) {
    preloadCode(href).catch(() => { /* network miss — non-fatal */ });
  }

  // Demo banner dismiss state — persisted across reloads in localStorage.
  let _demoBannerDismissed = $state(false);

  // ── Keyboard shortcuts (slice AU) ─────────────────────────────────
  // Global keydown listener wired below in onMount. Pauses while the
  // operator is typing in a field (input, textarea, contenteditable)
  // so shortcuts don't fire mid-sentence. Discoverable via `?`.
  let _cheatsheetOpen = $state(false);
  /** Two-key navigation buffer — operator presses `g` then a target
   * letter within 800ms. Mirrors GitHub / Linear muscle memory. */
  let _gPending = false;
  let _gTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);
  function _clearG() {
    _gPending = false;
    if (_gTimer != null) { clearTimeout(_gTimer); _gTimer = null; }
  }
  function _onGlobalKeydown(/** @type {KeyboardEvent} */ e) {
    // Always allow Esc to close the cheat-sheet — even when typing.
    if (e.key === 'Escape' && _cheatsheetOpen) {
      _cheatsheetOpen = false;
      return;
    }
    // Cmd+K / Ctrl+K works even while typing — command palette exception.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      // Phase 3 stub — future Cmd+K command palette.
      e.preventDefault();
      // TODO: open command palette when built; for now open activity modal
      // as the closest "search all actions" surface.
      openActivityModal();
      return;
    }
    // Pause when the operator is typing in a field. document.activeElement
    // returns the focused control; we treat input / textarea / select /
    // contenteditable as "operator wants this key". Esc defocuses.
    const ae = /** @type {HTMLElement|null} */ (document.activeElement);
    if (ae) {
      const tag = ae.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
          || ae.isContentEditable) {
        if (e.key === 'Escape') {
          // Defocus the input so the next key press is not blocked.
          (/** @type {HTMLElement} */ (ae)).blur();
        }
        return;
      }
    }
    // Don't intercept modifier-key combos (Cmd+K handled above; all others
    // pass through to the browser). Plain `O` must not fire on Ctrl+O etc.
    if (e.altKey || e.metaKey || e.ctrlKey) return;

    const k = e.key;

    // `g` opens the two-key buffer; second key triggers the route jump.
    if (_gPending) {
      const target = k.toLowerCase();
      _clearG();
      // Bloomberg-style two-key navigation. `g m` scrolls to the movers
      // section on /pulse via the anchor fragment.
      const route =
        target === 'p' ? '/pulse'
      : target === 'd' ? '/dashboard'
      : target === 'o' ? '/orders'
      : target === 'e' ? '/admin/derivatives'
      : target === 'c' ? '/charts'
      : target === 'v' ? '/performance'
      : target === 'a' ? '/automation'
      : target === 'h' ? '/admin/history'
      : target === 'm' ? '/pulse#movers'
      : null;
      if (route) { e.preventDefault(); goto(route); }
      return;
    }
    if (k === 'g') {
      _gPending = true;
      _gTimer = setTimeout(_clearG, 800);
      return;
    }

    // Single-key shortcuts.
    if (k === '?') { e.preventDefault(); _cheatsheetOpen = true; return; }
    if (k === '/') {
      // Focus the first searchable input on the page. Prefers symbol
      // pickers (.oes-sym-input, .cmd-input) then any text input.
      const sel = '.oes-sym-input, .cmd-input, input[type="search"], input[type="text"]';
      const target = /** @type {HTMLInputElement|null} */ (document.querySelector(sel));
      if (target) { e.preventDefault(); target.focus(); target.select?.(); }
      return;
    }
    if (k.toLowerCase() === 'r') {
      e.preventDefault();
      // Dispatch `refresh-page` event — every mounted RefreshButton hears
      // it and fires its own onClick. Falls back to SvelteKit invalidateAll
      // when no RefreshButton is present on the page.
      const hadButton = document.querySelector('.rf-btn') !== null;
      window.dispatchEvent(new CustomEvent('refresh-page'));
      if (!hadButton) {
        goto(window.location.pathname + window.location.search,
             { invalidateAll: true });
      }
      return;
    }
    // `t` = trade → order ticket modal.
    if (k.toLowerCase() === 't') {
      e.preventDefault();
      openOrderTicketModal();
      return;
    }
    // `h` = history/log → activity modal.
    if (k.toLowerCase() === 'h') {
      e.preventDefault();
      openActivityModal();
      return;
    }

    // ── Grid contextual shortcuts ──────────────────────────────────────
    // `j` = vim-style down navigation on a focused ag-Grid.
    // Outside a grid: reserved (no-op).
    if (k.toLowerCase() === 'j') {
      const aej = /** @type {HTMLElement|null} */ (document.activeElement);
      if (aej?.closest('.ag-root-wrapper')) {
        e.preventDefault();
        aej.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'ArrowDown', code: 'ArrowDown',
          bubbles: true, cancelable: true,
        }));
      }
      return;
    }
    // `k` (plain, no modifier) = kline → chart modal outside a grid;
    // inside a grid, dispatch ArrowUp so ag-Grid moves its own selection.
    if (k.toLowerCase() === 'k') {
      const aek = /** @type {HTMLElement|null} */ (document.activeElement);
      if (aek?.closest('.ag-root-wrapper')) {
        e.preventDefault();
        aek.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'ArrowUp', code: 'ArrowUp',
          bubbles: true, cancelable: true,
        }));
      } else {
        e.preventDefault();
        openChartModal();
      }
      return;
    }

    // `f` = fullscreen toggle on the nearest algo-card ancestor.
    if (k.toLowerCase() === 'f') {
      const afocus = /** @type {HTMLElement|null} */ (document.activeElement);
      const card = afocus?.closest('.algo-card');
      const fsBtn = /** @type {HTMLButtonElement|null} */ (card?.querySelector('.fs-btn'));
      if (fsBtn) { e.preventDefault(); fsBtn.click(); }
      return;
    }
    // `c` = collapse toggle on the nearest algo-card ancestor.
    if (k.toLowerCase() === 'c') {
      const afocus2 = /** @type {HTMLElement|null} */ (document.activeElement);
      const card2 = afocus2?.closest('.algo-card');
      const colBtn = /** @type {HTMLButtonElement|null} */ (card2?.querySelector('.collapse-btn'));
      if (colBtn) { e.preventDefault(); colBtn.click(); }
      return;
    }
    // `Enter` on a focused ag-Grid cell = open context menu if available.
    if (k === 'Enter') {
      const aenter = /** @type {HTMLElement|null} */ (document.activeElement);
      if (aenter?.closest('.ag-root-wrapper')) {
        // ag-Grid handles Enter natively for cell editing; emit a synthetic
        // ContextMenu event so row-action menus open via keyboard.
        aenter.dispatchEvent(new MouseEvent('contextmenu', {
          bubbles: true, cancelable: true,
          clientX: aenter.getBoundingClientRect().left,
          clientY: aenter.getBoundingClientRect().top,
        }));
      }
      return;
    }
  }

  const bullSrc = "/bull.webp";

  // ── Activity polling state ─────────────────────────────────────────
  // Declared early so the demo derivation + nav filter below can
  // read `paperStatus.branch`. Pollers themselves run from onMount.
  let simStatus    = $state(/** @type {any} */ ({ active: false }));
  let paperStatus  = $state(/** @type {any} */ ({ enabled: false, open_order_count: 0 }));
  let replayStatus = $state(/** @type {any} */ ({ active: false }));
  // Shadow + Live status polls were dropped — those status objects
  // were declared and polled every 5 s but read by zero template
  // consumers (the SHADOW / LIVE pills in the navbar derive from
  // the master execution-mode response loaded by `loadMode` every
  // 30 s, not from these polls). Net win: 24 wasted req/min/tab.

  // Demo mode == anonymous visitor on the prod (main) branch. The
  // `paperStatus.branch` flag arrives via the poll; before the
  // first response lands, `isDemo` is conservatively false so the
  // page doesn't flicker.
  //
  // Both `isDemo` and the template `{#if $authStore.user}` read the
  // same `$authStore` rune subscription, so they update atomically
  // in one render tick — no DEMO + Sign Out co-visibility glitch.
  const isDemo = $derived(
    !$authStore.user && paperStatus?.branch === 'main'
  );

  setContext('algoStatus', {
    get paperStatus() { return paperStatus; },
    get isDemo()      { return isDemo; },
    get branch()      { return paperStatus?.branch; },
  });

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

  function _dismissDemoBanner() {
    _demoBannerDismissed = true;
    localStorage.setItem('ramboq.demo_banner_dismissed', '1');
  }

  // Grouped by operator activity, ordered by daily-touch frequency:
  //
  //   Monitor  — Pulse, Dashboard, Agents, Orders, Alerts.
  //              Top-of-funnel "what's happening" surfaces. Hit every
  //              session.
  //   Analyze  — Options. Drill-down workspace.
  //   Modes    — Lab (sim / replay / paper / shadow / live hub). The
  //              mode dropdown is a separate UI element; this is the
  //              workspace router.
  //   Build    — Terminal, Tokens. For extending the agent grammar
  //              and dry-running commands.
  //   Config   — Brokers, Settings, Users, Health. Ordered by
  //              edit-frequency: Brokers first (credential setup),
  //              Health last (diagnostic only).
  //
  // The `branches` field controls visibility:
  //   'dev'  = non-main branch only
  //   'main' = prod only
  //   absent = always shown
  const _algoLinksAll = [
    // ── Tour ── narrative entry point; recruiter / demo lands here from
    // the public-side "Platform Demo" link and walks the architecture
    // before clicking into the live surfaces.
    { href: '/showcase',         label: 'About',     group: 'monitor' },
    // ── Monitor ── ordered by daily-trader workflow frequency:
    //   Pulse → Dashboard (the two always-open watch surfaces)
    //   Orders (active trading entry point)
    //   Derivatives → Charts (analysis surfaces, reached from Orders)
    //   Automation (rule review, lower frequency than active trading)
    //   Strategies → NAV (attribution + LP / fund views, weekly)
    { href: '/pulse',            label: 'Pulse',     group: 'monitor' },
    { href: '/dashboard',        label: 'Dashboard', group: 'monitor' },
    { href: '/orders',           label: 'Orders',    group: 'monitor' },
    { href: '/admin/derivatives', label: 'Derivatives', group: 'monitor' },
    { href: '/charts',           label: 'Charts',    group: 'monitor' },
    { href: '/automation',       label: 'Automation', group: 'monitor' },
    { href: '/strategies',       label: 'Strategies', group: 'monitor' },
    // /nav route deleted — NAV curve now lives as the first tab of
    // the Dashboard equity-curve card. Operator: "remove nav page,
    // move nav to dashboard as the first tab in with intraday and
    // performance." Deep links to /nav should redirect via the
    // SvelteKit 404 — adding a +page.server.ts redirect is overkill
    // for a removed-feature link the operator only used themselves.
    // /admin/alerts is reachable from the 🔔 History link in the
    // /automation page header. Dropped from the top nav to slim the
    // monitor cluster — alert history naturally lives inside the
    // Automation workspace (Agents = rules, Activity = fires).
    // ── Explore ── scenario + replay sandbox. Renamed Jun 2026:
    // group was "modes" (vestigial — the sim/paper/live/shadow/replay
    // mode toggles now live in the navbar dropdown, so this is just
    // the research entry point, not a mode selector). Label was
    // "Lab" — every quant platform (QuantConnect, Streak, Sensibull)
    // uses "Sandbox" for this surface, so the new label reads
    // faster to a first-time visitor. URL kept at /admin/execution
    // for backward-compat with deep links + bookmarks.
    { href: '/admin/execution',  label: 'Sandbox',                       group: 'explore' },
    // ── Build / extend ──
    // /console keeps the URL but the navbar label aligns with the URL
    // ('Console') now that 'Terminal' is the brand name of the whole
    // umbrella (Rambo Terminal). Avoids the visual collision where a
    // first-time visitor sees 'Terminal' in the nav and wonders if it's
    // the whole platform or just one tool.
    { href: '/activity',         label: 'Activity',  group: 'build' },
    { href: '/console',          label: 'Console',   group: 'build' },
    { href: '/admin/research',   label: 'Research',  adminOnly: true, group: 'build' },
    { href: '/admin/tokens',     label: 'Tokens',    group: 'build' },
    // ── Config ── ordered by edit frequency, not alphabetic.
    //   Brokers — most-touched (account creds, IP binding, secrets).
    //   Settings — occasional threshold tuning during volatile days.
    //   Users — invitation-only, rare.
    //   Health — diagnostic surface, glance-only, last.
    { href: '/admin/brokers',    label: 'Brokers',   adminOnly: true, group: 'config' },
    { href: '/admin/settings',   label: 'Settings',  adminOnly: true, group: 'config' },
    { href: '/admin',            label: 'Users',     adminOnly: true, group: 'config' },
    { href: '/admin/statements', label: 'Statements', adminOnly: true, group: 'config' },
    { href: '/admin/history',    label: 'History',   adminOnly: true, group: 'config' },
    { href: '/admin/audit',      label: 'Audit',     adminOnly: true, group: 'config' },
    { href: '/admin/metrics',    label: 'Metrics',   adminOnly: true, group: 'config' },
    { href: '/admin/perf',       label: 'Perf',      adminOnly: true, group: 'config' },
    { href: '/admin/health',     label: 'Health',    adminOnly: true, group: 'config' },
  ];
  // Branch-aware + demo-aware + mode-aware filter.
  // `modes: [...]` on a link entry means "show only when the current
  // executionMode is in this list" — used to hide Execution when the
  // operator is in PAPER/LIVE/SHADOW (modes that have no dedicated
  // workspace).
  const algoLinks = $derived(
    _algoLinksAll.filter(l => {
      if (l.adminOnly && isDemo) return false;
      if (l.branches) {
        const branch = paperStatus?.branch || 'dev';
        const key = branch === 'main' ? 'main' : 'dev';
        if (!l.branches.includes(key)) return false;
      }
      if (l.modes && !l.modes.includes($executionMode)) return false;
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

  // ── Group disclosure for the desktop nav ──────────────────────────
  //
  // Groups with ≥2 items collapse behind a labelled dropdown button;
  // single-item groups (Analyze=Derivatives, Modes=Lab) render inline
  // because a dropdown wrapping a single child is overhead with no
  // benefit. Monitor stays inline always — high-frequency surfaces
  // (Pulse / Dashboard / Agents / Orders) should be one-click.
  const GROUP_LABELS = {
    monitor: 'Monitor',
    analyze: 'Analyze',
    explore: 'Explore',
    build:   'Build',
    config:  'Config',
  };
  // Order matters — defines the visual position of each dropdown
  // trigger after the inline section. Operator-frequency-ordered.
  const DROPDOWN_GROUPS = ['build', 'config'];
  // Render these groups inline (never collapse).
  const INLINE_GROUPS = new Set(['monitor', 'analyze', 'explore']);

  let openGroup = $state(/** @type {string | null} */ (null));

  const groupedLinks = $derived.by(() => {
    /** @type {any[]} */ const inline = [];
    /** @type {Record<string, any[]>} */ const dropdowns = {};
    for (const g of DROPDOWN_GROUPS) dropdowns[g] = [];
    for (const link of algoLinks) {
      if (INLINE_GROUPS.has(link.group) || !DROPDOWN_GROUPS.includes(link.group)) {
        inline.push(link);
      } else {
        dropdowns[link.group].push(link);
      }
    }
    return { inline, dropdowns };
  });

  // Which group is the current page in? Used to keep the dropdown
  // trigger lit (algo-nav-btn-active) when any item inside it is
  // the active route — so the operator can see at a glance "I'm
  // inside Config" even without opening the panel.
  const activeGroup = $derived.by(() => {
    const path = page.url.pathname;
    let best = null;
    let bestLen = 0;
    for (const link of algoLinks) {
      const h = link.href;
      if ((path === h || path.startsWith(h + '/')) && h.length > bestLen) {
        best = link.group;
        bestLen = h.length;
      }
    }
    return best;
  });

  function gotoGroupItem(/** @type {string} */ href) {
    goto(href);
    openGroup = null;
  }

  // Broker auth health modal — opened from the 5/5 broker-chip click.
  // BrokerHealthBadge renders modal-only (no standalone trigger).
  let brokerHealthOpen  = $state(false);

  // ── Execution-mode combobox ────────────────────────────────────────
  let modeOpen          = $state(false);
  let modeError         = $state('');
  let allowedModes      = $state(/** @type {string[]} */ ([]));
  let modeBranch        = $state('');
  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _liveConfirmRef   = $state(null);
  // Computed position for the mode dropdown — recalculated from the
  // trigger's getBoundingClientRect() each time the dropdown opens so
  // the dropdown renders via position:fixed above any stacking context
  // (including the order-modal overlay at z-index 10500).
  /** @type {{ top: number, right: number } | null} */
  let modeDropdownPos   = $state(null);
  /** @type {HTMLButtonElement | null} */
  let _modeTriggerEl    = $state(null);
  /** @type {HTMLButtonElement | null} */
  let _modeTriggerElMobile = $state(null);

  /** Open the mode dropdown and compute its fixed position from the trigger rect. */
  function openModeDropdown(/** @type {HTMLButtonElement | null} */ triggerEl) {
    modeError = '';
    if (modeOpen) { modeOpen = false; return; }
    if (triggerEl) {
      const r = triggerEl.getBoundingClientRect();
      modeDropdownPos = {
        top: r.bottom + 4,
        // anchor right edge to trigger right edge (same as absolute right:0)
        right: window.innerWidth - r.right,
      };
    }
    modeOpen = true;
  }

  // Mode colors — keep aligned with the .mode-pill-* CSS in
  // LogPanel.svelte and the .algo-mode-* badges below. SIM and REPLAY
  // both use green (#4ade80) — CLAUDE.md spec: "SIM/REPLAY green".
  // PAPER sky-blue (#7dd3fc), LIVE red-400 (#f87171), SHADOW orange.
  // LIVE shifted from emerald-400 to red-400 (#f87171). The old
  // palette put LIVE and REPLAY both on green, indistinguishable at
  // a glance — operationally a footgun since LIVE is the only mode
  // that moves real money. RED is the universal trading-platform
  // convention for "real broker". Aligns with the LIVE banner red
  // and keeps the safe modes (paper sky, replay green, sim green,
  // shadow orange) visually distinct from the dangerous one.
  const MODE_COLOR = {
    idle:   '#94a3b8',   // slate-400 — engine dormant (dev only)
    sim:    'var(--c-long)',   // green-400 — matches replay (both safe/sandbox)
    replay: 'var(--c-long)',   // pos-green
    paper:  '#7dd3fc',   // info-sky
    shadow: '#fb923c',   // short-orange
    live:   'var(--c-short)',   // red-400 — danger / real broker
  };
  // ── Persistence refresh-cycle mode (slice AK) ───────────────────
  // Admin-only — surfaces when the operator has flipped persistence
  // bypass on so it stays visible from every algo page (otherwise
  // they'd have to navigate to /admin/health to remember they're
  // skipping cache + DB). Only fetched for admin users to avoid 401
  // spam from non-admin sessions.
  let persistMode = $state(/** @type {'off'|'soft'|'hard'} */ ('off'));
  const _canViewPersistMode = $derived(hasCap('view_audit', $userCaps, $userRole));
  async function loadPersistMode() {
    if (!_canViewPersistMode) return;
    try {
      const r = await fetchPersistenceMode();
      if (r?.mode && r.mode !== persistMode) persistMode = r.mode;
    } catch (_) { /* silent — not load-bearing */ }
  }
  async function _flipPersistOff() {
    try {
      // applyPersistenceMode handles both the backend call and any
      // frontend symbolStore / market-store cleanup. For OFF the
      // cleanup path is a no-op (backend resumes normal three-tier
      // reads), so this is functionally equivalent to the old direct
      // setPersistenceMode('off') call — but routes through the
      // canonical refresh-cycle wrapper for consistency.
      const { applyPersistenceMode } = await import('$lib/data/refreshCycle');
      await applyPersistenceMode('off');
      persistMode = 'off';
      toast.success('Refresh cycle restored to normal');
    } catch (e) {
      toast.error(`Failed to restore: ${e.message || e}`);
    }
  }

  async function loadMode() {
    try {
      const res = await fetchExecutionMode();
      if (res?.mode && get(executionMode) !== res.mode) executionMode.set(res.mode);
      if (res?.allowed_modes && res.allowed_modes.length) {
        allowedModes = res.allowed_modes;
      }
      if (res?.branch)        modeBranch   = res.branch;
    } catch (e) {
      console.warn('[mode] /api/admin/execution/mode fetch failed:', e);
    }
    if (allowedModes.length === 0) {
      // Fallback so the chip is selectable even when the API is unreachable.
      allowedModes = paperStatus?.branch === 'main'
        ? ['paper', 'live', 'shadow', 'sim', 'replay']
        : ['paper', 'sim', 'replay'];
    }
  }

  async function pickMode(/** @type {string} */ mode) {
    modeOpen  = false;
    modeError = '';
    // IDLE — dev-only kill-switch. Backend sets execution.dev_active=False
    // + stops the KiteTicker; the chip flips immediately so the operator
    // sees the change. No navigation, no confirm — toggling IDLE is
    // cheap (the only side-effect is stopping broker calls).
    if (mode === 'idle') {
      executionMode.set(/** @type {any} */ ('idle'));
      await _commitMode('idle');
      return;
    }
    // SIM + REPLAY aren't settings toggles — they need a driver to
    // be started. Navigate to the dedicated page where the operator
    // configures + starts the driver. The chip auto-flips when the
    // driver becomes active (poller picks it up via _get_current_mode).
    if (mode === 'sim' || mode === 'replay') {
      await goto(`/admin/execution?mode=${mode}`);
      return;
    }
    if (mode === 'live') {
      // LIVE keeps the confirm modal — real broker calls deserve a
      // dress-rehearsal click. Confirmed via ConfirmModal (danger variant).
      const ok = await _liveConfirmRef?.ask({
        title: 'Switch to LIVE mode?',
        message: 'Orders placed from this session will hit the real broker.',
        danger: true,
        confirmLabel: 'Switch to LIVE',
        cancelLabel: 'Cancel',
      });
      if (!ok) return;
      await _commitMode('live');
      return;
    }
    // PAPER / SHADOW: master-toggle only. Commit the flag, optimistically
    // flip the chip, stay on the current page.
    executionMode.set(/** @type {any} */ (mode));
    await _commitMode(mode);
  }

  async function _commitMode(/** @type {string} */ mode) {
    try {
      const res = await setExecutionMode(mode);
      if (res?.mode && get(executionMode) !== res.mode) executionMode.set(res.mode);
    } catch (e) {
      modeError = /** @type {any} */ (e)?.message ?? 'Mode change failed.';
      setTimeout(() => { modeError = ''; }, 3000);
    }
  }

  // ── Chase chip + timeline drawer ───────────────────────────────────
  let chaseOrders     = $state(/** @type {any[]} */ ([]));
  let drawerOpen      = $state(false);
  let lastTerminalAt  = $state(/** @type {Date|null} */ (null));
  let chaseInteracted = $state(false);  // tracks hover/click in the 60s auto-hide window

  // Open order count derived from chaseOrders (flat event list grouped by order_id).
  const openOrderIds = $derived.by(() => {
    const ids = new Set();
    for (const ev of chaseOrders) {
      if ((ev.status ?? ev.kind) === 'open' || ev.order_status === 'OPEN') {
        ids.add(ev.order_id ?? ev.id);
      }
    }
    return ids;
  });

  /** Unique symbols in open orders — shown in the hover tooltip. */
  const openSymbols = $derived.by(() => {
    const syms = new Set();
    for (const ev of chaseOrders) {
      if (openOrderIds.has(ev.order_id ?? ev.id)) {
        const sym = ev.symbol ?? ev.tradingsymbol;
        if (sym) syms.add(sym);
      }
    }
    return [...syms];
  });

  async function pollChase() {
    try {
      const res = await fetchOrderEvents(50, 'open');
      // Accept { events: [...] } or bare array.
      const raw = Array.isArray(res) ? res : (res?.events ?? res?.orders ?? []);
      chaseOrders = raw;
      // Track last terminal event for auto-hide timer.
      const terminal = raw.filter(e => {
        const k = e.kind ?? e.event_type ?? '';
        return ['fill','unfill','reject','cancel'].includes(k);
      });
      if (terminal.length) {
        const ts = terminal.map(e => new Date(e.created_at ?? e.timestamp ?? 0));
        const newest = new Date(Math.max(...ts.map(d => d.getTime())));
        if (!lastTerminalAt || newest > lastTerminalAt) lastTerminalAt = newest;
      }
    } catch (_) { /* treat as no open orders */ }
  }

  // Auto-hide chip + drawer 60s after last terminal unless the operator
  // is currently hovering/has clicked (chaseInteracted). The interaction
  // flag resets itself 60s after last interaction so idle eventually wins.
  let _chaseHideTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
  function _onChaseInteract() {
    chaseInteracted = true;
    if (_chaseHideTimer) clearTimeout(_chaseHideTimer);
    _chaseHideTimer = setTimeout(() => { chaseInteracted = false; }, 60_000);
  }

  const showChaseChip = $derived.by(() => {
    if (openOrderIds.size > 0) return true;
    if (!lastTerminalAt) return false;
    if (chaseInteracted) return true;
    const age = Date.now() - lastTerminalAt.getTime();
    return age < 60_000;
  });

  // ── Polling pipeline ───────────────────────────────────────────────
  // The state vars are declared at the top of the script so the
  // `isDemo` derivation + nav filter can read them; the actual
  // pollers + lifecycle live here.
  let simTeardown, paperTeardown, replayTeardown;
  let modeTeardown, chaseTeardown, persistTeardown;
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
  // Adaptive cadence — Sim/Paper/Replay fast-poll only while their
  // mode is active (banner + chip data is live); when idle they
  // back off to 30 s. A typical session has all three idle most of
  // the time, so this drops baseline load from ~63 to ~6 req/min
  // per tab while keeping snappy updates when something IS running.
  function _adaptiveInterval(/** @type {() => Promise<void>} */ poll,
                              /** @type {() => boolean} */ isActive,
                              fastMs = 4000, slowMs = 30000) {
    let stopped = false;
    let timer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);
    const schedule = () => {
      if (stopped) return;
      const ms = isActive() ? fastMs : slowMs;
      timer = setTimeout(async () => {
        if (stopped) return;
        try { await poll(); } catch (_) { /* swallow */ }
        schedule();
      }, ms);
    };
    schedule();
    return () => {
      stopped = true;
      if (timer != null) clearTimeout(timer);
    };
  }
  onMount(() => {
    _demoBannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
    // Global keyboard shortcuts (slice AU). Window-level keydown
    // listener; pauses while the operator is typing in a field.
    window.addEventListener('keydown', _onGlobalKeydown);
    // Broker connectivity poller (slice AX). Singleton — safe to call
    // from multiple components; the poller idempotently short-circuits
    // on second invocation. Already used by RefreshButton; calling here
    // ensures the navbar chip stays fresh even on pages with no
    // RefreshButton mounted.
    startConnStatusPoller();
    // Broker auth-health store — drives chip color (green/amber/red) and
    // feeds BrokerHealthBadge popup without duplicate polling. Singleton.
    startBrokerHealthPoller();
    // Holiday-aware /api/market/status poller — feeds isNseOpen/isMcxOpen
    // so the RefreshButton "market closed" popup fires correctly on
    // Indian-market holidays where weekday+time alone says "open".
    startMarketStatusPoller();
    // RBAC bootstrap — populates `userRole` + `userCaps` stores from
    // /api/auth/whoami. Idempotent across remounts (the bootstrap
    // function itself short-circuits on second call). Fires before
    // any of the data-poll work below since pollers + nav gating
    // both read off the role/caps state.
    bootstrapRBAC();
    // Start the singleton book_changed WS subscriber. Pages that
    // depend on position-derived data subscribe to the bookChanged
    // store and refetch on increment — replaces the prior "wait for
    // next poll tick" pattern where a postback took 2+ iterations
    // to settle the snapshot grid.
    startBookChangedBus();
    // Read polling.idle_timeout_min from /api/admin/settings so the
    // hibernation threshold matches the operator's configured value.
    // Same fetch lifts `pulse.tick_interval_ms` to drive the cross-page
    // book poller cadence — operator's stated end-state (2026-06-28):
    //   "every page should poll when viewport active. only when viewport
    //    is not active for 5 mins, go into hibernation."
    // Fire-and-forget — failure keeps the in-code defaults (5 min idle,
    // 5 s book cadence).
    (async () => {
      try {
        const rows = await fetchSettings();
        const all = Array.isArray(rows) ? rows : (rows?.settings || []);
        const idleRow = all.find?.(s => s?.key === 'polling.idle_timeout_min');
        const idleV = Number(idleRow?.value ?? idleRow?.default_value);
        if (Number.isFinite(idleV) && idleV >= 0) setHibernationIdleMinutes(idleV);
        const tickRow = all.find?.(s => s?.key === 'pulse.tick_interval_ms');
        const tickV = Number(tickRow?.value ?? tickRow?.default_value);
        if (Number.isFinite(tickV) && tickV >= 500 && tickV <= 60000) {
          setBookPollerInterval(tickV);
        }
      } catch { /* anon/demo — keep defaults */ }
    })();
    // Cross-page book poller (positions / holdings / funds). Layout-resident
    // singleton — runs at `pulse.tick_interval_ms` cadence (default 5 s)
    // regardless of which page is routed. Pages stay consumers (their
    // existing `*.load()` on mount dedups via createDataStore's in-flight
    // Promise); nav is instant because the stores are already hot.
    // Hibernation gates fire for free via `marketAwareInterval` (throttle
    // to 30 s after 5 min hidden, immediate refire on tab return).
    startBookPollers();
    // Canonical account display ordering (Jul 2026). Loaded once at boot;
    // refreshed on demand via accountDisplayOrder.refresh() after PATCH.
    // Non-fatal: UI falls back to insertion order when API unreachable.
    loadAccountOrder().catch(() => {});
    // Expose tickBus on window.__stores for Playwright specs (dev only).
    // Lets specs call window.__stores.tickBus.emit(sym, dir) to inject
    // synthetic ticks and assert flash class synchrony without needing
    // a real SSE stream. Kept out of the store module so the prod bundle
    // has no window reference from the data layer.
    if (typeof window !== 'undefined' &&
        (window.location.hostname === 'dev.ramboq.com' ||
         window.location.hostname === 'localhost' ||
         window.location.hostname === '127.0.0.1')) {
      /** @type {any} */ (window).__stores = /** @type {any} */ (window).__stores || {};
      /** @type {any} */ (window).__stores.tickBus = tickBus;
    }
    // Market-gated quote stream gate. Starts the SSE if any segment is
    // open right now; runs a 30 s visibility-aware watcher that pauses /
    // resumes the stream on open ↔ close transitions. Idempotent —
    // MarketPulse also calls startQuoteStream() directly; the singleton
    // pattern in quoteStream.js deduplicates. Torn down in onDestroy.
    startMarketGatedQuoteStream();
    // Fire once, then schedule adaptive polls.
    pollSim();
    pollPaper();
    pollReplay();
    simTeardown    = _adaptiveInterval(pollSim,
      () => !!(simStatus?.active || simStatus?.run_active), 4000, 30000);
    paperTeardown  = _adaptiveInterval(pollPaper,
      () => !!(paperStatus?.open_order_count > 0), 4000, 30000);
    replayTeardown = _adaptiveInterval(pollReplay,
      () => !!replayStatus?.active, 5000, 30000);
    loadMode();   modeTeardown   = visibleInterval(loadMode,  30000);
    // Persistence-mode poller (admin-only; non-admins see nothing).
    // Same 30s cadence as execution mode.
    loadPersistMode();
    persistTeardown = visibleInterval(loadPersistMode, 30000);
    // pollChase — adaptive. 5 s when an order is open (operator wants
    // live chase progress), 60 s otherwise. Without this it ran at 10 s
    // on every algo page including Tokens / Settings / Brokers / Users
    // where chase is irrelevant — 6 wasted req/min per idle tab.
    pollChase();
    chaseTeardown = _adaptiveInterval(pollChase,
      () => openOrderIds.size > 0, 5000, 60000);
  });
  onDestroy(() => {
    simTeardown?.(); paperTeardown?.(); replayTeardown?.();
    modeTeardown?.(); chaseTeardown?.(); persistTeardown?.();
    window.removeEventListener('keydown', _onGlobalKeydown);
    _clearG();
    stopMarketGatedQuoteStream();
  });

  // ── Demo / signin redirect ─────────────────────────────────────────
  //   - Anonymous on main     → demo mode (no redirect, isDemo = true).
  //   - Anonymous on non-main → /signin (devs are expected to log in).
  //   - Logged in (any role)  → free pass; per-page cap_guard catches
  //                             permission denials surface-by-surface.
  //                             The 5-role RBAC (admin / trader / risk
  //                             / ops / observer) all have legitimate
  //                             algo access — the prior blanket
  //                             "non-admin → /signin" redirect was
  //                             vestigial from the old admin / partner
  //                             two-tier model and broke the tour for
  //                             trader / risk / ops / observer users
  //                             who clicked any algo link.
  // While the paper-status poll is still pending the branch is
  // unknown — we stay put rather than flicker the user away.
  $effect(() => {
    const branchKnown = paperStatus?.branch !== undefined;
    if (!$authStore.user && branchKnown && paperStatus.branch !== 'main') {
      goto('/signin');
    }
  });
</script>

<!-- Navigation loading indicator — top-bar cyan progress strip.
     Mounted above everything else so it is always the first DOM node
     rendered and paints before any page-level work begins. bind:this
     gives the onNavigate / afterNavigate hooks a direct reference to
     call start() and complete(). -->
<NavigationIndicator bind:this={_navIndicator} variant="algo" />

<ConfirmModal bind:this={_liveConfirmRef} />
<ShortcutCheatsheet open={_cheatsheetOpen}
                    onClose={() => { _cheatsheetOpen = false; }} />
<!-- Single ActivityLogModal mount for the entire algo surface — driven
     by the activityModal store. PageHeaderActions's Log button and the
     navbar broker-status chip both write to the store; this is the only
     instance rendered, so they can't stack duplicates.
     Tab selection is now owned by activityStore (shared with /activity
     page); openActivityModal(tab) writes to activityStore before setting
     open=true so the modal renders on the correct tab from the first frame. -->
{#if $activityModal.open}
  <ActivityLogModal
    onClose={closeActivityModal} />
{/if}
{#if $chartModal.open}
  <ChartModal
    symbol={$chartModal.symbol}
    exchange={$chartModal.exchange} />
{/if}

<!-- Broker auth health modal (modal-only) — opens from the 5/5 chip
     in either the desktop or mobile navbar block. Single mount keeps
     state shared across viewport variants. -->
{#if $authStore.user && ($authStore.user.role === 'admin' || $authStore.user.role === 'designated')}
  <BrokerHealthBadge bind:open={brokerHealthOpen} />
{/if}

<div class="algo-viewport card-theme-dark">
  <div class="algo-card">
    <!-- Top bar -->
    <header class="algo-navbar">
      <div class="algo-nav-inner hidden lg:flex items-center gap-1 h-12">
        <!-- Vertical ALGO label, flush at the left edge. Bare text —
             no chip, no background, no border. -->
        <div class="flex items-center h-full">
          <span class="algo-vert" aria-hidden="true">ALGO</span>
          <!-- Site label — bull + name link to the main landing page
               at ramboq.com. Operator: "when I click on bull hero logo,
               it should show main landing page of ramboq.com." -->
          <button onclick={() => goto('/pulse')} class="algo-brand">
            <img src={bullSrc} alt="" class="algo-brand-bull" />
            <span class="algo-brand-name">RamboQuant</span>
          </button>
        </div>

        <nav class="flex items-center justify-center gap-0.5 flex-1">
          <!-- Inline section — Monitor + Analyze + Modes groups always
               visible (high-frequency surfaces). Group separators
               match the original flat-bar look. Keyed by link.href
               so Svelte 5's reconciler diffs by identity when the
               algoLinks list updates in place (e.g. when
               executionMode changes filter the visible items). -->
          {#each groupedLinks.inline as link, i (link.href)}
            {#if i > 0 && link.group !== groupedLinks.inline[i - 1].group}
              <span class="algo-nav-sep" aria-hidden="true"></span>
            {/if}
            <button
              onclick={() => goto(link.href)}
              onmouseenter={() => _preloadHover(link.href)}
              class="algo-nav-btn {isActive(link.href) ? 'algo-nav-btn-active' : ''}"
            >{link.label}</button>
          {/each}

          <!-- Disclosure dropdowns — Build + Config groups collapse
               behind their group label. Trigger stays lit when the
               operator's current page is inside that group. -->
          {#each DROPDOWN_GROUPS as g}
            {#if groupedLinks.dropdowns[g]?.length > 0}
              <span class="algo-nav-sep" aria-hidden="true"></span>
              <div class="algo-group-wrap">
                <button
                  type="button"
                  class="algo-nav-btn algo-group-trigger {activeGroup === g ? 'algo-nav-btn-active' : ''}"
                  aria-haspopup="menu"
                  aria-expanded={openGroup === g}
                  onclick={() => openGroup = openGroup === g ? null : g}
                >{GROUP_LABELS[g]}<svg width="8" height="8" viewBox="0 0 10 6" fill="none" class="algo-group-caret">
                    <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5"
                          stroke-linecap="round" stroke-linejoin="round"/>
                  </svg></button>
                {#if openGroup === g}
                  <div class="algo-group-overlay" role="presentation"
                       onclick={() => openGroup = null}></div>
                  <div class="algo-group-panel" role="menu">
                    {#each groupedLinks.dropdowns[g] as link}
                      <button role="menuitem"
                        onclick={() => gotoGroupItem(link.href)}
                        onmouseenter={() => _preloadHover(link.href)}
                        class="algo-group-item {isActive(link.href) ? 'algo-group-item-active' : ''}"
                      >{link.label}</button>
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
          {/each}
        </nav>

        <!-- ── Execution-mode chip + dropdown ──────────────────────
             This is BOTH the current-mode badge AND the picker trigger.
             Replaces the old "MODE: PAPER ▾" button and the transient
             mode-* badges below — one element does both jobs. -->
        {#if $authStore.user && allowedModes.length > 0}
          <div class="mode-combo-wrap">
            <button class="algo-mode-badge mode-trigger"
                    bind:this={_modeTriggerEl}
                    data-mode={$executionMode ?? 'live'}
                    onclick={() => openModeDropdown(_modeTriggerEl)}
                    aria-haspopup="listbox" aria-expanded={modeOpen}
                    title="Click to change execution mode">
              {($executionMode ?? 'live').toUpperCase()}
              <svg width="8" height="8" viewBox="0 0 10 6" fill="none" class="mode-trigger-caret">
                <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            {#if modeOpen && modeDropdownPos}
              <!-- Portalled to document.body so the dropdown escapes
                   EVERY ancestor stacking context — z-index alone was
                   insufficient (operator 2026-07-01: "mode dropdown is
                   not showing over modals. double check"). -->
              <div class="mode-combo-overlay" use:portal role="presentation"
                   onclick={() => { modeOpen = false; }}></div>
              <ul class="mode-combo-dropdown" use:portal role="listbox"
                  style="top:{modeDropdownPos.top}px; right:{modeDropdownPos.right}px">
                {#each allowedModes as m}
                  <li>
                    <button class="mode-combo-item {$executionMode === m ? 'mode-combo-item-active' : ''}"
                            style="--mc: {MODE_COLOR[m] ?? '#94a3b8'}"
                            role="option" aria-selected={$executionMode === m}
                            onclick={() => pickMode(m)}>
                      {m.toUpperCase()}
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if modeError}<span class="mode-combo-error">{modeError}</span>{/if}
          </div>
        {/if}

        <!-- ── Persistence refresh-cycle chip (slice AK) ─────────────
             Visible only when admin AND mode is non-default (soft/hard).
             Operator's reminder that they're skipping cache+DB — clicking
             flips it back to OFF immediately. Hidden when mode='off' to
             avoid navbar clutter during normal operation. -->
        {#if _canViewPersistMode && persistMode !== 'off'}
          <button class="persist-chip persist-chip-{persistMode}"
                  onclick={_flipPersistOff}
                  title={persistMode === 'soft'
                    ? 'SOFT bypass: every read skips cache + DB, hits broker, writes back. Click to restore normal mode.'
                    : 'HARD bypass: soft + ticker recycle on transition. Click to restore normal mode.'}>
            <svg width="9" height="9" viewBox="0 0 12 12" fill="none"
                 stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
                 stroke-linejoin="round" aria-hidden="true">
              <path d="M1 6a5 5 0 0 1 9-3" />
              <path d="M10 1v3h-3" />
              <path d="M11 6a5 5 0 0 1-9 3" />
              <path d="M2 11V8h3" />
            </svg>
            BYPASS · {persistMode.toUpperCase()}
          </button>
        {/if}

        <!-- ── Broker connectivity chip (slice AX, consolidated AY) ───
             Single navbar entry point for broker state. Ambient color:
             green=all loaded, amber=partial, red=none, ?=API unreachable.
             Click → opens BrokerHealthBadge modal with per-account auth
             detail (last_good_at + reason). The standalone AUTH badge
             was removed — operator: two chips for adjacent concerns
             felt redundant; consolidate into one. -->
        {#if $authStore.user && $connStatus.total > 0}
          {@const _loaded  = $connStatus.loaded}
          {@const _total   = $connStatus.total}
          {@const _ok      = $connStatus.backendOk}
          {@const _worst   = $brokerHealthStore.worstState}
          {@const _cls     = _worst === 'red'   ? 'broker-chip-down'
                           : _worst === 'amber' ? 'broker-chip-partial'
                           : _worst === 'green' ? 'broker-chip-ok'
                           : 'broker-chip-partial'}
          {@const _failList = $connStatus.failingAccounts.join(', ')}
          <button class="broker-chip {_cls}"
                  onclick={() => brokerHealthOpen = !brokerHealthOpen}
                  title={!_ok ? 'Broker status: API unreachable. Click for per-account auth detail.'
                       : _loaded === _total ? `Broker status: ${_loaded} / ${_total} accounts loaded. Click for per-account auth detail.`
                       : `Broker status: ${_loaded} / ${_total} loaded. Failing: ${_failList}. Click for per-account auth detail.`}>
            <span class="broker-chip-dot" aria-hidden="true"></span>
            {_loaded}/{_total}
          </button>
        {/if}

        <!-- ── Chase chip ─────────────────────────────────────────── -->
        {#if $authStore.user && showChaseChip}
          <button
            class="chase-chip"
            onclick={() => { drawerOpen = !drawerOpen; _onChaseInteract(); }}
            onmouseenter={() => { _onChaseInteract(); }}
            title={openSymbols.length ? `Chasing: ${openSymbols.join(', ')}` : 'Chase orders'}
          >
            <svg width="10" height="10" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clip-rule="evenodd"/>
            </svg>
            <span>{openOrderIds.size} chasing</span>
            <span class="chase-chip-arrow">→</span>
          </button>
        {/if}

        <!-- DEMO badge — session-state indicator (anonymous-on-prod),
             not a mode setting, so it stays separate from the mode chip. -->
        {#if isDemo}
          <span class="algo-mode-badge algo-mode-demo"
                title="Demo mode — anonymous visitor, paper-only, no broker connection">DEMO</span>
        {/if}
        {#if $authStore.user}
          <span class="algo-user-pill">
            {$authStore.user.username ?? ''}
            {#if $authStore.user.role === 'designated'}
              <span class="algo-user-role algo-user-role-designated" title="designated">DSG</span>
            {:else if $authStore.user.role === 'admin'}
              <span class="algo-user-role algo-user-role-admin">admin</span>
            {:else if $authStore.user.role === 'trader'}
              <span class="algo-user-role algo-user-role-trader">trader</span>
            {:else if $authStore.user.role === 'risk'}
              <span class="algo-user-role algo-user-role-risk">risk</span>
            {:else if $authStore.user.role === 'partner'}
              <span class="algo-user-role algo-user-role-partner">partner</span>
            {/if}
          </span>
        {/if}
        <button onclick={() => goto('/about')} class="algo-pub-link">↙ Investor site</button>
        {#if isDemo && !$authStore.user}
          <button onclick={() => goto('/signin')} class="algo-nav-btn">Sign In</button>
        {/if}
        {#if $authStore.user}
          <button onclick={signOut} class="algo-nav-btn">Sign Out</button>
        {/if}
      </div>

      <!-- Mobile -->
      <div class="algo-nav-inner lg:hidden flex items-center justify-between h-12">
        <div class="flex items-center">
          <span class="algo-vert algo-vert-sm" aria-hidden="true">ALGO</span>
          <button onclick={() => goto('/pulse')} class="algo-brand">
            <img src={bullSrc} alt="" class="algo-brand-bull algo-brand-bull-sm" />
            <span class="algo-brand-name">RamboQuant</span>
          </button>
        </div>
        {#if isDemo}
          <span class="algo-mode-badge algo-mode-demo" title="Demo mode — paper-only">DEMO</span>
        {/if}
        <!-- Mobile mode chip — same chip-as-combobox pattern as desktop. -->
        {#if $authStore.user && allowedModes.length > 0}
          <div class="mode-combo-wrap">
            <button class="algo-mode-badge mode-trigger"
                    bind:this={_modeTriggerElMobile}
                    data-mode={$executionMode ?? 'live'}
                    onclick={() => openModeDropdown(_modeTriggerElMobile)}
                    aria-haspopup="listbox" aria-expanded={modeOpen}
                    title="Click to change execution mode">
              {($executionMode ?? 'live').toUpperCase()}
              <svg width="8" height="8" viewBox="0 0 10 6" fill="none" class="mode-trigger-caret">
                <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            {#if modeOpen && modeDropdownPos}
              <!-- Portalled to document.body so the dropdown escapes
                   EVERY ancestor stacking context — z-index alone was
                   insufficient (operator 2026-07-01: "mode dropdown is
                   not showing over modals. double check"). -->
              <div class="mode-combo-overlay" use:portal role="presentation"
                   onclick={() => { modeOpen = false; }}></div>
              <ul class="mode-combo-dropdown" use:portal role="listbox"
                  style="top:{modeDropdownPos.top}px; right:{modeDropdownPos.right}px">
                {#each allowedModes as m}
                  <li>
                    <button class="mode-combo-item {$executionMode === m ? 'mode-combo-item-active' : ''}"
                            style="--mc: {MODE_COLOR[m] ?? '#94a3b8'}"
                            role="option" aria-selected={$executionMode === m}
                            onclick={() => pickMode(m)}>
                      {m.toUpperCase()}
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if modeError}<span class="mode-combo-error">{modeError}</span>{/if}
          </div>
        {/if}

        <!-- Broker connectivity chip (mobile mirror of the desktop block above). -->
        {#if $authStore.user && $connStatus.total > 0}
          {@const _loaded = $connStatus.loaded}
          {@const _total  = $connStatus.total}
          {@const _ok     = $connStatus.backendOk}
          {@const _worst  = $brokerHealthStore.worstState}
          {@const _cls    = _worst === 'red'   ? 'broker-chip-down'
                          : _worst === 'amber' ? 'broker-chip-partial'
                          : _worst === 'green' ? 'broker-chip-ok'
                          : 'broker-chip-partial'}
          <button class="broker-chip {_cls}"
                  onclick={() => brokerHealthOpen = !brokerHealthOpen}
                  title={!_ok ? 'Broker status: API unreachable. Click for per-account auth detail.'
                       : `Broker status: ${_loaded} / ${_total} loaded. Click for per-account auth detail.`}>
            <span class="broker-chip-dot" aria-hidden="true"></span>
            {_loaded}/{_total}
          </button>
        {/if}

        {#if $authStore.user && showChaseChip}
          <button
            class="chase-chip"
            onclick={() => { drawerOpen = !drawerOpen; _onChaseInteract(); }}
            onmouseenter={() => { _onChaseInteract(); }}
            title={openSymbols.length ? `Chasing: ${openSymbols.join(', ')}` : 'Chase orders'}
          >
            <svg width="10" height="10" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clip-rule="evenodd"/>
            </svg>
            <span>{openOrderIds.size}</span>
            <span class="chase-chip-arrow">→</span>
          </button>
        {/if}
        {#if $authStore.user}
          <span class="algo-user-pill">
            {$authStore.user.username ?? ''}
            {#if $authStore.user.role === 'designated'}
              <span class="algo-user-role algo-user-role-designated" title="designated">DSG</span>
            {:else if $authStore.user.role === 'admin'}
              <span class="algo-user-role algo-user-role-admin">admin</span>
            {:else if $authStore.user.role === 'trader'}
              <span class="algo-user-role algo-user-role-trader">trader</span>
            {:else if $authStore.user.role === 'risk'}
              <span class="algo-user-role algo-user-role-risk">risk</span>
            {:else if $authStore.user.role === 'partner'}
              <span class="algo-user-role algo-user-role-partner">partner</span>
            {/if}
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

      <!-- Mobile drawer — grouped by section. Each group renders its
           caption first, then its items. Order mirrors the desktop
           layout (Monitor → Analyze → Modes → Build → Config). -->
      {#if menuOpen}
        <nav class="algo-mobile-dropdown">
          {#each Object.keys(GROUP_LABELS) as g (g)}
            {@const items = algoLinks.filter(l => l.group === g)}
            {#if items.length > 0}
              <div class="algo-mobile-group-label">{GROUP_LABELS[g]}</div>
              {#each items as link (link.href)}
                <button
                  onclick={() => { goto(link.href); closeMenu(); }}
                  onmouseenter={() => _preloadHover(link.href)}
                  class="algo-mobile-item {isActive(link.href) ? 'algo-mobile-active' : ''}"
                >{link.label}</button>
              {/each}
            {/if}
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

    <!-- Mode banners removed entirely. The colored navbar mode pill
         (PAPER sky · LIVE red · SHADOW orange · SIM rose · REPLAY
         green) is the single source of mode visibility. SIM and
         REPLAY tick/iteration progress is visible inside the
         /admin/execution mode page; no need to broadcast it across
         every page. Operator: "remove banner for every mode
         including shadow". -->

    <!-- Glanceable position / holdings strip — pinned just under
         the navbar (and below SIM / PAPER banners when those are
         active) so it reads as part of the chrome. Self-fetches
         from /api/positions + /api/holdings on mount; auto-hides
         when the operator has no positions. -->
    <PositionStrip />

    <!-- Chase timeline drawer — rendered outside normal flow so it
         overlays everything. Mounts inside .algo-card for z-index context. -->
    <OrderTimelineDrawer
      open={drawerOpen}
      orders={chaseOrders}
      onClose={() => { drawerOpen = false; }}
    />

    <ImpersonationBanner />

    <main class="algo-content">
      {#if isDemo && !_demoBannerDismissed}
        <div class="demo-banner" role="status">
          <span class="demo-banner-text">
            <strong>Demo mode</strong> — live market data · paper trades only · no real orders
          </span>
          <button onclick={_dismissDemoBanner} class="demo-banner-close" aria-label="Dismiss">×</button>
        </div>
      {/if}
      {@render children()}
    </main>

    <!-- In-app rich popup channel: AgentToast subscribes to /ws/algo
         for inapp notifications + paints stacked toasts top-right.
         Click a toast → opens AgentFireModal. Mounted once at the
         layout root so it survives route changes. -->
    <AgentToast />

    <!-- Programmatic toast system: success / error / info / warning.
         Mounted once; pages fire via: import { toast } from '$lib/data/toastStore.svelte.js'
         z-index 80 — below AgentToast (9997) and modals (9998). -->
    <ToastContainer />

    <footer class="algo-footer">
      <span class="algo-footer-text">RamboQuant Analytics</span>
      <span class="algo-footer-sep">·</span>
      <span class="algo-footer-text">{pageLabel}</span>
      <span class="algo-footer-sep">·</span>
      <span class="algo-footer-text">
        Built by
        <a class="algo-footer-link"
           href="https://ramanaambore.me" target="_blank" rel="noopener">Ramana Ambore</a>
      </span>
    </footer>
  </div>
</div>

<style>
  /* ── Algo viewport ─────────────────────────────────────────────────────── */
  /* 100dvh (dynamic viewport height) follows the actual visible
     area on mobile — adjusts on rotation, URL bar show/hide,
     keyboard open. Plain 100vh caches the prior orientation's
     viewport on iOS Safari which leaves a phantom whitespace
     strip at the bottom after rotating portrait → landscape →
     portrait. Falls back to 100vh on older browsers via the
     stacked declaration. */
  .algo-viewport {
    min-height: 100vh;
    min-height: 100dvh;
    background-color: var(--algo-bg-base);
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  /* Force the html + body background to the algo dark navy on every
     page that mounts this layout. Without this, the public-site
     cream (`body { background-color: #f0ece3 }` in app.css) shows
     through whenever the .algo-viewport doesn't fully cover the
     paint area — iOS Safari rubber-band overscroll at the top or
     bottom of the page, browser bottom-nav reveal during scroll,
     wide-margin desktop layouts. Mobile reliably hit the "white
     background under the card" symptom because scroll-bounce
     exposes the body underneath the dark .algo-viewport. */
  :global(html), :global(body) {
    background-color: #080f1c;
  }
  /* `overscroll-behavior: contain` still stops the iOS Safari bounce
     from exposing the body background on edge pulls AND prevents inner
     scroll containers from chaining their scroll up to the document —
     but DOES NOT disable Chrome's mobile pull-to-refresh gesture (which
     the previous `none` value also killed). Operator reported losing
     pull-to-refresh on mobile; `contain` keeps the original bounce
     suppression without taking the gesture with it. */
  :global(html) {
    overscroll-behavior: contain;
  }

  /* No max-width: let the card consume the whole viewport so the
     grids / log panel / simulator don't leave dead gutters on wide
     monitors. Borders dropped because there's nothing to separate
     from anymore. */
  .algo-card {
    width: 100%;
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    background-color: var(--algo-bg-elev2);
    /* app.css defines a component-card .algo-card with padding:
       0.55rem 0.7rem (for health / settings content cards).  This
       outer page-wrapper div shares the same class name but must
       have zero padding — its children (navbar, algo-content,
       footer) do their own internal spacing. */
    padding: 0;
  }

  /* ── Navbar ─────────────────────────────────────────────────────────────── */
  /* Position: fixed (was sticky). Operator: "navbar is not fixed at
     the top of the viewport. it is moving during scroll." Sticky was
     becoming detached from viewport top in some scroll contexts (likely
     a flex containing block edge case in Chrome on mobile). Fixed is
     unambiguous: pinned to viewport regardless of ancestor setup. */
  .algo-navbar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 50;
    background: var(--algo-bg-elev1);
    /* Was border-bottom: 1px solid #fbbf24 (alpha 1.0) — ~2.5× heavier
       than the footer's rgba(251,191,36,0.2) and page-header's 0.30
       border. Slice AT audit fix: matched to canonical
       --algo-amber-border (0.55) for a uniform hairline across all
       chrome bands. */
    border-bottom: 1px solid var(--algo-amber-border);
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
    outline: none;
    height: 100%;
  }
  .algo-brand-name {
    font-size: var(--fs-lg);
    font-weight: 800;
    color: var(--c-action);
    letter-spacing: 0.08em;
    font-family: var(--font-numeric);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
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
    font-size: var(--fs-2xs);
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
    font-size: var(--fs-md);
    font-weight: 500;
    border-radius: 0.2rem;
    background: transparent;
    color: rgba(180, 200, 230, 0.75);
    border: none;
    border-left: 2px solid transparent;
    cursor: pointer;
    letter-spacing: 0.03em;
    font-family: var(--font-numeric);
    transition: background-color 0.06s, color 0.06s, border-left-color 0.06s;
    white-space: nowrap;
    outline: none;
    -webkit-tap-highlight-color: transparent;
  }
  :global(.algo-nav-btn:hover) {
    background: rgba(251,191,36,0.1);
    color: var(--c-action);
    border-left-color: var(--c-action);
  }
  /* Ghost variant — demo Sign In button. De-emphasised so Sign In
     recedes as a secondary affordance. */
  :global(.algo-nav-btn.algo-nav-btn-ghost) {
    color: rgba(180, 200, 230, 0.40);
    font-weight: 400;
  }
  :global(.algo-nav-btn.algo-nav-btn-ghost:hover) {
    background: rgba(180, 200, 230, 0.06);
    color: rgba(180, 200, 230, 0.75);
    border-left-color: transparent;
  }
  :global(.algo-nav-btn-active) {
    background: rgba(251,191,36,0.15);
    color: var(--c-action);
    font-weight: 700;
    border-left-color: var(--c-action);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 800;
    letter-spacing: 0.10em;
    line-height: 1;
    flex: 0 0 auto;
    margin-right: 0.35rem;
    border: 1.5px solid currentColor;
  }
  /* LIVE gets a halo so it reads as the urgent / pay-attention pill
     even at a glance — matches the live-banner-pulse rhythm. */
  .algo-mode-badge[data-mode='live'] {
    box-shadow: 0 0 8px rgba(248, 113, 113, 0.45);
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
  .algo-mode-demo   { color: var(--algo-violet); background: rgba(192,132,252,0.10); }
  @keyframes algo-mode-dot {
    0%, 100% { opacity: 1;   transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(0.8); }
  }
  @media (prefers-reduced-motion: reduce) {
    .algo-mode-badge::before { animation: none; }
  }

  .algo-pub-link {
    padding: 0.2rem 0.65rem;
    font-size: var(--fs-md);
    font-weight: 500;
    border-radius: 0.25rem;
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    border: 1px solid rgba(251,191,36,0.32);
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.02em;
    transition: color 0.08s, border-color 0.08s, background-color 0.08s;
    outline: none;
    margin-left: 0.5rem;
    white-space: nowrap;
  }
  .algo-pub-link:hover {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.5);
    color: var(--c-action);
  }

  /* User pill */
  .algo-user-pill {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--fs-md);
    font-weight: 500;
    color: var(--algo-slate);
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(255,255,255,0.1);
    margin-right: 0.25rem;
    white-space: nowrap;
    font-family: var(--font-numeric);
  }
  .algo-user-role {
    font-size: var(--fs-2xs);
    color: var(--c-action);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  /* Designated tier — violet, matches the DESIGNATED badge in /admin. */
  .algo-user-role.algo-user-role-designated { color: #c084fc; }
  /* Admin tier — amber-400, canonical config/ops colour. */
  .algo-user-role.algo-user-role-admin { color: var(--c-action); }
  /* Trader — green-400 (BUY/long convention, active-trading role). */
  .algo-user-role.algo-user-role-trader { color: var(--c-long); }
  /* Risk — amber-400 at slightly reduced alpha to distinguish from admin
     in dense contexts; same hue signals "caution/oversight" tier. */
  .algo-user-role.algo-user-role-risk { color: rgba(251,191,36,0.75); }
  /* Partner (LP/investor) — muted green to distinguish from full trader. */
  .algo-user-role.algo-user-role-partner { color: rgba(74,222,128,0.6); }

  /* ── Desktop disclosure dropdowns (Build / Config groups) ──────── */
  .algo-group-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .algo-group-trigger {
    display: inline-flex !important;
    align-items: center;
    gap: 0.25rem;
  }
  .algo-group-caret {
    opacity: 0.7;
    transition: transform 0.1s;
  }
  .algo-group-wrap > .algo-group-trigger[aria-expanded="true"] .algo-group-caret {
    transform: rotate(180deg);
  }
  /* Full-viewport click-catch — same pattern as the mode-combo so a
     click anywhere outside the panel closes it. z-index sits below
     the panel itself so clicks ON the panel still register. */
  .algo-group-overlay {
    position: fixed;
    inset: 0;
    z-index: 48;
    background: transparent;
    cursor: default;
  }
  .algo-group-panel {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 49;
    margin-top: 0.3rem;
    min-width: 8rem;
    padding: 0.3rem;
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border: 1px solid rgba(251,191,36,0.32);
    border-radius: 0.3rem;
    box-shadow: 0 10px 24px rgba(0,0,0,0.65);
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
  }
  .algo-group-item {
    padding: 0.32rem 0.7rem;
    font-size: var(--fs-md);
    font-weight: 500;
    color: rgba(180,200,230,0.85);
    background: transparent;
    border: none;
    border-radius: 0.2rem;
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
    text-align: left;
    white-space: nowrap;
    outline: none;
    transition: background-color 0.06s, color 0.06s;
  }
  .algo-group-item:hover {
    background: rgba(251,191,36,0.12);
    color: var(--c-action);
  }
  .algo-group-item-active {
    background: rgba(251,191,36,0.18);
    color: var(--c-action);
    font-weight: 700;
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
    outline: none;
  }
  .algo-hamburger:hover { background: rgba(251,191,36,0.12); }

  /* Mobile dropdown */
  .algo-mobile-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    z-index: 49;
    background: var(--algo-bg-elev1);
    border-top: 1px solid var(--algo-amber-border);
    border-bottom: 1px solid var(--algo-amber-border);
    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
    /* Cap the drawer height so a many-item nav (Monitor + Analyze +
       Modes + Build + Config + 30+ entries) doesn't extend past the
       viewport with the last items unreachable. The trailing
       padding-bottom adds a safe area inset so the last item clears
       the iOS home indicator. */
    max-height: calc(100dvh - 3rem - 1rem);
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior: contain;
    padding-bottom: calc(env(safe-area-inset-bottom, 0.5rem) + 0.5rem);
  }
  .algo-mobile-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.65rem 1.25rem;
    font-size: var(--fs-xl);
    font-weight: 500;
    color: rgba(180,200,230,0.8);
    background: transparent;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.05);
    cursor: pointer;
    font-family: var(--font-numeric);
    transition: background-color 0.05s;
    outline: none;
  }
  .algo-mobile-item:last-child { border-bottom: none; }
  .algo-mobile-item:hover { background: rgba(251,191,36,0.1); color: var(--c-action); }
  .algo-mobile-active { color: var(--c-action); background: rgba(251,191,36,0.1); }

  /* Group caption inside the mobile drawer — small all-caps label that
     marks each section (Monitor / Analyze / Modes / Build / Config).
     Subtle amber so it reads as a header, not a clickable item. */
  .algo-mobile-group-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    color: rgba(251,191,36,0.55);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    padding: 0.55rem 1.25rem 0.2rem;
    background: rgba(0,0,0,0.18);
    border-top: 1px solid rgba(255,255,255,0.04);
  }
  .algo-mobile-group-label + .algo-mobile-item { border-top: none; }
  .algo-mobile-dropdown > .algo-mobile-group-label:first-child {
    border-top: none;
  }
  /* Investor-site cross-link inside the mobile menu — amber-pill
     emphasis matching the desktop button. Separated from the regular
     mobile items by a thin top border so it reads as a context-switch
     row, not another tab. Symmetric with the public-side mobile
     menu's gold-pill algo-site link. */
  .algo-mobile-site {
    color: var(--c-action);
    font-size: var(--fs-lg);
    font-weight: 500;
    background: rgba(251,191,36,0.10);
    border-top: 1px solid rgba(251,191,36,0.32);
    margin-top: 0.3rem;
    padding-top: 0.55rem;
    letter-spacing: 0.02em;
  }

  /* Banner CSS retired with the mode-banner removal. The navbar
     mode pill is the single mode indicator now. Operator: "remove
     banner for every mode including shadow". */

  /* ── Content ─────────────────────────────────────────────────────────────── */
  .algo-content {
    flex: 1;
    /* Top padding = navbar (3rem = 48px) + page-header strip
       (~2.3rem: 2rem min-height + 0.3rem padding + 1px border).
       The :has(.ps-strip) override below adds the strip's 1.5rem.
       Bottom padding = footer (1.6rem) + safety (0.4rem).

       Both navbar and footer are now fixed-position so content
       needs explicit clearance. Horizontal padding 0.5rem aligns
       the content edge with the navbar/footer inner padding.

       Operator: "page heading is overlapping content in all the
       pages. pinned text is partially hidden. order status cards
       are partially hidden." — prior 1.8rem under-counted the
       page-header height by ~0.5rem and didn't include the
       ps-strip at all. */
    padding: calc(3rem + 1.8rem) 0.5rem calc(1.6rem + 0.4rem);
    color: var(--algo-slate);
    /* flex column so descendant containers (e.g. orders page's
       `.oc-page-wrap`) can use `flex: 1` to fill the actual
       available space INSIDE algo-content's padded content box.
       Without this, descendants that need finite height for
       their inner flex chains were resorting to fixed
       `calc(100vh - Nrem)` calculations — which under-counted
       algo-content's own padding and over-counted by Nrem,
       making the wrap extend below algo-card's 100vh into the
       body's default-white background AND visually shoving the
       footer "on top of" the wrap's last few rows. */
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  /* algo-content padding-top is always EXACTLY the page-header
     strip height (1.8rem). The ps-strip is sticky-IN-FLOW so it
     pushes .algo-content's natural top edge down — no need to
     double-count it in padding-top. Only the page-header `top`
     offset (rule below) needs to grow when the ps-strip is on. */

  /* Charts page: zero horizontal padding so the chart card
     spans edge-to-edge. The page-header is position:fixed (left:0,
     right:0) so it already fills the viewport regardless. Only the
     content box below the header needs the padding removed.
     :global() wrapper suppresses the svelte-check "unused selector"
     warning that fires because .charts-page-wrap is defined in the
     child route component, invisible to layout-scoped CSS analysis. */
  :global(.algo-content:has(.charts-page-wrap)) {
    padding-left: 0;
    padding-right: 0;
  }

  /* ── Footer ─────────────────────────────────────────────────────────────── */
  .algo-footer {
    /* Fixed-bottom — matches the navbar's fixed pattern. Sticky was
       drifting on some scroll contexts; fixed guarantees the footer
       stays on the viewport bottom edge. Padding matches .algo-nav-inner
       (0 0.5rem) so footer left/right edges align with the navbar. */
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 40;
    background: var(--algo-bg-elev1);
    /* Slice AT audit fix: was rgba(251,191,36,0.2), now matches the
       navbar's --algo-amber-border so all three chrome bands share
       the same hairline weight. */
    border-top: 1px solid var(--algo-amber-border);
    height: 1.6rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    padding: 0 0.5rem;
  }
  .algo-footer-text { font-size: var(--fs-sm); color: rgba(160,185,220,0.7); font-family: var(--font-numeric); }
  .algo-footer-sep  { font-size: var(--fs-sm); color: rgba(251,191,36,0.6); margin: 0 0.4rem; }
  .algo-footer-link {
    color: rgba(251,191,36,0.85);
    text-decoration: none;
    border-bottom: 1px dotted rgba(251,191,36,0.45);
  }
  .algo-footer-link:hover { color: var(--c-action); border-bottom-color: var(--c-action); }

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
    background: var(--c-action);
    background-clip: padding-box;
  }

  /* Page-top header row — H1 on the left, timestamp right-aligned on
     the same line to conserve vertical space. Wraps to its own line on
     narrow widths. Used by every admin page via `.page-header`. */
  :global(.page-header) {
    display: flex;
    /* align-items: baseline pushed tall buttons (1.4-1.6rem) above
       the visible strip on some viewports because the buttons'
       baseline = their bottom edge, so they extended UP from the
       text baseline. center-aligned keeps every child inside the
       strip box reliably. */
    align-items: center;
    justify-content: space-between;
    /* Tight gap so title chip + timestamp + action cluster stay on
       one row on mobile. flex-wrap still kicks in only when the row
       genuinely overflows. */
    gap: 0.15rem;
    flex-wrap: wrap;
    margin-bottom: 0;
    /* Operator: "i don't want header row to scroll in all the
       pages". Fixed at top:3rem, full viewport width, so the
       title chip + timestamp + Refresh/Order/Chart/Log icons
       never scroll regardless of how the page wraps its content
       (sticky was breaking inside pages that used
       overflow:hidden / transform containers). z-index 45 sits
       below the navbar (50) and below the sim/paper banner
       stickies (49) — the :has() rules below shift the header
       DOWN to accommodate visible banners so there's no overlap. */
    position: fixed;
    top: 3rem;
    left: 0;
    right: 0;
    z-index: 45;
    background: #0a1020;
    /* Symmetric tight padding — operator: "header row is getting
       more space at the bottom of the row within the row".
       Breathing room between strip and content lives OUTSIDE the
       strip (in .algo-content's padding-top below), not inside. */
    padding: 0.1rem 0.65rem;
    min-height: 1.8rem;
    box-sizing: border-box;
    overflow: visible;
    border-bottom: 1px solid var(--algo-amber-border-soft);
  }
  /* Strip-aware vertical offset — only the ps-strip
     (PositionStrip, 1.5rem) can sit between the navbar and
     .algo-content now that the mode banners are gone. */
  :global(.algo-viewport:has(.ps-strip) .page-header) {
    top: calc(3rem + 1.5rem);  /* 72px — flush with ps-strip's sticky bottom */
  }
  /* When ps-strip is present, content also needs to clear it on top
     of clearing the page-header. Without this override the first
     card / Pinned grid / order status row slid under page-header.
     Slice fix for the operator's "pinned text is partially hidden /
     order status cards are partially hidden" report. */
  :global(.algo-viewport:has(.ps-strip)) .algo-content {
    padding-top: calc(3rem + 1.5rem + 1.8rem);  /* 100.8px */
  }
  /* Page-header timestamp — leaves only a hair before the bells (operator
     feedback: gap was pushing the agent icon to a second line on mobile)
     but takes a small left-margin so the title chip and timestamp aren't
     crowded against each other. */
  :global(.page-header .algo-ts) {
    margin-left: 0.5rem;
    margin-right: 0.15rem;
  }
  /* Title chip cluster on the LEFT of every algo page header. Without
     this, flex-wrap separates them onto two lines on narrow viewports.
     Shared so per-page declarations like .pulse-title-group /
     .opt-title-group can be retired. */
  :global(.page-header .algo-title-group) {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  /* Action icon cluster on the RIGHT — RefreshButton + PageHeaderActions
     (+ any page-specific buttons). Keeps all icons on the same flex line
     even when the title + timestamp wrap to the first row on narrow
     viewports. flex-shrink: 0 prevents the span from collapsing;
     white-space: nowrap keeps the inner buttons together. */
  :global(.page-header-actions) {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    flex-shrink: 0;
    white-space: nowrap;
  }
  :global(.page-header .onb-wrap + .anb-wrap),
  :global(.page-header .algo-ts + .onb-wrap) {
    margin-left: -0.2rem;
  }
  /* When the row genuinely can't fit (mobile-portrait at narrowest)
     keep the bells as a paired unit at the END of whatever line they
     land on, instead of wrapping the agent bell alone. */
  :global(.page-header .onb-wrap),
  :global(.page-header .anb-wrap) {
    flex-shrink: 0;
  }
  /* Every algo page gets the full-width amber underline that dashboard
     already had — keeps the headline visually separated from the
     content cards below without crowding the title chip itself.
     Tighter spacing below the underline so the page content sits
     close to the title (was 0.35rem padding + 1rem margin = ~22px
     of empty zone). */
  /* Fixed-strip style on .page-header above already handles
     border-bottom + margins for every algo page. Override block
     retired with the sticky→fixed switch — the strip looks the
     same on every page without per-page-scoped overrides. */

  /* Page-level timestamp — sky-300 so it sits cleanly next to the
     amber page-title-chip without colour-blending. Matches the algo
     palette's "info" tone (PAPER nav badge, INFO chips). Works inline
     inside .page-header OR stand-alone when a page renders it on its
     own row. */
  :global(.algo-ts) {
    font-size: var(--fs-sm);
    color: #7dd3fc;
    font-family: var(--font-numeric);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  /* Mobile — squeeze the dual-TZ timestamp so the title chip + timestamp
     + RefreshButton + PageHeaderActions trio all fit on the same flex row.
     Smaller font-size + slight negative letter-spacing trims ~20 % width
     without sacrificing legibility on a 360 px viewport. */
  @media (max-width: 640px) {
    :global(.algo-ts) {
      font-size: var(--fs-xs);
      letter-spacing: -0.015em;
    }
    /* Mobile chrome — keep all three sticky bands (navbar / ps-strip /
       footer) at the same 0.25rem horizontal padding so their inner
       content aligns vertically. Drop the algo-content horizontal
       padding to 0 so cards span the full viewport edge to edge —
       operator: "overall mobile is using less viewport width because
       of which there is a lot of wasted space before and after cards
       on mobile". */
    .algo-nav-inner { padding: 0 0.25rem; }
    .algo-footer    { padding: 0 0.25rem; }
    .algo-content   { padding-left: 0; padding-right: 0; }
    /* Tighter card padding on mobile — operator: "increase card width
       to reduce wasted space". algo-status-card defaults to 0.75rem
       all sides; trimming to 0.35rem reclaims ~0.8rem of horizontal
       inner-width per card. Vertical padding tightened similarly so
       the card chrome doesn't waste height either. */
    :global(.algo-status-card) {
      padding: 0.35rem;
    }
    /* Keep 0.7rem left padding so card headings (.mp-section-label,
       .algo-card-title) are at least 11.2px from the viewport edge
       on narrow screens (operator brief: heading touches left edge). */
    :global(.algo-card) {
      padding: 0.35rem 0.4rem 0.35rem 0.7rem;
    }
    :global(.bucket-card) {
      padding: 0.35rem 0.4rem 0.35rem 0.7rem;
    }
    /* Bare page-level section headings (.algo-section-title) sit
       directly in .algo-content (padding-left: 0 on mobile) — give
       them the same 0.7rem left indent as the cards beside them. */
    :global(.algo-section-title) {
      padding-left: 0.7rem;
    }
    /* Tighter page-header on mobile — saves ~0.5rem of vertical
       chrome between the ps-strip and the page's first content
       (operator: "reduce the space between nav strip and pinned text
       in pulse page"). The page-header still holds the title chip +
       timestamp + RefreshButton trio; only the padding + min-height
       shrink. algo-content's padding-top is adjusted in lockstep
       below so the first card sits at the new page-header bottom. */
    :global(.page-header) {
      padding: 0.1rem 0.4rem;
      min-height: 1.8rem;
    }
    /* Page-header is 1.8rem on both desktop + mobile now — icons
       (1.4rem buttons) get 0.2rem breathing room above/below instead
       of touching the strip's top/bottom borders. algo-content
       padding-top stays in lockstep with desktop. */
    .algo-content {
      padding-top: calc(3rem + 1.8rem);
    }
    :global(.algo-viewport:has(.ps-strip)) .algo-content {
      padding-top: calc(3rem + 1.5rem + 1.8rem);
    }
    /* Mobile relaxation: the page-header action cluster has
       `white-space: nowrap` + `flex-shrink: 0` for desktop so the
       buttons stay glued together when there's headroom. On a
       375px viewport the automation page injects two extra pills
       (History + Ask AI) and the cluster's max-content width
       overflows the viewport — `flex-wrap: wrap` on `.page-header`
       can't help because the cluster itself refuses to shrink.
       Allow the buttons to wrap and shrink on narrow viewports so
       the navbar stops being pushed out of frame. */
    :global(.page-header-actions) {
      flex-wrap: wrap;
      white-space: normal;
      flex-shrink: 1;
      min-width: 0;
    }
  }

  /* Algo dark-theme overrides for classes shared with public pages */
  :global(.algo-content .text-muted) { color: var(--algo-muted); }
  :global(.algo-content .field-label) { color: var(--algo-muted); }
  :global(.algo-content .field-input) {
    background: #152033;
    border-color: rgba(255,255,255,0.12);
    color: var(--algo-slate);
    color-scheme: dark;
    accent-color: #fbbf24;
  }
  :global(.algo-content .field-input:focus) { border-color: var(--c-action) !important; }

  /* .field-input sets border-color shorthand which overwrites the amber
     left rule shipped with .cmd-input — restore it inside algo pages so
     the Terminal textarea and any other cmd-input surface keep the
     Bloomberg-style amber accent that the Orders command bar uses. */
  :global(.algo-content .cmd-input) {
    border-left: 3px solid var(--c-action) !important;
  }
  :global(.algo-content .cmd-input:focus) {
    border-left-color: var(--c-action) !important;
  }

  /* .cmd-surface left-accent rule retired — operator reverted the
     universal amber left rule on cards. Class name preserved in
     markup as a no-op layout hook for any local CSS that still
     targets it. */

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
    color: var(--algo-slate);
    padding: 0.35rem 0.5rem;
  }
  :global(.algo-content select.field-input option:checked),
  :global(.algo-content select option:checked) {
    background-color: #273552;
    color: var(--c-action);
    font-weight: 700;
  }
  :global(.algo-content select.field-input option:hover),
  :global(.algo-content select option:hover) {
    background-color: rgba(251,191,36,0.15);
    color: var(--c-action);
  }
  /* section-heading in algo context: align to canonical .algo-card-title
     typography so every card / section header reads with the same amber
     intensity + monospace stack. Operator (2026-07-01): "header text
     color is not consistent. GREEKS is good — make them uniform." */
  :global(.algo-content .section-heading) {
    color: var(--c-action);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    letter-spacing: 0.04em;
  }
  :global(.algo-content .page-title-chip) {
    color: var(--c-action);
    border-bottom: none;
    padding-bottom: 0;
    /* h1 default browser margin (~0.67em top/bottom) inflated the
       page-header flex container above its 1.8rem min-height,
       pushing the strip taller and breaking the matched
       padding-top reservation. Reset to 0 + tight line-height
       so the strip stays exactly 1.8rem. */
    margin: 0;
    line-height: 1.1;
  }
  :global(.algo-content .btn-secondary) {
    color: var(--algo-slate);
    border-color: rgba(255,255,255,0.2);
    background: transparent;
  }
  :global(.algo-content .btn-secondary:hover:not(:disabled)) {
    background: rgba(251,191,36,0.1);
    border-color: rgba(251,191,36,0.5);
    color: var(--c-action);
  }
  :global(.algo-content .btn-tertiary) { color: var(--algo-slate); }
  :global(.algo-content .btn-tertiary:hover) { background: rgba(251,191,36,0.1); color: var(--c-action); }
  :global(.algo-content .btn-tertiary.active) { color: var(--c-action); background: rgba(251,191,36,0.15); }

  /* ── Execution-mode combobox ─────────────────────────────────────────── */
  .mode-combo-wrap {
    position: relative;
    display: inline-flex;
    flex-direction: column;
    align-items: flex-end;
    margin-right: 0.3rem;
    flex-shrink: 0;
  }
  /* Mode trigger — the chip IS the dropdown button. Picks up colour from
     data-mode attribute so it reads as the current mode at a glance.
     Extends .algo-mode-badge (defined above) with cursor + caret. */
  .mode-trigger {
    cursor: pointer;
    padding: 0 0.5rem;
    gap: 0.3rem;
    outline: none;
    transition: filter 0.08s;
  }
  .mode-trigger:hover  { filter: brightness(1.2); }
  .mode-trigger:active { filter: brightness(0.85); }
  .mode-trigger-caret  { flex-shrink: 0; opacity: 0.7; pointer-events: none; }
  /* Per-mode colour override via data-mode attribute so the trigger
     matches the colour scheme previously applied via inline style. */
  .algo-mode-badge[data-mode='idle']   { color:#94a3b8; background:rgba(148,163,184,0.10);  border-color:rgba(148,163,184,0.45); }
  .algo-mode-badge[data-mode='paper']  { color:#7dd3fc; background:rgba(125,211,252,0.10);  border-color:#7dd3fc; }
  /* LIVE: red palette — see MODE_COLOR comment. Heavier fill alpha
     than the safer modes so the pill reads as ALARMED on a glance. */
  .algo-mode-badge[data-mode='live']   { color:var(--c-short); background:var(--c-short-22); border-color:var(--c-short); }
  .algo-mode-badge[data-mode='shadow'] { color:#fb923c; background:rgba(251,146,60,0.10);  border-color:#fb923c; }
  /* SIM uses green — matches REPLAY and CLAUDE.md spec: "SIM/REPLAY green". */
  .algo-mode-badge[data-mode='sim']    { color:var(--c-long); background:var(--c-long-10);  border-color:var(--c-long); }
  .algo-mode-badge[data-mode='replay'] { color:var(--c-long); background:var(--c-long-10);  border-color:var(--c-long); }

  /* Full-viewport invisible overlay so clicking outside closes the dropdown.
     Must sit just below the dropdown itself, but above the order modal
     (z-index 10500) so clicks on the overlay close the dropdown without
     accidentally hitting the modal underneath. */
  .mode-combo-overlay {
    position: fixed;
    inset: 0;
    z-index: 10600;
  }
  /* Dropdown rendered via position:fixed so it escapes the navbar's own
     stacking context (navbar is position:fixed; z-index:50 — any child
     positioned absolutely is trapped inside that context and can never
     paint above the order-modal overlay at z-index:10500).
     Coordinates (top / right) are injected as inline styles computed
     from the trigger's getBoundingClientRect() when the dropdown opens. */
  .mode-combo-dropdown {
    position: fixed;
    z-index: 10601;
    background: #0a1020;
    border: 1px solid rgba(251, 191, 36, 0.25);
    border-radius: 6px;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.55);
    list-style: none;
    margin: 0;
    padding: 0.2rem 0;
    min-width: 7rem;
  }
  .mode-combo-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.28rem 0.65rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 600;
    letter-spacing: 0.07em;
    color: var(--mc, #94a3b8);
    background: transparent;
    border: none;
    cursor: pointer;
    transition: background-color 0.06s;
    outline: none;
  }
  .mode-combo-item:hover { background: rgba(255,255,255,0.07); }
  .mode-combo-item-active {
    background: rgba(255,255,255,0.05);
    font-weight: 800;
  }
  .mode-combo-error {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    background: rgba(248,113,113,0.12);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--c-short);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    white-space: nowrap;
    z-index: 61;
  }

  /* ── Broker connectivity chip (slice AX) ─────────────────────────
     Ambient navbar chip showing N/M brokers loaded. Green = all
     loaded, amber = partial, red = none, slate = API unreachable.
     Same pill shape as .chase-chip; click → /admin/health.
     Operator: "ambient broker-status indicator" — IBKR equivalent. */
  .broker-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    height: 1.4rem;
    padding: 0 0.55rem;
    border-radius: 9999px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.07em;
    cursor: pointer;
    white-space: nowrap;
    outline: none;
    margin-right: 0.3rem;
    transition: background-color 0.08s, filter 0.08s;
  }
  .broker-chip:hover { filter: brightness(1.15); }
  .broker-chip-dot {
    width: 0.4rem;
    height: 0.4rem;
    border-radius: 50%;
    background: currentColor;
  }
  .broker-chip-ok {
    color: var(--c-long);
    background: rgba(74, 222, 128, 0.12);
    border: 1px solid rgba(74, 222, 128, 0.5);
  }
  .broker-chip-partial {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.5);
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .broker-chip-down {
    color: var(--c-short);
    background: rgba(248, 113, 113, 0.14);
    border: 1px solid rgba(248, 113, 113, 0.55);
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .broker-chip-unknown {
    color: #94a3b8;
    background: rgba(148, 163, 184, 0.10);
    border: 1px solid rgba(148, 163, 184, 0.40);
  }
  @media (prefers-reduced-motion: reduce) {
    .broker-chip-partial, .broker-chip-down { animation: none; }
    .chase-chip  { animation: none; }
    .persist-chip { animation: none; }
  }

  /* ── Chase chip ──────────────────────────────────────────────────────── */
  .chase-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    height: 1.4rem;
    padding: 0 0.55rem;
    border-radius: 9999px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.07em;
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.5);
    cursor: pointer;
    white-space: nowrap;
    outline: none;
    margin-right: 0.3rem;
    transition: background-color 0.08s, filter 0.08s;
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .chase-chip:hover {
    background: var(--algo-amber-bg-strong);
    filter: brightness(1.1);
  }
  .chase-chip-arrow { opacity: 0.7; }

  /* ── Persistence bypass chip (slice AK) ───────────────────────────
     Same chip-shape as .chase-chip but with mode-specific colours so
     SOFT (amber, recoverable) and HARD (red, includes ticker recycle)
     read at a glance. Click flips back to OFF — the operator's
     "I forgot I was in bypass" recovery affordance. */
  .persist-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    height: 1.4rem;
    padding: 0 0.55rem;
    border-radius: 9999px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.07em;
    cursor: pointer;
    white-space: nowrap;
    outline: none;
    margin-right: 0.3rem;
    transition: background-color 0.08s, filter 0.08s;
    animation: algo-mode-dot 2s ease-in-out infinite;
  }
  .persist-chip:hover { filter: brightness(1.15); }
  .persist-chip-soft {
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.5);
  }
  .persist-chip-hard {
    color: var(--c-short);
    background: rgba(248, 113, 113, 0.14);
    border: 1px solid rgba(248, 113, 113, 0.55);
  }

  /* ── Status-driven surface card — used across algo pages ───────────────────
     Operator: "make accent consistent with colors for cards in all
     pages". Every card now carries the same 3px amber left-border
     accent by default. Status variants override `border-left-color`
     (NOT `border-left` shorthand) so the left rule keeps its 3px
     width but recolours to match the status tint — active green,
     triggered red, etc. Non-status cards keep the canonical amber
     ascent. Pages that previously added `.cmd-surface` for an amber
     left rule now get it for free; the per-page custom accents
     (`.opt-trade-surface`, `.brokers-h`, …) inherit a single
     visual rhythm. */
  :global(.algo-status-card) {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    padding: 0.75rem;
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.08);
    color: var(--algo-slate);
    transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  }
  /* Status-coded card chrome retired. Agent cards already carry a
     colored status dot in their title row (line 696 of /automation
     +page.svelte: <span class="w-2 h-2 rounded-full {statusDot}">),
     plus an ON/OFF pill on the right and an animate-pulse class on
     triggered rows. The frame ring + colored left rule duplicated
     the same info on a more cluttered surface. Inactive gets opacity
     dim only — keeps the row scannable as "this one's idle" without
     repainting the chrome. */
  :global(.algo-status-card[data-status="inactive"]) {
    opacity: 0.82;
  }
  :global(.algo-status-card[data-status="running"]) {
    border-color: rgba(251,191,36,0.65);
    border-left-color: var(--c-action);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(251,191,36,0.18);
  }
  :global(.algo-status-card[data-status="cooldown"]) {
    border-color: rgba(251,191,36,0.4);
    border-left-color: rgba(251,191,36,0.75);
  }
  :global(.algo-status-card[data-status="error"]) {
    border-color: rgba(248,113,113,0.85);
    border-left-color: var(--c-short);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45), 0 0 0 1px rgba(248,113,113,0.28);
  }

  /* ── View-transition cross-fade (slice AY, item 23) ─────────────────
     Fires when document.startViewTransition() is available. 120ms
     opacity sweep replaces the hard content swap; invisible under
     prefers-reduced-motion because the animation-duration is zeroed.
     Duration is intentionally short — this is page navigation, not
     a cinematic entrance. The :global selector is required because
     these pseudo-elements live outside any Svelte component scope. */
  @media (prefers-reduced-motion: no-preference) {
    :global(::view-transition-old(root)),
    :global(::view-transition-new(root)) {
      animation-duration: 120ms;
      animation-timing-function: ease-out;
    }
  }

  /* ── CardHeader theming — algo dark scheme ───────────────────────────
     Matches the canonical .card-header-row / .mp-bucket-head idiom:
     bare flex row, no bg, no border, no padding — only a gap between
     items. Title typography follows .algo-card-title exactly (amber
     #fbbf24, monospace, uppercase, 0.04em tracking, 700 weight, 0.6rem).
     Timestamp uses the canonical muted secondary color. */
  :global(body) {
    --ch-bg: transparent;
    --ch-border-bottom: none;
    --ch-padding: 0;
    --ch-gap: 0.4rem;
    --ch-title-font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --ch-title-size: 0.6rem;
    --ch-title-weight: 700;
    --ch-title-color: #fbbf24;
    --ch-title-letter-spacing: 0.04em;
    --ch-title-transform: uppercase;
    --ch-ts-size: 0.65rem;
    --ch-ts-color: #7e97b8;
  }

  /* Demo banner — in normal document flow, first child of .algo-content.
     Scrolls with content; no fixed positioning needed. */
  .demo-banner {
    z-index: 10;
    height: 2rem;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0 0.75rem;
    background: rgba(15, 23, 42, 0.97);
    border-bottom: 1px solid rgba(126, 151, 184, 0.30);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
  }
  .demo-banner-text { color: rgba(148, 163, 184, 0.85); flex: 1; }
  .demo-banner-text strong { color: var(--c-action); font-weight: 700; }
  .demo-banner-close {
    flex-shrink: 0;
    background: none;
    border: none;
    color: rgba(148, 163, 184, 0.50);
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    padding: 0 0.15rem;
    transition: color 0.1s;
  }
  .demo-banner-close:hover { color: rgba(148, 163, 184, 0.90); }
</style>
