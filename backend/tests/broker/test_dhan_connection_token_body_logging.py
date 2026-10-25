"""2026-10 notification/credentials-masking audit, Wave 2 bundled item —
`DhanConnection._do_login` and `_try_renew` logged the raw Dhan
token-response body (DEBUG and WARNING) — which can carry the start of a
real access token. Fixed to log only status/keys, never the body.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.brokers.connections import DhanConnection


def _make_conn():
    return DhanConnection(
        "DH3747", client_id="1100000000", api_key="k", api_secret="s",
        pin="1234", totp_token="JBSWY3DPEHPK3PXP",
    )


class TestDoLoginNeverLogsRawBody:
    def test_debug_log_has_no_body_only_keys(self):
        conn = _make_conn()
        fake_response = MagicMock(
            status_code=200, content=b'{"accessToken":"super-secret-token-value"}',
        )
        fake_response.json.return_value = {"accessToken": "super-secret-token-value"}
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response

        with patch.object(conn, "_login_session", return_value=fake_session), \
             patch("backend.brokers.connections.generate_totp", return_value="123456"), \
             patch("backend.brokers.connections.logger") as mock_logger:
            token = conn._do_login()

        assert token == "super-secret-token-value"
        debug_calls = [c for c in mock_logger.debug.call_args_list]
        assert len(debug_calls) == 1
        rendered = " ".join(str(a) for a in debug_calls[0].args)
        assert "super-secret-token-value" not in rendered
        assert "keys=" in debug_calls[0].args[0]
        assert ["accessToken"] in debug_calls[0].args


class TestTryRenewNeverLogsRawBody:
    def test_non_dict_response_warning_has_no_body(self):
        conn = _make_conn()
        conn._access_token = "old-token"
        fake_response = MagicMock(status_code=200, content=b'"not-a-dict-secret-blob"')
        fake_response.json.return_value = "not-a-dict-secret-blob"
        fake_session = MagicMock()
        fake_session.get.return_value = fake_response

        with patch.object(conn, "_login_session", return_value=fake_session), \
             patch("backend.brokers.connections.logger") as mock_logger:
            result = conn._try_renew()

        assert result is None
        warn_text = " ".join(
            str(a) for call in mock_logger.warning.call_args_list for a in call.args
        )
        assert "not-a-dict-secret-blob" not in warn_text

    def test_no_access_token_warning_has_no_body(self):
        conn = _make_conn()
        conn._access_token = "old-token"
        fake_response = MagicMock(
            status_code=401, content=b'{"error":"secret-detail-blob"}',
        )
        fake_response.json.return_value = {"error": "secret-detail-blob"}
        fake_session = MagicMock()
        fake_session.get.return_value = fake_response

        with patch.object(conn, "_login_session", return_value=fake_session), \
             patch("backend.brokers.connections.logger") as mock_logger:
            result = conn._try_renew()

        assert result is None
        warn_text = " ".join(
            str(a) for call in mock_logger.warning.call_args_list for a in call.args
        )
        assert "secret-detail-blob" not in warn_text
        assert "keys=" in warn_text
