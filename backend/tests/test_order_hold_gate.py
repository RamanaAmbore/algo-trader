"""Expiry-close gate: held by default, released only by the global setting."""
import asyncio

from backend.api.algo import order_hold_gate as gate


def test_expiry_close_held_by_default(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: default)
    assert gate.expiry_close_held() is True


def test_expiry_close_released_when_setting_on(monkeypatch):
    monkeypatch.setattr(gate, "get_bool",
                        lambda key, default=False: True if key == gate.EXPIRY_CLOSE_RELEASED_KEY else default)
    assert gate.expiry_close_held() is False


def test_record_held_close_returns_none_on_db_failure(monkeypatch):
    import backend.api.database as db

    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(db, "async_session", _Boom())
    out = asyncio.run(gate.record_held_close(
        account="ZG0790", symbol="CRUDEOIL26OCT8600CE", exchange="MCX",
        side="SELL", qty=200, product="NRML", reason="test"))
    assert out is None
