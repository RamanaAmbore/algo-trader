<script module>
  // Module-scope singleton — one mount per app is enough since modals
  // are one-at-a-time. Provides a Promise-returning `ask()` so callers
  // can replace `if (!confirm(...))` with `if (!await confirm(...))`
  // without restructuring.
</script>

<script>
  import ModalShell from '$lib/ModalShell.svelte';

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
    // svelte-check widens _resolve into the union `((boolean)=>void) |
    // ((string|null)=>void) | null` whose narrowed call-signature
    // becomes `(never)=>void` — so directly calling `r(ok)` or `r(v)`
    // fails type-check even though we know exactly which arm is live.
    // Cast through `any` here is the cleanest fix; the runtime branch
    // above already picks the correct payload for the resolver.
    const r = /** @type {any} */ (_resolve);
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

  function _cancel() { _resolve_and_close(false); }

  function onKey(/** @type {KeyboardEvent} */ e) {
    if (!_open) return;
    if (e.key === 'Escape') { e.preventDefault(); _resolve_and_close(false); }
    else if (e.key === 'Enter') { e.preventDefault(); _resolve_and_close(true); }
  }
</script>

<svelte:window onkeydown={onKey} />

<ModalShell open={_open} onClose={_cancel} ariaLabel="Confirm action" zIndex={400}>
    <div class="cm-modal algo-modal" role="presentation"
         onclick={(e) => e.stopPropagation()}>
      <div class="cm-title">{_title}</div>
      {#if _message}<div class="cm-message">{@html _message}</div>{/if}
      {#if _inputType}
        <div class="cm-input-wrap">
          {#if _inputLabel}<label class="cm-input-label" for="cm-input">{_inputLabel}</label>{/if}
          <!-- svelte-ignore a11y_autofocus -->
          <input id="cm-input"
                 type={_inputType}
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
</ModalShell>

<style>
  .cm-modal {
    /* Composes .algo-modal chrome (amber halo + radius + shadow + flex
       column + overflow hidden). Overrides:
       - background: elevated navy gradient (lighter than base dark
         --card-bg-gradient) for modal depth. Confirm dialogs sit ON
         TOP of already-dark surfaces so we lift them one notch.
       - border: soft amber to distinguish confirm from content-rich
         canonical modals which use the full amber-halo border. */
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid var(--algo-amber-border-soft);
    padding: 0.85rem 1rem;
    width: min(22rem, calc(100vw - 2rem));
    color: var(--algo-slate);
    font-family: var(--font-numeric);
  }
  .cm-title {
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--algo-amber);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.45rem;
  }
  .cm-message {
    font-size: var(--fs-lg);
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
    font-size: var(--fs-sm);
    color: rgba(200,216,240,0.6);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .cm-input {
    background: rgba(10,16,32,0.6);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    padding: 0.4rem 0.55rem;
    outline: none;
  }
  .cm-input:focus { border-color: var(--algo-amber-border); }
  .cm-cancel,
  .cm-confirm {
    padding: 0.4rem 0.95rem;
    font-size: var(--fs-lg);
    font-weight: 700;
    font-family: var(--font-numeric);
    border-radius: 3px;
    cursor: pointer;
    border: 1px solid transparent;
  }
  .cm-cancel {
    background: transparent;
    border-color: rgba(255,255,255,0.18);
    color: var(--algo-slate);
  }
  .cm-cancel:hover { border-color: rgba(255,255,255,0.35); }
  .cm-confirm {
    background: var(--algo-green-bg);
    border-color: var(--algo-green-border);
    color: var(--algo-green);
  }
  .cm-confirm:hover { background: var(--algo-green-bg-strong); }
  /* Danger variant (suspend, terminate, delete, etc.) — red Confirm. */
  .cm-confirm-danger {
    background: var(--algo-red-bg);
    border-color: var(--algo-red-border);
    color: var(--algo-red);
  }
  .cm-confirm-danger:hover { background: var(--algo-red-bg-strong); }
</style>
