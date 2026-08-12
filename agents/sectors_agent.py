from agents.base_agent import BaseAgent, FactorResult
from utils.news_scraper import fetch_news_headlines, assess_headline_tone


class SectorsAgent(BaseAgent):
    agent_name = "sectors"
    cache_ttl_hours = 0.5

    def set_external_data(self, crude_results: dict, currency_results: dict,
                          global_results: dict):
        self.crude_results = crude_results
        self.currency_results = currency_results
        self.global_results = global_results

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        crude_dir = self._get_direction(getattr(self, "crude_results", {}), "brent")
        rupee_dir = self._get_direction(getattr(self, "currency_results", {}), "usd_inr")
        nasdaq_dir = self._get_direction(getattr(self, "global_results", {}), "nasdaq")

        results = {}

        def crude_reaction(positive_details: str, negative_details: str) -> FactorResult:
            if crude_dir == "bearish":
                return self._make_result(label="", value="Bullish", direction="bullish",
                                         details=positive_details, source="derived")
            if crude_dir == "bullish":
                return self._make_result(label="", value="Bearish", direction="bearish",
                                         details=negative_details, source="derived")
            return self._make_result(label="", value="Neutral", direction="neutral",
                                     details="Crude flat vs prev close — no fuel-cost trigger. " + positive_details,
                                     source="derived")

        r = crude_reaction("Fuel is biggest cost; low crude = bullish, high = bearish",
                           "Fuel is biggest cost; high crude = bearish")
        r.label = "Aviation (crude-linked)"
        results["aviation"] = r

        results["banks"] = self._make_result(
            label="Banks / Financials",
            value="Depends on FII flows",
            status="manual",
            manual=True,
            details="Gains on inflows; NIMs react to rates. Cross-check FII flows.",
            source="derived",
        )

        it_score = 0
        if rupee_dir == "bearish":
            it_score += 1
        elif rupee_dir == "bullish":
            it_score -= 1
        if nasdaq_dir == "bullish":
            it_score += 1
        elif nasdaq_dir == "bearish":
            it_score -= 1
        if it_score > 0:
            it_value, it_dir = "Bullish", "bullish"
        elif it_score < 0:
            it_value, it_dir = "Bearish", "bearish"
        else:
            it_value, it_dir = "Neutral", "neutral"
        results["it"] = self._make_result(
            label="IT (global tech, rupee)",
            value=it_value,
            direction=it_dir,
            details=f"Weak rupee (USD/INR up) boosts IT earnings; Nasdaq direction sets global tech tone. "
                    f"Signals: rupee {'weak' if rupee_dir == 'bearish' else 'strong' if rupee_dir == 'bullish' else 'flat'}, "
                    f"Nasdaq {nasdaq_dir}",
            source="derived",
        )

        results["us_visa"] = self._fetch_us_visa()

        results["ai_disruption"] = self._fetch_ai_news()

        r = crude_reaction("Low oil + GST cuts = demand boost = bullish",
                           "High oil = higher fuel prices, demand hit = bearish")
        r.label = "Auto (GST, oil, demand)"
        results["auto"] = r

        r = crude_reaction("Low crude = better margins = bullish",
                           "High crude = margin compression = bearish")
        r.label = "OMCs / Paints / Tyres"
        results["omcs"] = r

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results

    def _get_direction(self, results: dict, key: str) -> str:
        r = results.get(key)
        if r and hasattr(r, "direction"):
            return r.direction
        return "neutral"

    def _fetch_us_visa(self) -> FactorResult:
        headlines = fetch_news_headlines("US visa H1B India IT outsourcing restriction", max_results=4)
        if headlines:
            joined = " ".join(h["title"] for h in headlines).lower()
            recent = headlines[0]["title"]
            if "restrict" in joined or "ban" in joined or "tighten" in joined or "cap" in joined:
                return self._make_result(
                    label="US Visa / IT Outsourcing",
                    value="Restrictions flagged",
                    direction="bearish",
                    details=f"Recent: {recent[:100]} | Restrictions hurt Indian IT = sector drag",
                    source="google_news_rss",
                )
            return self._make_result(
                label="US Visa / IT Outsourcing",
                value="No restriction news",
                details=f"Latest: {recent[:100]}",
                source="google_news_rss",
            )
        return self._make_result(
            label="US Visa / IT Outsourcing",
            value="No news found",
            status="manual",
            manual=True,
            details="Restrictions on H1B hurt Indian IT = sector drag",
            source="manual",
        )

    def _fetch_ai_news(self) -> FactorResult:
        headlines = fetch_news_headlines("AI disruption India IT outsourcing jobs", max_results=4)
        if headlines:
            joined = " ".join(h["title"] for h in headlines).lower()
            recent = headlines[0]["title"]
            if "disrupt" in joined or "replace" in joined or "cut" in joined or "layoff" in joined:
                return self._make_result(
                    label="AI Disrupting IT Outsourcing",
                    value="Disruption headlines",
                    status="warning",
                    direction="bearish",
                    details=f"Recent: {recent[:100]} | Structural fear hits IT sector sentiment",
                    source="google_news_rss",
                )
            return self._make_result(
                label="AI Disrupting IT Outsourcing",
                value="No disruption headlines",
                details=f"Latest: {recent[:100]}",
                source="google_news_rss",
            )
        return self._make_result(
            label="AI Disrupting IT Outsourcing",
            value="No news found",
            status="manual",
            manual=True,
            details="Long-term structural fear; hits IT sector sentiment",
            source="manual",
        )
