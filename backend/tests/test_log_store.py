"""log_store: owner tags, row shape, bounded queue, batch flush, failure path, retention."""
import logging
from datetime import datetime, timezone

import pytest

from backend.shared.helpers import log_store


def _rec(name="backend.api.routes.orders", level=logging.INFO, msg="order filled", **extra):
    rec = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


class _Session:
    def __init__(self, store):
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self._store.append(("execute", stmt, params))

    async def commit(self):
        self._store.append(("commit",))


def _factory(store):
    return lambda: _Session(store)


def test_owner_tags_are_kept_with_level_first():
    assert log_store.tags_for(_rec(tags=["orders", "chase"])) == ["info", "orders", "chase"]


def test_untagged_record_falls_back_to_logger_name():
    assert log_store.tags_for(_rec(name="backend.brokers.adapters.kite")) == ["info", "kite"]


def test_tags_are_deduplicated_and_capped():
    tags = ["orders", "orders"] + [f"t{i}" for i in range(40)]
    out = log_store.tags_for(_rec(tags=tags))
    assert out.count("orders") == 1
    assert len(out) == log_store._MAX_TAGS


def test_row_has_plain_message_and_json_safe_extra():
    rec = _rec(msg="fill — done", tags=["orders"], order_id=101, when=datetime(2026, 10, 6))
    row = log_store.row_for(rec)
    assert row["message"] == "fill - done"
    assert row["level"] == "INFO"
    assert row["logger"] == "backend.api.routes.orders"
    assert row["tags"] == ["info", "orders"]
    assert row["extra"]["order_id"] == 101
    assert isinstance(row["extra"]["when"], str)
    assert "tags" not in row["extra"]
    assert row["ts"].tzinfo is timezone.utc or row["ts"].utcoffset().total_seconds() == 0


def test_row_without_extra_stores_null_extra():
    assert log_store.row_for(_rec())["extra"] is None


def test_handler_stores_info_and_above_only():
    h = log_store.LogStoreHandler()
    h.emit(_rec(level=logging.DEBUG, msg="noisy"))
    h.emit(_rec(level=logging.INFO, msg="kept"))
    rows = h.drain(10)
    assert [r["message"] for r in rows] == ["kept"]


@pytest.mark.parametrize("name", [
    "backend.shared.helpers.log_store",
    "backend.shared.helpers.alert_utils",
    "backend.shared.helpers.error_alerts",
    "backend.api.algo.event_agents",
    "sqlalchemy.engine.Engine",
])
def test_skipped_loggers_never_reach_the_queue(name):
    h = log_store.LogStoreHandler()
    h.emit(_rec(name=name))
    assert h.drain(10) == []


def test_full_queue_drops_and_never_raises():
    h = log_store.LogStoreHandler()
    for _ in range(log_store._QUEUE_MAX):
        h.emit(_rec())
    h.emit(_rec(msg="one too many"))
    assert h.dropped == 1
    assert len(h.drain(log_store._QUEUE_MAX + 5)) == log_store._QUEUE_MAX


def test_drain_respects_limit():
    h = log_store.LogStoreHandler()
    for i in range(5):
        h.emit(_rec(msg=str(i)))
    assert len(h.drain(3)) == 3
    assert len(h.drain(10)) == 2


@pytest.mark.asyncio
async def test_flush_writes_one_batch_with_tags():
    h = log_store.LogStoreHandler()
    h.emit(_rec(tags=["orders"], msg="a"))
    h.emit(_rec(tags=["broker"], msg="b"))
    store = []
    n = await log_store.flush_once(h, _factory(store))
    assert n == 2
    execs = [e for e in store if e[0] == "execute"]
    assert len(execs) == 1
    rows = execs[0][2]
    assert [r["message"] for r in rows] == ["a", "b"]
    assert rows[0]["tags"] == ["info", "orders"]
    assert ("commit",) in store


@pytest.mark.asyncio
async def test_flush_with_nothing_queued_writes_nothing():
    store = []
    assert await log_store.flush_once(log_store.LogStoreHandler(), _factory(store)) == 0
    assert store == []


@pytest.mark.asyncio
async def test_failed_insert_counts_drops_and_writes_stderr(capsys):
    h = log_store.LogStoreHandler()
    h.emit(_rec(msg="lost"))

    def broken():
        raise RuntimeError("db down")

    n = await log_store.flush_once(h, broken)
    assert n == 0
    assert h.dropped == 1
    assert "log_store: insert of 1 rows failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_prune_runs_a_delete_and_commits():
    store = []
    await log_store.prune(7, _factory(store))
    kinds = [e[0] for e in store]
    assert kinds == ["execute", "commit"]
    assert "DELETE" in str(store[0][1]).upper()


def test_retention_and_batch_settings_have_defaults():
    from backend.shared.helpers import settings
    assert settings.get_int("log.retention_days", 7) == 7
    assert settings.get_int("log.db_min_level", logging.INFO) == logging.INFO


def test_unknown_owner_tag_is_reported_once(monkeypatch, capsys):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, "log_tags", {"orders": {}})
    monkeypatch.setattr(log_store, "_warned_tags", set())
    log_store.tags_for(_rec(tags=["mystery"]))
    log_store.tags_for(_rec(tags=["mystery"]))
    assert capsys.readouterr().err.count("tag 'mystery' is not in the tag catalog") == 1


def test_known_owner_tag_is_not_reported(monkeypatch, capsys):
    from backend.api.algo.grammar_registry import REGISTRY
    monkeypatch.setattr(REGISTRY, "log_tags", {"orders": {}})
    monkeypatch.setattr(log_store, "_warned_tags", set())
    log_store.tags_for(_rec(tags=["orders"]))
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_emit_wakes_the_writer_on_the_loop():
    import asyncio
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    monkeypatch_loop, monkeypatch_wake = log_store._loop, log_store._wake
    log_store._loop, log_store._wake = loop, wake
    try:
        h = log_store.LogStoreHandler()
        h.emit(_rec(tags=["orders"]))
        await asyncio.sleep(0.01)
        assert wake.is_set()
    finally:
        log_store._loop, log_store._wake = monkeypatch_loop, monkeypatch_wake


def test_origin_is_stamped_from_the_request_context(monkeypatch):
    from backend.shared.helpers.log_store import ORIGIN_BRANCH, OriginFilter
    rec = _rec()
    token = ORIGIN_BRANCH.set("dev")
    try:
        OriginFilter().filter(rec)
    finally:
        ORIGIN_BRANCH.reset(token)
    assert rec.origin == "dev"
    assert log_store.row_for(rec)["extra"]["origin"] == "dev"


def test_no_origin_outside_a_request():
    from backend.shared.helpers.log_store import OriginFilter
    rec = _rec()
    OriginFilter().filter(rec)
    assert not hasattr(rec, "origin")
