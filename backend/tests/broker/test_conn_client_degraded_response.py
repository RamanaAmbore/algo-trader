"""Regression coverage for the conn-service → sync-client failure boundary.

Bug (A1, 2026-09 plan "Fix 0-instead-of-last-known-good"): conn_service's
/internal/holdings|positions|margins handlers (backend/brokers/service/
routes.py) catch every exception raised during the per-account fetch loop
and return HTTP 200 with `accounts: [], errors: [...]`. Before this fix,
backend.brokers.client.sync._fetch_per_account never read `payload.errors`
and treated `accounts: []` as a genuine empty result — no `fetch_failed`
sentinel was ever attached, so a real broker-layer exception rendered as
an empty-but-"successful" fetch. Downstream (NavStrip, Payoff chart) then
displayed 0 instead of freezing on the last-known-good value.

Fix: `_fetch_per_account` now treats a non-empty `payload.errors` list as
a degraded response and returns the same `fetch_failed` sentinel shape
already used on transport/HTTP failure — `[pd.DataFrame()]` with
`attrs["fetch_failed"] = True`.

These tests reproduce the exact scenario end-to-end (conn_service's real
route handler raising internally → sync client decoding its response) and
guard the negative case (a genuinely empty book must NOT be marked failed,
or the 08:00 daily-rollover / "operator closed everything" state would be
permanently stuck showing "degraded").
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import msgspec
import pandas as pd
import pytest

from backend.brokers.client import sync as conn_sync
from backend.brokers.service.routes import (
    InternalBrokerController,
    _df_list_to_per_account,
)
from backend.brokers.service.schemas import InternalAccountEnvelope, InternalPerAccountResp


def _mock_resp(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    return resp


class TestSyncClientDegradedResponse:
    """`payload.errors` non-empty ⇒ fetch_failed sentinel, not a silent []."""

    @pytest.mark.parametrize("fetch_fn_name,path", [
        ("fetch_holdings", "/internal/holdings"),
        ("fetch_positions", "/internal/positions"),
        ("fetch_margins", "/internal/margins"),
    ])
    def test_degraded_empty_200_becomes_fetch_failed(self, fetch_fn_name, path):
        """conn_service's internal exception path (200, accounts=[],
        errors=[...]) must surface as a fetch_failed sentinel, matching
        the existing transport-error convention — never a silent []."""
        payload = InternalPerAccountResp(accounts=[], errors=["boom: connection reset"])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            fn = getattr(conn_sync, fetch_fn_name)
            result = fn()

        assert result != [], (
            f"{fetch_fn_name}: a degraded 200 response must never silently "
            f"collapse to an empty list — downstream treats [] as a "
            f"confirmed-empty book, not an outage"
        )
        assert len(result) == 1
        assert isinstance(result[0], pd.DataFrame)
        assert result[0].attrs.get("fetch_failed") is True, (
            f"{fetch_fn_name}: fetch_failed sentinel must be attached when "
            f"conn_service reports errors"
        )

    def test_genuinely_empty_book_is_not_marked_failed(self):
        """The negative case: accounts=[] with NO errors (e.g. zero broker
        accounts configured on this box, or a legitimately empty response
        entry) must NOT be treated as a failure — this is what lets the
        08:00 daily-rollover / 'operator closed everything' states still
        show a real 0 instead of being stuck permanently 'degraded'."""
        payload = InternalPerAccountResp(accounts=[], errors=[])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_positions()

        assert result == [], "zero configured accounts + no errors is a genuine empty result"

    def test_genuinely_empty_account_book_is_not_marked_failed(self):
        """A real account with a real empty book (ok=True, rows=[]) and no
        top-level errors must round-trip as a normal empty-but-healthy
        frame — not fetch_failed."""
        payload = InternalPerAccountResp(
            accounts=[InternalAccountEnvelope(account="ZG0790", ok=True, rows=[])],
            errors=[],
        )
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_holdings()

        assert len(result) == 1
        assert result[0].attrs.get("fetch_failed") is not True

    def test_partial_failure_still_marks_only_the_failing_account(self):
        """ok=False on one envelope entry (per-account failure, NOT the
        whole-batch exception path) must still mark only that frame —
        unrelated to the errors-list check, this is the pre-existing
        per-entry `ok` contract and must keep working unchanged."""
        payload = InternalPerAccountResp(
            accounts=[
                InternalAccountEnvelope(account="ZG0790", ok=True, rows=[{"tradingsymbol": "RELIANCE"}]),
                InternalAccountEnvelope(account="DH6847", ok=False, rows=[]),
            ],
            errors=[],
        )
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_positions()

        assert len(result) == 2
        assert result[0].attrs.get("fetch_failed") is not True
        assert result[1].attrs.get("fetch_failed") is True


class TestConnServiceRouteToClientEndToEnd:
    """Full repro of the brief's exact scenario: conn_service's real route
    handler raises internally → produces the real wire bytes → sync client
    decodes them. Confirms the fix at the actual process boundary, not
    just against a hand-built payload."""

    @pytest.mark.parametrize("handler_name,broker_apis_fn,client_fn_name", [
        ("holdings", "fetch_holdings", "fetch_holdings"),
        ("positions", "fetch_positions", "fetch_positions"),
        ("margins", "fetch_margins", "fetch_margins"),
    ])
    def test_internal_route_exception_never_reads_as_silent_empty(
        self, handler_name, broker_apis_fn, client_fn_name,
    ):
        handler = InternalBrokerController.__dict__[handler_name].fn

        with patch(
            f"backend.brokers.broker_apis.{broker_apis_fn}",
            side_effect=RuntimeError("kite session expired"),
        ):
            route_resp = asyncio.run(handler(object()))

        assert route_resp.accounts == []
        assert route_resp.errors, "route handler must record the exception in errors"

        wire_bytes = msgspec.json.encode(route_resp)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(wire_bytes))
            mock_get_client.return_value = mock_client

            client_fn = getattr(conn_sync, client_fn_name)
            result = client_fn()

        assert result != []
        assert result[0].attrs.get("fetch_failed") is True


class TestBrokerApisSurvivesConnServiceDegradedResponse:
    """Confirms the fetch_failed marker set by the client-boundary fix
    actually survives broker_apis.py's post-processing (_apply_backfill_
    to_list) and reaches the public fetch_positions()/fetch_holdings()/
    fetch_margins() entry points that routes/positions.py etc. call.

    Read-only w.r.t. broker_apis.py — no changes made there; this test
    exists to prove the A1 client-boundary fix is not silently undone by
    a downstream transform living in a file owned by a parallel agent."""

    def _run(self, monkeypatch, fetch_name: str):
        from backend.brokers import broker_apis

        monkeypatch.setattr(broker_apis, "_use_conn_service", lambda: True)
        payload = InternalPerAccountResp(accounts=[], errors=["boom"])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            broker_apis._raw_cache_invalidate(None)
            try:
                fn = getattr(broker_apis, fetch_name)
                result = fn()
            finally:
                broker_apis._raw_cache_invalidate(None)

        assert result, f"{fetch_name}: degraded conn_service response must not vanish into []"
        assert any(
            bool(getattr(df, "attrs", {}).get("fetch_failed")) for df in result
        ), f"{fetch_name}: fetch_failed marker must survive broker_apis post-processing"

    def test_fetch_positions_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_positions")

    def test_fetch_holdings_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_holdings")

    def test_fetch_margins_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_margins")


# ---------------------------------------------------------------------------
# 2026-11 wire-format fix: a zero-row failed/stale account must survive the
# full conn_service → sync-client round trip, not just the all-failed
# `errors`-list shape covered above.
#
# Root cause this closes: `_df_list_to_per_account` (routes.py) never read
# `_per_account_signal` (the per-account stale/fetch_failed snapshot
# `_apply_backfill_to_list` captures from every input frame BEFORE
# `pd.concat` drops attrs that differ). An account whose fetch failed with
# zero rows (no last-known-good to substitute) contributed nothing to the
# combined frame, so it was entirely absent from the wire response — the
# main API saw a fully "healthy" envelope list even though one account's
# data was silently missing.
# ---------------------------------------------------------------------------
from backend.brokers.broker_apis import (  # noqa: E402
    _apply_backfill_to_list,
    _record_lkg_frame,
    _stale_substitute_frame,
    _LKG_FRAME_BY_ACCT,
    _LKG_FRAME_LOCK,
    _DB_LKG_CACHE,
    _DB_LKG_CACHE_LOCK,
)
from backend.api.routes.positions import (  # noqa: E402
    _accounts_flagged_stale,
    _build_stale_since_map,
    _is_positions_outage,
    _positions_partial_outage_accounts,
)


def _row(account: str, **overrides) -> dict:
    base = {
        "account": account,
        "tradingsymbol": "NIFTY24JUN22000CE",
        "exchange": "NFO",
        "last_price": 150.0,
        "prev_close": 100.0,
        "quantity": 10,
        "average_price": 90.0,
        "pnl": 600.0,
    }
    base.update(overrides)
    return base


def _healthy_frame(account: str) -> pd.DataFrame:
    return pd.DataFrame([_row(account)])


@pytest.fixture(autouse=True)
def _clear_lkg_caches():
    """`_LKG_FRAME_BY_ACCT`/`_DB_LKG_CACHE` are module-level globals shared
    by every test file in this process. Snapshot + restore (not a bare
    wholesale clear) so this file can never discard an entry some OTHER
    test module seeded and still needs after this module's tests run —
    the two dicts are shared process-wide state, not scoped to this
    file."""
    with _LKG_FRAME_LOCK:
        _lkg_snapshot = dict(_LKG_FRAME_BY_ACCT)
        _LKG_FRAME_BY_ACCT.clear()
    with _DB_LKG_CACHE_LOCK:
        _db_lkg_snapshot = dict(_DB_LKG_CACHE)
        _DB_LKG_CACHE.clear()
    yield
    with _LKG_FRAME_LOCK:
        _LKG_FRAME_BY_ACCT.clear()
        _LKG_FRAME_BY_ACCT.update(_lkg_snapshot)
    with _DB_LKG_CACHE_LOCK:
        _DB_LKG_CACHE.clear()
        _DB_LKG_CACHE.update(_db_lkg_snapshot)


def _conn_service_side_envelopes(frames: list[pd.DataFrame]) -> list[InternalAccountEnvelope]:
    """Mirrors exactly what conn_service's own /internal/positions handler
    does: `broker_apis.fetch_positions()`'s real post-processing
    (`_apply_backfill_to_list`) followed by `_df_list_to_per_account`."""
    with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
        dfs = _apply_backfill_to_list(frames)
    return _df_list_to_per_account(dfs)


def _client_raw_reconstruct(envelopes: list[InternalAccountEnvelope]) -> list[pd.DataFrame]:
    """Encodes `envelopes` exactly as conn_service would over the wire and
    decodes + reconstructs via the real `sync._fetch_per_account` — the
    RAW per-account list, one frame per envelope, before any second
    `_apply_backfill_to_list` pass recombines them. This is the exact
    shape Step C (sync.py) is responsible for: each frame's own `.attrs`
    must carry `account`/`stale`/`stale_since`/`fetch_failed` identically
    to what `_stale_substitute_frame` would have set in-process."""
    payload = InternalPerAccountResp(accounts=envelopes, errors=[])
    content = msgspec.json.encode(payload)
    with patch("backend.brokers.client.sync._get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=_mock_resp(content))
        mock_get_client.return_value = mock_client
        return conn_sync.fetch_positions()


def _client_side_reconstruct(envelopes: list[InternalAccountEnvelope]) -> list[pd.DataFrame]:
    """As `_client_raw_reconstruct`, then runs the SAME `_apply_backfill_
    to_list` pass the main API process runs on `conn_sync.fetch_positions()`'s
    result inside `broker_apis._fetch_positions_cached` — the shape
    `positions.py`'s helpers actually receive as `per_acct`."""
    client_result = _client_raw_reconstruct(envelopes)
    with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
        return _apply_backfill_to_list(client_result)


class TestZeroRowFailedAccountSurvivesFullRoundTrip:
    """The headline scenario: an account with a genuinely failed fetch and
    NO prior last-known-good (would otherwise collapse to a zero-row
    frame) going through the full envelope round trip — built on the
    conn_service side, encoded, decoded on the client side, and fed back
    through the main API's own post-processing."""

    def test_failed_zero_row_account_reaches_the_client_and_is_flagged(self):
        healthy = _healthy_frame("ZG0790")
        # No LKG seeded for GR87DF — _stale_substitute_frame's no-LKG
        # fallback path: empty frame, fetch_failed=True, account set.
        failed = _stale_substitute_frame("positions", "GR87DF")
        assert failed.empty
        assert failed.attrs.get("fetch_failed") is True

        envelopes = _conn_service_side_envelopes([healthy, failed])

        # The account must appear in the per-account envelope list at
        # all — this is the core of the bug: before the fix, a zero-row
        # fetch_failed account was silently absent from `_df_list_to_
        # per_account`'s output entirely.
        by_acct = {e.account: e for e in envelopes}
        assert "GR87DF" in by_acct, "failed account must not be dropped from the wire envelope list"
        assert by_acct["GR87DF"].ok is False
        assert by_acct["GR87DF"].fetch_failed is True
        assert by_acct["GR87DF"].rows == []
        assert by_acct["ZG0790"].ok is True

        # Step C check: the RAW client-side reconstruction (one frame per
        # envelope, before any re-concat) must carry the exact .attrs
        # shape _stale_substitute_frame would have produced in-process.
        raw_client_result = _client_raw_reconstruct(envelopes)
        failed_frames = [
            df for df in raw_client_result
            if bool(getattr(df, "attrs", {}).get("fetch_failed"))
        ]
        assert failed_frames, "no reconstructed frame carries fetch_failed=True"
        assert all(df.empty for df in failed_frames)
        assert any(
            df.attrs.get("account") == "GR87DF" for df in failed_frames
        ), "reconstructed failed frame must carry attrs['account'] — not just ok=False"

        reconstructed = _client_side_reconstruct(envelopes)

        # Reconstitute the final per-account signal exactly as
        # positions.py would, and assert both accounts are visible —
        # NOT silently dropped — with the failed account's attrs intact.
        assert _accounts_flagged_stale(reconstructed) == {"GR87DF"}
        assert _positions_partial_outage_accounts(reconstructed) == ["GR87DF"]
        assert _is_positions_outage(reconstructed) is False

    def test_stale_with_rows_account_also_survives_and_rows_are_not_lost(self):
        healthy = _healthy_frame("ZG0790")
        _record_lkg_frame("positions", "DH6847", _healthy_frame("DH6847"))
        stale = _stale_substitute_frame("positions", "DH6847")
        assert not stale.empty
        assert stale.attrs.get("stale") is True

        envelopes = _conn_service_side_envelopes([healthy, stale])
        by_acct = {e.account: e for e in envelopes}
        assert by_acct["DH6847"].stale is True
        assert by_acct["DH6847"].fetch_failed is False
        assert by_acct["DH6847"].ok is True
        assert len(by_acct["DH6847"].rows) == 1

        reconstructed = _client_side_reconstruct(envelopes)
        assert _accounts_flagged_stale(reconstructed) == {"DH6847"}
        assert _build_stale_since_map(reconstructed).get("DH6847") is not None
        assert _is_positions_outage(reconstructed) is False

        total_rows = sum(len(df) for df in reconstructed)
        assert total_rows == 2  # ZG0790's row + DH6847's stale-substituted row

    def test_three_accounts_stale_failed_and_healthy_all_correctly_flagged(self):
        """The full repro from the audit, carried through the wire:
        one stale-substituted (has rows), one empty fetch_failed
        (no LKG), one plain healthy account."""
        healthy = _healthy_frame("ZG0790")
        _record_lkg_frame("positions", "DH6847", _healthy_frame("DH6847"))
        stale = _stale_substitute_frame("positions", "DH6847")
        failed = _stale_substitute_frame("positions", "GR87DF")

        envelopes = _conn_service_side_envelopes([healthy, stale, failed])
        assert {e.account for e in envelopes} == {"ZG0790", "DH6847", "GR87DF"}

        reconstructed = _client_side_reconstruct(envelopes)
        assert _accounts_flagged_stale(reconstructed) == {"DH6847", "GR87DF"}
        assert _positions_partial_outage_accounts(reconstructed) == ["GR87DF"]
        assert _is_positions_outage(reconstructed) is False
        assert _build_stale_since_map(reconstructed).get("DH6847") is not None

    def test_every_account_stale_substituted_is_not_a_false_outage(self):
        """`ok` must stay tied to `fetch_failed` only, never `stale` — a
        batch where EVERY account is stale-substituted (but each still
        has rows from its own LKG) is a degraded-but-serving batch, not
        an outage, and must never trip the 'all failed -> 503' gate."""
        _record_lkg_frame("positions", "DH6847", _healthy_frame("DH6847"))
        _record_lkg_frame("positions", "ZJ6294", _healthy_frame("ZJ6294"))
        stale_a = _stale_substitute_frame("positions", "DH6847")
        stale_b = _stale_substitute_frame("positions", "ZJ6294")

        envelopes = _conn_service_side_envelopes([stale_a, stale_b])
        assert all(e.ok for e in envelopes), "stale != failed — ok must stay True"

        reconstructed = _client_side_reconstruct(envelopes)
        assert _is_positions_outage(reconstructed) is False
        assert _accounts_flagged_stale(reconstructed) == {"DH6847", "ZJ6294"}

    def test_old_conn_service_envelope_without_new_fields_decodes_to_defaults(self):
        """An envelope encoded WITHOUT the new stale/stale_since/
        fetch_failed fields (simulating an older, not-yet-redeployed
        conn_service — see InternalAccountEnvelope's own docstring on
        this deploy-coupling hazard) must still decode cleanly and fall
        back to the pre-existing `not entry.ok` check."""
        payload = InternalPerAccountResp(
            accounts=[InternalAccountEnvelope(account="DH6847", ok=False, rows=[])],
            errors=[],
        )
        # Drop the new fields entirely to simulate an old wire payload —
        # msgspec still decodes this fine since they default.
        raw = msgspec.to_builtins(payload)
        for acct in raw["accounts"]:
            acct.pop("stale", None)
            acct.pop("stale_since", None)
            acct.pop("fetch_failed", None)
        content = msgspec.json.encode(raw)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client
            result = conn_sync.fetch_positions()

        assert len(result) == 1
        assert result[0].attrs.get("fetch_failed") is True

    def test_non_float_stale_since_degrades_to_none_not_a_500(self):
        """A `stale_since` value that can't be coerced to a float (e.g. a
        pandas Timestamp, a malformed string from some future producer
        bug) must not raise inside the handler — that would turn the
        whole response into `errors=[...]`, reading as a FULL outage for
        every account in the batch over what is at most a cosmetic
        staleness-timestamp defect. `_as_epoch` degrades it to None."""
        healthy = _healthy_frame("ZG0790")
        _record_lkg_frame("positions", "DH6847", _healthy_frame("DH6847"))
        stale = _stale_substitute_frame("positions", "DH6847")
        stale.attrs["stale_since"] = "not-a-number"

        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            dfs = _apply_backfill_to_list([healthy, stale])
        envelopes = _df_list_to_per_account(dfs)

        by_acct = {e.account: e for e in envelopes}
        assert by_acct["DH6847"].stale is True
        assert by_acct["DH6847"].stale_since is None
        assert by_acct["DH6847"].ok is True


class TestShapeTwoRawPerAccountListSurvivesFullRoundTrip:
    """Shape 2 — the raw, un-concatenated per-account list — is margins
    ALWAYS (broker_apis never runs `_apply_backfill_to_list` on margins)
    and holdings/positions whenever EVERY account's frame is empty (the
    early-return branch in `_apply_backfill_to_list`). Neither shape-1
    test above exercises this branch of `_df_list_to_per_account` or the
    attrs-first account-resolution fix in `_account_attr_or_first_row`."""

    def test_all_accounts_failed_zero_row_round_trip_is_a_real_outage(self):
        """Every account fails with no LKG — `_apply_backfill_to_list`
        early-returns the raw per-frame list (no `_per_account_signal`
        anywhere), so `_df_list_to_per_account` must take shape 2's
        per-frame attrs-first path. Before the fix, an empty frame's
        account resolved to None here (attrs were never consulted for
        a zero-row frame), so the failed accounts were unidentifiable
        and a genuine full outage could silently read as partial/ok."""
        failed_a = _stale_substitute_frame("positions", "GR87DF")
        failed_b = _stale_substitute_frame("positions", "ZJ6294")
        assert failed_a.empty and failed_b.empty

        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            dfs = _apply_backfill_to_list([failed_a, failed_b])
        # Confirms we are genuinely exercising shape 2, not shape 1.
        assert all(
            (getattr(d, "attrs", {}) or {}).get("_per_account_signal") is None
            for d in dfs
        )

        envelopes = _df_list_to_per_account(dfs)
        by_acct = {e.account: e for e in envelopes}
        assert set(by_acct) == {"GR87DF", "ZJ6294"}, (
            "both failed accounts must be identified by account code, "
            "not resolve to None, in the raw per-frame shape"
        )
        assert all(not e.ok and e.fetch_failed for e in envelopes)

        reconstructed = _client_side_reconstruct(envelopes)
        assert _is_positions_outage(reconstructed) is True
        assert _accounts_flagged_stale(reconstructed) == {"GR87DF", "ZJ6294"}

    def test_margins_failed_zero_row_account_via_real_handler(self):
        """Margins never runs `_apply_backfill_to_list` at all — this
        exercises shape 2 through conn_service's REAL `/internal/margins`
        handler (not a hand-built frame list), proving the fix applies
        to the margins endpoint specifically, not just a positions-shaped
        reproduction."""
        healthy_margins = pd.DataFrame([{"account": "ZG0790", "avail_margin": 50000.0}])
        failed_margins = _stale_substitute_frame("margins", "GR87DF")
        assert failed_margins.empty

        handler = InternalBrokerController.__dict__["margins"].fn
        with patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[healthy_margins, failed_margins],
        ):
            route_resp = asyncio.run(handler(object()))

        assert not route_resp.errors
        by_acct = {e.account: e for e in route_resp.accounts}
        assert set(by_acct) == {"ZG0790", "GR87DF"}
        assert by_acct["GR87DF"].ok is False
        assert by_acct["GR87DF"].fetch_failed is True
        assert by_acct["GR87DF"].rows == []
        assert by_acct["ZG0790"].ok is True
        assert len(by_acct["ZG0790"].rows) == 1

        wire_bytes = msgspec.json.encode(route_resp)
        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(wire_bytes))
            mock_get_client.return_value = mock_client
            client_result = conn_sync.fetch_margins()

        assert len(client_result) == 2
        failed_frames = [
            df for df in client_result
            if bool(getattr(df, "attrs", {}).get("fetch_failed"))
        ]
        assert len(failed_frames) == 1
        assert failed_frames[0].attrs.get("account") == "GR87DF"
