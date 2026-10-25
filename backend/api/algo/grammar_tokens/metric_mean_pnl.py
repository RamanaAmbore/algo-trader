"""Metric resolver — `mean_pnl` factory + its fixed-window bindings
(`mean_pnl_30m`, `mean_pnl_1h`).

Phase 26 (Sprint 2) — parameterized function-call tokens, e.g.
`mean_pnl(30)`. The factory is the resolver bound to the BASE token
name (registered in agent_grammar.yaml with a `params_schema`);
GrammarRegistry calls it once per distinct literal call-string (cached
forever after) and gets back the (ctx, row) -> value callable a metric
resolver normally is. The pre-existing fixed-window tokens are kept
forever, unchanged, as one-line bindings to this same factory — no
deprecation, no migration, zero behavior change for existing agents.

The bound instances live in THIS file (not back in grammar.py) so the
import direction between grammar.py and grammar_tokens/ stays strictly
one-way — grammar_tokens/ may import shared helpers from grammar.py,
but grammar.py never imports back from grammar_tokens/.
"""

from __future__ import annotations

from backend.api.algo.grammar import _w_key_pos


def _metric_factory_mean_pnl(minutes):
    return lambda ctx, row: ctx.window_mean(_w_key_pos(row), minutes)


_metric_mean_pnl_30m = _metric_factory_mean_pnl(30)
_metric_mean_pnl_1h = _metric_factory_mean_pnl(60)
