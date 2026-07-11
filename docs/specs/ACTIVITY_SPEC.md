# Activity Specification

Single source of truth for the activity log surface behavior across mount points,
tab switching, and filter state sharing. The activity system is a unified viewer
for system events, connection health, agent triggers, order fills, and terminal
output.

**Version**: 2.0 — 2026-07-11  
**Owner**: Platform  
**Key files**: `frontend/src/lib/data/activityStore.svelte.js` · 
`frontend/src/lib/LogPanel.svelte` · `frontend/src/lib/ActivityLogSurface.svelte`
· `frontend/src/lib/ActivityLogModal.svelte` · 
`frontend/src/routes/(algo)/activity/+page.svelte` · `backend/api/routes/admin.py`

---

## Contents

1. [Mount Points and Surface Variants](#1-mount-points-and-surface-variants)
2. [ActivityStore — Tab and Filter SSOT](#2-activitystore--tab-and-filter-ssot)
3. [Tab Catalog](#3-tab-catalog)
4. [LogPanel Component](#4-logpanel-component)
5. [ActivityHeaderFilters Component](#5-activityheaderfilters-component)
6. [Filter State Sharing](#6-filter-state-sharing)
7. [Deep-Link Tab Override](#7-deep-link-tab-override)
8. [Backend API Endpoints](#8-backend-api-endpoints)
9. [Layout and Responsiveness](#9-layout-and-responsiveness)
10. [Keyboard Shortcuts](#10-keyboard-shortcuts)
11. [Edge Cases](#11-edge-cases)
12. [Test Coverage Map](#12-test-coverage-map)

---

## 1. Mount Points and Surface Variants

Activity log appears in five contexts with different default behaviors and scope:

| Mount | Route | Tab Default | Filters | Scope |
|---|---|---|---|---|
| ActivityLogModal | Keyboard `h` or navbar Log button | Persisted (activityStore) | Shared (activityStore) | All tabs, shared with `/activity` |
| /activity page | `/activity` | Persisted (activityStore) | Shared (activityStore) | Full-page, bookmarkable |
| Orders activity card | `/orders` page sidebar | "order" | Local (per-card) | Orders tab only, isolated |
| Dashboard activity card | `/dashboard` | "order" (default) | Local (per-card) | All tabs, isolated |
| Inline panels | `/console`, `/automation`, others | Context-dependent | Local | Configurable tab subset |

**ActivityLogModal** — keyboard shortcut `h` or navbar "Log" button opens a modal overlay.
Tab selection persists in `activityStore` and is shared with `/activity` page. Closed via
Esc key or × button (overlay click doesn't close; modal stays focused). Renders all canonical
tabs.

**/activity page** — dedicated full-page route (`/activity`). Same `activityStore` tab +
filter state as the modal, so navigating to the page uses the last-selected tab and filters.
Bookmarkable (can be nested under `/activity?tab=conn` to deep-link to a specific tab).

**Orders activity card** — right-sidebar panel on `/orders` page showing Order tab only
(other tabs are hidden via the `tabs=` prop). Local filter state isolated from `activityStore`
so setting a filter in the card doesn't affect the modal.

**Dashboard activity card** — embedded on `/dashboard`, can show any subset of tabs (no
`tabs=` override defaults to all). Filter state is local and isolated per card instance.

**Inline panels** — components like `/console`, `/automation`, `/symbol-panel` pass custom
`defaultTab` and `tabs=` props to `ActivityLogSurface`, which forwards to `LogPanel`.
Each mount can hide irrelevant tabs (e.g., ReplayPanel might hide Agent/System/Conn) and
set its own poll cadence via `pollMs=`.

---

## 2. ActivityStore — Tab and Filter SSOT

File: `frontend/src/lib/data/activityStore.svelte.js` — module-level `$state` with
reactive getters and setters for all shared state. Consumed by `ActivityLogModal` and
`/activity` page exclusively (embedded cards have local state).

**Export: `activityStore` object**

```javascript
// Getters (reactive subscriptions)
activityStore.activeTab       // ActivityTab (string)
activityStore.accountFilter   // string[] — codes of selected accounts
activityStore.levelFilter     // 'all'|'error'|'warning'|'info'

// Setters (work with Svelte 5 bind: directives)
activityStore.activeTab = 'agent';
activityStore.accountFilter = ['ZG0790', 'DH3747'];
activityStore.levelFilter = 'error';
```

**Export: `ACTIVITY_TABS` constant** — canonical list of all tab identifiers.

```javascript
['order', 'agent', 'terminal', 'simulator', 'system', 'conn', 'news']
```

**Setter validation**: `activeTab` setter guards against unknown ids (returns silently
if id is not in `ACTIVITY_TABS`). Level and account setters accept any value.

**Persistence scope**: In-memory `$state` only (no localStorage write). Survives
modal open/close within a session; hard page reload resets to `activeTab='order'`,
`accountFilter=[]`, `levelFilter='all'`.

---

## 3. Tab Catalog

Seven tabs form the canonical activity surface. Tabs are selectable in `ActivityLogModal` /
`/activity` page; individual mounts can hide tabs via the `tabs=` prop.

| Tab | Data Source | Parsing | Content | Endpoint |
|---|---|---|---|---|
| **order** | Merged broker + algo orders | None (structured rows) | Order fills, modifications, cancellations across modes (SIM/PAPER/LIVE/SHADOW/REPLAY). Broker rows carry order_timestamp; algo rows carry created_at | `fetchOrders()` + `fetchAlgoOrdersRecent(100, 'all')` |
| **agent** | Agent event rows (DB) | Event type → level mapping | Agent fires, action success/fail, cooldown events. Level: action_failed/error → ERROR; cooldown/warn → WARNING; action_success/fired → INFO | `fetchRecentAgentEvents(100)` or `fetchSimEvents(100)` if sim mode |
| **terminal** | Command history + broker events | Mode prefix parsing `[SIM]`, `[PAPER]`, `[LIVE]`, `[SHADOW]`, `[REPLAY]` | Operator commands and their outputs; execution-mode prefixed when gated (when `gateByMode=true`) | Internal (`cmdHistory` prop) + `fetchOrders()` deduplication |
| **simulator** | Sim tick stream (DB) | Tick kind (price/fill/event) marker | Per-tick events from market simulator: price updates, simulated fills, bracket fills. Display shows timestamp, price, quantity | `fetchSimTicks(100)` (deferred load on first tab click) |
| **system** | Tail of API log file | Extract `[LEVEL]` token from line start | Infrastructure events: startup/shutdown, config reload, persistence mode changes, market hours updates. Lines parsed with regex `/(ERROR\|WARN(?:ING)?\|INFO\|DEBUG)/i` | `GET /api/admin/logs?n=200` (deferred on first click) |
| **conn** | Tail of conn_service log file | Extract `[LEVEL]` token from line start | Broker connection health: auth failures, ticker stale events, circuit breaker state, IPv6 binding, rebuild_from_db activity. Same line parsing as System tab | `GET /api/admin/logs/conn?n=200` (deferred on first click) |
| **news** | Static / curated feed | None | Market news, announcements, trading halts (deprecated; embedded on `/dashboard` instead). Legacy tab kept for backward compat | None (curated inline) |

**Level mapping** — used by filter dropdown to suppress rows:

| Tab | Level → Rows |
|---|---|
| Order | No per-row level (all INFO equivalent); filter by status instead (OPEN / COMPLETE / REJECTED / CANCELLED) |
| Agent | action_failed/error → ERROR; cooldown/warn/skip → WARNING; action_success/fired → INFO |
| Terminal | Inferred from result status (success → INFO, error → ERROR, warning → WARNING) or explicit prefix |
| Simulator | All INFO (no level distinction; can be enhanced) |
| System | Extracted from line: `[ERROR]` → ERROR, `[WARN]` → WARNING, `[INFO]` → INFO, `[DEBUG]` → DEBUG (else INFO) |
| Conn | Extracted from line (same as System) |
| News | No filtering (all shown) |

---

## 4. LogPanel Component

File: `frontend/src/lib/LogPanel.svelte` — the canonical log renderer. Encapsulates all
tab rendering, polling, filtering, and row actions. Mounted once per `ActivityLogSurface`
instance (modal, page, card).

**Props**

| Prop | Type | Default | Notes |
|---|---|---|---|
| `tabs` | `string[]` | `['order','agent','terminal','simulator','system','conn','news']` | Tab ids to display; omit tabs irrelevant to the mount (e.g., orders card passes `['order']`) |
| `defaultTab` | `string` | `'order'` | Initial active tab; overridden by parent when `onTabChange` is called |
| `pollMs` | `number` | `3000` | Poll cadence (ms) for each tab's data source; 0 disables polling (one-shot load) |
| `gateByMode` | `boolean` | `true` | When true, filter Order/Agent/Terminal/Simulator tabs by `$executionMode` store (hide non-matching rows); Ticks tab is hidden entirely when mode ≠ 'sim' |
| `accountFilter` | `string[] | undefined` (bindable) | `undefined` | Multi-select account filter; when provided (bound), parent owns state; LogPanel writes back when user changes filter. When undefined, LogPanel uses internal `_internalAccountFilter` |
| `levelFilter` | `'all'\|'error'\|'warning'\|'info'` (bindable) | `'all'` | Log level threshold; applied per-tab per section §3; when 'all' all rows pass; 'error' shows only ERROR rows, etc. |
| `multiColumn` | `boolean \| undefined` | `undefined` | Enable 2-column magazine layout (CSS `column-count: 2`) on Agent/Terminal/System/Conn tabs when viewport ≥900px. Explicit value overrides context-derived default |
| `availableAccounts` | `string[] | undefined` (bindable) | `undefined` | Derived from current order rows and mirrored to parent (ActivityLogModal uses this to populate its header dropdown) |
| `statusFilter` | `'all'\|'open'\|'complete'\|'rejected'\|'cancelled'` | `'all'` | Order tab status filter (from /orders page counter cards); applied before display |
| `symbolFilter` | `Set<string> \| null` | `null` | Order tab symbol scope (from /orders strategy filter); when null all symbols pass; empty Set means "show no rows"; non-empty Set filters to symbols in the set (upper case) |
| `simScope` | `boolean` | `false` | Scope Agent tab to sim-mode events only (when true, overrides `gateByMode` and forces `fetchSimEvents` instead of `fetchRecentAgentEvents`) |
| `mode` | `string \| null` | `null` | Execution mode override; when set, auto-flips tab (sim → 'simulator' tab; others → 'order' tab) and filters Order rows to matching mode. Fires only on mode CHANGE via `_lastMode` gate |
| `cmdHistory` | `Array<{status, message, fields?, time}>` | `[]` | Terminal tab command history (from CommandLineTab or other sources) |
| `hideInlineAccountFilter` | `boolean` | `false` | Hide the account dropdown in the tab row (set by ActivityLogModal which renders its own dropdown in the header instead) |
| `heightClass` | `string` | `'flex-1 min-h-0'` | Tailwind class for LogPanel height (flex-fill by default) |
| `onTabChange` | `(tab: string) => void` | `() => {}` | Callback when user clicks a tab; parent should update `defaultTab` and/or `activityStore.activeTab` |

**Key behaviors**

1. **Lazy polling** — System / Conn / Simulator tabs don't start polling until first activation
(prevents wasted requests on unvisited tabs). Agent / Order / Terminal tabs poll from mount.

2. **Mode auto-flip** — when `mode` prop changes (sim/paper/live/shadow/replay), tab
auto-switches and order rows are filtered to that mode. Uses `_lastMode` state to detect
change (prevents re-trigger on tab clicks).

3. **Account filtering** — three sources: `accountFilter` prop (preferred when provided);
internal `_internalAccountFilter` (fallback for local mounts); auto-populated
`_availableAccounts` from order rows. Account dropdown hidden when only 1 account in row set.

4. **Level filtering** — applied per-tab during $derived phases (before rendering). System /
Conn lines matched via regex `/(ERROR|WARN(?:ING)?|INFO|DEBUG)/i`; Agent rows via `_agentLevel()`;
Order rows not filtered by level (status filter used instead).

5. **Row deduplication** — Order tab merges broker rows + algo rows; broker rows win on
duplicate order_id. Newest-first sort via `order_timestamp` or `created_at`.

6. **Scroll preservation** — scroll position NOT persisted across mounts (reset per
ActivityLogSurface instance).

---

## 5. ActivityHeaderFilters Component

File: `frontend/src/lib/ActivityHeaderFilters.svelte` — header filter control strip used
by `ActivityLogModal` and `/activity` page. Single canonical component ensures consistent
UI across both mounts.

**Props**

| Prop | Type | Default | Notes |
|---|---|---|---|
| `accountFilter` | `string[]` (bindable) | `[]` | Selected account codes; parent owns state and threads to LogPanel |
| `levelFilter` | `'all'\|'error'\|'warning'\|'info'` (bindable) | `'all'` | Log level threshold; reflects what LogPanel is filtering; changes propagate back |
| `availableAccounts` | `string[]` | `[]` | Account codes currently in the log rows (from LogPanel's `_availableAccounts`) |

**Sub-components**

- **`ActivityAccountSelect`** — renders as `<AccountMultiSelect>` when `availableAccounts.length > 1`;
renders nothing for single-account systems. Dropdown shows available codes only; no persistence
of unavailable selections (different mounts have different row sets).

- **`<select>` level filter** — four options: All, Error, Warning, Info. Chrome matches
`AccountMultiSelect` (amber-on-slate gradient, same border/radius/font/height). Dropdown
persists in `activityStore` and applies across all tabs.

**Layout** — flex row pinned above LogPanel. Account dropdown uses `margin-left: auto` to
claim the spacer slot. On mobile (≤520px), dropdowns shrink (`max-width: 4.2rem` vs desktop
`5rem`, `6rem`) so modal header (title + account + level + close) fits on ≤360px without wrap.

**Rendering rules** — Account dropdown hidden when:
- `availableAccounts.length ≤ 1` (single account or demo mode)
- Level dropdown always renders (four options always available)

---

## 6. Filter State Sharing

**Shared** — `activityStore` is the SSOT for `AccountFilter` and `levelFilter` across
`ActivityLogModal` and `/activity` page. Both mounts bind to:

```javascript
bind:accountFilter={activityStore.accountFilter}
bind:levelFilter={activityStore.levelFilter}
```

When the operator changes a filter in the modal, navigating to `/activity` shows the same
filter state. Vice versa: filter in the page, close and reopen the modal, filter persists.

**Not shared** — Embedded mounts (Orders card, Dashboard card, inline panels) have local
filter state (don't bind to `activityStore`). This prevents a filter set in the modal from
affecting unrelated cards (e.g., setting a level filter in the Activity modal shouldn't
silence system logs in the Dashboard news card).

**Available accounts** — per-mount ephemeral (derived from current row set via LogPanel's
`_availableAccounts`). NOT persisted. A stale selection from a closed modal doesn't carry
to the page until the first poll populates new rows.

**Account dropdown behavior**:
- Dropdown renders only when `availableAccounts.length > 1`
- Options show current codes only (no historical or unavailable accounts)
- Empty selection (`[]`) means "show all accounts" (default)
- Implicit reset: switching tabs may show different account codes (Order tab has `account_id`;
Agent tab uses different schema). Dropdown options change; selected codes that are not in the
new available set are silently dropped (filter still applies to matching rows, if any)

**Level dropdown behavior**:
- Single-choice select with four options (All / Error / Warning / Info)
- Selection persists across tab changes and mounts (bound to `activityStore.levelFilter`)
- 'All' shows every row regardless of level
- 'Error' shows only ERROR-level rows (and hides INFO/WARNING/DEBUG)
- Changes apply immediately to the active tab

---

## 7. Deep-Link Tab Override

**Mechanism** — `openActivityModal(tab?)` function (exported from `stores.js`) accepts
optional tab override parameter.

**Behavior**

| Call | Result |
|---|---|
| `openActivityModal('conn')` | Open modal, force Conn tab (override persisted tab) |
| `openActivityModal('order')` | Open modal, force Orders tab |
| `openActivityModal()` (no arg) | Open modal, use persisted `activityStore.activeTab` (default) |
| `closeActivityModal()` | Close modal (no-op if already closed) |

**Usage examples**

- **BrokerHealthBadge** — when operator clicks a broker health row (red/amber state):
  ```javascript
  onclick={() => { open = false; openActivityModal('conn'); }}
  ```
  This overrides the persisted tab and shows Conn events directly without requiring manual
  tab click.

- **Order success flash** — when an order is placed, a toast could call:
  ```javascript
  openActivityModal('order')
  ```
  to show the order immediately in the Activity modal.

- **Keyboard shortcut `h`** — layout calls `openActivityModal()` with no argument, so the
  persisted tab selection is preserved across open/close cycles.

**Tab override semantics**

1. Operator has Agents tab selected (persisted in `activityStore`)
2. Operator clicks a broker health indicator (red dot)
3. `openActivityModal('conn')` is called → modal opens with Conn tab (override)
4. Operator manually switches to Orders tab inside the modal
5. Operator closes the modal (X or Esc)
6. Operator presses `h` again → modal reopens with Orders tab (now persisted)
7. Operator clicks broker health again → `openActivityModal('conn')` overrides again,
   forcing Conn tab

**Validation** — tab override is validated against `ACTIVITY_TABS` in `activityStore.set activeTab()`.
Unknown tab ids are silently ignored (setter returns early).

---

## 8. Backend API Endpoints

**System log tail**

```
GET /api/admin/logs?n=200
```

| Field | Value | Notes |
|---|---|---|
| `n` | `int` (default 200) | Number of lines to tail (clamped to max 2000) |
| **Response** | `LogsResponse` | `{ lines: string[], path: string }` |
| Lines | Newline-split tails from `.log/log_file` | Timestamps + log level tokens extracted in frontend |
| Path | Absolute file path | For debugging (file-not-found scenarios) |

**Connection service log tail**

```
GET /api/admin/logs/conn?n=200
```

| Field | Value | Notes |
|---|---|---|
| `n` | `int` (default 200) | Number of lines to tail (clamped to max 2000) |
| **Response** | `LogsResponse` | `{ lines: string[], path: string }` |
| Lines | Tail from conn_service log (`.log/conn_log_file` or fallback prod path) | KiteTicker, watchdog, rebuild_from_db events |
| Path | Resolved absolute path (prefer `/opt/ramboq/.log/conn_log_file`) | Conn service always runs from prod path even on dev API |

**Async execution** — both endpoints use `asyncio.create_subprocess_exec()` so `tail` runs
without blocking other routes. Timeout: 10s per tail command; returns 504 Gateway Timeout
on timeout.

**Error handling** — if log file doesn't exist, returns `LogsResponse` with sentinel message
and the resolved path.

**Agent events endpoint**

```
GET /api/agent-events/recent?limit=100
GET /api/agent-events/sim?limit=100
```

| Param | Type | Notes |
|---|---|---|
| `limit` | `int` (default 100) | Max rows to return |
| **Response** | Agent event rows | Fields: `id`, `event_type`, `triggered_at`, `timestamp`, `account`, `trigger_condition`, `detail` |

Sim endpoint returns `sim_mode=true` events only; real endpoint returns non-sim.

**Order rows endpoint** (merged broker + algo)

```
GET /api/orders
GET /api/algo-orders/recent?limit=100&mode=all|sim|paper|live|shadow|replay
```

| Field | Type | Notes |
|---|---|---|
| Broker rows | Order schema | `order_id`, `tradingsymbol`, `quantity`, `status`, `order_timestamp`, `account` |
| Algo rows | AlgoOrderInfo | `id`, `order_id`, `symbol`, `qty`, `status`, `mode`, `created_at`, `account` |
| Merged | Dedup on `order_id`; broker rows win | Sorted newest-first via `order_timestamp` or `created_at` |

**Simulator ticks endpoint**

```
GET /api/sim/ticks?limit=100
```

Response: Array of tick objects `{ ts: string, kind: 'tick'|'fill'|'event', symbol?: string, price?: number, qty?: number }`

**No pagination** — all endpoints return fixed-size arrays (last N rows). Frontend handles
infinite scroll via user tab clicks (refreshes on activation). Real-time streams via WebSocket
for agents at `/ws/algo` (separate channel, not activity log).

---

## 10. Keyboard Shortcuts

**`h` — open Activity modal**

Handler: `frontend/src/routes/(algo)/+layout.svelte _onGlobalKeydown()` (line 199–202).

```javascript
if (k.toLowerCase() === 'h') {
  e.preventDefault();
  openActivityModal();  // No tab arg — uses persisted tab
  return;
}
```

Behavior:
- Pauses when operator is typing in an input/textarea/select/contenteditable
- Esc defocuses the active field and allows `h` to fire on next press
- Opens modal with persisted `activityStore.activeTab` (last-used tab in this session)
- Subsequent presses while modal is open are no-ops (modal stays focused)

**Modal navigation** — Tab key cycles focus within the modal (first focusable ↔ last):

```javascript
if (e.key === 'Tab') {
  const els = _focusables();
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
}
```

Esc closes the modal via `stopImmediatePropagation()` so nested modals (ChartModal
stacked on ActivityLogModal) don't cascade (top modal closes first).

**Tab-switching shortcuts** — implemented inside LogPanel via custom ag-Grid
integration (not global):
- `j` — next row (when grid is focused)
- `k` — previous row (when grid is focused)
- `Enter` — open context menu on focused cell

**Discovery** — `?` (shift+/) opens a cheatsheet (not related to Activity surface).

---

## 9. Layout and Responsiveness

**Container breakpoint** — 900px viewport width is the canonical threshold for
multi-column vs single-column layout.

**LogPanel multi-column** (≥900px):
- Agent / Terminal / System / Conn tabs use CSS `column-count: 2` magazine layout
- Each row is a separate DOM node (not joined HTML strings), so text selection isn't wiped
on poll (audit defect fix)
- Timestamp, level tag, account code, and message all visible in each row
- On <900px: falls back to single-column (all fields on one line or wrapping)

**ActivityHeaderFilters mobile** (≤520px):
- Account dropdown: `max-width: 4.4rem` (vs desktop `6rem`)
- Level dropdown: `max-width: 4.2rem` (vs desktop `5rem`)
- Padding and caret SVG rescaled so both controls fit in modal header with title + close
on small phones (360px viewport)
- Operator feedback: "keep the drop downs on header without overlapping or pushing x button
on modal on mobile"

**ActivityLogModal modal** (≤640px):
- Full-width modal (`right: 0.25rem; left: 0.25rem`)
- Grid columns adjusted for smaller viewport: `grid-template-columns: 0.6rem 4rem 2.5rem 3rem 1fr 4rem` (vs desktop 5.5rem+ columns)
- Max height: `70vh` (vs desktop `min(90vh, 480px)`)

**Scroll behavior** — each tab's scroll position is per-instance (reset on LogPanel unmount).
Operator scrolls up in Conn tab, switches to System tab, returns to Conn tab → scroll position
is NOT restored (fresh scroll from bottom on new fetch). This is intentional: focus on latest
rows after a poll.

---

## 11. Edge Cases

### Tab persisted but no data in current session

Operator selects Agent tab; no agents have fired yet this session.
- LogPanel shows empty state: "No events"
- Not an error; operator can switch to other tabs or wait for an agent fire
- Agent tab polling starts immediately (not deferred); other data eventually arrives

### Modal opened before auth / data loads

Operator presses `h` while the page is still hydrating (auth token pending, data endpoints
not yet called).
- Modal renders with empty LogPanel
- Order/Agent/Terminal tabs show empty state
- System/Conn tabs haven't started polling yet (deferred until first click)
- Once auth and data fetches complete, rows render on next poll tick
- First poll for Agent/Order/Terminal fires immediately on mount

### Account dropdown with many accounts

System has 50+ accounts; dropdown gets unwieldy.
- `AccountMultiSelect` is searchable (type to filter options)
- `ActivityAccountSelect` hides when ≤1 account available
- Available accounts list is dynamic (changes on tab switch if row set differs)

### Switch tab, then navigate to /activity page

Operator in ActivityLogModal on Agent tab, clicks Orders tab, then navigates to `/activity`.
- Modal closes (navigation context lost)
- `/activity` page loads and reads `activityStore.activeTab` (which is now 'order')
- Page renders Orders tab (same persisted selection)

### Hard page reload (Cmd+R)

Session state in `$state` is lost.
- `activityStore` resets to defaults: `activeTab='order'`, `accountFilter=[]`, `levelFilter='all'`
- Modal doesn't re-open (modal state is purely component-local, not persisted)
- Operator lands on activity page (if they were on it) with Orders tab showing all levels

### SSE stream disconnects (transient network error)

WebSocket `/ws/algo` drops; browser auto-reconnects after backoff.
- Activity modal stays open
- Agent tab rows may briefly stale during reconnect window
- No duplicate rows on reconnect (frontend deduplicates via unique `key` per row in `_agentRows`)

### BrokerHealthBadge deep-link while modal is open

Modal is open on Agent tab; operator clicks a red broker health indicator.
- `openActivityModal('conn')` is called (with tab override)
- Modal is already open, so no second overlay (store-driven UI)
- Active tab flips from Agent → Conn immediately
- Operator sees Conn log for the specific account

### Account selection with mismatch (account in filter, not in current rows)

Operator filters to account ZG0790; System tab rows have no ZG0790 entries (system-wide logs).
- Filter still applies; no rows pass because account pattern match fails
- Dropdown shows ZG0790 as a selected option (persisted in `activityStore`)
- Operator can clear the filter or switch tabs (Order tab has the same ZG0790)

### Mode flip while on non-filterable tab

Mode changes to 'sim'; operator is viewing System tab.
- System tab is unaffected (no mode filtering)
- Order tab auto-flips if operator switches (due to `mode` prop handler)
- Simulator tab is hidden entirely when mode ≠ 'sim' (via `VISIBLE_TABS` filter)

---

## 12. Test Coverage Map

### Frontend — Playwright

- **Tab persistence**: Select 'agent' → close modal → reopen modal → 'agent' still active
- **Deep-link override**: `openActivityModal('conn')` shows Conn tab; manual switch to 'order' persists
- **Filter sharing**: Set account filter in modal → navigate to `/activity` page → filter still applied
- **Account dropdown**: Order tab shows order account codes; Agent tab shows distinct agent account codes
- **Level filter**: Select 'error' → all non-error rows hidden; switch tabs → filter still applied
- **Multi-column layout**: ≥900px shows Account/Level/Message/Timestamp columns; <900px condenses to one
- **Empty state**: No logs in current tab → shows "No events"; different tab shows data if available
- **LogPanel scroll**: Scroll down to load more rows; switch tabs; new tab scroll position independent

### Backend — pytest

- **agent_events schema**: `event_type`, `triggered_at`, `fired`, `detail`, per-agent logging
- **Order tab schema**: `order_id`, `symbol`, `fill_price`, `filled_at`, account mapping
- **System/Conn log formats**: Log lines with `[LEVEL]` token parse correctly; no token → level=info
- **Account filtering**: Query rows filtered by account_id IN (list); empty list returns all
- **Level filtering**: Query rows filtered by log_level >= threshold; 'all' returns unfiltered

---

### Frontend — Playwright

- **Tab persistence**: Select 'agent' → close modal → reopen modal → 'agent' still active
- **Deep-link override**: `openActivityModal('conn')` shows Conn tab; manual switch to 'order' persists
- **Filter sharing**: Set account filter in modal → navigate to `/activity` page → filter still applied
- **Account dropdown**: Order tab shows order account codes; Agent tab shows distinct agent account codes
- **Level filter**: Select 'error' → all non-error rows hidden; switch tabs → filter still applied
- **Multi-column layout**: ≥900px shows multi-column magazine (Agent/Terminal/System/Conn);
<900px condenses to single-column
- **Empty state**: No logs in current tab → shows "No events"; different tab shows data if available
- **LogPanel scroll**: Scroll down in Conn tab; switch to System tab; new tab scroll position independent
- **Keyboard shortcut**: Press `h` → modal opens with persisted tab; press Esc → modal closes
- **BrokerHealthBadge deep-link**: Click red indicator → `openActivityModal('conn')` forces Conn tab
- **Modal focus trap**: Tab key cycles focus within modal; Esc closes without affecting parent

### Backend — pytest

- **Agent events schema**: Endpoint returns `event_type`, `triggered_at`, `timestamp`, `account`,
`trigger_condition`, `detail` fields correctly formatted
- **Order rows schema**: Broker rows + algo rows merged; dedup on `order_id` works correctly
- **System/Conn log parsing**: Tail endpoint returns lines; level token regex `/(ERROR|WARN(?:ING)?|INFO|DEBUG)/i`
parses correctly; no token defaults to INFO
- **Account filtering**: Log line account pattern `\b([A-Z]{2}[0-9A-Z]{4})\b` extracts correctly;
filter predicate works for both Agent and System/Conn lines
- **Level filtering**: Agent rows map `event_type` to level; System/Conn rows extract level from
line prefix; filter gate works correctly for each

### Known gaps

- No e2e test for WebSocket reconnect on `/ws/algo` drop (transient network error scenario)
- No test for multi-modal stacking (ChartModal + ActivityLogModal simultaneously open)
- No performance test for 1000+ rows on multi-column layout
- Activity tab deep-link URL params not tested (only modal deep-link arg tested)

---

## Change log

| Date | Version | Change |
|---|---|---|
| 2026-07-11 | 2.0 | Comprehensive rewrite: added LogPanel + ActivityHeaderFilters component specs, backend
API endpoint shapes, 11 edge cases, keyboard shortcut wiring, test coverage map. Expanded from
§1-9 to §1-12. |
| 2026-07-11 | 1.0 | Initial spec from codebase audit; tab persistence, shared filter state, deep-link
override |
