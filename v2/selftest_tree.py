#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline self-test for the decision tree (training, export, inference).

Run from the project root:

    python v2/selftest_tree.py

Uses synthetic data plus the real exported model if one exists.  Never writes
to the live database.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tree_model as tm  # noqa: E402

FAILS = []


def check(condition, label):
    if condition:
        print("  ok  %s" % label)
    else:
        print("  FAIL %s" % label)
        FAILS.append(label)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synth_marks(count=140, seed=5):
    rnd = random.Random(seed)
    price = 20000.0
    marks = []
    day = "2026-11-16"
    for i in range(count):
        if i and i % 7 == 0:
            day = "2026-11-%02d" % (16 + i // 7)
        ret = 0.25 * math.sin(i / 9.0) + rnd.gauss(0, 0.22)
        price *= 1.0 + ret / 100.0
        hour = 9 + (i % 7)
        factors = {}
        for key in ("1. CRUDE OIL::brent", "2. CURRENCY::usd_inr",
                    "4. GLOBAL MARKETS::dow", "9. SECTOR SIGNALS::banks"):
            delta = ret * rnd.uniform(0.2, 1.8) + rnd.gauss(0, 0.05)
            factors[key] = {"delta": round(delta, 4),
                            "direction": "bullish" if delta > 0 else "bearish"}
        marks.append({
            "id": i, "ts": 1_790_000_000 + i * 3600,
            "time": "%s %02d:15 IST" % (day, hour), "date": day,
            "nifty": round(price, 2),
            "nifty_day_pct": round(ret, 3),
            "gap_hours": 1.0,
            "factors": factors,
        })
    return marks


def main():
    train = load_module("train_tree_selftest",
                        os.path.join(ROOT, "v2", "train_tree.py"))

    print("labels:")
    cuts = {"vn_max": -0.30, "n_max": -0.10, "ne_max": 0.10, "p_max": 0.30}
    check(tm.label_for(-0.31, cuts) == 0, "below vn_max is VN")
    check(tm.label_for(-0.30, cuts) == 0, "exactly vn_max is VN")
    check(tm.label_for(-0.29, cuts) == 1, "just above vn_max is N")
    check(tm.label_for(0.0, cuts) == 2, "zero is NE")
    check(tm.label_for(0.10, cuts) == 2, "exactly ne_max is NE")
    check(tm.label_for(0.11, cuts) == 3, "just above ne_max is P")
    check(tm.label_for(0.30, cuts) == 3, "exactly p_max is P")
    check(tm.label_for(0.31, cuts) == 4, "above p_max is VP")

    marks = synth_marks()
    pairs = train.build_pairs(marks, 2.5)
    keys = train.factor_keys(marks, pairs, 0.4)
    check(len(pairs) > 100, "synthetic pairs built (%d)" % len(pairs))
    check(len(keys) == 4, "factor coverage filter keeps all 4 keys")

    targets = [t for _i, t in pairs]
    auto_cuts = train.make_cuts(targets)
    labels = train.label_array(targets, auto_cuts)
    counts = [int((labels == i).sum()) for i in range(5)]
    check(all(c > 0 for c in counts), "quantile cuts populate all 5 classes")
    check(max(counts) - min(counts) <= max(2, len(targets) // 12),
          "quantile classes are roughly balanced %s" % counts)

    print("features:")
    names, X = train.feature_matrix(marks, pairs, keys)
    check(X.shape[0] == len(pairs), "one row per pair")
    check(X.shape[1] == len(names), "column count matches feature names")
    check(any(math.isnan(v) for row in X for v in row) is False,
          "synthetic matrix has no unexpected NaN")

    base_index = pairs[0][0]
    row_a = tm.feature_row(marks, base_index, keys)
    tampered = [dict(m) for m in marks]
    tampered[base_index + 1] = dict(tampered[base_index + 1],
                                    nifty=999999.0, nifty_day_pct=99.0)
    row_b = tm.feature_row(tampered, base_index, keys)
    check(row_a == row_b, "feature row ignores the next mark (no leakage)")

    export = load_module("export_training_csv_selftest",
                         os.path.join(ROOT, "export_training_csv.py"))
    check(tm.momentum(marks, 5) == export.momentum(marks, 5),
          "momentum matches export_training_csv")

    print("tree export:")
    args = types.SimpleNamespace(depth=4, leaf=8)
    y = train.label_array(targets, auto_cuts)
    model = train.new_tree(args).fit(X, y)
    exported = train.export_node(model, names, 0)
    check(train.node_count(exported) > 1, "tree has more than one node")
    check(train.tree_depth(exported) <= 5, "tree depth respects the cap")

    mismatches = 0
    for i in range(len(X)):
        row = dict(zip(names, X[i].tolist()))
        proba, _n = tm.predict_proba({"tree": exported}, row)
        if int(max(range(5), key=lambda k: proba[k])) != int(model.predict(X[i:i + 1])[0]):
            mismatches += 1
    check(mismatches == 0, "JSON walker matches sklearn on %d rows" % len(X))

    proba, leaf_n = tm.predict_proba({"tree": exported}, dict(zip(names, X[0].tolist())))
    check(abs(sum(proba) - 1.0) < 0.02, "leaf probabilities sum to 1")
    check(leaf_n > 0, "leaf reports its sample count")

    print("missing model:")
    tmp = tempfile.mkdtemp(prefix="pulse_tree_selftest_")
    empty = tm.SharedTree(os.path.join(tmp, "nope.json"))
    check(empty.get() is None, "absent model file loads as None")
    payload = empty.payload(marks)
    check(payload["status"] == "missing", "absent model reports missing")
    check(payload["prediction"] is None, "absent model makes no prediction")

    print("real model:")
    real = tm.load()
    if real:
        check(real.get("classes") == list(tm.CLASSES), "model has the 5 classes")
        check(real.get("status") in ("learning", "ready"), "model status is valid")
        check(bool(real.get("tree")), "model has an exported tree")
        real_payload = tm.describe(real, marks[-8:])
        check(real_payload["status"] in ("learning", "ready"),
              "describe() returns a valid status")
        if real_payload["probabilities"]:
            check(abs(sum(real_payload["probabilities"]) - 1.0) < 0.02,
                  "served probabilities sum to 1")
        if real_payload["status"] == "learning":
            check(real_payload["prediction"] is None and "preview" in real_payload,
                  "learning model exposes preview but no prediction")
        else:
            check(real_payload["prediction"] is not None,
                  "ready model exposes a prediction")
    else:
        print("  --  no data/v2/tree_model.json yet (run v2/train_tree.py)")

    print("")
    if FAILS:
        print("SELFTEST FAILED: %d check(s)" % len(FAILS))
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
