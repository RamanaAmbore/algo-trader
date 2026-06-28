# Connection service

Litestar app that owns every broker session (Kite + Dhan + Groww)
so the main API can restart for backend code changes without tearing
down those sessions.

## Why

The main Litestar API restarts on every backend change. Each restart:

- Tears down the `Connections` singleton (Kite + Dhan + Groww auth).
- Rebinds the KiteTicker WebSocket (~30-90s tick blackout).
- Triggers fresh Dhan logins which hit the 2-min `generate_token`
  rate-limit on multi-account cold-starts.
- Re-mints Groww access tokens via `GrowwAPI.get_access_token`.

This service moves all that lifecycle into its OWN process. Backend
changes in the main API don't touch broker sessions any more.

## What it owns

- `Connections` singleton — Kite + Dhan + Groww connection objects.
- KiteTicker WebSocket + the `_tick_map` per-symbol LTP cache.
- Dhan login state (token cache, rate-limit cool-off, recency guard).
- Groww TOTP session + access-token cache.
- Per-account fetch-health tracker (`broker_apis._FETCH_HEALTH`).

## Transport

Unix domain socket at `/tmp/ramboq_conn.sock` (mode 0660, owner
www-data). The main API speaks HTTP over the UDS via a small client
in `backend/conn_client/` (slice 2 of the migration).

## Process layout

```
ramboq_conn.service        port: UDS /tmp/ramboq_conn.sock
  └─ uvicorn backend.conn_service.app:app
  └─ rare restart (only on connection-layer changes)

ramboq_api.service         port: 8000 (TCP)
  └─ uvicorn backend.api.app:app
  └─ frequent restart (every backend.api/* push)
  └─ delegates every broker call to ramboq_conn via the UDS
```

## Endpoints

| Method | Path                          | What |
|---|---|---|
| GET    | `/health`                     | Readiness + currently-loaded broker count |
| GET    | `/internal/accounts`          | Per-account broker class (Kite/Dhan/Groww) |
| GET    | `/internal/holdings`          | Every account's holdings rows, unioned |
| GET    | `/internal/positions`         | Every account's positions rows |
| GET    | `/internal/margins`           | Every account's funds/margins (raw broker column names) |
| GET    | `/internal/health/brokers`    | Per-account fetch-health snapshot |

All endpoints are internal — no auth at the HTTP layer because the
UDS file mode (0660 owned by www-data) restricts access to the same
OS user that runs `ramboq_api.service`.

## Migration plan (slice progress)

- [x] **Slice 1:** Build conn_service Litestar app + routes
- [x] **Slice 2:** Build `backend/conn_client/` HTTP wrapper (async + sync)
- [x] **Slice 3A:** Flag-gated proxy inside `broker_apis.fetch_*`
- [x] **Slice 3B:** Generic `/internal/broker/{account}/call/{method}`
                   dispatch + `RemoteBroker` (Broker ABC over UDS) +
                   `registry.get_broker` flag-aware + skip
                   `rebuild_from_db` on main API + caller migrations
                   for 8 direct-Connections callers (actions, nav,
                   expiry × 2, instruments, history, snapshot, health,
                   brokers, simulator, orders postback HMAC) +
                   `_start_kite_ticker` fetches access_token via UDS.
- [ ] **Slice 4:** Move KiteTicker WebSocket ownership into
                   conn_service. Main API becomes pure SSE relay
                   (subscribes to a conn_service SSE feed). Removes
                   the last per-restart Kite session in main API.
- [ ] **Slice 5:** Optional — migrate `broker_apis.fetch_*` callers
                   directly to `conn_client` instead of going through
                   the broker_apis proxy. Saves one extra function-
                   call hop; cosmetic, not behavioural.
- [ ] **Slice 6:** Update `webhook/deploy.sh` to recognize the
                   conn_service vs api distinction and only restart
                   the right service per push.

## Cutover flag — `RAMBOQ_USE_CONN_SERVICE`

After slice 3A, the cutover is a single env var on the main API
process:

  • Unset (default) — main API runs broker code in-process, same
    as today. No behaviour change.
  • Set to `1` — `broker_apis.fetch_holdings/positions/margins/
    health_snapshot` proxy through conn_client.sync to the UDS
    instead of calling the local Connections singleton.

Flip it on by installing the drop-in at
`webhook/ramboq_api.service.d-conn.conf` into the systemd unit
directory. See that file's header comment for the exact steps.

Conn_service ITSELF must run with the flag UNSET — otherwise its
own `broker_apis.fetch_*` call inside `routes.py` would recurse
back into itself via the UDS. The `ramboq_conn.service` unit
deliberately doesn't set the env var.

## Running locally

```bash
# Foreground (for development)
uvicorn backend.conn_service.app:app --uds /tmp/ramboq_conn.sock --workers 1

# Health check
curl --unix-socket /tmp/ramboq_conn.sock http://localhost/health
```

On production, the systemd unit at `etc/systemd/system/ramboq_conn.service`
manages the lifecycle. See that file for the canonical ExecStart.
