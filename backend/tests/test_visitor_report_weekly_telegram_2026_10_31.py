"""2026-10-31 — operator: "the visitor alert should happen once in a
week on weekend. please update it".

`_task_visitor_log_daily()` (`backend/api/background.py`) wrote the
visitor markdown report and upserted `visitor_log` every single
calendar day, then unconditionally sent a Telegram alert on the SAME
daily cadence. The daily report/upsert is unaffected (ongoing audit
data, not alert noise — see the function's own pre-existing comment on
why the markdown stays on disk daily). Only the Telegram SEND is now
gated to once a week, via `_visitor_report_send_telegram_today()`, a
small pure function extracted specifically so this schedule decision is
unit-testable without driving the surrounding `while True`/`sleep` loop.

Sunday (`weekday() == 6`, Python's Monday=0 convention) was chosen as
the single firing day — the end of the traffic week.
"""
from __future__ import annotations

import pytest

from backend.api.background import _visitor_report_send_telegram_today


@pytest.mark.parametrize("weekday,expected", [
    (0, False),  # Monday
    (1, False),  # Tuesday
    (2, False),  # Wednesday
    (3, False),  # Thursday
    (4, False),  # Friday
    (5, False),  # Saturday
    (6, True),   # Sunday
])
def test_fires_exactly_once_per_week_on_sunday(weekday, expected):
    assert _visitor_report_send_telegram_today(weekday) is expected


def test_exactly_one_day_of_the_week_fires():
    """A stronger, order-independent guard against the "once a week"
    requirement regressing to "every weekend day" or "never" — exactly
    one of the 7 weekdays must return True."""
    fires = [d for d in range(7) if _visitor_report_send_telegram_today(d)]
    assert fires == [6]
