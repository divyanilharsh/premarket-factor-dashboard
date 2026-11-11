#!/usr/bin/env python3
"""Pre-Market Pulse V2 - long-term learning lab.

This is a completely separate dashboard from ``market_mobile.py``.  It never
writes the V1 files.  It reuses the V1 data fetchers/read-only caches, keeps
its own SQLite database in ``data/v2`` and serves its own UI on port 8081.

V2 model
--------
* one full factor + NIFTY snapshot per market hour, stored in SQLite
* at least the latest 2,000 marks are retained (all marks are kept by default)
* power  = lagged correlation between a factor's hourly delta and the NEXT
           hourly NIFTY return, shrunk for sample size
* effect = current signal using the learned long-term correlation sign and a
           robust recent scale; missing/zero deltas are neutral (50)
* NN V2  = small pure-Python MLP trained on point-in-time factor features,
           retrained on every hourly mark, with walk-forward validation
* GUI    = distinct bars for LONG-TERM P/E and NN V2, plus a data-progress bar

Run:
    python v2\\market_pulse_v2.py --port 8081

The V1 dashboard is untouched; this process only reads V1 hourly marks once
to seed a new V2 database (the V1 file is never modified).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import random
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Reuse the proven live-data fetchers from V1.  Importing this module creates
# V1's TRACKER object in memory but never calls its apply/record methods, so no
# V1 state file is written.
try:
    import market_mobile as mm
except Exception as exc:  # pragma: no cover - gives a useful startup message
    sys.stderr.write("Could not import market_mobile.py from %s: %s\n" %
                     (BASE_DIR, exc))
    raise

DATA_DIR = os.path.join(BASE_DIR, "data", "v2")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "market.db")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshot.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
MODEL_FILE = os.path.join(DATA_DIR, "model.json")
OLD_HOURLY_FILE = os.path.join(BASE_DIR, "data", "mobile", "hourly.json")

# Keep V1's FII cache reader intact but send any V1 helper's writes to V2.
mm.FII_HISTORY_FILE = os.path.join(DATA_DIR, "fii_history.json")

TRACK_CATEGORY = "0. LONG-TERM LEARNING"
TARGET_MARKS = 2000
MIN_POWER_SAMPLES = 20
MIN_NN_SAMPLES = 60
MAX_FEATURES = 20
SHRINK_N = 30
PAIR_MAX_GAP_HOURS = 2.5
TARGET_CAP = 4.0
SNAPSHOT_TTL = 300

IST = mm.IST
now_ist = mm.now_ist
stamp = mm.stamp
is_market_open = mm.is_market_open
factor = mm.factor

_PCT_RE = mm._PCT_RE
_NUM_RE = mm._NUM_RE
_DATE_RE = mm._DATE_RE


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return default


def _write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _clamp(value, low, high):
    return max(low, min(high, value))


def _pct(value):
    if value is None:
        return None
    m = _PCT_RE.search(str(value))
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _num(value):
    if value is None:
        return None
    text = str(value)
    if not text or "N/A" in text or _DATE_RE.search(text):
        return None
    text = text.replace("\u20b9", "").replace("$", "")
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _raw_kind(f):
    """Parse a V1 factor into (raw numeric value, kind)."""
    p = _pct((f or {}).get("change"))
    if p is not None:
        return p, "pct"
    n = _num((f or {}).get("value"))
    if n is not None:
        return n, "level"
    return None, None


def _safe_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _median(values):
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _quantile(values, q):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = _clamp(q, 0.0, 1.0) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def _winsorize(xs, ys):
    """Clip extreme x/y values so one bad print cannot own the correlation."""
    if len(xs) < 4:
        return xs, ys
    x_lo, x_hi = _quantile(xs, 0.01), _quantile(xs, 0.99)
    y_lo, y_hi = _quantile(ys, 0.01), _quantile(ys, 0.99)
    if x_lo is None or x_hi is None or y_lo is None or y_hi is None:
        return xs, ys
    if x_hi <= x_lo:
        x_lo, x_hi = -abs(x_lo or 1.0), abs(x_hi or 1.0)
    if y_hi <= y_lo:
        y_lo, y_hi = -abs(y_lo or 1.0), abs(y_hi or 1.0)
    return ([_clamp(x, x_lo, x_hi) for x in xs],
            [_clamp(y, y_lo, y_hi) for y in ys])


def _pearson(xs, ys):
    n = len(xs)
    if n < 4 or n != len(ys):
        return None, None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None, None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return _clamp(r, -0.999999, 0.999999), mx, my


def _shrink_corr(r, n):
    """Fisher-z shrink small samples toward zero; return (r_eff, confidence)."""
    if r is None or n < MIN_POWER_SAMPLES:
        return 0.0, 0.0
    confidence = n / float(n + SHRINK_N)
    r_eff = math.tanh(math.atanh(_clamp(r, -0.999999, 0.999999)) *
                      (n / float(n + SHRINK_N)))
    noise = 1.5 / math.sqrt(max(1, n))
    if abs(r_eff) < noise:
        return 0.0, confidence
    return r_eff, confidence


# --------------------------------------------------------------------------
# SQLite mark store
# --------------------------------------------------------------------------

class MarkStore:
    """Append-only SQLite store for hourly marks and prediction scores."""

    def __init__(self, path=DB_FILE, seed=True):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()
        self.imported = self.seed_from_v1() if seed else 0

    def _init_schema(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    time TEXT NOT NULL,
                    date TEXT NOT NULL,
                    bucket TEXT NOT NULL UNIQUE,
                    nifty REAL,
                    nifty_day_pct REAL,
                    nifty_source TEXT,
                    gap_hours REAL,
                    factors_json TEXT NOT NULL,
                    imported INTEGER NOT NULL DEFAULT 0,
                    created_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_marks_ts ON marks(ts);
                CREATE INDEX IF NOT EXISTS idx_marks_date ON marks(date);
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    base_ts REAL NOT NULL,
                    target_ts REAL,
                    predicted_pct REAL NOT NULL,
                    predicted_price REAL,
                    actual_pct REAL,
                    err REAL,
                    hit INTEGER,
                    samples INTEGER,
                    model TEXT,
                    created_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pred_base ON predictions(base_ts);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            self.conn.commit()

    # ---- read -----------------------------------------------------------

    def count(self):
        with self.lock:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM marks").fetchone()[0])

    def last(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM marks ORDER BY ts DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._row_to_mark(row) if row else None

    def first(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM marks ORDER BY ts ASC, id ASC LIMIT 1"
            ).fetchone()
        return self._row_to_mark(row) if row else None

    def load_marks(self, limit=None):
        with self.lock:
            if limit:
                rows = self.conn.execute(
                    "SELECT * FROM (SELECT * FROM marks ORDER BY ts DESC, id DESC LIMIT ?) "
                    "ORDER BY ts ASC, id ASC", (int(limit),)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM marks ORDER BY ts ASC, id ASC").fetchall()
        return [self._row_to_mark(r) for r in rows]

    @staticmethod
    def _row_to_mark(row):
        d = dict(row)
        try:
            factors = json.loads(d.pop("factors_json") or "{}")
        except Exception:
            factors = {}
        d["factors"] = factors if isinstance(factors, dict) else {}
        return d

    def dump_marks(self, limit=None):
        marks = self.load_marks(limit)
        return {"count": self.count(), "records": marks}

    # ---- write ----------------------------------------------------------

    def insert_mark(self, rec):
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO marks "
                "(ts,time,date,bucket,nifty,nifty_day_pct,nifty_source,gap_hours,"
                " factors_json,imported,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (float(rec["ts"]), rec["time"], rec["date"], rec["bucket"],
                 rec.get("nifty"), rec.get("nifty_day_pct"),
                 rec.get("nifty_source"), rec.get("gap_hours"),
                 json.dumps(rec.get("factors") or {}, ensure_ascii=False,
                            separators=(",", ":")),
                 int(rec.get("imported") or 0), time.time()))
            self.conn.commit()
            return cur.lastrowid

    def latest_unscored_prediction(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM predictions WHERE actual_pct IS NULL "
                "ORDER BY base_ts DESC, id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def insert_prediction(self, base_ts, predicted_pct, predicted_price,
                          samples, model="nnv2"):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO predictions "
                "(base_ts,target_ts,predicted_pct,predicted_price,samples,model,created_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (float(base_ts), None, float(predicted_pct),
                 predicted_price, int(samples), model, time.time()))
            self.conn.commit()
            return cur.lastrowid

    def score_prediction(self, pred_id, target_ts, actual_pct):
        hit = None
        if abs(actual_pct) >= 0.03:
            # The prediction sign is stored in predicted_pct.
            with self.lock:
                row = self.conn.execute(
                    "SELECT predicted_pct FROM predictions WHERE id=?",
                    (int(pred_id),)).fetchone()
            if row:
                hit = 1 if (float(row[0]) >= 0) == (actual_pct >= 0) else 0
        err = None
        if pred_id:
            with self.lock:
                row = self.conn.execute(
                    "SELECT predicted_pct FROM predictions WHERE id=?",
                    (int(pred_id),)).fetchone()
            if row:
                err = abs(float(row[0]) - actual_pct)
        with self.lock:
            self.conn.execute(
                "UPDATE predictions SET target_ts=?, actual_pct=?, err=?, hit=? "
                "WHERE id=?", (float(target_ts), round(actual_pct, 4),
                               round(err, 4) if err is not None else None,
                               hit, int(pred_id)))
            self.conn.commit()

    def prediction_stats(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*), AVG(err), "
                "SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM predictions WHERE actual_pct IS NOT NULL").fetchone()
        n = int(row[0] or 0)
        return {
            "n": n,
            "mae": round(float(row[1]), 3) if row[1] is not None else None,
            "hits": int(row[2] or 0),
            "scored": int(row[3] or 0),
            "hit_rate": (round(100.0 * float(row[2]) / int(row[3]), 1)
                         if row[3] else None),
        }

    def seed_from_v1(self):
        """Read V1 hourly marks once, only when this database is empty.

        This is read-only with respect to the V1 files: it gives V2 a small
        head start without touching the running V1 dashboard.
        """
        if self.count():
            return 0
        data = _read_json(OLD_HOURLY_FILE, [])
        if not isinstance(data, list) or not data:
            return 0
        imported = 0
        for old in data[-TARGET_MARKS:]:
            try:
                factors = {}
                for key, f in (old.get("factors") or {}).items():
                    raw, kind = _raw_kind(f)
                    delta = f.get("delta")
                    try:
                        delta = float(delta) if delta is not None else None
                    except (TypeError, ValueError):
                        delta = None
                    factors[key] = {
                        "label": f.get("label"),
                        "value": f.get("value"),
                        "change": f.get("change"),
                        "direction": f.get("direction"),
                        "status": f.get("status"),
                        "source": f.get("source"),
                        "details": (f.get("details") or "")[:240],
                        "raw": raw,
                        "kind": kind,
                        "delta": delta,
                    }
                rec = {
                    "ts": float(old.get("ts") or 0),
                    "time": old.get("time") or "",
                    "date": old.get("date") or "",
                    "bucket": old.get("bucket") or "",
                    "nifty": old.get("nifty"),
                    "nifty_day_pct": old.get("nifty_day_pct"),
                    "nifty_source": old.get("nifty_source") or "v1-import",
                    "gap_hours": old.get("gap_hours"),
                    "factors": factors,
                    "imported": 1,
                }
                if rec["ts"] and rec["bucket"]:
                    self.insert_mark(rec)
                    imported += 1
            except Exception:
                continue
        return imported


# --------------------------------------------------------------------------
# Long-term learner
# --------------------------------------------------------------------------

class LongTermLearner:
    """Builds long-term power/effect nodes and point-in-time NN features."""

    def __init__(self):
        self.last_pairs = 0

    @staticmethod
    def _valid_pairs(marks):
        """Pairs (base index -> next mark) usable for next-hour learning."""
        out = []
        for i in range(len(marks) - 1):
            a, b = marks[i], marks[i + 1]
            na, nb = _safe_float(a.get("nifty")), _safe_float(b.get("nifty"))
            if not na or not nb or a.get("date") != b.get("date"):
                continue
            gap = (float(b.get("ts") or 0) - float(a.get("ts") or 0)) / 3600.0
            if gap <= 0 or gap > PAIR_MAX_GAP_HOURS:
                continue
            # A base mark whose own factor delta spans a long gap (for
            # example after the PC slept) is not an hourly observation.
            base_gap = _safe_float(a.get("gap_hours"))
            if base_gap is not None and base_gap > PAIR_MAX_GAP_HOURS:
                continue
            target = (nb - na) / na * 100.0
            if abs(target) > TARGET_CAP:
                continue
            out.append({"base_i": i, "target": target,
                        "target_ts": float(b.get("ts") or 0)})
        return out

    @staticmethod
    def _recent_deltas(marks, key, limit=120):
        vals = []
        for m in marks[-limit:]:
            f = (m.get("factors") or {}).get(key)
            if not f:
                continue
            d = f.get("delta")
            try:
                d = float(d) if d is not None else None
            except (TypeError, ValueError):
                d = None
            vals.append(d)
        return vals

    @staticmethod
    def _effect(latest_delta, recent, sign, confidence, r_eff=1.0):
        if latest_delta is None:
            return 50.0, 0, None
        try:
            latest_delta = float(latest_delta)
        except (TypeError, ValueError):
            return 50.0, 0, None
        if not math.isfinite(latest_delta) or abs(latest_delta) < 1e-12 or not sign:
            return 50.0, 0, None
        usable = []
        for v in recent:
            try:
                fv = float(v) if v is not None else None
            except (TypeError, ValueError):
                fv = None
            if fv is not None and math.isfinite(fv) and abs(fv) > 1e-12:
                usable.append(abs(fv))
        n = len(usable)
        if n < 3:
            return 50.0, n, None
        med = _median(usable)
        mad = _median([abs(v - med) for v in usable])
        scale = 1.4826 * (mad or 0.0)
        if scale <= 1e-12:
            scale = sum(usable) / n
        if scale <= 1e-12:
            return 50.0, n, None
        z = _clamp(latest_delta / scale, -5.0, 5.0)
        raw = 50.0 + 50.0 * math.tanh(0.55 * sign * z)
        # Be honest early: effect is muted until there is both a long-term
        # direction, a meaningful signal, and enough recent observations.
        signal = min(1.0, max(0.0, abs(float(r_eff or 0.0))))
        effect_conf = (min(1.0, n / 20.0) *
                       (0.35 + 0.65 * confidence) *
                       (0.5 + 0.5 * signal))
        return _clamp(50.0 + (raw - 50.0) * effect_conf, 0.0, 100.0), n, scale

    def analyze(self, marks):
        """Return {nodes, pairs, long_pe} from the full stored history."""
        self.last_pairs = 0
        pairs = self._valid_pairs(marks)
        self.last_pairs = len(pairs)
        keys = set()
        for m in marks:
            keys.update((m.get("factors") or {}).keys())

        nodes = {}
        for key in keys:
            xs, ys = [], []
            for p in pairs:
                f = (marks[p["base_i"]].get("factors") or {}).get(key)
                if not f:
                    continue
                d = f.get("delta")
                try:
                    d = float(d) if d is not None else None
                except (TypeError, ValueError):
                    d = None
                if d is None or not math.isfinite(d):
                    continue
                xs.append(d)
                ys.append(p["target"])
            if len(xs) >= 4:
                xs_w, ys_w = _winsorize(xs, ys)
                r, mx, my = _pearson(xs_w, ys_w)
            else:
                r, mx, my = None, None, None
            r_eff, confidence = _shrink_corr(r, len(xs))
            power = round(100.0 * abs(r_eff), 1) if r_eff else 0.0
            sign = 1 if r_eff > 0 else -1 if r_eff < 0 else 0
            latest = marks[-1] if marks else {}
            lf = (latest.get("factors") or {}).get(key) or {}
            try:
                latest_delta = (float(lf.get("delta"))
                                if lf.get("delta") is not None else None)
            except (TypeError, ValueError):
                latest_delta = None
            recent = self._recent_deltas(marks, key)
            effect, effect_n, scale = self._effect(
                latest_delta, recent, sign, confidence, abs(r_eff))
            nodes[key] = {
                "power": round(power, 1),
                "effect": round(effect, 1),
                "corr": round(r_eff, 3) if r_eff else None,
                "raw_corr": round(r, 3) if r is not None else None,
                "sign": sign,
                "confidence": round(confidence, 3),
                "samples": len(xs),
                "effect_samples": effect_n,
                "delta": latest_delta,
                "scale": round(scale, 5) if scale is not None else None,
                "label": lf.get("label") or key.split("::")[-1],
            }
        return {"nodes": nodes, "pairs": len(pairs),
                "long_pe": self.long_pe(nodes)}

    @staticmethod
    def long_pe(nodes, keys=None):
        score = 0.0
        weight = 0.0
        for key, node in (nodes or {}).items():
            if keys is not None and key not in keys:
                continue
            p = _safe_float(node.get("power")) or 0.0
            c = _safe_float(node.get("confidence")) or 0.0
            w = (p / 100.0) * c
            if w <= 0:
                continue
            e = _safe_float(node.get("effect"))
            if e is None:
                e = 50.0
            score += e * w
            weight += w
        bull = score / weight if weight else 50.0
        return {"bull": round(bull, 1), "bear": round(100.0 - bull, 1),
                "weight": round(weight, 3)}

    def top_features(self, nodes, limit=MAX_FEATURES):
        ranked = sorted(
            (nodes or {}).items(),
            key=lambda kv: ((_safe_float(kv[1].get("power")) or 0.0) *
                            (_safe_float(kv[1].get("confidence")) or 0.0)),
            reverse=True)
        out = []
        for key, node in ranked:
            if (_safe_float(node.get("power")) or 0.0) <= 0:
                continue
            out.append({
                "key": key,
                "label": node.get("label") or key.split("::")[-1],
                "power": node.get("power"),
                "corr": node.get("corr"),
                "confidence": node.get("confidence"),
                "samples": node.get("samples"),
                "sign": node.get("sign"),
            })
            if len(out) >= limit:
                break
        return out

    def build_features(self, marks, nodes, top_keys):
        """Point-in-time features for every mark.

        Pair i-1 -> i is added before the feature for mark i, so a feature
        used to predict i -> i+1 never contains the i -> i+1 target.
        """
        pairs = self._valid_pairs(marks)
        by_base = {p["base_i"]: p for p in pairs}
        keys = list(top_keys)
        stats = {k: [0, 0.0, 0.0, 0.0, 0.0, 0.0] for k in keys}
        # [n, sum_x, sum_y, sum_xx, sum_yy, sum_xy]
        X = []
        Y = []
        valid_pair = []
        for i, mark in enumerate(marks):
            p = by_base.get(i - 1)
            if p:
                base = marks[i - 1]
                for key in keys:
                    f = (base.get("factors") or {}).get(key)
                    if not f or f.get("delta") is None:
                        continue
                    try:
                        x = float(f.get("delta"))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(x):
                        continue
                    y = float(p["target"])
                    st = stats[key]
                    st[0] += 1
                    st[1] += x
                    st[2] += y
                    st[3] += x * x
                    st[4] += y * y
                    st[5] += x * y
            row = []
            for key in keys:
                st = stats[key]
                n = st[0]
                f = (mark.get("factors") or {}).get(key)
                d = None if not f else f.get("delta")
                try:
                    d = float(d) if d is not None else None
                except (TypeError, ValueError):
                    d = None
                feat = 0.0
                if (d is not None and math.isfinite(d) and
                        abs(d) > 1e-12 and n >= 4):
                    mx = st[1] / n
                    my = st[2] / n
                    varx = st[3] / n - mx * mx
                    vary = st[4] / n - my * my
                    if varx > 1e-12 and vary > 1e-12:
                        cov = st[5] / n - mx * my
                        r = _clamp(cov / math.sqrt(varx * vary),
                                   -0.999999, 0.999999)
                        r_eff, confidence = _shrink_corr(r, n)
                        sign = 1 if r_eff > 0 else -1 if r_eff < 0 else 0
                        if sign:
                            z = _clamp((d - mx) / math.sqrt(varx), -5.0, 5.0)
                            feat = (sign * math.tanh(0.55 * z) *
                                    abs(r_eff) * confidence)
                row.append(feat)
            row.extend(self._momentum(marks, i))
            X.append(row)
            if i in by_base:
                Y.append(by_base[i]["target"])
                valid_pair.append(True)
            else:
                Y.append(None)
                valid_pair.append(False)
        return X, Y, valid_pair

    @staticmethod
    def _momentum(marks, i):
        rec = marks[i]
        out = []
        for lag in (1, 2, 3):
            a = marks[i - lag] if i - lag >= 0 else None
            b = rec
            if not a or a.get("date") != b.get("date"):
                out.append(0.0)
                continue
            na, nb = _safe_float(a.get("nifty")), _safe_float(b.get("nifty"))
            if not na or not nb:
                out.append(0.0)
            else:
                out.append(math.tanh(((nb - na) / na * 100.0) / 0.5))
        day = _safe_float(rec.get("nifty_day_pct")) or 0.0
        out.append(math.tanh(day / 1.5))
        return out


# --------------------------------------------------------------------------
# Tiny neural net (V2, pure Python)
# --------------------------------------------------------------------------

class TinyNetV2:
    def __init__(self, n_in, n_hidden=5, seed=20261104):
        self.n_in = max(1, int(n_in))
        self.n_hidden = max(2, int(n_hidden))
        rnd = random.Random(seed)
        scale = 1.0 / math.sqrt(self.n_in)
        self.w1 = [[rnd.uniform(-scale, scale) for _ in range(self.n_in)]
                   for _ in range(self.n_hidden)]
        self.b1 = [0.0] * self.n_hidden
        self.w2 = [rnd.uniform(-scale, scale) for _ in range(self.n_hidden)]
        self.b2 = 0.0

    def forward(self, x):
        hidden = []
        for row, bias in zip(self.w1, self.b1):
            s = bias
            for w, xi in zip(row, x):
                s += w * xi
            hidden.append(math.tanh(s))
        y = self.b2 + sum(w * h for w, h in zip(self.w2, hidden))
        return y, hidden

    def predict(self, x):
        return self.forward(x)[0]

    def fit(self, xs, ys, epochs=45, lr=0.025, l2=0.003, deadline=None):
        n = len(xs)
        if not n:
            return self
        n_in, nh = self.n_in, self.n_hidden
        for _ in range(epochs):
            gw1 = [[0.0] * n_in for _ in range(nh)]
            gb1 = [0.0] * nh
            gw2 = [0.0] * nh
            gb2 = 0.0
            for x, y in zip(xs, ys):
                pred, hidden = self.forward(x)
                dy = 2.0 * (pred - y) / n
                for j in range(nh):
                    h = hidden[j]
                    gw2[j] += dy * h
                    dh = dy * self.w2[j] * (1.0 - h * h)
                    gb1[j] += dh
                    row = gw1[j]
                    for k in range(n_in):
                        row[k] += dh * x[k]
                gb2 += dy
            for j in range(nh):
                row = self.w1[j]
                for k in range(n_in):
                    row[k] -= lr * (gw1[j][k] + l2 * row[k])
                self.b1[j] -= lr * gb1[j]
                self.w2[j] -= lr * (gw2[j] + l2 * self.w2[j])
            self.b2 -= lr * gb2
            if deadline and time.time() > deadline:
                break
        return self

    def to_dict(self):
        return {"n_in": self.n_in, "n_hidden": self.n_hidden,
                "w1": self.w1, "b1": self.b1,
                "w2": self.w2, "b2": self.b2}


def _metrics(net, xs, ys):
    if not xs:
        return None, None
    errors, hits = [], []
    for x, y in zip(xs, ys):
        pred = net.predict(x)
        errors.append(abs(pred - y))
        if abs(y) >= 0.03:
            hits.append(1 if (pred >= 0) == (y >= 0) else 0)
    mae = round(sum(errors) / len(errors), 3) if errors else None
    hit = round(100.0 * sum(hits) / len(hits), 1) if hits else None
    return mae, hit


# --------------------------------------------------------------------------
# Tracker: record, learn, and attach V2 nodes
# --------------------------------------------------------------------------

class V2Tracker:
    def __init__(self, store, record_override=None):
        self.store = store
        self.record_override = record_override
        self.lock = threading.RLock()
        self.nodes = {}
        self.pairs = 0
        self.nn = {}
        self.long_pe = {"bull": 50.0, "bear": 50.0, "weight": 0.0}
        self.last_bucket = (store.last() or {}).get("bucket")
        self._recompute(train=True)

    # ---- public ---------------------------------------------------------

    def apply(self, categories, nifty, day_pct=None):
        with self.lock:
            now = now_ist()
            open_now = (self.record_override if self.record_override is not None
                        else is_market_open())
            bucket = now.strftime("%Y-%m-%d %H")
            price = (nifty or {}).get("price")
            if open_now and price and bucket != self.last_bucket:
                self._record(categories, nifty, now, bucket)
                self.last_bucket = bucket

            live_keys = self._attach(categories)
            self.long_pe = LongTermLearner.long_pe(self.nodes, live_keys)
            latest = self.store.last()
            return self._track(categories, latest, nifty, now, open_now,
                               day_pct, live_keys)

    def recent(self, limit=24):
        try:
            limit = max(1, min(int(limit), 2000))
        except (TypeError, ValueError):
            limit = 24
        return self.store.dump_marks(limit)

    def dump(self):
        return self.store.dump_marks()

    # ---- recording ------------------------------------------------------

    def _record(self, categories, nifty, now, bucket):
        prev = self.store.last()
        price = float(nifty["price"])
        gap = 0.0
        if prev and prev.get("ts"):
            gap = max(0.0, (now.timestamp() - float(prev["ts"])) / 3600.0)
        factors = {}
        for cat in categories:
            if cat.get("name") == TRACK_CATEGORY:
                continue
            for f in cat.get("factors") or []:
                key = "%s::%s" % (cat.get("name"), f.get("key"))
                raw, kind = _raw_kind(f)
                delta = None
                if prev and raw is not None:
                    pf = (prev.get("factors") or {}).get(key)
                    if pf and pf.get("kind") == kind and pf.get("raw") is not None:
                        try:
                            delta = raw - float(pf["raw"])
                        except (TypeError, ValueError):
                            delta = None
                factors[key] = {
                    "label": f.get("label"),
                    "value": f.get("value"),
                    "change": f.get("change"),
                    "direction": f.get("direction"),
                    "status": f.get("status"),
                    "source": f.get("source"),
                    "details": (f.get("details") or "")[:240],
                    "raw": raw,
                    "kind": kind,
                    "delta": round(delta, 6) if delta is not None else None,
                }
        rec = {
            "ts": now.timestamp(),
            "time": now.strftime("%Y-%m-%d %H:%M IST"),
            "date": now.strftime("%Y-%m-%d"),
            "bucket": bucket,
            "nifty": round(price, 2),
            "nifty_day_pct": (round(float(nifty.get("day_pct")), 3)
                              if nifty.get("day_pct") is not None else None),
            "nifty_source": nifty.get("source"),
            "gap_hours": round(gap, 2),
            "factors": factors,
            "imported": 0,
        }
        self.store.insert_mark(rec)
        self._score_previous(prev, rec)
        self._recompute(train=True)
        pred = self.nn.get("prediction")
        if pred and pred.get("pct") is not None:
            pred_id = self.store.insert_prediction(
                rec["ts"], pred["pct"], pred.get("price"),
                int(pred.get("samples") or 0), "nnv2")
            self.nn["prediction_id"] = pred_id

    def _score_previous(self, prev, rec):
        if not prev:
            return
        pred = self.store.latest_unscored_prediction()
        if not pred:
            return
        try:
            gap = (float(rec["ts"]) - float(pred["base_ts"])) / 3600.0
        except (TypeError, ValueError):
            return
        if gap <= 0 or gap > PAIR_MAX_GAP_HOURS:
            return
        na, nb = _safe_float(prev.get("nifty")), _safe_float(rec.get("nifty"))
        if not na or not nb:
            return
        actual = (nb - na) / na * 100.0
        self.store.score_prediction(int(pred["id"]), rec["ts"], actual)

    def _recompute(self, train=True):
        marks = self.store.load_marks()
        if not marks:
            self.nodes = {}
            self.pairs = 0
            self.nn = {}
            self._save_state()
            return
        learner = LongTermLearner()
        result = learner.analyze(marks)
        self.nodes = result["nodes"]
        self.pairs = result["pairs"]
        if train:
            self.nn = self._train_nn(marks, self.nodes, learner)
        self._save_state()

    def _train_nn(self, marks, nodes, learner):
        top = learner.top_features(nodes)
        keys = [t["key"] for t in top]
        base = {
            "version": 2,
            "top_features": top,
            "feature_keys": keys,
            "min_samples": MIN_NN_SAMPLES,
            "prediction": None,
            "samples": 0,
            "features": len(keys) + 4,
            "fit_mae": None,
            "fit_hit": None,
            "val_mae": None,
            "val_hit": None,
            "val_samples": 0,
            "weights": {},
            "trained_ts": time.time(),
        }
        if not keys:
            base["status"] = "no_powered_factors"
            return base
        X, Y, valid = learner.build_features(marks, nodes, keys)
        xs = [X[i] for i, ok in enumerate(valid) if ok and Y[i] is not None]
        ys = [float(Y[i]) for i, ok in enumerate(valid) if ok and Y[i] is not None]
        base["samples"] = len(xs)
        if len(xs) < MIN_NN_SAMPLES:
            base["status"] = "warming"
            base["need"] = max(0, MIN_NN_SAMPLES - len(xs))
            return base
        # Walk-forward validation on the last 20% of time-ordered pairs.
        val_n = max(1, int(len(xs) * 0.2)) if len(xs) >= 80 else 0
        if val_n:
            tr_x, tr_y = xs[:-val_n], ys[:-val_n]
            va_x, va_y = xs[-val_n:], ys[-val_n:]
            if len(tr_x) >= MIN_NN_SAMPLES:
                val_net = TinyNetV2(len(tr_x[0]))
                val_net.fit(tr_x, tr_y, epochs=35, lr=0.025, l2=0.003,
                            deadline=time.time() + 1.6)
                base["val_mae"], base["val_hit"] = _metrics(val_net, va_x, va_y)
                base["val_samples"] = len(va_x)
        net = TinyNetV2(len(xs[0]))
        net.fit(xs, ys, epochs=55, lr=0.025, l2=0.003,
                deadline=time.time() + 2.2)
        base["weights"] = net.to_dict()
        base["fit_mae"], base["fit_hit"] = _metrics(net, xs, ys)
        latest_x = X[-1] if X else None
        pred = None
        if latest_x and len(latest_x) == net.n_in:
            pct = _clamp(net.predict(latest_x), -2.0, 2.0)
            last = marks[-1]
            price = _safe_float(last.get("nifty"))
            pred = {
                "pct": round(pct, 3),
                "price": round(price * (1.0 + pct / 100.0), 2) if price else None,
                "as_of": last.get("time"),
                "samples": len(xs),
                "fit_mae": base["fit_mae"],
                "fit_hit": base["fit_hit"],
                "val_mae": base["val_mae"],
                "val_hit": base["val_hit"],
                "kind": "nnv2",
            }
        base["prediction"] = pred
        base["status"] = "ready" if pred else "warming"
        return base

    def _save_state(self):
        _write_json(NODES_FILE, self.nodes)
        _write_json(MODEL_FILE, self.nn)

    # ---- display --------------------------------------------------------

    @staticmethod
    def _default_node():
        return {"power": 0.0, "effect": 50.0, "corr": None, "sign": 0,
                "confidence": 0.0, "samples": 0, "effect_samples": 0,
                "delta": None, "scale": None}

    def _attach(self, categories):
        live_keys = set()
        for cat in categories:
            if cat.get("name") == TRACK_CATEGORY:
                continue
            for f in cat.get("factors") or []:
                key = "%s::%s" % (cat.get("name"), f.get("key"))
                live_keys.add(key)
                node = self.nodes.get(key) or self._default_node()
                p = round(float(node.get("power") or 0.0), 1)
                e = round(float(node.get("effect") if node.get("effect") is not None else 50.0), 1)
                c = round(float(node.get("confidence") or 0.0), 3)
                f["power"] = p
                f["effect"] = e
                f["power_long"] = p
                f["effect_long"] = e
                f["confidence"] = c
                f["power_corr"] = node.get("corr")
                f["power_samples"] = node.get("samples")
                f["node_samples"] = node.get("samples")
                f["effect_samples"] = node.get("effect_samples")
                f["node_delta"] = node.get("delta")
                f["learn_sign"] = node.get("sign")
                f["node_scale"] = node.get("scale")
        return live_keys

    def _track(self, categories, latest, nifty, now, open_now, day_pct,
               live_keys):
        n_marks = self.store.count()
        first = self.store.first()
        recent = self.store.load_marks(limit=2)
        prev = recent[0] if len(recent) >= 2 else None
        price = (nifty or {}).get("price")
        if price is None and latest:
            price = latest.get("nifty")
        day = day_pct
        if day is None and nifty:
            day = nifty.get("day_pct")
        if day is None and latest:
            day = latest.get("nifty_day_pct")
        hour_pct = None
        if latest and prev and latest.get("nifty") and prev.get("nifty"):
            gap = (float(latest.get("ts") or 0) -
                   float(prev.get("ts") or 0)) / 3600.0
            if 0 < gap <= 4.0:
                hour_pct = ((float(latest["nifty"]) - float(prev["nifty"])) /
                            float(prev["nifty"]) * 100.0)
        mark_pct = hour_pct
        if hour_pct is None:
            hour_pct = day
        hour_dir = ("bullish" if hour_pct is not None and hour_pct > 0.02
                    else "bearish" if hour_pct is not None and hour_pct < -0.02
                    else "neutral")
        pred = self.nn.get("prediction")
        samples = int(self.nn.get("samples") or 0)
        live = self.store.prediction_stats()
        pe = LongTermLearner.long_pe(self.nodes, live_keys)
        pe_bull, pe_bear = round(pe["bull"]), round(pe["bear"])

        rows = {}
        nifty_value = "{:,.2f}".format(float(price)) if price else "N/A"
        if hour_pct is None:
            n_change = ""
        else:
            suffix = " / mark" if mark_pct is not None else \
                (" day" if day is not None else "")
            n_change = "%+.2f%%%s" % (hour_pct, suffix)
        span = ""
        if first and latest:
            span = "%s -> %s" % (first.get("time"), latest.get("time"))
        rows["nifty_hourly"] = factor(
            label="NIFTY 50 - hourly mark",
            value=nifty_value,
            change=n_change,
            direction=hour_dir,
            status="ok" if price else "warning",
            details=("Day change %s | Last mark: %s | %d marks stored. "
                     "Each mark is stored in SQLite; all history is kept." %
                     (("%+.2f%%" % float(day)) if day is not None else "N/A",
                      (latest or {}).get("time") or "not yet", n_marks)),
            source=((nifty or {}).get("source") or "NSE") + " hourly mark")
        rows["long_pe"] = factor(
            label="Long-term P/E bias",
            value="BULL %d / BEAR %d" % (pe_bull, pe_bear),
            change="power x confidence weighted",
            direction=("bullish" if pe_bull > 52 else
                       "bearish" if pe_bull < 48 else "neutral"),
            status="ok",
            details=("Ticked factors only. Weight = power x confidence; "
                     "missing/zero delta counts as neutral 50. "
                     "This is the long-term view, not the old V1 P/E bar."),
            source="V2 learner")
        if pred:
            pred_pct = float(pred.get("pct") or 0)
            pred_dir = ("bullish" if pred_pct > 0.02 else
                        "bearish" if pred_pct < -0.02 else "neutral")
            rows["nn_prediction"] = factor(
                label="NN V2 - next-hour NIFTY",
                value="%s (%+.2f%%)" % (
                    "{:,.2f}".format(pred.get("price") or 0), pred_pct),
                change="next hourly mark",
                direction=pred_dir,
                status="ok",
                details=("New V2 net: %d inputs, 5 hidden units, trained on %d "
                         "pairs. Walk-forward MAE %s%% / hit %s%%. "
                         "In-sample MAE %s%%." %
                         (int(self.nn.get("features") or 0), samples,
                          ("%.2f" % pred["val_mae"])
                          if pred.get("val_mae") is not None else "n/a",
                          ("%.0f" % pred["val_hit"])
                          if pred.get("val_hit") is not None else "n/a",
                          ("%.2f" % pred["fit_mae"])
                          if pred.get("fit_mae") is not None else "n/a")),
                source="V2 neural net")
        else:
            need = int(self.nn.get("need") or max(0, MIN_NN_SAMPLES - samples))
            if not self.nn.get("feature_keys"):
                value = "waiting for powered factors"
                change = "needs a significant factor"
                details = ("No factor has a significant lagged signal yet "
                           "(20+ pairs and above the noise floor). V2 "
                           "refuses to invent weights; it waits for real "
                           "evidence.")
            else:
                value = "collecting data (%d/%d)" % (samples, MIN_NN_SAMPLES)
                change = "needs %d more pairs" % max(0, need)
                details = ("The V2 net starts predicting after %d pairs. "
                           "It is retrained from scratch on every mark." %
                           MIN_NN_SAMPLES)
            rows["nn_prediction"] = factor(
                label="NN V2 - next-hour NIFTY",
                value=value,
                change=change,
                direction="neutral", status="ok",
                details=details, source="V2 neural net")
        target = max(TARGET_MARKS, n_marks)
        coverage = round(100.0 * n_marks / target, 1) if target else 0.0
        rows["data_status"] = factor(
            label="Long-term data store",
            value="%d marks" % n_marks,
            change="%d / %d target" % (n_marks, TARGET_MARKS),
            direction="neutral", status="ok",
            details=("SQLite: data/v2/market.db | %d learning pairs | "
                     "coverage %.1f%% | %s" %
                     (self.pairs, coverage,
                      ("span " + span) if span else "waiting for first mark")),
            source="V2 SQLite")
        model_value = ("ready" if pred else "learning")
        model_details = ("%d selected factor inputs + 4 momentum inputs. "
                         "%d hidden units. %d stored marks, %d pairs." %
                         (len(self.nn.get("feature_keys") or []),
                          int(self.nn.get("weights", {}).get("n_hidden") or 5),
                          n_marks, self.pairs))
        if live.get("mae") is not None:
            model_details += " Live MAE %.2f%%" % live["mae"]
        if live.get("hit_rate") is not None:
            model_details += " | live direction %.0f%% (%d scored)" % (
                live["hit_rate"], live.get("scored") or 0)
        rows["model_status"] = factor(
            label="V2 model status",
            value=model_value,
            change="%d pairs" % samples,
            direction="neutral", status="ok",
            details=model_details, source="V2 learner")
        for row in rows.values():
            row["power"] = 0.0
            row["effect"] = 50.0
            row["confidence"] = 0.0
            row["power_long"] = 0.0
            row["effect_long"] = 50.0
        rows["long_pe"]["effect"] = pe["bull"]
        rows["long_pe"]["effect_long"] = pe["bull"]
        rows["long_pe"]["power"] = 0.0
        rows["long_pe"]["power_long"] = 0.0
        for key, row in rows.items():
            row["key"] = key

        category = {
            "name": TRACK_CATEGORY,
            "short": "Long-Term Lab",
            "num": "0",
            "factors": [rows[k] for k in
                        ("nifty_hourly", "long_pe", "nn_prediction",
                         "data_status", "model_status")],
        }

        chips = []
        if price:
            day_change = ("%+.2f%% day" % float(day)) if day is not None else ""
            if day is None:
                day_dir = hour_dir
            elif day > 0.02:
                day_dir = "bullish"
            elif day < -0.02:
                day_dir = "bearish"
            else:
                day_dir = "neutral"
            chips.append({"label": "NIFTY 50", "value": nifty_value,
                          "change": day_change or n_change,
                          "direction": day_dir, "status": "ok"})
        chips.append({
            "label": "NN V2",
            "value": ("%+.2f%%" % float(pred["pct"])) if pred else "learning",
            "change": (("%s" % "{:,.0f}".format(pred.get("price") or 0))
                       if pred else
                       ("%d/%d pairs" % (samples, MIN_NN_SAMPLES)
                        if self.nn.get("feature_keys") else
                        "waiting for power")),
            "direction": (("bullish" if float(pred["pct"]) > 0.02 else
                           "bearish" if float(pred["pct"]) < -0.02 else
                           "neutral") if pred else "neutral"),
            "status": "ok" if pred else "manual",
        })
        chips.append({
            "label": "DATA",
            "value": "%d marks" % n_marks,
            "change": "%d pairs - all kept" % self.pairs,
            "direction": "neutral", "status": "ok",
        })

        return {
            "category": category,
            "chips": chips,
            "meta": {
                "nifty": price,
                "nifty_day_pct": day,
                "hour_pct": hour_pct,
                "marks": n_marks,
                "pairs": self.pairs,
                "target": TARGET_MARKS,
                "last_mark": (latest or {}).get("time"),
                "first_mark": first.get("time") if first else None,
                "prediction": pred,
                "long_pe": pe,
                "model": {
                    "samples": samples,
                    "features": self.nn.get("features"),
                    "min_samples": MIN_NN_SAMPLES,
                    "fit_mae": self.nn.get("fit_mae"),
                    "fit_hit": self.nn.get("fit_hit"),
                    "val_mae": self.nn.get("val_mae"),
                    "val_hit": self.nn.get("val_hit"),
                    "val_samples": self.nn.get("val_samples"),
                    "live_mae": live.get("mae"),
                    "live_hit": live.get("hit_rate"),
                    "live_scored": live.get("scored"),
                    "top_features": self.nn.get("top_features") or [],
                    "status": self.nn.get("status"),
                },
            },
        }


# --------------------------------------------------------------------------
# Snapshot assembly (same live sources as V1, V2 learning only)
# --------------------------------------------------------------------------

CATEGORY_ORDER = mm.CATEGORY_ORDER
CATEGORY_SHORT = mm.CATEGORY_SHORT
KEY_CHIPS = mm.KEY_CHIPS


def build_snapshot():
    errors = []
    raw = {}
    started = time.time()

    with ThreadPoolExecutor(max_workers=14) as pool:
        futures = {
            "indices": pool.submit(mm.fetch_indices),
            "fii": pool.submit(mm.fetch_fii),
            "chain": pool.submit(mm.fetch_chain_bundle),
            "gift": pool.submit(mm.fetch_gift),
            "deals": pool.submit(mm.fetch_block_deals),
            "news": pool.submit(mm.fetch_news_batch),
            "te": pool.submit(mm.fetch_te_batch),
            "mc": pool.submit(mm.fetch_results_today),
        }
        for symbol in mm.TICKERS:
            futures["quote:" + symbol] = pool.submit(mm.yahoo_quote, symbol)
        for key, fut in futures.items():
            try:
                raw[key] = fut.result(timeout=80)
            except Exception as exc:
                raw[key] = None
                errors.append("%s: %s" % (key, exc))

    quotes = {}
    for key, value in raw.items():
        if key.startswith("quote:") and value:
            quotes[key[6:]] = value
    indices = raw.get("indices")
    fii_data = raw.get("fii")
    chain, metrics = raw.get("chain") or (None, None)
    if metrics is not None:
        metrics["nifty_pct"] = mm.nifty_pct(indices)
    gift = raw.get("gift")
    deals = raw.get("deals")
    news = raw.get("news") or {}
    te = raw.get("te") or {}
    mc = raw.get("mc") or []

    builders = [
        ("1. CRUDE OIL", lambda: mm.cat_crude(quotes)),
        ("2. CURRENCY", lambda: mm.cat_currency(quotes)),
        ("3. FOREIGN FLOWS", lambda: mm.cat_flows(fii_data, indices, news)),
        ("4. GLOBAL MARKETS", lambda: mm.cat_global(quotes, gift, news)),
        ("5. US BOND YIELDS / DOLLAR", lambda: mm.cat_bonds(quotes)),
        ("6. GEOPOLITICS", lambda: mm.cat_geopolitics(news)),
        ("7. MACRO DATA / POLICY", lambda: mm.cat_macro(te, news)),
        ("8. EARNINGS / CORPORATE NEWS", lambda: mm.cat_earnings(mc, news)),
        ("9. SECTOR SIGNALS", lambda: mm.cat_sectors(quotes, news)),
        ("10. SENTIMENT / TECHNICAL",
         lambda: mm.cat_sentiment(indices, fii_data, metrics, deals, quotes)),
        ("11. OPTION CHAIN - NIFTY", lambda: mm.cat_option_chain(metrics)),
    ]
    built = {}
    for name, fn in builders:
        try:
            data = fn()
            if data:
                built[name] = data
            else:
                errors.append("%s: empty result" % name)
        except Exception as exc:
            errors.append("%s: %s" % (name, exc))
            if mm.VERBOSE:
                traceback.print_exc()

    categories = []
    chip_lookup = {}
    counts = {"bullish": 0, "bearish": 0, "neutral": 0, "manual": 0, "warning": 0}
    for name in CATEGORY_ORDER:
        data = built.get(name) or {}
        factors = []
        for key, value in data.items():
            item = dict(value)
            item["key"] = key
            factors.append(item)
            if item.get("direction") == "bullish":
                counts["bullish"] += 1
            elif item.get("direction") == "bearish":
                counts["bearish"] += 1
            else:
                counts["neutral"] += 1
            if item.get("status") == "manual":
                counts["manual"] += 1
            elif item.get("status") == "warning":
                counts["warning"] += 1
            chip_lookup[(name, key)] = item
        categories.append({"name": name, "short": CATEGORY_SHORT.get(name, name),
                           "num": name.split(".")[0], "factors": factors})

    chips = []
    for cat, key, label in KEY_CHIPS:
        item = chip_lookup.get((cat, key))
        if not item:
            continue
        chips.append({"label": label, "value": item["value"],
                      "change": item["change"],
                      "direction": item["direction"],
                      "status": item["status"]})

    track_meta = {}
    try:
        nifty = mm.nifty_mark(indices, quotes)
        track = TRACKER.apply(categories, nifty)
        if track:
            track_meta = track.get("meta") or {}
            if track.get("category"):
                categories.insert(0, track["category"])
            chips = (track.get("chips") or []) + chips
    except Exception as exc:
        errors.append("V2 tracker: %s" % exc)
        if mm.VERBOSE:
            traceback.print_exc()

    total = counts["bullish"] + counts["bearish"]
    sentiment = round(counts["bullish"] / total * 100) if total else 50
    return {
        "updated": stamp(),
        "updated_ts": time.time(),
        "market_open": is_market_open(),
        "sentiment": sentiment,
        "counts": counts,
        "chips": chips,
        "categories": categories,
        "track": track_meta,
        "errors": errors[:8],
        "duration": round(time.time() - started, 1),
        "v2": True,
        "storage": "SQLite data/v2/market.db",
        "target_marks": TARGET_MARKS,
    }


def merge_previous(snapshot, previous):
    if not previous or not previous.get("categories"):
        return snapshot
    have = {c["name"] for c in snapshot["categories"] if c.get("factors")}
    for old in previous["categories"]:
        if old["name"] in have:
            continue
        factors = []
        for item in old.get("factors") or []:
            f = dict(item)
            f["stale"] = True
            if f.get("status") == "ok":
                f["status"] = "warning"
            f["details"] = ((f.get("details") or "") + " | ").lstrip(" |") + \
                "STALE: cached data - refresh failed"
            factors.append(f)
        if factors:
            snapshot["categories"].append({
                "name": old["name"], "short": old.get("short", old["name"]),
                "num": old.get("num"), "factors": factors})
    order = {TRACK_CATEGORY: -1}
    order.update({n: i for i, n in enumerate(CATEGORY_ORDER)})
    snapshot["categories"].sort(key=lambda c: order.get(c["name"], 99))
    counts = {"bullish": 0, "bearish": 0, "neutral": 0, "manual": 0, "warning": 0}
    for cat in snapshot["categories"]:
        if cat.get("name") == TRACK_CATEGORY:
            continue
        for f in cat["factors"]:
            if f.get("direction") == "bullish":
                counts["bullish"] += 1
            elif f.get("direction") == "bearish":
                counts["bearish"] += 1
            else:
                counts["neutral"] += 1
            if f.get("status") == "manual":
                counts["manual"] += 1
            elif f.get("status") == "warning":
                counts["warning"] += 1
    snapshot["counts"] = counts
    total = counts["bullish"] + counts["bearish"]
    snapshot["sentiment"] = round(counts["bullish"] / total * 100) if total else 50
    return snapshot


def load_disk_snapshot():
    data = _read_json(SNAPSHOT_FILE, None)
    if isinstance(data, dict) and data.get("categories"):
        return data
    return None


def save_disk_snapshot(snapshot):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception:
        pass


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.snapshot = load_disk_snapshot()
        self.refreshing = False
        self.last_error = ""
        self.last_attempt = 0.0

    def get(self):
        with self.lock:
            return self.snapshot

    def age(self):
        snap = self.get() or {}
        ts = snap.get("updated_ts") or 0
        return time.time() - ts if ts else 1e9

    def ensure_fresh(self, max_age=SNAPSHOT_TTL):
        if self.age() > max_age:
            self.refresh_async()

    def refresh_async(self):
        with self.lock:
            if self.refreshing:
                return False
            self.refreshing = True
            self.last_attempt = time.time()
        threading.Thread(target=self._worker, daemon=True).start()
        return True

    def _worker(self):
        previous = self.get()
        try:
            snap = build_snapshot()
            snap = merge_previous(snap, previous)
            with self.lock:
                self.snapshot = snap
                self.last_error = ""
            save_disk_snapshot(snap)
            print("[%s] V2 refreshed in %.1fs (%d factors, %d errors)"
                  % (stamp(), snap.get("duration", 0),
                     sum(len(c["factors"]) for c in snap["categories"]),
                     len(snap.get("errors") or [])))
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            print("[%s] V2 refresh failed: %s" % (stamp(), self.last_error))
            if mm.VERBOSE:
                traceback.print_exc()
        finally:
            with self.lock:
                self.refreshing = False

    def status(self):
        snap = self.get() or {}
        with self.lock:
            return {"refreshing": self.refreshing, "ready": bool(snap),
                    "updated": snap.get("updated"), "age": round(self.age()) if snap else None,
                    "error": self.last_error, "last_attempt": self.last_attempt}


# --------------------------------------------------------------------------
# V2 page
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1420">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Market Pulse V2">
<title>Pre-Market Pulse V2 - Long-Term Lab</title>
<style>
:root{
  --bg:#0f1420; --card:#161f33; --card2:#111a2b; --fg:#e8edf7; --mut:#8b97ad;
  --line:#243149; --accent:#e94560; --green:#00c853; --red:#ff4b5c;
  --amber:#ffb300; --blue:#4aa3ff; --violet:#b18cff;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:15px;line-height:1.35;padding-bottom:56px;
  -webkit-font-smoothing:antialiased;text-size-adjust:100%;
}
header{
  position:sticky;top:0;z-index:30;padding:calc(8px + env(safe-area-inset-top)) 12px 8px;
  background:rgba(15,20,32,.94);backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line);
}
.titlebar{display:flex;align-items:center;gap:8px}
.logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--violet),#2b6cff);
  display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff;flex:none}
h1{font-size:15px;letter-spacing:.6px;margin:0;font-weight:800;flex:1}
.v2tag{font-size:9px;font-weight:900;letter-spacing:.8px;padding:2px 6px;border-radius:4px;
  color:#0b0620;background:var(--violet);vertical-align:middle;margin-left:4px}
.badge{font-size:10px;font-weight:800;letter-spacing:.4px;padding:3px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.badge.on{color:#04120a;background:var(--green);border-color:var(--green)}
.badge.off{color:var(--mut)}
.meta{display:flex;align-items:center;gap:8px;margin-top:4px;font-size:11px;color:var(--mut);flex-wrap:wrap}
.meta .dot{width:4px;height:4px;border-radius:50%;background:var(--line)}
.meta .grow{flex:1}
button, a.btn{
  font:inherit;font-size:12px;font-weight:700;color:var(--fg);background:var(--card);
  border:1px solid var(--line);border-radius:9px;padding:6px 10px;text-decoration:none;
  display:inline-flex;align-items:center;gap:5px;cursor:pointer;
}
button:active,a.btn:active{transform:translateY(1px)}
button:disabled{opacity:.55}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.sentbar{margin-top:8px}
.meter{height:9px;border-radius:99px;background:#0a1120;overflow:hidden;border:1px solid var(--line)}
.meter>i{display:block;height:100%;width:50%;background:var(--amber);transition:width .5s ease,background .3s}
.meter.small{height:5px}
.sentrow{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-top:4px}
.sentrow b{color:var(--fg);font-size:12px}
.biasbars{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:7px}
.biasbar{min-width:0}
.biashead{display:flex;align-items:baseline;justify-content:space-between;gap:6px;
  font-size:9.5px;font-weight:800;letter-spacing:.45px;color:var(--mut);white-space:nowrap}
.biashead b{font-size:10.5px}
.biashead b.g{color:var(--green)}
.biashead b.r{color:var(--red)}
.split{display:flex;height:8px;border-radius:99px;overflow:hidden;border:1px solid var(--line);
  background:#0a1120;margin-top:3px}
.split i{display:block;height:100%;width:50%;transition:width .5s ease}
.split .bull{background:var(--green)}
.split .bear{background:var(--red)}
.split.muted{opacity:.55}
.dataprogress{margin-top:7px}
.dataprogress .biashead{color:#71809b}
.dataprogress .meter>i{background:linear-gradient(90deg,#2b6cff,var(--violet));width:0}
.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px}
.chip{background:var(--card2);border:1px solid var(--line);border-radius:9px;padding:5px 7px;min-width:0}
.chip .k{font-size:9px;letter-spacing:.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .v{font-size:12px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .c{font-size:10px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toolbar{display:flex;gap:6px;padding:8px 12px 2px;align-items:center}
.search{flex:1;display:flex;align-items:center;gap:6px;background:var(--card);
  border:1px solid var(--line);border-radius:10px;padding:6px 9px;min-width:0}
.search input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:var(--fg);font:inherit;font-size:13px}
.search input::placeholder{color:#5d6b83}
.iconbtn{padding:6px 9px}
.nav{display:flex;gap:6px;overflow-x:auto;padding:8px 12px 4px;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav a{flex:none;font-size:11px;color:var(--mut);border:1px solid var(--line);
  border-radius:99px;padding:4px 9px;text-decoration:none;background:var(--card2)}
main{padding:0 10px}
.cat{background:var(--card);border:1px solid var(--line);border-radius:13px;margin:8px 0;overflow:hidden}
.cat-head{width:100%;display:flex;align-items:center;gap:8px;background:transparent;
  border:0;border-radius:0;padding:11px 12px;text-align:left;color:var(--fg)}
.cat-head .num{width:22px;height:22px;border-radius:7px;background:#0b1322;border:1px solid var(--line);
  display:grid;place-items:center;font-size:10px;font-weight:800;color:var(--violet);flex:none}
.cat-head .nm{flex:1;font-size:13px;font-weight:800;letter-spacing:.2px}
.cat-head .cnt{font-size:10px;color:var(--mut);white-space:nowrap}
.cat-head .cnt b.b{color:var(--green)}
.cat-head .cnt b.r{color:var(--red)}
.cat-head .chev{color:var(--mut);font-size:15px;transition:transform .2s;flex:none}
.cat:not(.open) .cat-head .chev{transform:rotate(-90deg)}
.cat-body{display:none;border-top:1px solid var(--line)}
.cat.open .cat-body{display:block}
.row{display:flex;gap:8px;padding:9px 11px 9px 0;border-bottom:1px solid rgba(36,49,73,.65);
  border-left:3px solid var(--mut);position:relative}
.row:last-child{border-bottom:0}
.row.g{border-left-color:var(--green)}
.row.r{border-left-color:var(--red)}
.row.n{border-left-color:#5d6b83}
.row.warn{border-left-color:var(--amber)}
.row .tick{padding:0 0 0 8px;display:flex;align-items:flex-start;padding-top:2px;flex:none}
.row .tick.blank{width:25px;padding:0}
.row .tick input{appearance:none;-webkit-appearance:none;width:17px;height:17px;border-radius:5px;
  border:1.5px solid #43536f;background:#0b1322;display:grid;place-items:center;margin:0}
.row .tick input:checked{background:var(--accent);border-color:var(--accent)}
.row .tick input:checked::after{content:"";width:8px;height:4px;border-left:2px solid #fff;
  border-bottom:2px solid #fff;transform:rotate(-45deg);margin-top:-2px}
.row .body{flex:1;min-width:0}
.row .top{display:flex;gap:8px;align-items:baseline}
.row .lbl{flex:1;font-size:12.5px;font-weight:700;min-width:0}
.row .val{font-size:12.5px;font-weight:800;text-align:right;white-space:normal;max-width:52%;word-break:break-word}
.row .val em{font-style:normal;font-size:11px;font-weight:700;display:block;color:var(--mut)}
.row.g .val{color:var(--green)}
.row.r .val{color:var(--red)}
.row.warn .val{color:var(--amber)}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.tag{font-size:8.5px;font-weight:800;letter-spacing:.4px;padding:1px 5px;border-radius:4px;
  border:1px solid var(--line);color:var(--mut)}
.tag.manual{color:#141005;background:var(--amber);border-color:var(--amber)}
.tag.warning{color:#1a1200;background:#ffd166;border-color:#ffd166}
.tag.error{color:#fff;background:var(--red);border-color:var(--red)}
.tag.stale{color:#141005;background:#ff9800;border-color:#ff9800}
.nodes{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.node{font-size:8.5px;font-weight:800;letter-spacing:.3px;padding:1px 5px;border-radius:4px;
  border:1px solid var(--line);color:var(--mut);background:#0b1322;white-space:nowrap}
.node b{font-size:9.5px;color:var(--fg)}
.node.p b{color:var(--blue)}
.node.e.g{border-color:rgba(0,200,83,.45)}
.node.e.g b{color:var(--green)}
.node.e.r{border-color:rgba(255,75,92,.45)}
.node.e.r b{color:var(--red)}
.node.e.a b{color:var(--amber)}
.node.c b{color:var(--violet)}
.cat.live{border-color:rgba(177,140,255,.5)}
.cat.live .cat-head .num{color:var(--violet);border-color:rgba(177,140,255,.5)}
.det{font-size:11.5px;color:var(--mut);margin-top:3px;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.row.open .det{-webkit-line-clamp:unset}
.src{font-size:9.5px;color:#546178;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{padding:18px;text-align:center;color:var(--mut);font-size:13px}
.foot{padding:14px 12px calc(18px + env(safe-area-inset-bottom));color:#5d6b83;font-size:10.5px;text-align:center;line-height:1.55}
.errbox{margin:8px 10px;border:1px solid #4a2b2f;background:#24161a;color:#ffb3bb;border-radius:10px;
  padding:8px 10px;font-size:11px}
.errbox b{display:block;margin-bottom:2px;color:var(--red)}
@media (max-width:520px){
  .row .top{flex-wrap:wrap;gap:1px}
  .row .val{max-width:100%;text-align:left;word-break:break-word}
  .row .val em{display:inline;margin-left:5px}
  header{padding-bottom:6px}
  .chips{gap:5px}
  .biasbars{grid-template-columns:1fr}
}
@media (min-width:700px){ main{max-width:760px;margin:0 auto} header>div{max-width:760px;margin:0 auto} }
@media (min-width:1100px){
  body{font-size:15.5px}
  main{max-width:1120px;margin:0 auto}
  header>div{max-width:1120px;margin:0 auto}
  .row .det{max-width:82%}
}
</style>
</head>
<body>
<header>
  <div>
    <div class="titlebar">
      <div class="logo">&#9650;</div>
      <h1>PRE-MARKET PULSE <span class="v2tag">V2 LAB</span></h1>
      <span id="market" class="badge off">...</span>
    </div>
    <div class="meta">
      <span id="updated">waiting for first refresh</span>
      <span class="dot"></span>
      <span id="dataset">data: --</span>
      <span class="dot"></span>
      <span id="live">auto in <b id="count">60</b>s</span>
      <span class="grow"></span>
      <button id="btnRefresh" class="primary" type="button">&#8635; Refresh</button>
    </div>
    <div class="sentbar">
      <div class="meter"><i id="meterFill"></i></div>
      <div class="sentrow">
        <span><b id="sentPct">--%</b> bullish <span id="sentCounts"></span></span>
        <span id="refreshState"></span>
      </div>
      <div class="biasbars">
        <div class="biasbar" title="Long-term P/E: power x confidence weighted effect across ticked factors. Missing or zero delta counts as neutral 50.">
          <div class="biashead"><span>LONG-TERM P/E</span><span id="peVals">-- / --</span></div>
          <div class="split" id="peBar" role="img" aria-label="Long-term power/effect bias">
            <i class="bull" id="peBullFill"></i><i class="bear" id="peBearFill"></i>
          </div>
        </div>
        <div class="biasbar" id="nnBias" title="New V2 neural net next-hour NIFTY bias (bull / bear, out of 100).">
          <div class="biashead"><span>NN V2</span><span id="nnVals">warming up</span></div>
          <div class="split muted" id="nnBar" role="img" aria-label="NN V2 warming up">
            <i class="bull" id="nnBullFill"></i><i class="bear" id="nnBearFill"></i>
          </div>
        </div>
      </div>
      <div class="dataprogress">
        <div class="biashead"><span>LONG-TERM DATA</span><span id="dataVals">0 / 2000 marks</span></div>
        <div class="meter small"><i id="dataFill"></i></div>
      </div>
    </div>
    <div class="chips" id="chips"></div>
  </div>
</header>

<div class="toolbar">
  <label class="search">&#128269;
    <input id="q" type="search" placeholder="Filter factors, details, source..." autocomplete="off">
  </label>
  <button id="btnToggle" class="iconbtn" type="button" title="Expand / collapse all">&#9776;</button>
  <a class="btn iconbtn" href="/marks.json" title="Download the V2 SQLite-backed mark log" target="_blank">LOG</a>
  <a class="btn iconbtn" href="/export.csv" title="Download CSV">CSV</a>
  <a class="btn iconbtn" href="/api/model" title="View the V2 model state" target="_blank">MODEL</a>
</div>

<nav class="nav" id="nav"></nav>

<main id="cats"><div class="empty">Fetching live data&hellip; this can take up to 60 seconds on first run.</div></main>

<div id="errs"></div>
<div class="foot">
  <b>V2 LAB</b> &mdash; separate process, separate SQLite store, V1 untouched.<br>
  <b>P</b> = long-term power (0-100): lagged correlation between a factor's hourly delta and the <i>next</i> hourly NIFTY return, shrunk for sample size.<br>
  <b>E</b> = long-term effect (0-100): learned sign &times; robust current move; missing/zero delta = 50. <b>c</b> = confidence n/(n+30).<br>
  <b>LONG-TERM P/E</b> = power &times; confidence weighted effect among ticked factors. <b>NN V2</b> = new net, 50 neutral, &plusmn;1% = full bull/bear.<br>
  All hourly marks are appended to <code>data/v2/market.db</code>; the latest 2,000 are always retained and older data is kept too.
  <a href="/marks.json" target="_blank" style="color:var(--blue)">Mark log</a> &middot;
  <a href="/api/model" target="_blank" style="color:var(--blue)">Model</a><br>
  Local dashboard &mdash; not investment advice. Tick factors to recompute the bias meter.
</div>

<script>
"use strict";
var $ = function(s, r){ return (r || document).querySelector(s); };
var state = { data:null, unticked:new Set(), fetching:false, left:60, allOpen:true };

function esc(v){
  return String(v == null ? "" : v).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function el(id){ return document.getElementById(id); }
function num(v, d){ var n = Number(v); return isFinite(n) ? n : d; }
function saveTicks(){
  try { localStorage.setItem("pmp_v2_unticked", JSON.stringify(Array.from(state.unticked))); } catch(e){}
}
function loadTicks(){
  try {
    var raw = localStorage.getItem("pmp_v2_unticked");
    if (raw) { state.unticked = new Set(JSON.parse(raw)); }
  } catch(e){ state.unticked = new Set(); }
}
function isTicked(id){ return !state.unticked.has(id); }
function dirClass(f){
  if (f.status === "manual" || f.status === "warning") return "warn";
  if (f.direction === "bullish") return "g";
  if (f.direction === "bearish") return "r";
  return "n";
}
function tags(f){
  var out = "";
  if (f.status === "manual") out += '<span class="tag manual">MANUAL</span>';
  if (f.status === "warning") out += '<span class="tag warning">WARNING</span>';
  if (f.status === "error") out += '<span class="tag error">ERROR</span>';
  if (f.stale) out += '<span class="tag stale">STALE</span>';
  return out ? '<div class="tags">' + out + '</div>' : "";
}
function rowHtml(cat, f){
  var id = cat.name + "::" + f.key;
  var checked = isTicked(id) ? " checked" : "";
  var change = f.change ? '<em>' + esc(f.change) + '</em>' : "";
  var src = [f.source, f.updated].filter(Boolean).map(esc).join(" \u00b7 ");
  var p = Math.round(num(f.power_long, num(f.power, 0)));
  var e = Math.round(num(f.effect_long, num(f.effect, 50)));
  var c = Math.round(num(f.confidence, 0) * 100);
  var n = num(f.power_samples, 0);
  var ptitle = "Long-term power: |r| after small-sample shrinkage (0-100).";
  if (f.power_corr != null) {
    ptitle += " | r=" + Number(f.power_corr).toFixed(2) +
      " over " + n + " lagged pairs";
  }
  ptitle += " | confidence " + c + "%";
  var etitle = "Long-term effect: learned sign x robust current move. " +
    "Missing/zero delta = 50.";
  if (f.learn_sign != null) etitle += " | learned sign " + f.learn_sign;
  if (f.node_delta != null) etitle += " | delta " + Number(f.node_delta).toFixed(4);
  if (f.effect_samples != null) etitle += " | recent n=" + f.effect_samples;
  var ecls = e >= 60 ? "g" : e <= 40 ? "r" : "a";
  var track = (cat.name || "").indexOf("0.") === 0;
  var nodes = track ? "" : '<div class="nodes">' +
    '<span class="node p" title="' + esc(ptitle) + '">P <b>' + p + '</b> c' + c + '%</span>' +
    '<span class="node e ' + ecls + '" title="' + esc(etitle) + '">E <b>' + e + '</b></span>' +
    '<span class="node c" title="Lagged pairs used for power">n <b>' + n + '</b></span>' +
    '</div>';
  var tick = track
    ? '<span class="tick blank"></span>'
    : '<label class="tick"><input class="tickbox" type="checkbox" data-id="' + esc(id) + '"' + checked + '></label>';
  return '<div class="row ' + dirClass(f) + '" data-id="' + esc(id) + '" data-text="' +
    esc((f.label + " " + f.value + " " + f.change + " " + f.details + " " + f.source).toLowerCase()) + '">' +
    tick +
    '<div class="body">' +
      '<div class="top"><span class="lbl">' + esc(f.label) + '</span>' +
      '<span class="val">' + esc(f.value) + change + '</span></div>' +
      tags(f) + nodes +
      (f.details ? '<div class="det">' + esc(f.details) + '</div>' : "") +
      (src ? '<div class="src">' + src + '</div>' : "") +
    '</div></div>';
}
function catHtml(cat, idx){
  var bull = 0, bear = 0;
  cat.factors.forEach(function(f){
    if (f.direction === "bullish") bull++;
    else if (f.direction === "bearish") bear++;
  });
  var openCls = state.allOpen ? " open" : "";
  var liveCls = (cat.name || "").indexOf("0.") === 0 ? " live" : "";
  var num = cat.num != null ? cat.num : ((cat.name || "").split(".")[0] || (idx + 1));
  var rows = cat.factors.map(function(f){ return rowHtml(cat, f); }).join("");
  return '<section class="cat' + openCls + liveCls + '" id="cat-' + idx + '" data-name="' + esc(cat.name) + '">' +
    '<button class="cat-head" type="button" aria-expanded="true">' +
      '<span class="num">' + esc(num) + '</span>' +
      '<span class="nm">' + esc(cat.short || cat.name) + '</span>' +
      '<span class="cnt"><b class="b">' + bull + '</b> up &nbsp;<b class="r">' + bear + '</b> dn</span>' +
      '<span class="chev">&#9662;</span>' +
    '</button>' +
    '<div class="cat-body">' + (rows || '<div class="empty">No data for this section</div>') + '</div>' +
  '</section>';
}
function chipHtml(c){
  var color = c.direction === "bullish" ? "var(--green)" :
              c.direction === "bearish" ? "var(--red)" : "var(--fg)";
  return '<div class="chip"><div class="k">' + esc(c.label) + '</div>' +
    '<div class="v" style="color:' + color + '">' + esc(c.value) + '</div>' +
    '<div class="c">' + esc(c.change || "\u00a0") + '</div></div>';
}
function setBiasBar(prefix, bull, bear, label, muted){
  var b = num(bull, 50), r = num(bear, 100 - b);
  b = Math.max(0, Math.min(100, b));
  r = Math.max(0, Math.min(100, r));
  var sum = b + r;
  if (sum <= 0) { b = 50; r = 50; sum = 100; }
  var bw = b / sum * 100;
  var bf = el(prefix + "BullFill"), rf = el(prefix + "BearFill");
  if (bf) bf.style.width = bw + "%";
  if (rf) rf.style.width = (100 - bw) + "%";
  var bar = el(prefix + "Bar");
  if (bar) {
    bar.classList.toggle("muted", !!muted);
    bar.setAttribute("aria-label", label);
  }
  var vals = el(prefix + "Vals");
  if (vals) vals.textContent = label;
}
function computeBiasBars(){
  if (!state.data || !state.data.categories) return;
  var score = 0, weight = 0;
  state.data.categories.forEach(function(cat){
    if ((cat.name || "").indexOf("0.") === 0) return;
    cat.factors.forEach(function(f){
      var id = cat.name + "::" + f.key;
      if (!isTicked(id)) return;
      var e = num(f.effect_long, num(f.effect, 50));
      var p = num(f.power_long, num(f.power, 0));
      var c = num(f.confidence, 0);
      e = Math.max(0, Math.min(100, e));
      p = Math.max(0, Math.min(100, p));
      c = Math.max(0, Math.min(1, c));
      var w = (p / 100) * c;
      if (w <= 0) return;
      var d = f.node_delta;
      if (d == null || !isFinite(Number(d)) || Math.abs(Number(d)) < 1e-9) e = 50;
      score += e * w;
      weight += w;
    });
  });
  var peBull = weight ? score / weight : 50;
  setBiasBar("pe", peBull, 100 - peBull,
    Math.round(peBull) + " / " + Math.round(100 - peBull), weight <= 0);

  var t = state.data.track || {};
  var pred = t.prediction;
  var nnBull = 50, nnBear = 50, muted = true, label = "warming up";
  if (pred && pred.pct != null && isFinite(Number(pred.pct))) {
    var pct = Number(pred.pct);
    nnBull = Math.max(0, Math.min(100, 50 + pct * 50));
    nnBear = 100 - nnBull;
    muted = false;
    label = Math.round(nnBull) + " / " + Math.round(nnBear);
    el("nnBias").title = "NN V2 next-hour NIFTY bias: " +
      (pct >= 0 ? "+" : "") + pct.toFixed(2) +
      "% | 50 = neutral, +1% = full bull, -1% = full bear.";
  } else {
    var model = t.model || {};
    var samples = num(model.samples, 0);
    var need = num(model.min_samples, 60);
    var top = model.top_features || [];
    if (!top.length) {
      label = "waiting for power";
      el("nnBias").title = "NN V2 waits for at least one factor with a " +
        "significant long-term lagged signal.";
    } else {
      label = "warming " + samples + "/" + need;
      el("nnBias").title = "NN V2 waits for " + need +
        " time-ordered pairs before its first prediction.";
    }
  }
  setBiasBar("nn", nnBull, nnBear, label, muted);
}
function computeDataBar(){
  var t = state.data && state.data.track ? state.data.track : {};
  var marks = num(t.marks, 0);
  var target = Math.max(2000, num(t.target, 2000), marks);
  var pct = target ? Math.min(100, marks / target * 100) : 0;
  el("dataFill").style.width = pct + "%";
  el("dataVals").textContent = marks.toLocaleString() + " / " +
    target.toLocaleString() + " marks";
  el("dataset").textContent = "data: " + marks.toLocaleString() + " marks, " +
    num(t.pairs, 0).toLocaleString() + " pairs";
}
function computeSentiment(){
  if (!state.data) return;
  var bull = 0, bear = 0, total = 0;
  state.data.categories.forEach(function(cat){
    if ((cat.name || "").indexOf("0.") === 0) return;
    cat.factors.forEach(function(f){
      var id = cat.name + "::" + f.key;
      if (!isTicked(id)) return;
      total++;
      if (f.direction === "bullish") bull++;
      else if (f.direction === "bearish") bear++;
    });
  });
  var pair = bull + bear;
  var pct = pair ? Math.round(bull / pair * 100) : 50;
  var color = pct >= 60 ? "var(--green)" : pct <= 40 ? "var(--red)" : "var(--amber)";
  el("sentPct").textContent = pct + "%";
  el("sentPct").style.color = color;
  el("meterFill").style.width = pct + "%";
  el("meterFill").style.background = color;
  el("sentCounts").textContent = "(" + bull + " up / " + bear + " down / " + total + " checked)";
  computeBiasBars();
  computeDataBar();
}
function render(){
  var d = state.data;
  if (!d) return;
  el("updated").textContent = d.updated ? "Updated " + d.updated : "waiting for first refresh";
  var mk = el("market");
  mk.textContent = d.market_open ? "MARKET OPEN" : "MARKET CLOSED";
  mk.className = "badge " + (d.market_open ? "on" : "off");
  el("chips").innerHTML = (d.chips || []).map(chipHtml).join("");
  var nav = d.categories.map(function(cat, i){
    var num = cat.num != null ? cat.num : ((cat.name || "").split(".")[0] || (i + 1));
    return '<a href="#cat-' + i + '">' + esc(num) + '. ' + esc(cat.short || cat.name) + '</a>';
  }).join("");
  el("nav").innerHTML = nav;
  el("cats").innerHTML = d.categories.map(catHtml).join("");
  if (!d.categories.length) {
    el("cats").innerHTML = '<div class="empty">' +
      (d.refreshing ? "First refresh in progress&hellip;" : "No data yet - tap Refresh.") + '</div>';
  }
  var errs = (d.errors || []);
  el("errs").innerHTML = errs.length
    ? '<div class="errbox"><b>Partial data</b>' + errs.map(esc).join("<br>") + '</div>'
    : "";
  computeSentiment();
  applyFilter();
}
function applyFilter(){
  var q = (el("q").value || "").trim().toLowerCase();
  document.querySelectorAll("section.cat").forEach(function(sec){
    var visible = 0;
    sec.querySelectorAll(".row").forEach(function(row){
      var hit = !q || (row.getAttribute("data-text") || "").indexOf(q) >= 0;
      row.style.display = hit ? "" : "none";
      if (hit) visible++;
    });
    if (q) {
      sec.style.display = visible ? "" : "none";
      if (visible) sec.classList.add("open");
    } else {
      sec.style.display = "";
      if (!state.allOpen) sec.classList.remove("open");
    }
  });
}
async function load(){
  try {
    var r = await fetch("/api/data", {cache:"no-store"});
    state.data = await r.json();
    el("live").innerHTML = 'auto in <b id="count">' + state.left + '</b>s';
    render();
  } catch(e) {
    el("updated").textContent = "connection lost - retrying";
  }
}
var wasRefreshing = false;
async function loadStatus(){
  try {
    var r = await fetch("/api/status", {cache:"no-store"});
    var s = await r.json();
    el("refreshState").textContent = s.refreshing ? "refreshing\u2026" :
      (s.ready ? "" : (s.error ? "feed unavailable - retrying" : "warming up\u2026"));
    if (wasRefreshing && !s.refreshing) { await load(); }
    else if (!s.refreshing && s.ready && state.data && !(state.data.categories || []).length) {
      await load();
    }
    wasRefreshing = !!s.refreshing;
    return s;
  } catch(e){ return {refreshing:false}; }
}
async function refreshNow(){
  if (state.fetching) return;
  state.fetching = true;
  var btn = el("btnRefresh");
  btn.disabled = true;
  btn.innerHTML = "&#8635; Refreshing\u2026";
  try {
    await fetch("/api/refresh", {method:"POST"});
    for (var i = 0; i < 45; i++) {
      var s = await loadStatus();
      if (!s.refreshing) break;
      await new Promise(function(res){ setTimeout(res, 2000); });
    }
    await load();
  } catch(e){}
  state.fetching = false;
  btn.disabled = false;
  btn.innerHTML = "&#8635; Refresh";
  state.left = 60;
}
el("btnRefresh").addEventListener("click", refreshNow);
el("btnToggle").addEventListener("click", function(){
  state.allOpen = !state.allOpen;
  document.querySelectorAll("section.cat").forEach(function(sec){
    sec.classList.toggle("open", state.allOpen);
  });
});
el("q").addEventListener("input", applyFilter);
el("cats").addEventListener("change", function(e){
  var t = e.target;
  if (!t.classList || !t.classList.contains("tickbox")) return;
  var id = t.getAttribute("data-id");
  if (t.checked) state.unticked.delete(id); else state.unticked.add(id);
  saveTicks();
  computeSentiment();
});
el("cats").addEventListener("click", function(e){
  var head = e.target.closest(".cat-head");
  if (head) {
    var sec = head.parentElement;
    var open = sec.classList.toggle("open");
    head.setAttribute("aria-expanded", open ? "true" : "false");
    return;
  }
  var row = e.target.closest(".row");
  if (row && !e.target.closest(".tick")) row.classList.toggle("open");
});
setInterval(function(){
  state.left--;
  if (state.left <= 0) { state.left = 60; load(); }
  var c = el("count");
  if (c) c.textContent = state.left;
}, 1000);
setInterval(loadStatus, 5000);
setInterval(load, 60000);
document.addEventListener("visibilitychange", function(){
  if (!document.hidden) { state.left = 60; load(); }
});
loadTicks();
load();
loadStatus();
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Pre-Market Pulse V2",
    "short_name": "Pulse V2",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1420",
    "theme_color": "#0f1420",
    "icons": [{"src": "/icon.svg", "sizes": "any",
               "type": "image/svg+xml", "purpose": "any maskable"}],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="42" fill="#0f1420"/>
<path d="M30 125 L66 84 L96 106 L134 55 L162 76" fill="none" stroke="#b18cff" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="162" cy="76" r="9" fill="#00c853"/>
</svg>"""


def csv_bytes(snapshot):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Category", "Factor", "Value", "Change", "Direction",
                     "PowerLong", "EffectLong", "Confidence", "Samples",
                     "Corr", "Delta", "Status", "Details", "Source"])
    for cat in (snapshot or {}).get("categories") or []:
        for f in cat.get("factors") or []:
            writer.writerow([cat.get("name"), f.get("label"), f.get("value"),
                             f.get("change"), f.get("direction"),
                             f.get("power_long"), f.get("effect_long"),
                             f.get("confidence"), f.get("power_samples"),
                             f.get("power_corr"), f.get("node_delta"),
                             f.get("status"), f.get("details"),
                             f.get("source")])
    return buf.getvalue().encode("utf-8-sig")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

STORE = None
TRACKER = None


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MarketPulseV2/2.0"

    def log_message(self, fmt, *args):
        if mm.VERBOSE:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                STORE.ensure_fresh()
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/data":
                STORE.ensure_fresh()
                snap = STORE.get() or {
                    "updated": None, "updated_ts": 0,
                    "market_open": is_market_open(), "sentiment": 50,
                    "counts": {}, "chips": [], "categories": [],
                    "track": {}, "errors": [], "refreshing": STORE.refreshing,
                    "v2": True,
                }
                data = dict(snap)
                data["refreshing"] = STORE.refreshing
                data["server_time"] = stamp()
                self._json(data)
            elif path == "/api/status":
                self._json(STORE.status())
            elif path == "/api/marks":
                q = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                try:
                    limit = int((q.get("limit") or [24])[0])
                except (TypeError, ValueError):
                    limit = 24
                self._json(TRACKER.recent(limit))
            elif path == "/marks.json":
                self._send(200, json.dumps(TRACKER.dump(), ensure_ascii=False),
                           "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="market_pulse_v2_marks.json"'})
            elif path == "/api/model":
                self._json(TRACKER.nn or {})
            elif path == "/export.csv":
                STORE.ensure_fresh()
                body = csv_bytes(STORE.get() or {})
                name = "market_pulse_v2_%s.csv" % now_ist().strftime("%Y%m%d_%H%M")
                self._send(200, body, "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % name})
            elif path == "/manifest.webmanifest":
                self._send(200, json.dumps(MANIFEST), "application/manifest+json")
            elif path == "/icon.svg":
                self._send(200, ICON_SVG, "image/svg+xml")
            elif path == "/api/health":
                self._json({"ok": True, "time": stamp(), "v2": True,
                            "status": STORE.status()})
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/refresh":
            started = STORE.refresh_async()
            self._json({"started": started, "refreshing": STORE.refreshing,
                        "status": STORE.status()})
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def backup_database(store, keep=30):
    """One SQLite backup per day, with a small rolling retention."""
    src = getattr(store, "conn", None)
    if src is None:
        return
    try:
        day = now_ist().strftime("%Y%m%d")
        folder = os.path.join(DATA_DIR, "backups")
        path = os.path.join(folder, "market-%s.db" % day)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        os.makedirs(folder, exist_ok=True)
        with store.lock:
            dst = sqlite3.connect(path)
            try:
                src.backup(dst)
            finally:
                dst.close()
            try:
                src.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
        files = sorted(
            os.path.join(folder, n) for n in os.listdir(folder)
            if n.startswith("market-") and n.endswith(".db"))
        for old in files[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
        print("[%s] V2 backup written: %s" % (stamp(), path))
    except Exception as exc:
        print("[%s] V2 backup skipped: %s" % (stamp(), exc))


def scheduler(api_store, mark_store, market_minutes, off_minutes):
    while True:
        time.sleep(60)
        try:
            backup_database(mark_store)
        except Exception:
            pass
        try:
            if api_store.refreshing:
                continue
            limit = (market_minutes if is_market_open() else off_minutes) * 60
            if api_store.age() > limit:
                print("[%s] V2 scheduled refresh (age %ds)"
                      % (stamp(), int(api_store.age())))
                api_store.refresh_async()
        except Exception:
            pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Pre-Market Pulse V2 - long-term lab")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1)")
    p.add_argument("--lan", action="store_true", help="expose to local network")
    p.add_argument("--port", type=int, default=8081, help="port (default 8081)")
    p.add_argument("--open", action="store_true", help="open a browser")
    p.add_argument("--refresh", type=int, default=10,
                   help="refresh minutes while market open (default 10)")
    p.add_argument("--off-hours", type=int, default=60,
                   help="refresh minutes while closed (default 60)")
    p.add_argument("--verbose", action="store_true", help="log HTTP requests")
    p.add_argument("--record-anytime", action="store_true",
                   help="development only: record marks even when market is closed")
    return p.parse_args(argv)


def main(argv=None):
    global STORE, TRACKER
    args = parse_args(argv)
    mm.VERBOSE = bool(args.verbose)
    host = "0.0.0.0" if args.lan else args.host
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    os.makedirs(DATA_DIR, exist_ok=True)
    store = MarkStore(DB_FILE)
    if store.imported:
        print("Imported %d V1 hourly marks into the new V2 SQLite store (read-only seed)"
              % store.imported)
    TRACKER = V2Tracker(store, record_override=True if args.record_anytime else None)
    STORE = Store()

    cached = STORE.get()
    if cached:
        print("Loaded cached V2 snapshot from %s" % (cached.get("updated") or "disk"))
    else:
        print("No V2 cache yet - building the first snapshot now")
    STORE.refresh_async()
    threading.Thread(target=scheduler,
                     args=(STORE, store, args.refresh, args.off_hours),
                     daemon=True).start()

    try:
        server = QuietServer((host, args.port), Handler)
    except OSError as exc:
        sys.stderr.write("\nCould not start V2 on %s:%s - %s\n"
                         "Try another port: python v2\\market_pulse_v2.py --port 8082\n"
                         % (host, args.port, exc))
        raise SystemExit(1)

    ip = lan_ip()
    print("")
    print("=" * 60)
    print(" PRE-MARKET PULSE V2 - LONG-TERM LAB")
    print("=" * 60)
    print(" Open on this PC :  http://127.0.0.1:%d" % args.port)
    if host in ("0.0.0.0", ""):
        print(" Phone / tablet  :  http://%s:%d" % (ip, args.port))
    else:
        print(" Mode            :  PC only (use --lan for phone/tablet)")
    print(" V1 dashboard    :  untouched (usually http://127.0.0.1:8080)")
    print(" V2 SQLite       :  %s" % DB_FILE)
    print(" V2 marks        :  %d stored, %d learning pairs"
          % (store.count(), TRACKER.pairs))
    print(" Keep this window open during market hours to collect marks.")
    print("=" * 60)
    print("")

    if args.open:
        try:
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d" % args.port)
        except Exception:
            pass

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping V2...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
