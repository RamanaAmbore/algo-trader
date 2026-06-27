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

from backend.conn_service.routes import (
    BrokerDispatchController,
    HealthController,
    InternalBrokerController,
)


def create_app() -> Litestar:
    """Construct the conn_service Litestar app.

    Mirrors backend.api.app.create_app() shape so the systemd unit
    can keep the same uvicorn entrypoint (`backend.conn_service.app:app`).
    """
    app = Litestar(
        route_handlers=[
            HealthController,
            InternalBrokerController,
            BrokerDispatchController,
        ],
        on_startup=[_init_connections_on_startup],
        debug=False,
    )
    return app


async def _init_connections_on_startup(app: Litestar) -> None:
    """Build the Connections singleton on service boot.

    Called by the Litestar startup hook. Loads broker_accounts from
    the DB and constructs each KiteConnection / DhanConnection /
    GrowwConnection. After this returns, fetch_* calls in routes.py
    can use the loaded singleton.
    """
    import logging
    from backend.shared.helpers.connections import Connections

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
