"""recoverable(): retries, warns while failures are transient, escalates to error on repeats."""
import asyncio

import pytest

from backend.shared.helpers import recovery


class _FakeLogger:
    def __init__(self):
        self.records = []

    def warning(self, msg, **kw):
        self.records.append(("WARNING", msg, kw))

    def error(self, msg, **kw):
        self.records.append(("ERROR", msg, kw))

    def debug(self, msg, **kw):
        self.records.append(("DEBUG", msg, kw))


@pytest.fixture
def fake(monkeypatch):
    logger = _FakeLogger()
    monkeypatch.setattr(recovery, "get_logger", lambda name: logger)
    monkeypatch.setattr(recovery.time, "sleep", lambda s: None)
    recovery._reset_for_tests()
    recovery._reset_dedup_for_tests()
    return logger


def test_success_after_retry_logs_warning_only(fake):
    calls = {"n": 0}

    @recovery.recoverable("kite.orders", attempts=3, backoff_s=0)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("502")
        return "ok"

    assert flaky() == "ok"
    assert [r[0] for r in fake.records] == ["WARNING"]
    assert "attempt 1/3" in fake.records[0][1]


def test_repeated_failed_calls_stay_warning_then_escalate(fake):
    @recovery.recoverable("kite.orders", attempts=2, backoff_s=0)
    def down():
        raise RuntimeError("502")

    for _ in range(recovery.ESCALATE_AFTER):
        with pytest.raises(RuntimeError):
            down()
    levels = [r[0] for r in fake.records if "failed after" in r[1]]
    assert levels == ["WARNING"] * recovery.ESCALATE_AFTER

    with pytest.raises(RuntimeError):
        down()
    last = [r for r in fake.records if "failed after" in r[1]][-1]
    assert last[0] == "ERROR"
    assert last[2] == {"extra": {"alert_now": True}}


def test_escalation_does_not_realert_on_every_subsequent_failure(fake):
    """2026-10-31 live incident: a genuinely down broker (circuit
    breaker's own half-open recovery test still probes every ~60s by
    design) kept crossing ESCALATE_AFTER on every call once past the
    threshold, firing a fresh ERROR_AGENT alert each time — 8+ alerts
    in minutes for the SAME ongoing outage. Only the FIRST escalation
    past threshold should reach ERROR; repeats within the dedup cooldown
    must not log a second one. `already_logged()` must still read True
    on every failure, deduped or not, so a caller's own fallback error
    log stays suppressed throughout."""
    @recovery.recoverable("kite.margins", attempts=1, backoff_s=0)
    def down():
        raise RuntimeError("502")

    for _ in range(recovery.ESCALATE_AFTER):
        with pytest.raises(RuntimeError):
            down()

    # Three more calls past the threshold — in the old behaviour each one
    # would escalate to a fresh ERROR; now only ever one ERROR total.
    excs = []
    for _ in range(3):
        try:
            down()
        except RuntimeError as e:
            excs.append(e)
    errors = [r for r in fake.records if r[0] == "ERROR"]
    assert len(errors) == 1, f"expected exactly one ERROR escalation, got {len(errors)}"
    assert all(recovery.already_logged(e) for e in excs), (
        "already_logged() must stay True even when the ERROR itself was deduped"
    )


def test_exception_is_reraised_unchanged(fake):
    @recovery.recoverable("x", attempts=1, backoff_s=0)
    def bad():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        bad()


def test_non_retryable_exception_passes_through_without_retry(fake):
    calls = {"n": 0}

    @recovery.recoverable("x", attempts=3, backoff_s=0, retry_on=(ConnectionError,))
    def wrong_kind():
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        wrong_kind()
    assert calls["n"] == 1
    assert fake.records == []


def test_async_success_after_retry(fake):
    calls = {"n": 0}

    @recovery.recoverable("kite.orders", attempts=3, backoff_s=0)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("502")
        return 5

    assert asyncio.run(flaky()) == 5
    assert [r[0] for r in fake.records] == ["WARNING", "WARNING"]


def test_async_repeated_failures_escalate(fake):
    @recovery.recoverable("feed", attempts=1, backoff_s=0)
    async def down():
        raise RuntimeError("down")

    for _ in range(recovery.ESCALATE_AFTER + 1):
        with pytest.raises(RuntimeError):
            asyncio.run(down())
    assert fake.records[-1][0] == "ERROR"
