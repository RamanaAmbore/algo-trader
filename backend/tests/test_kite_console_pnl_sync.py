"""Tests for `scripts/kite_console_pnl_sync.py` — the Playwright-driven
Zerodha Kite Console Tradebook/P&L scraper + ingester.

COVERAGE NOTE (per this module's own standing test-authoring convention):
these tests cover every pure/unit-testable surface of the script — CSV
parsing and validation, idempotent-upsert row/key construction, the
trigger-date logic (should_sync_today / compute_trigger_dates), the
real `KiteConnection` login-cooloff state machine as driven through this
script's own subclass, the account-masking convention, and the
scraping-robustness contract (verify-every-step, never retry a broken
selector, screenshot+alert on any structural failure) via a fully mocked
`KiteConsoleReportsPage`. What CANNOT be covered here, and is explicitly
NOT claimed to be: Kite Console's real, live DOM structure — every
selector inside `KiteConsoleReportsPage` is a documented placeholder (see
that class's own docstring) and must be verified against a real
authenticated session before the first real run. No test in this file
asserts anything about the real Kite Console page; they all assert this
script's OWN control flow given a mocked page.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
import types
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import kite_console_pnl_sync as mod


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_login_cooloff_paths(tmp_path, monkeypatch):
    """Redirect the shared, host-wide Kite login-cooloff file, the
    token-cache file, AND this script's own (separate) Console cooloff
    file to per-test tmp_path locations — mirrors
    `backend/tests/broker/conftest.py`'s own autouse fixture, reproduced
    here because this test module lives in `backend/tests/` directly,
    not `backend/tests/broker/`, so that conftest's fixture does not
    apply automatically.

    The live path and the Console path are redirected to TWO DIFFERENT
    filenames, never the same one — reusing one tmp file for both would
    silently hide the exact cross-contamination bug the independence
    tests below exist to catch."""
    import backend.brokers.connections as connections

    monkeypatch.setattr(
        connections, "_KITE_LOGIN_COOLOFF_PATH",
        str(tmp_path / "kite_login_cooloff.json"),
    )
    monkeypatch.setattr(
        connections, "_TOKEN_CACHE_PATH",
        tmp_path / "kite_tokens.json",
    )
    monkeypatch.setattr(
        mod, "_CONSOLE_LOGIN_COOLOFF_PATH",
        str(tmp_path / "kite_console_login_cooloff.json"),
    )


def _fake_broker_account_row(account: str = "ZCONSOLE01") -> types.SimpleNamespace:
    """Minimal `BrokerAccount`-shaped object. Credential fields are empty
    strings — `decrypt("")` returns `""` without needing a real Fernet
    key (see `broker_creds.decrypt`'s own docstring: "Empty input passes
    through"), so these tests never depend on `cookie_secret`/secrets.yaml
    being configured."""
    return types.SimpleNamespace(
        account=account,
        broker_id="zerodha_kite",
        api_key="test_api_key",
        api_secret_enc="",
        password_enc="",
        totp_token_enc="",
        source_ip=None,
        is_active=True,
    )


def _console_conn_bare(account: str = "ZCONSOLE_BARE") -> "mod.KiteConsoleConnection":
    """Construct the console-login mixin applied onto a bare
    `KiteConnection`-shaped object via `__new__`, skipping `__init__`'s
    disk I/O and SDK calls — mirrors `test_kite_login_cooloff.py`'s own
    `_kite_conn_bare()` helper, extended with the console-mixin's own
    attributes."""
    from backend.brokers.connections import KiteConnection

    class _Conn(mod.KiteConsoleConnection, KiteConnection):
        pass

    obj = _Conn.__new__(_Conn)
    obj.account = account
    obj._password = "pw"
    obj.api_key = "ak"
    obj._api_secret = "as"
    obj.totp_token = "JBSWY3DPEHPK3PXP"
    obj._source_ip = None
    obj.login_url = "http://example.invalid/login"
    obj.twofa_url = "http://example.invalid/twofa"
    obj._access_token = None
    obj._initialized = True
    obj._login_lock = threading.Lock()
    obj._conn_created_at = None
    obj._login_blocked_until = 0.0
    obj._login_fail_streak = 0
    obj._blocked_alert_sent = False
    obj.session = MagicMock()
    obj.kite = MagicMock()
    obj._console_page = None
    obj._console_context = None
    obj._console_browser = None
    obj._console_playwright = None
    return obj


def _csv_bytes(rows: "list[dict]", header: "list[str] | None" = None) -> bytes:
    header = header or [
        "Symbol", "ISIN", "Trade Date", "Exchange", "Segment", "Series",
        "Trade Type", "Auction", "Quantity", "Price", "Trade ID", "Order ID",
        "Order Execution Time",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(mod._normalize_header(h), "") for h in header})
    return buf.getvalue().encode("utf-8")


_LEDGER_HEADER = "particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"


def _ledger_csv_bytes(rows: "list[dict] | None" = None) -> bytes:
    """Minimal real-shaped ledger CSV -- header-only (zero data rows) by
    default, matching the real export's own column order. Used by tests
    whose scenario is about the tradebook/pnl path, not the ledger pull
    itself -- the ledger pull still runs once per sync_account() call
    (see _console_login_and_scrape_sync's own ledger loop), so it needs
    SOME well-formed response to stay out of those tests' way."""
    lines = [_LEDGER_HEADER]
    for r in (rows or []):
        lines.append(
            f"{r.get('particulars', '')},{r.get('posting_date', '')},"
            f"{r.get('cost_center', '')},{r.get('voucher_type', '')},"
            f"{r.get('debit', 0)},{r.get('credit', 0)},{r.get('net_balance', 0)}\n"
        )
    return "".join(lines).encode("utf-8")


def _one_tradebook_row(**overrides) -> dict:
    row = {
        "symbol": "NIFTY25DEC25000CE",
        "trade_date": "2026-11-20",
        "exchange": "NFO",
        "segment": "FO",
        "trade_type": "BUY",
        "quantity": "50",
        "price": "123.45",
        "trade_id": "T1001",
        "order_id": "O2001",
    }
    row.update(overrides)
    return row


def _pnl_xlsx_bytes(rows: "list[dict]") -> bytes:
    """Builds a minimal, REAL XLSX matching the live Kite Console P&L
    export's confirmed shape (see `parse_pnl_statement_xlsx`'s own
    docstring) -- a header row carrying both "Symbol" and "Realized
    P&L" (the two cell values `parse_pnl_statement_xlsx` searches for
    to locate the header dynamically), followed by one data row per
    `rows` entry, then a blank row (end-of-table sentinel the real
    parser also relies on). Deliberately skips the real export's
    Summary-block preamble -- the parser doesn't require a header at
    any fixed row number, so a fixture without that preamble is still
    a faithful, minimal real file, not a shortcut around the parser's
    own logic."""
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Symbol", "Realized P&L"])
    for r in rows:
        ws.append([r.get("symbol", ""), float(r.get("realized_pnl", 0) or 0)])
    ws.append([])  # blank row -- end-of-table sentinel
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _one_pnl_row(**overrides) -> dict:
    row = {
        "symbol": "GOLDM26DECFUT",
        "expiry": "2026-12-24",
        "realized_pnl": "12500.50",
    }
    row.update(overrides)
    return row


# ===========================================================================
# 1. CSV parsing & validation
# ===========================================================================

class TestParseTradebookCsv:
    def test_well_formed_file_parses_every_row(self):
        raw = _csv_bytes([_one_tradebook_row(), _one_tradebook_row(trade_id="T1002")])
        rows = mod.parse_tradebook_csv(raw)
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
        assert rows[0]["trade_id"] == "T1001", f"unexpected trade_id: {rows[0]}"
        assert rows[0]["symbol"] == "NIFTY25DEC25000CE"

    def test_header_normalization_handles_spaces_and_casing(self):
        raw = _csv_bytes([_one_tradebook_row()])
        rows = mod.parse_tradebook_csv(raw)
        # Header was "Trade ID" / "Trade Date" / "Trade Type" etc — must
        # resolve to the lowercase/underscore keys this module reads
        # everywhere else.
        for key in ("symbol", "trade_date", "quantity", "price", "trade_id"):
            assert key in rows[0], f"normalized key {key!r} missing from {rows[0]}"

    def test_missing_required_column_raises_validation_error(self):
        header = ["Symbol", "Trade Date", "Quantity", "Price"]  # no Trade ID
        raw = _csv_bytes([_one_tradebook_row()], header=header)
        with pytest.raises(mod.ReportValidationError, match="missing required column"):
            mod.parse_tradebook_csv(raw)

    def test_empty_file_raises_validation_error(self):
        with pytest.raises(mod.ReportValidationError, match="no header row"):
            mod.parse_tradebook_csv(b"")

    def test_truncated_corrupt_file_raises_validation_error(self):
        # Non-UTF-8 byte sequence that can't be decoded at all.
        with pytest.raises(mod.ReportValidationError, match="not valid UTF-8"):
            mod.parse_tradebook_csv(b"\xff\xfe\x00bad")

    def test_header_only_file_parses_to_zero_rows_not_an_error(self):
        raw = _csv_bytes([])
        rows = mod.parse_tradebook_csv(raw)
        assert rows == [], f"expected zero rows for a header-only file, got {rows}"


def _realistic_pnl_xlsx_bytes(symbol_rows: "list[tuple[str, float]]") -> bytes:
    """Mirrors the REAL Kite Console P&L-statement XLSX export, confirmed
    live 2026-11-26 (segment=F&O, view=Realised P&L) -- including the
    Client-ID/title/Summary/Charges preamble BEFORE the per-symbol
    table, and a second "Other Debits and Credits" sheet, so these tests
    genuinely exercise the dynamic header-row search rather than a
    trivial two-row fixture. Column order for the per-symbol table
    matches the real export exactly: Symbol, ISIN, Quantity, Buy Value,
    Sell Value, Realized P&L, Realized P&L Pct., Previous Closing
    Price, Open Quantity, Open Quantity Type, Open Value, Unrealized
    P&L, Unrealized P&L Pct."""
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "F&O"
    for _ in range(5):
        ws.append([])
    ws.append([None, "Client ID", "ZG0790"])
    ws.append([])
    ws.append([None, "P&L Statement for F&O from 2026-04-01 to 2026-04-30"])
    ws.append([])
    ws.append([None, "Summary"])
    ws.append([])
    ws.append([None, "Charges", 1005.2288])
    ws.append([None, "Other Credit & Debit", 0.0])
    ws.append([None, "Realized P&L", sum(v for _, v in symbol_rows)])
    ws.append([None, "Unrealized P&L", 0.0])
    ws.append([])
    ws.append([])
    ws.append([None, "Charges"])
    ws.append([])
    ws.append([None, "Account Head", "Amount"])
    ws.append([None, "Brokerage - Z", 300.0])
    ws.append([])
    ws.append([])
    ws.append([
        None, "Symbol", "ISIN", "Quantity", "Buy Value", "Sell Value",
        "Realized P&L", "Realized P&L Pct.", "Previous Closing Price",
        "Open Quantity", "Open Quantity Type", "Open Value",
        "Unrealized P&L", "Unrealized P&L Pct.",
    ])
    for symbol, realized_pnl in symbol_rows:
        ws.append([
            None, symbol, "", 150.0, 29850.0, 64147.5, realized_pnl,
            114.8995, 0.0, 0.0, "", 0.0, 0.0, 0.0,
        ])
    ws.append([])
    ws.append([None, "Other Debits and Credits for FO from 2026-04-01 to 2026-04-30"])

    ws2 = wb.create_sheet("Other Debits and Credits")
    ws2.append([None, "Particulars", "Posting Date", "Debit", "Credit"])
    ws2.append([None, "Some charge", "2026-04-15", 100.0, 0.0])

    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestParsePnlStatementXlsx:
    """Direct unit tests for `parse_pnl_statement_xlsx()` -- confirmed
    live, 2026-11-26, against a real Kite Console P&L export (segment=
    F&O, view=Realised P&L). See that function's own docstring for the
    full real-workbook-shape writeup this fixture mirrors."""

    def test_well_formed_file_with_realistic_preamble_parses_every_symbol(self):
        raw = _realistic_pnl_xlsx_bytes([
            ("INDIGO26APR4150CE", 34297.5),
            ("INDIGO26APR4250CE", -52365.0),
        ])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert len(rows) == 2, f"expected 2 rows, got {rows}"
        assert rows[0] == {"symbol": "INDIGO26APR4150CE", "realized_pnl": 34297.5}
        assert rows[1] == {"symbol": "INDIGO26APR4250CE", "realized_pnl": -52365.0}

    def test_header_row_located_dynamically_not_at_a_fixed_row_number(self):
        """The real export's own Summary-block preamble varies in length
        per segment (different charge line-items) -- the header row must
        be found by searching for its content, never assumed to sit at a
        hardcoded row index."""
        raw = _realistic_pnl_xlsx_bytes([("GOLDM26DECFUT", 12500.5)])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert rows == [{"symbol": "GOLDM26DECFUT", "realized_pnl": 12500.5}]

    def test_second_sheet_other_debits_and_credits_is_never_read(self):
        """Only sheet 0 (the segment's own per-symbol P&L table) is
        ingested -- the real second sheet is a charges-by-posting-date
        breakdown this script has no use for."""
        raw = _realistic_pnl_xlsx_bytes([("GOLDM26DECFUT", 12500.5)])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert all(r["symbol"] != "Some charge" for r in rows)
        assert len(rows) == 1

    def test_minimal_file_with_no_preamble_also_parses(self):
        """`_pnl_xlsx_bytes()` (used by the higher-level sync_account
        tests) skips the preamble entirely -- confirms the header search
        works just as well when the header row IS the first row."""
        raw = _pnl_xlsx_bytes([{"symbol": "GOLDM26DECFUT", "realized_pnl": "12500.50"}])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert rows == [{"symbol": "GOLDM26DECFUT", "realized_pnl": 12500.5}]

    def test_header_only_file_parses_to_zero_rows_not_an_error(self):
        raw = _pnl_xlsx_bytes([])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert rows == [], f"expected zero rows for a header-only file, got {rows}"

    def test_no_header_row_found_raises_validation_error(self):
        import io as _io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["not", "the", "right", "header"])
        buf = _io.BytesIO()
        wb.save(buf)
        with pytest.raises(mod.ReportValidationError, match="could not locate"):
            mod.parse_pnl_statement_xlsx(buf.getvalue())

    def test_corrupt_file_raises_validation_error(self):
        with pytest.raises(mod.ReportValidationError, match="not a valid XLSX"):
            mod.parse_pnl_statement_xlsx(b"this is not a zip file at all")

    def test_stale_dimension_tag_does_not_truncate_rows(self):
        """Regression guard for a real bug found 2026-11-26 while
        testing against the genuine downloaded export (not a self-built
        fixture): Kite Console's own generated XLSX carries a stale
        `<dimension ref="A1">` tag that declares only cell A1, despite
        having real data through row 43+. `openpyxl`'s `read_only=True`
        mode trusts that hint to decide how much of the sheet to
        stream, so with read_only=True every row past A1 was silently
        dropped -- confirmed to fail against the real file even though
        every self-built (and therefore correctly-dimensioned) fixture
        in this test class passed. This test reproduces that exact
        defect class by rewriting a normal fixture's own dimension tag
        to the same wrong "A1" value, without embedding any real
        account data."""
        import io as _io
        import re as _re
        import zipfile as _zipfile

        raw = _realistic_pnl_xlsx_bytes([("INDIGO26APR4150CE", 34297.5)])
        zin = _zipfile.ZipFile(_io.BytesIO(raw))
        sheet_path = next(n for n in zin.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheet_xml = zin.read(sheet_path).decode("utf-8")
        assert "<dimension" in sheet_xml, "fixture sanity check: must have a real dimension tag to corrupt"
        corrupted_xml = _re.sub(r'<dimension ref="[^"]*"', '<dimension ref="A1"', sheet_xml)
        assert 'ref="A1"' in corrupted_xml and corrupted_xml != sheet_xml

        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zout:
            for name in zin.namelist():
                data = corrupted_xml.encode("utf-8") if name == sheet_path else zin.read(name)
                zout.writestr(name, data)
        corrupted_raw = buf.getvalue()

        rows = mod.parse_pnl_statement_xlsx(corrupted_raw)
        assert rows == [{"symbol": "INDIGO26APR4150CE", "realized_pnl": 34297.5}], (
            "a stale dimension tag must never truncate the real rows -- "
            f"got {rows}"
        )

    def test_blank_symbol_cell_ends_the_table_even_with_more_rows_below(self):
        """Mirrors the real export's own structure: a blank row marks the
        end of the per-symbol table, with unrelated sections (Other
        Debits and Credits' own title row) following it on the SAME
        sheet -- those trailing rows must never be mistaken for more
        symbol rows."""
        raw = _realistic_pnl_xlsx_bytes([("INDIGO26APR4150CE", 34297.5)])
        rows = mod.parse_pnl_statement_xlsx(raw)
        assert len(rows) == 1
        assert all("Other Debits" not in str(r.get("symbol", "")) for r in rows)


class TestValidateTradebookRows:
    def test_well_formed_rows_pass(self):
        rows = mod.parse_tradebook_csv(_csv_bytes([_one_tradebook_row()]))
        mod.validate_tradebook_rows(
            rows, segment="derivatives", start=date(2026, 11, 1), end=date(2026, 11, 30),
        )  # must not raise

    def test_empty_report_is_not_an_error_by_default(self):
        mod.validate_tradebook_rows(
            [], segment="derivatives", start=date(2026, 11, 1), end=date(2026, 11, 30),
        )  # must not raise — a genuine no-trade day/segment

    def test_empty_report_raises_when_allow_empty_is_false(self):
        with pytest.raises(mod.ReportValidationError, match="zero rows"):
            mod.validate_tradebook_rows(
                [], segment="derivatives", start=date(2026, 11, 1),
                end=date(2026, 11, 30), allow_empty=False,
            )

    def test_row_date_outside_requested_range_raises(self):
        rows = mod.parse_tradebook_csv(
            _csv_bytes([_one_tradebook_row(trade_date="2025-01-01")])
        )
        with pytest.raises(mod.ReportValidationError, match="outside the requested range"):
            mod.validate_tradebook_rows(
                rows, segment="derivatives", start=date(2026, 11, 1), end=date(2026, 11, 30),
            )

    def test_unparsable_row_date_raises(self):
        rows = mod.parse_tradebook_csv(
            _csv_bytes([_one_tradebook_row(trade_date="not-a-date")])
        )
        with pytest.raises(mod.ReportValidationError):
            mod.validate_tradebook_rows(
                rows, segment="derivatives", start=date(2026, 11, 1), end=date(2026, 11, 30),
            )

    def test_numeric_edge_cases_zero_and_large_values_parse(self):
        rows = mod.parse_tradebook_csv(_csv_bytes([
            _one_tradebook_row(trade_id="T_ZERO", quantity="0", price="0"),
            _one_tradebook_row(trade_id="T_BIG", quantity="1000000", price="99999.99"),
        ]))
        mod.validate_tradebook_rows(
            rows, segment="derivatives", start=date(2026, 11, 1), end=date(2026, 11, 30),
        )
        built = [mod.build_sync_row("Z1####", "derivatives", "tradebook", r) for r in rows]
        assert built[0]["buy_qty"] == 0.0, f"zero qty should parse to 0.0, got {built[0]['buy_qty']}"
        assert built[1]["buy_value"] == pytest.approx(1000000 * 99999.99), (
            f"large value math wrong: {built[1]['buy_value']}"
        )


# ===========================================================================
# 2. Dedup key / row building / idempotent ingestion
# ===========================================================================

class TestBuildDedupKey:
    def test_tradebook_key_uses_trade_id(self):
        row = _one_tradebook_row()
        key = mod.build_dedup_key("tradebook", row=row)
        assert key == "NFO:O2001:T1001", f"unexpected key: {key}"

    def test_tradebook_key_degrades_when_trade_id_missing(self):
        row = _one_tradebook_row(trade_id="")
        key = mod.build_dedup_key("tradebook", row=row)
        assert "T1001" not in key
        assert "NIFTY25DEC25000CE" in key, f"degraded key should embed symbol: {key}"

    def test_pnl_key_includes_requested_range_not_just_symbol(self):
        row = {"symbol": "GOLDM26DECFUT", "expiry": date(2026, 12, 24)}
        k1 = mod.build_dedup_key(
            "pnl", row=row, pull_start=date(2026, 3, 1), pull_end=date(2026, 11, 1),
        )
        k2 = mod.build_dedup_key(
            "pnl", row=row, pull_start=date(2026, 3, 1), pull_end=date(2026, 6, 1),
        )
        assert k1 != k2, (
            "a full-range P&L pull and a narrower partial-range pull for "
            "the SAME symbol must get DIFFERENT dedup keys — colliding "
            "would let the narrower pull silently overwrite the wider "
            "aggregate under one row"
        )

    def test_unknown_report_type_raises(self):
        with pytest.raises(ValueError, match="unknown report_type"):
            mod.build_dedup_key("bogus", row={})


class TestBuildSyncRowIdempotency:
    def test_two_overlapping_tradebook_pulls_produce_the_same_dedup_key(self):
        """The real idempotency guarantee: re-ingesting an overlapping
        date-range pull for the same real trade must produce the SAME
        (account, report_type, dedup_key) so the DB-level UPSERT collapses
        them into one row, never a duplicate."""
        row_pull1 = _one_tradebook_row()
        row_pull2 = _one_tradebook_row()  # identical broker-reported row,
        # as if re-downloaded on a later, wider-range pull
        built1 = mod.build_sync_row("Z1####", "derivatives", "tradebook", row_pull1)
        built2 = mod.build_sync_row("Z1####", "derivatives", "tradebook", row_pull2)
        assert built1["dedup_key"] == built2["dedup_key"]
        assert built1["account"] == built2["account"] == "Z1####"

    def test_expiry_is_derived_from_symbol_via_parse_tradingsymbol(self):
        row = _one_tradebook_row(symbol="NIFTY25DEC25000CE")
        built = mod.build_sync_row("Z1####", "derivatives", "tradebook", row)
        assert built["expiry"] is not None, "expected a derived expiry date for an F&O symbol"
        assert isinstance(built["expiry"], date)

    def test_equity_symbol_has_no_expiry(self):
        row = _one_tradebook_row(symbol="RELIANCE", exchange="NSE")
        built = mod.build_sync_row("Z1####", "equity", "tradebook", row)
        assert built["expiry"] is None, f"equity row should have no expiry, got {built['expiry']}"

    def test_raw_row_preserved_verbatim(self):
        row = _one_tradebook_row()
        built = mod.build_sync_row("Z1####", "derivatives", "tradebook", row)
        assert built["raw_row"] == row


class TestIngestRowsSqlShape:
    @pytest.mark.asyncio
    async def test_empty_rows_is_a_no_op(self):
        session = AsyncMock()
        n = await mod.ingest_rows(session, "Z1####", "derivatives", "tradebook", [])
        assert n == 0
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_execute_and_commit_with_an_on_conflict_statement(self):
        session = AsyncMock()
        rows = [_one_tradebook_row()]
        n = await mod.ingest_rows(session, "Z1####", "derivatives", "tradebook", rows)
        assert n == 1
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()
        stmt = session.execute.call_args[0][0]
        from sqlalchemy.dialects import postgresql
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in compiled, f"expected an upsert statement, got: {compiled}"
        assert "kite_console_pnl_sync" in compiled

    @pytest.mark.asyncio
    async def test_account_in_stored_payload_is_masked_form(self):
        session = AsyncMock()
        await mod.ingest_rows(session, "Z1####", "derivatives", "tradebook", [_one_tradebook_row()])
        stmt = session.execute.call_args[0][0]
        payload = stmt.compile().params
        account_values = [v for k, v in payload.items() if k.startswith("account")]
        assert all(v == "Z1####" for v in account_values), (
            f"expected only the masked account form in the insert payload, got {account_values}"
        )
        assert "ZCONSOLE01" not in str(payload), "raw account code must never reach the stored payload"


# ===========================================================================
# 2b. Ledger (Funds Statement) — parsing, validation, dedup key, sync row,
#     and REPLACE-semantics ingestion
# ===========================================================================

def _one_ledger_row(**overrides) -> dict:
    row = {
        "particulars": "Span margin blocked for MCX F&O",
        "posting_date": "2026-11-02",
        "cost_center": "MCX-F&O - Z",
        "voucher_type": "Delivery Voucher",
        "debit": "790270.000000",
        "credit": "0.000000",
        "net_balance": "1043822.913976",
    }
    row.update(overrides)
    return row


class TestParseLedgerCsv:
    def test_well_formed_file_parses_every_data_row(self):
        raw = _ledger_csv_bytes([_one_ledger_row(), _one_ledger_row(posting_date="2026-11-03")])
        rows = mod.parse_ledger_csv(raw)
        assert len(rows) == 2
        assert rows[0]["particulars"] == "Span margin blocked for MCX F&O"
        assert rows[0]["cost_center"] == "MCX-F&O - Z"

    def test_header_only_file_parses_to_zero_rows_not_an_error(self):
        rows = mod.parse_ledger_csv(_ledger_csv_bytes())
        assert rows == []

    def test_missing_required_column_raises_validation_error(self):
        raw = b"particulars,posting_date,debit,credit\nfoo,2026-11-02,1,0\n"  # no net_balance
        with pytest.raises(mod.ReportValidationError, match="missing required column"):
            mod.parse_ledger_csv(raw)

    def test_empty_file_raises_validation_error(self):
        with pytest.raises(mod.ReportValidationError, match="no header row"):
            mod.parse_ledger_csv(b"")

    def test_real_amounts_have_no_thousands_separators(self):
        """Confirmed live, 2026-11-26: the downloaded CSV's own amounts
        are plain decimals (e.g. "27579.231718") -- never comma-grouped
        like the UI's own rendered display. _parse_float_cell must
        still parse them correctly either way (it strips commas
        defensively), but this pins what the REAL file actually looks
        like, so a future change can't silently assume the wrong
        format."""
        raw = _ledger_csv_bytes([_one_ledger_row(debit="1241460.000000")])
        rows = mod.parse_ledger_csv(raw)
        assert rows[0]["debit"] == "1241460.000000"
        assert "," not in rows[0]["debit"]


class TestValidateLedgerRows:
    def test_well_formed_rows_pass(self):
        rows = mod.parse_ledger_csv(_ledger_csv_bytes([_one_ledger_row()]))
        mod.validate_ledger_rows(rows, start=date(2026, 11, 1), end=date(2026, 11, 30))  # no raise

    def test_empty_report_is_not_an_error_by_default(self):
        mod.validate_ledger_rows([], start=date(2026, 11, 1), end=date(2026, 11, 30))  # no raise

    def test_empty_report_raises_when_allow_empty_is_false(self):
        with pytest.raises(mod.ReportValidationError, match="zero rows"):
            mod.validate_ledger_rows(
                [], start=date(2026, 11, 1), end=date(2026, 11, 30), allow_empty=False,
            )

    def test_opening_and_closing_balance_sentinels_are_exempt_not_flagged(self):
        """The two sentinel rows carry no posting_date at all -- they
        must never be treated as "a row with a date outside the
        range", the real failure mode a naive date-range check would
        produce."""
        rows = [
            {"particulars": "Opening Balance", "posting_date": "", "cost_center": "",
             "voucher_type": "", "debit": "", "credit": "", "net_balance": "1861672.145694"},
            _one_ledger_row(),
            {"particulars": "Closing Balance", "posting_date": "", "cost_center": "",
             "voucher_type": "", "debit": "", "credit": "", "net_balance": "-6331331.418538"},
        ]
        mod.validate_ledger_rows(rows, start=date(2026, 11, 1), end=date(2026, 11, 30))  # no raise

    def test_row_date_outside_requested_range_raises(self):
        rows = [_one_ledger_row(posting_date="2025-01-01")]
        with pytest.raises(mod.ReportValidationError, match="outside the requested range"):
            mod.validate_ledger_rows(rows, start=date(2026, 11, 1), end=date(2026, 11, 30))

    def test_missing_particulars_raises(self):
        rows = [_one_ledger_row(particulars="")]
        with pytest.raises(mod.ReportValidationError, match="missing particulars"):
            mod.validate_ledger_rows(rows, start=date(2026, 11, 1), end=date(2026, 11, 30))

    def test_unparsable_debit_raises(self):
        rows = [_one_ledger_row(debit="not-a-number")]
        with pytest.raises(mod.ReportValidationError, match="unparsable debit/credit"):
            mod.validate_ledger_rows(rows, start=date(2026, 11, 1), end=date(2026, 11, 30))


class TestBuildDedupKeyLedger:
    def test_ledger_key_is_range_plus_sequence(self):
        key = mod.build_dedup_key(
            "ledger", row=_one_ledger_row(),
            pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30), seq=3,
        )
        assert key == "2026-11-01:2026-11-30:3"

    def test_two_different_rows_in_the_same_pull_get_different_keys(self):
        k1 = mod.build_dedup_key("ledger", row=_one_ledger_row(),
                                  pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30), seq=0)
        k2 = mod.build_dedup_key("ledger", row=_one_ledger_row(),
                                  pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30), seq=1)
        assert k1 != k2


class TestBuildLedgerSyncRow:
    def test_builds_the_expected_shape(self):
        payload = mod.build_ledger_sync_row(
            "Z1####", _one_ledger_row(), seq=0,
            pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        assert payload["account"] == "Z1####"
        assert payload["segment"] == "all"
        assert payload["report_type"] == "ledger"
        assert payload["trade_date"] == date(2026, 11, 2)
        assert payload["symbol"] is None
        assert payload["exchange"] is None
        assert payload["trade_id"] is None
        assert payload["order_id"] is None
        assert payload["pull_range_start"] == date(2026, 11, 1)
        assert payload["pull_range_end"] == date(2026, 11, 30)
        assert payload["raw_row"]["particulars"] == "Span margin blocked for MCX F&O"
        assert payload["raw_row"]["debit"] == "790270.000000"

    def test_sentinel_row_with_no_posting_date_returns_none(self):
        sentinel = {"particulars": "Opening Balance", "posting_date": "", "cost_center": "",
                    "voucher_type": "", "debit": "", "credit": "", "net_balance": "1861672.145694"}
        payload = mod.build_ledger_sync_row(
            "Z1####", sentinel, seq=0,
            pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        assert payload is None


class TestIngestLedgerRowsSqlShape:
    @pytest.mark.asyncio
    async def test_always_deletes_the_chunk_range_even_with_zero_rows(self):
        """Confirms the REPLACE half of the contract runs even when the
        fresh pull is empty — a genuinely no-activity month must clear
        any stale rows a prior (buggy or since-corrected) pull left
        behind, not leave them in place forever."""
        session = AsyncMock()
        n = await mod.ingest_ledger_rows(
            session, "Z1####", [], pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        assert n == 0
        session.execute.assert_awaited_once()  # delete only, no insert
        session.commit.assert_awaited_once()
        stmt = session.execute.call_args[0][0]
        from sqlalchemy.dialects import postgresql
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "DELETE" in compiled.upper()
        assert "kite_console_pnl_sync" in compiled

    @pytest.mark.asyncio
    async def test_deletes_then_inserts_when_rows_are_present(self):
        session = AsyncMock()
        rows = [_one_ledger_row(), _one_ledger_row(posting_date="2026-11-05")]
        n = await mod.ingest_ledger_rows(
            session, "Z1####", rows, pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        assert n == 2
        assert session.execute.await_count == 2
        from sqlalchemy.dialects import postgresql
        delete_stmt = session.execute.call_args_list[0][0][0]
        insert_stmt = session.execute.call_args_list[1][0][0]
        assert "DELETE" in str(delete_stmt.compile(dialect=postgresql.dialect())).upper()
        insert_compiled = str(insert_stmt.compile(dialect=postgresql.dialect()))
        assert "INSERT" in insert_compiled.upper()
        assert "ON CONFLICT" not in insert_compiled.upper(), (
            "ledger ingestion is delete-then-plain-insert, never an upsert "
            "(see ingest_ledger_rows's own docstring for why)"
        )

    @pytest.mark.asyncio
    async def test_sentinel_rows_are_silently_dropped_not_inserted(self):
        session = AsyncMock()
        rows = [
            {"particulars": "Opening Balance", "posting_date": "", "cost_center": "",
             "voucher_type": "", "debit": "", "credit": "", "net_balance": "1861672.145694"},
            _one_ledger_row(),
        ]
        n = await mod.ingest_ledger_rows(
            session, "Z1####", rows, pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        assert n == 1  # only the real entry, sentinel dropped

    @pytest.mark.asyncio
    async def test_account_in_stored_payload_is_masked_form(self):
        session = AsyncMock()
        await mod.ingest_ledger_rows(
            session, "Z1####", [_one_ledger_row()],
            pull_start=date(2026, 11, 1), pull_end=date(2026, 11, 30),
        )
        insert_stmt = session.execute.call_args_list[1][0][0]
        payload = insert_stmt.compile().params
        account_values = [v for k, v in payload.items() if k.startswith("account")]
        assert all(v == "Z1####" for v in account_values)


# ===========================================================================
# 3. Account masking
# ===========================================================================

class TestAccountMasking:
    def test_register_accounts_then_mask_produces_canonical_form(self):
        from backend.shared.helpers.utils import mask_account, register_accounts
        register_accounts(["ZCONSOLE01"])
        assert mask_account("ZCONSOLE01") == "ZC####", mask_account("ZCONSOLE01")

    def test_sync_outcome_account_field_never_carries_raw_code(self):
        outcome = mod.SyncOutcome(account="Z1####", ok=True)
        assert "ZCONSOLE01" not in outcome.account


# ===========================================================================
# 4. Login / cooloff reuse — the REAL state machine, not a reimplementation
# ===========================================================================

class TestLoginCooloffReuse:
    def test_first_login_failure_arms_its_own_cooloff_120s_on_its_own_file(self):
        """Renamed (was '...arms_the_real_shared_cooloff...') — the
        formula/state machine is still reused verbatim, but the file it
        persists to is no longer shared with the live REST path. See
        TestLoginCooloffIndependence below for the discriminating test."""
        conn = _console_conn_bare("ZCONSOLE_COOL1")
        conn.init_kite_conn = MagicMock(side_effect=mod.ConsoleLoginError("boom"))
        before = time.time()

        with pytest.raises(mod.ConsoleLoginError):
            conn._attempt_login_with_cooloff()

        assert conn._login_fail_streak == 1
        cooloff_s = conn._login_blocked_until - before
        assert 115 <= cooloff_s <= 125, f"expected ~120s, got {cooloff_s:.1f}s"

        # The persisted file must be this script's OWN console path, never
        # the live REST path's file.
        assert conn._login_cooloff_path() == mod._CONSOLE_LOGIN_COOLOFF_PATH
        import json
        import os
        from backend.brokers import connections as _conn_mod
        assert os.path.exists(mod._CONSOLE_LOGIN_COOLOFF_PATH)
        with open(mod._CONSOLE_LOGIN_COOLOFF_PATH) as f:
            console_data = json.load(f)
        assert "ZCONSOLE_COOL1" in console_data
        assert not os.path.exists(_conn_mod._KITE_LOGIN_COOLOFF_PATH), (
            "a Console-login failure must never write to the live REST "
            "path's own cooloff file"
        )

    def test_escalation_sequence_matches_120_240_480_960_1800(self):
        """The REAL sequence the shared formula produces — pinned
        explicitly because the operator brief's own summary omitted the
        960s tier."""
        conn = _console_conn_bare("ZCONSOLE_COOL2")
        conn.init_kite_conn = MagicMock(side_effect=mod.ConsoleLoginError("boom"))
        expected = [120, 240, 480, 960, 1800, 1800]
        for expected_s in expected:
            conn._login_blocked_until = 0.0  # simulate the window having elapsed
            before = time.time()
            with pytest.raises(mod.ConsoleLoginError):
                conn._attempt_login_with_cooloff()
            cooloff_s = conn._login_blocked_until - before
            assert expected_s - 5 <= cooloff_s <= expected_s + 5, (
                f"streak={conn._login_fail_streak}: expected ~{expected_s}s, got {cooloff_s:.1f}s"
            )

    def test_cooling_off_state_short_circuits_with_no_outbound_call(self):
        """Mirrors the base class's own 'C' test class — proves our
        subclass's init_kite_conn override is never invoked while a prior
        failure's cooloff window is still active."""
        conn = _console_conn_bare("ZCONSOLE_COOL3")
        conn._login_blocked_until = time.time() + 999
        conn.init_kite_conn = MagicMock(
            side_effect=AssertionError("init_kite_conn must never be called while cooling off")
        )
        assert conn.is_login_cooling_off() is True
        conn.init_kite_conn.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_account_skips_scraping_entirely_when_cooling_off(self, monkeypatch):
        """End-to-end (within sync_account) confirmation that a cooling-off
        account makes no login attempt and no scraping call at all."""
        conn = _console_conn_bare("ZCONSOLE_COOL4")
        conn._login_blocked_until = time.time() + 999
        conn._attempt_login_with_cooloff = MagicMock(
            side_effect=AssertionError("must not attempt login while cooling off")
        )
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)
        row = _fake_broker_account_row("ZCONSOLE_COOL4")

        outcome = await mod.sync_account(row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))})

        assert outcome.ok is False
        assert outcome.reason == "login_cooling_off"
        conn._attempt_login_with_cooloff.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_account_runs_scrape_off_the_main_event_loop_thread(self, monkeypatch):
        """2026-11-26 threading fix, discriminating regression test — the
        first real run against the live Console failed with
        'It looks like you are using Playwright Sync API inside the
        asyncio loop' because `sync_account()` used to call the sync,
        Playwright-touching login/scrape work directly on the main
        event-loop thread. Confirms the fix: `_console_login_and_scrape_sync`
        now runs on a DIFFERENT thread than the one driving this test's
        own event loop, via `asyncio.to_thread()`."""
        main_thread_ident = threading.get_ident()
        seen_thread_idents: "list[int]" = []

        def fake_scrape_sync(conn, plan):
            seen_thread_idents.append(threading.get_ident())
            return {"login_ok": True, "login_failed": False, "chunks": []}

        monkeypatch.setattr(mod, "_console_login_and_scrape_sync", fake_scrape_sync)
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: object())
        row = _fake_broker_account_row("ZCONSOLE_THREAD1")

        outcome = await mod.sync_account(
            row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))},
        )

        assert outcome.ok is True
        assert len(seen_thread_idents) == 1
        assert seen_thread_idents[0] != main_thread_ident, (
            "the Playwright-touching scrape must run off the main event "
            "loop thread -- calling it directly on this thread is exactly "
            "the bug a real run hit."
        )

    def test_console_login_error_is_distinct_from_structural_error(self):
        """ConsoleStructuralError (post-login scraping) must NEVER be
        mistaken, by type, for a ConsoleLoginError (pre-login failure) —
        they drive different code paths (one feeds the cooloff, one
        must not)."""
        assert not issubclass(mod.ConsoleStructuralError, mod.ConsoleLoginError)
        assert issubclass(mod.ConsoleUnexpectedChallengeError, mod.ConsoleLoginError)


# ===========================================================================
# 4b. Cooloff INDEPENDENCE — the discriminating tests. Comparing two
# EXISTING instances' in-memory state (what the original design already
# had) passes even without the fix — these tests instead construct a
# FRESH instance on the OTHER side, after a failure on one side, and
# prove it was never blocked. Confirmed to fail against the pre-fix code
# (both classes resolving the same bare module-global
# `_KITE_LOGIN_COOLOFF_PATH` name) before `_login_cooloff_path()` existed.
# ===========================================================================

class TestLoginCooloffIndependence:
    def test_failed_console_login_never_blocks_a_fresh_live_rest_connection(self):
        from backend.brokers.connections import KiteConnection

        account = "ZINDEP_CONSOLE_FIRST"
        console_conn = _console_conn_bare(account)
        console_conn.init_kite_conn = MagicMock(
            side_effect=mod.ConsoleLoginError("bad TOTP / CAPTCHA")
        )

        with pytest.raises(mod.ConsoleLoginError):
            console_conn._attempt_login_with_cooloff()

        assert console_conn._login_blocked_until > time.time(), (
            "sanity check: the console connection itself must be cooling off"
        )

        # A FRESH, real KiteConnection for the SAME account — restoring
        # from disk during __init__ — must come up with no block at all.
        live_conn = KiteConnection(account, _fake_secrets_blob(account))
        assert live_conn._login_blocked_until == 0.0, (
            f"a failed Console login must never block a fresh live REST "
            f"connection for the same account, got "
            f"_login_blocked_until={live_conn._login_blocked_until}"
        )

        # And it must actually be able to attempt a (mocked) fresh login —
        # not short-circuit on a cooloff gate it never should have hit.
        live_conn.init_kite_conn = MagicMock(return_value=None)  # succeeds
        result = live_conn.get_kite_conn(test_conn=True)
        assert result is live_conn.kite
        live_conn.init_kite_conn.assert_called_once()

    def test_failed_live_rest_login_never_blocks_a_fresh_console_connection(self):
        from backend.brokers.connections import KiteConnection

        account = "ZINDEP_LIVE_FIRST"
        live_conn = KiteConnection(account, _fake_secrets_blob(account))
        live_conn.init_kite_conn = MagicMock(side_effect=RuntimeError("400 Bad Request"))

        with pytest.raises(RuntimeError):
            live_conn._attempt_login_with_cooloff()

        assert live_conn._login_blocked_until > time.time(), (
            "sanity check: the live connection itself must be cooling off"
        )

        # A FRESH console connection for the SAME account must come up
        # with no block, and must actually be able to attempt its own
        # (mocked) browser login.
        console_conn = mod.build_console_connection(account, _fake_secrets_blob(account))
        assert console_conn.is_login_cooling_off() is False, (
            "a failed live REST login must never block a fresh Console "
            "connection for the same account"
        )
        console_conn.init_kite_conn = MagicMock(return_value=None)  # succeeds
        console_conn._attempt_login_with_cooloff()
        console_conn.init_kite_conn.assert_called_once()

    def test_the_two_disk_files_never_collide_on_the_same_path(self):
        """Belt-and-suspenders: the two path constants themselves must
        differ, independent of any monkeypatching this test module's own
        fixture applies."""
        from backend.brokers import connections as _conn_mod

        assert mod._CONSOLE_LOGIN_COOLOFF_PATH != _conn_mod._KITE_LOGIN_COOLOFF_PATH


# ===========================================================================
# 5. Scraping robustness — mocked KiteConsoleReportsPage
# ===========================================================================

class TestScrapingRobustness:
    """2026-11-26 — `sync_account()` now pulls BOTH report types
    (tradebook + pnl) per segment, so every test here that used to
    expect exactly ONE scrape attempt for a single-segment plan now
    expects exactly TWO (one per report type) — "retries per chunk" means
    "up to `_CHUNK_SCRAPE_MAX_ATTEMPTS` retries of the SAME pull before
    alerting once", never "retries across different pulls".

    2026-11-26, same day, follow-up (sixth/seventh real runs found a
    genuine Kite Console race a single retry-less attempt could never
    recover from -- see `_CHUNK_SCRAPE_MAX_ATTEMPTS`'s own comment):
    each of the 3 pulls below (tradebook, pnl, ledger) now retries
    internally up to `_CHUNK_SCRAPE_MAX_ATTEMPTS` (3) times before this
    module alerts even once for that pull -- so every underlying
    Playwright call this class asserts on now fires 3x per pull, while
    the alert/diagnostics count per pull stays exactly 1 (3 total for a
    single-segment plan), unchanged from before this follow-up."""

    @pytest.mark.asyncio
    async def test_reports_page_not_loaded_alerts_once_after_exhausting_retries(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_SCR1")
        conn._attempt_login_with_cooloff = MagicMock()  # succeeds, no-op
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(
            side_effect=mod.ConsoleStructuralError("reports page never loaded")
        )
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)

        diag_mock = MagicMock(return_value={"screenshot": "x.png", "html": "x.html"})
        monkeypatch.setattr(mod, "capture_diagnostics", diag_mock)
        alert_mock = AsyncMock()
        monkeypatch.setattr(mod, "alert_critical", alert_mock)

        row = _fake_broker_account_row("ZCONSOLE_SCR1")
        outcome = await mod.sync_account(
            row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))},
        )

        assert outcome.ok is False
        # One segment x two report types, plus one ledger pull per
        # account = 3 pulls, each retried _CHUNK_SCRAPE_MAX_ATTEMPTS
        # times before this pull's own single alert fires.
        assert fake_page.navigate_to_reports.call_count == 3 * mod._CHUNK_SCRAPE_MAX_ATTEMPTS
        assert diag_mock.call_count == 3
        assert alert_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_date_range_filter_mismatch_alerts_once_after_exhausting_retries(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_SCR2")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        fake_page = MagicMock()
        _date_range_error = mod.ConsoleStructuralError("date range did not register")
        fake_page.set_date_range = MagicMock(side_effect=_date_range_error)
        # The ledger pull uses a SEPARATE method (different page, different
        # selector) but shares the same underlying fill-then-confirm logic
        # in production (`_set_date_range_on()`) -- a real date-range
        # regression would plausibly hit both, so extending the mock to
        # cover both is the honest way to widen this scenario to 3 pulls.
        fake_page.set_ledger_date_range = MagicMock(side_effect=_date_range_error)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)
        monkeypatch.setattr(mod, "capture_diagnostics", MagicMock(return_value={}))
        alert_mock = AsyncMock()
        monkeypatch.setattr(mod, "alert_critical", alert_mock)

        row = _fake_broker_account_row("ZCONSOLE_SCR2")
        await mod.sync_account(row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))})

        assert fake_page.set_date_range.call_count == 2 * mod._CHUNK_SCRAPE_MAX_ATTEMPTS
        assert fake_page.set_ledger_date_range.call_count == 1 * mod._CHUNK_SCRAPE_MAX_ATTEMPTS
        assert alert_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_download_never_fires_alerts_once_after_exhausting_retries(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_SCR3")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        fake_page = MagicMock()
        fake_page.trigger_download = MagicMock(
            side_effect=mod.ConsoleStructuralError("download never fired")
        )
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)
        monkeypatch.setattr(mod, "capture_diagnostics", MagicMock(return_value={}))
        alert_mock = AsyncMock()
        monkeypatch.setattr(mod, "alert_critical", alert_mock)

        row = _fake_broker_account_row("ZCONSOLE_SCR3")
        await mod.sync_account(row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))})

        # tradebook + pnl (per-segment) + ledger (once per account) == 3
        # pulls, each retried _CHUNK_SCRAPE_MAX_ATTEMPTS times.
        assert fake_page.trigger_download.call_count == 3 * mod._CHUNK_SCRAPE_MAX_ATTEMPTS
        assert alert_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_transient_failure_recovers_on_retry_no_alert_fires(self, monkeypatch):
        """The actual point of the retry: a chunk that fails on its first
        attempt but succeeds on a later one must ingest normally and
        never alert at all -- confirms the retry is load-bearing, not
        just present in the call count."""
        conn = _console_conn_bare("ZCONSOLE_SCR_RETRY")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(
            side_effect=[mod.ConsoleStructuralError("transient"), None, None, None, None, None],
        )
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)
        monkeypatch.setattr(mod, "capture_diagnostics", MagicMock(return_value={}))
        alert_mock = AsyncMock()
        monkeypatch.setattr(mod, "alert_critical", alert_mock)
        monkeypatch.setattr(
            mod, "parse_tradebook_csv", lambda raw: [],
        )
        monkeypatch.setattr(mod, "parse_pnl_statement_xlsx", lambda raw: [])
        monkeypatch.setattr(mod, "parse_ledger_csv", lambda raw: [])
        ingest_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)
        monkeypatch.setattr(mod, "ingest_ledger_rows", AsyncMock(return_value=0))

        row = _fake_broker_account_row("ZCONSOLE_SCR_RETRY")
        outcome = await mod.sync_account(
            row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))},
        )

        assert alert_mock.await_count == 0
        assert outcome.ok is True

    @pytest.mark.asyncio
    async def test_invalid_downloaded_file_is_never_silently_ingested(self, monkeypatch, tmp_path):
        conn = _console_conn_bare("ZCONSOLE_SCR4")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        bad_file = tmp_path / "tradebook.csv"
        bad_file.write_bytes(b"")  # empty/corrupt download
        fake_page = MagicMock()
        fake_page.trigger_download = MagicMock(return_value=bad_file)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)
        alert_mock = AsyncMock()
        monkeypatch.setattr(mod, "alert_critical", alert_mock)
        ingest_mock = AsyncMock()
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)

        row = _fake_broker_account_row("ZCONSOLE_SCR4")
        outcome = await mod.sync_account(row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))})

        ingest_mock.assert_not_called()
        # tradebook pull + pnl pull + ledger pull, all empty/corrupt
        assert alert_mock.await_count == 3
        assert outcome.ok is False

    @pytest.mark.asyncio
    async def test_one_segment_failure_does_not_abort_the_other_segment(self, monkeypatch, tmp_path):
        conn = _console_conn_bare("ZCONSOLE_SCR5")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        tradebook_file = tmp_path / "tradebook.csv"
        tradebook_file.write_bytes(_csv_bytes([_one_tradebook_row(trade_date="2026-11-15")]))
        pnl_file = tmp_path / "pnl.xlsx"
        pnl_file.write_bytes(_pnl_xlsx_bytes([_one_pnl_row()]))
        ledger_file = tmp_path / "ledger.csv"
        ledger_file.write_bytes(_ledger_csv_bytes())  # header-only, 0 rows

        calls: list[str] = []
        last_report_type: "list[str]" = []

        def _fake_navigate(report_type):
            calls.append("navigate")
            last_report_type.append(report_type)

        def _fake_select_segment(segment):
            calls.append(f"segment:{segment}")
            if segment == "derivatives":
                raise mod.ConsoleStructuralError("boom on derivatives")

        def _fake_trigger_download(report_type=None, start=None, end=None, masked_account=None):
            if last_report_type[-1] == "tradebook":
                return tradebook_file
            if last_report_type[-1] == "ledger":
                return ledger_file
            return pnl_file

        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(side_effect=_fake_navigate)
        fake_page.select_segment = MagicMock(side_effect=_fake_select_segment)
        fake_page.set_date_range = MagicMock()
        fake_page.set_ledger_date_range = MagicMock()
        fake_page.trigger_download = MagicMock(side_effect=_fake_trigger_download)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)
        monkeypatch.setattr(mod, "capture_diagnostics", MagicMock(return_value={}))
        monkeypatch.setattr(mod, "alert_critical", AsyncMock())
        ingest_mock = AsyncMock(return_value=1)
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)
        ingest_ledger_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(mod, "ingest_ledger_rows", ingest_ledger_mock)

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        row = _fake_broker_account_row("ZCONSOLE_SCR5")
        outcome = await mod.sync_account(
            row,
            plan={
                "derivatives": (date(2026, 11, 1), date(2026, 11, 30)),
                "commodities": (date(2026, 11, 1), date(2026, 11, 30)),
            },
        )

        assert "segment:derivatives" in calls and "segment:commodities" in calls, (
            f"both segments must be attempted, got calls={calls}"
        )
        assert outcome.ok is False  # derivatives segment failed (both report types)
        # commodities: both tradebook AND pnl pulls succeed and ingest.
        assert outcome.rows_ingested == 2
        # derivatives failed on select_segment for BOTH report types.
        assert len(outcome.segment_errors) == 2
        assert ingest_mock.await_count == 2


class _FakeSessionCtx:
    """A minimal stand-in for `async_session()`'s async-context-manager
    shape, used only by the one multi-segment test above that needs
    `sync_account`'s `async with async_session() as session:` block to
    resolve to *something* without a real DB."""

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *a):
        return False

    def __call__(self):
        return self


# ===========================================================================
# 5b. KiteConsoleReportsPage.verify_logged_in — ERR_ABORTED swallowed
# ===========================================================================

class _FakePlaywrightError(Exception):
    """Stand-in for `playwright.sync_api.Error` — monkeypatched into
    `mod`'s own `Error`/`TimeoutError` names so `verify_logged_in()`'s
    local `from playwright.sync_api import Error as PlaywrightError`
    binds to these fakes instead of needing the real playwright package
    installed in the test environment."""


class _FakePlaywrightTimeoutError(_FakePlaywrightError):
    pass


def _patch_playwright_errors(monkeypatch):
    import sys
    import types as _types
    fake_module = _types.SimpleNamespace(
        Error=_FakePlaywrightError,
        TimeoutError=_FakePlaywrightTimeoutError,
    )
    fake_parent = _types.SimpleNamespace(sync_api=fake_module)
    monkeypatch.setitem(sys.modules, "playwright", fake_parent)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)


def _get_by_role_dispatcher(button_mock, link_mock):
    """Real `page.get_by_role()` returns a DIFFERENT locator per
    (role, name) pair — a single blanket `return_value` conflates the
    "Login with Kite" button probe and the "Reports" link wait onto one
    mock, hiding real bugs where one is called the wrong number of times
    or with the wrong args. This dispatches by the `name=` kwarg, matching
    the real two distinct locators `verify_logged_in()` asks for."""
    def _dispatch(role, name=None, exact=None):
        if name == "Login with Kite":
            return button_mock
        if name == "Reports":
            return link_mock
        return MagicMock()
    return _dispatch


class TestVerifyLoggedInErrAborted:
    """2026-11-26 fixes (second, third, fourth real runs found these):

    1. `goto()` can raise a bare `Error: net::ERR_ABORTED` — a known
       Playwright/Chromium race where the navigation we started is
       cancelled by a second, client-side-redirect navigation before
       `domcontentloaded` fires. Swallowed, but not sufficient alone.
    2. Swallowing ERR_ABORTED let kite.zerodha.com's own in-flight
       post-TOTP redirect WIN that race, landing back on kite's own
       dashboard (no Reports link there) instead of console.zerodha.com.
       The fix checks `page.url` after each attempt and retries (bounded,
       3 attempts) until it genuinely lands on the console host.
    3. Landing on console.zerodha.com is not the same as being
       authenticated there — the real page is a logged-out marketing
       landing page with a real "Login with Kite" button (confirmed via
       the real HTML dump). The fix clicks it when present, as an
       expected one-time OAuth bounce, not a failure."""

    def test_err_aborted_on_goto_is_swallowed_when_landed_url_is_console(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.side_effect = _FakePlaywrightError("Page.goto: net::ERR_ABORTED")
        page.url = "https://console.zerodha.com/dashboard"
        button, reports_link = MagicMock(), MagicMock()
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        console.verify_logged_in()  # must not raise

        reports_link.wait_for.assert_called_once()

    def test_non_err_aborted_playwright_error_still_propagates(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.side_effect = _FakePlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED")

        console = mod.KiteConsoleReportsPage(page)
        with pytest.raises(_FakePlaywrightError, match="ERR_CONNECTION_REFUSED"):
            console.verify_logged_in()

    def test_timeout_waiting_for_reports_link_still_raises_console_login_error(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.return_value = None
        page.url = "https://console.zerodha.com/dashboard"
        page.inner_text.return_value = "welcome back"  # no captcha tokens present
        button, reports_link = MagicMock(), MagicMock()
        button.wait_for.side_effect = _FakePlaywrightTimeoutError("no button")  # already authenticated
        reports_link.wait_for.side_effect = _FakePlaywrightTimeoutError("timed out")
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        with pytest.raises(mod.ConsoleLoginError, match="authenticated dashboard"):
            console.verify_logged_in()

    def test_kite_redirect_wins_the_race_retries_then_raises_a_distinct_error(self, monkeypatch):
        """The exact live incident: every attempt lands back on
        kite.zerodha.com's own dashboard, never console.zerodha.com. Must
        raise a message naming the real cause (the redirect race), not
        the generic "Reports link timed out" message — they are different
        failures and must not be conflated."""
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.return_value = None
        page.url = "https://kite.zerodha.com/dashboard"
        page.wait_for_timeout = MagicMock()
        button, reports_link = MagicMock(), MagicMock()
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        with pytest.raises(mod.ConsoleLoginError, match="redirect kept winning"):
            console.verify_logged_in()

        assert page.goto.call_count == 3
        button.wait_for.assert_not_called()
        reports_link.wait_for.assert_not_called()

    def test_retry_succeeds_on_second_attempt_after_losing_first_race(self, monkeypatch):
        """First attempt loses the race (lands on kite's dashboard),
        second attempt wins (lands on console) -- confirms the retry
        loop recovers from a transient race rather than needing the full
        3 attempts every time."""
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.url = "https://kite.zerodha.com/dashboard"
        call_count = {"n": 0}

        def _first_attempt_loses_second_wins(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                page.url = "https://console.zerodha.com/dashboard"

        page.goto.side_effect = _first_attempt_loses_second_wins
        page.wait_for_timeout = MagicMock()
        button, reports_link = MagicMock(), MagicMock()
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        console.verify_logged_in()  # must not raise

        assert page.goto.call_count == 2
        reports_link.wait_for.assert_called_once()

    def test_login_with_kite_button_clicked_when_landing_page_is_logged_out(self, monkeypatch):
        """The real live case: landing on console.zerodha.com shows the
        logged-out marketing page with a real "Login with Kite" button
        (confirmed via live HTML dump). It must be clicked, and the page
        must settle, before the Reports-link wait."""
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.return_value = None
        page.url = "https://console.zerodha.com/"
        button, reports_link = MagicMock(), MagicMock()
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        console.verify_logged_in()  # must not raise

        button.wait_for.assert_called_once_with(state="visible", timeout=3000)
        button.click.assert_called_once()
        page.wait_for_load_state.assert_called_once_with("domcontentloaded", timeout=20000)
        reports_link.wait_for.assert_called_once()

    def test_login_with_kite_button_absent_is_a_silent_no_op(self, monkeypatch):
        """Already-authenticated landing (no button) must not click
        anything or treat the absence as an error — only the Reports
        link is checked."""
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        page.goto.return_value = None
        page.url = "https://console.zerodha.com/reports"
        button, reports_link = MagicMock(), MagicMock()
        button.wait_for.side_effect = _FakePlaywrightTimeoutError("no button present")
        page.get_by_role.side_effect = _get_by_role_dispatcher(button, reports_link)

        console = mod.KiteConsoleReportsPage(page)
        console.verify_logged_in()  # must not raise

        button.click.assert_not_called()
        reports_link.wait_for.assert_called_once()


# ===========================================================================
# 5c. navigate_to_reports — retry on a slow/blank render
# ===========================================================================

class TestNavigateToReportsRetry:
    """2026-11-26 fix (fifth real run found this): a long single-browser
    backfill session sometimes renders a genuinely BLANK page well past
    the original 20s landmark wait, confirmed live for commodities/pnl
    and the ledger report, late in a run. `navigate_to_reports()` now
    retries once with a longer timeout before raising."""

    def test_succeeds_on_first_attempt_no_retry(self, monkeypatch):
        page = MagicMock()
        console = mod.KiteConsoleReportsPage(page)
        console.navigate_to_reports("tradebook")
        assert page.locator.return_value.first.wait_for.call_count == 1

    def test_slow_first_render_recovers_on_retry(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        select_locator = page.locator.return_value.first
        select_locator.wait_for.side_effect = [
            _FakePlaywrightTimeoutError("first attempt too slow"),
            None,
        ]

        console = mod.KiteConsoleReportsPage(page)
        console.navigate_to_reports("tradebook")  # must not raise

        assert select_locator.wait_for.call_count == 2
        assert page.goto.call_count == 2

    def test_both_attempts_blank_raises_with_report_type_and_url(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page = MagicMock()
        select_locator = page.locator.return_value.first
        select_locator.wait_for.side_effect = _FakePlaywrightTimeoutError("blank page")

        console = mod.KiteConsoleReportsPage(page)
        with pytest.raises(mod.ConsoleStructuralError, match="'pnl' report page"):
            console.navigate_to_reports("pnl")

        assert select_locator.wait_for.call_count == 2


# ===========================================================================
# 5d. trigger_download — real results-rendered wait, date-range staleness
# ===========================================================================

class TestTriggerDownloadResultsRendered:
    """2026-11-26 fix (sixth real run found this): a fixed 2s sleep for
    "results table render" was too short, so the download control was
    sometimes clicked against the PREVIOUS chunk's still-rendered table
    -- surfacing as a download timeout, or a silent download of the
    wrong date range caught only by this module's own post-parse
    validation. Replaced with a real landmark wait
    (`_wait_for_results_rendered`) plus a date-range staleness check
    (`_verify_fetched_date_range`)."""

    def _page_with_csv_download(self):
        page = MagicMock()
        download = MagicMock(suggested_filename="tradebook-ZG0790.csv")
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock(value=download)
        cm.__exit__.return_value = False
        page.expect_download.return_value = cm
        return page, download

    def test_waits_for_results_rendered_before_looking_for_download(self, monkeypatch, tmp_path):
        _patch_playwright_errors(monkeypatch)
        page, _download = self._page_with_csv_download()
        monkeypatch.setattr(mod, "_DOWNLOADS_DIR", tmp_path)
        console = mod.KiteConsoleReportsPage(page)
        console.trigger_download(report_type="tradebook")

        page.get_by_text.assert_called_once_with(mod.KiteConsoleReportsPage._RESULTS_RENDERED_TEXT)
        page.get_by_text.return_value.first.wait_for.assert_called_once()

    def test_date_range_verified_when_label_present_and_matches(self, monkeypatch, tmp_path):
        _patch_playwright_errors(monkeypatch)
        page, _download = self._page_with_csv_download()
        label = page.locator.return_value
        label.count.return_value = 1
        label.inner_text.return_value = "2026-04-01 to 2026-04-30"
        monkeypatch.setattr(mod, "_DOWNLOADS_DIR", tmp_path)

        console = mod.KiteConsoleReportsPage(page)
        console.trigger_download(
            report_type="tradebook", start=date(2026, 4, 1), end=date(2026, 4, 30),
        )  # must not raise

    def test_stale_date_range_label_raises_distinct_error(self, monkeypatch):
        _patch_playwright_errors(monkeypatch)
        page, _download = self._page_with_csv_download()
        label = page.locator.return_value
        label.count.return_value = 1
        label.inner_text.return_value = "2026-04-01 to 2026-04-30"  # stale, never updates

        console = mod.KiteConsoleReportsPage(page)
        with pytest.raises(mod.ConsoleStructuralError, match="stale date range"):
            console.trigger_download(
                report_type="tradebook", start=date(2026, 5, 1), end=date(2026, 5, 31),
                timeout_ms=50,
            )

    def test_date_range_label_absent_is_a_silent_no_op(self, monkeypatch, tmp_path):
        """The P&L report page has no #date_fetched_label at all --
        confirmed via a live HTML dump. Absence must never be treated as
        a staleness failure."""
        _patch_playwright_errors(monkeypatch)
        page, _download = self._page_with_csv_download()
        label = page.locator.return_value
        label.count.return_value = 0
        monkeypatch.setattr(mod, "_DOWNLOADS_DIR", tmp_path)

        console = mod.KiteConsoleReportsPage(page)
        console.trigger_download(
            report_type="pnl", start=date(2026, 4, 1), end=date(2026, 4, 30),
        )  # must not raise

        label.inner_text.assert_not_called()

    def test_no_start_end_skips_date_range_verification_entirely(self, monkeypatch, tmp_path):
        """Optional only so existing direct callers/tests that don't pass
        start/end keep working -- real callers always pass both."""
        _patch_playwright_errors(monkeypatch)
        page, _download = self._page_with_csv_download()
        label = page.locator.return_value
        label.count.return_value = 1
        label.inner_text.return_value = "anything, doesn't matter"
        monkeypatch.setattr(mod, "_DOWNLOADS_DIR", tmp_path)

        console = mod.KiteConsoleReportsPage(page)
        console.trigger_download(report_type="tradebook")  # must not raise

        label.inner_text.assert_not_called()


# ===========================================================================
# 6. KiteConsoleConnection.init_kite_conn — mocked Playwright
# ===========================================================================

class TestInitKiteConnPlaywrightFlow:
    def _patch_sync_playwright(self, monkeypatch, page: MagicMock):
        pw_instance = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        pw_instance.chromium.launch.return_value = browser
        pw_factory = MagicMock()
        pw_factory.start.return_value = pw_instance

        fake_module = types.SimpleNamespace(
            sync_playwright=lambda: pw_factory,
            TimeoutError=RuntimeError,
        )
        fake_parent = types.SimpleNamespace(sync_api=fake_module)
        import sys
        monkeypatch.setitem(sys.modules, "playwright", fake_parent)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
        return pw_instance, browser, context, page

    def test_successful_login_stores_console_page_and_browser(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_INIT1")
        page = MagicMock()
        self._patch_sync_playwright(monkeypatch, page)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda p: MagicMock())
        from backend.shared.helpers import utils as shared_utils
        monkeypatch.setattr(shared_utils, "generate_totp", lambda token: "123456")

        conn.init_kite_conn(test_conn=True)

        assert conn._console_page is page
        assert conn._console_browser is not None

    def test_login_step_exception_wraps_as_console_login_error_and_captures_diagnostics(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_INIT2")
        page = MagicMock()
        self._patch_sync_playwright(monkeypatch, page)
        broken_reports = MagicMock()
        broken_reports.goto_login = MagicMock(side_effect=RuntimeError("page.goto timed out"))
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda p: broken_reports)
        diag_mock = MagicMock(return_value={})
        monkeypatch.setattr(mod, "capture_diagnostics", diag_mock)

        with pytest.raises(mod.ConsoleLoginError, match="timed out"):
            conn.init_kite_conn(test_conn=True)
        diag_mock.assert_called_once()

    def test_test_conn_false_is_a_no_op(self, monkeypatch):
        conn = _console_conn_bare("ZCONSOLE_INIT3")
        # No playwright patch at all — if this tried to launch a browser
        # it would raise (playwright isn't installed in this test env).
        conn.init_kite_conn(test_conn=False)
        assert conn._console_page is None

    def test_chromium_launched_with_ipv6_disabled(self, monkeypatch):
        """2026-11-26 live incident: the server's IPv6 route to
        console.zerodha.com is unreachable (`curl -6` times out, `curl -4`
        succeeds), and Chromium's own network stack doesn't go through the
        Python-level IPv4-forcing patch used elsewhere in this codebase
        (see CLAUDE.md's "Network egress quirks" entry) — the very first
        `page.goto()` failed with a bare `net::ERR_ABORTED`, not a CAPTCHA
        or login failure. Pins that `chromium.launch()` is always called
        with `--disable-ipv6`, confirmed to fail against the pre-fix call
        (`pw.chromium.launch(headless=True)`, no args)."""
        conn = _console_conn_bare("ZCONSOLE_INIT4")
        page = MagicMock()
        pw_instance, browser, context, page = self._patch_sync_playwright(monkeypatch, page)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda p: MagicMock())
        from backend.shared.helpers import utils as shared_utils
        monkeypatch.setattr(shared_utils, "generate_totp", lambda token: "123456")

        conn.init_kite_conn(test_conn=True)

        pw_instance.chromium.launch.assert_called_once_with(
            headless=True, args=["--disable-ipv6"],
        )


# ===========================================================================
# 7. Trigger logic — should_sync_today (pure) + supporting helpers
# ===========================================================================

class TestShouldSyncToday:
    def test_true_when_trigger_date_is_today_and_unsynced(self):
        assert mod.should_sync_today(
            date(2026, 11, 20), {date(2026, 11, 20)}, None,
        ) is True

    def test_false_when_no_trigger_dates(self):
        assert mod.should_sync_today(date(2026, 11, 20), set(), None) is False

    def test_false_when_only_trigger_date_already_synced(self):
        assert mod.should_sync_today(
            date(2026, 11, 20), {date(2026, 11, 20)}, date(2026, 11, 20),
        ) is False

    def test_catches_up_on_a_missed_trigger_day(self):
        """A missed run — the script didn't fire on the exact trigger
        day — must still sync on the NEXT run, not skip the expiry
        forever."""
        assert mod.should_sync_today(
            date(2026, 11, 23), {date(2026, 11, 20)}, None,
        ) is True

    def test_does_not_fire_for_a_future_trigger_date(self):
        assert mod.should_sync_today(
            date(2026, 11, 18), {date(2026, 11, 20)}, None,
        ) is False

    def test_multiple_trigger_dates_any_unsynced_one_fires(self):
        assert mod.should_sync_today(
            date(2026, 11, 25),
            {date(2026, 11, 10), date(2026, 11, 24)},
            date(2026, 11, 15),
        ) is True


class TestComputeTriggerDatesAndCollection:
    @pytest.mark.asyncio
    async def test_compute_trigger_dates_uses_next_market_open_ist(self, monkeypatch):
        async def _fake_next_open(after_date, exchange="MCX"):
            return datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist", _fake_next_open,
        )
        out = await mod.compute_trigger_dates({("NFO", date(2026, 11, 20))})
        assert out == {date(2026, 11, 24)}

    def test_collect_recent_fo_expiry_dates_ignores_unparsable_symbols(self):
        """Pure-ish check of the extraction half, independent of the DB
        query — drives parse_tradingsymbol directly the way the real
        function does, on a symbol shape that doesn't parse."""
        from backend.api.algo.derivatives import parse_tradingsymbol
        assert parse_tradingsymbol("NOT_AN_FO_SYMBOL") is None


class TestSyncMarkerPersistence:
    @pytest.mark.asyncio
    async def test_load_last_synced_through_returns_none_when_unset(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_string", lambda key, default="": default,
        )
        assert await mod.load_last_synced_through() is None

    @pytest.mark.asyncio
    async def test_load_last_synced_through_parses_stored_iso_date(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_string",
            lambda key, default="": "2026-11-20" if key == mod._SYNC_MARKER_KEY else default,
        )
        assert await mod.load_last_synced_through() == date(2026, 11, 20)

    @pytest.mark.asyncio
    async def test_mark_synced_through_calls_set_string_with_the_marker_key(self, monkeypatch):
        set_mock = AsyncMock()
        monkeypatch.setattr("backend.shared.helpers.settings.set_string", set_mock)
        await mod.mark_synced_through(date(2026, 11, 24))
        set_mock.assert_awaited_once()
        args, kwargs = set_mock.call_args
        assert args[0] == mod._SYNC_MARKER_KEY
        assert args[1] == "2026-11-24"


# ===========================================================================
# 8. Backfill vs sync scope — genuinely distinct code paths
# ===========================================================================

class TestBackfillVsSyncPlans:
    def test_backfill_plan_covers_all_three_segments_from_fixed_start(self):
        today = date(2026, 11, 24)
        plan = mod.backfill_plan(today)
        assert set(plan.keys()) == {"equity", "derivatives", "commodities"}
        for start, end in plan.values():
            assert start == mod._BACKFILL_START
            assert end == today

    def test_backfill_start_is_april_1_2026(self):
        """2026-11-26 operator instruction — was 2026-03-01."""
        assert mod._BACKFILL_START == date(2026, 4, 1)


# ===========================================================================
# 8b. chunk_by_calendar_month — the ~1-month Kite Console CSV export cap
# ===========================================================================

class TestChunkByCalendarMonth:
    def test_matches_the_operators_own_worked_example(self):
        chunks = mod.chunk_by_calendar_month(date(2026, 4, 1), date(2026, 11, 26))
        assert chunks == [
            (date(2026, 4, 1), date(2026, 4, 30)),
            (date(2026, 5, 1), date(2026, 5, 31)),
            (date(2026, 6, 1), date(2026, 6, 30)),
            (date(2026, 7, 1), date(2026, 7, 31)),
            (date(2026, 8, 1), date(2026, 8, 31)),
            (date(2026, 9, 1), date(2026, 9, 30)),
            (date(2026, 10, 1), date(2026, 10, 31)),
            (date(2026, 11, 1), date(2026, 11, 26)),
        ]

    def test_single_day_range_yields_one_chunk(self):
        assert mod.chunk_by_calendar_month(date(2026, 11, 20), date(2026, 11, 20)) == [
            (date(2026, 11, 20), date(2026, 11, 20)),
        ]

    def test_exact_calendar_month_range_yields_one_chunk(self):
        assert mod.chunk_by_calendar_month(date(2026, 11, 1), date(2026, 11, 30)) == [
            (date(2026, 11, 1), date(2026, 11, 30)),
        ]

    def test_range_spanning_a_december_year_boundary(self):
        chunks = mod.chunk_by_calendar_month(date(2026, 12, 15), date(2027, 1, 10))
        assert chunks == [
            (date(2026, 12, 15), date(2026, 12, 31)),
            (date(2027, 1, 1), date(2027, 1, 10)),
        ]

    def test_inverted_range_returns_empty_list_not_an_error(self):
        assert mod.chunk_by_calendar_month(date(2026, 11, 20), date(2026, 11, 1)) == []

    def test_every_chunk_is_non_overlapping_and_contiguous(self):
        chunks = mod.chunk_by_calendar_month(date(2026, 4, 1), date(2026, 11, 26))
        for (_, prev_end), (cur_start, _) in zip(chunks, chunks[1:]):
            assert cur_start == prev_end + mod.timedelta(days=1)


# ===========================================================================
# 8c. compute_trigger_map / resolve_sync_plan — the 2026-11-26 "previous
# expiry -> current expiry" ongoing-sync redesign
# ===========================================================================

async def _fake_next_open_plus_one_day(after_date, exchange="MCX"):
    return datetime(after_date.year, after_date.month, after_date.day, 8, 0, tzinfo=timezone.utc) + mod.timedelta(days=1)


class TestComputeTriggerMap:
    @pytest.mark.asyncio
    async def test_maps_each_pair_to_its_own_trigger(self, monkeypatch):
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("MCX", date(2026, 10, 20)), ("NFO", date(2026, 11, 20))}
        trigger_map = await mod.compute_trigger_map(pairs)
        assert trigger_map == {
            ("MCX", date(2026, 10, 20)): date(2026, 10, 21),
            ("NFO", date(2026, 11, 20)): date(2026, 11, 21),
        }

    @pytest.mark.asyncio
    async def test_compute_trigger_dates_is_the_flat_value_set(self, monkeypatch):
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("MCX", date(2026, 10, 20)), ("NFO", date(2026, 11, 20))}
        assert await mod.compute_trigger_dates(pairs) == {
            date(2026, 10, 21), date(2026, 11, 21),
        }


class TestResolveSyncPlan:
    """`resolve_sync_plan()` resolves each exchange's OWN due set via
    `compute_trigger_map()` — which calls the REAL `next_market_open_
    ist()` — so every test here monkeypatches it the same way
    `TestComputeTriggerDatesAndCollection` already does, rather than
    hitting a real DB-backed holiday lookup."""

    @pytest.mark.asyncio
    async def test_excludes_equity_always(self, monkeypatch):
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("MCX", date(2026, 11, 20)), ("NFO", date(2026, 11, 20))}
        plan = await mod.resolve_sync_plan(pairs, date(2026, 11, 24), None)
        assert "equity" not in plan

    @pytest.mark.asyncio
    async def test_range_is_previous_expiry_plus_one_to_current_expiry(self, monkeypatch):
        """Exactly one cycle due — `last_synced_through` sits strictly
        between the Oct cycle's own trigger (10-21) and the Nov cycle's
        (11-21), so only Nov is due and Oct serves purely as the
        previous-expiry anchor, never itself as a due cycle."""
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("MCX", date(2026, 10, 20)), ("MCX", date(2026, 11, 20))}
        plan = await mod.resolve_sync_plan(
            pairs, date(2026, 11, 24), date(2026, 10, 21),
        )
        assert plan["commodities"] == (date(2026, 10, 21), date(2026, 11, 20))

    @pytest.mark.asyncio
    async def test_no_previous_expiry_falls_back_to_conservative_window(self, monkeypatch):
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("MCX", date(2026, 11, 20))}
        plan = await mod.resolve_sync_plan(pairs, date(2026, 11, 24), None)
        start, end = plan["commodities"]
        assert end == date(2026, 11, 20)
        assert start == date(2026, 11, 20) - mod.timedelta(days=mod._SYNC_RANGE_FALLBACK_DAYS)

    @pytest.mark.asyncio
    async def test_exchange_with_no_due_trigger_is_excluded(self, monkeypatch):
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        # trigger for 2026-10-01 is 2026-10-02 (via the fake), already
        # covered by last_synced_through — nothing new for NFO today.
        pairs = {("NFO", date(2026, 10, 1))}
        plan = await mod.resolve_sync_plan(
            pairs, date(2026, 11, 24), date(2026, 10, 2),
        )
        assert plan == {}

    @pytest.mark.asyncio
    async def test_segment_shared_by_two_exchanges_takes_the_union(self, monkeypatch):
        """`last_synced_through` (11-06) is chosen so exactly one cycle
        is due per exchange: NFO's Oct cycle (trigger 10-21) and BFO's
        first Nov cycle (trigger 11-06) both predate it and serve only
        as previous-expiry anchors."""
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {
            ("NFO", date(2026, 10, 20)), ("NFO", date(2026, 11, 20)),
            ("BFO", date(2026, 11, 5)), ("BFO", date(2026, 11, 25)),
        }
        plan = await mod.resolve_sync_plan(
            pairs, date(2026, 11, 27), date(2026, 11, 6),
        )
        start, end = plan["derivatives"]
        assert start == date(2026, 10, 21)  # NFO's own previous+1 (the earlier)
        assert end == date(2026, 11, 25)    # BFO's own current (the later)

    @pytest.mark.asyncio
    async def test_expiry_landing_exactly_today_is_not_yet_due(self, monkeypatch):
        """The naive 'latest expiry <= today' rule (the bug this fix
        closes) would wrongly pick an expiry landing on TODAY — common
        for a weekly NFO expiry — even though that cycle's own trigger
        (next market open strictly after it) is tomorrow, not yet
        reached. The correct per-expiry trigger check excludes it and
        picks the PRIOR week's already-settled cycle instead."""
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {("NFO", date(2026, 11, 19)), ("NFO", date(2026, 11, 26))}
        plan = await mod.resolve_sync_plan(pairs, date(2026, 11, 26), None)
        assert plan["derivatives"] == (
            date(2026, 11, 19) - mod.timedelta(days=mod._SYNC_RANGE_FALLBACK_DAYS),
            date(2026, 11, 19),
        ), (
            "the cycle expiring TODAY must be excluded (its own trigger "
            "is tomorrow) — only the already-settled prior week's cycle "
            "is due"
        )

    @pytest.mark.asyncio
    async def test_two_missed_cycles_catch_up_covers_the_full_span(self, monkeypatch):
        """A run missed for more than one cycle must pull the FULL
        missed span — end = the latest due cycle, start = (the expiry
        immediately before the EARLIEST due cycle) + 1 — never only the
        newest cycle's own narrow window, which would silently lose the
        middle cycle(s)."""
        monkeypatch.setattr(
            "backend.api.algo.expiry_freeze.next_market_open_ist",
            _fake_next_open_plus_one_day,
        )
        pairs = {
            ("MCX", date(2026, 9, 20)),    # anchor — serves as "previous" only
            ("MCX", date(2026, 10, 20)),   # missed cycle 1
            ("MCX", date(2026, 11, 20)),   # missed cycle 2 (the most recent)
        }
        # last_synced predates the Oct cycle's own trigger (10-21), so
        # BOTH Oct and Nov are due. Sep must never itself be due.
        plan = await mod.resolve_sync_plan(
            pairs, date(2026, 11, 24), date(2026, 9, 25),
        )
        assert plan["commodities"] == (date(2026, 9, 21), date(2026, 11, 20))


# ===========================================================================
# 9. Alert delivery — real LogStoreHandler, real flush/dispatch pipeline
# ===========================================================================

class TestAlertDeliveryFromStandaloneProcess:
    @pytest.mark.asyncio
    async def test_critical_log_call_reaches_the_in_process_queue(self):
        """Proves logger.critical() from this script's own logger
        genuinely lands in log_store.HANDLER (via the QueueHandler ->
        QueueListener background thread started at import time) — the
        first half of the real pipeline, exercised for real, not mocked."""
        from backend.shared.helpers import log_store

        log_store.HANDLER.drain(10_000)  # clear anything left over from other tests
        mod.logger.critical(
            "test alert body", extra={
                "source_type": "process", "source_name": "Kite Console P&L sync test",
                "tags": ["alert"], "alert_event": "kite_console_sync_failed_TEST",
            },
        )
        deadline = time.monotonic() + 3.0
        while not log_store.HANDLER.pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert log_store.HANDLER.pending(), "CRITICAL record never reached log_store.HANDLER"
        rows = log_store.HANDLER.drain(10)
        matches = [r for r in rows if r["extra"].get("alert_event") == "kite_console_sync_failed_TEST"]
        assert matches, f"expected our tagged record among drained rows, got {rows}"
        assert "error" in matches[0]["tags"], f"CRITICAL must auto-tag 'error', got {matches[0]['tags']}"

    @pytest.mark.asyncio
    async def test_flush_and_dispatch_once_calls_the_real_flush_once(self, monkeypatch):
        """_flush_and_dispatch_once must call log_store.flush_once with
        the SAME HANDLER singleton and event_agents.dispatch_rows — not a
        reimplemented sender."""
        from backend.shared.helpers import log_store
        from backend.api.algo import event_agents

        flush_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(log_store, "flush_once", flush_mock)
        log_store.HANDLER._q.put_nowait({"fake": "row"})  # make pending() True immediately

        await mod._flush_and_dispatch_once(wait_s=0.5)

        flush_mock.assert_awaited_once()
        args, kwargs = flush_mock.call_args
        assert args[0] is log_store.HANDLER
        assert kwargs.get("dispatch") is event_agents.dispatch_rows

    @pytest.mark.asyncio
    async def test_alert_critical_sets_action_required_and_process_identity(self, monkeypatch):
        captured = {}

        def _fake_critical(*args, **kwargs):
            captured["extra"] = kwargs.get("extra")

        monkeypatch.setattr(mod.logger, "critical", _fake_critical)
        flush_mock = AsyncMock()
        monkeypatch.setattr(mod, "_flush_and_dispatch_once", flush_mock)

        await mod.alert_critical("Something failed", "Z1####", detail="boom")

        extra = captured["extra"]
        assert extra["source_type"] == "process"
        assert extra["source_name"] == "Kite Console P&L sync"
        assert extra["action_required"] is True
        assert "alert" in extra["tags"]
        flush_mock.assert_awaited_once()


# ===========================================================================
# 10. Diagnostics capture — directory permissions, never raises
# ===========================================================================

class TestCaptureDiagnostics:
    def test_creates_directory_with_restricted_permissions(self, tmp_path, monkeypatch):
        diag_dir = tmp_path / "diag"
        monkeypatch.setattr(mod, "_DIAGNOSTICS_DIR", diag_dir)
        page = MagicMock()
        page.screenshot = MagicMock()
        page.content = MagicMock(return_value="<html></html>")

        result = mod.capture_diagnostics(page, "Z1####", "test_step")

        assert diag_dir.exists()
        mode = diag_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"expected 0700 permissions, got {oct(mode)}"
        assert result["screenshot"] is not None
        assert result["html"] is not None

    def test_never_raises_even_if_screenshot_fails(self, tmp_path, monkeypatch):
        diag_dir = tmp_path / "diag2"
        monkeypatch.setattr(mod, "_DIAGNOSTICS_DIR", diag_dir)
        page = MagicMock()
        page.screenshot = MagicMock(side_effect=RuntimeError("no display"))
        page.content = MagicMock(return_value="<html></html>")

        result = mod.capture_diagnostics(page, "Z1####", "test_step")  # must not raise
        assert result["screenshot"] is None
        assert result["html"] is not None


# ===========================================================================
# 11. One fully-successful mocked end-to-end run
# ===========================================================================

class TestHappyPathEndToEnd:
    @pytest.mark.asyncio
    async def test_successful_run_ingests_the_expected_rows(self, monkeypatch, tmp_path):
        conn = _console_conn_bare("ZCONSOLE_E2E1")
        conn._attempt_login_with_cooloff = MagicMock()  # login "succeeds"
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        tradebook_file = tmp_path / "tradebook.csv"
        tradebook_file.write_bytes(_csv_bytes([
            _one_tradebook_row(trade_date="2026-11-10", trade_id="T_E2E_1"),
            _one_tradebook_row(trade_date="2026-11-12", trade_id="T_E2E_2"),
        ]))
        pnl_file = tmp_path / "pnl.xlsx"
        pnl_file.write_bytes(_pnl_xlsx_bytes([_one_pnl_row()]))
        ledger_file = tmp_path / "ledger.csv"
        ledger_file.write_bytes(_ledger_csv_bytes())  # header-only, 0 rows

        last_report_type: "list[str]" = []

        def _fake_trigger_download(report_type=None, start=None, end=None, masked_account=None):
            if last_report_type[-1] == "tradebook":
                return tradebook_file
            if last_report_type[-1] == "ledger":
                return ledger_file
            return pnl_file

        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(side_effect=lambda rt: last_report_type.append(rt))
        fake_page.select_segment = MagicMock()
        fake_page.set_date_range = MagicMock()
        fake_page.set_ledger_date_range = MagicMock()
        fake_page.trigger_download = MagicMock(side_effect=_fake_trigger_download)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)

        ingest_mock = AsyncMock(return_value=2)
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)
        monkeypatch.setattr(mod, "ingest_ledger_rows", AsyncMock(return_value=0))
        monkeypatch.setattr("backend.api.database.async_session", lambda: _FakeSessionCtx())

        row = _fake_broker_account_row("ZCONSOLE_E2E1")
        outcome = await mod.sync_account(
            row, plan={"derivatives": (date(2026, 11, 1), date(2026, 11, 30))},
        )

        assert outcome.ok is True, outcome
        assert outcome.rows_ingested == 4  # 2 tradebook pull + 2 pnl pull (mocked return value)
        assert ingest_mock.await_count == 2  # one per report type
        tradebook_call = next(c for c in ingest_mock.call_args_list if c[0][3] == "tradebook")
        ingested_rows = tradebook_call[0][4]
        assert len(ingested_rows) == 2
        assert {r["trade_id"] for r in ingested_rows} == {"T_E2E_1", "T_E2E_2"}
        pnl_call = next(c for c in ingest_mock.call_args_list if c[0][3] == "pnl")
        # 2026-11-26 redesign — the pnl pull no longer uses a separate
        # fixed-anchor-to-today range; it now uses the SAME `plan`
        # range as the tradebook pull (here, a single calendar-month
        # chunk, since the plan's own range IS one calendar month).
        assert pnl_call[1]["pull_start"] == date(2026, 11, 1)
        assert pnl_call[1]["pull_end"] == date(2026, 11, 30)


# ===========================================================================
# 12. load_active_kite_accounts / build_console_secrets_blob
# ===========================================================================

class _FakeDbResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDbSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return _FakeDbResult(self._rows)


class TestLoadActiveKiteAccountsAndSecretsBlob:
    @pytest.mark.asyncio
    async def test_filters_to_requested_accounts(self, monkeypatch):
        row1 = _fake_broker_account_row("ZALL1")
        row2 = _fake_broker_account_row("ZALL2")
        monkeypatch.setattr(
            "backend.api.database.shared_async_session",
            lambda: _FakeDbSession([row1, row2]),
        )
        rows = await mod.load_active_kite_accounts(["ZALL2"])
        assert [r.account for r in rows] == ["ZALL2"]

    @pytest.mark.asyncio
    async def test_no_filter_returns_every_row(self, monkeypatch):
        row1 = _fake_broker_account_row("ZALL3")
        monkeypatch.setattr(
            "backend.api.database.shared_async_session",
            lambda: _FakeDbSession([row1]),
        )
        rows = await mod.load_active_kite_accounts(None)
        assert [r.account for r in rows] == ["ZALL3"]

    def test_build_console_secrets_blob_shape(self):
        row = _fake_broker_account_row("ZBLOB1")
        blob = mod.build_console_secrets_blob("ZBLOB1", row)
        assert blob["kite_accounts"]["ZBLOB1"]["api_key"] == "test_api_key"
        assert blob["kite_accounts"]["ZBLOB1"]["password"] == ""  # decrypt("") == ""
        assert "kite_login_url" in blob
        assert "kite_twofa_url" in blob


# ===========================================================================
# 13. collect_recent_fo_expiry_dates (mocked session) + marker edge case
# ===========================================================================

class TestCollectRecentFoExpiryDates:
    @pytest.mark.asyncio
    async def test_extracts_expiry_pairs_from_daily_book_rows(self):
        class _Result:
            def all(self):
                return [("NFO", "NIFTY25DEC25000CE"), ("MCX", "GOLDM26DECFUT")]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_Result())

        out = await mod.collect_recent_fo_expiry_dates(session, as_of=date(2026, 11, 24))
        assert len(out) == 2, f"expected 2 expiry pairs, got {out}"
        exchanges = {e for e, _ in out}
        assert exchanges == {"NFO", "MCX"}

    @pytest.mark.asyncio
    async def test_rows_with_no_parsable_expiry_are_skipped(self):
        class _Result:
            def all(self):
                return [("NSE", "RELIANCE")]  # equity — not F&O-shaped

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_Result())

        out = await mod.collect_recent_fo_expiry_dates(session, as_of=date(2026, 11, 24))
        assert out == set()


class TestLoadLastSyncedThroughEdgeCase:
    @pytest.mark.asyncio
    async def test_invalid_stored_date_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_string",
            lambda key, default="": "not-a-date",
        )
        assert await mod.load_last_synced_through() is None


# ===========================================================================
# 14. build_console_connection / shutdown_console_browser
# ===========================================================================

def _fake_secrets_blob(account: str = "ZBUILD1") -> dict:
    return {
        "kite_accounts": {
            account: {
                "password": "", "api_key": "", "api_secret": "",
                "totp_token": "JBSWY3DPEHPK3PXP", "source_ip": None,
            }
        },
        "kite_login_url": "http://example.invalid/login",
        "kite_twofa_url": "http://example.invalid/twofa",
    }


class TestBuildConsoleConnectionAndShutdown:
    def test_build_console_connection_mixes_in_the_override(self):
        conn = mod.build_console_connection("ZBUILD1", _fake_secrets_blob("ZBUILD1"))
        assert isinstance(conn, mod.KiteConsoleConnection)
        assert conn._console_page is None
        assert conn._console_browser is None

    def test_shutdown_closes_browser_context_and_stops_playwright(self):
        conn = types.SimpleNamespace(
            _console_context=MagicMock(), _console_browser=MagicMock(),
            _console_playwright=MagicMock(),
        )
        mod.shutdown_console_browser(conn)
        conn._console_context.close.assert_called_once()
        conn._console_browser.close.assert_called_once()
        conn._console_playwright.stop.assert_called_once()

    def test_shutdown_never_raises_even_if_close_fails(self):
        conn = types.SimpleNamespace(
            _console_context=MagicMock(close=MagicMock(side_effect=RuntimeError("x"))),
            _console_browser=None, _console_playwright=None,
        )
        mod.shutdown_console_browser(conn)  # must not raise

    def test_shutdown_is_a_no_op_on_a_connection_that_never_logged_in(self):
        conn = types.SimpleNamespace(
            _console_context=None, _console_browser=None, _console_playwright=None,
        )
        mod.shutdown_console_browser(conn)  # must not raise


# ===========================================================================
# 15. main() CLI entrypoint
# ===========================================================================

class TestMainEntrypoint:
    @pytest.mark.asyncio
    async def test_dry_run_backfill_prints_the_plan_and_returns_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mod, "load_active_kite_accounts",
            AsyncMock(return_value=[_fake_broker_account_row("ZDRY1")]),
        )
        rc = await mod.main(["--mode", "backfill", "--dry-run"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "backfill"
        assert set(payload["plan"].keys()) == {"equity", "derivatives", "commodities"}

    @pytest.mark.asyncio
    async def test_dry_run_sync_mode_prints_nothing_when_no_trigger_today(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mod, "load_active_kite_accounts",
            AsyncMock(return_value=[_fake_broker_account_row("ZDRY2")]),
        )
        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        monkeypatch.setattr(mod, "collect_recent_fo_expiry_dates", AsyncMock(return_value=set()))
        monkeypatch.setattr(mod, "compute_trigger_dates", AsyncMock(return_value=set()))
        monkeypatch.setattr(mod, "load_last_synced_through", AsyncMock(return_value=None))

        rc = await mod.main(["--mode", "sync", "--dry-run"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "", "no trigger today — nothing should print"

    @pytest.mark.asyncio
    async def test_dry_run_sync_mode_prints_derivatives_and_commodities_only_when_triggered(
        self, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            mod, "load_active_kite_accounts",
            AsyncMock(return_value=[_fake_broker_account_row("ZDRY3")]),
        )
        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        monkeypatch.setattr(
            mod, "collect_recent_fo_expiry_dates",
            AsyncMock(return_value={("NFO", date(2026, 11, 20))}),
        )
        monkeypatch.setattr(mod, "compute_trigger_dates", AsyncMock(return_value={date(2026, 11, 24)}))
        monkeypatch.setattr(mod, "load_last_synced_through", AsyncMock(return_value=None))
        import backend.shared.helpers.date_time_utils as dtu
        monkeypatch.setattr(
            dtu, "timestamp_indian",
            lambda: datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
        )

        rc = await mod.main(["--mode", "sync", "--dry-run"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "sync"
        assert "equity" not in payload["plan"], "ongoing sync must never include equity"

    @pytest.mark.asyncio
    async def test_no_active_accounts_returns_zero_without_scraping(self, monkeypatch):
        monkeypatch.setattr(mod, "load_active_kite_accounts", AsyncMock(return_value=[]))
        sync_mock = AsyncMock()
        monkeypatch.setattr(mod, "sync_account", sync_mock)

        rc = await mod.main(["--mode", "backfill"])

        assert rc == 0
        sync_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_account_unexpected_crash_does_not_abort_the_batch(self, monkeypatch):
        rows = [_fake_broker_account_row("ZCRASH1"), _fake_broker_account_row("ZCRASH2")]
        monkeypatch.setattr(mod, "load_active_kite_accounts", AsyncMock(return_value=rows))

        attempted: "list[str]" = []

        async def _fake_sync_account(row, *, plan, today=None):
            attempted.append(row.account)
            if row.account == "ZCRASH1":
                raise RuntimeError("unexpected crash")
            return mod.SyncOutcome(account="Z2####", ok=True)

        monkeypatch.setattr(mod, "sync_account", _fake_sync_account)
        monkeypatch.setattr(mod, "alert_critical", AsyncMock())

        rc = await mod.main(["--mode", "backfill"])

        assert attempted == ["ZCRASH1", "ZCRASH2"], (
            f"both accounts must be attempted even though the first crashed, got {attempted}"
        )
        assert rc == 1

    @pytest.mark.asyncio
    async def test_successful_sync_mode_run_marks_synced_through(self, monkeypatch):
        rows = [_fake_broker_account_row("ZMARK1")]
        monkeypatch.setattr(mod, "load_active_kite_accounts", AsyncMock(return_value=rows))
        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        monkeypatch.setattr(
            mod, "collect_recent_fo_expiry_dates",
            AsyncMock(return_value={("NFO", date(2026, 11, 20))}),
        )
        monkeypatch.setattr(mod, "compute_trigger_dates", AsyncMock(return_value={date(2026, 11, 24)}))
        monkeypatch.setattr(mod, "load_last_synced_through", AsyncMock(return_value=None))
        import backend.shared.helpers.date_time_utils as dtu
        monkeypatch.setattr(
            dtu, "timestamp_indian",
            lambda: datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            mod, "sync_account",
            AsyncMock(return_value=mod.SyncOutcome(account="Z1####", ok=True)),
        )
        mark_mock = AsyncMock()
        monkeypatch.setattr(mod, "mark_synced_through", mark_mock)

        rc = await mod.main(["--mode", "sync"])

        assert rc == 0
        mark_mock.assert_awaited_once_with(date(2026, 11, 24))

    @pytest.mark.asyncio
    async def test_failed_run_does_not_mark_synced_through(self, monkeypatch):
        rows = [_fake_broker_account_row("ZMARK2")]
        monkeypatch.setattr(mod, "load_active_kite_accounts", AsyncMock(return_value=rows))
        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        monkeypatch.setattr(
            mod, "collect_recent_fo_expiry_dates",
            AsyncMock(return_value={("NFO", date(2026, 11, 20))}),
        )
        monkeypatch.setattr(mod, "compute_trigger_dates", AsyncMock(return_value={date(2026, 11, 24)}))
        monkeypatch.setattr(mod, "load_last_synced_through", AsyncMock(return_value=None))
        import backend.shared.helpers.date_time_utils as dtu
        monkeypatch.setattr(
            dtu, "timestamp_indian",
            lambda: datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            mod, "sync_account",
            AsyncMock(return_value=mod.SyncOutcome(account="Z1####", ok=False, reason="login_failed")),
        )
        mark_mock = AsyncMock()
        monkeypatch.setattr(mod, "mark_synced_through", mark_mock)

        rc = await mod.main(["--mode", "sync"])

        assert rc == 1
        mark_mock.assert_not_called()


# ===========================================================================
# 15. pnl_pull_range / sync_plan() — REMOVED, 2026-11-26. Both report
# types now pull `plan`'s own range (see `sync_account()`), chunked by
# `chunk_by_calendar_month()` — there is no separate fixed-anchor pnl
# range any more, and the ongoing-sync plan comes from
# `resolve_sync_plan()` (see `TestResolveSyncPlan` above), not a
# generic trailing lookback window. See `TestChunkByCalendarMonth` /
# `TestResolveSyncPlan` for this area's current coverage.
# ===========================================================================


# ===========================================================================
# 16. classify_instrument — future / option / equity, reusing
# parse_tradingsymbol(), never endswith('CE'/'PE')
# ===========================================================================

class TestClassifyInstrument:
    def test_monthly_future_nfo(self):
        itype, root = mod.classify_instrument("NIFTY25DECFUT")
        assert itype == "future"
        assert root == "NIFTY"

    def test_monthly_future_mcx(self):
        itype, root = mod.classify_instrument("GOLDM26DECFUT")
        assert itype == "future"
        assert root == "GOLDM"

    def test_monthly_option_call(self):
        itype, root = mod.classify_instrument("NIFTY25DEC25000CE")
        assert itype == "option"
        assert root == "NIFTY"

    def test_monthly_option_put_mcx_commodity(self):
        itype, root = mod.classify_instrument("CRUDEOIL26DEC6500PE")
        assert itype == "option"
        assert root == "CRUDEOIL"

    def test_weekly_option(self):
        itype, root = mod.classify_instrument("NIFTY2512424000CE")
        assert itype == "option"
        assert root == "NIFTY"

    def test_plain_equity_symbol_is_equity(self):
        itype, root = mod.classify_instrument("INFY")
        assert itype == "equity"
        assert root == "INFY"

    def test_reliance_never_misclassified_as_option(self):
        """The exact defect class this plan's own CLAUDE.md entry
        documents as already fixed elsewhere (derivativesMath.js's
        isFOSymbol() fix) — RELIANCE ends in neither CE nor PE as a
        literal 2-char suffix match would wrongly think, but a correct
        parser must never be fooled by it either."""
        itype, root = mod.classify_instrument("RELIANCE")
        assert itype == "equity", f"RELIANCE must never be classified as an option, got {itype}"
        assert root == "RELIANCE"

    def test_bajfinance_never_misclassified_as_option(self):
        itype, root = mod.classify_instrument("BAJFINANCE")
        assert itype == "equity", f"BAJFINANCE must never be classified as an option, got {itype}"

    def test_empty_symbol_degrades_to_equity_with_empty_root(self):
        itype, root = mod.classify_instrument("")
        assert itype == "equity"
        assert root == ""

    def test_none_symbol_degrades_to_equity(self):
        itype, root = mod.classify_instrument(None)
        assert itype == "equity"


# ===========================================================================
# 17. build_sync_row — instrument_type/symbol_root wiring + realized_pnl
# only populated for report_type='pnl'
# ===========================================================================

class TestBuildSyncRowInstrumentAndPnl:
    def test_tradebook_row_gets_instrument_type_and_root_but_no_realized_pnl(self):
        row = _one_tradebook_row(symbol="NIFTY25DEC25000CE")
        built = mod.build_sync_row("Z1####", "derivatives", "tradebook", row)
        assert built["instrument_type"] == "option"
        assert built["symbol_root"] == "NIFTY"
        assert built["realized_pnl"] is None

    def test_pnl_row_populates_realized_pnl(self):
        row = _one_pnl_row(realized_pnl="88888.88")
        built = mod.build_sync_row("Z1####", "derivatives", "pnl", row,
                                     pull_start=date(2026, 3, 1), pull_end=date(2026, 11, 20))
        assert built["realized_pnl"] == pytest.approx(88888.88)
        assert built["instrument_type"] == "future"
        assert built["symbol_root"] == "GOLDM"

    def test_pnl_row_never_populates_buy_sell_fields(self):
        row = _one_pnl_row()
        built = mod.build_sync_row("Z1####", "derivatives", "pnl", row)
        assert built["buy_qty"] is None
        assert built["sell_qty"] is None
        assert built["buy_value"] is None
        assert built["sell_value"] is None

    def test_equity_tradebook_row_classified_equity(self):
        row = _one_tradebook_row(symbol="RELIANCE", exchange="NSE")
        built = mod.build_sync_row("Z1####", "equity", "tradebook", row)
        assert built["instrument_type"] == "equity"
        assert built["symbol_root"] == "RELIANCE"


# ===========================================================================
# 18. P&L-statement pull — mocked-page test mirroring the Tradebook one
# ===========================================================================

class TestPnlStatementPull:
    @pytest.mark.asyncio
    async def test_pnl_report_type_is_pulled_and_ingested_with_realized_pnl(self, monkeypatch, tmp_path):
        conn = _console_conn_bare("ZCONSOLE_PNL1")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        pnl_file = tmp_path / "pnl.xlsx"
        pnl_file.write_bytes(_pnl_xlsx_bytes([
            _one_pnl_row(symbol="GOLDM26DECFUT", realized_pnl="45000.00"),
        ]))

        navigated_report_types: "list[str]" = []
        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(
            side_effect=lambda rt: navigated_report_types.append(rt)
        )
        fake_page.select_segment = MagicMock()
        fake_page.set_date_range = MagicMock()
        fake_page.trigger_download = MagicMock(return_value=pnl_file)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)

        ingest_mock = AsyncMock(return_value=1)
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)
        monkeypatch.setattr("backend.api.database.async_session", lambda: _FakeSessionCtx())

        row = _fake_broker_account_row("ZCONSOLE_PNL1")
        await mod.sync_account(
            row, plan={"commodities": (date(2026, 11, 1), date(2026, 11, 30))},
        )

        assert "pnl" in navigated_report_types, (
            "the pnl report type must actually be navigated to, not only tradebook"
        )
        pnl_call = next(c for c in ingest_mock.call_args_list if c[0][3] == "pnl")
        pnl_rows = pnl_call[0][4]
        # The real XLSX export's P&L cells are genuinely numeric, not
        # text (confirmed live, 2026-11-26) -- parse_pnl_statement_xlsx
        # returns the native float, not a string.
        assert pnl_rows[0]["realized_pnl"] == 45000.0

    @pytest.mark.asyncio
    async def test_pnl_chunk_start_is_floored_to_month_1_tradebook_is_not(self, monkeypatch, tmp_path):
        """THE important wiring test for the backfill/sync seam fix —
        when the plan's own range does NOT start on the 1st (the
        ongoing-sync case: previous_expiry + 1), the PNL request must
        still be floored to the 1st of that month, while the tradebook
        request keeps the real, clipped start. Confirms the request
        sent to `set_date_range()` AND the stored `pull_start` agree."""
        conn = _console_conn_bare("ZCONSOLE_PNLFLOOR1")
        conn._attempt_login_with_cooloff = MagicMock()
        conn._console_page = MagicMock()
        monkeypatch.setattr(mod, "build_console_connection", lambda *a, **k: conn)

        tradebook_file = tmp_path / "tradebook.csv"
        tradebook_file.write_bytes(_csv_bytes([]))
        pnl_file = tmp_path / "pnl.xlsx"
        pnl_file.write_bytes(_pnl_xlsx_bytes([]))
        ledger_file = tmp_path / "ledger.csv"
        ledger_file.write_bytes(_ledger_csv_bytes())  # header-only, 0 rows

        last_report_type: "list[str]" = []
        set_date_range_calls: "list[tuple]" = []

        def _fake_trigger_download(report_type=None, start=None, end=None, masked_account=None):
            if last_report_type[-1] == "tradebook":
                return tradebook_file
            if last_report_type[-1] == "ledger":
                return ledger_file
            return pnl_file

        fake_page = MagicMock()
        fake_page.navigate_to_reports = MagicMock(side_effect=lambda rt: last_report_type.append(rt))
        fake_page.select_segment = MagicMock()
        fake_page.set_date_range = MagicMock(
            side_effect=lambda s, e: set_date_range_calls.append((last_report_type[-1], s, e))
        )
        fake_page.set_ledger_date_range = MagicMock()
        fake_page.trigger_download = MagicMock(side_effect=_fake_trigger_download)
        monkeypatch.setattr(mod, "KiteConsoleReportsPage", lambda page: fake_page)

        ingest_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(mod, "ingest_rows", ingest_mock)
        monkeypatch.setattr(mod, "ingest_ledger_rows", AsyncMock(return_value=0))
        monkeypatch.setattr("backend.api.database.async_session", lambda: _FakeSessionCtx())

        row = _fake_broker_account_row("ZCONSOLE_PNLFLOOR1")
        # Mid-month start — the shape `resolve_sync_plan()` produces
        # (previous_expiry + 1 day), never the 1st of a month.
        await mod.sync_account(
            row, plan={"commodities": (date(2026, 11, 15), date(2026, 11, 20))},
        )

        tradebook_calls = [c for c in set_date_range_calls if c[0] == "tradebook"]
        pnl_calls = [c for c in set_date_range_calls if c[0] == "pnl"]
        assert tradebook_calls == [("tradebook", date(2026, 11, 15), date(2026, 11, 20))], (
            "tradebook keeps the real, clipped start — no flooring"
        )
        assert pnl_calls == [("pnl", date(2026, 11, 1), date(2026, 11, 20))], (
            "pnl floors its own start to the 1st of the month"
        )
        # The stored pull_range must match what was actually requested.
        pnl_ingest_call = next(c for c in ingest_mock.call_args_list if c[0][3] == "pnl")
        assert pnl_ingest_call[1]["pull_start"] == date(2026, 11, 1)
        assert pnl_ingest_call[1]["pull_end"] == date(2026, 11, 20)


# ===========================================================================
# 19. _migrate_kite_console_pnl_sync_columns — idempotent, additive
# ===========================================================================

class TestKiteConsolePnlSyncColumnsMigration:
    def test_migration_emits_idempotent_add_column_if_not_exists(self):
        """The real migration runs against Postgres in production (the
        table itself already uses JSONB, a Postgres-only type, so this
        migration is exercised at Postgres-dialect-compilation level,
        matching this codebase's own convention for Postgres-only
        migrations)."""
        import inspect
        from backend.api.database import _migrate_kite_console_pnl_sync_columns

        src = inspect.getsource(_migrate_kite_console_pnl_sync_columns)
        assert "ADD COLUMN IF NOT EXISTS" in src
        assert "instrument_type" in src
        assert "symbol_root" in src
        assert "CREATE INDEX IF NOT EXISTS" in src

    @pytest.mark.asyncio
    async def test_migration_is_registered_in_init_db_call_order(self):
        """Confirms the call is actually wired into init_db()'s migration
        list, not just defined and forgotten."""
        import inspect
        from backend.api import database as db_mod

        src = inspect.getsource(db_mod.init_db)
        assert "_migrate_kite_console_pnl_sync_columns(conn)" in src

    @pytest.mark.asyncio
    async def test_migration_runs_twice_without_error_on_sqlite(self):
        """SQLite's ALTER TABLE ADD COLUMN doesn't support IF NOT EXISTS,
        so running the REAL function against SQLite would fail on the
        second call — this test instead confirms the idempotent SHAPE by
        running the equivalent bare ADD COLUMN once (SQLite has no
        IF NOT EXISTS clause to test against) and confirming a second
        identical ADD COLUMN raises the expected 'duplicate column'
        error from SQLite itself — i.e. proves the IF NOT EXISTS clause
        in the real Postgres-targeted SQL is doing real, necessary work,
        not a no-op precaution."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE t (id INTEGER PRIMARY KEY)"
            ))
            await conn.execute(text("ALTER TABLE t ADD COLUMN instrument_type VARCHAR(16)"))
        with pytest.raises(Exception):
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE t ADD COLUMN instrument_type VARCHAR(16)"))
        await engine.dispose()


# ===========================================================================
# 20. get_accumulated_pnl / reconcile_against_daily_book — real in-memory
# SQLite DB, exercising the real window-function query (not Postgres-only
# pg_insert, so this runs fine under SQLite per this codebase's own
# documented precedent for aggregation-only queries, e.g. test_pnl_range.py)
# ===========================================================================

import pytest_asyncio as _pytest_asyncio  # noqa: E402
from sqlalchemy import (  # noqa: E402
    JSON, Column, Date, DateTime, Integer, MetaData, Numeric, String, Table,
    UniqueConstraint, text as _sqltext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

_RECON_DB_URL = "sqlite+aiosqlite:///:memory:"


@_pytest_asyncio.fixture
async def recon_engine():
    meta = MetaData()
    Table(
        "kite_console_pnl_sync", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("account", String(16), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("report_type", String(16), nullable=False),
        Column("dedup_key", String(256), nullable=False),
        Column("symbol", String(64), nullable=True),
        Column("exchange", String(16), nullable=True),
        Column("instrument_type", String(16), nullable=True),
        Column("symbol_root", String(64), nullable=True),
        Column("expiry", Date, nullable=True),
        Column("trade_date", Date, nullable=True),
        Column("trade_id", String(64), nullable=True),
        Column("order_id", String(64), nullable=True),
        Column("pull_range_start", Date, nullable=True),
        Column("pull_range_end", Date, nullable=True),
        Column("buy_qty", Numeric, nullable=True),
        Column("sell_qty", Numeric, nullable=True),
        Column("buy_value", Numeric, nullable=True),
        Column("sell_value", Numeric, nullable=True),
        Column("realized_pnl", Numeric, nullable=True),
        Column("raw_row", JSON, nullable=False, default=dict),
        Column("synced_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)),
        UniqueConstraint("account", "report_type", "dedup_key",
                          name="uq_kite_console_pnl_sync_account_type_key"),
    )
    Table(
        "daily_book", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False),
        Column("account", String(32), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("kind", String(16), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("exchange", String(16), nullable=True),
        Column("qty", Integer, nullable=False, default=0),
        Column("avg_cost", Numeric, nullable=True),
        Column("ltp", Numeric, nullable=True),
        Column("day_pnl", Numeric, nullable=True),
        Column("total_pnl", Numeric, nullable=True),
        Column("payload_json", String, nullable=True),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                          name="uq_daily_book_day_acct_kind_sym"),
    )
    engine = create_async_engine(_RECON_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
    yield engine
    await engine.dispose()


@_pytest_asyncio.fixture
async def recon_session_factory(recon_engine, monkeypatch):
    factory = async_sessionmaker(recon_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.api.database.async_session", factory)
    return factory


_PNL_INSERT_SQL = _sqltext("""
    INSERT INTO kite_console_pnl_sync
        (account, segment, report_type, dedup_key, symbol, exchange,
         instrument_type, symbol_root, expiry, pull_range_start,
         pull_range_end, realized_pnl, raw_row, updated_at)
    VALUES
        (:account, :segment, :report_type, :dedup_key, :symbol, :exchange,
         :instrument_type, :symbol_root, :expiry, :pull_range_start,
         :pull_range_end, :realized_pnl, :raw_row, :updated_at)
""")

_DAILY_BOOK_INSERT_SQL = _sqltext("""
    INSERT INTO daily_book
        (date, account, segment, kind, symbol, exchange, qty, total_pnl, captured_at)
    VALUES
        (:date, :account, :segment, :kind, :symbol, :exchange, :qty, :total_pnl, :captured_at)
""")


# Register the real account roster once, up front, and use the REAL
# mask_account() output everywhere below — never a hand-typed "Z1####"
# literal. With exactly one Zerodha account registered, register_accounts()
# preserves the natural prefix (ZG0790 -> ZG####, per its own docstring),
# NOT the ordinal form — computing it for real avoids baking in a guess
# about which of mask_account's/register_accounts's own (contradictory)
# docstring examples is the actual behaviour.
from backend.shared.helpers.utils import mask_account as _recon_mask_account  # noqa: E402
from backend.shared.helpers.utils import register_accounts as _recon_register_accounts  # noqa: E402

_recon_register_accounts(["ZG0790"])
_RECON_MASKED_ACCOUNT = _recon_mask_account("ZG0790")


async def _insert_pnl_row(session, **kwargs):
    defaults = dict(
        account=_RECON_MASKED_ACCOUNT, segment="commodities", report_type="pnl",
        symbol="GOLDM26DECFUT", exchange="MCX", instrument_type="future",
        symbol_root="GOLDM", expiry=date(2026, 12, 31),
        pull_range_start=date(2026, 3, 1), pull_range_end=date(2026, 6, 1),
        realized_pnl=0.0, raw_row="{}",
        updated_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    defaults["dedup_key"] = (
        f"{defaults['symbol']}:{defaults['expiry']}:"
        f"{defaults['pull_range_start']}:{defaults['pull_range_end']}"
    )
    await session.execute(_PNL_INSERT_SQL, defaults)


async def _insert_daily_book_row(session, **kwargs):
    defaults = dict(
        date=date(2026, 6, 1), account="ZG0790", segment="commodity",
        kind="positions", symbol="GOLDM26DECFUT", exchange="MCX", qty=0,
        total_pnl=0.0, captured_at=datetime(2026, 6, 1, 15, 30, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    await session.execute(_DAILY_BOOK_INSERT_SQL, defaults)


class TestGetAccumulatedPnlCalendarMonthChunking:
    """2026-11-26 redesign — real-world constraint discovered before any
    real data existed in this table (confirmed 0 rows in prod at the
    time): Kite Console's Tradebook/P&L-statement CSV export does not
    reliably support a date range wider than ~1 month, so every real
    PNL pull now floors its own requested start to the 1st of its
    calendar month (`sync_account()`) and is chunked via
    `chunk_by_calendar_month()`. `get_accumulated_pnl()` picks, per
    (account, segment, symbol, expiry, `pull_range_start`), only the
    row with the LATEST `pull_range_end` — then SUMS across the
    resulting per-month 'latest' rows. A bare SUM across every matching
    row (an earlier, simpler version of this redesign) double-counts
    both a same-month re-run and a backfill/sync seam — see the two
    dedicated tests below, which are confirmed to fail against that
    bare-SUM version."""

    @pytest.mark.asyncio
    async def test_two_distinct_calendar_months_correctly_sum(self, recon_session_factory):
        async with recon_session_factory() as session:
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 10, 1), pull_range_end=date(2026, 10, 31),
                realized_pnl=50000.0,
            )
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 11, 1), pull_range_end=date(2026, 11, 26),
                realized_pnl=70000.0,
            )
            await session.commit()

        result = await mod.get_accumulated_pnl(symbol_root="GOLDM")

        assert result["overall_total"] == pytest.approx(120000.0), (
            "two DISTINCT, non-overlapping calendar MONTHS for the same "
            "symbol+expiry must SUM, not pick one"
        )

    @pytest.mark.asyncio
    async def test_same_month_rerun_with_a_later_end_picks_latest_not_sum(self, recon_session_factory):
        """THE important same-month case — a retried/re-triggered pull
        for a month already pulled gets a DIFFERENT `pull_range_end`
        (today's date moved), hence a genuinely different `dedup_key`,
        hence a SECOND row rather than an upsert in place. A bare SUM
        would wrongly add both; the real rule picks only the latest
        (widest) one per month bucket."""
        async with recon_session_factory() as session:
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 10, 1), pull_range_end=date(2026, 10, 20),
                realized_pnl=50000.0,
                updated_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
            )
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 10, 1), pull_range_end=date(2026, 10, 25),
                realized_pnl=52000.0,
                updated_at=datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc),
            )
            await session.commit()

        result = await mod.get_accumulated_pnl(symbol_root="GOLDM")

        assert result["overall_total"] == pytest.approx(52000.0), (
            f"expected the LATEST pull's value for this month (52000), "
            f"got {result['overall_total']} — a naive SUM would wrongly give 102000"
        )

    @pytest.mark.asyncio
    async def test_backfill_then_sync_seam_for_the_same_month_picks_latest_not_sum(self, recon_session_factory):
        """The FIRST ongoing sync after a one-time backfill can overlap
        the backfill's own last month — an expiry essentially never
        lands on a calendar-month boundary. Because `sync_account()`
        floors every PNL chunk's own start to the 1st of the month,
        BOTH the backfill's row and the sync's row for that shared
        month land in the SAME bucket (same `pull_range_start`), so
        this resolves the same way as a same-month re-run: latest
        `pull_range_end` wins, never summed."""
        async with recon_session_factory() as session:
            # Backfill's own chunk for November, as of the day it ran.
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 11, 1), pull_range_end=date(2026, 11, 20),
                realized_pnl=100000.0,
                updated_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
            )
            # The first ongoing sync's own chunk for the SAME month,
            # a few days later, now fully covering a later cycle.
            await _insert_pnl_row(
                session,
                pull_range_start=date(2026, 11, 1), pull_range_end=date(2026, 11, 25),
                realized_pnl=130000.0,
                updated_at=datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc),
            )
            await session.commit()

        result = await mod.get_accumulated_pnl(symbol_root="GOLDM")

        assert result["overall_total"] == pytest.approx(130000.0), (
            f"expected the sync row's later total (130000), got "
            f"{result['overall_total']} — a naive SUM would wrongly give 230000"
        )

    @pytest.mark.asyncio
    async def test_different_symbols_within_one_group_are_correctly_summed(self, recon_session_factory):
        """Summing across DIFFERENT symbols sharing one (account,
        symbol_root, expiry) group — e.g. two option strikes under the
        same underlying+expiry — was already correct before this
        redesign and remains so."""
        async with recon_session_factory() as session:
            await _insert_pnl_row(
                session, symbol="NIFTY25DEC25000CE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24), realized_pnl=10000.0,
            )
            await _insert_pnl_row(
                session, symbol="NIFTY25DEC25500PE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24), realized_pnl=-4000.0,
            )
            await session.commit()

        result = await mod.get_accumulated_pnl(symbol_root="NIFTY")
        assert result["overall_total"] == pytest.approx(6000.0)
        assert result["by_account_root_expiry"][(_RECON_MASKED_ACCOUNT, "NIFTY", date(2025, 12, 24))] == pytest.approx(6000.0)


class TestReconcileAgainstDailyBook:
    @pytest.mark.asyncio
    async def test_clean_match_no_discrepancy(self, recon_session_factory):
        async with recon_session_factory() as session:
            await _insert_pnl_row(session, realized_pnl=100000.0)
            await _insert_daily_book_row(session, total_pnl=100100.0)  # within tolerance
            await session.commit()

        summary = await mod.reconcile_against_daily_book(symbol_root="GOLDM")

        assert summary["discrepancy_count"] == 0
        assert summary["missing_count"] == 0
        key = (_RECON_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert summary["groups"][key]["status"] == "match"

    @pytest.mark.asyncio
    async def test_daily_book_missing_is_flagged_missing_not_disagree(self, recon_session_factory):
        async with recon_session_factory() as session:
            await _insert_pnl_row(session, realized_pnl=75000.0)
            await session.commit()
            # No daily_book row inserted at all for this group.

        summary = await mod.reconcile_against_daily_book(symbol_root="GOLDM")

        assert summary["missing_count"] == 1
        assert summary["disagree_count"] == 0
        key = (_RECON_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert summary["groups"][key]["status"] == "missing"
        assert summary["groups"][key]["daily_book_pnl"] is None

    @pytest.mark.asyncio
    async def test_genuine_mismatch_flagged_disagree_gold_recovery_shaped(self, recon_session_factory):
        """Real-world-shaped fixture per the task's own suggestion: a
        large disagreement in the GOLD-recovery direction (daily_book
        badly understated vs. the broker-authoritative ground truth)."""
        async with recon_session_factory() as session:
            await _insert_pnl_row(session, realized_pnl=367000.0)  # ground truth, corrected
            await _insert_daily_book_row(session, total_pnl=-444000.0)  # daily_book, understated
            await session.commit()

        summary = await mod.reconcile_against_daily_book(symbol_root="GOLDM")

        assert summary["disagree_count"] == 1
        assert summary["missing_count"] == 0
        key = (_RECON_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        group = summary["groups"][key]
        assert group["status"] == "disagree"
        assert group["delta"] == pytest.approx(367000.0 - (-444000.0))

    @pytest.mark.asyncio
    async def test_within_tolerance_small_delta_not_flagged(self, recon_session_factory):
        async with recon_session_factory() as session:
            await _insert_pnl_row(session, realized_pnl=100000.0)
            await _insert_daily_book_row(session, total_pnl=100300.0)  # ₹300 delta, within ₹500 floor
            await session.commit()

        summary = await mod.reconcile_against_daily_book(symbol_root="GOLDM")

        assert summary["discrepancy_count"] == 0
        key = (_RECON_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert summary["groups"][key]["status"] == "match"


# ===========================================================================
# 21. Part 3 — surfacing a reconciliation "disagree" finding through the
# standard log-then-dispatch alert pipeline. Never a direct send.
# ===========================================================================

class TestBuildReconciliationAlert:
    """Pure builder — `_build_reconciliation_alert()`."""

    def test_no_disagree_groups_returns_none(self):
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 100000.0, "daily_book_pnl": 100100.0,
                    "delta": -100.0, "status": "match",
                },
                ("Z1####", "SILVER", None): {
                    "ground_truth_pnl": 5000.0, "daily_book_pnl": None,
                    "delta": None, "status": "missing",
                },
            },
            "groups_compared": 2, "missing_count": 1, "disagree_count": 0,
        }
        assert mod._build_reconciliation_alert(summary) is None

    def test_empty_summary_returns_none(self):
        assert mod._build_reconciliation_alert({}) is None
        assert mod._build_reconciliation_alert({"groups": {}}) is None

    def test_only_disagree_groups_are_included(self):
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
                ("Z1####", "SILVER", date(2026, 12, 31)): {
                    "ground_truth_pnl": 5000.0, "daily_book_pnl": 5100.0,
                    "delta": -100.0, "status": "match",
                },
                ("Z2####", "NIFTY", None): {
                    "ground_truth_pnl": 2000.0, "daily_book_pnl": None,
                    "delta": None, "status": "missing",
                },
            },
            "groups_compared": 3, "missing_count": 1, "disagree_count": 1,
        }
        built = mod._build_reconciliation_alert(summary)
        assert built is not None
        title, body, extra = built
        assert "1 group(s) disagree" in title
        assert "GOLDM" in body
        assert "SILVER" not in body
        assert "NIFTY" not in body
        assert extra["disagree_count"] == 1
        assert len(extra["groups"]) == 1
        assert extra["groups"][0]["symbol_root"] == "GOLDM"
        assert extra["groups"][0]["expiry"] == "2026-12-31"
        assert extra["groups"][0]["account"] == "Z1####"

    def test_extra_is_json_safe(self):
        import json
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
            },
            "groups_compared": 1, "missing_count": 0, "disagree_count": 1,
        }
        _, _, extra = mod._build_reconciliation_alert(summary)
        json.dumps(extra)  # must not raise

    def test_null_expiry_disagree_group_renders_as_n_a(self):
        summary = {
            "groups": {
                ("Z1####", "RELIANCE", None): {
                    "ground_truth_pnl": 1000.0, "daily_book_pnl": 9000.0,
                    "delta": -8000.0, "status": "disagree",
                },
            },
            "groups_compared": 1, "missing_count": 0, "disagree_count": 1,
        }
        title, body, extra = mod._build_reconciliation_alert(summary)
        assert "n/a" in body
        assert extra["groups"][0]["expiry"] is None

    def test_sorted_by_abs_delta_descending_and_truncated_with_remainder_note(self):
        groups = {}
        for i in range(20):
            groups[(f"Z{i}####", f"ROOT{i}", date(2026, 12, 31))] = {
                "ground_truth_pnl": float(i * 1000), "daily_book_pnl": 0.0,
                "delta": float(i * 1000), "status": "disagree",
            }
        summary = {"groups": groups, "groups_compared": 20, "missing_count": 0, "disagree_count": 20}
        title, body, extra = mod._build_reconciliation_alert(summary)
        assert len(extra["groups"]) == 20, "full detail always survives in extra, even truncated"
        assert "ROOT19" in body, "the largest |delta| is listed first"
        assert f"+{20 - mod._RECONCILE_ALERT_TOP_N} more" in body


class TestAlertPnlReconciliationDisagreement:
    @pytest.mark.asyncio
    async def test_no_disagree_never_logs_or_flushes(self, monkeypatch):
        summary = {"groups": {}, "groups_compared": 0, "missing_count": 0, "disagree_count": 0}
        log_mock = MagicMock()
        monkeypatch.setattr(mod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(mod, "_flush_and_dispatch_once", flush_mock)

        await mod.alert_pnl_reconciliation_disagreement(summary)

        log_mock.assert_not_called()
        flush_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_match_and_missing_only_never_logs(self, monkeypatch):
        """THE important test — a mix of match/missing groups (no
        disagree) must never fire the alert, confirming the gate is
        genuinely `disagree_count > 0`, not merely `discrepancy_count`
        (which also counts 'missing')."""
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 100000.0, "daily_book_pnl": 100100.0,
                    "delta": -100.0, "status": "match",
                },
                ("Z1####", "SILVER", None): {
                    "ground_truth_pnl": 5000.0, "daily_book_pnl": None,
                    "delta": None, "status": "missing",
                },
            },
            "groups_compared": 2, "missing_count": 1, "disagree_count": 0,
        }
        log_mock = MagicMock()
        monkeypatch.setattr(mod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(mod, "_flush_and_dispatch_once", flush_mock)

        await mod.alert_pnl_reconciliation_disagreement(summary)

        log_mock.assert_not_called()
        flush_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_disagree_group_logs_exactly_one_tagged_record_and_flushes(self, monkeypatch):
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
                ("Z1####", "SILVER", date(2026, 12, 31)): {
                    "ground_truth_pnl": 5000.0, "daily_book_pnl": 5100.0,
                    "delta": -100.0, "status": "match",
                },
            },
            "groups_compared": 2, "missing_count": 0, "disagree_count": 1,
        }
        log_mock = MagicMock()
        monkeypatch.setattr(mod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(mod, "_flush_and_dispatch_once", flush_mock)

        await mod.alert_pnl_reconciliation_disagreement(summary)

        log_mock.assert_called_once()
        _, kwargs = log_mock.call_args
        extra = kwargs["extra"]
        assert extra["alert_event"] == "pnl_reconciliation_disagreement"
        assert extra["tags"] == ["alert"]
        assert extra["source_type"] == "process"
        assert extra["source_name"] == "Kite Console P&L sync"
        assert "GOLDM" in extra["body"]
        assert "SILVER" not in extra["body"], "only the disagreeing group is listed"
        flush_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logged_at_warning_never_error_or_above(self, monkeypatch):
        """WARNING, not ERROR+ — an ERROR+/CRITICAL record is
        auto-tagged "error" by `tags_for()` and would double-fire
        through `ERROR_AGENT`'s `min_level='ERROR'` catch-all alongside
        the dedicated `pnl-reconciliation-alert` agent this record is
        meant for."""
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
            },
            "groups_compared": 1, "missing_count": 0, "disagree_count": 1,
        }
        warning_mock = MagicMock()
        critical_mock = MagicMock()
        error_mock = MagicMock()
        monkeypatch.setattr(mod.logger, "warning", warning_mock)
        monkeypatch.setattr(mod.logger, "critical", critical_mock)
        monkeypatch.setattr(mod.logger, "error", error_mock)
        monkeypatch.setattr(mod, "_flush_and_dispatch_once", AsyncMock())

        await mod.alert_pnl_reconciliation_disagreement(summary)

        warning_mock.assert_called_once()
        critical_mock.assert_not_called()
        error_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_the_real_flush_and_dispatch_once_function(self, monkeypatch):
        """Confirms the WIRING, not just that SOME flush function was
        called — `alert_pnl_reconciliation_disagreement()` must call the
        real, unmocked `_flush_and_dispatch_once()`, which in turn calls
        `log_store.flush_once` with the SAME `HANDLER` singleton and
        `event_agents.dispatch_rows` — matching `alert_critical()`'s own
        already-shipped pattern (one real flush+dispatch cycle before a
        standalone, short-lived process exits). The queue is seeded
        directly (same technique as
        `test_flush_and_dispatch_once_calls_the_real_flush_once` above)
        so `HANDLER.pending()` is already True and the wait loop exits
        on its first check, regardless of the real `logger.warning()`
        call this function also makes."""
        summary = {
            "groups": {
                ("Z1####", "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
            },
            "groups_compared": 1, "missing_count": 0, "disagree_count": 1,
        }
        from backend.shared.helpers import log_store
        from backend.api.algo.event_agents import dispatch_rows
        flush_once_mock = AsyncMock(return_value=0)
        monkeypatch.setattr(log_store, "flush_once", flush_once_mock)
        log_store.HANDLER._q.put_nowait({"fake": "row"})  # make pending() True immediately

        await mod.alert_pnl_reconciliation_disagreement(summary)

        flush_once_mock.assert_awaited_once()
        args, kwargs = flush_once_mock.call_args
        assert args[0] is log_store.HANDLER
        assert kwargs.get("dispatch") is dispatch_rows
