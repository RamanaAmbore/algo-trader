/**
 * toastStore.svelte.js — programmatic toast notification system.
 *
 * Public API:
 *   toast.success(message, opts?)
 *   toast.error(message, opts?)
 *   toast.info(message, opts?)
 *   toast.warning(message, opts?)
 *   toast.dismiss(id)
 *
 * opts shape:
 *   { timeoutMs?: number, action?: { label: string, onClick: () => void }, dismissible?: boolean }
 *
 * Default timeoutMs:
 *   success / info  → 3000 ms (auto-dismiss)
 *   warning         → 5000 ms
 *   error           → 0 (sticky — operator must dismiss)
 *
 * Returns the toast id from each push call so callers can dismiss early:
 *   const id = toast.info('Saving…', { timeoutMs: 0 });
 *   await save();
 *   toast.dismiss(id);
 *
 * Max stack: 5. When a 6th arrives, the oldest is auto-evicted.
 * Toasts are NOT persisted to localStorage — they are ephemeral.
 */

const MAX_TOASTS = 5;

const DEFAULT_TIMEOUT = {
  success: 3000,
  info:    3000,
  warning: 5000,
  error:   0,   // sticky
};

let _nextId = 1;

/** @type {{ id: number, kind: 'success'|'error'|'info'|'warning', message: string,
 *           timeoutMs: number, dismissible: boolean,
 *           action?: { label: string, onClick: () => void },
 *           createdAt: number }[]} */
export const toasts = $state([]);

/**
 * @param {'success'|'error'|'info'|'warning'} kind
 * @param {string} message
 * @param {{ timeoutMs?: number, action?: { label: string, onClick: () => void }, dismissible?: boolean }} [opts]
 * @returns {number} toast id
 */
function _push(kind, message, opts = {}) {
  const id = _nextId++;
  const timeoutMs = opts.timeoutMs !== undefined ? opts.timeoutMs : DEFAULT_TIMEOUT[kind];
  const dismissible = opts.dismissible !== false; // default true
  const entry = {
    id,
    kind,
    message,
    timeoutMs,
    dismissible,
    action: opts.action,
    createdAt: Date.now(),
  };
  // Newest appended last (stack renders newest at bottom, closest to the
  // action area). Evict oldest if we'd exceed the cap.
  if (toasts.length >= MAX_TOASTS) {
    toasts.splice(0, 1);
  }
  toasts.push(entry);
  return id;
}

/**
 * Remove a toast by id. No-op if already gone.
 * @param {number} id
 */
export function dismiss(id) {
  const idx = toasts.findIndex(t => t.id === id);
  if (idx !== -1) toasts.splice(idx, 1);
}

export const toast = {
  /** @param {string} m @param {Parameters<typeof _push>[2]} [o] */
  success: (m, o) => _push('success', m, o),
  /** @param {string} m @param {Parameters<typeof _push>[2]} [o] */
  error:   (m, o) => _push('error',   m, o),
  /** @param {string} m @param {Parameters<typeof _push>[2]} [o] */
  info:    (m, o) => _push('info',    m, o),
  /** @param {string} m @param {Parameters<typeof _push>[2]} [o] */
  warning: (m, o) => _push('warning', m, o),
  /** @param {number} id */
  dismiss,
};
