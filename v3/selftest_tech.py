#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline self-test for the V3 technical module (no network).

    python3 v3/selftest_tech.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tech import candles, ema, golden_gate  # noqa: E402

FAILS = []


def check(condition, label):
    if condition:
        print("  ok  %s" % label)
    else:
        print("  FAIL %s" % label)
        FAILS.append(label)


def make_bars(closes, volumes=None, step=3600, start=1_790_000_000):
    volumes = volumes or [1000] * len(closes)
    out = []
    for index, close in enumerate(closes):
        out.append({
            "t": start + index * step,
            "o": close, "h": close * 1.001, "l": close * 0.999,
            "c": close, "v": volumes[index],
        })
    return out


def main():
    print("ema:")
    flat = [100.0] * 220
    series = ema.ema_series(flat, 20)
    check(series[18] is None, "EMA has no value before its warmup")
    check(abs(series[-1] - 100.0) < 1e-9, "flat series gives EMA == price")
    rising = ema.ema_series([float(x) for x in range(1, 121)], 20)
    check(rising[-1] < 120 and rising[-1] > 100,
          "EMA lags a rising series (%.2f)" % rising[-1])
    check(ema.confidence(1000, 200) == "high", "1000 bars / 200 span = high")
    check(ema.confidence(400, 200) == "low", "400 bars / 200 span = low")
    check(ema.confidence(700, 200) == "medium", "700 bars / 200 span = medium")

    print("alignment:")
    up = make_bars([100 + i for i in range(240)])
    snapshot = ema.snapshot(up)
    check(snapshot["alignment"] == "bull", "rising series is bullish")
    down = make_bars([400 - i for i in range(240)])
    check(ema.snapshot(down)["alignment"] == "bear", "falling series is bearish")
    check(ema.snapshot(make_bars([100] * 240))["alignment"] == "mixed",
          "flat series is mixed")

    print("golden gate:")
    box_closes = [100 + (i % 3) * 0.4 for i in range(30)]
    box = make_bars(box_closes)
    state = golden_gate.evaluate("1d", box)
    check(state["ok"], "evaluation returns ok")
    check(state["gate"] in ("none", "compressing", "forming"),
          "flat box is not a breakout")
    check(state["box"] is not None and state["box"]["range_pct"] < 5,
          "box range is tight")

    breakout_closes = box_closes[:-1] + [106.0]
    breakout_volumes = [1000] * (len(breakout_closes) - 1) + [4000]
    broke = golden_gate.evaluate("1d", make_bars(breakout_closes, breakout_volumes))
    check(broke["breakout"] is True, "close above box high is a breakout")
    check(broke["volume_surge"] is True, "4x volume is a surge")
    check(broke["gate"] == "breakout", "gate state is breakout")
    check(broke["score"] > 0, "breakout scores positive (%d)" % broke["score"])
    check(broke["class"] in ("P", "VP"), "breakout maps to P/VP")

    weak = golden_gate.evaluate("1d", make_bars(breakout_closes, [1000] * len(breakout_closes)))
    check(weak["volume_surge"] is False, "flat volume is not a surge")
    check(weak["score"] < broke["score"],
          "weak-volume breakout scores lower")

    print("classification:")
    check(golden_gate.classify(80) == "VP", "80 -> VP")
    check(golden_gate.classify(20) == "P", "20 -> P")
    check(golden_gate.classify(0) == "NE", "0 -> NE")
    check(golden_gate.classify(-20) == "N", "-20 -> N")
    check(golden_gate.classify(-80) == "VN", "-80 -> VN")

    print("market clock:")
    ist = candles.IST
    check(candles.market_open(datetime(2026, 11, 20, 10, 0, tzinfo=ist)) is True,
          "Friday 10:00 IST is open")
    check(candles.market_open(datetime(2026, 11, 20, 16, 0, tzinfo=ist)) is False,
          "Friday 16:00 IST is closed")
    check(candles.market_open(datetime(2026, 11, 21, 10, 0, tzinfo=ist)) is False,
          "Saturday is closed")

    print("")
    if FAILS:
        print("SELFTEST FAILED: %d check(s)" % len(FAILS))
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
