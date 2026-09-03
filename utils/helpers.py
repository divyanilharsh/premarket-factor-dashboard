import re
from datetime import datetime, timezone, timedelta
from typing import Optional


def extract_number(text) -> Optional[float]:
    """Extract the first usable numeric value from a display string.

    Handles currency symbols, thousands separators, percent signs and units.
    Returns None when the value carries no real figure (N/A, error, None).
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    low = text.lower()
    if any(token in low for token in (
        "n/a", " na ", "err", "unavailable", " none ", "no data",
        "not avail", "n.a", "closed", "loading", "---",
    )):
        return None
    # First signed number, tolerating commas as thousand separators.
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def format_timestamp(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = datetime.now(timezone(timedelta(hours=5, minutes=30))).timestamp()
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M IST")


def compute_direction_pct(current: float, previous: float,
                          bullish_up: bool = True, threshold: float = 0.001) -> str:
    if previous == 0:
        return "neutral"
    pct = (current - previous) / abs(previous)
    if abs(pct) < threshold:
        return "neutral"
    if bullish_up:
        return "bullish" if pct > 0 else "bearish"
    else:
        return "bearish" if pct > 0 else "bullish"


def pct_change_str(current: float, previous: float) -> str:
    if previous == 0:
        return "N/A"
    pct = ((current - previous) / abs(previous)) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def is_market_open() -> bool:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    hour = now.hour + now.minute / 60.0
    weekday = now.weekday()
    if weekday >= 5:
        return False
    return 9.0 <= hour < 15.5
