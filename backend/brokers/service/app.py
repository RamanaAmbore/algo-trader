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

from litestar import Litestar

from backend.brokers.service.routes import (
    BrokerDispatchController,
    HealthController,
    InternalBrokerController,
)


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


async def _start_kite_ticker(app: Litestar) -> None:
    """Start the KiteTicker WebSocket inside conn_service and wire it
    to the shared-memory tick buffer.

    Slice 4: KiteTicker now lives in conn_service. ramboq_api consumes
    LTPs by mmap'ing the buffer — no UDS hop per read, no WS in the
    main process, no broker re-auth when ramboq_api restarts.

    Account selection — first Kite account whose token is live.
    Defer-silent if no Kite account is authenticated yet; the watchdog
    on conn_service's side can retry later.
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        from backend.brokers.connections import Connections
        from backend.brokers.kite_ticker import get_ticker
        from backend.brokers.tick_buffer import TickBufferWriter

        # Attach the shared-memory buffer BEFORE start() so the very
        # first tick frame after handshake is mirrored. Buffer is a
        # process singleton; safe to instantiate here.
        buffer = TickBufferWriter()
        ticker = get_ticker()
        ticker.attach_tick_buffer(buffer)

        api_key = access_token = ticker_account = None
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
                api_key, access_token, ticker_account = ak, tok, acct
                break

        if not api_key or not access_token:
            log.warning(
                "conn_service: no live Kite access_token at startup — "
                "ticker deferred; subscribe calls will trigger start later"
            )
            return

        import asyncio as _asyncio
        ticker.set_loop(_asyncio.get_event_loop())
        ticker.start(api_key, access_token, account=ticker_account)
        log.info(
            "conn_service: KiteTicker started · account=%s · mmap=/dev/shm/ramboq_ticks",
            ticker_account,
        )
    except Exception:
        log.exception("conn_service: KiteTicker startup failed")


async def _init_connections_on_startup(app: Litestar) -> None:
    """Build the Connections singleton on service boot.

    Called by the Litestar startup hook. Loads broker_accounts from
    the DB and constructs each KiteConnection / DhanConnection /
    GrowwConnection. After this returns, fetch_* calls in routes.py
    can use the loaded singleton.
    """
    import logging
    from backend.brokers.connections import Connections

    log = logging.getLogger(__name__)
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
