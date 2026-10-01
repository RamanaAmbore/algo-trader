/**
 * layerStack.js — shared Escape-stack coordinator for every dismissible
 * overlay layer in the app (modals, portalled dropdowns, nested popovers).
 *
 * Root cause this fixes (2026-09 stacking-defect audit): every overlay
 * layer used to listen for Escape independently (its own `window` or
 * `document` keydown listener), so pressing Escape once could close the
 * WRONG layer, close MULTIPLE layers at once, or do nothing at all —
 * whichever listener's bubble-order happened to fire first/last won,
 * with no coordination between layers.
 *
 * Design — deliberately minimal, no store, no context, just a
 * module-level LIFO stack + one real DOM listener for the whole app:
 *
 *   - `pushLayer(onEscape)` registers a new topmost layer and returns an
 *     opaque id. Call this the moment the layer becomes visible (dropdown
 *     opens, modal mounts, etc).
 *   - `popLayer(id)` removes that layer. Call this from the layer's own
 *     close/cleanup/onDestroy path — the coordinator never auto-pops, so
 *     a layer that forgets to pop will keep intercepting Escape even
 *     after it's visually gone (treat this like forgetting to
 *     removeEventListener).
 *   - On Escape, only the CURRENT top-of-stack layer's `onEscape` runs.
 *     `event.stopPropagation()` is called first (capture-phase, on
 *     `document`) so the keydown never reaches any other still-mounted
 *     listener (a not-yet-migrated component's own window/document
 *     listener, or the browser's default Escape behaviour) — this is
 *     what prevents "one Escape closes two layers" once at least the
 *     topmost layer is migrated.
 *   - An empty stack does nothing — Escape falls through to whatever
 *     page-level shortcut handling exists (e.g. the global `?`
 *     cheatsheet / `g`+letter navigation in the algo layout), which is
 *     intentionally NOT part of this coordinator (it isn't a dismissible
 *     overlay layer).
 *
 * The single `document` listener is installed lazily (on the first ever
 * `pushLayer` call) and never removed — this is a tiny, permanent,
 * app-lifetime listener, equivalent in cost to the many per-component
 * listeners it replaces.
 */

/** @type {{ id: string, onEscape: () => void }[]} */
const _stack = [];

let _installed = false;
let _nextId = 1;

/** @param {KeyboardEvent} event */
function _onDocumentKeydownCapture(event) {
  if (event.key !== 'Escape') return;
  if (_stack.length === 0) return;
  // Capture-phase + stopPropagation: consume the Escape before it can
  // reach any other listener (bubble-phase window/document listeners on
  // not-yet-migrated components, or further capture-phase listeners
  // deeper in the tree) — only the top layer reacts.
  event.stopPropagation();
  const top = _stack[_stack.length - 1];
  top.onEscape?.();
}

function _ensureInstalled() {
  if (_installed) return;
  if (typeof document === 'undefined') return;
  document.addEventListener('keydown', _onDocumentKeydownCapture, { capture: true });
  _installed = true;
}

/**
 * Register a new topmost dismissible layer.
 * @param {() => void} onEscape — called when Escape is pressed while this
 *   layer is the topmost registered layer. Must close/dismiss the layer;
 *   the coordinator does not pop the layer itself — call `popLayer` from
 *   inside this callback (or from wherever else the layer actually closes).
 * @returns {string} an opaque id — pass it to `popLayer` on close.
 */
export function pushLayer(onEscape) {
  _ensureInstalled();
  const id = `layer-${_nextId++}`;
  _stack.push({ id, onEscape });
  return id;
}

/**
 * Remove a previously-pushed layer. Safe to call multiple times / with an
 * id that's already gone (no-op) — components can call this unconditionally
 * from their own close path without tracking whether they already popped.
 * @param {string | null | undefined} id
 */
export function popLayer(id) {
  if (!id) return;
  const idx = _stack.findIndex((l) => l.id === id);
  if (idx !== -1) _stack.splice(idx, 1);
}
