#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan all six timeframes and produce the shared technical payload."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

from tech import candles, ema, golden_gate, store

LATEST_FILE = os.path.join(candles.DATA_DIR, "tech_latest.json")

MAX_POINTS = 300


def _stamp():
    return datetime.now(candles.IST).strftime("%Y-%m-%d %H:%M:%S")


def _points(bars, series, days, limit=MAX_POINTS):
    """Last ``days`` of EMA values, trimmed to ``limit`` points for the API."""
    if not bars:
        return []
    cutoff = bars[-1]["t"] - int(days) * 86400
    rows = []
    for index, bar in enumerate(bars):
        if bar["t"] < cutoff:
            continue
        row = {"t": int(bar["t"]), "c": round(float(bar["c"]), 2)}
        for key, values in series.items():
            value = values[index]
            row[key] = round(value, 2) if value is not None else None
        rows.append(row)
    if len(rows) > limit:
        step = max(1, len(rows) // limit)
        rows = rows[::step][-limit:]
    return rows


def scan(allow_fetch=True, force=False, days=30, persist=True, only=None,
         fetch_order=None):
    if persist:
        store.init()
    frames = {}
    for tf in (fetch_order or candles.ORDER):
        if only and tf != only:
            series = candles.get_series(tf, allow_fetch=False)
        else:
            series = candles.get_series(tf, allow_fetch=allow_fetch, force=force)
        bars = series["bars"]
        if not bars:
            frames[tf] = {"ok": False, "tf": tf, "error": series["error"],
                          "stale": True, "bars": 0}
            continue
        stats = ema.snapshot(bars)
        signal = golden_gate.evaluate(tf, bars, stats=stats)
        signal["stale"] = bool(series["stale"])
        signal["error"] = series["error"]
        signal["bars"] = len(bars)
        signal["quality"] = stats.get("confidence")
        signal["points"] = _points(bars, ema.ribbon(bars), days)
        frames[tf] = signal
        if persist:
            try:
                store.save_timeframe(tf, bars[-6000:], signal)
            except Exception as exc:
                signal["store_error"] = str(exc)

    payload = {
        "updated": _stamp(),
        "updated_ts": time.time(),
        "market_open": candles.market_open(),
        "symbol": candles.SYMBOL,
        "frames": frames,
        "order": list(candles.ORDER),
        "counts": store.counts() if persist else {},
        "fetch_spacing_seconds": candles.MIN_GAP_SECONDS,
    }
    if persist:
        os.makedirs(candles.DATA_DIR, exist_ok=True)
        temp = LATEST_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), default=str)
        os.replace(temp, LATEST_FILE)
    return payload


def load_latest():
    try:
        with open(LATEST_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def next_due():
    return candles.next_due_tf()
