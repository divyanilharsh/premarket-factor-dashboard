import requests
import time
from typing import Optional

from config import USER_AGENT, NSE_BASE_URL, NSE_HOMEPAGE, NSE_RATE_LIMIT


class ScraperSession:
    _instance: Optional["ScraperSession"] = None
    _lock = None
    _request_lock = None

    def __new__(cls):
        import threading
        if cls._instance is None:
            cls._lock = threading.Lock()
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._session = requests.Session()
                    obj._session.headers.update({
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.5",
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/112.0.0.0 Safari/537.36",
                        "X-Requested-With": "XMLHttpRequest",
                    })
                    obj._last_request = 0
                    obj._nse_cookies_set = False
                    obj._request_lock = threading.Lock()
                    cls._instance = obj
        return cls._instance

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < NSE_RATE_LIMIT:
            time.sleep(NSE_RATE_LIMIT - elapsed)
        self._last_request = time.time()

    def get(self, url: str, **kwargs) -> requests.Response:
        with self._request_lock:
            self._rate_limit()
            resp = self._session.get(url, timeout=15, **kwargs)
        return resp

    def post(self, url: str, **kwargs) -> requests.Response:
        with self._request_lock:
            self._rate_limit()
            resp = self._session.post(url, timeout=15, **kwargs)
        return resp

    def warm_nse(self):
        with self._request_lock:
            if self._nse_cookies_set:
                return
            try:
                self._rate_limit()
                self._session.get(NSE_HOMEPAGE, timeout=15)
                self._nse_cookies_set = True
            except Exception:
                pass

    def refresh_nse_session(self):
        self._nse_cookies_set = False
        self.warm_nse()

    @property
    def session(self) -> requests.Session:
        return self._session
