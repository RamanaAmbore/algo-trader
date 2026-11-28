"""P3-3 / P3-4 fixes (2026-11-27) to `date_time_utils.close_time_for_
exchange()`:

1. Type validation — a `str` `today` (e.g. a conn_service JSON payload
   that never got parsed back into a `date`) is coerced via
   `date.fromisoformat()` with an explicit WARNING naming the real
   problem, instead of silently falling through into
   `resolve_dst_aware_sessions()`'s own, differently-scoped fallback.
   An unparseable string, or any other non-date type, raises
   `TypeError` loudly rather than returning a plausible-but-wrong
   answer.
2. Memoization — a successfully-resolved `(hour, minute)` is cached,
   keyed on `(EXCHANGE, date)`, so a repeated call for the same pair
   doesn't re-walk the YAML config + DST resolver. A `None` (failure)
   result is NEVER cached, so a transient failure stays retryable.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from backend.shared.helpers.date_time_utils import (
    _CLOSE_TIME_CACHE,
    _coerce_close_time_today,
    clear_close_time_cache,
    close_time_for_exchange,
)

_WINTER_DATE = date(2026, 11, 27)  # confirmed winter-window MCX date elsewhere


class TestCoerceCloseTimeToday:
    def test_real_date_passthrough(self):
        assert _coerce_close_time_today("MCX", _WINTER_DATE) is _WINTER_DATE

    def test_datetime_is_a_date_subclass_and_passes_through(self):
        from datetime import datetime
        dt = datetime(2026, 11, 27, 10, 0, 0)
        assert _coerce_close_time_today("MCX", dt) is dt

    def test_iso_string_is_coerced_with_warning(self):
        # `get_logger()` sets `propagate = False` (see ramboq_logger.py),
        # so pytest's `caplog` (which attaches to the root logger) never
        # sees these records — patch the module's own `logger` directly,
        # the established convention elsewhere in this test suite (e.g.
        # `test_broker_apis_coverage.py`).
        from backend.shared.helpers import date_time_utils as dtu

        with patch.object(dtu, "logger") as mock_logger:
            result = _coerce_close_time_today("MCX", "2026-11-27")
            mock_logger.warning.assert_called_once()
        assert result == _WINTER_DATE
        assert isinstance(result, date)

    def test_unparseable_string_raises_type_error(self):
        with pytest.raises(TypeError):
            _coerce_close_time_today("MCX", "not-a-date")

    def test_none_raises_type_error(self):
        with pytest.raises(TypeError):
            _coerce_close_time_today("MCX", None)

    def test_int_raises_type_error(self):
        with pytest.raises(TypeError):
            _coerce_close_time_today("MCX", 20261127)


class TestCloseTimeForExchangeTypeValidation:
    def test_real_date_works_unchanged(self):
        assert close_time_for_exchange("MCX", _WINTER_DATE) == (23, 55)

    def test_str_today_is_coerced_and_still_resolves(self):
        assert close_time_for_exchange("MCX", "2026-11-27") == (23, 55)

    def test_bad_string_raises_type_error_not_a_wrong_answer(self):
        with pytest.raises(TypeError):
            close_time_for_exchange("MCX", "garbage")

    def test_none_today_raises_type_error(self):
        with pytest.raises(TypeError):
            close_time_for_exchange("MCX", None)

    def test_unrecognised_exchange_returns_none_before_type_check(self):
        """An unrecognised exchange short-circuits to None BEFORE the
        type validation even runs — a bad `today` for a bogus exchange
        must not raise; there's no real resolution to protect."""
        assert close_time_for_exchange("NOT-A-REAL-EXCHANGE", "garbage") is None


class TestCloseTimeForExchangeMemoization:
    def setup_method(self):
        clear_close_time_cache()

    def teardown_method(self):
        clear_close_time_cache()

    def test_second_call_for_same_pair_is_served_from_cache(self):
        """The DST resolver is called once for the first lookup of a
        given (exchange, date) pair; a second lookup for the EXACT same
        pair must not call it again."""
        from backend.shared.helpers import date_time_utils as dtu

        calls = []
        real = dtu.resolve_dst_aware_sessions

        def _spy(seg_cfg, today):
            calls.append((seg_cfg.get("holiday_exchange"), today))
            return real(seg_cfg, today)

        with patch.object(dtu, "resolve_dst_aware_sessions", side_effect=_spy):
            first = close_time_for_exchange("MCX", _WINTER_DATE)
            second = close_time_for_exchange("MCX", _WINTER_DATE)

        assert first == second == (23, 55)
        assert len(calls) == 1, "second call must be served from the cache, not re-resolved"

    def test_different_date_is_a_cache_miss(self):
        """The cache key includes the date — a DIFFERENT date for the
        same exchange must not reuse yesterday's cached value (this is
        the literal cross-midnight/DST-transition invalidation the task
        calls out)."""
        from backend.shared.helpers import date_time_utils as dtu

        calls = []
        real = dtu.resolve_dst_aware_sessions

        def _spy(seg_cfg, today):
            calls.append(today)
            return real(seg_cfg, today)

        summer_date = date(2026, 6, 15)  # confirmed in-DST elsewhere
        with patch.object(dtu, "resolve_dst_aware_sessions", side_effect=_spy):
            close_time_for_exchange("MCX", _WINTER_DATE)
            close_time_for_exchange("MCX", summer_date)

        assert len(calls) == 2
        assert calls[0] != calls[1]

    def test_failure_is_never_cached_and_stays_retryable(self):
        """A `None` result (e.g. a transient config-load failure) must
        never be cached — the very next call must retry the real
        resolution instead of being poisoned for the rest of the day."""
        from backend.shared.helpers import date_time_utils as dtu

        call_count = {"n": 0}

        def _fail_once_then_succeed(seg_cfg, today):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")
            return [{"start": "09:00", "end": "23:30"}]

        # The failure path inside close_time_for_exchange is the
        # `app_config.get(...)` try/except, not resolve_dst_aware_
        # sessions directly — simulate it by making the segment config
        # loader itself raise on the first call.
        from backend.shared.helpers import utils as app_utils

        real_config_get = app_utils.config.get
        state = {"n": 0}

        class _FlakyDict(dict):
            def get(self, key, default=None):
                if key == "market_segments":
                    state["n"] += 1
                    if state["n"] == 1:
                        raise RuntimeError("transient config load failure")
                return real_config_get(key, default)

        with patch.object(app_utils, "config", _FlakyDict(app_utils.config)):
            first = close_time_for_exchange("MCX", _WINTER_DATE)
            second = close_time_for_exchange("MCX", _WINTER_DATE)

        assert first is None, "the first (failing) call must return None, not raise"
        assert second == (23, 55), "the second call must retry, not reuse a cached None"

    def test_cache_bounded_size_clears_instead_of_growing_unbounded(self):
        from backend.shared.helpers import date_time_utils as dtu

        clear_close_time_cache()
        base = date(2026, 1, 1)
        for i in range(dtu._CLOSE_TIME_CACHE_MAX + 5):
            close_time_for_exchange("MCX", base + timedelta(days=i))
        assert len(_CLOSE_TIME_CACHE) <= dtu._CLOSE_TIME_CACHE_MAX
