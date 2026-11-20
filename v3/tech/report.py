#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot EMA / Golden Gate report for NIFTY 50.

    python3 v3/tech/report.py                 # all six timeframes
    python3 v3/tech/report.py --no-fetch      # cache only
    python3 v3/tech/report.py --force         # refetch everything
    python3 v3/tech/report.py --days 30 --csv data/exports/v3_ema_30d.csv

First run backfills history and is deliberately slow (one request every few
seconds so V1/V2 keep their own Yahoo budget).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
V3_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(V3_DIR)
for path in (V3_DIR, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from tech import candles, scan  # noqa: E402
from tech import ema as ema_mod  # noqa: E402

DEFAULT_CSV = os.path.join(ROOT, "data", "exports", "v3_ema_30d.csv")


def print_table(payload):
    header = ("TF", "bars", "close", "EMA20", "EMA50", "EMA100", "EMA150",
              "EMA200", "spr%", "align", "gate", "score", "class", "conf")
    print("%-4s %6s %10s %10s %10s %10s %10s %10s %6s %-6s %-10s %6s %-4s %-6s"
          % header)
    print("-" * 116)
    for tf in payload["order"]:
        frame = payload["frames"].get(tf) or {}
        if not frame.get("ok"):
            print("%-4s  %s" % (tf, frame.get("error") or "no data"))
            continue
        emas = frame.get("emas") or {}

        def value(span):
            v = emas.get(str(span))
            return ("%.2f" % v) if v is not None else "-"

        print("%-4s %6d %10.2f %10s %10s %10s %10s %10s %6s %-6s %-10s %6d %-4s %-6s"
              % (tf, frame.get("bars") or 0, frame.get("close") or 0,
                 value(20), value(50), value(100), value(150), value(200),
                 ("%.2f" % frame["spread_pct"])
                 if frame.get("spread_pct") is not None else "-",
                 frame.get("alignment") or "-", frame.get("gate") or "-",
                 frame.get("score") or 0, frame.get("class") or "-",
                 frame.get("quality") or "-"))


def write_csv(payload, path, days):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timeframe", "time_ist", "close", "ema20", "ema50",
                         "ema100", "ema150", "ema200"])
        for tf in payload["order"]:
            frame = payload["frames"].get(tf) or {}
            for point in frame.get("points") or []:
                writer.writerow([
                    tf,
                    time_text(point["t"]),
                    point.get("c"),
                    point.get("ema20"), point.get("ema50"), point.get("ema100"),
                    point.get("ema150"), point.get("ema200"),
                ])
                rows += 1
    return rows


def time_text(ts):
    from datetime import datetime
    return datetime.fromtimestamp(int(ts), candles.IST).strftime("%Y-%m-%d %H:%M")


def main(argv=None):
    parser = argparse.ArgumentParser(description="NIFTY 50 EMA / Golden Gate report")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-csv", action="store_true")
    args = parser.parse_args(argv)

    payload = scan.scan(allow_fetch=not args.no_fetch, force=args.force,
                        days=args.days, fetch_order=candles.BACKFILL_ORDER)
    print("NIFTY 50 technical scan | %s | market %s"
          % (payload["updated"], "OPEN" if payload["market_open"] else "CLOSED"))
    print("")
    print_table(payload)
    print("")
    for tf in payload["order"]:
        frame = payload["frames"].get(tf) or {}
        if frame.get("ok"):
            print("%-4s volume: %-12s | gate: %-10s | box: %s"
                  % (tf, frame.get("volume_source"),
                     frame.get("gate"),
                     (("high %.2f low %.2f (%.2f%%)"
                       % (frame["box"]["high"], frame["box"]["low"],
                          frame["box"]["range_pct"]))
                      if frame.get("box") else "-")))
    if not args.no_csv:
        rows = write_csv(payload, args.csv, args.days)
        print("\nCSV: %s (%d rows, last %d days)" % (args.csv, rows, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
