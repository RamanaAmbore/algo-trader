# NAV and Investor Portal Specification

Single source of truth for NAV computation, investor slice calculations, and
token-gated investor portal behavior. The NAV system tracks firm-level asset value
and distributes it proportionally to limited partners via a units-based model.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/nav.py` · `backend/api/routes/nav.py` · 
`backend/api/routes/investor.py` · `frontend/src/lib/data/nav.js` · 
`frontend/src/lib/NavCard.svelte` · `frontend/src/lib/NavBreakdown.svelte`

---

## Contents

1. [NAV v4 Formula](#1-nav-v4-formula)
2. [Units Model](#2-units-model)
3. [Endpoints](#3-endpoints)
4. [Investor Portal](#4-investor-portal)
5. [Snapshots and Persistence](#5-snapshots-and-persistence)
6. [Edge Cases](#6-edge-cases)
7. [Test Coverage Map](#7-test-coverage-map)

---

## 1. NAV v4 Formula

NAV (Net Asset Value) is computed daily and represents the firm-wide mark-to-market
wealth available for distribution. The v4 formula resolves double-counting that
plagued v3 by separating cash and position contributions clearly.

```
firm_nav = cash_sod + option_premium + Σ position.unrealised + Σ holding.cur_val
```

| Component | Definition | Source |
|---|---|---|
| `cash_sod` | Start-of-day cash (avail opening_balance) | `funds.avail_opening_balance` |
| `option_premium` | Long-option premiums paid (tied-up capital) | `funds.util option_premium` |
| `Σ position.unrealised` | Open P&L across all positions | Broker LTP × qty − cost |
| `Σ holding.cur_val` | Current value of equity holdings | Broker qty × LTP |

**Why v4 replaces v3**: v3 added `used_margin` back to cash to compensate for broker
subtraction — unintentionally double-counting SPAN margin already embedded in
position.unrealised. v4 removes this and uses only `option_premium` (cost basis of
long options), leaving futures margin to flow purely through position M2M.

**LTP resolution** (fallback chain): KiteTicker subscribed-symbol ticks (zero
broker quota) → row.last_price → 0 (under-estimate safer than refusing compute).

**Backend SSOT**: `backend/api/algo/nav.py:compute_firm_nav()`. Vectorized via
Polars for ~50–100× speedup over iterrows on typical 5-account frames.

**Frontend equivalent**: `frontend/src/lib/data/nav.js:navByAccount()` shares the
same v4 formula; both files must be updated together on any revision.

---

## 2. Units Model

Investor slices are distributed via a proportional units ledger. Each limited
partner holds units; NAV per unit is computed daily and their slice = units × NAV/unit.

**Slice calculation**:
```
units_held(user)       = Σ units_delta from subscription/redemption events
total_units(date)      = Σ units_held across every LP
nav_per_unit(date)     = firm_nav(date) / total_units(date)
nav_share              = units_held × nav_per_unit
cost_basis             = Σ subscription.amount - Σ redemption.amount
pnl                    = nav_share - cost_basis
pnl_pct                = pnl / cost_basis (when basis > 0)
```

**Events** (subscription/redemption journal):
- Subscription: `units_delta = +amount / nav_per_unit` (positive)
- Redemption: `units_delta = -amount / nav_per_unit` (negative)
- Bootstrap (v1 migration): synthetic event from `User.share_pct + User.contribution`

**Auto-bootstrap**: On first compute, `ensure_all_bootstrapped()` backfills missing
LPs into the events table, encoding their v1 share_pct + contribution. When
share_pcts sum to 100, bootstrap reproduces v1 numbers exactly. Otherwise units
redistribute proportionally and slices sum to firm_nav by construction.

**Day delta** (investor slice move): Recompute slice for prior-day NAV using the
same event set, subtract from today. Capital movements (subscriptions/redemptions)
show as capital changes (not P&L) in the difference.

---

## 3. Endpoints

### Authenticated (JWT required)

| Endpoint | Method | Caps | Returns |
|---|---|---|---|
| `/api/nav/` | GET | view_nav | NavListResponse (default 90 days, max 1825) |
| `/api/nav/latest` | GET | view_nav | NavLatestResponse (2 rows + delta) |
| `/api/nav/me` | GET | jwt_guard | InvestorSlice (live firm_nav, day delta) |
| `/api/nav/me/history` | GET | jwt_guard | InvestorHistoryResponse (curve + cost basis) |
| `/api/nav/compute` | POST | trigger_nav_compute | NavComputeResponse + DB upsert |

**Live intraday NAV**: `/api/nav/me` calls `compute_firm_nav()` in the request path
(not relying on the daily 16:00 IST EOD cron). This ensures the investor's slice
tracks the same NAV that `/dashboard` and NavCard display intraday. Falls back to
the EOD snapshot on broker outage.

### Public (token in URL — no auth)

| Endpoint | Method | Auth | Returns |
|---|---|---|---|
| `/api/investor/{token}/slice` | GET | InvestorToken.token | InvestorSliceResponse |
| `/api/investor/{token}/history` | GET | InvestorToken.token | InvestorHistoryResponse (paginated) |
| `/api/investor/{token}/statement/{y}/{m}` | GET | InvestorToken.token | Binary PDF (with attachment headers) |

**Token validation**: `_resolve_token()` checks active (not revoked AND not expired),
tracks visit_count + last_visit_at (best-effort, non-blocking), resolves to User.

---

## 4. Investor Portal

The investor portal is a **token-as-credential** surface. The operator mints a
long-lived token via `/api/admin/users/{id}/investor-tokens` (POST), copies the URL,
and forwards it to the LP. No login. No password. Token revocation is the
operator's escape hatch if a URL leaks.

| Operation | Endpoint | Cap | Shape |
|---|---|---|---|
| Mint token | POST `/api/admin/users/{id}/investor-tokens` | manage_investor_tokens | MintTokenResponse (full token shown once) |
| List tokens | GET `/api/admin/users/{id}/investor-tokens` | manage_investor_tokens | TokenListResponse (preview + metadata) |
| Revoke token | DELETE `/api/admin/users/{id}/investor-tokens/{tid}` | manage_investor_tokens | 204 (idempotent) |
| List events | GET `/api/admin/users/{id}/investor-events` | manage_investor_tokens | EventListResponse (sub/redemption journal) |
| Create event | POST `/api/admin/users/{id}/investor-events` | manage_investor_tokens | CreateEventResponse |
| Delete event | DELETE `/api/admin/users/{id}/investor-events/{eid}` | manage_investor_tokens | 204 (fat-finger escape) |

**Token shape**: 32 bytes hex = 64 chars, ~128-bit entropy, URL-safe. First 8 chars
shown in admin list view; full token returned ONCE on mint. Operator must copy to
clipboard immediately.

**Token metadata**: `expires_at`, `revoked_at`, `last_visit_at`, `visit_count`,
`note` (operator-supplied context, e.g. "Q3 2026 Sarah LP"). Expiry defaults to
90 days, capped at 10 years.

---

## 5. Snapshots and Persistence

**Daily snapshot**: `write_nav_snapshot()` runs on demand (operator via `/api/nav/compute`)
and daily at 16:00 IST (background task). Idempotent UPSERT on `nav_daily.as_of_date`:
same date re-writes the existing row (e.g. operator triggers mid-day after outage clears).

**Snapshot payload** (`nav_daily` table):
```json
{
  "as_of_date": "2026-07-11",
  "nav": 1250000.50,
  "cash_total": 50000.25,
  "positions_mtm": 800000.00,
  "holdings_mtm": 400000.25,
  "accounts_snapshot": ["ZG0790", "ZG0791"],
  "note": "errors: positions: broker timeout"
}
```

**Retention**: `nav_daily` forever (immutable historical record). No pruning.

**Error handling**: Each broker-account call (funds, positions, holdings) wrapped
in try/except. Single offline broker does not block the snapshot; errors listed
in `note` field. `accounts_snapshot` lists which brokers contributed.

**NavBreakdown component** (frontend sidebar): SSOT for dashboard tabs (NAV / Capital /
Equity). Consumes backend `compute_firm_nav` and render the breakdown simultaneously
with the header pill.

---

## 6. Edge Cases

### No positions or holdings
- All position/holding terms = 0; NAV = cash_sod + option_premium only

### Option premium calculation with zero longs
- If no long CE/PE positions, `option_premium` = 0 (Kite omits the field)
- NAV degrades gracefully; still reflects cash + position M2M

### Stale snapshot during broker outage
- `compute_firm_nav()` catches broker exceptions; investor slice falls back to
  the EOD snapshot from `nav_daily` (stale beats zero)

### Token expiry / revocation
- `_resolve_token()` raises 401 immediately; LP cannot access `/investor/{token}/*`
- Operator revokes via `/api/admin/users/{id}/investor-tokens/{tid}` (DELETE)
- Re-mint a new token if rotation needed

### Subscriber/redemption same day
- Event uses `nav_per_unit` supplied by operator — not recomputed. Legitimate
  same-day same-amount events possible; operator can delete duplicates via
  `/api/admin/users/{id}/investor-events/{eid}` (DELETE)

### Multiple accounts with zero cash
- Funds aggregation sums across all accounts; if all offline, cash_total = 0
- Positions/holdings aggregation continues independently

---

## 7. Test Coverage Map

### Backend — covered

- `test_nav_formula.py` — v4 formula matches manual calculation
- `test_nav_snapshot.py` — upsert idempotency, error swallowing per-tier
- `test_investor_units.py` — subscription/redemption event math, bootstrap backfill
- `test_investor_slice.py` — slice value + cost basis + pnl_pct
- `test_investor_token.py` — mint, active check, revoke, visit tracking

### Backend — gaps

- Polars vectorization performance vs pandas iterrows (benchmark missing)
- Margin aggregation with mixed account types (funded vs intraday only)
- LTP fallback chain (ticker miss → last_price → zero) end-to-end

### Frontend — covered

- NavCard renders investor slice + day delta (when authenticated)
- NavBreakdown tabs show cash/positions/holdings breakdown
- NavStrip P pill slot 1 matches PositionStrip day P&L SSOT

### Frontend — gaps

- Investor portal `/investor/<token>` loads slice + history (e2e missing)
- Statement PDF render: fpdf2 layout on public token portal (render test missing)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
