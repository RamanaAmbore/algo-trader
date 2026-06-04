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

  /** Hit-test the viewport center and walk up to the nearest
   *  scrollable ancestor. This is what TV spatial-nav libraries do
   *  to figure out "what card is the operator looking at". For ag-Grid
   *  specifically this lands on .ag-body-viewport (the actual scroll
   *  container). Returns null when the centre lands on a non-scrolling
   *  element with no scrolling ancestor. */
  function _centerScrollable() {
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const el = document.elementFromPoint(vw / 2, vh / 2);
    if (!el) return null;
    // ag-Grid: prefer the body viewport over any cell — cells don't
    // overflow, the viewport does.
    const agViewport = /** @type {HTMLElement|null} */ (
      el.closest('.ag-body-viewport, .ag-center-cols-viewport') || null
    );
    if (agViewport && _isOverflowing(agViewport)) return agViewport;
    // Walk up looking for the first ancestor with actual overflow.
    return _nearestScrollAncestor(el) || null;
  }

  /** Find the largest visible overflowing element on the page. Used as
   *  the last-resort fallback. */
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

    // 2. Nearest scrollable ancestor of the focused element. Skips
    //    when focus is on the hidden TV sink (it lives at body level
    //    so has no meaningful card ancestor).
    if (!target && ae && ae !== _tvSink) target = _nearestScrollAncestor(ae);

    // 3. Center-of-viewport probe. TV-app spatial-nav libraries solve
    //    this by hit-testing the visible centre — the card the
    //    operator is actually staring at. Walks up from that element
    //    to find the first scrollable ancestor; for ag-Grid that
    //    means landing on .ag-body-viewport directly.
    if (!target) target = _centerScrollable();

    // 4. Largest visible overflowing card region as a last-resort
    //    fallback (some cards extend above / below the centre).
    if (!target) target = _largestVisibleScrollable();

    // 5. Fall back to the page.
    if (!target) target = /** @type {HTMLElement} */ (document.scrollingElement || document.documentElement);
    if (!target) return;
    _showDbg(`scroll → ${target.tagName.toLowerCase()}.${(target.className || '').split(/\s+/).slice(0, 3).join('.')}`);

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
  // Hard-DOM debug banner — bypasses Svelte reactivity. Writes directly
  // to a fixed-position <div> we inject on mount. If we see this update
  // we know JS is at least getting the event; if not, the TV browser is
  // intercepting at the OS level and we need a manifest-based intercept.
  /** @type {HTMLDivElement | null} */
  let _dbgEl = null;
  function _ensureDbg() {
    if (typeof document === 'undefined') return null;
    if (typeof sessionStorage !== 'undefined' && sessionStorage.tvkdbg === 'off') return null;
    if (_dbgEl) return _dbgEl;
    const el = document.createElement('div');
    el.id = 'tvkdbg';
    el.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
      'background:rgba(0,0,0,0.92)', 'color:#fbbf24',
      'font:13px/1.3 monospace', 'padding:6px 10px',
      'border-bottom:2px solid #fbbf24', 'pointer-events:none',
      'text-align:center', 'letter-spacing:0.04em',
    ].join(';');
    el.textContent = 'tv-debug: waiting for key…';
    document.body.appendChild(el);
    _dbgEl = el;
    return el;
  }
  let _dbgCount = 0;
  function _showDbg(/** @type {string} */ text) {
    const el = _ensureDbg();
    if (!el) return;
    _dbgCount++;
    el.textContent = `tv-debug #${_dbgCount}: ${text}`;
  }

  // Listener wrappers — install on BOTH document and window, BOTH
  // capture and bubble. If any of them fires, the chip updates. Each
  // wrapper carries its tag so we know which path the event took.
  /** @param {KeyboardEvent} e */
  function _dbgOnly(e, tag) {
    _showDbg(`${tag} key=${e.key} code=${e.code} kc=${e.keyCode} which=${e.which}`);
  }
  /** @type {Record<string, (e: KeyboardEvent) => void>} */
  const _handlers = {};
  function _installHandlers() {
    if (typeof window === 'undefined') return;
    const variants = [
      ['wcap-down',    window,   'keydown',  true ],
      ['wbub-down',    window,   'keydown',  false],
      ['wcap-press',   window,   'keypress', true ],
      ['wbub-press',   window,   'keypress', false],
      ['wcap-up',      window,   'keyup',    true ],
      ['dcap-down',    document, 'keydown',  true ],
      ['dbub-down',    document, 'keydown',  false],
    ];
    for (const [tag, tgt, ev, cap] of variants) {
      const fn = (/** @type {KeyboardEvent} */ e) => {
        _dbgOnly(e, /** @type {string} */ (tag));
        if (ev === 'keydown' && cap) onKey(e);
      };
      _handlers[/** @type {string} */ (tag)] = fn;
      /** @type {any} */ (tgt).addEventListener(ev, fn, { capture: cap, passive: false });
    }
  }
  function _removeHandlers() {
    if (typeof window === 'undefined') return;
    const variants = [
      ['wcap-down',    window,   'keydown',  true ],
      ['wbub-down',    window,   'keydown',  false],
      ['wcap-press',   window,   'keypress', true ],
      ['wbub-press',   window,   'keypress', false],
      ['wcap-up',      window,   'keyup',    true ],
      ['dcap-down',    document, 'keydown',  true ],
      ['dbub-down',    document, 'keydown',  false],
    ];
    for (const [tag, tgt, ev, cap] of variants) {
      const fn = _handlers[/** @type {string} */ (tag)];
      if (fn) /** @type {any} */ (tgt).removeEventListener(ev, fn, { capture: cap });
    }
  }

  // ── TV-mode key capture via hidden focused input ─────────────────
  // Fire Stick Silk / Android TV Chrome consume D-pad keys at the OS
  // level UNLESS an input/textarea is focused. Injecting an always-
  // focused hidden input forces the browser into keyboard-input mode
  // — arrow / page / home / end keys now fire keydown ON THE INPUT
  // rather than being eaten by spatial navigation. We catch them on
  // the input, preventDefault to suppress caret movement, then route
  // through the scroll handler.
  /** @type {HTMLElement | null} */
  let _tvSink = null;
  function _ensureTvSink() {
    if (typeof document === 'undefined' || _tvSink) return _tvSink;
    // Use a contenteditable DIV (not an <input>) — contenteditable
    // takes focus and fires keydown for arrow keys WITHOUT triggering
    // the on-screen keyboard on Fire Stick / Android TV (the OSK only
    // pops for form inputs). tabindex=-1 makes it programmatically
    // focusable; contenteditable=true lets it receive key events.
    const div = document.createElement('div');
    div.id = '_tvkeysink';
    div.setAttribute('aria-hidden', 'true');
    div.setAttribute('tabindex', '-1');
    div.setAttribute('contenteditable', 'true');
    div.setAttribute('role', 'application');  // explicit "we handle keys"
    div.style.cssText = [
      'position:fixed', 'left:0', 'top:0',
      'width:1px', 'height:1px',
      'opacity:0', 'background:transparent',
      'border:none', 'outline:none',
      'pointer-events:none', 'z-index:-1',
      'caret-color:transparent',
      'user-select:none', '-webkit-user-select:none',
      'overflow:hidden', 'white-space:nowrap',
    ].join(';');
    document.body.appendChild(div);
    div.addEventListener('keydown', (e) => {
      _dbgOnly(e, 'sink-down');
      if (_ALL_KEYS.has(e.key)) {
        e.preventDefault();
        onKey(e);
      }
    }, { capture: true, passive: false });
    // beforeinput captures input mutations BEFORE they apply — kills
    // any accidental text entry on contenteditable.
    div.addEventListener('beforeinput', (e) => {
      e.preventDefault();
    });
    div.addEventListener('blur', () => {
      setTimeout(() => {
        const ae = document.activeElement;
        if (!ae || ae === document.body) div.focus({ preventScroll: true });
      }, 0);
    });
    div.focus({ preventScroll: true });
    _tvSink = div;
    return div;
  }

  // Refocus the sink whenever the page regains focus (operator
  // returns from a different app), navigates, etc.
  function _refocusSink() {
    if (!_tvSink) return;
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) return;
    _tvSink.focus({ preventScroll: true });
  }

  onMount(() => {
    _ensureDbg();
    _ensureTvSink();
    _installHandlers();
    _assignFocusableScrollables();
    _scrollScanTimer = setInterval(() => {
      _assignFocusableScrollables();
      _refocusSink();
    }, 2000);
    window.addEventListener('focus', _refocusSink);
  });
  onDestroy(() => {
    _removeHandlers();
    if (_scrollScanTimer) clearInterval(_scrollScanTimer);
    window.removeEventListener('focus', _refocusSink);
  });
</script>

{@render children()}
