/**
 * Svelte action that portals the host node to document.body on mount,
 * restores its original parent on destroy. Used by modals so their
 * `position: fixed` resolves against the viewport regardless of
 * which ancestor in the source tree has transform / filter /
 * will-change / perspective applied (any of those promote the ancestor
 * to a CSS containing block and break fixed positioning).
 *
 * Usage:
 *   <div class="alm-overlay" use:portal>...</div>
 *
 * Conditional (no-op when false):
 *   <div use:portal={!inline}>...</div>
 *   When the parameter is explicitly false, the node is NOT moved —
 *   useful when the same element must conditionally portal (overlay
 *   mode) or stay in-flow (inline mode) based on a prop.
 */
export function portal(node, enabled = true) {
  if (!enabled) return {};
  document.body.appendChild(node);
  return {
    destroy() {
      // Svelte's reconciler tries to remove this node from its anchored
      // slot in the source tree — but the node was moved to <body> on
      // mount, so Svelte's targeted remove can't find it and silently
      // no-ops, leaving the node orphaned in <body>. Remove the node
      // explicitly here so closing the modal actually unmounts the DOM.
      // (Restoring to originalParent — the previous behaviour — caused
      // the X / Esc close path to fail: cmDestroyCalls=1 fired but
      // overlaysInDom stayed at 1, the modal visually stayed open.)
      try { node.parentNode?.removeChild(node); } catch (_) { /* already removed */ }
    },
  };
}
