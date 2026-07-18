<script>
  import { tick } from 'svelte';
  import Select from '$lib/Select.svelte';
  import ModalShell from '$lib/ModalShell.svelte';
  import { displaySymbol } from '$lib/data/displaySymbol.js';

  /**
   * Add-to-watchlist modal extracted from MarketPulse.svelte (Phase 3).
   *
   * All mutable form state is $bindable so MarketPulse retains the SSOT
   * for every value — the async backend-calling functions (addRow,
   * dropList, commitRename, cancelRename, searchSymbols, pickFromTypeahead,
   * loadActive, closeSearch) stay in MarketPulse and are wired in as
   * callbacks.
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
    typeahead    = $bindable(/** @type {any[]} */ ([])),
    typeaheadOpen = $bindable(false),
    renameId     = $bindable(/** @type {number|null} */ (null)),
    renameName   = $bindable(''),
    renameError  = $bindable(''),
    isDemo       = false,
    // Callbacks — all async operations remain in MarketPulse.
    onAdd,            // () => Promise<void>  — parent's addRow()
    onDropList,       // (id) => Promise<void>
    onCommitRename,   // () => Promise<void>
    onCancelRename,   // () => void
    onSearchSymbols,  // (q) => Promise<void>  — populates typeahead
    onPickTypeahead,  // (inst) => void         — picks first match
    onClose,          // () => void             — caller sets open=false + clears inputs
  } = $props();

  /** @type {HTMLInputElement | null} */
  let symInputEl = $state(null);

  // Auto-focus the symbol input when the modal opens.
  $effect(() => {
    if (open) {
      // Defer until the modal is painted (same tick as Svelte render).
      tick().then(() => { symInputEl?.focus(); symInputEl?.select(); });
    }
  });
</script>

<ModalShell open={!!open} {onClose} ariaLabel="Add to Pulse">
    <div class="search-modal" role="presentation" onclick={(e) => e.stopPropagation()}>
      <div class="search-header">
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
                else if (e.key === 'Escape') { e.preventDefault(); onCancelRename(); }
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
              onkeydown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault();
                  if (typeaheadOpen && typeahead.length) { typeaheadOpen = false; }
                  else { onClose(); }
                }
              }}
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
            <input bind:this={symInputEl} bind:value={symInput}
              oninput={(e) => { onSearchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
              onfocus={() => typeaheadOpen = true}
              onkeydown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  if (typeaheadOpen && typeahead.length && symInput.trim()) onPickTypeahead(typeahead[0]);
                  else onAdd();
                } else if (e.key === 'Escape') {
                  // First Esc closes the typeahead suggestions if they're
                  // ACTUALLY rendered (typeaheadOpen AND non-empty); a
                  // second Esc closes the popup. When the dropdown is
                  // invisible — onfocus sets typeaheadOpen=true even when
                  // typeahead is [] — first Esc would otherwise no-op,
                  // forcing operators to press Esc twice to dismiss.
                  e.preventDefault();
                  if (typeaheadOpen && typeahead.length) { typeaheadOpen = false; }
                  else { onClose(); }
                }
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="Symbol (≥ 3 chars) — stocks, futures, options" autocomplete="off" />
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
                else if (e.key === 'Escape') { e.preventDefault(); onClose(); }
              }}
              class="field-input text-[0.7rem] py-1 px-2 flex-1"
              placeholder="Display name (optional) — e.g. Crude oil"
              autocomplete="off" />
          </div>
          {#if typeahead.length}
            <div class="search-typeahead">
              {#each typeahead as inst}
                <button onclick={() => onPickTypeahead(inst)}
                  class="search-typeahead-item">
                  <!-- displaySymbol renders GOLD_NEXT → GOLD.NEXT for virtual roots;
                       real contracts pass through unchanged. -->
                  <span class="font-mono text-[var(--c-action)]">{displaySymbol(inst.s)}</span>
                  <span class="text-[0.6rem] text-[var(--c-muted)] ml-2">{inst.e}{inst.virtual ? ' · virtual' : ''}</span>
                </button>
              {/each}
            </div>
          {/if}
          <div class="search-hint">
            Type ≥ 3 characters · Enter picks the first match · F&amp;O underlyings open the option chain picker
          </div>
        {/if}
      </div>
    </div>
</ModalShell>
