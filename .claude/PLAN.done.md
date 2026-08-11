# Plan: Broker connection resilience — pre-warm, cooloff persist, Groww hardening, false-amber fix

## Task

**Root cause of 2026-08-11 morning outage** (both Dhan accounts, ~8h, chip showed green throughout):
- Both Dhan tokens were minted at the same time the previous morning → expired together at ~same
  time today (23h window). No pre-warm ran (dead in conn-service). First holdings call at market
  open entered `_dhan_conn_under_lock`, tried `_try_renew()` (failed — stale token rejected),
  fell to `_do_login()` via IPv6 source-bound session to `auth.dhan.co`. Login failed (network
  blip or Dhan auth API down) → `_login_blocked_until = now + 120s`. Retried every 120s — loop
  continued until the underlying cause resolved. Both accounts failed simultaneously because tokens
  were co-minted and both go through IPv6 source binding for login.
- Chip stayed green throughout: `_health_heartbeat` iterates `Connections().conn.keys()` and
  calls `record_session_ok(account)` every 90s unconditionally. Even as API calls wrote
  `last_fail_at`, the heartbeat reset `last_ok_at` 90s later → chip green. Docstring literally
  says "connection object present in conn is sufficient evidence that the session token is
  established" — this assumption is wrong.

Seven confirmed defects to fix (all re-audited clean):

1. **P1** Token pre-warm dead in production — `_task_token_refresh` in `background.py:416`
   iterates `Connections().conn = {}` under `RAMBOQ_USE_CONN_SERVICE=1`. Conn-service
   `on_startup` has no equivalent. Kite expires at 6AM IST; Dhan expires 23h from mint (both
   accounts co-mint → co-expire).

2. **P1** Dhan `_login_blocked_until` memory-only (`connections.py:744`). Restart during a
   failure window resets to 0.0, allowing immediate re-hammering of Dhan's rate-limited endpoint.

3. **P2** Groww no token expiry check — `get_groww_conn()` returns `self._groww`
   unconditionally; no `_conn_created_at`, `_is_token_expired()`, `CONN_RESET_HOURS`.

4. **P2** Groww no login rate-limit cooloff — `refresh()` calls `_build()` directly with no
   throttle on repeated failures.

5. **P2** Groww no pre-warm in any execution mode — `isinstance(conn, KiteConnection)` filter
   at `background.py:417` skips Groww; conn-service has nothing for Groww either.

6. **P2** Health heartbeat (`service/app.py:257-276`) stamps `record_session_ok` for every key
   in `Connections().conn` without checking token validity — iterates `.keys()` only so has no
   access to the conn object. Expired tokens show chip-green until the next real API call fails,
   which is then overridden by the next 90s heartbeat tick anyway.

7. **P2** False-amber on Dhan cold priority: `_BROKER_HEALTH_FRESH_WINDOW_S = 300s` <
   cold poll interval `600s` → chip oscillates amber→green→amber every cycle even when healthy.

Also fix: redundant second `_try_restore_token()` at `connections.py:1145` (no-op — reads same
file twice in sequence on contended lock path when no cached token exists).

---

## Agents

### broker
File scope: `backend/brokers/connections.py`, `backend/brokers/service/app.py`

**1. Token pre-warm task in conn-service** (`service/app.py`)

Add `_task_prewarm_tokens()` as a background coroutine, appended to the `on_startup` list
alongside `_start_kite_ticker`. Run as an hourly loop (not a one-shot): this handles both Kite
(fixed 6AM expiry) and Dhan (23h from mint — variable per account, co-expiry risk when
co-minted):

```
async def _task_prewarm_tokens():
    await asyncio.sleep(60)  # let startup settle
    while True:
        now_ist = datetime.now(IST)
        for account, conn in list(Connections().conn.items()):
            if isinstance(conn, KiteConnection):
                # Kite: pre-warm 5:45–5:59 AM window only
                if now_ist.hour == 5 and now_ist.minute >= 45:
                    conn._login()  # mints fresh token before 6AM cutoff
            elif isinstance(conn, DhanConnection):
                # Dhan: pre-warm if expired or expiring within 60 min
                if conn._is_token_expired() or \
                   (conn._conn_created_at and
                    time.time() > conn._conn_created_at.timestamp() + 22*3600):
                    conn.get_dhan_conn(test_conn=True)  # triggers renew/mint
            elif isinstance(conn, GrowwConnection):
                if conn._is_token_expired():
                    conn.refresh()
        await asyncio.sleep(3600)  # check hourly
```

Logging requirements (all at module logger, prefixed `[PREWARM]`):
- Before each attempt: `INFO "[PREWARM] {account} ({broker}): token age {N}h — refreshing"`
- On success: `INFO "[PREWARM] {account}: token renewed OK (new expiry in ~23h)"`
- On renew falling through to full TOTP mint: `WARNING "[PREWARM] {account}: renew failed — falling back to full TOTP mint"`
- On mint failure: `ERROR "[PREWARM] {account}: mint failed — {err}; chip will go amber in {_BROKER_HEALTH_FRESH_WINDOW_S}s"`
- On skip (token still valid, not expiring soon): `DEBUG "[PREWARM] {account}: token fresh, skipping"`
Errors caught per-account (one failure must not abort the loop for other accounts).

**2. Persist Dhan `_login_blocked_until`** (`connections.py`)

In `DhanConnection._mint_and_build()` (line 1093), after setting
`self._login_blocked_until = _time_mod.time() + 120.0`:
- Write `{"blocked_until": self._login_blocked_until}` to
  `/tmp/ramboq_dhan_login_cooloff.json` (distinct from `/tmp/ramboq_dhan_cooloff.json`
  which is the poll-priority gate — do not merge them).

In `DhanConnection._try_restore_token()`, after loading the cached token:
- Read `/tmp/ramboq_dhan_login_cooloff.json` if exists
- If `blocked_until > time.time()`: `self._login_blocked_until = blocked_until`
  → `WARNING "[DHAN-COOLOFF] {account}: login rate-limit cooloff restored from disk — {wait_s}s remaining; auth blocked until {datetime}"`
- Else: discard (expired cooloff — allow normal login)
  → `DEBUG "[DHAN-COOLOFF] {account}: prior cooloff expired, cleared"`
- Wrap in try/except → `WARNING "[DHAN-COOLOFF] {account}: failed to read login cooloff file: {err}"`

Also add to `_mint_and_build` on failure (line 1094), alongside setting `_login_blocked_until`:
→ `ERROR "[DHAN-LOGIN] {account}: login failed — cooloff active for 120s (persisted to disk). Next retry at {datetime}. Cause: {err}"`

On success (line 1104, clearing `_login_blocked_until`):
→ `INFO "[DHAN-LOGIN] {account}: login succeeded — cooloff cleared, token valid ~24h"`

**3. Groww token expiry check** (`connections.py`)

`GrowwConnection` additions:
- Class constant: `CONN_RESET_HOURS: int = 23`
- `__init__`: `self._conn_created_at: float = 0.0`
- `_build()` (wherever `self._groww` is assigned): `self._conn_created_at = time.time()`
- `_is_token_expired(self) -> bool`: `self._conn_created_at > 0 and time.time() > self._conn_created_at + self.CONN_RESET_HOURS * 3600`
- `get_groww_conn()`: if `self._groww is not None and self._is_token_expired()`, call `self.refresh()` before returning.

**4. Groww login rate-limit cooloff** (`connections.py`)

`GrowwConnection` additions (mirror `DhanConnection._check_login_rate_limit`):
- `__init__`: `self._login_blocked_until: float = 0.0`
- `_check_login_rate_limit(self, *, test_conn: bool = False)`:
  - If inside cooloff AND `test_conn=True`: raise `RuntimeError("Groww login rate-limited")`
  - If inside cooloff AND `test_conn=False`: return stale `_groww` if available, else raise
- `refresh()`: call `_check_login_rate_limit()` before acquiring lock. On `_build()` failure:
  `self._login_blocked_until = time.time() + 120.0`
  → `ERROR "[GROWW-LOGIN] {account}: auth failed — cooloff 120s. Next retry at {datetime}. Cause: {err}"`
  On success (token minted):
  → `INFO "[GROWW-LOGIN] {account}: auth succeeded — token valid ~23h"`
  `_check_login_rate_limit` when blocking:
  → `WARNING "[GROWW-LOGIN] {account}: rate-limit active, {wait_s}s remaining — {'raising' if test_conn else 'returning stale client'}"`

**5. Health heartbeat token validity gate** (`service/app.py:257-276`)

Change `for account in list(Connections().conn.keys())` →
`for account, conn_obj in list(Connections().conn.items())`.

Before `record_session_ok(account)`:
```python
if hasattr(conn_obj, '_is_token_expired') and conn_obj._is_token_expired():
    logger.warning("[HEARTBEAT] %s token expired — skipping green stamp; chip will go amber", account)
    continue
```
Log only when transitioning into the expired state (not every 90s tick) — track with a
`_heartbeat_warned: set[str]` module-level set; add account on first warning, remove on
`record_session_ok` after recovery so the WARNING fires again on next expiry.

This lets `last_ok_at` age naturally past `_BROKER_HEALTH_FRESH_WINDOW_S` → amber → red,
giving the operator an accurate chip signal instead of perpetual false-green.

When a previously-expired account becomes valid again (token refreshed by pre-warm):
`INFO "[HEARTBEAT] {account}: token valid again — resuming green stamps"`  
(Emit this by detecting account removal from `_heartbeat_warned` on a subsequent tick.)

**6. Remove redundant `_try_restore_token` call** (`connections.py:1145`)

Line 1141 already calls `_try_restore_token()` unconditionally (the TOCTOU fix). Line 1145
immediately calls it again when `_access_token` is still absent — reads the same file that
just returned nothing. Remove line 1145 only; keep line 1141.

---

### backend
File scope: `backend/api/routes/health.py`, `backend/api/background.py`

**7. Raise `_BROKER_HEALTH_FRESH_WINDOW_S`** (`health.py:731`)

`300.0` → `660.0` (cold interval 600s + 60s slack → cold-priority Dhan sustains green across
a full poll cycle, no more false-amber oscillation).

**8. Early-return warning in `_task_token_refresh`** (`background.py:416`)

Before the `for conn, account in ...` loop:
```python
if os.environ.get("RAMBOQ_USE_CONN_SERVICE"):
    logger.warning(
        "_task_token_refresh: no-op under conn_service — "
        "token pre-warm is handled by service/app.py _task_prewarm_tokens"
    )
    return
```
Eliminates the misleading INFO log that implies the task ran.

---

### broker-test
File scope: `backend/tests/broker/`

1. **Dhan cooloff survives restart** — trigger `_mint_and_build` failure, assert
   `/tmp/ramboq_dhan_login_cooloff.json` written. Create fresh `DhanConnection`, assert
   `_login_blocked_until` restored from file and `_check_login_rate_limit(test_conn=True)` raises.

2. **Dhan pre-warm fires when token nears expiry** — mock `DhanConnection._conn_created_at` to
   `now - 22.5*3600`, run one pre-warm loop tick, assert `get_dhan_conn(test_conn=True)` called.

3. **Groww token expiry triggers refresh** — set `_conn_created_at = time.time() - 24*3600`,
   assert `get_groww_conn()` calls `refresh()` before returning.

4. **Groww rate-limit cooloff blocks rapid auth** — set `_login_blocked_until = time.time() + 60`,
   assert `_check_login_rate_limit(test_conn=True)` raises `RuntimeError`.

5. **Health heartbeat skips expired token** — mock `Connections().conn` with one entry whose
   `_is_token_expired()` returns True, run one heartbeat cycle, assert `record_session_ok`
   NOT called for that account.

6. **Health heartbeat stamps valid token** — same setup but `_is_token_expired()` returns False,
   assert `record_session_ok` IS called.

7. **`_BROKER_HEALTH_FRESH_WINDOW_S = 660`** — assert constant value in `health.py`.

---

### doc: skip
BROKER_SPEC updated in post-commit spec-sync step.

### frontend: skip

### playwright: skip

---

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

---

## Commit message
fix(brokers): conn-service pre-warm, Dhan cooloff persist, Groww token expiry + rate-limit, heartbeat validity gate, false-amber threshold

---

## Done when
- `service/app.py` hourly pre-warm task refreshes Kite (5:45AM window), Dhan (expiring within 1h), Groww (expired)
- Dhan `_login_blocked_until` persisted to `/tmp/ramboq_dhan_login_cooloff.json` and restored on startup
- Groww: `CONN_RESET_HOURS=23`, `_conn_created_at`, `_is_token_expired()`, `_check_login_rate_limit()` implemented
- `_health_heartbeat` iterates `.items()`, skips `record_session_ok` when `_is_token_expired()` is true
- `_BROKER_HEALTH_FRESH_WINDOW_S = 660.0`
- `_task_token_refresh` logs warning and returns early under conn_service
- Redundant second `_try_restore_token()` at connections.py:1145 removed
- All 7 new broker tests pass; full pytest suite green
