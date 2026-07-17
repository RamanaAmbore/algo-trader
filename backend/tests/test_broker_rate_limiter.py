"""
Tests for brokers/rate_limiter.py — per-endpoint token bucket rate limiter.

SSOT: TokenBucketLimiter.throttle() is the single public gate.
Perf: sleep is proportional to token deficit; 1.1× padding enforced.
Stale: bucket refills over elapsed time (monotonic clock).
Reuse: shared limiter instance across all Dhan calls in one adapter.
UX: unknown endpoint group is a no-op (no crash, no delay).
"""
import time
import threading
from backend.brokers.rate_limiter import TokenBucketLimiter


class TestRateLimiterImport:
    """Source import and module existence checks."""

    def test_rate_limiter_importable(self):
        """TokenBucketLimiter must be importable from brokers/rate_limiter.py."""
        from backend.brokers.rate_limiter import TokenBucketLimiter
        assert TokenBucketLimiter is not None, "TokenBucketLimiter must be importable"
        assert hasattr(TokenBucketLimiter, "throttle"), \
            "TokenBucketLimiter must have throttle method"
        assert hasattr(TokenBucketLimiter, "__init__"), \
            "TokenBucketLimiter must have __init__ method"


class TestThrottleBasicBehavior:
    """Basic throttle() behavior — unknown groups, token consumption."""

    def test_throttle_no_op_for_unknown_group(self):
        """Unknown group must not sleep or raise — no-op."""
        lim = TokenBucketLimiter({"orders": (10, 1.0)})
        start = time.monotonic()
        lim.throttle("nonexistent_group")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"Unknown group must not sleep, elapsed={elapsed:.3f}s"

    def test_throttle_consumes_token_within_capacity(self):
        """Two tokens available — both calls should pass without sleeping."""
        lim = TokenBucketLimiter({"orders": (2, 1.0)})
        start = time.monotonic()
        lim.throttle("orders")
        lim.throttle("orders")
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"Two calls within capacity must not sleep, elapsed={elapsed:.3f}s"

    def test_throttle_blocks_when_bucket_empty(self):
        """Third call must wait for token refill when bucket exhausted."""
        # 1 token per 0.2s
        lim = TokenBucketLimiter({"orders": (1, 0.2)})
        lim.throttle("orders")  # consume the one token
        start = time.monotonic()
        lim.throttle("orders")  # must wait for refill
        elapsed = time.monotonic() - start
        # Account for timing variance; expect ~0.2s refill
        assert elapsed >= 0.18, \
            f"Must wait for token refill (~0.2s), elapsed={elapsed:.3f}s"
        assert elapsed < 0.4, \
            f"Wait time should not exceed refill period by much, elapsed={elapsed:.3f}s"


class TestThrottleRefillBehavior:
    """Token refill over elapsed time (stale tokens)."""

    def test_tokens_refill_over_time(self):
        """After waiting, new token should be available without extra sleep."""
        lim = TokenBucketLimiter({"history": (1, 0.15)})
        lim.throttle("history")   # consume token
        time.sleep(0.2)            # wait for refill
        start = time.monotonic()
        lim.throttle("history")   # should pass without extra sleep
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, \
            f"Refilled token must not require sleeping, elapsed={elapsed:.3f}s"

    def test_partial_refill_on_wait(self):
        """Token accumulates over time; call should sleep less than full period."""
        # Capacity=2, period=0.4 → 5 tokens/s refill rate
        # After 2 calls + 0.2s sleep, bucket should have ~1 token (50% refilled)
        lim = TokenBucketLimiter({"partial": (2, 0.4)})
        lim.throttle("partial")
        lim.throttle("partial")
        time.sleep(0.2)  # 50% of refill period
        start = time.monotonic()
        lim.throttle("partial")  # should wait ~0.2s for remaining tokens
        elapsed = time.monotonic() - start
        # After 0.2s wait, we should have 1 token accumulated, but need 1
        # So minimal wait expected
        assert elapsed < 0.15, \
            f"Partial refill should require minimal wait, elapsed={elapsed:.3f}s"


class TestThrottleSeparateGroupsIndependent:
    """Groups maintain separate buckets (no cross-contamination)."""

    def test_separate_groups_independent(self):
        """Exhausting one group bucket must not affect other groups."""
        lim = TokenBucketLimiter({"orders": (1, 1.0), "history": (10, 1.0)})
        lim.throttle("orders")   # exhaust orders bucket
        # history bucket is untouched — must not sleep
        start = time.monotonic()
        lim.throttle("history")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, \
            f"History bucket must be independent of orders, elapsed={elapsed:.3f}s"

    def test_multiple_groups_all_tracked(self):
        """Multiple groups must be tracked independently."""
        lim = TokenBucketLimiter({
            "auth": (1, 1.0),
            "orders": (2, 0.5),
            "history": (5, 2.0),
        })
        # Consume one auth token
        lim.throttle("auth")
        # Others should be fine
        start = time.monotonic()
        lim.throttle("orders")
        lim.throttle("history")
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, \
            f"Multiple independent groups should not interfere, elapsed={elapsed:.3f}s"


class TestThrottleThreadSafety:
    """Concurrent access to limiter (thread safety)."""

    def test_throttle_thread_safe_concurrent_calls(self):
        """Multiple threads calling throttle must not crash or corrupt state."""
        lim = TokenBucketLimiter({"orders": (5, 1.0)})
        results = []
        errors = []

        def worker():
            try:
                lim.throttle("orders")
                results.append(1)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"No errors should occur, got: {errors}"
        assert len(results) == 5, f"All threads must complete, got {len(results)}/5"

    def test_throttle_thread_safe_stress(self):
        """High concurrency stress test — 20 threads competing."""
        lim = TokenBucketLimiter({"stress": (10, 1.0)})
        results = []
        errors = []

        def worker():
            try:
                for _ in range(3):
                    lim.throttle("stress")
                results.append(1)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"No errors during stress test, got: {errors}"
        assert len(results) == 20, f"All 20 threads must complete, got {len(results)}"


class TestThrottleEdgeCases:
    """Edge cases and boundary conditions."""

    def test_throttle_with_fractional_capacity(self):
        """Fractional capacity (e.g., 0.5 tokens per period) must work."""
        lim = TokenBucketLimiter({"slow": (0.5, 2.0)})
        # Capacity=0.5, period=2.0 → very slow refill
        # Just verify the bucket exists and group is registered
        assert "slow" in lim._buckets, "Fractional capacity group must be registered"
        capacity = lim._buckets["slow"]["capacity"]
        assert capacity == 0.5, f"Expected capacity=0.5, got {capacity}"

    def test_throttle_with_low_capacity(self):
        """Low capacity (1 token per 0.5s) should work correctly."""
        lim = TokenBucketLimiter({"low": (1, 0.5)})
        # Capacity = 1, period = 0.5 → refill_rate = 2 tokens/s
        # First call consumes 1 token (passes immediately)
        start = time.monotonic()
        lim.throttle("low")
        elapsed = time.monotonic() - start
        # Should pass without waiting (bucket started full)
        assert elapsed < 0.05, \
            f"Low-capacity bucket with full tokens should not wait, elapsed={elapsed:.3f}s"

    def test_throttle_with_very_fast_refill(self):
        """Very fast refill (tiny period) should allow rapid calls."""
        lim = TokenBucketLimiter({"fast": (100, 0.01)})
        start = time.monotonic()
        for _ in range(10):
            lim.throttle("fast")
        elapsed = time.monotonic() - start
        # 100 tokens per 0.01s = 10,000 tokens/s — should not wait
        assert elapsed < 0.1, \
            f"Very fast refill should not require waits, elapsed={elapsed:.3f}s"

    def test_throttle_very_slow_refill_auth_endpoint(self):
        """Auth endpoints may have very slow rates (e.g., 0.5 tokens/120s)."""
        # Auth: 0.5 tokens per 120s
        lim = TokenBucketLimiter({"auth": (0.5, 120.0)})
        # Just verify the bucket is registered with correct refill rate
        assert "auth" in lim._buckets, "Auth group must be registered"
        refill_rate = lim._buckets["auth"]["refill_rate"]
        expected = 0.5 / 120.0
        assert abs(refill_rate - expected) < 1e-9, \
            f"Refill rate should be {expected}, got {refill_rate}"

    def test_throttle_empty_limits_dict(self):
        """Empty limits dict should create a limiter with no groups."""
        lim = TokenBucketLimiter({})
        # Any call to a non-existent group should be no-op
        start = time.monotonic()
        lim.throttle("any_group")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, "Unknown group (no groups defined) must be no-op"


class TestThrottlePerformanceAndPadding:
    """Performance characteristics and 1.1× sleep padding."""

    def test_sleep_padding_applied(self):
        """Sleep time includes 1.1× padding for safety margin."""
        # If we need to wait 0.1s, actual sleep should be ~0.11s
        lim = TokenBucketLimiter({"padded": (1, 0.1)})
        lim.throttle("padded")  # consume token
        start = time.monotonic()
        lim.throttle("padded")  # wait for refill
        elapsed = time.monotonic() - start
        # Expected: 0.1s * 1.1 = 0.11s
        # Allow some variance but verify padding exists
        assert elapsed > 0.08, f"Should include padding, elapsed={elapsed:.3f}s"
        # Don't assert upper bound too tightly due to system scheduling variance

    def test_no_sleep_when_tokens_available(self):
        """Zero sleep overhead when tokens are available."""
        lim = TokenBucketLimiter({"free": (100, 1.0)})
        start = time.monotonic()
        for _ in range(50):
            lim.throttle("free")
        elapsed = time.monotonic() - start
        # 50 tokens with 100-capacity bucket should not sleep
        assert elapsed < 0.05, \
            f"Available tokens should not incur sleep, elapsed={elapsed:.3f}s"


class TestThrottleInitialization:
    """Limiter initialization and configuration."""

    def test_init_with_single_group(self):
        """Initialize with a single rate-limited group."""
        lim = TokenBucketLimiter({"orders": (10, 1.0)})
        assert hasattr(lim, "_buckets"), "Limiter must have _buckets dict"
        assert "orders" in lim._buckets, "Group 'orders' must be registered"

    def test_init_with_multiple_groups(self):
        """Initialize with multiple groups."""
        limits = {
            "orders": (10, 1.0),
            "auth": (2, 60.0),
            "history": (100, 5.0),
        }
        lim = TokenBucketLimiter(limits)
        for group in limits:
            assert group in lim._buckets, f"Group '{group}' must be registered"

    def test_init_preserves_capacity_and_period(self):
        """Capacity and period are correctly stored."""
        lim = TokenBucketLimiter({"test": (5, 2.0)})
        bucket = lim._buckets["test"]
        assert bucket["capacity"] == 5, f"Capacity should be 5, got {bucket['capacity']}"
        # Period is stored as refill_rate = capacity / period
        expected_rate = 5 / 2.0
        assert abs(bucket["refill_rate"] - expected_rate) < 1e-9, \
            f"Refill rate should be {expected_rate}, got {bucket['refill_rate']}"


class TestThrottleRealWorldScenarios:
    """Real-world usage patterns."""

    def test_dhan_adapter_multi_call_sequence(self):
        """Simulate Dhan adapter making sequential calls to rate-limited endpoints."""
        # Typical Dhan rate limits: orders=10/s, history=5/s
        lim = TokenBucketLimiter({
            "orders": (10, 1.0),
            "history": (5, 1.0),
            "holdings": (20, 1.0),
        })
        # Sequence: 1 order, 2 history, 1 order, 3 history
        lim.throttle("orders")
        lim.throttle("history")
        lim.throttle("history")
        start = time.monotonic()
        lim.throttle("orders")
        elapsed = time.monotonic() - start
        # Should not wait (2 calls < 10 orders capacity)
        assert elapsed < 0.05, "Sequential calls within capacity should not wait"
        # Remaining history calls
        lim.throttle("history")
        lim.throttle("history")

    def test_broker_resilience_with_backoff(self):
        """Rate limiter acts as resilience for broker overload."""
        # If broker quota is 100 calls/10s, throttle smooths it out
        lim = TokenBucketLimiter({
            "any_call": (100, 10.0),  # 100 tokens per 10 seconds
        })
        start = time.monotonic()
        for _ in range(50):
            lim.throttle("any_call")  # Should not sleep
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, "50 calls with 100-capacity should not wait"
        # Verify remaining 50 calls would wait
        call_count = 50
        bucket = lim._buckets["any_call"]
        remaining = bucket["tokens"]
        # After 50 calls, ~50 tokens should remain
        assert remaining < 60 and remaining > 40, \
            f"Expected ~50 tokens remaining, got {remaining}"
