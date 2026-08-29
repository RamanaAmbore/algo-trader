# Plan: Exchange schedule CRUD guards + MCX snapshot/P&L fixes

## Context
Two fixes identified during the NON-MCX gate rename investigation:

1. **Exchange schedule CRUD protection**: No guards exist on delete/update for default rows or past-date overrides. Default gate rows (date IS NULL) are the permanent schedule and must not be deletable. Past-date overrides are historical records and must be immutable.

2. **MCX P&L regression**: MCX snapshot_time was changed 23:45→00:15 in the prior commit, creating a 45-minute window (23:30–00:15) where `latest_snapshot_ltp_map` returns yesterday's settlement LTP → day P&L shows 0 for all MCX positions during that window. Also, `daily_snapshot.py::_is_exchange_open_at` uses hardcoded times inconsistent with exchange_schedule after the 08:00 open_time change.

## Task
Two related fixes:

**1. Exchange schedule CRUD protection**  
Default rows (date IS NULL): update allowed, delete blocked (409).  
Past-date overrides (date < today): update blocked (409), delete blocked (409).  
Future/today overrides: full CRUD.  
Add `deletable` and `editable` bool fields to DTO; frontend conditionally renders controls.

**2. MCX snapshot timing + `_is_exchange_open_at` fix**  
Revert MCX `snapshot_time` from 00:15 → 23:45 (restores 15-minute post-close window).  
Fix `_is_exchange_open_at` in `daily_snapshot.py` to delegate to `exchange_clock.is_exchange_open(exchange)` instead of hardcoded times.

## Agents
- backend: (1) `backend/api/routes/exchange_schedule.py`: add `deletable: bool` and `editable: bool` to `ExchangeScheduleDTO`; compute in `_to_dto(row)` as: `deletable = row.date is not None and row.date >= date.today()`, `editable = row.date is None or row.date >= date.today()`. In `delete_schedule`: raise HTTPException(409) if `row.date is None` (detail "default gate rows cannot be deleted") or `row.date < date.today()` (detail "past-date overrides cannot be deleted"). In `update_schedule`: raise HTTPException(409) if `row.date is not None and row.date < date.today()` (detail "past-date overrides cannot be updated"). (2) `backend/api/algo/daily_snapshot.py`: remove hardcoded `_NSE_OPEN_T/_NSE_CLOSE_T/_MCX_OPEN_T/_MCX_CLOSE_T` constants; replace `_is_exchange_open_at` body with `return exchange_clock.is_exchange_open(exchange)` (ignore `now_ist` param). Add `from backend.api.helpers import exchange_clock` import. (3) `backend/api/helpers/exchange_clock.py`: revert MCX `snapshot_time` in `_SEED_ROWS` from `time(0, 15)` → `time(23, 45)`. In `seed_and_warm()` MCX migration UPDATE: change `snapshot_time = '00:15'` → `snapshot_time = '23:45'`. Update module docstring MCX snapshot column (00:15* → 23:45).
- frontend: Find the exchange schedule admin UI component at `frontend/src/routes/(algo)/admin/` or similar. Hide the delete button when `!row.deletable`; disable/grey the edit button (or show lock icon) when `!row.editable`.
- broker: skip
- doc: skip
- backend-test: Add pytest cases in `backend/tests/test_exchange_schedule.py`: (a) DELETE default row → 409; (b) DELETE past-date row → 409; (c) DELETE today/future-date row → 204; (d) UPDATE default row → 200; (e) UPDATE past-date row → 409; (f) UPDATE today/future-date row → 200. Also update `backend/tests/test_exchange_clock.py`: MCX seed snapshot_time assertion → time(23, 45).
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(exchange-schedule): CRUD guards for default/past rows + revert MCX snapshot to 23:45 + delegate _is_exchange_open_at to exchange_clock

## Done when
- DELETE on default gate row → 409
- DELETE on past-date override → 409
- UPDATE on past-date override → 409
- UPDATE on default gate row → 200
- DTO has `deletable` and `editable` booleans
- Frontend hides/disables controls accordingly
- MCX seed snapshot_time = time(23, 45)
- `_is_exchange_open_at` delegates to `exchange_clock.is_exchange_open`
- All pytest green, svelte-check 0 errors
