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


def test_cutoff_is_close_minus_default_lead_nfo(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    now = datetime(2026, 10, 15, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.cutoff_for("NFO", now).strftime("%H:%M") == "15:15"


def test_cutoff_uses_mcx_lead_default(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    now = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.cutoff_for("MCX", now).strftime("%H:%M") == "23:00"


def test_before_cutoff_true_until_cutoff(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    early = datetime(2026, 10, 15, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    late = datetime(2026, 10, 15, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.before_cutoff("NFO", early) is True
    assert gate.before_cutoff("NFO", late) is False


def test_template_exit_held_by_default(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: default)
    assert gate.template_exit_held() is True
