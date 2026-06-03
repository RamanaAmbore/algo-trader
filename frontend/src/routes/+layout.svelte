<script>
  import '../app.css';
  import { onMount, onDestroy } from 'svelte';

  const { children } = $props();

  // ── TV remote scroll handler ─────────────────────────────────────
  // Google TV / Android TV / Amazon Fire Stick browsers all send the
  // D-pad as plain KeyboardEvents (ArrowUp/Down/Left/Right, PageUp/Dn,
  // Home/End, Enter, Escape). The issue is that default browser key
  // scroll only fires when document.body / scrollingElement has focus
  // — on first load (after a navigation, modal close, etc.) the focus
  // is usually nowhere, so arrow keys do nothing on TV.
  //
  // This handler routes arrow / page / home / end keys to the
  // appropriate scrollable container:
  //   1. When a modal is open (any .canonical-modal-overlay in DOM),
  //      scroll the modal's body (.cm-body / .alm-body / .oes-body or
  //      the panel itself).
  //   2. Otherwise scroll the document.
  //
  // No-op when the active element is an editable form field — typing
  // an arrow inside <input> / <textarea> / <select> must still move
  // the caret, not scroll the page. Same for ag-Grid cells (built-in
  // arrow nav) and elements that have explicitly opted in to handling
  // their own arrow keys via `data-tv-handles-keys` or are inside
  // `[role="listbox"]` / `[role="menu"]`.

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    const k = e.key;
    if (k !== 'ArrowUp' && k !== 'ArrowDown' && k !== 'ArrowLeft' && k !== 'ArrowRight'
        && k !== 'PageUp' && k !== 'PageDown' && k !== 'Home' && k !== 'End') return;
    // A component (Select / MultiSelect / custom listbox) called
    // preventDefault — it's handling the key itself. Skip so we don't
    // scroll the page underneath a dropdown option-navigation.
    if (e.defaultPrevented) return;

    const ae = /** @type {HTMLElement|null} */ (document.activeElement);
    if (ae) {
      const tag = ae.tagName;
      // Editable form fields — leave alone so caret keys still work.
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (ae.isContentEditable) return;
      // Components that own their own key handling.
      if (ae.matches('[role="listbox"], [role="menu"], [role="menuitem"], [role="option"], [data-tv-handles-keys]')) return;
      if (ae.closest('[role="listbox"], [role="menu"], [data-tv-handles-keys], .ag-root-wrapper')) return;
    }

    // Find the scroll target. If a modal is open, prefer its body;
    // otherwise the document.
    const overlay = document.querySelector('.canonical-modal-overlay');
    /** @type {HTMLElement} */
    let target;
    if (overlay) {
      // Modal-internal scroll containers carry the bulk of the
      // content. Pick the first that has actual overflow.
      const candidates = overlay.querySelectorAll(
        '.cm-body, .alm-body, .oes-body, .canonical-modal-panel'
      );
      target = /** @type {HTMLElement} */ (
        Array.from(candidates).find((el) => el.scrollHeight > el.clientHeight + 4) || candidates[0] || overlay
      );
    } else {
      target = /** @type {HTMLElement} */ (document.scrollingElement || document.documentElement);
    }

    if (!target) return;

    const step = Math.max(60, Math.round(target.clientHeight * 0.18));
    const page = Math.max(240, Math.round(target.clientHeight * 0.9));

    let dy = 0, dx = 0;
    if      (k === 'ArrowUp')    dy = -step;
    else if (k === 'ArrowDown')  dy =  step;
    else if (k === 'ArrowLeft')  dx = -step;
    else if (k === 'ArrowRight') dx =  step;
    else if (k === 'PageUp')     dy = -page;
    else if (k === 'PageDown')   dy =  page;
    else if (k === 'Home')       { target.scrollTo({ top: 0, behavior: 'smooth' }); e.preventDefault(); return; }
    else if (k === 'End')        { target.scrollTo({ top: target.scrollHeight, behavior: 'smooth' }); e.preventDefault(); return; }

    if (dy === 0 && dx === 0) return;
    target.scrollBy({ top: dy, left: dx, behavior: 'smooth' });
    e.preventDefault();
  }

  onMount(() => { window.addEventListener('keydown', onKey, { passive: false }); });
  onDestroy(() => { window.removeEventListener('keydown', onKey); });
</script>

{@render children()}
