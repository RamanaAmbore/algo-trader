<script>
  /**
   * NavigationIndicator — thin top-bar progress strip for SvelteKit
   * route transitions.
   *
   * Mount once in each layout root (algo + public). When a navigation
   * starts the bar slides in from the left in cyan-400 (algo) or
   * champagne-gold (public), animates to ~80%, then completes and
   * fades out on afterNavigate.
   *
   * Design notes:
   *  - z-index 9200 — above navbar (50/z-50), modals (9998 is toast),
   *    and the ReconnectingPopup (9100). The indicator must always be
   *    visible over page content during a transition.
   *  - The bar is 3px tall, inset to 0 top so it sits flush under the
   *    pub-accent-top / algo viewport top edge.
   *  - Two CSS custom-property variants so a single component works for
   *    both the algo (cyan) and public (gold) palettes. The caller
   *    passes the `variant` prop.
   *  - Uses `requestAnimationFrame` to move the "indeterminate" phase
   *    from 0→80% smoothly, then a CSS transition handles the final
   *    100% snap on completion.
   *  - The bar never blocks pointer events (pointer-events:none).
   */

  // eslint-disable-next-line no-undef
  let { variant } = $props();
  variant ??= 'algo';

  /** Whether a navigation is currently in progress. */
  let _active = $state(false);

  /** Current bar width as a percentage string (e.g. '72%'). */
  let _width = $state('0%');

  /** CSS opacity for the fade-out after complete. */
  let _opacity = $state('0');

  let _rafId = /** @type {number | null} */ (null);
  let _completeTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);

  /**
   * Called by the parent layout's onNavigate hook.
   * Shows the bar and animates it to ~80%.
   */
  export function start() {
    // Cancel any in-progress completion animation.
    if (_rafId != null) { cancelAnimationFrame(_rafId); _rafId = null; }
    if (_completeTimer != null) { clearTimeout(_completeTimer); _completeTimer = null; }

    _active  = true;
    _opacity = '1';
    _width   = '0%';

    // Next frame: jump to 15% instantly so the bar is immediately
    // visible (zero width is invisible). Then animate to ~82% via CSS
    // transition — gives a "loading" feel without knowing the real
    // load time.
    _rafId = requestAnimationFrame(() => {
      _width = '15%';
      _rafId = requestAnimationFrame(() => {
        _width   = '82%';
        _rafId   = null;
      });
    });
  }

  /**
   * Called by the parent layout's afterNavigate hook.
   * Completes the bar to 100% then fades it out.
   */
  export function complete() {
    if (_rafId != null) { cancelAnimationFrame(_rafId); _rafId = null; }
    // Snap to 100% on the next frame (CSS transition picks it up).
    _rafId = requestAnimationFrame(() => {
      _width = '100%';
      _rafId = null;
      // After the transition finishes (~250ms), fade out.
      _completeTimer = setTimeout(() => {
        _opacity = '0';
        // After opacity fades (~200ms), reset to hidden.
        _completeTimer = setTimeout(() => {
          _active  = false;
          _width   = '0%';
          _completeTimer = null;
        }, 220);
      }, 260);
    });
  }
</script>

{#if _active}
  <div
    class="nav-indicator nav-indicator-{variant}"
    style="width:{_width}; opacity:{_opacity};"
    aria-hidden="true"
  ></div>
{/if}

<style>
  .nav-indicator {
    position: fixed;
    top: 0;
    left: 0;
    height: 3px;
    /* Start from 0, animated by JS writes to style.width.
       The 0→15% jump is instant (first RAF); 15→82% transitions
       over 800ms so the bar smoothly "loads"; 82→100% transitions
       over 200ms for the snap-to-complete feel. */
    transition: width 0.8s cubic-bezier(0.2, 0.8, 0.4, 1),
                opacity 0.2s ease;
    pointer-events: none;
    z-index: 9200;
    border-radius: 0 2px 2px 0;
    /* Reduce-motion: cut the transition, keep the instant paint. */
  }

  /* Algo variant — canonical cyan-400 (#22d3ee) with a right-edge
     glow matching the platform's action palette. */
  .nav-indicator-algo {
    background: linear-gradient(
      90deg,
      var(--c-info) 0%,
      #67e8f9 85%,
      rgba(103, 232, 249, 0.6) 100%
    );
    box-shadow: 0 0 8px rgba(34, 211, 238, 0.6),
                0 0 2px rgba(34, 211, 238, 0.9);
  }

  /* Public variant — champagne gold matching the public site border. */
  .nav-indicator-pub {
    background: linear-gradient(
      90deg,
      #c8a84b 0%,
      #f0d878 80%,
      rgba(240, 216, 120, 0.5) 100%
    );
    box-shadow: 0 0 8px rgba(200, 168, 75, 0.6),
                0 0 2px rgba(200, 168, 75, 0.9);
  }

  @media (prefers-reduced-motion: reduce) {
    .nav-indicator {
      transition: none;
    }
  }
</style>
