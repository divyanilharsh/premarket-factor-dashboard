from typing import Optional
import re

import yfinance as yf

from agents.base_agent import BaseAgent, FactorResult
from config import TICKERS


class GlobalMarketsAgent(BaseAgent):
    agent_name = "global_markets"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        us_pairs = [
            ("dow", "Dow Jones", TICKERS["dow"]),
            ("sp500", "S&P 500", TICKERS["sp500"]),
            ("nasdaq", "Nasdaq", TICKERS["nasdaq"]),
        ]
        for key, label, ticker in us_pairs:
            results[key] = self._fetch_quote(label, ticker)

        asian_pairs = [
            ("nikkei", "Nikkei 225", TICKERS["nikkei"]),
            ("hang_seng", "Hang Seng", TICKERS["hang_seng"]),
            ("shanghai", "Shanghai Composite", TICKERS["shanghai"]),
        ]
        for key, label, ticker in asian_pairs:
            results[key] = self._fetch_quote(label, ticker)

        results["sgx_nifty"] = self._fetch_sgx_nifty()

        results["recession_fears"] = self._compute_risk_mood(results)

        results["fed_decision"] = self._fetch_fed_decision()

        results["currency_interventions"] = self._fetch_currency_interventions()

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data)
        return results

    def _fetch_quote(self, label: str, ticker: str) -> FactorResult:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if hist.empty:
                info = t.info
                price = info.get("regularMarketPrice") or info.get("previousClose")
                if price is not None:
                    prev = info.get("previousClose", price)
                    return self._make_result(
                        label=label,
                        value=f"{price:,.2f}",
                        change=self._pct_change(price, prev),
                        direction=self._compute_direction(price, prev),
                        details=f"Prev close: {prev:,.2f}",
                        source="yfinance",
                    )
                return self._make_result(
                    label=label, value="N/A", status="error",
                    details="No data", source="yfinance",
                )
            latest = hist.iloc[-1]
            current = float(latest["Close"])
            prev_close = float(hist.iloc[0]["Close"]) if len(hist) > 1 else current
            return self._make_result(
                label=label,
                value=f"{current:,.2f}",
                change=self._pct_change(current, prev_close),
                direction=self._compute_direction(current, prev_close),
                details=f"Prev close: {prev_close:,.2f}",
                source="yfinance",
            )
        except Exception as e:
            return self._make_result(
                label=label, value="Error", status="error",
                details=str(e), source="yfinance",
            )

    def _fetch_sgx_nifty(self) -> FactorResult:
        gift = self._fetch_gift_nifty_nse()
        if gift:
            return gift
        for ticker in [TICKERS["sgx_nifty"], TICKERS["gift_nifty"]]:
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="2d")
                if not hist.empty:
                    latest = hist.iloc[-1]
                    current = float(latest["Close"])
                    prev_close = float(hist.iloc[0]["Close"]) if len(hist) > 1 else current
                    return self._make_result(
                        label="SGX / GIFT Nifty",
                        value=f"{current:,.2f}",
                        change=self._pct_change(current, prev_close),
                        direction=self._compute_direction(current, prev_close),
                        details=f"India open gap indicator. Prev: {prev_close:,.2f}",
                        source="yfinance",
                    )
            except Exception:
                continue
        return self._make_result(
            label="SGX / GIFT Nifty",
            value="N/A",
            status="warning",
            details="Ticker unavailable — compute gap from Nifty futures",
            source="yfinance",
        )

    def _fetch_gift_nifty_nse(self) -> Optional[FactorResult]:
        try:
            from utils.nse_scraper import get_gift_nifty
            resp = get_gift_nifty()
            if not resp:
                return None
            data = resp.get("data") or {}
            gift = data.get("giftNifty") or {}
            last = float(gift.get("lastprice", 0) or 0)
            if last > 0:
                pct = float(gift.get("perchange", 0) or 0)
                day_change = float(gift.get("daychange", 0) or 0)
                direction = "bullish" if pct > 0.05 else "bearish" if pct < -0.05 else "neutral"
                details = (f"NSE GIFT Nifty futures (exp {gift.get('expirydate', '?')}). "
                           f"Day change: {day_change:+,.2f} pts")
                ts = gift.get("timestmp", "")
                if ts:
                    details += f" | As of {ts}"
                try:
                    nifty_spot = self._nifty_spot_level()
                    if nifty_spot:
                        gap = (last - nifty_spot) / nifty_spot * 100
                        sign = "+" if gap >= 0 else ""
                        details += f" | Gap vs Nifty spot: {sign}{gap:.2f}%"
                except Exception:
                    pass
                return self._make_result(
                    label="SGX / GIFT Nifty",
                    value=f"{last:,.2f}",
                    change=f"{'+' if pct >= 0 else ''}{pct:.2f}%",
                    direction=direction,
                    details=details,
                    source="nse",
                )
            index_list = data.get("indexList") or []
            if index_list:
                idx = index_list[0]
                current = float(idx.get("last", 0) or 0)
                prev = float(idx.get("previousClose", 0) or 0)
                pct = float(idx.get("percChange", 0) or 0)
                if current > 0:
                    direction = "bullish" if pct > 0.05 else "bearish" if pct < -0.05 else "neutral"
                    details = f"NSE NIFTY50 USD (GIFT Nifty proxy). Prev: {prev:,.2f}"
                    usd_inr = data.get("usdInr") or {}
                    try:
                        inr_rate = float(usd_inr.get("ltp", 0) or 0)
                        if inr_rate:
                            details += f" | Nifty equiv: {current * inr_rate:,.0f}"
                    except (TypeError, ValueError):
                        pass
                    return self._make_result(
                        label="SGX / GIFT Nifty",
                        value=f"{current:,.2f} USD",
                        change=f"{'+' if pct >= 0 else ''}{pct:.2f}%",
                        direction=direction,
                        details=details,
                        source="nse",
                    )
        except Exception:
            pass
        return None

    def _nifty_spot_level(self) -> Optional[float]:
        try:
            from utils.nse_scraper import get_all_indices
            all_idx = get_all_indices()
            if not all_idx:
                return None
            for idx in all_idx.get("data", []):
                if idx.get("index") == "NIFTY 50":
                    return float(idx.get("last", 0) or 0)
        except Exception:
            pass
        return None

    def _compute_risk_mood(self, results: dict) -> FactorResult:
        scores = {"bullish": 1, "bearish": -1, "neutral": 0}
        total = 0
        count = 0
        for key in ("dow", "sp500", "nasdaq", "nikkei", "hang_seng", "shanghai"):
            r = results.get(key)
            if r and r.direction in scores:
                total += scores[r.direction]
                count += 1
        if count == 0:
            return self._make_result(
                label="Global Risk Mood", value="N/A", status="warning",
                details="Could not compute from index moves", source="derived")
        avg = total / count
        if avg > 0.3:
            mood, direction = "Risk-ON", "bullish"
        elif avg < -0.3:
            mood, direction = "Risk-OFF", "bearish"
        else:
            mood, direction = "Mixed", "neutral"
        green = sum(1 for k in ("dow", "sp500", "nasdaq", "nikkei", "hang_seng", "shanghai")
                    if results.get(k) and results[k].direction == "bullish")
        return self._make_result(
            label="Global Risk Mood",
            value=mood,
            direction=direction,
            details=f"{green}/6 major indices positive. Deep risk-off = bearish for EM; relief = bullish.",
            source="derived",
        )

    def _fetch_fed_decision(self) -> FactorResult:
        try:
            from utils.news_scraper import fetch_news_headlines
            headlines = fetch_news_headlines("Federal Reserve FOMC rate decision", max_results=4)
            if headlines:
                joined = " ".join(h["title"] for h in headlines).lower()
                recent = headlines[0]["title"]
                if re.search(r"\bcut|lower|ease\b", joined):
                    return self._make_result(
                        label="Fed Rate Decision",
                        value="Rate CUT leaning",
                        direction="bullish",
                        details=f"Recent: {recent[:100]} | Dovish = risk-on, bullish for EM",
                        source="google_news_rss",
                    )
                if re.search(r"\bhike|raise|tighten\b", joined):
                    return self._make_result(
                        label="Fed Rate Decision",
                        value="Rate HIKE leaning",
                        direction="bearish",
                        details=f"Recent: {recent[:100]} | Hawkish Fed = pressure on EM",
                        source="google_news_rss",
                    )
                return self._make_result(
                    label="Fed Rate Decision",
                    value="Hold / neutral news",
                    status="warning",
                    manual=True,
                    details=f"Recent: {recent[:100]} | Hawkish Fed = pressure on EM; dovish = risk-on",
                    source="google_news_rss",
                )
        except Exception:
            pass
        return self._make_result(
            label="Fed Rate Decision",
            value="Check news",
            status="manual",
            manual=True,
            details="Hawkish Fed = pressure on EM; dovish = risk-on bullish",
            source="manual",
        )

    def _fetch_currency_interventions(self) -> FactorResult:
        try:
            from utils.news_scraper import fetch_news_headlines
            headlines = fetch_news_headlines("yen intervention BOJ currency", max_results=4)
            if headlines:
                joined = " ".join(h["title"] for h in headlines).lower()
                if "interven" in joined:
                    return self._make_result(
                        label="Currency Interventions",
                        value="Intervention flagged",
                        status="warning",
                        direction="neutral",
                        details=f"Recent: {headlines[0]['title'][:100]} | Intervention shifts FX flows, adds volatility",
                        source="google_news_rss",
                    )
                return self._make_result(
                    label="Currency Interventions",
                    value="No intervention news",
                    details=f"Latest: {headlines[0]['title'][:100]}",
                    source="google_news_rss",
                )
        except Exception:
            pass
        return self._make_result(
            label="Currency Interventions",
            value="No news found",
            details="Watch for BOJ yen intervention, PBOC fixing shifts",
            source="google_news_rss",
        )
