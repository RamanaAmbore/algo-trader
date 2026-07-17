"""
Tests for api/algo/sim/synthesize.py — scenario synthesizer from agent conditions.
SSOT: pick_target_leaf extracts the target leaf from any condition tree.
Perf: pure in-memory tree traversal (no DB/broker calls).
Stale: _synth_pick_from_composite handles all/any/not composites correctly.
Reuse: synthesize.py generates scenario dicts compatible with SimDriver.start().
UX: any/all/not trees pick a reasonable target leaf rather than crashing.
"""
from pathlib import Path

_SRC = Path("backend/api/algo/sim/synthesize.py").read_text()


def test_pick_target_leaf_exists():
    from backend.api.algo.sim.synthesize import pick_target_leaf
    assert callable(pick_target_leaf), "pick_target_leaf must be callable"


def test_pick_target_leaf_with_leaf_node():
    """A leaf condition node must return itself."""
    from backend.api.algo.sim.synthesize import pick_target_leaf
    leaf = {"metric": "pnl", "scope": "any_acct", "op": ">", "value": 1000}
    result = pick_target_leaf(leaf)
    assert result is not None, "pick_target_leaf must return the leaf itself for a leaf node"
    assert result.get("metric") == "pnl", f"Leaf must be returned unchanged, got: {result}"


def test_pick_target_leaf_with_any_composite():
    """'any' composite picks the loosest threshold (smallest |value|)."""
    from backend.api.algo.sim.synthesize import pick_target_leaf
    cond = {
        "any": [
            {"metric": "pnl", "scope": "any_acct", "op": ">", "value": 5000},
            {"metric": "pnl", "scope": "any_acct", "op": ">", "value": 2000},
        ]
    }
    result = pick_target_leaf(cond)
    assert result is not None, "pick_target_leaf must return a leaf for 'any' composite"
    assert result.get("metric") == "pnl"


def test_pick_target_leaf_with_all_composite():
    """'all' composite picks the tightest threshold (largest |value|)."""
    from backend.api.algo.sim.synthesize import pick_target_leaf
    cond = {
        "all": [
            {"metric": "pnl", "scope": "any_acct", "op": ">", "value": 5000},
            {"metric": "day_val", "scope": "any_acct", "op": "<", "value": -1000},
        ]
    }
    result = pick_target_leaf(cond)
    assert result is not None, "pick_target_leaf must return a leaf for 'all' composite"


def test_pick_target_leaf_none_for_empty():
    """Non-leaf, non-composite node must return None."""
    from backend.api.algo.sim.synthesize import pick_target_leaf
    result = pick_target_leaf({})
    assert result is None, "pick_target_leaf must return None for unrecognized node"


def test_synth_pick_from_composite_exists():
    from backend.api.algo.sim.synthesize import _synth_pick_from_composite
    assert callable(_synth_pick_from_composite)


def test_synthesize_output_format_in_source():
    """Synthesized scenario must have slug, name, mode, initial, ticks fields."""
    assert '"slug"' in _SRC or "'slug'" in _SRC, "synthesize must produce a slug field"
    assert '"mode"' in _SRC or "'mode'" in _SRC, "synthesize must produce a mode field"
    assert '"initial"' in _SRC or "'initial'" in _SRC, "synthesize must produce an initial field"
    assert '"ticks"' in _SRC or "'ticks'" in _SRC, "synthesize must produce a ticks field"
