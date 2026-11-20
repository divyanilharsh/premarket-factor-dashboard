#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patient one-time backfill for the V3 technical module.

Yahoo allows only a small number of candle requests per window from one IP.
This script fetches one timeframe at a time, waits out 429 cooldowns, and
resumes from cache, so it can run in the background for as long as needed.

    python3 v3/tech/backfill.py
    python3 v3/tech/backfill.py --gap 60 --max-wait 3600

Already-cached timeframes are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
V3_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(V3_DIR)
for path in (V3_DIR, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from tech import candles, store, scan  # noqa: E402


def log(message):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill V3 candles")
    parser.add_argument("--gap", type=float, default=45.0,
                        help="seconds between candle requests (default 45)")
    parser.add_argument("--max-wait", type=float, default=3600.0,
                        help="overall budget in seconds (default 3600)")
    parser.add_argument("--retry-wait", type=float, default=120.0)
    args = parser.parse_args(argv)

    candles.MIN_GAP_SECONDS = max(8.0, args.gap)
    store.init()
    started = time.time()
    pending = list(candles.BACKFILL_ORDER)

    while pending and time.time() - started < args.max_wait:
        tf = pending[0]
        result = candles.get_series(tf, allow_fetch=True, force=True)
        bars = result["bars"]
        if bars:
            log("%-4s cached %d bars" % (tf, len(bars)))
            pending.pop(0)
            continue
        log("%-4s failed: %s (waiting %.0fs)" % (tf, result["error"][:60],
                                                 args.retry_wait))
        if time.time() - started >= args.max_wait:
            break
        time.sleep(max(args.retry_wait, candles.MIN_GAP_SECONDS))

    payload = scan.scan(allow_fetch=False)
    ok = [tf for tf, frame in (payload.get("frames") or {}).items()
          if frame.get("ok")]
    log("done: %d/%d timeframes cached (%s)"
        % (len(ok), len(candles.ORDER), ", ".join(sorted(ok)) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
