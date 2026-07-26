# Plan: Modal chrome + Payoff SSOT + Conn tab sync + System tab warnings + Groww ssot_fetch

## Context

Six post-deploy items:

**1. Log modal** (ActivityLogModal) wraps with `ModalShell` (centers panel via
`align-items:center`); canonical pattern is `.canonical-modal-overlay` (sheet below
navbar, `padding-top:var(--modal-sheet-top)`). LogPanel modal close button shows SVG
restore icon; user wants `×` matching ChartModal.

**2. Fullscreen card modal** — `inset:1.5rem` (floating gutters); should be
`inset:var(--modal-sheet-top) 0 0 0` (full-width sheet below navbar matching
canonical-modal-panel). DefaultSizeButton portals `.fs-modal-close-btn` to body — second
`×` to remove; in-card `×` button (DefaultSizeButton itself) is the one to keep.
Page-header fullscreen button not needed; remove with dead code.

**3. Payoff chart spot price not SSOT** — `fetchStrategyAnalytics(cleanLegs)` at
`derivatives/+page.svelte:3520` passes NO spot → backend calls `_resolve_spot()` which
hits the broker independently. `liveSpot` is already resolved at scope (line 1725).
`fetchStrategyAnalytics` already accepts `opts.spot` (`api.js:948`); backend
short-circuits broker call when provided (`options.py:1221`). One-line fix eliminates
the extra broker round-trip.

**4. Conn tab not in sync with system tab** — three gaps:
- *Timestamp*: conn uses IST-only `_fmtConnEvtTime()`; system uses `formatDualTz()`
  (IST + EDT, `stores.js:638`). Fix: replace `_fmtConnEvtTime` body with `formatDualTz`.
- *Magazine layout*: line 1611 explicitly excludes conn (`logTab !== 'conn'`).
  Fix: remove that exclusion.
- *Colors*: conn applies color only to `.lp-conn-type` span; system colors full row.
  Fix: target `.lp-conn-row.conn-ev-*` directly (not child span).

**5. System tab too many warnings** — root cause: `file_log_level: 10` (DEBUG) in
`backend/config/backend_config.yaml` captures every broker operation (token renewals,
reconnection backoffs, rate limits) into the log file that the system tab tails
(`/api/admin/logs` at `admin.py:1085`). Fix: raise `file_log_level` from `10` to `20`
(INFO level) — drops DEBUG-level chatter while keeping INFO, WARNING, ERROR. Additionally,
initialize the system tab levelFilter to `'warning'` by default (not `'all'`) so
operators see only actionable rows; they can toggle to 'all' or 'info' for more.

**6. Groww instruments ssot_fetch key** — fixed string `"groww_instruments"` shared
across all GrowwBroker instances. Fix: lambda key per `self.account`.

## Agents

- backend: One change in `backend/config/backend_config.yaml`:
  - Change `file_log_level: 10` → `file_log_level: 20`
  - (Raises log file level from DEBUG to INFO — removes broker DEBUG chatter;
    WARNING/ERROR/INFO still captured and visible in system tab)

- frontend: All changes in `frontend/src/`:

  **A — `lib/ActivityLogModal.svelte`** — replace ModalShell with canonical-modal-overlay:
  - Remove `import ModalShell from '$lib/ModalShell.svelte';`
  - Add `import { portal } from '$lib/portal';`
  - Replace `<ModalShell open={true} {onClose} zIndex={10500} ...>` + closing tag with:
    ```svelte
    <!-- svelte-ignore a11y_interactive_supports_focus -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="canonical-modal-overlay" style="z-index:10500"
         role="dialog" aria-modal="true" aria-label="Activity log"
         use:portal>
    ```
    close with `</div>`. Keep `_onKey`, `_focusables`, `onMount`, `onDestroy` unchanged.

  **B — `lib/LogPanel.svelte`** — four changes:

  B1. Modal close button → `×` (around lines 1465-1477):
  - In `{#if context === 'modal'}` replace `.lp-default-btn` SVG button with:
    ```svelte
    <button type="button" class="lp-close-btn"
            title="Close" aria-label="Close activity log"
            onclick={() => onClose?.()}>×</button>
    ```
  - Add `.lp-close-btn` style (cyan palette, 1.4rem × 1.4rem, matching `.cm-close` in ChartModal).
  - Remove `.lp-default-btn` style block if no longer used elsewhere in LogPanel.

  B2. Conn tab timestamp → dual IST+EDT (around lines 428-436):
  - Import `formatDualTz` from `$lib/stores.js` (exported at line 638).
  - Rewrite `_fmtConnEvtTime(iso)` to return `formatDualTz(new Date(iso))`.
  - Expand `lp-conn-time` min-width CSS to at least `13rem` to fit the longer string.

  B3. Conn tab magazine layout (line 1611):
  - Remove `logTab !== 'conn'` from the multicol conditional:
    ```svelte
    {multiColumn && logTab !== 'order' ? 'lp-multicol' : ''}
    ```
  - Check if `white-space: nowrap` on `.lp-conn-row` breaks 2-column grid;
    if so, allow wrapping on `lp-conn-time` and `lp-conn-det` spans.

  B4. Conn tab full-row colors (around lines 2361-2364):
  - Change selectors from targeting only `.lp-conn-type` to targeting the full row:
    ```css
    .lp-conn-row.conn-ev-green { color: var(--c-long); }
    .lp-conn-row.conn-ev-red   { color: var(--c-short); }
    .lp-conn-row.conn-ev-amber { color: var(--c-action); }
    .lp-conn-row.conn-ev-muted { color: var(--algo-muted); }
    ```

  B5. System tab default levelFilter → 'warning' (line 125):
  - In `ActivityLogSurface.svelte` (line 58), change `levelFilter` default from `'all'`
    to `'warning'`. The levelFilter UI chip remains so operator can toggle to 'info' or
    'all'. Note: conn tab green events (`conn-ev-green`) map to 'info' level
    (`lines 368-370`), so with default 'warning', conn shows only amber/red by default.
    This is acceptable since the user's concern is noise reduction. Operator can toggle.

  **C — `lib/DefaultSizeButton.svelte`** — remove portalled .fs-modal-close-btn:
  - In `$effect` remove the `closeBtn` createElement / appendChild block + cleanup.
  - Keep backdrop, scroll-lock, Esc handler unchanged.

  **D — `app.css`** — fix `.fs-card-on` + remove dead CSS:
  - Change `inset: 1.5rem !important;` →
    `inset: var(--modal-sheet-top, calc(3rem + 1.8rem)) 0 0 0 !important;`
  - Change `border-radius: 0.75rem !important;` → `border-radius: 0 !important;`
  - Remove `.fs-modal-close-btn { … }` + `.fs-modal-close-btn:hover { … }` block (~19 lines)
  - Remove `.fs-x-icon { … }` rule
  - Update `@media (max-width:600px) .fs-card-on` override with same inset formula

  **E — Remove page-header fullscreen button**:
  - `lib/PageHeaderActions.svelte`: remove import + `<PageFullscreenButton />`.
  - `lib/CardHeader.svelte`: remove `import { activeCardStore }`, `_onCardFocus()`, `onmouseenter={_onCardFocus}`.
  - `lib/stores.js`: remove `activeCardStore` export.
  - Delete `lib/PageFullscreenButton.svelte`.
  - `e2e/fullscreen_card_modal.spec.js`: remove test 5 (`page_header_fullscreen_button_expands_active_card`) and test 9 (`stale_code_fullscreen_button_still_creates_close_btn`).

  **F — `routes/(algo)/admin/derivatives/+page.svelte:3520`** — payoff SSOT fix:
  - Change:
    ```js
    const resp = await fetchStrategyAnalytics(cleanLegs);
    ```
    To:
    ```js
    const resp = await fetchStrategyAnalytics(cleanLegs, { spot: liveSpot ?? null });
    ```

- broker: One-line fix in `backend/brokers/adapters/groww.py`:
  ```python
  @ssot_fetch(mode="coalesce", key=lambda self, *a, **kw: f"groww_instruments_{self.account}")
  ```

- doc: skip
- backend-test: Update `backend/tests/broker/test_source_ip_overlay.py` — add multi-instance
  Groww test: two GrowwBroker instances, concurrent `instruments()` → each hits SDK
  independently (distinct cache keys, no cross-account collision).

- playwright: skip

## Tests

- pytest: yes (broker tests: `venv/bin/pytest backend/tests/broker/ -q --tb=line`)
- svelte-check: yes
- playwright: no

## Commit message

fix: modal sheet + payoff SSOT + conn tab sync + system log noise + Groww ssot_fetch key

## Done when

- ActivityLogModal: canonical-modal-overlay (sheet below navbar, not centered)
- LogPanel modal close: `×` cyan button
- DefaultSizeButton: no portalled .fs-modal-close-btn
- .fs-card-on: starts below navbar, border-radius 0
- PageFullscreenButton deleted, activeCardStore removed
- fetchStrategyAnalytics passes liveSpot — payoff instant (no extra broker call)
- Conn tab: dual IST+EDT timestamps, magazine layout, full-row colors
- System tab: default levelFilter 'warning'; file_log_level raised to INFO (20)
- Groww instruments(): per-account lambda key
- pytest broker tests pass; svelte-check 0 errors
