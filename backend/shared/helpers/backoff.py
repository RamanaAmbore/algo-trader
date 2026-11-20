"""Shared exponential-backoff formula.

2026-11-20 (Part 3, incident-fix batch) — an operator-requested audit found
two of the codebase's retry/backoff mechanisms independently reimplementing
the identical shape (`min(base * 2**n, cap)`) with different literal
numbers: `retry_kite_conn` (`decorators.py`) and the Kite login cooloff
(`_attempt_login_with_cooloff`, `backend/brokers/connections.py`). This is
the one shared formula both now call — a pure function, with no sleeping,
no state, and no persistence of its own. Every call site keeps deciding HOW
to use the returned number: `retry_kite_conn` sleeps on it inside an active
retry loop; the Kite login cooloff computes a future `blocked_until` and
raises immediately (a passive gate), and never sleeps.

Everything else the audit covered — Dhan's flat 2-minute cooloff,
`PriceBroker`'s flat rate-limit cooloff, chase's fixed re-quote backoff,
KiteTicker's vendor-internal SDK reconnect — has a genuine reason to stay
bespoke (a real provider-documented fixed window, or vendor-SDK-owned
reconnect logic) and is deliberately NOT migrated onto this utility.

No broker imports — this module has zero dependencies, same convention as
`auth_error.py`, so it can be imported from anywhere with no circular-import
risk.
"""

from __future__ import annotations

import random


def exponential_backoff_seconds(
    attempt: int, *, base: float = 2.0, cap: float = 30.0, jitter: bool = False,
) -> float:
    """Return the exponential-backoff delay for a 0-indexed `attempt`.

    `min(base * 2**attempt, cap)` — the exact shape `retry_kite_conn` and
    the Kite login cooloff already computed inline before this fix, now
    shared. A negative `attempt` is treated as `0` (the first attempt),
    rather than raising or returning a value less than `base` — a caller
    computing `attempt` from a decrementing counter (e.g. a streak minus
    one) should never be able to request a NEGATIVE delay from a formula
    whose whole purpose is to always wait at least `base` seconds.

    `jitter=True` applies +/-10% uniform jitter AFTER the cap is applied —
    spreads out multiple callers that would otherwise retry in lockstep,
    without ever exceeding `cap * 1.1` or going below `cap * 0.9` at the
    capped tier. Not used by either of today's two call sites (both pass
    the default `jitter=False`, preserving their exact pre-existing
    byte-identical delay values) — available for a future caller that
    wants it.
    """
    n = max(attempt, 0)
    delay = min(base * (2 ** n), cap)
    if jitter:
        delay *= random.uniform(0.9, 1.1)
    return delay
