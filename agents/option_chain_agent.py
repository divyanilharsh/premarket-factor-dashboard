"""NIFTY option-chain agent.

Aggregates the NSE option chain into the statistics that matter for the
pre-market open and grades each one bull / bear / neutral using the
microstructure logic described below:

    - Total Open Interest (OI)
    - Change in Open Interest (Chng in OI)
    - Highest Call Open Interest (resistance)
    - Highest Put Open Interest (support)
    - Put-Call Ratio (PCR)
    - Near-the-Money PCR (ATM ±4 strikes)
    - Implied Volatility (IV)
    - Overall Option-Chain Bias (weighted composite)

Bull / bear interpretation (grounded in dealer-positioning research):

    PCR (OI) bands:  >1.30 extreme bull, 0.90-1.30 bullish, 0.70-0.90 neutral
    (equity baseline), 0.50-0.70 moderately bearish, <0.50 extreme bearish.
    Extreme readings are contrarian (overbought / oversold) markers.

    Change in OI x price-offset quadrant:
        price up   & OI up   = long buildup      -> bullish
        price down & OI up   = short buildup     -> bearish
        price down & OI down = long unwinding    -> bearish
        price up   & OI down = short covering    -> bullish

    Call wall (highest call OI): fresh writing (ΔOI>0) strengthens resistance
    -> bearish; unwinding (ΔOI<0) weakens it -> bullish.
    Put wall (highest put OI): fresh writing (ΔOI>0) builds support -> bullish;
    unwinding (ΔOI<0) is support capitulation -> bearish.
"""

from typing import Optional

from agents.base_agent import BaseAgent, FactorResult
from utils.nse_scraper import (get_option_chain, get_nifty_index_snapshot,
                               compute_option_chain_metrics)

# Ordered so the keys line up one-to-one with the factor items in factors.txt.
ITEM_META = [
    ("oi", "Total Open Interest (OI)"),
    ("chng_oi", "Change in Open Interest (Chng in OI)"),
    ("highest_call", "Highest Call Open Interest"),
    ("highest_put", "Highest Put Open Interest"),
    ("pcr", "Put-Call Ratio (PCR)"),
    ("atm_pcr", "Near-the-Money PCR (ATM ±4 strikes)"),
    ("iv", "Implied Volatility (IV)"),
    ("bias", "Option-Chain Bias"),
]

IV_HIGH = 18.0   # elevated IV warning threshold
IV_LOW = 14.0    # cheap-option threshold


class OptionChainAgent(BaseAgent):
    agent_name = "option_chain"
    cache_ttl_hours = 0.25

    def fetch(self) -> dict[str, FactorResult]:
        cache = self._load_cache()
        if cache:
            return {k: FactorResult.from_dict(v) for k, v in cache.items()
                    if not k.startswith("_")}

        chain = get_option_chain("NIFTY")
        metrics = compute_option_chain_metrics(chain) if chain else None
        if metrics is not None:
            snap = get_nifty_index_snapshot()
            if snap:
                metrics["nifty_pct"] = snap.get("pct")
                if metrics.get("underlying") is None:
                    metrics["underlying"] = snap.get("spot")

        if not metrics:
            return self._unavailable()

        results: dict[str, FactorResult] = {}
        results["oi"] = self._build_oi(metrics)
        results["chng_oi"] = self._build_chng_oi(metrics)
        results["highest_call"] = self._build_highest_call(metrics)
        results["highest_put"] = self._build_highest_put(metrics)
        results["pcr"] = self._build_pcr(metrics)
        results["atm_pcr"] = self._build_atm_pcr(metrics)
        results["iv"] = self._build_iv(metrics)
        results["bias"] = self._build_bias(metrics)

        cache_data = {k: v.to_dict() for k, v in results.items()}
        self._save_cache(cache_data, ttl_hours=1)
        return results

    # --- interpretation helpers --------------------------------------------

    @staticmethod
    def _pcr_band(pcr: Optional[float]):
        if pcr is None:
            return "neutral", "n/a"
        if pcr > 1.30:
            return "bullish", "extreme bullish (heavy put writing; contrarian top risk)"
        if pcr >= 0.90:
            return "bullish", "bullish / neutral drift"
        if pcr >= 0.70:
            return "neutral", "neutral / cautious (equity baseline 0.8-0.9)"
        if pcr >= 0.50:
            return "bearish", "moderately bearish (call writing dominates)"
        return "bearish", "extreme bearish (put liquidation; contrarian bounce risk)"

    @staticmethod
    def _quadrant(m: dict):
        price_up = m.get("nifty_pct") is not None and m["nifty_pct"] >= 0
        oi_up = m.get("chng_oi", 0) >= 0
        if price_up and oi_up:
            return ("bullish", "long buildup (price ↑, OI ↑)")
        if not price_up and oi_up:
            return ("bearish", "short buildup (price ↓, OI ↑)")
        if not price_up and not oi_up:
            return ("bearish", "long unwinding (price ↓, OI ↓)")
        return ("bullish", "short covering (price ↑, OI ↓)")

    @staticmethod
    def _vote(direction: str) -> int:
        return 1 if direction == "bullish" else -1 if direction == "bearish" else 0

    # --- per-field builders -------------------------------------------------

    def _build_oi(self, m: dict) -> FactorResult:
        total = m.get("total_oi", 0)
        call, put = m.get("total_call_oi", 0), m.get("total_put_oi", 0)
        return self._make_result(
            label="Total Open Interest (OI)",
            value=f"{total:,}",
            details=(f"Total NIFTY OI: {call:,} calls + {put:,} puts. High OI = "
                     f"strong institutional interest; OI at OTM puts = support "
                     f"floor, at OTM calls = overhead supply."),
            source="nse",
        )

    def _build_chng_oi(self, m: dict) -> FactorResult:
        chng = m.get("chng_oi", 0)
        sign = "+" if chng >= 0 else ""
        net = m.get("total_put_chng", 0) - m.get("total_call_chng", 0)
        if m.get("nifty_pct") is not None and chng:
            direction, label = self._quadrant(m)
        else:
            thr = 0.05 * (abs(m.get("total_call_chng", 0)) + abs(m.get("total_put_chng", 0))) or 1000
            if net > thr:
                direction, label = "bullish", "puts added > calls (support building)"
            elif net < -thr:
                direction, label = "bearish", "calls added > puts (resistance building)"
            else:
                direction, label = "neutral", "balanced call/put OI change"
        return self._make_result(
            label="Change in Open Interest (Chng in OI)",
            value=f"{sign}{chng:,}",
            direction=direction,
            details=(f"Net ΔOI {chng:+,} | {label}. Price ↑ with ΔOI ↑ = long "
                     f"buildup; price ↓ with ΔOI ↑ = short buildup."),
            source="nse",
        )

    def _build_highest_call(self, m: dict) -> FactorResult:
        val = m.get("highest_call_oi")
        if not val:
            return self._unavailable_one("Highest Call Open Interest")
        strike, oi = val
        chng = m.get("highest_call_chng", 0)
        if chng > 0:
            direction, wall = "bearish", "resistance building (fresh call writing)"
        elif chng < 0:
            direction, wall = "bullish", "resistance unwinding (call sellers covering)"
        else:
            direction, wall = "neutral", "resistance steady"
        return self._make_result(
            label="Highest Call Open Interest",
            value=f"{strike:,.0f} (OI {oi:,})",
            direction=direction,
            details=(f"Max call writers at {strike:,.0f} = major resistance. "
                     f"{wall} ({chng:+,}). Break above with ΔOI<0 = bullish."),
            source="nse",
        )

    def _build_highest_put(self, m: dict) -> FactorResult:
        val = m.get("highest_put_oi")
        if not val:
            return self._unavailable_one("Highest Put Open Interest")
        strike, oi = val
        chng = m.get("highest_put_chng", 0)
        if chng > 0:
            direction, wall = "bullish", "support building (fresh put writing)"
        elif chng < 0:
            direction, wall = "bearish", "support unwinding (put sellers capitulating)"
        else:
            direction, wall = "neutral", "support steady"
        return self._make_result(
            label="Highest Put Open Interest",
            value=f"{strike:,.0f} (OI {oi:,})",
            direction=direction,
            details=(f"Max put writers at {strike:,.0f} = major support. "
                     f"{wall} ({chng:+,}). Break below with ΔOI<0 = bearish."),
            source="nse",
        )

    def _build_pcr(self, m: dict) -> FactorResult:
        pcr = m.get("pcr")
        if pcr is None:
            return self._unavailable_one("Put-Call Ratio (PCR)")
        direction, band = self._pcr_band(pcr)
        return self._make_result(
            label="Put-Call Ratio (PCR)",
            value=f"{pcr:.2f}",
            direction=direction,
            details=(f"Total Put OI / Total Call OI = {pcr:.2f} — {band}. "
                     f"PCR>1.3 extreme bull (contrarian top), <0.5 extreme bear "
                     f"(contrarian bounce)."),
            source="nse",
        )

    def _build_atm_pcr(self, m: dict) -> FactorResult:
        pcr = m.get("atm_pcr")
        if pcr is None:
            return self._unavailable_one("Near-the-Money PCR (ATM ±4 strikes)")
        direction, band = self._pcr_band(pcr)
        spot = m.get("underlying")
        return self._make_result(
            label="Near-the-Money PCR (ATM ±4 strikes)",
            value=f"{pcr:.2f}",
            direction=direction,
            details=(f"PCR over ±4 strikes around spot{f' {spot:,.0f}' if spot else ''} "
                     f"= {pcr:.2f} — {band}. More current/ATM-sensitive than the "
                     f"full-chain PCR; best pulse of who is defending the money."),
            source="nse",
        )

    def _build_iv(self, m: dict) -> FactorResult:
        iv = m.get("avg_iv")
        if iv is None:
            return self._unavailable_one("Implied Volatility (IV)")
        atm_iv = m.get("atm_iv")
        underlying = m.get("underlying")
        expiry = m.get("expiry")
        max_pain = m.get("max_pain")
        pct = m.get("nifty_pct")

        status = "warning" if iv >= IV_HIGH else "ok"
        if pct is not None and pct < 0 and iv >= IV_HIGH:
            direction = "bearish"
        elif pct is not None and pct > 0 and iv < IV_LOW:
            direction = "bullish"
        else:
            direction = "neutral"

        details = (f"OI-weighted IV {iv:.2f}%"
                   f"{f' (ATM {atm_iv:.2f}%)' if atm_iv is not None else ''}"
                   f"{f' | spot {underlying:,.0f}' if underlying else ''}"
                   f"{f' | expiry {expiry}' if expiry else ''}"
                   f"{f' | max pain {max_pain:,.0f}' if max_pain else ''}. ")
        details += ("High IV = expensive options / fear; low IV = cheap. Falling "
                    "IV during a rally = steady accumulation (bullish); IV spikes "
                    "during a decline = panic put buying (bearish).")
        return self._make_result(
            label="Implied Volatility (IV)",
            value=f"{iv:.2f}%",
            direction=direction,
            status=status,
            details=details,
            source="nse",
        )

    def _build_bias(self, m: dict) -> FactorResult:
        votes: list[int] = []
        weights: list[float] = []

        def add(direction: str, w: float):
            votes.append(self._vote(direction))
            weights.append(w)

        signal = []

        d, band = self._pcr_band(m.get("pcr"))
        add(d, 3.0)
        signal.append(f"PCR {m.get('pcr'):.2f}={d}" if m.get("pcr") is not None else "PCR n/a")

        d, band = self._pcr_band(m.get("atm_pcr"))
        add(d, 2.0)
        signal.append(f"ATM PCR {m.get('atm_pcr'):.2f}={d}" if m.get("atm_pcr") is not None else "ATM PCR n/a")

        net = m.get("total_put_chng", 0) - m.get("total_call_chng", 0)
        thr = 0.05 * (abs(m.get("total_call_chng", 0)) + abs(m.get("total_put_chng", 0))) or 1000
        if net > thr:
            add("bullish", 2.0)
        elif net < -thr:
            add("bearish", 2.0)
        else:
            add("neutral", 2.0)
        signal.append(f"net ΔOI {net:+,}")

        cc = m.get("highest_call_chng", 0)
        add("bearish" if cc > 0 else "bullish" if cc < 0 else "neutral", 1.5)
        signal.append(f"call wall ΔOI {cc:+,}")

        pc = m.get("highest_put_chng", 0)
        add("bullish" if pc > 0 else "bearish" if pc < 0 else "neutral", 1.5)
        signal.append(f"put wall ΔOI {pc:+,}")

        if m.get("nifty_pct") is not None and m.get("chng_oi"):
            d, qlabel = self._quadrant(m)
            add(d, 2.0)
            signal.append(qlabel)

        total_w = sum(weights) or 1.0
        score = sum(v * w for v, w in zip(votes, weights)) / total_w

        if score >= 0.25:
            direction = "bullish"
        elif score <= -0.25:
            direction = "bearish"
        else:
            direction = "neutral"

        level = "strong" if abs(score) >= 0.5 else "moderate" if abs(score) >= 0.25 else "balanced"
        return self._make_result(
            label="Option-Chain Bias",
            value=f"{direction.upper()} ({score*100:+.0f}%)",
            direction=direction,
            details=(f"Weighted composite of OI-positioning signals. "
                     f"Score {score*100:+.0f}% ({level}). | " + " · ".join(signal)
                     + ". Extreme PCR readings are contrarian — fade the crowd."),
            source="nse",
        )

    # --- failure paths ------------------------------------------------------

    def _unavailable_one(self, label: str) -> FactorResult:
        return self._make_result(
            label=label, value="N/A", status="warning",
            details="Option chain data unavailable (NSE API restricted) — check manually",
            source="nse",
        )

    def _unavailable(self) -> dict[str, FactorResult]:
        return {key: self._unavailable_one(label) for key, label in ITEM_META}
