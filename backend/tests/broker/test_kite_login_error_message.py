"""Tests for F2 (2026-11-20 incident): KiteConnection.login() /
totp_authenticate() / setup_access_token() must surface Zerodha's real
response body (e.g. "Invalid CAPTCHA values.") instead of the bare
`requests.exceptions.HTTPError` string ("400 Client Error: Bad Request
for url: ..."). Before this fix, operators only ever saw the generic
HTTP status line in the log and the `broker_connection_events` detail —
never the actionable Zerodha message that would have explained WHY the
login was being rejected (a CAPTCHA challenge, during the live incident).

Five quality dimensions:
  SSOT        — login()/totp_authenticate()/setup_access_token() all
                route through the single `_extract_http_error_message()`
                helper; no duplicated JSON/text-extraction logic per
                method.
  Correctness — JSON body with a "message" key wins; a non-JSON body
                falls back to raw text; an exception with no `.response`
                at all falls back to `str(e)`; every path truncates to
                200 chars.
  Performance — `_retry_count` is patched to 1 attempt so no real
                exponential-backoff sleep occurs across these tests.
  Reuse       — mirrors `test_kite_login_cooloff.py`'s own
                `_kite_conn_bare` bare-construction pattern (avoids
                __init__'s real credential/network setup).
  UX          — every assertion checks the REAL captured message text,
                not just that *some* string was logged.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.brokers.connections import KiteConnection, _extract_http_error_message


def _kite_conn_bare(account: str = "ZG_ERRMSG") -> KiteConnection:
    """Construct a bare KiteConnection via __new__, bypassing __init__'s
    real credential setup / network calls / disk reads — same convention
    as test_kite_login_cooloff.py's own helper."""
    obj = KiteConnection.__new__(KiteConnection)
    obj.account = account
    obj._password = "pw"
    obj.api_key = "ak"
    obj._api_secret = "as"
    obj.totp_token = "tt"
    obj._source_ip = None
    obj.login_url = "http://example.invalid/login"
    obj.twofa_url = "http://example.invalid/twofa"
    obj._access_token = None
    obj._initialized = True
    obj._login_lock = threading.Lock()
    obj._conn_created_at = None
    obj._login_blocked_until = 0.0
    obj._login_fail_streak = 0
    obj._blocked_alert_sent = False
    obj.session = MagicMock()
    obj.kite = MagicMock()
    return obj


class _FakeResponse:
    """Minimal stand-in for `requests.Response` — just enough surface
    for `_extract_http_error_message` and `raise_for_status()`."""

    def __init__(self, json_body=None, text: str = "", status_code: int = 400,
                 json_raises: bool = False):
        self._json_body = json_body
        self.text = text
        self.status_code = status_code
        self._json_raises = json_raises

    def json(self):
        if self._json_raises or self._json_body is None:
            raise ValueError("response body is not valid JSON")
        return self._json_body

    def raise_for_status(self):
        err = requests.exceptions.HTTPError(
            f"{self.status_code} Client Error: Bad Request for url: fake",
            response=self,
        )
        raise err


@pytest.fixture(autouse=True)
def _single_retry_attempt(monkeypatch):
    """Force `@retry_kite_conn`'s per-call attempt count to 1 and strip
    its real exponential-backoff sleep, so login()/totp_authenticate()/
    setup_access_token() make exactly one attempt with no wall-clock
    delay in these tests. `_retry_count` is looked up at DECORATION time
    (it's the literal object passed to `retry_kite_conn(...)`), so
    monkeypatching the module attribute alone has no effect on the
    already-wrapped methods — `get_int` (what `_retry_count()` calls
    internally) is the real knob that's re-read on every call."""
    import backend.shared.helpers.decorators as decorators
    monkeypatch.setattr(
        "backend.shared.helpers.settings.get_int", lambda *a, **k: 1
    )
    monkeypatch.setattr(decorators.time, "sleep", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# _extract_http_error_message — standalone helper behaviour
# ---------------------------------------------------------------------------

class TestExtractHttpErrorMessage:
    def test_prefers_json_message_field(self):
        resp = _FakeResponse(json_body={
            "status": "error",
            "message": "Invalid CAPTCHA values.",
            "data": {"captcha": True},
            "error_type": "InputException",
        })
        err = requests.exceptions.HTTPError("400 Client Error", response=resp)
        assert _extract_http_error_message(err) == "Invalid CAPTCHA values.", (
            f"expected the JSON body's 'message' field, got "
            f"{_extract_http_error_message(err)!r}"
        )

    def test_falls_back_to_raw_text_on_non_json_body(self):
        resp = _FakeResponse(json_raises=True, text="<html>Bad Gateway</html>")
        err = requests.exceptions.HTTPError("502 Bad Gateway", response=resp)
        assert _extract_http_error_message(err) == "<html>Bad Gateway</html>", (
            f"expected raw response text fallback, got "
            f"{_extract_http_error_message(err)!r}"
        )

    def test_falls_back_to_raw_text_when_json_has_no_message_key(self):
        resp = _FakeResponse(json_body={"status": "error", "error_type": "GeneralException"},
                              text='{"status": "error", "error_type": "GeneralException"}')
        err = requests.exceptions.HTTPError("400 Client Error", response=resp)
        result = _extract_http_error_message(err)
        assert "GeneralException" in result, (
            f"expected a fallback to raw text when no 'message' key is "
            f"present, got {result!r}"
        )

    def test_falls_back_to_str_e_when_no_response_at_all(self):
        err = RuntimeError("connection reset by peer")
        assert _extract_http_error_message(err) == "connection reset by peer"

    def test_truncates_to_200_chars(self):
        long_msg = "x" * 500
        resp = _FakeResponse(json_body={"message": long_msg})
        err = requests.exceptions.HTTPError("400 Client Error", response=resp)
        result = _extract_http_error_message(err)
        assert len(result) == 200, f"expected truncation to 200 chars, got {len(result)}"


# ---------------------------------------------------------------------------
# login() — real Zerodha body reaches the log + _emit_conn_event detail
# ---------------------------------------------------------------------------

class TestLoginSurfacesRealErrorMessage:
    def test_login_emit_conn_event_carries_captcha_message(self, monkeypatch):
        conn = _kite_conn_bare("ZG_ERRMSG_LOGIN")
        resp = _FakeResponse(json_body={
            "status": "error",
            "message": "Invalid CAPTCHA values.",
            "data": {"captcha": True},
            "error_type": "InputException",
        })
        conn.session.post = MagicMock(return_value=resp)

        emit_mock = MagicMock()
        monkeypatch.setattr(
            "backend.brokers.connections._emit_conn_event", emit_mock
        )

        with pytest.raises(requests.exceptions.HTTPError):
            conn.login()

        emit_mock.assert_called_once()
        call_args = emit_mock.call_args
        account, broker_id, event_type, detail = call_args[0][:4]
        assert account == "ZG_ERRMSG_LOGIN"
        assert event_type == "auth_fail"
        assert detail["error"] == "Invalid CAPTCHA values.", (
            f"expected the real Zerodha message in the event detail, got "
            f"{detail['error']!r}"
        )
        assert detail["stage"] == "login"

    def test_login_logs_real_message_not_bare_400(self, monkeypatch):
        """connections.py's `logger` has `propagate=False` (see
        ramboq_logger.get_logger), so pytest's `caplog` fixture (which
        attaches to the root logger) never sees these records — mock
        `logger.error` directly instead, same as every other assertion
        in this module."""
        conn = _kite_conn_bare("ZG_ERRMSG_LOGIN2")
        resp = _FakeResponse(json_body={"message": "Invalid CAPTCHA values."})
        conn.session.post = MagicMock(return_value=resp)

        error_mock = MagicMock()
        monkeypatch.setattr(
            "backend.brokers.connections.logger.error", error_mock
        )

        with pytest.raises(requests.exceptions.HTTPError):
            conn.login()

        error_mock.assert_called_once()
        logged_text = str(error_mock.call_args[0][0])
        assert "Invalid CAPTCHA values." in logged_text, (
            f"expected the real Zerodha message in the logged error, got "
            f"{logged_text!r}"
        )
        assert "400 Client Error" not in logged_text, (
            "the logged error must not just be the bare HTTPError status "
            f"line — got {logged_text!r}"
        )


# ---------------------------------------------------------------------------
# totp_authenticate() — same extraction applied
# ---------------------------------------------------------------------------

class TestTotpAuthenticateSurfacesRealErrorMessage:
    def test_totp_emit_conn_event_carries_real_message(self, monkeypatch):
        conn = _kite_conn_bare("ZG_ERRMSG_TOTP")
        resp = _FakeResponse(json_body={"message": "Invalid TOTP token."})
        conn.session.post = MagicMock(return_value=resp)
        monkeypatch.setattr(
            "backend.brokers.connections.generate_totp", lambda token: "000000"
        )

        emit_mock = MagicMock()
        monkeypatch.setattr(
            "backend.brokers.connections._emit_conn_event", emit_mock
        )

        with pytest.raises(requests.exceptions.HTTPError):
            conn.totp_authenticate("req123")

        emit_mock.assert_called_once()
        detail = emit_mock.call_args[0][3]
        assert detail["error"] == "Invalid TOTP token.", (
            f"expected the real Zerodha message, got {detail['error']!r}"
        )
        assert detail["stage"] == "totp_authenticate"


# ---------------------------------------------------------------------------
# setup_access_token() — falls back to str(e) when the SDK exception has
# no `.response` (no behaviour regression from today), but still routes
# through the shared helper for consistent truncation.
# ---------------------------------------------------------------------------

class TestSetupAccessTokenFallsBackGracefully:
    def test_setup_access_token_detail_uses_sdk_exception_str(self, monkeypatch):
        conn = _kite_conn_bare("ZG_ERRMSG_SETUP")
        conn._new_kite = MagicMock(return_value=conn.kite)
        conn.kite.generate_session = MagicMock(
            side_effect=RuntimeError("Invalid checksum. Please login again.")
        )

        emit_mock = MagicMock()
        monkeypatch.setattr(
            "backend.brokers.connections._emit_conn_event", emit_mock
        )

        with pytest.raises(RuntimeError):
            conn.setup_access_token("req_token_abc")

        emit_mock.assert_called_once()
        detail = emit_mock.call_args[0][3]
        assert detail["error"] == "Invalid checksum. Please login again.", (
            f"expected the SDK exception's own message, got {detail['error']!r}"
        )
        assert detail["stage"] == "setup_access_token"
