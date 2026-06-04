<script>
  import '../app.css';
  import { onMount, onDestroy } from 'svelte';

  const { children } = $props();

  // ── TV remote scroll handler ─────────────────────────────────────
  // Google TV / Android TV / Amazon Fire Stick browsers send the D-pad
  // as KeyboardEvents but the default browser scroll only fires when
  // document.scrollingElement / body has focus. After a route change,
  // a modal close, or when the hamburger drawer is open, focus often
  // lands on a button or nowhere — arrow keys go nowhere unless we
  // intercept and forward them to the right scroll container.
  //
  // Priority order for the scroll target:
  //   1. Any visible overlay / drawer / popup with overflow:
  //      .canonical-modal-overlay, .oes-overlay, .search-overlay,
  //      .alm-overlay, .algo-mobile-dropdown (hamburger), any
  //      element matching [data-tv-scroll-container].
  //   2. The nearest scrollable ancestor of document.activeElement.
  //   3. document.scrollingElement (page scroll).
  //
  // No-op when the active element is an editable form field,
  // contentEditable, role=listbox/menu/option/menuitem, has
  // data-tv-handles-keys, or sits inside an ag-Grid — those components
  // own their own arrow-key handling.

  // Some TV browsers (older WebOS / Tizen / certain Fire OS builds)
  // send legacy key names — accept both modern and legacy forms.
  const _UP    = new Set(['ArrowUp', 'Up']);
  const _DOWN  = new Set(['ArrowDown', 'Down']);
  const _LEFT  = new Set(['ArrowLeft', 'Left']);
  const _RIGHT = new Set(['ArrowRight', 'Right']);
  const _PGUP  = new Set(['PageUp', 'Prior']);
  const _PGDN  = new Set(['PageDown', 'Next']);
  const _HOME  = new Set(['Home']);
  const _END   = new Set(['End']);
  const _ALL_KEYS = new Set([
    ..._UP, ..._DOWN, ..._LEFT, ..._RIGHT,
    ..._PGUP, ..._PGDN, ..._HOME, ..._END,
  ]);

  // Selectors that mark a popup / drawer / overlay whose body should
  // be scrolled when the operator hits arrow keys.
  const OVERLAY_SELECTORS = [
    '.canonical-modal-overlay',
    '.oes-overlay',
    '.search-overlay',
    '.alm-overlay',
    '.cm-overlay',
    '.algo-mobile-dropdown',
    '[data-tv-scroll-container]',
  ].join(',');

  // Inside an overlay, the actual scroll container is usually one of
  // these — pick the first one that overflows.
  const OVERLAY_BODY_SELECTORS = [
    '.cm-body',
    '.alm-body',
    '.oes-body',
    '.search-modal',
    '.search-body',
    '.canonical-modal-panel',
    '.algo-mobile-dropdown',
  ].join(',');

  /** @param {Element|null} el */
  function _isOverflowing(el) {
    if (!el) return false;
    const h = el.scrollHeight - el.clientHeight;
    return h > 4;
  }

  /** Find the nearest scrollable ancestor of the active element.
   *  Stops at body. Returns null when nothing scrolls along the way. */
  function _nearestScrollAncestor(/** @type {Element|null} */ el) {
    let cur = el?.parentElement || null;
    while (cur && cur !== document.body) {
      if (_isOverflowing(cur)) {
        const cs = window.getComputedStyle(cur);
        if (/(auto|scroll)/.test(cs.overflowY) || /(auto|scroll)/.test(cs.overflow)) return cur;
      }
      cur = cur.parentElement;
    }
    return null;
  }

  /** Find the largest visible overflowing element on the page. Used as
   *  the last-resort scroll target when nothing is focused and no
   *  overlay is open — picks the card the operator is most likely
   *  looking at by visible area. Skips the body (handled separately as
   *  the explicit page fallback) and elements smaller than ~120px tall
   *  to avoid latching onto tiny chip strips. */
  function _largestVisibleScrollable() {
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    let best = null;
    let bestArea = 0;
    const all = document.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      const el = /** @type {HTMLElement} */ (all[i]);
      if (el === document.body || el === document.documentElement) continue;
      if (!_isOverflowing(el)) continue;
      const cs = window.getComputedStyle(el);
      const ovY = cs.overflowY, ov = cs.overflow;
      if (!/(auto|scroll)/.test(ovY) && !/(auto|scroll)/.test(ov)) continue;
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.height < 120) continue;
      // Intersection with the viewport.
      const visW = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
      const visH = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
      const area = visW * visH;
      if (area > bestArea) { bestArea = area; best = el; }
    }
    return best;
  }

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    if (!_ALL_KEYS.has(e.key)) return;
    // A component (Select / MultiSelect / custom listbox) called
    // preventDefault — it's handling the key itself.
    if (e.defaultPrevented) return;

    const ae = /** @type {HTMLElement|null} */ (document.activeElement);
    if (ae) {
      const tag = ae.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (ae.isContentEditable) return;
      if (ae.matches('[role="listbox"], [role="menu"], [role="menuitem"], [role="option"], [data-tv-handles-keys]')) return;
      if (ae.closest('[role="listbox"], [role="menu"], [data-tv-handles-keys], .ag-root-wrapper')) return;
    }

    // 1. Find the topmost visible overlay / drawer with overflow.
    /** @type {HTMLElement|null} */
    let target = null;
    const overlays = Array.from(document.querySelectorAll(OVERLAY_SELECTORS));
    // Reverse-iterate so the most recently-mounted overlay wins (the
    // operator's focus is on top of the stack).
    for (let i = overlays.length - 1; i >= 0; i--) {
      const ov = /** @type {HTMLElement} */ (overlays[i]);
      // Skip hidden overlays — they're still in the DOM but not active.
      const cs = window.getComputedStyle(ov);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
      // Pick the first inner candidate that overflows; fall back to
      // the overlay itself (drawer panels are their own scroll
      // container).
      const candidates = ov.matches(OVERLAY_BODY_SELECTORS)
        ? [ov]
        : Array.from(ov.querySelectorAll(OVERLAY_BODY_SELECTORS));
      const found = candidates.find((c) => _isOverflowing(c))
                 || (_isOverflowing(ov) ? ov : null);
      if (found) { target = /** @type {HTMLElement} */ (found); break; }
    }

    // 2. Nearest scrollable ancestor of the focused element.
    if (!target && ae) target = _nearestScrollAncestor(ae);

    // 3. Largest VISIBLE overflowing card region in the viewport.
    //    Covers per-card scroll surfaces that aren't wrapped in an
    //    overlay (LogPanel pre, OptionChainTab strikes, dashboard
    //    bucket grids, ag-Grid viewports, etc.). Walks every scrollable
    //    element on the page and picks the one with the biggest visible
    //    area — the operator's eye is naturally on the biggest visible
    //    card with content to scroll.
    if (!target) target = _largestVisibleScrollable();

    // 4. Fall back to the page.
    if (!target) target = /** @type {HTMLElement} */ (document.scrollingElement || document.documentElement);
    if (!target) return;

    const step = Math.max(60, Math.round(target.clientHeight * 0.18));
    const page = Math.max(240, Math.round(target.clientHeight * 0.9));

    let dy = 0, dx = 0;
    if      (_UP.has(e.key))    dy = -step;
    else if (_DOWN.has(e.key))  dy =  step;
    else if (_LEFT.has(e.key))  dx = -step;
    else if (_RIGHT.has(e.key)) dx =  step;
    else if (_PGUP.has(e.key))  dy = -page;
    else if (_PGDN.has(e.key))  dy =  page;
    else if (_HOME.has(e.key))  { target.scrollTo({ top: 0, behavior: 'smooth' }); e.preventDefault(); return; }
    else if (_END.has(e.key))   { target.scrollTo({ top: target.scrollHeight, behavior: 'smooth' }); e.preventDefault(); return; }

    if (dy === 0 && dx === 0) return;

    // Edge-bump focus walk — TV remotes have no Tab key, so when the
    // operator hits the top / bottom of a scroll container and presses
    // again, jump focus to the previous / next scrollable region above
    // or below. Natural D-pad use walks the operator between cards
    // without teaching them anything new.
    const atTop    = target.scrollTop <= 1;
    const atBottom = target.scrollTop + target.clientHeight >= target.scrollHeight - 1;
    if (dy < 0 && atTop) {
      const moved = _focusAdjacentScrollable(target, -1);
      if (moved) { e.preventDefault(); return; }
    } else if (dy > 0 && atBottom) {
      const moved = _focusAdjacentScrollable(target, +1);
      if (moved) { e.preventDefault(); return; }
    }

    target.scrollBy({ top: dy, left: dx, behavior: 'smooth' });
    e.preventDefault();
  }

  /** Move focus to the next / previous scrollable container relative
   *  to `current`. Returns true when a target was found + focused. */
  function _focusAdjacentScrollable(/** @type {HTMLElement} */ current, /** @type {1|-1} */ dir) {
    const all = Array.from(document.querySelectorAll(
      '[data-tv-scroll-container], .canonical-modal-panel, .oes-body, .cm-body, .alm-body, .search-modal, .algo-mobile-dropdown'
    )).filter((el) => {
      const cs = window.getComputedStyle(/** @type {HTMLElement} */ (el));
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      return _isOverflowing(/** @type {HTMLElement} */ (el));
    });
    if (!all.includes(current)) return false;
    // Sort by vertical position in viewport so "next" = visually next
    // down, "previous" = visually next up.
    all.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    const idx = all.indexOf(current);
    const next = all[idx + dir];
    if (!next) return false;
    /** @type {HTMLElement} */ (next).focus({ preventScroll: false });
    /** @type {HTMLElement} */ (next).scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    return true;
  }

  // Walk the DOM periodically and assign tabindex="-1" to every
  // scrollable element so _focusAdjacentScrollable() can move focus
  // into them. tabindex="-1" makes elements programmatically focusable
  // without inserting them into the Tab order — D-pad nav still works
  // but the operator never accidentally lands on them via Tab.
  function _assignFocusableScrollables() {
    const all = document.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      const el = /** @type {HTMLElement} */ (all[i]);
      if (el.hasAttribute('tabindex')) continue;
      if (!_isOverflowing(el)) continue;
      const cs = window.getComputedStyle(el);
      if (!/(auto|scroll)/.test(cs.overflowY) && !/(auto|scroll)/.test(cs.overflow)) continue;
      const r = el.getBoundingClientRect();
      if (r.height < 120) continue;
      el.tabIndex = -1;
      el.classList.add('tv-scrollable');
    }
  }

  /** @type {ReturnType<typeof setInterval> | null} */
  let _scrollScanTimer = null;
  // Visible debug chip — top-right corner. Confirms (a) whether the
  // global keydown handler fires AT ALL on the TV browser, and (b)
  // what key code arrives. Toggle off by deleting #tvkdbg or setting
  // sessionStorage.tvkdbg='off'.
  let _dbgKey = $state('');
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _dbgClear = null;
  function _showDbg(/** @type {string} */ text) {
    if (typeof sessionStorage !== 'undefined' && sessionStorage.tvkdbg === 'off') return;
    _dbgKey = text;
    if (_dbgClear) clearTimeout(_dbgClear);
    _dbgClear = setTimeout(() => { _dbgKey = ''; }, 1500);
  }

  // Capture-phase wrapper that runs BEFORE any other key listener on
  // the page. Fire Stick Silk's spatial-navigation mode treats arrow
  // keys as focus-rect moves before passing to JS — this hooks above
  // it so our scroll handler always wins. stopImmediatePropagation
  // prevents the OS / browser from running its own arrow behaviour.
  /** @param {KeyboardEvent} e */
  function onKeyCapture(e) {
    if (!_ALL_KEYS.has(e.key)) return;
    _showDbg(`key=${e.key} code=${e.code}`);
    onKey(e);
    if (e.defaultPrevented) e.stopImmediatePropagation();
  }

  onMount(() => {
    // Capture phase + non-passive so e.preventDefault works.
    window.addEventListener('keydown', onKeyCapture, { capture: true, passive: false });
    _assignFocusableScrollables();
    _scrollScanTimer = setInterval(_assignFocusableScrollables, 2000);
  });
  onDestroy(() => {
    window.removeEventListener('keydown', onKeyCapture, /** @type {any} */ ({ capture: true }));
    if (_scrollScanTimer) clearInterval(_scrollScanTimer);
    if (_dbgClear) clearTimeout(_dbgClear);
  });
</script>

{@render children()}

{#if _dbgKey}
  <div id="tvkdbg"
       style="position:fixed; top:6px; right:6px; z-index:99999;
              background:rgba(0,0,0,0.85); color:#fbbf24; font:11px monospace;
              padding:4px 8px; border:1px solid #fbbf24; border-radius:3px;
              pointer-events:none;">
    {_dbgKey}
  </div>
{/if}
