# Audit Log and History Specification

Single source of truth for forensic audit trail and operator-visible history surfaces.
Tracks every mutating action across the platform — from order placement to agent edits
to settings changes. Immutable, queryable, and retention-managed.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/models.py` · `backend/api/middleware/audit.py` · 
`backend/api/routes/audit.py` · `backend/api/routes/history.py` · 
`frontend/src/routes/(algo)/admin/history/+page.svelte`

---

## Contents

1. [Schema and Write Paths](#1-schema-and-write-paths)
2. [Audit Middleware](#2-audit-middleware)
3. [Category Routing](#3-category-routing)
4. [Read Endpoints](#4-read-endpoints)
5. [History Surfaces](#5-history-surfaces)
6. [Retention Policy](#6-retention-policy)
7. [Edge Cases](#7-edge-cases)
8. [Test Coverage Map](#8-test-coverage-map)

---

## 1. Schema and Write Paths

**AuditLog table**:

| Column | Type | Description |
|---|---|---|
| id | BIGINT PK | Auto-increment |
| actor_user_id | INT FK | User.id or null (system job) |
| actor_username | VARCHAR | Display name (anon / system / named user) |
| actor_role | VARCHAR | User.role at time of action (cached) |
| action | VARCHAR | Verb + path (e.g. "POST /api/orders/place") |
| category | VARCHAR | Routing bucket (order.place, order.fill, agent, etc.) |
| method | VARCHAR | HTTP method (GET, POST, PATCH, DELETE) |
| path | VARCHAR | Request path (e.g. `/api/orders/ticket`) |
| target_type | VARCHAR | Entity type (order_id, agent_id, broker_id, etc.) |
| target_id | VARCHAR | Entity identifier (123, ABC-456, etc.) |
| status_code | INT | HTTP response (200, 400, 401, etc.) |
| summary | TEXT | Human-readable one-liner ("Placed order for RELIANCE") |
| request_id | VARCHAR | Correlation token (trace across middleware + tasks) |
| client_ip | INET | Source IP |
| user_agent | TEXT | Browser / client signature |
| created_at | TIMESTAMP | UTC insertion |

**Two write paths**:

1. **AuditMiddleware** (HTTP request-response cycle)
   - Runs on EVERY request, skips non-mutating (GET, HEAD, OPTIONS)
   - Extracts actor, action, target, status from the request/response
   - Writes one row per HTTP request
   - Fails silently if write fails (audit loss is acceptable, request must succeed)

2. **write_audit_event()** (non-HTTP, background tasks + postbacks)
   - Called explicitly for async operations that span multiple HTTP requests
   - Postback webhook delivery (order_fill event from broker)
   - Agent action execution (order place / modify / cancel)
   - Background tasks (market lifecycle snapshots, data backfill)
   - Supplies actor_user_id, action, target, summary directly

---

## 2. Audit Middleware

**Trigger condition**: `method not in ("GET", "HEAD", "OPTIONS")` — any mutation.

**Actor extraction**:
```python
actor_user_id = request.state.token_payload.get("user_id") or None
actor_username = request.state.token_payload.get("sub") or "anon"
actor_role = User.role (fresh query or cached state)
```

**Action format**: `"{method} {path}"` (e.g. `"POST /api/orders/ticket"`).

**Category assignment** (by path prefix):
- `order.place` → `/api/orders/ticket`, `/api/orders/basket`
- `order.fill` → postback webhook, broker webhook
- `order.modify` → `/api/orders/{id}/modify`, `/api/orders/{id}/update`
- `order.cancel` → `/api/orders/{id}/cancel`
- `user.*` → `/api/admin/users/*`, auth routes
- `config.*` → `/api/admin/settings`, `/api/admin/brokers`, etc.
- `system.*` → `/api/admin/persistence`, `/api/admin/market-*`, background tasks
- `agent` → `/api/automation`, agent activation/deactivation
- `strategy` → `/api/templates`, GTT/bracket strategy changes
- `http` → catch-all (unclassified requests)

**Target extraction**:
- URL path params: `/api/orders/{order_id}/cancel` → target_type=order_id, target_id=order_id value
- JSON body: `{"agent_id": 42}` → target_type=agent_id, target_id=42
- Path segments: `/api/admin/brokers/{broker_id}` → target_type=broker_id

**Status code**: Direct from response object. Includes error responses (400, 401, 500).

**Summary** (human-readable hint):
```
"Placed 1-lot NSE:RELIANCE call @ ₹2600"
"Canceled order ZG0790.2024.123456 (partial)"
"Disabled loss-recovery agent (threshold changed)"
"Settings: changed alert_cooldown_minutes to 45"
```

**Request ID**: Injected by middleware into response headers (X-Request-ID). Enables
cross-referencing AuditLog + application logs when debugging.

---

## 3. Category Routing

| Category | Examples | Trigger |
|---|---|---|
| order.place | POST /api/orders/ticket, /api/orders/basket | Ticket submission |
| order.modify | PATCH /api/orders/{id}, POST /api/orders/{id}/update | Order price/qty change |
| order.cancel | DELETE /api/orders/{id}, POST /api/orders/{id}/cancel | Manual cancellation |
| order.fill | Postback webhook, broker fill notification | Broker-side fill event |
| user | POST /api/admin/users, PATCH /api/admin/users/{id}, auth/login | User CRUD + auth |
| config.broker | POST /api/admin/brokers, PATCH /api/admin/brokers/{id} | Broker account setup |
| config.settings | PATCH /api/admin/settings/{key}, POST /api/admin/settings/reset | Tunable changes |
| system.persistence | POST /api/admin/persistence/mode, POST /api/admin/persistence/backfill | Cache mode flip |
| system.market | Background tasks (sparkline warm, market lifecycle events) | Automatic snapshots |
| agent | POST /api/automation/{id}/activate, PATCH /api/automation/{id} | Agent rule changes |
| strategy | POST /api/templates, DELETE /api/templates/{id} | GTT/bracket CRUD |
| http | Uncategorized requests | Fallback bucket |

---

## 4. Read Endpoints

**`GET /api/admin/audit`** (gated by `view_audit` cap — designated / risk / admin only)

| Query Param | Type | Behavior |
|---|---|---|
| actor | string | Case-insensitive substring match on actor_username |
| action | string | Substring match on action label (e.g. "POST /api") |
| category | string | Exact match on category; supports comma-separated OR (e.g. "order.place,order.fill") |
| target_type | string | Exact match (e.g. "order_id", "broker_id") |
| target_id | string | Exact match (e.g. "ZG0790.2024.123456") |
| request_id | string | Exact match on correlation token |
| since_hours | int | Rows newer than N hours (default: all-time) |
| status_code | int | Exact match (e.g. 200, 400, 401) |
| limit | int | Rows per page (default 50, capped 500) |
| offset | int | Pagination (default 0, unbounded) |

**Response shape**:
```json
{
  "rows": [
    {
      "id": 12345,
      "actor_user_id": 1,
      "actor_username": "operator@ramboq.com",
      "actor_role": "admin",
      "action": "POST /api/orders/ticket",
      "category": "order.place",
      "method": "POST",
      "path": "/api/orders/ticket",
      "target_type": "order_id",
      "target_id": "ZG0790.2024.123456",
      "status_code": 201,
      "summary": "Placed 1-lot NSE:RELIANCE call @ ₹2600",
      "request_id": "req-abc-def-123",
      "client_ip": "203.0.113.42",
      "user_agent": "Mozilla/5.0 (Macintosh; ...)",
      "created_at": "2026-07-11T10:15:30Z"
    }
  ],
  "total": 1250,
  "limit": 50,
  "offset": 0
}
```

---

## 5. History Surfaces

**`/admin/history`** — Three tabs, each a queryable grid with filters + pagination.

### Orders tab
- **Rows**: Last 30 days of order.place + order.modify + order.cancel events
- **Columns**: Timestamp · Action · Symbol · Qty · Price · Order ID · Status · Mode
- **Filters**: Actor (pill) · Status (histogram) · Mode (SIM / PAPER / LIVE / SHADOW)
- **Pagination**: 50 rows/page
- **Source**: AuditLog (category IN order.place, order.modify, order.cancel)

### Trades tab
- **Rows**: Broker-reported fills (last 30 days) — one row per filled leg
- **Columns**: Date · Symbol · Qty · Avg Price · Exchange · Account · P&L (if closed)
- **Source**: daily_book table (kind='trade', last 30 days)
- **Pagination**: 50 rows/page

### Funds tab
- **Rows**: Daily fund snapshots (cash, margin, collateral) — last 90 days
- **Columns**: Date · Account · Cash · Available Margin · Used Margin · Collateral
- **Source**: daily_book table (kind='funds', last 90 days, grouped by date/account)
- **Pagination**: 50 rows/page

**CSV export**: All three tabs support Download button (ag-Grid export with
current filters + sort applied).

---

## 6. Retention Policy

**Retention by category**:

| Category | Retention | Operator tunable |
|---|---|---|
| order.* + agent + strategy | 30 days (Slice Q) | audit_log_retention_days (default 365) |
| user + config.* + system.* | 365 days | audit_log_retention_days |
| All others | 365 days | audit_log_retention_days |

**Cleanup**: Daily background task (02:00 IST) deletes rows older than the
configured window. Safe to flip the setting mid-retention (existing rows pruned
prospectively). Never removes current-day rows (calendar-day boundary), so same-day
edits always visible in `/admin/history`.

**Immutability**: AuditLog rows never updated after creation. No corrections,
no redactions. operator cannot delete individual rows; only bulk retention pruning.

---

## 7. Edge Cases

### Background task with no request_id
- `write_audit_event()` called from background task (no active HTTP request)
- request_id supplied by caller (e.g. "bg-task-market-lifecycle")
- actor_user_id = None (system action)
- actor_username = "system"

### Postback webhook with broker order_id
- Broker delivers fill notification to webhook endpoint
- Middleware logs the webhook request (POST /api/orders/postback)
- write_audit_event() called separately with target_type=order_id, target_id=broker_order_id
- Same request_id correlation tag used in both rows

### Failed requests still logged
- Status code 400 / 401 / 500 written to AuditLog
- Operator can query by status_code to see failed transactions
- `summary` field captures the error message (e.g. "NSE:SYMBOL not found")

### Write-audit failure (DB unavailable)
- Middleware catches exceptions, logs to stderr, continues
- HTTP request succeeds even if audit write fails
- Operator loses the forensic record for that one request, but system remains live

### Auth outage (no token_payload)
- actor_user_id = None
- actor_username = "anon" (no JWT present)
- actor_role = "unknown"
- request still logged for forensic trail (failed login attempts, etc.)

---

## 8. Test Coverage Map

### Backend — covered

- `test_audit_middleware.py` — middleware skip logic (GET, HEAD, OPTIONS)
- `test_audit_category_routing.py` — path → category assignment
- `test_audit_write_event.py` — non-HTTP write path (postback, agent action)
- `test_audit_query.py` — filter combinations, pagination, OR on category
- `test_audit_retention.py` — daily cleanup, retention window, no current-day delete

### Backend — gaps

- Actor role caching (refresh frequency, stale role reflection)
- Request ID injection + propagation (trace correlation end-to-end)
- Target extraction edge cases (URL vs body vs path, precedence)
- Middleware performance impact (audit write latency on critical paths)

### Frontend — covered

- `history_orders.spec.js` — Orders tab renders AuditLog rows, filters work
- `history_trades.spec.js` — Trades tab pulls daily_book kind='trade'
- `history_funds.spec.js` — Funds tab aggregates by date/account

### Frontend — gaps

- CSV export button (handler in CardControls component)
- Status histogram + mode pill filters (backend aggregation query)
- Deep-link to history with pre-filled filters (query param → filter state)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
