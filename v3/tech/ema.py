#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EMA ribbon for the V3 technical lab.

Spans 20 / 50 / 100 / 150 / 200, one series per timeframe.  Confidence is
reported honestly: an EMA200 computed from 30 monthly bars is not the same
thing as an EMA200 computed from 1,000 daily bars.
"""

from __future__ import annotations

import math

SPANS = (20, 50, 100, 150, 200)


def ema_series(values, span):
    """Standard EMA, seeded with the SMA of the first ``span`` values."""
    n = len(values)
    out = [None] * n
    if n == 0 or span <= 0:
        return out
    k = 2.0 / (span + 1.0)
    if n < span:
        # Not enough bars for this span: report nothing rather than a value
        # that is mostly the first print.
        return out
    out[span - 1] = sum(float(v) for v in values[:span]) / span
    for i in range(span, n):
        out[i] = float(values[i]) * k + out[i - 1] * (1.0 - k)
    return out


def confidence(bars, span):
    if not span:
        return "low"
    ratio = float(bars) / float(span)
    if ratio >= 5.0:
        return "high"
    if ratio >= 3.0:
        return "medium"
    return "low"


def ribbon(bars, spans=SPANS):
    """{'ema20': [...], ...} aligned to ``bars`` (leading Nones until warm)."""
    closes = [float(bar["c"]) for bar in bars]
    return {"ema%d" % span: ema_series(closes, span) for span in spans}


def alignment(close, values):
    """bull / bear / mixed for close > e20 > e50 > e100 > e150 > e200."""
    seq = [values.get(span) for span in SPANS if values.get(span) is not None]
    if len(seq) < 2:
        return "unknown"
    ordered_down = all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1))
    ordered_up = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
    if close > seq[0] and ordered_down:
        return "bull"
    if close < seq[0] and ordered_up:
        return "bear"
    return "mixed"


def snapshot(bars, spans=SPANS):
    """Latest ribbon state for one timeframe."""
    if not bars:
        return {"ok": False, "reason": "no bars"}
    closes = [float(bar["c"]) for bar in bars]
    series = ribbon(bars, spans)
    close = closes[-1]
    values = {}
    details = {}
    for span in spans:
        value = series["ema%d" % span][-1]
        values[span] = value
        if value is None:
            details[str(span)] = None
            continue
        details[str(span)] = {
            "value": round(value, 2),
            "dist_pct": round((close - value) / value * 100.0, 3),
            "at_or_above": close >= value,
            "confidence": confidence(len(bars), span),
        }
    present = [v for v in values.values() if v is not None]
    spread_pct = None
    if len(present) >= 2 and close:
        spread_pct = (max(present) - min(present)) / close * 100.0
    tight = [v for v in present[-2:]] if len(present) >= 2 else []
    spread_20_200 = None
    if values.get(20) and values.get(200):
        spread_20_200 = abs(values[20] - values[200]) / close * 100.0
    return {
        "ok": True,
        "bars": len(bars),
        "close": round(close, 2),
        "last_bar_ts": int(bars[-1].get("t") or 0),
        "emas": {str(span): (round(values[span], 2) if values[span] is not None else None)
                 for span in spans},
        "details": details,
        "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        "spread_20_200_pct": (round(spread_20_200, 3)
                              if spread_20_200 is not None else None),
        "alignment": alignment(close, values),
        "above_50": (close >= values[50]) if values.get(50) else None,
        "confidence": confidence(len(bars), max(spans)),
    }
