"""
Tests for the deploy-sync reconciliation safety net in api/background.py.

Incident this guards against (2026-09): a GitHub webhook delivery for a
`main` push was silently dropped; prod ran a stale commit for ~4 hours
with zero automated detection. `_deploy_sync_decision` is the pure
comparison/decision unit — no subprocess, no network, no asyncio — so it
is tested here with mocked (local_head, remote_head, now, state) tuples
only. The async task loop (`_task_deploy_sync_check`) and its subprocess
helpers are exercised only for import/registration and graceful-failure
shape; they are NOT invoked against live git/network in this suite
(matches the "no live git/network calls" requirement).

Dimensions covered per the five-dimension test convention:
  - SSOT: `_task_deploy_sync_check` delegates to `_deploy_sync_decision`
    for the actual alert/no-alert call, not inline branching.
  - Correctness: matching / within-grace / past-grace / already-alerted /
    missing-HEAD / remote-moves-mid-episode transitions.
  - Reuse: alert path reuses `send_ntfy_alert` (grepped, not reimplemented).
  - Registration: task is wired into `on_startup`'s task list.
  - Safety: subprocess helpers never raise on a bad/non-git path — they
    degrade to None so the pure decision function's "missing HEAD" branch
    is reachable in practice, not just in theory.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import inspect

from backend.api.background import (
    _deploy_sync_decision,
    _deploy_sync_git_heads,
    _deploy_sync_local_branch,
    _deploy_sync_repo_root,
    _task_deploy_sync_check,
    _DEPLOY_SYNC_GRACE_SECONDS,
)

_SRC = Path("backend/api/background.py").read_text()

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def _seconds_after(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------

def test_matching_heads_no_alert():
    should_alert, state = _deploy_sync_decision("abc123", "abc123", NOW, {})
    assert should_alert is False
    assert state == {"diverged_since": None, "diverged_pair": None, "alerted_pair": None}


def test_diverged_within_grace_no_alert_yet():
    # First tick: divergence just detected — starts the clock, no alert.
    should_alert, state = _deploy_sync_decision("local1", "remote1", NOW, {})
    assert should_alert is False
    assert state["diverged_since"] == NOW
    assert state["diverged_pair"] == ("local1", "remote1")
    assert state["alerted_pair"] is None

    # Second tick, still within the grace window.
    should_alert2, state2 = _deploy_sync_decision(
        "local1", "remote1", _seconds_after(300), state,
    )
    assert should_alert2 is False
    assert state2["alerted_pair"] is None


def test_diverged_past_grace_fires_once():
    state = {"diverged_since": NOW, "diverged_pair": ("local1", "remote1"), "alerted_pair": None}
    past_grace = _seconds_after(_DEPLOY_SYNC_GRACE_SECONDS + 1)

    should_alert, new_state = _deploy_sync_decision("local1", "remote1", past_grace, state)
    assert should_alert is True
    assert new_state["alerted_pair"] == ("local1", "remote1")


def test_already_alerted_suppresses_repeat_ticks():
    # Simulate the tick right after the alert fired — same pair, already alerted.
    state = {
        "diverged_since": NOW,
        "diverged_pair": ("local1", "remote1"),
        "alerted_pair": ("local1", "remote1"),
    }
    later = _seconds_after(_DEPLOY_SYNC_GRACE_SECONDS + (15 * 60))

    should_alert, new_state = _deploy_sync_decision("local1", "remote1", later, state)
    assert should_alert is False, "must not re-fire every 15-min tick for the same episode"
    assert new_state["alerted_pair"] == ("local1", "remote1")


def test_recovery_clears_state_after_alert():
    state = {
        "diverged_since": NOW,
        "diverged_pair": ("local1", "remote1"),
        "alerted_pair": ("local1", "remote1"),
    }
    should_alert, new_state = _deploy_sync_decision(
        "remote1", "remote1", _seconds_after(20 * 60), state,
    )
    assert should_alert is False
    assert new_state == {"diverged_since": None, "diverged_pair": None, "alerted_pair": None}


def test_remote_moves_again_mid_episode_starts_new_grace_window():
    # Already alerted for (local1, remote1); remote advances to remote2
    # while local is still unpatched — this is functionally a NEW
    # divergence (a second commit landed and was also missed), so it
    # must re-arm the grace window rather than silently reusing the old
    # alerted_pair (which would never match remote2 anyway, but this
    # test locks in that the timer also restarts, not just the pair key).
    state = {
        "diverged_since": NOW,
        "diverged_pair": ("local1", "remote1"),
        "alerted_pair": ("local1", "remote1"),
    }
    moment = _seconds_after(20 * 60)  # long past the original grace window
    should_alert, new_state = _deploy_sync_decision("local1", "remote2", moment, state)

    assert should_alert is False, "new episode must not alert on the same tick it's detected"
    assert new_state["diverged_since"] == moment
    assert new_state["diverged_pair"] == ("local1", "remote2")
    assert new_state["alerted_pair"] is None

    # Confirm the new episode alerts once its OWN grace window elapses.
    later = moment + timedelta(seconds=_DEPLOY_SYNC_GRACE_SECONDS + 1)
    should_alert2, new_state2 = _deploy_sync_decision("local1", "remote2", later, new_state)
    assert should_alert2 is True
    assert new_state2["alerted_pair"] == ("local1", "remote2")


def test_missing_local_head_no_alert_state_unchanged():
    state = {"diverged_since": NOW, "diverged_pair": ("l", "r"), "alerted_pair": None}
    should_alert, new_state = _deploy_sync_decision(None, "remote1", _seconds_after(10000), state)
    assert should_alert is False
    assert new_state == state


def test_missing_remote_head_no_alert_state_unchanged():
    state = {"diverged_since": NOW, "diverged_pair": ("l", "r"), "alerted_pair": ("l", "r")}
    should_alert, new_state = _deploy_sync_decision("local1", None, _seconds_after(10000), state)
    assert should_alert is False
    assert new_state == state


def test_decision_never_mutates_input_state():
    state = {"diverged_since": NOW, "diverged_pair": ("local1", "remote1"), "alerted_pair": None}
    frozen = dict(state)
    _deploy_sync_decision("local1", "remote1", _seconds_after(1), state)
    assert state == frozen, "the pure function must not mutate its `state` argument in place"


# ---------------------------------------------------------------------------
# Subprocess helpers — graceful-failure shape only, no live network calls
# ---------------------------------------------------------------------------

def test_repo_root_resolves_to_actual_repo(tmp_path):
    root = _deploy_sync_repo_root()
    assert isinstance(root, Path)
    assert (root / "backend" / "api" / "background.py").exists()


def test_git_heads_returns_none_pair_on_non_repo_path(tmp_path):
    # `tmp_path` is a plain directory, not a git repo — `git -C <path>
    # rev-parse HEAD` / `ls-remote` fail immediately (no network attempt,
    # since git refuses before ever contacting a remote), so both sides
    # must degrade to None rather than raise.
    local_head, remote_head = _deploy_sync_git_heads(tmp_path, "main")
    assert local_head is None
    assert remote_head is None


def test_local_branch_returns_none_on_non_repo_path(tmp_path):
    assert _deploy_sync_local_branch(tmp_path) is None


# ---------------------------------------------------------------------------
# Wiring / reuse / SSOT
# ---------------------------------------------------------------------------

def test_task_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(_task_deploy_sync_check)


def test_task_registered_in_on_startup():
    assert "_task_deploy_sync_check" in _SRC.split("async def on_startup")[1].split(
        "async def on_shutdown"
    )[0], "_task_deploy_sync_check must be registered in on_startup's bg_tasks list"


def test_task_wrapped_in_supervised_like_its_siblings():
    on_startup_body = _SRC.split("async def on_startup")[1].split("async def on_shutdown")[0]
    assert "_supervised(_task_deploy_sync_check" in on_startup_body, (
        "must use the _supervised() wrapper like other long-running background "
        "tasks, so a crash restarts the task instead of silently killing it"
    )


def test_task_reuses_send_ntfy_alert_not_a_new_transport():
    src = inspect.getsource(_task_deploy_sync_check)
    assert "send_ntfy_alert" in src
    assert "requests." not in src and "httpx" not in src, (
        "must reuse the existing ntfy transport (alert_utils.send_ntfy_alert), "
        "not a new httpx/requests call (IPv6 happy-eyeballs breaks ntfy.sh "
        "delivery from this server — see CLAUDE.md 'Things to Avoid')"
    )


def test_task_delegates_alert_decision_to_pure_function():
    src = inspect.getsource(_task_deploy_sync_check)
    assert "_deploy_sync_decision(" in src, (
        "the async loop must delegate to the pure decision function, not "
        "reimplement grace-window/alert-suppression logic inline"
    )


def test_task_documents_manual_fallback_command():
    src = inspect.getsource(_task_deploy_sync_check)
    assert "dispatch.sh" in src and "refs/heads/" in src, (
        "the alert message should carry the manual remediation command"
    )
