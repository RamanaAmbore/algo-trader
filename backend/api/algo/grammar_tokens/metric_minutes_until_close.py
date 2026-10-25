"""Metric resolver — `minutes_until_close`: minutes until the engine's
own session-close anchor."""

from __future__ import annotations


def _metric_minutes_until_close(ctx, row):
    return ctx.minutes_until_close()
