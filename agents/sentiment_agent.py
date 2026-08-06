from datetime import datetime, timezone, timedelta

from agents.base_agent import BaseAgent, FactorResult
from utils.nse_scraper import (get_fii_dii_data, get_option_chain,
                                get_block_deals, get_all_indices,
                                compute_pcr, compute_max_pain)


class SentimentAgent(BaseAgent):
    agent_name = "sentiment"
    cache_ttl_hours = 0.25

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        results = {}

        all_indices = get_all_indices()

        results["vix"] = self._fetch_vix(all_indices)
        results["nifty_levels"] = self._fetch_nifty_levels(all_indices)
        results["fii_fno"] = self._fetch_fii_fno()
        results["pcr"] = self._fetch_pcr()
        results["cas"] = self._make_result(
            label="SEBI CAS Distortions",
            value="CAS rule active",
            status="manual",
            manual=True,
            details="Closing Auction Session can cause artificial end-day spikes and next-day unwinding",
            source="manual",
        )
        results["expiry"] = self._compute_expiry()
        results["block_deals"] = self._fetch_block_deals()
        results["market_breadth"] = self._fetch_market_breadth(all_indices)

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results

    def _find_index(self, all_indices: dict, name: str):
        if not all_indices:
            return None
        for idx in all_indices.get("data", []):
            if idx.get("index") == name:
                return idx
        return None

    def _fetch_vix(self, all_indices: dict) -> FactorResult:
        idx = self._find_index(all_indices, "INDIA VIX")
        if idx:
            val = float(idx.get("last", 0) or 0)
            pct = float(idx.get("percentChange", 0) or 0)
            direction = "bearish" if val > 20 else "bullish" if val < 12 else "neutral"
            return self._make_result(
                label="India VIX",
                value=f"{val:.2f}",
                change=f"{'+' if pct >= 0 else ''}{pct:.2f}%",
                direction=direction,
                details="High VIX (>20) = fear/weakness; low (<12) = complacency",
                source="nse",
            )
        try:
            import nsetools
            nse = nsetools.Nse()
            quote = nse.get_index_quote("INDIA VIX")
            if quote:
                val = float(quote.get("last", 0))
                pct_change_val = float(quote.get("pChange", 0))
                direction = "bearish" if val > 20 else "bullish" if val < 12 else "neutral"
                return self._make_result(
                    label="India VIX",
                    value=f"{val:.2f}",
                    change=f"{'+' if pct_change_val >= 0 else ''}{pct_change_val:.2f}%",
                    direction=direction,
                    details="High VIX (>20) = fear/weakness; low (<12) = complacency",
                    source="nsetools",
                )
        except Exception:
            pass
        return self._make_result(
            label="India VIX", value="N/A", status="warning",
            details="NSE data unavailable", source="nse",
        )

    def _fetch_nifty_levels(self, all_indices: dict) -> FactorResult:
        idx = self._find_index(all_indices, "NIFTY 50")
        if idx:
            val = float(idx.get("last", 0) or 0)
            high = float(idx.get("high", 0) or 0)
            low = float(idx.get("low", 0) or 0)
            prev = float(idx.get("previousClose", 0) or 0)
            return self._make_result(
                label="Nifty Support/Resistance",
                value=f"{val:,.0f}",
                change=self._pct_change(val, prev) if prev else "",
                details=f"Prev close: {prev:,.0f} | H: {high:,.0f} | L: {low:,.0f} | "
                        f"Breakout above high = buy; breakdown below low = sell",
                source="nse",
            )
        return self._make_result(
            label="Nifty Support/Resistance", value="N/A", status="warning",
            details="NSE data unavailable", source="nse",
        )

    def _fetch_fii_fno(self) -> FactorResult:
        fii_data = get_fii_dii_data()
        if fii_data:
            try:
                return self._make_result(
                    label="FII/DII F&O Positioning",
                    value="Provisional data available",
                    details="Heavy FII shorts = squeeze risk; longs = bullish. "
                            "Cross-check FII net figure in FOREIGN FLOWS section.",
                    source="nse",
                )
            except Exception:
                pass
        return self._make_result(
            label="FII/DII F&O Positioning", value="N/A", status="warning",
            details="NSE data unavailable", source="nse",
        )

    def _fetch_pcr(self) -> FactorResult:
        oc = get_option_chain("NIFTY")
        if oc:
            pcr = compute_pcr(oc)
            max_pain = compute_max_pain(oc)
            if pcr is not None:
                direction = "bearish" if pcr < 0.8 else "bullish" if pcr > 1.2 else "neutral"
                details = f"PCR: {pcr:.2f}"
                if max_pain is not None:
                    details += f" | Max Pain: {max_pain:,.0f}"
                return self._make_result(
                    label="PCR / Max Pain",
                    value=f"PCR {pcr:.2f}",
                    direction=direction,
                    details=details + " | Falling PCR = bearish; heavy call OI = ceiling",
                    source="nse",
                )
        return self._make_result(
            label="PCR / Max Pain", value="N/A", status="warning",
            details="Option chain unavailable (NSE API restricted) — check manually",
            source="nse",
        )

    def _compute_expiry(self) -> FactorResult:
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        month = now.month
        year = now.year
        last_day = 28
        d = datetime(year, month, last_day, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        while d.month != month:
            last_day -= 1
            d = datetime(year, month, last_day, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        while d.weekday() != 3:
            last_day -= 1
            d = datetime(year, month, last_day, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        expiry_str = d.strftime("%d %b %Y")
        days_left = (d - now).days
        is_today = d.date() == now.date()
        return self._make_result(
            label="Expiry Day",
            value="TODAY" if is_today else f"{expiry_str} ({days_left}d)",
            status="warning" if days_left <= 1 else "ok",
            details="Expiry can cause whipsaw, sharp swings, premium distortion",
            source="calendar",
        )

    def _fetch_block_deals(self) -> FactorResult:
        deals = get_block_deals()
        if deals:
            try:
                data = deals.get("data", [])
                timestamp = deals.get("timestamp", "")
                if isinstance(data, list) and data:
                    first = data[0]
                    detail_items = []
                    for item in data[:3]:
                        symbol = item.get("symbol", "")
                        change = item.get("pChange", 0)
                        value = item.get("totalTradedValue", 0)
                        detail_items.append(
                            f"{symbol} ({'+' if (change or 0) >= 0 else ''}{change or 0:.1f}%)")
                    details = "Large off-market trades move prices sharply. Top: " + ", ".join(detail_items)
                    if timestamp:
                        details += f" | As of {timestamp}"
                    return self._make_result(
                        label="Block / Bulk Deals",
                        value=f"{len(data)} deal(s)",
                        details=details,
                        source="nse",
                    )
                return self._make_result(
                    label="Block / Bulk Deals", value="None reported",
                    details="No block deals recorded today",
                    source="nse",
                )
            except Exception:
                pass
        return self._make_result(
            label="Block / Bulk Deals", value="N/A", status="warning",
            details="Block deals data unavailable (NSE API restricted) — check pre-market reports",
            source="nse",
        )

    def _fetch_market_breadth(self, all_indices: dict) -> FactorResult:
        if all_indices:
            advances = int(all_indices.get("advances", 0) or 0)
            declines = int(all_indices.get("declines", 0) or 0)
            total = advances + declines
            if total > 0:
                ratio = advances / total
                direction = "bullish" if ratio > 0.6 else "bearish" if ratio < 0.4 else "neutral"
                return self._make_result(
                    label="Market Breadth",
                    value=f"A:{advances} D:{declines}",
                    direction=direction,
                    details=f"Advance ratio: {ratio:.1%} | Broad positive breadth = strong open",
                    source="nse",
                )
        try:
            import nsetools
            nse = nsetools.Nse()
            ad = nse.get_advances_declines()
            if ad:
                advances = sum(v.get("advances", 0) for v in ad)
                declines = sum(v.get("declines", 0) for v in ad)
                total = advances + declines
                if total > 0:
                    ratio = advances / total
                    direction = "bullish" if ratio > 0.6 else "bearish" if ratio < 0.4 else "neutral"
                    return self._make_result(
                        label="Market Breadth",
                        value=f"A:{advances} D:{declines}",
                        direction=direction,
                        details=f"Advance ratio: {ratio:.1%} | Broad positive breadth = strong open",
                        source="nsetools",
                    )
        except Exception:
            pass
        return self._make_result(
            label="Market Breadth", value="N/A", status="warning",
            details="NSE data unavailable", source="nse",
        )
