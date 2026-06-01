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
  const originalParent = node.parentNode;
  document.body.appendChild(node);
  return {
    destroy() {
      // Re-adopt the node to its original parent so Svelte's normal
      // DOM teardown can find and remove it. If the original parent
      // is already gone (e.g. during a full page tear-down), skip.
      if (originalParent && originalParent !== node.parentNode) {
        try { originalParent.appendChild(node); } catch (_) { /* parent gone */ }
      }
    },
  };
}
