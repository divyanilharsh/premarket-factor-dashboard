import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import MONEYCONTROL_CALENDAR_URL
from utils.scraper import ScraperSession


def fetch_results_due_today(max_results: int = 8) -> list[dict]:
    """Fetch today's scheduled results from Moneycontrol results calendar.

    The calendar page embeds a `resultCalendarData` JSON blob server-side.
    Returns [{date, stockName, resultType, ltp, change, time}].
    """
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    today = now.strftime("%Y-%m-%d")
    url = MONEYCONTROL_CALENDAR_URL.format(date=today)
    try:
        scraper = ScraperSession()
        resp = scraper.get(url)
        if resp.status_code != 200:
            return []
        html = resp.text
        data = _extract_result_calendar_json(html)
        if not data:
            return []
        table = data.get("tableData", {})
        entries = table.get("list", [])
        results = []
        for entry in entries:
            if _entry_is_today(entry, now):
                results.append({
                    "name": entry.get("stockName", ""),
                    "result_type": entry.get("resultType", ""),
                    "time": entry.get("time", ""),
                    "ltp": entry.get("ltp", ""),
                    "change": entry.get("change", ""),
                })
        return results[:max_results]
    except Exception:
        return []


def _extract_result_calendar_json(html: str) -> Optional[dict]:
    marker = "resultCalendarData"
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[j:k + 1])
    return None


def _entry_is_today(entry: dict, now: datetime) -> bool:
    date_str = entry.get("date", "")
    try:
        entry_date = datetime.strptime(date_str, "%d %b")
        return entry_date.month == now.month and entry_date.day == now.day
    except (ValueError, TypeError):
        return False
