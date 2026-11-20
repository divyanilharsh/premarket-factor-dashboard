#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NSE archive candles for daily / weekly / monthly NIFTY 50.

Yahoo rate-limits candle requests hard, but NSE publishes one CSV per trading
day at a static archive URL:

    https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

Each file contains every index's open/high/low/close **and volume** for that
day.  This module downloads them once, caches the parsed rows, and resamples to
weekly and monthly.  Different host, so it never competes with the dashboards'
Yahoo budget.

    python3 v3/tech/nse_archive.py --days 1000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
V3_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(V3_DIR)
if V3_DIR not in sys.path:
    sys.path.insert(0, V3_DIR)

DATA_DIR = os.path.join(ROOT, "data", "v3")
CACHE_FILE = os.path.join(DATA_DIR, "candles", "nse_daily.json")
URL = ("https://nsearchives.nseindia.com/content/indices/"
       "ind_close_all_%s.csv")
IST = timezone(timedelta(hours=5, minutes=30))
INDEX_NAME = "nifty 50"
MIN_GAP_SECONDS = 0.35
TTL_DAILY = 6 * 3600

_session = None


def _session_get():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
            "Accept": "text/csv,*/*",
        })
    return _session


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"updated_ts": 0, "days": {}}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    cache["updated_ts"] = time.time()
    temp = CACHE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, separators=(",", ":"))
    os.replace(temp, CACHE_FILE)


def _parse_day(text):
    lines = text.splitlines()
    if not lines:
        return None
    header = [cell.strip().lower() for cell in lines[0].split(",")]
    try:
        name_i = header.index("index name")
        date_i = header.index("index date")
        o_i = header.index("open index value")
        h_i = header.index("high index value")
        l_i = header.index("low index value")
        c_i = header.index("closing index value")
    except ValueError:
        return None
    vol_i = header.index("volume") if "volume" in header else None
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) <= c_i:
            continue
        if cells[name_i].strip().lower() != INDEX_NAME:
            continue
        try:
            stamp = datetime.strptime(cells[date_i].strip(), "%d-%m-%Y")
            stamp = stamp.replace(hour=15, minute=30, tzinfo=IST)
            volume = 0.0
            if vol_i is not None and vol_i < len(cells):
                volume = float(cells[vol_i] or 0)
            return {
                "t": int(stamp.timestamp()),
                "o": float(cells[o_i]), "h": float(cells[h_i]),
                "l": float(cells[l_i]), "c": float(cells[c_i]),
                "v": volume,
            }
        except (TypeError, ValueError):
            return None
    return None


def fetch_day(day, cache, save_every=25, counter=None):
    key = day.strftime("%Y%m%d")
    if key in cache["days"]:
        return cache["days"][key]
    if day.weekday() >= 5:
        cache["days"][key] = None
        return None
    url = URL % day.strftime("%d%m%Y")
    try:
        response = _session_get().get(url, timeout=25)
    except Exception:
        return None
    if response.status_code == 200 and "Index Name" in response.text[:200]:
        bar = _parse_day(response.text)
        cache["days"][key] = bar
    elif response.status_code == 404:
        cache["days"][key] = None      # holiday or not published yet
    else:
        return None                    # transient: retry next run
    if counter is not None:
        counter[0] += 1
        if counter[0] % save_every == 0:
            save_cache(cache)
    time.sleep(MIN_GAP_SECONDS)
    return cache["days"][key]


def backfill(days=1000, end=None, progress=None):
    cache = load_cache()
    end = end or datetime.now(IST).date()
    counter = [0]
    cursor = end
    fetched = 0
    misses = 0
    while fetched < days and misses < 40:
        if cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
            continue
        bar = fetch_day(cursor, cache, counter=counter)
        if bar:
            fetched += 1
            misses = 0
        else:
            misses += 1
        cursor -= timedelta(days=1)
        if progress and counter[0] % 50 == 0 and counter[0]:
            progress(fetched, days, cursor)
    save_cache(cache)
    return fetched


def daily_bars():
    cache = load_cache()
    rows = [row for row in cache["days"].values() if row]
    rows.sort(key=lambda row: row["t"])
    return rows


def _resample(bars, key_of):
    groups = {}
    for bar in bars:
        moment = datetime.fromtimestamp(bar["t"], IST)
        groups.setdefault(key_of(moment), []).append(bar)
    out = []
    for key in sorted(groups):
        chunk = sorted(groups[key], key=lambda b: b["t"])
        out.append({
            "t": chunk[-1]["t"],
            "o": chunk[0]["o"],
            "h": max(b["h"] for b in chunk),
            "l": min(b["l"] for b in chunk),
            "c": chunk[-1]["c"],
            "v": sum(b["v"] for b in chunk),
        })
    return out


def weekly_bars():
    return _resample(daily_bars(),
                     lambda m: m.isocalendar()[:2])


def monthly_bars():
    return _resample(daily_bars(), lambda m: (m.year, m.month))


def get_series(tf, allow_fetch=True):
    """Same shape as candles.get_series for 1d/1wk/1mo."""
    cache = load_cache()
    if allow_fetch:
        age = time.time() - float(cache.get("updated_ts") or 0)
        if age > TTL_DAILY and (cache.get("days")):
            # only refresh the tail; the bulk is immutable history
            backfill(days=10)
    bars = {"1d": daily_bars, "1wk": weekly_bars,
            "1mo": monthly_bars}.get(tf, daily_bars)()
    return {"bars": bars, "stale": not bars,
            "fetched_ts": cache.get("updated_ts"),
            "error": "" if bars else "no NSE archive data"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="NSE archive backfill")
    parser.add_argument("--days", type=int, default=1000)
    args = parser.parse_args(argv)

    def progress(fetched, target, cursor):
        print("  %d/%d trading days (at %s)" % (fetched, target, cursor),
              flush=True)

    print("Downloading NSE index archive for NIFTY 50 ...")
    fetched = backfill(days=args.days, progress=progress)
    bars = daily_bars()
    print("Cached %d trading days (%s -> %s)"
          % (len(bars),
             datetime.fromtimestamp(bars[0]["t"], IST).date() if bars else "-",
             datetime.fromtimestamp(bars[-1]["t"], IST).date() if bars else "-"))
    print("Weekly bars : %d" % len(weekly_bars()))
    print("Monthly bars: %d" % len(monthly_bars()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
