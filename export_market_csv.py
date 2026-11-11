#!/usr/bin/env python3
"""Export every mark in the V2 SQLite database to one CSV file.

The export is read-only and always reads the complete database.  Each run
replaces the previous CSV, so only the newest full export remains.

Output layout: one row per factor per mark.  This keeps every factor field
readable in Excel instead of hiding it inside one JSON cell.  Predictions are
joined on the mark timestamp; predictions without a matching mark are still
written at the end.

Usage:
    python export_market_csv.py
    python export_market_csv.py --db data/v2/backups/market-20261111.db
    python export_market_csv.py --out data/exports/market_data.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "v2" / "market.db"
DEFAULT_OUT = ROOT / "data" / "exports" / "market_data.csv"

FACTOR_FIELD_ORDER = [
    "label",
    "value",
    "change",
    "direction",
    "status",
    "source",
    "details",
    "raw",
    "kind",
    "delta",
]


def open_database(path: Path) -> sqlite3.Connection:
    """Open the database without writing to it."""
    if not path.is_file():
        raise FileNotFoundError("Database not found: %s" % path)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(
        'PRAGMA table_info("%s")' % table)]


def read_rows(conn: sqlite3.Connection, table: str, order_by: str):
    return [dict(row) for row in conn.execute(
        'SELECT * FROM "%s" ORDER BY %s' % (table, order_by))]


def parse_factors(raw) -> list[dict]:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return [{"_parse_error": str(raw)[:500]}]
    if isinstance(data, dict):
        return [dict(value) if isinstance(value, dict) else {"value": value}
                for value in data.values()]
    return [{"value": data}]


def factor_key_for(raw, index):
    """Keep the original category::factor key when possible."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", "factor_%d" % index
    if isinstance(data, dict):
        keys = list(data.keys())
        if index < len(keys):
            return keys[index], keys[index]
    return "", "factor_%d" % index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the complete V2 database to one CSV file.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="SQLite database (default: %s)" % DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="CSV output path (default: %s)" % DEFAULT_OUT)
    args = parser.parse_args(argv)

    db_path = args.db.expanduser().resolve()
    out_path = args.out.expanduser().resolve()

    try:
        conn = open_database(db_path)
    except (OSError, sqlite3.Error) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    try:
        mark_cols = table_columns(conn, "marks")
        pred_cols = table_columns(conn, "predictions")
        if "factors_json" not in mark_cols:
            raise sqlite3.DatabaseError(
                "marks table has no factors_json column")

        marks = read_rows(conn, "marks", "ts ASC, id ASC")
        predictions = read_rows(conn, "predictions", "id ASC")

        # Work out every factor field used anywhere in the database, so a
        # future extra field is not silently dropped.
        factor_fields = list(FACTOR_FIELD_ORDER)
        seen_factors = set()
        for mark in marks:
            for factor in parse_factors(mark.get("factors_json")):
                for field in factor:
                    if field not in seen_factors:
                        seen_factors.add(field)
        for field in sorted(seen_factors):
            if field not in factor_fields:
                factor_fields.append(field)

        pred_by_base = {}
        for pred in predictions:
            try:
                base = float(pred.get("base_ts"))
            except (TypeError, ValueError):
                continue
            pred_by_base.setdefault(base, []).append(pred)

        mark_cols = [c for c in mark_cols if c != "factors_json"]
        header = (
            mark_cols
            + ["prediction_" + c for c in pred_cols]
            + ["factor_category", "factor_key"]
            + ["factor_" + f for f in factor_fields]
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = out_path.with_name(out_path.name + ".tmp")
        rows_written = 0
        matched_pred_ids = set()

        with open(temp_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)

            for mark in marks:
                raw_factors = mark.get("factors_json")
                factors = parse_factors(raw_factors)
                if not factors:
                    factors = [{}]

                try:
                    base_ts = float(mark.get("ts"))
                except (TypeError, ValueError):
                    base_ts = None
                preds = pred_by_base.get(base_ts) or [None]
                for popt in preds:
                    if popt is not None:
                        matched_pred_ids.add(popt.get("id"))
                    for index, factor in enumerate(factors):
                        key, short_key = factor_key_for(raw_factors, index)
                        category = key.split("::", 1)[0] if "::" in key else ""
                        row = [mark.get(c) for c in mark_cols]
                        if popt is None:
                            row.extend([None] * len(pred_cols))
                        else:
                            row.extend([popt.get(c) for c in pred_cols])
                        row.extend([category, key or short_key])
                        row.extend([factor.get(f) for f in factor_fields])
                        writer.writerow(row)
                        rows_written += 1

            # Keep predictions whose base mark is no longer present.
            for pred in predictions:
                if pred.get("id") in matched_pred_ids:
                    continue
                row = [None] * len(mark_cols)
                row.extend([pred.get(c) for c in pred_cols])
                row.extend([None, None])
                row.extend([None] * len(factor_fields))
                writer.writerow(row)
                rows_written += 1

        try:
            os.replace(temp_path, out_path)
        except PermissionError:
            temp_path.unlink(missing_ok=True)
            raise PermissionError(
                "Cannot replace %s. Close it in Excel/another program and "
                "run again." % out_path)

        print("Database : %s" % db_path)
        print("CSV      : %s" % out_path)
        print("Marks    : %d" % len(marks))
        print("Predictions: %d" % len(predictions))
        print("CSV rows : %d (one row per factor per mark)" % rows_written)
        print("Previous CSV replaced.")
        return 0
    except (sqlite3.Error, OSError, PermissionError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
