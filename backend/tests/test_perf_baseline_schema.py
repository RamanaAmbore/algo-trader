"""Schema-shape tests for `scripts/perf_baseline.py`.

These are STATIC-ONLY tests — no radon subprocess, no Playwright, no
network. They exercise the pure functions the perf-baseline scaffold
relies on (radon walk dedup, Svelte cyclomatic heuristic, runtime merge)
against synthetic input so regressions in any of the three helpers are
caught before the tooling ships broken.

Run:
    ./venv/bin/pytest backend/tests/test_perf_baseline_schema.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
_SCRIPT = REPO / "scripts" / "perf_baseline.py"


@pytest.fixture(scope="module")
def perf_baseline():
    """Import scripts/perf_baseline.py by path — it isn't a package."""
    spec = importlib.util.spec_from_file_location("perf_baseline", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─── _radon_walk dedup ──────────────────────────────────────────────

def test_radon_walk_dedupes_class_method_double_emit(perf_baseline):
    """Radon emits methods twice — once flat, once nested inside its
    class row. Our walk MUST collapse to a single hotspot per lineno."""
    entries = [
        {
            "type": "class", "name": "PositionsController",
            "lineno": 705, "complexity": 50,
            "methods": [
                {"type": "method", "classname": "PositionsController",
                 "name": "get_positions", "lineno": 709, "complexity": 49,
                 "closures": []},
            ],
        },
        # Same method reappears flat at file top-level.
        {"type": "method", "classname": "PositionsController",
         "name": "get_positions", "lineno": 709, "complexity": 49,
         "closures": []},
        {"type": "function", "name": "_fetch",
         "lineno": 196, "complexity": 20, "closures": []},
    ]
    out: dict = {}
    perf_baseline._radon_walk(entries, out)
    # Exactly TWO entries — class row skipped, method deduped by lineno.
    assert set(out.keys()) == {709, 196}
    # The one at 709 kept the class-prefixed name (walked from methods first).
    assert out[709]["fn_name"] == "PositionsController.get_positions"
    assert out[709]["cc"] == 49


def test_radon_walk_recurses_into_closures(perf_baseline):
    """Nested closures should surface as their own hotspots when cc ≥ 10."""
    entries = [
        {"type": "function", "name": "outer",
         "lineno": 10, "complexity": 5,
         "closures": [
             {"type": "function", "name": "_helper",
              "lineno": 25, "complexity": 15, "closures": []},
         ]},
    ]
    out: dict = {}
    perf_baseline._radon_walk(entries, out)
    assert 10 in out and 25 in out
    assert out[25]["fn_name"] == "outer._helper"


# ─── _svelte_cyclomatic heuristic ───────────────────────────────────

def test_svelte_cyclomatic_returns_int_est(perf_baseline):
    src = """
    <script>
      function foo() {
        if (a && b) return 1;
        for (const x of xs) {
          if (x > 0) yield x;
        }
      }
    </script>
    <div>hello</div>
    """
    result = perf_baseline._svelte_cyclomatic(src)
    assert isinstance(result["cyclomatic_est"], int)
    # 2× if + 1× for + 1× && ≥ 4
    assert result["cyclomatic_est"] >= 4
    assert isinstance(result["cyclomatic_hotspots"], list)


def test_svelte_cyclomatic_no_script_block(perf_baseline):
    """A template-only Svelte file should yield est=0 and no hotspots
    rather than an exception."""
    src = "<div>{@html raw}</div>"
    result = perf_baseline._svelte_cyclomatic(src)
    assert result == {"cyclomatic_est": 0, "cyclomatic_hotspots": []}


def test_svelte_cyclomatic_optional_chain_not_double_counted(perf_baseline):
    """`?.` should NOT inflate the ternary count. `foo?.bar` has zero
    decision tokens."""
    src = """
    <script>
      const x = foo?.bar?.baz;
      const y = arr?.[0];
    </script>
    """
    result = perf_baseline._svelte_cyclomatic(src)
    assert result["cyclomatic_est"] == 0


# ─── _merge_runtime ──────────────────────────────────────────────────

def test_merge_runtime_folds_into_matching_page(perf_baseline, tmp_path):
    snap = {
        "frontend": {"pages": {
            "/pulse": {"file": "x.svelte", "loc": 100},
        }},
    }
    cap = {"frontend": {"pages": {
        "/pulse": {"runtime": {"lcp_ms": 500, "tbt_ms": 12}},
    }}}
    cap_path = tmp_path / "cap.json"
    cap_path.write_text(json.dumps(cap))
    perf_baseline._merge_runtime(snap, cap_path)
    assert snap["frontend"]["pages"]["/pulse"]["runtime"]["lcp_ms"] == 500
    assert snap["frontend"]["pages"]["/pulse"]["loc"] == 100  # preserved


def test_merge_runtime_captures_route_absent_from_baseline(perf_baseline, tmp_path):
    """A capture entry with no matching baseline row still lands — under
    a `runtime_only` marker so nothing is silently dropped."""
    snap = {"frontend": {"pages": {}}}
    cap = {"frontend": {"pages": {
        "/new-route": {"runtime": {"lcp_ms": 100}},
    }}}
    cap_path = tmp_path / "cap.json"
    cap_path.write_text(json.dumps(cap))
    perf_baseline._merge_runtime(snap, cap_path)
    assert "/new-route" in snap["frontend"]["pages"]
    assert snap["frontend"]["pages"]["/new-route"]["runtime_only"] is True


def test_merge_runtime_missing_file_is_soft_fail(perf_baseline, tmp_path):
    snap = {"frontend": {"pages": {"/pulse": {"loc": 10}}}}
    # File doesn't exist — merge should log warning, not raise.
    perf_baseline._merge_runtime(snap, tmp_path / "nope.json")
    assert "runtime" not in snap["frontend"]["pages"]["/pulse"]
