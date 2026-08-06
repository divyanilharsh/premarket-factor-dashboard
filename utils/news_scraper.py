import feedparser
import time
from datetime import datetime, timezone
from typing import Optional

from config import GOOGLE_NEWS_RSS
from utils.scraper import ScraperSession


def format_relative_time(published_str: str) -> str:
    """Convert an RSS published timestamp to 'Xh ago' / 'Xm ago' / 'Xd ago'."""
    if not published_str:
        return "?"
    try:
        # feedparser returns a struct_time via published_parsed; parse it here
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(published_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_sec = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        if age_sec < 3600:
            return f"{int(age_sec // 60)}m ago"
        if age_sec < 86400:
            return f"{int(age_sec // 3600)}h ago"
        return f"{int(age_sec // 86400)}d ago"
    except Exception:
        return "?"


def fetch_news_headlines(query: str, max_results: int = 5) -> list[dict]:
    url = GOOGLE_NEWS_RSS.format(query=query.replace(" ", "%20"))
    try:
        feed = feedparser.parse(url)
        entries = feed.get("entries", [])
        results = []
        for entry in entries[:max_results]:
            published = entry.get("published", "")
            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": published,
                "published_ago": format_relative_time(published),
                "source": entry.get("source", {}).get("title", ""),
            })
        return results
    except Exception:
        return []


def assess_headline_tone(headlines: list[dict]) -> str:
    negative_keywords = [
        "crash", "plunge", "tumble", "fear", "risk", "tension", "war",
        "tariff", "sanction", "selloff", "crisis", "ban", "restriction",
        "inflation", "recession", "slowdown", "deficit", "crude spike",
        "oil surge", "rate hike", "hawkish", "volatility", "uncertainty",
    ]
    positive_keywords = [
        "rally", "surge", "gain", "bull", "growth", "recovery", "deal",
        "peace", "ceasefire", "rate cut", "dovish", "stimulus", "boost",
        "strong", "record", "order win", "upgrade",
    ]
    neg_count = 0
    pos_count = 0
    for h in headlines:
        title = h.get("title", "").lower()
        for kw in negative_keywords:
            if kw in title:
                neg_count += 1
        for kw in positive_keywords:
            if kw in title:
                pos_count += 1
    total = neg_count + pos_count
    if total == 0:
        return "neutral"
    ratio = pos_count / total
    if ratio > 0.6:
        return "bullish"
    elif ratio < 0.4:
        return "bearish"
    return "neutral"
