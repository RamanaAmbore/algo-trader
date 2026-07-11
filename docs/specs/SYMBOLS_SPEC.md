# Symbol Resolution + Virtual Roots Specification

MCX and CDS futures never expose raw contract names in the UI. Virtual symbols map to
front-month (CRUDEOIL), back-month (_NEXT), or far-month contracts. All symbol resolution
flows through a single SSOT module to keep symbol display, autocomplete, and order routing
in sync across frontend and backend.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/symbol_resolver.py` · `backend/api/routes/symbols.py` ·
`backend/api/routes/instruments.py` · `frontend/src/lib/data/rootOf.js` · `frontend/src/lib/data/instruments.js`

---

## Contents

1. [Virtual Roots and Exchange Coverage](#1-virtual-roots-and-exchange-coverage)
2. [SSOT Functions](#2-ssot-functions)
3. [Rollover and Settling-Contract Rule](#3-rollover-and-settling-contract-rule)
4. [API Contract](#4-api-contract)
5. [Frontend Implementation](#5-frontend-implementation)
6. [Instruments Cache](#6-instruments-cache)
7. [MCX Lot-Size Overrides](#7-mcx-lot-size-overrides)
8. [Edge Cases and Recovery](#8-edge-cases-and-recovery)
9. [Test Coverage Map](#9-test-coverage-map)

---

## 1. Virtual Roots and Exchange Coverage

MCX and CDS commodities and currencies roll contracts monthly (or weekly for CDS minors).
Virtual symbols provide a stable operator-facing name that always points to the active
front-month contract.

**MCX virtual roots** (19 commodities):
CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI, GOLD, GOLDM, GOLDGUINEA, GOLDPETAL,
SILVER, SILVERM, SILVERMIC, COPPER, ZINC, LEAD, ALUMINIUM, NICKEL, MENTHAOIL, COTTON, CPO.

**CDS virtual roots** (4 majors + weekly variants):
USDINR, EURINR, GBPINR, JPYINR (monthly; plus weekly USDINR contracts excluded via regex).

Non-MCX/CDS exchanges (NSE, NFO, BSE) pass symbols through unchanged — resolution
is a no-op for equities and index options.

---

## 2. SSOT Functions

Three functions in `backend/api/algo/symbol_resolver.py` are the canonical sources
of truth for all symbol mapping:

**`list_active_futures(root, exchange, limit=2)`** — Returns the next `limit`
non-expired futures contracts sorted ascending by expiry. A contract on its expiry date
(settling today IST) is excluded so callers never read settlement prices as live spot.
Returns empty list if the instruments cache is cold or no contracts match.

**`resolve_symbol(virtual, exchange)`** — Maps a virtual root to the real tradingsymbol.
`CRUDEOIL` → front-month, `CRUDEOIL_NEXT` → back-month (or front if only one exists),
real contracts pass through unchanged. Falls back to the most-recently-listed contract
when all active futures are in cache-lag (all expired but cache not yet refreshed).

**`root_of(contract, exchange)`** — Reverse resolver. Given a real contract like
`CRUDEOIL26JUNFUT` returns the virtual display root (`CRUDEOIL`, `CRUDEOIL_NEXT`, or
the raw contract for far-month positions). Non-futures symbols pass through unchanged.

All three functions read the instruments cache (24-hour TTL, warmed at startup and
08:00 IST). They use a single `asyncio.Lock` per cache key to deduplicate concurrent
fetches.

---

## 3. Rollover and Settling-Contract Rule

On any given day, multiple MCX/CDS contracts may exist in the instruments cache:
active front-month (e.g. CRUDEOIL26JUNFUT), active back-month (CRUDEOIL26JULFUT),
and settling today (CRUDEOIL26MAYFUT if today is May 20).

**Settling-contract exclusion rule**: When `inst.x > today_iso` (ISO date string, IST),
the contract is considered active. Contracts with `inst.x <= today_iso` are excluded.

This ensures `list_active_futures` never returns a contract on its own expiry day,
preventing operators from accidentally trading settlement prices as if they were
live spot prices.

**`_MONTHLY_FUT_RE` regex filter**: Only contracts matching `^[A-Z]+\d{2}[A-Z]{3}FUT$`
are considered for resolution. CDS weekly contracts (USDINR26710FUT) are filtered out,
ensuring `resolve_symbol("USDINR")` always returns the monthly-cadence front-month
contract.

---

## 4. API Contract

| Endpoint | Auth | Method | Query Params |
|---|---|---|---|
| `/api/symbols/resolve` | Optional | GET | `symbol`, `exchange` |
| `/api/symbols/root_of` | Optional | GET | `contract`, `exchange` |

**`resolve_symbol` response**:
```json
{
  "virtual": "CRUDEOIL",
  "exchange": "MCX",
  "resolved": "CRUDEOIL26JUNFUT",
  "is_front": true,
  "is_back": false,
  "is_passthrough": false
}
```

**`root_of` response**:
```json
{
  "contract": "CRUDEOIL26JUNFUT",
  "exchange": "MCX",
  "root": "CRUDEOIL",
  "is_front": true,
  "is_back": false,
  "is_far": false
}
```

Both endpoints are auth-gated (demo allowed). The instruments cache is shared with
`GET /api/instruments` (daily TTL) so no extra broker calls are incurred.

---

## 5. Frontend Implementation

**`rootOf.js` module** provides four sync helpers after the instruments cache is seeded:

**`rootOf(contract, exchange)`** — Maps a real contract to its virtual root. Uses a
seeded two-slot map (front-month at index 0, back-month at index 1) to round-trip
without async fetches. Falls back to pass-through when the map is not yet seeded
(cold boot).

**`rootOfLabel(contract, exchange)`** — Human-readable label applying the display symbol
formatter. `CRUDEOIL_NEXT` becomes `CRUDEOIL.NEXT` (dot separator, render-only; the
machine key stays `_NEXT` for API bodies and store keys).

**`resolveVirtual(virtual, exchange)`** — Forward resolver. `CRUDEOIL` → front-month
contract, `CRUDEOIL_NEXT` → back-month slot (falls back to front if only one exists).
Sync, no async — uses seeded map.

**`getVirtualRoots(exchange)`** — Returns all active virtual roots (front + back-month)
for the given exchange, sorted by name. Used by symbol search to inject synthetic
instrument rows into autocomplete results.

**`seedRootMapFromInstruments(items)`** — Hydrates the internal maps from the flat
instruments array in one pass. Iterates, filters by `t='FUT'`, groups by underlying,
sorts by expiry, keeps first two per root. Called after the instruments cache loads.

---

## 6. Instruments Cache

**Endpoint**: `GET /api/instruments` — full Kite instrument dump across NSE, NFO, BSE, MCX, CDS.

**Format** (compact msgspec.Struct):
```json
{
  "s": "CRUDEOIL26JUNFUT",
  "e": "MCX",
  "t": "FUT",
  "u": "CRUDEOIL",
  "x": "2026-06-20",
  "ls": 100,
  "ts": 1.0
}
```

**TTL**: 24 hours, refreshed daily at 08:00 IST. Warmed at startup. Field abbreviations
(s/e/t/u/x/k/ls/ts) keep the payload small (~2–3 MB).

**Kite-only walk** (July 2026 defect fix): The fetch path walks all loaded Kite accounts
and breaks on the first non-empty response per exchange. Dhan/Groww instruments() return
a different schema (missing `instrument_type`, `name`, `expiry`, `strike` fields), which
would poison the cache with 156K rows having `t=''` / `u=None`. The fix ensures we never
fall over to Dhan even when a Kite account is rate-limited — partial Kite data is strictly
better than poisoned Dhan data.

---

## 7. MCX Lot-Size Overrides

Kite's `instruments("MCX")` response returns `lot_size=1` for all commodities. The actual
contract multiplier (CRUDEOIL=100 barrels, NATURALGAS=1250 mmBtu) is documented offline.

**Override map** in `backend/api/routes/instruments.py` (`_MCX_LOT_OVERRIDES`):
- Keyed by Kite `name` field (e.g. "CRUDEOIL", "CRUDE OIL", space variants)
- Applies to FUT and CE/PE rows alike (options use same multiplier as underlying)
- Applied during instruments fetch so the OrderTicket can render "Lots: 2 (× 100 = 200)"

When a new MCX commodity is added to the platform, the override must be manually inserted
after the trader desk verifies the contract size.

---

## 8. Edge Cases and Recovery

**Instruments cache lag**: All active futures on a root have expired but the 24-hour
cache hasn't refreshed yet. Resolution falls back to `_list_all_futures_fallback`
which includes expiring-today contracts (without the `inst.x > today_iso` gate).
Returns the most-recently-listed contract; operator sees the stale contract for a
few hours until the daily 08:00 IST refresh.

**Cold boot**: Instruments cache has never been warmed. Resolution returns the virtual
symbol unchanged (identity mapping). The cache warms at startup so this is rare;
subsequent calls succeed immediately.

**Unknown virtual root**: No contracts match the root in the instruments cache.
Resolution returns the virtual symbol unchanged. This happens when a commodity is
delisted or the instruments dump hasn't been updated yet.

**CDS weekly contracts**: Weekly USDINR variants (USDINR26710FUT) are filtered out
by `_MONTHLY_FUT_RE`. Only monthly-cadence contracts (USDINR26JULFUT) contribute to
the front/back-month map, so weekly positions render as far-month pass-through (raw
contract name).

---

## 9. Test Coverage Map

### Backend

- `test_symbol_resolver_list_active_futures.py` — settling-contract exclusion (today+1 vs today), limit truncation
- `test_symbol_resolver_resolve.py` — front-month selection, _NEXT suffix, identity pass-through
- `test_symbol_resolver_root_of.py` — reverse resolver round-trip, far-month detection
- `test_symbol_resolver_fallback.py` — cache-lag fallback to all-futures, cold boot identity
- `test_resolve_market_data_keys.py` — batch resolver with deduplication + re-keying

### Frontend

- `symbol_resolver.spec.js` — `rootOf()` round-trip, `rootOfLabel()` display format, `resolveVirtual()` forward
- `seed_root_map.spec.js` — `seedRootMapFromInstruments()` sorting + limit, monthly-only filtering (CDS weeklies excluded)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; Kite-only fetch + MCX lot override behavior documented |
