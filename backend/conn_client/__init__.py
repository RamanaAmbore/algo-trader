"""Client wrapper for the conn_service broker wrapper.

The main API uses these functions instead of importing
`backend.shared.helpers.broker_apis` directly. The shape is
intentionally identical to broker_apis (`list[DataFrame]` with
`df.attrs['fetch_failed']` per account) so callers can swap the
import line and nothing else.

See `backend/conn_service/README.md` for the architecture.
"""

from backend.conn_client.api import (  # noqa: F401
    fetch_holdings,
    fetch_positions,
    fetch_margins,
    fetch_health_snapshot,
    list_accounts,
)
