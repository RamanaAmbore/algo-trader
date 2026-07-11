# Activity Specification

Single source of truth for the activity log surface behavior across mount points,
tab switching, and filter state sharing. The activity system is a unified viewer
for system events, connection health, agent triggers, order fills, and terminal output.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/data/activityStore.svelte.js` · `frontend/src/lib/LogPanel.svelte` · `frontend/src/lib/ActivityLogSurface.svelte` · `frontend/src/routes/(algo)/activity/+page.svelte`

---

## Contents

1. [Mount Points and Surface Variants](#1-mount-points-and-surface-variants)
2. [ActivityStore — Tab and Filter SSOT](#2-activitystore--tab-and-filter-ssot)
3. [Tab Lifecycle and Persistence](#3-tab-lifecycle-and-persistence)
4. [Log-Level Parsing per Tab](#4-log-level-parsing-per-tab)
5. [Filter State Sharing](#5-filter-state-sharing)
6. [Layout and Responsiveness](#6-layout-and-responsiveness)
7. [Deep-Link Tab Override](#7-deep-link-tab-override)
8. [Edge Cases](#8-edge-cases)
9. [Test Coverage Map](#9-test-coverage-map)

---

## 1. Mount Points and Surface Variants

Activity log appears in four contexts with different default behaviors:

| Mount | Route | Tab Default | Filters | Full UI |
|---|---|---|---|---|
| ActivityLogModal | Navbar "Log" | Persisted (activityStore) | Shared (activityStore) | Yes — all tabs |
| Activity card | `/admin/execution` | Persisted | Shared | No — collapsed until expanded |
| Orders card | `/orders` page | "order" (forced) | Per-card local | No — card-level only |
| /activity page | `/activity` | Persisted | Shared | Yes — bookmarkable, full-screen |

**ActivityLogModal** and `/activity` page both use `activityStore` as SSOT. Selecting a tab
in the modal persists the choice; navigating to `/activity` uses the persisted tab.

**Orders card** (`/orders` page inline activity) always shows Orders tab; local filter state.

**Admin card** (`/admin/execution`) shows Activity card with default "order" tab; may override per-product need.

---

## 2. ActivityStore — Tab and Filter SSOT

**`activityStore.svelte.js`** exports reactive getters/setters backed by module-level `$state`:

```javascript
activityStore.activeTab       // string (reactive getter)
activityStore.accountFilter   // string[] (reactive getter)
activityStore.levelFilter     // 'all'|'error'|'warning'|'info' (reactive getter)

activityStore.activeTab = 'agent';  // setter routes to _activeTab
activityStore.setAccountFilter(['ZG0790', 'ZG7890']);
activityStore.setLevelFilter('error');
```

**Tab list** (`ACTIVITY_TABS`):
- 'order' — order fills, cancellations, modifications
- 'agent' — agent fires, actions (from agent_events)
- 'terminal' — parsed command interpreter output
- 'simulator' — SIM mode tick/execution markers
- 'system' — infrastructure: startup, config reload, persistence mode
- 'conn' — connection health: broker auth, ticker stale, failover
- 'news' — legacy news feed (deprecated; embedded on dashboard)

---

## 3. Tab Lifecycle and Persistence

**Persistence**: Persisted to browser's local state (no localStorage write; in-memory $state survives page reloads within a session).

**Tab switching behavior**:
1. User clicks tab in ActivityLogModal ("Agent")
2. `activeTab = 'agent'` writes to activityStore
3. Immediately updates both LogPanel display AND localStorage (if enabled for recovery across hard-reload)
4. Modal stays open; tab persists when modal closes and reopens
5. Navigating to `/activity` page uses the same persisted tab (no modal open, but store is shared)

**Reset behavior**: Navigating away from `/activity` and `/admin/execution` does NOT reset the tab.
Closing the ActivityLogModal (X button) also does NOT reset. Tab choice is sticky across
session while at least one mount point is active.

**Cold-start default**: If user has never selected a tab (cold session), default to 'order'.

---

## 4. Log-Level Parsing per Tab

**Log parsing rules** (per tab):

| Tab | Parsing Rule | Example |
|---|---|---|
| System | Extract `[LEVEL]` prefix from log line | `[INFO] Market opened: NSE` → level=info, msg=`Market opened…` |
| Conn | Extract `[LEVEL]` prefix | `[ERROR] Ticker stale for 35s` → level=error, msg=`Ticker stale…` |
| Agent | Map `event_type` from agent_events row | `event_type='fire'` → level=info; `event_type='error'` → level=error |
| Order | No token parsing; all info from row schema | All orders show level=info; filter by status instead |
| Terminal | Extract `[LEVEL]` or infer from content | Command success → info; syntax error → error |
| Simulator | Extract `[LEVEL]` or parse marker (tick/fill) | `[TICK] NIFTY 18500.0` → info, type=tick |
| News | Static content; no level parsing | Always renders as-is; filter not applicable |

**Fallback**: If a log line has no `[LEVEL]` token, assume level=info (default).

---

## 5. Filter State Sharing

**Shared across** ActivityLogModal and `/activity` page only.

**Not shared** with Orders card or Admin card (those have local, isolated filter state).

**Filter fields**:
- `accountFilter` (string[]): Multi-select accounts displayed in current logs; [] = all
- `levelFilter` ('all'|'error'|'warning'|'info'): Hide logs below this level

**Account dropdown** in ActivityHeaderFilters reads `availableAccounts`:
- Derived per-mount (ephemeral) from current row set (order rows for Order tab, agent rows for Agent tab, etc.)
- NOT persisted (different mounts have different account sets)
- Selector shows available accounts only; a closed mount's account list is not cached

**Level dropdown**: Single choice; persisted in activityStore and shared across mounts.

**Implicit reset**: Switching tabs may show different available accounts (Order tab has account_id;
Agent tab uses different schema). Dropdown resets to "show all" implicitly. Persisted levelFilter
carries over between tabs.

---

## 6. Layout and Responsiveness

**LogPanel** (rendered inside ActivityLogSurface):
- ≥900px viewport width: Multi-column layout (Account, Level, Message, Timestamp)
- <900px: Single-column condensed (all fields in one line or vertical stack)

**Single-column sizing**: Message truncated to fit viewport; hover shows full text in tooltip.

**ActivityHeaderFilters** (account + level dropdowns):
- Pinned above LogPanel
- Account dropdown populated from available rows in current tab
- Level dropdown always shows all four options ('all', 'error', 'warning', 'info')

**Scroll behavior**:
- Logs loaded on-demand (infinite scroll or fixed page size, depending on backend pagination)
- Activity stores scroll position in memory (NOT localStorage) — reset on tab switch or mount unmount

---

## 7. Deep-Link Tab Override

**Deep-link override syntax**: `openActivityModal(tab?)` accepts optional tab parameter.

**Behavior**:
- `openActivityModal('conn')` → open modal and override persisted tab to 'conn'
- `openActivityModal()` (no arg) → open modal and use persisted tab (default)
- Called by BrokerHealthBadge → `openActivityModal('conn')` to show connection events directly

**Example flow**:
1. Operator viewing `/pulse`, clicks BrokerHealthBadge indicator
2. `openActivityModal('conn')` is called
3. Modal opens with Conn tab (even if persisted tab was 'agent')
4. Inside modal, operator switches to 'order' manually
5. Next time modal opens (without deep-link), 'order' is persisted and shown
6. Next BrokerHealthBadge click calls `openActivityModal('conn')` again, overriding to 'conn'

---

## 8. Edge Cases

### Tab persisted but no data in current session

- Operator selects 'agent' tab; no agents have fired yet this session
- LogPanel shows empty message: "No agent events yet"
- Not an error; user can switch to other tabs or wait for an agent to fire

### Modal opened before data loads

- Operator immediately clicks "Log" while `/api/*` routes are still hydrating
- Modal shows loading spinner or empty state
- Once data arrives, LogPanel renders rows and filter dropdowns populate

### Account dropdown with many accounts

- 50+ accounts in the system; dropdown gets unwieldy
- AccountFilter uses a searchable multi-select (FilterIcon + SearchInput)
- Operator can type to filter account list

### Switch tab, then switch surface (modal → page)

- Operator in ActivityLogModal, selects 'agent' tab
- Closes modal (X button); navigates to `/activity` page
- Page loads with 'agent' tab active (same persisted state)
- Filter state (account + level) also carries over

### Hard-reload (Cmd+R) clears session

- $state is in-memory; hard reload resets to defaults
- First mount re-initializes activeTab='order', filters=[]
- Not a defect; expected browser-refresh behavior

---

## 9. Test Coverage Map

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

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; tab persistence, shared filter state, deep-link override |
