"""
Tests for backend/scripts/visitor_report.py

Covers:
  - _parse_lines: fixture access.log → per-IP record counts + path/UA
  - Static-asset lines are filtered out
  - Lines outside target_date are excluded
  - _render_report: correct section headings, row count cap, country/path aggregation
  - _ua_short: recognises Chrome/Firefox/Edge/curl
  - DB upsert + purge (skipped if no async DB available; tests the parse+render path)
"""

import asyncio
from collections import defaultdict
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixture log lines
# ---------------------------------------------------------------------------

# fmt: off
FIXTURE_LINES = [
    # Normal page hit — should be counted
    '49.207.222.16 IN [02/Jun/2026:09:14:32 +0000] "GET /pulse HTTP/1.1" 200 12345 "https://www.google.com/" "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/137.0.0.0 Safari/537.36"',
    # Same IP, different path, same day
    '49.207.222.16 IN [02/Jun/2026:09:25:10 +0000] "GET /dashboard HTTP/1.1" 200 5432 "-" "Mozilla/5.0 Chrome/137"',
    # Different IP, same day
    '1.2.3.4 US [02/Jun/2026:11:00:00 +0000] "GET / HTTP/1.1" 200 1000 "-" "curl/7.88.1"',
    # Static asset — should be filtered
    '5.6.7.8 DE [02/Jun/2026:10:00:00 +0000] "GET /assets/app.css HTTP/1.1" 200 800 "-" "Chrome"',
    # JS bundle — should be filtered
    '5.6.7.8 DE [02/Jun/2026:10:01:00 +0000] "GET /_app/immutable/entry/start.js HTTP/1.1" 200 400 "-" "Chrome"',
    # Different date — should be excluded
    '9.9.9.9 SG [01/Jun/2026:23:59:59 +0000] "GET /orders HTTP/1.1" 200 3000 "-" "Firefox/113"',
    # Admin path — should be counted
    '200.100.50.25 BR [02/Jun/2026:15:30:00 +0000] "GET /admin/settings HTTP/1.1" 200 2000 "-" "Mozilla/5.0 Firefox/113.0"',
    # CDN probe — should be filtered
    '10.0.0.1 - [02/Jun/2026:12:00:00 +0000] "GET /cdn-cgi/rum HTTP/1.1" 200 0 "-" "Cloudflare"',
    # Malformed line — should be silently skipped
    'NOT A VALID LOG LINE',
]
# fmt: on

TARGET_DATE = date(2026, 6, 2)


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------

from backend.scripts.visitor_report import (
    _parse_lines,
    _render_report,
    _summary_block,
    _ua_short,
)
from backend.api.routes.visitors import _mask_ip


def test_parse_counts_unique_ips():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    # Expect: 49.207.222.16, 1.2.3.4, 200.100.50.25
    # Filtered out: 5.6.7.8 (static), 9.9.9.9 (wrong date), 10.0.0.1 (cdn-cgi)
    assert set(records.keys()) == {"49.207.222.16", "1.2.3.4", "200.100.50.25"}


def test_parse_request_count():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    # IP 49.207.222.16 has 2 page hits
    assert records["49.207.222.16"].count == 2


def test_parse_last_path_is_most_recent():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    # The second hit at 09:25:10 is /dashboard
    assert records["49.207.222.16"].last_path == "/dashboard"


def test_parse_cf_country_captured():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    assert records["1.2.3.4"].cf_country == "US"


def test_parse_wrong_date_excluded():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    assert "9.9.9.9" not in records


def test_parse_static_assets_filtered():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    assert "5.6.7.8" not in records


def test_parse_cdn_cgi_filtered():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    assert "10.0.0.1" not in records


def test_parse_malformed_line_skipped():
    # Should not raise
    records = _parse_lines(["NOT A VALID LOG LINE"], TARGET_DATE)
    assert records == {}


def test_parse_empty_log():
    records = _parse_lines([], TARGET_DATE)
    assert records == {}


# ---------------------------------------------------------------------------
# UA short tests
# ---------------------------------------------------------------------------

def test_ua_short_chrome():
    ua = "Mozilla/5.0 AppleWebKit/537.36 Chrome/137.0.0.0 Safari/537.36"
    assert _ua_short(ua) == "Chrome 137"


def test_ua_short_firefox():
    ua = "Mozilla/5.0 Firefox/113.0"
    assert _ua_short(ua) == "Firefox 113"


def test_ua_short_curl():
    assert _ua_short("curl/7.88.1") == "curl"


def test_ua_short_none():
    assert _ua_short(None) == "-"


# ---------------------------------------------------------------------------
# IP masking tests
# ---------------------------------------------------------------------------

def test_mask_ip_v4():
    assert _mask_ip("49.207.222.16") == "49.207.x.###"


def test_mask_ip_v4_preserves_first_two_octets():
    masked = _mask_ip("192.168.1.1")
    assert masked.startswith("192.168.")


def test_mask_ip_ipv6():
    masked = _mask_ip("2001:db8:85a3::8a2e:370:7334")
    # Should have #### in it
    assert "####" in masked


# ---------------------------------------------------------------------------
# Render tests
# ---------------------------------------------------------------------------

def test_render_report_headings():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    geo_map: dict = {ip: {} for ip in records}
    report = _render_report(TARGET_DATE, records, geo_map)
    assert "# Visitors — 2026-06-02 UTC" in report
    assert "## Summary" in report
    assert "## Detail" in report
    assert "| IP |" in report


def test_render_report_unique_ips_count():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    geo_map = {ip: {} for ip in records}
    report = _render_report(TARGET_DATE, records, geo_map)
    assert "**Unique IPs**: 3" in report


def test_render_report_total_requests():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    geo_map = {ip: {} for ip in records}
    report = _render_report(TARGET_DATE, records, geo_map)
    # 49.207.222.16 → 2, 1.2.3.4 → 1, 200.100.50.25 → 1 = 4
    assert "**Total requests**: 4" in report


def test_render_report_row_cap():
    """More than 200 IPs should produce an 'additional N IPs' line."""
    from backend.scripts.visitor_report import _IPRecord
    from datetime import datetime, timezone

    many_records = {}
    for i in range(250):
        ip = f"10.0.{i // 256}.{i % 256}"
        dt = datetime(2026, 6, 2, 9, 0, 0, tzinfo=timezone.utc)
        many_records[ip] = _IPRecord(ip, "IN", dt, "/pulse", "Chrome/137")

    geo_map = {ip: {} for ip in many_records}
    report = _render_report(TARGET_DATE, many_records, geo_map)
    assert "additional 50 IPs" in report


def test_render_top_paths_included():
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    geo_map = {ip: {} for ip in records}
    report = _render_report(TARGET_DATE, records, geo_map)
    # /dashboard appeared in the fixture (two hits on 49.207.222.16)
    assert "/dashboard" in report


# ---------------------------------------------------------------------------
# Summary block extractor
# ---------------------------------------------------------------------------

def test_summary_block_returns_header_section(tmp_path):
    records = _parse_lines(FIXTURE_LINES, TARGET_DATE)
    geo_map = {ip: {} for ip in records}
    report_content = _render_report(TARGET_DATE, records, geo_map)
    report_path = tmp_path / "visitors_2026-06-02.md"
    report_path.write_text(report_content, encoding="utf-8")

    summary = _summary_block(report_path)
    assert "# Visitors" in summary
    assert "## Summary" in summary
    # Detail section should NOT be in the summary block
    assert "## Detail" not in summary


# ---------------------------------------------------------------------------
# DB upsert smoke test (skipped if async DB unavailable)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_records_skips_on_empty():
    """_upsert_records with an empty records dict should not raise."""
    # We patch async_session to never be called (empty input → early return)
    from backend.scripts.visitor_report import _upsert_records
    # Should return immediately without touching DB
    await _upsert_records({}, TARGET_DATE, {})


@pytest.mark.asyncio
async def test_purge_old_rows_handles_db_error():
    """_purge_old_rows should log and return 0 on DB failure, not raise."""
    from backend.scripts.visitor_report import _purge_old_rows
    with patch("backend.api.database.async_session") as mock_session:
        mock_session.side_effect = Exception("DB unavailable")
        result = await _purge_old_rows(TARGET_DATE, retention_days=30)
    assert result == 0
