# Plan: D/I/W/E level chips on all LogPanel tabs + alerts normalisation + refresh button in SymbolPanel header

## Task
1. Add a small color-coded level chip (D/I/W/E) to every row in every LogPanel tab except News. Each chip is a single letter that identifies the severity. Normalise all existing severity signals (conn-ev-*, log-row-*, log-agent-*) to map into the same 4-level scheme.
2. Normalise the Alerts page (/admin/alerts) to show the same D/I/W/E chip alongside the existing event-type chip.
3. Move the refresh button OUT of the outer CardHeader on /orders page INTO SymbolPanel's own .oes-header so it appears in both the orders page embed AND the modal popup.

## Agents
- backend: skip
- frontend: Implement all three changes below. Working directory: /Users/ramanambore/projects/ramboq/frontend.

  ### A — Level chip in LogPanel (frontend/src/lib/LogPanel.svelte + frontend/src/app.css)

  Add `_levelChipHtml(lv)` helper that returns:
  `<span class="lv-chip lv-${lv}">${lv.toUpperCase()}</span>`  where lv ∈ {d, i, w, e}.

  Modify `_logRow(timeInput, contentHtml, tagText, rowClass)` to accept optional 5th param `lv`
  and INSERT the chip immediately before `<span class="log-row-msg">`:
  `[ts][lv-chip][msg][tag]`

  Map every tab's existing severity signal → lv:

  **System / Terminal tab** (uses `_lineLevel()` → 'error'|'warning'|'info'):
  ```
  'error'   → 'e'
  'warning' → 'w'
  'info'    → 'i'
  ''|other  → 'd'
  ```
  Pass as 5th arg to _logRow in _sysRows builder (line ~357).

  **Agent tab** (uses cls: log-agent-failed | log-agent-success | log-agent-cooldown | log-agent-default):
  ```
  'log-agent-failed'   → 'e'
  'log-agent-cooldown' → 'w'
  'log-agent-success'  → 'i'
  default              → 'd'
  ```
  Pass as 5th arg to _logRow in _agentRows builder (line ~314).

  **Conn tab** (uses _connEvtCls() → 'conn-ev-red'|'conn-ev-amber'|'conn-ev-green'|'conn-ev-muted'):
  Add helper `_connEvtLv(evType)`:
  ```
  'conn-ev-red'   → 'e'
  'conn-ev-amber' → 'w'
  'conn-ev-green' → 'i'
  default         → 'd'
  ```
  Conn rows are rendered INLINE in the template (not via _logRow). In the `{#each _connEventRows}` template block find where the event row is rendered and prepend `{@html _levelChipHtml(_connEvtLv(e.event_type))}` before the message content.

  **Order tab** (uses _orderEvtCls() → 'log-row-error'|'log-row-warn'|'log-row-info'|'log-row-ok'|'log-row-debug'):
  Add helper `_orderEvtLv(kind)`:
  ```
  'log-row-error'       → 'e'
  'log-row-warn'        → 'w'
  'log-row-info'|'log-row-ok' → 'i'
  default (log-row-debug etc.) → 'd'
  ```
  Order rows use OrderCard component (not _logRow). Find the order rows template and add `{@html _levelChipHtml(_orderEvtLv(_orderEvtCls(e.kind ?? e.status)))}` as a leading chip before the row content. Check how OrderCard rows are rendered (~line 860+).

  **Simulator tab** (_renderSimLine → uses _logRow internally):
  In _renderSimLine, map entry.kind → lv and pass as 5th arg to every _logRow call:
  ```
  'started' → 'i'
  'stopped' → 'w'
  'order'   → 'i'
  'tick' with _classifySimLine='log-agent-triggered' → 'w'
  default   → 'd'
  ```

  Add to app.css (inside the log-panel section, near .log-row):
  ```css
  .lv-chip {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.1rem; height: 1.1rem; border-radius: 0.2rem;
    font-size: 0.6rem; font-weight: 700; line-height: 1;
    flex-shrink: 0; margin-right: 0.3rem; letter-spacing: 0;
    border: 1px solid transparent;
  }
  .lv-d { background: rgba(148,163,184,0.1); border-color: rgba(148,163,184,0.25); color: #64748b; }
  .lv-i { background: rgba(103,232,249,0.1); border-color: rgba(103,232,249,0.25); color: #67e8f9; }
  .lv-w { background: rgba(251,191,36,0.12); border-color: rgba(251,191,36,0.35); color: var(--c-action); }
  .lv-e { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.35); color: var(--c-short); }
  ```

  Also set `display: inline-flex` or `align-items: center` on `.log-row` so the chip aligns with the text (if not already set).

  ### B — Alerts page (frontend/src/routes/(algo)/admin/alerts/+page.svelte)

  Add `_alertLv(evt)` helper:
  ```
  'action_failed' → 'e'
  'cooldown'      → 'w'
  'triggered'     → 'w'
  'action_success'→ 'i'
  default         → 'd'
  ```
  In the table row where `<span class="ev-chip {_eventCls(r.event_type)}">` is rendered (line ~284), prepend the lv-chip:
  ```svelte
  <span class="lv-chip lv-{_alertLv(r.event_type)}">{_alertLv(r.event_type).toUpperCase()}</span>
  <span class="ev-chip {_eventCls(r.event_type)}">{_eventLabel(r.event_type)}</span>
  ```
  No CSS changes needed in alerts page — lv-chip styles live in app.css.

  ### C — Refresh button in the order MODAL's SymbolPanel header only

  The order modal is the SymbolPanel popup opened by the page-header Order button
  (rendered in frontend/src/lib/PageHeaderActions.svelte). It already shows a chart
  icon button in the `.oes-header`. Add a refresh button next to it.

  **Do NOT touch frontend/src/routes/(algo)/orders/+page.svelte at all.**

  In SymbolPanel.svelte:
  - Add prop `showRefresh = false` (boolean, default false so /orders embed is unaffected)
  - In the `.oes-header` button cluster (where showChart button lives), add:
    ```svelte
    {#if showRefresh}
      <button class="oes-refresh-btn" onclick={_refreshAll} title="Refresh">
        <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
          <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          <path d="M13.5 2v3.5H10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    {/if}
    ```
  - Style `.oes-refresh-btn` to match the existing chart/close button style in SymbolPanel.

  In PageHeaderActions.svelte, find the `<SymbolPanel` modal call and add `showRefresh={true}`.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): D/I/W/E level chips on all LogPanel tabs + alerts + refresh in SymbolPanel header

## Done when
- Every non-news LogPanel tab row shows a color-coded D/I/W/E chip left of the message
- Alerts page rows show matching lv-chip beside the ev-chip
- Refresh button is inside SymbolPanel's own header (appears in both orders page embed and order modal)
- Orders page CardHeader no longer has its own refresh button
- svelte-check 0 errors
