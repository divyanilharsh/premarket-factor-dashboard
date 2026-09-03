import json
from typing import Optional

from config import (NSE_FII_DII_API, NSE_OPTION_CHAIN_CONTRACT_INFO,
                    NSE_OPTION_CHAIN_V3, NSE_BLOCK_DEALS_API,
                    NSE_GIFT_NIFTY_API, NSE_ALL_INDICES_API)
from utils.scraper import ScraperSession


def get_fii_dii_data() -> Optional[dict]:
    scraper = ScraperSession()
    scraper.refresh_nse_session()
    try:
        resp = scraper.get(NSE_FII_DII_API)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_all_indices() -> Optional[dict]:
    scraper = ScraperSession()
    scraper.refresh_nse_session()
    try:
        resp = scraper.get("https://www.nseindia.com/api/allIndices")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_option_chain(symbol: str = "NIFTY") -> Optional[dict]:
    scraper = ScraperSession()
    scraper.refresh_nse_session()
    headers = {"Referer": "https://www.nseindia.com/option-chain"}

    def _get(url: str):
        resp = scraper.get(url, headers=headers)
        if resp.status_code in (401, 403):
            # Session cookies expired / WAF re-challenge: flush and re-handshake.
            scraper.refresh_nse_session()
            resp = scraper.get(url, headers=headers)
        return resp

    try:
        info_resp = _get(
            f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}")
        if info_resp.status_code != 200:
            return None
        info = info_resp.json()
        expiry_dates = info.get("expiryDates") or []
        if not expiry_dates:
            return None
        url = (f"https://www.nseindia.com/api/option-chain-v3"
               f"?type=Indices&symbol={symbol}&expiry={expiry_dates[0]}")
        resp = _get(url)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_nifty_index_snapshot() -> Optional[dict]:
    """Return a NIFTY 50 snapshot {spot, prev_close, pct} from the all-indices feed.

    Used by the option-chain bias logic to incorporate the price-vs-OI quadrant
    (long buildup / short buildup / long unwinding / short covering).
    """
    data = get_all_indices()
    if not data:
        return None
    try:
        for idx in data.get("data", []):
            if idx.get("index") == "NIFTY 50":
                spot = idx.get("last")
                if spot is None:
                    return None
                prev = idx.get("previousClose")
                pct = idx.get("percentChange")
                return {
                    "spot": float(spot),
                    "prev_close": float(prev) if prev is not None else None,
                    "pct": float(pct) if pct is not None else None,
                }
    except Exception:
        pass
    return None


def get_block_deals() -> Optional[dict]:
    scraper = ScraperSession()
    scraper.refresh_nse_session()
    try:
        resp = scraper.get(
            "https://www.nseindia.com/api/NextApi/apiClient/egrApi"
            "?functionName=getEGRBlockDealData")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_gift_nifty() -> Optional[dict]:
    scraper = ScraperSession()
    scraper.refresh_nse_session()
    try:
        resp = scraper.get(
            "https://www.nseindia.com/api/NextApi/apiClient?functionName=getGiftNifty")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def compute_pcr(option_chain_data: dict) -> Optional[float]:
    try:
        records = option_chain_data.get("records", {})
        data = records.get("data", [])
        if not data:
            return None
        total_put_oi = 0
        total_call_oi = 0
        for entry in data:
            pe = entry.get("PE", {})
            ce = entry.get("CE", {})
            total_put_oi += pe.get("openInterest", 0) or 0
            total_call_oi += ce.get("openInterest", 0) or 0
        if total_call_oi == 0:
            return None
        return total_put_oi / total_call_oi
    except Exception:
        return None


def compute_option_chain_metrics(option_chain_data: dict,
                                 price_change_pct: Optional[float] = None) -> Optional[dict]:
    """Aggregate the key option-chain statistics for an index (e.g. NIFTY).

    Returns total OI, per-side OI changes, the highest call/put OI strikes and
    their change-in-OI (wall building vs unwinding), the overall put-call ratio,
    a near-the-money PCR computed over ±4 strikes around the spot, the
    OI-weighted average implied volatility, max pain, plus the underlying level
    and nearest expiry. Returns None when no data is usable.
    """
    if not option_chain_data:
        return None
    try:
        records = option_chain_data.get("records", {})
        data = records.get("data", [])
        if not data:
            return None

        spot = records.get("underlyingValue")
        spot = float(spot) if spot is not None else None

        total_call_oi = 0
        total_put_oi = 0
        total_call_chng = 0
        total_put_chng = 0
        iv_weighted_sum = 0.0
        iv_weight = 0.0

        ce_by_strike: dict = {}
        pe_by_strike: dict = {}
        call_by_strike: dict = {}
        put_by_strike: dict = {}

        max_call_oi = -1
        max_call_strike = None
        max_call_chng = 0
        max_put_oi = -1
        max_put_strike = None
        max_put_chng = 0

        for entry in data:
            strike = entry.get("strikePrice")
            ce = entry.get("CE", {}) or {}
            pe = entry.get("PE", {}) or {}

            ce_oi = int(ce.get("openInterest", 0) or 0)
            pe_oi = int(pe.get("openInterest", 0) or 0)
            ce_chng = int(ce.get("changeinOpenInterest", 0) or 0)
            pe_chng = int(pe.get("changeinOpenInterest", 0) or 0)

            total_call_oi += ce_oi
            total_put_oi += pe_oi
            total_call_chng += ce_chng
            total_put_chng += pe_chng

            if strike is not None:
                s = float(strike)
                ce_by_strike[s] = ce_oi
                pe_by_strike[s] = pe_oi
                if ce_oi:
                    call_by_strike[s] = max(call_by_strike.get(s, 0), ce_oi)
                if pe_oi:
                    put_by_strike[s] = max(put_by_strike.get(s, 0), pe_oi)
                if ce_oi > max_call_oi:
                    max_call_oi, max_call_strike, max_call_chng = ce_oi, s, ce_chng
                if pe_oi > max_put_oi:
                    max_put_oi, max_put_strike, max_put_chng = pe_oi, s, pe_chng

            # OI-weighted average IV across both sides.
            for side_oi, side in ((ce_oi, ce), (pe_oi, pe)):
                iv = side.get("impliedVolatility")
                if iv is None:
                    continue
                try:
                    iv = float(iv)
                except (TypeError, ValueError):
                    continue
                if side_oi > 0:
                    iv_weighted_sum += iv * side_oi
                    iv_weight += side_oi

        total_oi = total_call_oi + total_put_oi
        chng_oi = total_call_chng + total_put_chng
        pcr = (total_put_oi / total_call_oi) if total_call_oi else None
        avg_iv = (iv_weighted_sum / iv_weight) if iv_weight else None

        # Near-the-money PCR: only strikes within 4 steps above and below spot.
        atm_pcr = None
        atm_iv = None
        if spot is not None and call_by_strike:
            import bisect
            strikes = sorted(call_by_strike.keys())
            idx = bisect.bisect_left(strikes, spot)
            nearest = min(idx, len(strikes) - 1)
            lo = max(0, nearest - 4)
            hi = min(len(strikes) - 1, nearest + 4)
            window = strikes[lo:hi + 1]
            win_call = sum(ce_by_strike.get(s, 0) for s in window)
            win_put = sum(pe_by_strike.get(s, 0) for s in window)
            if win_call:
                atm_pcr = win_put / win_call
            # Windowed OI-weighted IV (near-the-money volatility).
            w_sum, w_w = 0.0, 0.0
            for s in window:
                for entry in data:
                    if entry.get("strikePrice") is not None and float(entry.get("strikePrice")) == s:
                        for side_oi, side in ((ce_by_strike.get(s, 0), entry.get("CE", {}) or {}),
                                              (pe_by_strike.get(s, 0), entry.get("PE", {}) or {})):
                            iv = side.get("impliedVolatility")
                            if iv is None:
                                continue
                            try:
                                iv = float(iv)
                            except (TypeError, ValueError):
                                continue
                            if side_oi > 0:
                                w_sum += iv * side_oi
                                w_w += side_oi
                        break
            atm_iv = (w_sum / w_w) if w_w else None

        return {
            "underlying": spot,
            "expiry": (records.get("expiryDates") or [None])[0],
            "total_oi": total_oi,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "chng_oi": chng_oi,
            "total_call_chng": total_call_chng,
            "total_put_chng": total_put_chng,
            "highest_call_oi": (max_call_strike, max_call_oi) if max_call_strike is not None else None,
            "highest_call_chng": max_call_chng,
            "highest_put_oi": (max_put_strike, max_put_oi) if max_put_strike is not None else None,
            "highest_put_chng": max_put_chng,
            "pcr": pcr,
            "atm_pcr": atm_pcr,
            "atm_iv": atm_iv,
            "avg_iv": avg_iv,
            "max_pain": compute_max_pain(option_chain_data),
            "nifty_pct": price_change_pct,
        }
    except Exception:
        return None


def compute_max_pain(option_chain_data: dict) -> Optional[float]:
    try:
        records = option_chain_data.get("records", {})
        data = records.get("data", [])
        if not data:
            return None
        strike_pain = {}
        for entry in data:
            pe = entry.get("PE", {})
            ce = entry.get("CE", {})
            strike = entry.get("strikePrice")
            if strike is None:
                continue
            pe_oi = pe.get("openInterest", 0) or 0
            ce_oi = ce.get("openInterest", 0) or 0
            total_pain = 0
            for inner in data:
                inner_strike = inner.get("strikePrice")
                if inner_strike is None:
                    continue
                i_pe_oi = (inner.get("PE", {}).get("openInterest", 0) or 0)
                i_ce_oi = (inner.get("CE", {}).get("openInterest", 0) or 0)
                if inner_strike < strike:
                    total_pain += (strike - inner_strike) * i_pe_oi
                elif inner_strike > strike:
                    total_pain += (inner_strike - strike) * i_ce_oi
            strike_pain[strike] = total_pain
        if not strike_pain:
            return None
        return min(strike_pain, key=strike_pain.get)
    except Exception:
        return None
