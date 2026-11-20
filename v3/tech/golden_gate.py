#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Golden Gate rule engine for the V3 technical lab.

Encodes the strategy as computable checks per timeframe:

* trend      : close above the EMA ribbon, ribbon in bullish order
* gate       : tight 5-15 candle box, EMA spread <= 2% of close
* breakout   : close above the box high, volume >= 1.5x its 20-bar average
* invalidation: 20/200 EMA spread > 8% (overextended), close below the 50 EMA

The output is a score in [-100, +100] and a class (VN/N/NE/P/VP).  Nothing
here is investment advice; it is a rule transcription.
"""

from __future__ import annotations

import math

from tech import ema as ema_mod

BOX_CANDLES = {"1m": 10, "5m": 10, "1h": 10, "1d": 10, "1wk": 8, "1mo": 5}
TIGHT_RANGE_PCT = 3.0          # box height as % of close
COMPRESSION_PCT = 2.0          # EMA ribbon spread as % of close
OVEREXTENDED_PCT = 8.0         # 20 vs 200 EMA spread
VOLUME_SURGE = 1.5             # breakout volume vs 20-bar average

CLASS_BANDS = ((40, "VP"), (10, "P"), (-10, "NE"), (-40, "N"))


def classify(score):
    for floor, name in CLASS_BANDS:
        if score >= floor:
            return name
    return "VN"


def _volume_stats(bars):
    values = [float(bar.get("v") or 0.0) for bar in bars]
    if not any(values):
        return None, None
    recent = values[-21:-1]
    if len(recent) < 5:
        return values[-1], None
    average = sum(recent) / len(recent)
    return values[-1], average


def _box(bars, count):
    window = bars[-count - 1:-1] if len(bars) > count else bars[:-1]
    if not window:
        return None
    high = max(float(bar["h"]) for bar in window)
    low = min(float(bar["l"]) for bar in window)
    return {"candles": len(window), "high": high, "low": low,
            "range_pct": (high - low) / high * 100.0 if high else None}


def evaluate(tf, bars, volume_series=None, stats=None):
    """Full rule evaluation for one timeframe."""
    stats = stats or ema_mod.snapshot(bars)
    if not stats.get("ok"):
        return {"tf": tf, "ok": False, "reason": stats.get("reason", "no bars")}

    close = stats["close"]
    emas = {int(k): v for k, v in (stats.get("emas") or {}).items()}
    spread = stats.get("spread_pct")
    spread_200 = stats.get("spread_20_200_pct")

    box = _box(bars, BOX_CANDLES.get(tf, 10))

    # --- trend ---------------------------------------------------------
    trend_pts = 0
    above = 0
    for span in ema_mod.SPANS:
        value = emas.get(span)
        if value is None:
            continue
        if close >= value:
            trend_pts += 8
            above += 1
        else:
            trend_pts -= 8
    alignment = stats.get("alignment")
    align_pts = 20 if alignment == "bull" else -20 if alignment == "bear" else 0

    # --- gate ----------------------------------------------------------
    compression = spread is not None and spread <= COMPRESSION_PCT
    tight_box = box is not None and box["range_pct"] is not None and \
        box["range_pct"] <= TIGHT_RANGE_PCT
    gate_pts = (10 if compression else 0) + (10 if tight_box else 0)

    # --- breakout ------------------------------------------------------
    prev_close = float(bars[-2]["c"]) if len(bars) > 1 else close
    above_box = box is not None and close > box["high"]
    breakout = above_box and prev_close <= box["high"]

    if volume_series:
        values = [float(v) for v in volume_series if v is not None]
        volume = values[-1] if values else None
        average = (sum(values[-21:-1]) / len(values[-21:-1])
                   if len(values) >= 6 else None)
        volume_source = "proxy"
    else:
        volume, average = _volume_stats(bars)
        volume_source = "candle" if volume is not None else "unavailable"

    surge = (average is not None and average > 0 and volume is not None and
             volume >= VOLUME_SURGE * average)
    volume_pts = 0
    if breakout and volume is not None and average:
        volume_pts = 10 if surge else -10
    elif surge:
        volume_pts = 5

    breakout_pts = 25 if breakout else (8 if above_box else 0)

    # --- invalidation --------------------------------------------------
    overextended = spread_200 is not None and spread_200 > OVEREXTENDED_PCT
    below_50 = emas.get(50) is not None and close < emas[50]
    invalid_pts = (-20 if overextended else 0) + (-10 if below_50 else 0)

    raw = trend_pts + align_pts + gate_pts + breakout_pts + volume_pts + invalid_pts
    score = int(max(-100, min(100, raw)))

    if breakout:
        gate = "breakout"
    elif compression and tight_box:
        gate = "forming"
    elif overextended:
        gate = "extended"
    elif compression:
        gate = "compressing"
    else:
        gate = "none"

    return {
        "tf": tf,
        "ok": True,
        "close": close,
        "emas": {str(k): v for k, v in emas.items()},
        "spread_pct": spread,
        "spread_20_200_pct": spread_200,
        "alignment": alignment,
        "above_emas": above,
        "box": ({"candles": box["candles"],
                 "high": round(box["high"], 2),
                 "low": round(box["low"], 2),
                 "range_pct": round(box["range_pct"], 2)}
                if box else None),
        "compression": compression,
        "tight_box": tight_box,
        "breakout": breakout,
        "above_box": above_box,
        "volume": volume,
        "volume_avg20": average,
        "volume_surge": surge,
        "volume_source": volume_source,
        "overextended": overextended,
        "below_50": below_50,
        "score": score,
        "class": classify(score),
        "confidence": stats.get("confidence"),
        "components": {
            "trend": trend_pts, "alignment": align_pts, "gate": gate_pts,
            "breakout": breakout_pts, "volume": volume_pts,
            "invalidation": invalid_pts,
        },
        "gate": gate,
        "bars": stats.get("bars"),
        "last_bar_ts": stats.get("last_bar_ts"),
    }
