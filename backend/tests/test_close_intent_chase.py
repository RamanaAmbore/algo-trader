"""Unit tests: intent="close" threads through _live_chase_config and
_start_live_chase so that close orders via LIMIT+chase bypass the 50-lot
Kite ceiling on each re-place attempt inside the chase loop.

These are pure-function tests — no I/O, no event loop needed.
"""
from backend.api.routes.orders_helpers import _live_chase_config


class TestLiveChaseConfig:
    def test_close_intent_propagated(self):
        cfg = _live_chase_config(aggressiveness="low", intent="close")
        assert cfg.intent == "close", (
            "intent='close' must reach ChaseConfig so chase loop bypasses 50-lot ceiling"
        )

    def test_no_intent_defaults_to_none(self):
        cfg = _live_chase_config(aggressiveness="low")
        assert cfg.intent is None

    def test_none_intent_explicit(self):
        cfg = _live_chase_config(aggressiveness="low", intent=None)
        assert cfg.intent is None

    def test_med_aggressiveness_close_intent(self):
        cfg = _live_chase_config(aggressiveness="med", intent="close")
        assert cfg.intent == "close"
        assert cfg.interval_seconds == 20

    def test_high_aggressiveness_close_intent(self):
        cfg = _live_chase_config(aggressiveness="high", intent="close")
        assert cfg.intent == "close"
        assert cfg.interval_seconds == 10

    def test_aggressiveness_params_unchanged_with_intent(self):
        """Supplying intent must not disturb interval/step/attempts."""
        cfg_plain = _live_chase_config(aggressiveness="low")
        cfg_close = _live_chase_config(aggressiveness="low", intent="close")
        assert cfg_close.interval_seconds == cfg_plain.interval_seconds
        assert cfg_close.aggression_step == cfg_plain.aggression_step
        assert cfg_close.max_attempts == cfg_plain.max_attempts
