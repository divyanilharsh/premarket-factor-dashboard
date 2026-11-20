#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Candle fetching and caching for the V3 technical lab.

One symbol (NIFTY 50), six timeframes, disk cache, and a hard request budget:
Yahoo allows only a couple of candle requests per short window from one IP, so
every fetch is spaced by ``MIN_GAP_SECONDS`` and served from cache when fresh.

Nothing here writes outside ``data/v3``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data", "v3")
CACHE_DIR = os.path.join(DATA_DIR, "candles")

SYMBOL = "^NSEI"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# One request every MIN_GAP_SECONDS keeps V1/V2's own Yahoo budget intact.
MIN_GAP_SECONDS = 6.0
MAX_ATTEMPTS = 2
RETRY_SLEEPS = (10, 20)

IST = timezone(timedelta(hours=5, minutes=30))

# timeframe -> yahoo interval, backfill spec, cache ttl
TF_SPEC = {
    "1m":  {"yahoo": "1m",  "range": "5d",    "backfill_days": 28,
            "chunk_days": 7,  "ttl": 300,   "warmup": 500},
    "5m":  {"yahoo": "5m",  "range": "60d",   "backfill_days": 60,
            "chunk_days": None, "ttl": 300, "warmup": 1000},
    "1h":  {"yahoo": "60m", "range": "730d",  "backfill_days": 730,
            "chunk_days": None, "ttl": 900, "warmup": 1000},
    "1d":  {"yahoo": "1d",  "range": "5y",    "backfill_days": 1825,
            "chunk_days": None, "ttl": 3600, "warmup": 1000},
    "1wk": {"yahoo": "1wk", "range": "15y",   "backfill_days": 5475,
            "chunk_days": None, "ttl": 21600, "warmup": 400},
    "1mo": {"yahoo": "1mo", "range": "max",   "backfill_days": None,
            "chunk_days": None, "ttl": 86400, "warmup": 400},
}

ORDER = ("1m", "5m", "1h", "1d", "1wk", "1mo")
# Heavy ranges go last: a fresh session gets throttled if the first request is
# a large one, so warm up small and climb.
BACKFILL_ORDER = ("1d", "1wk", "1mo", "1h", "5m", "1m")

COOLDOWN_UNTIL = [0.0]
WARM = [False]
FAILED_UNTIL = {}

_lock = threading.RLock()
_last_request = [0.0]
_session = None


def market_open(now=None):
    n = now or datetime.now(IST)
    if n.weekday() >= 5:
        return False
    hours = n.hour + n.minute / 60.0
    return 9.25 <= hours < 15.5


def _reset_session():
    """Yahoo flags sessions; a fresh one often gets through."""
    global _session
    with _lock:
        _session = None


def _get_session():
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
        return _session


def _throttle():
    """Global spacing between Yahoo requests. Serialises all fetches."""
    with _lock:
        wait = _last_request[0] + MIN_GAP_SECONDS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def cache_path(tf):
    return os.path.join(CACHE_DIR, "%s.json" % tf)


def _read_cache(tf):
    try:
        with open(cache_path(tf), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("bars"), list):
            return None
        return data
    except (OSError, ValueError):
        return None


def _write_cache(tf, payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload["tf"] = tf
    payload["symbol"] = SYMBOL
    temp = cache_path(tf) + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(temp, cache_path(tf))


def _request(params, attempts=MAX_ATTEMPTS):
    session = _get_session()
    url = YAHOO_URL.format(sym="%5ENSEI")
    last_error = ""
    for attempt in range(attempts):
        if time.time() < COOLDOWN_UNTIL[0]:
            return None, "cooldown (%ds left)" % int(COOLDOWN_UNTIL[0] - time.time())
        _throttle()
        try:
            response = session.get(url, params=params, timeout=25)
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
                result = (payload.get("chart", {}).get("result") or [None])[0]
                return result, ""
            except Exception as exc:
                last_error = "parse: %s" % exc
                continue
        last_error = "HTTP %s" % response.status_code
        if response.status_code in (429, 502, 503, 504):
            # Yahoo allows only a few requests per window, so a 429 is often
            # transient. Retry with growing gaps; only cool down after the
            # whole attempt budget is spent.
            _reset_session()
            time.sleep(RETRY_SLEEPS[min(attempt, len(RETRY_SLEEPS) - 1)])
            continue
        break
    COOLDOWN_UNTIL[0] = time.time() + 600
    return None, last_error


def warm_session():
    """A tiny request that establishes the session before the heavy ranges."""
    if WARM[0]:
        return True
    if COOLDOWN_UNTIL[0] > time.time():
        return False
    for attempt in range(2):
        _throttle()
        try:
            response = _get_session().get(
                YAHOO_URL.format(sym="%5ENSEI"),
                params={"interval": "1d", "range": "5d"}, timeout=20)
        except Exception:
            continue
        if response.status_code == 200:
            WARM[0] = True
            return True
        if response.status_code == 429:
            _reset_session()
            time.sleep(10 + attempt * 5)
            continue
        break
    return False


def _to_bars(result):
    if not result:
        return []
    stamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    volumes = quotes.get("volume") or []
    bars = []
    for i, stamp in enumerate(stamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        bars.append({
            "t": int(stamp),
            "o": float(opens[i]) if i < len(opens) and opens[i] is not None else float(close),
            "h": float(highs[i]) if i < len(highs) and highs[i] is not None else float(close),
            "l": float(lows[i]) if i < len(lows) and lows[i] is not None else float(close),
            "c": float(close),
            "v": float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0,
        })
    return bars


def _merge(bars_a, bars_b):
    merged = {bar["t"]: bar for bar in bars_a}
    merged.update({bar["t"]: bar for bar in bars_b})
    return [merged[key] for key in sorted(merged)]


def _fetch_range(tf, start=None, end=None):
    spec = TF_SPEC[tf]
    params = {"interval": spec["yahoo"], "includePrePost": "false"}
    if start is not None and end is not None:
        params["period1"] = int(start)
        params["period2"] = int(end)
    else:
        params["range"] = spec["range"]
    result, error = _request(params)
    return _to_bars(result), error


def _backfill_1m():
    """Yahoo caps 1m at 7 days per request and ~30 days total."""
    now = time.time()
    days = TF_SPEC["1m"]["backfill_days"]
    chunk = TF_SPEC["1m"]["chunk_days"] * 86400
    collected = []
    errors = []
    end = now
    while end > now - days * 86400:
        start = max(end - chunk, now - days * 86400)
        bars, error = _fetch_range("1m", start, end)
        if error:
            errors.append(error)
        collected = _merge(collected, bars)
        end = start
    return collected, (errors[-1] if errors else "")


def get_series(tf, allow_fetch=True, force=False):
    """Return {'bars', 'stale', 'fetched_ts', 'error'} for one timeframe."""
    if tf not in TF_SPEC:
        raise KeyError("unknown timeframe %r" % tf)
    nse_fallback = None
    if tf in ("1d", "1wk", "1mo"):
        try:
            from tech import nse_archive
            nse = nse_archive.get_series(tf, allow_fetch=allow_fetch)
            count = len(nse.get("bars") or [])
            # Monthly needs 200+ bars for EMA200; NSE's archive stops in 2015,
            # so try Yahoo for the longer monthly series and fall back to NSE.
            need = 200 if tf == "1mo" else 20
            if count >= need:
                return nse
            if count:
                nse_fallback = nse
        except Exception:
            pass
    spec = TF_SPEC[tf]
    cached = _read_cache(tf)
    now = time.time()
    fresh = (cached is not None and
             now - float(cached.get("fetched_ts") or 0) < spec["ttl"])
    if cached and fresh and not force:
        return {"bars": cached["bars"], "stale": False,
                "fetched_ts": cached.get("fetched_ts"), "error": ""}
    if FAILED_UNTIL.get(tf, 0) > now and not force:
        if cached:
            return {"bars": cached["bars"], "stale": True,
                    "fetched_ts": cached.get("fetched_ts"),
                    "error": "retry later (recent failure)"}
        if nse_fallback:
            return nse_fallback
    if not allow_fetch:
        if cached:
            return {"bars": cached["bars"], "stale": True,
                    "fetched_ts": cached.get("fetched_ts"), "error": "fetch disabled"}
        if nse_fallback:
            return nse_fallback
        return {"bars": [], "stale": True, "fetched_ts": None,
                "error": "no cache"}
    if not warm_session():
        if cached:
            return {"bars": cached["bars"], "stale": True,
                    "fetched_ts": cached.get("fetched_ts"),
                    "error": "throttled; serving cache"}
        if nse_fallback:
            return nse_fallback
        return {"bars": [], "stale": True, "fetched_ts": None,
                "error": "throttled; no cache yet"}

    bars, error = _fetch_range(tf)

    if bars:
        if cached and tf in ("1m", "5m", "1h"):
            bars = _merge(cached.get("bars") or [], bars)
        payload = {"fetched_ts": now, "bars": bars,
                   "interval": spec["yahoo"], "error": ""}
        _write_cache(tf, payload)
        return {"bars": bars, "stale": False, "fetched_ts": now, "error": ""}

    if cached:
        return {"bars": cached["bars"], "stale": True,
                "fetched_ts": cached.get("fetched_ts"), "error": error}
    FAILED_UNTIL[tf] = time.time() + 3600
    if nse_fallback:
        return nse_fallback
    return {"bars": [], "stale": True, "fetched_ts": None, "error": error}


def staleness(tf):
    cached = _read_cache(tf)
    if not cached:
        return float("inf")
    return time.time() - float(cached.get("fetched_ts") or 0)


def next_due_tf():
    """The timeframe most overdue for a refresh, or None if all are fresh."""
    best = None
    best_ratio = 0.0
    for tf in ORDER:
        ratio = staleness(tf) / TF_SPEC[tf]["ttl"]
        if ratio > 1.0 and ratio > best_ratio:
            best, best_ratio = tf, ratio
    return best


def warm_all():
    """Backfill every timeframe once, spaced by the global limiter."""
    results = {}
    for tf in ORDER:
        results[tf] = get_series(tf, force=True)
    return results
