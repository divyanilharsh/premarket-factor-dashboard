#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train the factor decision tree (VP / P / NE / N / VN).

Reads ``data/v3/market.db`` read-only, builds one row per valid hourly pair,
labels the next-hour NIFTY return with quantile bands, trains a shallow
decision tree and exports it as plain JSON for the running dashboards.

Benchmarks (RandomForest, HistGradientBoosting, majority class) are scored on
the same walk-forward folds but are **not** exported - only the tree is served.

Run:
    python v3/train_tree.py
    python v3/train_tree.py --min-pairs 40      # preview only, not a score
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import time

import numpy as np

from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import (balanced_accuracy_score, confusion_matrix,
                             f1_score, recall_score)
from sklearn.tree import DecisionTreeClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tree_model as tm  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "v3", "market.db")
DEFAULT_OUT = os.path.join(ROOT, "data", "v3", "tree_model.json")
DEFAULT_RULES = os.path.join(ROOT, "data", "exports", "v3_tree_rules.txt")
DEFAULT_CONFUSION = os.path.join(ROOT, "data", "exports", "v3_tree_confusion.csv")
HISTORY = os.path.join(ROOT, "data", "v3", "tree_history.json")

DEFAULT_MIN_PAIRS = 120
N_CLASSES = len(tm.CLASSES)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_marks(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, ts, time, date, bucket, nifty, nifty_day_pct, "
            "gap_hours, factors_json FROM marks ORDER BY ts ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()
    marks = []
    for row in rows:
        data = dict(row)
        try:
            factors = json.loads(data.pop("factors_json") or "{}")
        except ValueError:
            factors = {}
        data["factors"] = factors if isinstance(factors, dict) else {}
        marks.append(data)
    return marks


def build_pairs(marks, max_gap_hours):
    """(base_index, next_hour_return_pct) for valid consecutive marks."""
    pairs = []
    for index in range(len(marks) - 1):
        base, nxt = marks[index], marks[index + 1]
        gap = (float(nxt["ts"]) - float(base["ts"])) / 3600.0
        if gap <= 0 or gap > max_gap_hours:
            continue
        base_gap = tm._safe_float(base.get("gap_hours"))
        if base_gap is not None and base_gap > max_gap_hours:
            continue
        base_nifty = tm._safe_float(base.get("nifty"))
        next_nifty = tm._safe_float(nxt.get("nifty"))
        if not base_nifty or not next_nifty:
            continue
        target = (next_nifty - base_nifty) / base_nifty * 100.0
        if not math.isfinite(target):
            continue
        pairs.append((index, target))
    return pairs


def factor_keys(marks, pairs, min_coverage):
    """Factor keys whose delta is present for at least ``min_coverage`` of pairs."""
    counts = {}
    for index, _target in pairs:
        for key, value in (marks[index].get("factors") or {}).items():
            if isinstance(value, dict) and tm._safe_float(value.get("delta")) is not None:
                counts[key] = counts.get(key, 0) + 1
    needed = max(2, int(math.ceil(min_coverage * len(pairs))))
    return sorted(key for key, seen in counts.items() if seen >= needed)


def feature_matrix(marks, pairs, keys):
    names = ["f:" + key for key in keys] + list(tm.META_KEYS)
    rows = []
    for index, _target in pairs:
        row = tm.feature_row(marks, index, keys)
        rows.append([np.nan if tm._is_missing(row.get(name)) else float(row[name])
                     for name in names])
    return names, np.asarray(rows, dtype=float)


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def make_cuts(targets):
    q = np.percentile(np.asarray(targets, dtype=float), [20, 40, 60, 80])
    return {"vn_max": float(q[0]), "n_max": float(q[1]),
            "ne_max": float(q[2]), "p_max": float(q[3])}


def label_array(targets, cuts):
    return np.asarray([tm.label_for(float(v), cuts) for v in targets], dtype=int)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_block(y_true, y_pred):
    labels = list(range(N_CLASSES))
    matrix = confusion_matrix(y_true, y_pred, labels=labels)
    recalls = recall_score(y_true, y_pred, labels=labels, average=None,
                           zero_division=0)
    return {
        "n": int(len(y_true)),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=labels,
                                         average="macro", zero_division=0)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 4),
        "per_class_recall": {tm.CLASSES[i]: round(float(recalls[i]), 4)
                             for i in labels},
        "confusion": matrix.tolist(),
        "predicted_distribution": {
            tm.CLASSES[i]: int(matrix[:, i].sum()) for i in labels},
    }


def majority_baseline(y_train, y_test):
    values, counts = np.unique(y_train, return_counts=True)
    pick = int(values[int(np.argmax(counts))])
    return np.full(len(y_test), pick, dtype=int)


def new_tree(args):
    return DecisionTreeClassifier(
        max_depth=args.depth,
        min_samples_leaf=args.leaf,
        class_weight="balanced",
        random_state=42,
    )


def new_forest(args):
    return RandomForestClassifier(
        n_estimators=200,
        max_depth=args.depth + 2,
        min_samples_leaf=max(2, args.leaf // 3),
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )


def new_boosting(args):
    return HistGradientBoostingClassifier(
        max_iter=200,
        max_depth=3,
        learning_rate=0.06,
        class_weight="balanced",
        random_state=42,
    )


def walk_forward(X, targets, cuts, min_pairs, step, args):
    """Expanding-window validation with a 1-mark embargo.

    Cuts are frozen from the first training window so every fold is scored on
    the same class definition.  Folds never train on the mark they predict.
    """
    n = len(targets)
    if n < min_pairs + 1:
        return None

    models = {
        "tree": new_tree,
        "random_forest": new_forest,
        "hist_gradient_boosting": new_boosting,
    }
    collected = {name: ([], []) for name in models}
    collected["majority"] = ([], [])
    folds = 0

    for start in range(min_pairs, n, step):
        train_targets = targets[:start]
        test_targets = targets[start:start + step]
        y_train = label_array(train_targets, cuts)
        y_test = label_array(test_targets, cuts)
        X_train, X_test = X[:start], X[start:start + step]

        collected["majority"][0].extend(y_test)
        collected["majority"][1].extend(majority_baseline(y_train, y_test))

        for name, factory in models.items():
            try:
                model = factory(args)
                model.fit(X_train, y_train)
                collected[name][0].extend(y_test)
                collected[name][1].extend(model.predict(X_test))
            except Exception as exc:  # keep the tree result even if a benchmark fails
                collected[name][0].extend(y_test)
                collected[name][1].extend([-1] * len(y_test))
                collected.setdefault("errors", {})[name] = str(exc)
        folds += 1

    results = {}
    for name, (truth, pred) in collected.items():
        if not truth:
            continue
        truth_arr = np.asarray(truth)
        pred_arr = np.asarray(pred)
        mask = pred_arr >= 0
        results[name] = score_block(truth_arr[mask], pred_arr[mask])
    results["folds"] = folds
    results["step"] = step
    results["cuts"] = cuts
    return results


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_node(estimator, names, node_id):
    tree = estimator.tree_
    left = int(tree.children_left[node_id])
    right = int(tree.children_right[node_id])
    if left == -1:
        counts = np.zeros(N_CLASSES, dtype=float)
        raw = tree.value[node_id][0]
        for position, klass in enumerate(estimator.classes_):
            counts[int(klass)] = float(raw[position])
        total = counts.sum() or 1.0
        return {"p": [round(float(c / total), 4) for c in counts],
                "n": int(tree.n_node_samples[node_id])}
    missing = getattr(tree, "missing_go_to_left", None)
    goes_left = int(bool(missing[node_id])) if missing is not None else 0
    return {
        "f": names[int(tree.feature[node_id])],
        "t": float(tree.threshold[node_id]),
        "m": goes_left,
        "l": export_node(estimator, names, left),
        "r": export_node(estimator, names, right),
    }


def node_count(node):
    if "f" not in node:
        return 1
    return 1 + node_count(node["l"]) + node_count(node["r"])


def tree_depth(node):
    if "f" not in node:
        return 1
    return 1 + max(tree_depth(node["l"]), tree_depth(node["r"]))


def pretty_feature(name):
    if name.startswith("f:"):
        return name[2:]
    return name


def rule_lines(node, indent=0, lines=None):
    lines = [] if lines is None else lines
    pad = "  " * indent
    if "f" not in node:
        best = int(np.argmax(node.get("p") or [0]))
        proba = (node.get("p") or [0])[best]
        lines.append("%s-> %s  (%.0f%%, n=%d)"
                     % (pad, tm.CLASSES[best], 100.0 * proba, node.get("n") or 0))
        return lines
    name = pretty_feature(node["f"])
    missing = " (missing)" if node.get("m") else ""
    lines.append("%sIF %s <= %.6g%s" % (pad, name, node["t"], missing))
    rule_lines(node["l"], indent + 1, lines)
    lines.append("%sELSE %s > %.6g" % (pad, name, node["t"]))
    rule_lines(node["r"], indent + 1, lines)
    return lines


def write_rules(path, model, lines):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("PRE-MARKET PULSE - FACTOR DECISION TREE\n")
        handle.write("Trained : %s\n" % model["trained_at"])
        handle.write("Status  : %s (%d/%d pairs)\n"
                     % (model["status"], model["n_pairs"], model["min_pairs"]))
        handle.write("Target  : next hourly NIFTY % return, quantile bands\n")
        cuts = model["cuts"]
        handle.write("Cuts    : VN<=%.3f  N<=%.3f  NE<=%.3f  P<=%.3f  else VP\n"
                     % (cuts["vn_max"], cuts["n_max"], cuts["ne_max"], cuts["p_max"]))
        handle.write("Features: %d\n\n" % len(model["features"]))
        handle.write("\n".join(lines))
        handle.write("\n")


def write_confusion(path, block):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["actual\\predicted"] + list(tm.CLASSES))
        for name, row in zip(tm.CLASSES, block["confusion"]):
            writer.writerow([name] + row)


def append_history(model):
    try:
        history = []
        if os.path.exists(HISTORY):
            with open(HISTORY, "r", encoding="utf-8") as handle:
                history = json.load(handle)
            if not isinstance(history, list):
                history = []
        history.append({
            "trained_at": model["trained_at"],
            "status": model["status"],
            "n_pairs": model["n_pairs"],
            "cuts": model["cuts"],
            "macro_f1": (model.get("metrics") or {}).get("tree", {}).get("macro_f1"),
            "balanced_accuracy": (model.get("metrics") or {}).get("tree", {})
            .get("balanced_accuracy"),
        })
        with open(HISTORY, "w", encoding="utf-8") as handle:
            json.dump(history[-200:], handle, indent=1)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Train the VP/P/NE/N/VN factor decision tree.")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--rules", default=DEFAULT_RULES)
    parser.add_argument("--confusion", default=DEFAULT_CONFUSION)
    parser.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS,
                        help="sample gate before scores are shown (default 120)")
    parser.add_argument("--min-coverage", type=float, default=0.4,
                        help="drop factor deltas present in fewer than this "
                             "share of pairs (default 0.4)")
    parser.add_argument("--max-gap-hours", type=float,
                        default=tm.PAIR_MAX_GAP_HOURS)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--leaf", type=int, default=15)
    parser.add_argument("--step", type=int, default=1,
                        help="walk-forward stride (default 1)")
    parser.add_argument("--no-ensembles", action="store_true",
                        help="skip RandomForest / HistGradientBoosting benchmarks")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    def say(message):
        if not args.quiet:
            print(message)

    if not os.path.isfile(args.db):
        sys.stderr.write("ERROR: database not found: %s\n" % args.db)
        return 1

    marks = load_marks(args.db)
    pairs = build_pairs(marks, args.max_gap_hours)
    if not pairs:
        sys.stderr.write("ERROR: no valid hourly pairs in %s\n" % args.db)
        return 1

    keys = factor_keys(marks, pairs, args.min_coverage)
    names, X = feature_matrix(marks, pairs, keys)
    targets = np.asarray([target for _index, target in pairs], dtype=float)

    cuts = make_cuts(targets)
    y = label_array(targets, cuts)
    status = "ready" if len(pairs) >= args.min_pairs else "learning"

    # Walk-forward: at most ~60 folds so ensembles stay affordable.
    n = len(targets)
    step = max(args.step, int(math.ceil(max(0, n - args.min_pairs) / 60.0)) or 1)
    validation_cuts = make_cuts(targets[:args.min_pairs]) if n > args.min_pairs else cuts
    metrics = walk_forward(X, targets, validation_cuts, args.min_pairs, step,
                           args) or {}
    if args.no_ensembles:
        for key in ("random_forest", "hist_gradient_boosting"):
            metrics.pop(key, None)

    # Final served model: trained on every pair.
    tree = new_tree(args)
    tree.fit(X, y)
    exported = export_node(tree, names, 0)
    in_sample = score_block(y, tree.predict(X))

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    model = {
        "version": 1,
        "kind": "decision_tree",
        "label": "next_hour_nifty_pct",
        "classes": list(tm.CLASSES),
        "class_names": tm.CLASS_NAMES,
        "cuts": cuts,
        "features": names,
        "tree": exported,
        "status": status,
        "n_pairs": int(len(pairs)),
        "min_pairs": int(args.min_pairs),
        "trained_at": stamp,
        "trained_ts": time.time(),
        "db": os.path.relpath(args.db, ROOT),
        "max_gap_hours": args.max_gap_hours,
        "hyper": {"max_depth": args.depth, "min_samples_leaf": args.leaf,
                  "class_weight": "balanced", "random_state": 42},
        "metrics": metrics,
        "in_sample": dict(in_sample, optimistic=True,
                          note="in-sample; not a performance estimate"),
        "notes": [
            "Labels are quantile bands of the next hourly NIFTY % return.",
            "Features come from the base mark only; no future data.",
            "Scores are expanding-window walk-forward with a 1-mark embargo.",
            "Below min_pairs the tree is still exported but status is learning.",
        ],
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    temp = args.out + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(model, handle, indent=1)
    os.replace(temp, args.out)

    lines = rule_lines(exported)
    write_rules(args.rules, model, lines)
    if metrics.get("tree"):
        write_confusion(args.confusion, metrics["tree"])
    append_history(model)

    say("Database      : %s" % args.db)
    say("Pairs         : %d" % len(pairs))
    say("Features      : %d (%d factor deltas + %d meta)"
        % (len(names), len(keys), len(tm.META_KEYS)))
    say("Cuts (%% return): VN<=%.3f N<=%.3f NE<=%.3f P<=%.3f else VP"
        % (cuts["vn_max"], cuts["n_max"], cuts["ne_max"], cuts["p_max"]))
    say("Class balance : %s"
        % {tm.CLASSES[i]: int((y == i).sum()) for i in range(N_CLASSES)})
    say("Tree          : %d nodes, depth %d" % (node_count(exported),
                                                tree_depth(exported)))
    say("Status        : %s (%d/%d pairs)" % (status, len(pairs), args.min_pairs))
    if metrics.get("tree"):
        say("Walk-forward  : macro F1 %.3f | balanced acc %.3f | %d folds"
            % (metrics["tree"]["macro_f1"], metrics["tree"]["balanced_accuracy"],
               metrics.get("folds") or 0))
        if metrics.get("majority"):
            say("Majority base : macro F1 %.3f | balanced acc %.3f"
                % (metrics["majority"]["macro_f1"],
                   metrics["majority"]["balanced_accuracy"]))
        for name in ("random_forest", "hist_gradient_boosting"):
            if metrics.get(name):
                say("%-14s: macro F1 %.3f | balanced acc %.3f"
                    % (name.replace("_", " ").title(),
                       metrics[name]["macro_f1"],
                       metrics[name]["balanced_accuracy"]))
    else:
        say("Walk-forward  : insufficient data (needs %d pairs)" % args.min_pairs)
    say("Model         : %s" % args.out)
    say("Rules         : %s" % args.rules)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
