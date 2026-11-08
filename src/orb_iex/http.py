"""Read-only Alpaca REST client for Stage 1.

This class has a GET method and nothing else. It cannot submit, replace or
cancel orders: that code does not exist yet. Order submission will be added
in a later stage behind a separate, single choke point.

Every request:
- is checked against the host allowlist (paper trading + market data only);
- passes a per-host rate limiter (sliding 60-second window);
- is retried on 429 / 5xx / connection errors with backoff;
- is logged with its purpose, host, path, status and duration (never headers).
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from . import constants as C
from .logging_setup import LOGGER_NAME, log_event
from .safety import SafetyError, assert_allowed_url
from .secrets import Credentials

log = logging.getLogger(LOGGER_NAME)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 30.0


@dataclass
class ApiResponse:
    status: int
    data: Any
    headers: dict[str, str]
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class RateLimiter:
    """At most `per_minute` calls in any 60-second window."""

    def __init__(self, per_minute: int, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.per_minute = per_minute
        self.clock = clock
        self.sleep = sleep
        self.calls: deque[float] = deque()

    def acquire(self) -> float:
        waited = 0.0
        while True:
            now = self.clock()
            while self.calls and now - self.calls[0] >= 60.0:
                self.calls.popleft()
            if len(self.calls) < self.per_minute:
                self.calls.append(now)
                return waited
            pause = 60.0 - (now - self.calls[0]) + 0.01
            self.sleep(pause)
            waited += pause


class ReadOnlyClient:
    def __init__(
        self,
        credentials: Credentials,
        trading_per_minute: int,
        data_per_minute: int,
        timeout_seconds: int = 15,
        transport: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        max_attempts: int = 4,
    ):
        self._credentials = credentials
        self._transport = transport or requests.Session()
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._max_attempts = max_attempts
        self._limiters = {
            C.PAPER_TRADING_HOST: RateLimiter(trading_per_minute, clock, sleep),
            C.DATA_HOST: RateLimiter(data_per_minute, clock, sleep),
        }
        self.call_counts: Counter[str] = Counter()
        self.last_rate_limit_header: dict[str, str] = {}

    # -- public -----------------------------------------------------------
    def trading_get(self, path: str, params: dict | None = None, purpose: str = "") -> ApiResponse:
        return self._get(C.PAPER_TRADING_BASE_URL + path, params, purpose)

    def data_get(self, path: str, params: dict | None = None, purpose: str = "") -> ApiResponse:
        return self._get(C.DATA_BASE_URL + path, params, purpose)

    def _backoff(self, delay: float) -> float:
        """Full-ish jitter (75-100% of the capped delay) so retries from several callers do not align."""
        return min(delay, MAX_BACKOFF_SECONDS) * (0.75 + 0.25 * self._jitter())

    # -- internals ----------------------------------------------------------
    def _drop_connections(self) -> None:
        """A reset usually means the pooled keep-alive socket went stale (idle, or the host slept): close the
        pool so the retry dials a fresh connection instead of reusing the dead one."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001  closing a dead pool must never turn a retry into a failure
                log_event(log, logging.DEBUG, "http_pool_close", "closing the connection pool failed; retrying anyway")

    def _get(self, url: str, params: dict | None, purpose: str) -> ApiResponse:
        host = assert_allowed_url(url)  # raises SafetyError before any network I/O
        path = urlsplit(url).path
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            waited = self._limiters[host].acquire()
            self.call_counts[host] += 1
            started = self._clock()
            try:
                resp = self._transport.request(
                    "GET", url, params=params, headers=self._credentials.headers(),
                    timeout=self._timeout, allow_redirects=False,
                )
            except requests.RequestException as exc:
                last_exc = exc
                log_event(log, logging.WARNING, "http_error",
                          f"GET {host}{path} failed: {type(exc).__name__} (attempt {attempt})",
                          purpose=purpose, host=host, path=path, attempt=attempt)
                self._drop_connections()
                self._sleep(self._backoff(delay))
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue

            elapsed_ms = round((self._clock() - started) * 1000)
            if 300 <= resp.status_code < 400:
                raise SafetyError(f"unexpected redirect from {host}{path}; refusing to follow")
            headers = {k: v for k, v in resp.headers.items()
                       if k.lower().startswith("x-ratelimit")}
            if headers:
                self.last_rate_limit_header[host] = headers.get(
                    "X-RateLimit-Limit", headers.get("x-ratelimit-limit", ""))
            log_event(log, logging.DEBUG, "http_call",
                      f"GET {host}{path} -> {resp.status_code} in {elapsed_ms}ms ({purpose})",
                      purpose=purpose, host=host, path=path, status=resp.status_code,
                      elapsed_ms=elapsed_ms, rate_wait_s=round(waited, 3), attempt=attempt)

            if resp.status_code in RETRY_STATUSES and attempt < self._max_attempts:
                retry_after = resp.headers.get("Retry-After")
                pause = min(float(retry_after), MAX_BACKOFF_SECONDS) if retry_after and retry_after.isdigit() \
                    else self._backoff(delay)
                log_event(log, logging.WARNING, "http_retry",
                          f"GET {host}{path} -> {resp.status_code}; retrying in {pause:.1f}s",
                          purpose=purpose, status=resp.status_code, attempt=attempt)
                self._sleep(pause)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue

            try:
                data = resp.json()
            except ValueError:
                data = None
            return ApiResponse(resp.status_code, data, headers, resp.text[:500])

        raise ConnectionError(f"GET {host}{path} failed after {self._max_attempts} attempts") from last_exc
