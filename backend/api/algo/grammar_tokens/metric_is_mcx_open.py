"""Metric resolver — `is_mcx_open`: 1.0/0.0 from ctx.segment_flags['mcx_open'].

First real consumers of `agent_engine._ae_segment_flags()`'s computed
dict — see that function and `Context.segment_flags`
(agent_evaluator.py) for how the values get here. `ctx.segment_flags`
is `{}` on any caller that doesn't populate it (log-feed-driven event
agents via `event_agents.py:Context(log_records=[rec])`, or a
hand-built test Context) — this resolver degrades to `None`
("missing", not a false 0/closed reading) rather than raising.
"""

from __future__ import annotations


def _metric_is_mcx_open(ctx, row):
    """1.0/0.0 from `ctx.segment_flags['mcx_open']`. None when unset."""
    v = (ctx.segment_flags or {}).get('mcx_open')
    if v is None:
        return None
    return 1.0 if v else 0.0
