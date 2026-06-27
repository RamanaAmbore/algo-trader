"""Client wrapper for the conn_service broker wrapper.

The main API uses these functions instead of importing
`backend.shared.helpers.broker_apis` directly. The shape is
intentionally identical to broker_apis (`list[DataFrame]` with
`df.attrs['fetch_failed']` per account) so callers can swap the
import line and nothing else.

See `backend/conn_service/README.md` for the architecture.
"""

import os

from backend.conn_client.api import (  # noqa: F401
    fetch_holdings,
    fetch_positions,
    fetch_margins,
    fetch_health_snapshot,
    list_accounts,
)


def is_cutover_on() -> bool:
    """True when RAMBOQ_USE_CONN_SERVICE is set on this process.

    Single source of truth for the cutover-flag check. Sprinkled
    across registry.get_broker, Connections.rebuild_from_db,
    routes/orders.py postback, routes/health.py, app.py
    _start_kite_ticker. Import this helper there so the env-var
    name and accepted values stay in one place."""
    return os.environ.get("RAMBOQ_USE_CONN_SERVICE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
