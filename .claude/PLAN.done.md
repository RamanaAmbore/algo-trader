# Plan: LogPanel consistency batch — Console rename+wiring, row/header height audit, Agents-tab scope, oversized detail payloads

## Task
Six linked fixes, all investigated and root-caused directly (read-only) this session:

1. **Agents tab must not show manually-placed orders.** `record_manual_event()`
   (`backend/api/algo/agent_engine.py:3701`, called from `orders_place.py` lines
   1885/2014/2270) writes a real `agent_events` row attributed to the built-in
   `MANUAL_AGENT` (slug=`"manual"`) sentinel for every manually-placed order. This
   sentinel exists purely so order-origin labeling elsewhere (alerts, fill
   notifications — "any order placed manually should show manual") has an
   `agent_id` to key off. But it also means every manual order shows up as if an
   automation agent fired, polluting the Agents tab. Fix: exclude rows whose
   resolved agent slug is `"manual"` from the Agents-tab listing — at the backend
   query level (`backend/api/routes/agents.py`, wherever `AgentEventInfo` rows are
   listed) via a join/filter on `Agent.slug != 'manual'`, not a frontend filter
   (frontend has no slug on `AgentEventInfo`, only `agent_id: int`). Do NOT touch
   `record_manual_event()` itself or remove the sentinel — other consumers
   (alert-origin labeling) still need it.

2. **Oversized/unreadable `detail` payload on some Agents/Alerts rows.** A
   margin/funds-threshold agent's `EvalResult.detail` (`agent_engine.py:741`,
   `_v2_build_evalresult`) stores the FULL `matches`/`latched_matches` lists —
   each match dict can carry a nested `row`/`_raw` broker payload per account
   (fno_margin_details, commodity_margin_details, etc.). `chipsFromJson()`
   (`frontend/src/lib/logChips.js`) already drops nested objects and
   `.join(',')`s arrays, but (a) an array of objects currently renders as
   useless `[object Object],[object Object]...` junk instead of a count, and
   (b) if `JSON.parse` fails for any reason the fallback dumps the ENTIRE raw
   string verbatim with no length cap — this is what produced the screenshot's
   multi-line wall of raw nested JSON. `matches`/`latched_matches` must stay in
   the backend `detail` field unchanged (hydration in `_hydrate_latch_from_rows`
   reads them) — fix is display-only, frontend-side:
   - `chipsHtml()`: when a value is an array whose elements are objects, render
     `"N items"` instead of `.join(',')`.
   - `chipsFromJson()`: cap the raw-string fallback (parse-failure path) to a
     fixed length (e.g. 200 chars) with a trailing `…`, so a malformed/oversized
     payload can never flood a row regardless of root cause.

3. **Legs underlying-anchor row is taller than real leg rows.**
   `.cand-row` (`CandidateLegRow.svelte:616`) pins to `min-height:
   var(--grid-row-height)` (the row-height SSOT token) with `align-items:
   stretch` and 0.2rem/4px span padding. `.cand-row-underlying-anchor`
   (`+page.svelte:7697`) has no `min-height` at all and uses larger 0.32rem/
   0.45rem span padding — taller than every real row. Fix: add
   `min-height: var(--grid-row-height); box-sizing: border-box; align-items:
   stretch;` to `.cand-row-underlying-anchor` and change its `> span` padding
   to match `.cand-row > span` (`0.2rem` top/bottom, `4px` left/right).

4. **ag-Grid header height must equal row height, everywhere.** Confirmed via
   grep: `headerHeight` is never set on ANY ag-Grid instance in the codebase.
   `rowHeight` IS explicitly set in two places: `mkBaseGridOpts()`
   (`frontend/src/lib/data/algoGridUtils.js:132`, dark-theme SSOT used by all 4
   MarketPulse grid instances: `26` desktop / `34` mobile) and
   `PerformancePage.svelte:910` (its own deliberately-independent calc: `26`/`34`
   dark, `22`/`32` light — comment explains this divergence is intentional, do
   not collapse it into `mkBaseGridOpts`). Fix: add `headerHeight` equal to the
   same computed `rowHeight` value at both sites — one line in `mkBaseGridOpts`
   (`headerHeight: _isMobile ? 34 : 26`), one line in PerformancePage's existing
   IIFE (return the same `rowH` for headerHeight too, next to where it already
   sets `--ag-row-height`).

5. **Legs anchor row's "X" badge is hard to read — must match Positions'
   anchor "X" exactly.** Code inspection shows `positionStateBadge()` already
   returns the identical `color: var(--algo-muted)` / `bg: var(--algo-muted-bg)`
   pair for both surfaces, applied identically via `style:color`/`style:background`
   inline — the markup is not wrong. The visibility gap is almost certainly
   `--algo-muted-bg`'s low alpha (`rgba(126,151,184,0.14)`) compositing with the
   anchor row's own `--c-underlying-bg` purple tint (`rgba(192,132,252,0.10)`)
   underneath, washing out contrast in a way that doesn't happen on Positions'
   plain dark row background. Fix: bump the badge background specifically for
   the anchor state to a stronger token (`--algo-muted-bg-strong`,
   `rgba(126,151,184,0.22)`, already defined in `app.css`) — scoped to BOTH
   Positions and Legs anchor rows via `positionStateBadge.js`'s `anchor` branch
   (single source, so both stay in sync by construction). Verify in a real
   browser after the change (not just computed-style assertion) since this is a
   perceptual contrast fix.

6. **Rename "Terminal" tab → "Console"; make it a real audit trail of CLI
   input/output; re-sequence tabs.** `frontend/src/routes/(algo)/automation/+page.svelte`
   already has its OWN page-level "Console" tab (CLI grammar text editor for
   WHEN/GIVEN/ALERT/DO statements) — this is the real CLI surface. LogPanel's
   `cmdHistory` prop (`{status, message, fields, time}[]`) already exists and
   is specifically designed to feed the Terminal tab's `_terminalCmdLines()`,
   but confirmed via grep: NOTHING in the whole frontend ever populates it —
   every caller passes `[]`. The backend CLI-interpret endpoint
   (`api.js:interpretAgent` → `POST /agents/interpret`) is also confirmed
   dead — zero frontend callers. So today's "Terminal" tab is just Orders+Agents
   data reformatted, with no unique content. Fix:
   - Rename the tab everywhere: `LogPanel.svelte` tabs array/labels
     (`'terminal'` → `'console'`, `'Terminal'` → `'Console'`), `_terminalCmdLines`/
     `_terminalOrderLines`/`_terminalAgentLines` helper names stay (internal,
     not user-facing) or rename for clarity at the implementer's discretion,
     any CSS class/test selector referencing `terminal`.
   - Wire real input: add a `_cmdHistory` `$state` array at the
     `automation/+page.svelte` page level, push one entry (✓/✗, message,
     timestamp) whenever the operator's Console-tab CLI text is compiled/
     saved/tested (the existing `_editRow`/compile-decompile success/failure
     paths around line 320-370 — reuse those exact outcomes, don't invent a
     new signal), and pass `cmdHistory={_cmdHistory}` down through
     `ActivityLogSurface` into `LogPanel`.
   - Re-sequence LogPanel's tabs from `['order','agent','alerts','terminal',
     'simulator','system','conn','news']` to
     `['order','agent','alerts','system','conn','console','simulator','news']`
     — groups "what the trading system did" (order/agent/alerts), then "infra
     health" (system/conn), then "what the operator did" (console), then
     simulator (a distinct non-live mode), with news last (passive reference).

## Agents
- frontend: Implement items 2 (chipsHtml/chipsFromJson caps), 3 (anchor row
  height), 4 (headerHeight), 5 (anchor badge contrast token), and 6 (tab
  rename + re-sequence + cmdHistory wiring). Self-contained brief: read this
  plan's items 2–6 above for exact file/line targets; each is independently
  testable.
- backend: Implement item 1 (Agents-tab query excludes `agent.slug == 'manual'`
  rows) — read this plan's item 1 above for exact file/function targets.
- doc: skip (no spec/guide changes needed — these are display/UX fixes, not
  behavioral contract changes)
- backend-test: Add/extend a pytest covering the Agents-tab listing query
  excludes manual-origin events (new test or extend existing agents-route
  test file).
- playwright: Add/extend specs: (a) anchor row height equals real row height
  in Legs grid, (b) ag-Grid headerHeight equals rowHeight on a MarketPulse
  grid and on PerformancePage, (c) Console tab renders where Terminal used to
  (renamed), tab order matches the new sequence, (d) a compiled CLI statement
  in automation's Console tab produces a new Console-tab (LogPanel) entry,
  (e) chipsFromJson truncates an oversized/malformed detail string and never
  dumps raw multi-KB JSON into a row.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
fix(logpanel): rename Terminal->Console + wire CLI audit trail, fix anchor row height, ag-Grid header=row height, manual-order noise in Agents tab, oversized detail payload truncation

## Done when
- Agents tab no longer shows any manually-placed-order event.
- No Agents/Alerts row can render more than ~200 chars of raw JSON even on a
  malformed/oversized detail payload; arrays of objects render as a count.
- Legs underlying-anchor row's height visually matches every real leg row
  (same `--grid-row-height` token).
- Every ag-Grid instance's header height equals its own row height (26/34
  dark, 22/32 PerformancePage light — each theme's own value, just now
  mirrored onto headerHeight).
- Legs anchor "X" badge is visibly legible against the purple underlying-row
  tint, matching Positions' own anchor "X" exactly (same token, both surfaces).
- Tab labeled "Console" (not "Terminal"), positioned after System/Conn and
  before Simulator; compiling a CLI statement on the automation page's own
  Console tab produces a visible entry in LogPanel's Console tab.
- pytest + svelte-check + playwright all green.
