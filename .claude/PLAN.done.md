# Plan: Post-outage hardening — broker resilience + event-loop safety + UI palette

## Task
3-layer audit after the 2026-07-25 OOM/startup outage found 2 P1 crash-risk bugs, 8 P2
correctness risks, and 5 P3 cleanup items. The P1s are a live-fire race condition
(dict-changed-during-iteration in the circuit breaker) and a blocking `time.sleep(2)` in
an async context that freezes the event loop on every broker CRUD write. P2s cover
error-type loss across the conn_service boundary, a held-lock-during-network-download in
Dhan instruments, and latent OOM paths that are currently dead but dangerous to leave.
Frontend has two palette violations and an oversized error-trim.

## Agents

- backend: (1) Delete dead `_trigger_instruments_store_populate()` function from
  `backend/api/routes/quote.py` (lines 921-936) — it recreates the OOM storm if re-called.
  (2) Add `asyncio.Semaphore(2)` guard to `get_or_fetch_all_today()` in
  `backend/api/persistence/instruments_store.py:215` to cap concurrent broker.instruments()
  calls (currently unreachable but public API — add guard before it ships a new caller).
  (3) In `backend/api/background.py` move `_task_warm_backfill._fired = True` (currently
  line 4393, set before collection) to after the empty-symbols check so a future unguarded
  await can't permanently suppress backfill. (4) Make `_is_dhan_interval_due()` in
  `backend/brokers/broker_apis.py:421` consistent: it reads `_dhan_next_poll` without
  `_dhan_next_poll_lock` while the two writers hold the lock — either snapshot inside the
  lock or document the deliberate GIL-reliance in a comment and remove the lock from writers.

- frontend: (1) In `frontend/src/lib/api.js:87-103` reduce `_trimDetail()` clip from 60
  chars to 35 chars (CLAUDE.md mandates ~25-35 for error banners). (2) In
  `frontend/src/lib/MarketPulse.svelte` around line 3866 replace hardcoded
  `bg-red-500/15 text-red-300 border-red-500/40` Tailwind classes on the error banner
  with CSS variable tokens `--c-short` / `--c-short-10` (palette violation). (3) Same fix
  in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` around line 3976. (4)
  Remove dead CSS block `.bh-badge`, `.bh-badge-green`, `.bh-badge-amber`, `.bh-badge-red`,
  `.bh-dot`, `.bh-label` from `frontend/src/lib/BrokerHealthBadge.svelte:144-192`.

- broker: (1) P1 — In `backend/brokers/connections.py:1584` wrap the synchronous
  `self._build_conn_map(rows, _dhan_deferred)` call inside `async rebuild_from_db()` with
  `asyncio.to_thread(self._build_conn_map, rows, _dhan_deferred)`. The sync helper contains
  `time.sleep(2)` per Dhan account (line 1532) which blocks the event loop on every admin
  broker-CRUD write and at startup. (2) P1 — In `backend/brokers/broker_apis.py:804`
  inside `_persist_cb_state()`, snapshot `_FETCH_HEALTH` under `_BREAKER_LOCK` before
  iterating: `with _BREAKER_LOCK: snapshot = dict(_FETCH_HEALTH)` then iterate `snapshot`.
  (3) P2 — In `backend/brokers/service/routes.py:509-513` (call_broker exception handler)
  add `"error_type": type(e).__name__` to the error response dict so `RemoteBroker._call()`
  can re-raise the correct typed exception instead of demoting everything to `BrokerError`.
  (4) P2 — In `backend/brokers/adapters/dhan.py:470-476` fix `_ensure_dhan_instruments()`:
  extract the urlopen call into `_load_dhan_instruments_body()` (no lock), then acquire
  `_dhan_instruments_lock` only to swap the globals. (5) P2 — In
  `backend/brokers/broker_apis.py:443-448` fix `_update_dhan_next_poll()`: snapshot dict
  under lock, release lock, then write JSON to disk. (6) P3 — In
  `backend/brokers/client/remote_broker.py:102` remove dead `resp.raise_for_status()`.
  (7) P3 — In `backend/brokers/client/remote_broker.py:49-57` initialise `_client` eagerly
  at module load (not lazy + unguarded) to eliminate double-construction race; same fix in
  `backend/brokers/client/sync.py:34-42`. (8) P3 — In `backend/brokers/adapters/dhan.py:58`
  change `"DH-906": BrokerOrderError` to `"DH-906": BrokerAuthError`.

- doc: Update `docs/specs/BROKER_SPEC.md`: (1) Add a section documenting the `_supervised`
  park/sleep contract — any task that returns early MUST `await asyncio.sleep(N)` before
  returning. List the four existing examples. (2) Document `_TOKEN_MAP_FETCH_LOCK`
  double-checked-locking pattern. (3) Document 120s startup delay in `_task_instruments`.
  (4) Document removal of `_trigger_instruments_store_populate` and why.

- backend-test: Write pytest tests in `backend/tests/broker/test_broker_robustness_batch2.py`:
  (1) `test_persist_cb_state_no_dict_race` — spawn a thread calling `_record_fetch()` with
  a new account key while the main thread calls `_persist_cb_state()`; assert no
  RuntimeError after 50 concurrent iterations. (2) `test_rebuild_from_db_nonblocking` —
  mock `_build_conn_map` with a 200ms sleep and verify the asyncio event loop can process
  another task concurrently (i.e., `asyncio.to_thread` is used, not direct call).
  (3) `test_error_type_round_trip` — call `call_broker` route handler with a mock that
  raises `BrokerAuthError`; assert the JSON response contains `"error_type": "BrokerAuthError"`.

- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(hardening): CB dict-race, rebuild async, error_type propagation, Dhan lock scope, dead OOM path removal

## Done when
- P1s fixed: _FETCH_HEALTH snapshot under lock; rebuild_from_db uses asyncio.to_thread
- P2s fixed: typed exceptions survive conn_service boundary; _ensure_dhan_instruments releases lock before download; _trigger_instruments_store_populate deleted; Semaphore(2) on get_or_fetch_all_today
- P3s fixed: DH-906→BrokerAuthError; dead raise_for_status removed; _get_client eager-init
- Frontend: error banners use CSS var tokens; trim at 35 chars; dead BrokerHealthBadge CSS removed
- Docs: BROKER_SPEC documents _supervised park contract + OOM startup sequence
- pytest green; svelte-check 0 errors
