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
    try:
        info_resp = scraper.get(
            f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}")
        if info_resp.status_code != 200:
            return None
        info = info_resp.json()
        expiry_dates = info.get("expiryDates") or []
        if not expiry_dates:
            return None
        url = (f"https://www.nseindia.com/api/option-chain-v3"
               f"?type=Indices&symbol={symbol}&expiry={expiry_dates[0]}")
        resp = scraper.get(url)
        if resp.status_code == 200:
            return resp.json()
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
