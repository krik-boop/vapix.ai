"""Shared read-only HTTP client: GET only, rate limiting, retries with exponential backoff."""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504}) | frozenset(range(505, 600))


class ApiError(RuntimeError):
    def __init__(self, status_code: int, path: str, body: str = "") -> None:
        self.status_code = status_code
        self.path = path
        self.body = body[:500]
        super().__init__(f"HTTP {status_code} for GET {path}: {self.body}")


class ReadOnlyViolation(RuntimeError):
    pass


class RateLimiter:
    """Allows at most `rps` calls per second by spacing calls at least 1/rps apart."""

    def __init__(
        self,
        rps: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self.min_interval = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now += wait
            self._next_allowed = now + self.min_interval


def _forbid_non_get(request: httpx.Request) -> None:
    if request.method != "GET":
        raise ReadOnlyViolation(f"Only GET requests are allowed, got {request.method}")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class ReadOnlyClient:
    """Thin wrapper around httpx.Client that can only issue GET requests."""

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        rps: float = 5.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = httpx.URL(base_url)
        self._http = httpx.Client(
            base_url=base_url,
            headers=dict(headers),
            timeout=timeout,
            transport=transport,
            event_hooks={"request": [_forbid_non_get]},
            follow_redirects=False,
        )
        self._limiter = RateLimiter(min(rps, 5.0), clock=clock, sleep=sleep)
        self._sleep = sleep
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    def set_header(self, name: str, value: str) -> None:
        self._http.headers[name] = value

    def close(self) -> None:
        self._http.close()

    def _check_same_origin(self, url: str) -> None:
        # Absolute URLs (e.g. collections[].url from hh.ru) must point at the configured
        # API host, otherwise the Authorization header would leak to a third party.
        target = httpx.URL(url)
        if target.is_absolute_url and (target.scheme, target.host) != (self._base.scheme, self._base.host):
            raise ReadOnlyViolation(f"Refusing to call foreign host {target.host!r}")

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        if response is not None:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                delay = min(self.backoff_max, max(delay, retry_after))
        return delay

    def get(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """GET with rate limiting and retries on 429/5xx/network errors.

        Returns the response for 2xx; raises ApiError otherwise.
        """
        self._check_same_origin(url)
        # httpx replaces (not merges) an existing query string when `params` is given,
        # so merge explicitly: hh.ru collection URLs already carry ?vacancy_id=...
        target = httpx.URL(url).copy_merge_params(params or {})
        path = target.path
        attempt = 0
        while True:
            self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = self._http.get(target)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise ApiError(0, path, f"transport error: {type(exc).__name__}") from None
                reason = type(exc).__name__
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRY_STATUSES or attempt >= self.max_retries:
                    raise ApiError(response.status_code, path, response.text)
                reason = f"HTTP {response.status_code}"
            delay = self._backoff(attempt, response)
            attempt += 1
            logger.warning("GET %s failed (%s), retry %d/%d in %.1fs",
                           path, reason, attempt, self.max_retries, delay)
            self._sleep(delay)
