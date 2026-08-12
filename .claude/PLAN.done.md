# Plan: GTT audit fixes — stale code cleanup + two correctness gaps

## Context

Audit covered:
- Stale code across broker adapters and template layer
- GTT method implementations across Kite / Dhan / Groww
- Whether the template layer correctly gates on broker capabilities

**Overall picture**: GTT is fully implemented across all three brokers and capability
claims match implementations. The template layer correctly uses `BrokerCapabilities`
for OCO-vs-two-singles dispatch, translate_qty per leg, and MCX guard. No stale code
found (no dead imports, no `_safe_call` remnants, no orphaned tests).

Two correctness gaps found that need fixing:

**P1 — `_extract_dhan_status_rows()` returns `[]` instead of `None`**
`dhan.py:194-204`: `rows = _unwrap(resp)` returns `[]` on shape mismatch. When
`if rows:` is False, the function falls into the dict-fallback branch but reuses
the same `rows = []` reference. If no target codes match, it returns `[]` — not
`None` as documented. The caller `market_status()` checks `if rows is None:` and
treats `[]` as "parsed but closed" → returns `False` instead of `None` (unknown).
Silent correctness bug: Dhan market status incorrectly reports "closed" when the
SDK returns an unrecognised shape.

**P2 — `capabilities_for()` exception falls back to `None`, not `UNKNOWN_CAPS`**
`template_attach.py:2256-2262`: on any exception from `capabilities_for()`, `caps`
stays `None`. At line 1100, `if broker_caps is None or broker_caps.gtt_oco:` treats
`None` as "OCO supported" — sends a two-leg GTT to a broker that may not support
it. `UNKNOWN_CAPS` has `gtt_oco=False`, `gtt_single=False` (safe conservative
fallback). The fix also logs a warning so operators can diagnose the lookup failure.

**P2 — `apply_plan_live` has no pre-flight for `gtt_single=False`**
Currently the only capability check before placement is `validate_gtt_exchange()`
(exchange-level). If a new broker declares `gtt_single=False`, `broker.place_gtt()`
raises `NotImplementedError` which is caught and turned into a generic error string.
Adding an explicit pre-flight using `broker.capabilities.gtt_single` gives a clear
operator-visible message and skips all the GTT loop work immediately.

## Agents

- backend: skip
- frontend: skip
- broker: Fix P1 `_extract_dhan_status_rows()` in `backend/brokers/adapters/dhan.py:187-205`
  — initialize `rows = []` inside the `isinstance(resp, dict)` branch (not reusing
  `_unwrap`'s empty list), and return `rows if rows else None` instead of bare `rows`.
  Add a test in `backend/tests/test_dhan_adapter_coverage.py` that calls
  `_extract_dhan_status_rows({"NSE_EQ": {"other": 1}}, ("MCX_COMM",))` directly and
  asserts the return is `None`, not `[]`.
- doc: skip
- backend-test: Fix P2a + P2b in `backend/api/algo/template_attach.py`:
  1. `template_attach.py:2256-2262` — change the exception fallback from `caps = None`
     to `caps = UNKNOWN_CAPS` (imported from `backend.brokers.capabilities`), and add
     `logger.warning(...)` so operators can see the lookup failure in logs.
  2. `template_attach.py:1669` — after `broker.validate_gtt_exchange()`, add:
     ```python
     _bcaps = broker.capabilities
     if not _bcaps.gtt_single:
         result.errors.append(
             f"{broker.broker_id} does not support GTT (gtt_single=False) — "
             "template attach skipped"
         )
         return result
     ```
  Add tests in `backend/tests/test_broker_client.py` or a new
  `backend/tests/broker/test_template_gtt_preflight.py`:
  - Test that `apply_plan_live` returns early with a clear error when broker caps
    have `gtt_single=False` (mock `broker.capabilities` to return UNKNOWN_CAPS).
  - Test that the caps fallback to `UNKNOWN_CAPS` on `capabilities_for()` exception
    (patch `capabilities_for` to raise, verify `caps` ends up as `UNKNOWN_CAPS`).

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(broker+template): _extract_dhan_status_rows None contract, UNKNOWN_CAPS fallback, gtt_single preflight

## Done when

- `_extract_dhan_status_rows({"X": {"other": 1}}, ("MCX_COMM",))` returns `None`
- `capabilities_for()` exception falls back to `UNKNOWN_CAPS`, not `None`
- `apply_plan_live` returns immediately with clear message when `gtt_single=False`
- All existing GTT adapter tests continue to pass
- `pytest backend/tests/ -q` green

## Critical files

| File | Change |
|---|---|
| `backend/brokers/adapters/dhan.py:187-205` | P1 fix: init fresh `rows = []` in dict branch, `return rows if rows else None` |
| `backend/api/algo/template_attach.py:2256-2262` | P2a fix: `caps = UNKNOWN_CAPS` + warning log on exception |
| `backend/api/algo/template_attach.py:1669-1674` | P2b fix: add `gtt_single` pre-flight check using `broker.capabilities` |
| `backend/tests/test_dhan_adapter_coverage.py` | New test for `_extract_dhan_status_rows` null-return contract |
| `backend/tests/broker/test_template_gtt_preflight.py` (new) | Tests for caps fallback + gtt_single preflight |

## Reuse

- `UNKNOWN_CAPS` from `backend/brokers/capabilities.py:178` — already defined, conservative all-False
- `broker.capabilities` property from `backend/brokers/base.py:84-90` — returns via `capabilities_for_broker_id`
- `AttachResult` from `backend/api/algo/template_attach.py` — existing result type, use `.errors.append()`
