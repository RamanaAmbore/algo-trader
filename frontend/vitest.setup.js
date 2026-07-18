/**
 * Vitest global setup — runs before any test file is imported.
 *
 * Purpose: install a minimal localStorage mock at the top level so that
 * modules evaluated at import time (like chartPrefs.js whose _LS_AVAILABLE
 * is a module-level const) see a truthy `typeof localStorage !== 'undefined'`.
 *
 * Node environment has no window / localStorage — we inject a plain-JS
 * substitute on `globalThis` before any module under test loads.
 */

const _store = {};

globalThis.localStorage = {
  getItem:    (k)    => Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null,
  setItem:    (k, v) => { _store[k] = String(v); },
  removeItem: (k)    => { delete _store[k]; },
  clear:      ()     => { Object.keys(_store).forEach(k => delete _store[k]); },
};
