"""Metric resolver — `minutes_since_open`: minutes since the engine's
own `baseline_live` session-open anchor."""

from __future__ import annotations


def _metric_minutes_since_open(ctx, row):
    return ctx.minutes_since_open()
