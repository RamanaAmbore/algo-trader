"""Log leaf: tag/level/field matching, validation against the registry, the per-cycle feed, and channel tag filters."""
import json
from types import SimpleNamespace

import pytest

from backend.api.algo import agent_evaluator as ev
from backend.api.algo import events as ev_events
from backend.api.algo import grammar_registry
from backend.api.algo import log_feed
from backend.api.algo.grammar import LOG_TAG_TOKENS, SYSTEM_TOKENS


def _rec(i, tags, level="INFO", **extra):
    return {"id": i, "level": level, "message": f"m{i}", "tags": tags, "extra": extra}


def _ctx(records):
    return ev.Context(log_records=records)


def test_matches_tag_and_min_level():
    recs = [_rec(1, ["info", "orders"]), _rec(2, ["info", "broker"]), _rec(3, ["warning", "orders"], level="WARNING")]
    out = ev._eval_log({"log": {"tag": "orders", "min_level": "INFO"}}, _ctx(recs))
    assert [m["value"] for m in out] == [1, 3]


def test_min_level_excludes_lower_levels():
    recs = [_rec(1, ["orders"], level="INFO")]
    assert ev._eval_log({"log": {"tag": "orders", "min_level": "WARNING"}}, _ctx(recs)) == []


def test_where_matches_exact_extra_fields():
    recs = [_rec(1, ["orders"], mode="live"), _rec(2, ["orders"], mode="paper")]
    out = ev._eval_log({"log": {"tag": "orders", "where": {"mode": "live"}}}, _ctx(recs))
    assert [m["value"] for m in out] == [1]


def test_match_entry_is_json_serialisable_and_carries_tags():
    out = ev._eval_log({"log": {"tag": "orders"}}, _ctx([_rec(7, ["orders"], account="ZG0790")]))
    assert out[0]["account"] == "ZG0790"
    assert out[0]["tags"] == ["orders"]
    json.dumps(out)


def test_leaf_dispatch_routes_log_leaves_to_log_matcher():
    out = ev._eval_leaf({"log": {"tag": "orders"}}, _ctx([_rec(1, ["orders"])]))
    assert len(out) == 1 and out[0]["metric"] == "log"


def test_validate_rejects_unknown_tag(monkeypatch):
    monkeypatch.setattr(grammar_registry.REGISTRY, "log_tags", {"orders": {"table": "log_events"}})
    errs = ev.validate({"log": {"tag": "nope"}})
    assert any("unknown log tag 'nope'" in e for e in errs)


def test_validate_accepts_registered_tag_and_level(monkeypatch):
    monkeypatch.setattr(grammar_registry.REGISTRY, "log_tags", {"orders": {"table": "log_events"}})
    assert ev.validate({"log": {"tag": "orders", "min_level": "WARNING"}}) == []


def test_validate_rejects_unknown_level_and_missing_tag(monkeypatch):
    monkeypatch.setattr(grammar_registry.REGISTRY, "log_tags", {})
    errs = ev.validate({"log": {"min_level": "LOUD"}})
    assert any("missing 'tag'" in e for e in errs)
    assert any("unknown min_level" in e for e in errs)


def test_registry_loads_log_tag_rows_with_source():
    tables = {"log_tags": {}}
    row = SimpleNamespace(grammar_kind="log", token_kind="tag", token="chase", source={"table": "log_events"},
                          resolver=None, params_schema=None, template_body=None, value_type=None)
    assert grammar_registry.GrammarRegistry._load_one_token(row, tables) is True
    assert tables["log_tags"] == {"chase": {"table": "log_events"}}


def test_seed_log_tags_are_grammar_kind_log_with_source():
    assert LOG_TAG_TOKENS
    for spec in LOG_TAG_TOKENS:
        assert spec["grammar_kind"] == "log" and spec["token_kind"] == "tag"
        assert spec["source"]["table"] == "log_events"
        assert spec in SYSTEM_TOKENS


@pytest.mark.asyncio
async def test_feed_starts_at_newest_row_then_returns_only_new_rows():
    log_feed.reset_for_tests()
    calls = []

    async def newest():
        return 10

    async def fetch(after):
        calls.append(after)
        return [{"id": 11, "tags": ["orders"]}, {"id": 12, "tags": ["orders"]}]

    assert await log_feed.records_since_last_cycle(fetch=fetch, newest=newest) == []
    rows = await log_feed.records_since_last_cycle(fetch=fetch, newest=newest)
    assert [r["id"] for r in rows] == [11, 12]
    assert calls == [10]
    await log_feed.records_since_last_cycle(fetch=fetch, newest=newest)
    assert calls == [10, 12]
    log_feed.reset_for_tests()


def _eval_result(tags):
    return ev_events.EvalResult(triggered=True, condition_text="x",
                                detail={"matches": [{"tags": tags}]})


@pytest.mark.asyncio
async def test_channel_with_tags_sends_only_when_a_match_carries_one(monkeypatch):
    sent = []
    monkeypatch.setattr(ev_events, "is_enabled", lambda cap: True)

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(ev_events, "_send_telegram", fake_send)

    def _call(tags):
        return ev_events._dispatch_channel(
            {"channel": "telegram", "tags": ["orders"]}, SimpleNamespace(name="a", slug="a"),
            "body", "subj", "ebody", "cond", "ts", _eval_result(tags), None, False, "main", "")

    await _call(["chase"])
    assert sent == []
    await _call(["orders", "chase"])
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_channel_without_tags_is_unchanged(monkeypatch):
    sent = []
    monkeypatch.setattr(ev_events, "is_enabled", lambda cap: True)

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(ev_events, "_send_telegram", fake_send)
    await ev_events._dispatch_channel(
        {"channel": "telegram"}, SimpleNamespace(name="a", slug="a"),
        "body", "subj", "ebody", "cond", "ts", _eval_result([]), None, False, "main", "")
    assert sent == ["body"]


@pytest.mark.asyncio
async def test_feed_failure_returns_no_rows_and_retries_next_cycle():
    log_feed.reset_for_tests()

    async def newest():
        raise RuntimeError("db down")

    assert await log_feed.records_since_last_cycle(newest=newest) == []
    assert log_feed._high_water is None

    async def ok_newest():
        return 5

    assert await log_feed.records_since_last_cycle(newest=ok_newest) == []
    assert log_feed._high_water == 5
    log_feed.reset_for_tests()


@pytest.mark.asyncio
async def test_log_channel_writes_a_tagged_agent_record(monkeypatch):
    calls = []

    class _Fake:
        def warning(self, msg, **kw):
            calls.append((msg, kw))

    monkeypatch.setattr(ev_events, "logger", _Fake())
    await ev_events._dispatch_channel(
        {"channel": "log"}, SimpleNamespace(name="Loss", slug="loss-funds-negative"),
        "body", "subj", "ebody", "cash < 0", "ts", _eval_result([]), None, False, "main", "")
    assert len(calls) == 1
    msg, kw = calls[0]
    assert "[loss-funds-negative]" in msg
    assert kw["extra"]["tags"] == ["agent"]
    assert kw["extra"]["agent_slug"] == "loss-funds-negative"
    assert kw["extra"]["sim_mode"] is False


@pytest.mark.asyncio
async def test_feed_resumes_from_the_stored_high_water_mark(monkeypatch):
    log_feed.reset_for_tests()
    stored = {}
    monkeypatch.setattr(log_feed, "_load_stored_high_water", lambda: 5)
    monkeypatch.setattr(log_feed, "_store_high_water", lambda v: stored.__setitem__("hw", v))
    calls = []

    async def fetch(after):
        calls.append(after)
        return [{"id": 6, "tags": ["orders"]}, {"id": 7, "tags": ["orders"]}]

    async def newest():
        raise AssertionError("must not read newest when a mark is stored")

    rows = await log_feed.records_since_last_cycle(fetch=fetch, newest=newest)
    assert [r["id"] for r in rows] == [6, 7]
    assert calls == [5]
    assert stored["hw"] == 7
    log_feed.reset_for_tests()


@pytest.mark.asyncio
async def test_feed_without_stored_mark_starts_at_newest(monkeypatch):
    log_feed.reset_for_tests()
    monkeypatch.setattr(log_feed, "_load_stored_high_water", lambda: None)
    monkeypatch.setattr(log_feed, "_store_high_water", lambda v: None)

    async def newest():
        return 9

    async def fetch(after):
        raise AssertionError("first call without a mark must not fetch")

    assert await log_feed.records_since_last_cycle(fetch=fetch, newest=newest) == []
    assert log_feed._high_water == 9
    log_feed.reset_for_tests()
