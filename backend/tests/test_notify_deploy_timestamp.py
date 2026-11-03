"""webhook/notify_deploy.py gained a dual-tz timestamp line (2026-11-03) —
deploy alerts previously carried no timestamp at all, unlike every other
alert in the system (which gets one via dispatch_payload/
format_notification_header). The script deliberately avoids importing
backend modules (see its own docstring), so this pins its inlined
zoneinfo-based reproduction against the real shared format.
"""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_notify_deploy():
    path = REPO_ROOT / "webhook" / "notify_deploy.py"
    spec = importlib.util.spec_from_file_location("notify_deploy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["notify_deploy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def notify_deploy():
    return _load_notify_deploy()


def test_dual_tz_timestamp_matches_shared_format(notify_deploy, monkeypatch):
    from backend.shared.helpers.date_time_utils import format_dual_tz

    fixed = datetime(2026, 11, 3, 12, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        notify_deploy, "datetime",
        type("_FixedDatetime", (), {"now": staticmethod(lambda tz=None: fixed.astimezone(tz))}),
    )
    got = notify_deploy._dual_tz_timestamp()
    expected = format_dual_tz(fixed)
    assert got == expected
    assert " IST | " in got
    assert got.count("\n") == 0


def test_dual_tz_timestamp_shape():
    # Direct shape check independent of the shared helper, matching the
    # literal format string in _dual_tz_timestamp().
    ist = datetime.now(tz=ZoneInfo("Asia/Kolkata"))
    est = ist.astimezone(ZoneInfo("US/Eastern"))
    expected_pattern = f"{ist.strftime('%a %d %b %H:%M')} IST | {est.strftime('%a %d %b %H:%M')}"
    module = _load_notify_deploy()
    got = module._dual_tz_timestamp()
    assert got.startswith(expected_pattern[: len("Mon 01 Jan 00:00")])
    assert "IST" in got and ("EST" in got or "EDT" in got)
