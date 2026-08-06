from typing import Optional
import yfinance as yf

from agents.base_agent import BaseAgent, FactorResult
from config import TICKERS


class CrudeAgent(BaseAgent):
    agent_name = "crude"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        for key, label, ticker in [
            ("brent", "Brent Crude (BZ=F)", TICKERS["brent"]),
            ("wti", "WTI Crude (CL=F)", TICKERS["wti"]),
        ]:
            result = self._fetch_yf_quote(key, label, ticker)
            results[key] = result

        brent_dir = results.get("brent", FactorResult(label="")).direction
        wti_dir = results.get("wti", FactorResult(label="")).direction
        if brent_dir == wti_dir:
            dir_label = brent_dir.capitalize()
        elif brent_dir == "neutral":
            dir_label = wti_dir.capitalize()
        elif wti_dir == "neutral":
            dir_label = brent_dir.capitalize()
        else:
            dir_label = "Mixed"

        results["direction"] = self._make_result(
            label="Direction",
            value=dir_label,
            direction="bearish" if dir_label == "Rising" or dir_label == "Bearish"
            else "bullish" if dir_label == "Falling" or dir_label == "Bullish"
            else "neutral",
            details="Rising crude = bearish for India (net importer)",
            source="yfinance",
        )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data)
        return results

    def _fetch_yf_quote(self, key: str, label: str, ticker: str) -> FactorResult:
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
                        value=f"${price:.2f}",
                        change=self._pct_change(price, prev),
                        direction=self._compute_direction(price, prev),
                        details=f"Prev close: ${prev:.2f}",
                        source="yfinance",
                    )
                return self._make_result(
                    label=label, value="N/A", status="error",
                    details="No data available", source="yfinance",
                )

            latest = hist.iloc[-1]
            current = float(latest["Close"])
            prev_close = float(hist.iloc[0]["Close"]) if len(hist) > 1 else current

            return self._make_result(
                label=label,
                value=f"${current:.2f}",
                change=self._pct_change(current, prev_close),
                direction=self._compute_direction(current, prev_close),
                details=f"Prev close: ${prev_close:.2f}",
                source="yfinance",
            )
        except Exception as e:
            return self._make_result(
                label=label, value="Error", status="error",
                details=str(e), source="yfinance",
            )
