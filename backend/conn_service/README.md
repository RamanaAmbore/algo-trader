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

- [x] **Slice 1:** Build conn_service Litestar app + routes (this commit)
- [ ] **Slice 2:** Build `backend/conn_client/` HTTP wrapper
- [ ] **Slice 3:** Migrate `broker_apis.fetch_*` callers in main API
- [ ] **Slice 4:** Migrate KiteTicker LTP access (`_ticker.get_ltp`)
- [ ] **Slice 5:** Migrate order placement (`broker.place_order`)
- [ ] **Slice 6:** Update `webhook/deploy.sh` to recognize the
                  conn_service vs api distinction and only restart
                  the right service per push.

Slices 1 and 2 are safe to ship without breaking anything — the
conn_service runs in parallel with the existing in-process broker
code; no production caller talks to it yet. Slices 3-5 progressively
flip callers over; each can be guarded behind a feature flag for
side-by-side verification before the cutover.

## Running locally

```bash
# Foreground (for development)
uvicorn backend.conn_service.app:app --uds /tmp/ramboq_conn.sock --workers 1

# Health check
curl --unix-socket /tmp/ramboq_conn.sock http://localhost/health
```

On production, the systemd unit at `etc/systemd/system/ramboq_conn.service`
manages the lifecycle. See that file for the canonical ExecStart.
