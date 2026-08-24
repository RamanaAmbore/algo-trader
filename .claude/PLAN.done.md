# Plan: qty/lots/lot_size normalization + UPSERT stale fix + DB query tool

## Task

Four related fixes to position data consistency across the daily_book, broker layer, and frontend:

1. **qty uniform in CONTRACTS + add lots/lot_size fields** — `qty` in `daily_book` must be CONTRACTS (lots * lot_size) uniformly. Currently MCX stores LOTS (e.g., qty=1 for 1 GOLDM lot); NFO stores CONTRACTS (e.g., qty=75 for 1 NIFTY lot). Fix: MCX rows multiply quantity by multiplier to get contracts. Two new columns added: `lots` (integer — number of lots) and `lot_size` (integer — contracts per lot). Invariant after fix: `qty = lots x lot_size` for all rows. P&L math already uses contracts; the fix makes MCX match. Frontend displays lots with lot_size annotation; P&L math uses qty (contracts).

2. **DB query tool** — `scripts/dbq.sh dev|prod "SQL"` SSHes to server and runs psql. Credentials read from server's `secrets.yaml` at runtime — never stored locally.

3. **UPSERT day_pnl zero-guard stale bug** — `COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)` keeps stale 780 when formula legitimately returns 0 (e.g., ltp=prev_close on flat weekend). Fix: when EXCLUDED.ltp IS NOT NULL (valid settlement captured), always use EXCLUDED.day_pnl even if 0.

4. **prev_close timing** — prev_close for a symbol should come from the last session where that segment was actually open. For MCX (which trades Saturdays), Saturday is a valid session. For NSE (closed Saturdays), no Saturday snapshot exists. The real symptom is issue 3. Document as-is; no code change needed here beyond the UPSERT fix.

## Agents

- backend: In `backend/brokers/broker_apis.py`, rename `_apply_mcx_multiplier` to `_annotate_lot_size(df)`. New logic: (a) MCX/NCO rows: set `lots = quantity` (already in lots from Kite), `lot_size = multiplier`, then overwrite `quantity = quantity * multiplier` (convert to contracts); scale `overnight_quantity`, `day_buy_quantity`, `day_sell_quantity` by multiplier too. (b) NFO/CDS/BFO rows: read `lot_size` from `_LOT_INDEX` (sync dict in `backend/brokers/adapters/kite.py`, import directly; fall back to 1 if cold), set `lots = quantity // lot_size` (already in contracts), `lot_size = lot_size_from_LOT_INDEX`, `quantity` unchanged (already contracts). (c) Equity: `lots = quantity`, `lot_size = 1`, `quantity` unchanged. Update call site in `_fetch_positions_local` to call `_annotate_lot_size`. The API response for positions now always includes `lots` and `lot_size` fields; `quantity` is always in contracts. In `backend/api/algo/daily_snapshot.py`, update `_positions_rows`: MCX rows -- `qty = int(r['quantity'] * r['multiplier'])` (contracts), `lots = int(r['quantity'])`, `lot_size = int(r['multiplier'])`; NFO rows -- `qty = int(r['quantity'])` (contracts, unchanged), `lots = qty // lot_size_from_LOT_INDEX`, `lot_size = lot_size_from_LOT_INDEX` (import `_LOT_INDEX` directly; fall back to 1). Fix two lines in `_UPSERT_SQL` (around line 706): (1) day_pnl guard: change `day_pnl = COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)` to `day_pnl = CASE WHEN EXCLUDED.ltp IS NOT NULL THEN COALESCE(EXCLUDED.day_pnl, daily_book.day_pnl) ELSE daily_book.day_pnl END`; (2) previous_close freeze gate: change `previous_close = COALESCE(daily_book.previous_close, EXCLUDED.previous_close)` to `previous_close = CASE WHEN EXCLUDED.ltp IS NOT NULL AND (daily_book.ltp IS NULL OR EXCLUDED.ltp != daily_book.ltp) THEN daily_book.ltp ELSE daily_book.previous_close END` -- this advances previous_close only when ltp actually changes (new session settlement), preserving it during frozen weekend snapshots. Also add `lots` and `lot_size` to the INSERT column list and UPDATE clause of `_UPSERT_SQL`. In `backend/api/models.py`, add to `DailyBook`: `lots: Mapped[int] = mapped_column(Integer, nullable=False, default=1)` and `lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)`. Create `backend/migrations/add_daily_book_lots_lot_size.sql`: `ALTER TABLE daily_book ADD COLUMN IF NOT EXISTS lots INTEGER NOT NULL DEFAULT 1; ALTER TABLE daily_book ADD COLUMN IF NOT EXISTS lot_size INTEGER NOT NULL DEFAULT 1;`. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory -- not optional.
- frontend: In every component that renders position `quantity` (search for `quantity` in `frontend/src/routes/(algo)/` and `frontend/src/lib/`): display as lots using the new `lots` field from the API, with lot_size shown alongside (e.g. "2 lots x10"). For P&L value computations (inv_val, cur_val, day P&L), use `quantity` (contracts) x price -- same formula as today since qty is now uniformly in contracts. No formula change needed for NFO (already contracts); MCX formula is now correct since qty is contracts. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory -- not optional.
- broker: skip
- doc: skip
- backend-test: Write pytest tests in `backend/tests/test_lot_size_normalization.py`: (a) `_annotate_lot_size` MCX row -- qty=1 lot, multiplier=10 -> output quantity=10 (contracts), lots=1, lot_size=10; (b) `_annotate_lot_size` NFO row -- quantity=75 (contracts), lot_size=75 from _LOT_INDEX -> output quantity=75, lots=1, lot_size=75; (c) `_annotate_lot_size` equity row -- quantity=100, lot_size=1, lots=100; (d) `_positions_rows` MCX entry in daily_book -> qty=contracts, lots=original, lot_size=multiplier; (e) UPSERT zero-guard: second snapshot with day_pnl=0 and non-null ltp DOES overwrite prior non-zero value; (f) UPSERT mid-session guard: snapshot with ltp=NULL preserves prior day_pnl. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory -- not optional.
- playwright: skip

## DB tool
- Create `scripts/dbq.sh` -- executable shell script. Usage: `./scripts/dbq.sh <dev|prod> "SQL"`. SSHes to ramboq server, reads DB credentials from `/opt/ramboq/backend/config/secrets.yaml` on the server (never stored locally), runs psql with `-h 127.0.0.1`. Dev: database `ramboq_dev`; prod: database `ramboq`. Also run the migration `add_daily_book_lots_lot_size.sql` on both dev and prod via this script as part of deployment.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(positions): normalize MCX/NFO qty to contracts + add lots/lot_size fields, fix UPSERT day_pnl zero-guard stale preservation, add scripts/dbq.sh DB query tool

## Done when
- `quantity` in positions API response and daily_book is always CONTRACTS (lots x lot_size) for MCX and NFO
- `lots` and `lot_size` fields present on every position row in the API response and in daily_book
- MCX: `quantity = lots x lot_size` (e.g., GOLDM 1 lot -> qty=10, lots=1, lot_size=10)
- NFO: `quantity = lots x lot_size` (e.g., NIFTY 1 lot -> qty=75, lots=1, lot_size=75)
- Equity: `lot_size=1`, `lots=quantity`
- `daily_book.lots` and `daily_book.lot_size` columns exist (migration applied on dev and prod)
- UPSERT no longer preserves stale day_pnl when a subsequent snapshot computes 0 with valid non-null ltp
- UPSERT no longer advances previous_close from a frozen weekend snapshot (ltp unchanged)
- Frontend displays qty as lots with lot_size annotation; P&L math uses contracts
- `scripts/dbq.sh dev "SELECT 1"` works from local machine
- All pytest tests pass; svelte-check 0 errors

## Context

**qty inconsistency root cause:**
- MCX: Kite ships `quantity` in LOTS, `multiplier` = lot_size (e.g., GOLDM: qty=1 lot, multiplier=10)
- NFO/CDS/BFO: Kite ships `quantity` in CONTRACTS (lot_size already applied: e.g., NIFTY 1 lot -> qty=75)
- `_apply_mcx_multiplier` converts MCX lots->contracts at runtime; snapshot path skips it -> daily_book has LOTS for MCX, CONTRACTS for NFO -- inconsistent
- Fix: `_annotate_lot_size` converts MCX to contracts + populates `lots` and `lot_size` for all segments
- P&L math (`decomposed_intraday_pnl` in `backend/api/algo/pnl_math.py`) already multiplies by `lot_mult`; after fix, qty is contracts for MCX too so the existing multiplier in that function now double-counts -- review and fix the call site in `_enrich_positions` so lot_mult=1 is passed for MCX (since quantity is already contracts)

**NFO lot_size sync lookup:**
- `_LOT_INDEX` in `backend/brokers/adapters/kite.py` is a plain dict (sync-readable), keyed by (exchange, tradingsymbol) -> lot_size
- Import directly -- no await needed
- Falls back to `lot_size=1` if cache cold (instruments load in ~30s on startup)

**UPSERT stale day_pnl:**
- Current: `COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)` -- "if new value is 0, keep old"
- Bug: when ltp=prev_close (flat weekend settlement), day_pnl formula correctly returns 0 but stale 780 preserved
- Fix: `CASE WHEN EXCLUDED.ltp IS NOT NULL THEN COALESCE(EXCLUDED.day_pnl, daily_book.day_pnl) ELSE daily_book.day_pnl END`
- Evidence: Aug 24 (Sunday) day_pnl=780 preserved; should be 0 because ltp=836=prev_close

**prev_close timing (requires snapshot gate):**
- MCX metals/energy (GOLDM, CRUDEOIL, SILVER, etc.) do NOT trade on Saturdays. Only MCX agri commodities have Saturday morning sessions.
- The Saturday snapshot for GOLDM captures frozen ltp=836 (Kite returns Friday's EOD settlement since contract hasn't ticked).
- This Saturday snapshot wrongly becomes the "previous record" in prev_ltp_map for Sunday. Value is the same (836=Friday settlement), so prev_close is numerically correct, but conceptually wrong: we're using a non-trading-day record as the settlement reference.
- Fix: in `snapshot_daily_book` in `backend/api/algo/daily_snapshot.py`, gate snapshot execution with `is_market_open()` (or equivalent). If NO segment is currently open (pure weekend for that exchange group), skip capturing snapshots for that segment. NSE positions already don't snapshot on weekends (market closed). MCX metals similarly should be gated.
- Implementation: before calling `snapshot_daily_book` in the background task (`_task_daily_snapshot`), check `is_market_open()`. If market is closed, skip. This is already partially handled by the task schedule but needs an explicit guard so off-hours restarts don't capture stale weekend snapshots.
- Alternate simpler approach: in `_UPSERT_SQL`, gate `previous_close` update so it only updates when the incoming record's ltp differs meaningfully from the prior ltp (i.e., the market actually moved). `previous_close = CASE WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp != daily_book.ltp THEN daily_book.ltp ELSE daily_book.previous_close END` -- this advances previous_close only when ltp changes (new session settlement), not on frozen weekend snapshots.
- **Chosen fix**: use the simpler alternate approach for previous_close update gate (no need to restructure the task schedule). The UPSERT fix (issue 3) remains required and separate.

**DDL + one-time data strategy:**
- Migration adds two columns with DEFAULT 1 -- no complex backfill
- Historical rows: lot_size=1, lots=qty (MCX rows will show wrong lots for history, but history is not displayed as lots in UI)
- Going forward: next snapshot after deploy populates lots and lot_size correctly for all open positions
- Existing P&L values in daily_book remain valid (computed with lot_mult already applied correctly)
- NOTE: after deploy, first snapshot will also correct the qty field for MCX rows (multiply by lot_size). This means historical MCX qty rows in daily_book will still show LOTS for dates before deploy; this is acceptable.

**DB tool:**
- `scripts/dbq.sh` reads DB credentials from server's secrets.yaml via SSH at runtime (never stored locally)
- Supports `dev` (ramboq_dev) and `prod` (ramboq) targets
- SSH host alias `ramboq` (or `dev.ramboq.com`) assumed to be configured in user's ~/.ssh/config
