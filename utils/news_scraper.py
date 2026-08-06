import feedparser
import time
from typing import Optional

from config import GOOGLE_NEWS_RSS
from utils.scraper import ScraperSession


def fetch_news_headlines(query: str, max_results: int = 5) -> list[dict]:
    url = GOOGLE_NEWS_RSS.format(query=query.replace(" ", "%20"))
    try:
        feed = feedparser.parse(url)
        entries = feed.get("entries", [])
        results = []
        for entry in entries[:max_results]:
            results.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
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
