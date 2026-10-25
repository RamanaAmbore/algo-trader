"""2026-10 notification-masking audit, Wave 2 cluster 4 —
`backend.brokers.registry.get_broker()` raised its `KeyError`/`ValueError`
with the raw broker account code embedded. Those messages are caught
upstream (e.g. `template_attach.py`'s `apply_plan`) and can reach an alert
renderer via `result.errors`. Masked at the point of raising so no future
caller can forget to scrub it.

Account masked at runtime via `mask_account` (not hardcoded) — the
module-global mask registry's exact output depends on test execution
order elsewhere in the suite.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.brokers.registry import get_broker
from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


class TestGetBrokerErrorMessagesMaskAccount:
    @patch("backend.brokers.registry.Connections")
    def test_no_conn_keyerror_masks_account(self, mock_connections_cls):
        mock_conn = MagicMock()
        mock_conn.conn = {}
        mock_connections_cls.return_value = mock_conn

        with pytest.raises(KeyError) as ei:
            get_broker(RAW_ACCOUNT)

        msg = str(ei.value)
        assert RAW_ACCOUNT not in msg
        assert mask_account(RAW_ACCOUNT) in msg

    @patch("backend.brokers.registry.Connections")
    @patch("backend.brokers.registry._ADAPTERS", {"zerodha_kite": MagicMock()})
    def test_unknown_adapter_valueerror_masks_account(self, mock_connections_cls):
        mock_conn = MagicMock()
        mock_conn.conn = {RAW_ACCOUNT: MagicMock()}
        mock_connections_cls.return_value = mock_conn

        with patch("backend.brokers.registry._broker_id_for", return_value="unknown_broker"):
            with pytest.raises(ValueError) as ei:
                get_broker(RAW_ACCOUNT)

        msg = str(ei.value)
        assert RAW_ACCOUNT not in msg
        assert mask_account(RAW_ACCOUNT) in msg
