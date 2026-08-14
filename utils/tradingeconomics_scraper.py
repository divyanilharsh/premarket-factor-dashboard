import re
from typing import Optional

from utils.scraper import ScraperSession


def get_summary(path: str) -> Optional[str]:
    """Fetch the latest-release summary paragraph from a TradingEconomics
    indicator page (e.g. /india/interest-rate). Returns trimmed text."""
    url = f"https://tradingeconomics.com/{path.lstrip('/')}"
    try:
        scraper = ScraperSession()
        resp = scraper.get(url)
        if resp.status_code != 200:
            return None
        text = resp.text
        m = re.search(r'<span class=\'source-present\'>(.{0,200}?)</span>', text, re.S)
        if not m:
            m = re.search(r'<span class="source-present">(.{0,200}?)</span>', text, re.S)
        if not m:
            return None
        # find the parent h2/p text before the source span
        i = text.rfind("<h2", 0, m.start())
        if i < 0:
            i = text.rfind("<p", 0, m.start())
        if i < 0:
            return None
        segment = text[i:m.end()]
        segment = re.sub(r"<[^>]+>", " ", segment)
        segment = re.sub(r"\s+", " ", segment).strip()
        return segment[:500] or None
    except Exception:
        return None


def fetch_repo_policy() -> Optional[dict]:
    summary = get_summary("india/interest-rate")
    if not summary:
        return None
    rate = None
    m = re.search(r"repo rate(?: was|,)?\s*(?:held|left|kept)?\s*(?:unchanged)?\s*(?:at|by)?\s*([\d.]+)%", summary, re.I)
    if not m:
        m = re.search(r"([\d.]+)%\s*repo rate", summary, re.I)
    if m:
        try:
            rate = float(m.group(1))
        except ValueError:
            rate = None
    decision_text = summary[:260].lower()
    stance = "neutral"
    if re.search(r"\bhiked\b", decision_text) or re.search(r"\braised\b", decision_text) or "hawkish" in decision_text:
        stance = "hawkish"
    elif re.search(r"\bcut\b", decision_text) or re.search(r"\breduced\b", decision_text) or "dovish" in decision_text:
        stance = "dovish"
    elif "neutral" in decision_text:
        stance = "neutral"
    direction = "bearish" if stance == "hawkish" else "bullish" if stance == "dovish" else "neutral"
    return {
        "rate": rate,
        "stance": stance,
        "direction": direction,
        "summary": summary,
    }


def fetch_forex_reserves() -> Optional[dict]:
    summary = get_summary("india/foreign-exchange-reserves")
    if not summary:
        return None
    m = re.search(
        r"(?:increased|decreased|fell|rose|declined|grew)\s+to\s+([\d,]+)\s+USD Million"
        r"(?:[^.]*?)\s+from\s+([\d,]+)\s+USD Million", summary, re.I)
    value = prev = None
    if m:
        try:
            value = int(m.group(1).replace(",", ""))
            prev = int(m.group(2).replace(",", ""))
        except ValueError:
            value = prev = None
    direction = "neutral"
    if value is not None and prev is not None:
        direction = "bullish" if value >= prev else "bearish"
    return {
        "value": value,
        "previous": prev,
        "direction": direction,
        "summary": summary,
    }


def fetch_inflation() -> Optional[dict]:
    summary = get_summary("india/inflation-cpi")
    if not summary:
        return None
    m = re.search(
        r"(?:surged|rose|increased|fell|eased|declined|slowed|accelerated)\s+to\s+([\d.]+)%"
        r".{0,120}?\s+from\s+([\d.]+)%", summary, re.I)
    current = prev = None
    if m:
        try:
            current = float(m.group(1))
            prev = float(m.group(2))
        except ValueError:
            current = prev = None
    direction = "neutral"
    if current is not None and prev is not None:
        direction = "bearish" if current > prev else "bullish"
    return {
        "rate": current,
        "previous": prev,
        "direction": direction,
        "summary": summary,
    }


def fetch_trade_balance() -> Optional[dict]:
    exports_summary = get_summary("india/exports")
    imports_summary = get_summary("india/imports")
    if not exports_summary or not imports_summary:
        return None
    exports = None
    imports = None
    exports_prev = None
    imports_prev = None
    m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion.{0,80}?\s+from\s+\$?([\d.]+)\s*(?:USD\s*)?Billion",
                  exports_summary, re.I)
    if not m:
        m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion", exports_summary, re.I)
    if m:
        try:
            exports = float(m.group(1))
            exports_prev = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else None
        except (ValueError, IndexError):
            exports = None
    m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion.{0,80}?\s+from\s+\$?([\d.]+)\s*(?:USD\s*)?Billion",
                  imports_summary, re.I)
    if not m:
        m = re.search(r"to\s+\$?([\d.]+)\s*(?:USD\s*)?Billion", imports_summary, re.I)
    if m:
        try:
            imports = float(m.group(1))
            imports_prev = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else None
        except (ValueError, IndexError):
            imports = None
    balance = None
    if exports is not None and imports is not None:
        balance = exports - imports
    direction = "neutral"
    if balance is not None:
        direction = "bearish" if balance < 0 else "bullish"
    return {
        "exports": exports,
        "imports": imports,
        "exports_prev": exports_prev,
        "imports_prev": imports_prev,
        "balance": balance,
        "direction": direction,
        "summary": f"Exports: {exports_summary[:160]} | Imports: {imports_summary[:160]}",
    }


def fetch_industrial_production() -> Optional[dict]:
    summary = get_summary("india/industrial-production")
    if not summary:
        return None
    m = re.search(r"(?:rose|increased|fell|declined|expanded|grew|contracted)\s+(?:by\s+)?([\d.]+)%", summary, re.I)
    value = None
    if m:
        try:
            value = float(m.group(1))
        except ValueError:
            value = None
    if value is None:
        m = re.search(r"([\d.]+)%", summary)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                value = None
    direction = "neutral"
    if value is not None:
        direction = "bullish" if value >= 0 else "bearish"
    return {
        "value": value,
        "direction": direction,
        "summary": summary,
    }
