"""
Tests for the 2026-11-23 deploy.sh CONN_TOUCHED widening (operator:
"widen CONN_TOUCHED to cover backend/shared/helpers too").

Live incident this closes: a fix to backend/shared/helpers/recovery.py
(recoverable()'s retry/escalate decorator, actually used by
backend/brokers/adapters/kite.py's @recoverable calls) landed on disk via
a normal deploy, but ramboq_conn (the process that actually runs kite.py)
never restarted to pick it up — CONN_TOUCHED's own grep pattern was scoped
to `backend/brokers/` only, and recovery.py sits outside that path even
though ramboq_conn imports it. Confirmed live: ramboq_conn's own
ActiveEnterTimestamp predated the deploy that shipped the fix. The
one-off workaround (documenting the dependency at kite.py's own import
site so THAT push's CHANGED-file list included a backend/brokers/ path)
fixed that one instance; this widens the actual detection so any future
backend/shared/ change ramboq_conn imports is caught automatically.

Two parts:
  1. Static source check — the grep pattern itself.
  2. A live reproduction of the grep classification using the pattern
     EXTRACTED from deploy.sh (not a reimplementation) against sample
     file lists, so a future edit that breaks the pattern's shape
     fails this test, not just a string-contains check.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[2] / "webhook" / "deploy.sh"


class TestConnTouchedStaticPattern:
    @property
    def src(self) -> str:
        return DEPLOY_SH.read_text()

    def test_conn_touched_pattern_covers_backend_shared(self):
        assert re.search(
            r"grep -qE '\^\(backend/brokers/\|backend/shared/\|webhook/ramboq_conn\\\.service\)'",
            self.src,
        ), (
            "CONN_TOUCHED's grep pattern must cover backend/brokers/, "
            "backend/shared/, AND the systemd unit — found pattern does not "
            "match the expected widened shape"
        )

    def test_conn_touched_still_gated_to_prod_only(self):
        """Dev pushes must never restart ramboq_conn — dev shares the
        prod UDS (same invariant as before this widening, untouched)."""
        assert 'if [ "$ENV" = "prod" ] && echo "$CHANGED" | grep -qE' in self.src


class TestConnTouchedLiveGrepReproduction:
    """Extracts the REAL grep -E pattern from deploy.sh and runs real
    `grep -qE` subprocess calls against sample file lists — proves the
    actual shell classification, not a Python reimplementation that
    could silently drift from the real regex."""

    def _extract_pattern(self) -> str:
        src = DEPLOY_SH.read_text()
        # Anchor on the CONN_TOUCHED= block specifically — the file has
        # more than one `grep -qE '^(...)'` pattern (e.g. BACKEND_TOUCHED's
        # own), so a bare first-match search can silently grab the wrong one.
        block_start = src.index("CONN_TOUCHED=false")
        block = src[block_start:block_start + 400]
        m = re.search(r"grep -qE '(\^\([^']+\))'", block)
        assert m, "could not extract the CONN_TOUCHED grep -qE pattern from deploy.sh"
        return m.group(1)

    def _matches(self, changed_files: list[str]) -> bool:
        pattern = self._extract_pattern()
        changed = "\n".join(changed_files)
        result = subprocess.run(
            ["grep", "-qE", pattern],
            input=changed, capture_output=True, text=True,
        )
        return result.returncode == 0

    def test_recovery_py_path_now_matches(self):
        """The exact live-incident path: a shared-helper file ramboq_conn
        imports, which did NOT trip CONN_TOUCHED before this fix."""
        assert self._matches(["backend/shared/helpers/recovery.py"])

    def test_brokers_path_still_matches(self):
        assert self._matches(["backend/brokers/adapters/kite.py"])

    def test_systemd_unit_still_matches(self):
        assert self._matches(["webhook/ramboq_conn.service"])

    def test_unrelated_routes_file_does_not_match(self):
        """A plain backend/api/ change must still leave ramboq_conn warm
        — this widening must not become an accidental blanket match."""
        assert not self._matches(["backend/api/routes/orders.py"])

    def test_frontend_file_does_not_match(self):
        assert not self._matches(["frontend/src/lib/stores.js"])

    def test_mixed_list_matches_when_any_entry_qualifies(self):
        assert self._matches([
            "backend/api/routes/orders.py",
            "backend/shared/helpers/settings.py",
            "CLAUDE.md",
        ])
