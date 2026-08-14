from typing import Optional
import yfinance as yf

from agents.base_agent import BaseAgent, FactorResult
from config import TICKERS


class CurrencyAgent(BaseAgent):
    agent_name = "currency"
    cache_ttl_hours = 0.25

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        ticker = TICKERS["usd_inr"]
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")

            if hist.empty:
                info = t.info
                price = info.get("regularMarketPrice") or info.get("previousClose")
                if price is not None:
                    prev = info.get("previousClose", price)
                    pct = (price - prev) / prev if prev else 0
                    if pct < -0.0002:
                        direction = "strengthening"
                    elif pct > 0.0002:
                        direction = "weakening"
                    else:
                        direction = "flat"
                    results["usd_inr"] = self._make_result(
                        label="USD/INR",
                        value=f"{price:.4f}",
                        change=self._pct_change(price, prev),
                        direction="bullish" if direction == "strengthening" else "bearish" if direction == "weakening" else "neutral",
                        details=f"Prev: {prev:.4f} | Lower USD/INR = stronger rupee = bullish",
                        source="yfinance",
                    )
                    results["direction"] = self._make_result(
                        label="Direction",
                        value=direction.capitalize(),
                        direction="bullish" if direction == "strengthening" else "bearish" if direction == "weakening" else "neutral",
                        details=f"Rupee is {direction} vs USD",
                        source="yfinance",
                    )
                else:
                    results["usd_inr"] = self._make_result(
                        label="USD/INR", value="N/A", status="error",
                        details="No data available", source="yfinance",
                    )
                cache_data = {k: v.to_dict() for k, v in results.items()}
                self._save_cache(cache_data)
                return results

            latest = hist.iloc[-1]
            current = float(latest["Close"])
            prev_close = float(hist.iloc[-2]["Close"]) if len(hist) > 1 else current

            pct = (current - prev_close) / prev_close if prev_close else 0
            if pct < -0.0002:
                direction = "strengthening"
            elif pct > 0.0002:
                direction = "weakening"
            else:
                direction = "flat"
            results["usd_inr"] = self._make_result(
                label="USD/INR",
                value=f"{current:.4f}",
                change=self._pct_change(current, prev_close),
                direction="bullish" if direction == "strengthening" else "bearish" if direction == "weakening" else "neutral",
                details=f"Prev: {prev_close:.4f} | Lower = stronger rupee",
                source="yfinance",
            )
            results["direction"] = self._make_result(
                label="Direction",
                value=direction.capitalize(),
                direction="bullish" if direction == "strengthening" else "bearish" if direction == "weakening" else "neutral",
                details=f"Rupee is {direction} vs USD | Steep fall triggers FII selling",
                source="yfinance",
            )
        except Exception as e:
            results["usd_inr"] = self._make_result(
                label="USD/INR", value="Error", status="error",
                details=str(e), source="yfinance",
            )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data)
        return results
