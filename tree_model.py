#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared decision-tree inference for Pre-Market Pulse.

This module is deliberately **stdlib only**.  Both dashboards import it:

* ``market_mobile.py``      (V1, port 8080)
* ``v2/market_pulse_v2.py`` (V2, port 8081)

Training happens elsewhere (``v2/train_tree.py``) and writes a plain JSON
model.  Nothing here imports sklearn, numpy or pandas, so serving a
prediction costs a few dictionary lookups.

Model file: ``data/v2/tree_model.json``

Class order is fixed:

    VN  very negative
    N   negative
    NE  neutral
    P   positive
    VP  very positive
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "data", "v2", "tree_model.json")

CLASSES = ("VN", "N", "NE", "P", "VP")
CLASS_NAMES = {
    "VN": "very negative",
    "N": "negative",
    "NE": "neutral",
    "P": "positive",
    "VP": "very positive",
}

# Same pair definition as export_training_csv.py / V2's learner.
PAIR_MAX_GAP_HOURS = 2.5

MOMENTUM_KEYS = ("momentum_1", "momentum_2", "momentum_3", "momentum_day")
META_KEYS = MOMENTUM_KEYS + ("nifty_day_pct", "hour_ist",
                             "factor_count", "breadth")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _safe_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def mark_hour(mark):
    """Return the IST hour-of-day of a mark as a float (14:33 -> 14.55)."""
    text = (mark or {}).get("time") or ""
    parts = text.split()
    if len(parts) < 2:
        return None
    clock = parts[1].split(":")
    try:
        hour = int(clock[0])
        minute = int(clock[1]) if len(clock) > 1 else 0
    except (TypeError, ValueError):
        return None
    return hour + minute / 60.0


def momentum(marks, index):
    """The four momentum inputs used by the V2 neural net.

    Kept byte-for-byte compatible with ``export_training_csv.momentum`` so a
    tree trained on the CSV and a tree trained on the DB see the same inputs.
    """
    current = marks[index]
    out = []
    for lag in (1, 2, 3):
        previous = marks[index - lag] if index - lag >= 0 else None
        if not previous or previous.get("date") != current.get("date"):
            out.append(0.0)
            continue
        na = _safe_float(previous.get("nifty"))
        nb = _safe_float(current.get("nifty"))
        if not na or not nb:
            out.append(0.0)
        else:
            out.append(math.tanh(((nb - na) / na * 100.0) / 0.5))
    day = _safe_float(current.get("nifty_day_pct")) or 0.0
    out.append(math.tanh(day / 1.5))
    return out


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

def factor_delta(factors, key):
    entry = (factors or {}).get(key)
    if not isinstance(entry, dict):
        return None
    return _safe_float(entry.get("delta"))


def feature_row(marks, index, factor_keys):
    """Build one point-in-time feature row from ``marks[index]``.

    Only data known at the base mark is used.  ``marks[index + 1]`` (the
    target) is never touched - see selftest_tree.py.
    """
    base = marks[index]
    factors = base.get("factors") or {}
    row = {}

    for key in factor_keys:
        row["f:" + key] = factor_delta(factors, key)

    mom = momentum(marks, index)
    for name, value in zip(MOMENTUM_KEYS, mom):
        row[name] = value

    row["nifty_day_pct"] = _safe_float(base.get("nifty_day_pct"))
    row["hour_ist"] = mark_hour(base)

    present = 0
    bullish = 0
    bearish = 0
    for key in factor_keys:
        if row.get("f:" + key) is not None:
            present += 1
        direction = ((factors.get(key) or {}).get("direction") or "").lower()
        if direction == "bullish":
            bullish += 1
        elif direction == "bearish":
            bearish += 1
    total = bullish + bearish
    row["factor_count"] = float(present)
    row["breadth"] = ((bullish - bearish) / float(total)) if total else 0.0
    return row


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def label_for(value, cuts):
    """Map a next-hour NIFTY % return to a class index (0..4)."""
    if cuts is None or value is None:
        return None
    if value <= cuts["vn_max"]:
        return 0
    if value <= cuts["n_max"]:
        return 1
    if value <= cuts["ne_max"]:
        return 2
    if value <= cuts["p_max"]:
        return 3
    return 4


# --------------------------------------------------------------------------
# Tree walking
# --------------------------------------------------------------------------

def _walk(node, features):
    """Walk the exported JSON tree.  Returns (probabilities, leaf samples)."""
    while isinstance(node, dict) and "f" in node:
        value = features.get(node["f"])
        if _is_missing(value):
            node = node["l"] if node.get("m") else node["r"]
        else:
            node = node["l"] if float(value) <= float(node["t"]) else node["r"]
    proba = node.get("p") or [0.0] * len(CLASSES)
    return proba, int(node.get("n") or 0)


def predict_proba(model, row):
    if not model or not model.get("tree"):
        return None, 0
    return _walk(model["tree"], row or {})


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load(path=MODEL_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            model = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(model, dict) or not isinstance(model.get("tree"), dict):
        return None
    return model


def model_age_hours(model):
    trained = _safe_float((model or {}).get("trained_ts"))
    if trained is None:
        return None
    return max(0.0, (time.time() - trained) / 3600.0)


def describe(model, marks):
    """Full payload shared by V1's and V2's ``/api/tree`` endpoints."""
    if not model:
        return {
            "status": "missing",
            "classes": list(CLASSES),
            "class_names": CLASS_NAMES,
            "prediction": None,
            "probabilities": None,
            "n_pairs": 0,
            "min_pairs": 0,
            "detail": "No tree model yet - run v2/train_tree.py",
        }
    payload = {
        "status": model.get("status") or "learning",
        "classes": model.get("classes") or list(CLASSES),
        "class_names": model.get("class_names") or CLASS_NAMES,
        "cuts": model.get("cuts"),
        "n_pairs": model.get("n_pairs") or 0,
        "min_pairs": model.get("min_pairs") or 0,
        "trained_at": model.get("trained_at"),
        "age_hours": round(model_age_hours(model) or 0.0, 1),
        "metrics": model.get("metrics") or {},
        "prediction": None,
        "probabilities": None,
        "features_used": 0,
    }
    if not marks:
        payload["detail"] = "No hourly marks stored yet"
        return payload

    index = len(marks) - 1
    base = marks[index]
    factor_keys = [name[2:] for name in (model.get("features") or [])
                   if isinstance(name, str) and name.startswith("f:")]
    row = feature_row(marks, index, factor_keys)
    proba, leaf_n = predict_proba(model, row)

    payload["as_of"] = base.get("time")
    payload["features_used"] = sum(
        1 for name in factor_keys if row.get("f:" + name) is not None)
    payload["leaf_samples"] = leaf_n

    if not proba:
        payload["detail"] = "Model has no tree"
        return payload

    best = max(range(len(proba)), key=lambda i: proba[i])
    payload["probabilities"] = [round(float(p), 4) for p in proba]
    payload["best"] = {
        "class": CLASSES[best],
        "label": CLASS_NAMES.get(CLASSES[best], CLASSES[best]),
        "probability": round(float(proba[best]), 4),
    }
    if payload["status"] == "ready":
        payload["prediction"] = payload["best"]
    else:
        # Below the sample gate: expose it, but clearly as an in-sample preview.
        payload["preview"] = dict(payload["best"], in_sample=True)
        payload["detail"] = ("Collecting data: %d/%d pairs. Tree is trained "
                             "but its score is not shown until the gate."
                             % (payload["n_pairs"], payload["min_pairs"]))
    return payload


class SharedTree:
    """Loads the model once and hot-reloads it when the file changes."""

    def __init__(self, path=MODEL_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.model = None
        self.mtime = None

    def get(self):
        with self.lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                self.model = None
                self.mtime = None
                return None
            if mtime != self.mtime:
                self.model = load(self.path)
                self.mtime = mtime
            return self.model

    def payload(self, marks):
        return describe(self.get(), marks)
