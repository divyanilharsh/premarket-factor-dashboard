#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NSE live intraday fallback for the V3 technical lab.

The NSE chart endpoint serves the current session's intraday series and is
empty outside market hours.  It returns price points (no OHLC, no volume), so
bars are built as close-only.  That is enough for an EMA ribbon.

This exists because Yahoo rate-limits intraday candle requests per IP and the
dashboards share that IP.  When Yahoo refuses, NSE usually still answers.
"""

from __future__ import annotations

import sys
import time

import market_mobile as mm

PATH = "/api/chart-databyindex?index=NIFTY%2050&indices=true"
REFERER = "https://www.nseindia.com/market-data/live-market-indices"

_last = [0.0]
MIN_GAP = 30.0          # one NSE intraday read per 30 seconds at most
CACHE = {"bars": [], "ts": 0.0}


def fetch_1m(force=False):
    """Return close-only 1-minute bars for the current session, or []."""
    now = time.time()
    if not force and now - CACHE["ts"] < MIN_GAP:
        return CACHE["bars"]
    if now - _last[0] < MIN_GAP:
        return CACHE["bars"]
    _last[0] = now
    try:
        payload = mm.NseClient.get().api(PATH, referer=REFERER)
    except Exception:
        return CACHE["bars"]
    rows = (payload or {}).get("grapthData") or []
    if not rows:
        return CACHE["bars"]
    bars = {}
    for row in rows:
        try:
            stamp, price = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if stamp > 1e11:          # milliseconds
            stamp /= 1000.0
        minute = int(stamp) // 60 * 60
        bars[minute] = price
    out = [{"t": minute, "o": price, "h": price, "l": price,
            "c": price, "v": 0.0} for minute, price in sorted(bars.items())]
    if out:
        CACHE["bars"] = out
        CACHE["ts"] = now
    return CACHE["bars"]
