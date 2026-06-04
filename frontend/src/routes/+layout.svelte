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

    // 3. Fall back to the page.
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
    target.scrollBy({ top: dy, left: dx, behavior: 'smooth' });
    e.preventDefault();
  }

  onMount(() => { window.addEventListener('keydown', onKey, { passive: false }); });
  onDestroy(() => { window.removeEventListener('keydown', onKey); });
</script>

{@render children()}
