/**
 * layerStack.test.js
 *
 * Unit tests for the Escape-stack coordinator (frontend/src/lib/utils/layerStack.js).
 *
 * Tests run in the Vitest `node` environment (no real DOM — see
 * vitest.config.js), so `document` is mocked minimally on `globalThis`
 * before importing the module under test, mirroring the existing
 * tickFlash.test.js convention for DOM-touching pure modules.
 *
 * Five quality dimensions:
 *   1. SSOT   — one stack, one real listener, LIFO top-only dispatch
 *   2. Perf   — listener installs lazily exactly once, never duplicated
 *   3. Stale  — popped layers never fire again; empty stack is a no-op
 *   4. Reuse  — exercises the exact pushLayer/popLayer API every
 *               migrated component (ConfirmModal, SymbolPanel, Select,
 *               SymbolSearchInput, mode-dropdown) now depends on
 *   5. UX     — simulates the real defect scenario: two stacked layers,
 *               Escape must hit only the topmost one
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

/** @type {Array<{type: string, fn: Function, opts: any}>} */
let _listeners = [];

function _installMockDocument() {
  _listeners = [];
  // Minimal stand-in — Vitest runs in the `node` environment (no real
  // DOM; see vitest.config.js), and layerStack.js only ever calls
  // addEventListener/removeEventListener on `document`. Cast through
  // `any` since this intentionally doesn't implement the full `Document`
  // interface — same convention as tickFlash.test.js's visibility mock.
  globalThis.document = /** @type {any} */ ({
    addEventListener: (type, fn, opts) => { _listeners.push({ type, fn, opts }); },
    removeEventListener: (type, fn) => {
      _listeners = _listeners.filter((l) => !(l.type === type && l.fn === fn));
    },
  });
}

/** Dispatch a fake Escape keydown to every registered capture-phase listener. */
function _fireEscape() {
  const stopPropagation = vi.fn();
  for (const l of _listeners) {
    l.fn({ key: 'Escape', stopPropagation });
  }
  return stopPropagation;
}

beforeEach(() => {
  _installMockDocument();
  vi.resetModules();
});

describe('layerStack', () => {
  it('installs exactly one document keydown listener, lazily on first pushLayer', async () => {
    const { pushLayer } = await import('$lib/utils/layerStack.js');
    expect(_listeners.length).toBe(0);
    pushLayer(() => {});
    expect(_listeners.length).toBe(1);
    expect(_listeners[0].type).toBe('keydown');
    expect(_listeners[0].opts).toEqual({ capture: true });
  });

  it('does not install a second listener on subsequent pushLayer calls', async () => {
    const { pushLayer } = await import('$lib/utils/layerStack.js');
    pushLayer(() => {});
    pushLayer(() => {});
    expect(_listeners.length).toBe(1);
  });

  it('Escape invokes only the topmost layer, not lower layers', async () => {
    const { pushLayer } = await import('$lib/utils/layerStack.js');
    const bottom = vi.fn();
    const top = vi.fn();
    pushLayer(bottom);
    pushLayer(top);

    const stopPropagation = _fireEscape();

    expect(top).toHaveBeenCalledTimes(1);
    expect(bottom).not.toHaveBeenCalled();
    expect(stopPropagation).toHaveBeenCalledTimes(1);
  });

  it('after popping the top layer, Escape falls through to the next one', async () => {
    const { pushLayer, popLayer } = await import('$lib/utils/layerStack.js');
    const bottom = vi.fn();
    const top = vi.fn();
    pushLayer(bottom);
    const topId = pushLayer(top);

    _fireEscape();
    expect(top).toHaveBeenCalledTimes(1);
    expect(bottom).not.toHaveBeenCalled();

    popLayer(topId);
    _fireEscape();
    expect(top).toHaveBeenCalledTimes(1); // unchanged — popped, no longer fires
    expect(bottom).toHaveBeenCalledTimes(1);
  });

  it('empty stack: Escape is a no-op (no callback, no throw)', async () => {
    const { pushLayer, popLayer } = await import('$lib/utils/layerStack.js');
    const only = vi.fn();
    const id = pushLayer(only);
    popLayer(id);

    expect(() => _fireEscape()).not.toThrow();
    expect(only).not.toHaveBeenCalled();
  });

  it('popLayer is a safe no-op for an unknown or already-popped id', async () => {
    const { pushLayer, popLayer } = await import('$lib/utils/layerStack.js');
    const cb = vi.fn();
    const id = pushLayer(cb);
    popLayer(id);
    expect(() => popLayer(id)).not.toThrow();
    expect(() => popLayer('not-a-real-id')).not.toThrow();
    expect(() => popLayer(null)).not.toThrow();
  });

  it('non-Escape keys never invoke any layer callback', async () => {
    const { pushLayer } = await import('$lib/utils/layerStack.js');
    const cb = vi.fn();
    pushLayer(cb);
    for (const l of _listeners) {
      l.fn({ key: 'Enter', stopPropagation: vi.fn() });
    }
    expect(cb).not.toHaveBeenCalled();
  });
});
