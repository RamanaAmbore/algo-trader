<script>
  import { tick } from 'svelte';
  import Select from '$lib/Select.svelte';
  import ModalShell from '$lib/ModalShell.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import { getInstrument } from '$lib/data/instruments.js';
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';

  /**
   * Add-to-watchlist modal extracted from MarketPulse.svelte (Phase 3).
   *
   * All mutable form state is $bindable so MarketPulse retains the SSOT
   * for every value — the async backend-calling functions (addRow,
   * dropList, commitRename, cancelRename, pickFromTypeahead,
   * loadActive, closeSearch) stay in MarketPulse and are wired in as
   * callbacks.
   *
   * Symbol search (2026-10) uses the canonical $lib/SymbolSearchInput
   * component — same one SymbolPanel.svelte / ChartWorkspace.svelte use
   * for order entry — instead of a bespoke input + result list. See
   * `_handleSymbolPick` below for the adapter that keeps
   * `onPickTypeahead` (still owned by MarketPulse's `pickFromTypeahead`)
   * receiving the same `{s, e, virtual}` shape it always has.
   */

  let {
    open = $bindable(false),        // mirrors MarketPulse.searchOpen
    lists,                          // watchlist array (read-only)
    focusedListId,                  // currently-focused list id (seeds default, read-only)
    targetListId = $bindable(null), // selected watchlist or 'NEW'
    newListName  = $bindable(''),
    symInput     = $bindable(''),
    typeInput    = $bindable(/** @type {'EQ'|'FU'|'CE'|'PE'} */ ('EQ')),
    aliasInput   = $bindable(''),
    renameId     = $bindable(/** @type {number|null} */ (null)),
    renameName   = $bindable(''),
    renameError  = $bindable(''),
    isDemo       = false,
    // Callbacks — all async operations remain in MarketPulse.
    onAdd,            // () => Promise<void>  — parent's addRow()
    onDropList,       // (id) => Promise<void>
    onCommitRename,   // () => Promise<void>
    onCancelRename,   // () => void
    onPickTypeahead,  // (inst) => void         — picks a match ({s, e, virtual})
    onClose,          // () => void             — caller sets open=false + clears inputs
    // Forwarded to the internal ModalShell — caller overrides for
    // stacking (e.g. "var(--z-modal-nested)" when opened from inside a
    // full-screen card). Default matches ModalShell's own default so
    // every other caller is unaffected.
    zIndex = /** @type {number | string} */ (200),
  } = $props();

  /** @type {HTMLDivElement | null} */
  let symWrapEl = $state(null);
  // True for one keystroke-cycle once SymbolSearchInput's own onPick
  // fires synchronously inside its Enter handler — lets the wrapper's
  // bubbled keydown (below) tell "operator picked a row" apart from
  // "no match, fall through to manual Add", matching the original
  // inline-input's own Enter branch.
  let _pickedViaSearch = $state(false);

  // Auto-focus the symbol input when the modal opens. SymbolSearchInput
  // owns its own <input> internally (no exposed element binding), so
  // reach it via the wrapper div instead of a direct element ref.
  $effect(() => {
    if (open) {
      // Defer until the modal is painted (same tick as Svelte render).
      tick().then(() => {
        const el = symWrapEl?.querySelector('input');
        el?.focus(); el?.select();
      });
    }
  });

  // NSE index underlyings (NIFTY, BANKNIFTY, …) have no virtual-root
  // equivalent — those only exist for MCX/CDS roots (GOLD, CRUDEOIL,
  // USDINR, …) — so SymbolSearchInput's own bare-underlying filter
  // (instruments.js tradable guard) drops their raw index row from
  // search results entirely. That bare row is exactly what
  // `pickFromTypeahead` (MarketPulse.svelte) needs to open the F&O
  // option-chain picker for an index. Surfaced here as pins so they
  // stay reachable (shown below the 3-char search threshold) without
  // reaching into SymbolSearchInput's own filtering.
  const _NSE_INDEX_PINS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX'];

  /** Adapter: SymbolSearchInput's onPick(sym, meta) → the
   *  `{s, e, virtual}` shape `onPickTypeahead`/`pickFromTypeahead` has
   *  always received from the old raw-typeahead button's `inst`. */
  function _handleSymbolPick(/** @type {string} */ sym, /** @type {any} */ meta) {
    _pickedViaSearch = true;
    let exch = meta?.exchange || '';
    let virtual = !!meta?.virtual;
    if (!exch && meta?.pinLabel) exch = 'NSE'; // our own NSE index shortcut pins
    if (!exch) {
      const inst = getInstrument(sym);
      if (inst) { exch = inst.e || ''; virtual = virtual || !!inst.virtual; }
    }
    onPickTypeahead({ s: sym, e: exch, virtual });
  }

  // Escape-stack coordinator (layerStack.js) — this component is always
  // mounted by MarketPulse (never `{#if}`-gated; `open` just toggles its
  // internal <ModalShell>). Uses the push-then-return-popLayer teardown
  // form (same shape as OrderPairModal.svelte), NOT the if/open-else/pop
  // form Select.svelte uses — that form only pops when `open` itself
  // flips false in a LATER effect run; it never pops if the HOST
  // (MarketPulse) unmounts while this modal happens to be open (e.g. a
  // route change away from /pulse mid-session), leaking a layer that
  // would silently swallow every page-level Escape for the rest of the
  // tab's life. The teardown form's returned callback runs on both
  // paths (open→false AND component destroy), so it can't leak.
  //
  // Without this, pressing Escape while this modal is open over a
  // fullscreen card (or any other already-migrated layer) closed BOTH
  // at once — ModalShell's own `<svelte:window onkeydown>` fires
  // unconditionally on every Escape with no stacking awareness at all.
  // Registering a layer here means the capture-phase layerStack
  // listener consumes the Escape first (stopPropagation) and
  // ModalShell's bubble-phase listener never runs — same reliance
  // ConfirmModal.svelte's own ask()/prompt() already has on this exact
  // mechanic.
  //
  // Because the layerStack listener is capture-phase on `document` and
  // calls stopPropagation(), the event never reaches this modal's own
  // per-input `onkeydown` handlers below — their former Escape branches
  // are folded into this ONE callback instead (self-audit: left in
  // place they'd be structurally unreachable dead code). Two real,
  // user-visible cases existed and are preserved here:
  //   - renaming a watchlist (the "Esc to cancel" hint below the rename
  //     row) → cancel the rename, don't close the whole modal
  //   - otherwise → the real `onClose` prop (not a bare `open = false`)
  //     so the caller's own cleanup (MarketPulse's `closeSearch()`
  //     resets typeahead/newListName/aliasInput/rename state) still
  //     runs, same as every other close path in this component.
  // A third case — "first Esc closes the symbol-search dropdown, not
  // the whole modal" — is now real again (2026-10, SymbolSearchInput
  // migration): SymbolSearchInput pushes its OWN layer while its
  // dropdown is open (see its own layerStack effect), so it sits ABOVE
  // this modal's layer on the stack. The first Escape hits that
  // topmost layer and only closes the dropdown; this callback — the
  // modal's own close — only runs on a subsequent Escape once the
  // dropdown layer has popped. No code change needed here for that;
  // noted because it changes the operator-visible Escape count.
  $effect(() => {
    if (!open) return;
    const id = pushLayer(() => {
      if (renameId !== null && renameId === targetListId) { onCancelRename?.(); return; }
      onClose?.();
    });
    return () => popLayer(id);
  });
</script>

<ModalShell open={!!open} {onClose} ariaLabel="Add to Pulse" {zIndex}>
    <div class="search-modal" role="presentation" onclick={(e) => e.stopPropagation()}>
      <div class="search-header canonical-modal-header">
        <span class="search-title">Manage watchlists</span>
        <button type="button" class="search-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
      </div>
      <div class="search-body">
        <!-- Watchlist target — Default ★ pre-selected; "+ New watchlist"
             reveals an inline name input which is created on Add. The
             trailing × button deletes the currently-selected list
             (disabled for the Default list and the "+ New" sentinel). -->
        <div class="mp-add-section-label">Watchlist</div>
        <div class="search-row">
          <div class="flex-1">
            <Select ariaLabel="Watchlist" bind:value={targetListId}
              options={[
                ...lists.map(l => ({
                  value: l.id,
                  label: l.is_default ? `${l.name} ★` : l.name,
                })),
                ...(!isDemo ? [{ value: 'NEW', label: '+ New watchlist' }] : []),
              ]} />
          </div>
          {#if typeof targetListId === 'number'}
            {@const _tgtList = lists.find(l => l.id === targetListId)}
            <!-- Show the Rename / Delete affordances for operator-
                 created lists only. The shared global Pinned is the
                 canonical always-present list — its name is fixed and
                 it can't be deleted (would leave every user without a
                 pinned list). Designated users can still add / remove
                 ITEMS on it via the symbol picker + per-row × glyph;
                 only the list-level rename / delete is locked out.
                 Demo (anonymous) users cannot rename or delete anything. -->
            {#if !isDemo}
              {#if _tgtList && !_tgtList.is_global}
                <!-- ✎ Rename — reveals the inline name input row below
                     so the operator can edit the watchlist's name without
                     leaving the popup. -->
                <button type="button"
                  onclick={(e) => {
                    e.preventDefault();
                    const id = /** @type {number} */ (targetListId);
                    if (renameId === id) { onCancelRename(); return; }
                    renameId    = id;
                    renameName  = _tgtList.name;
                    renameError = '';
                  }}
                  class="text-[0.7rem] py-1 px-3 rounded font-bold border"
                  style="background: rgba(56,189,248,0.2); color: var(--algo-sky); border-color: rgba(56,189,248,0.55);"
                  title={renameId === targetListId ? 'Cancel rename' : `Rename "${_tgtList.name}" watchlist`}>
                  {renameId === targetListId ? '× Cancel' : '✎ Rename'}
                </button>
                <button type="button"
                  onclick={async (e) => {
                    e.preventDefault();
                    // Single-click delete (operator picked the list +
                    // clicked Delete inside the Manage popup — that's
                    // confirmation enough). The earlier two-click
                    // pattern confused operators ("when I delete test
                    // watchlist it is not getting deleted" — they
                    // missed the 4-second confirm window).
                    const id = /** @type {number} */ (targetListId);
                    try {
                      await onDropList(id);
                      onClose();
                    } catch (err) {
                      // Surface the failure inline so the operator sees
                      // why nothing happened (auth lapse, 403 on Pinned,
                      // network drop, etc.) instead of a silent no-op.
                      renameError = (err && err.message) || 'Delete failed.';
                    }
                  }}
                  class="text-[0.7rem] py-1 px-3 rounded font-bold border"
                  style="background: rgba(248,113,113,0.2); color: var(--c-short); border-color: var(--algo-red-border);"
                  title={`Delete "${_tgtList.name}" watchlist`}>
                  🗑 Delete
                </button>
              {/if}
            {/if}
          {/if}
        </div>
        {#if renameId !== null && renameId === targetListId}
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={renameName}
              onkeydown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); onCommitRename(); }
                // Escape is handled by the layerStack coordinator (pushLayer
                // in <script> above) instead of here — see its comment.
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="New name" autocomplete="off" />
            <button type="button" onclick={onCommitRename}
              disabled={!renameName.trim()}
              class="btn-primary text-[0.7rem] py-1 px-3 disabled:opacity-50">Save</button>
          </div>
          {#if renameError}
            <div class="search-hint" style="color:var(--c-short)">{renameError}</div>
          {:else}
            <div class="search-hint">Enter to save · Esc to cancel · names are case-insensitive and must be unique.</div>
          {/if}
        {/if}
        {#if targetListId === 'NEW'}
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={newListName}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="New watchlist name" autocomplete="off" />
          </div>
          <div class="search-hint">
            Names are case-insensitive and must be unique. The list is created when you press Add.
          </div>
        {:else if typeof targetListId === 'number'}
          {@const _tgtCheck = lists.find(l => l.id === targetListId)}
          {#if _tgtCheck && !_tgtCheck.is_default}
            <div class="search-hint">
              Pick a different list to switch target. Click 🗑 Delete to remove "{_tgtCheck.name}".
            </div>
          {/if}
        {/if}

        {#if !isDemo}
          <div class="mp-add-divider"></div>

          <!-- Symbol + Type. The two-letter type picker after the symbol
               input lets the operator disambiguate equity vs derivative
               without picking a raw exchange code (EQ/FU/CE/PE → NSE/NFO
               internally). Typeahead picks override the type from the
               matched instrument's tradingsymbol suffix. -->
          <div class="mp-add-section-label">Add symbol</div>
          <div class="search-row">
            <!-- SymbolSearchInput only writes its bindable `value` prop
                 on a pick, never on every keystroke, so the wrapper's
                 own `oninput`/`onkeydown` (native events bubble up from
                 its nested <input>) is what keeps `symInput` live for
                 the manual Add button and the Enter-with-no-match
                 fallback below — same two behaviours the old inline
                 input had via its own bind:value + onkeydown. -->
            <!-- svelte-ignore a11y_no_static_element_interactions -- this div
                 is a pure event-bubbling relay around SymbolSearchInput's own
                 real <input> (which already owns keyboard/focus semantics);
                 it holds no independent interactive role of its own. -->
            <div class="flex-1 atp-sym-pick" role="presentation"
              bind:this={symWrapEl}
              oninput={(e) => {
                _pickedViaSearch = false;
                symInput = /** @type {HTMLInputElement} */ (e.target).value;
              }}
              onkeydown={(e) => {
                // SymbolSearchInput's own onkeydown (bound directly on
                // its <input>) runs first and always preventDefault()s
                // on Enter; it also calls onPick synchronously when it
                // has a row/pin match, which flips _pickedViaSearch via
                // _handleSymbolPick before this bubbled handler runs.
                if (e.key === 'Enter' && !_pickedViaSearch) onAdd();
                // Escape is handled by the layerStack coordinator (pushLayer
                // in <script> above) instead of here — see its comment.
              }}>
              <SymbolSearchInput
                value={symInput}
                placeholder="Symbol (≥ 3 chars) — stocks, futures, options"
                ariaLabel="Symbol search — stocks, futures, options"
                pins={_NSE_INDEX_PINS}
                resolvePin={(label) => label}
                onPick={_handleSymbolPick} />
            </div>
            <div class="w-16">
              <Select ariaLabel="Type" bind:value={typeInput}
                options={[
                  { value: 'EQ', label: 'EQ' },
                  { value: 'FU', label: 'FU' },
                  { value: 'CE', label: 'CE' },
                  { value: 'PE', label: 'PE' },
                ]} />
            </div>
            <button onclick={onAdd}
              disabled={!symInput.trim() || (targetListId === 'NEW' && !newListName.trim())}
              class="btn-primary text-[0.7rem] py-1 px-3 disabled:opacity-50"
              title="Add to target watchlist">Add</button>
          </div>
          <!-- Optional display name (alias). Lets the operator label a
               contract by its underlying nickname — e.g. type "Crude oil"
               for CRUDEOIL26JUNFUT. Empty leaves the grid showing the
               raw tradingsymbol; non-empty replaces the symbol cell with
               the alias (and the tradingsymbol moves to the tooltip). -->
          <div class="search-row" style="margin-top: 0.4rem;">
            <input bind:value={aliasInput}
              onkeydown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); onAdd(); }
                // Escape is handled by the layerStack coordinator (pushLayer
                // in <script> above) instead of here — see its comment.
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="Display name (optional) — e.g. Crude oil"
              autocomplete="off" />
          </div>
          <div class="search-hint">
            Type ≥ 3 characters · Enter picks the first match · F&amp;O underlyings open the option chain picker
          </div>
        {/if}
      </div>
    </div>
</ModalShell>

<style>
  /* SymbolSearchInput's native width is a fixed 11rem (its compact
     default for inline order-entry toolbars) — stretch it to fill this
     row's flex-1 slot like every other field in this modal. Scoped to
     .atp-sym-pick so SymbolPanel/ChartWorkspace's own native sizing is
     untouched. Palette (amber-on-navy) is left as-is — same canonical
     symbol-picker look those two surfaces already use. */
  .atp-sym-pick { display: flex; }
  .atp-sym-pick :global(.ssi-wrap) { flex: 1; display: flex; }
  .atp-sym-pick :global(.ssi-input) { width: 100%; }
</style>
