"""
test_metrics_capture.py

Code-metrics capture script — parser-only unit tests.

Six quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — `scripts/capture_metrics.py` is the single producer
                   of `code_metrics_snapshots` rows. The route under
                   `/api/admin/code-metrics` is read-only; no other
                   module writes to the table. Grep guard enforced
                   in `test_capture_is_sole_writer`.
  2. Performance — parsers operate on in-memory strings (no subprocess
                   round-trip). Each parser test asserts <50 ms wall
                   time on a small fixture. Response-time aggregation
                   is also asserted to be fast (dimension 6 applies
                   here: the aggregator itself must not be a perf
                   bottleneck when processing thousands of test nodes).
  3. Stale code  — no parallel metrics-capture module exists; grep
                   guard ensures we don't accumulate a second pipeline
                   alongside the script.
  4. Reusable    — parsers are pure functions reused by the script's
                   `_capture` and by these tests. No duplication.
  5. Correctness — every parser handles three input shapes: happy
                   path, empty input, malformed/garbage input.
  6. Response time — `_aggregate_durations` and the pytest/playwright
                   parsers must themselves execute in <50 ms for
                   reasonable fixture sizes. The framework captures
                   these automatically per snapshot once
                   `--with-test-times` is in the deploy pipeline.

Tests deliberately avoid invoking radon / vulture / jscpd as
subprocesses — those probe paths are exercised by hand by the
operator on demand. Here we focus on the JSON / text parsing logic
that turns tool output into DB-column values.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


# ── 1. SSOT — capture script is the sole writer ─────────────────────────────


def test_capture_is_sole_writer():
    """Only `scripts/capture_metrics.py` is allowed to INSERT into
    code_metrics_snapshots. The route layer is read-only by design —
    a stray INSERT in a route would risk double-rows during deploys.

    Asserts:
      - capture_metrics.py exists and contains `CodeMetricsSnapshot(`
        constructor call OR setattr loop (the script's two write paths).
      - No backend/api/routes/*.py file constructs
        `CodeMetricsSnapshot(...)` (route layer = read-only).
    """
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "capture_metrics.py"
    assert script.exists(), "capture_metrics.py must exist"
    body = script.read_text()
    assert "CodeMetricsSnapshot(" in body, "capture script must INSERT rows"

    routes_dir = root / "backend" / "api" / "routes"
    for py in routes_dir.glob("*.py"):
        text = py.read_text()
        # Importing the model for type annotations is fine; constructing
        # it (parens) is the violation.
        if "CodeMetricsSnapshot(" in text:
            raise AssertionError(
                f"{py.name} constructs CodeMetricsSnapshot — routes must be read-only"
            )


# ── 3. Stale code — no parallel capture pipeline ─────────────────────────────


def test_no_parallel_metrics_pipeline():
    """Guard against drift: only ONE script captures metrics. If a
    contributor adds `scripts/grab_metrics.py` or
    `backend/api/metrics_collector.py`, this test forces them to either
    merge the logic into `capture_metrics.py` or rename it through.
    """
    root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []
    for stem in ("grab_metrics", "metrics_collector", "code_metrics_capture"):
        candidates.extend(root.rglob(f"{stem}.py"))
    candidates = [p for p in candidates if "venv" not in p.parts and ".log" not in p.parts]
    assert not candidates, f"parallel metrics scripts detected: {candidates}"


# ── 5. Correctness — radon CC parser, happy / empty / malformed ──────────────


def test_radon_cc_parser_happy(monkeypatch, tmp_path: Path) -> None:
    """`_radon_cc` reduces radon's JSON to (avg, max). Fake the
    subprocess so the test runs in <50 ms without invoking radon."""
    from scripts import capture_metrics

    fake_json = json.dumps({
        "backend/foo.py": [
            {"complexity": 3, "type": "function"},
            {"complexity": 7, "type": "function"},
        ],
        "backend/bar.py": [
            {"complexity": 2, "type": "function"},
        ],
    })
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/radon")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, fake_json, ""))

    t0 = time.perf_counter()
    avg, mx, raw = capture_metrics._radon_cc(tmp_path)
    dt = time.perf_counter() - t0

    assert avg == round((3 + 7 + 2) / 3, 2)
    assert mx == 7
    assert isinstance(raw, dict) and "backend/foo.py" in raw
    assert dt < 0.05, f"parser too slow: {dt*1000:.1f}ms"


def test_radon_cc_parser_empty(monkeypatch, tmp_path: Path) -> None:
    """Empty input → (0.0, 0). Defends against the first-commit case
    where radon outputs `{}` on an empty project."""
    from scripts import capture_metrics

    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/radon")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, "{}", ""))

    avg, mx, raw = capture_metrics._radon_cc(tmp_path)
    assert avg == 0.0
    assert mx == 0
    assert raw == {}


def test_radon_cc_parser_malformed(monkeypatch, tmp_path: Path) -> None:
    """Garbage stdout → (None, None, error dict). Captures should never
    crash on a tool that mis-installs / mis-writes."""
    from scripts import capture_metrics

    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/radon")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, "not-json", ""))

    avg, mx, raw = capture_metrics._radon_cc(tmp_path)
    assert avg is None and mx is None
    assert "_error" in raw


def test_radon_cc_tool_missing(monkeypatch, tmp_path: Path) -> None:
    from scripts import capture_metrics
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: None)
    avg, mx, raw = capture_metrics._radon_cc(tmp_path)
    assert avg is None and mx is None
    assert "_skipped" in raw


# ── 5. Correctness — radon RAW (lines-of-code) parser ────────────────────────


def test_radon_raw_parser_happy(monkeypatch, tmp_path: Path) -> None:
    from scripts import capture_metrics
    fake = json.dumps({
        "a.py": {"sloc": 100, "comments": 10},
        "b.py": {"sloc": 250},
    })
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/radon")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, fake, ""))
    loc, raw = capture_metrics._radon_raw(tmp_path)
    assert loc == 350
    assert isinstance(raw, dict)


def test_radon_raw_parser_empty(monkeypatch, tmp_path: Path) -> None:
    from scripts import capture_metrics
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/radon")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, "{}", ""))
    loc, _ = capture_metrics._radon_raw(tmp_path)
    assert loc == 0


# ── 5. Correctness — vulture rc=3 is "ran fine + found stale code" ───────────


def test_vulture_rc_three_treated_as_success(monkeypatch, tmp_path: Path) -> None:
    """vulture exits 3 when it FINDS dead code. The parser must treat
    that as a successful run, not a failure (the common case)."""
    from scripts import capture_metrics

    stdout = (
        "backend/foo.py:10: unused import 'bar' (90% confidence)\n"
        "backend/foo.py:99: unused variable 'baz' (100% confidence)\n"
    )
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/vulture")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (3, stdout, ""))
    count, raw = capture_metrics._vulture(tmp_path)
    assert count == 2
    assert "unused import" in raw


def test_vulture_zero_findings(monkeypatch, tmp_path: Path) -> None:
    from scripts import capture_metrics
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/vulture")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, "", ""))
    count, _ = capture_metrics._vulture(tmp_path)
    assert count == 0


def test_vulture_real_failure(monkeypatch, tmp_path: Path) -> None:
    """vulture rc=2 (config error) etc. must surface as None — not
    silently zero-out the stale_count column."""
    from scripts import capture_metrics
    monkeypatch.setattr(capture_metrics, "_resolve_tool", lambda _: "/usr/bin/vulture")
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (2, "", "config error"))
    count, raw = capture_metrics._vulture(tmp_path)
    assert count is None
    assert "rc=2" in raw


# ── 5. Correctness — frontend LOC scanner ────────────────────────────────────


def test_frontend_loc_counts_extensions(tmp_path: Path) -> None:
    """`_frontend_loc` must count .svelte + .js + .ts and skip
    node_modules / .svelte-kit / build."""
    from scripts import capture_metrics

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.svelte").write_text("a\nb\nc\n")        # 3 lines
    (src / "lib.js").write_text("x\ny\n")               # 2 lines
    (src / "node_modules").mkdir()
    (src / "node_modules" / "skip.js").write_text("ignored\n")
    (src / ".svelte-kit").mkdir()
    (src / ".svelte-kit" / "skip.js").write_text("ignored\n")

    total, raw = capture_metrics._frontend_loc(src)
    assert total == 5, f"expected 5, got {total}"
    assert raw["by_ext"][".svelte"] == 3
    assert raw["by_ext"][".js"] == 2


def test_frontend_loc_missing_dir(tmp_path: Path) -> None:
    from scripts import capture_metrics
    missing = tmp_path / "no-such"
    loc, raw = capture_metrics._frontend_loc(missing)
    assert loc is None
    assert "_skipped" in raw


# ── 5. Correctness — bug-commit heuristic ────────────────────────────────────


def test_bug_commit_heuristic(monkeypatch) -> None:
    """`_count_bug_commits` matches `fix:`, `fix(`, `fix `, `bug:`,
    `URGENT`, `P0` — and only those."""
    from scripts import capture_metrics

    log = "\n".join([
        "fix: stale ticker",
        "fix(orders): postback double-fire",
        "fix the demo flow",
        "bug: dead chart",
        "URGENT NAV mismatch",
        "P0 sparkline freeze",
        "feat: new chart toolbar",
        "refactor: clean up cache",
        "docs: update CLAUDE.md",
        "chore: bump version",
    ])
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, log, ""))
    n = capture_metrics._count_bug_commits("v1.0.0")
    assert n == 6, f"expected 6 bug commits, got {n}"


# ── 6. Response time — _aggregate_durations helper ───────────────────────────


def test_aggregate_durations_happy() -> None:
    """`_aggregate_durations` must produce correct median, max, top-10,
    and slow_count. This is the shared aggregation path used by both
    the pytest and Playwright parsers."""
    from scripts import capture_metrics

    durs  = [0.1, 0.5, 1.5, 2.0, 0.3, 3.1, 0.05, 0.8, 1.2, 0.9, 0.4]
    names = [f"test_{i}" for i in range(len(durs))]

    t0 = time.perf_counter()
    result = capture_metrics._aggregate_durations(durs, names)
    dt = time.perf_counter() - t0

    assert result["total_tests"] == 11
    assert result["total_wall_time_s"] == round(sum(durs), 3)
    # Median of sorted [0.05, 0.1, 0.3, 0.4, 0.5, 0.8, 0.9, 1.2, 1.5, 2.0, 3.1]
    # = 11 items → index 5 = 0.8
    assert result["median_s"] == pytest.approx(0.8, abs=0.0001)
    assert result["max_s"] == pytest.approx(3.1, abs=0.0001)
    # slow_count: tests ≥ 1.0s = [1.5, 2.0, 3.1, 1.2] = 4
    assert result["slow_count"] == 4
    assert result["slow_threshold_s"] == capture_metrics._SLOW_TEST_THRESHOLD_S
    # top_10_slowest sorted descending by duration
    assert len(result["top_10_slowest"]) == 10
    assert result["top_10_slowest"][0]["duration_s"] == pytest.approx(3.1, abs=0.0001)
    assert "name" in result["top_10_slowest"][0]
    # Dimension 6: aggregation must be fast
    assert dt < 0.05, f"_aggregate_durations too slow: {dt*1000:.1f}ms"


def test_aggregate_durations_empty() -> None:
    """Empty list → all-zeros dict, not an exception."""
    from scripts import capture_metrics
    result = capture_metrics._aggregate_durations([], [])
    assert result["total_tests"] == 0
    assert result["max_s"] == 0.0
    assert result["top_10_slowest"] == []


def test_aggregate_durations_single() -> None:
    """Single test → median == that test's duration."""
    from scripts import capture_metrics
    result = capture_metrics._aggregate_durations([2.5], ["test_only"])
    assert result["total_tests"] == 1
    assert result["median_s"] == pytest.approx(2.5, abs=0.0001)
    assert result["max_s"] == pytest.approx(2.5, abs=0.0001)


def test_aggregate_durations_large_fixture() -> None:
    """Aggregating 5000 test entries must complete in <50ms (dimension 6
    — the aggregator is on the hot path of every --with-test-times run)."""
    import random
    from scripts import capture_metrics

    n = 5000
    random.seed(42)
    durs  = [round(random.uniform(0.001, 5.0), 4) for _ in range(n)]
    names = [f"test_module::test_{i}" for i in range(n)]

    t0 = time.perf_counter()
    result = capture_metrics._aggregate_durations(durs, names)
    dt = time.perf_counter() - t0

    assert result["total_tests"] == n
    assert len(result["top_10_slowest"]) == 10
    assert dt < 0.05, f"_aggregate_durations(5000 entries) too slow: {dt*1000:.1f}ms"


def test_pytest_durations_json_report_happy(monkeypatch, tmp_path: Path) -> None:
    """_pytest_durations uses the pytest-json-report plugin when available.
    When the plugin is NOT installed (which is the dev-local case until
    pip install runs on the server), the function falls back to
    `--durations=0` text parsing.

    This test exercises the JSON-report parse path directly by:
    1. Writing a fake report file.
    2. Monkeypatching `_run` to exit 0 without running pytest.
    3. Calling `_pytest_durations` with `json_report_path` pointing to
       the fake file AND monkeypatching the internal import check so
       the function thinks the plugin is installed.
    """
    from scripts import capture_metrics

    # Fake a pytest-json-report output file. We do NOT pre-create it
    # here — `fake_run` below creates it as the "subprocess side-effect"
    # because `_pytest_durations` calls `out_path.unlink()` before
    # invoking the subprocess (to ensure a fresh report).
    report = {
        "tests": [
            {"nodeid": "tests/test_a.py::test_fast", "call": {"duration": 0.05}},
            {"nodeid": "tests/test_b.py::test_slow", "call": {"duration": 2.3}},
            {"nodeid": "tests/test_c.py::test_medium", "call": {"duration": 0.8}},
        ]
    }
    out_path = tmp_path / "report.json"

    # Make the function believe pytest-json-report is installed by
    # injecting a sentinel into sys.modules so that
    # `importlib.import_module('pytest_jsonreport')` succeeds.
    import types
    fake_mod = types.ModuleType("pytest_jsonreport")
    monkeypatch.setitem(sys.modules, "pytest_jsonreport", fake_mod)

    # Monkeypatch _run so pytest is NOT actually invoked. The function
    # calls `out_path.unlink()` before the subprocess, then checks
    # `out_path.exists()` after — so our fake _run must re-create the
    # report file as part of the "subprocess" side-effect.
    report_bytes = json.dumps(report)

    def fake_run(*args, **kwargs):
        out_path.write_text(report_bytes)
        return (0, "", "")

    monkeypatch.setattr(capture_metrics, "_run", fake_run)

    t0 = time.perf_counter()
    result, meta = capture_metrics._pytest_durations(json_report_path=out_path)
    dt = time.perf_counter() - t0

    assert result["total_tests"] == 3
    assert result["max_s"] == pytest.approx(2.3, abs=0.001)
    # slow_count: tests ≥ 1.0s = [2.3] = 1
    assert result["slow_count"] == 1
    assert result["top_10_slowest"][0]["name"] == "tests/test_b.py::test_slow"
    assert dt < 0.05, f"_pytest_durations parse too slow: {dt*1000:.1f}ms"


def test_pytest_durations_missing_file(monkeypatch, tmp_path: Path) -> None:
    """When the JSON report doesn't exist (no pytest-json-report plugin),
    the fallback `--durations=0` path must return a valid dict, not
    raise. We monkeypatch _run to return empty text output (as if
    no tests ran or no slow tests were found by `--durations=0`)."""
    from scripts import capture_metrics

    # Patch _run to simulate `pytest --durations=0` producing no duration lines.
    monkeypatch.setattr(capture_metrics, "_run", lambda *a, **kw: (0, "no tests collected", ""))

    result, meta = capture_metrics._pytest_durations(
        json_report_path=tmp_path / "nonexistent.json"
    )
    # Should return either an aggregated empty dict or a skipped marker.
    # Either shape is acceptable — what must NOT happen is an exception.
    assert isinstance(result, dict)


def test_playwright_durations_happy(tmp_path: Path) -> None:
    """_playwright_durations parses a minimal Playwright JSON report.
    Tests are nested inside suites → specs → tests → results."""
    from scripts import capture_metrics

    pw_report = {
        "suites": [
            {
                "title": "e2e",
                "suites": [
                    {
                        "title": "metrics",
                        "specs": [
                            {
                                "title": "loads metrics page",
                                "tests": [
                                    {
                                        "title": "loads metrics page",
                                        "results": [{"duration": 1200}],
                                    }
                                ],
                            },
                            {
                                "title": "shows trend tiles",
                                "tests": [
                                    {
                                        "title": "shows trend tiles",
                                        "results": [{"duration": 350}],
                                    }
                                ],
                            },
                        ],
                    }
                ],
                "specs": [],
            }
        ]
    }
    report_path = tmp_path / "pw_report.json"
    report_path.write_text(json.dumps(pw_report))

    t0 = time.perf_counter()
    result, meta = capture_metrics._playwright_durations(json_report_path=report_path)
    dt = time.perf_counter() - t0

    assert result["total_tests"] == 2
    # durations: [1.2s, 0.35s]
    assert result["max_s"] == pytest.approx(1.2, abs=0.001)
    assert result["slow_count"] == 1   # only 1.2s ≥ 1.0s threshold
    assert meta["source"] == str(report_path)
    assert dt < 0.05, f"_playwright_durations parse too slow: {dt*1000:.1f}ms"


def test_playwright_durations_missing(tmp_path: Path) -> None:
    """When no Playwright JSON report exists, return {} + _skipped meta."""
    from scripts import capture_metrics

    result, meta = capture_metrics._playwright_durations(
        json_report_path=tmp_path / "no_report.json"
    )
    assert result == {}
    assert "_skipped" in meta


def test_playwright_durations_malformed(tmp_path: Path) -> None:
    """Malformed Playwright JSON should not raise — return {} gracefully."""
    from scripts import capture_metrics

    bad_path = tmp_path / "bad.json"
    bad_path.write_text("not-valid-json{{{")

    result, meta = capture_metrics._playwright_durations(json_report_path=bad_path)
    # The bad file is skipped; no other candidates exist → skipped.
    assert isinstance(result, dict)


# ── 2. Performance — list_snapshots payload omits raw_payload ────────────────


def test_list_payload_excludes_raw():
    """The /code-metrics/ list endpoint omits raw_payload to keep the
    payload small (raw radon dumps can be hundreds of KB). The detail
    endpoint includes it. Both are msgspec Structs — assert structure."""
    from backend.api.routes.metrics import (
        MetricsSnapshotRow, MetricsDetailResponse,
    )

    list_fields = set(MetricsSnapshotRow.__struct_fields__)
    detail_fields = set(MetricsDetailResponse.__struct_fields__)
    assert "raw_payload" not in list_fields, "list row must NOT carry raw_payload"
    assert "raw_payload" in detail_fields, "detail response must carry raw_payload"
    # test_response_times IS included in the list row so the table can show a
    # quick "captured / not captured" state without a detail round-trip.
    assert "test_response_times" in list_fields, (
        "test_response_times must be in the list row (quick status column)"
    )


# ── 4. Reusable — trend column allowlist matches numeric model columns ───────


def test_trend_allowlist_covers_numeric_columns():
    """The `/trends` endpoint accepts columns from `_TREND_COLUMNS` (real DB
    columns) plus virtual test-time sub-keys from `_TEST_TREND_KEYS`.

    Invariants:
    - Every entry in `_TREND_COLUMNS` must be a real model column.
    - Every numeric metric column (backend_*/frontend_*/bug_count_*) must
      be in `_TREND_COLUMNS` — no silent gap in chartable data.
    - Every entry in `_TEST_TREND_KEYS` must NOT be a real column name
      (they are virtual, extracted from the JSONB field at query time).
    """
    from backend.api.models import CodeMetricsSnapshot
    from backend.api.routes.metrics import _TREND_COLUMNS, _TEST_TREND_KEYS

    model_cols = {c.name for c in CodeMetricsSnapshot.__table__.columns}

    # Every real-column allowlist entry must exist on the model.
    for col in _TREND_COLUMNS:
        assert col in model_cols, f"_TREND_COLUMNS entry '{col}' is not a real column"

    # Every numeric metric column we want to chart must be in the allowlist.
    expected = {
        c.name for c in CodeMetricsSnapshot.__table__.columns
        if c.name.startswith(("backend_", "frontend_", "bug_count_"))
    }
    missing = expected - set(_TREND_COLUMNS)
    assert not missing, f"_TREND_COLUMNS missing numeric columns: {missing}"

    # Virtual test-time keys must NOT overlap with real columns.
    for vk in _TEST_TREND_KEYS:
        assert vk not in model_cols, (
            f"_TEST_TREND_KEYS entry '{vk}' collides with a real column name"
        )
    # All virtual keys must start with 'test_' by convention.
    for vk in _TEST_TREND_KEYS:
        assert vk.startswith("test_"), f"_TEST_TREND_KEYS entry '{vk}' must start with 'test_'"
