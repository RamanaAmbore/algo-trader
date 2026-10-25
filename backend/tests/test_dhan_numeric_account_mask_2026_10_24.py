"""
Regression tests for the 2026-10-24 Dhan numeric-account-id masking gap.

Background: `mask_account_in_text()` (and the new centralized
`_AccountMaskFilter`, see test_log_store.py) rewrite account codes via
`_ACCT_IN_TEXT_RE = re.compile(r'\\b([A-Z]{2}[A-Z0-9]{4,8})\\b')` — a
regex that requires two leading uppercase letters. Dhan's raw
`dhanClientId` is purely numeric (e.g. "1100012345"), a shape that regex
can never match. Three sites needed explicit, numeric-safe handling:

  1. `orders_postback.py:_sync_algo_order_rows` — the CRITICAL
     fuzzy-match log and the WARNING orphan-row log both interpolated
     the raw `account` value directly; both now wrap it in
     `mask_account()`.
  2. `orders.py` — the Dhan and Groww postback routes both logged the
     FULL raw payload dict (`{body!r}`), which can't be regex-masked
     reliably once the account id is embedded inside a dumped dict.
     Both now route through `_pb_redact_payload_for_log()`.

Amended 2026-10-24 (same day, follow-up fix): `_pb_redact_payload_for_log()`
originally redacted `dhanClientId` to the literal string "<redacted>".
Now that `register_accounts()` aliases each account's raw numeric
`client_id` to that account's own already-computed mask (see
`backend/tests/broker/test_dhan_client_id_mask_alias.py`), the function
masks the value in place via `mask_account()` instead — a genuinely
useful, distinguishable string (e.g. `D1####`) rather than an opaque
placeholder, while never leaking the raw digits. `body` itself, used by
the real parsers downstream, remains untouched either way.

Deliberately NOT masked: `orders.py:_rco_parse_dhan_postback_body`'s own
`account` extraction (~line 1211) — that raw value feeds real DB/account
routing downstream (`_process_broker_postback` → `_sync_algo_order_rows`),
and `_process_broker_postback` already masks it for its OWN log line at
`masked = mask_account(account)`. Masking at extraction would silently
break Dhan fill reconciliation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_RAW_DHAN_ID = "1100012345"  # purely numeric — the regex gap


def _empty_rows_session():
    _rows_result = MagicMock()
    _rows_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_rows_result)
    mock_session.commit = AsyncMock()
    return mock_session


# ─────────────────────────────────────────────────────────────────────────
# orders_postback.py:_sync_algo_order_rows — fuzzy-match CRITICAL +
# orphan-row WARNING logs
# ─────────────────────────────────────────────────────────────────────────

class TestSyncAlgoOrderRowsAccountMasking:
    @pytest.mark.asyncio
    async def test_fuzzy_match_critical_log_masks_numeric_dhan_account(self):
        """Regression guard — pre-fix, the CRITICAL log interpolated the
        raw `account` value directly: `acct=%s` with `account` unmasked.
        For a numeric Dhan id this leaked straight into the log file and
        log_events.message (the generic account-mask filter can't catch
        a purely-numeric code).

        Patches `m.logger` directly rather than using `caplog` — this
        module's loggers are built via `get_logger()`, which sets
        `propagate = False` on a `QueueHandler`-based logger (see
        ramboq_logger.py), so records never reach caplog's root-logger
        capture handler regardless of level."""
        from backend.api.routes import orders_postback as m

        fallback_row = MagicMock()
        fallback_row.id = 77
        mock_session = _empty_rows_session()

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch.object(m, "_pb_fallback_lookup_row", new=AsyncMock(return_value=fallback_row)), \
             patch.object(m, "_pb_write_ledger_fills", new=AsyncMock()), \
             patch.object(m, "logger") as mock_logger:
            await m._sync_algo_order_rows(
                broker_id="dhan", order_id="bo-9", status="TRADED",
                price=101.0, status_message="", qty=50,
                account=_RAW_DHAN_ID, symbol="RELIANCE", txn="BUY",
            )

        mock_logger.critical.assert_called_once()
        args, kwargs = mock_logger.critical.call_args
        rendered = " ".join(str(a) for a in args)
        assert _RAW_DHAN_ID not in rendered, (
            "raw numeric Dhan account id leaked into the fuzzy-match "
            "CRITICAL log line"
        )

    @pytest.mark.asyncio
    async def test_orphan_row_warning_log_masks_numeric_dhan_account(self):
        """Same gap, the WARNING orphan-row-creation log line."""
        from backend.api.routes import orders_postback as m

        mock_session = _empty_rows_session()

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch.object(m, "_pb_fallback_lookup_row", new=AsyncMock(return_value=None)), \
             patch.object(m, "_create_postback_orphan_row", new=AsyncMock()), \
             patch.object(m, "logger") as mock_logger:
            await m._sync_algo_order_rows(
                broker_id="dhan", order_id="bo-10", status="TRADED",
                price=101.0, status_message="", qty=50,
                account=_RAW_DHAN_ID, symbol="RELIANCE", txn="BUY",
            )

        mock_logger.warning.assert_called_once()
        args, kwargs = mock_logger.warning.call_args
        rendered = " ".join(str(a) for a in args)
        assert _RAW_DHAN_ID not in rendered, (
            "raw numeric Dhan account id leaked into the orphan-row "
            "WARNING log line"
        )


# ─────────────────────────────────────────────────────────────────────────
# orders.py — raw payload dumps + _pb_redact_payload_for_log
# ─────────────────────────────────────────────────────────────────────────

class TestRedactPayloadForLog:
    def test_redacts_dhan_client_id(self):
        from backend.api.routes.orders import _pb_redact_payload_for_log
        body = {"dhanClientId": _RAW_DHAN_ID, "orderId": "O1", "orderStatus": "TRADED"}
        out = _pb_redact_payload_for_log(body)
        assert out["dhanClientId"] != _RAW_DHAN_ID
        assert _RAW_DHAN_ID not in repr(out)
        # Other fields pass through unchanged.
        assert out["orderId"] == "O1"
        assert out["orderStatus"] == "TRADED"

    def test_masks_in_place_using_the_registered_client_id_alias(self):
        """2026-10-24 follow-up: once `register_accounts()` has aliased
        this client_id to its paired account's mask, the redacted value
        must be that SAME useful mask — not a placeholder, and not an
        independently-derived (and potentially colliding) mask."""
        from backend.shared.helpers import utils as utils_mod
        from backend.shared.helpers.utils import mask_account, register_accounts
        from backend.api.routes.orders import _pb_redact_payload_for_log

        saved = dict(utils_mod._REGISTRY)
        try:
            register_accounts(["DH3747", "DH6847"], {"DH3747": _RAW_DHAN_ID})
            expected = mask_account("DH3747")
            body = {"dhanClientId": _RAW_DHAN_ID, "orderId": "O1"}
            out = _pb_redact_payload_for_log(body)
            assert out["dhanClientId"] == expected, (
                f"expected the aliased mask {expected!r}, got {out['dhanClientId']!r}"
            )
            assert _RAW_DHAN_ID not in repr(out)
        finally:
            utils_mod._REGISTRY = saved

    def test_does_not_mutate_the_original_body(self):
        """The real parsers downstream (`_rco_parse_dhan_postback_body`)
        need the RAW value for account routing — redaction must happen
        on a copy, never on `body` itself."""
        from backend.api.routes.orders import _pb_redact_payload_for_log
        body = {"dhanClientId": _RAW_DHAN_ID}
        _pb_redact_payload_for_log(body)
        assert body["dhanClientId"] == _RAW_DHAN_ID

    def test_is_a_noop_for_a_body_with_no_sensitive_keys(self):
        """Groww's payload carries no account identifier at all."""
        from backend.api.routes.orders import _pb_redact_payload_for_log
        body = {"groww_order_id": "G1", "order_status": "COMPLETE"}
        assert _pb_redact_payload_for_log(body) == body

    def test_handles_non_dict_input_gracefully(self):
        from backend.api.routes.orders import _pb_redact_payload_for_log
        assert _pb_redact_payload_for_log(None) is None


class TestDhanPostbackRouteRedactsRawPayloadLog:
    @pytest.mark.asyncio
    async def test_dhan_postback_route_never_logs_the_raw_client_id(self):
        """Behavioral check on the real route handler — the raw numeric
        dhanClientId must never appear in any log record emitted while
        processing a Dhan postback, even though the SAME value is still
        correctly forwarded to the real processing pipeline.

        Patches `m.logger` directly (see the class above's docstring for
        why `caplog` can't see records from a `get_logger()`-built,
        `propagate=False` logger)."""
        from backend.api.routes.orders import OrdersController
        from backend.api.routes import orders as m

        fn = OrdersController.order_postback_dhan.fn
        req = MagicMock()
        req.query_params = {"token": "s3cr3t"}
        body = {
            "dhanClientId": _RAW_DHAN_ID, "orderId": "O1", "orderStatus": "TRADED",
            "tradingSymbol": "RELIANCE", "transactionType": "BUY",
            "filledQuantity": 1, "averageTradedPrice": 100.0,
        }
        req.json = AsyncMock(return_value=body)

        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}), \
             patch.object(m, "_process_broker_postback", new=AsyncMock()) as mock_process, \
             patch.object(m, "_subscribe_filled_pairs", new=AsyncMock()), \
             patch.object(m, "logger") as mock_logger:
            result = await fn(MagicMock(), req)

        assert result == {"status": "ok"}
        # The redacted payload log must not leak the raw numeric id, on
        # any level/call.
        for call in (mock_logger.info.call_args_list + mock_logger.warning.call_args_list
                     + mock_logger.critical.call_args_list):
            args, kwargs = call
            rendered = " ".join(str(a) for a in args) + " ".join(str(v) for v in kwargs.values())
            assert _RAW_DHAN_ID not in rendered, (
                f"raw Dhan client id leaked into a log call: {call!r}"
            )
        # But the real downstream pipeline still receives the RAW value
        # (routing must not be broken by the logging fix).
        mock_process.assert_awaited_once()
        assert mock_process.await_args.kwargs["account"] == _RAW_DHAN_ID

    @pytest.mark.asyncio
    async def test_groww_postback_route_redaction_is_a_noop_but_safe(self):
        """Groww's payload carries no account id — confirms the shared
        redaction call site doesn't break anything for it."""
        from backend.api.routes.orders import OrdersController
        from backend.api.routes import orders as m

        fn = OrdersController.order_postback_groww.fn
        req = MagicMock()
        req.query_params = {"token": "s3cr3t"}
        body = {"groww_order_id": "G1", "order_status": "COMPLETE",
                "trading_symbol": "RELIANCE", "transaction_type": "BUY"}
        req.json = AsyncMock(return_value=body)

        with patch("backend.shared.helpers.utils.secrets", {"groww_postback_token": "s3cr3t"}), \
             patch.object(m, "_process_broker_postback", new=AsyncMock()):
            result = await fn(MagicMock(), req)

        assert result == {"status": "ok"}
