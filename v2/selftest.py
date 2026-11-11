#!/usr/bin/env python3
"""Offline self-test for the V2 long-term learner.

Run from the project root:

    python v2\\selftest.py

It uses only temporary files and never touches the live V1 or V2 stores.
"""

from __future__ import annotations

import importlib.util
import math
import os
import random
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(ROOT, "v2", "market_pulse_v2.py")
spec = importlib.util.spec_from_file_location("market_pulse_v2_selftest", MODULE)
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)

TMP = tempfile.mkdtemp(prefix="pulse_v2_selftest_")
v2.NODES_FILE = os.path.join(TMP, "nodes.json")
v2.MODEL_FILE = os.path.join(TMP, "model.json")


def make_marks(n=180, seed=7):
    rnd = random.Random(seed)
    returns = [0.45 * math.sin(i / 11.0) + rnd.gauss(0, 0.10)
               for i in range(n)]
    price = 20000.0
    marks = []
    for i in range(n):
        if i:
            price *= 1.0 + returns[i - 1] / 100.0
        nxt = returns[i] if i < n - 1 else 0.0
        pos = nxt * 18.0 + rnd.gauss(0, 0.5)
        neg = -nxt * 14.0 + rnd.gauss(0, 0.7)
        noise = rnd.gauss(0, 1.0)
        if i == n - 1:
            # Give the display effect a decisive current move.
            pos, neg, noise = 2.8, -2.2, 0.0
        factors = {}
        for key, raw in (("pos", pos), ("neg", neg), ("noise", noise),
                         ("zero", 0.0), ("missing", None)):
            factors["test::" + key] = {
                "label": key,
                "value": "" if raw is None else "%.4f" % raw,
                "change": "",
                "direction": "neutral",
                "status": "ok",
                "source": "selftest",
                "details": "",
                "raw": raw,
                "kind": "level",
                "delta": None if raw is None else raw,
            }
        marks.append({
            "ts": 1700000000 + i * 3600,
            "time": "t%04d" % i,
            "date": "2026-01-01",
            "bucket": "b%04d" % i,
            "nifty": round(price, 2),
            "nifty_day_pct": 0.0,
            "nifty_source": "selftest",
            "gap_hours": 1.0,
            "factors": factors,
            "imported": 0,
        })
    return marks


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  ok  " + message)


def main():
    print("V2 self-test")
    marks = make_marks()
    learner = v2.LongTermLearner()
    result = learner.analyze(marks)
    nodes = result["nodes"]
    print("power/effect nodes:")
    for key in ("pos", "neg", "noise", "zero", "missing"):
        node = nodes["test::" + key]
        print("  %-7s P=%5.1f E=%5.1f r=%s n=%d c=%.2f"
              % (key, node["power"], node["effect"],
                 node["corr"], node["samples"], node["confidence"]))

    check(nodes["test::pos"]["power"] > 60, "positive factor has high power")
    check(nodes["test::pos"]["sign"] == 1, "positive factor learns + sign")
    check(nodes["test::neg"]["power"] > 60, "negative factor has high power")
    check(nodes["test::neg"]["sign"] == -1, "negative factor learns - sign")
    check(nodes["test::noise"]["power"] < 35, "noise factor has low power")
    check(nodes["test::zero"]["power"] == 0, "constant-zero factor has no power")
    check(nodes["test::zero"]["effect"] == 50, "zero delta is neutral")
    check(nodes["test::missing"]["power"] == 0, "missing factor has no power")
    check(nodes["test::missing"]["effect"] == 50, "missing delta is neutral")
    check(nodes["test::pos"]["effect"] > 55, "positive learned effect is bullish")
    check(nodes["test::neg"]["effect"] > 55,
          "negative factor flips a negative move to bullish")

    # One crazy print must not own the correlation.
    out = make_marks(seed=8)
    out[90]["factors"]["test::pos"]["delta"] = 10000.0
    out_node = v2.LongTermLearner().analyze(out)["nodes"]["test::pos"]
    check(out_node["power"] > 50, "outlier does not destroy the real signal")

    # Generate a database and train the actual V2 net.
    store = v2.MarkStore(os.path.join(TMP, "selftest.db"), seed=False)
    for rec in marks:
        store.insert_mark(rec)
    tracker = v2.V2Tracker(store)
    print("network:")
    print("  status=%s samples=%d features=%s"
          % (tracker.nn.get("status"), tracker.nn.get("samples"),
             tracker.nn.get("features")))
    pred = tracker.nn.get("prediction")
    check(pred is not None, "network produces a prediction")
    check(tracker.nn.get("val_mae") is not None,
          "network has walk-forward validation")
    check(len(tracker.nn.get("top_features") or []) >= 2,
          "network selects powered factor inputs")
    print("  fit MAE=%s hit=%s | walk-forward MAE=%s hit=%s"
          % (tracker.nn.get("fit_mae"), tracker.nn.get("fit_hit"),
             tracker.nn.get("val_mae"), tracker.nn.get("val_hit")))

    # Prediction scoring path.
    pid = store.insert_prediction(marks[-1]["ts"], pred["pct"], pred["price"],
                                  pred["samples"])
    store.score_prediction(pid, marks[-1]["ts"] + 3600, 0.20)
    stats = store.prediction_stats()
    check(stats["n"] == 1 and stats["mae"] is not None,
          "prediction scoring works")

    print("SELFTEST OK")


if __name__ == "__main__":
    main()
