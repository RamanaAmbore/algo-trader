<script module>
  // Module-scope singleton — one mount per app is enough since modals
  // are one-at-a-time. Provides a Promise-returning `ask()` so callers
  // can replace `if (!confirm(...))` with `if (!await confirm(...))`
  // without restructuring.
</script>

<script>
  /**
   * Confirm modal. Promise-returning `ask()` resolves true on confirm,
   * false on cancel/Esc/overlay-click.
   *
   * Replaces native `window.confirm()` because:
   *   1. iOS PWA standalone-mode silently no-ops `confirm()` — the
   *      operator's "Are you sure?" click was bypassed AND the action
   *      ran. Real defect for destructive ops (suspend / terminate
   *      user, etc.).
   *   2. Native dialog visually breaks the dark UI on every desktop OS.
   *   3. No keyboard-friendly Confirm shortcut on every browser.
   *
   * Usage:
   *   <ConfirmModal bind:this={confirmRef} />
   *   if (!await confirmRef.ask({title:'Suspend user?', danger:true})) return;
   */
  let _open    = $state(false);
  let _title   = $state('Confirm');
  let _message = $state('');
  let _danger  = $state(false);
  let _confirmLabel = $state('Confirm');
  let _cancelLabel  = $state('Cancel');
  // Prompt mode — when _inputType !== null the modal also renders an
  // <input> and the resolver receives the typed value (or null on
  // cancel). Used for password resets, list-rename, etc.
  let _inputType  = $state(/** @type {'text'|'password'|null} */ (null));
  let _inputLabel = $state('');
  let _inputValue = $state('');
  let _inputPlaceholder = $state('');
  /** @type {((ok: boolean) => void) | ((val: string|null) => void) | null} */
  let _resolve = null;

  /**
   * @param {{title?:string, message?:string, danger?:boolean,
   *          confirmLabel?:string, cancelLabel?:string}} opts
   * @returns {Promise<boolean>}
   */
  export function ask(opts = {}) {
    _title         = opts.title         ?? 'Confirm';
    _message       = opts.message       ?? '';
    _danger        = !!opts.danger;
    _confirmLabel  = opts.confirmLabel  ?? (opts.danger ? 'Delete' : 'Confirm');
    _cancelLabel   = opts.cancelLabel   ?? 'Cancel';
    _inputType     = null;
    _inputValue    = '';
    _open = true;
    return new Promise((res) => { _resolve = res; });
  }

  /**
   * Prompt mode — like ask() but also collects an input value.
   * Resolves to the typed value on confirm or null on cancel.
   *
   * @param {{title?:string, message?:string, label?:string,
   *          placeholder?:string, defaultValue?:string,
   *          inputType?:'text'|'password', danger?:boolean,
   *          confirmLabel?:string, cancelLabel?:string}} opts
   * @returns {Promise<string|null>}
   */
  export function prompt(opts = {}) {
    _title         = opts.title         ?? 'Enter value';
    _message       = opts.message       ?? '';
    _danger        = !!opts.danger;
    _confirmLabel  = opts.confirmLabel  ?? (opts.danger ? 'Delete' : 'OK');
    _cancelLabel   = opts.cancelLabel   ?? 'Cancel';
    _inputType     = opts.inputType     ?? 'text';
    _inputLabel    = opts.label         ?? '';
    _inputPlaceholder = opts.placeholder ?? '';
    _inputValue    = opts.defaultValue  ?? '';
    _open = true;
    return new Promise((res) => { _resolve = res; });
  }

  function _resolve_and_close(/** @type {boolean} */ ok) {
    const isPrompt = _inputType !== null;
    const r = _resolve;
    _resolve = null;
    _open = false;
    if (isPrompt) {
      // Prompt mode resolves to the typed value (or null on cancel).
      // Empty string on confirm counts as null — most callers want a
      // single non-empty-string-or-cancel branch.
      const v = ok ? (_inputValue || null) : null;
      r?.(v);
    } else {
      r?.(ok);
    }
  }

  function onKey(/** @type {KeyboardEvent} */ e) {
    if (!_open) return;
    if (e.key === 'Escape') { e.preventDefault(); _resolve_and_close(false); }
    else if (e.key === 'Enter') { e.preventDefault(); _resolve_and_close(true); }
  }
</script>

<svelte:window onkeydown={onKey} />

{#if _open}
  <div class="cm-overlay" role="dialog" aria-modal="true" aria-label={_title}
       onclick={() => _resolve_and_close(false)}>
    <div class="cm-modal" role="document"
         onclick={(e) => e.stopPropagation()}>
      <div class="cm-title">{_title}</div>
      {#if _message}<div class="cm-message">{@html _message}</div>{/if}
      {#if _inputType}
        <div class="cm-input-wrap">
          {#if _inputLabel}<label class="cm-input-label">{_inputLabel}</label>{/if}
          <!-- svelte-ignore a11y_autofocus -->
          <input type={_inputType}
                 class="cm-input"
                 placeholder={_inputPlaceholder}
                 bind:value={_inputValue}
                 autofocus
                 autocomplete="off" />
        </div>
      {/if}
      <div class="cm-footer">
        <button type="button" class="cm-cancel"
                onclick={() => _resolve_and_close(false)}>{_cancelLabel}</button>
        <button type="button" class="cm-confirm" class:cm-confirm-danger={_danger}
                onclick={() => _resolve_and_close(true)}>{_confirmLabel}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .cm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    /* Above OrderTicket (z=300) so a confirm raised from within a
       ticket flow always renders on top. */
    z-index: 400;
    padding: 1rem;
  }
  .cm-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 6px;
    padding: 0.85rem 1rem;
    width: min(22rem, calc(100vw - 2rem));
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
  }
  .cm-title {
    font-size: 0.72rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.45rem;
  }
  .cm-message {
    font-size: 0.7rem;
    color: rgba(200,216,240,0.85);
    line-height: 1.45;
    margin-bottom: 0.85rem;
  }
  .cm-footer {
    display: flex;
    gap: 0.45rem;
    justify-content: flex-end;
  }
  /* Prompt-mode input — text or password. Sits between the message
     and the button row. */
  .cm-input-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    margin-bottom: 0.85rem;
  }
  .cm-input-label {
    font-size: 0.6rem;
    color: rgba(200,216,240,0.6);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .cm-input {
    background: rgba(10,16,32,0.6);
    border: 1px solid rgba(251,191,36,0.30);
    border-radius: 3px;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.72rem;
    padding: 0.4rem 0.55rem;
    outline: none;
  }
  .cm-input:focus { border-color: rgba(251,191,36,0.65); }
  .cm-cancel,
  .cm-confirm {
    padding: 0.4rem 0.95rem;
    font-size: 0.7rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    border-radius: 3px;
    cursor: pointer;
    border: 1px solid transparent;
  }
  .cm-cancel {
    background: transparent;
    border-color: rgba(255,255,255,0.18);
    color: #c8d8f0;
  }
  .cm-cancel:hover { border-color: rgba(255,255,255,0.35); }
  .cm-confirm {
    background: rgba(74,222,128,0.12);
    border-color: rgba(74,222,128,0.55);
    color: #4ade80;
  }
  .cm-confirm:hover { background: rgba(74,222,128,0.20); }
  /* Danger variant (suspend, terminate, delete, etc.) — red Confirm. */
  .cm-confirm-danger {
    background: rgba(248,113,113,0.12);
    border-color: rgba(248,113,113,0.55);
    color: #f87171;
  }
  .cm-confirm-danger:hover { background: rgba(248,113,113,0.22); }
</style>
