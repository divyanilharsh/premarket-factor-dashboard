from typing import Optional

from utils.scraper import ScraperSession


def fetch_rbi_repo_rate() -> Optional[str]:
    try:
        scraper = ScraperSession()
        resp = scraper.get("https://www.rbi.org.in/")
        if resp.status_code != 200:
            return None
        text = resp.text
        if "repo rate" in text.lower() or "6.50" in text or "6.75" in text:
            return "Check https://www.rbi.org.in for latest repo rate"
        return "Unable to parse - visit https://www.rbi.org.in"
    except Exception:
        return None


def fetch_forex_reserves_rbi() -> Optional[str]:
    try:
        scraper = ScraperSession()
        resp = scraper.get("https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx")
        if resp.status_code == 200 and "forex" in resp.text.lower():
            return "Latest data at RBI press releases"
        return "Check RBI weekly statistical supplement"
    except Exception:
        return None
