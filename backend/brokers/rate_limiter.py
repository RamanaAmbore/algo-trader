"""
Token bucket rate limiter for broker API endpoints.

Per-endpoint rate limiting with shared limiter instance across all Dhan
(or other broker adapter) calls. Blocks on throttle() when tokens exhausted,
refills over elapsed time using monotonic clock.
"""
import time
import threading
from typing import Dict, Tuple


class TokenBucketLimiter:
    """Token bucket rate limiter for per-endpoint throttling.

    SSOT: throttle(group) is the single public gate.
    Perf: sleep is proportional to token deficit; 1.1× padding enforced.
    Stale: bucket refills over elapsed time (monotonic clock).
    Reuse: shared limiter instance across all adapter calls.
    UX: unknown endpoint group is a no-op (no crash, no delay).

    Args:
        limits: dict[group: str, (capacity: float, period_seconds: float)]
            - capacity: max tokens in bucket
            - period_seconds: time (s) to refill from empty to full
    """

    def __init__(self, limits: Dict[str, Tuple[float, float]]) -> None:
        """Initialize limiter with per-endpoint capacity and refill rates.

        Args:
            limits: dict mapping group name to (capacity, period_seconds).
        """
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()

        for group, (capacity, period) in limits.items():
            refill_rate = capacity / period if period > 0 else 0
            self._buckets[group] = {
                "capacity": capacity,
                "refill_rate": refill_rate,
                "tokens": float(capacity),  # Start with full bucket
                "last_refill": time.monotonic(),
            }

    def throttle(self, group: str) -> None:
        """Throttle (block if needed) until a token is available for group.

        No-op if group not configured. Blocks with 1.1× sleep padding
        when bucket empty.

        Args:
            group: endpoint group name (e.g., "orders", "history", "auth").
        """
        if group not in self._buckets:
            # Unknown group — no-op
            return

        with self._lock:
            bucket = self._buckets[group]

            # Refill tokens based on elapsed time
            now = time.monotonic()
            elapsed = now - bucket["last_refill"]
            refill_amount = bucket["refill_rate"] * elapsed
            bucket["tokens"] = min(
                bucket["capacity"],
                bucket["tokens"] + refill_amount
            )
            bucket["last_refill"] = now

            # Check if we have at least 1 token
            if bucket["tokens"] >= 1.0:
                # Token available — consume and return
                bucket["tokens"] -= 1.0
                return

            # No tokens available — sleep until one refills
            deficit = 1.0 - bucket["tokens"]
            if bucket["refill_rate"] > 0:
                # Sleep time: deficit / refill_rate (with 1.1× padding)
                sleep_time = (deficit / bucket["refill_rate"]) * 1.1
                bucket["tokens"] = 0  # Mark bucket as exhausted before sleep
                bucket["last_refill"] = time.monotonic()
            else:
                # Zero refill rate — bucket never refills; release lock and return
                # without sleeping or consuming a token (no-op for unknown/disabled groups).
                return

        # Sleep outside the lock to allow other threads access
        if sleep_time != float('inf'):
            time.sleep(sleep_time)

        # After sleep, consume the token (non-blocking, always succeeds)
        with self._lock:
            bucket = self._buckets[group]
            now = time.monotonic()
            elapsed = now - bucket["last_refill"]
            bucket["tokens"] = min(
                bucket["capacity"],
                bucket["tokens"] + bucket["refill_rate"] * elapsed
            )
            bucket["last_refill"] = now
            bucket["tokens"] = max(0, bucket["tokens"] - 1.0)
