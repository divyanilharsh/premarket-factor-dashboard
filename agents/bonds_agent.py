import yfinance as yf

from agents.base_agent import BaseAgent, FactorResult
from config import TICKERS


class BondsAgent(BaseAgent):
    agent_name = "bonds"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        tnx = self._fetch_quote("10-yr US Treasury Yield (^TNX)", TICKERS["tnx"],
                                suffix="%", bullish_up=False)
        results["tnx"] = tnx

        dxy = self._fetch_quote("US Dollar Index (DXY)", TICKERS["dxy"],
                                suffix="", bullish_up=False)
        results["dxy"] = dxy

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data)
        return results

    def _fetch_quote(self, label: str, ticker: str,
                     suffix: str = "", bullish_up: bool = True) -> FactorResult:
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
                        value=f"{price:.2f}{suffix}",
                        change=self._pct_change(price, prev),
                        direction=self._compute_direction(price, prev, bullish_up=bullish_up),
                        details=f"Prev close: {prev:.2f}{suffix}",
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
                value=f"{current:.2f}{suffix}",
                change=self._pct_change(current, prev_close),
                direction=self._compute_direction(current, prev_close, bullish_up=bullish_up),
                details=f"Prev close: {prev_close:.2f}{suffix} | "
                        f"{'Rising = bearish for EM' if not bullish_up else ''}",
                source="yfinance",
            )
        except Exception as e:
            return self._make_result(
                label=label, value="Error", status="error",
                details=str(e), source="yfinance",
            )
