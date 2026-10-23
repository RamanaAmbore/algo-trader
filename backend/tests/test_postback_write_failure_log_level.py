"""
Regression test for audit finding O10 (Wave 1, cluster 1E).

`_pb_event_kite`'s DB write — applying a COMPLETE/REJECTED status to an
AlgoOrder row — is wrapped in a broad try/except that previously logged
any failure at DEBUG. This is a genuine write failure (lost ledger/
template-attach fan-out from the original event), not an expected skip,
and DEBUG-level logging makes it invisible in production log levels
until the 5s postback follow-up verification catches the status
mismatch — but not the side effects already lost from the original
failed write. The log level must be WARNING, matching the file's own
convention for a real write failure.

Note: `orders_postback.py`'s logger has `propagate=False` (see
`ramboq_logger.get_logger`), so pytest's `caplog` fixture (which hooks
the root logger) never sees its records — asserting on the module's
own `logger` mock directly instead, same pattern as
`test_chase_sync_final_status_race.py`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_pb_event_kite_db_write_failure_logs_at_warning_not_debug():
    from backend.api.routes import orders_postback as m

    def _raise_session(*_a, **_k):
        raise RuntimeError("simulated DB write failure")

    with patch.object(m, "logger", wraps=m.logger) as mock_logger, \
         patch("backend.api.database.async_session", _raise_session):
        await m._pb_event_kite(
            order_id="OID1", tradingsymbol="NIFTY25JULFUT", txn="BUY",
            qty=50, price=100.0, status="COMPLETE", status_msg="",
            account="ZG0790",
        )

    mock_logger.debug.assert_not_called(), (
        "a genuine DB write failure applying COMPLETE/REJECTED to the "
        "AlgoOrder row must not be logged at DEBUG — this is a real "
        "failure, not an expected skip."
    )
    mock_logger.warning.assert_called_once(), (
        "the postback event write failure must be logged at WARNING."
    )
    assert "postback event write failed" in mock_logger.warning.call_args[0][0]
