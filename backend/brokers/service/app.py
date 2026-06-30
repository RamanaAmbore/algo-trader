"""Connection service — Litestar app that owns every broker session.

Scope (what this process owns, and what nothing else gets to touch):

  • Connections singleton (Kite + Dhan + Groww — all 3 brokers).
  • KiteTicker WebSocket + tick map.
  • Dhan login state (token cache, rate-limit cool-off, recency guard).
  • Groww TOTP session + access-token cache.
  • Per-account fetch-health tracker (broker_apis._FETCH_HEALTH).

Why a separate process:

The main Litestar API restarts on every backend code change (NAV
math, mask registry, route logic). Each restart tears down the
broker sessions above and forces fresh logins, which:
  • Triggers Dhan's 2-min generate_token rate-limit on multi-account
    cold-starts (the DH3747 stuck-loop the operator saw today).
  • Rebinds the KiteTicker WebSocket (~30-90s tick blackout).
  • Re-mints Groww TOTP tokens via GrowwAPI.get_access_token.

Isolating broker lifecycle in its own process means backend changes
in the main API service don't reach these sessions.

Transport — Unix domain socket at /tmp/ramboq_conn.sock. The main
API (ramboq_api.service) speaks HTTP over the UDS via a thin client
in backend/conn_client/.

Process boundary:

  systemctl restart ramboq_api    ← happens often (every backend push)
      ↓ does NOT touch broker sessions
  systemctl restart ramboq_conn   ← rare (connection code change)
      ↓ tears down sessions, triggers fresh logins
"""

from __future__ import annotations

import logging

from litestar import Litestar

from backend.brokers.service.routes import (
    BrokerDispatchController,
    HealthController,
    InternalBrokerController,
)

# Module-level logger — getLogger is singleton-per-name; hoisting it here
# avoids repeated lookups inside each startup/watchdog function body.
logger = logging.getLogger(__name__)


def create_app() -> Litestar:
    """Construct the conn_service Litestar app.

    Mirrors backend.api.app.create_app() shape so the systemd unit
    can keep the same uvicorn entrypoint (`backend.brokers.service.app:app`).
    """
    app = Litestar(
        route_handlers=[
            HealthController,
            InternalBrokerController,
            BrokerDispatchController,
        ],
        on_startup=[_init_connections_on_startup, _start_kite_ticker],
        debug=False,
    )
    return app


def _pick_kite_account() -> tuple[str | None, str | None, str]:
    """Walk the loaded Connections singleton and return
    (api_key, access_token, account_code) for the first Kite account
    whose token is live. Returns (None, None, "") when no Kite account
    is authenticated yet — caller decides whether to retry."""
    from backend.brokers.connections import Connections

    for acct, conn in Connections().conn.items():
        tok_getter = getattr(conn, "get_access_token", None)
        ak = getattr(conn, "api_key", None)
        if tok_getter is None or not ak:
            continue  # non-Kite (Dhan / Groww)
        try:
            tok = tok_getter()
        except Exception:
            continue
        if tok:
            return ak, tok, acct
    return None, None, ""


def _try_start_ticker() -> bool:
    """Single attempt to start the KiteTicker against the first live
    Kite account. Returns True when the ticker is started (or already
    running). False when no token is available yet; caller should retry."""
    log = logger
    from backend.brokers.kite_ticker import get_ticker
    from backend.brokers.tick_buffer import TickBufferWriter

    ticker = get_ticker()
    # Already running — nothing to do. status()["started"] is the
    # authoritative flag inside TickerManager.
    if ticker.status().get("started"):
        return True

    # Buffer is a process singleton; safe to attach idempotently.
    if getattr(ticker, "_tick_buffer", None) is None:
        try:
            ticker.attach_tick_buffer(TickBufferWriter())
        except Exception:
            log.exception("conn_service: tick buffer attach failed")
            return False

    api_key, access_token, ticker_account = _pick_kite_account()
    if not api_key or not access_token:
        return False

    import asyncio as _asyncio
    ticker.set_loop(_asyncio.get_event_loop())
    ticker.start(api_key, access_token, account=ticker_account)
    log.info(
        "conn_service: KiteTicker started · account=%s · mmap=/dev/shm/ramboq_ticks",
        ticker_account,
    )
    return True


async def _start_kite_ticker(app: Litestar) -> None:
    """Start the KiteTicker WebSocket and spawn the supervising watchdog.

    Slice 4: KiteTicker lives in conn_service. ramboq_api consumes
    LTPs by mmap'ing the buffer — no UDS hop per read, no WS in the
    main process, no broker re-auth when ramboq_api restarts.

    First attempt happens inline so a successful boot doesn't waste
    a 30s tick. If no Kite account is authenticated yet (chicken-and-egg
    when rebuild_from_db just re-minted the token), the watchdog task
    keeps trying every 30s until it succeeds or the process exits.
    """
    log = logger
    try:
        if _try_start_ticker():
            return
        log.warning(
            "conn_service: no live Kite access_token at startup — "
            "watchdog will retry every 30s until success"
        )
    except Exception:
        log.exception("conn_service: KiteTicker startup failed")

    # Spawn the watchdog. Use create_task on the running loop so the
    # task outlives this on_startup hook. Litestar's lifespan keeps
    # the loop alive for the process's lifetime.
    import asyncio as _asyncio
    _asyncio.get_event_loop().create_task(_ticker_watchdog())


async def _ticker_watchdog() -> None:
    """Background supervisor that brings the ticker up when it isn't
    running yet (chicken-and-egg at boot when Connections is still
    re-minting tokens) AND restarts it if the WebSocket dies later.

    Interval: 30s. Cheap — status() is a single dict copy plus a few
    field reads; no broker calls. When the ticker is healthy the loop
    just sleeps; when it isn't, it tries _try_start_ticker() which
    is idempotent."""
    import asyncio
    log = logger
    from backend.brokers.kite_ticker import get_ticker

    INTERVAL_S = 30.0
    while True:
        try:
            await asyncio.sleep(INTERVAL_S)
            ticker = get_ticker()
            status = ticker.status()
            if status.get("started") and status.get("connected"):
                # All good — nothing to do.
                continue
            # Not started OR disconnected — try to (re)start.
            if _try_start_ticker():
                log.info(
                    "ticker_watchdog: ticker recovered "
                    "(prev state started=%s connected=%s)",
                    status.get("started"), status.get("connected"),
                )
        except asyncio.CancelledError:
            return
        except Exception:
            # Don't let the watchdog die on a transient error; log and
            # continue. The next iteration will retry.
            log.exception("ticker_watchdog: loop error")


async def _init_connections_on_startup(app: Litestar) -> None:
    """Build the Connections singleton on service boot.

    Called by the Litestar startup hook. Loads broker_accounts from
    the DB and constructs each KiteConnection / DhanConnection /
    GrowwConnection. After this returns, fetch_* calls in routes.py
    can use the loaded singleton.
    """
    from backend.brokers.connections import Connections

    log = logger
    log.info("conn_service: rebuilding Connections singleton on startup")
    try:
        await Connections().rebuild_from_db()
        log.info(
            "conn_service: Connections ready · accounts=%s",
            sorted(Connections().conn.keys()),
        )
    except Exception as e:
        # Don't refuse to start — partial connectivity (e.g. Dhan
        # rate-limited at cold-start) is fine; subsequent fetch calls
        # will retry through the existing Connections-level retry
        # logic. Log loudly so the operator sees the boot state.
        log.exception("conn_service: rebuild_from_db failed: %s", e)


# Module-level `app` is the uvicorn entrypoint.
app = create_app()
