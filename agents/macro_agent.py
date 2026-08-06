from agents.base_agent import BaseAgent, FactorResult
import re

from utils.tradingeconomics_scraper import (
    fetch_repo_policy, fetch_forex_reserves, fetch_inflation,
    fetch_trade_balance, fetch_industrial_production,
)
from utils.rbi_scraper import fetch_rbi_repo_rate, fetch_forex_reserves_rbi
from utils.news_scraper import fetch_news_headlines, assess_headline_tone


class MacroAgent(BaseAgent):
    agent_name = "macro"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        results["rbi_repo"] = self._fetch_rbi_repo()
        results["rbi_guidance"] = self._fetch_rbi_guidance(results["rbi_repo"])
        results["trade_balance"] = self._fetch_trade_balance()
        results["cpi_wpi"] = self._fetch_cpi()
        results["iip_gst"] = self._fetch_iip()
        results["monsoon"] = self._fetch_monsoon()
        results["forex_reserves"] = self._fetch_forex()
        results["economic_calendar"] = self._fetch_calendar()
        results["liquidity"] = self._make_result(
            label="Central Bank Liquidity / Forex Measures",
            value="Check RBI announcements",
            status="manual",
            manual=True,
            details="Tighter liquidity = bearish; rupee-support measures = positive",
            source="manual",
        )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=3)
        return results

    def _fetch_rbi_repo(self) -> FactorResult:
        data = fetch_repo_policy()
        if data:
            rate = data.get("rate")
            stance = data.get("stance", "neutral")
            value = f"{rate}%" if rate is not None else stance.capitalize()
            return self._make_result(
                label="RBI Policy / Repo Rate",
                value=value,
                direction=data.get("direction", "neutral"),
                details=f"Stance: {stance}. {data['summary'][:220]}",
                source="tradingeconomics",
            )
        fallback = fetch_rbi_repo_rate()
        return self._make_result(
            label="RBI Policy / Repo Rate",
            value=fallback or "N/A",
            status="warning",
            manual=True,
            details="Rate cut = bullish liquidity; hike/tight = bearish",
            source="rbi.org.in",
        )

    def _fetch_rbi_guidance(self, repo_result: FactorResult) -> FactorResult:
        stance = "neutral"
        details = repo_result.details or ""
        low = details.lower()
        if re.search(r"\bhawkish\b|\bhiked\b|\braised\b", low):
            stance = "hawkish"
        elif re.search(r"\bdovish\b|\bcut\b|\breduced\b", low):
            stance = "dovish"
        elif "neutral" in low:
            stance = "neutral"
        direction = "bearish" if stance == "hawkish" else "bullish" if stance == "dovish" else "neutral"
        return self._make_result(
            label="RBI Forward Guidance",
            value=stance.capitalize(),
            direction=direction,
            details=f"Policy tone reads {stance}. Even with rate held, hawkish tone = bearish; dovish = relief rally.",
            source="tradingeconomics",
        )

    def _fetch_trade_balance(self) -> FactorResult:
        data = fetch_trade_balance()
        if data and data.get("summary"):
            bal = data.get("balance")
            value = f"${bal:,.1f} B" if bal is not None else "N/A"
            details = f"Exports ${data.get('exports') or '?':,.1f}B / Imports ${data.get('imports') or '?':,.1f}B. " if data.get("exports") else ""
            details += data["summary"][:250]
            return self._make_result(
                label="Trade Balance / CAD",
                value=value,
                direction=data.get("direction", "neutral"),
                details=details,
                source="tradingeconomics",
            )
        return self._make_result(
            label="Trade Balance / CAD",
            value="Check news",
            status="warning",
            manual=True,
            details="Widening deficit = rupee pressure = bearish",
            source="manual",
        )

    def _fetch_cpi(self) -> FactorResult:
        data = fetch_inflation()
        if data and data.get("rate") is not None:
            return self._make_result(
                label="CPI / WPI Inflation",
                value=f"{data['rate']:.2f}%",
                change=f"prev {data['previous']:.2f}%" if data.get("previous") is not None else "",
                direction=data.get("direction", "neutral"),
                details=f"High inflation = fewer rate cuts = bearish. {data['summary'][:220]}",
                source="tradingeconomics",
            )
        cpi_headlines = fetch_news_headlines("India CPI WPI inflation", max_results=3)
        tone = assess_headline_tone(cpi_headlines)
        return self._make_result(
            label="CPI / WPI Inflation",
            value=tone.capitalize(),
            direction="bearish" if tone == "bearish" else "bullish",
            status="warning",
            manual=True,
            details="High inflation = fewer rate cuts = bearish",
            source="google_news_rss",
        )

    def _fetch_iip(self) -> FactorResult:
        data = fetch_industrial_production()
        if data and data.get("value") is not None:
            return self._make_result(
                label="IIP / Industrial Production",
                value=f"{data['value']:+.1f}% YoY",
                direction=data.get("direction", "neutral"),
                details=data["summary"][:250],
                source="tradingeconomics",
            )
        return self._make_result(
            label="IIP / GST Collections",
            value="Check latest release",
            status="warning",
            manual=True,
            details="Strong growth = bullish economic outlook",
            source="manual",
        )

    def _fetch_monsoon(self) -> FactorResult:
        monsoon_headlines = fetch_news_headlines("India monsoon El Nino rainfall", max_results=3)
        tone = assess_headline_tone(monsoon_headlines)
        direction = "neutral"
        joined = " ".join(h["title"] for h in monsoon_headlines).lower()
        if "deficit" in joined or "weak" in joined or "below normal" in joined:
            direction = "bearish"
        elif "surplus" in joined or "good" in joined or "above normal" in joined:
            direction = "bullish"
        details = " | ".join(h["title"][:60] for h in monsoon_headlines[:2]) if monsoon_headlines else \
            "Weak monsoon = food inflation = rate pressure = bearish"
        return self._make_result(
            label="Monsoon / El Nino",
            value=tone.capitalize(),
            direction=direction,
            status="manual",
            manual=True,
            details=details,
            source="google_news_rss",
        )

    def _fetch_forex(self) -> FactorResult:
        data = fetch_forex_reserves()
        if data and data.get("value") is not None:
            change_str = ""
            if data.get("previous") is not None:
                delta = data["value"] - data["previous"]
                change_str = f"{'+' if delta >= 0 else ''}{delta:,.0f} M"
            return self._make_result(
                label="Forex Reserves",
                value=f"${data['value'] / 1000:.0f} B",
                change=change_str,
                direction=data.get("direction", "neutral"),
                details=data["summary"][:220],
                source="tradingeconomics",
            )
        fallback = fetch_forex_reserves_rbi()
        return self._make_result(
            label="Forex Reserves",
            value=fallback or "N/A",
            status="warning",
            manual=True,
            details="Rising reserves = external stability = bullish; falling = bearish",
            source="rbi.org.in",
        )

    def _fetch_calendar(self) -> FactorResult:
        today_headlines = fetch_news_headlines("India economic data releases today", max_results=3)
        if today_headlines:
            details = " | ".join(h["title"][:60] for h in today_headlines[:2])
            return self._make_result(
                label="Scheduled Economic Releases Today",
                value="Check headlines",
                status="warning",
                manual=True,
                details=details,
                source="google_news_rss",
            )
        return self._make_result(
            label="Scheduled Economic Releases Today",
            value="Check calendar",
            status="manual",
            manual=True,
            details="Uncertainty before data = volatile; beat = bullish",
            source="manual",
        )
