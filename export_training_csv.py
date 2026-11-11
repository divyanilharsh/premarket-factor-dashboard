#!/usr/bin/env python3
"""Build a supervised training CSV from the V2 SQLite database.

One row = one valid hourly pair:

    base mark factor deltas  ->  next mark NIFTY return (%)

Overnight and long-gap pairs are skipped, matching the V2 learner.  The
existing training CSV is replaced each run.

Usage:
    python export_training_csv.py
    python export_training_csv.py --db data/v2/backups/market-20261110.db
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "v2" / "market.db"
DEFAULT_OUT = ROOT / "data" / "exports" / "training_data.csv"
MAX_GAP_HOURS = 2.5
MIN_V2_SAMPLES = 60


def safe_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_marks(conn):
    rows = conn.execute(
        "SELECT id, ts, time, date, bucket, nifty, nifty_day_pct, "
        "gap_hours, factors_json FROM marks ORDER BY ts ASC, id ASC")
    marks = []
    for row in rows:
        data = dict(row)
        try:
            data["factors"] = json.loads(data.get("factors_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data["factors"] = {}
        if not isinstance(data["factors"], dict):
            data["factors"] = {}
        marks.append(data)
    return marks


def momentum(marks, index):
    """Same four momentum inputs used by the V2 neural net."""
    current = marks[index]
    out = []
    for lag in (1, 2, 3):
        previous = marks[index - lag] if index - lag >= 0 else None
        if not previous or previous.get("date") != current.get("date"):
            out.append(0.0)
            continue
        na = safe_float(previous.get("nifty"))
        nb = safe_float(current.get("nifty"))
        if not na or not nb:
            out.append(0.0)
        else:
            out.append(math.tanh(((nb - na) / na * 100.0) / 0.5))
    day = safe_float(current.get("nifty_day_pct")) or 0.0
    out.append(math.tanh(day / 1.5))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create one training row per valid hourly NIFTY pair.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-gap-hours", type=float, default=MAX_GAP_HOURS)
    args = parser.parse_args(argv)

    db_path = args.db.expanduser().resolve()
    out_path = args.out.expanduser().resolve()

    if not db_path.is_file():
        print("ERROR: database not found: %s" % db_path, file=sys.stderr)
        return 1

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        marks = load_marks(conn)
    except (sqlite3.Error, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()

    keys = sorted({
        key
        for mark in marks
        for key in (mark.get("factors") or {})
    })
    factor_columns = ["factor_" + key for key in keys]
    header = [
        "base_mark_id", "base_ts", "base_date", "base_time",
        "next_mark_id", "next_date", "next_time", "gap_hours",
        "nifty", "nifty_day_pct", "next_nifty", "target_next_pct",
        "momentum_1", "momentum_2", "momentum_3", "momentum_day",
        "factor_count",
    ] + factor_columns

    rows = []
    for index in range(len(marks) - 1):
        base = marks[index]
        nxt = marks[index + 1]
        gap = (float(nxt["ts"]) - float(base["ts"])) / 3600.0
        if gap <= 0 or gap > args.max_gap_hours:
            continue
        base_gap = safe_float(base.get("gap_hours"))
        if base_gap is not None and base_gap > args.max_gap_hours:
            continue
        base_nifty = safe_float(base.get("nifty"))
        next_nifty = safe_float(nxt.get("nifty"))
        if not base_nifty or not next_nifty:
            continue
        target = (next_nifty - base_nifty) / base_nifty * 100.0
        if not math.isfinite(target):
            continue

        base_factors = base.get("factors") or {}
        factor_values = []
        present = 0
        for key in keys:
            factor = base_factors.get(key) or {}
            delta = safe_float(factor.get("delta"))
            if delta is not None:
                present += 1
            factor_values.append(delta if delta is not None else "")

        rows.append([
            base.get("id"), base.get("ts"), base.get("date"), base.get("time"),
            nxt.get("id"), nxt.get("date"), nxt.get("time"), round(gap, 3),
            base_nifty, base.get("nifty_day_pct"), next_nifty, round(target, 5),
            *[round(x, 6) for x in momentum(marks, index)],
            present,
            *factor_values,
        ])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(temp_path, out_path)
    except PermissionError:
        temp_path.unlink(missing_ok=True)
        print("ERROR: close %s in Excel and run again." % out_path,
              file=sys.stderr)
        return 1
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    print("Database      : %s" % db_path)
    print("Training CSV  : %s" % out_path)
    print("Valid pairs   : %d" % len(rows))
    print("Factor inputs : %d" % len(keys))
    print("Total columns : %d" % len(header))
    if len(rows) < MIN_V2_SAMPLES:
        print("V2 minimum    : %d pairs" % MIN_V2_SAMPLES)
        print("Still needed  : %d valid hourly pairs" %
              (MIN_V2_SAMPLES - len(rows)))
    else:
        print("Ready for V2 training.")
    print("Previous training CSV replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
