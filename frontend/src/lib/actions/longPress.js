/**
 * Svelte action that fires the handler on long-press (touch) OR
 * contextmenu (right-click). Both gestures map to the same handler.
 * Single tap / left click pass through to the host element's onclick.
 *
 * Usage:
 *   <span use:longPress={(e) => openMenu(e.clientX, e.clientY)}>RELIANCE</span>
 */
export function longPress(node, handler) {
  let timer = null;
  let fired = false;
  const DELAY = 500; // ms

  function onTouchStart(e) {
    fired = false;
    const t = e.touches[0];
    const x = t.clientX;
    const y = t.clientY;
    timer = setTimeout(() => {
      fired = true;
      handler?.({ clientX: x, clientY: y, type: 'longpress' });
    }, DELAY);
  }
  function onTouchEnd() {
    if (timer) clearTimeout(timer);
    timer = null;
  }
  function onTouchMove() {
    // any movement cancels long-press
    if (timer) clearTimeout(timer);
    timer = null;
  }
  function onContextMenu(e) {
    e.preventDefault();
    handler?.({ clientX: e.clientX, clientY: e.clientY, type: 'contextmenu' });
  }
  function onClick(e) {
    // If a long-press fired moments ago, suppress the click so it
    // doesn't fall through to the symbol-click handler.
    if (fired) {
      e.preventDefault();
      e.stopPropagation();
      fired = false;
    }
  }

  node.addEventListener('touchstart',   onTouchStart,  { passive: true });
  node.addEventListener('touchend',     onTouchEnd);
  node.addEventListener('touchcancel',  onTouchEnd);
  node.addEventListener('touchmove',    onTouchMove);
  node.addEventListener('contextmenu',  onContextMenu);
  node.addEventListener('click',        onClick, { capture: true });

  return {
    update(newHandler) { handler = newHandler; },
    destroy() {
      if (timer) clearTimeout(timer);
      node.removeEventListener('touchstart',  onTouchStart);
      node.removeEventListener('touchend',    onTouchEnd);
      node.removeEventListener('touchcancel', onTouchEnd);
      node.removeEventListener('touchmove',   onTouchMove);
      node.removeEventListener('contextmenu', onContextMenu);
      node.removeEventListener('click',       onClick, { capture: true });
    },
  };
}
