"""2026-10 notification-masking audit, Wave 2 cluster 4 —
`template_attach.py:_route_apply_path`'s live branch caught a
broker-resolution failure (`get_broker(parent_account)` raising) and built
`result.errors` with the raw account embedded (`{parent_account!r}: {e}`).
`result.errors` is read unmasked by `event_agents.py:_render_partial_gtt`
(also fixed, see test_event_agents_renderer_account_masking.py).

Two sources are masked separately at the catch site: `parent_account` is
known-raw (scalar `mask_account`); `str(e)` may already be masked
(registry.py masks its own raised messages) OR may come back unmasked from
conn_service in cutover mode — masked via the idempotent-safe
`mask_account_in_text` either way. This test simulates the worst case: the
underlying exception text itself still carries a raw account (as if it
came back from a not-yet-upgraded conn_service), to prove the catch site's
own masking is what protects this path, not reliance on the raised
exception already being clean.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


@pytest.mark.asyncio
async def test_broker_resolve_failure_masks_account_in_result_errors():
    from backend.api.algo.template_attach import _route_apply_path

    plan = MagicMock()

    def _raise(_account):
        # Simulates an unmasked error text reaching this catch site from
        # a cold/un-upgraded conn_service in cutover mode — the catch
        # site's own masking must not depend on the exception already
        # being clean.
        raise KeyError(f"No broker client configured for account {RAW_ACCOUNT!r}")

    with patch("backend.brokers.registry.get_broker", side_effect=_raise):
        result = await _route_apply_path(
            plan=plan, apply_path="live",
            parent_account=RAW_ACCOUNT, parent_order_id=1,
        )

    assert len(result.errors) == 1
    err = result.errors[0]
    assert RAW_ACCOUNT not in err
    assert mask_account(RAW_ACCOUNT) in err
