import json
import os
import time

from agents.base_agent import BaseAgent, FactorResult
from config import CACHE_DIR
from utils.nse_scraper import get_fii_dii_data, get_all_indices
from utils.news_scraper import fetch_news_headlines


class FlowsAgent(BaseAgent):
    agent_name = "flows"

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}
        fii_data = get_fii_dii_data()

        if not fii_data:
            fallback_items = [
                ("fii_net", "FII Net Buy/Sell"),
                ("dii_net", "DII Net Buy/Sell"),
                ("dii_ofs", "DII Selling for OFS"),
                ("fii_selloff", "FII Buying During Selloff"),
                ("fii_trend", "FII Consecutive Days Trend"),
                ("flows_manual", "FII/DII Data Interpretation"),
            ]
            for key, label in fallback_items:
                results[key] = self._make_result(
                    label=label,
                    value="N/A",
                    status="warning",
                    manual=True,
                    details="NSE data unavailable (market closed or blocked) — check daily report",
                    source="nse",
                )
            cache_data = {k: v.to_dict() for k, v in results.items()}
            self._save_cache(cache_data, ttl_hours=1)
            return results

        try:
            fii_net_val = None
            dii_net_val = None
            for item in fii_data:
                cat = item.get("category", "")
                val_str = item.get("netValue", "0")
                try:
                    val = float(val_str.replace(",", ""))
                except (ValueError, TypeError):
                    val = 0
                if "FII" in cat.upper() or "FPI" in cat.upper():
                    fii_net_val = val
                elif "DII" in cat.upper():
                    dii_net_val = val

            if fii_net_val is None:
                fii_net_val = 0
            if dii_net_val is None:
                dii_net_val = 0

            fii_dir = "bullish" if fii_net_val > 0 else "bearish" if fii_net_val < 0 else "neutral"
            dii_dir = "bullish" if dii_net_val > 0 else "bearish" if dii_net_val < 0 else "neutral"

            results["fii_net"] = self._make_result(
                label="FII Net Buy/Sell",
                value=f"₹{fii_net_val:,.0f} Cr",
                direction=fii_dir,
                details=f"{'Net buying = liquidity boost' if fii_net_val > 0 else 'Net selling = bearish pressure'}",
                source="nse",
            )
            results["dii_net"] = self._make_result(
                label="DII Net Buy/Sell",
                value=f"₹{dii_net_val:,.0f} Cr",
                direction=dii_dir,
                details=f"{'DII buying cushions FII selling' if dii_net_val > 0 else 'DII also selling = double bearish'}",
                source="nse",
            )
        except Exception as e:
            results["fii_net"] = self._make_result(
                label="FII Net Buy/Sell", value="Parse Error", status="error",
                details=str(e), source="nse",
            )

        results["dii_ofs"] = self._compute_ofs_signal(dii_net_val if dii_net_val is not None else 0)
        results["fii_selloff"] = self._compute_fii_selloff_signal(
            fii_net_val if fii_net_val is not None else 0)

        fii_history = self._read_fii_history()
        fii_history.append({
            "ts": time.time(),
            "fii_net": fii_net_val if fii_net_val is not None else 0,
        })
        fii_history = fii_history[-10:]
        self._write_fii_history(fii_history)

        trend = self._compute_fii_trend(fii_history)
        results["fii_trend"] = self._make_result(
            label="FII Consecutive Days Trend",
            value=trend["summary"],
            direction=trend["direction"],
            details=trend["details"],
            source="nse+cache",
        )

        results["flows_manual"] = self._make_result(
            label="FII/DII Data Interpretation",
            value="Needs cross-checking",
            status="manual",
            manual=True,
            details="Net numbers alone can be misleading — check bulk/block deal breakdowns",
            source="manual",
        )

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results

    def _compute_ofs_signal(self, dii_net: float) -> FactorResult:
        ofs_headlines = fetch_news_headlines("government OFS divestment stake sale India", max_results=3)
        has_ofs_news = bool(ofs_headlines)
        headline_text = " | ".join(h["title"][:60] for h in ofs_headlines[:2]) if has_ofs_news else ""

        if dii_net < -1000 and has_ofs_news:
            return self._make_result(
                label="DII Selling for OFS",
                value="Suspected (DII selling + OFS news)",
                direction="bearish",
                details=f"DII net selling ₹{abs(dii_net):,.0f} Cr with OFS/divestment news: {headline_text}",
                source="nse+news",
            )
        if has_ofs_news:
            return self._make_result(
                label="DII Selling for OFS",
                value="OFS news present",
                status="warning",
                manual=True,
                details=f"OFS/divestment news: {headline_text}. "
                        f"Primary mkt absorbs cash = secondary liquidity drain = bearish.",
                source="news",
            )
        return self._make_result(
            label="DII Selling for OFS",
            value="No OFS news detected",
            details="No govt OFS/divestment headlines. Primary mkt absorbs cash = bearish when active.",
            source="news",
        )

    def _compute_fii_selloff_signal(self, fii_net: float) -> FactorResult:
        nifty_change = self._nifty_pct_change()
        if fii_net > 500 and nifty_change is not None and nifty_change < -0.5:
            return self._make_result(
                label="FII Buying During Selloff",
                value="YES — accumulation signal",
                direction="bullish",
                details=f"FII net buy ₹{fii_net:,.0f} Cr while Nifty {nifty_change:+.2f}% — "
                        f"FII accumulating at dips = long-term confidence",
                source="nse",
            )
        if fii_net > 500:
            return self._make_result(
                label="FII Buying During Selloff",
                value=f"FII buying ₹{fii_net:,.0f} Cr",
                direction="bullish",
                details=f"Nifty {nifty_change:+.2f}% — buying not during a selloff, but positive",
                source="nse",
            )
        return self._make_result(
            label="FII Buying During Selloff",
            value=f"FII not net buying ({fii_net:,.0f} Cr)",
            direction="neutral",
            status="warning",
            manual=True,
            details="Accumulation signal only when FII buy >500 Cr while Nifty falls >0.5%",
            source="nse",
        )

    def _nifty_pct_change(self) -> float:
        try:
            all_idx = get_all_indices()
            if not all_idx:
                return 0.0
            for idx in all_idx.get("data", []):
                if idx.get("index") == "NIFTY 50":
                    return float(idx.get("percentChange", 0) or 0)
        except Exception:
            pass
        return 0.0

    def _read_fii_history(self) -> list:
        try:
            f = os.path.join(CACHE_DIR, "fii_history.json")
            if os.path.exists(f):
                with open(f, "r") as fh:
                    return json.load(fh)
        except Exception:
            pass
        return []

    def _write_fii_history(self, data: list):
        try:
            f = os.path.join(CACHE_DIR, "fii_history.json")
            with open(f, "w") as fh:
                json.dump(data, fh)
        except Exception:
            pass

    def _compute_fii_trend(self, history: list) -> dict:
        if len(history) < 2:
            return {
                "summary": "Insufficient data",
                "direction": "neutral",
                "details": "Need 2+ days of history",
            }
        net_vals = [h["fii_net"] for h in history]
        consecutive = 1
        last_sign = 1 if net_vals[-1] > 0 else -1 if net_vals[-1] < 0 else 0
        for val in reversed(net_vals[:-1]):
            sign = 1 if val > 0 else -1 if val < 0 else 0
            if sign == last_sign and sign != 0:
                consecutive += 1
            else:
                break

        if last_sign > 0:
            direction = "bullish"
            summary = f"Buying × {consecutive} days"
        elif last_sign < 0:
            direction = "bearish"
            summary = f"Selling × {consecutive} days"
        else:
            direction = "neutral"
            summary = "Flat / no clear trend"

        return {
            "summary": summary,
            "direction": direction,
            "details": f"Last {len(history)} trading days. Sustained trend > single-day noise.",
        }
