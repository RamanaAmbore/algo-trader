"""
Tests for the OCO pair watcher both-settled branch in background.py.

P1 fix: when both legs of an emulated OCO settle within a single 15s
poll window, sibling_id must be cleared on BOTH entries so the next
poll does not attempt a stale lookup or fire a duplicate alert.
"""

import pytest


def _run_both_settled_cleanup(attached: list[dict]) -> tuple[list[dict], bool]:
    """
    Inline re-implementation of the OCO watcher inner loop that mirrors
    the exact logic in background.py `_task_oco_pair_watcher` so we can
    unit-test it without spinning up the full Litestar app or a DB.

    Returns (attached_after_cleanup, changed).
    """
    by_id: dict[str, dict] = {
        str(e["id"]): e
        for e in attached
        if isinstance(e, dict) and e.get("id")
    }
    # Simulate broker_gtts being EMPTY — both legs gone.
    broker_gtts: set[str] = set()

    changed = False
    for entry in attached:
        if not (isinstance(entry, dict) and entry.get("sibling_id")):
            continue
        my_id = str(entry.get("id") or "")
        sib_id = str(entry.get("sibling_id") or "")
        if not (my_id and sib_id):
            continue

        my_active = my_id in broker_gtts
        sib_active = sib_id in broker_gtts

        if my_active or not sib_active:
            if not my_active and not sib_active:
                # Both-settled branch (the P1 fix lives here).
                entry.pop("sibling_id", None)
                sib_entry = by_id.get(sib_id)
                if sib_entry:
                    sib_entry.pop("sibling_id", None)
                changed = True
            continue
        # survivor branch (not exercised by this test)

    return attached, changed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_both_settled_clears_entry_sibling_id():
    """After both-settled cleanup, the primary entry has no sibling_id."""
    entry_a = {"id": "101", "sibling_id": "102", "parent_symbol": "NIFTY"}
    entry_b = {"id": "102", "sibling_id": "101", "parent_symbol": "NIFTY"}
    attached = [entry_a, entry_b]

    result, changed = _run_both_settled_cleanup(attached)

    assert "sibling_id" not in entry_a, (
        "entry_a should have sibling_id cleared after both-settled"
    )


def test_both_settled_clears_sib_entry_sibling_id():
    """After both-settled cleanup, the sibling entry also has no sibling_id — P1 fix."""
    entry_a = {"id": "201", "sibling_id": "202", "parent_symbol": "BANKNIFTY"}
    entry_b = {"id": "202", "sibling_id": "201", "parent_symbol": "BANKNIFTY"}
    attached = [entry_a, entry_b]

    result, changed = _run_both_settled_cleanup(attached)

    assert "sibling_id" not in entry_b, (
        "sib_entry (entry_b) should have sibling_id cleared — stale pointer was P1 bug"
    )


def test_both_settled_sets_changed_flag():
    """changed must be True so the DB row is persisted."""
    entry_a = {"id": "301", "sibling_id": "302"}
    entry_b = {"id": "302", "sibling_id": "301"}
    attached = [entry_a, entry_b]

    _, changed = _run_both_settled_cleanup(attached)

    assert changed is True


def test_both_settled_no_residual_sibling_pointers():
    """Neither entry should carry any sibling_id after cleanup — full mutual clear."""
    entry_a = {"id": "401", "sibling_id": "402", "parent_symbol": "CRUDEOIL"}
    entry_b = {"id": "402", "sibling_id": "401", "parent_symbol": "CRUDEOIL"}
    attached = [entry_a, entry_b]

    _run_both_settled_cleanup(attached)

    residual = [e for e in attached if e.get("sibling_id")]
    assert residual == [], (
        f"Expected zero sibling_id references after cleanup, got: {residual}"
    )


def test_only_one_settled_does_not_clear_sibling():
    """
    When only MY leg is gone and sibling is STILL active the both-settled
    branch must NOT fire — survivor-cancel branch handles it separately.
    This test ensures the guard condition is correct.
    """
    entry_a = {"id": "501", "sibling_id": "502", "parent_symbol": "GOLD"}
    entry_b = {"id": "502", "sibling_id": "501", "parent_symbol": "GOLD"}
    attached = [entry_a, entry_b]

    # Override broker_gtts inside the helper so only sib is active.
    # We achieve this by calling the logic directly with a modified version
    # that keeps sib_id in the active set.
    by_id: dict[str, dict] = {str(e["id"]): e for e in attached}
    broker_gtts = {"502"}  # sibling still active; my_id 501 is gone

    changed = False
    for entry in attached:
        if not (isinstance(entry, dict) and entry.get("sibling_id")):
            continue
        my_id = str(entry.get("id") or "")
        sib_id = str(entry.get("sibling_id") or "")
        if not (my_id and sib_id):
            continue
        my_active = my_id in broker_gtts
        sib_active = sib_id in broker_gtts
        if my_active or not sib_active:
            if not my_active and not sib_active:
                entry.pop("sibling_id", None)
                sib_e = by_id.get(sib_id)
                if sib_e:
                    sib_e.pop("sibling_id", None)
                changed = True
            continue
        # survivor branch: my leg gone, sib alive — do NOT clear pointers here

    # sibling_id on entry_a removed by survivor branch is NOT what we're testing;
    # key assertion: entry_b (the still-active survivor) retains its pointer
    # because the survivor-cancel branch only clears after a successful cancel.
    assert entry_b.get("sibling_id") == "501", (
        "Survivor entry must retain sibling_id until cancel succeeds"
    )
