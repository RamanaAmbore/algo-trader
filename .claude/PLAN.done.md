# Plan: Unified AppMessage — DB-backed Notification + Log System + Order Book Panel

## Context
Three related changes in one plan:

1. **AppMessage system** — DB-backed `app_messages` table + dispatcher + `/api/messages` REST
   endpoint. All sinks (ntfy, Telegram, email) and LogPanel tabs route through it. Tags are
   metadata only — never displayed. News/terminal/simulator tabs excluded.

2. **Order Book panel** — Order cards currently live in the LogPanel "order" tab, which has a
   different visual format from every other tab. Extract cards into a dedicated `OrderBook.svelte`
   panel shown on the Orders page and in the Order modal. The LogPanel "order" tab becomes a
   plain text log feed (AppMessage rows tagged `order`), consistent with agent/system/conn tabs.
   LogPanel is removed from the Orders page and Order modal.

3. **Consolidated post-market cron** — Replace the current N individual post-market background
   tasks (each running an infinite poll loop checking time) with a single `_task_post_market_cron`
   orchestrator. It fires the right sub-jobs at the right windows; individual task functions
   become helpers called by the orchestrator.

## Constraints
- No changes to chase.py, template_attach.py, agent_engine.py, alert_utils.py internals
- Dispatcher is always fire-and-forget from callers; DB/sink failures never propagate
- `news`, `terminal`, `simulator` tabs excluded from AppMessage system
- Order card rendering logic is preserved — just moved to OrderBook.svelte
- Post-market task logic is preserved — just consolidated under one orchestrator loop

## Agents
- backend: AppMessage model + migration + dispatcher + REST endpoint + register route +
  consolidated post-market cron + 2 initial callsites (deploy + close summary)
- frontend: OrderBook.svelte (extract from LogPanel order tab) + wire to Orders page +
  wire to Order modal + remove LogPanel from Orders page/modal +
  update LogPanel order tab to poll /api/messages?tags=order
- backend-test: tests for dispatcher, endpoint, and cron orchestrator
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

---

## Part 1 — AppMessage Backend

### 1a. ORM Model (`backend/api/models.py`)

```python
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

class AppMessage(Base):
    __tablename__ = "app_messages"

    id:           Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    created_at:   Mapped[datetime]       = mapped_column(
                      DateTime(timezone=True),
                      default=lambda: datetime.now(timezone.utc), index=True)
    level:        Mapped[str]            = mapped_column(String(10), nullable=False)
    tags:         Mapped[list]           = mapped_column(ARRAY(String), nullable=False)
    title:        Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    body:         Mapped[str]            = mapped_column(Text, nullable=False)
    account:      Mapped[Optional[str]]  = mapped_column(String(50), nullable=True, index=True)
    symbol:       Mapped[Optional[str]]  = mapped_column(String(50), nullable=True)
    data:         Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    retain_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    __table_args__ = (
        Index("ix_app_messages_tags", "tags", postgresql_using="gin"),
        Index("ix_app_messages_retain", "retain_until",
              postgresql_where=text("retain_until IS NOT NULL")),
    )
```

### 1b. Migration (`backend/api/database.py`)

Add `async def _migrate_app_messages_table(conn)` using raw idempotent SQL
(`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). Call it from `init_db()`
after the last existing migration.

### 1c. Dispatcher (`backend/shared/helpers/app_message.py`) — new file

```python
@dataclass
class AppMessage:
    body:    str
    tags:    list[str]
    level:   str            = "info"   # info | warning | error | critical
    title:   str            = ""
    account: Optional[str]  = None
    symbol:  Optional[str]  = None
    data:    Optional[dict] = field(default=None)

async def dispatch(msg: AppMessage) -> None:
    """Write to DB; fan-out to sinks fire-and-forget. Swallows all errors."""

def fire(msg: AppMessage) -> None:
    """Sync entry point — schedules dispatch on the running event loop."""
```

**Retention rules (set at write time):**
| level | Retention |
|-------|-----------|
| critical / error | Permanent (retain_until = NULL) |
| warning | 90 days |
| info — order/chase/template/agent | 30 days |
| info — broker/conn | 14 days |
| info — system/deploy/market | 7 days |

**Sink routing** (fire-and-forget via `asyncio.create_task`):
- error/critical → ntfy urgent
- warning + agent tag → ntfy default
- deploy/alert tag → ntfy always
- Telegram/email: reuse existing `_alert_route` for now; full migration in Phase 2

### 1d. REST endpoint (`backend/api/routes/messages.py`) — new file

```
GET /api/messages?tags=order,chase&limit=500&since=<ISO>&account=<str>
```
- `tags` filter uses PostgreSQL `&&` (array overlap)
- `limit` capped at 500
- `since` filters `created_at >`
- Returns `list[AppMessageRow]` (id, created_at, level, tags, title, body, account, symbol)
- Tags NOT included in display data (only for routing/filtering)

Register `MessagesController` in `backend/api/app.py` route_handlers list.

### 1e. Initial callsites (additive, no internals touched)

**Deploy notification** (`backend/shared/helpers/notify_deploy.py`):
```python
fire(AppMessage(body=f"Deploy: {branch}→{env}", tags=["deploy","system"], level="info", title="Deploy"))
```

**Market close summary** (`_task_close` helper, after `send_summary(...)` call):
```python
fire(AppMessage(body=f"{label} close summary sent", tags=["market","system"], level="info"))
```

---

## Part 2 — Order Book Panel (Frontend)

### 2a. New component: `frontend/src/lib/OrderBook.svelte`

Extract the order card rendering from `LogPanel.svelte`'s `order` tab into a standalone
`OrderBook.svelte` component:

```
Props:
  orderId?: number          — when set, filters to that single order (modal usage)
  accountFilter?: string[]  — optional account scope
  title?: string            — defaults to "Order Book"

Behaviour:
  - Polls existing /api/logs/unified?kinds=placed,filled,rejected,cancelled,chase_modify endpoint
    at 3 s interval (same source as current LogPanel order tab)
  - Renders order cards (status chips, fill price, qty, symbol, time) exactly as they look now
  - No LogPanel dependency
```

### 2b. Orders page (`frontend/src/routes/(algo)/orders/+page.svelte`)

- Replace the `<LogPanel>` embedded in this page with `<OrderBook />`
- `<OrderBook>` sits below the live order table, labelled "Order Book" / "Order History"

### 2c. Order modal (wherever `LogPanel` appears with order filter)

- Replace `<LogPanel tab="order" orderId={...}>` with `<OrderBook orderId={order.id} />`
- Shows history for that specific order only

### 2d. LogPanel "order" tab — convert to AppMessage text feed

- Remove card rendering from the `order` tab in `LogPanel.svelte`
- Add parallel poll to `GET /api/messages?tags=order&limit=500&since=<lastSince>`
- Render rows as plain text log entries (same format as agent/system/conn tabs):
  `[timestamp] [level chip] body`
- Merge with existing `/api/logs/unified` rows (de-duplicate by `source + id` prefix)
- Tab will be sparse initially; fills as AppMessage callsites are wired in Phase 2

---

## Part 3 — Consolidated Post-Market Cron

### Current fragmentation (tasks to consolidate)

| Task | Current fire time | Action |
|------|------------------|--------|
| `_task_close` | polls every 5min, fires at 16:15/00:15 IST | → sub-job in orchestrator |
| `_task_nav_compute` | polls every 5min, fires at 16:00 IST | → sub-job |
| `_task_strategy_snapshot` | polls every 5min, fires at 15:45 IST | → sub-job (pre-close window) |
| `_task_daily_snapshot` | polls every 5min, fires at 16:15/00:15 IST | → sub-job |
| `_task_visitor_log_daily` | polls every 5min, fires at 23:35 IST | → sub-job |
| `_task_monthly_statement` | polls every 5min, fires at 02:00 IST | → sub-job |
| `_task_app_messages_cleanup` | new, 00:30 IST | → sub-job |

### New consolidated task: `_task_post_market_cron`

Single infinite loop, polls every 60 s. Three time windows:

```python
async def _task_post_market_cron(state: dict) -> None:
    """Single post-market orchestrator. Replaces individual post-market task loops."""
    while True:
        await asyncio.sleep(60)
        now  = timestamp_indian()
        date = now.date()
        h, m = now.hour, now.minute

        # ── Pre-close: 15:40–16:00 IST (NSE strategy snapshot) ──────────
        if (15, 40) <= (h, m) < (16, 0) and state.get('pre_close') != date:
            state['pre_close'] = date
            await _run_strategy_snapshot(date)

        # ── NSE post-close: 16:00–16:30 IST ─────────────────────────────
        if (16, 0) <= (h, m) < (16, 30) and state.get('nse_close') != date:
            state['nse_close'] = date
            await _run_nse_close(date)    # nav_compute + close_summary_nse + daily_snapshot_nse

        # ── MCX post-close: 23:30–23:59 IST ─────────────────────────────
        if (23, 30) <= (h, m) < (24, 0) and state.get('mcx_close') != date:
            state['mcx_close'] = date
            await _run_mcx_close(date)    # close_summary_mcx + daily_snapshot_mcx

        # ── Late night: 00:00–00:45 IST ──────────────────────────────────
        late_date = (now - timedelta(days=1)).date()  # yesterday's marker
        if (0, 0) <= (h, m) < (0, 45) and state.get('late_night') != date:
            state['late_night'] = date
            await _run_late_night(date)   # visitor_log + app_messages_cleanup

        # ── Monthly: 02:00–02:10 IST ─────────────────────────────────────
        if (2, 0) <= (h, m) < (2, 10) and state.get('monthly') != date:
            state['monthly'] = date
            await _run_monthly_statement(date)
```

**Each `_run_*` function** is the extracted body of the corresponding existing task (no logic change, just restructured). The old `_task_close`, `_task_nav_compute`, etc. loop wrappers are removed; their inner logic becomes helpers.

**Register** `_task_post_market_cron` in `_start_background_tasks()`. Remove the individual old task registrations that are absorbed.

---

## Commit message
feat(app-message): unified AppMessage system + Order Book panel + consolidated post-market cron

## Done when
- `app_messages` table created on `init_db()`; GIN index on tags
- `dispatch(AppMessage(...))` writes to DB; `fire()` works from sync callers
- `GET /api/messages?tags=order&limit=500` returns rows correctly
- `OrderBook.svelte` renders order cards; wired to Orders page + Order modal
- LogPanel order tab shows AppMessage text rows; LogPanel removed from Orders page/modal
- Single `_task_post_market_cron` runs all post-market jobs in time windows; old individual loops removed
- 2 initial callsites (deploy + close summary) write AppMessages
- svelte-check 0 errors | pytest green
- No changes to chase, template, agent, or broker internals
