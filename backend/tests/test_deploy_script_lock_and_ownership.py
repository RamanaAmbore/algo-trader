"""
Tests for the 2026-11-22 deploy.sh fixes (operator: "fix any issues with
deploy script. the ownership of files is sometimes issue. even lock may
be an issues with multiple processes running in dev and prod").

Two independent, confirmed-by-reading-the-code bugs in webhook/deploy.sh:

D13 (ownership) — the pre-git-ops `chown` only covered a 6-path whitelist
(.git, .log, backend/config, frontend/.svelte-kit, frontend/build,
frontend/node_modules). `git reset --hard origin/$BRANCH` can touch ANY
file in the repo; a file left root-owned anywhere else (a manual SSH
edit, an aborted sudo op) would make that `git reset --hard` fail with a
permission error and, under `set -e`, abort the whole deploy. Fixed by
widening the chown to the whole `$APP_ROOT` tree.

D14 (lock) — the lock release used to ALSO delete the lock file
(`rm -f "$LOCK"`) right after releasing the flock. `flock` locks an
INODE, not a path — deleting the file right after release opens a real
race: a THIRD deploy starting in the window after the delete but before
a second, already-queued deploy finishes will `exec 200>"$LOCK"` and find
no file, CREATING A NEW INODE with no competing lock holder, and proceed
fully concurrently with the second deploy. Three or more overlapping
deploys (a dev push and a main push landing seconds apart, or a webhook
redelivery) could silently defeat the whole-host serialization the lock
exists for. Fixed by never deleting the lock file — closing the fd
(`exec 200>&-`) already releases the flock immediately; the file itself
is a permanent sentinel.

This file has two parts:
  1. Static source checks on webhook/deploy.sh itself (the bug patterns
     must be gone, the fixes must be present).
  2. A live reproduction of the D14 race using Python's own `fcntl.flock`
     (portable — doesn't need the Linux-only `flock(1)` binary, which
     isn't installed on this macOS dev machine) — proves the OLD pattern
     (delete-after-release) really does let a third waiter bypass the
     lock, and proves the NEW pattern (never delete) closes it.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[2] / "webhook" / "deploy.sh"


# ── Part 1: static source checks ───────────────────────────────────────

class TestDeployScriptStaticFixes:
    @pytest.fixture(autouse=True)
    def _source(self):
        self.src = DEPLOY_SH.read_text()

    def test_pre_git_chown_covers_whole_app_root(self):
        """D13 — the pre-git-ops chown must cover the whole $APP_ROOT tree,
        not a narrow whitelist of 6 subpaths (the pre-fix shape)."""
        assert re.search(
            r'sudo chown -R www-data:www-data "\$APP_ROOT"\s', self.src
        ), "pre-git-ops chown must target the whole $APP_ROOT, not a subpath list"

    def test_pre_git_chown_is_before_git_reset(self):
        """The widened chown must still run BEFORE `git reset --hard` —
        order matters, a chown after the fact doesn't help the reset itself."""
        chown_idx = self.src.index('sudo chown -R www-data:www-data "$APP_ROOT"')
        reset_idx = self.src.index('git reset --hard "origin/$BRANCH"')
        assert chown_idx < reset_idx

    def test_lock_file_is_never_deleted_at_runtime(self):
        """D14 — the normal deploy path must never `rm` the lock file. Only
        the one-time stale-ownership-recovery branch (a DIFFERENT user's
        leftover lock) may remove it, and that's a startup-time recovery,
        not the end-of-deploy release path this test targets."""
        release_block_start = self.src.index("Release the host-wide lock")
        release_block = self.src[release_block_start:release_block_start + 2000]
        # Only non-comment CODE lines count — the surrounding comment text
        # (including this fix's own explanation) legitimately mentions the
        # literal string `rm -f "$LOCK"` as prose.
        code_lines = [
            line for line in release_block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        offending = [line for line in code_lines if "rm -f" in line and "LOCK" in line]
        assert not offending, (
            f"the lock-release path must not delete the lock file — found: {offending}. "
            "See D14: deleting it opens a flock-on-a-recreated-inode race for a "
            "third overlapping deploy"
        )
        assert any("exec 200>&-" in line for line in code_lines), (
            "the lock must still be released by closing fd 200"
        )

    def test_startup_stale_lock_recovery_is_unchanged(self):
        """The ownership-mismatch recovery at startup (a lock left behind by
        a different user, e.g. a manual sudo run) is a different, legitimate
        case and must still be present — this fix only removes the NORMAL
        per-deploy delete, not this one-time recovery branch."""
        assert '[ ! -w "$LOCK" ]' in self.src
        assert "sudo rm -f \"$LOCK\"" in self.src


# ── Part 2: live, deterministic reproduction of the D14 race ──────────
#
# Single-process, no threading/multiprocessing timing needed — the whole
# bug is about INODE identity, which `os.stat().st_ino` proves directly
# and deterministically, with LOCK_NB used purely to detect contention
# (never blocks, so there is nothing timing-sensitive to get flaky about).

def _try_lock_nb(fd: int) -> bool:
    """True if fd's flock was acquired without blocking."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


class TestLockFileDeleteRaceReproduction:
    """A acquires the lock (like deploy.sh's `exec 200>"$LOCK"; flock -w 900
    200`). B then opens the SAME path and blocks (simulated via LOCK_NB
    returning False — real deploy.sh would actually block in `flock -w`,
    but the contention is the same fact being tested). A releases.

    Old pattern: A also deletes the file. C — a third deploy arriving in
    that window — opens the (now re-created) path and gets a BRAND NEW
    inode with zero contention, even though B is still sitting on the old
    inode's lock. That's the bug: two different inodes, both "locked",
    serializing nothing against each other.

    New pattern: A never deletes the file. There is no window where the
    path doesn't exist, so C can only ever open the SAME inode B is on.
    """

    def test_old_pattern_lets_third_deploy_get_a_fresh_unlocked_inode(self, tmp_path):
        lock = str(tmp_path / "ramboq_deploy.lock")

        # A acquires.
        fd_a = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        assert _try_lock_nb(fd_a) is True
        ino_a = os.fstat(fd_a).st_ino

        # B opens the same path and is blocked (A still holds it).
        fd_b = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        ino_b = os.fstat(fd_b).st_ino
        assert ino_b == ino_a, "B must open the same inode A is on"
        assert _try_lock_nb(fd_b) is False, "B must be blocked while A holds the lock"

        # A releases AND deletes (the pre-fix deploy.sh behaviour).
        os.close(fd_a)
        os.remove(lock)

        # C arrives in the gap: opens the path (recreated, since A deleted
        # it), and because it's a FRESH inode with no holder, its lock
        # succeeds immediately — even though B is still blocked/queued on
        # the OLD inode.
        fd_c = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        ino_c = os.fstat(fd_c).st_ino
        assert ino_c != ino_a, (
            "this IS the bug: C's open() created a new inode because A "
            "deleted the path — if this assertion ever starts failing, the "
            "filesystem stopped reproducing the race, not that the race "
            "stopped existing"
        )
        assert _try_lock_nb(fd_c) is True, (
            "C acquired a lock on a fresh inode while B is still blocked on "
            "the old one — serialization bypassed, reproducing the real bug"
        )

        # B, still parked on the old (unlinked but open) inode, remains
        # free to proceed once ITS flock is granted — proving B and C are
        # now unserialized with respect to each other.
        assert _try_lock_nb(fd_b) is True, (
            "once A closed fd_a, B's own wait on the OLD inode should "
            "resolve independently of C — confirming B and C hold two "
            "separate, non-competing locks simultaneously"
        )
        os.close(fd_b)
        os.close(fd_c)

    def test_new_pattern_keeps_third_deploy_on_the_same_inode(self, tmp_path):
        lock = str(tmp_path / "ramboq_deploy.lock")

        fd_a = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        assert _try_lock_nb(fd_a) is True
        ino_a = os.fstat(fd_a).st_ino

        fd_b = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        assert _try_lock_nb(fd_b) is False

        # A releases — WITHOUT deleting the file (the fix).
        os.close(fd_a)

        # C arrives afterward. The path was never removed, so it opens the
        # SAME inode B is already on.
        fd_c = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
        ino_c = os.fstat(fd_c).st_ino
        assert ino_c == ino_a, (
            "with the fix, the lock file's inode must stay stable — no "
            "delete means no window for a fresh, unlocked inode to appear"
        )
        # B already holds (or is about to hold) the lock on this inode —
        # once B actually acquires and releases, THEN C can. Simulate that
        # ordering directly: B acquires now that A is gone, C must still
        # be blocked until B also releases.
        assert _try_lock_nb(fd_b) is True, "B should now acquire the lock A released"
        assert _try_lock_nb(fd_c) is False, (
            "C must still be blocked — it's on the SAME inode B now holds, "
            "so serialization is intact even with a third overlapping "
            "deploy in play"
        )
        os.close(fd_b)
        assert _try_lock_nb(fd_c) is True, "C acquires only after B releases"
        os.close(fd_c)
