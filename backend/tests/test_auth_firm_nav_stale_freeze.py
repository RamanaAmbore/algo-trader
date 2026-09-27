"""
Tests for `_compute_firm_nav`'s live-serving-path staleness freeze
(2026-09-27 council audit, Bug 1 extension — architect/risk/devil's-advocate
confirmed BLOCK-severity finding).

Root cause: `_compute_firm_nav()` (backend/api/routes/auth.py) pre-
initialized `firm_nav = 0.0` before its outer `try:` block. An unexpected
failure anywhere inside that block (NOT the ordinary "one broker account
degraded" case — `algo.nav.compute_firm_nav()` itself now freezes that via
`errors`/`_substitute_degraded_frames`, see test_nav_degraded_fetch_freeze.py
— but a genuinely unexpected internal error) fell through to the bare
`except Exception` and returned the 0.0 pre-init with NO signal
distinguishing "the firm genuinely has ₹0 NAV" from "broker outage". This
value was then cached for 30s and served with a plain HTTP 200 to
`GET /api/auth/firm-nav` (fully public/unauthenticated) and
`GET /api/auth/me/nav`.

Fix: `_NAV_LAST_GOOD` (no TTL, updated only on a fully successful compute)
freezes the served figures to the last good result on a hard failure, and
the tuple/response now carries an explicit `stale: bool` signal — set True
either on a hard failure (frozen to LKG) or when `algo.nav.compute_firm_nav`
itself reported per-account `errors` (a degraded-but-frozen leg).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# _NAV_CACHE / _NAV_LAST_GOOD reset globally, autouse, in
# backend/tests/conftest.py (_reset_auth_nav_module_caches) — every test
# below still calls auth_mod._NAV_CACHE.update(...) /
# _NAV_LAST_GOOD.update(...) explicitly at its own start too, which is
# redundant but harmless (belt-and-suspenders against fixture ordering).


class TestComputeFirmNavHardFailureFreezesToLastGood:
    def test_hard_failure_after_a_success_serves_last_good_not_zero(self):
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)
        auth_mod._NAV_LAST_GOOD.update(ts=0.0, value=None)

        async def _ok_compute_firm_nav():
            return {"nav": 4_200_000.0, "errors": []}

        # First call: succeeds normally — populates _NAV_LAST_GOOD.
        with (
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_ok_compute_firm_nav),
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
        ):
            firm_nav, _, _, _, stale = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav == pytest.approx(4_200_000.0)
        assert stale is False
        assert auth_mod._NAV_LAST_GOOD["value"] is not None

        # Force TTL expiry so the next call actually recomputes.
        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        # Second call: a genuinely unexpected internal error blows past the
        # outer except (not a per-account broker degradation — a bug-class
        # failure, e.g. compute_firm_nav itself raising).
        async def _boom():
            raise RuntimeError("unexpected internal error")

        with patch("backend.api.algo.nav.compute_firm_nav", side_effect=_boom):
            firm_nav2, day2, cum2, as_of2, stale2 = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav2 == pytest.approx(4_200_000.0), (
            "a hard failure must serve the last-known-good NAV, not reset "
            "to 0.0 — a fresh 0.0 is indistinguishable from a genuine ₹0 "
            "firm NAV on the public unauthenticated endpoint"
        )
        assert stale2 is True, (
            "the response must carry an explicit staleness signal so "
            "firm_nav==0-due-to-failure is never confused with a real 0"
        )

    def test_cold_start_hard_failure_has_no_last_good_but_is_marked_stale(self):
        """Never-succeeded-once case: nothing to freeze to, 0.0 is
        unavoidable, but `stale` must still be True — the distinguishing
        signal, not the number itself, is the contract."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)
        auth_mod._NAV_LAST_GOOD.update(ts=0.0, value=None)

        async def _boom():
            raise RuntimeError("cold start broker outage")

        with patch("backend.api.algo.nav.compute_firm_nav", side_effect=_boom):
            firm_nav, _, _, _, stale = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav == pytest.approx(0.0)
        assert stale is True

    def test_degraded_but_no_hard_failure_marks_stale_via_nav_errors(self):
        """algo.nav.compute_firm_nav() itself froze a degraded account leg
        (errors populated) without raising — the outer try/except never
        fires, but `stale` must still surface the degradation."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)
        auth_mod._NAV_LAST_GOOD.update(ts=0.0, value=None)

        async def _degraded_compute_firm_nav():
            return {
                "nav": 1_500_000.0,
                "errors": ["margins: DH6847 fetch failed, no last-known-good available"],
            }

        with (
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_degraded_compute_firm_nav),
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
        ):
            firm_nav, _, _, _, stale = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav == pytest.approx(1_500_000.0)
        assert stale is True

    def test_understated_compute_does_not_poison_last_good(self):
        """2026-09-27 council audit refinement: an UNDERSTATED compute
        (nav.py's `understated` list non-empty — the number is actually
        wrong, not just old) must NOT overwrite `_NAV_LAST_GOOD`. A LATER
        hard failure must freeze to the earlier, trustworthy value — not
        the understated one that briefly served."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)
        auth_mod._NAV_LAST_GOOD.update(ts=0.0, value=None)

        async def _ok_compute_firm_nav():
            return {"nav": 4_200_000.0, "errors": [], "understated": []}

        # First call: clean success — populates _NAV_LAST_GOOD with the
        # TRUSTWORTHY figure.
        with (
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_ok_compute_firm_nav),
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
        ):
            asyncio.run(auth_mod._compute_firm_nav())

        assert auth_mod._NAV_LAST_GOOD["value"][0] == pytest.approx(4_200_000.0)
        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        # Second call: understated (a leg has NO last-known-good) — must
        # NOT overwrite _NAV_LAST_GOOD, even though this call itself
        # succeeds without raising.
        async def _understated_compute_firm_nav():
            return {
                "nav": 100.0,  # would be a badly wrong number to remember
                "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
                "understated": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            }

        with (
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_understated_compute_firm_nav),
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
        ):
            firm_nav_mid, _, _, _, stale_mid = asyncio.run(auth_mod._compute_firm_nav())

        # 2026-09-27 audit refinement: an understated compute freezes to
        # _NAV_LAST_GOOD immediately (not just on a later hard failure) —
        # algo.nav.compute_firm_nav() now catches per-phase internally,
        # so it essentially never raises for the common degraded case;
        # gating the freeze on outer_failed alone would leave this path
        # unprotected and serve the badly-wrong 100.0 to the public
        # endpoint with only a boolean `stale` flag as the signal.
        assert firm_nav_mid == pytest.approx(4_200_000.0), (
            "an understated compute must freeze to the last TRUSTWORTHY "
            "value immediately, not surface its own wrong number"
        )
        assert stale_mid is True
        assert auth_mod._NAV_LAST_GOOD["value"][0] == pytest.approx(4_200_000.0), (
            "the understated compute must NOT have overwritten _NAV_LAST_GOOD"
        )
        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        # Third call: a genuinely hard failure — must freeze to the
        # ORIGINAL trustworthy figure, not the understated 100.0.
        async def _boom():
            raise RuntimeError("unexpected internal error")

        with patch("backend.api.algo.nav.compute_firm_nav", side_effect=_boom):
            firm_nav_final, _, _, _, stale_final = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav_final == pytest.approx(4_200_000.0), (
            "a hard failure must freeze to the last TRUSTWORTHY value, "
            "not an intervening understated one"
        )
        assert stale_final is True

    def test_clean_success_marks_not_stale(self):
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)
        auth_mod._NAV_LAST_GOOD.update(ts=0.0, value=None)

        async def _ok_compute_firm_nav():
            return {"nav": 900_000.0, "errors": []}

        with (
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_ok_compute_firm_nav),
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("no data")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
        ):
            firm_nav, _, _, _, stale = asyncio.run(auth_mod._compute_firm_nav())

        assert firm_nav == pytest.approx(900_000.0)
        assert stale is False
