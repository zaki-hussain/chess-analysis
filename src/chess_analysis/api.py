"""Client for the chess.com published-data API (https://www.chess.com/news/view/published-data-api)."""

import time

import requests

BASE_URL = "https://api.chess.com/pub"
# chess.com asks API users to identify themselves with contact details.
USER_AGENT = "chess-analysis (+https://github.com/zaki-hussain/chess-analysis)"

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(Exception):
    pass


class NotFound(ApiError):
    pass


class ChessComClient:
    """Makes one request at a time: chess.com rate-limits parallel requests."""

    def __init__(
        self,
        session: requests.Session | None = None,
        max_retries: int = 4,
        backoff_seconds: float = 2.0,
        timeout_seconds: float = 30.0,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds

    def player(self, username: str) -> dict:
        return self._get(f"{BASE_URL}/player/{username}")

    def archive_urls(self, username: str) -> list[str]:
        """URLs of the player's monthly game archives, oldest first."""
        return self._get(f"{BASE_URL}/player/{username}/games/archives")["archives"]

    def month(self, url: str) -> dict:
        return self._get(url)

    def _get(self, url: str) -> dict:
        problem = ""
        for attempt in range(self.max_retries + 1):
            wait = self.backoff_seconds * 2**attempt
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = str(exc)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    raise NotFound(url)
                if response.status_code not in _RETRY_STATUSES:
                    raise ApiError(f"{url}: HTTP {response.status_code}")
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, int(retry_after))
            if attempt < self.max_retries:
                time.sleep(wait)
        raise ApiError(f"{url}: gave up after {self.max_retries + 1} attempts ({problem})")
