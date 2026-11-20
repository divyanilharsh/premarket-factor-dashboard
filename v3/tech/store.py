#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite storage for the V3 technical lab (separate from market.db)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_FILE = os.path.join(BASE_DIR, "data", "v3", "tech.db")

lock = threading.RLock()


def connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    with lock:
        conn = connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    tf TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    o REAL, h REAL, l REAL, c REAL, v REAL,
                    PRIMARY KEY (tf, ts)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    ts INTEGER NOT NULL,
                    tf TEXT NOT NULL,
                    score INTEGER,
                    klass TEXT,
                    gate TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (ts, tf)
                );
                CREATE INDEX IF NOT EXISTS idx_signals_tf ON signals(tf, ts);
            """)
            conn.commit()
        finally:
            conn.close()


def save_timeframe(tf, bars, signal):
    if not bars:
        return 0
    with lock:
        conn = connect()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO candles (tf, ts, o, h, l, c, v) "
                "VALUES (?,?,?,?,?,?,?)",
                [(tf, int(b["t"]), b.get("o"), b.get("h"), b.get("l"),
                  b.get("c"), b.get("v")) for b in bars])
            if signal and signal.get("ok"):
                conn.execute(
                    "INSERT OR REPLACE INTO signals "
                    "(ts, tf, score, klass, gate, payload) VALUES (?,?,?,?,?,?)",
                    (int(signal.get("last_bar_ts") or 0), tf,
                     int(signal.get("score") or 0), signal.get("class"),
                     signal.get("gate"),
                     json.dumps(signal, separators=(",", ":"), default=str)))
            conn.commit()
            return len(bars)
        finally:
            conn.close()


def history(tf, limit=100):
    with lock:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT ts, score, klass, gate, payload FROM signals "
                "WHERE tf = ? ORDER BY ts DESC LIMIT ?", (tf, int(limit))).fetchall()
        finally:
            conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = {}
        out.append(payload)
    return out


def counts():
    with lock:
        conn = connect()
        try:
            candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        finally:
            conn.close()
    return {"candles": int(candles), "signals": int(signals)}
