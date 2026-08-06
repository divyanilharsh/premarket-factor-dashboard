from agents.base_agent import BaseAgent, FactorResult
from utils.news_scraper import fetch_news_headlines, assess_headline_tone


class GeopoliticsAgent(BaseAgent):
    agent_name = "geopolitics"

    TOPICS = [
        ("oil_tensions", "Middle East / Oil Region Tensions",
         "middle east oil supply tensions"),
        ("trade_war", "Trade War / Tariffs",
         "trade war tariffs"),
        ("sanctions", "Sanctions / Military Action",
         "sanctions military action"),
        ("us_india_tariff", "US-India Tariff / Trade Action",
         "US India tariff trade"),
        ("peace_talks", "Peace-Talk Status (Ukraine/Russia)",
         "peace talks ukraine russia"),
    ]

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        for key, label, query in self.TOPICS:
            headlines = fetch_news_headlines(query, max_results=3)
            tone = assess_headline_tone(headlines)

            if headlines:
                summary = " | ".join(h["title"][:60] for h in headlines[:2])
            else:
                summary = "No recent headlines found"

            if key == "peace_talks":
                direction = "bullish" if tone == "bullish" else "bearish" if tone == "bearish" else "neutral"
            elif key == "oil_tensions":
                direction = "bearish" if tone == "bearish" else "bullish" if tone == "bullish" else "neutral"
            else:
                direction = tone

            results[key] = self._make_result(
                label=label,
                value=tone.capitalize(),
                direction=direction,
                details=summary,
                source="google_news_rss",
                manual=True,
            )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results
