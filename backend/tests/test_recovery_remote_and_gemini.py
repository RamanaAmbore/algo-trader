"""Retry-then-escalate across the conn_service boundary and the Gemini call."""
from unittest.mock import MagicMock

import pytest

from backend.brokers.client import remote_broker as rb
from backend.brokers.errors import BrokerError
from backend.shared.helpers import genai_api, recovery


def _fake_client(payload):
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = payload
    client = MagicMock()
    client.post.return_value = resp
    return client


def test_remote_error_carries_already_logged_flag(monkeypatch):
    monkeypatch.setattr(rb, "_get_client", lambda: _fake_client(
        {"ok": False, "error": "502 Bad Gateway", "error_type": "DataException", "logged": True}))
    broker = rb.RemoteBroker("ZJ6294")
    with pytest.raises(BrokerError) as exc:
        broker.orders()
    assert recovery.already_logged(exc.value) is True


def test_remote_error_without_flag_is_not_marked(monkeypatch):
    monkeypatch.setattr(rb, "_get_client", lambda: _fake_client(
        {"ok": False, "error": "bad token", "error_type": "BrokerAuthError", "logged": False}))
    broker = rb.RemoteBroker("ZJ6294")
    with pytest.raises(Exception) as exc:
        broker.orders()
    assert recovery.already_logged(exc.value) is False


def test_gemini_call_retries_server_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(recovery, "time", MagicMock(sleep=lambda s: None))
    monkeypatch.setattr(genai_api, "_generate_content_retry_on", lambda: (ValueError,))
    calls = {"n": 0}

    class _Models:
        def generate_content(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("503 high demand")
            return "ok"

    client = MagicMock()
    client.models = _Models()
    assert genai_api._generate_content(client, model="m") == "ok"
    assert calls["n"] == 2
