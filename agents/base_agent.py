import json
import os
import time
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import CACHE_DIR, CACHE_TTL_HOURS, AGENT_TIMEOUT, RETRY_ATTEMPTS


@dataclass
class FactorResult:
    label: str
    value: str = ""
    change: str = ""
    direction: str = "neutral"
    status: str = "ok"
    details: str = ""
    source: str = ""
    last_updated: str = ""
    manual: bool = False
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "value": self.value,
            "change": self.change,
            "direction": self.direction,
            "status": self.status,
            "details": self.details,
            "source": self.source,
            "last_updated": self.last_updated,
            "manual": self.manual,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FactorResult":
        return cls(
            label=d.get("label", ""),
            value=d.get("value", ""),
            change=d.get("change", ""),
            direction=d.get("direction", "neutral"),
            status=d.get("status", "ok"),
            details=d.get("details", ""),
            source=d.get("source", ""),
            last_updated=d.get("last_updated", ""),
            manual=d.get("manual", False),
            stale=d.get("stale", False),
        )


class BaseAgent(threading.Thread):
    agent_name: str = "base"
    cache_ttl_hours: float = CACHE_TTL_HOURS

    def __init__(self, callback=None):
        super().__init__(daemon=True)
        self.callback = callback
        self.results: dict[str, FactorResult] = {}
        self.error: Optional[str] = None
        self.completed = False
        self._stop_event = threading.Event()

    def fetch(self) -> dict[str, FactorResult]:
        raise NotImplementedError

    def run(self):
        try:
            self.results = self._fetch_with_retry()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.completed = True
            if self.callback:
                self.callback(self)

    def _fetch_with_retry(self) -> dict[str, FactorResult]:
        last_error = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                results = self.fetch()
                if results:
                    return results
            except Exception as e:
                last_error = e
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(2 * attempt)
        stale = self._load_cache(allow_stale=True)
        if stale:
            return self._cache_results(stale)
        raise last_error

    def stop(self):
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def _load_cache(self, allow_stale: bool = False) -> Optional[dict]:
        cache_file = os.path.join(CACHE_DIR, f"{self.agent_name}.json")
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("_timestamp", 0)
            ttl_hours = data.get("_cache_ttl_hours", CACHE_TTL_HOURS)
            if allow_stale:
                return data
            if time.time() - ts < ttl_hours * 3600:
                return data
        except Exception:
            pass
        return None

    def _cache_results(self, cache: dict) -> dict[str, FactorResult]:
        ts = cache.get("_timestamp", 0)
        ttl_hours = cache.get("_cache_ttl_hours", CACHE_TTL_HOURS)
        is_stale = bool(ts) and (time.time() - ts) >= ttl_hours * 3600
        results = {}
        for key, value in cache.items():
            if key.startswith("_"):
                continue
            r = FactorResult.from_dict(value)
            if is_stale:
                r.stale = True
                if r.status == "ok":
                    r.status = "warning"
                r.details = (r.details + " | " if r.details else "") + \
                    "STALE: cached data — refresh failed"
            results[key] = r
        return results

    def _save_cache(self, data: dict, ttl_hours: float = None):
        if ttl_hours is None:
            ttl_hours = self.cache_ttl_hours
        cache_file = os.path.join(CACHE_DIR, f"{self.agent_name}.json")
        data["_timestamp"] = time.time()
        data["_cache_ttl_hours"] = ttl_hours
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def results_from_cache(agent_name: str, allow_stale: bool = True) -> Optional[dict]:
        cache_file = os.path.join(CACHE_DIR, f"{agent_name}.json")
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            ts = cache.get("_timestamp", 0)
            ttl_hours = cache.get("_cache_ttl_hours", CACHE_TTL_HOURS)
            is_stale = bool(ts) and (time.time() - ts) >= ttl_hours * 3600
            results = {}
            for key, value in cache.items():
                if key.startswith("_"):
                    continue
                r = FactorResult.from_dict(value)
                if is_stale:
                    r.stale = True
                    if r.status == "ok":
                        r.status = "warning"
                    r.details = (r.details + " | " if r.details else "") + \
                        "STALE: cached data — refresh failed"
                results[key] = r
            return results
        except Exception:
            return None

    def _make_result(self, label: str, value: str = "", change: str = "",
                     direction: str = "neutral", status: str = "ok",
                     details: str = "", source: str = "", manual: bool = False) -> FactorResult:
        return FactorResult(
            label=label,
            value=value,
            change=change,
            direction=direction,
            status=status,
            details=details,
            source=source,
            last_updated=datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M IST"),
            manual=manual,
        )

    @staticmethod
    def _compute_direction(current: float, previous: float,
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

    @staticmethod
    def _pct_change(current: float, previous: float) -> str:
        if previous == 0:
            return "N/A"
        pct = ((current - previous) / abs(previous)) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"
