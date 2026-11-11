#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
PRE-MARKET PULSE - one-file live market dashboard (PC version)
==============================================================

Runs all 11 pre-market factor checks from this project (crude, currency,
FII/DII flows, global markets, bonds/dollar, geopolitics, macro, earnings,
sectors, sentiment/technical, NIFTY option chain) and serves them as a
local web page. The only external dependency is ``requests``.

NEW IN THIS VERSION
-------------------
* Every factor has a **power node** and an **effect node** (both start at
  100). Effect follows the requested mapping: bullish 75-100 (all-time
  biggest positive move = 100), bearish 0-25, neutral 50.
* A full-data "screenshot" (all factor values + NIFTY 50) is saved every
  hour while the market is open in ``data/mobile/hourly.json``.
* Power nodes learn each factor's importance from the correlation between
  its hourly change and NIFTY's hourly return (after 6 paired marks).
* A tiny pure-Python neural net (6 hidden units) is retrained on every
  hourly mark and predicts the next hourly NIFTY move.

The tracker needs the app to be running during market hours. It writes one
mark per hour (09:15-15:30 IST, weekdays). Power nodes need 6 paired marks;
the first NN prediction appears after 4 pairs (about 5 marks). State files:
``data/mobile/hourly.json``, ``nodes.json`` and ``nn.json``.

QUICK START (PC ONLY)
---------------------
    pip install requests
    python market_mobile.py

Then open ``http://127.0.0.1:8080``. The server binds to localhost by
default. Use ``--open`` to launch the browser automatically.

OPTIONAL: PHONE / TABLET ON THE SAME WI-FI
------------------------------------------
    python market_mobile.py --lan

The script prints a LAN URL and writes ``data/mobile/phone_qr.png`` when the
optional ``qrcode`` package is installed.

OPTIONS
-------
    python market_mobile.py --open          # open a browser on this PC
    python market_mobile.py --lan           # also serve to the local network
    python market_mobile.py --port 8080
    python market_mobile.py --refresh 5     # market-hours refresh minutes
    python market_mobile.py --off-hours 60  # closed-market refresh minutes

Endpoints: /             dashboard page
           /api/data     JSON snapshot (includes power/effect + track)
           /api/hourly   recent hourly marks (JSON)
           /hourly.json  full hourly log (download)
           /api/refresh  POST, start a refresh
           /api/status   refresh state
           /export.csv   CSV download
"""

import argparse
import bisect
import csv
import io
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "This app needs the 'requests' package.\n"
        "Install it with:  pip install requests\n"
    )
    raise SystemExit(1)

try:  # optional: lets the server render a scannable QR code for the phone
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "mobile")
SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshot.json")
FII_HISTORY_FILE = os.path.join(DATA_DIR, "fii_history.json")
LEGACY_FII_HISTORY = os.path.join(DATA_DIR, "data", "cache", "fii_history.json")

# Hourly tracker: one "screenshot" of every factor every hour the market is
# open, plus the learned power/effect nodes and the tiny prediction net.
HOURLY_FILE = os.path.join(DATA_DIR, "hourly.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
NN_FILE = os.path.join(DATA_DIR, "nn.json")
HOURLY_MAX = 400
TRACK_CATEGORY = "0. HOURLY TRACK"

SNAPSHOT_TTL = 300          # seconds - rebuild when the page asks and data is older

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

NSE_BASE = "https://www.nseindia.com"
NSE_HOME = NSE_BASE + "/"
NSE_FII_DII = "/api/fiidiiTradeReact"
NSE_ALL_INDICES = "/api/allIndices"
NSE_OC_CONTRACT = "/api/option-chain-contract-info?symbol=NIFTY"
NSE_OC_V3 = ("/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry={expiry}")
NSE_GIFT = "/api/NextApi/apiClient?functionName=getGiftNifty"
NSE_BLOCK_DEALS = "/api/NextApi/apiClient/egrApi?functionName=getEGRBlockDealData"
NSE_OC_REFERER = NSE_BASE + "/option-chain"

GOOGLE_NEWS = ("https://news.google.com/rss/search?q={q}"
               "&hl=en-IN&gl=IN&ceid=IN:en")
MONEYCONTROL_CAL = ("https://www.moneycontrol.com/markets/earnings/results-calendar"
                    "?id=All&name=All&activeDate={date}")
YAHOO_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               "?range=5d&interval=1d")

TICKERS = [
    "BZ=F", "CL=F", "INR=X",
    "^DJI", "^GSPC", "^IXIC", "^N225", "^HSI", "000001.SS",
    "^TNX", "DX-Y.NYB", "^NSEI", "^INDIAVIX",
]

NEWS_QUERIES = {
    "oil_tensions": "middle east oil supply tensions",
    "trade_war": "trade war tariffs",
    "sanctions": "sanctions military action",
    "us_india_tariff": "US India tariff trade",
    "peace_talks": "peace talks ukraine russia",
    "fed": "Federal Reserve FOMC rate decision",
    "yen": "yen intervention BOJ currency",
    "earnings": "earnings results NSE BSE quarterly",
    "orders": "order win contract NSE BSE India",
    "ofs": "government OFS divestment stake sale India",
    "ipo": "IPO listing NSE BSE India",
    "sebi": "SEBI order ban regulatory India",
    "cpi": "India CPI WPI inflation",
    "monsoon": "India monsoon El Nino rainfall",
    "calendar": "India economic data releases today",
    "visa": "US visa H1B India IT outsourcing restriction",
    "ai": "AI disruption India IT outsourcing jobs",
}

NEGATIVE_WORDS = (
    "crash", "plunge", "tumble", "fear", "risk", "tension", "war",
    "tariff", "sanction", "selloff", "crisis", "ban", "restriction",
    "inflation", "recession", "slowdown", "deficit", "crude spike",
    "oil surge", "rate hike", "hawkish", "volatility", "uncertainty",
)
POSITIVE_WORDS = (
    "rally", "surge", "gain", "bull", "growth", "recovery", "deal",
    "peace", "ceasefire", "rate cut", "dovish", "stimulus", "boost",
    "strong", "record", "order win", "upgrade",
)

VERBOSE = False
SSL_INSECURE = False


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_ist():
    return datetime.now(IST)


def stamp():
    return now_ist().strftime("%Y-%m-%d %H:%M IST")


def is_market_open():
    """NSE cash market: weekdays 09:15-15:30 IST."""
    n = now_ist()
    if n.weekday() >= 5:
        return False
    hours = n.hour + n.minute / 60.0
    return 9.25 <= hours < 15.5


def pct_str(current, previous, digits=2):
    if not previous:
        return "N/A"
    p = (current - previous) / abs(previous) * 100.0
    return "%s%.*f%%" % ("+" if p >= 0 else "-", digits, abs(p))


def direction_of(current, previous, bullish_up=True, threshold=0.001):
    if not previous:
        return "neutral"
    pct = (current - previous) / abs(previous)
    if abs(pct) < threshold:
        return "neutral"
    if bullish_up:
        return "bullish" if pct > 0 else "bearish"
    return "bearish" if pct > 0 else "bullish"


def factor(label, value="", change="", direction="neutral", status="ok",
           details="", source=""):
    return {
        "label": label,
        "value": str(value),
        "change": str(change or ""),
        "direction": direction,
        "status": status,
        "details": details,
        "source": source,
        "updated": stamp(),
        "stale": False,
        # Every factor carries two nodes. Both start at 100 (the schema
        # default); the hourly tracker lowers/raises them as it learns.
        "power": 100.0,
        "effect": 100.0,
    }



def failed(label, details="Data unavailable", source=""):
    return factor(label=label, value="N/A", status="warning",
                  details=details, source=source)


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

_thread_local = threading.local()


def _session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": UA,
            "Connection": "keep-alive",
        })
        _thread_local.session = s
    return s


def http_get(url, timeout=15, headers=None, params=None):
    global SSL_INSECURE
    try:
        return _session().get(url, timeout=timeout, headers=headers, params=params)
    except requests.exceptions.SSLError:
        if SSL_INSECURE:
            return None
        warnings.filterwarnings("ignore")
        try:
            SSL_INSECURE = True
            return _session().get(url, timeout=timeout, headers=headers,
                                  params=params, verify=False)
        except Exception:
            return None
    except Exception as exc:
        if VERBOSE:
            print("GET failed %s: %s" % (url, exc))
        return None


def http_json(url, timeout=15, headers=None, params=None):
    r = http_get(url, timeout=timeout, headers=headers, params=params)
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Yahoo Finance (chart API - no pandas / yfinance needed)
# --------------------------------------------------------------------------

def yahoo_quote(symbol, timeout=12):
    """Return {price, prev, pct, ts} for a Yahoo symbol, or None."""
    url = YAHOO_CHART.format(sym=urllib.parse.quote(symbol, safe=""))
    headers = {"Accept": "application/json", "User-Agent": UA}
    r = http_get(url, timeout=timeout, headers=headers)
    if r is None or r.status_code != 200:
        r = http_get(url.replace("query1", "query2"), timeout=timeout,
                     headers=headers)
    if r is None or r.status_code != 200:
        return None
    try:
        payload = r.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta") or {}
        closes = []
        quote_block = (result.get("indicators", {}).get("quote") or [{}])[0]
        for c in quote_block.get("close") or []:
            if c is not None:
                try:
                    closes.append(float(c))
                except (TypeError, ValueError):
                    pass
        price = meta.get("regularMarketPrice")
        if price is None and closes:
            price = closes[-1]
        if price is None:
            return None
        price = float(price)
        prev = meta.get("previousClose")
        if prev is None:
            prev = meta.get("chartPreviousClose")
        if prev is None and len(closes) >= 2:
            prev = closes[-2]
        if prev is None:
            prev = price
        return {"price": price, "prev": float(prev),
                "pct": pct_str(price, float(prev)),
                "ts": meta.get("regularMarketTime") or 0}
    except Exception:
        return None


def quote_direction(quotes, symbol, bullish_up=True, threshold=0.0005):
    q = quotes.get(symbol)
    if not q:
        return "neutral"
    return direction_of(q["price"], q["prev"], bullish_up=bullish_up,
                        threshold=threshold)


# --------------------------------------------------------------------------
# NSE session + endpoints
# --------------------------------------------------------------------------

class NseClient:
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": UA,
            "Referer": NSE_HOME,
            "X-Requested-With": "XMLHttpRequest",
        })
        self.lock = threading.RLock()
        self.warm = False
        self.last = 0.0

    def _throttle(self):
        gap = time.time() - self.last
        if gap < 1.0:
            time.sleep(1.0 - gap)
        self.last = time.time()

    def _warm(self, force=False):
        if self.warm and not force:
            return
        try:
            self._throttle()
            self.session.get(NSE_HOME, timeout=15)
        except Exception:
            pass
        self.warm = True

    def api(self, path, referer=None):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if attempt:
                        self.warm = False
                    self._warm(force=bool(attempt))
                    self._throttle()
                    headers = {"Referer": referer} if referer else None
                    r = self.session.get(NSE_BASE + path, timeout=15,
                                         headers=headers)
                    if r.status_code in (401, 403):
                        self.warm = False
                        continue
                    if r.status_code == 200:
                        return r.json()
                except Exception as exc:
                    self.warm = False
                    if VERBOSE:
                        print("NSE %s failed: %s" % (path, exc))
            return None


def fetch_indices():
    return NseClient.get().api(NSE_ALL_INDICES)


def fetch_fii():
    return NseClient.get().api(NSE_FII_DII)


def fetch_gift():
    return NseClient.get().api(NSE_GIFT)


def fetch_block_deals():
    return NseClient.get().api(NSE_BLOCK_DEALS)


def fetch_chain_bundle():
    """Return (raw_chain, metrics) for NIFTY's nearest expiry."""
    nse = NseClient.get()
    info = nse.api(NSE_OC_CONTRACT, referer=NSE_OC_REFERER)
    if not info:
        return None, None
    expiries = info.get("expiryDates") or []
    if not expiries:
        return None, None
    expiry = urllib.parse.quote(str(expiries[0]))
    chain = nse.api(NSE_OC_V3.format(expiry=expiry), referer=NSE_OC_REFERER)
    if not chain:
        return None, None
    return chain, chain_metrics(chain)


def find_index(indices, name):
    if not indices:
        return None
    for row in indices.get("data") or []:
        if row.get("index") == name:
            return row
    return None


def nifty_pct(indices):
    row = find_index(indices, "NIFTY 50")
    if not row:
        return None
    try:
        return float(row.get("percentChange") or 0)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# NIFTY option-chain statistics (port of utils/nse_scraper.py)
# --------------------------------------------------------------------------

def chain_metrics(chain, price_change_pct=None):
    if not chain:
        return None
    try:
        records = chain.get("records") or {}
        data = records.get("data") or []
        if not data:
            return None

        spot = records.get("underlyingValue")
        spot = float(spot) if spot is not None else None

        total_call_oi = total_put_oi = 0
        total_call_chng = total_put_chng = 0
        iv_weighted = iv_weight = 0.0

        ce_by_strike = {}
        pe_by_strike = {}
        iv_by_strike = {}

        max_call_oi, max_call_strike, max_call_chng = -1, None, 0
        max_put_oi, max_put_strike, max_put_chng = -1, None, 0

        for entry in data:
            strike = entry.get("strikePrice")
            ce = entry.get("CE") or {}
            pe = entry.get("PE") or {}

            ce_oi = int(ce.get("openInterest") or 0)
            pe_oi = int(pe.get("openInterest") or 0)
            ce_chng = int(ce.get("changeinOpenInterest") or 0)
            pe_chng = int(pe.get("changeinOpenInterest") or 0)

            total_call_oi += ce_oi
            total_put_oi += pe_oi
            total_call_chng += ce_chng
            total_put_chng += pe_chng

            if strike is not None:
                s = float(strike)
                ce_by_strike[s] = ce_oi
                pe_by_strike[s] = pe_oi
                iv_by_strike[s] = (ce_oi, ce.get("impliedVolatility"),
                                   pe_oi, pe.get("impliedVolatility"))
                if ce_oi > max_call_oi:
                    max_call_oi, max_call_strike, max_call_chng = ce_oi, s, ce_chng
                if pe_oi > max_put_oi:
                    max_put_oi, max_put_strike, max_put_chng = pe_oi, s, pe_chng

            for side_oi, side in ((ce_oi, ce), (pe_oi, pe)):
                iv = side.get("impliedVolatility")
                if iv is None or side_oi <= 0:
                    continue
                try:
                    iv = float(iv)
                except (TypeError, ValueError):
                    continue
                iv_weighted += iv * side_oi
                iv_weight += side_oi

        total_oi = total_call_oi + total_put_oi
        chng_oi = total_call_chng + total_put_chng
        pcr = (total_put_oi / total_call_oi) if total_call_oi else None
        avg_iv = (iv_weighted / iv_weight) if iv_weight else None

        atm_pcr = None
        atm_iv = None
        strikes = sorted(ce_by_strike.keys())
        if spot is not None and strikes:
            i = bisect.bisect_left(strikes, spot)
            nearest = min(max(i, 0), len(strikes) - 1)
            lo = max(0, nearest - 4)
            hi = min(len(strikes) - 1, nearest + 4)
            window = strikes[lo:hi + 1]
            win_call = sum(ce_by_strike.get(s, 0) for s in window)
            win_put = sum(pe_by_strike.get(s, 0) for s in window)
            if win_call:
                atm_pcr = win_put / win_call
            w_sum = w_w = 0.0
            for s in window:
                co, civ, po, piv = iv_by_strike.get(s, (0, None, 0, None))
                for oi, iv in ((co, civ), (po, piv)):
                    if iv is None or oi <= 0:
                        continue
                    try:
                        iv = float(iv)
                    except (TypeError, ValueError):
                        continue
                    w_sum += iv * oi
                    w_w += oi
            atm_iv = (w_sum / w_w) if w_w else None

        return {
            "underlying": spot,
            "expiry": (records.get("expiryDates") or [None])[0],
            "total_oi": total_oi,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "chng_oi": chng_oi,
            "total_call_chng": total_call_chng,
            "total_put_chng": total_put_chng,
            "highest_call_oi": ((max_call_strike, max_call_oi)
                                if max_call_strike is not None else None),
            "highest_call_chng": max_call_chng,
            "highest_put_oi": ((max_put_strike, max_put_oi)
                               if max_put_strike is not None else None),
            "highest_put_chng": max_put_chng,
            "pcr": pcr,
            "atm_pcr": atm_pcr,
            "atm_iv": atm_iv,
            "avg_iv": avg_iv,
            "max_pain": compute_max_pain(chain),
            "nifty_pct": price_change_pct,
        }
    except Exception:
        return None


def compute_max_pain(chain):
    try:
        data = ((chain or {}).get("records") or {}).get("data") or []
        rows = []
        for entry in data:
            strike = entry.get("strikePrice")
            if strike is None:
                continue
            pe = int((entry.get("PE") or {}).get("openInterest") or 0)
            ce = int((entry.get("CE") or {}).get("openInterest") or 0)
            rows.append((float(strike), pe, ce))
        if not rows:
            return None
        best_strike = None
        best_pain = None
        for k, _, _ in rows:
            pain = 0.0
            for s, pe_oi, ce_oi in rows:
                if s < k:
                    pain += (k - s) * pe_oi
                elif s > k:
                    pain += (s - k) * ce_oi
            if best_pain is None or pain < best_pain:
                best_pain, best_strike = pain, k
        return best_strike
    except Exception:
        return None


# --------------------------------------------------------------------------
# Google News RSS, Moneycontrol, TradingEconomics, RBI
# --------------------------------------------------------------------------

def relative_age(pub_date):
    if not pub_date:
        return "?"
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 3600:
            return "%dm ago" % int(secs // 60)
        if secs < 86400:
            return "%dh ago" % int(secs // 3600)
        return "%dd ago" % int(secs // 86400)
    except Exception:
        return "?"


def fetch_news(query, limit=3, timeout=12):
    url = GOOGLE_NEWS.format(q=urllib.parse.quote(query))
    r = http_get(url, timeout=timeout)
    if r is None or r.status_code != 200:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    out = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        out.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "published": pub,
            "published_ago": relative_age(pub),
            "source": (source_el.text or "").strip() if source_el is not None else "",
        })
    return out


def news_tone(headlines):
    neg = pos = 0
    for h in headlines:
        title = (h.get("title") or "").lower()
        for kw in NEGATIVE_WORDS:
            if kw in title:
                neg += 1
        for kw in POSITIVE_WORDS:
            if kw in title:
                pos += 1
    total = neg + pos
    if total == 0:
        return "neutral"
    ratio = pos / total
    if ratio > 0.6:
        return "bullish"
    if ratio < 0.4:
        return "bearish"
    return "neutral"


def fetch_news_batch():
    """Every RSS query used by the agents, fetched concurrently."""
    result = {}

    def job(key):
        q = NEWS_QUERIES[key]
        if key in ("oil_tensions", "trade_war", "sanctions",
                   "us_india_tariff", "peace_talks"):
            items = fetch_news(q + " when:1d")
            if not items:
                items = fetch_news(q)
            return items
        return fetch_news(q)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(job, k): k for k in NEWS_QUERIES}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                result[key] = fut.result()
            except Exception:
                result[key] = []
    return result


def fetch_results_today(max_results=8):
    """Moneycontrol earnings-results calendar for today (IST)."""
    today = now_ist().strftime("%Y-%m-%d")
    url = MONEYCONTROL_CAL.format(date=today)
    r = http_get(url, timeout=15)
    if r is None or r.status_code != 200:
        return []
    html = r.text
    marker = "resultCalendarData"
    i = html.find(marker)
    if i < 0:
        return []
    j = html.find("{", i)
    if j < 0:
        return []
    depth = 0
    payload = None
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                payload = html[j:k + 1]
                break
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except Exception:
        return []
    entries = ((data.get("tableData") or {}).get("list") or [])
    out = []
    now = now_ist()
    for entry in entries:
        date_str = entry.get("date", "")
        try:
            d = datetime.strptime("%s %d" % (date_str, now_ist().year), "%d %b %Y")
        except (ValueError, TypeError):
            continue
        if d.month != now.month or d.day != now.day:
            continue
        out.append({
            "name": entry.get("stockName", ""),
            "result_type": entry.get("resultType", ""),
            "time": entry.get("time", ""),
            "ltp": entry.get("ltp", ""),
            "change": entry.get("change", ""),
        })
        if len(out) >= max_results:
            break
    return out


def te_summary(path, timeout=15):
    """Latest-release paragraph from a TradingEconomics indicator page."""
    url = "https://tradingeconomics.com/" + path.lstrip("/")
    r = http_get(url, timeout=timeout)
    if r is None or r.status_code != 200:
        return None
    text = r.text
    m = re.search(r"<span class=['\"]source-present['\"]>(.{0,200}?)</span>",
                  text, re.S)
    if not m:
        return None
    i = text.rfind("<h2", 0, m.start())
    if i < 0:
        i = text.rfind("<p", 0, m.start())
    if i < 0:
        return None
    segment = re.sub(r"<[^>]+>", " ", text[i:m.end()])
    segment = re.sub(r"\s+", " ", segment).strip()
    return segment[:500] or None


def te_repo_policy(summary):
    if not summary:
        return None
    rate = None
    m = re.search(r"repo rate(?: was|,)?\s*(?:held|left|kept)?\s*"
                  r"(?:unchanged)?\s*(?:at|by)?\s*([\d.]+)%", summary, re.I)
    if not m:
        m = re.search(r"([\d.]+)%\s*repo rate", summary, re.I)
    if m:
        try:
            rate = float(m.group(1))
        except ValueError:
            rate = None
    head = summary[:260].lower()
    stance = "neutral"
    if re.search(r"\bhiked\b|\braised\b", head) or "hawkish" in head:
        stance = "hawkish"
    elif re.search(r"\bcut\b|\breduced\b", head) or "dovish" in head:
        stance = "dovish"
    direction = ("bearish" if stance == "hawkish"
                 else "bullish" if stance == "dovish" else "neutral")
    return {"rate": rate, "stance": stance, "direction": direction,
            "summary": summary}


def te_forex_reserves(summary):
    if not summary:
        return None
    m = re.search(r"(?:increased|decreased|fell|rose|declined|grew)\s+to\s+"
                  r"([\d,]+)\s+USD Million(?:[^.]*?)\s+from\s+([\d,]+)\s+USD Million",
                  summary, re.I)
    value = prev = None
    if m:
        try:
            value = int(m.group(1).replace(",", ""))
            prev = int(m.group(2).replace(",", ""))
        except ValueError:
            value = prev = None
    direction = "neutral"
    if value is not None and prev is not None:
        direction = "bullish" if value >= prev else "bearish"
    return {"value": value, "previous": prev, "direction": direction,
            "summary": summary}


def te_inflation(summary):
    if not summary:
        return None
    m = re.search(r"(?:surged|rose|increased|fell|eased|declined|slowed|"
                  r"accelerated)\s+to\s+([\d.]+)%.{0,120}?\s+from\s+([\d.]+)%",
                  summary, re.I)
    current = prev = None
    if m:
        try:
            current, prev = float(m.group(1)), float(m.group(2))
        except ValueError:
            current = prev = None
    direction = "neutral"
    if current is not None and prev is not None:
        direction = "bearish" if current > prev else "bullish"
    return {"rate": current, "previous": prev, "direction": direction,
            "summary": summary}


def te_industrial_production(summary):
    if not summary:
        return None
    m = re.search(r"(?:rose|increased|fell|declined|expanded|grew|contracted)"
                  r"\s+(?:by\s+)?([\d.]+)%", summary, re.I)
    value = None
    if m:
        try:
            value = float(m.group(1))
        except ValueError:
            value = None
    if value is None:
        m = re.search(r"([\d.]+)%", summary)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                value = None
    direction = "neutral" if value is None else ("bullish" if value >= 0 else "bearish")
    return {"value": value, "direction": direction, "summary": summary}


def parse_billion(summary):
    if not summary:
        return None, None
    m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion.{0,80}?\s+from\s+"
                  r"\$?([\d.]+)\s*(?:USD\s*)?Billion", summary, re.I)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            return None, None
    m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion", summary, re.I)
    if m:
        try:
            return float(m.group(1)), None
        except ValueError:
            return None, None
    return None, None


def te_trade_balance(exports_summary, imports_summary):
    if not exports_summary or not imports_summary:
        return None
    exports, exports_prev = parse_billion(exports_summary)
    imports, imports_prev = parse_billion(imports_summary)
    balance = None
    if exports is not None and imports is not None:
        balance = exports - imports
    direction = "neutral"
    if balance is not None:
        direction = "bearish" if balance < 0 else "bullish"
    return {
        "exports": exports, "imports": imports,
        "exports_prev": exports_prev, "imports_prev": imports_prev,
        "balance": balance, "direction": direction,
        "summary": ("Exports: %s | Imports: %s"
                    % (exports_summary[:160], imports_summary[:160])),
    }


def fetch_te_batch():
    """All TradingEconomics pages used by the macro agent, in parallel."""
    pages = ("india/interest-rate", "india/foreign-exchange-reserves",
             "india/inflation-cpi", "india/exports", "india/imports",
             "india/industrial-production")
    summaries = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(te_summary, p): p for p in pages}
        for fut in as_completed(futures):
            page = futures[fut]
            try:
                summaries[page] = fut.result()
            except Exception:
                summaries[page] = None
    return {
        "repo": te_repo_policy(summaries.get("india/interest-rate")),
        "forex": te_forex_reserves(summaries.get("india/foreign-exchange-reserves")),
        "cpi": te_inflation(summaries.get("india/inflation-cpi")),
        "iip": te_industrial_production(summaries.get("india/industrial-production")),
        "trade": te_trade_balance(summaries.get("india/exports"),
                                  summaries.get("india/imports")),
    }


def rbi_repo_fallback():
    r = http_get("https://www.rbi.org.in/", timeout=15)
    if r is None or r.status_code != 200:
        return None
    low = r.text.lower()
    if "repo rate" in low:
        return "Check https://www.rbi.org.in for latest repo rate"
    return "Unable to parse - visit https://www.rbi.org.in"


# --------------------------------------------------------------------------
# Category builders (mirror agents/*.py)
# --------------------------------------------------------------------------

def merged_factor(label, rows, names):
    """Merge several factors into one dashboard row (US indices, Asia)."""
    if not rows:
        return factor(label=label, value="N/A", status="error",
                      details="Agent returned no data")
    if len(rows) == 1 and not names:
        return rows[0]
    parts = []
    for name, row in zip(names, rows):
        if row.get("change"):
            parts.append("%s: %s (%s)" % (name, row["value"], row["change"]))
        else:
            parts.append("%s: %s" % (name, row["value"]))
    bull = sum(1 for r in rows if r["direction"] == "bullish")
    bear = sum(1 for r in rows if r["direction"] == "bearish")
    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    status = "ok" if all(r["status"] == "ok" for r in rows) else "warning"
    details = " | ".join(r["details"] for r in rows if r["details"])
    return factor(label=label, value="; ".join(parts), direction=direction,
                  status=status, details=details, source=rows[0].get("source", ""))


def cat_crude(quotes):
    out = {}
    for key, label, symbol in (("brent", "Brent Crude (BZ=F)", "BZ=F"),
                               ("wti", "WTI Crude (CL=F)", "CL=F")):
        q = quotes.get(symbol)
        if not q:
            out[key] = factor(label, "N/A", status="error",
                              details="No quote available", source="yahoo")
            continue
        out[key] = factor(
            label=label,
            value="$%.2f" % q["price"],
            change=pct_str(q["price"], q["prev"]),
            direction=direction_of(q["price"], q["prev"], bullish_up=False,
                                   threshold=0.0002),
            details="Prev close: $%.2f | Higher crude = bearish for India "
                    "(net importer)" % q["prev"],
            source="yahoo",
        )

    brent_dir = out["brent"]["direction"]
    wti_dir = out["wti"]["direction"]
    if brent_dir == wti_dir:
        label = ("Rising" if brent_dir == "bearish"
                 else "Falling" if brent_dir == "bullish" else "Flat")
    elif "neutral" in (brent_dir, wti_dir):
        other = wti_dir if brent_dir == "neutral" else brent_dir
        label = ("Rising" if other == "bearish"
                 else "Falling" if other == "bullish" else "Flat")
    else:
        label = "Mixed"
    out["direction"] = factor(
        label="Direction", value=label,
        direction=("bearish" if label == "Rising"
                   else "bullish" if label == "Falling" else "neutral"),
        details="Rising crude = higher inflation, costs, CAD pressure = bearish "
                "for India (net importer)",
        source="derived",
    )
    return out


def cat_currency(quotes):
    out = {}
    q = quotes.get("INR=X")
    if not q:
        out["usd_inr"] = factor("USD/INR", "N/A", status="error",
                                details="No quote available", source="yahoo")
        out["direction"] = factor("Direction", "Flat",
                                  details="Rupee direction unavailable",
                                  source="yahoo")
        return out
    pct = (q["price"] - q["prev"]) / q["prev"] if q["prev"] else 0.0
    if pct < -0.0002:
        move = "strengthening"
    elif pct > 0.0002:
        move = "weakening"
    else:
        move = "flat"
    dirn = ("bullish" if move == "strengthening"
            else "bearish" if move == "weakening" else "neutral")
    out["usd_inr"] = factor(
        label="USD/INR",
        value="%.4f" % q["price"],
        change=pct_str(q["price"], q["prev"]),
        direction=dirn,
        details="Prev: %.4f | Lower = stronger rupee" % q["prev"],
        source="yahoo",
    )
    out["direction"] = factor(
        label="Direction", value=move.capitalize(), direction=dirn,
        details="Rupee is %s vs USD | Steep fall triggers FII selling" % move,
        source="yahoo",
    )
    return out


def cat_bonds(quotes):
    out = {}
    for key, label, symbol, suffix in (
            ("tnx", "10-yr US Treasury Yield (^TNX)", "^TNX", "%"),
            ("dxy", "US Dollar Index (DXY)", "DX-Y.NYB", "")):
        q = quotes.get(symbol)
        if not q:
            out[key] = factor(label, "N/A", status="error",
                              details="No quote available", source="yahoo")
            continue
        out[key] = factor(
            label=label,
            value="%.2f%s" % (q["price"], suffix),
            change=pct_str(q["price"], q["prev"]),
            direction=direction_of(q["price"], q["prev"], bullish_up=False,
                                   threshold=0.0005),
            details="Prev close: %.2f%s | Rising = bearish for EM"
                    % (q["prev"], suffix),
            source="yahoo",
        )
    return out


def cat_global(quotes, gift, news):
    out = {}
    us = [("dow", "Dow Jones", "^DJI"), ("sp500", "S&P 500", "^GSPC"),
          ("nasdaq", "Nasdaq", "^IXIC")]
    asia = [("nikkei", "Nikkei 225", "^N225"),
            ("hang_seng", "Hang Seng", "^HSI"),
            ("shanghai", "Shanghai Composite", "000001.SS")]

    def row(label, symbol):
        q = quotes.get(symbol)
        if not q:
            return factor(label, "N/A", status="error", details="No data",
                          source="yahoo")
        return factor(
            label=label,
            value="%s" % ("{:,.2f}".format(q["price"])),
            change=pct_str(q["price"], q["prev"]),
            direction=direction_of(q["price"], q["prev"]),
            details="Prev close: %s" % "{:,.2f}".format(q["prev"]),
            source="yahoo",
        )

    us_rows = [row(lbl, sym) for _, lbl, sym in us]
    asia_rows = [row(lbl, sym) for _, lbl, sym in asia]

    out["us_indices"] = merged_factor("US Indices Overnight", us_rows,
                                      [x[1] for x in us])
    out["asia_indices"] = merged_factor("Asian Markets This Morning", asia_rows,
                                        [x[1] for x in asia])

    # Global risk mood: -1/0/+1 across six indices.
    dirs = [r["direction"] for r in us_rows + asia_rows]
    if not any(d in ("bullish", "bearish", "neutral") for d in dirs):
        out["recession_fears"] = factor("Global Risk Mood", "N/A",
                                         status="warning",
                                         details="Could not compute from index moves",
                                         source="derived")
    else:
        score = sum(1 if d == "bullish" else -1 if d == "bearish" else 0
                    for d in dirs)
        avg = score / len(dirs)
        if avg > 0.3:
            mood, dirn = "Risk-ON", "bullish"
        elif avg < -0.3:
            mood, dirn = "Risk-OFF", "bearish"
        else:
            mood, dirn = "Mixed", "neutral"
        green = sum(1 for d in dirs if d == "bullish")
        out["recession_fears"] = factor(
            label="Global Risk Mood", value=mood, direction=dirn,
            details="%d/6 major indices positive. Deep risk-off = bearish for "
                    "EM; relief = bullish." % green,
            source="derived",
        )

    out["sgx_nifty"] = build_gift(gift, quotes)

    fed_items = news.get("fed") or []
    if fed_items:
        joined = " ".join(h["title"] for h in fed_items).lower()
        recent = fed_items[0]["title"]
        if re.search(r"\bcut|lower|ease\b", joined):
            out["fed_decision"] = factor(
                "Fed Rate Decision", "Rate CUT leaning", direction="bullish",
                details="Recent: %s | Dovish = risk-on, bullish for EM"
                        % recent[:100], source="google_news_rss")
        elif re.search(r"\bhike|raise|tighten\b", joined):
            out["fed_decision"] = factor(
                "Fed Rate Decision", "Rate HIKE leaning", direction="bearish",
                details="Recent: %s | Hawkish Fed = pressure on EM"
                        % recent[:100], source="google_news_rss")
        else:
            out["fed_decision"] = factor(
                "Fed Rate Decision", "Hold / neutral news", status="warning",
                details="Recent: %s | Hawkish Fed = pressure on EM; dovish = "
                        "risk-on" % recent[:100], source="google_news_rss")
    else:
        out["fed_decision"] = factor(
            "Fed Rate Decision", "Check news", status="manual",
            details="Hawkish Fed = pressure on EM; dovish = risk-on bullish",
            source="manual")

    yen_items = news.get("yen") or []
    if yen_items:
        joined = " ".join(h["title"] for h in yen_items).lower()
        if "interven" in joined:
            out["currency_interventions"] = factor(
                "Currency Interventions", "Intervention flagged",
                status="warning", direction="neutral",
                details="Recent: %s | Intervention shifts FX flows, adds "
                        "volatility" % yen_items[0]["title"][:100],
                source="google_news_rss")
        else:
            out["currency_interventions"] = factor(
                "Currency Interventions", "No intervention news",
                details="Latest: %s" % yen_items[0]["title"][:100],
                source="google_news_rss")
    else:
        out["currency_interventions"] = factor(
            "Currency Interventions", "No news found",
            details="Watch for BOJ yen intervention, PBOC fixing shifts",
            source="google_news_rss")
    return out


def build_gift(gift, quotes):
    last = None
    try:
        data = (gift or {}).get("data") or {}
        g = data.get("giftNifty") or {}
        last = float(g.get("lastprice") or 0)
        if last > 0:
            pct = float(g.get("perchange") or 0)
            day_change = float(g.get("daychange") or 0)
            dirn = "bullish" if pct > 0.05 else "bearish" if pct < -0.05 else "neutral"
            details = ("NSE GIFT Nifty futures (exp %s). Day change: %s pts"
                       % (g.get("expirydate", "?"), "{:+,.2f}".format(day_change)))
            ts = g.get("timestmp", "")
            if ts:
                details += " | As of %s" % ts
            nifty = quotes.get("^NSEI")
            if nifty and nifty.get("price"):
                gap = (last - nifty["price"]) / nifty["price"] * 100
                details += " | Gap vs Nifty spot: %s%.2f%%" % (
                    "+" if gap >= 0 else "", gap)
            return factor("SGX / GIFT Nifty", "%s" % "{:,.2f}".format(last),
                          change="%s%.2f%%" % ("+" if pct >= 0 else "", pct),
                          direction=dirn, details=details, source="nse")

        index_list = data.get("indexList") or []
        if index_list:
            idx = index_list[0]
            current = float(idx.get("last") or 0)
            prev = float(idx.get("previousClose") or 0)
            pct = float(idx.get("percChange") or 0)
            if current > 0:
                dirn = ("bullish" if pct > 0.05
                        else "bearish" if pct < -0.05 else "neutral")
                return factor(
                    "SGX / GIFT Nifty", "{:,.2f} USD".format(current),
                    change="%s%.2f%%" % ("+" if pct >= 0 else "", pct),
                    direction=dirn,
                    details="NSE NIFTY50 USD (GIFT Nifty proxy). Prev: %s"
                            % "{:,.2f}".format(prev),
                    source="nse")
    except Exception:
        pass

    nifty = quotes.get("^NSEI")
    if nifty:
        return factor(
            "SGX / GIFT Nifty", "{:,.2f}".format(nifty["price"]),
            change=pct_str(nifty["price"], nifty["prev"]),
            direction=direction_of(nifty["price"], nifty["prev"]),
            details="Nifty 50 spot (proxy - NSE GIFT API down). Prev: %s"
                    % "{:,.2f}".format(nifty["prev"]),
            source="yahoo")
    return factor("SGX / GIFT Nifty", "N/A", status="warning",
                  details="Ticker unavailable - compute gap from Nifty futures",
                  source="yahoo")


def cat_flows(fii_data, indices, news):
    out = {}
    ofs_items = news.get("ofs") or []
    if not fii_data:
        fallback = [
            ("fii_net", "FII Net Buy/Sell"),
            ("dii_net", "DII Net Buy/Sell"),
            ("dii_ofs", "DII Selling for OFS"),
            ("fii_selloff", "FII Buying During Selloff"),
            ("fii_trend", "FII Consecutive Days Trend"),
            ("flows_manual", "FII/DII Data Interpretation"),
        ]
        for key, label in fallback:
            out[key] = factor(label, "N/A", status="warning",
                              details="NSE data unavailable (market closed or "
                                      "blocked) - check daily report",
                              source="nse")
        return out

    fii_net = dii_net = 0.0
    as_of = None
    try:
        for item in fii_data:
            cat = item.get("category", "")
            try:
                val = float(str(item.get("netValue", "0")).replace(",", ""))
            except (TypeError, ValueError):
                val = 0.0
            if as_of is None and item.get("date"):
                as_of = str(item.get("date"))
            if "FII" in cat.upper() or "FPI" in cat.upper():
                fii_net = val
            elif "DII" in cat.upper():
                dii_net = val
    except Exception as exc:
        out["fii_net"] = factor("FII Net Buy/Sell", "Parse Error",
                                status="error", details=str(exc), source="nse")
        return out

    fii_dir = "bullish" if fii_net > 0 else "bearish" if fii_net < 0 else "neutral"
    dii_dir = "bullish" if dii_net > 0 else "bearish" if dii_net < 0 else "neutral"
    date_note = (" (NSE provisional, %s)" % as_of) if as_of else ""

    out["fii_net"] = factor(
        "FII Net Buy/Sell", "\u20b9%s Cr" % "{:,.0f}".format(fii_net),
        direction=fii_dir,
        details=("Net buying = liquidity boost" if fii_net > 0
                 else "Net selling = bearish pressure") + date_note,
        source="nse")
    out["dii_net"] = factor(
        "DII Net Buy/Sell", "\u20b9%s Cr" % "{:,.0f}".format(dii_net),
        direction=dii_dir,
        details=("DII buying cushions FII selling" if dii_net > 0
                 else "DII also selling = double bearish") + date_note,
        source="nse")

    ofs_text = " | ".join(h["title"][:60] for h in ofs_items[:2])
    if dii_net < -1000 and ofs_items:
        out["dii_ofs"] = factor(
            "DII Selling for OFS", "Suspected (DII selling + OFS news)",
            direction="bearish",
            details="DII net selling \u20b9%s Cr with OFS/divestment news: %s"
                    % ("{:,.0f}".format(abs(dii_net)), ofs_text),
            source="nse+news")
    elif ofs_items:
        out["dii_ofs"] = factor(
            "DII Selling for OFS", "OFS news present", status="warning",
            details="OFS/divestment news: %s. Primary mkt absorbs cash = "
                    "secondary liquidity drain = bearish." % ofs_text,
            source="news")
    else:
        out["dii_ofs"] = factor(
            "DII Selling for OFS", "No OFS news detected",
            details="No govt OFS/divestment headlines. Primary mkt absorbs "
                    "cash = bearish when active.",
            source="news")

    change = nifty_pct(indices)
    change = 0.0 if change is None else change
    if fii_net > 500 and change < -0.5:
        out["fii_selloff"] = factor(
            "FII Buying During Selloff", "YES - accumulation signal",
            direction="bullish",
            details="FII net buy \u20b9%s Cr while Nifty %+.2f%% - FII "
                    "accumulating at dips = long-term confidence"
                    % ("{:,.0f}".format(fii_net), change),
            source="nse")
    elif fii_net > 500:
        out["fii_selloff"] = factor(
            "FII Buying During Selloff",
            "FII buying \u20b9%s Cr" % "{:,.0f}".format(fii_net),
            direction="bullish",
            details="Nifty %+.2f%% - buying not during a selloff, but positive"
                    % change,
            source="nse")
    else:
        out["fii_selloff"] = factor(
            "FII Buying During Selloff",
            "FII not net buying (%s Cr)" % "{:,.0f}".format(fii_net),
            direction="neutral", status="warning",
            details="Accumulation signal only when FII buy >500 Cr while Nifty "
                    "falls >0.5%",
            source="nse")

    # Consecutive-day trend (one sample per IST day, persisted).
    history = _read_fii_history()
    today_key = now_ist().strftime("%Y-%m-%d")
    history = [h for h in history
               if _day_key(h.get("ts")) != today_key]
    history.append({"ts": time.time(), "fii_net": fii_net})
    history = _dedupe_by_day(history)[-30:]
    _write_fii_history(history)

    if len(history) < 2:
        summary, dirn = "Insufficient data", "neutral"
        detail = "Need 2+ days of history"
    else:
        values = [h["fii_net"] for h in history]
        last_sign = 1 if values[-1] > 0 else -1 if values[-1] < 0 else 0
        streak = 1
        prev_ts = history[-1]["ts"]
        for h in reversed(history[:-1]):
            sign = 1 if h["fii_net"] > 0 else -1 if h["fii_net"] < 0 else 0
            # A multi-week gap in samples is not a "consecutive" trend.
            if sign == last_sign and sign != 0 and \
                    (prev_ts - h["ts"]) <= 7 * 86400:
                streak += 1
                prev_ts = h["ts"]
            else:
                break
        if last_sign > 0:
            summary, dirn = "Buying \u00d7 %d %s" % (streak, "day" if streak == 1 else "days"), "bullish"
        elif last_sign < 0:
            summary, dirn = "Selling \u00d7 %d %s" % (streak, "day" if streak == 1 else "days"), "bearish"
        else:
            summary, dirn = "Flat / no clear trend", "neutral"
        detail = ("Last %d trading days. Sustained trend > single-day noise."
                  % len(history))
    out["fii_trend"] = factor("FII Consecutive Days Trend", summary,
                              direction=dirn, details=detail, source="nse+cache")

    out["flows_manual"] = factor(
        "FII/DII Data Interpretation", "Needs cross-checking", status="manual",
        details="Net numbers alone can be misleading - check bulk/block deal "
                "breakdowns",
        source="manual")
    return out


def _day_key(ts):
    try:
        return datetime.fromtimestamp(float(ts), IST).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _dedupe_by_day(history):
    by_day = {}
    for h in history:
        day = _day_key(h.get("ts"))
        if day:
            by_day[day] = h
    return [by_day[d] for d in sorted(by_day)]


def _read_fii_history():
    """Read FII history, merging the desktop app's cache with this app's."""
    entries = []
    for path in (LEGACY_FII_HISTORY, FII_HISTORY_FILE):
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                entries.extend(data)
        except Exception:
            continue
    return entries


def _write_fii_history(history):
    try:
        with open(FII_HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(history, fh)
    except Exception:
        pass


def cat_geopolitics(news):
    topics = [
        ("oil_tensions", "Middle East / Oil Region Tensions", True),
        ("trade_war", "Trade War / Tariffs", False),
        ("sanctions", "Sanctions / Military Action", False),
        ("us_india_tariff", "US-India Tariff / Trade Action", False),
        ("peace_talks", "Peace-Talk Status (Ukraine/Russia)", False),
    ]
    out = {}
    for key, label, invert in topics:
        items = news.get(key) or []
        tone = news_tone(items)
        newest = _newest_age(items)
        if items:
            parts = []
            for h in items[:2]:
                src = h.get("source") or "news"
                parts.append("%s [%s, %s]" % (h["title"][:55],
                                              h["published_ago"], src[:18]))
            summary = "Newest: %s | %s" % (newest, " | ".join(parts))
        else:
            summary = "No recent headlines found"
        direction = tone
        if key == "oil_tensions":
            direction = ("bearish" if tone == "bearish"
                         else "bullish" if tone == "bullish" else "neutral")
        out[key] = factor(
            label, "%s (%s)" % (tone.capitalize(), newest),
            direction=direction, details=summary, source="google_news_rss")
    return out


def _newest_age(headlines):
    best, best_min = "?", 10 ** 9
    for h in headlines:
        m = re.match(r"(\d+)([mhd])", h.get("published_ago") or "")
        if not m:
            continue
        minutes = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]
        if minutes < best_min:
            best_min, best = minutes, h["published_ago"]
    return best


def cat_macro(te, news):
    out = {}
    te = te or {}

    repo = te.get("repo")
    if repo:
        rate = repo.get("rate")
        value = ("%s%%" % rate) if rate is not None else repo["stance"].capitalize()
        out["rbi_repo"] = factor(
            "RBI Policy / Repo Rate", value,
            direction=repo.get("direction", "neutral"),
            details="Stance: %s. %s" % (repo.get("stance", "neutral"),
                                        repo["summary"][:220]),
            source="tradingeconomics")
        low = (repo["summary"] or "").lower()
        if re.search(r"\bhawkish\b|\bhiked\b|\braised\b", low):
            stance = "hawkish"
        elif re.search(r"\bdovish\b|\bcut\b|\breduced\b", low):
            stance = "dovish"
        else:
            stance = "neutral"
        out["rbi_guidance"] = factor(
            "RBI Forward Guidance", stance.capitalize(),
            direction=("bearish" if stance == "hawkish"
                       else "bullish" if stance == "dovish" else "neutral"),
            details="Policy tone reads %s. Even with rate held, hawkish tone = "
                    "bearish; dovish = relief rally." % stance,
            source="tradingeconomics")
    else:
        fallback = rbi_repo_fallback()
        out["rbi_repo"] = factor(
            "RBI Policy / Repo Rate", fallback or "N/A", status="warning",
            details="Rate cut = bullish liquidity; hike/tight = bearish",
            source="rbi.org.in")
        out["rbi_guidance"] = factor(
            "RBI Forward Guidance", "Check RBI statement", status="manual",
            details="Hawkish tone = bearish; dovish = relief rally",
            source="manual")

    trade = te.get("trade")
    if trade and trade.get("summary"):
        bal = trade.get("balance")
        value = ("$%s B" % "{:,.1f}".format(bal)) if bal is not None else "N/A"
        ex, im = trade.get("exports"), trade.get("imports")
        ex_prev, im_prev = trade.get("exports_prev"), trade.get("imports_prev")
        if ex is not None and im is not None:
            details = "Exports $%sB / Imports $%sB. " % (
                "{:,.1f}".format(ex), "{:,.1f}".format(im))
            if ex_prev is not None and im_prev is not None:
                details += ("Prev month: exports $%sB, imports $%sB. Deficit %s. "
                            % ("{:,.1f}".format(ex_prev), "{:,.1f}".format(im_prev),
                               "widening" if bal < (ex_prev - im_prev) else "narrowing"))
            details += trade["summary"][:250]
        else:
            details = trade["summary"][:250]
        out["trade_balance"] = factor(
            "Trade Balance / CAD", value,
            direction=trade.get("direction", "neutral"), details=details,
            source="tradingeconomics")
    else:
        out["trade_balance"] = factor(
            "Trade Balance / CAD", "Check news", status="warning",
            details="Widening deficit = rupee pressure = bearish",
            source="manual")

    cpi = te.get("cpi")
    if cpi and cpi.get("rate") is not None:
        out["cpi_wpi"] = factor(
            "CPI / WPI Inflation", "%.2f%%" % cpi["rate"],
            change=("prev %.2f%%" % cpi["previous"]
                    if cpi.get("previous") is not None else ""),
            direction=cpi.get("direction", "neutral"),
            details="High inflation = fewer rate cuts = bearish. %s"
                    % cpi["summary"][:220],
            source="tradingeconomics")
    else:
        tone = news_tone(news.get("cpi") or [])
        out["cpi_wpi"] = factor(
            "CPI / WPI Inflation", tone.capitalize(),
            direction="bearish" if tone == "bearish" else "bullish",
            status="warning",
            details="High inflation = fewer rate cuts = bearish",
            source="google_news_rss")

    iip = te.get("iip")
    if iip and iip.get("value") is not None:
        out["iip_gst"] = factor(
            "IIP / Industrial Production", "%+.1f%% YoY" % iip["value"],
            direction=iip.get("direction", "neutral"),
            details=iip["summary"][:250], source="tradingeconomics")
    else:
        out["iip_gst"] = factor(
            "IIP / GST Collections", "Check latest release", status="warning",
            details="Strong growth = bullish economic outlook", source="manual")

    monsoon = news.get("monsoon") or []
    tone = news_tone(monsoon)
    joined = " ".join(h["title"] for h in monsoon).lower()
    direction = "neutral"
    if "deficit" in joined or "weak" in joined or "below normal" in joined:
        direction = "bearish"
    elif "surplus" in joined or "good" in joined or "above normal" in joined:
        direction = "bullish"
    out["monsoon"] = factor(
        "Monsoon / El Nino", tone.capitalize(), direction=direction,
        status="manual",
        details=(" | ".join(h["title"][:60] for h in monsoon[:2])
                 if monsoon else
                 "Weak monsoon = food inflation = rate pressure = bearish"),
        source="google_news_rss")

    fx = te.get("forex")
    if fx and fx.get("value") is not None:
        change = ""
        if fx.get("previous") is not None:
            delta = fx["value"] - fx["previous"]
            change = "%s%s M" % ("+" if delta >= 0 else "",
                                 "{:,.0f}".format(delta))
        out["forex_reserves"] = factor(
            "Forex Reserves", "$%s B" % "{:.0f}".format(fx["value"] / 1000),
            change=change, direction=fx.get("direction", "neutral"),
            details=fx["summary"][:220], source="tradingeconomics")
    else:
        out["forex_reserves"] = factor(
            "Forex Reserves", "Check RBI press releases", status="warning",
            details="Rising reserves = external stability = bullish; falling = bearish",
            source="rbi.org.in")

    cal = news.get("calendar") or []
    if cal:
        out["economic_calendar"] = factor(
            "Scheduled Economic Releases Today", "Check headlines",
            status="warning",
            details=" | ".join(h["title"][:60] for h in cal[:2]),
            source="google_news_rss")
    else:
        out["economic_calendar"] = factor(
            "Scheduled Economic Releases Today", "Check calendar",
            status="manual",
            details="Uncertainty before data = volatile; beat = bullish",
            source="manual")

    out["liquidity"] = factor(
        "Central Bank Liquidity / Forex Measures", "Check RBI announcements",
        status="manual",
        details="Tighter liquidity = bearish; rupee-support measures = positive",
        source="manual")
    return out


def cat_earnings(results_today, news):
    out = {}
    if results_today:
        names = ", ".join("%s (%s)" % (e["name"], e["result_type"])
                          for e in results_today[:8])
        out["major_results"] = factor(
            "Major Results Due Today", "%d result(s)" % len(results_today),
            details=names + " | Beats lift index; misses drag",
            source="moneycontrol")
    else:
        out["major_results"] = factor(
            "Major Results Due Today", "None listed", status="warning",
            details="No results scheduled today on Moneycontrol calendar - "
                    "check company announcements",
            source="moneycontrol")

    def news_row(key, label):
        items = news.get(key) or []
        tone = news_tone(items)
        return factor(label, tone.capitalize(), direction=tone,
                      details=(" | ".join(h["title"][:60] for h in items[:2])
                               if items else "No major headlines"),
                      source="google_news_rss")

    out["earnings_beats"] = news_row("earnings", "Earnings Beats / Misses")
    out["order_wins"] = news_row("orders", "Order Wins / Deals")

    out["govt_ofs"] = factor(
        "Government OFS / Divestment", "Check news", status="manual",
        details="Discounted OFS = supply pressure + liquidity drain = bearish",
        source="google_news_rss")
    out["ipo_listings"] = factor(
        "IPO Listings / New Issues", "Check news", status="manual",
        details="Big IPO absorbs market liquidity, adds volatility",
        source="google_news_rss")
    out["regulatory_orders"] = factor(
        "Regulatory / Compliance Orders", "Check news", status="manual",
        details="SEBI ban / governance risk hits stock + sector contagion",
        source="google_news_rss")
    return out


def cat_sectors(quotes, news):
    crude_dir = quote_direction(quotes, "BZ=F", bullish_up=False, threshold=0.0002)
    q = quotes.get("INR=X")
    if q and q["prev"]:
        move = (q["price"] - q["prev"]) / q["prev"]
        rupee_dir = ("bearish" if move > 0.0002
                     else "bullish" if move < -0.0002 else "neutral")
    else:
        rupee_dir = "neutral"
    nasdaq_dir = quote_direction(quotes, "^IXIC")

    def crude_reaction(label, positive, negative):
        if crude_dir == "bearish":
            return factor(label, "Bullish", direction="bullish",
                          details=positive, source="derived")
        if crude_dir == "bullish":
            return factor(label, "Bearish", direction="bearish",
                          details=negative, source="derived")
        return factor(label, "Neutral",
                      details="Crude flat vs prev close - no fuel-cost trigger. "
                              + positive,
                      source="derived")

    out = {}
    out["aviation"] = crude_reaction(
        "Aviation (crude-linked)",
        "Fuel is biggest cost; low crude = bullish, high = bearish",
        "Fuel is biggest cost; high crude = bearish")
    out["banks"] = factor(
        "Banks / Financials", "Depends on FII flows", status="manual",
        details="Gains on inflows; NIMs react to rates. Cross-check FII flows.",
        source="derived")

    it_score = 0
    if rupee_dir == "bearish":
        it_score += 1
    elif rupee_dir == "bullish":
        it_score -= 1
    if nasdaq_dir == "bullish":
        it_score += 1
    elif nasdaq_dir == "bearish":
        it_score -= 1
    it_value, it_dir = (("Bullish", "bullish") if it_score > 0
                        else ("Bearish", "bearish") if it_score < 0
                        else ("Neutral", "neutral"))
    out["it"] = factor(
        "IT (global tech, rupee)", it_value, direction=it_dir,
        details="Weak rupee (USD/INR up) boosts IT earnings; Nasdaq direction "
                "sets global tech tone. Signals: rupee %s, Nasdaq %s"
                % ("weak" if rupee_dir == "bearish"
                   else "strong" if rupee_dir == "bullish" else "flat",
                   nasdaq_dir),
        source="derived")

    out["us_visa"] = _news_flag(
        "US Visa / IT Outsourcing",
        news.get("visa") or [],
        ("restrict", "ban", "tighten", "cap"),
        "Restrictions flagged",
        "No restriction news",
        "Restrictions hurt Indian IT = sector drag")
    out["ai_disruption"] = _news_flag(
        "AI Disrupting IT Outsourcing",
        news.get("ai") or [],
        ("disrupt", "replace", "cut", "layoff"),
        "Disruption headlines",
        "No disruption headlines",
        "Structural fear hits IT sector sentiment")
    out["auto"] = crude_reaction(
        "Auto (GST, oil, demand)",
        "Low oil + GST cuts = demand boost = bullish",
        "High oil = higher fuel prices, demand hit = bearish")
    out["omcs"] = crude_reaction(
        "OMCs / Paints / Tyres",
        "Low crude = better margins = bullish",
        "High crude = margin compression = bearish")
    return out


def _news_flag(label, items, keywords, hit_value, miss_value, hit_detail):
    if not items:
        return factor(label, "No news found", status="manual",
                      details=hit_detail, source="manual")
    joined = " ".join(h["title"] for h in items).lower()
    recent = items[0]["title"]
    if any(k in joined for k in keywords):
        return factor(label, hit_value, status="warning", direction="bearish",
                      details="Recent: %s | %s" % (recent[:100], hit_detail),
                      source="google_news_rss")
    return factor(label, miss_value, details="Latest: %s" % recent[:100],
                  source="google_news_rss")


def cat_sentiment(indices, fii_data, metrics, deals, quotes):
    out = {}
    vix = find_index(indices, "INDIA VIX")
    if vix:
        try:
            val = float(vix.get("last") or 0)
            pct = float(vix.get("percentChange") or 0)
            dirn = "bearish" if val > 20 else "bullish" if val < 12 else "neutral"
            out["vix"] = factor(
                "India VIX", "%.2f" % val,
                change="%s%.2f%%" % ("+" if pct >= 0 else "", pct),
                direction=dirn,
                details="High VIX (>20) = fear/weakness; low (<12) = complacency",
                source="nse")
        except (TypeError, ValueError):
            vix = None
    if not vix:
        q = quotes.get("^INDIAVIX")
        if q:
            val = q["price"]
            dirn = "bearish" if val > 20 else "bullish" if val < 12 else "neutral"
            out["vix"] = factor(
                "India VIX", "%.2f" % val, change=pct_str(q["price"], q["prev"]),
                direction=dirn,
                details="High VIX (>20) = fear/weakness; low (<12) = complacency",
                source="yahoo")
        else:
            out["vix"] = factor("India VIX", "N/A", status="warning",
                                details="NSE data unavailable", source="nse")

    nifty = find_index(indices, "NIFTY 50")
    if nifty:
        try:
            val = float(nifty.get("last") or 0)
            high = float(nifty.get("high") or 0)
            low = float(nifty.get("low") or 0)
            prev = float(nifty.get("previousClose") or 0)
            out["nifty_levels"] = factor(
                "Nifty Support/Resistance", "%s" % "{:,.0f}".format(val),
                change=pct_str(val, prev) if prev else "",
                details="Prev close: %s | H: %s | L: %s | Breakout above high = "
                        "buy; breakdown below low = sell"
                        % ("{:,.0f}".format(prev), "{:,.0f}".format(high),
                           "{:,.0f}".format(low)),
                source="nse")
        except (TypeError, ValueError):
            nifty = None
    if not nifty:
        out["nifty_levels"] = factor("Nifty Support/Resistance", "N/A",
                                      status="warning",
                                      details="NSE data unavailable", source="nse")

    if fii_data:
        out["fii_fno"] = factor(
            "FII/DII F&O Positioning", "Provisional data available",
            details="Heavy FII shorts = squeeze risk; longs = bullish. "
                    "Cross-check FII net figure in FOREIGN FLOWS section.",
            source="nse")
    else:
        out["fii_fno"] = factor(
            "FII/DII F&O Positioning", "N/A", status="warning",
            details="NSE data unavailable", source="nse")

    if metrics and metrics.get("pcr") is not None:
        pcr = metrics["pcr"]
        max_pain = metrics.get("max_pain")
        dirn = "bearish" if pcr < 0.8 else "bullish" if pcr > 1.2 else "neutral"
        details = "PCR: %.2f" % pcr
        if max_pain is not None:
            details += " | Max Pain: %s" % "{:,.0f}".format(max_pain)
        out["pcr"] = factor(
            "PCR / Max Pain", "PCR %.2f" % pcr, direction=dirn,
            details=details + " | Falling PCR = bearish; heavy call OI = ceiling",
            source="nse")
    else:
        out["pcr"] = factor(
            "PCR / Max Pain", "N/A", status="warning",
            details="Option chain unavailable (NSE API restricted) - check manually",
            source="nse")

    out["cas"] = factor(
        "SEBI CAS Distortions", "CAS rule active", status="manual",
        details="Closing Auction Session can cause artificial end-day spikes and "
                "next-day unwinding",
        source="manual")

    # Weekly expiry: last Thursday of the month (NIFTY monthly expiry).
    now = now_ist()
    last_day = 28
    d = datetime(now.year, now.month, last_day, tzinfo=IST)
    while d.month != now.month:
        last_day -= 1
        d = datetime(now.year, now.month, last_day, tzinfo=IST)
    while d.weekday() != 3:
        last_day -= 1
        d = datetime(now.year, now.month, last_day, tzinfo=IST)
    days_left = (d - now).days
    out["expiry"] = factor(
        "Expiry Day",
        "TODAY" if d.date() == now.date()
        else "%s (%dd)" % (d.strftime("%d %b %Y"), days_left),
        status="warning" if days_left <= 1 else "ok",
        details="Expiry can cause whipsaw, sharp swings, premium distortion",
        source="calendar")

    if deals and isinstance(deals.get("data"), list) and deals["data"]:
        rows = deals["data"]
        bits = []
        for item in rows[:3]:
            symbol = item.get("symbol", "")
            chg = item.get("pChange") or 0
            try:
                chg = float(chg)
            except (TypeError, ValueError):
                chg = 0.0
            bits.append("%s (%s%.1f%%)" % (symbol, "+" if chg >= 0 else "", chg))
        details = "Large off-market trades move prices sharply. Top: " + ", ".join(bits)
        if deals.get("timestamp"):
            details += " | As of %s" % deals["timestamp"]
        out["block_deals"] = factor(
            "Block / Bulk Deals", "%d deal(s)" % len(rows), details=details,
            source="nse")
    elif deals is not None:
        out["block_deals"] = factor(
            "Block / Bulk Deals", "None reported",
            details="No block deals recorded today", source="nse")
    else:
        out["block_deals"] = factor(
            "Block / Bulk Deals", "N/A", status="warning",
            details="Block deals data unavailable (NSE API restricted) - check "
                    "pre-market reports",
            source="nse")

    advances = declines = 0
    if indices:
        try:
            advances = int(indices.get("advances") or 0)
            declines = int(indices.get("declines") or 0)
        except (TypeError, ValueError):
            advances = declines = 0
    total = advances + declines
    if total > 0:
        ratio = advances / total
        dirn = "bullish" if ratio > 0.6 else "bearish" if ratio < 0.4 else "neutral"
        out["market_breadth"] = factor(
            "Market Breadth", "A:%d D:%d" % (advances, declines),
            direction=dirn,
            details="Advance ratio: %.1f%% | Broad positive breadth = strong open"
                    % (ratio * 100),
            source="nse")
    else:
        out["market_breadth"] = factor(
            "Market Breadth", "N/A", status="warning",
            details="NSE data unavailable", source="nse")
    return out


def pcr_band(pcr):
    if pcr is None:
        return "neutral", "n/a"
    if pcr > 1.30:
        return "bullish", "extreme bullish (heavy put writing; contrarian top risk)"
    if pcr >= 0.90:
        return "bullish", "bullish / neutral drift"
    if pcr >= 0.70:
        return "neutral", "neutral / cautious (equity baseline 0.8-0.9)"
    if pcr >= 0.50:
        return "bearish", "moderately bearish (call writing dominates)"
    return "bearish", "extreme bearish (put liquidation; contrarian bounce risk)"


def quadrant(m):
    price_up = m.get("nifty_pct") is not None and m["nifty_pct"] >= 0
    oi_up = m.get("chng_oi", 0) >= 0
    if price_up and oi_up:
        return "bullish", "long buildup (price \u2191, OI \u2191)"
    if not price_up and oi_up:
        return "bearish", "short buildup (price \u2193, OI \u2191)"
    if not price_up and not oi_up:
        return "bearish", "long unwinding (price \u2193, OI \u2193)"
    return "bullish", "short covering (price \u2191, OI \u2193)"


def vote(direction):
    return 1 if direction == "bullish" else -1 if direction == "bearish" else 0


def cat_option_chain(metrics):
    if not metrics:
        labels = [
            ("oi", "Total Open Interest (OI)"),
            ("chng_oi", "Change in Open Interest (Chng in OI)"),
            ("highest_call", "Highest Call Open Interest"),
            ("highest_put", "Highest Put Open Interest"),
            ("pcr", "Put-Call Ratio (PCR)"),
            ("atm_pcr", "Near-the-Money PCR (ATM \u00b14 strikes)"),
            ("iv", "Implied Volatility (IV)"),
            ("bias", "Option-Chain Bias"),
        ]
        return {k: factor(lbl, "N/A", status="warning",
                          details="Option chain data unavailable (NSE API "
                                  "restricted) - check manually",
                          source="nse")
                for k, lbl in labels}

    m = metrics
    out = {}

    out["oi"] = factor(
        "Total Open Interest (OI)", "%s" % "{:,}".format(m.get("total_oi", 0)),
        details="Total NIFTY OI: %s calls + %s puts. High OI = strong "
                "institutional interest; OI at OTM puts = support floor, at OTM "
                "calls = overhead supply."
                % ("{:,}".format(m.get("total_call_oi", 0)),
                   "{:,}".format(m.get("total_put_oi", 0))),
        source="nse")

    chng = m.get("chng_oi", 0)
    sign = "+" if chng >= 0 else ""
    if m.get("nifty_pct") is not None and chng:
        dirn, label = quadrant(m)
    else:
        net = m.get("total_put_chng", 0) - m.get("total_call_chng", 0)
        thr = 0.05 * (abs(m.get("total_call_chng", 0))
                      + abs(m.get("total_put_chng", 0))) or 1000
        if net > thr:
            dirn, label = "bullish", "puts added > calls (support building)"
        elif net < -thr:
            dirn, label = "bearish", "calls added > puts (resistance building)"
        else:
            dirn, label = "neutral", "balanced call/put OI change"
    out["chng_oi"] = factor(
        "Change in Open Interest (Chng in OI)", "%s%s" % (sign, "{:,}".format(chng)),
        direction=dirn,
        details="Net \u0394OI %s | %s. Price \u2191 with \u0394OI \u2191 = long "
                "buildup; price \u2193 with \u0394OI \u2191 = short buildup."
                % ("{:+,}".format(chng), label),
        source="nse")

    call = m.get("highest_call_oi")
    if not call:
        out["highest_call"] = factor(
            "Highest Call Open Interest", "N/A", status="warning",
            details="Option chain data unavailable - check manually", source="nse")
    else:
        strike, oi = call
        chg = m.get("highest_call_chng", 0)
        if chg > 0:
            dirn, wall = "bearish", "resistance building (fresh call writing)"
        elif chg < 0:
            dirn, wall = "bullish", "resistance unwinding (call sellers covering)"
        else:
            dirn, wall = "neutral", "resistance steady"
        out["highest_call"] = factor(
            "Highest Call Open Interest",
            "%s (OI %s)" % ("{:,.0f}".format(strike), "{:,}".format(oi)),
            direction=dirn,
            details="Max call writers at %s = major resistance. %s (%s). "
                    "Break above with \u0394OI<0 = bullish."
                    % ("{:,.0f}".format(strike), wall, "{:+,}".format(chg)),
            source="nse")

    put = m.get("highest_put_oi")
    if not put:
        out["highest_put"] = factor(
            "Highest Put Open Interest", "N/A", status="warning",
            details="Option chain data unavailable - check manually", source="nse")
    else:
        strike, oi = put
        chg = m.get("highest_put_chng", 0)
        if chg > 0:
            dirn, wall = "bullish", "support building (fresh put writing)"
        elif chg < 0:
            dirn, wall = "bearish", "support unwinding (put sellers capitulating)"
        else:
            dirn, wall = "neutral", "support steady"
        out["highest_put"] = factor(
            "Highest Put Open Interest",
            "%s (OI %s)" % ("{:,.0f}".format(strike), "{:,}".format(oi)),
            direction=dirn,
            details="Max put writers at %s = major support. %s (%s). "
                    "Break below with \u0394OI<0 = bearish."
                    % ("{:,.0f}".format(strike), wall, "{:+,}".format(chg)),
            source="nse")

    pcr = m.get("pcr")
    if pcr is None:
        out["pcr"] = factor("Put-Call Ratio (PCR)", "N/A", status="warning",
                            details="Option chain data unavailable", source="nse")
    else:
        dirn, band = pcr_band(pcr)
        out["pcr"] = factor(
            "Put-Call Ratio (PCR)", "%.2f" % pcr, direction=dirn,
            details="Total Put OI / Total Call OI = %.2f - %s. PCR>1.3 extreme "
                    "bull (contrarian top), <0.5 extreme bear (contrarian bounce)."
                    % (pcr, band),
            source="nse")

    atm_pcr = m.get("atm_pcr")
    if atm_pcr is None:
        out["atm_pcr"] = factor(
            "Near-the-Money PCR (ATM \u00b14 strikes)", "N/A", status="warning",
            details="Option chain data unavailable", source="nse")
    else:
        dirn, band = pcr_band(atm_pcr)
        spot = m.get("underlying")
        out["atm_pcr"] = factor(
            "Near-the-Money PCR (ATM \u00b14 strikes)", "%.2f" % atm_pcr,
            direction=dirn,
            details="PCR over \u00b14 strikes around spot%s = %.2f - %s. More "
                    "current/ATM-sensitive than the full-chain PCR; best pulse of "
                    "who is defending the money."
                    % ((" %s" % "{:,.0f}".format(spot)) if spot else "", atm_pcr, band),
            source="nse")

    iv = m.get("avg_iv")
    if iv is None:
        out["iv"] = factor("Implied Volatility (IV)", "N/A", status="warning",
                           details="Option chain data unavailable", source="nse")
    else:
        atm_iv = m.get("atm_iv")
        underlying = m.get("underlying")
        expiry = m.get("expiry")
        max_pain = m.get("max_pain")
        pct = m.get("nifty_pct")
        status = "warning" if iv >= 18.0 else "ok"
        if pct is not None and pct < 0 and iv >= 18.0:
            dirn = "bearish"
        elif pct is not None and pct > 0 and iv < 14.0:
            dirn = "bullish"
        else:
            dirn = "neutral"
        details = "OI-weighted IV %.2f%%" % iv
        if atm_iv is not None:
            details += " (ATM %.2f%%)" % atm_iv
        if underlying:
            details += " | spot %s" % "{:,.0f}".format(underlying)
        if expiry:
            details += " | expiry %s" % expiry
        if max_pain:
            details += " | max pain %s" % "{:,.0f}".format(max_pain)
        details += (". High IV = expensive options / fear; low IV = cheap. "
                    "Falling IV during a rally = steady accumulation (bullish); "
                    "IV spikes during a decline = panic put buying (bearish).")
        out["iv"] = factor("Implied Volatility (IV)", "%.2f%%" % iv,
                           direction=dirn, status=status, details=details,
                           source="nse")

    votes, weights, signals = [], [], []

    def add(direction, weight):
        votes.append(vote(direction))
        weights.append(weight)

    d, _ = pcr_band(m.get("pcr"))
    add(d, 3.0)
    signals.append("PCR %.2f=%s" % (m["pcr"], d) if m.get("pcr") is not None
                   else "PCR n/a")

    d, _ = pcr_band(m.get("atm_pcr"))
    add(d, 2.0)
    signals.append("ATM PCR %.2f=%s" % (m["atm_pcr"], d)
                   if m.get("atm_pcr") is not None else "ATM PCR n/a")

    net = m.get("total_put_chng", 0) - m.get("total_call_chng", 0)
    thr = 0.05 * (abs(m.get("total_call_chng", 0))
                  + abs(m.get("total_put_chng", 0))) or 1000
    add("bullish" if net > thr else "bearish" if net < -thr else "neutral", 2.0)
    signals.append("net \u0394OI %s" % "{:+,}".format(net))

    cc = m.get("highest_call_chng", 0)
    add("bearish" if cc > 0 else "bullish" if cc < 0 else "neutral", 1.5)
    signals.append("call wall \u0394OI %s" % "{:+,}".format(cc))

    pc = m.get("highest_put_chng", 0)
    add("bullish" if pc > 0 else "bearish" if pc < 0 else "neutral", 1.5)
    signals.append("put wall \u0394OI %s" % "{:+,}".format(pc))

    if m.get("nifty_pct") is not None and m.get("chng_oi"):
        d, qlabel = quadrant(m)
        add(d, 2.0)
        signals.append(qlabel)

    total_w = sum(weights) or 1.0
    score = sum(v * w for v, w in zip(votes, weights)) / total_w
    dirn = ("bullish" if score >= 0.25
            else "bearish" if score <= -0.25 else "neutral")
    level = ("strong" if abs(score) >= 0.5
             else "moderate" if abs(score) >= 0.25 else "balanced")
    out["bias"] = factor(
        "Option-Chain Bias", "%s (%+.0f%%)" % (dirn.upper(), score * 100),
        direction=dirn,
        details="Weighted composite of OI-positioning signals. Score %+.0f%% "
                "(%s). | %s. Extreme PCR readings are contrarian - fade the crowd."
                % (score * 100, level, " \u00b7 ".join(signals)),
        source="nse")
    return out


# --------------------------------------------------------------------------
# Hourly marks, power/effect nodes and the tiny prediction net
# --------------------------------------------------------------------------

_PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_DATE_RE = re.compile(r"\b\d{1,2}[-/][A-Za-z]{3,}[-/]\d{2,4}\b")


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


def _pct(value):
    """Signed percent from '+1.23%' / '-0.4%', else None."""
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
    """First plain number in a display string, else None.

    Used for level factors such as Brent ($113.2), PCR (1.62) or FII flow
    (-3810 Cr). Dates like 03-Nov-2026 are deliberately ignored.
    """
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


def _pearson(xs, ys):
    n = len(xs)
    if n < 4 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _clamp(value, low, high):
    return max(low, min(high, value))


class TinyNet:
    """A deliberately small one-hidden-layer MLP, pure Python.

    Inputs are already bounded (~[-1, 1]) by the effect/power nodes, so the
    net needs no scaler. It is retrained from scratch on every hourly mark.
    """

    def __init__(self, n_in, n_hidden=6, seed=1234):
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

    def expand(self, n_in):
        """Add zero-weight inputs so old learning is preserved."""
        n_in = max(1, int(n_in))
        while self.n_in < n_in:
            for row in self.w1:
                row.append(0.0)
            self.n_in += 1

    def fit(self, xs, ys, epochs=90, lr=0.03, l2=0.002, deadline=None):
        n = len(xs)
        if not n:
            return self
        n_in = self.n_in
        nh = self.n_hidden
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
                    for i in range(n_in):
                        row[i] += dh * x[i]
                gb2 += dy
            for j in range(nh):
                row = self.w1[j]
                for i in range(n_in):
                    row[i] -= lr * (gw1[j][i] + l2 * row[i])
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

    @classmethod
    def from_dict(cls, data):
        try:
            net = cls(int(data["n_in"]), int(data["n_hidden"]))
            if (len(data["w1"]) != net.n_hidden or
                    len(data["w1"][0]) != net.n_in):
                return None
            net.w1 = [[float(v) for v in row] for row in data["w1"]]
            net.b1 = [float(v) for v in data["b1"]]
            net.w2 = [float(v) for v in data["w2"]]
            net.b2 = float(data["b2"])
            return net
        except Exception:
            return None


def nifty_mark(indices, quotes):
    """Current NIFTY 50 spot as {price, day_pct, source} (NSE, then Yahoo)."""
    row = find_index(indices, "NIFTY 50")
    if row:
        try:
            price = float(row.get("last") or 0)
            prev = float(row.get("previousClose") or 0)
            pct = row.get("percentChange")
            try:
                day = float(pct) if pct is not None else None
            except (TypeError, ValueError):
                day = None
            if day is None and prev:
                day = (price - prev) / prev * 100.0
            if price > 0:
                return {"price": price, "day_pct": day, "source": "NSE"}
        except (TypeError, ValueError):
            pass
    q = (quotes or {}).get("^NSEI")
    if q and q.get("price"):
        try:
            price = float(q["price"])
            prev = float(q.get("prev") or 0)
            day = (price - prev) / prev * 100.0 if prev else None
            return {"price": price, "day_pct": day, "source": "Yahoo"}
        except (TypeError, ValueError):
            pass
    return None


class HourlyTracker:
    """Persists one full-data mark per market hour and learns from the marks.

    Nodes
    -----
    power  : 0-100, how strongly this factor's hourly change has historically
             co-moved with the NIFTY hourly return. Starts at 100 (unknown)
             and becomes |Pearson r| * 100 once >= 6 paired marks exist.
    effect : 0-100, the current signal strength. The mapping is exactly:
               bullish  -> 75 at/below the mean move, 100 at the
                           all-time biggest positive move
               bearish  -> 25 at/below the mean move, 0 at the
                           all-time biggest negative move
               neutral  -> 50
               no measurable delta / first mark -> 100 (schema default)
             The mean is the running mean of that factor's hourly |delta|
             for the same direction; the max is its all-time max.
    """

    def __init__(self, nodes_file=None, hourly_file=None, nn_file=None,
                 record_override=None):
        self.lock = threading.RLock()
        self.nodes_file = nodes_file or NODES_FILE
        self.hourly_file = hourly_file or HOURLY_FILE
        self.nn_file = nn_file or NN_FILE
        self.record_override = record_override
        self.nodes = _read_json(self.nodes_file, {}) or {}
        hourly = _read_json(self.hourly_file, []) or []
        self.hourly = hourly if isinstance(hourly, list) else []
        nn_data = _read_json(self.nn_file, {}) or {}
        self.nn_data = nn_data if isinstance(nn_data, dict) else {}
        self.feature_keys = list(self.nn_data.get("feature_keys") or [])
        self.net = TinyNet.from_dict(self.nn_data.get("weights") or {})
        if self.net and self.net.n_in != len(self.feature_keys) + 4:
            self.net = None
        self.last_bucket = (self.hourly[-1].get("bucket")
                            if self.hourly else None)

    # ---- public ----------------------------------------------------------

    def apply(self, categories, nifty, day_pct=None):
        """Attach nodes to factors, record a mark on a new hour, and return
        the hourly-track category plus chip data."""
        with self.lock:
            now = now_ist()
            open_now = (self.record_override if self.record_override is not None
                        else is_market_open())
            live = {}
            current_keys = []
            for cat in categories:
                if cat.get("name") == TRACK_CATEGORY:
                    continue
                for f in cat.get("factors") or []:
                    key = "%s::%s" % (cat.get("name"), f.get("key"))
                    live[key] = f
                    current_keys.append(key)
                    node = self.nodes.get(key) or {}
                    f["power"] = round(float(node.get("power", 100.0)), 1)
                    f["effect"] = round(float(node.get("effect", 100.0)), 1)
                    f["power_corr"] = node.get("corr")
                    f["power_samples"] = node.get("power_samples")
                    f["node_samples"] = int(node.get("samples") or 0)
                    f["node_delta"] = node.get("last_delta")

            bucket = now.strftime("%Y-%m-%d %H")
            price = (nifty or {}).get("price")
            if open_now and price and bucket != self.last_bucket:
                self._record(categories, live, current_keys, nifty,
                             now, bucket, day_pct)

            latest = self.hourly[-1] if self.hourly else None
            track = self._track(categories, latest, nifty, now, open_now,
                                day_pct, len(live))
            return track

    def recent(self, limit=24):
        with self.lock:
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 24
            n = max(1, min(n, 200))
            return {"marks": len(self.hourly),
                    "records": self.hourly[-n:]}

    def dump(self):
        with self.lock:
            return list(self.hourly)

    # ---- recording -------------------------------------------------------

    def _record(self, categories, live, current_keys, nifty,
                now, bucket, day_pct):
        prev = self.hourly[-1] if self.hourly else None
        price = float(nifty["price"])
        gap = 0.0
        if prev and prev.get("ts"):
            gap = max(0.0, (now.timestamp() - float(prev["ts"])) / 3600.0)
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
            "factors": {},
        }

        # Grow the input list (never shrink it: partial fetch failures must
        # not wipe the net). New inputs start with zero weight.
        new_keys = [k for k in current_keys if k not in self.feature_keys]
        if new_keys:
            self.feature_keys.extend(new_keys)
        if self.net is None:
            self.net = TinyNet(len(self.feature_keys) + 4)
        else:
            self.net.expand(len(self.feature_keys) + 4)

        for key, f in live.items():
            node = self.nodes.setdefault(key, {})
            node.setdefault("power", 100.0)
            node.setdefault("effect", 100.0)
            node.setdefault("max_pos", 0.0)
            node.setdefault("max_neg", 0.0)
            node.setdefault("sum_pos", 0.0)
            node.setdefault("sum_neg", 0.0)
            node.setdefault("n_pos", 0)
            node.setdefault("n_neg", 0)
            node.setdefault("last_raw", None)
            node.setdefault("last_kind", None)
            raw_pct = _pct(f.get("change"))
            if raw_pct is not None:
                raw, kind = raw_pct, "pct"
            else:
                raw, kind = _num(f.get("value")), "level"
            previous_raw = node.get("last_raw")
            if node.get("last_kind") != kind:
                previous_raw = None
            delta = None
            if raw is not None and previous_raw is not None:
                delta = raw - float(previous_raw)
            direction = f.get("direction") or "neutral"
            if delta is None:
                effect = 100.0
            else:
                magnitude = abs(delta)
                if direction == "bullish":
                    node["max_pos"] = max(float(node.get("max_pos") or 0),
                                          magnitude)
                    node["sum_pos"] = float(node.get("sum_pos") or 0) + magnitude
                    node["n_pos"] = int(node.get("n_pos") or 0) + 1
                    top = float(node.get("max_pos") or 0)
                    mean = node["sum_pos"] / node["n_pos"]
                    if top > mean:
                        ratio = (magnitude - mean) / (top - mean)
                    else:
                        ratio = 1.0
                    effect = 75.0 + 25.0 * _clamp(ratio, 0.0, 1.0)
                elif direction == "bearish":
                    node["max_neg"] = max(float(node.get("max_neg") or 0),
                                          magnitude)
                    node["sum_neg"] = float(node.get("sum_neg") or 0) + magnitude
                    node["n_neg"] = int(node.get("n_neg") or 0) + 1
                    top = float(node.get("max_neg") or 0)
                    mean = node["sum_neg"] / node["n_neg"]
                    if top > mean:
                        ratio = (magnitude - mean) / (top - mean)
                    else:
                        ratio = 1.0
                    effect = 25.0 - 25.0 * _clamp(ratio, 0.0, 1.0)
                else:
                    effect = 50.0
            effect = round(_clamp(effect, 0.0, 100.0), 1)
            node["effect"] = effect
            node["last_raw"] = raw
            node["last_kind"] = kind
            node["last_delta"] = round(delta, 4) if delta is not None else None
            node["samples"] = int(node.get("samples") or 0) + 1
            f["effect"] = effect
            f["power"] = round(float(node.get("power", 100.0)), 1)
            rec["factors"][key] = {
                "label": f.get("label"),
                "value": f.get("value"),
                "change": f.get("change"),
                "direction": direction,
                "status": f.get("status"),
                "source": f.get("source"),
                "delta": (round(delta, 4) if delta is not None else None),
                "effect": effect,
                "power": round(float(node.get("power", 100.0)), 1),
                "details": (f.get("details") or "")[:240],
            }

        self.hourly.append(rec)
        self._score_prediction(prev, rec)
        self._update_power()
        self._train_and_predict()
        if len(self.hourly) > HOURLY_MAX:
            self.hourly = self.hourly[-HOURLY_MAX:]

        # Reflect learned power back onto the live snapshot.
        for key, f in live.items():
            node = self.nodes.get(key) or {}
            f["power"] = round(float(node.get("power", 100.0)), 1)
            f["effect"] = round(float(node.get("effect", 100.0)), 1)
            f["power_corr"] = node.get("corr")
            f["power_samples"] = node.get("power_samples")
            f["node_samples"] = int(node.get("samples") or 0)
            f["node_delta"] = node.get("last_delta")

        self.last_bucket = bucket
        _write_json(self.nodes_file, self.nodes)
        _write_json(self.hourly_file, self.hourly)
        _write_json(self.nn_file, self.nn_data)

    def _score_prediction(self, prev, rec):
        if not prev:
            return
        pred = (prev.get("prediction") or {}).get("pct")
        na = prev.get("nifty")
        nb = rec.get("nifty")
        if pred is None or not na or not nb:
            return
        gap = (float(rec["ts"]) - float(prev.get("ts") or 0)) / 3600.0
        if gap <= 0 or gap > 4.0:
            return
        actual = (float(nb) - float(na)) / float(na) * 100.0
        err = abs(float(pred) - actual)
        hit = None
        if abs(actual) >= 0.03:
            hit = 1 if (float(pred) >= 0) == (actual >= 0) else 0
        live = list(self.nn_data.get("live") or [])
        live.append({"at": rec.get("time"), "pred": round(float(pred), 3),
                     "actual": round(actual, 3), "err": round(err, 3),
                     "hit": hit})
        self.nn_data["live"] = live[-60:]

    def _update_power(self):
        """Pearson r between each factor's hourly delta and NIFTY's hourly
        return over the same interval. Needs >= 6 paired marks."""
        if len(self.hourly) < 2:
            return
        returns = {}
        for i in range(1, len(self.hourly)):
            a, b = self.hourly[i - 1], self.hourly[i]
            na, nb = a.get("nifty"), b.get("nifty")
            if not na or not nb:
                continue
            gap = (float(b.get("ts") or 0) - float(a.get("ts") or 0)) / 3600.0
            if gap <= 0 or gap > 4.0:
                continue
            returns[i] = (float(nb) - float(na)) / float(na) * 100.0
        latest = self.hourly[-1].get("factors") or {}
        for key, node in self.nodes.items():
            xs, ys = [], []
            for i, ret in returns.items():
                f = (self.hourly[i].get("factors") or {}).get(key)
                if not f:
                    continue
                delta = f.get("delta")
                if delta is None:
                    continue
                xs.append(float(delta))
                ys.append(ret)
            if len(xs) < 6:
                continue
            r = _pearson(xs, ys)
            if r is None:
                continue
            node["power"] = round(_clamp(abs(r) * 100.0, 5.0, 100.0), 1)
            node["corr"] = round(r, 3)
            node["power_samples"] = len(xs)
            if key in latest:
                latest[key]["power"] = node["power"]

    # ---- neural net ------------------------------------------------------

    def _features(self, records, index):
        rec = records[index]
        out = []
        for key in self.feature_keys:
            f = (rec.get("factors") or {}).get(key)
            if not f:
                out.append(0.0)
                continue
            direction = f.get("direction")
            sign = 1.0 if direction == "bullish" else \
                -1.0 if direction == "bearish" else 0.0
            effect = f.get("effect")
            power = f.get("power")
            effect = 100.0 if effect is None else float(effect)
            power = 100.0 if power is None else float(power)
            delta = f.get("delta")
            confidence = 0.3 if delta is None else 1.0
            # The effect node says how bullish/bearish this factor is in
            # general; the sign of its hourly delta says whether it is
            # currently pushing that way. Combining them gives the net a
            # signed hourly signal instead of a static state.
            delta_sign = -1.0 if (delta is not None and float(delta) < 0) else 1.0
            out.append(sign * (effect / 100.0) * (power / 100.0) *
                       confidence * delta_sign)

        def momentum(lag):
            if index - lag < 0:
                return 0.0
            a = records[index - lag].get("nifty")
            b = rec.get("nifty")
            if not a or not b:
                return 0.0
            return math.tanh((((float(b) - float(a)) / float(a)) * 100.0) /
                             0.5)

        day = rec.get("nifty_day_pct")
        out.extend([momentum(1), momentum(2), momentum(3),
                    math.tanh((float(day) if day is not None else 0.0) / 1.5)])
        return out

    def _pairs(self):
        xs, ys = [], []
        records = self.hourly
        for i in range(len(records) - 1):
            a, b = records[i], records[i + 1]
            na, nb = a.get("nifty"), b.get("nifty")
            if not na or not nb or a.get("date") != b.get("date"):
                continue
            gap = (float(b.get("ts") or 0) - float(a.get("ts") or 0)) / 3600.0
            if gap <= 0 or gap > 2.5:
                continue
            target = (float(nb) - float(na)) / float(na) * 100.0
            if abs(target) > 4.0:
                continue
            xs.append(self._features(records, i))
            ys.append(target)
        return xs[-200:], ys[-200:]

    def _train_and_predict(self):
        xs, ys = self._pairs()
        fit_mae = None
        fit_hit = None
        if len(xs) >= 4:
            # Fresh net on every hourly mark. Warm-starting would carry
            # weights learned from an older feature set into the new hour.
            self.net = TinyNet(len(xs[0]))
            self.net.fit(xs, ys, epochs=90, lr=0.03, l2=0.002,
                         deadline=time.time() + 4.0)
            errors, hits = [], []
            for x, y in zip(xs, ys):
                pred = self.net.predict(x)
                errors.append(abs(pred - y))
                if abs(y) >= 0.03:
                    hits.append(1 if (pred >= 0) == (y >= 0) else 0)
            if errors:
                fit_mae = round(sum(errors) / len(errors), 3)
            if hits:
                fit_hit = round(100.0 * sum(hits) / len(hits), 1)

        prediction = None
        latest = self.hourly[-1] if self.hourly else None
        if latest and self.net is not None and len(xs) >= 4:
            pct = _clamp(self.net.predict(self._features(self.hourly,
                                                         len(self.hourly) - 1)),
                         -3.0, 3.0)
            price = float(latest.get("nifty") or 0) * (1.0 + pct / 100.0)
            prediction = {
                "pct": round(pct, 3),
                "price": round(price, 2),
                "as_of": latest.get("time"),
                "samples": len(xs),
                "fit_mae": fit_mae,
                "fit_hit": fit_hit,
            }
        latest["prediction"] = prediction if latest else None
        self.nn_data["feature_keys"] = self.feature_keys
        self.nn_data["weights"] = self.net.to_dict() if self.net else {}
        self.nn_data["samples"] = len(xs)
        self.nn_data["fit_mae"] = fit_mae
        self.nn_data["fit_hit"] = fit_hit
        self.nn_data["trained_ts"] = time.time()
        self.nn_data["prediction"] = prediction

    # ---- display ---------------------------------------------------------

    def _track(self, categories, latest, nifty, now, open_now, day_pct,
               live_count=0):
        prev = self.hourly[-2] if len(self.hourly) >= 2 else None
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
        if hour_pct is None:
            hour_dir = "neutral"
        elif hour_pct > 0.02:
            hour_dir = "bullish"
        elif hour_pct < -0.02:
            hour_dir = "bearish"
        else:
            hour_dir = "neutral"

        pred = self.nn_data.get("prediction")
        samples = int(self.nn_data.get("samples") or 0)
        live = self.nn_data.get("live") or []
        live_mae = None
        live_hit = None
        if live:
            live_mae = round(sum(float(x.get("err") or 0) for x in live) /
                             len(live), 3)
            scored = [x.get("hit") for x in live if x.get("hit") is not None]
            if scored:
                live_hit = round(100.0 * sum(scored) / len(scored), 1)

        rows = {}
        nifty_value = ("{:,.2f}".format(float(price))) if price else "N/A"
        if hour_pct is None:
            n_change = ""
        else:
            suffix = " / mark" if mark_pct is not None else \
                (" day" if day is not None else "")
            n_change = ("%+.2f%%%s" % (hour_pct, suffix))
        span = ""
        if self.hourly:
            span = "%s -> %s" % (self.hourly[0].get("time"),
                                 self.hourly[-1].get("time"))
        rows["nifty_hourly"] = factor(
            label="NIFTY 50 - hourly mark",
            value=nifty_value,
            change=n_change,
            direction=hour_dir,
            status="ok" if price else "warning",
            details=("Day change %s | Last mark: %s | %d marks stored.%s" %
                     (("%+.2f%%" % float(day)) if day is not None else "N/A",
                      (latest or {}).get("time") or "not yet",
                      len(self.hourly),
                      (" Next mark after %s." %
                       (now.replace(minute=0, second=0, microsecond=0) +
                        timedelta(hours=1)).strftime("%H:00"))
                      if open_now else
                      " Market closed - no new marks.")),
            source=((nifty or {}).get("source") or "NSE") + " hourly mark")

        if pred:
            pred_pct = float(pred.get("pct") or 0)
            pred_dir = ("bullish" if pred_pct > 0.02 else
                        "bearish" if pred_pct < -0.02 else "neutral")
            rows["nn_prediction"] = factor(
                label="Next-hour NIFTY prediction",
                value="%s (%+.2f%%)" % (("{:,.2f}".format(pred.get("price") or 0)),
                                        pred_pct),
                change="next hourly mark",
                direction=pred_dir,
                status="ok",
                details=("Tiny neural net (6 hidden units), trained on %d "
                         "hourly pairs; in-sample MAE %s%%; direction %s%%. "
                         "Target = next hourly NIFTY change." %
                         (samples,
                          ("%.2f" % pred["fit_mae"])
                          if pred.get("fit_mae") is not None else "n/a",
                          ("%.0f" % pred["fit_hit"])
                          if pred.get("fit_hit") is not None else "n/a")),
                source="hourly neural net")
            pred_effect = 75.0 if pred_dir == "bullish" else \
                25.0 if pred_dir == "bearish" else 50.0
        else:
            need = max(0, 4 - samples)
            rows["nn_prediction"] = factor(
                label="Next-hour NIFTY prediction",
                value="collecting data (%d/4)" % samples,
                change="needs %d more hourly pairs" % need,
                direction="neutral",
                status="ok",
                details=("The net starts predicting after 4 hourly pairs "
                         "(about 5 marks). It is retrained from scratch on "
                         "every new mark."),
                source="hourly neural net")
            pred_effect = 50.0

        rows["hourly_log"] = factor(
            label="Hourly data snapshots",
            value="%d saved" % len(self.hourly),
            change=(latest or {}).get("time") or "waiting for first mark",
            direction="neutral",
            status="ok",
            details=("One full screenshot of every factor + NIFTY 50 is saved "
                     "each market hour. File: data/mobile/hourly.json"
                     + ((" | span: " + span) if span else "") +
                     " | view: /hourly.json"),
            source="local hourly log")

        model_value = "ready" if pred else ("learning %d/4" % samples)
        feature_count = len(self.feature_keys) if self.feature_keys else live_count
        model_details = ("Features: %d factor nodes + 4 momentum inputs. "
                         "Hidden: 6. Retrained on every hourly mark." %
                         feature_count)
        if samples:
            model_details += (" Fit MAE %s%%." %
                              (("%.2f" % self.nn_data["fit_mae"])
                               if self.nn_data.get("fit_mae") is not None
                               else "n/a"))
        if live_mae is not None:
            model_details += " Live MAE %.2f%%" % live_mae
        if live_hit is not None:
            model_details += " | direction %.0f%% (%d scored)" % (
                live_hit, len([x for x in live if x.get("hit") is not None]))
        rows["model_status"] = factor(
            label="Neural net status",
            value=model_value,
            change="%d pairs" % samples,
            direction="neutral",
            status="ok",
            details=model_details,
            source="local model")

        # Nodes on the track rows: power is always the 100 default; effect
        # follows the same bullish/bearish mapping where a direction exists.
        rows["nifty_hourly"]["effect"] = 75.0 if hour_dir == "bullish" else \
            25.0 if hour_dir == "bearish" else 50.0
        rows["nn_prediction"]["effect"] = pred_effect
        rows["hourly_log"]["effect"] = 100.0
        rows["model_status"]["effect"] = 100.0
        for row in rows.values():
            row["power"] = 100.0
        for key, row in rows.items():
            row["key"] = key

        category = {
            "name": TRACK_CATEGORY,
            "short": "Hourly Track / NN",
            "num": "0",
            "factors": [rows[k] for k in
                        ("nifty_hourly", "nn_prediction",
                         "hourly_log", "model_status")],
        }

        chips = []
        if price:
            day_change = (("%+.2f%% day" % float(day))
                          if day is not None else "")
            if day is None:
                day_dir = hour_dir
            elif day > 0.02:
                day_dir = "bullish"
            elif day < -0.02:
                day_dir = "bearish"
            else:
                day_dir = "neutral"
            chips.append({
                "label": "NIFTY 50",
                "value": nifty_value,
                "change": day_change or n_change,
                "direction": day_dir,
                "status": "ok",
            })
        chips.append({
            "label": "NN NEXT",
            "value": (("%+.2f%%" % float(pred["pct"]))
                      if pred else "learning"),
            "change": (("%s" % ("{:,.0f}".format(pred.get("price") or 0)))
                       if pred else "%d/4 marks" % samples),
            "direction": (("bullish" if float(pred["pct"]) > 0.02 else
                           "bearish" if float(pred["pct"]) < -0.02 else
                           "neutral") if pred else "neutral"),
            "status": "ok" if pred else "manual",
        })

        return {
            "category": category,
            "chips": chips,
            "meta": {
                "nifty": price,
                "nifty_day_pct": day,
                "hour_pct": hour_pct,
                "marks": len(self.hourly),
                "last_mark": (latest or {}).get("time"),
                "prediction": pred,
                "model": {
                    "samples": samples,
                    "feature_keys": (len(self.feature_keys)
                                     if self.feature_keys
                                     else live_count) + 4,
                    "fit_mae": self.nn_data.get("fit_mae"),
                    "fit_hit": self.nn_data.get("fit_hit"),
                    "live_mae": live_mae,
                    "live_hit": live_hit,
                },
            },
        }


TRACKER = HourlyTracker()


# --------------------------------------------------------------------------
# Snapshot assembly
# --------------------------------------------------------------------------

CATEGORY_ORDER = [
    "1. CRUDE OIL",
    "2. CURRENCY",
    "3. FOREIGN FLOWS",
    "4. GLOBAL MARKETS",
    "5. US BOND YIELDS / DOLLAR",
    "6. GEOPOLITICS",
    "7. MACRO DATA / POLICY",
    "8. EARNINGS / CORPORATE NEWS",
    "9. SECTOR SIGNALS",
    "10. SENTIMENT / TECHNICAL",
    "11. OPTION CHAIN - NIFTY",
]

CATEGORY_SHORT = {
    "1. CRUDE OIL": "Crude Oil",
    "2. CURRENCY": "Currency",
    "3. FOREIGN FLOWS": "Foreign Flows",
    "4. GLOBAL MARKETS": "Global Markets",
    "5. US BOND YIELDS / DOLLAR": "Bonds / Dollar",
    "6. GEOPOLITICS": "Geopolitics",
    "7. MACRO DATA / POLICY": "Macro / Policy",
    "8. EARNINGS / CORPORATE NEWS": "Earnings",
    "9. SECTOR SIGNALS": "Sector Signals",
    "10. SENTIMENT / TECHNICAL": "Sentiment",
    "11. OPTION CHAIN - NIFTY": "Option Chain",
}

KEY_CHIPS = [
    ("1. CRUDE OIL", "brent", "BRENT"),
    ("2. CURRENCY", "usd_inr", "USD/INR"),
    ("4. GLOBAL MARKETS", "sgx_nifty", "GIFT NIFTY"),
    ("10. SENTIMENT / TECHNICAL", "vix", "INDIA VIX"),
    ("10. SENTIMENT / TECHNICAL", "pcr", "PCR"),
    ("5. US BOND YIELDS / DOLLAR", "dxy", "DXY"),
]


def build_snapshot():
    """Fetch every data source once and assemble the 11 factor categories."""
    errors = []
    raw = {}
    started = time.time()

    with ThreadPoolExecutor(max_workers=14) as pool:
        futures = {
            "indices": pool.submit(fetch_indices),
            "fii": pool.submit(fetch_fii),
            "chain": pool.submit(fetch_chain_bundle),
            "gift": pool.submit(fetch_gift),
            "deals": pool.submit(fetch_block_deals),
            "news": pool.submit(fetch_news_batch),
            "te": pool.submit(fetch_te_batch),
            "mc": pool.submit(fetch_results_today),
        }
        for symbol in TICKERS:
            futures["quote:" + symbol] = pool.submit(yahoo_quote, symbol)

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
        metrics["nifty_pct"] = nifty_pct(indices)
    gift = raw.get("gift")
    deals = raw.get("deals")
    news = raw.get("news") or {}
    te = raw.get("te") or {}
    mc = raw.get("mc") or []

    builders = [
        ("1. CRUDE OIL", lambda: cat_crude(quotes)),
        ("2. CURRENCY", lambda: cat_currency(quotes)),
        ("3. FOREIGN FLOWS", lambda: cat_flows(fii_data, indices, news)),
        ("4. GLOBAL MARKETS", lambda: cat_global(quotes, gift, news)),
        ("5. US BOND YIELDS / DOLLAR", lambda: cat_bonds(quotes)),
        ("6. GEOPOLITICS", lambda: cat_geopolitics(news)),
        ("7. MACRO DATA / POLICY", lambda: cat_macro(te, news)),
        ("8. EARNINGS / CORPORATE NEWS", lambda: cat_earnings(mc, news)),
        ("9. SECTOR SIGNALS", lambda: cat_sectors(quotes, news)),
        ("10. SENTIMENT / TECHNICAL",
         lambda: cat_sentiment(indices, fii_data, metrics, deals, quotes)),
        ("11. OPTION CHAIN - NIFTY", lambda: cat_option_chain(metrics)),
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
            if VERBOSE:
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
            if item["direction"] == "bullish":
                counts["bullish"] += 1
            elif item["direction"] == "bearish":
                counts["bearish"] += 1
            else:
                counts["neutral"] += 1
            if item["status"] == "manual":
                counts["manual"] += 1
            elif item["status"] == "warning":
                counts["warning"] += 1
            chip_lookup[(name, key)] = item
        categories.append({
            "name": name,
            "short": CATEGORY_SHORT.get(name, name),
            "num": name.split(".")[0],
            "factors": factors,
        })

    chips = []
    for cat, key, label in KEY_CHIPS:
        item = chip_lookup.get((cat, key))
        if not item:
            continue
        chips.append({
            "label": label,
            "value": item["value"],
            "change": item["change"],
            "direction": item["direction"],
            "status": item["status"],
        })

    # Hourly track: attach power/effect nodes, append this hour's full-data
    # mark if the market is open, and add the NN prediction card.
    track_meta = {}
    try:
        nifty = nifty_mark(indices, quotes)
        track = TRACKER.apply(categories, nifty)
        if track:
            track_meta = track.get("meta") or {}
            if track.get("category"):
                categories.insert(0, track["category"])
            chips = (track.get("chips") or []) + chips
    except Exception as exc:
        errors.append("hourly track: %s" % exc)
        if VERBOSE:
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
    }


def merge_previous(snapshot, previous):
    """Keep last-good categories when a refresh partially failed."""
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
                "name": old["name"],
                "short": old.get("short", old["name"]),
                "num": old.get("num"),
                "factors": factors,
            })
    order = {TRACK_CATEGORY: -1}
    order.update({n: i for i, n in enumerate(CATEGORY_ORDER)})
    snapshot["categories"].sort(key=lambda c: order.get(c["name"], 99))

    counts = {"bullish": 0, "bearish": 0, "neutral": 0, "manual": 0, "warning": 0}
    for cat in snapshot["categories"]:
        if cat.get("name") == TRACK_CATEGORY:
            continue
        for f in cat["factors"]:
            if f["direction"] == "bullish":
                counts["bullish"] += 1
            elif f["direction"] == "bearish":
                counts["bearish"] += 1
            else:
                counts["neutral"] += 1
            if f["status"] == "manual":
                counts["manual"] += 1
            elif f["status"] == "warning":
                counts["warning"] += 1
    snapshot["counts"] = counts
    total = counts["bullish"] + counts["bearish"]
    snapshot["sentiment"] = round(counts["bullish"] / total * 100) if total else 50
    return snapshot


def load_disk_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("categories"):
                return data
    except Exception:
        pass
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
    """Holds the current snapshot and coordinates background refreshes."""

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
            print("[%s] refreshed in %.1fs (%d factors, %d error notes)"
                  % (stamp(), snap.get("duration", 0),
                     sum(len(c["factors"]) for c in snap["categories"]),
                     len(snap.get("errors") or [])))
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            print("[%s] refresh failed: %s" % (stamp(), self.last_error))
            if VERBOSE:
                traceback.print_exc()
        finally:
            with self.lock:
                self.refreshing = False

    def status(self):
        snap = self.get() or {}
        with self.lock:
            return {
                "refreshing": self.refreshing,
                "ready": bool(snap),
                "updated": snap.get("updated"),
                "age": round(self.age()) if snap else None,
                "error": self.last_error,
                "last_attempt": self.last_attempt,
            }


# --------------------------------------------------------------------------
# Mobile page (single file, no external assets)
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1420">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Market Pulse">
<title>Pre-Market Pulse</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<style>
:root{
  --bg:#0f1420; --card:#161f33; --card2:#111a2b; --fg:#e8edf7; --mut:#8b97ad;
  --line:#243149; --accent:#e94560; --green:#00c853; --red:#ff4b5c;
  --amber:#ffb300; --blue:#4aa3ff;
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
.logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#7a2bff);
  display:grid;place-items:center;font-size:12px;font-weight:800;color:#fff;flex:none}
h1{font-size:15px;letter-spacing:.6px;margin:0;font-weight:800;flex:1}
.badge{font-size:10px;font-weight:800;letter-spacing:.4px;padding:3px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.badge.on{color:#04120a;background:var(--green);border-color:var(--green)}
.badge.off{color:var(--mut)}
.meta{display:flex;align-items:center;gap:8px;margin-top:4px;font-size:11px;color:var(--mut)}
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
.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px}
.chip{background:var(--card2);border:1px solid var(--line);border-radius:9px;padding:5px 7px;min-width:0}
.chip .k{font-size:9px;letter-spacing:.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .v{font-size:12px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .c{font-size:10px;color:var(--mut)}
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
  display:grid;place-items:center;font-size:10px;font-weight:800;color:var(--accent);flex:none}
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
.cat.live{border-color:rgba(74,163,255,.45)}
.cat.live .cat-head .num{color:var(--blue);border-color:rgba(74,163,255,.5)}
.det{font-size:11.5px;color:var(--mut);margin-top:3px;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.row.open .det{-webkit-line-clamp:unset}
.src{font-size:9.5px;color:#546178;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{padding:18px;text-align:center;color:var(--mut);font-size:13px}
.foot{padding:14px 12px calc(18px + env(safe-area-inset-bottom));color:#5d6b83;font-size:10.5px;text-align:center;line-height:1.5}
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
      <h1>PRE-MARKET PULSE</h1>
      <span id="market" class="badge off">...</span>
    </div>
    <div class="meta">
      <span id="updated">waiting for first refresh</span>
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
        <div class="biasbar" title="Power-weighted effect node bias across ticked factors with a measurable hourly delta; no or zero delta counts as neutral (bull / bear, out of 100).">
          <div class="biashead"><span>P/E BIAS</span><span id="peVals">-- / --</span></div>
          <div class="split" id="peBar" role="img" aria-label="Power/effect bias">
            <i class="bull" id="peBullFill"></i><i class="bear" id="peBearFill"></i>
          </div>
        </div>
        <div class="biasbar" id="nnBias" title="Neural net next-hour NIFTY bias (bull / bear, out of 100).">
          <div class="biashead"><span>NEURAL BIAS</span><span id="nnVals">warming up</span></div>
          <div class="split muted" id="nnBar" role="img" aria-label="Neural bias warming up">
            <i class="bull" id="nnBullFill"></i><i class="bear" id="nnBearFill"></i>
          </div>
        </div>
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
  <a class="btn iconbtn" href="/hourly.json" title="Download the hourly data log" target="_blank">LOG</a>
  <a class="btn iconbtn" href="/export.csv" title="Download CSV">CSV</a>
</div>

<nav class="nav" id="nav"></nav>

<main id="cats"><div class="empty">Fetching live data&hellip; this can take up to 60 seconds on first run.</div></main>

<div id="errs"></div>
<div class="foot">
  Data: NSE &middot; Yahoo Finance &middot; Google News &middot; TradingEconomics &middot; Moneycontrol &middot; RBI<br>
  <b>P</b> = power node (learned importance 0-100) &middot;
  <b>E</b> = effect node (bullish 75 at the mean move up to 100 at the all-time max; bearish 25 down to 0; neutral 50)<br>
  Effect updates every hourly mark; power learns after 6 paired marks.
  Bias bars: <b>P/E</b> = power-weighted effect nodes with a measurable hourly delta (no/zero delta = neutral); <b>Neural</b> = next-hour NN output (50 neutral, &plusmn;1% = full bull/bear).<br>
  Nodes + NN retrain hourly while the market is open. <a href="/hourly.json" target="_blank" style="color:var(--blue)">Hourly log</a><br>
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
function saveTicks(){
  try { localStorage.setItem("pmp_unticked", JSON.stringify(Array.from(state.unticked))); } catch(e){}
}
function loadTicks(){
  try {
    var raw = localStorage.getItem("pmp_unticked");
    if (raw) { state.unticked = new Set(JSON.parse(raw)); }
  } catch(e){ state.unticked = new Set(); }
}
function isTicked(id){
  return !state.unticked.has(id);
}
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
  var p = Math.round(f.power == null ? 100 : f.power);
  var e = Math.round(f.effect == null ? 100 : f.effect);
  var ptitle = "Power node: learned importance for NIFTY (0-100)";
  if (f.power_corr != null) {
    ptitle += " | r=" + Number(f.power_corr).toFixed(2) + " over " + (f.power_samples || 0) + " marks";
  }
  var ecls = (f.direction === "neutral" || f.status === "manual" || f.status === "warning")
    ? "a" : (e >= 60 ? "g" : e <= 40 ? "r" : "a");
  var nodes = '<div class="nodes">' +
    '<span class="node p" title="' + esc(ptitle) + '">P <b>' + p + '</b></span>' +
    '<span class="node e ' + ecls + '" title="Effect node: bullish 75 at the mean move to 100 at the all-time max; bearish 25 down to 0; neutral 50; no measurable delta = 100 default">E <b>' + e + '</b></span>' +
    '</div>';
  var tick = (cat.name || "").indexOf("0.") === 0
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
  var cls = c.direction === "bullish" ? "g" : c.direction === "bearish" ? "r" : "";
  var color = c.direction === "bullish" ? "var(--green)" : c.direction === "bearish" ? "var(--red)" : "var(--fg)";
  return '<div class="chip"><div class="k">' + esc(c.label) + '</div>' +
    '<div class="v" style="color:' + color + '">' + esc(c.value) + '</div>' +
    '<div class="c">' + esc(c.change || "\u00a0") + '</div></div>';
}
function setBiasBar(prefix, bull, bear, label, muted){
  var b = Number(bull), r = Number(bear);
  if (!isFinite(b)) b = 50;
  if (!isFinite(r)) r = 100 - b;
  b = Math.max(0, Math.min(100, b));
  r = Math.max(0, Math.min(100, r));
  var sum = b + r;
  if (sum <= 0) { b = 50; r = 50; sum = 100; }
  var bw = b / sum * 100;
  var rw = 100 - bw;
  var bf = el(prefix + "BullFill"), rf = el(prefix + "BearFill");
  if (bf) bf.style.width = bw + "%";
  if (rf) rf.style.width = rw + "%";
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
      var e = Number(f.effect == null ? 100 : f.effect);
      var p = Number(f.power == null ? 100 : f.power);
      if (!isFinite(e)) e = 100;
      if (!isFinite(p)) p = 100;
      e = Math.max(0, Math.min(100, e));
      p = Math.max(0, Math.min(100, p));
      var d = f.node_delta;
      if (d == null || !isFinite(Number(d)) || Math.abs(Number(d)) < 1e-9) e = 50;
      var w = Math.max(0.05, p / 100);
      score += e * w;
      weight += w;
    });
  });
  var peBull = weight ? score / weight : 50;
  var peBear = 100 - peBull;
  setBiasBar("pe", peBull, peBear,
    Math.round(peBull) + " / " + Math.round(peBear), false);

  var t = state.data.track || {};
  var pred = t.prediction;
  var nnBull = 50, nnBear = 50, muted = true, label = "warming up";
  if (pred && pred.pct != null && isFinite(Number(pred.pct))) {
    var pct = Number(pred.pct);
    nnBull = Math.max(0, Math.min(100, 50 + pct * 50));
    nnBear = 100 - nnBull;
    muted = false;
    label = Math.round(nnBull) + " / " + Math.round(nnBear);
    el("nnBias").title = "Neural net next-hour NIFTY bias: " +
      (pct >= 0 ? "+" : "") + pct.toFixed(2) +
      "% | 50 = neutral, +1% = full bull, -1% = full bear.";
  } else {
    var samples = (t.model && t.model.samples) || 0;
    label = "warming up " + samples + "/4";
    el("nnBias").title = "Neural net is collecting hourly pairs. " +
      "It predicts after 4 pairs.";
  }
  setBiasBar("nn", nnBull, nnBear, label, muted);
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
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(function(){});
}
load();
loadStatus();
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Pre-Market Pulse",
    "short_name": "Market Pulse",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1420",
    "theme_color": "#0f1420",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
    ],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="42" fill="#0f1420"/>
<rect x="18" y="18" width="156" height="156" rx="32" fill="#161f33" stroke="#243149" stroke-width="4"/>
<path d="M42 128 L74 92 L98 112 L124 66 L150 84" fill="none" stroke="#e94560" stroke-width="12"
 stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="150" cy="84" r="9" fill="#00c853"/>
</svg>"""

SW_JS = """/* minimal offline shell: network-first, cache the page */
const CACHE = "pmp-v1";
self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/", "/icon.svg"])));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  if (e.request.url.indexOf("/api/") >= 0) return;
  e.respondWith(
    fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
"""


def qr_png(url, box_size=9):
    """PNG QR code for a URL, or None when the optional qrcode lib is absent."""
    if qrcode is None:
        return None
    try:
        qr = qrcode.QRCode(version=None, box_size=box_size, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f1420", back_color="#ffffff")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def csv_bytes(snapshot):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Category", "Factor", "Value", "Change", "Direction",
                     "Power", "Effect", "Status", "Details", "Source",
                     "Updated", "Stale"])
    for cat in (snapshot or {}).get("categories") or []:
        for f in cat.get("factors") or []:
            writer.writerow([cat.get("name"), f.get("label"), f.get("value"),
                             f.get("change"), f.get("direction"),
                             f.get("power"), f.get("effect"),
                             f.get("status"), f.get("details"),
                             f.get("source"), f.get("updated"),
                             "yes" if f.get("stale") else ""])
    return buf.getvalue().encode("utf-8-sig")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

STORE = None


class QuietServer(ThreadingHTTPServer):
    """Threaded server that ignores browser keep-alive disconnects."""

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
    server_version = "MarketPulse/1.0"

    def log_message(self, fmt, *args):
        if VERBOSE:
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
                    "updated": None, "updated_ts": 0, "market_open": is_market_open(),
                    "sentiment": 50, "counts": {}, "chips": [], "categories": [],
                    "track": {}, "errors": [], "refreshing": STORE.refreshing,
                }
                data = dict(snap)
                data["refreshing"] = STORE.refreshing
                data["server_time"] = stamp()
                self._json(data)
            elif path == "/api/status":
                self._json(STORE.status())
            elif path == "/api/hourly":
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                try:
                    limit = int((query.get("limit") or [24])[0])
                except (TypeError, ValueError):
                    limit = 24
                self._json(TRACKER.recent(limit))
            elif path == "/hourly.json":
                body = json.dumps(TRACKER.dump(), ensure_ascii=False)
                self._send(200, body, "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="market_pulse_hourly.json"'})
            elif path == "/export.csv":
                STORE.ensure_fresh()
                body = csv_bytes(STORE.get() or {})
                name = "market_pulse_%s.csv" % now_ist().strftime("%Y%m%d_%H%M")
                self._send(200, body, "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % name})
            elif path == "/manifest.webmanifest":
                self._send(200, json.dumps(MANIFEST), "application/manifest+json")
            elif path == "/qr":
                url = "http://%s:%d/" % (lan_ip(),
                                         self.server.server_address[1])
                png = qr_png(url)
                if png:
                    self._send(200, png, "image/png")
                else:
                    self._send(200,
                               "QR needs: pip install qrcode[pil]\nURL: %s\n" % url,
                               "text/plain; charset=utf-8")
            elif path == "/sw.js":
                self._send(200, SW_JS, "application/javascript; charset=utf-8")
            elif path == "/icon.svg":
                self._send(200, ICON_SVG, "image/svg+xml")
            elif path in ("/market_mobile.py", "/download"):
                try:
                    with open(os.path.abspath(__file__), "rb") as fh:
                        body = fh.read()
                    self._send(200, body, "text/x-python; charset=utf-8",
                               {"Content-Disposition":
                                'attachment; filename="market_mobile.py"'})
                except Exception:
                    self._send(404, "Script not found",
                               "text/plain; charset=utf-8")
            elif path == "/api/health":
                self._json({"ok": True, "time": stamp(), "status": STORE.status()})
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


def scheduler(store, market_minutes, off_minutes):
    while True:
        time.sleep(60)
        try:
            if store.refreshing:
                continue
            limit = (market_minutes if is_market_open() else off_minutes) * 60
            if store.age() > limit:
                print("[%s] scheduled refresh (age %ds)" % (stamp(), int(store.age())))
                store.refresh_async()
        except Exception:
            pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Pre-market pulse - one-file live market dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 = this PC only)")
    p.add_argument("--lan", action="store_true",
                   help="also expose to phone/tablet on the local network")
    p.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    p.add_argument("--open", action="store_true", help="open a browser on this machine")
    p.add_argument("--refresh", type=int, default=10,
                   help="auto-refresh minutes while the market is open (default 10)")
    p.add_argument("--off-hours", type=int, default=60,
                   help="auto-refresh minutes while closed (default 60)")
    p.add_argument("--verbose", action="store_true", help="log every HTTP request")
    return p.parse_args(argv)


def main(argv=None):
    global STORE, VERBOSE
    args = parse_args(argv)
    VERBOSE = args.verbose
    host = "0.0.0.0" if args.lan else args.host

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    os.makedirs(DATA_DIR, exist_ok=True)

    STORE = Store()
    cached = STORE.get()
    if cached:
        print("Loaded cached snapshot from %s" % (cached.get("updated") or "disk"))
    else:
        print("No cache yet - building the first snapshot now (may take a minute)")

    STORE.refresh_async()
    threading.Thread(target=scheduler,
                     args=(STORE, args.refresh, args.off_hours),
                     daemon=True).start()

    try:
        server = QuietServer((host, args.port), Handler)
    except OSError as exc:
        sys.stderr.write(
            "\nCould not start on %s:%s - %s\n"
            "Try a different port:  python market_mobile.py --port 8081\n"
            % (host, args.port, exc))
        raise SystemExit(1)

    ip = lan_ip()
    print("")
    print("=" * 58)
    print(" PRE-MARKET PULSE is running (PC version)")
    print("=" * 58)
    print(" Open on this PC :  http://127.0.0.1:%d" % args.port)
    if host in ("0.0.0.0", ""):
        print(" Phone / tablet  :  http://%s:%d" % (ip, args.port))
        print("                   (same Wi-Fi; use --lan to enable this)")
    else:
        print(" Mode            :  PC only (use --lan for phone/tablet)")
    print("")
    print(" Hourly tracker  :  saves all factors + NIFTY every market hour")
    print(" Nodes / NN files:  %s" % DATA_DIR)
    print(" Keep this window open during market hours to collect the hourly")
    print(" marks and retrain the net. Press Ctrl+C to stop.")
    print("=" * 58)
    print("")

    if args.lan:
        print(" If the phone cannot connect, allow the port through the "
              "firewall:")
        print("   netsh advfirewall firewall add rule name=\"MarketPulse %d\" "
              "dir=in action=allow protocol=TCP localport=%d"
              % (args.port, args.port))
        print(" Tip: in the phone browser use 'Add to Home Screen'.")
        qr_path = os.path.join(DATA_DIR, "phone_qr.png")
        png = qr_png("http://%s:%d/" % (ip, args.port))
        if png:
            try:
                with open(qr_path, "wb") as fh:
                    fh.write(png)
                print(" Scan this image with the phone camera: %s" % qr_path)
            except Exception:
                pass
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
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
