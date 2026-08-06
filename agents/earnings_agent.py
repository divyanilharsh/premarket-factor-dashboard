from agents.base_agent import BaseAgent, FactorResult
from utils.news_scraper import fetch_news_headlines, assess_headline_tone
from utils.moneycontrol_scraper import fetch_results_due_today


class EarningsAgent(BaseAgent):
    agent_name = "earnings"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        due_today = fetch_results_due_today()
        if due_today:
            names = ", ".join(f"{e['name']} ({e['result_type']})" for e in due_today[:8])
            results["major_results"] = self._make_result(
                label="Major Results Due Today",
                value=f"{len(due_today)} result(s)",
                direction="neutral",
                details=names + " | Beats lift index; misses drag",
                source="moneycontrol",
            )
        else:
            results["major_results"] = self._make_result(
                label="Major Results Due Today",
                value="None listed",
                status="warning",
                manual=True,
                details="No results scheduled today on Moneycontrol calendar — check company announcements",
                source="moneycontrol",
            )

        earn_headlines = fetch_news_headlines("earnings results NSE BSE quarterly", max_results=3)
        earn_tone = assess_headline_tone(earn_headlines)
        results["earnings_beats"] = self._make_result(
            label="Earnings Beats / Misses",
            value=earn_tone.capitalize(),
            direction=earn_tone,
            details=" | ".join(h["title"][:60] for h in earn_headlines[:2]) if earn_headlines else "No major headlines",
            source="google_news_rss",
            manual=True,
        )

        order_headlines = fetch_news_headlines("order win contract NSE BSE India", max_results=3)
        order_tone = assess_headline_tone(order_headlines)
        results["order_wins"] = self._make_result(
            label="Order Wins / Deals",
            value=order_tone.capitalize(),
            direction=order_tone,
            details=" | ".join(h["title"][:60] for h in order_headlines[:2]) if order_headlines else "No major headlines",
            source="google_news_rss",
            manual=True,
        )

        ofs_headlines = fetch_news_headlines("government OFS divestment India", max_results=3)
        results["govt_ofs"] = self._make_result(
            label="Government OFS / Divestment",
            value="Check news",
            status="manual",
            manual=True,
            details="Discounted OFS = supply pressure + liquidity drain = bearish",
            source="google_news_rss",
        )

        ipo_headlines = fetch_news_headlines("IPO listing NSE BSE India", max_results=3)
        results["ipo_listings"] = self._make_result(
            label="IPO Listings / New Issues",
            value="Check news",
            status="manual",
            manual=True,
            details="Big IPO absorbs market liquidity, adds volatility",
            source="google_news_rss",
        )

        sebi_headlines = fetch_news_headlines("SEBI order ban regulatory India", max_results=3)
        results["regulatory_orders"] = self._make_result(
            label="Regulatory / Compliance Orders",
            value="Check news",
            status="manual",
            manual=True,
            details="SEBI ban / governance risk hits stock + sector contagion",
            source="google_news_rss",
        )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results
