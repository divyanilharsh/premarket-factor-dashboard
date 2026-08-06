from datetime import datetime, timezone, timedelta
from typing import Optional


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
